#!/usr/bin/env python3
"""
工单回合的并发调度（缺陷 D-48.5d）自测

D-48.5 把三个运行时接成「一轮」，D-48.5b 给了注入点的真实实现，但**并发**这件事
一直没人管：引擎端口同时收到同一工单的两条唤醒时，两个线程各跑一轮就是两份检查点
互相覆盖、两条外发事务交错、一份领取被当成两轮的自述。这里锁的是调度层四条口径，
并且用**真线程**取证（模拟不出竞态）：

1. 同工单单飞：同一时刻只允许一轮在飞；投递先于复查单飞位，所以「一轮刚收尾、
   一条刚好进来」不会把唤醒丢在箱里没人管。
2. 跨工单真并行：4 个工单的回合真的同时开跑（栅栏只有重叠才过得去），上限确实
   挡住（cap=1 时同一道栅栏必然断）。
3. 续跑由收件箱驱动且有上限：到顶就松手并如实报告还剩几条，kick 能接手；
   领取已结束时的滞留唤醒被点名，新领取按恢复注记把它们带走。
4. 线程里抛出的异常不吞、单飞位必还；槽位裁决仍归 D-48.1 的槽位池（验收项②）。

顺带把装配级的验收项③（后台作业超 30 分钟被杀并点名）与④（同工单同会话不串序）
在「调度器 + Dispatcher + 三个运行时 + 适配器」这一整条链上各钉一条用例。

离线运行：模型 / HTTP / 进程全部由假实现注入（§11 用适配器的真实装配）；
`TONGTIANWORK_HOME` 指向临时目录。纯 stdlib。

用法: python tests/integration/engine_ticket_turn_scheduler_selftest.py
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

_TMP = Path(tempfile.mkdtemp(prefix="tt-ticket-turn-scheduler-selftest-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")

sys.path.insert(0, str(PROJECT_ROOT / "engine"))

FAILURES: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


import checkpoint_runtime as cr  # noqa: E402
import ticket_dispatch_runtime as tdr  # noqa: E402
import ticket_outbox_runtime as tor  # noqa: E402
import ticket_recovery_runtime as trr  # noqa: E402
import ticket_turn_scheduler as tts  # noqa: E402
import ticket_worker_adapter as ta  # noqa: E402
import ticket_worker_runtime as twr  # noqa: E402

CASE = {"n": 0}


def turn(**kw):
    base = {"ok": True, "outcome": "finished", "messages": [], "sends": [], "jobs": [], "reason": ""}
    base.update(kw)
    return base


class Recorder:
    """假 execute_turn：记录每工单的并发进入数、抓到的提示词，并可选在轮内制造到达。

    并发计数是这层的关键证据——`max_active[ticket] > 1` 就是「同一工单同时开两轮」，
    正是本刀要消灭的缺陷；栅栏（barrier）反过来证明跨工单真的重叠，串行就超时。"""

    def __init__(
        self,
        *,
        outcome="running",
        sleep_seconds=0.0,
        block_event=None,
        block_first_round_only=True,
        barrier=None,
        per_round=None,
        sends_for_round=None,
        jobs_for_round=None,
    ):
        self.lock = threading.Lock()
        self.outcome = outcome
        self.sleep_seconds = sleep_seconds
        self.block_event = block_event
        self.block_first_round_only = block_first_round_only
        self.barrier = barrier
        self.per_round = per_round
        self.sends_for_round = sends_for_round
        self.jobs_for_round = jobs_for_round
        self.active: dict = {}
        self.max_active: dict = {}
        self.prompts: dict = {}
        self.rounds = 0
        self.started = threading.Event()
        self.errors: list = []

    def texts(self, ticket_id: str) -> list:
        with self.lock:
            return list(self.prompts.get(ticket_id, []))

    def __call__(self, snapshot, prompt, history):
        tid = str(snapshot.get("ticketId") or "")
        self.started.set()
        with self.lock:
            self.rounds += 1
            mine = self.prompts.setdefault(tid, [])
            index = len(mine)
            mine.append(prompt)
            self.active[tid] = self.active.get(tid, 0) + 1
            self.max_active[tid] = max(self.max_active.get(tid, 0), self.active[tid])
        try:
            if self.barrier is not None:
                try:
                    self.barrier.wait(timeout=3.0)
                except Exception as exc:  # noqa: BLE001  断栏要在结果里如实可见
                    self.errors.append(f"{type(exc).__name__}")
                    raise
            if self.block_event is not None and (index == 0 or not self.block_first_round_only):
                self.block_event.wait(timeout=10.0)
            if self.sleep_seconds:
                time.sleep(self.sleep_seconds)
            if self.per_round is not None:
                self.per_round(tid, index, prompt)
        finally:
            with self.lock:
                self.active[tid] -= 1
        result = {"ok": True, "outcome": self.outcome, "messages": [{"role": "assistant", "content": f"{tid}#{index}"}]}
        if self.sends_for_round is not None:
            result["sends"] = list(self.sends_for_round(tid, index) or [])
        if self.jobs_for_round is not None:
            result["jobs"] = list(self.jobs_for_round(tid, index) or [])
        else:
            result["jobs"] = []
        return turn(**{k: v for k, v in result.items() if k != "outcome"}, outcome=self.outcome)


class Case:
    """一套互不污染的运行时 + 一台挂在调度器下的 Dispatcher。"""

    def __init__(self, *, agent_slots=3, human_extra=1, timeout_seconds=trr.JOB_TIMEOUT_SECONDS, **sched_kw):
        CASE["n"] += 1
        n = CASE["n"]
        self.n = n
        self.workers_root = _TMP / f"workers-{n}"
        self.registry = twr.TicketWorkerRegistry(
            agent_slots=agent_slots,
            human_extra=human_extra,
            workspace=twr.TicketWorkspace(self.workers_root),
        )
        self.outbox = tor.TicketOutbox(_TMP / f"outbox-{n}.json")
        self.ledger = trr.ReadLedger(_TMP / f"reads-{n}.json")
        self.now = {"t": 1_700_000_000.0}
        self.watchdog = trr.JobWatchdog(timeout_seconds=timeout_seconds, clock=lambda: self.now["t"])
        self.sent: list = []
        self.killed: list = []
        self.dispatcher = None
        self.sched_kw = sched_kw

    def dispatch(self, record):
        self.sent.append(record["payload"]["text"])
        return True

    def kill_ok(self, job):
        self.killed.append(job["jobId"])
        return True

    def kill_boom(self, job):
        self.killed.append(job["jobId"])
        raise OSError("权限不足")

    def mount(self, execute_turn, *, send_dispatch=None, kill_job=None, dispatcher_kw=None, **scheduler_kw):
        """挂一台 Dispatcher 并包上调度器；调度器参数走 **scheduler_kw，不混进 Dispatcher。"""
        self.dispatcher = tdr.TicketDispatcher(
            registry=self.registry,
            outbox=self.outbox,
            ledger=self.ledger,
            watchdog=self.watchdog,
            execute_turn=execute_turn,
            send_dispatch=send_dispatch or self.dispatch,
            kill_job=kill_job or self.kill_ok,
            **(dispatcher_kw or {}),
        )
        opts = dict(self.sched_kw)
        opts.update(scheduler_kw)
        return tts.TicketTurnScheduler(self.dispatcher, **opts)

    def advance(self, minutes):
        self.now["t"] += minutes * 60

    def occupied(self):
        return self.registry.pool.snapshot()["occupied"]

    def dirs(self):
        return sorted(p.name for p in self.workers_root.iterdir()) if self.workers_root.exists() else []

    def live_assignment(self, ticket_id):
        w = self.registry.worker(ticket_id)
        return w.assignment_id if w else ""


def submit_many(sched, items):
    """真线程并发投递：返回每条的即时答复（按提交顺序）。"""
    results = {}
    gate = threading.Barrier(len(items))

    def go(idx, kw):
        gate.wait(timeout=5.0)
        results[idx] = sched.submit(**kw)

    threads = [threading.Thread(target=go, args=(i, kw), daemon=True) for i, kw in enumerate(items)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    return [results[i] for i in range(len(items))]


# ── 1. 参数与容量：拒绝要说清「这条唤醒此刻在哪」 ──────────────

c = Case()
rec = Recorder(outcome="running")
sched = c.mount(rec)

bad_ticket = sched.submit(ticket_id="", message="x", assignment_id="as-1")
check("ticket_id 为空被拒（不是静默返回）", bad_ticket["ok"] is False and bad_ticket["action"] == "rejected", repr(bad_ticket))
bad_text = sched.submit(ticket_id="tk-1", message="   ", assignment_id="as-1")
check("空正文被拒并说明空消息不进收件箱", bad_text["ok"] is False and "空" in bad_text["reason"], repr(bad_text))
bad_aid = sched.submit(ticket_id="tk-1", message="干活", assignment_id="")
check("缺 assignment_id 被拒（检查点按领取命名）", bad_aid["ok"] is False and "assignment_id" in bad_aid["reason"], repr(bad_aid))
check("三次被拒都没有留下单飞位", sched.snapshot()["inFlight"] == [], repr(sched.snapshot()["inFlight"]))
check("三次被拒都没有占槽", c.occupied() == {}, repr(c.occupied()))
check("三次被拒连目录都没建（裁决先于磁盘）", c.dirs() == [], repr(c.dirs()))

for bad_kw, label in [({"max_parallel_turns": 0}, "回合上限为 0"), ({"max_followup_turns": 0}, "续跑上限为 0")]:
    try:
        tts.TicketTurnScheduler(tdr.TicketDispatcher(registry=c.registry, outbox=c.outbox, ledger=c.ledger, watchdog=c.watchdog, execute_turn=rec, send_dispatch=c.dispatch), **bad_kw)
        check(f"{label} 被当场拒绝", False, "构造成功了（等于允许一个永不推进的调度器）")
    except ValueError as exc:
        check(f"{label} 被当场拒绝", True, str(exc)[:60])

# ── 2. deliver=False 的续跑轮（Dispatcher 新增语义） ────────────

c2 = Case()
rec2 = Recorder(outcome="running")
d2 = tdr.TicketDispatcher(
    registry=c2.registry,
    outbox=c2.outbox,
    ledger=c2.ledger,
    watchdog=c2.watchdog,
    execute_turn=rec2,
    send_dispatch=c2.dispatch,
    kill_job=c2.kill_ok,
)
nobody = d2.wake_and_run(ticket_id="tk-none", deliver=False)
check("没有活领取时续跑轮当场失败（不顺手新建）", nobody["ok"] is False and nobody["stage"] == "advance", repr(nobody))
check("续跑轮的拒绝理由写明不新建领取/不占槽", "不新建领取" in nobody["reason"] and "不占槽" in nobody["reason"], nobody["reason"])
check("续跑轮没留下槽位", c2.occupied() == {}, repr(c2.occupied()))

c2.registry.start_or_wake(ticket_id="tk-live", title="续跑", assignment_id="as-live", message="第一条唤醒", origin="human")
c2.registry.start_or_wake(ticket_id="tk-live", title="续跑", assignment_id="as-other", message="第二条唤醒", origin="agent")
occupied_before = dict(c2.occupied())
round_before = rec2.rounds
cont = d2.wake_and_run(ticket_id="tk-live", deliver=False)
check("续跑轮消费收件箱里的两条", cont["ok"] and cont["wakesConsumed"] == 2 and cont["stage"] == "done", repr({k: cont[k] for k in ("ok", "wakesConsumed", "reason")}))
check("续跑轮没有将消息重投进收件箱", c2.registry.worker("tk-live").inbox == [], repr(c2.registry.worker("tk-live").inbox))
check("续跑轮不落新领取（检查点仍是原一份）", cont["assignmentId"] == "as-live" and c2.live_assignment("tk-live") == "as-live", repr(cont["assignmentId"]))
check("续跑轮不占新槽", c2.occupied() == occupied_before, repr(c2.occupied()))
check("两条唤醒都进了本轮提示词", "第一条唤醒" in rec2.texts("tk-live")[0] and "第二条唤醒" in rec2.texts("tk-live")[0], rec2.texts("tk-live")[0][:200])
check("续跑轮多跑了一轮（execute_turn 真被调用）", rec2.rounds == round_before + 1, repr(rec2.rounds))

empty_round = d2.wake_and_run(ticket_id="tk-live", deliver=False)
check("空收件箱的续跑轮如实说没有新唤醒（不假装收到过）", empty_round["ok"] and "没有新到达的唤醒" in rec2.texts("tk-live")[-1], rec2.texts("tk-live")[-1][:120])

# ── 3. 同工单单飞：六路并发只允许一轮在飞 ──────────────────────

c3 = Case(agent_slots=4, human_extra=1)
rec3 = Recorder(outcome="running", sleep_seconds=0.02)
sched3 = c3.mount(rec3)
six = [
    {"ticket_id": "tk-hot", "title": "同一个工单", "assignment_id": f"as-{i}", "message": f"第{i}条并发唤醒", "origin": "human"}
    for i in range(1, 7)
]
answers3 = submit_many(sched3, six)
idle3 = sched3.wait_idle(timeout_seconds=15.0)
actions3 = sorted({a["action"] for a in answers3})
scheduled3 = sum(1 for a in answers3 if a["action"] == "turn-scheduled")
check("六路并发投递全部被接受", all(a["ok"] for a in answers3), repr(answers3))
check("答复只有 turn-scheduled / deliver-only 两种", actions3 == ["deliver-only", "turn-scheduled"], repr(actions3))
check("每条投递要么拿到单飞位、要么只投递（没有第三种）", scheduled3 + sum(1 for a in answers3 if a["action"] == "deliver-only") == 6, repr(actions3))
check("至少一条拿到单飞位（不会六条全只投递没人跑）", scheduled3 >= 1, repr(scheduled3))
check("同一工单从未同时开两轮", rec3.max_active.get("tk-hot", 0) == 1, repr(rec3.max_active))
check("六条唤醒各被喂进会话恰好一次（不重投不丢）", all("".join(rec3.texts("tk-hot")).count(f"第{i}条并发唤醒") == 1 for i in range(1, 7)), repr([len(rec3.texts("tk-hot"))]))
consumed3 = sum(h["wakesConsumed"] for h in sched3.history("tk-hot"))
check("累计消费数等于投递数", consumed3 == 6, repr(consumed3))
check("全程只有一份领取在跑（单飞不裂成两轮）", len({h["assignmentId"] for h in sched3.history("tk-hot")}) == 1, repr(sched3.history("tk-hot")))
check("只占一个槽位", len(c3.occupied()) == 1, repr(c3.occupied()))
check("等待收尾没有超时", idle3["ok"] is True and idle3["inFlight"] == [], repr(idle3))

rec3.outcome = "finished"
final3 = sched3.submit(ticket_id="tk-hot", title="同一个工单", assignment_id="as-final", message="收个尾", origin="human")
sched3.wait_idle(timeout_seconds=15.0)
check("宣告完成后按 finished 收口并还槽", final3["ok"] and c3.occupied() == {}, repr({"answer": final3, "occupied": c3.occupied()}))
check("收口后单飞位已释放", sched3.snapshot()["inFlight"] == [], repr(sched3.snapshot()["inFlight"]))

# ── 4. 同步工厂下的最小闭环：占位 → 跑一轮 → 交还 ──────────────

c4 = Case()
rec4 = Recorder(outcome="finished")
sched4 = c4.mount(rec4, thread_factory=tts.inline_thread_factory)
first4 = sched4.submit(ticket_id="tk-sync", title="同步", assignment_id="as-s1", message="跑一轮", origin="agent")
check("同步工厂下首轮当场跑完", first4["ok"] and first4["action"] == "turn-scheduled", repr(first4))
check("跑完即交还单飞位（不留悬着的 claim）", sched4.snapshot()["inFlight"] == [], repr(sched4.snapshot()["inFlight"]))
check("finished 收口把槽位归还", c4.occupied() == {}, repr(c4.occupied()))
check("历史里这一轮记着 ended=finished", sched4.history("tk-sync")[-1]["ended"] == "finished", repr(sched4.history("tk-sync")[-1]))

# ── 5. 在飞期间到达：只投递、不起第二轮、由续跑消费 ────────────

gate5 = threading.Event()
c5 = Case()
rec5 = Recorder(outcome="running", block_event=gate5)
sched5 = c5.mount(rec5)
start5 = sched5.submit(ticket_id="tk-busy", title="在飞", assignment_id="as-b1", message="第一轮", origin="agent")
rec5.started.wait(timeout=5.0)
occ_before5 = dict(c5.occupied())
second5 = sched5.submit(ticket_id="tk-busy", title="在飞", assignment_id="as-b2", message="在飞时又来一条", origin="human")
idle5 = sched5.wait_idle(timeout_seconds=15.0)
check("在飞期间的第二条只投递不进第二轮", second5["action"] == "deliver-only" and second5["delivered"] == "existing-worker", repr(second5))
check("只投递不占新槽", c5.occupied() == occ_before5, repr({"before": occ_before5, "after": c5.occupied()}))
check("第二条由同一 claim 的续跑轮消费", any(h["round"] == 2 and h["wakesConsumed"] == 1 for h in sched5.history("tk-busy")), repr(sched5.history("tk-busy")))
check("第二条唤醒的正文进了续跑轮的提示词", "在飞时又来一条" in "".join(rec5.texts("tk-busy")), repr(rec5.texts("tk-busy")))
check("两轮从未重叠", rec5.max_active.get("tk-busy", 0) == 1, repr(rec5.max_active))
check("两轮从未重叠", rec5.max_active.get("tk-busy", 0) == 1, repr(rec5.max_active))
gate5.set()  # 放开在飞那轮：续跑轮才会接手刚投递进来的那条
idle5 = sched5.wait_idle(timeout_seconds=15.0)
check("首个投递拿到的是回合线程（不是只投递）", start5["action"] == "turn-scheduled", repr(start5))
check("收尾后单飞位释放", idle5["ok"] and sched5.snapshot()["inFlight"] == [], repr({"idle": idle5, "snap": sched5.snapshot()["inFlight"]}))

# ── 6. 跨工单真并行（栅栏只在重叠时过得去） ────────────────────

c6 = Case(agent_slots=4, human_extra=1)
bar6 = threading.Barrier(4)
rec6 = Recorder(outcome="running", barrier=bar6)
sched6 = c6.mount(rec6, max_parallel_turns=4)
tickets6 = [f"tk-p{i}" for i in range(1, 5)]
answers6 = [
    sched6.submit(ticket_id=t, title=f"并行工单{i}", assignment_id=f"as-p{i}", message=f"并行第{i}单", origin="agent")
    for i, t in enumerate(tickets6, 1)
]
idle6 = sched6.wait_idle(timeout_seconds=20.0)
hist6 = [h for h in sched6.history() if h["ticketId"] in tickets6]
check("四个工单的回合都成功（栅栏过了 = 真的同时在跑）", all(h["ok"] for h in hist6) and len(hist6) == 4, repr(hist6))
check("等待收尾没有超时", idle6["ok"] is True, repr(idle6))
check("四个工单各占一份领取、互不冒用", len({c6.live_assignment(t) for t in tickets6}) == 4, repr([c6.live_assignment(t) for t in tickets6]))
check("四个工单目录互不相同（隔离）", len(c6.dirs()) == 4, repr(c6.dirs()))
check("四个工单检查点键互不相同", len({h["assignmentId"] for h in hist6}) == 4, repr(hist6))
check("四个槽位都被占住（running 未收口）", len(c6.occupied()) == 4, repr(c6.occupied()))

c7 = Case(agent_slots=4, human_extra=1)
bar7 = threading.Barrier(4)
rec7 = Recorder(outcome="running", barrier=bar7)
sched7 = c7.mount(rec7, max_parallel_turns=1)
for i, t in enumerate([f"tk-s{i}" for i in range(1, 5)], 1):
    sched7.submit(ticket_id=t, title=f"串行{i}", assignment_id=f"as-s{i}", message=f"串行第{i}单", origin="agent")
idle7 = sched7.wait_idle(timeout_seconds=25.0)
hist7 = [h for h in sched7.history() if h["ticketId"] in {f"tk-s{i}" for i in range(1, 5)}]
check("回合上限为 1 时四单无法重叠（同一天花板确实挡住）", any(h["ok"] is False for h in hist7), repr(hist7))
check("挡住的原因在案（栅栏断裂被如实带回）", "BrokenBarrierError" in str(rec7.errors), repr(rec7.errors))
check("即使被挡住，单飞位也全部归还", idle7["ok"] and sched7.snapshot()["inFlight"] == [], repr({"idle": idle7, "snap": sched7.snapshot()["inFlight"]}))

# ── 7. 验收项②：Agent 槽占满时，人的唤醒仍立即送达并真跑 ──────

c8 = Case(agent_slots=3, human_extra=1)
rec8 = Recorder(outcome="running")
sched8 = c8.mount(rec8, max_parallel_turns=4)
for i in range(1, 4):
    sched8.submit(ticket_id=f"tk-full{i}", title=f"占位{i}", assignment_id=f"as-f{i}", message="干活", origin="agent")
sched8.wait_idle(timeout_seconds=15.0)
check("三个 Agent 工单把 Agent 槽占满", len([k for k, v in c8.occupied().items() if v["origin"] == "agent"]) == 3, repr(c8.occupied()))
blocked8 = sched8.submit(ticket_id="tk-agent4", title="第四个 Agent", assignment_id="as-f4", message="也要干活", origin="agent")
sched8.wait_idle(timeout_seconds=15.0)
check("第四个 Agent 工单被拒且理由说清预留槽不接 Agent", blocked8["ok"] is False and "预留槽不接 Agent" in blocked8["reason"], repr(blocked8))
check("被拒的 Agent 工单没留目录", "tk-agent4" not in c8.dirs() and len(c8.dirs()) == 3, repr(c8.dirs()))
human8 = sched8.submit(ticket_id="tk-agent4", title="第四个工单", assignment_id="as-h4", message="人的消息要立刻到", origin="human")
sched8.wait_idle(timeout_seconds=15.0)
check("同单改由人唤醒时立即送达（用掉人用预留槽）", human8["ok"] and human8["action"] == "turn-scheduled", repr(human8))
check("人的这一轮真的跑完并留下历史", any(h["ticketId"] == "tk-agent4" and h["ok"] for h in sched8.history("tk-agent4")), repr(sched8.history("tk-agent4")))
check("此刻占用为 3 Agent + 1 人", len(c8.occupied()) == 4, repr(c8.occupied()))
fifth8 = sched8.submit(ticket_id="tk-five", title="第五个", assignment_id="as-h5", message="人也满了", origin="human")
sched8.wait_idle(timeout_seconds=10.0)
check("总槽位也占满时人同样被如实拒绝", fifth8["ok"] is False and "槽位已满" in fifth8["reason"], repr(fifth8))

# ── 8. 续跑上限：到顶如实报告，kick 能接手，报告随消费消失 ─────

c9 = Case()
arrivals = {"n": 0}


def arrive_during_round(tid, index, prompt):
    """在轮内制造一条到达：模拟「这一轮正在跑，用户又发了一句」。"""
    if index == 0:
        arrivals["n"] += 1
        c9.registry.start_or_wake(ticket_id=tid, title="上限", assignment_id="as-x2", message="轮内新到的唤醒", origin="human")


rec9 = Recorder(outcome="running", per_round=arrive_during_round)
sched9 = c9.mount(rec9, max_followup_turns=1, thread_factory=tts.inline_thread_factory)
first9 = sched9.submit(ticket_id="tk-cap", title="上限", assignment_id="as-x1", message="开工", origin="agent")
snap9 = sched9.snapshot()
check("首尾两轮之内没有插队起第二轮（上限=1 只跑首轮）", first9["ok"] and len(sched9.history("tk-cap")) == 1, repr(sched9.history("tk-cap")))
check("到上限时如实报告还压着几条", len(snap9["deferred"]) == 1 and snap9["deferred"][0]["queuedWakes"] == 1, repr(snap9["deferred"]))
check("报告写明不自动连推的理由与出路", "上限" in snap9["deferred"][0]["note"] and "kick" in snap9["deferred"][0]["note"], repr(snap9["deferred"][0]["note"]))
check("上限到点时单飞位已交还（不卡住工单）", snap9["inFlight"] == [], repr(snap9["inFlight"]))
kicked9 = sched9.kick(ticket_id="tk-cap")
check("kick 接手把压着的唤醒推进", kicked9["ok"] and kicked9["action"] == "kick-scheduled", repr(kicked9))
check("kick 之后那条唤醒被消费", any(h["wakesConsumed"] == 1 for h in sched9.history("tk-cap")[1:]), repr(sched9.history("tk-cap")))
check("消费后缺口报告随之消失（不再挂着旧账）", sched9.snapshot()["deferred"] == [], repr(sched9.snapshot()["deferred"]))
noop9 = sched9.kick(ticket_id="tk-cap")
check("收件箱空时 kick 不起空转轮", noop9["ok"] is False and "空转" in noop9["reason"], repr(noop9))

# ── 9. 验收项③（装配级）：后台作业超 30 分钟被杀并在报告里点名 ─

c10 = Case()
jobs_round = {0: [{"jobId": "job-build", "description": "npm run build"}], 1: []}
rec10 = Recorder(outcome="running", jobs_for_round=lambda tid, i: jobs_round.get(i, []))
sched10 = c10.mount(rec10, thread_factory=tts.inline_thread_factory)
r10a = sched10.submit(ticket_id="tk-job", title="打包", assignment_id="as-j1", message="先起个包", origin="agent")
check("登记作业时未到点（本轮没杀任何东西）", r10a["ok"] and c10.killed == [] and c10.watchdog.running_for("tk-job"), repr({"killed": c10.killed, "running": c10.watchdog.running_for("tk-job")}))
check("作业在跑时对外状态是 working 不是 waiting", c10.dispatcher.state_of("tk-job")["external"] == "working", repr(c10.dispatcher.state_of("tk-job")))
c10.advance(31)
r10b = sched10.submit(ticket_id="tk-job", title="打包", assignment_id="as-j1", message="到点了吧", origin="agent")
killed_lines = [h for h in sched10.history("tk-job")]
report = c10.watchdog.kill_report_lines()
check("超过 30 分钟的作业被杀", c10.killed == ["job-build"], repr(c10.killed))
check("报告点名被杀的作业与时长", any("npm run build" in line and "30 分钟" in line for line in report), repr(report))
check("杀掉的作业不再算在跑", c10.watchdog.running_for("tk-job") == [], repr(c10.watchdog.running_for("tk-job")))

c11 = Case()
jobs_round11 = {0: [{"jobId": "job-stuck", "description": "长驻进程"}]}
rec11 = Recorder(outcome="running", jobs_for_round=lambda tid, i: jobs_round11.get(i, []))
sched11 = c11.mount(rec11, kill_job=c11.kill_boom, thread_factory=tts.inline_thread_factory)
sched11.submit(ticket_id="tk-kill", title="杀不掉", assignment_id="as-k1", message="起个进程", origin="agent")
c11.advance(45)
sched11.submit(ticket_id="tk-kill", title="杀不掉", assignment_id="as-k1", message="再试一次", origin="agent")
report11 = c11.watchdog.kill_report_lines()
check("杀不掉时如实说杀不掉（不吞异常）", any("中止失败" in line and "需要人工处理" in line for line in report11), repr(report11))
check("杀不掉时 kill_fn 确实被调用过", c11.killed == ["job-stuck"], repr(c11.killed))

# ── 10. 验收项④（装配级）：同工单同会话按轮次顺序出去 ──────────

c12 = Case()
sends12 = {
    0: [
        {"kind": "reply", "sessionKey": "slack:C9", "payload": "第一句", "dedupeKey": "s-1"},
        {"kind": "reply", "sessionKey": "slack:C9", "payload": "第二句", "dedupeKey": "s-2"},
    ],
    1: [{"kind": "reply", "sessionKey": "slack:C9", "payload": "第三句", "dedupeKey": "s-3"}],
}
rec12 = Recorder(outcome="running", sends_for_round=lambda tid, i: sends12.get(i, []))
sched12 = c12.mount(rec12, thread_factory=tts.inline_thread_factory)
sched12.submit(ticket_id="tk-order", title="保序", assignment_id="as-o1", message="发两句", origin="agent")
sched12.submit(ticket_id="tk-order", title="保序", assignment_id="as-o1", message="再补一句", origin="agent")
check("同会话正文按轮次顺序送达（不串序）", c12.sent == ["第一句", "第二句", "第三句"], repr(c12.sent))
check("账本里三条都落 done", c12.outbox.snapshot()["statusCounts"].get("done") == 3, repr(c12.outbox.snapshot()["statusCounts"]))

c13 = Case()
sends13 = {0: [{"kind": "reply", "sessionKey": "slack:C9", "payload": "会失败的那句", "dedupeKey": "f-1"}]}
rec13 = Recorder(outcome="running", sends_for_round=lambda tid, i: sends13.get(i, []))
sched13 = c13.mount(rec13, send_dispatch=lambda record: False, thread_factory=tts.inline_thread_factory)
r13 = sched13.submit(ticket_id="tk-block", title="阻塞", assignment_id="as-b9", message="发一句", origin="agent")
check("投递失败时退避重投（不静默成功）", r13["ok"] and c13.outbox.snapshot()["statusCounts"].get("failed", 0) + c13.outbox.snapshot()["statusCounts"].get("pending", 0) > 0, repr(c13.outbox.snapshot()["statusCounts"]))

# ── 11. 领取已结束时的滞留唤醒：点名 + 新领取带走 ─────────────

c14 = Case()
stranded_msg = "收口时又进来的唤醒"


def arrive_at_finish(tid, index, prompt):
    """只在第一轮内制造一条到达：这一条会在收口之后仍压在箱里（滞留的证据）。"""
    if index == 0:
        c14.registry.start_or_wake(ticket_id=tid, title="滞留", assignment_id="as-late", message=stranded_msg, origin="human")


rec14 = Recorder(outcome="finished", per_round=arrive_at_finish)
sched14 = c14.mount(rec14, thread_factory=tts.inline_thread_factory)
done14 = sched14.submit(ticket_id="tk-end", title="滞留", assignment_id="as-e1", message="干完这条", origin="agent")
snap14 = sched14.snapshot()
check("以 finished 收口后槽位归还", done14["ok"] and c14.occupied() == {}, repr(c14.occupied()))
check("在飞期间到达的唤醒被点名滞留", len(snap14["strandedWakes"]) == 1 and snap14["strandedWakes"][0]["queuedWakes"] == 1, repr(snap14["strandedWakes"]))
check("滞留报告写明 ended=finished 且需要新的领取", snap14["strandedWakes"][0]["endedStatus"] == "finished" and "新的领取" in snap14["strandedWakes"][0]["note"], repr(snap14["strandedWakes"][0]["note"]))
check("滞留时不再自动连推（续跑轮只落在活领取上）", len(sched14.history("tk-end")) == 1, repr(sched14.history("tk-end")))
stuck14 = sched14.kick(ticket_id="tk-end")
check("对已结束领取 kick 被拒并给出出路", stuck14["ok"] is False and "submit" in stuck14["reason"], repr(stuck14))

rec14.outcome = "running"
carry14 = sched14.submit(ticket_id="tk-end", title="滞留", assignment_id="as-e2", message="重新领一遍", origin="human", prev_assignment_id="as-e1")
carry_prompt = rec14.texts("tk-end")[-1]
check("新领取把滞留唤醒一起带走", carry14["ok"] and sched14.history("tk-end")[-1]["wakesConsumed"] == 2, repr(sched14.history("tk-end")[-1]))
check("滞留唤醒的正文与新消息都进了提示词", stranded_msg in carry_prompt and "重新领一遍" in carry_prompt, carry_prompt[:200])
check("恢复注记按 carried_wakes 点名这批唤醒", "还带着 1 条" in carry_prompt, carry_prompt[:200])
check("消费后滞留报告消失", sched14.snapshot()["strandedWakes"] == [], repr(sched14.snapshot()["strandedWakes"]))

# ── 12. 线程异常不吞、单飞位必还 ──────────────────────────────


class BoomDispatcher:
    """只满足调度器用到的接口：wake_and_run 当场抛，验证异常不被吞在线程里。"""

    def __init__(self, registry):
        self.registry = registry
        self.calls = 0

    def wake_and_run(self, **kw):
        self.calls += 1
        raise RuntimeError("回合里没预料的炸法")

    def state_of(self, ticket_id):
        return {"ticketId": str(ticket_id), "external": "unknown"}


c15 = Case()
boom15 = BoomDispatcher(c15.registry)
c15.registry.start_or_wake(ticket_id="tk-boom", title="炸", assignment_id="as-boom", message="先绑一份", origin="human")
sched15 = tts.TicketTurnScheduler(boom15)
boom_answer = sched15.submit(ticket_id="tk-boom", title="炸", assignment_id="as-boom", message="这条会撞上异常", origin="human")
idle15 = sched15.wait_idle(timeout_seconds=10.0)
check("异常被接受而不是静默丢弃", boom_answer["ok"] is True, repr(boom_answer))
check("线程里的异常进了 errors（不吞）", any("回合线程异常" in e and "没预料的炸法" in e for e in sched15.snapshot()["errors"]), repr(sched15.snapshot()["errors"]))
check("异常后单飞位被释放（工单不会被永久卡住）", idle15["ok"] and sched15.snapshot()["inFlight"] == [], repr({"idle": idle15, "snap": sched15.snapshot()["inFlight"]}))
again15 = sched15.submit(ticket_id="tk-boom", title="炸", assignment_id="as-boom", message="还能再投", origin="human")
sched15.wait_idle(timeout_seconds=10.0)
check("释放后可以再次投递并真的跑了一轮", again15["action"] in ("turn-scheduled", "deliver-only") and boom15.calls >= 2, repr({"answer": again15, "calls": boom15.calls}))


class DeadThread:
    def __init__(self, **kw):
        pass

    def start(self):
        raise OSError("线程创建失败：资源不足")


c16 = Case()
rec16 = Recorder(outcome="running")
sched16 = c16.mount(rec16, thread_factory=DeadThread)
dead16 = sched16.submit(ticket_id="tk-nodev", title="起不来", assignment_id="as-nd", message="这条投不出去", origin="human")
check("线程起不来时如实回拒绝（不假装已排程）", dead16["ok"] is False and dead16["action"] == "rejected", repr(dead16))
check("线程起不来时唤醒没被丢（已进收件箱并报了条数）", dead16.get("queuedWakes", 0) >= 1 and c16.registry.worker("tk-nodev").inbox, repr(dead16))
check("线程起不来时单飞位当场退回", sched16.snapshot()["inFlight"] == [], repr(sched16.snapshot()["inFlight"]))
check("线程起不来也留下中文错误记录", any("回合线程起不来" in e for e in sched16.snapshot()["errors"]), repr(sched16.snapshot()["errors"]))

# ── 13. 真装配：适配器 + 调度器跑通一整个工单 ─────────────────

delivered17: list = []
plan17 = [
    {
        "text": "收到，先回一句",
        "calls": [{"tool": "send_to_session", "args": {"kind": "reply", "sessionKey": "slack:C17", "text": "已开始处理", "dedupeKey": "w-1"}}],
    },
    {"text": "完成", "calls": [{"tool": "finish_work", "args": {"summary": "已回复"}}]},
]
n17 = {"n": 0}


def model17(payload: dict) -> dict:
    got = plan17[min(n17["n"], len(plan17) - 1)]
    n17["n"] += 1
    return got


parts17 = ta.build_dispatcher(
    model_call=model17,
    send_fn=lambda kind, target, text: delivered17.append((kind, target, text)) or True,
    goal="回复客户",
    agent_slots=3,
    human_extra=1,
    workspace=twr.TicketWorkspace(_TMP / "workers-wired"),
)
sched17 = tts.TicketTurnScheduler(parts17["dispatcher"], thread_factory=tts.inline_thread_factory)
answer17 = sched17.submit(ticket_id="tk-wired", title="客户咨询", assignment_id="as-w1", message="帮我回复客户", origin="human")
snap17 = sched17.snapshot()
check("装配级：调度器驱动真适配器跑完一单", answer17["ok"] and any(h["ok"] and h["ended"] == "finished" for h in sched17.history("tk-wired")), repr(sched17.history("tk-wired")))
check("装配级：外发经适配器落到通道", delivered17 == [("slack", "C17", "已开始处理")], repr(delivered17))
check("装配级：收口后槽位归还且检查点删除", parts17["registry"].pool.snapshot()["occupied"] == {} and cr.load_checkpoint(sched17.history("tk-wired")[0]["assignmentId"] and twr.checkpoint_task_id("as-w1")) is None, repr(parts17["registry"].pool.snapshot()["occupied"]))
state17 = sched17.state_of("tk-wired")
check("装配级：state_of 一次交出调度器视角与对外状态", state17["scheduler"]["inFlight"] is False and state17["worker"]["external"] == "free", repr(state17))

sd17 = sched17.shutdown(timeout_seconds=2.0)
check("shutdown 在无在飞时回报干净收尾", sd17["ok"] and sd17["inFlight"] == [], repr(sd17))

# ── 14. 查询面形状（接线端点只读这些字段） ────────────────────

snap18 = sched.snapshot()
check("快照字段齐备（端点不必自己拼判定）", set(snap18) == {"maxParallelTurns", "maxFollowupTurns", "turnsRun", "inFlight", "strandedWakes", "deferred", "errors"}, repr(sorted(snap18)))
check("turnsRun 统计的是真实跑过的轮数", snap18["turnsRun"] == len(sched.history()), repr({"turnsRun": snap18["turnsRun"], "history": len(sched.history())}))
state18 = sched.state_of("tk-1")
check("state_of 交出的 worker 判定来自 Dispatcher", set(state18) == {"ticketId", "scheduler", "worker"} and "external" in state18["worker"], repr(state18))

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} 项")
    for name in FAILURES:
        print(f"  - {name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(1)
print("OK: 工单回合并发调度（单飞/真并行/续跑上限/滞留点名）全部检查通过")
shutil.rmtree(_TMP, ignore_errors=True)
