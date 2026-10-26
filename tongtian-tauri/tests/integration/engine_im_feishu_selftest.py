#!/usr/bin/env python3
"""
IM 频道自测 —— 飞书长连接（官方 lark-oapi SDK 的协议 + 框架共用连接循环）

分三段，A/B 全部打真实网络（对端是本机起的假飞书，不是 Mock 返回值）：

A. HTTP 契约面与「绑定时才加载」：引擎启动不碰官方 SDK（lark_oapi 有 20.3MB/万把文件，
   import 一次实测 8~33 秒，拖在启动上就是整个应用卡住），只有绑定那次才加载；
   清单里飞书 supported=True 且凭据字段是 App ID/App Secret，微信仍是 IM_UNSUPPORTED；
   App ID 前缀不对、Secret 太短、缺一项各报各的码，格式没过时一个 HTTP 请求都不发出去。
B. 长连接全链路：假飞书只做官方那几件事（发一次性连接地址与 ClientConfig、按 sum/seq
   推 protobuf 帧、心跳收 ping 回 pong、回贴走 Web API 并校验 Bearer token），验证
   「换地址 → 连 wss → 解码合包 → 逐帧回执（原帧 + biz_rt + code:200）→ 收消息起任务 →
   reply 回贴」，并检查同 event_id 重投、富文本、机器人自己发的、别的事件类型、card 帧
   都不起任务，@机器人占位符被剥掉，单聊按 chat_id 建会话。
C. 飞书方言分支（不联网，直接喂官方 protobuf 帧）：ClientConfig 只取保活间隔且脏值不生效、
   分片没收齐不算事件、过期分片重开、事件正文不是 JSON 如实报错、card 与控制帧一个字节都不
   回执而事件帧回写原帧、pong 的 payload 改保活间隔、SDK 没预热时同步路径不许偷偷加载、
   缺依赖时 IM_DEPENDENCY_MISSING、文本抽取遇到坏正文只取空文本、账号信息里是掩码。
C+. token 与回贴的真实 HTTP 分支（同一条循环里跑完）：token 被拒映射 IM_AUTH_FAILED 且报错
   不带 App Secret、token 中途失效换一个重发一次、正常回贴复用缓存 token、回贴非 0 报
   IM_REPLY_FAILED、没有可 reply 的消息改走按 chat_id 主动发。
   请求形状取自官方在 A/B 段验：AppID/AppSecret + locale + SDK UA 换地址，
   Authorization: Bearer <token> + SDK UA 回贴。

数据隔离：TT_DATA_HOME 与 WORK_DIR 都指向临时目录，绝不写 ~/.tongtianwork 的存量台账。
用法: python tests/integration/engine_im_feishu_selftest.py
"""

import asyncio
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TMP = Path(tempfile.mkdtemp(prefix="tt-im-feishu-"))
os.environ["TT_DATA_HOME"] = str(_TMP / "home")
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from importlib.util import find_spec

    from fastapi.testclient import TestClient

    import server as engine_server
    import feishu_channel
    import im_channels

    if find_spec("lark_oapi") is None:  # 依赖探针：find_spec 不执行包代码，所以不会自己把 SDK 拉进来
        raise ImportError("官方 lark-oapi 未安装")

    # 上面这些 import 就是「引擎启动」那一刻的样子：此刻绝不能已经加载过官方 SDK。
    # 假飞书自己要用官方 protobuf 编帧，所以它必须在这之后才 import。
    BOOT_HAS_LARK = "lark_oapi.ws" in sys.modules
    BOOT_SDK_CACHE = feishu_channel._SDK is not None
    from fake_feishu import (
        APP_ID,
        APP_SECRET,
        GROUP_CHAT_ID,
        P2P_CHAT_ID,
        SECOND_OPEN_ID,
        SENDER_OPEN_ID,
        FakeFeishu,
        control_frame,
        data_frame,
        message_event,
    )
