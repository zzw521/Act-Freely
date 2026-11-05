"""
通天办公 - 个人微信通道（官方 iLink 机器人协议，长轮询 HTTP）

协议事实取自腾讯官方 npm 包 @tencent-weixin/openclaw-weixin 2.4.10
（package.json 的 ilink_appid="bot"；读的是它的 src/auth/login-qr.ts、
src/api/api.ts、src/api/types.ts、src/messaging/send.ts、src/monitor/monitor.ts），
不是凭记忆写的：

- 扫码登录：POST {base}/ilink/bot/get_bot_qrcode?bot_type=3（body 只有 local_token_list），
  回 qrcode 与 qrcode_img_content；再 GET {base}/ilink/bot/get_qrcode_status?qrcode=..，
  服务端把请求挂到 35 秒或状态变化为止。status 有 wait / scaned / need_verifycode /
  verify_code_blocked / expired / scaned_but_redirect / binded_redirect / confirmed，
  只有 confirmed 那一帧才下发 bot_token、ilink_bot_id、ilink_user_id、baseurl。
- 收消息：POST {base}/ilink/bot/getupdates，body {get_updates_buf, base_info}，长轮询；
  ret/errcode 非 0 是失败，-14 表示登录态过期（官方会暂停 30 分钟再试，这里如实让用户重扫）。
  get_updates_buf 是服务端给的游标，要原样带回下一轮；丢了会重放历史消息，所以本通道
  按 ilink_bot_id 落盘一份游标，并按 message_id 排重。
- 发消息：POST {base}/ilink/bot/sendmessage，msg 里 message_type=2（机器人）、
  message_state=2（已完成）、item_list 只有文本项 type=1；context_token 是回贴凭据，
  用户 24 小时没说话就失效（官方约束），失效时如实报 IM_REPLY_FAILED，不假装已送达。
- 请求头固定：AuthorizationType: ilink_bot_token、iLink-App-Id: bot、
  iLink-App-ClientVersion: uint32(0x00MMNNPP)、X-WECHAT-UIN: base64(随机 uint32 的十进制串)、
  有登录态时再带 Authorization: Bearer <bot_token>。
- 官方限制：只支持单聊（没有群聊），机器人不能主动发起会话，文本单条上限 4000 字。

诚实边界：没有真实微信机器人凭据做过端到端验收，本实现只在 tests/integration/
fake_wechat.py 的本机假服务端上对拍过协议形状；界面文案不得说成「已打通」。
bot_token / context_token 只进内存与 0600 配置文件，绝不进视图、日志或错误详情。

Mock 开关：TT_WECHAT_BASE_URL 覆盖协议基址（默认 https://ilinkai.weixin.qq.com），
TT_WECHAT_POLL_SECONDS / TT_WECHAT_LOGIN_POLL_SECONDS 压缩长轮询等待时间供自测使用。
"""

import asyncio
import base64
import json
import os
import random
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import aiohttp

import data_paths as dp
from im_channels import ImChannel, ImError, InboundEvent, OpenTicket, register

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
# 官方包 2.4.10 -> uint32(0x0002040A)
ILINK_APP_CLIENT_VERSION = (2 << 16) | (4 << 8) | 10
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.4.10"
BOT_AGENT = "TongtianOffice/1.0"

QR_TTL_SECONDS = 300
MAX_LOGIN_SESSIONS = 4
TEXT_LIMIT = 4000
MAX_SEEN_MSGIDS = 256
MAX_CONTEXT_TOKENS = 64
STALE_TOKEN_ERRCODE = -14


class PollTimeout(Exception):
    """客户端侧长轮询超时：正常控制流，不是错误"""


