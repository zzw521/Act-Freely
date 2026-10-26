"""
通天办公 - IM 频道框架（多通道同构）

业务口径：IM（Slack/钉钉/飞书/企业微信）发起的会话归「频道」，软件端发起的归「任务」。

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

个人微信没有可用的官方机器人长连接，走 PendingChannel 如实拒绝（不做非官方协议）。

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
    return Path(os.environ.get("TT_DATA_HOME") or (Path.home() / ".tongtianwork"))


def config_path() -> Path:
    return _data_home() / "im_channels.json"


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
    """②③ 解析结果：kind 决定框架动作，回执内容一律由通道自己在 ack() 里发"""

    kind: str  # inbound | reconnect | ignore
    text: str = ""
    conversation_id: str = ""
    user_id: str = ""
    message_id: str = ""


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

    def decode_frame(self, raw: bytes) -> "dict | None":
        """二进制帧 → 通道自己的中间表示（parse_event 收到的就是它）

        返回 None 表示这帧还不完整（飞书一条消息会按 sum/seq 拆开发），不进事件流。
        """
        raise ImError("IM_UNSUPPORTED", f"{self.label} 通道未按二进制帧声明 decode_frame")

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
    def __init__(self, channel: ImChannel, ledger: SessionLedger):
        self.channel = channel
        channel.runtime = self
        self.ledger = ledger
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

    @property
    def http(self) -> aiohttp.ClientSession:
        """HTTP 会话按当前事件循环惰性建（引擎的循环在 uvicorn 里，不在模块导入时）"""
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    # ── 配置读写 ──────────────────────────────────────────────────

    def configured(self) -> bool:
        return bool(load_config().get(self.channel.kind))

    def creds(self) -> dict:
        return dict(load_config().get(self.channel.kind) or {})

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
        merged = {**self.creds(), **{k: str(v or "").strip() for k, v in creds.items()}}
        for f in self.channel.fields:
            if f.required and not merged.get(f.key):
                raise ImError("IM_CREDENTIAL_MISSING", f"缺少必填凭据：{f.label}")
        account = await self.channel.verify(merged)
        save_config(self.channel.kind, merged)
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

    async def forget(self) -> None:
        await self.stop()
        drop_config(self.channel.kind)
        self.account = {}
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

    def _spawn(self, ev: InboundEvent) -> None:
        """任务不能压在收帧循环里跑：企业微信的回贴要等的回执正是这个循环读到的下一帧，
        原地 await 会永远等不到。并到后台执行，同一会话内用锁串行，保证台账里一问一答成对。"""
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
        channel = self.channel
        title = (ev.text or "").strip().splitlines()[0][:60] if ev.text.strip() else "（空消息）"
        session_id = f"im-{channel.kind}-{ev.conversation_id}"
        session = self.ledger.upsert(
            session_id, source=channel.kind, title=title, conversation_id=ev.conversation_id
        )
        self.ledger.append_message(session, direction="in", text=ev.text)
        if self._handler is None:
            reply = "引擎未注册消息处理方，本条消息已入台账但未执行任务"
        else:
            try:
                reply = await self._handler(ev.text, session)
            except Exception as exc:
                reply = f"任务执行失败：{exc}"
        self.ledger.append_message(session, direction="out", text=reply)
        try:
            await channel.send(self.creds(), ev.conversation_id, reply)
        except Exception as exc:
            self._set_state("failed", f"IM_REPLY_FAILED: {exc}")

    # ── 对外视图 ──────────────────────────────────────────────────

    def view(self) -> dict:
        creds = self.creds()
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
            "fields": [f.view(bool(creds.get(f.key))) for f in self.channel.fields],
            "sessions": self.ledger.sources().get(self.channel.kind, 0),
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


@register
class WeChatChannel(PendingChannel):
    kind = "wechat"
    label = "微信"
    setup_url = ""
    setup_note = "个人微信无可用的官方机器人长连接，接入方案待定（不做非官方协议）"


def runtime_of(kind: str) -> ChannelRuntime:
    cls = CHANNEL_CLASSES.get(kind)
    if cls is None:
        raise ImError("IM_CHANNEL_UNKNOWN", f"没有名为 {kind} 的 IM 通道")
    runtime = _RUNTIMES.get(kind)
    if runtime is None:
        runtime = ChannelRuntime(cls(), _LEDGER)
        _RUNTIMES[kind] = runtime
    return runtime


def all_runtimes() -> list[ChannelRuntime]:
    """已接入的通道排在未接入之前（界面按这个顺序出卡片，注册顺序本身不保证）"""
    return sorted((runtime_of(kind) for kind in CHANNEL_CLASSES), key=lambda r: not r.channel.supported)


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
    "load_config",
    "register",
    "runtime_of",
    "sessions_path",
    "shutdown",
]
