"""
通天办公 - AgentScope 引擎服务

基于 agentscope ReActAgent 的自研引擎，实现与 Rust AgentBridge 的 HTTP/SSE 契约：
- GET  /health            → 健康检查 + 版本信息
- POST /api/chat          → 执行任务，流式输出 SSE 事件（plan/step_*/deliver/done/error）
                             请求体除 message/task_id 外还携带 Rust 装配的执行面输入：
                             attachments（已抽取正文的附件）、runtime（意识 + 启用技能 + 已连接连接器 + Hooks）
- GET  /api/events/replay → 事件重放（三层架构演进 P0：seq + 重放窗口 ≥10min）
- POST /api/chat/cancel   → 取消任务（cancelled 事件 + done）
- GET  /api/audit         → 请求审计（服务化 JWT 模式下按调用主体记录与过滤）
- GET  /api/sandbox/environment/usage → 工作环境文件磁盘用量
- POST /api/sandbox/environment/clear → 清理工作环境文件（保留对话记录与工作产物）

三层架构演进 P0（需求分析报告）：
- 每任务单调递增 seq，SSE 输出 `id: {task_id}:{seq}` 行，seq 同时写入 data
- 内存重放窗口（≥10min，条数+时间双约束），支持 Last-Event-ID 断线续传
- keepalive 心跳注释行（15s）防空闲断连
- 本引擎为过渡产物，仅做最小增量；权威事件持久化在 Rust event_store

引擎模式说明（TT_ENGINE 环境变量由 Rust 桥接层控制）：
- agentscope: 本服务（默认）
- 模型配置: 读取项目根 .env 文件（TT_PROVIDER / TT_MODEL / TT_API_KEY / TT_BASE_URL）
"""

import argparse
import asyncio
import base64
import contextvars
import hashlib
import hmac
import inspect
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import json
import uvicorn

# IM 通道实现与框架是同目录模块：无论从哪个 cwd 拉起引擎（Rust 子进程、集成测试、
# 直接 python engine/server.py），都要保证引擎目录可 import
sys.path.insert(0, str(Path(__file__).resolve().parent))
import im_channels  # noqa: E402  （IM 多通道同构框架）
import slack_channel  # noqa: E402,F401  （导入即注册 Slack Socket Mode 通道）
import dingtalk_channel  # noqa: E402,F401  （导入即注册钉钉 Stream 通道，协议取自官方 SDK）
import wecom_channel  # noqa: E402,F401  （导入即注册企业微信智能机器人通道，协议取自官方 aibot SDK）
import feishu_channel  # noqa: E402,F401  （导入即注册飞书通道：模块只探测 lark_oapi 在不在，真正加载推迟到绑定/启动）
import runtime_context as rc  # noqa: E402  （执行面 P0：意识/技能/附件 → 模型上下文）
import mcp_runtime as mr  # noqa: E402  （执行面 P0：连接器工具 → Toolkit）
import context_budget as cb  # noqa: E402  （P1 上下文效率：迭代上限 + 压缩阈值）
import tool_search as tsr  # noqa: E402  （P1 工具效率：MCP 工具按需搜索载入）
import hooks_runtime as hr  # noqa: E402  （执行面 P0：Hooks 事件 → 本机网关执行）
import sandbox_runtime as sr  # noqa: E402  （执行面 P1：沙箱策略 → 工具/文件真实约束）
import subagent_runtime as sar  # noqa: E402  （多 Agent 第一层：本地子 Agent 派发与结果汇总）
import checkpoint_runtime as ckpt  # noqa: E402  （任务检查点：模型调用前快照 + 副作用幂等台账）
import tool_grants as tg  # noqa: E402  （执行面 P1：受控命令执行/联网获取，默认不注册）
import web_search as ws  # noqa: E402  （联网搜索：受同一份联网授权约束，默认不注册）
import computer_use as cu  # noqa: E402  （执行面 P1/P2：Windows 电脑操控，仅桌面壳可执行）
import browser_use as bu  # noqa: E402  （执行面 P2：受控浏览器会话，CDP 操作由桌面壳放行）
import knowledge_runtime as kn  # noqa: E402  （本地知识库 RAG：索引 + 检索工具，无可用库时不注册）
import privacy_guard as pg  # noqa: E402  （出站内容脱敏：模型调用前处理文本，off/mask/block）
import tool_approval as tap  # noqa: E402  （逐工具审批：副作用工具执行前经本机网关确认）
import expert_runtime as er  # noqa: E402  （专家团队：负责人提示词、每专家模型与子 Agent 路由）
import terminal_runtime as trm  # noqa: E402  （终端 AI+：真实 shell 会话 + 命令建议）
import acp_client as acpm  # noqa: E402  （ACP 客户端：把编码子任务委派给外部 Agent，默认不注册）

# ── .env 加载（项目根目录，无第三方依赖） ──────────────────────

def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    # 编码容错：优先 UTF-8，Windows 默认 ANSI(GBK) 保存的 .env 降级读取
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="gbk", errors="ignore")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


ENGINE_VERSION = "0.3.0"
WORK_DIR = Path(".")

# ── 三层架构演进 P0：任务会话与事件重放窗口 ────────────────────
# 参数对齐需求分析报告附录 A：重放窗口 ≥10min；桌面侧保守取值
REPLAY_WINDOW_SECONDS = 10 * 60   # 重放窗口时间上限（≥10min）
REPLAY_WINDOW_MAX_EVENTS = 2048   # 重放窗口条数上限（drop-oldest）
KEEPALIVE_INTERVAL_SECONDS = 15   # SSE 心跳注释行间隔


class TaskSession:
    """单任务会话：单调 seq 计数器 + 内存重放窗口 + 取消句柄 + 两阶段协议状态"""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.seq = 0
        self.created_at = time.time()
        # 窗口条目：(seq, ts, event, data)
        self.window: deque = deque(maxlen=REPLAY_WINDOW_MAX_EVENTS)
        self.agent_task: asyncio.Task | None = None
        self.cancelled = False
        # ── 两阶段协议（plan → review → execute） ──
        self.goal: str = ""
        self.plan: list | None = None
        # 执行期间的活动队列（事件按 seq 写入窗口）
        self.exec_queue: "RecordingQueue | None" = None
        # 会话子目录（工作目录下按对话时间创建，存会话内容与产出物）
        self.session_dir: Path | None = None
        # 对话内模型档位绑定（approve 重新执行时沿用）
        self.model_binding: str | None = None
        # 本任务工作目录（前端选择的工作目录，approve 重新执行时沿用）
        self.work_dir: Path | None = None
        # 安全工作环境开关（true 且未自选工作目录时，任务落在 environment/{taskId} 隔离目录）
        self.secure_env: bool = False
        # 隐私模式（Rust 随提交下发）：跳过会话目录 / conversation.md / 自动记忆，
        # 本轮内容只存在于进程与实时事件流，不落到磁盘
        self.privacy_mode: bool = False
        # 出站脱敏档位（off/mask/block）：runtime.privacy 优先，浏览器直连随请求下发
        self.pii_mode: str = pg.DEFAULT_MODE
        # 本任务自定义模型配置（models.json 条目解析后的调用配置，approve 重新执行时沿用）
        self.model_config: dict | None = None
        # 本轮在数据源中选中的模型 id（models.json 条目 id；未选/档位绑定时为空）
        self.model_id: str | None = None
        # 本轮负责人专家（runtime.experts 中的 id；委派子任务仍可另行指定专家）
        self.expert_id: str | None = None
        # 此前轮次对话历史（两阶段协议下 plan 阶段带上来，approve 执行时沿用）
        self.history: list = []
        # 运行时上下文（意识 / 技能）：由 Rust 随提交下发，approve 重跑沿用
        self.runtime: dict = {}
        # 工作台模式（general/design/slides/writing）：随提交下发，approve 重跑沿用
        self.mode: str = "general"
        # 附件（Rust 侧已抽取正文）：执行时拼进用户消息，approve 重跑沿用
        self.attachments: list = []
        # 任务检查点（进程内）：记忆快照 + 副作用台账；新任务在 /api/chat 重置
        self.checkpoint = ckpt.TaskCheckpoint(task_id)

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    def record(self, seq: int, event: str, data: dict) -> None:
        self.window.append((seq, time.time(), event, data))

    def replay_after(self, last_seq: int):
        """返回 seq > last_seq 且在窗口时间内的条目（惰性清理过期项）"""
        now = time.time()
        while self.window and now - self.window[0][1] > REPLAY_WINDOW_SECONDS:
            self.window.popleft()
        return [(s, e, d) for (s, _ts, e, d) in self.window if s > last_seq]


# 任务会话注册表（进程内；引擎重启后由检查点磁盘文件恢复可续跑任务）
_TASK_SESSIONS: dict[str, TaskSession] = {}


def _get_or_create_session(task_id: str) -> TaskSession:
    session = _TASK_SESSIONS.get(task_id)
    if session is None:
        session = TaskSession(task_id)
        _TASK_SESSIONS[task_id] = session
    return session


# ── 检查点磁盘持久化 ──────────────────────────────────────────
#
# 只在「可续跑」收尾时落盘；正常完成/回滚/隐私模式删除。落盘内容不含模型密钥、
# Hook 网关令牌与 MCP 环境变量：恢复时由 Rust 侧重新装配 runtime 与模型配置，
# 浏览器直连则退回 models.json 与已保存的非机密上下文。

# 附件图片 base64 可能很大：先带图试写，超过阈值再剥掉图片数据并如实标记
_DISK_ATTACHMENT_IMAGE_BUDGET = ckpt.MAX_DISK_CHECKPOINT_CHARS - 1_000_000


_GATE_KEYS = ("gateUrl", "gateToken")
_EXPERT_SECRET_STRIPPED_NOTE = "专家绑定的模型密钥不写入磁盘检查点；恢复时由本机重新装配"


def _runtime_for_disk(runtime: Any) -> dict:
    if not isinstance(runtime, dict):
        return {}
    out = {
        k: v
        for k, v in runtime.items()
        if k not in ("mcp", "hooks", "computerUse", "browserUse", "toolApproval", "experts")
    }
    hooks = runtime.get("hooks")
    if isinstance(hooks, dict):
        # gateUrl/token 是本次引擎进程与 Rust 网关之间的凭据，不写磁盘
        out["hooks"] = {
            k: v for k, v in hooks.items() if k not in ("gateUrl", "token")
        }
    # 电脑操控 / 浏览器 / 逐工具审批：同样只保留档位等非机密字段，网关令牌不落盘
    for name, keys in (
        ("computerUse", _GATE_KEYS),
        ("browserUse", _GATE_KEYS),
        ("toolApproval", _GATE_KEYS),
    ):
        block = runtime.get(name)
        if isinstance(block, dict):
            out[name] = {k: v for k, v in block.items() if k not in keys}
    # 专家注册表：保留 id/名称/提示词/模型名，剥掉绑定的模型密钥
    experts = runtime.get("experts")
    if isinstance(experts, dict):
        items: list = []
        stripped = 0
        for item in experts.get("items") or []:
            if not isinstance(item, dict):
                continue
            clean = dict(item)
            cfg = clean.get("modelConfig")
            if isinstance(cfg, dict) and cfg.get("apiKey"):
                cfg = dict(cfg)
                cfg.pop("apiKey", None)
                clean["modelConfig"] = cfg
                # 标记「密钥只存在于提交请求中」：浏览器直连恢复时无法重建，必须显式报错
                clean["modelSecretsStripped"] = True
                stripped += 1
            items.append(clean)
        clean_experts = {k: v for k, v in experts.items() if k != "items"}
        clean_experts["items"] = items
        if stripped:
            clean_experts["secretsStripped"] = stripped
        out["experts"] = clean_experts
    if runtime.get("mcp"):
        out["mcpNote"] = "连接器凭据不写入磁盘检查点；恢复时由本机重新装配"
    return out


def _strip_attachment_images(attachments: list) -> tuple[list, int]:
    out: list = []
    omitted = 0
    for item in attachments or []:
        att = dict(item) if isinstance(item, dict) else item
        if isinstance(att, dict) and isinstance(att.get("image"), dict):
            img = dict(att["image"])
            if img.get("data"):
                img.pop("data", None)
                omitted += 1
            att["image"] = img
        out.append(att)
    return out, omitted


def _checkpoint_context(session: TaskSession) -> dict:
    return {
        "goal": session.goal,
        "workDir": str(session.work_dir) if session.work_dir else "",
        "secureEnv": bool(session.secure_env),
        "mode": session.mode,
        "modelBinding": session.model_binding or "",
        "modelId": session.model_id or "",
        "expertId": session.expert_id or "",
        "history": session.history,
        "runtime": _runtime_for_disk(session.runtime),
        "attachments": session.attachments,
        "sessionDir": str(session.session_dir) if session.session_dir else "",
        "attachmentsMediaOmitted": 0,
    }


def _persist_checkpoint(session: TaskSession) -> tuple[bool, str]:
    """可续跑时写盘；不可续跑/隐私模式删除旧文件。任何失败都不影响任务收尾。"""
    checkpoint = session.checkpoint
    if session.privacy_mode:
        ckpt.delete_checkpoint(session.task_id)
        return False, "隐私模式不写磁盘检查点"
    if not checkpoint.resumable or not checkpoint.messages:
        ckpt.delete_checkpoint(session.task_id)
        checkpoint.scope = "same-process"
        checkpoint.disk_error = ""
        return False, ""
    context = _checkpoint_context(session)
    payload = checkpoint.disk_payload()
    payload["context"] = context
    encoded_len = len(json.dumps(
        {"version": ckpt.CHECKPOINT_VERSION, "taskId": session.task_id, **payload},
        ensure_ascii=False,
        default=str,
    ))
    if encoded_len > _DISK_ATTACHMENT_IMAGE_BUDGET and context.get("attachments"):
        context["attachments"], omitted = _strip_attachment_images(context["attachments"])
        context["attachmentsMediaOmitted"] = omitted
        checkpoint.attachments_media_omitted = omitted
    ok, err = ckpt.save_checkpoint(session.task_id, payload)
    if ok:
        checkpoint.scope = "disk"
        checkpoint.disk_error = ""
        return True, ""
    checkpoint.scope = "same-process"
    checkpoint.disk_error = err
    if err and "超过磁盘上限" not in checkpoint.reason:
        checkpoint.reason = f"{checkpoint.reason}（写入磁盘失败：{err}，仅当前引擎进程内有效）"
    return False, err


def _checkpoint_public(session: TaskSession) -> dict:
    running = session.agent_task is not None and not session.agent_task.done()
    info = {
        "type": "checkpoint",
        "taskId": session.task_id,
        **session.checkpoint.final_event(),
    }
    if running:
        # 执行中不能续跑：查询接口如实返回，避免刷新后出现一点就 409 的入口
        info["resumable"] = False
        info["running"] = True
    return info


def _apply_resume_overrides(session: TaskSession, payload: dict) -> None:
    """Rust 在 resume 时重新下发模型配置与 runtime（磁盘里不存密钥）。
    浏览器直连没有这两项：模型配置已在 _restore_session_from_disk 里按 modelId 解析。"""
    model_config_raw = payload.get("modelConfig")
    if isinstance(model_config_raw, dict) and model_config_raw:
        model_config = dict(model_config_raw)
        if not str(model_config.get("baseUrl") or "").strip():
            default_base = MODEL_DEFAULT_BASE_URLS.get(str(model_config.get("source") or ""), "")
            if default_base:
                model_config["baseUrl"] = default_base
        session.model_config = model_config
    runtime_raw = payload.get("runtime")
    if isinstance(runtime_raw, dict) and runtime_raw:
        session.runtime = runtime_raw
    if payload.get("expertId") is not None:
        session.expert_id = str(payload.get("expertId") or "").strip() or None


def _restore_session_from_disk(
    task_id: str, payload: dict
) -> tuple[TaskSession | None, dict | None]:
    """引擎重启后按磁盘检查点重建任务会话；返回 (session, error)。"""
    data = ckpt.load_checkpoint(task_id)
    if data is None:
        return None, {
            "status": 404,
            "code": "TASK_NOT_FOUND",
            "message": (
                "任务会话不在当前引擎进程中，也没有找到可用的磁盘检查点"
                "（可能已正常完成、被清理或超过 7 天保留期）"
            ),
        }
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    goal = str(context.get("goal") or "").strip()
    if not goal:
        return None, {
            "status": 409,
            "code": "CHECKPOINT_CORRUPT",
            "message": "磁盘检查点缺少任务目标，无法安全恢复",
        }

    work_dir_raw = str(context.get("workDir") or "").strip()
    work_dir = None
    if work_dir_raw:
        work_dir = Path(work_dir_raw).expanduser()
        if not work_dir.is_dir():
            return None, {
                "status": 409,
                "code": "CHECKPOINT_WORKDIR_MISSING",
                "message": f"检查点的工作目录已不存在：{work_dir_raw}（恢复会写错位置，已中止）",
            }

    session = TaskSession(task_id)
    session.goal = goal
    session.work_dir = work_dir
    session.secure_env = bool(context.get("secureEnv"))
    mode = str(context.get("mode") or "general")
    session.mode = mode if mode in rc.VALID_MODES else "general"
    session.model_binding = str(context.get("modelBinding") or "").strip() or None
    session.model_id = str(context.get("modelId") or "").strip() or None
    session.expert_id = str(context.get("expertId") or "").strip() or None
    session.history = _normalize_history(context.get("history"))
    session.attachments = rc.normalize_attachments(context.get("attachments"))
    runtime_raw = context.get("runtime")
    session.runtime = runtime_raw if isinstance(runtime_raw, dict) else {}
    session_dir_raw = str(context.get("sessionDir") or "").strip()
    if session_dir_raw:
        session_dir = Path(session_dir_raw)
        session.session_dir = session_dir if session_dir.is_dir() else None

    # 模型配置：Rust 会在 resume 里重新下发；浏览器直连按 modelId 在 models.json 找回
    model_config_raw = payload.get("modelConfig")
    if isinstance(model_config_raw, dict) and model_config_raw:
        session.model_config = dict(model_config_raw)
    elif session.model_id:
        selected, model_err = _models_find(session.model_id)
        if not model_err and selected:
            session.model_config, model_err = _model_runtime_config(selected)
        if model_err:
            return None, {
                "status": 409,
                "code": "CHECKPOINT_MODEL_UNAVAILABLE",
                "message": (
                    f"检查点可恢复，但模型配置无法重新解析（{model_err}）；"
                    "请确认该模型仍然可用后重试"
                ),
            }

    err = session.checkpoint.restore_from_disk(data)
    if err:
        return None, {"status": 409, "code": "CHECKPOINT_CORRUPT", "message": err}
    _TASK_SESSIONS[task_id] = session
    return session, None


# ── 模型用量台账（真实 token 采集） ────────────────────────────
#
# 权威持久化在 Rust 侧 model_usage 表（按 usage 事件记账，见 bridge/agent.rs）；
# 本台账服务于浏览器直连模式（无 Rust）与计划阶段用量展示，进程重启即清零。

# 当前请求的调用主体（JWT 模式为账号 uid；桌面/无令牌模式为 None）。
# 模型调用链较深，用 contextvar 传递，避免把 principal 穿过每一层函数。
_CURRENT_PRINCIPAL: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "tt_engine_principal", default=None
)


def current_principal() -> dict | None:
    return _CURRENT_PRINCIPAL.get()


def _utc_day(ms: int | None = None) -> str:
    """UTC 日期 yyyy-mm-dd（与 Rust 台账同口径：跨日按 UTC 分桶）"""
    ts = time.time() if ms is None else ms / 1000
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _utc_month(ms: int | None = None) -> str:
    return _utc_day(ms)[:7]


class UsageLedger:
    """进程内用量台账：按「模型 × 日」累计 token、调用次数、延迟与成本

    延迟取每次真实模型调用的端到端耗时（流式为整段生成耗时）；成本按调用方
    配置的每 1M tokens 单价换算成 micros。没有单价时只记调用次数，不按 0 元
    假装免费（`pricedCalls`/`unpricedCalls` 分开暴露）。
    """

    def __init__(self) -> None:
        # key = (model_name, day) → row
        self.rows: dict[tuple[str, str], dict] = {}

    def add(self, model_name: str, prompt_tokens: int, completion_tokens: int,
            latency_ms: int | None = None, cost_micros: int | None = None,
            currency: str | None = None, priced: bool = False,
            sub: str | None = None, org: str | None = None) -> dict:
        name = (model_name or "").strip() or "unknown"
        subject = (sub or "").strip() or "local"
        day = _utc_day()
        row = self.rows.get((subject, name, day))
        if row is None:
            row = {
                "sub": subject,
                "org": (org or "").strip(),
                "modelName": name,
                "day": day,
                "promptTokens": 0,
                "completionTokens": 0,
                "calls": 0,
                "lastUsedAtMs": 0,
                "latencyMsTotal": 0,
                "latencySamples": 0,
                "costs": {},
                "pricedCalls": 0,
                "unpricedCalls": 0,
            }
            self.rows[(subject, name, day)] = row
        row["promptTokens"] += max(0, int(prompt_tokens))
        row["completionTokens"] += max(0, int(completion_tokens))
        row["calls"] += 1
        row["lastUsedAtMs"] = int(time.time() * 1000)
        if latency_ms is not None:
            row["latencyMsTotal"] += max(0, int(latency_ms))
            row["latencySamples"] += 1
        if priced and cost_micros is not None:
            code = (str(currency or "CNY").strip().upper() or "CNY")
            row["costs"][code] = row["costs"].get(code, 0) + max(0, int(cost_micros))
            row["pricedCalls"] += 1
        else:
            row["unpricedCalls"] += 1
        return row

    def aggregate(self, period: str, subject: str | None = None) -> list[dict]:
        """按周期聚合为「模型一行」（daily=今日 / monthly=本月 / total=全部）

        subject 为 None 时汇总全部用户；传入具体 uid 时只看该用户。
        """
        if period == "daily":
            match = lambda day: day == _utc_day()  # noqa: E731
        elif period == "monthly":
            month = _utc_month()
            match = lambda day: day.startswith(month)  # noqa: E731
        else:  # total（含非法值降级：引擎侧不阻断只读查询）
            match = lambda day: True  # noqa: E731

        agg: dict[str, dict] = {}
        for (row_subject, name, day), row in self.rows.items():
            if subject is not None and row_subject != subject:
                continue
            if not match(day):
                continue
            cur = agg.get(name)
            if cur is None:
                cur = {
                    "modelName": name,
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "tokensUsed": 0,
                    "calls": 0,
                    "lastUsedAtMs": 0,
                    "latencyMsTotal": 0,
                    "latencySamples": 0,
                    "costs": {},
                    "pricedCalls": 0,
                    "unpricedCalls": 0,
                }
                agg[name] = cur
            cur["promptTokens"] += row["promptTokens"]
            cur["completionTokens"] += row["completionTokens"]
            cur["tokensUsed"] += row["promptTokens"] + row["completionTokens"]
            cur["calls"] += row["calls"]
            cur["lastUsedAtMs"] = max(cur["lastUsedAtMs"], row["lastUsedAtMs"])
            cur["latencyMsTotal"] += row["latencyMsTotal"]
            cur["latencySamples"] += row["latencySamples"]
            cur["pricedCalls"] += row["pricedCalls"]
            cur["unpricedCalls"] += row["unpricedCalls"]
            for code, micros in row["costs"].items():
                cur["costs"][code] = cur["costs"].get(code, 0) + micros
        rows = sorted(agg.values(), key=lambda r: r["tokensUsed"], reverse=True)
        for row in rows:
            _finalize_usage_row(row)
        return rows

    def _match(self, period: str):
        if period == "daily":
            day = _utc_day()
            return lambda d: d == day
        if period == "monthly":
            month = _utc_month()
            return lambda d: d.startswith(month)
        return lambda d: True

    def days(self, period: str, subject: str | None = None) -> list[dict]:
        """按 UTC 日分桶的趋势点（升序），口径与 aggregate 同一周期过滤"""
        match = self._match(period)
        buckets: dict[str, dict] = {}
        for (row_subject, name, day), row in self.rows.items():
            if subject is not None and row_subject != subject:
                continue
            if not match(day):
                continue
            cur = buckets.get(day)
            if cur is None:
                cur = {
                    "day": day, "tokensUsed": 0, "calls": 0, "models": 0,
                    "_names": set(), "latencyMsTotal": 0, "latencySamples": 0,
                    "costs": {}, "pricedCalls": 0, "unpricedCalls": 0,
                }
                buckets[day] = cur
            cur["tokensUsed"] += row["promptTokens"] + row["completionTokens"]
            cur["calls"] += row["calls"]
            cur["_names"].add(name)
            cur["latencyMsTotal"] += row["latencyMsTotal"]
            cur["latencySamples"] += row["latencySamples"]
            cur["pricedCalls"] += row["pricedCalls"]
            cur["unpricedCalls"] += row["unpricedCalls"]
            for code, micros in row["costs"].items():
                cur["costs"][code] = cur["costs"].get(code, 0) + micros
        out = []
        for day in sorted(buckets):
            point = buckets[day]
            point["models"] = len(point.pop("_names"))
            _finalize_usage_row(point)
            out.append(point)
        return out

    def users(self, period: str, subject: str | None = None) -> list[dict]:
        """按用户聚合：谁用了多少 token、调用了多少次、涉及哪些模型"""
        match = self._match(period)
        agg: dict[str, dict] = {}
        for (row_subject, name, day), row in self.rows.items():
            if subject is not None and row_subject != subject:
                continue
            if not match(day):
                continue
            cur = agg.get(row_subject)
            if cur is None:
                cur = {
                    "sub": row_subject,
                    "org": row.get("org") or "",
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "tokensUsed": 0,
                    "calls": 0,
                    "lastUsedAtMs": 0,
                    "models": 0,
                    "_names": set(),
                    "latencyMsTotal": 0,
                    "latencySamples": 0,
                    "costs": {},
                    "pricedCalls": 0,
                    "unpricedCalls": 0,
                }
                agg[row_subject] = cur
            cur["promptTokens"] += row["promptTokens"]
            cur["completionTokens"] += row["completionTokens"]
            cur["tokensUsed"] += row["promptTokens"] + row["completionTokens"]
            cur["calls"] += row["calls"]
            cur["lastUsedAtMs"] = max(cur["lastUsedAtMs"], row["lastUsedAtMs"])
            cur["latencyMsTotal"] += row["latencyMsTotal"]
            cur["latencySamples"] += row["latencySamples"]
            cur["pricedCalls"] += row["pricedCalls"]
            cur["unpricedCalls"] += row["unpricedCalls"]
            cur["_names"].add(name)
            if row.get("org"):
                cur["org"] = row["org"]
            for code, micros in row["costs"].items():
                cur["costs"][code] = cur["costs"].get(code, 0) + micros
        rows = sorted(agg.values(), key=lambda r: r["tokensUsed"], reverse=True)
        for row in rows:
            row["models"] = len(row.pop("_names"))
            _finalize_usage_row(row)
        return rows

    def totals(self, period: str, models: list[dict] | None = None) -> dict:
        """周期汇总（延迟按调用数加权；成本按币种分开，未配单价不按 0 元计）"""
        rows = models if models is not None else self.aggregate(period)
        samples = sum(int(r.get("latencySamples") or 0) for r in rows)
        weighted = sum(
            int(r.get("avgLatencyMs") or 0) * int(r.get("latencySamples") or 0)
            for r in rows
        )
        costs: dict[str, int] = {}
        for r in rows:
            for item in r.get("costs") or []:
                code = str(item.get("currency") or "CNY")
                costs[code] = costs.get(code, 0) + int(item.get("micros") or 0)
        return {
            "promptTokens": sum(r["promptTokens"] for r in rows),
            "completionTokens": sum(r["completionTokens"] for r in rows),
            "tokensUsed": sum(r["tokensUsed"] for r in rows),
            "calls": sum(r["calls"] for r in rows),
            "activeModels": len(rows),
            "lastUsedAtMs": max((r["lastUsedAtMs"] for r in rows), default=0),
            "avgLatencyMs": round(weighted / samples) if samples else None,
            "latencySamples": samples,
            "costs": [{"currency": code, "micros": costs[code]} for code in sorted(costs)],
            "pricedCalls": sum(int(r.get("pricedCalls") or 0) for r in rows),
            "unpricedCalls": sum(int(r.get("unpricedCalls") or 0) for r in rows),
        }


