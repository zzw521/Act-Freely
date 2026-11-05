"""Provider 与厂商密钥管理：管理 API 的存储层。

明文密钥只在创建/轮换响应返回一次；列表与审计只保留 id / last4。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Mapping

from .adapters.base import ModelTarget
from .audit import append as append_audit
from .db import Database, now_ms
from .errors import GatewayError
from .providers import PROVIDERS
from .secrets import Secrets
from .upstream import LOCAL_PROVIDERS, validate_provider_url, verify_target_url


@dataclass(frozen=True)
class ActiveKey:
    id: str
    secret: str


class ProviderStore:
    def __init__(
        self,
        db: Database,
        secrets: Secrets,
        *,
        allow_private_global: bool = False,
    ) -> None:
        self._db = db
        self._secrets = secrets
        self._allow_private_global = allow_private_global

    # ---- 查询 ----

    def list_providers(self) -> list[dict]:
        rows = {
            row["id"]: row
            for row in self._db.query("SELECT * FROM providers ORDER BY id")
        }
        payload = []
        for spec in PROVIDERS.values():
            if spec.id in rows:
                payload.append(self._provider_public(rows[spec.id]))
            else:
                payload.append(
                    {
                        "id": spec.id,
                        "type": spec.id,
                        "label": spec.label,
                        "baseUrl": "",
                        "apiVersion": None,
                        "timeoutSeconds": 120,
                        "allowPrivate": False,
                        "enabled": False,
                        "configured": False,
                        "keys": [],
                    }
                )
        return payload

    def get_provider(self, provider_id: str) -> dict:
        row = self._row(provider_id)
        return self._provider_public(row)

    def configured_count(self) -> int:
        rows = self._db.query(
            "SELECT COUNT(*) AS n FROM providers WHERE enabled = 1"
        )
        return int(rows[0]["n"]) if rows else 0

    def key_count(self) -> int:
        rows = self._db.query(
            "SELECT COUNT(*) AS n FROM provider_keys WHERE status = 'active'"
        )
        return int(rows[0]["n"]) if rows else 0

    # ---- provider 写操作 ----

    def create_provider(
        self, payload: Mapping, *, actor: str = "admin", actor_role: str = "admin"
    ) -> dict:
        provider_id = str(payload.get("id") or "").strip()
        spec = PROVIDERS.get(provider_id)
        if spec is None:
            raise GatewayError.of(
                "INVALID_REQUEST", f"未知 provider：{provider_id}", param="id"
            )
        base_url = str(payload.get("baseUrl") or "").strip()
        allow_private = bool(payload.get("allowPrivate", False))
        self._check_allow_private(allow_private, provider_id)
        validated_url = validate_provider_url(
            base_url,
            allow_private=allow_private or self._allow_private_global,
        )
        api_version = str(payload.get("apiVersion") or "").strip()
        timeout = self._timeout(payload.get("timeoutSeconds", 120))
        when = now_ms()
        label = str(payload.get("label") or spec.label).strip() or spec.label
        try:
            with self._db.tx() as conn:
                conn.execute(
                    "INSERT INTO providers(id, type, label, base_url, api_version, "
                    "timeout_seconds, allow_private, enabled, created_at_ms, updated_at_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (
                        provider_id,
                        spec.id,
                        label,
                        validated_url,
                        api_version,
                        timeout,
                        1 if allow_private else 0,
                        when,
                        when,
                    ),
                )
                self._audit(
                    conn,
                    actor,
                    actor_role,
                    "provider.create",
                    provider_id,
                    {},
                    self._safe_provider(
                        provider_id, label, validated_url, api_version, timeout, allow_private
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - 唯一约束异常因后端而异
            if not self._db.is_integrity_error(exc):
                raise
            raise GatewayError.of(
                "INVALID_REQUEST", f"provider 已存在：{provider_id}", param="id"
            ) from exc
        return self.get_provider(provider_id)

    def update_provider(
        self,
        provider_id: str,
        payload: Mapping,
        *,
        actor: str = "admin",
        actor_role: str = "admin",
    ) -> dict:
        row = self._row(provider_id)
        before = self._provider_public(row)
        label = str(payload.get("label", row["label"])).strip() or row["label"]
        base_url = str(payload.get("baseUrl", row["base_url"])).strip()
        api_version = str(payload.get("apiVersion", row["api_version"])).strip()
        timeout = self._timeout(payload.get("timeoutSeconds", row["timeout_seconds"]))
        allow_private = bool(payload.get("allowPrivate", bool(row["allow_private"])))
        enabled = bool(payload.get("enabled", bool(row["enabled"])))
        self._check_allow_private(allow_private, provider_id)
        validated_url = validate_provider_url(
            base_url,
            allow_private=allow_private or self._allow_private_global,
        )
        with self._db.tx() as conn:
            conn.execute(
                "UPDATE providers SET label=?, base_url=?, api_version=?, "
                "timeout_seconds=?, allow_private=?, enabled=?, updated_at_ms=? "
                "WHERE id=?",
                (
                    label,
                    validated_url,
                    api_version,
                    timeout,
                    1 if allow_private else 0,
                    1 if enabled else 0,
                    now_ms(),
                    provider_id,
                ),
            )
            self._audit(
                conn,
                actor,
                actor_role,
                "provider.update",
                provider_id,
                _public_before(before),
                self._safe_provider(
                    provider_id, label, validated_url, api_version, timeout, allow_private
                )
                | {"enabled": enabled},
            )
        return self.get_provider(provider_id)

    # ---- 密钥写操作 ----

    def add_key(
        self,
        provider_id: str,
        *,
        label: str,
        secret: str,
        weight: int = 100,
        actor: str = "admin",
        actor_role: str = "admin",
    ) -> dict:
        self._row(provider_id)
        if not isinstance(secret, str) or not secret:
            raise GatewayError.of("INVALID_REQUEST", "secret 不能为空", param="secret")
        if not isinstance(weight, int) or isinstance(weight, bool) or not 1 <= weight <= 100:
            raise GatewayError.of(
                "INVALID_REQUEST", "weight 必须在 1–100", param="weight"
            )
        encrypted = self._encrypt(secret)
        key_id = f"pk_{uuid.uuid4().hex}"
        when = now_ms()
        with self._db.tx() as conn:
            conn.execute(
                "INSERT INTO provider_keys(id, provider_id, label, ciphertext, nonce, "
                "key_version, last4, weight, status, created_at_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (
                    key_id,
                    provider_id,
                    label or "",
                    encrypted.ciphertext,
                    encrypted.nonce,
                    encrypted.key_version,
                    encrypted.last4,
                    weight,
                    when,
                ),
            )
            self._audit(
                conn,
                actor,
                actor_role,
                "provider.key.create",
                f"{provider_id}/{key_id}",
                {},
                {"id": key_id, "label": label or "", "last4": encrypted.last4,
                 "weight": weight},
            )
        return {
            "id": key_id,
            "label": label or "",
            "last4": encrypted.last4,
            "weight": weight,
            "createdAt": when,
            "secret": secret,
        }

    def rotate_key(
        self,
        provider_id: str,
        key_id: str,
        *,
        secret: str,
        grace_seconds: int = 0,
        actor: str = "admin",
        actor_role: str = "admin",
    ) -> dict:
        old = self._key_row(provider_id, key_id)
        if not isinstance(grace_seconds, int) or isinstance(grace_seconds, bool):
            raise GatewayError.of("INVALID_REQUEST", "graceSeconds 必须是整数")
        if not 0 <= grace_seconds <= 300:
            raise GatewayError.of(
                "INVALID_REQUEST", "graceSeconds 必须在 0–300", param="graceSeconds"
            )
        encrypted = self._encrypt(secret)
        new_id = f"pk_{uuid.uuid4().hex}"
        when = now_ms()
        retire_at = when + grace_seconds * 1000
        with self._db.tx() as conn:
            conn.execute(
                "UPDATE provider_keys SET status='disabled', disabled_at_ms=?, "
                "rotated_at_ms=? WHERE provider_id=? AND id=?",
                (when, when, provider_id, key_id),
            )
            conn.execute(
                "INSERT INTO provider_keys(id, provider_id, label, ciphertext, nonce, "
                "key_version, last4, weight, status, created_at_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
                (
                    new_id,
                    provider_id,
                    old["label"],
                    encrypted.ciphertext,
                    encrypted.nonce,
                    encrypted.key_version,
                    encrypted.last4,
                    old["weight"],
                    when,
                ),
            )
            self._audit(
                conn,
                actor,
                actor_role,
                "provider.key.rotate",
                f"{provider_id}/{key_id}->{new_id}",
                {"id": key_id, "last4": old["last4"]},
                {"id": new_id, "last4": encrypted.last4, "retireAt": retire_at},
            )
        return {
            "key": {
                "id": new_id,
                "label": old["label"],
                "last4": encrypted.last4,
                "weight": old["weight"],
                "createdAt": when,
                "secret": secret,
            },
            "previousKeyId": key_id,
            "retireAt": retire_at,
        }

    def disable_key(
        self,
        provider_id: str,
        key_id: str,
        *,
        actor: str = "admin",
        actor_role: str = "admin",
    ) -> dict:
        row = self._key_row(provider_id, key_id)
        if row["status"] == "disabled":
            return {"keyId": key_id, "disabledAt": int(row["disabled_at_ms"])}
        when = now_ms()
        with self._db.tx() as conn:
            conn.execute(
                "UPDATE provider_keys SET status='disabled', disabled_at_ms=? "
                "WHERE provider_id=? AND id=?",
                (when, provider_id, key_id),
            )
            self._audit(
                conn,
                actor,
                actor_role,
                "provider.key.disable",
                f"{provider_id}/{key_id}",
                {"status": row["status"]},
                {"status": "disabled", "disabledAt": when},
            )
        return {"keyId": key_id, "disabledAt": when}

    # ---- 请求期解析 ----

    def active_key(self, provider_id: str) -> ActiveKey:
        rows = self._db.query(
            "SELECT * FROM provider_keys WHERE provider_id=? AND status='active' "
            "ORDER BY created_at_ms DESC, weight DESC LIMIT 1",
            (provider_id,),
        )
        if not rows:
            raise GatewayError.of(
                "PROVIDER_UNAVAILABLE", f"provider {provider_id} 没有可用密钥"
            )
        row = rows[0]
        try:
            secret = self._secrets.decrypt(
                row["ciphertext"], row["nonce"], int(row["key_version"])
            )
        except (ValueError, RuntimeError) as exc:
            raise GatewayError.of(
                "INTERNAL_ERROR", "厂商密钥无法解密，请检查主密钥配置"
            ) from exc
        return ActiveKey(id=row["id"], secret=secret)

    def resolve_target(
        self,
        provider_id: str,
        *,
        model: str,
        upstream_model: str,
        alias: str | None = None,
    ) -> ModelTarget:
        rows = self._db.query("SELECT * FROM providers WHERE id=?", (provider_id,))
        if not rows:
            raise GatewayError.of(
                "PROVIDER_UNAVAILABLE", f"provider {provider_id} 未配置"
            )
        row = rows[0]
        if not row["enabled"]:
            raise GatewayError.of(
                "PROVIDER_UNAVAILABLE", f"provider {provider_id} 已停用"
            )
        allow_private = bool(row["allow_private"]) or self._allow_private_global
        verify_target_url(row["base_url"], allow_private=allow_private)
        key = self.active_key(provider_id)
        return ModelTarget(
            provider=row["type"],
            model=model,
            upstream_model=upstream_model,
            alias=alias,
            base_url=row["base_url"],
            api_key=key.secret,
            api_version=row["api_version"] or None,
            timeout_seconds=int(row["timeout_seconds"]),
        )

    # ---- 内部 ----

    def _check_allow_private(self, allow_private: bool, provider_id: str) -> None:
        if (
            allow_private
            and provider_id not in LOCAL_PROVIDERS
            and not self._allow_private_global
        ):
            raise GatewayError.of(
                "INVALID_REQUEST",
                f"allowPrivate 只对 {'/'.join(sorted(LOCAL_PROVIDERS))} 生效",
                param="allowPrivate",
            )

    @staticmethod
    def _timeout(value) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise GatewayError.of(
                "INVALID_REQUEST", "timeoutSeconds 必须是整数", param="timeoutSeconds"
            )
        if not 1 <= value <= 600:
            raise GatewayError.of(
                "INVALID_REQUEST", "timeoutSeconds 必须在 1–600", param="timeoutSeconds"
            )
        return value

    def _row(self, provider_id: str):
        rows = self._db.query("SELECT * FROM providers WHERE id=?", (provider_id,))
        if not rows:
            raise GatewayError.of(
                "INVALID_REQUEST", f"provider 不存在：{provider_id}", param="id"
            )
        return rows[0]

    def _key_row(self, provider_id: str, key_id: str):
        rows = self._db.query(
            "SELECT * FROM provider_keys WHERE provider_id=? AND id=?",
            (provider_id, key_id),
        )
        if not rows:
            raise GatewayError.of(
                "INVALID_REQUEST", f"密钥不存在：{key_id}", param="keyId"
            )
        return rows[0]

    def _provider_public(self, row) -> dict:
        keys = [
            {
                "id": key["id"],
                "label": key["label"],
                "last4": key["last4"],
                "weight": int(key["weight"]),
                "createdAt": int(key["created_at_ms"]),
                "disabledAt": int(key["disabled_at_ms"]) or None,
                "active": key["status"] == "active",
            }
            for key in self._db.query(
                "SELECT * FROM provider_keys WHERE provider_id=? "
                "ORDER BY created_at_ms DESC",
                (row["id"],),
            )
        ]
        return {
            "id": row["id"],
            "type": row["type"],
            "label": row["label"],
            "baseUrl": row["base_url"],
            "apiVersion": row["api_version"] or None,
            "timeoutSeconds": int(row["timeout_seconds"]),
            "allowPrivate": bool(row["allow_private"]),
            "enabled": bool(row["enabled"]),
            "configured": True,
            "keys": keys,
        }

    def _encrypt(self, secret: str):
        if not self._secrets.available:
            raise GatewayError.of(
                "INTERNAL_ERROR", "未配置 TT_GATEWAY_MASTER_KEY，无法保存厂商密钥"
            )
        try:
            return self._secrets.encrypt(secret)
        except ValueError as exc:
            raise GatewayError.of("INVALID_REQUEST", "密钥不合法", param="secret") from exc

    @staticmethod
    def _safe_provider(
        provider_id: str,
        label: str,
        base_url: str,
        api_version: str,
        timeout: int,
        allow_private: bool,
    ) -> dict:
        return {
            "id": provider_id,
            "label": label,
            "baseUrl": base_url,
            "apiVersion": api_version,
            "timeoutSeconds": timeout,
            "allowPrivate": allow_private,
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


def _public_before(payload: dict) -> dict:
    return {key: payload[key] for key in ("id", "label", "baseUrl", "apiVersion",
                                          "timeoutSeconds", "allowPrivate", "enabled")
            if key in payload}
