"""三套假上游：OpenAI / Anthropic / Gemini，用于契约测试，不发真实请求。"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CREATED = 1761825600


def _error_payload(status: int) -> dict:
    kind = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        429: "rate_limit_error",
    }.get(status, "api_error")
    return {
        "error": {
            "message": f"fake upstream error {status}",
            "type": kind,
            "code": f"fake_{status}",
        }
    }


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fake-upstream/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        body = self._read_body() if method == "POST" else {}
        self.server.records.append(  # type: ignore[attr-defined]
            {
                "method": method,
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
            }
        )
        delay = self.server.delay_seconds  # type: ignore[attr-defined]
        if delay:
            time.sleep(delay)
        fail_status = self.server.fail_status  # type: ignore[attr-defined]
        if fail_status:
            limit = self.server.fail_times  # type: ignore[attr-defined]
            if limit <= 0 or self.server.failures_served < limit:  # type: ignore[attr-defined]
                self.server.failures_served += 1  # type: ignore[attr-defined]
                self._json(_error_payload(fail_status), fail_status)
                return
        kind = self.server.kind  # type: ignore[attr-defined]
        if kind == "openai":
            self._openai(method, path, body)
        elif kind == "anthropic":
            self._anthropic(method, path, body)
        else:
            self._gemini(method, path, body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _json(self, payload: Any, status: int = 200) -> None:
        raw = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def _sse(self, blocks: list[str], cut_after: int | None = None) -> None:
        if cut_after is not None:
            blocks = blocks[:cut_after]
        stall_after = self.server.stall_after  # type: ignore[attr-defined]
        stall_seconds = self.server.stall_seconds  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        if stall_after is None:
            self.wfile.write("".join(blocks).encode("utf-8"))
        else:
            for index, block in enumerate(blocks):
                if index == stall_after:
                    self.wfile.flush()
                    time.sleep(stall_seconds)
                    break
                self.wfile.write(block.encode("utf-8"))
                self.wfile.flush()
        self.close_connection = True

    def _not_found(self) -> None:
        self._json(
            {
                "error": {
                    "message": f"unknown path {self.path}",
                    "type": "invalid_request_error",
                    "code": "NOT_FOUND",
                }
            },
            404,
        )

    # ---- OpenAI ----

    def _openai(self, method: str, path: str, body: dict) -> None:
        if method == "GET" and path == "/v1/models":
            self._json(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "fake-openai-model",
                            "object": "model",
                            "owned_by": "fake",
                        }
                    ],
                }
            )
            return
        if method == "POST" and path == "/v1/chat/completions":
            model = str(body.get("model") or "fake-openai-model")
            has_tools = bool(body.get("tools"))
            include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
            if body.get("stream"):
                self._openai_stream(model, has_tools, include_usage)
            else:
                message = (
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_fake",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city":"Beijing"}',
                                },
                            }
                        ],
                    }
                    if has_tools
                    else {"role": "assistant", "content": "你好"}
                )
                self._json(
                    {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion",
                        "created": CREATED,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": message,
                                "finish_reason": "tool_calls" if has_tools else "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 8,
                            "total_tokens": 20,
                        }
                        if not self.server.drop_usage  # type: ignore[attr-defined]
                        else None,
                    }
                )
            return
        if method == "POST" and path == "/v1/embeddings":
            raw_input = body.get("input")
            count = len(raw_input) if isinstance(raw_input, list) and raw_input else 1
            self._json(
                {
                    "object": "list",
                    "data": [
                        {
                            "object": "embedding",
                            "index": index,
                            "embedding": [0.1, 0.2, 0.3],
                        }
                        for index in range(count)
                    ],
                    "model": str(body.get("model") or "fake-embedding"),
                    "usage": {
                        "prompt_tokens": 5 * count,
                        "total_tokens": 5 * count,
                    },
                }
            )
            return
        self._not_found()

    def _openai_stream(
        self, model: str, has_tools: bool = False, include_usage: bool = False
    ) -> None:
        base = {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "created": CREATED,
            "model": model,
        }

        def chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> str:
            payload = {
                **base,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            if usage is not None:
                payload["usage"] = usage
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        if has_tools:
            frames = [
                chunk({"role": "assistant"}),
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_fake",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": ""},
                            }
                        ]
                    }
                ),
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '{"city":"Beijing"}'},
                            }
                        ]
                    }
                ),
                chunk({}, "tool_calls"),
            ]
        else:
            frames = [
                chunk({"role": "assistant"}),
                chunk({"content": "你好"}),
                chunk({}, "stop"),
            ]
        if include_usage and not self.server.drop_usage:  # type: ignore[attr-defined]
            # 与 OpenAI 一致：include_usage 时单独一帧 choices 为空、只带 usage
            frames.append(
                "data: "
                + json.dumps(
                    {
                        **base,
                        "choices": [],
                        "usage": {
                            "prompt_tokens": 12,
                            "completion_tokens": 8,
                            "total_tokens": 20,
                        },
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            )
        self._sse(
            [*frames, "data: [DONE]\n\n"],
            cut_after=self.server.stream_cut_after,  # type: ignore[attr-defined]
        )

    # ---- Anthropic ----

    def _anthropic(self, method: str, path: str, body: dict) -> None:
        if method != "POST":
            self._not_found()
            return
        if path == "/v1/messages/count_tokens":
            self._json({"input_tokens": 12})
            return
        if path != "/v1/messages":
            self._not_found()
            return
        model = str(body.get("model") or "claude-fake")
        has_tools = bool(body.get("tools"))
        if body.get("stream"):
            self._anthropic_stream(model, has_tools)
            return
        content = (
            [
                {
                    "type": "tool_use",
                    "id": "toolu_fake",
                    "name": "get_weather",
                    "input": {"city": "Beijing"},
                }
            ]
            if has_tools
            else [{"type": "text", "text": "你好"}]
        )
        self._json(
            {
                "id": "msg_fake",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": content,
                "stop_reason": "tool_use" if has_tools else "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 8},
            }
        )

    def _anthropic_stream(self, model: str, has_tools: bool = False) -> None:
        def frame(event: str, payload: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        message_start = frame(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_fake",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 12, "output_tokens": 0},
                },
            },
        )
        if has_tools:
            blocks = [
                frame(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "tool_use",
                            "id": "toolu_fake",
                            "name": "get_weather",
                            "input": {},
                        },
                    },
                ),
                frame(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": '{"city":"Beijing"}',
                        },
                    },
                ),
                frame("content_block_stop", {"type": "content_block_stop", "index": 0}),
                frame(
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "tool_use"},
                        "usage": {"output_tokens": 8},
                    },
                ),
            ]
        else:
            blocks = [
                frame(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                frame(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "你好"},
                    },
                ),
                frame("content_block_stop", {"type": "content_block_stop", "index": 0}),
                frame(
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn"},
                        "usage": {"output_tokens": 8},
                    },
                ),
            ]
        self._sse([message_start, *blocks, frame("message_stop", {"type": "message_stop"})])

    # ---- Gemini ----

    def _gemini(self, method: str, path: str, body: dict) -> None:
        if method == "GET" and path == "/v1beta/models":
            self._json(
                {
                    "models": [
                        {
                            "name": "models/gemini-fake",
                            "displayName": "Gemini Fake",
                            "supportedGenerationMethods": [
                                "generateContent",
                                "streamGenerateContent",
                                "countTokens",
                            ],
                        }
                    ]
                }
            )
            return
        if method != "POST":
            self._not_found()
            return
        prefix = "/v1beta/models/"
        if not path.startswith(prefix):
            self._not_found()
            return
        model, _, action = path[len(prefix) :].partition(":")
        if action == "countTokens":
            self._json({"totalTokens": 12})
            return
        has_tools = bool(body.get("tools"))
        if action == "generateContent":
            parts = (
                [{"functionCall": {"name": "get_weather", "args": {"city": "Beijing"}}}]
                if has_tools
                else [{"text": "你好"}]
            )
            self._json(
                {
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": parts},
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 12,
                        "candidatesTokenCount": 8,
                        "totalTokenCount": 20,
                    },
                }
            )
            return
        if action == "streamGenerateContent":
            self._gemini_stream(has_tools)
            return
        self._not_found()

    def _gemini_stream(self, has_tools: bool = False) -> None:
        def frame(payload: dict) -> str:
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        if has_tools:
            frames = [
                frame(
                    {
                        "candidates": [
                            {
                                "content": {
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
                                "finishReason": "STOP",
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 12,
                            "candidatesTokenCount": 8,
                            "totalTokenCount": 20,
                        },
                    }
                )
            ]
            self._sse(frames)
            return
        self._sse(
            [
                frame(
                    {
                        "candidates": [
                            {"content": {"role": "model", "parts": [{"text": "你"}]}}
                        ]
                    }
                ),
                frame(
                    {
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": "好"}],
                                },
                                "finishReason": "STOP",
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 12,
                            "candidatesTokenCount": 8,
                            "totalTokenCount": 20,
                        },
                    }
                ),
            ]
        )


class _UpstreamServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 256  # 默认 5，并发压测时会让客户端 SYN 重排队

    def __init__(
        self,
        address: tuple[str, int],
        kind: str,
        *,
        fail_status: int | None = None,
        fail_times: int = 0,
        stream_cut_after: int | None = None,
        stall_after: int | None = None,
        stall_seconds: float = 0.0,
        delay_seconds: float = 0.0,
        drop_usage: bool = False,
    ) -> None:
        self.kind = kind
        self.fail_status = fail_status
        self.fail_times = fail_times
        self.failures_served = 0
        self.stream_cut_after = stream_cut_after
        self.stall_after = stall_after
        self.stall_seconds = stall_seconds
        self.delay_seconds = delay_seconds
        self.drop_usage = drop_usage
        self.records: list[dict] = []
        super().__init__(address, _Handler)


class FakeUpstream:
    def __init__(
        self,
        kind: str,
        *,
        fail_status: int | None = None,
        fail_times: int = 0,
        stream_cut_after: int | None = None,
        stall_after: int | None = None,
        stall_seconds: float = 0.0,
        delay_seconds: float = 0.0,
        drop_usage: bool = False,
    ) -> None:
        self.kind = kind
        self._server = _UpstreamServer(
            ("127.0.0.1", 0),
            kind,
            fail_status=fail_status,
            fail_times=fail_times,
            stream_cut_after=stream_cut_after,
            stall_after=stall_after,
            stall_seconds=stall_seconds,
            delay_seconds=delay_seconds,
            drop_usage=drop_usage,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, name=f"fake-{kind}", daemon=True
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def records(self) -> list[dict]:
        return self._server.records

    @property
    def fail_status(self) -> int | None:
        return self._server.fail_status

    @fail_status.setter
    def fail_status(self, value: int | None) -> None:
        self._server.fail_status = value

    @property
    def fail_times(self) -> int:
        return self._server.fail_times

    @fail_times.setter
    def fail_times(self, value: int) -> None:
        self._server.fail_times = value

    @property
    def failures_served(self) -> int:
        return self._server.failures_served

    @property
    def stall_after(self) -> int | None:
        return self._server.stall_after

    @stall_after.setter
    def stall_after(self, value: int | None) -> None:
        self._server.stall_after = value

    @property
    def stall_seconds(self) -> float:
        return self._server.stall_seconds

    @stall_seconds.setter
    def stall_seconds(self, value: float) -> None:
        self._server.stall_seconds = value

    @property
    def stream_cut_after(self) -> int | None:
        return self._server.stream_cut_after

    @stream_cut_after.setter
    def stream_cut_after(self, value: int | None) -> None:
        self._server.stream_cut_after = value

    @property
    def delay_seconds(self) -> float:
        return self._server.delay_seconds

    @delay_seconds.setter
    def delay_seconds(self, value: float) -> None:
        self._server.delay_seconds = value

    @property
    def drop_usage(self) -> bool:
        return self._server.drop_usage

    @drop_usage.setter
    def drop_usage(self, value: bool) -> None:
        self._server.drop_usage = value

    def start(self) -> "FakeUpstream":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