def _finalize_usage_row(row: dict) -> None:
    """把内部累计字段收口为对外字段：平均延迟 + 按币种成本清单"""
    samples = int(row.pop("latencySamples", 0) or 0)
    total = int(row.pop("latencyMsTotal", 0) or 0)
    row["avgLatencyMs"] = round(total / samples) if samples else None
    row["latencySamples"] = samples
    costs = row.pop("costs", {}) or {}
    row["costs"] = [
        {"currency": code, "micros": int(costs[code])} for code in sorted(costs)
    ]


USAGE_LEDGER = UsageLedger()


def _usage_of(resp) -> tuple[int, int] | None:
    """从 ChatResponse 取真实 usage（缺失/异常返回 None，不阻断主流程）

    注意：agentscope 的 ChatResponse 是 DictMixin(dict) 子类，属性即字典键，
    对缺失键调用 hasattr/getattr(..., default) 会抛 KeyError 而非返回默认值，
    故这里按 dict 取值。"""
    usage = resp.get("usage") if isinstance(resp, dict) else getattr(resp, "usage", None)
    if usage is None:
        return None
    try:
        return int(usage["input_tokens"] or 0), int(usage["output_tokens"] or 0)
    except Exception:
        try:
            return int(getattr(usage, "input_tokens") or 0), int(getattr(usage, "output_tokens") or 0)
        except Exception:
            return None


def _prices_of(config: dict | None) -> dict | None:
    """模型配置 → 可用单价（输入/输出必须成对且为非负有限数）"""
    prices = config.get("prices") if isinstance(config, dict) else None
    if not isinstance(prices, dict):
        return None
    try:
        price_in = float(prices.get("inputPerM"))
        price_out = float(prices.get("outputPerM"))
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(price_in)
        or not math.isfinite(price_out)
        or price_in < 0
        or price_out < 0
    ):
        return None
    code = str(prices.get("currency") or "CNY").strip().upper() or "CNY"
    return {"inputPerM": price_in, "outputPerM": price_out, "currency": code}


def cost_micros_of(
    prices: dict | None, prompt_tokens: int, completion_tokens: int
) -> tuple[int | None, str | None]:
    """按「每 1M tokens 单价」换算本次调用成本（micros）。

    1 货币单位 = 1_000_000 micros，故 micros = tokens × 单价。未配置单价返回
    (None, None)，由调用方按「未计价」记账，不能当 0 元倾销。
    """
    if not prices:
        return None, None
    try:
        cost = (
            max(0, int(prompt_tokens)) * float(prices["inputPerM"])
            + max(0, int(completion_tokens)) * float(prices["outputPerM"])
        )
    except (KeyError, TypeError, ValueError):
        return None, None
    if not math.isfinite(cost) or cost < 0:
        return None, None
    return int(round(cost)), str(prices.get("currency") or "CNY")


def _parse_last_event_id(header_value: str | None) -> tuple[str, int] | None:
    """解析 Last-Event-ID：格式 `{task_id}:{seq}`"""
    if not header_value or ":" not in header_value:
        return None
    task_id, _, seq_str = header_value.rpartition(":")
    try:
        return task_id, int(seq_str)
    except ValueError:
        return None


# ── 会话目录：工作目录下按对话时间建子目录，存会话内容与产出物 ──

def _create_session_dir(work_dir: str, goal: str) -> Path:
    """在工作目录下按对话时间创建会话子目录：{workDir}/YYYY-MM-DD_HH-MM-SS_{目标摘要}"""
    base = Path(work_dir).expanduser() if work_dir else WORK_DIR / "sessions"
    ts = time.strftime("%Y-%m-%d_%H-%M-%S")
    slug = "".join(c for c in goal[:12] if c.isalnum() or c in "-_ ").strip().replace(" ", "-")
    name = f"{ts}_{slug}" if slug else ts
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_conversation(session_dir: "Path | None", line: str) -> None:
    """追加写入会话记录 conversation.md（失败不阻断主流程）"""
    if not session_dir:
        return
    try:
        with open(session_dir / "conversation.md", "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except Exception as exc:
        print(f"[engine] 会话记录写入失败: {exc}", flush=True)


def _materialize_artifacts(session_dir: "Path | None", data: dict) -> dict:
    """产出物路径校验：_diff_files 已返回工具真实写出的文件绝对路径，
    此处仅过滤掉不存在的条目后透传。

    注意：绝不能把产物复制到会话子目录并用 final_text 覆盖，
    否则预览到的将是回复文本而非真实文件内容。
    无产物时直接返回（简单问答类任务无交付物，最终答案随 final_text 返回）。"""
    artifacts = data.get("artifacts") or []
    if not artifacts:
        return data
    paths: list = []
    for a in artifacts:
        try:
            if Path(str(a)).exists():
                paths.append(str(a))
        except Exception:
            continue
    return {**data, "artifacts": paths}


def _preserve_artifacts_in_session(session_dir: "Path | None", task_dir: Path, paths: list) -> list:
    """把隔离环境内的产物复制进会话目录归档，返回副本路径。

    environment/ 整体可被「清理工作环境文件」删除，产物只有落在会话目录里才谈得上保留。
    单条复制失败时保留原路径并打印告警，不静默丢产物。"""
    if session_dir is None or not paths:
        return paths
    kept_root = session_dir / "artifacts"
    out: list = []
    for raw in paths:
        src = Path(str(raw))
        try:
            rel = src.relative_to(task_dir)
        except ValueError:
            out.append(str(src))
            continue
        dst = kept_root / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            out.append(str(dst))
        except OSError as exc:
            print(f"[engine] 产物归档到会话目录失败: {exc}", flush=True)
            out.append(str(src))
    return out


# ── 两阶段协议：计划生成（流 3.1，核心闭环补齐） ────────────────

PLAN_PROMPT = """请将用户目标拆解为 2-5 个执行步骤，仅输出 JSON 数组，格式：
[{{"step_id": "step-1", "type": "llm_call", "description": "步骤说明"}}]
type 可选值：llm_call / file_read / file_write / file_convert / user_confirm
用户目标：{goal}{context}"""

# 计划阶段的材料摘录上限：足够让步骤贴合真实附件，又不让规划烧掉整篇正文
PLAN_CONTEXT_CHARS = 8000


def _plan_context_block(raw: str) -> str:
    """计划阶段同样要看到随问材料，否则「分析这份附件」会拆出与正文无关的步骤"""
    text = (raw or "").strip()
    if not text:
        return ""
    if len(text) > PLAN_CONTEXT_CHARS:
        text = text[:PLAN_CONTEXT_CHARS] + "\n…（材料过长，计划阶段只取前段）"
    return "\n\n随问材料：\n" + text


def _fallback_plan(goal: str) -> list:
    """无模型/生成失败时的确定性兜底计划（降级不阻断）"""
    return [
        {"step_id": "step-1", "type": "llm_call", "description": f"分析并处理：{goal[:60]}"},
        {"step_id": "step-2", "type": "file_write", "description": "产出交付物并总结"},
    ]


async def _generate_plan(goal: str, context: str = "", mode: str = "general",
                         images: list | None = None, pii_mode: str = "off",
                         on_redaction=None, model_binding: str | None = None,
                         model_config: dict | None = None
                         ) -> tuple[list, dict | None, list[dict]]:
    """单次 LLM 调用生成计划 steps；任何异常/解析失败均回退兜底计划。
    返回 (steps, usage, route_switches)：usage 为该次计划生成的真实用量（缺省 None，
    含延迟/成本），route_switches 为计划阶段发生的模型故障转移（供上游摊开说明）。
    mode 决定计划的产出取向（幻灯片要先列页级大纲、写作要先给结构等）；
    images 为视觉模型下随计划一起下发的图片（让「分析这张截图」拆出对的步骤）。
    pii_mode 为出站脱敏档位：block 档命中时必须中止，不能回退兜底计划假装成功。"""
    captured: list = []
    switches: list[dict] = []

    def _capture(prompt: int, completion: int, model_name: str,
                 latency_ms: int | None = None, cost_micros: int | None = None,
                 currency: str | None = None, priced: bool = False) -> None:
        captured.append({
            "promptTokens": prompt,
            "completionTokens": completion,
            "model": model_name,
            "latencyMs": latency_ms,
            "costMicros": cost_micros,
            "costCurrency": currency,
            "priced": priced,
        })

    def _capture_switch(info: dict) -> None:
        switches.append({
            "from": str(info.get("from") or ""),
            "to": str(info.get("to") or ""),
            "reason": str(info.get("reason") or ""),
        })

    try:
        # 计划需按完整响应解析 JSON，故以非流式调用（流式下 await model(...) 得到的是
        # 异步生成器，取不到 content，此前恒回退兜底计划）
        # 计划阶段也必须用本轮真正生效的模型（此前没传，选了自定义模型/专家时
        # 计划仍走 .env 默认模型，和后面的执行不是同一个模型）
        model, formatter = build_model_and_formatter(
            model_binding, model_config, stream=False, on_usage=_capture,
            pii_mode=pii_mode, on_redaction=on_redaction,
            on_route_switch=_capture_switch,
        )
        from agentscope.message import Msg
        plan_text = PLAN_PROMPT.format(goal=goal, context=_plan_context_block(context))
        mode_hint = rc.compose_mode(mode).strip()
        if mode_hint:
            plan_text = f"{mode_hint}\n\n{plan_text}"
        if images:
            plan_text += rc.compose_image_hint(images)
            blocks: list = [{"type": "text", "text": plan_text}]
            for img in images:
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img["mime"],
                        "data": img["data"],
                    },
                })
            msgs = [Msg(name="user", content=blocks, role="user")]
        else:
            msgs = [Msg(name="user", content=plan_text, role="user")]
        formatted = await formatter.format(msgs)
        resp = await model(formatted)
        text = _text_of(getattr(resp, "content", ""))
        start, end = text.find("["), text.rfind("]")
        if start >= 0 and end > start:
            steps = json.loads(text[start:end + 1])
            if isinstance(steps, list):
                norm = []
                for i, s in enumerate(steps[:5]):
                    if isinstance(s, dict):
                        norm.append({
                            "step_id": str(s.get("step_id") or f"step-{i + 1}"),
                            "type": str(s.get("type") or "llm_call"),
                            "description": str(s.get("description") or f"步骤 {i + 1}"),
                        })
                if norm:
                    return norm, (captured[-1] if captured else None), switches
    except pg.PrivacyBlocked:
        # 脱敏档位为 block：命中就不该把内容发出去，也不该退回兜底计划继续
        raise
    except Exception as exc:
        print(f"[engine] plan 生成失败，回退兜底计划: {exc}", flush=True)
    return _fallback_plan(goal), (captured[-1] if captured else None), switches


def _sse_formatter(session: "TaskSession", task_id: str):
    """构造 sse(event, data) 格式化函数：缺 seq 时补分配并入窗口"""

    def sse(event: str, data: dict) -> str:
        if "seq" not in data:
            seq = session.next_seq()
            data = {**data, "seq": seq, "taskId": task_id}
            session.record(seq, event, data)
        return (
            f"id: {task_id}:{data.get('seq')}\n"
            f"event: {event}\n"
            f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        )

    return sse


def _replay_prefix(session: "TaskSession", task_id: str, replay_from: list):
    """重放窗口内历史事件的 SSE 文本生成器"""
    for seq, event, data in replay_from:
        yield (
            f"id: {task_id}:{seq}\n"
            f"event: {event}\n"
            f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        )


def _experts_block(session: "TaskSession") -> Any:
    """runtime.experts 是注册表本体；normalize_registry 不接受整包 runtime。"""
    runtime = session.runtime if isinstance(session.runtime, dict) else {}
    return runtime.get("experts")


def _fallback_notes(model_config: dict | None) -> list[str]:
    """装配侧对转移链的如实说明（目标删除/缺密钥/接入方式不同等）"""
    cfg = model_config if isinstance(model_config, dict) else {}
    notes = cfg.get("fallbackNotes")
    if not isinstance(notes, list):
        return []
    return [str(n).strip() for n in notes if str(n).strip()]


def _fallback_summary(model_config: dict | None) -> str:
    """生效中的转移链摘要；没有可用目标时返回空串（不谎称容灾已生效）"""
    cfg = model_config if isinstance(model_config, dict) else {}
    fallbacks = cfg.get("fallbacks")
    if not isinstance(fallbacks, list):
        return ""
    names = []
    for item in fallbacks[:3]:
        if isinstance(item, dict):
            name = str(item.get("displayName") or item.get("modelName") or "").strip()
            if name:
                names.append(name)
    if not names:
        return ""
    primary = str(cfg.get("displayName") or cfg.get("modelName") or "主模型").strip()
    chain = " → ".join([primary] + names)
    return (
        f"模型容灾已开启：{chain}；"
        "主模型在输出首个响应前失败时自动切换，已输出的流式响应不会拼接另一个模型的内容。"
    )


def _resolve_lead_expert(
    session: "TaskSession",
) -> tuple[dict | None, dict | None, str]:
    """解析本轮负责人专家；返回 (expert, 绑定的模型配置, 错误)。

    注册表损坏、专家被删除、绑定模型不可用都必须失败关闭——静默退回通用 Agent
    会让用户以为选中的专家在工作，实际口径和模型都不是专家的。
    """
    experts, registry_error = er.normalize_registry(_experts_block(session))
    if not session.expert_id:
        return None, None, registry_error or ""
    if registry_error:
        return None, None, f"专家团队不可用：{registry_error}"
    expert = er.find_expert(experts, session.expert_id)
    if expert is None:
        return None, None, (
            f"本轮负责人专家 {session.expert_id} 不在专家注册表中（可能已被删除），"
            "请刷新页面后重新选择"
        )
    model_config, model_error = er.model_config_of(expert)
    if model_error:
        return expert, None, model_error
    return expert, model_config, ""


def _effective_model_settings(
    model_binding: str | None, model_config: dict | None, expert_config: dict | None
) -> tuple[str | None, dict | None]:
    """专家绑定模型优先于对话内模型档位；未绑定时才沿用本轮所选模型。"""
    if expert_config:
        return None, expert_config
    return model_binding, model_config


async def _plan_stream(session: "TaskSession", task_id: str, goal: str):
    """阶段一：计划生成流（plan + [usage] + status_change(pending_review) + done）"""
    sse = _sse_formatter(session, task_id)
    if not goal:
        yield sse("error", {"code": "EMPTY_GOAL", "message": "任务描述为空"})
        yield sse("done", {})
        return
    lead_expert, lead_config, lead_error = _resolve_lead_expert(session)
    if lead_error and session.expert_id:
        yield sse("error", {"code": "EXPERT_UNAVAILABLE", "message": lead_error})
        yield sse("done", {})
        return
    if lead_expert:
        note = f"本轮负责人专家：「{lead_expert['name']}」；{er.model_note(lead_expert)}"
        yield sse("step_start", {
            "step_id": "expert-context",
            "stepType": "context",
            "description": note,
        })
        yield sse("step_done", {"step_id": "expert-context", "output": note})
    elif lead_error:
        note = f"专家团队未生效：{lead_error}"
        yield sse("step_start", {
            "step_id": "expert-context",
            "stepType": "context",
            "description": note,
        })
        yield sse("step_done", {"step_id": "expert-context", "output": note})
    eff_binding, eff_config = _effective_model_settings(
        session.model_binding, session.model_config, lead_config
    )
    plan_vision, _ = vision_status(eff_binding, eff_config)
    plan_images = rc.collect_vision_images(session.attachments) if plan_vision else []
    attach_text, attach_notes = rc.compose_attachments(
        session.attachments, images_inlined=plan_vision
    )
    # 审阅计划前先把「哪些材料没进模型」摊出来：否则用户批准的是一个够不着的计划
    for i, note in enumerate(attach_notes, 1):
        yield sse("step_start", {
            "step_id": f"ctx-{i}",
            "stepType": "context",
            "description": note,
        })
        yield sse("step_done", {"step_id": f"ctx-{i}", "output": note})
    # 容灾链与不可用目标：计划阶段就要可见，不能等执行失败才让用户发现
    for i, note in enumerate(_fallback_notes(eff_config), 1):
        yield sse("step_start", {
            "step_id": f"fallback-{i}",
            "stepType": "context",
            "description": note,
        })
        yield sse("step_done", {"step_id": f"fallback-{i}", "output": note})
    fallback_summary = _fallback_summary(eff_config)
    if fallback_summary:
        yield sse("step_start", {
            "step_id": "fallback-chain",
            "stepType": "context",
            "description": fallback_summary,
        })
        yield sse("step_done", {"step_id": "fallback-chain", "output": fallback_summary})
    pii_mode = pg.from_runtime(session.runtime, session.pii_mode)
    pii_error = pg.runtime_error(session.runtime)
    if pii_error:
        # 档位读不出来时不能按 off 继续：用户选过脱敏，未知档位下出站等于静默降级
        yield sse("error", {
            "code": "PRIVACY_CONFIG_UNAVAILABLE",
            "message": f"出站脱敏档位不可读，任务已中止（避免按未脱敏发送）：{pii_error}",
        })
        yield sse("done", {})
        return
    plan_counts: dict[str, int] = {}

    def _on_plan_redaction(counts: dict) -> None:
        for key, value in counts.items():
            plan_counts[key] = plan_counts.get(key, 0) + int(value or 0)

    try:
        steps, plan_usage, plan_switches = await _generate_plan(
            goal, attach_text, session.mode, plan_images,
            pii_mode=pii_mode, on_redaction=_on_plan_redaction,
            model_binding=eff_binding, model_config=eff_config,
        )
    except pg.PrivacyBlocked as exc:
        yield sse("error", {"code": "PRIVACY_OUTBOUND_BLOCKED", "message": str(exc)})
        yield sse("done", {})
        return
    if plan_counts:
        note = (
            f"计划阶段出站脱敏：{pg.describe(plan_counts)}已替换为掩码，"
            "模型看到的是脱敏后的内容。"
        )
        yield sse("step_start", {
            "step_id": "privacy-plan",
            "stepType": "context",
            "description": note,
        })
        yield sse("step_done", {"step_id": "privacy-plan", "output": note})
    for i, switch in enumerate(plan_switches, 1):
        yield sse("route_switch", switch)
        note = (
            f"计划阶段主模型「{switch['from']}」调用失败，已切换到备用模型"
            f"「{switch['to']}」：{switch['reason']}"
        )
        yield sse("step_start", {
            "step_id": f"route-plan-{i}",
            "stepType": "context",
            "description": note,
        })
        yield sse("step_done", {"step_id": f"route-plan-{i}", "output": note})
    session.plan = steps
    session.goal = goal
    yield sse("plan", {"plan_id": f"plan-{task_id}", "steps": steps})
    # 计划阶段的真实 token 用量（Rust 侧按 usage 事件记账；前端不消费该类型）
    if plan_usage:
        yield sse("usage", {"usage": plan_usage})
    yield sse("status_change", {"taskId": task_id, "status": "pending_review"})
    yield sse("done", {})


def _normalize_history(raw) -> list:
    """对话历史归一：仅保留带正文的 user/assistant 条目（上限 40 条）。

    桌面端走两阶段协议时，历史随 plan 请求一起到达，approve 之后才真正执行；
    没有这一步归一，非法条目会在预填充阶段被静默跳过，日志里看不出丢了什么。
    """
    if not isinstance(raw, list):
        return []
    out: list = []
    for item in raw[-40:]:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        role = str(item.get("role") or "user")
        out.append({"role": role if role in ("user", "assistant") else "user", "content": content})
    return out


def _effective_history(session: "TaskSession", history: list | None) -> list:
    """执行流取历史的两阶段语义：approve 不再重复带历史（None → 用会话里的），
    本轮确实没有历史时前端传空列表（[] → 保持为空，不把上一轮的会话历史翻出来）。"""
    return session.history if history is None else history


def _snapshot_files(base: Path) -> dict:
    """工作目录文件快照：{相对路径: mtime}（用于产出物 diff）"""
    snap: dict = {}
    try:
        for p in base.rglob("*"):
            if p.is_file():
                try:
                    snap[str(p.relative_to(base))] = p.stat().st_mtime
                except Exception:
                    pass
    except Exception:
        pass
    return snap


def _diff_files(base: Path, before: dict, after: dict, exclude_dir: "Path | None" = None) -> list:
    """对比前后快照，返回新增/变更文件的绝对路径（排除会话记录目录）"""
    excl_rel: str | None = None
    if exclude_dir is not None:
        try:
            excl_rel = str(exclude_dir.resolve().relative_to(base.resolve()))
        except Exception:
            excl_rel = None
    changed: list = []
    for rel, mtime in after.items():
        if before.get(rel) == mtime:
            continue
        # 排除会话记录目录（conversation.md 等不属于任务产物）
        if excl_rel is not None and (rel == excl_rel or rel.startswith(excl_rel + os.sep)):
            continue
        changed.append(str(base / rel))
    return changed


