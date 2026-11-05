"""connect 车道（W5）：MCP 连接器、IM 通道、定时任务、浏览器会话。

占用前缀见 routes_lanes.LANE_PREFIXES["connect"]。既有 /api/im/* 留在 server.py
（im-channel-contract 逐字钉住），新端点写这里。
长连接沿用既有约束：每条连接器独占一个 slot 任务，有界等待只用 asyncio.timeout。

D-39 在这一层落的是**只算不连**的那几件事（连接器目录、表单到 spec、白名单预览、
常驻健康面、与目录对账）：

1. **不在这里拉起任何连接**。目录是模板、spec 是配置、预览是纯函数、健康面是读数——
   真连接只有两条路（Rust 的探测、执行面的 `mcp_runtime.attach`），这里一条都不冒充。
   「界面点一下就绿了但引擎根本没连过」是这一族最容易犯的假绿。
2. **凭据只经调用方的手进来，再原样出去给桌面端落库**。引擎不存令牌、不写钥匙串：
   存储归 Rust（`mcp_servers.auth_json` + 系统钥匙串）。所以 `spec` 那一条只对桌面壳
   （本机令牌 / 无凭据回环）开放，带远程身份一律 403——它经手的是即将被存下的凭据形状。
3. **业务性拒绝回 200 + ok=false + 中文 message**（缺字段、没令牌、白名单为空都是用户
   在表单里会遇到的事）；只有读不出来的意外才回 500 带错误码（与 exec 车道同口径）。
4. **目录里没核对过的条目要如实标出来**。`verified="unverified"` 的那些连同「以服务商
   当前文档为准」一起下发，界面不许把它们画成「官方已验证」。
"""

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import im_channels as imch
import im_policy as ipol
import mcp_connector_catalog as mcc
import mcp_errors as mex
import routes_lanes as rl

LANE = "connect"
OWNER = rl.LANE_OWNERS["connect"]

router = APIRouter()

#: 本车道登记的全部路径（自测与分片都从这里取，别在两边各抄一份字符串）
#: IM 这四条刻意都是**两段字面路径**：server.py 里已有 `/api/im/{kind}/config`
#: 这类三段通配，车道是后挂的，写成三段会被通配吞掉（D-07.4 那个坑，mock 测不出来）。
CONNECT_ENDPOINT_PATHS = (
    "/api/mcp/connectors",
    "/api/mcp/connectors/spec",
    "/api/mcp/allowlist/preview",
    "/api/mcp/reconcile",
    "/api/mcp/health",
    "/api/im/policies",
    "/api/im/capabilities",
    "/api/im/traffic",
)


def _fail(code: str, message: str, status: int = 400, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, "message": message, **extra}, status_code=status)


def _soft_fail(code: str, message: str, **extra: Any) -> JSONResponse:
    """用户在表单里会遇到的拒绝：HTTP 200 + ok=false，界面按 message 说人话"""
    return JSONResponse({"ok": False, "code": code, "message": message, **extra}, status_code=200)


async def _body(request: Request) -> dict[str, Any]:
    """无效 JSON 与顶层不是对象的请求体都当「没带参数」，由调用方按缺参拒绝。"""
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return payload if isinstance(payload, dict) else {}


