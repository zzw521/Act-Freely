"""适配层接口与内部规范化结构。

协议面（OpenAI / Anthropic / Gemini）只负责把自己的请求翻译成这里的结构，
再由 ChatAdapter / EmbeddingAdapter 打到上游。适配器之间不得互相依赖，
也不得把上游原始响应直接漏给协议层。

约定：
- `ValueError` 表示调用方构造了非法请求，由协议层转成 `INVALID_REQUEST`；
- `AdapterError` 表示上游失败，由协议层按统一错误码回包；
- 流式开始后再出错，用 `StreamError` 事件收尾，不再抛异常。
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ..errors import ERROR_SPECS, GatewayError

Role = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["stop", "length", "tool_calls", "content_filter", "error"]
ToolChoiceMode = Literal["auto", "none", "required"]

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True, slots=True)
class Usage:
    """token 口径与契约一致：total = prompt + completion。

    cached 是 prompt 的子集，reasoning 是 completion 的子集；只做披露，
    不重复计入总量。`estimated=True` 表示上游没给 usage，网关按估算结算。
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    estimated: bool = False

    def __post_init__(self) -> None:
        for name in (
            "prompt_tokens",
            "completion_tokens",
            "cached_tokens",
            "reasoning_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class ModelTarget:
    """一次请求实际要打的模型与连接信息。

    `api_key` / `extra_headers` 可能是厂商密钥，repr 里已排除；日志禁止 asdict。
    """

    provider: str
    model: str
    upstream_model: str
    alias: str | None = None
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    api_version: str | None = None
    timeout_seconds: int = 120
    extra_headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    extra_params: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for name in ("provider", "model", "upstream_model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 不能为空")
        if not isinstance(self.timeout_seconds, int) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须是正整数")
        if self.base_url and not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url 必须是 http(s) 地址")


@dataclass(frozen=True, slots=True)
class TextPart:
    text: str


@dataclass(frozen=True, slots=True)
class ThinkingPart:
    text: str


@dataclass(frozen=True, slots=True)
class ImagePart:
    url: str | None = None
    data: bytes | None = None
    media_type: str = "image/png"
    detail: str | None = None

    def __post_init__(self) -> None:
        if (self.url is None) == (self.data is None):
            raise ValueError("ImagePart 必须二选一：url 或 data")
        if self.data is not None and not self.data:
            raise ValueError("ImagePart.data 不能为空")
        if not self.media_type.startswith("image/"):
            raise ValueError("media_type 必须是 image/*")
        if self.url is not None and not self.url.startswith(
            ("http://", "https://", "data:image/")
        ):
            raise ValueError("ImagePart.url 必须是 http(s) 或 data:image URI")
        if self.detail is not None and self.detail not in ("auto", "low", "high"):
            raise ValueError("ImagePart.detail 只支持 auto/low/high")


ContentPart = TextPart | ImagePart


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("ToolCall.id 不能为空")
        if not _TOOL_NAME_RE.fullmatch(self.name):
            raise ValueError("ToolCall.name 不合法")
        if not isinstance(self.arguments, str):
            raise ValueError("ToolCall.arguments 必须是 JSON 文本")

    def arguments_json(self) -> dict:
        try:
            value = json.loads(self.arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("ToolCall.arguments 不是合法 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("ToolCall.arguments 必须是 JSON 对象")
        return value


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str = ""
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _TOOL_NAME_RE.fullmatch(self.name):
            raise ValueError("ToolSpec.name 不合法")
        if not isinstance(self.parameters, Mapping):
            raise ValueError("ToolSpec.parameters 必须是 JSON Schema 对象")
        try:
            json.dumps(dict(self.parameters))
        except (TypeError, ValueError) as exc:
            raise ValueError("ToolSpec.parameters 必须可 JSON 序列化") from exc


@dataclass(frozen=True, slots=True)
class NamedToolChoice:
    name: str

    def __post_init__(self) -> None:
        if not _TOOL_NAME_RE.fullmatch(self.name):
            raise ValueError("NamedToolChoice.name 不合法")


ToolChoice = ToolChoiceMode | NamedToolChoice


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str | tuple[ContentPart, ...] | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        if self.role not in ("system", "user", "assistant", "tool"):
            raise ValueError(f"未知 role：{self.role}")
        if self.content is not None and not isinstance(self.content, (str, tuple)):
            raise ValueError("content 必须是字符串、part 元组或 None")
        if isinstance(self.content, tuple):
            if not self.content:
                raise ValueError("content parts 不能为空")
            for part in self.content:
                if not isinstance(part, (TextPart, ImagePart)):
                    raise ValueError("content parts 只支持 TextPart / ImagePart")
        if self.role == "tool":
            if self.content is None or not isinstance(self.content, str):
                raise ValueError("tool 消息必须带字符串 content")
            if not self.tool_call_id:
                raise ValueError("tool 消息必须带 tool_call_id")
            if self.tool_calls:
                raise ValueError("tool 消息不能带 tool_calls")
        else:
            if self.tool_call_id is not None:
                raise ValueError("只有 tool 消息可以带 tool_call_id")
            if (
                self.role == "assistant"
                and self.tool_calls
                and self.content not in (None, "")
            ):
                raise ValueError("assistant 工具调用消息的 content 必须为空")
            if self.role != "assistant" and self.tool_calls:
                raise ValueError("只有 assistant 可以带 tool_calls")
            if self.role in ("system", "user") and self.content is None:
                raise ValueError(f"{self.role} 消息必须带 content")


@dataclass(frozen=True, slots=True)
class ChatRequest:
    target: ModelTarget
    messages: tuple[ChatMessage, ...]
    system: str | None = None
    tools: tuple[ToolSpec, ...] = ()
    tool_choice: ToolChoice = "auto"
    max_tokens: int = 1024
    temperature: float | None = None
    top_p: float | None = None
    stop: tuple[str, ...] = ()
    stream: bool = False
    user: str | None = None
    request_id: str = ""
    response_format: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("messages 不能为空")
        if self.system is not None and not self.system.strip():
            raise ValueError("system 不能为空字符串")
        if (
            isinstance(self.max_tokens, bool)
            or not isinstance(self.max_tokens, int)
            or self.max_tokens <= 0
        ):
            raise ValueError("max_tokens 必须是正整数")
        if self.temperature is not None:
            if isinstance(self.temperature, bool) or not isinstance(
                self.temperature, (int, float)
            ):
                raise ValueError("temperature 必须是数字")
            if not 0 <= self.temperature <= 2:
                raise ValueError("temperature 必须在 [0, 2]")
        if self.top_p is not None:
            if isinstance(self.top_p, bool) or not isinstance(self.top_p, (int, float)):
                raise ValueError("top_p 必须是数字")
            if not 0 <= self.top_p <= 1:
                raise ValueError("top_p 必须在 [0, 1]")
        if not isinstance(self.stop, tuple) or any(
            not isinstance(item, str) or not item for item in self.stop
        ):
            raise ValueError("stop 必须是非空字符串元组")
        if isinstance(self.tool_choice, NamedToolChoice):
            names = {tool.name for tool in self.tools}
            if self.tool_choice.name not in names:
                raise ValueError("tool_choice 指向了未声明的工具")
        elif self.tool_choice not in ("auto", "none", "required"):
            raise ValueError("tool_choice 只支持 auto/none/required 或具名工具")
        if self.tool_choice == "required" and not self.tools:
            raise ValueError("没有 tools 时不能使用 tool_choice=required")
        if self.request_id and len(self.request_id) > 128:
            raise ValueError("request_id 超过 128 个字符")
        if self.response_format is not None:
            if not isinstance(self.response_format, Mapping):
                raise ValueError("response_format 必须是对象")
            try:
                json.dumps(dict(self.response_format))
            except (TypeError, ValueError) as exc:
                raise ValueError("response_format 必须可 JSON 序列化") from exc


ResponsePart = TextPart | ThinkingPart | ToolCall


@dataclass(frozen=True, slots=True)
class ChatResponse:
    parts: tuple[ResponsePart, ...]
    finish_reason: FinishReason
    usage: Usage
    model: str
    provider: str
    response_id: str | None = None

    def __post_init__(self) -> None:
        if not self.parts:
            raise ValueError("ChatResponse.parts 不能为空")
        if any(
            not isinstance(part, (TextPart, ThinkingPart, ToolCall))
            for part in self.parts
        ):
            raise ValueError("ChatResponse.parts 只支持 Text/Thinking/ToolCall")
        if self.finish_reason not in (
            "stop",
            "length",
            "tool_calls",
            "content_filter",
            "error",
        ):
            raise ValueError(f"未知 finish_reason：{self.finish_reason}")
        if not isinstance(self.usage, Usage):
            raise ValueError("usage 必须是 Usage")
        if not self.model or not self.provider:
            raise ValueError("model/provider 不能为空")

    @property
    def text(self) -> str:
        return "".join(
            part.text for part in self.parts if isinstance(part, TextPart)
        )

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return tuple(part for part in self.parts if isinstance(part, ToolCall))


@dataclass(frozen=True, slots=True)
class EmbeddingRequest:
    target: ModelTarget
    inputs: tuple[str, ...]
    user: str | None = None
    request_id: str = ""

    def __post_init__(self) -> None:
        if not self.inputs:
            raise ValueError("inputs 不能为空")
        if any(not isinstance(item, str) or not item.strip() for item in self.inputs):
            raise ValueError("inputs 只能包含非空字符串")
        if self.request_id and len(self.request_id) > 128:
            raise ValueError("request_id 超过 128 个字符")


@dataclass(frozen=True, slots=True)
class EmbeddingResponse:
    vectors: tuple[tuple[float, ...], ...]
    usage: Usage
    model: str
    provider: str

    def __post_init__(self) -> None:
        if not self.vectors:
            raise ValueError("vectors 不能为空")
        dimensions = len(self.vectors[0])
        if dimensions == 0:
            raise ValueError("向量维度不能为 0")
        if any(len(vector) != dimensions for vector in self.vectors):
            raise ValueError("所有向量维度必须一致")
        if not isinstance(self.usage, Usage):
            raise ValueError("usage 必须是 Usage")
        if not self.model or not self.provider:
            raise ValueError("model/provider 不能为空")

    @property
    def dimensions(self) -> int:
        return len(self.vectors[0])


@dataclass(frozen=True, slots=True)
class StreamStart:
    model: str
    provider: str
    response_id: str | None = None


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallStart:
    index: int
    id: str
    name: str

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("tool call index 不能为负")
        if not self.id or not _TOOL_NAME_RE.fullmatch(self.name):
            raise ValueError("tool call id/name 不合法")


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    index: int
    arguments: str

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("tool call index 不能为负")


@dataclass(frozen=True, slots=True)
class ToolCallEnd:
    index: int

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("tool call index 不能为负")


@dataclass(frozen=True, slots=True)
class UsageEvent:
    usage: Usage

    def __post_init__(self) -> None:
        if not isinstance(self.usage, Usage):
            raise ValueError("usage 必须是 Usage")


@dataclass(frozen=True, slots=True)
class StreamStop:
    finish_reason: FinishReason

    def __post_init__(self) -> None:
        if self.finish_reason not in (
            "stop",
            "length",
            "tool_calls",
            "content_filter",
            "error",
        ):
            raise ValueError(f"未知 finish_reason：{self.finish_reason}")


@dataclass(frozen=True, slots=True)
class StreamError:
    code: str
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.code not in ERROR_SPECS:
            raise ValueError(f"未知错误码：{self.code}")
        if not self.message:
            raise ValueError("StreamError.message 不能为空")


StreamEvent = (
    StreamStart
    | TextDelta
    | ThinkingDelta
    | ToolCallStart
    | ToolCallDelta
    | ToolCallEnd
    | UsageEvent
    | StreamStop
    | StreamError
)


ADAPTER_ERROR_CODES = frozenset(
    {
        "INVALID_REQUEST",
        "PROVIDER_AUTH_FAILED",
        "UPSTREAM_RATE_LIMITED",
        "UPSTREAM_TIMEOUT",
        "PROVIDER_UNAVAILABLE",
        "UPSTREAM_ERROR",
        "CLIENT_CANCELLED",
    }
)


def sanitize_error_message(
    message: str, *, secrets: tuple[str, ...] | list[str] = (), limit: int = 2000
) -> str:
    """错误消息进日志/回包前先脱敏：去掉已知密钥，并限制长度。"""
    cleaned = str(message or "")
    for secret in secrets:
        if secret and len(secret) >= 4:
            cleaned = cleaned.replace(secret, "[redacted]")
    cleaned = re.sub(
        r"(?i)(authorization|x-api-key|api[_-]?key)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        cleaned,
    )
    return cleaned[:limit]


class AdapterError(Exception):
    """上游失败。code 必须是网关统一码，协议层直接用它回包。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool | None = None,
        provider: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        if code not in ADAPTER_ERROR_CODES:
            raise ValueError(f"适配器不支持的错误码：{code}")
        spec = ERROR_SPECS[code]
        self.code = code
        self.message = sanitize_error_message(message)
        self.status = status or spec.status
        self.retryable = spec.retryable if retryable is None else retryable
        self.provider = provider
        self.retry_after = retry_after
        super().__init__(f"{code}: {self.message}")

    def to_gateway_error(self) -> GatewayError:
        error = GatewayError(self.code, self.message, status=self.status)
        error.retryable = self.retryable
        return error


class ChatAdapter(Protocol):
    name: str

    async def complete(self, request: ChatRequest) -> ChatResponse: ...

    def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]: ...


class EmbeddingAdapter(Protocol):
    name: str

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse: ...


@dataclass(frozen=True, slots=True)
class AdapterSet:
    chat: ChatAdapter
    embedding: EmbeddingAdapter


__all__ = [
    "ADAPTER_ERROR_CODES",
    "AdapterError",
    "AdapterSet",
    "ChatAdapter",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "ContentPart",
    "EmbeddingAdapter",
    "EmbeddingRequest",
    "EmbeddingResponse",
    "FinishReason",
    "ImagePart",
    "ModelTarget",
    "NamedToolChoice",
    "ResponsePart",
    "Role",
    "StreamError",
    "StreamEvent",
    "StreamStart",
    "StreamStop",
    "TextDelta",
    "TextPart",
    "ThinkingDelta",
    "ThinkingPart",
    "ToolCall",
    "ToolCallDelta",
    "ToolCallEnd",
    "ToolCallStart",
    "ToolChoice",
    "ToolSpec",
    "Usage",
    "UsageEvent",
    "sanitize_error_message",
]