async def _run_execution(session: "TaskSession", task_id: str, goal: str, replay_from: list, model_binding: str | None = None, work_dir: "Path | None" = None, history: list | None = None, model_config: dict | None = None, resume: bool = False):
    """阶段二：执行流（status_change(executing) → Agent 事件 → deliver/done）。
    model_binding 为对话内选中的模型档位；
    work_dir 为前端选择的工作目录（工具操作与产物落盘均限定其中）；
    history 为此前轮次对话历史（保障连续对话上下文）。
    启用安全工作环境且未自选目录时，执行落在 environment/{taskId} 隔离目录。
    resume=True 时从 session.checkpoint 恢复记忆与副作用台账，不重新预填充历史。"""
    sse = _sse_formatter(session, task_id)
    # 两阶段协议下历史随 plan 请求到达并存在会话里，approve 的执行流不再重复带
    history = _effective_history(session, history)
    isolated = work_dir is None and session.secure_env
    eff_work_dir = work_dir or (_task_work_dir(task_id) if isolated else WORK_DIR)

    for chunk in _replay_prefix(session, task_id, replay_from):
        yield chunk

    if not goal:
        yield sse("error", {"code": "EMPTY_GOAL", "message": "任务描述为空"})
        yield sse("done", {})
        return

    # 出站脱敏档位：runtime 优先，浏览器直连随请求下发；off 时不包装模型
    pii_mode = pg.from_runtime(session.runtime, session.pii_mode)
    pii_error = pg.runtime_error(session.runtime)
    if pii_error:
        # 计划阶段已拦一次；approve 前设置损坏也要中止，不能带着未知档位发出去
        yield sse("error", {
            "code": "PRIVACY_CONFIG_UNAVAILABLE",
            "message": f"出站脱敏档位不可读，任务已中止（避免按未脱敏发送）：{pii_error}",
        })
        yield sse("done", {})
        return
    # 逐工具审批：档位读不出来或网关不可用都中止——审批策略不可知时继续执行
    # 等于绕过审批（曾把无效档位降成 off，结果静默放行）
    tool_approval = tap.ToolApproval.from_runtime(session.runtime)
    if tool_approval.runtime_present and tool_approval.error:
        yield sse("error", {
            "code": "TOOL_APPROVAL_UNAVAILABLE",
            "message": (
                f"工具审批策略不可用，任务已中止：{tool_approval.error}"
                "（避免未经审批就写文件或执行命令）"
            ),
        })
        yield sse("done", {})
        return

    # 负责人专家：注册表损坏、专家被删除、绑定模型不可用都中止。
    # 静默退回通用 Agent 会让用户以为专家在工作，实际提示词和模型都不是专家的。
    experts, experts_error = er.normalize_registry(_experts_block(session))
    lead_expert, lead_config, lead_error = _resolve_lead_expert(session)
    if session.expert_id and lead_error:
        yield sse("error", {"code": "EXPERT_UNAVAILABLE", "message": lead_error})
        yield sse("done", {})
        return
    # 专家绑定的模型优先于对话内所选模型；未绑定才沿用本轮模型
    eff_binding, eff_config = _effective_model_settings(
        model_binding, model_config, lead_config
    )

    # 构建 Agent（模型配置错误在此暴露；模型档位随对话绑定；工具限定在工作目录；model_config 支持自定义模型真实调用）
    # 队列先建：用量回调需要随模型调用即时产出 usage 事件（Rust 侧据此记账）
    queue = RecordingQueue(session)
    checkpoint = session.checkpoint
    checkpoint.emit = lambda ev, data: _safe_put(queue, ev, data)
    checkpoint.begin_run()

    def _on_usage(prompt_tokens: int, completion_tokens: int, model_name: str,
                  latency_ms: int | None = None, cost_micros: int | None = None,
                  currency: str | None = None, priced: bool = False) -> None:
        _safe_put(queue, "usage", {
            "usage": {
                "promptTokens": prompt_tokens,
                "completionTokens": completion_tokens,
                "model": model_name,
                "latencyMs": latency_ms,
                "costMicros": cost_micros,
                "costCurrency": currency,
                "priced": priced,
            },
        })

    route_count = 0

    def _on_route_switch(info: dict) -> None:
        nonlocal route_count
        route_count += 1
        source = str(info.get("from") or "")
        target = str(info.get("to") or "")
        reason = str(info.get("reason") or "")
        _safe_put(queue, "route_switch", {
            "from": source,
            "to": target,
            "reason": reason,
        })
        note = (
            f"主模型「{source}」调用失败，已切换到备用模型「{target}」：{reason}"
        )
        _safe_put(queue, "step_start", {
            "step_id": f"route-{route_count}",
            "stepType": "context",
            "description": note,
        })
        _safe_put(queue, "step_done", {
            "step_id": f"route-{route_count}",
            "output": note,
        })

    privacy_counts: dict[str, int] = {}

    def _on_privacy(counts: dict) -> None:
        for key, value in counts.items():
            privacy_counts[key] = privacy_counts.get(key, 0) + int(value or 0)
        _safe_put(queue, "step_progress", {
            "step_id": "privacy-outbound",
            "message": (
                f"出站脱敏：{pg.describe(privacy_counts)}已替换为掩码，"
                "模型看到的是脱敏后的内容"
            ),
        })

    # 工作区沙箱策略：工具白/黑名单与文件边界随 runtime 下发（P1-9）
    sandbox = sr.SandboxPolicy.from_runtime(session.runtime, work_dir=eff_work_dir)
    # 受控执行/联网：同一份沙箱策略里的显式授权；默认关闭，未授权不注册工具
    grants = tg.ToolGrantPolicy.from_runtime(session.runtime)
    # 联网搜索：搜索源来自进程环境，授权沿用同一份联网白/名单；只给父 Agent
    web = ws.WebSearchRuntime.from_env(grants)
    # 视觉能力按本轮真正生效的模型判定（专家绑定的模型可能没有视觉能力）
    vision, vision_reason = vision_status(eff_binding, eff_config)
    # 电脑操控：就绪才注册工具；档位/网关/平台/视觉能力任一不满足都如实说明。
    # 只给父 Agent：子 Agent 不继承桌面输入能力（与受控执行/派发工具的边界一致）。
    computer = cu.ComputerUse.from_runtime(session.runtime, vision=vision, task_id=task_id)
    # 浏览器 AI+：桌面壳持有独立配置目录与放行网关；子 Agent 同样不继承浏览器控制能力
    browser = bu.BrowserUse.from_runtime(session.runtime, vision=vision, task_id=task_id)
    # 本地知识库：索引是派生物，库目录由 Rust 装配；没有可用库时不注册检索工具
    knowledge = kn.KnowledgeRuntime.from_runtime(session.runtime)
    # ACP 委派：用户显式配置启动命令后才注册；只给父 Agent，子 Agent 不能递归外派
    acp = acpm.AcpRuntime.from_runtime(session.runtime, work_dir=str(eff_work_dir))
    # 执行面 P0-3：用户配置的 Hooks 由本机 Rust 网关执行（与 Hooks 页试运行同一个 run_command），
    # 引擎只负责在真实存在的执行节点上报事件，并把「没触发/没放行/没通道」摊成可见说明。
    # 先于 Agent 构建，子 Agent 运行时与父 Agent 共用同一个 HookRunner（SubagentStart/Stop 也走这里）。
    hook_gw = hr.from_runtime(session.runtime)
    hook_runner = hr.HookRunner(
        hook_gw, lambda ev, data: _safe_put(queue, ev, data),
        task_id=task_id, cwd=str(eff_work_dir),
    )
    # 子 Agent 也是执行能力：沙箱配置了工具白/黑名单时，spawn_subagents 同样要被判定。
    # 不在允许范围就不注册、不写进 system prompt，并把原因作为可见说明下发。
    subagent_denial = sandbox.tool_denial("spawn_subagents") if sandbox.configured else None
    subagent_runner = None
    if subagent_denial is None:
        def _make_child_agent(name: str, max_iters: int, expert: dict | None):
            # 子 Agent 只带技能清单，不再注入父 Agent 的对话/意识，也不注册派发工具；
            # 沙箱、Hooks 与用量回调与父任务同一套。专家绑定模型优先于对话模型，
            # 未绑定（或通用子 Agent）则沿用本轮对话选择的模型，不继承负责人的绑定。
            expert_config, expert_err = er.model_config_of(expert)
            if expert_err:
                # parse_tasks 已拦过；这里再兜一层，避免绕过工具直接构造时静默换模型
                raise RuntimeError(expert_err)
            child_binding, child_config = _effective_model_settings(
                model_binding, model_config, expert_config
            )
            child_skills, _missing = er.filter_skills(
                rc.normalize_skills(session.runtime), expert
            )
            return build_agent(
                child_binding, eff_work_dir, child_config, on_usage=_on_usage,
                runtime={"skills": child_skills},
                sandbox=sandbox, mode="general", knowledge=knowledge,
                base_prompt=sar.SUBAGENT_PROMPT,
                extra_prompt=er.child_role_prompt(expert),
                agent_name=name, max_iters=max_iters,
                pii_mode=pii_mode, on_privacy=_on_privacy,
                on_route_switch=_on_route_switch,
            )

        subagent_runner = sar.SubagentRunner(
            make_agent=_make_child_agent,
            emit=lambda ev, data: _safe_put(queue, ev, data),
            task_id=task_id,
            goal=goal,
            work_dir=str(eff_work_dir),
            sandbox=sandbox,
            hooks=hook_runner,
            approval=tool_approval,
            experts=experts,
            experts_error=experts_error,
            available_skill_ids=[
                str(s.get("id") or "")
                for s in rc.normalize_skills(session.runtime)
            ],
        )
    try:
        agent = build_agent(eff_binding, eff_work_dir, eff_config, on_usage=_on_usage,
                            runtime=session.runtime, sandbox=sandbox, mode=session.mode,
                            subagent_runner=subagent_runner, grants=grants, computer=computer,
                            browser=browser, knowledge=knowledge, acp=acp, web=web,
                            extra_prompt=er.lead_system_hint(lead_expert),
                            pii_mode=pii_mode, on_privacy=_on_privacy,
                            emit=lambda ev, data: _safe_put(queue, ev, data), task_id=task_id,
                            query=goal, on_route_switch=_on_route_switch)
    except Exception as exc:
        # 构建失败没有改变已有检查点：恢复请求仍可再试，不把可恢复状态误标成不可用；
        # 首次执行就没有任何快照，如实说明「无可恢复点」，不承诺能继续
        if resume and checkpoint.messages:
            checkpoint.mark_final(True, "上次执行在模型调用前中断，可从检查点继续")
        else:
            checkpoint.mark_final(False, "执行环境构建失败，尚未产生可恢复的检查点")
        yield sse("error", {"code": "ENGINE_CONFIG_ERROR", "message": str(exc)})
        yield sse("checkpoint", checkpoint.final_event())
        yield sse("done", {})
        return

    # 模型代理：每次真实模型调用前保存记忆快照（用量包装在内层，互不影响）
    checkpoint_model = ckpt.CheckpointedModel(agent.model, checkpoint)
    checkpoint_model.agent = agent
    agent.model = checkpoint_model

    yield sse("status_change", {"taskId": task_id, "status": "executing"})

    session.exec_queue = queue
    # 副作用幂等门禁必须最先注册（洋葱最内层）：沙箱/Hooks 在外层，
    # 被拒绝的调用不会进入台账；恢复时装不上必须中止，避免重复写盘/外发。
    ckpt_err = ckpt.install(agent.toolkit, checkpoint)
    if ckpt_err:
        message = f"断点续跑的副作用去重门禁没有装上：{ckpt_err}"
        if resume:
            yield sse("error", {
                "code": "CHECKPOINT_GUARD_FAILED",
                "message": message + "（已中止恢复，避免重复执行副作用）",
            })
            yield sse("checkpoint", checkpoint.final_event())
            yield sse("done", {})
            return
        _safe_put(queue, "step_start", {
            "step_id": "checkpoint-guard",
            "stepType": "checkpoint",
            "description": message + "（本次执行继续，但中断后无法保证副作用不重复）",
        })
    # 沙箱门禁：白/黑名单拒绝发生在 Hooks 之前，越权工具不会先触达用户脚本
    guard_err = sandbox.install(
        agent.toolkit, lambda ev, data: _safe_put(queue, ev, data), task_id=task_id
    )
    if guard_err:
        # 同上：恢复时已有快照才保留「可再试」，首次执行如实标为不可续跑
        if resume and checkpoint.messages:
            checkpoint.mark_final(True, "上次执行在模型调用前中断，可从检查点继续")
        else:
            checkpoint.mark_final(False, "沙箱门禁未装上，尚未产生可恢复的检查点")
        yield sse("error", {
            "code": "SANDBOX_GUARD_FAILED",
            "message": f"沙箱策略已配置，但工具门禁没有装上：{guard_err}",
        })
        yield sse("checkpoint", checkpoint.final_event())
        yield sse("done", {})
        return
    attach_hooks(agent, queue)
    # system prompt 侧没能生效的部分：与 build_agent 同一口径，供事件如实展示
    ctx_notes = [
        *rc.context_notes(session.runtime, goal), *sandbox.notes(), *grants.notes(),
        *computer.notes(), *browser.notes(), *knowledge.notes(),
        *acp.notes(), *web.notes(), *tool_approval.notes(),
        *getattr(agent, "_tt_context_notes", []),
    ]
    # 容灾链同样属于「本轮真实执行口径」：哪些目标生效、哪些被跳过都要可见
    fallback_summary = _fallback_summary(eff_config)
    if fallback_summary:
        ctx_notes.append(fallback_summary)
    ctx_notes.extend(_fallback_notes(eff_config))
    if pg.mode_note(pii_mode):
        ctx_notes.append(pg.mode_note(pii_mode))
    if lead_expert:
        ctx_notes.append(
            f"本轮负责人专家：「{lead_expert['name']}」；{er.model_note(lead_expert)}"
        )
    elif experts_error:
        ctx_notes.append(f"专家团队不可用，子 Agent 不能按专家派发：{experts_error}")
    if computer.ready and sandbox.configured:
        # 工具仍然注册（由沙箱门禁逐次判定），但把「会被拒绝」提前说清楚
        for tool_name in ("screen_capture", "computer_action"):
            denial = sandbox.tool_denial(tool_name)
            if denial is not None:
                ctx_notes.append(f"电脑操控工具「{tool_name}」会被沙箱拒绝：{denial[0]}")
    if browser.ready and sandbox.configured:
        for tool_name in ("browser_open", "browser_snapshot", "browser_click",
                          "browser_type", "browser_scroll", "browser_screenshot"):
            denial = sandbox.tool_denial(tool_name)
            if denial is not None:
                ctx_notes.append(f"浏览器工具「{tool_name}」会被沙箱拒绝：{denial[0]}")
    if subagent_denial is not None:
        ctx_notes.append(f"本地子 Agent 未启用：{subagent_denial[0]}")
    hr.install(agent.toolkit, hook_runner)
    # 逐工具审批注册在沙箱/Hooks 之后：越权调用不会先弹确认框，用户看到的就是能执行的调用
    approval_err = tool_approval.install(
        agent.toolkit, lambda ev, data: _safe_put(queue, ev, data), task_id=task_id
    )
    if approval_err:
        if resume and checkpoint.messages:
            checkpoint.mark_final(True, "上次执行在模型调用前中断，可从检查点继续")
        else:
            checkpoint.mark_final(False, "工具审批门禁未装上，尚未产生可恢复的检查点")
        yield sse("error", {
            "code": "TOOL_APPROVAL_GUARD_FAILED",
            "message": f"工具审批已开启，但审批门禁没有装上：{approval_err}",
        })
        yield sse("checkpoint", checkpoint.final_event())
        yield sse("done", {})
        return

    from agentscope.message import Msg

    async def run_agent():
        mcp_clients: list = []
        end_reason = "completed"
        end_note = ""
        resume_info: dict = {}
        try:
            await hook_runner.fire(
                "SessionStart", payload={"source": "resume" if resume else "desktop"}
            )
            if resume:
                # 恢复记忆：末尾用户消息由本轮重新组装（保留附件正文与图片能力判定），
                # 其余消息按快照顺序还原，模型据此看到中断前的工具结果。
                resume_info = await checkpoint.restore_into(agent, Msg)
                scope_note = (
                    "（检查点已持久化到本机磁盘，引擎重启后仍可继续）"
                    if checkpoint.scope == "disk"
                    else "（检查点仅在当前引擎进程内有效）"
                )
                note = (
                    f"从检查点恢复：{resume_info.get('restoredMessages', 0)} 条消息、"
                    f"{resume_info.get('sideEffects', 0)} 条副作用记录{scope_note}"
                )
                if resume_info.get("droppedMessages"):
                    note += f"；为控制体积省略较早消息 {resume_info['droppedMessages']} 条"
                if resume_info.get("mediaOmitted"):
                    note += f"；{resume_info['mediaOmitted']} 处媒体内容未进入快照"
                if checkpoint.attachments_media_omitted:
                    note += (
                        f"；{checkpoint.attachments_media_omitted} 张图片未写入磁盘检查点，"
                        "恢复后不会重新下发"
                    )
                _safe_put(queue, "step_start", {
                    "step_id": "checkpoint-restore",
                    "stepType": "checkpoint",
                    "description": note,
                })
                _safe_put(queue, "step_done", {"step_id": "checkpoint-restore", "output": note})
                if checkpoint.capture_error:
                    warn = f"上次检查点采集提示：{checkpoint.capture_error}"
                    _safe_put(queue, "step_start", {
                        "step_id": "checkpoint-capture",
                        "stepType": "checkpoint",
                        "description": warn,
                    })
                    _safe_put(queue, "step_done", {"step_id": "checkpoint-capture", "output": warn})
                if checkpoint.records_overflow:
                    warn = "副作用台账已达上限，较早的调用记录不再用于续跑去重"
                    _safe_put(queue, "step_start", {
                        "step_id": "checkpoint-overflow",
                        "stepType": "checkpoint",
                        "description": warn,
                    })
                    _safe_put(queue, "step_done", {"step_id": "checkpoint-overflow", "output": warn})
            else:
                # 预填充历史对话（保障连续对话：第二问可知第一问上下文）
                # 上下文压缩：仅保留最近 20 条，单条截断到 600 字符，控制 token 占用
                for h in (history or [])[-20:]:
                    try:
                        role = str(h.get("role") or "user")
                        content = str(h.get("content") or "").strip()
                        if not content:
                            continue
                        if len(content) > 600:
                            content = content[:600] + "…（已截断）"
                        role = role if role in ("user", "assistant") else "user"
                        await agent.memory.add(Msg(
                            name="user" if role == "user" else "通天助手",
                            content=content,
                            role=role,
                        ))
                    except Exception:
                        continue

                # 上下文真的被截过才报 PreCompact：没截就不该让用户以为压缩钩子跑了
                if len(history or []) > 20:
                    await hook_runner.fire(
                        "PreCompact", payload={"trigger": "auto", "kept": 20}
                    )

            # 执行面 P0：附件正文、显式引用的技能随本轮消息进入模型；
            # 图片按模型视觉能力走 ImageBlock，未能进入的部分（无视觉能力、超限、
            # 抽取失败、意识关闭…）作为步骤事件摊出来
            mcp_result = await mr.attach(
                agent.toolkit, session.runtime,
                lazy=bool(getattr(agent, "_tt_lazy_mcp", False)),
            )
            mcp_clients = mcp_result["clients"]
            images = rc.collect_vision_images(session.attachments) if vision else []
            prompt, msg_notes, _skill = rc.compose_user_message(
                goal, session.runtime, session.attachments, images_inlined=vision
            )
            if images:
                prompt += rc.compose_image_hint(images)
            elif session.attachments and any(
                isinstance(a.get("image"), dict) for a in session.attachments
            ):
                # 有图片但没有下发：把判定原因（模型名 / TT_VISION）摊给用户
                msg_notes.insert(0, vision_reason)
            if resume:
                prompt += (
                    "\n\n[断点续跑] 这是同一任务在中断后的继续执行。"
                    "中断前已完成的工具调用与结果已从检查点恢复；"
                    "请先核对已有结果，从上次中断处继续，不要重复已完成的步骤。"
                )
            # 恢复轮不重复播报意识/技能等静态上下文，只播报本轮真实的新信息
            notes = [
                *([] if resume else ctx_notes),
                *hook_gw.notes(), *mcp_result["notes"], *msg_notes,
            ]
            for i, note in enumerate(notes, 1):
                _safe_put(queue, "step_start", {
                    "step_id": f"ctx-{i}",
                    "stepType": "context",
                    "description": note,
                })
                _safe_put(queue, "step_done", {"step_id": f"ctx-{i}", "output": note})

            # 执行前快照：执行后 diff 得到真实产出物。
            # 恢复时沿用中断前的基线，把上次已产出的文件也算进最终交付物。
            if resume and checkpoint.baseline_files is not None:
                before = checkpoint.baseline_files
            else:
                before = _snapshot_files(eff_work_dir)
                checkpoint.baseline_files = before
            if images:
                blocks: list = [{"type": "text", "text": prompt}]
                for img in images:
                    blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img["mime"],
                            "data": img["data"],
                        },
                    })
                msg = Msg(name="user", content=blocks, role="user")
            else:
                msg = Msg(name="user", content=prompt, role="user")
            if not resume:
                # 恢复不是一次新的用户输入：不重复触发 UserPromptSubmit
                await hook_runner.fire("UserPromptSubmit", payload={"message": goal})
            result = await agent(msg)

            # 压缩是否真的发生，以记忆里的摘要为准；不说「已压缩」除非确实有摘要
            if getattr(agent.memory, "_compressed_summary", ""):
                compress_note = (
                    "本轮已触发上下文压缩：较早消息已折叠为摘要（压缩时额外调用了一次模型），"
                    "后续步骤只带摘要与最近消息。"
                )
                _safe_put(queue, "step_start", {
                    "step_id": "ctx-compress",
                    "stepType": "context",
                    "description": compress_note,
                })
                _safe_put(queue, "step_done", {"step_id": "ctx-compress", "output": compress_note})
            compress_error = str(getattr(agent, "_tt_compression_error", "") or "")
            counter = getattr(agent, "_tt_token_counter", None)
            count_error = str(getattr(counter, "last_error", "") or "")
            if compress_error or count_error:
                reason = compress_error or f"token 计数失败：{count_error}"
                compress_note = f"上下文压缩本轮未生效：{reason}；任务继续使用未压缩上下文完成。"
                _safe_put(queue, "step_start", {
                    "step_id": "ctx-compress-error",
                    "stepType": "context",
                    "description": compress_note,
                })
                _safe_put(queue, "step_done", {
                    "step_id": "ctx-compress-error", "output": compress_note,
                })

            # 回填真实执行步骤：从 memory 提取工具调用，展示多步执行过程
            try:
                mem_msgs = await agent.memory.get_memory()
            except Exception:
                mem_msgs = []
            step_i = 1
            for m in mem_msgs or []:
                content = getattr(m, "content", None)
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            step_i += 1
                            tool_name = str(block.get("name", ""))
                            _safe_put(queue, "step_start", {
                                "step_id": f"step-{step_i}",
                                "stepType": "tool_call",
                                # tool 供 IM 回贴的「执行过程」直接取用，不让消费方解析中文描述
                                "tool": tool_name,
                                "description": f"调用工具: {tool_name}",
                            })
                            _safe_put(queue, "step_done", {
                                "step_id": f"step-{step_i}",
                                "output": f"工具 {block.get('name', '')} 执行完成",
                            })
            _safe_put(queue, "step_done", {
                "step_id": "step-1",
                "output": _text_of(getattr(result, "content", ""))[:300],
            })

            # 真实产出物：工作目录内新增/变更文件（排除会话记录目录）
            artifacts = _diff_files(
                eff_work_dir, before, _snapshot_files(eff_work_dir),
                exclude_dir=session.session_dir,
            )
            if isolated:
                # 隔离目录可被整体清理，产物先归档进会话目录再上报
                artifacts = _preserve_artifacts_in_session(
                    session.session_dir, eff_work_dir, artifacts
                )
            final_text = _text_of(getattr(result, "content", ""))
            # 自动记忆：只在成功收尾时追加一条可审计摘要；开关/凭据/条数上限
            # 未通过时不写，但把原因作为步骤事件摊出来（不静默少写）。
            _memory_ok, memory_note = _auto_memory_after_success(
                session.runtime, goal, final_text, artifacts,
                privacy=session.privacy_mode,
            )
            if memory_note:
                _safe_put(queue, "step_start", {
                    "step_id": "memory-auto",
                    "stepType": "context",
                    "description": memory_note,
                })
                _safe_put(queue, "step_done", {"step_id": "memory-auto", "output": memory_note})
            _safe_put(queue, "deliver", {
                "summary": "任务完成",
                "artifacts": artifacts,
                "final_text": final_text,
            })
            # Stop：脚本可以 exit 2 表达「别停，继续」；能否续跑由下面的说明如实交代
            await hook_runner.fire("Stop", payload={"reason": "completed"})
        except asyncio.CancelledError:
            end_reason = "cancelled"
            end_note = "用户取消了任务，可从最近一次模型调用前的检查点继续"
            # 取消路径（POST /api/chat/cancel）：cancelled 状态事件后正常收尾
            _safe_put(queue, "status_change", {
                "taskId": task_id,
                "status": "cancelled",
                "statusReasonCode": "USER_CANCELLED",
            })
        except pg.PrivacyBlocked as exc:
            # 出站脱敏档位为 block：命中后不再发下一次模型调用，直接中止本轮
            end_reason = "privacy_blocked"
            end_note = str(exc)
            _safe_put(queue, "error", {
                "code": "PRIVACY_OUTBOUND_BLOCKED",
                "message": str(exc),
            })
        except Exception as exc:
            end_reason = "error"
            end_note = f"执行中断：{exc}"
            traceback.print_exc()
            _safe_put(queue, "error", {"code": "AGENT_ERROR", "message": str(exc)})
        finally:
            # 连接器子进程随本轮结束退出（必须在这里，与 attach 同任务）
            await mr.close_all(mcp_clients)
            try:
                # 取消路径下这个 await 可能被打断：尽力补一条 SessionEnd，失败不拖累收尾
                await hook_runner.fire("SessionEnd", payload={"reason": end_reason})
            except BaseException:  # noqa: BLE001
                pass
            # 收尾判定检查点：正常完成不保留，可重试的中断保留并如实说明
            if end_reason == "completed":
                checkpoint.mark_final(False, "任务已正常完成，无需续跑")
            elif end_reason == "cancelled":
                checkpoint.mark_final(
                    True, end_note or "用户取消了任务，可从最近一次模型调用前的检查点继续"
                )
            elif end_reason == "privacy_blocked":
                # 不改档位重试仍会被拦：不把这次中止伪装成「可续跑失败」
                checkpoint.mark_final(False, end_note or "出站内容命中敏感信息，已中止本次执行")
            else:
                checkpoint.mark_final(
                    True, end_note or "执行中断，可从最近一次模型调用前的检查点继续"
                )
            # 可续跑收尾写盘（正常完成删除旧文件）；写盘失败只降级为进程内，
            # final_event 的 scope 与原因会如实反映，不让界面承诺刷新后还能继续。
            try:
                await asyncio.shield(asyncio.to_thread(_persist_checkpoint, session))
            except BaseException as exc:  # noqa: BLE001
                checkpoint.scope = "same-process"
                checkpoint.disk_error = str(exc)
            # 先发 checkpoint 再发 done：前端收到即可决定是否显示「从检查点继续」
            _safe_put(queue, "checkpoint", checkpoint.final_event())
            _safe_put(queue, "done", {})

    task = asyncio.create_task(run_agent())
    session.agent_task = task

    last_output = time.monotonic()
    try:
        while True:
            try:
                # 批量 drain：取到一条后一次性清空队列，降低高吞吐下循环开销
                event, data = await asyncio.wait_for(queue.queue.get(), timeout=0.5)
                batch = [(event, data)]
                while True:
                    try:
                        batch.append(queue.queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                for ev, dt in batch:
                    # 产出物落盘到会话子目录并重写路径；全部事件追加写入会话记录
                    if ev == "deliver":
                        dt = _materialize_artifacts(session.session_dir, dt)
                    yield sse(ev, dt)
                    _log_conversation(
                        session.session_dir,
                        f"[{time.strftime('%H:%M:%S')}][{ev}] {json.dumps(dt, ensure_ascii=False)[:500]}",
                    )
                    last_output = time.monotonic()
                    if ev == "done":
                        return
            except asyncio.TimeoutError:
                if task.done():
                    # 兜底：任务结束但队列已空
                    yield sse("done", {})
                    return
                # keepalive 心跳注释行（防空闲断连，不影响事件 seq）
                if time.monotonic() - last_output >= KEEPALIVE_INTERVAL_SECONDS:
                    yield ": keepalive\n\n"
                    last_output = time.monotonic()
    finally:
        session.exec_queue = None

# ── agentscope 组件构建 ────────────────────────────────────────

SYS_PROMPT = """你是“通天办公”AI 工作助手，运行在用户的本地电脑上。

工作方式:
1. 分析用户目标，必要时先查看相关文件再动手
2. 使用工具完成任务（读写文件、执行代码等）
3. 涉及删除或覆盖文件前务必谨慎，优先保留原内容
4. 完成后用简洁中文总结做了什么、产出了哪些文件

约束:
- 只在当前工作目录范围内操作文件
- 不执行危险系统命令
- 用户要求创建/生成/保存文件时，必须调用文件写入工具真实写入，禁止仅在回复中声称已写入
- 声称任何文件操作完成前，必须先确认对应工具调用已成功执行
"""


def _effective_model_name(model_override: str | None = None, model_config: dict | None = None) -> str:
    """本轮真正使用的模型名（模型档位 > 自定义模型 > .env 默认）"""
    cfg = model_config or {}
    return (
        (model_override or "").strip()
        or str(cfg.get("modelName") or "").strip()
        or os.environ.get("TT_MODEL", "qwen-plus")
    )


# 视觉能力判定：厂商没有统一的能力位，只能按已知的多模态系列名匹配。
# 宁可保守——误判为「有视觉」会把整轮调用打成 400；误判为「无视觉」只是模型
# 看不到图，界面上会有一条明确说明，用户还能用 TT_VISION=on 覆盖。
_VISION_NAME_HINTS = (
    "-vl", "vl-", "vision", "visual", "multimodal",
    "gpt-4o", "gpt-4.1", "gpt-4-turbo", "gpt-5", "o1", "o3", "o4",
    "claude-3", "claude-4", "claude-sonnet", "claude-opus", "claude-haiku",
    "gemini", "glm-4v", "glm-4.5v", "glm-4.6", "llava", "internvl",
    "pixtral", "minicpm-v", "yi-vl", "doubao-vision", "step-1v", "ernie-4.5-vl",
)


def vision_status(model_override: str | None = None, model_config: dict | None = None) -> tuple[bool, str]:
    """返回 (是否把图片作为 ImageBlock 下发, 给用户看的原因)。

    TT_VISION=on/off 显式覆盖；缺省 auto 时按模型名启发式判定。
    原因会写进 issue 说明（用户可在执行过程里看到为什么图片没进模型）。
    """
    name = _effective_model_name(model_override, model_config)
    override = os.environ.get("TT_VISION", "auto").strip().lower()
    if override == "on":
        return True, f"TT_VISION=on：按视觉模型下发图片（{name}）"
    if override == "off":
        return False, f"TT_VISION=off：图片不进入模型（{name}）"
    lowered = name.lower()
    if any(hint in lowered for hint in _VISION_NAME_HINTS):
        return True, f"按模型名判定为视觉模型（{name}）"
    return False, f"模型「{name}」未匹配视觉模型名（TT_VISION=on 可强制尝试）"


def _model_kind(model_config: dict | None) -> str:
    """本轮主模型的接入方式：自定义端点 / 环境 DashScope / 环境 OpenAI 兼容"""
    cfg = model_config if isinstance(model_config, dict) else {}
    if str(cfg.get("baseUrl") or "").strip():
        return "custom"
    if os.environ.get("TT_PROVIDER", "dashscope").lower() == "dashscope":
        return "dashscope"
    return "openai"


def _build_chat_model(model_name: str, api_key: str, base_url: str,
                      kind: str, stream: bool):
    """按接入方式构造模型与匹配的 formatter（主模型与转移目标共用）"""
    from agentscope.model import DashScopeChatModel, OpenAIChatModel
    from agentscope.formatter import DashScopeChatFormatter, OpenAIChatFormatter

    if kind == "custom":
        model = OpenAIChatModel(
            model_name=model_name,
            api_key=api_key or "EMPTY",
            stream=stream,
            client_kwargs={"base_url": base_url},
        )
        return model, OpenAIChatFormatter()
    if kind == "dashscope":
        enable_search = os.environ.get("TT_ENABLE_SEARCH", "true").lower() != "false"
        model = DashScopeChatModel(
            model_name=model_name,
            api_key=api_key,
            stream=stream,
            enable_search=enable_search,
        )
        return model, DashScopeChatFormatter()
    model = OpenAIChatModel(
        model_name=model_name,
        api_key=api_key or "EMPTY",
        stream=stream,
        client_kwargs={"base_url": base_url} if base_url else {},
    )
    return model, OpenAIChatFormatter()


def _usable_fallbacks(model_config: dict | None, kind: str) -> list[dict]:
    """筛选可共用同一 formatter 的转移目标：接入方式必须与主模型一致

    Rust/引擎装配侧已经滤过一轮（并写进 fallbackNotes）；这里再兜一层，
    防止浏览器直连或旧配置塞进跨接入方式的目标后，用错 formatter 把请求打废。
    """
    cfg = model_config if isinstance(model_config, dict) else {}
    raw = cfg.get("fallbacks")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw[:3]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("modelName") or "").strip()
        if not name:
            continue
        base = str(item.get("baseUrl") or "").strip()
        if kind == "custom" and not base:
            print(f"[engine] 转移目标「{name}」缺少 baseUrl，已跳过", flush=True)
            continue
        if kind != "custom" and base:
            print(
                f"[engine] 转移目标「{name}」接入方式与主模型不同，已跳过",
                flush=True,
            )
            continue
        out.append(item)
    return out


