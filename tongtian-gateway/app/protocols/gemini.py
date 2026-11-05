"""Gemini 协议面：generateContent / streamGenerateContent / countTokens / models。

Gemini 的 `contents[].parts` 与内部消息差异较大：工具调用没有 id，函数结果以
`functionResponse` 的形式混在 user 回合里，思考内容用 `thought: true` 的文本
part 表示。这里统一翻译：函数调用按出现顺序分配 `call_<n>` 作为内部 id，
回放的 `functionResponse` 按名字匹配回该 id。
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
from ..routing import allowed_models, model_visible
from ..state import policy_store, service
from ..usage_store import estimate_prompt_tokens, estimate_text_tokens
from .common import current_principal, json_body, resolve_model, scoped_principal, sse

logger = logging.getLogger("tongtian.gateway.gemini")

router = APIRouter(tags=["gemini"])

GENERATE_ENDPOINT = "/v1beta/models/{model}:generateContent"
STREAM_ENDPOINT = "/v1beta/models/{model}:streamGenerateContent"
COUNT_TOKENS_ENDPOINT = "/v1beta/models/{model}:countTokens"
MODELS_ENDPOINT = "/v1beta/models"

# 单张图片解码后的上限；更大的请求直接 413，避免把内存打满
MAX_IMAGE_BYTES = 20 * 1024 * 1024

_FINISH_REASONS = {
    "stop": "STOP",
    "length": "MAX_TOKENS",
    "tool_calls": "STOP",
    "content_filter": "SAFETY",
    "error": "OTHER",
}

_METHODS = ["generateContent", "streamGenerateContent", "countTokens"]


def _invalid(message: str, param: str | None = None) -> GatewayError:
    return GatewayError.of("INVALID_REQUEST", message, param=param)


def _message(**kwargs) -> ChatMessage:
    try:
        return ChatMessage(**kwargs)
    except ValueError as exc:
        raise _invalid(str(exc), "contents") from None


def _model_name(raw: str) -> str:
    name = (raw or "").strip()
    if name.startswith("models/"):
        name = name[len("models/") :]
    if not name:
        raise _invalid("缺少模型名", "model")
    return name


def _lower_schema(value: Any) -> Any:
    """Gemini 的 Schema 用大写类型名，转成 OpenAI JSON Schema 的小写。"""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "type" and isinstance(item, str):
                result[key] = item.lower()
            elif key == "properties" and isinstance(item, dict):
                result[key] = {name: _lower_schema(spec) for name, spec in item.items()}
            elif key in ("items", "additionalProperties") and isinstance(item, dict):
                result[key] = _lower_schema(item)
            elif key in ("anyOf", "allOf", "oneOf") and isinstance(item, list):
                result[key] = [_lower_schema(spec) for spec in item]
            else:
                result[key] = item
        if "type" not in result and "properties" in result:
            result["type"] = "object"
        return result
    if isinstance(value, list):
        return [_lower_schema(item) for item in value]
    return value


def _image_from_inline(part: dict) -> ImagePart:
    inline = part.get("inlineData")
    if not isinstance(inline, dict):
        raise _invalid("inlineData 必须是对象", "contents")
    media_type = inline.get("mimeType")
    data = inline.get("data")
    if not isinstance(media_type, str) or not isinstance(data, str):
        raise _invalid("inlineData 缺少 mimeType/data", "contents")
    try:
        raw = base64.b64decode("".join(data.split()), validate=True)
    except (ValueError, TypeError) as exc:
        raise _invalid("inlineData.data 不是合法 base64", "contents") from exc
    if len(raw) > MAX_IMAGE_BYTES:
        raise GatewayError.of(
            "PAYLOAD_TOO_LARGE",
            f"单张图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB 上限",
        )
    try:
        return ImagePart(data=raw, media_type=media_type)
    except ValueError as exc:
        raise _invalid(str(exc), "contents") from None


def _image_from_file(part: dict) -> ImagePart:
    file_data = part.get("fileData")
    if not isinstance(file_data, dict):
        raise _invalid("fileData 必须是对象", "contents")
    uri = file_data.get("fileUri")
    media_type = file_data.get("mimeType")
    if not isinstance(uri, str) or not isinstance(media_type, str):
        raise _invalid("fileData 缺少 fileUri/mimeType", "contents")
    try:
        return ImagePart(url=uri, media_type=media_type)
    except ValueError as exc:
        raise _invalid(str(exc), "contents") from None


def _function_call(part: dict, state: "_ParseState") -> ToolCall:
    call = part.get("functionCall")
    if not isinstance(call, dict):
        raise _invalid("functionCall 必须是对象", "contents")
    name = call.get("name")
    if not isinstance(name, str) or not name:
        raise _invalid("functionCall 缺少 name", "contents")
    args = call.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise _invalid("functionCall.args 必须是 JSON 对象", "contents")
    call_id = state.next_call_id(name)
    try:
        return ToolCall(id=call_id, name=name, arguments=json.dumps(args, ensure_ascii=False))
    except ValueError as exc:
        raise _invalid(str(exc), "contents") from None


def _function_response(part: dict, state: "_ParseState") -> ChatMessage:
    result = part.get("functionResponse")
    if not isinstance(result, dict):
        raise _invalid("functionResponse 必须是对象", "contents")
    name = result.get("name")
    if not isinstance(name, str) or not name:
        raise _invalid("functionResponse 缺少 name", "contents")
    payload = result.get("response")
    if isinstance(payload, str):
        text = payload
    elif isinstance(payload, dict):
        text = json.dumps(payload, ensure_ascii=False)
    elif payload is None:
        text = ""
    else:
        raise _invalid("functionResponse.response 必须是对象或字符串", "contents")
    call_id = result.get("id")
    if not isinstance(call_id, str) or not call_id:
        call_id = state.resolve_call_id(name)
    return _message(role="tool", content=text, tool_call_id=call_id)


class _ParseState:
    """按顺序给函数调用分配 id，并把 functionResponse 配回最近的同名调用。"""

    def __init__(self) -> None:
        self._counter = 0
        self._pending: dict[str, list[str]] = {}

    def next_call_id(self, name: str) -> str:
        self._counter += 1
        call_id = f"call_{self._counter}"
        self._pending.setdefault(name, []).append(call_id)
        return call_id

    def resolve_call_id(self, name: str) -> str:
        queue = self._pending.get(name)
        if queue:
            return queue.pop(0)
        self._counter += 1
        return f"call_{self._counter}"


def _content_parts(content: dict, state: "_ParseState") -> list[tuple[str, Any]]:
    parts = content.get("parts")
    if not isinstance(parts, list) or not parts:
        raise _invalid("content.parts 必须是非空数组", "contents")
    parsed: list[tuple[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            raise _invalid("part 必须是对象", "contents")
        if "functionResponse" in part:
            parsed.append(("tool", _function_response(part, state)))
        elif "functionCall" in part:
            parsed.append(("call", _function_call(part, state)))
        elif "inlineData" in part:
            parsed.append(("image", _image_from_inline(part)))
        elif "fileData" in part:
            parsed.append(("image", _image_from_file(part)))
        elif "text" in part:
            text = part.get("text")
            if not isinstance(text, str):
                raise _invalid("text part 缺少 text", "contents")
            if part.get("thought"):
                # 回放的历史思考内容，内部没有对应字段
                continue
            parsed.append(("text", text))
        else:
            raise _invalid(
                "part 只支持 text/inlineData/fileData/functionCall/functionResponse",
                "contents",
            )
    return parsed


def _user_content(content: dict, state: "_ParseState") -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    parts: list[TextPart | ImagePart] = []
    texts: list[str] = []

    def flush() -> None:
        if parts:
            messages.append(_message(role="user", content=tuple(parts)))
            parts.clear()
        elif texts:
            messages.append(_message(role="user", content="".join(texts)))
            texts.clear()

    for kind, value in _content_parts(content, state):
        if kind == "tool":
            flush()
            messages.append(value)
        elif kind == "image":
            parts.append(value)
        elif kind == "text":
            if parts:
                parts.append(TextPart(text=value))
            else:
                texts.append(value)
        else:
            raise _invalid("user 回合不能包含 functionCall", "contents")
    flush()
    return messages


def _model_content(content: dict, state: "_ParseState") -> list[ChatMessage]:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for kind, value in _content_parts(content, state):
        if kind == "text":
            texts.append(value)
        elif kind == "call":
            calls.append(value)
        else:
            raise _invalid("model 回合不能包含 functionResponse/图片", "contents")
    messages: list[ChatMessage] = []
    text = "".join(texts)
    if text:
        messages.append(_message(role="assistant", content=text))
    if calls:
        messages.append(_message(role="assistant", content=None, tool_calls=tuple(calls)))
    return messages


def _parse_contents(raw: Any, state: _ParseState) -> tuple[ChatMessage, ...]:
    if not isinstance(raw, list) or not raw:
        raise _invalid("contents 必须是非空数组", "contents")
    messages: list[ChatMessage] = []
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid("content 必须是对象", "contents")
        role = str(item.get("role") or "").strip().lower()
        if role in ("user", ""):
            messages.extend(_user_content(item, state))
        elif role in ("model", "assistant"):
            messages.extend(_model_content(item, state))
        else:
            raise _invalid(f"不支持的 role：{item.get('role')}", "contents")
    if not messages:
        raise _invalid("contents 不能为空", "contents")
    return tuple(messages)


def _parse_system(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        if not raw.strip():
            raise _invalid("systemInstruction 不能为空", "systemInstruction")
        return raw
    if not isinstance(raw, dict):
        raise _invalid("systemInstruction 必须是对象", "systemInstruction")
    parts = raw.get("parts")
    if not isinstance(parts, list) or not parts:
        raise _invalid("systemInstruction.parts 必须是非空数组", "systemInstruction")
    texts = []
    for part in parts:
        if not isinstance(part, dict) or not isinstance(part.get("text"), str):
            raise _invalid("systemInstruction 只支持 text part", "systemInstruction")
        texts.append(part["text"])
    joined = "".join(texts)
    if not joined.strip():
        raise _invalid("systemInstruction 不能为空", "systemInstruction")
    return joined


def _parse_tools(raw: Any) -> tuple[ToolSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _invalid("tools 必须是数组", "tools")
    specs: list[ToolSpec] = []
    for group in raw:
        if not isinstance(group, dict):
            raise _invalid("tools[] 必须是对象", "tools")
        declarations = group.get("functionDeclarations")
        if declarations is None:
            # googleSearch 等厂商原生工具没有 OpenAI 对应物，忽略
            logger.debug("忽略不含 functionDeclarations 的工具组")
            continue
        if not isinstance(declarations, list):
            raise _invalid("functionDeclarations 必须是数组", "tools")
        for item in declarations:
            if not isinstance(item, dict):
                raise _invalid("functionDeclaration 必须是对象", "tools")
            name = item.get("name")
            description = item.get("description") or ""
            parameters = item.get("parameters")
            if parameters is None:
                parameters = {"type": "object", "properties": {}}
            if not isinstance(name, str) or not name:
                raise _invalid("functionDeclaration 缺少 name", "tools")
            if not isinstance(description, str):
                raise _invalid("functionDeclaration description 必须是字符串", "tools")
            if not isinstance(parameters, dict):
                raise _invalid("functionDeclaration parameters 必须是对象", "tools")
            try:
                specs.append(
                    ToolSpec(
                        name=name,
                        description=description,
                        parameters=_lower_schema(parameters),
                    )
                )
            except ValueError as exc:
                raise _invalid(str(exc), "tools") from None
    return tuple(specs)


def _parse_tool_choice(raw: Any, tools: tuple[ToolSpec, ...]):
    if raw is None:
        return "auto"
    if not isinstance(raw, dict):
        raise _invalid("toolConfig 必须是对象", "toolConfig")
    config = raw.get("functionCallingConfig")
    if config is None:
        return "auto"
    if not isinstance(config, dict):
        raise _invalid("functionCallingConfig 必须是对象", "toolConfig")
    mode = config.get("mode")
    if mode in (None, "AUTO"):
        return "auto"
    if mode == "NONE":
        return "none"
    if mode == "ANY":
        if not tools:
            raise _invalid("没有 tools 时不能使用 mode=ANY", "toolConfig")
        names = config.get("allowedFunctionNames")
        if isinstance(names, list) and len(names) == 1 and names[0] in {
            tool.name for tool in tools
        }:
            return NamedToolChoice(names[0])
        return "required"
    raise _invalid(f"不支持的 functionCallingConfig.mode：{mode}", "toolConfig")


def _parse_number(raw: Any, name: str, low: float, high: float) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _invalid(f"{name} 必须是数字", "generationConfig")
    value = float(raw)
    if not low <= value <= high:
        raise _invalid(f"{name} 必须在 [{low}, {high}]", "generationConfig")
    return value


def _parse_generation(raw: Any) -> dict:
    empty = {
        "max_tokens": None,
        "temperature": None,
        "top_p": None,
        "stop": (),
        "response_format": None,
    }
    if raw is None:
        return empty
    if not isinstance(raw, dict):
        raise _invalid("generationConfig 必须是对象", "generationConfig")
    candidate_count = raw.get("candidateCount")
    if candidate_count is not None and candidate_count != 1:
        raise _invalid("candidateCount 只支持 1", "generationConfig")
    max_tokens = raw.get("maxOutputTokens")
    if max_tokens is not None and (
        isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0
    ):
        raise _invalid("maxOutputTokens 必须是正整数", "generationConfig")
    stop = raw.get("stopSequences")
    if stop is not None and (
        not isinstance(stop, list)
        or not all(isinstance(item, str) and item for item in stop)
    ):
        raise _invalid("stopSequences 必须是非空字符串数组", "generationConfig")
    if raw.get("topK") is not None:
        logger.debug("丢弃 generationConfig.topK：内部没有对应字段")
    mime = raw.get("responseMimeType")
    response_format = None
    if mime == "application/json":
        schema = raw.get("responseSchema")
        if schema is None:
            response_format = {"type": "json_object"}
        elif isinstance(schema, dict):
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": _lower_schema(schema)},
            }
        else:
            raise _invalid("responseSchema 必须是对象", "generationConfig")
    elif mime not in (None, "text/plain"):
        raise _invalid("responseMimeType 只支持 text/plain 或 application/json", "generationConfig")
    return {
        "max_tokens": max_tokens,
        "temperature": _parse_number(raw.get("temperature"), "temperature", 0, 2),
        "top_p": _parse_number(raw.get("topP"), "topP", 0, 1),
        "stop": tuple(stop or ()),
        "response_format": response_format,
    }


def parse_payload(body: dict, model: str) -> dict:
    name = _model_name(model)
    state = _ParseState()
    messages = _parse_contents(body.get("contents"), state)
    system = _parse_system(body.get("systemInstruction"))
    tools = _parse_tools(body.get("tools"))
    tool_choice = _parse_tool_choice(body.get("toolConfig"), tools)
    generation = _parse_generation(body.get("generationConfig"))
    if body.get("safetySettings") is not None:
        logger.debug("丢弃 safetySettings：上游安全策略由 provider 决定")
    if body.get("cachedContent") is not None:
        logger.debug("丢弃 cachedContent：M1 不代理显式缓存")
    return {
        "model": name,
        "messages": messages,
        "system": system,
        "tools": tools,
        "tool_choice": tool_choice,
        "max_tokens": generation["max_tokens"],
        "temperature": generation["temperature"],
        "top_p": generation["top_p"],
        "stop": generation["stop"],
        "stream": False,
        "user": None,
        "response_format": generation["response_format"],
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
            response_format=parsed["response_format"],
        )
    except ValueError as exc:
        raise _invalid(str(exc)) from None


def _tool_args(arguments: str) -> dict:
    try:
        value = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _response_parts(response) -> list[dict]:
    parts: list[dict] = []
    for part in response.parts:
        if isinstance(part, ThinkingPart):
            parts.append({"text": part.text, "thought": True})
        elif isinstance(part, TextPart):
            parts.append({"text": part.text})
        elif isinstance(part, ToolCall):
            parts.append(
                {"functionCall": {"name": part.name, "args": _tool_args(part.arguments)}}
            )
    return parts or [{"text": ""}]


def _usage_metadata(usage: Usage) -> dict:
    payload = {
        "promptTokenCount": usage.prompt_tokens,
        "candidatesTokenCount": usage.completion_tokens,
        "totalTokenCount": usage.total_tokens,
    }
    if usage.cached_tokens:
        payload["cachedContentTokenCount"] = usage.cached_tokens
    if usage.reasoning_tokens:
        payload["thoughtsTokenCount"] = usage.reasoning_tokens
    return payload


def _response_payload(execution, response) -> dict:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": _response_parts(response)},
                "finishReason": _FINISH_REASONS[response.finish_reason],
            }
        ],
        "usageMetadata": _usage_metadata(response.usage),
        "modelVersion": execution.plan.requested,
    }


class _StreamEncoder:
    """把内部流事件编成 Gemini 的 data-only SSE 帧。"""

    def __init__(self, execution) -> None:
        self.model = execution.plan.requested
        self.prompt_estimate = max(1, execution.prompt_estimate)
        self.usage: Usage | None = None
        self._tools: dict[int, dict[str, str]] = {}
        self._texts: list[str] = []

    def _chunk(self, parts: list[dict], finish: str | None = None) -> str:
        candidate: dict[str, Any] = {"content": {"role": "model", "parts": parts}}
        if finish is not None:
            candidate["finishReason"] = finish
        return sse({"candidates": [candidate]})

    def _tool_frame(self, state: dict[str, str] | None) -> list[str]:
        if not state or not state["name"]:
            return []
        args = _tool_args(state["arguments"])
        return [self._chunk([{"functionCall": {"name": state["name"], "args": args}}])]

    def start_frame(self) -> str:
        return sse(
            {
                "candidates": [{"content": {"role": "model", "parts": []}}],
                "modelVersion": self.model,
            }
        )

    def frames(self, event) -> list[str]:
        if isinstance(event, StreamStart):
            return []
        if isinstance(event, ThinkingDelta):
            return [self._chunk([{"text": event.text, "thought": True}])]
        if isinstance(event, TextDelta):
            self._texts.append(event.text)
            return [self._chunk([{"text": event.text}])]
        if isinstance(event, ToolCallStart):
            self._tools[event.index] = {"name": event.name, "arguments": ""}
            return []
        if isinstance(event, ToolCallDelta):
            state = self._tools.setdefault(
                event.index, {"name": "", "arguments": ""}
            )
            state["arguments"] += event.arguments
            return []
        if isinstance(event, ToolCallEnd):
            return self._tool_frame(self._tools.pop(event.index, None))
        if isinstance(event, UsageEvent):
            self.usage = event.usage
            return []
        if isinstance(event, StreamStop):
            frames: list[str] = []
            for index in sorted(self._tools):
                frames.extend(self._tool_frame(self._tools[index]))
            self._tools.clear()
            usage = self.usage or Usage(
                prompt_tokens=self.prompt_estimate,
                completion_tokens=estimate_text_tokens("".join(self._texts)),
                estimated=True,
            )
            final = sse(
                {
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": []},
                            "finishReason": _FINISH_REASONS[event.finish_reason],
                        }
                    ],
                    "usageMetadata": _usage_metadata(usage),
                }
            )
            frames.append(final)
            return frames
        if isinstance(event, StreamError):
            return [sse(envelope("gemini", event.code, event.message))]
        return []


@router.post(GENERATE_ENDPOINT)
async def generate_content(
    model: str, request: Request, principal: Principal = Depends(scoped_principal("gemini"))
):
    body = await json_body(request)
    parsed = parse_payload(body, model)
    request_id = request.state.request_id
    parsed["request_id"] = request_id
    execution = service.begin_chat(
        principal,
        parsed["model"],
        max_tokens=parsed["max_tokens"],
        prompt_estimate=parsed["prompt_estimate"],
        request_id=request_id,
        endpoint="/v1beta/models/{model}:generateContent",
        stream=False,
        project_id=request.headers.get("x-tt-project"),
        protocol="gemini",
    )
    factory = lambda target, max_tokens: _chat_request(parsed, target, max_tokens)
    response, _candidate, headers = await service.complete_chat(execution, factory)
    return JSONResponse(content=_response_payload(execution, response), headers=headers)


@router.post(STREAM_ENDPOINT)
async def stream_generate_content(
    model: str, request: Request, principal: Principal = Depends(scoped_principal("gemini"))
):
    body = await json_body(request)
    parsed = parse_payload(body, model)
    request_id = request.state.request_id
    parsed["request_id"] = request_id
    parsed["stream"] = True
    execution = service.begin_chat(
        principal,
        parsed["model"],
        max_tokens=parsed["max_tokens"],
        prompt_estimate=parsed["prompt_estimate"],
        request_id=request_id,
        endpoint="/v1beta/models/{model}:streamGenerateContent",
        stream=True,
        project_id=request.headers.get("x-tt-project"),
        protocol="gemini",
    )
    factory = lambda target, max_tokens: _chat_request(parsed, target, max_tokens)

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
    model: str, request: Request, principal: Principal = Depends(scoped_principal("gemini"))
):
    body = await json_body(request)
    resolution = resolve_model(principal, _model_name(model))
    state = _ParseState()
    messages = _parse_contents(body.get("contents"), state)
    system = _parse_system(body.get("systemInstruction"))
    tools = _parse_tools(body.get("tools"))
    tokens = estimate_prompt_tokens(messages, system) + _tool_tokens(tools)
    return JSONResponse(
        {"totalTokens": tokens},
        headers={
            "X-TT-Estimated": "1",
            "X-TT-Request-Id": request.state.request_id,
            "X-TT-Model": resolution.model,
        },
    )


@router.get(MODELS_ENDPOINT)
async def list_models(
    request: Request, principal: Principal = Depends(scoped_principal("gemini"))
) -> JSONResponse:
    snapshot = policy_store.snapshot(principal.tenant)
    policy = snapshot.document
    models = policy.get("models") or {}
    routes = policy.get("routes") or {}
    allowed = allowed_models(policy, principal)
    names = policy.get("allowedModels") or list(models)
    data = []
    for name in names:
        if not model_visible(principal, name):
            continue
        if name not in allowed:
            continue
        if name in routes:
            chain = [
                item
                for item in (routes[name].get("chain") or [])
                if item in models
                and item in allowed
                and "chat" in (models[item].get("capabilities") or [])
            ]
            if not chain:
                continue
            spec = models[chain[0]]
        elif name in models and "chat" in (models[name].get("capabilities") or []):
            spec = models[name]
        else:
            continue
        entry = {
            "name": f"models/{name}",
            "displayName": name,
            "description": spec.get("description") or f"通天办公模型 {name}",
            "supportedGenerationMethods": list(_METHODS),
        }
        context = spec.get("contextWindow")
        if isinstance(context, int) and context > 0:
            entry["inputTokenLimit"] = context
        output = spec.get("maxOutputTokens")
        if isinstance(output, int) and output > 0:
            entry["outputTokenLimit"] = output
        data.append(entry)
    headers = {} if not snapshot.stale else {"X-TT-Policy-Stale": "1"}
    return JSONResponse(content={"models": data}, headers=headers)
