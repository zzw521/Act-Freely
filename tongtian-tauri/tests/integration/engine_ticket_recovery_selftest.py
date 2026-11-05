#!/usr/bin/env python3
"""
重启恢复三件套（缺陷 D-48.3）自测

锁住三条口径，防止「恢复靠自觉」的假落地：

1. 恢复注记：新 Assignment 的续跑指令必须按顺序点名证据——先查目录、再查
   git/远端、再对照副作用账本的在途外发，最后才谈重复外部动作；旧检查点属于
   旧领取不能当「本轮已做完」；崩溃时在跑的后台作业必须点名「已消失」；
   收件箱带着的唤醒要提醒先消费。
2. 已读落账：(Assignment, 消息) 落磁盘、幂等；unread_of 是重启补查的唯一事实
   来源；不同 Assignment 的落账互不冒充；坏账本改名让路不静默。
3. 看门狗：30 分钟到点杀并在报告里点名（含 elapsed）；杀失败要如实说杀不掉；
   回合结束但作业在跑时状态是 working 不是 waiting。

离线运行：不联网、不起服务；TONGTIANWORK_HOME 指向临时目录，kill_fn 是假函数。
纯 stdlib。

用法: python tests/integration/engine_ticket_recovery_selftest.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TMP = Path(tempfile.mkdtemp(prefix="tt-ticket-recovery-selftest-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")

sys.path.insert(0, str(PROJECT_ROOT / "engine"))

FAILURES: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


import ticket_recovery_runtime as trr  # noqa: E402

CLOCK = {"now": 1_000_000.0}

# ── 1. 恢复注记 ───────────────────────────────────────────────

note = trr.build_recovery_note(
    ticket_id="tk-1",
    prev_assignment_id="as-old",
    workspace_dir=str(_TMP / "worker-x"),
    dir_exists=True,
    checkpoint_saved_at="2026-11-02 21:00:00",
    checkpoint_messages=14,
    inflight_outbox_ids=["out-7", "out-8"],
    carried_wakes=2,
    crashed_jobs=["编译发布（build-1）"],
)
lines = note.split("\n")
check("注记点名工单续跑性质", "恢复注记" in lines[0] and "禁止直接重复任何外部动作" in lines[0])
order = [i for i, x in enumerate(lines) if x.startswith(("1)", "2)", "3)", "4)", "5)", "6)"))]
check("动作顺序按 1→6 编号排布", order == sorted(order) and len(order) == 6, str(order))
check("先查目录且承认半成品", "worker-x" in note and "半成品" in note)
check("再查 git 历史与远端", "git" in note and "远端" in note)
check("点名在途外发 id 并要求先核实再重发", "out-7" in note and "out-8" in note and "确认没送达才允许重发" in note)
check("旧检查点写明归属旧 Assignment", "as-old" in note and "2026-11-02 21:00:00" in note and "不能当作「本轮已做完」" in note)
check("崩溃作业点名已消失", "编译发布（build-1）" in note and "已随进程消失" in note)
check("收件箱唤醒提醒先消费", "2 条" in note and "先消费" in note)
check("空工单 id 不产注记", trr.build_recovery_note(ticket_id="") == "")

note2 = trr.build_recovery_note(ticket_id="tk-2", dir_exists=False, pending_outbox_kinds=["reply", "relay"])
check("无目录时如实说从零开始", "目录不在" in note2 and "从零开始" in note2, note2)
check("queued 按未发出处理并去重列 kind", "kind：reply、relay" in note2 and "开新事务" in note2, note2)
note3 = trr.build_recovery_note(ticket_id="tk-3")
check("什么都没有时也报「无悬空外发」", "没有悬空的外发" in note3 and "上一轮信息缺失" in note3, note3)
big = trr.build_recovery_note(ticket_id="tk-4", inflight_outbox_ids=[f"out-{i}" for i in range(30)])
check("在途 id 过多只点名前 12 条并报总数", "共 30 条" in big and "out-12" not in big)

# ── 2. 已读落账 ───────────────────────────────────────────────

ledger = trr.ReadLedger(_TMP / "reads.json", clock=lambda: CLOCK["now"])
m1 = ledger.mark_read("as-1", "msg-A", step_id="step-3")
check("首次落账成功并带步骤", m1["ok"] and not m1["deduped"] and m1["mark"]["stepId"] == "step-3")
m2 = ledger.mark_read("as-1", "msg-A", step_id="step-9")
check("重复落账幂等（保留第一次的事实）", m2["ok"] and m2["deduped"] and m2["mark"]["stepId"] == "step-3")
check("空 id 组合被拒并给理由", not ledger.mark_read("", "msg-B")["ok"] and not ledger.mark_read("as-1", "")["ok"])
check("is_read 只认落过账的", ledger.is_read("as-1", "msg-A") and not ledger.is_read("as-1", "msg-B"))
ledger.mark_read("as-1", "msg-B")
check("unread_of 保持候选顺序、剔除已读", ledger.unread_of("as-1", ["msg-C", "msg-A", "msg-B"]) == ["msg-C"], str(ledger.unread_of("as-1", ["msg-C", "msg-A", "msg-B"])))
check("换 Assignment 从零核对：旧落账不算新领取已读", ledger.unread_of("as-2", ["msg-A"]) == ["msg-A"])
ledger.mark_read("as-2", "msg-A", step_id="new-1")
check("新 Assignment 自己落账后才算已读", ledger.unread_of("as-2", ["msg-A"]) == [] and ledger.is_read("as-2", "msg-A"))
check("未落账的新 Assignment 全算未读", ledger.unread_of("as-3", ["msg-A", "msg-C"]) == ["msg-A", "msg-C"])
snap = ledger.snapshot()
check("快照按 Assignment 计数", snap["byAssignment"].get("as-1") == 2 and snap["byAssignment"].get("as-2") == 1, str(snap))
check("落盘重启后事实仍在", trr.ReadLedger(_TMP / "reads.json").is_read("as-1", "msg-A"))

bad = _TMP / "bad-reads.json"
bad.write_text("{ nope", encoding="utf-8")
lb = trr.ReadLedger(bad)
check("坏已读账本改名保留", any(p.name.startswith("bad-reads.json.corrupt-") for p in bad.parent.iterdir()))
check("坏账本暴露 loadError 且照常可写", "损坏" in lb.load_error and lb.mark_read("as-x", "msg-x")["ok"], lb.load_error)

# ── 3. 看门狗：30 分钟到点杀、点名、槽位口径 ──────────────────

dog = trr.JobWatchdog(clock=lambda: CLOCK["now"])
r = dog.register("job-1", description="导出季度报表", ticket_id="tk-W")
check("登记作业成功", r["ok"])
check("重复登记被拒（计时不合并）", not dog.register("job-1")["ok"])
check("空 id 作业被拒", not dog.register("")["ok"])
killed = dog.tick(kill_fn=lambda j: True, now=CLOCK["now"] + 29 * 60)
check("未满 30 分钟不杀", killed == [] and dog.running_for("tk-W"))
check("回合结束但作业在跑：状态保持 working 不是 waiting", dog.turn_done_state("tk-W") == "working")
killed = dog.tick(kill_fn=lambda j: True, now=CLOCK["now"] + 31 * 60)
check("超 30 分钟被杀并返回清单", len(killed) == 1 and killed[0]["jobId"] == "job-1" and killed[0]["killOk"], str(killed))
check("报告行点名作业与时长（验收项③）", "导出季度报表" in dog.kill_report_lines(killed)[0] and "31 分钟" in dog.kill_report_lines(killed)[0], str(dog.kill_report_lines(killed)))
check("被杀后不再占在跑位：回合结束可报 waiting", dog.turn_done_state("tk-W") == "waiting")
check("杀掉的记入历史供汇报", dog.killed_history() and dog.killed_history()[0]["jobId"] == "job-1")

dog.register("job-2", description="长跑基线", ticket_id="tk-W")


def kill_boom(job):
    raise RuntimeError("进程句柄已丢失")


killed2 = dog.tick(kill_fn=kill_boom, now=CLOCK["now"] + 62 * 60)
check("杀不掉也到点出列并如实标失败", len(killed2) == 1 and not killed2[0]["killOk"] and "进程句柄" in killed2[0]["killError"], str(killed2))
line2 = dog.kill_report_lines(killed2)[0]
check("杀失败的报告点名要求人工处理", "中止失败" in line2 and "人工处理" in line2, line2)

dog.register("job-3", description="快任务", ticket_id="tk-V")
c = dog.complete("job-3")
check("正常完成销账并回报时长", c["ok"] and c["elapsedSeconds"] == 0.0)
tick_after = dog.tick(kill_fn=lambda j: True, now=CLOCK["now"] + 999999)
check("完成后再 tick 不会被杀", all(e["jobId"] != "job-3" for e in tick_after))
check("重复 complete 被诚实拒绝", not dog.complete("job-3")["ok"])
check("崩溃注记输入能拿到在跑作业名", dog.crashed_job_descriptions() == [], "全部作业已出列")

# ── 4. 三件机制的接线面：注记吃看门狗与账本的输出 ─────────────

dog.register("job-9", description="部署预览", ticket_id="tk-R")
note4 = trr.build_recovery_note(
    ticket_id="tk-R",
    prev_assignment_id="as-r1",
    workspace_dir="worker-abc",
    dir_exists=True,
    crashed_jobs=dog.crashed_job_descriptions("tk-R"),
)
check("看门狗在跑清单直接喂给恢复注记", "部署预览（job-9）" in note4 and "已随进程消失" in note4, note4)

# ── 收尾 ──────────────────────────────────────────────────────

shutil.rmtree(_TMP, ignore_errors=True)
if FAILURES:
    print(f"\nFAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
    sys.exit(1)
print("\nOK: 重启恢复注记/已读落账/看门狗全部检查通过")