def build_model_and_formatter(model_override: str | None = None, model_config: dict | None = None,
                              stream: bool = True, on_usage=None,
                              pii_mode: str = "off", on_redaction=None,
                              on_route_switch=None):
    """按 .env 配置构建模型与 formatter（支持 DashScope 与 OpenAI 兼容端点）；
    model_override 非空时覆盖模型名（对话内模型档位绑定）；
    model_config 为前端传入的自定义模型配置（models.json，含 baseUrl/apiKey/modelName/
    prices/fallbacks），存在 baseUrl 时走 OpenAI 兼容端点真实调用自定义模型；
    stream=False 用于「一次性取值」场景（计划生成按完整响应解析 JSON）；
    on_usage(prompt, completion, model_name, latency_ms, cost_micros, currency, priced)
    在每次真实模型调用后回调；
    on_route_switch({from, to, reason}) 在主模型失败、切到备用模型时回调；
    pii_mode/on_redaction 为出站脱敏档位与命中回调（包装在最外层，覆盖每次模型调用）"""
    cfg = model_config or {}
    kind = _model_kind(model_config)
    custom_base = str(cfg.get("baseUrl") or "").strip()
    custom_key = str(cfg.get("apiKey") or "").strip()
    env_key = os.environ.get("TT_API_KEY", "")

    model_name = _effective_model_name(model_override, model_config)
    api_key = custom_key or env_key

    if kind != "custom" and not api_key:
        # 引导去设置页配自定义模型（models.json 随请求 modelConfig 下发）；安装版没有
        # "项目根 .env" 这个概念，.env 只是开发/内网联调时的兜底通道。
        raise RuntimeError("未配置可用模型：请在「设置 → 模型与用量」中添加模型（或配置 TT_API_KEY 兜底）")

    base_url = custom_base if kind == "custom" else (
        os.environ.get("TT_BASE_URL", "") if kind == "openai" else ""
    )
    primary, formatter = _build_chat_model(model_name, api_key, base_url, kind, stream)
    candidates = [
        _with_usage_tracking(primary, on_usage, _prices_of(model_config))
    ]
    for item in _usable_fallbacks(model_config, kind):
        fb_name = str(item.get("modelName") or "").strip()
        fb_base = str(item.get("baseUrl") or "").strip() if kind == "custom" else base_url
        fb_key = str(item.get("apiKey") or "").strip() or (api_key if kind != "custom" else "")
        fb_model, _ = _build_chat_model(fb_name, fb_key, fb_base, kind, stream)
        candidates.append(_with_usage_tracking(fb_model, on_usage, _prices_of(item)))

    model = (
        candidates[0]
        if len(candidates) == 1
        else RoutingChatModel(candidates, on_switch=on_route_switch)
    )
    return pg.wrap_model(model, pii_mode, on_redaction=on_redaction), formatter


try:
    from agentscope.model import ChatModelBase
except Exception:  # agentscope 缺失时降级为本地基类，仅影响用量采集包装
    class ChatModelBase:  # type: ignore[no-redef]
        """agentscope 缺失时的占位基类（model_name/stream 语义一致）"""

        def __init__(self, model_name: str = "", stream: bool = True) -> None:
            self.model_name = model_name
            self.stream = stream


class UsageTrackingModel(ChatModelBase):
    """模型包装层：把每次真实调用的 token 用量、延迟与成本记入台账并回调。

    agentscope 的 ReActAgent 只依赖 `stream` 属性与 `__call__`（含 `stream` 的
    运行时改写），故包装层透传这两者即可，不改动内核行为。

    流式响应的 usage 仅在末尾 chunk 出现（DashScope 末 chunk / OpenAI
    stream_options.include_usage），故按「最后一次非空 usage」计一次，
    避免逐 chunk 重复累计；延迟按整段生成耗时计算。
    """

    def __init__(self, inner, on_usage=None, prices: dict | None = None):
        self._inner = inner  # 必须先于 super().__init__：stream setter 需要它
        super().__init__(model_name=inner.model_name, stream=inner.stream)
        self._on_usage = on_usage
        self._prices = prices if isinstance(prices, dict) else None

    @property
    def stream(self) -> bool:
        return self._inner.stream

    @stream.setter
    def stream(self, value: bool) -> None:
        self._inner.stream = value

    def _record(self, usage, latency_ms: int) -> None:
        if usage is None:
            return
        prompt, completion = usage
        cost_micros, currency = cost_micros_of(self._prices, prompt, completion)
        priced = cost_micros is not None
        principal = current_principal() or {}
        row = USAGE_LEDGER.add(
            self.model_name, prompt, completion,
            latency_ms=latency_ms, cost_micros=cost_micros,
            currency=currency, priced=priced,
            sub=principal.get("sub"), org=principal.get("org"),
        )
        if self._on_usage is not None:
            try:
                self._on_usage(
                    prompt, completion, self.model_name,
                    latency_ms, cost_micros, currency, priced,
                )
            except Exception as exc:  # 回调失败不影响模型调用链
                print(f"[engine] usage 回调失败: {exc}", flush=True)
        cost_text = (
            f" cost={cost_micros / 1_000_000:.6f}{currency}" if priced else " cost=未配置单价"
        )
        print(
            f"[engine] usage model={self.model_name} +{prompt}/+{completion} "
            f"{latency_ms}ms{cost_text} "
            f"累计={row['promptTokens'] + row['completionTokens']} calls={row['calls']}",
            flush=True,
        )

    async def __call__(self, *args, **kwargs):
        started = time.monotonic()
        res = await self._inner(*args, **kwargs)
        if inspect.isasyncgen(res):
            return self._wrap_stream(res, started)
        self._record(_usage_of(res), int((time.monotonic() - started) * 1000))
        return res

    async def _wrap_stream(self, agen, started: float):
        last = None
        async for chunk in agen:
            parsed = _usage_of(chunk)
            if parsed is not None:
                last = parsed
            yield chunk
        self._record(last, int((time.monotonic() - started) * 1000))


def _with_usage_tracking(model, on_usage, prices: dict | None = None):
    """统一挂载用量采集（无 agentscope 基类可用时原样返回，保证不阻断）"""
    try:
        return UsageTrackingModel(model, on_usage, prices=prices)
    except Exception as exc:
        print(f"[engine] 用量采集包装跳过: {exc}", flush=True)
        return model


class ModelRouteError(RuntimeError):
    """所有候选模型都失败：保留每次尝试的原因，避免只报最后一个错误"""

    def __init__(self, failures: list[dict]):
        self.failures = failures
        detail = "；".join(
            f"{item['model']}: {item['error']}" for item in failures
        )
        super().__init__(
            f"所有候选模型均调用失败（{len(failures)} 个）：{detail}"
        )


