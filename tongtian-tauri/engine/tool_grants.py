"""执行面 P1：受控命令执行与联网获取（默认不注册）

Rust 随 `runtime.sandbox` 下发用户在工作区里的显式授权：

    sandbox = { ..., execEnabled, execAllowlist, networkEnabled, networkAllowlist }

本模块把这些授权变成两个有约束力的工具：

* `run_command` —— 只允许白名单里的裸命令名；argv 方式执行，**不经 shell**，
  工作目录固定为任务工作目录，超时与输出都有上限，子进程环境变量按白名单裁剪
  （避免把引擎进程里的 API Key 等秘密带进去）。
* `fetch_url` —— 只允许白名单里的主机；只接受 http/https，重定向逐跳复核，
  本机/内网/链路本地/云元数据地址一律拒绝（防 SSRF），响应体按字节与字符双重截断。

铁律：

1. 未授权就**不注册工具**，system prompt 也不提这两个工具；
2. 授权了但白名单为空时同样不注册，并把原因摊成可见说明，不假装可用；
3. 每次真实调用（成功、失败、超时、拒绝）都发 `tool_audit` 事件，
   由 Rust 归一动作码后写入审计库；引擎侧不直接写库。
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MAX_EXEC_TIMEOUT = 120
DEFAULT_EXEC_TIMEOUT = 30
MAX_EXEC_ARGS = 32
MAX_EXEC_OUTPUT_CHARS = 8000
MAX_TARGET_CHARS = 200

# 单次受控执行的系统资源上限：Windows 用 Job Object，Unix 用 rlimit（尽力而为）
EXEC_MEMORY_LIMIT_MB = 2048
EXEC_PROCESS_LIMIT = 64

FETCH_TIMEOUT = 20
MAX_FETCH_BYTES = 2 * 1024 * 1024
MAX_FETCH_CHARS = 20000
MAX_REDIRECTS = 3
USER_AGENT = "TongtianWork/1.0 (local agent; controlled fetch)"

COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
ALLOWED_SCHEMES = ("http", "https")
TEXT_CONTENT_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/x-ndjson",
    "application/yaml",
    "application/x-yaml",
    "application/javascript",
)

# 子进程只保留运行所需的环境变量：Windows 的系统变量 + 用户目录与 PATH。
# 刻意不保留任意 `*_KEY` / `*_TOKEN`，避免模型跑的命令读到引擎进程的秘密。
ENV_KEEP = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "OS", "LANG", "LC_ALL", "TZ",
})


def _brief(text: str, limit: int = 200) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[:limit] + "…"


def _str_list(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        s = str(item or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def _cap(text: str, limit: int = MAX_EXEC_OUTPUT_CHARS) -> str:
    body = text or ""
    if len(body) <= limit:
        return body
    return body[:limit] + f"\n…（已截断，原文共 {len(body)} 字符）"


def _audit_url(url: str) -> str:
    """审计只记 scheme://host/path：用户信息、查询串与片段可能带凭据，不入库"""
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
        if parts.scheme and parts.hostname:
            host = parts.hostname
            if parts.port:
                host += f":{parts.port}"
            return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))
    except Exception:  # noqa: BLE001  解析失败按原文截断，至少不中断工具
        pass
    return str(url or "").strip()


def _safe_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_KEEP}
    env["PYTHONIOENCODING"] = "utf-8"
    env["NO_COLOR"] = "1"
    return env


# ── 系统资源沙箱（受控执行的进程/内存边界） ──────────────────────
#
# Windows：Job Object 统一约束整棵进程树（内存、进程数、关闭即杀），
# 子进程先以 CREATE_SUSPENDED 创建、挂进 Job 后再恢复，避免「先跑起来再挂」的竞态。
# Unix：以 rlimit 尽力限制单进程地址空间，并用 setsid + killpg 终止整组；
# 进程数上限在 Unix 不做（RLIMIT_NPROC 按用户计数，误设会让正常命令直接失败），
# 这一点在系统提示与台账里如实说明，不假装等价。

_IS_WINDOWS = os.name == "nt"
_CREATE_SUSPENDED = 0x00000004
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200

_KERNEL32 = None
_NTDLL = None

