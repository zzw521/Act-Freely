#!/usr/bin/env python3
"""
IM 频道自测 —— 企业微信智能机器人长连接（官方 aibot SDK 的协议 + 框架共用连接循环）

分三段，A/B 全部打真实网络（对端是本机起的假企业微信，不是 Mock 返回值）：

A. HTTP 契约面：企业微信在 /api/im/channels 里从「尚未接入」变成 supported=True 且带两项凭据
   字段（BotID + 长连接 Secret，不是回调地址模式的 Token）；未接入的微信仍如实
   IM_UNSUPPORTED；凭据缺失/带空格/被拒各报各的码，格式没过时一条帧都不发出去。
B. 长连接全链路：假网关只做官方那几件事（认订阅帧、回 errcode、推回调、回贴要在同一条
   socket 上等回执、新连接顶掉旧连接），验证「连固定地址 → 带内订阅 → 收消息 → 透传
   req_id 回贴 → 等 errcode → 应用层心跳 → 被顶掉后不再重连」，并检查重复 msgid、
   compat_info、图片、enter_chat、模板卡片回调都不会触发任务；单聊没有 chatid 时按
   from.userid 建会话。
C. 企业微信方言分支：缺 errcode 的响应按失败算、订阅被拒在认证前后分别映射
   IM_AUTH_FAILED 与不可重试的 IM_CONNECTION_KICKED、回贴只在 req_id 还新鲜时走
   respond_msg 否则改走主动推送、等不到回执如实报 IM_REPLY_FAILED、连接关闭时撤掉
   待回执并作废旧 req_id、心跳连续没等到回执就主动断开、回贴台账有界、SDK 缺失时的
   「尚未接入」话术。

数据隔离：TT_DATA_HOME 与 WORK_DIR 都指向临时目录，绝不写 ~/.tongtianwork 的存量台账。
用法: python tests/integration/engine_im_wecom_selftest.py
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TMP = Path(tempfile.mkdtemp(prefix="tt-im-wecom-"))
os.environ["TT_DATA_HOME"] = str(_TMP / "home")
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import aibot  # noqa: F401  依赖探针：企业微信通道的协议就来自这个官方 SDK
    from fastapi.testclient import TestClient
    from fake_wecom import (
        DEFAULT_BOT_ID,
        DEFAULT_SECRET,
        GROUP_CHAT_ID,
        SECOND_USERID,
        SENDER_USERID,
        FakeWeCom,
        event_callback,
        msg_callback,
    )
    import server as engine_server
    import im_channels
    import wecom_channel
except Exception as exc:  # 依赖缺失时优雅跳过（与其余引擎自测一致）
    print(f"SKIP: engine im wecom selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def wait_for(predicate, timeout=8.0, interval=0.05):
    """等条件成立（带内往返是异步的，不能靠 sleep 猜时间）"""
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


def _fake_post(fake: FakeWeCom, path: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{fake.port}{path}",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _body_of(frame: dict) -> dict:
    return frame.get("body") if isinstance(frame.get("body"), dict) else {}


# ── A/B 段：HTTP 契约面 + 假企业微信全链路 ─────────────────────────


def phase_ab(client_fake: FakeWeCom) -> None:
    print("\n== A. /api/im 契约面（企业微信已接入） ==")
    engine_server.WORK_DIR = _TMP / "work"
    engine_server.WORK_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["TT_WECOM_WS_URL"] = client_fake.ws_url
    # 官方建议 30 秒；自测里压到 0.5 秒才能在几秒内观察到应用层心跳真的发出去
    os.environ["TT_WECOM_HEARTBEAT_SECONDS"] = "0.5"

    with TestClient(engine_server.app) as client:
        body = client.get("/api/im/channels").json()
        check("通道清单返回 ok", body.get("ok") is True, str(body)[:200])
        channels = {c["kind"]: c for c in body["channels"]}
        supported = [c["kind"] for c in body["channels"] if c["supported"]]
        check(
            "清单仍是五个通道",
            {"slack", "dingtalk", "feishu", "wecom", "wechat"} == set(channels),
            str(sorted(channels)),
        )
        check("已接入的通道全部排在未接入之前", list(channels)[: len(supported)] == supported, str(list(channels)))
        wc = channels["wecom"]
        check("企业微信标记为已接入", wc["supported"] is True, str(wc)[:200])
        check(
            "凭据字段取自订阅帧而不是回调地址模式的 Token",
            [f["key"] for f in wc["fields"]] == ["botId", "secret"]
            and all(f["secret"] is True for f in wc["fields"])
            and "长连接" in wc["setupNote"]
            and "不回显" in wc["fields"][1]["note"],
            str(wc["fields"])[:260],
        )
        check(
            "「一个 BotID 一条连接」这条硬约束写在指引里",
            "只允许一条有效长连接" in wc["setupNote"],
            wc["setupNote"][:200],
        )
        # 飞书已接入（第三个官方 SDK 通道），清单里只剩微信还是「尚未接入」
        wx = channels["wechat"]
        check(
            "微信仍未接入且有可读原因",
            wx["supported"] is False and wx["unsupportedReason"] and not wx["fields"],
            str(wx)[:160],
        )
        check(
            "飞书已接入且首次连接要加载 SDK 这件事写在指引里",
            channels["feishu"]["supported"] is True and "加载官方 SDK" in channels["feishu"]["setupNote"],
            str(channels["feishu"])[:200],
        )

        neg = client.post("/api/im/wechat/config", json={"credentials": {"botId": "x"}}).json()
        check("未接入通道拒绝写凭据（IM_UNSUPPORTED）", neg.get("code") == "IM_UNSUPPORTED", str(neg)[:160])
        start = client.post("/api/im/wecom/start").json()
        check("未配置就启动报 IM_NOT_CONFIGURED", start.get("code") == "IM_NOT_CONFIGURED", str(start)[:160])
        unknown = client.post("/api/im/qq/start").json()
        check("未知通道报 IM_CHANNEL_UNKNOWN", unknown.get("code") == "IM_CHANNEL_UNKNOWN", str(unknown)[:160])

        missing = client.post("/api/im/wecom/config", json={"credentials": {"botId": DEFAULT_BOT_ID}}).json()
        check("缺长连接 Secret 被拒绝", missing.get("code") == "IM_CREDENTIAL_MISSING", str(missing)[:160])
        spaced = client.post(
            "/api/im/wecom/config", json={"credentials": {"botId": "tt fake bot", "secret": DEFAULT_SECRET}}
        ).json()
        check("BotID 带空格报 IM_CREDENTIAL_INVALID", spaced.get("code") == "IM_CREDENTIAL_INVALID", str(spaced)[:160])
        check(
            "凭据格式没过时一条帧都不发（假网关没被连过）",
            client_fake.sockets == 0 and client_fake.subs == [],
            f"sockets={client_fake.sockets} subs={len(client_fake.subs)}",
        )

        wrong = client.post(
            "/api/im/wecom/config",
            json={"credentials": {"botId": DEFAULT_BOT_ID, "secret": DEFAULT_SECRET.replace("0", "o", 1)}},
        ).json()
        check(
            "订阅被拒映射成 IM_AUTH_FAILED",
            wrong.get("code") == "IM_AUTH_FAILED",
            str(wrong)[:220],
        )
        check("鉴权失败不留半套配置", _channel_of(client, "wecom")["configured"] is False)
        check(
            "鉴权失败不把 Secret 写进错误详情",
            DEFAULT_SECRET not in json.dumps(wrong, ensure_ascii=False),
        )
        check(
            "订阅帧形状取自官方：body 只有 bot_id 与 secret",
            [set(_body_of(s)) for s in client_fake.subs] == [{"bot_id", "secret"}],
            str([sorted(_body_of(s)) for s in client_fake.subs])[:200],
        )

        print("\n== B. 长连接全链路（对端为本机假企业微信） ==")
        runtime = im_channels.runtime_of("wecom")
        check("启动时已注册真实任务处理方", runtime.handler is engine_server._im_run_task)

        async def stub_handler(text: str, session: dict) -> str:
            # 真实处理方（IM→Agent）由 slack 自测的 C 段覆盖；这里只验框架接线
            return f"已收到：{text}"

        runtime.set_handler(stub_handler)
        # 第 1 条连接是上面那次鉴权失败用的探测连接；正确的凭据会再探测一次，
        # 然后由框架建真正干活的那条 —— 剧本要排在后者上
        client_fake.script_on = client_fake.sockets + 2
        ok = client.post(
            "/api/im/wecom/config",
            json={"credentials": {"botId": DEFAULT_BOT_ID, "secret": DEFAULT_SECRET}},
        ).json()
        account = ok.get("account") or {}
        check("绑定成功返回真实账号事实", ok.get("ok") is True and account.get("wsUrl") == client_fake.ws_url, str(ok)[:200])
        check(
            "账号信息只给掩码 BotID 与协议事实",
            account.get("botId") != DEFAULT_BOT_ID
            and "…" in account.get("botId", "")
            and account.get("oneConnectionPerBot") is True
            and account.get("heartbeatSeconds") == 0.5,
            str(account)[:200],
        )
        check(
            "带内订阅真的打到假网关（不是打桩返回值）",
            len(client_fake.subs) >= 2 and client_fake.sockets >= 2,
            f"sockets={client_fake.sockets} subs={len(client_fake.subs)}",
        )

        state = wait_for(lambda: "connected" if _channel_of(client, "wecom")["state"] == "connected" else None)
        check("长连接进入 connected", state == "connected", str(state))
        both = wait_for(lambda: client_fake.replies if len(client_fake.replies) >= 2 else None)
        check(
            "群聊与单聊各起一个任务，重复 msgid/compat_info/图片/事件都不起",
            both is not None
            and sorted(((_body_of(r).get("stream") or {}).get("content") or "") for r in both)
            == sorted(["已收到：帮我把季度总结整理成一页", "已收到：把这份清单按人分一下"]),
            f"replies={str(both)[:260]} sends={str(client_fake.sends)[:160]}",
        )
        group_reply = next((r for r in both or [] if (_body_of(r).get("stream") or {}).get("content") == "已收到：帮我把季度总结整理成一页"), None)
        check(
            "回贴用 aibot_respond_msg + stream，一次发完就 finish",
            group_reply is not None
            and group_reply.get("cmd") == "aibot_respond_msg"
            and _body_of(group_reply).get("msgtype") == "stream"
            and _body_of(group_reply).get("stream", {}).get("finish") is True
            and _body_of(group_reply).get("stream", {}).get("id"),
            str(group_reply)[:260],
        )
        check(
            "req_id 原样透传回调帧的（官方口径，不是自己新生成）",
            (group_reply.get("headers") or {}).get("req_id") == "cb-group-1",
            str(group_reply.get("headers"))[:160],
        )
        check(
            "req_id 还新鲜时不走主动推送",
            client_fake.sends == [],
            str(client_fake.sends)[:200],
        )
        check(
            "引擎没有发明协议外的帧",
            client_fake.unexpected == [],
            str(client_fake.unexpected)[:260],
        )

        pinged = wait_for(lambda: client_fake.pings if len(client_fake.pings) >= 2 else None)
        check("应用层 ping 帧按心跳间隔持续发出去", bool(pinged), str(client_fake.pings)[:200])
        check(
            "ping 帧用官方 SDK 的 req_id 前缀（ping_<ms>_<rand>）",
            all(p.startswith("ping_") for p in client_fake.pings),
            str(client_fake.pings)[:200],
        )
        check(
            "心跳与回贴都在同一条 socket 上等 errcode 回执",
            all(str(a.get("errcode")) == "0" for a in client_fake.acks_sent) and len(client_fake.acks_sent) >= 3,
            str(client_fake.acks_sent)[:220],
        )
        ch = runtime.channel
        check("待回执字典不攒垃圾", ch._pending_acks == {}, str(ch._pending_acks)[:200])

        view = _channel_of(client, "wecom")
        check("连接建立后账号信息仍在视图里", view["account"].get("botId", "").startswith("tt-"), str(view["account"])[:160])
        check(
            "凭据绝不出现在接口视图",
            DEFAULT_SECRET not in json.dumps(view, ensure_ascii=False)
            and DEFAULT_BOT_ID not in json.dumps(view["account"], ensure_ascii=False),
            str(view["account"])[:200],
        )

        sessions = wait_for(lambda: [s for s in _sessions_of(client) if s["source"] == "wecom"] if len([s for s in _sessions_of(client) if s["source"] == "wecom"]) == 2 else None)
        wc_sessions = sessions or []
        check("企业微信会话落入频道台账", len(wc_sessions) == 2, str(_sessions_of(client))[:260])
        group = next((s for s in wc_sessions if s["id"] == f"im-wecom-{GROUP_CHAT_ID}"), None)
        single = next((s for s in wc_sessions if s["id"] == f"im-wecom-{SECOND_USERID}"), None)
        check("群聊按 chatid 建会话", group is not None and group["conversationId"] == GROUP_CHAT_ID, str(wc_sessions)[:220])
        check("单聊没有 chatid，会话号取 from.userid", single is not None, str(wc_sessions)[:220])
        if group:
            check("标题取自首条 IM 消息", group["title"].startswith("帮我把季度总结"), group["title"])
            check("台账记录一进一出两条", [m["direction"] for m in group["messages"]] == ["in", "out"], str(group["messages"])[:160])
            ledger_file = _TMP / "home" / "im_sessions.json"
            check("台账真实落盘", ledger_file.exists() and GROUP_CHAT_ID in ledger_file.read_text(encoding="utf-8"))

        cfg = _TMP / "home" / "im_channels.json"
        check(
            "凭据落盘且只含两项",
            cfg.exists() and set(json.loads(cfg.read_text(encoding="utf-8"))["wecom"]) == {"botId", "secret"},
        )

        sockets_before_kick = client_fake.sockets
        _fake_post(client_fake, "/kick", {})

        def _kicked() -> dict | None:
            v = _channel_of(client, "wecom")
            return v if (v.get("lastError") or {}).get("code") == "IM_CONNECTION_KICKED" else None

        kicked = wait_for(_kicked)
        check(
            "被新连接顶掉后如实报 IM_CONNECTION_KICKED",
            kicked is not None and kicked["state"] == "failed",
            str(_channel_of(client, "wecom"))[:260],
        )
        check("顶掉之后连接循环自己收尾，不再挂着", runtime._task is None or runtime._task.done())
        time.sleep(2.5)  # 退避的第一档是 1 秒：2.5 秒内没有新连接才说明确实没重试
        check(
            "顶掉之后不再退避重连（否则两个进程互相踢）",
            client_fake.sockets == sockets_before_kick,
            f"before={sockets_before_kick} now={client_fake.sockets}",
        )
        check(
            "顶掉后在等回执的帧与旧 req_id 一起作废",
            ch._pending_acks == {} and ch._authenticated is False,
            f"{str(ch._pending_acks)[:120]} auth={ch._authenticated}",
        )
        check(
            "错误文案说的是能照做的原因，不是「正在重试」",
            "同一时间只有一条有效长连接" in (kicked or {}).get("lastError", {}).get("detail", "")
            and "已停止自动重连" in (kicked or {}).get("lastError", {}).get("detail", ""),
            str((kicked or {}).get("lastError"))[:220],
        )

        stopped = client.post("/api/im/wecom/stop").json()
        check("停止后 state=stopped", stopped.get("state") == "stopped", str(stopped)[:160])
        forgotten = client.post("/api/im/wecom/forget").json()
        check("解绑成功", forgotten.get("ok") is True, str(forgotten)[:160])
        check("解绑只删企业微信凭据", "wecom" not in json.loads(cfg.read_text(encoding="utf-8")))
        check("解绑保留历史会话台账（数据保护）", len([s for s in _sessions_of(client) if s["source"] == "wecom"]) == 2)


# ── C 段：企业微信方言分支 ────────────────────────────────────────


class StubWs:
    """只用来喂带内帧的假 socket：send_str 顺手把对端该回的那一帧灌回 parse_event"""

    def __init__(self, channel, *, answer: bool = True, errcode: int = 0):
        self.channel = channel
        self.answer = answer
        self.errcode = errcode
        self.sent: list[dict] = []
        self.closed = False
        self.close_calls = 0

    async def send_str(self, raw: str) -> None:
        frame = json.loads(raw)
        self.sent.append(frame)
        if not self.answer:
            return
        req_id = str((frame.get("headers") or {}).get("req_id") or "")
        self.channel.parse_event({"headers": {"req_id": req_id}, "errcode": self.errcode, "errmsg": "ok" if not self.errcode else "rejected"})

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def _code_raised(fn) -> tuple[str, str, bool]:
    try:
        fn()
    except im_channels.ImError as exc:
        return exc.code, exc.detail, exc.retryable
    return "", "", True


def phase_c() -> None:
    print("\n== C. 企业微信方言分支 ==")
    ch = wecom_channel.WeComAibotChannel()

    text_frame = msg_callback("cb-x-1", text="写周报", msgid="msgid-x-1")
    ev = ch.parse_event(text_frame)
    check("群聊文本回调解析为 inbound", ev.kind == "inbound" and ev.text == "写周报" and ev.conversation_id == GROUP_CHAT_ID, str(ev)[:200])
    check("user_id 取 from.userid", ev.user_id == SENDER_USERID, str(ev)[:160])
    check("消息号取 msgid", ev.message_id == "msgid-x-1", str(ev)[:160])
    ev2 = ch.parse_event(msg_callback("cb-x-1", text="写周报", msgid="msgid-x-1"))
    check("重复 msgid 不再起任务（官方：msgid 用于事件排重）", ev2.kind == "ignore", str(ev2)[:160])
    ev3 = ch.parse_event(msg_callback("cb-x-2", msgtype="image", msgid="msgid-x-2"))
    check("图片回调不当事件（本通道不做 aeskey 下载解密）", ev3.kind == "ignore", str(ev3)[:160])
    ev4 = ch.parse_event(msg_callback("cb-x-3", msgtype="voice", msgid="msgid-x-3"))
    check("语音回调不当事件", ev4.kind == "ignore", str(ev4)[:160])
    ev5 = ch.parse_event(msg_callback("cb-x-4", text="转来的调用", msgid="msgid-x-4", compat={"source": "ai_assistant"}))
    check("compat_info 表示非用户消息，不当事件", ev5.kind == "ignore", str(ev5)[:160])
    ev6 = ch.parse_event(msg_callback("cb-x-5", text="单聊一句", msgid="msgid-x-5", chattype="single", userid=SECOND_USERID))
    check("单聊没有 chatid 时用 from.userid 当会话号", ev6.kind == "inbound" and ev6.conversation_id == SECOND_USERID, str(ev6)[:200])
    ev7 = ch.parse_event(msg_callback("cb-x-6", text="", msgid="msgid-x-6"))
    check("空文本不进任务", ev7.kind == "ignore", str(ev7)[:160])
    ev8 = ch.parse_event(event_callback("ev-x-1", "enter_chat", msgid="msgid-ev-1"))
    check("enter_chat 事件不当事件", ev8.kind == "ignore", str(ev8)[:160])
    ev9 = ch.parse_event(event_callback("ev-x-2", "template_card_event", msgid="msgid-ev-2", chattype="group"))
    check("模板卡片事件不当事件", ev9.kind == "ignore", str(ev9)[:160])
    ev10 = ch.parse_event({"cmd": "aibot_unknown", "headers": {"req_id": "u"}, "body": {}})
    check("未知 cmd 一律忽略而不是猜", ev10.kind == "ignore", str(ev10)[:160])

    # 被新连接顶掉：认证前后两种来源，都不能退避重试
    code, detail, retryable = _code_raised(lambda: ch.parse_event(event_callback("ev-x-3", "disconnected_event", msgid="msgid-ev-3")))
    check("disconnected_event 映射 IM_CONNECTION_KICKED 且不可重试", code == "IM_CONNECTION_KICKED" and retryable is False, f"{code} {detail}"[:200])

    ch_sub = wecom_channel.WeComAibotChannel()
    code, _, retryable = _code_raised(lambda: ch_sub.parse_event({"headers": {"req_id": "aibot_subscribe_x"}, "errcode": 850004}))
    check("认证前订阅被拒映射 IM_AUTH_FAILED", code == "IM_AUTH_FAILED" and retryable is True, f"{code}"[:160])
    ch_sub._authenticated = True
    code, detail, retryable = _code_raised(lambda: ch_sub.parse_event({"headers": {"req_id": "aibot_subscribe_y"}, "errcode": 850004}))
    check("认证后订阅被拒按顶掉处理，不再重试", code == "IM_CONNECTION_KICKED" and retryable is False, f"{code} {detail}"[:200])
    check("缺 errcode 的响应帧按失败算（不给假成功）", wecom_channel._errcode_of({"headers": {}}) is None)
    check("errcode 不是数字也按失败算", wecom_channel._errcode_of({"errcode": "abc"}) is None)
    check("拒绝原因带 errcode 与 errmsg", "errcode=850004" in wecom_channel._reject_reason({"errcode": 850004, "errmsg": "x"}), wecom_channel._reject_reason({"errcode": 850004, "errmsg": "x"}))

    # 心跳回执：errcode 0 清零，非 0 累加（累加到上限由 _keepalive 主动断开）
    ch_hb = wecom_channel.WeComAibotChannel()
    ch_hb._missed_pong = 1
    ch_hb.parse_event({"headers": {"req_id": "ping_a"}, "errcode": 0})
    check("ping 回执把未响应计数清零", ch_hb._missed_pong == 0, str(ch_hb._missed_pong))
    ch_hb.parse_event({"headers": {"req_id": "ping_b"}, "errcode": 40014})
    check("ping 被拒把未响应计数累加", ch_hb._missed_pong == 1, str(ch_hb._missed_pong))

    async def _keepalive_closes_when_dead() -> bool:
        hb = wecom_channel.WeComAibotChannel()
        ws = StubWs(hb)
        hb._ws = ws
        hb._missed_pong = wecom_channel.MAX_MISSED_PONG
        task = asyncio.create_task(hb._keepalive())
        await asyncio.sleep(0.7)
        task.cancel()
        return ws.closed and not ws.sent

    check("连续没等到心跳回执就主动断开（SDK 的 _max_missed_pong）", asyncio.run(_keepalive_closes_when_dead()))

    # 回贴：req_id 新鲜走 respond_msg，过期或丢了走 send_msg 主动推送
    ch_send = wecom_channel.WeComAibotChannel()
    ws_send = StubWs(ch_send)
    ch_send._ws = ws_send
    ch_send.parse_event(msg_callback("cb-send-1", text="一句话", msgid="msgid-send-1"))
    asyncio.run(ch_send.send({}, GROUP_CHAT_ID, "回一句"))
    sent = ws_send.sent[0]
    check(
        "新鲜 req_id 用 aibot_respond_msg 透传回去",
        sent.get("cmd") == "aibot_respond_msg" and (sent.get("headers") or {}).get("req_id") == "cb-send-1",
        str(sent)[:200],
    )
    ch_send.parse_event(msg_callback("cb-send-2", text="又一句", msgid="msgid-send-2", chattype="single", userid=SECOND_USERID))
    asyncio.run(ch_send.send({}, SECOND_USERID, "再回一句"))
    push_shape = ws_send.sent[-1]
    check("单聊会话的回贴 req_id 也按会话各自记", (push_shape.get("headers") or {}).get("req_id") == "cb-send-2", str(push_shape)[:200])

    ch_old = wecom_channel.WeComAibotChannel()
    ws_old = StubWs(ch_old)
    ch_old._ws = ws_old
    ch_old.parse_event(msg_callback("cb-old-1", text="隔天再说", msgid="msgid-old-1"))
    ch_old._reply_targets[GROUP_CHAT_ID]["atMs"] = int((time.time() - wecom_channel.REPLY_WINDOW_SECONDS) * 1000)
    asyncio.run(ch_old.send({}, GROUP_CHAT_ID, "隔天回一句"))
    late = ws_old.sent[0]
    check(
        "超过 24 小时回复窗口改走 aibot_send_msg 主动推送",
        late.get("cmd") == "aibot_send_msg"
        and _body_of(late).get("chatid") == GROUP_CHAT_ID
        and _body_of(late).get("chat_type") == 2
        and _body_of(late).get("msgtype") == "markdown"
        and _body_of(late).get("markdown", {}).get("content") == "隔天回一句",
        str(late)[:260],
    )

    ch_new = wecom_channel.WeComAibotChannel()
    ws_new = StubWs(ch_new)
    ch_new._ws = ws_new
    asyncio.run(ch_new.send({}, "wrUnknown", "没回调过的会话"))
    check("没有回调过的会话也只能走主动推送", ws_new.sent[0].get("cmd") == "aibot_send_msg" and ws_new.sent[0]["body"]["chat_type"] == 1, str(ws_new.sent)[:200])

    ch_dead = wecom_channel.WeComAibotChannel()
    code, _, _ = None, None, None
    try:
        asyncio.run(ch_dead.send({}, GROUP_CHAT_ID, "hi"))
        check("连接不在时如实失败", False, "send 居然返回成功")
    except im_channels.ImError as exc:
        check("连接不在时如实失败（不假装送达）", exc.code == "IM_CONNECTION_FAILED" and "回贴发不出去" in exc.detail, str(exc)[:200])

    ch_timeout = wecom_channel.WeComAibotChannel()
    ch_timeout._ws = StubWs(ch_timeout, answer=False)
    ch_timeout.parse_event(msg_callback("cb-to-1", text="等不到回执", msgid="msgid-to-1"))
    original_timeout = wecom_channel.REPLY_ACK_TIMEOUT_SECONDS
    try:
        wecom_channel.REPLY_ACK_TIMEOUT_SECONDS = 0.2
        asyncio.run(ch_timeout.send({}, GROUP_CHAT_ID, "hi"))
        check("等不到 errcode 回执不假装发出去了", False, "send 居然返回成功")
    except im_channels.ImError as exc:
        check(
            "等不到 errcode 回执不假装发出去了",
            exc.code == "IM_REPLY_FAILED" and "没有" in exc.detail and "cb-to-1" in exc.detail,
            str(exc)[:220],
        )
    finally:
        wecom_channel.REPLY_ACK_TIMEOUT_SECONDS = original_timeout
    check("超时后待回执字典不留下悬空 future", ch_timeout._pending_acks == {}, str(ch_timeout._pending_acks)[:160])

    ch_reject = wecom_channel.WeComAibotChannel()
    ch_reject._ws = StubWs(ch_reject, errcode=60020)
    ch_reject.parse_event(msg_callback("cb-rj-1", text="被拒的回贴", msgid="msgid-rj-1"))
    try:
        asyncio.run(ch_reject.send({}, GROUP_CHAT_ID, "hi"))
        check("errcode 非 0 的回执按失败算", False, "send 居然返回成功")
    except im_channels.ImError as exc:
        check("errcode 非 0 的回执按失败算", exc.code == "IM_REPLY_FAILED" and "60020" in exc.detail, str(exc)[:220])

    # 连接关闭：待回执必须撤掉，旧 req_id 作废但会话类型留着
    ch_close = wecom_channel.WeComAibotChannel()
    ws_close = StubWs(ch_close, answer=False)
    ch_close._ws = ws_close
    ch_close.parse_event(msg_callback("cb-cl-1", text="还没回", msgid="msgid-cl-1"))

    async def _pending_then_close() -> tuple[str, dict]:
        task = asyncio.create_task(ch_close.send({}, GROUP_CHAT_ID, "hi"))
        await asyncio.sleep(0.1)
        await ch_close.on_close()
        try:
            await task
        except im_channels.ImError as exc:
            return exc.code, dict(ch_close._reply_targets)
        return "", dict(ch_close._reply_targets)

    code_after_close, targets_after_close = asyncio.run(_pending_then_close())
    check("连接关闭时把在等的回贴如实报错", code_after_close == "IM_REPLY_FAILED", code_after_close)
    check(
        "关闭后旧 req_id 作废，但会话是群聊还是单聊留着",
        targets_after_close.get(GROUP_CHAT_ID, {}).get("req_id") == ""
        and targets_after_close.get(GROUP_CHAT_ID, {}).get("chat_type") == 2,
        str(targets_after_close)[:200],
    )

    ch_bound = wecom_channel.WeComAibotChannel()
    for i in range(wecom_channel.MAX_REPLY_TARGETS + 20):
        ch_bound.parse_event(msg_callback(f"cb-b{i}", text="hi", msgid=f"b{i}", chatid=f"wr{i:04d}"))
    check(
        "回贴台账有界（长连接跑久不无限攒）",
        len(ch_bound._reply_targets) == wecom_channel.MAX_REPLY_TARGETS,
        str(len(ch_bound._reply_targets)),
    )
    ch_dedup = wecom_channel.WeComAibotChannel()
    for i in range(wecom_channel.MAX_SEEN_MSGIDS + 30):
        ch_dedup.parse_event(msg_callback(f"cb-s{i}", text="hi", msgid=f"s{i}"))
    check(
        "消息号台账同样有界（排重表不能无限长大）",
        len(ch_dedup._seen_msgids) == wecom_channel.MAX_SEEN_MSGIDS,
        str(len(ch_dedup._seen_msgids)),
    )
    check(
        "最近的消息号还在，老的被挤掉",
        f"s{wecom_channel.MAX_SEEN_MSGIDS + 29}" in ch_dedup._seen_msgids
        and "s0" not in ch_dedup._seen_msgids,
        str(ch_dedup._seen_msgids[:3])[:120],
    )

    # 环境变量口子：地址与心跳间隔（假网关靠这两个注入）
    saved = (os.environ.get("TT_WECOM_WS_URL"), os.environ.get("TT_WECOM_HEARTBEAT_SECONDS"))
    try:
        os.environ["TT_WECOM_WS_URL"] = "ws://127.0.0.1:19999/ws"
        check("连接地址可被环境变量顶掉（官方 SDK 的 ws_url 注入点）", wecom_channel.ws_url_base() == "ws://127.0.0.1:19999/ws", wecom_channel.ws_url_base())
        os.environ["TT_WECOM_HEARTBEAT_SECONDS"] = "abc"
        check("心跳间隔写坏了退回官方建议的 30 秒", wecom_channel.heartbeat_seconds() == 30.0, str(wecom_channel.heartbeat_seconds()))
        os.environ["TT_WECOM_HEARTBEAT_SECONDS"] = "0"
        check("心跳间隔设成 0 也退回 30 秒", wecom_channel.heartbeat_seconds() == 30.0, str(wecom_channel.heartbeat_seconds()))
        os.environ.pop("TT_WECOM_WS_URL")
        check("没注入时就是官方的 openws 地址", wecom_channel.ws_url_base() == "wss://openws.work.weixin.qq.com", wecom_channel.ws_url_base())
    finally:
        for key, value in zip(("TT_WECOM_WS_URL", "TT_WECOM_HEARTBEAT_SECONDS"), saved):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    ticket = asyncio.run(wecom_channel.WeComAibotChannel().open_connection({"botId": DEFAULT_BOT_ID, "secret": DEFAULT_SECRET}))
    check(
        "B 族方言不换 ticket：地址固定，账号信息给协议事实",
        ticket.ws_url == wecom_channel.ws_url_base() and ticket.info.get("oneConnectionPerBot") is True,
        str(ticket)[:200],
    )
    code, _, _ = _code_raised(lambda: asyncio.run(wecom_channel.WeComAibotChannel().open_connection({"botId": ""})))
    check("换地址前也先验凭据格式", code == "IM_CREDENTIAL_MISSING", code)

    check("企业微信没有逐帧回执：ack 留给基类做空操作", asyncio.run(_ack_is_noop(ch)) is None)
    check("传输层 ping 关掉，保活交给应用层帧", wecom_channel.WeComAibotChannel.protocol_heartbeat is None)

    original = wecom_channel.SDK_IMPORT_ERROR
    try:
        wecom_channel.SDK_IMPORT_ERROR = "引擎运行时未安装官方 wecom-aibot-python-sdk（No module named aibot）"
        reason = wecom_channel.WeComAibotChannel().unsupported_reason
        check("SDK 缺失时原因说清是缺依赖", "wecom-aibot-python-sdk" in reason, reason[:200])
    finally:
        wecom_channel.SDK_IMPORT_ERROR = original
    check("本机 SDK 可用（所以通道 supported=True）", ch.supported is True and original == "", str((ch.supported, original))[:200])
    check("协议常量确实取自官方 SDK 而不是自己拼的", str(wecom_channel.WsCmd.SUBSCRIBE) == "aibot_subscribe", str(wecom_channel.WsCmd.SUBSCRIBE))


async def _ack_is_noop(ch) -> None:
    await ch.ack(None, {"cmd": "aibot_msg_callback"}, ch.parse_event(msg_callback("cb-noop", text="hi", msgid="msgid-noop")))


def main() -> None:
    fake = FakeWeCom(script_on=10**9)  # 剧本先关着：等知道哪条连接是引擎真正那条再打开
    fake.start()
    try:
        phase_ab(fake)
        phase_c()
    finally:
        fake.stop()
        asyncio.run(im_channels.shutdown())
    print("-" * 52)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine im wecom selftest: ALL PASS")


if __name__ == "__main__":
    main()