def _short_error(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return (text or type(exc).__name__)[:240]


class RoutingChatModel(ChatModelBase):
    """按顺序尝试候选模型，主模型失败时切到下一个。

    切换只允许发生在「还没有向调用方吐出任何 chunk」之前：流式响应一旦已经
    输出半截，再换模型会把两个模型的内容拼在一起，调用方拿到的是看似完整、
    实际自相矛盾的回复。此时直接把原错误抛出，让用户看到真实失败。

    候选模型各自包了 UsageTrackingModel（模型名与单价不同），路由层不重复记账；
    每次切换通过 `on_switch` 回调暴露给上层，转成用户可见的上下文说明。
    """

    def __init__(self, candidates: list, on_switch=None):
        if not candidates:
            raise ValueError("RoutingChatModel 至少需要一个候选模型")
        self._candidates = list(candidates)
        self._on_switch = on_switch
        super().__init__(
            model_name=self._candidates[0].model_name,
            stream=self._candidates[0].stream,
        )

    @property
    def stream(self) -> bool:
        return self._candidates[0].stream

    @stream.setter
    def stream(self, value: bool) -> None:
        # ReActAgent 可能在运行中改写 stream：所有候选必须保持同一种调用形态
        for candidate in self._candidates:
            candidate.stream = value

    @property
    def candidate_names(self) -> list[str]:
        return [candidate.model_name for candidate in self._candidates]

    def _notify_switch(self, index: int, exc: BaseException) -> None:
        if index + 1 >= len(self._candidates):
            return
        failed = self._candidates[index]
        nxt = self._candidates[index + 1]
        info = {
            "from": failed.model_name,
            "to": nxt.model_name,
            "reason": _short_error(exc),
        }
        print(
            f"[engine] 模型故障转移 {info['from']} → {info['to']}：{info['reason']}",
            flush=True,
        )
        if self._on_switch is not None:
            try:
                self._on_switch(info)
            except Exception as cb_exc:  # 回调失败不影响路由
                print(f"[engine] 故障转移回调失败: {cb_exc}", flush=True)

    async def __call__(self, *args, **kwargs):
        failures: list[dict] = []
        for index, candidate in enumerate(self._candidates):
            try:
                res = await candidate(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — 任何候选失败都要能落到下一个
                failures.append({"model": candidate.model_name, "error": _short_error(exc)})
                self._notify_switch(index, exc)
                continue
            if not inspect.isasyncgen(res):
                return res
            try:
                first = await res.__anext__()
            except StopAsyncIteration:
                return self._empty_stream()
            except Exception as exc:  # noqa: BLE001
                failures.append({"model": candidate.model_name, "error": _short_error(exc)})
                self._notify_switch(index, exc)
                continue
            return self._forward_stream(res, first)
        raise ModelRouteError(failures)

    async def _forward_stream(self, agen, first):
        yield first
        async for chunk in agen:
            yield chunk

    async def _empty_stream(self):
        # 空响应不切换到下一个模型：模型确实返回了空结果，不是连接失败
        if False:  # pragma: no cover — 生成器语法占位
            yield None


# ── 幻灯片模板（runtime.pptxTemplates） ───────────────────────

_PPTX_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_PPTX_MAX_TEMPLATE_BYTES = 20 * 1024 * 1024
_DEFAULT_PPTX_THEME = {
    "background": "#FFFFFF", "foreground": "#1F2937", "accent": "#2563EB",
    "muted": "#64748B", "surface": "#EFF6FF",
    "titleFont": "Microsoft YaHei", "bodyFont": "Microsoft YaHei",
    "titleSize": 40, "bodySize": 18, "margin": 0.7, "accentBar": True,
}
_DEFAULT_PPTX_TEMPLATE = {
    "id": "business-blue", "name": "商务蓝", "builtin": True,
    "available": True, "theme": _DEFAULT_PPTX_THEME,
}


def _pptx_theme(raw) -> dict:
    """把 runtime 下发的主题规格归一成可安全用于 python-pptx 的值。

    颜色只接受 #RRGGBB，字号/边距限界，字体名去掉控制字符；
    非法值逐项回退默认，不因一个坏字段让整份演示文稿生成失败。
    """
    theme = dict(_DEFAULT_PPTX_THEME)
    if not isinstance(raw, dict):
        return theme
    for key in ("background", "foreground", "accent", "muted", "surface"):
        v = raw.get(key)
        if isinstance(v, str) and _PPTX_HEX.match(v.strip()):
            theme[key] = v.strip()
    for key in ("titleFont", "bodyFont"):
        v = raw.get(key)
        if (isinstance(v, str) and v.strip() and len(v.strip()) <= 64
                and not any(ord(c) < 32 for c in v)):
            theme[key] = v.strip()
    for key, lo, hi in (("titleSize", 16, 72), ("bodySize", 10, 32)):
        n = raw.get(key)
        if isinstance(n, (int, float)) and lo <= n <= hi:
            theme[key] = int(n)
    m = raw.get("margin")
    if isinstance(m, (int, float)) and 0.2 <= m <= 1.5:
        theme["margin"] = float(m)
    if "accentBar" in raw:
        theme["accentBar"] = bool(raw.get("accentBar"))
    return theme


def _pptx_selected(cfg) -> dict:
    """按 selectedId 取模板；选择失效时退回内置商务蓝（调用方负责说明）。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    items = cfg.get("templates") if isinstance(cfg.get("templates"), list) else []
    sid = str(cfg.get("selectedId") or "").strip()
    chosen = next(
        (t for t in items if isinstance(t, dict) and str(t.get("id") or "") == sid),
        None,
    )
    if chosen is None:
        chosen = next(
            (t for t in items
             if isinstance(t, dict) and t.get("builtin")
             and t.get("available", True)),
            None,
        )
    return dict(chosen or _DEFAULT_PPTX_TEMPLATE)


def _pptx_template_path(cfg, tpl) -> tuple[Path | None, str | None]:
    """导入模板的路径收敛：必须是模板目录内的 .pptx 文件。

    渲染层/模型都不能指定任意路径，避免把工作目录里的文件当模板读取。
    """
    root_raw = str((cfg or {}).get("root") or "").strip()
    path_raw = str(tpl.get("path") or "").strip()
    if not root_raw or not path_raw:
        return None, "模板路径缺失（请重新导入或改选内置模板）"
    try:
        root = Path(root_raw).expanduser().resolve()
        path = Path(path_raw).expanduser().resolve()
        root_key = os.path.normcase(str(root))
        path_key = os.path.normcase(str(path))
        if os.path.commonpath([root_key, path_key]) != root_key:
            return None, "模板路径不在模板目录内"
    except (OSError, ValueError):
        return None, "模板路径不可用"
    if path.suffix.lower() != ".pptx" or not path.is_file():
        return None, "模板文件不存在或不是 .pptx"
    try:
        if path.stat().st_size > _PPTX_MAX_TEMPLATE_BYTES:
            return None, "模板文件超过 20MB 上限"
    except OSError:
        return None, "模板文件读不到"
    return path, None


def _pptx_layout_score(layout, tokens: tuple[str, ...]) -> int:
    name = str(getattr(layout, "name", "") or "").lower()
    return sum(1 for token in tokens if token in name)


def _pptx_pick_layout(prs, tokens: tuple[str, ...], fallback: int):
    layouts = list(prs.slide_layouts)
    if not layouts:
        return None
    ranked = sorted(layouts, key=lambda item: _pptx_layout_score(item, tokens), reverse=True)
    if ranked and _pptx_layout_score(ranked[0], tokens) > 0:
        return ranked[0]
    return layouts[fallback] if fallback < len(layouts) else layouts[0]


_PPTX_R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _pptx_clear_slides(prs) -> None:
    """清掉模板自带的示例页与关系，只保留母版/版式/主题。"""
    slide_ids = prs.slides._sldIdLst
    for slide_id in list(slide_ids):
        rel_id = slide_id.get(_PPTX_R_ID)
        if rel_id:
            prs.part.drop_rel(rel_id)
        slide_ids.remove(slide_id)


def _pptx_fill_text(shape, text: str, theme: dict | None, *, title: bool = False,
                    bullet: bool = False) -> None:
    from pptx.dml.color import RGBColor
    from pptx.util import Pt

    tf = shape.text_frame
    tf.clear()
    para = tf.paragraphs[0]
    para.text = f"• {text}" if bullet else str(text)
    para.level = 0
    if theme is None:
        return
    for run in para.runs:
        run.font.name = theme["titleFont" if title else "bodyFont"]
        run.font.size = Pt(theme["titleSize" if title else "bodySize"])
        if title:
            run.font.bold = True
        run.font.color.rgb = RGBColor.from_string(theme["foreground"].lstrip("#"))


def _pptx_body_shape(slide):
    title = slide.shapes.title
    title_id = title.shape_id if title is not None else None
    for ph in slide.placeholders:
        if ph.shape_id == title_id:
            continue
        try:
            if ph.placeholder_format.idx == 0:
                continue
        except Exception:
            pass
        if getattr(ph, "has_text_frame", False):
            return ph
    return None


def _pptx_fill_body(slide, bullets: list[str], theme: dict | None = None,
                    box: tuple | None = None) -> None:
    shape = _pptx_body_shape(slide)
    if shape is None and box is not None:
        shape = slide.shapes.add_textbox(*box)
    if shape is None:
        return
    tf = shape.text_frame
    tf.word_wrap = True
    tf.clear()
    if not bullets:
        tf.paragraphs[0].text = "（本页无要点）"
        return
    for index, bullet in enumerate(bullets):
        para = tf.paragraphs[0] if index == 0 else tf.add_paragraph()
        para.text = f"• {bullet}" if theme is not None else str(bullet)
        para.level = 0
        if theme is not None:
            from pptx.dml.color import RGBColor
            from pptx.util import Pt
            for run in para.runs:
                run.font.name = theme["bodyFont"]
                run.font.size = Pt(theme["bodySize"])
                run.font.color.rgb = RGBColor.from_string(theme["foreground"].lstrip("#"))


def _pptx_add_notes(slide, text: str) -> None:
    note = str(text or "").strip()
    if note:
        slide.notes_slide.notes_text_frame.text = note


def _pptx_render_builtin(prs, title: str, pages: list, theme: dict) -> None:
    """内置主题没有实体模板文件，用配色/字体/版式参数直接绘制。"""
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches, Pt

    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    layouts = list(prs.slide_layouts)
    blank = layouts[6] if len(layouts) > 6 else layouts[0]

    bg = RGBColor.from_string(theme["background"].lstrip("#"))
    accent = RGBColor.from_string(theme["accent"].lstrip("#"))
    muted = RGBColor.from_string(theme["muted"].lstrip("#"))
    margin = Inches(theme["margin"])
    width = prs.slide_width - 2 * margin

    def paint_background(slide) -> None:
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = bg

    def accent_bar(slide) -> None:
        if not theme["accentBar"]:
            return
        bar = slide.shapes.add_shape(
            MSO_SHAPE.RECTANGLE, 0, 0, Inches(0.16), prs.slide_height,
        )
        bar.fill.solid()
        bar.fill.fore_color.rgb = accent
        bar.line.fill.background()

    def text_box(slide, left, top, box_width, box_height, text, size, color, bold=False):
        box = slide.shapes.add_textbox(left, top, box_width, box_height)
        tf = box.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = Inches(0.04)
        para = tf.paragraphs[0]
        para.text = str(text)
        for run in para.runs:
            run.font.name = theme["titleFont"] if bold else theme["bodyFont"]
            run.font.size = Pt(size)
            run.font.bold = bold
            run.font.color.rgb = color
        return box

    cover = prs.slides.add_slide(blank)
    paint_background(cover)
    accent_bar(cover)
    text_box(cover, margin, Inches(2.15), width, Inches(1.6),
             title or "演示文稿", theme["titleSize"], RGBColor.from_string(theme["foreground"].lstrip("#")), True)
    text_box(cover, margin, Inches(3.95), width, Inches(0.5),
             time.strftime("%Y-%m-%d"), 16, muted)
    if theme["accentBar"]:
        line = cover.shapes.add_shape(
            MSO_SHAPE.RECTANGLE, margin, Inches(3.7), Inches(1.1), Inches(0.06),
        )
        line.fill.solid()
        line.fill.fore_color.rgb = accent
        line.line.fill.background()

    for index, page in enumerate(pages, 1):
        slide = prs.slides.add_slide(blank)
        paint_background(slide)
        accent_bar(slide)
        text_box(slide, margin, Inches(theme["margin"] + 0.15), width, Inches(0.9),
                 page.get("title") or f"第 {index} 页", theme["titleSize"] - 8,
                 RGBColor.from_string(theme["foreground"].lstrip("#")), True)
        if theme["accentBar"]:
            line = slide.shapes.add_shape(
                MSO_SHAPE.RECTANGLE, margin, Inches(theme["margin"] + 1.05),
                Inches(1.0), Inches(0.05),
            )
            line.fill.solid()
            line.fill.fore_color.rgb = accent
            line.line.fill.background()
        bullets = page.get("bullets") or []
        _pptx_fill_body(slide, bullets, theme, box=(
            margin, Inches(theme["margin"] + 1.35), width,
            prs.slide_height - Inches(theme["margin"] + 1.35) - Inches(0.55),
        ))
        text_box(slide, prs.slide_width - margin - Inches(0.6), prs.slide_height - Inches(0.5),
                 Inches(0.6), Inches(0.3), str(index), 11, muted)
        _pptx_add_notes(slide, page.get("notes"))


def _pptx_render_imported(prs, title: str, pages: list, fallback_theme: dict) -> None:
    """打开用户模板：清示例页，按模板母版/版式填标题与正文。"""
    from pptx.util import Inches

    _pptx_clear_slides(prs)
    cover_layout = _pptx_pick_layout(
        prs, ("title slide", "cover", "封面", "标题幻灯片"), 0,
    )
    content_layout = _pptx_pick_layout(
        prs, ("title and content", "标题和内容", "content", "正文", "内容"), 1,
    )
    cover = prs.slides.add_slide(cover_layout)
    if cover.shapes.title is not None:
        _pptx_fill_text(cover.shapes.title, title or "演示文稿", None, title=True)
    else:
        _pptx_fill_text(
            cover.shapes.add_textbox(
                Inches(fallback_theme["margin"]), Inches(2.0),
                prs.slide_width - Inches(fallback_theme["margin"] * 2), Inches(1.4),
            ),
            title or "演示文稿", fallback_theme, title=True,
        )
    for ph in cover.placeholders:
        if cover.shapes.title is not None and ph.shape_id == cover.shapes.title.shape_id:
            continue
        if getattr(ph, "has_text_frame", False):
            ph.text_frame.text = time.strftime("%Y-%m-%d")
            break

    margin = Inches(fallback_theme["margin"])
    box_width = prs.slide_width - 2 * margin
    for index, page in enumerate(pages, 1):
        slide = prs.slides.add_slide(content_layout)
        heading = page.get("title") or f"第 {index} 页"
        if slide.shapes.title is not None:
            _pptx_fill_text(slide.shapes.title, heading, None, title=True)
        else:
            _pptx_fill_text(
                slide.shapes.add_textbox(margin, Inches(fallback_theme["margin"]),
                                         box_width, Inches(0.9)),
                heading, fallback_theme, title=True,
            )
        bullets = page.get("bullets") or []
        body_box = (
            margin, prs.slide_height * 0.28, box_width, prs.slide_height * 0.62,
        )
        if _pptx_body_shape(slide) is None:
            _pptx_fill_body(slide, bullets, fallback_theme, box=body_box)
        else:
            _pptx_fill_body(slide, bullets)
        _pptx_add_notes(slide, page.get("notes"))


def build_toolkit(work_dir: Path, sandbox: "sr.SandboxPolicy | None" = None, slides: bool = False,
                  grants: "tg.ToolGrantPolicy | None" = None, emit=None, task_id: str = "",
                  pptx_templates: dict | None = None):
    """注册文件操作工具（自实现：路径限定在工作目录内，相对路径自动解析到工作目录）

    sandbox 存在时按工作区沙箱策略校验路径（黑名单优先；L0 拒绝写；L2 放开目录限制）。
    slides=True（幻灯片模式）时额外注册 generate_pptx：产出真实 .pptx，而不是文本改名。
    grants 是用户在工作区里的执行/联网显式授权：未授权或白名单为空时工具**不注册**。
    """
    from agentscope.tool import Toolkit, ToolResponse
    from agentscope.message import TextBlock

    base = work_dir.resolve()

    def _resolve(path: str) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = base / p
        return p.resolve()

    def _guard(p: Path, write: bool = False) -> str | None:
        """沙箱/目录越界检查，返回错误文案或 None"""
        if sandbox is not None:
            denial = sandbox.file_denial(p, write=write, base=base)
            if denial is not None:
                return f"错误：沙箱拒绝访问 {p}：{denial[0]}"
            if sandbox.configured:
                # 策略已生效：边界由策略判定（L0/L1 限定工作目录与白名单，L2 刻意放开）
                return None
        # 未配置策略时保留原有工作目录兜底
        try:
            p.relative_to(base)
        except ValueError:
            return f"错误：路径超出工作目录范围: {p}"
        return None

    def view_text_file(file_path: str, start_line: int | None = None, end_line: int | None = None):
        """查看工作目录内的文本文件内容。

        Args:
            file_path (`str`):
                文件路径（相对路径会解析到工作目录内）。
            start_line (`int`, optional):
                起始行号（从 1 开始），缺省为文件开头。
            end_line (`int`, optional):
                结束行号（含），缺省为文件末尾。
        """
        p = _resolve(file_path)
        err = _guard(p)
        if err:
            return ToolResponse(content=[TextBlock(type="text", text=err)])
        if not p.exists() or not p.is_file():
            return ToolResponse(content=[TextBlock(type="text", text=f"错误：文件不存在: {file_path}")])
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        s = max(0, (start_line or 1) - 1)
        e = end_line or len(lines)
        return ToolResponse(content=[TextBlock(type="text", text="\n".join(lines[s:e]))])

    def write_text_file(file_path: str, content: str):
        """创建或覆盖工作目录内的文本文件（写入前自动创建父目录）。

        Args:
            file_path (`str`):
                文件路径（相对路径会解析到工作目录内）。
            content (`str`):
                要写入的完整文件内容。
        """
        p = _resolve(file_path)
        err = _guard(p, write=True)
        if err:
            return ToolResponse(content=[TextBlock(type="text", text=err)])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return ToolResponse(content=[TextBlock(type="text", text=f"已成功写入文件: {p}")])

    def insert_text_file(file_path: str, content: str, line: int | None = None):
        """向工作目录内的文本文件插入内容（不指定行号则追加到末尾）。

        Args:
            file_path (`str`):
                文件路径（相对路径会解析到工作目录内）。
            content (`str`):
                要插入的内容。
            line (`int`, optional):
                插入到的行号（从 1 开始），缺省追加到文件末尾。
        """
        p = _resolve(file_path)
        err = _guard(p, write=True)
        if err:
            return ToolResponse(content=[TextBlock(type="text", text=err)])
        if not p.exists():
            return ToolResponse(content=[TextBlock(type="text", text=f"错误：文件不存在: {file_path}")])
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        new_lines = content.splitlines()
        if line is None:
            lines.extend(new_lines)
        else:
            idx = max(0, min(line - 1, len(lines)))
            lines[idx:idx] = new_lines
        p.write_text("\n".join(lines), encoding="utf-8")
        return ToolResponse(content=[TextBlock(type="text", text=f"已成功更新文件: {p}")])

    def generate_pptx(file_path: str, title: str, slides_json: str):
        """按当前幻灯片模板生成真实的 PowerPoint 演示文稿（.pptx）。

        Args:
            file_path (`str`):
                输出路径，必须以 .pptx 结尾（相对路径会解析到工作目录内）。
            title (`str`):
                演示文稿标题（封面页）。
            slides_json (`str`):
                内容页数组的 JSON 字符串，每项形如
                {"title": "页标题", "bullets": ["要点1", "要点2"], "notes": "演讲备注"}。
                bullets 每页最多 8 条，超出会被截断并在返回里说明。
        """
        from pptx import Presentation

        p = _resolve(file_path)
        if p.suffix.lower() != ".pptx":
            return ToolResponse(content=[TextBlock(
                type="text", text=f"错误：演示文稿路径必须以 .pptx 结尾（收到 {p.name}）"
            )])
        err = _guard(p, write=True)
        if err:
            return ToolResponse(content=[TextBlock(type="text", text=err)])
        try:
            parsed = json.loads(slides_json or "[]")
        except Exception as exc:
            return ToolResponse(content=[TextBlock(
                type="text", text=f"错误：slides_json 不是合法 JSON：{exc}"
            )])
        if isinstance(parsed, dict):
            parsed = parsed.get("slides") or []
        if not isinstance(parsed, list) or not parsed:
            return ToolResponse(content=[TextBlock(
                type="text", text="错误：slides_json 必须是非空的页数组"
            )])

        notes: list[str] = []
        pages_raw = parsed[:40]
        if len(parsed) > len(pages_raw):
            notes.append(f"页数超过上限 40，已忽略后 {len(parsed) - len(pages_raw)} 页")
        pages: list[dict] = []
        for i, page in enumerate(pages_raw, 1):
            if isinstance(page, str):
                page = {"title": page, "bullets": []}
            if not isinstance(page, dict):
                notes.append(f"第 {i} 页不是对象，已跳过")
                continue
            bullets = page.get("bullets") or []
            if isinstance(bullets, str):
                bullets = [bullets]
            bullets = [str(b).strip() for b in bullets if str(b).strip()]
            if len(bullets) > 8:
                bullets = bullets[:8]
                notes.append(f"第 {i} 页要点超过 8 条，已截断")
            pages.append({
                "title": str(page.get("title") or f"第 {i} 页"),
                "bullets": bullets,
                "notes": str(page.get("notes") or "").strip(),
            })

        # 模板选择来自设置页（Rust 从 settings_kv 读取后随 runtime 下发）；
        # 浏览器直连按请求顶层 pptxTemplates 下发；两者都没有时用内置商务蓝。
        tpl = _pptx_selected(pptx_templates)
        template_label = str(tpl.get("name") or tpl.get("id") or "商务蓝")
        builtin = bool(tpl.get("builtin")) or not str(tpl.get("path") or "").strip()
        theme = _pptx_theme(tpl.get("theme"))
        if not builtin and not tpl.get("available", True):
            notes.append(f"模板「{template_label}」的文件不在模板目录，已改用内置「商务蓝」")
            tpl = dict(_DEFAULT_PPTX_TEMPLATE)
            template_label = "商务蓝"
            theme = _pptx_theme(tpl.get("theme"))
            builtin = True

        prs = None
        if not builtin:
            tpl_path, why = _pptx_template_path(pptx_templates, tpl)
            if why:
                notes.append(f"模板「{template_label}」不可用：{why}；已改用内置「商务蓝」")
                template_label = "商务蓝"
                builtin = True
            else:
                try:
                    prs = Presentation(str(tpl_path))
                    _pptx_render_imported(prs, str(title or "演示文稿"), pages, theme)
                except Exception as exc:
                    notes.append(
                        f"模板「{template_label}」读取失败（{exc}）；已改用内置「商务蓝」"
                    )
                    prs = None
                    template_label = "商务蓝"
                    builtin = True

        try:
            if prs is None:
                prs = Presentation()
                _pptx_render_builtin(prs, str(title or "演示文稿"), pages, theme)
            p.parent.mkdir(parents=True, exist_ok=True)
            prs.save(str(p))
        except ImportError:
            return ToolResponse(content=[TextBlock(
                type="text", text="错误：未安装 python-pptx，无法生成 .pptx（请安装 python-pptx 后重试）"
            )])
        except Exception as exc:
            return ToolResponse(content=[TextBlock(
                type="text", text=f"错误：演示文稿生成失败：{exc}"
            )])

        size_label = "16:9" if builtin else "沿用模板尺寸"
        summary = (
            f"已生成演示文稿: {p}（{len(pages) + 1} 页，{size_label}，模板：{template_label}）"
        )
        if notes:
            summary += "\n说明：" + "；".join(notes)
        return ToolResponse(content=[TextBlock(type="text", text=summary)])

    toolkit = Toolkit()
    toolkit.register_tool_function(view_text_file)
    toolkit.register_tool_function(write_text_file)
    toolkit.register_tool_function(insert_text_file)
    if slides:
        toolkit.register_tool_function(generate_pptx)
    if grants is not None:
        grants.install(toolkit, base, emit=emit, task_id=task_id)
    return toolkit


def build_skill_tool(skills: list):
    """技能/套件指令正文工具工厂：注册 use_skill 让模型自己取正文。

    清单只把 name/description 放进 system prompt（装几十个技能也不撑爆上下文），
    正文按需用工具读。套件指令与技能走同一条路径，缺正文时返回安装侧原因。
    这条注册刻意留在 build_toolkit 之外：/api/tools 与
    tests/unit/extensions/builtin-tools-contract.test.ts 锁的是「内置」三件套，
    技能工具是随启停出现的运行时能力，不该混进同一张清单。
    """
    from agentscope.tool import ToolResponse
    from agentscope.message import TextBlock

    def use_skill(skill_id: str):
        """读取指定技能或专家套件指令的全文。正文里的流程、脚本位置与约束以此为准，
        不要凭名称或描述臆测做法。

        Args:
            skill_id (`str`):
                技能/指令标识，取自 system prompt 的「可用技能与套件指令」清单
                （形如 echo、pdf-report、suite:finance-suite:weekly-report）。
        """
        body = rc.skill_body_of(skills, skill_id)
        if body is None:
            ids = "、".join(s["id"] for s in skills) or "（当前没有启用的技能）"
            return ToolResponse(content=[TextBlock(
                type="text", text=f"错误：技能「{skill_id}」未启用或不存在。当前可用：{ids}"
            )])
        return ToolResponse(content=[TextBlock(type="text", text=body)])

    return use_skill


def build_agent(model_binding: str | None = None, work_dir: "Path | None" = None, model_config: dict | None = None,
                on_usage=None, runtime: dict | None = None, sandbox: "sr.SandboxPolicy | None" = None,
                mode: str = "general", extra_prompt: str = "",
                base_prompt: str | None = None, agent_name: str = "通天助手",
                max_iters: int | None = None, subagent_runner: "sar.SubagentRunner | None" = None,
                grants: "tg.ToolGrantPolicy | None" = None,
                computer: "cu.ComputerUse | None" = None,
                browser: "bu.BrowserUse | None" = None,
                knowledge: "kn.KnowledgeRuntime | None" = None,
                acp: "acpm.AcpRuntime | None" = None,
                web: "ws.WebSearchRuntime | None" = None,
                pii_mode: str = "off", on_privacy=None,
                emit=None, task_id: str = "", query: str = "",
                on_route_switch=None):
    from agentscope.agent import ReActAgent
    from agentscope.memory import InMemoryMemory

    model, formatter = build_model_and_formatter(
        model_binding, model_config, on_usage=on_usage,
        pii_mode=pii_mode, on_redaction=on_privacy,
        on_route_switch=on_route_switch,
    )
    # P1 上下文效率：单轮工具循环上限与压缩阈值来自设置页 + 模型上下文窗口。
    # 子 Agent 显式传 max_iters 时以子任务上限为准，其余口径（窗口/压缩）沿用同一套。
    # 转移链里的备用模型窗口更小时按最小的算，避免切过去后上下文装不下。
    policy_config = model_config
    fallback_window = cb.fallback_window(model_config)
    if isinstance(model_config, dict) and fallback_window is not None:
        primary_window = cb.resolve(None, model_config).context_window
        if fallback_window < primary_window:
            policy_config = {**model_config, "contextWindow": fallback_window}
    policy = cb.resolve(runtime, policy_config)
    if (
        isinstance(model_config, dict)
        and fallback_window is not None
        and policy.context_window == fallback_window
    ):
        from dataclasses import replace as _dc_replace
        policy = _dc_replace(policy, config_notes=policy.config_notes + (
            f"故障转移链里最小上下文窗口为 {fallback_window} tokens，压缩阈值按该窗口计算"
            "（保证切换到备用模型后上下文仍装得下）。",
        ))
    if max_iters is not None:
        from dataclasses import replace as _dc_replace
        policy = _dc_replace(policy, max_iters=cb.normalize_max_iters(max_iters)[0])
    model_name = (
        getattr(model, "model_name", "")
        or _effective_model_name(model_binding, model_config)
    )
    compression, compression_error = cb.build_compression(policy, model_name)
    mcp_count = tsr.mcp_server_count(runtime)
    lazy_mcp = bool(policy.lazy_mcp_tools and mcp_count > 0)
    context_notes = policy.notes(mcp_count)
    if compression_error:
        # 策略上写着「已开启」但依赖不可用时，不能两句自相矛盾
        context_notes = [
            n for n in context_notes if not n.startswith("上下文压缩已开启")
        ]
        context_notes.append(compression_error)
    # 注入当前时间：模型涉及日期/“今天、本周”等概念及生成带日期文件名时以此为准
    weekdays = "一二三四五六日"
    now = time.localtime()
    time_hint = (
        f"\n\n当前时间：{time.strftime('%Y-%m-%d %H:%M:%S', now)}，星期{weekdays[now.tm_wday]}。"
        f"涉及日期、时间或\u201c今天/本周/本月\u201d等表述时以此为准；"
        f"生成带日期的文件名时使用该日期。"
    )
    # 执行面 P0：意识（协作风格/画像/手册/记忆）与启用技能清单进 system prompt，
    # 技能正文由 use_skill 按需取；这两者是 runtime bundle 的真实消费点。
    # 工作台模式决定本轮取向（设计→HTML 原型、幻灯片→真实 pptx、写作→长文）。
    skills = rc.normalize_skills(runtime)
    # query=本轮目标：长期记忆按相关性检索，而非每轮整篇注入
    sys_prompt, _ = rc.compose_system_prompt((base_prompt or SYS_PROMPT) + time_hint, runtime, query=query)
    sys_prompt += rc.compose_mode(mode)
    if extra_prompt:
        # 负责人专家的角色设定（本轮身份与职责）；工具权限/脱敏/审批优先级更高，
        # 该约束已写在 expert_runtime.lead_system_hint 的正文里
        sys_prompt += extra_prompt
    if grants is not None:
        # 提示词与工具同时出现：没授权就不提，避免「模型知道有这能力但调不到」
        sys_prompt += grants.system_hint()
    toolkit = build_toolkit(
        work_dir or WORK_DIR, sandbox=sandbox, slides=rc.normalize_mode(mode) == "slides",
        grants=grants, emit=emit, task_id=task_id,
        pptx_templates=(runtime or {}).get("pptxTemplates")
        if isinstance(runtime, dict) else None,
    )
    if knowledge is not None and knowledge.ready():
        # 知识库清单与检索工具同进同出；不可用的库已由 notes() 摊给用户
        knowledge.install(toolkit)
        sys_prompt += knowledge.system_hint()
    if computer is not None and computer.ready:
        # 电脑操控工具与提示词同进同出；未就绪的原因已经由 notes() 摊给用户
        computer.install(toolkit)
        sys_prompt += computer.system_hint()
    if browser is not None and browser.ready:
        browser.install(toolkit, emit=emit, task_id=task_id)
        sys_prompt += browser.system_hint()
    if acp is not None and acp.ready:
        # 外部编码 Agent：启动命令只来自用户设置，模型只能给任务描述与工作子目录
        acp.install(toolkit, emit=emit, task_id=task_id)
        sys_prompt += acp.system_hint()
    if web is not None and web.ready:
        # 联网搜索与提示词同进同出；未配置/未授权的原因由 notes() 摊给用户
        web.install(toolkit, emit=emit, task_id=task_id)
        sys_prompt += web.system_hint()
    if (computer is not None and computer.ready) or (browser is not None and browser.ready):
        # 工具结果里的截图默认会被 formatter 落成临时文件 + 本地路径，
        # 远端模型看不到画面；包装一层把最近截图提升为用户消息图片
        sources = []
        if computer is not None and computer.ready:
            sources.append("电脑操控")
        if browser is not None and browser.ready:
            sources.append("浏览器")
        formatter = cu.ToolImageFormatter(
            formatter,
            lead=(
                "以下是" + " / ".join(sources) + "刚刚截取的画面（按时间从旧到新；"
                "电脑操控截图中的坐标以最后一张截图的像素为准）："
            ),
        )
    if skills:
        toolkit.register_tool_function(build_skill_tool(skills))
    # 本地子 Agent：只有父 Agent 注册派发工具；子 Agent 走 subagent_runner=None，
    # 从工具面上杜绝递归派生。提示词与工具同时出现，避免「模型知道有这能力但无工具」。
    if subagent_runner is not None:
        toolkit.register_tool_function(subagent_runner.tool())
        sys_prompt += subagent_runner.system_hint()
    if lazy_mcp:
        try:
            tsr.install(toolkit)
            sys_prompt += tsr.system_hint(mcp_count)
        except Exception as exc:  # noqa: BLE001
            # 搜索工具装不上就不能把 MCP 工具藏起来，否则模型永远够不到；
            # 同时撤掉「按需载入」说明，避免用户看到的策略与实际执行不一致
            lazy_mcp = False
            context_notes = [
                n for n in context_notes if not n.startswith("连接器工具按需载入：")
            ]
            context_notes.append(
                f"连接器工具按需载入未启用，工具将直接载入上下文：{' '.join(str(exc).split())[:200]}"
            )
    agent = ReActAgent(
        name=agent_name,
        sys_prompt=sys_prompt,
        model=model,
        formatter=formatter,
        toolkit=toolkit,
        memory=InMemoryMemory(),
        max_iters=policy.max_iters,
        compression_config=compression,
    )
    agent._tt_agent_policy = policy
    agent._tt_lazy_mcp = lazy_mcp
    agent._tt_context_notes = context_notes
    if compression is not None:
        # AgentScope 在 ReAct 循环里直接 await 压缩，异常会冒出 reply()；
        # 包装一层把「压缩失败」降级为可见说明，任务继续用未压缩上下文完成。
        original_compress = agent._compress_memory_if_needed
        agent._tt_compression_error = ""

        async def _safe_compress():
            try:
                await original_compress()
            except Exception as exc:  # noqa: BLE001
                agent._tt_compression_error = " ".join(str(exc).split())[:240]
                if getattr(agent, "compression_config", None) is not None:
                    agent.compression_config.enable = False

        agent._compress_memory_if_needed = _safe_compress
        agent._tt_token_counter = getattr(compression, "agent_token_counter", None)
    try:
        agent.set_console_output_enabled(False)
    except Exception:
        pass
    return agent


# ── hooks → SSE 事件队列 ───────────────────────────────────────

def _safe_put(queue: asyncio.Queue, event: str, data: dict) -> None:
    try:
        queue.put_nowait((event, data))
    except Exception:
        pass


class RecordingQueue:
    """生产时即分配 seq 并写入重放窗口的队列包装。

    关键语义：断线后 SSE 消费者消失，但 Agent 仍在产出事件；
    若仅在 yield 时记录，断线期间事件将丢失。故在生产时（put）
    就完成 seq 分配 + 窗口记录，保障重放完整性（事件丢失=0 目标）。
    """

    def __init__(self, session: "TaskSession"):
        self.session = session
        self.queue: asyncio.Queue = asyncio.Queue()

    def put_nowait(self, item) -> None:
        event, data = item
        seq = self.session.next_seq()
        data = {**data, "seq": seq, "taskId": self.session.task_id}
        self.session.record(seq, event, data)
        self.queue.put_nowait((event, data))


def _text_of(content) -> str:
    """从 Msg content blocks 提取纯文本"""
    if isinstance(content, str):
        return content
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def _tool_name_of(content) -> str:
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return block.get("name", "")
    return ""


def attach_hooks(agent, queue: asyncio.Queue) -> None:
    """将 Agent 回复阶段映射为执行过程事件。

    agentscope 1.0.x 仅支持 pre_reply/post_reply 等实例 hook（签名：
    register_instance_hook(hook_type, hook_name, hook)）。
    推理开始时发 step_start；工具调用步骤与完成事件由 run_agent 在
    回复结束后从 memory 回填（保证事件顺序）。"""

    def pre_reply(self, kwargs=None):
        _safe_put(queue, "step_start", {
            "step_id": "step-1",
            "stepType": "llm_call",
            "description": "模型推理与工具调用中",
        })

    try:
        agent.register_instance_hook("pre_reply", "tt_step_start", pre_reply)
    except Exception as exc:  # hook 类型不存在时降级跳过
        print(f"[engine] skip hook pre_reply: {exc}", flush=True)


# ── IM 频道：消息 → 真实任务 → 回贴 ──────────────────────────────

IM_REPLY_MAX_CHARS = 1600


def _parse_sse_chunk(chunk: str) -> tuple[str, dict]:
    """把执行流产出的 SSE 文本还原成 (事件名, data)，供 IM 回贴取终态"""
    event = ""
    data = ""
    for line in chunk.split("\n"):
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data += line[5:].strip()
    if not event:
        return "", {}
    try:
        return event, json.loads(data) if data else {}
    except Exception:
        return event, {}


def _im_reply_of(final_text: str, artifacts: list, error: str, steps: list | None = None) -> str:
    """IM 回贴正文：终态如实。失败就说失败，有产物就列产物名，不编「任务完成」

    「在 IM 频道中展示工具调用执行过程」这个开关的事实来源就是这里（设置页写的也是这里）：
    开着才把本次真实调用过的工具附在正文后，关掉只有最终回复 —— 不是界面层的显示偏好。"""
    if error:
        return f"任务失败：{error}"
    body = (final_text or "").strip() or "（模型没有产出正文）"
    if artifacts:
        names = "、".join(Path(str(a)).name for a in artifacts[:6])
        more = f" 等 {len(artifacts)} 个" if len(artifacts) > 6 else ""
        body = f"{body}\n\n产出文件：{names}{more}"
    if steps and im_channels.load_preferences().get("showToolProcess"):
        proc = "\n".join(f"{i}. 调用工具 {name}" for i, name in enumerate(steps, 1))
        body = f"{body}\n\n执行过程：\n{proc}"
    return body if len(body) <= IM_REPLY_MAX_CHARS else body[:IM_REPLY_MAX_CHARS] + "…"


async def _im_run_task(text: str, im_session: dict) -> str:
    """IM 消息 → 引擎真实执行（与桌面端同一套 _run_execution，不是 Mock）"""
    task_id = f"im-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"
    session = _get_or_create_session(task_id)
    session.goal = text
    session.history = []
    try:
        session.session_dir = _create_session_dir("", text)
        _log_conversation(session.session_dir, f"# 任务（IM 发起）：{text}")
        _log_conversation(
            session.session_dir,
            f"- 频道：{im_session.get('source')} / {im_session.get('conversationId')}"
            f"，任务 ID：{task_id}\n",
        )
    except Exception as exc:
        print(f"[engine][im] 会话目录创建失败（降级为不落盘）: {exc}", flush=True)
        session.session_dir = None

    final_text = ""
    artifacts: list = []
    error = ""
    tool_steps: list = []
    async for chunk in _run_execution(session, task_id, text, [], None, None, [], None):
        event, data = _parse_sse_chunk(chunk)
        if event == "deliver":
            final_text = str(data.get("final_text") or "")
            artifacts = list(data.get("artifacts") or [])
        elif event == "error":
            error = f"{data.get('code') or 'ENGINE_ERROR'} {data.get('message') or ''}".strip()
        elif event == "step_start" and data.get("stepType") == "tool_call":
            name = str(data.get("tool") or "").strip()
            if name:
                tool_steps.append(name)
    return _im_reply_of(final_text, artifacts, error, tool_steps)


async def _im_bootstrap() -> None:
    """引擎启动即接管各通道：注册消息处理方，已绑定过的通道自动重连。

    自动重连失败不阻断引擎启动（凭据失效/断网都会落到 state=failed 并带原因，
    界面看得到「最近错误」），所以这里只打印不抛出。"""
    for runtime in im_channels.all_runtimes():
        runtime.set_handler(_im_run_task)
        if runtime.channel.supported and runtime.configured():
            try:
                await runtime.start()
            except Exception as exc:
                print(f"[engine][im] {runtime.channel.kind} 自动重连启动失败: {exc}", flush=True)


async def _im_lifespan(_app: FastAPI):
    await _im_bootstrap()
    yield
    await im_channels.shutdown()
    try:
        TERMINALS.close_all()
    except Exception as exc:
        print(f"[engine] 终端会话清理失败: {exc}", flush=True)


# ── FastAPI 应用 ───────────────────────────────────────────────

app = FastAPI(title="tongtian-agent-engine", version=ENGINE_VERSION, lifespan=_im_lifespan)

# CORS：浏览器直连联调（开发/联调阶段）
try:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
except Exception:
    pass


# ── 服务化（可选）：Token 守卫与静态站点托管 ─────────────────
# 桌面模式不设置 TT_ENGINE_TOKEN，行为与原来完全一致。
# 服务化模式（容器/内网）必须显式设置令牌，CLI 与前置反代携带 Authorization: Bearer。

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_JWT_ISSUER = "tongtian-auth"
DEFAULT_JWT_AUDIENCE = "tongtian-engine"
JWT_ALG = "HS256"
AUDIT_MAX_ENTRIES = 2000

JWT_UNAUTHORIZED_MESSAGE = (
    "服务化模式需要携带有效的引擎令牌"
    "（Authorization: Bearer <TT_ENGINE_TOKEN 或账号服务 access token>）"
)


def _engine_jwt_secret() -> str:
    return (
        str(os.environ.get("TT_ENGINE_JWT_SECRET") or "").strip()
        or str(os.environ.get("TT_AUTH_JWT_SECRET") or "").strip()
    )


def _engine_jwt_issuer() -> str:
    return str(os.environ.get("TT_ENGINE_JWT_ISSUER") or "").strip() or DEFAULT_JWT_ISSUER


def _engine_jwt_audience() -> str:
    return str(os.environ.get("TT_ENGINE_JWT_AUDIENCE") or "").strip() or DEFAULT_JWT_AUDIENCE


def _engine_allowed_users() -> set[str]:
    raw = str(os.environ.get("TT_ENGINE_ALLOWED_USERS") or "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _b64url_decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def verify_engine_jwt(
    token: str, secret: str, issuer: str, audience: str
) -> tuple[dict | None, str]:
    """校验账号服务签发的 HS256 access token。

    返回 (claims, reason)；reason 只用于审计，不回显给客户端。
    """
    try:
        header_raw, claims_raw, sig = token.split(".")
        header = json.loads(_b64url_decode(header_raw))
        if not isinstance(header, dict) or header.get("alg") != JWT_ALG:
            return None, "unsupported_alg"
        expected = hmac.new(
            secret.encode("utf-8"),
            f"{header_raw}.{claims_raw}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig)):
            return None, "bad_signature"
        claims = json.loads(_b64url_decode(claims_raw))
    except Exception:
        return None, "malformed"
    if not isinstance(claims, dict):
        return None, "malformed"
    if claims.get("type") != "access":
        return None, "not_access_token"
    try:
        expired = int(claims.get("exp") or 0) < int(time.time())
    except (TypeError, ValueError):
        return None, "malformed"
    if expired:
        return None, "expired"
    if claims.get("iss") != issuer:
        return None, "bad_issuer"
    aud = claims.get("aud")
    aud_ok = audience in aud if isinstance(aud, list) else aud == audience
    if not aud_ok:
        return None, "bad_audience"
    if not str(claims.get("sub") or "").strip():
        return None, "missing_sub"
    if not str(claims.get("sid") or "").strip():
        # 没有会话绑定的令牌无法被账号服务吊销，不接受
        return None, "missing_sid"
    return claims, ""


def principal_from_claims(claims: dict) -> dict:
    quota = claims.get("quota")
    return {
        "sub": str(claims.get("sub") or "").strip(),
        "sid": str(claims.get("sid") or "").strip(),
        "org": str(claims.get("org") or "").strip(),
        "role": str(claims.get("role") or "user").strip() or "user",
        "quota": quota if isinstance(quota, dict) else {},
        "auth": "jwt",
    }


STATIC_PRINCIPAL = {
    "sub": "static",
    "sid": "",
    "org": "",
    "role": "service",
    "quota": {},
    "auth": "static",
}


def _authenticate_engine_request(request: Request) -> tuple[dict | None, str]:
    """解析服务化请求的调用主体。

    未配置任何凭据时返回 (None, "")，即桌面/回环模式，行为保持不变。
    配置后静态令牌与账号 JWT 都接受（静态令牌保留为应急通道）。
    """
    static_token = str(os.environ.get("TT_ENGINE_TOKEN") or "").strip()
    jwt_secret = _engine_jwt_secret()
    if not static_token and not jwt_secret:
        return None, ""
    auth = request.headers.get("authorization") or ""
    got = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not got:
        got = (request.headers.get("x-tt-engine-token") or "").strip()
    if not got:
        return None, "missing_token"
    if static_token and hmac.compare_digest(static_token, got):
        return dict(STATIC_PRINCIPAL), ""
    if not jwt_secret:
        return None, "invalid_token"
    claims, reason = verify_engine_jwt(
        got, jwt_secret, _engine_jwt_issuer(), _engine_jwt_audience()
    )
    if not claims:
        return None, reason or "invalid_token"
    principal = principal_from_claims(claims)
    allowed = _engine_allowed_users()
    if allowed and principal["sub"] not in allowed:
        return None, "user_not_allowed"
    return principal, ""


class AuditLog:
    """请求审计台账：内存环形缓冲；设置 TT_ENGINE_AUDIT_LOG 时同步追加 JSONL。"""

    def __init__(self, maxlen: int = AUDIT_MAX_ENTRIES) -> None:
        self._entries: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def record(self, entry: dict) -> None:
        entry = {"atMs": int(time.time() * 1000), **entry}
        with self._lock:
            self._entries.append(entry)
            path = str(os.environ.get("TT_ENGINE_AUDIT_LOG") or "").strip()
            if not path:
                return
            try:
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError as exc:
                print(f"[engine] 审计日志写入失败：{exc}", flush=True)

    def query(self, limit: int = 100, subject: str | None = None) -> list[dict]:
        try:
            limit = max(1, min(int(limit), AUDIT_MAX_ENTRIES))
        except (TypeError, ValueError):
            limit = 100
        with self._lock:
            rows = list(self._entries)
        if subject is not None:
            rows = [row for row in rows if row.get("sub") == subject]
        rows = rows[-limit:]
        rows.reverse()  # 新 → 旧
        return rows


AUDIT_LOG = AuditLog()


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _is_jwt_admin(principal: dict | None) -> bool:
    return bool(principal) and principal.get("auth") == "jwt" and principal.get("role") == "admin"


def _usage_scope(sub: str) -> str | None:
    """JWT 非管理员强制只看自己的用量；其他模式按请求参数过滤。"""
    principal = current_principal()
    if principal and principal.get("auth") == "jwt" and not _is_jwt_admin(principal):
        return principal.get("sub") or ""
    return (sub or "").strip() or None


def service_bind_error(
    host: str, allow_remote: str, token: str, jwt_secret: str | None = None
) -> str:
    """绑定非回环地址的准入判断（纯函数，便于离线测试）。

    服务化会暴露可执行面；没有桌面壳时更依赖显式凭据。因此要求同时满足：
    显式确认 + 非空静态令牌或 JWT 密钥。
    """
    if host in LOOPBACK_HOSTS:
        return ""
    if allow_remote.strip() != "1":
        return "拒绝绑定非回环地址：服务化会暴露无用户体系的执行面；如确需，请设置 TT_ALLOW_REMOTE_BIND=1"
    secret = _engine_jwt_secret() if jwt_secret is None else jwt_secret.strip()
    if not token.strip() and not secret:
        return (
            "拒绝绑定非回环地址：必须设置 TT_ENGINE_TOKEN 或 TT_ENGINE_JWT_SECRET"
            "（所有 /api 请求需携带 Bearer 令牌）"
        )
    return ""


@app.middleware("http")
async def _engine_token_guard(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or request.method == "OPTIONS":
        return await call_next(request)
    principal, error = _authenticate_engine_request(request)
    if error:
        status = 403 if error == "user_not_allowed" else 401
        AUDIT_LOG.record(
            {
                "sub": "",
                "org": "",
                "role": "",
                "sid": "",
                "auth": "",
                "method": request.method,
                "path": path,
                "status": status,
                "durationMs": 0,
                "ip": _client_host(request),
                "reason": error,
            }
        )
        return JSONResponse(
            {
                "ok": False,
                "code": "ENGINE_USER_NOT_ALLOWED" if status == 403 else "ENGINE_UNAUTHORIZED",
                "message": (
                    "当前账号不在引擎允许的用户名单内"
                    if status == 403
                    else JWT_UNAUTHORIZED_MESSAGE
                ),
            },
            status_code=status,
        )
    token = _CURRENT_PRINCIPAL.set(principal)
    request.state.principal = principal
    started = time.monotonic()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        if principal is not None:
            AUDIT_LOG.record(
                {
                    "sub": principal.get("sub") or "",
                    "org": principal.get("org") or "",
                    "role": principal.get("role") or "",
                    "sid": principal.get("sid") or "",
                    "auth": principal.get("auth") or "",
                    "method": request.method,
                    "path": path,
                    "status": status,
                    "durationMs": int((time.monotonic() - started) * 1000),
                    "ip": _client_host(request),
                }
            )
        _CURRENT_PRINCIPAL.reset(token)


@app.get("/api/audit")
async def audit_get(limit: int = 100, sub: str = ""):
    """请求审计：管理员可查全部或指定用户，普通用户只能查到自己的记录。"""
    principal = current_principal()
    subject = (sub or "").strip() or None
    if principal and principal.get("auth") == "jwt" and not _is_jwt_admin(principal):
        subject = principal.get("sub") or ""
    entries = AUDIT_LOG.query(limit, subject)
    return JSONResponse({"ok": True, "count": len(entries), "entries": entries})


def mount_web(app_: FastAPI, directory: str | Path) -> Path:
    """把前端构建产物挂到根路径；API 路由先注册、静态站点兜底。

    只服务 `TT_WEB_DIR` 指定目录内的文件；不做软链穿越，也不接管 /api。
    """
    from fastapi.staticfiles import StaticFiles

    root = Path(directory).expanduser().resolve()
    if not root.is_dir() or not (root / "index.html").is_file():
        raise RuntimeError(
            f"静态站点目录不可用：{root}（需要存在 index.html；请先执行 npm run build，"
            "或把 TT_WEB_DIR 指向构建产物目录）"
        )
    app_.mount("/", StaticFiles(directory=str(root), html=True), name="web")
    return root


@app.get("/health")
async def health():
    # 只如实报告 .env 兜底 key 是否存在：自定义模型（models.json）由前端随每次请求以
    # modelConfig 传入，引擎既不掌握也不应假装掌握"用户是否已配好可用模型"。
    has_env_key = bool(os.environ.get("TT_API_KEY"))
    return JSONResponse({
        "status": "ok",
        "version": ENGINE_VERSION,
        "api_version": "1",
        "engine": "agentscope",
        "env_api_key": has_env_key,
        "uptime": time.time(),
    })


# ── 终端 AI+ ───────────────────────────────────────────────────
# 原始 shell 是本项目里权限最高的执行面之一：端点只接受桌面壳注入的进程级令牌。
# 手工 python engine/server.py、浏览器直连、IM 通道都拿不到令牌，接口统一 403。
TERMINALS = trm.TerminalManager()


def _terminal_denied() -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "code": "TERMINAL_DISABLED",
            "message": "终端 AI+ 仅桌面版可用：引擎没有收到本机终端授权令牌",
        },
        status_code=403,
    )


def _terminal_authorized(request: Request) -> bool:
    expected = str(os.environ.get("TT_TERMINAL_TOKEN") or "")
    got = request.headers.get("x-tt-terminal-token") or ""
    return bool(expected) and hmac.compare_digest(expected, got)


def _terminal_error(exc: trm.TerminalError) -> JSONResponse:
    status = 404 if exc.code == "TERMINAL_NOT_FOUND" else 400
    return JSONResponse({"ok": False, "code": exc.code, "message": exc.message}, status_code=status)


@app.post("/api/terminal/status")
async def terminal_status(request: Request):
    if not _terminal_authorized(request):
        return _terminal_denied()
    return JSONResponse({
        "ok": True,
        "enabled": True,
        "pipe": True,
        "maxSessions": trm.MAX_SESSIONS,
        "note": "管道模式：支持普通命令行工具，不支持全屏交互程序",
    })


@app.post("/api/terminal/open")
async def terminal_open(request: Request):
    if not _terminal_authorized(request):
        return _terminal_denied()
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    try:
        result = await asyncio.to_thread(TERMINALS.open, str(payload.get("cwd") or ""))
    except trm.TerminalError as exc:
        return _terminal_error(exc)
    return JSONResponse({"ok": True, **result})


@app.post("/api/terminal/input")
async def terminal_input(request: Request):
    if not _terminal_authorized(request):
        return _terminal_denied()
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    session_id = str(payload.get("sessionId") or "")
    data = payload.get("data")
    if not isinstance(data, str):
        return JSONResponse(
            {"ok": False, "code": "TERMINAL_INPUT_INVALID", "message": "data 必须是字符串"},
            status_code=400,
        )
    try:
        result = await asyncio.to_thread(TERMINALS.write, session_id, data)
    except trm.TerminalError as exc:
        return _terminal_error(exc)
    return JSONResponse(result)


@app.get("/api/terminal/read")
async def terminal_read(request: Request, session: str = "", offset: int = 0):
    if not _terminal_authorized(request):
        return _terminal_denied()
    try:
        result = await asyncio.to_thread(TERMINALS.read, session, offset)
    except trm.TerminalError as exc:
        return _terminal_error(exc)
    return JSONResponse({"ok": True, **result})


@app.post("/api/terminal/close")
async def terminal_close(request: Request):
    if not _terminal_authorized(request):
        return _terminal_denied()
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    try:
        result = await asyncio.to_thread(
            TERMINALS.close, str(payload.get("sessionId") or ""), "user"
        )
    except trm.TerminalError as exc:
        return _terminal_error(exc)
    return JSONResponse(result)


@app.post("/api/terminal/ai")
async def terminal_ai(request: Request):
    """根据最近输出给命令建议；永不执行，执行由用户在界面上逐条确认。"""
    if not _terminal_authorized(request):
        return _terminal_denied()
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    session_id = str(payload.get("sessionId") or "")
    question = str(payload.get("question") or "").strip()
    if not question:
        return JSONResponse(
            {"ok": False, "code": "TERMINAL_QUESTION_REQUIRED", "message": "请先描述要解决的问题"},
            status_code=400,
        )
    try:
        context, cwd = TERMINALS.context(session_id)
        shell = TERMINALS.info(session_id)["shell"] or ("cmd.exe" if os.name == "nt" else "shell")
    except trm.TerminalError as exc:
        return _terminal_error(exc)

    pii_raw = payload.get("piiOutboundMode")
    pii_mode = "off"
    if pii_raw is not None:
        pii_value = str(pii_raw).strip().lower()
        if pii_value not in pg.MODES:
            return JSONResponse(
                {"ok": False, "code": "PII_MODE_INVALID",
                 "message": f"未知出站脱敏档位 {pii_raw}（可用：{'/'.join(pg.MODES)}）"},
                status_code=400,
            )
        pii_mode = pii_value

    model_config = payload.get("modelConfig")
    model_config = dict(model_config) if isinstance(model_config, dict) and model_config else None
    if model_config is None:
        model_id = str(payload.get("modelId") or "").strip()
        selected, model_err = _models_find(model_id or None)
        if model_err:
            code, _, message = model_err.partition(": ")
            return JSONResponse(
                {"ok": False, "code": code or "MODEL_INVALID", "message": message or model_err},
                status_code=404 if code == "MODEL_NOT_FOUND" else 400,
            )
        if selected:
            model_config, model_err = _model_runtime_config(selected)
            if model_err:
                code, _, message = model_err.partition(": ")
                return JSONResponse(
                    {"ok": False, "code": code or "MODEL_INVALID", "message": message or model_err},
                    status_code=400,
                )

    redactions: list[str] = []

    def _note_redaction(kind: str) -> None:
        redactions.append(kind)

    try:
        model, formatter = build_model_and_formatter(
            None, model_config, stream=False, pii_mode=pii_mode, on_redaction=_note_redaction
        )
        from agentscope.message import Msg

        prompt = trm.build_suggest_prompt(shell, cwd, context, question)
        formatted = await formatter.format([Msg(name="user", content=prompt, role="user")])
        resp = await model(formatted)
        raw = _text_of(getattr(resp, "content", ""))
    except pg.PrivacyBlocked:
        return JSONResponse(
            {"ok": False, "code": "PII_BLOCKED",
             "message": "终端输出命中出站脱敏的阻断规则，未发送给模型；请检查输出中的敏感信息"},
            status_code=400,
        )
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "code": "TERMINAL_AI_FAILED", "message": f"模型调用失败：{exc}"},
            status_code=502,
        )

    return JSONResponse({
        "ok": True,
        "suggestion": trm.parse_suggestion(raw),
        "model": _effective_model_name(None, model_config),
        "piiMode": pii_mode,
        "redactions": redactions,
        "executed": False,
    })


@app.post("/api/chat")
async def chat(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    goal = (payload.get("message") or "").strip()
    # 模型档位绑定（高级/基础/经济 → 具体模型名，Auto/缺省时用 .env 默认模型）
    model_binding = str(payload.get("modelBinding") or "").strip() or None
    # 任务 ID：优先由调用方（Rust 桥接层）传入，兼容旧契约自动生成
    task_id = (payload.get("task_id") or "").strip() or \
        f"task-{int(time.time() * 1000)}"
    session = _get_or_create_session(task_id)
    if goal:
        # 新一轮提交：旧检查点作废（同一 task 重试也不应恢复上一次的失败现场）
        session.checkpoint = ckpt.TaskCheckpoint(task_id)
        ckpt.delete_checkpoint(task_id)
        session.cancelled = False
        # 单阶段提交也要记住目标：断点续跑要用同一目标重新组装本轮用户消息
        session.goal = goal
    if model_binding:
        session.model_binding = model_binding
    # 自定义模型：桌面端由 Rust 解析成调用配置（兼容旧通道），浏览器直连按 modelId 在
    # models.json 里解析；两者都没有时才使用默认模型。密钥不出渲染层，也不进事件流。
    model_config_raw = payload.get("modelConfig")
    model_config = dict(model_config_raw) if isinstance(model_config_raw, dict) and model_config_raw else None
    model_id = str(payload.get("modelId") or "").strip()
    if model_config:
        # 预置模型源未填 baseUrl 时补默认端点（桌面端 Rust 只传 source 的旧条目也能调用）
        if not str(model_config.get("baseUrl") or "").strip():
            default_base = MODEL_DEFAULT_BASE_URLS.get(str(model_config.get("source") or ""), "")
            if default_base:
                model_config["baseUrl"] = default_base
        session.model_config = model_config
        session.model_id = model_id or None
    else:
        selected: dict | None = None
        model_err = ""
        if model_id:
            selected, model_err = _models_find(model_id)
            if not model_err and selected:
                model_config, model_err = _model_runtime_config(selected)
        elif not model_binding:
            # Auto：浏览器直连以 models.json 的默认模型为真实归属；没有默认才回落环境变量
            selected, model_err = _models_find(None)
            if not model_err and selected:
                model_config, model_err = _model_runtime_config(selected)
        if model_err:
            code, _, message = model_err.partition(": ")
            return JSONResponse(
                {"ok": False, "code": code or "MODEL_INVALID", "message": message or model_err},
                status_code=404 if code == "MODEL_NOT_FOUND" else 400,
            )
        if model_config:
            session.model_config = model_config
            session.model_id = str((selected or {}).get("id") or "") or None
    # 工作台模式（general/design/slides/writing）：Rust 已校验白名单，
    # 浏览器直连/IM 也要在这里拒绝未知值，避免静默按 general 跑偏。
    mode_raw = str(payload.get("mode") or "").strip().lower()
    if mode_raw:
        if mode_raw not in rc.VALID_MODES:
            return JSONResponse(
                {
                    "ok": False,
                    "code": "MODE_INVALID",
                    "message": f"未知工作台模式 {mode_raw}（可用：{'/'.join(rc.VALID_MODES)}）",
                },
                status_code=400,
            )
        session.mode = mode_raw
    # 前端选择的工作目录：工具操作与产物落盘均限定其中（approve 重跑沿用）
    work_dir_raw = str(payload.get("workDir") or "").strip()
    if work_dir_raw:
        try:
            session.work_dir = Path(work_dir_raw).expanduser()
        except Exception:
            session.work_dir = None
    # 安全工作环境开关（Rust 从设置读取后随提交下发，approve 重跑沿用）
    if payload.get("secureEnv") is not None:
        session.secure_env = bool(payload.get("secureEnv"))
    # 隐私模式：不建会话目录、不写 conversation.md、不写自动记忆
    if payload.get("privacyMode") is not None:
        session.privacy_mode = bool(payload.get("privacyMode"))
        if session.privacy_mode:
            session.session_dir = None
    # 出站脱敏档位：桌面端经 runtime.privacy 下发，浏览器直连随请求携带；
    # 这里只接受白名单值，未知值直接拒绝而不是静默按关闭处理
    pii_raw = payload.get("piiOutboundMode")
    if pii_raw is not None:
        pii_value = str(pii_raw).strip().lower()
        if pii_value not in pg.MODES:
            return JSONResponse(
                {
                    "ok": False,
                    "code": "PII_MODE_INVALID",
                    "message": f"未知出站脱敏档位 {pii_raw}（可用：{'/'.join(pg.MODES)}）",
                },
                status_code=400,
            )
        session.pii_mode = pii_value
    # 对话历史（保障连续对话）：桌面端两阶段协议下随 plan 请求到达，
    # 存进会话后 approve 的执行流才能预填充；单阶段请求直接用本次传入的
    history = _normalize_history(payload.get("history"))
    if history:
        session.history = history
    # 运行时上下文（意识 / 启用技能）与附件：由 Rust 装配后随提交下发。
    # 键缺失时不覆盖会话上的既有值（approve 重跑沿用），也不报错——
    # 浏览器直连与 IM 通道本就没有这两项。
    runtime_raw = payload.get("runtime")
    if isinstance(runtime_raw, dict) and runtime_raw:
        session.runtime = runtime_raw
    # 浏览器直连没有 Rust 装配的 runtime，效率策略随请求顶层下发；
    # runtime 里已有 agentPolicy（桌面端 settings_kv）时以它为权威，不被覆盖。
    agent_policy_raw = payload.get("agentPolicy")
    if isinstance(agent_policy_raw, dict) and agent_policy_raw:
        rt = session.runtime if isinstance(session.runtime, dict) else {}
        if not isinstance(rt.get("agentPolicy"), dict):
            session.runtime = {**rt, "agentPolicy": agent_policy_raw}
    # 幻灯片模板同理：桌面端以 runtime.pptxTemplates 为准（含导入模板路径），
    # 浏览器直连只能带内置主题，随请求顶层下发。
    pptx_raw = payload.get("pptxTemplates")
    if isinstance(pptx_raw, dict) and pptx_raw:
        rt = session.runtime if isinstance(session.runtime, dict) else {}
        if not isinstance(rt.get("pptxTemplates"), dict):
            session.runtime = {**rt, "pptxTemplates": pptx_raw}
    # 负责人专家：注册表随 runtime 下发，这里只收 id。
    # 不存在/绑定模型不可用直接拒绝，不留下必然失败的任务；
    # 计划→批准的第二阶段不带该字段，沿用 session 上的选择。
    expert_ref = str(payload.get("expertId") or "").strip()
    if expert_ref:
        experts, experts_error = er.normalize_registry(_experts_block(session))
        if experts_error:
            return JSONResponse(
                {"ok": False, "code": "EXPERT_UNAVAILABLE",
                 "message": f"专家团队不可用：{experts_error}"},
                status_code=400,
            )
        if not experts:
            return JSONResponse(
                {"ok": False, "code": "EXPERT_UNAVAILABLE",
                 "message": "本次请求没有携带专家注册表，无法按专家执行（请从桌面端重新提交）"},
                status_code=400,
            )
        expert = er.find_expert(experts, expert_ref)
        if expert is None:
            return JSONResponse(
                {"ok": False, "code": "EXPERT_NOT_FOUND",
                 "message": f"负责人专家 {expert_ref} 不存在，请刷新专家团队"},
                status_code=404,
            )
        _, expert_model_error = er.model_config_of(expert)
        if expert_model_error:
            return JSONResponse(
                {"ok": False, "code": "EXPERT_UNAVAILABLE", "message": expert_model_error},
                status_code=400,
            )
        session.expert_id = expert["id"]
    elif goal:
        # 新一轮没有选择专家：显式清空，避免上一轮的专家身份延续到这一轮
        session.expert_id = None
    attachments_raw = payload.get("attachments")
    if isinstance(attachments_raw, list) and attachments_raw:
        session.attachments = rc.normalize_attachments(attachments_raw)

    # 会话子目录：工作目录下按对话时间创建，存会话内容与产出物。
    # 隐私模式不建目录、不写 conversation.md（产出物仍落在工作目录，不会被复制归档）。
    if session.session_dir is None and goal and not session.privacy_mode:
        work_dir = str(payload.get("workDir") or payload.get("work_dir") or "").strip()
        try:
            session.session_dir = _create_session_dir(work_dir, goal)
            _log_conversation(session.session_dir, f"# 任务：{goal}")
            _log_conversation(session.session_dir, f"- 对话时间：{time.strftime('%Y-%m-%d %H:%M:%S')}，任务 ID：{task_id}\n")
            if session.attachments:
                # 附件名进会话记录：事后要能看出这轮带了什么材料、哪些没进模型
                _log_conversation(
                    session.session_dir,
                    "- 附件：" + "、".join(str(a.get("fileName") or "附件") for a in session.attachments),
                )
        except Exception as exc:
            print(f"[engine] 会话目录创建失败（降级为不落盘）: {exc}", flush=True)
            session.session_dir = None

    # 断线续传：Last-Event-ID 命中则先回放窗口内后续事件再接续活流
    last_event_id = request.headers.get("last-event-id")
    replay_from: list = []
    parsed = _parse_last_event_id(last_event_id)
    if parsed and parsed[0] == task_id:
        replay_from = session.replay_after(parsed[1])

    sse_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

    # 两阶段协议（核心闭环补齐）：stage=plan 仅生成计划待审阅；
    # 缺省保持旧契约全流程执行（向后兼容/Mock 测试触发器）
    if payload.get("stage") == "plan":
        return StreamingResponse(
            _plan_stream(session, task_id, goal),
            media_type="text/event-stream",
            headers=sse_headers,
        )

    return StreamingResponse(
        _run_execution(session, task_id, goal, replay_from, model_binding, session.work_dir, history, model_config),
        media_type="text/event-stream",
        headers=sse_headers,
    )


@app.post("/api/chat/review")
async def chat_review(request: Request):
    """计划审阅（两阶段协议，转移 #9/#10/#11）：
    - approve → SSE 执行流（executing → step_* → deliver/done）
    - reject  → status_change(done, REJECTED) + done
    - modify  → 应用 patch 后重新产出 plan（回 pending_review）
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    task_id = (payload.get("task_id") or "").strip()
    action = (payload.get("action") or "").strip()
    patch = payload.get("patch") or []
    session = _TASK_SESSIONS.get(task_id)
    if session is None and action == "resume":
        # 引擎重启后会话不在内存：尝试按磁盘检查点重建（密钥不落盘，恢复时重解析）
        session, restore_err = _restore_session_from_disk(task_id, payload)
        if restore_err:
            return JSONResponse(
                {
                    "ok": False,
                    "taskId": task_id,
                    "code": restore_err["code"],
                    "message": restore_err["message"],
                },
                status_code=int(restore_err["status"]),
            )
    if session is None:
        return JSONResponse(
            {
                "ok": False,
                "code": "TASK_NOT_FOUND",
                "taskId": task_id,
                "message": (
                    "任务会话不在当前引擎进程中（引擎可能已重启），"
                    "且没有找到可用的磁盘检查点"
                ),
            },
            status_code=404,
        )

    sse_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    sse = _sse_formatter(session, task_id)

    if action == "resume":
        # 断点续跑：从最近一次模型调用前的检查点继续同一任务（taskId 不变）
        # Rust 每次 resume 都重新下发模型配置与 runtime（磁盘里不保存密钥与网关令牌）
        _apply_resume_overrides(session, payload)
        if session.agent_task is not None and not session.agent_task.done():
            return JSONResponse(
                {
                    "ok": False,
                    "code": "CHECKPOINT_TASK_RUNNING",
                    "message": "任务仍在执行中，不能同时从检查点恢复",
                },
                status_code=409,
            )
        reason = session.checkpoint.unavailable_reason()
        if reason:
            return JSONResponse(
                {"ok": False, "code": "CHECKPOINT_UNAVAILABLE", "message": reason},
                status_code=409,
            )
        session.cancelled = False
        return StreamingResponse(
            _run_execution(
                session, task_id, session.goal or "", [],
                session.model_binding, session.work_dir, None,
                session.model_config, resume=True,
            ),
            media_type="text/event-stream",
            headers=sse_headers,
        )

    if action == "approve":
        return StreamingResponse(
            _run_execution(session, task_id, session.goal or "", [], session.model_binding, session.work_dir, None, session.model_config),
            media_type="text/event-stream",
            headers=sse_headers,
        )

    if action == "reject":
        async def reject_stream():
            yield sse("status_change", {
                "taskId": task_id,
                "status": "done",
                "statusReasonCode": "REJECTED",
            })
            yield sse("done", {})
        return StreamingResponse(reject_stream(), media_type="text/event-stream", headers=sse_headers)

    if action == "modify":
        # 应用 patch：按 step_id 合并 name/description（不允许新增步骤，交互补设计 §5.2）
        steps = [dict(s) for s in (session.plan or _fallback_plan(session.goal or ""))]
        if isinstance(patch, list):
            patch_map = {}
            for p in patch:
                if isinstance(p, dict) and p.get("id"):
                    patch_map[p["id"]] = p
            for s in steps:
                p = patch_map.get(s.get("step_id"))
                if p:
                    desc = p.get("name") or p.get("description")
                    if desc:
                        s["description"] = str(desc)
        session.plan = steps

        async def modify_stream():
            yield sse("plan", {"plan_id": f"plan-{task_id}", "steps": steps})
            yield sse("status_change", {"taskId": task_id, "status": "pending_review"})
            yield sse("done", {})
        return StreamingResponse(modify_stream(), media_type="text/event-stream", headers=sse_headers)

    return JSONResponse({"ok": False, "code": "VALIDATION_ERROR", "message": f"未知 action: {action}"})


@app.post("/api/chat/checkpoint/query")
async def chat_checkpoint_query(request: Request):
    """批量查询任务检查点状态（刷新页面后重建「从检查点继续」入口）。

    只返回状态与计数，不返回对话正文/工具参数；进程内任务优先，其次读磁盘。
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    raw = payload.get("task_ids") or payload.get("taskIds") or []
    if not isinstance(raw, list):
        raw = []
    task_ids: list[str] = []
    for item in raw:
        task_id = str(item or "").strip()
        if task_id and task_id not in task_ids:
            task_ids.append(task_id)
        if len(task_ids) >= 200:
            break
    out: dict[str, dict] = {}
    for task_id in task_ids:
        session = _TASK_SESSIONS.get(task_id)
        if session is not None:
            info = _checkpoint_public(session)
        else:
            data = ckpt.load_checkpoint(task_id)
            info = ckpt.public_info(task_id, data) if data else None
        if info:
            out[task_id] = info
    return JSONResponse({"checkpoints": out})


@app.get("/api/events/replay")
async def events_replay(task_id: str = "", from_seq: int = 0):
    """事件重放（三层架构演进 P0）：返回任务窗口内 seq > from_seq 的事件。
    供 Rust 桥接层在 SSE 断线且无法续流时全量补齐。"""
    session = _TASK_SESSIONS.get(task_id)
    if session is None:
        return JSONResponse({"taskId": task_id, "events": [], "windowExpired": True})
    entries = session.replay_after(from_seq)
    return JSONResponse({
        "taskId": task_id,
        "windowExpired": False,
        "events": [
            {"seq": seq, "type": event, "data": data}
            for (seq, event, data) in entries
        ],
    })


@app.post("/api/chat/cancel")
async def chat_cancel(request: Request):
    """取消任务（三层架构演进 P0，转移 #3/#8/#19/#23/#28）"""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    task_id = (payload.get("task_id") or "").strip()
    session = _TASK_SESSIONS.get(task_id)
    if session is None or session.agent_task is None:
        return JSONResponse({"taskId": task_id, "cancelled": False, "reason": "TASK_NOT_FOUND"})
    if session.agent_task.done():
        return JSONResponse({"taskId": task_id, "cancelled": False, "reason": "TASK_ALREADY_FINISHED"})
    session.cancelled = True
    session.agent_task.cancel()
    return JSONResponse({"taskId": task_id, "cancelled": True})


@app.post("/api/files/upload")
async def files_upload(request: Request):
    """对话附件真实上传（联调/浏览器开发阶段）：
    接收 base64 文件内容，落盘到工作目录 uploads/attachments/，
    返回与 AttachmentMeta 契约一致的字段，随 /api/chat 提交。"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            {"ok": False, "code": "VALIDATION_ERROR", "message": "无效 JSON"}, status_code=400
        )
    file_name = str(payload.get("fileName") or "").strip()
    data_b64 = payload.get("dataBase64") or ""
    if not file_name or not data_b64:
        return JSONResponse(
            {"ok": False, "code": "VALIDATION_ERROR", "message": "fileName/dataBase64 必填"},
            status_code=400,
        )
    try:
        raw = base64.b64decode(data_b64)
    except Exception:
        return JSONResponse(
            {"ok": False, "code": "VALIDATION_ERROR", "message": "base64 内容无效"}, status_code=400
        )
    # 文件名净化：去除路径分隔符，防止目录穿越
    safe_name = Path(file_name).name or "attachment.bin"
    target_dir = WORK_DIR / "uploads" / "attachments"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
    target.write_bytes(raw)
    return JSONResponse({
        "ok": True,
        "fileName": safe_name,
        "stagedPath": str(target),
        "sizeBytes": len(raw),
        "mime": payload.get("mime") or "application/octet-stream",
        "sensitivity": "public",
    })


@app.get("/api/files/content")
async def files_content(path: str = ""):
    """产物预览：返回文件文本内容（限 2MB，文本类文件），供前端点击产物打开预览"""
    p = Path(path)
    if not path or not p.exists() or not p.is_file():
        return JSONResponse({"ok": False, "message": "文件不存在或已移动"}, status_code=404)
    try:
        if p.stat().st_size > 2 * 1024 * 1024:
            return JSONResponse({"ok": False, "message": "文件超过 2MB，请用系统应用打开"}, status_code=413)
    except Exception:
        pass
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            text = p.read_text(encoding="gbk", errors="replace")
        except Exception:
            return JSONResponse({"ok": False, "message": "二进制文件暂不支持预览"}, status_code=415)
    except Exception as exc:
        return JSONResponse({"ok": False, "message": f"读取失败: {exc}"}, status_code=500)
    suffix = p.suffix.lower().lstrip(".")
    mime = {
        "md": "text/markdown", "markdown": "text/markdown",
        "html": "text/html", "htm": "text/html",
        "txt": "text/plain", "log": "text/plain",
        "json": "application/json",
        "csv": "text/csv",
        "py": "text/x-python", "js": "text/javascript", "ts": "text/typescript",
    }.get(suffix, "text/plain")
    return JSONResponse({"ok": True, "fileName": p.name, "mime": mime, "content": text})


# ── 意识持久化：真实目录 ~/.tongtianwork/awareness/main ─────────────

AWARENESS_FILE_NAMES = ("SOUL.md", "WORK_MANUAL.md", "USER_PROFILE.md", "MEMORY.md")

# 意识能力文件默认模板：协作风格/工作手册/用户画像/长期记忆首次加载时初始化落盘
AWARENESS_DEFAULTS = {
    "SOUL.md": """# SOUL.md - 协作风格 - 高效极简

用最少的话把事情做好。

## 核心原则
- **直接给结果。** 代码优于描述。能用一行说清的不用三段。
- **不要复述问题。** 我知道我问了什么，直接回答。
- **默认执行。** 方案明确时直接做，不用每步都确认。
- **不确定时一句话确认。** 不要列出一堆选项让我选，给出你的判断，问我对不对。

## 沟通风格
- 回复尽可能简短
- 跳过寒暄、过渡句和总结性废话
- 代码注释只在逻辑不明显时添加
- 用列表而非段落

## 边界
- 高风险操作前简短确认
- 私密信息不外泄""",
    "WORK_MANUAL.md": """# 工作手册

## 工作规范
- （在此补充你的规范）

## 常用流程
- （在此补充常用流程）""",
    "USER_PROFILE.md": """# 用户画像

## 基本信息
- 称呼：

## 偏好
- （待补充）

## 习惯
- （待补充）""",
    "MEMORY.md": """# 长期记忆

（暂无记忆条目，可在下方手动新增）""",
}


def _awareness_home() -> Path:
    """意识目录：`TONGTIANWORK_HOME` 覆盖（与 Rust `awareness::dir()` 同规则），
    缺省 `~/.tongtianwork`，两条通道因此读写同一份文件"""
    root = str(os.environ.get("TONGTIANWORK_HOME") or "").strip()
    base = Path(root) if root else Path.home() / ".tongtianwork"
    return base / "awareness" / "main"


# 协作风格预设 id（与 Rust `awareness::STYLE_IDS`、contracts.ts 一致）
AWARENESS_STYLES = ("efficient", "deliberate", "gentle", "assertive")
AWARENESS_DEFAULT_STYLE = "efficient"
# 意识画像落盘文件名（不在可编辑能力文件白名单内，只经 /api/awareness 接口读写）
AWARENESS_PROFILE_FILE = "PROFILE.json"
# 长期记忆条目的行分隔符（`- {id} · {source} · {createdAt} · {content}`，与 Rust 同格式）
MEMORY_DELIM = " · "
MEMORY_SOURCES = ("auto", "manual", "reflection")


def _iso_utc(ms: int) -> str:
    """epoch 毫秒 → `2026-09-21T00:00:00.000Z`（与 Rust `audit::iso8601_utc` 同格式）"""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000)) + ".%03dZ" % (ms % 1000)


def _awareness_updated_at(p: Path) -> str:
    try:
        return _iso_utc(int(p.stat().st_mtime * 1000))
    except Exception:
        return "系统默认"


def _profile_default() -> dict:
    return {
        "mode": True,
        "autoMemory": True,
        "style": AWARENESS_DEFAULT_STYLE,
        "updatedAt": "",
    }


def _profile_load(home: Path) -> dict:
    """读取 PROFILE.json；缺失/损坏回落默认值（与 Rust load_profile 同口径）"""
    profile = _profile_default()
    try:
        raw = json.loads((home / AWARENESS_PROFILE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return profile
    if not isinstance(raw, dict):
        return profile
    if "mode" in raw:
        profile["mode"] = raw["mode"] is True or str(raw["mode"]).strip() == "true"
    if "autoMemory" in raw:
        profile["autoMemory"] = raw["autoMemory"] is True or str(raw["autoMemory"]).strip() == "true"
    if raw.get("style") in AWARENESS_STYLES:
        profile["style"] = raw["style"]
    profile["updatedAt"] = str(raw.get("updatedAt") or "")
    return profile


def _profile_save(home: Path, profile: dict) -> None:
    (home / AWARENESS_PROFILE_FILE).write_text(
        json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _files_load(home: Path) -> dict:
    """四个能力文件；缺失的按默认模板初始化落盘，已存在的绝不覆盖"""
    files: dict = {}
    for name in AWARENESS_FILE_NAMES:
        p = home / name
        if not p.exists():
            try:
                p.write_text(AWARENESS_DEFAULTS.get(name, ""), encoding="utf-8")
            except Exception:
                pass
            files[name] = {
                "content": AWARENESS_DEFAULTS.get(name, ""),
                "updatedAt": "系统默认",
            }
            continue
        try:
            files[name] = {"content": p.read_text(encoding="utf-8"), "updatedAt": _awareness_updated_at(p)}
        except Exception:
            files[name] = {"content": "", "updatedAt": _awareness_updated_at(p)}
    return files


def _awareness_view(home: Path) -> dict:
    profile = _profile_load(home)
    return {
        "ok": True,
        "mode": profile["mode"],
        "autoMemory": profile["autoMemory"],
        "style": profile["style"],
        "updatedAt": profile["updatedAt"],
        "dir": str(home),
        "files": _files_load(home),
    }


def _memory_line(entry: dict) -> str:
    return "- {}{}{}{}{}{}{}".format(
        entry["id"], MEMORY_DELIM, entry["source"], MEMORY_DELIM, entry["createdAt"], MEMORY_DELIM, entry["content"]
    )


def _parse_memory_line(line: str):
    """解析结构化行；用户自己写的正文/默认占位段返回 None 并原样保留"""
    body = line.strip()
    if not body.startswith("- "):
        return None
    parts = body[2:].split(MEMORY_DELIM, 3)
    if len(parts) != 4:
        return None
    mid, source, created_at, content = (p.strip() for p in parts)
    if not mid or not content or source not in MEMORY_SOURCES:
        return None
    return {"id": mid, "content": content, "source": source, "createdAt": created_at}


def _memories_load(home: Path) -> list:
    """长期记忆条目（新→旧，与 Rust list_memories 同排序口径）"""
    p = home / "MEMORY.md"
    if not p.exists():
        home.mkdir(parents=True, exist_ok=True)
        p.write_text(AWARENESS_DEFAULTS.get("MEMORY.md", ""), encoding="utf-8")
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        entry = _parse_memory_line(line)
        if entry:
            rows.append(entry)
    rows.sort(key=lambda e: (e["createdAt"], e["id"]), reverse=True)
    return rows


def _memory_id(now_ms: int, existing) -> str:
    base = "mem-%d" % now_ms
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


# ── 自动记忆：成功任务收尾时追加可审计摘要 ────────────────────────
#
# 不做「模型再总结一遍」的额外调用：那会把整轮对话（可能含凭据）再送一次远端。
# 这里只用本机已有事实（目标、最终回复、产物文件名）拼一条紧凑记录，可追溯、
# 可手动删除；开关关闭、疑似凭据、重复内容、条数上限都如实作为步骤事件播报。

# 自动记忆条数上限：条目会按相关性进入 prompt（检索前仍要控制总量），
# 到上限后只提示，不自动删除任何历史条目（删除必须由用户点名）。
MAX_AUTO_MEMORY_ENTRIES = 50
MAX_AUTO_MEMORY_CHARS = 400
_AUTO_MEMORY_SECRET_HINTS = (
    "sk-", "akia", "ghp_", "xoxb-", "xoxp-", "bearer ",
    "api_key=", "apikey=", "api-key=", "password=", "passwd=",
    "secret=", "token=", "-----begin",
)


def _one_line(text, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _auto_memory_content(goal: str, final_text: str, artifacts: list) -> str:
    """目标 + 结论 + 产物文件名，压成一行（记忆文件按行解析，不能带换行）"""
    parts: list[str] = []
    goal_line = _one_line(goal, 120)
    if goal_line:
        parts.append(f"目标：{goal_line}")
    conclusion = _one_line(final_text, 180)
    if conclusion:
        parts.append(f"结论：{conclusion}")
    names: list[str] = []
    for item in artifacts or []:
        name = Path(str(item)).name
        if name and name not in names:
            names.append(name)
    if names:
        shown = "、".join(names[:3])
        parts.append(f"产物：{shown}{'…' if len(names) > 3 else ''}")
    return _one_line("；".join(parts), MAX_AUTO_MEMORY_CHARS)


def _looks_like_secret(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(hint in lowered for hint in _AUTO_MEMORY_SECRET_HINTS)


def _auto_memory_decision(runtime: dict | None, privacy: bool = False) -> tuple[bool, str]:
    """返回 (是否写入, 不写的原因)。关闭开关属于用户意愿，不产生噪音说明。"""
    if privacy:
        return False, "隐私模式已开启，本轮未写入自动记忆"
    block = (runtime or {}).get("awareness")
    if isinstance(block, dict):
        if str(block.get("error") or "").strip():
            return False, "意识配置读取失败，本轮未写入自动记忆"
        mode = block.get("mode")
        auto = block.get("autoMemory")
    else:
        # 浏览器直连 / IM 通道没有 Rust 下发的 awareness 段：回落到本机 PROFILE.json
        try:
            profile = _profile_load(_awareness_home())
        except Exception:
            return False, "意识配置读取失败，本轮未写入自动记忆"
        mode, auto = profile.get("mode", True), profile.get("autoMemory", True)
    if mode is False:
        return False, "意识模式已关闭，本轮未写入自动记忆"
    if auto is False:
        return False, ""
    return True, ""


def _auto_memory_after_success(runtime: dict | None, goal: str, final_text: str,
                               artifacts: list, privacy: bool = False) -> tuple[bool, str]:
    """成功任务 → 追加一条 source=auto 的记忆；返回 (是否写入, 可见说明)。"""
    enabled, reason = _auto_memory_decision(runtime, privacy=privacy)
    if not enabled:
        return False, reason
    content = _auto_memory_content(goal, final_text, artifacts)
    if not content:
        return False, ""
    if _looks_like_secret(content):
        return False, "本轮内容疑似包含凭据，已跳过自动记忆（避免密钥落盘）"

    home = _awareness_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        entries = _memories_load(home)
    except Exception as exc:
        return False, f"自动记忆读取失败，本轮未写入：{exc}"

    if any(str(e.get("content") or "").strip() == content for e in entries):
        return False, "本轮自动记忆与已有条目相同，未重复写入"
    auto_count = sum(1 for e in entries if e.get("source") == "auto")
    if auto_count >= MAX_AUTO_MEMORY_ENTRIES:
        return False, (
            f"自动记忆已有 {auto_count} 条，本轮未写入"
            "（不会自动删除历史记忆，可在设置页整理）"
        )

    now_ms = int(time.time() * 1000)
    entry = {
        "id": _memory_id(now_ms, [e["id"] for e in entries]),
        "content": content,
        "source": "auto",
        "createdAt": _iso_utc(now_ms),
    }
    p = home / "MEMORY.md"
    tmp: Path | None = None
    try:
        # 只追加新行：先读全文再原子替换，任何已有正文/手动记忆原样保留
        raw = p.read_text(encoding="utf-8") if p.exists() else ""
        merged = raw.rstrip("\n")
        merged = (merged + "\n" if merged else "") + _memory_line(entry) + "\n"
        tmp = p.with_name(f".MEMORY.md.tmp-{os.getpid()}-{now_ms}")
        tmp.write_text(merged, encoding="utf-8")
        os.replace(tmp, p)
    except Exception as exc:
        if tmp is not None:
            try:
                tmp.unlink()
            except Exception:
                pass
        return False, f"自动记忆写入失败：{exc}"
    return True, f"已写入自动记忆：{content}"


# ── 自定义模型配置文件：真实路径 {TONGTIANWORK_HOME 或 ~/.tongtianwork}/models.json ──
#
# 浏览器直连模式下引擎是模型配置的权威方（桌面版权威在 Rust SQLite + keyring）。
# 两条通道的共同约定：
# - 读取接口只返回 hasApiKey，绝不回传明文 apiKey
# - 更新时 apiKey 缺省或占位符 = 保留原密钥，空串 = 显式清除
# - 对话只收 modelId（或档位 modelBinding），由后端解析端点与密钥

MODEL_SOURCES = (
    "openai_compat",
    "ollama",
    "preset_qwen",
    "preset_deepseek",
    "preset_openai",
    "custom",
)

# 预置模型源的 OpenAI 兼容端点（保存时未填 baseUrl 也能真实调用）
MODEL_DEFAULT_BASE_URLS = {
    "preset_qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "preset_deepseek": "https://api.deepseek.com/v1",
    "preset_openai": "https://api.openai.com/v1",
    "ollama": "http://127.0.0.1:11434/v1",
}

# 前端编辑态密钥占位符：回传它表示「不改密钥」
_MODEL_KEY_PLACEHOLDER = "●●●●●●●●"


def _models_config_path() -> Path:
    """与意识目录同一套 `TONGTIANWORK_HOME` 规则，测试可整体隔离到临时目录"""
    root = str(os.environ.get("TONGTIANWORK_HOME") or "").strip()
    base = Path(root) if root else Path.home() / ".tongtianwork"
    return base / "models.json"


def _models_load() -> list[dict]:
    """读取模型条目（文件缺失/损坏按空清单处理，不覆盖原文件）"""
    p = _models_config_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw = data.get("models") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    return [dict(m) for m in raw if isinstance(m, dict)]


def _models_save(models: list[dict]) -> None:
    """原子写入：先写临时文件再替换，避免半截 JSON 损坏历史配置"""
    p = _models_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    now_ms = int(time.time() * 1000)
    tmp = p.with_name(f".models.json.tmp-{os.getpid()}-{now_ms}")
    tmp.write_text(
        json.dumps({"models": models}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, p)


def _as_int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _model_price_of(value) -> float | None:
    """单价解析：仅接受有限非负数字；None/空=未配置（不按 0 元计价）"""
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num) or num < 0:
        return None
    return num


def _model_prices_of(entry: dict) -> dict | None:
    """条目 → runtime prices；输入输出必须成对才计价"""
    pin = _model_price_of(entry.get("priceInputPerM"))
    pout = _model_price_of(entry.get("priceOutputPerM"))
    if pin is None or pout is None:
        return None
    currency = str(entry.get("priceCurrency") or "CNY").strip().upper() or "CNY"
    return {"inputPerM": pin, "outputPerM": pout, "currency": currency}


def _model_public(entry: dict) -> dict:
    """对外视图：密钥只暴露「有没有」，不回传明文（API 视图/日志/错误详情同口径）"""
    return {
        "id": str(entry.get("id") or ""),
        "name": str(entry.get("name") or ""),
        "source": str(entry.get("source") or "openai_compat"),
        "baseUrl": str(entry.get("baseUrl") or ""),
        "modelName": str(entry.get("modelName") or ""),
        "contextWindow": _as_int(entry.get("contextWindow"), 4096),
        "isDefault": bool(entry.get("isDefault")),
        "agentBinding": str(entry.get("agentBinding") or ""),
        "forceLocal": bool(entry.get("forceLocal")),
        # 计费与故障转移：单价缺省=未配置（None），绝不回填 0 元
        "priceInputPerM": _model_price_of(entry.get("priceInputPerM")),
        "priceOutputPerM": _model_price_of(entry.get("priceOutputPerM")),
        "priceCurrency": str(entry.get("priceCurrency") or "CNY").strip().upper() or "CNY",
        "fallbackIds": [
            str(x) for x in (entry.get("fallbackIds") or []) if str(x).strip()
        ][:3],
        "hasApiKey": bool(str(entry.get("apiKey") or "").strip()),
        # 与 shared/types.ts ModelConfig 对齐：密钥永不落这份视图
        "apiKeyEncrypted": "",
        "createdAt": str(entry.get("createdAt") or ""),
        "updatedAt": str(entry.get("updatedAt") or ""),
    }


def _model_host(base_url: str) -> str:
    try:
        return (urlsplit(base_url).hostname or "").strip().lower()
    except Exception:
        return ""


def _model_is_local(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost")


def _model_effective_base(entry: dict) -> str:
    base = str(entry.get("baseUrl") or "").strip()
    if base:
        return base
    return MODEL_DEFAULT_BASE_URLS.get(str(entry.get("source") or ""), "")


def _model_validate(entry: dict) -> str:
    name = str(entry.get("name") or "").strip()
    if not name:
        return "MODEL_INVALID: 模型名称不能为空"
    if not str(entry.get("modelName") or "").strip():
        return "MODEL_INVALID: 服务端模型名（modelName）不能为空"
    source = str(entry.get("source") or "openai_compat")
    if source not in MODEL_SOURCES:
        return f"MODEL_INVALID: 未知模型源 {source}"
    base = _model_effective_base(entry)
    if not base:
        return "MODEL_INVALID: 自定义模型必须填写 baseUrl（OpenAI 兼容接口地址）"
    if urlsplit(base).scheme not in ("http", "https"):
        return "MODEL_INVALID: baseUrl 必须是 http(s) 地址"
    pin = entry.get("priceInputPerM")
    pout = entry.get("priceOutputPerM")
    if (pin is None) != (pout is None):
        return "MODEL_PRICE_INCOMPLETE: 输入与输出单价必须同时配置或同时留空"
    for raw in (pin, pout):
        if raw is not None and _model_price_of(raw) is None:
            return "MODEL_PRICE_INVALID: 单价必须是非负有限数字"
    currency = str(entry.get("priceCurrency") or "CNY").strip().upper()
    if len(currency) != 3 or not all("A" <= ch <= "Z" for ch in currency):
        return "MODEL_PRICE_CURRENCY_INVALID: 币种必须是 3 位字母代码（如 CNY / USD）"
    ids = entry.get("fallbackIds")
    if ids is None:
        ids = []
    if not isinstance(ids, list):
        return "MODEL_FALLBACK_INVALID: fallbackIds 必须是数组"
    if len(ids) > 3:
        return "MODEL_FALLBACK_LIMIT: 故障转移链最多 3 个模型"
    seen: set[str] = set()
    for item in ids:
        fid = str(item or "").strip()
        if not fid:
            return "MODEL_FALLBACK_INVALID: 故障转移链包含空 id"
        if fid == str(entry.get("id") or ""):
            return "MODEL_FALLBACK_SELF: 故障转移链不能指向自身"
        if fid in seen:
            return f"MODEL_FALLBACK_DUPLICATE: 故障转移链重复 {fid}"
        seen.add(fid)
    return ""


def _model_candidate(entry: dict) -> tuple[dict | None, str]:
    """单个可调用模型 → 引擎调用配置（主模型与转移目标同形状，不嵌套转移链）"""
    label = str(entry.get("name") or entry.get("modelName") or "未命名模型").strip()
    model_name = str(entry.get("modelName") or "").strip()
    if not model_name:
        return None, f"MODEL_INCOMPLETE: 模型「{label}」缺少服务端模型名"
    base = _model_effective_base(entry)
    if not base:
        return None, f"MODEL_INCOMPLETE: 模型「{label}」缺少 baseUrl"
    key = str(entry.get("apiKey") or "").strip()
    if not key and not _model_is_local(_model_host(base)):
        return None, f"MODEL_KEY_MISSING: 模型「{label}」未配置 API Key"
    cw = _as_int(entry.get("contextWindow"), 0)
    return {
        "modelId": str(entry.get("id") or ""),
        "displayName": label,
        "baseUrl": base,
        "apiKey": key,
        "modelName": model_name,
        "source": str(entry.get("source") or ""),
        "contextWindow": cw if cw > 0 else None,
        "prices": _model_prices_of(entry),
    }, ""


def _model_runtime_config(entry: dict) -> tuple[dict | None, str]:
    """条目 → 引擎调用配置；缺模型名/端点/密钥时给出可读错误码，不静默回落环境变量。

    故障转移目标逐条解析：不可用的跳过并写进 fallbackNotes，不阻断主模型。
    """
    primary, err = _model_candidate(entry)
    if err:
        return None, err
    models = _models_load()
    by_id = {str(m.get("id") or ""): m for m in models}
    fallback_ids = [str(x) for x in (entry.get("fallbackIds") or []) if str(x).strip()]
    fallbacks: list[dict] = []
    notes: list[str] = []
    for fid in fallback_ids[:3]:
        if fid == str(entry.get("id") or ""):
            notes.append("转移目标与主模型重复，已跳过")
            continue
        target = by_id.get(fid)
        if target is None:
            notes.append(f"转移目标 {fid} 不存在（可能已被删除），已跳过")
            continue
        cand, ferr = _model_candidate(target)
        if ferr:
            name = str(target.get("name") or fid)
            notes.append(f"转移目标「{name}」不可用：{ferr}，已跳过")
            continue
        fallbacks.append(cand)
    if len(fallback_ids) > 3:
        notes.append(f"故障转移链共 {len(fallback_ids)} 个目标，仅前 3 个生效")
    primary["fallbacks"] = fallbacks
    primary["fallbackNotes"] = notes
    return primary, ""


def _models_find(model_id: str | None = None) -> tuple[dict | None, str]:
    """按 id 找模型；id 为空时取 isDefault。找不到只报事实，不返回清单首项冒充默认"""
    models = _models_load()
    if model_id:
        hit = next((m for m in models if str(m.get("id") or "") == model_id), None)
        if hit is None:
            return None, f"MODEL_NOT_FOUND: 未找到模型 {model_id}"
        return hit, ""
    return next((m for m in models if bool(m.get("isDefault"))), None), ""


def _model_apply(
    existing: dict | None,
    raw: dict,
    now: str,
    known_models: list[dict] | None = None,
) -> tuple[dict | None, str]:
    """把请求字段合并进条目（不改的字段保持原值，密钥缺省=保留）"""
    entry = dict(existing or {})
    if existing is None:
        entry["id"] = str(raw.get("id") or "").strip() or f"m-{uuid.uuid4().hex[:12]}"
        entry["createdAt"] = now
        entry["priceInputPerM"] = None
        entry["priceOutputPerM"] = None
        entry["priceCurrency"] = "CNY"
        entry["fallbackIds"] = []
    entry["name"] = str(raw.get("name", entry.get("name", "")) or "").strip()
    source = str(raw.get("source", entry.get("source", "openai_compat")) or "").strip()
    # 旧版本的 source 是中文展示名：落库时归一，避免校验把历史配置判死
    entry["source"] = source if source in MODEL_SOURCES else "openai_compat"
    if "baseUrl" in raw:
        entry["baseUrl"] = str(raw.get("baseUrl") or "").strip()
    entry["baseUrl"] = str(entry.get("baseUrl") or "").strip()
    if "modelName" in raw:
        entry["modelName"] = str(raw.get("modelName") or "").strip()
    entry["modelName"] = str(entry.get("modelName") or "").strip()
    if "contextWindow" in raw:
        entry["contextWindow"] = _as_int(raw.get("contextWindow"), 4096)
    entry["contextWindow"] = _as_int(entry.get("contextWindow"), 4096)
    if "isDefault" in raw:
        entry["isDefault"] = bool(raw.get("isDefault"))
    entry["isDefault"] = bool(entry.get("isDefault"))
    if "agentBinding" in raw:
        entry["agentBinding"] = str(raw.get("agentBinding") or "").strip()
    entry["agentBinding"] = str(entry.get("agentBinding") or "")
    if "forceLocal" in raw:
        entry["forceLocal"] = bool(raw.get("forceLocal"))
    entry["forceLocal"] = bool(entry.get("forceLocal"))
    # 计费单价：prices 缺省=不动；null 或两项都空=回到未配置
    if "prices" in raw:
        prices_raw = raw.get("prices")
        if prices_raw is None:
            entry["priceInputPerM"] = None
            entry["priceOutputPerM"] = None
        elif isinstance(prices_raw, dict):
            pin_raw = prices_raw.get("inputPerM")
            pout_raw = prices_raw.get("outputPerM")
            if pin_raw is None and pout_raw is None:
                entry["priceInputPerM"] = None
                entry["priceOutputPerM"] = None
            elif pin_raw is None or pout_raw is None:
                return None, "MODEL_PRICE_INCOMPLETE: 输入与输出单价必须同时配置或同时留空"
            else:
                pin = _model_price_of(pin_raw)
                pout = _model_price_of(pout_raw)
                if pin is None or pout is None:
                    return None, "MODEL_PRICE_INVALID: 单价必须是非负有限数字"
                entry["priceInputPerM"] = pin
                entry["priceOutputPerM"] = pout
            entry["priceCurrency"] = (
                str(prices_raw.get("currency", entry.get("priceCurrency") or "CNY"))
                .strip()
                .upper()
                or "CNY"
            )
        else:
            return None, "MODEL_PRICE_INVALID: prices 必须是对象或 null"
    entry["priceCurrency"] = str(entry.get("priceCurrency") or "CNY").strip().upper() or "CNY"
    # 故障转移链：缺省=不动；[]=清空
    if "fallbackIds" in raw:
        raw_ids = raw.get("fallbackIds")
        if raw_ids is None:
            raw_ids = []
        if not isinstance(raw_ids, list):
            return None, "MODEL_FALLBACK_INVALID: fallbackIds 必须是数组"
        cleaned: list[str] = []
        for item in raw_ids:
            fid = str(item or "").strip()
            if not fid:
                return None, "MODEL_FALLBACK_INVALID: 故障转移链包含空 id"
            if fid in cleaned:
                return None, f"MODEL_FALLBACK_DUPLICATE: 故障转移链重复 {fid}"
            cleaned.append(fid)
        entry["fallbackIds"] = cleaned
    else:
        entry["fallbackIds"] = [
            str(x).strip() for x in (entry.get("fallbackIds") or []) if str(x).strip()
        ]
    if "apiKey" in raw:
        key_raw = raw.get("apiKey")
        if key_raw is not None:
            key_text = str(key_raw)
            if key_text != _MODEL_KEY_PLACEHOLDER:
                entry["apiKey"] = key_text.strip()
    entry["apiKey"] = str(entry.get("apiKey") or "")
    entry["updatedAt"] = now
    err = _model_validate(entry)
    if err:
        return None, err
    known_ids = {
        str(m.get("id") or "")
        for m in (known_models if known_models is not None else _models_load())
    }
    for fid in entry.get("fallbackIds") or []:
        if fid not in known_ids:
            return None, f"MODEL_FALLBACK_NOT_FOUND: 未找到转移目标 {fid}"
    return entry, ""


def _models_normalize_defaults(models: list[dict], keep_id: str | None = None) -> list[dict]:
    """默认模型最多一个；keep_id 指定本次明确设为默认的条目"""
    if keep_id is None:
        keep_id = next(
            (str(m.get("id") or "") for m in models if bool(m.get("isDefault"))),
            None,
        )
    out: list[dict] = []
    for m in models:
        item = dict(m)
        item["isDefault"] = bool(item.get("isDefault")) and str(item.get("id") or "") == keep_id
        out.append(item)
    return out


def _model_error_response(err: str, status_code: int = 400) -> JSONResponse:
    code, _, message = err.partition(": ")
    return JSONResponse(
        {"ok": False, "code": code or "MODEL_INVALID", "message": message or err},
        status_code=status_code,
    )


def _probe_model(base_url: str, api_key: str, model_name: str) -> dict:
    """阻塞式连通性探测（放线程池跑）：POST /chat/completions，max_tokens=1"""
    import urllib.error
    import urllib.request

    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    body = json.dumps(
        {
            "model": model_name,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            resp.read(2048)
        return {"ok": True, "latencyMs": elapsed(), "model": model_name}
    except urllib.error.HTTPError as exc:
        detail = exc.read(200).decode("utf-8", "replace").strip()
        code = "MODEL_AUTH_FAILED" if exc.code in (401, 403) else "MODEL_CONNECTION_FAILED"
        return {
            "ok": False,
            "latencyMs": elapsed(),
            "model": model_name,
            "error": f"{code}: HTTP {exc.code} {detail}".strip(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "latencyMs": elapsed(),
            "model": model_name,
            "error": f"MODEL_CONNECTION_FAILED: {exc}",
        }


# ── 零配置本地模型：探测本机 Ollama（原生 /api/tags） ─────────────
#
# 与 Rust model_detect_local 同一口径：
# - 只接受本机回环地址，路径只允许根或 /v1，避免把渲染层变成任意探测通道
# - 不可达/非 Ollama 响应返回 available=false 与原因，不伪报成功
# - 本机探测强制直连，不受系统/环境代理影响

OLLAMA_DEFAULT_BASE = "http://127.0.0.1:11434"
_OLLAMA_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def _ollama_root(raw: str) -> tuple[str, str]:
    """归一化探测地址：返回 (root, error)，error 非空时 root 不可用。"""
    candidate = str(raw or "").strip() or OLLAMA_DEFAULT_BASE
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError as exc:
        return "", f"LOCAL_MODEL_INVALID: 地址无法解析（{exc}）"
    if parts.scheme not in ("http", "https"):
        return "", "LOCAL_MODEL_INVALID: 地址必须以 http:// 或 https:// 开头"
    if parts.username or parts.password:
        return "", "LOCAL_MODEL_INVALID: 地址不能包含用户信息"
    if parts.query or parts.fragment:
        return "", "LOCAL_MODEL_INVALID: 地址不能包含查询串或片段"
    host = (parts.hostname or "").lower()
    if host not in _OLLAMA_LOOPBACK_HOSTS:
        return "", f"LOCAL_MODEL_INVALID: 自动检测只支持本机回环地址（当前主机 {host or '空'}）"
    path = parts.path.rstrip("/")
    if path and path.lower() != "/v1":
        return "", "LOCAL_MODEL_INVALID: 地址只接受服务根或 /v1 结尾"
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{parts.scheme}://{authority}", ""


def _probe_ollama_root(root: str) -> dict:
    """阻塞式探测（放线程池跑）：GET {root}/api/tags。"""
    import urllib.error
    import urllib.request

    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    result = {
        "supported": True,
        "available": False,
        "endpoint": root,
        "openaiBaseUrl": f"{root}/v1",
        "latencyMs": 0,
        "models": [],
    }
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        request = urllib.request.Request(f"{root}/api/tags", method="GET")
        with opener.open(request, timeout=3) as resp:
            body = resp.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        result["latencyMs"] = elapsed()
        result["error"] = f"LOCAL_MODEL_NOT_RUNNING: Ollama 返回 HTTP {exc.code}"
        return result
    except Exception as exc:
        result["latencyMs"] = elapsed()
        result["error"] = f"LOCAL_MODEL_NOT_RUNNING: 无法连接 {root}（{exc}）"
        return result
    result["latencyMs"] = elapsed()
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        result["error"] = "LOCAL_MODEL_NOT_RUNNING: 返回内容不是合法 JSON"
        return result
    tags = data.get("models") if isinstance(data, dict) else None
    if not isinstance(tags, list):
        result["error"] = "LOCAL_MODEL_NOT_RUNNING: 返回内容没有 models 数组"
        return result
    seen: set[str] = set()
    models: list[dict] = []
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        name = str(tag.get("name") or tag.get("model") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        item = {"name": name, "sizeBytes": _as_int(tag.get("size"), 0)}
        modified = str(tag.get("modified_at") or "").strip()
        if modified:
            item["modifiedAt"] = modified
        models.append(item)
    result["available"] = True
    result["models"] = models
    return result



@app.get("/api/usage")
async def usage_get(period: str = "monthly", sub: str = ""):
    """模型用量（真实 token 台账）：daily=今日 / monthly=本月 / total=全部。

    仅供浏览器直连模式读取（无 Rust 后端时的联调通道）；
    Tauri 模式的权威数据在 Rust model_usage 表（按 usage 事件持久化）。
    台账为进程内累计，引擎重启清零。JWT 非管理员只能看到自己的用量。"""
    p = period if period in ("daily", "monthly", "total") else "total"
    subject = _usage_scope(sub)
    return JSONResponse(
        {"ok": True, "period": p, "subject": subject, "rows": USAGE_LEDGER.aggregate(p, subject)}
    )


@app.get("/api/usage/report")
async def usage_report_get(period: str = "monthly", sub: str = ""):
    """用量报表（按日趋势 + 明细 + 用户汇总 + 总计）：与 Rust `usage_report` 同一口径。"""
    p = period if period in ("daily", "monthly", "total") else "total"
    subject = _usage_scope(sub)
    models = USAGE_LEDGER.aggregate(p, subject)
    return JSONResponse(
        {
            "ok": True,
            "period": p,
            "subject": subject,
            "generatedAtMs": int(time.time() * 1000),
            "days": USAGE_LEDGER.days(p, subject),
            "models": models,
            "users": USAGE_LEDGER.users(p, subject),
            "totals": USAGE_LEDGER.totals(p, models),
        }
    )


@app.get("/api/tools")
async def tools_get():
    """内置工具清单：直接读 Toolkit 注册表，不在别处再抄一份。

    桌面端「扩展 → 内置工具」以此为准；Rust 侧的声明清单由契约测试与此对齐
    （引擎没跑时桌面端仍能显示声明值，但两边漂移会让测试先红）。"""
    try:
        toolkit = build_toolkit(WORK_DIR)
    except Exception as exc:  # agentscope 缺失等：如实报错，不返回空清单装作「没有工具」
        return JSONResponse(
            {"ok": False, "code": "TOOLS_LIST_FAILED", "detail": str(exc)}, status_code=500
        )
    tools = []
    for name, reg in toolkit.tools.items():
        desc = ""
        try:
            schema = getattr(reg, "json_schema", None) or {}
            desc = str((schema.get("function") or {}).get("description") or "").strip()
        except Exception:
            desc = ""
        mcp = getattr(reg, "mcp_name", None)
        tools.append({
            "id": name,
            "name": name,
            "desc": desc,
            "source": "mcp" if mcp else "builtin",
        })
    tools.sort(key=lambda t: t["id"])
    return JSONResponse({"ok": True, "count": len(tools), "tools": tools})


def _models_view() -> dict:
    models = _models_load()
    default_id = next(
        (str(m.get("id") or "") for m in models if bool(m.get("isDefault"))),
        None,
    )
    return {
        "ok": True,
        "path": str(_models_config_path()),
        "models": [_model_public(m) for m in models],
        "defaultId": default_id,
    }


@app.get("/api/models")
async def models_list():
    """模型清单（数据源=models.json；密钥只回报 hasApiKey）"""
    return JSONResponse(_models_view())


@app.post("/api/models")
async def models_create(request: Request):
    """新增模型（密钥保存在本机 models.json，读取视图不回传明文）"""
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "code": "MODEL_INVALID", "message": "无效 JSON"}, status_code=400)
    if not isinstance(raw, dict):
        return JSONResponse({"ok": False, "code": "MODEL_INVALID", "message": "请求体必须是对象"}, status_code=400)
    now = _iso_utc(int(time.time() * 1000))
    models = _models_load()
    entry, err = _model_apply(None, raw, now, models)
    if err:
        return _model_error_response(err)
    models.append(entry)
    models = _models_normalize_defaults(models, entry["id"] if entry.get("isDefault") else None)
    try:
        _models_save(models)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "code": "MODEL_WRITE_FAILED", "message": f"写入模型配置失败：{exc}"},
            status_code=500,
        )
    saved = next((m for m in models if m.get("id") == entry.get("id")), entry)
    return JSONResponse({"ok": True, "model": _model_public(saved)})


@app.post("/api/models/update")
async def models_update(request: Request):
    """更新模型（apiKey 缺省/占位符=保留，空串=清除；其余字段按请求覆盖）"""
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "code": "MODEL_INVALID", "message": "无效 JSON"}, status_code=400)
    if not isinstance(raw, dict):
        return JSONResponse({"ok": False, "code": "MODEL_INVALID", "message": "请求体必须是对象"}, status_code=400)
    model_id = str(raw.get("id") or "").strip()
    if not model_id:
        return JSONResponse({"ok": False, "code": "MODEL_INVALID", "message": "缺少模型 id"}, status_code=400)
    models = _models_load()
    idx = next((i for i, m in enumerate(models) if str(m.get("id") or "") == model_id), None)
    if idx is None:
        return _model_error_response(f"MODEL_NOT_FOUND: 未找到模型 {model_id}", status_code=404)
    now = _iso_utc(int(time.time() * 1000))
    entry, err = _model_apply(models[idx], raw, now, models)
    if err:
        return _model_error_response(err)
    models[idx] = entry
    models = _models_normalize_defaults(models, model_id if entry.get("isDefault") else None)
    try:
        _models_save(models)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "code": "MODEL_WRITE_FAILED", "message": f"写入模型配置失败：{exc}"},
            status_code=500,
        )
    saved = next((m for m in models if m.get("id") == model_id), entry)
    return JSONResponse({"ok": True, "model": _model_public(saved)})


@app.post("/api/models/delete")
async def models_delete(request: Request):
    """删除模型（只按 id 移除指定条目，不动清单内其他模型）"""
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "code": "MODEL_INVALID", "message": "无效 JSON"}, status_code=400)
    model_id = str((raw or {}).get("id") or "").strip()
    if not model_id:
        return JSONResponse({"ok": False, "code": "MODEL_INVALID", "message": "缺少模型 id"}, status_code=400)
    models = _models_load()
    if not any(str(m.get("id") or "") == model_id for m in models):
        return _model_error_response(f"MODEL_NOT_FOUND: 未找到模型 {model_id}", status_code=404)
    next_models: list[dict] = []
    for m in models:
        if str(m.get("id") or "") == model_id:
            continue
        item = dict(m)
        ids = item.get("fallbackIds")
        if isinstance(ids, list) and model_id in [str(x) for x in ids]:
            item["fallbackIds"] = [x for x in ids if str(x) != model_id]
        next_models.append(item)
    try:
        _models_save(_models_normalize_defaults(next_models))
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "code": "MODEL_WRITE_FAILED", "message": f"写入模型配置失败：{exc}"},
            status_code=500,
        )
    return JSONResponse({"ok": True})


@app.post("/api/models/default")
async def models_set_default(request: Request):
    """设为默认模型（Auto 在浏览器直连模式下解析到它）"""
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "code": "MODEL_INVALID", "message": "无效 JSON"}, status_code=400)
    model_id = str((raw or {}).get("id") or "").strip()
    if not model_id:
        return JSONResponse({"ok": False, "code": "MODEL_INVALID", "message": "缺少模型 id"}, status_code=400)
    models = _models_load()
    if not any(str(m.get("id") or "") == model_id for m in models):
        return _model_error_response(f"MODEL_NOT_FOUND: 未找到模型 {model_id}", status_code=404)
    now = _iso_utc(int(time.time() * 1000))
    updated: list[dict] = []
    for m in models:
        item = dict(m)
        item["isDefault"] = str(item.get("id") or "") == model_id
        if item["isDefault"]:
            item["updatedAt"] = now
        updated.append(item)
    try:
        _models_save(updated)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "code": "MODEL_WRITE_FAILED", "message": f"写入模型配置失败：{exc}"},
            status_code=500,
        )
    return JSONResponse({"ok": True, "defaultId": model_id})


@app.post("/api/models/test")
async def models_test(request: Request):
    """连通性测试：传 id 时用已存条目补全端点/密钥；表单未保存时可直接传字段"""
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "code": "MODEL_INVALID", "message": "无效 JSON"}, status_code=400)
    if not isinstance(raw, dict):
        return JSONResponse({"ok": False, "code": "MODEL_INVALID", "message": "请求体必须是对象"}, status_code=400)
    model_id = str(raw.get("id") or "").strip()
    stored: dict | None = None
    if model_id:
        stored, err = _models_find(model_id)
        if err:
            return _model_error_response(err, status_code=404)
    base = str(raw.get("baseUrl") or "").strip() or _model_effective_base(stored or {})
    model_name = (
        str(raw.get("modelName") or "").strip()
        or str((stored or {}).get("modelName") or "").strip()
    )
    key_raw = raw.get("apiKey")
    if key_raw in (None, "", _MODEL_KEY_PLACEHOLDER):
        api_key = str((stored or {}).get("apiKey") or "").strip()
    else:
        api_key = str(key_raw).strip()
    if not base or not model_name:
        return _model_error_response("MODEL_INCOMPLETE: baseUrl 与服务端模型名不能为空")
    result = await asyncio.to_thread(_probe_model, base, api_key, model_name)
    return JSONResponse(result)


@app.post("/api/models/detect-local")
async def models_detect_local(request: Request):
    """零配置本地模型：真实探测本机 Ollama（只接受回环地址）。"""
    try:
        raw = await request.json()
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    root, err = _ollama_root(str(raw.get("baseUrl") or ""))
    if err:
        return _model_error_response(err, status_code=400)
    result = await asyncio.to_thread(_probe_ollama_root, root)
    return JSONResponse(result)


# ── 旧版整表读写接口（兼容旧渲染层；已改为按 id 合并，不再整表覆盖） ──

@app.get("/api/models/config")
async def models_config_get():
    """读取模型清单（旧路径）：与 /api/models 同一份数据、同样脱敏"""
    return JSONResponse(_models_view())


@app.post("/api/models/config")
async def models_config_set(request: Request):
    """旧版整表写入：按 id 逐条合并，缺省字段保持原值，密钥不回写占位符"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "code": "MODEL_INVALID", "message": "无效 JSON"}, status_code=400)
    incoming = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(incoming, list):
        return JSONResponse({"ok": False, "code": "MODEL_INVALID", "message": "models 必须为数组"}, status_code=400)
    models = _models_load()
    now = _iso_utc(int(time.time() * 1000))
    for raw in incoming:
        if not isinstance(raw, dict):
            continue
        model_id = str(raw.get("id") or "").strip()
        idx = next(
            (i for i, m in enumerate(models) if model_id and str(m.get("id") or "") == model_id),
            None,
        )
        entry, err = _model_apply(models[idx] if idx is not None else None, raw, now, models)
        if err:
            return _model_error_response(err)
        if idx is None:
            models.append(entry)
        else:
            models[idx] = entry
    models = _models_normalize_defaults(models)
    try:
        _models_save(models)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "code": "MODEL_WRITE_FAILED", "message": f"写入模型配置失败：{exc}"},
            status_code=500,
        )
    return JSONResponse(_models_view())