except Exception as exc:  # 依赖缺失时优雅跳过（与其余引擎自测一致）
    print(f"SKIP: engine im feishu selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def wait_for(predicate, timeout=10.0, interval=0.05):
    """等条件成立（收帧、起任务、回贴是异步的，不能靠 sleep 猜时间）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def _channel_of(client, kind: str) -> dict:
    return next(c for c in client.get("/api/im/channels").json()["channels"] if c["kind"] == kind)


def _sessions_of(client) -> list[dict]:
    return client.get("/api/im/channels").json().get("sessions") or []


def _text_of_event(event: dict) -> str:
    return event.get("event", {}).get("message", {}).get("content", "")


# ── A/B 段：契约面 + 假飞书全链路 ─────────────────────────────────


def phase_ab(fake: FakeFeishu) -> None:
    print("\n== A. /api/im 契约面与「绑定时才加载官方 SDK」 ==")
    engine_server.WORK_DIR = _TMP / "work"
    engine_server.WORK_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["TT_FEISHU_DOMAIN"] = fake.domain
    # 官方默认 120 秒；自测压到 0.2 秒才能在几秒内看到应用层 ping 帧真的发出去
    os.environ["TT_FEISHU_PING_INTERVAL"] = "0.2"

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
        fs = channels["feishu"]
        check("飞书标记为已接入", fs["supported"] is True and fs["unsupportedReason"] == "", str(fs)[:200])
        check(
            "凭据字段取自 App ID/App Secret（不是回调地址模式的 Token）",
            [f["key"] for f in fs["fields"]] == ["appId", "appSecret"]
            and all(f["secret"] is True for f in fs["fields"])
            and "不回显" in fs["fields"][1]["note"],
            str(fs["fields"])[:260],
        )
        check(
            "「事件订阅要选长连接」与「首次连接要加载 SDK」都写在指引里",
            "长连接" in fs["setupNote"] and "加载官方 SDK" in fs["setupNote"],
            fs["setupNote"][:220],
        )
        wx = channels["wechat"]
        check(
            "只剩微信未接入且有可读原因",
            wx["supported"] is False and wx["unsupportedReason"] and not wx["fields"],
            str(wx)[:160],
        )
        check(
            "引擎启动时没有加载官方 SDK（绑定时才加载）",
            BOOT_HAS_LARK is False and BOOT_SDK_CACHE is False,
            f"lark_oapi.ws in modules={BOOT_HAS_LARK} cache={BOOT_SDK_CACHE}",
        )

        neg = client.post("/api/im/wechat/config", json={"credentials": {"appId": "x"}}).json()
        check("未接入通道拒绝写凭据（IM_UNSUPPORTED）", neg.get("code") == "IM_UNSUPPORTED", str(neg)[:160])
        start = client.post("/api/im/feishu/start").json()
        check("未配置就启动报 IM_NOT_CONFIGURED", start.get("code") == "IM_NOT_CONFIGURED", str(start)[:160])

        missing = client.post("/api/im/feishu/config", json={"credentials": {"appId": APP_ID}}).json()
        check("缺 App Secret 被拒绝", missing.get("code") == "IM_CREDENTIAL_MISSING", str(missing)[:160])
        bad_prefix = client.post(
            "/api/im/feishu/config", json={"credentials": {"appId": "cli", "appSecret": APP_SECRET}}
        ).json()
        check("App ID 不以 cli_ 开头报 IM_CREDENTIAL_INVALID", bad_prefix.get("code") == "IM_CREDENTIAL_INVALID",
              str(bad_prefix)[:160])
        short = client.post(
            "/api/im/feishu/config", json={"credentials": {"appId": APP_ID, "appSecret": "abcdefg"}}
        ).json()
        check("App Secret 太短报 IM_CREDENTIAL_INVALID", short.get("code") == "IM_CREDENTIAL_INVALID", str(short)[:160])
        check(
            "凭据格式没过时一个请求都不发（假飞书没被问过）",
            fake.endpoint_calls == [] and fake.sockets == 0 and fake.token_calls == 0,
            f"endpoints={len(fake.endpoint_calls)} sockets={fake.sockets} tokens={fake.token_calls}",
        )
        check(
            "格式校验阶段也没加载官方 SDK（慢 import 只为真请求买单）",
            feishu_channel._SDK is None,
            "SDK 已被提前加载",
        )

        fake.reject_endpoint = True
        wrong = client.post(
            "/api/im/feishu/config", json={"credentials": {"appId": APP_ID, "appSecret": APP_SECRET}}
        ).json()
        fake.reject_endpoint = False
        check("换连接地址被拒映射成 IM_AUTH_FAILED", wrong.get("code") == "IM_AUTH_FAILED", str(wrong)[:220])
        check("鉴权失败不留半套配置", _channel_of(client, "feishu")["configured"] is False)
        check(
            "鉴权失败不把 App Secret 写进错误详情",
            APP_SECRET not in json.dumps(wrong, ensure_ascii=False),
            str(wrong)[:200],
        )
        first_call = fake.endpoint_calls[0] if fake.endpoint_calls else {}
        check(
            "换地址请求形状取自官方：AppID/AppSecret + locale + SDK UA",
            sorted((first_call.get("body") or {}).keys()) == ["AppID", "AppSecret"]
            and first_call.get("locale") == "zh"
            and str(first_call.get("ua", "")).startswith("oapi-sdk-python/"),
            str(first_call)[:240],
        )
        check("鉴权失败后不留下运行中的任务", _channel_of(client, "feishu")["running"] is False)

        print("\n== B. 长连接全链路（对端为本机假飞书） ==")
        runtime = im_channels.runtime_of("feishu")
        check("启动时已注册真实任务处理方", runtime.handler is engine_server._im_run_task)

        async def stub_handler(text: str, session: dict) -> str:
            # 真实处理方（IM→Agent）由 slack 自测的 C 段覆盖；这里只验框架接线
            return f"已收到：{text}"

        runtime.set_handler(stub_handler)
        fake.script_on = fake.sockets + 1
        ok = client.post(
            "/api/im/feishu/config", json={"credentials": {"appId": APP_ID, "appSecret": APP_SECRET}}
        ).json()
        account = ok.get("account") or {}
        check("绑定成功返回真实账号事实", ok.get("ok") is True and account.get("apiBase") == fake.domain, str(ok)[:220])
        check(
            "账号信息只给掩码 App ID 与协议事实",
            account.get("appId") != APP_ID
            and "…" in account.get("appId", "")
            and account.get("subscribeEvent") == "im.message.receive_v1"
            and account.get("pingIntervalSeconds") == 1,
            str(account)[:220],
        )
        check(
            "绑定这一次才真正加载官方 SDK",
            feishu_channel._SDK is not None and "lark_oapi.ws" in sys.modules,
            "绑定后仍未加载 SDK",
        )
        # configure() 里的 start() 只是把监督任务排上队，握手在下一拍才发生，所以要等而不是当场读
        connected_first = wait_for(lambda: fake.sockets if fake.sockets >= 1 else None)
        check("长连接真的建起来了（不是打桩返回值）", bool(connected_first) and len(fake.endpoint_calls) >= 2,
              f"sockets={fake.sockets} endpoints={len(fake.endpoint_calls)}")

        state = wait_for(lambda: "connected" if _channel_of(client, "feishu")["state"] == "connected" else None)
        check("长连接进入 connected", state == "connected", str(state))

        got = wait_for(lambda: fake.replies if len(fake.replies) >= 2 else None)
        texts = sorted(r["text"] for r in got or [])
        check(
            "群聊与单聊各起一个任务，@机器人占位符已剥掉",
            got is not None
            and texts == sorted(["已收到：帮我把季度总结整理成一页", "已收到：把这份清单按人分一下"]),
            f"texts={texts} 最近错误={_channel_of(client, 'feishu')['lastError']}",
        )
        check(
            "回贴走 reply 接口并带上回调里那条消息的 id",
            got is not None
            and all(r["msg_type"] == "text" and r["message_id"].startswith("om_ttfake_") for r in got),
            str([r["message_id"] for r in got or []])[:200],
        )
        check(
            "回贴带头取自官方 transport：Authorization: Bearer <token> 与 SDK UA",
            got is not None and all(r["auth"].startswith("Bearer ") and r["ua"].startswith("oapi-sdk-python/")
                                    for r in got),
            str([(r["auth"], r["ua"]) for r in got or []][:1])[:220],
        )
        check(
            "有可 reply 的消息时不会退到主动发接口",
            fake.sends == [],
            str(fake.sends)[:200],
        )
        acked = wait_for(lambda: fake.acks if len(fake.acks) >= 6 else None)
        check(
            "六个事件帧都按官方形状回执：原帧 id + code:200 + biz_rt",
            acked is not None
            and len(acked) == 6
            and all(a["code"] == 200 and a["message_id"].startswith("ftf-") and a["biz_rt"] != "" for a in acked)
            and {a["type"] for a in acked} == {"event"},
            str(acked)[:300],
        )
        check(
            "card 帧与控制帧不回执、引擎没发明协议外的帧",
            all(a["message_id"] != "ftf-card" for a in fake.acks) and fake.unexpected == [],
            str(fake.unexpected)[:260],
        )

        sessions = {s["id"]: s for s in _sessions_of(client)}
        group_id = f"im-feishu-{GROUP_CHAT_ID}"
        p2p_id = f"im-feishu-{P2P_CHAT_ID}"
        check(
            "群聊与单聊各建一个会话（按 chat_id）",
            group_id in sessions and p2p_id in sessions,
            str(sorted(sessions))[:260],
        )
        group_msgs = [m for m in (sessions.get(group_id, {}).get("messages") or []) if m["direction"] == "in"]
        check(
            "同一 event_id 重投只起一次任务",
            len(group_msgs) == 1 and group_msgs[0]["text"] == "帮我把季度总结整理成一页",
            str(group_msgs)[:220],
        )
        p2p_msgs = [m for m in (sessions.get(p2p_id, {}).get("messages") or []) if m["direction"] == "in"]
        check(
            "单聊会话里的发起来源记的是这个 open_id 的会话",
            len(p2p_msgs) == 1 and p2p_msgs[0]["text"] == "把这份清单按人分一下",
            str(p2p_msgs)[:220],
        )
        feishu_sessions = [s for s in sessions.values() if s["source"] == "feishu"]
        check(
            "只这两条事件建了会话，其余（富文本/机器人自发/别的事件类型）一条都没进",
            len(feishu_sessions) == 2 and all(len(s.get("messages") or []) == 2 for s in feishu_sessions),
            str({k: len(v.get("messages") or []) for k, v in sessions.items()})[:220],
        )
        check(
            "台账与错误详情里都不出现 App Secret",
            APP_SECRET not in json.dumps(list(sessions.values()), ensure_ascii=False)
            and APP_SECRET not in json.dumps(_channel_of(client, "feishu"), ensure_ascii=False),
        )

        pinged = wait_for(lambda: fake.pings if fake.pings >= 2 else None)
        check("应用层 ping 帧按间隔持续发出去", bool(pinged), f"pings={fake.pings}")
        check(
            "心跳帧回填了换地址时拿到的 service_id（SDK 就是这么带的）",
            set(fake.ping_services) == {7},
            str(sorted(set(fake.ping_services)))[:120],
        )
        balanced = wait_for(lambda: (fake.pings, fake.pongs_sent) if fake.pongs_sent == fake.pings else None)
        check("每条 ping 都收到一个 pong（服务端不回时引擎也不会误判断开）", balanced is not None,
              f"pings={fake.pings} pongs={fake.pongs_sent}")
        check("心跳期间连接没有被误判断开", _channel_of(client, "feishu")["state"] == "connected",
              str(_channel_of(client, "feishu")["lastError"])[:220])

        stopped = client.post("/api/im/feishu/stop").json()
        check("停止连接如实回 stopped", stopped.get("state") == "stopped", str(stopped)[:120])
        check("停止后带内保活任务已撤掉", runtime.channel._ping_task is None, str(runtime.channel._ping_task))
        client.post("/api/im/feishu/start")
        again = wait_for(lambda: "connected" if _channel_of(client, "feishu")["state"] == "connected" else None)
        check("重新启动会再换一次连接地址并连上", again == "connected" and fake.sockets >= 2,
              f"sockets={fake.sockets}")

        print("\n== B+. 分片下发（sum>1）与合包 ==")
        fake.split_event = True
        pushed_before = len(fake.acks)
        inj = _fake_post(fake, "/inject", {"text": "分两片发过来的消息"})
        check("注入一条分片消息", inj.get("ok") is True, str(inj)[:160])
        got2 = wait_for(lambda: fake.replies if len(fake.replies) >= 3 else None)
        check(
            "两片合起来才是一条事件，回执只发一次",
            got2 is not None and "已收到：分两片发过来的消息" in [r["text"] for r in got2]
            and len(fake.acks) == pushed_before + 1,
            f"acks={len(fake.acks)} replies={[r['text'] for r in got2 or []]}",
        )
        fake.split_event = False

        client.post("/api/im/feishu/forget")
        check("解绑后回到未配置", _channel_of(client, "feishu")["state"] == "not_configured")


def _fake_post(fake: FakeFeishu, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{fake.port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── C 段：飞书方言分支（直接对着通道对象打） ──────────────────────


def phase_c() -> None:
    print("\n== C. 飞书方言分支 ==")
    ch = feishu_channel.FeishuChannel()
    sdk = feishu_channel._SDK or asyncio.run(feishu_channel.load_sdk())
    const = sdk["const"]

    check(
        "帧形状与端点常量确实取自官方 SDK 而不是自己拼的",
        const.GEN_ENDPOINT_URI == "/callback/ws/endpoint"
        and const.HEADER_BIZ_RT == "biz_rt"
        and sdk["FrameType"].CONTROL.value == 0
        and sdk["MessageType"].EVENT.value == "event",
        str((const.GEN_ENDPOINT_URI, const.HEADER_BIZ_RT))[:160],
    )

    # 1) ClientConfig：只取保活间隔，脏值不生效
    ch._ping_interval = 120.0
    ch._configure({"PingInterval": 30})
    check("服务端下发的 PingInterval 生效", ch._ping_interval == 30.0, str(ch._ping_interval))
    ch._configure({"PingInterval": "abc"})
    check("PingInterval 是脏值时保持原值", ch._ping_interval == 30.0, str(ch._ping_interval))
    ch._configure({"PingInterval": 0})
    check("PingInterval=0 不当成有效间隔", ch._ping_interval == 30.0, str(ch._ping_interval))
    ch._configure({"ReconnectInterval": 5})
    check("重连相关字段不越权改动框架退避", ch._ping_interval == 30.0)

    # 2) 合包：没收齐不算事件，收齐才拼回原样
    event = message_event("分片也要能拼回去", event_id="evt-c-1", message_id="om_c_1")
    pieces = [
        data_frame(json.dumps(event, ensure_ascii=False)[:60], message_id="m-c-1", seq=0, total=3),
        data_frame(json.dumps(event, ensure_ascii=False)[60:120], message_id="m-c-1", seq=1, total=3),
        data_frame(json.dumps(event, ensure_ascii=False)[120:], message_id="m-c-1", seq=2, total=3),
    ]
    check("第 1 片只占位，不成事件", ch.decode_frame(pieces[0]) is None)
    check("第 2 片仍不成事件", ch.decode_frame(pieces[1]) is None)
    merged = ch.decode_frame(pieces[2])
    check("第 3 片到齐后拼回完整正文", merged is not None and bytes(merged["payload"]) ==
          json.dumps(event, ensure_ascii=False).encode("utf-8"), str(merged)[:160])
    check("合包缓冲已清空", ch._fragments == {}, str(ch._fragments)[:120])
    ch._fragments["stale"] = ({0: b"part"}, 2, time.time() - 1)
    ch.decode_frame(pieces[0])
    check("过期分片重开而不是接在旧消息后面", len(ch._fragments) == 1 and list(ch._fragments) == ["m-c-1"],
          str(list(ch._fragments))[:160])

    # 3) 控制帧与 card 帧：不回执，pong 的 payload 改保活间隔
    control = ch.decode_frame(control_frame("pong", {"PingInterval": 7}))
    check("pong 解成控制帧而不是事件", control["kind"] == "control" and control["type"] == "pong", str(control)[:160])
    ch._on_control_frame(control)
    check("pong 带来的 ClientConfig 改了保活间隔", ch._ping_interval == 7.0, str(ch._ping_interval))
    ch._on_control_frame(ch.decode_frame(control_frame("ping")))
    check("服务端 ping 不需要回执也不改配置", ch._ping_interval == 7.0)

    class _Socket:
        """替身 socket：只记账发了几个字节，用来证明「没发」而不是猜"""

        def __init__(self):
            self.buffers: list[bytes] = []

        async def send_bytes(self, raw: bytes) -> None:
            self.buffers.append(raw)

    async def _ack(frame):
        sock = _Socket()
        await ch.ack(sock, frame, im_channels.InboundEvent(kind="ignore"))
        return sock

    sock = asyncio.run(_ack(ch.decode_frame(control_frame("pong", {"PingInterval": 7}))))
    check("控制帧的回执真的一个字节都没发（SDK 同样不发）", sock.buffers == [], str(sock.buffers)[:120])
    sock = asyncio.run(_ack(ch.decode_frame(data_frame({"card": "x"}, message_id="ftf-card", type_name="card"))))
    check("card 帧的回执真的一个字节都没发", sock.buffers == [], str(sock.buffers)[:120])

    dup_event = message_event("只该起一次", event_id="evt-dup", message_id="m-dup")
    event_frame = ch.decode_frame(data_frame(dup_event, message_id="ftf-evt-c-ack"))
    sock = asyncio.run(_ack(event_frame))
    sent = sdk["Frame"]()
    sent.ParseFromString(sock.buffers[0])
    sent_headers = {h.key: h.value for h in sent.headers}
    check(
        "事件帧的回执就是原帧回写，只换 payload 成 {\"code\": 200} 并追加 biz_rt",
        len(sock.buffers) == 1
        and sent.method == sdk["FrameType"].DATA.value
        and json.loads(bytes(sent.payload).decode("utf-8")) == {"code": 200}
        and sent_headers.get("biz_rt", "").isdigit()
        and sent_headers.get("message_id") == "ftf-evt-c-ack"
        and sent_headers.get("type") == "event",
        str((sent_headers, bytes(sent.payload)))[:220],
    )

    # 4) 事件正文不是 JSON：如实报错而不是当没看见
    broken = ch.decode_frame(data_frame("{不是 JSON", message_id="m-broken"))
    try:
        ch.parse_event(broken)
        check("事件正文不是 JSON 会抛 IM_BAD_RESPONSE", False, "没抛")
    except im_channels.ImError as exc:
        check("事件正文不是 JSON 会抛 IM_BAD_RESPONSE", exc.code == "IM_BAD_RESPONSE", str(exc)[:160])

    # 5) 忽略项：非消息事件、非文本、机器人自发、空文本
    for name, event_body in [
        ("别的事件类型", {"schema": "2.0", "header": {"event_id": "x1", "event_type": "im.chat.updated"}, "event": {}}),
        ("富文本消息", message_event("", event_id="x2", message_id="m2", message_type="post", content={"post": {}})),
        ("机器人自己发的", message_event("hi", event_id="x3", message_id="m3", sender_type="app")),
        ("空文本", message_event("   ", event_id="x4", message_id="m4")),
        ("没有 chat_id", message_event("有文本", event_id="x5", message_id="m5", chat_id="")),
    ]:
        ev = ch.parse_event(ch.decode_frame(data_frame(event_body, message_id=f"m-{name}")))
        check(f"{name} 如实忽略，不进任务", ev.kind == "ignore", str(ev)[:160])

    # 6) 同一条事件重投：第二次必须是 ignore（否则一条消息起两个任务）
    first = ch.parse_event(ch.decode_frame(data_frame(dup_event, message_id="ftf-evt-dup")))
    second = ch.parse_event(ch.decode_frame(data_frame(dup_event, message_id="ftf-evt-dup")))
    check("重投的同 event_id 事件只当一次", first.kind == "inbound" and second.kind == "ignore",
          f"{first.kind}/{second.kind}")
    check("事件里记的是正文那条 IM 消息的 id，回贴要 reply 它", first.message_id == "m-dup", str(first)[:200])

    # 7) 同步路径不许偷偷触发慢 import
    cached = feishu_channel._SDK
    feishu_channel._SDK = None
    try:
        ch.decode_frame(pieces[0])
        check("SDK 没预热时 decode_frame 不自己加载", False, "居然加载了")
    except im_channels.ImError as exc:
        check("SDK 没预热时 decode_frame 不自己加载", exc.code == "IM_CHANNEL_ERROR", str(exc)[:160])
    finally:
        feishu_channel._SDK = cached

    # 8) 缺依赖：原因说清缺的是哪个包，绑定第一步就如实报 IM_DEPENDENCY_MISSING
    #    （supported 是引擎启动那一刻 find_spec 的结论，运行中改全局变量不会回头改它，
    #     所以这里只验原因文案与绑定分支，不假装能热切换 supported）
    original_present = feishu_channel.SDK_PRESENT
    feishu_channel.SDK_PRESENT = False
    feishu_channel._SDK = None
    try:
        probe = feishu_channel.FeishuChannel()
        check("探测不到官方 SDK 时原因点名 lark_oapi", "lark_oapi" in probe.unsupported_reason,
              probe.unsupported_reason[:200])
        try:
            asyncio.run(probe.verify({"appId": APP_ID, "appSecret": APP_SECRET}))
            check("缺依赖时绑定报 IM_DEPENDENCY_MISSING", False, "没抛")
        except im_channels.ImError as exc:
            check("缺依赖时绑定报 IM_DEPENDENCY_MISSING", exc.code == "IM_DEPENDENCY_MISSING" and exc.retryable is False,
                  str(exc)[:160])
        check(
            "缺依赖的报错先于任何网络往返（不转完圈才说没装）",
            probe.runtime is None,  # 这个通道实例没有 runtime：真发出过请求就会是 AttributeError 而不是 ImError
            str(probe.runtime),
        )
    finally:
        feishu_channel.SDK_PRESENT = original_present
        feishu_channel._SDK = cached
    check("本机 SDK 可用（所以通道 supported=True）", feishu_channel.FeishuChannel.supported is True)

    # 10) 文本抽取：正文形状不对只能取空文本，不能抛出去把收帧循环打死
    def msg(content, mentions=None):
        return SimpleNamespace(content=content, mentions=mentions or [])

    check("正常文本抽出来", ch._text_of(msg(json.dumps({"text": "你好"}, ensure_ascii=False))) == "你好")
    check("content 不是 JSON 时取到空文本而不是崩", ch._text_of(msg("{坏掉的 JSON")) == "")
    check("content 是 JSON 但没有 text 字段时取到空文本", ch._text_of(msg('{"post": {}}')) == "")
    check("content 直接给对象（不是 JSON 字符串）时也能取到文本", ch._text_of(msg({"text": " 带空格的 "})) == "带空格的")
    check("@机器人占位符按 mentions 剥掉",
          ch._text_of(msg(json.dumps({"text": "@_user_1 把这份清单按人分一下"}, ensure_ascii=False),
                         [SimpleNamespace(key="@_user_1")])) == "把这份清单按人分一下")
    check("mentions 里没有 key 时不误伤正文",
          ch._text_of(msg(json.dumps({"text": "正常一句"}, ensure_ascii=False), [SimpleNamespace(key="")])) == "正常一句")

    # 11) 掩码：账号信息里不能出现完整 App ID / Secret
    masked = feishu_channel._mask(APP_ID)
    check("掩码后仍能分辨绑的是哪个应用", masked != APP_ID and masked.startswith("cli_") and masked.endswith("01"),
          masked)


def phase_c_live() -> None:
    """token 与回贴的分支全都要发真 HTTP，所以挤在同一条事件循环里一次跑完。

    aiohttp 的 ClientSession 绑在创建它的那条循环上，而 phase_ab 的 TestClient 一退出
    那条循环就关掉了；每个分支各开一个 asyncio.run() 就会在复用旧会话时撞
    「Event loop is closed」——那是测试自己的循环用法，不是通道的问题。
    """
    print("\n== C+. token 与回贴的真实 HTTP 分支 ==")
    fake = FakeFeishu(script_on=10**9)
    fake.start()
    try:
        os.environ["TT_FEISHU_DOMAIN"] = fake.domain
        runtime = im_channels.runtime_of("feishu")
        ch_live = runtime.channel
        # 上一条循环留下的会话不能用：置空让 http 属性在这条新循环里重建
        runtime._http = None
        im_channels.save_config("feishu", {"appId": APP_ID, "appSecret": APP_SECRET})

        async def body() -> None:
            creds = {"appId": APP_ID, "appSecret": APP_SECRET}
            try:
                # 9) token 分支：被拒 → IM_AUTH_FAILED；中途失效 → 换一个重发一次
                fake.token_reject = True
                try:
                    await ch_live.verify(creds)
                    check("token 被拒映射成 IM_AUTH_FAILED", False, "没抛")
                    auth_exc = None
                except im_channels.ImError as exc:
                    auth_exc = exc
                check("token 被拒映射成 IM_AUTH_FAILED",
                      auth_exc is not None and auth_exc.code == "IM_AUTH_FAILED" and auth_exc.retryable is False,
                      str(auth_exc)[:200])
                check("token 被拒的报错里不出现 App Secret",
                      auth_exc is not None and APP_SECRET not in f"{auth_exc.code} {auth_exc.detail}",
                      str(auth_exc)[:200])
                fake.token_reject = False

                ch_live._token = ""
                ch_live._reply_targets[GROUP_CHAT_ID] = "om_ttfake_seed"
                # 先正常发一条，让通道手里真的握着一个「看起来仍有效」的缓存 token：
                # 否则下面那次取 token 是首次取，跟「失效后重发」这个分支混在一起就测不准了
                await ch_live.send(creds, GROUP_CHAT_ID, "先正常发一条")
                cached_before = fake.token_calls
                fake.bad_token_once = True
                await ch_live.send(creds, GROUP_CHAT_ID, "换 token 再发一次")
                check(
                    "token 被认失效时换一个并重发一次（不是静默失败）",
                    len(fake.replies) == 2 and fake.replies[1]["text"] == "换 token 再发一次"
                    and fake.token_calls == cached_before + 1,
                    f"replies={str([r['text'] for r in fake.replies])[:160]} "
                    f"tokens={fake.token_calls}(前值 {cached_before})",
                )
                tokens_before = fake.token_calls
                await ch_live.send(creds, GROUP_CHAT_ID, "第二次用缓存 token")
                check("正常回贴复用缓存 token，不再取一次", fake.token_calls == tokens_before,
                      f"tokens={fake.token_calls}")
                check("回贴用的是 reply 接口（带原消息 id）", fake.replies[-1]["message_id"] == "om_ttfake_seed",
                      str(fake.replies[-1])[:160])

                unknown_conv = "oc_ttfake_never_seen"
                await ch_live.send(creds, unknown_conv, "没有可回的消息就走主动发")
                check(
                    "没有 reply 目标时按 chat_id 主动发",
                    len(fake.sends) == 1 and fake.sends[0]["receive_id"] == unknown_conv
                    and fake.sends[0]["text"] == "没有可回的消息就走主动发",
                    str(fake.sends)[:200],
                )

                fake.reply_errcode = 40001
                try:
                    await ch_live.send(creds, GROUP_CHAT_ID, "会被拒的一条")
                    check("飞书返回非 0 时如实报 IM_REPLY_FAILED", False, "没抛")
                except im_channels.ImError as exc:
                    check("飞书返回非 0 时如实报 IM_REPLY_FAILED",
                          exc.code == "IM_REPLY_FAILED" and "40001" in exc.detail, str(exc)[:200])
                fake.reply_errcode = 0
            finally:
                if runtime._http and not runtime._http.closed:
                    await runtime._http.close()

        asyncio.run(body())
    finally:
        im_channels.drop_config("feishu")
        fake.stop()


def main() -> None:
    fake = FakeFeishu(script_on=10**9)  # 剧本先关着：等知道哪条连接是引擎真正那条再打开
    fake.start()
    try:
        phase_ab(fake)
        phase_c()
        phase_c_live()
    finally:
        fake.stop()
        asyncio.run(im_channels.shutdown())
    print("-" * 52)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine im feishu selftest: ALL PASS")


if __name__ == "__main__":
    main()
