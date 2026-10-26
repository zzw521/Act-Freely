"""
通天办公 - 飞书通道（官方 lark-oapi WSClient 的协议 + 框架共用连接循环）

协议事实取自官方 SDK lark-oapi 1.7.5（读的是它的 ws/client.py / ws/const.py /
ws/enum.py / ws/model.py / ws/pb/pbbp2_pb2.py / api/im/v1/model/*.py /
core/token/*.py，不是凭记忆写的）。

这条通道属于 A 族（和 Slack、钉钉同族：换取一次性连接地址 + HTTP 回贴），但帧不是
JSON 而是 protobuf 二进制，所以框架给它开了 `binary_frames` + `decode_frame`：
- 换连接地址：POST {domain}/callback/ws/endpoint，请求体 {"AppID","AppSecret"}，
  头带 locale 与 SDK User-Agent；返回 {code,msg,data:{URL,ClientConfig}}，
  code==0 才是成功；URL 的 query 里带 device_id / service_id（SDK 从这两个参数取
  连接标识，service_id 要回填进心跳帧）
- 帧：pbbp2.Frame{SeqID,LogID,service,method,headers[],payload,...}，method 0=控制帧
  1=数据帧；headers 是 key/value 列表，数据帧里带 type/message_id/sum/seq/trace_id
- 一条消息可能按 sum/seq 拆成多帧下发，收齐才是一次完整事件（SDK 用 5 秒过期的缓存合包），
  没收齐的帧既不是事件也不该回执 —— 框架的 decode_frame 返回 None 正是这个语义
- 回执：把收到的数据帧原样回写，只往 headers 追加 biz_rt（处理耗时毫秒）、payload 换成
  JSON.marshal(Response(code=200))；控制帧不回执，type=card 的数据帧也不回执（SDK 同样）
- 保活是应用层帧：type=ping 的控制帧（service=service_id，method=0，SeqID/LogID=0），
  间隔默认 120 秒，服务端可在 pong 的 payload 里下发 ClientConfig 改这四个值
  （ReconnectCount/ReconnectInterval/ReconnectNonce/PingInterval）
- 回贴走普通 Web API：先 {domain}/open-apis/auth/v3/tenant_access_token/internal 换 token
  （头 Authorization: Bearer），再 POST /open-apis/im/v1/messages/{message_id}/reply；
  没有可回复的消息_id 时用 POST /open-apis/im/v1/messages?receive_id_type=chat_id
- 事件正文是 schema 2.0 的回调 JSON，字段名以 SDK 的 P2ImMessageReceiveV1 /
  EventSender / EventMessage / EventHeader 为准，机器人消息只看 im.message.receive_v1

为什么不跑 SDK 的 ws.Client.start()：它在模块导入时就抓住一个全局事件循环，
`run_until_complete` + `loop.create_task` 的写法和引擎的 uvicorn 循环不能共存，而且自带
一套重连（默认间隔 120 秒、首次随机抖动 30 秒）与框架的 BACKOFF_SECONDS、状态机、会话
台账会成「两套循环、两套重连」。所以只借它的帧编解码、常量与响应模型，传输仍由框架的
aiohttp 会话承担，飞书因此和 Slack、钉钉共用同一条循环。

代价与随之而来的口径（实测，不是猜的）：官方 lark-oapi 是整包发布，20.3MB/10733 个文件，
根 __init__.py 里 `from .api import *` 会把 11120 个模块拉进来说话，开发解释器里实测 import
一次 8~23 秒，随包的 embeddable 运行时实测 33 秒。所以本文件在模块作用域只做
`find_spec("lark_oapi")`（实测 0.01 秒，不执行包代码），真正的 import 推迟到用户点
「绑定/启动连接」那一次，由 load_sdk() 承担并缓存 —— 引擎启动不陪它等三十秒，代价是第一次
点下去要等一会儿，界面按这个事实说明。

Mock 开关：SDK 用 ws.Client(domain=...) 注入开放平台域名，本通道沿同一个口子读环境变量
TT_FEISHU_DOMAIN（测试时指向本机假飞书），心跳间隔用 TT_FEISHU_PING_INTERVAL 覆盖；
合包缓存与 req_id 之类没有环境变量可注入的，测试用假服务端按协议原样发帧。
集成自测见 tests/integration/fake_feishu.py 与 engine_im_feishu_selftest.py。

诚实边界：现在没有真实飞书应用凭据，所以这条链路只在本地按官方协议对拍验证过，界面不能说
「已打通」。另外两条是官方 SDK 常量给的事实，不是本实现的选择：一个应用的长连接数有上限
（EXCEED_CONN_LIMIT=1000040350），换连接地址的接口若返回这个码，退避重连只会继续占名额，
所以按不可重试处理；事件订阅必须在开发者后台选「长连接」，否则换不到连接地址。
app_secret 只进换取连接地址与 token 的请求体，绝不进视图、日志或错误详情。
"""

