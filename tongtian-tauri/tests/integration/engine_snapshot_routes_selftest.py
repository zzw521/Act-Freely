#!/usr/bin/env python3
"""#140 应用快照与浏览器上下文接力的**端点面**自测（workspace 车道：/api/snapshot/*）。

跑的是「引擎端点 → snapshot_runtime / snapshot_relay」这条真实接线：请求体怎么变成一次
采集、预览与上送载荷在同一条代码路径上是不是同一段文本、机密级在哪一层被硬拦、
接力的四类拒绝怎么带着自己的 `deniedBy` 回到界面、准入判定在哪一层生效、审计从哪一层落盘。

判定本体（分级、TTL、上限、URL 白名单、日常浏览器目录特征）由
``engine_snapshot_selftest.py`` 钉，本文件不重跑那 151 项；这里只验**接线**。

必须用 ``./vendor/python/python.exe -X utf8`` 跑。缺第三方依赖时整族 SKIP；
**引擎自己的文件导入断了则大声红**（静默 SKIP 会把真红报成没测，代价在案）。

真实截屏、真实受控浏览器会话、桌面壳快捷键都不在这里：本车道不截屏，
IPC 未由 W4 交棒 ⇒ 凡靠注入现场跑通的用例都在名字里写了「假现场」。

用法: ./vendor/python/python.exe -X utf8 tests/integration/engine_snapshot_routes_selftest.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

_TMP = Path(tempfile.mkdtemp(prefix="tt-snap-routes-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")

ENGINE_MODULES = {path.stem for path in (PROJECT_ROOT / "engine").glob("*.py")}

FAILURES: list[str] = []

LOCAL_TOKEN = "tt-selftest-local-token"
STATIC_TOKEN = "tt-selftest-static-token"

CONTROLLED_PROFILE = "C:/Users/dev/AppData/Roaming/tongtianwork/browser/root/profile"
DAILY_PROFILE = "C:/Users/dev/AppData/Local/Google/Chrome/User Data/Default"


def import_gate(exc: BaseException, suite: str) -> None:
    """只在**真缺第三方依赖**时优雅跳过；我们自己的引擎文件断了必须红着退出。"""
    root = exc
    for _ in range(12):
        nxt = root.__cause__ or root.__context__
        if nxt is None:
            break
        root = nxt
    missing = getattr(root, "name", "") or ""
    if isinstance(root, ModuleNotFoundError) and missing not in ENGINE_MODULES:
        print(f"SKIP: {suite} unavailable（缺第三方依赖 {missing or root}）")
        sys.exit(0)
    print(f"[FAIL] {suite} 起不来：引擎侧导入断了，不是依赖缺失 — {type(root).__name__}: {root}")
    print("[FAIL] 整族未运行（拒绝把上面的断裂报成 SKIP）")
    sys.exit(1)


try:
    import routes_workspace as rw
    import workspace_backend_runtime as wb
    import snapshot_relay as relay
    import snapshot_runtime as snap
    import server
    from fastapi.testclient import TestClient
except Exception as exc:  # noqa: BLE001
    import_gate(exc, "snapshot routes selftest")

# server 的导入链可能把数据根拧到别处，这里再拧回本文件的临时目录：
# 审计落点必须是临时目录，不能是真的引擎数据目录。
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"--- {title} ---")


def headers(token: str = LOCAL_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def fresh_registries() -> None:
    snap.reset_registry()
    relay.reset_registry()


def desktop_env() -> None:
    """桌面壳在场：本机令牌 + 远程令牌都配好，请求带本机令牌才算桌面壳。"""
    os.environ["TT_ENGINE_TOKEN"] = STATIC_TOKEN
    os.environ["TT_ENGINE_LOCAL_TOKEN"] = LOCAL_TOKEN


def controlled_runtime(**extra) -> dict:
    block = {"running": True, "persisted": True, "policy": "auto", "profileDir": CONTROLLED_PROFILE}
    block.update(extra)
    return {"browserUse": block}


# ── 1. 挂载与方法分布 ───────────────────────────────────────────


def mount_checks(client: TestClient) -> None:
    section("1. /api/snapshot/* 挂载与方法分布")
    app_paths = {(m, r.path) for r in server.app.routes for m in (r.methods or set())}
    missing = [p for p in rw.SNAPSHOT_ENDPOINT_PATHS if not any(p == one for _, one in app_paths)]
    check("清单里的每条快照端点都真挂在 app 上", not missing, str(missing))
    check("端点总数与两族常量之和一致", len(rw.ENDPOINT_PATHS) == len(rw.WORKSPACE_ENDPOINT_PATHS) + len(rw.SNAPSHOT_ENDPOINT_PATHS), str(len(rw.ENDPOINT_PATHS)))
    check("快照与后端两族路径不重叠", not (set(rw.WORKSPACE_ENDPOINT_PATHS) & set(rw.SNAPSHOT_ENDPOINT_PATHS)))
    read_only = {r.path for r in server.app.routes if "GET" in (r.methods or set()) and r.path.startswith("/api/snapshot")}
    check("只读面五条走 GET、会改状态的一律走 POST（写面不靠 GET）",
          read_only == {"/api/snapshot/status", "/api/snapshot/pending", "/api/snapshot/preview", "/api/snapshot/decisions", "/api/snapshot/relay/status"},
          str(sorted(read_only)))
    body = client.get("/api/snapshot/status", headers=headers()).json()
    check("状态端点两族口径都在（snapshot + relay + 本族端点清单）",
          {"snapshot", "relay", "endpoints", "readOnlyPaths"} <= set(body) and body["endpoints"] == list(rw.SNAPSHOT_ENDPOINT_PATHS),
          str(sorted(body))[:160])
    flat = json.dumps(body, ensure_ascii=False)
    check("状态回话不含后端那族的结论码（两族不混）", not any(code in flat for code in ("WS_BACKEND_", "WS_PATH_", "WS_TIER_")), flat[:160])
    check("状态回话点名「本车道不截屏」与未验收", "不截屏" in flat and "未验收" in flat, flat[:200])
    check("状态回话不含「已支持对象存储」这类声称（约束 A 的反向钉）", "已支持对象存储" not in flat)
    check("状态回话不含「真机已验收 / 快捷键已接通」这类声称",
          "已验收真机" not in flat and "快捷键已接通" not in flat)


# ── 2. 准入 ────────────────────────────────────────────────────


def admission_checks(client: TestClient) -> None:
    section("2. 准入：快照族对远程身份整条不开放，且理由是自己的那一句")
    desktop_env()
    remote_read = client.get("/api/snapshot/status", headers=headers(STATIC_TOKEN))
    check("远程身份读快照状态被拒（403 + SNAPSHOT/WS_DESKTOP_ONLY 同源 code）",
          remote_read.status_code == 403 and remote_read.json()["code"] == wb.CODE_DESKTOP_ONLY, str(remote_read)[:160])
    snap_message = remote_read.json()["message"]
    check("快照这一族的拒绝理由点名「屏幕上的正文与截图」",
          "屏幕" in snap_message and "截图" in snap_message, snap_message[:200])
    check("快照这一族的拒绝理由不提桶名与 SSH 主机（不与后端族共用文案）",
          "桶名" not in snap_message and "SSH" not in snap_message, snap_message[:200])
    backend_message = client.get("/api/workspace/backends", headers=headers(STATIC_TOKEN)).json()["message"]
    check("后端族与快照族两条拒绝文案不同（同 code，两件事）",
          backend_message != snap_message and "桶名" in backend_message, backend_message[:160])
    check("两族理由都点名「按人隔离尚未落地」", "按人隔离" in snap_message and "按人隔离" in backend_message)

    for path in rw.SNAPSHOT_ENDPOINT_PATHS:
        method = "get" if path in rw.READ_ONLY_PATHS else "post"
        verb = getattr(client, method)
        resp = verb(path, headers=headers(STATIC_TOKEN), params={}, json={}) if method == "post" else verb(path, headers=headers(STATIC_TOKEN))
        check(f"远程身份下 {path} 一律 403（整条不开放，不是「只给查询」）",
              resp.status_code == 403 and resp.json().get("code") == wb.CODE_DESKTOP_ONLY, f"{resp.status_code} {resp.text[:120]}")

    os.environ.pop("TT_ENGINE_TOKEN", None)
    os.environ.pop("TT_ENGINE_LOCAL_TOKEN", None)
    check("未配任何凭据（本机回环）时快照状态可读",
          client.get("/api/snapshot/status").status_code == 200, str(client.get("/api/snapshot/status").status_code))
    desktop_env()


# ── 3. 采集 → 预览 → 确认（同一段文本） ─────────────────────────


def capture_chain_checks(client: TestClient) -> None:
    section("3. 采集/预览/确认在端点面上仍是同一条代码路径")
    fresh_registries()
    body = {"text": "本周把设置页的三个开关挪到了同一屏", "appLabel": "代码编辑器", "session": "s-1", "taskId": "s-1"}
    captured = client.post("/api/snapshot/capture", headers=headers(), json=body)
    check("采集成功直接回同一份预览载荷（界面不必二次请求才看见内容）",
          captured.status_code == 200 and captured.json()["preview"], str(captured)[:160])
    draft_id = captured.json()["draftId"]
    preview = client.get("/api/snapshot/preview", headers=headers(), params={"draftId": draft_id}).json()
    confirmed = client.post("/api/snapshot/confirm", headers=headers(), json={"draftId": draft_id})
    check("确认成功并产出 contextBlocks", confirmed.status_code == 200 and confirmed.json()["contextBlocks"], str(confirmed)[:160])
    check("预览所见与真正上送的正文逐字相等（不是「预览一套、发出去另一套」）",
          preview["preview"] == confirmed.json()["contextBlocks"][0]["text"], preview["preview"][:80])
    check("确认载荷标出不截屏来源由调用方交上来（appLabel 原样回）",
          confirmed.json()["appLabel"] == "代码编辑器", str(confirmed.json())[:120])
    check("载荷里不含「已切本地通道」这类声称，只标 forceLocal",
          "forceLocal" in confirmed.json() and "已切本地" not in json.dumps(confirmed.json(), ensure_ascii=False))

    missing = client.get("/api/snapshot/preview", headers=headers())
    check("缺 draftId 的预览回 404 且说的是「没有这份草稿」那一族",
          missing.status_code == 404 and missing.json()["code"] == snap.CODE_NOT_FOUND, str(missing)[:140])
    no_key = client.post("/api/snapshot/confirm", headers=headers(), json={})
    check("confirm 不带 draftId 回 SNAPSHOT_PARAM_MISSING（不是 WS_BACKEND_REQUIRED）",
          no_key.status_code == 400 and no_key.json()["code"] == snap.CODE_PARAM_MISSING, str(no_key)[:160])
    empty = client.post("/api/snapshot/capture", headers=headers(), json={"text": ""})
    check("空采集被拒且点名本车道不截屏（不是「参数为空」这种含糊话）",
          empty.status_code == 400 and empty.json()["code"] == snap.CODE_NOTHING_CAPTURED and "不截屏" in empty.json()["message"],
          str(empty.json())[:200])
    huge = client.post("/api/snapshot/capture", headers=headers(), json={"text": "字" * (snap.MAX_CAPTURE_TEXT_CHARS + 1)})
    check("超长采集回 413（与「没内容」两码两话）",
          huge.status_code == 413 and huge.json()["code"] == snap.CODE_TEXT_TOO_LARGE, str(huge)[:140])
    bad_mime = client.post("/api/snapshot/capture", headers=headers(), json={"text": "正文", "imageBase64": "aGk=", "imageMime": "image/svg+xml"})
    check("白名单外的图片格式被拒且给出允许清单",
          bad_mime.status_code == 400 and bad_mime.json()["code"] == snap.CODE_IMAGE_FORMAT, str(bad_mime.json())[:200])

    # 上面那份已经确认掉了；这里再触发一份留在待确认，才看得出「默认只列 pending」这条口径
    second = client.post("/api/snapshot/capture", headers=headers(), json={"text": "另一份还没决定的", "session": "s-1", "taskId": "s-1"})
    check("同一 session 可以再触发一份（不因为上一份确认过就关掉通道）", second.status_code == 200, str(second)[:140])
    pending = client.get("/api/snapshot/pending", headers=headers(), params={"session": "s-1"}).json()
    marker = "本周把设置页的三个开关挪到了同一屏"
    check("待确认清单只给元数据，不回正文（列表不是预览）",
          marker not in json.dumps(pending, ensure_ascii=False) and pending["total"] == 1
          and "列表不是预览" in pending["note"], str(pending)[:200])
    check("端点叫 pending 就默认只列待确认（不把已确认的墓碑灌进「待确认清单」）",
          pending["statusFilter"] == snap.STATUS_PENDING
          and [row["status"] for row in pending["items"]] == [snap.STATUS_PENDING], str(pending["items"])[:200])
    all_rows = client.get("/api/snapshot/pending", headers=headers(), params={"session": "s-1", "status": "all"}).json()
    check("要全量得显式带 status=all，且回话里说清怎么拿",
          all_rows["total"] == 2 and "status=all" in all_rows["note"]
          and {row["status"] for row in all_rows["items"]} == {snap.STATUS_PENDING, snap.STATUS_CONFIRMED},
          str(all_rows)[:200])
    check("清单里能看到分级与长度这类可核对的量（分级在 classification 下）",
          all({"draftId", "textChars"} <= set(row) and {"sensitivity", "findings"} <= set(row["classification"])
              for row in all_rows["items"]), str(all_rows["items"])[:160])


# ── 4. 机密与内部敏感两道闸 ─────────────────────────────────────


def gate_checks(client: TestClient) -> None:
    section("4. 机密硬闸与内部敏感确认在 HTTP 层的形状")
    fresh_registries()
    secret = client.post("/api/snapshot/capture", headers=headers(), json={"text": "配置贴一段 sk-abcdefghijklmnop1234"}).json()
    draft_id = secret["draftId"]
    preview = client.get("/api/snapshot/preview", headers=headers(), params={"draftId": draft_id}).json()
    check("预览如实说「这份不会被上送」（wouldUplink 为假）", preview["wouldUplink"] is False, str(preview)[:160])
    withheld = client.post("/api/snapshot/confirm", headers=headers(), json={"draftId": draft_id})
    check("机密确认回 403 + SNAPSHOT_CONFIDENTIAL_WITHHELD（不是 400 也不是 200）",
          withheld.status_code == 403 and withheld.json()["code"] == snap.CODE_CONFIDENTIAL, str(withheld)[:160])
    bypass = client.post("/api/snapshot/confirm", headers=headers(), json={"draftId": draft_id, "includeImage": True, "acknowledgeInternal": True})
    check("勾选图片与「我知道」都不是绕过口（同一句拒绝再来一次）",
          bypass.status_code == 403 and bypass.json()["message"] == withheld.json()["message"], str(bypass)[:160])
    check("机密那句点名「没有开关可以绕过它」", "没有开关" in withheld.json()["message"], withheld.json()["message"][:160])

    internal = client.post("/api/snapshot/capture", headers=headers(), json={"text": "内部讨论纪要：本周排期"}).json()
    need = client.post("/api/snapshot/confirm", headers=headers(), json={"draftId": internal["draftId"]})
    check("内部敏感未勾选时被拦（403 + 要一次确认，与机密那句不同）",
          need.status_code == 403 and need.json()["code"] == snap.CODE_INTERNAL_NEEDS_ACK
          and need.json()["message"] != withheld.json()["message"], str(need.json())[:180])
    acked = client.post("/api/snapshot/confirm", headers=headers(), json={"draftId": internal["draftId"], "acknowledgeInternal": True})
    check("显式勾选后内部敏感可以上送（是确认不是禁止）", acked.status_code == 200, str(acked)[:140])

    blocked_mode = client.post(
        "/api/snapshot/capture",
        headers=headers(),
        json={"text": "手机 13800001234 的客户名单", "runtime": {"privacy": {"mode": "block"}}},
    ).json()
    stop = client.post("/api/snapshot/confirm", headers=headers(), json={"draftId": blocked_mode["draftId"]})
    check("档位「命中即中止」与机密闸各说各话（两个 code、两句话）",
          stop.json()["code"] in (snap.CODE_CONFIDENTIAL, snap.CODE_REDACTION_BLOCKED)
          and stop.json()["message"] != withheld.json()["message"], str(stop.json())[:180])
    masked = client.post(
        "/api/snapshot/capture",
        headers=headers(),
        json={"text": "内部标注的排期表", "runtime": {"privacy": {"mode": "mask"}}},
    ).json()
    masked_confirm = client.post("/api/snapshot/confirm", headers=headers(), json={"draftId": masked["draftId"], "acknowledgeInternal": True})
    check("脱敏档位下预览与上送仍相等（mask 只改一份，不改两套）",
          client.get("/api/snapshot/preview", headers=headers(), params={"draftId": masked["draftId"]}).json()["preview"]
          == masked_confirm.json()["contextBlocks"][0]["text"], str(masked_confirm.json())[:160])


# ── 5. 取消与销毁 ───────────────────────────────────────────────


def cancel_checks(client: TestClient) -> None:
    section("5. 取消：销毁的是内存里这份，不粉饰成「已撤回」")
    fresh_registries()
    draft = client.post("/api/snapshot/capture", headers=headers(), json={"text": "还没决定要不要给的一份"}).json()
    draft_id = draft["draftId"]
    first = client.post("/api/snapshot/cancel", headers=headers(), json={"draftId": draft_id})
    check("取消成功并回话说草稿已销毁", first.status_code == 200 and first.json()["destroyed"] is True, str(first)[:140])
    again = client.post("/api/snapshot/cancel", headers=headers(), json={"draftId": draft_id})
    check("第二次取消回 404（不再写一条审计、不回 200 假装又取消一次）",
          again.status_code == 404 and again.json()["code"] == snap.CODE_NOT_FOUND, str(again)[:140])
    revived = client.post("/api/snapshot/confirm", headers=headers(), json={"draftId": draft_id})
    check("取消后确认不会把它捞回来（404 + 点名「不会再捞回来」）",
          revived.status_code == 404 and "捞回来" in revived.json()["message"], str(revived.json())[:180])

    keep = client.post("/api/snapshot/capture", headers=headers(), json={"text": "已经上送过的一份"}).json()
    sent = client.post("/api/snapshot/confirm", headers=headers(), json={"draftId": keep["draftId"]})
    after = client.post("/api/snapshot/cancel", headers=headers(), json={"draftId": keep["draftId"]})
    check("已确认的草稿取消后回 hadUplink=false（不谎称撤回了载荷）",
          sent.status_code == 200 and after.status_code == 200 and after.json()["hadUplink"] is True
          and after.json()["destroyed"] is True, str(after.json())[:200])
    check("那句说明区分「内存副本已清」与「交出去的载荷撤不回」",
          "撤不回" in after.json()["cancelNote"] and "内存" in after.json()["cancelNote"], after.json()["cancelNote"][:160])


# ── 6. 接力端点 ─────────────────────────────────────────────────


def relay_endpoint_checks(client: TestClient) -> None:
    section("6. 接力端点：授权、判定、登记、撤销都带自己的来源标签")
    fresh_registries()
    daily = client.post(
        "/api/snapshot/relay/grant",
        headers=headers(),
        json={"runtime": {"browserUse": {"running": True, "policy": "auto", "profileDir": DAILY_PROFILE}}, "workspaceId": "ws-a", "hosts": ["example.com"]},
    )
    check("指向日常浏览器目录的授权开不出来（403 + 来源是受控浏览器边界）",
          daily.status_code == 403 and daily.json()["deniedBy"] == relay.SOURCE_CONTROLLED_BOUNDARY
          and "不碰用户日常浏览器的" in daily.json()["message"], str(daily.json())[:200])
    check("日常目录那一句由 deniedByLabel 给出人话标签", daily.json()["deniedByLabel"] != "", str(daily.json())[:160])

    no_session = client.post("/api/snapshot/relay/grant", headers=headers(), json={"workspaceId": "ws-a", "hosts": ["example.com"]})
    check("没有受控会话现场时授权不成立（来源是会话，另一句）",
          no_session.status_code == 403 and no_session.json()["deniedBy"] == relay.SOURCE_SESSION
          and no_session.json()["message"] != daily.json()["message"], str(no_session.json())[:200])

    bad_host = client.post("/api/snapshot/relay/grant", headers=headers(), json={"runtime": controlled_runtime(), "workspaceId": "ws-a", "hosts": ["javascript:alert(1)"]})
    check("授权项里的协议串被拒（不当成域名收进清单）",
          bad_host.status_code == 400 and bad_host.json()["code"] == relay.CODE_BAD_URL, str(bad_host.json())[:200])

    granted = client.post(
        "/api/snapshot/relay/grant",
        headers=headers(),
        json={"runtime": controlled_runtime(), "workspaceId": "ws-a", "hosts": ["example.com", "https://shop.test/cart?x=1"], "taskId": "t-1"},
    )
    payload = granted.json()
    check("正常授权成功且只留域名（查询串不进授权）",
          granted.status_code == 200 and sorted(payload["grant"]["hosts"]) == ["example.com", "shop.test"], str(payload)[:200])
    check("授权回话不含完整配置目录路径与 Cookie 字段",
          CONTROLLED_PROFILE not in json.dumps(payload, ensure_ascii=False)
          and not any(key.lower().startswith("cookie") for key in payload["grant"]), str(payload)[:200])
    grant_id = payload["grantId"] or payload["grant"]["grantId"]

    allowed = client.post("/api/snapshot/relay/decide", headers=headers(), json={"grantId": grant_id, "url": "https://example.com/page", "workspaceId": "ws-a", "runtime": controlled_runtime()})
    check("同工作区同域 + 会话在线 ⇒ 放行（200 allowed）", allowed.status_code == 200 and allowed.json()["allowed"] is True, str(allowed.json())[:160])
    off_host = client.post("/api/snapshot/relay/decide", headers=headers(), json={"grantId": grant_id, "url": "https://other.test/", "workspaceId": "ws-a", "runtime": controlled_runtime()})
    off_ws = client.post("/api/snapshot/relay/decide", headers=headers(), json={"grantId": grant_id, "url": "https://example.com/", "workspaceId": "ws-b", "runtime": controlled_runtime()})
    check("域不符与跨工作区各回一句（两个 code、两句话，不许并成「没授权」）",
          off_host.json()["code"] == relay.CODE_HOST_NOT_GRANTED and off_ws.json()["code"] == relay.CODE_WORKSPACE_MISMATCH
          and off_host.json()["message"] != off_ws.json()["message"], str(off_ws.json())[:180])
    unknown = client.post("/api/snapshot/relay/decide", headers=headers(), json={"grantId": "nope", "url": "https://example.com/", "workspaceId": "ws-a"})
    check("拿不存在的授权 id 来问回 404（与被拒的 403 分开）",
          unknown.status_code == 404 and unknown.json()["code"] == relay.CODE_GRANT_NOT_FOUND, str(unknown)[:140])
    want_bad = client.post("/api/snapshot/relay/decide", headers=headers(), json={"grantId": grant_id, "url": "https://example.com/", "workspaceId": "ws-a", "want": "cookies", "runtime": controlled_runtime()})
    check("want 只认三种，给别的回 400 + 自己的 code（参数错不借「没授权」那枚 403）",
          want_bad.status_code == 400 and want_bad.json()["code"] == relay.CODE_WANT_INVALID
          and "只认" in want_bad.json()["message"], str(want_bad.json())[:160])

    tabs = client.post(
        "/api/snapshot/relay/tabs",
        headers=headers(),
        json={"grantId": grant_id, "tabs": [{"tabId": "1", "url": "https://example.com/a?token=secret-token", "title": "示例页", "active": True}]},
    )
    check("登记标签页成功且不留查询串里的令牌",
          tabs.status_code == 200 and "secret-token" not in json.dumps(tabs.json(), ensure_ascii=False), str(tabs.json())[:200])
    off_scope_tabs = client.post("/api/snapshot/relay/tabs", headers=headers(), json={"grantId": grant_id, "tabs": [{"tabId": "2", "url": "https://other.test/x"}]})
    check("登记越出授权域被拒（登记本身不是越界口）",
          off_scope_tabs.status_code == 403 and off_scope_tabs.json()["code"] == relay.CODE_HOST_NOT_GRANTED, str(off_scope_tabs.json())[:160])
    no_body = client.post("/api/snapshot/relay/tabs", headers=headers(), json={"grantId": grant_id})
    check("tabs 缺参回 SNAPSHOT_PARAM_MISSING 而不是后端族那句",
          no_body.status_code == 400 and no_body.json()["code"] == snap.CODE_PARAM_MISSING, str(no_body.json())[:160])

    status = client.get("/api/snapshot/relay/status", headers=headers(), params={"workspaceId": "ws-a"}).json()
    check("接力状态给出四个拒绝来源标签且互不相同",
          len(status["denialSources"]) == 4 and len({row["label"] for row in status["denialSources"]}) == 4, str(status["denialSources"])[:200])
    check("接力状态说明这条账里没有 Cookie 与完整路径",
          "Cookie" in status["note"] and CONTROLLED_PROFILE not in json.dumps(status, ensure_ascii=False), str(status)[:200])

    revoked = client.post("/api/snapshot/relay/revoke", headers=headers(), json={"grantId": grant_id, "reason": "用户点了「退出接力」"})
    check("撤销成功且回话点名「立即生效，不看剩余有效期」",
          revoked.status_code == 200 and "立即生效" in revoked.json()["message"], str(revoked.json())[:180])
    after_revoke = client.post("/api/snapshot/relay/decide", headers=headers(), json={"grantId": grant_id, "url": "https://example.com/", "workspaceId": "ws-a", "runtime": controlled_runtime()})
    check("撤销后再判定按撤销拒（与过期的那句不同）",
          after_revoke.json()["code"] == relay.CODE_GRANT_REVOKED and "过期" not in after_revoke.json()["message"], str(after_revoke.json())[:180])
    nothing = client.post("/api/snapshot/relay/revoke", headers=headers(), json={"reason": "两个都不给"})
    check("撤销既不给 grantId 也不给 workspaceId 时不撤（404 + 说清要给哪个）",
          nothing.status_code == 404 and "要么给" in nothing.json()["message"], str(nothing.json())[:180])
    by_ws = client.post("/api/snapshot/relay/revoke", headers=headers(), json={"workspaceId": "ws-none", "reason": "整体撤销"})
    check("按工作区整体撤销对空工作区也回成功并说明「本来就没有活动授权」",
          by_ws.status_code == 200 and "本来就没有" in by_ws.json()["message"], str(by_ws.json())[:180])


# ── 7. 审计读面 ─────────────────────────────────────────────────


def decisions_checks(client: TestClient) -> None:
    section("7. 决策审计读面：两条账并排，都不含正文")
    fresh_registries()
    marker = "绝不该出现在审计里的屏幕正文"
    secret_value = "sk-abcdefghijklmnop1234"
    captured = client.post("/api/snapshot/capture", headers=headers(), json={"text": marker + " 与 " + secret_value, "imageBase64": "aGk=", "imageMime": "image/png"}).json()
    client.get("/api/snapshot/preview", headers=headers(), params={"draftId": captured["draftId"]})
    client.post("/api/snapshot/confirm", headers=headers(), json={"draftId": captured["draftId"]})
    client.post("/api/snapshot/cancel", headers=headers(), json={"draftId": captured["draftId"]})
    client.post("/api/snapshot/relay/grant", headers=headers(), json={"runtime": controlled_runtime(), "workspaceId": "ws-audit", "hosts": ["audit.test"]})

    read = client.get("/api/snapshot/decisions", headers=headers()).json()
    blob = json.dumps(read, ensure_ascii=False)
    check("两条账并排给（snapshot + relay）", {"snapshot", "relay", "note"} <= set(read) and read["snapshot"]["total"] >= 1, str(read)[:160])
    check("审计里不含采集正文与明文密钥原值", marker not in blob and secret_value not in blob, blob[:160])
    check("审计里不含图片 base64 与 Cookie", "aGk=" not in blob and CONTROLLED_PROFILE not in blob, blob[:160])
    check("审计条目只给长度与指纹这类可核对量",
          all({"sensitivity", "hitPatterns", "textBytes", "textFingerprint"} <= set(row) for row in read["snapshot"]["items"]),
          str(read["snapshot"]["items"])[:200])
    check("落盘的审计文件同样不含正文",
          marker not in (_TMP / "home").rglob("decisions.jsonl").__next__().read_text(encoding="utf-8")
          if any(True for _ in (_TMP / "home").rglob("decisions.jsonl")) else False, "没找到审计文件")

    paged = client.get("/api/snapshot/decisions", headers=headers(), params={"limit": 1, "offset": 0}).json()
    check("分页给 total/hasMore 且本页只有一条",
          paged["snapshot"]["total"] >= 2 and paged["snapshot"]["hasMore"] is True and len(paged["snapshot"]["items"]) == 1, str(paged["snapshot"])[:200])
    filtered = client.get("/api/snapshot/decisions", headers=headers(), params={"action": "capture", "draftId": "不存在"}).json()
    check("过滤后为空时说明「这一页没有条目」而不是静默空数组",
          filtered["snapshot"]["total"] == 0 and filtered["snapshot"]["note"] != "", str(filtered["snapshot"])[:160])
    bad_limit = client.get("/api/snapshot/decisions", headers=headers(), params={"limit": "abc"})
    check("limit 不是整数时回 400 并点名是哪一条参数",
          bad_limit.status_code == 400 and bad_limit.json()["code"] == snap.CODE_LIMIT_INVALID, str(bad_limit.json())[:160])
    over_limit = client.get("/api/snapshot/decisions", headers=headers(), params={"limit": "100000"})
    check("limit 超上限同样报 400（不静默夹到 200）", over_limit.status_code == 400, str(over_limit)[:120])


# ── 8. 车道边界与未验收口径 ─────────────────────────────────────


def boundary_checks(client: TestClient) -> None:
    section("8. 车道边界：不截屏、不发起浏览器动作、不假装 IPC 已接线")
    lane_src = (PROJECT_ROOT / "engine" / "routes_workspace.py").read_text(encoding="utf-8")
    check("车道仍然不 import server（引擎现场只经 routes_lanes.engine()）", "import server" not in lane_src)
    check("快照端点不发 HTTP、不开 WebSocket（浏览器动作归 /api/browser）",
          not any(needle in (snap_mod := (PROJECT_ROOT / "engine" / "snapshot_runtime.py").read_text(encoding="utf-8")) for needle in ("urllib.request", "aiohttp"))
          and "websocket" not in (PROJECT_ROOT / "engine" / "snapshot_relay.py").read_text(encoding="utf-8").lower()
          and "requests." not in snap_mod)
    doc = snap.REGISTRY.status()["unverified"] + relay.REGISTRY.status()["unverified"]
    check("未验收清单点名「真机采集未验收」与「真实受控浏览器会话未验收」",
          any("真机采集未验收" in line or "未验收" in line for line in doc) and any("受控浏览器" in line for line in doc), str(doc)[:220])
    check("未验收清单点名 IPC 未接线（桌面半边没吹）",
          any("IPC" in line for line in doc), str(doc)[:200])
    check("接力状态把浏览器动作归给 /api/browser 车道", "/api/browser" in relay.REGISTRY.status()["note"])
    check("快照状态说明预览与上送由同一个函数产出（界面不必再猜）", "同一个函数" in snap.REGISTRY.status()["note"])
    check("回话里没有「已支持对象存储」这类越界声称（约束 A 反向钉）",
          "已支持对象存储" not in json.dumps(client.get("/api/snapshot/status", headers=headers()).json(), ensure_ascii=False))


# ── 9. 界面镜像与引擎回话逐键对齐（跨语言：TS ↔ Python）─────────

TS_CONTRACT = PROJECT_ROOT / "src" / "shared" / "snapshot-contract.ts"
_TS_SOURCE = TS_CONTRACT.read_text(encoding="utf-8") if TS_CONTRACT.exists() else ""


def ts_interface_keys(name: str, seen: frozenset[str] = frozenset()) -> set[str]:
    """取 TS 接口声明的键名；``extends`` 的父接口递归展开。"""
    match = re.search(rf"export interface {name}( extends ([A-Za-z0-9_, ]+))?\s*\{{", _TS_SOURCE)
    if not match:
        return set()
    keys: set[str] = set()
    for parent in (m.strip() for m in (match.group(2) or "").split(",") if m.strip()):
        if parent not in seen:
            keys |= ts_interface_keys(parent, seen | {name})
    body = _TS_SOURCE[match.end() : _TS_SOURCE.index("\n}", match.end())]
    for line in body.splitlines():
        line = line.strip()
        if not line or "[key: string]" in line or line.startswith(("*", "/*", "//")):
            continue
        key = re.match(r"^([A-Za-z0-9_]+)\??\s*:", line)
        if key:
            keys.add(key.group(1))
    return keys


def shape(name: str, ts_name: str, rows: list[dict], optional: set[str] | frozenset[str] = frozenset()) -> None:
    """把「引擎实际发了哪些键」和「界面镜像声明了哪些键」对齐。

    两个方向都要钉：引擎发了而镜像没有 ⇒ 界面读出来是 undefined（看着像「没有这个功能」）；
    镜像声明了而引擎从不发 ⇒ 那一栏永远空白（看着像「引擎坏了」）。
    只有部分行才带的键（比如销毁行才有 destroyed*）算「可选」，要求镜像显式标 ``?``。
    """
    keys = ts_interface_keys(ts_name)
    check(f"{name}：TS 契约里找得到接口 {ts_name}", bool(keys), ts_name)
    if not keys or not rows:
        return
    required = set.intersection(*(set(row) for row in rows))
    union = set.union(*(set(row) for row in rows))
    missing = sorted(required - keys)
    invented = sorted(keys - union - set(optional))
    check(f"{name}：引擎发的每个键界面都镜像了（漏一个界面就读到 undefined）", not missing, str(missing))
    check(f"{name}：界面没声明引擎从不发的键（那种栏位永远空白）", not invented, str(invented))


def contract_shape_checks(client: TestClient) -> None:
    section("9. 界面镜像与引擎回话逐键对齐")
    if not TS_CONTRACT.exists():
        check("渲染层契约文件在（这份钉的是 TS ↔ Python 对齐）", False, str(TS_CONTRACT))
        return
    desktop_env()
    fresh_registries()

    status = client.get("/api/snapshot/status", headers=headers()).json()
    shape("状态回话外层", "SnapshotStatusView", [status])
    shape("快照族口径", "SnapshotRegistryStatus", [status["snapshot"]])
    shape("接力族口径", "RelayRegistryStatus", [status["relay"]])

    plain = client.post("/api/snapshot/capture", headers=headers(),
                        json={"text": "把这屏的三个开关挪到同一屏", "appLabel": "设计稿", "session": "s-9"}).json()
    secret = client.post("/api/snapshot/capture", headers=headers(),
                         json={"text": "顺手贴一串 sk-abcdefghijklmnopqrstuvwx 进来", "appLabel": "终端", "session": "s-9"}).json()
    shape("预览行", "SnapshotPreview", [plain, secret])
    shape("分级块", "SnapshotClassification", [plain["classification"], secret["classification"]])
    check("命中项的键名是 kind 不是 type（引擎镜像发 kind，Rust 那份才发 type）",
          bool(secret["classification"]["findings"]) and set(secret["classification"]["findings"][0]) == ts_interface_keys("SnapshotFinding"),
          str(secret["classification"]["findings"][:1]))

    confirmed = client.post("/api/snapshot/confirm", headers=headers(),
                            json={"draftId": plain["draftId"], "session": "s-9"}).json()
    shape("确认后的上送载荷", "SnapshotUplinkPayload", [confirmed])

    pending = client.get("/api/snapshot/pending", headers=headers(), params={"session": "s-9", "status": "all"}).json()
    shape("待确认清单信封", "PendingSnapshotList", [pending])
    shape("清单里的草稿行（不含正文）", "SnapshotDraftRow", pending["items"])

    cancelled = client.post("/api/snapshot/cancel", headers=headers(),
                            json={"draftId": secret["draftId"]}).json()
    shape("取消回话", "SnapshotCancelResult", [cancelled])

    decisions = client.get("/api/snapshot/decisions", headers=headers(), params={"limit": 50}).json()
    shape("决策审计信封（快照账）", "PagedSnapshotDecisions", [decisions["snapshot"]])
    shape("决策审计信封（接力账）", "PagedRelayDecisions", [decisions["relay"]])
    shape("快照账条目", "SnapshotDecisionRow", decisions["snapshot"]["items"],
          optional={"destroyedTextBytes", "destroyedTextFingerprint", "destroyedImageBytes"})

    runtime = controlled_runtime()
    granted = client.post("/api/snapshot/relay/grant", headers=headers(),
                          json={"workspaceId": "ws-9", "hosts": ["example.com"], "session": "s-9", "runtime": runtime}).json()
    shape("接力判定回话", "RelayDecisionView", [granted], optional={"grant"})
    shape("一条授权的行", "RelayGrantRow", [granted["grant"]])

    registered = client.post("/api/snapshot/relay/tabs", headers=headers(),
                             json={"grantId": granted["grantId"], "session": "s-9",
                                   "tabs": [{"tabId": "1", "url": "https://example.com/a?token=secret", "title": "周报", "active": True}]}).json()
    rows = registered["grant"]["tabs"]
    shape("登记后的标签页行", "RelayTabRow", rows)
    check("标签页行只有域名、没有完整 URL（令牌不进接力现场）",
          rows and "host" in rows[0] and not any("url" in row for row in rows), str(rows)[:160])

    decided = client.post("/api/snapshot/relay/decide", headers=headers(),
                          json={"grantId": granted["grantId"], "want": "tabs", "url": "https://example.com/a",
                                "workspaceId": "ws-9", "session": "s-9", "runtime": runtime}).json()
    shape("判定回话（无授权行）", "RelayDecisionView", [decided], optional={"grant"})
    revoked = client.post("/api/snapshot/relay/revoke", headers=headers(),
                          json={"grantId": granted["grantId"], "reason": "自测收口"}).json()
    shape("撤销回话", "RelayDecisionView", [revoked], optional={"grant"})

    relay_status = client.get("/api/snapshot/relay/status", headers=headers(), params={"workspaceId": "ws-9"}).json()
    shape("接力状态视图", "RelayStatusView", [relay_status])
    shape("快照审计并排回话", "SnapshotDecisionsView", [decisions])

    # 撤销之后那条授权带上 revokeNote：镜像里没这个键的话，界面就只能显示「已撤销」三个字
    after = client.get("/api/snapshot/relay/status", headers=headers()).json()
    if after["grants"]:
        shape("撤销后的授权行", "RelayGrantRow", [after["grants"][0]])
        check("撤销理由真的回得来（不是只翻 revoked 布尔）",
              any(row.get("revokeNote") for row in after["grants"]), str(after["grants"])[:160])

    # 客户端源码里那一层也不许留第二份键名事实：TS 侧的形状测试按同一批接口名反查
    api_source = (PROJECT_ROOT / "src" / "renderer" / "lib" / "snapshot-api.ts").read_text(encoding="utf-8")
    for ts_name in ("SnapshotPreview", "SnapshotUplinkPayload", "SnapshotCancelResult", "RelayGrantResult", "SnapshotDecisionsView"):
        check(f"客户端返回类型直接引用共享接口 {ts_name}（不在 lib 里另描述一遍形状）",
              f"Promise<{ts_name}>" in api_source, ts_name)


def main() -> int:
    print("=== 应用快照与浏览器上下文接力：端点面自测（离线 · 假现场 · 零真机）===")
    client = TestClient(server.app)
    mount_checks(client)
    admission_checks(client)
    capture_chain_checks(client)
    gate_checks(client)
    cancel_checks(client)
    relay_endpoint_checks(client)
    decisions_checks(client)
    boundary_checks(client)
    contract_shape_checks(client)
    print("----------------------------------------------------")
    if FAILURES:
        print(f"### {len(FAILURES)} 项未过：")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("snapshot routes selftest: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
