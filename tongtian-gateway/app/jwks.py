"""JWKS 公钥缓存：10 分钟 TTL、未知 kid 强制刷新、失败降级。

控制面轮换密钥后，网关可能在缓存窗口内收到未知 kid 的令牌；此时立即刷新
一次（两次刷新间隔不小于 30 秒），刷新成功即可继续服务。
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa

from .config import Settings
from .errors import GatewayError

logger = logging.getLogger("tongtian.gateway.jwks")

MAX_JWKS_BYTES = 64 * 1024
CACHE_TTL_SECONDS = 600
MIN_REFRESH_INTERVAL_SECONDS = 30
FETCH_TIMEOUT_SECONDS = 3.0


def _b64url_int(value: str) -> int:
    padding = "=" * (-len(value) % 4)
    return int.from_bytes(base64.urlsafe_b64decode(value + padding), "big")


class JwksClient:
    def __init__(
        self,
        url: str,
        *,
        ttl_seconds: int = CACHE_TTL_SECONDS,
        min_refresh_seconds: int = MIN_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        self._url = url
        self._ttl = ttl_seconds
        self._min_refresh = min_refresh_seconds
        self._lock = threading.RLock()
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._fetched_at = 0.0
        self._last_attempt = 0.0

    def get_key(self, kid: str) -> rsa.RSAPublicKey:
        with self._lock:
            now = time.monotonic()
            fresh = now - self._fetched_at < self._ttl
            if kid in self._keys and fresh:
                return self._keys[kid]

            may_refresh = now - self._last_attempt >= self._min_refresh
            if may_refresh:
                self._last_attempt = now
                try:
                    keys = self._fetch()
                except Exception as exc:  # noqa: BLE001 - 统一降级
                    logger.warning("刷新 JWKS 失败：%s", type(exc).__name__)
                else:
                    self._keys = keys
                    self._fetched_at = now
                    if kid in self._keys:
                        return self._keys[kid]
                    raise GatewayError.of("AUTH_REQUIRED", "令牌签名密钥未知")

            if not self._keys:
                raise GatewayError.of("AUTH_UNAVAILABLE", "认证公钥不可用")
            if kid in self._keys:
                # 缓存过期但刷新受限（30 秒内已试过）：继续用旧钥验签
                return self._keys[kid]
            raise GatewayError.of("AUTH_UNAVAILABLE", "令牌签名密钥未知且公钥不可刷新")

    def _fetch(self) -> dict[str, rsa.RSAPublicKey]:
        with httpx.Client(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False) as client:
            response = client.get(self._url, headers={"accept": "application/json"})
        if response.status_code != 200:
            raise RuntimeError(f"JWKS 返回 {response.status_code}")
        raw = response.content
        if len(raw) > MAX_JWKS_BYTES:
            raise RuntimeError("JWKS 超过 64KiB 上限")
        payload = json.loads(raw.decode("utf-8"))
        entries = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise RuntimeError("JWKS 缺少 keys")
        keys: dict[str, rsa.RSAPublicKey] = {}
        for item in entries:
            if not isinstance(item, dict):
                continue
            kid = str(item.get("kid") or "").strip()
            if not kid:
                continue
            if item.get("kty") != "RSA":
                continue
            if item.get("use") not in (None, "sig"):
                continue
            if item.get("alg") not in (None, "RS256"):
                continue
            n = item.get("n")
            e = item.get("e")
            if not isinstance(n, str) or not isinstance(e, str):
                continue
            try:
                public = rsa.RSAPublicNumbers(_b64url_int(e), _b64url_int(n)).public_key()
            except Exception:  # noqa: BLE001 - 单把坏钥不影响其它
                continue
            keys[kid] = public
        return keys

    def prime(self, keys: dict[str, rsa.RSAPublicKey] | None = None) -> None:
        """测试/预热：直接注入公钥集合。"""
        with self._lock:
            if keys is not None:
                self._keys = dict(keys)
                self._fetched_at = time.monotonic()


_clients: dict[str, JwksClient] = {}
_clients_lock = threading.Lock()


def client_for(settings: Settings) -> JwksClient:
    url = settings.jwks_url
    if not url:
        raise GatewayError.of("AUTH_UNAVAILABLE", "未配置 JWKS 地址")
    with _clients_lock:
        client = _clients.get(url)
        if client is None:
            client = JwksClient(url)
            _clients[url] = client
        return client


def reset_for_tests() -> None:
    with _clients_lock:
        _clients.clear()
