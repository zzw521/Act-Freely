"""网关管理代理（M1.4 T4.10）。

浏览器只与控制面打交道：`/api/v1/admin/gateway/*` 用平台管理员 JWT 鉴权，
由服务端补上 `X-TT-Admin-Token` 再转发到网关管理 API。这样管理令牌永远不进
浏览器、不进页面源码，也不随静态资源缓存。

代理不解析业务语义，只做四件事：
- 鉴权与越权拦截（未登录 401 / 非管理员 403）；
- 固定目标主机，拒绝 `..` 之类的路径穿越，避免被当成开放代理；
- 透传网关的管理信封与状态码，连接失败/超时给出稳定的错误码；
- 所有写操作落控制面审计（请求体按 `audit.redact` 脱敏，密钥只留 `***`）。

网关自己的审计仍由网关记录（provider/密钥/配额），`X-TT-Admin-Actor` 携带
操作人 uid，两边能对上同一个人。
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .audit import write_audit
from .auth import effective_org, require_gateway_staff
from .config import settings
from .db import tx
from .errors import ApiError
from .orgs import department_set
from .roles import OPERATOR, can_write_gateway, role_of

router = APIRouter()

PREFIX = "/api/v1/admin/gateway"
UPSTREAM_PREFIX = "/gateway/v1/admin"
AUDITED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_PROXY_BODY = 2 * 1024 * 1024


def _gateway_base() -> str:
    base = settings.gateway_admin_url
    if not base or not settings.gateway_admin_token:
        raise ApiError(
            503,
            "GATEWAY_ADMIN_UNAVAILABLE",
            "控制面未配置网关管理地址或管理令牌（TT_GATEWAY_ADMIN_URL/TT_GATEWAY_ADMIN_TOKEN）",
        )
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ApiError(503, "GATEWAY_ADMIN_UNAVAILABLE", "网关管理地址必须是 http(s) URL")
    return base


def _clean_path(raw: str) -> str:
    parts = [part for part in (raw or "").split("/") if part]
    if any(part in (".", "..") or "\\" in part or "\x00" in part for part in parts):
        raise ApiError(400, "GATEWAY_ADMIN_INVALID_PATH", "管理代理路径不合法")
    return "/".join(parts)


def _upstream_url(path: str, query: str) -> str:
    target = f"{_gateway_base()}{UPSTREAM_PREFIX}"
    if path:
        target = f"{target}/{path}"
    if query:
        target = f"{target}?{query}"
    return target


@lru_cache(maxsize=4)
def _ssl_context(cert: str, key: str, ca: str) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=ca or None)
    context.load_cert_chain(cert, key or None)
    return context


def _opener(url: str):
    """网关管理地址是 https 且配置了客户端证书时，用带证书的 opener 做 mTLS。"""
    if not url.startswith("https://") or not settings.gateway_admin_client_cert:
        return None
    try:
        context = _ssl_context(
            settings.gateway_admin_client_cert,
            settings.gateway_admin_client_key,
            settings.gateway_admin_ca,
        )
    except (OSError, ssl.SSLError) as exc:
        raise ApiError(
            503,
            "GATEWAY_ADMIN_TLS_CONFIG",
            f"控制面 mTLS 客户端证书不可用：{exc}",
        ) from None
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=context))


def _valid_scope_item(value: str) -> bool:
    if not value or len(value) > 128 or ":" in value or "*" in value or "," in value:
        return False
    return not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _operator_scope(staff: dict, role: str) -> tuple[str, ...]:
    """部门范围运维：按操作者当前成员部门下发；admin/auditor/未分配部门不下发。"""
    if role != OPERATOR:
        return ()
    uid = str(staff.get("uid") or "")
    tenant = effective_org(staff)
    if not uid or not tenant:
        return ()
    with tx() as conn:
        ids = department_set(conn, uid, tenant)
    return tuple(dept for dept in ids if _valid_scope_item(dept))


def _forward(
    method: str,
    url: str,
    body: bytes,
    content_type: str,
    actor: str,
    role: str,
    depts: str = "",
    tenant: str = "",
) -> tuple[int, bytes, str]:
    headers = {
        "X-TT-Admin-Token": settings.gateway_admin_token,
        "X-TT-Admin-Actor": actor,
        "X-TT-Admin-Roles": role,
        "Accept": "application/json, application/x-ndjson",
    }
    if depts:
        # 只有部门范围运维才下发；网关据此收窄 Key/配额/用量
        headers["X-TT-Admin-Depts"] = depts
        if tenant:
            headers["X-TT-Admin-Tenant"] = tenant
    if body:
        headers["Content-Type"] = content_type or "application/json"
    request = urllib.request.Request(url, data=body or None, headers=headers, method=method)
    opener = _opener(url)
    open_url = opener.open if opener is not None else urllib.request.urlopen
    try:
        with open_url(
            request, timeout=max(1, settings.gateway_proxy_timeout_seconds)
        ) as response:
            return (
                response.status,
                response.read(),
                response.headers.get("content-type", ""),
            )
    except urllib.error.HTTPError as exc:
        # 网关的管理错误信封要原样交给页面（401/403/400 都靠它显示原因）
        return exc.code, exc.read(), exc.headers.get("content-type", "")
    except (socket.timeout, TimeoutError):
        raise ApiError(504, "GATEWAY_ADMIN_TIMEOUT", "网关管理接口响应超时") from None
    except ssl.SSLError:
        raise ApiError(502, "GATEWAY_ADMIN_TLS_ERROR", "与网关的 mTLS 握手失败") from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise ApiError(504, "GATEWAY_ADMIN_TIMEOUT", "网关管理接口响应超时") from None
        if isinstance(reason, ssl.SSLError):
            raise ApiError(
                502, "GATEWAY_ADMIN_TLS_ERROR", "与网关的 mTLS 握手失败"
            ) from None
        raise ApiError(502, "GATEWAY_ADMIN_UNREACHABLE", "无法连接模型网关") from None


def _json_body(raw: bytes) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _audit(actor: str, role: str, method: str, path: str, raw: bytes, status: int) -> None:
    if method not in AUDITED_METHODS:
        return
    try:
        with tx() as conn:
            write_audit(
                conn,
                actor=actor,
                actor_role=role,
                action="gateway.admin",
                object=f"{method} {UPSTREAM_PREFIX}/{path}",
                payload={"role": role, "request": _json_body(raw), "responseStatus": status},
                status=status,
            )
    except Exception as exc:  # noqa: BLE001 - 审计失败不能盖住真实响应
        print(f"[server] 网关管理审计写入失败: {exc}", flush=True)


def _response(status: int, raw: bytes, content_type: str = "") -> Response:
    media = content_type.split(";", 1)[0].strip().lower()
    if media == "application/x-ndjson":
        # 审计导出走原始字节透传，浏览器不需要管理令牌
        return Response(
            content=raw,
            status_code=status,
            media_type=media,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    if raw and "json" not in media:
        raise ApiError(502, "GATEWAY_ADMIN_BAD_RESPONSE", "网关管理接口返回了非 JSON 响应")
    if not raw:
        return JSONResponse(
            {"ok": status < 400},
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )
    try:
        content = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ApiError(502, "GATEWAY_ADMIN_BAD_RESPONSE", "网关管理接口返回了非 JSON 响应") from None
    return JSONResponse(content, status_code=status, headers={"Cache-Control": "no-store"})


@router.api_route(
    f"{PREFIX}/{{path:path}}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def api_gateway_admin_proxy(
    path: str,
    request: Request,
    staff: dict = Depends(require_gateway_staff),
):
    clean = _clean_path(path)
    raw = await request.body()
    if len(raw) > MAX_PROXY_BODY:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "请求体超过 2MB 上限")
    actor = str(staff.get("uid") or "").strip() or "admin"
    role = role_of(staff)
    method = request.method.upper()
    if method in AUDITED_METHODS and not can_write_gateway(role):
        raise ApiError(403, "FORBIDDEN", "审计员只能查看网关管理数据")
    scope = _operator_scope(staff, role)
    if scope:
        if (clean == "providers" or clean.startswith("providers/")) and (
            method in AUDITED_METHODS
        ):
            raise ApiError(403, "FORBIDDEN", "部门范围运维不能修改 provider")
        if clean == "audit" or clean.startswith("audit/"):
            raise ApiError(403, "FORBIDDEN", "部门范围运维不能读取审计")
    url = _upstream_url(clean, request.url.query)
    status, body, content_type = await run_in_threadpool(
        _forward,
        method,
        url,
        raw,
        request.headers.get("content-type", ""),
        actor,
        role,
        ",".join(scope),
        effective_org(staff) if scope else "",
    )
    _audit(actor, role, method, clean, raw, status)
    return _response(status, body, content_type)
