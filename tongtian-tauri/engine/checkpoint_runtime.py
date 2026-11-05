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

import data_paths as dp  # 引擎数据根的唯一解析处（同目录模块，导入方已把引擎目录加入 sys.path）
import side_effect_registry as ser  # #164：副作用名单的唯一落表处，本文件只派生不定义

# 单份快照的体积上限（JSON 字符数）：超出时从最早消息开始丢弃并如实标记
MAX_CHECKPOINT_CHARS = 400_000
# 单条工具结果的保留上限：恢复需要的是「工具做过什么、返回了什么关键信息」
MAX_TOOL_RESULT_CHARS = 4_000
# 工具调用输入（如写文件正文）的保留上限
MAX_TOOL_INPUT_CHARS = 8_000
# 副作用台账条数上限（每条约几 KB，进程内内存可接受）
MAX_TOOL_RECORDS = 1_000

_MEDIA_TYPES = {"image", "audio", "video"}
_READ_ONLY_TOOLS = {"list_directory", "view_text_file", "read_document", "use_skill"}
# 名单本体在 engine/side_effect_registry.py（四张视图同源，含本表与沙箱/审批表的成员差异说明）。
# 派生成本地名字是为了保住判定里的查表写法与 tests/unit/workbench/step-replay.test.ts 的同源校验。
_SIDE_EFFECT_TOOLS = set(ser.view("checkpoint"))
_SIDE_EFFECT_KEYWORDS = ser.keywords()


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
# 位置由 data_paths 统一解析（与 models.json / 意识目录同一数据根）；文件名用 taskId 的
# 哈希，避免外部传入的 id 拼出目录穿越路径。文件里只有恢复所需内容，不含密钥。

CHECKPOINT_VERSION = 1
CHECKPOINT_TTL_SECONDS = 7 * 24 * 3600
MAX_DISK_CHECKPOINT_CHARS = 16_000_000


def checkpoint_home() -> Path:
    """检查点目录：数据根由 `data_paths` 统一解析（D-43 路径单一来源）"""
    return dp.checkpoints_dir()


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


# ── 成员真运行时检查点台账（缺陷 D-40.5 / #149）───────────────────
#
# 团队房间里的一个成员 = 一条工单（`房间~成员`），它跑的是 D-48 那条执行回路：
# 每轮结束由 `ticket_dispatch_runtime` 按**这份领取的 checkpoint_key** 落一份磁盘检查点
# （带 ticketId / assignmentId 自证）。所以「成员有没有真运行时检查点」这件事的答案
# 不在这里另建一套存储，而是：**这份台账只做索引与结论**，正文仍旧躺在
# `checkpoints/<摘要>.json` 里——一份事实只有一个来源。
#
# 台账要回答的是编排层答不出的三个问题：
# 1. 这个成员此刻有没有可续跑的快照（`resumable` 由磁盘那份自证，不由内存猜）；
# 2. 换一份新领取时该接哪一份旧检查点（`carry_forward` 给出来源键与恢复注记）；
# 3. 收口之后证据还在不在（`finished` 会被执行体删档，`dropped/lost` 留着——
#    这里如实转述执行体的回执，不自己删、也不假装删了）。


#: 台账条目的检查点状态。`inflight`=这份领取还开着；`interrupted`=领取结束但没干完，
#: 检查点留在盘上等着被接；`released`=干完收口，执行体已按口径删档。
MEMBER_CHECKPOINT_STATES = ("inflight", "interrupted", "released")

#: 恢复注记的兜底文案：没接调度器（`dispatcher`）时用。内容与
#: `ticket_restart_resume.build_recovery_note` 的检查点段同口径，但不冒充它生成过。
_MEMBER_CARRY_NOTE = (
    "接上你上一次中断的派工（{prev_job_id}）：那份检查点里有 {messages} 条对话快照。"
    "续跑前先查目录、git 历史与远端现状，再重复任何外部动作——"
    "上一轮的收发与写盘可能已经发生，不要当成没做过。"
)
_MEMBER_CARRY_EMPTY = (
    "上一次派工（{prev_job_id}）留下了中断记录，但磁盘上没有可恢复的对话快照："
    "这一份按新任务从头做，别声称接上了上下文。"
)


