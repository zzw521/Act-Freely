"""网关 API Key（`ttg_`）：签发、列表、吊销与校验。

明文只在创建响应出现一次；库里只存 `sha256(secret)`。没有缓存，吊销与过期
从下一次请求起生效（契约要求 ≤30 秒）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Mapping

from .audit import append as append_audit
from .db import Database, now_ms
from .errors import GatewayError
from .quota import current_period

KEY_PREFIX = "ttg_"
SCOPES = ("chat", "embeddings", "models", "messages", "gemini")
KEY_ID_HEX = 12
SECRET_BYTES = 32
MAX_LABEL = 128
MAX_IDENTITY = 128
MAX_ALLOWLIST = 100
MAX_ALLOWLIST_ITEM = 128
MAX_RATE_LIMIT_RPM = 10_000
MAX_RATE_LIMIT_TPM = 10**9
MAX_MONTHLY_TOKENS = 10**15
TOKEN_RE = re.compile(r"^ttg_([0-9a-f]{12})_([A-Za-z0-9_-]{43})$")
_DUMMY_HASH = hashlib.sha256(b"ttg-dummy-secret").hexdigest()


@dataclass(frozen=True)
class ApiKeyRecord:
    key_id: str
    label: str
    tenant: str
    dept: str
    sub: str
    scopes: tuple[str, ...]
    model_allowlist: tuple[str, ...]
    rate_limit_rpm: int
    rate_limit_tpm: int
    quota_monthly_tokens: int
    expires_at_ms: int
    revoked_at_ms: int
    rotated_at_ms: int
    created_at_ms: int


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _invalid() -> GatewayError:
    return GatewayError.of("AUTH_REQUIRED", "API Key 无效或已失效")


class ApiKeyStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- 管理面 ----

    def create(
        self, payload: Mapping, *, actor: str = "admin", actor_role: str = "admin"
    ) -> dict:
        label = _label(payload.get("label", ""))
        tenant = _identity(payload.get("tenant"), "tenant")
        dept = _identity(payload.get("dept"), "dept")
        sub = _identity(payload.get("sub"), "sub")
        scopes = _scopes(payload.get("scopes"))
        allowlist = _allowlist(payload.get("modelAllowlist"))
        rate_limit = _rate_limit_rpm(payload.get("rateLimitRpm"))
        rate_limit_tpm = _rate_limit_tpm(payload.get("rateLimitTpm"))
        monthly_quota = _monthly_quota(payload.get("quotaMonthlyTokens"))
        expires_at = _expires(payload.get("expiresAt"))
        when = now_ms()
        for _ in range(3):
            key_id = secrets.token_hex(KEY_ID_HEX // 2)
            secret = base64.urlsafe_b64encode(
                secrets.token_bytes(SECRET_BYTES)
            ).rstrip(b"=").decode("ascii")
            try:
                with self._db.tx() as conn:
                    conn.execute(
                        "INSERT INTO api_keys(key_id, secret_hash, label, tenant, dept, "
                        "sub, scopes_json, model_allowlist_json, expires_at_ms, "
                        "revoked_at_ms, created_at_ms, rate_limit_rpm, rate_limit_tpm, "
                        "quota_monthly_tokens) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                        (
                            key_id,
                            _digest(secret),
                            label,
                            tenant,
                            dept,
                            sub,
                            json.dumps(list(scopes)),
                            json.dumps(list(allowlist)),
                            expires_at,
                            when,
                            rate_limit,
                            rate_limit_tpm,
                            monthly_quota,
                        ),
                    )
                    self._audit(
                        conn,
                        actor,
                        actor_role,
                        "api_key.create",
                        key_id,
                        {},
                        {
                            "label": label,
                            "tenant": tenant,
                            "dept": dept,
                            "sub": sub,
                            "scopes": list(scopes),
                            "modelAllowlist": list(allowlist),
                            "rateLimitRpm": rate_limit,
                            "rateLimitTpm": rate_limit_tpm,
                            "quotaMonthlyTokens": monthly_quota,
                            "expiresAt": expires_at or None,
                        },
                    )
                public = {
                    "id": key_id,
                    "label": label,
                    "tenant": tenant,
                    "dept": dept,
                    "sub": sub,
                    "scopes": list(scopes),
                    "modelAllowlist": list(allowlist),
                    "rateLimitRpm": rate_limit,
                    "rateLimitTpm": rate_limit_tpm,
                    "quotaMonthlyTokens": monthly_quota,
                    "expiresAt": expires_at or None,
                    "rotatedAt": None,
                    "revokedAt": None,
                    "createdAt": when,
                    "usedTokens": 0,
                }
                public["secret"] = f"{KEY_PREFIX}{key_id}_{secret}"
                return public
            except Exception as exc:  # noqa: BLE001 - 唯一约束异常因后端而异
                if not self._db.is_integrity_error(exc):
                    raise
                continue
        raise GatewayError.of("INTERNAL_ERROR", "生成 API Key 失败，请重试")

    def list(self) -> list[dict]:
        rows = self._db.query(
            "SELECT * FROM api_keys ORDER BY created_at_ms DESC, key_id"
        )
        used = {
            str(row["key_id"]): int(row["used_tokens"])
            for row in self._db.query(
                "SELECT key_id, used_tokens FROM api_key_counters WHERE period=?",
                (current_period(),),
            )
        }
        return [self._public(row, used.get(str(row["key_id"]), 0)) for row in rows]

    def dept_of(self, key_id: str) -> str:
        return str(self._row(key_id)["dept"])

    def rotate(
        self, key_id: str, *, actor: str = "admin", actor_role: str = "admin"
    ) -> dict:
        row = self._row(key_id)
        if int(row["revoked_at_ms"]) > 0:
            raise GatewayError.of(
                "INVALID_REQUEST", "API Key 已吊销，不能轮换", param="keyId"
            )
        previous = int(row["rotated_at_ms"]) or None
        when = now_ms()
        for _ in range(3):
            secret = base64.urlsafe_b64encode(
                secrets.token_bytes(SECRET_BYTES)
            ).rstrip(b"=").decode("ascii")
            try:
                with self._db.tx() as conn:
                    conn.execute(
                        "UPDATE api_keys SET secret_hash=?, rotated_at_ms=? "
                        "WHERE key_id=?",
                        (_digest(secret), when, key_id),
                    )
                    self._audit(
                        conn,
                        actor,
                        actor_role,
                        "api_key.rotate",
                        key_id,
                        {"rotatedAt": previous},
                        {"rotatedAt": when},
                    )
                break
            except Exception as exc:  # noqa: BLE001 - 两种后端的数据库异常
                if not self._db.is_db_error(exc):
                    raise
                continue
        else:
            raise GatewayError.of("INTERNAL_ERROR", "轮换 API Key 失败，请重试")
        refreshed = self._row(key_id)
        used = self._used(refreshed["key_id"])
        public = self._public(refreshed, used)
        public["secret"] = f"{KEY_PREFIX}{key_id}_{secret}"
        return public

    def revoke(
        self, key_id: str, *, actor: str = "admin", actor_role: str = "admin"
    ) -> dict:
        row = self._row(key_id)
        revoked_at = int(row["revoked_at_ms"])
        if revoked_at:
            return {"keyId": key_id, "revokedAt": revoked_at}
        when = now_ms()
        with self._db.tx() as conn:
            conn.execute(
                "UPDATE api_keys SET revoked_at_ms=? WHERE key_id=?",
                (when, key_id),
            )
            self._audit(
                conn,
                actor,
                actor_role,
                "api_key.revoke",
                key_id,
                {"revokedAt": None},
                {"revokedAt": when},
            )
        return {"keyId": key_id, "revokedAt": when}

    # ---- 请求期校验 ----

    def verify(self, token: str, *, now: int | None = None) -> ApiKeyRecord:
        parsed = TOKEN_RE.match(token if isinstance(token, str) else "")
        if parsed is None:
            _digest("")
            raise _invalid()
        key_id, secret = parsed.group(1), parsed.group(2)
        rows = self._db.query(
            "SELECT * FROM api_keys WHERE key_id=?", (key_id,)
        )
        if not rows:
            hmac.compare_digest(_digest(secret), _DUMMY_HASH)
            raise _invalid()
        row = rows[0]
        if not hmac.compare_digest(_digest(secret), str(row["secret_hash"])):
            raise _invalid()
        current = now if now is not None else now_ms()
        if int(row["revoked_at_ms"]) > 0:
            raise _invalid()
        expires_at = int(row["expires_at_ms"])
        if expires_at and current >= expires_at:
            raise _invalid()
        return ApiKeyRecord(
            key_id=str(row["key_id"]),
            label=str(row["label"]),
            tenant=str(row["tenant"]),
            dept=str(row["dept"]),
            sub=str(row["sub"]),
            scopes=tuple(json.loads(row["scopes_json"])),
            model_allowlist=tuple(json.loads(row["model_allowlist_json"])),
            rate_limit_rpm=int(row["rate_limit_rpm"]),
            rate_limit_tpm=int(row["rate_limit_tpm"]),
            quota_monthly_tokens=int(row["quota_monthly_tokens"]),
            expires_at_ms=expires_at,
            revoked_at_ms=int(row["revoked_at_ms"]),
            rotated_at_ms=int(row["rotated_at_ms"]),
            created_at_ms=int(row["created_at_ms"]),
        )

    # ---- 内部 ----

    def _row(self, key_id: str):
        rows = self._db.query("SELECT * FROM api_keys WHERE key_id=?", (key_id,))
        if not rows:
            raise GatewayError.of(
                "INVALID_REQUEST", f"API Key 不存在：{key_id}", param="keyId"
            )
        return rows[0]

    def _used(self, key_id: str) -> int:
        rows = self._db.query(
            "SELECT used_tokens FROM api_key_counters WHERE period=? AND key_id=?",
            (current_period(), key_id),
        )
        return int(rows[0]["used_tokens"]) if rows else 0

    @staticmethod
    def _public(row, used_tokens: int = 0) -> dict:
        return {
            "id": row["key_id"],
            "label": row["label"],
            "tenant": row["tenant"],
            "dept": row["dept"],
            "sub": row["sub"],
            "scopes": list(json.loads(row["scopes_json"])),
            "modelAllowlist": list(json.loads(row["model_allowlist_json"])),
            "rateLimitRpm": int(row["rate_limit_rpm"]),
            "rateLimitTpm": int(row["rate_limit_tpm"]),
            "quotaMonthlyTokens": int(row["quota_monthly_tokens"]),
            "expiresAt": int(row["expires_at_ms"]) or None,
            "rotatedAt": int(row["rotated_at_ms"]) or None,
            "revokedAt": int(row["revoked_at_ms"]) or None,
            "createdAt": int(row["created_at_ms"]),
            "usedTokens": used_tokens,
        }

    @staticmethod
    def _audit(
        conn,
        actor: str,
        actor_role: str,
        action: str,
        object_id: str,
        before,
        after,
    ) -> None:
        append_audit(
            conn,
            actor=actor,
            actor_role=actor_role,
            action=action,
            object_id=object_id,
            before=before,
            after=after,
        )


def _label(value) -> str:
    text = value if isinstance(value, str) else ""
    text = text.strip()
    if len(text) > MAX_LABEL or any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise GatewayError.of(
            "INVALID_REQUEST", f"label 不能超过 {MAX_LABEL} 位且不得含控制字符", param="label"
        )
    return text


def _identity(value, field: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if (
        not text
        or len(text) > MAX_IDENTITY
        or ":" in text
        or "*" in text
        or _unsafe(text)
    ):
        raise GatewayError.of(
            "INVALID_REQUEST",
            f"{field} 必须是 1–{MAX_IDENTITY} 位且不含冒号/星号/空白",
            param=field,
        )
    return text


def _unsafe(text: str) -> bool:
    if not text:
        return True
    return any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in text)


def _scopes(value) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise GatewayError.of(
            "INVALID_REQUEST", "scopes 必须是非空数组", param="scopes"
        )
    ordered: list[str] = []
    for item in value:
        scope = item.strip() if isinstance(item, str) else ""
        if scope not in SCOPES:
            raise GatewayError.of(
                "INVALID_REQUEST",
                f"未知 scope：{item!r}（可选 {'/'.join(SCOPES)}）",
                param="scopes",
            )
        if scope not in ordered:
            ordered.append(scope)
    if not ordered:
        raise GatewayError.of(
            "INVALID_REQUEST", "scopes 必须是非空数组", param="scopes"
        )
    return tuple(ordered)


def _allowlist(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise GatewayError.of(
            "INVALID_REQUEST", "modelAllowlist 必须是数组", param="modelAllowlist"
        )
    ordered: list[str] = []
    for item in value:
        model = item.strip() if isinstance(item, str) else ""
        if (
            not model
            or len(model) > MAX_ALLOWLIST_ITEM
            or _unsafe(model)
        ):
            raise GatewayError.of(
                "INVALID_REQUEST",
                f"modelAllowlist 每项必须是 1–{MAX_ALLOWLIST_ITEM} 位",
                param="modelAllowlist",
            )
        if model not in ordered:
            ordered.append(model)
    if len(ordered) > MAX_ALLOWLIST:
        raise GatewayError.of(
            "INVALID_REQUEST",
            f"modelAllowlist 最多 {MAX_ALLOWLIST} 项",
            param="modelAllowlist",
        )
    return tuple(ordered)


def _rate_limit_rpm(value) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise GatewayError.of(
            "INVALID_REQUEST", "rateLimitRpm 必须是整数", param="rateLimitRpm"
        )
    if value < 0 or value > MAX_RATE_LIMIT_RPM:
        raise GatewayError.of(
            "INVALID_REQUEST",
            f"rateLimitRpm 必须在 0–{MAX_RATE_LIMIT_RPM} 之间（0 表示不限）",
            param="rateLimitRpm",
        )
    return value


def _rate_limit_tpm(value) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise GatewayError.of(
            "INVALID_REQUEST", "rateLimitTpm 必须是整数", param="rateLimitTpm"
        )
    if value < 0 or value > MAX_RATE_LIMIT_TPM:
        raise GatewayError.of(
            "INVALID_REQUEST",
            f"rateLimitTpm 必须在 0–{MAX_RATE_LIMIT_TPM} 之间（0 表示不限）",
            param="rateLimitTpm",
        )
    return value


def _monthly_quota(value) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise GatewayError.of(
            "INVALID_REQUEST",
            "quotaMonthlyTokens 必须是整数",
            param="quotaMonthlyTokens",
        )
    if value < 0 or value > MAX_MONTHLY_TOKENS:
        raise GatewayError.of(
            "INVALID_REQUEST",
            f"quotaMonthlyTokens 必须在 0–{MAX_MONTHLY_TOKENS} 之间（0 表示不限）",
            param="quotaMonthlyTokens",
        )
    return value


def _expires(value) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise GatewayError.of(
            "INVALID_REQUEST", "expiresAt 必须是毫秒整数或 null", param="expiresAt"
        )
    if value <= int(time.time() * 1000):
        raise GatewayError.of(
            "INVALID_REQUEST", "expiresAt 必须晚于当前时间", param="expiresAt"
        )
    return value
