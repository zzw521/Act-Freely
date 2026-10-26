"""
通天办公 - 企业微信智能机器人通道（官方 aibot SDK 的协议 + 框架共用连接循环）

协议事实取自两处权威来源，不是凭记忆写的：
- 官方 SDK wecom-aibot-python-sdk 1.0.2（作者 WeCom <wecom@tencent.com>，
  github.com/WecomTeam/wecom-aibot-python-sdk；读的是它的 ws.py / client.py /
  message_handler.py / types.py / utils.py）
- 企业微信开发者中心《智能机器人长连接》（2026/10/10 版，document/path/101463）

这条通道属于 B 族方言（和钉钉/Slack 的「换地址 → HTTP 回贴」不是一族）：
- 长连接地址固定为 wss://openws.work.weixin.qq.com，**不换取 ticket**；连上之后发带内
  订阅帧 {cmd: aibot_subscribe, headers:{req_id}, body:{bot_id, secret}} 完成身份校验，
  对端回一帧 {headers:{req_id 透传}, errcode, errmsg}，errcode==0 才算认证成功
- 保活是应用层帧：每 30 秒发 {cmd: ping, headers:{req_id}}，对端回 errcode（官方建议值，
  SDK 的 heartbeat_interval=30000ms 就是它；连续没等到回执视为连接已死）
- 推送有两种 cmd：aibot_msg_callback（消息，body.msgtype = text/image/mixed/voice/file/video）
  和 aibot_event_callback（事件，body.event.eventtype = enter_chat/template_card_event/
  feedback_event/disconnected_event）
- **没有逐帧回执**，回贴要把回调帧的 headers.req_id 原样透传回去：
  {cmd: aibot_respond_msg, headers:{req_id}, body:{msgtype:"stream", stream:{id,finish,content}}}
  对端仍在同一条 socket 上回同 req_id 的 errcode，SDK 等它 5 秒，这里也等 —— 不等就等于
  「界面以为消息发出去了」
- 会话标识：群聊取 body.chatid（chattype=group），单聊不返回 chatid，取 body.from.userid
  （官方 aibot_send_msg 的说明：单聊填用户的 userid，群聊填 chatid）
- 主动推送用 aibot_send_msg + chatid + chat_type(1 单聊/2 群聊)，body 用 markdown；
  本通道在回贴的 req_id 已经不成立（进程重启、超过保留上限、超过 24 小时回复窗口）时走它

为什么不跑 SDK 的 WSClient.connect()：它基于 websockets 客户端 + pyee 事件 + 自己一套
指数退避重连（1s 起、上限 30s、最多 10 次）和串行回复队列，和框架已有的 BACKOFF_SECONDS、
状态机、会话台账会变成「两套循环、两套重连」。所以只借它的协议常量与 req_id 拼法
（WsCmd / generate_req_id / DEFAULT_WS_URL / MessageType / EventType），传输仍由框架的
aiohttp 会话承担，企业微信因此和 Slack、钉钉共用同一条循环。SDK 自己等回执的 5 秒超时和
「连续 2 次没等到心跳回执就断开」的判定也在本文件里复刻了一份，因为这两条是协议行为不是
实现细节。

Mock 开关：SDK 用 WSClientOptions.ws_url 注入自定义连接地址（企业微信没有为长连接提供
HTTP 侧的 endpoint 环境变量），本通道沿同一个口子读环境变量 TT_WECOM_WS_URL；心跳间隔用
TT_WECOM_HEARTBEAT_SECONDS 覆盖（默认取官方建议的 30 秒）。集成自测用本机假企业微信
（tests/integration/fake_wecom.py）顶掉这两个变量，把「带内订阅 → 心跳 → 收消息 → 带内
回贴 → 等 errcode 回执 → 被新连接顶掉」真跑一遍。

诚实边界：现在没有真实企业微信机器人凭据，所以这条链路只在本地对拍验证过，界面不能说
「已打通」。另外两条是官方协议给的硬约束，不是本实现的选择：同一个 BotID 同一时间只允许
一条有效长连接（新订阅会踢掉旧连接，旧连接收到 disconnected_event 后被服务端断开），
以及订阅请求有频率保护 —— 所以校验凭据用的探测连接会先停掉本通道自己的连接，避免一条
正在干活的连接被自己踢掉。
bot_id 与 secret 只进订阅帧，绝不进视图、日志或错误详情。
"""

