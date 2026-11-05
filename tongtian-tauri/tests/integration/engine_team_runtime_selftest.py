#!/usr/bin/env python3
"""
多 Agent 团队协作（#148 / D-34）与成员运行时检查点（#149 / D-40.5）自测

锁住 D-34 那五条验收，防止出现「界面宣称有多 Agent 团队，真实任务里只是把名字换了」：

1. 创建含 2 名成员的团队，主持人**一次并行**派工两人（两条派工、各占一个 worker 槽）
2. 两名成员的回复按**说话人**上墙；主持人拿到总结（回叫进的是主持人那张单）
3. 成员各自**检查点独立**（按 `房间~成员` 的工单派生）；重启后未完成任务显示
   `interrupted`，**不标记成功**，并且可以重派（重派时接上旧检查点的注记）
4. **停止只停主持人**：成员仍占着槽位继续跑，之后照常回叫
5. 团队编制**重启后保留**；在途成员既不能被移出、也不能随之删掉团队

外加 #149 的台账口径：成员检查点只做**索引与结论**，正文留在 checkpoints 目录；
`resumable` 由磁盘那份自证，读不到就说读不到；工单自证不符的一份不许接（不把人家的
对话接进这个成员）。

离线运行：不联网、无需 API Key；执行体用真注册表 + 临时数据根，不起模型、不起回合。
fastapi 缺失时端到端分组 SKIP 并以 0 退出。

用法: python tests/integration/engine_team_runtime_selftest.py
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
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

FAILURES: list = []
COUNT = {"n": 0}

try:
    import checkpoint_runtime as ckpt
    import data_paths as dp
    import team_runtime as tm
    import ticket_inbox_runtime as tir
    import ticket_worker_runtime as twr
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: 团队运行时自测不可用（{exc}）")
    sys.exit(0)


def check(name: str, cond: bool, detail: str = "") -> None:
    COUNT["n"] += 1
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def expert(eid: str, name: str, **over) -> dict:
    """可用的假专家条目（注册表归一化前的原始形状）。"""
    item = {
        "id": eid, "name": name, "description": f"{name}的分工",
        "prompt": f"你是{name}", "modelId": "", "modelName": "",
        "skills": [],
    }
    item.update(over)
    return item


HOST_ID = "host"
MEMBER_A = "researcher"
MEMBER_B = "writer"
MEMBER_C = "third"
MEMBER_D = "fourth"


def registry_experts(*, broken: str = "", extra: dict | None = None) -> list[dict]:
    items = [
        expert(HOST_ID, "主持"),
        expert(MEMBER_A, "研究"),
        expert(MEMBER_B, "写作"),
        expert(MEMBER_C, "第三人"),
        expert(MEMBER_D, "第四人"),
    ]
    if broken == "registry":
        return [{"id": "", "name": "缺 id 的专家"}]
    if extra:
        items = [{**i, **extra} for i in items] if extra.get("all") else [
            {**(i if i["id"] != extra.get("for") else {**i, **{k: v for k, v in extra.items() if k not in ("for", "all")}})}
            for i in items
        ]
    return items


def build_registry() -> twr.TicketWorkerRegistry:
    """真注册表 + 真耐久收件箱：派工的落盘与保序走的就是 D-48 验收过那条路径。"""
    return twr.TicketWorkerRegistry(
        agent_slots=3, human_extra=1, inbox_journal=tir.TicketInbox()
    )


def build_runtime(registry=None) -> tm.TeamRuntime:
    return tm.TeamRuntime(workers=registry or build_registry())


def make_team(runtime: tm.TeamRuntime, session: str, *, members=(MEMBER_A, MEMBER_B)):
    res = runtime.create_team(
        {"name": "取材与成稿", "hostExpertId": HOST_ID, "memberExpertIds": list(members)},
        registry_experts(),
    )
    if not res.get("ok"):
        return res
    res["room"] = runtime.open_room(team_id=res["team"]["id"], session_id=session)
    return res


def job_state(runtime: tm.TeamRuntime, job_id: str) -> str:
    job = runtime.jobs.get_job(job_id) or {}
    return str(job.get("state") or "")


# ── 1. 编制校验：不合法就整条拒，不猜、不静默剔人 ──────────────


def group_roster() -> None:
    print("\n── 编制校验 ──")
    rt = build_runtime()
    experts = registry_experts()

    ok = make_team(rt, "s-roster")
    check("编制：两名成员的团队能建出来", bool(ok.get("ok")), str(ok))
    check("编制：主持人带出名字（界面徽章要写得出谁主持）",
          ok["team"]["hostExpertName"] == "主持")

    r = rt.create_team({"name": "没主持人", "memberExpertIds": [MEMBER_A]}, experts)
    check("编制：缺主持人专家直接拒", not r.get("ok") and "主持人" in r.get("reason", ""), str(r))

    r = rt.create_team({"name": "空成员", "hostExpertId": HOST_ID, "memberExpertIds": []}, experts)
    check("编制：零成员的团队拒（没有成员就不叫团队）", not r.get("ok"), str(r))

    r = rt.create_team(
        {"name": "自派", "hostExpertId": HOST_ID, "memberExpertIds": [HOST_ID, MEMBER_A]}, experts)
    check("编制：主持人不能兼任成员（否则他会给自己派工再自己汇总）",
          not r.get("ok") and "主持人" in r.get("reason", ""), str(r))

    r = rt.create_team(
        {"name": "重复", "hostExpertId": HOST_ID, "memberExpertIds": [MEMBER_A, MEMBER_A]}, experts)
    check("编制：成员重复直接拒", not r.get("ok") and "重复" in r.get("reason", ""), str(r))

    r = rt.create_team(
        {"name": "不存在", "hostExpertId": HOST_ID, "memberExpertIds": ["ghost"]}, experts)
    check("编制：不存在的成员直接拒并列出可用专家",
          not r.get("ok") and "ghost" in r.get("reason", ""), str(r))

    seven = [f"m{i}" for i in range(7)]
    many = experts + [expert(x, x) for x in seven]
    r = rt.create_team(
        {"name": "超编", "hostExpertId": HOST_ID, "memberExpertIds": seven}, many)
    check("编制：超过 6 名成员拒（更多成员主持人收不了口）", not r.get("ok"), str(r))

    r = rt.create_team(
        {"name": "坏模型", "hostExpertId": HOST_ID, "memberExpertIds": [MEMBER_A]},
        registry_experts(), experts_error="专家注册表不可读：items 不是数组")
    check("编制：注册表本身坏了的时候不建队（否则编制的合法性无从校验）",
          not r.get("ok") and "注册表" in r.get("reason", ""), str(r))

    r = rt.create_team(
        {"name": "模型不可用", "hostExpertId": HOST_ID, "memberExpertIds": [MEMBER_A]},
        registry_experts(extra={"for": MEMBER_A, "modelError": "密钥缺失"}))
    check("编制：成员绑的模型不可用时拒（不建「看着能派其实起不来」的团队）",
          not r.get("ok") and "不能接活" in r.get("reason", ""), str(r))

    r = rt.create_team(
        {"name": "主持人模型坏", "hostExpertId": HOST_ID, "memberExpertIds": [MEMBER_A]},
        registry_experts(extra={"for": HOST_ID, "modelSecretsStripped": True}))
    check("编制：主持人模型不可用时拒", not r.get("ok") and "主持人" in r.get("reason", ""), str(r))

    # 复用同一个 id 再建一次 = 整体替换（桌面「编辑团队」的形状）
    team_id = ok["team"]["id"]
    before = len(rt.teams.teams())
    r = rt.create_team(
        {"id": team_id, "name": "取材与成稿", "hostExpertId": HOST_ID,
         "memberExpertIds": [MEMBER_A, MEMBER_C]}, experts)
    check("编制：同 id 再建走整体替换，团队数不变",
          r.get("ok") and len(rt.teams.teams()) == before, f"{before} -> {rt.teams.teams()}")
    listed = rt.list_teams()
    check("编制：列出来的成员名单是替换后的",
          [m["id"] for m in listed[0]["members"]] == [MEMBER_A, MEMBER_C], str(listed))


# ── 2. 派工：并行、串行、上限与如实拒绝 ──────────────────────


def group_dispatch() -> None:
    print("\n── 派工（验收①）──")
    rt = build_runtime()
    make_team(rt, "s-dispatch")

    d = rt.dispatch(room="s-dispatch", member_ids=[MEMBER_A, MEMBER_B], brief="各自写两段材料")
    check("派工：一次并行派两人，两条都投出去",
          d.get("ok") and len(d.get("dispatched") or []) == 2, str(d)[:300])
    check("派工：一条也没被拒（槽位够）", not d.get("refused"), str(d.get("refused")))
    slots = sorted(rt.workers.snapshot()["slots"]["occupied"])
    check("派工：两名成员各占一个 worker 槽（不同成员真并行）",
          slots == [f"s-dispatch~{MEMBER_A}", f"s-dispatch~{MEMBER_B}"], str(slots))
    check("派工：派工号一条，两条 job 共用（一次调用一个 dispatchId）",
          len({x["dispatchId"] for x in d["dispatched"]}) == 1)

    dirs = {x["dirName"] for x in d["dispatched"]}
    keys = {x["checkpointKey"] for x in d["dispatched"]}
    check("派工：两名成员目录不同（成员不共享主持人工作区）", len(dirs) == 2, str(dirs))
    check("派工：两名成员检查点键不同（各自可单独重跑）", len(keys) == 2, str(keys))

    d2 = rt.dispatch(room="s-dispatch", member_ids=[MEMBER_A], brief="再补一段")
    one = d2["dispatched"][0]
    check("派工：同一成员的第二次派工并进他当前那份领取（同人串行）",
          one.get("delivered") == "existing-worker", str(one))
    check("派工：同人串行不占新槽（槽位还是那两个）",
          len(rt.workers.snapshot()["slots"]["occupied"]) == 2,
          str(rt.workers.snapshot()["slots"]["occupied"]))
    check("派工：排队那条在工单上带出「排在他当前那件活后面」",
          any("排在" in n for n in d2.get("notes") or []), str(d2.get("notes")))

    d3 = rt.dispatch(room="s-dispatch", member_ids=[MEMBER_A, MEMBER_A], brief="重复点名")
    check("派工：一次调用里重复点名同一个人只派一条（不派两遍同一件事）",
          len(d3.get("dispatched") or []) == 1, str(d3.get("dispatched")))

    d4 = rt.dispatch(room="s-dispatch", member_ids=["ghost"], brief="派给不存在的人")
    check("派工：不在编制里的成员拒，并把 id 原样点出来",
          not d4.get("ok") and "ghost" in d4.get("reason", ""), str(d4))

    d5 = rt.dispatch(room="s-dispatch", member_ids=[MEMBER_A], brief="   ")
    check("派工：说明为空直接拒（成员看不到主持人的对话，说明必须自足）",
          not d5.get("ok") and "自足" in d5.get("reason", ""), str(d5))

    d6 = rt.dispatch(room="s-none", member_ids=[MEMBER_A], brief="没绑房间就派")
    check("派工：会话没绑编制时整次拒（不静默派给「随便一个成员」）",
          not d6.get("ok") and "没有绑定团队" in d6.get("reason", ""), str(d6))

    ids = [f"x{i}" for i in range(7)]
    experts7 = registry_experts() + [expert(i, i) for i in ids]
    r7 = rt.create_team(
        {"name": "七人", "hostExpertId": HOST_ID, "memberExpertIds": ids}, experts7)
    check("派工：七个成员的编制本身就建不出来（上限在编制处挡下）",
          not r7.get("ok"), str(r7))

    # 槽位满：第四条派工如实 refused，不假装在跑。这里必须换一个**还没有活绑定**的成员：
    # 同一成员的第二条派工是并进他当前那份领取（同人串行），本来就不占新槽，测不出槽位满。
    full = build_registry()
    rt_full = tm.TeamRuntime(workers=full)
    make_team_full = rt_full.create_team(
        {"name": "占满", "hostExpertId": HOST_ID,
         "memberExpertIds": [MEMBER_A, MEMBER_B, MEMBER_C, MEMBER_D]}, registry_experts())
    rt_full.open_room(team_id=make_team_full["team"]["id"], session_id="s-full")
    first = rt_full.dispatch(room="s-full", member_ids=[MEMBER_A, MEMBER_B, MEMBER_C], brief="三人各一件")
    check("派工：三人各起一份领取（3 个 Agent 槽用满）",
          len(first.get("dispatched") or []) == 3, str(first)[:200])
    d7 = rt_full.dispatch(room="s-full", member_ids=[MEMBER_D], brief="挤不进的那条")
    check("派工：槽位满时这条记 refused 并把原话回出来（不假装在跑）",
          not d7.get("ok") and len(d7.get("refused") or []) == 1
          and "槽位" in str(d7.get("reason")), str(d7)[:300])
    refused_job = (d7.get("refused") or [{}])[0]
    check("派工：被拒那条在台账里就写 refused（不会重启后被当成在途/中断）",
          refused_job.get("state") == "refused"
          and job_state(rt_full, refused_job.get("jobId", "")) == "refused", str(refused_job)[:250])
    check("派工：被拒那条没有检查点索引（它连一次领取都没占到）",
          not (rt_full.jobs.get_job(refused_job.get("jobId", "")) or {}).get("checkpointKey"),
          str(rt_full.jobs.get_job(refused_job.get("jobId", "")))[:250])
    check("派工：槽位满的原话进了 notes（主持人不用解析结构体也知道为什么没派出去）",
          any("槽位" in n for n in d7.get("notes") or []), str(d7.get("notes")))


# ── 3. 时间线与回叫：谁说的、说没说完 ─────────────────────────


def group_timeline() -> None:
    print("\n── 时间线与回叫（验收②）──")
    rt = build_runtime()
    make_team(rt, "s-line")
    d = rt.dispatch(room="s-line", member_ids=[MEMBER_A, MEMBER_B], brief="分头取材")
    jobs = {x["memberId"]: x for x in d["dispatched"]}

    ev = rt.rooms.events("s-line", limit=50)
    kinds = [e.get("kind") for e in ev]
    check("时间线：建房间与派工都上了墙", "room_opened" in kinds and "dispatch" in kinds, str(kinds))
    dispatch_entry = next((e for e in ev if e.get("kind") == "dispatch"), {})
    check("时间线：派工那条的说话人是主持人（不是引擎自己冒名）",
          dispatch_entry.get("speakerAgentId") == tm.HOST_SPEAKER, str(dispatch_entry))
    check("时间线：派工那条写清了请了谁", "研究" in dispatch_entry.get("text", "")
          and "写作" in dispatch_entry.get("text", ""), str(dispatch_entry))

    r1 = rt.member_reported(room="s-line", member_id=MEMBER_A, job_id=jobs[MEMBER_A]["jobId"],
                            report="材料一：三段要点", ok=True)
    r2 = rt.member_reported(room="s-line", member_id=MEMBER_B, job_id=jobs[MEMBER_B]["jobId"],
                            report="成稿两段", ok=False)
    check("回叫：两名成员各回一条，台账都收口", r1.get("ok") and r2.get("ok"), str(r1)[:200])

    ev2 = rt.rooms.events("s-line", limit=50)
    replies = [e for e in ev2 if e.get("kind") in ("member_reply", "member_failed")]
    check("时间线：成员回复各带自己的说话人（谁说的看得出来）",
          len(replies) == 2 and {e["speakerAgentId"] for e in replies} == {MEMBER_A, MEMBER_B},
          str(replies)[:300])
    check("时间线：回复带出成员名字（气泡上墙不用二次查编制）",
          all(e.get("speakerName") for e in replies), str(replies)[:200])
    failed = next((e for e in replies if e.get("kind") == "member_failed"), {})
    check("时间线：失败那条单独标 kind，正文带「未完成」",
          "未完成" in failed.get("text", ""), str(failed))

    pend = rt.host_pending(room="s-line")
    check("回叫：主持人的待读里有两条（成功与失败都回叫，不静默）",
          pend.get("enabled") and pend.get("count") == 2, str(pend)[:300])
    texts = " ".join(str(w.get("text")) for w in pend.get("pending") or [])
    check("回叫：待读正文里点名是谁回的", "研究" in texts and "写作" in texts, texts[:300])
    check("回叫：失败的回叫说得出「没有做完」", "没有做完" in texts, texts[:300])

    occupied = rt.workers.snapshot()["slots"]["occupied"]
    check("收口：干完的成员那份领取归还槽位（否则三个人就把整池占死）",
          not [k for k in occupied if "~" in k], str(occupied))
    check("回叫：主持人不在跑时回叫不占槽位（槽是留给成员的，回叫只落耐久箱等下一轮）",
          "s-line" not in occupied and r1["callback"].get("delivered") == "journal-only",
          f"{str(occupied)} / {str(r1['callback'])[:160]}")
    check("回叫：这条落进耐久箱的回叫在主持人下一轮读得到（重启也算得到）",
          any("研究" in str(w.get("text")) for w in rt.host_pending(room="s-line")["pending"]),
          str(rt.host_pending(room="s-line"))[:250])

    r3 = rt.member_reported(room="s-line", member_id=MEMBER_A, job_id=jobs[MEMBER_A]["jobId"],
                            report="再说一遍")
    check("回叫：同一条 job 收两次口被拒（台账里必须只有一个「完成」）",
          not r3.get("ok") and "收" in str(r3.get("ledger", {}).get("reason")), str(r3)[:300])

    hint = rt.system_hint(room="s-line")
    check("提示词：绑了编制的房间才出现团队段", "团队编制" in hint, hint[:120])
    check("提示词：把成员名单与「未经验证必须核对」一起给主持人",
          "研究" in hint and "未经验证" in hint, hint[:200])
    check("提示词：没绑编制的会话一个团队字都不提（不承诺不存在的工具）",
          rt.system_hint(room="s-none-here") == "")


# ── 4. 停止只停主持人（验收④）───────────────────────────────


def group_stop() -> None:
    print("\n── 停止（验收④）──")
    rt = build_runtime()
    make_team(rt, "s-stop")
    # 主持人这一轮由执行体那侧领取（桌面上用户发一句话就走这条路）；本层只往他那一份投递，
    # **不替他起新领取**——起一份就占一个 Agent 槽，而那一轮根本不由工单调度器跑。
    host_claim = rt.workers.start_or_wake(
        ticket_id="s-stop", title="主持人本轮", assignment_id="host-round-1",
        message="用户：帮我凑一队人做完这件事", origin="human",
    )
    check("停止：主持人那一轮是执行体那侧的一份领取（本层不代领）",
          host_claim.get("ok") and host_claim.get("delivered") != "existing-worker",
          str(host_claim)[:200])
    before = sorted(rt.workers.snapshot()["slots"]["occupied"])
    host_first = rt.notify_host(room="s-stop", text="先把话头接住")
    check("回叫：主持人有活绑定时唤醒直接进他那一份收件箱（同人同单，不另起领取）",
          host_first.get("ok") and host_first.get("delivered") == "existing-worker",
          str(host_first)[:250])
    check("回叫：这条路一个新槽位都不占（主持人的槽是他自己那一轮领的，不是回叫替他占的）",
          sorted(rt.workers.snapshot()["slots"]["occupied"]) == before,
          f"{before} -> {sorted(rt.workers.snapshot()['slots']['occupied'])}")
    pend1 = rt.host_pending(room="s-stop")
    check("回叫：这条唤醒在耐久收件箱里（返回 ok 之前就落完盘，D-48.6 的口径）",
          pend1.get("count") == 2
          and any("先把话头接住" in str(w.get("text")) for w in pend1.get("pending") or []),
          str(pend1)[:250])
    d = rt.dispatch(room="s-stop", member_ids=[MEMBER_A, MEMBER_B], brief="分头做")
    jobs = {x["memberId"]: x for x in d["dispatched"]}

    st = rt.stop_host_round(room="s-stop")
    check("停止：主持人本轮收口成功", st.get("ok"), str(st)[:200])
    check("停止：范围只写着 host（界面据此决定要不要灰掉成员条）",
          st.get("stoppedScope") == "host", str(st)[:200])
    check("停止：在途成员一条也没被牵连（两名仍在跑）",
          len(st.get("inflightMembers") or []) == 2, str(st)[:200])
    live = sorted(rt.workers.snapshot()["slots"]["occupied"])
    check("停止：成员占的槽位还在（停止不释放别人的活）",
          live == [f"s-stop~{MEMBER_A}", f"s-stop~{MEMBER_B}"], str(live))
    host_worker = rt.workers.worker("s-stop")
    check("停止：主持人那份领取标成 dropped（被停不等于做完，检查点要留着）",
          host_worker is not None and host_worker.ended_status == "dropped",
          str(getattr(host_worker, "ended_status", None)))

    rep = rt.member_reported(room="s-stop", member_id=MEMBER_A, job_id=jobs[MEMBER_A]["jobId"],
                             report="我这份做完了")
    check("停止：成员在主持人被停之后照常回叫", rep.get("ok") and rep["callback"].get("ok"),
          str(rep)[:300])
    check("停止：回叫落进的是主持人那张单（下一轮主持人读得到）",
          rep["callback"].get("ticketId") == "s-stop", str(rep["callback"])[:200])
    kinds = [e.get("kind") for e in rt.rooms.events("s-stop", limit=50)]
    check("停止：主持人被停这件事上了墙（用户看得见停的是谁）", "host_stopped" in kinds, str(kinds))
    check("回叫：主持人这一轮已经停了，回叫不再替他起领取（不占 Agent 槽位）",
          rep["callback"].get("delivered") == "journal-only"
          and "s-stop" not in rt.workers.snapshot()["slots"]["occupied"],
          str(rep["callback"])[:250])
    check("回叫：落在耐久箱里的这一条，主持人下一轮读得到",
          any("研究" in str(w.get("text")) for w in rt.host_pending(room="s-stop")["pending"]),
          str(rt.host_pending(room="s-stop"))[:250])


# ── 5. 在途保护：不能移出、不能删（验收⑤后半）───────────────


def group_inflight_guard() -> None:
    print("\n── 在途保护（验收⑤）──")
    rt = build_runtime()
    created = make_team(rt, "s-guard")
    team_id = created["team"]["id"]
    d = rt.dispatch(room="s-guard", member_ids=[MEMBER_A, MEMBER_B], brief="两人都开工")

    r = rt.amend_members(team_id=team_id, member_ids=[MEMBER_B], experts=registry_experts())
    check("在途：把正在跑的成员移出编制被拒", not r.get("ok"), str(r)[:200])
    check("在途：拒绝理由点名是谁在途（不是泛泛一句「不行」）",
          "研究" in r.get("reason", ""), str(r)[:200])

    r = rt.remove_team(team_id=team_id)
    check("在途：成员正在跑时不能删团队（否则回叫找不到收件人）", not r.get("ok"), str(r)[:250])

    jobs = {x["memberId"]: x for x in d["dispatched"]}
    rt.member_reported(room="s-guard", member_id=MEMBER_A, job_id=jobs[MEMBER_A]["jobId"],
                       report="交回")
    r = rt.end_member_job(room="s-guard", member_id=MEMBER_B, job_id=jobs[MEMBER_B]["jobId"],
                          status="dropped", reason="超时")
    check("在途：另一名下超时收口", r.get("ok"), str(r)[:200])

    r = rt.amend_members(team_id=team_id, member_ids=[MEMBER_A], experts=None)
    check("在途：两人都收口后可以移出（保护不是永久锁死）", r.get("ok"), str(r)[:200])
    check("改编制：只减人不需要专家注册表（编制里那份快照就是当初对着注册表验过的结论）",
          r.get("ok") and [m["id"] for m in r["team"]["members"]] == [MEMBER_A], str(r)[:250])

    r = rt.amend_members(team_id=team_id, member_ids=[MEMBER_A, MEMBER_C], experts=None)
    check("改编制：加新人时没有注册表就拒（不猜这个人存不存在）",
          not r.get("ok") and r.get("code") == "EXPERTS_REQUIRED"
          and MEMBER_C in r.get("reason", ""), str(r)[:250])

    r = rt.amend_members(team_id=team_id, member_ids=[MEMBER_A, MEMBER_C],
                         experts=registry_experts())
    check("改编制：带上注册表就能加人，并逐个验模型可用性",
          r.get("ok") and {m["id"] for m in r["team"]["members"]} == {MEMBER_A, MEMBER_C},
          str(r)[:250])

    r = rt.amend_members(
        team_id=team_id, member_ids=[MEMBER_A, MEMBER_C, MEMBER_D],
        experts=registry_experts(extra={"for": MEMBER_D, "modelError": "密钥缺失"}))
    check("改编制：新加的那个人模型不可用时整条拒（旧名单一个字没改）",
          not r.get("ok") and r.get("code") == "ROSTER_INVALID"
          and {m["id"] for m in (rt.teams.get(team_id) or {}).get("members", [])} == {MEMBER_A, MEMBER_C},
          str(r)[:250])

    r = rt.end_member_job(room="s-guard", member_id=MEMBER_C, job_id="不存在",
                          status="finished", reason="")
    check("收口：有结论的走 member_reported，finished 不许从 end_member 这条路进来",
          not r.get("ok") and "member_reported" in r.get("reason", ""), str(r)[:200])

    r = rt.remove_team(team_id=team_id)
    check("删除：收口后台账不再挡路，团队可删", r.get("ok"), str(r)[:250])
    check("删除：房间绑定一起清掉（不留指向已删团队的绑定）",
          rt.teams.by_room("s-guard") is None, str(rt.teams.room_of_team(team_id)))


# ── 6. 重启：编制留着、在途标中断、可重派（验收③⑤前半）──────


def group_restart() -> None:
    print("\n── 重启（验收③⑤）──")
    registry = build_registry()
    rt = tm.TeamRuntime(workers=registry)
    created = make_team(rt, "s-boot")
    team_id = created["team"]["id"]
    d = rt.dispatch(room="s-boot", member_ids=[MEMBER_A, MEMBER_B], brief="重启前派出去")
    job_a = d["dispatched"][0]["jobId"]
    # 成员跑到一半留下检查点（执行回路每轮末尾就是这么落的）
    key_a = d["dispatched"][0]["checkpointKey"]
    saved, err = ckpt.save_checkpoint(key_a, {
        "messages": [{"role": "user", "content": "干了一半"}],
        "ticketId": d["dispatched"][0]["ticketId"],
        "assignmentId": job_a,
        "resumable": True,
        "reason": "进程结束",
    })
    check("重启：先把成员那一轮的运行时检查点落到磁盘", saved, err)

    # 模拟引擎重启：注册表是空的（内存里的 worker 全没了），三份存储从盘上重读
    new_registry = build_registry()
    rt2 = tm.TeamRuntime(workers=new_registry)
    teams = rt2.list_teams()
    check("重启：团队编制还在（用户不用重新建一队人）",
          len(teams) == 1 and teams[0]["id"] == team_id, str(teams)[:250])
    check("重启：房间绑定还在（会话 id 与编制的对应没丢）",
          teams[0]["room"] == "s-boot", str(teams[0])[:200])

    states = {j["jobId"]: j["state"] for j in rt2.jobs.jobs_for_room("s-boot")}
    check("重启：两条在途派工都标成 interrupted",
          set(states.values()) == {"interrupted"}, str(states))
    check("重启：一条也没被标成 ended/成功（未确认做完过任何事）",
          not any(v in ("ended", "refused") for v in states.values()), str(states))
    check("重启：中断条数自述得出来（界面要写「N 条中断」而不是「0 条失败」）",
          int(rt2.jobs.data.get("restartedInterrupted") or 0) == 2,
          str(rt2.jobs.data.get("restartedInterrupted")))

    carry = rt2.checkpoints.carry_forward(room="s-boot", member_id=MEMBER_A, next_assignment_id="td-next")
    check("重派：接得上中断的那一份（carriedFrom 就是原 job）",
          carry.get("carriedFrom") == job_a, str(carry)[:250])
    check("重派：可续跑由磁盘那份自证（有对话快照才算 resumable）",
          carry.get("resumable") is True, str(carry)[:250])
    check("重派：恢复注记非空，并要求先查现状再重复外部动作",
          bool(carry.get("recoveryNote")) and "先查" in carry["recoveryNote"],
          str(carry.get("recoveryNote"))[:160])

    d2 = rt2.dispatch(room="s-boot", member_ids=[MEMBER_A], brief="接着做")
    one = d2["dispatched"][0]
    check("重派：新那份领取带上了恢复注记（执行体读得到「你接到的是中断的那一份」）",
          one.get("carriedFrom") == job_a and bool(one.get("recoveryNote")), str(one)[:300])
    check("重派：注记来源如实标出（没接调度器就写 fallback，不冒充调度器生成）",
          one.get("carriedFrom") == job_a and one.get("noteSource") == "fallback", str(one)[:300])

    notes = rt2.room_state(room="s-boot")
    check("重派：房间状态里旧 job 仍是 interrupted（新派工不覆盖旧证据）",
          any(j["state"] == "interrupted" for j in notes["jobs"]), str(notes["jobs"])[:300])

    # 没有快照的那一份：只能按新任务做，不许说「接上了」
    carry_none = rt2.checkpoints.carry_forward(room="s-boot", member_id=MEMBER_B, next_assignment_id="td-x")
    check("重派：读不到快照时 resumable=False 且注记说「按新任务从头做」",
          carry_none.get("resumable") is False and "从头做" in carry_none.get("recoveryNote", ""),
          str(carry_none)[:300])

    d3 = rt2.dispatch(room="s-boot", member_ids=[MEMBER_B], brief="重新做")
    b_job = d3["dispatched"][0]
    check("重派：无可恢复快照时不带 carriedFrom（不声称接上了上下文）",
          not b_job.get("carriedFrom") or b_job.get("carriedResumable") is False,
          str(b_job)[:300])


# ── 7. 成员运行时检查点台账（#149 / D-40.5）──────────────────


def group_member_checkpoint() -> None:
    print("\n── 成员运行时检查点 ──")
    rt = build_runtime()
    make_team(rt, "s-cp")
    d = rt.dispatch(room="s-cp", member_ids=[MEMBER_A, MEMBER_B], brief="各做一件")
    a, b = d["dispatched"][0], d["dispatched"][1]

    check("台账：投递成功后按实际领取建索引",
          (rt.jobs.get_job(a["jobId"]) or {}).get("checkpointState") == "inflight",
          str(rt.jobs.get_job(a["jobId"]))[:250])
    check("台账：索引用的键就是执行体落盘那一份（不另算一次摘要）",
          (rt.jobs.get_job(a["jobId"]) or {}).get("checkpointKey") == a["checkpointKey"],
          str(rt.jobs.get_job(a["jobId"]))[:250])

    st = rt.checkpoints.status_of("s-cp", MEMBER_A)
    check("台账：还没跑过一轮的成员如实说「磁盘上没有这份」",
          st.get("exists") is False and st.get("resumable") is False, str(st)[:250])
    check("台账：读不到就说读不到，而不是「可续跑」" ,
          "从未落过" in st.get("reason", ""), st.get("reason", ""))

    ckpt.save_checkpoint(a["checkpointKey"], {
        "messages": [{"role": "user", "content": "一半"}],
        "ticketId": a["ticketId"], "assignmentId": a["jobId"],
        "resumable": True, "reason": "被停", "toolRecords": {"sig": {"tool": "write_text_file"}},
    })
    st2 = rt.checkpoints.status_of("s-cp", MEMBER_A)
    check("台账：落了盘之后计数报得出来（正文不上屏）",
          st2.get("exists") and st2.get("snapshotMessages") == 1 and st2.get("sideEffects") == 1,
          str(st2)[:250])
    check("台账：in-flight 的那一份不当作「可接的旧检查点」（carry 只认中断留证的）",
          rt.checkpoints.carry_forward(room="s-cp", member_id=MEMBER_A,
                                       next_assignment_id="td-y").get("carriedFrom") == "",
          str(rt.checkpoints.carry_forward(room="s-cp", member_id=MEMBER_A,
                                          next_assignment_id="td-y"))[:250])

    rep = rt.member_reported(room="s-cp", member_id=MEMBER_A, job_id=a["jobId"], report="做完了")
    job_a = rt.jobs.get_job(a["jobId"]) or {}
    check("台账：finished 收口后索引标 released",
          job_a.get("checkpointState") == "released", str(job_a)[:250])
    check("台账：删档是执行体干的，台账只转述回执（不自己删也不假装删）",
          job_a.get("checkpointDeletedByWorker") is True and rep["workerEnd"].get("checkpointDeleted") is True,
          str(job_a)[:250])
    check("台账：finished 之后磁盘上确实读不到那一份（TTL/删除口径同 checkpoint_runtime）",
          ckpt.load_checkpoint(a["checkpointKey"]) is None)

    # dropped：留证据，下一次派工能接上
    rt.workers.end_assignment(b["ticketId"], "dropped", reason="超时")
    rt.checkpoints.release(room="s-cp", member_id=MEMBER_B, assignment_id=b["jobId"],
                           status="dropped", end_result={"ok": True, "checkpointKey": b["checkpointKey"],
                                                         "checkpointDeleted": False})
    job_b = rt.jobs.get_job(b["jobId"]) or {}
    check("台账：dropped 之后索引标 interrupted（这才是下一次能接的那一份）",
          job_b.get("checkpointState") == "interrupted", str(job_b)[:250])

    # 归属自证不符：另一条工单的检查点不许接进这个成员
    other_key = twr.checkpoint_task_id("别的派工")
    ckpt.save_checkpoint(other_key, {
        "messages": [{"role": "user", "content": "别人的"}],
        "ticketId": "别的工单", "assignmentId": "别的派工", "resumable": True, "reason": "",
    })
    rt.jobs.set_fields(b["jobId"], checkpointKey=other_key, checkpointState="interrupted",
                       checkpointTicketId=b["ticketId"])
    carry = rt.checkpoints.carry_forward(room="s-cp", member_id=MEMBER_B, next_assignment_id="td-z")
    check("台账：检查点自证的工单与成员工单不符时不接（不把别人的对话接进来）",
          not carry.get("carriedFrom") and "不符" in carry.get("reason", ""), str(carry)[:300])

    view = rt.checkpoints.room_view("s-cp")
    check("台账：房间概览按成员列（界面一行一个人）",
          view.get("ok") and len(view.get("members") or []) == 2, str(view)[:250])
    body = json.dumps(view, ensure_ascii=False)
    check("台账：概览里没有对话正文（只报计数与状态）",
          "别人的" not in body and "一半" not in body, body[:250])

    # 同一成员并进别人的领取时，索引要挂在实际那一份上
    d2 = rt.dispatch(room="s-cp", member_ids=[MEMBER_B], brief="第一件")
    j1 = d2["dispatched"][0]["jobId"]
    d3 = rt.dispatch(room="s-cp", member_ids=[MEMBER_B], brief="第二件（排队）")
    j2 = d3["dispatched"][0]["jobId"]
    job2 = rt.jobs.get_job(j2) or {}
    check("台账：排队的第二条标出「并进了哪份领取」（它自己没有独立检查点）",
          job2.get("checkpointSharedWith") == j1, str(job2)[:300])
    st3 = rt.checkpoints.status_of("s-cp", MEMBER_B)
    check("台账：状态视图把这条「不是独立检查点」说出来",
          "并进" in st3.get("note", ""), str(st3)[:300])

    # 没注入键算式就不许建索引（不许抄第二份摘要规则）
    bare = ckpt.MemberCheckpointLedger(rt.jobs)
    r = bare.record_assignment(room="s-cp", member_id=MEMBER_B, assignment_id=j1)
    check("台账：没注入检查点键算式时拒着写（不自己抄一份第二摘要）",
          not r.get("ok") and "checkpoint_task_id" in r.get("reason"), str(r)[:250])

    # 空装配：没接台账的房间不自称有检查点
    rt_off = tm.TeamRuntime(workers=build_registry(), member_checkpoints=False)
    make_team(rt_off, "s-off")
    check("台账：明确关掉台账时房间状态里没有 checkpoint 字段（不半接）",
          "checkpoint" not in (rt_off.room_state(room="s-off")["members"][0]),
          str(rt_off.room_state(room="s-off")["members"][0])[:250])


# ── 8. 复用而非另造：装配口径 ────────────────────────────────


def group_reuse() -> None:
    print("\n── 复用 D-47/D-48 那套执行体 ──")
    try:
        tm.TeamRuntime(workers=None)  # type: ignore[arg-type]
        built = False
    except ValueError:
        built = True
    check("装配：没有执行体注册表就建不出团队运行时（派工会只是台账上一行字）", built)

    reg = build_registry()
    rt = tm.TeamRuntime(workers=reg)
    make_team(rt, "s-reuse")
    d = rt.dispatch(room="s-reuse", member_ids=[MEMBER_A], brief="一条唤醒")
    tid = d["dispatched"][0]["ticketId"]
    journal = reg.inbox_journal
    pend = journal.pending_wakes_for_ticket(tid)
    check("投递：派工的唤醒进了**同一个**耐久收件箱（不是本层另开的队列）",
          len(pend) == 1 and pend[0].get("text"), str(pend)[:250])
    check("投递：收件箱文件就在数据根（与工单层同一份账本）",
          (dp.ticket_inbox_file().exists()), str(dp.ticket_inbox_file()))

    # 人用预留槽：Agent 把槽占满之后，人那条唤醒仍然进得来（继承 D-48 的槽位口径）
    full_rt = tm.TeamRuntime(workers=build_registry())
    r = full_rt.create_team(
        {"name": "占满", "hostExpertId": HOST_ID,
         "memberExpertIds": [MEMBER_A, MEMBER_B, MEMBER_C]}, registry_experts())
    full_rt.open_room(team_id=r["team"]["id"], session_id="s-slot")
    full_rt.dispatch(room="s-slot", member_ids=[MEMBER_A, MEMBER_B, MEMBER_C], brief="三人")
    ok, deny = full_rt.workers.pool.try_acquire("s-slot~human", "human")
    check("槽位：Agent 占满后人的唤醒仍然进得来（预留槽没被挤掉）", ok, deny)
    full_rt.workers.pool.release("s-slot~human")


# ── 9. 端到端（路由 + 车道自检）─────────────────────────────


def group_routes() -> None:
    print("\n── 端到端：orchestrate 车道 ──")
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import routes_lanes as rl
        import routes_orchestrate as ro
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: 端到端依赖缺失（{exc}）")
        return

    class FakeEngine:
        _TASK_SESSIONS: dict = {}

    app = FastAPI()
    report = rl.attach(app, FakeEngine(), ["routes_orchestrate"])
    check("车道：orchestrate 挂载通过（前缀不越界、字面路径不被吞）",
          report and report[0]["lane"] == "orchestrate", str(report)[:250])
    check("车道：端点数量与路由文件一致",
          report[0]["endpoints"] == len(ro.router.routes), str(report)[:250])

    client = TestClient(app)
    reg = build_registry()
    tm.reset_runtime()
    tm.get_team_runtime(workers=reg)

    r = client.post("/api/experts/teams", json={
        "name": "取材与成稿", "hostExpertId": HOST_ID,
        "memberExpertIds": [MEMBER_A, MEMBER_B], "experts": {"items": registry_experts()},
        "session": "s-http",
    })
    body = r.json()
    check("路由：建队并绑定房间一次请求做完", r.status_code == 200 and body.get("ok"), str(body)[:250])
    check("路由：返回体带出房间", body.get("room", {}).get("room") == "s-http", str(body)[:250])
    team_id = body["team"]["id"]

    r = client.post("/api/experts/teams", json={
        "name": "坏编制", "hostExpertId": HOST_ID, "memberExpertIds": ["ghost"],
        "experts": {"items": registry_experts()},
    })
    check("路由：不合法的编制回 422 并带原因", r.status_code == 422 and "ghost" in str(r.json()),
          str(r.json())[:250])

    r = client.post("/api/experts/teams", json={"name": "没注册表", "hostExpertId": HOST_ID,
                                                "memberExpertIds": [MEMBER_A]})
    check("路由：没有 experts 也没有 session 时 400（不猜一份注册表出来建队）",
          r.status_code == 400 and "注册表" in str(r.json()), str(r.json())[:250])

    r = client.post("/api/experts/rooms/dispatch", json={
        "session": "s-http", "memberIds": [MEMBER_A, MEMBER_B], "brief": "分头做",
    })
    d = r.json()
    check("路由：一次并行派工两人", r.status_code == 200 and len(d.get("dispatched") or []) == 2,
          str(d)[:250])

    r = client.get("/api/experts/rooms/state", params={"session": "s-http"})
    state = r.json()
    check("路由：房间状态里两名成员都在途",
          all(m["inflight"] for m in state["members"]) and len(state["members"]) == 2,
          str(state)[:300])
    check("路由：房间状态带成员检查点字段（#149 与 #148 同一份视图）",
          all("checkpoint" in m for m in state["members"]), str(state["members"])[:250])

    r = client.get("/api/experts/rooms/timeline", params={"session": "s-http"})
    tl_data = r.json()
    check("路由：时间线按说话人给出，并带回翻页游标",
          tl_data["ok"] and any(e["speakerAgentId"] == tm.HOST_SPEAKER for e in tl_data["events"])
          and tl_data["nextAfterMs"] > 0, str(tl_data)[:300])

    r = client.get("/api/experts/rooms/hint", params={"session": "s-http"})
    check("路由：提示词端点与工具面同源（同一个房间绑定）",
          r.json()["hasTeam"] and "团队编制" in r.json()["hint"], str(r.json())[:200])

    r = client.get("/api/checkpoint/members", params={"session": "s-http"})
    view = r.json()
    check("路由：成员检查点概览只报计数不报正文",
          view["ok"] and "正文不落在这里" in view["note"]
          and all("messages" not in m for m in view["members"]), str(view)[:300])

    job_a = next(m for m in state["members"] if m["id"] == MEMBER_A)
    r = client.get("/api/checkpoint/member/carry", params={"session": "s-http", "member": MEMBER_A})
    carry = r.json()
    check("路由：carry 预览不改台账（读两次结果一样）",
          carry["ok"] and "预览" in carry["note"]
          and client.get("/api/checkpoint/member", params={"session": "s-http", "member": MEMBER_A}
                         ).json()["state"] == "inflight", str(carry)[:250])

    r = client.post("/api/experts/rooms/stop", json={"session": "s-http", "reason": "用户点的停止"})
    st = r.json()
    check("路由：停止只停主持人并回出在途成员", st.get("ok") and st.get("stoppedScope") == "host"
          and len(st.get("inflightMembers") or []) == 2, str(st)[:300])
    check("路由：主持人本来没在跑时不谎称停过（hostRoundClaimed=False，墙上不补「已停止」）",
          st.get("hostRoundClaimed") is False
          and "host_stopped" not in [e.get("kind") for e in
                                     client.get("/api/experts/rooms/timeline",
                                                params={"session": "s-http"}).json()["events"]],
          str(st)[:250])

    r = client.post("/api/experts/rooms/member-report", json={
        "session": "s-http", "memberId": MEMBER_A, "jobId": job_a["lastJobId"], "report": "交回一段",
    })
    rep = r.json()
    check("路由：成员回叫收口并回叫主持人", rep.get("ok") and rep["callback"].get("ok"), str(rep)[:300])

    r = client.post("/api/experts/rooms/member-report", json={
        "session": "s-http", "memberId": MEMBER_A, "jobId": job_a["lastJobId"], "report": "重复收口",
    })
    check("路由：重复收口回 409（台账里只有一个「完成」）", r.status_code == 409, str(r.json())[:250])

    r = client.get("/api/experts/rooms/pending", params={"session": "s-http"})
    check("路由：主持人待读从耐久收件箱答得出（重启后同样读得到）",
          r.json()["ok"] and r.json()["count"] >= 1, str(r.json())[:250])

    r = client.post("/api/experts/teams/amend", json={
        "teamId": team_id, "memberExpertIds": [MEMBER_A], "session": "s-http",
    })
    check("路由：B 还在途时把他移出回 409（在途是状态冲突，不是参数写错）",
          r.status_code == 409 and r.json().get("code") == "MEMBER_INFLIGHT"
          and "在途" in str(r.json()), str(r.json())[:250])

    r = client.post("/api/experts/teams/amend", json={
        "teamId": team_id, "memberExpertIds": [MEMBER_A, MEMBER_B, MEMBER_C], "session": "s-http",
    })
    check("路由：往名单里加新人而注册表读不出时回 422 并点出缺什么",
          r.status_code == 422 and r.json().get("code") == "EXPERTS_REQUIRED"
          and "注册表" in str(r.json()), str(r.json())[:300])

    r = client.post("/api/experts/rooms/end-member", json={
        "session": "s-http", "memberId": MEMBER_B,
        "jobId": next(m for m in client.get("/api/experts/rooms/state", params={"session": "s-http"}
                                           ).json()["members"] if m["id"] == MEMBER_B)["lastJobId"],
        "status": "dropped", "reason": "超时",
    })
    check("路由：另一名按 dropped 收口", r.json().get("ok"), str(r.json())[:250])

    r = client.post("/api/experts/teams/remove", json={"teamId": team_id, "force": True})
    check("路由：收口后可以删编制（force 用来清中断证据，不是用来跳过在途）",
          r.json().get("ok"), str(r.json())[:250])

    r = client.get("/api/experts/rooms/snapshot")
    snap = r.json()
    check("路由：自述里带出三份存储的落点（出问题要知道去哪看）",
          snap["ok"] and all(k in snap["stores"] for k in ("teams", "rooms", "jobs")), str(snap)[:250])

    r = client.get("/api/experts/rooms/state")
    check("路由：缺 session 回 400（不拿空房间号回一份「什么都不是」的状态）",
          r.status_code == 400, str(r.json())[:200])

    # 会话表那条路：桌面建队时如果没带 experts，就按 session 读那一份注册表
    FakeEngine._TASK_SESSIONS["s-from-session"] = type("S", (), {
        "runtime": {"experts": {"items": registry_experts()}},
    })()
    r = client.post("/api/experts/teams", json={
        "name": "从会话读注册表", "hostExpertId": HOST_ID, "memberExpertIds": [MEMBER_C],
        "session": "s-from-session",
    })
    check("路由：按 session 从会话表读专家注册表能建队", r.json().get("ok"), str(r.json())[:250])
    r = client.post("/api/experts/teams", json={
        "name": "坏会话", "hostExpertId": HOST_ID, "memberExpertIds": [MEMBER_C],
        "session": "s-not-here",
    })
    check("路由：会话不在表里时 400 说清「没有第二份可查」",
          r.status_code == 400 and "会话表" in str(r.json()), str(r.json())[:250])
    FakeEngine._TASK_SESSIONS.pop("s-from-session", None)
    tm.reset_runtime()


# ── 10. 工具车道：没接线就一件都不注册 ──────────────────────


def group_toolkit() -> None:
    print("\n── 工具车道（team 开关未接线）──")
    try:
        import toolkit_lanes as tl
        import toolkit_orchestrate as tk
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: 工具车道依赖缺失（{exc}）")
        return

    class FakeToolkit:
        def __init__(self):
            self.tools: dict = {}

        def register_tool_function(self, fn):
            self.tools[fn.__name__] = fn

    def make_ctx(team=None, subagent=None):
        ctx = tl.ToolkitContext(
            toolkit=FakeToolkit(), work_dir=".", base=None, sandbox=None, emit=None,
            task_id="t", slides=False, office=False, grants=None, pptx_templates=None,
            extra={"subagent": subagent, "team": team},
        )
        return ctx

    ctx = make_ctx(subagent=object())
    report = tl.register_all(ctx, ["toolkit_orchestrate"])
    check("车道：只接了 subagent、没接 team 时一件都不注册（不承诺不存在的派工）",
          report[0]["registered"] == [], str(report)[:250])

    rt = build_runtime()
    make_team(rt, "s-tool")
    ctx2 = make_ctx(subagent=object(), team={"runtime": rt, "room": "s-tool"})
    registered = tl.register_all(ctx2, ["toolkit_orchestrate"])[0]["registered"]
    if "team" in getattr(tl, "SWITCH_BOARD", {}).get("orchestrate", ()):
        check("车道：接上 team 句柄后注册出两个派工工具",
              set(registered) == set(tk.TEAM_TOOLS), str(registered))
    else:
        check("车道：SWITCH_BOARD 里还没有 team ⇒ 句柄传了也注册不出来（开关在 W4 那份表里）",
              registered == [], str(registered))
    check("车道：常驻六件没被本车道抢名",
          not (set(tk.TEAM_TOOLS) & set(tl.RESIDENT_TOOLS)), str(tk.TEAM_TOOLS))
    check("车道：提示词 helper 与工具同门（没句柄时返回空串，不写一句「你有 team_dispatch」）",
          tk.host_hint(make_ctx(subagent=object())) == "")

    # 工具闭包本身（直接经 TeamRuntime.tools 拿，绕开车道开关）
    tools = rt.tools(room="s-tool", emit=None)
    names = sorted(f.__name__ for f in tools)
    check("工具：房间在闭包里定死，参数里没有 room（派不到别人的会话）",
          names == ["team_dispatch", "team_room_status"]
          and "room" not in tools[0].__code__.co_varnames, str(names))
    doc = tools[0].__doc__ or ""
    check("工具：说明里写清「成员看不到你的对话，说明必须自足」",
          "自足" in doc, doc[:160])


GROUPS = [
    group_roster, group_dispatch, group_timeline, group_stop, group_inflight_guard,
    group_restart, group_member_checkpoint, group_reuse, group_routes, group_toolkit,
]


def main() -> None:
    prev_home = os.environ.get("TONGTIANWORK_HOME")
    homes: list[str] = []
    try:
        for group in GROUPS:
            # 一组一套数据根：三份团队存储按数据根落盘，共用一个根会让上一组建的编制
            # 出现在下一组「重启后还剩几个团队」的断言里——那是测试互相踩，不是被测代码的问题。
            home = tempfile.mkdtemp(prefix="tongtian-w6-team-")
            homes.append(home)
            os.environ["TONGTIANWORK_HOME"] = home
            group()
    finally:
        if prev_home is None:
            os.environ.pop("TONGTIANWORK_HOME", None)
        else:
            os.environ["TONGTIANWORK_HOME"] = prev_home
        for home in homes:
            shutil.rmtree(home, ignore_errors=True)
    print("-" * 52)
    print(f"共 {COUNT['n']} 项")
    if FAILURES:
        print(f"engine team runtime selftest: {len(FAILURES)} 项失败")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("engine team runtime selftest: ALL PASS")


if __name__ == "__main__":
    main()
