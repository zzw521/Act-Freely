#!/usr/bin/env python3
"""
重启后的唤醒补查与新领取续跑（缺陷 D-48.5g）

D-48.5c 用真子进程 + 真 kill 证明了**副作用层**重启不重放；但编排层一直没人回答另一个
问题：**收件箱是内存件**。进程没了，活领取、槽位、看门狗、箱里压着的唤醒全没了，而
「上一进程里哪几条唤醒从没进过会话历史」这件事恰好只有那份内存知道——`state_of` 里的
`unreadWakes` 在空注册表上只会返回空列表，查询本身随进程死了。本刀给的就是这条编排：
`TicketRestartResume.plan` 按已读账本把调用方（服务端消息表）给的候选分成「没进过会话」
与「进过，不再重投」，`resume` 只把前者投给一次**新领取**并起一轮。

本文件钉住的口径（每条都问过「什么缺陷会让它红」）：

* **上下文真的借到了**——走调度器 `submit(prev_assignment_id=…)` 的续跑此前 `historyMessages`
  恒为 0（注记里明明白白写着旧检查点含消息、可读它，代码却因「已有活绑定」把旧键
  判空）。这是本刀撞出的真缺陷，§3 按轮次索引钉住它。
* **同一句话不被重启喂 k 遍**——已读键按 (正文, 到达时间, 领取) 计，换一份领取从零核对是
  「不丢唤醒」；但只看当前 prev 的键，二次重启会把旧领取读过的每条再重投一遍。§4 用跨领取
  凭据（正文+到达时间）钉住这一半。
* **到达时间是源头给的，不是引擎盖的**——重投时要带崩溃前那条的真实到达时刻，否则已读键
  对不上、提示词里的顺序也失真（§5）。
* **done 的外发不重放**（磁盘账本在新进程里重读，done 就是 done），**残留唤醒不复制**
  （本进程箱里已压着的只算携带、不再投一遍），**没有未读就不起空转轮**（不调模型不占槽）。

「重启」在这里的形态是**同一数据根上的第二套装配**：磁盘件（副作用账本 / 已读账本 /
检查点）从盘重读，内存件（注册表 / 槽位池 / 看门狗 / 调度器）全新——编排层的语义恰好只
依赖这一条。回合执行体是记录用的替身（`Recorder`），模型与 IM 通道不外呼；§8 另把同一条
补查跑在 `ticket_worker_adapter.build_dispatcher` 的真回合上。`TONGTIANWORK_HOME` 一节
一根。纯 stdlib，不碰 `engine/server.py` 与 Rust。

用法: python tests/integration/engine_ticket_restart_resume_selftest.py
"""

import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TMP = Path(tempfile.mkdtemp(prefix="tt-ticket-resume-selftest-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home-0")

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
import ticket_restart_resume as trs  # noqa: E402
import ticket_turn_scheduler as tts  # noqa: E402
import ticket_worker_adapter as ta  # noqa: E402
import ticket_worker_runtime as twr  # noqa: E402

#: 崩溃前那批消息的**源头**到达时刻：固定值才能断言「重投带的是原始时刻」
T0 = 1_700_000_000_000
MSG1 = "第一条：把报告改成中文"
MSG2 = "第二条：附上最近三天的数据"
MSG3 = "第三条：别再问授权，直接做"


def read_id(text: str, at_ms: int, assignment_id: str) -> str:
    return tdr.wake_read_id({"text": text, "atMs": at_ms, "assignmentId": assignment_id})


class Recorder:
    """`execute_turn` 的替身：记下每轮看到的提示词与**借到的历史**，按脚本返回回合。

    `messages` 写成「历史 + 本轮提示词」，检查点因此逐轮变长——续跑到底接上了没有，
    看下一轮的 `history` 长度就知道，不靠自述。第一条检查点因此是 **1** 条（首轮历史为空，
    只有本轮那一句提示词），断言里的条数都按这条算术来。"""

    def __init__(self, *, outcome: str = "running", sends_by_round: dict | None = None) -> None:
        self.outcome = outcome
        self.sends_by_round = dict(sends_by_round or {})
        self.calls: list = []
        self._lock = threading.Lock()

    def __call__(self, worker_snapshot: dict, prompt: str, history: list) -> dict:
        with self._lock:
            index = len(self.calls)
            self.calls.append({"prompt": prompt, "history": list(history), "assignmentId": worker_snapshot.get("assignmentId")})
        return {
            "ok": True,
            "messages": list(history) + [{"role": "user", "content": prompt}],
            "sends": list(self.sends_by_round.get(index, [])),
            "jobs": [],
            "outcome": self.outcome,
            "reason": "",
        }

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.calls)

    def last(self) -> dict:
        with self._lock:
            return self.calls[-1] if self.calls else {}


