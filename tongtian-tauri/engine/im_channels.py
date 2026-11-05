"""
通天办公 - IM 频道框架（多通道同构）

业务口径：IM（Slack/钉钉/飞书/企业微信/个人微信/腾讯元宝）发起的会话归「频道」，
软件端发起的归「任务」。

为什么这几类 IM 能用同一套循环：它们的长连接模式机制同构（钉钉 Stream / Slack Socket Mode /
企业微信智能机器人 aibot / 飞书长连接），都是客户端主动向外建连、连接上收帧、把任务回贴给同一个
会话，不需要公网回调地址。但方言有**三族**，别只落在五个实现点上：
    A 族（钉钉 / Slack）：①用凭据换一个一次性反向连接地址 → ②连 wss → ③逐帧回执
                         → ④收到消息后用普通 Web API（HTTP）回贴
    B 族（企业微信 aibot）：连固定 wss 地址，凭据在带内订阅帧里换认证；没有逐帧回执，
                         回贴也走同一条 socket（透传回调帧的 req_id，并等对端在同一条
                         socket 上下发的 errcode 回执）
    A 族的二进制变体（飞书）：换地址、逐帧回执、HTTP 回贴都同 A 族，但帧不是 JSON 文本
                         而是 protobuf envelope，且一条消息会按 sum/seq 拆成多帧下发。
                         框架因此不假设帧的形状：二进制通道用 `binary_frames` +
                         `decode_frame(raw)` 自己回答「这一帧是什么」，返回 None 表示
                         分片未收齐；回执仍走 `ack()`，由通道把整帧原样回写只换 payload。
所以框架另外给这些可选钩子：`on_open(ws)` / `on_close()` 承载带内握手与保活，
`protocol_heartbeat` 让自带应用层心跳的通道关掉传输层 ping。收帧循环因此不能被任务
执行阻塞（回贴要等的回执正是这个循环要读的），任务并到后台、同一会话内串行保序。
连接循环、重连退避、会话台账、状态机全部共用。

第四族是**长轮询 HTTP**（个人微信 iLink）：没有 socket，客户端反复 POST getupdates
让服务端把请求挂到有新消息或超时为止。传输形状差得太远，所以 `transport="long_poll"`
的通道不实现 open_connection 的 ws_url，只实现 `poll_once(creds)` 返回这一轮的事件；
框架仍复用同一套状态机、退避、台账和任务后台化，不另起一套循环。

个人微信与腾讯元宝都按官方协议接入（iLink 机器人 / 元宝开放平台机器人插件），不做任何
非官方协议；但两者都还没有真实凭据做过端到端验收，界面文案不得说成「已打通」。

诚实边界：只有 supported=True 的通道能写凭据并启动。未实现的通道如实返回
IM_UNSUPPORTED，界面显示「尚未接入」，不给能点但什么都不做的控件。
凭据只落 {home}/.tongtianwork/im_channels.json（接口与日志一律脱敏，不出明文）。
"""

import asyncio
import json
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

import data_paths as dp
import im_media as imedia
import im_policy as ipolicy
import im_ratelimit as iratelimit

# ── 错误 ─────────────────────────────────────────────────────────────


class ImError(Exception):
    """`CODE: 原因` 口径与 Rust/引擎其他接口一致，前端 errorTextOf 直接映射

    retryable=False 表示「再连一次也不会变好，而且重试本身有副作用」：例如企业微信同一个
    BotID 只允许一条有效长连接，被新连接顶掉后继续退避重连会变成两个进程互相踢来踢去。
    """

    def __init__(self, code: str, detail: str = "", retryable: bool = True):
        self.code = code
        self.detail = detail
        self.retryable = retryable
        super().__init__(f"{code}: {detail}" if detail else code)


# ── 落盘位置（与 models.json / awareness 同一数据根） ─────────────────


def _data_home() -> Path:
    return dp.engine_home()


def config_path() -> Path:
    return dp.im_config_file()


def sessions_path() -> Path:
    return _data_home() / "im_sessions.json"


def preferences_path() -> Path:
    return _data_home() / "im_preferences.json"


# ── 值对象 ───────────────────────────────────────────────────────────


@dataclass
class CredentialField:
    key: str
    label: str
    placeholder: str = ""
    required: bool = True
    help_url: str = ""
    note: str = ""

    def view(self, present: bool) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "placeholder": self.placeholder,
            "required": self.required,
            "helpUrl": self.help_url,
            "note": self.note,
            "secret": True,
            "present": present,
        }


@dataclass
class OpenTicket:
    """①换取到的反向连接地址 + 该次连接建立后可展示的账号信息"""

    ws_url: str
    info: dict = field(default_factory=dict)


@dataclass
class InboundEvent:
    """②③ 解析结果：kind 决定框架动作，回执内容一律由通道自己在 ack() 里发

    D-41 之后一条消息不再只是一个字符串：
    - `parts` 是这条消息的全部形状（文本 + 图片/文件/语音）。通道**能给出多少就给多少**，
      给不出下载信息的部分照样进列表——框架会把它摊成一行中文说明，不静默丢。
      留空时框架按 `text` 造一个纯文本部分，旧通道一行代码都不用改。
    - `is_group` / `mentioned_me` 是群策略的输入。`mentioned_me=None` 表示**通道答不出**
      「这条有没有 @ 我」，框架因此拒绝凭空判定（见 `im_policy.decide`），不是当成没被 @。
    - `channel_id` 让一帧带上它来自哪个通道实例（同类型多实例时台账与配额都按实例分账）。
    """

    kind: str  # inbound | reconnect | ignore
    text: str = ""
    conversation_id: str = ""
    user_id: str = ""
    message_id: str = ""
    parts: list = field(default_factory=list)
    is_group: bool = False
    mentioned_me: bool | None = None
    channel_id: str = ""
    at_ms: int = 0

    def effective_parts(self) -> list:
        """通道没填 parts 时按旧形状补一个文本部分（单一来源，别在各处 if ev.parts 各写一遍）"""
        if self.parts:
            return list(self.parts)
        return [imedia.ContentPart(kind="text", text=self.text)] if self.text else []


