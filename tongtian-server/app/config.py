"""服务端配置：全部来自环境变量，导入时固化（测试可在导入前覆盖）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEV_JWT_SECRET = "tongtian-dev-secret-change-me"


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


@dataclass(frozen=True)
class Settings:
    environment: str
    host: str
    port: int
    data_dir: Path
    db_path: Path
    packages_dir: Path
    jwt_secret: str
    jwt_issuer: str
    jwt_audience: str
    access_ttl_seconds: int
    refresh_ttl_seconds: int
    allow_registration: bool
    cors_origins: tuple[str, ...]
    trust_proxy: bool
    seed_demo: bool
    admin_email: str
    admin_password: str
    admin_nickname: str
    max_zip_bytes: int
    max_entries: int
    max_entry_bytes: int
    max_total_uncompressed: int
    max_manifest_bytes: int
    max_skill_md_bytes: int

    @property
    def production(self) -> bool:
        return self.environment.strip().lower() in ("production", "prod")


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("TT_SERVER_DATA_DIR", str(BASE_DIR / "storage"))).resolve()
    jwt_secret = (
        os.environ.get("TT_SERVER_JWT_SECRET")
        or os.environ.get("TT_AUTH_JWT_SECRET")
        or DEV_JWT_SECRET
    )
    cors_raw = os.environ.get("TT_CORS_ORIGINS", "*")
    cors = tuple(item.strip() for item in cors_raw.split(",") if item.strip()) or ("*",)
    return Settings(
        environment=os.environ.get("TT_SERVER_ENV", "development"),
        host=os.environ.get("TT_SERVER_HOST", "127.0.0.1"),
        port=_env_int("TT_SERVER_PORT", 8000),
        data_dir=data_dir,
        db_path=data_dir / "market.db",
        packages_dir=data_dir / "packages",
        jwt_secret=jwt_secret,
        jwt_issuer=os.environ.get("TT_AUTH_ISSUER", "tongtian-auth").strip() or "tongtian-auth",
        jwt_audience=os.environ.get("TT_AUTH_AUDIENCE", "tongtian-engine").strip()
        or "tongtian-engine",
        access_ttl_seconds=_env_int("TT_ACCESS_TTL_SECONDS", 15 * 60),
        refresh_ttl_seconds=_env_int("TT_REFRESH_TTL_SECONDS", 30 * 24 * 3600),
        allow_registration=_env_bool("TT_ALLOW_REGISTRATION", True),
        cors_origins=cors,
        trust_proxy=_env_bool("TT_TRUST_PROXY", False),
        seed_demo=_env_bool("TT_SEED_DEMO", False),
        admin_email=os.environ.get("TT_ADMIN_EMAIL", "").strip().lower(),
        admin_password=os.environ.get("TT_ADMIN_PASSWORD", ""),
        admin_nickname=os.environ.get("TT_ADMIN_NICKNAME", "平台管理员").strip() or "平台管理员",
        max_zip_bytes=_env_int("TT_MAX_ZIP_BYTES", 16 * 1024 * 1024),
        max_entries=_env_int("TT_MAX_ZIP_ENTRIES", 256),
        max_entry_bytes=_env_int("TT_MAX_ENTRY_BYTES", 4 * 1024 * 1024),
        max_total_uncompressed=_env_int("TT_MAX_UNCOMPRESSED_BYTES", 32 * 1024 * 1024),
        max_manifest_bytes=_env_int("TT_MAX_MANIFEST_BYTES", 256 * 1024),
        max_skill_md_bytes=_env_int("TT_MAX_SKILL_MD_BYTES", 512 * 1024),
    )


settings = load_settings()