ASSEMBLIES: list = []


class Assembly:
    """一个「进程」：内存件全新，磁盘件落在同一个数据根上。"""

    def __init__(self, *, home: Path | None = None, recorder: Recorder | None = None, agent_slots: int = 3, human_extra: int = 1) -> None:
        if home is not None:
            os.environ["TONGTIANWORK_HOME"] = str(home)
        self.home = Path(os.environ["TONGTIANWORK_HOME"])
        self.label = f"proc#{len(ASSEMBLIES) + 1}"
        self.recorder = recorder if recorder is not None else Recorder()
        self.sent: list = []
        self.registry = twr.TicketWorkerRegistry(agent_slots=agent_slots, human_extra=human_extra)
        self.outbox = tor.TicketOutbox()
        self.ledger = trr.ReadLedger()
        self.watchdog = trr.JobWatchdog()
        self.dispatcher = tdr.TicketDispatcher(
            registry=self.registry,
            outbox=self.outbox,
            ledger=self.ledger,
            watchdog=self.watchdog,
            execute_turn=self.recorder,
            send_dispatch=self._dispatch,
        )
        self.sched = tts.TicketTurnScheduler(self.dispatcher, thread_factory=tts.inline_thread_factory)
        self.resumer = trs.TicketRestartResume(self.sched)
        ASSEMBLIES.append(self)

    def _dispatch(self, record: dict) -> bool:
        self.sent.append(record["payload"]["text"])
        return True

    def restart(self, *, recorder: Recorder | None = None) -> "Assembly":
        """新进程：不换数据根——磁盘件从盘重读，内存件重建。"""
        return Assembly(home=None, recorder=recorder)

    def wake(self, ticket_id: str, text: str, at_ms: int, *, assignment_id: str, origin: str = "human", title: str = "") -> dict:
        return self.registry.start_or_wake(
            ticket_id=ticket_id, title=title, assignment_id=assignment_id, message=text, origin=origin, arrived_at_ms=at_ms
        )

    def inbox(self, ticket_id: str) -> int:
        worker = self.registry.worker(ticket_id)
        return len(worker.inbox) if worker is not None else 0

    def inbox_items(self, ticket_id: str) -> list:
        worker = self.registry.worker(ticket_id)
        return list(worker.inbox) if worker is not None else []

    def last_round(self, ticket_id: str) -> dict:
        items = self.sched.history(ticket_id)
        return items[-1] if items else {}

    def done_count(self) -> int:
        """done 的条数。

        **不能**从 `snapshot()["orderedGroups"]` 数——那里的过滤条件是
        `status in ("pending","running","failed")`，done 的记录被刻意留在保序组外
        （组是给「下一块挡路的是谁」用的）。判 done 只能看 `statusCounts`。"""
        return int(self.outbox.snapshot()["statusCounts"].get("done") or 0)

    def occupied(self) -> dict:
        return dict(self.registry.pool.snapshot()["occupied"])

    @staticmethod
    def candidates(*items: tuple) -> list:
        return [{"text": t, "atMs": a} for t, a in items]


# ── 1. 崩溃前的世界：一轮读了一条，另两条刚到时进程没了 ──────────

