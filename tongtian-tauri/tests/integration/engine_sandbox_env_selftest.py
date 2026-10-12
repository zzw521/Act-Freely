#!/usr/bin/env python3
"""
engine/server.py 工作环境通道自测（任务 #26/#27）

验证项：
1. /api/sandbox/environment/usage 磁盘实测（未落地即 0、递归统计、不跟随符号链接）
2. /api/sandbox/environment/clear 只删 environment/ 内的内容，会话记录与工作产物逐字节保留
3. 清理幂等：二次清理释放 0 字节
4. secureEnv 随 /api/chat 落到会话（引擎直连通道的隔离开关来源）
5. _task_work_dir / _preserve_artifacts_in_session 的隔离与归档边界

所有写入都发生在临时目录（tempfile.mkdtemp），不触碰任何真实数据目录。
fastapi.testclient 不可用时输出 SKIP 并以 0 退出（不阻塞无依赖环境）。

用法: python tests/integration/engine_sandbox_env_selftest.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    from fastapi.testclient import TestClient
    import server as engine_server
except Exception as exc:  # fastapi/httpx 缺失时优雅跳过
    print(f"SKIP: engine sandbox selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def write(path: Path, text: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tt-sandbox-env-"))
    engine_server.WORK_DIR = tmp
    env = tmp / "environment"
    try:
        client = TestClient(engine_server.app)

        # 1 ── 未落地即 0，不谎报占用
        usage = client.get("/api/sandbox/environment/usage").json()
        check(
            "用量：environment 未落地时字节数为 0",
            usage["bytes"] == 0 and usage["files"] == 0 and usage["workspaceCount"] == 0,
            str(usage)[:200],
        )
        check("用量：rootPath 即本次临时工作目录", Path(usage["rootPath"]) == tmp, str(usage)[:200])

        # 2 ── 递归实测，且只统计 environment/
        size_a = write(env / "toolchain" / "a.txt", "x" * 1024)
        size_b = write(env / "b.bin", "y" * 2048)
        write(tmp / "files" / "artifact.md", "工作产物")
        write(tmp / "sessions" / "conversation.md", "对话记录")
        usage = client.get("/api/sandbox/environment/usage").json()
        check(
            "用量：递归统计 environment 内的嵌套文件",
            usage["bytes"] == size_a + size_b and usage["files"] == 2,
            str(usage)[:200],
        )
        check("用量：environment 之外不计入（files/sessions 排除）", usage["bytes"] < 4096, str(usage)[:200])

        # 3 ── 清理范围与数据保护边界
        cleared = client.post("/api/sandbox/environment/clear").json()
        check(
            "清理：释放量与磁盘实测用量一致",
            cleared["freedBytes"] == size_a + size_b and cleared["freedFiles"] == 2,
            str(cleared)[:200],
        )
        check("清理：environment 目录本身保留、内容清空", env.is_dir() and not any(env.iterdir()))
        check(
            "清理：对话记录与工作产物逐字节保留",
            (tmp / "sessions" / "conversation.md").read_text(encoding="utf-8") == "对话记录"
            and (tmp / "files" / "artifact.md").read_text(encoding="utf-8") == "工作产物",
        )
        check("清理：无失败条目", cleared["failed"] == [], str(cleared)[:200])

        # 4 ── 幂等
        again = client.post("/api/sandbox/environment/clear").json()
        check("清理：幂等（二次清理释放 0 字节且无 cleared）",
              again["freedBytes"] == 0 and again["cleared"] == [], str(again)[:200])
        usage = client.get("/api/sandbox/environment/usage").json()
        check("清理：用量回落到 0 且工作空间仍可见",
              usage["bytes"] == 0 and usage["workspaceCount"] == 1, str(usage)[:200])

        # 5 ── 符号链接：不统计、不删除（本平台无权限时跳过）
        outside = tmp / "outside.txt"
        write(outside, "外部数据")
        link = env / "external-link"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError) as exc:
            print(f"[SKIP] 符号链接用例：当前平台无法创建符号链接（{exc}）")
        else:
            usage = client.get("/api/sandbox/environment/usage").json()
            check("用量：符号链接不跟随（不计入文件数）", usage["files"] == 0, str(usage)[:200])
            cleared = client.post("/api/sandbox/environment/clear").json()
            check(
                "清理：符号链接条目跳过并报 SANDBOX_ENV_CLEAR_SKIPPED_SYMLINK",
                any("SANDBOX_ENV_CLEAR_SKIPPED_SYMLINK" in f.get("error", "") for f in cleared["failed"]),
                str(cleared)[:200],
            )
            check("清理：链接指向的外部文件未被删除", outside.read_text(encoding="utf-8") == "外部数据")

        # 6 ── secureEnv 落到会话（隔离开关的引擎侧消费点）
        task = "selftest-secure-env"
        client.post("/api/chat", json={"message": "", "task_id": task, "secureEnv": True})
        session = engine_server._TASK_SESSIONS.get(task)
        check("会话：secureEnv=True 被记录", session is not None and session.secure_env is True)
        task_off = "selftest-secure-env-off"
        client.post("/api/chat", json={"message": "", "task_id": task_off, "secureEnv": False})
        session_off = engine_server._TASK_SESSIONS.get(task_off)
        check("会话：secureEnv=False 保持不隔离", session_off is not None and session_off.secure_env is False)

        # 7 ── 隔离目录与产物归档边界
        task_dir = engine_server._task_work_dir("t-iso")
        check("隔离：任务工作目录落在 environment/{task_id}",
              task_dir == env / "t-iso" and task_dir.is_dir())
        write(task_dir / "report.md", "报告正文")
        session_dir = tmp / "sessions" / "s-1"
        kept = engine_server._preserve_artifacts_in_session(session_dir, task_dir, [str(task_dir / "report.md")])
        kept_path = Path(kept[0])
        check(
            "隔离：产物归档到会话目录 artifacts 下且内容一致",
            kept_path == session_dir / "artifacts" / "report.md"
            and kept_path.read_text(encoding="utf-8") == "报告正文",
            str(kept)[:200],
        )
        outside_artifact = tmp / "files" / "other.md"
        write(outside_artifact, "原样返回")
        kept2 = engine_server._preserve_artifacts_in_session(session_dir, task_dir, [str(outside_artifact)])
        check("隔离：任务目录外的产物路径保持原样（不复制、不搬移）", kept2 == [str(outside_artifact)])
        check("隔离：无会话目录时降级返回原路径（不抛错）",
              engine_server._preserve_artifacts_in_session(None, task_dir, [str(outside_artifact)])
              == [str(outside_artifact)])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 50)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine sandbox env selftest: ALL PASS")


if __name__ == "__main__":
    main()
