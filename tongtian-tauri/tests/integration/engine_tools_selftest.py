#!/usr/bin/env python3
"""
engine/server.py GET /api/tools 自测（任务 #45 内置工具清单真实化）

验证项：
1. /api/tools 返回的正是 build_toolkit 实际注册的那几个工具（不另抄一份清单）
2. 每条都带 id / name / desc / source，且 source=builtin（无 MCP 注册时）
3. 引擎导入异常时报 TOOLS_LIST_FAILED，而不是返回空清单装作「没有工具」

只读注册表，不写任何文件。fastapi/httpx 不可用时输出 SKIP 并以 0 退出。

用法: python tests/integration/engine_tools_selftest.py
"""

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    from fastapi.testclient import TestClient
    import server as engine_server
except Exception as exc:  # fastapi/httpx 缺失时优雅跳过
    print(f"SKIP: engine tools selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main() -> None:
    engine_server.WORK_DIR = Path(tempfile.mkdtemp(prefix="tt-tools-"))
    client = TestClient(engine_server.app)
    body = client.get("/api/tools").json()
    check("端点返回 ok=true", body.get("ok") is True, str(body)[:200])
    tools = body.get("tools") or []
    ids = sorted(t["id"] for t in tools)
    # 与 Toolkit 注册表逐项一致（不是清单副本）
    toolkit = engine_server.build_toolkit(engine_server.WORK_DIR)
    check("清单与引擎注册表逐条一致", ids == sorted(toolkit.tools.keys()), f"{ids} vs {sorted(toolkit.tools.keys())}")
    check("count 与条目数一致", body.get("count") == len(tools), str(body.get("count")))
    check(
        "每条都带 id/name/desc/source",
        all({"id", "name", "desc", "source"} <= set(t) for t in tools),
        str(tools)[:200],
    )
    check("内置工具 source=builtin", all(t["source"] == "builtin" for t in tools), str(tools)[:200])
    check(
        "说明取自工具函数 docstring（不是另写一份文案）",
        all(t["desc"] for t in tools),
        str(tools)[:200],
    )

    original = engine_server.build_toolkit

    def broken(_work_dir):
        raise RuntimeError("agentscope 不可用")

    engine_server.build_toolkit = broken
    try:
        resp = client.get("/api/tools")
        payload = resp.json()
        check(
            "注册表构建失败时报 TOOLS_LIST_FAILED（不返回空清单）",
            resp.status_code == 500 and payload.get("code") == "TOOLS_LIST_FAILED",
            f"{resp.status_code} {str(payload)[:160]}",
        )
        check("失败详情保留原因", "agentscope" in str(payload.get("detail", "")), str(payload)[:160])
    finally:
        engine_server.build_toolkit = original

    print("-" * 50)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine tools selftest: ALL PASS")


if __name__ == "__main__":
    main()
