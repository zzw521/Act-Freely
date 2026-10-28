#!/usr/bin/env python3
"""系统沙箱套壳自测（引擎侧）。

锁住这些不能退让的约束：
1. 没有 `TT_SANDBOX_BIN` 时 `wrap()` 返回 None，原有启动路径完全不变；
2. 开启后命令行结构正确：启动器标记 / profile / 网络 / 工作区 / 只读目录 /
   允许保留的环境变量，全部落在 `--` 之前，原命令原参数落在 `--` 之后；
3. 配了沙箱却找不到启动器时必须抛 `SandboxUnavailable`，不许静默降级；
4. 终端会话真的接了这一步：启动器缺失时报 `TERMINAL_SANDBOX_FAILED`，不留下半个会话。

用法: python tests/integration/engine_sandbox_wrapper_selftest.py
"""

import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

import sandbox_launcher as sandbox
import terminal_runtime as trm

ENV_KEYS = ("TT_SANDBOX_BIN", "TT_SANDBOX_NETWORK", "TT_SANDBOX_RUNTIME_DIRS")
checks = 0


def check(condition: bool, message: str) -> None:
    global checks
    if not condition:
        raise AssertionError(message)
    checks += 1


def clear_env() -> None:
    for key in ENV_KEYS:
        os.environ.pop(key, None)


def case_disabled() -> None:
    clear_env()
    check(not sandbox.enabled(), "未配置时不应认为已启用")
    check(sandbox.wrap("python", ["-V"]) is None, "未配置时必须原样启动，不套壳")


def case_argv_shape() -> None:
    exe = sys.executable
    os.environ["TT_SANDBOX_BIN"] = exe
    os.environ["TT_SANDBOX_NETWORK"] = "on"
    os.environ["TT_SANDBOX_RUNTIME_DIRS"] = str(PROJECT_ROOT)
    wrapped = sandbox.wrap(
        "python",
        ["-c", "print(1)"],
        workspace="D:/ws",
        keep_env=["GITHUB_TOKEN", ""],
    )
    check(wrapped is not None, "已配置时必须套壳")
    program, argv = wrapped
    check(program == exe, "启动器应使用 TT_SANDBOX_BIN")
    check(argv[0] == sandbox.CLI_FLAG, "首参数应为隐藏子命令标记")
    sep = argv.index("--")
    head, tail = argv[:sep], argv[sep + 1 :]
    check(sandbox.PROFILE in head, "必须带 profile 名")
    check(head[head.index("--network") + 1] == "on", "网络授权应透传")
    check(head[head.index("--workspace") + 1] == "D:/ws", "工作区应透传")
    check("--read-dir" in head, "解释器目录应作为只读授权")
    check(str(PROJECT_ROOT) in head, "运行时目录应透传")
    check("GITHUB_TOKEN" in head, "用户显式配置的变量名应保留")
    check("" not in head, "空变量名不应写进命令行")
    check(tail == ["python", "-c", "print(1)"], "原命令与参数应完整落在 -- 之后")


def case_missing_launcher() -> None:
    clear_env()
    os.environ["TT_SANDBOX_BIN"] = str(Path(tempfile.gettempdir()) / "tt-sandbox-not-there.exe")
    try:
        sandbox.wrap("python", ["-V"])
    except sandbox.SandboxUnavailable as exc:
        check("SANDBOX_LAUNCHER_MISSING" in str(exc), "错误码应可定位")
    else:
        raise AssertionError("启动器缺失时必须抛 SandboxUnavailable，不能放行")


def case_terminal_refuses() -> None:
    clear_env()
    os.environ["TT_SANDBOX_BIN"] = str(Path(tempfile.gettempdir()) / "tt-sandbox-not-there.exe")
    os.environ["TT_TERMINAL_SHELL"] = f'"{sys.executable}" -V'
    workdir = Path(tempfile.mkdtemp(prefix="tt-sandbox-term-"))
    try:
        manager = trm.TerminalManager()
        try:
            manager.open(str(workdir))
        except trm.TerminalError as exc:
            check(exc.code == "TERMINAL_SANDBOX_FAILED", f"应报终端沙箱失败，实际 {exc.code}")
        else:
            raise AssertionError("启动器缺失时终端不应创建会话")
    finally:
        os.environ.pop("TT_TERMINAL_SHELL", None)
        clear_env()


def main() -> int:
    original = {key: os.environ.get(key) for key in ENV_KEYS}
    try:
        case_disabled()
        case_argv_shape()
        case_missing_launcher()
        case_terminal_refuses()
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print(f"OK: {checks} 项断言通过")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
