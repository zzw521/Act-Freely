#!/usr/bin/env python3
"""
IM 频道自测 —— 腾讯元宝（官方 sign-token + WebSocket + protobuf 协议）

分三段，A/B 全部打真实网络（对端是本机起的假元宝网关，不是 Mock 返回值）：

A. 凭据与签名：/api/im/channels 里元宝来自引擎注册表且要 AppID + AppSecret；手填时缺项/
   带空格各报各的码；错误 AppSecret 被 IM_AUTH_FAILED 拒绝且不落盘；假网关独立复算
   HMAC-SHA256(nonce+timestamp+appKey+appSecret) 并校验四个请求头，通过才发令牌；
   code=10099 自动重试三次。
B. 长连接全链路：auth-bind 帧按官方描述符（bizId=ybBot、authInfo.uid/source/token、
   deviceInfo.instanceId=16）；应用层 ping 有回执；文本单聊起任务，重复 msgid/机器人回声/
   图片/空文本都不起；群内那条在默认「被 @ 时才回应」下被挡下（元宝协议没有任何 @ 字段，
   答不出就不猜）并如实写原因，把该群策略改成「每条都处理」后同一条通道立刻起任务；
   回贴按会话选 send_c2c_message / send_group_message 并按 msgId 配对；
   推送 needAck 时回 cmdType=3 的 ACK；被顶掉后不再重连。
C. 协议分支：auth 响应码 0/41101/41103/50400/其它分别映射；canonical 字节 fixture 校验
   protobuf 字段号；群/单聊与 @ 判定的证据来源与「答不出」口径（mention_note）；msgTime
   秒→毫秒；非文本/回声/重复消息不进任务；无连接时回贴如实失败；回贴被拒映射
   IM_REPLY_FAILED；kickout 帧不可重试；默认基址与心跳间隔的环境变量口子。

数据隔离：TT_DATA_HOME 与 WORK_DIR 都指向临时目录，绝不写 ~/.tongtianwork 的存量台账。
用法: python tests/integration/engine_im_yuanbao_selftest.py
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

_TMP = Path(tempfile.mkdtemp(prefix="tt-im-yuanbao-"))
os.environ["TT_DATA_HOME"] = str(_TMP / "home")
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import aiohttp
    from fastapi.testclient import TestClient
    from fake_yuanbao import (
        DEFAULT_APP_KEY,
        DEFAULT_APP_SECRET,
        DEFAULT_BOT_ID,
        DEFAULT_BOT_TOKEN,
        DEFAULT_USER,
        GROUP_CODE,
        SECOND_USER,
        FakeYuanbao,
        inbound_push,
    )
    import server as engine_server
    import im_channels
    import yuanbao_channel as yb
except Exception as exc:  # 依赖缺失时优雅跳过（与其余引擎自测一致）
    print(f"SKIP: engine im yuanbao selftest unavailable ({exc})")
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


def _group_session(client, *, min_messages: int = 1) -> dict | None:
    """群会话（会话号 = groupCode）；`min_messages` 用来等台账真的写完再断言"""
    for s in _sessions_of(client):
        if s["source"] == "yuanbao" and s["id"] == f"im-yuanbao-{GROUP_CODE}":
            return s if len(s.get("messages") or []) >= min_messages else None
    return None


class _HttpRuntime:
    def __init__(self, session):
        self.http = session


def _creds(secret: str = DEFAULT_APP_SECRET) -> dict:
    return {"appKey": DEFAULT_APP_KEY, "appSecret": secret}


def _error(fn):
    try:
        fn()
    except im_channels.ImError as exc:
        return exc.code, exc.detail, exc.retryable
    return "", "", True


# ── A/B 段：凭据、签名、连接全链路 ────────────────────────────────


def phase_ab(fake: FakeYuanbao) -> None:
    print("\n== A. 凭据与签名（对端为本机假元宝网关） ==")
    engine_server.WORK_DIR = _TMP / "work"
    engine_server.WORK_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["TT_YUANBAO_API_BASE"] = fake.api_base
    os.environ["TT_YUANBAO_WS_URL"] = fake.ws_url
    os.environ["TT_YUANBAO_HEARTBEAT_SECONDS"] = "0.3"

    with TestClient(engine_server.app) as client:
        channels = {c["kind"]: c for c in client.get("/api/im/channels").json()["channels"]}
        ybc = channels["yuanbao"]
        check("元宝由引擎注册且标记为已接入", ybc["supported"] is True, str(ybc)[:200])
        check(
            "凭据字段是 AppID + AppSecret，且都按机密处理",
            [f["key"] for f in ybc["fields"]] == ["appKey", "appSecret"]
            and all(f["secret"] is True for f in ybc["fields"])
            and "不回显" in ybc["fields"][1]["note"],
            str(ybc["fields"])[:260],
        )
        check(
            "指引说清一条机器人只允许一条有效连接",
            "一条有效连接" in ybc["setupNote"],
            ybc["setupNote"][:220],
        )

        missing = client.post("/api/im/yuanbao/config", json={"credentials": {"appKey": DEFAULT_APP_KEY}}).json()
        check("缺 AppSecret 报 IM_CREDENTIAL_MISSING", missing.get("code") == "IM_CREDENTIAL_MISSING", str(missing)[:200])
        spaced = client.post(
            "/api/im/yuanbao/config",
            json={"credentials": {"appKey": DEFAULT_APP_KEY, "appSecret": "ttfakeybsecret 0123456789abcdef00"}},
        ).json()
        check("带空格报 IM_CREDENTIAL_INVALID", spaced.get("code") == "IM_CREDENTIAL_INVALID", str(spaced)[:200])
        check("格式没过时一条签名请求都不发", fake.sign_requests == [], str(fake.sign_requests)[:160])

        wrong = client.post(
            "/api/im/yuanbao/config",
            json={"credentials": {"appKey": DEFAULT_APP_KEY, "appSecret": DEFAULT_APP_SECRET[:-4] + "beef"}},
        ).json()
        check("错误 AppSecret 被拒绝（IM_AUTH_FAILED）", wrong.get("code") == "IM_AUTH_FAILED", str(wrong)[:220])
        check("错误 AppSecret 不出现在错误详情里", DEFAULT_APP_SECRET not in json.dumps(wrong, ensure_ascii=False), str(wrong)[:200])
        check("鉴权失败不留半套配置", _channel_of(client, "yuanbao")["configured"] is False)
        check(
            "假网关独立复算签名，不匹配的请求都被记账",
            len(fake.sign_bad) == 1,
            f"bad={len(fake.sign_bad)} all={len(fake.sign_requests)}",
        )
        first = fake.sign_requests[0]
        check(
            "签名请求形状取自官方：app_key + hex32 nonce + 北京时间 + hex64 签名",
            first.get("app_key") == DEFAULT_APP_KEY
            and len(str(first.get("nonce") or "")) == 32
            and str(first.get("timestamp") or "").endswith("+08:00")
            and len(str(first.get("signature") or "")) == 64,
            str(first)[:260],
        )
        check(
            "请求头带 AppVersion / OperationSystem / Instance-Id=16 / Bot-Version",
            fake.sign_headers.get("X-Instance-Id") == "16"
            and fake.sign_headers.get("X-AppVersion") == yb.APP_VERSION
            and fake.sign_headers.get("X-OperationSystem") not in (None, "")
            and fake.sign_headers.get("X-Bot-Version") == yb.BOT_VERSION,
            str(fake.sign_headers)[:280],
        )

        # 10099 自动重试
        before = len(fake.sign_requests)
        fake.sign_retry_times = 2
        retried = client.post(
            "/api/im/yuanbao/config",
            json={"credentials": {"appKey": DEFAULT_APP_KEY, "appSecret": DEFAULT_APP_SECRET}},
        ).json()
        check("10099 自动重试后成功签发", retried.get("ok") is True, str(retried)[:200])
        check("10099 一共重试三次", len(fake.sign_requests) - before == 3, str(len(fake.sign_requests) - before))
        account = retried.get("account") or {}
        check(
            "账号信息只给掩码 botId 与协议事实",
            "…" in account.get("botId", "")
            and account.get("botId") != DEFAULT_BOT_ID
            and account.get("wsUrl") == fake.ws_url
            and account.get("apiBase") == fake.api_base
            and account.get("tokenSource") == "bot",
            str(account)[:240],
        )

        cfg = _TMP / "home" / "im_channels.json"
        stored = json.loads(cfg.read_text(encoding="utf-8"))["yuanbao"] if cfg.exists() else {}
        check(
            "凭据只落盘 appKey 与 appSecret",
            set(stored) == {"appKey", "appSecret"} and stored.get("appKey") == DEFAULT_APP_KEY,
            str(sorted(stored)),
        )

        print("\n== B. 长连接全链路（auth-bind → 推送 → 回贴） ==")
        runtime = im_channels.runtime_of("yuanbao")
        check("启动时已注册真实任务处理方", runtime.handler is engine_server._im_run_task)

        async def stub_handler(text: str, session: dict) -> str:
            return f"已收到：{text}"

        runtime.set_handler(stub_handler)

        state = wait_for(lambda: "connected" if _channel_of(client, "yuanbao")["state"] == "connected" else None)
        check("WebSocket 鉴权后进入 connected", state == "connected", str(state))
        check("确实建立了到假网关的连接", fake.sockets >= 1 and len(fake.auth_frames) >= 1, f"sockets={fake.sockets} auth={len(fake.auth_frames)}")
        bind = (fake.auth_frames[0].get("bind") or {}) if fake.auth_frames else {}
        check(
            "auth-bind 帧按官方描述符：bizId=ybBot + uid/source/token + instanceId=16",
            bind.get("bizId") == "ybBot"
            and (bind.get("authInfo") or {}).get("uid") == DEFAULT_BOT_ID
            and (bind.get("authInfo") or {}).get("source") == "bot"
            and (bind.get("authInfo") or {}).get("token") == DEFAULT_BOT_TOKEN
            and (bind.get("deviceInfo") or {}).get("instanceId") == "16",
            str(bind)[:300],
        )

        def push_now(payloads: list[bytes]) -> None:
            """从测试线程把推送排到假网关自己的事件循环上（连接已经建立）"""
            async def _send():
                deadline = time.time() + 5
                while not fake.live() and time.time() < deadline:
                    await asyncio.sleep(0.05)
                if not fake.live():
                    view = _channel_of(client, "yuanbao")
                    raise RuntimeError(
                        f"no live socket: sockets={fake.sockets} state={view['state']} err={view.get('lastError')}"
                    )
                ws = fake.live()[-1]
                for payload in payloads:
                    await ws.send_bytes(fake.push_frame(payload))

            asyncio.run_coroutine_threadsafe(_send(), fake._loop).result(timeout=10)

        push_now(
            [
                inbound_push("帮我把合同要点列出来", msg_id="yb-msg-1"),
                inbound_push("帮我把合同要点列出来", msg_id="yb-msg-1"),
                inbound_push("机器人自己的回声", msg_id="yb-msg-2", from_account=DEFAULT_BOT_ID),
                inbound_push("图片", msg_id="yb-msg-3", msg_type="TIMImageElem"),
                inbound_push("在群里汇总一下", msg_id="yb-msg-4", group_code=GROUP_CODE, from_account=SECOND_USER, callback="Group.CallbackAfterSendMsg"),
                inbound_push(" ", msg_id="yb-msg-5"),
            ]
        )

        sends = wait_for(lambda: fake.c2c if fake.c2c else None)
        check(
            "单聊起一个任务，重复 msgid/机器人回声/图片/空文本都不起",
            sends is not None and len(fake.c2c) == 1,
            f"c2c={len(fake.c2c)}",
        )
        if fake.c2c:
            c2c = fake.c2c[0]
            check(
                "单聊回贴走 send_c2c_message，toAccount 取 fromAccount",
                c2c["head"].get("cmd") == "send_c2c_message"
                and c2c["head"].get("module") == "yuanbao_openclaw_proxy"
                and (c2c["body"] or {}).get("toAccount") == DEFAULT_USER
                and (((c2c["body"] or {}).get("msgBody") or [{}])[0].get("msgType")) == "TIMTextElem"
                and (((c2c["body"] or {}).get("msgBody") or [{}])[0].get("msgContent") or {}).get("text") == "已收到：帮我把合同要点列出来",
                str(c2c)[:320],
            )

        # 群里那条：元宝的 InboundMessagePush 没有任何 @ 字段，答不出「这条是不是对着
        # 机器人说的」。「被 @ 时才回应」的策略下不能凭空判定 —— 挡下、写清原因、不回贴。
        grp = wait_for(lambda: _group_session(client))
        check(
            "群里那条以中文原因落台账：答不出 @ 名单就不起任务",
            grp is not None
            and len(grp["messages"]) == 1
            and grp["messages"][0]["direction"] == "in"
            and "未起任务" in grp["messages"][0]["text"]
            and "答不出" in grp["messages"][0]["text"],
            str(grp)[:280],
        )
        check("策略挡下的群消息不回贴：一个 send_group_message 也没发", not fake.groups, f"groups={len(fake.groups)}")

        # 把该群的群内触发改成「每条都处理」：保存即生效，不重启连接、不重绑凭据
        pol = client.post(
            "/api/im/policies", json={"scope": GROUP_CODE, "patch": {"groupActivation": "always"}}
        ).json()
        check(
            "按会话作用域存策略并立刻算出生效值",
            pol.get("ok") is True and (pol.get("policy") or {}).get("groupActivation") == "always",
            str(pol)[:200],
        )
        push_now(
            [
                inbound_push(
                    "在群里汇总一下（策略已放开）",
                    msg_id="yb-msg-7",
                    group_code=GROUP_CODE,
                    from_account=SECOND_USER,
                    callback="Group.CallbackAfterSendMsg",
                )
            ]
        )
        grp_send = wait_for(lambda: fake.groups if fake.groups else None, timeout=15)
        check("群内触发放开后同一条通道起任务并回贴", grp_send is not None, f"groups={len(fake.groups)}")
        if fake.groups:
            first_grp_send = fake.groups[0]
            check(
                "群聊回贴走 send_group_message，groupCode 取 InboundMessagePush.groupCode",
                first_grp_send["head"].get("cmd") == "send_group_message"
                and (first_grp_send["body"] or {}).get("groupCode") == GROUP_CODE
                and (((first_grp_send["body"] or {}).get("msgBody") or [{}])[0].get("msgContent") or {}).get("text")
                == "已收到：在群里汇总一下（策略已放开）",
                str(first_grp_send)[:320],
            )
        grp = wait_for(lambda: _group_session(client, min_messages=3), timeout=15) or _group_session(client)
        check(
            "台账按真实经过记录：挡下的那条只有进，放开后的那条一进一出（也不重复回贴）",
            grp is not None
            and [m["direction"] for m in grp["messages"]] == ["in", "in", "out"],
            str([m["direction"] for m in (grp or {}).get("messages") or []])[:200],
        )
        dropped = client.post("/api/im/policies", json={"scope": GROUP_CODE, "drop": True}).json()
        check(
            "用完删掉该会话的策略覆盖（不留下别人看不见的永久放宽）",
            dropped.get("ok") is True and dropped.get("dropped") is True
            and GROUP_CODE not in (dropped.get("scopes") or {}),
            str(dropped)[:200],
        )
        check(
            "needAck 的推送都回了 cmdType=3 的 ACK",
            wait_for(lambda: True if len(fake.acks) >= 6 else None, timeout=5) is not None
            and all(int(a.get("cmdType") or 0) == 3 for a in fake.acks)
            and all(str(a.get("msgId") or "") for a in fake.acks),
            f"acks={len(fake.acks)}",
        )
        pinged = wait_for(lambda: fake.pings if len(fake.pings) >= 2 else None)
        check("应用层 ping 有回执并持续发出（官方 5 秒一次，自测压到 0.3 秒）", bool(pinged), str(fake.pings)[:200])

        sessions = wait_for(
            lambda: [s for s in _sessions_of(client) if s["source"] == "yuanbao"]
            if len([s for s in _sessions_of(client) if s["source"] == "yuanbao"]) >= 2
            else None
        ) or []
        check("元宝会话落入频道台账（单聊 + 群聊各一条）", len(sessions) >= 2, str(_sessions_of(client))[:300])
        single = next((s for s in sessions if s["id"] == f"im-yuanbao-{DEFAULT_USER}"), None)
        group = next((s for s in sessions if s["id"] == f"im-yuanbao-{GROUP_CODE}"), None)
        check("单聊会话号取 fromAccount", single is not None, str(sessions)[:240])
        check("群聊会话号取 groupCode", group is not None, str(sessions)[:240])
        if single:
            check("标题取自首条消息首行", single["title"].startswith("帮我把合同要点"), single["title"])
            check("台账一进一出成对", [m["direction"] for m in single["messages"]] == ["in", "out"], str(single["messages"])[:200])
        view = _channel_of(client, "yuanbao")
        check(
            "接口视图不泄 appSecret / bot_token",
            DEFAULT_APP_SECRET not in json.dumps(view, ensure_ascii=False)
            and DEFAULT_BOT_TOKEN not in json.dumps(view, ensure_ascii=False)
            and "…" in view["account"].get("botId", "")
            and view["account"].get("heartbeatSeconds") == 0.3,
            str(view.get("account"))[:240],
        )

        # 回贴被拒：引擎如实落成 IM_REPLY_FAILED，不假装已送达
        fake.reject_send = True
        asyncio.run_coroutine_threadsafe(
            fake.live()[-1].send_bytes(fake.push_frame(inbound_push("这条会被拒", msg_id="yb-msg-6", from_account=SECOND_USER))),
            fake._loop,
        ).result(timeout=5)
        failed = wait_for(
            lambda: _channel_of(client, "yuanbao")
            if (_channel_of(client, "yuanbao").get("lastError") or {}).get("code") == "IM_REPLY_FAILED"
            else None
        )
        check(
            "回贴被拒映射 IM_REPLY_FAILED",
            failed is not None and "2001" in (failed.get("lastError") or {}).get("detail", ""),
            str(_channel_of(client, "yuanbao"))[:300],
        )
        fake.reject_send = False

        sockets_before_kick = fake.sockets

        def _post(path: str, payload: dict | None = None):
            req = urllib.request.Request(
                f"{fake.api_base}{path}",
                data=json.dumps(payload or {}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode())

        _post("/kick")
        kicked = wait_for(
            lambda: _channel_of(client, "yuanbao")
            if (_channel_of(client, "yuanbao").get("lastError") or {}).get("code") == "IM_CONNECTION_KICKED"
            else None
        )
        check("kickout 帧映射 IM_CONNECTION_KICKED 且状态 failed", kicked is not None and kicked["state"] == "failed", str(_channel_of(client, "yuanbao"))[:300])
        check("顶掉后连接循环自己收尾", runtime._task is None or runtime._task.done())
        time.sleep(1.5)
        check(
            "顶掉后不再退避重连",
            fake.sockets == sockets_before_kick,
            f"before={sockets_before_kick} now={fake.sockets}",
        )

        stopped = client.post("/api/im/yuanbao/stop").json()
        check("停止后 state=stopped", stopped.get("state") == "stopped", str(stopped)[:200])
        forgotten = client.post("/api/im/yuanbao/forget").json()
        check("解绑成功", forgotten.get("ok") is True, str(forgotten)[:200])
        check("解绑只删元宝凭据", "yuanbao" not in json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else True)
        check("解绑保留历史会话台账（数据保护）", len([s for s in _sessions_of(client) if s["source"] == "yuanbao"]) >= 2)


# ── C 段：协议分支（不依赖 TestClient） ───────────────────────────


def phase_c(fake: FakeYuanbao) -> None:
    print("\n== C. 元宝协议分支 ==")

    # canonical 字节 fixture：字段号直接从官方描述符手写，不经过本仓库编码器
    head = b"\x08\x02\x12\x04push\x18\x07\x22\x02m1\x2a\x03mod\x30\x01"
    frame = b"\x0a" + bytes([len(head)]) + head + b"\x12\x02hi"
    decoded = yb.YuanbaoChannel().decode_frame(frame)
    check(
        "protobuf 字段号与官方描述符一致（canonical 字节 fixture）",
        decoded is not None
        and decoded["head"]["cmdType"] == 2
        and decoded["head"]["cmd"] == "push"
        and decoded["head"]["seqNo"] == 7
        and decoded["head"]["msgId"] == "m1"
        and decoded["head"]["module"] == "mod"
        and decoded["head"]["needAck"] is True
        and decoded["data"] == b"hi",
        str(decoded)[:260],
    )
    check("坏帧不猜，decode_frame 返回 None", yb.YuanbaoChannel().decode_frame(b"\xff\xff\xff") is None)

    def auth_codes(code: int) -> tuple[str, str, bool, bool]:
        ch = yb.YuanbaoChannel()
        ch._token = "cached"

        async def _run():
            head = {"cmdType": 1, "cmd": "auth-bind", "status": 0}
            data = yb._pack(yb._f_uint(1, code), yb._f_str(2, "x"))
            try:
                ch._on_response(head, data, yb.CMD_AUTH)
                return "", "", True
            except im_channels.ImError as exc:
                return exc.code, exc.detail, exc.retryable
            finally:
                for task in (ch._hb_task, ch._auth_task):
                    if task is not None and not task.done():
                        task.cancel()

        status, detail, retryable = asyncio.run(_run())
        return status, detail, retryable, ch._authenticated

    code, _, _, authed = auth_codes(0)
    check("auth code=0 视为鉴权成功", code == "" and authed is True, f"{code} authed={authed}")
    code, _, _, authed = auth_codes(yb.AUTH_ALREADY)
    check("auth code=41101（已认证）也视为成功", code == "" and authed is True, f"{code} authed={authed}")
    code, _, _, authed = auth_codes(41103)
    check("auth code=41103 映射 IM_CONNECTION_FAILED 并清令牌缓存", code == "IM_CONNECTION_FAILED" and authed is False, f"{code} authed={authed}")
    code, _, retryable, _ = auth_codes(50400)
    check("auth code=50400 映射可重试的 IM_AUTH_FAILED", code == "IM_AUTH_FAILED" and retryable is True, f"{code} retryable={retryable}")
    code, _, retryable, _ = auth_codes(99999)
    check("auth 未知错误码不可重试", code == "IM_AUTH_FAILED" and retryable is False, f"{code} retryable={retryable}")

    # 非文本 / 回声 / 重复
    ch = yb.YuanbaoChannel()
    ch._bot_id = DEFAULT_BOT_ID
    push = yb._pack(yb._f_str(1, "C2C.CallbackAfterSendMsg"), yb._f_str(2, yb.BIZ_MODULE), yb._f_str(3, "c-1"), yb._f_bytes(4, yb._pack(
        yb._f_str(1, "C2C.CallbackAfterSendMsg"), yb._f_str(2, DEFAULT_USER), yb._f_str(3, DEFAULT_BOT_ID),
        yb._f_str(12, "c-1"), yb._f_bytes(13, yb._msg_body(" 你好 ")),
    )))
    ev = ch.parse_event({"head": {"cmdType": 2, "cmd": "push", "needAck": True, "msgId": "p-1", "module": "x"}, "data": push})
    check("文本推送解析为 inbound，并记下回贴目标", ev.kind == "inbound" and ev.text == "你好" and ev.conversation_id == DEFAULT_USER, str(ev)[:220])
    check("推送 needAck 时记下 ACK 用的 head", ch._ack_head is not None and ch._ack_head.get("msgId") == "p-1", str(ch._ack_head)[:160])
    ev2 = ch.parse_event({"head": {"cmdType": 2, "cmd": "push", "msgId": "p-2"}, "data": push})
    check("重复 msgId 不再起任务", ev2.kind == "ignore", str(ev2)[:160])
    echo = yb._pack(yb._f_str(2, DEFAULT_BOT_ID), yb._f_str(12, "c-echo"), yb._f_bytes(13, yb._msg_body("回声")))
    push_echo = yb._pack(yb._f_str(1, "C2C.CallbackAfterSendMsg"), yb._f_str(2, yb.BIZ_MODULE), yb._f_str(3, "p-3"), yb._f_bytes(4, echo))
    check("机器人自己的消息（fromAccount=bot_id）不当事件", ch.parse_event({"head": {"cmdType": 2, "cmd": "push", "msgId": "p-3"}, "data": push_echo}).kind == "ignore")
    img = yb._pack(yb._f_str(2, SECOND_USER), yb._f_str(12, "c-img"), yb._f_bytes(13, yb._pack(
        yb._f_str(1, "TIMImageElem"), yb._f_bytes(2, yb._pack(yb._f_str(10, "https://example.invalid/p.png")))
    )))
    push_img = yb._pack(yb._f_str(1, "C2C.CallbackAfterSendMsg"), yb._f_str(2, yb.BIZ_MODULE), yb._f_str(3, "p-4"), yb._f_bytes(4, img))
    check("非文本元素不当事件", ch.parse_event({"head": {"cmdType": 2, "cmd": "push", "msgId": "p-4"}, "data": push_img}).kind == "ignore")

    # 群 / 单聊与 @ 的证据：群标识是帧里有的，@ 名单是帧里没有的
    single_ev = ch.parse_event(
        {"head": {"cmdType": 2, "cmd": "push", "msgId": "p-5"}, "data": yb._pack(
            yb._f_str(1, "C2C.CallbackAfterSendMsg"), yb._f_str(2, yb.BIZ_MODULE), yb._f_str(3, "c-5"),
            yb._f_bytes(4, yb._pack(
                yb._f_str(2, SECOND_USER), yb._f_str(12, "c-5"),
                yb._f_uint(10, 1712345678), yb._f_bytes(13, yb._msg_body("单聊一条")),
            )),
        )}
    )
    check(
        "单聊帧：is_group=False（没有 groupCode 就是真的不是群）",
        single_ev.kind == "inbound" and single_ev.is_group is False and single_ev.conversation_id == SECOND_USER,
        str(single_ev)[:220],
    )
    check("单聊帧的 msgTime 按秒换成毫秒", single_ev.at_ms == 1712345678000, str(single_ev.at_ms))
    group_ev = ch.parse_event(
        {"head": {"cmdType": 2, "cmd": "push", "msgId": "p-6"}, "data": yb._pack(
            yb._f_str(1, "Group.CallbackAfterSendMsg"), yb._f_str(2, yb.BIZ_MODULE), yb._f_str(3, "c-6"),
            yb._f_bytes(4, yb._pack(
                yb._f_str(2, SECOND_USER), yb._f_str(6, GROUP_CODE), yb._f_str(12, "c-6"),
                yb._f_bytes(13, yb._msg_body("群里一条")),
            )),
        )}
    )
    check(
        "群帧：groupCode 非空即群，会话号取 groupCode",
        group_ev.kind == "inbound" and group_ev.is_group is True and group_ev.conversation_id == GROUP_CODE,
        str(group_ev)[:220],
    )
    check(
        "元宝答不出「有没有 @ 我」：mentioned_me 是 None，不是 False（False 会永远不回）",
        group_ev.mentioned_me is None,
        str(group_ev.mentioned_me),
    )
    check(
        "mention_note 说清证据来自哪、用户能改什么",
        "groupCode" in yb.YuanbaoChannel.mention_note
        and "没有" in yb.YuanbaoChannel.mention_note
        and "每条都处理" in yb.YuanbaoChannel.mention_note,
        yb.YuanbaoChannel.mention_note[:160],
    )
    check(
        "msgTime 缺失/坏值都回 0（等于不参与时间窗），不编时间戳",
        yb._ms_of_seconds(None) == 0 and yb._ms_of_seconds("abc") == 0 and yb._ms_of_seconds(0) == 0
        and yb._ms_of_seconds(-5) == 0 and yb._ms_of_seconds("1712345678") == 1712345678000,
        str([yb._ms_of_seconds(v) for v in (None, "abc", 0, -5, "1712345678")]),
    )

    # kickout
    kickout = yb._pack(yb._f_uint(1, 1), yb._f_str(2, "another connection"))
    code, detail, retryable = _error(lambda: ch.parse_event({"head": {"cmdType": 2, "cmd": yb.CMD_KICKOUT}, "data": kickout}))
    check("kickout 帧映射 IM_CONNECTION_KICKED 且不可重试", code == "IM_CONNECTION_KICKED" and retryable is False, f"{code} {detail}"[:220])

    # ACK：复制 head、换 seqNo、cmdType=3
    class StubWs:
        def __init__(self):
            self.sent: list[bytes] = []

        async def send_bytes(self, raw: bytes) -> None:
            self.sent.append(raw)

    ch_ack = yb.YuanbaoChannel()
    ch_ack.parse_event({"head": {"cmdType": 2, "cmd": "push", "needAck": True, "msgId": "m-ack", "module": "mod", "seqNo": 4}, "data": b""})
    ws = StubWs()
    asyncio.run(ch_ack.ack(ws, {}, im_channels.InboundEvent(kind="ignore")))
    if ws.sent:
        acked = yb._decode(ws.sent[0], yb.CONN_SCHEMA)["head"]
        check(
            "ACK 复制 head 只换 seqNo，cmdType=3",
            acked["cmdType"] == 3 and acked["msgId"] == "m-ack" and acked["cmd"] == "push" and acked["seqNo"] != 4,
            str(acked)[:220],
        )
    else:
        check("ACK 复制 head 只换 seqNo，cmdType=3", False, "没有发出 ACK")

    # 无连接时回贴
    code, detail, _ = _error(lambda: asyncio.run(yb.YuanbaoChannel().send(_creds(), DEFAULT_USER, "hi")))
    check("连接不在时回贴如实失败", code == "IM_CONNECTION_FAILED" and "回贴发不出去" in detail, f"{code} {detail}"[:220])

    # 签名失败：错误 secret 不被写进错误详情；10099 重试次数正确
    async def _sign(secret: str, retry: int) -> tuple[str, str, int]:
        before = len(fake.sign_requests)
        fake.sign_retry_times = retry
        async with aiohttp.ClientSession() as session:
            probe = yb.YuanbaoChannel()
            probe.runtime = _HttpRuntime(session)
            try:
                await probe._sign_token({"appKey": DEFAULT_APP_KEY, "appSecret": secret}, force=True)
                return "", "", len(fake.sign_requests) - before
            except im_channels.ImError as exc:
                return exc.code, exc.detail, len(fake.sign_requests) - before

    bad_code, bad_detail, bad_calls = asyncio.run(_sign("wrong-secret-value", 0))
    check("签名被拒映射 IM_AUTH_FAILED", bad_code == "IM_AUTH_FAILED", f"{bad_code} {bad_detail}"[:220])
    check("错误详情里没有 AppSecret", "wrong-secret-value" not in bad_detail, bad_detail[:220])

    original_sign = yb.SIGN_RETRYABLE
    ok_code, _, ok_calls = asyncio.run(_sign(DEFAULT_APP_SECRET, 2))
    check("10099 重试三次后成功（含首次）", ok_code == "" and ok_calls == 3, f"{ok_code} calls={ok_calls}")
    check("SIGN_RETRYABLE 常量与官方一致（10099）", original_sign == 10099, str(original_sign))

    # 环境变量口子
    saved = {k: os.environ.get(k) for k in ("TT_YUANBAO_API_BASE", "TT_YUANBAO_WS_URL", "TT_YUANBAO_HEARTBEAT_SECONDS")}
    try:
        os.environ.pop("TT_YUANBAO_API_BASE", None)
        os.environ.pop("TT_YUANBAO_WS_URL", None)
        os.environ.pop("TT_YUANBAO_HEARTBEAT_SECONDS", None)
        check("默认 sign-token 基址是官方域名", yb.api_base() == "https://bot.yuanbao.tencent.com", yb.api_base())
        check("默认长连接地址是官方域名", yb.ws_url() == "wss://bot-wss.yuanbao.tencent.com/wss/connection", yb.ws_url())
        os.environ["TT_YUANBAO_HEARTBEAT_SECONDS"] = "abc"
        check("心跳间隔写坏了退回官方 5 秒", yb._seconds("TT_YUANBAO_HEARTBEAT_SECONDS", 5.0) == 5.0)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> None:
    fake = FakeYuanbao()
    fake.start()
    try:
        phase_ab(fake)
        phase_c(fake)
    finally:
        fake.stop()
        asyncio.run(im_channels.shutdown())
    print("-" * 52)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine im yuanbao selftest: ALL PASS")


if __name__ == "__main__":
    main()
