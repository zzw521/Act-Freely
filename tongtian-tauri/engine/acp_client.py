"""ACP 客户端：把编码子任务委派给外部编码 Agent（Claude Code / OpenCode / Codex 等）。

协议：Agent Client Protocol，JSON-RPC 2.0，按行分隔（newline-delimited），
通过子进程 stdin/stdout 通信。启动命令只来自用户设置，模型不能指定。

边界：
- 只实现本机引擎当前需要的能力：initialize → session/new → session/prompt，
  并把外部 Agent 的 `session/update` 通知汇总成文本结果。
- 外部 Agent 请求的 `fs/*` 读写被限制在本次任务的工作目录内；
  `terminal/*` 一律回复方法不存在（clientCapabilities.terminal=false），
  外部 Agent 应改用自带的工具能力。
- `session/request_permission` 按用户设置处理：默认拒绝（deny），
  用户显式选「自动放行」后才选择 allow 类选项。每次决定写入审计。
- 超时或取消时杀进程树，不留孤儿进程。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import sandbox_launcher as sandbox

MAX_TIMEOUT_SECONDS = 1800
DEFAULT_TIMEOUT_SECONDS = 600
HANDSHAKE_TIMEOUT_SECONDS = 30
MAX_OUTPUT_CHARS = 20_000
MAX_FS_BYTES = 2 * 1024 * 1024
MAX_TRACE_LINES = 20
PERMISSIONS = ("deny", "allow")


class AcpError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _split_command(raw: str) -> list[str]:
    """解析用户在设置里填的启动命令（不经过 shell）。

    Windows 用系统自带的 CommandLineToArgvW 解析引号与反斜杠，
    避免 shlex 把 `C:\\Python\\python.exe` 里的反斜杠当转义吃掉。
    """
    raw = str(raw or "").strip()
    if not raw:
        return []
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            shell32.CommandLineToArgvW.argtypes = [
                wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int),
            ]
            shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
            kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
            kernel32.LocalFree.restype = wintypes.HLOCAL
            argc = ctypes.c_int()
            argv = shell32.CommandLineToArgvW(raw, ctypes.byref(argc))
            if argv:
                try:
                    return [argv[i] for i in range(argc.value)]
                finally:
                    kernel32.LocalFree(ctypes.cast(argv, wintypes.HLOCAL))
        except Exception:
            pass
    try:
        return shlex.split(raw, posix=True)
    except ValueError:
        return []


def _brief(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class AcpClient:
    """一次性的 ACP 会话：每次委派都新建进程与会话，结束即回收。"""

    def __init__(
        self,
        command: list[str],
        cwd: str,
        *,
        permission: str = "deny",
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        audit=None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.permission = permission if permission in PERMISSIONS else "deny"
        self.timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS))
        self.audit = audit
        self.proc: subprocess.Popen | None = None
        self._send_lock = threading.Lock()
        self._pending: dict[int, list] = {}
        self._pending_lock = threading.Lock()
        self._next_id = 0
        self._closed = threading.Event()
        self._output: list[str] = []
        self._tool_trace: list[str] = []
        self._fs_writes = 0
        self._stderr_tail: list[str] = []
        self.stop_reason = ""

    # ── 进程 ──────────────────────────────────────────────────

    def start(self) -> None:
        exe = Path(self.command[0])
        # 绝对路径直接校验；裸命令交给 PATH 解析（spawn 失败会给出可读错误）
        if exe.is_absolute() and not exe.exists():
            raise AcpError("ACP_COMMAND_NOT_FOUND", f"启动命令不存在：{self.command[0]}")
        env = {
            k: v
            for k, v in os.environ.items()
            if not (k.startswith("TT_") and ("TOKEN" in k.upper() or "SECRET" in k.upper()))
        }
        env["PYTHONIOENCODING"] = "utf-8"
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        try:
            wrapped = sandbox.wrap(
                self.command[0], list(self.command[1:]), workspace=self.cwd
            )
        except sandbox.SandboxUnavailable as exc:
            raise AcpError("ACP_SANDBOX_FAILED", str(exc)) from None
        argv = [wrapped[0], *wrapped[1]] if wrapped else self.command
        try:
            self.proc = subprocess.Popen(
                argv,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                **kwargs,
            )
        except FileNotFoundError:
            raise AcpError(
                "ACP_COMMAND_NOT_FOUND",
                f"找不到外部 Agent 启动命令：{self.command[0]}（请在设置里检查命令与 PATH）",
            ) from None
        except OSError as exc:
            raise AcpError("ACP_SPAWN_FAILED", f"外部 Agent 启动失败：{exc}") from None
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except Exception:
                    # 有些实现会往 stdout 打日志；忽略但不是协议消息
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and "method" in message:
                    self._handle_request(message)
                elif "id" in message:
                    self._resolve(message)
                elif "method" in message:
                    self._handle_notification(message)
        except (ValueError, OSError):
            pass
        self._closed.set()
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for slot in pending:
            slot[1] = {"error": {"code": "ACP_PROCESS_EXITED", "message": self._exit_note()}}
            slot[0].set()

    def _read_stderr(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in proc.stderr:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self._stderr_tail.append(line)
                    if len(self._stderr_tail) > 20:
                        self._stderr_tail.pop(0)
        except (ValueError, OSError):
            pass

    def _exit_note(self) -> str:
        code = self.proc.poll() if self.proc is not None else None
        tail = " | ".join(self._stderr_tail[-3:])
        note = f"外部 Agent 进程已退出（exit={code}）"
        return f"{note}：{_brief(tail, 300)}" if tail else note

    # ── JSON-RPC ──────────────────────────────────────────────

    def _send(self, payload: dict) -> None:
        proc = self.proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise AcpError("ACP_PROCESS_EXITED", self._exit_note())
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self._send_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                raise AcpError("ACP_PROCESS_EXITED", self._exit_note()) from None

    def request(self, method: str, params: dict, timeout: float) -> dict:
        if self._closed.is_set():
            raise AcpError("ACP_PROCESS_EXITED", self._exit_note())
        with self._pending_lock:
            self._next_id += 1
            req_id = self._next_id
            slot = [threading.Event(), None]
            self._pending[req_id] = slot
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        if not slot[0].wait(timeout):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise AcpError("ACP_TIMEOUT", f"外部 Agent 在 {int(timeout)} 秒内没有响应 {method}")
        with self._pending_lock:
            self._pending.pop(req_id, None)
        message = slot[1] or {}
        error = message.get("error")
        if isinstance(error, dict):
            raise AcpError(
                str(error.get("code") or "ACP_RPC_ERROR"),
                _brief(str(error.get("message") or "外部 Agent 返回错误"), 500),
            )
        return message.get("result") or {}

    def _resolve(self, message: dict) -> None:
        req_id = message.get("id")
        with self._pending_lock:
            slot = self._pending.get(req_id)
        if slot is not None:
            slot[1] = message
            slot[0].set()

    # ── 外部 Agent 发来的请求 ─────────────────────────────────

    def _handle_request(self, message: dict) -> None:
        req_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params") or {}
        try:
            result = self._serve_request(method, params)
        except AcpError as exc:
            self._reply_error(req_id, -32000, exc.message, exc.code)
            return
        except Exception as exc:  # noqa: BLE001
            self._reply_error(req_id, -32603, f"ACP 客户端内部错误：{exc}", "ACP_CLIENT_ERROR")
            return
        self._reply(req_id, result)

    def _serve_request(self, method: str, params: dict):
        if method == "fs/read_text_file":
            return self._fs_read(params)
        if method == "fs/write_text_file":
            return self._fs_write(params)
        if method == "session/request_permission":
            return self._permission(params)
        if method.startswith("terminal/"):
            # clientCapabilities.terminal=false；明确拒绝而不是假装执行
            raise AcpError("ACP_TERMINAL_UNSUPPORTED", "本客户端不提供终端能力，请使用外部 Agent 自带的工具")
        raise AcpError("ACP_METHOD_NOT_FOUND", f"本客户端不支持 {method}")

    def _root(self) -> Path:
        return Path(self.cwd).resolve()

    def _resolve_path(self, raw: str) -> Path:
        p = Path(str(raw or "")).expanduser()
        path = (self._root() / p).resolve() if not p.is_absolute() else p.resolve()
        try:
            path.relative_to(self._root())
        except ValueError:
            self._audit("fs_denied", {"path": _brief(str(path), 300)})
            raise AcpError("ACP_PATH_DENIED", f"路径不在工作目录内：{path}") from None
        return path

    def _fs_read(self, params: dict) -> dict:
        path = self._resolve_path(str(params.get("path") or ""))
        if not path.is_file():
            raise AcpError("ACP_FILE_NOT_FOUND", f"文件不存在：{path}")
        if path.stat().st_size > MAX_FS_BYTES:
            raise AcpError("ACP_FILE_TOO_LARGE", f"文件超过 {MAX_FS_BYTES // 1024} KB，拒绝读取")
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise AcpError("ACP_FILE_READ_FAILED", f"读取失败：{exc}") from None
        start = params.get("line")
        limit = params.get("limit")
        if isinstance(start, int) and start > 0:
            lines = content.splitlines(True)
            content = "".join(lines[start - 1 : start - 1 + int(limit)] if isinstance(limit, int) else lines[start - 1 :])
        self._audit("fs_read", {"path": _brief(str(path), 300)})
        return {"content": content}

    def _fs_write(self, params: dict) -> dict:
        path = self._resolve_path(str(params.get("path") or ""))
        content = str(params.get("content") or "")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise AcpError("ACP_FILE_WRITE_FAILED", f"写入失败：{exc}") from None
        self._fs_writes += 1
        self._audit("fs_write", {"path": _brief(str(path), 300), "bytes": len(content.encode("utf-8"))})
        return {}

    def _permission(self, params: dict) -> dict:
        options = params.get("options") if isinstance(params.get("options"), list) else []
        wanted = "allow" if self.permission == "allow" else "reject"
        chosen = None
        for option in options:
            if not isinstance(option, dict):
                continue
            kind = str(option.get("kind") or "")
            if kind.startswith(wanted):
                chosen = option
                # allow_once / reject_once 优先，避免默认永久放行
                if kind.endswith("_once"):
                    break
        tool = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
        self._audit("permission", {
            "decision": "allow" if chosen and wanted == "allow" else "deny",
            "tool": _brief(str(tool.get("title") or tool.get("toolCallId") or ""), 200),
        })
        if chosen is None:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": chosen.get("optionId")}}

    def _reply(self, req_id, result) -> None:
        try:
            self._send({"jsonrpc": "2.0", "id": req_id, "result": result})
        except AcpError:
            pass

    def _reply_error(self, req_id, code: int, message: str, data: str) -> None:
        try:
            self._send({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": code, "message": message, "data": data},
            })
        except AcpError:
            pass

    # ── 通知 ──────────────────────────────────────────────────

    def _handle_notification(self, message: dict) -> None:
        if message.get("method") != "session/update":
            return
        update = (message.get("params") or {}).get("update") or {}
        kind = str(update.get("sessionUpdate") or "")
        content = update.get("content") if isinstance(update.get("content"), dict) else {}
        if kind == "agent_message_chunk":
            text = str(content.get("text") or "")
            if text:
                self._output.append(text)
        elif kind == "tool_call":
            title = str(update.get("title") or update.get("kind") or "").strip()
            if title and len(self._tool_trace) < MAX_TRACE_LINES:
                self._tool_trace.append(_brief(title, 160))

    # ── 会话流程 ──────────────────────────────────────────────

    def run_prompt(self, task: str) -> dict:
        started = time.monotonic()
        self.start()
        try:
            init = self.request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": True, "writeTextFile": True},
                        "terminal": False,
                    },
                },
                HANDSHAKE_TIMEOUT_SECONDS,
            )
            auth_methods = init.get("authMethods") if isinstance(init.get("authMethods"), list) else []
            if auth_methods:
                raise AcpError(
                    "ACP_AUTH_REQUIRED",
                    "外部 Agent 要求先认证；请在外部 Agent 自己的配置里完成登录后重试",
                )
            session = self.request(
                "session/new",
                {"cwd": self.cwd, "mcpServers": []},
                HANDSHAKE_TIMEOUT_SECONDS,
            )
            session_id = str(session.get("sessionId") or "")
            if not session_id:
                raise AcpError("ACP_NO_SESSION", "外部 Agent 没有返回 sessionId")
            try:
                result = self.request(
                    "session/prompt",
                    {
                        "sessionId": session_id,
                        "prompt": [{"type": "text", "text": task}],
                    },
                    self.timeout,
                )
            except AcpError as exc:
                if exc.code == "ACP_TIMEOUT":
                    self.kill()
                    raise AcpError(
                        "ACP_TIMEOUT",
                        f"外部 Agent 超过 {self.timeout} 秒未完成，进程已终止",
                    ) from None
                raise
            self.stop_reason = str(result.get("stopReason") or "end_turn")
            return {
                "ok": self.stop_reason == "end_turn",
                "stopReason": self.stop_reason,
                "output": self.output_text(),
                "toolTrace": list(self._tool_trace),
                "fsWrites": self._fs_writes,
                "durationMs": int((time.monotonic() - started) * 1000),
            }
        finally:
            self.close()

    def output_text(self) -> str:
        text = "".join(self._output).strip()
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS] + f"\n…（输出超过 {MAX_OUTPUT_CHARS} 字符，已截断）"
        return text

    # ── 回收 ──────────────────────────────────────────────────

    def kill(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                os.killpg(os.getpgid(proc.pid), 9)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def close(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            self.kill()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        self._closed.set()

    def _audit(self, event: str, fields: dict) -> None:
        if self.audit is None:
            return
        try:
            self.audit(event, fields)
        except Exception:
            pass


class AcpRuntime:
    """runtime["acp"] 的消费端：只把用户显式启用的委派能力装进父 Agent。"""

    def __init__(self) -> None:
        self.runtime_present = False
        self.enabled = False
        self.command: list[str] = []
        self.permission = "deny"
        self.timeout = DEFAULT_TIMEOUT_SECONDS
        self.work_dir = ""
        self.audit_path = ""
        self.reason = ""
        self.error = ""

    @classmethod
    def from_runtime(cls, runtime: dict | None, *, work_dir: str = "") -> "AcpRuntime":
        me = cls()
        me.work_dir = str(work_dir or "")
        block = (runtime or {}).get("acp") if isinstance(runtime, dict) else None
        if not isinstance(block, dict):
            me.reason = "未收到 ACP 委派配置（桌面壳可能过旧）"
            return me
        me.runtime_present = True
        me.enabled = block.get("enabled") is True
        me.command = _split_command(str(block.get("command") or ""))
        me.permission = str(block.get("permission") or "deny").strip()
        try:
            me.timeout = int(block.get("timeoutSeconds") or DEFAULT_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            me.timeout = DEFAULT_TIMEOUT_SECONDS
        me.timeout = max(60, min(me.timeout, MAX_TIMEOUT_SECONDS))
        me.work_dir = str(block.get("workDir") or me.work_dir or "")
        me.audit_path = str(block.get("auditPath") or "")
        me.error = str(block.get("error") or "").strip()
        if me.error:
            me.reason = f"ACP 委派配置读取失败：{_brief(me.error, 300)}"
        elif not me.enabled:
            me.reason = "ACP 委派未启用"
        elif not me.command:
            me.reason = "ACP 委派已启用但没有填写外部 Agent 启动命令"
        elif me.permission not in PERMISSIONS:
            me.reason = f"ACP 审批策略非法「{me.permission}」，已按拒绝处理"
            me.permission = "deny"
        return me

    @property
    def ready(self) -> bool:
        return self.runtime_present and not self.reason

    def notes(self) -> list[str]:
        if self.ready:
            return [
                "ACP 委派已启用：可通过 delegate_coding_task 把编码任务交给外部 Agent"
                f"（审批策略：{'自动放行' if self.permission == 'allow' else '拒绝外部工具请求'}）"
            ]
        if self.runtime_present and (self.enabled or self.error):
            return [f"ACP 委派不可用：{self.reason}"]
        return []

    def system_hint(self) -> str:
        if not self.ready:
            return ""
        permission_line = (
            "- 外部 Agent 的权限请求会被自动放行（用户选择了「自动放行」）。"
            if self.permission == "allow"
            else "- 外部 Agent 的权限请求会被拒绝；如果它因此无法完成任务，如实说明，不要改用其他途径绕过。"
        )
        return (
            "\n\n## 外部编码 Agent（ACP，用户已启用）\n"
            "- delegate_coding_task：把一个边界清晰的编码子任务交给外部编码 Agent 执行"
            "（改代码、跑测试、修构建）。任务描述要自包含：目标、涉及文件/模块、验收标准、"
            "以及允许它改动的范围。\n"
            "- 外部 Agent 只能在本任务的工作目录内读写文件；它的改动直接落盘，"
            "不会自动进入本会话的交付物清单，完成后请自己核对或读取关键文件。\n"
            "- 不要在任务描述里放密钥、口令或与编码无关的隐私数据。\n"
            "- 普通问答、写文档、做表格不要委派；只有在需要真实改代码/跑测试时才用。\n"
            + permission_line
        )

    def _audit(self, event: str, fields: dict, task_id: str) -> None:
        if not self.audit_path:
            return
        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": event,
            "taskId": task_id,
            "command": Path(self.command[0]).name if self.command else "",
            "cwd": self.work_dir,
            **fields,
        }
        try:
            path = Path(self.audit_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def install(self, toolkit, emit=None, task_id: str = "") -> None:
        from agentscope.tool import ToolResponse
        from agentscope.message import TextBlock

        def _event(step_id: str, message: str) -> None:
            if emit is not None:
                try:
                    emit("step_progress", {"step_id": step_id, "message": message})
                except Exception:
                    pass

        def delegate_coding_task(task: str, cwd: str = ""):
            """把编码子任务交给外部 ACP 编码 Agent 执行。

            外部 Agent 会在工作目录内直接读写文件、修改代码并运行它自己的工具；
            完成后返回它的最终说明与改动概况。

            Args:
                task (`str`):
                    自包含的任务描述：目标、涉及的文件/模块、验收标准、允许改动的范围。
                    不要包含密钥、口令或与编码无关的隐私数据。
                cwd (`str`):
                    可选，相对本任务工作目录的子目录；省略则使用任务工作目录。
            """
            root = Path(self.work_dir or os.getcwd()).resolve()
            if cwd.strip():
                target = (root / cwd).resolve() if not Path(cwd).is_absolute() else Path(cwd).resolve()
                try:
                    target.relative_to(root)
                except ValueError:
                    return ToolResponse(content=[TextBlock(
                        type="text", text=f"错误：cwd「{cwd}」不在本任务工作目录内，拒绝委派。"
                    )])
                if not target.is_dir():
                    return ToolResponse(content=[TextBlock(
                        type="text", text=f"错误：cwd「{cwd}」不存在或不是目录。"
                    )])
            else:
                target = root
            text = str(task or "").strip()
            if len(text) < 8:
                return ToolResponse(content=[TextBlock(
                    type="text", text="错误：任务描述太短，请写清目标、涉及文件与验收标准。"
                )])
            started = time.monotonic()

            def audit(event: str, fields: dict) -> None:
                self._audit(event, fields, task_id)
                if emit is not None:
                    try:
                        emit("tool_audit", {
                            "tool": "delegate_coding_task",
                            "event": event,
                            "taskId": task_id,
                            **fields,
                        })
                    except Exception:
                        pass

            _event("acp-delegate", f"正在把编码任务交给外部 Agent（{Path(self.command[0]).name}）…")
            audit("delegate_start", {"permission": self.permission, "taskChars": len(text)})
            client = AcpClient(
                self.command,
                str(target),
                permission=self.permission,
                timeout=self.timeout,
                audit=audit,
            )
            try:
                result = client.run_prompt(text)
            except AcpError as exc:
                audit("delegate_failed", {
                    "code": exc.code,
                    "durationMs": int((time.monotonic() - started) * 1000),
                })
                _event("acp-delegate", f"外部 Agent 执行失败：{exc.code}")
                return ToolResponse(content=[TextBlock(
                    type="text", text=f"错误：外部编码 Agent 执行失败（{exc.code}）：{exc.message}"
                )])
            audit("delegate_done", {
                "stopReason": result["stopReason"],
                "durationMs": result["durationMs"],
                "fsWrites": result["fsWrites"],
                "outputChars": len(result["output"]),
            })
            trace = ""
            if result["toolTrace"]:
                trace = "\n外部 Agent 动作：\n" + "\n".join(f"- {t}" for t in result["toolTrace"])
            body = result["output"] or "（外部 Agent 没有返回正文）"
            if result["ok"]:
                return ToolResponse(content=[TextBlock(
                    type="text",
                    text=(
                        f"外部编码 Agent 执行完成（{result['durationMs'] / 1000:.1f}s，"
                        f"写文件 {result['fsWrites']} 次）。{trace}\n\n{body}"
                    ),
                )])
            return ToolResponse(content=[TextBlock(
                type="text",
                text=(
                    f"外部编码 Agent 结束但未正常完成（stopReason={result['stopReason']}，"
                    f"{result['durationMs'] / 1000:.1f}s）。{trace}\n\n{body}"
                ),
            )])

        toolkit.register_tool_function(delegate_coding_task)


if __name__ == "__main__":
    # 手动冒烟：python acp_client.py "<command>" "<cwd>" "<task>"
    if len(sys.argv) < 4:
        print("用法：python acp_client.py \"<command>\" \"<cwd>\" \"<task>\"", file=sys.stderr)
        raise SystemExit(2)
    runtime = AcpRuntime()
    runtime.runtime_present = True
    runtime.enabled = True
    runtime.command = _split_command(sys.argv[1])
    runtime.work_dir = sys.argv[2]
    runtime.audit_path = str(Path.home() / ".tongtianwork" / "acp" / "audit.jsonl")
    client = AcpClient(runtime.command, runtime.work_dir, permission="deny")
    try:
        print(json.dumps(client.run_prompt(sys.argv[3]), ensure_ascii=False, indent=2))
    except AcpError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        raise SystemExit(1)
