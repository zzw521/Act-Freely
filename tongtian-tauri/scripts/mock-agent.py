#!/usr/bin/env python3
"""
通天办公 - Mock QwenPaw Agent

开发阶段 Mock 服务（开发规范 Mock 开关）：
- 模拟 qwenpaw 的 HTTP/SSE 接口（GET /health, POST /api/chat）
- 纯 Python 标准库实现，零依赖，开箱即用
- 由 Rust AgentBridge 在 TT_AGENT_MOCK=1 时自动拉起

三层架构演进 P0（与 engine/server.py 契约对齐，黄金 SSE fixture 同源）：
- 每任务单调 seq，SSE 输出 `id: {task_id}:{seq}` 行，seq 写入 data
- GET  /api/events/replay?task_id=&from_seq=  事件重放（内存窗口）
- POST /api/chat/cancel                       取消任务
- goal 测试触发器（集成测试专项）：
  · [test:burst]   突发 500 事件验证管道吞吐与 seq 无丢失
  · [test:broken]  中途断流模拟（不发 done，验证断线补齐）
  · [test:quota]   产出 quota_warning 事件

用法: python mock-agent.py --port 18000 --work-dir <dir>
"""

import argparse
import json
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


def _force_utf8_stdio() -> None:
    """日志一律按 UTF-8 输出。

    父进程（vitest / 终端）用非 UTF-8 代码页时，Python 的 stdout 会退化成 charmap
    编码，打印中文目标直接抛 UnicodeEncodeError —— 由于 print 发生在请求处理线程里，
    异常会让该请求的连接被直接关闭，客户端只看到 "other side closed"。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_force_utf8_stdio()

# 重放窗口（与 engine/server.py 参数对齐）
REPLAY_WINDOW_MAX_EVENTS = 2048
REPLAY_WINDOW_SECONDS = 10 * 60

# 任务会话注册表：task_id -> {"seq": int, "window": deque, "cancelled": bool}
_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()


def _get_session(task_id):
    with _SESSIONS_LOCK:
        s = _SESSIONS.get(task_id)
        if s is None:
            s = {"seq": 0, "window": deque(maxlen=REPLAY_WINDOW_MAX_EVENTS), "cancelled": False}
            _SESSIONS[task_id] = s
        return s


class MockAgentHandler(BaseHTTPRequestHandler):
    """模拟 QwenPaw v1.1.x API"""

    def log_message(self, fmt, *args):
        print(f"[mock-agent] {fmt % args}", flush=True)

    # ── 工具方法 ──────────────────────────────────────────

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, session, task_id, event, data):
        """带单调 seq 的 SSE 输出（黄金 fixture 格式：id/event/data 三行）"""
        session["seq"] += 1
        seq = session["seq"]
        data = {**data, "seq": seq, "taskId": task_id}
        session["window"].append((seq, time.time(), event, data))
        chunk = (
            f"id: {task_id}:{seq}\n"
            f"event: {event}\n"
            f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        )
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()

    # ── 路由 ──────────────────────────────────────────────

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, {
                "status": "ok",
                "version": "1.1.11",
                "api_version": "1",
                "uptime": time.time(),
                "mock": True,
            })
        elif parsed.path == "/api/events/replay":
            self._handle_replay(parse_qs(parsed.query))
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        # 任何未预期异常都要回一个错误响应：直接让线程崩掉的话，客户端只能看到
        # 连接被关（"other side closed"），排查时拿不到任何原因
        try:
            self._route_post()
        except Exception as exc:  # noqa: BLE001 - Mock 服务需要把失败原因吐给调用方
            print(f"[mock-agent] POST {self.path} failed: {exc}", flush=True)
            try:
                self._send_json(500, {"error": type(exc).__name__, "detail": str(exc)})
            except Exception:
                pass

    def _route_post(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/chat":
            self._handle_chat()
        elif parsed.path == "/api/chat/review":
            self._handle_review()
        elif parsed.path == "/api/chat/control":
            self._handle_control()
        elif parsed.path == "/api/chat/cancel":
            self._handle_cancel()
        elif parsed.path == "/api/config":
            self._send_json(200, {"success": True})
        else:
            self._send_json(404, {"error": "not found"})

    # ── 重放（断线补齐） ───────────────────────────────────

    def _handle_replay(self, query):
        task_id = (query.get("task_id") or [""])[0]
        try:
            from_seq = int((query.get("from_seq") or ["0"])[0])
        except ValueError:
            from_seq = 0
        session = _SESSIONS.get(task_id)
        if session is None:
            self._send_json(200, {"taskId": task_id, "events": [], "windowExpired": True})
            return
        now = time.time()
        events = [
            {"seq": seq, "type": event, "data": data}
            for (seq, ts, event, data) in session["window"]
            if seq > from_seq and now - ts <= REPLAY_WINDOW_SECONDS
        ]
        self._send_json(200, {"taskId": task_id, "events": events, "windowExpired": False})

    # ── 取消 ───────────────────────────────────────────────

    def _handle_cancel(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            task_id = json.loads(raw).get("task_id", "")
        except Exception:
            task_id = ""
        session = _SESSIONS.get(task_id)
        if session is None:
            self._send_json(200, {"taskId": task_id, "cancelled": False, "reason": "TASK_NOT_FOUND"})
            return
        session["cancelled"] = True
        self._send_json(200, {"taskId": task_id, "cancelled": True})

    # ── 两阶段协议辅助（核心闭环补齐 流 5） ────────────────

    def _mock_plan_steps(self, goal: str):
        return [
            {"step_id": "step-1", "type": "llm_call", "description": "解析任务目标"},
            {"step_id": "step-2", "type": "llm_call", "description": f"处理: {goal[:50]}"},
            {"step_id": "step-3", "type": "file_write", "description": "生成 Mock 交付文件"},
        ]

    def _sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def _exec_flow(self, session, task_id: str, goal: str):
        """阶段二执行流：status_change(executing) → steps → deliver → done"""
        self._send_sse(session, task_id, "status_change", {"taskId": task_id, "status": "executing"})
        self._send_sse(session, task_id, "step_start", {"step_id": "step-1", "description": "解析任务目标"})
        time.sleep(0.3)
        self._send_sse(session, task_id, "step_progress", {"step_id": "step-1", "message": "正在分析意图..."})
        time.sleep(0.3)
        self._send_sse(session, task_id, "step_done", {
            "step_id": "step-1", "output": "目标解析完成",
            "usage": {"promptTokens": 800, "completionTokens": 400},
        })
        if "[test:quota]" in goal:
            self._send_sse(session, task_id, "quota_warning", {
                "level": "85", "action": "degrade", "usedTokens": 170000, "quotaTokens": 200000,
            })
        self._send_sse(session, task_id, "step_start", {"step_id": "step-2", "description": "执行核心处理"})
        time.sleep(0.3)
        if session["cancelled"]:
            self._send_sse(session, task_id, "status_change", {
                "taskId": task_id, "status": "cancelled", "statusReasonCode": "USER_CANCELLED",
            })
            self._send_sse(session, task_id, "done", {})
            return
        self._send_sse(session, task_id, "step_done", {
            "step_id": "step-2", "output": "处理完成",
            "usage": {"promptTokens": 1200, "completionTokens": 600},
        })
        self._send_sse(session, task_id, "step_start", {"step_id": "step-3", "description": "生成交付文件"})
        time.sleep(0.3)
        self._send_sse(session, task_id, "step_done", {"step_id": "step-3", "output": "文件已生成"})
        self._send_sse(session, task_id, "deliver", {
            "summary": f"Mock 任务完成: {goal[:30]}",
            "artifacts": ["Mock成果报告.md", "Mock数据摘要.xlsx"],
        })
        self._send_sse(session, task_id, "done", {})

    def _handle_review(self):
        """计划审阅：approve → 执行流 / reject → done(REJECTED) / modify → 重新计划"""
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        task_id = (payload.get("task_id") or "").strip()
        action = (payload.get("action") or "").strip()
        patch = payload.get("patch") or []
        session = _SESSIONS.get(task_id)
        if session is None:
            self._send_json(404, {"ok": False, "code": "TASK_NOT_FOUND", "taskId": task_id})
            return
        goal = session.get("goal", "")

        self._sse_headers()
        try:
            if action == "approve":
                self._exec_flow(session, task_id, goal)
            elif action == "reject":
                self._send_sse(session, task_id, "status_change", {
                    "taskId": task_id, "status": "done", "statusReasonCode": "REJECTED",
                })
                self._send_sse(session, task_id, "done", {})
            elif action == "modify":
                steps = [dict(s) for s in session.get("plan") or self._mock_plan_steps(goal)]
                if isinstance(patch, list):
                    pmap = {p.get("id"): p for p in patch if isinstance(p, dict) and p.get("id")}
                    for s in steps:
                        p = pmap.get(s.get("step_id"))
                        if p and (p.get("name") or p.get("description")):
                            s["description"] = str(p.get("name") or p.get("description"))
                session["plan"] = steps
                self._send_sse(session, task_id, "plan", {
                    "plan_id": f"mock-plan-{task_id}", "steps": steps,
                })
                self._send_sse(session, task_id, "status_change", {
                    "taskId": task_id, "status": "pending_review",
                })
                self._send_sse(session, task_id, "done", {})
            else:
                self._send_sse(session, task_id, "error", {
                    "code": "VALIDATION_ERROR", "message": f"未知 action: {action}",
                })
                self._send_sse(session, task_id, "done", {})
        except (BrokenPipeError, ConnectionResetError):
            print("[mock-agent] client disconnected", flush=True)

    def _handle_control(self):
        """任务控制：pause/resume/rollback_to（状态语义，文件恢复随备份模块）"""
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        task_id = (payload.get("task_id") or "").strip()
        action = (payload.get("action") or "").strip()
        step_id = (payload.get("step_id") or "").strip()
        session = _SESSIONS.get(task_id)
        if session is None:
            self._send_json(200, {"ok": False, "code": "TASK_NOT_FOUND", "taskId": task_id})
            return
        if action in ("pause", "resume"):
            self._send_json(200, {"ok": True, "taskId": task_id, "action": action})
            return
        if action == "rollback_to":
            session["cancelled"] = True  # 终止进行中的执行流
            self._send_json(200, {"ok": True, "taskId": task_id, "action": action, "stepId": step_id})
            return
        self._send_json(200, {"ok": False, "code": "VALIDATION_ERROR", "message": f"未知 action: {action}"})

    # ── 聊天流（两阶段协议 + 兼容全流程） ──────────────────

    def _handle_chat(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {}
        goal = payload.get("message", "")
        task_id = payload.get("task_id") or f"mock-task-{int(time.time() * 1000)}"
        session = _get_session(task_id)
        session["goal"] = goal

        print(f"[mock-agent] chat goal: {goal} (task={task_id})", flush=True)

        # 两阶段协议：stage=plan 仅生成计划待审阅（核心闭环补齐）
        if payload.get("stage") == "plan":
            steps = self._mock_plan_steps(goal)
            session["plan"] = steps
            self._sse_headers()
            try:
                self._send_sse(session, task_id, "status_change", {"taskId": task_id, "status": "queued", "queuePosition": 0})
                self._send_sse(session, task_id, "status_change", {"taskId": task_id, "status": "planning"})
                time.sleep(0.2)
                self._send_sse(session, task_id, "plan", {"plan_id": f"mock-plan-{task_id}", "steps": steps})
                self._send_sse(session, task_id, "status_change", {"taskId": task_id, "status": "pending_review"})
                self._send_sse(session, task_id, "done", {})
            except (BrokenPipeError, ConnectionResetError):
                print("[mock-agent] client disconnected", flush=True)
            return

        self._sse_headers()

        try:
            # ── 专项测试触发器 ──
            if "[test:burst]" in goal:
                # 突发 500 事件：验证批量 emit 管道吞吐与 seq 无丢失
                for i in range(500):
                    self._send_sse(session, task_id, "step_progress",
                                   {"step_id": f"burst-{i}", "message": f"burst {i}"})
                self._send_sse(session, task_id, "done", {})
                return
            if "[test:broken]" in goal:
                # 断流模拟：发 3 条后直接关流，不发 done（验证断线补齐链路）
                for i in range(3):
                    self._send_sse(session, task_id, "step_progress",
                                   {"step_id": f"s-{i}", "message": "before break"})
                return

            # 留出前端事件订阅窗口，避免首个事件丢失
            time.sleep(0.8)

            # 1. 计划
            self._send_sse(session, task_id, "plan", {
                "plan_id": f"mock-plan-{int(time.time())}",
                "steps": [
                    {"step_id": "step-1", "type": "llm_call", "description": "解析任务目标"},
                    {"step_id": "step-2", "type": "llm_call", "description": f"处理: {goal[:50]}"},
                    {"step_id": "step-3", "type": "file_write", "description": "生成 Mock 交付文件"},
                ],
            })
            time.sleep(0.6)

            # 2. 逐步执行（step_done 携带 usage 供配额计量）
            self._send_sse(session, task_id, "step_start", {"step_id": "step-1", "description": "解析任务目标"})
            time.sleep(0.5)
            self._send_sse(session, task_id, "step_progress", {"step_id": "step-1", "message": "正在分析意图..."})
            time.sleep(0.5)
            self._send_sse(session, task_id, "step_done", {
                "step_id": "step-1", "output": "目标解析完成",
                "usage": {"promptTokens": 800, "completionTokens": 400},
            })

            # [test:quota] 触发配额预警事件
            if "[test:quota]" in goal:
                self._send_sse(session, task_id, "quota_warning", {
                    "level": "85", "action": "degrade", "usedTokens": 170000, "quotaTokens": 200000,
                })

            self._send_sse(session, task_id, "step_start", {"step_id": "step-2", "description": "执行核心处理"})
            time.sleep(0.6)
            if session["cancelled"]:
                self._send_sse(session, task_id, "status_change", {
                    "taskId": task_id, "status": "cancelled", "statusReasonCode": "USER_CANCELLED",
                })
                self._send_sse(session, task_id, "done", {})
                return
            self._send_sse(session, task_id, "step_progress", {"step_id": "step-2", "message": "Mock 引擎处理中..."})
            time.sleep(0.6)
            self._send_sse(session, task_id, "step_done", {
                "step_id": "step-2", "output": "处理完成",
                "usage": {"promptTokens": 1200, "completionTokens": 600},
            })

            self._send_sse(session, task_id, "step_start", {"step_id": "step-3", "description": "生成交付文件"})
            time.sleep(0.5)
            self._send_sse(session, task_id, "step_done", {"step_id": "step-3", "output": "文件已生成"})

            # 3. 交付
            self._send_sse(session, task_id, "deliver", {
                "summary": f"Mock 任务完成: {goal[:30]}",
                "artifacts": ["Mock成果报告.md", "Mock数据摘要.xlsx"],
            })
            time.sleep(0.3)

            # 4. 结束
            self._send_sse(session, task_id, "done", {})
        except (BrokenPipeError, ConnectionResetError):
            print("[mock-agent] client disconnected", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Mock QwenPaw Agent")
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--work-dir", type=str, default=".")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), MockAgentHandler)
    print(f"[mock-agent] listening on 127.0.0.1:{args.port} (work-dir: {args.work_dir})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