@app.post("/api/knowledge/search")
async def knowledge_search(request: Request):
    """知识库检索自测：只允许检索本机知识库根目录下的库（与执行面同一索引）"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "无效 JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "message": "请求体必须是对象"}, status_code=400)
    root = (_awareness_home().parent.parent / "knowledge").resolve()
    raw_dir = str(payload.get("dir") or "").strip()
    try:
        target = Path(raw_dir).resolve()
    except Exception:
        target = None
    if target is None or not target.is_dir() or target.parent != root or not (target / "meta.json").is_file():
        return JSONResponse(
            {"ok": False, "code": "KNOWLEDGE_DIR_INVALID", "message": "知识库目录不在本机知识库根目录下"},
            status_code=400,
        )
    query = str(payload.get("query") or "").strip()
    if not query:
        return JSONResponse({"ok": False, "code": "KNOWLEDGE_QUERY_EMPTY", "message": "检索词为空"}, status_code=400)
    base = kn.KnowledgeBase(
        id=target.name,
        name=str(payload.get("name") or target.name),
        description="",
        dir=str(target),
        doc_count=0,
    )
    runtime = kn.KnowledgeRuntime(bases=[base])
    result = runtime.search(query, "", payload.get("topK") or kn.DEFAULT_TOP_K)
    return JSONResponse({"ok": True, **result})


@app.get("/api/awareness")
async def awareness_get_view():
    """意识视图（`awareness:get`）：画像 + 目录 + 四个能力文件真实内容
    （缺失文件按默认模板初始化落盘，已存在的绝不覆盖）"""
    home = _awareness_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "code": "AWARENESS_DIR_FAILED", "message": f"意识目录不可访问：{exc}"},
            status_code=500,
        )
    return JSONResponse(_awareness_view(home))


@app.post("/api/awareness")
async def awareness_update(request: Request):
    """保存画像与能力文件（`awareness:update`）：先整体校验再落盘，避免半成品状态"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "无效 JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse(
            {"ok": False, "code": "AWARENESS_FILE_INVALID", "message": "意识补丁必须是对象"}, status_code=400
        )
    home = _awareness_home()
    files = payload.get("files") or {}
    if not isinstance(files, dict):
        return JSONResponse(
            {"ok": False, "code": "AWARENESS_FILE_INVALID", "message": "files 必须是文件名到正文的对象"},
            status_code=400,
        )
    for name in files:
        if Path(str(name)).name != str(name) or str(name) not in AWARENESS_FILE_NAMES:
            return JSONResponse(
                {
                    "ok": False,
                    "code": "AWARENESS_FILE_INVALID",
                    "message": f"意识文件「{name}」不在可编辑清单内，仅支持 {' / '.join(AWARENESS_FILE_NAMES)}",
                },
                status_code=400,
            )
    style = payload.get("style")
    if style is not None and str(style).strip() not in AWARENESS_STYLES:
        return JSONResponse(
            {
                "ok": False,
                "code": "AWARENESS_STYLE_INVALID",
                "message": f"协作风格「{style}」非法，仅支持 {' / '.join(AWARENESS_STYLES)}",
            },
            status_code=400,
        )
    try:
        home.mkdir(parents=True, exist_ok=True)
        profile = _profile_load(home)
        if isinstance(payload.get("mode"), (bool, str)):
            profile["mode"] = payload["mode"] is True or str(payload["mode"]).strip() == "true"
        if isinstance(payload.get("autoMemory"), (bool, str)):
            profile["autoMemory"] = (
                payload["autoMemory"] is True or str(payload["autoMemory"]).strip() == "true"
            )
        if style is not None:
            profile["style"] = str(style).strip()
        for name, content in files.items():
            (home / str(name)).write_text(str(content or ""), encoding="utf-8")
        profile["updatedAt"] = _iso_utc(int(time.time() * 1000))
        _profile_save(home, profile)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "code": "AWARENESS_SAVE_FAILED", "message": f"意识内容保存失败：{exc}"},
            status_code=500,
        )
    return JSONResponse(_awareness_view(home))


