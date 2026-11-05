"""M0 契约测试：仅用标准库 + uvicorn，不依赖 pytest / httpx / litellm。"""

from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError  # noqa: E402

from tests.support import (  # noqa: E402
    ROOT,
    FakeUpstream,
    bearer,
    check,
    configure_openai_provider,
    error_code,
    http,
    make_token,
    parse_sse,
    principal,
    report,
    start_fake_upstreams,
    start_gateway,
    stop_fake_upstreams,
    stop_gateway,
)
from app.contract import PolicyDocument, UsageEvent  # noqa: E402
from app.policy import DEFAULT_POLICY  # noqa: E402
from app.quota import QuotaTracker, reset_at  # noqa: E402
from app.routing import quota_limits, resolve_route  # noqa: E402
from app.usage import UsageBuffer  # noqa: E402

def run_gateway_tests() -> None:
    status, headers, body = http("GET", "/health")
    check("健康检查 200", status == 200, str(body))
    check("健康检查服务名", body.get("service") == "tongtian-gateway", str(body))
    check("健康检查网关 id", body.get("gatewayId") == "gw-test-01", str(body))

    status, _, capabilities = http("GET", "/gateway/v1/capabilities")
    check("能力发现 200", status == 200, str(capabilities))
    check(
        "能力发现契约版本",
        capabilities.get("contractVersion") == "gateway-contract.v1",
        str(capabilities.get("contractVersion")),
    )
    protocols = {item["name"] for item in capabilities.get("protocols", [])}
    check("能力发现三协议", protocols == {"openai", "anthropic", "gemini"}, str(protocols))
    protocol_items = {
        item["name"]: item for item in capabilities.get("protocols", [])
    }
    check(
        "逐协议能力字段齐全",
        all(
            {"stream", "tools", "vision", "embeddings"} <= set(item)
            for item in protocol_items.values()
        ),
        str(protocol_items),
    )
    check(
        "OpenAI 逐协议能力已声明",
        all(
            protocol_items["openai"][key]
            for key in ("stream", "tools", "vision", "embeddings")
        ),
        str(protocol_items["openai"]),
    )
    check(
        "Anthropic/Gemini 逐协议能力已声明",
        all(
            protocol_items[name][key]
            for name in ("anthropic", "gemini")
            for key in ("stream", "tools", "vision")
        )
        and not any(
            protocol_items[name]["embeddings"] for name in ("anthropic", "gemini")
        ),
        str(protocol_items),
    )
    features = capabilities.get("features", {})
    check(
        "M1.2 能力发现只声明已测能力",
        features.get("stream") is True
        and features.get("tools") is True
        and features.get("vision") is True
        and features.get("embeddings") is True
        and features.get("countTokens") is True
        and features.get("promptCaching") is False,
        str(features),
    )
    check(
        "能力发现 provider",
        {"azure_openai", "bailian", "anthropic", "gemini", "vllm", "ollama"}
        <= set(capabilities.get("providers", [])),
        str(capabilities.get("providers")),
    )
    check(
        "能力发现请求上限",
        capabilities.get("limits", {}).get("maxRequestBytes") == 33554432,
        str(capabilities.get("limits")),
    )

    status, _, body = http("GET", "/")
    check("根路径 200", status == 200 and body.get("service") == "通天办公模型网关", str(body))

    status, headers, body = http("POST", "/v1/chat/completions", body={})
    check("OpenAI 缺令牌 401", status == 401, str(body))
    check(
        "OpenAI 缺令牌错误码",
        body.get("error", {}).get("code") == "AUTH_REQUIRED",
        str(body),
    )
    check(
        "OpenAI 缺令牌信封类型",
        body.get("error", {}).get("type") == "authentication_error",
        str(body),
    )
    check("OpenAI 错误响应带请求 id", bool(headers.get("x-tt-request-id")), str(headers))

    status, _, body = http(
        "POST", "/v1/chat/completions", headers=bearer(exp=int(time.time()) - 5)
    )
    check("OpenAI 过期令牌 401", status == 401, str(body))
    check(
        "OpenAI 过期令牌错误码",
        body.get("error", {}).get("code") == "TOKEN_EXPIRED",
        str(body),
    )

    status, _, body = http(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": "Bearer not-a-jwt"},
    )
    check("OpenAI 伪造令牌 401", status == 401, str(body))
    check(
        "OpenAI 伪造令牌错误码",
        body.get("error", {}).get("code") == "AUTH_REQUIRED",
        str(body),
    )

    status, _, body = http(
        "POST",
        "/v1/chat/completions",
        headers=bearer(aud=["tongtian-engine"]),
    )
    check("OpenAI 受众不符 401", status == 401, str(body))
    check(
        "OpenAI 受众不符错误码",
        body.get("error", {}).get("code") == "AUTH_REQUIRED",
        str(body),
    )

    status, _, body = http(
        "POST",
        "/v1/chat/completions",
        headers=bearer(type="refresh"),
    )
    check("OpenAI 刷新令牌被拒 401", status == 401, str(body))

    status, headers, body = http(
        "POST",
        "/v1/chat/completions",
        headers={**bearer(), "X-TT-Request-Id": "req-client-1"},
        body={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    check("OpenAI 合法请求 200", status == 200, str(body))
    check(
        "OpenAI 回复内容",
        body.get("choices", [{}])[0].get("message", {}).get("content") == "你好",
        str(body),
    )
    check(
        "OpenAI 回显请求 id",
        headers.get("x-tt-request-id") == "req-client-1",
        str(headers),
    )
    check("OpenAI 实际模型头", headers.get("x-tt-model") == "fake-chat", str(headers))
    check("OpenAI 别名头", headers.get("x-tt-model-alias") == "auto", str(headers))
    check(
        "OpenAI 配额余量头",
        str(headers.get("x-tt-quota-remaining", "")).isdigit(),
        str(headers),
    )

    status, _, body = http("POST", "/v1/embeddings", headers=bearer(), body={})
    check(
        "OpenAI 嵌入缺参 400",
        status == 400 and body.get("error", {}).get("code") == "INVALID_REQUEST",
        str(body),
    )

    status, _, body = http(
        "POST",
        "/v1/embeddings",
        headers=bearer(),
        body={"model": "embedding-default", "input": ["hello", "world"]},
    )
    check("OpenAI 嵌入 200", status == 200 and len(body.get("data", [])) == 2, str(body))
    check(
        "OpenAI 嵌入向量",
        body.get("data", [{}])[0].get("embedding") == [0.1, 0.2, 0.3],
        str(body),
    )

    status, _, body = http("GET", "/v1/models", headers=bearer())
    model_ids = {item["id"] for item in body.get("data", [])}
    check(
        "OpenAI 模型列表 200",
        status == 200 and body.get("object") == "list" and "auto" in model_ids,
        str(body),
    )
    check(
        "OpenAI 模型列表包含具体模型",
        "missing-model" in model_ids,
        str(model_ids),
    )
    check(
        "OpenAI 模型列表不混入嵌入模型",
        "fake-embed" not in model_ids,
        str(model_ids),
    )

    status, _, body = http("POST", "/v1/messages", body={})
    check("Anthropic 缺令牌 401", status == 401, str(body))
    check("Anthropic 信封顶层 type", body.get("type") == "error", str(body))
    check(
        "Anthropic 缺令牌错误码",
        body.get("error", {}).get("tt_code") == "AUTH_REQUIRED",
        str(body),
    )
    check(
        "Anthropic 缺令牌信封类型",
        body.get("error", {}).get("type") == "authentication_error",
        str(body),
    )

    status, _, body = http(
        "POST",
        "/v1/messages",
        headers={"x-api-key": make_token(), "anthropic-version": "2023-06-01"},
        body={
            "model": "auto",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    check("Anthropic x-api-key 200", status == 200, str(body))
    check(
        "Anthropic x-api-key 回复",
        (body.get("content") or [{}])[0].get("text") == "你好",
        str(body),
    )

    status, headers, body = http(
        "POST",
        "/v1/messages/count_tokens",
        headers={**bearer(), "anthropic-version": "2023-06-01"},
        body={
            "model": "auto",
            "messages": [{"role": "user", "content": "你好"}],
        },
    )
    check(
        "Anthropic count_tokens 200",
        status == 200 and body.get("input_tokens", 0) > 0,
        str(body),
    )
    check("Anthropic count_tokens 估算标记", headers.get("x-tt-estimated") == "1", str(headers))

    status, _, body = http("POST", "/v1beta/models/gemini-fake:generateContent", body={})
    check("Gemini 缺令牌 401", status == 401, str(body))
    check(
        "Gemini 缺令牌状态",
        body.get("error", {}).get("status") == "UNAUTHENTICATED",
        str(body),
    )
    details = body.get("error", {}).get("details") or [{}]
    check("Gemini 缺令牌错误码", details[0].get("code") == "AUTH_REQUIRED", str(body))

    status, _, body = http(
        "POST",
        "/v1beta/models/auto:generateContent",
        headers={"x-goog-api-key": make_token()},
        body={"contents": []},
    )
    check("Gemini x-goog-api-key 参数校验 400", status == 400, str(body))
    check(
        "Gemini 参数校验状态",
        body.get("error", {}).get("status") == "INVALID_ARGUMENT",
        str(body),
    )

    status, _, text = http(
        "POST",
        "/v1beta/models/auto:streamGenerateContent?alt=sse",
        headers=bearer(),
        body={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    )
    frames = parse_sse(text) if isinstance(text, str) else []
    check("Gemini 流式 200", status == 200 and len(frames) >= 2, str(text)[:200])
    check(
        "Gemini 流式终帧",
        bool(frames) and "finishReason" in frames[-1]["data"],
        str(frames[-1]) if frames else "",
    )

    status, _, body = http("GET", "/v1beta/models", headers=bearer())
    model_names = {item.get("name") for item in body.get("models", [])}
    check(
        "Gemini 模型列表 200",
        status == 200 and "models/auto" in model_names,
        str(model_names),
    )

    status, _, body = http(
        "POST",
        "/v1/chat/completions",
        headers={**bearer(), "X-TT-Department": "dept-eng"},
        body={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    check("部门切换通过", status == 200, str(body))

    status, _, body = http(
        "POST",
        "/v1/chat/completions",
        headers={**bearer(), "X-TT-Department": "dept-ops"},
        body={},
    )
    check("越权部门切换 403", status == 403, str(body))
    check(
        "越权部门切换错误码",
        body.get("error", {}).get("code") == "FORBIDDEN",
        str(body),
    )

    legacy = make_token(tenant=None, org="acme", dept=None, depts=None)
    status, _, body = http(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {legacy}"},
        body={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    check("旧 org 令牌兼容", status == 200, str(body))

    missing_tenant = make_token(tenant=None, org=None)
    status, _, body = http(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {missing_tenant}"},
        body={},
    )
    check("缺 tenant 401", status == 401, str(body))

    status, _, body = http("GET", "/gateway/v1/admin/health")
    check("管理 API 缺令牌 401", status == 401, str(body))
    check("管理错误信封", body.get("ok") is False and body.get("code") == "AUTH_REQUIRED", str(body))

    status, _, body = http(
        "GET", "/gateway/v1/admin/health", headers={"X-TT-Admin-Token": "wrong"}
    )
    check("管理 API 错令牌 401", status == 401, str(body))

    admin_headers = {"X-TT-Admin-Token": "test-admin-token"}
    status, _, body = http("GET", "/gateway/v1/admin/health", headers=admin_headers)
    check("管理健康 200", status == 200 and body.get("ok") is True, str(body))
    check("管理健康策略版本", body.get("policyVersion") == "dev#1", str(body))
    check(
        "管理健康配额重置时间",
        isinstance(body.get("quotaResetAt"), str) and body["quotaResetAt"].endswith("Z"),
        str(body),
    )

    status, _, body = http("GET", "/gateway/v1/admin/providers", headers=admin_headers)
    provider_ids = {item["id"] for item in body.get("providers", [])}
    check("管理 provider 清单", status == 200 and "bailian" in provider_ids, str(body))
    provider_text = json.dumps(body.get("providers", [])).lower()
    check(
        "provider 不返回明文密钥",
        "secret" not in provider_text and "plaintext" not in provider_text,
        str(body),
    )

    status, _, body = http("GET", "/gateway/v1/admin/quotas", headers=admin_headers)
    check("管理配额快照", status == 200 and isinstance(body.get("quotas"), list), str(body))
    check(
        "配额快照带周期与覆盖",
        body.get("period") and isinstance(body.get("overrides"), list),
        str(body),
    )

    status, _, body = http("GET", "/gateway/v1/admin/usage", headers=admin_headers)
    check(
        "管理用量查询 200",
        status == 200
        and isinstance(body.get("rows"), list)
        and isinstance((body.get("totals") or {}).get("calls"), int)
        and isinstance(body.get("generatedAtMs"), int),
        str(body)[:200],
    )
    check(
        "用量行不含正文",
        all("prompt" not in row and "content" not in row for row in body.get("rows", [])),
        str(body.get("rows"))[:200],
    )

    status, _, body = http("POST", "/gateway/v1/admin/providers", headers=admin_headers)
    check(
        "管理写操作缺 body 400",
        status == 400 and body.get("code") == "INVALID_REQUEST",
        str(body),
    )


def run_policy_tests() -> None:
    parsed = PolicyDocument.model_validate(DEFAULT_POLICY)
    check("默认策略通过模型校验", True)
    check(
        "策略顶层 version 与 policyVersion 一致",
        parsed.version == parsed.policyVersion == "dev#1",
        str(parsed.version),
    )
    pricing = parsed.models["azure-gpt-4o"].pricing
    check(
        "策略模型定价可解析",
        pricing is not None
        and pricing.currency == "USD"
        and pricing.outputPer1M == "10.00"
        and pricing.reasoningAsOutput is True,
        str(pricing),
    )
    check(
        "未配置缓存单价时为 None",
        parsed.models["bailian-qwen-max"].pricing.cachedInputPer1M is None,
        str(parsed.models["bailian-qwen-max"].pricing),
    )

    try:
        PolicyDocument.model_validate({**DEFAULT_POLICY, "version": "dev#2"})
        rejected = False
    except ValidationError:
        rejected = True
    check("策略 version 冲突被拒", rejected)

    bad_pricing = copy.deepcopy(DEFAULT_POLICY)
    bad_pricing["models"]["azure-gpt-4o"]["pricing"]["inputPer1M"] = "-1"
    try:
        PolicyDocument.model_validate(bad_pricing)
        rejected = False
    except ValidationError:
        rejected = True
    check("非法单价被拒", rejected)

    sales = principal("dept-sales")
    eng = principal("dept-eng")

    resolved = resolve_route(DEFAULT_POLICY, sales, "auto")
    check("销售部门别名链过滤", resolved.model == "bailian-qwen-max", str(resolved))
    check("销售部门别名链只剩一个", resolved.chain == ("bailian-qwen-max",), str(resolved.chain))
    check("销售部门输出上限", resolved.max_output_tokens == 2048, str(resolved))

    resolved = resolve_route(DEFAULT_POLICY, eng, "auto")
    check("研发部门取链首", resolved.model == "azure-gpt-4o", str(resolved))
    check("研发部门保留回退链", len(resolved.chain) == 2, str(resolved.chain))
    check("研发部门输出上限", resolved.max_output_tokens == 8192, str(resolved))

    resolved = resolve_route(DEFAULT_POLICY, sales, "bailian-qwen-max")
    check("具体模型白名单内", resolved.alias is None and resolved.model == "bailian-qwen-max", str(resolved))

    check(
        "具体模型越权",
        error_code(lambda: resolve_route(DEFAULT_POLICY, sales, "azure-gpt-4o"))
        == "MODEL_NOT_ALLOWED",
    )
    check(
        "未知模型",
        error_code(lambda: resolve_route(DEFAULT_POLICY, eng, "no-such-model"))
        == "MODEL_NOT_ALLOWED",
    )
    check(
        "嵌入模型不能当聊天",
        error_code(lambda: resolve_route(DEFAULT_POLICY, eng, "azure-text-embedding-3-small"))
        == "MODEL_NOT_ALLOWED",
    )

    resolved = resolve_route(
        DEFAULT_POLICY, sales, "embedding-default", kind="embedding"
    )
    check(
        "嵌入路由",
        resolved.model == "azure-text-embedding-3-small"
        and resolved.provider == "azure_openai",
        str(resolved),
    )
    check(
        "聊天别名不能走嵌入",
        error_code(lambda: resolve_route(DEFAULT_POLICY, sales, "auto", kind="embedding"))
        == "MODEL_NOT_ALLOWED",
    )

    limits = quota_limits(DEFAULT_POLICY, sales)
    check("部门配额覆盖租户", limits.get("monthlyTokens") == 500000, str(limits))
    check("部门继承未覆盖字段", limits.get("rpm") == 60, str(limits))


def run_quota_tests() -> None:
    tracker = QuotaTracker(clock=lambda: 1761825600.0)
    limits = {"monthlyTokens": 1000, "concurrency": 1}

    reservation = tracker.reserve("acme:dept:u-1", limits, 600)
    check("配额预留成功", reservation.tokens == 600)
    check(
        "月配额不足被拒",
        error_code(lambda: tracker.reserve("acme:dept:u-1", limits, 500))
        == "QUOTA_TOKENS_EXCEEDED",
    )

    tracker.settle(reservation, 300)
    check("结算后剩余正确", tracker.remaining("acme:dept:u-1", limits) == 700)
    tracker.settle(reservation, 999)
    check("重复结算幂等", tracker.remaining("acme:dept:u-1", limits) == 700)

    second = tracker.reserve("acme:dept:u-1", limits, 700)
    check("结清后并发释放", second.tokens == 700)
    tracker.settle(second, 700)
    check("配额用尽", tracker.remaining("acme:dept:u-1", limits) == 0)
    check(
        "用尽后拒绝",
        error_code(lambda: tracker.reserve("acme:dept:u-1", limits, 1))
        == "QUOTA_TOKENS_EXCEEDED",
    )

    concurrency_only = {"monthlyTokens": 10000, "concurrency": 1}
    first = tracker.reserve("acme:dept:u-2", concurrency_only, 1)
    check(
        "并发超限错误码",
        error_code(lambda: tracker.reserve("acme:dept:u-2", concurrency_only, 1))
        == "QUOTA_CONCURRENCY_EXCEEDED",
    )
    tracker.release(first)
    check(
        "释放后恢复并发",
        tracker.reserve("acme:dept:u-2", concurrency_only, 1).tokens == 1,
    )

    reset = reset_at(1761825600.0)
    check("配额重置时间", reset == "2025-11-01T00:00:00Z", reset)
    check("配额快照结构", isinstance(tracker.snapshot(), list))


def build_event(**overrides) -> UsageEvent:
    payload = {
        "gatewayId": "gw-test-01",
        "requestId": "req-1",
        "tenantId": "acme",
        "departmentId": "dept-sales",
        "userId": "u-1",
        "protocol": "openai",
        "endpoint": "/v1/chat/completions",
        "modelAlias": "auto",
        "model": "azure-gpt-4o",
        "provider": "azure_openai",
        "promptTokens": 12,
        "completionTokens": 8,
        "latencyMs": 100,
        "status": "ok",
        "startedAt": 1761825600000,
        "finishedAt": 1761825600100,
    }
    payload.update(overrides)
    return UsageEvent(**payload)


def run_usage_tests() -> None:
    event = build_event(totalTokens=999)
    check("用量事件重算 totalTokens", event.totalTokens == 20, str(event.totalTokens))
    check("用量事件保留成本字段", event.cost == "0" and event.currency == "USD")
    check("用量事件默认来源 gateway", event.origin == "gateway", event.origin)

    minimal = UsageEvent(
        gatewayId="gw-test-01",
        requestId="req-missing-usage",
        tenantId="acme",
        protocol="openai",
        endpoint="/v1/chat/completions",
        model="azure-gpt-4o",
        provider="azure_openai",
        latencyMs=0,
        status="error",
        errorCode="UPSTREAM_TIMEOUT",
        estimated=True,
        startedAt=1761825600000,
        finishedAt=1761825600000,
    )
    check(
        "estimated 允许 usage 缺省",
        minimal.totalTokens == 0,
        str(minimal.totalTokens),
    )

    try:
        build_event(origin="unknown")
        rejected = False
    except ValidationError:
        rejected = True
    check("未知 origin 被拒", rejected)

    try:
        build_event(promptTokens=-1)
        rejected = False
    except ValidationError:
        rejected = True
    check("负 token 被拒绝", rejected)

    try:
        build_event(finishedAt=1)
        rejected = False
    except ValidationError:
        rejected = True
    check("结束时间早于开始时间被拒", rejected)

    buffer = UsageBuffer(batch_size=2)
    check("缓冲首次写入", buffer.append(event) is True)
    check("相同 requestId 去重", buffer.append(build_event(totalTokens=1)) is False)
    check("缓冲长度", len(buffer) == 1, str(len(buffer)))
    check("未满批不触发", buffer.ready() is False)
    buffer.append(build_event(requestId="req-2"))
    check("满批触发", buffer.ready() is True)
    flushed = buffer.flush()
    check("刷出两条", len(flushed) == 2, str(flushed))
    check("刷出后清空", len(buffer) == 0, str(len(buffer)))
    check("刷出后允许重放", buffer.append(event) is True)


def run_schema_tests() -> None:
    names = [
        "capabilities.schema.json",
        "policy.schema.json",
        "usage-event.schema.json",
        "usage-batch.schema.json",
    ]
    for name in names:
        path = ROOT / "schemas" / name
        check(f"Schema 存在 {name}", path.is_file(), str(path))
        if not path.is_file():
            continue
        schema = json.loads(path.read_text(encoding="utf-8"))
        check(f"Schema 可加载 {name}", isinstance(schema.get("properties"), dict), str(schema.keys()))
        check(f"Schema 版本 {name}", schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema")

    policy_schema = json.dumps(
        json.loads((ROOT / "schemas" / "policy.schema.json").read_text(encoding="utf-8"))
    )
    check("策略 Schema 含 version", '"version"' in policy_schema)
    check("策略 Schema 含 pricing", '"pricing"' in policy_schema)
    usage_schema = json.dumps(
        json.loads((ROOT / "schemas" / "usage-event.schema.json").read_text(encoding="utf-8"))
    )
    check("用量 Schema 含 origin", '"origin"' in usage_schema)
    capabilities_schema = json.dumps(
        json.loads((ROOT / "schemas" / "capabilities.schema.json").read_text(encoding="utf-8"))
    )
    check(
        "能力 Schema 含逐协议字段",
        all(f'"{key}"' in capabilities_schema for key in ("stream", "tools", "vision", "embeddings")),
    )

    for name in ("policy.example.json", "usage-event.example.json"):
        path = ROOT / "tests" / "fixtures" / name
        check(f"示例存在 {name}", path.is_file(), str(path))

    policy = json.loads(
        (ROOT / "tests" / "fixtures" / "policy.example.json").read_text(encoding="utf-8")
    )
    PolicyDocument.model_validate(policy)
    check("示例策略可校验", True)

    event_payload = json.loads(
        (ROOT / "tests" / "fixtures" / "usage-event.example.json").read_text(
            encoding="utf-8"
        )
    )
    event = UsageEvent(**event_payload)
    check("示例用量事件可校验", event.totalTokens == 1500, str(event.totalTokens))


def run_upstream_tests(upstreams: dict[str, FakeUpstream]) -> None:
    openai = upstreams["openai"]
    status, headers, body = http(
        "POST",
        "/v1/chat/completions",
        base=openai.base_url,
        headers={"Authorization": "Bearer fake-key"},
        body={"model": "fake-openai-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    check("假 OpenAI 非流式", status == 200, str(body))
    check(
        "假 OpenAI 响应内容",
        body.get("choices", [{}])[0].get("message", {}).get("content") == "你好",
        str(body),
    )
    check(
        "假 OpenAI usage",
        body.get("usage", {}).get("total_tokens") == 20,
        str(body),
    )
    check(
        "假 OpenAI 记录鉴权头",
        openai.records[-1]["headers"].get("authorization") == "Bearer fake-key",
        str(openai.records[-1]),
    )

    status, headers, text = http(
        "POST",
        "/v1/chat/completions",
        base=openai.base_url,
        body={"model": "fake-openai-model", "stream": True},
    )
    check("假 OpenAI 流式 200", status == 200, str(text)[:200])
    check(
        "假 OpenAI 流式类型",
        headers.get("content-type", "").startswith("text/event-stream"),
        str(headers),
    )
    check("假 OpenAI 流式含 [DONE]", isinstance(text, str) and "[DONE]" in text)
    check(
        "假 OpenAI 流式含增量",
        isinstance(text, str) and '"delta"' in text,
    )

    status, _, body = http(
        "POST",
        "/v1/embeddings",
        base=openai.base_url,
        body={"model": "fake-embedding", "input": "hi"},
    )
    check("假 OpenAI 嵌入", status == 200 and body["data"][0]["embedding"] == [0.1, 0.2, 0.3], str(body))

    status, _, body = http("GET", "/v1/models", base=openai.base_url)
    check("假 OpenAI 模型列表", status == 200 and body["data"][0]["id"] == "fake-openai-model", str(body))

    anthropic = upstreams["anthropic"]
    status, _, body = http(
        "POST",
        "/v1/messages",
        base=anthropic.base_url,
        headers={"x-api-key": "fake-key"},
        body={"model": "claude-fake", "max_tokens": 64, "messages": []},
    )
    check("假 Anthropic 非流式", status == 200, str(body))
    check(
        "假 Anthropic 响应结构",
        body.get("type") == "message"
        and body.get("content", [{}])[0].get("text") == "你好",
        str(body),
    )
    check(
        "假 Anthropic usage",
        body.get("usage", {}).get("input_tokens") == 12,
        str(body),
    )

    status, _, text = http(
        "POST",
        "/v1/messages",
        base=anthropic.base_url,
        body={"model": "claude-fake", "max_tokens": 64, "stream": True, "messages": []},
    )
    events = parse_sse(text) if isinstance(text, str) else []
    order = [event["event"] for event in events]
    check(
        "假 Anthropic 流式事件顺序",
        order
        == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ],
        str(order),
    )
    if events:
        start = json.loads(events[0]["data"])
        check(
            "假 Anthropic message_start usage",
            start["message"]["usage"]["input_tokens"] == 12,
            str(start),
        )

    status, _, body = http(
        "POST",
        "/v1/messages/count_tokens",
        base=anthropic.base_url,
        body={"model": "claude-fake", "messages": []},
    )
    check("假 Anthropic count_tokens", status == 200 and body.get("input_tokens") == 12, str(body))

    gemini = upstreams["gemini"]
    status, _, body = http(
        "POST",
        "/v1beta/models/gemini-fake:generateContent",
        base=gemini.base_url,
        headers={"x-goog-api-key": "fake-key"},
        body={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    )
    check("假 Gemini 非流式", status == 200, str(body))
    check(
        "假 Gemini 响应结构",
        body.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text")
        == "你好",
        str(body),
    )
    check(
        "假 Gemini usage",
        body.get("usageMetadata", {}).get("totalTokenCount") == 20,
        str(body),
    )

    status, _, text = http(
        "POST",
        "/v1beta/models/gemini-fake:streamGenerateContent?alt=sse",
        base=gemini.base_url,
        body={"contents": []},
    )
    frames = parse_sse(text) if isinstance(text, str) else []
    check("假 Gemini 流式帧数", len(frames) >= 2, str(frames))
    if frames:
        final = json.loads(frames[-1]["data"])
        check(
            "假 Gemini 流式终帧 usage",
            final.get("usageMetadata", {}).get("totalTokenCount") == 20,
            str(final),
        )

    status, _, body = http(
        "POST",
        "/v1beta/models/gemini-fake:countTokens",
        base=gemini.base_url,
        body={"contents": []},
    )
    check("假 Gemini countTokens", status == 200 and body.get("totalTokens") == 12, str(body))

    status, _, body = http("GET", "/v1beta/models", base=gemini.base_url)
    check(
        "假 Gemini 模型列表",
        status == 200 and body["models"][0]["name"] == "models/gemini-fake",
        str(body),
    )


def main() -> int:
    server = start_gateway()
    upstreams = start_fake_upstreams()
    try:
        configure_openai_provider(upstreams["openai"])
        run_gateway_tests()
        run_policy_tests()
        run_quota_tests()
        run_usage_tests()
        run_schema_tests()
        run_upstream_tests(upstreams)
    finally:
        stop_fake_upstreams(upstreams)
        stop_gateway(server)

    return report("全部契约测试通过")


if __name__ == "__main__":
    raise SystemExit(main())
