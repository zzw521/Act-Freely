#!/usr/bin/env python3
"""
假腾讯元宝机器人网关：sign-token + WebSocket + protobuf 的本地对端替身。

线形状照腾讯官方 npm 包 openclaw-plugin-yuanbao 2.18.3 随包的 protobuf 描述符
（dist/src/access/ws/proto/{conn,biz}.json）与实现（access/http/request.js 的签名、
access/ws/client.js 的鉴权/心跳/推送、gateway.js 的推送解码）：
- POST /api/v5/robotLogic/sign-token，校验 HMAC-SHA256(secret=AppSecret,
  msg=nonce+timestamp+appKey+AppSecret)，成功回 {code:0, data:{bot_id, token, duration, source}}
- WS 首帧必须是 cmdType=0、cmd=auth-bind、module=conn_access 的 ConnMsg，
  data 为 AuthBindReq{bizId:"ybBot", authInfo:{uid,source,token}, deviceInfo:{... instanceId:"16"}}；
  对端回 cmdType=1、cmd=auth-bind 的 AuthBindRsp{code}
- 推送是 cmdType=2 的 ConnMsg，needAck 时客户端要回 cmdType=3、复制 head 换 seqNo 的 ACK；
  data 是 PushMsg{cmd,module,msgId,data}，data 再解 InboundMessagePush
- 回贴是业务请求（send_c2c_message / send_group_message），按 head.msgId 配对响应
- 保活是应用层 ping 帧，回 PingRsp

protobuf 编解码直接复用 engine/yuanbao_channel.py 的同一份实现（字段号只有一份来源），
但鉴权帧与回贴请求的 schema 在本文件里独立声明，避免「引擎自己编码又自己解码」的假通过。
引擎侧走真实 aiohttp WebSocket 客户端与真实签名往返，不是打桩返回值。

两处消费方：
  tests/integration/engine_im_yuanbao_selftest.py —— 进程内起服务，跑完整对拍
  python tests/integration/fake_yuanbao.py --port 19613 —— 独立进程，供界面实机验证；
    POST /inject {"text": "..."} 向在线连接推一条用户消息，
    POST /kick 让网关下发 kickout 帧，POST /behavior 切换故障开关，GET /state 看计数器。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import secrets
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

from aiohttp import WSMsgType, web

ENGINE_DIR = Path(__file__).resolve().parents[2] / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import yuanbao_channel as yb  # noqa: E402  同一份 protobuf 编解码

DEFAULT_APP_KEY = "ttfake-yb-app-key-0001"
DEFAULT_APP_SECRET = "ttfakeybsecret0123456789abcdef00"
DEFAULT_BOT_ID = "ttfake-yb-bot-0001"
DEFAULT_BOT_TOKEN = "ttfake-yb-token-0123456789abcdef"
DEFAULT_USER = "ttfake-yb-user-0001"
SECOND_USER = "ttfake-yb-user-0002"
GROUP_CODE = "ttfake-yb-group-0001"

# 官方描述符里的字段号，独立声明（不复用引擎侧 schema）
AUTH_INFO_SCHEMA = {1: ("uid", "str", None, False), 2: ("source", "str", None, False), 3: ("token", "str", None, False)}
DEVICE_SCHEMA = {
    1: ("appVersion", "str", None, False),
    2: ("appOperationSystem", "str", None, False),
    24: ("botVersion", "str", None, False),
    10: ("instanceId", "str", None, False),
}
AUTH_BIND_SCHEMA = {
    1: ("bizId", "str", None, False),
    2: ("authInfo", "msg", AUTH_INFO_SCHEMA, False),
    3: ("deviceInfo", "msg", DEVICE_SCHEMA, False),
}
MSG_CONTENT_SCHEMA = {1: ("text", "str", None, False), 10: ("url", "str", None, False), 12: ("fileName", "str", None, False)}
ELEMENT_SCHEMA = {1: ("msgType", "str", None, False), 2: ("msgContent", "msg", MSG_CONTENT_SCHEMA, False)}
SEND_C2C_SCHEMA = {
    1: ("msgId", "str", None, False),
    2: ("toAccount", "str", None, False),
    3: ("fromAccount", "str", None, False),
    4: ("msgRandom", "uint", None, False),
    5: ("msgBody", "msg", ELEMENT_SCHEMA, True),
    6: ("groupCode", "str", None, False),
    7: ("msgSeq", "uint", None, False),
    8: ("logExt", "str", None, False),
}
SEND_GROUP_SCHEMA = {
    1: ("msgId", "str", None, False),
    2: ("groupCode", "str", None, False),
    3: ("fromAccount", "str", None, False),
    4: ("toAccount", "str", None, False),
    5: ("random", "str", None, False),
    6: ("msgBody", "msg", ELEMENT_SCHEMA, True),
    7: ("refMsgId", "str", None, False),
    8: ("msgSeq", "uint", None, False),
    9: ("logExt", "str", None, False),
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def inbound_push(
    text: str,
    *,
    msg_id: str,
    from_account: str = DEFAULT_USER,
    group_code: str = "",
    msg_type: str = "TIMTextElem",
    nickname: str = "测试用户",
    callback: str = "C2C.CallbackAfterSendMsg",
) -> bytes:
    """InboundMessagePush 的线形状，字段号照官方描述符"""
    element = yb._msg_body(text) if msg_type == "TIMTextElem" else yb._pack(
        yb._f_str(1, msg_type), yb._f_bytes(2, yb._pack(yb._f_str(10, "https://example.invalid/p.png")))
    )
    inbound = yb._pack(
        yb._f_str(1, callback),
        yb._f_str(2, from_account),
        yb._f_str(3, DEFAULT_BOT_ID),
        yb._f_str(4, nickname),
        yb._f_str(6, group_code),
        yb._f_uint(8, int(time.time() * 1000) % 2**31),
        yb._f_uint(9, secrets.randbelow(2**31)),
        yb._f_uint(10, int(time.time())),
        yb._f_str(11, f"key-{msg_id}"),
        yb._f_str(12, msg_id),
        yb._f_bytes(13, element),
        yb._f_uint(18, 1 if group_code else 0),
    )
    return yb._pack(
        yb._f_str(1, callback),
        yb._f_str(2, yb.BIZ_MODULE),
        yb._f_str(3, msg_id),
        yb._f_bytes(4, inbound),
    )


class FakeYuanbao:
    def __init__(
        self,
        app_key: str = DEFAULT_APP_KEY,
        app_secret: str = DEFAULT_APP_SECRET,
        *,
        bot_id: str = DEFAULT_BOT_ID,
        bot_token: str = DEFAULT_BOT_TOKEN,
    ):
        self.app_key = app_key
        self.app_secret = app_secret
        self.bot_id = bot_id
        self.bot_token = bot_token
        self.port = 0
        # 计数与快照
        self.sign_requests: list[dict] = []
        self.sign_headers: dict = {}
        self.sign_bad: list[dict] = []
        self.sockets = 0
        self.auth_frames: list[dict] = []
        self.acks: list[dict] = []
        self.pings: list[str] = []
        self.c2c: list[dict] = []
        self.groups: list[dict] = []
        self.unexpected: list[dict] = []
        self.pending: list[bytes] = []
        self.connected: list = []
        # 故障开关
        self.reject_send = False
        self.auth_code = 0
        self.sign_code = 0
        self.sign_retry_times = 0
        self.ignore_pings = False
        self._runner = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()

    # -- 生命周期 --
    def start(self, port: int | None = None):
        self.port = port or free_port()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        assert self._ready.wait(10), "假元宝网关未能启动"

    def stop(self):
        asyncio.run_coroutine_threadsafe(self._teardown(), self._loop).result(timeout=5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    async def _teardown(self):
        if self._runner:
            await self._runner.cleanup()

    def _serve(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        app = web.Application()
        app.router.add_post("/api/v5/robotLogic/sign-token", self.handle_sign)
        app.router.add_get("/ws", self.handle_ws)
        app.router.add_post("/inject", self.handle_inject)
        app.router.add_post("/kick", self.handle_kick)
        app.router.add_post("/behavior", self.handle_behavior)
        app.router.add_get("/state", self.handle_state)
        self._runner = web.AppRunner(app)
        self._loop.run_until_complete(self._runner.setup())
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        self._loop.run_until_complete(site.start())
        self._ready.set()
        self._loop.run_forever()

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    def live(self) -> list:
        return [ws for ws in self.connected if not ws.closed]

    # -- ① sign-token：独立复算 HMAC，不匹配就拒绝 --
    async def handle_sign(self, request):
        body = await request.json()
        self.sign_requests.append(body)
        self.sign_headers = dict(request.headers)
        nonce = str(body.get("nonce") or "")
        timestamp = str(body.get("timestamp") or "")
        app_key = str(body.get("app_key") or "")
        signature = str(body.get("signature") or "")
        expected = hmac.new(
            self.app_secret.encode(),
            f"{nonce}{timestamp}{app_key}{self.app_secret}".encode(),
            hashlib.sha256,
        ).hexdigest()
        ok = (
            app_key == self.app_key
            and len(nonce) == 32
            and timestamp.endswith("+08:00")
            and hmac.compare_digest(signature, expected)
        )
        if self.sign_retry_times > 0:
            self.sign_retry_times -= 1
            return web.json_response({"code": 10099, "msg": "please retry"})
        if self.sign_code:
            return web.json_response({"code": self.sign_code, "msg": "forced failure"})
        if not ok:
            self.sign_bad.append(body)
            return web.json_response({"code": 10001, "msg": "invalid app_key or signature"})
        return web.json_response(
            {
                "code": 0,
                "data": {
                    "bot_id": self.bot_id,
                    "token": self.bot_token,
                    "duration": 7200,
                    "source": "bot",
                },
            }
        )

    # -- ② WebSocket：auth-bind → 推送 → ping/回贴 --
    async def handle_ws(self, request):
        self.sockets += 1
        ws = web.WebSocketResponse(heartbeat=None)
        await ws.prepare(request)
        self.connected.append(ws)
        msg = await ws.receive()
        if msg.type != WSMsgType.BINARY:
            self.unexpected.append({"type": str(msg.type)})
            await ws.close(code=1002)
            return ws
        frame = yb._decode(msg.data, yb.CONN_SCHEMA)
        head = frame.get("head") or {}
        data = frame.get("data") or b""
        bind = yb._decode(data, AUTH_BIND_SCHEMA) if data else {}
        self.auth_frames.append({"head": head, "bind": bind, "raw_len": len(msg.data)})
        auth_ok = (
            # proto3 默认值不上线：cmdType=0 的字段可能整段缺省，按默认值 0 解码
            int(head.get("cmdType") or 0) == 0
            and head.get("cmd") == yb.CMD_AUTH
            and head.get("module") == yb.CONN_MODULE
            and (bind.get("bizId") == yb.BIZ_ID)
            and (bind.get("authInfo") or {}).get("uid") == self.bot_id
            and (bind.get("authInfo") or {}).get("source") == "bot"
            and (bind.get("authInfo") or {}).get("token") == self.bot_token
            and (bind.get("deviceInfo") or {}).get("instanceId") == yb.TERMINAL_TYPE_ID
        )
        code = self.auth_code or (0 if auth_ok else 41103)
        rsp_data = yb._pack(
            yb._f_uint(1, code),
            yb._f_str(2, "ok" if code in (0, yb.AUTH_ALREADY) else "auth rejected"),
            yb._f_str(3, f"conn-{self.sockets}"),
            yb._f_uint(4, int(time.time())),
        )
        await ws.send_bytes(
            yb._conn(yb._head(1, yb.CMD_AUTH, str(head.get("msgId") or ""), yb.CONN_MODULE), rsp_data)
        )
        if code not in (0, yb.AUTH_ALREADY):
            await ws.close()
            return ws

        for payload in self.pending:
            await ws.send_bytes(payload)
        self.pending.clear()

        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                await self.handle_frame(ws, msg.data)
            elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                break
        return ws

    async def handle_frame(self, ws, raw: bytes) -> None:
        frame = yb._decode(raw, yb.CONN_SCHEMA)
        head = frame.get("head") or {}
        data = frame.get("data") or b""
        cmd_type = int(head.get("cmdType") or 0)
        cmd = str(head.get("cmd") or "")
        msg_id = str(head.get("msgId") or "")
        if cmd_type == 3:
            self.acks.append(head)
            return
        if cmd_type == 0 and cmd == yb.CMD_PING:
            self.pings.append(msg_id)
            if self.ignore_pings:
                return
            await ws.send_bytes(
                yb._conn(
                    yb._head(1, yb.CMD_PING, msg_id, yb.CONN_MODULE),
                    yb._pack(yb._f_uint(1, 1000), yb._f_uint(2, int(time.time()))),
                )
            )
            return
        if cmd == yb.CMD_SEND_C2C:
            self.c2c.append({"head": head, "body": yb._decode(data, SEND_C2C_SCHEMA) if data else {}})
        elif cmd == yb.CMD_SEND_GROUP:
            self.groups.append({"head": head, "body": yb._decode(data, SEND_GROUP_SCHEMA) if data else {}})
        else:
            self.unexpected.append({"head": head})
            return
        code = 2001 if self.reject_send else 0
        await ws.send_bytes(
            yb._conn(
                yb._head(1, cmd, msg_id, yb.BIZ_MODULE),
                yb._pack(yb._f_uint(1, code), yb._f_str(2, "ok" if code == 0 else "rejected")),
            )
        )

    def push_frame(self, data: bytes, *, cmd: str = "push", need_ack: bool = True) -> bytes:
        return yb._conn(
            yb._pack(
                yb._f_uint(1, 2),
                yb._f_str(2, cmd),
                yb._f_uint(3, yb._next_seq()),
                yb._f_str(4, uuid.uuid4().hex[:16]),
                yb._f_str(5, yb.BIZ_MODULE),
                yb._f_bool(6, need_ack),
            ),
            data,
        )

    # -- HTTP 侧：实机验证用的注入与故障开关 --
    async def handle_inject(self, request):
        payload = await request.json()
        data = inbound_push(
            str(payload.get("text") or "你好"),
            msg_id=f"inject-{int(time.time() * 1000)}",
            from_account=str(payload.get("fromAccount") or DEFAULT_USER),
            group_code=str(payload.get("groupCode") or ""),
        )
        self.pending.append(self.push_frame(data))
        live = self.live()
        if live:
            for frame in self.pending:
                await live[-1].send_bytes(frame)
            self.pending.clear()
        return web.json_response({"ok": True, "sockets": self.sockets, "pending": len(self.pending)})

    async def handle_kick(self, request):
        live = self.live()
        if not live:
            return web.json_response({"ok": False, "error": "no_live_socket"})
        data = yb._pack(
            yb._f_uint(1, 1),
            yb._f_str(2, "another connection authenticated with the same bot"),
            yb._f_str(3, "other-device"),
        )
        await live[-1].send_bytes(self.push_frame(data, cmd=yb.CMD_KICKOUT, need_ack=False))
        return web.json_response({"ok": True})

    async def handle_behavior(self, request):
        patch = await request.json()
        for key in ("reject_send", "ignore_pings"):
            if key in patch:
                setattr(self, key, bool(patch[key]))
        for key in ("auth_code", "sign_code", "sign_retry_times"):
            if key in patch:
                setattr(self, key, int(patch[key]))
        return web.json_response(
            {
                "ok": True,
                "reject_send": self.reject_send,
                "auth_code": self.auth_code,
                "sign_code": self.sign_code,
            }
        )

    async def handle_state(self, request):
        return web.json_response(
            {
                "sockets": self.sockets,
                "live": len(self.live()),
                "sign": len(self.sign_requests),
                "pings": len(self.pings),
                "acks": len(self.acks),
                "c2c": [
                    {
                        "to": (x["body"] or {}).get("toAccount"),
                        "text": (((x["body"] or {}).get("msgBody") or [{}])[0].get("msgContent") or {}).get("text"),
                    }
                    for x in self.c2c
                ],
                "groups": [
                    {
                        "code": (x["body"] or {}).get("groupCode"),
                        "text": (((x["body"] or {}).get("msgBody") or [{}])[0].get("msgContent") or {}).get("text"),
                    }
                    for x in self.groups
                ],
                "unexpected": self.unexpected[:3],
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="本地假腾讯元宝机器人网关（供界面实机验证）")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    fake = FakeYuanbao()
    fake.start(args.port or None)
    print(f"TT_YUANBAO_API_BASE={fake.api_base}", flush=True)
    print(f"TT_YUANBAO_WS_URL={fake.ws_url}", flush=True)
    print(f"TT_FAKE_YUANBAO_APP_KEY={fake.app_key}", flush=True)
    print(f"TT_FAKE_YUANBAO_APP_SECRET={fake.app_secret}", flush=True)
    print(f"端口 {fake.port}：POST /inject {{\"text\":\"...\"}} · POST /kick · POST /behavior · GET /state", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        fake.stop()


if __name__ == "__main__":
    main()