def _desktop_only(request: Request, what: str) -> JSONResponse | None:
    """经手凭据形状的一族只接受桌面壳（本机令牌）或无凭据回环模式。

    判定用引擎主模块里的同一个 `_is_desktop_local`，不在这层复制第二份实现。
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
            "code": "MCP_DESKTOP_ONLY",
            "message": (
                f"连接器的{what}只接受桌面壳（本机令牌）发起的请求：当前请求带着远程身份"
                f"（sub={who.get('sub') or '未知'}）。这一族经手的是访问令牌与落库前的配置形状，"
                "交给远程身份等于让别人替你决定往哪个地址发凭据"
            ),
        },
        status_code=403,
    )


def _catalog_rows(category: str = "") -> list[dict]:
    """目录读数（模板，不含任何用户填的值）

    不下发 `command` / `args` / `url` / `headers` 的模板原文：那是 spec 生成那一步的
    中间产物，界面拿它会显示半截带 `{占位符}` 的地址，看起来像「配置坏了」。
    """
    rows = mcc.entries(category) if category else mcc.entries()
    out: list[dict] = []
    for entry in rows:
        out.append(
            {
                "id": entry["id"],
                "name": entry["name"],
                "category": entry["category"],
                "summary": entry["summary"],
                "transport": entry["transport"],
                "mode": entry["mode"],
                "auth": entry["auth"],
                "verified": entry["verified"],
                "docs": entry["docs"],
                "icon": entry["icon"],
                "timeoutSecs": entry["timeoutSecs"],
                "fields": [dict(f) for f in entry["fields"]],
                "suggestedAllowlist": list(entry["suggestedAllowlist"]),
                # 「要填哪些 scope / 回调长什么样」是接入成本的大头，摊开给界面
                "scopes": list(entry["scopes"]),
                "scopesFromServer": bool(entry["scopesFromServer"]),
                "callbackNote": entry["callbackNote"],
                "gatewayNote": entry["gatewayNote"],
                "needsToken": entry["auth"] in ("oauth_pkce", "bearer"),
            }
        )
    return out


@router.get("/api/mcp/connectors")
async def mcp_connectors(request: Request):
    """连接器目录（D-39「先覆盖办公 / 知识 / 协作至少 8 个」的读数面）。

    只有模板与表单字段，**没有任何连接事实**：这条连接器能不能连上、装没装包、
    令牌有没有过期，一概不在这里回答（那分别是 Rust 探测与 `/api/mcp/health` 的事）。
    """
    category = str(request.query_params.get("category") or "").strip()
    try:
        rows = _catalog_rows(category)
        full = rows if not category else _catalog_rows()
    except Exception as exc:  # noqa: BLE001
        return _fail("MCP_CATALOG_FAILED", f"连接器目录读取失败：{type(exc).__name__}：{str(exc)[:200]}", 500)
    if category and not rows:
        return _soft_fail(
            "MCP_CATALOG_EMPTY_CATEGORY",
            f"分类「{category}」下没有连接器条目；可选分类：{'、'.join(mcc.CATEGORIES)}",
            categories=list(mcc.CATEGORIES),
        )
    unverified = [r["id"] for r in full if r["verified"] != "doc"]
    return JSONResponse(
        {
            "ok": True,
            "rows": rows,
            "total": len(full),
            "categories": list(mcc.CATEGORIES),
            "modes": list(mcc.MODES),
            "authKinds": list(mcc.AUTH_TYPES),
            # 未核验的条目要摊开：界面把它们画成「已验证」就是替服务商撒谎
            "unverifiedIds": unverified,
            "unverifiedNote": (
                f"其中 {len(unverified)} 条的端点/包名以服务商当前文档为准（本目录未联网核验）"
                if unverified
                else "全部条目均按服务商文档核对过形状（仍未联网实测）"
            ),
        }
    )


@router.post("/api/mcp/connectors/spec")
async def mcp_connector_spec(request: Request):
    """目录记录 + 用户填的值 → 运行期 spec（D-39 验收「无需手写 JSON」的那一步）。

    引擎只拼形状、不存不落库：返回的 spec 交回桌面端，由 Rust 写 `mcp_servers`
    （令牌进 `auth_json` / 钥匙串）。缺必填字段回 200 + 中文原因，让表单就地提示。
    """
    blocked = _desktop_only(request, "配置生成")
    if blocked is not None:
        return blocked
    payload = await _body(request)
    connector_id = str(payload.get("connectorId") or "").strip()
    values = payload.get("values") if isinstance(payload.get("values"), dict) else {}
    if not connector_id:
        return _fail("MCP_CONNECTOR_ID_MISSING", "没带 connectorId：目录里取一条的 id 再调这一族")
    if mcc.get(connector_id) is None:
        return _fail(
            "MCP_CONNECTOR_UNKNOWN",
            f"目录里没有连接器「{connector_id}」：请从 GET /api/mcp/connectors 的 rows 里取 id",
        )
    try:
        spec, notes = mcc.build_spec(
            connector_id,
            values,
            token=str(payload.get("token") or ""),
            expires_at_ms=payload.get("expiresAtMs"),
            server_id=str(payload.get("serverId") or ""),
        )
    except ValueError as exc:
        text = str(exc)
        code = text.split(":", 1)[0].strip() if ":" in text else "MCP_CONNECTOR_INVALID"
        return _soft_fail(code or "MCP_CONNECTOR_INVALID", text.split(":", 1)[-1].strip() or text)
    except Exception as exc:  # noqa: BLE001
        return _fail("MCP_CONNECTOR_SPEC_FAILED", f"配置生成失败：{type(exc).__name__}：{str(exc)[:200]}", 500)
    return JSONResponse({"ok": True, "spec": spec, "notes": notes})


@router.post("/api/mcp/allowlist/preview")
async def mcp_allowlist_preview(request: Request):
    """白名单预览（保存前就告诉用户「这几条规则会放行几个工具」）。

    纯计算：拿调用方给的（或目录建议的）工具清单展开规则，**不连接、不探测**。
    清单为空时通配规则展不开，如实说「需要一次工具清单」而不是报 0 个。
    """
    payload = await _body(request)
    connector_id = str(payload.get("connectorId") or "").strip()
    entry = mcc.get(connector_id) if connector_id else None
    tools = mcc.tool_names_from(payload.get("tools"))
    patterns_raw = payload.get("allowedTools")
    if patterns_raw is None and entry is not None:
        patterns_raw = entry["suggestedAllowlist"]
    expert_ref = str(payload.get("expertId") or "").strip()
    spec = {"allowedTools": patterns_raw}
    overrides = payload.get("expertToolAllowlists")
    if isinstance(overrides, dict):
        spec["expertToolAllowlists"] = overrides
    patterns, reason = mcc.allowlist_for(spec, expert_ref)
    if patterns is None:
        return JSONResponse(
            {
                "ok": True,
                "unrestricted": True,
                "patterns": [],
                "kept": tools,
                "total": len(tools),
                "note": (
                    ("没设白名单：这个连接器的工具全量可见" if not reason else f"不限制白名单：{reason}")
                    + ("（本轮没给工具清单，只报口径不报数量）" if not tools else "")
                ),
            }
        )
    if not patterns:
        return JSONResponse(
            {
                "ok": True,
                "unrestricted": False,
                "patterns": [],
                "kept": [],
                "total": len(tools),
                "note": mcc.describe_allowlist(patterns, len(tools), 0),
            }
        )
    kept = [t for t in tools if mcc.tool_allowed(patterns, t)]
    note = mcc.describe_allowlist(patterns, len(tools), len(kept))
    if not tools:
        note = "没给工具清单，通配规则展不开；保存后第一次连接会按真实清单生效" + f"（规则：{'、'.join(patterns[:8])}）"
    return JSONResponse(
        {
            "ok": True,
            "unrestricted": False,
            "patterns": patterns,
            "kept": kept,
            "total": len(tools),
            "note": note,
            "reason": reason,
        }
    )


@router.post("/api/mcp/reconcile")
async def mcp_reconcile(request: Request):
    """把运行期连接器清单与目录对账（D-39「统一 Rust / Python 的连接器注册表」的读数面）。

    只识别与说明，不改任何配置：命中目录的行标出 `catalogId` 与还缺什么，没命中的行
    如实标 `manual`（手工配置不是坏东西）。调用方是桌面端（它才读得到 `mcp_servers` 表）。
    """
    payload = await _body(request)
    servers = payload.get("servers")
    if not isinstance(servers, list):
        return _soft_fail(
            "MCP_RECONCILE_NO_ROWS",
            "没带 servers 数组：这一族要的是当前已配置的连接器清单（Rust 的 mcp_servers 行）",
            rows=[], catalogTotal=len(mcc.CONNECTORS), manual=0,
        )
    try:
        report = mcc.reconcile(servers)
    except Exception as exc:  # noqa: BLE001
        return _fail("MCP_RECONCILE_FAILED", f"连接器对账失败：{type(exc).__name__}：{str(exc)[:200]}", 500)
    report["ok"] = True
    return JSONResponse(report)


@router.get("/api/mcp/health")
async def mcp_health(request: Request):
    """跨轮常驻连接的健康面（D-39「健康检查」的读路径，只读不重连）。

    每连接器一行：`live`（此刻有常驻槽位）/ `blocked`（失败退避中）/ `idle`（没连接）。
    重连永远由真实的一轮任务触发，这一族不代劳——点一下「刷新」就重试一次的界面，
    会把退避彻底架空。
    """
    eng = rl.engine()
    hub = getattr(eng, "MCP_HUB", None)
    if hub is None:
        return JSONResponse(
            {
                "ok": True,
                "available": False,
                "rows": [],
                "counters": {},
                "note": "引擎主模块里没有常驻枢纽：本轮没有任何跨轮连接可报（不是连接器坏了）",
                "errorKinds": list(mex.ALL_KINDS),
            }
        )
    try:
        rows = hub.health()
        snap = hub.snapshot()
    except Exception as exc:  # noqa: BLE001
        return _fail("MCP_HEALTH_FAILED", f"连接器健康面读取失败：{type(exc).__name__}：{str(exc)[:200]}", 500)
    blocked = [r for r in rows if r.get("state") == "blocked"]
    return JSONResponse(
        {
            "ok": True,
            "available": True,
            "rows": rows,
            "live": len([r for r in rows if r.get("state") == "live"]),
            "blocked": len(blocked),
            "counters": snap.get("counters", {}),
            "lastError": snap.get("lastError") or "",
            # 类别名给界面做图标/着色，中文句子已经按行给了 `hint`
            "errorKinds": list(mex.ALL_KINDS),
            "note": (
                f"{len(blocked)} 个连接器在退避冷却中，本轮不会重复握手；"
                "改好配置或重新授权后立刻放行"
                if blocked
                else "没有退避中的连接器"
            ),
        }
    )


# ── D-41：IM 群策略 / 能力 / 流量面 ────────────────────────────────
#
# 这三条都是**读引擎的事实**：界面不许自己判断「这个平台能不能收图」「还有几秒恢复发送」
# 「这条为什么没起任务」——抄一份到前端就会和引擎漂移。写的那一条（POST policies）只落
# 配置，不碰连接、不碰凭据；错误一律 `ok=false` + 中文 message（用户在设置页会遇到的
# 拒绝不是意外）。


def _policy_shape() -> dict:
    """策略的取值域与中文标签：设置页据此出选项，不在前端写死第二份枚举"""
    return {
        "enums": {
            "groupActivation": list(ipol.ACTIVATIONS),
            "dmActivation": list(ipol.ACTIVATIONS),
            "groupContext": list(ipol.CONTEXT_MODES),
            "groupMedia": list(ipol.MEDIA_MODES),
            "dmMedia": list(ipol.MEDIA_MODES),
        },
        "labels": {
            "groupActivation": dict(ipol.ACTIVATION_LABELS),
            "dmActivation": dict(ipol.ACTIVATION_LABELS),
            "groupContext": dict(ipol.CONTEXT_MODE_LABELS),
            "groupMedia": {"off": "不下载附件，只在消息里以文字说明呈现", "on": "下载并保存到本机附件目录"},
            "dmMedia": {"off": "不下载附件，只在消息里以文字说明呈现", "on": "下载并保存到本机附件目录"},
        },
        "bounds": {
            key: {"min": low, "max": high, "label": label, "default": ipol.POLICY_DEFAULTS[key]}
            for key, (low, high, label) in ipol.NUMERIC_BOUNDS.items()
        },
        "contextLimits": {
            "maxMessages": ipol.MAX_CONTEXT_MESSAGES,
            "maxChars": ipol.MAX_CONTEXT_CHARS,
            "note": "群内非 @ 消息只在内存里暂存，被 @ 那一次读走并并入任务；不落盘、不跨重启",
        },
        "scopeNote": (
            "作用域由细到粗：会话（群号/单聊用户）→ 通道实例（写成 `类型:实例标识`，"
            "与引擎的 instance_key 同形；同名不同实例不会互相继承）→ 通道类型 → 全局默认；"
            "改粗的不影响已经单独配过的细作用域"
        ),
    }


@router.get("/api/im/policies")
async def im_policies_get(request: Request):
    """读策略：整本配置 + 按询问的作用域链算出的生效值（只读，不改任何东西）。"""
    query = request.query_params
    kind = str(query.get("kind") or "").strip()
    channel_id = str(query.get("channelId") or "").strip()
    conversation_id = str(query.get("conversationId") or "").strip()
    scope = str(query.get("scope") or "").strip()
    try:
        store = ipol.load_store()
    except Exception as exc:  # noqa: BLE001
        return _fail("IM_POLICY_UNREADABLE", f"策略表读不出来：{type(exc).__name__}：{str(exc)[:160]}", 500)
    chain = ipol.scope_chain(channel_id, kind, conversation_id)
    if scope and scope not in chain:
        chain.insert(0, scope)
    return JSONResponse(
        {
            "ok": True,
            "scope": scope,
            "chain": chain,
            "policy": ipol.effective(channel_id, kind, conversation_id, store=store),
            "defaults": dict(store.get("defaults") or {}),
            "scopes": {k: dict(v) for k, v in (store.get("scopes") or {}).items()},
            "shape": _policy_shape(),
        }
    )


@router.post("/api/im/policies")
async def im_policies_post(request: Request):
    """写策略：`{scope, patch}` 落一份配置，或 `{scope, drop:true}` 删掉某个作用域。

    只写配置：不重启连接、不重绑凭据、不替用户猜「改了要不要立刻生效」——
    每一条入站消息都重新读一次策略，所以保存即生效，这条接口不需要碰运行态。
    """
    payload = await _body(request)
    scope = str(payload.get("scope") or "").strip()
    if payload.get("drop"):
        if not scope:
            return _soft_fail("IM_POLICY_INVALID", "删除作用域要指明 scope：全局默认不允许从这里删")
        try:
            store = ipol.drop_scope(scope)
        except ipol.PolicyError as exc:
            return _soft_fail(exc.code, exc.detail or str(exc))
        except Exception as exc:  # noqa: BLE001
            return _fail("IM_POLICY_WRITE_FAILED", f"策略删除失败：{type(exc).__name__}：{str(exc)[:160]}", 500)
        return JSONResponse({"ok": True, "scope": scope, "dropped": True, "scopes": dict(store.get("scopes") or {})})

    patch = payload.get("patch")
    if not isinstance(patch, dict) or not patch:
        return _soft_fail("IM_POLICY_INVALID", "没带 patch 对象：要改的策略项按 {键: 值} 提交")
    try:
        store = ipol.save_policy(patch, scope)
    except ipol.PolicyError as exc:
        # 取值域/作用域标识不合法都是表单能改出来的，按业务拒绝回 200 带中文原因
        return _soft_fail(exc.code, exc.detail or str(exc), shape=_policy_shape())
    except Exception as exc:  # noqa: BLE001
        return _fail("IM_POLICY_WRITE_FAILED", f"策略写入失败：{type(exc).__name__}：{str(exc)[:160]}", 500)
    kind = scope if ":" not in scope and scope in imch.CHANNEL_CLASSES else ""
    return JSONResponse(
        {
            "ok": True,
            "scope": scope,
            "stored": dict((store.get("scopes") or {}).get(scope) or store.get("defaults") or {}),
            "policy": ipol.effective(scope if not kind else "", kind, scope),
            "defaults": dict(store.get("defaults") or {}),
            "scopes": {k: dict(v) for k, v in (store.get("scopes") or {}).items()},
        }
    )


@router.get("/api/im/capabilities")
async def im_capabilities(request: Request):
    """每条通道、每个实例「能做到什么」的唯一来源：媒体收发 / typing / 是否答得出被 @

    未接入的一律 False 且带中文原因（`...Note`）。台账与界面拿到的都是这套事实，
    于是「支持图片」这句话只可能在引擎真的换到下载地址时才出现。
    """
    rows: list[dict] = []
    for runtime in imch.all_runtimes():
        channel = runtime.channel
        rows.append(
            {
                "kind": channel.kind,
                "channelId": runtime.channel_id,
                "instanceKey": runtime.instance_key,
                "label": channel.label,
                "supported": bool(channel.supported),
                "unsupportedReason": imch.channel_reason(channel),
                "configured": runtime.configured(),
                "running": bool(runtime._task and not runtime._task.done()),
                "mediaIn": bool(channel.supports_media_in),
                "mediaOut": bool(channel.supports_media_out),
                "typing": bool(channel.supports_typing),
                "mediaInNote": channel.media_in_note,
                "mediaOutNote": channel.media_out_note,
                "typingNote": channel.typing_note,
                "mentionEvidence": channel.mention_note,
                "loginMode": channel.login_mode,
                "transport": channel.transport,
            }
        )
    return JSONResponse(
        {
            "ok": True,
            "rows": rows,
            "kinds": sorted({r["kind"] for r in rows}),
            "note": "能力位描述的是引擎今天真的会做的动作，不是平台官方宣传；False 都带中文原因",
        }
    )


@router.get("/api/im/traffic")
async def im_traffic(request: Request):
    """流量面：逐实例的限流读数、计数、最近一次被挡下的原因与附件用量

    只读：不放行、不清冷却。「429 时退避并显示中文状态」这件事要有地方能被看见，
    否则退避只存在于日志里，用户看到的就是「机器人不说话了」。
    """
    rows: list[dict] = []
    try:
        for runtime in imch.all_runtimes():
            rows.append(
                {
                    "kind": runtime.channel.kind,
                    "channelId": runtime.channel_id,
                    "instanceKey": runtime.instance_key,
                    "label": runtime.channel.label,
                    "state": runtime.state,
                    "configured": runtime.configured(),
                    "running": bool(runtime._task and not runtime._task.done()),
                    "rateLimit": runtime.governor.view(),
                    "counters": dict(runtime.counters),
                    "lastSkip": dict(runtime.last_skip or {}),
                    "sendNote": runtime.send_note,
                    "typingState": runtime.typing_state,
                    "dedupeNote": runtime.dedupe.note(
                        int(ipol.effective(runtime.channel_id, runtime.channel.kind).get("dedupeSeconds") or 0)
                    ),
                    "media": runtime.media.footprint(runtime.instance_key),
                    "lastError": runtime.last_error,
                }
            )
    except Exception as exc:  # noqa: BLE001
        return _fail("IM_TRAFFIC_UNREADABLE", f"流量面读取失败：{type(exc).__name__}：{str(exc)[:160]}", 500)
    cooled = sum(int(r["rateLimit"].get("blockedConversations") or 0) for r in rows)
    throttled = sum(int(r["counters"].get("throttled") or 0) for r in rows)
    return JSONResponse(
        {
            "ok": True,
            "rows": rows,
            "totals": {
                "instances": len(rows),
                "inbound": sum(int(r["counters"].get("inbound") or 0) for r in rows),
                "runs": sum(int(r["counters"].get("runs") or 0) for r in rows),
                "skipped": sum(int(r["counters"].get("skipped") or 0) for r in rows),
                "deduped": sum(int(r["counters"].get("deduped") or 0) for r in rows),
                "throttled": throttled,
                "mediaStored": sum(int(r["counters"].get("mediaStored") or 0) for r in rows),
                "mediaRejected": sum(int(r["counters"].get("mediaRejected") or 0) for r in rows),
            },
            "note": (
                f"{cooled} 个会话在平台冷却中（发送自动暂停），另有 {throttled} 条回复被逐实例限流挡下"
                if cooled or throttled
                else "没有限流或冷却中的会话"
            ),
        }
    )
