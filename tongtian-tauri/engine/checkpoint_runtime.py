# -*- coding: utf-8 -*-
"""任务检查点 / 断点续跑运行时。

能力边界（界面与文档同一口径）：
- 中断/取消/失败收尾时，把可恢复的快照与副作用台账写一份到本机磁盘；
  正常完成与回滚会删除磁盘检查点。隐私模式不落盘。引擎重启后仍可从磁盘恢复，
  刷新页面则通过查询接口重新显示「从检查点继续」入口。
- 每次模型调用前保存一次 Agent 记忆快照；工具副作用按「工具名 + 输入摘要」
  记录成功结果。恢复时只跳过中断前已成功执行过的同签名副作用调用，
  本次恢复中新产生的调用照常执行（同一轮内重复写文件仍是合法操作）。
- 中断发生在「工具已执行、响应尚未写成快照」的极窄窗口时，靠副作用台账
  兜底；台账在工具响应返回模型的同一时刻写入，写盘类工具同输入重放无害。
- 磁盘文件只包含恢复所需的对话快照、副作用台账与会话上下文；模型密钥、
  Hook 网关令牌、MCP 环境变量一律不写入。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

# 单份快照的体积上限（JSON 字符数）：超出时从最早消息开始丢弃并如实标记
MAX_CHECKPOINT_CHARS = 400_000
# 单条工具结果的保留上限：恢复需要的是「工具做过什么、返回了什么关键信息」
MAX_TOOL_RESULT_CHARS = 4_000
# 工具调用输入（如写文件正文）的保留上限
MAX_TOOL_INPUT_CHARS = 8_000
# 副作用台账条数上限（每条约几 KB，进程内内存可接受）
MAX_TOOL_RECORDS = 1_000

_MEDIA_TYPES = {"image", "audio", "video"}
_READ_ONLY_TOOLS = {"view_text_file", "use_skill"}
_SIDE_EFFECT_TOOLS = {
    "write_text_file",
    "insert_text_file",
    "generate_pptx",
    "spawn_subagents",
}
_SIDE_EFFECT_KEYWORDS = (
    "write", "create", "delete", "remove", "move", "rename", "upload",
    "send", "execute", "insert", "append", "save", "export", "publish",
    "shell", "command", "run",
)


def is_side_effect_tool(name: str) -> bool:
    """保守判定：可能改变工作目录或外部状态的工具一律按副作用处理。

    宁可多记一条台账（恢复时跳过一次只读调用最多损失一次刷新），
    也不漏记一次真实写盘/外发。"""
    tool = (name or "").strip()
    if not tool or tool in _READ_ONLY_TOOLS:
        return False
    if tool in _SIDE_EFFECT_TOOLS:
        return True
    lowered = tool.lower()
    return any(keyword in lowered for keyword in _SIDE_EFFECT_KEYWORDS)


def signature_of(name: str, args: Any) -> str:
    """工具调用指纹：工具名 + 规范化 JSON 输入（不保存原始参数）。"""
    try:
        canonical = json.dumps(
            args if isinstance(args, (dict, list)) else (args or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        canonical = str(args)
    return hashlib.sha256(f"{name}\n{canonical}".encode("utf-8", "replace")).hexdigest()


# ── 磁盘检查点 ────────────────────────────────────────────────
#
# 位置与 models.json / 意识目录同一套 TONGTIANWORK_HOME 规则；文件名用 taskId 的
# 哈希，避免外部传入的 id 拼出目录穿越路径。文件里只有恢复所需内容，不含密钥。

CHECKPOINT_VERSION = 1
CHECKPOINT_TTL_SECONDS = 7 * 24 * 3600
MAX_DISK_CHECKPOINT_CHARS = 16_000_000


def checkpoint_home() -> Path:
    root = str(os.environ.get("TONGTIANWORK_HOME") or "").strip()
    base = Path(root) if root else Path.home() / ".tongtianwork"
    return base / "checkpoints"


def _checkpoint_file(task_id: str) -> Path:
    digest = hashlib.sha256(str(task_id).encode("utf-8", "replace")).hexdigest()
    return checkpoint_home() / f"{digest}.json"


def delete_checkpoint(task_id: str) -> None:
    try:
        _checkpoint_file(task_id).unlink(missing_ok=True)
    except Exception:
        pass


def save_checkpoint(task_id: str, payload: dict) -> tuple[bool, str]:
    """原子写入磁盘；返回 (是否成功, 失败原因)。失败不抛异常，不影响任务收尾。"""
    if not task_id:
        return False, "taskId 为空"
    try:
        data = {
            "version": CHECKPOINT_VERSION,
            "taskId": task_id,
            "savedAtMs": int(time.time() * 1000),
            **payload,
        }
        encoded = json.dumps(data, ensure_ascii=False, default=str)
        if len(encoded) > MAX_DISK_CHECKPOINT_CHARS:
            return False, (
                f"检查点体积 {len(encoded)} 字符超过磁盘上限 {MAX_DISK_CHECKPOINT_CHARS}"
            )
        path = _checkpoint_file(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{int(time.time() * 1000)}")
        tmp.write_text(encoded, encoding="utf-8")
        os.replace(tmp, path)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def load_checkpoint(task_id: str) -> dict | None:
    """读取磁盘检查点；版本不符/过期/损坏一律按不存在处理（并清掉坏文件）。"""
    if not task_id:
        return None
    path = _checkpoint_file(task_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("taskId") != task_id:
            delete_checkpoint(task_id)
            return None
        if int(data.get("version") or 0) != CHECKPOINT_VERSION:
            delete_checkpoint(task_id)
            return None
        saved_ms = int(data.get("savedAtMs") or 0)
        if not saved_ms or time.time() * 1000 - saved_ms > CHECKPOINT_TTL_SECONDS * 1000:
            delete_checkpoint(task_id)
            return None
        if not isinstance(data.get("messages"), list):
            delete_checkpoint(task_id)
            return None
        return data
    except Exception:  # noqa: BLE001
        delete_checkpoint(task_id)
        return None


def public_info(task_id: str, data: dict) -> dict:
    """查询接口的对外视图：只报告状态与计数，不返回对话内容/工具参数。"""
    saved_ms = int(data.get("savedAtMs") or 0)
    stats = data.get("stats") if isinstance(data.get("stats"), dict) else {}
    return {
        "type": "checkpoint",
        "taskId": task_id,
        "resumable": bool(data.get("resumable")) and bool(data.get("messages")),
        "reason": str(data.get("reason") or ""),
        "savedAt": (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(saved_ms / 1000))
            if saved_ms else ""
        ),
        "scope": "disk",
        "snapshotMessages": len(data.get("messages") or []),
        "executedTools": int(data.get("executedSideEffects") or 0),
        "sideEffects": len(data.get("toolRecords") or {}),
        "skippedDuplicates": int(stats.get("skippedDuplicates") or 0),
        "modelCalls": int(data.get("modelCalls") or 0),
        "mediaOmitted": int(data.get("mediaOmitted") or 0),
        "droppedMessages": int(data.get("droppedMessages") or 0),
        "captureError": str(data.get("captureError") or ""),
    }


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…（已截断）"


def _safe_json(value: Any, limit: int = MAX_TOOL_INPUT_CHARS) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return _truncate(str(value), limit)
    if len(encoded) <= limit:
        return value
    return _truncate(encoded, limit)


def _sanitize_output(value: Any, stats: dict) -> Any:
    if isinstance(value, str):
        return _truncate(value, MAX_TOOL_RESULT_CHARS)
    if isinstance(value, list):
        return [_sanitize_block(block, stats) for block in value]
    return _safe_json(value, MAX_TOOL_RESULT_CHARS)


def _sanitize_block(block: Any, stats: dict) -> Any:
    if not isinstance(block, dict):
        return _safe_json(block, MAX_TOOL_RESULT_CHARS)
    kind = str(block.get("type") or "")
    if kind in _MEDIA_TYPES:
        stats["mediaOmitted"] = int(stats.get("mediaOmitted") or 0) + 1
        label = {"image": "图片", "audio": "音频", "video": "视频"}.get(kind, "媒体")
        return {"type": "text", "text": f"[检查点：此处有一段{label}内容，未保存在内存快照中]"}
    if kind == "text":
        return {"type": "text", "text": _truncate(str(block.get("text") or ""), MAX_TOOL_RESULT_CHARS)}
    if kind == "tool_use":
        return {
            "type": "tool_use",
            "id": str(block.get("id") or ""),
            "name": str(block.get("name") or ""),
            "input": _safe_json(block.get("input") or {}),
        }
    if kind == "tool_result":
        return {
            "type": "tool_result",
            "id": str(block.get("id") or ""),
            "name": str(block.get("name") or ""),
            "output": _sanitize_output(block.get("output"), stats),
        }
    return _safe_json(block, MAX_TOOL_RESULT_CHARS)


def _sanitize_content(content: Any, stats: dict) -> Any:
    if isinstance(content, str):
        return _truncate(content, MAX_TOOL_RESULT_CHARS)
    if isinstance(content, list):
        return [_sanitize_block(block, stats) for block in content]
    return _truncate(str(content), MAX_TOOL_RESULT_CHARS)


class TaskCheckpoint:
    """单个任务的检查点状态（进程内）。"""

    def __init__(self, task_id: str):
        self.task_id = task_id
        # 最近一次成功采集的记忆快照（list[dict]）；空表示还没有可恢复点
        self.messages: list[dict] = []
        self.saved_at: float | None = None
        self.size_chars = 0
        self.dropped_messages = 0
        self.media_omitted = 0
        self.model_calls = 0
        self.capture_error = ""
        # 是否可作为恢复起点（模型调用前快照已存在且未正常收尾）
        self.resumable = False
        self.reason = "尚未产生检查点"
        self.finished_at: float | None = None
        # 副作用台账：signature → {tool, content, metadata, at, run}
        self.tool_records: dict[str, dict] = {}
        self.records_overflow = False
        # 本次执行（approve / resume 各算一次）的序号
        self.run_id = 0
        self.skipped_duplicates = 0
        self.executed_side_effects = 0
        # 执行前文件基线：None 表示尚未采集（空目录是合法的已采集状态）
        self.baseline_files: dict | None = None
        self.restore_count = 0
        self.last_restore: dict = {}
        # 由 _run_execution 注入：把过程说明放进本轮事件队列
        self.emit: Callable[[str, dict], None] | None = None
        # 正常收尾时清掉快照前，先把统计留给 checkpoint 事件
        self.last_summary: dict = {}
        # same-process=只在当前引擎进程内可恢复；disk=已写入磁盘，重启后仍可恢复
        self.scope = "same-process"
        # 最近一次写盘失败的原因（写盘失败不影响进程内续跑）
        self.disk_error = ""
        # 写盘时为控制体积省略的图片附件数量
        self.attachments_media_omitted = 0

    # ── 生命周期 ──────────────────────────────────────────────

    def begin_run(self) -> int:
        self.run_id += 1
        self.finished_at = None
        if not self.resumable and self.messages:
            # 上一轮已正常收尾但用户又发起了执行：旧的「不可恢复」结论不再适用
            self.resumable = True
        return self.run_id

    def capture_memory(self, msgs: Any) -> bool:
        """模型调用前采集记忆；失败时保留上一份有效快照并记录原因。"""
        try:
            stats = {"mediaOmitted": 0}
            items: list[dict] = []
            for msg in msgs or []:
                items.append({
                    "name": str(getattr(msg, "name", "") or ""),
                    "role": str(getattr(msg, "role", "") or "user"),
                    "content": _sanitize_content(getattr(msg, "content", ""), stats),
                    "metadata": _safe_json(getattr(msg, "metadata", None) or {}, 2_000),
                    "timestamp": str(getattr(msg, "timestamp", "") or "") or None,
                })
            encoded = json.dumps(items, ensure_ascii=False, default=str)
            dropped = 0
            while len(encoded) > MAX_CHECKPOINT_CHARS and len(items) > 1:
                items.pop(0)
                dropped += 1
                encoded = json.dumps(items, ensure_ascii=False, default=str)
            if len(encoded) > MAX_CHECKPOINT_CHARS:
                raise ValueError(f"单条消息超过检查点上限（{len(encoded)} 字符）")
            self.messages = items
            self.saved_at = time.time()
            self.size_chars = len(encoded)
            self.dropped_messages = dropped
            self.media_omitted = int(stats.get("mediaOmitted") or 0)
            self.model_calls += 1
            self.capture_error = ""
            self.resumable = True
            self.reason = "执行中断后可从此检查点继续"
            self.finished_at = None
            return True
        except Exception as exc:  # noqa: BLE001
            self.capture_error = str(exc)
            return False

    def mark_final(self, resumable: bool, reason: str) -> None:
        """执行收尾：决定是否保留可恢复快照，并留下本次统计。"""
        has_snapshot = bool(self.messages)
        self.resumable = bool(resumable and has_snapshot)
        if resumable and not has_snapshot:
            reason = reason + "（尚未采集到模型调用前快照，无法续跑）"
        self.last_summary = {
            "snapshotMessages": len(self.messages),
            "restoredMessages": int(self.last_restore.get("restoredMessages") or 0),
            "executedTools": self.executed_side_effects,
            "sideEffects": len(self.tool_records),
            "skippedDuplicates": self.skipped_duplicates,
            "modelCalls": self.model_calls,
            "sizeChars": self.size_chars,
        }
        self.reason = reason
        self.finished_at = time.time()
        if not self.resumable:
            # 正常结束 / 已回滚 / 没有快照：不保留大对象，避免长期占用内存
            self.messages = []
            self.saved_at = None
            self.size_chars = 0

    def final_event(self) -> dict:
        return {
            "resumable": self.resumable,
            "reason": self.reason,
            "savedAt": (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.saved_at))
                if self.saved_at else ""
            ),
            "scope": self.scope,
            **self.last_summary,
            "mediaOmitted": self.media_omitted,
            "droppedMessages": self.dropped_messages,
            "captureError": self.capture_error,
            "diskError": self.disk_error,
        }

    # ── 磁盘持久化 ────────────────────────────────────────────

    def disk_payload(self) -> dict:
        """可落盘的部分：快照、台账与计数；调用方再补会话上下文字段。"""
        return {
            "messages": self.messages,
            "toolRecords": self.tool_records,
            "recordsOverflow": self.records_overflow,
            "runId": self.run_id,
            "modelCalls": self.model_calls,
            "sizeChars": self.size_chars,
            "droppedMessages": self.dropped_messages,
            "mediaOmitted": self.media_omitted,
            "captureError": self.capture_error,
            "reason": self.reason,
            "resumable": self.resumable,
            "baselineFiles": self.baseline_files,
            "executedSideEffects": self.executed_side_effects,
            "skippedDuplicates": self.skipped_duplicates,
            "attachmentsMediaOmitted": self.attachments_media_omitted,
            "stats": dict(self.last_summary),
        }

    def restore_from_disk(self, data: dict) -> str:
        """从磁盘检查点恢复进程内状态；返回空串表示成功。"""
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            return "磁盘检查点没有可恢复的对话快照"
        records = data.get("toolRecords")
        self.messages = [dict(m) for m in messages if isinstance(m, dict)]
        self.tool_records = {
            str(k): dict(v) for k, v in (records or {}).items() if isinstance(v, dict)
        } if isinstance(records, dict) else {}
        self.records_overflow = bool(data.get("recordsOverflow"))
        # 跨进程恢复时进程内 run_id 已归零：沿用落盘轮次，否则旧台账会被当成「本轮」
        # 而漏跳过（同签名写盘会重复执行）。旧文件没有 runId 时按台账最大轮次兜底。
        record_runs = [
            int(v.get("run") or 0)
            for v in (self.tool_records.values() if isinstance(self.tool_records, dict) else [])
        ]
        self.run_id = int(data.get("runId") or (max(record_runs) if record_runs else 0))
        self.model_calls = int(data.get("modelCalls") or 0)
        saved_ms = int(data.get("savedAtMs") or 0)
        self.saved_at = saved_ms / 1000 if saved_ms else time.time()
        self.size_chars = int(data.get("sizeChars") or 0)
        self.dropped_messages = int(data.get("droppedMessages") or 0)
        self.media_omitted = int(data.get("mediaOmitted") or 0)
        self.capture_error = str(data.get("captureError") or "")
        self.baseline_files = (
            data.get("baselineFiles") if isinstance(data.get("baselineFiles"), dict) else None
        )
        self.executed_side_effects = int(data.get("executedSideEffects") or 0)
        self.skipped_duplicates = int(data.get("skippedDuplicates") or 0)
        self.attachments_media_omitted = int(data.get("attachmentsMediaOmitted") or 0)
        self.last_summary = dict(data.get("stats") or {})
        self.scope = "disk"
        self.resumable = True
        self.reason = "从磁盘检查点恢复（引擎重启后仍可继续）"
        self.finished_at = None
        return ""

    def unavailable_reason(self) -> str:
        if self.resumable and self.messages:
            return ""
        if not self.messages and not self.model_calls:
            return "该任务尚无可恢复的检查点：中断发生在首次模型调用之前"
        if self.reason:
            return f"该任务当前不可续跑：{self.reason}"
        return "该任务没有可恢复的检查点"

    # ── 副作用台账 ────────────────────────────────────────────

    def record_tool(self, name: str, signature: str, response: Any) -> None:
        if not is_side_effect_tool(name):
            return
        try:
            content = _sanitize_output(getattr(response, "content", None) or [], {"mediaOmitted": 0})
            metadata = _safe_json(getattr(response, "metadata", None), 2_000)
        except Exception:
            return
        if len(self.tool_records) >= MAX_TOOL_RECORDS and signature not in self.tool_records:
            self.records_overflow = True
            return
        self.tool_records[signature] = {
            "tool": name,
            "content": content,
            "metadata": metadata,
            "at": time.time(),
            "run": self.run_id,
        }
        self.executed_side_effects += 1

    def previous_record(self, signature: str) -> dict | None:
        """只返回「此前执行轮次」的记录；同一轮内的重复调用照常执行。"""
        record = self.tool_records.get(signature)
        if record is None:
            return None
        if int(record.get("run") or 0) >= self.run_id:
            return None
        return record

    def skip_previous(self, signature: str) -> None:
        record = self.tool_records.get(signature)
        if record is not None:
            record["skipped"] = int(record.get("skipped") or 0) + 1
            self.skipped_duplicates += 1

    # ── 恢复 ──────────────────────────────────────────────────

    async def restore_into(self, agent, msg_cls) -> dict:
        """把快照恢复进新 Agent 的记忆。

        末尾的用户消息会被摘出来：恢复时用同一目标重新组装本轮消息，
        避免「同一句用户话出现两次」以及图片块丢失后留下的空壳。
        """
        from agentscope.message import Msg  # noqa: F401  （调用方注入的类型保持一致）

        items = [dict(item) for item in self.messages]
        trailing_user = None
        if items and str(items[-1].get("role") or "") == "user":
            trailing_user = items.pop()
        restored = 0
        for item in items:
            try:
                await agent.memory.add(msg_cls(
                    name=str(item.get("name") or "user"),
                    content=item.get("content") or "",
                    role=str(item.get("role") or "user"),
                    metadata=item.get("metadata") or {},
                    timestamp=item.get("timestamp") or None,
                ))
                restored += 1
            except Exception:
                continue
        self.restore_count += 1
        self.last_restore = {
            "restoredMessages": restored,
            "trailingUser": trailing_user is not None,
            "droppedMessages": self.dropped_messages,
            "mediaOmitted": self.media_omitted,
            "sideEffects": len(self.tool_records),
        }
        return dict(self.last_restore)


class CheckpointedModel:
    """模型代理：每次真实模型调用前保存一次记忆快照。

    放在 UsageTrackingModel 外层：用量记账仍由内层完成，快照失败绝不影响模型调用。
    """

    def __init__(self, inner: Any, checkpoint: TaskCheckpoint):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_checkpoint", checkpoint)
        object.__setattr__(self, "agent", None)

    def __getattr__(self, item: str) -> Any:
        return getattr(object.__getattribute__(self, "_inner"), item)

    @property
    def stream(self) -> bool:
        return object.__getattribute__(self, "_inner").stream

    @stream.setter
    def stream(self, value: bool) -> None:
        object.__getattribute__(self, "_inner").stream = value

    async def __call__(self, *args, **kwargs):
        await self.capture()
        return await object.__getattribute__(self, "_inner")(*args, **kwargs)

    async def capture(self) -> None:
        agent = object.__getattribute__(self, "agent")
        checkpoint = object.__getattribute__(self, "_checkpoint")
        if agent is None:
            return
        try:
            # 压缩后 memory 里同时留着「被压缩的原始消息」和「摘要」，
            # 全量快照会让恢复时上下文翻倍；只存摘要与未压缩消息。
            try:
                msgs = await agent.memory.get_memory(exclude_mark="compressed")
            except TypeError:
                msgs = await agent.memory.get_memory()
        except Exception as exc:  # noqa: BLE001
            checkpoint.capture_error = f"读取记忆失败：{exc}"
            return
        checkpoint.capture_memory(msgs)


def tool_middleware(checkpoint: TaskCheckpoint):
    """副作用幂等中间件：必须最先注册（洋葱最内层），门禁/ Hooks 在外层。

    只拦截「中断前已成功执行过」的同签名调用；同一轮内的重复调用不拦。
    """

    async def middleware(kwargs: dict, next_handler):
        from agentscope.message import TextBlock
        from agentscope.tool import ToolResponse

        tool_call = kwargs.get("tool_call") or {}
        name = str(tool_call.get("name") or "")
        if not is_side_effect_tool(name):
            async for chunk in await next_handler(**kwargs):
                yield chunk
            return

        signature = signature_of(name, tool_call.get("input") or {})
        previous = checkpoint.previous_record(signature)
        if previous is not None:
            checkpoint.skip_previous(signature)
            note = (
                f"[断点续跑] 工具「{name}」的这次调用与中断前已成功执行的一次完全相同，"
                "已跳过重复执行，直接返回当时的结果。"
            )
            if checkpoint.emit is not None:
                step_id = f"checkpoint-skip-{checkpoint.skipped_duplicates}"
                try:
                    checkpoint.emit("step_start", {
                        "step_id": step_id,
                        "stepType": "checkpoint",
                        "description": note,
                    })
                    checkpoint.emit("step_done", {"step_id": step_id, "output": note})
                except Exception:
                    pass
            content = [dict(block) for block in (previous.get("content") or []) if isinstance(block, dict)]
            if content and content[0].get("type") == "text":
                content[0] = {
                    "type": "text",
                    "text": note + "\n\n" + str(content[0].get("text") or ""),
                }
            else:
                content.insert(0, {"type": "text", "text": note})
            yield ToolResponse(content=content or [TextBlock(type="text", text=note)])
            return

        last = None
        async for chunk in await next_handler(**kwargs):
            last = chunk
            yield chunk
        if last is not None and not getattr(last, "is_interrupted", False):
            checkpoint.record_tool(name, signature, last)

    return middleware


def install(toolkit, checkpoint: TaskCheckpoint) -> str | None:
    """注册幂等中间件；返回错误说明或 None。

    恢复执行时这个门禁装不上必须中止（否则可能重复写盘/外发），
    新执行装不上只影响将来能否续跑，由调用方决定是否降级。"""
    try:
        toolkit.register_middleware(tool_middleware(checkpoint))
        return None
    except Exception as exc:  # noqa: BLE001
        return str(exc)
