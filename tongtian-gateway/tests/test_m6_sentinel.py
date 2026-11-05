"""M6.6 自测：Redis Sentinel 客户端、健康披露、主从切换与 TLS。

覆盖：
- 配置解析：Sentinel 主名/地址、TLS 证书文件、互斥校验；
- 未启用/单实例/Sentinel 三种 mode 与 connected 语义，状态不回显 URL/密码；
- 假 Sentinel + Docker 主从：主节点停止并提升副本后，客户端重新解析主地址并恢复；
- 副本保留主节点写入的共享令牌桶（数据随复制保留）；
- TLS：自签 CA 起一个仅 TLS 的 Redis，客户端带 CA 连通。

Docker 不可用时跳过容器相关用例（本地无 Redis 也可跑配置与状态部分）。

用法: python tests/test_m6_sentinel.py
"""

from __future__ import annotations

import os
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import check, report  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.shared_state import SharedState, SharedStateError  # noqa: E402

import redis  # noqa: E402


def _env(**values: str | None):
    """临时设置环境变量；yield 后恢复。"""
    saved = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    class _Scope:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    return _Scope()


def _expect_settings_error(name: str, **env: str | None) -> None:
    with _env(**env):
        try:
            load_settings()
        except RuntimeError:
            check(name, True)
        else:
            check(name, False, "未抛 RuntimeError")


def test_config() -> None:
    _expect_settings_error(
        "Sentinel 主名缺少地址被拒",
        TT_GATEWAY_REDIS_SENTINEL_MASTER="mymaster",
        TT_GATEWAY_REDIS_SENTINEL_URLS=None,
    )
    _expect_settings_error(
        "Sentinel 地址格式非法被拒",
        TT_GATEWAY_REDIS_SENTINEL_MASTER="mymaster",
        TT_GATEWAY_REDIS_SENTINEL_URLS="bad-address",
    )
    _expect_settings_error(
        "TLS 证书缺少私钥被拒",
        TT_GATEWAY_REDIS_TLS_CERT="/tmp/client.crt",
        TT_GATEWAY_REDIS_TLS_KEY=None,
    )
    _expect_settings_error(
        "TLS CA 文件不存在被拒",
        TT_GATEWAY_REDIS_TLS_CA="C:/definitely/missing/ca.crt",
    )
    _expect_settings_error(
        "TLS 开启但 URL 非 rediss 被拒",
        TT_GATEWAY_REDIS_URL="redis://127.0.0.1:6379/0",
        TT_GATEWAY_REDIS_TLS="1",
        TT_GATEWAY_REDIS_TLS_CA=None,
    )
    with _env(
        TT_GATEWAY_REDIS_URL="rediss://127.0.0.1:6379/0",
        TT_GATEWAY_REDIS_TLS=None,
        TT_GATEWAY_REDIS_TLS_CA=None,
        TT_GATEWAY_REDIS_SENTINEL_MASTER=None,
        TT_GATEWAY_REDIS_SENTINEL_URLS=None,
    ):
        settings = load_settings()
        check("rediss:// 自动视为启用 TLS", settings.redis_tls is True)
    with _env(
        TT_GATEWAY_REDIS_URL=None,
        TT_GATEWAY_REDIS_SENTINEL_MASTER="mymaster",
        TT_GATEWAY_REDIS_SENTINEL_URLS="127.0.0.1:26379,[::1]:26380",
        TT_GATEWAY_REDIS_SENTINEL_PASSWORD="sentinel-pass",
        TT_GATEWAY_REDIS_PASSWORD="data-pass",
        TT_GATEWAY_REDIS_DB="3",
        TT_GATEWAY_REDIS_TLS="0",
    ):
        settings = load_settings()
        check(
            "Sentinel 配置可解析",
            settings.redis_sentinel_master == "mymaster"
            and settings.redis_sentinel_urls
            == (("127.0.0.1", 26379), ("::1", 26380))
            and settings.redis_sentinel_password == "sentinel-pass"
            and settings.redis_password == "data-pass"
            and settings.redis_db == 3,
            str(settings.redis_sentinel_urls),
        )
        check("Sentinel 模式视为已配置共享状态", settings.shared_state_configured)


