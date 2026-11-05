#!/usr/bin/env python3
"""假 OpenAI 兼容模型端点（仅供 D-01 服务化部署演练使用）。

用途：在没有真实厂商凭据的前提下，让引擎走完整的
「计划 → 审阅 → 执行（多轮工具调用）→ 产出」链路，并能把一条 SSE 流
人为撑到十分钟以上，用来验证反向代理对 SSE 的缓冲、空闲超时与长连接行为。

行为全部由环境变量控制：
  FAKE_PORT              监听端口（默认 9000）
  FAKE_MODEL             模型名（默认 fake-chat，仅回显）
  FAKE_PLAN_DELAY_S      非流式（计划生成）响应前的静默秒数（默认 1）
  FAKE_EXEC_DELAY_S      流式响应首字节前的静默秒数（默认 5）
  FAKE_TOOL_ROUNDS       前多少次流式调用返回工具调用（默认 2）
  FAKE_TOOL_NAME         工具名（默认 list_directory，只读，引擎内置）
  FAKE_CHUNKS            最终答复的流式分块数（默认 8）
  FAKE_CHUNK_GAP_S       分块之间的间隔秒数（默认 1）

纯标准库，零依赖。这不是产品代码，也不进镜像。
"""

from __future__ import annotations

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("FAKE_PORT", "9000"))
MODEL = os.environ.get("FAKE_MODEL", "fake-chat")
PLAN_DELAY_S = float(os.environ.get("FAKE_PLAN_DELAY_S", "1"))
EXEC_DELAY_S = float(os.environ.get("FAKE_EXEC_DELAY_S", "5"))
TOOL_ROUNDS = int(os.environ.get("FAKE_TOOL_ROUNDS", "2"))
TOOL_NAME = os.environ.get("FAKE_TOOL_NAME", "list_directory")
CHUNKS = int(os.environ.get("FAKE_CHUNKS", "8"))
CHUNK_GAP_S = float(os.environ.get("FAKE_CHUNK_GAP_S", "1"))

_counter = {"stream": 0}

# 运行时可调（演练脚本用 POST /__config 改这些值，避免为不同阶段重启容器）
_CFG = {
    "plan_delay_s": PLAN_DELAY_S,
    "exec_delay_s": EXEC_DELAY_S,
    "tool_rounds": TOOL_ROUNDS,
    "chunks": CHUNKS,
    "chunk_gap_s": CHUNK_GAP_S,
}


def _apply_config(payload: dict) -> None:
    for key in ("plan_delay_s", "exec_delay_s", "chunk_gap_s"):
        if key in payload:
            _CFG[key] = float(payload[key])
    for key in ("tool_rounds", "chunks"):
        if key in payload:
            _CFG[key] = int(payload[key])
    if payload.get("reset"):
        _counter["stream"] = 0


def _chunk(cid: str, created: int, delta: dict, finish: str | None = None) -> bytes:
    body = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n".encode("utf-8")


def _emit(handler: BaseHTTPRequestHandler, payload: bytes) -> None:
    """写一块并立刻 flush：SSE 的节奏就是被测对象，不能留在缓冲区里"""
    handler.wfile.write(payload)
    handler.wfile.flush()


def _tool_call_response(handler: BaseHTTPRequestHandler, cid: str, created: int) -> None:
    """流式返回一次工具调用：先给 name，再分片给 arguments，末尾 tool_calls 收尾"""
    call_id = f"call-{cid}-{created}"
    _emit(handler, _chunk(cid, created, {"role": "assistant", "content": None}))
    _emit(
        handler,
        _chunk(
            cid,
            created,
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": TOOL_NAME, "arguments": ""},
                    }
                ]
            },
        ),
    )
    arguments = json.dumps({"path": ".", "depth": 1, "limit": 50})
    for i in range(0, len(arguments), 12):
        _emit(
            handler,
            _chunk(
                cid,
                created,
                {"tool_calls": [{"index": 0, "function": {"arguments": arguments[i : i + 12]}}]},
            ),
        )
        time.sleep(0.05)
    _emit(handler, _chunk(cid, created, {}, finish="tool_calls"))
    _emit(handler, b"data: [DONE]\n\n")


def _final_response(handler: BaseHTTPRequestHandler, cid: str, created: int) -> None:
    """流式返回最终答复，分块之间留可配置的间隔（制造真实的慢流）"""
    chunks = max(1, _CFG["chunks"])
    gap = _CFG["chunk_gap_s"]
    _emit(handler, _chunk(cid, created, {"role": "assistant", "content": ""}))
    text = "已完成部署演练所需的检查。"
    step = max(1, len(text) // chunks)
    for i in range(0, len(text), step):
        _emit(handler, _chunk(cid, created, {"content": text[i : i + step]}))
        time.sleep(gap)
    _emit(handler, _chunk(cid, created, {}, finish="stop"))
    _emit(
        handler,
        json.dumps(
            {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL,
                "choices": [],
                "usage": {"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54},
            }
        ).encode("utf-8")
        + b"\n\n",
    )
    _emit(handler, b"data: [DONE]\n\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeOpenAI/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: D102
        sys.stderr.write("[fake-model] %s\n" % (fmt % args))

    def _send_json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(200, {"ok": True, "model": MODEL})
        else:
            self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"

        if self.path == "/__reset":
            _counter["stream"] = 0
            self._send_json(200, {"ok": True})
            return
        if self.path == "/__config":
            try:
                _apply_config(json.loads(raw or b"{}"))
            except Exception as exc:  # noqa: BLE001
                self._send_json(400, {"error": {"message": f"bad config: {exc}"}})
                return
            self._send_json(200, {"ok": True, "config": dict(_CFG)})
            return
        if not self.path.endswith("/chat/completions"):
            self._send_json(404, {"error": {"message": f"unsupported path {self.path}"}})
            return

        try:
            req = json.loads(raw or b"{}")
        except Exception as exc:  # noqa: BLE001
            self._send_json(400, {"error": {"message": f"bad json: {exc}"}})
            return

        created = int(time.time())
        cid = f"chatcmpl-fake-{created}"

        if not req.get("stream"):
            # 计划生成：静默一会儿后给一个可解析的步骤数组
            time.sleep(_CFG["plan_delay_s"])
            plan = [
                {"step_id": "step-1", "type": "tool_call", "description": "列出工作目录内容"},
                {"step_id": "step-2", "type": "llm_call", "description": "汇总目录结构"},
                {"step_id": "step-3", "type": "llm_call", "description": "给出部署检查结论"},
            ]
            self._send_json(
                200,
                {
                    "id": cid,
                    "object": "chat.completion",
                    "created": created,
                    "model": MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 40, "total_tokens": 70},
                },
            )
            return

        time.sleep(_CFG["exec_delay_s"])
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # 不声明长度，用连接关闭收尾（SSE 的标准做法），每块写完立刻 flush
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        _counter["stream"] += 1
        seq = _counter["stream"]
        if seq <= _CFG["tool_rounds"]:
            _tool_call_response(self, cid, created)
        else:
            _final_response(self, cid, created)
        self.wfile.flush()


def main() -> int:
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(
        f"[fake-model] listening :{PORT} model={MODEL} tool_rounds={TOOL_ROUNDS} "
        f"exec_delay={EXEC_DELAY_S}s chunk_gap={CHUNK_GAP_S}s chunks={CHUNKS}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
