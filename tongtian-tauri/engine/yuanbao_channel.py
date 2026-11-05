"""
通天办公 - 腾讯元宝通道（官方 yuanbao 机器人协议：sign-token + WebSocket + protobuf）

协议事实取自腾讯官方 npm 包 openclaw-plugin-yuanbao 2.18.3（作者 Tencent YuanBao Team），
读的是它随包的 protobuf 描述符 dist/src/access/ws/proto/{conn,biz}.json 与实现
access/http/request.js（sign-token）、access/ws/client.js（连接/鉴权/心跳/推送）、
gateway.js（推送解码）—— 字段号与类型照描述符抄，不凭记忆：

- 换票：POST {api}/api/v5/robotLogic/sign-token
    body {app_key, nonce(hex32), signature, timestamp(北京时间 ISO+08:00)}
    signature = HMAC-SHA256(secret=appSecret, msg=nonce+timestamp+appKey+appSecret).hex
    头 X-AppVersion / X-OperationSystem / X-Instance-Id: 16 / X-Bot-Version
    成功 code=0，data 给 bot_id / token / duration；10099 可重试。
- 长连接：wss://bot-wss.yuanbao.tencent.com/wss/connection，帧是二进制 ConnMsg{head, data}。
    连上先发 cmdType=0、cmd=auth-bind、module=conn_access 的帧，data 是 AuthBindReq
    {bizId:"ybBot", authInfo:{uid:bot_id, source:"bot", token}, deviceInfo:{... instanceId:"16"}}；
    对端回 cmdType=1、cmd=auth-bind，AuthBindRsp.code 为 0 或 41101（已认证）才算成功。
- 保活：应用层 ping，cmd=ping/module=conn_access，官方 5 秒一次；连续 2 次没等到
  PingRsp 就断开重连。传输层不再发 ping（protocol_heartbeat=None）。
- 推送：cmdType=2；head.needAck 为真要先回一帧 cmdType=3 的 ACK（复制 head、换 seqNo）。
    head.cmd=kickout 表示被同账号另一条连接顶掉；其余解码 PushMsg{cmd,module,msgId,data}，
    再按业务 cmd（如 C2C.CallbackAfterSendMsg）把 data 解成 InboundMessagePush。
- 回贴：业务请求同样走 ConnMsg，cmd=send_c2c_message / send_group_message、
    module=yuanbao_openclaw_proxy，按 head.msgId 与响应配对；回包 code=0 才算发出。
    单聊用 toAccount=对方账号；群聊用 groupCode（InboundMessagePush.groupCode / clawMsgType=1）。
- 文本元素：MsgBodyElement{msgType:"TIMTextElem", msgContent:{text}}；其它元素本通道不消费。

为什么手写 protobuf 编解码：随包 Python 运行时只保证 requirements.txt 里声明的依赖，
而 protobuf 只是 agentscope/lark-oapi 的传递依赖。为十几个消息类型引入生成器或把传递
依赖升级成契约，代价都比这里几十行 varint/length-delimited 读写大；字段号与类型从官方
描述符逐条对过，集成自测再用同一份描述符做对拍。

诚实边界：没有真实元宝机器人凭据做过端到端验收，只在 tests/integration/fake_yuanbao.py
的本机假服务端上对拍；界面文案不得说成「已打通」。appSecret 只用于本地签名，绝不进
视图、日志或错误详情；签出来的 token 只在内存里，过期前 5 分钟自动重签。

Mock 开关：TT_YUANBAO_API_BASE（默认 https://bot.yuanbao.tencent.com）、
TT_YUANBAO_WS_URL（默认 wss://bot-wss.yuanbao.tencent.com/wss/connection）、
TT_YUANBAO_HEARTBEAT_SECONDS（默认 5）。
"""

import asyncio
import hashlib
import hmac
import itertools
import json
import os
import platform
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp

from im_channels import CredentialField, ImChannel, ImError, InboundEvent, OpenTicket, register

DEFAULT_API_BASE = "https://bot.yuanbao.tencent.com"
DEFAULT_WS_URL = "wss://bot-wss.yuanbao.tencent.com/wss/connection"
APP_VERSION = "2.18.3"
BOT_VERSION = "0.0.0"
TERMINAL_TYPE_ID = "16"
CHANNEL_VERSION = "2.18.3"
BIZ_ID = "ybBot"
CONN_MODULE = "conn_access"
BIZ_MODULE = "yuanbao_openclaw_proxy"

