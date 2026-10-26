#!/usr/bin/env python3
"""
假飞书开放平台：长连接（WSClient）模式的本地对端替身。

帧形状照官方 SDK lark-oapi 1.7.5，不自己发明字段：
- ws/client.py：POST {domain}/callback/ws/endpoint 的请求体 {"AppID","AppSecret"} 与
  头（locale、SDK User-Agent）、返回体 {code,msg,data:{URL,ClientConfig}}；
  连接地址 query 里的 device_id/service_id；pbbp2.Frame 的收发形状；
  数据帧头 sum/seq/message_id/trace_id/type；回执是把原帧的 payload 换成
  JSON.marshal(Response(code=200)) 并追加 biz_rt 头；控制帧 ping/pong
- ws/const.py：GEN_ENDPOINT_URI、OK/SYSTEM_BUSY/FORBIDDEN/AUTH_FAILED/EXCEED_CONN_LIMIT
- api/im/v1/model/*：im.message.receive_v1 的 header/sender/message 字段名
- core/token/*：POST /open-apis/auth/v3/tenant_access_token/internal 与
  Authorization: Bearer（core/http/transport.py）

编码用的就是官方 protobuf 模型（lark_oapi.ws.pb.pbbp2_pb2.Frame），所以线上形状是真的，
只有服务端行为是替身。引擎侧走真实 aiohttp WebSocket 客户端与真实 HTTP 回贴，两边只在
127.0.0.1 上对话，不联网、不落盘。

替身自定的失败码（不代表飞书真实码，只用来驱动引擎的分支）：
- behavior.reject_endpoint：换连接地址返回 AUTH_FAILED
- behavior.token_reject：token 接口返回非 0，引擎应映射 IM_AUTH_FAILED
- behavior.bad_token_once：下一次发消息被拒 {code:401,msg:"access token invalid"}，
  用来验证引擎会换新 token 重发一次
- behavior.reply_errcode：收消息接口（reply 与 create 共用）返回非 0，引擎应报 IM_REPLY_FAILED

两处消费方：
  tests/integration/engine_im_feishu_selftest.py —— 进程内起服务，跑完整对拍
  python tests/integration/fake_feishu.py --port 19612 —— 独立进程，供界面实机验证；
    POST /inject {"text":"..."} 往在线连接注入一条用户消息，
    POST /behavior 切换 reject_endpoint / split_event / pong_config / token_reject /
      reply_errcode，GET /state 看计数器（换地址次数、连接数、心跳数、回贴内容、回执）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import threading
import time

from aiohttp import web

try:
    from lark_oapi.ws import const as ws_const
    from lark_oapi.ws.enum import FrameType, MessageType
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame
except Exception as exc:  # 随包/开发解释器缺官方 SDK 时如实跳过，不猜帧形状
    raise SystemExit(f"SKIP: 假飞书需要官方 lark-oapi 提供帧形状（{exc}）")

APP_ID = "cli_ttfakefeishu0001"
APP_SECRET = "ttfakefeishusecret01248a1c9e7b3d5f"
TOKEN = "t-fake-tenant-token"
P2P_CHAT_ID = "oc_ttfake_p2p_0001"
GROUP_CHAT_ID = "oc_ttfake_group_0002"
SENDER_OPEN_ID = "ou_ttfake_zhangsan"
SECOND_OPEN_ID = "ou_ttfake_lisi"
SERVICE_ID = 7


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _frame(*, method: int, headers: dict[str, str], payload: bytes, service: int = SERVICE_ID) -> bytes:
    f = Frame()
    f.SeqID = int(headers.get(ws_const.HEADER_SEQ) or 0)
    f.LogID = 1
    f.service = service
    f.method = method
    for key, value in headers.items():
        h = f.headers.add()
        h.key = key
        h.value = str(value)
    f.payload = payload
    return f.SerializeToString()


def data_frame(payload: dict | str, *, message_id: str, type_name: str = "event",
               seq: int = 0, total: int = 1) -> bytes:
    """一条数据帧：sum>1 时按 seq 分片下发（官方 _combine 的输入形状）"""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    headers = {
        ws_const.HEADER_TYPE: type_name,
        ws_const.HEADER_MESSAGE_ID: message_id,
        ws_const.HEADER_SUM: str(total),
        ws_const.HEADER_SEQ: str(seq),
        ws_const.HEADER_TRACE_ID: "ttfake-trace-1",
    }
    return _frame(method=FrameType.DATA.value, headers=headers, payload=text.encode("utf-8"))


def control_frame(type_name: str, payload: dict | None = None) -> bytes:
    headers = {ws_const.HEADER_TYPE: type_name}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else b""
    return _frame(method=FrameType.CONTROL.value, headers=headers, payload=body)


def split_data_frames(payload: dict, *, message_id: str) -> list[bytes]:
    """把一条事件正文切成两帧：官方按 sum/seq 合包，未收齐前不该当事件"""
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    mid = len(raw) // 2
    return [
        data_frame(raw[:mid].decode("utf-8", "ignore"), message_id=message_id, seq=0, total=2),
        data_frame(raw[mid:].decode("utf-8", "ignore"), message_id=message_id, seq=1, total=2),
    ]


def message_event(text: str, *, event_id: str, message_id: str, chat_id: str = GROUP_CHAT_ID,
                  chat_type: str = "group", sender_open_id: str = SENDER_OPEN_ID,
                  sender_type: str = "user", message_type: str = "text",
                  mentions: list[dict] | None = None, content: dict | None = None) -> dict:
    """im.message.receive_v1 的回调正文，字段名取自官方 EventHeader/EventSender/EventMessage"""
    body: dict = {"text": text} if content is None else content
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "create_time": str(int(time.time() * 1000)),
            "token": "fake-verification-token",
            "app_id": APP_ID,
            "tenant_key": "ttfake tenant key",
        },
        "event": {
            "sender": {
                "sender_id": {"union_id": "on_ttfake_u", "user_id": sender_open_id[2:],
                              "open_id": sender_open_id},
                "sender_type": sender_type,
                "tenant_key": "ttfake tenant key",
            },
            "message": {
                "message_id": message_id,
                "create_time": str(int(time.time() * 1000)),
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": message_type,
                "content": json.dumps(body, ensure_ascii=False),
                "mentions": mentions or [],
            },
        },
    }


class FakeFeishu:
    """第 1 条连接跑完整剧本（凭据探测之后引擎真正连上的那条），后续连接在线供 /inject 用"""

    def __init__(self, app_id: str = APP_ID, app_secret: str = APP_SECRET, script_on: int = 1):
        self.app_id = app_id
        self.app_secret = app_secret
        self.script_on = script_on
        self.port = 0
        self.sockets = 0
        self.endpoint_calls: list[dict] = []
        self.token_calls = 0
        self.replies: list[dict] = []
        self.sends: list[dict] = []
        self.acks: list[dict] = []
        self.pings = 0
        self.ping_services: list[int] = []
        self.pongs_sent = 0
        self.unexpected: list[dict] = []
        self.events_pushed = 0
        self.open_ws: list = []
        # 故障开关
        self.reject_endpoint = False
        self.split_event = False
        self.pong_config = False
        self.token_reject = False
        self.bad_token_once = False
        self.reply_errcode = 0
        self._runner = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()

    # ── 生命周期 ────────────────────────────────────────────────────
    def start(self, port: int | None = None):
        self.port = port or free_port()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        assert self._ready.wait(10), "假飞书未能启动"

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
        app.router.add_post(ws_const.GEN_ENDPOINT_URI, self.handle_endpoint)
        app.router.add_post("/open-apis/auth/v3/tenant_access_token/internal", self.handle_token)
        app.router.add_post("/open-apis/im/v1/messages", self.handle_create)
        app.router.add_post("/open-apis/im/v1/messages/{message_id}/reply", self.handle_reply)
        app.router.add_get("/ws", self.handle_ws)
        app.router.add_post("/inject", self.handle_inject)
        app.router.add_post("/behavior", self.handle_behavior)
        app.router.add_get("/state", self.handle_state)
        self._runner = web.AppRunner(app)
        self._loop.run_until_complete(self._runner.setup())
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        self._loop.run_until_complete(site.start())
        self._ready.set()
        self._loop.run_forever()

    @property
    def domain(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def live(self) -> list:
        return [ws for ws in self.open_ws if not ws.closed]

    # ── HTTP 侧：换连接地址与回贴（官方形状） ────────────────────────
    async def handle_endpoint(self, request):
        raw = await request.text()
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        self.endpoint_calls.append({"body": body, "locale": request.headers.get("locale", ""),
                                   "ua": request.headers.get("User-Agent", "")})
        if self.reject_endpoint:
            return web.json_response({"code": ws_const.AUTH_FAILED, "msg": "app secret invalid"})
        if body.get("AppID") != self.app_id or body.get("AppSecret") != self.app_secret:
            return web.json_response({"code": ws_const.AUTH_FAILED, "msg": "app secret invalid"})
        url = f"ws://127.0.0.1:{self.port}/ws?device_id=ttfake-device-1&service_id={SERVICE_ID}"
        return web.json_response(
            {
                "code": ws_const.OK,
                "msg": "ok",
                "data": {
                    "URL": url,
                    "ClientConfig": {"ReconnectCount": -1, "ReconnectInterval": 120,
                                     "ReconnectNonce": 30, "PingInterval": 1},
                },
            }
        )

    async def handle_token(self, request):
        body = json.loads(await request.text())
        self.token_calls += 1
        if self.token_reject or body.get("app_id") != self.app_id or body.get("app_secret") != self.app_secret:
            return web.json_response({"code": ws_const.AUTH_FAILED, "msg": "app secret invalid"})
        return web.json_response({"code": ws_const.OK, "msg": "ok", "tenant_access_token": TOKEN,
                                  "expire": 7200})

    def _token_ok(self, request) -> bool:
        return request.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    async def _read_message_body(self, request) -> tuple[dict, str]:
        if self.bad_token_once:
            self.bad_token_once = False
            # 替身自定码：模拟 token 在飞书侧提前失效，引擎应换新 token 重发一次
            return {"code": 401, "msg": "access token invalid"}, "rejected"
        if not self._token_ok(request):
            return {"code": 401, "msg": "access token invalid"}, "rejected"
        payload = json.loads(await request.text())
        return payload, ""

    async def handle_reply(self, request):
        payload, _ = await self._read_message_body(request)
        if payload.get("code"):
            return web.json_response(payload)
        text = ""
        try:
            text = json.loads(payload.get("content") or "{}").get("text", "")
        except Exception:
            text = f"<unparsed:{payload.get('content')}>"
        self.replies.append({"message_id": request.match_info["message_id"], "text": text,
                             "msg_type": payload.get("msg_type", ""), "raw": payload,
                             "auth": request.headers.get("Authorization", ""),
                             "ua": request.headers.get("User-Agent", "")})
        code = self.reply_errcode
        return web.json_response({"code": code, "msg": "ok" if not code else "reply rejected by server",
                                  "data": {"message_id": "om_ttfake_reply_1"}})

    async def handle_create(self, request):
        payload, _ = await self._read_message_body(request)
        if payload.get("code"):
            return web.json_response(payload)
        if request.query.get("receive_id_type") != "chat_id":
            return web.json_response({"code": 99991672, "msg": "receive_id_type is required"})
        text = ""
        try:
            text = json.loads(payload.get("content") or "{}").get("text", "")
        except Exception:
            text = f"<unparsed:{payload.get('content')}>"
        self.sends.append({"receive_id": payload.get("receive_id", ""), "text": text,
                           "msg_type": payload.get("msg_type", "")})
        code = self.reply_errcode
        return web.json_response({"code": code, "msg": "ok" if not code else "send rejected by server",
                                  "data": {"message_id": "om_ttfake_send_1"}})

    # ── 长连接侧 ────────────────────────────────────────────────────
    async def handle_ws(self, request):
        self.sockets += 1
        ws = web.WebSocketResponse(heartbeat=None)
        await ws.prepare(request)
        self.open_ws.append(ws)
        if self.sockets == self.script_on:
            await self.run_script(ws)
        async for msg in ws:
            if msg.type is web.WSMsgType.BINARY:
                await self.handle_client_frame(ws, msg.data)
            elif msg.type in (web.WSMsgType.CLOSED, web.WSMsgType.ERROR):
                break
        return ws

    def decode(self, raw: bytes) -> dict:
        f = Frame()
        f.ParseFromString(raw)
        headers = {h.key: h.value for h in f.headers}
        return {"method": f.method, "service": f.service, "headers": headers, "payload": bytes(f.payload)}

    async def handle_client_frame(self, ws, raw: bytes) -> None:
        got = self.decode(raw)
        if got["method"] == FrameType.CONTROL.value:
            if got["headers"].get(ws_const.HEADER_TYPE) != MessageType.PING.value:
                self.unexpected.append(got["headers"])
                return
            self.pings += 1
            # 官方 SDK 把换地址时从 URL query 拿到的 service_id 回填进心跳帧
            self.ping_services.append(got["service"])
            pong_payload = None
            if self.pong_config:
                pong_payload = {"ReconnectCount": -1, "ReconnectInterval": 60,
                                "ReconnectNonce": 5, "PingInterval": 2}
            self.pongs_sent += 1
            await ws.send_bytes(control_frame(MessageType.PONG.value, pong_payload))
            return
        if got["method"] != FrameType.DATA.value:
            self.unexpected.append(got["headers"])
            return
        # 引擎的回执：原帧回写，payload 换成 {"code":200}，追加 biz_rt
        headers = got["headers"]
        try:
            code = json.loads(got["payload"].decode("utf-8")).get("code")
        except Exception:
            code = None
        self.acks.append({"message_id": headers.get(ws_const.HEADER_MESSAGE_ID, ""),
                          "type": headers.get(ws_const.HEADER_TYPE, ""),
                          "code": code, "biz_rt": headers.get(ws_const.HEADER_BIZ_RT, "")})

    async def run_script(self, ws) -> None:
        """官方协议里引擎该处理什么、该忽略什么，一次推完"""
        # 1) 群聊文本：起任务并回执
        await self.push(ws, message_event("帮我把季度总结整理成一页", event_id="evt-1",
                                          message_id="om_ttfake_1"))
        # 2) 同一条事件重投（回执没送出去时飞书会再发一次）：不能再起一个任务
        await self.push(ws, message_event("帮我把季度总结整理成一页", event_id="evt-1",
                                          message_id="om_ttfake_1"))
        # 3) 单聊 + @机器人占位符：会话标识取 chat_id，正文要剥掉 @_user_1
        await self.push(
            ws,
            message_event(
                "@_user_1 把这份清单按人分一下",
                event_id="evt-2",
                message_id="om_ttfake_2",
                chat_type="p2p",
                chat_id=P2P_CHAT_ID,
                sender_open_id=SECOND_OPEN_ID,
                mentions=[{"key": "@_user_1", "id": {"open_id": "ou_ttfake_bot"},
                            "name": "小通天", "mentioned_type": "bot"}],
            ),
        )
        # 4) 富文本消息：本通道只把文本当任务，其它形状如实忽略
        await self.push(ws, message_event("", event_id="evt-3", message_id="om_ttfake_3",
                                         message_type="post", content={"post": {}}))
        # 5) 机器人自己发的（sender_type=app）：不能触发第二个任务
        await self.push(ws, message_event("机器人自己贴回去的", event_id="evt-4",
                                          message_id="om_ttfake_4", sender_type="app"))
        # 6) 别的事件类型：不是消息事件
        await self.push(ws, {"schema": "2.0",
                             "header": {"event_id": "evt-5", "event_type": "application.v3.bot.menu"},
                             "event": {}})
        # 7) 服务端主动 ping：引擎不必回执，但连接要活着
        await ws.send_bytes(control_frame(MessageType.PING.value))
        # 8) card 帧：官方 SDK 对它不回执，本通道也不当任务
        await ws.send_bytes(data_frame({"card": "互动卡片回调"}, message_id="ftf-card",
                                       type_name="card"))

    async def push(self, ws, event: dict) -> None:
        # 帧头的 message_id 是飞书给这一帧的编号（合包用的 key），和事件正文里那条 IM
        # 消息的 message_id（回贴要 reply 的 id）不是同一个东西，所以前缀故意区分开
        frame_id = f"ftf-{event['header']['event_id']}"
        if self.split_event:
            for piece in split_data_frames(event, message_id=frame_id):
                await ws.send_bytes(piece)
        else:
            await ws.send_bytes(data_frame(event, message_id=frame_id))
        self.events_pushed += 1

    # ── 实机验证用的注入与故障开关 ──────────────────────────────────
    async def handle_inject(self, request):
        payload = await request.json()
        live = self.live()
        if not live:
            return web.json_response({"ok": False, "error": "no_live_socket", "sockets": self.sockets})
        n = len(self.replies) + len(self.sends) + 1
        await self.push(live[-1], message_event(
            str(payload.get("text") or "你好"), event_id=f"inject-{int(time.time() * 1000)}",
            message_id=f"om_ttfake_inject_{n}",
            chat_id=str(payload.get("chat_id") or GROUP_CHAT_ID),
            chat_type=str(payload.get("chat_type") or "group")))
        return web.json_response({"ok": True, "sockets": self.sockets, "pushed": n})

    async def handle_behavior(self, request):
        patch = await request.json()
        for key in ("reject_endpoint", "split_event", "pong_config", "token_reject", "bad_token_once"):
            if key in patch:
                setattr(self, key, bool(patch[key]))
        if "reply_errcode" in patch:
            self.reply_errcode = int(patch["reply_errcode"])
        if "script_on" in patch:
            self.script_on = int(patch["script_on"])
        return web.json_response({"ok": True})

    async def handle_state(self, request):
        return web.json_response(
            {
                "sockets": self.sockets,
                "live": len(self.live()),
                "endpoints": len(self.endpoint_calls),
                "tokens": self.token_calls,
                "pings": self.pings,
                "ping_services": sorted(set(self.ping_services)),
                "pongs": self.pongs_sent,
                "acks": self.acks,
                "replies": [r["text"] for r in self.replies],
                "sends": [s["text"] for s in self.sends],
                "unexpected": self.unexpected[:3],
                "endpoint_headers": self.endpoint_calls[:1],
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="本地假飞书开放平台（供界面实机验证）")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--script", type=int, default=0, help="在第几条连接上跑完整剧本（0=不跑）")
    args = parser.parse_args()
    fake = FakeFeishu(script_on=args.script if args.script > 0 else 10**9)
    fake.start(args.port or None)
    print(f"TT_FEISHU_DOMAIN={fake.domain}", flush=True)
    print(f"TT_FAKE_FEISHU_APP_ID={fake.app_id}", flush=True)
    print(f"TT_FAKE_FEISHU_SECRET={fake.app_secret}", flush=True)
    print("端口 {}: POST /inject {{\"text\":\"...\"}} · POST /behavior · GET /state".format(fake.port), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        fake.stop()


if __name__ == "__main__":
    main()
