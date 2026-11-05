#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网关压测：50 并发非流式 + 流式，统计 p50/p95/p99、错误率与账本一致性。

自起三个进程：假上游（固定 20ms 延迟，用于算网关净开销）、网关（临时数据目录）、
不用控制面（策略走本地文件，避免把上游延迟算进控制面）。

用法: python scripts/loadtest.py [--concurrency 50] [--requests 250]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import statistics
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS_DIR = Path(__file__).resolve().parent
GATEWAY_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

import g3_acceptance as g3  # noqa: E402 - 复用进程与 HTTP 工具

from tests.fake_upstreams import FakeUpstream  # noqa: E402

ADMIN_TOKEN = "loadtest-admin-token"
HS256_SECRET = "loadtest-hs256-secret"
MASTER_KEY = g3.MASTER_KEY
TENANT = "acme"
DEPT = "dept-default"
UPSTREAM_DELAY_MS = 20.0

check = g3.check
free_port = g3.free_port
wait_ready = g3.wait_ready
http = g3.http


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_token(ttl: int = 3600) -> str:
    now = int(time.time())
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = b64url(
        json.dumps(
            {
                "sub": "load-user",
                "sid": "load-session",
                "tenant": TENANT,
                "dept": DEPT,
                "depts": [DEPT],
                "roles": ["member"],
                "type": "access",
                "iss": "tongtian-auth",
                "aud": ["tongtian-gateway"],
                "iat": now,
                "exp": now + ttl,
            }
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode()
    signature = hmac.new(HS256_SECRET.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{claims}.{b64url(signature)}"


def policy_document(gateway_base: str) -> dict:
    return {
        "contractVersion": "gateway-contract.v1",
        "policyVersion": "load#1",
        "version": "load#1",
        "tenant": TENANT,
        "gateway": {"url": gateway_base},
        "noLog": True,
        "embeddingRoute": "advanced",
        "byok": {"allowed": False, "hardQuota": True},
        "models": {
            "fake-chat": {
                "provider": "openai_compatible",
                "upstreamModel": "fake-openai-model",
                "capabilities": ["chat", "tools", "vision"],
                "contextWindow": 32768,
                "maxOutputTokens": 4096,
                "priceVersion": "load-2026-10",
                "pricing": {
                    "currency": "USD",
                    "inputPer1M": "1.00",
                    "outputPer1M": "2.00",
                },
            }
        },
        "routes": {
            "advanced": {"chain": ["fake-chat"], "fallbackAllowed": False},
        },
        "allowedModels": ["advanced", "fake-chat"],
        "quotaDefaults": {
            "monthlyTokens": 10**9,
            "concurrency": 256,
            "maxOutputTokens": 4096,
        },
        "departments": {
            DEPT: {
                "allowedModels": ["advanced", "fake-chat"],
                "quota": {
                    "monthlyTokens": 10**9,
                    "concurrency": 256,
                    "maxOutputTokens": 4096,
                },
            }
        },
        "issuedAt": "2026-10-30T00:00:00Z",
        "expiresAt": "2027-10-30T00:00:00Z",
    }


def start_gateway(port: int, data_dir: Path, log_path: Path, policy_path: Path) -> g3.Process:
    env = g3._clean_env(
        {
            "TT_GATEWAY_ENV": "development",
            "TT_GATEWAY_JWT_MODE": "hs256-dev",
            "TT_GATEWAY_DEV_HS256_SECRET": HS256_SECRET,
            "TT_GATEWAY_ADMIN_TOKEN": ADMIN_TOKEN,
            "TT_GATEWAY_ID": "gw-load-01",
            "TT_GATEWAY_DATA_DIR": str(data_dir),
            "TT_GATEWAY_POLICY_FILE": str(policy_path),
            "TT_GATEWAY_MASTER_KEY": MASTER_KEY,
            "TT_GATEWAY_ALLOW_PRIVATE_PROVIDERS": "1",
            "TT_GATEWAY_UPSTREAM_RETRIES": "0",
            "TT_GATEWAY_UPSTREAM_TIMEOUT_SECONDS": "60",
            "TT_GATEWAY_STREAM_IDLE_TIMEOUT_SECONDS": "60",
        }
    )
    process = g3.Process(
        [
            g3.venv_python(GATEWAY_ROOT),
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
    wait_ready(f"http://127.0.0.1:{port}", "/health", "网关")
    return process


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


def _admin(base: str) -> dict[str, str]:
    return {"X-TT-Admin-Token": ADMIN_TOKEN}


def configure_provider(base: str, upstream: FakeUpstream) -> None:
    status, _, body = http(
        base,
        "POST",
        "/gateway/v1/admin/providers",
        headers=_admin(base),
        body={
            "id": "openai_compatible",
            "label": "loadtest",
            "baseUrl": f"{upstream.base_url}/v1",
            "timeoutSeconds": 60,
            "allowPrivate": True,
        },
    )
    if status not in (200, 201):
        raise RuntimeError(f"创建 provider 失败：{status} {body}")
    status, _, body = http(
        base,
        "POST",
        "/gateway/v1/admin/providers/openai_compatible/keys",
        headers=_admin(base),
        body={"label": "load", "secret": "sk-loadtest-provider"},
    )
    if status != 201:
        raise RuntimeError(f"写入 provider 密钥失败：{status} {body}")


def one_chat(base: str, token: str, stream: bool) -> tuple[float, int, str]:
    payload = json.dumps(
        {
            "model": "advanced",
            "messages": [{"role": "user", "content": "压测"}],
            "max_tokens": 32,
            "stream": stream,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        base + "/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8", "replace")
            elapsed = (time.perf_counter() - started) * 1000
            if stream and "[DONE]" not in body:
                return elapsed, response.status, "流式响应缺 [DONE]"
            return elapsed, response.status, ""
    except urllib.error.HTTPError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        detail = exc.read().decode("utf-8", "replace")[:120]
        return elapsed, exc.code, detail
    except Exception as exc:  # noqa: BLE001
        elapsed = (time.perf_counter() - started) * 1000
        return elapsed, 0, str(exc)[:120]


def run_phase(
    label: str,
    base: str,
    token: str,
    *,
    concurrency: int,
    requests: int,
    stream: bool,
) -> tuple[list[float], list[tuple[int, str]]]:
    latencies: list[float] = []
    failures: list[tuple[int, str]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [
            pool.submit(one_chat, base, token, stream) for _ in range(requests)
        ]
        for future in as_completed(futures):
            elapsed, status, detail = future.result()
            latencies.append(elapsed)
            if status != 200 or detail:
                failures.append((status, detail))
    overhead = [value - UPSTREAM_DELAY_MS for value in latencies]
    print(
        f"  {label}: n={len(latencies)} 错误={len(failures)} "
        f"p50={percentile(latencies, 0.50):.0f}ms "
        f"p95={percentile(latencies, 0.95):.0f}ms "
        f"p99={percentile(latencies, 0.99):.0f}ms "
        f"净开销p95={percentile(overhead, 0.95):.0f}ms "
        f"均值={statistics.fmean(latencies):.0f}ms"
    )
    return latencies, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--requests", type=int, default=250)
    args = parser.parse_args()
    if args.concurrency < 1 or args.requests < 1:
        parser.error("并发与请求数必须为正整数")

    temp_dir = Path(tempfile.mkdtemp(prefix="tt-gateway-loadtest-"))
    policy_path = temp_dir / "policy.json"
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    policy_path.write_text(
        json.dumps(policy_document(base), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    upstream = FakeUpstream(
        "openai", delay_seconds=UPSTREAM_DELAY_MS / 1000.0
    ).start()
    gateway = start_gateway(port, temp_dir / "data", temp_dir / "gateway.log", policy_path)
    token = make_token()
    try:
        configure_provider(base, upstream)
        print(
            f"\n[非流式] 并发 {args.concurrency}，共 {args.requests} 次，"
            f"上游固定延迟 {UPSTREAM_DELAY_MS:.0f}ms"
        )
        non_stream, non_failures = run_phase(
            "非流式",
            base,
            token,
            concurrency=args.concurrency,
            requests=args.requests,
            stream=False,
        )
        stream_requests = max(10, args.concurrency)
        print(f"\n[流式] 并发 {args.concurrency}，共 {stream_requests} 次")
        streaming, stream_failures = run_phase(
            "流式",
            base,
            token,
            concurrency=args.concurrency,
            requests=stream_requests,
            stream=True,
        )

        total_calls = args.requests + stream_requests
        status, _, usage = http(
            base,
            "GET",
            f"/gateway/v1/admin/usage?tenant={TENANT}&period=total",
            headers=_admin(base),
        )
        rows = usage.get("rows") or []
        calls = sum(int(row["calls"]) for row in rows)
        failures = non_failures + stream_failures
        log = gateway.text()

        check("非流式零错误", not non_failures, non_failures[:3])
        check("流式零错误", not stream_failures, stream_failures[:3])
        check(
            f"非流式 p95 < 1500ms（实测 {percentile(non_stream, 0.95):.0f}ms）",
            percentile(non_stream, 0.95) < 1500,
        )
        check(
            f"流式 p95 < 2500ms（实测 {percentile(streaming, 0.95):.0f}ms）",
            percentile(streaming, 0.95) < 2500,
        )
        check(
            f"账本调用次数与成功请求一致（{calls}/{total_calls}）",
            status == 200 and calls == total_calls,
            rows[:3],
        )
        check(
            f"上游收到同样多的对话请求（{len(upstream.records)}）",
            len(upstream.records) == total_calls,
        )
        check("日志没有 SQLite 锁冲突", "database is locked" not in log)
        check("日志没有未处理异常", "Traceback" not in log)
        health_status, _, health = http(base, "GET", "/health")
        check("压测后健康检查正常", health_status == 200 and health.get("ok") is True)
        return g3.FAILURES and 1 or 0
    finally:
        gateway.stop()
        upstream.stop()


if __name__ == "__main__":
    raise SystemExit(main())
