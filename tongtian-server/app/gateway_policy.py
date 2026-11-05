"""网关策略：PolicyDocument 校验、按组织存储与分发（ETag/304）。

契约唯一来源是网关仓库的 CONTRACT.md §5；这里保留最小校验，避免控制面
误存会导致网关启动失败的文档。
"""

from __future__ import annotations

import hmac
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .auth import require_admin
from .config import settings
from .db import now_ms, tx
from .errors import ApiError
from .orgs import ensure_org

router = APIRouter()

CONTRACT_VERSION = "gateway-contract.v1"
TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
AMOUNT_RE = r"^(0|[1-9]\d*)(\.\d{1,8})?$"


class ModelPricing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    inputPer1M: str = Field(pattern=AMOUNT_RE)
    outputPer1M: str = Field(pattern=AMOUNT_RE)
    cachedInputPer1M: str | None = Field(default=None, pattern=AMOUNT_RE)
    reasoningAsOutput: bool = True


class ModelPolicy(BaseModel):
    model_config = ConfigDict(extra="allow")
    provider: str
    upstreamModel: str
    capabilities: list[str] = Field(default_factory=list)
    contextWindow: int | None = None
    maxOutputTokens: int | None = None
    priceVersion: str | None = None
    pricing: ModelPricing | None = None


class RoutePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chain: list[str]
    fallbackAllowed: bool = True


class QuotaDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    monthlyTokens: int | None = None
    concurrency: int | None = None
    rpm: int | None = None
    tpm: int | None = None
    maxOutputTokens: int | None = None


class DepartmentPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowedModels: list[str] | None = None
    quota: QuotaDefaults | None = None
    byokAllowed: bool | None = None


class ByokPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allowed: bool = True
    hardQuota: bool = False


class GatewayEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str


class PolicyDocumentIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    contractVersion: str = CONTRACT_VERSION
    policyVersion: str = ""
    version: str | None = None
    tenant: str
    gateway: GatewayEndpoint
    noLog: bool = True
    embeddingRoute: str | None = None
    byok: ByokPolicy = Field(default_factory=ByokPolicy)
    models: dict[str, ModelPolicy]
    routes: dict[str, RoutePolicy]
    allowedModels: list[str]
    quotaDefaults: QuotaDefaults = Field(default_factory=QuotaDefaults)
    departments: dict[str, DepartmentPolicy] = Field(default_factory=dict)
    issuedAt: str | None = None
    expiresAt: str | None = None

    @model_validator(mode="after")
    def _version_matches(self) -> "PolicyDocumentIn":
        if self.version and self.policyVersion and self.version != self.policyVersion:
            raise ValueError("version 必须与 policyVersion 相同")
        return self


def _normalize_tenant(raw: str) -> str:
    tenant = (raw or "").strip().lower()
    if not TENANT_RE.match(tenant):
        raise ApiError(400, "INVALID_TENANT", "租户标识不合法")
    return tenant


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_version(current: str) -> str:
    match = re.search(r"#(\d+)$", current or "")
    seq = int(match.group(1)) + 1 if match else 1
    return f"{_utc_stamp()}#{seq}"


def _load_row(conn: sqlite3.Connection, tenant: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT org_id, document_json, version, updated_at_ms FROM gateway_configs "
        "WHERE org_id = ?1",
        (tenant,),
    ).fetchone()


def _document(row: sqlite3.Row) -> dict:
    try:
        payload = json.loads(row["document_json"])
    except (TypeError, json.JSONDecodeError):
        raise ApiError(500, "POLICY_CORRUPT", "策略文档损坏，请重新保存") from None
    return payload if isinstance(payload, dict) else {}


def save_policy(tenant: str, payload: dict[str, Any]) -> dict:
    body = dict(payload)
    body["tenant"] = tenant
    body.pop("policyVersion", None)
    body.pop("version", None)
    try:
        document = PolicyDocumentIn.model_validate(body)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ()))
        message = f"策略文档不合法：{location} {first.get('msg', '')}".strip()
        raise ApiError(400, "POLICY_INVALID", message) from None
    with tx() as conn:
        row = _load_row(conn, tenant)
        version = _next_version(str(row["version"])) if row else _next_version("")
        data = document.model_dump(mode="json", exclude_none=False)
        data["policyVersion"] = version
        data["version"] = version
        if not data.get("issuedAt"):
            data["issuedAt"] = _utc_stamp()
        ensure_org(conn, tenant)
        conn.execute(
            "INSERT INTO gateway_configs (org_id, document_json, version, updated_at_ms) "
            "VALUES (?1, ?2, ?3, ?4) "
            "ON CONFLICT(org_id) DO UPDATE SET document_json = excluded.document_json, "
            "version = excluded.version, updated_at_ms = excluded.updated_at_ms",
            (
                tenant,
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                version,
                now_ms(),
            ),
        )
    return data


def require_service(request: Request) -> None:
    """网关 → 控制面服务调用的唯一鉴权：策略拉取与用量上报共用同一个令牌。"""
    expected = settings.gateway_service_token
    if not expected:
        raise ApiError(503, "AUTH_UNAVAILABLE", "控制面未配置网关服务令牌")
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token or not hmac.compare_digest(expected, token):
        raise ApiError(401, "AUTH_REQUIRED", "网关服务令牌无效")


@router.get("/api/v1/gateway/policy")
def api_gateway_policy(request: Request, tenant: str = Query("")):
    require_service(request)
    tenant_id = _normalize_tenant(tenant)
    with tx() as conn:
        row = _load_row(conn, tenant_id)
    if row is None:
        raise ApiError(404, "POLICY_NOT_FOUND", "该租户尚未配置网关策略")
    version = str(row["version"])
    etag = f'"{version}"'
    if_none_match = (request.headers.get("if-none-match") or "").strip()
    if if_none_match and if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-store"})
    return JSONResponse(
        _document(row),
        headers={"ETag": etag, "Cache-Control": "no-store"},
    )


@router.get("/api/v1/admin/gateway/policies")
def api_admin_policies(_admin: dict = Depends(require_admin)):
    with tx() as conn:
        rows = conn.execute(
            "SELECT org_id, version, updated_at_ms FROM gateway_configs "
            "ORDER BY updated_at_ms DESC"
        ).fetchall()
    return {
        "ok": True,
        "policies": [
            {
                "tenant": row["org_id"],
                "policyVersion": row["version"],
                "updatedAtMs": row["updated_at_ms"],
            }
            for row in rows
        ],
    }


@router.get("/api/v1/admin/gateway/policy/{tenant}")
def api_admin_get_policy(tenant: str, _admin: dict = Depends(require_admin)):
    tenant_id = _normalize_tenant(tenant)
    with tx() as conn:
        row = _load_row(conn, tenant_id)
    if row is None:
        raise ApiError(404, "POLICY_NOT_FOUND", "该租户尚未配置网关策略")
    return {
        "ok": True,
        "tenant": tenant_id,
        "policyVersion": row["version"],
        "updatedAtMs": row["updated_at_ms"],
        "document": _document(row),
    }


@router.put("/api/v1/admin/gateway/policy/{tenant}")
def api_admin_put_policy(
    tenant: str,
    payload: dict[str, Any] = Body(...),
    _admin: dict = Depends(require_admin),
):
    tenant_id = _normalize_tenant(tenant)
    document = save_policy(tenant_id, payload)
    return {
        "ok": True,
        "tenant": tenant_id,
        "policyVersion": document["policyVersion"],
        "document": document,
    }
