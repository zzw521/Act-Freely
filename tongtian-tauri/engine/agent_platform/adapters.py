"""通道网关：CLI / IM / 定时任务都是平等的 ChannelAdapter。

适配器只做「入口 → AgentMessage」「AgentReply → 出口」的转换，不写 Agent 思考逻辑。
新增渠道 = 新增一个适配器并注册到 ChannelGateway，调度层与总线不用改。

真实收发仍复用存量实现：
- IM：`engine/im_channels.py` 的 ChannelRuntime + 各通道长连接（set_handler 是既有接缝）
- 定时任务：platform.yaml 的 schedules，按固定间隔投递，不引入 cron 依赖
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import time
import traceback
import uuid
from abc import ABC, abstractmethod
from typing import Any

from .dispatcher import AgentDispatcher
from .message import AgentMessage, AgentReply, MessageBus

DEFAULT_TIMEOUT = 900.0

# 与 engine/server.py 同一份通道注册口径：模块导入即向 im_channels 注册实现
IM_CHANNEL_MODULES = ("slack_channel", "dingtalk_channel", "wecom_channel", "feishu_channel")


def load_im_channels() -> tuple[Any, list[str]]:
    """惰性导入 IM 框架与各通道实现；单个通道依赖缺失只影响它自己。"""
    import im_channels as imc

    errors: list[str] = []
    for name in IM_CHANNEL_MODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name} 无法加载：{exc}")
    return imc, errors


class ChannelAdapter(ABC):
    name: str = "adapter"

    def __init__(self, bus: MessageBus, dispatcher: AgentDispatcher):
        self.bus = bus
        self.dispatcher = dispatcher

    async def dispatch(self, message: AgentMessage, timeout: float | None = DEFAULT_TIMEOUT) -> AgentReply:
        """投递到总线并等待本会话的回复；总线保证不同会话互不串线。"""
        await self.bus.publish(message)
        return await self.bus.wait_reply(message.route_key, timeout=timeout)

    @abstractmethod
    async def start(self) -> None:
        ...

    @abstractmethod
    async def stop(self) -> None:
        ...


class CliAdapter(ChannelAdapter):
    """交互式终端：调试与管理入口。"""

    name = "cli"

    def __init__(self, bus: MessageBus, dispatcher: AgentDispatcher,
                 conversation_id: str = "", prompt: str = "> "):
        super().__init__(bus, dispatcher)
        self.conversation_id = conversation_id or f"cli-{uuid.uuid4().hex[:6]}"
        self.prompt = prompt
        self._closed = False

    def to_message(self, text: str, **kwargs: Any) -> AgentMessage:
        return AgentMessage(
            text=text,
            channel="cli",
            channel_id="terminal",
            conversation_id=kwargs.pop("conversation_id", self.conversation_id),
            sender=kwargs.pop("sender", "local"),
            metadata=kwargs.pop("metadata", {}),
            **kwargs,
        )

    async def ask(self, text: str, **kwargs: Any) -> AgentReply:
        return await self.dispatch(self.to_message(text, **kwargs))

    async def start(self) -> None:
        """逐行读取 stdin；空行跳过，`/exit` 或 EOF 结束。"""
        self._closed = False
        print("通天 Agent 平台 · 交互模式（/exit 退出）", flush=True)
        while not self._closed:
            try:
                line = await asyncio.to_thread(input, self.prompt)
            except (EOFError, KeyboardInterrupt):
                print("", flush=True)
                return
            text = (line or "").strip()
            if not text:
                continue
            if text in ("/exit", "/quit"):
                return
            reply = await self.ask(text)
            print(self.render(reply), flush=True)

    async def stop(self) -> None:
        self._closed = True

    @staticmethod
    def render(reply: AgentReply) -> str:
        if reply.error:
            return f"失败：{reply.error}"
        body = reply.text or "（模型没有产出正文）"
        if reply.artifacts:
            body += "\n产出文件：" + "、".join(reply.artifacts[:8])
        if reply.steps:
            body += "\n执行过程：" + " → ".join(reply.steps)
        return body


class ImAdapter(ChannelAdapter):
    """把存量 IM 长连接包装成平等通道：收到消息 → AgentMessage → 回复正文。"""

    def __init__(self, bus: MessageBus, dispatcher: AgentDispatcher, kind: str):
        super().__init__(bus, dispatcher)
        self.kind = kind
        self.name = f"im-{kind}"
        self._started = False

    def to_message(self, text: str, im_session: dict | None = None) -> AgentMessage:
        im_session = im_session or {}
        conversation = str(
            im_session.get("conversationId") or im_session.get("conversation_id") or "default"
        )
        return AgentMessage(
            text=text,
            channel="im",
            channel_id=self.kind,
            conversation_id=conversation,
            sender=str(im_session.get("sender") or im_session.get("userId") or ""),
            metadata={"imSession": im_session},
        )

    async def on_inbound(self, text: str, im_session: dict) -> str:
        """ChannelRuntime.set_handler 的回调；回贴由 IM 通道自己完成。"""
        reply = await self.dispatch(self.to_message(text, im_session))
        return CliAdapter.render(reply)

    async def start(self) -> None:
        imc, _errors = load_im_channels()
        runtime = imc.runtime_of(self.kind)
        if not runtime.configured():
            raise RuntimeError(
                f"{runtime.channel.label} 尚未配置凭据；先在桌面端「消息频道」完成配置"
            )
        runtime.set_handler(self.on_inbound)
        await runtime.start()
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        imc, _errors = load_im_channels()
        await imc.runtime_of(self.kind).stop()
        self._started = False


class ScheduleAdapter(ChannelAdapter):
    """定时任务入口：platform.yaml 的 schedules，按 everySeconds 周期投递。"""

    name = "schedule"

    def __init__(self, bus: MessageBus, dispatcher: AgentDispatcher, schedules: list[dict]):
        super().__init__(bus, dispatcher)
        self.schedules = [s for s in schedules if isinstance(s, dict) and str(s.get("text") or "").strip()]
        self._tasks: list[asyncio.Task] = []

    def to_message(self, item: dict) -> AgentMessage:
        sid = str(item.get("id") or "job").strip() or "job"
        return AgentMessage(
            text=str(item.get("text") or "").strip(),
            channel="schedule",
            channel_id=sid,
            conversation_id=f"schedule-{sid}",
            sender="scheduler",
            skill=str(item.get("skill") or "").strip() or None,
            work_dir=str(item.get("workDir") or "").strip() or None,
            metadata={"schedule": item},
        )

    async def _loop(self, item: dict) -> None:
        try:
            interval = max(5.0, float(item.get("everySeconds") or 3600))
        except (TypeError, ValueError):
            interval = 3600.0
        while True:
            await asyncio.sleep(interval)
            try:
                reply = await self.dispatch(self.to_message(item))
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{stamp}][schedule:{item.get('id')}] {CliAdapter.render(reply)}", flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"[schedule:{item.get('id')}] 投递失败：{exc}", file=sys.stderr, flush=True)

    async def start(self) -> None:
        for item in self.schedules:
            self._tasks.append(asyncio.create_task(self._loop(item)))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()


class ChannelGateway:
    """总线消费者 + 适配器生命周期；调度层只暴露 handle(AgentMessage)。"""

    def __init__(self, bus: MessageBus, dispatcher: AgentDispatcher, workers: int = 2):
        self.bus = bus
        self.dispatcher = dispatcher
        self.workers = max(1, min(8, workers))
        self.adapters: list[ChannelAdapter] = []
        self._tasks: list[asyncio.Task] = []
        self._running = False

    def register(self, adapter: ChannelAdapter) -> ChannelAdapter:
        self.adapters.append(adapter)
        return adapter

    async def _worker(self) -> None:
        while True:
            message = await self.bus.receive()
            try:
                reply = await self.dispatcher.handle(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                reply = AgentReply(
                    message_id=message.message_id,
                    route_key=message.route_key,
                    channel=message.channel,
                    conversation_id=message.conversation_id,
                    error=f"GATEWAY_ERROR: {exc}",
                    status="error",
                )
            self.bus.deliver(reply)

    async def start_workers(self) -> None:
        """只起总线消费者：CLI 一次性命令用它拿到回复，不启动适配器入口。"""
        if self._running:
            return
        self._running = True
        self._tasks = [asyncio.create_task(self._worker()) for _ in range(self.workers)]

    async def start(self) -> None:
        await self.start_workers()
        for adapter in self.adapters:
            await adapter.start()

    async def stop(self) -> None:
        self._running = False
        for adapter in self.adapters:
            try:
                await adapter.stop()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.bus.close()
