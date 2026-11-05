"""M1.5 加固自测：超时、重试、流式空闲、SSRF/DNS 与用量幂等。"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须先于 tests.support 导入：Settings 在 import 时固化
os.environ["TT_GATEWAY_STREAM_IDLE_TIMEOUT_SECONDS"] = "1"
os.environ["TT_GATEWAY_CONNECT_TIMEOUT_SECONDS"] = "2"
os.environ["TT_GATEWAY_UPSTREAM_RETRIES"] = "1"

from tests import support  # noqa: E402
from tests.support import check, report  # noqa: E402

from app.adapters import litellm_adapter  # noqa: E402
from app.adapters.base import (  # noqa: E402
    AdapterError,
    ChatMessage,
    ChatRequest,
    ModelTarget,
    StreamError,
    StreamStop,
    TextDelta,
)
from app.adapters.litellm_adapter import (  # noqa: E402
    LiteLLMChatAdapter,
    _map_exception,
    _retry_after,
    _timeout,
    litellm_adapters,
)
from app.config import settings  # noqa: E402
from app.errors import GatewayError  # noqa: E402
from app.upstream import validate_provider_url, verify_target_url  # noqa: E402


def _target(base_url: str) -> ModelTarget:
    return ModelTarget(
        provider="openai_compatible",
        model="fake-chat",
        upstream_model="fake-openai-model",
        base_url=f"{base_url}/v1",
        api_key="sk-hardening-secret",
        timeout_seconds=5,
    )


def _request(base_url: str, **overrides) -> ChatRequest:
    return ChatRequest(
        target=_target(base_url),
        messages=(ChatMessage(role="user", content="hi"),),
        max_tokens=16,
        **overrides,
    )


async def _collect(adapter: LiteLLMChatAdapter, request: ChatRequest) -> list:
    events = []
    async for event in adapter.stream(request):
        events.append(event)
    return events


def test_timeout_and_retry_helpers() -> None:
    timeout = _timeout(10, 120)
    check("连接超时与总超时分开配置", timeout.connect == 10 and timeout.read == 120)

    class Stub(Exception):
        def __init__(self):
            self.response = type("R", (), {"headers": {"retry-after": "2"}})()

    check("Retry-After 解析成秒", _retry_after(Stub()) == 2.0)
    check("没有 Retry-After 时为 None", _retry_after(RuntimeError("boom")) is None)

    error = AdapterError("UPSTREAM_RATE_LIMITED", "x", retry_after=1.5)
    delay = litellm_adapter._retry_delay(3, error)
    check("重试等待不超过总上限且尊重 Retry-After", delay == 1.5)

    mapped = _map_exception(Stub(), provider="openai_compatible", api_key="sk-x")
    check("异常映射带上 Retry-After", mapped.retry_after == 2.0)

    adapters = litellm_adapters(
        connect_timeout_seconds=2, idle_timeout_seconds=1, max_retries=0
    )
    check(
        "适配器工厂接线",
        adapters.chat._idle == 1.0
        and adapters.chat._connect == 2.0
        and adapters.chat._max_retries == 0
        and adapters.embedding._max_retries == 0,
    )
    check(
        "环境变量接线到 Settings",
        settings.stream_idle_timeout_seconds == 1
        and settings.connect_timeout_seconds == 2
        and settings.upstream_retries == 1,
    )


def test_retry_non_stream() -> None:
    upstream = support.FakeUpstream("openai", fail_status=429, fail_times=1).start()
    try:
        adapter = LiteLLMChatAdapter(max_retries=1, connect_timeout_seconds=2)
        response = asyncio.run(adapter.complete(_request(upstream.base_url)))
        check("限流后重试成功", response.usage.prompt_tokens == 12)
        check("重试确实打了两次上游", len(upstream.records) == 2)
        check("重试用尽前上游只失败一次", upstream.failures_served == 1)
    finally:
        upstream.stop()

    upstream = support.FakeUpstream("openai", fail_status=429, fail_times=1).start()
    try:
        adapter = LiteLLMChatAdapter(max_retries=0)
        try:
            asyncio.run(adapter.complete(_request(upstream.base_url)))
            code = None
        except AdapterError as exc:
            code = exc.code
        check("重试次数为 0 时直接失败", code == "UPSTREAM_RATE_LIMITED")
        check("未重试时只打一次上游", len(upstream.records) == 1)
    finally:
        upstream.stop()

    upstream = support.FakeUpstream("openai", fail_status=401).start()
    try:
        adapter = LiteLLMChatAdapter(max_retries=3)
        try:
            asyncio.run(adapter.complete(_request(upstream.base_url)))
            code = None
        except AdapterError as exc:
            code = exc.code
        check("鉴权失败不重试", code == "PROVIDER_AUTH_FAILED")
        check("鉴权失败只打一次上游", len(upstream.records) == 1)
    finally:
        upstream.stop()


def test_retry_stream_open() -> None:
    upstream = support.FakeUpstream("openai", fail_status=503, fail_times=1).start()
    try:
        adapter = LiteLLMChatAdapter(max_retries=1)
        events = asyncio.run(_collect(adapter, _request(upstream.base_url, stream=True)))
        errors = [event for event in events if isinstance(event, StreamError)]
        stops = [event for event in events if isinstance(event, StreamStop)]
        check("流式建连失败可重试", not errors and len(stops) == 1, events[-1:])
        check("流式重试打了两次上游", len(upstream.records) == 2)
    finally:
        upstream.stop()


def test_stream_idle_timeout() -> None:
    upstream = support.FakeUpstream(
        "openai", stall_after=2, stall_seconds=3.0
    ).start()
    try:
        adapter = LiteLLMChatAdapter(idle_timeout_seconds=0.5, max_retries=0)
        started = time.time()
        events = asyncio.run(_collect(adapter, _request(upstream.base_url, stream=True)))
        elapsed = time.time() - started
        texts = [event.text for event in events if isinstance(event, TextDelta)]
        errors = [event for event in events if isinstance(event, StreamError)]
        check("空闲超时前的内容已下发", texts == ["你好"], texts)
        check(
            "流式空闲超时按 UPSTREAM_TIMEOUT 收尾",
            bool(errors) and errors[-1].code == "UPSTREAM_TIMEOUT",
            errors[-1:] if errors else events[-1:],
        )
        check("空闲超时按配置触发（<3 秒）", elapsed < 3.0, f"{elapsed:.2f}s")
    finally:
        upstream.stop()


def test_service_retry_records_one_usage() -> None:
    upstream = support.FakeUpstream("openai", fail_status=429, fail_times=1).start()
    server = support.start_gateway()
    try:
        support.configure_openai_provider(upstream)
        status, _, body = support.http(
            "POST",
            "/v1/chat/completions",
            headers=support.bearer(),
            body={
                "model": "advanced",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 16,
            },
        )
        check("网关层重试后对话成功", status == 200, f"{status} {body}")
        check("网关层重试打两次上游", len(upstream.records) == 2)
        status, _, usage = support.http(
            "GET",
            "/gateway/v1/admin/usage?tenant=acme&sub=u-1",
            headers=support.admin_headers(),
        )
        rows = usage.get("rows") or []
        calls = sum(int(row["calls"]) for row in rows)
        check("重试只记一条用量", status == 200 and calls == 1, f"{status} {rows}")
        check(
            "用量 token 不重复累计",
            sum(int(row["totalTokens"]) for row in rows) == 20,
            rows,
        )
    finally:
        support.stop_gateway(server)
        upstream.stop()


def _rejected(url: str, *, allow_private: bool = False) -> str | None:
    try:
        validate_provider_url(url, allow_private=allow_private)
    except GatewayError as exc:
        return exc.code
    return None


def test_ssrf() -> None:
    check(
        "metadata 地址被拒",
        _rejected("http://169.254.169.254/latest/meta-data") == "INVALID_REQUEST",
    )
    check(
        "IPv6 链路本地被拒",
        _rejected("http://[fe80::1]:8080/v1") == "INVALID_REQUEST",
    )
    check(
        "IPv4 映射 IPv6 私网被拒",
        _rejected("http://[::ffff:127.0.0.1]:8000/v1") == "INVALID_REQUEST",
    )
    check(
        "allowPrivate 只放开私网，不放链路本地",
        _rejected("http://[::ffff:169.254.169.254]/v1", allow_private=True)
        == "INVALID_REQUEST",
    )
    check(
        "userinfo 被拒",
        _rejected("http://user:pass@example.com/v1") == "INVALID_REQUEST",
    )
    check("非 http(s) 协议被拒", _rejected("file:///etc/passwd") == "INVALID_REQUEST")
    check(
        "metadata 主机名被拒",
        _rejected("http://metadata.google.internal/v1") == "INVALID_REQUEST",
    )
    check(
        "十进制编码的回环地址被拒",
        _rejected("http://2130706433:8000/v1") == "INVALID_REQUEST",
    )
    try:
        verify_target_url("http://127.0.0.1:9/v1", allow_private=False)
        code = None
    except GatewayError as exc:
        code = exc.code
    check("请求期复检把私网映射成 PROVIDER_UNAVAILABLE", code == "PROVIDER_UNAVAILABLE")


def main() -> int:
    test_timeout_and_retry_helpers()
    test_retry_non_stream()
    test_retry_stream_open()
    test_stream_idle_timeout()
    test_service_retry_records_one_usage()
    test_ssrf()
    return report("M1.5 加固自测通过")


if __name__ == "__main__":
    raise SystemExit(main())
