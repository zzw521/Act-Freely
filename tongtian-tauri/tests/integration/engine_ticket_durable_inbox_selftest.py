#!/usr/bin/env python3
"""
工单唤醒的耐久收件箱（缺陷 D-48.6）自测

D-48.5g 让引擎在重启后能说清「哪几条唤醒从没进过会话历史」，但那份核对**整个站在调用方
给的候选上**：服务端消息表说有哪些，引擎才知道有哪些。根因很实在——唤醒投进 `worker.inbox`
之后只活在内存里，进程一没，「引擎收到过但没喂进会话」这批事实连到达顺序一起蒸发。本刀把
这批落到盘上，钉住这些口径（每条都问过「什么缺陷会让它红」）：

* **投递即落盘、消费才销账**：`start_or_wake` 的三条投递路径（新领取 / 已有活绑定 / 并发首触）
  都在磁盘上留一条；喂进本轮提示词并落账为已读之后才销。回合没起就崩 → 那条一直算未消费。
* **落盘失败不拦投递**：唤醒已进内存箱，此刻拒投等于把用户的话扣下来；原话进
  `journalWarning`，投递照样成功。
* **它是第二来源，不是替代**：引擎**停机期间**到达的唤醒磁盘上没有痕迹，调用方候选不能撤。
  §6 用同数据根的第二套装配证明「一条候选都不给，引擎也知道自己收过哪两条」。
* **两边同一把键**：投递键与已读键是同一个摘要换前缀，销账按 (领取键, 到达键) 两道一起清——
  只按前者会让补查重投后旧领取那条永远留在箱里（耐久日志自己变成不丢的垃圾）。
* **读过的不重投、也不留垃圾**：账本证明早进过会话、盘上还挂着的（销账那次没落盘），
  `plan` 点名、`resume` 清理；`plan` 自身不动盘。
* **容量与坏文件如实说**：到上限拒收并写明「这条没有落盘」；坏账本改名 `.corrupt-*` 让路，
  不静默删、不在坏文件上追加事实。
* **口径不动**：槽位池、人用预留槽、单飞、外发事务一律不变——收件箱不是第二个队列，
  §9 用「外发账本仍是 0 条」钉住这条边界。

`TONGTIANWORK_HOME` 一节一根（收件箱是**跨工单共享的单文件账本**，共根会让别的用例的条数
掺进本节判定）。真子进程崩溃的证据在 D-48.5c 的演练里，本文件用「同一数据根上的第二套装配」
代表重启：磁盘件从盘重读，内存件全新。纯 stdlib，不碰 `engine/server.py` 与 Rust。

用法: python tests/integration/engine_ticket_durable_inbox_selftest.py
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

_TMP = Path(tempfile.mkdtemp(prefix="tt-ticket-inbox-selftest-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home-0")

sys.path.insert(0, str(PROJECT_ROOT / "engine"))

FAILURES: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


import checkpoint_runtime as cr  # noqa: E402
import data_paths as dp  # noqa: E402
import ticket_dispatch_runtime as tdr  # noqa: E402
import ticket_inbox_runtime as tir  # noqa: E402
import ticket_outbox_runtime as tor  # noqa: E402
import ticket_recovery_runtime as trr  # noqa: E402
import ticket_restart_resume as trs  # noqa: E402
import ticket_turn_scheduler as tts  # noqa: E402
import ticket_worker_adapter as ta  # noqa: E402
import ticket_worker_runtime as twr  # noqa: E402

#: 崩溃前那批消息的**源头**到达时刻：固定值才能断言「重投带的是原始时刻」
T0 = 1_700_000_000_000
M_A = "第一条：把周报整理成中文"
M_B = "第二条：附上本周的数据"
M_C = "第三条：别再问我要授权"


def read_id(text: str, at_ms: int, assignment_id: str) -> str:
    return tdr.wake_read_id({"text": text, "atMs": at_ms, "assignmentId": assignment_id})


def wake(text: str, at_ms: int, assignment_id: str) -> dict:
    return {"text": text, "atMs": at_ms, "assignmentId": assignment_id}


class Recorder:
    """`execute_turn` 的替身：记下每轮看到的提示词，按脚本回一个回合。"""

    def __init__(self, outcome: str = "running") -> None:
        self.outcome = outcome
        self.calls: list = []

    def __call__(self, worker_snapshot: dict, prompt: str, history: list) -> dict:
        self.calls.append({"prompt": prompt, "history": list(history), "assignmentId": worker_snapshot.get("assignmentId")})
        return {
            "ok": True,
            "messages": list(history) + [{"role": "user", "content": prompt}],
            "sends": [],
            "jobs": [],
            "outcome": self.outcome,
            "reason": "",
        }

    @property
    def count(self) -> int:
        return len(self.calls)

    def last(self) -> dict:
        return self.calls[-1] if self.calls else {}


class Proc:
    """一个「进程」：内存件全新，磁盘件落在指定数据根上；耐久收件箱默认接上。"""

    def __init__(self, *, home: Path | None = None, journal: object = "default", recorder: Recorder | None = None, agent_slots: int = 3, human_extra: int = 1) -> None:
        if home is not None:
            os.environ["TONGTIANWORK_HOME"] = str(home)
        self.home = Path(os.environ["TONGTIANWORK_HOME"])
        self.recorder = recorder if recorder is not None else Recorder()
        if journal == "default":
            journal_obj = tir.TicketInbox()
        elif journal is None:
            journal_obj = None
        else:
            journal_obj = journal
        # registry 与 dispatcher 必须拿到**同一个对象**：两份对象读的是同一个文件，
        # 写的时候却互相看不到对方内存里的记录——那等于把耐久日志退化成会丢的缓存。
        self.inbox = journal_obj
        self.registry = twr.TicketWorkerRegistry(agent_slots=agent_slots, human_extra=human_extra, inbox_journal=journal_obj)
        self.outbox = tor.TicketOutbox()
        self.ledger = trr.ReadLedger()
        self.watchdog = trr.JobWatchdog()
        self.dispatcher = tdr.TicketDispatcher(
            registry=self.registry,
            outbox=self.outbox,
            ledger=self.ledger,
            watchdog=self.watchdog,
            execute_turn=self.recorder,
            send_dispatch=lambda record: True,
            inbox_journal=journal_obj,
        )
        self.sched = tts.TicketTurnScheduler(self.dispatcher, thread_factory=tts.inline_thread_factory)
        self.resumer = trs.TicketRestartResume(self.sched)

    def wake(self, ticket_id: str, text: str, at_ms: int, *, assignment_id: str, origin: str = "human") -> dict:
        return self.registry.start_or_wake(
            ticket_id=ticket_id, title="", assignment_id=assignment_id, message=text, origin=origin, arrived_at_ms=at_ms
        )

    def pending(self, ticket_id: str) -> list:
        return self.inbox.pending_for_ticket(ticket_id) if self.inbox is not None else []

    def pending_of(self, assignment_id: str) -> list:
        return self.inbox.pending_of_assignment(assignment_id) if self.inbox is not None else []

    def memory_inbox(self, ticket_id: str) -> list:
        worker = self.registry.worker(ticket_id)
        return list(worker.inbox) if worker is not None else []


# ── 1. 路径与文件名走 D-43 单一来源 ────────────────────────────

home1 = _TMP / "home-1"
os.environ["TONGTIANWORK_HOME"] = str(home1)
check("耐久收件箱落在引擎数据根里（不新建第二套目录规则）", dp.ticket_inbox_file() == home1 / "ticket_inbox.json", str(dp.ticket_inbox_file()))
check("文件名有常量也有默认值两处同步", dp.TICKET_INBOX_FILENAME == dp.ticket_inbox_file().name, dp.TICKET_INBOX_FILENAME)

# ── 2. 两边的键必须是同一个摘要换前缀 ──────────────────────────

home2 = _TMP / "home-2"
os.environ["TONGTIANWORK_HOME"] = str(home2)
w2 = wake(M_A, T0, "as-key")
check("投递键与已读键同摘要（只差前缀）", tir.wake_journal_id(w2)[3:] == read_id(M_A, T0, "as-key")[5:], repr({"journal": tir.wake_journal_id(w2), "read": read_id(M_A, T0, "as-key")}))
check("到达键与跨领取凭据同摘要", tir.arrival_journal_key(w2)[6:] == tdr.wake_arrival_key(w2)[4:], repr({"journal": tir.arrival_journal_key(w2), "arrival": tdr.wake_arrival_key(w2)}))
box2 = tir.TicketInbox()
app2 = box2.append(ticket_id="tk-key", assignment_id="as-key", text=M_A, origin="human", at_ms=T0)
check("落盘记录能直接换成已读键（补查靠这条认已读）", tir.read_id_of_journal_record(app2["record"]) == read_id(M_A, T0, "as-key"), repr(app2["record"]))
check("非投递键的记录换不出已读键（不给假身份）", tir.read_id_of_journal_record({"wakeId": "wake-abc"}) == "" and tir.read_id_of_journal_record({}) == "")
check("字段缺失时两边仍算同一把键（同一字典喂两边，不许一边补 0）", tir.wake_journal_id({"text": "x", "assignmentId": "a"})[3:] == tdr.wake_read_id({"text": "x", "assignmentId": "a"})[5:], repr({"journal": tir.wake_journal_id({"text": "x", "assignmentId": "a"}), "read": tdr.wake_read_id({"text": "x", "assignmentId": "a"})}))
check("到达时间取到 0 也同键（真实投递永远带时刻，缺省不能分道）", tir.wake_journal_id(wake("x", 0, "a"))[3:] == read_id("x", 0, "a")[5:] and tir.arrival_journal_key(wake("x", 0, "a"))[6:] == tdr.wake_arrival_key(wake("x", 0, "a"))[4:], repr({"j0": tir.wake_journal_id(wake("x", 0, "a")), "r0": read_id("x", 0, "a")}))

# ── 3. 收件箱自己的语义 ────────────────────────────────────────

home3 = _TMP / "home-3"
os.environ["TONGTIANWORK_HOME"] = str(home3)
box3 = tir.TicketInbox()
no_owner = box3.append(ticket_id="", assignment_id="as-1", text="没有工单归属")
check("缺归属拒收：没有归属的投递没法销账", no_owner["ok"] is False and "都必填" in no_owner["reason"], repr(no_owner))
empty = box3.append(ticket_id="tk", assignment_id="as-1", text="   ", at_ms=T0)
check("空正文拒收：空消息不进耐久日志", empty["ok"] is False and "正文为空" in empty["reason"], repr(empty))
first3 = box3.append(ticket_id="tk", assignment_id="as-1", text=M_A, at_ms=T0)
check("一条投递落盘：文件出现且快照记着它", first3["ok"] and dp.ticket_inbox_file().exists() and box3.snapshot()["records"] == 1, repr(box3.snapshot()))
again3 = box3.append(ticket_id="tk", assignment_id="as-1", text=M_A, at_ms=T0)
check("同一条重投幂等（正文+到达时间+领取）：只留一条", again3["ok"] and again3["deduped"] is True and box3.snapshot()["records"] == 1, repr(again3))
other3 = box3.append(ticket_id="tk", assignment_id="as-2", text=M_A, at_ms=T0)
check("换一份领取再投是另一条：各留一条（与已读键同口径）", other3["ok"] and not other3["deduped"] and box3.snapshot()["records"] == 2, repr(other3))
box3.append(ticket_id="tk", assignment_id="as-1", text=M_B, at_ms=T0 - 500)
order = [r["text"] for r in box3.pending_for_ticket("tk")]
check("补查读到的顺序按到达时间升序", order == [M_B, M_A, M_A] and len(box3.pending_of_assignment("as-1")) == 2, repr(order))
check("别的工单掺不进来", box3.pending_for_ticket("tk-other") == [] and box3.snapshot()["byTicket"] == {"tk": 3}, repr(box3.snapshot()["byTicket"]))
gone = box3.mark_consumed([tir.wake_journal_id(wake(M_B, T0 - 500, "as-1"))])
check("按投递键销账：一条走、missing 为空", gone["ok"] and gone["removed"] == 1 and gone["missing"] == [], repr(gone))
ghost = box3.mark_consumed(["wj-不存在的一条"])
check("找不到就点名，不静默也不补记", ghost["ok"] and ghost["removed"] == 0 and ghost["missing"] == ["wj-不存在的一条"], repr(ghost))
by_arrival = box3.mark_consumed(["wj-本轮的新键"], [tir.arrival_journal_key(wake(M_A, T0, "as-1"))])
check("到达键一起清：补查重投后旧领取那条不会烂在箱里", by_arrival["removed"] == 2 and box3.pending_of_assignment("as-1") == [], repr({"removed": by_arrival["removed"], "left": box3.pending_of_assignment("as-1"), "missing": by_arrival["missing"]}))
long_text = "长日志粘贴" * 6000
big = box3.append(ticket_id="tk", assignment_id="as-9", text=long_text, at_ms=T0 + 1)
check("超长正文截断留痕但整条不丢", big["ok"] and "已截断" in big.get("note", "") and big["record"]["text"].endswith("…（已截断）"), repr({"note": big.get("note"), "tail": big["record"]["text"][-12:]}))
check("截断不改键：键按原文算，否则永远销不掉", big["wakeId"] == tir.wake_journal_id(wake(long_text, T0 + 1, "as-9")), repr({"wakeId": big["wakeId"], "期望": tir.wake_journal_id(wake(long_text, T0 + 1, "as-9"))}))
check("已读键也按原文换出来（补查才认得出这条读过）", tir.read_id_of_journal_record(big["record"]) == read_id(long_text, T0 + 1, "as-9"))
check("截断过的记录按原文算出的投递键照样销得掉", box3.mark_consumed([big["wakeId"]])["removed"] == 1, repr(box3.snapshot()))

# 容量：到上限拒收，并如实说这条没落盘（先摆满两条，让上限真的顶到）
box3.append(ticket_id="tk", assignment_id="as-old1", text=M_A, at_ms=T0 - 900)
box3.append(ticket_id="tk", assignment_id="as-old2", text=M_B, at_ms=T0 - 800)
_saved_max = tir.MAX_RECORDS
try:
    tir.MAX_RECORDS = 2
    capped = box3.append(ticket_id="tk", assignment_id="as-cap", text=M_C, at_ms=T0 + 2)
    check("容量到顶：拒收且写明「这条没有落盘」", capped["ok"] is False and "没有落盘" in capped["reason"] and "只在内存箱里" in capped["reason"], repr(capped))
    check("拒收不改变已有记录（不挤掉最旧那条唤醒）", box3.snapshot()["records"] == 2 and [r["text"] for r in box3.pending_for_ticket("tk")] == [M_A, M_B], repr(box3.snapshot()))
    check("被容量挡下的那条补查读不到", box3.pending_of_assignment("as-cap") == [], repr(box3.pending_of_assignment("as-cap")))
finally:
    tir.MAX_RECORDS = _saved_max
after_cap = box3.append(ticket_id="tk", assignment_id="as-cap", text=M_C, at_ms=T0 + 2)
check("腾出容量后同一条能落盘（拒收是容量问题不是键问题）", after_cap["ok"] is True and after_cap["deduped"] is False and box3.snapshot()["records"] == 3, repr(after_cap))

# 落盘失败：把日志的父目录指到一个**普通文件**上，mkdir/write 必然失败（不靠 monkeypatch）
home4 = _TMP / "home-4"
home4.mkdir(parents=True, exist_ok=True)
a_file = home4 / "not-a-dir"
a_file.write_text("我占着这个路径", encoding="utf-8")
box4 = tir.TicketInbox(path=a_file / "ticket_inbox.json")
fail4 = box4.append(ticket_id="tk", assignment_id="as-4", text=M_A, at_ms=T0)
check("落盘失败不假装成功：ok=False 且写明只在内存里", fail4["ok"] is False and fail4.get("memoryOnly") is True and "落盘失败" in fail4["reason"], repr(fail4))
check("落盘失败的那条不留在内存里（没有耐久副本就不许装成有）", box4.pending_for_ticket("tk") == [] and box4.snapshot()["records"] == 0, repr(box4.snapshot()))
fail4b = box4.mark_consumed(["wj-随便一把键"])
check("销账没落盘要说清后果：下次重启会重投一次", fail4b["ok"] is False and "重投一次" in fail4b["reason"], repr(fail4b))

# 坏文件：改名让路，不静默删，也不在坏文件上追加事实
home5 = _TMP / "home-5"
home5.mkdir(parents=True, exist_ok=True)
bad_file = home5 / "ticket_inbox.json"
bad_file.write_text("{这不是 JSON", encoding="utf-8")
box5 = tir.TicketInbox(path=bad_file)
check("坏账本改名保留（不静默删用户的证据）", "耐久收件箱损坏" in box5.load_error and list(home5.glob("ticket_inbox.json.corrupt-*")), repr(box5.load_error))
check("从空日志起步并暴露 loadError（不假装读到了东西）", box5.snapshot()["records"] == 0 and box5.snapshot()["loadError"] == box5.load_error, repr(box5.snapshot()))
ok5 = box5.append(ticket_id="tk", assignment_id="as-5", text=M_A, at_ms=T0)
check("让路之后能正常落盘", ok5["ok"] and bad_file.exists() and box5.snapshot()["records"] == 1, repr(box5.snapshot()))

# ── 4. 投递即落盘：三条投递路径 ────────────────────────────────

home6 = _TMP / "home-6"
p6 = Proc(home=home6)
r_started = p6.wake("tk6", M_A, T0 + 1, assignment_id="as6-old")
check("新领取的投递落盘：盘上出现这一条", r_started["ok"] and [r["text"] for r in p6.pending("tk6")] == [M_A], repr({"result": {k: r_started.get(k) for k in ("ok", "delivered")}, "pending": p6.pending("tk6")}))
check("落盘成功时不带 journalWarning（有就代表没落盘）", "journalWarning" not in r_started, repr(r_started))
r_wake = p6.wake("tk6", M_B, T0 + 2, assignment_id="as6-old")
check("已有活绑定的投递也落盘（不只是新领取那条路径）", r_wake["delivered"] == "existing-worker" and [r["text"] for r in p6.pending("tk6")] == [M_A, M_B], repr(p6.pending("tk6")))
check("投递记录带归属与到达时间（补查要按源头时间重投）", [(r["assignmentId"], r["atMs"]) for r in p6.pending("tk6")] == [("as6-old", T0 + 1), ("as6-old", T0 + 2)], repr(p6.pending("tk6")))

# 并发首触：B 已过「没有活绑定」的第一道检查、A 在这中间把领取挂上，B 才会走 racer 分支。
# 这一路径在磁盘上留不留痕是独立的缺陷面（少一句 _journal_append 就只有这条不落盘），
# 所以要把那个极窄窗口拉开：钩住 B 的槽位申请，等 A 绑定完成再让它进第二把锁。
home7 = _TMP / "home-7"
p7 = Proc(home=home7)
b_past_first_check = threading.Event()
a_bound = threading.Event()
release = threading.Event()
_orig_acquire = p7.registry.pool.try_acquire


def acquire_hook(key: str, origin: str):
    out = _orig_acquire(key, origin)
    if threading.current_thread().name == "B":
        b_past_first_check.set()
        a_bound.wait(5)
    return out


p7.registry.pool.try_acquire = acquire_hook
racer_result: dict = {}


def thread_b_body() -> None:
    racer_result["r"] = p7.registry.start_or_wake(
        ticket_id="tk7", title="", assignment_id="as7-other", message=M_B, origin="human", arrived_at_ms=T0 + 2
    )


thread_b = threading.Thread(target=thread_b_body, name="B")
thread_b.start()
check("构造前提：B 已过第一道检查、此刻确实没有活绑定", b_past_first_check.wait(5) and p7.registry.worker("tk7") is None, repr(p7.registry.worker("tk7")))


def slow_prepare(path):
    a_bound.set()
    release.wait(5)


thread_a = threading.Thread(
    target=lambda: p7.registry.start_or_wake(
        ticket_id="tk7", title="", assignment_id="as7-old", message=M_A, origin="human", arrived_at_ms=T0 + 1, prepare_fn=slow_prepare
    ),
    name="A",
)
thread_a.start()
check("构造前提：A 已绑定领取、正卡在目录准备里", a_bound.wait(5) and p7.registry.worker("tk7").assignment_id == "as7-old", repr(p7.registry.worker("tk7")))
thread_b.join(5)
release.set()
thread_a.join(5)
p7.registry.pool.try_acquire = _orig_acquire
r_racer = racer_result.get("r") or {}
check("并发首触那条也落盘（三条投递路径一个不漏）", r_racer.get("delivered") == "existing-worker" and "并发首触" in r_racer.get("note", "") and [r["text"] for r in p7.pending("tk7")] == [M_A, M_B], repr({"result": r_racer, "pending": p7.pending("tk7")}))
check("并发首触没有起第二份领取，记录挂在那份真领取名下", p7.registry.worker("tk7").assignment_id == "as7-old" and p7.pending_of("as7-other") == [] and [r["assignmentId"] for r in p7.pending("tk7")] == ["as7-old", "as7-old"], repr(p7.pending("tk7")))

# 槽位被拒的投递不落盘：什么都没投出去，盘上就不该有条
home8 = _TMP / "home-8"
p8 = Proc(home=home8, agent_slots=1, human_extra=1)
p8.wake("tk8a", M_A, T0 + 1, assignment_id="as8-a", origin="agent")
denied = p8.wake("tk8b", M_B, T0 + 2, assignment_id="as8-b", origin="agent")
check("槽位满被 Agent 唤醒不占槽（前提口径仍在）", denied["ok"] is False and "槽位" in denied["reason"], repr(denied))
check("被拒的唤醒没有落盘：盘上只有一条", len(p8.pending("tk8b")) == 0 and len(p8.pending("tk8a")) == 1, repr({"tk8b": p8.pending("tk8b"), "tk8a": [r["text"] for r in p8.pending("tk8a")]}))
human_ok = p8.wake("tk8b", M_C, T0 + 3, assignment_id="as8-c", origin="human")
check("人用预留槽仍接得住（验收项②没被耐久层改动），这条落盘", human_ok["ok"] and [r["text"] for r in p8.pending("tk8b")] == [M_C], repr(p8.pending("tk8b")))

# ── 5. 落盘失败不拦投递 ────────────────────────────────────────

home9 = _TMP / "home-9"


class RefusingJournal:
    """投递与销账都「落不了盘」的假箱：按契约返回 ok=False，不抛。"""

    def __init__(self) -> None:
        self.seen: list = []
        self.consumed: list = []

    def append(self, **kwargs) -> dict:
        self.seen.append(kwargs)
        return {"ok": False, "reason": "耐久收件箱到上限 5000 条"}

    def mark_consumed(self, wake_ids: list[str], arrival_keys: list[str] | None = None) -> dict:
        self.consumed.append(list(wake_ids))
        return {"ok": False, "removed": 0, "missing": list(wake_ids), "reason": "销账没落盘：磁盘只读"}

    def pending_for_ticket(self, ticket_id: str) -> list:
        return []


class ExplodingJournal(RefusingJournal):
    def append(self, **kwargs) -> dict:
        raise RuntimeError("磁盘被拔了")


p9 = Proc(home=home9, journal=RefusingJournal())
r9 = p9.wake("tk9", M_A, T0 + 1, assignment_id="as9")
check("落盘失败时投递照样成功（不把用户的话扣下来）", r9["ok"] and len(p9.memory_inbox("tk9")) == 1, repr(r9))
check("失败原话进 journalWarning（不是静默降级成内存件）", "journalWarning" in r9 and "耐久收件箱没落盘" in r9["journalWarning"] and "到上限 5000 条" in r9["journalWarning"], repr(r9))
p9b = Proc(home=_TMP / "home-9b", journal=ExplodingJournal())
r9b = p9b.wake("tk9b", M_A, T0 + 1, assignment_id="as9b")
check("收件箱写炸了也不拖垮投递：异常类型与原因都带出来", r9b["ok"] is True and "耐久收件箱写入异常" in r9b.get("journalWarning", "") and "磁盘被拔了" in r9b["journalWarning"], repr(r9b))
r9c = p9b.sched.submit(ticket_id="tk9b", title="", assignment_id="as9b", message=M_B, origin="human", arrived_at_ms=T0 + 2)
check("起轮路径也照样推进（耐久层不改成败）", r9c["action"] == "turn-scheduled" and p9b.recorder.count == 1, repr(r9c))
stats9b = (cr.load_checkpoint(twr.checkpoint_task_id("as9b")) or {}).get("stats") or {}
check("销账拿的就是本轮那批投递键（对不上只报数，不拦回合）", p9b.inbox.consumed == [[tir.wake_journal_id(wake(M_A, T0 + 1, "as9b")), tir.wake_journal_id(wake(M_B, T0 + 2, "as9b"))]] and stats9b.get("journalMissing") == 2, repr({"consumed": p9b.inbox.consumed, "stats": stats9b}))
p9c = Proc(home=_TMP / "home-9c", journal=None)
r9d = p9c.wake("tk9c", M_A, T0, assignment_id="as9c")
check("没接耐久箱就是旧行为：投递成功、不带 journalWarning、也不凭空写出一个收件箱文件", r9d["ok"] and "journalWarning" not in r9d and not (p9c.home / "ticket_inbox.json").exists(), repr(r9d))

# ── 6. 消费才销账：喂进会话并落账为已读之后 ────────────────────

home10 = _TMP / "home-10"
p10 = Proc(home=home10, recorder=Recorder(outcome="running"))
p10.wake("tk10", M_A, T0 + 1, assignment_id="as10")
p10.wake("tk10", M_B, T0 + 2, assignment_id="as10")
check("前提：两条都压在箱里（内存 2、磁盘 2）", len(p10.memory_inbox("tk10")) == 2 and len(p10.pending("tk10")) == 2, repr({"memory": len(p10.memory_inbox("tk10")), "disk": len(p10.pending("tk10"))}))
state_before = p10.dispatcher.state_of("tk10")
check("查询接口点名磁盘条数（引擎自己知道收到过什么）", state_before["journalPending"] == 2 and state_before["queuedWakes"] == 2, repr(state_before))
run10 = p10.dispatcher.wake_and_run(ticket_id="tk10", deliver=False)
check("起轮把两条一起喂进提示词", run10["ok"] and M_A in p10.recorder.last()["prompt"] and M_B in p10.recorder.last()["prompt"], repr(run10)[:300])
check("喂进会话并落账为已读之后才销账：磁盘归零", len(p10.pending("tk10")) == 0 and p10.ledger.unread_of("as10", [read_id(M_A, T0 + 1, "as10"), read_id(M_B, T0 + 2, "as10")]) == [], repr({"pending": p10.pending("tk10")}))
stats10 = (cr.load_checkpoint(twr.checkpoint_task_id("as10")) or {}).get("stats") or {}
check("每轮统计里看得见销账条数（清了几条、有没有对不上）", stats10.get("journalCleared") == 2 and stats10.get("journalMissing") == 0, repr(stats10))
check("两条都销得掉时不播「找不到」那条注记", not any("找不到对应投递" in n for n in run10.get("notes") or []), repr(run10.get("notes")))

# 崩在喂进会话之前：一条都没销，磁盘上仍算未消费
home11 = _TMP / "home-11"
p11 = Proc(home=home11)
p11.wake("tk11", M_B, T0 + 2, assignment_id="as11")
check("回合没起就崩：这条在盘上仍算「投过但没进会话」", len(p11.pending("tk11")) == 1 and p11.ledger.marks_of("as11") == [], repr({"pending": p11.pending("tk11"), "marks": p11.ledger.marks_of("as11")}))

# ── 7. 重启后的第二来源：一条候选都不给也认得回 ────────────────

home12 = _TMP / "home-12"
os.environ["TONGTIANWORK_HOME"] = str(home12)
parts12 = ta.build_dispatcher(
    model_call=lambda request: {"text": "崩溃前那一轮", "calls": [{"tool": "finish_work", "args": {"summary": "收口"}}]},
    send_fn=lambda channel, target, text: True,
    goal="把这条工单做完",
    run_command=lambda command: {"output": f"已执行：{command}"},
    read_file=lambda path, units, start=0: {"text": f"{path} 正文"},
    list_dir=lambda path, limit: {"items": []},
    write_file=lambda path, content: {"reason": f"写入 {path}"},
)
box12 = parts12["inboxJournal"]
sched12 = tts.TicketTurnScheduler(parts12["dispatcher"], thread_factory=tts.inline_thread_factory)
resumer12 = trs.TicketRestartResume(sched12)
sched12.submit(ticket_id="tk12", title="耐久单", assignment_id="as12-old", message=M_A, origin="human", arrived_at_ms=T0 + 1)
check("真装配前提：第一条已被本轮消费（盘上不留它）", box12.pending_for_ticket("tk12") == [] and parts12["registry"].worker("tk12").ended_status == "finished", repr(box12.pending_for_ticket("tk12")))
parts12["registry"].start_or_wake(ticket_id="tk12", title="耐久单", assignment_id="as12-old", message=M_B, origin="human", arrived_at_ms=T0 + 2)
parts12["registry"].start_or_wake(ticket_id="tk12", title="耐久单", assignment_id="as12-old", message=M_C, origin="human", arrived_at_ms=T0 + 3)
check("崩溃前那两条压在内存箱：磁盘上各有对应记录", [r["text"] for r in box12.pending_for_ticket("tk12")] == [M_B, M_C], repr(box12.pending_for_ticket("tk12")))
refused12 = resumer12.resume("tk12", prev_assignment_id="as12-old", new_assignment_id="as12-else", candidate_wakes=[], title="耐久单")
check("活领取还在的进程不许补查插手（耐久层没把它变成自动重投）", refused12["ok"] is False and "活领取" in refused12["reason"] and box12.pending_for_ticket("tk12") != [], repr(refused12))

# 新进程：同一数据根、全新装配（含真 execute_turn 的模型往返循环）
seen13: list = []


def model13(request: dict) -> dict:
    blob = "\n".join(str(m.get("content") or "") for m in (request.get("messages") or [])) + "\n" + str(request.get("system") or "")
    seen13.append(blob)
    return {"text": "接着做", "calls": [{"tool": "finish_work", "args": {"summary": "耐久收件箱续跑收口"}}]}


parts13 = ta.build_dispatcher(
    model_call=model13,
    send_fn=lambda channel, target, text: True,
    goal="把这条工单做完",
    run_command=lambda command: {"output": f"已执行：{command}"},
    read_file=lambda path, units, start=0: {"text": f"{path} 正文"},
    list_dir=lambda path, limit: {"items": []},
    write_file=lambda path, content: {"reason": f"写入 {path}"},
)
os.environ["TONGTIANWORK_HOME"] = str(home12)
box13 = parts13["inboxJournal"]
sched13 = tts.TicketTurnScheduler(parts13["dispatcher"], thread_factory=tts.inline_thread_factory)
resumer13 = trs.TicketRestartResume(sched13)
check("build_dispatcher 默认就接上耐久收件箱（接线侧不必自己拼）", isinstance(box13, tir.TicketInbox) and parts13["registry"].inbox_journal is box13 and parts13["dispatcher"].inbox_journal is box13, repr({k: type(v).__name__ for k, v in (("registry", parts13["registry"].inbox_journal), ("dispatcher", parts13["dispatcher"].inbox_journal))}))
check("重启后的新进程读同一份盘：两条在箱记录从盘重读", [r["text"] for r in box13.pending_for_ticket("tk12")] == [M_B, M_C], repr(box13.snapshot()))
check("新进程的注册表是空的（补查的前提：内存件随进程死了）", parts13["registry"].worker("tk12") is None)
check("空注册表上的查询也报得出磁盘条数（不是只有活绑定才看得见）", parts13["dispatcher"].state_of("tk12")["journalPending"] == 2, repr(parts13["dispatcher"].state_of("tk12")))
plan_nocand = resumer13.plan("tk12", prev_assignment_id="as12-old", candidate_wakes=[], title="耐久单")
check("**一条候选都不给**，引擎也认得出这两条没进过会话", plan_nocand["ok"] and [w["text"] for w in plan_nocand["unread"]] == [M_B, M_C], repr({"action": plan_nocand.get("action"), "unread": plan_nocand.get("unread"), "reason": plan_nocand.get("reason")}))
check("证据里分清「从盘捞回的」与「磁盘在箱总数」", plan_nocand["evidence"]["journalSourced"] == 2 and plan_nocand["evidence"]["journalPending"] == 2 and plan_nocand["evidence"]["journalAttached"] is True, repr(plan_nocand["evidence"]))
check("注记说明第二来源不能撤（停机期间到达的只有消息表知道）", any("条候选来自耐久收件箱" in n and "停机期间" in n for n in plan_nocand["notes"]), repr(plan_nocand["notes"]))
check("耐久候选按**各自投递时的领取**核对已读（不是一律按 prev）", [w["assignmentId"] for w in plan_nocand["unread"]] == ["as12-old", "as12-old"], repr(plan_nocand["unread"]))
res13 = resumer13.resume("tk12", prev_assignment_id="as12-old", new_assignment_id="as13-new", candidate_wakes=[], title="耐久单")
check("重投只投这两条（无候选也带得起来）", res13["ok"] and res13["action"] == "resumed" and res13["redelivered"] == 2 and res13["journalSourced"] == 2, repr({k: res13.get(k) for k in ("action", "redelivered", "journalSourced", "skippedAlreadyRead")}))
prompt13 = seen13[-1] if seen13 else ""
check("重投带的是源头到达时刻，不是重启这一刻", f"@ {T0 + 2}" in prompt13 and f"@ {T0 + 3}" in prompt13, repr(prompt13[-400:]))
check("旧领取那两条随到达键一起清掉（耐久日志自己不产垃圾）", box13.pending_for_ticket("tk12") == [] and box13.snapshot()["records"] == 0, repr(box13.snapshot()))
plan_again = resumer13.plan("tk12", prev_assignment_id="as12-old", candidate_wakes=[{"text": M_B, "atMs": T0 + 2}, {"text": M_C, "atMs": T0 + 3}], title="耐久单")
check("二次重启拿着同一批候选不再投一遍（跨领取凭据认得出）", plan_again["action"] == "nothing-to-resume" and len(plan_again["alreadyRead"]) == 2 and plan_again["evidence"]["journalSourced"] == 0, repr({"action": plan_again["action"], "read": len(plan_again["alreadyRead"]), "sourced": plan_again["evidence"]["journalSourced"]}))

# 候选与磁盘重复时只算一条（不裂成 4 条）
home14 = _TMP / "home-14"
os.environ["TONGTIANWORK_HOME"] = str(home14)
p14 = Proc(home=home14, recorder=Recorder(outcome="running"))
p14.wake("tk14", M_A, T0 + 1, assignment_id="as14")
p14b = Proc(home=None, recorder=Recorder(outcome="running"))
plan14 = p14b.resumer.plan("tk14", prev_assignment_id="as14", candidate_wakes=[{"text": M_A, "atMs": T0 + 1}], title="")
check("同一句话「候选里有、盘上也有」只算一条未读", [w["text"] for w in plan14["unread"]] == [M_A] and plan14["evidence"]["journalSourced"] == 0, repr({"unread": plan14["unread"], "sourced": plan14["evidence"]["journalSourced"]}))
check("在箱总数仍如实报（去重不改变盘上有什么）", plan14["evidence"]["journalPending"] == 1, repr(plan14["evidence"]))

# ── 8. 读过的不重投，也不留垃圾 ────────────────────────────────

home15 = _TMP / "home-15"
os.environ["TONGTIANWORK_HOME"] = str(home15)
box15 = tir.TicketInbox()
rec15 = box15.append(ticket_id="tk15", assignment_id="as15-old", text=M_A, origin="human", at_ms=T0 + 1)
p15 = Proc(home=None, recorder=Recorder(outcome="running"))
p15.ledger.mark_read("as15-old", read_id(M_A, T0 + 1, "as15-old"), step_id="as15-old-turn", ticket_id="tk15", arrival_key=tdr.wake_arrival_key(wake(M_A, T0 + 1, "as15-old")))
check("构造前提：账本说读过、盘上还挂着（销账那次没落盘的形态）", box15.pending_for_ticket("tk15") and p15.ledger.unread_of("as15-old", [read_id(M_A, T0 + 1, "as15-old")]) == [], repr({"pending": box15.pending_for_ticket("tk15")}))
plan15 = p15.resumer.plan("tk15", prev_assignment_id="as15-old", candidate_wakes=[], title="")
check("这条不重投（已读优先于「盘上说没进会话」）", plan15["action"] == "nothing-to-resume" and [w["text"] for w in plan15["alreadyRead"]] == [M_A], repr({"action": plan15["action"], "read": plan15["alreadyRead"]}))
check("但它被点名成待清理的遗留", plan15["evidence"]["journalStale"] == [rec15["wakeId"]] and any("在磁盘上还挂着" in n for n in plan15["notes"]), repr({"stale": plan15["evidence"]["journalStale"], "notes": plan15["notes"]}))
reader_after_plan = tir.TicketInbox()
check("plan 只点名不动盘（换一个读者从盘重读，那条还在）", [r["wakeId"] for r in reader_after_plan.pending_for_ticket("tk15")] == [rec15["wakeId"]], repr(reader_after_plan.pending_for_ticket("tk15")))
res15 = p15.resumer.resume("tk15", prev_assignment_id="as15-old", new_assignment_id="as15-new", candidate_wakes=[], title="")
reader_after_resume = tir.TicketInbox()
check("resume 顺手清掉遗留：磁盘归零且报出条数", res15["action"] == "nothing-to-resume" and res15["staleJournalCleared"] == 1 and reader_after_resume.pending_for_ticket("tk15") == [] and reader_after_resume.snapshot()["records"] == 0, repr({"action": res15["action"], "cleared": res15.get("staleJournalCleared"), "disk": reader_after_resume.pending_for_ticket("tk15")}))
check("同一份盘上的两个对象互不见写（这就是装配必须共享一个收件箱的原因）", box15.pending_for_ticket("tk15") != [] and reader_after_resume.pending_for_ticket("tk15") == [], repr({"oldInstance": len(box15.pending_for_ticket("tk15")), "freshReader": len(reader_after_resume.pending_for_ticket("tk15"))}))
plan15b = p15.resumer.plan("tk15", prev_assignment_id="as15-old", candidate_wakes=[], title="")
check("清理之后不再重复点名（同一垃圾不会每次重启都播一遍）", plan15b["evidence"]["journalStale"] == [] and not any("在磁盘上还挂着" in n for n in plan15b["notes"]), repr(plan15b["notes"]))

# ── 9. 不动的边界：这不是第二个队列 ────────────────────────────

home16 = _TMP / "home-16"
os.environ["TONGTIANWORK_HOME"] = str(home16)
p16 = Proc(home=None, recorder=Recorder(outcome="running"))
p16.sched.submit(ticket_id="tk16", title="", assignment_id="as16", message=M_A, origin="human", arrived_at_ms=T0 + 1)
snap16 = p16.outbox.snapshot()
check("收件箱不进外发账本：一条外发记录、一个开放事务都没多出", snap16["records"] == 0 and snap16["statusCounts"] == {} and snap16["openTx"] == [], repr(snap16))
check("耐久箱没有 kind / 幂等键 / 保序组（不是第二个队列）", not any(hasattr(p16.inbox, a) for a in ("begin", "enqueue", "commit", "run_pending")), repr([a for a in ("begin", "enqueue", "commit", "run_pending") if hasattr(p16.inbox, a)]))
check("单飞与续跑口径不变：一轮之后箱里没有滞留", p16.dispatcher.state_of("tk16")["queuedWakes"] == 0 and p16.recorder.count == 1, repr(p16.dispatcher.state_of("tk16")))
check("磁盘只由会话驱动：本轮读过之后箱里不留、查询也报 0", p16.inbox.pending_for_ticket("tk16") == [] and p16.dispatcher.state_of("tk16")["journalPending"] == 0, repr(p16.dispatcher.state_of("tk16")))
dup_home = _TMP / "home-17"
os.environ["TONGTIANWORK_HOME"] = str(dup_home)
p17 = Proc(home=None)
p17.wake("tk17", M_A, T0 + 1, assignment_id="as17")
p17.wake("tk17", M_A, T0 + 1, assignment_id="as17")
check("同一条唤醒重复投递：内存 2 条、磁盘 1 条（幂等只算投递事实）", len(p17.memory_inbox("tk17")) == 2 and len(p17.pending("tk17")) == 1, repr({"memory": len(p17.memory_inbox("tk17")), "disk": len(p17.pending("tk17"))}))
check("销账吃不到第二条：一次清光，不留半条", p17.dispatcher.wake_and_run(ticket_id="tk17", deliver=False)["ok"] and p17.pending("tk17") == [], repr(p17.pending("tk17")))
state17 = p17.dispatcher.state_of("tk17")
check("耐久层没改结局口径：outcome=running 的轮次不收口，对外报 waiting", state17["endedStatus"] == "" and state17["external"] == "waiting" and state17["runState"] != "running", repr(state17))

# 关掉耐久箱 = 回到 5g 的行为（候选是唯一来源）
home18 = _TMP / "home-18"
os.environ["TONGTIANWORK_HOME"] = str(home18)
p18 = Proc(home=None, journal=None, recorder=Recorder(outcome="running"))
p18.wake("tk18", M_A, T0 + 1, assignment_id="as18")
check("没接耐久箱时投递照常、内存箱照旧（关闭只是少一份耐久副本）", p18.memory_inbox("tk18") != [] and "journalWarning" not in p18.wake("tk18", M_B, T0 + 2, assignment_id="as18"), repr(p18.memory_inbox("tk18")))
p18b = Proc(home=None, journal=None, recorder=Recorder(outcome="running"))
plan18 = p18b.resumer.plan("tk18", prev_assignment_id="as18", candidate_wakes=[], title="")
check("没接耐久箱时如实说明「只能靠调用方清单」", plan18["action"] == "nothing-to-resume" and any("本装配没接耐久收件箱" in n for n in plan18["notes"]), repr(plan18))
check("没接耐久箱时查询接口报 0 而不是 None", p18.dispatcher.state_of("tk18")["journalPending"] == 0 and plan18["evidence"]["journalAttached"] is False, repr(p18.dispatcher.state_of("tk18")))
closed18 = ta.build_dispatcher(
    model_call=lambda request: {"text": "用不上", "calls": [{"tool": "finish_work", "args": {"summary": "关箱装配"}}]},
    send_fn=lambda channel, target, text: True,
    goal="把这条工单做完",
    run_command=lambda command: {"output": f"已执行：{command}"},
    read_file=lambda path, units, start=0: {"text": f"{path} 正文"},
    list_dir=lambda path, limit: {"items": []},
    write_file=lambda path, content: {"reason": f"写入 {path}"},
    inbox_journal=False,
)
closed_sched = tts.TicketTurnScheduler(closed18["dispatcher"], thread_factory=tts.inline_thread_factory)
closed_resumer = trs.TicketRestartResume(closed_sched)
closed_sched.submit(ticket_id="tk18b", title="关箱单", assignment_id="as18b", message=M_A, origin="human", arrived_at_ms=T0 + 1)
closed_state = closed18["dispatcher"].state_of("tk18b")
check("build_dispatcher(inbox_journal=False) 真把三层都关掉（不是只关查询）", closed18["inboxJournal"] is None and closed18["registry"].inbox_journal is None and closed18["dispatcher"].inbox_journal is None, repr({k: type(v).__name__ for k, v in (("returned", closed18["inboxJournal"]), ("registry", closed18["registry"].inbox_journal), ("dispatcher", closed18["dispatcher"].inbox_journal))}))
check("关掉后投递照样成功且不写文件", closed_state["journalPending"] == 0 and not dp.ticket_inbox_file().exists(), repr(closed_state))
check("关掉后补查只剩调用方候选（5g 行为逐字仍在）", any("本装配没接耐久收件箱" in n for n in closed_resumer.plan("tk18b", prev_assignment_id="as18b", candidate_wakes=[], title="关箱单")["notes"]), repr(closed_resumer.plan("tk18b", prev_assignment_id="as18b", candidate_wakes=[], title="关箱单")["notes"]))

print("-" * 52)
total = len(FAILURES)
if FAILURES:
    print(f"FAILED: {total} 项")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print(f"PASS: 耐久收件箱全部检查通过")
sys.exit(0)