if _IS_WINDOWS:
    try:
        import ctypes
        from ctypes import wintypes

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _NTDLL = ctypes.WinDLL("ntdll", use_last_error=True)
        _KERNEL32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        _KERNEL32.CreateJobObjectW.restype = wintypes.HANDLE
        _KERNEL32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
        ]
        _KERNEL32.SetInformationJobObject.restype = wintypes.BOOL
        _KERNEL32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        _KERNEL32.AssignProcessToJobObject.restype = wintypes.BOOL
        _KERNEL32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        _KERNEL32.TerminateJobObject.restype = wintypes.BOOL
        _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
        _KERNEL32.CloseHandle.restype = wintypes.BOOL
        _NTDLL.NtResumeProcess.argtypes = [wintypes.HANDLE]
        _NTDLL.NtResumeProcess.restype = ctypes.c_long
    except Exception:  # noqa: BLE001  ctypes 不可用时退化为仅超时约束
        _KERNEL32 = None
        _NTDLL = None


def _win_job() -> tuple[int | None, str]:
    """创建带内存/进程数/关闭即杀限制的 Job Object；失败时返回原因。"""
    if not _IS_WINDOWS or _KERNEL32 is None:
        return None, "当前平台不支持 Windows Job Object"
    try:
        job = _KERNEL32.CreateJobObjectW(None, None)
        if not job:
            return None, f"CreateJobObject 失败（错误码 {ctypes.get_last_error()}）"
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | _JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | _JOB_OBJECT_LIMIT_JOB_MEMORY
        )
        info.BasicLimitInformation.ActiveProcessLimit = EXEC_PROCESS_LIMIT
        mem = EXEC_MEMORY_LIMIT_MB * 1024 * 1024
        info.ProcessMemoryLimit = mem
        info.JobMemoryLimit = mem
        if not _KERNEL32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            err = ctypes.get_last_error()
            _KERNEL32.CloseHandle(job)
            return None, f"SetInformationJobObject 失败（错误码 {err}）"
        return job, ""
    except Exception as exc:  # noqa: BLE001
        return None, _brief(exc)


def _close_job(job) -> None:
    if not job:
        return
    try:
        _KERNEL32.CloseHandle(job)
    except Exception:  # noqa: BLE001
        pass


def _win_assign_resume(job, proc) -> tuple[bool, str]:
    """把已挂起的子进程挂进 Job 再恢复；返回 (是否挂上, 失败原因)。"""
    try:
        handle = int(proc._handle)
        if not _KERNEL32.AssignProcessToJobObject(job, handle):
            err = ctypes.get_last_error()
            # 挂不上也必须恢复，否则命令会永远停在挂起状态
            _NTDLL.NtResumeProcess(handle)
            return False, f"AssignProcessToJobObject 失败（错误码 {err}）"
        status = _NTDLL.NtResumeProcess(handle)
        if status != 0:
            return True, f"NtResumeProcess 失败（0x{status & 0xFFFFFFFF:08X}）"
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, _brief(exc)


def _kill_tree(proc, job) -> None:
    if _IS_WINDOWS:
        if job:
            try:
                _KERNEL32.TerminateJobObject(job, 1)
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:  # noqa: BLE001
                pass
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:
            pass


def _posix_limit_setup(read_fd: int, write_fd: int):
    """子进程 exec 前设置 rlimit；失败原因经管道回传给父进程。"""

    def setup() -> None:  # pragma: no cover - 在 fork 出的子进程内执行
        try:
            os.setsid()
        except Exception:  # noqa: BLE001
            pass
        problems: list[str] = []
        try:
            import resource
        except Exception:  # noqa: BLE001
            resource = None
        if resource is None:
            problems.append("平台没有 resource 模块")
        else:
            mem = EXEC_MEMORY_LIMIT_MB * 1024 * 1024
            try:
                resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            except Exception as exc:  # noqa: BLE001
                problems.append(f"内存上限未生效（{type(exc).__name__}）")
        try:
            os.close(read_fd)
            if problems:
                os.write(write_fd, "；".join(problems).encode("utf-8", "replace")[:300])
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                os.close(write_fd)
            except Exception:  # noqa: BLE001
                pass

    return setup


