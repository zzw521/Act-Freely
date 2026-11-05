"""管理 API：provider 与密钥管理、健康、配额快照。

密钥明文只在创建/轮换响应出现一次；列表与审计只保留 id / last4。
"""

from __future__ import annotations

import hmac
import math
import os
import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import __version__
from .audit import export_ndjson, list_entries, verify as verify_audit
from .auth import Principal
from .config import settings
from .errors import GatewayError
from .mtls import resolve_admin_peer
from .quota import current_period, reset_at
from .ratelimit import admin_limiter
from .roles import (
    ADMIN,
    OPERATOR,
    allows,
    parse_depts,
    parse_roles,
    parse_tenant,
    strongest,
)
from .routing import quota_limits
from .state import (
    api_key_store,
    audit_webhook,
    db,
    policy_store,
    provider_store,
    quota_tracker,
    shared_state,
    usage_store,
    usage_uploader,
)

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
MAX_MONTHLY_TOKENS = 10**15


def _now() -> int:
    return int(time.time() * 1000)

router = APIRouter(prefix="/gateway/v1/admin", tags=["admin"])


def require_admin(request: Request, *, action: str = "read") -> None:
    peer = resolve_admin_peer(request)
    if settings.mtls_mode == "proxy" and not peer.verified:
        raise GatewayError.of(
            "MTLS_REQUIRED", "管理面要求 mTLS 客户端证书（经受信反向代理校验）"
        )
    retry_after = admin_limiter.consume(peer.rate_key)
    if retry_after > 0:
        raise GatewayError.of(
            "RATE_LIMITED",
            "管理接口请求过于频繁，请稍后重试",
            headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
        )
    request.state.admin_peer = peer
    token = request.headers.get("x-tt-admin-token", "")
    expected = settings.admin_token
    if not expected or not hmac.compare_digest(token, expected):
        raise GatewayError.of("AUTH_REQUIRED", "管理令牌无效")
    header_present = any(key.lower() == "x-tt-admin-roles" for key in request.headers)
    roles = parse_roles(request.headers)
    if not header_present:
        # 直连管理 API（运维脚本/验收）只持有共享令牌，按 root 处理
        roles = (ADMIN,)
    if not roles:
        raise GatewayError.of("FORBIDDEN", "管理角色无效")
    if not allows(roles, action):
        raise GatewayError.of("FORBIDDEN", "当前管理角色无权执行该操作")
    role = strongest(roles)
    depts = parse_depts(request.headers) if role == OPERATOR else None
    request.state.admin_role = role
    request.state.admin_depts = depts
    request.state.admin_tenant = parse_tenant(request.headers) if depts else ""


def _actor_role(request: Request) -> str:
    return getattr(request.state, "admin_role", ADMIN)


def _dept_scope(request: Request) -> tuple[str, ...] | None:
    return getattr(request.state, "admin_depts", None)


def _tenant_scope(request: Request) -> str:
    return getattr(request.state, "admin_tenant", "") or ""


def _require_global_scope(request: Request, what: str) -> None:
    if _dept_scope(request) is not None:
        raise GatewayError.of("FORBIDDEN", f"部门范围运维无权{what}")


def _check_dept(request: Request, dept: str) -> None:
    scope = _dept_scope(request)
    if scope is not None and dept not in scope:
        raise GatewayError.of("FORBIDDEN", f"部门 {dept} 不在当前管理范围")


def _check_tenant(request: Request, tenant: str) -> None:
    scope = _tenant_scope(request)
    if scope and tenant and tenant != scope:
        raise GatewayError.of("FORBIDDEN", f"租户 {tenant} 不在当前管理范围")


def _principal_parts(principal: str) -> tuple[str, str, str]:
    parts = principal.split(":", 2)
    if len(parts) != 3:
        return "", "", ""
    return parts[0].strip(), parts[1].strip(), parts[2].strip()


def _in_scope(request: Request, tenant: str, dept: str) -> bool:
    scope = _dept_scope(request)
    if scope is None:
        return True
    if dept not in scope:
        return False
    tenant_scope = _tenant_scope(request)
    return not tenant_scope or tenant == tenant_scope


def _principal_in_scope(request: Request, principal: str) -> bool:
    tenant, dept, _ = _principal_parts(principal)
    return _in_scope(request, tenant, dept)