def checkpoint_summary(task_id: Any) -> dict:
    """一份检查点的**只读摘要**（不返回正文）：有没有、可不可续跑、多少条。

    界面与 `status_of` 都要回答「这个成员的检查点还在不在」，但都不该把对话正文
    端上屏幕，所以自述走这里；正文只有真要续跑的那一方按 `load_checkpoint` 自己取。
    这里读的是 `load_checkpoint`，所以过期/版本不符/坏文件的既有口径一并继承——
    不另写一份「能不能读」的判断。
    """
    tid = str(task_id or "").strip()
    if not tid:
        return {"exists": False, "resumable": False, "messages": 0, "sideEffects": 0,
                "savedAtMs": 0, "scope": "none", "reason": "没有检查点键：这份领取还没落过检查点"}
    data = load_checkpoint(tid)
    if data is None:
        return {"exists": False, "resumable": False, "messages": 0, "sideEffects": 0,
                "savedAtMs": 0, "scope": "none", "reason": (
                    "磁盘上没有这份检查点（从未落过、已过 7 天 TTL、或收口时按「干完了」删掉）")}
    messages = data.get("messages") if isinstance(data.get("messages"), list) else []
    records = data.get("toolRecords") if isinstance(data.get("toolRecords"), dict) else {}
    resumable = bool(data.get("resumable")) and bool(messages)
    reason = str(data.get("reason") or "")
    if not resumable:
        reason = reason or (
            "快照里没有对话（例如只有 worker 退出证据），只能当侧证：续跑请按新任务做"
            if not messages else "这份检查点当时标为不可续跑"
        )
    return {
        "exists": True,
        "resumable": resumable,
        "reason": reason,
        "scope": "disk",
        "messages": len(messages),
        "sideEffects": len(records),
        "modelCalls": int(data.get("modelCalls") or 0),
        "savedAtMs": int(data.get("savedAtMs") or 0),
        "ownerTicketId": str(data.get("ticketId") or ""),
        "ownerAssignmentId": str(data.get("assignmentId") or ""),
    }


