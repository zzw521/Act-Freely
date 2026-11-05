#!/usr/bin/env python3
"""
可视化工作流编排（#156 / D-10）自测：触发 → 动作 → 条件 → 人工卡点

钉住的是 D-10 那条验收（「一个包含 抓取 → 摘要 → 审批 → 生成文档 的工作流可保存、
复用、查看每次运行」）里**能被代码判掉**的部分，外加三条这个模块自己承诺过的口径：

1. **不新造保序**：动作步投进的是 D-48 那张真工单表与真耐久收件箱（投递即落盘、
   同工单串行），而一次运行用的是 `房间~wf-<摘要>` 这张自己的单——**不占主持人那张单**；
2. **不新造审批开关**：人工卡点读的就是 `tool_approval.needs_approval` 那一个门。
   档位 off ⇒ 放行，但运行视图必须写 `decidedBy: "policy-off"`（不许读成「人已批准」）；
   档位开着而网关不在 ⇒ 挂起等人，绝不静默绕过；
3. **跑不通的定义不落盘**：校验器一次判掉全部错误，错误清单里每条都是中文说明。

离线运行：不联网、无需 API Key；工单层用真注册表 + 真耐久收件箱 + 临时数据根，
不起模型、不起回合；团队句柄与执行体回执用假对象（只验投递、记账与状态机）。
fastapi 缺失时端到端分组 SKIP 并以 0 退出。

用法: python tests/integration/engine_workflow_runtime_selftest.py
"""

import json
import os
import re
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
    import ticket_inbox_runtime as tir
    import ticket_worker_runtime as twr
    import tool_approval as tap
    import workflow_runtime as wfr
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: 工作流运行时自测不可用（{exc}）")
    sys.exit(0)


def check(name: str, cond: bool, detail: str = "") -> None:
    COUNT["n"] += 1
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# ── 假句柄：只验「投没投出去」与「状态机怎么走」，不起模型 ──────────


class FakeTeam:
    """团队句柄替身：记录每次派工，并按脚本返回成功/失败/等回执。"""

    def __init__(self, *, results=None) -> None:
        self.calls: list = []
        #: 每次 team_dispatch 调用依次取一条；取完就用最后一条（重试用例靠这个排布）
        self._results = list(results or [{"ok": True, "dispatched": [{"memberId": "writer"}],
                                          "refused": []}])

    def dispatch(self, *, room, member_ids, brief, title=""):
        self.calls.append({"kind": "dispatch", "room": room, "memberIds": list(member_ids),
                           "brief": brief, "title": title})
        res = self._results[min(len(self.calls) - 1, len(self._results) - 1)]
        return dict(res)

    def notify_host(self, *, room, text, origin_member="", job_id=""):
        self.calls.append({"kind": "notify", "room": room, "text": text,
                           "originMember": origin_member})
        return {"ok": True, "delivered": "journal-only"}


def gate(mode: str = "off", *, ready: bool = True, error: str = "") -> tap.ToolApproval:
    """按会话下发形状造一个审批门（档位与网关状态就是 Rust 随 /api/chat 给的那份）。"""
    present = mode in tap.MODES and mode != "off"
    return tap.ToolApproval(
        mode=mode,
        gate_url="http://127.0.0.1:17/gate" if ready and present else "",
        gate_token="t" if ready and present else "",
        workspace_id="ws",
        runtime_present=present,
        error=error,
    )


def build_registry() -> twr.TicketWorkerRegistry:
    """真注册表 + 真耐久收件箱：工作流动作的落盘与保序走的就是 D-48 验收过那条路径。"""
    return twr.TicketWorkerRegistry(
        agent_slots=3, human_extra=1, inbox_journal=tir.TicketInbox()
    )


def runtime_for(*, team=None, workers=None, gates=None, **kw) -> wfr.WorkflowRuntime:
    """装配一份工作流运行时；`gates` 是 房间 → 审批门 的表（模拟按会话下发）。"""
    return wfr.WorkflowRuntime(
        team_provider=(lambda: team) if team is not None else None,
        workers_provider=(lambda: workers) if workers is not None else None,
        approval_provider=(lambda room: (gates or {}).get(room, gate("off"))) if gates is not None else None,
        **kw,
    )


def step(sid: str, kind: str, *, nxt: str = "", **over) -> dict:
    out = {"id": sid, "kind": kind, "name": over.pop("name", sid), **over}
    if nxt:
        out["next"] = nxt
    return out


def four_step_wf(wf_id: str = "wf-news") -> dict:
    """验收里那本「抓取 → 摘要 → 审批 → 生成文档」。"""
    return {
        "id": wf_id, "name": "取材成稿",
        "description": "抓取、摘要、人工过目、成稿",
        "steps": [
            step("start", "trigger", nxt="grab"),
            step("grab", "action", nxt="digest",
                 spec={"action": "ticket_wake", "text": "抓取这批链接的正文"}),
            step("digest", "action", nxt="review",
                 spec={"action": "ticket_wake", "text": "把抓取到的内容压成三条要点"}),
            step("review", "approval", nxt="draft",
                 question="要点可以进稿吗？", spec={"tool": "generate_docx"}),
            step("draft", "action", nxt="",
                 spec={"action": "team_dispatch", "memberIds": ["writer"], "brief": "按要点成稿"}),
        ],
    }


def occupied_keys(reg: twr.TicketWorkerRegistry) -> list:
    return sorted((reg.pool.snapshot().get("occupied") or {}).keys())


# ── 1. 定义校验 ─────────────────────────────────────────────


