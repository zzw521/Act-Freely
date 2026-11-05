"""OpenAI 兼容协议面：/v1/chat/completions、/v1/embeddings、/v1/models。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..adapters.base import (
    ChatMessage,
    ChatRequest,
    EmbeddingRequest,
    ImagePart,
    NamedToolChoice,
    TextDelta,
    TextPart,
    ThinkingDelta,
    ThinkingPart,
    ToolCallDelta,
    ToolCallStart,
    Usage,
    UsageEvent,
    StreamError,
    StreamStart,
    StreamStop,
)
from ..auth import Principal
from ..errors import GatewayError, envelope
from ..routing import allowed_models, model_visible
from ..state import policy_store, service
from ..usage_store import estimate_prompt_tokens, estimate_text_tokens
from .common import current_principal, json_body, scoped_principal, sse

router = APIRouter(tags=["openai"])

CHAT_ENDPOINT = "/v1/chat/completions"
EMBEDDINGS_ENDPOINT = "/v1/embeddings"


def _invalid(message: str, param: str | None = None) -> GatewayError:
    return GatewayError.of("INVALID_REQUEST", message, param=param)


def _parse_content(raw: Any, role: str):
    if raw is None:
        if role == "assistant":
            return None
        raise _invalid(f"{role} 消息必须带 content", "messages")
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, list):
        raise _invalid("content 必须是字符串或 part 数组", "messages")
    parts = []
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid("content part 必须是对象", "messages")
        kind = item.get("type")
        if kind == "text":
            text = item.get("text")
            if not isinstance(text, str):
                raise _invalid("text part 缺少 text", "messages")
            parts.append(TextPart(text=text))
        elif kind == "image_url":
            image = item.get("image_url")
            if isinstance(image, str):
                url, detail = image, None
            elif isinstance(image, dict):
                url, detail = image.get("url"), image.get("detail")
            else:
                raise _invalid("image_url part 格式错误", "messages")
            if not isinstance(url, str) or not url:
                raise _invalid("image_url 缺少 url", "messages")
            parts.append(ImagePart(url=url, detail=detail))
        else:
            raise _invalid(f"不支持的 content part：{kind}", "messages")
    if not parts:
        raise _invalid("content part 数组不能为空", "messages")
    return tuple(parts)


def _parse_tool_calls(raw: Any):
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _invalid("tool_calls 必须是数组", "messages")
    calls = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or item.get("type", "function") != "function":
            raise _invalid("tool_call 必须是 function 类型", "messages")
        function = item.get("function")
        if not isinstance(function, dict):
            raise _invalid("tool_call 缺少 function", "messages")
        name = function.get("name")
        arguments = function.get("arguments", "")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False)
        if not isinstance(name, str) or not isinstance(arguments, str):
            raise _invalid("tool_call name/arguments 不合法", "messages")
        calls.append(
            {
                "id": str(item.get("id") or f"call_{index}"),
                "name": name,
                "arguments": arguments,
            }
        )
    from ..adapters.base import ToolCall

    return tuple(
        ToolCall(id=item["id"], name=item["name"], arguments=item["arguments"])
        for item in calls
    )


def _parse_messages(raw: Any) -> tuple[ChatMessage, ...]:
    if not isinstance(raw, list) or not raw:
        raise _invalid("messages 必须是非空数组", "messages")
    messages = []
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid("message 必须是对象", "messages")
        role = str(item.get("role") or "").strip().lower()
        if role == "developer":
            role = "system"
        if role not in ("system", "user", "assistant", "tool"):
            raise _invalid(f"不支持的 role：{item.get('role')}", "messages")
        content = _parse_content(item.get("content"), role)
        tool_calls = _parse_tool_calls(item.get("tool_calls"))
        if role == "assistant" and tool_calls and content not in (None, ""):
            raise _invalid("assistant 工具调用消息的 content 必须为空", "messages")
        tool_call_id = item.get("tool_call_id")
        if role == "tool":
            if not isinstance(content, str):
                raise _invalid("tool 消息必须带字符串 content", "messages")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise _invalid("tool 消息必须带 tool_call_id", "messages")
        elif tool_call_id is not None:
            raise _invalid("只有 tool 消息可以带 tool_call_id", "messages")
        name = item.get("name")
        if name is not None and not isinstance(name, str):
            raise _invalid("message name 必须是字符串", "messages")
        messages.append(
            ChatMessage(
                role=role,
                content=content,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id if role == "tool" else None,
                name=name,
            )
        )
    return tuple(messages)


def _parse_tools(raw: Any):
    from ..adapters.base import ToolSpec

    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _invalid("tools 必须是数组", "tools")
    specs = []
    for item in raw:
        if not isinstance(item, dict) or item.get("type") != "function":
            raise _invalid("tools[] 只支持 function 类型", "tools")
        function = item.get("function")
        if not isinstance(function, dict):
            raise _invalid("tools[] 缺少 function", "tools")
        name = function.get("name")
        description = function.get("description") or ""
        parameters = function.get("parameters") or {
            "type": "object",
            "properties": {},
        }
        if not isinstance(name, str) or not isinstance(description, str):
            raise _invalid("tools[] name/description 不合法", "tools")
        if not isinstance(parameters, dict):
            raise _invalid("tools[] parameters 必须是 JSON Schema 对象", "tools")
        specs.append(
            ToolSpec(name=name, description=description, parameters=parameters)
        )
    return tuple(specs)


def _parse_tool_choice(raw: Any, tools):
    if raw is None:
        return "auto"
    if isinstance(raw, str):
        if raw not in ("auto", "none", "required"):
            raise _invalid("tool_choice 只支持 auto/none/required", "tool_choice")
        return raw
    if isinstance(raw, dict) and raw.get("type") == "function":
        function = raw.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name:
            return NamedToolChoice(name)
    raise _invalid("tool_choice 格式错误", "tool_choice")


def _parse_stop(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        if not raw:
            raise _invalid("stop 不能为空字符串", "stop")
        return (raw,)
    if isinstance(raw, list) and all(isinstance(item, str) and item for item in raw):
        return tuple(raw)
    raise _invalid("stop 必须是字符串或字符串数组", "stop")


def _parse_limit(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise _invalid("max_tokens 必须是正整数", "max_tokens")
    return raw


def parse_chat_body(body: dict) -> dict:
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise _invalid("缺少 model 参数", "model")
    messages = _parse_messages(body.get("messages"))
    tools = _parse_tools(body.get("tools"))
    tool_choice = _parse_tool_choice(body.get("tool_choice"), tools)
    if isinstance(tool_choice, NamedToolChoice) and tool_choice.name not in {
        tool.name for tool in tools
    }:
        raise _invalid("tool_choice 指向了未声明的工具", "tool_choice")
    if tool_choice == "required" and not tools:
        raise _invalid("没有 tools 时不能使用 tool_choice=required", "tool_choice")
    if body.get("n") not in (None, 1):
        raise _invalid("n 只支持 1", "n")
    stream = body.get("stream", False)
    if not isinstance(stream, bool):
        raise _invalid("stream 必须是布尔值", "stream")
    stream_options = body.get("stream_options")
    if stream_options is not None and not isinstance(stream_options, dict):
        raise _invalid("stream_options 必须是对象", "stream_options")
    include_usage = bool((stream_options or {}).get("include_usage"))
    temperature = body.get("temperature")
    if temperature is not None and (
        isinstance(temperature, bool) or not isinstance(temperature, (int, float))
    ):
        raise _invalid("temperature 必须是数字", "temperature")
    top_p = body.get("top_p")
    if top_p is not None and (
        isinstance(top_p, bool) or not isinstance(top_p, (int, float))
    ):
        raise _invalid("top_p 必须是数字", "top_p")
    response_format = body.get("response_format")
    if response_format is not None:
        if not isinstance(response_format, dict):
            raise _invalid("response_format 必须是对象", "response_format")
        kind = response_format.get("type")
        if kind not in ("text", "json_object", "json_schema"):
            raise _invalid("response_format.type 不受支持", "response_format")
    user = body.get("user")
    if user is not None and not isinstance(user, str):
        raise _invalid("user 必须是字符串", "user")

    return {
        "model": model.strip(),
        "messages": messages,
        "tools": tools,
        "tool_choice": tool_choice,
        "max_tokens": _parse_limit(
            body.get("max_tokens", body.get("max_completion_tokens"))
        ),
        "temperature": float(temperature) if temperature is not None else None,
        "top_p": float(top_p) if top_p is not None else None,
        "stop": _parse_stop(body.get("stop")),
        "stream": stream,
        "include_usage": include_usage,
        "response_format": response_format,
        "user": user,
        "prompt_estimate": estimate_prompt_tokens(messages),
    }


def _chat_request(parsed: dict, target, max_tokens: int) -> ChatRequest:
    return ChatRequest(
        target=target,
        messages=parsed["messages"],
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


def _finish_reason(value: str) -> str:
    return "stop" if value == "error" else value


def _usage_payload(usage: Usage) -> dict:
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "prompt_tokens_details": {"cached_tokens": usage.cached_tokens},
        "completion_tokens_details": {"reasoning_tokens": usage.reasoning_tokens},
    }


def _completion_payload(execution, response, request_id: str) -> dict:
    tools = response.tool_calls
    text = response.text
    thinking = "".join(
        part.text for part in response.parts if isinstance(part, ThinkingPart)
    )
    message: dict[str, Any] = {
        "role": "assistant",
        "content": text if (text or not tools) else None,
    }
    if thinking:
        message["reasoning_content"] = thinking
    if tools:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in tools
        ]
    return {
        "id": response.response_id or f"chatcmpl-{request_id}",
        "object": "chat.completion",
        "created": execution.started_ms // 1000,
        "model": execution.plan.requested,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": _finish_reason(response.finish_reason),
            }
        ],
        "usage": _usage_payload(response.usage),
    }


class _StreamFrames:
    def __init__(self, execution, include_usage: bool) -> None:
        self.execution = execution
        self.include_usage = include_usage
        self.created = execution.started_ms // 1000
        self.model = execution.plan.requested
        self.response_id = f"chatcmpl-{execution.request_id}"
        self.usage: Usage | None = None

    def chunk(self, delta: dict, finish: str | None = None) -> str:
        return sse(
            {
                "id": self.response_id,
                "object": "chat.completion.chunk",
                "created": self.created,
                "model": self.model,
                "choices": [
                    {"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}
                ],
            }
        )

    def frames(self, event) -> list[str]:
        if isinstance(event, StreamStart):
            if event.response_id:
                self.response_id = event.response_id
            return [self.chunk({"role": "assistant", "content": ""})]
        if isinstance(event, ThinkingDelta):
            return [self.chunk({"reasoning_content": event.text})]
        if isinstance(event, TextDelta):
            return [self.chunk({"content": event.text})]
        if isinstance(event, ToolCallStart):
            return [
                self.chunk(
                    {
                        "tool_calls": [
                            {
                                "index": event.index,
                                "id": event.id,
                                "type": "function",
                                "function": {"name": event.name, "arguments": ""},
                            }
                        ]
                    }
                )
            ]
        if isinstance(event, ToolCallDelta):
            return [
                self.chunk(
                    {
                        "tool_calls": [
                            {
                                "index": event.index,
                                "function": {"arguments": event.arguments},
                            }
                        ]
                    }
                )
            ]
        if isinstance(event, UsageEvent):
            self.usage = event.usage
            return []
        if isinstance(event, StreamStop):
            frames = [
                self.chunk({}, _finish_reason(event.finish_reason)),
            ]
            if self.include_usage and self.usage is not None:
                frames.append(
                    sse(
                        {
                            "id": self.response_id,
                            "object": "chat.completion.chunk",
                            "created": self.created,
                            "model": self.model,
                            "choices": [],
                            "usage": _usage_payload(self.usage),
                        }
                    )
                )
            frames.append(sse("[DONE]"))
            return frames
        if isinstance(event, StreamError):
            return [sse(envelope("openai", event.code, event.message))]
        return []


@router.post(CHAT_ENDPOINT)
async def chat_completions(
    request: Request, principal: Principal = Depends(scoped_principal("chat"))
):
    body = await json_body(request)
    parsed = parse_chat_body(body)
    request_id = request.state.request_id
    parsed["request_id"] = request_id
    execution = service.begin_chat(
        principal,
        parsed["model"],
        max_tokens=parsed["max_tokens"],
        prompt_estimate=parsed["prompt_estimate"],
        request_id=request_id,
        endpoint=CHAT_ENDPOINT,
        stream=parsed["stream"],
        project_id=request.headers.get("x-tt-project"),
    )
    factory = lambda target, max_tokens: _chat_request(parsed, target, max_tokens)

    if not parsed["stream"]:
        response, _candidate, headers = await service.complete_chat(execution, factory)
        return JSONResponse(
            content=_completion_payload(execution, response, request_id),
            headers=headers,
        )

    generator = service.stream_chat(execution, factory)
    try:
        first = await generator.__anext__()
    except StopAsyncIteration:
        raise GatewayError.of("UPSTREAM_ERROR", "上游没有返回任何事件") from None
    frames = _StreamFrames(execution, parsed["include_usage"])

    async def body_stream():
        try:
            for frame in frames.frames(first):
                yield frame
            async for event in generator:
                for frame in frames.frames(event):
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


def _parse_embedding_input(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str) and raw.strip():
        return (raw,)
    if isinstance(raw, list) and raw and all(
        isinstance(item, str) and item.strip() for item in raw
    ):
        return tuple(raw)
    raise _invalid("input 必须是字符串或字符串数组", "input")


@router.post(EMBEDDINGS_ENDPOINT)
async def embeddings(
    request: Request, principal: Principal = Depends(scoped_principal("embeddings"))
):
    body = await json_body(request)
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise _invalid("缺少 model 参数", "model")
    inputs = _parse_embedding_input(body.get("input"))
    encoding = body.get("encoding_format", "float")
    if encoding not in ("float", "base64"):
        raise _invalid("encoding_format 只支持 float/base64", "encoding_format")
    if encoding == "base64":
        raise _invalid("encoding_format=base64 尚未支持", "encoding_format")
    request_id = request.state.request_id
    execution = service.begin_embedding(
        principal,
        model.strip(),
        prompt_estimate=sum(estimate_text_tokens(item) for item in inputs),
        request_id=request_id,
        endpoint=EMBEDDINGS_ENDPOINT,
        project_id=request.headers.get("x-tt-project"),
    )
    user = body.get("user")
    response, _candidate, headers = await service.embed(
        execution,
        lambda target: EmbeddingRequest(
            target=target,
            inputs=inputs,
            user=user if isinstance(user, str) else None,
            request_id=request_id,
        ),
    )
    return JSONResponse(
        content={
            "object": "list",
            "data": [
                {"object": "embedding", "index": index, "embedding": list(vector)}
                for index, vector in enumerate(response.vectors)
            ],
            "model": execution.plan.requested,
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "total_tokens": response.usage.prompt_tokens,
            },
        },
        headers=headers,
    )


@router.get("/v1/models")
async def list_models(
    request: Request, principal: Principal = Depends(scoped_principal("models"))
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
            capabilities = list(models[chain[0]].get("capabilities") or [])
            data.append(
                {
                    "id": name,
                    "object": "model",
                    "owned_by": "tongtian",
                    "tt": {"alias": True, "chain": chain, "capabilities": capabilities},
                }
            )
        elif name in models and "chat" in (models[name].get("capabilities") or []):
            data.append(
                {
                    "id": name,
                    "object": "model",
                    "owned_by": "tongtian",
                    "tt": {
                        "alias": False,
                        "capabilities": list(models[name].get("capabilities") or []),
                    },
                }
            )
    headers = {} if not snapshot.stale else {"X-TT-Policy-Stale": "1"}
    return JSONResponse(content={"object": "list", "data": data}, headers=headers)