CMD_AUTH = "auth-bind"
CMD_PING = "ping"
CMD_KICKOUT = "kickout"
CMD_SEND_C2C = "send_c2c_message"
CMD_SEND_GROUP = "send_group_message"

AUTH_ALREADY = 41101
AUTH_BAD_TOKEN = {41103, 41104, 41108}
AUTH_RETRYABLE = {50400, 50503, 90001, 90003}
SIGN_RETRYABLE = 10099

MAX_SEEN_MSGIDS = 256
MAX_TARGETS = 64
TEXT_LIMIT = 4000
SEND_TIMEOUT_SECONDS = 30.0
TOKEN_MARGIN_SECONDS = 300


def api_base() -> str:
    return (os.environ.get("TT_YUANBAO_API_BASE") or DEFAULT_API_BASE).rstrip("/")


def ws_url() -> str:
    return os.environ.get("TT_YUANBAO_WS_URL") or DEFAULT_WS_URL


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


def _bj_timestamp() -> str:
    now = datetime.now(timezone(timedelta(hours=8)))
    return now.strftime("%Y-%m-%dT%H:%M:%S+08:00")


# ── 最小 protobuf 编解码（proto3，只覆盖本通道用到的字段） ─────────────


def _varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _f_str(field: int, value) -> bytes:
    raw = str(value).encode("utf-8") if value else b""
    return _tag(field, 2) + _varint(len(raw)) + raw if raw else b""


