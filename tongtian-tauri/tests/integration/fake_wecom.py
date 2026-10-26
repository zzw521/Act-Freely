#!/usr/bin/env python3
"""
假企业微信智能机器人网关：长连接模式的本地对端替身。

帧形状照两处权威来源，不自己发明字段：
- 官方 SDK wecom-aibot-python-sdk 1.0.2（ws.py 的订阅/心跳/回贴线形状与 errcode 回执，
  client.py 的 reply/reply_stream/send_message 请求体，types.py 的 WsCmd/MessageType）
- 企业微信开发者中心《智能机器人长连接》2026/10/10（document/path/101463）：
  aibot_subscribe / aibot_msg_callback / aibot_event_callback / aibot_respond_msg /
  aibot_send_msg / ping 的请求与响应示例，以及「群聊才有 chatid、单聊用 from.userid」、
  「msgid 需排重」「新连接订阅会踢掉旧连接并下发 disconnected_event」这三条约束

引擎侧走的是真实 aiohttp WebSocket 客户端与真实的带内往返，不是打桩返回值。替身按官方
示例发帧，引擎按官方示例解帧，两边只在 127.0.0.1 上对话，不联网、不落盘。

两处消费方：
  tests/integration/engine_im_wecom_selftest.py —— 进程内起服务，跑完整对拍
  python tests/integration/fake_wecom.py --port 19611 —— 独立进程，供界面实机验证；
    POST /inject {"text": "..."} 往在线连接注入一条用户消息，
    POST /behavior 可切换 reject_subscribe / ignore_pings / reply_errcode 三种故障，
    GET /state 看计数器（连接数、订阅次数、心跳数、回贴内容）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import threading
import time

from aiohttp import web

DEFAULT_BOT_ID = "tt-fake-wecom-bot-0001"
DEFAULT_SECRET = "ttfakewecomsecret0123456789abcdef00"
GROUP_CHAT_ID = "wrTTFAKEgroup0001"
SENDER_USERID = "zhangsan"
SECOND_USERID = "lisi"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def msg_callback(req_id: str, *, text: str = "", msgtype: str = "text", chattype: str = "group",
                 msgid: str, chatid: str = GROUP_CHAT_ID, userid: str = SENDER_USERID,
                 compat: dict | None = None, bot_id: str = DEFAULT_BOT_ID) -> dict:
    """《接收消息回调》的 body 形状：群聊才带 chatid，单聊靠 from.userid 定位会话"""
    body: dict = {
        "msgid": msgid,
        "aibotid": bot_id,
        "chattype": chattype,
        "from": {"userid": userid},
        "msgtype": msgtype,
    }
    if chattype == "group":
        body["chatid"] = chatid
    if msgtype == "text":
        body["text"] = {"content": text}
    elif msgtype == "image":
        body["image"] = {"url": "https://example.invalid/p.png", "aeskey": "FAKEAESKEY"}
    elif msgtype == "voice":
        body["voice"] = {"content": "语音转写出来的文本"}
    if compat is not None:
        body["compat_info"] = compat
    return {"cmd": "aibot_msg_callback", "headers": {"req_id": req_id}, "body": body}


def event_callback(req_id: str, eventtype: str, *, msgid: str, chattype: str = "single",
                   userid: str = SENDER_USERID, bot_id: str = DEFAULT_BOT_ID) -> dict:
    body: dict = {
        "msgid": msgid,
        "create_time": int(time.time()),
        "aibotid": bot_id,
        "chattype": chattype,
        "msgtype": "event",
        "event": {"eventtype": eventtype},
    }
    if chattype == "group":
        body["chatid"] = GROUP_CHAT_ID
    else:
        body["from"] = {"userid": userid}
    return {"cmd": "aibot_event_callback", "headers": {"req_id": req_id}, "body": body}


class FakeWeCom:
    """按订阅连接编号决定谁跑剧本：第 1 条是引擎的凭据探测连接（订阅完就关），
    第 2 条是真正的长连接，在它上面推完整序列；后续连接只保持在线供 /inject 使用。"""

    def __init__(self, bot_id: str = DEFAULT_BOT_ID, secret: str = DEFAULT_SECRET, script_on: int = 2):
        self.bot_id = bot_id
        self.secret = secret
        self.script_on = script_on
        self.subs: list[dict] = []
        self.pings: list[str] = []
        self.replies: list[dict] = []
        self.sends: list[dict] = []
        self.events_pushed = 0
        self.unexpected: list[dict] = []
        self.acks_sent: list[dict] = []
        self.open_ws: list = []
        self.sockets = 0
        self.port = 0
        # 故障开关（/behavior 可改）
        self.reject_subscribe = False
        self.ignore_pings = False
        self.reply_errcode = 0
        self._runner = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()

    # -- 生命周期 --
    def start(self, port: int | None = None):
        self.port = port or free_port()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        assert self._ready.wait(10), "假企业微信未能启动"

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
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    def live(self) -> list:
        return [ws for ws in self.open_ws if not ws.closed]

    # -- 引擎发来的帧：心跳要回 errcode，回贴要回 errcode，其余如实记账 --
    async def handle_client_frame(self, ws, frame: dict) -> None:
        cmd = str(frame.get("cmd") or "")
        req_id = str((frame.get("headers") or {}).get("req_id") or "")
        if cmd == "ping":
            self.pings.append(req_id)
            if self.ignore_pings:
                return  # 网关不响应：引擎应当判定连接已死并主动断开
        elif cmd == "aibot_respond_msg":
            self.replies.append(frame)
        elif cmd == "aibot_send_msg":
            self.sends.append(frame)
        else:
            self.unexpected.append(frame)
            return
        ack = {"headers": {"req_id": req_id}, "errcode": self.reply_errcode,
               "errmsg": "ok" if self.reply_errcode == 0 else "reply rejected by gateway"}
        self.acks_sent.append(ack)
        await ws.send_json(ack)

    async def handle_ws(self, request):
        self.sockets += 1
        ws = web.WebSocketResponse(heartbeat=None)
        await ws.prepare(request)
        self.open_ws.append(ws)

        first = json.loads(await ws.receive_str())
        if str(first.get("cmd") or "") != "aibot_subscribe":
            self.unexpected.append(first)
            await ws.close(code=1002, message=b"expect aibot_subscribe first")
            return ws
        self.subs.append(first)
        body = first.get("body") or {}
        req_id = str((first.get("headers") or {}).get("req_id") or "")
        creds_ok = body.get("bot_id") == self.bot_id and body.get("secret") == self.secret
        ok = creds_ok and not self.reject_subscribe
        await ws.send_json(
            {"headers": {"req_id": req_id}, "errcode": 0 if ok else 850004,
             "errmsg": "ok" if ok else "invalid bot_id or secret"}
        )
        if not ok:
            await ws.close()
            return ws

        if self.sockets == self.script_on:
            await self.run_script(ws)

        async for msg in ws:
            if msg.type is web.WSMsgType.TEXT:
                await self.handle_client_frame(ws, json.loads(msg.data))
            elif msg.type in (web.WSMsgType.CLOSED, web.WSMsgType.ERROR):
                break
        return ws

    async def run_script(self, ws) -> None:
        """官方协议里引擎该处理什么、该忽略什么，一次推完（推送不依赖引擎先说话）"""
        # 1) 群聊文本：起任务并等 errcode 回执
        await ws.send_json(msg_callback("cb-group-1", text="帮我把季度总结整理成一页", msgid="ttfake-msg-1"))
        # 2) 同一条消息重复回调（官方：msgid 用于事件排重）：不能再起一个任务
        await ws.send_json(msg_callback("cb-group-1-dup", text="帮我把季度总结整理成一页", msgid="ttfake-msg-1"))
        # 3) compat_info：非普通用户消息（智能助理调用），不该当任务
        await ws.send_json(msg_callback("cb-assistant", text="智能助理转来的调用", msgid="ttfake-msg-2",
                                        compat={"source": "ai_assistant"}))
        # 4) 图片消息：本通道不做 aeskey 下载解密，如实忽略
        await ws.send_json(msg_callback("cb-image", msgtype="image", msgid="ttfake-msg-3"))
        # 5) 单聊文本：没有 chatid，会话标识取 from.userid
        await ws.send_json(msg_callback("cb-single-1", text="把这份清单按人分一下", msgid="ttfake-msg-4",
                                        chattype="single", userid=SECOND_USERID))
        # 6) 事件回调：enter_chat 不是任务文本
        self.events_pushed += 1
        await ws.send_json(event_callback("ev-enter-1", "enter_chat", msgid="ttfake-ev-1"))
        # 7) 模板卡片点击：本通道没有卡片，如实忽略
        self.events_pushed += 1
        await ws.send_json(event_callback("ev-card-1", "template_card_event", msgid="ttfake-ev-2",
                                          chattype="group"))

    # -- HTTP 侧：实机验证用的注入与故障开关 --
    async def handle_inject(self, request):
        payload = await request.json()
        live = self.live()
        if not live:
            return web.json_response({"ok": False, "error": "no_live_socket", "sockets": self.sockets})
        n = len(self.replies) + len(self.sends) + 1
        await live[-1].send_json(
            msg_callback(f"inject-{int(time.time() * 1000)}", text=payload.get("text") or "你好",
                         msgid=f"ttfake-inject-{n}", chattype=str(payload.get("chattype") or "group"))
        )
        return web.json_response({"ok": True, "sockets": self.sockets, "pushed": n})

    async def handle_kick(self, request):
        """官方：新连接完成订阅后，旧连接收到 disconnected_event 并被服务端断开"""
        live = self.live()
        if not live:
            return web.json_response({"ok": False, "error": "no_live_socket"})
        await live[-1].send_json(event_callback("ev-kick", "disconnected_event", msgid="ttfake-kick-1"))
        await live[-1].close()
        return web.json_response({"ok": True})

    async def handle_behavior(self, request):
        patch = await request.json()
        for key in ("reject_subscribe", "ignore_pings"):
            if key in patch:
                setattr(self, key, bool(patch[key]))
        if "reply_errcode" in patch:
            self.reply_errcode = int(patch["reply_errcode"])
        if "script_on" in patch:
            self.script_on = int(patch["script_on"])
        return web.json_response({"ok": True, "reject_subscribe": self.reject_subscribe,
                                  "ignore_pings": self.ignore_pings, "reply_errcode": self.reply_errcode})

    async def handle_state(self, request):
        return web.json_response(
            {
                "sockets": self.sockets,
                "live": len(self.live()),
                "subscribe": len(self.subs),
                "pings": len(self.pings),
                "replies": [((r.get("body") or {}).get("stream") or {}).get("content") for r in self.replies],
                "sends": [((s.get("body") or {}).get("markdown") or {}).get("content") for s in self.sends],
                "unexpected": self.unexpected[:3],
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="本地假企业微信智能机器人网关（供界面实机验证）")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--script", type=int, default=2, help="在第几条连接上跑完整剧本（0=不跑）")
    args = parser.parse_args()
    fake = FakeWeCom(script_on=args.script if args.script > 0 else 10**9)
    fake.start(args.port or None)
    print(f"TT_WECOM_WS_URL={fake.ws_url}", flush=True)
    print(f"TT_FAKE_WECOM_BOT_ID={fake.bot_id}", flush=True)
    print(f"TT_FAKE_WECOM_SECRET={fake.secret}", flush=True)
    print(f"端口 {fake.port}：POST /inject {{\"text\":\"...\"}} · POST /kick · POST /behavior · GET /state", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        fake.stop()


if __name__ == "__main__":
    main()