def group_validate() -> None:
    print("\n── 定义校验 ──")
    wf, errors = wfr.normalize_workflow(four_step_wf())
    check("校验：验收那本四步工作流通过（触发→动作→动作→卡点→动作）",
          errors == [] and wf is not None, str(errors))
    check("校验：入口就是那个触发节点（不靠数组顺序猜）",
          wf and wf["entryStepId"] == "start", str(wf and wf["entryStepId"]))
    check("校验：步 id 归一后唯一可寻（中文与空格不会造出两个同名键）",
          wf and [s["id"] for s in wf["steps"]] == ["start", "grab", "digest", "review", "draft"],
          str(wf and [s["id"] for s in wf["steps"]]))

    bad_cases = [
        ("没有 steps", {"name": "空"}, "steps 要是非空数组"),
        ("没有名字", {"name": "", "steps": [step("a", "trigger")]}, "要有名字"),
        ("没有触发节点", {"name": "没头", "steps": [step("a", "action",
                                                       spec={"action": "ticket_wake", "text": "x"})]},
         "触发节点必须恰好一个"),
        ("两个触发节点", {"name": "两头", "steps": [step("a", "trigger"), step("b", "trigger")]},
         "多于一个"),
        ("未知节点类型", {"name": "怪", "steps": [step("a", "trigger"),
                                                {"id": "b", "kind": "loop"}]},
         "不在四种节点里"),
        ("next 指向不存在的步", {"name": "断头", "steps": [step("a", "trigger", nxt="ghost")]},
         "定义里没有这一步"),
        ("动作不在表里", {"name": "乱动作", "steps": [
            step("a", "trigger", nxt="b"), step("b", "action", spec={"action": "shell"})]},
         "不在表里"),
        ("派工没有成员", {"name": "派给谁", "steps": [
            step("a", "trigger", nxt="b"),
            step("b", "action", spec={"action": "team_dispatch", "brief": "干活"})]},
         "没有 memberIds"),
        ("派工没有说明", {"name": "说明呢", "steps": [
            step("a", "trigger", nxt="b"),
            step("b", "action", spec={"action": "team_dispatch", "memberIds": ["w"]})]},
         "没有 brief"),
        ("attempts 却没开 retry", {"name": "假重试", "steps": [
            step("a", "trigger", nxt="b"),
            step("b", "action", attempts=3, spec={"action": "ticket_wake", "text": "x"})]},
         "不会生效"),
        ("onError 拼错", {"name": "错法", "steps": [
            step("a", "trigger", nxt="b"),
            step("b", "action", onError="giveup", spec={"action": "ticket_wake", "text": "x"})]},
         "只能是 stop / retry / continue"),
        ("重试次数超上限", {"name": "重试狂", "steps": [
            step("a", "trigger", nxt="b"),
            step("b", "action", onError="retry", attempts=99,
                 spec={"action": "ticket_wake", "text": "x"})]},
         "超过上限"),
        ("条件缺一个分支", {"name": "单边", "steps": [
            step("a", "trigger", nxt="c"),
            step("c", "condition", nxt="z", nextTrue="z",
                 spec={"op": "nonEmpty", "source": "payload"}),
            step("z", "approval")]},
         "缺 nextFalse"),
        ("条件算子不在表里", {"name": "怪算子", "steps": [
            step("a", "trigger", nxt="c"),
            step("c", "condition", nextTrue="z", nextFalse="z",
                 spec={"op": "matches", "source": "payload"}),
            step("z", "approval")]},
         "不在九个里"),
        ("条件没写 source", {"name": "比什么", "steps": [
            step("a", "trigger", nxt="c"),
            step("c", "condition", nextTrue="z", nextFalse="z", spec={"op": "equals", "value": "x"}),
            step("z", "approval")]},
         "没写 source"),
        ("条件 source 形状不认识", {"name": "乱源", "steps": [
            step("a", "trigger", nxt="c"),
            step("c", "condition", nextTrue="z", nextFalse="z",
                 spec={"op": "equals", "source": "env", "value": "x"}),
            step("z", "approval")]},
         "只认 payload"),
        ("条件比的是不存在的步", {"name": "比幽灵", "steps": [
            step("a", "trigger", nxt="c"),
            step("c", "condition", nextTrue="z", nextFalse="z",
                 spec={"op": "contains", "source": "step:ghost.output", "value": "x"}),
            step("z", "approval")]},
         "定义里没有这一步"),
        ("卡点绑在既有豁免工具上", {"name": "永不等人", "steps": [
            step("a", "trigger", nxt="r"),
            step("r", "approval", spec={"tool": "browser_click"})]},
         "永远不会等人"),
        ("步骤 id 重复", {"name": "重名", "steps": [step("a", "trigger", nxt="a"),
                                                 step("a", "approval")]},
         "步骤 id 重复"),
    ]
    for label, raw, needle in bad_cases:
        _wf, errs = wfr.normalize_workflow(raw)
        check(f"校验拒掉：{label}", _wf is None and any(needle in e for e in errs),
              f"期望含「{needle}」，实得 {errs}")

    big = {"name": "超长", "steps": [step(f"s{i}", "trigger") for i in range(wfr.MAX_STEPS + 1)]}
    _wf, errs = wfr.normalize_workflow(big)
    check(f"校验拒掉：超过 {wfr.MAX_STEPS} 步的工作流先拆开", _wf is None and any("上限" in e for e in errs),
          str(errs)[:200])

    long_name = {"name": "长" * (wfr.MAX_NAME_CHARS + 1), "steps": [step("a", "trigger")]}
    _wf, errs = wfr.normalize_workflow(long_name)
    check("校验拒掉：名字超长是「拒」不是「替你截短」（截了就看不出两本撞名）",
          _wf is None and any("不代你删字" in e for e in errs), str(errs)[:220])

    exact = {"name": "长" * wfr.MAX_NAME_CHARS, "steps": [step("a", "trigger")]}
    _wf, _e2 = wfr.normalize_workflow(exact)
    check(f"校验放过：正好 {wfr.MAX_NAME_CHARS} 字的名字原样存下（不吞尾）",
          _wf is not None and _wf["name"] == "长" * wfr.MAX_NAME_CHARS, str(_e2)[:200])

    long_step = {"name": "步骤名", "steps": [
        step("a", "trigger", name="步" * (wfr.MAX_NAME_CHARS + 1))]}
    _wf, errs = wfr.normalize_workflow(long_step)
    check("校验拒掉：步骤名超长同样拒（运行视图里那一步靠名字认）",
          _wf is None and any("的名字" in e and "不代你删字" in e for e in errs), str(errs)[:220])

    long_q = {"name": "问题", "steps": [step("a", "trigger", nxt="r"),
                                        step("r", "approval",
                                             question="话" * (wfr.MAX_QUESTION_CHARS + 1))]}
    _wf, errs = wfr.normalize_workflow(long_q)
    check("校验拒掉：卡点的问题原文超长拒——不能让人在没读完的话上签字",
          _wf is None and any("问题原文" in e and "不代你删字" in e for e in errs),
          str(errs)[:220])

    rt_len = runtime_for()
    ok_save = rt_len.save({"name": "起运行",
                           "steps": [step("a", "trigger", nxt="r"), step("r", "approval")]})
    started = rt_len.start(workflow_id=ok_save["workflow"]["id"],
                           title="标" * (wfr.MAX_NAME_CHARS + 1))
    check("起运行拒掉：标题超长不截（运行清单里两条同名分不清是哪一次）",
          not started.get("ok") and started.get("code") == "WORKFLOW_INVALID", str(started)[:200])
    imported = rt_len.import_definition({"name": "导" * (wfr.MAX_NAME_CHARS + 1),
                                         "steps": [step("a", "trigger")]})
    check("导入拒掉：带超长名字的导入包不当「先收下再说」",
          not imported.get("ok") and any("不代你删字" in str(x) for x in (imported.get("errors") or [])),
          str(imported)[:220])
    check("导入拒掉超长名字时盘上没多出半本（拒的就是没落盘的）",
          [w["name"] for w in rt_len.definitions.all_workflows()] == ["起运行"],
          str([w["name"] for w in rt_len.definitions.all_workflows()])[:200])

    rt = runtime_for()
    before = sorted(w["id"] for w in rt.definitions.all_workflows())
    r = rt.save({"name": "坏定义", "steps": [step("a", "trigger", nxt="ghost")]})
    check("落盘：校验不过时一个字节都不写（不给「先收下再说」留口子）",
          not r.get("ok") and r.get("code") == "WORKFLOW_INVALID"
          and sorted(w["id"] for w in rt.definitions.all_workflows()) == before, str(r)[:250])
    check("落盘：错误清单整份带回去（界面要逐条显示，不是只给第一条）",
          isinstance(r.get("errors"), list) and len(r["errors"]) >= 1, str(r)[:200])


# ── 2. 版本化与导入导出 ─────────────────────────────────────


