#!/usr/bin/env python3
"""
IM 频道自测 —— 个人微信（腾讯官方 iLink 机器人协议，长轮询 HTTP）

分三段，A/B 全部打真实网络（对端是本机起的假 iLink 网关，不是 Mock 返回值）：

A. 扫码绑定：/api/im/channels 里微信来自引擎注册表且 loginMode=qr、没有可手填字段；
   手填凭据被 IM_LOGIN_REQUIRED 拒绝；login/start 真的按官方形状请求
   （bot_type=3、body 只有 local_token_list、四个固定请求头）；状态按
   wait → scaned → confirmed 推进，confirmed 时引擎落盘并起连接，接口响应里没有 bot_token。
B. 长轮询全链路：getupdates 带上游标与 base_info；文本起任务、重复 msgid/机器人回声/
   图片/空文本都不起；回贴跑 sendmessage（message_type=2、message_state=2、item_list 只有
   文本项、带该用户的 context_token）；会话台账与游标各自落盘；视图不泄 token。
C. 方言分支：-14 登录态过期映射成不可重试的 IM_AUTH_FAILED 并停止轮询；二维码过期/未知票据/
   配对码状态各自映射；没有 context_token 时如实报 IM_REPLY_FAILED；超长回贴按 4000 字切分；
   请求头与 UIN 的编码形状；12 位 client version = 132106。

数据隔离：TT_DATA_HOME 与 WORK_DIR 都指向临时目录，绝不写 ~/.tongtianwork 的存量台账。
用法: python tests/integration/engine_im_wechat_selftest.py
"""

import asyncio
import base64
import json
import os
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TMP = Path(tempfile.mkdtemp(prefix="tt-im-wechat-"))
os.environ["TT_DATA_HOME"] = str(_TMP / "home")
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import aiohttp
    from fastapi.testclient import TestClient
    from fake_wechat import (
        DEFAULT_BOT_ID,
        DEFAULT_BOT_TOKEN,
        DEFAULT_USER_ID,
        SECOND_USER_ID,
        FakeWeChat,
        user_message,
    )
    import server as engine_server
    import im_channels
    import wechat_channel
