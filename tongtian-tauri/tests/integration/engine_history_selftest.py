#!/usr/bin/env python3
"""
engine/server.py 对话提交覆盖项自测（任务 #38）

桌面端曾把「输入框选的模型档位 / 自定义模型 / 自选工作目录 / 此前轮次历史」丢掉：
Rust 侧装配的请求体没有这些键，引擎侧自然既不落会话也不参与执行。本文件从引擎
入口验证这四件事真的被接收、被记住、并被执行流取用：

1. _normalize_history 归一（非法条目、空正文、角色兜底、只留最近 40 条）
2. _effective_history 的两阶段语义（approve 不带历史 → 用会话里的；本轮传 [] → 保持为空）
3. POST /api/chat(stage=plan)：modelBinding / modelConfig / workDir / history 落到会话
4. POST /api/chat/review(approve)：执行流拿到的是 None（由会话补齐）+ 会话级 work_dir
5. 缺省键不清空：后续提交不带这些键时，会话里已有的设置逐项保留

所有写入都在临时目录（tempfile.mkdtemp）内，不触碰任何真实工作目录；模型调用与
执行流均被替身拦截，不产生任何网络请求。fastapi/httpx 不可用时输出 SKIP 并 0 退出。

用法: python tests/integration/engine_history_selftest.py
"""