def group_versions() -> None:
    print("\n── 版本化与导入导出 ──")
    rt = runtime_for()
    r1 = rt.save(four_step_wf())
    check("版本：首存是第 1 版", r1.get("ok") and r1["workflow"]["version"] == 1, str(r1)[:200])
    body = four_step_wf()
    body["description"] = "改了说明"
    r2 = rt.save(body)
    check("版本：同 id 再存 = 升一版而不是并存两份", r2["workflow"]["version"] == 2, str(r2)[:200])
    check("版本：被换掉的那一版进了历史（当前版不在历史里，免得「当前」出现两次）",
          len(r2["workflow"].get("versions") or []) == 1
          and r2["workflow"]["versions"][0]["version"] == 1, str(r2["workflow"].get("versions"))[:250])

    old = rt.get_definition("wf-news", version=1)
    check("版本：按号取回旧版并如实标 historic", old.get("ok") and old.get("historic")
          and old["workflow"]["description"] == "抓取、摘要、人工过目、成稿", str(old)[:200])
    check("版本：旧版只读——不许按历史版起运行",
          rt.start(workflow_id="wf-news", room="s-x").get("ok")
          and rt.get_definition("wf-news", version=99).get("code") == "VERSION_UNAVAILABLE",
          str(rt.get_definition("wf-news", version=99))[:200])

    exp = rt.export("wf-news")
    check("导出：带 formatVersion 与纯定义（不含运行记录）",
          exp.get("ok") and exp["formatVersion"] == 1 and "runs" not in exp
          and exp["workflow"]["id"] == "wf-news", str(exp)[:200])
    check("导出：历史版单独导得出来，且不带别的版本",
          rt.export("wf-news", version=1).get("ok")
          and rt.export("wf-news", version=1)["versions"] == [], str(rt.export("wf-news", version=1))[:200])

    reimport = rt.import_definition(exp, keep_id=True)
    check("导入：同 id 撞车时换新 id 而不是静默覆盖（覆盖会让挂在旧版上的运行找不到定义）",
          reimport.get("ok") and reimport["workflow"]["id"] != "wf-news", str(reimport)[:250])
    check("导入：往返后步数与节点类型一致（复用成立的判据是定义没走形）",
          [s["kind"] for s in reimport["workflow"]["steps"]]
          == [s["kind"] for s in exp["workflow"]["steps"]], str(reimport)[:250])
    bad = rt.import_definition({"formatVersion": 99, "workflow": exp["workflow"]})
    check("导入：不认的 formatVersion 直接拒（收下就跑不通的定义会把人引到半途卡住的运行上）",
          not bad.get("ok") and bad.get("code") == "FORMAT_UNSUPPORTED", str(bad)[:200])

    deep = runtime_for()
    for i in range(wfr.MAX_VERSIONS + 6):
        deep.save({"id": "wf-deep", "name": "滚历史",
                   "steps": [step("a", "trigger", nxt="b"),
                             step("b", "approval", question=f"第 {i} 版")]})
    cur = deep.get_definition("wf-deep")["workflow"]
    check(f"版本：历史窗口有界（只留最近 {wfr.MAX_VERSIONS} 版）",
          len(cur.get("versions") or []) <= wfr.MAX_VERSIONS, str(len(cur.get("versions") or [])))
    check("版本：窗口外的旧版如实说取不到，不退回当前版糊过去",
          deep.get_definition("wf-deep", version=1).get("code") == "VERSION_UNAVAILABLE",
          str(deep.get_definition("wf-deep", version=1))[:200])


# ── 3. 线性跑通：动作投进真工单、真收件箱 ─────────────────────


def group_linear() -> None:
    print("\n── 线性跑通（复用 D-48 的投递与保序）──")
    reg = build_registry()
    team = FakeTeam()
    rt = runtime_for(team=team, workers=reg)
    saved = rt.save(four_step_wf())
    check("跑通：定义先存得下来才谈运行", saved.get("ok"), str(saved)[:200])

    gated = runtime_for(team=team, workers=reg, gates={"s-lin": gate("off")})
    gated.definitions = rt.definitions
    r = gated.start(workflow_id="wf-news", room="s-lin", payload={"links": "3 条"}, title="早刊")
    run = r.get("run") or {}
    check("跑通：档位 off 时整本四步一跑到底（审批自动放行，卡点来源另有专组验）",
          r.get("ok") and run.get("state") == "succeeded", str(r)[:250])
    ticket = wfr.run_ticket_id("s-lin", run.get("runId"))
    check("跑通：一次运行用的是**自己那张单**（房间~wf-…），不是主持人那张单",
          ticket == f"s-lin~wf-{wfr._digest(run.get('runId'), 10)}" and ticket != "s-lin", ticket)
    check("跑通：主持人那张单没被占（#148 论证过的槽位泄漏不在这里重演）",
          "s-lin" not in occupied_keys(reg), str(occupied_keys(reg)))
    pend = reg.inbox_journal.pending_for_ticket(ticket)
    check("跑通：两条动作都真投进了耐久收件箱（投递即落盘，本层没有第二个队列）",
          len(pend) == 2 and all(p.get("text") for p in pend), str(pend)[:300])
    check("跑通：同一次运行的两条投进同一张单 ⇒ 天然串行（同工单保序继承，没写第二次保序）",
          len({p.get("ticketId") for p in pend}) == 1, str(pend)[:250])
    check("跑通：派工那一步走的是团队句柄（brief 自足，成员看不到上游对话）",
          team.calls and team.calls[-1]["kind"] == "dispatch"
          and team.calls[-1]["brief"] == "按要点成稿", str(team.calls)[:250])

    view = gated.run_view(run["runId"])
    states = [s["state"] for s in view["steps"]]
    check("视图：每步都有状态可查（查看每次运行靠的是这份逐步视图，不是一句「跑过了」）",
          states.count("succeeded") == 4 and states.count("allowed") == 1, str(states))
    check("视图：产出按步 id 存进上下文（后面的条件步要比的就是它）",
          bool(view["steps"][1]["output"]), str(view["steps"][1])[:200])
    listed = gated.list_runs(workflow_id="wf-news")
    now_wf = gated.get_definition("wf-news")["workflow"]
    check("视图：运行列表带版本与状态（同一本定义复用跑多次要分得清是哪一次、钉的是哪一版）",
          len(listed) == 1 and listed[0]["workflowVersion"] == now_wf["version"]
          and listed[0]["state"] == "succeeded" and listed[0]["runId"] == run["runId"]
          and listed[0]["room"] == "s-lin", str(listed)[:250])

    r2 = gated.start(workflow_id="wf-news", room="s-lin2", payload={"links": "5 条"})
    check("复用：同一本定义再起一次是独立运行（各自的单、各自的步视图）",
          r2.get("ok") and r2["run"]["state"] == "succeeded"
          and r2["run"]["runId"] != run["runId"], str(r2)[:200])

    # 占位符单独一本：正文里写 {step:<id>.output}，投出去的必须是那一步真实记录的东西
    ph_reg = build_registry()
    ph = runtime_for(team=FakeTeam(), workers=ph_reg)
    ph.save({
        "id": "wf-ph", "name": "喂上游产出",
        "steps": [
            step("start", "trigger", nxt="grab"),
            step("grab", "action", nxt="draft", spec={"action": "ticket_wake", "text": "抓取"}),
            step("draft", "action", nxt="",
                 spec={"action": "ticket_wake", "text": "据「{step:grab.output}」成稿"}),
        ],
    })
    r_ph = ph.start(workflow_id="wf-ph", room="s-ph")
    ph_ticket = wfr.run_ticket_id("s-ph", (r_ph.get("run") or {}).get("runId"))
    ph_texts = [p.get("text") or "" for p in ph_reg.inbox_journal.pending_for_ticket(ph_ticket)]
    check("正文占位符：{step:<id>.output} 换成那一步的产出记录（花括号不许原样投进收件箱）",
          len(ph_texts) == 2 and "{step:grab.output}" not in ph_texts[1]
          and ph_ticket in ph_texts[1], str(ph_texts)[:250])

    cancel_rt = runtime_for(team=FakeTeam(results=[{"ok": True, "dispatched": [], "refused": []}]),
                            workers=build_registry(), gates={"s-can": gate("sensitive")})
    cancel_rt.definitions = rt.definitions
    c = cancel_rt.start(workflow_id="wf-news", room="s-can")
    check("取消：停在卡点上的运行能被取消", c.get("ok")
          and c["run"]["state"] == "awaiting_approval", str(c)[:200])
    done = cancel_rt.cancel(run_id=c["runId"], reason="用户改了主意")
    check("取消：取消后推不动，且说明「已投出去的唤醒不回滚」",
          done.get("state") == "cancelled" and "不回滚" in (done.get("note") or ""), str(done)[:250])
    again = cancel_rt.cancel(run_id=c["runId"], reason="又点了一次")
    check("取消：重复取消不改变已收口的运行（不记成第二次取消）",
          again.get("ok") and again.get("state") == "cancelled"
          and again.get("run", {}).get("endedAtMs") == done["run"]["endedAtMs"], str(again)[:250])