except Exception as exc:  # 依赖缺失时优雅跳过（与其余引擎自测一致）
    print(f"SKIP: engine im wechat selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def wait_for(predicate, timeout=10.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def _channel_of(client, kind: str) -> dict:
    body = client.get("/api/im/channels").json()
    return next(c for c in body["channels"] if c["kind"] == kind)


def _sessions_of(client) -> list[dict]:
    return client.get("/api/im/channels").json().get("sessions") or []


def _creds() -> dict:
    return {
        "botToken": DEFAULT_BOT_TOKEN,
        "ilinkBotId": DEFAULT_BOT_ID,
        "ilinkUserId": DEFAULT_USER_ID,
        "baseUrl": os.environ["TT_WECHAT_BASE_URL"],
    }


class _HttpRuntime:
    """只给通道一个 HTTP 会话：send/_request 只用到 runtime.http"""

    def __init__(self, session):
        self.http = session


# ── A/B 段：扫码绑定 + 长轮询全链路 ────────────────────────────────


def phase_ab(fake: FakeWeChat) -> None:
    print("\n== A. 扫码绑定（对端为本机假 iLink 网关） ==")
    engine_server.WORK_DIR = _TMP / "work"
    engine_server.WORK_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["TT_WECHAT_BASE_URL"] = fake.base_url
    os.environ["TT_WECHAT_POLL_SECONDS"] = "0.5"
    os.environ["TT_WECHAT_LOGIN_POLL_SECONDS"] = "0.5"

    with TestClient(engine_server.app) as client:
        body = client.get("/api/im/channels").json()
        channels = {c["kind"]: c for c in body["channels"]}
        check("清单仍是六个通道", {"slack", "dingtalk", "feishu", "wecom", "wechat", "yuanbao"} == set(channels), str(sorted(channels)))
        wx = channels["wechat"]
        check("个人微信由引擎注册且标记为已接入", wx["supported"] is True, str(wx)[:200])
        check("个人微信是扫码登录，不提供手填凭据字段", wx["loginMode"] == "qr" and wx["fields"] == [], str(wx)[:200])
        check(
            "指引说清是官方 iLink 协议、只支持单聊、24 小时窗口",
            "iLink" in wx["setupNote"] and "单聊" in wx["setupNote"] and "24 小时" in wx["setupNote"],
            wx["setupNote"][:220],
        )
        check(
            "元宝通道也已接入（本轮同时补的两条）",
            channels["yuanbao"]["supported"] is True
            and [f["key"] for f in channels["yuanbao"]["fields"]] == ["appKey", "appSecret"],
            str(channels["yuanbao"])[:200],
        )

        manual = client.post("/api/im/wechat/config", json={"credentials": {"botToken": "x"}}).json()
        check("扫码通道拒绝手填凭据（IM_LOGIN_REQUIRED）", manual.get("code") == "IM_LOGIN_REQUIRED", str(manual)[:200])
        empty_ticket = client.post("/api/im/wechat/login/poll", json={"ticket": ""}).json()
        check("缺票据报 IM_LOGIN_TICKET_MISSING", empty_ticket.get("code") == "IM_LOGIN_TICKET_MISSING", str(empty_ticket)[:200])
        unknown_ticket = client.post("/api/im/wechat/login/poll", json={"ticket": "no-such-ticket"}).json()
        check("未知票据报 IM_LOGIN_EXPIRED", unknown_ticket.get("code") == "IM_LOGIN_EXPIRED", str(unknown_ticket)[:200])
        not_qr = client.post("/api/im/slack/login/start").json()
        check("非扫码通道拒绝扫码登录", not_qr.get("code") == "IM_LOGIN_UNSUPPORTED", str(not_qr)[:200])

        started = client.post("/api/im/wechat/login/start").json()
        check("login/start 返回票据与二维码内容", started.get("ok") is True and started.get("ticket") and started.get("qrContent"), str(started)[:200])
        first_qr = fake.qr_requests[0]
        check(
            "二维码请求走官方形状：bot_type=3 + body 只有 local_token_list",
            first_qr["bot_type"] == "3" and first_qr["body"] == {"local_token_list": []},
            str(first_qr)[:220],
        )
        check(
            "二维码请求带齐四个固定头",
            first_qr["headers_ok"]
            and first_qr["headers"].get("iLink-App-ClientVersion") == "132106"
            and first_qr["headers"].get("iLink-App-Id") == "bot"
            and first_qr["headers"].get("AuthorizationType") == "ilink_bot_token",
            str(first_qr["headers"])[:260],
        )
        check(
            "二维码内容是可编码成二维码的字符串，不是令牌",
            isinstance(started.get("qrContent"), str)
            and DEFAULT_BOT_TOKEN not in started["qrContent"],
            str(started.get("qrContent"))[:120],
        )

        runtime = im_channels.runtime_of("wechat")
        check("启动时已注册真实任务处理方", runtime.handler is engine_server._im_run_task)

        async def stub_handler(text: str, session: dict) -> str:
            return f"已收到：{text}"

        runtime.set_handler(stub_handler)

        # 引擎一确认就会开始长轮询，先把这一轮的待收消息排好
        fake.pending_msgs = [
            user_message("帮我把周报写成三条", msgid="wx-msg-1", context_token="ctx-a"),
            user_message("帮我把周报写成三条", msgid="wx-msg-1", context_token="ctx-a"),
            user_message("机器人自己的回声", msgid="wx-msg-2", message_type=2),
            user_message("", msgid="wx-msg-3", item_type=2),
            user_message("把这份清单按人分一下", msgid="wx-msg-4", from_user=SECOND_USER_ID, context_token="ctx-b"),
        ]

        polls = []
        confirmed = None
        for _ in range(6):
            polls.append(client.post("/api/im/wechat/login/poll", json={"ticket": started["ticket"]}).json())
            if polls[-1].get("status") == "confirmed":
                confirmed = polls[-1]
                break
            time.sleep(0.05)
        check(
            "状态按 wait → scaned → confirmed 推进",
            [p.get("status") for p in polls[:3]] == ["wait", "scaned", "confirmed"],
            str(polls)[:260],
        )
        check("confirmed 时引擎报告已绑定", confirmed is not None and confirmed.get("configured") is True, str(confirmed)[:200])
        check(
            "确认响应里没有 bot_token（凭据由引擎直接落盘）",
            DEFAULT_BOT_TOKEN not in json.dumps(confirmed or {}, ensure_ascii=False),
            str(confirmed)[:200],
        )
        cfg = _TMP / "home" / "im_channels.json"
        stored = json.loads(cfg.read_text(encoding="utf-8"))["wechat"] if cfg.exists() else {}
        check(
            "凭据落盘且字段来自确认帧",
            stored.get("botToken") == DEFAULT_BOT_TOKEN and stored.get("ilinkBotId") == DEFAULT_BOT_ID and stored.get("baseUrl") == fake.base_url,
            str(sorted(stored))[:200],
        )
        view = _channel_of(client, "wechat")
        check(
            "视图只给掩码 botId 与协议事实",
            "…" in view["account"].get("botId", "")
            and view["account"].get("botId") != DEFAULT_BOT_ID
            and view["account"].get("directMessageOnly") is True
            and view["account"].get("protocol") == "iLink 官方机器人",
            str(view["account"])[:220],
        )
        check(
            "接口视图里没有 bot_token",
            DEFAULT_BOT_TOKEN not in json.dumps(view, ensure_ascii=False),
            str(view)[:220],
        )

        print("\n== B. 长轮询全链路（getupdates → 任务 → sendmessage） ==")
        state = wait_for(lambda: "connected" if _channel_of(client, "wechat")["state"] == "connected" else None)
        check("长轮询进入 connected", state == "connected", str(state))
        sends = wait_for(lambda: fake.sends if len(fake.sends) >= 2 else None)
        texts = sorted(
            ((s.get("msg") or {}).get("item_list") or [{}])[0].get("text_item", {}).get("text") or ""
            for s in sends or []
        )
        check(
            "两条文本各起一个任务，重复 msgid/机器人回声/图片都不起",
            texts == sorted(["已收到：帮我把周报写成三条", "已收到：把这份清单按人分一下"]),
            str(texts)[:260],
        )
        check(
            "回贴形状是官方 sendmessage：机器人 + 已完成 + 纯文本项",
            all(
                (s.get("msg") or {}).get("message_type") == 2
                and (s.get("msg") or {}).get("message_state") == 2
                and (s.get("msg") or {}).get("item_list") == [
                    {"type": 1, "text_item": {"text": ((s.get("msg") or {}).get("item_list") or [{}])[0]["text_item"]["text"]}}
                ]
                and ((s.get("msg") or {}).get("client_id") or "").startswith("tongtian-")
                for s in sends or []
            ),
            str(sends)[:300],
        )
        by_user = {}
        for s in sends or []:
            msg = s.get("msg") or {}
            by_user[msg.get("to_user_id")] = msg.get("context_token")
        check(
            "回贴目标取 from_user_id，context_token 按各自会话带",
            by_user == {DEFAULT_USER_ID: "ctx-a", SECOND_USER_ID: "ctx-b"},
            str(by_user)[:200],
        )
        check(
            "回贴带 Bearer 登录态，其余请求头仍是官方四个",
            fake.send_headers.get("Authorization") == f"Bearer {DEFAULT_BOT_TOKEN}"
            and fake.send_headers.get("iLink-App-Id") == "bot"
            and fake.send_headers.get("iLink-App-ClientVersion") == "132106",
            str(fake.send_headers)[:260],
        )
        check(
            "收消息请求带游标与 base_info，长轮询是 POST",
            fake.getupdates_calls[0].get("base_info", {}).get("channel_version") == "2.4.10"
            and "get_updates_buf" in fake.getupdates_calls[0],
            str(fake.getupdates_calls[0])[:220],
        )
        check(
            "getupdates 带 Bearer 登录态且 UIN 是 base64 十进制串",
            fake.getupdates_headers.get("Authorization") == f"Bearer {DEFAULT_BOT_TOKEN}"
            and base64.b64decode(fake.getupdates_headers.get("X-WECHAT-UIN", "")).decode().isdigit(),
            str(fake.getupdates_headers)[:240],
        )

        sessions = wait_for(
            lambda: [s for s in _sessions_of(client) if s["source"] == "wechat"]
            if len([s for s in _sessions_of(client) if s["source"] == "wechat"]) == 2
            else None
        ) or []
        check("微信会话落入频道台账（按用户各一条）", len(sessions) == 2, str(_sessions_of(client))[:260])
        first = next((s for s in sessions if s["id"] == f"im-wechat-{DEFAULT_USER_ID}"), None)
        check("会话号取 from_user_id", first is not None, str(sessions)[:220])
        if first:
            check("标题取自首条消息首行", first["title"].startswith("帮我把周报写成三条"), first["title"])
            check("台账一进一出成对", [m["direction"] for m in first["messages"]] == ["in", "out"], str(first["messages"])[:200])
        cursor_file = _TMP / "home" / "im_wechat_sync.json"
        check(
            "游标按 bot_id 落盘（丢游标会重放历史消息）",
            cursor_file.exists() and DEFAULT_BOT_ID in cursor_file.read_text(encoding="utf-8"),
            cursor_file.read_text(encoding="utf-8")[:200] if cursor_file.exists() else "missing",
        )
        print("\n== C. 微信方言分支 ==")
        ch = runtime.channel
        check("机器人回声（message_type=2）不当事件", ch.parse_event(user_message("x", msgid="e1", message_type=2)).kind == "ignore")
        check("图片消息不当事件", ch.parse_event(user_message("x", msgid="e2", item_type=2)).kind == "ignore")
        check("空文本不当事件", ch.parse_event(user_message(" ", msgid="e3")).kind == "ignore")
        check("重复 msgid 不再起任务", ch.parse_event(user_message("重复", msgid="wx-msg-1")).kind == "ignore")
        check("context_token 按用户记住", ch._context_tokens.get(SECOND_USER_ID) == "ctx-b", str(ch._context_tokens)[:160])

        async def _dialect() -> dict:
            out = {}
            async with aiohttp.ClientSession() as session:
                probe = wechat_channel.WeChatILinkChannel()
                probe.runtime = _HttpRuntime(session)
                start = await probe.login_start()
                ticket = start["ticket"]

                fake.qr_statuses = ["need_verifycode"]
                out["need"] = await probe.login_poll(ticket, "")

                probe._logins[ticket]["atMs"] = time.time() - wechat_channel.QR_TTL_SECONDS - 1
                out["expired"] = await probe.login_poll(ticket, "")

                start2 = await probe.login_start()
                fake.qr_statuses = ["verify_code_blocked"]
                out["blocked"] = await probe.login_poll(start2["ticket"], "0000")

                start3 = await probe.login_start()
                fake.qr_statuses = ["scaned_but_redirect"]
                out["redirect"] = await probe.login_poll(start3["ticket"], "")
                out["redirect_base"] = probe._logins.get(start3["ticket"], {}).get("base", "")

                # 回贴：没有 context_token 时必须如实失败
                try:
                    await probe.send(_creds(), "nobody", "hi")
                    out["no_ctx"] = ""
                except im_channels.ImError as exc:
                    out["no_ctx"] = f"{exc.code}: {exc.detail}"

                # 超长回贴按 4000 字切分（假网关收 3 条）
                before = len(fake.sends)
                probe._context_tokens["long-user"] = "ctx-long"
                await probe.send(_creds(), "long-user", "x" * 9000)
                out["chunks"] = [len(((s.get("msg") or {}).get("item_list") or [{}])[0]["text_item"]["text"]) for s in fake.sends[before:]]
            return out

        result = asyncio.run(_dialect())
        check("need_verifycode 要求输入配对码", result["need"].get("status") == "need_verifycode" and result["need"].get("needVerifyCode") is True, str(result["need"])[:200])
        check("配对码提示是可照做的文案", "数字" in result["need"].get("message", ""), str(result["need"])[:200])
        check("二维码超时后报 expired", result["expired"].get("status") == "expired", str(result["expired"])[:200])
        check("输错次数过多报 verify_code_blocked", result["blocked"].get("status") == "verify_code_blocked" and "重新生成" in result["blocked"].get("message", ""), str(result["blocked"])[:220])
        check(
            "scaned_but_redirect 切到服务端给的接入点",
            result["redirect"].get("status") == "scaned_but_redirect" and result["redirect_base"] == "https://redirect.invalid",
            f'{result["redirect"]} base={result["redirect_base"]}',
        )
        check(
            "没有 context_token 时如实报 IM_REPLY_FAILED（官方 24 小时窗口）",
            result["no_ctx"].startswith("IM_REPLY_FAILED:") and "context_token" in result["no_ctx"],
            result["no_ctx"][:220],
        )
        check("超长回贴按 4000 字切成 3 条", result["chunks"] == [4000, 4000, 1000], str(result["chunks"]))

        check("_api_error 带 ret/errcode/errmsg 三个事实", wechat_channel._api_error({"ret": -14, "errcode": -14, "errmsg": "expired"}) == "ret=-14 errcode=-14 errmsg=expired", wechat_channel._api_error({"ret": -14, "errcode": -14, "errmsg": "expired"}))
        uin = base64.b64decode(wechat_channel._wechat_uin()).decode()
        check("X-WECHAT-UIN 是 base64(随机 uint32 十进制串)", uin.isdigit() and 0 <= int(uin) < 2**32, uin)
        check("客户端版本按官方 uint32(0xMMNNPP) 算成 132106", wechat_channel.ILINK_APP_CLIENT_VERSION == 132106, str(wechat_channel.ILINK_APP_CLIENT_VERSION))
        check("长轮询没有传输层心跳", wechat_channel.WeChatILinkChannel.protocol_heartbeat is None)

        # -14：登录态过期，必须停轮询且不无限重试
        fake.stale_token = True

        def _expired() -> dict | None:
            v = _channel_of(client, "wechat")
            return v if (v.get("lastError") or {}).get("code") == "IM_AUTH_FAILED" else None

        expired = wait_for(_expired)
        check("errcode=-14 映射成 IM_AUTH_FAILED", expired is not None and expired["state"] == "failed", str(_channel_of(client, "wechat"))[:260])
        check(
            "过期文案让用户重新扫码",
            "重新扫码" in (expired or {}).get("lastError", {}).get("detail", ""),
            str((expired or {}).get("lastError"))[:200],
        )
        check("过期后连接循环自己收尾", runtime._task is None or runtime._task.done())
        settled = len(fake.getupdates_calls)
        time.sleep(1.5)
        check(
            "过期后不再继续轮询（再问也是同样的 -14）",
            len(fake.getupdates_calls) == settled,
            f"settled={settled} now={len(fake.getupdates_calls)}",
        )
        fake.stale_token = False

        stopped = client.post("/api/im/wechat/stop").json()
        check("停止后 state=stopped", stopped.get("state") == "stopped", str(stopped)[:200])
        forgotten = client.post("/api/im/wechat/forget").json()
        check("解绑成功", forgotten.get("ok") is True, str(forgotten)[:200])
        check("解绑只删微信凭据", "wechat" not in json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else True)
        check("解绑保留历史会话台账（数据保护）", len([s for s in _sessions_of(client) if s["source"] == "wechat"]) == 2)


def main() -> None:
    fake = FakeWeChat()
    fake.start()
    try:
        phase_ab(fake)
    finally:
        fake.stop()
        asyncio.run(im_channels.shutdown())
    print("-" * 52)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine im wechat selftest: ALL PASS")


if __name__ == "__main__":
    main()
