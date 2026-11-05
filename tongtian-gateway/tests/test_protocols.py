"""M1.2 协议面测试：Anthropic / Gemini 翻译、流式、错误、取消、工具与视觉。"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    bearer,
    check,
    configure_openai_provider,
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

from app.adapters.base import (  # noqa: E402
    AdapterSet,
    ChatMessage,
    ChatRequest,
    StreamError,
    StreamStart,
    StreamStop,
    TextDelta,
    ToolCallDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
    UsageEvent,
)
from app.protocols import anthropic as anthropic_protocol  # noqa: E402
from app.protocols import gemini as gemini_protocol  # noqa: E402
from app.protocols import openai as openai_protocol  # noqa: E402
from app.state import db, quota_tracker, usage_store  # noqa: E402

VERSION = {"anthropic-version": "2023-06-01"}
PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg=="
)
BIG_B64 = base64.b64encode(b"x" * 2048).decode("ascii")


def _usage_row(headers: dict) -> object:
    return usage_store.get(headers.get("x-tt-request-id", ""))


def _sse_payloads(text: str) -> list[dict]:
    payloads = []
    for frame in parse_sse(text):
        data = frame.get("data") or ""
        if not data or data == "[DONE]":
            continue
        try:
            payloads.append(json.loads(data))
        except json.JSONDecodeError:
            continue
    return payloads


def _tool_declaration() -> dict:
    return {
        "name": "get_weather",
        "description": "查询城市天气",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }


def _gemini_tool_group() -> dict:
    return {
        "functionDeclarations": [
            {
                "name": "get_weather",
                "description": "查询城市天气",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {"city": {"type": "STRING"}},
                    "required": ["city"],
                },
            }
        ]
    }


# ---- T2.1 三协议编码器往返 ----


def run_encoder_tests() -> None:
    execution = SimpleNamespace(
        request_id="req-encoder",
        prompt_estimate=9,
        started_ms=1761825600000,
        plan=SimpleNamespace(requested="fake-chat"),
        headers={},
    )
    events = [
        StreamStart(model="fake-chat", provider="openai_compatible", response_id="chatcmpl-x"),
        TextDelta(text="你好"),
        ToolCallStart(index=0, id="call_1", name="get_weather"),
        ToolCallDelta(index=0, arguments='{"city"'),
        ToolCallDelta(index=0, arguments=':"Beijing"}'),
        ToolCallEnd(index=0),
        UsageEvent(usage=Usage(prompt_tokens=12, completion_tokens=8)),
        StreamStop(finish_reason="tool_calls"),
    ]

    frames = openai_protocol._StreamFrames(execution, True)
    raw = "".join(frame for event in events for frame in frames.frames(event))
    payloads = _sse_payloads(raw)
    deltas = [p for p in payloads if p.get("choices")]
    check(
        "编码器 OpenAI 文本增量",
        any(
            item["choices"][0]["delta"].get("content") == "你好"
            for item in deltas
        ),
        str(payloads),
    )
    check(
        "编码器 OpenAI 工具增量",
        any(
            (item["choices"][0]["delta"].get("tool_calls") or [{}])[0]
            .get("function", {})
            .get("name")
            == "get_weather"
            for item in deltas
        ),
        str(payloads),
    )
    check(
        "编码器 OpenAI 结束帧",
        any(item["choices"][0].get("finish_reason") == "tool_calls" for item in deltas)
        and "[DONE]" in raw,
        raw,
    )
    check(
        "编码器 OpenAI 用量帧",
        any((item.get("usage") or {}).get("total_tokens") == 20 for item in payloads),
        str(payloads),
    )

    encoder = anthropic_protocol._StreamEncoder(execution)
    frames = [encoder.start_frame()]
    for event in events:
        frames.extend(encoder.frames(event))
    names = [frame["event"] for frame in parse_sse("".join(frames))]
    check(
        "编码器 Anthropic 事件顺序",
        names
        == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ],
        str(names),
    )
    payloads = _sse_payloads("".join(frames))
    check(
        "编码器 Anthropic 工具块",
        any(
            item.get("content_block", {}).get("type") == "tool_use"
            and item["content_block"]["name"] == "get_weather"
            for item in payloads
        ),
        str(payloads),
    )
    check(
        "编码器 Anthropic input_json_delta",
        any(
            item.get("delta", {}).get("partial_json") == '{"city":"Beijing"}'
            for item in payloads
        ),
        str(payloads),
    )
    check(
        "编码器 Anthropic 结束原因与用量",
        any(
            item.get("delta", {}).get("stop_reason") == "tool_use"
            and item.get("usage", {}).get("output_tokens") == 8
            and item.get("usage", {}).get("input_tokens") == 12
            for item in payloads
        ),
        str(payloads),
    )

    encoder = gemini_protocol._StreamEncoder(execution)
    frames = [encoder.start_frame()]
    for event in events:
        frames.extend(encoder.frames(event))
    payloads = _sse_payloads("".join(frames))
    chunks = [part for item in payloads for part in item["candidates"][0]["content"]["parts"]]
    check(
        "编码器 Gemini 文本与函数调用",
        any(part.get("text") == "你好" for part in chunks)
        and any(
            part.get("functionCall", {}).get("name") == "get_weather"
            and part["functionCall"]["args"] == {"city": "Beijing"}
            for part in chunks
        ),
        str(payloads),
    )
    final = payloads[-1]
    check(
        "编码器 Gemini 终帧",
        final["candidates"][0].get("finishReason") == "STOP"
        and final.get("usageMetadata", {}).get("totalTokenCount") == 20,
        str(final),
    )


# ---- T2.7 流式错误编码 ----


def run_stream_error_encoder_tests() -> None:
    execution = SimpleNamespace(
        request_id="req-error",
        prompt_estimate=5,
        started_ms=1761825600000,
        plan=SimpleNamespace(requested="fake-chat"),
        headers={},
    )
    error = StreamError(code="UPSTREAM_ERROR", message="上游断流")

    raw = "".join(openai_protocol._StreamFrames(execution, False).frames(error))
    payloads = _sse_payloads(raw)
    check(
        "流式错误 OpenAI 发错误帧不发 [DONE]",
        payloads
        and payloads[0]["error"]["code"] == "UPSTREAM_ERROR"
        and "[DONE]" not in raw,
        raw,
    )

    raw = "".join(anthropic_protocol._StreamEncoder(execution).frames(error))
    frames = parse_sse(raw)
    check(
        "流式错误 Anthropic 发 error 事件",
        frames
        and frames[0]["event"] == "error"
        and json.loads(frames[0]["data"])["error"]["tt_code"] == "UPSTREAM_ERROR"
        and "message_stop" not in raw,
        raw,
    )

    raw = "".join(gemini_protocol._StreamEncoder(execution).frames(error))
    payloads = _sse_payloads(raw)
    check(
        "流式错误 Gemini 发错误帧",
        payloads
        and payloads[0]["error"]["status"] == "INTERNAL"
        and payloads[0]["error"]["details"][0]["code"] == "UPSTREAM_ERROR"
        and "finishReason" not in raw,
        raw,
    )


# ---- Anthropic ----


def run_anthropic_tests(upstream) -> None:
    headers = {**bearer(), **VERSION}
    status, resp_headers, body = http(
        "POST",
        "/v1/messages",
        headers=headers,
        body={
            "model": "auto",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    check("Anthropic 非流式 200", status == 200, str(body))
    check(
        "Anthropic 非流式文本",
        body.get("type") == "message"
        and body.get("content") == [{"type": "text", "text": "你好"}]
        and body.get("stop_reason") == "end_turn",
        str(body),
    )
    check(
        "Anthropic 非流式用量",
        body.get("usage", {}).get("input_tokens", 0) > 0
        and body.get("usage", {}).get("output_tokens") == 8,
        str(body.get("usage")),
    )
    row = _usage_row(resp_headers)
    check(
        "Anthropic 落账本 protocol",
        row is not None and row.protocol == "anthropic" and row.status == "ok",
        str(row),
    )

    status, resp_headers, text = http(
        "POST",
        "/v1/messages",
        headers=headers,
        body={
            "model": "auto",
            "max_tokens": 64,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    check("Anthropic 流式 200", status == 200, str(text)[:200])
    check(
        "Anthropic 流式内容类型",
        resp_headers.get("content-type", "").startswith("text/event-stream"),
        str(resp_headers),
    )
    frames = parse_sse(text)
    names = [frame["event"] for frame in frames]
    check(
        "Anthropic 流式事件顺序",
        names[0] == "message_start"
        and names[-2:] == ["message_delta", "message_stop"]
        and "content_block_start" in names
        and "content_block_delta" in names,
        str(names),
    )
    payloads = _sse_payloads(text)
    check(
        "Anthropic 流式 message_start 用量",
        payloads[0]["message"]["usage"]["input_tokens"] > 0,
        str(payloads[0]),
    )
    check(
        "Anthropic 流式文本增量",
        any(
            item.get("delta", {}).get("type") == "text_delta"
            and item["delta"].get("text") == "你好"
            for item in payloads
        ),
        str(payloads),
    )
    check(
        "Anthropic 流式结束原因",
        any(
            item.get("delta", {}).get("stop_reason") == "end_turn"
            and item.get("usage", {}).get("output_tokens") == 8
            for item in payloads
        ),
        str(payloads),
    )
    row = _usage_row(resp_headers)
    check(
        "Anthropic 流式落账本",
        row is not None and row.protocol == "anthropic" and row.status == "ok",
        str(row),
    )

    status, _, body = http(
        "POST",
        "/v1/messages",
        headers=headers,
        body={
            "model": "auto",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "北京天气"}],
            "tools": [_tool_declaration()],
        },
    )
    blocks = body.get("content") or []
    check(
        "Anthropic 工具调用响应",
        status == 200
        and blocks
        and blocks[0].get("type") == "tool_use"
        and blocks[0].get("name") == "get_weather"
        and blocks[0].get("input") == {"city": "Beijing"}
        and body.get("stop_reason") == "tool_use",
        str(body),
    )

    status, _, body = http(
        "POST",
        "/v1/messages",
        headers=headers,
        body={
            "model": "auto",
            "max_tokens": 64,
            "messages": [
                {"role": "user", "content": "北京天气"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_fake",
                            "name": "get_weather",
                            "input": {"city": "Beijing"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_fake",
                            "content": [{"type": "text", "text": "晴 20 度"}],
                        }
                    ],
                },
            ],
        },
    )
    check("Anthropic 工具多轮 200", status == 200, str(body))
    messages = (upstream.records[-1]["body"] or {}).get("messages") or []
    check(
        "Anthropic 工具多轮回放",
        any(
            message.get("role") == "assistant"
            and (message.get("tool_calls") or [{}])[0].get("function", {}).get("name")
            == "get_weather"
            for message in messages
        )
        and any(
            message.get("role") == "tool"
            and message.get("tool_call_id") == "call_fake"
            and message.get("content") == "晴 20 度"
            for message in messages
        ),
        str(messages),
    )

    status, resp_headers, body = http(
        "POST",
        "/v1/messages/count_tokens",
        headers=headers,
        body={
            "model": "auto",
            "messages": [{"role": "user", "content": "你好，帮我算一下 token"}],
            "tools": [_tool_declaration()],
        },
    )
    check(
        "Anthropic count_tokens 估算",
        status == 200
        and body.get("input_tokens", 0) > 0
        and resp_headers.get("x-tt-estimated") == "1"
        and resp_headers.get("x-tt-model") in {"missing-model", "fake-chat"},
        f"{body} {resp_headers}",
    )

    status, _, body = http(
        "POST",
        "/v1/messages",
        headers=bearer(),
        body={
            "model": "auto",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    check(
        "Anthropic 缺版本头 400",
        status == 400
        and body.get("type") == "error"
        and body.get("error", {}).get("tt_code") == "INVALID_REQUEST",
        str(body),
    )

    status, resp_headers, body = http(
        "POST",
        "/v1/messages",
        headers=headers,
        body={
            "model": "auto",
            "max_tokens": 64,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": PNG_B64,
                            },
                        },
                    ],
                }
            ],
        },
    )
    check("Anthropic base64 图片 200", status == 200, str(body))
    messages = (upstream.records[-1]["body"] or {}).get("messages") or []
    parts = messages[-1].get("content") if messages else None
    check(
        "Anthropic base64 图片转 data URI",
        isinstance(parts, list)
        and any(
            part.get("type") == "image_url"
            and str(part.get("image_url", {}).get("url", "")).startswith(
                "data:image/png;base64,"
            )
            for part in parts
        ),
        str(parts),
    )

    status, _, body = http(
        "POST",
        "/v1/messages",
        headers=headers,
        body={
            "model": "auto",
            "max_tokens": 64,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "url",
                                "url": "https://example.com/cat.png",
                            },
                        }
                    ],
                }
            ],
        },
    )
    check("Anthropic URL 图片 200", status == 200, str(body))
    messages = (upstream.records[-1]["body"] or {}).get("messages") or []
    check(
        "Anthropic URL 图片透传",
        "https://example.com/cat.png" in json.dumps(messages, ensure_ascii=False),
        str(messages),
    )

    original = anthropic_protocol.MAX_IMAGE_BYTES
    anthropic_protocol.MAX_IMAGE_BYTES = 1024
    try:
        status, _, body = http(
            "POST",
            "/v1/messages",
            headers=headers,
            body={
                "model": "auto",
                "max_tokens": 64,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": BIG_B64,
                                },
                            }
                        ],
                    }
                ],
            },
        )
    finally:
        anthropic_protocol.MAX_IMAGE_BYTES = original
    check(
        "Anthropic 超大图片 413",
        status == 413 and body.get("error", {}).get("tt_code") == "PAYLOAD_TOO_LARGE",
        str(body),
    )

    upstream.stream_cut_after = 2
    try:
        status, resp_headers, text = http(
            "POST",
            "/v1/messages",
            headers=headers,
            body={
                "model": "auto",
                "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    finally:
        upstream.stream_cut_after = None
    frames = parse_sse(text) if isinstance(text, str) else []
    check(
        "Anthropic 中途错误事件",
        status == 200
        and frames
        and frames[-1]["event"] == "error"
        and json.loads(frames[-1]["data"])["error"]["tt_code"] == "UPSTREAM_ERROR"
        and not any(frame["event"] == "message_stop" for frame in frames),
        str(frames),
    )
    row = _usage_row(resp_headers)
    check(
        "Anthropic 中途错误落账本",
        row is not None
        and row.protocol == "anthropic"
        and row.status == "error"
        and row.errorCode == "UPSTREAM_ERROR"
        and row.totalTokens > 0
        and row.estimated is True,
        str(row),
    )


# ---- Gemini ----


def run_gemini_tests(upstream) -> None:
    headers = bearer()
    status, resp_headers, body = http(
        "POST",
        "/v1beta/models/auto:generateContent",
        headers=headers,
        body={
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "generationConfig": {"maxOutputTokens": 64},
        },
    )
    candidates = (body.get("candidates") or [{}])
    parts = candidates[0].get("content", {}).get("parts") or [{}]
    check("Gemini 非流式 200", status == 200, str(body))
    check(
        "Gemini 非流式文本与结束原因",
        parts[0].get("text") == "你好"
        and candidates[0].get("finishReason") == "STOP",
        str(body),
    )
    check(
        "Gemini 非流式用量",
        body.get("usageMetadata", {}).get("totalTokenCount") == 20,
        str(body.get("usageMetadata")),
    )
    row = _usage_row(resp_headers)
    check(
        "Gemini 落账本 protocol",
        row is not None and row.protocol == "gemini" and row.status == "ok",
        str(row),
    )

    status, resp_headers, text = http(
        "POST",
        "/v1beta/models/auto:streamGenerateContent?alt=sse",
        headers=headers,
        body={
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "generationConfig": {"maxOutputTokens": 64},
        },
    )
    check("Gemini 流式 200", status == 200, str(text)[:200])
    payloads = _sse_payloads(text)
    texts = [
        part.get("text")
        for item in payloads
        for part in (item.get("candidates") or [{}])[0]
        .get("content", {})
        .get("parts", [])
    ]
    check(
        "Gemini 流式文本增量",
        "".join(value for value in texts if value) == "你好",
        str(payloads),
    )
    final = payloads[-1] if payloads else {}
    check(
        "Gemini 流式终帧",
        final.get("candidates", [{}])[0].get("finishReason") == "STOP"
        and final.get("usageMetadata", {}).get("totalTokenCount") == 20,
        str(final),
    )
    check("Gemini 流式不使用 [DONE]", "[DONE]" not in str(text), str(text)[:200])

    status, _, body = http(
        "POST",
        "/v1beta/models/auto:generateContent",
        headers=headers,
        body={
            "contents": [{"role": "user", "parts": [{"text": "北京天气"}]}],
            "tools": [_gemini_tool_group()],
        },
    )
    parts = (
        (body.get("candidates") or [{}])[0].get("content", {}).get("parts") or [{}]
    )
    check(
        "Gemini 函数调用响应",
        status == 200
        and parts[0].get("functionCall", {}).get("name") == "get_weather"
        and parts[0].get("functionCall", {}).get("args") == {"city": "Beijing"},
        str(body),
    )

    status, _, body = http(
        "POST",
        "/v1beta/models/auto:generateContent",
        headers=headers,
        body={
            "contents": [
                {"role": "user", "parts": [{"text": "北京天气"}]},
                {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "name": "get_weather",
                                "args": {"city": "Beijing"},
                            }
                        }
                    ],
                },
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": "get_weather",
                                "response": {"temperature": 20},
                            }
                        }
                    ],
                },
            ],
        },
    )
    check("Gemini 函数多轮 200", status == 200, str(body))
    messages = (upstream.records[-1]["body"] or {}).get("messages") or []
    check(
        "Gemini 函数多轮回放",
        any(
            message.get("role") == "assistant"
            and (message.get("tool_calls") or [{}])[0].get("function", {}).get("name")
            == "get_weather"
            for message in messages
        )
        and any(
            message.get("role") == "tool"
            and message.get("tool_call_id") == "call_1"
            and "temperature" in str(message.get("content"))
            for message in messages
        ),
        str(messages),
    )

    status, resp_headers, body = http(
        "POST",
        "/v1beta/models/auto:countTokens",
        headers=headers,
        body={
            "contents": [{"role": "user", "parts": [{"text": "你好，帮我算一下 token"}]}],
            "tools": [_gemini_tool_group()],
        },
    )
    check(
        "Gemini countTokens 估算",
        status == 200
        and body.get("totalTokens", 0) > 0
        and resp_headers.get("x-tt-estimated") == "1"
        and resp_headers.get("x-tt-model") in {"missing-model", "fake-chat"},
        f"{body} {resp_headers}",
    )

    status, _, body = http("GET", "/v1beta/models", headers=headers)
    names = {item.get("name") for item in body.get("models", [])}
    methods = {
        method
        for item in body.get("models", [])
        for method in item.get("supportedGenerationMethods", [])
    }
    check(
        "Gemini 模型列表只含策略允许项",
        status == 200
        and {"models/auto", "models/fake-chat", "models/missing-model"} <= names
        and "models/fake-embed" not in names,
        str(names),
    )
    check(
        "Gemini 模型列表声明生成方法",
        {"generateContent", "streamGenerateContent", "countTokens"} <= methods,
        str(methods),
    )

    status, _, body = http(
        "POST",
        "/v1beta/models/auto:generateContent",
        headers=headers,
        body={"contents": []},
    )
    check(
        "Gemini 空 contents 400",
        status == 400
        and body.get("error", {}).get("status") == "INVALID_ARGUMENT"
        and body.get("error", {}).get("details", [{}])[0].get("code")
        == "INVALID_REQUEST",
        str(body),
    )

    status, _, body = http(
        "POST",
        "/v1beta/models/auto:generateContent",
        headers=headers,
        body={
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": "看图"},
                        {"inlineData": {"mimeType": "image/png", "data": PNG_B64}},
                    ],
                }
            ]
        },
    )
    check("Gemini inlineData 图片 200", status == 200, str(body))
    messages = (upstream.records[-1]["body"] or {}).get("messages") or []
    parts = messages[-1].get("content") if messages else None
    check(
        "Gemini inlineData 转 data URI",
        isinstance(parts, list)
        and any(
            part.get("type") == "image_url"
            and str(part.get("image_url", {}).get("url", "")).startswith(
                "data:image/png;base64,"
            )
            for part in parts
        ),
        str(parts),
    )

    status, _, body = http(
        "POST",
        "/v1beta/models/auto:generateContent",
        headers=headers,
        body={
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "fileData": {
                                "mimeType": "image/png",
                                "fileUri": "https://example.com/dog.png",
                            }
                        }
                    ],
                }
            ]
        },
    )
    check("Gemini fileData 图片 200", status == 200, str(body))
    messages = (upstream.records[-1]["body"] or {}).get("messages") or []
    check(
        "Gemini fileData URL 透传",
        "https://example.com/dog.png" in json.dumps(messages, ensure_ascii=False),
        str(messages),
    )

    original = gemini_protocol.MAX_IMAGE_BYTES
    gemini_protocol.MAX_IMAGE_BYTES = 1024
    try:
        status, _, body = http(
            "POST",
            "/v1beta/models/auto:generateContent",
            headers=headers,
            body={
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "image/png",
                                    "data": BIG_B64,
                                }
                            }
                        ],
                    }
                ]
            },
        )
    finally:
        gemini_protocol.MAX_IMAGE_BYTES = original
    check(
        "Gemini 超大图片 413",
        status == 413
        and body.get("error", {}).get("details", [{}])[0].get("code")
        == "PAYLOAD_TOO_LARGE",
        str(body),
    )

    upstream.stream_cut_after = 2
    try:
        status, resp_headers, text = http(
            "POST",
            "/v1beta/models/auto:streamGenerateContent?alt=sse",
            headers=headers,
            body={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        )
    finally:
        upstream.stream_cut_after = None
    payloads = _sse_payloads(text) if isinstance(text, str) else []
    check(
        "Gemini 中途错误帧",
        status == 200
        and payloads
        and payloads[-1].get("error", {}).get("details", [{}])[0].get("code")
        == "UPSTREAM_ERROR"
        and not any(
            (item.get("candidates") or [{}])[0].get("finishReason")
            for item in payloads
        ),
        str(payloads),
    )
    row = _usage_row(resp_headers)
    check(
        "Gemini 中途错误落账本",
        row is not None
        and row.protocol == "gemini"
        and row.status == "error"
        and row.errorCode == "UPSTREAM_ERROR"
        and row.totalTokens > 0
        and row.estimated is True,
        str(row),
    )


# ---- T2.8 客户端取消 ----


class _ScriptedChat:
    name = "scripted"

    def __init__(self, events) -> None:
        self._events = list(events)

    async def complete(self, request):  # pragma: no cover - 取消测试只走流式
        raise AssertionError("取消测试不应调用 complete")

    def stream(self, request):
        events = self._events

        async def generator():
            for event in events:
                yield event

        return generator()


class _NoEmbedding:
    name = "none"

    async def embed(self, request):  # pragma: no cover
        raise AssertionError("取消测试不应调用 embed")


def _cancel_principal():
    from app.auth import Principal

    return Principal(
        subject="u-cancel",
        session_id="s-cancel",
        tenant="acme",
        department="dept-sales",
        departments=("dept-sales",),
        roles=("member",),
        auth_kind="jwt",
    )


def _scripted_service(events):
    from app.config import settings
    from app.service import ExecutionService
    from app.state import policy_store, provider_store

    return ExecutionService(
        settings,
        policy_store,
        provider_store,
        quota_tracker,
        usage_store,
        AdapterSet(chat=_ScriptedChat(events), embedding=_NoEmbedding()),
    )


def run_cancel_tests() -> None:
    service = _scripted_service(
        [
            StreamStart(model="fake-chat", provider="openai_compatible"),
            TextDelta(text="你好"),
            TextDelta(text="世界"),
        ]
    )
    principal = _cancel_principal()
    count = 100

    async def cancel_all() -> None:
        for index in range(count):
            request_id = f"req-cancel-{index}"
            execution = service.begin_chat(
                principal,
                "auto",
                max_tokens=64,
                prompt_estimate=5,
                request_id=request_id,
                endpoint="/v1/chat/completions",
                stream=True,
            )

            def factory(target, max_tokens, request_id=request_id):
                return ChatRequest(
                    target=target,
                    messages=(ChatMessage(role="user", content="hi"),),
                    max_tokens=max_tokens,
                    stream=True,
                    request_id=request_id,
                )

            generator = service.stream_chat(execution, factory)
            await generator.__anext__()  # StreamStart
            await generator.__anext__()  # 已经吐出一段文本
            await generator.aclose()

    asyncio.run(cancel_all())

    rows = db.query(
        "SELECT status, error_code, total_tokens, estimated FROM usage_events "
        "WHERE request_id LIKE 'req-cancel-%'"
    )
    check(
        f"{count} 次取消都落账本",
        len(rows) == count and all(row["status"] == "cancelled" for row in rows),
        f"rows={len(rows)}",
    )
    check(
        "取消事件标记 CLIENT_CANCELLED",
        rows
        and all(row["error_code"] == "CLIENT_CANCELLED" for row in rows)
        and all(int(row["total_tokens"]) > 0 for row in rows),
        str(rows[:2]),
    )
    check(
        "取消按估算结算",
        rows and all(int(row["estimated"]) == 1 for row in rows),
        str(rows[:2]),
    )

    snapshot = {
        row["principal"]: row for row in quota_tracker.snapshot()
    }
    row = snapshot.get("acme:dept-sales:u-cancel")
    check(
        "取消后并发与预留归零",
        row is not None
        and row["activeRequests"] == 0
        and row["reservedTokens"] == 0,
        str(row),
    )


def main() -> int:
    server = start_gateway()
    upstreams = start_fake_upstreams(("openai",))
    upstream = upstreams["openai"]
    try:
        configure_openai_provider(upstream)
        run_encoder_tests()
        run_stream_error_encoder_tests()
        run_anthropic_tests(upstream)
        run_gemini_tests(upstream)
        run_cancel_tests()
    finally:
        stop_fake_upstreams(upstreams)
        stop_gateway(server)

    return report("全部协议面测试通过")


if __name__ == "__main__":
    raise SystemExit(main())