@app.get("/api/awareness/memories")
async def awareness_memories_get():
    """长期记忆条目列表（`awareness:memories`），来自 MEMORY.md 的结构化行"""
    try:
        rows = _memories_load(_awareness_home())
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "code": "AWARENESS_LOAD_FAILED", "message": f"长期记忆读取失败：{exc}"},
            status_code=500,
        )
    return JSONResponse({"ok": True, "entries": rows})


@app.post("/api/awareness/memories")
async def awareness_memory_add(request: Request):
    """新增长期记忆条目（`awareness:memoryAdd`）：只追加行，MEMORY.md 里用户自己的正文原样保留"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "无效 JSON"}, status_code=400)
    content = " ".join(str(payload.get("content") or "").split())
    if not content:
        return JSONResponse(
            {"ok": False, "code": "AWARENESS_MEMORY_EMPTY", "message": "记忆条目内容不能为空"},
            status_code=400,
        )
    source = str(payload.get("source") or "").strip()
    if source not in MEMORY_SOURCES:
        source = "manual"
    home = _awareness_home()
    try:
        existing = [e["id"] for e in _memories_load(home)]
        now_ms = int(time.time() * 1000)
        entry = {
            "id": _memory_id(now_ms, existing),
            "content": content,
            "source": source,
            "createdAt": _iso_utc(now_ms),
        }
        p = home / "MEMORY.md"
        raw = p.read_text(encoding="utf-8") if p.exists() else ""
        body = raw.rstrip("\n")
        merged = (body + "\n" if body else "") + _memory_line(entry) + "\n"
        p.write_text(merged, encoding="utf-8")
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "code": "AWARENESS_SAVE_FAILED", "message": f"长期记忆写入失败：{exc}"},
            status_code=500,
        )
    return JSONResponse({"ok": True, "entry": entry})


@app.post("/api/awareness/memories/delete")
async def awareness_memory_delete(request: Request):
    """删除指定长期记忆条目（`awareness:memoryDelete`）：只删点名的那一行，未命中如实报错"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "无效 JSON"}, status_code=400)
    target = str(payload.get("id") or "").strip()
    home = _awareness_home()
    p = home / "MEMORY.md"
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "code": "AWARENESS_LOAD_FAILED", "message": f"长期记忆读取失败：{exc}"},
            status_code=500,
        )
    kept = []
    removed = False
    for line in raw.splitlines():
        entry = _parse_memory_line(line)
        if entry and entry["id"] == target:
            removed = True
            continue
        kept.append(line)
    if not removed:
        return JSONResponse(
            {
                "ok": False,
                "code": "AWARENESS_MEMORY_NOT_FOUND",
                "message": f"记忆条目「{target}」已不存在，可能已被删除",
            },
            status_code=404,
        )
    try:
        p.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "code": "AWARENESS_SAVE_FAILED", "message": f"长期记忆写入失败：{exc}"},
            status_code=500,
        )
    return JSONResponse({"ok": True, "id": target})