import asyncio
import json
import os
import time

from aiohttp import ClientWebSocketResponse, WSMsgType

from im_channels import CredentialField, ImChannel, ImError, InboundEvent, OpenTicket, register

try:  # 官方 SDK 是这条通道的协议来源；随包运行时缺它就不能假装支持
    from aibot import WsCmd, generate_req_id
    from aibot.types import MessageType
    from aibot.ws import DEFAULT_WS_URL

    SDK_IMPORT_ERROR = ""
except Exception as exc:  # 缺依赖时通道如实落到「尚未接入」
    WsCmd = generate_req_id = None
    MessageType = None
    DEFAULT_WS_URL = ""
    SDK_IMPORT_ERROR = (
        f"引擎运行时未安装官方 wecom-aibot-python-sdk（{exc}），无法按企业微信长连接协议收发"
    )

# 官方文档里「新连接踢掉旧连接」的事件类型；SDK 1.0.2 的 EventType 枚举还没有这一项
# （只有 enter_chat/template_card_event/feedback_event），所以按文档取字面量
EVENT_DISCONNECTED = "disconnected_event"

# 官方文档给的硬事实：一个会话 24 小时内可回复，超过就只能走主动推送
REPLY_WINDOW_SECONDS = 24 * 3600
# SDK 的 _reply_ack_timeout / _max_missed_pong，等不到就是没送出去
REPLY_ACK_TIMEOUT_SECONDS = 5.0
MAX_MISSED_PONG = 2
# 回贴要的 req_id 随每条回调下发，攒着不用会无限增长；只保留最近这些会话的最后一发
MAX_REPLY_TARGETS = 64
# 官方要求按 body.msgid 排重（可能因网络等原因重复回调），只记最近这些
MAX_SEEN_MSGIDS = 256


def ws_url_base() -> str:
    """与 SDK 同一个注入点：WSClientOptions.ws_url，默认 wss://openws.work.weixin.qq.com"""
    return (os.environ.get("TT_WECOM_WS_URL") or DEFAULT_WS_URL or "wss://openws.work.weixin.qq.com").rstrip("/")


def heartbeat_seconds() -> float:
    raw = os.environ.get("TT_WECOM_HEARTBEAT_SECONDS") or ""
    try:
        value = float(raw) if raw else 30.0
    except ValueError:
        return 30.0
    return value if value > 0 else 30.0


def _mask(bot_id: str) -> str:
    """账号信息里只留足以分辨「绑的是哪个机器人」的头尾"""
    if len(bot_id) <= 8:
        return bot_id[:2] + "…"
    return f"{bot_id[:4]}…{bot_id[-2:]}"


def _errcode_of(frame: dict) -> int | None:
    """官方所有响应帧都是 {headers:{req_id}, errcode, errmsg}；缺 errcode 按失败算，
    和 SDK 的 `if errcode != 0` 一致，不给「没说不行就是行」的假成功"""
    value = frame.get("errcode")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _reject_reason(frame: dict) -> str:
    code = _errcode_of(frame)
    msg = str(frame.get("errmsg") or "")[:200]
    return f"errcode={code} errmsg={msg or '（无）'}"


