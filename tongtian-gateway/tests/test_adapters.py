"""T0.2 适配层测试：标准库 + 假适配器，不依赖 pytest / litellm。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import check, report  # noqa: E402
from app.adapters import (  # noqa: E402
    AdapterError,
    AdapterSet,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    FakeBehavior,
    FakeChatAdapter,
    FakeEmbeddingAdapter,
    FakeEmbeddingBehavior,
    ImagePart,
    ModelTarget,
    NamedToolChoice,
    StreamError,
    StreamStart,
    StreamStop,
    TextDelta,
    TextPart,
    ThinkingPart,
    ToolCall,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolSpec,
    Usage,
    UsageEvent,
    fake_adapters,
    sanitize_error_message,
)

def expect_value_error(name: str, factory) -> None:
    try:
        factory()
    except ValueError:
        check(name, True)
        return
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"抛出了 {type(exc).__name__}: {exc}")
        return
    check(name, False, "未抛 ValueError")


TARGET = ModelTarget(
    provider="openai_compatible",
    model="gpt-4o-mini",
    upstream_model="gpt-4o-mini-2024-07-18",
    alias="fast",
    base_url="https://llm.example.com/v1",
    api_key="sk-live-secret",
    extra_headers={"X-Provider-Key": "header-secret"},
    timeout_seconds=30,
)

WEATHER_TOOL = ToolSpec(
    name="get_weather",
    description="查天气",
    parameters={"type": "object", "properties": {"city": {"type": "string"}}},
)
SEARCH_TOOL = ToolSpec(
    name="web_search",
    description="联网搜索",
    parameters={"type": "object", "properties": {"q": {"type": "string"}}},
)


def chat_request(**overrides) -> ChatRequest:
    values = {
        "target": TARGET,
        "messages": (ChatMessage(role="user", content="今天北京天气如何"),),
    }
    values.update(overrides)
    return ChatRequest(**values)


def embedding_request(**overrides) -> EmbeddingRequest:
    values = {"target": TARGET, "inputs": ("第一段", "第二段")}
    values.update(overrides)
    return EmbeddingRequest(**values)


async def collect(adapter: FakeChatAdapter, request: ChatRequest) -> list:
    return [event async for event in adapter.stream(request)]


def run_structure_tests() -> None:
    check("Usage.total 等于 prompt+completion", Usage(3, 4).total_tokens == 7)
    expect_value_error("Usage 拒绝负数", lambda: Usage(prompt_tokens=-1))
    expect_value_error("Usage 拒绝布尔", lambda: Usage(completion_tokens=True))

    rendered = repr(TARGET)
    check("ModelTarget.repr 不含 api_key", "sk-live-secret" not in rendered, rendered)
    check(
        "ModelTarget.repr 不含 extra_headers",
        "header-secret" not in rendered,
        rendered,
    )
    expect_value_error(
        "ModelTarget 拒绝非 http base_url",
        lambda: ModelTarget(
            provider="p", model="m", upstream_model="m", base_url="ftp://x"
        ),
    )
    expect_value_error(
        "ModelTarget 拒绝 0 超时",
        lambda: ModelTarget(
            provider="p", model="m", upstream_model="m", timeout_seconds=0
        ),
    )

    ImagePart(url="https://x/y.png")
    ImagePart(data=b"\x89PNG", media_type="image/jpeg", detail="high")
    expect_value_error(
        "ImagePart 必须二选一", lambda: ImagePart(url="https://x/y.png", data=b"a")
    )
    expect_value_error("ImagePart 拒绝非图片", lambda: ImagePart(data=b"a", media_type="text/plain"))
    expect_value_error("ImagePart 拒绝 ftp", lambda: ImagePart(url="ftp://x/y.png"))

    ChatMessage(
        role="assistant",
        content="",
        tool_calls=(ToolCall(id="call_1", name="get_weather", arguments='{"city":"北京"}'),),
    )
    expect_value_error(
        "assistant 工具调用不能带正文",
        lambda: ChatMessage(
            role="assistant",
            content="thinking...",
            tool_calls=(ToolCall(id="call_1", name="get_weather"),),
        ),
    )
    expect_value_error(
        "tool 消息必须有 tool_call_id",
        lambda: ChatMessage(role="tool", content="{}"),
    )
    expect_value_error(
        "user 消息必须有 content", lambda: ChatMessage(role="user")
    )
    expect_value_error(
        "工具名必须符合正则", lambda: ToolCall(id="call_1", name="查天气")
    )
    expect_value_error(
        "arguments 必须是 JSON 对象",
        lambda: ToolCall(id="call_1", name="get_weather", arguments="[]").arguments_json(),
    )

    expect_value_error("ChatRequest 拒绝空 messages", lambda: chat_request(messages=()))
    expect_value_error("ChatRequest 拒绝 max_tokens=0", lambda: chat_request(max_tokens=0))
    expect_value_error("ChatRequest 拒绝布尔 max_tokens", lambda: chat_request(max_tokens=True))
    expect_value_error("ChatRequest 拒绝 temperature>2", lambda: chat_request(temperature=2.5))
    expect_value_error("ChatRequest 拒绝 top_p>1", lambda: chat_request(top_p=1.2))
    expect_value_error(
        "没有 tools 时不能 required", lambda: chat_request(tool_choice="required")
    )
    expect_value_error(
        "具名工具必须存在",
        lambda: chat_request(
            tools=(WEATHER_TOOL,), tool_choice=NamedToolChoice(name="web_search")
        ),
    )

    response = ChatResponse(
        parts=(
            ThinkingPart(text="想一想"),
            TextPart(text="您好，"),
            TextPart(text="北京晴"),
        ),
        finish_reason="stop",
        usage=Usage(10, 5),
        model="gpt-4o-mini",
        provider="openai_compatible",
    )
    check("ChatResponse.text 只拼接正文", response.text == "您好，北京晴")
    check("ChatResponse.tool_calls 为空", response.tool_calls == ())
    expect_value_error(
        "ChatResponse 拒绝空 parts",
        lambda: ChatResponse(
            parts=(),
            finish_reason="stop",
            usage=Usage(),
            model="m",
            provider="p",
        ),
    )
    expect_value_error(
        "ChatResponse 拒绝未知 part",
        lambda: ChatResponse(
            parts=(ImagePart(data=b"a"),),
            finish_reason="stop",
            usage=Usage(),
            model="m",
            provider="p",
        ),
    )

    expect_value_error(
        "EmbeddingResponse 拒绝维度不一致",
        lambda: EmbeddingResponse(
            vectors=((0.1, 0.2), (0.3,)),
            usage=Usage(),
            model="m",
            provider="p",
        ),
    )
    expect_value_error(
        "EmbeddingRequest 拒绝空输入", lambda: embedding_request(inputs=())
    )
    expect_value_error(
        "EmbeddingRequest 拒绝空白输入", lambda: embedding_request(inputs=("  ",))
    )

    cleaned = sanitize_error_message(
        "upstream said Authorization: Bearer sk-live-secret, key sk-live-secret",
        secrets=("sk-live-secret",),
    )
    check("错误消息脱敏密钥", "sk-live-secret" not in cleaned, cleaned)
    check("错误消息脱敏 header 形态", "Bearer" not in cleaned, cleaned)
    check(
        "错误消息截断",
        sanitize_error_message("x" * 50, limit=10) == "x" * 10,
    )


def run_adapter_error_tests() -> None:
    expect_value_error(
        "AdapterError 拒绝非适配器错误码",
        lambda: AdapterError("POLICY_STALE", "x"),
    )
    error = AdapterError(
        "UPSTREAM_TIMEOUT",
        "connect timeout, api_key=sk-live-secret",
        retryable=False,
    )
    check("AdapterError 脱敏", "sk-live-secret" not in error.message, error.message)
    check("AdapterError 可覆盖 retryable", error.retryable is False)
    gateway_error = error.to_gateway_error()
    check("AdapterError 转 GatewayError 保留 code", gateway_error.code == "UPSTREAM_TIMEOUT")
    check("AdapterError 转 GatewayError 保留 status", gateway_error.status == 504)
    check("AdapterError 转 GatewayError 保留 retryable", gateway_error.retryable is False)


def run_fake_chat_tests() -> None:
    adapter = FakeChatAdapter()
    response = asyncio.run(adapter.complete(chat_request(request_id="req-1")))
    check("假适配器返回文本", response.text == "你好", response.text)
    check("假适配器返回 stop", response.finish_reason == "stop")
    check("假适配器回填 usage", response.usage.total_tokens == 20)
    check("假适配器回填 model/provider", response.model == "gpt-4o-mini" and response.provider == "openai_compatible")
    check("假适配器使用 request_id 作 response_id", response.response_id == "req-1")

    tools = (WEATHER_TOOL, SEARCH_TOOL)
    tool_response = asyncio.run(
        FakeChatAdapter().complete(chat_request(tools=tools, tool_choice="required"))
    )
    check("required 触发工具调用", len(tool_response.tool_calls) == 1)
    check("required 使用第一个工具", tool_response.tool_calls[0].name == "get_weather")
    check("工具调用 finish_reason", tool_response.finish_reason == "tool_calls")
    check(
        "工具参数可解析",
        tool_response.tool_calls[0].arguments_json() == {"city": "Beijing"},
    )

    named_response = asyncio.run(
        FakeChatAdapter().complete(
            chat_request(tools=tools, tool_choice=NamedToolChoice(name="web_search"))
        )
    )
    check("具名工具优先", named_response.tool_calls[0].name == "web_search")

    behavior_response = asyncio.run(
        FakeChatAdapter(FakeBehavior(tool_name="web_search")).complete(
            chat_request(tools=tools, tool_choice="auto")
        )
    )
    check("behavior.tool_name 生效", behavior_response.tool_calls[0].name == "web_search")

    none_response = asyncio.run(
        FakeChatAdapter(FakeBehavior(tool_name="web_search")).complete(
            chat_request(tools=tools, tool_choice="none")
        )
    )
    check("tool_choice=none 抑制工具", not none_response.tool_calls)

    expect_value_error(
        "behavior.tool_name 必须已声明",
        lambda: asyncio.run(
            FakeChatAdapter(FakeBehavior(tool_name="missing")).complete(
                chat_request(tools=tools)
            )
        ),
    )

    thinking_response = asyncio.run(
        FakeChatAdapter(FakeBehavior(thinking="先查再答")).complete(chat_request())
    )
    check(
        "thinking 先于正文",
        isinstance(thinking_response.parts[0], ThinkingPart)
        and thinking_response.text == "你好",
    )

    failure = AdapterError("UPSTREAM_RATE_LIMITED", "上游限流")
    try:
        asyncio.run(FakeChatAdapter(FakeBehavior(fail_before=failure)).complete(chat_request()))
    except AdapterError as exc:
        check("fail_before 原样抛出", exc is failure)
    else:
        check("fail_before 原样抛出", False, "未抛出 AdapterError")

    stream_failure = AdapterError("PROVIDER_UNAVAILABLE", "上游不可用")
    try:
        FakeChatAdapter(FakeBehavior(fail_before=stream_failure)).stream(chat_request())
    except AdapterError as exc:
        check("stream 在开始前抛错", exc is stream_failure)
    else:
        check("stream 在开始前抛错", False, "未抛出 AdapterError")


def run_fake_stream_tests() -> None:
    events = asyncio.run(collect(FakeChatAdapter(), chat_request(request_id="s-1")))
    check("流以 StreamStart 开头", isinstance(events[0], StreamStart))
    check("StreamStart 带 model", events[0].model == "gpt-4o-mini")
    check("流以 StreamStop 结尾", isinstance(events[-1], StreamStop))
    check("StreamStop 为 stop", events[-1].finish_reason == "stop")
    check("流包含 UsageEvent", any(isinstance(event, UsageEvent) for event in events))
    check(
        "文本增量可拼回正文",
        "".join(event.text for event in events if isinstance(event, TextDelta)) == "你好",
    )
    check(
        "正常流不带 StreamError",
        not any(isinstance(event, StreamError) for event in events),
    )

    chunked = asyncio.run(
        collect(
            FakeChatAdapter(FakeBehavior(chunks=("你", "好", "呀"))),
            chat_request(),
        )
    )
    deltas = [event.text for event in chunked if isinstance(event, TextDelta)]
    check("自定义 chunks 原样下发", deltas == ["你", "好", "呀"], str(deltas))

    tool_events = asyncio.run(
        collect(
            FakeChatAdapter(),
            chat_request(tools=(WEATHER_TOOL,), tool_choice="required"),
        )
    )
    starts = [event for event in tool_events if isinstance(event, ToolCallStart)]
    ends = [event for event in tool_events if isinstance(event, ToolCallEnd)]
    arguments = "".join(
        event.arguments for event in tool_events if isinstance(event, ToolCallDelta)
    )
    check("只有一次工具开始", len(starts) == 1)
    check("工具开始带名字", starts and starts[0].name == "get_weather")
    check("工具增量可拼回参数", arguments == '{"city":"Beijing"}', arguments)
    check("工具结束在参数之后", len(ends) == 1 and tool_events.index(ends[0]) > tool_events.index(starts[0]))
    check("工具流 finish_reason", tool_events[-1].finish_reason == "tool_calls")

    broken = asyncio.run(
        collect(
            FakeChatAdapter(
                FakeBehavior(
                    text="你好呀朋友",
                    fail_after_chunks=1,
                    midstream_code="UPSTREAM_RATE_LIMITED",
                    midstream_message="上游限流",
                )
            ),
            chat_request(),
        )
    )
    errors = [event for event in broken if isinstance(event, StreamError)]
    check("中途错误产生 StreamError", len(errors) == 1)
    check("StreamError 带统一错误码", errors and errors[0].code == "UPSTREAM_RATE_LIMITED")
    check("StreamError 继承可重试性", errors and errors[0].retryable is True)
    check("中断后不再发 StreamStop", not any(isinstance(event, StreamStop) for event in broken))
    check("中断后不再发 UsageEvent", not any(isinstance(event, UsageEvent) for event in broken))
    check(
        "中断前已发一个增量",
        len([event for event in broken if isinstance(event, TextDelta)]) == 1,
    )


def run_fake_embedding_tests() -> None:
    adapter = FakeEmbeddingAdapter()
    first = asyncio.run(adapter.embed(embedding_request()))
    second = asyncio.run(adapter.embed(embedding_request()))
    check("嵌入返回两段", len(first.vectors) == 2)
    check("嵌入维度生效", first.dimensions == 8)
    check("嵌入确定可复现", first.vectors == second.vectors)
    check("嵌入用请求模型名", first.model == "gpt-4o-mini")
    check("嵌入估算 usage", first.usage.prompt_tokens > 0)
    check(
        "嵌入向量归一到 [-1,1]",
        all(-1.0 <= value <= 1.0 for vector in first.vectors for value in vector),
    )
    check(
        "不同输入不同向量", first.vectors[0] != first.vectors[1]
    )

    sized = asyncio.run(
        FakeEmbeddingAdapter(FakeEmbeddingBehavior(dimensions=16, usage=Usage(5, 0))).embed(
            embedding_request()
        )
    )
    check("可配置维度", sized.dimensions == 16)
    check("可指定 usage", sized.usage.total_tokens == 5)

    failure = AdapterError("PROVIDER_AUTH_FAILED", "密钥无效")
    try:
        asyncio.run(FakeEmbeddingAdapter(FakeEmbeddingBehavior(fail_before=failure)).embed(embedding_request()))
    except AdapterError as exc:
        check("嵌入失败原样抛出", exc is failure)
    else:
        check("嵌入失败原样抛出", False, "未抛出 AdapterError")

    adapters = fake_adapters()
    check("fake_adapters 返回 AdapterSet", isinstance(adapters, AdapterSet))
    check(
        "AdapterSet 两个实现都就位",
        adapters.chat.name == "fake" and adapters.embedding.name == "fake",
    )


def main() -> int:
    run_structure_tests()
    run_adapter_error_tests()
    run_fake_chat_tests()
    run_fake_stream_tests()
    run_fake_embedding_tests()

    return report("全部适配层测试通过")


if __name__ == "__main__":
    raise SystemExit(main())
