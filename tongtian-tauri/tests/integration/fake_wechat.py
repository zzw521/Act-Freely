#!/usr/bin/env python3
"""
假个人微信 iLink 机器人网关：长轮询 HTTP 的本地对端替身。

线形状照腾讯官方 npm 包 @tencent-weixin/openclaw-weixin 2.4.10（src/auth/login-qr.ts、
src/api/api.ts、src/api/types.ts、src/messaging/send.ts、src/monitor/monitor.ts）：
- POST ilink/bot/get_bot_qrcode?bot_type=3，body {local_token_list:[]}
  → {qrcode, qrcode_img_content}
- GET ilink/bot/get_qrcode_status?qrcode=..[&verify_code=..]，服务端挂起等待状态变化；
  wait / scaned / need_verifycode / verify_code_blocked / expired / scaned_but_redirect /
  binded_redirect / confirmed，只有 confirmed 才下发 bot_token、ilink_bot_id、ilink_user_id、baseurl
- POST ilink/bot/getupdates，body {get_updates_buf, base_info}，长轮询；
  → {ret:0, msgs:[...], get_updates_buf}；ret/errcode=-14 表示登录态过期（官方会暂停 30 分钟）
- POST ilink/bot/sendmessage，body {msg:{..., message_type:2, message_state:2, item_list, context_token}}，→ {ret:0}

引擎侧走的是真实 aiohttp 客户端与真实的 HTTP 往返，不是打桩返回值。替身校验
AuthorizationType / iLink-App-Id / iLink-App-ClientVersion / Authorization 这几个头，
回贴只认带 context_token 的请求，并可按开关返回 -14。

两处消费方：
  tests/integration/engine_im_wechat_selftest.py —— 进程内起服务，跑完整对拍
  python tests/integration/fake_wechat.py --port 19612 —— 独立进程，供界面实机验证；
    POST /inject {"text": "..."} 往下一轮 getupdates 塞一条用户消息，
    POST /behavior 切换 stale_token / reject_send / qr_statuses 三种故障，
    GET /state 看计数器（二维码请求数、收消息轮数、回贴内容、请求头快照）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import threading
import time

from aiohttp import web

DEFAULT_BOT_TOKEN = "ttfake-wx-bot-token-0123456789abcdef"
DEFAULT_BOT_ID = "ttfake-wx-bot-0001"
DEFAULT_USER_ID = "ttfake-wx-user-0001"
SECOND_USER_ID = "ttfake-wx-user-0002"
DEFAULT_QR_STATUSES = ["wait", "scaned", "confirmed"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def user_message(
    text: str,
    *,
    msgid: str,
    from_user: str = DEFAULT_USER_ID,
    context_token: str = "ttfake-ctx-1",
    message_type: int = 1,
    item_type: int = 1,
) -> dict:
    """WeixinMessage 的形状：message_type 1=USER 2=BOT；item_list 里 type 1=TEXT"""
    item = {"type": item_type}
    if item_type == 1:
        item["text_item"] = {"text": text}
    else:
        item["image_item"] = {"url": "https://example.invalid/p.png"}
    return {
        "message_id": msgid,
        "from_user_id": from_user,
        "to_user_id": DEFAULT_BOT_ID,
        "message_type": message_type,
        "message_state": 2,
        "context_token": context_token,
        "item_list": [item],
    }


class FakeWeChat:
    def __init__(
        self,
        bot_token: str = DEFAULT_BOT_TOKEN,
        bot_id: str = DEFAULT_BOT_ID,
        *,
        qr_statuses: list[str] | None = None,
        poll_wait_seconds: float = 0.2,
    ):
        self.bot_token = bot_token
        self.bot_id = bot_id
        self.qr_statuses = list(qr_statuses or DEFAULT_QR_STATUSES)
        self.poll_wait_seconds = poll_wait_seconds
        self.port = 0
        # 计数与快照
        self.qr_requests: list[dict] = []
        self.status_polls: list[dict] = []
        self.getupdates_calls: list[dict] = []
        self.getupdates_headers: dict = {}
        self.sends: list[dict] = []
        self.send_headers: dict = {}
        self.injections: list[dict] = []
        self.cursor = "ttfake-cursor-0"
        self.qr_index = 0
        # 故障开关
        self.stale_token = False
        self.reject_send = False
        self.pending_msgs: list[dict] = []
        self._runner = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()

    # -- 生命周期 --
    def start(self, port: int | None = None):
        self.port = port or free_port()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        assert self._ready.wait(10), "假微信网关未能启动"

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
        app.router.add_post("/ilink/bot/get_bot_qrcode", self.handle_qrcode)
        app.router.add_get("/ilink/bot/get_qrcode_status", self.handle_status)
        app.router.add_post("/ilink/bot/getupdates", self.handle_getupdates)
        app.router.add_post("/ilink/bot/sendmessage", self.handle_send)
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
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -- 通用头校验：官方包的四个固定头 --
    def _headers_ok(self, request) -> bool:
        h = request.headers
        return (
            h.get("AuthorizationType") == "ilink_bot_token"
            and h.get("iLink-App-Id") == "bot"
            and h.get("iLink-App-ClientVersion", "").isdigit()
            and h.get("X-WECHAT-UIN") != ""
        )

    # -- ① 二维码 --
    async def handle_qrcode(self, request):
        body = await request.json()
        ticket = f"ttfake-qr-{self.qr_index}"
        self.qr_index += 1
        self.qr_requests.append(
            {
                "bot_type": request.query.get("bot_type"),
                "body": body,
                "headers": dict(request.headers),
                "headers_ok": self._headers_ok(request),
            }
        )
        return web.json_response(
            {
                "qrcode": ticket,
                "qrcode_img_content": f"{self.base_url}/qr/{ticket}",
            }
        )

    # -- ② 扫码状态：按剧本一条一条给，最后一条一直重复 --
    async def handle_status(self, request):
        ticket = request.query.get("qrcode") or ""
        verify_code = request.query.get("verify_code") or ""
        self.status_polls.append({"ticket": ticket, "verify_code": verify_code})
        if not ticket:
            return web.json_response({"status": "expired"})
        if not self.qr_statuses:
            return web.json_response({"status": "wait"})
        status = self.qr_statuses.pop(0) if len(self.qr_statuses) > 1 else self.qr_statuses[0]
        if status == "confirmed":
            if verify_code and verify_code != "8642":
                return web.json_response({"status": "need_verifycode"})
            return web.json_response(
                {
                    "status": "confirmed",
                    "bot_token": self.bot_token,
                    "ilink_bot_id": self.bot_id,
                    "ilink_user_id": DEFAULT_USER_ID,
                    "baseurl": self.base_url,
                }
            )
        if status == "scaned_but_redirect":
            return web.json_response({"status": status, "redirect_host": "redirect.invalid"})
        return web.json_response({"status": status})

    # -- ③ 长轮询收消息 --
    async def handle_getupdates(self, request):
        body = await request.json()
        self.getupdates_calls.append(body)
        if not self.getupdates_headers:
            self.getupdates_headers = dict(request.headers)
        if self.stale_token:
            return web.json_response({"ret": -14, "errcode": -14, "errmsg": "login token expired"})
        await asyncio.sleep(self.poll_wait_seconds)
        if self.pending_msgs:
            msgs, self.pending_msgs = self.pending_msgs, []
            self.cursor = f"ttfake-cursor-{len(self.getupdates_calls)}"
            return web.json_response(
                {"ret": 0, "msgs": msgs, "get_updates_buf": self.cursor, "longpolling_timeout_ms": 35000}
            )
        return web.json_response(
            {"ret": 0, "msgs": [], "get_updates_buf": body.get("get_updates_buf") or self.cursor}
        )

    # -- ④ 回贴 --
    async def handle_send(self, request):
        body = await request.json()
        self.sends.append(body)
        self.send_headers = dict(request.headers)
        msg = body.get("msg") or {}
        if self.reject_send:
            return web.json_response({"ret": 2001, "errcode": 2001, "errmsg": "send rejected by gateway"})
        if not msg.get("context_token"):
            return web.json_response({"ret": 2002, "errcode": 2002, "errmsg": "missing context_token"})
        if not str(msg.get("to_user_id") or ""):
            return web.json_response({"ret": 2003, "errcode": 2003, "errmsg": "missing to_user_id"})
        return web.json_response({"ret": 0})

    # -- HTTP 侧：实机验证用的注入与故障开关 --
    async def handle_inject(self, request):
        payload = await request.json()
        n = int(time.time() * 1000)
        self.injections.append(payload)
        self.pending_msgs.append(
            user_message(
                str(payload.get("text") or "你好"),
                msgid=f"ttfake-inject-{n}",
                from_user=str(payload.get("fromUser") or DEFAULT_USER_ID),
            )
        )
        return web.json_response({"ok": True, "queued": len(self.pending_msgs)})

    async def handle_behavior(self, request):
        patch = await request.json()
        for key in ("stale_token", "reject_send"):
            if key in patch:
                setattr(self, key, bool(patch[key]))
        if "qr_statuses" in patch and isinstance(patch["qr_statuses"], list):
            self.qr_statuses = [str(s) for s in patch["qr_statuses"]] or ["wait"]
        return web.json_response(
            {"ok": True, "stale_token": self.stale_token, "reject_send": self.reject_send}
        )

    async def handle_state(self, request):
        return web.json_response(
            {
                "port": self.port,
                "qr": len(self.qr_requests),
                "status_polls": self.status_polls[-8:],
                "getupdates": len(self.getupdates_calls),
                "sends": [
                    {
                        "to": ((s.get("msg") or {}).get("to_user_id")),
                        "text": ((s.get("msg") or {}).get("item_list") or [{}])[0]
                        .get("text_item", {})
                        .get("text"),
                    }
                    for s in self.sends
                ],
                "pending": len(self.pending_msgs),
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="本地假个人微信 iLink 网关（供界面实机验证）")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--qr-statuses", default="wait,scaned,confirmed")
    args = parser.parse_args()
    fake = FakeWeChat(qr_statuses=[s for s in args.qr_statuses.split(",") if s])
    fake.start(args.port or None)
    print(f"TT_WECHAT_BASE_URL={fake.base_url}", flush=True)
    print(f"假网关端口 {fake.port}：POST /inject {{\"text\":\"...\"}} · POST /behavior · GET /state", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        fake.stop()


if __name__ == "__main__":
    main()
