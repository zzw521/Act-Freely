"""
通天办公 - 钉钉 Stream 通道（官方 SDK 的协议实现 + 框架共用连接循环）

不重抄字段名，协议事实取自官方 dingtalk_stream 0.24.3（读的是它的 stream.py /
frames.py / chatbot.py / handlers.py，不是凭记忆）：
- 换连接地址：POST {endpoint}/v1.0/gateway/connections/open，凭据在请求体里
  （clientId/clientSecret/subscriptions/ua/localIp），返回 {endpoint, ticket}；
  反向连接地址 = {endpoint}?ticket={quote(ticket)}（SDK 也是这么拼的）
- 帧分三类：SYSTEM / EVENT / CALLBACK，`data` 是 JSON 字符串（要多解一层），
  机器人消息走 CALLBACK + topic /v1.0/im/bot/messages/get
- 回执：{code, headers:{messageId, contentType}, message, data} —— data 还要再序列化一次
- SYSTEM + topic=disconnect：钉钉主动要求换地址重连
- 回贴：POST 消息自带的 sessionWebhook，载荷 {msgtype, text:{content}, at:{atUserIds}}

为什么不跑 SDK 的 DingTalkStreamClient.start()：它自带 while True + websockets 客户端
+ 阻塞 requests，和引擎的 uvicorn 循环、以及框架已有的退避重连/台账/状态机会变成
「两套重连、两个循环」。所以这里只借它的协议值对象（CallbackMessage / SystemMessage /
ChatbotMessage / AckMessage / Headers / topic 常量），传输仍由框架的 aiohttp 会话承担，
钉钉与 Slack 因此共用同一条循环。Mock 开关也沿用 SDK 自己的那一个：环境变量
DINGTALK_OPENAPI_ENDPOINT（SDK 的 utils.py 读的就是它），集成自测用本机假钉钉顶掉它，
把「换地址 → 连 wss → 逐帧回执 → 收消息 → sessionWebhook 回贴 → disconnect 重连」真跑一遍。

诚实边界：现在没有真实钉钉企业凭据，所以这条链路只在本地对拍验证过，界面不能说「已打通」。
clientSecret 只在换地址那一次进请求体，绝不进视图、日志或错误详情。
"""

import os
import platform
import time
from urllib.parse import quote_plus

from im_channels import CredentialField, ImChannel, ImError, InboundEvent, OpenTicket, register

try:  # 官方 SDK 是这条通道的协议来源；随包运行时缺它就不能假装支持
    from dingtalk_stream import (
        AckMessage,
        CallbackMessage,
        ChatbotMessage,
        EventMessage,
        SystemMessage,
    )
    from dingtalk_stream.frames import Headers
    from dingtalk_stream.version import VERSION_STRING

    SDK_IMPORT_ERROR = ""
except Exception as exc:  # 缺依赖时通道如实落到「尚未接入」
    AckMessage = CallbackMessage = ChatbotMessage = EventMessage = None
    Headers = SystemMessage = None
    VERSION_STRING = ""
    SDK_IMPORT_ERROR = f"引擎运行时未安装官方 dingtalk-stream SDK（{exc}），无法按钉钉 Stream 协议收发"

DEFAULT_OPENAPI_ENDPOINT = "https://api.dingtalk.com"
# 回贴地址随每条消息下发，攒着不用会无限增长；只保留最近这些会话的最后一发
MAX_REPLY_TARGETS = 64


def openapi_base() -> str:
    """与 SDK 同一个注入点：utils.DINGTALK_OPENAPI_ENDPOINT 也是读这个环境变量"""
    return (os.environ.get("DINGTALK_OPENAPI_ENDPOINT") or DEFAULT_OPENAPI_ENDPOINT).rstrip("/")


def _mask(secret_owner: str) -> str:
    """账号信息里只留足以分辨「绑的是哪个应用」的头尾"""
    if len(secret_owner) <= 8:
        return secret_owner[:2] + "…"
    return f"{secret_owner[:4]}…{secret_owner[-2:]}"


