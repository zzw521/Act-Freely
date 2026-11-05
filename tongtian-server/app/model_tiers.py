"""模型档位服务端供给（缺陷 D-44 服务端半边）。

档位表按「全局 / 组织」两级下发，个人账号的 org 声明是 `personal-<uid>`，
所以按账号下发与按组织下发走同一条路。表内容只有配置，没有凭据：
`byokAllowed` 表达「是否允许自带密钥」，密钥本身仍然只在本机。

版本口径：`revision` 由服务端单调递增，管理员改一次 +1，内容完全相同的重复
发布不产生新版本；`revert` 把历史某一版的内容作为**新版本**重新发布，而不是
把 `revision` 调回去——客户端「只接受递增版本」的判定因此永远成立，旧版本号
被拒绝，而运维确实需要的「回到上一版好策略」是一次可审计的新发布。

签名口径：`signature` 是 RS256 JWT，载荷里带 `digest = sha256(规范化文档字节)`，
公钥在既有的 `/.well-known/jwks.json`。客户端验签后必须再比一次 `digest`，
因为签名保护的是摘要，文档本身随响应一起传输。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .audit import write_audit
from .auth import ORG_RE, effective_org, require_admin, require_user
from .config import settings
from .db import now_ms, tx
from .errors import ApiError
from .keys import key_store

router = APIRouter()

TIER_CONTRACT_VERSION = "model-tiers.v1"
TIER_AUDIENCE = "tongtian-tiers"
TIERS = ("auto", "advanced", "basic", "economy")
CONCRETE_TIERS = ("advanced", "basic", "economy")
TIER_LABELS = {
    "auto": "自动",
    "advanced": "高级",
    "basic": "基础",
    "economy": "经济",
}
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,63}$")
SCOPE_KINDS = ("global", "org")

MAX_MODELS = 64
MAX_CHAIN = 8
MAX_RULES = 32
MAX_LABEL = 40
MAX_NOTE = 200
MAX_REVISIONS_PAGE = 50


# ── 文档模型 ────────────────────────────────────────────────


class TierModelIn(BaseModel):
    """档位链上的一跳：客户端 harness 仍按 id 自己解析端点，服务端只给标识与元数据。"""

    model_config = ConfigDict(extra="forbid")
    id: str
    provider: str = Field(default="", max_length=64)
    upstreamModel: str = Field(default="", max_length=120)
    displayName: str = Field(default="", max_length=60)
    contextWindow: int | None = Field(default=None, ge=1, le=1_000_000_000)
    maxOutputTokens: int | None = Field(default=None, ge=1, le=100_000_000)


class TierIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(default="", max_length=MAX_LABEL)
    chain: list[str] = Field(default_factory=list, max_length=MAX_CHAIN)
    byokAllowed: bool | None = None
    note: str = Field(default="", max_length=MAX_NOTE)


class AutoRuleIn(BaseModel):
    """auto 的选档依据：先按任务类型匹配，再按上下文长度上限匹配，命中即选。"""

    model_config = ConfigDict(extra="forbid")
    taskKind: str = Field(default="", max_length=40)
    maxContextTokens: int | None = Field(default=None, ge=1, le=1_000_000_000)
    tier: str


class AutoPolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[AutoRuleIn] = Field(default_factory=list, max_length=MAX_RULES)
    fallbackTier: str = "basic"


class TierDocumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contractVersion: str = TIER_CONTRACT_VERSION
    defaultTier: str = "auto"
    byokAllowed: bool = True
    models: list[TierModelIn] = Field(default_factory=list, max_length=MAX_MODELS)
    tiers: dict[str, TierIn] = Field(default_factory=dict)
    autoPolicy: AutoPolicyIn | None = None

    @model_validator(mode="after")
    def _check(self) -> "TierDocumentIn":
        if self.contractVersion != TIER_CONTRACT_VERSION:
            raise ValueError(f"contractVersion 必须是 {TIER_CONTRACT_VERSION}")
        for unknown in sorted(set(self.tiers) - set(TIERS)):
            raise ValueError(f"未知档位：{unknown}")
        if not (set(self.tiers) & set(CONCRETE_TIERS)):
            raise ValueError("至少要声明一个具体档位（advanced / basic / economy）")
        known = {item.id for item in self.models}
        if len(known) != len(self.models):
            raise ValueError("模型 id 重复")
        for name, tier in sorted(self.tiers.items()):
            if not tier.chain:
                raise ValueError(f"档位 {name} 的模型链为空")
            if len(set(tier.chain)) != len(tier.chain):
                raise ValueError(f"档位 {name} 的模型链有重复项")
            missing = [item for item in tier.chain if item not in known]
            if missing:
                raise ValueError(f"档位 {name} 引用未登记的模型：{missing[0]}")
        if self.defaultTier not in TIERS:
            raise ValueError(f"defaultTier 必须是 {'/'.join(TIERS)} 之一")
        if self.defaultTier == "auto":
            if self.autoPolicy is None and "auto" not in self.tiers:
                raise ValueError("缺省档位是 auto，但既没有 autoPolicy 也没有 auto 档的模型链")
        elif self.defaultTier not in self.tiers:
            raise ValueError(f"缺省档位 {self.defaultTier} 未声明")
        if self.autoPolicy is not None:
            declared = set(self.tiers) & set(CONCRETE_TIERS)
            if self.autoPolicy.fallbackTier not in declared:
                raise ValueError(
                    f"auto 回落档位 {self.autoPolicy.fallbackTier} 未声明为具体档位"
                )
            for rule in self.autoPolicy.rules:
                if rule.tier not in declared:
                    raise ValueError(f"auto 规则目标档位 {rule.tier} 未声明为具体档位")
        return self


# ── 规范化、摘要与签名 ──────────────────────────────────────


def canonical_bytes(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def document_digest(document: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def _sign(scope_kind: str, scope_id: str, revision: int, digest: str) -> tuple[str, str]:
    issued_at = int(now_ms() // 1000)
    claims = {
        "iss": settings.jwt_issuer,
        "aud": TIER_AUDIENCE,
        "typ": "model-tiers",
        "scope": f"{scope_kind}:{scope_id or '-'}",
        "rev": int(revision),
        "digest": digest,
        "iat": issued_at,
    }
    store = key_store()
    return store.sign(claims), store.current_kid


def verify_document(
    scope_kind: str, scope_id: str, revision: int, document: dict[str, Any], signature: str
) -> tuple[bool, str]:
    """验签并比对摘要；返回 (是否可信, 中文原因)。"""
    claims, reason = key_store().verify(signature, expected_audience=TIER_AUDIENCE)
    if not claims:
        return False, reason
    if claims.get("typ") != "model-tiers":
        return False, "签名载荷不是档位表类型"
    if claims.get("scope") != f"{scope_kind}:{scope_id or '-'}":
        return False, "签名作用域与档位表不一致"
    if int(claims.get("rev") or -1) != int(revision):
        return False, "签名版本号与档位表不一致"
    if str(claims.get("digest") or "") != document_digest(document):
        return False, "档位表内容与签名摘要不一致"
    return True, "可信"


# ── 存储 ────────────────────────────────────────────────────


def _normalize_scope(scope_kind: str, scope_id: str) -> tuple[str, str]:
    """全局作用域在路径里写成 `global/-`——空段在 URL 里表达不出来。"""
    kind = (scope_kind or "").strip().lower()
    ident = (scope_id or "").strip().lower()
    if kind not in SCOPE_KINDS:
        raise ApiError(400, "INVALID_SCOPE", "档位表作用域只能是 global 或 org")
    if kind == "global":
        if ident not in ("", "-"):
            raise ApiError(400, "INVALID_SCOPE", "全局档位表的 scopeId 需写成 -")
        return kind, ""
    if ident in ("", "-"):
        raise ApiError(400, "INVALID_SCOPE", "组织档位表必须给出组织标识")
    if not ORG_RE.match(ident):
        raise ApiError(400, "INVALID_SCOPE", "组织标识需为 1~64 位小写字母、数字、下划线或连字符")
    return kind, ident


def _row(conn: sqlite3.Connection, scope_kind: str, scope_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM model_tier_tables WHERE scope_kind = ?1 AND scope_id = ?2",
        (scope_kind, scope_id),
    ).fetchone()


def _load_document(row: sqlite3.Row) -> dict[str, Any]:
    try:
        payload = json.loads(row["document_json"])
    except (TypeError, json.JSONDecodeError):
        raise ApiError(500, "TIER_CORRUPT", "档位表损坏，请重新发布") from None
    return payload if isinstance(payload, dict) else {}


def _public_view(row: sqlite3.Row) -> dict[str, Any]:
    document = _load_document(row)
    return {
        "ok": True,
        "contractVersion": TIER_CONTRACT_VERSION,
        "scope": {"kind": row["scope_kind"], "id": row["scope_id"]},
        "revision": int(row["revision"]),
        "digest": str(row["digest"]),
        "signature": str(row["signature"]),
        "signingKid": str(row["signing_kid"]),
        "publishedAtMs": int(row["updated_at_ms"]),
        "document": document,
    }


def _etag(scope_kind: str, scope_id: str, revision: int) -> str:
    return f'"tiers-{scope_kind}-{scope_id or "global"}-{revision}"'


def resolve_scope(org_id: str) -> list[tuple[str, str]]:
    """按 org → global 的优先顺序给出候选作用域。"""
    candidates: list[tuple[str, str]] = []
    if org_id:
        candidates.append(("org", org_id))
    candidates.append(("global", ""))
    return candidates


def effective_for(conn: sqlite3.Connection, org_id: str) -> sqlite3.Row | None:
    for kind, ident in resolve_scope(org_id):
        row = _row(conn, kind, ident)
        if row is not None:
            return row
    return None


def _validate_body(body: dict[str, Any]) -> dict[str, Any]:
    try:
        document = TierDocumentIn.model_validate(body)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ()))
        raise ApiError(
            400,
            "TIER_INVALID",
            f"档位表不合法：{location} {first.get('msg', '')}".strip(),
        ) from None
    return document.model_dump(mode="json", exclude_none=False)


def publish(
    conn: sqlite3.Connection,
    *,
    scope_kind: str,
    scope_id: str,
    body: dict[str, Any],
    actor: str,
    action: str = "publish",
) -> dict[str, Any]:
    """发布一版：内容未变则复用当前版本，变了则 revision +1 并签名入历史。"""
    document = _validate_body(body)
    digest = document_digest(document)
    current = _row(conn, scope_kind, scope_id)
    if current is not None and str(current["digest"]) == digest:
        return _public_view(current)
    revision = int(current["revision"]) + 1 if current is not None else 1
    signature, kid = _sign(scope_kind, scope_id, revision, digest)
    stamp = now_ms()
    if current is None:
        conn.execute(
            "INSERT INTO model_tier_tables (scope_kind, scope_id, revision, document_json, "
            "signature, signing_kid, digest, updated_at_ms, updated_by) "
            "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            (
                scope_kind,
                scope_id,
                revision,
                json.dumps(document, ensure_ascii=False, separators=(",", ":")),
                signature,
                kid,
                digest,
                stamp,
                actor,
            ),
        )
    else:
        conn.execute(
            "UPDATE model_tier_tables SET revision = ?1, document_json = ?2, signature = ?3, "
            "signing_kid = ?4, digest = ?5, updated_at_ms = ?6, updated_by = ?7 "
            "WHERE scope_kind = ?8 AND scope_id = ?9",
            (
                revision,
                json.dumps(document, ensure_ascii=False, separators=(",", ":")),
                signature,
                kid,
                digest,
                stamp,
                actor,
                scope_kind,
                scope_id,
            ),
        )
    conn.execute(
        "INSERT INTO model_tier_revisions (scope_kind, scope_id, revision, document_json, "
        "signature, signing_kid, digest, action, actor, created_at_ms) "
        "VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
        (
            scope_kind,
            scope_id,
            revision,
            json.dumps(document, ensure_ascii=False, separators=(",", ":")),
            signature,
            kid,
            digest,
            action,
            actor,
            stamp,
        ),
    )
    row = _row(conn, scope_kind, scope_id)
    assert row is not None
    return _public_view(row)


def revisions_page(
    conn: sqlite3.Connection, scope_kind: str, scope_id: str, *, limit: int
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT revision, digest, action, signing_kid, created_at_ms FROM model_tier_revisions "
        "WHERE scope_kind = ?1 AND scope_id = ?2 ORDER BY revision DESC LIMIT ?3",
        (scope_kind, scope_id, max(1, min(MAX_REVISIONS_PAGE, int(limit)))),
    ).fetchall()
    return [
        {
            "revision": int(row["revision"]),
            "digest": str(row["digest"]),
            "action": str(row["action"]),
            "signingKid": str(row["signing_kid"]),
            "createdAtMs": int(row["created_at_ms"]),
        }
        for row in rows
    ]


def revision_document(
    conn: sqlite3.Connection, scope_kind: str, scope_id: str, revision: int
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT revision, document_json, signature, signing_kid, digest, created_at_ms, action "
        "FROM model_tier_revisions WHERE scope_kind = ?1 AND scope_id = ?2 AND revision = ?3",
        (scope_kind, scope_id, int(revision)),
    ).fetchone()
    if row is None:
        return None
    document = _load_document(row)
    return {
        "scope": {"kind": scope_kind, "id": scope_id},
        "revision": int(row["revision"]),
        "digest": str(row["digest"]),
        "signature": str(row["signature"]),
        "signingKid": str(row["signing_kid"]),
        "action": str(row["action"]),
        "publishedAtMs": int(row["created_at_ms"]),
        "document": document,
    }


def _utc_stamp(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 客户端拉取 ──────────────────────────────────────────────


@router.get("/api/v1/model-tiers/effective")
def api_tiers_effective(request: Request, user: dict = Depends(require_user)):
    """桌面/引擎登录后拉取档位表；ETag 命中返回 304，服务端没有表返回 404 让客户端回退本地配置。"""
    org_id = effective_org(user)
    with tx() as conn:
        row = effective_for(conn, org_id)
        if row is None:
            raise ApiError(
                404,
                "TIER_TABLE_NOT_FOUND",
                "服务端尚未下发模型档位表，客户端应继续使用本地 models.json 配置",
            )
        payload = _public_view(row)
        etag = _etag(str(row["scope_kind"]), str(row["scope_id"]), int(row["revision"]))
    if (request.headers.get("if-none-match") or "").strip() == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-store"})
    payload["source"] = "org" if payload["scope"]["kind"] == "org" else "global"
    payload["localFallbackSuggested"] = False
    return JSONResponse(
        payload,
        headers={
            "ETag": etag,
            "Cache-Control": "no-store",
            "X-TT-Tier-Revision": str(payload["revision"]),
        },
    )


@router.get("/api/v1/model-tiers/at")
def api_tiers_at(
    request: Request,
    revision: int = Query(..., ge=1),
    user: dict = Depends(require_user),
):
    """任务详情回看：按任务元数据里的 revision 取当时那一版的档位表（同一作用域链内可取）。"""
    org_id = effective_org(user)
    with tx() as conn:
        for kind, ident in resolve_scope(org_id):
            found = revision_document(conn, kind, ident, revision)
            if found is not None:
                found["source"] = "org" if kind == "org" else "global"
                return JSONResponse(
                    {"ok": True, **found},
                    headers={"Cache-Control": "no-store"},
                )
    raise ApiError(404, "TIER_REVISION_NOT_FOUND", "该版本不属于当前账号可见的档位表")


# ── 管理面 ──────────────────────────────────────────────────


@router.get("/api/v1/admin/model-tiers")
def api_admin_tiers_list(limit: int = Query(50), _admin: dict = Depends(require_admin)):
    size = max(1, min(MAX_REVISIONS_PAGE, int(limit)))
    with tx() as conn:
        rows = conn.execute(
            "SELECT scope_kind, scope_id, revision, digest, updated_at_ms, updated_by "
            "FROM model_tier_tables ORDER BY updated_at_ms DESC LIMIT ?1",
            (size,),
        ).fetchall()
    return {
        "ok": True,
        "contractVersion": TIER_CONTRACT_VERSION,
        "tiers": [{"id": name, "label": TIER_LABELS[name]} for name in TIERS],
        "tables": [
            {
                "scopeKind": row["scope_kind"],
                "scopeId": row["scope_id"],
                "revision": int(row["revision"]),
                "digest": str(row["digest"]),
                "updatedAtMs": int(row["updated_at_ms"]),
                "updatedBy": str(row["updated_by"]),
            }
            for row in rows
        ],
    }


@router.get("/api/v1/admin/model-tiers/{scope_kind}/{scope_id}")
def api_admin_tiers_get(
    scope_kind: str,
    scope_id: str,
    revision: int | None = Query(default=None, ge=1),
    revisions: int = Query(0, ge=0),
    _admin: dict = Depends(require_admin),
):
    kind, ident = _normalize_scope(scope_kind, scope_id)
    with tx() as conn:
        if revision is not None:
            found = revision_document(conn, kind, ident, revision)
            if found is None:
                raise ApiError(404, "TIER_REVISION_NOT_FOUND", "该版本不存在")
            return {"ok": True, **found}
        row = _row(conn, kind, ident)
        if row is None:
            raise ApiError(404, "TIER_TABLE_NOT_FOUND", "该作用域尚未发布档位表")
        payload = _public_view(row)
        payload["revisions"] = (
            revisions_page(conn, kind, ident, limit=revisions) if revisions > 0 else []
        )
    return {"ok": True, **payload}


@router.put("/api/v1/admin/model-tiers/{scope_kind}/{scope_id}")
def api_admin_tiers_put(
    scope_kind: str,
    scope_id: str,
    payload: dict[str, Any] = Body(...),
    admin: dict = Depends(require_admin),
):
    kind, ident = _normalize_scope(scope_kind, scope_id)
    actor = str(admin.get("uid") or "admin")
    with tx() as conn:
        before = _row(conn, kind, ident)
        before_view = (
            {"revision": int(before["revision"]), "digest": str(before["digest"])}
            if before is not None
            else {}
        )
        result = publish(
            conn, scope_kind=kind, scope_id=ident, body=payload, actor=actor
        )
        changed = before_view.get("revision") != result["revision"]
        if changed:
            write_audit(
                conn,
                actor=actor,
                action="model_tier.publish",
                object=f"{kind}:{ident or 'global'}",
                payload={
                    "revision": result["revision"],
                    "digest": result["digest"],
                    "signingKid": result["signingKid"],
                },
                before=before_view,
                after={
                    "revision": result["revision"],
                    "digest": result["digest"],
                },
            )
    return {"ok": True, "changed": changed, **result}


@router.post("/api/v1/admin/model-tiers/{scope_kind}/{scope_id}/revert")
def api_admin_tiers_revert(
    scope_kind: str,
    scope_id: str,
    payload: dict[str, Any] = Body(...),
    admin: dict = Depends(require_admin),
):
    """回到旧策略 = 把历史那一版的内容作为新版本重新发布；revision 仍然递增。"""
    kind, ident = _normalize_scope(scope_kind, scope_id)
    raw_revision = payload.get("revision") if isinstance(payload, dict) else None
    if not isinstance(raw_revision, int) or isinstance(raw_revision, bool) or raw_revision < 1:
        raise ApiError(400, "INVALID_REVISION", "revision 需为不小于 1 的整数")
    actor = str(admin.get("uid") or "admin")
    with tx() as conn:
        target = revision_document(conn, kind, ident, raw_revision)
        if target is None:
            raise ApiError(404, "TIER_REVISION_NOT_FOUND", "该版本不存在")
        current = _row(conn, kind, ident)
        if current is None:
            raise ApiError(404, "TIER_TABLE_NOT_FOUND", "该作用域尚未发布档位表，无需回滚")
        if int(current["revision"]) <= raw_revision:
            raise ApiError(400, "TIER_NOT_AHEAD", "只能回滚到当前版本之前的版本")
        before_view = {
            "revision": int(current["revision"]),
            "digest": str(current["digest"]),
        }
        result = publish(
            conn,
            scope_kind=kind,
            scope_id=ident,
            body=target["document"],
            actor=actor,
            action="revert",
        )
        if result["revision"] != before_view["revision"]:
            write_audit(
                conn,
                actor=actor,
                action="model_tier.revert",
                object=f"{kind}:{ident or 'global'}",
                payload={
                    "revertToRevision": raw_revision,
                    "revision": result["revision"],
                    "digest": result["digest"],
                },
                before=before_view,
                after={"revision": result["revision"], "digest": result["digest"]},
            )
    return {
        "ok": True,
        "revertToRevision": raw_revision,
        **result,
    }


@router.delete("/api/v1/admin/model-tiers/{scope_kind}/{scope_id}")
def api_admin_tiers_delete(
    scope_kind: str,
    scope_id: str,
    admin: dict = Depends(require_admin),
):
    """停止下发该作用域的档位表；历史版本保留，客户端下一次拉取会回落到上层作用域或本地配置。"""
    kind, ident = _normalize_scope(scope_kind, scope_id)
    actor = str(admin.get("uid") or "admin")
    with tx() as conn:
        row = _row(conn, kind, ident)
        if row is None:
            raise ApiError(404, "TIER_TABLE_NOT_FOUND", "该作用域尚未发布档位表")
        before_view = {
            "revision": int(row["revision"]),
            "digest": str(row["digest"]),
        }
        conn.execute(
            "DELETE FROM model_tier_tables WHERE scope_kind = ?1 AND scope_id = ?2",
            (kind, ident),
        )
        write_audit(
            conn,
            actor=actor,
            action="model_tier.withdraw",
            object=f"{kind}:{ident or 'global'}",
            payload=before_view,
            before=before_view,
            after={},
        )
    return {
        "ok": True,
        "withdrawn": {"scopeKind": kind, "scopeId": ident, **before_view},
        "clientBehavior": "回落到上层作用域；都没有时回退本地 models.json",
    }


@router.get("/api/v1/admin/model-tiers/verify")
def api_admin_tiers_verify(_admin: dict = Depends(require_admin)):
    """逐表验签巡检：任何一表的签名或摘要对不上都点名，供合规巡检与密钥轮换后自查。"""
    with tx() as conn:
        rows = conn.execute(
            "SELECT scope_kind, scope_id, revision, document_json, signature, signing_kid "
            "FROM model_tier_tables ORDER BY scope_kind, scope_id"
        ).fetchall()
        checked = []
        for row in rows:
            document = _load_document(row)
            trusted, reason = verify_document(
                str(row["scope_kind"]),
                str(row["scope_id"]),
                int(row["revision"]),
                document,
                str(row["signature"] or ""),
            )
            checked.append(
                {
                    "scopeKind": row["scope_kind"],
                    "scopeId": row["scope_id"],
                    "revision": int(row["revision"]),
                    "signingKid": str(row["signing_kid"] or ""),
                    "trusted": trusted,
                    "reason": reason,
                }
            )
    bad = [item for item in checked if not item["trusted"]]
    return {
        "ok": True,
        "tables": len(checked),
        "untrusted": len(bad),
        "items": checked,
    }