def test_status() -> None:
    memory = SharedState("")
    status = memory.status()
    check(
        "未配置时 mode=memory 且视为连通",
        status["mode"] == "memory" and status["connected"] is True,
    )
    check("未配置时不声明 TLS", status["tls"] is False)
    check("状态不含 URL/密码字段", "url" not in status and "password" not in status)

    single = SharedState("redis://127.0.0.1:1/0", timeout_ms=200)
    try:
        single.ping()
    except SharedStateError:
        check("单实例不可达时 ping 失败", True)
    else:
        check("单实例不可达时 ping 失败", False, "未抛错")
    status = single.status()
    check(
        "单实例模式健康披露",
        status["mode"] == "redis" and status["connected"] is False,
        str(status),
    )

    sentinel = SharedState(
        sentinel_master="mymaster",
        sentinel_urls=(("127.0.0.1", 1),),
        timeout_ms=200,
    )
    try:
        sentinel.ping()
    except SharedStateError:
        check("Sentinel 不可达时 ping 失败", True)
    else:
        check("Sentinel 不可达时 ping 失败", False, "未抛错")
    status = sentinel.status()
    check(
        "Sentinel 模式健康披露",
        status["mode"] == "sentinel" and status["connected"] is False,
        str(status),
    )


# ── 假 Sentinel（只实现 get-master-addr-by-name） ─────────────


class _FakeSentinel:
    def __init__(self) -> None:
        self.master = ("127.0.0.1", 1)
        self._lock = threading.Lock()
        self._server: socketserver.ThreadingTCPServer | None = None
        self.port = 0

    def set_master(self, port: int) -> None:
        with self._lock:
            self.master = ("127.0.0.1", port)

    def _current(self) -> tuple[str, int]:
        with self._lock:
            return self.master

    def start(self) -> "_FakeSentinel":
        outer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                while True:
                    args = self._read_command()
                    if args is None:
                        return
                    command = args[0].decode("utf-8", "replace").upper() if args else ""
                    if command == "PING":
                        self.wfile.write(b"+PONG\r\n")
                    elif command == "SENTINEL" and len(args) >= 2:
                        sub = args[1].decode("utf-8", "replace").lower()
                        if sub == "get-master-addr-by-name":
                            host, port = outer._current()
                            self.wfile.write(_array([host.encode(), str(port).encode()]))
                        elif sub == "masters":
                            host, port = outer._current()
                            self.wfile.write(_masters(host, port))
                        elif sub in ("replicas", "slaves", "sentinels"):
                            self.wfile.write(b"*0\r\n")
                        else:
                            self.wfile.write(b"+OK\r\n")
                    else:
                        self.wfile.write(b"+OK\r\n")
                    self.wfile.flush()

            def _read_command(self):
                line = self.rfile.readline()
                if not line:
                    return None
                if not line.startswith(b"*"):
                    return line.strip().split()
                try:
                    count = int(line[1:].strip())
                except ValueError:
                    return None
                args = []
                for _ in range(count):
                    header = self.rfile.readline()
                    if not header.startswith(b"$"):
                        return None
                    length = int(header[1:].strip())
                    data = self.rfile.read(length)
                    self.rfile.read(2)
                    args.append(data)
                return args

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def _array(items: list[bytes]) -> bytes:
    parts = [b"*" + str(len(items)).encode() + b"\r\n"]
    for item in items:
        parts.append(b"$" + str(len(item)).encode() + b"\r\n" + item + b"\r\n")
    return b"".join(parts)


def _masters(host: str, port: int) -> bytes:
    """SENTINEL MASTERS：一个扁平 field/value 数组的 master 列表。"""
    fields = {
        "name": "mymaster",
        "ip": host,
        "port": str(port),
        "flags": "master",
        "num-other-sentinels": "0",
        "quorum": "1",
    }
    flat = [b"*" + str(len(fields) * 2).encode() + b"\r\n"]
    for key, value in fields.items():
        for item in (key.encode(), value.encode()):
            flat.append(b"$" + str(len(item)).encode() + b"\r\n" + item + b"\r\n")
    return b"*1\r\n" + b"".join(flat)


# ── Docker Redis 主从 ────────────────────────────────────────