def _looks_like_auth_error(code: str) -> bool:
    """命中即认为是凭据问题：框架据此停止退避重试，等用户改配置而不是死磕"""
    lowered = code.lower()
    hints = ("unauthorized", "forbidden", "denied", "authentication", "invalid_app", "appkey", "secret", "token")
    if any(h in lowered for h in hints):
        return True
    return lowered in {"40001", "40014", "40089", "42001"}  # oapi 侧的鉴权类数字码


@register
class DingTalkStreamChannel(ImChannel):
    kind = "dingtalk"
    label = "钉钉"
    # SDK 没装进随包运行时就退回「尚未接入」，不给能点但必然失败的控件
    supported = SDK_IMPORT_ERROR == ""
    setup_url = "https://open-dev.dingtalk.com/"
    setup_note = (
        "钉钉开发者后台创建企业内部应用 → 添加机器人 → 「消息接收模式」选 Stream 模式"
        "（与 Slack Socket Mode 同构：换取 wss 反向连接 + 逐帧回执，不需要公网回调地址）"
        "→ 复制 AppKey（Client ID）与 AppSecret。应用需有机器人发消息权限。"
    )
    fields = (
        CredentialField(
            key="clientId",
            label="AppKey（Client ID）",
            placeholder="dingxxxxxxxxxxxxxxxx",
            help_url="https://open-dev.dingtalk.com/",
            note="企业内部应用凭证页，用于换取反向连接地址与 accessToken",
        ),
        CredentialField(
            key="clientSecret",
            label="AppSecret（Client Secret）",
            placeholder="粘贴完整 AppSecret",
            help_url="https://open-dev.dingtalk.com/",
            note="只进钉钉 OpenAPI 请求体，不回显、不写日志",
        ),
    )

    def __init__(self):
        # conversationId -> 该会话最后一次下发的回贴地址
        self._reply_targets: dict[str, dict] = {}

    @property
    def unsupported_reason(self) -> str:
        return SDK_IMPORT_ERROR or super().unsupported_reason

    # ── ①凭据校验：两次真实往返，格式过了不代表 Stream 模式开着 ──────

    async def verify(self, creds: dict) -> dict:
        client_id, client_secret = self._require(creds)
        token = await self.post_json(
            f"{openapi_base()}/v1.0/oauth2/accessToken",
            payload={"appKey": client_id, "appSecret": client_secret},
            extra_headers=self._sdk_headers(),
        )
        self._check(token, "获取 accessToken（AppKey/AppSecret 校验）")
        if not str(token.get("accessToken") or ""):
            raise ImError("IM_BAD_RESPONSE", "钉钉没有返回 accessToken")
        conn = await self._connections_open(client_id, client_secret)
        self._check(conn, "gateway/connections.open（Stream 模式与机器人订阅校验）")
        if not str(conn.get("endpoint") or ""):
            raise ImError("IM_BAD_RESPONSE", "钉钉没有返回长连接地址，请在后台确认已选 Stream 模式")
        return {
            "appKey": _mask(client_id),
            "apiBase": openapi_base(),
            "tokenExpireIn": int(token.get("expireIn") or 0),
            "subscribeTopic": ChatbotMessage.TOPIC,
        }

    def _require(self, creds: dict) -> tuple[str, str]:
        client_id = str(creds.get("clientId") or "").strip()
        client_secret = str(creds.get("clientSecret") or "").strip()
        if not client_id or not client_secret:
            raise ImError("IM_CREDENTIAL_MISSING", "钉钉需要 AppKey 与 AppSecret 两项凭据")
        if len(client_secret) < 16:
            raise ImError(
                "IM_CREDENTIAL_INVALID",
                f"AppSecret 长度只有 {len(client_secret)}，钉钉的 AppSecret 是 32 位以上，请回后台复制完整值",
            )
        return client_id, client_secret

    # ── ②换连接地址 ─────────────────────────────────────────────────

    async def open_connection(self, creds: dict) -> OpenTicket:
        client_id, client_secret = self._require(creds)
        conn = await self._connections_open(client_id, client_secret)
        self._check(conn, "gateway/connections.open")
        endpoint = str(conn.get("endpoint") or "")
        ticket = str(conn.get("ticket") or "")
        if not endpoint.startswith(("ws://", "wss://")) or not ticket:
            raise ImError("IM_BAD_RESPONSE", "钉钉返回的长连接地址不完整，请确认应用已开启 Stream 模式")
        # SDK 的拼法：uri = f'{connection["endpoint"]}?ticket={quote_plus(connection["ticket"])}'
        return OpenTicket(
            ws_url=f"{endpoint}?ticket={quote_plus(ticket)}",
            info={"apiBase": openapi_base(), "subscribeTopic": ChatbotMessage.TOPIC},
        )

    async def _connections_open(self, client_id: str, client_secret: str) -> dict:
        topics = [{"type": "CALLBACK", "topic": ChatbotMessage.TOPIC}]
        return await self.post_json(
            f"{openapi_base()}/v1.0/gateway/connections/open",
            payload={
                "clientId": client_id,
                "clientSecret": client_secret,
                "subscriptions": topics,
                "ua": f"dingtalk-sdk-python/v{VERSION_STRING}-union",
                "localIp": self._host_ip(),
            },
            extra_headers=self._sdk_headers(),
        )

    @staticmethod
    def _host_ip() -> str:
        """SDK 在 open_connection 里带上 localIp；取不到就留空，不该因此让连接失败"""
        import socket

        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("223.5.5.5", 80))
            return str(s.getsockname()[0])
        except Exception:
            return ""
        finally:
            if s is not None:
                s.close()

    @staticmethod
    def _sdk_headers() -> dict:
        # stream.py open_connection 的请求头，UA 里带 SDK 版本便于钉钉侧排查
        return {
            "Accept": "application/json",
            "User-Agent": (
                "DingTalkStream/1.0 SDK/%s Python/%s (+https://github.com/open-dingtalk/dingtalk-stream-sdk-python)"
                % (VERSION_STRING, platform.python_version())
            ),
        }

    # ── ③解析事件帧 ─────────────────────────────────────────────────

    def parse_event(self, frame: dict) -> InboundEvent:
        frame_type = str(frame.get("type") or "")
        if frame_type == SystemMessage.TYPE:
            msg = SystemMessage.from_dict(frame)
            if msg.headers.topic == SystemMessage.TOPIC_DISCONNECT:
                return InboundEvent(kind="reconnect")
            # ping/其他 SYSTEM 帧只需回执，不进任务
            return InboundEvent(kind="ignore")
        if frame_type != CallbackMessage.TYPE:
            # EVENT 帧是通讯录/审批等组织事件，本通道只消费机器人消息
            return InboundEvent(kind="ignore")
        msg = CallbackMessage.from_dict(frame)
        if msg.headers.topic != ChatbotMessage.TOPIC:
            return InboundEvent(kind="ignore")
        bot = ChatbotMessage.from_dict(msg.data or {})
        conversation_id = str(bot.conversation_id or "").strip()
        text = self._text_of(bot)
        if not conversation_id or not text:
            return InboundEvent(kind="ignore")
        if bot.sender_id and bot.sender_id == bot.chatbot_user_id:
            # 机器人自己贴回去的消息不能触发第二个任务
            return InboundEvent(kind="ignore")
        self._remember_reply_target(conversation_id, bot)
        return InboundEvent(
            kind="inbound",
            text=text,
            conversation_id=conversation_id,
            user_id=str(bot.sender_staff_id or bot.sender_id or ""),
            message_id=str(bot.message_id or ""),
        )

    @staticmethod
    def _text_of(bot) -> str:
        # 钉钉的机器人回调按 msgtype 分形状：text 有 text.content，picture/richText 走下载码，
        # 本通道只把文本消息当任务，其它形状如实忽略而不是猜
        if getattr(bot, "message_type", "") != "text":
            return ""
        content = getattr(bot, "text", None)
        return str(getattr(content, "content", "") or "").strip()

    def _remember_reply_target(self, conversation_id: str, bot) -> None:
        webhook = str(getattr(bot, "session_webhook", "") or "")
        if not webhook.startswith(("http://", "https://")):
            # 回贴地址必须是钉钉下发的绝对 URL，不接受任意目标（防被指到内网其他服务）
            return
        self._reply_targets[conversation_id] = {
            "webhook": webhook,
            "atUserId": str(getattr(bot, "sender_staff_id", "") or ""),
            "expiresAtMs": int(getattr(bot, "session_webhook_expired_time", 0) or 0),
        }
        while len(self._reply_targets) > MAX_REPLY_TARGETS:
            self._reply_targets.pop(next(iter(self._reply_targets)))

    # ── ④逐帧回执（形状由 SDK 的 AckMessage/Headers 给） ──────────────

    async def ack(self, ws, frame: dict, ev: InboundEvent) -> None:
        frame_type = str(frame.get("type") or "")
        if frame_type == SystemMessage.TYPE:
            incoming = SystemMessage.from_dict(frame)
        elif frame_type == CallbackMessage.TYPE:
            incoming = CallbackMessage.from_dict(frame)
        elif frame_type == EventMessage.TYPE:
            incoming = EventMessage.from_dict(frame)
        else:
            return
        ack = AckMessage()
        ack.code = AckMessage.STATUS_OK
        ack.headers.content_type = Headers.CONTENT_TYPE_APPLICATION_JSON
        ack.headers.message_id = incoming.headers.message_id
        ack.message = "OK"
        # handlers.py：CALLBACK 的 data 是 {"response": ...}，SYSTEM/EVENT 原样带回 data
        ack.data = {"response": "OK"} if frame_type == CallbackMessage.TYPE else (incoming.data or {})
        await ws.send_json(ack.to_dict())

    # ── ⑤回贴：sessionWebhook（与 SDK ChatbotHandler.reply_text 同载荷） ──

    async def send(self, creds: dict, conversation_id: str, text: str) -> None:
        target = self._reply_targets.get(conversation_id)
        if target is None:
            raise ImError(
                "IM_REPLY_FAILED",
                "钉钉只能用对方消息里下发的 sessionWebhook 回贴，本会话还没有可用回贴地址",
            )
        expires = int(target.get("expiresAtMs") or 0)
        if expires and expires < int(time.time() * 1000):
            raise ImError("IM_REPLY_FAILED", "钉钉的 sessionWebhook 已过期，请让对方再发一条消息")
        resp = await self.post_json(
            str(target["webhook"]),
            payload={
                "msgtype": "text",
                "text": {"content": text},
                "at": {"atUserIds": [target.get("atUserId") or ""]},
            },
        )
        code = self._error_of(resp)
        if code:
            detail = str(resp.get("errmsg") or resp.get("message") or "")
            raise ImError("IM_REPLY_FAILED", f"回贴失败（钉钉 errcode={code} {detail}）".strip())

    # ── 共用：钉钉用 {code, message} 表达失败，HTTP 码不一定非 2xx ──────

    @staticmethod
    def _error_of(resp: dict) -> str:
        code = resp.get("errcode")
        if code is None:
            code = resp.get("code")
        if code is None:
            return ""
        text = str(code)
        return "" if text in ("0", "ok", "OK") else text

    def _check(self, resp: dict, action: str) -> None:
        code = self._error_of(resp)
        if not code:
            return
        detail = str(resp.get("message") or resp.get("errmsg") or "")
        code_name = "IM_AUTH_FAILED" if _looks_like_auth_error(code) else "IM_API_ERROR"
        # 只带钉钉的错误码与文案：响应体里可能含 accessToken，不整包塞进 lastError
        raise ImError(code_name, f"{action} 失败（钉钉 code={code}{f' {detail}' if detail else ''}）")
