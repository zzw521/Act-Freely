"""系统级沙箱套壳（Windows AppContainer）。

桌面端启用「系统隔离」时，会向引擎进程注入三个变量：
- `TT_SANDBOX_BIN`：启动器路径（主程序的隐藏子命令 `--tt-sandbox-run`）
- `TT_SANDBOX_NETWORK`：`on` / `off`，本次会话是否允许出网
- `TT_SANDBOX_RUNTIME_DIRS`：额外可读目录（分号分隔）

真实子进程一律写成「启动器 + 参数 + `--` + 原命令」，由启动器以 AppContainer
令牌拉起、转发 stdio 与退出码。没有这三个变量就原样返回 `None`，调用方照常启动。

套壳失败必须拒绝执行（`SandboxUnavailable`），不允许静默降级成不受隔离的进程。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

CLI_FLAG = "--tt-sandbox-run"
PROFILE = "TongTian.Agent"


class SandboxUnavailable(RuntimeError):
    """系统隔离已启用，但无法把命令套进启动器：调用方必须拒绝执行"""


def launcher() -> str | None:
    raw = (os.environ.get("TT_SANDBOX_BIN") or "").strip()
    return raw or None


def enabled() -> bool:
    return launcher() is not None


def _network() -> str:
    value = (os.environ.get("TT_SANDBOX_NETWORK") or "").strip().lower()
    return "on" if value == "on" else "off"


def _runtime_dirs() -> list[str]:
    raw = os.environ.get("TT_SANDBOX_RUNTIME_DIRS") or ""
    return [item.strip() for item in raw.split(";") if item.strip()]


def read_dirs_for(command: str, args: list[str]) -> list[str]:
    """解释器自身与首个脚本参数所在目录：`node D:/mcp/server.js` 要读得到 `server.js`"""
    out: list[str] = []
    candidates: list[Path] = []
    resolved = shutil.which(command)
    if resolved:
        candidates.append(Path(resolved))
    for item in args[:1]:
        path = Path(item)
        if path.is_file():
            candidates.append(path)
    for path in candidates:
        parent = str(path.parent)
        if parent and parent not in out:
            out.append(parent)
    for item in _runtime_dirs():
        if item not in out:
            out.append(item)
    return out


def wrap(
    command: str,
    args: list[str],
    *,
    workspace: str | None = None,
    keep_env: list[str] | tuple[str, ...] = (),
) -> tuple[str, list[str]] | None:
    """返回 (启动器, 启动器参数)；未启用系统隔离时返回 None。

    `keep_env` 是用户为这个连接器显式配置的敏感变量名，启动器会保留它们
    （其余 TOKEN / SECRET 一类变量仍会被过滤，避免模型密钥泄进沙箱）。
    """
    exe = launcher()
    if not exe:
        return None
    if not Path(exe).is_file():
        raise SandboxUnavailable(
            f"SANDBOX_LAUNCHER_MISSING: 找不到沙箱启动器 {exe}，命令未执行"
        )
    argv = [CLI_FLAG, "--profile", PROFILE, "--network", _network()]
    if workspace:
        argv += ["--workspace", workspace]
    for directory in read_dirs_for(command, args):
        argv += ["--read-dir", directory]
    for name in keep_env:
        name = str(name).strip()
        if name:
            argv += ["--allow-env", name]
    argv += ["--", command, *args]
    return exe, argv