import asyncio
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
    print(f"SKIP: engine chat overrides selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tt-chat-overrides-"))
    work_dir = tmp / "work"
    work_dir.mkdir()
    original_plan = engine_server._generate_plan
    original_run = engine_server._run_execution
    try:
        # 计划生成替身：避免真实模型调用（本自测只验证覆盖项的接收与沿用）
        async def fake_plan(goal: str):
            return ([{"step_id": "step-1", "name": goal[:10], "description": goal}], None)

        engine_server._generate_plan = fake_plan
        client = TestClient(engine_server.app)

        # 1 ── 历史归一
        normalize = engine_server._normalize_history
        check("归一：非列表入参一律视为无历史", normalize(None) == [] and normalize("abc") == []
              and normalize({"role": "user"}) == [])
        check("归一：丢弃非条目与空正文，缺角色按 user 处理", normalize(
            [{"role": "user", "content": "  "}, "x", {"content": "只有正文没有角色"},
             {"role": "user", "content": "保留"}]
        ) == [{"role": "user", "content": "只有正文没有角色"}, {"role": "user", "content": "保留"}])
        check("归一：未知角色回落 user，assistant 保留", normalize(
            [{"role": "system", "content": "s"}, {"role": "assistant", "content": "a"}]
        ) == [{"role": "user", "content": "s"}, {"role": "assistant", "content": "a"}])
        check("归一：只保留最近 40 条（丢弃更早轮次）",
              len(normalize([{"role": "user", "content": f"m{i}"} for i in range(60)])) == 40
              and normalize([{"role": "user", "content": f"m{i}"} for i in range(60)])[0]
              == {"role": "user", "content": "m20"})

        # 2 ── _effective_history 的两阶段语义
        probe = engine_server.TaskSession("probe-task")
        probe.history = [{"role": "user", "content": "上一轮"}]
        check("沿用：入参为 None 时取会话历史（approve 执行流不再重复带）",
              engine_server._effective_history(probe, None) == probe.history)
        check("区分：本轮显式传空列表保持为空，不翻出会话历史",
              engine_server._effective_history(probe, []) == [])
        check("覆盖：本轮显式带历史时以本轮为准",
              engine_server._effective_history(probe, [{"role": "user", "content": "本轮"}])
              == [{"role": "user", "content": "本轮"}])

        # 3 ── 桌面端四项选择在 plan 请求中落到会话
        task = "selftest-overrides"
        history_in = [{"role": "user", "content": "上一轮提问"}, {"role": "assistant", "content": "上一轮回复"}]
        resp = client.post(
            "/api/chat",
            json={
                "message": "整理本周经营数据",
                "task_id": task,
                "stage": "plan",
                "modelBinding": "qwen-flash",
                "modelConfig": {"baseUrl": "https://custom.example/v1", "apiKey": "sk-test", "modelName": "my-model"},
                "workDir": str(work_dir),
                "history": history_in,
            },
        )
        check("提交：stage=plan 返回 200 且产出计划流",
              resp.status_code == 200 and b"event: plan" in resp.content, str(resp.status_code))
        session = engine_server._TASK_SESSIONS.get(task)
        check("会话：modelBinding 被记录（模型档位不再被丢弃）",
              session is not None and session.model_binding == "qwen-flash", str(getattr(session, "model_binding", None)))
        check("会话：modelConfig 被记录（自定义模型端点随提交下发）",
              session.model_config == {"baseUrl": "https://custom.example/v1", "apiKey": "sk-test", "modelName": "my-model"},
              str(session.model_config))
        check("会话：workDir 被记录为该目录", Path(session.work_dir) == work_dir, str(session.work_dir))
        check("会话：history 被记录（两阶段下 approve 才执行，历史需要先存住）",
              session.history == history_in, str(session.history)[:200])
        check("会话：会话子目录建在所选工作目录内",
              session.session_dir is not None and str(session.session_dir).startswith(str(work_dir)),
              str(session.session_dir))

        # 4 ── approve 的执行流拿到的参数（先执行，再验证后续提交的缺省语义）
        captured: dict = {}

        async def fake_run(session_arg, task_id, goal, replay_from, model_binding=None,
                           work_dir_arg=None, history=None, model_config=None):
            captured.update({
                "goal": goal,
                "model_binding": model_binding,
                "work_dir": work_dir_arg,
                "history": history,
                "model_config": model_config,
                "effective_history": engine_server._effective_history(session_arg, history),
            })
            yield "data: {}\n\n"

        engine_server._run_execution = fake_run
        approved = client.post("/api/chat/review", json={"task_id": task, "action": "approve"})
        check("审阅：approve 返回 200", approved.status_code == 200, str(approved.status_code))
        check("执行流：沿用了会话里的目标（approve 请求体不重复带 message）",
              captured.get("goal") == "整理本周经营数据", str(captured)[:200])
        check("执行流：模型档位取自会话", captured.get("model_binding") == "qwen-flash", str(captured)[:200])
        check("执行流：自定义模型配置取自会话", bool(captured.get("model_config")), str(captured)[:200])
        check("执行流：工作目录取自会话（工具与产物限定其中）",
              captured.get("work_dir") is not None and Path(captured["work_dir"]) == work_dir,
              str(captured.get("work_dir")))
        check("执行流：历史入参为 None，由 _effective_history 从会话补齐",
              captured.get("history") is None and captured.get("effective_history") == history_in,
              str(captured)[:200])

        # 5 ── 缺省键不清空（Rust 只在「本轮选了」时才写键）
        client.post("/api/chat", json={"message": "继续", "task_id": task, "stage": "plan", "secureEnv": False})
        check("缺省：未带 modelBinding 时保留已绑定的模型档位", session.model_binding == "qwen-flash")
        check("缺省：未带 modelConfig 时保留自定义模型配置", session.model_config is not None)
        check("缺省：未带 workDir 时保留已选工作目录", Path(session.work_dir) == work_dir)
        check("缺省：未带 history 时保留此前轮次历史", session.history == history_in)

        # 6 ── 非法历史不得污染会话
        bad_task = "selftest-history-bad"
        client.post("/api/chat", json={
            "message": "第一条", "task_id": bad_task, "stage": "plan",
            "history": [{"role": "user", "content": "合法"}, {"role": "user", "content": ""}, 7],
        })
        bad = engine_server._TASK_SESSIONS.get(bad_task)
        check("健壮性：非法历史条目被过滤后写入会话，其余保留",
              bad.history == [{"role": "user", "content": "合法"}], str(bad.history)[:200])
        client.post("/api/chat", json={"message": "第二条", "task_id": bad_task, "stage": "plan", "history": []})
        check("健壮性：空历史不会覆盖会话里已有的历史",
              bad.history == [{"role": "user", "content": "合法"}])
    finally:
        engine_server._generate_plan = original_plan
        engine_server._run_execution = original_run
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 50)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine chat overrides selftest: ALL PASS")


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    main()