def _f_uint(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(int(value)) if value else b""


def _f_bool(field: int, value: bool) -> bytes:
    return _tag(field, 0) + b"\x01" if value else b""


def _f_bytes(field: int, payload: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(payload)) + payload


def _pack(*parts: bytes) -> bytes:
    return b"".join(p for p in parts if p)


def _read_varint(buf: bytes, index: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while index < len(buf):
        byte = buf[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")
    raise ValueError("truncated varint")


def _signed(value: int) -> int:
    if value >= 1 << 63:
        value -= 1 << 64
    if value >= 1 << 31:
        value -= 1 << 32
    return value


def _decode(buf: bytes, schema: dict) -> dict:
    """schema: {field: (name, kind, sub_schema|None, repeated)}"""
    out: dict = {}
    index = 0
    while index < len(buf):
        key, index = _read_varint(buf, index)
        field, wire = key >> 3, key & 7
        if wire == 0:
            raw, index = _read_varint(buf, index)
        elif wire == 2:
            size, index = _read_varint(buf, index)
            raw = buf[index : index + size]
            if len(raw) != size:
                raise ValueError("truncated length-delimited field")
            index += size
        elif wire == 1:
            raw = buf[index : index + 8]
            index += 8
        elif wire == 5:
            raw = buf[index : index + 4]
            index += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        spec = schema.get(field)
        if spec is None:
            continue
        name, kind, sub, repeated = spec
        if kind == "str":
            value = raw.decode("utf-8", "replace")
        elif kind == "bool":
            value = bool(raw)
        elif kind == "int":
            value = _signed(int(raw))
        elif kind == "uint":
            value = int(raw)
        elif kind == "bytes":
            value = raw
        else:  # msg
            value = _decode(raw, sub)
        if repeated:
            out.setdefault(name, []).append(value)
        elif kind == "msg":
            out.setdefault(name, {}).update(value)
        else:
            out[name] = value
    return out


HEAD_META_SCHEMA = {
    1: ("key", "str", None, False),
    2: ("value", "str", None, False),
}
HEAD_SCHEMA_BASE = {
    1: ("cmdType", "uint", None, False),
    2: ("cmd", "str", None, False),
    3: ("seqNo", "uint", None, False),
    4: ("msgId", "str", None, False),
    5: ("module", "str", None, False),
    6: ("needAck", "bool", None, False),
    7: ("meta", "msg", HEAD_META_SCHEMA, True),
    10: ("status", "int", None, False),
}
CONN_SCHEMA = {
    1: ("head", "msg", HEAD_SCHEMA_BASE, False),
    2: ("data", "bytes", None, False),
}
AUTH_RSP_SCHEMA = {
    1: ("code", "int", None, False),
    2: ("message", "str", None, False),
    3: ("connectId", "str", None, False),
    4: ("timestamp", "uint", None, False),
    5: ("clientIp", "str", None, False),
}
PING_RSP_SCHEMA = {
    1: ("heartInterval", "uint", None, False),
    2: ("timestamp", "uint", None, False),
}
KICKOUT_SCHEMA = {
    1: ("status", "int", None, False),
    2: ("reason", "str", None, False),
    3: ("otherDeviceName", "str", None, False),
}
PUSH_SCHEMA = {
    1: ("cmd", "str", None, False),
    2: ("module", "str", None, False),
    3: ("msgId", "str", None, False),
    4: ("data", "bytes", None, False),
}
MSG_CONTENT_SCHEMA = {
    1: ("text", "str", None, False),
    10: ("url", "str", None, False),
    12: ("fileName", "str", None, False),
}
MSG_ELEMENT_SCHEMA = {
    1: ("msgType", "str", None, False),
    2: ("msgContent", "msg", MSG_CONTENT_SCHEMA, False),
}
INBOUND_SCHEMA = {
    1: ("callbackCommand", "str", None, False),
    2: ("fromAccount", "str", None, False),
    3: ("toAccount", "str", None, False),
    4: ("senderNickname", "str", None, False),
    5: ("groupId", "str", None, False),
    6: ("groupCode", "str", None, False),
    7: ("groupName", "str", None, False),
    8: ("msgSeq", "uint", None, False),
    9: ("msgRandom", "uint", None, False),
    10: ("msgTime", "uint", None, False),
    11: ("msgKey", "str", None, False),
    12: ("msgId", "str", None, False),
    13: ("msgBody", "msg", MSG_ELEMENT_SCHEMA, True),
    14: ("cloudCustomData", "str", None, False),
    16: ("botOwnerId", "str", None, False),
    18: ("clawMsgType", "uint", None, False),
    19: ("privateFromGroupCode", "str", None, False),
}
SEND_RSP_SCHEMA = {
    1: ("code", "int", None, False),
    2: ("message", "str", None, False),
}

_SEQ = itertools.count(1)


def _next_seq() -> int:
    return next(_SEQ)


def _ms_of_seconds(value) -> int:
    """帧里的 msgTime 是秒（INBOUND_SCHEMA 第 10 项 uint），框架的策略窗口按毫秒走

    缺字段、类型不对或明显不是时间戳（≤0）时一律回 0；0 在策略里等于「不参与时间
    窗口过滤」，比编一个假的时间戳安全。
    """
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return seconds * 1000 if seconds > 0 else 0


def _head(cmd_type: int, cmd: str, msg_id: str, module: str, *, need_ack: bool = False, status: int = 0) -> bytes:
    return _pack(
        _f_uint(1, cmd_type),
        _f_str(2, cmd),
        _f_uint(3, _next_seq()),
        _f_str(4, msg_id),
        _f_str(5, module),
        _f_bool(6, need_ack),
        _f_uint(10, status),
    )


def _conn(head: bytes, data: bytes = b"") -> bytes:
    return _pack(_f_bytes(1, head), _f_bytes(2, data))


def _msg_body(text: str) -> bytes:
    content = _f_str(1, text)
    element = _pack(_f_str(1, "TIMTextElem"), _f_bytes(2, content))
    return element


def _encode_send_c2c(to_account: str, text: str) -> bytes:
    return _pack(
        _f_str(1, uuid.uuid4().hex),
        _f_str(2, to_account),
        _f_uint(4, secrets.randbelow(2**32)),
        _f_bytes(5, _msg_body(text)),
    )


def _encode_send_group(group_code: str, text: str) -> bytes:
    return _pack(
        _f_str(1, uuid.uuid4().hex),
        _f_str(2, group_code),
        _f_str(5, str(secrets.randbelow(2**32))),
        _f_bytes(6, _msg_body(text)),
    )


@register
class YuanbaoChannel(ImChannel):
    kind = "yuanbao"
    label = "腾讯元宝"
    supported = True
    binary_frames = True
    protocol_heartbeat = None
    setup_url = "https://yuanbao.tencent.com/"
    setup_note = (
        "元宝 App → 我的 → 机器人/应用设置里创建机器人，复制 AppID（appKey）与 AppSecret。"
        "本机用 AppSecret 在本地签名换登录令牌，再主动向外建 WebSocket 长连接；"
        "不需要公网回调地址。同一个机器人同一时间只允许一条有效连接。"
    )
    fields = (
        CredentialField(
            key="appKey",
            label="AppID（appKey）",
            placeholder="机器人 AppID",
            note="元宝机器人配置页下发，用于签名与连接鉴权",
        ),
        CredentialField(
            key="appSecret",
            label="AppSecret",
            placeholder="粘贴完整 AppSecret",
            note="只在本地参与 HMAC 签名，不回显、不写日志、不进错误详情",
        ),
    )
    # 协议能给出群标识，但给不出「群里有没有 @ 我」——元宝的 InboundMessagePush 里
    # 没有任何 @ / mention / atUsers 字段（INBOUND_SCHEMA 逐项对过）。
    mention_note = (
        "元宝的帧里只有群标识（groupCode 非空即群），没有任何「谁被 @ 了」的字段，"
        "本通道无法判定消息是不是对着机器人说的。因此在默认的「被 @ 时才回应」策略下，"
        "群内消息一律不起任务并在界面写明原因；要把群内每条消息都交给任务，"
        "请在 IM 设置里把该通道（或该群）的群内触发改为「每条都处理」。"
    )

    def __init__(self):
        self._ws = None
        self._hb_task: asyncio.Task | None = None
        self._auth_task: asyncio.Task | None = None
        self._authenticated = False
        self._missed_pong = 0
        self._bot_id = ""
        self._token = ""
        self._token_expires_at = 0.0
        self._pending: dict[str, asyncio.Future] = {}
        self._ack_head: dict | None = None
        self._targets: dict[str, dict] = {}
        self._seen_msgids: list[str] = []

    @property
    def unsupported_reason(self) -> str:
        return ""

    def _require(self, creds: dict) -> tuple[str, str]:
        app_key = str(creds.get("appKey") or "").strip()
        app_secret = str(creds.get("appSecret") or "").strip()
        if not app_key or not app_secret:
            raise ImError("IM_CREDENTIAL_MISSING", "腾讯元宝需要 AppID 与 AppSecret 两项凭据")
        if any(ch.isspace() for ch in app_key + app_secret):
            raise ImError("IM_CREDENTIAL_INVALID", "AppID 或 AppSecret 里带进了空格，请回元宝机器人页复制完整值")
        return app_key, app_secret

    # ── sign-token：本地 HMAC 换一次性登录令牌 ─────────────────────────

    async def _sign_token(self, creds: dict, *, force: bool = False) -> dict:
        if not force and self._token and time.time() < self._token_expires_at:
            return {"bot_id": self._bot_id, "token": self._token, "source": "bot"}
        app_key, app_secret = self._require(creds)
        url = f"{api_base()}/api/v5/robotLogic/sign-token"
        headers = {
            "Content-Type": "application/json",
            "X-AppVersion": APP_VERSION,
            "X-OperationSystem": platform.system() or "Windows",
            "X-Instance-Id": TERMINAL_TYPE_ID,
            "X-Bot-Version": BOT_VERSION,
        }
        last = ""
        for attempt in range(4):
            nonce = secrets.token_hex(16)
            timestamp = _bj_timestamp()
            signature = hmac.new(
                app_secret.encode("utf-8"),
                f"{nonce}{timestamp}{app_key}{app_secret}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            body = {"app_key": app_key, "nonce": nonce, "signature": signature, "timestamp": timestamp}
            try:
                async with self.runtime.http.post(
                    url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    text = await resp.text()
                    status = resp.status
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                raise ImError("IM_CONNECTION_FAILED", f"连接元宝 sign-token 接口失败（{exc}）") from None
            if status >= 400:
                raise ImError("IM_CONNECTION_FAILED", f"元宝 sign-token 接口返回 HTTP {status}")
            try:
                payload = json.loads(text)
            except Exception:
                raise ImError("IM_BAD_RESPONSE", "元宝 sign-token 返回体不是 JSON") from None
            code = payload.get("code")
            if code == 0 and isinstance(payload.get("data"), dict):
                data = payload["data"]
                token = str(data.get("token") or "").strip()
                bot_id = str(data.get("bot_id") or "").strip()
                if not token or not bot_id:
                    raise ImError("IM_AUTH_FAILED", "元宝 sign-token 成功但没有返回 token 或 bot_id")
                try:
                    duration = int(data.get("duration") or 0)
                except (TypeError, ValueError):
                    duration = 0
                self._token = token
                self._bot_id = bot_id
                self._token_expires_at = time.time() + max(duration - TOKEN_MARGIN_SECONDS, 60)
                return {"bot_id": bot_id, "token": token, "source": str(data.get("source") or "bot")}
            last = f"code={code} msg={str(payload.get('msg') or payload.get('message') or '')[:160]}"
            if code != SIGN_RETRYABLE:
                break
            await asyncio.sleep(1.0)
        raise ImError(
            "IM_AUTH_FAILED",
            f"元宝拒绝了这份 AppID/AppSecret（{last or '未返回 code'}）。"
            "请回元宝机器人页确认凭据，注意不要复制到多余空格",
        )

    async def verify(self, creds: dict) -> dict:
        """配置阶段只做一次真实签名往返：拿得到 bot_id 才说明凭据是活的"""
        data = await self._sign_token(creds, force=True)
        return {
            "botId": _mask(str(data["bot_id"])),
            "wsUrl": ws_url(),
            "apiBase": api_base(),
            "tokenSource": data.get("source") or "bot",
        }

    async def open_connection(self, creds: dict) -> OpenTicket:
        data = await self._sign_token(creds)
        self._bot_id = str(data["bot_id"])
        return OpenTicket(
            ws_url=ws_url(),
            info={
                "botId": _mask(self._bot_id),
                "wsUrl": ws_url(),
                "apiBase": api_base(),
                "heartbeatSeconds": _seconds("TT_YUANBAO_HEARTBEAT_SECONDS", 5.0),
            },
        )

    # ── 连接建立：auth-bind + 心跳 ──────────────────────────────────────

    async def on_open(self, ws) -> None:
        self._ws = ws
        self._authenticated = False
        self._missed_pong = 0
        self._pending.clear()
        data = await self._sign_token(self.runtime.creds())
        self._bot_id = str(data["bot_id"])
        device = _pack(
            _f_str(1, APP_VERSION),
            _f_str(2, platform.system() or "Windows"),
            _f_str(24, BOT_VERSION),
            _f_str(10, TERMINAL_TYPE_ID),
        )
        auth = _pack(
            _f_str(1, BIZ_ID),
            _f_bytes(2, _pack(_f_str(1, self._bot_id), _f_str(2, "bot"), _f_str(3, str(data["token"])))),
            _f_bytes(3, device),
        )
        await ws.send_bytes(_conn(_head(0, CMD_AUTH, uuid.uuid4().hex, CONN_MODULE), auth))
        self._auth_task = asyncio.create_task(self._auth_watchdog())

    async def _auth_watchdog(self) -> None:
        await asyncio.sleep(_seconds("TT_YUANBAO_AUTH_SECONDS", 10.0))
        if not self._authenticated:
            ws = self._ws
            if ws is not None and not ws.closed:
                await ws.close()

    async def on_close(self) -> None:
        for task in (self._hb_task, self._auth_task):
            if task is not None and not task.done():
                task.cancel()
        self._hb_task = None
        self._auth_task = None
        self._ws = None
        self._authenticated = False
        self._ack_head = None
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(RuntimeError("元宝长连接已关闭，本次回贴没有送达"))
        self._pending.clear()
        # 会话标识与群/单聊类型保留：重连后主动推送仍然要按它选命令；只清消息关联
        for target in self._targets.values():
            target["msgId"] = ""

    async def _keepalive(self) -> None:
        interval = _seconds("TT_YUANBAO_HEARTBEAT_SECONDS", 5.0)
        try:
            while True:
                await asyncio.sleep(interval)
                ws = self._ws
                if ws is None or ws.closed:
                    return
                if self._missed_pong >= 2:
                    await ws.close()
                    return
                self._missed_pong += 1
                ping = _conn(_head(0, CMD_PING, uuid.uuid4().hex, CONN_MODULE))
                await ws.send_bytes(ping)
        except asyncio.CancelledError:
            raise
        except Exception:
            ws = self._ws
            if ws is not None and not ws.closed:
                await ws.close()

    def decode_frame(self, raw: bytes) -> dict | None:
        try:
            return _decode(raw, CONN_SCHEMA)
        except Exception:
            return None

    # ── 解析帧 ─────────────────────────────────────────────────────────

    def parse_event(self, frame: dict) -> InboundEvent:
        self._ack_head = None
        head = frame.get("head") if isinstance(frame.get("head"), dict) else {}
        data = frame.get("data") if isinstance(frame.get("data"), bytes) else b""
        cmd_type = int(head.get("cmdType") or 0)
        cmd = str(head.get("cmd") or "")

        if cmd_type == 1:
            return self._on_response(head, data, cmd)
        if cmd_type == 2:
            return self._on_push(head, data, cmd)
        return InboundEvent(kind="ignore")

    def _on_response(self, head: dict, data: bytes, cmd: str) -> InboundEvent:
        if cmd == CMD_AUTH:
            rsp = _decode(data, AUTH_RSP_SCHEMA) if data else {}
            code = int(rsp.get("code") or 0)
            status = int(head.get("status") or 0)
            if status and status != 0:
                code = status
            if code in AUTH_BAD_TOKEN:
                # 令牌过期/失效：清缓存，让下一轮重新签名再试
                self._token = ""
                self._token_expires_at = 0.0
                self._authenticated = False
                raise ImError(
                    "IM_CONNECTION_FAILED",
                    f"元宝登录令牌被拒（code={code}），已清除本地令牌缓存，将重新签名后重连",
                )
            if code not in (0, AUTH_ALREADY):
                retryable = code in AUTH_RETRYABLE
                raise ImError(
                    "IM_AUTH_FAILED",
                    f"元宝连接鉴权失败（code={code} {str(rsp.get('message') or '')[:120]}）",
                    retryable=retryable,
                )
            self._authenticated = True
            if self._auth_task is not None and not self._auth_task.done():
                self._auth_task.cancel()
            self._auth_task = None
            self._missed_pong = 0
            if self._hb_task is None or self._hb_task.done():
                self._hb_task = asyncio.create_task(self._keepalive())
            return InboundEvent(kind="ignore")

        if cmd == CMD_PING:
            self._missed_pong = 0
            return InboundEvent(kind="ignore")

        msg_id = str(head.get("msgId") or "")
        fut = self._pending.pop(msg_id, None)
        if fut is not None and not fut.done():
            rsp = _decode(data, SEND_RSP_SCHEMA) if data else {}
            status = int(head.get("status") or 0)
            if status:
                rsp["code"] = status
            fut.set_result(rsp)
        return InboundEvent(kind="ignore")

    def _on_push(self, head: dict, data: bytes, cmd: str) -> InboundEvent:
        if head.get("needAck"):
            # ack 要按原帧 head 回（同一 cmd/msgId/module，cmdType=3，换 seqNo）
            self._ack_head = head
        if cmd == CMD_KICKOUT:
            kick = _decode(data, KICKOUT_SCHEMA) if data else {}
            reason = str(kick.get("reason") or "")
            raise ImError(
                "IM_CONNECTION_KICKED",
                "该元宝机器人的长连接被另一条新连接顶掉了（同一个机器人同一时间只有一条有效连接）。"
                f"已停止自动重连，请确认没有另一个通天办公实例或脚本在用这个机器人（{reason or '无原因说明'}）",
                retryable=False,
            )
        push = _decode(data, PUSH_SCHEMA) if data else {}
        payload = push.get("data") if isinstance(push.get("data"), bytes) else data
        cmd_name = str(push.get("cmd") or cmd)
        try:
            inbound = _decode(payload, INBOUND_SCHEMA) if payload else {}
        except ValueError:
            inbound = {}
        if not inbound or not (inbound.get("fromAccount") or inbound.get("msgBody")):
            return InboundEvent(kind="ignore")
        text = "\n".join(
            str((element.get("msgContent") or {}).get("text") or "").strip()
            for element in inbound.get("msgBody") or []
            if str(element.get("msgType") or "") == "TIMTextElem"
            and str((element.get("msgContent") or {}).get("text") or "").strip()
        ).strip()
        from_account = str(inbound.get("fromAccount") or "").strip()
        msg_id = str(inbound.get("msgId") or "").strip()
        if not text or not from_account:
            return InboundEvent(kind="ignore")
        if self._bot_id and from_account == self._bot_id:
            return InboundEvent(kind="ignore")  # 自己发的消息回调，不给自己起任务
        if msg_id and msg_id in self._seen_msgids:
            return InboundEvent(kind="ignore")
        group_code = str(inbound.get("groupCode") or "").strip()
        conversation_id = group_code or from_account
        if msg_id:
            self._seen_msgids.append(msg_id)
            del self._seen_msgids[:-MAX_SEEN_MSGIDS]
        self._targets[conversation_id] = {
            "group": bool(group_code),
            "msgId": msg_id,
            "atMs": int(time.time() * 1000),
        }
        if len(self._targets) > MAX_TARGETS:
            for stale in list(self._targets)[: len(self._targets) - MAX_TARGETS]:
                self._targets.pop(stale, None)
        return InboundEvent(
            kind="inbound",
            text=text,
            conversation_id=conversation_id,
            user_id=from_account,
            message_id=msg_id or cmd_name,
            # 群判定是帧里有的：groupCode 非空即群（回贴寻址也按它分形状）
            is_group=bool(group_code),
            # 「有没有 @ 我」这条通道答不出：抄来的 InboundMessagePush 里没有任何 @ 字段
            # （见 mention_note），策略只能按「答不出」处理，不拿猜当事实。
            mentioned_me=None,
            at_ms=_ms_of_seconds(inbound.get("msgTime")),
        )

    async def ack(self, ws, frame: dict, ev: InboundEvent) -> None:
        head = self._ack_head
        self._ack_head = None
        if not head:
            return
        meta = b"".join(
            _f_bytes(7, _pack(_f_str(1, str(m.get("key") or "")), _f_str(2, str(m.get("value") or ""))))
            for m in head.get("meta") or []
            if isinstance(m, dict)
        )
        ack_head = _pack(
            _f_uint(1, 3),
            _f_str(2, str(head.get("cmd") or "")),
            _f_uint(3, _next_seq()),
            _f_str(4, str(head.get("msgId") or "")),
            _f_str(5, str(head.get("module") or "")),
            _f_bool(6, bool(head.get("needAck"))),
            meta,
            _f_uint(10, int(head.get("status") or 0)),
        )
        try:
            await ws.send_bytes(_conn(ack_head))
        except Exception:
            # ACK 发不出去说明连接已经坏了；收帧循环随后会撞上 CLOSED/ERROR 并重连
            pass

    # ── 回贴 ───────────────────────────────────────────────────────────

    async def _request(self, cmd: str, payload: bytes) -> dict:
        ws = self._ws
        if ws is None or ws.closed or not self._authenticated:
            raise ImError("IM_CONNECTION_FAILED", "元宝长连接不在（或还没通过鉴权），回贴发不出去")
        msg_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[msg_id] = fut
        try:
            await ws.send_bytes(_conn(_head(0, cmd, msg_id, BIZ_MODULE), payload))
            try:
                return await asyncio.wait_for(fut, timeout=SEND_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                raise ImError(
                    "IM_REPLY_FAILED",
                    f"{cmd} 发出去了，但元宝没有在 {int(SEND_TIMEOUT_SECONDS)} 秒内回包（msgId={msg_id}）",
                ) from None
            except RuntimeError as exc:
                raise ImError("IM_REPLY_FAILED", str(exc)) from None
        finally:
            self._pending.pop(msg_id, None)

    async def send(self, creds: dict, conversation_id: str, text: str) -> None:
        target = self._targets.get(conversation_id) or {}
        for chunk in [text[i : i + TEXT_LIMIT] for i in range(0, len(text), TEXT_LIMIT)] or [""]:
            if target.get("group"):
                rsp = await self._request(CMD_SEND_GROUP, _encode_send_group(conversation_id, chunk))
            else:
                rsp = await self._request(CMD_SEND_C2C, _encode_send_c2c(conversation_id, chunk))
            code = int(rsp.get("code") or 0)
            if code != 0:
                raise ImError(
                    "IM_REPLY_FAILED",
                    f"元宝拒绝这条回贴（code={code} {str(rsp.get('message') or '')[:120]}）",
                )
