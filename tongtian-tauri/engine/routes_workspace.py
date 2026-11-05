"""workspace 车道（W3）：#157 工作区后端抽象 + #140 应用快照与浏览器上下文接力的引擎端点面。

占用前缀见 ``routes_lanes.LANE_PREFIXES["workspace"]``：``/api/workspace/*`` 是 #157
那 11 条，``/api/snapshot/*`` 是 #140 那 12 条（快照流水线 + 接力边界），``/api/storage/*``
暂未占用。所有路径在 ``ENDPOINT_PATHS`` 里逐条登记，自测按它核对——车道路径散在装饰器里，
别人看不出这条车道到底占了哪些名字。

六条口径写在这儿，读端点时不用猜：

1. **整条车道只对本机桌面壳开放**（含「引擎完全没配凭据」的回环模式）。
   理由不是省事：后端清单是一份**进程级全局**的落点表（桶名、数据库 DSN 字段、SSH 主机
   与端口都在里面），本卡没有做按人隔离的第二份清单。带远程身份的请求既能看见别人的
   基础设施目标，也能把活动后端切走、影响别人的回合。所以读写面统一走 server.py 已有的
   ``_is_desktop_local`` + ``current_principal`` 判定，判定不过就明说「服务化按人隔离未落地」。
   快照这半边同样收在这道门后面：屏幕内容比后端清单更不该被远程身份读走。
2. **判定不在这一层重做**。三段判定（后端边界 / 工作区沙箱白名单 / 文件访问档位）全在
   ``workspace_backend_runtime.decide_access`` 里；分级与上送门全在
   ``snapshot_runtime`` 里；接力边界全在 ``snapshot_relay`` 里。本文件只负责把现场递进去、
   把结论原样回话。所以档位拒绝和沙箱拒绝天生是两句话，不会被这里合并成一句文案；
   回话里的 ``deniedBy`` 就是那三选一的来源，界面按它选文案，不要按 code 猜。
3. **探测草稿不进清单**。设置页的「测试连接」可以带一份还没保存的配置来探测，
   这条路只 ``parse_spec`` → ``build_backend`` → ``probe()``，绝不 ``REGISTRY.add()``：
   带病配置一旦进了清单就切得过去。
4. **审计只有各自那一份，都不落正文**。后端读写与执行/切换的审计由
   ``WorkspaceBackend._op`` 写（JSONL，只落字节数与内容指纹，不落正文与密钥）；
   快照的决策审计由 ``SnapshotRegistry._record`` 写，接力的由 ``RelayRegistry._record`` 写，
   同样只有长度、指纹、域名与结论。本文件只提供读面，不另记第二份账。
5. **车道不截屏**。``/api/snapshot/capture`` 收的是调用方交上来的采集物（文本 / base64 图），
   OS 捕获与全局快捷键在桌面壳里，而 workspace 车道的 IPC 还没交棒 ⇒ 真机采集**未验收**，
   回话里逐条点名，不许读成「应用快照已可用」。
6. **车道不发起浏览器动作**。``/api/snapshot/relay/*`` 只回答「这次能不能复用已授权会话」，
   开/切/关标签页与点击输入都在 W5 的 ``/api/browser`` 车道与 ``engine/browser_use.py`` 里。

凭据面：配置只接受 ``env:`` / ``keychain:`` 引用，本文件不从请求体里收任何凭据值，
也不从请求体里收 ``environ``（那等于把「谁的密钥」交给 HTTP 调用方决定）。
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import routes_lanes as rl
import sandbox_runtime as sr
import snapshot_relay as relay
import snapshot_runtime as snap
import workspace_backend_runtime as wb

LANE = "workspace"
OWNER = rl.LANE_OWNERS["workspace"]

router = APIRouter()

#: 本车道登记的全部路径（自测与分片都从这里取，别在两边各抄一份字符串）
WORKSPACE_ENDPOINT_PATHS = (
    "/api/workspace/backends",
    "/api/workspace/backends/configure",
    "/api/workspace/backends/probe",
    "/api/workspace/switch",
    "/api/workspace/audit",
    "/api/workspace/files/list",
    "/api/workspace/files/read",
    "/api/workspace/files/write",
    "/api/workspace/files/delete",
    "/api/workspace/files/move",
    "/api/workspace/exec",
)

#: #140：快照流水线（触发/预览/确认/取消/决策审计）+ 接力边界（授权/判定/登记/撤销）
SNAPSHOT_ENDPOINT_PATHS = (
    "/api/snapshot/status",
    "/api/snapshot/capture",
    "/api/snapshot/pending",
    "/api/snapshot/preview",
    "/api/snapshot/confirm",
    "/api/snapshot/cancel",
    "/api/snapshot/decisions",
    "/api/snapshot/relay/status",
    "/api/snapshot/relay/grant",
    "/api/snapshot/relay/decide",
    "/api/snapshot/relay/tabs",
    "/api/snapshot/relay/revoke",
)

ENDPOINT_PATHS = WORKSPACE_ENDPOINT_PATHS + SNAPSHOT_ENDPOINT_PATHS

#: 只读的那几条（GET）；其余一律 POST，写面不靠 GET
READ_ONLY_PATHS = (
    "/api/workspace/backends",
    "/api/workspace/audit",
    "/api/snapshot/status",
    "/api/snapshot/pending",
    "/api/snapshot/preview",
    "/api/snapshot/decisions",
    "/api/snapshot/relay/status",
)

#: 操作面（会碰后端的那几条）——自测按它逐条验「远程身份一律拒」
OP_PATHS = (
    "/api/workspace/files/list",
    "/api/workspace/files/read",
    "/api/workspace/files/write",
    "/api/workspace/files/delete",
    "/api/workspace/files/move",
    "/api/workspace/exec",
)

#: 三段拒绝来源 → 给人看的名字。界面拿它当文案前缀，`deniedBy` 才是机器可读的那一份。
DENIAL_SOURCE_LABELS = {
    wb.SOURCE_BACKEND_BOUNDARY: "工作区后端边界（根前缀）",
    wb.SOURCE_BACKEND: "后端设置（只读开关 / 远程删除开关 / 路径本身）",
    wb.SOURCE_SANDBOX: "工作区沙箱白名单",
    wb.SOURCE_FILE_PERMISSION: "文件访问档位",
}

#: 结论 → HTTP 状态码。拒绝归 403、够不着归 503，别让界面把「没连上」读成「不让做」。
_STATUS_BY_CODE = {
    wb.CODE_SPEC_INVALID: 400,
    wb.CODE_PATH_OUTSIDE_BACKEND: 403,
    wb.CODE_PATH_UNSAFE: 403,
    wb.CODE_BACKEND_READONLY: 403,
    wb.CODE_TIER_DENIES_REMOTE: 403,
    wb.CODE_DELETE_DENIED: 403,
    wb.CODE_DELETE_NOT_FULL_ACCESS: 403,
    wb.CODE_CAPABILITY_DENIED: 403,
    wb.CODE_CREDENTIAL_LITERAL: 400,
    wb.CODE_CREDENTIAL_UNRESOLVED: 503,
    wb.CODE_DRIVER_MISSING: 503,
    wb.CODE_TRANSPORT_MISSING: 503,
    wb.CODE_BACKEND_UNAVAILABLE: 503,
    wb.CODE_BACKEND_NOT_FOUND: 404,
    wb.CODE_BACKEND_REQUIRED: 400,
    wb.CODE_IMAGE_INVALID: 400,
    wb.CODE_NETWORK_INVALID: 400,
    wb.CODE_LIMIT_INVALID: 400,
    wb.CODE_INTERNAL: 500,
}


# ── 小件 ────────────────────────────────────────────────────────


def _fail(code: str, message: str, status: int = 400, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, "message": message, **extra}, status_code=status)


async def _body(request: Request) -> dict[str, Any]:
    """无效 JSON 与顶层不是对象的请求体都当「没带参数」，由调用方按缺参拒绝。"""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


#: 同一条准入闸服务两族端点，但「为什么这一族不能给远程身份」是两个不同的理由：
#: 共用一句会让远程调用方以为快照也在讲桶名与 SSH 主机。code 同源，文案分族。
DESKTOP_ONLY_REASONS = {
    "workspace": (
        "工作区后端清单与落点切换只接受桌面壳（本机令牌）发起的请求："
        "当前请求带着远程身份（sub={sub}），而这份清单是进程级全局的——桶名、数据库端点与 SSH 主机都在里面，"
        "切换还会改变后续回合的落点。按人隔离的后端清单尚未落地（见 docs/并行实施/157.md 的未验收条目），"
        "所以这一档现在是整条车道不开放，而不是「只给查询」"
    ),
    "snapshot": (
        "应用快照与浏览器上下文接力只接受桌面壳（本机令牌）发起的请求："
        "当前请求带着远程身份（sub={sub}），而这条通道经手的是屏幕上的正文、截图与受控浏览器的域名清单——"
        "把它们交给一个远程身份等于把别人屏幕上看到的东西端出去。"
        "按人隔离尚未落地（同 docs/并行实施/157.md 的未验收条目），所以这一族现在是整条不开放，"
        "而不是「只给查询」：查询这一半拿到的就是屏幕内容"
    ),
}


def _desktop_only(request: Request, family: str = "workspace") -> JSONResponse | None:
    """本车道的准入判定：桌面壳（本机令牌）或无凭据回环模式放行，带远程身份一律 403。"""
    eng = rl.engine()
    principal = eng.current_principal()
    if _is_local(request, eng) or principal is None:
        return None
    template = DESKTOP_ONLY_REASONS.get(family) or DESKTOP_ONLY_REASONS["workspace"]
    return JSONResponse(
        {
            "ok": False,
            "code": wb.CODE_DESKTOP_ONLY,
            "message": template.format(sub=principal.get("sub") or "未知"),
        },
        status_code=403,
    )


def _is_local(request: Request, eng: Any) -> bool:
    """取引擎主模块里的本机判定（不复制一份实现：两份实现迟早对不上）。"""
    checker = getattr(eng, "_is_desktop_local", None)
    return bool(checker(request)) if callable(checker) else False


def _policy_for(payload: dict[str, Any]) -> tuple[sr.SandboxPolicy, dict[str, Any]]:
    """按「会话 → 请求体自带 runtime → 缺省」三级取档位与沙箱，并如实说清用的是哪一级。

    缺省那一级是**最保守**的（``ask_every_time``，没有沙箱白名单）：
    宁可让界面多弹一次确认，也不能因为调用方没带 runtime 就按「完全访问」放开写盘与删除。
    """
    eng = rl.engine()
    runtime: dict[str, Any] = {}
    source = "缺省（未带 runtime/会话，按最保守档位 ask_every_time）"
    work_dir = getattr(eng, "WORK_DIR", None)
    session_ref = str(payload.get("session") or payload.get("sessionId") or "").strip()
    if session_ref:
        sessions = getattr(eng, "_TASK_SESSIONS", None) or {}
        session = sessions.get(session_ref)
        if session is None:
            # 会话不在这台引擎进程里：不回退到「猜一份宽档位」，也不硬拒——
            # 回退到缺省档位并在回话里点名，界面能把「这次没按你那轮的档位判」讲清楚。
            source = f"会话 {session_ref} 不在本引擎进程的会话表里，已回退缺省（最保守）档位"
        else:
            runtime = dict(getattr(session, "runtime", {}) or {})
            session_work_dir = getattr(session, "work_dir", None)
            if session_work_dir:
                work_dir = session_work_dir
            source = f"会话 {session_ref} 随轮下发的 runtime"
    elif isinstance(payload.get("runtime"), dict):
        runtime = dict(payload["runtime"])
        source = "请求体自带的 runtime（桌面壳随轮下发的那份）"
    policy = sr.SandboxPolicy.from_runtime(runtime, work_dir=work_dir)
    return policy, {
        "policySource": source,
        "filePermission": getattr(policy, "file_permission", ""),
        "sandboxConfigured": bool(getattr(policy, "configured", False)),
        "sandboxError": str(getattr(policy, "error", "") or ""),
    }


def _op_context(request: Request, payload: dict[str, Any], tool: str) -> wb.OpContext:
    eng = rl.engine()
    principal = eng.current_principal() or {}
    return wb.OpContext(
        task_id=str(payload.get("session") or payload.get("sessionId") or payload.get("task") or ""),
        principal=str(principal.get("sub") or ("desktop" if _is_local(request, eng) else "")),
        tool=f"http:{request.method} {request.url.path}",
    )


def _pick_backend(payload: dict[str, Any], policy: sr.SandboxPolicy) -> tuple[Any, JSONResponse | None]:
    """要操作哪个后端：显式 backendId → 清单里取；没写 → 本轮 runtime / 清单活动项。

    找不到时把「被拒过」和「压根没写过」分开说：前者是配置没通过校验（切不过去也动不了，
    但原因在配置上），后者是 id 写错或还没装配。两句混成「不存在」，设置页就永远修不完。
    """
    wanted = str(payload.get("backendId") or payload.get("backend_id") or "").strip()
    if wanted:
        backend = wb.REGISTRY.get(wanted)
        if backend is None:
            reasons = wb.REGISTRY.rejected(wanted)
            if reasons:
                return None, _fail(
                    wb.CODE_SPEC_INVALID,
                    f"后端「{wanted}」的配置未通过校验，没有装配进来，本条操作未执行：{'；'.join(reasons)}",
                    400,
                    rejectedReasons=reasons,
                )
            return None, _fail(
                wb.CODE_BACKEND_NOT_FOUND,
                f"没有 id 为「{wanted}」的工作区后端（清单里已装配的是"
                f"{'、'.join(wb.REGISTRY.ids()) or '空'}）；先调 /api/workspace/backends/configure",
                404,
            )
        backend.bind_policy(policy)
        return backend, None
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else None
    backend = wb.active_backend(policy, runtime=runtime)
    if backend is None:
        return None, _fail(
            wb.CODE_BACKEND_REQUIRED,
            "本轮没有可用的工作区后端：清单是空的，请求也没带 backendId/runtime 里的后端选择。"
            f"已装配的是：{'、'.join(wb.REGISTRY.ids()) or '（空）'}；"
            "先调 /api/workspace/backends/configure 装一份清单，或在本轮 runtime 里指定后端",
            400,
            rejected=wb.REGISTRY.rejected(),
        )
    return backend, None


def _result(payload: dict[str, Any], *, backend: Any = None, policy_meta: dict[str, Any] | None = None) -> JSONResponse:
    out = dict(payload or {})
    out.setdefault("ok", True)
    if backend is not None:
        out.setdefault("backendId", backend.spec.backend_id)
        out.setdefault("backendKind", backend.kind)
        out.setdefault("backendRoot", backend.spec.root_display)
    if policy_meta:
        out.setdefault("policy", policy_meta)
    if out.get("deniedBy"):
        out.setdefault("deniedByLabel", DENIAL_SOURCE_LABELS.get(str(out["deniedBy"]), str(out["deniedBy"])))
    status = 200 if out.get("ok") else _STATUS_BY_CODE.get(str(out.get("code") or ""), 400)
    return JSONResponse(_plain(out), status_code=status)


def _plain(value: Any) -> Any:
    """审计与回话都要 JSON 可序列化：后端已经过 _jsonable，这里只兜住额外塞进去的对象。"""
    return wb._jsonable(value)


def _required(payload: dict[str, Any], *names: str) -> JSONResponse | None:
    """只查「带没带这个键」，不查内容宽不宽。

    空字符串是有意义的载荷（写入空文件就是空文件），到后端那层才由三段判定说话；
    在这层把「键值为空」当成「没带参数」拒掉，等于替后端改了一条它自己已经定过的口径。
    """
    missing = [name for name in names if name not in payload or payload.get(name) is None]
    if missing:
        return _fail(
            wb.CODE_BACKEND_REQUIRED,
            f"缺少必填参数：{'、'.join(missing)}（键名要带上，值可以是空文本）",
        )
    return None


async def _op(
    request: Request,
    tool: str,
    handler: Callable[[Any, dict[str, Any], wb.OpContext], dict[str, Any]],
    *,
    required: tuple[str, ...] = (),
) -> JSONResponse:
    gate = _desktop_only(request)
    if gate is not None:
        return gate
    payload = await _body(request)
    missing = _required(payload, *required)
    if missing is not None:
        return missing
    policy, policy_meta = _policy_for(payload)
    backend, error = _pick_backend(payload, policy)
    if error is not None:
        return error
    result = handler(backend, payload, _op_context(request, payload, tool))
    return _result(result, backend=backend, policy_meta=policy_meta)


# ── 查询面 ──────────────────────────────────────────────────────
# 只读 GET 的路径清单在上面常量区（READ_ONLY_PATHS），这里不再重复定义一份：
# 两处定义迟早分叉，而自测是按那一处核对方法分布的。


#: #140 快照的结论 → HTTP 状态码。「草稿不存在」归 404、「早过期了」归 410、
#: 「拦下不上送」归 403——别让界面把三种完全不同的事读成同一条「这条通道没实现」。
SNAPSHOT_STATUS_BY_CODE = {
    snap.CODE_NOTHING_CAPTURED: 400,
    snap.CODE_TEXT_TOO_LARGE: 413,
    snap.CODE_IMAGE_TOO_LARGE: 413,
    snap.CODE_IMAGE_FORMAT: 400,
    snap.CODE_BAD_BASE64: 400,
    snap.CODE_NOT_FOUND: 404,
    snap.CODE_EXPIRED: 410,
    snap.CODE_CONFIDENTIAL: 403,
    snap.CODE_INTERNAL_NEEDS_ACK: 403,
    snap.CODE_REDACTION_BLOCKED: 403,
    snap.CODE_LIMIT_INVALID: 400,
}

#: #140 接力的结论 → HTTP 状态码。其余拒绝一律 403（是「不让」，不是「够不着」）
RELAY_STATUS_BY_CODE = {
    relay.CODE_GRANT_NOT_FOUND: 404,
    relay.CODE_GRANT_EXPIRED: 410,
    relay.CODE_TABS_OVERFLOW: 400,
    relay.CODE_HOSTS_OVERFLOW: 400,
    relay.CODE_BAD_URL: 400,
    relay.CODE_WANT_INVALID: 400,
}

#: 接力四类拒绝来源 → 给人看的名字（与 snapshot_relay 同源，界面按 deniedBy 选文案）
RELAY_DENIAL_LABELS = dict(relay.SOURCE_LABELS)


def _snapshot_gate(request: Request) -> JSONResponse | None:
    """快照与接力同样只对本机桌面壳开放：屏幕内容比后端清单更不该被远程身份读走。

    走同一条闸（`_desktop_only`）但取 snapshot 那一族的理由——两族不能共用人家的文案。
    """
    return _desktop_only(request, "snapshot")


def _snapshot_fail(code: str, message: str, **extra: Any) -> JSONResponse:
    status = SNAPSHOT_STATUS_BY_CODE.get(str(code or ""), RELAY_STATUS_BY_CODE.get(str(code or ""), 400))
    return JSONResponse({"ok": False, "code": code, "message": message, **extra}, status_code=status)


def _snap_required(payload: dict[str, Any], *names: str) -> JSONResponse | None:
    """快照/接力这几条的缺参检查：只查「带没带这个键」，与后端那族同一个口径。

    不复用 ``_required`` 是因为它回的是 ``WS_BACKEND_REQUIRED``——把「没带 draftId」
    说成「没有工作区后端」，界面就会去查清单，而清单根本不是这件事。
    """
    missing = [name for name in names if name not in payload or payload.get(name) is None]
    if missing:
        return _snapshot_fail(
            snap.CODE_PARAM_MISSING,
            f"缺少必填参数：{'、'.join(missing)}（键名要带上，值可以是空文本）",
        )
    return None


def _snapshot_outcome(row: dict | None, code: str, message: str, status: int) -> JSONResponse:
    """把 ``SnapshotRegistry`` 交回的 (数据, code, 说明, 状态) 原样落回话，不在这层改文案。"""
    if code:
        return JSONResponse({"ok": False, "code": code, "message": message}, status_code=status)
    return JSONResponse(_plain({**(row or {}), "ok": True}), status_code=status or 200)


def _relay_outcome(decision: relay.RelayDecision, row: dict | None = None) -> JSONResponse:
    payload = {
        "ok": bool(decision.allowed),
        "allowed": bool(decision.allowed),
        "code": decision.code,
        "message": decision.reason,
        "deniedBy": decision.source,
        "deniedByLabel": RELAY_DENIAL_LABELS.get(decision.source, decision.source) if decision.source else "",
        "grantId": decision.grant_id,
        "host": decision.host,
    }
    if row:
        payload["grant"] = row
    status = 200 if decision.allowed else RELAY_STATUS_BY_CODE.get(decision.code, 403)
    return JSONResponse(_plain(payload), status_code=status)


@router.get("/api/snapshot/status")
async def snapshot_status(request: Request):
    """快照与接力两族的口径一屏读完：上限、状态机、分级表、拒绝来源、结论码、未验收清单。"""
    gate = _snapshot_gate(request)
    if gate is not None:
        return gate
    return _plain(
        {
            "ok": True,
            "snapshot": snap.REGISTRY.status(),
            "relay": relay.REGISTRY.status(),
            "endpoints": list(SNAPSHOT_ENDPOINT_PATHS),
            "readOnlyPaths": list(READ_ONLY_PATHS),
        }
    )


@router.post("/api/snapshot/capture")
async def snapshot_capture(request: Request):
    """用户主动触发一次采集（正文/图片由调用方交上来，本车道不截屏）。"""
    gate = _snapshot_gate(request)
    if gate is not None:
        return gate
    payload = await _body(request)
    runtime = _runtime_of(payload)
    draft, code, message = snap.REGISTRY.capture(
        text=payload.get("text", ""),
        image_b64=payload.get("imageBase64", payload.get("image_b64", "")),
        image_mime=payload.get("imageMime", payload.get("image_mime", "")),
        app_label=payload.get("appLabel", payload.get("app_label", "")),
        task_id=payload.get("session", payload.get("taskId", "")),
        origin=str(request.url.path),
        privacy_runtime=runtime,
    )
    if code:
        return _snapshot_fail(code, message)
    preview, _code, _message, _status = snap.REGISTRY.preview(draft.draft_id)
    return _snapshot_outcome(preview, "", "", 200)


@router.get("/api/snapshot/pending")
async def snapshot_pending(request: Request):
    """待确认清单：只回元数据（长度、指纹、分级），**正文一律不回**——列表不是预览。"""
    gate = _snapshot_gate(request)
    if gate is not None:
        return gate
    task_id = str(request.query_params.get("session") or request.query_params.get("taskId") or "").strip()
    # 端点叫 pending，就默认只列待确认的那几份：把已确认/已取消的墓碑也灌进「待确认清单」
    # 会让界面以为用户还有东西没决定。要全量就显式带 status= 或 status=all。
    raw_status = str(request.query_params.get("status") or "").strip()
    status = "" if raw_status.lower() in ("all", "*") else raw_status or snap.STATUS_PENDING
    rows = [draft.base_row() for draft in snap.REGISTRY.list(task_id=task_id, status=status)]
    return _plain(
        {
            "ok": True,
            "total": len(rows),
            "items": rows,
            "statusFilter": status or "all",
            "note": (
                "这一条只给元数据（长度、指纹、分级），正文一概不回——列表不是预览；"
                "要看见正文请走 /api/snapshot/preview（只有那里才是「上送前可见」）。"
                "默认只列待确认，已确认/已取消/已过期的墓碑要 status=all 才给"
            ),
        }
    )


@router.get("/api/snapshot/preview")
async def snapshot_preview(request: Request):
    """上送前可见的那一眼：正文按真正要发出去的那份产出（同一个函数，同一段文本）。"""
    gate = _snapshot_gate(request)
    if gate is not None:
        return gate
    draft_id = str(request.query_params.get("draftId") or request.query_params.get("draft_id") or "").strip()
    if not draft_id:
        return _snapshot_fail(snap.CODE_NOT_FOUND, "缺少 draftId：预览必须指着某一份已触发的快照")
    row, code, message, status = snap.REGISTRY.preview(draft_id)
    return _snapshot_outcome(row, code, message, status)


@router.post("/api/snapshot/confirm")
async def snapshot_confirm(request: Request):
    """确认上送。机密级（含明文密钥）在这里被硬拦，没有绕过开关。"""
    gate = _snapshot_gate(request)
    if gate is not None:
        return gate
    payload = await _body(request)
    missing = _snap_required(payload, "draftId")
    if missing is not None:
        return missing
    row, code, message, status = snap.REGISTRY.confirm(
        str(payload.get("draftId")),
        include_image=bool(payload.get("includeImage")),
        acknowledge_internal=bool(payload.get("acknowledgeInternal")),
        task_id=str(payload.get("session") or payload.get("taskId") or ""),
    )
    return _snapshot_outcome(row, code, message, status)


@router.post("/api/snapshot/cancel")
async def snapshot_cancel(request: Request):
    """取消：草稿连同正文与图片一起销毁，取消之后再确认不会把它捞回来。"""
    gate = _snapshot_gate(request)
    if gate is not None:
        return gate
    payload = await _body(request)
    missing = _snap_required(payload, "draftId")
    if missing is not None:
        return missing
    row, code, message, status = snap.REGISTRY.cancel(
        str(payload.get("draftId")), reason=str(payload.get("reason") or "")
    )
    return _snapshot_outcome(row, code, message, status)


@router.get("/api/snapshot/decisions")
async def snapshot_decisions(request: Request):
    """决策审计读面（快照 + 接力两条账并排给，都只有元数据）。"""
    gate = _snapshot_gate(request)
    if gate is not None:
        return gate
    params = request.query_params
    limit, error = _int_param(params.get("limit"), 50, 200)
    if error:
        return _snapshot_fail(snap.CODE_LIMIT_INVALID, error)
    offset, error = _int_param(params.get("offset"), 0, 10_000_000)
    if error:
        return _snapshot_fail(snap.CODE_LIMIT_INVALID, error)
    draft_id = str(params.get("draftId") or params.get("grantId") or "").strip()
    action = str(params.get("action") or "").strip()
    return _plain(
        {
            "ok": True,
            "snapshot": snap.REGISTRY.decisions(draft_id=draft_id, action=action, limit=limit, offset=offset),
            "relay": relay.REGISTRY.decisions(grant_id=draft_id, action=action, limit=limit, offset=offset),
            "note": "两条账都只有长度、指纹、域名与结论：正文、图片、Cookie 与密钥一概不落",
        }
    )


def _int_param(raw: Any, default: int, ceiling: int) -> tuple[int, str]:
    """limit/offset 只认非负整数，且不超过上限——越界要说清是哪一条，不静默夹。"""
    if raw is None or str(raw).strip() == "":
        return default, ""
    try:
        value = int(str(raw).strip())
    except Exception:  # noqa: BLE001
        return default, f"「{raw}」不是整数，本端点只接受整数"
    if value < 0:
        return default, f"「{value}」是负数，本端点不接受"
    if value > ceiling:
        return default, f"「{value}」超过上限 {ceiling}"
    return value, ""


def _runtime_of(payload: dict[str, Any]) -> dict[str, Any]:
    """快照/接力要的 runtime（脱敏档位、浏览器现场）：会话 → 请求体 → 空。

    与 :func:`_policy_for` 同源的同一条取法，但这里要的只是那一份 dict，
    不需要构造沙箱策略——构造一份用不上的策略会把「没有沙箱」的警告带进回话。
    """
    eng = rl.engine()
    session_ref = str(payload.get("session") or payload.get("sessionId") or "").strip()
    if session_ref:
        session = (getattr(eng, "_TASK_SESSIONS", None) or {}).get(session_ref)
        if session is not None:
            return dict(getattr(session, "runtime", {}) or {})
    return dict(payload.get("runtime") or {}) if isinstance(payload.get("runtime"), dict) else {}


@router.get("/api/snapshot/relay/status")
async def relay_status(request: Request):
    """当前有哪些接力授权、各自授给了哪些域，以及四句互不相同的拒绝文案表。"""
    gate = _snapshot_gate(request)
    if gate is not None:
        return gate
    workspace_id = str(request.query_params.get("workspaceId") or request.query_params.get("workspace") or "").strip()
    return _plain(
        {
            "ok": True,
            "grants": relay.REGISTRY.list(workspace_id=workspace_id),
            "summary": relay.REGISTRY.status(),
            "denialSources": [{"value": key, "label": value} for key, value in RELAY_DENIAL_LABELS.items()],
            "note": "这里没有 Cookie、没有令牌、没有完整路径：只有域名、配置目录指纹与结论",
        }
    )


@router.post("/api/snapshot/relay/grant")
async def relay_grant(request: Request):
    """开一次接力授权（按工作区 + 域）。指向日常浏览器目录的授权根本开不出来。"""
    gate = _snapshot_gate(request)
    if gate is not None:
        return gate
    payload = await _body(request)
    hosts = payload.get("hosts")
    if not isinstance(hosts, list):
        return _snapshot_fail(relay.CODE_BAD_URL, "hosts 必须是一个数组（一次授权给若干个域）")
    decision, row = relay.REGISTRY.grant(
        runtime=_runtime_of(payload),
        workspace_id=str(payload.get("workspaceId") or payload.get("workspace") or ""),
        hosts=[str(item) for item in hosts],
        task_id=str(payload.get("session") or payload.get("taskId") or ""),
        profile_dir=str(payload.get("profileDir") or payload.get("profile_dir") or ""),
    )
    return _relay_outcome(decision, row)


@router.post("/api/snapshot/relay/decide")
async def relay_decide(request: Request):
    """这一次能不能复用已授权会话里的标签页 / 登录态。"""
    gate = _snapshot_gate(request)
    if gate is not None:
        return gate
    payload = await _body(request)
    missing = _snap_required(payload, "grantId")
    if missing is not None:
        return missing
    want = str(payload.get("want") or "tabs").strip().lower()
    if want not in ("tabs", "login", "url"):
        return _snapshot_fail(
            relay.CODE_WANT_INVALID,
            f"want 只认 tabs / login / url，给的是「{want}」：判定不猜要什么",
        )
    decision = relay.REGISTRY.decide(
        url=str(payload.get("url") or ""),
        grant_id=str(payload.get("grantId")),
        workspace_id=str(payload.get("workspaceId") or payload.get("workspace") or ""),
        runtime=_runtime_of(payload),
        want="url" if want == "url" else want,
    )
    return _relay_outcome(decision)


@router.post("/api/snapshot/relay/tabs")
async def relay_tabs(request: Request):
    """把调用方交上来的标签页登记进这条授权（本车道不枚举标签页）。"""
    gate = _snapshot_gate(request)
    if gate is not None:
        return gate
    payload = await _body(request)
    missing = _snap_required(payload, "grantId", "tabs")
    if missing is not None:
        return missing
    tabs = payload.get("tabs")
    if not isinstance(tabs, list):
        return _snapshot_fail(relay.CODE_TABS_OVERFLOW, "tabs 必须是一个数组（每项形如 {tabId,url,title,active}）")
    decision, row = relay.REGISTRY.register_tabs(str(payload.get("grantId")), tabs)
    return _relay_outcome(decision, row)


@router.post("/api/snapshot/relay/revoke")
async def relay_revoke(request: Request):
    """撤销：按一条授权撤，或按工作区整体撤。撤销立即生效，不看剩余有效期。"""
    gate = _snapshot_gate(request)
    if gate is not None:
        return gate
    payload = await _body(request)
    grant_id = str(payload.get("grantId") or "").strip()
    workspace_id = str(payload.get("workspaceId") or payload.get("workspace") or "").strip()
    reason = str(payload.get("reason") or "")
    if not grant_id and not workspace_id:
        return _snapshot_fail(
            relay.CODE_GRANT_NOT_FOUND,
            "撤销要么给 grantId（撤这一条），要么给 workspaceId（撤这个工作区的全部）：两个都不给不撤",
        )
    if grant_id:
        decision, row = relay.REGISTRY.revoke(grant_id, reason=reason)
        return _relay_outcome(decision, row)
    revoked = relay.REGISTRY.revoke_workspace(workspace_id, reason=reason)
    return _relay_outcome(
        relay.RelayDecision(
            allowed=True,
            reason=f"已撤销工作区「{workspace_id}」的 {len(revoked)} 条接力授权"
            + ("（这个工作区本来就没有活动授权）" if not revoked else ""),
        )
    )


@router.get("/api/workspace/backends")
async def backends(request: Request):
    """后端清单 + 每个后端的探测结论 + 被拒条目 + 未验收清单（设置页一屏读完）。"""
    gate = _desktop_only(request)
    if gate is not None:
        return gate
    snapshot = wb.list_backends()
    status = wb.runtime_status()
    return _plain(
        {
            "ok": True,
            "activeBackendId": snapshot.get("activeBackendId"),
            "backends": snapshot.get("backends"),
            "probes": snapshot.get("probes"),
            "rejected": snapshot.get("rejected"),
            "note": snapshot.get("note"),
            "kindsKnown": status.get("kindsKnown"),
            "credentialPolicy": status.get("credentialPolicy"),
            "unverified": status.get("unverified"),
            "auditFile": status.get("auditFile"),
            "auditInMemoryOnly": status.get("auditPersistWarning"),
            "filePermissionModes": list(sr.FILE_PERMISSION_MODES),
            "defaultFilePermission": sr.DEFAULT_FILE_PERMISSION,
            "denialSources": DENIAL_SOURCE_LABELS,
            "endpoints": list(ENDPOINT_PATHS),
        }
    )


@router.get("/api/workspace/audit")
async def audit(
    request: Request,
    backendId: str = "",
    kind: str = "",
    outcome: str = "",
    limit: int = 50,
    offset: int = 0,
):
    """倒序读远程操作审计（新→旧）。只回字节数与指纹，正文与密钥从来不在盘上这份账里。"""
    gate = _desktop_only(request)
    if gate is not None:
        return gate
    return _result(wb.read_audit(
        backend_id=backendId, kind=kind, outcome=outcome, limit=limit, offset=offset
    ))


# ── 装配与切换 ──────────────────────────────────────────────────


@router.post("/api/workspace/backends/configure")
async def configure(request: Request):
    """装配后端清单（单条坏配置只废那一条并留在 rejected 里，不拖垮整份清单）。

    请求体：``{"backends": [...], "activeBackendId": 可选, "session"/"runtime": 可选}``。
    凭据只收 ``env:`` / ``keychain:`` 引用；本端点不从请求体收 environ，也不收凭据原值。
    """
    gate = _desktop_only(request)
    if gate is not None:
        return gate
    payload = await _body(request)
    items = payload.get("backends")
    if items is None:
        items = payload.get("items")
    if items is None:
        return _fail(
            wb.CODE_BACKEND_REQUIRED,
            "缺少 backends：给一份后端清单（数组），每条至少含 backendId 与 kind"
            f"（本版本认识 {'/'.join(wb.BACKEND_KINDS)}）",
        )
    policy, policy_meta = _policy_for(payload)
    result = wb.REGISTRY.configure(
        items,
        policy=policy,
        active=str(payload.get("activeBackendId") or payload.get("active") or "").strip(),
    )
    result["policy"] = policy_meta
    result["rejected"] = wb.REGISTRY.rejected()
    result["unverified"] = wb.runtime_status().get("unverified")
    return _result(result)


@router.post("/api/workspace/backends/probe")
async def probe(request: Request):
    """探测：已装的后端按 backendId；还没保存的配置按 backend 草稿（草稿绝不进清单）。"""
    gate = _desktop_only(request)
    if gate is not None:
        return gate
    payload = await _body(request)
    policy, policy_meta = _policy_for(payload)
    wanted = str(payload.get("backendId") or payload.get("backend_id") or "").strip()
    draft = payload.get("backend")
    if wanted and isinstance(draft, dict):
        # 两份都给：不猜「以哪份为准」。猜错了设置页会显示成「探的是已存的、我以为在探草稿」
        return _fail(
            wb.CODE_BACKEND_REQUIRED,
            "探测请求同时带了 backendId 与 backend 草稿：要么探清单里已装的（只带 backendId），"
            "要么探还没保存的（只带 backend，且草稿里写它自己的 backendId）",
        )
    if isinstance(draft, dict) and not wanted:
        if not str(draft.get("backendId") or draft.get("backend_id") or "").strip():
            return _fail(
                wb.CODE_BACKEND_REQUIRED,
                "草稿探测也要带 backendId：探测回话要落进审计，没名字的后端说不清探的是哪一个",
            )
        spec = wb.parse_spec(draft)
        if spec.errors:
            return _result(
                {
                    "ok": False,
                    "code": wb.CODE_SPEC_INVALID,
                    "message": "草稿配置未通过校验，没有探测也没有装配：" + "；".join(spec.errors),
                    "errors": list(spec.errors),
                    "warnings": list(spec.warnings),
                    "registered": False,
                },
                policy_meta=policy_meta,
            )
        backend = wb.build_backend(spec, policy=policy)
        result = backend.probe()
        result["registered"] = False
        result["note"] = (
            "这是对还没保存的配置做的探测，没有装进后端清单（带病配置进清单就等于切得过去）；"
            "要启用请在保存时调 /api/workspace/backends/configure"
        )
        result["warnings"] = list(result.get("warnings") or []) + list(spec.warnings)
        return _result(result, backend=backend, policy_meta=policy_meta)
    if wanted:
        backend = wb.REGISTRY.get(wanted)
        if backend is None:
            reasons = wb.REGISTRY.rejected(wanted)
            if reasons:
                return _fail(
                    wb.CODE_SPEC_INVALID,
                    f"后端「{wanted}」配置未通过校验，没装进清单，探不了：{'；'.join(reasons)}",
                    400,
                    rejectedReasons=reasons,
                )
            return _fail(wb.CODE_BACKEND_NOT_FOUND, f"没有 id 为「{wanted}」的工作区后端", 404)
        backend.bind_policy(policy)
        return _result(backend.probe(), backend=backend, policy_meta=policy_meta)
    snapshot = wb.REGISTRY.snapshot()
    return _result(
        {
            "ok": True,
            "backendId": "",
            "probes": snapshot.get("probes"),
            "message": (
                f"已探测 {len(snapshot.get('probes') or {})} 个后端；"
                "带 backendId 探单个，带 backend 草稿探还没保存的配置"
            ),
        },
        policy_meta=policy_meta,
    )


@router.post("/api/workspace/switch")
async def switch(request: Request):
    """切落点。**只换后端句柄，不改 Agent 配置**：回话里逐条列出换了什么、没动什么。"""
    gate = _desktop_only(request)
    if gate is not None:
        return gate
    payload = await _body(request)
    wanted = str(payload.get("backendId") or payload.get("backend_id") or "").strip()
    if not wanted:
        return _fail(wb.CODE_BACKEND_REQUIRED, "切换工作区后端要给 backendId")
    policy, policy_meta = _policy_for(payload)
    backend = wb.REGISTRY.get(wanted)
    if backend is not None:
        backend.bind_policy(policy)
    result = wb.REGISTRY.switch(wanted)
    return _result(result, policy_meta=policy_meta)


# ── 文件面（三段判定在这里只被「递进去」，不被重述）──────────────


@router.post("/api/workspace/files/list")
async def files_list(request: Request):
    return await _op(request, "list", lambda b, p, c: b.list_dir(p.get("path", ""), ctx=c))


@router.post("/api/workspace/files/read")
async def files_read(request: Request):
    return await _op(request, "read", lambda b, p, c: b.read(p.get("path"), ctx=c), required=("path",))


@router.post("/api/workspace/files/write")
async def files_write(request: Request):
    def run(backend: Any, payload: dict[str, Any], ctx: wb.OpContext) -> dict[str, Any]:
        content = payload.get("content")
        if not isinstance(content, str):
            return {
                "ok": False,
                "code": wb.CODE_BACKEND_REQUIRED,
                "message": (
                    f"content 要是一段文本（收到的是 {type(content).__name__}）："
                    "本卡的后端写面只承载文本对象，二进制上传不在 #157 范围"
                ),
            }
        return backend.write(payload.get("path"), content, ctx=ctx)

    return await _op(request, "write", run, required=("path", "content"))


@router.post("/api/workspace/files/delete")
async def files_delete(request: Request):
    return await _op(
        request, "delete", lambda b, p, c: b.delete(p.get("path"), ctx=c), required=("path",)
    )


@router.post("/api/workspace/files/move")
async def files_move(request: Request):
    return await _op(
        request,
        "move",
        lambda b, p, c: b.move(p.get("from"), p.get("to"), ctx=c),
        required=("from", "to"),
    )


@router.post("/api/workspace/exec")
async def exec_command(request: Request):
    """在后端上起命令（只有 docker / ssh 承载得了；其余后端回能力拒绝，不是静默空跑）。"""

    def run(backend: Any, payload: dict[str, Any], ctx: wb.OpContext) -> dict[str, Any]:
        command = payload.get("command")
        if not isinstance(command, str) or not command.strip():
            return {
                "ok": False,
                "code": wb.CODE_BACKEND_REQUIRED,
                "message": "command 要是一段非空命令文本（本端点不接受数组形式，也不猜「空命令=列目录」）",
            }
        return backend.run(command, ctx=ctx)

    return await _op(request, "exec", run, required=("command",))
