#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G4 验收：用量与运营（M1-PLAN §7.3）。

四件事全部走真实进程与真实 HTTP：
1. 经网关完成一次对话后，控制面 10 秒内出现该条用量（model、tokens、latency、cost）；
2. 同一批用量上报 3 次总量不变；
3. 桌面经网关的调用在合并视图只算一次，BYOK 直连照常由客户端上报；
4. 部门月额度调到 1 万 token，超限后 429 QUOTA_TOKENS_EXCEEDED 且响应头带重置时间。

用法: python scripts/g4_acceptance.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import g3_acceptance as g3  # noqa: E402  复用进程启动、HTTP、断言等脚手架

check = g3.check
step = g3.step
http = g3.http
free_port = g3.free_port
wait_ready = g3.wait_ready
FAILURES = g3.FAILURES

GATEWAY_ROOT = g3.GATEWAY_ROOT
SERVER_ROOT = g3.SERVER_ROOT

SERVICE_TOKEN = "g4-gateway-service-token"
ADMIN_TOKEN = "g4-gateway-admin-token"
ADMIN_EMAIL = "g4-admin@test.local"
ADMIN_PASSWORD = "g4-admin-secret-123"
USER_EMAIL = "g4-user@test.local"
USER_PASSWORD = "g4-user-secret-123"
GATEWAY_ID = "gw-g4-01"
CONTRACT = "gateway-contract.v1"

TENANT = "acme"
DEPT = "dept-default"
MODEL = "fake-chat"
PRICE_VERSION = "g4-2026-10"
PROVIDER_SECRET = "sk-g4-fake-provider-secret"
MASTER_KEY = g3.MASTER_KEY

PROMPT_TOKENS = 12
COMPLETION_TOKENS = 8
TOKENS_PER_CALL = PROMPT_TOKENS + COMPLETION_TOKENS
# 策略价：输入 1.00 USD/1M、输出 2.00 USD/1M → 12×1 + 8×2 = 28 微元
COST_MICROS = 28

DEPT_MONTHLY_TOKENS = 10000
LONG_PROMPT_CHARS = 7000
REPORT_WINDOW_SECONDS = 10.0
IDEM_SUB = "g4-idempotent-user"


@dataclass
class World:
    server_base: str
    gateway_base: str
    admin: str
    access: str
    uid: str
    gateway_ticket: str