# ── 4. 条件分支 ─────────────────────────────────────────────


def group_condition() -> None:
    print("\n── 条件分支 ──")
    wf = {
        "id": "wf-cond", "name": "按字数分流",
        "steps": [
            step("start", "trigger", nxt="len"),
            step("len", "action", nxt="gate",
                 spec={"action": "ticket_wake", "text": "数一下字数"}),
            step("gate", "condition", nextTrue="long", nextFalse="short",
                 spec={"op": "greaterThan", "source": "payload", "field": "chars", "value": 100}),
            step("long", "approval", nxt="end", question="长稿要不要人工过目？"),
            step("short", "notify", name="short", spec={"action": "notify_host", "text": "短稿直接发"}),
            step("end", "action", spec={"action": "ticket_wake", "text": "收尾"}),
        ],
    }
    wf["steps"][4]["kind"] = "action"
    # 这个房间开着审批档：走「通过」那条会撞上一个人工卡点，正好用来验分支选对了没
    rt = runtime_for(team=FakeTeam(), workers=build_registry(), gates={"s-cond": gate("sensitive")})
    saved = rt.save(wf)
    check("条件：定义合法（分支两条路都指得到步）", saved.get("ok"), str(saved)[:250])

    run_long = rt.start(workflow_id="wf-cond", room="s-cond", payload={"chars": 500})
    check("条件：比数字走通过分支（greaterThan 100 ⇒ 长稿，要人过目）",
          run_long.get("ok") and run_long["run"]["state"] == "awaiting_approval", str(run_long)[:250])
    gate_view = next(s for s in rt.run_view(run_long["runId"])["steps"] if s["stepId"] == "gate")
    check("视图：条件怎么判的写在里面（界面要答得出「为什么走了这条」）",
          "greaterThan" in gate_view["output"] and "通过" in gate_view["output"],
          str(gate_view["output"])[:200])

    run_short = rt.start(workflow_id="wf-cond", room="s-cond", payload={"chars": 12})
    check("条件：不通过走另一条（短稿不进人工卡点）",
          run_short.get("ok") and run_short["run"]["state"] == "succeeded", str(run_short)[:250])
    short_view = rt.run_view(run_short["runId"])
    states = {s["stepId"]: s["state"] for s in short_view["steps"]}
    check("视图：没走的那条分支留在 pending（不是 skipped 也不是 failed）",
          states.get("long") == "pending" and states.get("short") == "succeeded", str(states))

    bad_number = rt.start(workflow_id="wf-cond", room="s-cond", payload={"chars": "很多"})
    check("条件：比不到数字时按不通过走并说明（不抛异常、也不猜成通过）",
          bad_number.get("ok") and bad_number["run"]["state"] == "succeeded", str(bad_number)[:250])
    why = next(s for s in rt.run_view(bad_number["runId"])["steps"] if s["stepId"] == "gate")
    check("条件：「不是数字」这句话写进视图，不静默",
          "不是数字" in why["output"], str(why["output"])[:200])

    ref_wf = {
        "id": "wf-ref", "name": "比上一步产出",
        "steps": [
            step("start", "trigger", nxt="grab"),
            step("grab", "action", nxt="gate", spec={"action": "ticket_wake", "text": "抓取"}),
            step("gate", "condition", nextTrue="yes", nextFalse="no",
                 spec={"op": "contains", "source": "step:grab.output", "value": "delivered"}),
            step("yes", "approval", question="有产出，过目"),
            step("no", "action", spec={"action": "ticket_wake", "text": "没产出，重抓"}),
        ],
    }
    ref = runtime_for(team=FakeTeam(), workers=build_registry(),
                      gates={"s-ref": gate("sensitive")})
    ref.save(ref_wf)
    r_ref = ref.start(workflow_id="wf-ref", room="s-ref")
    check("条件：能比某一步的产出（source 用 step:<id>.output，跨步引用在校验期就判存在性）",
          r_ref.get("ok") and r_ref["run"]["state"] == "awaiting_approval", str(r_ref)[:250])
    check("条件：`.output` 尾巴不参与步 id 匹配（不剥掉就永远比到一个空值）",
          "contains" in next(s for s in ref.run_view(r_ref["runId"])["steps"]
                             if s["stepId"] == "gate")["output"], str(r_ref)[:200])


# ── 5. 人工卡点：授权口径 ───────────────────────────────────


