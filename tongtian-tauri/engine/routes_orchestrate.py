"""orchestrate 车道（W6）：多 Agent 团队协作、成员运行时检查点、可视化工作流。

占用前缀见 `routes_lanes.LANE_PREFIXES["orchestrate"]`（`/api/subagents` 一条没占：
子 Agent 的端点今天还在 server.py 里，动它就是把两条车道接在同一个名字上）。
派工与投递复用 D-47/D-48 已有的耐久收件箱与副作用队列语义（投递即落盘、同工单保序、
先落已读再销账），本文件不新造第二套保序逻辑：所有「投一条唤醒」的动作都走
`team_runtime.TeamRuntime`，它只调引擎那一份 `TicketWorkerRegistry`。

三条口径先写在这儿，免得读端点时误会「引擎自己会起回合」：

* 端点只保证**投递**与**记账**：投进去的唤醒一定在盘上，没投出去的一定做如实回执；
  起不起回合由 worker 调度器与桌面领取回路决定（Rust 那半边见 `docs/并行实施/148.md`）。
* 团队编制要对着**专家注册表**校验，而注册表是 Rust 随 `/api/chat` 下发的会话上下文
  （`session.runtime.experts`）。所以本车道要么在同一个进程里拿到它（`experts` 参数，
  桌面与自测用），要么按 `session` 从会话表读。两条都拿不到时**直接拒绝并说清为什么**，
  不退回「按名字猜一个专家」——猜出来的编制会在派工时把活投给一个不存在的人。
* 所有 `/api/` 路径自动过 server.py 的令牌守卫与审计，本文件不再写鉴权。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import expert_runtime as er
import routes_lanes as rl
import team_runtime as tm
import ticket_worker_service as tws
import tool_approval as tap
import workflow_runtime as wfr

LANE = "orchestrate"
OWNER = rl.LANE_OWNERS["orchestrate"]

router = APIRouter()


def _bad_request(code: str, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, "message": message}, status_code=400)


def _failed(payload: dict[str, Any], status: int = 422) -> JSONResponse:
    return JSONResponse(payload, status_code=status if not payload.get("ok") else 200)


#: 这些拒绝是「当前状态不允许」（有人在跑、台账还没收口），不是「请求体写错了」：
#: 界面据此决定是「等一等再试」还是「把参数改对」。
CONFLICT_CODES = ("MEMBER_INFLIGHT", "LEDGER_PENDING")


def _failed_by_code(payload: dict[str, Any]) -> JSONResponse:
    status = 409 if str(payload.get("code") or "") in CONFLICT_CODES else 422
    return _failed(payload, status=status)


async def _body(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001  没有请求体就是没参数，按缺参处理
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _runtime_or_error() -> tuple[Any, JSONResponse | None]:
    """取本进程那一份团队运行时；取不到就说人话。

    这里不降级成「临时自建一套」：自建注册表等于开第二个槽位池与第二个内存箱，
    同一条派工会有两套保序，而 D-48 验收过的「同工单串行」当场只对一半。
    """
    try:
        return tm.get_team_runtime(), None
    except Exception as exc:  # noqa: BLE001
        return None, JSONResponse({
            "ok": False,
            "code": "TEAM_RUNTIME_UNAVAILABLE",
            "message": (
                f"团队运行时没接上执行体注册表：{exc}。"
                "工单执行体装配（/api/tickets 那一侧）起来之后这条才可用——"
                "没有注册表就没有成员目录与槽位，派工只是台账上一行字"
            ),
        }, status_code=503)


def _experts_from_request(body: dict[str, Any]) -> tuple[list[dict], str]:
    """专家注册表的两个来源，一个都不猜。

    1. `experts`：注册表本体（`{"items": [...]}`）或 items 数组——桌面在提交会话之前
       就要能建编制，那时还没有会话；自测也走这条。
    2. `session`：从引擎会话表里读 `runtime.experts`（Rust 随 /api/chat 下发的那份）。
    """
    raw = body.get("experts")
    if raw is not None:
        if isinstance(raw, dict) and raw.get("items") is not None:
            block = raw
        elif isinstance(raw, dict) and "items" not in raw:
            # 单条专家对象被当成注册表传了进来：明确退回，不猜「这一条就是全部专家」——
            # normalize_registry 会把它读成「没有 items」，于是空注册表也能建出团队。
            return [], (
                "experts 收到的是单个对象且没有 items：注册表本体要长 "
                '{"items": [...]}，或者直接传专家数组'
            )
        elif isinstance(raw, list):
            block = {"items": raw}
        else:
            return [], 'experts 参数要成注册表对象（{"items": [...]}）或专家数组'
        items, err = er.normalize_registry(block)
        return items, (err or "")
    session_ref = str(body.get("session") or body.get("sessionId") or "").strip()
    if not session_ref:
        return [], (
            "没有专家注册表：要么带 experts 参数（桌面建队时还没有会话），"
            "要么带 session（从引擎会话表读那一份 runtime.experts）"
        )
    eng = rl.engine()
    sessions = getattr(eng, "_TASK_SESSIONS", None)
    session = (sessions or {}).get(session_ref)
    if session is None:
        return [], (
            f"会话 {session_ref} 不在本引擎进程的会话表里（引擎重启后未续跑、或 id 写错）："
            "专家注册表是随会话下发的，这里没有第二份可查"
        )
    runtime = session.runtime if isinstance(session.runtime, dict) else {}
    block = runtime.get("experts")
    if block is None:
        return [], (
            f"会话 {session_ref} 的 runtime 里没有 experts：这一轮提交没带专家注册表"
            "（没选专家就没有团队编制可校验，不是「注册表是空的」）"
        )
    items, err = er.normalize_registry(block)
    return items, (err or "")


def _session_of(body: dict[str, Any], query: str = "") -> str:
    return str(body.get("session") or body.get("sessionId") or query or "").strip()


# ── 编制（D-34 验收项⑤：重启后保留）──────────────────────────


@router.get("/api/experts/teams")
async def teams_list(request: Request):
    """本机全部团队编制，每条带当前房间与在途成员。"""
    runtime, err = _runtime_or_error()
    if err is not None:
        return err
    return JSONResponse({"ok": True, "teams": runtime.list_teams()})


@router.post("/api/experts/teams")
async def teams_create(request: Request):
    """建（或整体替换）一份团队编制：主持人专家 + 成员名单，对着注册表逐条校验。

    主持人自己不能当成员、成员不能重复、不能超过 6 人，这些都在 `normalize_team` 里
    一次判掉；任何一条不合法就整条拒——不做「把不认识的成员悄悄剔掉」，
    因为主持人会以为那队人在干活。
    """
    body = await _body(request)
    runtime, err = _runtime_or_error()
    if err is not None:
        return err
    experts, experts_error = _experts_from_request(body)
    if experts_error:
        return _bad_request("EXPERTS_UNAVAILABLE", experts_error)
    raw = body.get("team") if isinstance(body.get("team"), dict) else body
    result = runtime.create_team(raw, experts, experts_error=experts_error)
    if result.get("ok") and str(body.get("session") or body.get("sessionId") or "").strip():
        # 建队就顺手绑房间：桌面「选一个团队开一轮」是同一个动作，
        # 分成两次请求会让中间那次失败变成「有编制没房间」的半套状态。
        bound = runtime.open_room(
            team_id=result["team"]["id"],
            session_id=_session_of(body),
        )
        result["room"] = bound
    return _failed(result)


@router.post("/api/experts/teams/amend")
async def teams_amend(request: Request):
    """换成员（整体替换名单）。有在途派工的成员不许移出——移出去那份活就没人收口了。

    这条**不强行要求注册表**：只减人/换序时编制里那份快照就是当初对着注册表验过的结论，
    再要一次注册表只会把「会话已结束、这一轮没下发 experts」变成「改不了编制」。
    要往名单里**加**新人时注册表不可少，缺了会由运行时拒掉并说清缺的是什么。"""
    body = await _body(request)
    runtime, err = _runtime_or_error()
    if err is not None:
        return err
    experts, experts_error = _experts_from_request(body)
    member_ids = body.get("memberExpertIds")
    if member_ids is None:
        member_ids = body.get("members")
    if not isinstance(member_ids, list):
        return _bad_request("MEMBERS_REQUIRED", "memberExpertIds 要是数组：这里只接受整体替换名单")
    result = runtime.amend_members(
        team_id=str(body.get("teamId") or ""),
        member_ids=member_ids,
        experts=experts or None,
        registry_error=experts_error,
    )
    return _failed_by_code(result)


@router.post("/api/experts/teams/remove")
async def teams_remove(request: Request):
    """删编制。成员正在跑的时候拒绝；只有台账里留着「中断证据」时才允许 force。"""
    body = await _body(request)
    runtime, err = _runtime_or_error()
    if err is not None:
        return err
    result = runtime.remove_team(
        team_id=str(body.get("teamId") or ""), force=bool(body.get("force"))
    )
    return _failed_by_code(result)


# ── 房间（主持人会话）────────────────────────────────────────


@router.post("/api/experts/rooms/open")
async def rooms_open(request: Request):
    """把编制绑到主持人会话；房间 id 就是会话 id，成员工单与检查点都从它派生。"""
    body = await _body(request)
    runtime, err = _runtime_or_error()
    if err is not None:
        return err
    session = _session_of(body)
    if not session:
        return _bad_request("SESSION_REQUIRED", "缺 session：房间就是主持人会话，没有会话就没有房间")
    result = runtime.open_room(team_id=str(body.get("teamId") or ""), session_id=session)
    return _failed(result)


@router.get("/api/experts/rooms/state")
async def rooms_state(request: Request, session: str = "", timelineLimit: int = 30):
    """房间状态：编制、每个成员的在途/活绑定、派工台账、最近的群聊投影。"""
    runtime, err = _runtime_or_error()
    if err is not None:
        return err
    if not session.strip():
        return _bad_request("SESSION_REQUIRED", "缺 session 参数")
    state = runtime.room_state(room=session, timeline_limit=max(1, min(int(timelineLimit or 30), 200)))
    return _failed(state, status=404)


@router.get("/api/experts/rooms/timeline")
async def rooms_timeline(request: Request, session: str = "", afterMs: int = 0, limit: int = 50):
    """群聊时间线翻页（带说话人）；`afterMs` 填上次结果里最大的 atMs。"""
    runtime, err = _runtime_or_error()
    if err is not None:
        return err
    if not session.strip():
        return _bad_request("SESSION_REQUIRED", "缺 session 参数")
    events = runtime.rooms.events(session, after_ms=int(afterMs or 0), limit=max(1, min(int(limit or 50), 200)))
    return JSONResponse({"ok": True, "room": session, "events": events,
                         "nextAfterMs": max([int(e.get("atMs") or 0) for e in events], default=int(afterMs or 0))})


@router.get("/api/experts/rooms/hint")
async def rooms_hint(request: Request, session: str = ""):
    """主持人本轮该看到的编制提示词（工具面没接线时界面用它自述「这一队人是谁」）。"""
    runtime, err = _runtime_or_error()
    if err is not None:
        return err
    if not session.strip():
        return _bad_request("SESSION_REQUIRED", "缺 session 参数")
    hint = runtime.system_hint(room=session)
    return JSONResponse({
        "ok": True,
        "room": session,
        "hint": hint,
        "hasTeam": bool(hint),
        "note": (
            "编制提示词与工具面同源：这里读的就是 team_dispatch 用的那一份房间绑定"
            if hint else "这个会话没有绑定团队编制（提示词里不会出现团队段）"
        ),
    })


@router.post("/api/experts/rooms/dispatch")
async def rooms_dispatch(request: Request):
    """主持人派工（与 team_dispatch 工具同一条实现，给桌面/自测/外部编排用）。"""
    body = await _body(request)
    runtime, err = _runtime_or_error()
    if err is not None:
        return err
    session = _session_of(body)
    if not session:
        return _bad_request("SESSION_REQUIRED", "缺 session：派工只能由主持人对自己的房间发起")
    member_ids = body.get("memberIds")
    if member_ids is None:
        member_ids = body.get("memberExpertIds")
    if not isinstance(member_ids, list) or not member_ids:
        return _bad_request("MEMBERS_REQUIRED", "memberIds 要是非空数组：派给谁都没有就说不出派工成功了什么")
    result = runtime.dispatch(
        room=session,
        member_ids=member_ids,
        brief=str(body.get("brief") or ""),
        title=str(body.get("title") or ""),
    )
    return _failed(result, status=409)


@router.post("/api/experts/rooms/member-report")
async def rooms_member_report(request: Request):
    """成员交回结论：收执行体那侧的口、上墙带姓名的回复、回叫主持人。

    `ok=false` 也要收：主持人必须知道「这摊活没人做完」，静默失败正是 D-34 要修的毛病。"""
    body = await _body(request)
    runtime, err = _runtime_or_error()
    if err is not None:
        return err
    session = _session_of(body)
    if not session or not str(body.get("memberId") or "").strip():
        return _bad_request("ARGS_REQUIRED", "session 与 memberId 都必填：缺一个就不知道这条回复属于谁")
    result = runtime.member_reported(
        room=session,
        member_id=body.get("memberId"),
        job_id=str(body.get("jobId") or ""),
        report=str(body.get("report") or ""),
        ok=bool(body.get("ok", True)),
        end_status=str(body.get("endStatus") or "finished"),
    )
    return _failed(result, status=409)


@router.post("/api/experts/rooms/end-member")
async def rooms_end_member(request: Request):
    """成员没做完就结束（超时/被杀）：留证据，不回叫成「做完了」。"""
    body = await _body(request)
    runtime, err = _runtime_or_error()
    if err is not None:
        return err
    session = _session_of(body)
    if not session or not str(body.get("memberId") or "").strip():
        return _bad_request("ARGS_REQUIRED", "session 与 memberId 都必填")
    result = runtime.end_member_job(
        room=session,
        member_id=body.get("memberId"),
        job_id=str(body.get("jobId") or ""),
        status=str(body.get("status") or ""),
        reason=str(body.get("reason") or ""),
    )
    return _failed(result, status=409)


@router.post("/api/experts/rooms/stop")
async def rooms_stop(request: Request):
    """只停主持人本轮（D-34 验收项④）：成员照常跑完并回叫，不受牵连。"""
    body = await _body(request)
    runtime, err = _runtime_or_error()
    if err is not None:
        return err
    session = _session_of(body)
    if not session:
        return _bad_request("SESSION_REQUIRED", "缺 session：停止的范围就是主持人这一轮，得先说是哪一轮")
    result = runtime.stop_host_round(room=session, reason=str(body.get("reason") or ""))
    return _failed(result, status=409)


@router.get("/api/experts/rooms/pending")
async def rooms_pending(request: Request, session: str = "", limit: int = 50):
    """主持人还没读走的回叫——直接读耐久收件箱，重启后照样答得出。"""
    runtime, err = _runtime_or_error()
    if err is not None:
        return err
    if not session.strip():
        return _bad_request("SESSION_REQUIRED", "缺 session 参数")
    return JSONResponse(runtime.host_pending(room=session, limit=max(1, min(int(limit or 50), 200))))


@router.get("/api/experts/rooms/snapshot")
async def rooms_snapshot(request: Request):
    """编排层自述：编制数、在途房间、三份存储的落点与读取错误。"""
    runtime, err = _runtime_or_error()
    if err is not None:
        return err
    return JSONResponse({"ok": True, **runtime.snapshot()})


# ── 成员真运行时检查点（D-40.5 / #149）─────────────────────────
#
# 这一组端点**只读**：正文留在 checkpoints 目录里那一份文件，端点只报「有没有、
# 可不可续跑、多少条」。为什么不开写面：成员检查点由执行回路按领取逐轮落盘
# （`ticket_dispatch_runtime`），从外面往里写等于造出第二份事实源。


def _ledger_or_error() -> tuple[Any, JSONResponse | None]:
    runtime, err = _runtime_or_error()
    if err is not None:
        return None, err
    ledger = getattr(runtime, "checkpoints", None)
    if ledger is None:
        return None, JSONResponse({
            "ok": False,
            "code": "MEMBER_LEDGER_DISABLED",
            "message": (
                "这套装配没接成员检查点台账（TeamRuntime 建的时候 member_checkpoints=False）："
                "这里如实报不可用，不按「没有检查点」回答——两者的区别是"
                "「我没看」和「我看了说没有」"
            ),
        }, status_code=503)
    return ledger, None


@router.get("/api/checkpoint/members")
async def checkpoint_members(request: Request, session: str = ""):
    """房间里每个成员的运行时检查点概览（计数与可续跑性，不含正文）。"""
    ledger, err = _ledger_or_error()
    if err is not None:
        return err
    if not session.strip():
        return _bad_request("SESSION_REQUIRED", "缺 session 参数：成员检查点按房间问")
    return JSONResponse(ledger.room_view(session))


@router.get("/api/checkpoint/member")
async def checkpoint_member(request: Request, session: str = "", member: str = ""):
    """单个成员的检查点状态。"""
    ledger, err = _ledger_or_error()
    if err is not None:
        return err
    if not session.strip() or not member.strip():
        return _bad_request("ARGS_REQUIRED", "session 与 member 都必填：缺一个就不知道问的是谁的检查点")
    view = ledger.status_of(session, member)
    view["ok"] = True
    return JSONResponse(view)


@router.get("/api/checkpoint/member/carry")
async def checkpoint_member_carry(request: Request, session: str = "", member: str = ""):
    """下一次派工会接上哪一份旧检查点（**只预览，不改台账、不投递**）。

    界面要回答的是「现在重派这个人，他会从头做还是接着做」。真正派工时
    `TeamRuntime.dispatch` 会自己再问一次并把注记交给执行体——这条端点不预留、不消费，
    因为「已经被接走」这种状态一旦被预览写进台账，投递失败后就留下一条假记录。
    """
    ledger, err = _ledger_or_error()
    if err is not None:
        return err
    if not session.strip() or not member.strip():
        return _bad_request("ARGS_REQUIRED", "session 与 member 都必填")
    preview = ledger.carry_forward(room=session, member_id=member, next_assignment_id="")
    preview["note"] = (
        "这是预览：不消费、不改状态。派工时引擎会重新问一次，"
        "并把恢复注记随那份领取一起交给执行体"
    )
    return JSONResponse(preview)


@router.get("/api/checkpoint/snapshot")
async def checkpoint_snapshot(request: Request):
    """台账自述：索引了多少条、按状态分布、恢复注记由谁生成。"""
    ledger, err = _ledger_or_error()
    if err is not None:
        return err
    return JSONResponse({"ok": True, "ledger": ledger.snapshot()})


# ── 可视化工作流编排（D-10 / #156）─────────────────────────────
#
# 与上面两条车道同一份口径：**端点只保证投递与记账**。工作流的一步「投出去了」
# 不等于「跑完了」——跑没跑由 worker 调度器与桌面领取回路决定，异步结果由
# `/api/workflow/runs/report` 收。人工卡点的授权门读的是会话自己那份 `toolApproval`
# （约束 C：不新造开关），取不到会话就按缺省 off ⇒ 自动放行，并在运行视图里写
# `decidedBy: "policy-off"`，界面不许把它画成「人已批准」。


def _team_for_workflow() -> Any:
    """团队句柄只在**已经装配好**的时候给：这里不构造 TeamRuntime。

    构造它会连带要求执行体注册表（`TeamRuntime(workers=...)` 拒绝 None），而路由层
    拿不到引擎那一份装配现场；自建一份就是第二个槽位池与第二个内存箱（D-48 的口径）。
    没装配就返回 None，让 `team_dispatch` 那一步如实 blocked——比在 HTTP 层抛 500 好，
    因为blocked 会留在运行视图里，用户看得见「这一步为什么没跑」。
    """
    return tm.peek_runtime()


def _workers_for_workflow() -> Any:
    service = tws.peek_runtime()
    return getattr(service, "registry", None) if service is not None else None


def _approval_for_session(room: str) -> Any:
    """按房间读那一份审批档位；读不到就按缺省 off（并让运行视图说明是谁放的行）。

    这里**不吞异常**：`rl.engine()` 在车道没装配时会报错，那正是「读不出档位」而不是
    「档位是 off」——工作流层把它记成 blocked，不许按 off 静默放行（约束 C）。
    """
    key = str(room or "").strip()
    if not key:
        # snapshot 那类「不属于任何会话」的自述：没有房间就没有档位可查，如实给缺省 off。
        return tap.ToolApproval()
    sessions = getattr(rl.engine(), "_TASK_SESSIONS", None) or {}
    session = sessions.get(key)
    runtime = getattr(session, "runtime", None) if session is not None else None
    if isinstance(runtime, dict):
        return tap.ToolApproval.from_runtime(runtime)
    return tap.ToolApproval()


def _workflow_runtime() -> Any:
    return wfr.get_workflow_runtime(
        team_provider=_team_for_workflow,
        workers_provider=_workers_for_workflow,
        approval_provider=_approval_for_session,
    )


@router.get("/api/workflow/definitions")
async def workflow_definitions(request: Request):
    """本机全部工作流定义（带当前版本、步数、运行数与在跑条数）。"""
    return JSONResponse({"ok": True, "workflows": _workflow_runtime().list_definitions()})


@router.post("/api/workflow/definitions")
async def workflow_definitions_save(request: Request):
    """存一份定义（同 id 再存 = 升一版，旧版进有界历史）。校验不过一个字节都不落。"""
    body = await _body(request)
    raw = body.get("workflow") if isinstance(body.get("workflow"), dict) else body
    result = _workflow_runtime().save(raw)
    return _failed(result, status=422)


@router.post("/api/workflow/definitions/remove")
async def workflow_definitions_remove(request: Request):
    """删定义。还有运行挂在上面时拒绝；`force` 只用于明确承认那些运行留在盘上。"""
    body = await _body(request)
    result = _workflow_runtime().remove(
        str(body.get("workflowId") or body.get("id") or ""), force=bool(body.get("force"))
    )
    return _failed(result, status=422)


@router.get("/api/workflow/definitions/export")
async def workflow_definitions_export(request: Request, workflow: str = "", version: int = 0):
    """导出一份定义（纯定义，不含运行记录）；`version` 填了就是导那一版历史。"""
    if not workflow.strip():
        return _bad_request("WORKFLOW_REQUIRED", "缺 workflow 参数：导出要指名是哪一本")
    return _failed(_workflow_runtime().export(workflow, version=int(version or 0)), status=404)


@router.post("/api/workflow/definitions/import")
async def workflow_definitions_import(request: Request):
    """导入一份导出的定义。撞 id 时**换新 id**，不静默覆盖。"""
    body = await _body(request)
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else body
    result = _workflow_runtime().import_definition(payload, keep_id=bool(body.get("keepId", True)))
    return _failed(result, status=422)


@router.post("/api/workflow/runs/start")
async def workflow_runs_start(request: Request):
    """按定义起一次运行；`session` 就是房间（动作步的派工与卡点档位都从这里派生）。"""
    body = await _body(request)
    workflow = str(body.get("workflowId") or body.get("id") or "").strip()
    if not workflow:
        return _bad_request("WORKFLOW_REQUIRED", "缺 workflowId：不起定义就没有可运行的步骤")
    result = _workflow_runtime().start(
        workflow_id=workflow,
        room=_session_of(body),
        payload=body.get("payload") if isinstance(body.get("payload"), dict) else body.get("text", ""),
        title=str(body.get("title") or ""),
    )
    return _failed(result, status=422)


@router.post("/api/workflow/runs/advance")
async def workflow_runs_advance(request: Request):
    """手动推进一步（装配现场没有回合调度器时的兜底；有回执时不该靠这条）。"""
    body = await _body(request)
    run = str(body.get("runId") or "").strip()
    if not run:
        return _bad_request("RUN_REQUIRED", "缺 runId：推进要有明确的对象")
    return _failed(_workflow_runtime().advance(run), status=404)


@router.post("/api/workflow/runs/report")
async def workflow_runs_report(request: Request):
    """执行体对某一步的异步回执（把停在 `awaiting_step` 的运行往前推）。"""
    body = await _body(request)
    run = str(body.get("runId") or "").strip()
    step = str(body.get("stepId") or "").strip()
    if not run or not step:
        return _bad_request("ARGS_REQUIRED", "runId 与 stepId 都必填：缺一个就不知道这条回执属于哪一步")
    result = _workflow_runtime().report(
        run_id=run, step_id=step, ok=bool(body.get("ok", True)),
        output=str(body.get("output") or ""), error=str(body.get("error") or ""),
    )
    return _failed(result, status=404)


@router.post("/api/workflow/runs/approve")
async def workflow_runs_approve(request: Request):
    """人工卡点的结论（`allow` / `deny`）。只认停在「等人」的运行，重复点不再推进一步。"""
    body = await _body(request)
    run = str(body.get("runId") or "").strip()
    step = str(body.get("stepId") or "").strip()
    if not run or not step:
        return _bad_request("ARGS_REQUIRED", "runId 与 stepId 都必填：不能替一条不知道是哪一步的卡点给结论")
    result = _workflow_runtime().resolve_approval(
        run_id=run, step_id=step, decision=str(body.get("decision") or ""),
        note=str(body.get("note") or ""), decided_by=str(body.get("decidedBy") or "human"),
    )
    return _failed(result, status=404)


@router.post("/api/workflow/runs/cancel")
async def workflow_runs_cancel(request: Request):
    """取消一次运行。**已投出去的唤醒不回滚**——那条要靠工单自己的收口。"""
    body = await _body(request)
    run = str(body.get("runId") or "").strip()
    if not run:
        return _bad_request("RUN_REQUIRED", "缺 runId")
    result = _workflow_runtime().cancel(run_id=run, reason=str(body.get("reason") or ""))
    return _failed(result, status=404)


@router.get("/api/workflow/runs")
async def workflow_runs_list(request: Request, workflow: str = "", limit: int = 30):
    """每次运行的列表（D-10 验收项「查看每次运行」）；按开始时间倒序。"""
    return JSONResponse({"ok": True, "runs": _workflow_runtime().list_runs(
        workflow_id=workflow, limit=max(1, min(int(limit or 30), wfr.MAX_RUNS))
    )})


@router.get("/api/workflow/runs/view")
async def workflow_runs_view(request: Request, run: str = ""):
    """单次运行的逐步视图：每步状态、第几次尝试、产出摘要、卡点结论与来源。"""
    if not run.strip():
        return _bad_request("RUN_REQUIRED", "缺 run 参数")
    return _failed(_workflow_runtime().run_view(run), status=404)


@router.get("/api/workflow/pending")
async def workflow_pending(request: Request, limit: int = 50):
    """还没收口的运行（等人、等回执、装配缺件）——全读盘，重启后照样答得出。"""
    return JSONResponse(_workflow_runtime().pending(limit=max(1, min(int(limit or 50), wfr.MAX_RUNS))))


@router.get("/api/workflow/snapshot")
async def workflow_snapshot(request: Request):
    """工作流层自述：定义数、运行分布、两份存储落点，以及**三个句柄接没接**。"""
    return JSONResponse({"ok": True, **_workflow_runtime().snapshot()})