def _execute_limited(
    argv: list[str], cwd: str, timeout: int
) -> tuple[subprocess.CompletedProcess, str]:
    """执行命令并施加系统资源限制；返回 (结果, 降级说明)。

    降级说明为空表示限制已生效；非空表示本次只受超时约束，调用方必须如实展示。
    超时由调用方捕获 `subprocess.TimeoutExpired`，此处已终止整棵进程树并回收。
    """
    env = _safe_env()
    job = None
    limits_note = ""
    if _IS_WINDOWS:
        job, job_err = _win_job()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if job:
            flags |= _CREATE_SUSPENDED
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                shell=False,
                creationflags=flags,
            )
        except Exception:
            _close_job(job)
            raise
        if job:
            assigned, limit_err = _win_assign_resume(job, proc)
            if not assigned:
                _close_job(job)
                job = None
                limits_note = (
                    f"系统资源限制未生效：{limit_err}；本次执行仅受超时约束"
                )
            elif limit_err:
                # 进程仍处于挂起态，关闭 Job 会直接杀掉它，不能继续执行
                _close_job(job)
                try:
                    proc.communicate(timeout=2)
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError(f"恢复子进程失败：{limit_err}")
        elif job_err:
            limits_note = f"系统资源限制未生效：{job_err}；本次执行仅受超时约束"
    else:
        read_fd, write_fd = os.pipe()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                shell=False,
                preexec_fn=_posix_limit_setup(read_fd, write_fd),
                pass_fds=(write_fd,),
            )
        except Exception:
            os.close(read_fd)
            os.close(write_fd)
            raise
        os.close(write_fd)
        try:
            limits_note = os.read(read_fd, 512).decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            limits_note = ""
        finally:
            os.close(read_fd)
        if limits_note:
            limits_note = f"系统资源限制未生效：{limits_note}；本次执行仅受超时约束"

    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc, job)
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        _close_job(job)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err), limits_note