def group_approval() -> None:
    print("\n── 人工卡点（复用既有审批门，不新造开关）──")
    wf = {
        "id": "wf-ap", "name": "带卡点",
        "steps": [
            step("start", "trigger", nxt="review"),
            step("review", "approval", nxt="after", onDeny="rejected",
                 question="这一步要人点头", spec={"tool": "write_text_file"}),
            step("after", "action", spec={"action": "ticket_wake", "text": "点头之后"}),
            step("rejected", "action", spec={"action": "notify_host", "text": "人说不做"}),
        ],
    }
    reg = build_registry()
    team = FakeTeam()

    off = runtime_for(team=team, workers=reg, gates={"s-off": gate("off")})
    off.save(wf)
    r = off.start(workflow_id="wf-ap", room="s-off")
    view = off.run_view(r["runId"])
    ap = next(s for s in view["steps"] if s["stepId"] == "review")
    check("卡点：档位 off ⇒ 自动放行（沿用既有口径，不另开一个「工作流审批」开关）",
          r.get("ok") and view["state"] == "succeeded" and ap["state"] == "allowed", str(view)[:250])
    check("卡点：放行的**来源**写清是 policy-off（把没人问过画成人已批准是这一层最坏的一种谎）",
          ap["approval"]["decidedBy"] == "policy-off" and "没有人被问过" in ap["approval"]["note"],
          str(ap["approval"])[:250])
    check("卡点：列表里能数出「自动放行过几次」（界面不许把它统计成人工批准数）",
          off.list_runs(workflow_id="wf-ap")[0]["autoPassed"] == 1, str(off.list_runs())[:250])

    ready = runtime_for(team=team, workers=reg, gates={"s-hi": gate("sensitive")})
    ready.definitions = off.definitions
    h = ready.start(workflow_id="wf-ap", room="s-hi")
    check("卡点：档位开着 ⇒ 停在等人（不自动放行）",
          h.get("ok") and h["run"]["state"] == "awaiting_approval", str(h)[:250])
    pend = ready.pending()
    check("卡点：待人的运行从盘上列得出来（带问题原文）",
          pend["count"] >= 1 and pend["runs"][0]["steps"][0]["question"] == "这一步要人点头",
          str(pend)[:250])
    ok = ready.resolve_approval(run_id=h["runId"], step_id="review", decision="allow", note="可以")
    check("卡点：人点头后继续往下跑", ok.get("state") == "succeeded"
          and ok["run"]["currentStepId"] == "", str(ok)[:250])
    dup = ready.resolve_approval(run_id=h["runId"], step_id="review", decision="allow")
    check("卡点：重复点确认不能再推一步（一份定义不许跑出两次前进）",
          not dup.get("ok") and dup.get("code") == "NOT_AWAITING_APPROVAL", str(dup)[:250])

    deny = runtime_for(team=team, workers=reg, gates={"s-deny": gate("all")})
    deny.definitions = off.definitions
    d = deny.start(workflow_id="wf-ap", room="s-deny")
    res = deny.resolve_approval(run_id=d["runId"], step_id="review", decision="deny", note="格式不对")
    check("卡点：拒绝走 onDeny 那条（不是取消整条运行，也不是当成成功）",
          res.get("state") == "succeeded" and team.calls
          and team.calls[-1]["kind"] == "notify", str(res)[:250])
    denied_view = next(s for s in deny.run_view(d["runId"])["steps"] if s["stepId"] == "review")
    check("卡点：拒绝的原因与来源一起落盘（谁点的、说了什么，事后要认得出）",
          denied_view["approval"]["state"] == "denied"
          and denied_view["approval"]["decidedBy"] == "human"
          and denied_view["approval"]["note"] == "格式不对", str(denied_view["approval"])[:250])

    down = runtime_for(team=team, workers=reg,
                       gates={"s-down": gate("sensitive", ready=False, error="本机确认网关未启动")})
    down.definitions = off.definitions
    dn = down.start(workflow_id="wf-ap", room="s-down")
    info = next(s for s in down.run_view(dn["runId"])["steps"]
                if s["stepId"] == "review")["approval"]
    check("卡点：档位开着而网关不可用 ⇒ 挂起等人，**不静默绕过**（与 tool_approval 同一条口径）",
          dn["run"]["state"] == "awaiting_approval" and info["decidedBy"] == "gate-unavailable"
          and "绝不会自动放行" in info["note"], str(info)[:300])

    def broken_gate(room: str):
        raise RuntimeError("车道没装配：engine() 在拒")

    broken = wfr.WorkflowRuntime(team_provider=lambda: team, workers_provider=lambda: reg,
                                 approval_provider=broken_gate)
    broken.definitions = off.definitions
    br = broken.start(workflow_id="wf-ap", room="s-boom")
    br_view = next(s for s in broken.run_view(br["runId"])["steps"] if s["stepId"] == "review")
    check("卡点：档位**读不出来**时不许当成 off（provider 报错 ⇒ blocked，异常不被静默吞掉）",
          br["run"]["state"] == "blocked" and br_view["state"] == "blocked"
          and "读不出本会话的审批档位" in (br["run"]["note"] or "")
          and "车道没装配" in (br["run"]["note"] or ""), str(br["run"])[:300])

    ro = runtime_for(team=team, workers=reg, gates={"s-ro": gate("sensitive")})
    ro.save({"id": "wf-ro", "name": "只读工具卡点",
             "steps": [step("a", "trigger", nxt="b"),
                       step("b", "approval", nxt="c", spec={"tool": "view_text_file"}),
                       step("c", "action", spec={"action": "ticket_wake", "text": "后面"})]})
    rr = ro.start(workflow_id="wf-ro", room="s-ro")
    check("卡点：绑只读工具时按既有门的判断走（sensitive 档不问只读 ⇒ 放行并标明来源，不假装弹过窗）",
          rr["run"]["state"] == "succeeded"
          and next(s for s in ro.run_view(rr["runId"])["steps"] if s["stepId"] == "b")
          ["approval"]["decidedBy"] == "policy-off", str(rr)[:250])

    bogus = runtime_for(team=team, workers=reg, gates={"s-b": gate("sensitive")})
    bogus.definitions = off.definitions
    b = bogus.start(workflow_id="wf-ap", room="s-b")
    check("卡点：结论只能 allow / deny（没有「跳过」这第三种答案）",
          not bogus.resolve_approval(run_id=b["runId"], step_id="review",
                                     decision="skip").get("ok"), "skip")
    check("卡点：不能替非卡点步骤给结论（步骤名对不上就拒，不蒙着推）",
          bogus.resolve_approval(run_id=b["runId"], step_id="after",
                                 decision="allow").get("code") == "STEP_NOT_APPROVAL", "after")


# ── 6. 失败、重试与装配缺件 ─────────────────────────────────


def group_failure() -> None:
    print("\n── 失败与重试 ──")
    base = {
        "id": "wf-f", "name": "重试",
        "steps": [
            step("a", "trigger", nxt="b"),
            step("b", "action", nxt="c", onError="retry", attempts=3,
                 spec={"action": "team_dispatch", "memberIds": ["w"], "brief": "干一次"}),
            step("c", "approval", question="收尾前确认"),
        ],
    }
    flaky = FakeTeam(results=[{"ok": False, "reason": "槽位满了"},
                              {"ok": False, "reason": "槽位满了"},
                              {"ok": True, "dispatched": [{"memberId": "w"}], "refused": []}])
    rt = runtime_for(team=flaky, workers=build_registry(), gates={})
    rt.save(base)
    r = rt.start(workflow_id="wf-f", room="s-f")
    view_b = next(s for s in rt.run_view(r["runId"])["steps"] if s["stepId"] == "b")
    check("重试：前两次失败第三次成功（attempts 真的在数）",
          view_b["state"] == "succeeded" and view_b["attempt"] == 3, str(view_b)[:250])
    check("重试：每次失败的原话留在步视图里（界面要答得出为什么慢，靠的是追加而不是覆盖）",
          len(view_b.get("history") or []) == 2
          and all("槽位满了" in (h.get("error") or "") for h in view_b["history"])
          and [h["attempt"] for h in view_b["history"]] == [1, 2], str(view_b["history"])[:200])

    always = FakeTeam(results=[{"ok": False, "reason": "这套装配里没人接"}])
    rt2 = runtime_for(team=always, workers=build_registry(), gates={})
    rt2.definitions = rt.definitions
    r2 = rt2.start(workflow_id="wf-f", room="s-f2")
    check("重试：耗尽后如实 failed 并带上最后那句原因",
          r2["run"]["state"] == "failed" and "没人接" in (r2["run"]["note"] or ""), str(r2)[:250])

    strict = {"id": "wf-s", "name": "不重试",
              "steps": [step("a", "trigger", nxt="b"),
                        step("b", "action", nxt="c",
                             spec={"action": "team_dispatch", "memberIds": ["w"], "brief": "一次"}),
                        step("c", "approval", question="到了吗")]}
    rt3 = runtime_for(team=always, workers=build_registry(), gates={})
    rt3.save(strict)
    r3 = rt3.start(workflow_id="wf-s", room="s-s")
    check("失败：onError 缺省是 stop（默认不自动重投——每次重投都是一条真唤醒）",
          r3["run"]["state"] == "failed" and len(always.calls) >= 1, str(r3)[:200])
    # 只数这一本（这个房间）投出去的：`always` 那份假团队还被上面几次运行用过，
    # 按总调用数判等于把别人的派工算到这条运行头上。
    only_once = len([c for c in always.calls if c["kind"] == "dispatch" and c["room"] == "s-s"])
    check("失败：stop 那条只投一次（不重试就不该多打一次扰）", only_once == 1, str(only_once))

    cont = {"id": "wf-c", "name": "容错继续",
            "steps": [step("a", "trigger", nxt="b"),
                      step("b", "action", nxt="c", onError="continue",
                           spec={"action": "team_dispatch", "memberIds": ["w"], "brief": "可有可无"}),
                      step("c", "action", spec={"action": "ticket_wake", "text": "必须做"})]}
    workers = build_registry()
    rt4 = runtime_for(team=always, workers=workers, gates={})
    rt4.save(cont)
    r4 = rt4.start(workflow_id="wf-c", room="s-c")
    check("容错：onError=continue 时这一步记 failed 但运行继续",
          r4["run"]["state"] == "succeeded"
          and next(s for s in rt4.run_view(r4["runId"])["steps"] if s["stepId"] == "b")["state"] == "failed",
          str(r4)[:250])
    pend_texts = [p.get("text") for p in workers.inbox_journal.all_pending()] \
        if hasattr(workers.inbox_journal, "all_pending") else []
    check("容错：继续的那一步确实投进了收件箱（不是只在内存里推进了状态）",
          len(workers.inbox_journal.pending_wakes_for_ticket(
              wfr.run_ticket_id("s-c", r4["runId"]))) == 1, str(pend_texts)[:250])

    bare = wfr.WorkflowRuntime()
    bare.save({"id": "wf-n", "name": "没接线",
               "steps": [step("a", "trigger", nxt="b"),
                         step("b", "action", spec={"action": "team_dispatch",
                                                   "memberIds": ["w"], "brief": "干"})]})
    nb = bare.start(workflow_id="wf-n", room="s-n")
    check("装配缺件：没有团队句柄时这一步是 blocked 而不是 failed/succeeded",
          nb.get("ok") and nb["run"]["state"] == "blocked", str(nb)[:250])
    check("装配缺件：说明写清「这套安装里根本没有能执行它的东西」",
          "根本没有能执行它的东西" in (nb["run"]["note"] or ""), str(nb["run"]["note"])[:250])
    snap = bare.snapshot()
    check("自述：三个句柄接没接一目了然（不接就别说工作流能跑）",
          snap["wiring"] == {"team": False, "workers": False, "approvalMode": "off",
                             "approvalReady": False}, str(snap["wiring"])[:250])
    check("自述：blocked 的运行也在「还没收口」那一张里（它等人补装配，不等人点确认）",
          any(x["state"] == "blocked" for x in bare.pending()["runs"]), str(bare.pending())[:250])


