#!/usr/bin/env python3
"""
真装配下的工单并发端到自测（缺陷 D-48.5e）

前面几刀的并发用例跑在**替身**上：D-48.5d 给调度器注入的是假 `execute_turn`，
D-48.5a 给 Dispatcher 注入的是假回合字典。替身证明了时序口径，但证明不了
「真实回合（模型往返 → 工具 → 副作用账本 → 通道投递 → 检查点落盘）跑在那套时序上
仍然成立」——真实装配里多了三件替身没有的东西：**账本的保序组与按轮放行上限、
真起的进程、以及模型正文到底从哪条通道落到了哪个会话**。

本刀把 `ticket_worker_adapter.build_dispatcher`（真注册表 / 槽位池 / outbox / 已读账本 /
看门狗 / 进程表）接上 `ticket_turn_scheduler`，在同一条代码路径上复验 D-48 验收项：

* ② Agent 槽位占满时人的唤醒仍立即送达并开跑（预留槽 + 活绑定复用），第 5 个 Agent
  工单与总槽占满时的新单都被**如实拒绝**且不留目录；
* ④ 同工单同会话严格保序，前序永久失败**停住整组**，只有显式放行才继续；
* ③ 后台作业到点被处置的是**真进程**（操作系统层面确认它没了，报告点名作业与时长）；
* 以及同工单单飞、在飞期间到达由续跑轮消费、finished 后的滞留唤醒点名 + 新领取带走、
  未知结局不采信、幂等键挡下、被拒动作不进账本但原话回给下一轮。

离线运行：模型与 IM 通道是脚本化假实现，一次 HTTP 也不发；唯一动真格的是 §9 那条后台
作业进程（`{sys.executable} -c "import threading;threading.Event().wait()"`——一个长驻的
真进程），用例里由看门狗处置掉，收尾再兜一道。`TONGTIANWORK_HOME` **每个用例指向自己的
临时目录**：outbox 与已读账本是跨工单共享的单文件账本，共用了就会拿别的用例的记录挡本节
判定的队头，测出来的 blocked 是假的。纯 stdlib，不碰 `engine/server.py` 与 Rust。

用法: python tests/integration/engine_ticket_worker_e2e_selftest.py
"""

