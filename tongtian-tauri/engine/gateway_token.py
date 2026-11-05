"""网关模式换票：账号 access token → 短期 RS256 网关令牌。

换取走账号服务 ``POST /api/v1/auth/gateway-token``（Bearer access token，
可选部门）。令牌只存在本进程内存：不写日志、不落盘、不进事件流；
错误信息只带稳定错误码与中文说明，绝不回显令牌。
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

EXCHANGE_PATH = "/api/v1/auth/gateway-token"
# 到期前 2 分钟刷新：网关令牌 TTL ≤15 分钟，留出一次重试余量
REFRESH_BEFORE_SECONDS = 120
REQUEST_TIMEOUT_SECONDS = 5.0
MAX_BODY_BYTES = 64 * 1024


class GatewayTokenError(RuntimeError):
    """换票失败（message 不含任何令牌/密钥）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ExchangedToken:
    token: str = field(repr=False)
    expires_at: float
    tenant: str = ""
    dept: str = ""


def _exchange_sync(
    base: str, access_token: str, dept: str | None
) -> ExchangedToken:
    url = base + EXCHANGE_PATH
    body = json.dumps({"dept": dept}).encode("utf-8") if dept else b""
    try:
        request = urllib.request.Request(
            url,
            data=body or None,
            method="POST",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if body else {}),
            },
        )
    except ValueError:
        raise GatewayTokenError(
            "GATEWAY_CONFIG_INVALID", "网关模式的控制面地址无效"
        ) from None
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            raw = resp.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # 只取稳定错误码，不把响应体或请求头带进错误链
        code = ""
        try:
            payload = json.loads(exc.read(MAX_BODY_BYTES).decode("utf-8"))
            if isinstance(payload, dict):
                code = str(payload.get("code") or "")
        except Exception:  # noqa: BLE001
            code = ""
        if exc.code in (401, 403):
            hint = f"HTTP {exc.code}{'，' + code if code else ''}"
            raise GatewayTokenError(
                "GATEWAY_TOKEN_REJECTED",
                f"网关令牌换取被拒绝（{hint}）：登录状态可能已失效，或账号没有可用部门",
            ) from None
        if exc.code == 429:
            raise GatewayTokenError(
                "GATEWAY_TOKEN_RATE_LIMITED", "网关令牌换取过于频繁（HTTP 429），请稍后重试"
            ) from None
        raise GatewayTokenError(
            "GATEWAY_TOKEN_UNAVAILABLE", f"网关令牌换取失败（HTTP {exc.code}）"
        ) from None
    except GatewayTokenError:
        raise
    except Exception:  # noqa: BLE001
        raise GatewayTokenError(
            "GATEWAY_TOKEN_UNAVAILABLE", "无法连接账号服务换取网关令牌"
        ) from None

    if len(raw) > MAX_BODY_BYTES:
        raise GatewayTokenError(
            "GATEWAY_TOKEN_BAD_RESPONSE", "账号服务返回的网关令牌响应过大"
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        raise GatewayTokenError(
            "GATEWAY_TOKEN_BAD_RESPONSE", "账号服务返回的网关令牌响应无法解析"
        ) from None
    if not isinstance(payload, dict):
        raise GatewayTokenError(
            "GATEWAY_TOKEN_BAD_RESPONSE", "账号服务返回的网关令牌响应格式不正确"
        )
    token = str(payload.get("token") or "").strip()
    if not token:
        raise GatewayTokenError(
            "GATEWAY_TOKEN_BAD_RESPONSE", "账号服务没有返回网关令牌"
        )
    now = time.time()
    try:
        expires_at = float(payload.get("expiresAt") or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    if expires_at <= 0:
        try:
            expires_in = int(payload.get("expiresIn") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        expires_at = now + expires_in if expires_in > 0 else 0.0
    if expires_at <= now:
        raise GatewayTokenError(
            "GATEWAY_TOKEN_BAD_RESPONSE", "账号服务返回的网关令牌已过期"
        )
    return ExchangedToken(
        token=token,
        expires_at=expires_at,
        tenant=str(payload.get("tenant") or ""),
        dept=str(payload.get("dept") or ""),
    )


async def _exchange_http(
    base: str, access_token: str, dept: str | None
) -> ExchangedToken:
    return await asyncio.to_thread(_exchange_sync, base, access_token, dept)


class GatewayTokenProvider:
    """单主体令牌缓存：TTL 前 2 分钟刷新；401 时 invalidate 后强制换票一次。"""

    def __init__(
        self,
        control_plane_url: str,
        access_token: str,
        dept: str | None = None,
        *,
        exchange: Callable[[str, str, str | None], "ExchangedToken"]
        | Callable[[str, str, str | None], "asyncio.Future"]
        | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        base = str(control_plane_url or "").strip().rstrip("/")
        parts = urlsplit(base)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise GatewayTokenError(
                "GATEWAY_CONFIG_INVALID", "网关模式需要有效的控制面地址（http/https）"
            )
        self._base = base
        self._access_token = str(access_token or "").strip()
        self._dept = str(dept or "").strip()
        self._exchange = exchange or _exchange_http
        self._now = now or time.time
        self._lock = asyncio.Lock()
        self._token = ""
        self._expires_at = 0.0

    @property
    def dept(self) -> str:
        return self._dept

    @property
    def cached_expires_at(self) -> float:
        return self._expires_at

    def _fresh(self) -> str:
        if self._token and self._expires_at - REFRESH_BEFORE_SECONDS > self._now():
            return self._token
        return ""

    async def get(self) -> str:
        token = self._fresh()
        if token:
            return token
        async with self._lock:
            token = self._fresh()
            if token:
                return token
            await self._refresh_locked()
            return self._token

    def invalidate(self) -> None:
        self._token = ""
        self._expires_at = 0.0

    async def refresh(self) -> str:
        async with self._lock:
            await self._refresh_locked()
            return self._token

    async def _refresh_locked(self) -> None:
        if not self._access_token:
            raise GatewayTokenError(
                "GATEWAY_TOKEN_MISSING",
                "网关模式需要账号登录令牌，但当前请求没有可用凭据",
            )
        result = await self._exchange(self._base, self._access_token, self._dept or None)
        self._token = result.token
        self._expires_at = result.expires_at