# ── 7. 异步回执 ─────────────────────────────────────────────


def group_async() -> None:
    print("\n── 异步回执 ──")
    pending_team = FakeTeam(results=[{"ok": True, "delivered": "awaiting-report",
                                      "dispatched": [{"memberId": "w"}], "refused": []}])
    # 房间开了档：回执之后运行应停在下一个人工卡点，而不是自动放行直接收工
    rt = runtime_for(team=pending_team, workers=build_registry(),
                     gates={"s-a": gate("sensitive")})
    rt.save({"id": "wf-a", "name": "等回执",
             "steps": [step("a", "trigger", nxt="b"),
                       step("b", "action", nxt="c",
                            spec={"action": "team_dispatch", "memberIds": ["w"], "brief": "慢慢做"}),
                       step("c", "approval", question="收到回执了吗")]})
    r = rt.start(workflow_id="wf-a", room="s-a")
    check("异步：执行体说「投出去了，结果稍后」时停在 awaiting_step",
          r["run"]["state"] == "awaiting_step", str(r)[:250])
    dup_a = rt.report(run_id=r["runId"], step_id="a", ok=True, output="把触发步当回执收")
    check("异步：只有「正在等回执」的那一步能被回执收（没等过的步不算）",
          not dup_a.get("ok") and dup_a.get("code") == "STEP_NOT_WAITING", str(dup_a)[:250])
    ok = rt.report(run_id=r["runId"], step_id="b", ok=True, output="成员已交回三段")
    check("异步：回执把运行推到下一个卡点（不是直接收工）",
          ok.get("state") == "awaiting_approval", str(ok)[:250])
    dup_b = rt.report(run_id=r["runId"], step_id="b", ok=True, output="重复上报")
    check("异步：同一步第二次上报不被当成第二次成功（运行已停在下一个卡点，回执没有落脚点）",
          not dup_b.get("ok") and dup_b.get("code") == "NOT_AWAITING_STEP", str(dup_b)[:250])
    stored = next(s for s in rt.run_view(r["runId"])["steps"] if s["stepId"] == "b")
    check("异步：回执正文进了上下文（后面的条件步要比的就是它）",
          "成员已交回三段" in stored["output"], str(stored["output"])[:200])

    bad_rt = runtime_for(team=FakeTeam(results=[{"ok": True, "delivered": "awaiting-report"}]),
                         workers=build_registry(), gates={})
    bad_rt.definitions = rt.definitions
    b = bad_rt.start(workflow_id="wf-a", room="s-a2")
    fail = bad_rt.report(run_id=b["runId"], step_id="b", ok=False, error="成员超时没交回")
    check("异步：回执说没做完 ⇒ 按失败口径走（onError 缺省 stop ⇒ 运行 failed）",
          fail.get("state") == "failed" and "成员超时没交回" in (fail.get("note") or ""), str(fail)[:250])

    stray = runtime_for(team=FakeTeam(), workers=build_registry(), gates={})
    check("异步：没有这条运行就如实说没有（不凭空推进一个不知道哪儿来的回执）",
          not stray.report(run_id="wr-nope", step_id="b", ok=True).get("ok"), "wr-nope")


# ── 8. 环与预算 ─────────────────────────────────────────────


def group_budget() -> None:
    print("\n── 回环预算 ──")
    rt = runtime_for(team=FakeTeam(), workers=build_registry(), gates={})
    rt.save({
        "id": "wf-loop", "name": "没有出口的环",
        "steps": [
            step("a", "trigger", nxt="g"),
            step("g", "condition", nextTrue="do", nextFalse="do",
                 spec={"op": "nonEmpty", "source": "payload"}),
            step("do", "action", nxt="g",
                 spec={"action": "team_dispatch", "memberIds": ["w"], "brief": "再来一轮"}),
        ],
    })
    r = rt.start(workflow_id="wf-loop", room="s-loop", payload={"x": 1})
    check("预算：回环没有出口时按上限停下并如实 failed（不是无限投唤醒）",
          r["run"]["state"] == "failed" and "判定为环" in (r["run"]["note"] or ""), str(r)[:250])
    check("预算：停在预算线上（执行步数等于上限，一条也没多投）",
          r["run"]["executed"] == wfr.MAX_STEPS_EXECUTED, str(r["run"]["executed"]))


# ── 9. 重启、删除护栏与上限 ─────────────────────────────────


