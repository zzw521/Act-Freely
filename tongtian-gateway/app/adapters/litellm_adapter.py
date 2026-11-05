"""LiteLLM 适配器：OpenAI 兼容 / Azure / Anthropic / Gemini。

只把上游响应翻译成内部结构；协议层不接触 LiteLLM 对象。
LiteLLM 冷启动 3–4 秒，必须延迟导入；流式 usage 对 Anthropic/Gemini
必须等 `stream_chunk_builder` 才能拿到。密钥不进日志与错误消息。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any

from ..errors import ERROR_SPECS
from .base import (
    AdapterError,
    AdapterSet,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    FinishReason,
    ImagePart,
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
    sanitize_error_message,
)

_LITELLM: Any = None


def _litellm() -> Any:
    global _LITELLM
    if _LITELLM is None:
        os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
        os.environ.setdefault("LITELLM_LOG", "ERROR")
        os.environ.setdefault("LITELLM_TELEMETRY", "False")
        os.environ.setdefault("LITELLM_DONT_SHOW_FEEDBACK_BOX", "True")
        import litellm  # noqa: PLC0415 - 必须延迟导入

        litellm.suppress_debug_info = True
        litellm.set_verbose = False
        _LITELLM = litellm
    return _LITELLM


async def warm_litellm(timeout_seconds: float = 0.5) -> float:
    """把首请求的惰性导入（OpenAI SDK 资源树、离线分词器等，约 1 秒）挪到启动期。

    合成请求指向本机关闭端口（127.0.0.1:9）：不走外网、不带任何 provider 密钥，
    连接即刻被拒；异常只用来驱动代码路径，不对外抛。返回预热耗时（毫秒）。
    """
    litellm = _litellm()
    started = time.perf_counter()
    kwargs: dict[str, Any] = {
        "model": "openai/tongtian-warmup",
        "api_base": "http://127.0.0.1:9/v1",
        "api_key": "sk-warmup",
        "messages": [{"role": "user", "content": "warmup"}],
        "max_tokens": 1,
        "timeout": timeout_seconds,
        "num_retries": 0,
    }
    logger = logging.getLogger("LiteLLM")
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        for stream in (False, True):
            try:
                response = await litellm.acompletion(**kwargs, stream=stream)
                if stream:
                    async for _chunk in response:
                        break
            except Exception:  # noqa: BLE001 - 端口关闭是预期结果
                continue
    finally:
        logger.setLevel(previous_level)
    return (time.perf_counter() - started) * 1000


RETRYABLE_CODES = frozenset(
    {"UPSTREAM_RATE_LIMITED", "UPSTREAM_TIMEOUT", "PROVIDER_UNAVAILABLE"}
)
RETRY_BASE_SECONDS = 0.2
RETRY_MAX_SECONDS = 5.0


def _timeout(connect_seconds: float, total_seconds: float) -> Any:
    """连接超时与整次响应超时分开；上游 SDK 都接受 httpx.Timeout。"""
    try:
        import httpx  # noqa: PLC0415 - 只在真正调用上游时需要
    except ImportError:  # pragma: no cover - httpx 是必装依赖
        return total_seconds
    connect = min(float(connect_seconds), float(total_seconds))
    return httpx.Timeout(
        connect=connect, read=float(total_seconds), write=float(total_seconds), pool=connect
    )


def _retry_after(exc: Exception) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    raw = None
    if headers is not None:
        try:
            raw = headers.get("retry-after")
        except Exception:  # noqa: BLE001 - 第三方异常对象不可信
            raw = None
    if raw is None:
        raw = getattr(exc, "retry_after", None)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _retry_delay(attempt: int, error: AdapterError) -> float:
    if error.retry_after is not None:
        return min(error.retry_after, RETRY_MAX_SECONDS)
    jitter = random.uniform(0, 0.05)
    return min(RETRY_BASE_SECONDS * (2**attempt) + jitter, RETRY_MAX_SECONDS)


def _model_id(target) -> str:
    provider = target.provider
    if provider in ("azure", "azure_openai"):
        return f"azure/{target.upstream_model}"
    if provider == "anthropic":
        return f"anthropic/{target.upstream_model}"
    if provider == "gemini":
        return f"gemini/{target.upstream_model}"
    # bailian / volcengine_ark / tencent_hunyuan / openai_compatible / vllm / ollama
    return f"openai/{target.upstream_model}"


def _api_base(target) -> str | None:
    base = (target.base_url or "").rstrip("/")
    if not base:
        return None
    if target.provider == "gemini" and not base.endswith("/v1beta"):
        base = f"{base}/v1beta"
    return base


def _image_url(part: ImagePart) -> str:
    if part.url:
        return part.url
    encoded = base64.b64encode(part.data or b"").decode("ascii")
    return f"data:{part.media_type};base64,{encoded}"


def _messages(request: ChatRequest) -> list[dict]:
    messages: list[dict] = []
    if request.system:
        messages.append({"role": "system", "content": request.system})
    for message in request.messages:
        item: dict[str, Any] = {"role": message.role}
        content = message.content
        if isinstance(content, tuple):
            parts = []
            for part in content:
                if isinstance(part, TextPart):
                    parts.append({"type": "text", "text": part.text})
                elif isinstance(part, ImagePart):
                    image: dict[str, Any] = {"url": _image_url(part)}
                    if part.detail:
                        image["detail"] = part.detail
                    parts.append({"type": "image_url", "image_url": image})
            item["content"] = parts
        else:
            item["content"] = content
        if message.name:
            item["name"] = message.name
        if message.tool_calls:
            item["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in message.tool_calls
            ]
        if message.tool_call_id:
            item["tool_call_id"] = message.tool_call_id
        messages.append(item)
    return messages


def _tool_specs(request: ChatRequest) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": dict(tool.parameters),
            },
        }
        for tool in request.tools
    ]


def _is_openai_family(provider: str) -> bool:
    return provider in (
        "openai",
        "openai_compatible",
        "azure",
        "azure_openai",
        "bailian",
        "volcengine_ark",
        "tencent_hunyuan",
        "vllm",
        "ollama",
    )


def _kwargs(request: ChatRequest, *, stream: bool, timeout: Any) -> dict[str, Any]:
    target = request.target
    kwargs: dict[str, Any] = {
        "model": _model_id(target),
        "messages": _messages(request),
        "max_tokens": request.max_tokens,
        "timeout": timeout,
        # 重试由网关统一控制，避免 SDK 隐藏重试造成重复计费
        "num_retries": 0,
        "drop_params": True,
    }
    api_base = _api_base(target)
    if api_base:
        kwargs["api_base"] = api_base
    if target.api_key:
        kwargs["api_key"] = target.api_key
    if target.api_version:
        kwargs["api_version"] = target.api_version
    if request.temperature is not None:
        kwargs["temperature"] = request.temperature
    if request.top_p is not None:
        kwargs["top_p"] = request.top_p
    if request.stop:
        kwargs["stop"] = list(request.stop)
    if request.user:
        kwargs["user"] = request.user
    if request.response_format:
        kwargs["response_format"] = dict(request.response_format)
    if request.tools:
        kwargs["tools"] = _tool_specs(request)
        choice = request.tool_choice
        if isinstance(choice, NamedToolChoice):
            kwargs["tool_choice"] = {
                "type": "function",
                "function": {"name": choice.name},
            }
        else:
            kwargs["tool_choice"] = choice
    if stream:
        kwargs["stream"] = True
        if _is_openai_family(target.provider):
            kwargs["stream_options"] = {"include_usage": True}
    return kwargs


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _int_attr(value: Any, name: str) -> int:
    raw = _field(value, name)
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _usage_from(
    raw: Any,
    *,
    prompt_estimate: int,
    completion_text: str,
    estimated: bool = False,
) -> Usage:
    prompt = _int_attr(raw, "prompt_tokens")
    completion = _int_attr(raw, "completion_tokens")
    if prompt == 0 and completion == 0:
        return Usage(
            prompt_tokens=max(1, prompt_estimate),
            completion_tokens=_estimate_completion(completion_text),
            estimated=True,
        )
    cached = 0
    prompt_details = _field(raw, "prompt_tokens_details")
    if prompt_details is not None:
        cached = _int_attr(prompt_details, "cached_tokens")
    reasoning = 0
    completion_details = _field(raw, "completion_tokens_details")
    if completion_details is not None:
        reasoning = _int_attr(completion_details, "reasoning_tokens")
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=min(cached, prompt),
        reasoning_tokens=min(reasoning, completion),
        estimated=estimated,
    )


def _estimate_completion(text: str) -> int:
    from ..usage_store import estimate_text_tokens

    return estimate_text_tokens(text)


def _finish_reason(raw: Any) -> FinishReason:
    value = str(raw or "").strip().lower()
    if value in ("length", "max_tokens"):
        return "length"
    if value in ("tool_calls", "tool_use", "function_call"):
        return "tool_calls"
    if value in ("content_filter", "safety", "recitation"):
        return "content_filter"
    if value == "error":
        return "error"
    return "stop"


def _message_text(message: Any) -> str:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
            else:
                text = getattr(part, "text", None)
            if text:
                chunks.append(str(text))
        return "".join(chunks)
    return ""


def _tool_calls(message: Any) -> tuple[ToolCall, ...]:
    calls = []
    for index, call in enumerate(getattr(message, "tool_calls", None) or []):
        function = getattr(call, "function", None)
        name = getattr(function, "name", "") or ""
        arguments = getattr(function, "arguments", "") or ""
        call_id = getattr(call, "id", "") or f"call_{index}"
        if not name:
            continue
        calls.append(
            ToolCall(
                id=call_id,
                name=name,
                arguments=arguments if isinstance(arguments, str) else "{}",
            )
        )
    return tuple(calls)


def _map_exception(exc: Exception, *, provider: str, api_key: str | None) -> AdapterError:
    name = type(exc).__name__.lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "timeout" in name:
        code = "UPSTREAM_TIMEOUT"
    elif "authentication" in name or "permission" in name or status in (401, 403):
        code = "PROVIDER_AUTH_FAILED"
    elif "ratelimit" in name or status == 429:
        code = "UPSTREAM_RATE_LIMITED"
    elif "contextwindow" in name or "badrequest" in name or status == 400:
        code = "INVALID_REQUEST"
    elif "serviceunavailable" in name or status in (502, 503):
        code = "PROVIDER_UNAVAILABLE"
    elif "connection" in name or "connect" in name:
        code = "PROVIDER_UNAVAILABLE"
    else:
        code = "UPSTREAM_ERROR"
    message = sanitize_error_message(str(exc), secrets=(api_key or "",))
    return AdapterError(
        code,
        message or "上游调用失败",
        provider=provider,
        retry_after=_retry_after(exc),
    )


class LiteLLMChatAdapter:
    name = "litellm"

    def __init__(
        self,
        *,
        connect_timeout_seconds: float = 10,
        idle_timeout_seconds: float = 120,
        max_retries: int = 1,
    ) -> None:
        self._connect = max(0.001, float(connect_timeout_seconds))
        self._idle = max(0.001, float(idle_timeout_seconds))
        self._max_retries = max(0, int(max_retries))

    async def complete(self, request: ChatRequest) -> ChatResponse:
        litellm = _litellm()
        timeout = _timeout(self._connect, request.target.timeout_seconds)
        attempt = 0
        while True:
            try:
                response = await litellm.acompletion(
                    **_kwargs(request, stream=False, timeout=timeout)
                )
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 统一映射成网关错误码
                error = _map_exception(
                    exc,
                    provider=request.target.provider,
                    api_key=request.target.api_key,
                )
                if attempt < self._max_retries and error.retryable:
                    await asyncio.sleep(_retry_delay(attempt, error))
                    attempt += 1
                    continue
                raise error from None
        choice = response.choices[0]
        message = choice.message
        parts = []
        thinking = getattr(message, "reasoning_content", None)
        if thinking:
            parts.append(ThinkingPart(text=str(thinking)))
        text = _message_text(message)
        tools = _tool_calls(message)
        if text or not tools:
            parts.append(TextPart(text=text))
        parts.extend(tools)
        from ..usage_store import estimate_prompt_tokens

        usage = _usage_from(
            getattr(response, "usage", None),
            prompt_estimate=estimate_prompt_tokens(request.messages, request.system),
            completion_text=text,
        )
        return ChatResponse(
            parts=tuple(parts),
            finish_reason=_finish_reason(choice.finish_reason),
            usage=usage,
            model=request.target.model,
            provider=request.target.provider,
            response_id=str(getattr(response, "id", "") or "") or None,
        )

    def stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        return self._stream(request)

    async def _stream(self, request: ChatRequest) -> AsyncIterator[StreamEvent]:
        litellm = _litellm()
        timeout = _timeout(self._connect, request.target.timeout_seconds)
        attempt = 0
        while True:
            try:
                response = await litellm.acompletion(
                    **_kwargs(request, stream=True, timeout=timeout)
                )
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                error = _map_exception(
                    exc,
                    provider=request.target.provider,
                    api_key=request.target.api_key,
                )
                if attempt < self._max_retries and error.retryable:
                    # 还没发出任何事件，重试是安全的；发出后就不再重试
                    await asyncio.sleep(_retry_delay(attempt, error))
                    attempt += 1
                    continue
                yield StreamError(
                    code=error.code, message=error.message, retryable=error.retryable
                )
                return

        chunks: list[Any] = []
        text_parts: list[str] = []
        usage_raw: Any = None
        emitted_usage: Any = None
        finish_reason: FinishReason = "stop"
        started = False
        tool_state: dict[int, dict[str, Any]] = {}
        started_tools: set[int] = set()

        def start_event() -> StreamStart:
            return StreamStart(
                model=request.target.model,
                provider=request.target.provider,
                response_id=str(getattr(response, "id", "") or "") or None,
            )

        try:
            stream = response.__aiter__()
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        stream.__anext__(), timeout=self._idle
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    # 空闲超时按可重试错误收尾；已发出的内容由 service 按估算结算
                    await _aclose(response)
                    yield StreamError(
                        code="UPSTREAM_TIMEOUT",
                        message=f"上游流式响应空闲超过 {int(self._idle)} 秒",
                        retryable=True,
                    )
                    return
                chunks.append(chunk)
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None and (
                    _int_attr(chunk_usage, "prompt_tokens")
                    or _int_attr(chunk_usage, "completion_tokens")
                ):
                    emitted_usage = chunk_usage
                if not started:
                    started = True
                    yield start_event()
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                if getattr(choice, "finish_reason", None):
                    finish_reason = _finish_reason(choice.finish_reason)
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                thinking = getattr(delta, "reasoning_content", None)
                if thinking:
                    yield ThinkingDelta(text=str(thinking))
                content = getattr(delta, "content", None)
                if content:
                    text_parts.append(str(content))
                    yield TextDelta(text=str(content))
                for call in getattr(delta, "tool_calls", None) or []:
                    index = int(getattr(call, "index", 0) or 0)
                    function = getattr(call, "function", None)
                    state = tool_state.setdefault(
                        index, {"id": "", "name": "", "args": "", "pending": ""}
                    )
                    call_id = getattr(call, "id", None)
                    if call_id:
                        state["id"] = str(call_id)
                    name = getattr(function, "name", None) if function else None
                    if name:
                        state["name"] = str(name)
                    arguments = (
                        getattr(function, "arguments", None) if function else None
                    )
                    if arguments:
                        state["pending"] += str(arguments)
                    if index not in started_tools and state["id"] and state["name"]:
                        started_tools.add(index)
                        yield ToolCallStart(
                            index=index, id=state["id"], name=state["name"]
                        )
                        if state["pending"]:
                            yield ToolCallDelta(index=index, arguments=state["pending"])
                            state["pending"] = ""
                    elif index in started_tools and state["pending"]:
                        yield ToolCallDelta(index=index, arguments=state["pending"])
                        state["pending"] = ""
        except asyncio.CancelledError:
            await _aclose(response)
            raise
        except Exception as exc:  # noqa: BLE001 - 流中失败用错误事件收尾
            error = _map_exception(
                exc,
                provider=request.target.provider,
                api_key=request.target.api_key,
            )
            yield StreamError(
                code=error.code, message=error.message, retryable=error.retryable
            )
            return

        if not started:
            yield start_event()

        # LiteLLM 会在上游提前断流时补一个 finish_reason=stop 的合成帧，
        # 只有 wrapper 自己记录到结束原因才说明上游真的给了结束帧。
        upstream_finished = bool(
            getattr(response, "received_finish_reason", None)
            or getattr(response, "intermittent_finish_reason", None)
        )
        if not upstream_finished:
            yield StreamError(
                code="UPSTREAM_ERROR",
                message="上游流式响应意外结束（缺少结束帧）",
                retryable=ERROR_SPECS["UPSTREAM_ERROR"].retryable,
            )
            return

        for index, state in sorted(tool_state.items()):
            if index in started_tools:
                continue
            if not state["name"]:
                continue
            call_id = state["id"] or f"call_{index}"
            yield ToolCallStart(index=index, id=call_id, name=state["name"])
            if state["pending"]:
                yield ToolCallDelta(index=index, arguments=state["pending"])
            started_tools.add(index)
        for index in sorted(started_tools):
            yield ToolCallEnd(index=index)

        text = "".join(text_parts)
        from ..usage_store import estimate_prompt_tokens

        # LiteLLM 会用 stream_chunk_builder 本地合成 usage 帧，
        # 只有 response.chunks 里留下的才是上游真的报过的用量。
        provided_usage = _upstream_usage(response)
        usage_raw = provided_usage or emitted_usage
        if usage_raw is None:
            usage_raw = _builder_usage(litellm, chunks)
        usage = _usage_from(
            usage_raw,
            prompt_estimate=estimate_prompt_tokens(request.messages, request.system),
            completion_text=text,
            estimated=provided_usage is None,
        )
        yield UsageEvent(usage=usage)
        yield StreamStop(finish_reason=finish_reason)


def _upstream_usage(response: Any) -> Any:
    """上游流式 usage 分帧上报：Anthropic 输入在 message_start、输出在
    message_delta。按字段取最大值合并，避免丢掉先报的那一半。"""
    merged: dict[str, Any] = {}
    seen = False
    for raw_chunk in getattr(response, "chunks", None) or []:
        candidate = _field(raw_chunk, "usage")
        if candidate is None:
            continue
        seen = True
        for name in ("prompt_tokens", "completion_tokens"):
            merged[name] = max(merged.get(name, 0), _int_attr(candidate, name))
        for name, detail in (
            ("prompt_tokens_details", "cached_tokens"),
            ("completion_tokens_details", "reasoning_tokens"),
        ):
            details = _field(candidate, name)
            if details is None:
                continue
            bucket = merged.setdefault(name, {})
            bucket[detail] = max(bucket.get(detail, 0), _int_attr(details, detail))
    return merged if seen else None


def _builder_usage(litellm: Any, chunks: list[Any]) -> Any:
    if not chunks:
        return None
    try:
        built = litellm.stream_chunk_builder(chunks)
    except Exception:  # noqa: BLE001 - builder 失败退化为估算
        return None
    return getattr(built, "usage", None)


async def _aclose(response: Any) -> None:
    closer = getattr(response, "aclose", None)
    if closer is None:
        return
    try:
        await closer()
    except Exception:  # noqa: BLE001 - 关闭失败不掩盖取消
        pass


class LiteLLMEmbeddingAdapter:
    name = "litellm"

    def __init__(
        self,
        *,
        connect_timeout_seconds: float = 10,
        max_retries: int = 1,
    ) -> None:
        self._connect = max(0.001, float(connect_timeout_seconds))
        self._max_retries = max(0, int(max_retries))

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        litellm = _litellm()
        kwargs: dict[str, Any] = {
            "model": _model_id(request.target),
            "input": list(request.inputs),
            "timeout": _timeout(self._connect, request.target.timeout_seconds),
            "num_retries": 0,
            "drop_params": True,
        }
        api_base = _api_base(request.target)
        if api_base:
            kwargs["api_base"] = api_base
        if request.target.api_key:
            kwargs["api_key"] = request.target.api_key
        if request.target.api_version:
            kwargs["api_version"] = request.target.api_version
        if request.user:
            kwargs["user"] = request.user
        attempt = 0
        while True:
            try:
                response = await litellm.aembedding(**kwargs)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                error = _map_exception(
                    exc,
                    provider=request.target.provider,
                    api_key=request.target.api_key,
                )
                if attempt < self._max_retries and error.retryable:
                    await asyncio.sleep(_retry_delay(attempt, error))
                    attempt += 1
                    continue
                raise error from None
        vectors = tuple(
            tuple(float(value) for value in item["embedding"])
            for item in response.data
        )
        from ..usage_store import estimate_prompt_tokens

        usage = _usage_from(
            getattr(response, "usage", None),
            prompt_estimate=max(
                1, sum(estimate_text(item) for item in request.inputs)
            ),
            completion_text="",
        )
        return EmbeddingResponse(
            vectors=vectors,
            usage=usage,
            model=request.target.model,
            provider=request.target.provider,
        )


def estimate_text(text: str) -> int:
    from ..usage_store import estimate_text_tokens

    return estimate_text_tokens(text)


def litellm_adapters(
    *,
    connect_timeout_seconds: float = 10,
    idle_timeout_seconds: float = 120,
    max_retries: int = 1,
) -> AdapterSet:
    return AdapterSet(
        chat=LiteLLMChatAdapter(
            connect_timeout_seconds=connect_timeout_seconds,
            idle_timeout_seconds=idle_timeout_seconds,
            max_retries=max_retries,
        ),
        embedding=LiteLLMEmbeddingAdapter(
            connect_timeout_seconds=connect_timeout_seconds,
            max_retries=max_retries,
        ),
    )


__all__ = ["LiteLLMChatAdapter", "LiteLLMEmbeddingAdapter", "litellm_adapters"]