def _docker_available() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    try:
        result = subprocess.run(
            [docker, "image", "inspect", "redis:7-alpine"],
            capture_output=True,
            timeout=20,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _run_docker(*args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("docker 不可用")
    return subprocess.run(
        [docker, *args], capture_output=True, text=True, timeout=timeout
    )


class _RedisPair:
    def __init__(self) -> None:
        self.network = f"tt-m6-net-{uuid.uuid4().hex[:8]}"
        self.master = f"tt-m6-m-{uuid.uuid4().hex[:8]}"
        self.replica = f"tt-m6-r-{uuid.uuid4().hex[:8]}"
        self.port = _free_port()
        self.replica_port = _free_port()

    def start(self) -> "_RedisPair":
        result = _run_docker("network", "create", self.network)
        if result.returncode != 0:
            raise RuntimeError(f"创建网络失败：{result.stderr.strip()}")
        common = ["-d", "--rm", "--network", self.network]
        result = _run_docker(
            "run",
            *common,
            "--name",
            self.master,
            "-p",
            f"127.0.0.1:{self.port}:6379",
            "redis:7-alpine",
            "redis-server",
            "--save",
            "",
            "--appendonly",
            "no",
        )
        if result.returncode != 0:
            raise RuntimeError(f"启动主节点失败：{result.stderr.strip()}")
        result = _run_docker(
            "run",
            *common,
            "--name",
            self.replica,
            "-p",
            f"127.0.0.1:{self.replica_port}:6379",
            "redis:7-alpine",
            "redis-server",
            "--save",
            "",
            "--appendonly",
            "no",
            "--replicaof",
            self.master,
            "6379",
        )
        if result.returncode != 0:
            raise RuntimeError(f"启动副本失败：{result.stderr.strip()}")
        self._wait_ping(self.port)
        self._wait_ping(self.replica_port)
        deadline = time.time() + 30
        while time.time() < deadline:
            info = self._client(self.replica_port).info("replication")
            if info.get("master_link_status") == "up":
                return self
            time.sleep(0.2)
        raise RuntimeError("副本未能与主节点同步")

    def _client(self, port: int) -> redis.Redis:
        return redis.Redis(
            host="127.0.0.1",
            port=port,
            socket_connect_timeout=1,
            socket_timeout=1,
        )

    def _wait_ping(self, port: int) -> None:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if self._client(port).ping():
                    return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.2)
        raise RuntimeError(f"Redis {port} 未就绪")

    def promote_replica(self) -> None:
        self._client(self.replica_port).execute_command("REPLICAOF", "NO", "ONE")
        deadline = time.time() + 15
        while time.time() < deadline:
            info = self._client(self.replica_port).info("replication")
            if info.get("role") == "master":
                return
            time.sleep(0.2)
        raise RuntimeError("副本提升为主节点超时")

    def stop_master(self) -> None:
        _run_docker("stop", self.master, timeout=60)

    def stop(self) -> None:
        _run_docker("stop", self.master, self.replica, timeout=60)
        _run_docker("network", "rm", self.network, timeout=60)


def test_failover(prefix: str) -> None:
    if not _docker_available():
        print("SKIP Docker/redis:7-alpine 不可用，跳过主从切换用例")
        return
    pair = _RedisPair()
    sentinel = _FakeSentinel()
    state = None
    try:
        pair.start()
        sentinel.set_master(pair.port)
        sentinel.start()
        state = SharedState(
            sentinel_master="mymaster",
            sentinel_urls=(("127.0.0.1", sentinel.port),),
            prefix=prefix,
            timeout_ms=1000,
            workers=2,
        )
        state.ping()
        status = state.status()
        check(
            "Sentinel 模式连通",
            status["mode"] == "sentinel" and status["connected"] is True,
            str(status),
        )
        first = state.consume("key", "failover", per_minute=6, burst=2)
        second = state.consume("key", "failover", per_minute=6, burst=2)
        third = state.consume("key", "failover", per_minute=6, burst=2)
        check(
            "主节点桶可正常消费并耗尽",
            first == 0.0 and second == 0.0 and third > 0,
            f"{first} {second} {third}",
        )

        pair.promote_replica()
        pair.stop_master()
        sentinel.set_master(pair.replica_port)

        recovered = False
        deadline = time.time() + 20
        last_error = ""
        while time.time() < deadline:
            try:
                state.ping()
                recovered = True
                break
            except SharedStateError as exc:
                last_error = str(exc)
                time.sleep(0.3)
        check("主从切换后客户端恢复连接", recovered, last_error)
        if recovered:
            status = state.status()
            check(
                "切换后健康披露仍为 Sentinel 且连通",
                status["mode"] == "sentinel" and status["connected"] is True,
                str(status),
            )
            raw = redis.Redis(
                host="127.0.0.1", port=pair.replica_port, decode_responses=True
            ).hget(f"{prefix}:rl:key:failover", "tokens")
            check("副本保留了主节点的共享桶", raw is not None, str(raw))
            retry = state.consume("key", "failover", per_minute=6, burst=2)
            check("切换后限流继续生效", retry > 0, str(retry))
    finally:
        if state is not None:
            state.stop()
        sentinel.stop()
        pair.stop()


def _openssl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["openssl", *args], capture_output=True, text=True, timeout=60
    )