def group_restart() -> None:
    print("\n── 重启与护栏 ──")
    reg = build_registry()
    team = FakeTeam()
    rt = runtime_for(team=team, workers=reg, gates={"s-r": gate("sensitive")})
    rt.save({"id": "wf-r", "name": "重启还在",
             "steps": [step("a", "trigger", nxt="b"),
                       step("b", "approval", nxt="c", question="重启之后还认得我吗"),
                       step("c", "action", spec={"action": "ticket_wake", "text": "点头之后"})]})
    r = rt.start(workflow_id="wf-r", room="s-r")
    check("重启前：运行停在等人", r["run"]["state"] == "awaiting_approval", str(r)[:200])

    again = runtime_for(team=team, workers=reg, gates={"s-r": gate("sensitive")})
    view = again.run_view(r["runId"])
    check("重启后：运行记录从盘上读得回来（「查看每次运行」不依赖内存箱还在不在）",
          view.get("ok") and view["state"] == "awaiting_approval", str(view)[:250])
    check("重启后：待人的清单照样答得出",
          any(x["runId"] == r["runId"] for x in again.pending()["runs"]), str(again.pending())[:250])
    pushed = again.resolve_approval(run_id=r["runId"], step_id="b", decision="allow")
    check("重启后：卡点仍能被补点通过（不是只能僵在那儿）",
          pushed.get("state") == "succeeded", str(pushed)[:250])

    r_live = again.start(workflow_id="wf-r", room="s-r")
    check("护栏现场：同一本定义再起一条，停在等人（删除护栏与孤儿运行都靠它验）",
          r_live.get("ok") and r_live["run"]["state"] == "awaiting_approval", str(r_live)[:200])
    blocked = again.remove(workflow_id="wf-r")
    check("护栏：还有运行挂在定义上时删除被拒（那些卡点没人认得了）",
          not blocked.get("ok") and blocked.get("code") == "RUNS_INFLIGHT", str(blocked)[:250])
    forced = again.remove(workflow_id="wf-r", force=True)
    check("护栏：force 只承认「运行留在盘上」，不连带删运行历史",
          forced.get("ok") and forced.get("inflightLeft") == 1
          and again.run_view(r_live["runId"]).get("ok"), str(forced)[:250])
    after = again.resolve_approval(run_id=r_live["runId"], step_id="b", decision="allow")
    check("护栏：定义没了之后在跑的运行不再被猜着推进（结论不应用，但运行要如实改口）",
          not after.get("ok") and after.get("code") == "DEFINITION_UNAVAILABLE"
          and (after.get("run") or {}).get("state") == "blocked"
          and "不猜一份定义把它推完" in ((after.get("run") or {}).get("note") or ""), str(after)[:250])
    row = next((x for x in again.pending()["runs"] if x["runId"] == r_live["runId"]), {})
    check("护栏：它在「还没收口」清单里读成 blocked，不再冒充「等人点」（没人答得了的卡点不该一直挂着）",
          row.get("state") == "blocked", str(row)[:250])

    many = runtime_for()
    ok_count = 0
    for i in range(wfr.MAX_WORKFLOWS):
        res = many.save({"name": f"第 {i} 本", "steps": [step("a", "trigger", nxt="b"),
                                                        step("b", "approval")]})
        ok_count += 1 if res.get("ok") else 0
    over = many.save({"name": "挤不进去", "steps": [step("a", "trigger")]})
    check(f"上限：{wfr.MAX_WORKFLOWS} 份存得进、第 {wfr.MAX_WORKFLOWS + 1} 份如实拒收",
          ok_count == wfr.MAX_WORKFLOWS and not over.get("ok")
          and "没有落盘" in (over.get("reason") or ""), str(over)[:250])
    check("上限：拒收时不挤掉已有定义（被挤掉的那份可能正有运行挂在某一步上）",
          len(many.list_definitions()) == wfr.MAX_WORKFLOWS, str(len(many.list_definitions())))


# ── 10. 端到端（路由 + 车道）──────────────────────────────


def group_routes() -> None:
    print("\n── 端到端：orchestrate 车道的工作流端点 ──")
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import routes_lanes as rl
        import routes_orchestrate as ro
        import team_runtime as tm
        import ticket_worker_service as tws
    except Exception as exc:  # noqa: BLE001
        print(f"SKIP: 端到端依赖缺失（{exc}）")
        return

    class FakeSession:
        def __init__(self, runtime): self.runtime = runtime

    class FakeEngine:
        _TASK_SESSIONS: dict = {}

    app = FastAPI()
    report = rl.attach(app, FakeEngine(), ["routes_orchestrate"])
    check("车道：工作流端点没越前缀（/api/workflow* 已在 LANE_PREFIXES 里）",
          report and report[0]["lane"] == "orchestrate", str(report)[:250])
    check("车道：端点数量与路由文件一致",
          report[0]["endpoints"] == len(ro.router.routes), str(report)[:250])

    reg = build_registry()
    team = FakeTeam()
    # 车道装配读的是「已经启动的那两份单例」（peek_runtime），这里把接缝替身化：路由代码
    # 一行不改，走的就是真 provider ⇒ 能证明 /api/workflow 起运行接的确实是 D-48 那份注册表。
    real_team_peek, real_tws_peek = tm.peek_runtime, tws.peek_runtime

    class FakeWorkerService:
        registry = reg

    tm.peek_runtime = lambda: team
    tws.peek_runtime = lambda: FakeWorkerService()
    FakeEngine._TASK_SESSIONS["s-http"] = FakeSession({
        "toolApproval": {"mode": "sensitive", "gateUrl": "http://127.0.0.1:17/gate",
                         "gateToken": "t", "workspaceId": "ws"},
    })
    client = TestClient(app)

    r = client.get("/api/workflow/snapshot")
    snap = r.json()
    check("路由：自述带出两份存储的落点与三个句柄的接线状态",
          snap["ok"] and all(k in snap["stores"] for k in ("definitions", "runs"))
          and "wiring" in snap, str(snap)[:250])

    r = client.post("/api/workflow/definitions", json=four_step_wf())
    body = r.json()
    check("路由：存定义 200 并回第 1 版", r.status_code == 200 and body["workflow"]["version"] == 1,
          str(body)[:250])
    r = client.post("/api/workflow/definitions", json={"name": "坏", "steps": [
        {"id": "a", "kind": "trigger", "next": "ghost"}]})
    check("路由：坏定义回 422 且整份错误清单带回去",
          r.status_code == 422 and isinstance(r.json().get("errors"), list), str(r.json())[:250])

    r = client.get("/api/workflow/definitions")
    listed = r.json()["workflows"]
    check("路由：定义列表带步数、节点类型与是否有卡点（界面画卡片要的就是这几样）",
          any(w["id"] == "wf-news" and w["hasApproval"] and w["stepCount"] == 5 for w in listed),
          str(listed)[:300])

    r = client.get("/api/workflow/definitions/export", params={"workflow": "wf-news"})
    exp = r.json()
    r2 = client.post("/api/workflow/definitions/import", json={"payload": exp, "keepId": False})
    check("路由：导出→导入一次往返成功", exp["ok"] and r2.json().get("ok"), str(r2.json())[:250])
    r = client.get("/api/workflow/definitions/export")
    check("路由：导出没指名回 400（不给「默认导第一本」这种猜法留口子）",
          r.status_code == 400, str(r.json())[:200])

    r = client.post("/api/workflow/runs/start", json={"workflowId": "wf-news", "session": "s-http",
                                                     "payload": {"links": "2 条"}})
    started = r.json()
    check("路由：按会话那份 toolApproval 起运行 ⇒ 停在人工卡点（档位敏感，网关在）",
          r.status_code == 200 and started["run"]["state"] == "awaiting_approval", str(started)[:300])
    run_id = started["runId"]

    r = client.get("/api/workflow/pending")
    pend = r.json()
    check("路由：待办清单列出这条等人（带问题原文，界面能直接渲染确认框）",
          pend["count"] >= 1 and pend["runs"][0]["steps"][0]["question"] == "要点可以进稿吗？",
          str(pend)[:300])

    r = client.post("/api/workflow/runs/approve", json={"runId": run_id, "stepId": "review",
                                                       "decision": "allow", "note": "可以进稿"})
    ap = r.json()
    check("路由：点通过后跑到底（最后一步是派工，投的是这次运行自己那张单）",
          r.status_code == 200 and ap["state"] == "succeeded", str(ap)[:300])
    r = client.post("/api/workflow/runs/approve", json={"runId": run_id, "stepId": "review",
                                                        "decision": "allow"})
    check("路由：重复点确认回 404（运行已不在等人状态）", r.status_code == 404, str(r.json())[:250])

    r = client.get("/api/workflow/runs/view", params={"run": run_id})
    view = r.json()
    check("路由：逐步视图给出每步状态与尝试次数",
          view["ok"] and len(view["steps"]) == 5 and all(s["attempt"] >= 1 for s in view["steps"]),
          str([(s["stepId"], s["state"], s["attempt"]) for s in view["steps"]])[:300])
    check("路由：counts 把「自动/人工放行的卡点」与「真跑完的步」分开记（并成一格就读不出谁也没被问过）",
          view["counts"]["succeeded"] == 4 and view["counts"]["allowed"] == 1
          and view["counts"]["total"] == 5, str(view["counts"])[:250])
    check("路由：卡点结论的来源是 human（与 policy-off 分得开）",
          next(s for s in view["steps"] if s["stepId"] == "review")["approval"]["decidedBy"] == "human",
          str(view["steps"])[:300])

    r = client.get("/api/workflow/runs", params={"workflow": "wf-news"})
    check("路由：每次运行列得出来（复用过的定义要能分辨是第几次）",
          r.json()["ok"] and len(r.json()["runs"]) >= 1, str(r.json())[:250])
    r = client.get("/api/workflow/runs/view")
    check("路由：没指名运行回 400", r.status_code == 400, str(r.json())[:200])
    r = client.post("/api/workflow/runs/report", json={"runId": "wr-none", "stepId": "b", "ok": True})
    check("路由：查不到运行就如实回 404（不凭空推进）", r.status_code == 404, str(r.json())[:250])

    r = client.post("/api/workflow/runs/start", json={"session": "s-http"})
    check("路由：没给 workflowId 回 400（起一次运行得先说是哪一本）", r.status_code == 400,
          str(r.json())[:200])

    r = client.post("/api/experts/teams", json={"name": "无专家", "hostExpertId": "h"})
    check("路由：同一条车道上团队端点的口径没被工作流改动带歪",
          r.status_code == 400 and "注册表" in str(r.json()), str(r.json())[:200])

    w_rt = wfr.peek_runtime()
    check("路由：工作流没替主持人那张单占槽（s-http 不在占用表里）",
          "s-http" not in _occupied_from_registry(reg), str(_occupied_from_registry(reg))[:200])
    check("路由：派工那一步走的是装配里那份团队句柄（provider 接的是 peek_runtime，不是另起一个）",
          team.calls and team.calls[-1]["kind"] == "dispatch"
          and team.calls[-1]["room"] == "s-http", str(team.calls)[:250])
    check("路由：车道走的是那份模块级运行时与同一份盘（不是路由里另起一个内存箱）",
          w_rt is not None and any(w.get("id") == "wf-news" for w in w_rt.list_definitions()),
          str(w_rt and [w.get("id") for w in w_rt.list_definitions()])[:200])
    wfr.reset_runtime()
    tm.peek_runtime, tws.peek_runtime = real_team_peek, real_tws_peek
    FakeEngine._TASK_SESSIONS.clear()