import asyncio
import json
import os
import time
from importlib.util import find_spec
from urllib.parse import parse_qs, urlparse

from im_channels import CredentialField, ImChannel, ImError, InboundEvent, OpenTicket, register

SDK_PACKAGE = "lark_oapi"
# 只在绑定/启动时加载，所以这里不能 import：模块作用域只回答「装没装」
SDK_PRESENT = find_spec(SDK_PACKAGE) is not None
_SDK: dict | None = None

DEFAULT_DOMAIN = "https://open.feishu.cn"
# 机器人消息事件；其余事件（通讯录、审批等）本通道不消费
EVENT_MESSAGE_RECEIVE = "im.message.receive_v1"
# 合包有效期：与 SDK 的 _combine 一致（self._cache.set(msg_id, buf, 5)）
FRAGMENT_TTL_SECONDS = 5
REPLY_TARGETS = 64
MAX_SEEN_EVENTS = 256
# 服务端没下发 ClientConfig 时的本地默认（SDK 的 ws/client.py 同样是 120）
DEFAULT_PING_SECONDS = 120.0
# tenant_access_token 提前刷新，避免卡在过期边界上
TOKEN_REFRESH_MARGIN_SECONDS = 60


def domain_base() -> str:
    """与 SDK 同一个注入点：ws.Client(domain=...)，默认 https://open.feishu.cn"""
    return (os.environ.get("TT_FEISHU_DOMAIN") or DEFAULT_DOMAIN).rstrip("/")


def ping_seconds_override() -> float:
    raw = os.environ.get("TT_FEISHU_PING_INTERVAL") or ""
    try:
        value = float(raw) if raw else 0.0
    except ValueError:
        return 0.0
    return value if value > 0 else 0.0


def _load_sdk_sync() -> dict:
    """真正 import 官方 SDK（开发解释器 8~23 秒，随包运行时 33 秒），一次进程只付一次代价

    失败如实抛 IM_DEPENDENCY_MISSING：随包运行时缺件时界面会显示「尚未接入」，
    不能让它退化成「按钮能点，转圈之后什么都不发生」。
    """
    global _SDK
    if _SDK is not None:
        return _SDK
    if not SDK_PRESENT:
        raise ImError(
            "IM_DEPENDENCY_MISSING",
            f"引擎运行时未安装官方 {SDK_PACKAGE}（飞书开放平台 SDK），无法按飞书长连接协议收发",
            retryable=False,
        )
    try:
        from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
        from lark_oapi.core.const import AUTHORIZATION, USER_AGENT
        from lark_oapi.core.json import JSON
        from lark_oapi.core.utils.user_agent import build_user_agent
        from lark_oapi.ws import const as ws_const
        from lark_oapi.ws.enum import FrameType, MessageType
        from lark_oapi.ws.model import Response
        from lark_oapi.ws.pb.pbbp2_pb2 import Frame
    except Exception as exc:
        raise ImError(
            "IM_DEPENDENCY_MISSING",
            f"官方 {SDK_PACKAGE} 已登记但导入失败（{type(exc).__name__}: {exc}），飞书通道不可用",
            retryable=False,
        )
    _SDK = {
        "Frame": Frame,
        "FrameType": FrameType,
        "MessageType": MessageType,
        "Response": Response,
        "Event": P2ImMessageReceiveV1,
        "JSON": JSON,
        "const": ws_const,
        "AUTHORIZATION": AUTHORIZATION,
        "USER_AGENT": USER_AGENT,
        "build_user_agent": build_user_agent,
    }
    return _SDK


