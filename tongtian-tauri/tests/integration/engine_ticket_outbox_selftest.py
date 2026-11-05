#!/usr/bin/env python3
"""
工单耐久副作用队列（缺陷 D-48.2）自测

锁住三条硬口径，防止「文档说外发耐久，代码里只是个内存队列」的假落地：

1. 提交后才执行：queued 永不外发；commit 后才可能发；rollback 永久作废；
   未知事务拒绝入队（忘 begin 就不该发出去）；重启时未提交的按作废处理。
2. 幂等：同 dedupeKey 的活记录不重排——重启后重放 enqueue 不会二次发送；
   只有 aborted / failed 之后允许显式重排。
3. 同工单同会话严格保序：ORDERED kind 组内队头放行；失败退避重投；
   到上限置 failed 并**停住整组**——放行只有 retry_failed（重投）与
   resolve_block(skip)（显式跳过）两个留痕出口，没有静默越序。

外加：kind 并发上限、指数退避、在途租约回收（done 永不重放）、
单文件账本的重启耐久、坏账本改名让路、终态剪枝（failed 不剪）。

离线运行：不联网、不起服务；TONGTIANWORK_HOME 指向临时目录，dispatch 是
记录调用顺序的假函数，不触碰用户数据。纯 stdlib。

用法: python tests/integration/engine_ticket_outbox_selftest.py
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

_TMP = Path(tempfile.mkdtemp(prefix="tt-ticket-outbox-selftest-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")

sys.path.insert(0, str(PROJECT_ROOT / "engine"))

FAILURES: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


import ticket_outbox_runtime as tor  # noqa: E402

CLOCK = {"now": 1_000_000.0}


def make_outbox(name: str, **kw) -> tor.TicketOutbox:
    return tor.TicketOutbox(_TMP / "ledgers" / name, clock=lambda: CLOCK["now"], **kw)


def text_of(record: dict) -> str:
    return record["payload"]["text"]


# ── 1. 入队校验：不在表里的 kind / 缺事务 / 超限载荷都拒 ──────

ob = make_outbox("validate.json")
r = ob.enqueue(kind="carrier-pigeon", ticket_id="tk-1", tx_id="tx-a")
check("未知 kind 被拒并列出合法集合", not r["ok"] and "carrier-pigeon" in r["reason"], str(r))
r = ob.enqueue(kind="reply", ticket_id="", tx_id="tx-a")
check("空工单 id 被拒", not r["ok"] and "ticketId" in r["reason"])
r = ob.enqueue(kind="reply", ticket_id="tk-1", tx_id="")
check("缺事务 id 被拒（提交后才执行的口径）", not r["ok"] and "事务" in r["reason"])
r = ob.enqueue(kind="reply", ticket_id="tk-1", tx_id="tx-nope")
check("未知事务拒绝入队", not r["ok"] and "未知事务" in r["reason"])
check("空事务 id 不能 begin", not ob.begin("")["ok"])
check("begin 正常开事务", ob.begin("tx-a")["ok"])
check("同名事务不能重复 begin", not ob.begin("tx-a")["ok"])
r = ob.enqueue(kind="reply", ticket_id="tk-1", payload="x" * 30000, tx_id="tx-a")
check("超限载荷被拒（队列不是第二存储）", not r["ok"] and "落盘" in r["reason"], str(r.get("reason", ""))[:60])

# ── 2. 提交后才执行 / 回滚永不执行 ────────────────────────────

calls: list = []


def dispatch_ok(record):
    calls.append(record)
    return True


e1 = ob.enqueue(kind="reply", ticket_id="tk-1", session_key="sess-A", payload="第一条", tx_id="tx-a")
check("enqueue 返回 queued 记录", e1["ok"] and e1["record"]["status"] == "queued")
s = ob.run_pending(dispatch_ok)
check("未提交时一条都不外发", s["dispatched"] == 0 and calls == [], str(s))
c = ob.commit("tx-a")
check("commit 报告放行条数", c["ok"] and c["released"] == 1, str(c))
s = ob.run_pending(dispatch_ok)
check("提交之后才真的发出去", s["dispatched"] == 1 and len(calls) == 1 and text_of(calls[0]) == "第一条", str([text_of(x) for x in calls]))
ob.begin("tx-b")
ob.enqueue(kind="reply", ticket_id="tk-1", session_key="sess-A", payload="回滚项", tx_id="tx-b")
rb = ob.rollback("tx-b")
check("rollback 作废未发的命令", rb["ok"] and rb["aborted"] == 1)
calls.clear()
s = ob.run_pending(dispatch_ok)
check("回滚过的命令永不出队", s["dispatched"] == 0 and calls == [], str(s))
check("回滚后事务名不能 commit", not ob.commit("tx-b")["ok"])

# ── 3. 幂等：活记录挡住重放 ───────────────────────────────────

r2 = ob.enqueue(kind="reply", ticket_id="tk-1", session_key="sess-A", payload="第一条", tx_id="tx-c")
check("重放前事务名已随 commit 失效，直接入队被拒", not r2["ok"])
ob.begin("tx-c")
r2 = ob.enqueue(kind="reply", ticket_id="tk-1", session_key="sess-A", payload="第一条", tx_id="tx-c")
check(
    "同幂等键已有活记录：deduped 且不新增",
    r2["ok"] and r2.get("deduped") and r2["record"]["status"] == "done",
    str(r2.get("note", "")),
)
before = ob.snapshot()["records"]
ob.enqueue(kind="reply", ticket_id="tk-1", session_key="sess-A", payload="第一条", tx_id="tx-c")
check("重放不会堆记录", ob.snapshot()["records"] == before)
ob.rollback("tx-c")
ob.begin("tx-d")
r3 = ob.enqueue(kind="reply", ticket_id="tk-1", session_key="sess-B", payload="回滚后重发", tx_id="tx-d")
check("新会话同载荷是新命令（组键不同，不 dedupe）", r3["ok"] and not r3.get("deduped"), str(r3))
ob.commit("tx-d")
ob.begin("tx-e")
r4 = ob.enqueue(kind="reply", ticket_id="tk-1", session_key="sess-B", payload="回滚后重发", tx_id="tx-e")
check("pending 也算活记录：同键重放被幂等挡住", r4["ok"] and r4.get("deduped"))
ob.rollback("tx-e")

# ── 4. 保序：组内队头放行、failed 停组、skip/retry 留痕 ───────

ob2 = make_outbox("order.json")
order_calls: list = []


def flaky(record):
    order_calls.append(text_of(record))
    return text_of(record) != "一"  # 「一」永远失败，直到被显式跳过


for i, tx in enumerate(["t1", "t2", "t3"]):
    ob2.begin(tx)
    ob2.enqueue(kind="reply", ticket_id="tk-X", session_key="s-1", payload="一二三"[i], tx_id=tx)
    ob2.commit(tx)
# 跨 kind 同组也保序：deliver_input 排在 reply 后就要排在后面
ob2.begin("t4")
ob2.enqueue(kind="deliver_input", ticket_id="tk-X", session_key="s-1", payload="四", tx_id="t4")
ob2.commit("t4")
ob2.begin("t5")
ob2.enqueue(kind="notify", ticket_id="tk-X", payload="旁路通知", tx_id="t5")
ob2.commit("t5")

s = ob2.run_pending(flaky)
check(
    "队头失败时只碰队头，后序不越序（旁路 notify 同轮发出属预期）",
    set(order_calls) == {"一", "旁路通知"},
    str(order_calls),
)
check("旁路 notify 不受保序组影响", "旁路通知" in order_calls, str(order_calls))
order_calls.clear()
s = ob2.run_pending(flaky)
check("退避窗口内队头不重投", s["dispatched"] == 0 and order_calls == [], str(s))
CLOCK["now"] += 30  # 越过退避
s = ob2.run_pending(flaky)
check("窗口过后队头按序重投，仍然不越序", order_calls == ["一"], str(order_calls))
# 打到上限置 failed
for _ in range(8):
    CLOCK["now"] += 60
    ob2.run_pending(flaky)
head_id = next(r["id"] for r in ob2.snapshot()["orderedGroups"]["tk-X::s-1"] if text_of(r) == "一")
head = ob2.record_view(head_id)
check("到 max_attempts 置 failed 终态", head and head["status"] == "failed", str(head))
order_calls.clear()
s = ob2.run_pending(flaky)
check("failed 队头停住整组：二三四都发不出去", order_calls == [], str(order_calls))
check("停组原因如实上报（不静默）", any("越序" in x or "停" in x for x in s["reasons"]), str(s["reasons"]))
group = ob2.snapshot()["orderedGroups"]["tk-X::s-1"]
check("后序记录 blockedBy 指向卡点", group[1].get("blockedBy") == head_id, str([g.get("blockedBy") for g in group]))
check("未知记录 id 拒绝重投", not ob2.retry_failed("out-999")["ok"])
rr = ob2.retry_failed(head_id)
check("retry_failed 让 failed 回到 pending", rr["ok"] and rr["record"]["status"] == "pending", str(rr))
CLOCK["now"] += 60
order_calls.clear()
s = ob2.run_pending(flaky)
check("重投后组继续卡（这条故意一直失败）", order_calls == ["一"], str(order_calls))
sb = ob2.resolve_block(head_id, "jump-over")
check("处置只有 skip：其它写法被拒", not sb["ok"] and "skip" in sb["reason"], str(sb))
head = ob2.record_view(head_id)
check("非 failed 状态不给跳过", not ob2.resolve_block(head_id, "skip")["ok"] and head["status"] == "pending")
# 让它重新到达 failed，再走 skip 出口
for _ in range(8):
    CLOCK["now"] += 60
    ob2.run_pending(flaky)
head = ob2.record_view(head_id)
check("重投失败后再次到达 failed 终态", head["status"] == "failed", str(head))
sb = ob2.resolve_block(head_id, "skip")
check("skip 显式跳过并放行后序", sb["ok"] and sb["record"]["status"] == "skipped", str(sb))
CLOCK["now"] += 60
order_calls.clear()
s = ob2.run_pending(flaky)
check("跳过后按原顺序补发（二三四；旁路已完成不重发）", order_calls == ["二", "三", "四"], str(order_calls))

# ── 5. kind 并发上限 ──────────────────────────────────────────

ob3 = make_outbox("kind.json", kind_limits={"notify": 1, "launch": 2})
n_calls: list = []
ob3.begin("k")
for i in range(3):
    ob3.enqueue(kind="notify", ticket_id="tk-K", payload=f"n{i}", tx_id="k")
for i in range(3):
    ob3.enqueue(kind="launch", ticket_id=f"tk-K{i}", payload=f"l{i}", tx_id="k")
ob3.commit("k")


def n_dispatch(record):
    n_calls.append(f"{record['kind']}:{text_of(record)}")
    return True


s = ob3.run_pending(n_dispatch)
check("notify 上限 1：一轮只放行 1 条", "notify:n1" not in n_calls and n_calls.count("notify:n0") == 1, str(n_calls))
check("launch 上限 2：一轮最多 2 条", sum(1 for c in n_calls if c.startswith("launch")) == 2, str(n_calls))
n_calls.clear()
s = ob3.run_pending(n_dispatch)
check("下一轮继续按上限放行", "notify:n1" in n_calls, str(n_calls))
try:
    make_outbox("badkinds.json", kind_limits={"pigeon": 1})
    check("非法 kind_limits 在构造期就拒", False)
except ValueError as exc:
    check("非法 kind_limits 在构造期就拒", "pigeon" in str(exc))

# ── 6. 租约回收：崩溃遗留的在途重投，done 永不重放 ────────────

ob4 = make_outbox("lease.json", lease_seconds=60)
ob4.begin("L")
ob4.enqueue(kind="reply", ticket_id="tk-L", session_key="s-L", payload="在途", tx_id="L")
ob4.enqueue(kind="reply", ticket_id="tk-L", session_key="s-L", payload="已完成", tx_id="L")
ob4.commit("L")
done_calls: list = []
ob4.run_pending(lambda r: (done_calls.append(text_of(r)), True)[1])
check("两条都已发出", done_calls == ["在途", "已完成"], str(done_calls))
# 模拟崩溃：账本里第一条改回 running、租约时间戳调老
data = json.loads(ob4.path.read_text(encoding="utf-8"))
for rec in data["records"]:
    if text_of(rec) == "在途":
        rec["status"] = "running"
        rec["acquiredAtMs"] = int(CLOCK["now"] * 1000) - 90_000
ob4.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
ob4b = tor.TicketOutbox(ob4.path, clock=lambda: CLOCK["now"], lease_seconds=60)
crash_calls: list = []
s = ob4b.run_pending(lambda r: (crash_calls.append((text_of(r), r.get("error", ""))), True)[1])
check("过期租约被回收重投", [t for t, _ in crash_calls] == ["在途"], str(crash_calls))
check("回收原因（租约）写进重投记录", crash_calls and "租约" in crash_calls[0][1], str(crash_calls))
again: list = []
s = ob4b.run_pending(lambda r: (again.append(text_of(r)), True)[1])
check("done 的记录不再重放（重启语义的地基）", again == [], str(again))

# ── 7. 单文件账本的重启耐久 / 未提交事务作废 ──────────────────

ob5 = make_outbox("dur.json")
ob5.begin("D")
ob5.enqueue(kind="reply", ticket_id="tk-D", session_key="s-D", payload="待发", tx_id="D")
ob5.commit("D")
ob5.begin("D2")
ob5.enqueue(kind="reply", ticket_id="tk-D", session_key="s-D", payload="未提交", tx_id="D2")
dur_calls: list = []
ob5b = tor.TicketOutbox(ob5.path, clock=lambda: CLOCK["now"])
ob5b.run_pending(lambda r: (dur_calls.append(text_of(r)), True)[1])
check("重启后 pending 仍在账本里并补发", dur_calls == ["待发"], str(dur_calls))
ob5c = tor.TicketOutbox(ob5.path, clock=lambda: CLOCK["now"])
rec_uncommitted = ob5c.record_view("out-2")
check(
    "重启时 queued 按未提交作废（宁可丢未提交，不发未提交）",
    rec_uncommitted and rec_uncommitted["status"] == "aborted" and "未提交" in rec_uncommitted["error"],
    str(rec_uncommitted),
)
ob5c.begin("X")
r = ob5c.enqueue(kind="reply", ticket_id="tk-D", session_key="s-D", payload="未提交", tx_id="X")
check("aborted 后同载荷重排返回新记录（显式重发，非意外重复）", r["ok"] and not r.get("deduped"), str(r))

# ── 8. 坏账本：改名让路并暴露原因，不在坏文件上追加 ───────────

bad_path = _TMP / "ledgers" / "bad.json"
bad_path.write_text("{ not json", encoding="utf-8")
ob6 = tor.TicketOutbox(bad_path, clock=lambda: CLOCK["now"])
check(
    "坏账本改名保留而非删除",
    any(p.name.startswith("bad.json.corrupt-") for p in bad_path.parent.iterdir()),
    str(list(bad_path.parent.iterdir())),
)
check("loadError 如实暴露", "损坏" in ob6.load_error, ob6.load_error)
ob6.begin("B")
ok_b = ob6.enqueue(kind="reply", ticket_id="tk-B", session_key="s", payload="坏后新发", tx_id="B")
ob6.commit("B")
got: list = []
ob6.run_pending(lambda r: (got.append(text_of(r)), True)[1])
check("让路后队列照常可用", ok_b["ok"] and got == ["坏后新发"], str(got))

# ── 9. 剪枝：终态有上限，failed 是证据不剪 ────────────────────

orig_retain = tor.MAX_RETAINED_FINISHED
tor.MAX_RETAINED_FINISHED = 2
try:
    ob7 = make_outbox("prune.json", max_attempts=2)
    ob7.begin("P")
    for i in range(4):
        ob7.enqueue(kind="notify", ticket_id=f"tk-P{i}", payload=f"p{i}", tx_id="P")
    ob7.commit("P")
    ob7.run_pending(lambda r: True)
    ob7.begin("P2")
    ob7.enqueue(kind="notify", ticket_id="tk-P9", payload="will-fail", tx_id="P2", dedupe_key="fixed-key")
    ob7.commit("P2")
    for _ in range(6):
        CLOCK["now"] += 60
        ob7.run_pending(lambda r: text_of(r) != "will-fail")
    snap = ob7.snapshot()
    check("failed 记录不受剪枝（没发出去的证据必须留着）", snap["statusCounts"].get("failed") == 1, str(snap["statusCounts"]))
    done_like = sum(snap["statusCounts"].get(k, 0) for k in ("done", "aborted", "skipped"))
    check("终态记录按保留上限剪枝（只留最近的）", done_like <= 2, str(snap["statusCounts"]))
finally:
    tor.MAX_RETAINED_FINISHED = orig_retain

# ── 收尾 ──────────────────────────────────────────────────────

shutil.rmtree(_TMP, ignore_errors=True)
if FAILURES:
    print(f"\nFAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
    sys.exit(1)
print("\nOK: 工单耐久副作用队列全部检查通过")
