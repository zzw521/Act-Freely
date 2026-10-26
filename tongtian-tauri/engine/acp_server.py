#!/usr/bin/env python3
"""ACP 服务端：让外部 IDE / 终端通过 Agent Client Protocol 使用通天办公 Agent。

用法（在 Zed 等客户端的 ACP agent 配置里填写）：
    python D:/path/to/tongtian-tauri/engine/acp_server.py

- JSON-RPC 2.0，按行分隔（newline-delimited JSON），stdin/stdout。
- 只做 text prompt；图片/音频块会明确返回不支持，不静默丢弃。
- 实际执行仍走本机引擎 /api/chat（SSE），本进程只做协议适配与取消转发。
- 引擎地址优先级：--engine-url > TT_ENGINE_URL > 探测 127.0.0.1:18000-18010。

边界：
- 不携带桌面端的意识/技能/专家运行时，也不读写桌面端 SQLite；模型由引擎侧的
  models.json 默认项或 .env 决定。
- 不实现 session/load 与工具调用回放；agentCapabilities.loadSession=false。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PROTOCOL_VERSION = 1
DISCOVER_RANGE = range(18000, 18011)


def log(message: str) -> None:
    print(f"[acp] {message}", file=sys.stderr, flush=True)


def _engine_healthy(base: str, timeout: float = 0.25) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
            return data.get("status") == "ok"
    except Exception:
        return False


def discover_engine(explicit: str | None) -> str | None:
    candidates: list[str] = []
    if explicit:
        candidates.append(explicit.rstrip("/"))
    env = str(os.environ.get("TT_ENGINE_URL") or "").strip()
    if env:
        candidates.append(env.rstrip("/"))
    for base in candidates:
        if _engine_healthy(base):
            return base
        log(f"引擎不可达：{base}")
    for port in DISCOVER_RANGE:
        base = f"http://127.0.0.1:{port}"
        if _engine_healthy(base):
            return base
    return None


class Session:
    def __init__(self, session_id: str, cwd: str) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.task_id = f"acp-{session_id}"
        self.history: list[dict] = []
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.response = None


class AcpServer:
    def __init__(self, engine_url: str | None) -> None:
        self.engine_url = engine_url
        self.sessions: dict[str, Session] = {}
        self.write_lock = threading.Lock()
        self.threads: list[threading.Thread] = []

    # ── 输出 ──────────────────────────────────────────────────
    def _send(self, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self.write_lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    def result(self, req_id, result) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def error(self, req_id, code: int, message: str, data=None) -> None:
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self._send({"jsonrpc": "2.0", "id": req_id, "error": err})

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # ── 分发 ──────────────────────────────────────────────────
    def handle(self, message: dict) -> None:
        method = message.get("method")
        req_id = message.get("id")
        params = message.get("params") or {}
        is_notification = "id" not in message
        if not isinstance(method, str):
            if not is_notification:
                self.error(req_id, -32600, "无效请求")
            return
        try:
            if method == "initialize":
                self.result(req_id, {
                    "protocolVersion": PROTOCOL_VERSION,
                    "agentCapabilities": {
                        "loadSession": False,
                        "promptCapabilities": {
                            "image": False,
                            "audio": False,
                            "embeddedContext": False,
                        },
                    },
                    "authMethods": [],
                })
            elif method == "authenticate":
                # 本机引擎不需要 ACP 层鉴权；桌面端的模型密钥不经过这里
                self.result(req_id, {})
            elif method == "session/new":
                self._session_new(req_id, params)
            elif method == "session/prompt":
                self._session_prompt_async(req_id, params)
            elif method == "session/cancel":
                self._session_cancel(params)
                if not is_notification:
                    self.result(req_id, {})
            elif method == "session/load":
                self.error(
                    req_id, -32601,
                    "ACP_SESSION_LOAD_UNSUPPORTED: 本 Agent 不支持恢复历史会话，请新建会话",
                )
            else:
                if not is_notification:
                    self.error(req_id, -32601, f"未知方法：{method}")
        except Exception as exc:  # noqa: BLE001
            log(f"处理 {method} 失败：{exc}")
            if not is_notification:
                self.error(req_id, -32603, f"内部错误：{exc}")

    def _session_new(self, req_id, params: dict) -> None:
        cwd = str(params.get("cwd") or "").strip()
        if not cwd:
            self.error(req_id, -32602, "session/new 缺少 cwd")
            return
        path = Path(cwd).expanduser()
        if not path.is_dir():
            self.error(req_id, -32602, f"ACP_CWD_INVALID: 工作目录不存在：{cwd}")
            return
        session = Session(uuid.uuid4().hex, str(path.resolve()))
        self.sessions[session.session_id] = session
        self.result(req_id, {"sessionId": session.session_id})

    def _session_cancel(self, params: dict) -> None:
        session = self.sessions.get(str(params.get("sessionId") or ""))
        if session is None:
            return
        session.cancel.set()
        # 先通知引擎停止，再断开本地 SSE 连接：先 close 可能阻塞在读操作上，
        # 让引擎继续跑完这一轮，取消就只发生在客户端一侧。
        self._post_json("/api/chat/cancel", {"task_id": session.task_id})
        with session.lock:
            resp = session.response
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def _session_prompt_async(self, req_id, params: dict) -> None:
        session = self.sessions.get(str(params.get("sessionId") or ""))
        if session is None:
            self.error(req_id, -32602, "ACP_SESSION_NOT_FOUND: 会话不存在，请先 session/new")
            return
        prompt = params.get("prompt")
        if not isinstance(prompt, list) or not prompt:
            self.error(req_id, -32602, "prompt 必须是非空内容块数组")
            return
        texts: list[str] = []
        for block in prompt:
            if not isinstance(block, dict):
                self.error(req_id, -32602, "prompt 内容块格式无效")
                return
            kind = block.get("type")
            if kind == "text":
                texts.append(str(block.get("text") or ""))
            else:
                self.error(
                    req_id, -32602,
                    f"ACP_UNSUPPORTED_CONTENT: 当前只支持 text 内容，收到 {kind}",
                )
                return
        text = "\n".join(t for t in texts if t).strip()
        if not text:
            self.error(req_id, -32602, "ACP_EMPTY_PROMPT: 提问正文为空")
            return
        if self.engine_url is None:
            self.engine_url = discover_engine(None)
        if self.engine_url is None:
            self.error(
                req_id, -32000,
                "ACP_ENGINE_UNAVAILABLE: 未找到本机引擎，请先启动通天办公桌面端，或设置 TT_ENGINE_URL",
            )
            return
        thread = threading.Thread(
            target=self._run_prompt, args=(req_id, session, text), daemon=True
        )
        thread.start()
        self.threads.append(thread)

    # ── 引擎桥接 ──────────────────────────────────────────────
    def _post_json(self, path: str, body: dict) -> None:
        if self.engine_url is None:
            return
        try:
            req = urllib.request.Request(
                f"{self.engine_url}{path}",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as exc:  # noqa: BLE001
            log(f"回调 {path} 失败：{exc}")

    def _run_prompt(self, req_id, session: Session, text: str) -> None:
        session.cancel.clear()
        body = {
            "message": text,
            "task_id": session.task_id,
            "workDir": session.cwd,
            "history": list(session.history),
        }
        final_text = ""
        error: dict | None = None
        cancelled = False
        assistant_note = ""
        try:
            req = urllib.request.Request(
                f"{self.engine_url}/api/chat",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=600)
            with session.lock:
                session.response = resp
            event, data_buf = "", []
            while True:
                if session.cancel.is_set():
                    cancelled = True
                    break
                raw = resp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data_buf.append(line[5:].strip())
                elif line == "":
                    if event and data_buf:
                        payload = _loads("\n".join(data_buf))
                        if payload is not None:
                            note = self._apply_event(session, event, payload)
                            if note == "deliver":
                                final_text = str(payload.get("final_text") or "")
                            elif note == "cancelled":
                                cancelled = True
                            elif note == "error":
                                error = payload
                    event, data_buf = "", []
            try:
                resp.close()
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            if session.cancel.is_set():
                cancelled = True
            else:
                error = {"code": "ACP_ENGINE_FAILED", "message": str(exc)}
        finally:
            with session.lock:
                session.response = None

        if cancelled:
            self.result(req_id, {"stopReason": "cancelled"})
            return
        if error is not None:
            self.error(
                req_id, -32000,
                f"{error.get('code') or 'ACP_TASK_FAILED'}: {error.get('message') or '任务执行失败'}",
            )
            return
        if final_text.strip():
            self.notify("session/update", {
                "sessionId": session.session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": final_text},
                },
            })
        else:
            assistant_note = "（本轮没有产出正文）"
            self.notify("session/update", {
                "sessionId": session.session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": assistant_note},
                },
            })
        session.history.append({"role": "user", "content": text})
        session.history.append({
            "role": "assistant",
            "content": final_text.strip() or assistant_note,
        })
        self.result(req_id, {"stopReason": "end_turn"})

    def _apply_event(self, session: Session, event: str, data: dict) -> str:
        if event == "plan":
            steps = data.get("steps") or []
            lines = []
            for i, step in enumerate(steps, 1):
                if isinstance(step, dict):
                    lines.append(f"{i}. {step.get('description') or step.get('step_id') or ''}")
            if lines:
                self._thought(session, "计划：\n" + "\n".join(lines))
        elif event == "step_start":
            desc = str(data.get("description") or "").strip()
            if desc:
                self._thought(session, f"▶ {desc}")
        elif event == "step_done":
            out = str(data.get("output") or "").strip()
            if out and len(out) <= 500:
                self._thought(session, f"✓ {out}")
        elif event == "deliver":
            artifacts = data.get("artifacts") or []
            if artifacts:
                names = "、".join(Path(str(p)).name for p in artifacts[:8])
                self._thought(session, f"交付物：{names}")
            return "deliver"
        elif event == "error":
            return "error"
        elif event == "status_change" and data.get("status") == "cancelled":
            return "cancelled"
        return ""

    def _thought(self, session: Session, text: str) -> None:
        self.notify("session/update", {
            "sessionId": session.session_id,
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": text},
            },
        })

    # ── 主循环 ────────────────────────────────────────────────
    def serve(self) -> None:
        log(f"ACP 服务端已启动（engine={self.engine_url or '待发现'}）")
        while True:
            raw = sys.stdin.readline()
            if not raw:
                break
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except Exception:
                self._send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON 解析失败"}})
                continue
            if isinstance(message, list):
                for item in message:
                    if isinstance(item, dict):
                        self.handle(item)
            elif isinstance(message, dict):
                self.handle(message)


def _loads(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="通天办公 ACP 服务端（stdio）")
    parser.add_argument("--engine-url", default=None, help="本机引擎地址，如 http://127.0.0.1:18000")
    args = parser.parse_args()
    engine = discover_engine(args.engine_url)
    if engine is None:
        log("暂未发现本机引擎；启动桌面端后可继续，session/prompt 会重新探测")
    server = AcpServer(engine)
    try:
        server.serve()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sys.stdout.flush()
        except Exception:
            pass
        log("ACP 服务端已退出")


if __name__ == "__main__":
    main()