class MemberCheckpointLedger:
    """按（房间, 成员）索引成员运行时检查点；正文仍归磁盘检查点文件。

    `jobs` 必须就是团队那一层的派工台账（同一份 `team_jobs.json`）——再存一份
    「哪个成员有检查点」就会出现第二个事实源，两份不一致时没人说得出哪条是真的。
    `dispatcher` 只用于借它已有的恢复注记生成器（与重启补查同一口径），没接就用兜底文案。
    `key_fn` 必须是 `ticket_worker_runtime.checkpoint_task_id` 本身（由装配现场注入）：
    本模块不许 import 它——`ticket_worker_runtime` 已经 import 本模块，反过来再引一次
    就是循环导入；而键的算式更不能在这里抄第二遍，抄错一次的结果是索引指向一个
    执行体从来没写过的文件，续跑永远读空。
    """

    def __init__(
        self, jobs: Any, *, dispatcher: Any = None, key_fn: Callable[[str], str] | None = None
    ) -> None:
        self.jobs = jobs
        self.dispatcher = dispatcher
        self.key_fn = key_fn

    def bind_dispatcher(self, dispatcher: Any) -> None:
        """装配现场晚一步接上调度器（注册表先于调度器存在是 D-48 装配的形状）。"""
        self.dispatcher = dispatcher

    def bind_key_fn(self, key_fn: Callable[[str], str] | None) -> None:
        self.key_fn = key_fn

    # ── 写入 ────────────────────────────────────────────────

    def record_assignment(
        self,
        *,
        room: Any = "",
        member_id: Any = "",
        assignment_id: Any,
        ticket_id: Any = "",
        delivered_assignment_id: Any = "",
        checkpoint_key: Any = "",
    ) -> dict:
        """把这份领取标成「有检查点索引」。

        `delivered_assignment_id` 是执行体**实际**绑上的那份：同一成员的第二条派工会
        并进他当前那份领取（`existing-worker`），此时传进来的 job id 只是台账号，
        真正会落检查点的是实际那一份。索引记错这条，续跑时就会去读一个永远为空的键。

        索引字段走 `JobStore.set_fields`（只补记、不动状态）：这是台账唯一的第二个
        写入口，与 `transition` 共用同一把锁与同一次原子写。不开 `transition` 的同态
        改写是因为 `ended` 那道拒绝防的是「同一件事收两次口」，而检查点状态恰恰要在收口
        之后才问得到执行体——把那条拒绝改弱，代价是台账从此分不清重复收口与补记。
        """
        jid = str(assignment_id or "").strip()
        job = self._job(jid)
        if job is None:
            return {"ok": False, "reason": f"查不到派工「{jid}」：没有台账条目的领取无法索引检查点"}
        if self.key_fn is None:
            return {"ok": False, "reason": (
                "装配没注入检查点键算式（`checkpoint_task_id`）：这里不许自己抄一份——"
                "抄出来的键与执行体落盘用的键只要差一次摘要，续跑就会去读一个空文件，"
                "而读空会被当成「这个成员没有检查点」，把真实存在的证据说成没有"
            )}
        actual = str(delivered_assignment_id or "").strip() or jid
        try:
            key = str(checkpoint_key or "").strip() or str(self.key_fn(actual) or "").strip()
        except Exception as exc:  # noqa: BLE001  算不出键就等于没有可索引的坐标
            return {"ok": False, "reason": f"检查点键算不出来：{exc}"}
        if not key:
            return {"ok": False, "reason": f"领取「{actual}」算出的检查点键是空的，无法索引"}
        fields = {
            "checkpointKey": key,
            "checkpointAssignmentId": actual,
            "checkpointTicketId": str(ticket_id or job.get("ticketId") or ""),
            "checkpointState": "inflight",
            "checkpointTrackedAtMs": int(time.time() * 1000),
        }
        # 同一成员被并进别人那份领取时，这条 job 不会单独落检查点：标出来，
        # 否则「这条派工有可续跑检查点」会是假的（它连自己的键都没有）。
        if actual != jid:
            fields["checkpointSharedWith"] = actual
        return self.jobs.set_fields(jid, **fields)

    def release(
        self,
        *,
        room: Any = "",
        member_id: Any = "",
        assignment_id: Any,
        status: Any,
        end_result: dict | None = None,
    ) -> dict:
        """领取收口：按执行体的回执更新索引（`finished` 档已被删、`dropped/lost` 留证）。"""
        jid = str(assignment_id or "").strip()
        job = self._job(jid)
        if job is None:
            return {"ok": False, "reason": f"查不到派工「{jid}」：没有台账条目的收口不记检查点状态"}
        end = end_result if isinstance(end_result, dict) else {}
        st = str(status or "").strip()
        state = "released" if st == "finished" else "interrupted"
        fields = {
            "checkpointState": state,
            "checkpointReleasedAtMs": int(time.time() * 1000),
            "checkpointReleaseStatus": st,
        }
        if end:
            fields["checkpointDeletedByWorker"] = bool(end.get("checkpointDeleted"))
            fields["checkpointKeyAtRelease"] = str(end.get("checkpointKey") or "")
            if not end.get("ok"):
                fields["checkpointReleaseNote"] = (
                    f"执行体那侧没有收成口：{end.get('reason')}（索引仍按 {state} 记，"
                    "但那份领取可能还占着槽位）"
                )
        return self.jobs.set_fields(jid, **fields)

    # ── 读取与续跑 ──────────────────────────────────────────

    def _job(self, job_id: str) -> dict | None:
        getter = getattr(self.jobs, "get_job", None)
        if callable(getter):
            found = getter(job_id)
            if isinstance(found, dict):
                return found
        for job in self.jobs.all_jobs():
            if str(job.get("jobId") or "") == job_id:
                return job
        return None

    def tracked_jobs(self, room: Any = "", member_id: Any = "") -> list:
        """带检查点索引的派工条目（按房间/成员过滤，都留空就是全量）。"""
        rid, mid = str(room or "").strip(), str(member_id or "").strip().lower()
        out = []
        for job in self.jobs.all_jobs():
            if rid and str(job.get("room") or "") != rid:
                continue
            if mid and str(job.get("memberId") or "") != mid:
                continue
            if job.get("checkpointKey"):
                out.append(job)
        return out

    def last_interrupted(self, room: Any, member_id: Any) -> dict | None:
        """最近一份**可以接**的旧领取（中断留证的）；没有就返回 None。

        带 `checkpointSharedWith` 的那些先排掉：同一成员被并进别人那份领取时，这条 job
        自己没有独立的检查点文件（落盘的是实际那份领取），把它当候选会把「接上了上下文」
        记在一个从来没有独立检查点的 job 上。
        排序取「中断时刻 + 创建时刻」里最新的一条；重启降级的那一批用
        `checkpointInterruptedAtMs`（没有收口时刻），两者取其一即可比出先后。
        """
        candidates = [
            j for j in self.tracked_jobs(room, member_id)
            if str(j.get("checkpointState") or "") == "interrupted"
            and not str(j.get("checkpointSharedWith") or "")
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda j: (
                int(j.get("checkpointReleasedAtMs") or j.get("checkpointInterruptedAtMs") or 0),
                int(j.get("createdAtMs") or 0),
            ),
        )

    def carry_forward(
        self,
        *,
        room: Any,
        member_id: Any,
        next_assignment_id: Any,
        title: str = "",
    ) -> dict:
        """派工前问一句「这个成员有没有该接的上一份」；**只读**，不改台账。

        改状态归 `record_assignment`（投递成功之后）与 `release`（收口之后）：
        这里如果顺手把旧条目改成「已被接走」，投递一旦被拒（槽位满）就会留下一条
        「检查点已被消费」的假记录——那份证据下次再没人会接。
        """
        rid, mid = str(room or "").strip(), str(member_id or "").strip().lower()
        nxt = str(next_assignment_id or "").strip()
        out: dict = {"ok": True, "carriedFrom": "", "resumable": False, "recoveryNote": ""}
        if not rid or not mid:
            out["reason"] = "房间与成员 id 都必填：缺一个就不知道要接谁的检查点"
            return out
        prev = self.last_interrupted(rid, mid)
        if prev is None:
            out["reason"] = "这个成员没有留下中断的检查点：按新任务起一份"
            return out
        prev_job_id = str(prev.get("jobId") or "")
        if prev_job_id == nxt:
            out["reason"] = "该接的那一份就是这一次领取本身，不接自己"
            return out
        source_key = str(prev.get("checkpointKey") or "")
        ticket_id = str(prev.get("checkpointTicketId") or prev.get("ticketId") or "")
        summary = checkpoint_summary(source_key)
        if summary.get("ownerTicketId") and ticket_id and summary["ownerTicketId"] != ticket_id:
            # D-48.5g 那条 wrong-owner 拒绝的同一条防线，这里提前一步：
            # 检查点里自证的工单与要接的成员工单不一致，就当没有这份可续跑证据。
            out["reason"] = (
                f"候选检查点自证属于工单「{summary['ownerTicketId']}」，与成员工单「{ticket_id}」不符："
                "按新任务起一份，不把别人的对话接进这个成员"
            )
            out["rejectedSource"] = prev_job_id
            return out
        note = ""
        note_source = ""
        if self.dispatcher is not None:
            try:
                note = str(self.dispatcher.recovery_note_for(
                    ticket_id, prev_assignment_id=prev_job_id, title=title
                ) or "")
                note_source = "dispatcher"
            except Exception as exc:  # noqa: BLE001  注记生成失败不能挡住派工
                note = ""
                note_source = f"调度器生成失败，改用兜底文案：{exc}"
        if not note:
            template = _MEMBER_CARRY_NOTE if summary["resumable"] else _MEMBER_CARRY_EMPTY
            note = template.format(
                prev_job_id=prev_job_id, messages=int(summary.get("messages") or 0)
            )
            note_source = note_source or "fallback"
        out.update({
            "carriedFrom": prev_job_id,
            "sourceCheckpointKey": source_key,
            "resumable": bool(summary["resumable"]),
            "snapshot": summary,
            "recoveryNote": note,
            "noteSource": note_source,
            "ticketId": ticket_id,
        })
        return out

    def status_of(self, room: Any, member_id: Any) -> dict:
        """这个成员的检查点现状（给房间视图与界面）；只报状态与计数，不报正文。"""
        rid, mid = str(room or "").strip(), str(member_id or "").strip().lower()
        tracked = self.tracked_jobs(rid, mid)
        live = [j for j in tracked if str(j.get("checkpointState") or "") == "inflight"]
        interrupted = [j for j in tracked if str(j.get("checkpointState") or "") == "interrupted"]
        released = [j for j in tracked if str(j.get("checkpointState") or "") == "released"]
        newest = (
            max(
                tracked,
                key=lambda j: (int(j.get("updatedAtMs") or 0), int(j.get("createdAtMs") or 0)),
            )
            if tracked else None
        )
        view: dict = {
            "room": rid,
            "memberId": mid,
            "tracked": len(tracked),
            "inflight": [str(j.get("jobId") or "") for j in live],
            "interrupted": [str(j.get("jobId") or "") for j in interrupted],
            "released": len(released),
            "hasResumable": False,
        }
        if newest is None:
            view["reason"] = "这个成员还没有带检查点索引的派工"
            return view
        key = str(newest.get("checkpointKey") or "")
        shared = str(newest.get("checkpointSharedWith") or "")
        summary = checkpoint_summary(key)
        view.update({
            "lastJobId": str(newest.get("jobId") or ""),
            "state": str(newest.get("checkpointState") or ""),
            "checkpointKey": key,
            "scope": str(summary.get("scope") or "none"),
            "exists": bool(summary.get("exists")),
            "resumable": bool(summary.get("resumable")),
            "snapshotMessages": int(summary.get("messages") or 0),
            "sideEffects": int(summary.get("sideEffects") or 0),
            "savedAtMs": int(summary.get("savedAtMs") or 0),
            "reason": str(summary.get("reason") or ""),
            "releaseStatus": str(newest.get("checkpointReleaseStatus") or ""),
        })
        if shared:
            view["note"] = (
                f"这条派工并进了领取「{shared}」（同一成员串行）：它自己没有独立的检查点，"
                "要看那份领取的状态"
            )
        # 「有没有可续跑的」跨条目回答：最新一条可能刚收口（released、档已删），
        # 但更早那条可能留着中断证据——那才是下一次派工真能接上的东西。
        for job in interrupted:
            if checkpoint_summary(str(job.get("checkpointKey") or ""))["resumable"]:
                view["hasResumable"] = True
                view["resumableFrom"] = str(job.get("jobId") or "")
                break
        return view

    def room_view(self, room: Any) -> dict:
        """整个房间的成员检查点概览（`/api/checkpoint/members` 的直接返回体）。"""
        rid = str(room or "").strip()
        members: dict = {}
        for job in self.tracked_jobs(rid):
            mid = str(job.get("memberId") or "")
            if mid:
                members.setdefault(mid, str(job.get("memberName") or mid))
        items = []
        for mid, name in members.items():
            status = self.status_of(rid, mid)
            status["memberName"] = name
            items.append(status)
        items.sort(key=lambda s: str(s.get("memberId") or ""))
        return {
            "ok": True,
            "room": rid,
            "members": items,
            "resumableCount": sum(1 for s in items if s.get("hasResumable")),
            "interruptedCount": sum(1 for s in items if str(s.get("state") or "") == "interrupted"),
            "note": "正文不落在这里：本视图只自述「有没有、可不可续跑、多少条」，"
                    "对话快照仍旧在 checkpoints 目录那一份文件里",
        }

    def snapshot(self) -> dict:
        """装配自述：索引条数与「调度器接没接」——注记口径的差别要看得见。"""
        tracked = self.tracked_jobs()
        return {
            "tracked": len(tracked),
            "byState": {
                state: sum(1 for j in tracked if str(j.get("checkpointState") or "") == state)
                for state in MEMBER_CHECKPOINT_STATES
            },
            "dispatcher": self.dispatcher is not None,
        }
