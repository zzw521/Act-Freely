"""
通天办公 - Slack Socket Mode 通道（IM 多通道同构框架的第一个实现）

协议字段取自 slack_sdk 3.45.0 的 socket_mode/request.py、response.py、async_client.py
（读的是权威实现，不是凭记忆）：
- 换连接地址：POST {api_base}/apps.connections.open，Bearer xapp- App-Level Token → {"ok,url,...}
- 收到的每一帧：{"type","envelope_id","payload","accepts_response_payload","retry_attempt"}
- 回执：{"envelope_id": ...}；url_verification 需要带响应体 → {"envelope_id":...,"payload":{"challenge":...}}
- type == "disconnect"：Slack 主动要求换地址重连（不是掉线）
- 心跳：Socket Mode 要求客户端维持 ping/pong，这里交给 aiohttp 的 heartbeat=10s

api_base 可经 TT_SLACK_API_BASE 注入，集成自测因此能用本机假 Slack 把
「换地址 → 连 wss → challenge/ack → 收消息 → 回贴」整条链路真跑一遍；
真 token 到位后同一份代码只是换了 base 和凭据，不需要再改机制。
"""

import os
import re

from im_channels import CredentialField, ImChannel, ImError, InboundEvent, OpenTicket, register

DEFAULT_API_BASE = "https://slack.com/api"

# 事件里 `channel_type` 的群形态（Web API 文档列的这几类都是「不止两个人」）；
# 只有 "im" 是确定的单聊。认不出来的取值一律按群处理——策略上群比单聊严，
# 宁可多要求一次 @，也不把陌生频道当「用户私下找我」直接起任务。
GROUP_CHANNEL_TYPES = ("channel", "private_channel", "group", "mpim", "shared_channel")
# Slack 文本里的 @ 形状：<@U012AB3CD> 或带显示名的 <@U012AB3CD|someone>
MENTION_TOKEN = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")


def api_base() -> str:
    return (os.environ.get("TT_SLACK_API_BASE") or DEFAULT_API_BASE).rstrip("/")


def _require_prefix(token: str, prefix: str, label: str) -> None:
    if not token.startswith(prefix):
        raise ImError(
            "IM_CREDENTIAL_INVALID",
            f"{label} 必须以 {prefix} 开头（当前长度 {len(token)}），请从 Slack 后台复制完整 Token",
        )


