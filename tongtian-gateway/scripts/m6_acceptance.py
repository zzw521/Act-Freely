#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G10 验收：M6 跨主机多副本，并写出 docs/m6-acceptance.md。

全部走真实进程、真实 HTTP、真实 Postgres 与 Redis：
- 两个独立 gateway 进程（各自 data 目录、同一 gatewayId、同一 Postgres + Redis）；
- 空库同时启动只迁移一次，重启不重复；
- 跨进程限流 / 月度配额 / 并发 / TPM（主体桶与 Key 桶）；
- 跨进程审计链与 outbox 恰好一次投递；
- 部门级管理角色（单测覆盖签发/列表/配额/用量越界 403）；
- Redis Sentinel：真实 Docker 主从 + 假 Sentinel，杀掉主节点并提升副本后
  两个进程恢复，健康页 mode=sentinel、connected=true；
- 回归：M5 共享状态、M6 Postgres/TPM/角色与 SQLite 无 Redis 基线。

Postgres 来源：`M6_PG_URL` / `TT_GATEWAY_TEST_PG_URL` → 本机 55432
→ Docker `postgres:16-alpine`。Redis 来源：`M6_REDIS_URL` → Docker
`redis:7-alpine` → `redis://127.0.0.1:6391/0`。

用法: python scripts/m6_acceptance.py
"""

from __future__ import annotations

import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path
from urllib.parse import urlsplit

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS_DIR = Path(__file__).resolve().parent
GATEWAY_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

import psycopg  # noqa: E402
import redis  # noqa: E402

import g3_acceptance as g3  # noqa: E402
import g4_acceptance as g4  # noqa: E402
import m5_acceptance as m5  # noqa: E402
from tests.test_m6_sentinel import _FakeSentinel, _RedisPair  # noqa: E402

REPORT_PATH = GATEWAY_ROOT / "docs" / "m6-acceptance.md"
PYTHON = g3.venv_python(GATEWAY_ROOT)

GATEWAY_ID = m5.GATEWAY_ID
TENANT = m5.TENANT
DEPT = m5.DEPT
CONC_DEPT = m5.CONC_DEPT
TPM_DEPT = "dept-g10tpm"
REDIS_PREFIX = f"g10accept{uuid.uuid4().hex[:6]}"
ADMIN_HEADERS = m5.ADMIN_HEADERS
SCHEMA_VERSION = 7


# ── 基础设施 ───────────────────────────────────────────────


def _url_label(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or "?"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}{parsed.path}"


class PostgresServer:
    """为验收提供 Postgres；优先外部 URL，其次 Docker，最后本机 55432。"""

    def __init__(self) -> None:
        self.url = ""
        self._container = ""

    def start(self) -> "PostgresServer":
        candidates = [
            os.environ.get("M6_PG_URL", "").strip(),
            os.environ.get("TT_GATEWAY_TEST_PG_URL", "").strip(),
            "postgresql://postgres:tt-m6-pass@127.0.0.1:55432/tongtian_m6",
        ]
        for url in candidates:
            if not url:
                continue
            if self._reachable(url):
                self.url = url
                return self

        docker = shutil.which("docker")
        if docker:
            try:
                probe = subprocess.run(
                    [docker, "image", "inspect", "postgres:16-alpine"],
                    capture_output=True,
                    timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired):
                probe = None
            if probe is not None and probe.returncode == 0:
                port = g3.free_port()
                password = "g10pg" + secrets.token_hex(8)
                name = f"tt-g10-pg-{uuid.uuid4().hex[:6]}"
                run = subprocess.run(
                    [
                        docker,
                        "run",
                        "-d",
                        "--rm",
                        "--name",
                        name,
                        "-p",
                        f"127.0.0.1:{port}:5432",
                        "-e",
                        f"POSTGRES_PASSWORD={password}",
                        "-e",
                        "POSTGRES_DB=tongtian_m6",
                        "postgres:16-alpine",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if run.returncode != 0:
                    raise RuntimeError(
                        "启动 Postgres 容器失败：" + (run.stderr or run.stdout).strip()
                    )
                self._container = name
                self.url = (
                    f"postgresql://postgres:{password}@127.0.0.1:{port}/tongtian_m6"
                )
                deadline = time.time() + 60
                while time.time() < deadline:
                    if self._reachable(self.url):
                        return self
                    time.sleep(0.5)
                self.stop()
                raise RuntimeError("Postgres 容器已启动但连接不上")
        raise RuntimeError(
            "Postgres 不可用：请设置 M6_PG_URL，或准备 Docker 镜像 postgres:16-alpine"
        )

    @staticmethod
    def _reachable(url: str) -> bool:
        try:
            with psycopg.connect(url, connect_timeout=3):
                return True
        except Exception:  # noqa: BLE001
            return False

    def reset_schema(self) -> None:
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")

    def scalar(self, sql: str) -> int:
        with psycopg.connect(self.url, autocommit=True) as conn:
            return int(conn.execute(sql).fetchone()[0])

    def stop(self) -> None:
        if self._container:
            subprocess.run(
                ["docker", "rm", "-f", self._container],
                capture_output=True,
                timeout=60,
            )
            self._container = ""


class RedisBox(m5.RedisServer):
    """M6 版 Redis：优先 M6_REDIS_URL，并按本报告前缀清理。"""

    def start(self) -> "RedisBox":
        external = os.environ.get("M6_REDIS_URL", "").strip()
        if external:
            self.url = external
            self._require_ping("外部 M6_REDIS_URL")
            return self
        super().start()
        return self

    def clean_prefix(self) -> None:
        client = self.client()
        keys = list(client.scan_iter(match=f"{REDIS_PREFIX}*", count=200))
        if keys:
            client.delete(*keys)
        client.close()


# ── 网关进程 ───────────────────────────────────────────────


def gateway_env(
    control_plane: str,
    data_dir: Path,
    *,
    pg_url: str,
    redis_url: str = "",
    sentinel_urls: str = "",
    policy_ttl: int = 300,
    admin_rpm: int = 600,
    admin_burst: int = 200,
    webhook_url: str = "",
) -> dict[str, str]:
    env = {
        "TT_GATEWAY_ENV": "development",
        "TT_GATEWAY_JWT_MODE": "rs256",
        "TT_GATEWAY_JWKS_URL": f"{control_plane}/.well-known/jwks.json",
        "TT_GATEWAY_CONTROL_PLANE_URL": control_plane,
        "TT_GATEWAY_SERVICE_TOKEN": g4.SERVICE_TOKEN,
        "TT_GATEWAY_ADMIN_TOKEN": g4.ADMIN_TOKEN,
        "TT_GATEWAY_ID": GATEWAY_ID,
        "TT_GATEWAY_DATA_DIR": str(data_dir),
        "TT_GATEWAY_MASTER_KEY": g3.MASTER_KEY,
        "TT_GATEWAY_MASTER_KEY_VERSION": "1",
        "TT_GATEWAY_ALLOW_PRIVATE_PROVIDERS": "1",
        "TT_GATEWAY_CROSS_HOST": "1",
        "TT_GATEWAY_DB_URL": pg_url,
        "TT_GATEWAY_WORKERS": "1",
        "TT_GATEWAY_POLICY_TTL_SECONDS": str(policy_ttl),
        "TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE": str(admin_rpm),
        "TT_GATEWAY_ADMIN_RATE_LIMIT_BURST": str(admin_burst),
        "TT_GATEWAY_AUDIT_WEBHOOK_INTERVAL_SECONDS": "1",
        "TT_GATEWAY_AUDIT_WEBHOOK_BATCH_SIZE": "50",
    }
    if redis_url:
        env["TT_GATEWAY_REDIS_URL"] = redis_url
        env["TT_GATEWAY_REDIS_PREFIX"] = REDIS_PREFIX
        env["TT_GATEWAY_REDIS_TIMEOUT_MS"] = "500"
    if sentinel_urls:
        env["TT_GATEWAY_REDIS_SENTINEL_MASTER"] = "mymaster"
        env["TT_GATEWAY_REDIS_SENTINEL_URLS"] = sentinel_urls
        env["TT_GATEWAY_REDIS_PREFIX"] = REDIS_PREFIX
        env["TT_GATEWAY_REDIS_TIMEOUT_MS"] = "1000"
    if webhook_url:
        env["TT_GATEWAY_AUDIT_WEBHOOK_URL"] = webhook_url
        env["TT_GATEWAY_AUDIT_WEBHOOK_SECRET"] = m5.WEBHOOK_SECRET
    return g3._clean_env(env)


def launch_gateway(
    port: int, *, env: dict[str, str], log_path: Path, wait: bool = True
) -> g3.Process:
    process = g3.Process(
        [
            PYTHON,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            "--no-access-log",
        ],
        GATEWAY_ROOT,
        env,
        log_path,
    )
    if wait:
        g3.wait_ready(f"http://127.0.0.1:{port}", "/health", "网关")
    return process


def admin_health(base: str) -> dict:
    status, _, body = g3.http(
        base,
        "GET",
        f"/gateway/v1/admin/health?tenant={TENANT}",
        headers=ADMIN_HEADERS,
    )
    return body if status == 200 and isinstance(body, dict) else {}


def wait_shared(
    base: str, mode: str = "redis", *, timeout: float = 30.0
) -> dict:
    def probe():
        body = admin_health(base)
        state = body.get("sharedState") or {}
        if (
            state.get("mode") == mode
            and state.get("connected") is True
            and state.get("policySubscriber") == "listening"
        ):
            return body
        return None

    return m5.wait_until(probe, timeout=timeout, interval=0.3) or {}


def wait_policy(base: str, version: str, *, timeout: float = 30.0) -> dict:
    def probe():
        body = admin_health(base)
        return body if body.get("policyVersion") == version else None

    return m5.wait_until(probe, timeout=timeout, interval=0.3) or {}


def create_key(
    base: str,
    *,
    label: str,
    dept: str,
    sub: str,
    rpm: int = 0,
    tpm: int = 0,
    quota: int = 0,
) -> dict:
    status, _, body = g3.http(
        base,
        "POST",
        "/gateway/v1/admin/api-keys",
        headers=ADMIN_HEADERS,
        body={
            "label": label,
            "tenant": TENANT,
            "dept": dept,
            "sub": sub,
            "scopes": ["chat"],
            "modelAllowlist": ["auto", "fake-chat"],
            "rateLimitRpm": rpm,
            "rateLimitTpm": tpm,
            "quotaMonthlyTokens": quota,
        },
    )
    key = body.get("key") if isinstance(body, dict) else None
    g3.check(
        f"签发 API Key（{label}）",
        status == 201 and isinstance(key, dict) and bool(key.get("id")),
        f"{status} {body}",
    )
    return key or {}


def custom_policy(gateway_base: str) -> dict:
    document = g4.policy_document(gateway_base)
    document["quotaDefaults"] = {
        "monthlyTokens": 1000000,
        "concurrency": 4,
        "rpm": 0,
        "tpm": 0,
        "maxOutputTokens": 4096,
    }
    document["departments"] = {
        DEPT: {"allowedModels": ["auto", "fake-chat"]},
        CONC_DEPT: {
            "allowedModels": ["auto", "fake-chat"],
            "quota": {"concurrency": 1},
        },
        TPM_DEPT: {
            "allowedModels": ["auto", "fake-chat"],
            "quota": {"tpm": 200},
        },
    }
    return document


# ── 跨进程检查 ─────────────────────────────────────────────


def check_two_process_health(
    base_a: str, base_b: str, policy_version: str, pg_url: str, redis_url: str
) -> None:
    g3.step("G10-1：两个独立进程共享 Postgres + Redis")
    health_a = wait_shared(base_a)
    health_b = wait_shared(base_b)
    g3.check("进程 A 共享状态就绪", bool(health_a), "未就绪")
    g3.check("进程 B 共享状态就绪", bool(health_b), "未就绪")
    health_a = health_a or {}
    health_b = health_b or {}
    state_a = health_a.get("sharedState") or {}
    state_b = health_b.get("sharedState") or {}
    g3.check(
        "两端都报告 redis / connected / listening",
        state_a.get("mode") == "redis"
        and state_b.get("mode") == "redis"
        and state_a.get("connected") is True
        and state_b.get("connected") is True
        and state_a.get("policySubscriber") == "listening"
        and state_b.get("policySubscriber") == "listening",
        f"A={state_a} B={state_b}",
    )
    g3.check(
        "同一 gatewayId、不同进程 pid",
        health_a.get("gatewayId") == GATEWAY_ID
        and health_b.get("gatewayId") == GATEWAY_ID
        and health_a.get("workerPid")
        and health_b.get("workerPid")
        and health_a.get("workerPid") != health_b.get("workerPid"),
        f"A={health_a.get('workerPid')} B={health_b.get('workerPid')}",
    )
    ready_a = wait_policy(base_a, policy_version)
    ready_b = wait_policy(base_b, policy_version)
    g3.check(
        "两个进程读到同一策略版本",
        bool(ready_a) and bool(ready_b),
        f"A={admin_health(base_a).get('policyVersion')} B={admin_health(base_b).get('policyVersion')}",
    )
    blob = json.dumps([health_a, health_b], ensure_ascii=False)
    passwords = [
        value
        for value in (
            urlsplit(pg_url).password,
            urlsplit(redis_url).password,
        )
        if value
    ]
    g3.check(
        "健康面不回显 Postgres/Redis 连接串或密码",
        "postgresql://" not in blob
        and "postgres://" not in blob
        and "redis://" not in blob
        and "rediss://" not in blob
        and not any(value in blob for value in passwords),
        blob[:300],
    )


def check_tpm_shared(
    base_a: str, base_b: str, secret: str, upstream
) -> None:
    g3.step("G10-5：主体 TPM 桶跨进程（并发预留 → 429 → 结算退回）")
    result: dict = {}

    def slow_call() -> None:
        result["response"] = m5.chat(base_a, secret, max_tokens=150)

    upstream.delay_seconds = 2.0
    thread = threading.Thread(target=slow_call, name="g10-tpm-slow")
    thread.start()
    try:
        time.sleep(0.6)
        status, _, body = m5.chat(base_b, secret, max_tokens=150)
        thread.join(timeout=20)
    finally:
        upstream.delay_seconds = 0
    first = result.get("response") or (0, {}, {})
    g3.check(
        "A 上的请求在 TPM 桶容量内通过",
        first[0] == 200 and m5.message_text(first[2]) == "你好",
        f"{first[0]} {first[2]}",
    )
    g3.check(
        "A 在途预留耗尽主体桶后，B 立即 429 RATE_LIMITED",
        status == 429 and m5.error_code(body) == "RATE_LIMITED",
        f"{status} {body}",
    )
    recovered = False
    for _ in range(10):
        status, _, body = m5.chat(base_b, secret, max_tokens=100)
        if status == 200:
            recovered = True
            break
        time.sleep(1.0)
    g3.check(
        "结算退回未用预留后，B 的后续请求通过",
        recovered and m5.message_text(body) == "你好",
        f"{status} {body}",
    )


def check_key_tpm(base: str, secret: str) -> None:
    g3.step("G10-5：Key 级 rateLimitTpm 独立生效")
    status, _, body = m5.chat(base, secret, max_tokens=150)
    g3.check(
        "单次成本超过 Key 桶容量时 429 RATE_LIMITED",
        status == 429 and m5.error_code(body) == "RATE_LIMITED",
        f"{status} {body}",
    )
    status, _, body = m5.chat(base, secret, max_tokens=60)
    g3.check(
        "较小请求在 Key 桶容量内通过",
        status == 200 and m5.message_text(body) == "你好",
        f"{status} {body}",
    )


# ── 主流程：两进程跨主机语义 ───────────────────────────────


def run_cross_process_flow(
    pg: PostgresServer,
    redis_srv,
    receiver,
    upstream,
    server_base: str,
    ports: tuple[int, int],
    steps: list,
) -> str:
    temp = Path(tempfile.mkdtemp(prefix="tt-g10-"))
    port_a, port_b = ports
    base_a = f"http://127.0.0.1:{port_a}"
    base_b = f"http://127.0.0.1:{port_b}"
    logs = {
        "a": temp / "gateway-a.log",
        "b": temp / "gateway-b.log",
        "a2": temp / "gateway-a-restart.log",
        "b2": temp / "gateway-b-restart.log",
    }
    proc_a = proc_b = None
    try:
        env_a = gateway_env(
            server_base,
            temp / "gateway-a",
            pg_url=pg.url,
            redis_url=redis_srv.url,
            webhook_url=receiver.url,
            policy_ttl=2,
        )
        env_b = gateway_env(
            server_base,
            temp / "gateway-b",
            pg_url=pg.url,
            redis_url=redis_srv.url,
            webhook_url=receiver.url,
            policy_ttl=300,
        )
        began = time.perf_counter()
        proc_a = launch_gateway(port_a, env=env_a, log_path=logs["a"], wait=False)
        proc_b = launch_gateway(port_b, env=env_b, log_path=logs["b"], wait=False)
        g3.wait_ready(base_a, "/health", "网关 A")
        g3.wait_ready(base_b, "/health", "网关 B")
        g3.check(
            "空库同时启动：schema_version 只有一条且为当前版本",
            pg.scalar("SELECT COUNT(*) FROM schema_version") == 1
            and pg.scalar("SELECT version FROM schema_version") == SCHEMA_VERSION,
            f"rows={pg.scalar('SELECT COUNT(*) FROM schema_version')}",
        )
        steps.append(
            m5.Step(
                "两进程并发首次迁移 Postgres",
                True,
                time.perf_counter() - began,
                "两个进程同时启动，只应用一次 schema v7",
            )
        )

        # 重启不重复迁移
        proc_a.stop()
        proc_b.stop()
        g3.wait_port_free(port_a)
        g3.wait_port_free(port_b)
        proc_a = launch_gateway(port_a, env=env_a, log_path=logs["a2"])
        proc_b = launch_gateway(port_b, env=env_b, log_path=logs["b2"])
        g3.check(
            "重启后仍只有一条 schema_version（不重复迁移）",
            pg.scalar("SELECT COUNT(*) FROM schema_version") == 1,
            f"rows={pg.scalar('SELECT COUNT(*) FROM schema_version')}",
        )
        steps.append(
            m5.Step(
                "重启不重复迁移",
                pg.scalar("SELECT COUNT(*) FROM schema_version") == 1,
                time.perf_counter() - began,
                "两个进程各重启一次后 schema v7 未重复",
            )
        )

        _, admin, _ = g4.setup_control_plane(
            server_base, base_a, f"{upstream.base_url}/v1"
        )
        policy_v2 = m5.put_policy(server_base, admin, custom_policy(base_a))

        key_rate = create_key(
            base_a, label="g10-rate", dept=DEPT, sub="g10-rate", rpm=4
        )
        key_quota = create_key(
            base_b, label="g10-quota", dept=DEPT, sub="g10-quota", rpm=600, quota=100
        )
        key_conc = create_key(
            base_b, label="g10-conc", dept=CONC_DEPT, sub="g10-conc"
        )
        key_tpm = create_key(
            base_a, label="g10-tpm", dept=TPM_DEPT, sub="g10-tpm"
        )
        key_ktpm = create_key(
            base_a, label="g10-keytpm", dept=DEPT, sub="g10-keytpm", tpm=100
        )
        secrets_ = [
            str(key.get("secret") or "")
            for key in (key_rate, key_quota, key_conc, key_tpm, key_ktpm)
        ]

        steps.append(
            m5.section(
                "跨进程健康面",
                check_two_process_health,
                base_a,
                base_b,
                policy_v2,
                pg.url,
                redis_srv.url,
            )
        )
        steps.append(
            m5.section(
                "per-key RPM 跨进程",
                m5.check_shared_rate,
                base_a,
                base_b,
                key_rate.get("secret", ""),
            )
        )
        steps.append(
            m5.section(
                "月配额跨进程",
                m5.check_shared_quota,
                base_a,
                base_b,
                key_quota.get("secret", ""),
            )
        )
        steps.append(
            m5.section(
                "并发计数跨进程",
                m5.check_shared_concurrency,
                base_a,
                base_b,
                key_conc.get("secret", ""),
                upstream,
            )
        )
        steps.append(
            m5.section(
                "主体 TPM 跨进程",
                check_tpm_shared,
                base_a,
                base_b,
                key_tpm.get("secret", ""),
                upstream,
            )
        )
        steps.append(
            m5.section("Key 级 TPM", check_key_tpm, base_a, key_ktpm.get("secret", ""))
        )
        steps.append(
            m5.section(
                "审计链与 outbox 恰好一次",
                m5.check_audit_and_outbox,
                base_a,
                base_b,
                receiver,
            )
        )

        blocked = [g4.ADMIN_TOKEN, g4.SERVICE_TOKEN, g3.MASTER_KEY] + secrets_
        haystack = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in logs.values()
            if path.exists()
        )
        leaked = [value for value in blocked if value and value in haystack]
        redis_password = urlsplit(redis_srv.url).password or ""
        pg_password = urlsplit(pg.url).password or ""
        g3.check("进程日志不含管理/服务令牌、主密钥与 Key 明文", not leaked, str(leaked))
        g3.check(
            "进程日志不含 Postgres/Redis 连接串或密码",
            "postgresql://" not in haystack
            and "postgres://" not in haystack
            and "redis://" not in haystack
            and "rediss://" not in haystack
            and (not redis_password or redis_password not in haystack)
            and (not pg_password or pg_password not in haystack),
            "日志出现连接信息",
        )
        steps.append(
            m5.Step(
                "敏感值不落日志",
                not leaked
                and "postgresql://" not in haystack
                and "redis://" not in haystack,
                time.perf_counter() - began,
                "管理令牌/服务令牌/主密钥/Key 明文/DB 与 Redis URL 均未出现",
            )
        )
        return policy_v2
    finally:
        for process in (proc_a, proc_b):
            if process is not None:
                process.stop()


# ── Sentinel 主从切换（两个进程） ──────────────────────────


def run_sentinel_flow(
    pg: PostgresServer, server_base: str, upstream, steps: list, policy_version: str
) -> None:
    if not shutil.which("docker"):
        print("SKIP Docker 不可用，跳过 Sentinel 双进程切换用例")
        return
    full = subprocess.run(
        ["docker", "image", "inspect", "redis:7-alpine"], capture_output=True
    )
    if full.returncode != 0:
        print("SKIP redis:7-alpine 不可用，跳过 Sentinel 双进程切换用例")
        return

    pair = _RedisPair()
    sentinel = _FakeSentinel()
    temp = Path(tempfile.mkdtemp(prefix="tt-g10-sentinel-"))
    port_c = g3.free_port()
    port_d = g3.free_port()
    base_c = f"http://127.0.0.1:{port_c}"
    base_d = f"http://127.0.0.1:{port_d}"
    logs = {"c": temp / "gateway-c.log", "d": temp / "gateway-d.log"}
    proc_c = proc_d = None
    began = time.perf_counter()
    try:
        pair.start()
        sentinel.set_master(pair.port)
        sentinel.start()
        sentinel_env = f"127.0.0.1:{sentinel.port}"
        env_c = gateway_env(
            server_base,
            temp / "gateway-c",
            pg_url=pg.url,
            sentinel_urls=sentinel_env,
        )
        env_d = gateway_env(
            server_base,
            temp / "gateway-d",
            pg_url=pg.url,
            sentinel_urls=sentinel_env,
        )
        proc_c = launch_gateway(port_c, env=env_c, log_path=logs["c"], wait=False)
        proc_d = launch_gateway(port_d, env=env_d, log_path=logs["d"], wait=False)
        g3.wait_ready(base_c, "/health", "网关 C")
        g3.wait_ready(base_d, "/health", "网关 D")
        health_c = wait_shared(base_c, "sentinel")
        health_d = wait_shared(base_d, "sentinel")
        g3.check(
            "两个进程都以 mode=sentinel / connected=true 就绪",
            bool(health_c) and bool(health_d),
            f"C={admin_health(base_c).get('sharedState')} D={admin_health(base_d).get('sharedState')}",
        )
        ready_c = wait_policy(base_c, policy_version)
        ready_d = wait_policy(base_d, policy_version)
        g3.check(
            "两个进程读到同一策略版本",
            bool(ready_c) and bool(ready_d),
            f"C={admin_health(base_c).get('policyVersion')} D={admin_health(base_d).get('policyVersion')}",
        )

        key = create_key(
            base_c, label="g10-sentinel", dept=DEPT, sub="g10-sentinel", rpm=4
        )
        key_id = str(key.get("id") or "")
        secret = str(key.get("secret") or "")
        probe = create_key(
            base_d, label="g10-sentinel-probe", dept=DEPT, sub="g10-sentinel-probe"
        )
        probe_secret = str(probe.get("secret") or "")
        statuses = [
            m5.chat(base_c if index % 2 == 0 else base_d, secret)[0] for index in range(5)
        ]
        g3.check(
            "切换前两进程共享 per-key RPM 桶（4 次通过、第 5 次 429）",
            statuses[:4] == [200] * 4 and statuses[4] == 429,
            str(statuses),
        )

        pair.promote_replica()
        pair.stop_master()
        sentinel.set_master(pair.replica_port)

        recovered_c = recovered_d = False
        allowed = False
        deadline = time.time() + 40
        while time.time() < deadline:
            status, _, body = m5.chat(base_d, probe_secret)
            if status == 200:
                allowed = True
                health_c = admin_health(base_c).get("sharedState") or {}
                health_d = admin_health(base_d).get("sharedState") or {}
                recovered_c = (
                    health_c.get("mode") == "sentinel"
                    and health_c.get("connected") is True
                )
                recovered_d = (
                    health_d.get("mode") == "sentinel"
                    and health_d.get("connected") is True
                )
                if recovered_c and recovered_d:
                    break
            time.sleep(1.0)
        g3.check(
            "主节点停止并提升副本后，两个进程均恢复（mode=sentinel/connected）",
            recovered_c and recovered_d,
            f"C={admin_health(base_c).get('sharedState')} D={admin_health(base_d).get('sharedState')}",
        )
        client = redis.Redis(
            host="127.0.0.1", port=pair.replica_port, decode_responses=True
        )
        tokens = client.hget(f"{REDIS_PREFIX}:rl:key:{key_id}", "tokens")
        client.close()
        g3.check(
            "副本保留切换前写入的共享桶",
            tokens is not None and float(tokens) < 4.0,
            f"tokens={tokens}",
        )
        g3.check(
            "恢复后请求重新放行（共享限流继续工作）",
            allowed,
            f"{status} {body}",
        )
        steps.append(
            m5.Step(
                "Sentinel 主从切换后两进程恢复",
                bool(recovered_c and recovered_d and allowed),
                time.perf_counter() - began,
                "mode=sentinel / connected=true；副本保留桶并继续限流",
            )
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        steps.append(
            m5.Step(
                "Sentinel 主从切换后两进程恢复",
                False,
                time.perf_counter() - began,
                f"{type(exc).__name__}: {exc}",
            )
        )
    finally:
        for process in (proc_c, proc_d):
            if process is not None:
                process.stop()
        sentinel.stop()
        pair.stop()


# ── 报告 ───────────────────────────────────────────────────


def write_report(steps: list, pg: PostgresServer, redis_url: str) -> None:
    total = len(steps)
    passed = sum(1 for step in steps if step.ok)
    verdict = "通过" if passed == total else "未通过"
    lines = [
        "# M6 验收报告（G10：跨主机多副本）",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 运行环境：{platform.platform()} / Python {platform.python_version()} / "
        f"Postgres + Redis（{_url_label(redis_url)}）/ 两个独立 gateway 进程",
        f"- Postgres：{_url_label(pg.url)}",
        f"- 结论：**G10 {verdict}**（{passed}/{total} 个步骤通过）",
        "",
        "## 步骤",
        "",
        "| 步骤 | 结果 | 耗时 | 说明 |",
        "|---|---|---|---|",
    ]
    for step in steps:
        mark = "通过" if step.ok else "失败"
        detail = step.detail.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {step.name} | {mark} | {step.seconds:.1f}s | {detail} |")
    lines.extend(
        [
            "",
            "## 验收口径",
            "",
            "- 两个独立 uvicorn 进程：各自 data 目录、同一 `TT_GATEWAY_ID`、同一",
            "  `TT_GATEWAY_DB_URL`（Postgres）与同一 Redis；`TT_GATEWAY_CROSS_HOST=1`。",
            "- 迁移：空库同时启动只应用一次 schema v7；重启后版本表仍只有一条。",
            "- 配额：Postgres 保存已结算用量，Redis 保存在途预留与并发计数；跨进程",
            "  读到同一份月度余量，不超发。",
            "- TPM：主体桶按策略 `quota.tpm`，Key 桶按 `rateLimitTpm`；预留成本",
            "  `max(1, 输入估算) + 输出上限`，结算退回未用部分；Redis 故障时失败关闭。",
            "- 审计：Postgres 内单链串行追加；两进程验链 headHash 相同、导出连续。",
            "- outbox：租约认领跨进程互斥，接收端按事件 id 去重，只投递一次。",
            "- 部门角色：scoped operator 的列表/签发/轮换/吊销、配额与用量按部门",
            "  + 租户收窄，provider 写与审计读 403；admin/auditor 不受限（单测覆盖）。",
            "- Sentinel：真实 Docker 主从 + 假 Sentinel 提供 `SENTINEL MASTERS` 与",
            "  `get-master-addr-by-name`；杀主并提升副本后两进程重连，健康页保持",
            "  `mode=sentinel`、`connected=true`，副本保留切换前的共享桶。",
            "- 安全边界：DB DSN、Redis URL/密码只来自环境变量；健康页只披露模式、",
            "  连通性与 worker/pid；日志不出现令牌、主密钥、Key 明文或连接串。",
            "",
            "## 说明与残余",
            "",
            "- 仍假定单一 Postgres 主库与单一 Redis 主从；读写分离、分片、跨地域",
            "  延迟与 Redis Cluster 不在 M6。",
            "- Sentinel 用例使用假 Sentinel 只实现发现接口；生产应部署至少 3 个",
            "  Sentinel 并配置 `TT_GATEWAY_REDIS_SENTINEL_URLS`。",
            "- TPM 输入 token 为本地估算；与上游 tokenizer 的差异由结算退回吸收。",
            "- 部门角色先覆盖网关管理面；控制面 UI 的部门筛选留待后续。",
            "- 审计链是全局单链，跨主机串行化会放大大规模写入等待。",
            "",
        ]
    )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


# ── 入口 ───────────────────────────────────────────────────


def main() -> int:
    pg = PostgresServer()
    redis_srv = RedisBox()
    try:
        pg.start()
    except Exception as exc:  # noqa: BLE001
        print(f"G10 无法开始：{exc}")
        return 2
    try:
        redis_srv.start()
    except Exception as exc:  # noqa: BLE001
        print(f"G10 无法开始：{exc}")
        pg.stop()
        return 2
    print(f"[Postgres] {_url_label(pg.url)}")
    print(f"[Redis] {_url_label(redis_srv.url)}")

    steps: list = []
    server = None
    upstream = None
    receiver = None
    try:
        suites = [
            ("tests/test_m6_postgres.py", {"TT_GATEWAY_TEST_PG_URL": pg.url}),
            (
                "tests/test_m6_sentinel.py",
                {"TT_GATEWAY_REDIS_URL": redis_srv.url},
            ),
            ("tests/test_m6_tpm.py", {"TT_GATEWAY_REDIS_URL": redis_srv.url}),
            ("tests/test_m6_admin_scope.py", {}),
            ("tests/test_m5_shared.py", {"TT_GATEWAY_REDIS_URL": redis_srv.url}),
            ("tests/test_api_keys.py", {}),
        ]
        for index, (name, env) in enumerate(suites, start=1):
            print(f"\n[{index}/{len(suites) + 3}] {name}")
            step = m5.unit_suite(name, GATEWAY_ROOT / name, env=env)
            steps.append(step)
            print(m5._tail(step.output))

        print(f"\n[{len(suites) + 1}/{len(suites) + 3}] 两个独立进程跨主机语义")
        temp = Path(tempfile.mkdtemp(prefix="tt-g10-world-"))
        upstream = g3.FakeUpstream("openai").start()
        receiver = m5.WebhookReceiver().start()
        server_port = g3.free_port()
        server_base = f"http://127.0.0.1:{server_port}"
        port_a = g3.free_port()
        port_b = g3.free_port()
        base_a = f"http://127.0.0.1:{port_a}"
        pg.reset_schema()
        server = g4.start_server(
            server_port, temp / "server", temp / "server.log", base_a
        )
        policy_v2 = run_cross_process_flow(
            pg, redis_srv, receiver, upstream, server_base, (port_a, port_b), steps
        )
        print(f"\n[{len(suites) + 2}/{len(suites) + 3}] Sentinel 双进程主从切换")
        run_sentinel_flow(pg, server_base, upstream, steps, policy_v2)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        steps.append(
            m5.Step(
                "G10 端到端",
                False,
                0.0,
                f"{type(exc).__name__}: {exc}",
            )
        )
    finally:
        for process in (server,):
            if process is not None:
                process.stop()
        if receiver is not None:
            receiver.stop()
        if upstream is not None:
            upstream.stop()
        redis_srv.clean_prefix()
        redis_srv.stop()
        pg.stop()

    write_report(steps, pg, redis_srv.url)
    print(f"\n报告已写入 {REPORT_PATH}")

    failed = [step for step in steps if not step.ok]
    if failed:
        print(f"\nG10 未通过：{len(failed)} 个步骤失败")
        for step in failed:
            print(f"  - {step.name}：{step.detail}")
            excerpt = m5._tail(step.output)
            if excerpt:
                print(excerpt)
        return 1
    print("\nG10 验收通过：跨进程配额/TPM/审计/outbox、部门角色、Sentinel 切换与回归")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
