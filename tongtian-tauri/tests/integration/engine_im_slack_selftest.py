#!/usr/bin/env python3
"""
IM 频道自测（任务 #46 引擎侧多通道同构骨架 + Slack Socket Mode 真链路）

分三段，全部打真实网络（对端是本机起的假 Slack，不是 Mock 返回值）：

A. HTTP 契约面：/api/im/channels 的通道清单与状态字段；未实现的通道类仍如实
   IM_UNSUPPORTED；未配置就启动报 IM_NOT_CONFIGURED；未知 kind 报 IM_CHANNEL_UNKNOWN。
B. 长连接全链路：假 Slack 提供 apps.connections.open + WebSocket + auth.test +
   chat.postMessage，验证「换连接地址 → 连 wss → challenge 回执 → events_api 回执 →
   收用户消息 → 回贴 → disconnect 后重新换地址」整条循环，并检查 bot 回声不会重复触发。
C. IM 文本 → 执行流 → 回贴正文：喂给 _im_run_task 一段真实的 SSE 执行流，
   验证 deliver.final_text / 产物名 / error 三种终态各自回什么（失败不假装完成），
   以及「在 IM 频道中展示工具调用执行过程」这个引擎偏好的两种取值各自贴什么。
D. Slack 方言（D-41）：channel_type 判群/单聊、app_mention 与文本 <@机器人用户 ID>
   判「有没有 @ 我」、信封没给身份时如实答不出（None），以及能力位读数。

数据隔离：TT_DATA_HOME 与 WORK_DIR 都指向临时目录，绝不写 ~/.tongtianwork 的存量台账。
用法: python tests/integration/engine_im_slack_selftest.py
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TMP = Path(tempfile.mkdtemp(prefix="tt-im-"))
os.environ["TT_DATA_HOME"] = str(_TMP / "home")
os.environ["TT_SLACK_API_BASE"] = ""  # 稍后按假 Slack 端口覆盖
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import aiohttp  # noqa: F401  依赖探针：引擎侧长连接与假 Slack 都要它
    from fastapi.testclient import TestClient
    from fake_slack import BOT_USER_ID, FakeSlack, event_payload
    import server as engine_server
    import im_channels
    import slack_channel  # noqa: F401
except Exception as exc:  # 依赖缺失时优雅跳过（与其余引擎自测一致）
    print(f"SKIP: engine im slack selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []
APP_TOKEN = "xapp-1-TTFAKE-abcdef123456"
BOT_TOKEN = "xoxb-TTFAKE-abcdef123456"


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


# ── C 段：执行流 → 回贴正文 ──────────────────────────────────────


def phase_c() -> None:
    print("\n== C. IM 文本 → 执行流 → 回贴正文 ==")
    check(
        "deliver 事件解析出终态正文",
        engine_server._parse_sse_chunk('event: deliver\ndata: {"final_text": "已完成"}\n\n')
        == ("deliver", {"final_text": "已完成"}),
    )
    check("keepalive 注释行不当事件", engine_server._parse_sse_chunk(": keepalive\n\n") == ("", {}))
    check(
        "坏 JSON 的 data 行不炸流",
        engine_server._parse_sse_chunk('event: deliver\ndata: {oops\n\n') == ("deliver", {}),
    )
    reply = engine_server._im_reply_of("季报已汇总", ["/tmp/a.xlsx", "/tmp/b.md"], "")
    check("有产物时列出真实文件名", "a.xlsx" in reply and "b.md" in reply, reply)
    check("失败终态如实说失败", engine_server._im_reply_of("", [], "ENGINE_CONFIG_ERROR 缺 key").startswith("任务失败"))
    check(
        "没有正文也没有产物时不编「任务完成」",
        "没有产出正文" in engine_server._im_reply_of("", [], ""),
    )

    # 用假的执行流生成器替身，验证 _im_run_task 真的消费 SSE 并组装回贴
    real_run = engine_server._run_execution

    async def fake_execution(session, task_id, goal, replay_from, *args, **kwargs):
        fake_execution.seen_goal = goal
        yield f'id: {task_id}:1\nevent: step_start\ndata: {{"step_id": "step-1"}}\n\n'
        yield (
            'event: step_start\ndata: {"step_id": "step-2", "stepType": "tool_call", '
            '"tool": "write_text_file", "description": "调用工具: write_text_file"}\n\n'
        )
        yield 'event: deliver\ndata: {"final_text": "已经写好一页总结", "artifacts": ["/tmp/summary.md"]}\n\n'
        yield "event: done\ndata: {}\n\n"

    engine_server._run_execution = fake_execution
    try:
        im_channels.save_preferences({"showToolProcess": False})
        out = asyncio.run(engine_server._im_run_task("帮我写季度总结", {"source": "slack", "conversationId": "D0123"}))
        check("IM 文本原样进入执行流", fake_execution.seen_goal == "帮我写季度总结", fake_execution.seen_goal)
        check("回贴取执行流终态正文与产物", "已经写好一页总结" in out and "summary.md" in out, out)
        check("偏好关闭时回贴不带工具步骤（只回最终回复）", "write_text_file" not in out, out)

        im_channels.save_preferences({"showToolProcess": True})
        verbose = asyncio.run(engine_server._im_run_task("帮我写季度总结", {"source": "slack", "conversationId": "D0123"}))
        check(
            "偏好打开时回贴附执行流里真实调用过的工具",
            "执行过程" in verbose and "调用工具 write_text_file" in verbose,
            verbose,
        )
        check(
            "偏好打开也不给失败任务铺执行过程（失败就说失败）",
            engine_server._im_reply_of("", [], "AGENT_ERROR 模型超时", ["write_text_file"]).startswith("任务失败"),
        )
    finally:
        engine_server._run_execution = real_run
        im_channels.save_preferences({"showToolProcess": False})

    im_channels.save_preferences({"showToolProcess": False})
    quiet = engine_server._im_reply_of("季报已汇总", [], "", ["view_text_file", "write_text_file"])
    check("开关关闭时正文里没有执行过程", "执行过程" not in quiet, quiet)
    im_channels.save_preferences({"showToolProcess": True})
    noisy = engine_server._im_reply_of("季报已汇总", [], "", ["view_text_file", "write_text_file"])
    check(
        "开关打开时按顺序列出每一步",
        "1. 调用工具 view_text_file" in noisy and "2. 调用工具 write_text_file" in noisy,
        noisy,
    )
    check("没有任何工具调用时不编执行过程", "执行过程" not in engine_server._im_reply_of("好的", [], "", []))
    im_channels.save_preferences({"showToolProcess": False})


# ── A/B 段：HTTP 契约面 + 假 Slack 全链路 ─────────────────────────


def phase_ab(fake: FakeSlack) -> None:
    print("\n== A. /api/im 契约面 ==")
    engine_server.WORK_DIR = _TMP / "work"
    engine_server.WORK_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["TT_SLACK_API_BASE"] = fake.api_base

    with TestClient(engine_server.app) as client:
        body = client.get("/api/im/channels").json()
        check("通道清单返回 ok", body.get("ok") is True, str(body)[:200])
        channels = {c["kind"]: c for c in body.get("channels", [])}
        check(
            "清单含六个通道",
            {"slack", "dingtalk", "feishu", "wecom", "wechat", "yuanbao"} <= set(channels),
            str(sorted(channels)),
        )
        check("Slack 标记为已接入", channels["slack"]["supported"] is True)
        check(
            "个人微信是扫码登录，腾讯元宝是凭据绑定，两者都已接入",
            channels["wechat"]["supported"] is True
            and channels["wechat"]["loginMode"] == "qr"
            and channels["yuanbao"]["supported"] is True
            and channels["yuanbao"]["loginMode"] == "",
            str({"wechat": channels["wechat"], "yuanbao": channels["yuanbao"]})[:260],
        )
        check(
            "飞书已接入且长连接口径写在指引里",
            channels["feishu"]["supported"] is True and "长连接" in channels["feishu"]["setupNote"],
            str(channels["feishu"])[:200],
        )
        check(
            "Slack 凭据字段带后台指引（不是扫码承诺）",
            [f["key"] for f in channels["slack"]["fields"]] == ["appToken", "botToken"]
            and channels["slack"]["setupNote"],
            str(channels["slack"]["fields"])[:200],
        )
        check("未配置时 configured=false 且 state=not_configured",
              channels["slack"]["configured"] is False and channels["slack"]["state"] == "not_configured")

        try:
            asyncio.run(im_channels.PendingChannel().open_connection({}))
            pending_code = ""
        except im_channels.ImError as exc:
            pending_code = exc.code
        check(
            "尚未实现的通道类仍如实拒绝（IM_UNSUPPORTED）",
            im_channels.PendingChannel.supported is False and pending_code == "IM_UNSUPPORTED",
            pending_code,
        )
        start = client.post("/api/im/slack/start").json()
        check("未配置就启动报 IM_NOT_CONFIGURED", start.get("code") == "IM_NOT_CONFIGURED", str(start)[:160])
        # 定时任务结果推送：入参在触达通道前逐项校验，不把脏值交给通道
        no_target = client.post("/api/im/slack/send", json={"text": "hi"}).json()
        check("主动推送缺目标报 IM_TARGET_MISSING", no_target.get("code") == "IM_TARGET_MISSING", str(no_target)[:160])
        no_text = client.post("/api/im/slack/send", json={"target": "C777"}).json()
        check("主动推送空正文报 IM_TEXT_MISSING", no_text.get("code") == "IM_TEXT_MISSING", str(no_text)[:160])
        too_long = client.post("/api/im/slack/send", json={"target": "C777", "text": "x" * 4001}).json()
        check("主动推送超长报 IM_TEXT_TOO_LONG", too_long.get("code") == "IM_TEXT_TOO_LONG", str(too_long)[:160])
        unconfigured = client.post("/api/im/slack/send", json={"target": "C777", "text": "hi"}).json()
        check("未配置时主动推送报 IM_NOT_CONFIGURED", unconfigured.get("code") == "IM_NOT_CONFIGURED", str(unconfigured)[:160])
        check(
            "未知通道推送报 IM_CHANNEL_UNKNOWN",
            client.post("/api/im/qq/send", json={"target": "u1", "text": "hi"}).json().get("code") == "IM_CHANNEL_UNKNOWN",
        )
        unknown = client.post("/api/im/qq/start").json()
        check("未知通道报 IM_CHANNEL_UNKNOWN", unknown.get("code") == "IM_CHANNEL_UNKNOWN", str(unknown)[:160])
        missing = client.post("/api/im/slack/config", json={"credentials": {"botToken": BOT_TOKEN}}).json()
        check("缺必填凭据被拒绝", missing.get("code") == "IM_CREDENTIAL_MISSING", str(missing)[:160])
        badprefix = client.post(
            "/api/im/slack/config", json={"credentials": {"appToken": "xoxb-wrong", "botToken": BOT_TOKEN}}
        ).json()
        check("App Token 前缀不对直接拒绝", badprefix.get("code") == "IM_CREDENTIAL_INVALID", str(badprefix)[:160])
        check("凭据校验失败不留下半套配置", _channel_of(client, "slack")["configured"] is False)

        prefs = client.get("/api/im/preferences").json()
        check(
            "偏好接口给的是引擎当前真值",
            prefs.get("ok") is True and prefs["preferences"]["showToolProcess"] is False,
            str(prefs)[:160],
        )
        check(
            "未知偏好键被拒绝",
            client.post("/api/im/preferences", json={"showToolProcess": True, "anyOther": True}).json().get("code")
            == "IM_PREFERENCE_INVALID",
        )
        check(
            "非布尔偏好值被拒绝",
            client.post("/api/im/preferences", json={"preferences": {"showToolProcess": "yes"}}).json().get("code")
            == "IM_PREFERENCE_INVALID",
        )
        check(
            "空补丁什么都不写",
            client.post("/api/im/preferences", json={}).json().get("code") == "IM_PREFERENCE_INVALID"
            and im_channels.load_preferences()["showToolProcess"] is False,
        )
        saved = client.post("/api/im/preferences", json={"preferences": {"showToolProcess": True}}).json()
        check("写入后返回引擎落盘的值", saved.get("ok") is True and saved["preferences"]["showToolProcess"] is True, str(saved)[:160])
        pf = _TMP / "home" / "im_preferences.json"
        check("偏好真实落盘", pf.exists() and json.loads(pf.read_text(encoding="utf-8"))["showToolProcess"] is True)
        check("重新读取仍是同一个值", client.get("/api/im/preferences").json()["preferences"]["showToolProcess"] is True)
        check(
            "坏掉的偏好文件按出厂默认读，不影响回贴",
            _prefs_with_broken_file(),
        )

        print("\n== B. Socket Mode 全链路（对端为本机假 Slack） ==")
        runtime = im_channels.runtime_of("slack")
        check("启动时已注册真实任务处理方", runtime.handler is engine_server._im_run_task)

        async def stub_handler(text: str, session: dict) -> str:
            # 真实处理方（IM→Agent）由 C 段单测覆盖；这里用替身只验框架接线，
            # 避免自测依赖模型可用性
            return f"已收到：{text}"

        runtime.set_handler(stub_handler)
        ok = client.post(
            "/api/im/slack/config", json={"credentials": {"appToken": APP_TOKEN, "botToken": BOT_TOKEN}}
        ).json()
        check("绑定成功返回真实账号事实", ok.get("ok") is True and ok["account"]["team"] == "通天测试空间", str(ok)[:200])
        check(
            "账号里带上机器人用户 ID（群里认 @ 就靠它）",
            ok["account"].get("botUserId") == BOT_USER_ID,
            str(ok.get("account"))[:200],
        )

        # 主动推送（定时任务结果）：按频道 ID 直发，真实到达假 Slack
        pushed = client.post(
            "/api/im/slack/send", json={"target": "C777", "text": "【定时任务】每日舆情\n已完成"}
        ).json()
        check(
            "主动推送返回真实送达回执",
            pushed.get("ok") is True and pushed.get("target") == "C777" and pushed.get("sentAtMs", 0) > 0,
            str(pushed)[:200],
        )
        check(
            "主动推送真实到达目标频道（chat.postMessage）",
            any(r.get("channel") == "C777" and "每日舆情" in str(r.get("text")) for r in fake.replies),
            str(fake.replies)[:220],
        )
        check(
            "主动推送不伪造 IM 会话台账",
            all(s["conversationId"] != "C777" for s in _sessions_of(client)),
            str(_sessions_of(client))[:220],
        )

        # 校验阶段真实打到假 Slack 的两次往返
        check("auth.test 被真实调用", len(fake.calls("auth.test")) >= 1, str(fake.apis)[:200])
        check("apps.connections.open 被真实调用", len(fake.calls("apps.connections.open")) >= 1)

        wait_for(lambda: _state_of(client) == "connected")
        state = _state_of(client)
        check("长连接进入 connected", state == "connected", state)

        # 回贴目标是消息来源会话（fake.replies 里还可能有主动推送的 C777，只看 D0123）
        replied = wait_for(lambda: [r for r in fake.replies if r.get("channel") == "D0123"] or None)
        check("IM 消息触发回贴（chat.postMessage 真实到达）", bool(replied), str(fake.replies)[:220])
        if replied:
            check("回贴目标是消息来源会话", replied[0].get("channel") == "D0123", str(replied[0])[:160])
            check("回贴内容来自消息处理方", "季度总结" in str(replied[0].get("text")), str(replied[0])[:200])
        check(
            "bot 回声不会重复触发任务",
            len([r for r in fake.replies if r.get("channel") == "D0123"]) == 1,
            f"收到 {len(fake.replies)} 次回贴: {str(fake.replies)[:200]}",
        )

        challenge_acks = [a for a in fake.acks if a.get("payload", {}).get("challenge")]
        check("url_verification 带 challenge 回执", len(challenge_acks) >= 1, str(fake.acks)[:220])
        envelope_acks = [a for a in fake.acks if set(a) == {"envelope_id"}]
        check("events_api 帧按 envelope_id 回执", len(envelope_acks) >= 2, str(fake.acks)[:220])
        check(
            "回执不带自造字段",
            all(set(a) <= {"envelope_id", "payload"} for a in fake.acks),
            str(fake.acks)[:220],
        )

        # disconnect → 必须重新 apps.connections.open 换新地址
        second = wait_for(lambda: len(fake.calls("apps.connections.open")) >= 3 and True)
        check("disconnect 后重新换取连接地址", bool(second), str([a["method"] for a in fake.apis])[:220])

        view = _channel_of(client, "slack")
        check("状态视图带重连计数", view["reconnects"] >= 1, str(view)[:200])
        check("账号信息在重连后仍保留", view["account"].get("team") == "通天测试空间", str(view["account"])[:160])
        check("连接凭据绝不出现在接口视图", APP_TOKEN not in json.dumps(view) and BOT_TOKEN not in json.dumps(view))
        caps = view.get("capabilities") or {}
        check(
            "能力位说清 Slack 这一侧能判 @、不能收附件与输入状态",
            caps.get("typing") is False and caps.get("mediaIn") is False
            and "channel_type" in str(caps.get("mentionEvidence")),
            str(caps)[:240],
        )
        check(
            "群策略默认「被 @ 才回应」，视图同时给出中文档名（界面不翻译枚举）",
            view["policy"]["groupActivation"] == "mention"
            and view["policyLabels"]["groupActivation"] == "被 @ 时才回应",
            str(view.get("policyLabels"))[:200],
        )
        check(
            "重复抑制的读数按实例给出（界面照抄，不再另写一份窗口规则）",
            "45 秒内" in str(view.get("dedupeNote")), str(view.get("dedupeNote"))[:160],
        )

        sessions = _sessions_of(client)
        check(
            "IM 会话按会话标识分条落账（单聊与群各一条，主动推送那条不算）",
            len(sessions) == 2 and {s["conversationId"] for s in sessions} == {"D0123", "C0777"}
            and {s["source"] for s in sessions} == {"slack"},
            str([(s["conversationId"], s["source"]) for s in sessions])[:220],
        )
        dm = next((s for s in sessions if s["conversationId"] == "D0123"), None)
        grp = next((s for s in sessions if s["conversationId"] == "C0777"), None)
        if dm:
            check("单聊标题取自首条 IM 消息", dm["title"].startswith("帮我把季度总结"), dm["title"])
            check(
                "单聊台账一进一出（dmActivation 默认 always，不需要 @）",
                [m["direction"] for m in dm["messages"]] == ["in", "out"],
                str([m["direction"] for m in dm["messages"]])[:120],
            )
        if grp:
            check(
                "群里没 @ 的那条只进台账、被 @ 的那条才回贴",
                [m["direction"] for m in grp["messages"]] == ["in", "in", "out"],
                str([m["direction"] for m in grp["messages"]])[:160],
            )
            skipped = [m for m in grp["messages"] if "未起任务" in m["text"]]
            check(
                "群策略挡下的那条写明中文原因（不是静默丢掉）",
                len(skipped) == 1 and "群里没有被 @" in skipped[0]["text"],
                str(skipped)[:260],
            )
            check(
                "被 @ 起任务时 @ 的原文留在台账里",
                any("<@U0BOTFAKE>" in m["text"] and "未起任务" not in m["text"] for m in grp["messages"]),
                str(grp["messages"])[:260],
            )
        else:
            check("群里被 @ 的那条建出了会话", False, str(sessions)[:220])
        ledger_file = _TMP / "home" / "im_sessions.json"
        check("台账真实落盘", ledger_file.exists() and "D0123" in ledger_file.read_text(encoding="utf-8"))

        stopped = client.post("/api/im/slack/stop").json()
        check("停止后 state=stopped", stopped.get("state") == "stopped", str(stopped)[:160])

        forgotten = client.post("/api/im/slack/forget").json()
        check("解绑成功", forgotten.get("ok") is True, str(forgotten)[:160])
        cfg = _TMP / "home" / "im_channels.json"
        check("解绑只删该通道凭据", cfg.exists() and "slack" not in json.loads(cfg.read_text(encoding="utf-8")))
        check("解绑保留历史会话台账（数据保护）", len(_sessions_of(client)) == 2)


def _channel_of(client, kind: str) -> dict:
    body = client.get("/api/im/channels").json()
    return next(c for c in body["channels"] if c["kind"] == kind)


def _prefs_with_broken_file() -> bool:
    """偏好文件坏掉时逐项退回出厂默认，而不是让每条回贴都抛（回贴路径每次都读它）"""
    p = im_channels.preferences_path()
    backup = p.read_text(encoding="utf-8") if p.exists() else None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")
        return im_channels.load_preferences() == {"showToolProcess": False}
    finally:
        p.write_text(backup if backup is not None else "{}", encoding="utf-8")


def _state_of(client) -> str:
    return _channel_of(client, "slack")["state"]


def _sessions_of(client) -> list[dict]:
    return client.get("/api/im/channels").json().get("sessions") or []


# ── D 段：Slack 的群/@ 方言（D-41 的判定只取事件里明写的事实） ──────────


def _frame(payload: dict, envelope: str = "env-x") -> dict:
    return {"type": "events_api", "envelope_id": envelope, "payload": payload}


def phase_d() -> None:
    print("\n== D. Slack 方言：群/单聊与被 @ ==")
    ch = slack_channel.SlackSocketChannel()
    auth = [{"team_id": "T1", "user_id": BOT_USER_ID, "is_bot": True}]

    ev = ch.parse_event(_frame(event_payload("今天例会改到三点", channel="C0777", channel_type="channel",
                                             ts="1700000000.123456", authorizations=auth)))
    check("频道消息判成群聊", ev.kind == "inbound" and ev.is_group is True, str(ev)[:200])
    check("群里没 @ 机器人时答案是 False", ev.mentioned_me is False, str(ev.mentioned_me))
    check("ts 换成毫秒时间戳（上下文窗口按它排新旧）", ev.at_ms == 1700000000123, str(ev.at_ms))

    ev2 = ch.parse_event(_frame(event_payload(f"<@{BOT_USER_ID}> 看下这份纪要", channel="C0777",
                                             channel_type="channel", authorizations=auth)))
    check("文本里 @ 到机器人用户 ID 时答案是 True", ev2.mentioned_me is True and ev2.is_group is True, str(ev2.mentioned_me))
    ev3 = ch.parse_event(_frame(event_payload("<@U0OTHER> 你怎么看", channel="C0777",
                                             channel_type="channel", authorizations=auth)))
    check("@ 的是别人时答案是 False（按 authorizations 认人）", ev3.mentioned_me is False, str(ev3.mentioned_me))

    ev4 = ch.parse_event(_frame(event_payload("<@U0OTHER> 你怎么看", channel="C0777", channel_type="channel")))
    check(
        "信封没带 authorizations 且本地也没查过 auth.test 时答不出（None），不猜",
        ev4.mentioned_me is None,
        str(ev4.mentioned_me),
    )
    ch._bot_user_id = BOT_USER_ID
    ev5 = ch.parse_event(_frame(event_payload(f"随手 @ 一个 <@{BOT_USER_ID}> 的人", channel="C0777", channel_type="channel")))
    check("绑定过的机器人用户 ID 也能认（不再依赖信封）", ev5.mentioned_me is True, str(ev5.mentioned_me))
    ev6 = ch.parse_event(_frame(event_payload("谁都没 @", channel="C0777", channel_type="channel")))
    check("文本里没有任何 @ 时即使不认识机器人也判 False", ev6.mentioned_me is False, str(ev6.mentioned_me))

    ev7 = ch.parse_event(_frame(event_payload("单聊里的一句话", channel="D0123", channel_type="im")))
    check("channel_type=im 才是确定单聊", ev7.is_group is False, str(ev7.is_group))
    ev8 = ch.parse_event(_frame(event_payload("陌生频道形状", channel="X0777", channel_type="")))
    check("认不出的 channel_type 按群处理（策略只会更严不会更松）", ev8.is_group is True, str(ev8.is_group))
    ev9 = ch.parse_event(_frame(event_payload("频道里 @ 了别人，事件类型也是 app_mention", channel="C0777",
                                             channel_type="channel", event_type="app_mention")))
    check("app_mention 事件也进任务面（订阅了它的App只有这一种）", ev9.kind == "inbound" and ev9.mentioned_me is True, str(ev9)[:200])
    ev10 = ch.parse_event(_frame(event_payload("", channel="C0777", channel_type="channel", subtype="tombstone",
                                              authorizations=auth)))
    check("带 subtype 的编辑/删除类事件仍被忽略", ev10.kind == "ignore", str(ev10)[:120])
    check(
        "@ 里的显示名形式（<@U012AB3CD|someone>）也能认",
        ch._mentioned({"text": f"<@{BOT_USER_ID}|tt-bot>"}, {}) is True
        and ch._mentioned({"text": "<@U0ZZZ|other>"}, {}) is False,
    )
    check("ts 不是数字时按 0 处理而不是抛异常", slack_channel.SlackSocketChannel._at_ms({"ts": "abc"}) == 0)
    check(
        "群/@ 判定依据在能力位读数里说清（界面不用猜）",
        "channel_type" in ch.mention_note and "authorizations" in ch.mention_note,
        ch.mention_note[:200],
    )

    check("Slack 尚未接附件与输入状态：关闭原因是中文说明",
          ch.supports_media_in is False and ch.supports_typing is False and "尚未" in ch.media_in_note,
          str((ch.media_in_note, ch.typing_note))[:200])


def main() -> None:
    fake = FakeSlack()
    fake.start()
    try:
        phase_ab(fake)
        phase_c()
        phase_d()
    finally:
        fake.stop()
        asyncio.run(im_channels.shutdown())
    print("-" * 52)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine im slack selftest: ALL PASS")


if __name__ == "__main__":
    main()
