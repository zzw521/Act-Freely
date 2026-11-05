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

import hashlib
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

import ticket_worker_adapter as twa  # 只借它的**睡等判定表**（D-48.4c）：什么算 sleep 只有一份答案，理由文案两边各写
import sandbox_launcher as sl  # 系统隔离套壳（D-08）：受控执行与终端/MCP/ACP/Hooks 用同一只容器

MAX_EXEC_TIMEOUT = 120
DEFAULT_EXEC_TIMEOUT = 30
MAX_EXEC_ARGS = 32
MAX_EXEC_OUTPUT_CHARS = 8000
MAX_TARGET_CHARS = 200
# 通用 shell 的整条命令上限：再长就不是「一次操作」，而是把脚本塞进参数
MAX_SHELL_COMMAND_CHARS = 8000

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


def _shell_target(command: str) -> str:
    """审计目标：通用 shell 的命令正文可能带凭据，只记长度与摘要，不原样入库"""
    raw = str(command or "")
    digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:12]
    return f"shell（{len(raw)} 字符，sha256:{digest}）"


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
    argv: list[str] | str, cwd: str, timeout: int
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


# ── 系统隔离容器（D-08）────────────────────────────────────────
#
# 终端 / MCP stdio / ACP / Hooks 四条通道早就走 `sandbox_launcher.wrap` 进 AppContainer，
# 但**受控执行**（`run_command` / `run_shell`）没有——而技能正文、套件指令、扩展带来的可执行件
# 真正驱动的就是这一条：模型按扩展的指示执行一条命令时，拿到的是宿主权限，出网开关也管不到它。
# D-08 的口径是把这条通道并进同一只容器，并且**未授权 / 容器不可用时一律不注册**，
# 而不是注册完再在执行时拒绝（后者会让模型看见一个点不通的按钮，提示词还在教它用）。


def container_state() -> dict:
    """当前受控执行会落到哪只容器里（只读判定，不产生任何副作用）。

    形状：`{"enabled", "ready", "launcher", "network", "reason"}`。
    `enabled` 只说明桌面端注入了 `TT_SANDBOX_BIN`；`ready` 还要求那个文件真的在盘上。
    """
    launcher = sl.launcher()
    if launcher is None:
        return {"enabled": False, "ready": False, "launcher": "", "network": "",
                "reason": "系统隔离未启用：受控执行只受资源限制、超时与工作目录约束"}
    if not Path(launcher).is_file():
        return {"enabled": True, "ready": False, "launcher": launcher, "network": "",
                "reason": f"系统隔离已启用但启动器不在盘上（{launcher}）：受控执行不注册"}
    return {"enabled": True, "ready": True, "launcher": launcher,
            "network": sl_network(), "reason": ""}


def sl_network() -> str:
    """出网档位（`on` / `off`）：缺省按关闭，与启动器的读法一致。"""
    return "on" if (os.environ.get("TT_SANDBOX_NETWORK") or "").strip().lower() == "on" else "off"


def container_wrap(
    program: str, args: list[str], workspace: str | None = None
) -> tuple[list[str], str]:
    """把一次执行套进系统隔离容器；返回 (交给 Popen 的 argv, 容器说明)。

    - 未启用系统隔离 ⇒ 原样返回 `([program, *args], "")`，与改造前逐字一致；
    - 启用 ⇒ 返回 `[启动器, --profile, …, --network, on|off, …, --, program, *args]`，
      说明里点名「在容器内」与出网档位，让审计与界面都看得到这一轮到底进没进容器；
    - 启动器不可用 ⇒ 抛 `sl.SandboxUnavailable`，**调用方必须拒绝执行**，不许静默降级成
      不受隔离的进程（这条是 `sandbox_launcher` 的铁律，不在这里打折）。
    """
    argv = [program, *[str(a) for a in args]]
    if not sl.enabled():
        return argv, ""
    wrapped = sl.wrap(program, [str(a) for a in args], workspace=workspace)
    if wrapped is None:  # enabled() 与 wrap() 之间环境变量被抽掉：按不可用处理，不放行
        raise sl.SandboxUnavailable(
            "SANDBOX_LAUNCHER_MISSING: 系统隔离已启用但拿不到启动器，命令未执行"
        )
    exe, launcher_argv = wrapped
    network = "开启" if sl_network() == "on" else "关闭"
    note = f"本次在系统隔离容器（AppContainer）内执行，出网{network}"
    return [exe, *launcher_argv], note


