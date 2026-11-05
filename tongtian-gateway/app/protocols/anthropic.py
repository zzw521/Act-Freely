"""Anthropic Messages 协议面：`/v1/messages` 与 `/v1/messages/count_tokens`。

请求侧把 Anthropic 的 content blocks 翻成内部 OpenAI 形态消息；响应侧把内部
规范化事件编回 `message_start` / `content_block_*` / `message_delta` / `message_stop`。
Anthropic 允许一个 assistant 回合同时带 text 与 tool_use，而内部模型要求
tool_calls 消息的 content 为空，因此这种回合会拆成两条 assistant 消息。
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..adapters.base import (
    ChatMessage,
    ChatRequest,
    ImagePart,
    NamedToolChoice,
    StreamError,
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
    ToolSpec,
    Usage,
    UsageEvent,
)
from ..auth import Principal
from ..errors import GatewayError, envelope
from ..state import service
from ..usage_store import (
    estimate_prompt_tokens,
    estimate_text_tokens,
)
from .common import (
    current_principal,
    json_body,
    require_header,
    resolve_model,
    scoped_principal,
    sse_event,
)

logger = logging.getLogger("tongtian.gateway.anthropic")

router = APIRouter(tags=["anthropic"])

MESSAGES_ENDPOINT = "/v1/messages"
COUNT_TOKENS_ENDPOINT = "/v1/messages/count_tokens"

# 单张图片解码后的上限；更大的请求直接 413，避免把内存打满
MAX_IMAGE_BYTES = 20 * 1024 * 1024

_STOP_REASONS = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
    "error": "end_turn",
}


def _invalid(message: str, param: str | None = None) -> GatewayError:
    return GatewayError.of("INVALID_REQUEST", message, param=param)


def _message(**kwargs) -> ChatMessage:
    try:
        return ChatMessage(**kwargs)
    except ValueError as exc:
        raise _invalid(str(exc), "messages") from None


def _tool_call(index: int, raw: dict) -> ToolCall:
    call_id = raw.get("id")
    name = raw.get("name")
    payload = raw.get("input")
    if not isinstance(call_id, str) or not call_id:
        raise _invalid("tool_use 缺少 id", "messages")
    if not isinstance(name, str) or not name:
        raise _invalid("tool_use 缺少 name", "messages")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise _invalid("tool_use.input 必须是 JSON 对象", "messages")
    try:
        return ToolCall(
            id=call_id, name=name, arguments=json.dumps(payload, ensure_ascii=False)
        )
    except ValueError as exc:
        raise _invalid(str(exc), "messages") from None


def _image_block(block: dict) -> ImagePart:
    source = block.get("source")
    if not isinstance(source, dict):
        raise _invalid("image block 缺少 source", "messages")
    kind = source.get("type")
    if kind == "base64":
        data = source.get("data")
        media = source.get("media_type")
        if not isinstance(data, str) or not isinstance(media, str):
            raise _invalid("image base64 缺少 data/media_type", "messages")
        try:
            raw = base64.b64decode("".join(data.split()), validate=True)
        except (ValueError, TypeError) as exc:
            raise _invalid("image base64 不合法", "messages") from exc
        if len(raw) > MAX_IMAGE_BYTES:
            raise GatewayError.of(
                "PAYLOAD_TOO_LARGE",
                f"单张图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB 上限",
            )
        with_data = ImagePart(data=raw, media_type=media)
        return with_data
    if kind == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url:
            raise _invalid("image url 缺少 url", "messages")
        return ImagePart(url=url)
    raise _invalid(f"不支持的 image source：{kind}", "messages")


def _parse_system(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        if not raw.strip():
            raise _invalid("system 不能为空", "system")
        return raw
    if isinstance(raw, list):
        texts = []
        for block in raw:
            if not isinstance(block, dict) or block.get("type") != "text":
                raise _invalid("system 只支持 text block", "system")
            text = block.get("text")
            if not isinstance(text, str):
                raise _invalid("system text block 缺少 text", "system")
            texts.append(text)
        joined = "".join(texts)
        if not joined.strip():
            raise _invalid("system 不能为空", "system")
        return joined
    raise _invalid("system 必须是字符串或 text block 数组", "system")


def _tool_result_message(block: dict) -> ChatMessage:
    call_id = block.get("tool_use_id")
    if not isinstance(call_id, str) or not call_id:
        raise _invalid("tool_result 缺少 tool_use_id", "messages")
    content = block.get("content")
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    elif isinstance(content, list):
        chunks = []
        for item in content:
            if not isinstance(item, dict):
                raise _invalid("tool_result.content block 必须是对象", "messages")
            kind = item.get("type")
            if kind == "text":
                value = item.get("text")
                if not isinstance(value, str):
                    raise _invalid("tool_result text block 缺少 text", "messages")
                chunks.append(value)
            elif kind in ("image", "document"):
                # 内部 tool 消息只承载文本，图片类结果丢弃
                logger.debug("tool_result 丢弃 %s block", kind)
            else:
                raise _invalid(f"tool_result 不支持 {kind} block", "messages")
        text = "".join(chunks)
    else:
        raise _invalid("tool_result.content 必须是字符串或 block 数组", "messages")
    return _message(role="tool", content=text, tool_call_id=call_id)


def _user_content(content: Any) -> list[ChatMessage]:
    if isinstance(content, str):
        if not content:
            raise _invalid("user 消息 content 不能为空", "messages")
        return [_message(role="user", content=content)]
    if not isinstance(content, list) or not content:
        raise _invalid("user 消息 content 必须是非空字符串或 block 数组", "messages")
    messages: list[ChatMessage] = []
    parts: list[Any] = []
    texts: list[str] = []

    def flush() -> None:
        if parts:
            messages.append(_message(role="user", content=tuple(parts)))
            parts.clear()
        elif texts:
            messages.append(_message(role="user", content="".join(texts)))
            texts.clear()

    for block in content:
        if not isinstance(block, dict):
            raise _invalid("content block 必须是对象", "messages")
        kind = block.get("type")
        if kind == "text":
            value = block.get("text")
            if not isinstance(value, str):
                raise _invalid("text block 缺少 text", "messages")
            if parts:
                parts.append(TextPart(text=value))
            else:
                texts.append(value)
        elif kind == "image":
            parts.append(_image_block(block))
        elif kind == "tool_result":
            flush()
            messages.append(_tool_result_message(block))
        else:
            raise _invalid(f"user 消息不支持 {kind} block", "messages")
    flush()
    if not messages:
        raise _invalid("user 消息 content 不能为空", "messages")
    return messages


def _assistant_content(content: Any) -> list[ChatMessage]:
    if content is None:
        return [_message(role="assistant", content=None)]
    if isinstance(content, str):
        return [_message(role="assistant", content=content)]
    if not isinstance(content, list):
        raise _invalid("assistant 消息 content 必须是字符串、block 数组或 null", "messages")
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in content:
        if not isinstance(block, dict):
            raise _invalid("content block 必须是对象", "messages")
        kind = block.get("type")
        if kind == "text":
            value = block.get("text")
            if not isinstance(value, str):
                raise _invalid("text block 缺少 text", "messages")
            texts.append(value)
        elif kind == "tool_use":
            calls.append(_tool_call(len(calls), block))
        elif kind in ("thinking", "redacted_thinking"):
            # 客户端回放的历史思考块，内部没有对应字段，直接丢弃
            continue
        else:
            raise _invalid(f"assistant 消息不支持 {kind} block", "messages")
    messages: list[ChatMessage] = []
    text = "".join(texts)
    if text:
        messages.append(_message(role="assistant", content=text))
    if calls:
        messages.append(_message(role="assistant", content=None, tool_calls=tuple(calls)))
    if not messages:
        messages.append(_message(role="assistant", content=None))
    return messages


def _parse_messages(raw: Any) -> tuple[ChatMessage, ...]:
    if not isinstance(raw, list) or not raw:
        raise _invalid("messages 必须是非空数组", "messages")
    messages: list[ChatMessage] = []
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid("message 必须是对象", "messages")
        role = str(item.get("role") or "").strip().lower()
        content = item.get("content")
        if role == "user":
            messages.extend(_user_content(content))
        elif role == "assistant":
            messages.extend(_assistant_content(content))
        else:
            raise _invalid(f"不支持的 role：{item.get('role')}", "messages")
    return tuple(messages)


def _parse_tools(raw: Any) -> tuple[ToolSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _invalid("tools 必须是数组", "tools")
    specs = []
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid("tools[] 必须是对象", "tools")
        name = item.get("name")
        description = item.get("description") or ""
        schema = item.get("input_schema")
        if schema is None:
            schema = {"type": "object", "properties": {}}
        if not isinstance(name, str) or not name:
            raise _invalid("tools[] 缺少 name", "tools")
        if not isinstance(description, str):
            raise _invalid("tools[] description 必须是字符串", "tools")
        if not isinstance(schema, dict):
            raise _invalid("tools[] input_schema 必须是 JSON Schema 对象", "tools")
        try:
            specs.append(
                ToolSpec(name=name, description=description, parameters=schema)
            )
        except ValueError as exc:
            raise _invalid(str(exc), "tools") from None
    return tuple(specs)


def _parse_tool_choice(raw: Any, tools: tuple[ToolSpec, ...]):
    if raw is None:
        return "auto"
    if not isinstance(raw, dict):
        raise _invalid("tool_choice 必须是对象", "tool_choice")
    kind = raw.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        if not tools:
            raise _invalid("没有 tools 时不能使用 tool_choice=any", "tool_choice")
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool":
        name = raw.get("name")
        if not isinstance(name, str) or name not in {tool.name for tool in tools}:
            raise _invalid("tool_choice 指向了未声明的工具", "tool_choice")
        return NamedToolChoice(name)
    raise _invalid(f"不支持的 tool_choice：{kind}", "tool_choice")


def _parse_stop(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and item for item in raw
    ):
        raise _invalid("stop_sequences 必须是非空字符串数组", "stop_sequences")
    return tuple(raw)


def _parse_number(raw: Any, name: str) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _invalid(f"{name} 必须是数字", name)
    return float(raw)


def parse_body(body: dict) -> dict:
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise _invalid("缺少 model 参数", "model")
    max_tokens = body.get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise _invalid("max_tokens 必须是正整数", "max_tokens")
    messages = _parse_messages(body.get("messages"))
    system = _parse_system(body.get("system"))
    tools = _parse_tools(body.get("tools"))
    tool_choice = _parse_tool_choice(body.get("tool_choice"), tools)
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise _invalid("stream 必须是布尔值", "stream")
    metadata = body.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise _invalid("metadata 必须是对象", "metadata")
    user = (metadata or {}).get("user_id")
    if user is not None and not isinstance(user, str):
        raise _invalid("metadata.user_id 必须是字符串", "metadata")
    if isinstance(body.get("thinking"), dict):
        logger.debug("丢弃 thinking 参数：内部没有对应字段")
    temperature = _parse_number(body.get("temperature"), "temperature")
    if temperature is not None and not 0 <= temperature <= 2:
        raise _invalid("temperature 必须在 [0, 2]", "temperature")
    top_p = _parse_number(body.get("top_p"), "top_p")
    if top_p is not None and not 0 <= top_p <= 1:
        raise _invalid("top_p 必须在 [0, 1]", "top_p")
    return {
        "model": model.strip(),
        "messages": messages,
        "system": system,
        "tools": tools,
        "tool_choice": tool_choice,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "stop": _parse_stop(body.get("stop_sequences")),
        "stream": stream,
        "user": user,
        "prompt_estimate": estimate_prompt_tokens(messages, system),
    }


def _chat_request(parsed: dict, target, max_tokens: int) -> ChatRequest:
    try:
        return ChatRequest(
            target=target,
            messages=parsed["messages"],
            system=parsed["system"],
            tools=parsed["tools"],
            tool_choice=parsed["tool_choice"],
            max_tokens=max_tokens,
            temperature=parsed["temperature"],
            top_p=parsed["top_p"],
            stop=parsed["stop"],
            stream=parsed["stream"],
            user=parsed["user"],
            request_id=parsed["request_id"],
        )
    except ValueError as exc:
        raise _invalid(str(exc)) from None


def _tool_input(arguments: str) -> dict:
    try:
        value = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _content_blocks(response) -> list[dict]:
    blocks = []
    for part in response.parts:
        if isinstance(part, ThinkingPart):
            blocks.append({"type": "thinking", "thinking": part.text, "signature": ""})
        elif isinstance(part, TextPart):
            blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, ToolCall):
            blocks.append(
                {
                    "type": "tool_use",
                    "id": part.id,
                    "name": part.name,
                    "input": _tool_input(part.arguments),
                }
            )
    return blocks


def _usage_payload(usage: Usage, *, include_input: bool) -> dict:
    payload: dict[str, Any] = {"output_tokens": usage.completion_tokens}
    if include_input:
        payload["input_tokens"] = usage.prompt_tokens
    if usage.cached_tokens:
        payload["cache_read_input_tokens"] = usage.cached_tokens
    return payload


class _StreamEncoder:
    """把内部流事件编成 Anthropic 的命名 SSE 事件。"""

    def __init__(self, execution) -> None:
        self.model = execution.plan.requested
        self.message_id = f"msg_{execution.request_id}"
        self.prompt_estimate = max(1, execution.prompt_estimate)
        self.usage: Usage | None = None
        self._block: str | None = None
        self._index = -1
        self._tools: dict[int, dict[str, str]] = {}
        self._texts: list[str] = []

    def _frame(self, name: str, payload: dict) -> str:
        return sse_event(name, payload)

    def _close_block(self) -> list[str]:
        if self._block is None:
            return []
        frames = [
            self._frame(
                "content_block_stop",
                {"type": "content_block_stop", "index": self._index},
            )
        ]
        self._block = None
        return frames

    def _open_block(self, kind: str, block: dict) -> list[str]:
        frames = self._close_block()
        self._index += 1
        self._block = kind
        frames.append(
            self._frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": self._index,
                    "content_block": block,
                },
            )
        )
        return frames

    def _delta(self, delta: dict) -> str:
        return self._frame(
            "content_block_delta",
            {"type": "content_block_delta", "index": self._index, "delta": delta},
        )

    def _flush_tool(self, index: int) -> list[str]:
        state = self._tools.pop(index, None)
        if not state or not state["name"]:
            return []
        frames = self._open_block(
            "tool",
            {
                "type": "tool_use",
                "id": state["id"],
                "name": state["name"],
                "input": {},
            },
        )
        frames.append(
            self._delta(
                {
                    "type": "input_json_delta",
                    "partial_json": state["arguments"] or "{}",
                }
            )
        )
        frames.extend(self._close_block())
        return frames

    def start_frame(self) -> str:
        return self._frame(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": self.message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": self.model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": self.prompt_estimate,
                        "output_tokens": 0,
                    },
                },
            },
        )

    def frames(self, event) -> list[str]:
        if isinstance(event, StreamStart):
            return []
        if isinstance(event, ThinkingDelta):
            frames = self._open_block(
                "thinking", {"type": "thinking", "thinking": "", "signature": ""}
            )
            frames.append(self._delta({"type": "thinking_delta", "thinking": event.text}))
            return frames
        if isinstance(event, TextDelta):
            frames = self._open_block("text", {"type": "text", "text": ""})
            frames.append(self._delta({"type": "text_delta", "text": event.text}))
            self._texts.append(event.text)
            return frames
        if isinstance(event, ToolCallStart):
            self._tools[event.index] = {
                "id": event.id,
                "name": event.name,
                "arguments": "",
            }
            return []
        if isinstance(event, ToolCallDelta):
            state = self._tools.setdefault(
                event.index, {"id": f"call_{event.index}", "name": "", "arguments": ""}
            )
            state["arguments"] += event.arguments
            return []
        if isinstance(event, ToolCallEnd):
            return self._flush_tool(event.index)
        if isinstance(event, UsageEvent):
            self.usage = event.usage
            return []
        if isinstance(event, StreamStop):
            frames = self._close_block()
            for index in list(self._tools):
                frames.extend(self._flush_tool(index))
            usage = self.usage or Usage(
                prompt_tokens=self.prompt_estimate,
                completion_tokens=estimate_text_tokens("".join(self._texts)),
                estimated=True,
            )
            frames.append(
                self._frame(
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": _STOP_REASONS[event.finish_reason],
                            "stop_sequence": None,
                        },
                        "usage": _usage_payload(usage, include_input=True),
                    },
                )
            )
            frames.append(self._frame("message_stop", {"type": "message_stop"}))
            return frames
        if isinstance(event, StreamError):
            return [sse_event("error", envelope("anthropic", event.code, event.message))]
        return []


@router.post(MESSAGES_ENDPOINT)
async def messages(
    request: Request, principal: Principal = Depends(scoped_principal("messages"))
):
    require_header(request, "anthropic-version")
    body = await json_body(request)
    parsed = parse_body(body)
    request_id = request.state.request_id
    parsed["request_id"] = request_id
    execution = service.begin_chat(
        principal,
        parsed["model"],
        max_tokens=parsed["max_tokens"],
        prompt_estimate=parsed["prompt_estimate"],
        request_id=request_id,
        endpoint=MESSAGES_ENDPOINT,
        stream=parsed["stream"],
        project_id=request.headers.get("x-tt-project"),
        protocol="anthropic",
    )
    factory = lambda target, max_tokens: _chat_request(parsed, target, max_tokens)

    if not parsed["stream"]:
        response, _candidate, headers = await service.complete_chat(execution, factory)
        return JSONResponse(
            content={
                "id": f"msg_{request_id}",
                "type": "message",
                "role": "assistant",
                "model": execution.plan.requested,
                "content": _content_blocks(response),
                "stop_reason": _STOP_REASONS[response.finish_reason],
                "stop_sequence": None,
                "usage": _usage_payload(response.usage, include_input=True),
            },
            headers=headers,
        )

    generator = service.stream_chat(execution, factory)
    try:
        first = await generator.__anext__()
    except StopAsyncIteration:
        raise GatewayError.of("UPSTREAM_ERROR", "上游没有返回任何事件") from None
    encoder = _StreamEncoder(execution)

    async def body_stream():
        try:
            yield encoder.start_frame()
            for frame in encoder.frames(first):
                yield frame
            async for event in generator:
                for frame in encoder.frames(event):
                    yield frame
        finally:
            await generator.aclose()

    headers = {
        **execution.headers,
        "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        body_stream(), media_type="text/event-stream", headers=headers
    )


def _tool_tokens(tools: tuple[ToolSpec, ...]) -> int:
    total = 0
    for tool in tools:
        total += 4
        total += estimate_text_tokens(tool.name) + estimate_text_tokens(tool.description)
        total += estimate_text_tokens(json.dumps(dict(tool.parameters), ensure_ascii=False))
    return total


@router.post(COUNT_TOKENS_ENDPOINT)
async def count_tokens(
    request: Request, principal: Principal = Depends(scoped_principal("messages"))
):
    require_header(request, "anthropic-version")
    body = await json_body(request)
    resolution = resolve_model(principal, body.get("model"))
    messages = _parse_messages(body.get("messages"))
    system = _parse_system(body.get("system"))
    tools = _parse_tools(body.get("tools"))
    tokens = estimate_prompt_tokens(messages, system) + _tool_tokens(tools)
    return JSONResponse(
        {"input_tokens": tokens},
        headers={
            "X-TT-Estimated": "1",
            "X-TT-Request-Id": request.state.request_id,
            "X-TT-Model": resolution.model,
        },
    )