def _actor(request: Request) -> str:
    """操作人：控制面代理带 uid；直连管理 API 时回落 mTLS 证书主体或 admin。"""
    actor = request.headers.get("x-tt-admin-actor", "").strip()
    if actor:
        return actor
    peer = getattr(request.state, "admin_peer", None)
    if peer is not None and peer.verified:
        return peer.subject or "mtls-client"
    return "admin"


async def _payload(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise GatewayError.of("INVALID_REQUEST", "请求体必须是 JSON 对象") from exc
    if not isinstance(body, dict):
        raise GatewayError.of("INVALID_REQUEST", "请求体必须是 JSON 对象")
    return body


@router.get("/health")
def admin_health(request: Request, tenant: str = "") -> dict:
    require_admin(request)
    snapshot = policy_store.snapshot(tenant.strip() or None)
    quotas = quota_tracker.snapshot()
    uploader = usage_uploader.status()
    return {
        "ok": True,
        "gatewayId": settings.gateway_id,
        "gatewayVersion": __version__,
        "environment": settings.environment,
        "workerPid": os.getpid(),
        "policyVersion": snapshot.policy_version,
        "policyStale": snapshot.stale,
        "providersConfigured": provider_store.configured_count(),
        "usageQueue": uploader["pending"],
        "usageUpload": uploader,
        "auditWebhook": audit_webhook.status(),
        "database": db.status(),
        "sharedState": shared_state.status(),
        "quotaPrincipals": len({row["principal"] for row in quotas}),
        "quotaResetAt": reset_at(),
    }


@router.get("/providers")
def list_providers(request: Request) -> dict:
    require_admin(request)
    return {"ok": True, "providers": provider_store.list_providers()}


@router.post("/providers")
async def create_provider(request: Request):
    require_admin(request, action="write")
    _require_global_scope(request, "管理 provider")
    payload = await _payload(request)
    provider = provider_store.create_provider(
        payload, actor=_actor(request), actor_role=_actor_role(request)
    )
    return JSONResponse(status_code=201, content={"ok": True, "provider": provider})


@router.patch("/providers/{provider_id}")
async def update_provider(provider_id: str, request: Request) -> dict:
    require_admin(request, action="write")
    _require_global_scope(request, "管理 provider")
    payload = await _payload(request)
    provider = provider_store.update_provider(
        provider_id, payload, actor=_actor(request), actor_role=_actor_role(request)
    )
    return {"ok": True, "provider": provider}


@router.post("/providers/{provider_id}/keys")
async def create_provider_key(provider_id: str, request: Request):
    require_admin(request, action="write")
    _require_global_scope(request, "管理 provider")
    payload = await _payload(request)
    label = payload.get("label", "")
    if not isinstance(label, str):
        raise GatewayError.of("INVALID_REQUEST", "label 必须是字符串", param="label")
    key = provider_store.add_key(
        provider_id,
        label=label,
        secret=payload.get("secret"),
        weight=payload.get("weight", 100),
        actor=_actor(request),
        actor_role=_actor_role(request),
    )
    return JSONResponse(status_code=201, content={"ok": True, "key": key})


@router.post("/providers/{provider_id}/keys/{key_id}/rotate")
async def rotate_provider_key(provider_id: str, key_id: str, request: Request) -> dict:
    require_admin(request, action="write")
    _require_global_scope(request, "管理 provider")
    payload = await _payload(request)
    result = provider_store.rotate_key(
        provider_id,
        key_id,
        secret=payload.get("secret"),
        grace_seconds=payload.get("graceSeconds", 0),
        actor=_actor(request),
        actor_role=_actor_role(request),
    )
    return {"ok": True, **result}


@router.delete("/providers/{provider_id}/keys/{key_id}")
def delete_provider_key(provider_id: str, key_id: str, request: Request) -> dict:
    require_admin(request, action="write")
    _require_global_scope(request, "管理 provider")
    result = provider_store.disable_key(
        provider_id, key_id, actor=_actor(request), actor_role=_actor_role(request)
    )
    return {"ok": True, **result}


@router.get("/usage")
def usage_report(
    request: Request,
    tenant: str = "",
    dept: str = "",
    sub: str = "",
    model: str = "",
    period: str = "monthly",
    limit: int = 500,
) -> dict:
    """网关本地账本聚合：按租户/部门/用户/模型/日/币种返回，可直接与手算对账。"""
    require_admin(request)
    if period not in ("daily", "monthly", "total"):
        raise GatewayError.of("INVALID_REQUEST", "period 只支持 daily/monthly/total")
    scope = _dept_scope(request)
    if scope is not None:
        dept = dept.strip()
        if dept:
            _check_dept(request, dept)
        elif len(scope) == 1:
            dept = scope[0]
        else:
            raise GatewayError.of(
                "INVALID_REQUEST",
                f"部门范围包含多个部门，请用 dept 指定（可选 {'/'.join(scope)}）",
                param="dept",
            )
        tenant_scope = _tenant_scope(request)
        if tenant_scope:
            if tenant.strip() and tenant.strip() != tenant_scope:
                raise GatewayError.of(
                    "FORBIDDEN", f"租户 {tenant.strip()} 不在当前管理范围"
                )
            tenant = tenant_scope
    result = usage_store.aggregate(
        tenant=tenant.strip(),
        dept=dept.strip(),
        sub=sub.strip(),
        model=model.strip(),
        period=period,
        limit=limit,
    )
    return {
        "ok": True,
        "gatewayId": settings.gateway_id,
        "period": period,
        "rows": [
            {
                "tenantId": row["tenant"],
                "departmentId": row["dept"],
                "userId": row["sub"],
                "model": row["model"],
                "provider": row["provider"],
                "day": row["day"],
                "currency": row["currency"],
                "calls": int(row["calls"]),
                "promptTokens": int(row["prompt_tokens"]),
                "completionTokens": int(row["completion_tokens"]),
                "totalTokens": int(row["total_tokens"]),
                "costMicros": int(row["cost_micros"]),
                "avgLatencyMs": int(row["latency_ms_total"]) // max(1, int(row["calls"])),
            }
            for row in result["rows"]
        ],
        "totals": result["totals"],
        "rowCount": result["rowCount"],
        "generatedAtMs": _now(),
    }


@router.get("/quotas")
def quota_snapshot(request: Request, period: str = "") -> dict:
    require_admin(request)
    target = period.strip()
    if target and not PERIOD_RE.match(target):
        raise GatewayError.of("INVALID_REQUEST", "period 必须是 YYYY-MM")
    quotas = quota_tracker.snapshot()
    overrides = quota_tracker.overrides(target or None)
    if _dept_scope(request) is not None:
        quotas = [
            row
            for row in quotas
            if _principal_in_scope(request, str(row["principal"]))
        ]
        overrides = [
            row
            for row in overrides
            if _principal_in_scope(request, str(row["principal"]))
        ]
    return {
        "ok": True,
        "resetAt": reset_at(),
        "period": target or current_period(),
        "quotas": quotas,
        "overrides": overrides,
    }


def _split_principal(principal: str) -> tuple[str, str, str]:
    parts = principal.split(":", 2)
    if len(parts) != 3 or not parts[0] or not parts[1]:
        raise GatewayError.of(
            "INVALID_REQUEST", "配额主体格式应为 tenant:dept:sub（部门级用 tenant:dept:*）"
        )
    tenant, dept, sub = (part.strip() for part in parts)
    if sub in ("", "*"):
        sub = ""
    return tenant, dept, sub


@router.patch("/quotas/{principal}")
async def update_quota(principal: str, request: Request) -> dict:
    """调整当月额度（写审计，立即生效）：monthlyTokens 为 null 时恢复策略值。"""
    require_admin(request, action="write")
    payload = await _payload(request)
    tenant, dept, sub = _split_principal(principal)
    _check_tenant(request, tenant)
    _check_dept(request, dept)
    if "monthlyTokens" not in payload:
        raise GatewayError.of("INVALID_REQUEST", "缺少 monthlyTokens（可为 null）")
    monthly = payload["monthlyTokens"]
    if monthly is not None:
        if isinstance(monthly, bool) or not isinstance(monthly, int):
            raise GatewayError.of("INVALID_REQUEST", "monthlyTokens 必须是整数或 null")
        if monthly < 0 or monthly > MAX_MONTHLY_TOKENS:
            raise GatewayError.of("INVALID_REQUEST", "monthlyTokens 超出允许范围")
    period = payload.get("period")
    if period is not None and (
        not isinstance(period, str) or not PERIOD_RE.match(period.strip())
    ):
        raise GatewayError.of("INVALID_REQUEST", "period 必须是 YYYY-MM")
    actor = request.headers.get("x-tt-admin-actor", "").strip() or "admin"

    result = quota_tracker.set_monthly_override(
        tenant,
        dept,
        sub,
        monthly,
        actor=actor,
        actor_role=_actor_role(request),
        period=period.strip() if isinstance(period, str) else None,
    )
    viewer = Principal(
        subject=sub,
        session_id="",
        tenant=tenant,
        department=dept,
        departments=(dept,),
        roles=("admin",),
        auth_kind="admin",
    )
    limits = quota_tracker.effective_limits(
        quota_limits(policy_store.snapshot(tenant).document, viewer),
        tenant,
        dept,
        sub,
    )
    usage = quota_tracker.used_tokens(tenant, dept, sub, result["period"])
    monthly_effective = limits.get("monthlyTokens")
    remaining = (
        max(0, int(monthly_effective) - usage["committedTokens"])
        if isinstance(monthly_effective, int)
        else None
    )
    return {
        "ok": True,
        "principal": f"{tenant}:{dept}:{sub or '*'}",
        "period": result["period"],
        "override": {"monthlyTokens": result["monthlyTokens"]},
        "previousMonthlyTokens": result["beforeMonthlyTokens"],
        "effectiveMonthlyTokens": monthly_effective,
        "usedTokens": usage["committedTokens"],
        "users": usage["users"],
        "remainingTokens": remaining,
        "resetAt": reset_at(),
        "updatedBy": actor,
    }


@router.post("/api-keys")
async def create_api_key(request: Request):
    require_admin(request, action="write")
    payload = await _payload(request)
    dept = payload.get("dept")
    if isinstance(dept, str) and dept.strip():
        _check_dept(request, dept.strip())
    tenant = payload.get("tenant")
    if isinstance(tenant, str) and tenant.strip():
        _check_tenant(request, tenant.strip())
    key = api_key_store.create(
        payload, actor=_actor(request), actor_role=_actor_role(request)
    )
    return JSONResponse(status_code=201, content={"ok": True, "key": key})


@router.get("/api-keys")
def list_api_keys(request: Request) -> dict:
    require_admin(request)
    keys = api_key_store.list()
    if _dept_scope(request) is not None:
        keys = [
            item
            for item in keys
            if _in_scope(request, str(item["tenant"]), str(item["dept"]))
        ]
    return {"ok": True, "keys": keys}


@router.post("/api-keys/{key_id}/rotate")
async def rotate_api_key(key_id: str, request: Request):
    require_admin(request, action="write")
    _check_dept(request, api_key_store.dept_of(key_id))
    key = api_key_store.rotate(
        key_id, actor=_actor(request), actor_role=_actor_role(request)
    )
    return {"ok": True, "key": key}


@router.delete("/api-keys/{key_id}")
def delete_api_key(key_id: str, request: Request) -> dict:
    require_admin(request, action="write")
    _check_dept(request, api_key_store.dept_of(key_id))
    result = api_key_store.revoke(
        key_id, actor=_actor(request), actor_role=_actor_role(request)
    )
    return {"ok": True, **result}


MAX_AUDIT_LIMIT = 1000


def _audit_page_args(after: int, limit: int) -> tuple[int, int]:
    if after < 0:
        raise GatewayError.of("INVALID_REQUEST", "after 必须是非负整数")
    if limit < 1 or limit > MAX_AUDIT_LIMIT:
        raise GatewayError.of(
            "INVALID_REQUEST", f"limit 必须在 1–{MAX_AUDIT_LIMIT} 之间"
        )
    return after, limit


@router.get("/audit")
def audit_entries(request: Request, after: int = 0, limit: int = 100) -> dict:
    require_admin(request)
    _require_global_scope(request, "读取审计")
    after, limit = _audit_page_args(after, limit)
    with db.read() as conn:
        page = list_entries(conn, after=after, limit=limit)
    return {"ok": True, **page}


@router.get("/audit/verify")
def audit_verify(request: Request) -> dict:
    require_admin(request)
    _require_global_scope(request, "读取审计")
    with db.read() as conn:
        result = verify_audit(conn)
    return {"ok": result["brokenAt"] is None, **result}


@router.get("/audit/export")
def audit_export(request: Request, after: int = 0, limit: int = 1000) -> Response:
    require_admin(request)
    _require_global_scope(request, "导出审计")
    after, limit = _audit_page_args(after, limit)
    with db.read() as conn:
        text, page = export_ndjson(conn, after=after, limit=limit)
    return Response(
        content=text.encode("utf-8"),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-store",
            "X-TT-Audit-Head": page["headHash"],
            "X-TT-Audit-Next": str(page["nextAfter"]),
        },
    )