# ── 通道基类：五个实现点，其余交给框架 ──────────────────────────────


class ImChannel(ABC):
    kind: str = ""
    label: str = ""
    supported: bool = False
    fields: tuple[CredentialField, ...] = ()
    setup_url: str = ""
    setup_note: str = ""

    runtime: "ChannelRuntime" = None  # 由 ChannelRuntime 构造时回填（共享它的 HTTP 会话）

    # 传输层 ping 间隔（秒）；自带应用层心跳的通道（企业微信 aibot）设 None，
    # 官方 SDK 也是把 websockets 的 ping_interval 关掉自己发心跳帧的
    protocol_heartbeat: float | None = 10

    # 帧的传输形状：Slack/钉钉/企业微信都是 JSON 文本帧，飞书长连接是 protobuf 二进制帧。
    # 二进制通道的「这一帧到底是什么」只有自己能回答，所以交给 decode_frame，
    # 框架只认 InboundEvent.kind 这一个决定，不把某一家的分帧字段上升成公共约定。
    binary_frames: bool = False

    # 传输形状：websocket（默认，A/B 族与二进制变体）或 long_poll（个人微信 iLink）。
    # 长轮询通道实现 poll_once()，框架用它替代「连 socket 再收帧」那一段。
    transport: str = "websocket"

    # 登录方式：空串表示提交凭据；"qr" 表示扫码授权（个人微信），界面出二维码弹窗
    # 而不是凭据表单。登录态由通道自己保管，凭据只在确认那一帧落盘一次。
    login_mode: str = ""

    # ── D-41 能力位：能不能收附件 / 发附件 / 显示输入状态 ────────────
    # 这三件事都取决于**平台有没有这个动作**，不由框架假设。默认全 False 不是偷懒，
    # 是「没验过协议就不画成能点」——与 supported 同一口径。
    # 关闭原因写成中文（`..._note`），界面直接显示，不逼前端猜「False 是为什么」。
    supports_media_in: bool = False
    supports_media_out: bool = False
    supports_typing: bool = False
    media_in_note: str = "本通道尚未接入附件下载：收到的图片/文件只在消息里以文字说明呈现"
    media_out_note: str = "本通道尚未接入附件发送：回贴只发文本，附件不会被发出"
    typing_note: str = "本通道未接入「正在输入」状态：任务执行期间界面上不会有平台侧的输入提示"

    # 「这条有没有 @ 我」的答案从哪来：只有三种正当来源——回调帧里的字段、
    # 可核对的官方文档口径，或按身份比对（飞书要一次机器人身份查询）。
    # 答不出的通道把 `parse_event` 里的 `mentioned_me` 留 None，群策略因此拒起任务；
    # 这句中文就是界面与台账向用户解释「为什么群里喊它不应」的依据（不许由框架猜）。
    mention_note: str = "本通道没有可用于判定「是否被 @」的协议字段，群内消息在「被 @ 才回应」策略下不起任务"

    async def resolve_media(self, creds: dict, parts: list) -> list:
        """把平台内引用换成可下载地址（钉钉 downloadCode、飞书 file_key 各家一套）

        默认原样返回：框架拿不到 `url` 时会如实标注「通道未实现这一步」，
        不去猜某个下载地址长什么样。
        """
        return list(parts)

    async def download(self, creds: dict, url: str) -> tuple[int, str, bytes]:
        """按已换好的地址取字节：返回 (HTTP 状态, Content-Type, body)

        凭据在这里用（签名下载地址有些平台仍要带头），返回值只交给 `im_media`。
        默认如实拒绝，框架因此把附件标成「未落盘」而不是假装成功。
        """
        raise imedia.MediaError("IM_MEDIA_UNSUPPORTED", self.media_in_note)

    async def set_typing(self, creds: dict, conversation_id: str, on: bool) -> str:
        """下发/撤下「正在输入」；返回中文说明（空串 = 已下发）

        只有 `supports_typing=True` 的通道会被框架调用；默认回一句人话，
        让台账与界面知道「这一步没做」而不是「做了但没效果」。
        """
        return self.typing_note

    async def send_parts(self, creds: dict, conversation_id: str, text: str, parts: list) -> str:
        """带附件的回贴：返回中文说明（空串 = 文本与附件都发出去了）

        默认退回纯文本发送，并把「附件没发出去」如实报出来——静默丢掉附件等于
        对用户谎报「他收到了那份文件」。实现了附件发送的通道自己覆盖这个方法。
        """
        await self.send(creds, conversation_id, text)
        return self.media_out_note if imedia.has_media(parts) else ""

    def decode_frame(self, raw: bytes) -> "dict | None":
        """二进制帧 → 通道自己的中间表示（parse_event 收到的就是它）

        返回 None 表示这帧还不完整（飞书一条消息会按 sum/seq 拆开发），不进事件流。
        """
        raise ImError("IM_UNSUPPORTED", f"{self.label} 通道未按二进制帧声明 decode_frame")

    async def poll_once(self, creds: dict) -> list[InboundEvent]:
        """长轮询通道的一轮：返回这一轮收到的帧解析结果（无消息返回空列表）。

        连接层错误直接抛 ImError / aiohttp 异常，由框架的状态机与退避处理；
        客户端侧的轮询超时是正常控制流，不抛。
        """
        raise ImError("IM_UNSUPPORTED", f"{self.label} 通道不是长轮询传输")

    async def login_start(self) -> dict:
        """扫码登录第一步：生成二维码。返回 {ticket, qrContent, expiresInSeconds}"""
        raise ImError("IM_LOGIN_UNSUPPORTED", f"{self.label} 通道不使用扫码登录")

    async def login_poll(self, ticket: str, verify_code: str = "") -> dict:
        """扫码登录第二步：查一次状态。返回 {status, message}；
        确认时额外返回 credentials（框架负责落盘，绝不回传给界面）与 account（脱敏事实）。"""
        raise ImError("IM_LOGIN_UNSUPPORTED", f"{self.label} 通道不使用扫码登录")

    @property
    def unsupported_reason(self) -> str:
        return f"{self.label} 通道尚未接入引擎侧长连接，当前不可用"

    async def verify(self, creds: dict) -> dict:
        """①凭据能否真换取到连接/账号信息（一次真实往返，不做格式表演）"""
        raise ImError("IM_UNSUPPORTED", self.unsupported_reason)

    @abstractmethod
    async def open_connection(self, creds: dict) -> OpenTicket:
        ...

    @abstractmethod
    def parse_event(self, frame: dict) -> InboundEvent:
        ...

    async def ack(self, ws: "aiohttp.ClientWebSocketResponse", frame: dict, ev: InboundEvent) -> None:
        """③逐帧回执；需要带响应体的（Slack challenge）由通道在这一步一并发出"""
        pass

    async def on_open(self, ws: "aiohttp.ClientWebSocketResponse") -> None:
        """连接建立后要在带内做的事：企业微信在这里发订阅帧（凭据走 socket，不走 HTTP）"""
        pass

    async def on_close(self) -> None:
        """连接结束：撤掉带内保活任务与待回执，不留悬空 task"""
        pass

    @abstractmethod
    async def send(self, creds: dict, conversation_id: str, text: str) -> None:
        ...

    # 框架共用：带鉴权的 JSON 请求（各通道的 Web API 都是这个形状）
    # extra_headers 给需要按开放平台规范带 UA/Accept 的通道（钉钉）用，Content-Type 由框架定死
    async def post_json(
        self, url: str, *, payload: dict | None = None, token: str = "", extra_headers: dict | None = None
    ) -> dict:
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = await self.runtime.http.post(url, json=payload or {}, headers=headers)
        try:
            data = await resp.json(content_type=None)
        except Exception as exc:
            raise ImError("IM_BAD_RESPONSE", f"{url} 返回体不是 JSON（HTTP {resp.status}）：{exc}")
        if not isinstance(data, dict):
            raise ImError("IM_BAD_RESPONSE", f"{url} 返回体不是对象")
        return data

    async def fetch_bytes(self, url: str, *, token: str = "", extra_headers: dict | None = None) -> tuple[int, str, bytes]:
        """下载附件字节：返回 `(HTTP 状态, Content-Type, body)`，形状就是 `im_media` 的 Fetch

        读满 `MAX_PART_BYTES` 就停，不给「平台没说大小、实际两个 G」留机会——超限之后
        `MediaBackend` 也会拒，但那已经是把整个文件读进内存之后了。
        地址是平台下发的签名地址或本通道自己拼的开放平台地址，本方法不猜第三个目标。
        """
        if not str(url or "").startswith(("https://", "http://")):
            raise imedia.MediaError("IM_MEDIA_BAD_URL", "附件下载地址不是 http(s) 绝对地址，不发起请求")
        headers = dict(extra_headers or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        limit = imedia.MAX_PART_BYTES + 1
        async with self.runtime.http.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            body = await resp.content.read(limit)
            return int(resp.status), str(resp.headers.get("Content-Type") or ""), body


# ── 会话台账（IM 发起的对话，append 语义，不改写历史） ───────────────

MAX_MESSAGES_PER_SESSION = 60


class SessionLedger:
    def __init__(self, path: Path):
        self.path = path
        self._sessions: dict[str, dict] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            # 台账损坏时不覆盖：改名留档后重新开一本，坏文件仍在磁盘上可查
            backup = self.path.with_suffix(".corrupt.json")
            try:
                self.path.replace(backup)
            except Exception:
                pass
            return
        items = raw.get("sessions") if isinstance(raw, dict) else None
        if isinstance(items, list):
            for s in items:
                if isinstance(s, dict) and s.get("id"):
                    self._sessions[str(s["id"])] = s

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(self._sessions.values(), key=lambda s: s.get("updatedAtMs", 0), reverse=True)
        self.path.write_text(
            json.dumps({"sessions": ordered[:200]}, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def upsert(self, session_id: str, *, source: str, title: str, conversation_id: str) -> dict:
        self._load()
        now = int(time.time() * 1000)
        sess = self._sessions.get(session_id)
        if sess is None:
            sess = {
                "id": session_id,
                "source": source,
                "title": title,
                "conversationId": conversation_id,
                "createdAtMs": now,
                "updatedAtMs": now,
                "messages": [],
            }
            self._sessions[session_id] = sess
        sess["updatedAtMs"] = now
        return sess

    def append_message(self, session: dict, *, direction: str, text: str) -> None:
        msgs = session.setdefault("messages", [])
        msgs.append({"direction": direction, "text": text, "atMs": int(time.time() * 1000)})
        if len(msgs) > MAX_MESSAGES_PER_SESSION:
            del msgs[: len(msgs) - MAX_MESSAGES_PER_SESSION]
        session["updatedAtMs"] = int(time.time() * 1000)
        self._flush()

    def list(self) -> list[dict]:
        self._load()
        return sorted(self._sessions.values(), key=lambda s: s.get("updatedAtMs", 0), reverse=True)

    def sources(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for s in self.list():
            counts[s.get("source", "")] = counts.get(s.get("source", ""), 0) + 1
        return counts


# ── 运行态：状态机 + 重连退避 + 消息分发 ────────────────────────────

BACKOFF_SECONDS = (1, 2, 5, 10, 30)
# 消息处理方由引擎在启动时注册（IM 文本 → 真实任务），框架不猜业务
MessageHandler = Callable[[str, dict], Awaitable[str]]


class ChannelRuntime:
    def __init__(self, channel: ImChannel, ledger: SessionLedger, *, channel_id: str = ""):
        self.channel = channel
        channel.runtime = self
        self.ledger = ledger
        # 同类型多实例（一个公司两个钉钉企业、两台 Slack 工作区）按实例分账：
        # 凭据、群策略、限流桶、会话台账都挂在 instance_key 上，互不串味。
        # channel_id 为空时 instance_key 就等于 kind —— 这是 D-41 之前的唯一形态，
        # 旧凭据文件与旧策略作用域因此继续生效，不需要用户重新绑定。
        self.channel_id = str(channel_id or "").strip()
        self.instance_key = f"{channel.kind}:{self.channel_id}" if self.channel_id else channel.kind
        self.state = "not_configured"
        self.last_error: dict | None = None
        self.account: dict = {}
        self.connected_at_ms: int | None = None
        self.reconnects = 0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._handler: MessageHandler | None = None
        self._http: aiohttp.ClientSession | None = None
        self._closed = False
        self._pending: set[asyncio.Task] = set()
        self._conv_locks: dict[str, asyncio.Lock] = {}
        # D-41 的三份运行态：谁被挡下了、为什么挡、现在发送是什么状态
        self.dedupe = ipolicy.MessageDedupe()
        self.contexts = ipolicy.ContextBuffer()
        self.governor = iratelimit.SendGovernor()
        self.media = imedia.MediaBackend()
        self.last_skip: dict | None = None
        self.send_note: str = ""
        self.typing_state: str = ""
        self.counters: dict[str, int] = {"inbound": 0, "runs": 0, "skipped": 0, "deduped": 0, "throttled": 0,
                                         "mediaStored": 0, "mediaRejected": 0}

    @property
    def http(self) -> aiohttp.ClientSession:
        """HTTP 会话按当前事件循环惰性建（引擎的循环在 uvicorn 里，不在模块导入时）"""
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    # ── 配置读写 ──────────────────────────────────────────────────

    def configured(self) -> bool:
        return bool(self.creds())

    def creds(self) -> dict:
        """实例键优先，落回 kind：让 D-41 之前绑好的单实例凭据不改文件也照样能跑"""
        data = load_config()
        return dict(data.get(self.instance_key) or data.get(self.channel.kind) or {})

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    @property
    def handler(self) -> MessageHandler | None:
        return self._handler

    # ── 状态机 ────────────────────────────────────────────────────

    def _set_state(self, state: str, error: str = "") -> None:
        self.state = state
        # connectedAtMs 必须与 state 同源：否则界面上会出现「已连接」却没有连接时刻
        self.connected_at_ms = int(time.time() * 1000) if state == "connected" else None
        if error:
            code, _, detail = error.partition(": ")
            self.last_error = {"code": code, "detail": detail, "atMs": int(time.time() * 1000)}
        elif state in ("connected", "stopped"):
            self.last_error = None

    # ── 生命周期 ──────────────────────────────────────────────────

    async def configure(self, creds: dict) -> dict:
        """先真实校验凭据，通过才落盘并起连接；校验失败什么都不写"""
        if not self.channel.supported:
            raise ImError("IM_UNSUPPORTED", self.channel.unsupported_reason)
        if self.channel.login_mode == "qr":
            # 扫码通道的凭据由服务端在确认那一帧下发，不接受手填：多一条写入路径就多一处泄漏面
            raise ImError("IM_LOGIN_REQUIRED", f"{self.channel.label} 需要扫码授权，不能手填凭据")
        merged = {**self.creds(), **{k: str(v or "").strip() for k, v in creds.items()}}
        for f in self.channel.fields:
            if f.required and not merged.get(f.key):
                raise ImError("IM_CREDENTIAL_MISSING", f"缺少必填凭据：{f.label}")
        account = await self.channel.verify(merged)
        save_config(self.instance_key, merged)
        self.account = account
        await self.start()
        return account

    async def start(self) -> None:
        if not self.channel.supported:
            raise ImError("IM_UNSUPPORTED", self.channel.unsupported_reason)
        if not self.creds():
            raise ImError("IM_NOT_CONFIGURED", f"{self.channel.label} 尚未配置凭据")
        if self._task and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task:
            try:
                await asyncio.wait_for(task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                task.cancel()
        # 后台跑着的服务不能比连接活得久：socket 已关，回贴只会撞到「未连接」，不如如实撤掉
        pending = list(self._pending)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._conv_locks.clear()
        self._set_state("stopped" if self.configured() else "not_configured")

    async def login_start(self) -> dict:
        """扫码登录：生成二维码。只有 login_mode="qr" 的通道能做。"""
        if not self.channel.supported:
            raise ImError("IM_UNSUPPORTED", self.channel.unsupported_reason)
        if self.channel.login_mode != "qr":
            raise ImError("IM_LOGIN_UNSUPPORTED", f"{self.channel.label} 不使用扫码登录")
        return await self.channel.login_start()

    async def login_poll(self, ticket: str, verify_code: str = "") -> dict:
        """扫码登录：查一次状态；确认时落盘凭据并起连接（凭据不回传给界面）。"""
        if self.channel.login_mode != "qr":
            raise ImError("IM_LOGIN_UNSUPPORTED", f"{self.channel.label} 不使用扫码登录")
        result = await self.channel.login_poll(str(ticket or "").strip(), str(verify_code or "").strip())
        creds = result.pop("credentials", None)
        if isinstance(creds, dict) and creds:
            save_config(self.instance_key, creds)
            self.account = dict(result.get("account") or {})
            await self.start()
            result["configured"] = True
        return result

    async def forget(self) -> None:
        await self.stop()
        # 只删这个实例自己的登记；实例还在借用旧的全局凭据（没绑过自己的）时删旧键，
        # 否则解绑一个按钮按下去了、creds() 却仍从旧键读到凭据，界面就是假状态。
        data = load_config()
        drop_config(self.instance_key if self.instance_key in data else self.channel.kind)
        self.governor.clear()
        self.dedupe = ipolicy.MessageDedupe()
        self.contexts = ipolicy.ContextBuffer()
        self.account = {}
        self.last_skip = None
        self.send_note = ""
        self._set_state("not_configured")

    async def close(self) -> None:
        self._closed = True
        await self.stop()
        if self._http and not self._http.closed:
            await self._http.close()

    async def _supervise(self) -> None:
        attempt = 0
        while not self._stop.is_set() and not self._closed:
            creds = self.creds()
            if not creds:
                self._set_state("not_configured")
                return
            self._set_state("connecting" if attempt == 0 else "reconnecting")
            try:
                connected = await self._connect_once(creds)
                attempt = 0 if connected else attempt + 1
            except aiohttp.ClientError as exc:
                self._set_state("failed", f"IM_CONNECTION_FAILED: {exc}")
                attempt += 1
            except ImError as exc:
                self._set_state("failed", str(exc))
                # 凭据被拒（鉴权类）不用死磕，等用户改配置；通道自己判定「重试只会更糟」的也不死磕
                if exc.code in ("IM_AUTH_FAILED", "IM_CREDENTIAL_MISSING") or not exc.retryable:
                    return
                attempt += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._set_state("failed", f"IM_CHANNEL_ERROR: {exc}")
                attempt += 1
            if self._stop.is_set() or self._closed:
                return
            delay = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)] if attempt else 1
            if attempt:
                self.reconnects += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(delay, 0.05))
                return
            except asyncio.TimeoutError:
                pass

    async def _connect_once(self, creds: dict) -> bool:
        """返回 True 表示正常连上并收到过帧后关闭；False 表示需要换连接地址重连"""
        channel = self.channel
        if channel.transport == "long_poll":
            return await self._connect_once_poll(creds)
        ticket = await channel.open_connection(creds)
        # 合并而非覆盖：auth.test 拿到的 team/机器人名是配置阶段的事实，重连不该丢掉
        self.account = {**self.account, **ticket.info}
        ws = await self.http.ws_connect(
            ticket.ws_url, heartbeat=channel.protocol_heartbeat, receive_timeout=None
        )
        try:
            # 带内握手（企业微信的订阅帧）先于 connected 发出：握手结果由下一帧的 errcode
            # 决定，没过就抛错，状态机随即落到 failed，不会一路挂着「已连接」
            await channel.on_open(ws)
            self._set_state("connected")
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    frame = json.loads(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    # 飞书这类通道的帧不是 JSON：先由通道解码，None 表示分片还没收齐，
                    # 既不构成事件也就不该回执
                    frame = channel.decode_frame(msg.data)
                    if frame is None:
                        continue
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    if not self._stop.is_set():
                        raise ImError("IM_CONNECTION_CLOSED", f"{channel.label} 长连接被对端关闭：{msg.data}")
                    return True
                else:
                    continue
                ev = channel.parse_event(frame)
                if ev.kind == "reconnect":
                    return False
                # 回执一律由通道自己发：Slack 的 challenge 需要带响应体，钉钉的 SYSTEM 帧是另一种形状，
                # 飞书要把整帧原样回写只换 payload
                await channel.ack(ws, frame, ev)
                if ev.kind == "inbound":
                    self._spawn(ev)
        finally:
            try:
                await channel.on_close()
            finally:
                if not ws.closed:
                    await ws.close()
        return True

    async def _connect_once_poll(self, creds: dict) -> bool:
        """长轮询传输：没有 socket，一轮一轮问；停止信号要能打断挂在服务端的请求。"""
        channel = self.channel
        ticket = await channel.open_connection(creds)
        self.account = {**self.account, **ticket.info}
        self._set_state("connected")
        try:
            while not self._stop.is_set() and not self._closed:
                stop_task = asyncio.create_task(self._stop.wait())
                poll_task = asyncio.create_task(channel.poll_once(creds))
                try:
                    done, _ = await asyncio.wait(
                        {poll_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    if not stop_task.done():
                        stop_task.cancel()
                if poll_task not in done:
                    poll_task.cancel()
                    await asyncio.gather(poll_task, return_exceptions=True)
                    return True
                events = poll_task.result()
                if not stop_task.done():
                    stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)
                for ev in events or []:
                    if ev.kind == "reconnect":
                        return False
                    if ev.kind == "inbound":
                        self._spawn(ev)
        finally:
            await channel.on_close()
        return True

    def _spawn(self, ev: InboundEvent) -> None:
        """任务不能压在收帧循环里跑：企业微信的回贴要等的回执正是这个循环读到的下一帧，
        原地 await 会永远等不到。并到后台执行，同一会话内用锁串行，保证台账里一问一答成对。"""
        if not ev.channel_id:
            # 帧本身不知道自己来自哪个实例；带上的目的是让台账、限流与媒体目录三处同源
            ev.channel_id = self.channel_id
        task = asyncio.create_task(self._run_inbound(ev, self._conversation_lock(ev.conversation_id)))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def _conversation_lock(self, conversation_id: str) -> asyncio.Lock:
        return self._conv_locks.setdefault(conversation_id, asyncio.Lock())

    async def _run_inbound(self, ev: InboundEvent, lock: asyncio.Lock) -> None:
        async with lock:
            if self._stop.is_set():
                return
            try:
                await self.handle_inbound(ev)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 后台任务没人接住，不兜住就静默吞掉一条消息
                self._set_state("failed", f"IM_CHANNEL_ERROR: {exc}")

    async def handle_inbound(self, ev: InboundEvent) -> None:
        """一条入站消息依次过六道闸：排重 → 群策略 → 附件 → 任务 → 限流 → 发送

        为什么是这个顺序：
        - 排重最先：协议重发（钉钉/Slack 会重投同一帧，企微官方要求按 msgid 排重）如果
          放到后面，等于重复下载附件、重复起任务、重复消耗额度。
        - 策略在模型之前：判「不该起任务」时根本不该碰执行面，也不该回贴。
        - 附件在任务之前落盘：任务拿到的是本机路径，不是三小时后就会失效的签名地址。
        - 限流在发送前、任务后：已经花掉的推理额度不假装能退回，但绝不假装已送达。
        """
        channel = self.channel
        now_ms = int(time.time() * 1000)
        parts = ev.effective_parts()
        text = ev.text or imedia.aggregate_text(parts)
        policy = ipolicy.effective(self.channel_id, channel.kind, ev.conversation_id)
        window = int(policy.get("dedupeSeconds") or 0)
        meta = ipolicy.MessageMeta(
            is_group=ev.is_group,
            mentioned_me=ev.mentioned_me,
            conversation_id=ev.conversation_id,
            message_id=ev.message_id,
            text=text,
            at_ms=int(ev.at_ms or now_ms),
            media_identity=imedia.parts_identity(parts),
            has_media=imedia.has_media(parts),
        )
        self.counters["inbound"] += 1
        session_id = f"im-{self.instance_key if self.channel_id else channel.kind}-{ev.conversation_id}"

        # ①重复抑制：挡下就完事，不重复入台账（这条真的已经记过一次了）
        if self.dedupe.seen_recently(meta, window, now_ms):
            self.counters["deduped"] += 1
            self.last_skip = {"kind": "dedupe", "reason": self.dedupe.note(window), "atMs": now_ms}
            return

        # ②群策略/单聊策略
        decision = ipolicy.decide(meta, policy, now_ms=now_ms, buffer=self.contexts)
        if not decision.run:
            self.counters["skipped"] += 1
            self.last_skip = {"kind": "policy", "reason": decision.reason, "atMs": now_ms}
            title = (text or "").strip().splitlines()[0][:60] if text.strip() else "（空消息）"
            session = self.ledger.upsert(
                session_id, source=channel.kind, title=title, conversation_id=ev.conversation_id
            )
            # 消息确实到了，如实入台账；「为什么没起任务」跟在后面，不是替用户猜
            self.ledger.append_message(session, direction="in", text=f"{text}\n（未起任务：{decision.reason}）")
            return

        # ③附件：能力位与策略双闸，任何一闸关着都以中文说明呈现，不静默丢
        parts = await self._prepare_media(parts, ev, policy)
        text = imedia.aggregate_text(parts)
        title = (text or "").strip().splitlines()[0][:60] if text.strip() else "（空消息）"
        session = self.ledger.upsert(
            session_id, source=channel.kind, title=title, conversation_id=ev.conversation_id
        )
        self.ledger.append_message(session, direction="in", text=text)
        self.counters["runs"] += 1
        task_text = text
        if decision.context:
            task_text = (
                "【群内此前的消息：按策略只作上下文，未 @ 我时不起任务】\n"
                + "\n".join(f"- {line}" for line in decision.context)
                + f"\n\n【本次被 @ 的消息】\n{text}"
            )

        # ④任务（输入状态只在平台真有的时候下发）
        typing_state = await self._set_typing(ev, True)
        try:
            if self._handler is None:
                reply = "引擎未注册消息处理方，本条消息已入台账但未执行任务"
            else:
                try:
                    reply = await self._handler(task_text, session)
                except Exception as exc:
                    reply = f"任务执行失败：{exc}"
        finally:
            self.typing_state = await self._set_typing(ev, False) or typing_state
        await self._deliver(ev, session, reply, parts, policy)

    async def _prepare_media(self, parts: list, ev: InboundEvent, policy: dict) -> list:
        """附件能不能落到本机：通道有没有这个动作 + 用户有没有同意，两个条件都要过"""
        media = [p for p in parts if p.kind != "text"]
        if not media:
            return list(parts)
        if not self.channel.supports_media_in:
            self.counters["mediaRejected"] += len(media)
            return imedia.unsupported_note(parts, self.channel.media_in_note)
        allow = str(policy.get("groupMedia" if ev.is_group else "dmMedia") or "off") == "on"
        if not allow:
            self.counters["mediaRejected"] += len(media)
            return imedia.unsupported_note(parts, "按当前策略没有下载保存附件（只在消息里以文字说明呈现）")
        try:
            resolved = await self.channel.resolve_media(self.creds(), parts)
        except Exception as exc:  # 换地址失败不影响文本任务：如实标注后照样往下走
            self.counters["mediaRejected"] += len(media)
            return imedia.unsupported_note(parts, f"附件下载地址换取失败：{type(exc).__name__}")
        try:
            out = await self.media.materialize(
                resolved,
                channel_id=self.instance_key,
                conversation_id=ev.conversation_id,
                fetch=self._fetch_media,
            )
        except imedia.MediaError as exc:
            self.counters["mediaRejected"] += len(media)
            return imedia.unsupported_note(parts, f"附件目录不可用：{exc.detail or exc.code}")
        self.counters["mediaStored"] += len([p for p in out if p.local_path])
        self.counters["mediaRejected"] += len([p for p in out if p.kind != "text" and not p.local_path])
        return out

    async def _fetch_media(self, url: str):
        return await self.channel.download(self.creds(), url)

    async def _set_typing(self, ev: InboundEvent, on: bool) -> str:
        """平台侧输入状态：能力位为假时连默认实现都不调（不产生「做了但没效果」的假动作）"""
        if not self.channel.supports_typing:
            return ""
        try:
            return await self.channel.set_typing(self.creds(), ev.conversation_id, on) or ""
        except Exception as exc:
            return f"输入状态下发失败：{type(exc).__name__}"

    async def _deliver(self, ev: InboundEvent, session: dict, reply: str, parts: list, policy: dict) -> None:
        """⑤限流 → ⑥发送：发不出去就在台账里补一条引擎状态，而不是让它看起来已经送达"""
        channel = self.channel
        key = f"{self.instance_key}|{ev.conversation_id}"
        # 用户改过的限流额度先落到闸门，再问闸门能不能发：否则策略里的两项数字只是界面装饰
        self.governor.apply_limits(
            policy.get("maxMessagesPerMinute"), policy.get("burst") or ipolicy.POLICY_DEFAULTS["burst"]
        )
        gate = self.governor.check(key)
        self.ledger.append_message(session, direction="out", text=reply)
        if not gate.allowed:
            self.counters["throttled"] += 1
            self.send_note = gate.reason
            self.ledger.append_message(session, direction="out", text=f"（引擎状态：本条回复未发往 IM——{gate.reason}）")
            return
        seconds = float(policy.get("sendTimeoutSeconds") or ipolicy.POLICY_DEFAULTS["sendTimeoutSeconds"])
        try:
            note = await iratelimit.bounded_send(
                lambda: channel.send_parts(self.creds(), ev.conversation_id, reply, parts),
                seconds,
                label=f"{channel.label}回贴",
                governor=self.governor,
            )
        except iratelimit.RateLimitError as exc:
            detail = str(exc)
            if iratelimit.is_rate_limited(message=detail) or exc.code == "IM_RATE_LIMITED":
                penalized = self.governor.penalize(key, "", why=detail[:80])
                self.send_note = penalized.reason
                self.ledger.append_message(session, direction="out", text=f"（引擎状态：对端限流——{penalized.reason}）")
                return
            self._set_state("failed", f"IM_REPLY_FAILED: {exc}")
            return
        if note:  # 超时/部分未发出：连接本身是好的，不该把状态机打成 failed
            self.send_note = note
            self.ledger.append_message(session, direction="out", text=f"（引擎状态：{note}）")
        else:
            self.send_note = ""

    # ── 对外视图 ──────────────────────────────────────────────────

    def view(self) -> dict:
        creds = self.creds()
        policy = ipolicy.effective(self.channel_id, self.channel.kind)
        return {
            "kind": self.channel.kind,
            "label": self.channel.label,
            "supported": self.channel.supported,
            "unsupportedReason": channel_reason(self.channel),
            "configured": bool(creds),
            "state": self.state,
            "running": bool(self._task and not self._task.done()),
            "connectedAtMs": self.connected_at_ms,
            "reconnects": self.reconnects,
            "lastError": self.last_error,
            "account": self.account,
            "setupUrl": self.channel.setup_url,
            "setupNote": self.channel.setup_note,
            "loginMode": self.channel.login_mode,
            "fields": [f.view(bool(creds.get(f.key))) for f in self.channel.fields],
            "sessions": self.ledger.sources().get(self.channel.kind, 0),
            # ── D-41：实例、能力、策略、限流与媒体五组事实 ──────────────
            # 界面不许自己判断「这个平台能不能收图」「还有几秒恢复发送」：
            # 这些都只有引擎知道，抄一份到前端就会和引擎漂移。
            "channelId": self.channel_id,
            "instanceKey": self.instance_key,
            "capabilities": {
                "mediaIn": bool(self.channel.supports_media_in),
                "mediaOut": bool(self.channel.supports_media_out),
                "typing": bool(self.channel.supports_typing),
                "mediaInNote": self.channel.media_in_note,
                "mediaOutNote": self.channel.media_out_note,
                "typingNote": self.channel.typing_note,
                "mentionEvidence": self.channel.mention_note,
            },
            "policy": dict(policy),
            "policyLabels": {
                "groupActivation": ipolicy.ACTIVATION_LABELS.get(str(policy.get("groupActivation")), ""),
                "dmActivation": ipolicy.ACTIVATION_LABELS.get(str(policy.get("dmActivation")), ""),
                "groupContext": ipolicy.CONTEXT_MODE_LABELS.get(str(policy.get("groupContext")), ""),
            },
            "lastSkip": self.last_skip,
            "sendNote": self.send_note,
            "typingState": self.typing_state,
            "dedupeNote": self.dedupe.note(int(policy.get("dedupeSeconds") or 0)),
            "rateLimit": self.governor.view(),
            "media": self.media.footprint(self.instance_key),
            "counters": dict(self.counters),
        }


def channel_reason(channel: ImChannel) -> str:
    if channel.supported:
        return ""
    return channel.unsupported_reason


# ── 配置台账（只增量写对应 kind，不动别人的凭据） ───────────────────


def load_config() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_config(kind: str, creds: dict) -> None:
    data = load_config()
    data[kind] = creds
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def drop_config(kind: str) -> None:
    data = load_config()
    if kind in data:
        data.pop(kind)
        config_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 频道偏好（回贴正文是引擎拼的，所以这个开关的事实来源也在引擎） ──────

PREFERENCE_DEFAULTS: dict = {"showToolProcess": False}


def load_preferences() -> dict:
    """读偏好：文件缺失或字段类型不对时逐项退回出厂默认，不抛。

    回贴路径每收到一条消息都会走这里，配置坏了不能让消息卡住。"""
    data = dict(PREFERENCE_DEFAULTS)
    try:
        raw = json.loads(preferences_path().read_text(encoding="utf-8"))
    except Exception:
        return data
    if not isinstance(raw, dict):
        return data
    for key, default in PREFERENCE_DEFAULTS.items():
        value = raw.get(key)
        if isinstance(value, type(default)):
            data[key] = value
    return data


def save_preferences(patch: dict) -> dict:
    """写偏好：只认已知键且必须是布尔，未知键直接报 IM_PREFERENCE_INVALID。

    不做「先收下再说」——设置页拨一下开关若没真的生效，用户看到的就是假状态。"""
    if not isinstance(patch, dict) or not patch:
        raise ImError("IM_PREFERENCE_INVALID", "未提交任何偏好项")
    data = load_preferences()
    for key, value in patch.items():
        if key not in PREFERENCE_DEFAULTS:
            raise ImError("IM_PREFERENCE_INVALID", f"未知偏好项：{key}")
        if not isinstance(value, bool):
            raise ImError("IM_PREFERENCE_INVALID", f"{key} 只接受 true/false")
        data[key] = value
    p = preferences_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


# ── 注册表（多通道同构：新增通道只在这里挂一次） ────────────────────

CHANNEL_CLASSES: dict[str, type[ImChannel]] = {}
_RUNTIMES: dict[str, ChannelRuntime] = {}
_LEDGER = SessionLedger(sessions_path())


def register(cls: type[ImChannel]) -> type[ImChannel]:
    CHANNEL_CLASSES[cls.kind] = cls
    return cls


class PendingChannel(ImChannel):
    """已列入频道清单、但引擎侧长连接还没实现的通道。

    存在的意义是让「支持哪些 IM」这个事实只有一份来源（引擎），前端不用另抄一份；
    任何写凭据/启动的调用都如实抛 IM_UNSUPPORTED，界面对应「尚未接入」的禁用态。
    """

    fields = ()

    def _no(self) -> ImError:
        return ImError("IM_UNSUPPORTED", self.unsupported_reason)

    async def open_connection(self, creds: dict) -> OpenTicket:
        raise self._no()

    def parse_event(self, frame: dict) -> InboundEvent:
        raise self._no()

    async def send(self, creds: dict, conversation_id: str, text: str) -> None:
        raise self._no()


def runtime_of(kind: str, channel_id: str = "") -> ChannelRuntime:
    """取某个通道实例的运行态（D-41 起同类型可以有多个实例）

    `channel_id` 为空就是 D-41 之前那个唯一实例：注册表键、凭据键、会话台账前缀
    全部等于 `kind`，旧配置与既有调用（`server.py` 的 `/api/im/{kind}/*`）一行都不用改。
    每个实例各自 `cls()`：两条钉钉企业各有自己的 socket、回贴地址与限流桶，
    共用一个通道对象就等于共用 `_reply_targets`——那会让 A 企业的回贴贴到 B 企业群里。
    """
    cls = CHANNEL_CLASSES.get(kind)
    if cls is None:
        raise ImError("IM_CHANNEL_UNKNOWN", f"没有名为 {kind} 的 IM 通道")
    key = f"{kind}:{str(channel_id or '').strip()}" if str(channel_id or "").strip() else kind
    runtime = _RUNTIMES.get(key)
    if runtime is None:
        runtime = ChannelRuntime(cls(), _LEDGER, channel_id=str(channel_id or "").strip())
        _RUNTIMES[key] = runtime
    return runtime


def instances_of(kind: str) -> list[ChannelRuntime]:
    """同一类型下已建起来的所有实例（含唯一实例）；界面列实例时不必猜有哪些标识"""
    return [r for r in _RUNTIMES.values() if r.channel.kind == kind]


def all_runtimes() -> list[ChannelRuntime]:
    """已接入的通道排在未接入之前（界面按这个顺序出卡片，注册顺序本身不保证）

    先保证每个登记过的 kind 都有一个唯一实例（旧行为），再把多出来的实例带上：
    清单因此不会因为「有人建过第二个实例」而少掉任何一个通道。
    """
    for kind in CHANNEL_CLASSES:
        runtime_of(kind)
    return sorted(_RUNTIMES.values(), key=lambda r: (not r.channel.supported, r.channel.kind, r.channel_id))


async def shutdown() -> None:
    for runtime in list(_RUNTIMES.values()):
        await runtime.close()
    _RUNTIMES.clear()


def im_sessions() -> list[dict]:
    return _LEDGER.list()


__all__ = [
    "ChannelRuntime",
    "CredentialField",
    "ImChannel",
    "ImError",
    "InboundEvent",
    "OpenTicket",
    "all_runtimes",
    "channel_reason",
    "config_path",
    "drop_config",
    "im_sessions",
    "instances_of",
    "load_config",
    "register",
    "runtime_of",
    "sessions_path",
    "shutdown",
]