def base_url() -> str:
    return (os.environ.get("TT_WECHAT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _seconds(env: str, default: float) -> float:
    raw = os.environ.get(env) or ""
    try:
        value = float(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def _mask(value: str) -> str:
    if len(value) <= 8:
        return value[:2] + "…"
    return f"{value[:4]}…{value[-2:]}"


def _wechat_uin() -> str:
    return base64.b64encode(str(random.getrandbits(32)).encode("utf-8")).decode("ascii")


def _sync_file() -> Path:
    return dp.engine_home() / "im_wechat_sync.json"


def _load_cursor(bot_id: str) -> str:
    try:
        data = json.loads(_sync_file().read_text(encoding="utf-8"))
    except Exception:
        return ""
    value = data.get(bot_id) if isinstance(data, dict) else None
    return str(value or "")


def _save_cursor(bot_id: str, cursor: str) -> None:
    path = _sync_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data[bot_id] = cursor
    for stale in list(data)[:-8]:
        data.pop(stale, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _base_info() -> dict:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


def _api_error(body: dict) -> str:
    return f"ret={body.get('ret')} errcode={body.get('errcode')} errmsg={body.get('errmsg') or '（无）'}"


@register
class WeChatILinkChannel(ImChannel):
    kind = "wechat"
    label = "微信"
    supported = True
    transport = "long_poll"
    login_mode = "qr"
    setup_url = "https://weixin.qq.com/"
    setup_note = (
        "点「扫码绑定」后用手机微信扫码并按提示确认；这是腾讯官方的 iLink 机器人协议，"
        "不是网页版微信或任何非官方协议。只支持单聊，机器人不能主动发起会话；"
        "用户 24 小时没说话后回贴窗口会失效。"
    )
    # 长轮询没有 socket，传输层心跳无从谈起；登录态由服务端按请求校验
    protocol_heartbeat = None
    # 凭据由扫码确认帧下发，没有可手填的字段（配置接口也会直接拒绝手填）
    fields = ()
    # 官方协议只有单聊（setup_note 里那条限制），既没有群，也就没有「群里被 @」这回事
    mention_note = (
        "微信 iLink 机器人协议只有单聊、没有群聊，所以本通道不存在群内触发与 @ 判定："
        "单聊消息按「单聊触发」策略处理，群内触发的设置对本通道不生效。"
    )

    def __init__(self):
        self._logins: dict[str, dict] = {}
        self._cursor = ""
        self._cursor_bot = ""
        self._context_tokens: dict[str, str] = {}
        self._seen_msgids: list[str] = []

    @property
    def unsupported_reason(self) -> str:
        return ""

    # ── HTTP 底座 ────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        base: str = "",
        token: str = "",
        payload: dict | None = None,
        timeout: float = 15.0,
    ) -> dict:
        url = f"{(base or base_url()).rstrip('/')}/{endpoint.lstrip('/')}"
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
            "X-WECHAT-UIN": _wechat_uin(),
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with self.runtime.http.request(
                method, url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                text = await resp.text()
                status = resp.status
        except asyncio.TimeoutError as exc:
            raise PollTimeout(str(exc)) from None
        except aiohttp.ClientError as exc:
            raise ImError("IM_CONNECTION_FAILED", f"连接微信接口失败（{exc}）") from None
        if status >= 400:
            raise ImError("IM_CONNECTION_FAILED", f"微信接口返回 HTTP {status}：{text[:200]}")
        try:
            body = json.loads(text)
        except Exception:
            raise ImError("IM_BAD_RESPONSE", f"微信接口返回体不是 JSON（HTTP {status}）") from None
        if not isinstance(body, dict):
            raise ImError("IM_BAD_RESPONSE", "微信接口返回体不是对象")
        return body

    def _require(self, creds: dict) -> tuple[str, str, str]:
        token = str(creds.get("botToken") or "").strip()
        bot_id = str(creds.get("ilinkBotId") or "").strip()
        base = str(creds.get("baseUrl") or "").strip() or base_url()
        if not token or not bot_id:
            raise ImError("IM_LOGIN_REQUIRED", "个人微信还没有扫码绑定，请先扫码授权")
        return token, bot_id, base

    # ── ①扫码登录 ───────────────────────────────────────────────────

    async def login_start(self) -> dict:
        body = await self._request(
            "POST", "ilink/bot/get_bot_qrcode?bot_type=3", payload={"local_token_list": []}
        )
        qrcode = str(body.get("qrcode") or "")
        content = str(body.get("qrcode_img_content") or "")
        if not qrcode or not content:
            raise ImError("IM_LOGIN_FAILED", f"微信没有返回二维码（{_api_error(body)}）")
        now = time.time()
        for ticket in [k for k, v in self._logins.items() if now - v["atMs"] > QR_TTL_SECONDS]:
            self._logins.pop(ticket, None)
        for stale in list(self._logins)[: max(0, len(self._logins) - MAX_LOGIN_SESSIONS + 1)]:
            self._logins.pop(stale, None)
        self._logins[qrcode] = {"base": base_url(), "atMs": now}
        return {
            "ticket": qrcode,
            "qrContent": content,
            "expiresInSeconds": QR_TTL_SECONDS,
        }

    async def login_poll(self, ticket: str, verify_code: str = "") -> dict:
        session = self._logins.get(ticket)
        if session is None:
            raise ImError("IM_LOGIN_EXPIRED", "二维码已失效，请重新生成")
        if time.time() - session["atMs"] > QR_TTL_SECONDS:
            self._logins.pop(ticket, None)
            return {"status": "expired", "message": "二维码已过期，请重新生成"}
        endpoint = f"ilink/bot/get_qrcode_status?qrcode={quote(ticket, safe='')}"
        if verify_code:
            endpoint += f"&verify_code={quote(verify_code, safe='')}"
        try:
            body = await self._request(
                "GET", endpoint, base=session["base"],
                timeout=_seconds("TT_WECHAT_LOGIN_POLL_SECONDS", 35.0),
            )
        except PollTimeout:
            return {"status": "wait", "message": "等待扫码"}
        status = str(body.get("status") or "wait")
        if status == "confirmed":
            token = str(body.get("bot_token") or "").strip()
            bot_id = str(body.get("ilink_bot_id") or "").strip()
            if not token or not bot_id:
                self._logins.pop(ticket, None)
                raise ImError("IM_LOGIN_FAILED", "微信确认了登录，但返回体里缺 bot_token 或 ilink_bot_id")
            self._logins.pop(ticket, None)
            # credentials 由框架落盘，不经过界面；account 只放脱敏事实
            return {
                "status": "confirmed",
                "message": "已绑定并连接",
                "credentials": {
                    "botToken": token,
                    "ilinkBotId": bot_id,
                    "ilinkUserId": str(body.get("ilink_user_id") or ""),
                    "baseUrl": str(body.get("baseurl") or session["base"]).rstrip("/"),
                },
                "account": {
                    "botId": _mask(bot_id),
                    "baseUrl": str(body.get("baseurl") or session["base"]),
                    "directMessageOnly": True,
                },
            }
        if status == "scaned_but_redirect":
            host = str(body.get("redirect_host") or "").strip()
            if host:
                session["base"] = f"https://{host}"
            return {"status": status, "message": "已扫码，正在切换接入点"}
        if status in ("expired", "verify_code_blocked", "binded_redirect"):
            self._logins.pop(ticket, None)
        messages = {
            "wait": "等待扫码",
            "scaned": "已扫码，请在手机上确认",
            "need_verifycode": "请输入手机微信显示的数字",
            "verify_code_blocked": "数字多次输错，二维码已作废，请重新生成",
            "expired": "二维码已过期，请重新生成",
            "binded_redirect": "这个微信已经绑定过本机机器人，无需重复绑定",
        }
        return {
            "status": status,
            "needVerifyCode": status == "need_verifycode",
            "message": messages.get(status, f"未知状态：{status}"),
        }

    # ── ②连接：长轮询没有 socket，这里只交账号事实 ──────────────────────

    async def open_connection(self, creds: dict) -> OpenTicket:
        _, bot_id, base = self._require(creds)
        if self._cursor_bot != bot_id:
            self._cursor = _load_cursor(bot_id)
            self._cursor_bot = bot_id
        return OpenTicket(
            ws_url="",
            info={
                "botId": _mask(bot_id),
                "baseUrl": base,
                "directMessageOnly": True,
                "protocol": "iLink 官方机器人",
            },
        )

    async def on_close(self) -> None:
        # context_token 是按用户发的回贴凭据（24 小时窗口），断线重连后仍然有效，不清；
        # 只在进程退出时随内存消失，绝不写盘
        pass

    # ── ③长轮询收消息 ─────────────────────────────────────────────────

    async def poll_once(self, creds: dict) -> list[InboundEvent]:
        token, bot_id, base = self._require(creds)
        try:
            body = await self._request(
                "POST", "ilink/bot/getupdates", base=base, token=token,
                payload={"get_updates_buf": self._cursor, "base_info": _base_info()},
                timeout=_seconds("TT_WECHAT_POLL_SECONDS", 35.0),
            )
        except PollTimeout:
            return []
        ret = body.get("ret")
        errcode = body.get("errcode")
        if (ret not in (None, 0)) or (errcode not in (None, 0)):
            if STALE_TOKEN_ERRCODE in (ret, errcode):
                raise ImError(
                    "IM_AUTH_FAILED",
                    "微信登录态已失效（errcode=-14），请重新扫码绑定",
                    retryable=False,
                )
            raise ImError("IM_CHANNEL_ERROR", f"微信收消息失败（{_api_error(body)}）")
        cursor = body.get("get_updates_buf")
        if isinstance(cursor, str) and cursor:
            self._cursor = cursor
            _save_cursor(bot_id, cursor)
        events: list[InboundEvent] = []
        for msg in body.get("msgs") or []:
            if not isinstance(msg, dict):
                continue
            ev = self.parse_event(msg)
            if ev.kind != "ignore":
                events.append(ev)
        return events

    def parse_event(self, frame: dict) -> InboundEvent:
        """一帧 = getupdates.msgs 里的一个 WeixinMessage"""
        if int(frame.get("message_type") or 0) != 1:
            # 2 是机器人自己发的回声；其它值不是用户消息，不猜
            return InboundEvent(kind="ignore")
        text_parts: list[str] = []
        for item in frame.get("item_list") or []:
            if not isinstance(item, dict) or int(item.get("type") or 0) != 1:
                continue  # 图片/语音/文件要 CDN 下载与解密，本通道只把文本当任务
            text = str((item.get("text_item") or {}).get("text") or "").strip()
            if text:
                text_parts.append(text)
        text = "\n".join(text_parts).strip()
        user_id = str(frame.get("from_user_id") or "").strip()
        if not text or not user_id:
            return InboundEvent(kind="ignore")
        message_id = str(frame.get("message_id") or "")
        if message_id and message_id in self._seen_msgids:
            return InboundEvent(kind="ignore")
        if message_id:
            self._seen_msgids.append(message_id)
            del self._seen_msgids[:-MAX_SEEN_MSGIDS]
        context_token = str(frame.get("context_token") or "").strip()
        if context_token:
            self._remember_context(user_id, context_token)
        return InboundEvent(
            kind="inbound",
            text=text,
            conversation_id=user_id,
            user_id=user_id,
            message_id=message_id,
            # 协议里没有群的概念（官方只支持单聊），所以这里是真的「不是群」，不是未知
            is_group=False,
            mentioned_me=None,
        )

    def _remember_context(self, user_id: str, context_token: str) -> None:
        self._context_tokens[user_id] = context_token
        if len(self._context_tokens) > MAX_CONTEXT_TOKENS:
            for stale in list(self._context_tokens)[: len(self._context_tokens) - MAX_CONTEXT_TOKENS]:
                self._context_tokens.pop(stale, None)

    # ── ④回贴：sendmessage，带该用户最近一次的 context_token ─────────────

    async def send(self, creds: dict, conversation_id: str, text: str) -> None:
        token, _, base = self._require(creds)
        context_token = self._context_tokens.get(conversation_id, "")
        if not context_token:
            raise ImError(
                "IM_REPLY_FAILED",
                "没有这个会话的 context_token（用户超过 24 小时没说话，或引擎重启过），"
                "官方协议下这条回贴发不出去；等对方再发一条消息即可恢复",
            )
        for chunk in [text[i : i + TEXT_LIMIT] for i in range(0, len(text), TEXT_LIMIT)] or [""]:
            body = await self._request(
                "POST", "ilink/bot/sendmessage", base=base, token=token,
                payload={
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": conversation_id,
                        "client_id": f"tongtian-{uuid.uuid4().hex}",
                        "message_type": 2,
                        "message_state": 2,
                        "item_list": [{"type": 1, "text_item": {"text": chunk}}],
                        "context_token": context_token,
                    },
                    "base_info": _base_info(),
                },
            )
            ret = body.get("ret")
            if ret not in (None, 0):
                raise ImError("IM_REPLY_FAILED", f"微信拒绝这条回贴（{_api_error(body)}）")
