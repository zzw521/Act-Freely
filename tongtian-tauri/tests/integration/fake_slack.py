#!/usr/bin/env python3
"""
假 Slack 服务端：Slack Socket Mode 协议的本地对端替身。

协议字段照 slack_sdk 3.45.0 的 socket_mode 实现（frames/envelope_id/challenge/
apps.connections.open/events_api/disconnect），所以引擎侧走的是真实 aiohttp
WebSocket 客户端与真实 HTTP 往返，不是打桩返回值。

两处消费方：
  tests/integration/engine_im_slack_selftest.py  —— 进程内起服务，跑完整三段断言
  python tests/integration/fake_slack.py --port 19600  —— 独立进程，供界面实机验证；
    独立进程模式下可 POST /inject {"text": "..."} 往在线连接注入一条用户消息，
    用来实机跑「IM 消息 → 真实任务 → 回贴」而不是只看剧本。

数据面只在本机 127.0.0.1，不联网、不落盘。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import threading
import time

from aiohttp import web

DEFAULT_APP_TOKEN = "xapp-1-TTFAKE-abcdef123456"
DEFAULT_BOT_TOKEN = "xoxb-TTFAKE-abcdef123456"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeSlack:
    """第一条 WebSocket 连接跑完整剧本（challenge→用户消息→bot 回声→disconnect），
    后续连接只保持在线，用来验证引擎在收到 disconnect 后会重新换取连接地址。"""

    def __init__(self, app_token: str = DEFAULT_APP_TOKEN, bot_token: str = DEFAULT_BOT_TOKEN):
        self.app_token = app_token
        self.bot_token = bot_token
        self.apis: list[dict] = []      # 到达的 Web API 调用
        self.acks: list[dict] = []      # 客户端发回的帧
        self.replies: list[dict] = []   # chat.postMessage 内容
        self.open_ws: list = []         # 当前在线的反向连接
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
        assert self._ready.wait(10), "假 Slack 未能启动"

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
        app.router.add_post("/api/{method}", self.handle_api)
        app.router.add_get("/ws/{ticket}", self.handle_ws)
        app.router.add_post("/inject", self.handle_inject)
        self._runner = web.AppRunner(app)
        self._loop.run_until_complete(self._runner.setup())
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        self._loop.run_until_complete(site.start())
        self._ready.set()
        self._loop.run_forever()

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.port}/api"

    def calls(self, method: str) -> list[dict]:
        return [a for a in self.apis if a["method"] == method]

    # -- Web API --
    async def handle_api(self, request):
        method = request.match_info["method"]
        body = await request.json()
        auth = request.headers.get("Authorization", "")
        self.apis.append({"method": method, "auth": auth, "body": body})
        if method == "auth.test":
            if not auth.endswith(self.bot_token):
                return web.json_response({"ok": False, "error": "invalid_auth"})
            return web.json_response(
                {"ok": True, "team": "通天测试空间", "team_id": "T1", "user": "tt-bot", "bot_id": "B1"}
            )
        if method == "apps.connections.open":
            if not auth.endswith(self.app_token):
                return web.json_response({"ok": False, "error": "invalid_auth"})
            self.tickets += 1
            return web.json_response(
                {
                    "ok": True,
                    "url": f"ws://127.0.0.1:{self.port}/ws/{self.tickets}",
                    "app_id": "A1",
                    "team_id": "T1",
                }
            )
        if method == "chat.postMessage":
            if not auth.endswith(self.bot_token):
                return web.json_response({"ok": False, "error": "not_authed"})
            self.replies.append(body)
            return web.json_response({"ok": True, "ts": f"{time.time():.6f}", "channel": body.get("channel")})
        return web.json_response({"ok": False, "error": "unknown_method"})

    # -- Socket Mode 反向连接 --
    async def handle_inject(self, request):
        """往最近一条在线连接注入一条用户消息，用于实机验证「IM 消息 → 任务 → 回贴」全链路"""
        body = await request.json()
        live = [ws for ws in self.open_ws if not ws.closed]
        if not live:
            return web.json_response({"ok": False, "error": "no_live_socket", "sockets": self.sockets})
        self.injected += 1
        ws = live[-1]
        await ws.send_json(
            {
                "type": "events_api",
                "envelope_id": f"env-inject-{self.injected}",
                "payload": {
                    "type": "events_api",
                    "event": {
                        "type": "message",
                        "channel": body.get("channel") or "D0123",
                        "user": body.get("user") or "U0456",
                        "text": body.get("text") or "",
                        "ts": f"{time.time():.6f}",
                    },
                },
                "accepts_response_payload": False,
                "retry_attempt": 0,
            }
        )
        return web.json_response({"ok": True, "injected": self.injected, "ticket": self.tickets})

    async def handle_ws(self, request):
        self.sockets += 1
        scripted = self.sockets == 1  # 只有第一条连接跑完整剧本，后续连接保持在线
        ws = web.WebSocketResponse(heartbeat=None)
        await ws.prepare(request)
        self.open_ws.append(ws)
        await ws.send_json(
            {
                "type": "url_verification",
                "envelope_id": f"env-challenge-{request.match_info['ticket']}",
                "payload": {
                    "token": "verification-token",
                    "challenge": f"chal-{request.match_info['ticket']}",
                },
                "accepts_response_payload": True,
            }
        )
        self.acks.append(json.loads(await ws.receive_str()))
        if not scripted:
            async for _ in ws:
                pass
            return ws
        await ws.send_json(
            {
                "type": "events_api",
                "envelope_id": "env-msg-1",
                "payload": {
                    "type": "events_api",
                    "event": {
                        "type": "message",
                        "channel": "D0123",
                        "user": "U0456",
                        "text": "帮我把季度总结整理成一页",
                        "ts": "1700000000.000100",
                    },
                },
                "accepts_response_payload": False,
                "retry_attempt": 0,
            }
        )
        self.acks.append(json.loads(await ws.receive_str()))
        # 机器人自己的回贴：必须被忽略，不能触发第二个任务
        await ws.send_json(
            {
                "type": "events_api",
                "envelope_id": "env-bot-echo",
                "payload": {
                    "type": "events_api",
                    "event": {
                        "type": "message",
                        "channel": "D0123",
                        "bot_id": "B1",
                        "text": "上一轮的回复回声",
                        "ts": "1700000000.000200",
                    },
                },
            }
        )
        self.acks.append(json.loads(await ws.receive_str()))
        # Slack 主动要求换新连接地址
        await ws.send_json({"type": "disconnect", "envelope_id": "env-disconnect", "payload": {}})
        await ws.close()
        return ws


def main() -> None:
    parser = argparse.ArgumentParser(description="本地假 Slack（供界面实机验证）")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    fake = FakeSlack()
    fake.start(args.port or None)
    print(f"FAKE_SLACK_API_BASE={fake.api_base}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        fake.stop()


if __name__ == "__main__":
    main()
