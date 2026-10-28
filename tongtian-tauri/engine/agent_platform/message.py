"""统一消息结构体与消息总线

所有入口（CLI / IM / 定时任务）先转换成 `AgentMessage`，再交给调度层；
回复统一是 `AgentReply`，按通道 + 会话隔离送回各自的出站队列。
总线不认识 LLM、工具或具体 IM 协议。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

CHANNELS = ("cli", "im", "schedule", "desktop", "api")


def _s(value: Any) -> str:
    return str(value or "").strip()


@dataclass
class AgentMessage:
    text: str
    channel: str = "cli"
    channel_id: str = ""
    conversation_id: str = ""
    sender: str = ""
    message_id: str = field(default_factory=lambda: f"msg-{uuid.uuid4().hex[:12]}")
    skill: str | None = None
    model: str | None = None
    work_dir: str | None = None
    attachments: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def route_key(self) -> str:
        """通道 + 渠道 + 会话：出站回复按它隔离，互不串线。"""
        return f"{self.channel}:{self.channel_id}:{self.conversation_id or 'default'}"

    def validate(self) -> str | None:
        if not _s(self.text):
            return "消息正文为空"
        if self.channel not in CHANNELS:
            return f"未知通道 {self.channel!r}（允许：{'、'.join(CHANNELS)}）"
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.message_id,
            "channel": self.channel,
            "channelId": self.channel_id,
            "conversationId": self.conversation_id,
            "sender": self.sender,
            "text": self.text,
            "skill": self.skill,
            "model": self.model,
            "workDir": self.work_dir,
            "attachments": list(self.attachments),
            "metadata": dict(self.metadata),
            "createdAt": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "AgentMessage":
        if not isinstance(raw, dict):
            raise ValueError("消息必须是对象")
        attachments = raw.get("attachments")
        metadata = raw.get("metadata")
        return cls(
            text=_s(raw.get("text")),
            channel=_s(raw.get("channel")) or "cli",
            channel_id=_s(raw.get("channelId") or raw.get("channel_id")),
            conversation_id=_s(raw.get("conversationId") or raw.get("conversation_id")),
            sender=_s(raw.get("sender")),
            message_id=_s(raw.get("id")) or f"msg-{uuid.uuid4().hex[:12]}",
            skill=_s(raw.get("skill")) or None,
            model=_s(raw.get("model")) or None,
            work_dir=_s(raw.get("workDir") or raw.get("work_dir")) or None,
            attachments=list(attachments) if isinstance(attachments, list) else [],
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )


@dataclass
class AgentReply:
    message_id: str
    route_key: str
    channel: str
    conversation_id: str = ""
    skill_id: str = ""
    text: str = ""
    artifacts: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    error: str = ""
    status: str = "ok"
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.error and self.status == "ok"

    def to_dict(self) -> dict:
        return {
            "messageId": self.message_id,
            "channel": self.channel,
            "conversationId": self.conversation_id,
            "skill": self.skill_id,
            "text": self.text,
            "artifacts": list(self.artifacts),
            "steps": list(self.steps),
            "notes": list(self.notes),
            "usage": dict(self.usage),
            "error": self.error,
            "status": self.status,
            "durationMs": self.duration_ms,
        }


class MessageBus:
    """入站一条队列，出站按 route_key 隔离；消费方只认结构体。"""

    def __init__(self) -> None:
        self.inbound: asyncio.Queue[AgentMessage] = asyncio.Queue()
        self._outbound: dict[str, asyncio.Queue[AgentReply]] = {}
        self._closed = False

    def outbound(self, route_key: str) -> asyncio.Queue[AgentReply]:
        queue = self._outbound.get(route_key)
        if queue is None:
            queue = asyncio.Queue()
            self._outbound[route_key] = queue
        return queue

    async def publish(self, message: AgentMessage) -> AgentMessage:
        error = message.validate()
        if error:
            raise ValueError(error)
        if self._closed:
            raise RuntimeError("消息总线已关闭")
        await self.inbound.put(message)
        return message

    async def receive(self) -> AgentMessage:
        return await self.inbound.get()

    def deliver(self, reply: AgentReply) -> None:
        self.outbound(reply.route_key).put_nowait(reply)

    async def wait_reply(self, route_key: str, timeout: float | None = 600.0) -> AgentReply:
        queue = self.outbound(route_key)
        return await asyncio.wait_for(queue.get(), timeout=timeout)

    def pending_outbound(self, route_key: str) -> int:
        queue = self._outbound.get(route_key)
        return queue.qsize() if queue is not None else 0

    def routes(self) -> list[str]:
        return sorted(self._outbound)

    def close(self) -> None:
        self._closed = True