rec1 = Recorder(
    outcome="running",
    sends_by_round={0: [{"kind": "reply", "sessionKey": "slack:C7", "payload": "崩溃前已发出的那句", "dedupeKey": "d-crash-1"}]},
)
p1 = Assembly(home=_TMP / "home-1", recorder=rec1)
first = p1.sched.submit(ticket_id="tk", title="补查单", assignment_id="as-old", message=MSG1, origin="human", arrived_at_ms=T0 + 1)
check("崩溃前：投递并起轮（inline 工厂当场跑完这一轮）", first["action"] == "turn-scheduled" and p1.inbox("tk") == 0, repr(first))
check("崩溃前：那条唤醒落账为已读（记在 as-old 名下）", p1.ledger.unread_of("as-old", [read_id(MSG1, T0 + 1, "as-old")]) == [], repr(p1.ledger.snapshot()))
check("崩溃前：投递用的是源头到达时间，不是引擎盖的戳", p1.recorder.last()["prompt"].count(MSG1) == 1 and f"@ {T0 + 1}" in p1.recorder.last()["prompt"], p1.recorder.last()["prompt"][:200])
check("崩溃前：检查点带工单归属落盘（补查核对归属用的就是它）", (cr.load_checkpoint(twr.checkpoint_task_id("as-old")) or {}).get("ticketId") == "tk", repr(cr.load_checkpoint(twr.checkpoint_task_id("as-old"))))
check("崩溃前：外发已 done", p1.sent == ["崩溃前已发出的那句"] and p1.done_count() == 1, repr({"sent": p1.sent, "done": p1.done_count()}))
p1.wake("tk", MSG2, T0 + 2, assignment_id="as-old")
p1.wake("tk", MSG3, T0 + 3, assignment_id="as-old")
check("构造前提：两条新唤醒压在**本进程**箱里（重启就没了）", p1.inbox("tk") == 2 and [w["atMs"] for w in p1.inbox_items("tk")] == [T0 + 2, T0 + 3], repr(p1.inbox_items("tk")))

# ── 2. 新进程的补查计划：谁进过会话，谁没有 ────────────────────

rec2 = Recorder(
    outcome="running",
    sends_by_round={0: [{"kind": "reply", "sessionKey": "slack:C7", "payload": "续跑后新说的那句", "dedupeKey": "d-after-1"}]},
)
p2 = p1.restart(recorder=rec2)
cands = p2.candidates((MSG1, T0 + 1), (MSG2, T0 + 2), (MSG3, T0 + 3))
plan = p2.resumer.plan("tk", prev_assignment_id="as-old", candidate_wakes=cands, title="补查单")
check("新进程的注册表是空的：活领取确实随进程死了（补查的前提）", p2.registry.worker("tk") is None)
check("已读账本从盘重读：as-old 那条落账还在", len(p2.ledger.marks_of("as-old")) == 1, repr(p2.ledger.snapshot()))
check("补查分出「从没进过会话历史」的两条", plan["ok"] and [w["text"] for w in plan["unread"]] == [MSG2, MSG3], repr(plan.get("unread")))
check("进过会话的那一条不再重投", [w["text"] for w in plan["alreadyRead"]] == [MSG1], repr(plan.get("alreadyRead")))
check("计划可续跑，携带条数按未读算", plan["resumable"] is True and plan["carriedWakes"] == 2, repr({"resumable": plan["resumable"], "carried": plan["carriedWakes"]}))
check("证据：旧检查点在、含 1 条消息（首轮历史为空+本轮提示词）、归属本工单", plan["evidence"]["prevCheckpointExists"] and plan["evidence"]["prevCheckpointMessages"] == 1 and plan["evidence"]["prevCheckpointOwner"] == "tk", repr(plan["evidence"]))
check("证据：账本里没有悬空在途外发", plan["evidence"]["inflightOutboxIds"] == [] and plan["evidence"]["queuedOutboxKinds"] == [], repr(plan["evidence"]))
check("崩溃后新进程的看门狗是空的，且如实说这条证据不在本层", plan["evidence"]["crashedJobs"] == [] and any("D-48.5c" in n for n in plan["notes"]), repr(plan["notes"]))
check("核对覆盖得到的落账就不报「旧格式缺口」", plan["evidence"]["unattributedMarks"] == 0 and not any("旧格式" in n for n in plan["notes"]), repr(plan["notes"]))
refused = p2.resumer.plan("tk", prev_assignment_id="", candidate_wakes=cands)
check("缺 prev 直接拒：没有旧领取就无从比对", refused["ok"] is False and refused["action"] == "need-prev" and "无从比对" in refused["reason"], repr(refused))

# ── 3. 重投 + 新领取续跑：一次带走、上下文真借到、done 不重放 ────

