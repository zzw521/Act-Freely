#!/usr/bin/env python3
"""
本地子 Agent（多 Agent 第一层）自测

锁住「父 Agent 派发 → 子 Agent 真的执行 → 结果回到父 Agent」这条链，防止出现
「页面/提示词宣称有多 Agent，真实任务里从不派发」的假承诺：

1. 任务解析：字符串/对象两种写法、坏 JSON、缺 instruction 整体拒绝、长文本截断提示
2. 派生约束：单次 ≤4、累计 ≤4、最多 3 路并行、子 Agent 不能再派生、超时中止、
   单个子 Agent 失败不拖垮其它、SubagentStart 阻断与 SubagentStop 续跑上限
3. /api/chat 端到端（假模型 + 假 Hook 网关）：父 Agent 的工具面真实注册 spawn_subagents，
   子 Agent 工具面没有该工具；两路子任务并行、各自写文件、汇报汇总回父 Agent；
   SubagentStart/SubagentStop 真的上报；子 Agent 继承技能/沙箱/Hooks 但不继承父对话与意识
4. 沙箱把 spawn_subagents 拉黑时：不注册工具、不写提示词，并把原因摊进事件流

离线运行：不联网、无需 API Key；Hook 网关用 127.0.0.1 随机端口的假服务，
WORK_DIR 与 TONGTIANWORK_HOME 都换成临时目录，不触碰用户数据。
fastapi/agentscope 缺失时 SKIP 并以 0 退出。

用法: python tests/integration/engine_subagent_selftest.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import subagent_runtime as sar
    from agentscope.message import Msg
    from agentscope.tool import Toolkit
except Exception as exc:
    print(f"SKIP: subagent runtime selftest unavailable ({exc})")
    sys.exit(0)

try:
    from fastapi.testclient import TestClient
    import server as engine_server
    from agentscope.model import ChatModelBase, ChatResponse
    from agentscope.formatter import DashScopeChatFormatter
except Exception as exc:
    print(f"SKIP: 端到端依赖缺失（{exc}）")
    engine_server = None
    ChatModelBase = ChatResponse = DashScopeChatFormatter = None

FAILURES: list = []
RECORDED: list = []

CHILD_MARK = "只负责一个被明确指定的子任务"
CONTINUE_MARK = "SubagentStop Hook 要求你继续"
SAFE_PROMPT_MARK = "## 本地子 Agent"
SOUL_MARK = "SOUL-ONLY-PARENT"
SKILL_MARK = "技能描述：整理两段材料"
ALPHA_MARK = "ALPHA-RESULT"
BETA_MARK = "BETA-RESULT"

GATE_TOKEN = "selftest-token"
_ACTIVE = {"now": 0, "max": 0}
# 并行判定用事件栅栏，不靠 sleep 窗口：机器负载高时 50ms 可能被调度吞掉，
# 两个子 Agent 明明并行却测出串行，属于测试自身的假阴性。
_BOTH_STARTED = {"event": None}
_SCENARIO = {"name": "parallel"}


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _blob(messages) -> str:
    try:
        return json.dumps(messages, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return str(messages)


# ── 1. 纯函数层 ────────────────────────────────────────────────

def pure_checks() -> None:
    print("--- 1. 任务解析与提示词（纯函数层） ---")

    tasks, err = sar.parse_tasks(json.dumps(["整理材料 A", "整理材料 B"], ensure_ascii=False))
    check("字符串数组也能派发，标题自动生成",
          err is None and len(tasks) == 2 and tasks[0]["instruction"] == "整理材料 A"
          and tasks[1]["title"] == "子任务 2", str(tasks))

    tasks, err = sar.parse_tasks(json.dumps({"tasks": [
        {"title": "甲", "instruction": "写 alpha", "context": "参考旧版"},
        {"task": "写 beta"},
    ]}, ensure_ascii=False))
    check("对象数组接受 title/instruction/context，task 作为 instruction 别名",
          err is None and tasks[0]["context"] == "参考旧版" and tasks[1]["instruction"] == "写 beta",
          str(tasks))

    _, err = sar.parse_tasks("not-json")
    check("坏 JSON 如实报错", err is not None and "不是合法 JSON" in err, str(err))

    _, err = sar.parse_tasks("[]")
    check("空数组被拒绝", err is not None and "非空" in err, str(err))

    _, err = sar.parse_tasks(json.dumps([{"title": "没说明"}]))
    check("缺 instruction 整项拒绝并指出序号", err is not None and "第 1 项" in err, str(err))

    _, err = sar.parse_tasks(json.dumps(["好的", {"title": "坏的"}]))
    check("任意一项不合法 → 整体拒绝（不静默跳过）", err is not None and "第 2 项" in err, str(err))

    _, err = sar.parse_tasks(json.dumps([1]))
    check("非对象/字符串项被拒绝", err is not None and "不是对象或字符串" in err, str(err))

    runner = sar.SubagentRunner(make_agent=lambda n, i: None, emit=lambda e, d: None)
    long_task = {"title": "长任务", "instruction": "要" * (sar.MAX_INSTRUCTION_CHARS + 50), "context": ""}
    prompt = runner._child_prompt(long_task)
    check("子任务说明过长时截断并显式提示",
          "已截断" in prompt and len(prompt) < sar.MAX_INSTRUCTION_CHARS + 1200, str(len(prompt)))
    check("子 Agent 提示词包含总目标与执行要求",
          "总目标" in prompt and "不要向用户提问" in prompt and "不要派发新的子 Agent" in prompt,
          prompt[:200])
    hint = runner.system_hint()
    check("父 Agent 提示词写明数量与并行上限",
          str(sar.MAX_SUBAGENTS_PER_TASK) in hint and str(sar.MAX_CONCURRENCY) in hint
          and "看不到你此前的对话" in hint, hint)


# ── 2. 运行时约束（假 Agent，无网络）──────────────────────────

class FakeAgent:
    """最小 ReActAgent 替身：只需要 toolkit 与 __call__"""

    def __init__(self, text: str = "子任务完成", fail: bool = False, delay: float = 0.0):
        self.toolkit = Toolkit()
        self.text = text
        self.fail = fail
        self.delay = delay
        self.calls = 0

    async def __call__(self, msg):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("子 Agent 内部故障")
        return Msg(name="child", content=self.text, role="assistant")


class StubHooks:
    def __init__(self, blocked_event: str | None = None):
        self.blocked_event = blocked_event
        self.calls: list = []

    async def fire(self, event: str, payload: dict | None = None):
        self.calls.append((event, payload or {}))
        blocked = event == self.blocked_event
        return {
            "blocked": blocked,
            "notes": ["Hook 要求继续完善"] if blocked else [],
            "results": [],
        }


def _runner(make_agent, hooks=None, sandbox=None, emit=None) -> sar.SubagentRunner:
    # 生产接口是 make_agent(name, max_iters, expert)；这里的老用例不关心专家，包一层即可
    return sar.SubagentRunner(
        make_agent=lambda name, iters, expert: make_agent(name, iters),
        emit=emit or (lambda e, d: None),
        task_id="subagent-selftest",
        goal="把两段材料整理成两份文件",
        work_dir=".",
        sandbox=sandbox,
        hooks=hooks,
    )


def _spawn(runner: sar.SubagentRunner, tasks) -> str:
    fn = runner.tool()
    resp = asyncio.run(fn(json.dumps(tasks, ensure_ascii=False)))
    return resp.content[0]["text"]


def runner_checks() -> None:
    print("--- 2. 派生约束（假 Agent） ---")

    made: list[FakeAgent] = []
    emitted: list = []

    def make(text):
        agent = FakeAgent(text)
        made.append(agent)
        return agent

    runner = _runner(lambda n, i: make(f"{n} 完成"), emit=lambda e, d: emitted.append((e, d)))
    text = _spawn(runner, [
        {"title": "甲", "instruction": "写 alpha"},
        {"title": "乙", "instruction": "写 beta"},
    ])
    check("两个子任务并行完成并汇总", "2/2 个成功" in text and "甲 完成" in text and "乙 完成" in text, text)
    check("子 Agent 开始/结束都进事件流",
          sum(1 for e, d in emitted if e == "step_start" and d.get("stepType") == "subagent") == 2
          and sum(1 for e, d in emitted if e == "step_done") >= 2, str(emitted[:4]))
    check("汇总明确标注「未经验证」", "未经验证" in text, text)

    made.clear()
    runner = _runner(lambda n, i: make("不该执行"))
    text = _spawn(runner, [{"title": f"t{i}", "instruction": f"任务 {i}"} for i in range(5)])
    check("单次超过 4 个直接拒绝且一个都不启动",
          "单次最多派发 4 个" in text and made == [], text + str(len(made)))

    made.clear()
    runner = _runner(lambda n, i: make("ok"))
    _spawn(runner, [{"title": f"a{i}", "instruction": f"任务 {i}"} for i in range(3)])
    text = _spawn(runner, [{"title": "b1", "instruction": "补一个"}, {"title": "b2", "instruction": "再补"}])
    check("累计超过 4 个时拒绝并说明已派发数",
          "累计最多派发 4 个" in text and "已派发 3 个" in text and len(made) == 3, text)

    runner = _runner(lambda n, i: FakeAgent(f"{n} 成功", fail=(n == "坏任务")))
    text = _spawn(runner, [
        {"title": "好任务", "instruction": "正常执行"},
        {"title": "坏任务", "instruction": "会抛异常"},
    ])
    check("单个子 Agent 失败不拖垮其它（1/2 成功）",
          "1/2 个成功" in text and "子 Agent 内部故障" in text and "好任务 成功" in text, text)

    old_timeout = sar.TIMEOUT_SECONDS
    sar.TIMEOUT_SECONDS = 0.05
    try:
        runner = _runner(lambda n, i: FakeAgent("太慢了", delay=1.0))
        text = _spawn(runner, [{"title": "慢任务", "instruction": "睡一会"}])
        check("超时子 Agent 被中止并如实回报", "未完成" in text and "0/1" in text, text)
    finally:
        sar.TIMEOUT_SECONDS = old_timeout

    hooks = StubHooks(blocked_event="SubagentStart")
    made.clear()
    runner = _runner(lambda n, i: make("不该启动"), hooks=hooks)
    text = _spawn(runner, [{"title": "被拦", "instruction": "启动"}])
    check("SubagentStart 阻断时子 Agent 不启动",
          "被 SubagentStart Hook 阻断" in text and made == [], text)
    check("SubagentStart 携带 agent_type 与任务说明",
          hooks.calls and hooks.calls[0][0] == "SubagentStart"
          and hooks.calls[0][1].get("agent_type") == sar.AGENT_TYPE
          and "启动" in hooks.calls[0][1].get("instruction", ""), str(hooks.calls))

    hooks = StubHooks(blocked_event="SubagentStop")
    agent = FakeAgent("第一版结果")
    runner = _runner(lambda n, i: agent, hooks=hooks)
    text = _spawn(runner, [{"title": "续跑", "instruction": "做两版"}])
    stops = [c for c in hooks.calls if c[0] == "SubagentStop"]
    check("SubagentStop 阻断 → 最多续跑 1 次并说明上限",
          agent.calls == 2 and "已达到 1 次续跑上限" in text and len(stops) == 2,
          f"calls={agent.calls}, stops={len(stops)}, text={text}")


# ── 3. /api/chat 端到端（假模型 + 假 Hook 网关）───────────────

def _make_gate_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
            token = self.headers.get("x-tt-hook-token")
            _GATE["calls"].append({"token": token, "body": body})
            if token != GATE_TOKEN:
                self._reply(401, {"ok": False, "error": "HOOK_GATE_UNAUTHORIZED"})
                return
            event = body.get("event")
            blocked = False
            notes: list = []
            if event == "SubagentStop":
                agent_id = str((body.get("payload") or {}).get("agentId") or "")
                _GATE["stop_seen"][agent_id] = _GATE["stop_seen"].get(agent_id, 0) + 1
                if _GATE["stop_seen"][agent_id] == 1:
                    blocked = True
                    notes = ["请补充结论后再生效"]
            self._reply(200, {"ok": True, "result": {
                "blocked": blocked, "notes": notes, "results": [],
            }})

        def _reply(self, status: int, obj: dict) -> None:
            payload = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            pass

    return Handler


_GATE = {"calls": [], "stop_seen": {}}


class ScriptedModel(ChatModelBase):
    """父 Agent：先派发两个子任务，看到汇总后收尾；
    子 Agent：先写自己的文件，再汇报；被 Hook 要求续跑时给第二版结论。"""

    def __init__(self):
        super().__init__(model_name="fake-subagent-model", stream=False)

    async def __call__(self, messages, *args, **kwargs):
        blob = _blob(messages)
        RECORDED.append(blob)
        if CHILD_MARK not in blob:
            return self._parent(blob)
        return await self._child(blob, messages)

    def _parent(self, blob: str):
        if SAFE_PROMPT_MARK not in blob:
            return ChatResponse(content=[{"type": "text", "text": "本轮没有子 Agent 能力，直接收尾。"}])
        if (
            "子任务结果汇总" in blob
            or "单次最多派发" in blob
            or "累计最多派发" in blob
        ):
            return ChatResponse(content=[{"type": "text", "text": "父 Agent 已核对并整合两份子任务结果。"}])
        if _SCENARIO["name"] == "overflow":
            tasks = [{"title": f"任务{i}", "instruction": f"处理第 {i} 段"} for i in range(5)]
        else:
            tasks = [
                {"title": "甲任务", "instruction": f"把 {ALPHA_MARK} 写入 alpha.txt"},
                {"title": "乙任务", "instruction": f"把 {BETA_MARK} 写入 beta.txt"},
            ]
        return ChatResponse(content=[{
            "type": "tool_use",
            "id": "call-spawn-1",
            "name": "spawn_subagents",
            "input": {"tasks_json": json.dumps(tasks, ensure_ascii=False)},
        }])

    async def _child(self, blob: str, messages):
        # 总目标里同时出现 alpha.txt 与 beta.txt，只能用各自任务说明里的唯一标记区分
        marker = ALPHA_MARK if ALPHA_MARK in blob else BETA_MARK
        if CONTINUE_MARK in blob:
            return ChatResponse(content=[{"type": "text", "text": f"{marker} 续跑完成：已补充结论"}])

        target = "alpha.txt" if marker == ALPHA_MARK else "beta.txt"
        if len(messages) <= 2:
            _ACTIVE["now"] += 1
            _ACTIVE["max"] = max(_ACTIVE["max"], _ACTIVE["now"])
            if _ACTIVE["now"] >= 2:
                _BOTH_STARTED["event"].set()
            try:
                # 两路都进到模型调用后才继续：观测到的并发才是真并行，而不是时间片错开
                await asyncio.wait_for(_BOTH_STARTED["event"].wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            finally:
                _ACTIVE["now"] -= 1
            return ChatResponse(content=[{
                "type": "tool_use",
                "id": f"call-write-{target}",
                "name": "write_text_file",
                "input": {"file_path": target, "content": f"{marker} content"},
            }])
        return ChatResponse(content=[{"type": "text", "text": f"{marker} 完成：已写入 {target}"}])


def _start_gate() -> tuple:
    from http.server import ThreadingHTTPServer

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _make_gate_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/hook"


def _reset_gate() -> None:
    _GATE["calls"].clear()
    _GATE["stop_seen"].clear()
    RECORDED.clear()
    _ACTIVE["now"] = 0
    _ACTIVE["max"] = 0
    _BOTH_STARTED["event"] = asyncio.Event()


def _runtime(gate_url: str) -> dict:
    return {
        "awareness": {"mode": True, "files": {"SOUL.md": SOUL_MARK}},
        "skills": [{"id": "s1", "name": "Skill One", "description": SKILL_MARK,
                    "content": "正文", "kind": "skill", "source": "installed"}],
        "hooks": {"gateUrl": gate_url, "token": GATE_TOKEN, "configured": 1},
    }


def e2e_checks() -> None:
    print("--- 3. /api/chat 端到端（父 → 子 → 汇总） ---")
    engine_server.build_model_and_formatter = (
        lambda *a, **k: (ScriptedModel(), DashScopeChatFormatter())
    )
    srv, url = _start_gate()
    work_dir = Path(tempfile.mkdtemp(prefix="tt-subagent-http-"))
    engine_server.WORK_DIR = work_dir
    client = TestClient(engine_server.app)
    try:
        _SCENARIO["name"] = "parallel"
        _reset_gate()
        resp = client.post("/api/chat", json={
            "message": "把两段材料分别整理成 alpha.txt 与 beta.txt",
            "task_id": "subagent-parallel",
            "runtime": _runtime(url),
        })
        body = resp.text
        parents = [b for b in RECORDED if CHILD_MARK not in b]
        children = [b for b in RECORDED if CHILD_MARK in b]

        check("任务正常收尾", "event: done" in body and "event: error" not in body, body[:300])
        check("父 Agent 的工具面注册了 spawn_subagents，子 Agent 没有",
              len(parents) >= 1 and SAFE_PROMPT_MARK in parents[0] and children
              and all(SAFE_PROMPT_MARK not in c and "spawn_subagents" not in c for c in children),
              f"parents={len(parents)}, children={len(children)}")
        check("子 Agent 继承技能清单但不继承父意识",
              all(SKILL_MARK in c for c in children)
              and all(SOUL_MARK not in c for c in children),
              str([SKILL_MARK in c for c in children]))
        check("子 Agent 只收到总目标与自己的任务（看不到父对话）",
              all((ALPHA_MARK in c) != (BETA_MARK in c) for c in children)
              and all("把两段材料分别整理" in c for c in children),
              str(len(children)))
        check("两路子任务真的并行（观测到并发 ≥2）", _ACTIVE["max"] >= 2, str(_ACTIVE))
        check("子任务写出的文件真实落在工作目录",
              (work_dir / "alpha.txt").exists()
              and (work_dir / "alpha.txt").read_text(encoding="utf-8") == f"{ALPHA_MARK} content"
              and (work_dir / "beta.txt").read_text(encoding="utf-8") == f"{BETA_MARK} content",
              str(list(work_dir.iterdir())))
        check("父 Agent 收到两份子任务汇报并整合",
              any("子任务结果汇总" in p and "ALPHA-RESULT" in p and "BETA-RESULT" in p
                  for p in parents), str(len(parents)))
        check("子 Agent 的步骤以 subagent 类型进事件流（用户看得见）",
              body.count('"stepType": "subagent"') >= 2
              and body.count('"stepType": "tool_call"') >= 2, body[:500])

        events = [c["body"].get("event") for c in _GATE["calls"]]
        starts = [c["body"] for c in _GATE["calls"] if c["body"].get("event") == "SubagentStart"]
        stops = [c["body"] for c in _GATE["calls"] if c["body"].get("event") == "SubagentStop"]
        check("SubagentStart 每个子 Agent 各上报一次，带 agent_type",
              len(starts) == 2 and all(s["payload"].get("agent_type") == sar.AGENT_TYPE for s in starts),
              str(starts))
        check("SubagentStop 真的上报（含续跑后的第二次）",
              len(stops) == 4 and all(s["payload"].get("agentId") for s in stops), str(len(stops)))
        check("子 Agent 的工具调用也走父任务的 Hooks 门禁",
              "PreToolUse" in events and "PostToolUse" in events
              and any(c["body"].get("tool") == "write_text_file"
                      for c in _GATE["calls"] if c["body"].get("event") == "PreToolUse"),
              str(events))
        check("SubagentStop 续跑请求被交回子 Agent 并产生第二版结论",
              any("续跑完成" in p for p in parents), str(parents)[-400:])

        # 单次超过上限：不启动任何子 Agent，错误交回模型
        _SCENARIO["name"] = "overflow"
        _reset_gate()
        resp = client.post("/api/chat", json={
            "message": "一次派发五个子任务",
            "task_id": "subagent-overflow",
            "runtime": _runtime(url),
        })
        body = resp.text
        check("单次超限：错误交回模型且没有子 Agent 启动",
              "单次最多派发 4 个" in "\n".join(RECORDED)
              and not any(CHILD_MARK in b for b in RECORDED)
              and '"stepType": "subagent"' not in body, body[:300])

        # 沙箱拉黑 spawn_subagents：不注册、不写提示词、原因可见
        _SCENARIO["name"] = "sandbox"
        _reset_gate()
        runtime = _runtime(url)
        runtime["sandbox"] = {
            "configured": True,
            "executionLevel": "L1",
            "toolBlocklist": ["spawn_subagents"],
        }
        resp = client.post("/api/chat", json={
            "message": "尝试派发子任务",
            "task_id": "subagent-denied",
            "runtime": runtime,
        })
        body = resp.text
        check("沙箱拉黑：不注册派发工具，也不写进 system prompt",
              not any(SAFE_PROMPT_MARK in b for b in RECORDED), str(len(RECORDED)))
        check("沙箱拉黑：原因作为上下文说明进事件流",
              "本地子 Agent 未启用" in body and "工具黑名单" in body, body[:400])
        check("沙箱拉黑：本轮照常收尾", "event: done" in body and "event: error" not in body, body[-300:])
    finally:
        srv.shutdown()
        engine_server.WORK_DIR = Path(".")
        shutil.rmtree(work_dir, ignore_errors=True)


def main() -> None:
    prev_home = os.environ.get("TONGTIANWORK_HOME")
    temp_home = Path(tempfile.mkdtemp(prefix="tt-subagent-home-"))
    os.environ["TONGTIANWORK_HOME"] = str(temp_home)
    try:
        pure_checks()
        runner_checks()
        if engine_server is not None and ChatModelBase is not None:
            e2e_checks()
        else:
            print("SKIP: 端到端分组未运行（依赖缺失）")
    finally:
        if prev_home is None:
            os.environ.pop("TONGTIANWORK_HOME", None)
        else:
            os.environ["TONGTIANWORK_HOME"] = prev_home
        shutil.rmtree(temp_home, ignore_errors=True)
    print("-" * 52)
    if FAILURES:
        print(f"engine subagent selftest: {len(FAILURES)} 项失败")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("engine subagent selftest: ALL PASS")


if __name__ == "__main__":
    main()
