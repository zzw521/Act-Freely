#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""M7 部署演练：真实 TLS/mTLS、真三节点 Sentinel、Postgres 备份恢复。

G5–G10 用注入的代理头和假 Sentinel 覆盖了逻辑；本脚本补上部署侧的真实链路，
只验证行为、留证据，不改功能代码。结论写入 docs/m7-drill.md。

段落：
- tls：nginx 容器终结 TLS，真实 CA/服务端/客户端证书 + CRL，覆盖吊销、过期、
  未知 CA、非受信对端伪造校验头、代理漏配 set_header、协议面不受 mTLS 约束；
- sentinel：1 主 2 从 + 3 个真 Sentinel，杀主验证自动切换与共享桶（见 run_sentinel_drill）；
- postgres：最小权限角色、迁移幂等、pg_dump 备份恢复后账本一致（见 run_postgres_drill）。

用法:
  python scripts/m7_drill.py
  python scripts/m7_drill.py --only tls --keep
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS_DIR = Path(__file__).resolve().parent
GATEWAY_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

REPORT_PATH = GATEWAY_ROOT / "docs" / "m7-drill.md"
WORK_DIR = GATEWAY_ROOT / ".m7-drill"
PKI_DIR = WORK_DIR / "pki"

DOCKER = shutil.which("docker") or "docker"
GATEWAY_IMAGE = os.environ.get("M7_GATEWAY_IMAGE", "tongtian-gateway:drill")
PROXY_IMAGE = os.environ.get("M7_PROXY_IMAGE", "nginx:1-alpine")

RUN_TAG = uuid.uuid4().hex[:6]
NETWORK = f"tt-m7-net-{RUN_TAG}"
GW_NAME = f"tt-m7-gw-{RUN_TAG}"
PROXY_NAME = f"tt-m7-proxy-{RUN_TAG}"
SUBNETS = ("172.28.97.0/24", "172.29.123.0/24", "172.30.77.0/24", "172.31.55.0/24")

GATEWAY_ID = "gw-m7-drill"
ADMIN_TOKEN = "m7-admin-" + secrets.token_hex(12)
OPERATOR_CN = "tt-drill-operator"
ATTACKER_DN = "CN=attacker-from-rogue-host,O=NotTongtian"
START_TIMEOUT = 240.0

SECTION_TLS = "M7.1 真实 TLS/mTLS 反向代理"
SECTION_SENTINEL = "M7.2 真三节点 Sentinel 主从切换"
SECTION_POSTGRES = "M7.3 Postgres 生产迁移与备份恢复"

GUARD_PORT = 0
PASSTHRU_PORT = 0
BROKEN_PORT = 0
PROXY_IP = ""
GATEWAY_IP = ""


@dataclass
class Step:
    name: str
    ok: bool
    seconds: float
    detail: str
    section: str = ""


@dataclass
class Response:
    """一次经代理的 HTTPS 请求结果；握手失败时 status 为 None，error 记录原因。"""

    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: object = None
    text: str = ""
    error: str = ""
    tls_version: str = ""
    cipher: str = ""
    subject_cn: str = ""
    issuer_cn: str = ""
    san: tuple[str, ...] = ()

    @property
    def code(self) -> str:
        """网关错误码；nginx 自己回包时为空。"""
        body = self.body
        if not isinstance(body, dict):
            return ""
        error = body.get("error")
        if isinstance(error, dict):
            details = error.get("details") or [{}]
            first = details[0] if isinstance(details[0], dict) else {}
            return str(error.get("tt_code") or first.get("code") or error.get("code") or "")
        return str(body.get("code") or "")

    @property
    def from_gateway(self) -> bool:
        return bool(self.code) or (isinstance(self.body, dict) and self.body.get("ok") is True)

    def summary(self) -> str:
        if self.status is None:
            return f"握手失败：{self.error}"
        return f"HTTP {self.status} code={self.code or '-'} body={_clip(self.text)}"


def log(message: str) -> None:
    print(message, flush=True)


def record(
    steps: list[Step],
    name: str,
    ok: bool,
    detail: str = "",
    began: float = 0.0,
    section: str = "",
) -> Step:
    step = Step(name, bool(ok), time.perf_counter() - began, _flat(detail), section)
    log(f"  {'OK' if step.ok else 'NG'}  {name}" + (f" — {step.detail}" if step.detail else ""))
    steps.append(step)
    return step


def _flat(value: object) -> str:
    return " ".join(str(value).split())[:700]


def _clip(value: object, limit: int = 260) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return " ".join(text.split())[:limit]


# ── Docker ────────────────────────────────────────────────


def docker(*args: object, timeout: float = 240.0, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [DOCKER, *[str(item) for item in args]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"docker {args[0]} {args[1] if len(args) > 1 else ''} 失败：{_clip(proc.stderr or proc.stdout)}"
        )
    return proc


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def create_network(name: str = NETWORK, candidates: tuple[str, ...] = SUBNETS) -> str:
    """用户自定义 bridge：容器之间是真实内网地址，能证明受信代理绑定的不是回环。"""
    failures = []
    for subnet in candidates:
        proc = docker("network", "create", "--subnet", subnet, name, check=False)
        if proc.returncode == 0:
            return subnet
        failures.append(f"{subnet}：{_clip(proc.stderr or proc.stdout, 120)}")
    raise RuntimeError("隔离网络创建失败（候选网段全部冲突）：" + " | ".join(failures))


def pre_cleanup() -> None:
    """扫掉历史演练残留：容器与网络都按 tt-m7- 前缀。"""
    stale = docker("ps", "-aq", "--filter", "name=tt-m7-", check=False).stdout.split()
    if stale:
        docker("rm", "-f", *stale, check=False)
    for name in docker("network", "ls", "--filter", "name=tt-m7-", "-q", check=False).stdout.split():
        docker("network", "rm", name, check=False)


def cleanup(keep: bool) -> None:
    if keep:
        log(f"[保留] 本次容器（名字含 {RUN_TAG}）与网络 {NETWORK} / {SENT_NETWORK} / {PG_NETWORK}")
        return
    listing = docker("ps", "--all", "--format", "{{.ID}} {{.Names}}", check=False).stdout
    ids = [line.split()[0] for line in listing.splitlines() if RUN_TAG in line]
    if ids:
        docker("rm", "-f", *ids, check=False)
    for network in (NETWORK, SENT_NETWORK, PG_NETWORK):
        docker("network", "rm", network, check=False)


def container_logs(name: str, tail: int = 400) -> str:
    proc = docker("logs", "--tail", str(tail), name, check=False)
    return f"{proc.stdout}\n{proc.stderr}"


def container_state(name: str) -> str:
    return docker("inspect", "-f", "{{.State.Status}}", name, check=False).stdout.strip()


def container_boot(name: str) -> str:
    """(启动时间, 重启次数)：用来证明切换前后是同一个进程，不是被拉起来的。"""
    proc = docker(
        "inspect", "-f", "{{.State.StartedAt}} {{.RestartCount}}", name, check=False
    )
    return proc.stdout.strip() or "? ?"


def wait_for(predicate, *, timeout: float = 60.0, interval: float = 1.0):
    """轮询到 predicate 给出真值为止，返回 (是否命中, 最后一次取值)。"""
    deadline = time.time() + timeout
    last = None
    while True:
        try:
            last = predicate()
        except Exception as exc:  # noqa: BLE001 - 轮询期间容器可能还在启停
            last = f"{type(exc).__name__}: {exc}"
        if last:
            return True, last
        if time.time() >= deadline:
            return False, last
        time.sleep(interval)


def proxy_verify_reason(dn_hint: str) -> str:
    """取代理访问日志中该主体最近一次的 $ssl_client_verify 值（含失败原因）。"""
    for line in reversed(container_logs(PROXY_NAME, 300).splitlines()):
        if dn_hint in line:
            match = re.search(r"verify=(.*?) dn=", line)
            if match:
                return match.group(1)
    return ""


# ── 证书 ──────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _gen_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _distinguished_name(cn: str):
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    return x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Tongtian M7 Drill"),
            x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
        ]
    )


def _key_usage(*, ca: bool):
    from cryptography import x509

    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=not ca,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=ca,
        crl_sign=ca,
        encipher_only=False,
        decipher_only=False,
    )


def _to_pem(value) -> bytes:
    from cryptography.hazmat.primitives import serialization

    if hasattr(value, "private_bytes"):
        return value.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    return value.public_bytes(serialization.Encoding.PEM)


def _certificate_authority(cn: str, now: datetime):
    """自签 CA；返回 (证书, 私钥, Name)。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    key = _gen_key()
    name = _distinguished_name(cn)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(int.from_bytes(secrets.token_bytes(8), "big"))
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_key_usage(ca=True), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    return cert, key, name


def _leaf(
    cn: str,
    *,
    issuer_key,
    issuer_name,
    not_before: datetime,
    not_after: datetime,
    server_auth: bool = False,
    client_auth: bool = False,
    loopback_san: bool = False,
):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import ExtendedKeyUsageOID

    key = _gen_key()
    builder = (
        x509.CertificateBuilder()
        .subject_name(_distinguished_name(cn))
        .issuer_name(issuer_name)
        .public_key(key.public_key())
        .serial_number(int.from_bytes(secrets.token_bytes(8), "big"))
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_key_usage(ca=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
    )
    usages = []
    if server_auth:
        usages.append(ExtendedKeyUsageOID.SERVER_AUTH)
    if client_auth:
        usages.append(ExtendedKeyUsageOID.CLIENT_AUTH)
    if usages:
        builder = builder.add_extension(x509.ExtendedKeyUsage(usages), critical=False)
    if loopback_san:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
    return builder.sign(issuer_key, hashes.SHA256()), key


def build_pki() -> dict[str, str]:
    """演练用 PKI：CA、服务端、合法/吊销/过期/未知 CA 客户端与 CRL。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    now = _utcnow()
    PKI_DIR.mkdir(parents=True, exist_ok=True)

    ca_cert, ca_key, ca_name = _certificate_authority(f"tt-drill-ca-{RUN_TAG}", now)
    issued = {}
    for cn, stem, kwargs in (
        ("localhost", "server", {"server_auth": True, "loopback_san": True}),
        (OPERATOR_CN, "client-ok", {"client_auth": True}),
        ("tt-drill-revoked", "client-revoked", {"client_auth": True}),
    ):
        cert, key = _leaf(
            cn,
            issuer_key=ca_key,
            issuer_name=ca_name,
            not_before=now - timedelta(days=1),
            not_after=now + timedelta(days=5),
            **kwargs,
        )
        issued[stem] = (cert, key)

    expired_cert, expired_key = _leaf(
        "tt-drill-expired",
        issuer_key=ca_key,
        issuer_name=ca_name,
        not_before=now - timedelta(days=10),
        not_after=now - timedelta(days=2),
        client_auth=True,
    )

    rogue_ca, rogue_ca_key, rogue_ca_name = _certificate_authority(f"tt-drill-rogue-ca-{RUN_TAG}", now)
    rogue_cert, rogue_client_key = _leaf(
        "tt-drill-rogue-client",
        issuer_key=rogue_ca_key,
        issuer_name=rogue_ca_name,
        not_before=now - timedelta(days=1),
        not_after=now + timedelta(days=5),
        client_auth=True,
    )

    revoked_cert = issued["client-revoked"][0]
    crl = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(ca_name)
        .last_update(now - timedelta(hours=1))
        .next_update(now + timedelta(days=7))
        .add_revoked_certificate(
            x509.RevokedCertificateBuilder()
            .serial_number(revoked_cert.serial_number)
            .revocation_date(now - timedelta(hours=1))
            .build()
        )
        .sign(ca_key, hashes.SHA256())
    )

    blobs = {
        "ca.crt": _to_pem(ca_cert),
        "ca.key": _to_pem(ca_key),
        "server.crt": _to_pem(issued["server"][0]),
        "server.key": _to_pem(issued["server"][1]),
        "client-ok.crt": _to_pem(issued["client-ok"][0]),
        "client-ok.key": _to_pem(issued["client-ok"][1]),
        "client-revoked.crt": _to_pem(revoked_cert),
        "client-revoked.key": _to_pem(issued["client-revoked"][1]),
        "client-expired.crt": _to_pem(expired_cert),
        "client-expired.key": _to_pem(expired_key),
        "client-rogue.crt": _to_pem(rogue_cert),
        "client-rogue.key": _to_pem(rogue_client_key),
        "rogue-ca.crt": _to_pem(rogue_ca),
        "crl.pem": _to_pem(crl),
    }
    for name, data in blobs.items():
        (PKI_DIR / name).write_bytes(data)
    return {
        "ca_subject": f"tt-drill-ca-{RUN_TAG}",
        "revoked_serial": str(revoked_cert.serial_number),
        "expired_not_after": expired_cert.not_valid_after_utc.isoformat(),
        "files": ", ".join(sorted(blobs)),
    }


# ── nginx ─────────────────────────────────────────────────


