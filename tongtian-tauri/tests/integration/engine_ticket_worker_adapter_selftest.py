#!/usr/bin/env python3
"""
工单执行体适配器（缺陷 D-48.5b 非竞争半边）自测

钉住的是「接线之前不该由模型/账本含糊掉」的四件事：

1. **读取分页预算**：正文默认 4000、上限 8000 单元；列表默认 20、上限 50 项。
   越界必须**如实带话**（改了哪个数、还剩多少），不能默默按默认——模型看不到
   自己被截断，下一轮就会以为文件就这么长。
2. **执行体工具面只有 6 个**，且拒绝发生在**进账本之前**：未知工具、非法外发
   kind、缺 sessionKey、空正文，都不该占一条队列记录，也不该静默丢掉——对话里
   要留下一句原话，下一轮的模型才改得对。
3. **等待靠排队不靠轮询**：shell 里的 sleep / Start-Sleep / timeout /t / ping -t
   一律拒，**解释器内联代码里的 `time.sleep(20)` / `setTimeout(…)` 一样拒**（D-48.5f：
   换个写法还是「占着槽位睡」），`background=true` 也不豁免（作业没结束槽位就不还）；
   理由要点名「让本轮结束、由唤醒再拉起」；拦的是睡等**写法**，不是跑得久——`npm run build`
   这类真活放行，`cat sleep-diag.log` 这类同名字串不误伤。模型没宣告 finished 就按
   running（保住槽位与检查点），不猜「大概做完了」。
4. **外发真的走既有通道**：`im_channels` 的 `runtime.channel.send(creds, 会话, 正文)`
   是唯一出口；通道不支持 / 没配凭据 / 超时都回中文理由给账本退避，绝不假装送达。

离线运行：不联网、不起服务；`TONGTIANWORK_HOME` 指向临时目录，模型通道、IM 通道、
进程句柄全是假件（`make_loop_bridge` 那一条用真实事件循环线程，但里面跑的是假 send）。
纯 stdlib（im_channels 只在真实 send_fn 被调用时才 import，自测不触发）。

用法: python tests/integration/engine_ticket_worker_adapter_selftest.py
"""

import asyncio
import json
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

