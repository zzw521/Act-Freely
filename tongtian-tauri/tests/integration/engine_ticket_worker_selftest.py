#!/usr/bin/env python3
"""
工单级执行体运行时（缺陷 D-48.1）自测

锁住三条口径，防止「文档说有工单执行体、代码里只是个共享目录的并发」的假落地：

1. 独立目录：每工单一个 worker-<工单id摘要> 目录，同 id 幂等（换标题不裂目录）、
   任何标题都拼不出目录穿越；
   prepare 只跑一次，prepare 失败收走半成品目录并给出中文原因。
2. 独立检查点：检查点键按 Assignment 命名；换一份领取就是另一份键；
   finished 删磁盘检查点，dropped / lost 保留（exit_payload 写进去）。
3. 槽位并发 + 人用预留槽：Agent 侧占满上限时，人起新 worker 仍进得来（预留槽）；
   已在跑的 worker 收唤醒不占新槽（验收项②：4 个工单占满时人的消息仍能立即唤醒）；
   被槽位拒绝的唤醒不留目录——先问槽位、再动磁盘。

离线运行：不联网、不起服务；TONGTIANWORK_HOME 与工作区根都指向临时目录，
不触碰用户数据。纯 stdlib，无 agentscope/fastapi 依赖。

用法: python tests/integration/engine_ticket_worker_selftest.py
"""

