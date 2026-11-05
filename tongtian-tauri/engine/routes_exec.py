"""exec 车道（W4）：执行面、沙箱、写暂存审批、人格模板。

占用前缀见 routes_lanes.LANE_PREFIXES["exec"]。/api/chat*、/api/terminal/*、
/api/sandbox/*、/api/tickets/* 全部留在 server.py（被多份契约逐字钉住，且属主线流程），
只有新能力写这里。

写暂存这一族（D-03.5，#161）的四条口径：

1. **判定不在这一层重做**。档位、暂存、diff、过期判定与合并全在
   ``write_staging_runtime``；本文件只做「把变更号递进去、把结论原样回话」。
2. **只对桌面壳开放**（含引擎没配凭据时的回环模式）。列待审阅与 ``change`` 会把
   整份待写入正文与目标绝对路径交给调用方，``apply`` 更是一次真实写盘——
   带远程身份的调用方既不该看见别人正在改的文件内容，也不该代人批准合并。
3. **业务性拒绝回 200 + ok=false + reason**（过期、找不到、位置越界都是用户在流程里
   会遇到的事，界面按 ``reason`` 说人话）；只有读不出来的意外才回 500 带错误码。
4. **这一族不改动「未批准的内容碰不到目标文件」这条底线**。所有写动作都在
   ``write_staging_runtime.apply`` 里重算指纹、重判位置之后才发生；本文件没有任何一条
   路径能绕过它直接写用户目录。

人格模板这一族（D-42 第一半，#161）另有一条口径要说明白：

- **不套桌面闸门**。写暂存经手的是「即将写进你文件里的正文」，模板经手的只是代码里的
  常量表（``persona_runtime.TEMPLATES``）与一份**还没保存的草稿**：既不含用户数据，
  也不写任何存储，所以按普通的引擎鉴权走就够了，拿 ``STAGING_DESKTOP_ONLY`` 拦它反而
  会让远程用户连「有哪些角色」都看不见。
- **引擎只出草稿，不调用 Rust 的 ``expert::save``**。专家注册表（``experts.registry``）
  的名额、上限与落盘只有一份来源，在桌面壳那边；这一族把校验提前做一遍，是为了让用户
  在点保存之前就看到拒绝理由，而不是被 Rust 弹回一句 ``EXPERT_INVALID:``。

遥测与崩溃两族（D-28，#141）各加一条口径，而且这两条互相不一样，别混着抄：

- **两族都只对桌面壳开放**，但理由不同，所以文案也不同（``_desktop_only`` 的
  ``family`` / ``why`` 就是为这个开的）。写暂存拦的是「别人替你决定写盘内容」，
  遥测拦的是「远程身份替本机点同意、并看见本机攒下的事件」，崩溃拦的是
  「把现场正文与一次真实导出交出去」。脱敏打的是凭据形状，拦不住「这台机器在哪、
  跑过什么」这类事实，所以崩溃族不能因为「已经脱敏」就放宽。
- **出口不能由 HTTP 设定**。``/api/exec/telemetry/consent`` 收到
  ``TELEMETRY_BODY_REFUSED_KEYS`` 里任何一个键就整条拒绝并说清原因——静默丢掉那些键
  比拒绝更坏：调用方会以为「发到哪」已经设上了。出口只在进程内登记
  （``telemetry_runtime.configure_sink``，由引擎启动路径调用）。
- **确认词只挡开启，不挡关闭**。带 ``word`` 才能开；关永远成功并且顺手删缓冲。
  被拒（没打对确认词）是流程内的正常结果，回 200 + ``ok=false`` + 中文 ``reason``。
- **崩溃族不接受 ``reportId="*"`` 的删除**。「一键清空崩溃记录」会把唯一留下的证据
  一次抹掉，而用户的下一步往往就是去问「为什么崩了」。导出（``*``）没有这个问题，
  它产的是副本、原件还在。
"""

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import artifact_history_runtime as ahr
import crash_runtime as cr
import extension_decls as exd
import persona_runtime as pr
import routes_lanes as rl
import telemetry_runtime as tr
import write_staging_runtime as wsr