def test_tls(prefix: str) -> None:
    if not _docker_available():
        print("SKIP Docker/redis:7-alpine 不可用，跳过 TLS 连通性用例")
        return
    if not shutil.which("openssl"):
        print("SKIP openssl 不可用，跳过 TLS 连通性用例")
        return
    cert_dir = Path(tempfile.mkdtemp(prefix="tt-m6-tls-"))
    container = f"tt-m6-tls-{uuid.uuid4().hex[:8]}"
    port = _free_port()
    ca_key, ca_crt = cert_dir / "ca.key", cert_dir / "ca.crt"
    srv_key, srv_csr, srv_crt = (
        cert_dir / "server.key",
        cert_dir / "server.csr",
        cert_dir / "server.crt",
    )
    ext = cert_dir / "ext.cnf"
    ext.write_text(
        "subjectAltName=IP:127.0.0.1\n"
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n",
        encoding="utf-8",
    )
    try:
        for command in (
            (
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(ca_key),
                "-out",
                str(ca_crt),
                "-subj",
                "/CN=tt-test-ca",
                "-days",
                "1",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
            ),
            (
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(srv_key),
                "-out",
                str(srv_csr),
                "-subj",
                "/CN=localhost",
            ),
        ):
            result = _openssl(*command)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip())
        result = _openssl(
            "x509",
            "-req",
            "-in",
            str(srv_csr),
            "-CA",
            str(ca_crt),
            "-CAkey",
            str(ca_key),
            "-CAcreateserial",
            "-out",
            str(srv_crt),
            "-days",
            "1",
            "-extfile",
            str(ext),
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        for path in (ca_crt, srv_crt, srv_key):
            os.chmod(path, 0o644)

        result = _run_docker(
            "run",
            "-d",
            "--rm",
            "--name",
            container,
            "-p",
            f"127.0.0.1:{port}:6379",
            "-v",
            f"{cert_dir}:/tls:ro",
            "redis:7-alpine",
            "redis-server",
            "--port",
            "0",
            "--tls-port",
            "6379",
            "--tls-cert-file",
            "/tls/server.crt",
            "--tls-key-file",
            "/tls/server.key",
            "--tls-ca-cert-file",
            "/tls/ca.crt",
            "--tls-auth-clients",
            "no",
            "--save",
            "",
            "--appendonly",
            "no",
        )
        if result.returncode != 0:
            raise RuntimeError(f"启动 TLS Redis 失败：{result.stderr.strip()}")

        state = SharedState(
            f"rediss://127.0.0.1:{port}/0",
            prefix=prefix,
            timeout_ms=1000,
            tls=True,
            tls_ca=str(ca_crt),
        )
        try:
            deadline = time.time() + 30
            last_error = ""
            while time.time() < deadline:
                try:
                    state.ping()
                    break
                except SharedStateError as exc:
                    last_error = str(exc)
                    time.sleep(0.3)
            else:
                raise RuntimeError(f"TLS Redis 未就绪：{last_error}")
            status = state.status()
            check(
                "TLS 客户端连通并披露 tls=true",
                status["mode"] == "redis"
                and status["connected"] is True
                and status["tls"] is True,
                str(status),
            )
            check(
                "TLS 连接可正常消费令牌桶",
                state.consume("key", "tls", per_minute=60, burst=1) == 0.0,
            )
        finally:
            state.stop()
    finally:
        _run_docker("stop", container, timeout=60)
        shutil.rmtree(cert_dir, ignore_errors=True)


def main() -> int:
    prefix = f"m6test{uuid.uuid4().hex[:8]}"
    test_config()
    test_status()
    test_failover(prefix)
    test_tls(prefix)
    return report("M6.6 Sentinel/TLS 自测通过")


if __name__ == "__main__":
    raise SystemExit(main())
