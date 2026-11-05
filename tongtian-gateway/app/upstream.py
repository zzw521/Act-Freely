"""上游地址安全校验：SSRF、DNS、私网与 metadata 黑名单。

管理员创建 provider 时校验一次；每次请求重新解析 DNS 再校验，
防止 DNS rebinding 到内网地址。`allowPrivate` 仅对本地推理服务生效。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from .errors import GatewayError

LOCAL_PROVIDERS = frozenset({"vllm", "ollama"})

METADATA_HOSTS = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)

MAX_HOST_LENGTH = 253


def parse_base_url(url: str):
    if not isinstance(url, str) or not url.strip():
        raise GatewayError.of("INVALID_REQUEST", "baseUrl 不能为空", param="baseUrl")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise GatewayError.of(
            "INVALID_REQUEST", "baseUrl 只支持 http/https", param="baseUrl"
        )
    if parts.username or parts.password:
        raise GatewayError.of(
            "INVALID_REQUEST", "baseUrl 不允许包含用户名或密码", param="baseUrl"
        )
    host = (parts.hostname or "").strip().rstrip(".")
    if not host:
        raise GatewayError.of("INVALID_REQUEST", "baseUrl 缺少主机名", param="baseUrl")
    if len(host) > MAX_HOST_LENGTH:
        raise GatewayError.of("INVALID_REQUEST", "baseUrl 主机名过长", param="baseUrl")
    try:
        port = parts.port
    except ValueError as exc:
        raise GatewayError.of(
            "INVALID_REQUEST", "baseUrl 端口不合法", param="baseUrl"
        ) from exc
    return parts, host, port


def _ip_blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address, allow_private: bool) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        return _ip_blocked(mapped, allow_private)
    # 云厂商 metadata 在链路本地段，任何情况下都不放行
    if address.is_link_local or address.is_multicast or address.is_reserved:
        return True
    if address.is_unspecified:
        return True
    if not address.is_global:
        return not allow_private
    return False


def _literal_ip(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def resolve_host(host: str, port: int | None) -> tuple[ipaddress._BaseAddress, ...]:
    if host.lower() in METADATA_HOSTS:
        raise GatewayError.of(
            "INVALID_REQUEST", "baseUrl 指向云厂商 metadata 地址", param="baseUrl"
        )
    literal = _literal_ip(host)
    if literal is not None:
        return (literal,)
    try:
        infos = socket.getaddrinfo(
            host, port or 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    except socket.gaierror as exc:
        raise GatewayError.of(
            "INVALID_REQUEST", f"baseUrl 主机无法解析：{host}", param="baseUrl"
        ) from exc
    addresses = []
    for info in infos:
        raw = info[4][0]
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise GatewayError.of(
            "INVALID_REQUEST", f"baseUrl 主机无法解析：{host}", param="baseUrl"
        )
    return tuple(addresses)


def validate_provider_url(
    url: str,
    *,
    allow_private: bool,
    resolve: bool = True,
) -> str:
    """管理端创建/修改 provider 时调用；失败抛 INVALID_REQUEST。"""
    parts, host, port = parse_base_url(url)
    if resolve:
        for address in resolve_host(host, port):
            if _ip_blocked(address, allow_private):
                raise GatewayError.of(
                    "INVALID_REQUEST",
                    "baseUrl 指向私网、回环或保留地址",
                    param="baseUrl",
                )
    return parts.geturl()


def verify_target_url(url: str, *, allow_private: bool) -> None:
    """每次请求前调用；解析失败或地址不安全时抛 PROVIDER_UNAVAILABLE。"""
    try:
        _parts, host, port = parse_base_url(url)
        addresses = resolve_host(host, port)
    except GatewayError as exc:
        raise GatewayError.of(
            "PROVIDER_UNAVAILABLE", "上游地址不可用", param=exc.param
        ) from exc
    for address in addresses:
        if _ip_blocked(address, allow_private):
            raise GatewayError.of(
                "PROVIDER_UNAVAILABLE", "上游地址解析到受限网络"
            )
