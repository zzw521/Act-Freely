"""控制面策略客户端：ETag 条件拉取、内存 + SQLite 双层缓存、stale 宽限。

单副本单 worker；拉取串行化，避免控制面抖动时并发打爆。
策略文档含定价与白名单，任何内容变化都必须换 policyVersion。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from dataclasses import dataclass

import httpx

from .config import Settings
from .contract import PolicyDocument
from .db import Database, now_ms
from .errors import GatewayError

logger = logging.getLogger("tongtian.gateway.policy_client")

FETCH_TIMEOUT_SECONDS = 3.0
MAX_POLICY_BYTES = 512 * 1024


@dataclass(frozen=True)
class CachedPolicy:
    document: dict
    version: str
    etag: str
    fetched_at: float


@dataclass(frozen=True)
class PolicyState:
    document: dict
    policy_version: str | None
    stale: bool
    loaded_at: float


class PolicyClient:
    def __init__(
        self, settings: Settings, db: Database, shared=None
    ) -> None:
        self._base = settings.control_plane_url.rstrip("/")
        self._token = settings.service_token
        self._ttl = max(1, settings.policy_ttl_seconds)
        self._grace = max(0, settings.policy_stale_grace_seconds)
        self._db = db
        self._lock = threading.RLock()
        self._cache: dict[str, CachedPolicy] = {}
        self._shared = shared

    def get(self, tenant: str) -> PolicyState:
        with self._lock:
            entry = self._cache.get(tenant) or self._from_db(tenant)
            now = time.time()
            if entry is not None and now - entry.fetched_at < self._ttl:
                return PolicyState(entry.document, entry.version, False, entry.fetched_at)

            refreshed: CachedPolicy | None = None
            try:
                refreshed = self._fetch(tenant, entry.etag if entry else "")
            except GatewayError:
                # 控制面明确答复“该租户没有策略”：永久拒绝，不能拿旧缓存顶上
                raise
            except Exception as exc:  # noqa: BLE001 - 控制面故障统一降级
                logger.warning("拉取策略失败 tenant=%s：%s", tenant, type(exc).__name__)
            if refreshed is not None:
                self._cache[tenant] = refreshed
                self._save_db(tenant, refreshed, now)
                if entry is None or entry.version != refreshed.version:
                    # 通知其他 worker 失效内存缓存；失败不阻断本次请求（TTL 兜底）。
                    if self._shared is not None and self._shared.enabled:
                        self._shared.publish_policy(tenant)
                return PolicyState(
                    refreshed.document, refreshed.version, False, refreshed.fetched_at
                )

            if entry is not None and now - entry.fetched_at <= self._grace:
                self._cache[tenant] = entry
                return PolicyState(entry.document, entry.version, True, entry.fetched_at)
            raise GatewayError.of(
                "POLICY_STALE", "策略已过期且控制面不可达，请稍后重试"
            )

    def invalidate(self, tenant: str | None = None) -> None:
        with self._lock:
            if tenant is None:
                self._cache.clear()
            else:
                self._cache.pop(tenant, None)

    # ── 内部 ────────────────────────────────────────────────

    def _fetch(self, tenant: str, etag: str) -> CachedPolicy | None:
        url = f"{self._base}/api/v1/gateway/policy?tenant={urllib.parse.quote(tenant)}"
        headers = {"accept": "application/json"}
        if self._token:
            headers["authorization"] = f"Bearer {self._token}"
        if etag:
            headers["if-none-match"] = etag
        with httpx.Client(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = client.get(url, headers=headers)
        if response.status_code == 304:
            cached = self._cache.get(tenant) or self._from_db(tenant)
            if cached is None:
                raise RuntimeError("策略返回 304 但本地无缓存")
            return CachedPolicy(
                cached.document, cached.version, cached.etag, time.time()
            )
        if response.status_code == 404:
            raise GatewayError.of("FORBIDDEN", "该租户未配置网关策略")
        if response.status_code != 200:
            raise RuntimeError(f"策略接口返回 {response.status_code}")
        raw = response.content
        if len(raw) > MAX_POLICY_BYTES:
            raise RuntimeError("策略文档超过 512KiB 上限")
        document = json.loads(raw.decode("utf-8"))
        if not isinstance(document, dict):
            raise RuntimeError("策略文档不是 JSON 对象")
        PolicyDocument.model_validate(document)
        version = str(document.get("policyVersion") or response.headers.get("etag") or "")
        if not version:
            raise RuntimeError("策略文档缺少 policyVersion")
        return CachedPolicy(
            document=document,
            version=version,
            etag=f'"{version}"',
            fetched_at=time.time(),
        )

    def _from_db(self, tenant: str) -> CachedPolicy | None:
        rows = self._db.query(
            "SELECT document_json, policy_version, etag, fetched_at_ms FROM policy_cache "
            "WHERE tenant = ?",
            (tenant,),
        )
        if not rows:
            return None
        row = rows[0]
        try:
            document = json.loads(row["document_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(document, dict):
            return None
        return CachedPolicy(
            document=document,
            version=str(row["policy_version"]),
            etag=str(row["etag"] or ""),
            fetched_at=int(row["fetched_at_ms"]) / 1000.0,
        )

    def _save_db(self, tenant: str, entry: CachedPolicy, now: float) -> None:
        try:
            with self._db.tx() as conn:
                conn.execute(
                    "INSERT INTO policy_cache "
                    "(tenant, document_json, policy_version, etag, fetched_at_ms) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(tenant) DO UPDATE SET document_json = excluded.document_json, "
                    "policy_version = excluded.policy_version, etag = excluded.etag, "
                    "fetched_at_ms = excluded.fetched_at_ms",
                    (
                        tenant,
                        json.dumps(entry.document, ensure_ascii=False, separators=(",", ":")),
                        entry.version,
                        entry.etag,
                        now_ms(),
                    ),
                )
        except Exception:  # noqa: BLE001 - 缓存写失败不影响本次请求
            logger.exception("写入策略缓存失败 tenant=%s", tenant)