def _is_public_ip(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_public_addresses(host: str) -> tuple[list[str], str]:
    """解析主机并确认所有解析结果都是公网地址；返回 (地址列表, 错误)"""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as exc:  # noqa: BLE001  解析失败按拒绝处理，错误原文进说明
        return [], f"无法解析主机 {host}：{_brief(exc, 120)}"
    addrs: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in addrs:
            addrs.append(addr)
    if not addrs:
        return [], f"主机 {host} 没有可用的解析结果"
    # 任一解析结果落在内网/本机就整体拒绝：避免 DNS 轮询把请求带进内网
    blocked = [a for a in addrs if not _is_public_ip(a)]
    if blocked:
        return [], (
            f"主机 {host} 解析到非公网地址（{blocked[0]}），"
            "本机/内网/链路本地/元数据地址一律拒绝"
        )
    return addrs, ""


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    """重定向逐跳复核：每一跳都必须仍在白名单内且是公网地址"""

    max_redirections = MAX_REDIRECTS

    def __init__(self, validate: Callable[[str], str]):
        super().__init__()
        self._validate = validate

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._validate(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class ToolGrantPolicy:
    workspace_id: str = ""
    error: str = ""
    exec_enabled: bool = False
    exec_allowlist: list[str] = field(default_factory=list)
    network_enabled: bool = False
    network_allowlist: list[str] = field(default_factory=list)
    _seq: int = 0

    @classmethod
    def from_runtime(cls, runtime: dict | None) -> "ToolGrantPolicy":
        block = (runtime or {}).get("sandbox") if isinstance(runtime, dict) else None
        if isinstance(block, str):
            try:
                block = json.loads(block)
            except Exception:  # noqa: BLE001
                return cls(error=f"沙箱配置不是合法 JSON：{_brief(block)}")
        if not isinstance(block, dict):
            return cls()
        return cls(
            workspace_id=str(block.get("workspaceId") or "").strip(),
            error=str(block.get("error") or "").strip(),
            exec_enabled=block.get("execEnabled") is True,
            exec_allowlist=[c.lower() for c in _str_list(block.get("execAllowlist"))],
            network_enabled=block.get("networkEnabled") is True,
            network_allowlist=[h.lower() for h in _str_list(block.get("networkAllowlist"))],
        )

    # ── 判定 ────────────────────────────────────────────────────

    def exec_denial(self, command: str) -> str | None:
        if self.error:
            return f"沙箱策略读取失败，执行工具不可用：{_brief(self.error)}"
        if not self.exec_enabled:
            return "用户未授权命令执行（受控执行默认关闭）"
        name = str(command or "").strip()
        if not COMMAND_NAME_RE.fullmatch(name):
            return (
                "命令名必须是裸命令名（字母/数字/下划线/连字符/点/加号），"
                "不能带路径、空格或 shell 元字符"
            )
        if name.lower() not in self.exec_allowlist:
            allowed = "、".join(self.exec_allowlist[:8]) or "（空）"
            return f"命令「{name}」不在授权白名单内（允许：{allowed}）"
        if shutil.which(name) is None:
            return f"本机找不到命令「{name}」（PATH 中没有可执行文件）"
        return None

    def network_denial(self, url: str) -> str | None:
        if self.error:
            return f"沙箱策略读取失败，联网工具不可用：{_brief(self.error)}"
        if not self.network_enabled:
            return "用户未授权联网获取（联网工具默认关闭）"
        try:
            parts = urllib.parse.urlsplit(str(url or "").strip())
        except Exception as exc:  # noqa: BLE001
            return f"URL 无法解析：{_brief(exc, 120)}"
        if parts.scheme.lower() not in ALLOWED_SCHEMES:
            return "只允许 http/https 地址"
        if parts.username or parts.password:
            return "URL 不允许携带用户名或密码"
        host = (parts.hostname or "").strip().lower()
        if not host:
            return "URL 缺少主机名"
        if host not in self.network_allowlist:
            allowed = "、".join(self.network_allowlist[:8]) or "（空）"
            return f"主机「{host}」不在联网白名单内（允许：{allowed}）"
        _, err = _resolve_public_addresses(host)
        return err or None

    def notes(self) -> list[str]:
        if self.error:
            return [f"受控命令执行/联网工具未注册：沙箱策略读取失败（{_brief(self.error)}）"]
        out: list[str] = []
        if self.exec_enabled:
            if self.exec_allowlist:
                out.append(
                    "受控命令执行已启用：仅 "
                    + "、".join(self.exec_allowlist[:8])
                    + " 可运行（不经 shell，工作目录固定，输出/超时/系统资源有上限）"
                )
            else:
                out.append("受控命令执行已开启但命令白名单为空，run_command 未注册")
        if self.network_enabled:
            if self.network_allowlist:
                out.append(
                    "联网获取已启用：仅公网主机 "
                    + "、".join(self.network_allowlist[:8])
                    + " 可访问；本机/内网/元数据地址一律拒绝"
                )
            else:
                out.append("联网获取已开启但主机白名单为空，fetch_url 未注册")
        return out

    def system_hint(self) -> str:
        if self.error:
            return ""
        lines: list[str] = []
        if self.exec_enabled and self.exec_allowlist:
            if _IS_WINDOWS and _KERNEL32 is not None:
                limits = (
                    f"单次执行受系统 Job Object 约束：内存 {EXEC_MEMORY_LIMIT_MB}MB、"
                    f"进程 {EXEC_PROCESS_LIMIT} 个，超时会终止整棵进程树；"
                )
            else:
                limits = (
                    f"单次执行有内存上限 {EXEC_MEMORY_LIMIT_MB}MB，"
                    "超时会终止整个进程组；"
                )
            lines.append(
                "- run_command：仅允许运行 "
                + "、".join(self.exec_allowlist)
                + "。工作目录固定为任务目录，不经 shell，参数按数组传递；"
                + limits
                + "每次调用都会写入审计日志。"
            )
        if self.network_enabled and self.network_allowlist:
            lines.append(
                "- fetch_url：仅允许访问 "
                + "、".join(self.network_allowlist)
                + " 的公网地址。本机/内网/云元数据地址会被拒绝；"
                "响应过长会截断；每次调用都会写入审计日志。"
            )
        if not lines:
            return ""
        return "\n\n## 受控执行与联网（用户显式授权）\n" + "\n".join(lines)

    # ── 审计 ────────────────────────────────────────────────────

    def _audit(
        self,
        emit: Callable[[str, dict], None] | None,
        task_id: str,
        tool: str,
        target: str,
        outcome: str,
        detail: str = "",
    ) -> None:
        if emit is None:
            return
        self._seq += 1
        try:
            emit("tool_audit", {
                "workspaceId": self.workspace_id,
                "taskId": task_id,
                "tool": tool,
                "target": _brief(target, MAX_TARGET_CHARS),
                "outcome": outcome,
                "detail": _brief(detail, 300),
                "seq": self._seq,
            })
        except Exception:  # noqa: BLE001  审计事件发不出去不应中断工具本身
            pass

    # ── 工具注册 ────────────────────────────────────────────────

    def install(
        self,
        toolkit,
        work_dir: Path,
        emit: Callable[[str, dict], None] | None = None,
        task_id: str = "",
    ) -> None:
        """按授权注册工具；未授权/白名单为空一律不注册（不写进工具面）"""
        if self.error:
            return
        if self.exec_enabled and self.exec_allowlist:
            toolkit.register_tool_function(self._exec_tool(Path(work_dir), emit, task_id))
        if self.network_enabled and self.network_allowlist:
            toolkit.register_tool_function(self._fetch_tool(emit, task_id))

    def _exec_tool(
        self,
        work_dir: Path,
        emit: Callable[[str, dict], None] | None,
        task_id: str,
    ):
        base = Path(work_dir).resolve()

        def run_command(command: str, args_json: str = "[]", timeout_seconds: int = 30):
            """在任务工作目录内运行一个已授权的命令（argv 方式，不经 shell）。

            Args:
                command (`str`):
                    裸命令名（如 python、git、node），必须在用户的命令白名单内。
                args_json (`str`, optional):
                    参数数组的 JSON 字符串，例如 ["-c", "print('hi')"]。
                    参数原样传给进程，不经过 shell 解析。
                timeout_seconds (`int`, optional):
                    超时秒数，默认 30，最大 120。
            """
            from agentscope.message import TextBlock
            from agentscope.tool import ToolResponse

            # 审计只记命令名与参数个数：参数里可能被模型塞入凭据，不原样入库
            target = _brief(command)
            denial = self.exec_denial(command)
            if denial is not None:
                self._audit(emit, task_id, "run_command", target, "denied", denial)
                return ToolResponse(content=[TextBlock(
                    type="text", text=f"错误：命令未执行。{denial}"
                )])

            try:
                parsed = json.loads(args_json or "[]")
            except Exception as exc:  # noqa: BLE001
                denial = f"args_json 不是合法 JSON：{_brief(exc, 120)}"
                self._audit(emit, task_id, "run_command", target, "denied", denial)
                return ToolResponse(content=[TextBlock(type="text", text=f"错误：{denial}")])
            if not isinstance(parsed, list) or len(parsed) > MAX_EXEC_ARGS:
                denial = f"args_json 必须是字符串数组，且最多 {MAX_EXEC_ARGS} 项"
                self._audit(emit, task_id, "run_command", target, "denied", denial)
                return ToolResponse(content=[TextBlock(type="text", text=f"错误：{denial}")])
            args = [str(a) for a in parsed]
            target = _brief(f"{command}（{len(args)} 个参数）")
            if any(len(a) > 4096 for a in args):
                denial = "单个参数超过 4096 字符"
                self._audit(emit, task_id, "run_command", target, "denied", denial)
                return ToolResponse(content=[TextBlock(type="text", text=f"错误：{denial}")])

            try:
                timeout = int(timeout_seconds)
            except Exception:  # noqa: BLE001
                timeout = DEFAULT_EXEC_TIMEOUT
            timeout = min(max(timeout, 1), MAX_EXEC_TIMEOUT)
            executable = shutil.which(command)
            if executable is None:  # exec_denial 已查过，防 PATH 在这之间变化
                denial = f"本机找不到命令「{command}」"
                self._audit(emit, task_id, "run_command", target, "failed", denial)
                return ToolResponse(content=[TextBlock(type="text", text=f"错误：{denial}")])

            try:
                proc, limits_note = _execute_limited([executable, *args], str(base), timeout)
            except subprocess.TimeoutExpired:
                detail = f"超过 {timeout} 秒被终止"
                self._audit(emit, task_id, "run_command", target, "timeout", detail)
                return ToolResponse(content=[TextBlock(
                    type="text", text=f"错误：命令执行超时（{detail}），整棵进程树已终止"
                )])
            except Exception as exc:  # noqa: BLE001
                detail = _brief(exc, 200)
                self._audit(emit, task_id, "run_command", target, "failed", detail)
                return ToolResponse(content=[TextBlock(
                    type="text", text=f"错误：命令启动失败：{detail}"
                )])

            self._audit(
                emit, task_id, "run_command", target,
                "ok" if proc.returncode == 0 else "failed",
                f"exit={proc.returncode} timeout={timeout}s "
                f"limits={'degraded' if limits_note else 'on'}",
            )
            parts = [f"命令：{command}", f"退出码：{proc.returncode}"]
            if limits_note:
                parts.append("资源限制：" + limits_note)
            parts.append("stdout:\n" + (_cap(proc.stdout) or "（空）"))
            parts.append("stderr:\n" + (_cap(proc.stderr) or "（空）"))
            return ToolResponse(content=[TextBlock(type="text", text="\n".join(parts))])

        return run_command

    def _fetch_tool(
        self,
        emit: Callable[[str, dict], None] | None,
        task_id: str,
    ):
        def fetch_url(url: str):
            """读取一个已授权公网地址的文本内容（GET；不跟随白名单外的重定向）。

            Args:
                url (`str`):
                    http/https 地址，主机必须在用户的联网白名单内。
            """
            from agentscope.message import TextBlock
            from agentscope.tool import ToolResponse

            target = _brief(_audit_url(url), MAX_TARGET_CHARS)
            denial = self.network_denial(url)
            if denial is not None:
                self._audit(emit, task_id, "fetch_url", target, "denied", denial)
                return ToolResponse(content=[TextBlock(
                    type="text", text=f"错误：请求未发出。{denial}"
                )])

            def validate(next_url: str) -> str:
                err = self.network_denial(next_url)
                if err:
                    raise ValueError(err)
                return next_url

            opener = urllib.request.build_opener(_CheckedRedirect(validate))
            req = urllib.request.Request(
                str(url).strip(),
                headers={"User-Agent": USER_AGENT, "Accept": "text/*, application/json;q=0.9, */*;q=0.1"},
                method="GET",
            )
            try:
                with opener.open(req, timeout=FETCH_TIMEOUT) as resp:
                    status = getattr(resp, "status", 200)
                    content_type = resp.headers.get("Content-Type", "") or ""
                    final_url = resp.geturl()
                    # 重定向最终地址再复核一次（handler 已逐跳校验，这里兜底）
                    validate(final_url)
                    mime = content_type.split(";", 1)[0].strip().lower()
                    if mime and not mime.startswith(TEXT_CONTENT_PREFIXES):
                        detail = f"Content-Type={mime} 不是文本"
                        self._audit(emit, task_id, "fetch_url", target, "failed", detail)
                        return ToolResponse(content=[TextBlock(
                            type="text",
                            text=f"错误：{detail}，为避免把二进制内容塞进上下文，已停止读取",
                        )])
                    length = resp.headers.get("Content-Length")
                    if length and length.isdigit() and int(length) > MAX_FETCH_BYTES:
                        detail = f"Content-Length={length} 超过 {MAX_FETCH_BYTES} 字节"
                        self._audit(emit, task_id, "fetch_url", target, "failed", detail)
                        return ToolResponse(content=[TextBlock(type="text", text=f"错误：{detail}")])
                    raw = resp.read(MAX_FETCH_BYTES + 1)
            except urllib.error.HTTPError as exc:
                detail = f"HTTP {exc.code}"
                self._audit(emit, task_id, "fetch_url", target, "failed", detail)
                return ToolResponse(content=[TextBlock(
                    type="text", text=f"错误：请求返回 {detail}（{_brief(exc.reason, 120)}）"
                )])
            except ValueError as exc:
                detail = _brief(exc, 200)
                self._audit(emit, task_id, "fetch_url", target, "denied", detail)
                return ToolResponse(content=[TextBlock(type="text", text=f"错误：请求被拒绝：{detail}")])
            except Exception as exc:  # noqa: BLE001
                detail = _brief(exc, 200)
                self._audit(emit, task_id, "fetch_url", target, "failed", detail)
                return ToolResponse(content=[TextBlock(
                    type="text", text=f"错误：联网获取失败：{detail}"
                )])

            truncated = len(raw) > MAX_FETCH_BYTES
            if truncated:
                raw = raw[:MAX_FETCH_BYTES]
            charset = "utf-8"
            for piece in content_type.split(";")[1:]:
                key, _, value = piece.strip().partition("=")
                if key.lower() == "charset" and value:
                    charset = value.strip().strip('"') or "utf-8"
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                charset = "utf-8"
                text = raw.decode("utf-8", errors="replace")
            text = _cap(text, MAX_FETCH_CHARS)
            self._audit(
                emit, task_id, "fetch_url", target, "ok",
                f"HTTP {status} bytes={len(raw)} truncated={truncated}",
            )
            header = [
                f"URL：{final_url}",
                f"状态：{status}",
                f"类型：{content_type or '（未声明）'}",
            ]
            if truncated:
                header.append(f"说明：响应超过 {MAX_FETCH_BYTES} 字节，已截断")
            return ToolResponse(content=[TextBlock(
                type="text", text="\n".join(header) + "\n\n" + (text or "（空响应）")
            )])

        return fetch_url
