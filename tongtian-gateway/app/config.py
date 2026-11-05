"""网关配置：全部来自环境变量，导入时固化。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent
DEV_HS256_SECRET = "tongtian-dev-secret-change-me"
DEV_ADMIN_TOKEN = "dev-admin-token"
REDIS_PREFIX_RE = re.compile(r"[A-Za-z0-9._-]{1,32}")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("false", "0", "off", "no", "")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_int_between(name: str, default: int, low: int, high: int) -> int:
    return min(high, max(low, _env_int(name, default)))


def _env_key_map(name: str) -> dict[int, str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    keys: dict[int, str] = {}
    for version, value in payload.items():
        try:
            keys[int(version)] = str(value).strip()
        except (TypeError, ValueError):
            continue
    return {k: v for k, v in keys.items() if v}


def _sentinel_addresses(raw: str) -> tuple[tuple[str, int], ...]:
    addresses: list[tuple[str, int]] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        host, separator, port = item.rpartition(":")
        if (
            not separator
            or not host
            or not port.isdigit()
            or not 1 <= int(port) <= 65535
        ):
            raise RuntimeError(
                "TT_GATEWAY_REDIS_SENTINEL_URLS 必须是 host:port,host:port"
            )
        addresses.append((host.strip("[]"), int(port)))
    if not addresses:
        raise RuntimeError("TT_GATEWAY_REDIS_SENTINEL_URLS 不能为空")
    return tuple(addresses)


@dataclass(frozen=True)
class Settings:
    environment: str
    host: str
    port: int
    data_dir: Path
    db_path: Path
    gateway_id: str
    jwt_mode: str
    dev_hs256_secret: str
    jwt_issuer: str
    jwt_audience: str
    admin_token: str
    mtls_mode: str
    trusted_proxies: tuple[str, ...]
    admin_rate_limit_per_minute: int
    admin_rate_limit_burst: int
    policy_file: Path | None
    control_plane_url: str
    service_token: str
    policy_ttl_seconds: int
    policy_stale_grace_seconds: int
    max_request_bytes: int
    max_output_tokens: int
    connect_timeout_seconds: int
    upstream_timeout_seconds: int
    stream_idle_timeout_seconds: int
    upstream_retries: int
    usage_batch_size: int
    usage_flush_seconds: int
    allow_private_providers: bool
    master_key: str
    master_key_version: int
    master_keys: dict[int, str]
    jwks_url: str
    audit_webhook_url: str
    audit_webhook_secret: str
    audit_webhook_interval_seconds: int
    audit_webhook_batch_size: int
    audit_webhook_timeout_seconds: int
    workers: int
    redis_url: str
    redis_prefix: str
    redis_timeout_ms: int
    db_url: str
    cross_host: bool
    redis_sentinel_master: str
    redis_sentinel_urls: tuple[tuple[str, int], ...]
    redis_sentinel_password: str
    redis_password: str
    redis_db: int
    redis_tls: bool
    redis_tls_ca: str
    redis_tls_cert: str
    redis_tls_key: str

    @property
    def production(self) -> bool:
        return self.environment.strip().lower() in ("production", "prod")

    @property
    def docs_enabled(self) -> bool:
        return not self.production

    @property
    def shared_state_configured(self) -> bool:
        """单实例 URL 或 Sentinel 任一配置即可；哨兵模式可留空 URL。"""
        return bool(self.redis_url or self.redis_sentinel_master)


def load_settings() -> Settings:
    environment = os.environ.get("TT_GATEWAY_ENV", "development")
    production = environment.strip().lower() in ("production", "prod")
    data_dir = Path(os.environ.get("TT_GATEWAY_DATA_DIR", str(BASE_DIR / "storage"))).resolve()
    policy_raw = os.environ.get("TT_GATEWAY_POLICY_FILE", "").strip()
    jwt_mode = os.environ.get("TT_GATEWAY_JWT_MODE", "").strip().lower()
    if not jwt_mode:
        jwt_mode = "rs256" if production else "hs256-dev"
    admin_token = os.environ.get("TT_GATEWAY_ADMIN_TOKEN", "").strip()
    if not admin_token and not production:
        admin_token = DEV_ADMIN_TOKEN
    mtls_mode = os.environ.get("TT_GATEWAY_MTLS_MODE", "").strip().lower()
    if not mtls_mode:
        mtls_mode = "proxy" if production else "off"
    trusted_proxies = tuple(
        part.strip()
        for part in os.environ.get(
            "TT_GATEWAY_TRUSTED_PROXIES", "127.0.0.1,::1"
        ).split(",")
        if part.strip()
    )
    admin_rpm = _env_int_between(
        "TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE", 120 if production else 0, 0, 1_000_000
    )
    default_burst = max(10, admin_rpm // 3) if admin_rpm else 0
    admin_burst = _env_int_between(
        "TT_GATEWAY_ADMIN_RATE_LIMIT_BURST", default_burst, 0, 1_000_000
    )
    if admin_rpm and admin_burst < 1:
        admin_burst = 1
    control_plane_url = (
        os.environ.get("TT_GATEWAY_CONTROL_PLANE_URL", "").strip().rstrip("/")
    )
    jwks_url = os.environ.get("TT_GATEWAY_JWKS_URL", "").strip()
    if not jwks_url and control_plane_url:
        jwks_url = f"{control_plane_url}/.well-known/jwks.json"
    webhook_url = os.environ.get("TT_GATEWAY_AUDIT_WEBHOOK_URL", "").strip()
    webhook_secret = os.environ.get("TT_GATEWAY_AUDIT_WEBHOOK_SECRET", "").strip()
    if webhook_url:
        parsed = urlsplit(webhook_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise RuntimeError(
                "TT_GATEWAY_AUDIT_WEBHOOK_URL 必须是 http(s) 绝对地址"
            )
        if parsed.username or parsed.password:
            raise RuntimeError("TT_GATEWAY_AUDIT_WEBHOOK_URL 不得包含账号密码")
        if len(webhook_secret) < 16:
            raise RuntimeError(
                "配置审计外送时必须提供至少 16 位的 TT_GATEWAY_AUDIT_WEBHOOK_SECRET"
            )
    redis_url = os.environ.get("TT_GATEWAY_REDIS_URL", "").strip()
    if redis_url:
        parsed = urlsplit(redis_url)
        if parsed.scheme not in ("redis", "rediss") or not parsed.netloc:
            raise RuntimeError("TT_GATEWAY_REDIS_URL 必须是 redis:// 或 rediss:// 地址")
        if any(ch.isspace() for ch in redis_url):
            raise RuntimeError("TT_GATEWAY_REDIS_URL 不得包含空白字符")
    redis_prefix = os.environ.get("TT_GATEWAY_REDIS_PREFIX", "ttgw").strip() or "ttgw"
    if not REDIS_PREFIX_RE.fullmatch(redis_prefix):
        raise RuntimeError(
            "TT_GATEWAY_REDIS_PREFIX 只能包含字母/数字/._-，长度 1–32"
        )
    db_url = os.environ.get("TT_GATEWAY_DB_URL", "").strip()
    if db_url:
        parsed = urlsplit(db_url)
        if parsed.scheme not in ("postgresql", "postgres") or not parsed.netloc:
            raise RuntimeError(
                "TT_GATEWAY_DB_URL 必须是 postgresql:// 或 postgres:// 地址"
            )
        if any(ch.isspace() for ch in db_url):
            raise RuntimeError("TT_GATEWAY_DB_URL 不得包含空白字符")
    sentinel_master = os.environ.get(
        "TT_GATEWAY_REDIS_SENTINEL_MASTER", ""
    ).strip()
    sentinel_raw = os.environ.get("TT_GATEWAY_REDIS_SENTINEL_URLS", "").strip()
    sentinel_urls = _sentinel_addresses(sentinel_raw) if sentinel_raw else ()
    if sentinel_master and not sentinel_urls:
        raise RuntimeError(
            "配置 TT_GATEWAY_REDIS_SENTINEL_MASTER 时必须提供 TT_GATEWAY_REDIS_SENTINEL_URLS"
        )
    redis_tls = _env_bool("TT_GATEWAY_REDIS_TLS", False)
    redis_tls_ca = os.environ.get("TT_GATEWAY_REDIS_TLS_CA", "").strip()
    redis_tls_cert = os.environ.get("TT_GATEWAY_REDIS_TLS_CERT", "").strip()
    redis_tls_key = os.environ.get("TT_GATEWAY_REDIS_TLS_KEY", "").strip()
    if bool(redis_tls_cert) != bool(redis_tls_key):
        raise RuntimeError(
            "TT_GATEWAY_REDIS_TLS_CERT 与 TT_GATEWAY_REDIS_TLS_KEY 必须同时配置"
        )
    for label, value in (
        ("TT_GATEWAY_REDIS_TLS_CA", redis_tls_ca),
        ("TT_GATEWAY_REDIS_TLS_CERT", redis_tls_cert),
        ("TT_GATEWAY_REDIS_TLS_KEY", redis_tls_key),
    ):
        if value and not Path(value).is_file():
            raise RuntimeError(f"{label} 指向的文件不存在：{value}")
    if redis_url:
        if urlsplit(redis_url).scheme == "rediss":
            redis_tls = True
        elif redis_tls:
            raise RuntimeError(
                "启用 TT_GATEWAY_REDIS_TLS 时 TT_GATEWAY_REDIS_URL 必须使用 rediss://"
            )
    return Settings(
        environment=environment,
        host=os.environ.get("TT_GATEWAY_HOST", "127.0.0.1"),
        port=_env_int("TT_GATEWAY_PORT", 8001),
        data_dir=data_dir,
        db_path=data_dir / "gateway.db",
        gateway_id=os.environ.get("TT_GATEWAY_ID", "gw-local-01").strip() or "gw-local-01",
        jwt_mode=jwt_mode,
        dev_hs256_secret=os.environ.get("TT_GATEWAY_DEV_HS256_SECRET", DEV_HS256_SECRET),
        jwt_issuer=os.environ.get("TT_GATEWAY_JWT_ISSUER", "tongtian-auth").strip()
        or "tongtian-auth",
        jwt_audience=os.environ.get("TT_GATEWAY_JWT_AUDIENCE", "tongtian-gateway").strip()
        or "tongtian-gateway",
        admin_token=admin_token,
        mtls_mode=mtls_mode,
        trusted_proxies=trusted_proxies,
        admin_rate_limit_per_minute=admin_rpm,
        admin_rate_limit_burst=admin_burst,
        policy_file=Path(policy_raw).resolve() if policy_raw else None,
        control_plane_url=control_plane_url,
        service_token=os.environ.get("TT_GATEWAY_SERVICE_TOKEN", "").strip(),
        policy_ttl_seconds=_env_int("TT_GATEWAY_POLICY_TTL_SECONDS", 300),
        policy_stale_grace_seconds=_env_int("TT_GATEWAY_POLICY_STALE_GRACE_SECONDS", 86400),
        max_request_bytes=_env_int("TT_GATEWAY_MAX_REQUEST_BYTES", 32 * 1024 * 1024),
        max_output_tokens=_env_int("TT_GATEWAY_MAX_OUTPUT_TOKENS", 8192),
        connect_timeout_seconds=_env_int_between(
            "TT_GATEWAY_CONNECT_TIMEOUT_SECONDS", 10, 1, 60
        ),
        upstream_timeout_seconds=_env_int_between(
            "TT_GATEWAY_UPSTREAM_TIMEOUT_SECONDS", 120, 1, 600
        ),
        stream_idle_timeout_seconds=_env_int_between(
            "TT_GATEWAY_STREAM_IDLE_TIMEOUT_SECONDS", 120, 1, 600
        ),
        upstream_retries=_env_int_between("TT_GATEWAY_UPSTREAM_RETRIES", 1, 0, 5),
        usage_batch_size=_env_int("TT_GATEWAY_USAGE_BATCH_SIZE", 200),
        usage_flush_seconds=_env_int("TT_GATEWAY_USAGE_FLUSH_SECONDS", 5),
        allow_private_providers=_env_bool("TT_GATEWAY_ALLOW_PRIVATE_PROVIDERS", False),
        master_key=os.environ.get("TT_GATEWAY_MASTER_KEY", "").strip(),
        master_key_version=_env_int("TT_GATEWAY_MASTER_KEY_VERSION", 1),
        master_keys=_env_key_map("TT_GATEWAY_MASTER_KEYS"),
        jwks_url=jwks_url,
        audit_webhook_url=webhook_url,
        audit_webhook_secret=webhook_secret,
        audit_webhook_interval_seconds=_env_int_between(
            "TT_GATEWAY_AUDIT_WEBHOOK_INTERVAL_SECONDS", 10, 1, 300
        ),
        audit_webhook_batch_size=_env_int_between(
            "TT_GATEWAY_AUDIT_WEBHOOK_BATCH_SIZE", 50, 1, 500
        ),
        audit_webhook_timeout_seconds=_env_int_between(
            "TT_GATEWAY_AUDIT_WEBHOOK_TIMEOUT_SECONDS", 10, 1, 60
        ),
        workers=_env_int_between("TT_GATEWAY_WORKERS", 1, 1, 64),
        redis_url=redis_url,
        redis_prefix=redis_prefix,
        redis_timeout_ms=_env_int_between(
            "TT_GATEWAY_REDIS_TIMEOUT_MS", 500, 100, 5000
        ),
        db_url=db_url,
        cross_host=_env_bool("TT_GATEWAY_CROSS_HOST", False),
        redis_sentinel_master=sentinel_master,
        redis_sentinel_urls=sentinel_urls,
        redis_sentinel_password=os.environ.get(
            "TT_GATEWAY_REDIS_SENTINEL_PASSWORD", ""
        ).strip(),
        redis_password=os.environ.get("TT_GATEWAY_REDIS_PASSWORD", ""),
        redis_db=_env_int_between("TT_GATEWAY_REDIS_DB", 0, 0, 255),
        redis_tls=redis_tls,
        redis_tls_ca=redis_tls_ca,
        redis_tls_cert=redis_tls_cert,
        redis_tls_key=redis_tls_key,
    )


settings = load_settings()
