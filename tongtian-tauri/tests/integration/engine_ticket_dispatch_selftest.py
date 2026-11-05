#!/usr/bin/env python3
"""
工单回合循环胶水（缺陷 D-48.5 预备切片）自测

TicketDispatcher 把 D-48.1（目录/检查点/槽位）、D-48.2（耐久副作用队列）、
D-48.3（恢复注记/已读落账/看门狗）接成「一轮 worker 回合」。这里锁的是**接线
口径**——三个运行时各自绿不等于连起来对：

1. 收口不靠执行体自述：finished 且看门狗确认无作业才 end_assignment(finished)
   （删点、还槽）；作业还在跑就保持 working、槽位不还在、不许上报 waiting。
2. 回合失败不外发：ok=false 或抛错 → 事务整体 rollback，一条也不 dispatch，
   已落盘的对话检查点不被覆盖（dropped 留证据）。
3. 第四种结局不采信：未知 outcome 走失败口径，不猜「大概干完了」。
4. 已读落账发生在「喂进本轮输入」之后；同文案不同到达时间是两条事实。
5. 续跑：恢复注记按 **旧 Assignment** 读检查点，点名在途外发、崩溃作业、
   收件箱残留；对话上下文可以借旧检查点，但落盘只写新 Assignment 的键。
6. 幂等：同 dedupeKey 的第二轮被账本挡下，不二次发送；未知 kind 被拒且留话。
7. 多轮连续性：同一 Assignment 的第二轮读到第一轮的检查点消息。
8. 槽位口径穿过接线：Agent 占满时人的唤醒仍能起 worker，被拒的工单不留目录。

离线运行：模型 / HTTP / 进程全部由假实现注入；TONGTIANWORK_HOME 指向临时目录。
纯 stdlib。

用法: python tests/integration/engine_ticket_dispatch_selftest.py
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TMP = Path(tempfile.mkdtemp(prefix="tt-ticket-dispatch-selftest-"))
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
import ticket_worker_runtime as twr  # noqa: E402

CASE = {"n": 0}


class FakeTurn:
    """按脚本给出回合结果；脚本里放 Exception 实例就当场抛（模拟回合炸掉）。"""

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls: list = []

    def __call__(self, snapshot, prompt, history):
        self.calls.append({"snapshot": snapshot, "prompt": prompt, "history": list(history)})
        if not self.scripted:
            raise AssertionError("脚本外的额外回合")
        result = self.scripted.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def turn(**kw):
    base = {"ok": True, "outcome": "finished", "messages": [], "sends": [], "jobs": [], "reason": ""}
    base.update(kw)
    return base


class Case:
    """一套互不污染的运行时 + 记录 dispatch 的假发送实现。"""

    def __init__(self, *, agent_slots=2, human_extra=1, timeout_seconds=trr.JOB_TIMEOUT_SECONDS):
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

    def dispatch(self, record):
        self.sent.append(record["payload"]["text"])
        return True

    def kill_ok(self, job):
        self.killed.append(job["jobId"])
        return True

    def kill_boom(self, job):
        self.killed.append(job["jobId"])
        raise OSError("权限不足")

    def dispatcher(self, execute_turn, **kw):
        return tdr.TicketDispatcher(
            registry=self.registry,
            outbox=self.outbox,
            ledger=self.ledger,
            watchdog=self.watchdog,
            execute_turn=execute_turn,
            send_dispatch=kw.pop("send_dispatch", self.dispatch),
            kill_job=kw.pop("kill_job", self.kill_ok),
            **kw,
        )

    def occupied(self):
        return self.registry.pool.snapshot()["occupied"]


def advance(case, minutes):
    case.now["t"] += minutes * 60


# ── 1. 正常干完一轮：删点、还槽、外发提交后才执行、唤醒落账 ──────

c1 = Case()
ex1 = FakeTurn([turn(messages=[{"role": "assistant", "content": "已交付"}], sends=[
    {"kind": "reply", "sessionKey": "sess-1", "payload": "报告已发出", "dedupeKey": "r-1"},
], reason="做完")])
d1 = c1.dispatcher(ex1)
r1 = d1.wake_and_run(ticket_id="tk-a", title="把季度报告发出去", assignment_id="as-a1", message="今天必须交")

check("回合成功且收口为 finished", r1["ok"] and r1["ended"] == "finished", json.dumps(r1, ensure_ascii=False))
check("外发在提交之后才执行（dispatch 收到正文）", c1.sent == ["报告已发出"], str(c1.sent))
check("副作用账本记一条 done", c1.outbox.snapshot()["statusCounts"].get("done") == 1, json.dumps(c1.outbox.snapshot()["statusCounts"]))
check("doneState=waiting（无在跑作业）", r1["doneState"] == "waiting")
check("唤醒被消费并计 1 条", r1["wakesConsumed"] == 1 and r1["readsMarked"] == 1)
check("已读事实落到账本", c1.ledger.snapshot()["marks"] == 1)
check("finished 删掉检查点（正常干完不留恢复点）", cr.load_checkpoint(r1["checkpointKey"]) is None)
check("finished 归还槽位", "tk-a" not in c1.occupied(), str(c1.occupied()))
check("工单目录按工单 id 建出来", any(p.startswith("worker-") for p in os.listdir(c1.workers_root)), str(os.listdir(c1.workers_root)))
check("提示词里点名本轮唤醒正文", "今天必须交" in ex1.calls[0]["prompt"] and "1." in ex1.calls[0]["prompt"])
check("标题没进目录名（可读性只在快照里）", all("季度" not in p for p in os.listdir(c1.workers_root)))
check("reason 如实带回", r1["reason"] == "做完")

w1 = {"text": "同一条文案", "atMs": 1, "assignmentId": "as-x"}
w2 = {"text": "同一条文案", "atMs": 2, "assignmentId": "as-x"}
w3 = {"text": "同一条文案", "atMs": 1, "assignmentId": "as-y"}
check("同文案不同到达时间是两条唤醒", tdr.wake_read_id(w1) != tdr.wake_read_id(w2))
check("同文案不同 Assignment 也是两条", tdr.wake_read_id(w1) != tdr.wake_read_id(w3))

# ── 2. 多轮连续 + 幂等 + 未知 kind ────────────────────────────

c2 = Case()
ex2 = FakeTurn([
    turn(outcome="running", messages=[{"role": "assistant", "content": "写完一半"}], sends=[
        {"kind": "relay", "sessionKey": "sess-2", "payload": "进度 50%", "dedupeKey": "relay-1"},
    ]),
    turn(messages=[{"role": "assistant", "content": "写完一半"}, {"role": "assistant", "content": "收尾"}], sends=[
        {"kind": "reply", "sessionKey": "sess-2", "payload": "进度 50%", "dedupeKey": "relay-1"},
        {"kind": "carrier-pigeon", "sessionKey": "sess-2", "payload": "飞鸽传书"},
        {"kind": "reply", "sessionKey": "sess-2", "payload": "全部完成", "dedupeKey": "reply-2"},
    ]),
])
d2 = c2.dispatcher(ex2)
p1 = d2.wake_and_run(ticket_id="tk-b", assignment_id="as-b1", message="开始做")
check("outcome=running 不收口（ended 为空）", p1["ok"] and p1["ended"] == "", json.dumps(p1, ensure_ascii=False))
check("running 轮把可续跑检查点落盘", p1["checkpointSaved"] and cr.load_checkpoint(p1["checkpointKey"]) is not None)
check("running 轮槽位仍在手上", "tk-b" in c2.occupied())
check("第一轮 relay 已提交并发送", c2.sent == ["进度 50%"], str(c2.sent))

mid = d2.state_of("tk-b")
check("两轮之间对外是 waiting（回合结束且无作业）", mid["external"] == "waiting", json.dumps(mid, ensure_ascii=False))

p2 = d2.wake_and_run(ticket_id="tk-b", assignment_id="as-b9", message="继续")
check("已有活绑定时复用原 Assignment（不抢新槽）", p2["assignmentId"] == "as-b1")
check("第二轮读到第一轮的检查点消息", p2["historySource"] == "assignment" and p2["historyMessages"] == 1, json.dumps({k: p2[k] for k in ("historySource", "historyMessages")}))
check("重复 dedupeKey 被幂等挡下不二次发送", p2["sendsDeduped"] == 1 and c2.sent == ["进度 50%", "全部完成"], str(c2.sent))
check("未知 kind 被拒且在结果里留话", p2["sendsRejected"] == 1 and any("carrier-pigeon" in x for x in p2["notes"]), json.dumps(p2["notes"], ensure_ascii=False))
check("第二轮收口 finished", p2["ended"] == "finished" and "tk-b" not in c2.occupied())
check("finished 后检查点已删", cr.load_checkpoint(p2["checkpointKey"]) is None)

# ── 3. 回合失败：事务整体回滚，一条也不外发；旧检查点不被覆盖 ────

c3 = Case()
ex3 = FakeTurn([
    turn(outcome="running", messages=[{"role": "assistant", "content": "第一轮的话"}], sends=[
        {"kind": "notify", "sessionKey": "sess-3", "payload": "第一轮通知", "dedupeKey": "n-1"},
    ]),
    turn(ok=False, outcome="finished", reason="模型超时", sends=[
        {"kind": "reply", "sessionKey": "sess-3", "payload": "半截结论", "dedupeKey": "r-2"},
        {"kind": "relay", "sessionKey": "sess-3", "payload": "半截进度", "dedupeKey": "r-3"},
    ]),
])
d3 = c3.dispatcher(ex3)
f1 = d3.wake_and_run(ticket_id="tk-c", assignment_id="as-c1", message="开工")
kept_key = f1["checkpointKey"]
f2 = d3.wake_and_run(ticket_id="tk-c", assignment_id="as-c1", message="再来")
check("回合失败对外报 ok=false", (not f2["ok"]) and f2["reason"] == "模型超时", json.dumps(f2, ensure_ascii=False))
check("失败轮的 2 条外发全部回滚作废", f2["sendsRolledBack"] == 2, json.dumps(f2, ensure_ascii=False))
check("回滚后一条也没 dispatch", c3.sent == ["第一轮通知"], str(c3.sent))
check("失败轮走 dropped 收口", f2["ended"] == "dropped")
check("dropped 不覆盖已落的对话检查点", (cr.load_checkpoint(kept_key) or {}).get("messages") == [{"role": "assistant", "content": "第一轮的话"}], json.dumps(cr.load_checkpoint(kept_key), ensure_ascii=False))
check("失败轮不落新检查点", f2["checkpointSaved"] is False)

# ── 4. 回合当场抛错：同样按失败口径，不带着半截状态外发 ─────────

c4 = Case()
ex4 = FakeTurn([RuntimeError("上游断了")])
d4 = c4.dispatcher(ex4)
x1 = d4.wake_and_run(ticket_id="tk-c4", assignment_id="as-c4", message="跑一下")
check("抛错也走失败口径并点名异常", (not x1["ok"]) and "RuntimeError" in x1["reason"] and "上游断了" in x1["reason"], x1["reason"])
check("抛错轮没有任何外发记账", x1["sendsQueued"] == 0 and x1["sendsRolledBack"] == 0 and c4.sent == [])
check("抛错轮收口 dropped（留槽位归还与证据）", x1["ended"] == "dropped" and "tk-c4" not in c4.occupied())

# ── 5. 第四种结局不采信 ───────────────────────────────────────

c5 = Case()
ex5 = FakeTurn([turn(outcome="vanished", sends=[
    {"kind": "reply", "sessionKey": "sess-5", "payload": "假装发完", "dedupeKey": "v-1"},
])])
d5 = c5.dispatcher(ex5)
u1 = d5.wake_and_run(ticket_id="tk-d5", assignment_id="as-d5", message="随便")
check("未知结局被拒且写明只接受两种", (not u1["ok"]) and "未知结局" in u1["reason"] and "finished/running" in u1["reason"], u1["reason"])
check("不猜结局：外发作废、检查点不覆盖", u1["sendsRolledBack"] == 1 and c5.sent == [] and u1["checkpointSaved"] is False)
check("未知结局收口 dropped", u1["ended"] == "dropped")

# ── 6. 后台作业在跑：保持 working、槽位不还在，到点点名杀 ───────

c6 = Case(timeout_seconds=30 * 60)
ex6 = FakeTurn([
    turn(messages=[{"role": "assistant", "content": "起了个长活"}], jobs=[{"jobId": "build-9", "description": "编译发布"}]),
    turn(messages=[{"role": "assistant", "content": "起了个长活"}, {"role": "assistant", "content": "活被杀了"}]),
])
d6 = c6.dispatcher(ex6)
j1 = d6.wake_and_run(ticket_id="tk-e", assignment_id="as-e1", message="跑个长任务")
check("回合 finished 但作业在跑 → 不收口，保持 working", j1["heldWorking"] and j1["ended"] == "" and j1["doneState"] == "working", json.dumps(j1, ensure_ascii=False))
check("作业在跑时槽位不还在手上", "tk-e" in c6.occupied())
st = d6.state_of("tk-e")
check("state_of 对外报 working 并列名在跑作业", st["external"] == "working" and st["runningJobs"] == ["编译发布"], json.dumps(st, ensure_ascii=False))

advance(c6, 31)
j2 = d6.wake_and_run(ticket_id="tk-e", assignment_id="as-e1", message="怎么样了")
check("超 30 分钟到点杀，作业名与时长进报告", any("编译发布" in x and "31 分钟" in x for x in j2["killedJobs"]), json.dumps(j2["killedJobs"], ensure_ascii=False))
check("kill_fn 收到被杀作业本体", c6.killed == ["build-9"], str(c6.killed))
check("作业清空后才收口 finished", j2["ended"] == "finished" and "tk-e" not in c6.occupied())

c7 = Case(timeout_seconds=30 * 60)
ex7 = FakeTurn([
    turn(outcome="running", jobs=[{"jobId": "build-8", "description": "跑测试"}]),
    turn(outcome="running"),
])
d7 = c7.dispatcher(ex7, kill_job=c7.kill_boom)
k1 = d7.wake_and_run(ticket_id="tk-e7", assignment_id="as-e7", message="起作业")
advance(c7, 31)
k2 = d7.wake_and_run(ticket_id="tk-e7", assignment_id="as-e7", message="还在跑吗")
check("杀不掉要如实说杀不掉并提示人工处理", any("中止失败" in x and "人工处理" in x for x in k2["killedJobs"]), json.dumps(k2["killedJobs"], ensure_ascii=False))
check("杀失败的作业不再占状态", k2["doneState"] == "waiting")

# ── 7. 续跑：恢复注记吃旧检查点 / 在途外发 / 崩溃作业 / 残留唤醒 ──

c8 = Case()
prev_key = twr.checkpoint_task_id("as-old")
cr.save_checkpoint(prev_key, {"messages": [{"role": "user", "content": f"旧第{i}条"} for i in range(4)]})

# 上一轮留下的唤醒：绑定因崩溃丢失（lost），收件箱里还压着 2 条没消费
boot = c8.registry.start_or_wake(ticket_id="tk-d", title="续跑单", assignment_id="as-old", message="第一批")
c8.registry.start_or_wake(ticket_id="tk-d", assignment_id="as-old", message="补充要求一")
c8.registry.start_or_wake(ticket_id="tk-d", assignment_id="as-old", message="补充要求二")
c8.registry.end_assignment("tk-d", "lost", reason="进程重启前没来得及收尾")
check("构造前提：lost 收口后收件箱仍带着唤醒", boot["ok"] and len(c8.registry.worker("tk-d").inbox) == 3, str(len(c8.registry.worker("tk-d").inbox)))

# 在途外发：把账本里那条改成 running（崩溃时「已发出待确认」的形状）
c8.outbox.begin("tx-old")
c8.outbox.enqueue(kind="relay", ticket_id="tk-d", session_key="sess-8", payload="上一轮的发信", tx_id="tx-old")
c8.outbox.commit("tx-old")
raw = json.loads(c8.outbox.path.read_text(encoding="utf-8"))
raw["records"][0]["status"] = "running"
c8.outbox.path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
c8.outbox = tor.TicketOutbox(c8.outbox.path)
check("重启后读到账本里仍在途的外发", c8.outbox.inflight_for("tk-d")["runningIds"] != [])

# 进程内还挂着的后台作业：恢复注记必须点名它「没有完成」
c8.watchdog.register("job-3", description="打包安装", ticket_id="tk-d")

ex8 = FakeTurn([turn(outcome="running", messages=[{"role": "user", "content": f"旧第{i}条"} for i in range(4)] + [{"role": "assistant", "content": "续上"}])])
d8 = c8.dispatcher(ex8)
rec = d8.wake_and_run(ticket_id="tk-d", title="续跑单", assignment_id="as-new", message="接着做", prev_assignment_id="as-old")
note = rec["recoveryNote"]
check("续跑生成恢复注记并绑进新 Assignment", note.startswith("【恢复注记") and "禁止直接重复任何外部动作" in note, note)
check("注记先查目录且承认半成品", "目录在，半成品文件按已发生处理" in note)
check("注记点名在途外发 id", "仍在途、可能已经发出去" in note and "out-1" in note, note)
check("注记点名崩溃时仍在跑的后台", "打包安装（job-3）" in note and "已随进程消失" in note)
check("注记提醒先消费残留唤醒", "收件箱里还带着 3 条" in note, note)
check("注记写明旧检查点属旧领取", "as-old" in note and "不能当作「本轮已做完」的证明" in note)
check("注记进了本轮提示词开头", ex8.calls[0]["prompt"].startswith("【恢复注记"))
check("对话上下文从旧检查点借（只借上下文）", rec["historySource"] == "prev-assignment" and rec["historyMessages"] == 4, json.dumps({k: rec[k] for k in ("historySource", "historyMessages")}))
check("残留唤醒被消费并落账", rec["wakesConsumed"] == 4, str(rec["wakesConsumed"]))
new_key = rec["checkpointKey"]
check("续跑用的是新 Assignment 的检查点键", new_key != prev_key and new_key)
r_after = d8.wake_and_run(ticket_id="tk-d", assignment_id="as-new", message="再推进")
check("新检查点独立落盘", cr.load_checkpoint(new_key) is not None)
check("旧检查点原样保留（不被新轮覆盖）", len((cr.load_checkpoint(prev_key) or {}).get("messages") or []) == 4, json.dumps(cr.load_checkpoint(prev_key), ensure_ascii=False))
check("第二轮起上下文来自自己这份 Assignment", r_after["historySource"] == "assignment" and r_after["historyMessages"] == 5)

# 没有旧证据时也要如实说缺
c9 = Case()
ex9 = FakeTurn([turn()])
d9 = c9.dispatcher(ex9)
plain = d9.wake_and_run(ticket_id="tk-fresh", assignment_id="as-fresh", message="新单")
check("首轮不带 prev 就不生成恢复注记", plain["recoveryNote"] == "" and "恢复注记" not in ex9.calls[0]["prompt"])
check("首轮上下文为空且不假装续跑", plain["historySource"] == "none" and plain["historyMessages"] == 0)

# ── 8. 槽位口径穿过接线：人用预留槽 + 被拒不留目录 ──────────────

c10 = Case(agent_slots=1, human_extra=1)
ex10 = FakeTurn([turn(outcome="running"), turn(outcome="running"), turn(outcome="running")])
d10 = c10.dispatcher(ex10)
s1 = d10.wake_and_run(ticket_id="tk-g1", assignment_id="as-g1", message="Agent 派的活", origin="agent")
s2 = d10.wake_and_run(ticket_id="tk-g2", assignment_id="as-g2", message="再来一单", origin="agent")
s3 = d10.wake_and_run(ticket_id="tk-g3", assignment_id="as-g3", message="人的消息", origin="human")
check("Agent 占满额度后第二单被拒", (not s2["ok"]) and s2["stage"] == "start_or_wake" and "槽位已占满" in s2["reason"], json.dumps(s2, ensure_ascii=False))
check("被拒的工单没留下目录", not c10.workers_root.joinpath(twr.worker_dir_name("tk-g2")).exists())
check("被拒时一条外发也没记账", c10.outbox.snapshot()["records"] == 0)
check("人用预留槽让人的唤醒立即起 worker", s3["ok"] and "tk-g3" in c10.occupied())

# ── 9. 事务开不成时不许静默丢外发 ─────────────────────────────

c11 = Case()


class DeadBegin:
    """只坏 begin 一件事：事务开不成，后面的 enqueue 根本不该发生。"""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def begin(self, tx_id):
        return {"ok": False, "reason": "模拟：事务名被占用"}

    def enqueue(self, **kw):
        raise AssertionError("事务没开成就不能再 enqueue，接线必须停在这一步")


ex11 = FakeTurn([turn(sends=[{"kind": "reply", "sessionKey": "s", "payload": "x", "dedupeKey": "z"}])])
d11 = c11.dispatcher(ex11)
d11.outbox = DeadBegin(c11.outbox)
b1 = d11.wake_and_run(ticket_id="tk-h", assignment_id="as-h", message="发一条")
check("事务开不成时写明一条也没记账", any("外发事务没开成" in x for x in b1["notes"]), json.dumps(b1["notes"], ensure_ascii=False))
check("事务开不成不误报已排队", b1["sendsQueued"] == 0 and b1["sendsRejected"] == 1)
check("这种情况也没有 dispatch 任何动作", c11.sent == [])

# ── 10. 不显式给路径时，账本落在 D-43 数据根里 ─────────────────

home = Path(os.environ["TONGTIANWORK_HOME"])
root_outbox = tor.TicketOutbox()
root_ledger = trr.ReadLedger()
check("副作用账本默认落在引擎数据根", root_outbox.path.parent == home, str(root_outbox.path))
check("已读账本默认落在引擎数据根", root_ledger.path.parent == home, str(root_ledger.path))
root_outbox.begin("tx-root")
root_outbox.enqueue(kind="notify", ticket_id="tk-root", payload="一条", tx_id="tx-root")
root_outbox.commit("tx-root")
root_ledger.mark_read("as-root", "msg-root")
check("两份账本都真写出来了", root_outbox.path.exists() and root_ledger.path.exists())

# ── 收尾 ──────────────────────────────────────────────────────

shutil.rmtree(_TMP, ignore_errors=True)
if FAILURES:
    print(f"\nFAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
    sys.exit(1)
print("\nOK: 工单回合循环接线（收口/回滚/续跑/幂等/槽位）全部检查通过")
