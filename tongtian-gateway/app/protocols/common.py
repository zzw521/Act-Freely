"""协议面公共部件：鉴权、请求体解析、SSE 编码与模型校验。"""

from __future__ import annotations

import json
import math
from typing import Any

from fastapi import Request

from ..auth import Principal, authenticate
from ..config import settings
from ..errors import GatewayError
from ..routing import RouteResolution, resolve_route


async def current_principal(request: Request) -> Principal:
    from ..state import api_key_limiter, api_key_store, policy_store

    principal = authenticate(
        request.headers, settings, api_keys=api_key_store
    )
    if principal.auth_kind == "api_key" and principal.api_key_rate_limit_rpm:
        retry_after = api_key_limiter.consume(
            principal.api_key_id or "", principal.api_key_rate_limit_rpm
        )
        if retry_after > 0:
            raise GatewayError.of(
                "RATE_LIMITED",
                f"API Key 请求速率超限（上限 {principal.api_key_rate_limit_rpm} RPM）",
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )
    # 先解析一次策略：控制面不可达且缓存超过宽限期时，请求在此返回 503，
    # 而不是等到计费/路由阶段才失败。
    snapshot = policy_store.snapshot(principal.tenant)
    request.state.policy_stale = snapshot.stale
    request.state.policy_version = snapshot.policy_version
    return principal


def scoped_principal(scope: str):
    """API Key 的端点级 scope 校验；JWT 主体不设 scope 限制。"""

    async def _principal(request: Request) -> Principal:
        principal = await current_principal(request)
        if principal.auth_kind == "api_key" and scope not in principal.scopes:
            raise GatewayError.of("FORBIDDEN", f"当前 API Key 缺少 {scope} 权限")
        return principal

    return _principal


async def json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都是客户端问题
        raise GatewayError.of("INVALID_REQUEST", "请求体必须是 JSON 对象") from exc
    if not isinstance(body, dict):
        raise GatewayError.of("INVALID_REQUEST", "请求体必须是 JSON 对象")
    return body


def sse(payload: dict | str) -> str:
    if isinstance(payload, str):
        return f"data: {payload}\n\n"
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"


def sse_event(name: str, payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {data}\n\n"


def require_header(request: Request, name: str, label: str | None = None) -> str:
    value = (request.headers.get(name) or "").strip()
    if not value:
        raise GatewayError.of(
            "INVALID_REQUEST", f"缺少 {label or name} 请求头", param=name
        )
    return value


def resolve_model(
    principal: Principal, model: Any, *, kind: str = "chat"
) -> RouteResolution:
    """校验模型/别名在当前部门可用，用于不产生上游调用的接口（如 count_tokens）。"""
    from ..state import policy_store

    if not isinstance(model, str) or not model.strip():
        raise GatewayError.of("INVALID_REQUEST", "缺少 model 参数", param="model")
    policy = policy_store.snapshot(principal.tenant).document
    return resolve_route(policy, principal, model.strip(), kind=kind)

