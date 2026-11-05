"""进程内共享状态：SQLite 账本 + 内存策略快照 + 可选 Redis 共享状态（M5）。"""

from __future__ import annotations

from .adapters import litellm_adapters
from .api_key_limits import api_key_limiter
from .api_keys_store import ApiKeyStore
from .audit_outbox import AuditWebhook
from .config import settings
from .db import Database
from .policy import PolicyStore
from .providers_store import ProviderStore
from .quota_sqlite import SqliteQuota
from .ratelimit import admin_limiter
from .secrets import Secrets
from .service import ExecutionService
from .shared_state import SharedState
from .tpm import TpmLimiter
from .usage_store import UsageStore
from .usage_uploader import UsageUploader

db = Database(settings.db_path, url=settings.db_url)
try:
    secrets = Secrets.from_settings(settings)
except ValueError as exc:
    raise RuntimeError(f"TT_GATEWAY_MASTER_KEY 配置无效：{exc}") from exc

shared_state = SharedState(
    settings.redis_url,
    prefix=settings.redis_prefix,
    timeout_ms=settings.redis_timeout_ms,
    workers=settings.workers,
    sentinel_master=settings.redis_sentinel_master,
    sentinel_urls=settings.redis_sentinel_urls,
    sentinel_password=settings.redis_sentinel_password,
    password=settings.redis_password,
    db=settings.redis_db,
    tls=settings.redis_tls,
    tls_ca=settings.redis_tls_ca,
    tls_cert=settings.redis_tls_cert,
    tls_key=settings.redis_tls_key,
)
policy_store = PolicyStore(settings, db, shared=shared_state)
provider_store = ProviderStore(
    db, secrets, allow_private_global=settings.allow_private_providers
)
quota_tracker = SqliteQuota(db, shared=shared_state)
usage_store = UsageStore(db)
api_key_store = ApiKeyStore(db)
admin_limiter.shared = shared_state
api_key_limiter.shared = shared_state
tpm_limiter = TpmLimiter(shared=shared_state)
service = ExecutionService(
    settings,
    policy_store,
    provider_store,
    quota_tracker,
    usage_store,
    litellm_adapters(
        connect_timeout_seconds=settings.connect_timeout_seconds,
        idle_timeout_seconds=settings.stream_idle_timeout_seconds,
        max_retries=settings.upstream_retries,
    ),
    tpm_limiter,
)
usage_uploader = UsageUploader(settings, usage_store)
audit_webhook = AuditWebhook(settings, db)
