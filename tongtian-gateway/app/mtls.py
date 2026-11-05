"""管理面 mTLS：证书由受信反向代理终结，网关只采信代理写入的校验结果。

网关自身不做 TLS 终结（见 deploy.md）：nginx 用 `ssl_verify_client on` 校验客户端证书，
再把 `$ssl_client_verify` / `$ssl_client_s_dn` 转成请求头。只有来自
`TT_GATEWAY_TRUSTED_PROXIES` 的对端，这两个头才会被采信；直连网关伪造无效。
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from functools import lru_cache

from starlette.requests import Request

from .config import settings

VERIFY_HEADER = "x-tt-client-verify"
DN_HEADER = "x-tt-client-dn"
VERIFIED_VALUE = "SUCCESS"
MAX_DN_LENGTH = 512
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


@dataclass(frozen=True)
class AdminPeer:
    """一次管理请求的对端身份；`verified` 表示代理已确认客户端证书有效。"""

    host: str
    subject: str
    verified: bool

    @property
    def rate_key(self) -> str:
        if self.verified:
            return f"cert:{self.subject or self.host or 'unknown'}"
        return f"ip:{self.host or 'unknown'}"


def parse_networks(raw: tuple[str, ...]) -> tuple[Network, ...]:
    return tuple(ipaddress.ip_network(item, strict=False) for item in raw)


@lru_cache(maxsize=4)
def _networks(raw: tuple[str, ...]) -> tuple[Network, ...]:
    # 配置非法时启动自检会拒绝；这里失败关闭，避免请求期抛异常
    try:
        return parse_networks(raw)
    except ValueError:
        return ()


def _address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def is_trusted_proxy(host: str, networks: tuple[str, ...]) -> bool:
    address = _address(host)
    if address is None:
        return False
    return any(address in network for network in _networks(networks))


def _subject(raw: str) -> str:
    cleaned = _CONTROL_CHARS.sub("", raw or "").strip()
    return cleaned[:MAX_DN_LENGTH]


def resolve_admin_peer(request: Request) -> AdminPeer:
    host = request.client.host if request.client else ""
    if settings.mtls_mode != "proxy" or not is_trusted_proxy(
        host, settings.trusted_proxies
    ):
        return AdminPeer(host, "", False)
    verify = (request.headers.get(VERIFY_HEADER) or "").strip().upper()
    if verify != VERIFIED_VALUE:
        return AdminPeer(host, "", False)
    return AdminPeer(host, _subject(request.headers.get(DN_HEADER) or ""), True)