# ── 安全工作环境：隔离执行目录与磁盘用量 ─────────────────────────
# 目录布局与 Rust `security::environment` 同源：{data_home}/workspaces/{ws}/environment
# 引擎的 WORK_DIR 即某个工作空间目录，因此这里只统计它自己那一份。

ENV_DIR_NAME = "environment"


def env_root() -> Path:
    return WORK_DIR / ENV_DIR_NAME


def _task_work_dir(task_id: str) -> Path:
    """安全工作环境开启且用户未自选工作目录时的任务隔离目录。

    对话记录仍写在 {WORK_DIR}/sessions，工作产物按会话落盘，
    二者都不受「清理工作环境文件」影响。"""
    d = env_root() / task_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _dir_stat(root: Path) -> tuple[int, int]:
    """递归统计目录内文件的字节数与个数。

    符号链接一律不跟随（防环路，也防顺着链接统计到目录树之外）；
    单个条目读不到就跳过，不中断整体统计。"""
    total_bytes = 0
    total_files = 0
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    stack.append(Path(entry.path))
                else:
                    total_bytes += entry.stat().st_size
                    total_files += 1
            except OSError:
                continue
    return total_bytes, total_files


def _env_usage_view() -> dict:
    root = env_root()
    landed = root.is_dir()
    size, files = _dir_stat(root) if landed else (0, 0)
    return {
        "supported": True,
        "bytes": size,
        "files": files,
        "workspaceCount": 1 if landed else 0,
        "workspaces": (
            [{"workspaceId": WORK_DIR.name, "path": str(root), "bytes": size, "files": files}]
            if landed
            else []
        ),
        "rootPath": str(WORK_DIR),
        "envDirName": ENV_DIR_NAME,
    }


@app.get("/api/sandbox/environment/usage")
async def sandbox_environment_usage():
    """工作环境文件用量（`sandbox:envUsage`）——磁盘实测，目录未落地即 0"""
    try:
        return JSONResponse(_env_usage_view())
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "code": "SANDBOX_ENV_USAGE_FAILED", "message": f"工作环境用量统计失败：{exc}"},
            status_code=500,
        )


@app.post("/api/sandbox/environment/clear")
async def sandbox_environment_clear():
    """清理工作环境文件（`sandbox:envClear`）：只删 environment/ 内的内容，目录本身保留。

    数据保护边界：对话记录（sessions/）、工作产物（files/）以及 environment/ 之外的任何
    路径都不在删除范围内；符号链接条目一律不删。"""
    root = env_root()
    if not root.is_dir():
        return JSONResponse({"freedBytes": 0, "freedFiles": 0, "cleared": [], "failed": []})
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        return JSONResponse(
            {
                "ok": False,
                "code": "SANDBOX_ENV_CLEAR_FAILED",
                "message": f"无法读取工作环境目录：{exc}",
            },
            status_code=500,
        )
    before_size, before_files = _dir_stat(root)
    failed: list = []
    for entry in entries:
        path = Path(entry.path)
        try:
            if path.is_symlink():
                failed.append({
                    "path": str(path),
                    "error": "SANDBOX_ENV_CLEAR_SKIPPED_SYMLINK: 符号链接不删除",
                })
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            failed.append({"path": str(path), "error": f"SANDBOX_ENV_CLEAR_FAILED: {exc}"})
    after_size, after_files = _dir_stat(root)
    freed_size = max(before_size - after_size, 0)
    freed_files = max(before_files - after_files, 0)
    cleared = (
        [{"workspaceId": WORK_DIR.name, "freedBytes": freed_size, "freedFiles": freed_files}]
        if freed_size or freed_files
        else []
    )
    return JSONResponse({
        "freedBytes": freed_size,
        "freedFiles": freed_files,
        "cleared": cleared,
        "failed": failed,
    })


@app.post("/api/files/open-folder")
async def files_open_folder(request: Request):
    """在系统文件管理器中打开目录（缺省意识目录；不存在时自动创建）"""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    raw = str(payload.get("path") or "").strip() or str(_awareness_home())
    p = Path(raw).expanduser()
    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            return JSONResponse({"ok": False, "message": "目录不存在且无法创建"}, status_code=404)
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception as exc:
        return JSONResponse({"ok": False, "message": f"打开文件夹失败: {exc}"}, status_code=500)
    return JSONResponse({"ok": True, "path": str(p)})


# ── IM 频道接口（`im:*` 通道的引擎侧权威） ──────────────────────


def _im_error(exc: Exception) -> JSONResponse:
    code = getattr(exc, "code", "IM_CHANNEL_ERROR")
    detail = getattr(exc, "detail", None) or str(exc)
    return JSONResponse({"ok": False, "code": code, "message": detail}, status_code=400)


@app.get("/api/im/channels")
async def im_channels_view():
    """通道清单 + 每个通道的真实状态（是否已绑定、连没连上、重连次数、最近错误）。

    未实现长连接的通道也在清单里，但 supported=false 且带原因——界面据此显示禁用态，
    而不是前端另抄一份「支持哪些 IM」。"""
    return JSONResponse(
        {
            "ok": True,
            "channels": [r.view() for r in im_channels.all_runtimes()],
            "sessions": im_channels.im_sessions()[:50],
        }
    )


@app.get("/api/im/preferences")
async def im_preferences_get():
    """IM 频道偏好当前值（引擎持久化，设置页读的就是这个真实值）"""
    return JSONResponse({"ok": True, "preferences": im_channels.load_preferences()})


@app.post("/api/im/preferences")
async def im_preferences_set(request: Request):
    """写 IM 频道偏好：未知键/非布尔一律报 IM_PREFERENCE_INVALID，不做「收下但不生效」"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "code": "VALIDATION_ERROR", "message": "无效 JSON"}, status_code=400)
    patch = payload.get("preferences") if isinstance(payload.get("preferences"), dict) else payload
    try:
        prefs = im_channels.save_preferences(patch or {})
    except Exception as exc:
        return _im_error(exc)
    return JSONResponse({"ok": True, "preferences": prefs})


@app.post("/api/im/{kind}/config")
async def im_configure(kind: str, request: Request):
    """绑定通道：先真实校验凭据（Slack 走 auth.test + apps.connections.open 两次往返），
    通过才落盘并起长连接；失败什么都不写，返回带原因的 IM_AUTH_FAILED 等错误码"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "code": "VALIDATION_ERROR", "message": "无效 JSON"}, status_code=400)
    creds = payload.get("credentials") if isinstance(payload.get("credentials"), dict) else payload
    try:
        account = await im_channels.runtime_of(kind).configure(creds or {})
    except Exception as exc:
        return _im_error(exc)
    return JSONResponse({"ok": True, "account": account})


@app.post("/api/im/{kind}/start")
async def im_start(kind: str):
    runtime = None
    try:
        runtime = im_channels.runtime_of(kind)
        await runtime.start()
    except Exception as exc:
        return _im_error(exc)
    return JSONResponse({"ok": True, "state": runtime.state})


@app.post("/api/im/{kind}/stop")
async def im_stop(kind: str):
    try:
        runtime = im_channels.runtime_of(kind)
        await runtime.stop()
    except Exception as exc:
        return _im_error(exc)
    return JSONResponse({"ok": True, "state": runtime.state})


@app.post("/api/im/{kind}/forget")
async def im_forget(kind: str):
    """解绑：停连接并只删除该通道的凭据登记；IM 会话台账保留（历史属于用户）"""
    try:
        runtime = im_channels.runtime_of(kind)
        await runtime.forget()
    except Exception as exc:
        return _im_error(exc)
    return JSONResponse({"ok": True, "state": runtime.state})


@app.post("/api/im/{kind}/send")
async def im_send(kind: str, request: Request):
    """主动发一条文本（定时任务结果推送用）。

    通道差异如实透传：Slack 可按频道 ID 直接发；钉钉/企微/飞书只能回贴到
    已有会话（需要对方先发过消息且回贴窗口未过期），发不出去就返回真实错误码，
    不假装已送达。
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "code": "VALIDATION_ERROR", "message": "无效 JSON"}, status_code=400)
    target = str(payload.get("target") or "").strip()
    text = str(payload.get("text") or "").strip()
    if not target:
        return JSONResponse(
            {"ok": False, "code": "IM_TARGET_MISSING", "message": "未提供目标会话"},
            status_code=400,
        )
    if not text:
        return JSONResponse(
            {"ok": False, "code": "IM_TEXT_MISSING", "message": "推送内容为空"},
            status_code=400,
        )
    if len(text) > 4000:
        return JSONResponse(
            {"ok": False, "code": "IM_TEXT_TOO_LONG", "message": "推送内容超过 4000 字"},
            status_code=400,
        )
    try:
        runtime = im_channels.runtime_of(kind)
        if not runtime.channel.supported:
            raise im_channels.ImError("IM_UNSUPPORTED", runtime.channel.unsupported_reason)
        if not runtime.creds():
            raise im_channels.ImError("IM_NOT_CONFIGURED", f"{runtime.channel.label} 尚未配置凭据")
        await runtime.channel.send(runtime.creds(), target, text)
    except Exception as exc:
        return _im_error(exc)
    return JSONResponse(
        {
            "ok": True,
            "kind": kind,
            "target": target,
            "sentAtMs": int(time.time() * 1000),
        }
    )


# ── 入口 ───────────────────────────────────────────────────────

def main():
    global WORK_DIR
    parser = argparse.ArgumentParser(description="TongTian Agent Engine (agentscope)")
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--work-dir", type=str, default=".")
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help=(
            "绑定地址；非回环地址需同时设置 TT_ALLOW_REMOTE_BIND=1 与 "
            "TT_ENGINE_TOKEN 或 TT_ENGINE_JWT_SECRET"
        ),
    )
    parser.add_argument(
        "--web-dir",
        type=str,
        default="",
        help="可选：前端构建产物目录（含 index.html）；也可用 TT_WEB_DIR",
    )
    args = parser.parse_args()

    WORK_DIR = Path(args.work_dir).resolve()
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    # 加载项目根 .env（引擎脚本位于 <root>/engine/，向上一级）
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    bind_error = service_bind_error(
        args.host,
        os.environ.get("TT_ALLOW_REMOTE_BIND", ""),
        os.environ.get("TT_ENGINE_TOKEN", ""),
        _engine_jwt_secret(),
    )
    if bind_error:
        parser.error(bind_error)

    web_raw = (args.web_dir or os.environ.get("TT_WEB_DIR") or "").strip()
    web_root: Path | None = None
    if web_raw:
        try:
            web_root = mount_web(app, web_raw)
        except RuntimeError as exc:
            parser.error(str(exc))

    print(
        f"[engine] tongtian-agent-engine v{ENGINE_VERSION} on {args.host}:{args.port} "
        f"(provider={os.environ.get('TT_PROVIDER', 'dashscope')}, "
        f"model={os.environ.get('TT_MODEL', 'qwen-plus')}, "
        f"key={'yes' if os.environ.get('TT_API_KEY') else 'NO'}, "
        f"work-dir={WORK_DIR}, "
        f"web={web_root or 'off'}, "
        f"service-token={'yes' if os.environ.get('TT_ENGINE_TOKEN') else 'NO'}, "
        f"jwt={'yes' if _engine_jwt_secret() else 'NO'}, "
        f"audit-log={'yes' if os.environ.get('TT_ENGINE_AUDIT_LOG') else 'memory'})",
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