@register
class SlackSocketChannel(ImChannel):
    kind = "slack"
    label = "Slack"
    supported = True
    setup_url = "https://api.slack.com/apps"
    setup_note = (
        "Slack 后台创建 App → 开启 Socket Mode 并生成 xapp- App-Level Token（scope: connections:write）"
        "→ 安装到工作区生成 xoxb- Bot Token（需 chat:write，并在 Event Subscriptions 订阅消息事件）。"
        "Socket Mode 走反向长连接，不需要公网回调地址。"
    )
    fields = (
        CredentialField(
            key="appToken",
            label="App-Level Token（xapp-）",
            placeholder="xapp-1-...",
            help_url="https://api.slack.com/apps?new_app=1",
            note="Socket Mode 页生成，仅用于换取反向连接地址",
        ),
        CredentialField(
            key="botToken",
            label="Bot Token（xoxb-）",
            placeholder="xoxb-...",
            help_url="https://api.slack.com/authentication/token-types",
            note="收发消息与 chat.postMessage 回贴都用它",
        ),
    )

    # 群里「有没有 @ 我」在 Slack 是有字段可查的（事件类型 + 文本里的 <@USER>），
    # 所以群策略的「被 @ 才回应」这一档在这条通道上是真能兑现的；
    # 附件与「正在输入」本通道仍未接（见框架默认的那几句中文关闭原因）。
    mention_note = (
        "Slack 的群/单聊取自事件的 channel_type（只有 im 是确定单聊，认不出的取值按群处理），"
        "「有没有 @ 我」取自 app_mention 事件类型或文本里的 <@机器人用户 ID>；"
        "机器人用户 ID 来自 auth.test 与事件信封的 authorizations，两者都没给且文本里 @ 了别人时如实答不出"
    )

    def __init__(self):
        # auth.test 里那个 U 开头的机器人用户 ID：@ 的是「用户」而不是 bot_id（B 开头），
        # 记下来才能在没带 authorizations 的信封里也认出「@ 的是不是我」
        self._bot_user_id = ""

    # ── ①凭据校验：两次真实往返，格式过了不代表能用 ──────────────

    async def verify(self, creds: dict) -> dict:
        app_token = str(creds.get("appToken") or "")
        bot_token = str(creds.get("botToken") or "")
        _require_prefix(app_token, "xapp-", "App-Level Token")
        _require_prefix(bot_token, "xoxb-", "Bot Token")
        auth = await self.post_json(f"{api_base()}/auth.test", token=bot_token)
        self._check_ok(auth, "auth.test（Bot Token 校验）")
        conn = await self.post_json(f"{api_base()}/apps.connections.open", token=app_token)
        self._check_ok(conn, "apps.connections.open（App-Level Token 校验）")
        if not str(conn.get("url") or "").startswith(("ws://", "wss://")):
            raise ImError("IM_BAD_RESPONSE", f"apps.connections.open 未返回连接地址：{conn}")
        self._bot_user_id = str(auth.get("user_id") or "")
        return {
            "team": str(auth.get("team") or conn.get("team_id") or ""),
            "teamId": str(auth.get("team_id") or conn.get("team_id") or ""),
            "botUser": str(auth.get("user") or ""),
            "botId": str(auth.get("bot_id") or ""),
            "botUserId": self._bot_user_id,
            "appId": str(conn.get("app_id") or ""),
            "apiBase": api_base(),
        }

    # ── ②换连接地址 ───────────────────────────────────────────────

    async def open_connection(self, creds: dict) -> OpenTicket:
        app_token = str(creds.get("appToken") or "")
        _require_prefix(app_token, "xapp-", "App-Level Token")
        resp = await self.post_json(f"{api_base()}/apps.connections.open", token=app_token)
        self._check_ok(resp, "apps.connections.open")
        url = str(resp.get("url") or "")
        if not url.startswith(("ws://", "wss://")):
            raise ImError("IM_BAD_RESPONSE", f"apps.connections.open 未返回连接地址：{resp}")
        info = {
            "teamId": str(resp.get("team_id") or ""),
            "appId": str(resp.get("app_id") or ""),
            "apiBase": api_base(),
        }
        info = {k: v for k, v in info.items() if v}
        return OpenTicket(ws_url=url, info=info)

    # ── ③逐帧回执 ─────────────────────────────────────────────────

    async def ack(self, ws, frame: dict, ev: InboundEvent) -> None:
        envelope_id = frame.get("envelope_id")
        if not envelope_id:
            return
        if frame.get("type") == "url_verification":
            challenge = str((frame.get("payload") or {}).get("challenge") or "")
            await ws.send_json({"envelope_id": envelope_id, "payload": {"challenge": challenge}})
            return
        await ws.send_json({"envelope_id": envelope_id})

    # ── ②解析事件帧 ───────────────────────────────────────────────

    def parse_event(self, frame: dict) -> InboundEvent:
        frame_type = str(frame.get("type") or "")
        if frame_type == "disconnect":
            # Slack 主动要求换新连接地址，等价于一次有序重连
            return InboundEvent(kind="reconnect")
        if frame_type == "url_verification":
            return InboundEvent(kind="ignore")
        if frame_type != "events_api":
            return InboundEvent(kind="ignore")
        payload = frame.get("payload") or {}
        event = payload.get("event") or {}
        event_type = str(event.get("type") or "")
        # app_mention 与 message 带同样的 channel/user/text/ts 字段：订阅了「@ 机器人」
        # 而没订阅频道消息的应用只收到这一种。两条都来时靠 message_id（同一个 ts）排重，
        # 不会起两个任务——框架那一道「同一条消息只处理一次」在这里正好用得上。
        if event_type not in ("message", "app_mention"):
            return InboundEvent(kind="ignore")
        # bot_id/subtype：机器人自己的回贴与编辑、加入频道等，不重复触发任务
        if event.get("bot_id") or event.get("subtype"):
            return InboundEvent(kind="ignore")
        text = str(event.get("text") or "").strip()
        conversation_id = str(event.get("channel") or "").strip()
        if not text or not conversation_id:
            return InboundEvent(kind="ignore")
        return InboundEvent(
            kind="inbound",
            text=text,
            conversation_id=conversation_id,
            user_id=str(event.get("user") or ""),
            message_id=str(event.get("ts") or ""),
            is_group=self._is_group(event),
            mentioned_me=self._mentioned(event, payload),
            at_ms=self._at_ms(event),
        )

    # ── D-41：这条是群还是单聊、有没有 @ 我（都只取事件里明写的事实） ──

    @staticmethod
    def _is_group(event: dict) -> bool:
        """channel_type 只有 "im" 能确定是单聊；其余（含陌生取值）都按群对待"""
        return str(event.get("channel_type") or "") != "im"

    def _mentioned(self, event: dict, payload: dict) -> bool | None:
        """被 @ 判定：app_mention 事件类型或文本里的 <@机器人用户 ID>

        返回 None 只有一种情况：文本里 @ 了人，但这一帧没告诉我们机器人是谁——
        把「不知道」说成「没被 @」会让群里该回的那条永远不回，比多查一次昂贵。
        """
        if str(event.get("type") or "") == "app_mention":
            return True
        bot_id = self._bot_user_id
        auths = payload.get("authorizations") if isinstance(payload, dict) else None
        if not bot_id and isinstance(auths, list):
            for row in auths:
                if isinstance(row, dict) and str(row.get("user_id") or ""):
                    bot_id = str(row["user_id"])
                    break
        tokens = MENTION_TOKEN.findall(str(event.get("text") or ""))
        if bot_id:
            return bot_id in tokens
        return False if not tokens else None

    @staticmethod
    def _at_ms(event: dict) -> int:
        """Slack 的 ts 是带 6 位小数的秒（"1712345678.123456"）"""
        try:
            return int(float(str(event.get("ts") or 0)) * 1000)
        except ValueError:
            return 0

    # ── ④Web API 回贴 ─────────────────────────────────────────────

    async def send(self, creds: dict, conversation_id: str, text: str) -> None:
        resp = await self.post_json(
            f"{api_base()}/chat.postMessage",
            payload={"channel": conversation_id, "text": text, "unfurl_links": False},
            token=str(creds.get("botToken") or ""),
        )
        self._check_ok(resp, "chat.postMessage")

    # ── 共用：Slack 用 ok=false + error 表达失败，不抛 HTTP 码 ─────

    @staticmethod
    def _check_ok(resp: dict, action: str) -> None:
        if resp.get("ok") is True:
            return
        error = str(resp.get("error") or "unknown_error")
        auth_errors = {
            "invalid_auth",
            "not_authed",
            "token_revoked",
            "account_inactive",
            "missing_scope",
            "invalid_token",
        }
        code = "IM_AUTH_FAILED" if error in auth_errors else "IM_API_ERROR"
        raise ImError(code, f"{action} 失败（Slack error={error}）")
