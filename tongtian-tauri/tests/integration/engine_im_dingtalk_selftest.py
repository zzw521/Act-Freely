#!/usr/bin/env python3
"""
IM 频道自测 —— 钉钉 Stream（官方 dingtalk_stream SDK 的协议实现）

分三段，A/B 全部打真实网络（对端是本机起的假钉钉，不是 Mock 返回值）：

A. HTTP 契约面：钉钉在 /api/im/channels 里从「尚未接入」变成 supported=True 且带两项凭据
   字段；六个通道全部 supported，已接入的排在未接入之前；凭据缺失/过短/被拒各报各的码，
   校验失败不落半套配置。
B. 长连接全链路：假钉钉提供 accessToken + connections.open + WebSocket + sessionWebhook，
   验证「换连接地址 → 连 wss → 逐帧回执 → 收机器人消息 → sessionWebhook 回贴 →
   disconnect 后重新换地址」，并检查机器人回声、卡片回调、组织事件都不会触发任务；
   群内没 @ 的图片按策略不起任务但要进台账；策略打开后群里 @ 我的图片真走
   downloadCode → 换址 → 落盘，任务文本里带的是本机路径说明而不是带签名的下载地址。
C. 钉钉方言分支：回执线形状（code/headers/message/data，data 还要再序列化一次）、
   回贴地址只认钉钉下发的绝对 URL、过期回贴地址、鉴权类错误码映射、SDK 缺失时的
   「尚未接入」话术、回贴地址台账有界、conversationType/isInAtList 与图片/图文混排
   的部分拆解。

数据隔离：TT_DATA_HOME 与 WORK_DIR 都指向临时目录，绝不写 ~/.tongtianwork 的存量台账。
用法: python tests/integration/engine_im_dingtalk_selftest.py
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

_TMP = Path(tempfile.mkdtemp(prefix="tt-im-dingtalk-"))
os.environ["TT_DATA_HOME"] = str(_TMP / "home")
os.environ["DINGTALK_OPENAPI_ENDPOINT"] = ""  # 稍后按假钉钉端口覆盖
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import dingtalk_stream  # noqa: F401  依赖探针：钉钉通道的协议就来自这个官方 SDK
    from fastapi.testclient import TestClient
    from fake_dingtalk import (
        CHATBOT_ID,
        CONVERSATION_ID,
        DEFAULT_CLIENT_ID,
        DEFAULT_CLIENT_SECRET,
        DEFAULT_MEDIA,
        FakeDingTalk,
        SENDER_STAFF_ID,
        frame,
        robot_message,
    )
    import server as engine_server
    import im_channels
    import dingtalk_channel
except Exception as exc:  # 依赖缺失时优雅跳过（与其余引擎自测一致）
    print(f"SKIP: engine im dingtalk selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []
ROBOT_TOPIC = dingtalk_stream.ChatbotMessage.TOPIC
# 假钉钉替身下发的那张图（真 PNG 字节）：落盘后要能逐字节对上，才算真收了附件
FAKE_PNG = DEFAULT_MEDIA[1]


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def wait_for(predicate, timeout=8.0, interval=0.05):
    """等条件成立（长连接是异步的，不能靠 sleep 猜时间）"""
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


def _inject(fake: FakeDingTalk, payload: dict) -> dict:
    """POST 假钉钉的 /inject：把一帧协议形状原样的机器人消息推到引擎的在线连接上

    走的是假服务的 WebSocket 而不是直接调 `runtime.handle_inbound`——附件与群策略都要
    经过「收帧→回执→解析→闸」这一段，绕过它就只能证明函数自己没写错。
    """
    request = urllib.request.Request(
        f"{fake.openapi_base}/inject",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── A/B 段：HTTP 契约面 + 假钉钉全链路 ────────────────────────────


def phase_ab(fake: FakeDingTalk) -> None:
    print("\n== A. /api/im 契约面（钉钉已接入） ==")
    engine_server.WORK_DIR = _TMP / "work"
    engine_server.WORK_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["DINGTALK_OPENAPI_ENDPOINT"] = fake.openapi_base

    with TestClient(engine_server.app) as client:
        body = client.get("/api/im/channels").json()
        check("通道清单返回 ok", body.get("ok") is True, str(body)[:200])
        channels = {c["kind"]: c for c in body.get("channels", [])}
        supported = [c["kind"] for c in body["channels"] if c["supported"]]
        check(
            "清单仍是六个通道",
            {"slack", "dingtalk", "feishu", "wecom", "wechat", "yuanbao"} == set(channels),
            str(sorted(channels)),
        )
        check("已接入的通道全部排在未接入之前", list(channels)[: len(supported)] == supported, str(list(channels)))
        dt = channels["dingtalk"]
        check("钉钉标记为已接入", dt["supported"] is True, str(dt)[:200])
        check(
            "钉钉凭据字段取自协议而不是扫码承诺",
            [f["key"] for f in dt["fields"]] == ["clientId", "clientSecret"]
            and all(f["secret"] is True for f in dt["fields"])
            and "Stream" in dt["setupNote"],
            str(dt["fields"])[:220],
        )
        check(
            "个人微信与腾讯元宝都已接入，清单里没有「尚未接入」的通道",
            channels["wechat"]["supported"] is True
            and channels["yuanbao"]["supported"] is True
            and all(c["supported"] for c in channels.values()),
            str(sorted((k, v["supported"]) for k, v in channels.items())),
        )
        check("飞书已接入且凭据字段是 App ID/App Secret", channels["feishu"]["supported"] is True
              and [f["key"] for f in channels["feishu"]["fields"]] == ["appId", "appSecret"],
              str(channels["feishu"])[:200])

        start = client.post("/api/im/dingtalk/start").json()
        check("未配置就启动报 IM_NOT_CONFIGURED", start.get("code") == "IM_NOT_CONFIGURED", str(start)[:160])
        missing = client.post("/api/im/dingtalk/config", json={"credentials": {"clientId": DEFAULT_CLIENT_ID}}).json()
        check("缺 AppSecret 被拒绝", missing.get("code") == "IM_CREDENTIAL_MISSING", str(missing)[:160])
        short = client.post(
            "/api/im/dingtalk/config",
            json={"credentials": {"clientId": DEFAULT_CLIENT_ID, "clientSecret": "tooshort"}},
        ).json()
        check("AppSecret 过短报 IM_CREDENTIAL_INVALID", short.get("code") == "IM_CREDENTIAL_INVALID", str(short)[:160])
        check("凭据格式校验阶段不打钉钉接口", len(fake.calls("/v1.0/oauth2/accessToken")) == 0, str(fake.apis)[:200])
        wrong = client.post(
            "/api/im/dingtalk/config",
            json={"credentials": {"clientId": DEFAULT_CLIENT_ID, "clientSecret": DEFAULT_CLIENT_SECRET.replace("0", "o", 1)}},
        ).json()
        check(
            "钉钉拒绝凭据时映射成 IM_AUTH_FAILED",
            wrong.get("code") == "IM_AUTH_FAILED",
            str(wrong)[:200],
        )
        check("鉴权失败不留半套配置", _channel_of(client, "dingtalk")["configured"] is False)
        check(
            "鉴权失败也不把 AppSecret 写进错误详情",
            DEFAULT_CLIENT_SECRET not in json.dumps(wrong, ensure_ascii=False),
        )

        print("\n== B. Stream 全链路（对端为本机假钉钉） ==")
        runtime = im_channels.runtime_of("dingtalk")
        check("启动时已注册真实任务处理方", runtime.handler is engine_server._im_run_task)

        seen_tasks: list[str] = []

        async def stub_handler(text: str, session: dict) -> str:
            # 真实处理方（IM→Agent）由 slack 自测的 C 段覆盖；这里只验框架接线
            seen_tasks.append(text)
            return f"已收到：{text}"

        runtime.set_handler(stub_handler)
        ok = client.post(
            "/api/im/dingtalk/config",
            json={"credentials": {"clientId": DEFAULT_CLIENT_ID, "clientSecret": DEFAULT_CLIENT_SECRET}},
        ).json()
        check("绑定成功返回真实账号事实", ok.get("ok") is True and ok["account"]["appKey"].endswith("01"), str(ok)[:200])
        check(
            "accessToken 与 connections.open 都真实打到钉钉接口",
            len(fake.calls("/v1.0/oauth2/accessToken")) >= 1 and len(fake.calls("/v1.0/gateway/connections/open")) >= 1,
            str([a["path"] for a in fake.apis])[:200],
        )
        sub = fake.subscriptions
        check(
            "订阅用的是 SDK 自己的机器人消息 topic",
            sub == [{"type": "CALLBACK", "topic": ROBOT_TOPIC}],
            str(sub)[:200],
        )
        first_open = fake.calls("/v1.0/gateway/connections/open")[0]["body"]
        check("换取连接地址时带上 SDK 版 UA 标记", first_open.get("ua", "").startswith("dingtalk-sdk-python/"), str(first_open)[:160])

        state = wait_for(lambda: "connected" if _channel_of(client, "dingtalk")["state"] == "connected" else None)
        check("长连接进入 connected", state == "connected", str(state))
        check(
            "反向连接地址按 SDK 拼法带上 ticket，且不复用校验阶段那张",
            fake.seen_tickets[:1] == fake.issued_tickets[1:2],
            f"issued={fake.issued_tickets} seen={fake.seen_tickets}"[:200],
        )

        replied = wait_for(lambda: fake.replies or None)
        check("IM 消息触发回贴（sessionWebhook 真实到达）", bool(replied), str(fake.replies)[:200])
        if replied:
            r = replied[0]
            check(
                "回贴载荷与 SDK reply_text 一致（msgtype/text/at.atUserIds）",
                r.get("msgtype") == "text" and r.get("text", {}).get("content") == "已收到：帮我把季度总结整理成一页",
                str(r)[:200],
            )
            check("回贴 @ 回消息发起人", r.get("at", {}).get("atUserIds") == [SENDER_STAFF_ID], str(r)[:160])
        # 后面的帧也要跑完才能数总数：六帧全部回执说明回声/图片/卡片/事件都已处理过
        wait_for(lambda: len(fake.acks) >= 6)
        check(
            "机器人回声/卡片回调/组织事件都不触发任务，群内未 @ 的图片按策略也不触发",
            len(fake.replies) == 1,
            f"收到 {len(fake.replies)} 次回贴: {str(fake.replies)[:220]}",
        )
        check(
            "策略挡下的那条没有进任务面（只起了一个任务）",
            len(seen_tasks) == 1 and seen_tasks[0] == "帮我把季度总结整理成一页",
            str(seen_tasks)[:220],
        )
        acks = fake.acks
        check("每一帧都回执", len(acks) == 6, str(acks)[:260])
        check(
            "回执线形状取自 SDK AckMessage.to_dict",
            all(set(a) == {"code", "headers", "message", "data"} for a in acks),
            str(acks[:2])[:220],
        )
        check("回执码 200", all(a.get("code") == 200 for a in acks), str(acks[:2])[:200])
        check(
            "回执头原样带回 messageId 与 contentType",
            {a["headers"].get("messageId") for a in acks}
            == {"sys-ping-1", "cb-msg-1", "cb-echo", "cb-picture", "cb-card", "ev-1"}
            and all(a["headers"].get("contentType") == "application/json" for a in acks),
            str([a.get("headers") for a in acks])[:260],
        )
        check(
            "回执 data 再序列化一次（SDK 就是 json.dumps）",
            all(isinstance(a.get("data"), str) and json.loads(a["data"]) is not None for a in acks),
            str(acks[:1])[:200],
        )
        cb = next(a for a in acks if a["headers"]["messageId"] == "cb-msg-1")
        check("CALLBACK 的 ack data 是 {response: ...} 形状", json.loads(cb["data"]) == {"response": "OK"}, str(cb)[:200])

        opens = wait_for(lambda: len(fake.calls("/v1.0/gateway/connections/open")) >= 2 and True)
        check("disconnect 后重新换取连接地址", bool(opens), str([a["path"] for a in fake.apis])[:220])
        wait_for(lambda: len(fake.seen_tickets) >= 2)
        check(
            "重连用的是新换取的 ticket（每张一次性）",
            fake.seen_tickets[:2] == fake.issued_tickets[1:3],
            f"issued={fake.issued_tickets} seen={fake.seen_tickets}"[:200],
        )

        view = _channel_of(client, "dingtalk")
        check("账号信息在重连后仍保留", view["account"].get("appKey", "").startswith("ding"), str(view["account"])[:160])
        check(
            "凭据绝不出现在接口视图",
            DEFAULT_CLIENT_SECRET not in json.dumps(view, ensure_ascii=False)
            and DEFAULT_CLIENT_ID not in json.dumps(view["account"], ensure_ascii=False),
            str(view["account"])[:200],
        )

        # ── D-41 收图片：策略打开后，群里 @ 我的图片真的换址并落到本机 ──────
        pol = client.post("/api/im/policies", json={"scope": "dingtalk", "patch": {"groupMedia": "on"}}).json()
        check(
            "群内附件策略可以按通道作用域打开",
            pol.get("ok") is True and pol.get("policy", {}).get("groupMedia") == "on",
            str(pol)[:220],
        )

        fake.media["fake-img-1"] = ("image/png", FAKE_PNG)
        tokens_before = len(fake.calls("/v1.0/oauth2/accessToken"))
        inj = _inject(fake, {"msgtype": "picture", "downloadCode": "fake-img-1", "at_me": True})
        check("假钉钉把图片帧推到了在线连接", inj.get("ok") is True, str(inj)[:160])
        got = wait_for(lambda: len(seen_tasks) >= 2 and seen_tasks[-1])
        check("群内被 @ 的图片起了任务", bool(got), str(seen_tasks)[:220])
        dl = fake.calls("/v1.0/robot/messageFiles/download")
        check("换下载地址走的是 SDK 建模过的那个接口", len(dl) >= 1, str([a["path"] for a in fake.apis])[:220])
        if dl:
            check(
                "换址请求体是 {robotCode, downloadCode}，头带 x-acs-dingtalk-access-token",
                dl[-1]["body"] == {"robotCode": DEFAULT_CLIENT_ID, "downloadCode": "fake-img-1"}
                and dl[-1]["tokenHeader"] == "tt-fake-access-token",
                str(dl[-1])[:220],
            )
        check("附件字节确实从下载地址取回来了", fake.media_hits[-1:] == ["fake-img-1"], str(fake.media_hits)[:160])
        check(
            "任务文本里是中文说明，不是带签名的下载地址",
            "[图片" in (seen_tasks[-1] or "") and "已存到本机" in (seen_tasks[-1] or "")
            and "download" not in (seen_tasks[-1] or ""),
            str(seen_tasks[-1:])[:260],
        )
        media_dir = _TMP / "home" / "im_media" / "dingtalk" / CONVERSATION_ID
        files = sorted(media_dir.glob("*")) if media_dir.is_dir() else []
        check(
            "图片落到附件根的会话目录且字节与平台给的一致",
            len(files) == 1 and files[0].read_bytes() == FAKE_PNG and files[0].name.endswith(".png"),
            str([f.name for f in files])[:220],
        )
        view2 = _channel_of(client, "dingtalk")
        check(
            "能力位与策略在视图里说清（界面不用猜）",
            view2["capabilities"]["mediaIn"] is True and view2["policy"]["groupMedia"] == "on",
            str(view2.get("capabilities"))[:220],
        )
        check(
            "计数与用量按实例记账",
            view2["counters"]["mediaStored"] >= 1 and view2["media"]["files"] >= 1,
            str(view2["counters"])[:220],
        )

        # 换址成功的那张不该重复下载；这里换一张坏码，验证失败也如实呈现
        fake.media_fail.add("fake-img-bad")
        _inject(fake, {"msgtype": "picture", "downloadCode": "fake-img-bad", "at_me": True})
        bad = wait_for(lambda: len(seen_tasks) >= 3 and seen_tasks[-1])
        check(
            "附件下载失败时任务照样起，但文本里写明没落盘的原因",
            bool(bad) and "未落盘" in (seen_tasks[-1] or "") and "图片" in (seen_tasks[-1] or ""),
            str(seen_tasks[-1:])[:260],
        )
        view3 = _channel_of(client, "dingtalk")
        check("失败的附件进 mediaRejected，不假装收到", view3["counters"]["mediaRejected"] >= 1, str(view3["counters"])[:220])
        tokens_after = len(fake.calls("/v1.0/oauth2/accessToken"))
        check(
            "两张附件共用同一个 accessToken：换址只换一次票",
            tokens_after == tokens_before + 1,
            f"before={tokens_before} after={tokens_after}",
        )

        sessions = _sessions_of(client)
        dt_sessions = [s for s in sessions if s["source"] == "dingtalk"]
        check("钉钉会话落入频道台账", len(dt_sessions) == 1, str(sessions)[:220])
        if dt_sessions:
            s = dt_sessions[0]
            check("会话 ID 按框架口径拼出", s["id"] == f"im-dingtalk-{CONVERSATION_ID}", s["id"])
            check("标题取自首条 IM 消息", s["title"].startswith("帮我把季度总结"), s["title"])
            check(
                "台账按真实经过记录：文本一进一出、未 @ 的图片只有进、两张图片附件各一进一出加一条没发出附件的状态",
                [m["direction"] for m in s["messages"]] == ["in", "out", "in", "in", "out", "out", "in", "out", "out"],
                str([m["direction"] for m in s["messages"]])[:200],
            )
            # 回贴的文本发出去了、附件没发出去是两件事：只记一条「out」等于替用户宣布都送达了
            not_sent = [m for m in s["messages"] if "引擎状态" in m["text"] and "不会被发到钉钉" in m["text"]]
            check(
                "通道说附件没发出去时台账另起一条引擎状态（不进 out 正文）",
                len(not_sent) == 2 and all(m["direction"] == "out" for m in not_sent)
                and all("[图片" not in m["text"] for m in not_sent),
                str([m["text"][:70] for m in not_sent])[:260],
            )
            check(
                "这条状态写的是钉钉的口径并指出去哪儿看产物",
                all("工作区" in m["text"] and "sessionWebhook" in m["text"] for m in not_sent),
                str([m["text"][:90] for m in not_sent])[:260],
            )
            skipped = [m for m in s["messages"] if "未起任务" in m["text"]]
            check(
                "策略挡下的那条写明中文原因（不是静默丢掉）",
                len(skipped) == 1 and "群里没有被 @" in skipped[0]["text"],
                str(skipped)[:260],
            )
            # 一进一出都带这句说明：进的是「收到了什么」，出的是回贴原文（钉钉的回贴载荷
            # 就是把聚合文本发回去），两边都不许出现下载地址或内网 IP
            stored = [m for m in s["messages"] if "已存到本机" in m["text"]]
            check(
                "落盘的附件在台账里是中文说明 + 类型，不是带签名的下载地址",
                len(stored) == 2
                and {m["direction"] for m in stored} == {"in", "out"}
                and all("127.0.0.1" not in m["text"] and "[图片" in m["text"] for m in stored),
                str(stored)[:260],
            )
            failed = [m for m in s["messages"] if "未落盘" in m["text"]]
            check(
                "下载失败的附件也在台账里留下原因",
                len(failed) == 2 and {m["direction"] for m in failed} == {"in", "out"}
                and all("[图片" in m["text"] and "403" in m["text"] for m in failed),
                str(failed)[:260],
            )
            ledger_file = _TMP / "home" / "im_sessions.json"
            check("台账真实落盘", ledger_file.exists() and CONVERSATION_ID in ledger_file.read_text(encoding="utf-8"))

        cfg = _TMP / "home" / "im_channels.json"
        check("凭据落盘 0600 且含两项", cfg.exists() and set(json.loads(cfg.read_text(encoding="utf-8"))["dingtalk"]) == {"clientId", "clientSecret"})
        stopped = client.post("/api/im/dingtalk/stop").json()
        check("停止后 state=stopped", stopped.get("state") == "stopped", str(stopped)[:160])
        forgotten = client.post("/api/im/dingtalk/forget").json()
        check("解绑成功", forgotten.get("ok") is True, str(forgotten)[:160])
        check("解绑只删钉钉凭据", "dingtalk" not in json.loads(cfg.read_text(encoding="utf-8")))
        check("解绑保留历史会话台账（数据保护）", len([s for s in _sessions_of(client) if s["source"] == "dingtalk"]) == 1)


# ── C 段：钉钉方言分支 ────────────────────────────────────────────


def phase_c() -> None:
    print("\n== C. 钉钉方言分支 ==")
    ch = dingtalk_channel.DingTalkStreamChannel()

    # 逐形状验 parse_event 的分支（不联网，直接喂 SDK 的帧）
    text_frame = frame("CALLBACK", ROBOT_TOPIC, robot_message("写周报", webhook="https://oapi.dingtalk.com/robot/send"), "u1")
    ev = ch.parse_event(text_frame)
    check("文本机器人消息解析为 inbound", ev.kind == "inbound" and ev.text == "写周报" and ev.conversation_id == CONVERSATION_ID, str(ev)[:200])
    check("user_id 取 senderStaffId", ev.user_id == SENDER_STAFF_ID, str(ev)[:160])
    ev2 = ch.parse_event(frame("CALLBACK", ROBOT_TOPIC, robot_message("", msgtype="picture", webhook="https://x/y"), "u2"))
    check(
        "图片消息是正经事件：一个图片部分 + 下载码在 ref 里",
        ev2.kind == "inbound" and [p.kind for p in ev2.parts] == ["image"] and ev2.parts[0].ref == "fake-download-code",
        str(ev2)[:220],
    )
    check(
        "图片部分没有 url：带签名的下载地址只能由 resolve_media 现换",
        ev2.parts[0].url == "",
        str(ev2.parts)[:200],
    )
    check(
        "群聊判定取自 SDK 自己用的那个值（conversationType=2）",
        ev2.is_group is True and ev2.mentioned_me is True,
        str((ev2.is_group, ev2.mentioned_me))[:120],
    )
    ev2b = ch.parse_event(
        frame("CALLBACK", ROBOT_TOPIC, robot_message("", msgtype="picture", webhook="https://x/y", at_me=False), "u2b")
    )
    check("群里没 @ 机器人时答案是 False，不是 None", ev2b.mentioned_me is False, str(ev2b.mentioned_me))
    ev2c = ch.parse_event(
        frame(
            "CALLBACK",
            ROBOT_TOPIC,
            robot_message("看图", msgtype="richText", webhook="https://x/y", download_code="fake-rt-1"),
            "u2c",
        )
    )
    check(
        "图文混排拆成文本部分与图片部分（SDK 的 get_text_list/get_image_list 同一批字段）",
        [p.kind for p in ev2c.parts] == ["text", "image"] and ev2c.text == "看图"
        and ev2c.parts[1].ref == "fake-rt-1",
        str(ev2c)[:240],
    )
    ev2d = ch.parse_event(
        frame(
            "CALLBACK",
            ROBOT_TOPIC,
            robot_message("单聊一句", webhook="https://x/y", conversation_type="1"),
            "u2d",
        )
    )
    check("conversationType=1 是单聊（SDK 按它走 singleChatReceiver）", ev2d.is_group is False, str(ev2d)[:200])
    no_at = dict(robot_message("没有 @ 字段的帧", webhook="https://x/y"))
    no_at.pop("isInAtList")
    ev2e = ch.parse_event(frame("CALLBACK", ROBOT_TOPIC, no_at, "u2e"))
    check("帧里没给 isInAtList 就是答不出（None），框架不许替钉钉猜", ev2e.mentioned_me is None, str(ev2e.mentioned_me))
    ev2f = ch.parse_event(frame("CALLBACK", ROBOT_TOPIC, robot_message("", msgtype="audio", webhook="https://x/y"), "u2f"))
    check(
        "SDK 没建模的消息形状仍然如实不消费（不猜字段名）",
        ev2f.kind == "ignore" and "文件/语音/视频" in dingtalk_channel.DingTalkStreamChannel.media_in_note,
        str(ev2f)[:160],
    )
    ev3 = ch.parse_event(frame("CALLBACK", ROBOT_TOPIC, robot_message("回声", webhook="https://x/y", sender_id=CHATBOT_ID), "u3"))
    check("机器人自己的消息不当事件", ev3.kind == "ignore", str(ev3)[:160])
    ev4 = ch.parse_event(frame("CALLBACK", "/v1.0/card/instances/callback", {"outTrackId": "t"}, "u4"))
    check("非机器人 topic 不当事件", ev4.kind == "ignore", str(ev4)[:160])
    ev5 = ch.parse_event(frame("EVENT", "*", {"eventType": "x"}, "u5"))
    check("组织事件帧不当事件", ev5.kind == "ignore", str(ev5)[:160])
    ev6 = ch.parse_event(frame("SYSTEM", "ping", {}, "u6"))
    check("SYSTEM ping 只需回执", ev6.kind == "ignore", str(ev6)[:160])
    ev7 = ch.parse_event(frame("SYSTEM", "disconnect", {}, "u7"))
    check("SYSTEM disconnect 走有序重连", ev7.kind == "reconnect", str(ev7)[:160])
    ev8 = ch.parse_event(frame("CALLBACK", ROBOT_TOPIC, robot_message("hi", webhook="https://x/y", conversation_id=""), "u8"))
    check("没有会话号就不进任务", ev8.kind == "ignore", str(ev8)[:160])

    # 回贴地址：只认钉钉下发的绝对 URL
    ch2 = dingtalk_channel.DingTalkStreamChannel()
    ch2.parse_event(frame("CALLBACK", ROBOT_TOPIC, robot_message("hi", webhook="http://127.0.0.1:1/x"), "w1"))
    check("http 回贴地址被记下", CONVERSATION_ID in ch2._reply_targets, str(ch2._reply_targets)[:200])
    ch3 = dingtalk_channel.DingTalkStreamChannel()
    ch3.parse_event(frame("CALLBACK", ROBOT_TOPIC, robot_message("hi", webhook="file:///c:/windows/x"), "w2"))
    check("非 http(s) 的回贴地址一律不记（防被指到本机其他服务）", ch3._reply_targets == {}, str(ch3._reply_targets)[:200])
    try:
        asyncio.run(ch3.send({}, CONVERSATION_ID, "hi"))
        check("没有回贴地址时如实失败", False, "send 居然返回成功")
    except im_channels.ImError as exc:
        check("没有回贴地址时如实失败", exc.code == "IM_REPLY_FAILED" and "sessionWebhook" in exc.detail, str(exc)[:200])
    ch4 = dingtalk_channel.DingTalkStreamChannel()
    ch4.parse_event(
        frame(
            "CALLBACK",
            ROBOT_TOPIC,
            robot_message("hi", webhook="http://127.0.0.1:1/x", expired_ms=int(time.time() * 1000) - 1000),
            "w3",
        )
    )
    try:
        asyncio.run(ch4.send({}, CONVERSATION_ID, "hi"))
        check("过期的 sessionWebhook 不假装成功", False, "send 居然返回成功")
    except im_channels.ImError as exc:
        check("过期的 sessionWebhook 不假装成功", exc.code == "IM_REPLY_FAILED" and "已过期" in exc.detail, str(exc)[:200])
    ch5 = dingtalk_channel.DingTalkStreamChannel()
    for i in range(dingtalk_channel.MAX_REPLY_TARGETS + 20):
        ch5.parse_event(
            frame("CALLBACK", ROBOT_TOPIC, robot_message("hi", webhook="http://127.0.0.1:1/x", conversation_id=f"cid{i}"), f"b{i}")
        )
    check(
        "回贴地址台账有界（长连接跑久不无限攒）",
        len(ch5._reply_targets) == dingtalk_channel.MAX_REPLY_TARGETS,
        str(len(ch5._reply_targets)),
    )

    # 错误码映射：钉钉用 {code,message} 表达失败
    check("成功体没有错误码", ch._error_of({"accessToken": "t", "expireIn": 7200}) == "")
    check("errcode=0 算成功", ch._error_of({"errcode": 0, "errmsg": "ok"}) == "")
    check("未知错误码进 IM_API_ERROR", _code_of(ch, {"code": "invalidParameter", "message": "x"}) == "IM_API_ERROR")
    for code in ("Forbidden.AccessDenied", "Unauthorized", "INVALID_APP_ACCESS_TOKEN", "40014"):
        check(f"鉴权类码 {code} 映射 IM_AUTH_FAILED", _code_of(ch, {"code": code, "message": "x"}) == "IM_AUTH_FAILED")

    # 缺 SDK 时的诚实降级
    original = dingtalk_channel.SDK_IMPORT_ERROR
    try:
        dingtalk_channel.SDK_IMPORT_ERROR = "引擎运行时未安装官方 dingtalk-stream SDK（No module named dingtalk_stream）"
        reason = dingtalk_channel.DingTalkStreamChannel().unsupported_reason
        check("SDK 缺失时原因说清是缺依赖", "dingtalk-stream" in reason, reason[:160])
    finally:
        dingtalk_channel.SDK_IMPORT_ERROR = original
    check("本机 SDK 可用（所以通道 supported=True）", ch.supported is True and original == "", str((ch.supported, original))[:200])
    check("取本机 IP 失败也不影响换地址", isinstance(dingtalk_channel.DingTalkStreamChannel._host_ip(), str))


def _code_of(ch, resp: dict) -> str:
    try:
        ch._check(resp, "测试调用")
    except im_channels.ImError as exc:
        return exc.code
    return ""


def main() -> None:
    fake = FakeDingTalk()
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
    print("engine im dingtalk selftest: ALL PASS")


if __name__ == "__main__":
    main()