res = p2.resumer.resume("tk", prev_assignment_id="as-old", new_assignment_id="as-new", candidate_wakes=cands, title="补查单")
round2 = p2.recorder.last()
check("补起重跑：只投未读的两条，已读那条跳过", res["ok"] and res["action"] == "resumed" and res["redelivered"] == 2 and res["skippedAlreadyRead"] == 1, repr({k: res.get(k) for k in ("action", "redelivered", "skippedAlreadyRead")}))
check("一轮把两条一起消费掉（一次带走，不拆成两轮）", p2.last_round("tk")["wakesConsumed"] == 2 and p2.inbox("tk") == 0, repr(p2.last_round("tk")))
check("未读的两条都进了本轮提示词", MSG2 in round2["prompt"] and MSG3 in round2["prompt"], round2["prompt"][:400])
check("已读那条没有再被喂一遍", round2["prompt"].count(MSG1) == 0, round2["prompt"][:400])
check("恢复注记按携带条数点名（等于真正进 prompt 的条数）", "还带着 2 条" in round2["prompt"], round2["prompt"][:200])
check("上下文真从旧领取的检查点借到（走调度器时曾恒为 0 的真缺陷）", len(round2["history"]) == 1 and MSG1 in str(round2["history"]), repr({"history": round2["history"]})[:400])
check("历史逐轮记着上下文来源，接线方可查", p2.last_round("tk")["historySource"] == "prev-assignment", repr(p2.last_round("tk")))
check("重投保留原始到达时刻（顺序与键都不失真）", f"@ {T0 + 2}" in round2["prompt"] and f"@ {T0 + 3}" in round2["prompt"], round2["prompt"][:400])
check("新落盘用新键，旧检查点原样保留（1 条消息没被新领取覆盖）", cr.load_checkpoint(twr.checkpoint_task_id("as-new")) is not None and len((cr.load_checkpoint(twr.checkpoint_task_id("as-old")) or {}).get("messages") or []) == 1)
check("已读落账记在新领取名下（换领取从零核对）", len(p2.ledger.marks_of("as-new")) == 2, repr(p2.ledger.snapshot()))
check("新落账带工单归属与跨领取凭据", p2.ledger.unattributed_marks() == 0, repr(p2.ledger.snapshot()))
check("重启后 done 的外发没被重放", p2.sent == ["续跑后新说的那句"] and "崩溃前已发出的那句" not in p2.sent, repr(p2.sent))
check("旧那一条在新进程读到的账本里仍是 done", p2.done_count() == 2, repr(p2.outbox.snapshot()["statusCounts"]))
state2 = p2.dispatcher.state_of("tk")
# inline 线程工厂下 resume 返回时这一轮已经跑完：runState 回到 idle、作业为空，
# 对外因此是 waiting（活领取还在、槽没还）而不是 working。真线程下的 working 在
# engine_ticket_turn_scheduler_selftest 里另有覆盖，本文件不假称跑过异步那一趟。
check("补查后仍是活领取：箱里无残留、没有未读悬空、槽没还", state2["external"] == "waiting" and state2["endedStatus"] == "" and state2["queuedWakes"] == 0 and state2["unreadWakes"] == [], repr(state2))
check("投递与起轮之间没有第二份领取（同工单单飞穿过补查）", [r["assignmentId"] for r in p2.sched.history("tk")][-1] == "as-new", repr(p2.sched.history("tk")))

# ── 4. 二次重启：同一批候选不再被喂第二遍（跨领取核对） ─────────

p3 = p2.restart(recorder=Recorder(outcome="running"))
plan3 = p3.resumer.plan("tk", prev_assignment_id="as-new", candidate_wakes=cands, title="补查单")
check("换一份领取来核对时，旧领取读过的不靠 prev 也算喂过", plan3["ok"] and plan3["unread"] == [] and len(plan3["alreadyRead"]) == 3, repr({"unread": plan3.get("unread"), "read": len(plan3.get("alreadyRead", []))}))
check("跨领取认出的那条带 via=cross-claim 留痕", any(w.get("via") == "cross-claim" for w in plan3["alreadyRead"]), repr(plan3["alreadyRead"]))
check("认出跨领取时如实说明，不静默吃掉", any("更早的某一份领取" in n for n in plan3["notes"]), repr(plan3["notes"]))
res3 = p3.resumer.resume("tk", prev_assignment_id="as-new", new_assignment_id="as-third", candidate_wakes=cands, title="补查单")
check("没有未读就不起重合（不调模型、不占槽）", res3["ok"] and res3["action"] == "nothing-to-resume" and p3.recorder.count == 0, repr({"action": res3.get("action"), "calls": p3.recorder.count}))
check("不起空转轮时新领取根本没绑上（槽位没被白占）", p3.registry.worker("tk") is None and p3.occupied() == {}, repr(p3.registry.pool.snapshot()))

