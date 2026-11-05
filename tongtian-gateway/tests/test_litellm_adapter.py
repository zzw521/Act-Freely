"""T1.5 / T1.10：LiteLLM 适配器直连假上游的契约测试。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import support  # noqa: E402,F401
from tests.fake_upstreams import FakeUpstream  # noqa: E402
from tests.support import check, report  # noqa: E402

from app.adapters.base import (  # noqa: E402
    AdapterError,
    ChatMessage,
    ChatRequest,
    EmbeddingRequest,
    ImagePart,
    ModelTarget,
    StreamError,
    StreamStart,
    StreamStop,
    TextDelta,
    TextPart,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolSpec,
    UsageEvent,
    sanitize_error_message,
)
from app.adapters.litellm_adapter import (  # noqa: E402
    LiteLLMChatAdapter,
    LiteLLMEmbeddingAdapter,
)

SECRET = "sk-adapter-secret-never-log-9876"


def _target(
    upstream: FakeUpstream,
    *,
    provider: str = "openai_compatible",
    upstream_model: str = "fake-openai-model",
    model: str = "fake-chat",
    base_suffix: str = "/v1",
) -> ModelTarget:
    return ModelTarget(
        provider=provider,
        model=model,
        upstream_model=upstream_model,
        base_url=upstream.base_url + base_suffix,
        api_key=SECRET,
        timeout_seconds=10,
    )


def _request(target: ModelTarget, **overrides) -> ChatRequest:
    payload = {
        "target": target,
        "messages": (ChatMessage(role="user", content="你好"),),
        "max_tokens": 32,
        "request_id": "req-adapter",
    }
    payload.update(overrides)
    return ChatRequest(**payload)


async def _complete(target: ModelTarget, **overrides):
    return await LiteLLMChatAdapter().complete(_request(target, **overrides))


async def _stream(target: ModelTarget, **overrides):
    return [
        event
        async for event in LiteLLMChatAdapter().stream(_request(target, **overrides))
    ]


def _kinds(events) -> list[str]:
    return [type(event).__name__ for event in events]


def run_openai_tests(upstream: FakeUpstream) -> None:
    target = _target(upstream)
    response = asyncio.run(_complete(target))
    record = upstream.records[-1]
    check("适配器 OpenAI 非流式文本", response.text == "你好")
    check(
        "适配器 OpenAI usage",
        response.usage.prompt_tokens == 12
        and response.usage.completion_tokens == 8
        and not response.usage.estimated,
    )
    check(
        "适配器 OpenAI 请求映射",
        record["path"] == "/v1/chat/completions"
        and record["body"]["model"] == "fake-openai-model"
        and record["body"]["max_tokens"] == 32
        and record["body"].get("stream") is not True,
    )
    check(
        "适配器密钥进入上游请求头",
        SECRET in record["headers"].get("authorization", ""),
    )
    check("适配器响应不含密钥", SECRET not in repr(response))

    events = asyncio.run(_stream(target))
    kinds = _kinds(events)
    text = "".join(event.text for event in events if isinstance(event, TextDelta))
    usage = [event.usage for event in events if isinstance(event, UsageEvent)]
    check(
        "适配器 OpenAI 流式事件序列",
        kinds[0] == "StreamStart"
        and kinds[-2:] == ["UsageEvent", "StreamStop"]
        and "StreamError" not in kinds,
        str(kinds),
    )
    check("适配器 OpenAI 流式文本", text == "你好")
    check(
        "适配器 OpenAI 流式 usage",
        len(usage) == 1
        and usage[0].prompt_tokens == 12
        and usage[0].completion_tokens == 8,
    )
    check(
        "适配器 OpenAI 流式结束原因",
        isinstance(events[-1], StreamStop) and events[-1].finish_reason == "stop",
    )
    check("适配器事件不含密钥", SECRET not in str(events))

    tool_events = asyncio.run(
        _stream(
            target,
            tools=(
                ToolSpec(
                    name="get_weather",
                    description="查询天气",
                    parameters={"type": "object", "properties": {}},
                ),
            ),
            tool_choice="auto",
        )
    )
    starts = [event for event in tool_events if isinstance(event, ToolCallStart)]
    deltas = [event for event in tool_events if isinstance(event, ToolCallDelta)]
    ends = [event for event in tool_events if isinstance(event, ToolCallEnd)]
    stop = tool_events[-1]
    check(
        "适配器工具流式映射",
        len(starts) == 1
        and starts[0].name == "get_weather"
        and starts[0].id == "call_fake"
        and "".join(item.arguments for item in deltas) == '{"city":"Beijing"}'
        and len(ends) == 1,
        str(_kinds(tool_events)),
    )
    check(
        "适配器工具流式结束原因",
        isinstance(stop, StreamStop) and stop.finish_reason == "tool_calls",
    )

    vision = asyncio.run(
        _complete(
            target,
            messages=(
                ChatMessage(
                    role="user",
                    content=(
                        TextPart(text="看图"),
                        ImagePart(data=b"\x89PNG\r\n", media_type="image/png"),
                    ),
                ),
            ),
        )
    )
    sent = json.dumps(upstream.records[-1]["body"], ensure_ascii=False)
    check("适配器视觉请求成功", vision.text == "你好")
    check(
        "适配器视觉 part 编码为 data URI",
        "data:image/png;base64," in sent and "image_url" in sent,
        sent[:400],
    )

    embedding_target = _target(
        upstream, upstream_model="fake-embedding", model="fake-embed"
    )
    embedding = asyncio.run(
        LiteLLMEmbeddingAdapter().embed(
            EmbeddingRequest(
                target=embedding_target,
                inputs=("一", "二"),
                request_id="req-embed",
            )
        )
    )
    check(
        "适配器嵌入多输入",
        len(embedding.vectors) == 2
        and embedding.dimensions == 3
        and embedding.usage.prompt_tokens == 10,
        f"{embedding.vectors} {embedding.usage}",
    )
    check(
        "适配器嵌入请求映射",
        upstream.records[-1]["path"] == "/v1/embeddings"
        and upstream.records[-1]["body"]["input"] == ["一", "二"],
    )


def run_anthropic_tests(upstream: FakeUpstream) -> None:
    target = _target(
        upstream,
        provider="anthropic",
        upstream_model="claude-fake",
        model="claude-fake",
        base_suffix="",
    )
    response = asyncio.run(_complete(target))
    check(
        "适配器 Anthropic 非流式",
        response.text == "你好"
        and response.usage.prompt_tokens == 12
        and response.usage.completion_tokens == 8,
        f"{response.text} {response.usage}",
    )
    check(
        "适配器 Anthropic 路径",
        upstream.records[-1]["path"] == "/v1/messages",
    )

    events = asyncio.run(_stream(target))
    kinds = _kinds(events)
    text = "".join(event.text for event in events if isinstance(event, TextDelta))
    usage = [event.usage for event in events if isinstance(event, UsageEvent)]
    stop = events[-1]
    check(
        "适配器 Anthropic 流式事件",
        kinds[0] == "StreamStart"
        and kinds[-2:] == ["UsageEvent", "StreamStop"]
        and "StreamError" not in kinds,
        str(kinds),
    )
    check("适配器 Anthropic 流式文本", text == "你好")
    check(
        "适配器 Anthropic 流式 usage 非零",
        len(usage) == 1
        and usage[0].prompt_tokens == 12
        and usage[0].completion_tokens == 8
        and not usage[0].estimated,
        str(usage),
    )
    check(
        "适配器 Anthropic 结束原因",
        isinstance(stop, StreamStop) and stop.finish_reason == "stop",
    )


def run_gemini_tests(upstream: FakeUpstream) -> None:
    target = _target(
        upstream,
        provider="gemini",
        upstream_model="gemini-fake",
        model="gemini-fake",
        base_suffix="",
    )
    response = asyncio.run(_complete(target))
    check(
        "适配器 Gemini 非流式",
        response.text == "你好"
        and response.usage.prompt_tokens == 12
        and response.usage.completion_tokens == 8,
        f"{response.text} {response.usage}",
    )
    check(
        "适配器 Gemini 自动补 /v1beta",
        upstream.records[-1]["path"]
        == "/v1beta/models/gemini-fake:generateContent",
        upstream.records[-1]["path"],
    )

    events = asyncio.run(_stream(target))
    kinds = _kinds(events)
    text = "".join(event.text for event in events if isinstance(event, TextDelta))
    usage = [event.usage for event in events if isinstance(event, UsageEvent)]
    check(
        "适配器 Gemini 流式事件",
        kinds[0] == "StreamStart"
        and kinds[-2:] == ["UsageEvent", "StreamStop"]
        and "StreamError" not in kinds,
        str(kinds),
    )
    check("适配器 Gemini 流式文本", text == "你好")
    check(
        "适配器 Gemini 流式 usage 非零",
        len(usage) == 1
        and usage[0].prompt_tokens == 12
        and usage[0].completion_tokens == 8
        and not usage[0].estimated,
        str(usage),
    )


def run_error_tests(upstream: FakeUpstream) -> None:
    target = _target(upstream)
    expected = {
        400: "INVALID_REQUEST",
        401: "PROVIDER_AUTH_FAILED",
        429: "UPSTREAM_RATE_LIMITED",
        500: "UPSTREAM_ERROR",
    }
    for status, code in expected.items():
        upstream.fail_status = status
        try:
            asyncio.run(_complete(target))
            observed = None
            message = ""
        except AdapterError as exc:
            observed = exc.code
            message = str(exc)
        check(
            f"适配器错误映射 {status}→{code}",
            observed == code,
            f"实际 {observed}",
        )
        check(f"适配器错误 {status} 不含密钥", SECRET not in message)

    upstream.fail_status = 429
    events = asyncio.run(_stream(target))
    check(
        "适配器流式限流返回 StreamError",
        len(events) == 1
        and isinstance(events[0], StreamError)
        and events[0].code == "UPSTREAM_RATE_LIMITED",
        str(_kinds(events)),
    )
    check(
        "适配器流式错误不带 StreamStop",
        not any(isinstance(event, StreamStop) for event in events),
    )
    upstream.fail_status = None

    upstream.stream_cut_after = 1
    try:
        events = asyncio.run(_stream(target))
    finally:
        upstream.stream_cut_after = None
    check(
        "适配器断流返回 StreamError",
        isinstance(events[-1], StreamError)
        and events[-1].code == "UPSTREAM_ERROR",
        str(_kinds(events)),
    )
    check(
        "适配器断流不伪造结束帧",
        not any(isinstance(event, StreamStop) for event in events)
        and not any(isinstance(event, UsageEvent) for event in events),
    )

    redacted = sanitize_error_message(
        f"Authorization: Bearer {SECRET} api_key={SECRET}", secrets=(SECRET,)
    )
    check("错误消息脱敏", SECRET not in redacted and "[redacted]" in redacted, redacted)


def run_missing_usage_tests(upstream: FakeUpstream) -> None:
    target = _target(upstream)
    upstream.drop_usage = True
    try:
        response = asyncio.run(_complete(target))
        events = asyncio.run(_stream(target))
    finally:
        upstream.drop_usage = False
    check(
        "上游缺 usage 时非流式标记估算",
        response.usage.estimated
        and response.usage.prompt_tokens > 0
        and response.usage.completion_tokens > 0,
        str(response.usage),
    )
    usage = [event.usage for event in events if isinstance(event, UsageEvent)]
    check(
        "上游缺 usage 时流式标记估算",
        len(usage) == 1
        and usage[0].estimated
        and usage[0].prompt_tokens > 0
        and usage[0].completion_tokens > 0,
        str(usage),
    )
    check(
        "缺 usage 仍有结束帧",
        isinstance(events[-1], StreamStop) and events[-1].finish_reason == "stop",
    )


def main() -> int:
    openai = FakeUpstream("openai").start()
    anthropic = FakeUpstream("anthropic").start()
    gemini = FakeUpstream("gemini").start()
    try:
        run_openai_tests(openai)
        run_anthropic_tests(anthropic)
        run_gemini_tests(gemini)
        run_error_tests(openai)
        run_missing_usage_tests(openai)
    finally:
        openai.stop()
        anthropic.stop()
        gemini.stop()
    return report("全部 LiteLLM 适配器测试通过")


if __name__ == "__main__":
    raise SystemExit(main())
