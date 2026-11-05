"""启动自检：生产环境缺关键配置时拒绝启动，并点名具体环境变量。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .config import Settings
from .mtls import parse_networks
from .secrets import Secrets

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注，避免与 db 形成循环导入
    from .db import Database

PRODUCTION_JWT_MODE = "rs256"
MTLS_MODES = {"off", "proxy"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _loopback(url: str) -> bool:
    try:
        return (urlsplit(url).hostname or "") in LOOPBACK_HOSTS
    except ValueError:
        return False


def _jwks_scheme_ok(url: str, production: bool) -> bool:
    try:
        scheme = urlsplit(url).scheme
    except ValueError:
        return False
    return scheme == "https" or (not production and scheme == "http") or _loopback(url)


@dataclass(frozen=True)
class StartupReport:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_startup(settings: Settings, secrets: Secrets | None = None) -> StartupReport:
    errors: list[str] = []
    warnings: list[str] = []
    secrets = secrets or Secrets.from_settings(settings)

    if settings.mtls_mode not in MTLS_MODES:
        errors.append("TT_GATEWAY_MTLS_MODE=off|proxy")
    elif settings.mtls_mode == "proxy":
        if not settings.trusted_proxies:
            errors.append("TT_GATEWAY_TRUSTED_PROXIES（mTLS proxy 模式不能为空）")
        else:
            try:
                parse_networks(settings.trusted_proxies)
            except ValueError as exc:
                errors.append(f"TT_GATEWAY_TRUSTED_PROXIES 含非法地址：{exc}")

    if settings.workers > 1 and not settings.shared_state_configured:
        errors.append(
            "TT_GATEWAY_REDIS_URL 或 TT_GATEWAY_REDIS_SENTINEL_MASTER"
            "（TT_GATEWAY_WORKERS>1 时必须配置共享状态）"
        )

    if settings.cross_host:
        if not settings.db_url:
            errors.append(
                "TT_GATEWAY_DB_URL（TT_GATEWAY_CROSS_HOST=1 时必须使用 Postgres 共享账本）"
            )
        if not settings.shared_state_configured:
            errors.append(
                "TT_GATEWAY_REDIS_URL 或 TT_GATEWAY_REDIS_SENTINEL_MASTER"
                "（跨主机部署必须共享在途配额与限流状态）"
            )

    if settings.redis_sentinel_master and not settings.redis_sentinel_urls:
        errors.append("TT_GATEWAY_REDIS_SENTINEL_URLS（启用 Sentinel 时必须提供）")
    if settings.redis_tls:
        for name, value in (
            ("TT_GATEWAY_REDIS_TLS_CA", settings.redis_tls_ca),
            ("TT_GATEWAY_REDIS_TLS_CERT", settings.redis_tls_cert),
            ("TT_GATEWAY_REDIS_TLS_KEY", settings.redis_tls_key),
        ):
            if value and not Path(value).is_file():
                errors.append(f"{name} 指向的文件不存在")

    if settings.production:
        if settings.jwt_mode != PRODUCTION_JWT_MODE:
            errors.append("TT_GATEWAY_JWT_MODE=rs256")
        if not settings.jwks_url:
            errors.append("TT_GATEWAY_JWKS_URL（或 TT_GATEWAY_CONTROL_PLANE_URL）")
        if not secrets.available:
            errors.append("TT_GATEWAY_MASTER_KEY（base64 编码的 32 字节）")
        if not settings.admin_token:
            errors.append("TT_GATEWAY_ADMIN_TOKEN")
        if settings.jwks_url and not _jwks_scheme_ok(settings.jwks_url, True):
            errors.append("TT_GATEWAY_JWKS_URL 必须为 https://（本机回环除外）")
        if settings.control_plane_url and not settings.service_token:
            errors.append("TT_GATEWAY_SERVICE_TOKEN（控制面策略拉取）")
        if settings.allow_private_providers:
            warnings.append(
                "TT_GATEWAY_ALLOW_PRIVATE_PROVIDERS=1：允许私网 provider 地址，仅限内网部署"
            )
        if settings.mtls_mode == "off":
            warnings.append(
                "生产环境管理面未启用 mTLS（TT_GATEWAY_MTLS_MODE=off）：仅应在受信内网使用"
            )
        if settings.admin_rate_limit_per_minute == 0:
            warnings.append(
                "生产环境管理面未启用限流（TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE=0）"
            )
    else:
        if settings.jwt_mode == "rs256":
            if not settings.jwks_url:
                errors.append("TT_GATEWAY_JWKS_URL（RS256 模式）")
            elif not _jwks_scheme_ok(settings.jwks_url, False):
                errors.append("TT_GATEWAY_JWKS_URL 必须为 http(s)://")
            if settings.control_plane_url and not settings.service_token:
                warnings.append("未配置 TT_GATEWAY_SERVICE_TOKEN：策略不会从控制面拉取")
        elif settings.jwt_mode != "hs256-dev":
            errors.append("TT_GATEWAY_JWT_MODE=hs256-dev 或 rs256（开发环境）")
        if not settings.admin_token:
            errors.append("TT_GATEWAY_ADMIN_TOKEN")
        if not secrets.available:
            warnings.append(
                "未配置 TT_GATEWAY_MASTER_KEY：provider 密钥将无法写入"
            )
        warnings.append("开发态 HS256 校验，仅限内网联调")

    return StartupReport(tuple(errors), tuple(warnings))


def enforce_startup(settings: Settings, secrets: Secrets | None = None) -> StartupReport:
    report = validate_startup(settings, secrets)
    if not report.ok:
        missing = "；".join(report.errors)
        raise RuntimeError(f"网关无法启动，缺少或无效的配置：{missing}")
    return report


_CREDENTIAL_IN_URL = re.compile(r"://[^@\s/]*@")
_PASSWORD_KEYWORD = re.compile(r"(?i)\b(password|passwd|pwd)\s*=\s*\S+")


def _ledger_error_detail(exc_text: str, settings: Settings) -> str:
    """驱动层错误可能带连接串：只留第一行，并抹掉口令与配置的 DSN。"""
    first_line = next((line.strip() for line in exc_text.splitlines() if line.strip()), "")
    detail = _CREDENTIAL_IN_URL.sub("://***@", first_line)
    detail = _PASSWORD_KEYWORD.sub(r"\1=***", detail)
    if settings.db_url:
        detail = detail.replace(settings.db_url, "***")
    return detail[:200] or "未知错误"


def enforce_ledger(database: Database, settings: Settings) -> dict:
    """启动即校验账本：连不上、迁移失败或库里版本高于程序支持值时拒绝启动。

    账本连接原本是懒式的：进程能起来不代表账本可用，第一个请求才回 500。
    """
    try:
        database.ensure()
    except Exception as exc:  # noqa: BLE001 - 统一换成不回显 DSN 的启动错误
        raise RuntimeError(
            f"账本不可用，拒绝启动：backend={database.dialect} "
            f"{_ledger_error_detail(str(exc), settings)}"
        ) from exc
    status = database.status()
    if not status["connected"]:
        raise RuntimeError(
            f"账本不可用，拒绝启动：backend={database.dialect} 迁移后仍读不到 schema_version"
        )
    return status