# ── 5. 拒绝面：活领取 / 缺新领取 id / prev 给错工单 ─────────────

p4 = Assembly(home=_TMP / "home-2", recorder=Recorder(outcome="running"))
p4.wake("tk-live", MSG1, T0, assignment_id="as-live", origin="human")
live_plan = p4.resumer.plan("tk-live", prev_assignment_id="as-live", candidate_wakes=p4.candidates((MSG1, T0)))
check("本进程已有活领取时补查不插手（再起一份就是两个自己）", live_plan["ok"] is False and live_plan["action"] == "already-live" and "在本进程已有活领取" in live_plan["reason"], repr(live_plan))
missing_new = p4.resumer.resume("tk-live", prev_assignment_id="as-live", new_assignment_id="", candidate_wakes=p4.candidates((MSG1, T0)))
check("缺新领取 id 直接拒：补查是一次新的领取", missing_new["ok"] is False and missing_new["action"] == "need-new-assignment", repr(missing_new))
p5 = Assembly(home=_TMP / "home-3", recorder=Recorder(outcome="running"))
p5.sched.submit(ticket_id="tk-x", title="别人单", assignment_id="as-x", message="另一单的唤醒", origin="agent", arrived_at_ms=T0)
wrong = p5.resumer.plan("tk-y", prev_assignment_id="as-x", candidate_wakes=p5.candidates(("这条不属于那一份领取", T0 + 7)))
check("prev 属于别的工单就被拒（拒绝把别人的上下文接进来）", wrong["ok"] is False and wrong["action"] == "wrong-owner" and "tk-x" in wrong["reason"], repr(wrong))

# ── 6. 槽位满：一条也没投出去，候选仍在调用方那边 ───────────────

p6 = Assembly(home=_TMP / "home-4", recorder=Recorder(outcome="running"), agent_slots=1, human_extra=1)
p6.wake("tk-o1", "占位一", T0, assignment_id="as-o1", origin="human")
p6.wake("tk-o2", "占位二", T0 + 1, assignment_id="as-o2", origin="human")
full_plan = p6.resumer.plan("tk-need", prev_assignment_id="as-gone", candidate_wakes=p6.candidates(("重启后要补的那条", T0 + 2)))
check("槽位满时计划照样成立（补查先看账本，裁决在投递那一刻）", full_plan["ok"] and full_plan["resumable"], repr(full_plan))
full = p6.resumer.resume("tk-need", prev_assignment_id="as-gone", new_assignment_id="as-new6", candidate_wakes=p6.candidates(("重启后要补的那条", T0 + 2)))
check("槽位池拒了就说清「一条也没投出去」", full["ok"] is False and full["action"] == "not-resumed" and full["redelivered"] == 0 and "槽位" in full["reason"], repr(full))
check("被拒的补查不留半截绑定，也不白调模型", p6.registry.worker("tk-need") is None and p6.recorder.count == 0, repr(p6.registry.snapshot()))

# ── 7. 本进程残留：只算携带，不复制成两条 ──────────────────────

p7 = Assembly(home=_TMP / "home-5", recorder=Recorder(outcome="running"))
p7.wake("tk", MSG1, T0, assignment_id="as-p7", origin="human")
p7.registry.end_assignment("tk", "dropped", reason="演练：这份领取作废")
box_plan = p7.resumer.plan("tk", prev_assignment_id="as-p7", candidate_wakes=p7.candidates((MSG1, T0), (MSG2, T0 + 9)), title="残留单")
check("构造前提：dropped 收口不清箱（残留是真的）", p7.inbox("tk") == 1 and p7.registry.worker("tk").ended_status == "dropped")
check("箱里已压着的那条归「不重复投」，不当未读再投一遍", [w["text"] for w in box_plan["alreadyInInbox"]] == [MSG1] and [w["text"] for w in box_plan["unread"]] == [MSG2], repr({"inBox": box_plan["alreadyInInbox"], "unread": box_plan["unread"]}))
check("携带条数 = 箱里的 + 将要重投的", box_plan["carriedWakes"] == 2, repr(box_plan["carriedWakes"]))
box_res = p7.resumer.resume("tk", prev_assignment_id="as-p7", new_assignment_id="as-p8", candidate_wakes=p7.candidates((MSG1, T0), (MSG2, T0 + 9)), title="残留单")
box_prompt = p7.recorder.last()["prompt"]
check("一轮把残留与新投的一起带走", box_res["ok"] and p7.last_round("tk")["wakesConsumed"] == 2 and p7.inbox("tk") == 0, repr({"res": box_res.get("redelivered"), "round": p7.last_round("tk")}))
check("同一条唤醒在提示词里只出现一次（没被复制成两份）", box_prompt.count(MSG1) == 1 and box_prompt.count(MSG2) == 1, box_prompt[:400])