import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TMP = Path(tempfile.mkdtemp(prefix="tt-ticket-e2e-selftest-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home-0")

sys.path.insert(0, str(PROJECT_ROOT / "engine"))

FAILURES: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


import checkpoint_runtime as cr  # noqa: E402
import ticket_outbox_runtime as tor  # noqa: E402
import ticket_turn_scheduler as tts  # noqa: E402
import ticket_worker_adapter as ta  # noqa: E402
import ticket_worker_runtime as twr  # noqa: E402

RIGS: list = []


# ── 假模型通道：按「唤醒正文里的工单标记」分派脚本 ─────────────────
class ScriptedModel:
    """`model_call(payload) -> {text, calls, outcome, reason}` 的脚本化实现。

    模型那一层只拿得到 `{system, messages, tools}`，**看不到工单 id**——身份靠唤醒
    正文里的标记（如 `[t1]`）识别：标记进第一轮提示词，之后随检查点里的对话历史一路
    带下去，续跑轮因此也认得出这是哪一单。顺带钉住一条真实口径：**上下文是跨轮续的**，
    模型在第二轮仍然看得见第一轮说过什么。

    闸门 / 栅栏 / 放行事件只作用在该键的**第一次**调用上——用来把回合真的按在飞。
    每次调用进入时快照 `messages` 正文：`execute_turn` 会往同一个列表里追加反馈，
    不快照就分不清「模型这次到底看到了什么」。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.order: list = []
        self.scripts: dict = {}
        self.gates: dict = {}
        self.barriers: dict = {}
        self.arrivals: dict = {}
        self.holds: dict = {}
        self.counts: dict = {}
        self.active: dict = {}
        self.max_active: dict = {}
        self.seen: dict = {}
        self.errors: list = []

    def add(self, key: str, replies: list, *, gate=None, barrier=None, arrive=None, hold=None) -> "ScriptedModel":
        self.order.append((key, f"[{key}]"))
        self.scripts[key] = list(replies)
        if gate is not None:
            self.gates[key] = gate
        if barrier is not None:
            self.barriers[key] = barrier
        if arrive is not None:
            self.arrivals[key] = arrive
        if hold is not None:
            self.holds[key] = hold
        return self

    def ask_count(self, key: str) -> int:
        with self.lock:
            return self.counts.get(key, 0)

    def request_texts(self, key: str, index: int) -> str:
        with self.lock:
            items = self.seen.get(key) or []
        if index < 1 or index > len(items):
            return ""
        return "\n".join(items[index - 1])

    def max_active_of(self, key: str) -> int:
        with self.lock:
            return self.max_active.get(key, 0)

    def __call__(self, request: dict) -> dict:
        messages = request.get("messages") or []
        blob = "\n".join(str(m.get("content") or "") for m in messages) + "\n" + str(request.get("system") or "")
        # 一份提示词里同时出现多个标记时以**最后登记**的脚本为准：滞留唤醒会把旧领取的
        # 标记带进新领取的上下文（§12），旧标记先登记，不让它把新一轮抢走。
        key = next((k for k, marker in reversed(list(self.order)) if marker in blob), "")
        with self.lock:
            self.counts[key] = self.counts.get(key, 0) + 1
            index = self.counts[key]
            self.active[key] = self.active.get(key, 0) + 1
            self.max_active[key] = max(self.max_active.get(key, 0), self.active[key])
            self.seen.setdefault(key, []).append([str(m.get("content") or "") for m in messages])
        arrive = self.arrivals.get(key)
        if arrive is not None:
            arrive.set()
        try:
            gate = self.gates.get(key)
            if gate is not None and index == 1 and not gate.wait(timeout=20.0):
                self.errors.append(f"闸门 {key} 20 秒没开——用例写错了")
            barrier = self.barriers.get(key)
            if barrier is not None and index == 1:
                barrier.wait(timeout=10.0)          # 只有真重叠才过得去
            hold = self.holds.get(key)
            if hold is not None and index == 1:
                go, timeout = hold
                if not go.wait(timeout=timeout):
                    self.errors.append(f"{key} 在模型里等了 {timeout}s 没被放行——回合上限挡住时本该如此")
            script = self.scripts.get(key) or [{"text": "", "calls": []}]
            reply = script[min(index - 1, len(script) - 1)]
            return {
                "text": str(reply.get("text") or ""),
                "calls": [dict(c) for c in (reply.get("calls") or [])],
                "outcome": reply.get("outcome", ""),
                "reason": reply.get("reason", ""),
            }
        finally:
            with self.lock:
                self.active[key] -= 1


class Channel:
    """假 IM 通道：按到达顺序记 (通道, 会话, 正文)，可让指定正文投递失败。

    通道收到的第一条是**通道 kind**（`parse_session_key` 把 `slack:C2` 拆成 kind + 会话 id
    后才交给 send_fn），不是外发记录的 kind（reply/relay 落在账本记录里）。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sent: list = []
        self.fail: set = set()

    def send(self, channel: str, target: str, text: str) -> None:
        if text in self.fail:
            raise ta.DeliveryError(f"通道拒绝投递「{text}」（用例设定）")
        with self.lock:
            self.sent.append((channel, target, text))

    def texts(self) -> list:
        with self.lock:
            return [t for _k, _s, t in self.sent]


def default_tools() -> dict:
    return {
        "run_command": lambda command: {"output": f"已执行：{command}"},
        "read_file": lambda path, units, start=0: {"text": f"{path} 的正文（预算 {units} 单元、起点 {start}）"},
        "list_dir": lambda path, limit: {"items": ["src", "README.md", "sleep-diag.log"][:limit]},
        "write_file": lambda path, content: {"reason": f"写入 {path}（{len(content)} 字）"},
    }


class Rig:
    """一套真装配 + 一个独占的数据根。

    `build_dispatcher` 交出来的 outbox / 已读账本 / 检查点目录都落在 `TONGTIANWORK_HOME`
    下（路径每次现读环境变量，所以能一节一个根）。共用一个根会让**别的用例**的保序组与
    幂等键掺进本节判断，测出来的「挡住」就不是真的。"""

    def __init__(
        self,
        *,
        agent_slots: int = 3,
        human_extra: int = 1,
        max_parallel_turns: int | None = None,
        max_followup_turns: int | None = None,
        inline: bool = True,
        goal: str = "把这条工单做完并如实收口",
        tools: dict | None = None,
        reply_limit: int | None = None,
    ):
        os.environ["TONGTIANWORK_HOME"] = str(_TMP / f"home-{len(RIGS) + 1}")
        self.label = f"rig#{len(RIGS) + 1}"
        self.model = ScriptedModel()
        self.channel = Channel()
        kw = dict(default_tools())
        kw.update(tools or {})
        self.parts = ta.build_dispatcher(
            model_call=self.model,
            send_fn=self.channel.send,
            goal=goal,
            agent_slots=agent_slots,
            human_extra=human_extra,
            **kw,
        )
        self.dispatcher = self.parts["dispatcher"]
        self.registry = self.parts["registry"]
        self.outbox = self.parts["outbox"]
        self.ledger = self.parts["ledger"]
        self.watchdog = self.parts["watchdog"]
        self.processes = self.parts["processes"]
        if reply_limit is not None:
            # reply 的并发上限默认 2（按轮放行）。四路并行那一节要把「没被这一趟放行」
            # 与「消息丢了」分清楚，所以那一节单独放宽上限，另有一节专门测默认口径。
            self.outbox.kind_limits[tor.KIND_REPLY] = reply_limit
        sched_kw: dict = {"thread_factory": tts.inline_thread_factory if inline else threading.Thread}
        if max_parallel_turns is not None:
            sched_kw["max_parallel_turns"] = max_parallel_turns
        if max_followup_turns is not None:
            sched_kw["max_followup_turns"] = max_followup_turns
        self.sched = tts.TicketTurnScheduler(self.dispatcher, **sched_kw)
        RIGS.append(self)

    def occupied(self) -> dict:
        return dict(self.registry.pool.snapshot()["occupied"])

    def worker(self, ticket_id: str):
        return self.registry.worker(ticket_id)

    def inbox(self, ticket_id: str) -> int:
        worker = self.worker(ticket_id)
        return len(worker.inbox) if worker is not None else 0

    def rounds(self, ticket_id: str) -> list:
        return self.sched.history(ticket_id)

    def last_round(self, ticket_id: str) -> dict:
        items = self.rounds(ticket_id)
        return items[-1] if items else {}

    def group_records(self) -> list:
        """账本里保序组的逐条记录（`snapshot()["records"]` 是条数，逐条只能走这里）。"""
        out: list = []
        for group in (self.outbox.snapshot()["orderedGroups"] or {}).values():
            out.extend(group)
        return out


def wait_until(cond, *, timeout: float = 8.0, poll: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(poll)
    return False


def reply(text: str, *calls, outcome: str = "", reason: str = "") -> dict:
    return {"text": text, "calls": list(calls), "outcome": outcome, "reason": reason}


def send_to(session: str, text: str, *, kind: str = "reply", dedupe: str = "") -> dict:
    return {"tool": "send_to_session", "args": {"kind": kind, "sessionKey": session, "text": text, "dedupeKey": dedupe}}


def finish(summary: str) -> dict:
    return {"tool": "finish_work", "args": {"summary": summary}}


# ── 1. 装配形状：真件齐了，调度器挂得上 ──────────────────────────

rig1 = Rig()
check(
    "build_dispatcher 交出七个真件",
    all(k in rig1.parts for k in ("dispatcher", "registry", "outbox", "ledger", "watchdog", "processes", "toolSpecs")),
    repr(sorted(rig1.parts)),
)
check(
    "交出来的都是真实运行时（不是替身）",
    isinstance(rig1.registry, twr.TicketWorkerRegistry) and isinstance(rig1.outbox, tor.TicketOutbox) and isinstance(rig1.processes, ta.JobProcessTable),
    repr([type(rig1.registry).__name__, type(rig1.outbox).__name__, type(rig1.processes).__name__]),
)
check("调度器直接吃这套真装配（端点接线只剩包一层 submit）", rig1.sched.dispatcher is rig1.dispatcher and rig1.sched.registry is rig1.registry, repr(rig1.sched.registry))
check("执行体工具面仍是那 6 个（装配没偷偷加宽）", [s["name"] for s in ta.tool_specs()] == list(ta.WORKER_TOOLS), repr([s["name"] for s in ta.tool_specs()]))
specs1 = {s["name"]: s["budget"] for s in ta.tool_specs()}
check("读取预算随工具面一起声明给模型（正文 4000/8000、列表 20/50）", "4000" in specs1["read_file"] and "8000" in specs1["read_file"] and "20" in specs1["list_dir"] and "50" in specs1["list_dir"], repr(specs1))
prompt1 = ta.worker_system_prompt(goal="把活干完", workspace_dir="worker-abc")
check("系统提示写明工作目录与「等待靠排队不靠轮询」（模型被告知了自己的边界）", "worker-abc" in prompt1 and "等待靠排队不靠轮询" in prompt1, prompt1[:160])

# ── 2. 最小闭环：一轮里外发 + 收口，正文真经通道 ──────────────────

rig2 = Rig()
rig2.model.add("a", [reply("[a] 先回一句", send_to("slack:C2", "已经开工"), finish("已回复并交付"))])
r2 = rig2.sched.submit(ticket_id="tk-a", title="最小闭环", assignment_id="as-a1", message="[a] 开工吧", origin="agent")
check("同步工厂下首轮当场跑完", r2["ok"] and r2["action"] == "turn-scheduled", repr(r2))
h2 = rig2.last_round("tk-a")
check("这一轮判定成功、以 finished 收口", h2.get("ok") and h2.get("ended") == "finished", repr(h2))
check("模型下的那条外发真经通道送出", rig2.channel.texts() == ["已经开工"], repr(rig2.channel.sent))
check("会话键被正确解析（slack:C2 → 通道 slack、会话 C2）", rig2.channel.sent[0] == ("slack", "C2", "已经开工"), repr(rig2.channel.sent[0]))
snap2 = rig2.outbox.snapshot()
check("账本里就一条记录且已 done（提交后才执行，执行完落 done）", snap2["records"] == 1 and snap2["statusCounts"].get("done") == 1, repr(snap2))
check("finished 收口把槽位归还（不留占位的死 worker）", rig2.occupied() == {}, repr(rig2.occupied()))
marks2 = rig2.ledger.marks_of("as-a1")
check("唤醒被落账为已读（读没读进历史看账本，不信执行体自述）", len(marks2) == 1 and rig2.ledger.is_read("as-a1", marks2[0]["messageId"]), repr(marks2))
check("finished 收口删掉了对话检查点（结清的活不留着当恢复点）", cr.load_checkpoint(rig2.worker("tk-a").checkpoint_key) is None, repr(rig2.worker("tk-a").checkpoint_key))

# ── 3. 同领取的第二轮从检查点读回第一轮的对话（上下文真续跑） ──────

rig3 = Rig()
rig3.model.add(
    "b",
    [
        reply("[b] 第一轮：先记下关键结论 42", send_to("slack:C3", "第一轮进度"), outcome="running"),
        reply("[b] 第二轮收口", send_to("slack:C3", "第二轮收口进度"), finish("已交付，结论是 42")),
    ],
)
r3a = rig3.sched.submit(ticket_id="tk-b", title="多轮", assignment_id="as-b1", message="[b] 开跑", origin="agent")
check("第一轮以 running 收口（模型没宣告完成就不许还槽）", r3a["ok"] and rig3.last_round("tk-b").get("ended") == "", repr(rig3.last_round("tk-b")))
check("running 保住槽位与检查点", "tk-b" in rig3.occupied() and bool(rig3.worker("tk-b").checkpoint_key), repr(rig3.occupied()))
cp3 = cr.load_checkpoint(rig3.worker("tk-b").checkpoint_key)
check("第一轮的对话真落到了检查点（不是只在内存里）", cp3 is not None and any("42" in str(m.get("content")) for m in cp3.get("messages") or []), repr(cp3 and len(cp3.get("messages") or [])))
r3b = rig3.sched.submit(ticket_id="tk-b", title="多轮", assignment_id="as-b1", message="[b] 继续把活收口", origin="agent")
check("第二轮复用同一份活领取（不新建 Assignment、不抢新槽）", r3b["action"] == "turn-scheduled" and rig3.worker("tk-b").assignment_id == "as-b1", repr({"submit": r3b, "aid": rig3.worker("tk-b").assignment_id}))
check("第二轮模型看到的上下文含第一轮的正文（检查点→提示词这条路是通的）", "关键结论 42" in rig3.model.request_texts("b", 2), rig3.model.request_texts("b", 2)[:240])
check("第二轮以 finished 收口并还槽", rig3.last_round("tk-b").get("ended") == "finished" and rig3.occupied() == {}, repr({"round": rig3.last_round("tk-b"), "occ": rig3.occupied()}))
check("两轮的外发按顺序都送出去了", rig3.channel.texts() == ["第一轮进度", "第二轮收口进度"], repr(rig3.channel.texts()))

# ── 4. 在飞期间到达：只投递，由同一次 claim 的续跑轮真消费 ─────────

gate4 = threading.Event()
rig4 = Rig(inline=False)
rig4.model.add(
    "c",
    [
        reply("[c] 第一轮卡住等活", send_to("slack:C4", "第一轮先说一句"), outcome="running"),
        reply("[c] 第二轮把补的活做完", send_to("slack:C4", "补的活也做完了"), finish("两轮都收口")),
    ],
    gate=gate4,
)
start4 = rig4.sched.submit(ticket_id="tk-c", title="在飞", assignment_id="as-c1", message="[c] 第一轮", origin="agent")
arrived4 = wait_until(lambda: rig4.model.ask_count("c") >= 1)
check("回合线程真起了（模型被问到第一次）", start4["ok"] and arrived4, repr({"submit": start4, "asked": rig4.model.ask_count("c")}))
busy4 = rig4.sched.submit(ticket_id="tk-c", title="在飞", assignment_id="as-c2", message="[c] 在飞时人又补了一条", origin="human")
check("在飞期间到达的第二条只投递、不起第二轮", busy4["action"] == "deliver-only" and busy4["delivered"] == "existing-worker", repr(busy4))
check("投递落在原来那份领取上（换了 assignment_id 也不裂成两份）", busy4["assignmentId"] == "as-c1" and rig4.worker("tk-c").assignment_id == "as-c1", repr(busy4["assignmentId"]))
check("第二条压在收件箱里等续跑消费，且本工单仍只占一个槽", rig4.inbox("tk-c") == 1 and sorted(rig4.occupied()) == ["tk-c"], repr({"inbox": rig4.inbox("tk-c"), "occ": sorted(rig4.occupied())}))
gate4.set()
idle4 = rig4.sched.wait_idle(timeout_seconds=20.0)
check("闸门开后两轮跑完，单飞位释放", idle4["ok"] and rig4.sched.snapshot()["inFlight"] == [], repr(idle4))
hist4 = rig4.rounds("tk-c")
check("第二轮是续跑轮并消费了那条在飞期间到达的唤醒", any(h["round"] == 2 and h["wakesConsumed"] == 1 for h in hist4), repr(hist4))
check("在飞那条的正文真进了第二轮提示词", "在飞时人又补了一条" in rig4.model.request_texts("c", 2), rig4.model.request_texts("c", 2)[:240])
check("同工单两轮从未重叠（真装配下也是单飞）", rig4.model.max_active_of("c") == 1, repr(rig4.model.max_active_of("c")))
check("两轮的外发按顺序落到同一会话", rig4.channel.texts() == ["第一轮先说一句", "补的活也做完了"], repr(rig4.channel.texts()))
check("以 finished 收口后槽位归还", rig4.occupied() == {}, repr(rig4.occupied()))

# ── 5. 跨工单真并行（栅栏只有重叠才过得去） ──────────────────────

bar5 = threading.Barrier(4)
arr5 = {f"p{i}": threading.Event() for i in range(1, 5)}
rig5 = Rig(agent_slots=4, human_extra=1, max_parallel_turns=4, inline=False, reply_limit=8)
for i in range(1, 5):
    rig5.model.add(f"p{i}", [reply(f"[p{i}] 四单并行中的一单", send_to(f"slack:P{i}", f"并行进度 {i}"), finish(f"工单 {i} 完成"))], barrier=bar5, arrive=arr5[f"p{i}"])
started5 = [rig5.sched.submit(ticket_id=f"tk-p{i}", title=f"并行{i}", assignment_id=f"as-p{i}", message=f"[p{i}] 起一轮", origin="agent") for i in range(1, 5)]
all_arrived5 = wait_until(lambda: all(ev.is_set() for ev in arr5.values()))
idle5 = rig5.sched.wait_idle(timeout_seconds=25.0)
check("四个工单的回合都跑完了（栅栏一次过 = 四路真重叠）", all(s["action"] == "turn-scheduled" for s in started5) and all_arrived5 and idle5["ok"], repr({"actions": [s["action"] for s in started5], "arrived": sum(ev.is_set() for ev in arr5.values()), "idle": idle5}))
check("栅栏没有断过（模型层没吃到 BrokenBarrierError）", rig5.model.errors == [], repr(rig5.model.errors))
check("四单各自的工作目录互不相同（工单级隔离在真装配下成立）", len({rig5.worker(f"tk-p{i}").dir_name for i in range(1, 5)}) == 4, repr([rig5.worker(f"tk-p{i}").dir_name for i in range(1, 5)]))
check("四单的外发各归各的会话", sorted({t[1] for t in rig5.channel.sent}) == ["P1", "P2", "P3", "P4"] and len(rig5.channel.sent) == 4, repr(rig5.channel.sent))
check("四轮都以 finished 收口、槽位全部归还", all(rig5.last_round(f"tk-p{i}").get("ended") == "finished" for i in range(1, 5)) and rig5.occupied() == {}, repr({"rounds": [rig5.last_round(f"tk-p{i}").get("ended") for i in range(1, 5)], "occ": rig5.occupied()}))
check("并发跑完账本没有互相覆盖（四条记录都在，全部 done）", rig5.outbox.snapshot()["records"] == 4 and rig5.outbox.snapshot()["statusCounts"].get("done") == 4, repr(rig5.outbox.snapshot()["statusCounts"]))

# ── 5b. 回合上限是排队不是丢；kind 上限是按轮放行不是丢 ────────────

go6 = threading.Event()
arr6 = {f"q{i}": threading.Event() for i in range(1, 4)}
rig6 = Rig(agent_slots=3, human_extra=1, max_parallel_turns=2, inline=False)
for i in range(1, 4):
    # 前两单被按在模型里（等 go6），才有时间去观察第三单确实没进模型：
    # 上限的实现是「线程先等并行额度信号量，等到才读收件箱」，所以挡住的是开跑不是投递。
    rig6.model.add(f"q{i}", [reply(f"[q{i}] 被上限挡住的那单", finish(f"工单 q{i} 完成"))], arrive=arr6[f"q{i}"], hold=(go6, 20.0))
started6 = [rig6.sched.submit(ticket_id=f"tk-q{i}", title=f"上限{i}", assignment_id=f"as-q{i}", message=f"[q{i}] 起一轮", origin="agent") for i in range(1, 4)]
two6 = wait_until(lambda: arr6["q1"].is_set() and arr6["q2"].is_set())
check("前两单真开跑了（都进了模型）", all(s["action"] == "turn-scheduled" for s in started6) and two6, repr({"actions": [s["action"] for s in started6], "q3": arr6["q3"].is_set()}))
third6_late = not arr6["q3"].wait(timeout=2.5)
check("第三单被本进程回合上限挡住（没进模型）", third6_late and rig6.model.ask_count("q3") == 0, repr({"asked": rig6.model.ask_count("q3")}))
check("被上限挡住不等于丢唤醒（那条还压在收件箱里等线程）", rig6.inbox("tk-q3") == 1, repr(rig6.inbox("tk-q3")))
go6.set()
idle6 = rig6.sched.wait_idle(timeout_seconds=25.0)
check("放行后第三单接着跑完（上限只是排队）", idle6["ok"] and rig6.model.ask_count("q3") == 1 and rig6.last_round("tk-q3").get("ended") == "finished", repr({"idle": idle6, "asked": rig6.model.ask_count("q3")}))

rig7 = Rig()
rig7.model.add("k", [reply("[k] 三条一起下", send_to("slack:K1", "第一趟发不出去的一条", dedupe="k1"), send_to("slack:K2", "第二趟", dedupe="k2"), send_to("slack:K3", "第三趟", dedupe="k3"), finish("三条已下"))])
r7 = rig7.sched.submit(ticket_id="tk-k", title="按轮放行", assignment_id="as-k1", message="[k] 跑一轮", origin="agent")
sent7 = rig7.channel.texts()
pending7 = rig7.outbox.snapshot()["statusCounts"].get("pending", 0)
check("默认 reply 上限 2：一趟只放行两条，第三条留在账本里（不是丢）", r7["ok"] and len(sent7) == 2 and pending7 == 1, repr({"sent": sent7, "pending": pending7}))
flush7 = rig7.outbox.run_pending(rig7.dispatcher.send_dispatch)
check("下一趟把没放行那条带走了（顺序不变、正文不重复）", flush7["dispatched"] == 1 and rig7.channel.texts() == ["第一趟发不出去的一条", "第二趟", "第三趟"], repr({"flush": flush7, "sent": rig7.channel.texts()}))
check("放行后账本没有半途卡住的在途记录", rig7.outbox.inflight_for("tk-k")["runningIds"] == [] and rig7.outbox.snapshot()["statusCounts"].get("pending", 0) == 0, repr(rig7.outbox.snapshot()["statusCounts"]))

# ── 6. 验收项②：Agent 槽位占满时，人的唤醒仍立即送达并开跑 ─────────

gates6 = {k: threading.Event() for k in ("w1", "w2", "w3", "w4", "hm")}
arrs6 = {k: threading.Event() for k in ("w1", "w2", "w3", "w4", "hm")}
rig8 = Rig(agent_slots=4, human_extra=1, max_parallel_turns=8, inline=False)
for k in ("w1", "w3", "w4"):
    rig8.model.add(k, [reply(f"[{k}] 占着槽位不放", finish(f"工单 {k} 完成"))], gate=gates6[k], arrive=arrs6[k])
rig8.model.add(
    "w2",
    [
        reply("[w2] 占着槽位这单还没做完", outcome="running"),
        reply("[w2] 把人补的活收掉", finish("工单 w2 完成（含人补的那条）")),
    ],
    gate=gates6["w2"],
    arrive=arrs6["w2"],
)
rig8.model.add("hm", [reply("[hm] 人新起的单跑完", finish("人的单已完成"))], gate=gates6["hm"], arrive=arrs6["hm"])
filled8 = [rig8.sched.submit(ticket_id=f"tk-{k}", title=f"占位{k}", assignment_id=f"as-{k}", message=f"[{k}] 起一轮", origin="agent") for k in ("w1", "w2", "w3", "w4")]
all_in8 = wait_until(lambda: all(arrs6[k].is_set() for k in ("w1", "w2", "w3", "w4")))
check("四个 Agent 工单真并行在飞（Agent 槽位占满）", all(f["action"] == "turn-scheduled" for f in filled8) and all_in8 and len(rig8.occupied()) == 4, repr({"occ": sorted(rig8.occupied()), "arrived": sum(arrs6[k].is_set() for k in ("w1", "w2", "w3", "w4"))}))
fifth8 = rig8.sched.submit(ticket_id="tk-w5", title="第五单", assignment_id="as-w5", message="[w5] Agent 起的第五个工单", origin="agent")
check("第五个 Agent 工单被如实拒绝（原话来自槽位池，不是调度器编的）", (not fifth8["ok"]) and "Agent 侧的 4 个槽位已占满" in fifth8["reason"], repr(fifth8))
check("被拒的第五单没留工作目录也没占槽（槽位裁决先于磁盘）", rig8.worker("tk-w5") is None and "tk-w5" not in rig8.occupied(), repr({"worker": rig8.worker("tk-w5"), "occ": sorted(rig8.occupied())}))
wake8 = rig8.sched.submit(ticket_id="tk-w2", title="占位2", assignment_id="as-w2-new", message="[w2] 人在槽位占满时补一条", origin="human")
check("人的唤醒立即送达在飞的工单（不排队、不占新槽）", wake8["ok"] and wake8["action"] == "deliver-only" and rig8.inbox("tk-w2") == 1, repr({"submit": wake8, "inbox": rig8.inbox("tk-w2")}))
check("这条投递没有等任何空位（四个 Agent 回合都还卡着）", all(not gates6[k].is_set() for k in ("w1", "w2", "w3", "w4")) and wake8["delivered"] == "existing-worker", repr({k: gates6[k].is_set() for k in gates6}))
new8 = rig8.sched.submit(ticket_id="tk-human", title="人的新单", assignment_id="as-hm", message="[hm] 人新起的单", origin="human")
human_in8 = wait_until(lambda: arrs6["hm"].is_set())
check("人新起的单用预留槽当场开跑（不被四个 Agent 工单挤掉）", new8["action"] == "turn-scheduled" and human_in8, repr({"submit": new8, "asked": rig8.model.ask_count("hm")}))
check("预留槽给到人之后占用数超过 Agent 上限也照常开跑", len(rig8.occupied()) == 5 and rig8.occupied()["tk-human"]["origin"] == "human", repr({k: v["origin"] for k, v in rig8.occupied().items()}))
sixth8 = rig8.sched.submit(ticket_id="tk-w6", title="总槽也满", assignment_id="as-w6", message="[w6] 总槽位也没了", origin="agent")
check("总槽位（含预留槽）占满时 Agent 新单同样被拒（不静默排队）", (not sixth8["ok"]) and "worker 槽位已满" in sixth8["reason"], repr(sixth8))
for ev in gates6.values():
    ev.set()
idle8 = rig8.sched.wait_idle(timeout_seconds=30.0)
check("放行后所有在飞工单收口", idle8["ok"] and rig8.sched.snapshot()["inFlight"] == [], repr(idle8))
check("人在槽满时补的那条被续跑轮真消费掉", any(h["round"] == 2 and h["wakesConsumed"] == 1 for h in rig8.rounds("tk-w2")), repr(rig8.rounds("tk-w2")))
check("人的新单跑完并结清（预留槽也归还）", rig8.last_round("tk-human").get("ended") == "finished" and rig8.occupied() == {}, repr({"round": rig8.last_round("tk-human"), "occ": rig8.occupied()}))
check("五路并发下没有吞掉的线程异常", rig8.sched.snapshot()["errors"] == [], repr(rig8.sched.snapshot()["errors"]))

# ── 7. 验收项④：同组保序、前序永久失败停住整组、显式放行才继续 ─────

rig9 = Rig()
rig9.model.add(
    "o",
    [
        reply(
            "[o] 三条一起下",
            send_to("slack:ORDER", "第一条", dedupe="o-1"),
            send_to("slack:ORDER", "第二条", kind="relay", dedupe="o-2"),
            send_to("slack:ORDER", "第三条", dedupe="o-3"),
            finish("三条外发已下"),
        ),
    ],
)
r9 = rig9.sched.submit(ticket_id="tk-o", title="保序", assignment_id="as-o1", message="[o] 按顺序发三条", origin="agent")
check("一轮里三条外发按下单顺序送出（同工单同会话不串序）", r9["ok"] and rig9.channel.texts() == ["第一条", "第二条", "第三条"], repr(rig9.channel.texts()))
check("三条都落在同一个会话上（组内保序的组就是它）", {t[1] for t in rig9.channel.sent} == {"ORDER"}, repr(rig9.channel.sent))
check("账本里这一组三条都 done（没有半途卡住的）", rig9.outbox.snapshot()["statusCounts"].get("done") == 3, repr(rig9.outbox.snapshot()["statusCounts"]))

rig10 = Rig()
rig10.model.add(
    "f",
    [
        reply("[f] 第一条发不出去", send_to("slack:BAD", "坏掉的第一条", dedupe="f-1"), send_to("slack:BAD", "被挡住的第二条", dedupe="f-2"), outcome="running"),
        reply("[f] 后面还想发", send_to("slack:BAD", "第三条", dedupe="f-3"), outcome="running"),
    ],
)
rig10.channel.fail.add("坏掉的第一条")
rig10.outbox.max_attempts = 1        # 首投即终态：真通道挂了不该无限重投
r10a = rig10.sched.submit(ticket_id="tk-f", title="失败停组", assignment_id="as-f1", message="[f] 先跑第一轮", origin="agent")
snap10 = rig10.outbox.snapshot()
failed_id10 = next((rec["id"] for rec in rig10.group_records() if rec["status"] == "failed"), "")
check("第一条投递失败后进终态（失败不藏在重试里）", r10a["ok"] and failed_id10 and snap10["statusCounts"].get("failed") == 1, repr(snap10["statusCounts"]))
check("第二条被前序挡在组内、没有越序送出", "被挡住的第二条" not in rig10.channel.texts(), repr(rig10.channel.texts()))
blocked10 = {rec["payload"]["text"]: rec for rec in rig10.group_records() if rec.get("blockedBy")}
check("账本写明第二条卡在谁前面（可诊断，不是静默不发包）", blocked10.get("被挡住的第二条", {}).get("blockedBy") == failed_id10, repr({k: v.get("blockedBy") for k, v in blocked10.items()}))
r10b = rig10.sched.submit(ticket_id="tk-f", title="失败停组", assignment_id="as-f1", message="[f] 再跑一轮", origin="agent")
check("后续轮的新外发同样被这个失败组挡住（整组停住，不是只挡当时那批）", "第三条" not in rig10.channel.texts() and len([rec for rec in rig10.group_records() if rec.get("blockedBy")]) >= 2, repr({"sent": rig10.channel.texts(), "blocked": [rec["payload"]["text"] for rec in rig10.group_records() if rec.get("blockedBy")]}))
flush10 = rig10.outbox.run_pending(rig10.dispatcher.send_dispatch)
check("再放行一次仍不动它，并把「不越序」的原话报出来", flush10["blocked"] >= 2 and any("不越序" in line for line in flush10["reasons"]), repr(flush10))
check("失败记录的账本里留着通道原话（为什么没送达看得见）", "通道拒绝投递" in str(rig10.outbox.record_view(failed_id10).get("error", "")), repr(rig10.outbox.record_view(failed_id10)))
resolved10 = rig10.outbox.resolve_block(failed_id10, "skip")
flush10b = rig10.outbox.run_pending(rig10.dispatcher.send_dispatch)
check("显式跳过之后，组里后续按**原顺序**放行（两个留痕出口之一）", resolved10.get("ok") and rig10.channel.texts() == ["被挡住的第二条", "第三条"], repr(rig10.channel.texts()))
check("放行后组内没有在途残留", rig10.outbox.inflight_for("tk-f")["runningIds"] == [], repr(rig10.outbox.inflight_for("tk-f")))

# ── 8. 未知结局不采信：动作从未被授权，一条也不提交 ────────────────

rig11 = Rig()
rig11.model.add("u", [reply("[u] 顺手发一条再说个怪结局", send_to("slack:U", "这条不该发出去"), outcome="mostly-done")])
r11 = rig11.sched.submit(ticket_id="tk-u", title="怪结局", assignment_id="as-u1", message="[u] 跑一轮", origin="agent")
h11 = rig11.last_round("tk-u")
check("第四种结局不采信（不猜「大概干完了」也不猜「还在跑」）", h11.get("ok") is False and "不采信" in str(h11.get("reason")), repr(h11))
check("未采信的回合一条也没发出去（模型下的外发从未被授权）", rig11.channel.texts() == [] and rig11.outbox.snapshot()["records"] == 0, repr({"sent": rig11.channel.texts(), "records": rig11.outbox.snapshot()["records"]}))
check("失败轮把领取置 dropped 并归还槽位", rig11.worker("tk-u").ended_status == "dropped" and rig11.occupied() == {}, repr({"ended": rig11.worker("tk-u").ended_status, "occ": rig11.occupied()}))
check("dropped 不覆盖对话检查点（没有半截上下文被当恢复点）", cr.load_checkpoint(rig11.worker("tk-u").checkpoint_key) is None, repr(rig11.worker("tk-u").checkpoint_key))

# ── 9. 验收项③：后台作业到点被处置的是真进程 ──────────────────────

py = sys.executable.replace("\\", "/")
# 看门狗要的是「一个还活着的真进程」，不是「一个会 sleep 的进程」。D-48.5f 之后
# `time.sleep(20)` 这类睡等写法在分类阶段就被 guard 拦下（见 §10），所以这里换一个
# 不靠睡等、而是长驻到被杀的进程——它同时也是 background 作业的真实形态：跑多久由
# 活儿决定，到点归看门狗管。
long_cmd = f"'{py}' -c 'import threading;threading.Event().wait()'"
rig12 = Rig()
rig12.model.add("j", [reply("[j] 起个后台作业再等它", {"tool": "run_shell", "args": {"command": long_cmd, "background": True}}, outcome="running")])
r12 = rig12.sched.submit(ticket_id="tk-j", title="后台作业", assignment_id="as-j1", message="[j] 起个长活", origin="agent")
h12 = rig12.last_round("tk-j")
handles12 = list(rig12.processes._handles.values())
check("回合结束但后台作业在跑 → 对外仍是 working、槽不还", h12.get("ok") and h12.get("doneState") == "working" and "tk-j" in rig12.occupied(), repr({"round": h12, "occ": sorted(rig12.occupied())}))
running12 = rig12.watchdog.running_for("tk-j")
check("看门狗里挂着这条作业（描述就是那条命令）", len(running12) == 1 and running12[0]["description"] == long_cmd[:120], repr(running12))
check("作业确实起了真进程（不是只登记一行字）", len(handles12) == 1 and handles12[0].poll() is None, repr(None if not handles12 else handles12[0].poll()))
killed12 = rig12.watchdog.tick(kill_fn=rig12.processes.kill_fn, now=time.time() + 31 * 60)
check("到 30 分钟上限被处置（验收项③在真装配上的证据）", len(killed12) == 1 and killed12[0]["killOk"] is True, repr(killed12))
check("被处置的是真进程：操作系统层面确认它已经没了", len(handles12) == 1 and handles12[0].poll() is not None, repr(None if not handles12 else handles12[0].poll()))
lines12 = rig12.watchdog.kill_report_lines(killed12)
check("报告点名了这条作业与「超过 30 分钟上限」（验收项③要求在报告里点名）", len(lines12) == 1 and long_cmd[:20] in lines12[0] and "超过 30 分钟上限，已中止" in lines12[0], repr(lines12))
check("杀完进程表里不留句柄（同一条命令可以再起，不会撞「已在表里」）", rig12.processes._handles == {}, repr(rig12.processes._handles))
check("看门狗的历史里留着这条被处置记录（复盘看得见）", len(rig12.watchdog.killed_history()) == 1 and rig12.watchdog.killed_history()[0]["jobId"] == killed12[0]["jobId"], repr(rig12.watchdog.killed_history()))

# ── 10. 工具面与守卫在真回合里：被拒的动作不进账本，原话回给下一轮 ──

rig13 = Rig()
rig13.model.add(
    "g",
    [
        reply("[g] 试个不存在的工具", {"tool": "browse_web", "args": {"url": "https://example.com"}}, send_to("slack:G", "进度一条")),
        reply("[g] 看到被拒就改对了", finish("已按 6 工具面收口")),
    ],
)
r13 = rig13.sched.submit(ticket_id="tk-g", title="工具面", assignment_id="as-g1", message="[g] 跑一轮", origin="agent")
check("未知工具被拒且没被执行（账本里只多了合法那条）", r13["ok"] and rig13.outbox.snapshot()["records"] == 1 and rig13.channel.texts() == ["进度一条"], repr({"records": rig13.outbox.snapshot()["records"], "sent": rig13.channel.texts()}))
check("被拒的原话回进了同一回合的下一轮往返（模型看得见自己哪里没做成）", "不在执行体的 6 个工具里" in rig13.model.request_texts("g", 2), rig13.model.request_texts("g", 2)[:240])
check("工具往返在同一次领取里收口（没有为一条被拒的调用新建领取）", rig13.worker("tk-g").assignment_id == "as-g1" and rig13.last_round("tk-g").get("ended") == "finished", repr({"aid": rig13.worker("tk-g").assignment_id, "round": rig13.last_round("tk-g")}))

rig14 = Rig()
rig14.model.add(
    "s",
    [
        reply("[s] 想靠 sleep 等", {"tool": "run_shell", "args": {"command": "sleep 60"}}),
        reply("[s] 改成立即返回", finish("没有靠轮询等")),
    ],
)
r14 = rig14.sched.submit(ticket_id="tk-s", title="禁等待", assignment_id="as-s1", message="[s] 跑一轮", origin="agent")
check("shell 里的等待类命令被 guard 拦下（等待靠排队不靠轮询）", r14["ok"] and rig14.processes._handles == {} and rig14.watchdog.running_for("tk-s") == [], repr({"handles": rig14.processes._handles, "jobs": rig14.watchdog.running_for("tk-s")}))
check("拦截理由进了对话（模型下一轮知道该怎么改）", "由唤醒（deliver_input）再拉起" in rig14.model.request_texts("s", 2), rig14.model.request_texts("s", 2)[:240])
check("解释器内联的 sleep 也被拦（D-48.5f：换 -c 写法不再是缺口）", ta.guard_shell_command("'x' -c 'import time;time.sleep(20)'")["ok"] is False, repr(ta.guard_shell_command("'x' -c 'import time;time.sleep(20)'")))
check("真起的长驻进程不误伤（看门狗的地盘不是 guard 的地盘）", ta.guard_shell_command(long_cmd)["ok"] is True, repr(ta.guard_shell_command(long_cmd)))
check("转 background 也不给睡等开绿灯", ta._classify_call({"tool": "run_shell", "args": {"command": "'x' -c 'import time;time.sleep(20)'", "background": True}}, default_session_key="")["channel"] == "reject")

# ── 11. 幂等：同 dedupeKey 重复下单只送一条，且挡下的事留在报告里 ───

rig15 = Rig()
rig15.model.add("d", [reply("[d] 同一句发两遍", send_to("slack:D", "只该发一次", dedupe="d-only"), send_to("slack:D", "只该发一次", dedupe="d-only"), finish("重复的已挡"))])
res15 = rig15.dispatcher.wake_and_run(ticket_id="tk-d", title="幂等", assignment_id="as-d1", message="[d] 跑一轮", origin="agent")
check("同幂等键的第二条被挡下（通道只收到一条）", res15["ok"] and rig15.channel.texts() == ["只该发一次"], repr(rig15.channel.texts()))
check("挡下的条数在结果里如实报告（sendsDeduped），notes 留原话", res15["sendsDeduped"] == 1 and any("幂等键挡下" in n for n in res15["notes"]), repr({"deduped": res15["sendsDeduped"], "notes": res15["notes"]}))
check("账本里没多占一条位", rig15.outbox.snapshot()["records"] == 1, repr(rig15.outbox.snapshot()["records"]))

# ── 12. 滞留唤醒：finished 收口后到达的唤醒被点名，新领取把它们带走 ─

gate16 = threading.Event()
rig16 = Rig(inline=False)
rig16.model.add("z", [reply("[z] 收口前占一会儿", finish("第一轮已完成"))], gate=gate16)
start16 = rig16.sched.submit(ticket_id="tk-z", title="滞留", assignment_id="as-z1", message="[z] 起一轮", origin="agent")
wait_until(lambda: rig16.model.ask_count("z") >= 1)
late16 = rig16.sched.submit(ticket_id="tk-z", title="滞留", assignment_id="as-z2", message="[z] 在飞时到达但这份领取即将结束", origin="human")
check("在飞期间到达的那条只进箱（不起第二轮）", late16["action"] == "deliver-only", repr(late16))
gate16.set()
idle16 = rig16.sched.wait_idle(timeout_seconds=20.0)
stranded16 = next((s for s in rig16.sched.snapshot()["strandedWakes"] if s["ticketId"] == "tk-z"), None)
check("领取以 finished 结束后滞留唤醒被点名（不是消失在箱里）", idle16["ok"] and stranded16 is not None and stranded16["queuedWakes"] == 1 and stranded16["endedStatus"] == "finished", repr({"idle": idle16, "stranded": rig16.sched.snapshot()["strandedWakes"]}))
check("点名说明写清楚要一次新领取才消费（带 prev_assignment_id）", stranded16 is not None and "prev_assignment_id" in str(stranded16.get("note")), repr(stranded16 and stranded16.get("note")))
check("结清之后箱里确实还压着那条（点名与磁盘事实一致）", rig16.inbox("tk-z") == 1 and rig16.worker("tk-z").ended_status == "finished", repr({"inbox": rig16.inbox("tk-z"), "ended": rig16.worker("tk-z").ended_status}))
rig16.model.add("z2", [reply("[z2] 新领取把带着的活做完", finish("续跑领取已收口"))])
carry16 = rig16.sched.submit(ticket_id="tk-z", title="滞留", assignment_id="as-z3", message="[z2] 新领取接着干", origin="agent", prev_assignment_id="as-z1")
idle16b = rig16.sched.wait_idle(timeout_seconds=20.0)
check("新领取带 prev 起回合时把滞留那条一起投递并消费掉（一条也不丢）", carry16["action"] == "turn-scheduled" and idle16b["ok"] and rig16.inbox("tk-z") == 0, repr({"submit": carry16, "idle": idle16b, "inbox": rig16.inbox("tk-z")}))
check("这一轮一次消费了两条唤醒（滞留的那条 + 新到的这条）", rig16.last_round("tk-z").get("wakesConsumed") == 2, repr(rig16.last_round("tk-z")))
check("恢复注记点名了携带的唤醒条数", "收件箱里还带着 1 条" in rig16.model.request_texts("z2", 1), rig16.model.request_texts("z2", 1)[:300])
check("消费掉之后滞留点名自动消失（报告不挂着已经解决的事）", all(s["ticketId"] != "tk-z" for s in rig16.sched.snapshot()["strandedWakes"]), repr(rig16.sched.snapshot()["strandedWakes"]))

# ── 13. 查询面在真装配下自洽（端点只读这些就能对人说清楚） ─────────

rig17 = Rig()
rig17.model.add("v", [reply("[v] 跑一轮", finish("已完成"))])
rig17.sched.submit(ticket_id="tk-v", title="查询面", assignment_id="as-v1", message="[v] 起", origin="human")
snap17 = rig17.sched.snapshot()
check("快照交出接线方要的全部口径：上限/在跑轮数/在飞/滞留/延迟/错误", set(("maxParallelTurns", "maxFollowupTurns", "turnsRun", "inFlight", "strandedWakes", "deferred", "errors")) <= set(snap17), repr(sorted(snap17)))
check("跑完之后在飞为空、错误为空（没有悬着的单飞位，也没有吞掉的异常）", snap17["inFlight"] == [] and snap17["errors"] == [], repr({"inFlight": snap17["inFlight"], "errors": snap17["errors"]}))
st17 = rig17.dispatcher.state_of("tk-v")
check("对外状态判定来自装配后的真件（结清即 free，且写明结束态）", st17["external"] == "free" and st17["endedStatus"] == "finished", repr(st17))
check("历史里逐轮记着结束态与消费的唤醒数（可复盘，不是只剩一句成功）", all(k in rig17.last_round("tk-v") for k in ("round", "ended", "wakesConsumed", "doneState", "atMs")), repr(rig17.last_round("tk-v")))

# ── 收尾 ────────────────────────────────────────────────────────

for rig in RIGS:
    for handle in list(rig.processes._handles.values()):
        try:
            handle.terminate()
        except Exception:  # noqa: BLE001  收尾尽力而为：遗留的测试进程不影响判定
            pass
    rig.sched.shutdown()

shutil.rmtree(_TMP, ignore_errors=True)

if FAILURES:
    print(f"\nFAILURES ({len(FAILURES)}):")
    for name in FAILURES:
        print(" -", name)
    sys.exit(1)
print("\nOK: 工单执行体真装配端到自测全部检查通过（并发、保序、真进程、收口口径跑在真件上，不只在替身上）")