LANE = "exec"
OWNER = rl.LANE_OWNERS["exec"]

router = APIRouter()

#: 本车道登记的全部路径（自测与分片都从这里取，别在两边各抄一份字符串）
EXEC_ENDPOINT_PATHS = (
    "/api/exec/extensions",
    "/api/staging/status",
    "/api/staging/pending",
    "/api/staging/change",
    "/api/staging/decisions",
    "/api/staging/apply",
    "/api/staging/reject",
    "/api/staging/discard",
    "/api/persona/templates",
    "/api/persona/template",
    "/api/persona/draft",
    "/api/persona/validate",
    "/api/persona/status",
    "/api/exec/telemetry/describe",
    "/api/exec/telemetry/status",
    "/api/exec/telemetry/preview",
    "/api/exec/telemetry/decisions",
    "/api/exec/telemetry/consent",
    "/api/exec/telemetry/flush",
    "/api/exec/telemetry/discard",
    "/api/exec/crash/describe",
    "/api/exec/crash/status",
    "/api/exec/crash/list",
    "/api/exec/crash/detail",
    "/api/exec/crash/export",
    "/api/exec/crash/delete",
)

#: 遥测同意端点**绝不接受**的键：能远程设定「发到哪」就等于让远程决定用户数据往哪走。
#: 出口只在进程内登记（``telemetry_runtime.configure_sink``），这里连读都不读。
TELEMETRY_BODY_REFUSED_KEYS = ("sink", "endpoint", "url", "destination", "target", "events", "fields")


def _fail(code: str, message: str, status: int = 400, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, "message": message, **extra}, status_code=status)


async def _body(request: Request) -> dict[str, Any]:
    """无效 JSON 与顶层不是对象的请求体都当「没带参数」，由调用方按缺参拒绝。"""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _query(request: Request, name: str, default: str = "") -> str:
    try:
        return str(request.query_params.get(name) or default).strip()
    except Exception:  # noqa: BLE001  极少数 ASGI 实现没有 query_params，退回空串
        return default


def _desktop_only(
    request: Request,
    what: str,
    *,
    code: str = "STAGING_DESKTOP_ONLY",
    family: str = "写暂存",
    why: str = (
        "这一族经手的是「即将写进你文件里的正文」与「批准合并」这个动作本身，"
        "交给远程身份等于让别人替你决定写盘内容；按人隔离的暂存区尚未落地，"
        "所以现在是整族不开放，而不是「只给查询」"
    ),
) -> JSONResponse | None:
    """只接受桌面壳（本机令牌）或无凭据回环模式；带远程身份一律 403。

    判定用引擎主模块里的同一个 ``_is_desktop_local``，不在这层复制第二份实现。
    ``family`` / ``why`` 让崩溃导出与遥测两族复用同一段拒绝逻辑而不必抄一份新文案——
    抄出来的第二份一定会和第一份说法不一致，而这三族的差别恰恰需要说清各自的理由。
    """
    eng = rl.engine()
    checker = getattr(eng, "_is_desktop_local", None)
    if callable(checker) and checker(request):
        return None
    principal = getattr(eng, "current_principal", None)
    who = principal() if callable(principal) else None
    if who is None:
        return None
    return JSONResponse(
        {
            "ok": False,
            "code": code,
            "message": (
                f"{family}的{what}只接受桌面壳（本机令牌）发起的请求："
                f"当前请求带着远程身份（sub={who.get('sub') or '未知'}）。{why}"
            ),
        },
        status_code=403,
    )