_TMP = Path(tempfile.mkdtemp(prefix="tt-ticket-adapter-selftest-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")

sys.path.insert(0, str(PROJECT_ROOT / "engine"))

FAILURES: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


import ticket_worker_adapter as ta  # noqa: E402
import checkpoint_runtime as cr  # noqa: E402
import ticket_outbox_runtime as tor  # noqa: E402
import ticket_recovery_runtime as trr  # noqa: E402
import ticket_worker_runtime as twr  # noqa: E402

# ── 1. 读取分页预算：越界如实带话 ─────────────────────────────

check("未声明请求量取默认 4000", ta.clamp_units(None, default=4000, maximum=8000, what="读取单元") == (4000, ""))
check("非整数请求退回默认并留话", ta.clamp_units("很多", default=4000, maximum=8000, what="读取单元")[1] != "")
check("零与负数不认（退回默认并留话）", ta.clamp_units(0, default=4000, maximum=8000, what="读取单元")[0] == 4000 and ta.clamp_units(-5, default=4000, maximum=8000, what="读取单元")[1] != "")
over = ta.clamp_units(99999, default=4000, maximum=8000, what="读取单元")
check("超上限按 8000 截断且点名要分页", over == (8000, over[1]) and "分页" in over[1], repr(over))
under = ta.clamp_units(500, default=4000, maximum=8000, what="读取单元")
check("低于默认按请求给（不自作主张放大）", under[0] == 500 and "500" in under[1], repr(under))

big = "甲" * 10_000
cut = ta.budgeted_read(big, 3000)
check("正文按预算截断", cut["text"] == big[:3000] and cut["truncated"] and cut["totalUnits"] == 10_000)
check("截断话术报出剩余量", "还有 7000 个单元" in cut["note"], cut["note"])
check("单元口径写明是字符不是 token", "单元=字符" in ta.budgeted_read(big, 20_000)["note"], ta.budgeted_read(big, 20_000)["note"])
short = ta.budgeted_read("很短", None)
check("没超限就不谎称截断", not short["truncated"] and short["text"] == "很短")

lst = ta.budgeted_list([f"n{i}" for i in range(120)], 30)
check("列表按请求给 30 项并报总数", len(lst["items"]) == 30 and lst["total"] == 120 and lst["truncated"])
check("列表话术点名还剩多少", "还有 90 项没列" in lst["note"], lst["note"])
check("列表上限 50 项封顶", len(ta.budgeted_list([1] * 300)["items"]) == ta.LIST_DEFAULT_ITEMS)
check("列表显式超上限被夹到 50", ta.budgeted_list([1] * 300, 999)["count"] == 50)

# 分页那一半（D-48.7）：预算话术让模型「需要更多就带 start 接着读」，这条路必须真存在
page1 = ta.budgeted_read(big, 3000)
page2 = ta.budgeted_read(big, 3000, 3000)
check("第二页给的是接着的那一段（不是同一前缀再发一遍）",
      page2["text"] == big[3000:6000] and page2["start"] == 3000,
      repr({k: page2[k] for k in ("start", "units", "truncated")}))
check("截断话术点名下一段从哪读（给的 start 可直接抄）", "start=3000" in page1["note"], page1["note"])
paged = ""
cursor = 0
pages = 0
while True:
    got = ta.budgeted_read(big, 3000, cursor)
    paged += got["text"]
    pages += 1
    if not got["truncated"]:
        break
    cursor += len(got["text"])
check("按话术翻页能把原文一页不落读全（分页读不等于漏读）", paged == big and pages == 4, repr({"chars": len(paged), "pages": pages}))
tail_page = ta.budgeted_read(big, 3000, 9000)
check("读到末尾不再谎称还有", tail_page["text"] == big[9000:] and not tail_page["truncated"] and "后面还有" not in tail_page["note"], repr(tail_page["note"]))
both = ta.budgeted_read(big, 99999, 2000)
check("start 与预算互不干扰：超量请求仍按上限夹、偏移照给", both["units"] == 8000 and both["text"] == big[2000:10000], repr({k: both[k] for k in ("units", "start")}))
past_end = ta.budgeted_read(big, 100, 99_999)
check("start 超正文长度如实说没内容可给（不是回空串当读完了）",
      past_end["text"] == "" and "没有内容可给" in past_end["note"], repr(past_end["note"]))
check("start 缺省就是从头（不给也读得到）", ta.clamp_start(None) == (0, "") and ta.clamp_start("") == (0, ""))
check("start 认不出来按 0 执行并带话（不默默改模型的意图）",
      ta.clamp_start("第二段")[0] == 0 and "从头开始" in ta.clamp_start("第二段")[1], repr(ta.clamp_start("第二段")))
check("负 start 同样带话", ta.clamp_start(-3)[0] == 0 and "从头开始" in ta.clamp_start(-3)[1], repr(ta.clamp_start(-3)))

# ── 2. shell 等待类命令拒绝 ───────────────────────────────────

for probe, label in (("sleep 30", "sleep"), ("Start-Sleep -Seconds 5", "Start-Sleep"), ("timeout /t 20", "timeout /t"), ("ping -t 127.0.0.1", "ping -t")):
    verdict = ta.guard_shell_command(probe)
    check(f"{label} 被拒且给出排队口径", not verdict["ok"] and "等待靠排队" in verdict["reason"], verdict["reason"])
check("正常命令放行", ta.guard_shell_command("git status")["ok"])
check("带 sleep 字样的路径名不误伤", ta.guard_shell_command("cat sleep-diag.log")["ok"])
check("空命令被拒", not ta.guard_shell_command("   ")["ok"])

# D-48.5f：解释器内联代码里的等待也是「占着槽位睡」，不因换了 -c / -e 写法放过
for probe, label in (
    ('python -c "import time;time.sleep(20)"', "time.sleep(20)"),
    ('python -c "import asyncio;asyncio.run(asyncio.sleep(9))"', "asyncio.sleep"),
    ('node -e "setTimeout(()=>{},20000)"', "setTimeout"),
    ('node -e "setInterval(tick,1000)"', "setInterval"),
    ('ruby -e "sleep(5)"', "ruby sleep(5)"),
    ("perl -e 'select(undef,undef,undef,0.5)'", "perl select(undef…)"),
    ("sleep 3 && usleep(100000)", "usleep(…)"),
    ('jshell -q -r "Thread.sleep(3000)"', "Thread.sleep"),
    ('ping -n 1 127.0.0.1 > nul', "ping 回环当 sleep"),
    ('bash -c "for i in 1 2 3; do sleep 2; done"', "循环里嵌的 sleep"),
    ("wsl sleep 3", "wsl sleep"),
):
    verdict = ta.guard_shell_command(probe)
    check(f"内联/别名等待被拦：{label}", not verdict["ok"] and "不许用 sleep 占着槽位等" in verdict["reason"], verdict["reason"])
check("拦的是「睡等写法」不是「跑得久」：真慢的活放行", all(ta.guard_shell_command(c)["ok"] for c in ("npm run build", "timeout 30 python long.py", "ping -c 4 8.8.8.8", "python -m http.server 8000", "docker compose up -d --build")), [c for c in ("npm run build", "timeout 30 python long.py", "ping -c 4 8.8.8.8", "python -m http.server 8000", "docker compose up -d --build") if not ta.guard_shell_command(c)["ok"]])
check("同名字串与选项不误伤", all(ta.guard_shell_command(c)["ok"] for c in ("python sleep.py", "pytest tests/test_sleep_timer.py -q", 'git commit -m "fix sleep bug"', "mysql --sleep 2")), [c for c in ("python sleep.py", "pytest tests/test_sleep_timer.py -q", 'git commit -m "fix sleep bug"', "mysql --sleep 2") if not ta.guard_shell_command(c)["ok"]])
check("拒绝理由点名「转后台不是不占槽」（挡住 background 绕行）", "background" in ta.guard_shell_command("sleep 60")["reason"], ta.guard_shell_command("sleep 60")["reason"])
check("边界如实：不叫 sleep 的阻塞本层判不出来（Event().wait() 放行）", ta.guard_shell_command('python -c "import threading; threading.Event().wait()"')["ok"], "这条是已知边界，不是漏网：判定只看命令字符串里的睡等写法")
check("系统提示把新口径告模型（内联写法与转后台都点名）", "time.sleep" in ta.worker_system_prompt(goal="g", workspace_dir="w") and "转 background 也不是不占槽" in ta.worker_system_prompt(goal="g", workspace_dir="w"), ta.worker_system_prompt(goal="g", workspace_dir="w")[:400])
check("工具面声明同步收窄（run_shell 预算写明内联也拦）", "解释器内联" in ta.tool_specs()[3]["budget"], ta.tool_specs()[3]["budget"])

# ── 3. 工具面只有 6 个 ────────────────────────────────────────

check("执行体工具面收窄到 6 个", len(ta.WORKER_TOOLS) == 6 and len(set(ta.WORKER_TOOLS)) == 6)
specs = ta.tool_specs()
check("工具声明与工具面一一对应", [s["name"] for s in specs] == list(ta.WORKER_TOOLS))
send_spec = next(s for s in specs if s["name"] == "send_to_session")
check("外发工具只声明三种会话 kind", all(k in send_spec["budget"] for k in ("reply", "relay", "notify")) and "launch/deliver_input" in send_spec["budget"])
check("工具声明把「那两个是引擎的动作」写进预算文案", "引擎对 worker 的动作" in send_spec["budget"], send_spec["budget"])
check("系统提示同步告知模型 launch/deliver_input 不可发", "launch` / `deliver_input` 不是你能发的" in ta.worker_system_prompt(goal="g", workspace_dir="w"))
prompt = ta.worker_system_prompt(goal="打包发版", workspace_dir="worker-ab12", machine="win-a", budget_hint="恢复注记：先查目录")
for needle in ("打包发版", "worker-ab12", "win-a", "4000", "20", "等待靠排队", "finish_work", "事务提交之后", "恢复注记：先查目录"):
    check(f"系统提示含「{needle}」", needle in prompt)

# ── 4. 调用分类：拒绝发生在进账本之前 ─────────────────────────

v = ta._classify_call({"tool": "browse_web", "args": {}}, default_session_key="slack:C1")
check("不在 6 个工具里的调用被拒", v["channel"] == "reject" and "browse_web" in v["reason"])
v = ta._classify_call({"tool": "send_to_session", "args": {"kind": "carrier-pigeon", "sessionKey": "slack:C1", "text": "hi"}}, default_session_key="")
check("非法外发 kind 被拒且不落队列", v["channel"] == "reject" and "carrier-pigeon" in v["reason"])
for worker_kind in ("launch", "deliver_input"):
    v = ta._classify_call({"tool": "send_to_session", "args": {"kind": worker_kind, "sessionKey": "slack:C1", "text": "开工"}}, default_session_key="")
    check(f"账本认识的「{worker_kind}」对模型仍被拒（那是引擎对 worker 的动作）",
          v["channel"] == "reject" and "引擎对 worker 的动作" in v["reason"] and worker_kind in v["reason"], v["reason"])
check("拒绝理由把替代路径说清（走领取与 submit/重启补查，不是往会话发启动指令）",
      "submit" in v["reason"] and "重启补查" in v["reason"], v["reason"])
v = ta._classify_call({"tool": "send_to_session", "args": {"kind": "deliver_input", "sessionKey": "slack:C1", "text": "x"}}, default_session_key="")
check("被拒的 worker kind 不进保序组（否则会同名挡住后续 reply）", v["channel"] == "reject" and "deliver_input" in v["reason"] and "保序" in v["reason"], v["reason"])
v = ta._classify_call({"tool": "send_to_session", "args": {"kind": "reply", "text": "hi"}}, default_session_key="")
check("缺 sessionKey 的外发被拒", v["channel"] == "reject" and "sessionKey" in v["reason"])
v = ta._classify_call({"tool": "send_to_session", "args": {"kind": "reply", "sessionKey": "slack:C1", "text": "   "}}, default_session_key="")
check("空正文的外发被拒", v["channel"] == "reject" and "为空" in v["reason"])
v = ta._classify_call({"tool": "send_to_session", "args": {"kind": "relay", "text": "进度 50%"}, "name": "x"}, default_session_key="dingtalk:conv-9")
check("缺省会话键被用上", v["channel"] == "send" and v["send"]["sessionKey"] == "dingtalk:conv-9" and v["send"]["kind"] == "relay")
v = ta._classify_call({"tool": "finish_work", "args": {"summary": "已发布 v0.2"}}, default_session_key="")
check("finish_work 带出 summary", v["channel"] == "finish" and v["summary"] == "已发布 v0.2")
j1 = ta._classify_call({"tool": "run_shell", "args": {"command": "make package", "background": True}}, default_session_key="")
j2 = ta._classify_call({"tool": "run_shell", "args": {"command": "make package", "background": True}}, default_session_key="")
check("后台作业的 id 由命令摘要决定（跨进程稳定）", j1["channel"] == "job" and j1["job"]["jobId"] == j2["job"]["jobId"])
check("后台作业描述取命令本身（不是工具名）", j1["job"]["description"].startswith("make package"))
check("后台 sleep 在分类阶段就被拒", ta._classify_call({"tool": "run_shell", "args": {"command": "sleep 600", "background": True}}, default_session_key="")["channel"] == "reject")

# ── 5. execute_turn：多轮工具往返与结局口径 ───────────────────

SCRIPT = [
    {"text": "先看目录", "calls": [{"tool": "list_dir", "args": {"path": "."}}, {"tool": "send_to_session", "args": {"kind": "relay", "sessionKey": "slack:C1", "text": "进度 50%"}}]},
    {"text": "顺手试个不存在的工具", "calls": [{"tool": "browse_web", "args": {"url": "https://example.com"}}]},
    {"text": "收工", "calls": [{"tool": "finish_work", "args": {"summary": "已发布 v0.2"}}]},
]
asked: list = []


def fake_model(payload: dict) -> dict:
    asked.append(payload)
    return SCRIPT[min(len(asked) - 1, len(SCRIPT) - 1)]


turn_fn = ta.make_execute_turn(fake_model, goal="打包发版", default_session_key="slack:C1", list_dir=lambda path, limit: {"items": ["a.txt", "b.txt"]})
snapshot = {"dirName": "worker-ab12cd", "machine": "win-a", "recoveryNote": "", "ticketId": "tk-1", "assignmentId": "as-1", "checkpointKey": "k"}
turn = turn_fn(snapshot, "本轮收到的唤醒：\n1. 继续", [{"role": "user", "content": "历史一句话"}])
check("回合成功并收口 finished", turn["ok"] and turn["outcome"] == "finished" and turn["reason"] == "已发布 v0.2", repr({k: turn[k] for k in ("ok", "outcome", "reason")}))
check("外发被翻成账本认识的 send（kind/sessionKey/payload）", turn["sends"] == [{"kind": "relay", "sessionKey": "slack:C1", "payload": "进度 50%", "dedupeKey": ""}], repr(turn["sends"]))
check("被拒动作留在 notes 里", any("browse_web" in n for n in turn["adapterNotes"]), repr(turn["adapterNotes"]))
check("三次往返都有反馈进对话", sum(1 for m in turn["messages"] if m.get("role") == "tool") == 3, repr([m.get("role") for m in turn["messages"]]))
check("目录工具的真实结果进了对话", any("a.txt" in (m.get("content") or "") for m in turn["messages"]))
check("请求里带上收窄后的工具面与系统提示", asked[0]["tools"] == list(ta.WORKER_TOOLS) and "worker-ab12cd" in asked[0]["system"])
check("历史被原样接在前面", asked[0]["messages"][0]["content"] == "历史一句话")
check("本轮唤醒作为最后一条 user 进去", asked[0]["messages"][1]["role"] == "user" and "继续" in asked[0]["messages"][1]["content"])

# 未宣告 finished = 还在跑
turn2 = ta.make_execute_turn(lambda p: {"text": "还没查完", "calls": []}, goal="g")({"dirName": "d"}, "唤醒", [])
check("模型没宣告完成就按 running", turn2["ok"] and turn2["outcome"] == "running" and "仍在进行" in turn2["reason"], repr({k: turn2[k] for k in ("outcome", "reason")}))
check("running 轮一条也不外发", turn2["sends"] == [] and turn2["jobs"] == [])

# 第四种结局不采信（在适配器这一层就拒）
turn3 = ta.make_execute_turn(lambda p: {"text": "", "calls": [], "outcome": "mostly-done"}, goal="g")({"dirName": "d"}, "唤醒", [])
check("未知结局在适配器就被判失败", not turn3["ok"] and "mostly-done" in turn3["reason"] and turn3["sends"] == [], repr({k: turn3[k] for k in ("ok", "reason")}))

# 模型通道抛错
turn4 = ta.make_execute_turn(lambda p: (_ for _ in ()).throw(RuntimeError("上游 502")), goal="g")({"dirName": "d"}, "唤醒", [
    {"role": "user", "content": "旧历史"}])
check("模型调用失败按回合失败处理", not turn4["ok"] and "上游 502" in turn4["reason"] and turn4["sends"] == [])
check("失败轮仍把历史带回（检查点不该凭空少一段）", any(m.get("content") == "旧历史" for m in turn4["messages"]))

# 通道已经判死的调用（带 rejectReason）：不重判、不补全、不进账本（D-48.7 接线时补的半边）
write_seen: list = []
reject_plan = [
    {"text": "试着写一笔", "calls": [
        {"tool": "write_file", "args": {}, "rejectReason": "参数不是合法 JSON（可能被输出上限截断）：请按工具声明重新给一次完整参数。"},
        {"args": {"kind": "reply", "sessionKey": "slack:C1", "text": "这条根本不该被执行"}, "rejectReason": "这条调用没有工具名，无法判断要做什么。"},
    ]},
    {"text": "收工", "calls": [{"tool": "finish_work", "args": {"summary": "坏调用一条也没执行"}}]},
]
reject_asked: list = []


def reject_model(payload: dict) -> dict:
    got = reject_plan[min(len(reject_asked), len(reject_plan) - 1)]
    reject_asked.append(payload)
    return got


reject_turn = ta.make_execute_turn(
    reject_model,
    goal="g",
    default_session_key="slack:C1",
    write_file=lambda path, content: write_seen.append((path, content)) or {"reason": "写了"},
)({"dirName": "worker-rej"}, "唤醒", [])
reject_blob = json.dumps([m.get("content") for m in reject_turn["messages"]], ensure_ascii=False)
check("通道判死的 write_file 没碰到工具实现（截断的参数不是一次真写入）", write_seen == [] and reject_turn["ok"] is True, repr({"write": write_seen, "ok": reject_turn["ok"]}))
check("带 rejectReason 的外发不进副作用账本（不变成一条待发）", reject_turn["sends"] == [], repr(reject_turn["sends"]))
check("通道原话进 notes（不改判、不重写成适配器自己的理由）",
      any("不是合法 JSON" in n for n in reject_turn["adapterNotes"]) and any("没有工具名" in n for n in reject_turn["adapterNotes"]),
      repr(reject_turn["adapterNotes"]))
check("下一轮对话拿到的就是那句原话（模型才改得对）", "被拒：参数不是合法 JSON" in reject_blob, repr(reject_blob[-200:]))
check("没有工具名那条没被猜成别的工具跑", "已记入副作用账本" not in reject_blob and "已写入" not in reject_blob, repr(reject_blob[-200:]))
check("两条坏调用两轮就收口（没被重判烧满 8 轮工具往返）",
      len(reject_asked) == 2 and reject_turn["outcome"] == "finished", repr({"rounds": len(reject_asked), "outcome": reject_turn["outcome"]}))

# 不收敛的模型：一直要工具
loops = {"n": 0}


def runaway(payload: dict) -> dict:
    loops["n"] += 1
    return {"text": "", "calls": [{"tool": "list_dir", "args": {"path": "."}}]}


turn5 = ta.make_execute_turn(runaway, goal="g", list_dir=lambda p, n: {"items": []}, max_tool_rounds=3)({"dirName": "d"}, "唤醒", [])
check("工具往返有上限，不无界烧轮次", not turn5["ok"] and loops["n"] == 3 and "3 轮" in turn5["reason"], repr({"n": loops["n"], "reason": turn5["reason"]}))

# ── 6. send_dispatch：与真账本联动 ────────────────────────────

sent: list = []
ob = tor.TicketOutbox(path=_TMP / "outbox.json")
dispatch = ta.make_send_dispatch(lambda kind, target, text: sent.append((kind, target, text)))
tx = "tx-1"
ob.begin(tx)
enq = ob.enqueue(kind="reply", ticket_id="tk-1", session_key="slack:C01", payload="交付说明", tx_id=tx, dedupe_key="d-1")
check("入队时一条也没发出", enq["ok"] and sent == [], repr(sent))
ob.commit(tx)
summary = ob.run_pending(dispatch)
check("提交后才真发，且落到 (通道, 会话, 正文)", sent == [("slack", "C01", "交付说明")] and summary["succeeded"] == 1, repr({"sent": sent, "summary": summary}))
check("done 记录不再重放", ob.run_pending(dispatch)["dispatched"] == 0 and len(sent) == 1)

sent2: list = []
ob2 = tor.TicketOutbox(path=_TMP / "outbox2.json")
ob2.begin("tx-2")
enq2 = ob2.enqueue(kind="notify", ticket_id="tk-2", session_key="dingtalk:conv-7", payload="开工了", tx_id="tx-2")
rec2 = enq2["record"]["id"]
fail_dispatch = ta.make_send_dispatch(None)
ob2.commit("tx-2")
summary2 = ob2.run_pending(fail_dispatch)
check("通道未接线时如实失败并退避", summary2["failed"] == 1 and summary2["dispatched"] == 1 and sent2 == [], repr(summary2))
check("失败原因写的是「尚未接线」而不是假装送达", "尚未接线" in (ob2.record_view(rec2) or {}).get("error", ""), repr(ob2.record_view(rec2)))

bad = ta.make_send_dispatch(lambda k, t, x: sent2.append((k, t, x)))
try:
    bad({"kind": "reply", "sessionKey": "没有冒号的键", "payload": {"text": "hi", "data": None}})
    check("坏 sessionKey 被拒", False, "没抛错")
except ta.DeliveryError as exc:
    check("坏 sessionKey 被拒并给出形状口径", "通道:会话id" in str(exc), str(exc))
try:
    bad({"kind": "reply", "sessionKey": "slack:C1", "payload": {"text": "  ", "data": None}})
    check("空正文在外发层再挡一次", False, "没抛错")
except ta.DeliveryError as exc:
    check("空正文在外发层再挡一次", "正文为空" in str(exc), str(exc))

# ── 7. 真实通道的口径（假 runtime，一次 HTTP 也不发） ─────────

class FakeChannel:
    kind = "slack"
    label = "Slack"
    supported = True

    def __init__(self):
        self.calls: list = []

    def unsupported_reason(self):
        return ""

    async def send(self, creds, conversation_id, text):
        self.calls.append((creds, conversation_id, text))
        return True


class FakeRuntime:
    def __init__(self, channel, creds):
        self.channel = channel
        self._creds = creds

    def creds(self):
        return self._creds


chan = FakeChannel()
runtimes = {"slack": FakeRuntime(chan, {"xoxb": "t"})}
im_send = ta.make_im_send_fn(runtime_of=lambda kind: runtimes[kind], run_coro=lambda coro: asyncio.new_event_loop().run_until_complete(coro))
im_send("slack", "C0123", "你好")
check("投递走的是 channel.send(creds, 会话, 正文)", chan.calls == [({"xoxb": "t"}, "C0123", "你好")], repr(chan.calls))

chan2 = FakeChannel()
chan2.supported = False
runtimes["broke"] = FakeRuntime(chan2, {"a": 1})
try:
    ta.make_im_send_fn(runtime_of=lambda kind: runtimes[kind], run_coro=lambda c: asyncio.new_event_loop().run_until_complete(c))("broke", "C2", "x")
    check("通道未接入时拒发并说明原因", False, "没抛错")
except ta.DeliveryError as exc:
    check("通道未接入时拒发并说明原因", "尚未接入引擎侧长连接" in str(exc), str(exc))

runtimes["noauth"] = FakeRuntime(FakeChannel(), {})
try:
    ta.make_im_send_fn(runtime_of=lambda kind: runtimes[kind], run_coro=lambda c: asyncio.new_event_loop().run_until_complete(c))("noauth", "C3", "x")
    check("没配凭据拒发", False, "没抛错")
except ta.DeliveryError as exc:
    check("没配凭据拒发", "没配置凭据" in str(exc), str(exc))


def boom_runtime(kind):
    raise KeyError(kind)


try:
    ta.make_im_send_fn(runtime_of=boom_runtime)("slack", "C4", "x")
    check("通道解析失败也回中文理由", False, "没抛错")
except ta.DeliveryError as exc:
    check("通道解析失败也回中文理由", "不存在或没起来" in str(exc), str(exc))

check("真实 im_channels 未被拉进自测（不发网络）", "im_channels" not in sys.modules, str([m for m in sys.modules if "im_channels" in m]))

# ── 8. 跨循环桥：worker 同步循环里驱动引擎事件循环 ────────────

loop = asyncio.new_event_loop()
started = threading.Event()


def drive():
    asyncio.set_event_loop(loop)
    started.set()
    loop.call_soon_threadsafe(lambda: None)
    loop.run_forever()


threading.Thread(target=drive, daemon=True).start()
started.wait(2)
bridge = ta.make_loop_bridge(loop)


async def slow_ok():
    await asyncio.sleep(0.01)
    return "ok"


async def slow_boom():
    await asyncio.sleep(0.5)
    return "never"


check("桥接把协程跑成同步结果", bridge(slow_ok()) == "ok")
try:
    bridge(slow_boom(), timeout=0.05)
    check("投递超时要打断而不是挂住槽位", False, "没抛错")
except ta.DeliveryError as exc:
    check("投递超时要打断而不是挂住槽位", "已取消" in str(exc), str(exc))
time.sleep(0.6)  # 让被取消的协程在循环里走完取消流程，不给自测输出留 pending task 噪声
loop.call_soon_threadsafe(loop.stop)

# ── 9. 作业表与 kill_fn（假 popen，不起真进程） ───────────────

class FakeProc:
    def __init__(self, argv, boom=False):
        self.argv = argv
        self.terminated = False
        self._boom = boom

    def terminate(self):
        if self._boom:
            raise OSError("拒绝访问")
        self.terminated = True

    def wait(self, timeout=None):
        return 0


spawned: list = []


def fake_popen(argv, cwd=None):
    proc = FakeProc(argv, boom="uncloseable" in " ".join(argv))
    spawned.append(proc)
    return proc


table = ta.JobProcessTable(popen=fake_popen)
r = table.spawn("job-a", ["make", "package"])
check("登记作业拿到句柄并回填描述", r["ok"] and r["description"] == "make package", repr(r))
check("重复起同 id 作业被拒", not table.spawn("job-a", ["make", "package"])["ok"])
check("空 jobId 不收", not table.spawn("", ["x"])["ok"])
check("kill_fn 杀掉并清表", table.kill_fn({"jobId": "job-a"}) is True and spawned[0].terminated)
try:
    table.kill_fn({"jobId": "job-a"})
    check("句柄已不在表里要如实抛（不假装杀过）", False, "没抛错")
except ta.DeliveryError as exc:
    check("句柄已不在表里要如实抛（不假装杀过）", "没有进程句柄" in str(exc), str(exc))
table.spawn("job-b", ["uncloseable"])
try:
    table.kill_fn({"jobId": "job-b"})
    check("杀不掉要抛中文理由给看门狗", False, "没抛错")
except ta.DeliveryError as exc:
    check("杀不掉要抛中文理由给看门狗", "终止作业" in str(exc), str(exc))
check("起进程失败在 spawn 就回原因", not ta.JobProcessTable(popen=lambda argv, cwd=None: (_ for _ in ()).throw(OSError("找不到程序"))).spawn("job-c", ["x"])["ok"])

# 作业真的起进程：execute_turn 与进程表接起来
job_table = ta.JobProcessTable(popen=fake_popen)
before = len(spawned)
job_turn = ta.make_execute_turn(
    lambda p: {"text": "先打包", "calls": [{"tool": "run_shell", "args": {"command": "npm run build", "background": True}}], "outcome": "running"},
    goal="打包",
    processes=job_table,
)({"dirName": "worker-j"}, "唤醒", [])
check("后台作业经进程表起了进程（且只起一次）并进作业清单", len(job_turn["jobs"]) == 1 and len(spawned) == before + 1 and spawned[-1].argv == ["npm", "run", "build"], repr({"jobs": job_turn["jobs"], "spawned": len(spawned) - before}))
check("作业 id 由命令摘要决定（与进程表同一把键）", job_table.spawn(job_turn["jobs"][0]["jobId"], ["dup"])["ok"] is False)

boom_table = ta.JobProcessTable(popen=lambda argv, cwd=None: (_ for _ in ()).throw(OSError("找不到程序")))
boom_turn = ta.make_execute_turn(
    lambda p: {"text": "先打包", "calls": [{"tool": "run_shell", "args": {"command": "npm run build", "background": True}}], "outcome": "running"},
    goal="打包",
    processes=boom_table,
)({"dirName": "worker-j"}, "唤醒", [])
check("起不来的作业不登记（不给看门狗计时的幽灵）", boom_turn["jobs"] == [] and any("后台作业没起来" in n for n in boom_turn["adapterNotes"]), repr(boom_turn["adapterNotes"]))
no_table = ta.make_execute_turn(
    lambda p: {"text": "先打包", "calls": [{"tool": "run_shell", "args": {"command": "npm run build", "background": True}}], "outcome": "running"},
    goal="打包",
)({"dirName": "worker-j"}, "唤醒", [])
check("没配进程表时也进作业清单并标「没起进程」（否则看门狗根本不知道它存在）",
      len(no_table["jobs"]) == 1 and no_table["jobs"][0]["processStarted"] is False, repr(no_table["jobs"]))
check("没配进程表时如实留话（不谎称已起进程）", any("没配进程表" in n for n in no_table["adapterNotes"]), repr(no_table["adapterNotes"]))
ghost_clock = {"t": 0.0}
no_table_to_watchdog = trr.JobWatchdog(clock=lambda: ghost_clock["t"])
for _job in no_table["jobs"]:
    no_table_to_watchdog.register(_job["jobId"], description=_job["description"], ticket_id="tk-notable")
ghost_killed = no_table_to_watchdog.tick(kill_fn=ta.JobProcessTable(popen=fake_popen).kill_fn, now=ghost_clock["t"] + trr.JOB_TIMEOUT_SECONDS + 1)
check("这类登记到点照样被处置（不悄悄从计时里消失）", len(ghost_killed) == 1, repr(ghost_killed))
check("处置报告说清「没有进程句柄」（不谎称已经把进程中止了）",
      ghost_killed and ghost_killed[0]["killOk"] is False and "没有进程句柄" in ghost_killed[0]["killError"], repr(ghost_killed))

# 长命令：给人看的描述被 120 字截过，起进程必须用完整那一份（D-48.7 e2e 撞出来的真坑）
py_path = sys.executable.replace("\\", "/")
long_command = f"'{py_path}' -c 'import threading;threading.Event().wait()' " + "A" * 200
check("这条测试命令本身确实超过 120 字（不然钉不住截断）", len(long_command) > 120, str(len(long_command)))
long_before = len(spawned)
long_turn = ta.make_execute_turn(
    lambda p: {"text": "起个长命令", "calls": [{"tool": "run_shell", "args": {"command": long_command, "background": True}}], "outcome": "running"},
    goal="打包",
    processes=ta.JobProcessTable(popen=fake_popen),
)({"dirName": "worker-long"}, "唤醒", [])
check("起进程用的是完整命令（argv 拆到结尾，不是截过的描述）",
      len(spawned) == long_before + 1 and spawned[-1].argv[-1].endswith("A" * 200), repr(spawned[-1].argv[-1][-12:] if len(spawned) > long_before else spawned[-1].argv))
check("给人看的描述仍按 120 字截（进对话与报告的口径不变）",
      len(long_turn["jobs"]) == 1 and long_turn["jobs"][0]["description"] == long_command[:120]
      and long_turn["jobs"][0]["command"] == long_command, repr(long_turn["jobs"][0]["description"][-20:]))
check("回合没被这条长命令烧掉（起进程成功、结局仍是 running）",
      long_turn["ok"] is True and long_turn["outcome"] == "running" and len(long_turn["jobs"]) == 1,
      repr({k: long_turn[k] for k in ("ok", "outcome", "reason")}))

# 引号不成的命令：起不来就如实报，不把整轮判死（一轮失败=副作用全回滚）
bad_turn = ta.make_execute_turn(
    lambda p: {"text": "少个引号", "calls": [{"tool": "run_shell", "args": {"command": 'git commit -m "没写完', "background": True}}], "outcome": "running"},
    goal="打包",
    processes=ta.JobProcessTable(popen=fake_popen),
)({"dirName": "worker-bad"}, "唤醒", [])
check("拆不出 argv 的后台命令不炸整轮（回合仍 ok、作业不登记）",
      bad_turn["ok"] is True and bad_turn["jobs"] == [], repr({k: bad_turn[k] for k in ("ok", "outcome", "reason")}))
check("拆不出 argv 也点名进 notes（模型与人都看得见这条没起）",
      any("拆不出 argv" in n for n in bad_turn["adapterNotes"]), repr(bad_turn["adapterNotes"]))
same_round = {"n": 0}


def once_with_outcome(payload: dict) -> dict:
    same_round["n"] += 1
    return {"text": "起个包就等它", "outcome": "running", "calls": [{"tool": "run_shell", "args": {"command": "npm run build", "background": True}}]}


declared_turn = ta.make_execute_turn(once_with_outcome, goal="打包")({"dirName": "d"}, "唤醒", [])
check("同一回合既下指令又报结局时结局优先（不再多要一轮）", same_round["n"] == 1 and declared_turn["outcome"] == "running", repr({"n": same_round["n"], "outcome": declared_turn["outcome"]}))


clock = {"t": 1_000_000.0}
watchdog = trr.JobWatchdog(clock=lambda: clock["t"])
watchdog.register("job-x", description="打包安装", ticket_id="tk-9")
clock["t"] += trr.JOB_TIMEOUT_SECONDS + 1
killed = watchdog.tick(kill_fn=table.kill_fn)
check("到点作业交给 kill_fn 且失败如实标注", killed and killed[0]["killOk"] is False, repr(killed))
check("kill 失败进了报告行（不静默）", any("需要人工处理" in line for line in watchdog.kill_report_lines(killed)), repr(watchdog.kill_report_lines(killed)))

# ── 10. build_dispatcher：一轮真跑通（假模型 + 假通道） ───────

delivered: list = []
plan = [
    {"text": "收到，先回一句", "calls": [{"tool": "send_to_session", "args": {"kind": "reply", "sessionKey": "slack:C1", "text": "开始处理", "dedupeKey": "r-1"}}]},
    {"text": "完成", "calls": [{"tool": "finish_work", "args": {"summary": "已回复"}}]},
]
calls = {"n": 0}


def model(payload: dict) -> dict:
    got = plan[min(calls["n"], len(plan) - 1)]
    calls["n"] += 1
    return got


parts = ta.build_dispatcher(
    model_call=model,
    send_fn=lambda kind, target, text: delivered.append((kind, target, text)) or True,
    goal="回复客户",
    workspace=twr.TicketWorkspace(_TMP / "workers"),
)
built = parts["dispatcher"]
built.outbox = tor.TicketOutbox(path=_TMP / "wired-outbox.json")
parts["outbox"] = built.outbox
res = built.wake_and_run(ticket_id="tk-w", title="客户咨询", assignment_id="as-w1", message="帮我回复客户")
check("装配好的 Dispatcher 一轮跑通", res["ok"] and res["ended"] == "finished", repr({k: res[k] for k in ("ok", "stage", "ended", "reason")}))
check("外发经适配器落到通道", delivered == [("slack", "C1", "开始处理")], repr(delivered))
check("收口后槽位归还", "tk-w" not in parts["registry"].pool.snapshot()["occupied"], repr(parts["registry"].pool.snapshot()["occupied"]))
check("收口后检查点删除", cr.load_checkpoint(res["checkpointKey"]) is None)
check("工具声明随装配一起交出（接线侧不必重述）", len(parts["toolSpecs"]) == 6 and "processes" in parts)
snapshot = built.state_of("tk-w")
check("收口后对外状态是 free", snapshot["external"] == "free", repr(snapshot))

# 接管者口（D-48.7 服务装配靠这两个把「本轮工单目录」注进工具实现）
try:
    ta.build_dispatcher(goal="什么都不给")
    check("model_call 与 execute_turn 都不给就当场报错（不留一台永远起不了回合的装配）", False, "没抛异常")
except ValueError as exc:
    check("model_call 与 execute_turn 都不给就当场报错（不留一台永远起不了回合的装配）",
          "至少要给一个" in str(exc) and "execute_turn" in str(exc), str(exc))

taken: list = []


def injected_turn(worker_snapshot: dict, prompt: str, history: list) -> dict:
    taken.append({"snapshot": dict(worker_snapshot), "prompt": prompt, "history": list(history)})
    return {"ok": True, "outcome": "finished", "reason": "接管者这一轮", "messages": list(history), "sends": [], "jobs": []}


injected_parts = ta.build_dispatcher(
    execute_turn=injected_turn,
    send_fn=lambda kind, target, text: True,
    workspace=twr.TicketWorkspace(_TMP / "workers-injected"),
    inbox_journal=False,
)
injected_res = injected_parts["dispatcher"].wake_and_run(ticket_id="tk-inj", title="接管单", assignment_id="as-inj", message="唤醒正文")
check("给了 execute_turn 就用它造回合（不再由 model_call 拼一层，也不两边都跑）",
      len(taken) == 1 and injected_res["ok"] is True and injected_res["ended"] == "finished",
      repr({k: injected_res.get(k) for k in ("ok", "stage", "ended", "reason")}))
check("接管者拿得到本轮工单快照与原始唤醒（工单目录只能从这里推出来）",
      taken[0]["snapshot"].get("ticketId") == "tk-inj" and "唤醒正文" in taken[0]["prompt"], repr(taken[0]["snapshot"]))
check("接管口的装配也交出六件齐（与 model_call 那一路同面）",
      set(injected_parts) >= {"dispatcher", "registry", "outbox", "ledger", "watchdog", "processes", "inboxJournal", "toolSpecs"},
      repr(sorted(injected_parts)))

given_table = ta.JobProcessTable(popen=fake_popen)
table_parts = ta.build_dispatcher(
    model_call=model,
    send_fn=lambda kind, target, text: True,
    workspace=twr.TicketWorkspace(_TMP / "workers-table"),
    processes=given_table,
    inbox_journal=False,
)
check("传进来的进程表就是 parts 交出的那一张（接管者之后拿到的是同一个对象）", table_parts["processes"] is given_table)
check("看门狗到点处置走的就是这张表（接管了表却没接管 kill_job 就杀不掉）",
      table_parts["dispatcher"].kill_job.__self__ is given_table, repr(table_parts["dispatcher"].kill_job))
default_parts = ta.build_dispatcher(
    model_call=model,
    send_fn=lambda kind, target, text: True,
    workspace=twr.TicketWorkspace(_TMP / "workers-default"),
    inbox_journal=False,
)
check("不传进程表时给一张真的（默认不是 None，也不与别人共用）",
      isinstance(default_parts["processes"], ta.JobProcessTable) and default_parts["processes"] is not given_table,
      repr(type(default_parts["processes"]).__name__))

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} 项")
    for name in FAILURES:
        print(f"  - {name}")
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(1)
print("OK: 工单执行体适配器自测全部通过")
shutil.rmtree(_TMP, ignore_errors=True)