NGINX_TEMPLATE = """worker_processes 1;
error_log /var/log/nginx/error.log info;
pid /tmp/nginx.pid;

events { worker_connections 256; }

http {
    default_type application/json;
    log_format drill '$time_iso8601 port=$server_port status=$status '
                     'proto=$ssl_protocol cipher=$ssl_cipher '
                     'verify=$ssl_client_verify dn="$ssl_client_s_dn"';
    access_log /var/log/nginx/access.log drill;

    server {
        listen 443 ssl;
        server_name localhost;
__SSL__
        location /gateway/v1/admin/ {
            if ($ssl_client_verify != SUCCESS) {
                return 401;
            }
            proxy_pass __UPSTREAM__;
__HEADERS__    }

        location / {
            proxy_pass __UPSTREAM__;
__HEADERS__    }
    }

    # 8443：代理正确写校验头但不做拦截 —— 验证网关自身失败关闭
    server {
        listen 8443 ssl;
        server_name localhost;
__SSL__
        location / {
            proxy_pass __UPSTREAM__;
__HEADERS__    }
    }

    # 9443：故意漏配 proxy_set_header —— 复现代理配置错误时的伪造面
    server {
        listen 9443 ssl;
        server_name localhost;
__SSL__
        location / {
            proxy_pass __UPSTREAM__;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
        }
    }
}
"""

SSL_BLOCK = """    ssl_certificate         /pki/server.crt;
    ssl_certificate_key     /pki/server.key;
    ssl_client_certificate  /pki/ca.crt;
    ssl_crl                 /pki/crl.pem;
    ssl_verify_client       optional;
    ssl_protocols           TLSv1.2 TLSv1.3;
    ssl_ciphers             HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
"""

HEADER_BLOCK = """            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-TT-Client-Verify $ssl_client_verify;
            proxy_set_header X-TT-Client-DN $ssl_client_s_dn;
            proxy_set_header X-TT-Admin-Actor "";
"""


def nginx_conf(gateway_ip: str) -> str:
    return (
        NGINX_TEMPLATE.replace("__UPSTREAM__", f"http://{gateway_ip}:8001")
        .replace("__SSL__", SSL_BLOCK)
        .replace("__HEADERS__", HEADER_BLOCK)
    )


# ── 请求 ──────────────────────────────────────────────────


def _common_name(groups) -> str:
    for group in groups or ():
        for key, value in group:
            if str(key).lower() == "commonname":
                return str(value)
    return ""