async def load_sdk() -> dict:
    """到别的线程里加载 SDK：同步 import 二十秒会把 uvicorn 的事件循环整个冻住，
    期间 /health 和其余接口一起停摆，用户看到的就是「整个应用卡住了」"""
    if _SDK is not None:
        return _SDK
    return await asyncio.to_thread(_load_sdk_sync)


def sdk_loaded() -> dict:
    """收帧、解析、心跳这些同步路径只认已就绪的缓存，绝不在这里触发慢 import

    open_connection 先 await load_sdk() 预热，所以正常链路上这里必然命中；
    没命中就是代码顺序错了，如实报错而不是悄悄冻住循环。
    """
    if _SDK is None:
        raise ImError("IM_CHANNEL_ERROR", "飞书通道还没加载完官方 SDK 就收到了帧")
    return _SDK


def _mask(app_id: str) -> str:
    """账号信息里只留足以分辨「绑的是哪个应用」的头尾"""
    if len(app_id) <= 8:
        return app_id[:2] + "…"
    return f"{app_id[:4]}…{app_id[-2:]}"


def _auth_error_codes(sdk: dict) -> set[int]:
    """命中即认为是凭据/权限问题：框架据此停止退避重试"""
    c = sdk["const"]
    return {c.FORBIDDEN, c.AUTH_FAILED, c.NO_CREDENTIAL}