import os
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 数据根整体重定向：检查点与 worker 目录都落进临时目录
_TMP = Path(tempfile.mkdtemp(prefix="tt-ticket-worker-selftest-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")

sys.path.insert(0, str(PROJECT_ROOT / "engine"))

FAILURES: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


import checkpoint_runtime as cr  # noqa: E402
import ticket_worker_runtime as twr  # noqa: E402


# ── 1. 目录命名与 prepare ─────────────────────────────────────

name_a = twr.worker_dir_name("tk-0001", "整理季度报告 Q3")
name_b = twr.worker_dir_name("tk-0001", "整理季度报告 Q3")
name_c = twr.worker_dir_name("tk-0002", "整理季度报告 Q3")
name_retitle = twr.worker_dir_name("tk-0001", "换了措辞的同一单")
check("同 id 同标题目录名幂等", name_a == name_b)
check("同 id 换标题仍是同一个目录", name_a == name_retitle)
check("不同 id 不撞车", name_a != name_c)
check("目录名带 worker- 前缀", name_a.startswith("worker-"))

evil_path_checked = twr.worker_dir_name("tk-evil", "../../../etc/passwd")
check("恶意标题拼不进目录名（名字只由 id 决定）", "/" not in evil_path_checked and "\\" not in evil_path_checked and ".." not in evil_path_checked)

ws = twr.TicketWorkspace(root=Path(_TMP) / "workers")
evil_path = ws.path_for("tk-evil", "../../../etc/passwd")
check("恶意标题落不进根外路径", ws.root.resolve() in evil_path.resolve().parents)

calls: list = []
path1, err1 = ws.prepare("tk-0001", "整理季度报告 Q3", prepare_fn=lambda p: calls.append(p))
check("首次 prepare 建目录成功", err1 is None and path1.is_dir())
check("prepare 回调真的跑了一次", calls == [path1])
marker = path1 / twr.PREPARE_MARKER
check("prepare 留痕写进标记文件", marker.exists())
path2, err2 = ws.prepare("tk-0001", "换个标题也无所谓", prepare_fn=lambda p: calls.append(p))
check("重复 prepare 幂等且不再跑回调", err2 is None and len(calls) == 1 and path2.name == path1.name)


def boom(_p: Path) -> None:
    raise RuntimeError("git worktree 挂了")


path3, err3 = ws.prepare("tk-0003", "will fail", prepare_fn=boom)
check("prepare 失败给出中文原因", err3 is not None and "准备失败" in err3, str(err3))
check("prepare 失败不收尾的半成品目录不留", not (path3 / twr.PREPARE_MARKER).exists())

# ── 2. 检查点键按 Assignment ──────────────────────────────────

k1 = twr.checkpoint_task_id("as-100")
k2 = twr.checkpoint_task_id("as-200")
check("检查点键按 Assignment 分开", k1 != k2)
check("键带 worker- 前缀便于排查", k1.startswith("worker-"))
try:
    twr.checkpoint_task_id("")
    check("空 Assignment id 拒绝生成键", False)
except ValueError:
    check("空 Assignment id 拒绝生成键", True)

# ── 3. 槽位池与预留槽 ─────────────────────────────────────────

pool = twr.SlotPool(agent_slots=2, human_extra=1)
ok1, _ = pool.try_acquire("t1", "agent")
ok2, _ = pool.try_acquire("t2", "agent")
ok3, deny3 = pool.try_acquire("t3", "agent")
ok4, _ = pool.try_acquire("t3h", "human")
ok5, deny5 = pool.try_acquire("t4h", "human")
check("Agent 占满自己的上限", ok1 and ok2)
check("Agent 挤不进人用预留槽", not ok3 and "预留" in deny3, deny3)
check("人能进预留槽", ok4)
check("全满时人也要排队（诚实拒绝，不抢别人的槽）", not ok5 and "排队" in deny5, deny5)
idem, _ = pool.try_acquire("t1", "agent")
check("同键重复 acquire 幂等", idem)
pool.release("t1")
ok6, _ = pool.try_acquire("t5", "agent")
check("release 后 Agent 槽回来了", ok6)
bad, deny_bad = pool.try_acquire("t6", "carrier-pigeon")
check("未知来源被拒并说明只认 human/agent", not bad and "human" in deny_bad)

# ── 4. 注册表：唤醒不占新槽、被拒不落盘 ───────────────────────

reg_ws = twr.TicketWorkspace(root=Path(_TMP) / "workers4")
reg = twr.TicketWorkerRegistry(agent_slots=2, human_extra=1, workspace=reg_ws)

r1 = reg.start_or_wake(ticket_id="tk-a", title="A 单", assignment_id="as-a1", message="开工", origin="agent")
r2 = reg.start_or_wake(ticket_id="tk-b", title="B 单", assignment_id="as-b1", message="开工", origin="agent")
check("前两单按 Agent 槽起来", r1["ok"] and r2["ok"] and r1["delivered"] == "started")

w_a = reg.start_or_wake(ticket_id="tk-a", assignment_id="as-ignored", message="人的追加要求", origin="human")
check("活 worker 收唤醒走既有绑定、不占新槽", w_a["ok"] and w_a["delivered"] == "existing-worker" and w_a["assignmentId"] == "as-a1", str(w_a))
check("唤醒进了收件箱", w_a["queuedWakes"] == 2)

r3 = reg.start_or_wake(ticket_id="tk-c", title="C 单", assignment_id="as-c1", message="开工", origin="agent")
check("Agent 上限已满时被诚实拒绝", not r3["ok"] and "预留" in r3["reason"], r3.get("reason", ""))
check("被拒的唤醒不留目录", not (reg_ws.root / twr.worker_dir_name("tk-c", "C 单") / twr.PREPARE_MARKER).exists())

r4 = reg.start_or_wake(ticket_id="tk-d", title="D 单", assignment_id="as-d1", message="人盯着这单", origin="human")
check("人用预留槽起得了新 worker", r4["ok"] and r4["workspaceDir"], str(r4))

# 验收项②的完整口径：4 单并行（2 Agent + 1 人起 + 池子全满）时唤醒仍然全进得来
w_all = [reg.start_or_wake(ticket_id=t, assignment_id="as-x", message="再推一把", origin=o)
         for t, o in [("tk-a", "human"), ("tk-b", "agent"), ("tk-d", "human")]]
check("槽位占满时每条唤醒都投递成功", all(x["ok"] and x["delivered"] == "existing-worker" for x in w_all), str(w_all))

items = reg.drain_inbox("tk-a")
check("收件箱按到达顺序整体交付", [it["text"] for it in items] == ["开工", "人的追加要求", "再推一把"], str(items))
check("drain 之后收件箱清空", reg.drain_inbox("tk-a") == [])

# ── 5. 结束语义：finished 删检查点，dropped / lost 留证据 ─────

# checkpoint_runtime.load_checkpoint 认「messages 必须是列表」，缺了会被当损坏删掉；
# 工单侧的检查点载荷必须带上这个字段（空列表=没有对话消息），这是对真实契约的尊重，
# 不是为了测试放宽断言。
cr.save_checkpoint(r1["checkpointKey"], {"messages": [], "snapshot": {"memo": "as-a1 跑到一半"}})
check("先写一份检查点作为前提", cr.load_checkpoint(r1["checkpointKey"]) is not None)
e1 = reg.end_assignment("tk-a", "finished", reason="干完了")
check("finished 结束删磁盘检查点", e1["ok"] and e1["checkpointDeleted"] and cr.load_checkpoint(r1["checkpointKey"]) is None)

cr.save_checkpoint(r2["checkpointKey"], {"messages": [], "snapshot": {"memo": "as-b1 被打断"}})
e2 = reg.end_assignment("tk-b", "dropped", reason="人要改范围", exit_payload={"eventSeq": 7})
check("dropped 保留检查点", e2["ok"] and not e2["checkpointDeleted"])
check("exit_payload 写进了保留的检查点", (reg.checkpoint_of("tk-b") or {}).get("workerExit", {}).get("eventSeq") == 7, str(reg.checkpoint_of("tk-b")))

re2 = reg.end_assignment("tk-b", "exploded")
check("发明第四种结束态被拒绝", not re2["ok"] and "只接受" in re2["reason"])
re3 = reg.end_assignment("tk-zzz", "lost")
check("没有活绑定的工单结束被诚实拒绝", not re3["ok"])

# 换一份领取 → 检查点键换新，旧的 dropped 证据不会被当恢复点误用
r5 = reg.start_or_wake(ticket_id="tk-b", title="B 单", assignment_id="as-b2", message="按新授权重开", origin="human")
check("重开走新 Assignment", r5["ok"] and r5["delivered"] == "started" and r5["assignmentId"] == "as-b2")
check("新检查点键与旧的不同", r5["checkpointKey"] != r2["checkpointKey"])
check("新键下没有旧恢复点", cr.load_checkpoint(r5["checkpointKey"]) is None and reg.checkpoint_of("tk-b") is None)

snap = reg.snapshot()
check("快照带槽位与 worker 两条线", "slots" in snap and len(snap["workers"]) == 3, str(snap)[:200])
check("快照里 worker 记录绑定与目录", all(w["dirName"].startswith("worker-") for w in snap["workers"]))

# ── 收尾 ──────────────────────────────────────────────────────

import shutil

shutil.rmtree(_TMP, ignore_errors=True)
if FAILURES:
    print(f"\nFAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
    sys.exit(1)
print("\nOK: 工单级执行体运行时全部检查通过")