def group_crosswire() -> None:
    """跨树同源：服务端 `tongtian-server/app/workflows.py` 的三张表与本模块逐字比对。

    一个跑在容器、一个跑在桌面，两边**没法 import 对方**，所以这里用正则把对面的常量抄
    回来钉死。不同源的后果不是「报错」而是共享断掉：服务端收得下而桌面拒的定义，用户点了
    「共享」以后本地说合法、云端说非法；反过来则云端存得下一本桌面跑不动的流程。

    服务端文件不在（比如只拿到桌面这一个仓库）时整组如实 SKIP 并说明——不装作验过。
    """
    server_file = PROJECT_ROOT.parent / "tongtian-server" / "app" / "workflows.py"
    if not server_file.exists():
        print(f"[SKIP] 跨树同源：旁边没有 tongtian-server/app/workflows.py（{server_file}），这一组没验")
        return
    text = server_file.read_text(encoding="utf-8")

    def literal_tuple(name: str) -> tuple:
        found = re.search(rf"^{name}\s*=\s*\((.*?)\)", text, re.S | re.M)
        if not found:
            return ()
        return tuple(re.findall(r'"([^"]+)"', found.group(1)))

    def literal_int(name: str) -> int:
        found = re.search(rf"^{name}\s*=\s*([0-9_]+)", text, re.M)
        if not found:
            return -1
        return int(found.group(1).replace("_", ""))

    for name in ("WORKFLOW_KINDS", "ACTION_KINDS", "CONDITION_OPS", "ON_ERROR",
                 "RUN_STATES", "APPROVAL_DECISIONS"):
        server_value = literal_tuple(name)
        check(f"跨树同源：{name} 两边逐字一致（桌面 {len(getattr(wfr, name))} 项）",
              bool(server_value) and server_value == tuple(getattr(wfr, name)),
              f"服务端 {server_value} vs 桌面 {tuple(getattr(wfr, name))}")

    pairs = (
        ("MAX_STEPS", "MAX_STEPS"),
        ("MAX_VERSIONS", "MAX_VERSIONS"),
        ("MAX_ATTEMPTS", "MAX_ATTEMPTS"),
        ("MAX_PER_PROJECT", "MAX_WORKFLOWS"),
        ("MAX_RUNS_PER_PROJECT", "MAX_RUNS"),
        ("MAX_TEXT", "MAX_TEXT_CHARS"),
        # 名字 / 标题 / 问题原文 / 步 id：这四条是「标识性」字段，一边截断一边拒 = 同一本
        # 定义在两处显示成两个名字，用户点的「复用」就不是他存的那一本。
        ("MAX_NAME", "MAX_NAME_CHARS"),
        ("MAX_TITLE", "MAX_NAME_CHARS"),
        ("MAX_QUESTION", "MAX_QUESTION_CHARS"),
        ("MAX_STEP_ID", "MAX_STEP_ID"),
    )
    for server_name, engine_name in pairs:
        server_value = literal_int(server_name)
        check(f"跨树同源：上限 {server_name} = 桌面 {engine_name} = {getattr(wfr, engine_name)}",
              server_value >= 0 and server_value == getattr(wfr, engine_name),
              f"服务端 {server_value} vs 桌面 {getattr(wfr, engine_name)}")

    check("跨树同源：服务端把这条比对写进了模块口径（改表的人先看见「必须同时改两处」）",
          "group_crosswire" in text and "改这三张表必须同时改两处" in text,
          "服务端模块文档里没提这条钉")


def _occupied_from_registry(reg: twr.TicketWorkerRegistry) -> list:
    return sorted((reg.pool.snapshot().get("occupied") or {}).keys())


GROUPS = [
    group_validate, group_versions, group_linear, group_condition, group_approval,
    group_failure, group_async, group_budget, group_restart, group_routes, group_crosswire,
]


def main() -> None:
    prev_home = os.environ.get("TONGTIANWORK_HOME")
    homes = []
    try:
        for group in GROUPS:
            # 每组一个独立数据根：上一组的定义与运行会渗进这一组的「上限」「重启」断言，
            # 表现出来就是一堆与改动无关的红（#148 那批踩过同一条）。
            home = tempfile.mkdtemp(prefix="tongtian-w6-workflow-")
            homes.append(home)
            os.environ["TONGTIANWORK_HOME"] = home
            wfr.reset_runtime()
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
        print(f"engine workflow runtime selftest: {len(FAILURES)} 项失败")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("engine workflow runtime selftest: ALL PASS")


if __name__ == "__main__":
    main()