@register
class FeishuChannel(ImChannel):
    kind = "feishu"
    label = "飞书"
    # 随包运行时没装官方 SDK 就退回「尚未接入」，不给能点但必然失败的控件
    supported = SDK_PRESENT
    # 帧是 protobuf 二进制，不是 JSON 文本帧
    binary_frames = True
    # 保活走应用层 ping 帧（SDK 就是这么做的），传输层 ping 关掉
    protocol_heartbeat = None
    setup_url = "https://open.feishu.cn/app"
    setup_note = (
        "飞书开放平台创建企业自建应用 → 「事件与回调」的订阅方式选**长连接** → 添加事件"
        "「im.message.receive_v1（接收消息）」→ 「权限管理」开通机器人发消息权限"
        "→ 复制 App ID 与 App Secret，并发布应用版本。"
        "（与 Slack/钉钉同构：换取 wss 反向连接 + 逐帧回执，不需要公网回调地址）"
        "首次绑定或启动连接要先加载官方 SDK，实测 8~33 秒，这期间会一直转圈。"
    )
    fields = (
        CredentialField(
            key="appId",
            label="App ID",
            placeholder="cli_xxxxxxxxxxxxxxxx",
            help_url="https://open.feishu.cn/app",
            note="应用凭证页，用于换取反向连接地址与 tenant_access_token",
        ),
        CredentialField(
            key="appSecret",
            label="App Secret",
            placeholder="粘贴完整 App Secret",
            help_url="https://open.feishu.cn/app",
            note="只进飞书开放平台请求体，不回显、不写日志",
        ),
    )

    def __init__(self):
        # 合包缓冲：message_id -> (已收到的片, 期望片数, 到期时刻)
        self._fragments: dict[str, tuple[dict[int, bytes], int, float]] = {}
        # conversationId -> 该会话最后一次下发的消息 id（回贴要 reply 的那条）
        self._reply_targets: dict[str, str] = {}
        self._seen_events: list[str] = []
        self._ping_task: asyncio.Task | None = None
        # 服务端 ClientConfig 下发的事实（重连会覆盖），SDK 存在 self._ping_interval 里
        self._ping_interval = DEFAULT_PING_SECONDS
        self._service_id = ""
        self._token = ""
        self._token_expire_at = 0.0

    @property
    def unsupported_reason(self) -> str:
        if not SDK_PRESENT:
            return f"引擎运行时未安装官方 {SDK_PACKAGE}（飞书开放平台 SDK），飞书通道暂不能绑定"
        return super().unsupported_reason

    # ── ①凭据校验：两次真实往返，格式过了不代表事件订阅选了长连接 ──────

    async def verify(self, creds: dict) -> dict:
        app_id, app_secret = self._require(creds)
        # 先确认官方 SDK 加载得到，再花两次网络往返：缺件时不该让绑定转完圈才报失败
        await load_sdk()
        await self._tenant_token(app_id, app_secret, force=True)
        endpoint = await self._endpoint_discovery(app_id, app_secret)
        url = str(endpoint.get("URL") or "")
        if not url.startswith(("ws://", "wss://")):
            raise ImError(
                "IM_BAD_RESPONSE",
                "飞书没有返回长连接地址：请在开放平台「事件与回调」把订阅方式改为长连接后重试",
            )
        return {
            "appId": _mask(app_id),
            "apiBase": domain_base(),
            "subscribeEvent": EVENT_MESSAGE_RECEIVE,
            "pingIntervalSeconds": self._ping_interval,
        }

    def _require(self, creds: dict) -> tuple[str, str]:
        app_id = str(creds.get("appId") or "").strip()
        app_secret = str(creds.get("appSecret") or "").strip()
        if not app_id or not app_secret:
            raise ImError("IM_CREDENTIAL_MISSING", "飞书需要 App ID 与 App Secret 两项凭据")
        if not app_id.startswith("cli_"):
            raise ImError(
                "IM_CREDENTIAL_INVALID",
                f"App ID 应以 cli_ 开头（当前为 {app_id[:4]}…），请回开放平台凭证页复制完整值",
            )
        if len(app_secret) < 16:
            raise ImError(
                "IM_CREDENTIAL_INVALID",
                f"App Secret 长度只有 {len(app_secret)}，飞书的 App Secret 是 32 位左右，请复制完整值",
            )
        return app_id, app_secret

    # ── ②换连接地址（SDK 的 _get_conn_url 同一个形状） ────────────────

    async def open_connection(self, creds: dict) -> OpenTicket:
        app_id, app_secret = self._require(creds)
        # 这里是慢 import 的唯一入口：绑定/启动连接之前，引擎其余代码不碰官方 SDK
        sdk = await load_sdk()
        data = await self._endpoint_discovery(app_id, app_secret)
        url = str(data.get("URL") or "")
        if not url.startswith(("ws://", "wss://")):
            raise ImError("IM_BAD_RESPONSE", "飞书返回的长连接地址不完整，请确认事件订阅已选长连接")
        # SDK 从连接地址的 query 取连接标识：device_id 用于日志，service_id 要回填进心跳帧
        query = parse_qs(urlparse(url).query)
        self._service_id = (query.get(sdk["const"].SERVICE_ID) or [""])[0]
        device_id = (query.get(sdk["const"].DEVICE_ID) or [""])[0]
        return OpenTicket(
            ws_url=url,
            info={
                "appId": _mask(app_id),
                "apiBase": domain_base(),
                "pingIntervalSeconds": self._ping_interval,
                "connectionId": _mask(device_id),
            },
        )

    async def _endpoint_discovery(self, app_id: str, app_secret: str) -> dict:
        """POST {domain}/callback/ws/endpoint → {code,msg,data:{URL,ClientConfig}}"""
        sdk = await load_sdk()
        const = sdk["const"]
        body = {"AppID": app_id, "AppSecret": app_secret}
        headers = {
            "locale": "zh",
            sdk["USER_AGENT"]: sdk["build_user_agent"](extra_tags=["tongtianwork"]),
        }
        resp = await self.runtime.http.post(
            f"{domain_base()}{const.GEN_ENDPOINT_URI}", json=body, headers=headers
        )
        raw = await resp.text()
        try:
            data = json.loads(raw)
        except Exception as exc:
            raise ImError("IM_BAD_RESPONSE", f"飞书长连接地址接口返回体不是 JSON（HTTP {resp.status}）：{exc}")
        if not isinstance(data, dict):
            raise ImError("IM_BAD_RESPONSE", "飞书长连接地址接口返回体不是对象")
        code = int(data.get("code") or 0)
        msg = str(data.get("msg") or "")
        if code == const.OK:
            payload = data.get("data") or {}
            conf = payload.get("ClientConfig")
            if isinstance(conf, dict):
                self._configure(conf)
            return dict(payload)
        if code == const.SYSTEM_BUSY:
            raise ImError("IM_API_ERROR", "飞书长连接地址接口返回 system busy，稍后自动重试")
        if code in _auth_error_codes(sdk):
            raise ImError("IM_AUTH_FAILED", f"飞书拒绝该凭据（code={code} {msg}）", retryable=False)
        if code == const.EXCEED_CONN_LIMIT:
            raise ImError(
                "IM_AUTH_FAILED",
                f"飞书该应用的长连接数已达上限（code={code} {msg}），请先停掉其它连接或旧实例",
                retryable=False,
            )
        raise ImError("IM_API_ERROR", f"飞书长连接地址接口返回 code={code} {msg}")

    def _configure(self, conf: dict) -> None:
        """采用服务端下发的保活间隔（SDK 的 _configure 还改重连次数/间隔/抖动，
        本通道的退避由框架统一负责，所以只取与心跳有关的一项）"""
        raw = conf.get("PingInterval")
        try:
            value = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            return
        if value > 0:
            self._ping_interval = value

    # ── ③带内保活：应用层 ping 帧（间隔由服务端下发） ─────────────────

    async def on_open(self, ws) -> None:
        await self._stop_ping()
        self._ping_task = asyncio.create_task(self._ping_loop(ws))

    async def on_close(self) -> None:
        await self._stop_ping()

    async def _stop_ping(self) -> None:
        task = self._ping_task
        self._ping_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _ping_loop(self, ws) -> None:
        """先睡后发（SDK 的 _ping_loop 也是 finally: sleep），连接一断就自己收摊"""
        while not ws.closed:
            await asyncio.sleep(ping_seconds_override() or self._ping_interval)
            if ws.closed:
                return
            try:
                await ws.send_bytes(self._ping_frame().SerializeToString())
            except Exception:
                return

    def _ping_frame(self):
        sdk = sdk_loaded()
        const = sdk["const"]
        frame = sdk["Frame"]()
        header = frame.headers.add()
        header.key = const.HEADER_TYPE
        header.value = sdk["MessageType"].PING.value
        try:
            frame.service = int(self._service_id or 0)
        except ValueError:
            frame.service = 0
        frame.method = sdk["FrameType"].CONTROL.value
        frame.SeqID = 0
        frame.LogID = 0
        return frame

    # ── ④二进制帧解码：合包 + 控制帧分类 ─────────────────────────────

    def decode_frame(self, raw: bytes) -> dict | None:
        sdk = sdk_loaded()
        const = sdk["const"]
        frame = sdk["Frame"]()
        try:
            frame.ParseFromString(raw)
        except Exception as exc:
            raise ImError("IM_BAD_RESPONSE", f"飞书下发的帧无法按 pbbp2.Frame 解析：{exc}")
        headers = {h.key: h.value for h in frame.headers}
        now_ms = time.time() * 1000
        try:
            method = sdk["FrameType"](frame.method)
        except ValueError:
            return {"kind": "unknown", "type": "", "headers": headers, "payload": b"",
                    "frame": frame, "arrivedMs": now_ms}

        if method == sdk["FrameType"].CONTROL:
            return {"kind": "control", "type": headers.get(const.HEADER_TYPE, ""), "headers": headers,
                    "payload": bytes(frame.payload or b""), "frame": frame, "arrivedMs": now_ms}

        message_id = headers.get(const.HEADER_MESSAGE_ID, "")
        type_name = headers.get(const.HEADER_TYPE, "")
        payload = bytes(frame.payload or b"")
        try:
            total = int(headers.get(const.HEADER_SUM) or 1)
            seq = int(headers.get(const.HEADER_SEQ) or 0)
        except ValueError:
            total, seq = 1, 0
        if total > 1 and message_id:
            merged = self._combine(message_id, total, seq, payload)
            if merged is None:
                return None
            payload = merged
        return {"kind": "data", "type": type_name, "headers": headers, "payload": payload,
                "frame": frame, "arrivedMs": now_ms}

    def _combine(self, message_id: str, total: int, seq: int, payload: bytes) -> bytes | None:
        """与 SDK 的 _combine 同语义：缺片先占位，5 秒没补齐就丢，收齐才返回完整正文"""
        now = time.time()
        entry = self._fragments.get(message_id)
        if entry is None or entry[2] < now or entry[1] != total:
            parts: dict[int, bytes] = {}
        else:
            parts = entry[0]
        parts[seq] = payload
        if len(parts) < total:
            self._fragments[message_id] = (parts, total, now + FRAGMENT_TTL_SECONDS)
            self._prune_fragments(now)
            return None
        self._fragments.pop(message_id, None)
        self._prune_fragments(now)
        return b"".join(parts[i] for i in range(total))

    def _prune_fragments(self, now: float) -> None:
        for key in [k for k, (_, _, expires) in self._fragments.items() if expires < now]:
            self._fragments.pop(key, None)
        while len(self._fragments) > REPLY_TARGETS:
            self._fragments.pop(next(iter(self._fragments)))

    # ── ⑤解析事件 ───────────────────────────────────────────────────

    def parse_event(self, frame: dict) -> InboundEvent:
        kind = frame.get("kind")
        if kind == "control":
            self._on_control_frame(frame)
            return InboundEvent(kind="ignore")
        if kind != "data" or frame.get("type") != "event":
            return InboundEvent(kind="ignore")
        try:
            body = json.loads(frame["payload"].decode("utf-8", "replace"))
        except Exception as exc:
            raise ImError("IM_BAD_RESPONSE", f"飞书事件正文不是 JSON：{exc}")
        if not isinstance(body, dict):
            return InboundEvent(kind="ignore")
        # 用 SDK 的事件模型解一层：字段名以官方 P2ImMessageReceiveV1 为准，不靠记忆
        event = sdk_loaded()["Event"](body)
        header = getattr(event, "header", None)
        if str(getattr(header, "event_type", "") or "") != EVENT_MESSAGE_RECEIVE:
            return InboundEvent(kind="ignore")
        event_id = str(getattr(header, "event_id", "") or "")
        if event_id and not self._remember_event(event_id):
            return InboundEvent(kind="ignore")
        data = getattr(event, "event", None)
        message = getattr(data, "message", None)
        sender = getattr(data, "sender", None)
        conversation_id = str(getattr(message, "chat_id", "") or "").strip()
        if not conversation_id:
            return InboundEvent(kind="ignore")
        if str(getattr(message, "message_type", "") or "") != "text":
            # 富文本/图片/文件不是任务文本，如实忽略而不是猜
            return InboundEvent(kind="ignore")
        if str(getattr(sender, "sender_type", "") or "user") != "user":
            # 机器人自己回贴的那条不能触发第二个任务
            return InboundEvent(kind="ignore")
        text = self._text_of(message)
        if not text:
            return InboundEvent(kind="ignore")
        message_id = str(getattr(message, "message_id", "") or "")
        if message_id:
            self._remember_reply_target(conversation_id, message_id)
        sender_id = getattr(sender, "sender_id", None)
        user_id = str(
            getattr(sender_id, "open_id", "") or getattr(sender_id, "user_id", "") or ""
        )
        return InboundEvent(
            kind="inbound",
            text=text,
            conversation_id=conversation_id,
            user_id=user_id,
            message_id=message_id,
        )

    def _on_control_frame(self, frame: dict) -> None:
        """pong 的 payload 是服务端下发的 ClientConfig（SDK 也是在这里改保活间隔）"""
        if frame.get("type") != "pong":
            return
        payload = frame.get("payload") or b""
        if not payload:
            return
        try:
            conf = json.loads(payload.decode("utf-8", "replace"))
        except Exception:
            return
        if isinstance(conf, dict):
            self._configure(conf)

    @staticmethod
    def _text_of(message) -> str:
        """文本消息的 content 是 JSON 字符串 {"text":"@_user_1 你好"}，@ 占位符按 mentions 去掉"""
        content = getattr(message, "content", "")
        try:
            parsed = json.loads(content) if isinstance(content, str) else content
        except Exception:
            return ""
        text = str((parsed or {}).get("text") or "") if isinstance(parsed, dict) else ""
        for mention in getattr(message, "mentions", None) or []:
            key = str(getattr(mention, "key", "") or "")
            if key:
                text = text.replace(key, "")
        return text.strip()

    def _remember_reply_target(self, conversation_id: str, message_id: str) -> None:
        self._reply_targets[conversation_id] = message_id
        while len(self._reply_targets) > REPLY_TARGETS:
            self._reply_targets.pop(next(iter(self._reply_targets)))

    def _remember_event(self, event_id: str) -> bool:
        """同一条事件只当一次：回执没送出去时飞书会重投，重复触发一个任务比漏一次更难解释"""
        if event_id in self._seen_events:
            return False
        self._seen_events.append(event_id)
        del self._seen_events[: max(0, len(self._seen_events) - MAX_SEEN_EVENTS)]
        return True

    # ── ⑥逐帧回执：原帧回写，只加 biz_rt 与 Response(code=200) ────────

    async def ack(self, ws, frame: dict, ev: InboundEvent) -> None:
        if frame.get("kind") != "data" or frame.get("type") != "event":
            # 控制帧与 card 帧 SDK 都不回执
            return
        sdk = await load_sdk()
        const = sdk["const"]
        proto = frame["frame"]
        arrived = int(frame.get("arrivedMs") or 0)
        elapsed_ms = max(0, int(round(time.time() * 1000)) - arrived) if arrived else 0
        header = proto.headers.add()
        header.key = const.HEADER_BIZ_RT
        header.value = str(max(0, elapsed_ms))
        proto.payload = sdk["JSON"].marshal(sdk["Response"](code=200)).encode("utf-8")
        await ws.send_bytes(proto.SerializeToString())

    # ── ⑦回贴：Web API（reply 优先，退到按 chat_id 主动发） ───────────

    async def send(self, creds: dict, conversation_id: str, text: str) -> None:
        app_id, app_secret = self._require(creds)
        content = json.dumps({"text": text}, ensure_ascii=False)
        message_id = self._reply_targets.get(conversation_id) or ""
        resp = await self._post_message(app_id, app_secret, message_id, conversation_id, content)
        code = int(resp.get("code") or 0)
        if code and "token" in str(resp.get("msg") or "").lower():
            # token 在飞书侧提前失效：换新 token 再发一次，只一次
            self._token = ""
            resp = await self._post_message(app_id, app_secret, message_id, conversation_id, content)
            code = int(resp.get("code") or 0)
        if code:
            raise ImError("IM_REPLY_FAILED", f"回贴失败（飞书 code={code} {str(resp.get('msg') or '')[:200]}）")

    async def _post_message(
        self, app_id: str, app_secret: str, message_id: str, chat_id: str, content: str
    ) -> dict:
        sdk = await load_sdk()
        token = await self._tenant_token(app_id, app_secret)
        # 官方 transport：headers[AUTHORIZATION] = f"Bearer {tenant_access_token}"
        headers = {
            sdk["AUTHORIZATION"]: f"Bearer {token}",
            sdk["USER_AGENT"]: sdk["build_user_agent"](extra_tags=["tongtianwork"]),
        }
        payload = {"content": content, "msg_type": "text"}
        if message_id:
            url = f"{domain_base()}/open-apis/im/v1/messages/{message_id}/reply"
        else:
            # 没有可 reply 的消息（例如进程重启后）就按会话主动发，收件人类型写在 query 上
            url = f"{domain_base()}/open-apis/im/v1/messages?receive_id_type=chat_id"
            payload["receive_id"] = chat_id
        resp = await self.runtime.http.post(url, json=payload, headers=headers)
        raw = await resp.text()
        try:
            data = json.loads(raw)
        except Exception as exc:
            raise ImError("IM_BAD_RESPONSE", f"飞书发消息接口返回体不是 JSON（HTTP {resp.status}）：{exc}")
        return data if isinstance(data, dict) else {}

    async def _tenant_token(self, app_id: str, app_secret: str, force: bool = False) -> str:
        """POST /open-apis/auth/v3/tenant_access_token/internal，带缓存与提前刷新"""
        if not force and self._token and self._token_expire_at - TOKEN_REFRESH_MARGIN_SECONDS > time.time():
            return self._token
        resp = await self.runtime.http.post(
            f"{domain_base()}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        raw = await resp.text()
        try:
            data = json.loads(raw)
        except Exception as exc:
            raise ImError("IM_BAD_RESPONSE", f"飞书 token 接口返回体不是 JSON（HTTP {resp.status}）：{exc}")
        token = str(data.get("tenant_access_token") or "")
        if not token:
            code = data.get("code")
            msg = str(data.get("msg") or "")
            raise ImError(
                "IM_AUTH_FAILED",
                f"飞书拒绝该 App ID/App Secret（code={code} {msg}）",
                retryable=False,
            )
        self._token = token
        try:
            self._token_expire_at = time.time() + float(data.get("expire") or 0)
        except (TypeError, ValueError):
            self._token_expire_at = time.time() + 3600
        return token