@register
class WeComAibotChannel(ImChannel):
    kind = "wecom"
    label = "企业微信"
    # SDK 没装进随包运行时就退回「尚未接入」，不给能点但必然失败的控件
    supported = SDK_IMPORT_ERROR == ""
    # 心跳是应用层 ping 帧，传输层再发 ping 反而和官方 SDK 的行为不一致（它就是 ping_interval=None）
    protocol_heartbeat = None
    setup_url = "https://work.weixin.qq.com/"
    setup_note = (
        "企业微信管理后台 → 智能机器人 → 创建机器人 → 开启「API 模式」并选择「长连接」方式"
        "（与 Slack Socket Mode、钉钉 Stream 同类：本机主动向外建连，不需要公网回调地址）"
        "→ 复制 BotID 与长连接专用 Secret。注意：同一个 BotID 同一时间只允许一条有效长连接，"
        "别处再订阅会踢掉这里的连接。"
    )
    fields = (
        CredentialField(
            key="botId",
            label="BotID",
            placeholder="机器人的 BotID",
            help_url="https://developer.work.weixin.qq.com/document/path/101463",
            note="智能机器人配置页下发，用于标识机器人身份",
        ),
        CredentialField(
            key="secret",
            label="长连接 Secret",
            placeholder="粘贴完整 Secret",
            help_url="https://developer.work.weixin.qq.com/document/path/101463",
            note="长连接专用密钥，与回调地址模式的 Token/EncodingAESKey 不同；只进订阅帧，不回显、不写日志",
        ),
    )

    def __init__(self):
        self._ws: ClientWebSocketResponse | None = None
        self._hb_task: asyncio.Task | None = None
        self._authenticated = False
        self._missed_pong = 0
        # 会话标识 -> 该会话最后一次回调的 req_id（回贴要透传）与群聊/单聊类型
        self._reply_targets: dict[str, dict] = {}
        # 我们发出去、还在等 errcode 回执的帧：req_id -> Future
        self._pending_acks: dict[str, asyncio.Future] = {}
        self._seen_msgids: list[str] = []

    @property
    def unsupported_reason(self) -> str:
        return SDK_IMPORT_ERROR or super().unsupported_reason

    def _require(self, creds: dict) -> tuple[str, str]:
        bot_id = str(creds.get("botId") or "").strip()
        secret = str(creds.get("secret") or "").strip()
        if not bot_id or not secret:
            raise ImError("IM_CREDENTIAL_MISSING", "企业微信需要 BotID 与长连接 Secret 两项凭据")
        if " " in bot_id or "\t" in bot_id or " " in secret or "\t" in secret:
            raise ImError("IM_CREDENTIAL_INVALID", "BotID 或 Secret 里带进了空格，请回机器人配置页复制完整值")
        return bot_id, secret

    # ── ①凭据校验：连上去真订阅一次，errcode 为 0 才算过 ────────────────

    async def verify(self, creds: dict) -> dict:
        bot_id, secret = self._require(creds)
        # 同一个 BotID 只允许一条有效长连接，探测连接会把正在干活的那条踢掉：
        # 先停自己，校验通过后框架再 start，不会留下「凭据没换成、连接却被自己打断」的状态
        if self.runtime is not None and self.runtime.state in ("connecting", "connected", "reconnecting"):
            await self.runtime.stop()
        url = ws_url_base()
        req_id = str(generate_req_id(WsCmd.SUBSCRIBE))
        ws = await self.runtime.http.ws_connect(url, heartbeat=None, receive_timeout=None)
        try:
            await ws.send_str(
                json.dumps(
                    {"cmd": WsCmd.SUBSCRIBE, "headers": {"req_id": req_id}, "body": {"bot_id": bot_id, "secret": secret}},
                    ensure_ascii=False,
                )
            )
            deadline = time.monotonic() + 10
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ImError("IM_CONNECTION_FAILED", "企业微信没有在 10 秒内返回订阅结果，请检查本机到 openws.work.weixin.qq.com 的网络")
                msg = await ws.receive(timeout=remaining)
                if msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                    raise ImError("IM_CONNECTION_CLOSED", f"订阅还没完成，企业微信就把连接关了：{msg.data}")
                if msg.type != WSMsgType.TEXT:
                    continue
                frame = json.loads(msg.data)
                if str((frame.get("headers") or {}).get("req_id") or "") != req_id:
                    continue  # 认证没完成前下发的推送帧：本通道不消费，留给真正的连接
                code = _errcode_of(frame)
                if code != 0:
                    raise ImError(
                        "IM_AUTH_FAILED",
                        f"订阅认证被企业微信拒绝（{_reject_reason(frame)}）。请确认机器人已开启「API 模式-长连接」，"
                        "且用的是长连接专用 Secret（不是回调地址模式的 Token）",
                    )
                return {
                    "botId": _mask(bot_id),
                    "wsUrl": url,
                    "heartbeatSeconds": heartbeat_seconds(),
                    "oneConnectionPerBot": True,
                }
        finally:
            if not ws.closed:
                await ws.close()

    # ── ②连接地址：B 族方言没有 ticket，地址是固定的 ────────────────────

    async def open_connection(self, creds: dict) -> OpenTicket:
        bot_id, _ = self._require(creds)
        return OpenTicket(
            ws_url=ws_url_base(),
            info={
                "botId": _mask(bot_id),
                "wsUrl": ws_url_base(),
                "heartbeatSeconds": heartbeat_seconds(),
                "oneConnectionPerBot": True,
            },
        )

    # ── 连上之后：带内订阅 + 应用层心跳（框架的 on_open 钩子） ────────────

    async def on_open(self, ws) -> None:
        """框架在这之后才置 connected，所以这里的 connected 说的是「socket 已建立、
        订阅帧已发出」——和官方 SDK 的 WSClient 同一口径。订阅结果由下一帧的 errcode
        决定：认证没过时 parse_event 直接抛 IM_AUTH_FAILED / IM_CONNECTION_KICKED，
        状态机随即落到 failed 并且不再退避重连，不会停在「已连接」上骗人。"""
        bot_id, secret = self._require(self.runtime.creds())
        self._ws = ws
        self._authenticated = False
        self._missed_pong = 0
        self._pending_acks.clear()
        await self._send_frame(
            {"cmd": WsCmd.SUBSCRIBE, "headers": {"req_id": str(generate_req_id(WsCmd.SUBSCRIBE))},
             "body": {"bot_id": bot_id, "secret": secret}}
        )
        if heartbeat_seconds() > 0:
            self._hb_task = asyncio.create_task(self._keepalive())

    async def on_close(self) -> None:
        task, self._hb_task = self._hb_task, None
        if task is not None and not task.done():
            task.cancel()
        self._ws = None
        self._authenticated = False
        reason = "企业微信长连接已关闭，本次回贴没有送达"
        for fut in list(self._pending_acks.values()):
            if not fut.done():
                fut.set_exception(RuntimeError(reason))
        self._pending_acks.clear()
        # 回调的 req_id 属于这条已经死掉的连接，换新连接再透传它没有依据；
        # 只把 req_id 抹掉，会话是群聊还是单聊（chat_type）留着，重连后的回贴改走主动推送
        for target in self._reply_targets.values():
            target["req_id"] = ""

    async def _keepalive(self) -> None:
        """官方：每 30 秒发一次 ping；SDK 连续 2 次没等到回执就主动断开，交给重连"""
        interval = heartbeat_seconds()
        try:
            while True:
                await asyncio.sleep(interval)
                ws = self._ws
                if ws is None or ws.closed:
                    return
                if self._missed_pong >= MAX_MISSED_PONG:
                    await ws.close()
                    return
                self._missed_pong += 1
                await self._send_frame({"cmd": WsCmd.HEARTBEAT, "headers": {"req_id": str(generate_req_id(WsCmd.HEARTBEAT))}})
        except asyncio.CancelledError:
            raise
        except Exception:
            # 心跳都发不出去说明连接已经没了；收帧循环会自己撞上 CLOSED/ERROR 并按退避重连
            ws = self._ws
            if ws is not None and not ws.closed:
                await ws.close()

    async def _send_frame(self, frame: dict) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise ImError("IM_CONNECTION_FAILED", "企业微信长连接还没建立，无法发送带内帧")
        await ws.send_str(json.dumps(frame, ensure_ascii=False))

    # ── ③解析帧 ──────────────────────────────────────────────────────

    def parse_event(self, frame: dict) -> InboundEvent:
        cmd = str(frame.get("cmd") or "")
        req_id = str((frame.get("headers") or {}).get("req_id") or "")

        if not cmd:
            # 无 cmd 的帧都是响应，按 req_id 认领（SDK 的 _handle_frame 是同一个分流）
            fut = self._pending_acks.get(req_id)
            if fut is not None:
                self._pending_acks.pop(req_id, None)
                if not fut.done():
                    fut.set_result(frame)
                return InboundEvent(kind="ignore")
            if req_id and str(req_id).startswith(str(WsCmd.SUBSCRIBE)):
                if _errcode_of(frame) != 0:
                    # 被新连接顶掉/凭据失效都会在这里现形；不能让它退避重试成两个进程互相踢
                    if not self._authenticated:
                        raise ImError("IM_AUTH_FAILED", f"订阅认证被企业微信拒绝（{_reject_reason(frame)}）")
                    raise ImError(
                        "IM_CONNECTION_KICKED",
                        f"订阅被企业微信拒绝（{_reject_reason(frame)}）：同一个 BotID 只允许一条有效长连接，已停止自动重连",
                        retryable=False,
                    )
                self._authenticated = True
                self._missed_pong = 0
                return InboundEvent(kind="ignore")
            if req_id and str(req_id).startswith(str(WsCmd.HEARTBEAT)):
                self._missed_pong = 0 if _errcode_of(frame) == 0 else self._missed_pong + 1
                return InboundEvent(kind="ignore")
            return InboundEvent(kind="ignore")

        if cmd == WsCmd.EVENT_CALLBACK:
            body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
            event = body.get("event") if isinstance(body.get("event"), dict) else {}
            if str(event.get("eventtype") or "") == EVENT_DISCONNECTED:
                raise ImError(
                    "IM_CONNECTION_KICKED",
                    "该机器人的长连接被另一条新连接顶掉了（官方：同一个 BotID 同一时间只有一条有效长连接）。"
                    "已停止自动重连，请确认没有另一个通天办公实例或脚本在用这个 BotID",
                    retryable=False,
                )
            # enter_chat / template_card_event / feedback_event 都不是任务文本；
            # 欢迎语与卡片更新有 5 秒窗口，本通道不在这里猜该怎么回，如实不消费
            return InboundEvent(kind="ignore")

        if cmd != WsCmd.CALLBACK:
            return InboundEvent(kind="ignore")

        body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
        if body.get("compat_info"):
            # 官方：有此字段表示本次回调不是普通用户消息（智能助理调用、频道），不该起任务
            return InboundEvent(kind="ignore")
        if str(body.get("msgtype") or "") != str(MessageType.Text.value):
            # image/file/video 要拿 aeskey 下载并 AES-256-CBC 解密，voice 是转写文本；
            # 本通道只把文本消息当任务，其它形状如实忽略而不是猜
            return InboundEvent(kind="ignore")
        text = str((body.get("text") or {}).get("content") or "").strip()
        msgid = str(body.get("msgid") or "")
        # 官方要求按 msgid 排重：同一条消息可能因网络原因重复回调，重复起任务就是重复消耗额度
        if msgid and msgid in self._seen_msgids:
            return InboundEvent(kind="ignore")
        conversation_id = self._conversation_of(body)
        if not conversation_id or not text:
            return InboundEvent(kind="ignore")
        if msgid:
            self._seen_msgids.append(msgid)
            del self._seen_msgids[:-MAX_SEEN_MSGIDS]
        self._remember_reply_target(conversation_id, body, req_id)
        return InboundEvent(
            kind="inbound",
            text=text,
            conversation_id=conversation_id,
            user_id=str((body.get("from") or {}).get("userid") or ""),
            message_id=msgid,
        )

    @staticmethod
    def _conversation_of(body: dict) -> str:
        """群聊回 chatid，单聊不返回 chatid，只能拿发送者 userid 当会话标识
        （官方 aibot_send_msg 说明：单聊填用户的 userid，群聊填 chatid）"""
        chat_type = str(body.get("chattype") or "")
        chat_id = str(body.get("chatid") or "").strip()
        if chat_type == "group" and chat_id:
            return chat_id
        if chat_type == "single":
            return str((body.get("from") or {}).get("userid") or "").strip()
        return chat_id or str((body.get("from") or {}).get("userid") or "").strip()

    def _remember_reply_target(self, conversation_id: str, body: dict, req_id: str) -> None:
        self._reply_targets[conversation_id] = {
            "req_id": req_id,
            "chat_type": 2 if str(body.get("chattype") or "") == "group" else 1,
            "atMs": int(time.time() * 1000),
        }
        if len(self._reply_targets) > MAX_REPLY_TARGETS:
            for stale in list(self._reply_targets)[: -MAX_REPLY_TARGETS]:
                self._reply_targets.pop(stale, None)

    # ── ④逐帧回执：企业微信长连接没有这个动作 ───────────────────────────
    # 官方协议里回贴本身就是对回调的应答（透传 req_id），没有钉钉那种 ACK 帧。
    # ack() 留在基类做 no-op，不是因为忘了实现。

    # ── ⑤回贴：带内 respond_msg，并等对端在同一条 socket 上下发的 errcode ──

    async def send(self, creds: dict, conversation_id: str, text: str) -> None:
        if self._ws is None or self._ws.closed:
            raise ImError("IM_CONNECTION_FAILED", "企业微信长连接不在，回贴发不出去")
        target = self._reply_targets.get(conversation_id) or {}
        req_id = str(target.get("req_id") or "")
        fresh = bool(req_id) and (time.time() * 1000 - int(target.get("atMs") or 0)) / 1000 < REPLY_WINDOW_SECONDS
        if fresh:
            # 官方：透传消息回调中的 req_id；stream.id 用来关联同一条流式消息，一次发完就 finish
            cmd, headers, body = (
                WsCmd.RESPONSE,
                {"req_id": req_id},
                {
                    "msgtype": "stream",
                    "stream": {
                        "id": str(generate_req_id("stream")),
                        "finish": True,
                        "content": text,
                    },
                },
            )
        else:
            # 回调过了 24 小时回复窗口（或进程重启把 req_id 丢了）→ 走官方主动推送，
            # 而不是假装回贴成功。前提是用户先在会话里给机器人发过消息（官方约束）
            cmd, headers, body = (
                WsCmd.SEND_MSG,
                {"req_id": str(generate_req_id(WsCmd.SEND_MSG))},
                {
                    "chatid": conversation_id,
                    "chat_type": int(target.get("chat_type") or 1),
                    "msgtype": "markdown",
                    "markdown": {"content": text},
                },
            )
        frame = {"cmd": cmd, "headers": headers, "body": body}
        req = str(headers["req_id"])
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_acks[req] = fut
        try:
            await self._send_frame(frame)
        except Exception:
            self._pending_acks.pop(req, None)
            raise
        try:
            ack = await asyncio.wait_for(fut, timeout=REPLY_ACK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self._pending_acks.pop(req, None)
            raise ImError(
                "IM_REPLY_FAILED",
                f"{cmd} 发出去了，但企业微信没有在 {REPLY_ACK_TIMEOUT_SECONDS} 秒内回执 errcode（req_id={req}）",
            ) from None
        except RuntimeError as exc:  # 连接在这期间关了
            raise ImError("IM_REPLY_FAILED", str(exc)) from None
        if _errcode_of(ack) != 0:
            raise ImError("IM_REPLY_FAILED", f"回贴被企业微信拒绝：{_reject_reason(ack)}")