def start_server(port: int, data_dir: Path, log_path: Path, gateway_base: str) -> g3.Process:
    env = g3._clean_env(
        {
            "TT_SERVER_DATA_DIR": str(data_dir),
            "TT_SERVER_JWT_SECRET": "g4-control-plane-secret",
            "TT_SERVER_ENV": "development",
            "TT_ALLOW_REGISTRATION": "true",
            "TT_SEED_DEMO": "false",
            "TT_ADMIN_EMAIL": ADMIN_EMAIL,
            "TT_ADMIN_PASSWORD": ADMIN_PASSWORD,
            "TT_ADMIN_NICKNAME": "G4 管理员",
            "TT_GATEWAY_SERVICE_TOKEN": SERVICE_TOKEN,
            # 管理代理（T4.10）：网关地址与管理令牌只留在控制面进程环境里
            "TT_GATEWAY_ADMIN_URL": gateway_base,
            "TT_GATEWAY_ADMIN_TOKEN": ADMIN_TOKEN,
            "TT_GATEWAY_PROXY_TIMEOUT_SECONDS": "20",
        }
    )
    process = g3.Process(
        [
            g3.venv_python(SERVER_ROOT),
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
        SERVER_ROOT,
        env,
        log_path,
    )
    wait_ready(f"http://127.0.0.1:{port}", "/health", "控制面")
    return process


def start_gateway(port: int, *, control_plane: str, data_dir: Path, log_path: Path) -> g3.Process:
    env = g3._clean_env(
        {
            "TT_GATEWAY_ENV": "development",
            "TT_GATEWAY_JWT_MODE": "rs256",
            "TT_GATEWAY_JWKS_URL": f"{control_plane}/.well-known/jwks.json",
            "TT_GATEWAY_CONTROL_PLANE_URL": control_plane,
            "TT_GATEWAY_SERVICE_TOKEN": SERVICE_TOKEN,
            "TT_GATEWAY_ADMIN_TOKEN": ADMIN_TOKEN,
            "TT_GATEWAY_ID": GATEWAY_ID,
            "TT_GATEWAY_DATA_DIR": str(data_dir),
            "TT_GATEWAY_MASTER_KEY": MASTER_KEY,
            "TT_GATEWAY_MASTER_KEY_VERSION": "1",
            "TT_GATEWAY_ALLOW_PRIVATE_PROVIDERS": "1",
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


def policy_document(gateway_base: str) -> dict:
    """月额度先用宽值：G4-4 再把部门额度压到 1 万 token 验证超限。"""
    return {
        "contractVersion": CONTRACT,
        "tenant": TENANT,
        "gateway": {"url": gateway_base},
        "noLog": True,
        "embeddingRoute": "embedding-default",
        "byok": {"allowed": True, "hardQuota": False},
        "models": {
            MODEL: {
                "provider": "openai_compatible",
                "upstreamModel": "fake-openai-model",
                "capabilities": ["chat", "tools", "vision"],
                "contextWindow": 32768,
                "maxOutputTokens": 4096,
                "priceVersion": PRICE_VERSION,
                "pricing": {"currency": "USD", "inputPer1M": "1.00", "outputPer1M": "2.00"},
            },
            "fake-embed": {
                "provider": "openai_compatible",
                "upstreamModel": "fake-embedding",
                "capabilities": ["embeddings"],
                "priceVersion": PRICE_VERSION,
                "pricing": {"currency": "USD", "inputPer1M": "0.10", "outputPer1M": "0"},
            },
        },
        "routes": {
            "auto": {"chain": [MODEL], "fallbackAllowed": False},
            "embedding-default": {"chain": ["fake-embed"], "fallbackAllowed": False},
        },
        "allowedModels": ["auto", "embedding-default", MODEL, "fake-embed"],
        "quotaDefaults": {"monthlyTokens": 1000000, "concurrency": 4, "maxOutputTokens": 4096},
        "departments": {DEPT: {"allowedModels": ["auto", MODEL]}},
    }


def usage_event(request_id: str, user_id: str, *, prompt: int, completion: int,
                latency: int) -> dict:
    """CONTRACT §6.1 事件；用于「同一批上报三次」的幂等验证。"""
    finished_at = int(time.time() * 1000)
    return {
        "contractVersion": CONTRACT,
        "gatewayId": GATEWAY_ID,
        "requestId": request_id,
        "tenantId": TENANT,
        "departmentId": DEPT,
        "userId": user_id,
        "origin": "gateway",
        "protocol": "openai",
        "endpoint": "/v1/chat/completions",
        "modelAlias": "auto",
        "model": MODEL,
        "provider": "openai_compatible",
        "promptTokens": prompt,
        "completionTokens": completion,
        "cachedTokens": 0,
        "reasoningTokens": 0,
        "totalTokens": prompt + completion,
        "cost": "0",
        "currency": "USD",
        "priceVersion": PRICE_VERSION,
        "latencyMs": latency,
        "status": "ok",
        "stream": False,
        "estimated": False,
        "startedAt": finished_at - latency,
        "finishedAt": finished_at,
    }


def usage_batch(batch_id: str, events: list[dict]) -> dict:
    return {
        "contractVersion": CONTRACT,
        "gatewayId": GATEWAY_ID,
        "batchId": batch_id,
        "events": events,
    }


def post_batch(world: World, payload: dict) -> tuple[int, dict]:
    status, _, body = http(
        world.server_base,
        "POST",
        "/api/v1/gateway/usage",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
        body=payload,
    )
    return status, body if isinstance(body, dict) else {}


def usage_me(world: World, period: str = "total") -> dict:
    _, _, body = http(
        world.server_base,
        "GET",
        f"/api/v1/usage/me?period={period}",
        headers={"Authorization": f"Bearer {world.access}"},
    )
    return body if isinstance(body, dict) else {}


def usage_user(world: World, sub: str, period: str = "total") -> dict:
    _, _, body = http(
        world.server_base,
        "GET",
        f"/api/v1/usage/users/{sub}?period={period}",
        headers={"Authorization": f"Bearer {world.admin}"},
    )
    return body if isinstance(body, dict) else {}


def totals_of(view: dict) -> dict:
    return view.get("totals") or {}


def sources_of(view: dict) -> dict:
    return view.get("sources") or {}


def model_row(view: dict, name: str) -> dict:
    for item in view.get("models") or []:
        if item.get("modelName") == name:
            return item
    return {}


def today_utc() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def wait_for_view(world: World, done, *, window: float = REPORT_WINDOW_SECONDS) -> tuple[float, dict]:
    """轮询合并视图直到谓词成立，返回（等待秒数, 最后一次响应）。"""
    began = time.time()
    view: dict = {}
    while time.time() - began < window:
        view = usage_me(world)
        if done(view):
            return time.time() - began, view
        time.sleep(0.2)
    return time.time() - began, view


def setup_control_plane(server_base: str, gateway_base: str, upstream_url: str) -> tuple[str, str, str]:
    step("控制面：注册用户、分配组织、签发策略")
    status, _, body = http(
        server_base,
        "POST",
        "/api/v1/auth/register",
        body={"email": USER_EMAIL, "password": USER_PASSWORD, "nickname": "G4 User"},
    )
    payload = body if isinstance(body, dict) else {}
    uid = str((payload.get("profile") or {}).get("uid") or "")
    check("注册用户", status == 201 and bool(uid), f"{status} {body}")

    status, _, body = http(
        server_base,
        "POST",
        "/api/v1/auth/login",
        body={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    admin = (body or {}).get("accessToken", "") if isinstance(body, dict) else ""
    check("管理员登录", status == 200 and bool(admin), f"{status} {body}")

    status, _, body = http(
        server_base,
        "PATCH",
        f"/api/v1/admin/users/{uid}",
        headers={"Authorization": f"Bearer {admin}"},
        body={"org": TENANT},
    )
    check("把用户分配到组织 acme", status == 200, f"{status} {body}")

    status, _, body = http(
        server_base,
        "POST",
        "/api/v1/auth/login",
        body={"email": USER_EMAIL, "password": USER_PASSWORD},
    )
    access = (body or {}).get("accessToken", "") if isinstance(body, dict) else ""
    check("用户登录", status == 200 and bool(access), f"{status} {body}")

    status, _, body = http(
        server_base,
        "PUT",
        f"/api/v1/admin/gateway/policy/{TENANT}",
        headers={"Authorization": f"Bearer {admin}"},
        body=policy_document(gateway_base),
    )
    check("保存租户策略（月额度先用宽值）", status == 200, f"{status} {body}")

    step("网关：经控制面管理代理登记 provider 与密钥（T4.10）")
    admin_headers = {"Authorization": f"Bearer {admin}"}
    status, _, body = http(
        server_base,
        "POST",
        "/api/v1/admin/gateway/providers",
        headers=admin_headers,
        body={
            "id": "openai_compatible",
            "label": "g4-fake",
            "baseUrl": upstream_url,
            "timeoutSeconds": 30,
            "allowPrivate": True,
        },
    )
    check("管理代理创建 provider", status in (200, 201), f"{status} {body}")

    status, _, body = http(
        server_base,
        "POST",
        "/api/v1/admin/gateway/providers/openai_compatible/keys",
        headers=admin_headers,
        body={"label": "g4-key", "secret": PROVIDER_SECRET},
    )
    key = (body or {}).get("key") or {} if isinstance(body, dict) else {}
    check("管理代理写入 provider 密钥", status == 201 and bool(key.get("id")), f"{status} {body}")

    status, _, listed = http(
        server_base, "GET", "/api/v1/admin/gateway/providers", headers=admin_headers
    )
    listed_text = json.dumps(listed, ensure_ascii=False)
    check(
        "密钥只在创建响应里出现一次，列表只留末 4 位",
        key.get("secret") == PROVIDER_SECRET
        and str(key.get("last4", "")) == PROVIDER_SECRET[-4:]
        and PROVIDER_SECRET not in listed_text,
        f"create={key} list={listed_text[:300]}",
    )
    check(
        "管理令牌不进任何管理响应体",
        ADMIN_TOKEN not in listed_text + json.dumps(body, ensure_ascii=False),
        listed_text[:200],
    )
    return uid, admin, access


def g4_one_chat(world: World) -> None:
    step("G4-1：一次网关对话 → 10 秒内控制面出现用量")
    status, headers, body = http(
        world.gateway_base,
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {world.gateway_ticket}"},
        body={
            "model": "auto",
            "messages": [{"role": "user", "content": "你好"}],
            "max_tokens": 64,
        },
    )
    content = ""
    if isinstance(body, dict):
        choices = body.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""
    check("经网关对话成功", status == 200 and content == "你好", f"{status} {body}")
    check("响应带 X-TT-Request-Id", bool(headers.get("x-tt-request-id")), str(headers))
    check("响应带 X-TT-Quota-Reset", bool(headers.get("x-tt-quota-reset")), str(headers))

    started = time.time()
    elapsed, view = wait_for_view(
        world, lambda item: sources_of(item).get("gatewayRows", 0) >= 1
    )
    check(
        f"用量在 {REPORT_WINDOW_SECONDS:.0f} 秒内进入控制面（对话结束后实测 "
        f"{time.time() - started:.1f}s）",
        elapsed < REPORT_WINDOW_SECONDS and sources_of(view).get("gatewayRows", 0) >= 1,
        f"{elapsed:.1f}s {sources_of(view)}",
    )

    model = model_row(view, MODEL)
    check(
        "用量带 model 与 tokens（12+8）",
        model.get("promptTokens") == PROMPT_TOKENS
        and model.get("completionTokens") == COMPLETION_TOKENS
        and model.get("tokensUsed") == TOKENS_PER_CALL,
        str(model),
    )
    check(
        "用量带 latency（1 个样本）",
        int(model.get("latencySamples") or 0) == 1 and model.get("avgLatencyMs") is not None,
        str(model),
    )
    check(
        "用量带 cost（按策略价 28 微元 USD）",
        model.get("costs") == [{"currency": "USD", "micros": COST_MICROS}],
        str(model.get("costs")),
    )
    check(
        "计入的是网关行而非客户端副本",
        int(totals_of(view).get("calls") or 0) == 1,
        str(totals_of(view)),
    )


def g4_replay_idempotency(world: World) -> None:
    step("G4-2：同一批用量上报 3 次总量不变")
    payload = usage_batch(
        "batch_g4_replay",
        [usage_event("g4-replay-req", IDEM_SUB, prompt=10, completion=5, latency=120)],
    )
    before = totals_of(usage_user(world, IDEM_SUB))
    results = []
    for round_no in range(1, 4):
        status, body = post_batch(world, payload)
        results.append(body)
        print(f"    第 {round_no} 次上报 → {status} {body}")

    check(
        "三次上报都成功且只第一次计入",
        all(item.get("ok") for item in results)
        and results[0].get("accepted") == 1
        and results[1].get("accepted") == 0
        and results[2].get("accepted") == 0
        and results[1].get("duplicates") == 1
        and results[2].get("duplicates") == 1,
        str(results),
    )
    after = totals_of(usage_user(world, IDEM_SUB))
    check(
        "总量不随重放增长（15 token / 1 次调用）",
        int(before.get("tokensUsed") or 0) == 0
        and int(after.get("tokensUsed") or 0) == 15
        and int(after.get("calls") or 0) == 1,
        f"{before} → {after}",
    )

    mixed = usage_batch(
        "batch_g4_mixed",
        [
            usage_event("g4-replay-req", IDEM_SUB, prompt=10, completion=5, latency=120),
            usage_event("g4-new-req", IDEM_SUB, prompt=7, completion=3, latency=90),
        ],
    )
    status, body = post_batch(world, mixed)
    check(
        "混合批次只计入新行",
        status == 200 and body.get("accepted") == 1 and body.get("duplicates") == 1,
        f"{status} {body}",
    )
    status, replayed = post_batch(world, mixed)
    check(
        "重放混合批次全部判重",
        status == 200 and replayed.get("accepted") == 0 and replayed.get("duplicates") == 2,
        f"{status} {replayed}",
    )
    final = totals_of(usage_user(world, IDEM_SUB))
    check(
        "两轮批次叠加后的总量与手算一致（15+10 token / 2 次）",
        int(final.get("tokensUsed") or 0) == 25 and int(final.get("calls") or 0) == 2,
        str(final),
    )


def report_rows(world: World, rows: list[dict], source: str, device: str) -> tuple[int, dict]:
    status, _, body = http(
        world.server_base,
        "POST",
        "/api/v1/usage/report",
        headers={"Authorization": f"Bearer {world.access}"},
        body={
            "source": source,
            "deviceId": device,
            "schemaVersion": 1,
            "rows": rows,
        },
    )
    return status, body if isinstance(body, dict) else {}


def g4_merged_view(world: World) -> None:
    step("G4-3：合并视图里经网关的调用只算一次，BYOK 直连照常计入")
    day = today_utc()
    before = usage_me(world)
    base_tokens = int(totals_of(before).get("tokensUsed") or 0)
    check(
        "基线只有网关那一条",
        base_tokens == TOKENS_PER_CALL and sources_of(before).get("gatewayRows") == 1,
        f"{totals_of(before)} {sources_of(before)}",
    )

    status, body = report_rows(
        world,
        [
            {
                "sub": world.uid,
                "modelName": MODEL,
                "day": day,
                "origin": "gateway",
                "promptTokens": PROMPT_TOKENS,
                "completionTokens": COMPLETION_TOKENS,
                "tokensUsed": TOKENS_PER_CALL,
                "calls": 1,
                "latencyMsTotal": 300,
                "latencySamples": 1,
            }
        ],
        "engine",
        "engine-node-1",
    )
    check("引擎上报经网关的副本被接收", status == 200 and bool(body.get("ok")), f"{status} {body}")

    after_engine = usage_me(world)
    check(
        "经网关的客户端副本被去重，总量不变",
        int(totals_of(after_engine).get("tokensUsed") or 0) == base_tokens
        and sources_of(after_engine).get("gatewayDedupedRows") == 1
        and sources_of(after_engine).get("gatewayDedupedTokens") == TOKENS_PER_CALL,
        f"{totals_of(after_engine)} {sources_of(after_engine)}",
    )

    status, body = report_rows(
        world,
        [
            {
                "sub": world.uid,
                "modelName": "byok-gpt",
                "day": day,
                "origin": "desktop",
                "promptTokens": 40,
                "completionTokens": 10,
                "tokensUsed": 50,
                "calls": 1,
                "latencyMsTotal": 800,
                "latencySamples": 1,
            }
        ],
        "desktop",
        "desktop-byok-1",
    )
    check("BYOK 直连行被接收", status == 200 and bool(body.get("ok")), f"{status} {body}")

    after_desktop = usage_me(world)
    check(
        "BYOK 直连照常计入（+50 token）",
        int(totals_of(after_desktop).get("tokensUsed") or 0) == base_tokens + 50
        and sources_of(after_desktop).get("desktopRows") == 1
        and int(totals_of(after_desktop).get("calls") or 0) == 2,
        f"{totals_of(after_desktop)} {sources_of(after_desktop)}",
    )

    status, _, dept = http(
        world.server_base,
        "GET",
        "/api/v1/admin/usage/departments?period=total",
        headers={"Authorization": f"Bearer {world.admin}"},
    )
    data = dept if isinstance(dept, dict) else {}
    departments = data.get("departments") or []
    summed = sum(int(item.get("tokensUsed") or 0) for item in departments)
    report_total = int((data.get("totals") or {}).get("tokensUsed") or 0)
    check(
        "部门报表与逐用户明细同源（各部门相加等于总量）",
        status == 200 and summed == report_total == base_tokens + 50 + 25,
        f"sum={summed} report={report_total} "
        f"{[(d.get('dept'), d.get('tokensUsed')) for d in departments]}",
    )


def g4_quota_limit(world: World) -> None:
    step("G4-4：部门月额度调到 1 万 token → 超限 429 并带重置时间")
    principal = f"{TENANT}:{DEPT}:*"
    status, _, body = http(
        world.server_base,
        "PATCH",
        f"/api/v1/admin/gateway/quotas/{principal}",
        headers={"Authorization": f"Bearer {world.admin}"},
        body={"monthlyTokens": DEPT_MONTHLY_TOKENS},
    )
    data = body if isinstance(body, dict) else {}
    check("管理代理调整部门月额度", status == 200 and data.get("ok") is True, f"{status} {body}")
    check(
        "覆盖立即生效（有效额度 1 万）",
        data.get("effectiveMonthlyTokens") == DEPT_MONTHLY_TOKENS
        and (data.get("override") or {}).get("monthlyTokens") == DEPT_MONTHLY_TOKENS,
        str(data),
    )
    check(
        "额度响应带重置时间",
        str(data.get("resetAt", "")).endswith("-01T00:00:00Z"),
        str(data.get("resetAt")),
    )

    small = http(
        world.gateway_base,
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {world.gateway_ticket}"},
        body={
            "model": "auto",
            "messages": [{"role": "user", "content": "你好"}],
            "max_tokens": 64,
        },
    )
    check("小请求在新额度内照常通过", small[0] == 200, f"{small[0]} {small[2]}")

    # 先把小请求的用量等上来，再发超限请求：这样「超限不产生用量」才是可判定的。
    # 同一网关/同一用户/同一模型/同一天的两条事件会并入同一条 rollup 行，
    # gatewayRows 仍是 1，因此这里按调用次数等待。
    elapsed, settled = wait_for_view(
        world, lambda item: int(totals_of(item).get("calls") or 0) >= 3
    )
    settled_totals = totals_of(settled)
    settled_tokens = int(settled_totals.get("tokensUsed") or 0)
    check(
        "第二次对话的用量也已上报（网关 2 次 + BYOK 1 次）",
        elapsed < REPORT_WINDOW_SECONDS
        and int(settled_totals.get("calls") or 0) == 3
        and int(settled_totals.get("pricedCalls") or 0) == 2
        and settled_tokens == TOKENS_PER_CALL * 2 + 50,
        f"{elapsed:.1f}s {settled_totals} {sources_of(settled)}",
    )

    big_prompt = "重" * LONG_PROMPT_CHARS
    status, headers, body = http(
        world.gateway_base,
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {world.gateway_ticket}"},
        body={
            "model": "auto",
            "messages": [{"role": "user", "content": big_prompt}],
            "max_tokens": 4096,
        },
    )
    code = ((body or {}).get("error") or {}).get("code") if isinstance(body, dict) else ""
    check("超限返回 429", status == 429, f"{status} {body}")
    check("错误码 QUOTA_TOKENS_EXCEEDED", code == "QUOTA_TOKENS_EXCEEDED", str(body))
    reset_header = headers.get("x-tt-quota-reset", "")
    check(
        "超限响应头带 X-TT-Quota-Reset（下月 1 日 UTC）",
        reset_header.endswith("-01T00:00:00Z"),
        reset_header,
    )

    # 给上报器再多跑一轮的时间：如果超限调用留下了任何事件，这里一定会看到
    time.sleep(6.0)
    view = usage_me(world)
    check(
        "被额度拒绝的调用不产生用量",
        totals_of(view) == settled_totals,
        f"{settled_totals} → {totals_of(view)}",
    )


def g4_queue_and_logs(world: World, gateway_proc: g3.Process, server_proc: g3.Process) -> None:
    step("网关本地账本与安全边界")
    status, _, body = http(
        world.gateway_base,
        "GET",
        "/gateway/v1/admin/health",
        headers={"X-TT-Admin-Token": ADMIN_TOKEN},
    )
    data = body if isinstance(body, dict) else {}
    uploader = data.get("usageUpload") or {}
    check(
        "上报队列已清空（pending=0）",
        status == 200 and uploader.get("pending") == 0 and data.get("usageQueue") == 0,
        f"{status} {uploader}",
    )
    check("健康页报告本网关 id", data.get("gatewayId") == GATEWAY_ID, str(data.get("gatewayId")))
    check("上报器统计到成功时间", bool(uploader.get("lastSuccessAtMs")), str(uploader))

    logs = {"网关": gateway_proc.text(), "控制面": server_proc.text()}
    secrets = {
        "网关管理令牌": ADMIN_TOKEN,
        "网关服务令牌": SERVICE_TOKEN,
        "provider 密钥": PROVIDER_SECRET,
        "主密钥": MASTER_KEY,
    }
    for label, text in logs.items():
        for name, value in secrets.items():
            check(f"{label}日志不含{name}", value not in text, text[:300])
    check("网关日志无 JWT 明文", "eyJ" not in logs["网关"], logs["网关"][:300])


def exchange_gateway_token(world_base: str, access: str) -> str:
    status, headers, body = http(
        world_base,
        "POST",
        "/api/v1/auth/gateway-token",
        headers={"Authorization": f"Bearer {access}"},
        body={},
    )
    ticket = (body or {}).get("token", "") if isinstance(body, dict) else ""
    check(
        "换网关票（响应禁止缓存）",
        status == 200 and bool(ticket) and headers.get("cache-control") == "no-store",
        f"{status} {body}",
    )
    return ticket


def run_acceptance() -> None:
    upstream = g3.FakeUpstream("openai").start()
    server_port = free_port()
    gateway_port = free_port()
    server_base = f"http://127.0.0.1:{server_port}"
    gateway_base = f"http://127.0.0.1:{gateway_port}"
    logs = Path(tempfile.mkdtemp(prefix="tt-g4-"))
    data = logs / "data"
    print(f"[G4] 临时目录：{logs}")
    server_proc = None
    gateway_proc = None
    try:
        server_proc = start_server(server_port, data / "server", logs / "server.log", gateway_base)
        gateway_proc = start_gateway(
            gateway_port,
            control_plane=server_base,
            data_dir=data / "gateway",
            log_path=logs / "gateway.log",
        )

        uid, admin, access = setup_control_plane(
            server_base, gateway_base, f"{upstream.base_url}/v1"
        )
        ticket = exchange_gateway_token(server_base, access)
        world = World(
            server_base=server_base,
            gateway_base=gateway_base,
            admin=admin,
            access=access,
            uid=uid,
            gateway_ticket=ticket,
        )

        g4_one_chat(world)
        g4_replay_idempotency(world)
        g4_merged_view(world)
        g4_quota_limit(world)
        g4_queue_and_logs(world, gateway_proc, server_proc)

        check("上游确实收到对话请求", any(
            item["path"].startswith("/v1/chat/completions") for item in upstream.records
        ), str(upstream.records[-1:]))
    except (urllib.error.URLError, OSError, AssertionError) as exc:
        check("验收脚本未抛异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        if gateway_proc is not None:
            gateway_proc.stop()
        if server_proc is not None:
            server_proc.stop()
        upstream.stop()


def main() -> int:
    run_acceptance()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} 项失败：")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(
        "G4 验收通过：10 秒内出用量、同批重放不重复计数、"
        "网关调用在合并视图只算一次（BYOK 照常）、部门额度超限 429 带重置时间"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