# ── 8. 同一条补查跑在真装配（适配器 + 真回合一轮）上 ────────────

home8 = _TMP / "home-6"
os.environ["TONGTIANWORK_HOME"] = str(home8)
sent8: list = []
seen8: list = []


def tools8() -> dict:
    return {
        "run_command": lambda command: {"output": f"已执行：{command}"},
        "read_file": lambda path, units: {"text": f"{path} 的正文"},
        "list_dir": lambda path, limit: {"items": ["README.md"][:limit]},
        "write_file": lambda path, content: {"reason": f"写入 {path}"},
    }


def make_model(replies: list, sink: list):
    """按调用次序取脚本；每次把请求正文快照进**这一套装配自己的** sink（历史借没借到看的就是它）。"""
    state = {"n": 0}

    def model_call(request: dict) -> dict:
        blob = "\n".join(str(m.get("content") or "") for m in (request.get("messages") or [])) + "\n" + str(request.get("system") or "")
        sink.append(blob)
        reply = replies[min(state["n"], len(replies) - 1)]
        state["n"] += 1
        return dict(reply)

    return model_call


def send_fn8(channel: str, target: str, text: str) -> bool:
    sent8.append((channel, target, text))
    return True


parts8 = ta.build_dispatcher(
    model_call=make_model(
        [
            {"text": "先回一句", "calls": [{"tool": "send_to_session", "args": {"kind": "reply", "sessionKey": "slack:C8", "text": "崩溃前发的那句", "dedupeKey": "d8-1"}}, {"tool": "finish_work", "args": {"summary": "第一轮收口"}}]},
        ],
        seen8,
    ),
    send_fn=send_fn8,
    goal="把这条工单做完并如实收口",
    **tools8(),
)
sched8 = tts.TicketTurnScheduler(parts8["dispatcher"], thread_factory=tts.inline_thread_factory)
resumer8 = trs.TicketRestartResume(sched8)
sched8.submit(ticket_id="tk8", title="真装配", assignment_id="as8-old", message="真装配的第一条", origin="human", arrived_at_ms=T0)
check("真装配前提：崩溃前那条已发到通道并收口", sent8 == [("slack", "C8", "崩溃前发的那句")] and parts8["registry"].worker("tk8").ended_status == "finished", repr({"sent": sent8, "ended": parts8["registry"].worker("tk8").ended_status}))
# 新进程：同一数据根、全新装配（含真 execute_turn 的模型往返循环）
sent9: list = []
seen9: list = []
parts9 = ta.build_dispatcher(
    model_call=make_model(
        [
            {"text": "接着做", "calls": [{"tool": "send_to_session", "args": {"kind": "reply", "sessionKey": "slack:C8", "text": "续跑后发的那句", "dedupeKey": "d9-1"}}, {"tool": "finish_work", "args": {"summary": "真装配续跑收口"}}]},
        ],
        seen9,
    ),
    send_fn=lambda channel, target, text: sent9.append((channel, target, text)) or True,
    goal="把这条工单做完并如实收口",
    **tools8(),
)
sched9 = tts.TicketTurnScheduler(parts9["dispatcher"], thread_factory=tts.inline_thread_factory)
resumer9 = trs.TicketRestartResume(sched9)
# 旧领取是 finished 收口（检查点已按口径删除）——补查据此如实报「上下文借不到」
plan9 = resumer9.plan("tk8", prev_assignment_id="as8-old", candidate_wakes=[{"text": "崩溃前刚到没读完的那条", "atMs": T0 + 5}], title="真装配")
check("真装配补查：一条未读候选被认出来", plan9["ok"] and [w["text"] for w in plan9["unread"]] == ["崩溃前刚到没读完的那条"], repr(plan9.get("unread")))
check("finished 删过的检查点如实报缺失（不假称借到上下文）", plan9["evidence"]["prevCheckpointExists"] is False and any("检查点" in n and "借不到" in n for n in plan9["notes"]), repr(plan9["notes"]))
run9 = resumer9.resume("tk8", prev_assignment_id="as8-old", new_assignment_id="as9-new", candidate_wakes=[{"text": "崩溃前刚到没读完的那条", "atMs": T0 + 5}], title="真装配")
check("真装配续跑起来：未读那条由真回合消费", run9["ok"] and sched9.history("tk8")[-1]["wakesConsumed"] == 1, repr({"run": run9.get("redelivered"), "round": sched9.history("tk8")[-1]}))
check("真回合的模型请求里看得见这条唤醒", "崩溃前刚到没读完的那条" in seen9[0], seen9[0][:300])
check("真装配下旧外发没被重放（通道只收到新一轮那句）", sent9 == [("slack", "C8", "续跑后发的那句")] and "崩溃前发的那句" not in [t for _c, _s, t in sent9], repr(sent9))
check("新一轮的收口与槽位归还走的是同一条真路径", parts9["registry"].worker("tk8").ended_status == "finished" and parts9["registry"].pool.snapshot()["occupied"] == {}, repr(parts9["registry"].pool.snapshot()))