def _policy_for(request: Request, task_id: str) -> tuple[wsr.WriteStaging, str]:
    """按「会话 → 缺省」两级取档位，并如实说清用的是哪一级（缺省 = 直接写盘）。

    不从请求体收档位：档位是用户在设置页与输入框旁定的事，让 HTTP 调用方能临时改它，
    「先审后写」就变成了一句可以被绕过的建议。
    """
    eng = rl.engine()
    session_ref = _query(request, "session") or task_id
    sessions = getattr(eng, "_TASK_SESSIONS", None) or {}
    session = sessions.get(session_ref) if session_ref else None
    if session is not None:
        runtime = dict(getattr(session, "runtime", {}) or {})
        return wsr.WriteStaging.from_runtime(runtime), f"档位取自会话 {session_ref} 的 runtime"
    return wsr.WriteStaging.from_runtime({}), "会话不在本引擎进程的会话表里，档位按缺省（直接写盘）报告"


@router.get("/api/exec/extensions")
async def exec_extensions(request: Request):
    """「这个扩展声明了什么能力」清单（D-08）。

    只读引擎**本轮从桌面端 runtime 摊到的读数**：不接受调用方给目录，也不去盘上扫扩展包，
    所以清单不会把「装了但本轮没进执行面」的东西说成可用。还没跑过任务时如实返回
    `observed=false` 加一条说明，不伪造一份空清单。
    """
    try:
        return JSONResponse(exd.snapshot())
    except Exception as exc:  # noqa: BLE001  读数失败要说清是哪一步，不能让界面显示「无扩展」
        return JSONResponse(
            {
                "ok": False,
                "code": "EXEC_EXTENSIONS_FAILED",
                "message": f"扩展声明清单读取失败：{type(exc).__name__}：{str(exc)[:200]}",
            },
            status_code=500,
        )


# ── D-03.5 写暂存：待审阅清单 / diff / 合并 / 拒绝 ────────────────


