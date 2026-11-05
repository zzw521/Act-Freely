#!/usr/bin/env python3
"""
杀进程重启演练（缺陷 D-48 验收项① 的副作用层，引擎级）

前面的自测都是「在同一进程里伪造崩溃形状」（手改 JSON、注入过期时钟）。这一刀
不做伪造：**真起一个子进程跑 worker 回合，在它把外发发到一半、租约已占、事务已
提交的时候把它杀掉**，然后由新进程重新打开同一份账本，验四件事：

1. **done 永不重放**——崩溃前已发送成功的 A，重启后无论跑多少轮 dispatch 都不
   再发第二次（这是验收项①「重启不重放已完成动作」在副作用层的本体）。
2. **未提交按作废**——崩溃后新开的 leaked 事务（enqueue 未 commit）在重启加载时
   置 aborted 并写明原因，永远不会被 dispatch。
3. **在途只按租约补投一次**——崩溃时 running 的 B 在租约未到期内不动；过租约后
   回收重投，且**只投这一次**，投完落 done。
4. **重启证据齐全**——恢复注记点名在途记录 id 与「确认没送达才允许重发」；对话
   上下文从旧领取的检查点借到、但不覆盖旧的那一份；已读事实跨进程存活，新领取
   不冒用旧已读。

离线运行：不起服务、不联网；子进程只跑引擎 Python，全程 TONGTIANWORK_HOME 指向
临时目录。模型回合由子进程里的假 execute_turn 提供——这里验的是崩溃语义，不是模型。

用法: python tests/integration/engine_ticket_crash_drill.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TMP = Path(tempfile.mkdtemp(prefix="tt-ticket-crash-drill-"))
_HOME = _TMP / "home"
_MARKERS = _TMP / "markers"
_MARKERS.mkdir(parents=True)
os.environ["TONGTIANWORK_HOME"] = str(_HOME)

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
import ticket_outbox_runtime as tor  # noqa: E402
import ticket_recovery_runtime as trr  # noqa: E402
import ticket_worker_runtime as twr  # noqa: E402

TICKET = "tk-crash"
CRASH_ASSIGNMENT = "as-crash"
RESTART_ASSIGNMENT = "as-restart"

# ── 1. 真起子进程，在 dispatch 中间把它杀掉 ────────────────────

child_env = dict(os.environ)
child_proc = subprocess.Popen(
    [sys.executable, str(Path(__file__).with_name("_ticket_crash_child.py")), str(_MARKERS), TICKET, CRASH_ASSIGNMENT],
    cwd=str(PROJECT_ROOT),
    env=child_env,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    encoding="utf-8",
    errors="replace",
)

deadline = time.time() + 30
while time.time() < deadline and not (_MARKERS / "blocked-in-dispatch").exists():
    if child_proc.poll() is not None:
        break
    time.sleep(0.05)

blocked = (_MARKERS / "blocked-in-dispatch").exists()
check("子进程真的跑到「外发已提交、租约已占、正在发送」这一步", blocked, f"markers={sorted(p.name for p in _MARKERS.iterdir())}")
if not blocked and child_proc.poll() is not None:
    out = child_proc.stdout.read() if child_proc.stdout else ""
    print(f"--- 子进程输出 ---\n{out}")
    check("子进程没有提前退出", False, out[-500:])

child_proc.kill()
try:
    child_proc.wait(timeout=10)
except subprocess.TimeoutExpired:  # noqa: BLE001  杀不掉就当场失败，不留模糊状态
    child_proc.terminate()
check("子进程已被杀掉（崩溃时刻是真的，不是模拟的）", child_proc.poll() is not None)
check("第一轮在崩溃前已完整收尾（A 确实发出去了）", (_MARKERS / "round1-done").exists() and (_MARKERS / "sent-A").exists())
check("第二轮从未「发完」（B 停在在途，正是崩溃形状）", not (_MARKERS / "round2-done").exists() and (_MARKERS / "sent-B").exists())

# ── 2. 崩溃留在磁盘上的账本形状 ────────────────────────────────

outbox_path = dp.ticket_outbox_file()
raw = json.loads(outbox_path.read_text(encoding="utf-8"))
by_text = {r["payload"]["text"]: r for r in raw["records"]}
check("账本里就是两条：A done + B running", set(by_text) == {"A", "B"} and by_text["A"]["status"] == "done" and by_text["B"]["status"] == "running", json.dumps(raw["records"], ensure_ascii=False)[:400])
b_id = by_text["B"]["id"]
a_id = by_text["A"]["id"]
check("B 占着租约（acquiredAtMs 非零，等着被回收）", int(by_text["B"].get("acquiredAtMs") or 0) > 0)

# 崩溃之后再开一个「忘了提交」的事务：新进程加载时必须按未提交作废
leak = tor.TicketOutbox()
leak.begin("tx-leaked")
leak.enqueue(kind="notify", ticket_id=TICKET, session_key="sess-crash", payload="C", tx_id="tx-leaked")
c_raw = json.loads(outbox_path.read_text(encoding="utf-8"))
c_rec = [r for r in c_raw["records"] if r["payload"]["text"] == "C"][0]
check("未提交的 C 此刻只是 queued（还没被授权执行）", c_rec["status"] == "queued", json.dumps(c_rec, ensure_ascii=False))

restarted = tor.TicketOutbox()
snap = restarted.snapshot()
check("重启加载把未提交的 C 按作废处理", snap["statusCounts"].get("aborted") == 1 and not snap["openTx"], json.dumps(snap["statusCounts"]))
c_view = restarted.record_view(c_rec["id"])
check("作废原因写明「提交没发生」", c_view is not None and c_view["status"] == "aborted" and "未提交" in (c_view.get("error") or ""), json.dumps(c_view, ensure_ascii=False))

# ── 3. 重启后第一轮：done 的 A 绝不重发，在途 B 租约没到期也不动 ──

sent_after_restart: list = []


def parent_dispatch(record):
    sent_after_restart.append(record["payload"]["text"])
    return True


summary1 = restarted.run_pending(parent_dispatch)
check("重启后立刻跑一轮：一条也不外发（A 已 done、B 租约未到）", summary1["dispatched"] == 0 and sent_after_restart == [], json.dumps(summary1, ensure_ascii=False))

# ── 4. 恢复注记与检查点：新进程能拿到「上一轮做到哪」的证据 ──────

ledger = trr.ReadLedger()
check("崩溃前的已读事实跨进程存活（两条唤醒都记在 as-crash 名下）", ledger.snapshot()["byAssignment"].get(CRASH_ASSIGNMENT) == 2, json.dumps(ledger.snapshot(), ensure_ascii=False))
crash_mark_ids = [m["messageId"] for m in ledger.marks_of(CRASH_ASSIGNMENT)]
check("新领取不冒用旧已读（同一个 id 在 as-restart 名下算未读）", crash_mark_ids and ledger.is_read(RESTART_ASSIGNMENT, crash_mark_ids[0]) is False)

registry = twr.TicketWorkerRegistry()
watchdog = trr.JobWatchdog()
restart_script = [{"ok": True, "outcome": "running", "messages": [], "sends": [], "jobs": [], "reason": "先看证据"}]


def restart_turn(snapshot, prompt, history):
    restart_turn.calls.append({"prompt": prompt, "history": list(history)})
    return dict(restart_script[0])


restart_turn.calls = []
dispatcher = tdr.TicketDispatcher(
    registry=registry,
    outbox=restarted,
    ledger=ledger,
    watchdog=watchdog,
    execute_turn=restart_turn,
    send_dispatch=parent_dispatch,
)
rec = dispatcher.wake_and_run(ticket_id=TICKET, title="崩溃演练单", assignment_id=RESTART_ASSIGNMENT, message="重启后再唤醒", prev_assignment_id=CRASH_ASSIGNMENT)
note = rec["recoveryNote"]
check("恢复注记点名「仍在途、可能已发出」的那条外发 id", b_id in note and "确认没送达才允许重发" in note, note)
check("恢复注记不点名已 done 的 A（done 不是待核实项）", a_id not in note)
check("对话上下文从旧领取的检查点借到两轮", rec["historySource"] == "prev-assignment" and rec["historyMessages"] == 2, json.dumps({k: rec[k] for k in ("historySource", "historyMessages")}))
check("借来的历史真进了本轮提示词的上下文参数", len(restart_turn.calls[0]["history"]) == 2)
prev_key = twr.checkpoint_task_id(CRASH_ASSIGNMENT)
check("旧检查点原样保留（新落盘不覆盖旧证据）", cr.load_checkpoint(prev_key) is not None and len((cr.load_checkpoint(prev_key) or {}).get("messages") or []) == 2)
check("新领取有自己的检查点键（不与旧键共用）", rec["checkpointKey"] and rec["checkpointKey"] != prev_key)

# ── 5. 租约到期的那一次补投：只投在途的 B，投完落 done ──────────

forward = tor.TicketOutbox(clock=lambda: time.time() + (tor.DEFAULT_LEASE_SECONDS + 300))
sent_on_reclaim: list = []
summary2 = forward.run_pending(lambda record: (sent_on_reclaim.append(record["payload"]["text"]), True)[1])
check("过租约后回收重投：reclaimed=1、dispatched=1", summary2["reclaimed"] == 1 and summary2["dispatched"] == 1, json.dumps(summary2, ensure_ascii=False))
check("补投的正是在途的 B，不是已完成的 A", sent_on_reclaim == ["B"], str(sent_on_reclaim))
after = tor.TicketOutbox()
after_snap = after.snapshot()
check("补投成功后 B 落 done（点名到记录本身）", (after.record_view(b_id) or {}).get("status") == "done", json.dumps(after.record_view(b_id), ensure_ascii=False))
check("账本里没有残留的在途记录", after_snap["statusCounts"].get("done") == 2 and after_snap["statusCounts"].get("running", 0) == 0, json.dumps(after_snap["statusCounts"]))
sent_third: list = []
summary3 = after.run_pending(lambda record: (sent_third.append(record["payload"]["text"]), True)[1])
check("再跑一轮什么都不发（done 永不重放）", summary3["dispatched"] == 0 and sent_third == [], json.dumps(summary3, ensure_ascii=False))
check("作废的 C 从头到尾没有被发出去", "C" not in (sent_after_restart + sent_on_reclaim + sent_third), str(sent_after_restart + sent_on_reclaim + sent_third))

# ── 6. 演练留下的文件都在 D-43 数据根里（不落到别处） ───────────

check("演练全程只写引擎数据根", _HOME.exists() and outbox_path.parent == _HOME and dp.ticket_reads_file().parent == _HOME, str(outbox_path))

# ── 收尾 ──────────────────────────────────────────────────────

if child_proc.poll() is None:
    child_proc.kill()
shutil.rmtree(_TMP, ignore_errors=True)
if FAILURES:
    print(f"\nFAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
    sys.exit(1)
print("\nOK: 真杀进程重启演练通过（done 不重放 / 未提交作废 / 在途按租约补投一次 / 恢复证据齐全）")