# ── 9. 没有可比对凭据时的诚实边界 ──────────────────────────────

p8 = Assembly(home=_TMP / "home-7", recorder=Recorder(outcome="running"))
bare = p8.resumer.plan("tk-bare", prev_assignment_id="as-bare", candidate_wakes=["只有正文没有到达时间的一条", {"text": "  ", "atMs": T0}])
check("候选可以是纯字符串（缺到达时间就按未读处理并说明）", bare["ok"] and len(bare["unread"]) == 1 and any("没有到达时间" in n for n in bare["notes"]), repr(bare["notes"]))
check("空正文的候选不参与补查（也不起空转投递）", any("正文为空" in n for n in bare["notes"]) and bare["unread"][0]["hasArrival"] is False, repr(bare["unread"]))
empty_only = p8.resumer.plan("tk-none", prev_assignment_id="as-none", candidate_wakes=[])
check("没有候选就不起重合", empty_only["ok"] and empty_only["resumable"] is False and empty_only["action"] == "nothing-to-resume", repr(empty_only))
p9 = Assembly(home=_TMP / "home-8", recorder=Recorder(outcome="running"))
# 旧格式落账要写进**这一套装配自己的** ledger：`ReadLedger` 在构造时把盘读进内存、此后
# 只信自己那份，另建一个实例落账，p9.dispatcher 永远看不见（还会被它下一次 _save 覆盖掉）。
p9.ledger.mark_read("as-legacy", read_id("旧格式落账的一条", T0, "as-legacy"))
check("旧格式落账（没有跨领取凭据）被如实计数", p9.ledger.unattributed_marks() == 1, repr(p9.ledger.snapshot()))
legacy_plan = p9.resumer.plan("tk-legacy", prev_assignment_id="as-legacy", candidate_wakes=p9.candidates(("旧格式落账的一条", T0)))
check("旧格式落账仍能挡住同一次领取内的重投", legacy_plan["ok"] and legacy_plan["resumable"] is False, repr({"unread": legacy_plan["unread"], "read": len(legacy_plan["alreadyRead"])}))
check("同时点名核对缺口：换领取的那一半覆盖不到", any("旧格式" in n for n in legacy_plan["notes"]), repr(legacy_plan["notes"]))

# ── 收尾 ────────────────────────────────────────────────────────

shutil.rmtree(_TMP, ignore_errors=True)

if FAILURES:
    print(f"\nFAILURES ({len(FAILURES)}):")
    for name in FAILURES:
        print(" -", name)
    sys.exit(1)
print("\nOK: 重启补查编排自测全部检查通过（未读才重投、读过的不重喂、上下文真借到、done 不重放、不起空转轮）")