@router.get("/api/staging/status")
async def staging_status(request: Request):
    """档位读数 + 待审阅条数：界面开场一次拿全，不用先猜模型这轮有没有开暂存。"""
    blocked = _desktop_only(request, "状态查询")
    if blocked is not None:
        return blocked
    task_id = _query(request, "taskId")
    try:
        policy, source = _policy_for(request, task_id)
        body = wsr.report(policy, task_id=task_id)
        body["ok"] = True
        body["policySource"] = source
        body["stagingDir"] = str(wsr.staging_home())
        return JSONResponse(body)
    except Exception as exc:  # noqa: BLE001
        return _fail("STAGING_STATUS_FAILED", f"写暂存状态读取失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.get("/api/staging/pending")
async def staging_pending(request: Request):
    """列待审阅（含已过期那条）。只读，不碰目标文件，也不改记录状态。"""
    blocked = _desktop_only(request, "待审阅清单读取")
    if blocked is not None:
        return blocked
    try:
        listing = wsr.pending(
            task_id=_query(request, "taskId"),
            limit=_query(request, "limit", str(wsr.DEFAULT_LIST_LIMIT)),
        )
        return JSONResponse(listing)
    except Exception as exc:  # noqa: BLE001
        return _fail("STAGING_LIST_FAILED", f"待审阅清单读取失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.get("/api/staging/change")
async def staging_change(request: Request):
    """单条详情：diff 正文 + 即将写入的完整内容（点开审批界面用）。"""
    blocked = _desktop_only(request, "改动详情读取")
    if blocked is not None:
        return blocked
    change_id = _query(request, "changeId")
    if not change_id:
        return _fail("STAGING_BAD_REQUEST", "缺少 changeId：没给变更号就不知道该看哪一条待审阅")
    try:
        detail = wsr.detail(change_id)
        if not detail.get("ok"):
            # 找不到记录是流程内的正常结果（上一条刚被合并掉），回 200 让界面照常说人话
            return JSONResponse({"ok": False, "reason": detail.get("reason", "")})
        return JSONResponse(detail)
    except Exception as exc:  # noqa: BLE001
        return _fail("STAGING_DETAIL_FAILED", f"改动详情读取失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.get("/api/staging/decisions")
async def staging_decisions(request: Request):
    """决定日志（合并/拒绝/过期/越权拦下）。日志只有指纹与字节数，不含正文。"""
    blocked = _desktop_only(request, "决定日志读取")
    if blocked is not None:
        return blocked
    try:
        return JSONResponse(wsr.decisions(
            limit=_query(request, "limit", "50"),
            change_id=_query(request, "changeId"),
        ))
    except Exception as exc:  # noqa: BLE001
        return _fail("STAGING_DECISIONS_FAILED", f"决定日志读取失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.post("/api/staging/apply")
async def staging_apply(request: Request):
    """批准并合并：这一步之后目标文件才被真正写入。

    合并前 ``write_staging_runtime.apply`` 会重算目标指纹（在暂存之后被改过就拒绝并要求
    模型重写）、按记录里的工作目录重判位置、比对暂存内容指纹，任一条不过都什么都不写。
    产物版本历史句柄与 ``build_toolkit`` 的 ``artifact_history`` 同源（server.py 里也是无条件
    传模块本体），所以这里同样直接传：留档失败不挡用户已经批准的合并，但原因会进 ``historyNote``。
    """
    blocked = _desktop_only(request, "合并写盘")
    if blocked is not None:
        return blocked
    payload = await _body(request)
    change_id = str(payload.get("changeId") or payload.get("change_id") or "").strip()
    if not change_id:
        return _fail("STAGING_BAD_REQUEST", "缺少 changeId：没有变更号不知道该合并哪一条")
    try:
        result = wsr.apply(change_id, history=ahr)
        return JSONResponse(result)
    except Exception as exc:  # noqa: BLE001  意外才 500；业务拒绝由 apply 自己回 ok=false
        return _fail("STAGING_APPLY_FAILED", f"合并过程出错，目标文件未被改动：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.post("/api/staging/reject")
async def staging_reject(request: Request):
    """拒绝：丢掉暂存内容，目标文件从头到尾没被碰过。"""
    blocked = _desktop_only(request, "拒绝改动")
    if blocked is not None:
        return blocked
    payload = await _body(request)
    change_id = str(payload.get("changeId") or payload.get("change_id") or "").strip()
    if not change_id:
        return _fail("STAGING_BAD_REQUEST", "缺少 changeId：没有变更号不知道该拒哪一条")
    reason = str(payload.get("reason") or "")
    try:
        return JSONResponse(wsr.reject(change_id, reason))
    except Exception as exc:  # noqa: BLE001
        return _fail("STAGING_REJECT_FAILED", f"拒绝处理出错：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.post("/api/staging/discard")
async def staging_discard(request: Request):
    """任务收尾：批量丢掉待审阅（不传 taskId 就是全量）。同样一个目标文件都不碰。"""
    blocked = _desktop_only(request, "清空待审阅")
    if blocked is not None:
        return blocked
    payload = await _body(request)
    task_id = str(payload.get("taskId") or payload.get("task_id") or "").strip()
    try:
        return JSONResponse(wsr.discard_all(task_id))
    except Exception as exc:  # noqa: BLE001
        return _fail("STAGING_DISCARD_FAILED", f"清空待审阅出错：{type(exc).__name__}：{str(exc)[:200]}", 500)


# ── D-42 人格模板：清单 / 单个 / 草稿 / 校验 / 读数 ────────────────


@router.get("/api/persona/templates")
async def persona_templates(request: Request):
    """人格模板清单（可选按气质组过滤）。

    分组写错**不回空表就完事**：``ok=false`` + 一条说明「可用分组有哪些」，
    否则界面只会显示「暂无模板」，用户分不清是库空了还是自己传错了参数。
    """
    group = _query(request, "group")
    try:
        if group and group not in pr.GROUPS:
            return JSONResponse({
                "ok": False,
                "reason": (
                    f"没有「{group[:40]}」这个分组；可用分组："
                    f"{'、'.join(pr.GROUPS)}（不分组查询就别带 group）"
                ),
                "group": group,
                "groups": list(pr.GROUPS),
                "templates": [],
                "total": 0,
            })
        items = pr.list_templates(group)
        return JSONResponse({
            "ok": True,
            "group": group,
            "groups": list(pr.GROUPS),
            "templates": items,
            "total": len(items),
            "limits": pr.catalog_status()["limits"],
            "note": (
                f"共 {len(pr.TEMPLATES)} 套模板"
                + (f"，本组 {len(items)} 套" if group else "")
                + "；模板在代码里（engine/persona_runtime.py），改它不发版做不到"
            ),
        })
    except Exception as exc:  # noqa: BLE001
        return _fail("PERSONA_TEMPLATES_FAILED", f"人格模板清单读取失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.get("/api/persona/template")
async def persona_template(request: Request):
    """单套模板全文（含合成好的提示词）：界面点开一张卡看详情用。"""
    template_id = _query(request, "templateId")
    if not template_id:
        return _fail("PERSONA_BAD_REQUEST", "缺少 templateId：没给模板号就不知道该返回哪套人格")
    try:
        tpl = pr.get_template(template_id)
        if tpl is None:
            return JSONResponse({
                "ok": False,
                "reason": (
                    f"没有这个人格模板「{template_id[:40]}」；"
                    f"可用：{'、'.join(pr.template_ids())}"
                ),
                "template": {},
                "templateIds": pr.template_ids(),
            })
        return JSONResponse({"ok": True, "template": tpl})
    except Exception as exc:  # noqa: BLE001
        return _fail("PERSONA_TEMPLATE_FAILED", f"人格模板读取失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.post("/api/persona/draft")
async def persona_draft(request: Request):
    """一键创建的引擎那一半：把模板 + 用户追加的话合成一份**专家草稿**。

    这里的 ``ok=false`` 是流程内的正常结果（模板号传错、名字填长了），照旧回 200；
    草稿不回注册表名额，也不保证 ``id`` 唯一——查重由界面上现有的「新建专家」负责。
    """
    payload = await _body(request)
    template_id = str(payload.get("templateId") or payload.get("template_id") or "").strip()
    if not template_id:
        return _fail("PERSONA_BAD_REQUEST", "缺少 templateId：没有模板号就生成不出草稿")
    try:
        draft = pr.draft_from_template(
            template_id,
            name=str(payload.get("name") or ""),
            description=str(payload.get("description") or ""),
            add_prompt=str(payload.get("addPrompt") or payload.get("add_prompt") or ""),
            expert_id=str(payload.get("expertId") or payload.get("expert_id") or ""),
        )
        draft["saved"] = False
        return JSONResponse(draft)
    except Exception as exc:  # noqa: BLE001
        return _fail("PERSONA_DRAFT_FAILED", f"草稿生成出错：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.post("/api/persona/validate")
async def persona_validate(request: Request):
    """按 Rust `expert::save` 的同一组上限预校验一份草稿。

    接受两种形状：草稿本身（``{"id":..., "name":...}``）或包一层（``{"expert":{...}}``）——
    界面从 ``/api/persona/draft`` 拿到的就是后一种，直接回传也该能用。
    """
    payload = await _body(request)
    candidate = payload.get("expert")
    draft = candidate if isinstance(candidate, dict) else payload
    if not draft:
        return _fail("PERSONA_BAD_REQUEST", "没带草稿：校验需要 expert（或顶层直接给 id/name/description/prompt）")
    try:
        problems = pr.validate_expert_draft(draft)
        notes = pr.normalization_notes(draft)
        return JSONResponse({
            "ok": not problems,
            "problems": problems,
            "notes": notes,
            "limits": pr.catalog_status()["limits"],
            "reason": "" if not problems else "；".join(problems),
            "note": (
                "校验只读不写：通过了也不代表已保存，保存仍然是界面调用现有「新建专家」那一下"
                + (f"；另有 {len(notes)} 条归一化提醒" if notes else "")
                if not problems
                else "以下问题会让 Rust 侧 expert::save 直接拒绝，界面按条说人话即可"
            ),
        })
    except Exception as exc:  # noqa: BLE001
        return _fail("PERSONA_VALIDATE_FAILED", f"草稿校验出错：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.get("/api/persona/status")
async def persona_status(request: Request):
    """模板库读数：有几套、分组怎么分布、哪一半还没接线（``unverified``）。"""
    try:
        return JSONResponse(pr.catalog_status())
    except Exception as exc:  # noqa: BLE001
        return _fail("PERSONA_STATUS_FAILED", f"人格模板读数失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


# ── D-28 前半：可选遥测（默认关、确认词、看得见将发送什么）────────────


def _telemetry_gate(request: Request, what: str) -> JSONResponse | None:
    """遥测整族只对桌面壳开放：同意状态是**这台机器**的事。

    理由与写暂存不同，所以文案也不同——这里经手的不是「即将写进文件的正文」，
    而是「这台机器的数据要不要发出去、以及发给谁的意愿」。让远程身份替本机点同意，
    比让远程身份看一份 diff 严重得多；而按人隔离的同意状态同样还没落地，
    所以现在整族不开放，而不是「只给查询」。
    """
    return _desktop_only(
        request,
        what,
        code="TELEMETRY_DESKTOP_ONLY",
        family="可选遥测",
        why=(
            "同意状态与本地缓冲都是**这台机器**的数据：远程身份既能替本机点「同意」，"
            "也能看见本机攒下的每一条事件。按人隔离的同意状态尚未落地，"
            "所以现在是整族不开放，而不是「只给查询」"
        ),
    )


@router.get("/api/exec/telemetry/describe")
async def telemetry_describe(request: Request):
    """字段白名单、确认词、上限与有没有出口：界面在用户点开关之前就该把这些摊开。"""
    blocked = _telemetry_gate(request, "口径说明读取")
    if blocked is not None:
        return blocked
    try:
        return JSONResponse(tr.describe())
    except Exception as exc:  # noqa: BLE001
        return _fail("TELEMETRY_DESCRIBE_FAILED", f"遥测口径读取失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.get("/api/exec/telemetry/status")
async def telemetry_status(request: Request):
    """一眼读数：开没开、缓冲里几条、计数器、出口状态（不含任何事件正文）。"""
    blocked = _telemetry_gate(request, "状态查询")
    if blocked is not None:
        return blocked
    try:
        return JSONResponse(tr.status())
    except Exception as exc:  # noqa: BLE001
        return _fail("TELEMETRY_STATUS_FAILED", f"遥测状态读取失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.get("/api/exec/telemetry/preview")
async def telemetry_preview(request: Request):
    """**将发送内容的逐字预览**：与 ``flush`` 交给出口的那份数组同源，不是另写的展示版。"""
    blocked = _telemetry_gate(request, "外发预览")
    if blocked is not None:
        return blocked
    try:
        return JSONResponse(tr.preview(_query(request, "limit", str(tr.DEFAULT_PREVIEW_LIMIT))))
    except Exception as exc:  # noqa: BLE001
        return _fail("TELEMETRY_PREVIEW_FAILED", f"外发预览读取失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.get("/api/exec/telemetry/decisions")
async def telemetry_decisions(request: Request):
    """同意侧的本地审计：开启 / 关闭 / 被拒的尝试。只在本机读，且不含内容本体。"""
    blocked = _telemetry_gate(request, "同意审计读取")
    if blocked is not None:
        return blocked
    try:
        return JSONResponse(tr.decisions(_query(request, "limit", "30")))
    except Exception as exc:  # noqa: BLE001
        return _fail("TELEMETRY_DECISIONS_FAILED", f"同意审计读取失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.post("/api/exec/telemetry/consent")
async def telemetry_consent(request: Request):
    """改同意状态：开 = 必须带确认词；关 = 无条件允许，且当场删掉本地缓冲。

    这一族**不接受**任何调用方指定出口（见 ``TELEMETRY_BODY_REFUSED_KEYS``）：出口只在
    进程内登记。带这些键来一律整条拒绝并说清为什么——静默丢掉比拒绝更容易让人以为设上了。
    """
    blocked = _telemetry_gate(request, "同意状态修改")
    if blocked is not None:
        return blocked
    payload = await _body(request)
    refused = [key for key in TELEMETRY_BODY_REFUSED_KEYS if key in payload]
    if refused:
        return JSONResponse(
            {
                "ok": False,
                "code": "TELEMETRY_EGRESS_NOT_SETTABLE",
                "message": (
                    f"遥测的出口不能由 HTTP 调用方设定（收到的 {('、'.join(refused))} 一律不接受）："
                    "「发到哪」只在引擎进程内登记，能远程设定它就等于让远程决定本机数据往哪走。"
                    "要改的只有同意状态本身：{on: true|false, word: 确认词}"
                ),
            },
            status_code=400,
        )
    if "on" not in payload:
        return _fail("TELEMETRY_BAD_REQUEST", "缺少 on：没给方向就不知道该开启还是关闭遥测")
    try:
        result = tr.set_consent(
            bool(payload.get("on")),
            word=str(payload.get("word") or ""),
            via=str(payload.get("via") or "http")[:60],
        )
        # 被拒（确认词不对）是流程内会遇到的正常结果：回 200 + ok=false + reason，
        # 让界面直接念那句话，而不是把「用户没打对字」报成一次服务故障
        return JSONResponse({"ok": bool(result.get("ok")), **result})
    except Exception as exc:  # noqa: BLE001
        return _fail("TELEMETRY_CONSENT_FAILED", f"同意状态修改失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.post("/api/exec/telemetry/flush")
async def telemetry_flush(request: Request):
    """试着把缓冲交给出口；**没有出口就照实回未发送**，一条也不删、不虚报。"""
    blocked = _telemetry_gate(request, "外发动作")
    if blocked is not None:
        return blocked
    payload = await _body(request)
    try:
        result = tr.flush(limit=payload.get("limit", tr.FLUSH_BATCH))
        return JSONResponse({"ok": bool(result.get("ok")), **result})
    except Exception as exc:  # noqa: BLE001
        return _fail("TELEMETRY_FLUSH_FAILED", f"遥测外发尝试失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.post("/api/exec/telemetry/discard")
async def telemetry_discard(request: Request):
    """清掉本地缓冲（不改同意状态）：这是「先把攒下的删掉」那条退出面。"""
    blocked = _telemetry_gate(request, "缓冲清理")
    if blocked is not None:
        return blocked
    try:
        result = tr.discard_all()
        return JSONResponse({"ok": bool(result.get("ok")), **result})
    except Exception as exc:  # noqa: BLE001
        return _fail("TELEMETRY_DISCARD_FAILED", f"本地缓冲清理失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


# ── D-28 后半：崩溃现场留存与导出 ──────────────────────────────


def _crash_gate(request: Request, what: str) -> JSONResponse | None:
    """崩溃族只对桌面壳开放：列表与详情会把现场正文（含绝对路径与任务号）交出去。

    已经脱敏不等于可以给陌生人看——脱敏打的是凭据形状，拦不住「你在用什么目录、
    哪条任务崩了」这类事实。导出更是把整份现场写成文件，只能由本机发起。
    """
    return _desktop_only(
        request,
        what,
        code="CRASH_DESKTOP_ONLY",
        family="崩溃现场",
        why=(
            "列表与详情会把现场正文（ traceback、绝对路径、任务号）交给调用方，"
            "导出更是一次真实写盘；脱敏打的是凭据形状，拦不住「这台机器在哪、"
            "跑过什么」这类事实，所以远程身份既不该看见也不该代人导出——"
            "而按人隔离的现场归属尚未落地，所以现在整族不开放，而不是「只给查询」"
        ),
    )


@router.get("/api/exec/crash/describe")
async def crash_describe(request: Request):
    """留存口径：目录、上限、类别表、脱敏规则名与钩子状态（不含任何现场正文）。"""
    blocked = _crash_gate(request, "口径说明读取")
    if blocked is not None:
        return blocked
    try:
        return JSONResponse(cr.describe())
    except Exception as exc:  # noqa: BLE001
        return _fail("CRASH_DESCRIBE_FAILED", f"崩溃口径读取失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.get("/api/exec/crash/status")
async def crash_status(request: Request):
    """一眼读数：崩过几份、有几份还没导出。界面据此提示「本机崩过 N 次，可导出」。"""
    blocked = _crash_gate(request, "状态查询")
    if blocked is not None:
        return blocked
    try:
        body = cr.status()
        body["ok"] = True
        body["hooks"] = cr.installed_state()
        return JSONResponse(body)
    except Exception as exc:  # noqa: BLE001
        return _fail("CRASH_STATUS_FAILED", f"崩溃状态读取失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.get("/api/exec/crash/list")
async def crash_list(request: Request):
    """现场列表（不含 traceback 正文）。只读，不改任何记录状态。"""
    blocked = _crash_gate(request, "现场列表读取")
    if blocked is not None:
        return blocked
    try:
        return JSONResponse(cr.list_reports(_query(request, "limit", str(cr.DEFAULT_LIST_LIMIT))))
    except Exception as exc:  # noqa: BLE001
        return _fail("CRASH_LIST_FAILED", f"崩溃现场列表读取失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.get("/api/exec/crash/detail")
async def crash_detail(request: Request):
    """单条详情（含已脱敏的 traceback 正文）。"""
    blocked = _crash_gate(request, "现场详情读取")
    if blocked is not None:
        return blocked
    report_id = _query(request, "reportId")
    if not report_id:
        return _fail("CRASH_BAD_REQUEST", "缺少 reportId：没给编号就不知道该看哪一份现场")
    try:
        detail = cr.detail(report_id)
        if not detail.get("ok"):
            # 找不到（刚被删掉）是流程内的正常结果：回 200 让界面按 reason 说人话
            return JSONResponse({"ok": False, "reason": detail.get("reason", "")})
        return JSONResponse(detail)
    except Exception as exc:  # noqa: BLE001
        return _fail("CRASH_DETAIL_FAILED", f"崩溃现场详情读取失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.post("/api/exec/crash/export")
async def crash_export(request: Request):
    """导出一份可附到问题报告里的文本副本；**不删原件**，``reportId`` 缺省导全部。"""
    blocked = _crash_gate(request, "导出现场")
    if blocked is not None:
        return blocked
    payload = await _body(request)
    try:
        result = cr.export(str(payload.get("reportId") or "*"))
        return JSONResponse({"ok": bool(result.get("ok")), **result})
    except Exception as exc:  # noqa: BLE001
        return _fail("CRASH_EXPORT_FAILED", f"崩溃现场导出失败：{type(exc).__name__}：{str(exc)[:200]}", 500)


@router.post("/api/exec/crash/delete")
async def crash_delete(request: Request):
    """删掉一份现场。**必须给具体编号**：不接受 `*`，「一键清空崩溃记录」这种按钮
    会把唯一留下的证据一次抹掉，而用户下一步往往就是去问「为什么崩了」。"""
    blocked = _crash_gate(request, "删除现场")
    if blocked is not None:
        return blocked
    payload = await _body(request)
    report_id = str(payload.get("reportId") or "").strip()
    if not report_id:
        return _fail("CRASH_BAD_REQUEST", "缺少 reportId：删除必须指名道姓，不接受整批清理")
    if report_id == "*":
        return JSONResponse(
            {
                "ok": False,
                "reason": (
                    "不接受一次删光全部崩溃现场：这些是「本机崩过」的唯一证据，"
                    "要清理就逐条删（先导出再看要不要留）。整批删除会把排查的下一步彻底堵死"
                ),
            },
        )
    try:
        result = cr.delete(report_id)
        return JSONResponse({"ok": bool(result.get("ok")), **result})
    except Exception as exc:  # noqa: BLE001
        return _fail("CRASH_DELETE_FAILED", f"崩溃现场删除失败：{type(exc).__name__}：{str(exc)[:200]}", 500)
