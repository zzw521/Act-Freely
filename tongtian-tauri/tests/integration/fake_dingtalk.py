#!/usr/bin/env python3
"""
假钉钉服务端：钉钉 Stream 模式的本地对端替身。

协议字段照官方 dingtalk_stream 0.24.3（stream.py 的 open_connection、frames.py 的
Headers/CallbackMessage/SystemMessage、chatbot.py 的 ChatbotMessage.TOPIC 与
ChatbotHandler.reply_text），所以引擎侧走的是真实 aiohttp WebSocket 客户端与真实
HTTP 往返，不是打桩返回值。`data` 一律是 JSON 字符串——这一层最容易写错，替身按
SDK 的 from_dict 要求发，引擎也用 SDK 的解。

两处消费方：
  tests/integration/engine_im_dingtalk_selftest.py —— 进程内起服务，跑完整对拍
  python tests/integration/fake_dingtalk.py --port 19610 —— 独立进程，供界面实机验证；
    独立进程模式下可 POST /inject {"text": "..."} 往在线连接注入一条用户消息。

数据面只在本机 127.0.0.1，不联网、不落盘。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import itertools
import json
import socket
import threading
import time

from aiohttp import WSMsgType, web

DEFAULT_CLIENT_ID = "dingttfakeappkey000001"
DEFAULT_CLIENT_SECRET = "dingttfakesecret000000000000000000000000000000"
ROBOT_TOPIC = "/v1.0/im/bot/messages/get"
CONVERSATION_ID = "cidTTFAKE0123"
SENDER_STAFF_ID = "staff-0456"
SENDER_ID = "$:LWCP_v1:$sender0456"
CHATBOT_ID = "$:LWCP_v1:$chatbot9999"

# 一张真实的 1x1 PNG：附件层按响应头归类、按字节落盘，用假字节等于没测「落盘的是不是张图」
DEFAULT_MEDIA = (
    "image/png",
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    ),
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_MSG_SEQ = itertools.count(1)


def next_msg_id() -> str:
    """msgId 必须帧帧不同：引擎按 msgId 排重（协议要求），同一毫秒造出两条会让第二条被吃掉"""
    return f"msg-{int(time.time() * 1000)}-{next(_MSG_SEQ)}"


def frame(type_: str, topic: str, data: dict | str, message_id: str) -> dict:
    """钉钉的帧头是 camelCase，data 是 JSON 字符串（SDK frames.py 就是这么解的）"""
    return {
        "specVersion": "1.0",
        "type": type_,
        "headers": {
            "appId": "tt-fake-app",
            "connectionId": "conn-1",
            "contentType": "application/json",
            "messageId": message_id,
            "time": str(int(time.time() * 1000)),
            "topic": topic,
        },
        "data": data if isinstance(data, str) else json.dumps(data, ensure_ascii=False),
    }


def robot_message(
    text: str,
    *,
    msgtype: str = "text",
    webhook: str,
    sender_id: str = SENDER_ID,
    chatbot_id: str = CHATBOT_ID,
    conversation_id: str = CONVERSATION_ID,
    expired_ms: int = 0,
    at_me: bool = True,
    conversation_type: str = "2",
    download_code: str = "fake-download-code",
) -> dict:
    """chatbot.py ChatbotMessage.from_dict 认的那批 camelCase 字段

    `isInAtList` 是钉钉给机器人的「这条有没有 @ 我」答案（D-41 群策略就吃这个字段），
    默认按群聊被 @ 给 True；测「群里没 @」的分支传 at_me=False。
    """
    body: dict = {
        "conversationId": conversation_id,
        "conversationType": conversation_type,
        "conversationTitle": "通天测试群",
        "senderId": sender_id,
        "senderStaffId": SENDER_STAFF_ID,
        "senderNick": "张三",
        "chatbotUserId": chatbot_id,
        "robotCode": "dingttfakeappkey000001",
        "chatbotCorpId": "dingttfakecorp",
        "msgId": next_msg_id(),
        "createAt": int(time.time() * 1000),
        "msgtype": msgtype,
        "sessionWebhook": webhook,
        "sessionWebhookExpiredTime": expired_ms or int((time.time() + 3600) * 1000),
        "isAdmin": True,
        "isInAtList": at_me,
    }
    if msgtype == "text":
        body["text"] = {"content": text}
    elif msgtype == "picture":
        body["content"] = {"downloadCode": download_code}
    elif msgtype == "richText":
        body["content"] = {"richText": [{"text": text}, {"downloadCode": download_code}]}
    return body


class FakeDingTalk:
    """第一条反向连接跑完整剧本（ping→用户消息→机器人回声→图片→未知 topic→
    组织事件→disconnect），后续连接保持在线并接 `/inject`，用来验证引擎收到
    disconnect 后会重新换取连接地址。

    在线连接上的消息**只有读循环这一处读者**：aiohttp 不允许同一个 socket 上有两处
    `receive()`（否则当场抛「Concurrent call to receive() is not allowed」），所以
    `/inject` 不能自己去收引擎的回执，而是先把一个队列挂在该 socket 上，再由读循环把
    收到的帧交给它——独立进程模式与进程内对拍走的是同一条路径。
    """

    def __init__(self, client_id: str = DEFAULT_CLIENT_ID, client_secret: str = DEFAULT_CLIENT_SECRET):
        self.client_id = client_id
        self.client_secret = client_secret
        self.apis: list[dict] = []
        self.acks: list[dict] = []
        self.replies: list[dict] = []
        self.subscriptions: list[dict] = []
        self.open_ws: list = []
        self.inject_sinks: dict = {}
        self.media: dict[str, tuple[str, bytes]] = {}
        self.media_hits: list[str] = []
        self.media_fail: set[str] = set()
        self.seen_tickets: list[str] = []
        self.issued_tickets: list[str] = []
        self.injected = 0
        self.tickets = 0
        self.sockets = 0
        self.port = 0
        self._runner = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()

    # -- 生命周期 --
    def start(self, port: int | None = None):
        self.port = port or free_port()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        assert self._ready.wait(10), "假钉钉未能启动"

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
        app.router.add_post("/v1.0/oauth2/accessToken", self.handle_token)
        app.router.add_post("/v1.0/gateway/connections/open", self.handle_open)
        app.router.add_post("/v1.0/robot/messageFiles/download", self.handle_file_download)
        app.router.add_get("/media/{code}", self.handle_media_bytes)
        app.router.add_post("/robot/send", self.handle_webhook)
        app.router.add_get("/wsconnect", self.handle_ws)
        app.router.add_post("/inject", self.handle_inject)
        self._runner = web.AppRunner(app)
        self._loop.run_until_complete(self._runner.setup())
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        self._loop.run_until_complete(site.start())
        self._ready.set()
        self._loop.run_forever()

    @property
    def openapi_base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def webhook_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/robot/send"

    def calls(self, path: str) -> list[dict]:
        return [a for a in self.apis if a["path"] == path]

    # -- OpenAPI --
    async def handle_token(self, request):
        body = await request.json()
        self.apis.append({"path": "/v1.0/oauth2/accessToken", "body": body})
        if body.get("appKey") != self.client_id or body.get("appSecret") != self.client_secret:
            return web.json_response(
                {"code": "Forbidden.AccessDenied", "message": "appKey or appSecret is invalid"}, status=401
            )
        return web.json_response({"accessToken": "tt-fake-access-token", "expireIn": 7200})

    async def handle_open(self, request):
        body = await request.json()
        self.apis.append({"path": "/v1.0/gateway/connections/open", "body": body})
        self.subscriptions = list(body.get("subscriptions") or [])
        if body.get("clientId") != self.client_id or body.get("clientSecret") != self.client_secret:
            return web.json_response({"code": "Unauthorized", "message": "clientId or clientSecret is wrong"}, status=401)
        self.tickets += 1
        ticket = f"dt-ticket-{self.tickets}"
        self.issued_tickets.append(ticket)
        return web.json_response({"endpoint": f"ws://127.0.0.1:{self.port}/wsconnect", "ticket": ticket})

    async def handle_webhook(self, request):
        body = await request.json()
        self.replies.append(body)
        return web.json_response({"errcode": 0, "errmsg": "ok", "messageId": f"fake-reply-{len(self.replies)}"})

    # -- 附件（D-41 收图片：downloadCode → downloadUrl → 字节） --
    async def handle_file_download(self, request):
        """照 SDK chatbot.get_image_download_url：体 {robotCode, downloadCode}，头 x-acs-dingtalk-access-token"""
        body = await request.json()
        self.apis.append(
            {
                "path": "/v1.0/robot/messageFiles/download",
                "body": body,
                "tokenHeader": request.headers.get("x-acs-dingtalk-access-token") or "",
            }
        )
        code = str(body.get("downloadCode") or "")
        if not code:
            return web.json_response({"code": "InvalidParameter", "message": "downloadCode is required"}, status=400)
        return web.json_response({"downloadUrl": f"{self.openapi_base}/media/{code}"})

    async def handle_media_bytes(self, request):
        code = request.match_info["code"]
        self.media_hits.append(code)
        if code in self.media_fail:
            return web.Response(status=403, text="denied")
        ctype, payload = self.media.get(code, DEFAULT_MEDIA)
        return web.Response(body=payload, content_type=ctype)

    # -- 反向连接 --
    async def handle_inject(self, request):
        """往最近一条在线连接注入一条机器人消息，用于实机验证「IM 消息 → 任务 → 回贴」全链路

        可带 `msgtype`（text/picture/richText）、`at_me`（模拟群里没 @ 的分支）、
        `downloadCode`（换哪个附件）与 `conversationType`（1 单聊 / 2 群聊）。
        回执由该 socket 的读循环收（见 handle_ws），这里只等它把这一帧的回执递过来。
        """
        body = await request.json()
        live = [ws for ws in self.open_ws if not ws.closed]
        if not live:
            return web.json_response({"ok": False, "error": "no_live_socket", "sockets": self.sockets})
        self.injected += 1
        ws = live[-1]
        message_id = f"inject-{self.injected}"
        sink: asyncio.Queue = asyncio.Queue()
        self.inject_sinks[message_id] = sink
        try:
            await ws.send_json(frame("CALLBACK", ROBOT_TOPIC, robot_message(
                body.get("text") or "",
                webhook=self.webhook_url,
                msgtype=str(body.get("msgtype") or "text"),
                at_me=bool(body.get("at_me", True)),
                download_code=str(body.get("downloadCode") or "fake-download-code"),
                conversation_type=str(body.get("conversationType") or "2"),
            ), message_id))
            try:
                reply = await asyncio.wait_for(sink.get(), timeout=30)
            except asyncio.TimeoutError:
                return web.json_response({"ok": False, "error": "no_ack", "messageId": message_id}, status=504)
        finally:
            self.inject_sinks.pop(message_id, None)
        return web.json_response({"ok": True, "injected": self.injected, "tickets": self.tickets})

    async def handle_ws(self, request):
        self.sockets += 1
        scripted = self.sockets == 1  # 只有第一条连接跑完整剧本，后续连接接 /inject
        ws = web.WebSocketResponse(heartbeat=None)
        await ws.prepare(request)
        self.seen_tickets.append(request.query.get("ticket") or "")
        self.open_ws.append(ws)
        if not scripted:
            async for msg in ws:
                if msg.type is not WSMsgType.TEXT:
                    break
                try:
                    payload = json.loads(msg.data)
                except Exception:
                    continue
                self.acks.append(payload)
                sink = self.inject_sinks.get(str((payload.get("headers") or {}).get("messageId") or ""))
                if sink is not None:
                    sink.put_nowait(payload)
            return ws
        # 1) SYSTEM ping：只需回执，不进任务
        await ws.send_json(frame("SYSTEM", "ping", {}, "sys-ping-1"))
        self.acks.append(json.loads(await ws.receive_str()))
        # 2) 用户文本消息：进任务并回贴
        await ws.send_json(frame("CALLBACK", ROBOT_TOPIC, robot_message("帮我把季度总结整理成一页", webhook=self.webhook_url), "cb-msg-1"))
        self.acks.append(json.loads(await ws.receive_str()))
        # 3) 机器人自己贴回去的回声：不能触发第二个任务
        await ws.send_json(
            frame(
                "CALLBACK",
                ROBOT_TOPIC,
                robot_message("上一轮的回复回声", webhook=self.webhook_url, sender_id=CHATBOT_ID),
                "cb-echo",
            )
        )
        self.acks.append(json.loads(await ws.receive_str()))
        # 4) 群内图片但没 @ 机器人：消息确实到了（进台账），按群策略不起任务也不回贴
        await ws.send_json(
            frame(
                "CALLBACK",
                ROBOT_TOPIC,
                robot_message("", msgtype="picture", webhook=self.webhook_url, at_me=False),
                "cb-picture",
            )
        )
        self.acks.append(json.loads(await ws.receive_str()))
        # 5) 已知类型但非机器人消息的 topic（卡片回调）：忽略
        await ws.send_json(frame("CALLBACK", "/v1.0/card/instances/callback", {"outTrackId": "t1"}, "cb-card"))
        self.acks.append(json.loads(await ws.receive_str()))
        # 6) 组织事件帧：本通道不消费
        await ws.send_json(frame("EVENT", "*", {"eventType": "bpms_task_change"}, "ev-1"))
        self.acks.append(json.loads(await ws.receive_str()))
        # 7) 钉钉主动要求换连接地址
        await ws.send_json(frame("SYSTEM", "disconnect", {}, "sys-disconnect"))
        await ws.close()
        return ws


def main() -> None:
    parser = argparse.ArgumentParser(description="本地假钉钉（供界面实机验证）")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    fake = FakeDingTalk()
    fake.start(args.port or None)
    print(f"DINGTALK_OPENAPI_ENDPOINT={fake.openapi_base}", flush=True)
    print(f"TT_FAKE_DINGTALK_CLIENT_ID={fake.client_id}", flush=True)
    print(f"TT_FAKE_DINGTALK_CLIENT_SECRET={fake.client_secret}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        fake.stop()


if __name__ == "__main__":
    main()
