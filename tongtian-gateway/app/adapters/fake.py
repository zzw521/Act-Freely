"""假适配器：不打网络，给路由 / 配额 / 协议层做端到端联调。

假实现只保证"形状正确"，不模拟真实模型的语义。真实适配器（T1.5）落位后，
协议层与结算逻辑的测试仍可继续用这里的 `fake_adapters()` 保持确定性。
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from ..errors import ERROR_SPECS
from .base import (
    AdapterError,
    AdapterSet,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    FinishReason,
    NamedToolChoice,
    StreamError,
    StreamEvent,
    StreamStart,
    StreamStop,
    TextDelta,
    TextPart,
    ThinkingDelta,
    ThinkingPart,
    ToolCall,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
    UsageEvent,
)


def _chunks(text: str, size: int = 4) -> tuple[str, ...]:
    return tuple(text[index : index + size] for index in range(0, len(text), size))


def _call_id(request_id: str, index: int) -> str:
    return f"call_{request_id or 'fake'}_{index}"


def _vector(text: str, dimensions: int) -> tuple[float, ...]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return tuple(round(digest[index % len(digest)] / 127.5 - 1.0, 6) for index in range(dimensions))


def _estimate_tokens(inputs: tuple[str, ...]) -> int:
    return max(1, sum((len(item) + 3) // 4 for item in inputs))


@dataclass(frozen=True, slots=True)
class FakeBehavior:
    text: str = "你好"
    thinking: str = ""
    tool_name: str | None = None
    tool_arguments: str = '{"city":"Beijing"}'
    usage: Usage = field(default_factory=lambda: Usage(prompt_tokens=12, completion_tokens=8))
    finish_reason: FinishReason = "stop"
    chunks: tuple[str, ...] | None = None
    delay_seconds: float = 0.0
    fail_before: AdapterError | None = None
    fail_after_chunks: int | None = None
    midstream_code: str = "UPSTREAM_ERROR"
    midstream_message: str = "上游在流式中途断开"
    response_id: str | None = None

    def __post_init__(self) -> None:
        if self.finish_reason not in (
            "stop",
            "length",
            "tool_calls",
            "content_filter",
            "error",
        ):
            raise ValueError(f"未知 finish_reason：{self.finish_reason}")
        if self.fail_after_chunks is not None:
            if (
                isinstance(self.fail_after_chunks, bool)
                or not isinstance(self.fail_after_chunks, int)
                or self.fail_after_chunks < 0
            ):
                raise ValueError("fail_after_chunks 必须是非负整数")
        if self.midstream_code not in ERROR_SPECS:
            raise ValueError(f"未知错误码：{self.midstream_code}")
        if self.delay_seconds < 0:
            raise ValueError("delay_seconds 不能为负")


@dataclass(frozen=True, slots=True)
class FakeEmbeddingBehavior:
    dimensions: int = 8
    usage: Usage | None = None
    delay_seconds: float = 0.0
    fail_before: AdapterError | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.dimensions, bool)
            or not isinstance(self.dimensions, int)
            or self.dimensions <= 0
        ):
            raise ValueError("dimensions 必须是正整数")
        if self.delay_seconds < 0:
            raise ValueError("delay_seconds 不能为负")


class FakeChatAdapter:
    name = "fake"

    def __init__(self, behavior: FakeBehavior | None = None) -> None:
        self.behavior = behavior or FakeBehavior()

    def _tool_name(self, request: ChatRequest) -> str | None:
        if not request.tools or request.tool_choice == "none":
            return None
        if isinstance(request.tool_choice, NamedToolChoice):
            return request.tool_choice.name
        if self.behavior.tool_name:
            if self.behavior.tool_name not in {tool.name for tool in request.tools}:
                raise ValueError("FakeBehavior.tool_name 不在请求的 tools 中")
            return self.behavior.tool_name
        if request.tool_choice == "required":
            return request.tools[0].name
        return None

    def _response_id(self, request: ChatRequest) -> str:
        return self.behavior.response_id or request.request_id or "fake-response"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        behavior = self.behavior
        if behavior.fail_before is not None:
            raise behavior.fail_before
        if behavior.delay_seconds:
            await asyncio.sleep(behavior.delay_seconds)

        name = self._tool_name(request)
        parts: list[TextPart | ThinkingPart | ToolCall] = []
        if behavior.thinking:
            parts.append(ThinkingPart(text=behavior.thinking))
        if name is not None:
            parts.append(
                ToolCall(
                    id=_call_id(request.request_id, 0),
                    name=name,
                    arguments=behavior.tool_arguments,
                )
            )
            finish_reason: FinishReason = "tool_calls"
        else:
            parts.append(TextPart(text=behavior.text))
            finish_reason = behavior.finish_reason

        return ChatResponse(
            parts=tuple(parts),
            finish_reason=finish_reason,
            usage=behavior.usage,
            model=request.target.model,
            provider=request.target.provider,
            response_id=self._response_id(request),
        )

    def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        behavior = self.behavior
        if behavior.fail_before is not None:
            raise behavior.fail_before
        return self._stream(request)

    async def _stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        behavior = self.behavior
        name = self._tool_name(request)
        yield StreamStart(
            model=request.target.model,
            provider=request.target.provider,
            response_id=self._response_id(request),
        )

        events: list[StreamEvent] = []
        if behavior.thinking:
            events.extend(ThinkingDelta(text=part) for part in _chunks(behavior.thinking))
        if name is not None:
            call_id = _call_id(request.request_id, 0)
            events.append(ToolCallStart(index=0, id=call_id, name=name))
            events.extend(
                ToolCallDelta(index=0, arguments=part)
                for part in _chunks(behavior.tool_arguments, size=2)
            )
            events.append(ToolCallEnd(index=0))
            finish_reason: FinishReason = "tool_calls"
        else:
            text_chunks = (
                behavior.chunks
                if behavior.chunks is not None
                else _chunks(behavior.text)
            )
            events.extend(TextDelta(text=part) for part in text_chunks)
            finish_reason = behavior.finish_reason

        for index, event in enumerate(events, start=1):
            if behavior.delay_seconds:
                await asyncio.sleep(behavior.delay_seconds)
            if (
                behavior.fail_after_chunks is not None
                and index > behavior.fail_after_chunks
            ):
                yield StreamError(
                    code=behavior.midstream_code,
                    message=behavior.midstream_message,
                    retryable=ERROR_SPECS[behavior.midstream_code].retryable,
                )
                return
            yield event

        yield UsageEvent(usage=behavior.usage)
        yield StreamStop(finish_reason=finish_reason)


class FakeEmbeddingAdapter:
    name = "fake"

    def __init__(self, behavior: FakeEmbeddingBehavior | None = None) -> None:
        self.behavior = behavior or FakeEmbeddingBehavior()

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        behavior = self.behavior
        if behavior.fail_before is not None:
            raise behavior.fail_before
        if behavior.delay_seconds:
            await asyncio.sleep(behavior.delay_seconds)

        return EmbeddingResponse(
            vectors=tuple(_vector(item, behavior.dimensions) for item in request.inputs),
            usage=behavior.usage
            or Usage(prompt_tokens=_estimate_tokens(request.inputs)),
            model=request.target.model,
            provider=request.target.provider,
        )


def fake_adapters(
    chat_behavior: FakeBehavior | None = None,
    embedding_behavior: FakeEmbeddingBehavior | None = None,
) -> AdapterSet:
    return AdapterSet(
        chat=FakeChatAdapter(chat_behavior),
        embedding=FakeEmbeddingAdapter(embedding_behavior),
    )


__all__ = [
    "FakeBehavior",
    "FakeChatAdapter",
    "FakeEmbeddingAdapter",
    "FakeEmbeddingBehavior",
    "fake_adapters",
]