def tls_call(
    port: int,
    method: str,
    path: str,
    *,
    cert: str = "",
    key: str = "",
    headers: dict[str, str] | None = None,
    payload: dict | None = None,
    timeout: float = 20.0,
    tls_version: ssl.TLSVersion | None = None,
) -> Response:
    context = ssl.create_default_context(cafile=str(PKI_DIR / "ca.crt"))
    if cert and key:
        context.load_cert_chain(str(PKI_DIR / cert), str(PKI_DIR / key))
    if tls_version is not None:
        context.minimum_version = tls_version
        context.maximum_version = tls_version
    result = Response()
    connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=timeout)
    try:
        send = dict(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            send.setdefault("Content-Type", "application/json")
        connection.request(method, path, body, send)
        response = connection.getresponse()
        raw = response.read().decode("utf-8", "replace")
        result.status = response.status
        result.headers = {str(item[0]).lower(): str(item[1]) for item in response.getheaders()}
        result.text = raw
        try:
            result.body = json.loads(raw)
        except json.JSONDecodeError:
            result.body = raw
        socket_object = connection.sock
        if isinstance(socket_object, ssl.SSLSocket):
            result.tls_version = socket_object.version()
            cipher = socket_object.cipher()
            result.cipher = cipher[0] if cipher else ""
            peer = socket_object.getpeercert() or {}
            result.subject_cn = _common_name(peer.get("subject"))
            result.issuer_cn = _common_name(peer.get("issuer"))
            result.san = tuple(str(item[1]) for item in (peer.get("subjectAltName") or ()))
    except ssl.SSLError as exc:
        result.error = str(exc.strerror or exc)[:300]
    except (OSError, http.client.HTTPException) as exc:
        result.error = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        connection.close()
    return result


def admin_headers(**extra: str) -> dict[str, str]:
    headers = {"x-tt-admin-token": ADMIN_TOKEN}
    headers.update(extra)
    return headers


def audit_entries() -> list[dict]:
    """经代理读回管理审计条目；读不到返回空表。"""
    result = tls_call(
        GUARD_PORT,
        "GET",
        "/gateway/v1/admin/audit?after=0&limit=200",
        cert="client-ok.crt",
        key="client-ok.key",
        headers=admin_headers(),
    )
    body = result.body
    if not isinstance(body, dict):
        return []
    entries = body.get("entries")
    return [item for item in (entries or []) if isinstance(item, dict)]


# ── 容器 ──────────────────────────────────────────────────


def gateway_env(trusted_proxy_cidr: str) -> dict[str, str]:
    return {
        "TT_GATEWAY_ENV": "development",
        "TT_GATEWAY_JWT_MODE": "hs256-dev",
        "TT_GATEWAY_DEV_HS256_SECRET": "m7-drill-dev-secret",
        "TT_GATEWAY_MTLS_MODE": "proxy",
        "TT_GATEWAY_TRUSTED_PROXIES": trusted_proxy_cidr,
        "TT_GATEWAY_ADMIN_TOKEN": ADMIN_TOKEN,
        "TT_GATEWAY_ID": GATEWAY_ID,
        "TT_GATEWAY_MASTER_KEY": "MjM0NTY3ODkwMTIzNDU2Nzg5MDEyMzQ1Njc4OWFiY2Q=",
        "TT_GATEWAY_MASTER_KEY_VERSION": "1",
        "TT_GATEWAY_DATA_DIR": "/tmp/tt-m7",
        "TT_GATEWAY_WORKERS": "1",
        "TT_GATEWAY_CROSS_HOST": "0",
        "TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE": "600",
        "TT_GATEWAY_ADMIN_RATE_LIMIT_BURST": "200",
        "TT_GATEWAY_POLICY_TTL_SECONDS": "300",
    }


def env_args(env: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    return args


def static_ip(subnet: str, offset: int) -> str:
    return str(ipaddress.ip_network(subnet).network_address + offset)


def start_containers(subnet: str) -> str:
    """网关先起且不发端口；nginx 起来后按固定 IP 互指，代理是唯一受信对端。"""
    global GUARD_PORT, PASSTHRU_PORT, BROKEN_PORT, PROXY_IP, GATEWAY_IP
    GATEWAY_IP = static_ip(subnet, 20)
    PROXY_IP = static_ip(subnet, 21)
    rogue_ip = static_ip(subnet, 22)
    GUARD_PORT, PASSTHRU_PORT, BROKEN_PORT = free_port(), free_port(), free_port()

    docker(
        "run",
        "-d",
        "--name",
        GW_NAME,
        "--network",
        NETWORK,
        "--ip",
        GATEWAY_IP,
        *env_args(gateway_env(f"{PROXY_IP}/32")),
        GATEWAY_IMAGE,
    )
    conf = WORK_DIR / "nginx.conf"
    conf.write_text(nginx_conf(GATEWAY_IP), encoding="utf-8", newline="\n")
    docker(
        "run",
        "-d",
        "--name",
        PROXY_NAME,
        "--network",
        NETWORK,
        "--ip",
        PROXY_IP,
        "-p",
        f"127.0.0.1:{GUARD_PORT}:443",
        "-p",
        f"127.0.0.1:{PASSTHRU_PORT}:8443",
        "-p",
        f"127.0.0.1:{BROKEN_PORT}:9443",
        "-v",
        f"{conf}:/etc/nginx/nginx.conf:ro",
        "-v",
        f"{PKI_DIR}:/pki:ro",
        PROXY_IMAGE,
    )
    log(
        f"[容器] 网关 {GATEWAY_IP}:8001（未发布端口）；代理 {PROXY_IP} → "
        f"127.0.0.1:{GUARD_PORT}(拦截)/{PASSTHRU_PORT}(透传)/{BROKEN_PORT}(漏配)；旁路主机 {rogue_ip}"
    )
    return rogue_ip


def wait_for_proxy() -> None:
    deadline = time.time() + START_TIMEOUT
    last = ""
    while time.time() < deadline:
        state = docker("inspect", "-f", "{{.State.Status}}", GW_NAME, check=False).stdout.strip()
        if state and state != "running":
            raise RuntimeError(f"网关容器状态 {state}：{_clip(container_logs(GW_NAME, 20))}")
        result = tls_call(GUARD_PORT, "GET", "/health", timeout=5)
        if result.status == 200:
            return
        last = result.summary()
        proxy_state = docker("inspect", "-f", "{{.State.Status}}", PROXY_NAME, check=False).stdout.strip()
        if proxy_state and proxy_state != "running":
            raise RuntimeError(f"代理容器状态 {proxy_state}：{_clip(container_logs(PROXY_NAME, 20))}")
        time.sleep(2.0)
    raise RuntimeError(f"经代理等待网关就绪超时（{START_TIMEOUT:.0f}s）：{last}")


def refuse_start_without_trusted_proxies() -> str:
    """mTLS=proxy 而 TT_GATEWAY_TRUSTED_PROXIES 为空时，启动自检必须拒绝并点名变量。"""
    name = f"tt-m7-bad-{uuid.uuid4().hex[:6]}"
    docker("rm", "-f", name, check=False)
    env = gateway_env("")
    env["TT_GATEWAY_TRUSTED_PROXIES"] = ""
    docker("run", "-d", "--name", name, "--network", NETWORK, *env_args(env), GATEWAY_IMAGE)
    try:
        deadline = time.time() + 120
        exited = False
        while time.time() < deadline:
            if docker("inspect", "-f", "{{.State.Status}}", name, check=False).stdout.strip() == "exited":
                exited = True
                break
            time.sleep(1.0)
        if not exited:
            return "容器没有退出"
        text = container_logs(name, 60)
        lines = [line for line in text.splitlines() if "TRUSTED_PROXIES" in line]
        return " | ".join(lines[-3:]) if lines else _clip(text, 300)
    finally:
        docker("rm", "-f", name, check=False)


FORGER_PROBE = """
import json, os, urllib.error, urllib.request
base = os.environ["TT_PROBE_BASE"]
headers = {}
if os.environ.get("TT_PROBE_TOKEN"):
    headers["x-tt-admin-token"] = os.environ["TT_PROBE_TOKEN"]
if os.environ.get("TT_PROBE_FORGE") == "1":
    headers["X-TT-Client-Verify"] = "SUCCESS"
    headers["X-TT-Client-DN"] = os.environ.get("TT_PROBE_DN", "CN=forged")
request = urllib.request.Request(base + "/gateway/v1/admin/health", headers=headers, method="GET")
try:
    with urllib.request.urlopen(request, timeout=8) as response:
        print("PROBE" + json.dumps({"status": response.status, "body": response.read().decode("utf-8", "replace")[:240]}))
except urllib.error.HTTPError as exc:
    print("PROBE" + json.dumps({"status": exc.code, "body": exc.read().decode("utf-8", "replace")[:240]}))
except Exception as exc:
    print("PROBE" + json.dumps({"status": None, "body": type(exc).__name__ + ": " + str(exc)}))
"""


def probe_from_rogue(*, forge: bool, with_token: bool) -> dict:
    """同网段另一台主机直连网关：对端不是受信代理，伪造校验头也必须失败关闭。"""
    name = f"tt-m7-rogue-{uuid.uuid4().hex[:6]}"
    docker("rm", "-f", name, check=False)
    env = {"TT_PROBE_BASE": f"http://{GATEWAY_IP}:8001", "TT_PROBE_DN": ATTACKER_DN}
    if with_token:
        env["TT_PROBE_TOKEN"] = ADMIN_TOKEN
    if forge:
        env["TT_PROBE_FORGE"] = "1"
    proc = docker(
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        NETWORK,
        *env_args(env),
        GATEWAY_IMAGE,
        "python",
        "-c",
        FORGER_PROBE,
        timeout=300,
    )
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith("PROBE"):
            return json.loads(line[len("PROBE") :])
    return {"status": None, "body": _clip((proc.stderr or proc.stdout or "")[-300:])}


# ── M7.1 TLS 检查 ─────────────────────────────────────────


def run_tls_checks(steps: list[Step], section: str) -> None:
    began = time.perf_counter()
    health = tls_call(GUARD_PORT, "GET", "/health", cert="client-ok.crt", key="client-ok.key")
    record(
        steps,
        "真实 TLS 握手：服务端证书由演练 CA 签发、SAN 覆盖 localhost、协议 ≥ TLS1.2",
        health.status == 200
        and health.tls_version in {"TLSv1.2", "TLSv1.3"}
        and health.subject_cn == "localhost"
        and health.issuer_cn.startswith("tt-drill-ca")
        and "localhost" in health.san
        and "127.0.0.1" in health.san,
        f"{health.tls_version} / {health.cipher} / subject={health.subject_cn} "
        f"issuer={health.issuer_cn} san={list(health.san)}",
        began,
        section,
    )

    began = time.perf_counter()
    legacy = tls_call(GUARD_PORT, "GET", "/health", tls_version=ssl.TLSVersion.TLSv1_2)
    record(
        steps,
        "TLS1.2 可握手（兼容旧客户端；配置只放行 TLSv1.2/1.3）",
        legacy.status == 200 and legacy.tls_version == "TLSv1.2",
        f"TLS1.2 → HTTP {legacy.status} cipher={legacy.cipher}",
        began,
        section,
    )

    began = time.perf_counter()
    guarded = tls_call(
        GUARD_PORT, "GET", "/gateway/v1/admin/health", cert="client-ok.crt", key="client-ok.key", headers=admin_headers()
    )
    body = guarded.body if isinstance(guarded.body, dict) else {}
    record(
        steps,
        "合法客户端证书 + 管理令牌 → 管理面 200，回包来自网关自身",
        guarded.status == 200 and body.get("gatewayId") == GATEWAY_ID,
        f"{guarded.summary()}；database={_clip(body.get('database'))} "
        f"sharedState={_clip(body.get('sharedState'))}",
        began,
        section,
    )

    began = time.perf_counter()
    created = tls_call(
        GUARD_PORT,
        "POST",
        "/gateway/v1/admin/api-keys",
        cert="client-ok.crt",
        key="client-ok.key",
        headers=admin_headers(),
        payload={
            "label": "m7-tls",
            "tenant": "acme",
            "dept": "dept-default",
            "sub": "m7-drill",
            "scopes": ["chat"],
        },
    )
    actors = {str(item.get("action") or ""): str(item.get("actor") or "") for item in audit_entries()}
    actor = actors.get("api_key.create", "")
    record(
        steps,
        "证书 DN 写入审计主体（代理清空 X-TT-Admin-Actor 后仍可归因）",
        created.status == 201 and OPERATOR_CN in actor and "attacker" not in actor,
        f"签发 HTTP {created.status}；审计 actor={_clip(actor, 120)}",
        began,
        section,
    )

    began = time.perf_counter()
    no_cert = tls_call(GUARD_PORT, "GET", "/gateway/v1/admin/health", headers=admin_headers())
    record(
        steps,
        "不带客户端证书访问管理面 → 代理直接回 401（不是网关的错误体）",
        no_cert.status == 401 and not no_cert.from_gateway,
        no_cert.summary(),
        began,
        section,
    )

    began = time.perf_counter()
    forged_at_proxy = tls_call(
        GUARD_PORT,
        "GET",
        "/gateway/v1/admin/health",
        headers={
            "x-tt-admin-token": ADMIN_TOKEN,
            "X-TT-Client-Verify": "SUCCESS",
            "X-TT-Client-DN": ATTACKER_DN,
        },
    )
    record(
        steps,
        "客户端自带的校验头被代理覆盖：无证书时伪造无效",
        forged_at_proxy.status in (400, 401) and not forged_at_proxy.from_gateway,
        forged_at_proxy.summary(),
        began,
        section,
    )

    for label, stem, dn_hint in (
        ("被 CRL 吊销", "client-revoked", "tt-drill-revoked"),
        ("已过期", "client-expired", "tt-drill-expired"),
        ("未知签发者", "client-rogue", "tt-drill-rogue-client"),
    ):
        began = time.perf_counter()
        result = tls_call(
            GUARD_PORT,
            "GET",
            "/gateway/v1/admin/health",
            cert=f"{stem}.crt",
            key=f"{stem}.key",
            headers=admin_headers(),
        )
        rejected = result.status is None or (result.status in (400, 401) and not result.from_gateway)
        reason = proxy_verify_reason(dn_hint)
        record(
            steps,
            f"{label}的客户端证书 → 管理面不放行",
            rejected and reason.startswith("FAILED"),
            f"{result.summary()}；代理判定 verify={reason or '未定位'}",
            began,
            section,
        )

    began = time.perf_counter()
    probe = tls_call(GUARD_PORT, "GET", "/health")
    record(
        steps,
        "/health 不需要客户端证书：运维可无证书拨测存活",
        probe.status == 200,
        probe.summary(),
        began,
        section,
    )

    began = time.perf_counter()
    protocol = tls_call(
        GUARD_PORT,
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": "Bearer m7-not-a-real-token"},
        payload={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    record(
        steps,
        "协议面不受 mTLS 约束：无效令牌回 401 认证错误而不是 MTLS_REQUIRED",
        protocol.status == 401 and protocol.from_gateway and "MTLS" not in protocol.code,
        f"{protocol.status} code={protocol.code or '-'} body={_clip(protocol.text, 140)}",
        began,
        section,
    )

    began = time.perf_counter()
    passthru = tls_call(PASSTHRU_PORT, "GET", "/gateway/v1/admin/health", headers=admin_headers())
    record(
        steps,
        "代理只写校验头不设门槛（8443）时，网关自身失败关闭 → 401 MTLS_REQUIRED",
        passthru.status == 401 and passthru.code == "MTLS_REQUIRED",
        passthru.summary(),
        began,
        section,
    )

    began = time.perf_counter()
    rogue_forged = probe_from_rogue(forge=True, with_token=True)
    rogue_plain = probe_from_rogue(forge=False, with_token=True)
    record(
        steps,
        "同网段非受信主机直连网关伪造校验头 → 401 MTLS_REQUIRED（对端绑定生效）",
        rogue_forged.get("status") == 401
        and "MTLS_REQUIRED" in str(rogue_forged.get("body") or "")
        and rogue_plain.get("status") == 401,
        f"伪造头：{_clip(rogue_forged, 200)}；不带伪造头：{_clip(rogue_plain, 160)}",
        began,
        section,
    )

    began = time.perf_counter()
    forged_no_token = tls_call(
        BROKEN_PORT,
        "GET",
        "/gateway/v1/admin/health",
        cert="client-ok.crt",
        key="client-ok.key",
        headers={"X-TT-Client-Verify": "SUCCESS", "X-TT-Client-DN": ATTACKER_DN},
    )
    forged_with_token = tls_call(
        BROKEN_PORT,
        "POST",
        "/gateway/v1/admin/api-keys",
        cert="client-ok.crt",
        key="client-ok.key",
        headers=admin_headers(**{"X-TT-Client-Verify": "SUCCESS", "X-TT-Client-DN": ATTACKER_DN}),
        payload={
            "label": "m7-forged",
            "tenant": "acme",
            "dept": "dept-default",
            "sub": "m7-forged",
            "scopes": ["chat"],
        },
    )
    spoof_actor = {
        str(item.get("action") or ""): str(item.get("actor") or "") for item in audit_entries()
    }.get("api_key.create", "")
    still_gated = forged_no_token.status == 401 and forged_no_token.code == "AUTH_REQUIRED"
    record(
        steps,
        "代理漏配 proxy_set_header（9443）：伪造头仍过不了管理令牌这道门槛",
        still_gated,
        f"无令牌伪造：{forged_no_token.status}/{forged_no_token.code or '-'}；"
        f"有令牌伪造：{forged_with_token.status}，审计主体被伪造成 {_clip(spoof_actor, 120)}"
        "（残余风险：归因可伪造，见文档处置）",
        began,
        section,
    )

    began = time.perf_counter()
    gw_log = container_logs(GW_NAME, 600)
    cert_rejects_at_app = len(re.findall(r"/gateway/v1/admin/health HTTP/1\.1\" 400", gw_log))
    proxy_peer = f"{PROXY_IP}:" in gw_log
    app_rejects = len(re.findall(r"/gateway/v1/admin/health HTTP/1\.1\" 401", gw_log))
    record(
        steps,
        "网关侧证据：对端是代理的真实内网 IP，被代理拒绝的证书请求没进应用",
        proxy_peer and cert_rejects_at_app == 0 and app_rejects >= 3,
        f"代理对端 {PROXY_IP} 出现在访问日志={proxy_peer}；应用内证书类 400={cert_rejects_at_app}；"
        f"应用内 401（网关/代理失败关闭）={app_rejects}",
        began,
        section,
    )

    began = time.perf_counter()
    log_text = container_logs(GW_NAME, 600) + container_logs(PROXY_NAME, 600)
    leaks = [
        token
        for token in (ADMIN_TOKEN, "BEGIN PRIVATE KEY", "BEGIN RSA PRIVATE KEY")
        if token in log_text
    ]
    health_body = json.dumps(body, ensure_ascii=False)
    credential_free = "://" not in health_body and "password" not in health_body.lower()
    record(
        steps,
        "凭据卫生：健康页不回显连接串，网关与代理日志不含管理令牌/私钥",
        not leaks and credential_free,
        f"泄露项={leaks or '无'}；健康页含 '://'={not credential_free}",
        began,
        section,
    )

    began = time.perf_counter()
    refused = refuse_start_without_trusted_proxies()
    record(
        steps,
        "TT_GATEWAY_TRUSTED_PROXIES 为空时容器拒绝启动并点名变量",
        "TRUSTED_PROXIES" in refused,
        _clip(refused, 300),
        began,
        section,
    )


def run_tls_drill(steps: list[Step]) -> None:
    section = SECTION_TLS
    log(f"\n=== {section} ===")
    pre_cleanup()
    began = time.perf_counter()
    info = build_pki()
    record(
        steps,
        "生成演练 PKI（CA、服务端、合法/吊销/过期/未知 CA 客户端与 CRL）",
        (PKI_DIR / "crl.pem").is_file() and (PKI_DIR / "client-rogue.crt").is_file(),
        f"CA={info['ca_subject']} 吊销序列={info['revoked_serial']} "
        f"过期证书 notAfter={info['expired_not_after']} 文件={info['files']}",
        began,
        section,
    )
    subnet = create_network()
    log(f"[网络] {NETWORK} ({subnet})")
    began = time.perf_counter()
    start_containers(subnet)
    wait_for_proxy()
    record(
        steps,
        "nginx 终结 TLS、网关只监听内网且不发端口（真实部署拓扑）",
        True,
        f"隔离网段 {subnet}，受信代理={PROXY_IP}/32，网关容器无宿主端口映射",
        began,
        section,
    )
    run_tls_checks(steps, section)


# ── M7.2 真三节点 Sentinel ────────────────────────────────

REDIS_IMAGE = os.environ.get("M7_REDIS_IMAGE", "redis:7-alpine")
SENT_NETWORK = f"tt-m7-snet-{RUN_TAG}"
SENT_SUBNETS = ("172.21.7.0/24", "172.22.51.0/24", "172.23.13.0/24", "172.24.9.0/24")
SENTINEL_NAME = "tt-drill"
SENT_QUORUM = 2
SENT_DOWN_AFTER_MS = 3000
SENT_RPM = 60
SENT_BURST = 20
SENT_PREFIX = f"ttm7s-{RUN_TAG}"
REDIS_PASSWORD = "m7-sent-" + secrets.token_urlsafe(18)
BAD_REDIS_PASSWORD = "m7-sent-bad-" + secrets.token_hex(9)

RD_MASTER = f"tt-m7-rd-{RUN_TAG}"
RD_REPLICA_1 = f"tt-m7-rd1-{RUN_TAG}"
RD_REPLICA_2 = f"tt-m7-rd2-{RUN_TAG}"
RD_SENTINELS = tuple(f"tt-m7-sn{i}-{RUN_TAG}" for i in (1, 2, 3))
SENT_GW_A = f"tt-m7-gwa-{RUN_TAG}"
SENT_GW_B = f"tt-m7-gwb-{RUN_TAG}"
CLIENT_HAMMER = f"tt-m7-clih-{RUN_TAG}"
CLIENT_CONTROL = f"tt-m7-cllc-{RUN_TAG}"
CLIENT_WATCH = f"tt-m7-cllw-{RUN_TAG}"


def redis_conf_text(*, replica_of: str = "") -> str:
    lines = [
        "port 6379",
        f"requirepass {REDIS_PASSWORD}",
        f"masterauth {REDIS_PASSWORD}",
        'save ""',
        "appendonly no",
    ]
    if replica_of:
        lines.append(f"replicaof {replica_of} 6379")
    return "\n".join(lines) + "\n"


def sentinel_conf_text(master_ip: str) -> str:
    return "\n".join(
        [
            "port 26379",
            f"sentinel monitor {SENTINEL_NAME} {master_ip} 6379 {SENT_QUORUM}",
            f"sentinel down-after-milliseconds {SENTINEL_NAME} {SENT_DOWN_AFTER_MS}",
            f"sentinel parallel-syncs {SENTINEL_NAME} 1",
            f"sentinel failover-timeout {SENTINEL_NAME} 15000",
            f"sentinel auth-pass {SENTINEL_NAME} {REDIS_PASSWORD}",
        ]
    ) + "\n"


def start_redis_node(
    name: str, ip: str, conf_name: str, conf_text: str, sentinel: bool, *, network: str = SENT_NETWORK
) -> None:
    """配置用 docker cp 注入：口令不进命令行、不进容器 Args，只落在容器内的配置文件里。"""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    local = WORK_DIR / f"{name}-{conf_name}"
    local.write_text(conf_text, encoding="utf-8", newline="\n")
    docker(
        "create",
        "--name",
        name,
        "--network",
        network,
        "--ip",
        ip,
        REDIS_IMAGE,
        "redis-server",
        f"/data/{conf_name}",
        *(["--sentinel"] if sentinel else []),
    )
    docker("cp", str(local), f"{name}:/data/{conf_name}")
    docker("start", name)


def start_redis_topology(subnet: str) -> dict:
    master_ip = static_ip(subnet, 11)
    replica_ips = (static_ip(subnet, 12), static_ip(subnet, 13))
    sentinel_ips = tuple(static_ip(subnet, offset) for offset in (21, 22, 23))
    start_redis_node(RD_MASTER, master_ip, "redis.conf", redis_conf_text(), False)
    for name, ip in zip((RD_REPLICA_1, RD_REPLICA_2), replica_ips):
        start_redis_node(name, ip, "redis.conf", redis_conf_text(replica_of=master_ip), False)
    for index, ip in enumerate(sentinel_ips):
        start_redis_node(RD_SENTINELS[index], ip, "sentinel.conf", sentinel_conf_text(master_ip), True)
    log(
        f"[Redis] 主 {master_ip} 副本 {replica_ips[0]} {replica_ips[1]} "
        f"哨兵 {','.join(sentinel_ips)}（quorum={SENT_QUORUM}，down-after={SENT_DOWN_AFTER_MS}ms）"
    )
    nodes = [(RD_MASTER, master_ip), (RD_REPLICA_1, replica_ips[0]), (RD_REPLICA_2, replica_ips[1])]
    return {
        "subnet": subnet,
        "master": master_ip,
        "replicas": replica_ips,
        "sentinels": sentinel_ips,
        "sentinel_names": RD_SENTINELS,
        "gw": (static_ip(subnet, 31), static_ip(subnet, 32)),
        "clients": (static_ip(subnet, 41), static_ip(subnet, 42), static_ip(subnet, 43)),
        "nodes": nodes,
        "by_ip": {ip: name for name, ip in nodes},
    }


def start_sentinel_gateways(topo: dict) -> None:
    env = sentinel_gateway_env(topo)
    clients = topo["clients"]
    for name, ip in zip((SENT_GW_A, SENT_GW_B), topo["gw"]):
        docker("run", "-d", "--name", name, "--network", SENT_NETWORK, "--ip", ip, *env_args(env), GATEWAY_IMAGE)
    for name, ip in zip((CLIENT_HAMMER, CLIENT_CONTROL, CLIENT_WATCH), clients):
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            SENT_NETWORK,
            "--ip",
            ip,
            GATEWAY_IMAGE,
            "python",
            "-c",
            "import time\nwhile True:\n    time.sleep(30)",
        )
    log(f"[网关] 两个独立进程 {topo['gw']}，压测主体 {clients}")


def sentinel_gateway_env(topo: dict) -> dict[str, str]:
    env = gateway_env("127.0.0.1/32")
    env["TT_GATEWAY_MTLS_MODE"] = "off"
    env["TT_GATEWAY_TRUSTED_PROXIES"] = ""
    env["TT_GATEWAY_DATA_DIR"] = "/tmp/tt-m7-sentinel"
    env["TT_GATEWAY_REDIS_SENTINEL_MASTER"] = SENTINEL_NAME
    env["TT_GATEWAY_REDIS_SENTINEL_URLS"] = ",".join(
        f"{ip}:26379" for ip in topo["sentinels"]
    )
    env["TT_GATEWAY_REDIS_PASSWORD"] = REDIS_PASSWORD
    env["TT_GATEWAY_REDIS_PREFIX"] = SENT_PREFIX
    env["TT_GATEWAY_REDIS_TIMEOUT_MS"] = "500"
    env["TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE"] = str(SENT_RPM)
    env["TT_GATEWAY_ADMIN_RATE_LIMIT_BURST"] = str(SENT_BURST)
    return env


def _info_pairs(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


def redis_cli(name: str, *args: object, port: int = 6379, auth: bool = True) -> str:
    cmd = ["exec"]
    if auth:
        cmd += ["-e", f"REDISCLI_AUTH={REDIS_PASSWORD}"]
    proc = docker(
        *cmd, name, "redis-cli", "-p", str(port), *[str(item) for item in args],
        check=False, timeout=60,
    )
    if proc.returncode != 0:
        return f"[redis-cli 失败] {(proc.stderr or proc.stdout or '').strip()}"
    return (proc.stdout or "").strip()


def sentinel_cli(topo: dict, index: int, *args: object) -> str:
    return redis_cli(topo["sentinel_names"][index], *args, port=26379, auth=False)


def sentinel_master_ip(topo: dict) -> str:
    out = sentinel_cli(topo, 0, "sentinel", "get-master-addr-by-name", SENTINEL_NAME)
    first = [line.strip() for line in out.splitlines() if line.strip()]
    if first and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", first[0]):
        return first[0]
    return ""


def sentinel_view(topo: dict, index: int) -> dict[str, str]:
    lines = [
        line.strip()
        for line in sentinel_cli(topo, index, "sentinel", "master", SENTINEL_NAME).splitlines()
        if line.strip()
    ]
    return {lines[i]: lines[i + 1] for i in range(0, len(lines) - 1, 2)}


def cluster_facts(topo: dict) -> dict:
    """三个 Sentinel 各自看到的同一条事实：主库地址、副本数、同伴数、法定人数。"""
    views = []
    for index in range(len(topo["sentinel_names"])):
        view = sentinel_view(topo, index)
        views.append(
            {
                "sentinel": topo["sentinel_names"][index],
                "master": f"{view.get('ip', '')}:{view.get('port', '')}",
                "flags": view.get("flags", ""),
                "replicas": view.get("num-replicas", view.get("num-slaves", "")),
                "peers": view.get("num-other-sentinels", ""),
                "quorum": view.get("quorum", ""),
            }
        )
    ready = all(
        item["master"] == f"{topo['master']}:6379"
        and "master" in item["flags"]
        and item["replicas"] == "2"
        and item["peers"] == "2"
        and item["quorum"] == str(SENT_QUORUM)
        for item in views
    )
    return {"ready": ready, "views": views}


def repl_status(topo: dict) -> dict:
    """切换前先确认副本追平主库写入偏移，否则分不清复制延迟与真的丢数据。"""
    master_ip = sentinel_master_ip(topo)
    master_name = topo["by_ip"].get(master_ip, "")
    if not master_name:
        return {"ready": False, "detail": "Sentinel 尚未报出主库地址"}
    master = _info_pairs(redis_cli(master_name, "info", "replication"))
    if master.get("role") != "master":
        return {"ready": False, "detail": f"{master_name} 角色为 {master.get('role') or '未知'}"}
    offset = int(master.get("master_repl_offset") or 0)
    lag: list[str] = []
    worst = 0
    for name, _ in topo["nodes"]:
        if name == master_name:
            continue
        info = _info_pairs(redis_cli(name, "info", "replication"))
        if info.get("role") != "slave" or info.get("master_link_status") != "up":
            return {
                "ready": False,
                "detail": f"{name} 尚未就绪：role={info.get('role') or '未知'} "
                f"link={info.get('master_link_status') or '未知'}",
            }
        applied = int(info.get("slave_repl_offset") or 0)
        worst = max(worst, offset - applied)
        lag.append(f"{name}:{offset - applied}")
    return {"ready": worst <= 0, "detail": f"主={master_name} 偏移={offset} 落后={' '.join(lag)}"}


def wait_replication(topo: dict, timeout: float = 45.0) -> tuple[bool, str]:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        status = repl_status(topo)
        last = status["detail"]
        if status["ready"]:
            return True, last
        time.sleep(1.0)
    return False, last


SENT_PROBE = """
import json, os, time, urllib.error, urllib.request
bases = [os.environ["TT_PROBE_BASE"]]
if os.environ.get("TT_PROBE_BASE2"):
    bases.append(os.environ["TT_PROBE_BASE2"])
path = os.environ.get("TT_PROBE_PATH", "/gateway/v1/admin/health")
token = os.environ.get("TT_PROBE_TOKEN", "")
bearer = os.environ.get("TT_PROBE_BEARER", "")
method = os.environ.get("TT_PROBE_METHOD", "GET")
payload = os.environ.get("TT_PROBE_BODY", "")
count = int(os.environ.get("TT_PROBE_COUNT", "1"))
gap = float(os.environ.get("TT_PROBE_GAP_MS", "0")) / 1000.0
timeout = float(os.environ.get("TT_PROBE_TIMEOUT", "10"))
body_chars = int(os.environ.get("TT_PROBE_BODY_CHARS", "220"))
wanted = [item for item in os.environ.get("TT_PROBE_FIELDS", "").split(",") if item]
results = []
for index in range(count):
    base = bases[index % len(bases)]
    headers = {"x-tt-admin-token": token} if token else {}
    if bearer:
        headers["authorization"] = "Bearer " + bearer
    data = None
    if payload:
        headers["content-type"] = "application/json"
        data = payload.encode("utf-8")
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    began = time.monotonic()
    status, code, body, retry = None, "", "", ""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        body = exc.read().decode("utf-8", "replace")
        retry = exc.headers.get("retry-after") or ""
    except Exception as exc:
        code = type(exc).__name__
        body = str(exc)
    code_out = code
    picked = {}
    try:
        document = json.loads(body)
    except Exception:
        document = None
    if isinstance(document, dict):
        if wanted:
            picked = {key: document.get(key) for key in wanted}
        if not code_out:
            error = document.get("error")
            if isinstance(error, dict):
                details = error.get("details") or [{}]
                first = details[0] if isinstance(details[0], dict) else {}
                code_out = str(error.get("tt_code") or first.get("code") or error.get("code") or "")
            else:
                code_out = str(document.get("code") or "")
    results.append(
        {
            "gw": base,
            "status": status,
            "code": code_out,
            "retry": retry,
            "ms": round((time.monotonic() - began) * 1000),
            "fields": picked,
            "body": " ".join(body.split())[:body_chars],
        }
    )
    if gap and index + 1 < count:
        time.sleep(gap)
print("PROBE" + json.dumps({"results": results}, ensure_ascii=False))
"""


def client_probe(
    client: str,
    base: str,
    *,
    base2: str = "",
    count: int = 1,
    gap_ms: int = 0,
    fields: str = "",
    token: str = ADMIN_TOKEN,
    body_chars: int = 220,
    method: str = "GET",
    payload: str = "",
    bearer: str = "",
    path: str = "/gateway/v1/admin/health",
    timeout: float = 300.0,
) -> list[dict]:
    env = {
        "TT_PROBE_BASE": base,
        "TT_PROBE_BASE2": base2,
        "TT_PROBE_PATH": path,
        "TT_PROBE_TOKEN": token,
        "TT_PROBE_COUNT": str(count),
        "TT_PROBE_GAP_MS": str(gap_ms),
        "TT_PROBE_FIELDS": fields,
        "TT_PROBE_BODY_CHARS": str(body_chars),
        "TT_PROBE_METHOD": method,
    }
    if bearer:
        env["TT_PROBE_BEARER"] = bearer
    if payload:
        env["TT_PROBE_BODY"] = payload
    proc = docker(
        "exec", *env_args(env), client, "python", "-c", SENT_PROBE, check=False, timeout=timeout
    )
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith("PROBE"):
            return json.loads(line[len("PROBE") :]).get("results") or []
    return [
        {
            "gw": base,
            "status": None,
            "code": "PROBE_FAILED",
            "retry": "",
            "ms": 0,
            "fields": {},
            "body": _clip((proc.stderr or proc.stdout or ""), 240),
        }
    ]


def api_call(
    client: str,
    base: str,
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    bearer: str = "",
    token: str = ADMIN_TOKEN,
    fields: str = "",
) -> dict:
    """容器内发一次真实 HTTP 请求：管理面用令牌，协议面用 Bearer。"""
    results = client_probe(
        client,
        base,
        path=path,
        count=1,
        fields=fields,
        token=token,
        method=method,
        payload="" if body is None else json.dumps(body, ensure_ascii=False),
        bearer=bearer,
        body_chars=1200,
    )
    return results[0]


def count_results(
    results: list[dict], *, base: str = "", status: int | None = None, code: str = ""
) -> int:
    total = 0
    for item in results:
        if base and item.get("gw") != base:
            continue
        if status is not None and item.get("status") != status:
            continue
        if code and item.get("code") != code:
            continue
        total += 1
    return total


def summarize(results: list[dict]) -> str:
    counts: dict[str, int] = {}
    for item in results:
        label = f"{item.get('status')}/{item.get('code') or '-'}"
        counts[label] = counts.get(label, 0) + 1
    return " ".join(f"{label}×{value}" for label, value in sorted(counts.items()))


def gateway_urls(topo: dict) -> tuple[str, str]:
    return tuple(f"http://{ip}:8001" for ip in topo["gw"])


def read_health(
    topo: dict,
    *,
    client: str = CLIENT_WATCH,
    fields: str = "sharedState,workerPid,policyVersion",
) -> dict[str, dict]:
    urls = gateway_urls(topo)
    results = client_probe(client, urls[0], base2=urls[1], count=2, fields=fields)
    out: dict[str, dict] = {}
    for index, label in enumerate(("A", "B")):
        entry = {"url": urls[index], "status": None, "code": "", "sharedState": {}, "workerPid": None}
        for item in results:
            if item.get("gw") == urls[index]:
                entry["status"] = item.get("status")
                entry["code"] = item.get("code")
                entry["body"] = item.get("body")
                if item.get("fields"):
                    entry.update(item["fields"])
                break
        out[label] = entry
    return out


def health_ready(entry: dict) -> bool:
    state = entry.get("sharedState") or {}
    return (
        entry.get("status") == 200
        and state.get("mode") == "sentinel"
        and state.get("connected") is True
    )


def wait_both_ready(
    topo: dict,
    *,
    client: str = CLIENT_WATCH,
    timeout: float = 150.0,
    interval: float = 3.0,
    fields: str = "sharedState,workerPid,policyVersion",
    ready=health_ready,
):
    """轮询两个进程的健康页；间隔刻意大于令牌回填周期，避免演练自身被限流饿死。"""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = read_health(topo, client=client, fields=fields)
        if all(ready(entry) for entry in last.values()):
            return True, last
        time.sleep(interval)
    return False, last


def promoted_master(topo: dict, dead_ip: str) -> str:
    ip = sentinel_master_ip(topo)
    return ip if ip and ip != dead_ip and ip in topo["replicas"] else ""


def refuse_start_with_wrong_password(topo: dict) -> tuple[str, str]:
    """哨兵能发现主库，但主库 AUTH 口令错误：必须拒绝启动，不能静默退回内存态。"""
    name = f"tt-m7-sbad-{RUN_TAG}"
    docker("rm", "-f", name, check=False)
    env = sentinel_gateway_env(topo)
    env["TT_GATEWAY_REDIS_PASSWORD"] = BAD_REDIS_PASSWORD
    docker("run", "-d", "--name", name, "--network", SENT_NETWORK, *env_args(env), GATEWAY_IMAGE)
    try:
        deadline = time.time() + 180
        while time.time() < deadline and container_state(name) != "exited":
            time.sleep(1.0)
        text = container_logs(name, 150)
        if container_state(name) != "exited":
            return "容器没有退出（口令错误仍继续启动）", text
        lines = [
            line
            for line in text.splitlines()
            if "拒绝启动" in line or "SharedStateError" in line or "AuthenticationError" in line
        ]
        return (" | ".join(lines[-3:]) or _clip(text, 300)), text
    finally:
        docker("rm", "-f", name, check=False)


def run_sentinel_checks(steps: list[Step], topo: dict) -> None:
    section = SECTION_SENTINEL
    urls = gateway_urls(topo)
    began = time.perf_counter()

    def arbitration():
        facts = cluster_facts(topo)
        return facts["views"] if facts["ready"] else None

    ok, views = wait_for(arbitration, timeout=120.0, interval=2.0)
    record(
        steps,
        f"1 主 2 从 + 3 个真 Sentinel 就位，三个哨兵对同一主库达成一致（quorum={SENT_QUORUM}）",
        ok,
        _clip(views or "三个哨兵视角未达成一致（见 docker logs 哨兵容器）", 600),
        began,
        section,
    )

    began = time.perf_counter()
    ok, snapshot = wait_both_ready(topo)
    boots = {label: container_boot(name) for label, name in (("A", SENT_GW_A), ("B", SENT_GW_B))}
    record(
        steps,
        "两个独立网关进程经 Sentinel 发现主库（mode=sentinel、connected=true）",
        ok,
        _clip(
            {
                "A": (snapshot.get("A") or {}).get("sharedState"),
                "B": (snapshot.get("B") or {}).get("sharedState"),
                "启动时间/重启次数": boots,
            },
            600,
        ),
        began,
        section,
    )

    began = time.perf_counter()
    drained = client_probe(CLIENT_HAMMER, urls[0], count=SENT_BURST + 12)
    limited = count_results(drained, status=429, code="RATE_LIMITED")
    first_limit = next(
        (index + 1 for index, item in enumerate(drained) if item.get("status") == 429), 0
    )
    record(
        steps,
        f"真实对端主体打满 A 的管理面令牌桶（容量 {SENT_BURST}／{SENT_RPM} 每分钟）",
        limited >= 3 and count_results(drained, status=200) >= 1,
        f"分布 {summarize(drained)}；首个 429 在第 {first_limit} 次；"
        f"Retry-After={next((i.get('retry') for i in drained if i.get('retry')), '')}",
        began,
        section,
    )

    began = time.perf_counter()
    other = client_probe(CLIENT_HAMMER, urls[1], count=1)
    control = client_probe(CLIENT_CONTROL, urls[1], count=1)
    hammer_key = f"{SENT_PREFIX}:rl:admin:ip:{topo['clients'][0]}"
    record(
        steps,
        "同一主体转而请求 B 立即 429：桶在 Redis 里，两个进程共用同一份",
        count_results(other, status=429, code="RATE_LIMITED") == 1,
        f"B 返回 {summarize(other)}；桶键 {hammer_key}",
        began,
        section,
    )
    record(
        steps,
        "另一来源主体在 B 上仍放行：共享不等于串桶",
        count_results(control, status=200) == 1,
        f"对照主体 ip:{topo['clients'][1]} 返回 {summarize(control)}",
        began,
        section,
    )

    began = time.perf_counter()
    time.sleep(60.0 / SENT_RPM + 1.0)
    recovered = client_probe(CLIENT_HAMMER, urls[0], count=1)
    record(
        steps,
        "令牌按速率回填后同一主体恢复放行",
        count_results(recovered, status=200) == 1,
        f"等待 {60.0 / SENT_RPM + 1.0:.0f}s 后 {summarize(recovered)}",
        began,
        section,
    )

    began = time.perf_counter()
    synced, gap = wait_replication(topo)
    record(
        steps,
        "切换前先确认两个副本都追平了主库写入偏移",
        synced,
        gap,
        began,
        section,
    )

    dead_ip = sentinel_master_ip(topo)
    dead_name = topo["by_ip"].get(dead_ip, "")
    if not dead_name:
        raise RuntimeError(f"杀主前找不到主库容器：Sentinel 报出 {dead_ip or '空地址'}")
    began = time.perf_counter()
    docker("kill", dead_name, timeout=90)
    outage = client_probe(CLIENT_WATCH, urls[0], base2=urls[1], count=16, gap_ms=400)
    fails_a = count_results(outage, base=urls[0], status=503, code="SHARED_STATE_UNAVAILABLE")
    fails_b = count_results(outage, base=urls[1], status=503, code="SHARED_STATE_UNAVAILABLE")
    record(
        steps,
        f"杀主（{dead_name}）窗口内两个进程都失败关闭：503 SHARED_STATE_UNAVAILABLE",
        fails_a >= 1 and fails_b >= 1,
        f"A 命中 {fails_a} 次、B 命中 {fails_b} 次；分布 {summarize(outage)}；"
        f"最慢一次 {max((item.get('ms') or 0) for item in outage)}ms（未挂死在 Redis 上）",
        began,
        section,
    )

    began = time.perf_counter()
    ok, promoted = wait_for(lambda: promoted_master(topo, dead_ip), timeout=150.0)
    switch_lines = [
        line
        for line in container_logs(topo["sentinel_names"][0], 120).splitlines()
        if "+switch-master" in line
    ]
    record(
        steps,
        "Sentinel 三节点仲裁把副本自动提升为新主（无人工介入）",
        bool(ok),
        f"新主 {promoted}（{topo['by_ip'].get(promoted, '未知容器')}，原副本）；"
        f"{switch_lines[-1] if switch_lines else '未取到 +switch-master 日志'}",
        began,
        section,
    )

    began = time.perf_counter()
    ok, after = wait_both_ready(topo)
    still_same = all(
        container_boot(name) == boots[label] for label, name in (("A", SENT_GW_A), ("B", SENT_GW_B))
    )
    restarts = sum(
        int(docker("inspect", "-f", "{{.RestartCount}}", name, check=False).stdout.strip() or 0)
        for name in (SENT_GW_A, SENT_GW_B)
    )
    record(
        steps,
        "两个进程不重启即自动重连新主（容器启动时间未变、RestartCount=0）",
        ok and still_same and restarts == 0,
        _clip(
            {
                "A": (after.get("A") or {}).get("sharedState"),
                "B": (after.get("B") or {}).get("sharedState"),
                "同一进程": still_same,
                "重启次数": restarts,
                "切换前": boots,
            },
            600,
        ),
        began,
        section,
    )

    began = time.perf_counter()
    new_name = topo["by_ip"].get(sentinel_master_ip(topo), "")
    keys = [line for line in redis_cli(new_name, "keys", f"{SENT_PREFIX}:rl:admin:*").split() if line]
    record(
        steps,
        "被提升的副本带着切换前的共享限流桶（异步复制已落到新主）",
        any(f"ip:{topo['clients'][0]}" in key for key in keys) and bool(keys),
        f"新主 {new_name} 的桶键 {_clip(keys, 500)}",
        began,
        section,
    )

    began = time.perf_counter()
    again = client_probe(CLIENT_HAMMER, urls[0], count=SENT_BURST + 12)
    then_b = client_probe(CLIENT_HAMMER, urls[1], count=1)
    record(
        steps,
        "新主上跨进程共享限流仍然成立（A 打满后同一主体在 B 立即 429）",
        count_results(again, status=429, code="RATE_LIMITED") >= 3
        and count_results(then_b, status=429, code="RATE_LIMITED") == 1,
        f"A：{summarize(again)}；B：{summarize(then_b)}",
        began,
        section,
    )

    began = time.perf_counter()
    pause_name = topo["by_ip"].get(sentinel_master_ip(topo), "")
    docker("pause", pause_name, timeout=90)
    blocked = client_probe(CLIENT_WATCH, urls[0], base2=urls[1], count=14, gap_ms=400)
    docker("unpause", pause_name, timeout=90)
    blocked_a = count_results(blocked, base=urls[0], status=503, code="SHARED_STATE_UNAVAILABLE")
    blocked_b = count_results(blocked, base=urls[1], status=503, code="SHARED_STATE_UNAVAILABLE")
    ok, healed = wait_both_ready(topo)
    record(
        steps,
        f"主库网络阻塞（pause {pause_name}）同样失败关闭，解除后两个进程自愈",
        blocked_a >= 1 and blocked_b >= 1 and ok,
        f"阻塞期 503：A {blocked_a} 次／B {blocked_b} 次；分布 {summarize(blocked)}；"
        f"自愈后 {_clip((healed.get('A') or {}).get('sharedState'))} "
        f"{_clip((healed.get('B') or {}).get('sharedState'))}",
        began,
        section,
    )

    began = time.perf_counter()
    conclusion, bad_logs = refuse_start_with_wrong_password(topo)
    record(
        steps,
        "主库口令错误的新进程拒绝启动（不静默退回内存态，不带着半套状态服务）",
        "拒绝启动" in conclusion and container_state(SENT_GW_A) == "running",
        conclusion,
        began,
        section,
    )

    began = time.perf_counter()
    names = [SENT_GW_A, SENT_GW_B, *topo["sentinel_names"], *[name for name, _ in topo["nodes"]]]
    haystack = "\n".join([container_logs(name, 800) for name in names] + [bad_logs])
    full_health = client_probe(CLIENT_WATCH, urls[0], body_chars=4000)
    haystack += "\n" + " ".join(item.get("body", "") for item in full_health)
    argv = "".join(
        docker(
            "inspect", "-f", "{{json .Config.Cmd}} {{json .Args}}", name, check=False
        ).stdout
        for name in [RD_MASTER, RD_REPLICA_1, RD_REPLICA_2, *topo["sentinel_names"]]
    )
    leaked = [
        value
        for value in (REDIS_PASSWORD, BAD_REDIS_PASSWORD, ADMIN_TOKEN, "redis://", "rediss://")
        if value and value in haystack
    ]
    record(
        steps,
        "Redis/Sentinel 口令、连接串不进日志与健康页，口令也不出现在容器命令行",
        not leaked and REDIS_PASSWORD not in argv,
        f"日志/健康页命中={leaked or '无'}；容器 Args 命中口令={'是' if REDIS_PASSWORD in argv else '否'}；"
        f"健康页摘要={_clip((full_health[0].get('body') or '')[:180])}",
        began,
        section,
    )


def run_sentinel_drill(steps: list[Step]) -> None:
    section = SECTION_SENTINEL
    log(f"\n=== {section} ===")
    pre_cleanup()
    began = time.perf_counter()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    subnet = create_network(SENT_NETWORK, SENT_SUBNETS)
    topo = start_redis_topology(subnet)
    record(
        steps,
        "起真实 Redis 主从与 3 个 Sentinel（口令只写进容器内配置文件）",
        all(container_state(name) == "running" for name in [RD_MASTER, RD_REPLICA_1, RD_REPLICA_2, *RD_SENTINELS]),
        f"网段 {subnet}，主从 3 个 + 哨兵 3 个",
        began,
        section,
    )
    began = time.perf_counter()
    start_sentinel_gateways(topo)
    ready = all(container_state(name) == "running" for name in (SENT_GW_A, SENT_GW_B))
    record(
        steps,
        "起两个独立网关进程（各自 SQLite，共享同一套 Sentinel 发现地址）",
        ready,
        f"网关 {SENT_GW_A} / {SENT_GW_B}，前缀 {SENT_PREFIX}，限流 {SENT_BURST}/{SENT_RPM} 每分钟",
        began,
        section,
    )
    run_sentinel_checks(steps, topo)


# ── M7.3 Postgres 生产迁移与备份恢复 ──────────────────────

PG_IMAGE = os.environ.get("M7_PG_IMAGE", "postgres:16-alpine")
PG_NETWORK = f"tt-m7-pnet-{RUN_TAG}"
PG_SUBNETS = ("172.25.19.0/24", "172.26.41.0/24", "172.27.8.0/24", "172.28.63.0/24")
PG_PREFIX = f"ttm7p-{RUN_TAG}"
PG_SUPER = "postgres"
PG_APP_ROLE = "ttgw_app"
PG_SUPER_PASSWORD = "m7-pg-super-" + secrets.token_urlsafe(18)
PG_APP_PASSWORD = "m7-pg-app-" + secrets.token_urlsafe(18)
PG_DB = "ttgw"
PG_BACKUP_DB = "ttgw_backup"
PG_TENANT = "acme"
PG_DEPT = "dept-m7pg"
PG_SUB = "u-m7pg"
PROVIDER_SECRET = "sk-m7pg-" + secrets.token_hex(9)
PROMPT_MARKER = "m7noprompt" + secrets.token_hex(8)
CHAT_COUNT = 6
PG_MODEL = "openai_compatible"

PG_NAME = f"tt-m7-pg-{RUN_TAG}"
PG_REDIS = f"tt-m7-pgrd-{RUN_TAG}"
PG_UPSTREAM = f"tt-m7-pgup-{RUN_TAG}"
PG_GW_A = f"tt-m7-pga-{RUN_TAG}"
PG_GW_B = f"tt-m7-pgb-{RUN_TAG}"
PG_GW_C = f"tt-m7-pgc-{RUN_TAG}"
PG_CLIENT = f"tt-m7-pgcl-{RUN_TAG}"
PG_BADGW = f"tt-m7-pgbad-{RUN_TAG}"
PG_POLICY_PATH = WORK_DIR / "policy.json"
DUMP_PATH = WORK_DIR / f"ttgw-{RUN_TAG}.dump"

HEALTH_FIELDS = "sharedState,database,policyVersion,usageQueue"

FAKE_UPSTREAM = """
import json, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

USAGE = {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, payload, status=200):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._send(
                {
                    "object": "list",
                    "data": [{"id": "fake-openai-model", "object": "model", "owned_by": "fake"}],
                }
            )
        else:
            self._send({"error": {"message": "unknown path"}}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)  # 请求体读完即丢：假上游也不留提示词
        self._send(
            {
                "id": "chatcmpl-m7pg",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "fake-openai-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": USAGE,
            }
        )


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
"""


def pg_init_sql() -> str:
    """最小权限：应用角色只登录、不建角色/库/复制；另设备份恢复目标库。"""
    return "\n".join(
        [
            f"CREATE ROLE {PG_APP_ROLE} LOGIN PASSWORD '{PG_APP_PASSWORD}'",
            "    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;",
            f"CREATE DATABASE {PG_DB} OWNER {PG_APP_ROLE};",
            f"CREATE DATABASE {PG_BACKUP_DB} OWNER {PG_APP_ROLE};",
            "REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;",
        ]
    ) + "\n"


def pg_policy_document(gateway_base: str) -> dict:
    return {
        "contractVersion": "gateway-contract.v1",
        "policyVersion": "m7-pg#1",
        "tenant": PG_TENANT,
        "gateway": {"url": gateway_base},
        "noLog": True,
        "byok": {"allowed": True, "hardQuota": False},
        "models": {
            "fake-chat": {
                "provider": PG_MODEL,
                "upstreamModel": "fake-openai-model",
                "capabilities": ["chat"],
                "contextWindow": 32768,
                "maxOutputTokens": 4096,
                "priceVersion": "m7-pg",
                "pricing": {"currency": "USD", "inputPer1M": "1.00", "outputPer1M": "2.00"},
            }
        },
        "routes": {"auto": {"chain": ["fake-chat"], "fallbackAllowed": False}},
        "allowedModels": ["auto", "fake-chat"],
        "quotaDefaults": {"monthlyTokens": 1000000, "concurrency": 4, "maxOutputTokens": 4096},
        "departments": {PG_DEPT: {"allowedModels": ["auto", "fake-chat"]}},
    }


def psql(
    sql: str, *, database: str = PG_DB, user: str = PG_APP_ROLE, password: str = ""
) -> subprocess.CompletedProcess:
    """在 Postgres 容器内以指定身份跑一条 SQL：口令走 PGPASSWORD，不进命令行。"""
    secret = password or (PG_APP_PASSWORD if user == PG_APP_ROLE else PG_SUPER_PASSWORD)
    return docker(
        "exec",
        "-e",
        f"PGPASSWORD={secret}",
        PG_NAME,
        "psql",
        "-X",
        "-q",
        "-v",
        "ON_ERROR_STOP=1",
        "-tA",
        "-c",
        sql,
        "-h",
        "127.0.0.1",
        "-U",
        user,
        "-d",
        database,
        check=False,
    )


def pg_value(sql: str, **kwargs: object) -> str:
    proc = psql(sql, **kwargs)  # type: ignore[arg-type]
    text = (proc.stdout or "").strip()
    return text.splitlines()[0].strip() if proc.returncode == 0 and text else ""


def as_int(text: object) -> int:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return -1


def start_postgres(ip: str) -> None:
    """初始化 SQL 用 docker cp 注入：应用口令只落在容器内文件与角色定义里。"""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    local = WORK_DIR / "01-init.sql"
    local.write_text(pg_init_sql(), encoding="utf-8", newline="\n")
    docker(
        "create",
        "--name",
        PG_NAME,
        "--network",
        PG_NETWORK,
        "--ip",
        ip,
        "-e",
        f"POSTGRES_PASSWORD={PG_SUPER_PASSWORD}",
        PG_IMAGE,
    )
    docker("cp", str(local), f"{PG_NAME}:/docker-entrypoint-initdb.d/01-init.sql")
    docker("start", PG_NAME)


def start_postgres_topology(subnet: str) -> dict:
    topo = {
        "subnet": subnet,
        "pg": static_ip(subnet, 11),
        "redis": static_ip(subnet, 12),
        "upstream": static_ip(subnet, 21),
        "gw": (static_ip(subnet, 31), static_ip(subnet, 32)),
        "gw_c": static_ip(subnet, 33),
        "clients": (static_ip(subnet, 41),),
    }
    start_postgres(topo["pg"])
    ready, probe = wait_for(
        lambda: psql("SELECT 1", database=PG_DB, user=PG_SUPER).returncode == 0,
        timeout=240.0,
        interval=3.0,
    )
    if not ready:
        raise RuntimeError(f"Postgres 未就绪：{_clip(container_logs(PG_NAME, 30), 400)} | {probe}")
    databases = pg_value(
        f"SELECT count(*) FROM pg_database WHERE datname IN ('{PG_DB}','{PG_BACKUP_DB}')"
    )
    if as_int(databases) != 2:
        raise RuntimeError(f"业务库/恢复库未建成：databases={databases}")
    log(f"[Postgres] {PG_IMAGE} 主实例 {topo['pg']}，库 {PG_DB} / {PG_BACKUP_DB}，角色 {PG_APP_ROLE}")
    return topo


def start_pg_dependencies(topo: dict) -> None:
    start_redis_node(
        PG_REDIS, topo["redis"], "redis-pg.conf", redis_conf_text(), False, network=PG_NETWORK
    )
    docker(
        "run",
        "-d",
        "--name",
        PG_UPSTREAM,
        "--network",
        PG_NETWORK,
        "--ip",
        topo["upstream"],
        GATEWAY_IMAGE,
        "python",
        "-c",
        FAKE_UPSTREAM,
    )
    docker(
        "run",
        "-d",
        "--name",
        PG_CLIENT,
        "--network",
        PG_NETWORK,
        "--ip",
        topo["clients"][0],
        GATEWAY_IMAGE,
        "python",
        "-c",
        "import time\nwhile True:\n    time.sleep(30)",
    )
    log(f"[依赖] Redis {topo['redis']}、假上游 {topo['upstream']}:8080、请求主体 {topo['clients'][0]}")


def pg_gateway_env(topo: dict, *, database: str = PG_DB) -> dict[str, str]:
    env = gateway_env("127.0.0.1/32")
    env["TT_GATEWAY_MTLS_MODE"] = "off"
    env["TT_GATEWAY_TRUSTED_PROXIES"] = ""
    # 假上游只在这个隔离的演练网段里，与其余验收脚本同一内网前提
    env["TT_GATEWAY_ALLOW_PRIVATE_PROVIDERS"] = "1"
    env["TT_GATEWAY_DATA_DIR"] = "/tmp/tt-m7-pg"
    env["TT_GATEWAY_DB_URL"] = (
        f"postgresql://{PG_APP_ROLE}:{PG_APP_PASSWORD}@{topo['pg']}:5432/{database}"
    )
    env["TT_GATEWAY_CROSS_HOST"] = "1"
    env["TT_GATEWAY_REDIS_URL"] = f"redis://:{REDIS_PASSWORD}@{topo['redis']}:6379/0"
    env["TT_GATEWAY_REDIS_PREFIX"] = PG_PREFIX
    env["TT_GATEWAY_REDIS_TIMEOUT_MS"] = "800"
    env["TT_GATEWAY_POLICY_FILE"] = "/policy/policy.json"
    env["TT_GATEWAY_USAGE_FLUSH_SECONDS"] = "1"
    env["TT_GATEWAY_USAGE_BATCH_SIZE"] = "1"
    return env


def start_pg_gateway(name: str, ip: str, env: dict[str, str]) -> None:
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        PG_NETWORK,
        "--ip",
        ip,
        "-v",
        f"{PG_POLICY_PATH}:/policy/policy.json:ro",
        *env_args(env),
        GATEWAY_IMAGE,
    )


def start_pg_gateways(topo: dict) -> None:
    """两个进程几乎同时首启，逼迁移在 pg_advisory_xact_lock 内竞争。"""
    env = pg_gateway_env(topo)
    for name, ip in zip((PG_GW_A, PG_GW_B), topo["gw"]):
        start_pg_gateway(name, ip, env)


def pg_health_ready(entry: dict) -> bool:
    database = entry.get("database") or {}
    shared = entry.get("sharedState") or {}
    return (
        entry.get("status") == 200
        and database.get("backend") == "postgres"
        and database.get("connected") is True
        and as_int(database.get("schemaVersion")) > 0
        and shared.get("connected") is True
    )


def health_single(client: str, base: str, *, fields: str = HEALTH_FIELDS) -> dict:
    entry = api_call(client, base, "/gateway/v1/admin/health", fields=fields)
    merged = dict(entry)
    merged.update(entry.get("fields") or {})
    return merged


def wait_gateway_ready(client: str, base: str, *, timeout: float = 200.0, interval: float = 3.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = health_single(client, base)
        if pg_health_ready(last):
            return True, last
        time.sleep(interval)
    return False, last


def chat_payload(marker: str) -> dict:
    return {
        "model": "auto",
        "messages": [{"role": "user", "content": f"{marker} 只回一个词"}],
        "max_tokens": 32,
    }


def pg_backup_restore() -> dict:
    """pg_dump -Fc → 宿主落盘 → 同一实例的恢复库 pg_restore，全程用最小权限角色。"""
    dump = docker(
        "exec",
        "-e",
        f"PGPASSWORD={PG_APP_PASSWORD}",
        PG_NAME,
        "pg_dump",
        "-U",
        PG_APP_ROLE,
        "-d",
        PG_DB,
        "-Fc",
        "-f",
        "/tmp/ttgw.dump",
        check=False,
    )
    out = docker("cp", f"{PG_NAME}:/tmp/ttgw.dump", str(DUMP_PATH), check=False)
    size = DUMP_PATH.stat().st_size if DUMP_PATH.is_file() else 0
    into = docker("cp", str(DUMP_PATH), f"{PG_NAME}:/tmp/ttgw-restore.dump", check=False)
    restore = docker(
        "exec",
        "-e",
        f"PGPASSWORD={PG_APP_PASSWORD}",
        PG_NAME,
        "pg_restore",
        "-U",
        PG_APP_ROLE,
        "-d",
        PG_BACKUP_DB,
        "--no-owner",
        "--exit-on-error",
        "/tmp/ttgw-restore.dump",
        check=False,
    )
    return {
        "dump_rc": dump.returncode,
        "cp_rc": out.returncode + into.returncode,
        "restore_rc": restore.returncode,
        "bytes": size,
        "restore_error": _clip(restore.stderr or restore.stdout, 240),
        "dump_error": _clip(dump.stderr, 200),
    }


def newer_schema_behavior(topo: dict, current: int) -> dict:
    """把 schema 版本改到高于程序支持值：M8.1 的启动门槛要让进程直接拒起，
    而不是放它出去服务、等第一个请求再回 500。"""
    name = PG_BADGW
    ip = static_ip(topo["subnet"], 34)
    url = f"http://{ip}:8001"
    docker("rm", "-f", name, check=False)
    newer = current + 92
    psql(f"UPDATE schema_version SET version = {newer}")
    facts = {
        "newer": newer,
        "state": "",
        "exited": False,
        "served": None,
        "versionAfter": "",
        "refusal": "",
        "named": "",
        "replay": [],
        "leak": [],
    }
    try:
        start_pg_gateway(name, ip, pg_gateway_env(topo))

        def settled() -> dict | None:
            """容器退出=拒绝启动；容器仍在且能回包=旧行为，必须让断言失败。"""
            state = container_state(name)
            if state != "running":
                return {"exited": state or "gone"}
            entry = api_call(PG_CLIENT, url, "/gateway/v1/admin/health")
            status = entry.get("status")
            return None if not status else {"served": status}

        settled_flag, outcome = wait_for(settled, timeout=120.0, interval=3.0)
        outcome = outcome if isinstance(outcome, dict) else {}
        facts["exited"] = settled_flag and "exited" in outcome
        facts["served"] = outcome.get("served")
        facts["state"] = container_state(name) or "gone"
        text = container_logs(name, 400)
        refusal = [line for line in text.splitlines() if "账本不可用，拒绝启动" in line]
        named = [
            line
            for line in text.splitlines()
            if f"版本 {newer} 高于程序支持的 {current}" in line
        ]
        facts["versionAfter"] = pg_value("SELECT coalesce(max(version), -1) FROM schema_version")
        facts["refusal"] = refusal[-1].strip() if refusal else ""
        facts["named"] = named[-1].strip() if named else ""
        facts["replay"] = [
            token for token in ("already exists", "duplicate key") if token in text
        ]
        facts["leak"] = [
            label
            for label, needle in (
                ("DSN", "postgresql://"),
                ("Postgres 应用口令", PG_APP_PASSWORD),
            )
            if needle in text
        ]
        return facts
    finally:
        docker("rm", "-f", name, check=False)
        psql(f"UPDATE schema_version SET version = {current}")


def prompt_marker_leaked() -> dict:
    """提示词既不能进日志，也不能进任何一行账本；顺带确认账本里没有存内容的列。"""
    logs = {}
    for name in (PG_GW_A, PG_GW_B, PG_GW_C):
        if container_state(name) == "running" and PROMPT_MARKER in container_logs(name, 900):
            logs[name] = "日志出现提示词"
    rows = pg_value(
        "SELECT (SELECT count(*) FROM usage_events WHERE usage_events::text LIKE "
        f"'%{PROMPT_MARKER}%') + (SELECT count(*) FROM audit_log WHERE audit_log::text LIKE "
        f"'%{PROMPT_MARKER}%')"
    )
    # 只算文本/JSON 型列：prompt_tokens 这类整型计数列是账本本身要存的量
    columns = pg_value(
        "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND "
        "data_type IN ('text', 'character varying', 'json', 'jsonb') AND (column_name ILIKE "
        "'%prompt%' OR column_name ILIKE '%completion%' OR column_name ILIKE '%content%' "
        "OR column_name ILIKE '%message%' OR column_name ILIKE '%body%' "
        "OR column_name ILIKE '%text%')"
    )
    counters = pg_value(
        "SELECT string_agg(column_name, ',') FROM information_schema.columns "
        "WHERE table_schema='public' AND data_type IN ('integer', 'bigint') AND "
        "(column_name ILIKE '%token%' OR column_name ILIKE '%prompt%')"
    )
    return {
        "logs": logs,
        "ledgerRows": as_int(rows),
        "contentColumns": as_int(columns),
        "counters": counters,
    }


def pg_secret_leak(topo: dict) -> dict[str, list[str]]:
    """日志/健康页/容器命令行里都不该出现 DSN、Postgres 与 Redis 口令、管理令牌、provider 明文密钥。"""
    needles = {
        "Postgres 应用口令": PG_APP_PASSWORD,
        "Postgres 超级口令": PG_SUPER_PASSWORD,
        "Redis 口令": REDIS_PASSWORD,
        "管理令牌": ADMIN_TOKEN,
        "provider 密钥明文": PROVIDER_SECRET,
        "DSN": "postgresql://",
        "Redis 连接串": "redis://",
    }
    blobs: dict[str, str] = {}
    for name in (PG_GW_A, PG_GW_B, PG_GW_C, PG_REDIS, PG_UPSTREAM):
        if container_state(name):
            blobs[f"{name} 日志"] = container_logs(name, 900)
    for name in (PG_GW_A, PG_GW_B, PG_GW_C):
        inspect = docker("inspect", "-f", "{{json .Config.Cmd}}", name, check=False)
        blobs[f"{name} Cmd"] = inspect.stdout
    for label, url in zip(("A", "B", "C"), pg_gateway_urls(topo)):
        entry = api_call(PG_CLIENT, url, "/gateway/v1/admin/health", fields="gatewayId")
        blobs[f"健康页 {label}"] = json.dumps(entry, ensure_ascii=False)
    found: dict[str, list[str]] = {}
    for label, needle in needles.items():
        hits = [source for source, text in blobs.items() if needle in text]
        if hits:
            found[label] = hits
    return found


def pg_gateway_urls(topo: dict) -> tuple[str, ...]:
    urls = gateway_urls(topo)
    return (urls[0], urls[1], f"http://{topo['gw_c']}:8001")


def run_postgres_checks(steps: list[Step], topo: dict) -> None:
    section = SECTION_POSTGRES
    urls = gateway_urls(topo)
    url_a, url_b = urls[0], urls[1]
    url_c = f"http://{topo['gw_c']}:8001"

    began = time.perf_counter()
    ok, snapshot = wait_both_ready(
        topo, client=PG_CLIENT, fields=HEALTH_FIELDS, ready=pg_health_ready
    )
    disclosed = {
        label: {
            "status": (snapshot.get(label) or {}).get("status"),
            "code": (snapshot.get(label) or {}).get("code"),
            "database": (snapshot.get(label) or {}).get("database"),
            "sharedState": (snapshot.get(label) or {}).get("sharedState"),
        }
        for label in ("A", "B")
    }
    leaked_body = [
        label
        for label in ("A", "B")
        if "postgresql://" in str((snapshot.get(label) or {}).get("body") or "")
        or PG_APP_PASSWORD in str((snapshot.get(label) or {}).get("body") or "")
    ]
    record(
        steps,
        "两个独立进程连同一个 Postgres：健康页都报 backend=postgres、connected、schemaVersion",
        ok and not leaked_body,
        f"{_clip(disclosed, 400)}；DSN/口令泄露={leaked_body or '无'}",
        began,
        section,
    )

    began = time.perf_counter()
    wait_for(lambda: pg_value("SELECT count(*) FROM schema_version"), timeout=60.0, interval=2.0)
    rows = pg_value("SELECT count(*) FROM schema_version")
    version = pg_value("SELECT coalesce(max(version), -1) FROM schema_version")
    tables = pg_value(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
    )
    schema_version = as_int(version)
    record(
        steps,
        "空库并发首启：迁移在 pg_advisory_xact_lock 内只应用一次，schema_version 只有一行",
        as_int(rows) == 1 and schema_version > 0 and as_int(tables) >= 10,
        f"schema_version 行数={rows} version={version} public 表数={tables}（两个进程同时首启竞争）",
        began,
        section,
    )

    began = time.perf_counter()
    flags = pg_value(
        "SELECT rolname || '=' || rolsuper || ',' || rolcreaterole || ',' || rolcreatedb "
        f"|| ',' || rolreplication FROM pg_roles WHERE rolname='{PG_APP_ROLE}'"
    )
    deny_role = psql("CREATE ROLE ttgw_hacker LOGIN PASSWORD 'nope'")
    deny_file = psql("SELECT pg_read_file('pg_hba.conf')")
    deny_connect = psql("SELECT 1", database="postgres")
    denied = {
        "CREATE ROLE": (deny_role.returncode, _clip(deny_role.stderr, 160)),
        "pg_read_file": (deny_file.returncode, _clip(deny_file.stderr, 160)),
        "连 postgres 库": (deny_connect.returncode, _clip(deny_connect.stderr, 160)),
    }
    record(
        steps,
        "应用角色最小权限：非超级用户/不建角色/不建库/不复制，越权语句与连 postgres 库全部被拒",
        flags.count("false") == 4
        and "true" not in flags
        and deny_role.returncode != 0
        and "permission denied" in deny_role.stderr
        and deny_file.returncode != 0
        and "permission denied" in deny_file.stderr
        and deny_connect.returncode != 0
        and "connect privilege" in deny_connect.stderr.lower(),
        f"{flags}；拒绝明细 {_clip(denied, 500)}",
        began,
        section,
    )

    began = time.perf_counter()
    docker("restart", PG_GW_B, timeout=120)
    ready_b, entry_b = wait_gateway_ready(PG_CLIENT, url_b)
    rows_after = pg_value("SELECT count(*) FROM schema_version")
    log_text = container_logs(PG_GW_B, 250)
    replay = [token for token in ("already exists", "Traceback", "duplicate key") if token in log_text]
    record(
        steps,
        "重启第二个进程不重放迁移：schema_version 仍一行、日志无 already exists/Traceback",
        ready_b and as_int(rows_after) == 1 and not replay,
        f"重启后健康 {_clip((entry_b or {}).get('database'), 200)}；行数={rows_after}；命中词={replay or '无'}",
        began,
        section,
    )

    began = time.perf_counter()
    provider = api_call(
        PG_CLIENT,
        url_a,
        "/gateway/v1/admin/providers",
        method="POST",
        body={
            "id": PG_MODEL,
            "label": "m7-pg-fake",
            "baseUrl": f"http://{topo['upstream']}:8080/v1",
            "timeoutSeconds": 30,
            "allowPrivate": True,
        },
    )
    provider_key = api_call(
        PG_CLIENT,
        url_a,
        f"/gateway/v1/admin/providers/{PG_MODEL}/keys",
        method="POST",
        body={"label": "m7-pg-secret", "secret": PROVIDER_SECRET},
        fields="key",
    )
    api_key = api_call(
        PG_CLIENT,
        url_a,
        "/gateway/v1/admin/api-keys",
        method="POST",
        body={
            "label": "m7-pg-user",
            "tenant": PG_TENANT,
            "dept": PG_DEPT,
            "sub": PG_SUB,
            "scopes": ["chat"],
            "modelAllowlist": ["auto", "fake-chat"],
        },
        fields="key",
    )
    secret = str(((api_key.get("fields") or {}).get("key") or {}).get("secret") or "")
    record(
        steps,
        "管理面写入 Postgres：provider、provider 密钥、签发 API Key 各一行",
        provider.get("status") == 201
        and provider_key.get("status") == 201
        and api_key.get("status") == 201
        and bool(secret),
        f"provider={provider.get('status')} 密钥={provider_key.get('status')}"
        f"（明文只在此响应出现一次） API Key={api_key.get('status')}；"
        f"providers={pg_value('SELECT count(*) FROM providers')} "
        f"provider_keys={pg_value('SELECT count(*) FROM provider_keys')} "
        f"api_keys={pg_value('SELECT count(*) FROM api_keys')}",
        began,
        section,
    )

    began = time.perf_counter()
    chats = client_probe(
        PG_CLIENT,
        url_a,
        base2=url_b,
        path="/v1/chat/completions",
        count=CHAT_COUNT,
        bearer=secret,
        token="",
        method="POST",
        payload=json.dumps(chat_payload(PROMPT_MARKER), ensure_ascii=False),
        fields="usage",
        gap_ms=300,
    )
    succeeded = count_results(chats, status=200)

    def ledger_caught_up() -> int | None:
        count = as_int(pg_value("SELECT count(*) FROM usage_events"))
        return count if count and count >= succeeded else None

    landed, usage_rows = wait_for(ledger_caught_up, timeout=60.0, interval=2.0)
    quota_rows = pg_value("SELECT count(*) FROM quota_counters")
    audit_rows = pg_value("SELECT count(*) FROM audit_log")
    record(
        steps,
        f"{CHAT_COUNT} 次真实对话（A/B 轮流）写进同一份 Postgres 账本",
        landed and succeeded == CHAT_COUNT and as_int(audit_rows) >= 3,
        f"对话 200={succeeded}/{CHAT_COUNT} 分布 {summarize(chats)}；"
        f"usage_events={usage_rows} quota_counters={quota_rows} audit_log={audit_rows}",
        began,
        section,
    )

    began = time.perf_counter()
    usage_a = api_call(
        PG_CLIENT, url_a, "/gateway/v1/admin/usage?period=total", fields="totals"
    )
    usage_b = api_call(
        PG_CLIENT, url_b, "/gateway/v1/admin/usage?period=total", fields="totals"
    )
    totals_a = (usage_a.get("fields") or {}).get("totals") or {}
    totals_b = (usage_b.get("fields") or {}).get("totals") or {}

    def stable(totals: dict) -> tuple:
        return tuple(
            (key, totals.get(key))
            for key in ("calls", "promptTokens", "completionTokens", "totalTokens", "costs")
        )

    record(
        steps,
        "A、B 读到的用量总额逐字段相同：跨主机账本不分叉",
        stable(totals_a) == stable(totals_b) and as_int(totals_a.get("calls")) >= CHAT_COUNT,
        f"A={_clip(totals_a, 260)} B={_clip(totals_b, 260)}",
        began,
        section,
    )

    began = time.perf_counter()
    verify_a = api_call(
        PG_CLIENT,
        url_a,
        "/gateway/v1/admin/audit/verify",
        fields="entries,headHash,brokenAt,legacyRows",
    )
    facts_a = verify_a.get("fields") or {}
    record(
        steps,
        "审计哈希链在 Postgres 上自洽：brokenAt 空、legacyRows=0",
        verify_a.get("status") == 200
        and not facts_a.get("brokenAt")
        and facts_a.get("legacyRows") in (0, None)
        and as_int(facts_a.get("entries")) >= 3,
        _clip(facts_a, 400),
        began,
        section,
    )

    began = time.perf_counter()
    backup = pg_backup_restore()
    backup_rows = pg_value("SELECT count(*) FROM schema_version", database=PG_BACKUP_DB)
    backup_tables = pg_value(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'",
        database=PG_BACKUP_DB,
    )
    record(
        steps,
        f"pg_dump -Fc 备份（{backup['bytes']} 字节）→ 宿主落盘 → pg_restore --no-owner --exit-on-error 到恢复库",
        backup["dump_rc"] == 0
        and backup["cp_rc"] == 0
        and backup["restore_rc"] == 0
        and backup["bytes"] > 0
        and as_int(backup_rows) == 1
        and as_int(backup_tables) == as_int(tables),
        f"dump rc={backup['dump_rc']} cp rc={backup['cp_rc']} restore rc={backup['restore_rc']}；"
        f"表数 主库={tables} 恢复库={backup_tables}；"
        f"{_clip(backup['restore_error'] or backup['dump_error'] or 'restore 无告警', 240)}",
        began,
        section,
    )

    began = time.perf_counter()
    start_pg_gateway(PG_GW_C, topo["gw_c"], pg_gateway_env(topo, database=PG_BACKUP_DB))
    ready_c, entry_c = wait_gateway_ready(PG_CLIENT, url_c)
    verify_c = api_call(
        PG_CLIENT,
        url_c,
        "/gateway/v1/admin/audit/verify",
        fields="entries,headHash,brokenAt,legacyRows",
    )
    facts_c = verify_c.get("fields") or {}
    usage_c = api_call(PG_CLIENT, url_c, "/gateway/v1/admin/usage?period=total", fields="totals")
    totals_c = (usage_c.get("fields") or {}).get("totals") or {}
    version_c = pg_value("SELECT count(*) FROM schema_version", database=PG_BACKUP_DB)
    record(
        steps,
        "指向恢复库的第三个进程不重放迁移，审计条数/headHash 与用量总额和主库逐字段一致",
        ready_c
        and facts_c.get("headHash") == facts_a.get("headHash")
        and facts_c.get("entries") == facts_a.get("entries")
        and stable(totals_c) == stable(totals_a)
        and as_int(version_c) == 1,
        f"C 健康 {_clip((entry_c or {}).get('database'), 200)}；恢复库 schema_version 行数={version_c}；"
        f"审计 A={facts_a.get('entries')}/{_clip(facts_a.get('headHash'), 20)} "
        f"C={facts_c.get('entries')}/{_clip(facts_c.get('headHash'), 20)}；"
        f"用量一致={stable(totals_c) == stable(totals_a)}",
        began,
        section,
    )

    began = time.perf_counter()
    restored = api_call(
        PG_CLIENT,
        url_c,
        "/v1/chat/completions",
        method="POST",
        token="",
        bearer=secret,
        body=chat_payload("m7restored"),
        fields="usage",
    )
    record(
        steps,
        "恢复库直接可用：同一 master key 解密 provider 密钥，Bearer 对话在新进程上 200",
        restored.get("status") == 200,
        _clip({"status": restored.get("status"), "usage": (restored.get("fields") or {}).get("usage"),
               "code": restored.get("code"), "body": restored.get("body")}, 400),
        began,
        section,
    )

    began = time.perf_counter()
    facts_new = newer_schema_behavior(topo, schema_version)
    rolled_back = pg_value("SELECT coalesce(max(version), -1) FROM schema_version")
    record(
        steps,
        f"schema 版本被改到 {facts_new['newer']}（高于程序支持的 {schema_version}）时：启动门槛拒起进程，"
        "不放它出去服务、不改写回版本、不静默降级",
        facts_new["exited"]
        and facts_new["served"] is None
        and bool(facts_new["refusal"])
        and bool(facts_new["named"])
        and facts_new["versionAfter"] == str(facts_new["newer"])
        and not facts_new["replay"]
        and not facts_new["leak"]
        and as_int(rolled_back) == schema_version,
        f"容器状态={facts_new['state']}；服务过请求={facts_new['served']}；"
        f"启动错误：{_clip(facts_new['refusal'], 160)}；点名两版本：{_clip(facts_new['named'], 160)}；"
        f"版本未被改写回={facts_new['versionAfter']}；迁移重放词={facts_new['replay'] or '无'}；"
        f"日志泄露={facts_new['leak'] or '无'}；改回后 version={rolled_back}",
        began,
        section,
    )

    began = time.perf_counter()
    leak = prompt_marker_leaked()
    record(
        steps,
        "提示词不落盘：唯一标记不出现在三个进程日志，也不在 usage_events/audit_log 任何行里",
        not leak["logs"] and leak["ledgerRows"] == 0 and leak["contentColumns"] == 0,
        f"日志命中={leak['logs'] or '无'}；账本命中行数={leak['ledgerRows']}；"
        f"文本/JSON 型内容列数={leak['contentColumns']}；"
        f"账本只存计数列：{_clip(leak['counters'], 200)}",
        began,
        section,
    )

    began = time.perf_counter()
    found = pg_secret_leak(topo)
    record(
        steps,
        "凭据卫生：DSN、Postgres/Redis 口令、管理令牌、provider 明文都不在日志/健康页/容器 Cmd",
        not found,
        f"命中 {_clip(found, 400) or '无'}（口令与 DSN 只经环境变量注入，"
        "`docker inspect .Config.Env` 仍可见，属宿主侧敏感信息）",
        began,
        section,
    )


def run_postgres_drill(steps: list[Step]) -> None:
    section = SECTION_POSTGRES
    log(f"\n=== {section} ===")
    pre_cleanup()
    began = time.perf_counter()
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    subnet = create_network(PG_NETWORK, PG_SUBNETS)
    topo = start_postgres_topology(subnet)
    record(
        steps,
        "起 Postgres（初始化 SQL 经 docker cp 注入：最小权限角色 + 业务库/恢复库）",
        container_state(PG_NAME) == "running",
        f"网段 {subnet}，实例 {PG_NAME} @ {topo['pg']}，库 {PG_DB}/{PG_BACKUP_DB}，角色 {PG_APP_ROLE}",
        began,
        section,
    )
    began = time.perf_counter()
    PG_POLICY_PATH.write_text(
        json.dumps(pg_policy_document(gateway_urls(topo)[0]), ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    start_pg_dependencies(topo)
    start_pg_gateways(topo)
    record(
        steps,
        "起 Redis + 假上游 + 两个网关进程（TT_GATEWAY_CROSS_HOST=1，共享同一 Postgres 账本）",
        all(
            container_state(name) == "running"
            for name in (PG_REDIS, PG_UPSTREAM, PG_CLIENT, PG_GW_A, PG_GW_B)
        ),
        f"网关 {topo['gw']}，策略文件只读挂载 /policy/policy.json，前缀 {PG_PREFIX}",
        began,
        section,
    )
    run_postgres_checks(steps, topo)


# ── 报告 ──────────────────────────────────────────────────

RUNNERS = {
    "tls": run_tls_drill,
    "sentinel": run_sentinel_drill,
    "postgres": run_postgres_drill,
}


TLS_NOTES = [
    "- TLS 只在反向代理终结，网关按 `TT_GATEWAY_MTLS_MODE=proxy` 只采信来自",
    "  `TT_GATEWAY_TRUSTED_PROXIES` 的 `$ssl_client_verify/$ssl_client_s_dn`；",
    "  uvicorn `--no-proxy-headers` 保证对端地址不可被 `X-Forwarded-For` 改写。",
    "- 吊销/过期/未知签发者的证书在代理侧即被拒（`ssl_crl` + `ssl_client_certificate`），",
    "  管理面不放行；合法证书的主体经 `$ssl_client_s_dn` 落到审计 `actor`。",
    "- 代理漏配 `proxy_set_header` 时客户端伪造的校验头可透传到网关：仍能挡住",
    "  无管理令牌的调用，但会污染审计归因。部署清单（`docs/deploy.md §6`、`§13.1`）因此要求",
    "  逐条核对三行：`X-TT-Client-Verify`/`X-TT-Client-DN` 由代理写入，`X-TT-Admin-Actor` 清空。",
]

SENTINEL_NOTES = [
    "- 主从与 3 个 Sentinel 都是真容器（`redis:7-alpine`），配置经 `docker cp` 注入：",
    "  Redis 口令不进命令行、不进容器 Args、不落日志，健康页只报模式与连通性。",
    "- 主库被杀后由 Sentinel 仲裁提升副本，两个网关进程不重启自动重连；限流桶跟着",
    "  异步复制落到新主，切换前脚本先等复制偏移追平，避免把复制延迟当成丢数据。",
    "- 无论进程退出还是网络阻塞（`docker pause`），Redis 不可用一律 `503",
    "  SHARED_STATE_UNAVAILABLE` 失败关闭，不回退成进程内计数、不静默放行。",
    "- 本段只覆盖 Redis 侧共享状态：配额账本仍是各容器自己的 SQLite",
    "  （`TT_GATEWAY_CROSS_HOST=0`）；跨主机共享账本属 Postgres，见 M7.3。",
    "- 残留：Sentinel 自身未开启客户端鉴权（`TT_GATEWAY_REDIS_SENTINEL_PASSWORD`",
    "  只有单测覆盖），Redis 侧 TLS（`rediss://`）与 Sentinel TLS 未在本段演练。",
]


POSTGRES_NOTES = [
    "- Postgres 侧用最小权限角色：`LOGIN` 但非超级用户、不建角色/库、不复制；`CREATE ROLE`、",
    "  `pg_read_file('pg_hba.conf')` 与连 `postgres` 库都被服务端拒绝（42501 / 无 CONNECT 权限）。",
    "- 两个进程同时首启时迁移在 `pg_advisory_xact_lock` 内竞争，`schema_version` 只落一行；",
    "  重启任一进程都不重放 DDL。版本高于程序支持值时不改写回，也不放进程出去服务：",
    "  M8.1 起账本在启动期校验，容器直接退出并在日志里点名两个版本（旧行为是懒连接，",
    "  进程照常起来、第一个请求才回 500）。",
    "- `pg_dump -Fc` 备份 → 同实例恢复库 → 第三个进程指向恢复库：审计条数与 `headHash` 与主库",
    "  逐字相同，用量总额一致，provider 密钥仍能用同一 `TT_GATEWAY_MASTER_KEY` 解密并跑通对话。",
    "- 对话账本只存 token 计数、成本与错误码：public 模式没有文本/JSON 型内容列，唯一提示词标记",
    "  既不进日志也不进 `usage_events`/`audit_log`。",
    "- DSN、Postgres/Redis 口令、管理令牌只经环境变量注入，不出现在日志、健康页与容器 Cmd；",
    "  但 `docker inspect` 的 `.Config.Env` 仍可见它们，宿主机上查看容器属于敏感操作。",
    "- 残留：本段是单实例 Postgres（无流复制/PITR），备份恢复为同实例库到库还原；多可用区部署",
    "  仍需自行验证主从复制与时间点恢复。",
]


def write_report(steps: list[Step], only: str) -> None:
    total = len(steps)
    passed = sum(1 for item in steps if item.ok)
    verdict = "通过" if total and passed == total else "未通过"
    ran = {item.section for item in steps}
    topology = []
    if SECTION_TLS in ran:
        topology.append(
            f"- TLS 拓扑：nginx 容器（{PROXY_IMAGE}）终结 TLS，网关容器"
            f"（{GATEWAY_IMAGE}）同网段互通且不发布宿主端口"
        )
    if SECTION_SENTINEL in ran:
        topology.append(
            f"- Sentinel 拓扑：1 主 2 从 + 3 个 Sentinel（{REDIS_IMAGE}）+ 2 个网关进程"
            " + 3 个压测主体，全在同一隔离 bridge 网络内用真实内网地址互访"
        )
    if SECTION_POSTGRES in ran:
        topology.append(
            f"- Postgres 拓扑：{PG_IMAGE} 单实例 + 1 个 Redis + 假上游 + 3 个网关进程"
            f"（A/B 共享 `{PG_DB}`，C 指向恢复库 `{PG_BACKUP_DB}`），请求主体在容器内发真实 HTTP"
        )
    notes = []
    if SECTION_TLS in ran:
        notes.extend(TLS_NOTES)
    if SECTION_SENTINEL in ran:
        notes.extend(SENTINEL_NOTES)
    if SECTION_POSTGRES in ran:
        notes.extend(POSTGRES_NOTES)
    lines = [
        "# M7 部署演练报告（真实 TLS/mTLS、Sentinel、Postgres）",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 运行环境：{platform.platform()} / Python {platform.python_version()}",
        *topology,
        f"- 结论：**M7 {verdict}**（{passed}/{total} 步通过；本次范围：{only or '全部'}）",
        "",
        "## 步骤",
        "",
        "| 段落 | 步骤 | 结果 | 耗时 | 说明 |",
        "|---|---|---|---|---|",
    ]
    for item in steps:
        detail = item.detail.replace("|", "\\|")
        lines.append(
            f"| {item.section or '-'} | {item.name} | {'通过' if item.ok else '失败'} "
            f"| {item.seconds:.1f}s | {detail} |"
        )
    lines.extend(["", "## 说明", "", *notes, ""])
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="M7 部署演练")
    parser.add_argument("--only", choices=("tls", "sentinel", "postgres"), default="")
    parser.add_argument("--keep", action="store_true", help="保留容器与证书便于排查")
    args = parser.parse_args()

    if shutil.which("docker") is None:
        log("M7 演练需要 Docker（nginx/redis/postgres 容器）")
        return 2

    steps: list[Step] = []
    sections = [args.only] if args.only else list(RUNNERS)
    try:
        for name in sections:
            RUNNERS[name](steps)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        steps.append(Step("M7 演练中断", False, 0.0, f"{type(exc).__name__}: {exc}", args.only or "全部"))
    finally:
        cleanup(args.keep)
        if not args.keep:
            shutil.rmtree(WORK_DIR, ignore_errors=True)

    write_report(steps, args.only)
    passed = sum(1 for item in steps if item.ok)
    log(f"\nM7：{passed}/{len(steps)} 步通过，报告 {REPORT_PATH}")
    return 0 if passed == len(steps) and steps else 1


if __name__ == "__main__":
    sys.exit(main())