def container_wrap_shell(
    plain: object,
    program: str,
    args: list[str],
    workspace: str | None = None,
    raw_command_line: bool = False,
) -> tuple[object, str]:
    """shell 通道的套壳（D-08），与 `container_wrap` 同一条判定，但**保住 D-48.4c 的形状**。

    未启用系统隔离时原样返回 `plain`：Windows 上那是整条命令行**字符串**——`cmd.exe` 不吃
    Python 列表形式拼出的 `\"`，交列表会让带引号的命令 rc=0 静默空跑（D-48.4c 实测并钉住的坑），
    接线时把它改回列表就是把这个坑重新挖开。
    启用系统隔离时交出去的是启动器的真 argv 列表，其中 `raw_command_line=True` 让启动器把
    参数**按原文**拼成子进程命令行（`--raw-command-line`，见 `sandbox_launcher.wrap`）：
    容器里跑的那条命令行与不进容器时逐字一致，隔离只加边界、不改命令的含义。
    """
    if not sl.enabled():
        return plain, ""
    wrapped = sl.wrap(
        program,
        [str(a) for a in args],
        workspace=workspace,
        raw_command_line=raw_command_line,
    )
    if wrapped is None:
        raise sl.SandboxUnavailable(
            "SANDBOX_LAUNCHER_MISSING: 系统隔离已启用但拿不到启动器，命令未执行"
        )
    exe, launcher_argv = wrapped
    network = "开启" if sl_network() == "on" else "关闭"
    return [exe, *launcher_argv], f"本次在系统隔离容器（AppContainer）内执行，出网{network}"


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
    shell_enabled: bool = False
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
            # 通用 shell 依赖命令执行授权：壳侧漏传/旧版本没有该键时按关闭处理
            shell_enabled=block.get("shellEnabled") is True,
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

    def shell_denial(self) -> str | None:
        """通用 shell 是否可用：必须在受控命令执行之上再显式开启"""
        if self.error:
            return f"沙箱策略读取失败，通用 shell 不可用：{_brief(self.error)}"
        if not self.exec_enabled:
            return "用户未授权命令执行（受控执行默认关闭），通用 shell 也不可用"
        if not self.shell_enabled:
            return "用户未授权通用 shell（默认关闭）"
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
        if self.shell_enabled:
            if self.exec_enabled:
                out.append(
                    "通用 shell 已启用（高风险）：可执行任意命令（含管道、重定向与链式命令），"
                    "工作目录固定为任务目录，超时/输出/系统资源有上限，每次调用写审计"
                )
            else:
                out.append("通用 shell 已开启，但受控命令执行未开启，run_shell 未注册")
        if self.network_enabled:
            if self.network_allowlist:
                out.append(
                    "联网获取已启用：仅公网主机 "
                    + "、".join(self.network_allowlist[:8])
                    + " 可访问；本机/内网/元数据地址一律拒绝"
                )
            else:
                out.append("联网获取已开启但主机白名单为空，fetch_url 未注册")
        # D-08：容器这一层的读数单独摊出来。未启用系统隔离时一句都不加（与改造前逐字一致），
        # 启用了就必须说清「这一轮到底进没进容器」——进不去就是**不注册**，不许装作能跑。
        container = container_state()
        if container["enabled"] and not container["ready"]:
            out.append(
                f"受控命令执行与通用 shell 未注册：{container['reason']}"
                "（不把没有容器可用的执行面交给模型）"
            )
        elif container["ready"]:
            out.append(
                "系统隔离已启用：受控执行与终端/MCP/ACP/Hooks 同进 AppContainer，"
                f"出网{'允许' if container['network'] == 'on' else '关闭'}"
            )
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
        if self.shell_enabled and self.exec_enabled:
            lines.append(
                "- run_shell：运行一条通用 shell 命令（Windows 为 cmd，其他平台为 /bin/sh），"
                "支持管道、重定向与 &&；工作目录固定为任务目录，超时与输出有上限，"
                "命令正文只以哈希形式写入审计。只有管道/链式步骤确实无法用 run_command 表达时才使用。"
                "shell 里禁用睡等（`sleep`/`Start-Sleep`/`timeout /t`/`ping -t`，"
                "换成 `python -c \"import time;time.sleep(20)\"` 这类内联写法一样拒）——"
                "等待靠排队不靠轮询；拦的是睡等写法，不是「跑得久」。"
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

    def registration_plan(self) -> dict:
        """这一轮执行面有哪几个工具会进 toolkit，没进去的原因各是什么（D-08）。

        `install()` 与「这个扩展声明了什么能力」的界面清单读的是**同一个判定**：
        界面显示可用而模型点不通（或反过来）只可能来自两处各写一份规则，所以只留一处。
        形状：`{"run_command": {"registered", "reason"}, "run_shell": {…}, "fetch_url": {…},
        "container": container_state()}`。
        """
        container = container_state()
        exec_blocked = container["enabled"] and not container["ready"]
        plan: dict = {"container": container}

        def item(registered: bool, reason: str) -> dict:
            return {"registered": registered, "reason": "" if registered else reason}

        if self.error:
            why = f"沙箱策略读取失败：{self.error}"
            return {
                "run_command": item(False, why),
                "run_shell": item(False, why),
                "fetch_url": item(False, why),
                "container": container,
            }
        if not self.exec_enabled:
            command = item(False, "用户未授权受控命令执行")
        elif not self.exec_allowlist:
            command = item(False, "命令白名单为空")
        elif exec_blocked:
            command = item(False, container["reason"])
        else:
            command = item(True, "")
        if not self.exec_enabled:
            shell = item(False, "通用 shell 依赖命令执行授权，命令执行未开启")
        elif not self.shell_enabled:
            shell = item(False, "用户未授权通用 shell（高风险）")
        elif exec_blocked:
            shell = item(False, container["reason"])
        else:
            shell = item(True, "")
        if not self.network_enabled:
            net = item(False, "用户未授权联网获取")
        elif not self.network_allowlist:
            net = item(False, "主机白名单为空")
        else:
            net = item(True, "")
        plan["run_command"] = command
        plan["run_shell"] = shell
        plan["fetch_url"] = net
        return plan

    def install(
        self,
        toolkit,
        work_dir: Path,
        emit: Callable[[str, dict], None] | None = None,
        task_id: str = "",
    ) -> None:
        """按授权注册工具；未授权/白名单为空一律不注册（不写进工具面）

        D-08 再加一条：**系统隔离已启用但容器起不来时，执行面同样不注册**。
        这条不是保守——技能正文、套件指令与扩展声明的可执行件都靠 `run_command` / `run_shell`
        落地，容器不可用时把它们注册上去，等于让扩展内容以宿主权限跑；
        联网获取走的是进程内 `urllib`（不产生子进程），不受这一条影响。
        「不注册」的判定与界面清单共用 `registration_plan()`，两处不会各说一套。
        """
        plan = self.registration_plan()
        if plan["run_command"]["registered"]:
            toolkit.register_tool_function(self._exec_tool(Path(work_dir), emit, task_id))
        if plan["run_shell"]["registered"]:
            toolkit.register_tool_function(self._shell_tool(Path(work_dir), emit, task_id))
        if plan["fetch_url"]["registered"]:
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
                argv, container_note = container_wrap(executable, args, str(base))
            except sl.SandboxUnavailable as exc:
                denial = _brief(exc, 200)
                self._audit(emit, task_id, "run_command", target, "denied", f"容器不可用：{denial}")
                return ToolResponse(content=[TextBlock(
                    type="text",
                    text=f"错误：系统隔离已启用但命令无法进入容器，为避免以宿主权限执行已拒绝运行：{denial}",
                )])

            try:
                proc, limits_note = _execute_limited(argv, str(base), timeout)
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
                f"limits={'degraded' if limits_note else 'on'} "
                f"container={'on' if container_note else 'off'}",
            )
            parts = [f"命令：{command}", f"退出码：{proc.returncode}"]
            if container_note:
                parts.append(container_note)
            if limits_note:
                parts.append("资源限制：" + limits_note)
            parts.append("stdout:\n" + (_cap(proc.stdout) or "（空）"))
            parts.append("stderr:\n" + (_cap(proc.stderr) or "（空）"))
            return ToolResponse(content=[TextBlock(type="text", text="\n".join(parts))])

        return run_command

    def _shell_tool(
        self,
        work_dir: Path,
        emit: Callable[[str, dict], None] | None,
        task_id: str,
    ):
        base = Path(work_dir).resolve()

        def run_shell(command: str, timeout_seconds: int = 60):
            """在任务工作目录内运行一条通用 shell 命令（用户显式授权，高风险）。

            禁止睡等：命令里出现 `sleep N` / `Start-Sleep` / `timeout /t N` / `ping -t`，
            或换成 `python -c "import time;time.sleep(20)"`、`setTimeout(...)` 这类内联写法
            一样会被拒绝执行——等待靠排队不靠轮询，要等外部事件就让本轮结束、下一轮再接着看。
            拦的是睡等**写法**不是「跑得久」：`npm run build`、`pytest -q` 再慢也放行。

            Args:
                command (`str`):
                    完整命令行，例如 `npm install && npm run build`。
                    Windows 交给 cmd，其他平台交给 /bin/sh；支持管道、重定向与链式命令。
                timeout_seconds (`int`, optional):
                    超时秒数，默认 60，最大 120。
            """
            from agentscope.message import TextBlock
            from agentscope.tool import ToolResponse

            denial = self.shell_denial()
            if denial is not None:
                self._audit(emit, task_id, "run_shell", "shell（未执行）", "denied", denial)
                return ToolResponse(content=[TextBlock(
                    type="text", text=f"错误：命令未执行。{denial}"
                )])

            raw = str(command or "").strip()
            if not raw:
                denial = "shell 命令不能为空"
                self._audit(emit, task_id, "run_shell", "shell（空命令）", "denied", denial)
                return ToolResponse(content=[TextBlock(type="text", text=f"错误：{denial}")])
            if len(raw) > MAX_SHELL_COMMAND_CHARS:
                denial = f"shell 命令超过 {MAX_SHELL_COMMAND_CHARS} 字符"
                self._audit(emit, task_id, "run_shell", _shell_target(raw), "denied", denial)
                return ToolResponse(content=[TextBlock(type="text", text=f"错误：{denial}")])

            # 等待靠排队，不靠轮询（DSH 对照里「主会话的 shell 禁止 sleep」那一条）。
            # 判定表与工单执行体**共用一份**（`twa.find_sleep_pattern`），理由各写各的：
            # 执行体占的是 worker 槽位、等的是唤醒，主会话占的是**这一轮**与任务额度。
            sleep_label = twa.find_sleep_pattern(raw)
            if sleep_label:
                denial = (
                    f"命令里有 {sleep_label}——主会话的 shell 不许用睡等占着这一轮，"
                    f"等待靠排队不靠轮询。要等外部事件就把这一轮说完、下一轮再接着看；"
                    f"真需要长时间运行的活请直接跑（timeout_seconds 上限 {MAX_EXEC_TIMEOUT} 秒管的是**干活**，不是干等）。"
                )
                self._audit(emit, task_id, "run_shell", _shell_target(raw), "denied", denial)
                return ToolResponse(content=[TextBlock(type="text", text=f"错误：命令未执行。{denial}")])

            try:
                timeout = int(timeout_seconds)
            except Exception:  # noqa: BLE001
                timeout = DEFAULT_EXEC_TIMEOUT
            timeout = min(max(timeout, 1), MAX_EXEC_TIMEOUT)

            if os.name == "nt":
                shell = os.environ.get("COMSPEC") or "cmd.exe"
                # cmd.exe 不是 MSVCRT 的 argv 消费者：把命令当**列表**交给 Popen 时，
                # Python 会把 `"` 转义成 `\"`，cmd 原样收到反斜杠——实测
                # `python -c "print(42)"` 退出码 0、stdout 全空（静默没跑），
                # `echo "hello world"` 打出 `\"hello world\"`。声明里那句「支持管道/引号」
                # 就是假的。正确形状是把整条命令行**拼成字符串**交给 CreateProcess，
                # 让 `/s` 剥掉最外面这一对引号，里面的引号按 cmd 自己的规则保留。
                tail = f'"{raw}"'
                argv: object = f"{subprocess.list2cmdline([shell, '/d', '/s', '/c'])} {tail}"
                shell_args = ["/d", "/s", "/c", tail]
                raw_line = True
            else:
                shell = "/bin/sh"
                argv = [shell, "-c", raw]
                shell_args = ["-c", raw]
                raw_line = False
            target = _shell_target(raw)

            # D-08：启用系统隔离时这条通道也进同一只 AppContainer（终端 / MCP / ACP / Hooks
            # 早就进了）。容器不可用就当次拒绝，不放行成宿主权限的裸跑。
            try:
                argv, container_note = container_wrap_shell(
                    argv, shell, shell_args, str(base), raw_command_line=raw_line
                )
            except sl.SandboxUnavailable as exc:
                denial = _brief(exc, 200)
                self._audit(emit, task_id, "run_shell", target, "denied", f"容器不可用：{denial}")
                return ToolResponse(content=[TextBlock(
                    type="text",
                    text=f"错误：系统隔离已启用但命令无法进入容器，为避免以宿主权限执行已拒绝运行：{denial}")])

            try:
                proc, limits_note = _execute_limited(argv, str(base), timeout)
            except subprocess.TimeoutExpired:
                detail = f"超过 {timeout} 秒被终止"
                self._audit(emit, task_id, "run_shell", target, "timeout", detail)
                return ToolResponse(content=[TextBlock(
                    type="text", text=f"错误：shell 命令执行超时（{detail}），整棵进程树已终止"
                )])
            except Exception as exc:  # noqa: BLE001
                detail = _brief(exc, 200)
                self._audit(emit, task_id, "run_shell", target, "failed", detail)
                return ToolResponse(content=[TextBlock(
                    type="text", text=f"错误：shell 命令启动失败：{detail}"
                )])

            self._audit(
                emit, task_id, "run_shell", target,
                "ok" if proc.returncode == 0 else "failed",
                f"exit={proc.returncode} timeout={timeout}s "
                f"limits={'degraded' if limits_note else 'on'} "
                f"container={'on' if container_note else 'off'}",
            )
            parts = ["shell：通用命令（用户显式授权）", f"退出码：{proc.returncode}"]
            if container_note:
                parts.append(container_note)
            if limits_note:
                parts.append("资源限制：" + limits_note)
            parts.append("stdout:\n" + (_cap(proc.stdout) or "（空）"))
            parts.append("stderr:\n" + (_cap(proc.stderr) or "（空）"))
            return ToolResponse(content=[TextBlock(type="text", text="\n".join(parts))])

        return run_shell

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
