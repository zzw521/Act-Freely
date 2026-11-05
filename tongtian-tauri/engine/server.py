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
from typing import Any, Callable
from urllib.parse import urlsplit
import urllib.request as urlreq

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
import wechat_channel  # noqa: E402,F401  （导入即注册个人微信通道：官方 iLink 协议，长轮询 HTTP）
import yuanbao_channel  # noqa: E402,F401  （导入即注册腾讯元宝通道：官方 WS + protobuf）
import runtime_context as rc  # noqa: E402  （执行面 P0：意识/技能/附件 → 模型上下文）
import mcp_runtime as mr  # noqa: E402  （执行面 P0：连接器工具 → Toolkit）
import mcp_session_runtime as msr  # noqa: E402  （D-40 第 5 项：连接器跨轮常驻连接）
import memory_tools as mt  # noqa: E402  （D-35：长期记忆检索/读取/写入工具，桌面通道才注册）
import context_budget as cb  # noqa: E402  （P1 上下文效率：迭代上限 + 压缩阈值）
import tool_search as tsr  # noqa: E402  （P1 工具效率：MCP 工具按需搜索载入）
import hooks_runtime as hr  # noqa: E402  （执行面 P0：Hooks 事件 → 本机网关执行）
import sandbox_runtime as sr  # noqa: E402  （执行面 P1：沙箱策略 → 工具/文件真实约束）
import subagent_runtime as sar  # noqa: E402  （多 Agent 第一层：本地子 Agent 派发与结果汇总）
import checkpoint_runtime as ckpt  # noqa: E402  （任务检查点：模型调用前快照 + 副作用幂等台账）
import tool_grants as tg  # noqa: E402  （执行面 P1：受控命令执行/联网获取，默认不注册）
import extension_decls as exd  # noqa: E402  （D-08：扩展声明了什么能力 ⇒ 现在能不能用，一份可核对的清单）
import web_search as ws  # noqa: E402  （联网搜索：受同一份联网授权约束，默认不注册）
import computer_use as cu  # noqa: E402  （执行面 P1/P2：Windows 电脑操控，仅桌面壳可执行）
import browser_use as bu  # noqa: E402  （执行面 P2：受控浏览器会话，CDP 操作由桌面壳放行）
import knowledge_runtime as kn  # noqa: E402  （本地知识库 RAG：索引 + 检索工具，无可用库时不注册）
import privacy_guard as pg  # noqa: E402  （出站内容脱敏：模型调用前处理文本，off/mask/block）
import tool_approval as tap  # noqa: E402  （逐工具审批：副作用工具执行前经本机网关确认）
import expert_runtime as er  # noqa: E402  （专家团队：负责人提示词、每专家模型与子 Agent 路由）
import terminal_runtime as trm  # noqa: E402  （终端 AI+：真实 shell 会话 + 命令建议）
import acp_client as acpm  # noqa: E402  （ACP 客户端：把编码子任务委派给外部 Agent，默认不注册）
import data_paths as dp  # noqa: E402  （D-43：引擎数据根与数据文件路径的唯一解析处）
import usage_store as us  # noqa: E402  （D-43：用量台账落盘 + 调用来源标记，合并视图靠它去重）
import gateway_token as gwt  # noqa: E402  （M1.3 网关模式：access token → 短期网关令牌，仅存内存）
import ticket_model_channel as tmc  # noqa: E402  （D-48.7：工单执行体的模型通道，路由只取它的用量口径）
import ticket_worker_service as tws  # noqa: E402  （D-48.7：工单执行体的进程内装配 + 端点面，路由只做薄包装）
import ticket_worker_adapter as twa  # noqa: E402  （D-48.4b：主会话读取件复用执行体的分页预算常量与夹取口径，数字只有一份来源）
import artifact_history_runtime as ahr  # noqa: E402  （D-26：产物版本历史；主会话写盘前经它留一版可回滚的旧内容）
import write_staging_runtime as wsr  # noqa: E402  （D-03.5：文件写入先落暂存区，用户在界面看过 diff 批准后才合并）
import crash_runtime as crt  # noqa: E402  （D-28 后半：崩溃现场本地留存——引擎的 stdout/stderr 被桌面壳丢掉了，现场必须落盘）
import telemetry_runtime as tlm  # noqa: E402  （D-28 前半：可选遥测，默认关、开启要确认词、字段白名单、看得见将发送什么）
import routes_lanes as rln  # noqa: E402  （#164 接缝：各窗口的引擎端点写 routes_<domain>.py，不再打开本文件）
import routes_knowledge  # noqa: E402  （W2：知识库/技能/产物历史）
import routes_artifact  # noqa: E402  （W4：产物与发布）
import routes_workspace  # noqa: E402  （W3：工作区后端/快照/存储）
import routes_exec  # noqa: E402  （W4：执行面/沙箱/写暂存）
import routes_connect  # noqa: E402  （W5：MCP/IM/定时/浏览器）
import routes_orchestrate  # noqa: E402  （W6：团队/编排/成员检查点）
import routes_voice  # noqa: E402  （W7：语音链路）
import toolkit_lanes as tl  # noqa: E402  （#164 接缝：模式作用域工具的条件注册集中声明处）

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
MAX_IN_MEMORY_SESSIONS = 500      # 进程内会话上限；超出时淘汰已结束的旧会话

# 连接器跨轮常驻连接枢纽（D-40 第 5 项）：同一 task_id 的多轮共用同一批客户端，
# 会话被淘汰或引擎关停时才收进程。绑定事件循环在 lifespan 里 start() 时完成，
# 在那之前（或跑在别的循环上）attach 会如实降级成本轮自建的一次性连接。
MCP_HUB = msr.McpSessionHub()


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
        # 取消的成因码（USER_CANCELLED / BUDGET_TASK_EXCEEDED）：Rust 的引擎拦停与用户
        # 点「停止」以前都只发 task_id，落到引擎侧同一条路，界面分不清是谁停的（D-14.5）。
        self.cancel_reason = ""
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
        # 文件访问档位（ask_every_time/auto_approve/full_access）：与 work_dir 配套，
        # 决定这一轮能不能越出绑定目录写文件、能不能删文件；approve 重跑沿用
        self.file_permission: str = ""
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
        # 服务化归属（D-02 组织隔离）：首建时按 JWT 主体盖章，桌面/无令牌为 None
        self.owner_sub: str | None = None
        self.owner_org: str = ""

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

# SSE 响应头：/api/chat 与 /api/chat/review 都要用。放在模块级是因为
# 审阅/续跑曾经是 chat_submit 的局部变量，浏览器直连引擎时四条审阅动作全部 NameError 500。
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def _evict_finished_sessions() -> None:
    """会话表超上限时淘汰已结束的旧会话（磁盘检查点不受影响）。

    只淘汰 agent_task 已结束或已取消的会话：待审阅的计划会话仍需留在内存里，
    否则用户点「批准」时会退化成重新生成计划。
    """
    overflow = len(_TASK_SESSIONS) - MAX_IN_MEMORY_SESSIONS
    if overflow <= 0:
        return
    candidates = sorted(
        (
            (session.created_at, tid)
            for tid, session in _TASK_SESSIONS.items()
            if session.cancelled
            or (session.agent_task is not None and session.agent_task.done())
        ),
        key=lambda item: item[0],
    )
    for _created, tid in candidates[:overflow]:
        # 会话出内存 = 它的常驻连接器也该收摊，否则子进程跟着会话数一起涨
        MCP_HUB.schedule_session_drop(tid)
        _TASK_SESSIONS.pop(tid, None)


def _get_or_create_session(task_id: str) -> TaskSession:
    session = _TASK_SESSIONS.get(task_id)
    if session is None:
        _evict_finished_sessions()
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
        "ownerSub": session.owner_sub or "",
        "ownerOrg": session.owner_org or "",
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


SERVICE_FORBIDDEN_RUNTIME_KEYS = ("mcp", "mcpServers")


def _runtime_guard_error(runtime_raw) -> str | None:
    """服务化模式禁止客户端下发 MCP 服务器配置。

    stdio MCP 会在引擎主机上按客户端给的命令启动子进程；JWT/静态令牌子账号
    若能任意下发，等于拿到宿主机执行面。桌面壳（本机令牌，principal=None）
    不受影响——那里的 MCP 配置本来就是用户本机自己的。
    """
    if current_principal() is None or not isinstance(runtime_raw, dict):
        return None
    for key in SERVICE_FORBIDDEN_RUNTIME_KEYS:
        if runtime_raw.get(key):
            return "服务化模式不允许客户端下发 MCP 服务器配置（会启动服务器进程）"
    return None


def _apply_resume_overrides(session: TaskSession, payload: dict) -> str | None:
    """Rust 在 resume 时重新下发模型配置与 runtime（磁盘里不存密钥）。
    浏览器直连没有这两项：模型配置已在 _restore_session_from_disk 里按 modelId 解析。
    返回错误文案表示该 runtime 不被当前调用主体接受。"""
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
        guard = _runtime_guard_error(runtime_raw)
        if guard:
            return guard
        session.runtime = runtime_raw
    if payload.get("expertId") is not None:
        session.expert_id = str(payload.get("expertId") or "").strip() or None
    return None


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

    # 服务化归属（D-02）：恢复后仍按原 owner 隔离，跨用户 resume 会被拒
    session.owner_sub = str(context.get("ownerSub") or "").strip() or None
    session.owner_org = str(context.get("ownerOrg") or "").strip()

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
# 桌面通道另有 Rust 侧 `model_usage` 表（按 usage 事件记账，见 bridge/agent.rs）；
# 本台账覆盖所有进入模型层的调用（浏览器直连、IM、CLI、定时任务、桌面通道），
# 并按「来源 × 用户 × 模型 × 日」追加落盘，引擎重启后重放恢复（D-43），
# 因此配额硬停与上报服务端都有连续依据，不再是一次性内存值。

# 当前请求的调用主体（JWT 模式为账号 uid；桌面/无令牌模式为 None）。
# 模型调用链较深，用 contextvar 传递，避免把 principal 穿过每一层函数。
_CURRENT_PRINCIPAL: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "tt_engine_principal", default=None
)

# 当前请求携带的账号 access token（JWT 模式才有）。网关模式换票用它，
# 只在本请求的上下文里存活；不写日志、不落盘、不进事件流。
_CURRENT_ACCESS_TOKEN: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tt_engine_access_token", default=""
)


def current_access_token() -> str:
    return _CURRENT_ACCESS_TOKEN.get()


def current_principal() -> dict | None:
    return _CURRENT_PRINCIPAL.get()


def _principal_is_admin(principal: dict | None) -> bool:
    return bool(principal) and principal.get("auth") == "jwt" and principal.get("role") == "admin"


def _stamp_or_check_session(session: "TaskSession") -> dict | None:
    """服务化按人隔离（D-02 组织数据隔离）：首建盖章，跨用户访问返回错误。

    - 桌面 / 无令牌模式（principal 为 None）：不隔离，直接放行。
    - 会话尚无 owner：按当前 JWT 主体盖章。
    - owner 与当前 sub 一致，或当前主体是管理员：放行。
    - 其余：返回 403 错误字典（调用者据此拒绝，不泄露他人会话与产物）。
    """
    principal = current_principal()
    if principal is None:
        return None
    sub = str(principal.get("sub") or "")
    if not session.owner_sub:
        session.owner_sub = sub
        session.owner_org = str(principal.get("org") or "")
        return None
    if session.owner_sub == sub or _principal_is_admin(principal):
        return None
    return {
        "status": 403,
        "code": "TASK_FORBIDDEN",
        "message": "该任务不属于当前用户（服务化按人隔离，管理员除外）",
    }


def _principal_can_access(principal: dict | None, owner_sub: str | None) -> bool:
    """判断当前主体能否访问归属为 owner_sub 的会话/检查点。

    桌面/无令牌放行；未归属（空 owner，如 IM/系统任务或隔离上线前的存量）放行；
    管理员放行；否则仅本人。
    """
    if principal is None:
        return True
    if not owner_sub:
        return True
    if _principal_is_admin(principal):
        return True
    return str(principal.get("sub") or "") == owner_sub


def _checkpoint_owner(data: dict) -> str:
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    return str(context.get("ownerSub") or "")


def _active_task_count(sub: str) -> int:
    """该用户名下仍在执行的任务数（用于并发配额硬停）"""
    if not sub:
        return 0
    count = 0
    for session in list(_TASK_SESSIONS.values()):
        if session.owner_sub != sub:
            continue
        task = session.agent_task
        if task is not None and not task.done():
            count += 1
    return count


def _quota_error(principal: dict | None) -> dict | None:
    """JWT 主体的配额硬停（D-02）：monthlyTokens / concurrentTasks 超限即拒绝且不执行。

    桌面 / 无令牌 / 静态令牌（没有 JWT 配额声明）不做硬停，返回 None。
    值为 0 或缺失表示不限制。
    """
    if not principal or principal.get("auth") != "jwt":
        return None
    quota = principal.get("quota")
    if not isinstance(quota, dict):
        return None
    sub = str(principal.get("sub") or "")
    monthly = quota.get("monthlyTokens")
    if isinstance(monthly, int) and monthly > 0:
        used = USAGE_LEDGER.tokens_used(sub, "monthly")
        if used >= monthly:
            return {
                "status": 429,
                "code": "QUOTA_TOKENS_EXCEEDED",
                "message": f"本月 token 配额已用尽（{used}/{monthly}），任务未执行",
            }
    concurrent = quota.get("concurrentTasks")
    if isinstance(concurrent, int) and concurrent > 0:
        active = _active_task_count(sub)
        if active >= concurrent:
            return {
                "status": 429,
                "code": "QUOTA_CONCURRENCY_EXCEEDED",
                "message": f"并发任务已达上限（{active}/{concurrent}），请等待其他任务完成后重试",
            }
    return None


def _utc_day(ms: int | None = None) -> str:
    """UTC 日期 yyyy-mm-dd（与 Rust 台账同口径：跨日按 UTC 分桶）"""
    ts = time.time() if ms is None else ms / 1000
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _utc_month(ms: int | None = None) -> str:
    return _utc_day(ms)[:7]


class UsageLedger:
    """模型用量台账：按「来源 × 用户 × 模型 × 日」累计并落盘

    延迟取每次真实模型调用的端到端耗时（流式为整段生成耗时）；成本按调用方
    配置的每 1M tokens 单价换算成 micros。没有单价时只记调用次数，不按 0 元
    假装免费（`pricedCalls`/`unpricedCalls` 分开暴露）。

    落盘为追加式增量日志（`usage_store.UsageDeltaLog`）：启动后首次访问时重放，
    引擎重启用量不丢；增量行累计到阈值再合并成快照行，避免文件无界增长。写入失败
    只记状态、不阻断模型调用链，但一定通过 `/api/usage` 的 `persist` 字段披露。
    """

    def __init__(self, log: us.UsageDeltaLog | None = None) -> None:
        # key = (subject, model_name, day) → row
        self.rows: dict[tuple[str, str, str], dict] = {}
        self._log = log or us.UsageDeltaLog()
        self._loaded = False
        self._drops = 0

    # ── 落盘与重放 ────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        lines, drops, _error = self._log.read()
        self._drops = drops
        for line in lines:
            key = line.get("key") if isinstance(line.get("key"), dict) else {}
            subject = str(key.get("sub") or "").strip() or "local"
            name = str(key.get("model") or "").strip() or "unknown"
            day = str(key.get("day") or "").strip()
            if not day:
                self._drops += 1
                continue
            if line.get("kind") == us.KIND_MERGED:
                restored = _row_from_snapshot(subject, name, day, line.get("row"))
                if restored is None:
                    self._drops += 1
                    continue
                self.rows[(subject, name, day)] = restored
            else:
                self._apply_delta(subject, name, day, line.get("d") or {})

    def reset(self) -> None:
        """清空内存与加载标记（仅测试用：模拟进程重启，不删落盘文件）"""
        self.rows = {}
        self._loaded = False
        self._drops = 0

    def persist_status(self) -> dict:
        self._ensure_loaded()
        return self._log.status(loaded=self._loaded, rows=len(self.rows), drops=self._drops)

    def _snapshot_rows(self) -> list[tuple[dict, dict]]:
        return [
            ({"sub": subject, "model": name, "day": day}, row)
            for (subject, name, day), row in self.rows.items()
        ]

    def _apply_delta(self, subject: str, name: str, day: str, delta: dict) -> dict:
        """把一条增量并入内存态：落盘重放与实时记账共用同一段口径，避免两处漂移"""
        row = self.rows.get((subject, name, day))
        if row is None:
            row = {
                "sub": subject,
                "org": "",
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
                "byOrigin": {},
            }
            self.rows[(subject, name, day)] = row
        prompt = max(0, int(delta.get("pt") or 0))
        completion = max(0, int(delta.get("ct") or 0))
        calls = max(1, int(delta.get("calls") or 1))
        priced = bool(delta.get("priced"))
        origin = us.normalize_origin(delta.get("origin"))
        row["promptTokens"] += prompt
        row["completionTokens"] += completion
        row["calls"] += calls
        at_ms = int(delta.get("atMs") or 0)
        row["lastUsedAtMs"] = max(row["lastUsedAtMs"], at_ms)
        latency = delta.get("lat")
        if latency is not None:
            row["latencyMsTotal"] += max(0, int(latency))
            row["latencySamples"] += calls
        bucket = row["byOrigin"].setdefault(
            origin,
            {
                "promptTokens": 0,
                "completionTokens": 0,
                "calls": 0,
                "pricedCalls": 0,
                "unpricedCalls": 0,
                "latencyMsTotal": 0,
                "latencySamples": 0,
                "costs": {},
            },
        )
        bucket["promptTokens"] += prompt
        bucket["completionTokens"] += completion
        bucket["calls"] += calls
        if latency is not None:
            bucket["latencyMsTotal"] += max(0, int(latency))
            bucket["latencySamples"] += calls
        if priced:
            row["pricedCalls"] += calls
            bucket["pricedCalls"] += calls
            for code, micros in (delta.get("cost") or {}).items():
                key = (str(code).strip().upper() or "CNY")
                row["costs"][key] = row["costs"].get(key, 0) + max(0, int(micros))
                bucket["costs"][key] = bucket["costs"].get(key, 0) + max(0, int(micros))
        else:
            row["unpricedCalls"] += calls
            bucket["unpricedCalls"] += calls
        org = str(delta.get("org") or "").strip()
        if org and not row["org"]:
            row["org"] = org
        return row

    # ── 记账 ─────────────────────────────────────────────────

    def add(self, model_name: str, prompt_tokens: int, completion_tokens: int,
            latency_ms: int | None = None, cost_micros: int | None = None,
            currency: str | None = None, priced: bool = False,
            sub: str | None = None, org: str | None = None,
            origin: str | None = None) -> dict:
        name = (model_name or "").strip() or "unknown"
        subject = (sub or "").strip() or "local"
        day = _utc_day()
        sample_origin = us.normalize_origin(origin) if origin not in (None, "") else us.usage_origin()
        self._ensure_loaded()
        prompt = max(0, int(prompt_tokens))
        completion = max(0, int(completion_tokens))
        cost_code = (str(currency or "CNY").strip().upper() or "CNY")
        delta = {
            "org": (org or "").strip(),
            "pt": prompt,
            "ct": completion,
            "calls": 1,
            "lat": latency_ms,
            "origin": sample_origin,
            "priced": 1 if (priced and cost_micros is not None) else 0,
            "cost": (
                {cost_code: max(0, int(cost_micros))}
                if priced and cost_micros is not None
                else {}
            ),
            "atMs": int(time.time() * 1000),
        }
        row = self._apply_delta(subject, name, day, delta)
        self._log.append({"sub": subject, "model": name, "day": day}, delta)
        if self._log.should_compact():
            self._log.write_snapshot(self._snapshot_rows())
        return row

    # ── 读法 ─────────────────────────────────────────────────

    def aggregate(self, period: str, subject: str | None = None) -> list[dict]:
        """按周期聚合为「模型一行」（daily=今日 / monthly=本月 / total=全部）

        subject 为 None 时汇总全部用户；传入具体 uid 时只看该用户。
        """
        self._ensure_loaded()
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
                    "byOrigin": {},
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
            for origin, bucket in (row.get("byOrigin") or {}).items():
                target = cur["byOrigin"].setdefault(
                    origin, {"promptTokens": 0, "completionTokens": 0, "calls": 0}
                )
                target["promptTokens"] += int(bucket.get("promptTokens") or 0)
                target["completionTokens"] += int(bucket.get("completionTokens") or 0)
                target["calls"] += int(bucket.get("calls") or 0)
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

    def daily_rows(self, period: str = "total", subject: str | None = None) -> list[dict]:
        """按「用户 × 模型 × 日 × 来源」导出明细：合并视图与上报服务端的唯一口径。

        为什么按来源拆开：桌面端 `model_usage` 与引擎台账会记到同一批调用，合并时必须
        只排除明确标记为桌面的那部分，其余（IM / CLI / 定时 / 浏览器直连）桌面端根本没有，
        必须原样补入，否则用量会在合并视图里静默消失。
        """
        self._ensure_loaded()
        match = self._match(period)
        out: list[dict] = []
        for (row_subject, name, day), row in self.rows.items():
            if subject is not None and row_subject != subject:
                continue
            if not match(day):
                continue
            for origin, bucket in sorted((row.get("byOrigin") or {}).items()):
                prompt = int(bucket.get("promptTokens") or 0)
                completion = int(bucket.get("completionTokens") or 0)
                samples = int(bucket.get("latencySamples") or 0)
                latency_total = int(bucket.get("latencyMsTotal") or 0)
                bucket_costs = bucket.get("costs") or {}
                out.append({
                    "sub": row_subject,
                    "org": row.get("org") or "",
                    "modelName": name,
                    "day": day,
                    "origin": origin,
                    "promptTokens": prompt,
                    "completionTokens": completion,
                    "tokensUsed": prompt + completion,
                    "calls": int(bucket.get("calls") or 0),
                    "pricedCalls": int(bucket.get("pricedCalls") or 0),
                    "unpricedCalls": int(bucket.get("unpricedCalls") or 0),
                    # 延迟与成本按来源一起导出：合并视图里引擎补入的那部分
                    # 若不带这两项，服务端算出的总口径会比客户端少一截
                    "latencyMsTotal": latency_total,
                    "latencySamples": samples,
                    "costs": [
                        {"currency": code, "micros": int(bucket_costs[code])}
                        for code in sorted(bucket_costs)
                    ],
                    "lastUsedAtMs": row.get("lastUsedAtMs") or 0,
                })
        out.sort(key=lambda r: (r["day"], r["sub"], r["modelName"], r["origin"]))
        return out

    def tokens_used(self, sub: str, period: str = "monthly") -> int:
        """指定用户在该周期内累计消耗的 token（prompt + completion），用于配额硬停。"""
        if not sub:
            return 0
        self._ensure_loaded()
        match = self._match(period)
        total = 0
        for (row_subject, _name, day), row in self.rows.items():
            if row_subject != sub or not match(day):
                continue
            total += row["promptTokens"] + row["completionTokens"]
        return total

    def days(self, period: str, subject: str | None = None) -> list[dict]:
        """按 UTC 日分桶的趋势点（升序），口径与 aggregate 同一周期过滤"""
        self._ensure_loaded()
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
        self._ensure_loaded()
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
            "avgLatencyMs": weighted // samples if samples else None,
            "latencySamples": samples,
            "costs": [{"currency": code, "micros": costs[code]} for code in sorted(costs)],
            "pricedCalls": sum(int(r.get("pricedCalls") or 0) for r in rows),
            "unpricedCalls": sum(int(r.get("unpricedCalls") or 0) for r in rows),
        }


def _finalize_usage_row(row: dict) -> None:
    """把内部累计字段收口为对外字段：平均延迟 + 按币种成本清单 + 按来源明细"""
    samples = int(row.pop("latencySamples", 0) or 0)
    total = int(row.pop("latencyMsTotal", 0) or 0)
    # 平均延迟一律向下取整：与 Rust 的 i64 整除、前端的 Math.floor 同口径，
    # 否则同一份台账在三处能差出 1ms，合并视图就对不上账
    row["avgLatencyMs"] = total // samples if samples else None
    row["latencySamples"] = samples
    costs = row.pop("costs", {}) or {}
    row["costs"] = [
        {"currency": code, "micros": int(costs[code])} for code in sorted(costs)
    ]
    by_origin = row.pop("byOrigin", {}) or {}
    row["origins"] = sorted(
        (
            {
                "origin": origin,
                "promptTokens": int(bucket.get("promptTokens") or 0),
                "completionTokens": int(bucket.get("completionTokens") or 0),
                "tokensUsed": int(bucket.get("promptTokens") or 0) + int(bucket.get("completionTokens") or 0),
                "calls": int(bucket.get("calls") or 0),
            }
            for origin, bucket in by_origin.items()
        ),
        key=lambda item: item["tokensUsed"],
        reverse=True,
    )


def _row_from_snapshot(subject: str, name: str, day: str, raw: object) -> dict | None:
    """快照行 → 内存行：逐字段取值并限非负；坏行丢弃而不是让台账读到脏数据"""
    if not isinstance(raw, dict):
        return None
    costs_src = raw.get("costs") or {}
    origins_src = raw.get("byOrigin") or {}
    if not isinstance(costs_src, dict) or not isinstance(origins_src, dict):
        return None
    try:
        costs = {
            (str(code).strip().upper() or "CNY"): max(0, int(micros))
            for code, micros in costs_src.items()
        }
        by_origin: dict[str, dict] = {}
        for origin, bucket in origins_src.items():
            if not isinstance(bucket, dict):
                continue
            bucket_costs = bucket.get("costs") or {}
            if not isinstance(bucket_costs, dict):
                return None
            by_origin[us.normalize_origin(origin)] = {
                "promptTokens": max(0, int(bucket.get("promptTokens") or 0)),
                "completionTokens": max(0, int(bucket.get("completionTokens") or 0)),
                "calls": max(0, int(bucket.get("calls") or 0)),
                "pricedCalls": max(0, int(bucket.get("pricedCalls") or 0)),
                "unpricedCalls": max(0, int(bucket.get("unpricedCalls") or 0)),
                "latencyMsTotal": max(0, int(bucket.get("latencyMsTotal") or 0)),
                "latencySamples": max(0, int(bucket.get("latencySamples") or 0)),
                "costs": {
                    (str(code).strip().upper() or "CNY"): max(0, int(micros))
                    for code, micros in bucket_costs.items()
                },
            }
        return {
            "sub": subject,
            "org": str(raw.get("org") or ""),
            "modelName": name,
            "day": day,
            "promptTokens": max(0, int(raw.get("promptTokens") or 0)),
            "completionTokens": max(0, int(raw.get("completionTokens") or 0)),
            "calls": max(0, int(raw.get("calls") or 0)),
            "lastUsedAtMs": max(0, int(raw.get("lastUsedAtMs") or 0)),
            "latencyMsTotal": max(0, int(raw.get("latencyMsTotal") or 0)),
            "latencySamples": max(0, int(raw.get("latencySamples") or 0)),
            "costs": costs,
            "pricedCalls": max(0, int(raw.get("pricedCalls") or 0)),
            "unpricedCalls": max(0, int(raw.get("unpricedCalls") or 0)),
            "byOrigin": by_origin,
        }
    except (TypeError, ValueError):
        return None


USAGE_LEDGER = UsageLedger()


def _stream_has_content(chunk) -> bool:
    """这条 chunk 是否真的吐出了正文或工具调用。

    「首包」指的是第一个有内容的增量：只有 role、只有空 blocks、或只带 usage 的
    收尾包都不算，否则会把排队时间读成 0ms。与 Rust harness 的同名口径一致，
    两侧的首包数字才可比。ChatResponse 是 DictMixin(dict) 子类，按 dict 取值。"""
    content = chunk.get("content") if isinstance(chunk, dict) else getattr(chunk, "content", None)
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                return True  # 非字典块（未知形态）宁可算作有内容，不低估首包
            kind = block.get("type")
            if kind == "text" and str(block.get("text") or "").strip():
                return True
            if kind == "tool_use" and (block.get("name") or block.get("input")):
                return True
        return False
    return bool(content)


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


def _metadata_of(resp) -> dict | None:
    """从 ChatResponse 取结构化输出（缺失/空返回 None）。

    AgentScope 的上下文压缩靠这条通道拿摘要：模型没按 `structured_model` 回内容时，
    内核只打一条 WARNING 就继续跑，压缩其实整轮没生效。包装层把它记下来，
    界面才有机会如实说明「压缩未生效」而不是假装压过了。"""
    meta = resp.get("metadata") if isinstance(resp, dict) else getattr(resp, "metadata", None)
    if isinstance(meta, dict) and meta:
        return meta
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
                 currency: str | None = None, priced: bool = False,
                 first_token_ms: int | None = None) -> None:
        captured.append({
            "promptTokens": prompt,
            "completionTokens": completion,
            "model": model_name,
            "latencyMs": latency_ms,
            "firstTokenMs": first_token_ms,
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


# 取消成因的白名单：引擎只认这两个码，其余（含空）一律按用户手动取消记账。
# 码表与 src/shared/error-messages.ts 同源，界面按码查中文文案，不在引擎里拼句子。
CANCEL_REASONS = ("USER_CANCELLED", "BUDGET_TASK_EXCEEDED")

# agentscope 的中断路径：AgentBase.__call__ 捕获 CancelledError 后改调 handle_interrupt，
# 回一句英文兜底话并在 metadata 里带 _is_interrupted（见 _agent_base.py:458 与
# _react_agent.py:814）——所以被取消的任务在引擎这一层看到的是「正常返回」。
AGENT_INTERRUPTED_METADATA_KEY = "_is_interrupted"


def _agent_reported_interruption(result) -> bool:
    """模型侧自己承认「这轮被打断了」吗？用于补 session.cancelled 看不到的中断来源。"""
    metadata = getattr(result, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get(AGENT_INTERRUPTED_METADATA_KEY))


def _cancel_note(reason: str) -> str:
    """取消路径给检查点的中文原因：谁停的就说是谁，不把预算拦停写成用户取消。"""
    if reason == "BUDGET_TASK_EXCEEDED":
        return "单任务预算已用满，可从最近一次模型调用前的检查点继续"
    return "用户取消了任务，可从最近一次模型调用前的检查点继续"


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
    # D-03.5 合并口径：写暂存开着时，那两件文本写入的批准由暂存面板承担（一次写入一次批准），
    # 这一层不再为同一次写入弹第二个框。判定与 build_agent 用同一个函数，不留第二套规则。
    tool_approval.staged_writes = wsr.staging_enabled(session.runtime)
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
                  currency: str | None = None, priced: bool = False,
                  first_token_ms: int | None = None) -> None:
        _safe_put(queue, "usage", {
            "usage": {
                "promptTokens": prompt_tokens,
                "completionTokens": completion_tokens,
                "model": model_name,
                "latencyMs": latency_ms,
                # 首包：台账不存这一列（库里没有对应字段），但用量事件里留着，
                # 长任务基准与「慢首包」诊断靠它把「排队」和「出字慢」分开看
                "firstTokenMs": first_token_ms,
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
    # D-08：把「这一轮的扩展声明了什么能力、现在能不能用」摊成一份读数，供
    # `/api/exec/extensions` 原样展示。清单只从 runtime 摊，不去盘上扫扩展包。
    exd.observe(session.runtime, task_id=task_id)
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
            "message": f"工具门禁没有装上（文件访问档位与工作区沙箱都靠它落地）：{guard_err}",
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
        mcp_clients: list = []   # 本轮自建、必须在同一任务里关闭的一次性连接
        mcp_held: list = []      # 本轮从枢纽借出的常驻连接（收尾只归还引用）
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
                hub=MCP_HUB, session_key=session.task_id,
            )
            mcp_clients = mcp_result["clients"]
            mcp_held = mcp_result.get("held") or []
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

            # D-14.5：中断被 agentscope 吞掉时这里拿到的是「正常返回」。被取消/被预算
            # 停下的任务不许继续走成功收尾（deliver「任务完成」、自动记忆、Stop 钩子、
            # 检查点标不可续），统一改道到下面的 cancelled 分支。
            if session.cancelled or _agent_reported_interruption(result):
                raise asyncio.CancelledError()

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
            # 桌面记忆库模式下引擎不碰 MEMORY.md：候选随 deliver 回给 Rust 落库。
            memory_candidates: list[dict] = []
            if rc.desktop_memory_mode(session.runtime):
                memory_candidates, memory_note = _auto_memory_candidates(
                    session.runtime, goal, final_text, artifacts,
                    privacy=session.privacy_mode,
                )
                # 可选模型抽取：失败只降级为规则结果，不阻断交付
                if _model_memory_extract_enabled(session.runtime, session.privacy_mode):
                    model_facts, model_note = await _model_fact_candidates(
                        goal, final_text,
                        model_binding=session.model_binding,
                        model_config=session.model_config,
                        pii_mode=session.pii_mode,
                        on_usage=_on_usage,
                    )
                    merged, added, secret_skipped = _merge_memory_candidates(
                        memory_candidates, model_facts
                    )
                    memory_candidates = merged
                    if added:
                        model_note = f"模型抽取补充 {added} 条候选"
                    elif secret_skipped:
                        model_note = (
                            f"{secret_skipped} 条模型候选疑似包含凭据，已跳过"
                        )
                    memory_note = "；".join(
                        part for part in (memory_note, model_note) if part
                    )
            else:
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
            deliver_event = {
                "summary": "任务完成",
                "artifacts": artifacts,
                "final_text": final_text,
            }
            if memory_candidates:
                # 复数键：一条摘要 + 若干规则抽取的事实，由 Rust 逐条去重/判冲突后落库
                deliver_event["memory_candidates"] = memory_candidates
            _safe_put(queue, "deliver", deliver_event)
            # Stop：脚本可以 exit 2 表达「别停，继续」；能否续跑由下面的说明如实交代
            await hook_runner.fire("Stop", payload={"reason": "completed"})
        except asyncio.CancelledError:
            end_reason = "cancelled"
            # 成因由取消请求带进来（Rust 的预算拦停发 BUDGET_TASK_EXCEEDED，界面上的
            # 「谁停的」跟着它走）；没带或不在码表里就按用户手动取消，不猜第三个码。
            reason_code = (
                session.cancel_reason
                if session.cancel_reason in CANCEL_REASONS
                else "USER_CANCELLED"
            )
            end_note = _cancel_note(reason_code)
            # 取消路径（POST /api/chat/cancel）：cancelled 状态事件后正常收尾
            _safe_put(queue, "status_change", {
                "taskId": task_id,
                "status": "cancelled",
                "statusReasonCode": reason_code,
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
            # 一次性连接随本轮退出（必须在这里，与 attach 同任务）；
            # 常驻连接只归还引用，进程留给同会话的下一轮（D-40 第 5 项）
            await mr.close_all(mcp_clients)
            await mr.release_hub(MCP_HUB, session.task_id, mcp_held)
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
            # D-28：收尾计一条 task_finished（放在 done 之后：事件流一个字节也不变）。
            # 隐私轮不记；没同意时 record() 自己回「未开启」，这里不额外判断档位。
            _telemetry_task_finished(session, end_reason, checkpoint)

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
1. 分析用户目标，必要时先列目录、查看相关文件再动手
2. 使用工具完成任务（列目录、读写文件、抽取文档正文等）
3. 涉及删除或覆盖文件前务必谨慎，优先保留原内容
4. 完成后用简洁中文总结做了什么、产出了哪些文件

约束:
- 文件操作遵循本轮的文件访问档位说明：档位允许的范围内可直接操作，被拒绝时如实说明原因与切换方式
- 不清楚目录里有什么时先调用 list_directory；Word/Excel/PowerPoint/PDF 用 read_document 抽取正文，文本文件用 view_text_file
- 读取有分页预算（正文默认 4000、上限 8000 字符，目录清单默认 20、上限 50 项）：返回值会写明还剩多少、带哪个 start/offset 接着读，需要更多就翻页，不要把整篇塞进上下文
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


def _gateway_of(model_config: dict | None) -> dict | None:
    """解析「网关模式」配置；存在但缺字段时直接报错，不静默退回直连。

    配置只含路由信息（baseUrl / controlPlaneUrl / dept），账号 access token
    由请求头或桌面壳在内存里注入，不写 models.json。
    """
    cfg = model_config if isinstance(model_config, dict) else {}
    raw = cfg.get("gateway")
    if not isinstance(raw, dict) or not raw:
        return None
    base = str(raw.get("baseUrl") or "").strip().rstrip("/")
    control = str(raw.get("controlPlaneUrl") or "").strip().rstrip("/")
    if not base or not control:
        raise RuntimeError(
            "网关模式配置不完整：需要 gateway.baseUrl 与 gateway.controlPlaneUrl"
        )
    return {
        "baseUrl": base,
        "controlPlaneUrl": control,
        "dept": str(raw.get("dept") or "").strip(),
        "accessToken": str(raw.get("accessToken") or "").strip(),
    }


def _model_kind(model_config: dict | None) -> str:
    """本轮主模型的接入方式：网关 / 自定义端点 / 环境 DashScope / 环境 OpenAI 兼容"""
    cfg = model_config if isinstance(model_config, dict) else {}
    if _gateway_of(model_config) is not None:
        return "gateway"
    if str(cfg.get("baseUrl") or "").strip():
        return "custom"
    if os.environ.get("TT_PROVIDER", "dashscope").lower() == "dashscope":
        return "dashscope"
    return "openai"


# 换票主体缓存：同一控制面 + 部门 + access token 复用同一个 provider。
# access token 只以摘要参与键名，缓存条目不会出现在日志或事件流里。
_GATEWAY_PROVIDERS: dict[tuple[str, str, str], gwt.GatewayTokenProvider] = {}
_GATEWAY_PROVIDERS_LOCK = threading.Lock()
_GATEWAY_PROVIDER_LIMIT = 32


def _gateway_provider(gateway: dict) -> gwt.GatewayTokenProvider:
    access_token = gateway.get("accessToken") or current_access_token()
    digest = hashlib.sha256(access_token.encode("utf-8")).hexdigest()[:16] if access_token else ""
    key = (gateway["controlPlaneUrl"], gateway.get("dept") or "", digest)
    with _GATEWAY_PROVIDERS_LOCK:
        provider = _GATEWAY_PROVIDERS.get(key)
        if provider is None:
            provider = gwt.GatewayTokenProvider(
                gateway["controlPlaneUrl"], access_token, gateway.get("dept") or None
            )
            if len(_GATEWAY_PROVIDERS) >= _GATEWAY_PROVIDER_LIMIT:
                _GATEWAY_PROVIDERS.pop(next(iter(_GATEWAY_PROVIDERS)))
            _GATEWAY_PROVIDERS[key] = provider
    return provider


def _reset_gateway_providers() -> None:
    with _GATEWAY_PROVIDERS_LOCK:
        _GATEWAY_PROVIDERS.clear()


def _build_chat_model(model_name: str, api_key, base_url: str,
                      kind: str, stream: bool):
    """按接入方式构造模型与匹配的 formatter（主模型与转移目标共用）"""
    from agentscope.model import DashScopeChatModel, OpenAIChatModel
    from agentscope.formatter import DashScopeChatFormatter, OpenAIChatFormatter

    if kind in ("custom", "gateway"):
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
    if kind == "gateway":
        # 网关模式的路由/降级由控制面策略决定，引擎不再自行换模型
        return []
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
    on_usage(prompt, completion, model_name, latency_ms, cost_micros, currency, priced,
             first_token_ms=None)
    在每次真实模型调用后回调（首包 = 第一个有内容增量的耗时，非流式时等于整包）；
    on_route_switch({from, to, reason}) 在主模型失败、切到备用模型时回调；
    pii_mode/on_redaction 为出站脱敏档位与命中回调（包装在最外层，覆盖每次模型调用）"""
    cfg = model_config or {}
    kind = _model_kind(model_config)
    provider: gwt.GatewayTokenProvider | None = None

    model_name = _effective_model_name(model_override, model_config)
    if kind == "gateway":
        # 网关模式：base_url 指向网关，密钥是换票得到的短期令牌（每次请求现取）
        gateway = _gateway_of(model_config) or {}
        provider = _gateway_provider(gateway)
        base_url = gateway["baseUrl"]
        api_key = provider.get
    else:
        custom_base = str(cfg.get("baseUrl") or "").strip()
        custom_key = str(cfg.get("apiKey") or "").strip()
        env_key = os.environ.get("TT_API_KEY", "")
        api_key = custom_key or env_key

        if kind != "custom" and not api_key:
            # 引导去设置页配自定义模型（models.json 随请求 modelConfig 下发）；安装版没有
            # "项目根 .env" 这个概念，.env 只是开发/内网联调时的兜底通道。
            raise RuntimeError("未配置可用模型：请在「设置 → 模型与用量」中添加模型（或配置 TT_API_KEY 兜底）")

        base_url = custom_base if kind == "custom" else (
            os.environ.get("TT_BASE_URL", "") if kind == "openai" else ""
        )

    primary, formatter = _build_chat_model(model_name, api_key, base_url, kind, stream)
    if provider is not None:
        primary = GatewayAuthRetryModel(primary, provider)
    # 网关调用在台账里标 gateway：网关侧也会上报同一批用量，合并视图据此排除引擎这份
    usage_origin = us.ORIGIN_GATEWAY if kind == "gateway" else None
    candidates = [
        _with_usage_tracking(primary, on_usage, _prices_of(model_config), usage_origin)
    ]
    for item in _usable_fallbacks(model_config, kind):
        fb_name = str(item.get("modelName") or "").strip()
        fb_base = str(item.get("baseUrl") or "").strip() if kind == "custom" else base_url
        fb_key = str(item.get("apiKey") or "").strip() or (api_key if kind != "custom" else "")
        fb_model, _ = _build_chat_model(fb_name, fb_key, fb_base, kind, stream)
        candidates.append(
            _with_usage_tracking(fb_model, on_usage, _prices_of(item), usage_origin)
        )

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
    避免逐 chunk 重复累计；延迟按整段生成耗时计算，首包按第一个有内容的
    chunk 计算（与 Rust harness 同口径，长任务基准要拿两侧的首包对比）。
    """

    def __init__(self, inner, on_usage=None, prices: dict | None = None,
                 origin: str | None = None):
        self._inner = inner  # 必须先于 super().__init__：stream setter 需要它
        super().__init__(model_name=inner.model_name, stream=inner.stream)
        self._on_usage = on_usage
        self._prices = prices if isinstance(prices, dict) else None
        # 经网关的调用固定标 gateway：网关自己会上报同一批用量，合并视图靠这个标记去重
        self._origin = us.normalize_origin(origin) if origin else None
        # 结构化输出（上下文压缩摘要用的就是这条通道）这一轮到底有没有拿到结果：
        # None = 这轮没走结构化，True = 拿到了，False = 模型没按协议回，压缩会静默失败
        self.last_structured_ok: bool | None = None

    @property
    def stream(self) -> bool:
        return self._inner.stream

    @stream.setter
    def stream(self, value: bool) -> None:
        self._inner.stream = value

    def _record(self, usage, latency_ms: int, first_token_ms: int | None = None) -> None:
        if usage is None:
            return
        # 没测到首包就等于整包：非流式调用只有「一次拿全」，与 harness 的缺省口径一致
        first_ms = latency_ms if first_token_ms is None else first_token_ms
        prompt, completion = usage
        cost_micros, currency = cost_micros_of(self._prices, prompt, completion)
        priced = cost_micros is not None
        principal = current_principal() or {}
        sample_origin = self._origin or us.usage_origin()
        row = USAGE_LEDGER.add(
            self.model_name, prompt, completion,
            latency_ms=latency_ms, cost_micros=cost_micros,
            currency=currency, priced=priced,
            sub=principal.get("sub"), org=principal.get("org"),
            origin=sample_origin,
        )
        if self._on_usage is not None:
            try:
                # 第 8 个位置参数：首包耗时。台账里没有这一列（库里没有对应字段），
                # 但用量事件与基准要靠它把「排队」和「出字慢」分开看
                self._on_usage(
                    prompt, completion, self.model_name,
                    latency_ms, cost_micros, currency, priced, first_ms,
                )
            except Exception as exc:  # 回调失败不影响模型调用链
                print(f"[engine] usage 回调失败: {exc}", flush=True)
        cost_text = (
            f" cost={cost_micros / 1_000_000:.6f}{currency}" if priced else " cost=未配置单价"
        )
        print(
            f"[engine] usage model={self.model_name} +{prompt}/+{completion} "
            f"{latency_ms}ms 首包{first_ms}ms{cost_text} "
            f"origin={row['byOrigin'].get(sample_origin, {}).get('calls', 0)} "
            f"累计={row['promptTokens'] + row['completionTokens']} calls={row['calls']}",
            flush=True,
        )

    async def __call__(self, *args, **kwargs):
        started = time.monotonic()
        structured = kwargs.get("structured_model") is not None
        if structured:
            self.last_structured_ok = None
        res = await self._inner(*args, **kwargs)
        if inspect.isasyncgen(res):
            return self._wrap_stream(res, started, structured)
        self._record(_usage_of(res), int((time.monotonic() - started) * 1000))
        if structured:
            self.last_structured_ok = _metadata_of(res) is not None
        return res

    async def _wrap_stream(self, agen, started: float, structured: bool = False):
        last = None
        first_ms: int | None = None
        metadata_ok = False
        try:
            async for chunk in agen:
                if first_ms is None and _stream_has_content(chunk):
                    first_ms = int((time.monotonic() - started) * 1000)
                if structured and _metadata_of(chunk) is not None:
                    metadata_ok = True
                parsed = _usage_of(chunk)
                if parsed is not None:
                    last = parsed
                yield chunk
        finally:
            # D-14.5 的计量半边：流被中途取消时生成器在 yield 处就被关掉，原来写在循环
            # 之后的那行 _record 永远跑不到——**已经到达的 usage 就这么丢了**（少计一次，
            # 单任务预算因此最多被一次在途调用越过去）。收口放到 finally：消费完、被取消、
            # 还是中途抛异常都只落一次账；一个 usage 都没收到就什么都不写，不拿估算值填台账。
            self._record(last, int((time.monotonic() - started) * 1000), first_ms)
            if structured:
                # 生成器被消费完才定性：整条流里没有一个非空 metadata，就是没按协议回
                self.last_structured_ok = metadata_ok


def _usage_trackers(model, max_depth: int = 6) -> list:
    """从任意层数的模型包装里找出用量包装层。

    Agent 拿到的是最外层（脱敏 → 网关重试 → 路由 → 用量 → agentscope），
    而「结构化输出到底有没有拿到摘要」只记在 UsageTrackingModel 上，得逐层往里问。"""
    found: list = []
    seen: set = set()
    queue: list = [(model, 0)]
    while queue:
        node, depth = queue.pop()
        if node is None or id(node) in seen or depth > max_depth:
            continue
        seen.add(id(node))
        if isinstance(node, UsageTrackingModel):
            found.append(node)
            continue
        for value in list(getattr(node, "__dict__", {}).values()):
            if isinstance(value, (list, tuple)):
                queue.extend((item, depth + 1) for item in value)
            else:
                queue.append((value, depth + 1))
    return found


def _is_auth_failure(exc: BaseException) -> bool:
    """是否为网关返回的 401（令牌过期/被吊销），而不是策略拒绝或上游故障。"""
    status = getattr(exc, "status_code", None)
    if status == 401:
        return True
    if type(exc).__name__ == "AuthenticationError":
        return True
    return getattr(getattr(exc, "response", None), "status_code", None) == 401


class GatewayAuthRetryModel(ChatModelBase):
    """网关模式包装：401 时强制换票并重试一次。

    只在尚未吐出任何 chunk 时重试，避免把两段生成内容拼在一起；
    第二次仍失败则原样抛出，由上层按既有语义报错/转移。
    """

    def __init__(self, inner, provider: gwt.GatewayTokenProvider):
        self._inner = inner  # 必须先于 super().__init__：stream setter 需要它
        super().__init__(model_name=inner.model_name, stream=inner.stream)
        self._provider = provider

    @property
    def stream(self) -> bool:
        return self._inner.stream

    @stream.setter
    def stream(self, value: bool) -> None:
        self._inner.stream = value

    async def __call__(self, *args, **kwargs):
        try:
            res = await self._inner(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if not _is_auth_failure(exc):
                raise
            self._provider.invalidate()
            return await self._inner(*args, **kwargs)
        if inspect.isasyncgen(res):
            return self._retry_stream(res, args, kwargs)
        return res

    async def _retry_stream(self, agen, args, kwargs):
        emitted = False
        try:
            async for chunk in agen:
                emitted = True
                yield chunk
        except Exception as exc:  # noqa: BLE001
            if emitted or not _is_auth_failure(exc):
                raise
            self._provider.invalidate()
            retried = await self._inner(*args, **kwargs)
            if not inspect.isasyncgen(retried):
                return
            async for chunk in retried:
                yield chunk


def _with_usage_tracking(model, on_usage, prices: dict | None = None,
                         origin: str | None = None):
    """统一挂载用量采集（无 agentscope 基类可用时原样返回，保证不阻断）"""
    try:
        return UsageTrackingModel(model, on_usage, prices=prices, origin=origin)
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


# ── 办公产物（D-33）：真实 .xlsx / .docx 的上限、入参归一与包自检 ──────────────────
#
# 这两个工具的存在理由只有一条：用户说「给我一份 Excel / Word」时必须落到真件，
# 不能拿 Markdown 或 CSV 改名冒充。所以每次落盘收尾都过一次 OpenXML 包自检
# （Office 文档是 zip 包，文件头必须是 PK），不过就把半成品删掉、按失败返回。
# 上限不是为了省内存，是为了让「一次调用写完」这件事是真的：超出的部分一律截断并
# 在返回里逐条如实带话，模型不会以为第 2001 行也写进去了。
XLSX_MAX_SHEETS = 20
XLSX_MAX_ROWS = 2000
XLSX_MAX_COLS = 100
DOCX_MAX_BLOCKS = 600
DOCX_MAX_TABLE_ROWS = 200
DOCX_MAX_TABLE_COLS = 20
OFFICE_MAX_CELL_CHARS = 2000
OFFICE_MAX_SHEET_NAME_CHARS = 31  # Excel 的硬限制，超了要等保存时才炸，这里先夹住
OFFICE_LATIN_FONT = "Calibri"
OFFICE_CJK_FONT = "微软雅黑"
# 工作表名里 Excel 不让用的字符（竖线排最后，免得看代码时以为是个转义）
XLSX_NAME_BAD_CHARS = ':*?/\\[]'
# 单元格引用：A1 / $B$12，最多 3 个字母（更宽的表 openpyxl 自己也写不下）
_XLSX_CELL_REF_RE = re.compile(r"^([A-Z]{1,3})([1-9][0-9]{0,6})$")
# 引擎验过能写的函数；白名单外的公式照样落盘，但要在返回里说明没有求值器这回事
_XLSX_KNOWN_FUNCS = frozenset({
    "SUM", "AVERAGE", "COUNT", "COUNTA", "MAX", "MIN", "ROUND", "ABS", "IF",
    "IFERROR", "SUMIF", "COUNTIF", "CONCAT", "CONCATENATE", "TEXT",
})
_XLSX_FUNC_RE = re.compile(r"([A-Z][A-Z0-9_.]{0,20})\s*\(")
# 块类型别名：模型写 paragraph/para/text 是同一件事，这里只认一套内部名字
_DOCX_BLOCK_ALIASES = {
    "heading": "heading", "h1": "heading", "h2": "heading", "h3": "heading", "h4": "heading",
    "title": "heading",
    "paragraph": "paragraph", "para": "paragraph", "text": "paragraph", "body": "paragraph",
    "bullet": "bullet", "bullets": "bullet", "ul": "bullet", "list": "bullet",
    "list-item": "bullet", "listitem": "bullet",
    "numbered": "numbered", "numbered-list": "numbered", "ordered": "numbered",
    "ol": "numbered",
    "table": "table",
}


def _office_clip(value: object, limit: int = OFFICE_MAX_CELL_CHARS) -> str:
    """文本按上限截断（不截时原样返回）。"""
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit]


def _office_cell(value: object) -> tuple[object, bool, bool]:
    """单元格落盘值的归一：(值, 是否被截断, 是否被转成文本)。

    数字/布尔/空一律保持原类型——数字格式和公式都指着「这格是数字」这件事；
    dict/list 转成 JSON 文本并如实报一句，模型把表塞成嵌套结构时不该静默丢内容。
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value, False, False
    if isinstance(value, str):
        return _office_clip(value), len(value) > OFFICE_MAX_CELL_CHARS, False
    if isinstance(value, (dict, list)):
        raw = json.dumps(value, ensure_ascii=False, default=str)
        return _office_clip(raw), len(raw) > OFFICE_MAX_CELL_CHARS, True
    raw = str(value)
    return _office_clip(raw), len(raw) > OFFICE_MAX_CELL_CHARS, True


def _office_list(parsed: object, field: str) -> tuple[list, str]:
    """已解析的 JSON → 列表：(条目, 错误文案)。

    三种写法都收：数组本体、{"<field>": [...]}、单个对象（模型经常忘了外层方括号，
    把它当「就一条」处理比打回去重写更省事）。
    """
    if isinstance(parsed, list):
        return parsed, ""
    if isinstance(parsed, dict):
        inner = parsed.get(field)
        if isinstance(inner, list):
            return inner, ""
        values = list(parsed.values())
        if inner is None and len(values) == 1 and isinstance(values[0], list):
            return values[0], ""
        return [parsed], ""
    return [], (
        f"格式不对：收到的是 {type(parsed).__name__}，请给 JSON 数组，"
        f'或形如 {{"{field}": [...]}} 的对象'
    )


def _office_items(raw_json: object, field: str) -> tuple[list, str]:
    """工具入参（JSON 字符串）→ 列表：(条目, 错误文案)；解析失败时条目为空。"""
    text = raw_json if isinstance(raw_json, str) else json.dumps(raw_json, ensure_ascii=False)
    try:
        parsed = json.loads(text if text.strip() else "[]")
    except Exception as exc:  # noqa: BLE001 - 这句要给模型看懂，不能只回个异常类名
        return [], f"不是合法 JSON：{exc}"
    return _office_list(parsed, field)


def _office_rows(header_raw: object, rows_raw: object, max_rows: int, max_cols: int,
                 where: str) -> tuple[list, list, list]:
    """表头 + 数据行的归一：(表头, 行, 说明)。行列数与单元格长度都按上限夹。

    行允许三种写法：数组、对象（有表头按表头取值，没表头按键顺序）、标量（当成一个单元格）。
    说明按「同类问题汇总成一条」给，不逐行刷屏——2000 行的表里 2000 句「已截断」没人看得完，
    但「有多少处、首个出现在哪」是模型下一轮能用的信息。
    """
    notes: list[str] = []
    raw_header = (
        header_raw if isinstance(header_raw, list)
        else ([] if header_raw is None else [header_raw])
    )
    if len(raw_header) > max_cols:
        notes.append(f"{where}表头超过 {max_cols} 列，只取前 {max_cols} 列")
    header = [_office_clip(value) for value in raw_header[:max_cols]]
    raw_rows = (
        rows_raw if isinstance(rows_raw, list)
        else ([] if rows_raw is None else [rows_raw])
    )
    if len(raw_rows) > max_rows:
        notes.append(
            f"{where}数据超过 {max_rows} 行，只写前 {max_rows} 行"
            f"（后面 {len(raw_rows) - max_rows} 行没进文件）"
        )
    rows: list[list] = []
    dropped = dropped_at = 0
    wide = 0
    clipped_cells = 0
    converted_cells = 0
    dict_no_header = 0
    extra_keys: list[str] = []
    for offset, row in enumerate(raw_rows[:max_rows], 1):
        if isinstance(row, dict):
            if header:
                cells = [row.get(name) for name in header]
                for key in row:
                    label = str(key)
                    if label not in header and label not in extra_keys:
                        extra_keys.append(label)
            else:
                cells = list(row.values())
                dict_no_header += 1
        elif isinstance(row, list):
            cells = row
        elif row is None:
            dropped += 1
            dropped_at = dropped_at or offset
            continue
        else:
            cells = [row]
        if len(cells) > max_cols:
            wide += 1
            cells = cells[:max_cols]
        held = []
        for value in cells:
            kept, was_clipped, was_converted = _office_cell(value)
            held.append(kept)
            clipped_cells += 1 if was_clipped else 0
            converted_cells += 1 if was_converted else 0
        rows.append(held)
    if dropped:
        notes.append(f"{where}有 {dropped} 行是空值（首个在第 {dropped_at} 行），已跳过")
    if wide:
        notes.append(f"{where}有 {wide} 行的列数超过 {max_cols}，已逐行截断")
    if clipped_cells:
        notes.append(f"{where}有 {clipped_cells} 个单元格超过 {OFFICE_MAX_CELL_CHARS} 字，已截断")
    if converted_cells:
        notes.append(
            f"{where}有 {converted_cells} 个单元格是嵌套结构或非数字标量，已转成文本写入"
        )
    if dict_no_header:
        notes.append(
            f"{where}有 {dict_no_header} 行是对象但没给表头，按键顺序落成单元格（列名会丢）"
        )
    if extra_keys:
        notes.append(
            f"{where}对象行里有 {len(extra_keys)} 个键不在表头中，已忽略：" + "、".join(extra_keys[:8])
        )
    return header, rows, notes


def _xlsx_col_index(letters: object) -> int:
    """列字母 → 1 起始的列号（A=1、AB=28）；不是纯字母（≤3 个）时回 0。"""
    text = str(letters or "").strip().upper()
    if not text or len(text) > 3 or not text.isalpha():
        return 0
    index = 0
    for char in text:
        index = index * 26 + (ord(char) - 64)
    return index


def _xlsx_col_letter(index: int) -> str:
    """1 起始的列号 → 列字母（1→A、28→AB）；与 _xlsx_col_index 互逆。"""
    letters = ""
    number = int(index)
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _xlsx_cell_ref(ref: object) -> tuple[int, int] | None:
    """单元格引用 → (行, 列)（都从 1 起）；写不进合法位置时回 None，不猜。"""
    match = _XLSX_CELL_REF_RE.match(str(ref or "").strip().upper().replace("$", ""))
    if not match:
        return None
    return int(match.group(2)), _xlsx_col_index(match.group(1))


def _xlsx_unknown_funcs(formula: str) -> list[str]:
    """公式里出现的、引擎没验过的函数名（够不着求值，只能如实说明落点在 Excel 一侧）。"""
    found = []
    for match in _XLSX_FUNC_RE.finditer(str(formula or "").upper()):
        name = match.group(1)
        if name not in _XLSX_KNOWN_FUNCS and name not in found:
            found.append(name)
    return found


def _xlsx_sheet_name(value: object, index: int, taken: set) -> tuple[str, str]:
    """干净的工作表名 + 一句改名说明（没改名时说明为空串）。

    Excel 对表名有三条硬限制：不许带 : \\ / ? * [ ]、不许超 31 字、不许重名。
    一次在这里处理完，否则要等保存时才炸出一个用户看不出所以然的错误。
    """
    name = str(value if value is not None else "").strip()
    note = ""
    if not name:
        name = f"工作表{index}"
        note = f"第 {index} 张表没给 name，已用「{name}」"
    elif any(char in name for char in XLSX_NAME_BAD_CHARS):
        cleaned = name
        for char in XLSX_NAME_BAD_CHARS:
            cleaned = cleaned.replace(char, "-")
        note = f"表名「{name}」含 Excel 不允许的字符，已改成「{cleaned}」"
        name = cleaned
    if len(name) > OFFICE_MAX_SHEET_NAME_CHARS:
        cut = name[:OFFICE_MAX_SHEET_NAME_CHARS].rstrip()
        note = (note + "；" if note else "") + (
            f"表名超过 {OFFICE_MAX_SHEET_NAME_CHARS} 字，已截断为「{cut}」"
        )
        name = cut
    if name in taken:
        dup, seq = name, 2
        while name in taken:
            tail = f"_{seq}"
            name = dup[: OFFICE_MAX_SHEET_NAME_CHARS - len(tail)] + tail
            seq += 1
        note = (note + "；" if note else "") + f"表名「{dup}」重复，已改成「{name}」"
    taken.add(name)
    return name, note


def _xlsx_normalize(items: list) -> tuple[list, list]:
    """入参数组 → 可落盘的工作表清单：(sheets, notes)；notes 每条都是「实际写的和你给的不一样」。"""
    notes: list[str] = []
    kept = items[:XLSX_MAX_SHEETS]
    if len(items) > len(kept):
        notes.append(
            f"工作表超过上限 {XLSX_MAX_SHEETS} 张，已忽略后 {len(items) - len(kept)} 张"
        )
    taken: set[str] = set()
    sheets: list[dict] = []
    for index, item in enumerate(kept, 1):
        if isinstance(item, dict):
            spec = item
        elif isinstance(item, list):
            spec = {"rows": item}  # 直接甩一张二维数组也认：当成一张无名表
        else:
            notes.append(f"第 {index} 项既不是对象也不是二维数组，已跳过")
            continue
        name, rename_note = _xlsx_sheet_name(spec.get("name"), index, taken)
        if rename_note:
            notes.append(rename_note)
        where = f"工作表「{name}」："
        header, rows, row_notes = _office_rows(
            spec.get("header"), spec.get("rows"), XLSX_MAX_ROWS, XLSX_MAX_COLS, where
        )
        notes.extend(row_notes)
        if not header and not rows:
            notes.append(f"第 {index} 项（「{name}」）既没有表头也没有数据行，已跳过")
            continue
        numeric_cols = set()
        for row in rows:
            for position, value in enumerate(row, 1):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    numeric_cols.add(position)
        formats_raw = spec.get("formats")
        pairs = []
        if isinstance(formats_raw, dict):
            pairs = list(formats_raw.items())
        elif isinstance(formats_raw, list):
            pairs = [(_xlsx_col_letter(i), f) for i, f in enumerate(formats_raw, 1) if f]
        formats: list[tuple[int, str]] = []
        bad_format_keys: list[str] = []
        for key, fmt in pairs:
            text = _office_clip(fmt, 60).strip()
            if not text:
                continue
            label = str(key).strip()
            column = (header.index(label) + 1) if label in header else _xlsx_col_index(label)
            if not column or column > XLSX_MAX_COLS:
                bad_format_keys.append(label)
                continue
            formats.append((column, text))
            if column not in numeric_cols:
                notes.append(
                    f"{where}数字格式「{text}」落在第 {column} 列（{_xlsx_col_letter(column)}），"
                    "但那几格不是数字，格式不会有效果"
                )
        if bad_format_keys:
            notes.append(
                f"{where}有 {len(bad_format_keys)} 个数字格式的键既不是表头名也不是列字母，已忽略："
                + "、".join(bad_format_keys[:8])
            )
        formulas_raw = spec.get("formulas")
        entries = []
        if isinstance(formulas_raw, dict):
            entries = [(k, v) for k, v in formulas_raw.items()]
        elif isinstance(formulas_raw, list):
            for entry in formulas_raw:
                if isinstance(entry, dict):
                    ref = entry.get("cell") or entry.get("at") or entry.get("ref")
                    expr = entry.get("formula") or entry.get("value")
                    if ref is None or expr is None:
                        entries.append((None, None))
                    else:
                        entries.append((ref, expr))
                else:
                    entries.append((None, entry))
        formulas: list[tuple[int, int, str]] = []
        bad_refs: list[str] = []
        missing_eq: list[str] = []
        unknown: list[str] = []
        for ref, expr in entries:
            position = _xlsx_cell_ref(ref)
            text = str(expr or "").strip()
            if position is None:
                bad_refs.append(str(ref) if ref is not None else "（没给 cell）")
                continue
            if not text.startswith("="):
                missing_eq.append(f"{ref}←{text[:24]}")
                continue
            row_index, column = position
            if row_index > XLSX_MAX_ROWS or column > XLSX_MAX_COLS:
                bad_refs.append(f"{ref}（超出 {XLSX_MAX_ROWS} 行 / {XLSX_MAX_COLS} 列）")
                continue
            formulas.append((row_index, column, text))
            # 循环变量别叫 name：外层的工作表名就叫 name，这里一 Rebind 就会把表名
            # 换成最后一个未知函数名（「F」这张表会被存成「FOO」），是本刀自测抓出的真缺陷。
            for func_name in _xlsx_unknown_funcs(text):
                if func_name not in unknown:
                    unknown.append(func_name)
        if bad_refs:
            notes.append(
                f"{where}有 {len(bad_refs)} 处公式的单元格引用不合法，已忽略："
                + "、".join(bad_refs[:8])
            )
        if missing_eq:
            notes.append(
                f"{where}有 {len(missing_eq)} 处公式没以 = 开头，已忽略（首个 {missing_eq[0]}）"
            )
        if unknown:
            notes.append(
                f"{where}公式里出现了引擎没验过的函数 { '、'.join(unknown[:6]) } —— "
                "仍会原样写入，但能不能算开 Excel/WPS 才知道"
            )
        sheets.append({
            "name": name,
            "header": header,
            "rows": rows,
            "widths": _xlsx_widths(spec.get("widths"), where, notes),
            "formats": formats,
            "formulas": formulas,
        })
    return sheets, notes


def _xlsx_widths(raw: object, where: str, notes: list) -> list[tuple[int, float]]:
    """列宽入参归一：按列顺序给数字数组，或按列字母/表头名给对象。"""
    pairs = []
    if isinstance(raw, list):
        pairs = list(enumerate(raw, 1))
    elif isinstance(raw, dict):
        for key, value in raw.items():
            label = str(key).strip()
            column = _xlsx_col_index(label)
            if not column:
                notes.append(f"{where}列宽键「{label}」不是列字母，已忽略")
                continue
            pairs.append((column, value))
    out: list[tuple[int, float]] = []
    junk = 0
    for column, value in pairs:
        if value is None or not 1 <= int(column) <= XLSX_MAX_COLS:
            continue
        try:
            width = float(value)
        except (TypeError, ValueError):
            junk += 1
            continue
        # Excel 的列宽是「字符数」量纲，夹到 4–100 是为了既看得清又不至于单列撑爆屏幕
        out.append((int(column), max(4.0, min(width, 100.0))))
    if junk:
        notes.append(f"{where}有 {junk} 个列宽不是数字，已忽略（用默认宽度）")
    return out


def _docx_normalize(items: list) -> tuple[list, list]:
    """块数组 → 可落盘的文档块清单：(blocks, notes)；未知类型跳过并说明可用类型。"""
    notes: list[str] = []
    kept = items[:DOCX_MAX_BLOCKS]
    if len(items) > len(kept):
        notes.append(
            f"内容块超过上限 {DOCX_MAX_BLOCKS} 块，只写前 {DOCX_MAX_BLOCKS} 块"
            f"（后面 {len(items) - len(kept)} 块没进文件）"
        )
    blocks: list[dict] = []
    skipped: list[str] = []
    for index, item in enumerate(kept, 1):
        if isinstance(item, str):
            text = _office_clip(item).strip()
            if text:
                blocks.append({"kind": "paragraph", "text": text, "level": 1})
            continue
        if not isinstance(item, dict):
            skipped.append(f"第 {index} 项不是对象/字符串")
            continue
        kind_raw = str(item.get("type") or item.get("kind") or "").strip().lower().replace("_", "-")
        if not kind_raw:
            # 没写类型时按形状猜：有 rows/header 当表，有 level 当标题，其余按段落
            if isinstance(item.get("rows"), list) or isinstance(item.get("header"), list):
                kind_raw = "table"
            elif item.get("level") is not None:
                kind_raw = "heading"
            else:
                kind_raw = "paragraph"
        kind = _DOCX_BLOCK_ALIASES.get(kind_raw)
        if kind is None:
            skipped.append(f'第 {index} 块的类型「{kind_raw}」不认识')
            continue
        if kind == "table":
            header, rows, row_notes = _office_rows(
                item.get("header"), item.get("rows"),
                DOCX_MAX_TABLE_ROWS, DOCX_MAX_TABLE_COLS, f"第 {index} 块表格：",
            )
            notes.extend(row_notes)
            if not header and not rows:
                skipped.append(f"第 {index} 块表格是空的")
                continue
            blocks.append({"kind": "table", "header": header, "rows": rows})
            continue
        text = _office_clip(str(item.get("text") or item.get("content") or "")).strip()
        if not text:
            skipped.append(f"第 {index} 块（{kind}）没有正文")
            continue
        level = 1
        if kind == "heading":
            if kind_raw.startswith("h") and kind_raw[1:].isdigit():
                level = int(kind_raw[1])
            else:
                try:
                    level = int(item.get("level", 1))
                except (TypeError, ValueError):
                    level = 1
            level = max(1, min(4, level))
        blocks.append({"kind": kind, "text": text, "level": level})
    if skipped:
        notes.append(
            f"{len(skipped)} 块被跳过（首个：{skipped[0]}）；可用类型："
            "heading / paragraph / bullet / numbered / table"
        )
    return blocks, notes


def _docx_apply_fonts(document) -> str:
    """给默认样式与标题挂上「西文 + 中文」两套字形；成功回空串，失败回一句说明。

    只设 `font.name` 管的是西文那一份。中文走 w:eastAsia，缺了它 Word 会退回主题的东亚字体
    ——中文文档最常见的外观就是「我明明设了字体，中文还是宋体/方块」，所以两份都要落进同一个
    rFonts 节点。样式缺失或结构不认识时不硬撑：文档还是要能生成，但如实说明字体没改成。
    """
    from docx.oxml.ns import qn

    problems = []
    for style_name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3"):
        try:
            style = document.styles[style_name]
            style.font.name = OFFICE_LATIN_FONT
            rpr = style.element.get_or_add_rPr()
            rfonts = rpr.get_or_add_rFonts()
            rfonts.set(qn("w:eastAsia"), OFFICE_CJK_FONT)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{style_name}: {exc}")
    if len(problems) >= 5:
        return f"中文字体未能设置（{problems[0]}），中文将退回 Word 的默认东亚字体"
    if problems:
        return "部分样式的中文字体未设置成功（" + "；".join(problems) + "）"
    return ""


def _docx_add_table(document, block: dict, notes: list) -> None:
    """按块落一张 Word 表格：表头加粗，行数列数已在归一层夹过上限。"""
    header = block.get("header") or []
    rows = block.get("rows") or []
    widest = max((len(row) for row in rows), default=0)
    columns = len(header) or widest
    if columns <= 0:
        notes.append("有一张表格既没有表头也没有单元格，已跳过")
        return
    table = document.add_table(rows=(1 if header else 0) + len(rows), cols=columns)
    try:
        table.style = "Table Grid"  # 带边框；内置模板没有它时退成无边框，但要说出来
    except Exception:  # noqa: BLE001 - 样式缺失不该让整份文档失败
        notes.append("当前模板没有「Table Grid」样式，表格按无边框输出")
    offset = 0
    if header:
        for column, value in enumerate(header):
            run = table.cell(0, column).paragraphs[0].add_run(str(value))
            run.bold = True
        offset = 1
    for row_index, row in enumerate(rows):
        for column, value in enumerate(row[:columns]):
            text = "" if value is None else (value if isinstance(value, str) else str(value))
            # 数据格不需要加粗，直接交 .text：它会把格子换成唯一那一段，不留多余空行
            table.cell(offset + row_index, column).text = text


def _office_check_package(path: Path) -> str:
    """产物收尾自检：OpenXML 是 zip 包，文件头必须是 PK；不合格就回错误文案。

    不合格时先把这份半成品删掉——留着它，等于允许「扩展名对、内容不是 Office」
    这种最坑人的假件留在用户的工作目录里，而 D-33 要堵的正是它。
    """
    reason = ""
    head = b""
    try:
        head = path.read_bytes()[:2]
    except Exception as exc:  # noqa: BLE001
        reason = str(exc)
    if head == b"PK":
        return ""
    try:
        path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - 删不掉也要把「这不是合法文件」说清楚
        pass
    return (
        f"错误：{path.name} 不是合法的 Office 文件（文件头 {head!r}），已按失败处理并删除"
        + (f"；读取原因：{reason}" if reason else "")
    )


def build_toolkit(work_dir: Path, sandbox: "sr.SandboxPolicy | None" = None, slides: bool = False,
                  office: bool = False,
                  grants: "tg.ToolGrantPolicy | None" = None, emit=None, task_id: str = "",
                  pptx_templates: dict | None = None, extra: dict | None = None):
    """注册文件操作工具（自实现：路径限定在工作目录内，相对路径自动解析到工作目录）

    sandbox 存在时按工作区沙箱策略校验路径（黑名单优先；L0 拒绝写；L2 放开目录限制），
    并按对话输入框选的文件访问档位判定：每次询问/自动审批只在绑定目录内写盘，
    完全访问可越界但家目录与系统关键目录仍硬拒，删除文件只有完全访问允许。
    slides=True（幻灯片模式）时额外注册 generate_pptx：产出真实 .pptx，而不是文本改名。
    office=True（通用/写作模式）时额外注册 generate_xlsx / generate_docx：办公产物落到真件，
    不用 Markdown 或 CSV 改名冒充。这两件刻意不常驻注册——内置工具清单的契约要求本地 harness
    为常驻清单里每个工具都给出实现（tests/unit/extensions/builtin-tools-contract.test.ts），
    没有 Rust 实现之前把它们塞进常驻清单，等于界面上又多两条空头承诺。
    grants 是用户在工作区里的执行/联网显式授权：未授权或白名单为空时工具**不注册**。
    通用 shell（run_shell）需要命令执行与 shell 两个开关同时打开，属于高风险能力。
    extra 是 #164 车道的补充开关（knowledge / subagent 等装配现场对象）：缺省为空即一切不注册，
    所以直接调 build_toolkit 的自测拿到的仍然是常驻六件。
    extra["staging"] 给了 D-03.5 的写暂存策略且档位为 staged 时，`write_text_file` /
    `insert_text_file` **不写目标文件**：内容进引擎数据根的暂存区并算出 diff，
    等用户在界面批准后才由 `/api/staging/apply` 合并（暂存失败也不回退成直接写盘）。
    """
    from agentscope.tool import Toolkit, ToolResponse
    from agentscope.message import TextBlock

    base = work_dir.resolve()

    def _resolve(path: str) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = base / p
        return p.resolve()

    def _guard(p: Path, write: bool = False, delete: bool = False) -> str | None:
        """沙箱/目录越界/文件访问档位检查，返回错误文案或 None"""
        if sandbox is not None:
            denial = sandbox.file_denial(p, write=write, base=base, delete=delete)
            if denial is not None:
                # 档位拒绝与沙箱拒绝分开措辞：前者是对话框里自己选的约束，
                # 告诉用户去哪儿改；后者是工作区策略，改不了就得找设置页。
                head = (
                    "错误：文件访问权限拒绝"
                    if denial[1].startswith("file_permission")
                    else "错误：沙箱拒绝访问"
                )
                return f"{head} {p}：{denial[0]}"
            if sandbox.configured:
                # 策略已生效：边界由策略判定（L0/L1 限定工作目录与白名单，L2 刻意放开）
                return None
            if (write or delete) and sandbox.file_permission == "full_access":
                # 没配工作区沙箱时的写/删越界放行权只属于「完全访问」档位：
                # file_denial 已经验过硬拒清单，这里不能再拿旧的工作目录兜底二次拦，
                # 否则「完全访问」在默认形态下等于没放开（读取仍按原边界走兜底）。
                return None
        # 未配置策略时保留原有工作目录兜底
        try:
            p.relative_to(base)
        except ValueError:
            return f"错误：路径超出工作目录范围: {p}"
        return None

    def view_text_file(
        file_path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        units: int | None = None,
        start: int = 0,
    ):
        """查看工作目录内的文本文件内容（正文按分页预算返回，超量如实带话）。

        Args:
            file_path (`str`):
                文件路径（相对路径会解析到工作目录内）。
            start_line (`int`, optional):
                起始行号（从 1 开始），缺省为文件开头。
            end_line (`int`, optional):
                结束行号（含），缺省为文件末尾。
            units (`int`, optional):
                这一页最多返回多少个单元（单元=字符，不是 token），默认 4000、上限 8000；
                超出上限会被夹到上限并写明夹到了多少，不默默改你的意图。
            start (`int`, optional):
                从选定范围的第几个单元开始读（字符偏移），缺省 0。截断提示里给的
                start= 可以照抄回来接着读，需要更多请翻页，不要把整篇塞进上下文。
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
        # 行轴（start_line/end_line）先选范围，字符轴（start/units）再按预算取页：
        # 两个轴各管一件事，混在一起就没法表达「第 200 行往后、这一页 4000 字」。
        page = twa.budgeted_read("\n".join(lines[s:e]), units, start, window=_window_reading())
        text = page["text"]
        note = str(page["note"] or "").strip()
        if note:
            text = note + "\n" + text
        return ToolResponse(content=[TextBlock(type="text", text=text)])

    history_sink = (extra or {}).get("artifact_history") is not None
    staging_policy = (extra or {}).get("staging")
    # D-48.8「按剩余窗口取页」：三个读取件都从这里现取本轮读数（对象是先建后填的）。
    # 没给对象（自测直接调 build_toolkit、或其它装配点）就回空 → 走固定预算，与改造前逐字相同。
    read_window = (extra or {}).get("read_window")

    def _window_reading() -> dict:
        """当前这一轮的窗口读数；读不到就回空字典，页面里也不出现关于窗口的话。"""
        if read_window is None:
            return {}
        try:
            return read_window.reading() or {}
        except Exception:  # noqa: BLE001  读数坏了不拦读取：宁可给固定预算那一页，也不让工具报错
            return {}

    def _stage_instead_of_write(p: Path, new_text: str, tool: str):
        """写暂存开启时：内容收进暂存区并返回给模型的回执；没开启返回 None（调用方照旧写盘）。

        暂存失败**不回退成直接写盘**——用户选这一档要的就是「没批准就不落盘」，
        静默降级等于把这一档变成装饰，也等于把「审批」变成事后通知。
        """
        if staging_policy is None or not getattr(staging_policy, "staged", False):
            return None
        try:
            r = wsr.stage(new_text, target=p, task_id=task_id, tool=tool, work_dir=base)
        except Exception as exc:  # noqa: BLE001  暂存模块自己出问题也不能写用户的文件
            return ToolResponse(content=[TextBlock(
                type="text",
                text=f"错误：写暂存不可用，内容**未写入** {p}：{type(exc).__name__}: {_one_line(str(exc), 160)}",
            )])
        if not r.get("ok"):
            return ToolResponse(content=[TextBlock(
                type="text",
                text=f"错误：{r.get('reason') or '暂存失败'}；内容**未写入** {p}",
            )])
        return ToolResponse(content=[TextBlock(type="text", text=str(r.get("note") or ""))])

    def _capture_before_write(p: Path, tool: str) -> str:
        """D-26：覆盖前把盘上那一份留成一版（W2 分片 138.md §5-c 请 W4 接的那根线）。

        留的是**即将被盖掉的旧内容**，不是新写入的内容，所以版本 0001 等于第一次覆盖前的盘上那份。
        三条口径：① 开关没点亮（`build_toolkit` 被自测直接调用、或装配现场没给句柄）时**什么都不做**
        并返回空串——回执与改造前逐字相同；② 留档成功与否**都不改变写入结果**，写盘是用户要的事，
        版本历史只是附加保险；③ 但留档的每一分代价与每一次没留住都要进回执，不许悄悄少一版
        （超限、读失败、首次创建、去重四类在 `record_current` 里各有一句中文原因，原样转达）。
        """
        if not history_sink:
            return ""
        try:
            rec = ahr.record_current(p, task_id=task_id, tool=tool)
        except Exception as exc:  # noqa: BLE001 （留档任何意外都不许把用户的写入带崩）
            return f"（覆盖前留档失败，这一次没有可回滚的旧版本：{exc}）"
        note = str(rec.get("note") or "").strip()
        return f"（{note}）" if note else ""

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
        staged = _stage_instead_of_write(p, content, "write_text_file")
        if staged is not None:
            # 暂存档：目标文件一个字节都没动，父目录也不提前建——
            # 「即将覆盖」和「即将新建」的区分属于用户批准时看的那份 diff
            return staged
        p.parent.mkdir(parents=True, exist_ok=True)
        history_note = _capture_before_write(p, "write_text_file")
        p.write_text(content, encoding="utf-8")
        return ToolResponse(content=[TextBlock(type="text", text=f"已成功写入文件: {p}{history_note}")])

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
        merged = "\n".join(lines)
        # 暂存档收的是**整份合并后的内容**，不是片段——审批者看到的 diff 必须等于批准后会落盘的那份
        staged = _stage_instead_of_write(p, merged, "insert_text_file")
        if staged is not None:
            return staged
        history_note = _capture_before_write(p, "insert_text_file")
        p.write_text(merged, encoding="utf-8")
        return ToolResponse(content=[TextBlock(type="text", text=f"已成功更新文件: {p}{history_note}")])

    def _to_trash(p: Path, reason: str) -> tuple[str, str]:
        """删除前把原件复制进引擎回收目录；返回 (备份路径, 失败原因)

        复制失败就绝不删除：这一档放开的是「能删」，不是「删了找不回来」。
        """
        try:
            import data_paths  # noqa: PLC0415

            trash_dir = data_paths.file_trash_dir()
            trash_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return "", f"回收目录不可用：{exc}"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = trash_dir / f"{stamp}_{p.name}"
        seq = 1
        while target.exists():
            target = trash_dir / f"{stamp}_{seq}_{p.name}"
            seq += 1
        try:
            shutil.copy2(p, target)
        except Exception as exc:  # noqa: BLE001
            return "", f"原件未能复制到回收目录：{exc}"
        try:
            meta = target.with_suffix(target.suffix + ".from.json")
            meta.write_text(
                json.dumps(
                    {
                        "origin": str(p),
                        "reason": sr._brief(reason) if reason else "",
                        "deletedAt": stamp,
                        "taskId": task_id,
                        "workDir": str(base),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001  说明文件写失败不影响原件已备份
            pass
        return str(target), ""

    def delete_text_file(file_path: str, reason: str = ""):
        """删除一个文件（仅「完全访问」档位可用；删除前会先复制到引擎回收目录）。

        Args:
            file_path (`str`):
                要删除的文件路径（相对路径会解析到工作目录内）。只允许单个文件，
                目录请逐个文件处理——批量删除目录属于不可逆操作，不提供给模型。
            reason (`str`, optional):
                删除原因，会随原路径一起记进回收目录的说明文件，便于事后找回。
        """
        p = _resolve(file_path)
        if p.is_dir():
            return ToolResponse(content=[TextBlock(
                type="text", text=f"错误：只能删除单个文件，不支持目录：{p}"
            )])
        err = _guard(p, write=True, delete=True)
        if err:
            return ToolResponse(content=[TextBlock(type="text", text=err)])
        if not p.exists():
            return ToolResponse(content=[TextBlock(type="text", text=f"错误：文件不存在: {file_path}")])
        backup, trash_error = _to_trash(p, reason)
        if trash_error:
            return ToolResponse(content=[TextBlock(
                type="text", text=f"错误：未删除（{trash_error}）。文件仍在: {p}"
            )])
        try:
            p.unlink()
        except Exception as exc:  # noqa: BLE001
            return ToolResponse(content=[TextBlock(
                type="text",
                text=f"错误：删除失败（{exc}）。原件仍在: {p}；回收副本: {backup}",
            )])
        return ToolResponse(content=[TextBlock(
            type="text", text=f"已删除文件: {p}\n回收副本（如需找回从这里复制回去）: {backup}"
        )])

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
        try:
            from pptx import Presentation
        except ImportError:
            return ToolResponse(content=[TextBlock(
                type="text", text="错误：未安装 python-pptx，无法生成 .pptx（请安装 python-pptx 后重试）"
            )])

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

    def generate_xlsx(file_path: str, sheets_json: str):
        """生成真实的 Excel 工作簿（.xlsx）：多工作表、表头加粗冻结、列宽、数字格式与公式。

        Args:
            file_path (`str`):
                输出路径，必须以 .xlsx 结尾（相对路径会解析到工作目录内）。
            sheets_json (`str`):
                工作表数组的 JSON 字符串，每项形如 {"name": "销售", "header": ["月份", "金额"],
                "rows": [["1月", 1200], {"月份": "2月", "金额": 1500}], "widths": [12, 14],
                "formats": {"金额": "#,##0.00"}, "formulas": {"B4": "=SUM(B2:B3)"}}。
                rows 三种写法都认：数组、对象（按 header 取值）、标量（当成一个单元格）；
                金额/数量这类要参与计算的格子请给裸数字，别写成带千分位的字符串。
                formats 的键可以是表头名或列字母，widths 按列顺序给数字。
                上限：20 张表、单表 2000 行、100 列、单元格 2000 字，超出会截断并在返回里逐条说明。
                公式只负责写入，数值由 Excel/WPS 打开时计算——引擎这一侧没有求值器。
        """
        p = _resolve(file_path)
        if p.suffix.lower() != ".xlsx":
            return ToolResponse(content=[TextBlock(
                type="text", text=f"错误：工作簿路径必须以 .xlsx 结尾（收到 {p.name}）"
            )])
        err = _guard(p, write=True)
        if err:
            return ToolResponse(content=[TextBlock(type="text", text=err)])
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Font
        except ImportError:
            return ToolResponse(content=[TextBlock(
                type="text", text="错误：未安装 openpyxl，无法生成 .xlsx（请安装 openpyxl 后重试）"
            )])
        items, bad = _office_items(sheets_json, "sheets")
        if bad:
            return ToolResponse(content=[TextBlock(type="text", text=f"错误：sheets_json {bad}")])
        sheets, notes = _xlsx_normalize(items)
        if not sheets:
            return ToolResponse(content=[TextBlock(
                type="text",
                text="错误：sheets_json 里没有可用的工作表（每张表至少要给 header 或 rows）"
                + ("\n说明：" + "；".join(notes) if notes else ""),
            )])
        total_rows = 0
        total_formulas = 0
        try:
            wb = Workbook()
            wb.remove(wb.active)  # 默认那张 Sheet 不用：逐张按归一后的表名建，名字才说了算
            for sheet in sheets:
                ws = wb.create_sheet(title=sheet["name"])
                header = sheet["header"]
                if header:
                    for column, value in enumerate(header, 1):
                        cell = ws.cell(row=1, column=column, value=value)
                        cell.font = Font(bold=True)
                        cell.alignment = Alignment(horizontal="left")
                    # 表头钉住：往下翻数据时第一行不跟着滚走，长表才有得看
                    ws.freeze_panes = "A2"
                for column, width in sheet["widths"]:
                    ws.column_dimensions[_xlsx_col_letter(column)].width = width
                first_data = 2 if header else 1
                # 行号按**本表**偏移算：total_rows 是跨表累计的，拿它当行号会让第二张表
                # 从第 N+2 行开始写，第一张表的行数全变成空洞。
                for offset, row in enumerate(sheet["rows"]):
                    for column, value in enumerate(row, start=1):
                        ws.cell(row=first_data + offset, column=column, value=value)
                total_rows += len(sheet["rows"])
                for column, fmt in sheet["formats"]:
                    for row_index in range(first_data, first_data + len(sheet["rows"])):
                        ws.cell(row=row_index, column=column).number_format = fmt
                for row_index, column, formula in sheet["formulas"]:
                    ws.cell(row=row_index, column=column, value=formula)
                    total_formulas += 1
            p.parent.mkdir(parents=True, exist_ok=True)
            wb.save(str(p))
        except Exception as exc:  # noqa: BLE001 - 生成失败要说清是哪一步
            return ToolResponse(content=[TextBlock(type="text", text=f"错误：工作簿生成失败：{exc}")])
        broken = _office_check_package(p)
        if broken:
            return ToolResponse(content=[TextBlock(type="text", text=broken)])
        summary = (
            f"已生成工作簿: {p}（{len(sheets)} 张表，{total_rows} 行数据"
            + (f"，{total_formulas} 处公式" if total_formulas else "")
            + "；公式数值由 Excel/WPS 打开时计算）"
        )
        if notes:
            summary += "\n说明：" + "；".join(notes)
        return ToolResponse(content=[TextBlock(type="text", text=summary)])

    def generate_docx(file_path: str, blocks_json: str, title: str = "",
                      header_text: str = "", footer_text: str = ""):
        """生成真实的 Word 文档（.docx）：标题层级、段落、两种列表、表格、页眉页脚与中文字体。

        Args:
            file_path (`str`):
                输出路径，必须以 .docx 结尾（相对路径会解析到工作目录内）。
            blocks_json (`str`):
                内容块数组的 JSON 字符串，每项形如 {"type": "heading", "text": "小节", "level": 2}
                / {"type": "paragraph", "text": "正文"} / {"type": "bullet", "text": "要点"}
                / {"type": "numbered", "text": "步骤"} /
                {"type": "table", "header": ["字段", "说明"], "rows": [["a", "b"]]}。
                type 也接受 para/text/h1~h4/ul/ol 等常见写法，纯字符串按段落处理；
                不认识的类型会被跳过并在返回里点名，不会静默少一段。
                上限：600 块、单表 200 行、20 列、单元格 2000 字，超出会截断并逐条说明。
            title (`str`, optional):
                文档大标题（Title 样式，排在最前），缺省不加。
            header_text (`str`, optional):
                页眉文字，缺省不加。
            footer_text (`str`, optional):
                页脚文字，缺省不加。
        """
        p = _resolve(file_path)
        if p.suffix.lower() != ".docx":
            return ToolResponse(content=[TextBlock(
                type="text", text=f"错误：文档路径必须以 .docx 结尾（收到 {p.name}）"
            )])
        err = _guard(p, write=True)
        if err:
            return ToolResponse(content=[TextBlock(type="text", text=err)])
        try:
            import docx
        except ImportError:
            return ToolResponse(content=[TextBlock(
                type="text", text="错误：未安装 python-docx，无法生成 .docx（请安装 python-docx 后重试）"
            )])
        items, bad = _office_items(blocks_json, "blocks")
        if bad:
            return ToolResponse(content=[TextBlock(type="text", text=f"错误：blocks_json {bad}")])
        blocks, notes = _docx_normalize(items)
        if not blocks and not str(title or "").strip():
            return ToolResponse(content=[TextBlock(
                type="text",
                text="错误：blocks_json 里没有可用的内容块"
                + ("\n说明：" + "；".join(notes) if notes else ""),
            )])
        counts = {"heading": 0, "paragraph": 0, "bullet": 0, "numbered": 0, "table": 0}
        chars = 0
        try:
            document = docx.Document()
            font_note = _docx_apply_fonts(document)
            if font_note:
                notes.append(font_note)
            heading_title = str(title or "").strip()
            if heading_title:
                document.add_heading(heading_title, level=0)
                counts["heading"] += 1
                chars += len(heading_title)
            for block in blocks:
                kind = block["kind"]
                chars += len(block.get("text") or "")
                for row in block.get("rows") or []:
                    chars += sum(len("" if value is None else str(value)) for value in row)
                chars += sum(len(str(value)) for value in (block.get("header") or []))
                if kind == "heading":
                    document.add_heading(block["text"], level=block["level"])
                elif kind == "table":
                    _docx_add_table(document, block, notes)
                else:
                    style_name = {"bullet": "List Bullet", "numbered": "List Number"}.get(kind)
                    if style_name is None:
                        document.add_paragraph(block["text"])
                    else:
                        try:
                            document.add_paragraph(block["text"], style=style_name)
                        except KeyError:
                            # 内置模板缺这个样式时退成普通段落，比整份文档生成失败好；
                            # 但必须说「不是真列表」，否则用户以为能接着往下编号。
                            document.add_paragraph(block["text"])
                            notes.append(f"当前模板没有列表样式「{style_name}」，已退为普通段落")
                counts[kind] = counts.get(kind, 0) + 1
            for label, text in (("页眉", header_text), ("页脚", footer_text)):
                value = str(text or "").strip()
                if not value:
                    continue
                try:
                    part = document.sections[0].header if label == "页眉" else document.sections[0].footer
                    part.is_linked_to_previous = False
                    part.paragraphs[0].text = _office_clip(value)
                except Exception as exc:  # noqa: BLE001 - 页眉页脚缺失不该拖垮整份文档
                    notes.append(f"{label}写入失败（{exc}），正文不受影响")
            p.parent.mkdir(parents=True, exist_ok=True)
            document.save(str(p))
        except Exception as exc:  # noqa: BLE001 - 生成失败要说清是哪一步
            return ToolResponse(content=[TextBlock(type="text", text=f"错误：文档生成失败：{exc}")])
        broken = _office_check_package(p)
        if broken:
            return ToolResponse(content=[TextBlock(type="text", text=broken)])
        summary = (
            f"已生成文档: {p}（{len(blocks)} 块：标题 {counts['heading']} 处、段落 {counts['paragraph']} 段、"
            f"列表 {counts['bullet'] + counts['numbered']} 条、表格 {counts['table']} 张，正文约 {chars} 字）"
        )
        if notes:
            summary += "\n说明：" + "；".join(notes)
        return ToolResponse(content=[TextBlock(type="text", text=summary)])

    def list_directory(path: str = ".", depth: int = 1, limit: int | None = None, offset: int = 0):
        """列出目录里的文件与子目录（名称、类型、大小），用于先看清目录再决定读哪些文件。

        Args:
            path (`str`, optional):
                目录路径（相对路径按工作目录解析）；缺省为工作目录本身。
            depth (`int`, optional):
                递归深度：1 只看当前目录，最大 3。
            limit (`int`, optional):
                这一页最多返回多少条，默认 20、上限 50；超出上限会被夹到上限并写明
                夹到了多少，需要更多请带 offset 翻页，别一次要全。
            offset (`int`, optional):
                从第几条开始列（从 0 计），缺省 0。截断提示里给的 offset= 可以照抄回来。
        """
        p = _resolve(path or ".")
        err = _guard(p)
        if err:
            return ToolResponse(content=[TextBlock(type="text", text=err)])
        if not p.exists():
            return ToolResponse(content=[TextBlock(type="text", text=f"错误：目录不存在: {path}")])
        if not p.is_dir():
            return ToolResponse(content=[TextBlock(type="text", text=f"错误：不是目录: {path}")])
        # 显式 0 走夹取（下限 1），只有缺省/非数字才回落到默认值
        try:
            depth_n = int(depth)
        except (TypeError, ValueError):
            depth_n = 1
        depth_n = max(1, min(depth_n, 3))
        cap, cap_note = twa.clamp_units(
            limit, default=twa.LIST_DEFAULT_ITEMS, maximum=twa.LIST_MAX_ITEMS, what="列出条数"
        )
        offset_note = ""
        if offset is None or offset == "":
            offset_n = 0
        else:
            try:
                offset_n = int(str(offset).strip())
            except (TypeError, ValueError):
                offset_n, offset_note = 0, f"offset「{offset}」不是整数，本次从头列起。"
            else:
                if offset_n < 0:
                    offset_n, offset_note = 0, f"offset「{offset_n}」是负数，本次从头列起。"

        root = p.resolve()
        # 多收一条才知道「这一页之后还有没有」；只收到 cap 会把「还压着没给的那几条」
        # 报成「已经列完」（与 Rust 侧同一把尺子，见 harness/tools.rs 的同名分支）
        want = offset_n + cap + 1
        rows: list[str] = []
        hit_cap = False

        def _size_label(size: int) -> str:
            value = float(size)
            for unit in ("B", "KB", "MB", "GB"):
                if value < 1024 or unit == "GB":
                    return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
                value /= 1024
            return f"{size} B"

        for current, dirs, files in os.walk(root, followlinks=False):
            cur = Path(current)
            level = len(cur.relative_to(root).parts)
            dirs[:] = sorted(dirs)
            entries = [(name, True) for name in dirs] + [(name, False) for name in sorted(files)]
            for name, is_dir in entries:
                if len(rows) >= want:
                    hit_cap = True
                    break
                item = cur / name
                rel = item.relative_to(root).as_posix()
                if is_dir:
                    rows.append(f"[目录] {rel}/")
                else:
                    try:
                        size = _size_label(item.stat().st_size)
                    except OSError:
                        size = "?"
                    rows.append(f"[文件] {rel}  {size}")
            if hit_cap or level + 1 >= depth_n:
                dirs[:] = []
            if hit_cap:
                break

        # 「按剩余窗口取页」对列表的落点：**收完这一页之后再压**，不预先猜每条多长。
        # 拿真实条目的平均字符折算单条成本（`list_unit_tokens`），只许把这一页压小、
        # 不许抬高（maximum 传当前 cap）；窗口宽到不需要压时连一句窗口的话都不多写。
        reading = _window_reading()
        if reading and rows:
            eff_default, eff_cap, window_note = twa.window_budget(
                reading,
                default=min(cap, len(rows)),
                maximum=cap,
                unit_tokens=twa.list_unit_tokens(rows[:cap]),
                what="项",
                floor=twa.WINDOW_PAGE_FLOOR_ITEMS,
            )
            if window_note and eff_cap < cap:
                cap, cap_note = eff_cap, (cap_note + " " if cap_note else "") + window_note
        page = rows[offset_n : offset_n + cap]
        has_more = hit_cap or len(rows) > offset_n + len(page)
        if page:
            header = f"目录：{root}（深度 {depth_n}，本次第 {offset_n + 1}–{offset_n + len(page)} 条，单页上限 {cap} 条）"
        else:
            header = f"目录：{root}（深度 {depth_n}，单页上限 {cap} 条）"
        notes = " ".join(x for x in (cap_note.strip(), offset_note.strip()) if x)
        if not rows:
            return ToolResponse(content=[TextBlock(type="text", text=header + "\n（目录为空）")])
        if not page:
            tail = "（这个偏移之后没有条目了；要回头看请带 offset=0）"
            return ToolResponse(
                content=[TextBlock(type="text", text=header + ("\n" + notes if notes else "") + "\n" + tail)]
            )
        body = "\n".join(page)
        if has_more:
            body += f"\n…（单页 {cap} 条已到，后面还有更多没列——接着列请带 offset={offset_n + cap}，别指望一次看全）"
        return ToolResponse(
            content=[TextBlock(type="text", text=header + ("\n" + notes if notes else "") + "\n" + body)]
        )

    def read_document(file_path: str, max_chars: int | None = None, start: int = 0):
        """读取 Word（.docx）、Excel（.xlsx/.xlsm）、PowerPoint（.pptx）或 PDF 的正文。

        文本类文件请用 view_text_file；本工具用于二进制文档，直接把正文转成文本返回。
        正文按分页预算给出：默认 4000 字符、上限 8000 字符，截断时会告诉你带哪个 start 接着读。

        Args:
            file_path (`str`):
                文件路径（相对路径会解析到工作目录内）。
            max_chars (`int`, optional):
                这一页最多返回多少字符，默认 4000、上限 8000；超出上限会被夹到上限并写明
                夹到了多少。要读整篇请带 start 翻页，别一次要超量。
            start (`int`, optional):
                从正文第几个字符开始返回，缺省 0。截断提示里给的 start= 可以照抄回来接着读。
        """
        p = _resolve(file_path)
        err = _guard(p)
        if err:
            return ToolResponse(content=[TextBlock(type="text", text=err)])
        if not p.exists() or not p.is_file():
            return ToolResponse(content=[TextBlock(type="text", text=f"错误：文件不存在: {file_path}")])

        suffix = p.suffix.lower()
        text = ""
        note = ""
        try:
            if suffix == ".docx":
                try:
                    import docx
                except ImportError:
                    return ToolResponse(content=[TextBlock(
                        type="text", text="错误：未安装 python-docx，无法读取 .docx（附件链路仍可抽取正文）"
                    )])
                document = docx.Document(str(p))
                blocks = [para.text for para in document.paragraphs]
                for table in document.tables:
                    for row in table.rows:
                        cells = [cell.text.strip() for cell in row.cells]
                        if any(cells):
                            blocks.append("\t".join(cells))
                text = "\n".join(block for block in blocks if block.strip())
            elif suffix in (".xlsx", ".xlsm"):
                try:
                    import openpyxl
                except ImportError:
                    return ToolResponse(content=[TextBlock(
                        type="text", text="错误：未安装 openpyxl，无法读取 Excel 文件（附件链路仍可抽取正文）"
                    )])
                workbook = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
                try:
                    sheets = []
                    for sheet in workbook.worksheets:
                        rows = []
                        for row in sheet.iter_rows(values_only=True):
                            values = ["" if value is None else str(value) for value in row]
                            if any(value.strip() for value in values):
                                rows.append("\t".join(values).rstrip())
                        if rows:
                            sheets.append(f"## 工作表：{sheet.title}\n" + "\n".join(rows))
                    text = "\n\n".join(sheets)
                finally:
                    workbook.close()
            elif suffix == ".pptx":
                try:
                    from pptx import Presentation
                except ImportError:
                    return ToolResponse(content=[TextBlock(
                        type="text", text="错误：未安装 python-pptx，无法读取 .pptx"
                    )])
                presentation = Presentation(str(p))
                slides = []
                for index, slide in enumerate(presentation.slides, 1):
                    lines = [
                        shape.text.strip()
                        for shape in slide.shapes
                        if hasattr(shape, "text") and str(shape.text).strip()
                    ]
                    if lines:
                        slides.append(f"## 第 {index} 页\n" + "\n".join(lines))
                text = "\n\n".join(slides)
            elif suffix == ".pdf":
                try:
                    from pypdf import PdfReader
                except ImportError:
                    return ToolResponse(content=[TextBlock(
                        type="text", text="错误：未安装 pypdf，无法读取 PDF（附件链路仍可抽取正文）"
                    )])
                reader = PdfReader(str(p))
                if reader.is_encrypted:
                    return ToolResponse(content=[TextBlock(
                        type="text", text="错误：PDF 已加密，请先解除密码或提供未加密副本"
                    )])
                max_pages = 300
                pages = []
                for index, page in enumerate(reader.pages):
                    if index >= max_pages:
                        note = f"（仅读取前 {max_pages} 页，全文共 {len(reader.pages)} 页）"
                        break
                    try:
                        page_text = page.extract_text() or ""
                    except Exception:  # noqa: BLE001 - 单页解析失败不应拖垮整份文档
                        page_text = ""
                    if page_text.strip():
                        pages.append(f"## 第 {index + 1} 页\n{page_text.strip()}")
                text = "\n\n".join(pages)
            elif suffix in {".txt", ".md", ".markdown", ".csv", ".log", ".json", ".yaml", ".yml"}:
                text = p.read_text(encoding="utf-8", errors="replace")
            else:
                return ToolResponse(content=[TextBlock(
                    type="text",
                    text=(
                        f"错误：不支持的文档格式 {suffix or '（无扩展名）'}；"
                        "支持 .docx/.xlsx/.pptx/.pdf 与文本类文件"
                    ),
                )])
        except Exception as exc:  # noqa: BLE001 - 第三方解析库的异常统一转成可读原因
            return ToolResponse(content=[TextBlock(type="text", text=f"错误：读取文档失败：{exc}")])

        if not text.strip():
            return ToolResponse(content=[TextBlock(
                type="text",
                text=(
                    f"错误：未能从 {p.name} 抽取到正文。"
                    "若为扫描件/图片型 PDF，请先用 OCR 转成可复制文本，或在输入框以附件方式发送"
                ),
            )])
        # 预算走执行体同一把尺子（默认 4000、上限 8000 字符）：一条自身就超窗口的工具结果
        # 是压缩救不回来的那一类，而「分段查看」这句提示自此才有对应参数——以前只截前缀、
        # 没有分页入口，模型每轮拿到的都是同一段。
        page = twa.budgeted_read(text, max_chars, start, window=_window_reading())
        clipped = str(page["note"] or "").strip()
        header = f"已读取：{p}（{suffix[1:]}）"
        if note:
            header += note
        return ToolResponse(content=[TextBlock(
            type="text",
            text=header + "\n\n" + page["text"] + (("\n" + clipped) if clipped else ""),
        )])

    toolkit = Toolkit()
    toolkit.register_tool_function(list_directory)
    toolkit.register_tool_function(view_text_file)
    toolkit.register_tool_function(read_document)
    toolkit.register_tool_function(write_text_file)
    toolkit.register_tool_function(insert_text_file)
    # 删除是这一版新加的能力，注册即对模型可见；能不能真删由文件访问档位判定
    # （只有「完全访问」放行），所以这里不需要按档位条件注册。
    toolkit.register_tool_function(delete_text_file)
    # 模式作用域工具与授权工具交给车道（#164 接缝）：开关在哪、由谁授权，只在
    # engine/toolkit_lanes.SWITCH_BOARD 里声明一次，各窗口只填自己那份 toolkit_<domain>.py。
    # 常驻六件刻意留在上面不动车道：没有 Rust harness 实现的能力进了常驻清单，
    # 界面上就是空头承诺（tests/unit/extensions/builtin-tools-contract.test.ts 锁这条口径），
    # 车道若抢注常驻名或开关全假还注册，register_all 当场拒掉。
    tl.register_all(tl.ToolkitContext(
        toolkit=toolkit,
        work_dir=work_dir,
        base=base,
        sandbox=sandbox,
        emit=emit,
        task_id=task_id,
        slides=slides,
        office=office,
        grants=grants,
        pptx_templates=pptx_templates,
        tools={
            "generate_pptx": generate_pptx,
            "generate_xlsx": generate_xlsx,
            "generate_docx": generate_docx,
        },
        extra=extra,
    ))
    return toolkit


def build_skill_tool(skills: list):
    """技能/套件指令正文工具工厂：注册 use_skill 让模型自己取正文。

    清单只把 name/description 放进 system prompt（装几十个技能也不撑爆上下文），
    正文按需用工具读。套件指令与技能走同一条路径，缺正文时返回安装侧原因。
    这条注册刻意留在 build_toolkit 之外：/api/tools 与
    tests/unit/extensions/builtin-tools-contract.test.ts 锁的是「内置」四个文件工具，
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


def _usage_capture_for_window(on_usage, meter: "twa.ReadWindowMeter"):
    """把「上一次调用回报的 prompt 量」记进取页窗口读数，然后**原样**转发用量回调。

    None 也接：没接用量回调的装配（自测、老调用点）照样要有窗口读数，否则取页永远只能走固定
    预算。回调本身失败不改行为——沿用的是 `UsageTrackingModel` 那条「记账失败不影响模型调用链」
    的既有口径，这里只是它前面的一站。

    `from_report=False` 的读法留给没回报用量的路径（上游没回 usage 时 `prompt` 是 0）；
    `note_usage` 对非正数直接不动，所以「这次没量」不会把已有的量冲掉。"""

    def captured(*args: Any, **kwargs: Any):
        try:
            if args:
                meter.note_usage(args[0])
            elif kwargs:
                meter.note_usage(kwargs.get("prompt_tokens"))
        except Exception:  # noqa: BLE001  读数记不上不影响真正该转发的用量回调
            pass
        if on_usage is not None:
            return on_usage(*args, **kwargs)
        return None

    return captured


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

    # D-48.8「按剩余窗口取页」：这一轮的窗口读数对象先建后填——窗口要等下面的策略归一化
    # 算完（故障转移链里可能换成更小的那个），用量要等第一次模型调用回报。取页工具读的
    # 是这个对象的**当下值**，所以装配期快照不会过期。
    read_window = twa.ReadWindowMeter()
    on_usage_with_window = _usage_capture_for_window(on_usage, read_window)

    model, formatter = build_model_and_formatter(
        model_binding, model_config, on_usage=on_usage_with_window,
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
    # 窗口读数在此填齐：策略已经算完（含故障转移链取最小窗口那一条），取页与压缩用的是同一个
    # `context_window`，不另算一套。`from_model` 决定页面能不能说「按你填的窗口」。
    read_window.configure(policy.context_window, policy.context_window_from_model)
    model_name = (
        getattr(model, "model_name", "")
        or _effective_model_name(model_binding, model_config)
    )
    compression, compression_error = cb.build_compression(policy, model_name)
    mcp_count = tsr.mcp_server_count(runtime)
    mcp_tools = tsr.declared_tool_count(runtime)
    # 阈值判定用 Rust 探测缓存里的工具数：在建立 Agent 前就能定，
    # 提示词与用户看到的策略说明不会和实际注册方式各说各的
    lazy_mcp = tsr.should_lazy(runtime, policy.lazy_mcp_tools)
    context_notes = policy.notes(mcp_count, mcp_tools, lazy_mcp)
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
    # D-03.5：写暂存档位（缺省 off = 照旧直接写盘）。开启后文本写入工具不再碰目标文件，
    # 内容先进引擎数据根的暂存区，用户在界面看过 diff 批准后才由 `/api/staging/apply` 合并。
    # 档位只影响 `write_text_file` / `insert_text_file` 两件；office/pptx 生成件与删除仍走原路，
    # 这一点在暂存模块的文档字符串和给模型的回执里都写明了，不在这里悄悄扩大范围。
    staging = wsr.WriteStaging.from_runtime(runtime)
    if staging.staged:
        sys_prompt += staging.system_hint()
    if staging.staged or staging.error:
        # 「本轮到底有没有生效、为什么没生效」要和沙箱/审批同一口径摊给用户；
        # 缺省档位不进 notes，否则每条对话都多一句没信息量的「写暂存：直接写盘」
        context_notes.append(staging.note())
    toolkit = build_toolkit(
        work_dir or WORK_DIR, sandbox=sandbox, slides=rc.normalize_mode(mode) == "slides",
        # 办公产物在通用与写作两种模式下都给：用户最常在最普通的对话里直接说
        # 「把这个做成一份 Excel」，只放行写作模式会让最常见的那句要求拿不到真件。
        office=rc.normalize_mode(mode) in ("general", "writing"),
        grants=grants, emit=emit, task_id=task_id,
        pptx_templates=(runtime or {}).get("pptxTemplates")
        if isinstance(runtime, dict) else None,
        # 车道补充开关：把 build_agent 已有的装配现场交下去，W2/W6 之后加工具不必再开本文件。
        # 这些对象在下面仍以原有顺序各自 install，车道只是「开关是否点亮」的读数，不改任何行为。
        # artifact_history 是 D-26 的第二把开关（分片 138.md §5-a）：产物版本历史不该挂在
        # 「装了知识库」那把门上——没挂知识库的会话同样需要能回滚被覆盖的产物。
        extra={"knowledge": knowledge, "subagent": subagent_runner, "artifact_history": ahr,
               # 写暂存是「装配现场的政策对象」，和 knowledge/subagent 同一条补充开关通道：
               # 直接调 build_toolkit 的自测拿不到它，行为与改造前逐字相同（缺省=直接写盘）。
               "staging": staging,
               # 取页窗口同理走 extra：没给（自测直接调 build_toolkit）就是固定预算，
               # 与改造前逐字相同；给了则三个读取件按剩余窗口压这一页的上限。
               "read_window": read_window},
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
    # 长期记忆工具：只在 Rust 下发了桌面记忆库时注册；浏览器直连没有落库通道，
    # 不注册假工具，避免模型以为保存成功。
    if mt.install(toolkit, runtime, emit, secret_check=_looks_like_secret):
        sys_prompt += mt.system_hint(runtime)
        context_notes.append(
            "长期记忆工具已启用：memory_search / memory_get / memory_save"
            "（写入由桌面端去重、判冲突并与待审开关联动）"
        )
    # 本地子 Agent：只有父 Agent 注册派发工具；子 Agent 走 subagent_runner=None，
    # 从工具面上杜绝递归派生。提示词与工具同时出现，避免「模型知道有这能力但无工具」。
    if subagent_runner is not None:
        toolkit.register_tool_function(subagent_runner.tool())
        sys_prompt += subagent_runner.system_hint()
    if lazy_mcp:
        try:
            tsr.install(toolkit)
            sys_prompt += tsr.system_hint(mcp_count, mcp_tools)
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
        trackers = _usage_trackers(model)

        async def _safe_compress():
            for tracker in trackers:
                tracker.last_structured_ok = None   # 每次压缩前复位，不读上一轮的结论
            try:
                await original_compress()
            except Exception as exc:  # noqa: BLE001
                agent._tt_compression_error = " ".join(str(exc).split())[:240]
                if getattr(agent, "compression_config", None) is not None:
                    agent.compression_config.enable = False
                return
            # 没抛异常不等于压成了：模型不按结构化协议回摘要时，AgentScope 只留一条
            # WARNING 就继续跑，上下文一点没减，而且每一轮都会再白烧一次摘要调用
            # （长任务基准实测：36 次触发、36 次失败、36 次白花）。这里把静默失败
            # 变成用户看得见的说明，并关掉本次任务的压缩重试。
            if trackers and all(t.last_structured_ok is False for t in trackers):
                agent._tt_compression_error = (
                    "模型未按结构化输出协议返回压缩摘要（端点不支持或未按 tools/response_format 应答）"
                )
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
    # D-28：这里有本轮唯一能拿到的事件循环对象——循环回调里抛出的异常只有接得上循环才留得住
    _crash_telemetry_bootstrap(asyncio.get_running_loop())
    await _im_bootstrap()
    try:
        # 常驻连接枢纽绑定到这个循环：attach/收尾都在它自己的任务里排队（D-40 第 5 项）
        await MCP_HUB.start()
    except Exception as exc:
        print(f"[engine] MCP 连接枢纽启动失败，连接器退回一次性连接: {exc}", flush=True)
    _ticket_worker_startup()  # D-48.7：工单执行体的装配 + 外发周期泵（失败不拦引擎启动）
    yield
    try:
        await MCP_HUB.shutdown()
    except Exception as exc:
        print(f"[engine] MCP 连接枢纽关停失败: {exc}", flush=True)
    await im_channels.shutdown()
    try:
        TERMINALS.close_all()
    except Exception as exc:
        print(f"[engine] 终端会话清理失败: {exc}", flush=True)
    await _ticket_worker_shutdown()  # D-48.7：收尾在飞回合并拆掉单例（下一轮启动要重新认数据根）


# ── 工单执行体的进程内生命周期（D-48.7）────────────────────────
# 装配本体在 ticket_worker_service（那里有 190 项自测）；这一层只补两件只在「跑在引擎里」
# 才存在的问题：
#   1) 事件循环。worker 回合是同步线程，IM 通道挂在 uvicorn 的循环上，投递要跨循环回投，
#      所以循环对象必须在 lifespan 里捕获——构造期拿不到，构造期也不该去猜。
#   2) 外发周期泵。盘上欠着的外发（退避未到的、上一进程没发完的）不该等「有人再点一下」
#      才发出去；没有这一条，D-48.2 的耐久队列在重启后就只是躺在那儿。
DEFAULT_TICKET_OUTBOX_POLL_SECONDS = 20.0
_TICKET_LOOP: asyncio.AbstractEventLoop | None = None
_TICKET_PUMP_TASK: "asyncio.Task | None" = None


def _ticket_outbox_poll_seconds() -> float:
    """周期泵间隔：`TT_TICKET_OUTBOX_POLL_SECONDS`，给 0/off 就是关掉泵（端点照旧能手动推）。

    下界 1 秒：外发是真发网络请求，0.1 秒一次的泵只会把通道打成排队。"""
    raw = str(os.environ.get("TT_TICKET_OUTBOX_POLL_SECONDS") or "").strip().lower()
    if raw in ("0", "off", "no", "false", ""):
        if raw == "":
            return DEFAULT_TICKET_OUTBOX_POLL_SECONDS
        return 0.0
    try:
        return max(1.0, float(raw))
    except ValueError:
        return DEFAULT_TICKET_OUTBOX_POLL_SECONDS


def _ticket_runtime():
    """取（必要时建）工单执行体装配；循环在这里给进去，构造之后通道投递就认它。

    只在建的时候生效（`get_runtime` 的 kwargs 语义）：已经建好之后再传循环是假的「换了一个
    循环」，旧装配里的通道还挂着原来那个。"""
    return tws.get_runtime(loop=_TICKET_LOOP)


def _ticket_worker_startup() -> None:
    """lifespan 启动段：捕获循环 + 建装配 + 起泵。任何一步失败都只打印，不拦引擎起来。

    工单执行体是可选能力：它建不起来（数据根不可写等）时，桌面其余功能照常用，
    端点会各自如实报失败；在这里抛出等于让一个可选执行体拖垮整个引擎。"""
    global _TICKET_LOOP, _TICKET_PUMP_TASK
    try:
        _TICKET_LOOP = asyncio.get_running_loop()
    except RuntimeError as exc:
        print(f"[engine][tickets] 没有可用事件循环，工单执行体退回无循环模式: {exc}", flush=True)
    try:
        runtime = _ticket_runtime()
    except Exception as exc:  # noqa: BLE001
        print(f"[engine][tickets] 工单执行体装配创建失败，工单端点将逐个如实报错: {type(exc).__name__}: {exc}", flush=True)
        return
    poll_seconds = _ticket_outbox_poll_seconds()
    if poll_seconds <= 0:
        print("[engine][tickets] 外发周期泵已关闭（TT_TICKET_OUTBOX_POLL_SECONDS=0）：只由端点手动推", flush=True)
        return
    try:
        _TICKET_PUMP_TASK = asyncio.create_task(_ticket_outbox_pump(poll_seconds))
    except Exception as exc:  # noqa: BLE001
        print(f"[engine][tickets] 外发周期泵启动失败，外发只能靠端点推: {type(exc).__name__}: {exc}", flush=True)


async def _ticket_outbox_pump(poll_seconds: float) -> None:
    """周期性推外发队列：真正的投递在 `asyncio.to_thread` 里跑，不占引擎循环。

    `run_outbox` 同步等通道回话（最长 30 秒一条），放在循环里会让整个引擎卡住；
    失败也不退出循环：一条发不出去是那条的事，泵停会让盘上所有欠账一起失联。"""
    while True:
        await asyncio.sleep(poll_seconds)
        try:
            if tws.peek_runtime() is None:
                continue
            result = await asyncio.to_thread(_ticket_runtime().run_outbox)
            if not result.get("ok", True):
                print(f"[engine][tickets] 周期推外发失败：{result.get('reason') or result}", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[engine][tickets] 周期推外发异常（下一轮继续）：{type(exc).__name__}: {exc}", flush=True)


async def _ticket_worker_shutdown() -> None:
    """lifespan 关停段：停泵 + 收在飞回合。报告里有没停干净的回合就如实打印。"""
    global _TICKET_PUMP_TASK, _TICKET_LOOP
    task = _TICKET_PUMP_TASK
    _TICKET_PUMP_TASK = None
    if task is not None:
        task.cancel()
        # 泵任务被取消时抛的是 CancelledError（BaseException 派生），不接住会让关停整段碎掉
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001  泵自己的异常在循环里已打过，这里只收尾
            print(f"[engine][tickets] 外发周期泵停用时抛错：{type(exc).__name__}: {exc}", flush=True)
    try:
        report = tws.reset_runtime(timeout_seconds=2.0)
    except Exception as exc:  # noqa: BLE001
        print(f"[engine][tickets] 工单执行体收尾失败: {type(exc).__name__}: {exc}", flush=True)
        report = {}
    _TICKET_LOOP = None
    if report.get("existed") and (report.get("inFlight") or report.get("runningJobs")):
        print(f"[engine][tickets] 关停时仍有未收口：{report}", flush=True)


# ── D-28：崩溃留存挂钩 + 可选遥测的装配现场 ────────────────────
#
# 为什么必须有这一段：引擎是桌面壳起的子进程，`src-tauri/src/bridge/agent.rs::pump_logs()`
# 把它的 stdout/stderr 一路转成 GUI 进程的 `println!`，而 GUI 是
# `windows_subsystem="windows"`（`src-tauri/src/main.rs:1`）——**那两条管道没有目的地**。
# 于是一条未捕获异常在用户那边只剩「任务转圈然后失败」，traceback 一个字都没落到任何地方，
# 重启之后连「刚才崩过」都无法证明。`crash_runtime` 负责留存与导出，`telemetry_runtime`
# 负责「默认关的可选计数」，这一段只做三件只有跑在引擎里才存在的问题：
#
#   1) 装钩子并拿到事件循环对象——构造期拿不到（与 `_TICKET_LOOP` 同一个理由），
#      循环回调里抛出的异常只有 lifespan 才接得上；
#   2) 把「新落了一份现场」转成一条 `crash_recorded` 计数。观察者由装配现场登记，
#      `crash_runtime` 本身不认识 `telemetry_runtime`：留存不该依赖外发面，
#      关掉遥测时崩溃现场照样落盘；
#   3) 起一轮记一条 `engine_started`，收尾记一条 `task_finished`。
#
# 三件都**不许拦住启动或收尾**：本段的任何失败只印一句中文说明（那句能不能被人看见另说，
# 但不能因为「想留现场」把引擎弄不起来）。字段来源逐条写在 `_telemetry_task_finished` 里，
# 不在表里的字段传不进去（白名单在 `telemetry_runtime.ALLOWED_FIELDS`）。

_CRASH_TELEMETRY: dict[str, Any] = {"bootstrap": {}, "startedEmitted": False, "crashObserver": 0}


def _os_family() -> str:
    """只到「哪个系统家族」这一层：不带发行版、不带机器名、不提用户名。"""
    try:
        import platform as _platform

        return str(_platform.system() or "")[:24]
    except Exception:  # noqa: BLE001  取不到就不带这一列，不为此丢一条计数
        return ""


def _crash_observer(event: dict) -> None:
    """崩溃 → 计数事件：只带类别与「本机还有几份没导出」，正文一个字也不进遥测。"""
    try:
        _CRASH_TELEMETRY["crashObserver"] = int(_CRASH_TELEMETRY.get("crashObserver") or 0) + 1
        tlm.record("crash_recorded", {
            "engineVersion": ENGINE_VERSION,
            "category": str(event.get("category") or ""),
            "pendingReports": int(crt.status().get("count") or 0),
        })
    except Exception as exc:  # noqa: BLE001  计数记不上不影响留存本身
        print(f"[engine][crash] 崩溃计数事件记不上: {type(exc).__name__}: {exc}", flush=True)


def _crash_telemetry_bootstrap(loop: Any = None) -> dict[str, Any]:
    """装配崩溃钩子 + 遥测观察者 + 一条 engine_started；**幂等**，任何失败只说明不拦停。

    两段各自带一层兜底，而且**钩子那一段在前**：如果 `set_recorder` 或记 `engine_started`
    这一半炸了，崩溃留存必须已经装好——遥测是可选项（默认还关着），现场是不可选项。
    反过来共用一个 try 的话，遥测侧一个异常就把留存一起带走了，正是这段想治的病。
    """
    installed: dict[str, Any] = {}
    try:
        installed = dict(crt.install(engine_version=ENGINE_VERSION, loop=loop))
    except Exception as exc:  # noqa: BLE001  挂钩本身失败也不能影响启动
        installed = {"ok": False, "installed": [], "installError": f"{type(exc).__name__}: {str(exc)[:200]}"}
    try:
        crt.set_recorder(_crash_observer)
        if not _CRASH_TELEMETRY["startedEmitted"]:
            _CRASH_TELEMETRY["startedEmitted"] = True
            tlm.record("engine_started", {
                "engineVersion": ENGINE_VERSION,
                "osFamily": _os_family(),
                "pythonFamily": str(getattr(sys, "implementation", None) and sys.implementation.name or ""),
            })
    except Exception as exc:  # noqa: BLE001  观察者/计数这一半炸了不该把已装好的钩子带下去
        installed["recorderError"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    result = {**installed, "recorder": crt.recorder_state()}
    _CRASH_TELEMETRY["bootstrap"] = result
    if not installed.get("ok"):
        print(f"[engine][crash] 崩溃钩子没装上: {installed.get('installError')}", flush=True)
    return result


def _telemetry_task_finished(session: "TaskSession", end_reason: str, checkpoint: Any) -> dict[str, Any]:
    """任务收尾计一条 `task_finished`。字段来源逐条写明，不在这张表里的东西传不进去。

    `privacy_mode` 走 `record(..., privacy=)`：隐私轮**连计数都不记**——隐私档的语义是
    「这一轮的内容与轨迹都不出本机」，遥测不是例外。其余字段：

    - `endReason` ← `run_agent` 的 `end_reason`（本文件里只有四处赋值，词表与之逐字对上）
    - `modelCallCount` / `toolCallCount` ← `checkpoint.last_summary` 的 `modelCalls` /
      `executedTools`（「崩/停在第几次调用上」是这条事件存在的理由）
    - `checkpointResumable` ← `checkpoint.resumable`
    - `secureEnv` ← `session.secure_env`；`durationMs` ← `session.created_at`
    """
    try:
        summary = getattr(checkpoint, "last_summary", {}) or {}
        fields = {
            "engineVersion": ENGINE_VERSION,
            "endReason": str(end_reason or ""),
            "durationMs": int(max(0.0, time.time() - float(getattr(session, "created_at", time.time()))) * 1000),
            "modelCallCount": int(summary.get("modelCalls") or 0),
            "toolCallCount": int(summary.get("executedTools") or 0),
            "hadError": end_reason == "error",
            "privacyBlocked": end_reason == "privacy_blocked",
            "checkpointResumable": bool(getattr(checkpoint, "resumable", False)),
            "secureEnv": bool(getattr(session, "secure_env", False)),
        }
        return tlm.record("task_finished", fields, privacy=bool(getattr(session, "privacy_mode", False)))
    except Exception as exc:  # noqa: BLE001  收尾计数记不上不影响这一轮交付
        return {"ok": False, "buffered": False, "reason": f"收尾计数失败：{type(exc).__name__}: {str(exc)[:160]}"}


# ── FastAPI 应用 ───────────────────────────────────────────────

app = FastAPI(title="tongtian-agent-engine", version=ENGINE_VERSION, lifespan=_im_lifespan)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """请求级未捕获异常：留一份崩溃现场，再回一句人话（缺陷 D-28 后半，#141）。

    为什么必须在这里接：Starlette 默认把未处理异常抛回 ASGI 服务器，uvicorn 只往
    stderr 印一段——而引擎的 stderr 在桌面壳那边没有目的地（见上一节的说明）。
    也就是说**今天这些异常是彻底消失的**，用户只看到一个转圈失败的界面。

    三条口径：

    - **只留现场、不改语义**：响应仍是 500，`code` 固定一个，`message` 是中文；
      异常原文与 traceback 不进响应体（远程调用方不该看见本机栈），只进本机现场；
    - **给得出编号**：`crashReport` 让界面能写「这次崩溃已记为 X，可在设置页导出」，
      人不用去猜目录名；本机才有详情，读详情走 desktop-only 的 `/api/exec/crash/detail`；
    - **留存自己再炸也不能把响应弄没**：`handle_exception` 本身永不抛出，这里再包一层，
      最坏情况是这次没留下现场，但请求仍回一个能读的 500。
    """
    scene = crt.handle_exception(
        "http_unhandled",
        type(exc),
        exc,
        exc.__traceback__,
        context={"endpoint": str(getattr(getattr(request, "url", None), "path", "") or "")[:120]},
        note=f"{getattr(request, 'method', '')} {getattr(getattr(request, 'url', None), 'path', '')}".strip(),
    )
    body: dict[str, Any] = {
        "ok": False,
        "code": "ENGINE_INTERNAL_ERROR",
        "message": (
            "引擎在处理这个请求时遇到了未预期的异常，已经在本机留下一份可导出的现场"
            "（设置页 → 崩溃记录）。这次请求没有完成，重复同样的操作很可能还是同一个结果。"
            if scene.get("ok")
            else "引擎在处理这个请求时遇到了未预期的异常，且现场没能落盘："
                 f"{str(scene.get('reason') or '')[:120]}"
        ),
        "crashReport": str(scene.get("reportId") or ""),
        "deduped": bool(scene.get("deduped")),
    }
    return JSONResponse(status_code=500, content=body)

# CORS：只放行桌面壳与本地开发端口（允许任意 localhost 端口便于 Vite 联调）。
# 过去用 `*`，任意网页都能读取本机引擎的响应，等于把本地执行面暴露给浏览器；
# 服务化部署用 TT_ENGINE_ALLOWED_ORIGINS 显式白名单（逗号分隔，`*` 表示全部）。
DEFAULT_CORS_ORIGINS = [
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
]
DEFAULT_CORS_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"


def _cors_origins() -> tuple[list[str], str | None]:
    raw = str(os.environ.get("TT_ENGINE_ALLOWED_ORIGINS") or "").strip()
    if not raw:
        return list(DEFAULT_CORS_ORIGINS), DEFAULT_CORS_ORIGIN_REGEX
    if raw == "*":
        return ["*"], None
    return [item.strip() for item in raw.split(",") if item.strip()], None


try:
    from fastapi.middleware.cors import CORSMiddleware

    _cors_allow, _cors_regex = _cors_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_allow,
        allow_origin_regex=_cors_regex,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Authorization",
            "X-TT-Engine-Token",
            "X-TT-Terminal-Token",
            "X-TT-Client",
        ],
    )
except Exception:
    pass


# ── 服务化（可选）：Token 守卫与静态站点托管 ─────────────────
# 桌面壳启动引擎时会注入 TT_ENGINE_LOCAL_TOKEN，所有 /api 请求必须携带；
# 手工启动/浏览器直连的引擎没有任何令牌配置，行为与原来一致（仅供本机联调）。
# 服务化模式（容器/内网）设置 TT_ENGINE_TOKEN / TT_ENGINE_JWT_SECRET，
# CLI 与前置反代携带 Authorization: Bearer。

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
DEFAULT_JWT_ISSUER = "tongtian-auth"
DEFAULT_JWT_AUDIENCE = "tongtian-engine"
JWT_ALG = "HS256"
AUDIT_MAX_ENTRIES = 2000
# 附件上传上限：base64 解码后 100MB（对话附件，非整盘文件服务）
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

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


# ── 账号服务吊销实时内省（可选，缩短 exp 前的 15 分钟窗口） ──────────
# 关闭时（默认，含桌面/离线）只按令牌 exp 失效，行为与原来一致。
# 开启后每个 JWT 请求先查账号服务该 sid 是否仍有效；命中缓存则不再发起网络请求。
INTROSPECT_CACHE_TTL_SECONDS = 30
# sid → (是否有效, 过期时刻 monotonic)
_INTROSPECT_CACHE: dict[str, tuple[bool, float]] = {}


def _engine_introspect_url() -> str:
    return str(os.environ.get("TT_ENGINE_INTROSPECT_URL") or "").strip()


def _engine_introspect_token() -> str:
    return str(os.environ.get("TT_ENGINE_INTROSPECT_TOKEN") or "").strip()


def session_is_active(sub: str, sid: str) -> bool:
    """向账号服务内省该会话令牌是否仍有效（未被吊销且未过期）。

    - 未配置 TT_ENGINE_INTROSPECT_URL：内省关闭，返回 True（只按 exp 失效，离线兼容）。
    - 命中缓存：返回缓存结果。
    - 网络异常或服务不可达：fail-open 返回 True，避免账号服务抖动时整体拒绝服务；
      吊销的确定答复（active=false）才拒绝，并写入缓存。
    """
    base = _engine_introspect_url()
    if not base:
        return True
    if not sub or not sid:
        return False
    cache_key = f"{sub}:{sid}"
    now = time.monotonic()
    cached = _INTROSPECT_CACHE.get(cache_key)
    if cached is not None and cached[1] > now:
        return cached[0]
    url = f"{base.rstrip('/')}/api/v1/engine/introspect?sub={sub}&sid={sid}"
    try:
        req = urlreq.Request(url, method="GET")
        token = _engine_introspect_token()
        if token:
            req.add_header("x-tt-engine-introspect", token)
        with urlreq.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        active = bool(isinstance(body, dict) and body.get("active"))
    except Exception as exc:  # 网络/解析异常 → fail-open，不缓存
        print(f"[engine] 令牌吊销内省失败（放行）：{exc}", flush=True)
        return True
    _INTROSPECT_CACHE[cache_key] = (active, now + INTROSPECT_CACHE_TTL_SECONDS)
    return active


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


def _authenticate_engine_request(request: Request) -> tuple[dict | None, str, bool]:
    """解析服务化/桌面请求的调用主体。

    返回 (principal, error, desktop_local)：
    - 未配置任何凭据：``(None, "", False)``，桌面/回环模式，行为保持不变；
    - 桌面壳注入的本机令牌：``(None, "", True)``，只证明请求来自桌面进程，
      不引入用户主体（保持按人隔离/用量归属的既有语义）；
    - 服务化静态令牌：role=service 的静态主体；账号 JWT：按 claims 建主体。
    """
    static_token = str(os.environ.get("TT_ENGINE_TOKEN") or "").strip()
    jwt_secret = _engine_jwt_secret()
    local_token = str(os.environ.get("TT_ENGINE_LOCAL_TOKEN") or "").strip()
    if not static_token and not jwt_secret and not local_token:
        return None, "", False
    auth = request.headers.get("authorization") or ""
    got = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not got:
        got = (request.headers.get("x-tt-engine-token") or "").strip()
    if not got:
        return None, "missing_token", False
    if local_token and hmac.compare_digest(local_token, got):
        return None, "", True
    if static_token and hmac.compare_digest(static_token, got):
        return dict(STATIC_PRINCIPAL), "", False
    if not jwt_secret:
        return None, "invalid_token", False
    claims, reason = verify_engine_jwt(
        got, jwt_secret, _engine_jwt_issuer(), _engine_jwt_audience()
    )
    if not claims:
        return None, reason or "invalid_token", False
    principal = principal_from_claims(claims)
    allowed = _engine_allowed_users()
    if allowed and principal["sub"] not in allowed:
        return None, "user_not_allowed", False
    if not session_is_active(principal["sub"], principal["sid"]):
        return None, "revoked_session", False
    return principal, "", False


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


def _is_desktop_local(request: Request) -> bool:
    """请求是否由桌面壳（持本机令牌）发起。"""
    return bool(getattr(request.state, "desktop_local", False))


def _engine_data_roots() -> list[Path]:
    """允许无用户主体时读写的目录：引擎工作目录 + 引擎数据根。"""
    roots = [WORK_DIR.resolve()]
    try:
        import data_paths  # noqa: PLC0415  与产出物台账同根

        roots.append(Path(data_paths.engine_home()).resolve())
    except Exception:  # noqa: BLE001
        roots.append((Path.home() / ".tongtianwork").resolve())
    return roots


def _path_within(path: Path, roots: list[Path]) -> bool:
    """规范化后判断 path 是否位于任一 roots 内（Windows 大小写不敏感）。"""
    try:
        target = os.path.normcase(str(path.resolve()))
    except Exception:  # noqa: BLE001
        return False
    for root in roots:
        base = os.path.normcase(str(root.resolve()))
        if target == base or target.startswith(base + os.sep):
            return True
    return False


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
    principal, error, desktop_local = _authenticate_engine_request(request)
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
    request_access = ""
    if principal and principal.get("auth") == "jwt":
        auth_header = request.headers.get("authorization") or ""
        if auth_header.lower().startswith("bearer "):
            request_access = auth_header[7:].strip()
        if not request_access:
            request_access = (request.headers.get("x-tt-engine-token") or "").strip()
    access_state = _CURRENT_ACCESS_TOKEN.set(request_access)
    origin_token = us.set_usage_origin(request.headers.get(us.ORIGIN_HEADER))
    request.state.principal = principal
    request.state.desktop_local = desktop_local
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
        _CURRENT_ACCESS_TOKEN.reset(access_state)
        us.reset_usage_origin(origin_token)


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
    # 服务化按人隔离：跨用户访问他人任务直接拒绝（桌面/无令牌放行）
    forbid = _stamp_or_check_session(session)
    if forbid:
        return JSONResponse(
            {"ok": False, "taskId": task_id, "code": forbid["code"],
             "message": forbid["message"]},
            status_code=forbid["status"],
        )
    # 配额硬停：超月度 token 或并发上限时返回明确错误码，不进入执行
    over_quota = _quota_error(current_principal())
    if over_quota:
        return JSONResponse(
            {"ok": False, "taskId": task_id, "code": over_quota["code"],
             "message": over_quota["message"]},
            status_code=over_quota["status"],
        )
    if goal:
        # 新一轮提交：旧检查点作废（同一 task 重试也不应恢复上一次的失败现场）
        session.checkpoint = ckpt.TaskCheckpoint(task_id)
        ckpt.delete_checkpoint(task_id)
        session.cancelled = False
        session.cancel_reason = ""
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
    # 文件访问档位（对话输入框选择）：与 workDir 一起构成本轮的真实边界。
    # 桌面端把「每会话选择 + 全局默认」解析成一个值随请求下发；未知值直接拒绝，
    # 不静默回落到最保守档——用户以为选了完全访问、实际却被限制住更难排查。
    perm_raw = payload.get("filePermission")
    if perm_raw is not None and str(perm_raw).strip():
        perm_value = str(perm_raw).strip().lower()
        if perm_value not in sr.FILE_PERMISSION_MODES:
            return JSONResponse(
                {
                    "ok": False,
                    "code": "FILE_PERMISSION_INVALID",
                    "message": (
                        f"未知文件访问档位 {perm_raw}"
                        f"（可用：{'/'.join(sr.FILE_PERMISSION_MODES)}）"
                    ),
                },
                status_code=400,
            )
        session.file_permission = perm_value
        # 顶层 runtime 键：Rust 装配的 runtime 里已有同键时以本次请求为准
        # （approve 重跑不带该字段时，沿用这里盖进去的值）
        rt = session.runtime if isinstance(session.runtime, dict) else {}
        session.runtime = {**rt, "filePermission": perm_value}
    elif not session.file_permission:
        session.file_permission = str(
            (session.runtime or {}).get("filePermission") or ""
        ).strip().lower() or None
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
        guard = _runtime_guard_error(runtime_raw)
        if guard:
            return JSONResponse(
                {"ok": False, "code": "RUNTIME_FORBIDDEN", "message": guard},
                status_code=403,
            )
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

    # 两阶段协议（核心闭环补齐）：stage=plan 仅生成计划待审阅；
    # 缺省保持旧契约全流程执行（向后兼容/Mock 测试触发器）
    if payload.get("stage") == "plan":
        return StreamingResponse(
            _plan_stream(session, task_id, goal),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    return StreamingResponse(
        _run_execution(session, task_id, goal, replay_from, model_binding, session.work_dir, history, model_config),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
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

    # 服务化按人隔离：审阅/执行/续跑前校验归属
    forbid = _stamp_or_check_session(session)
    if forbid:
        return JSONResponse(
            {"ok": False, "taskId": task_id, "code": forbid["code"],
             "message": forbid["message"]},
            status_code=forbid["status"],
        )
    # approve/resume 会真正执行模型调用，按配额硬停
    if action in ("approve", "resume"):
        over_quota = _quota_error(current_principal())
        if over_quota:
            return JSONResponse(
                {"ok": False, "taskId": task_id, "code": over_quota["code"],
                 "message": over_quota["message"]},
                status_code=over_quota["status"],
            )

    if action == "resume":
        # 断点续跑：从最近一次模型调用前的检查点继续同一任务（taskId 不变）
        # Rust 每次 resume 都重新下发模型配置与 runtime（磁盘里不保存密钥与网关令牌）
        runtime_error = _apply_resume_overrides(session, payload)
        if runtime_error:
            return JSONResponse(
                {"ok": False, "taskId": task_id, "code": "RUNTIME_FORBIDDEN", "message": runtime_error},
                status_code=403,
            )
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
        session.cancel_reason = ""
        return StreamingResponse(
            _run_execution(
                session, task_id, session.goal or "", [],
                session.model_binding, session.work_dir, None,
                session.model_config, resume=True,
            ),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    if action == "approve":
        return StreamingResponse(
            _run_execution(session, task_id, session.goal or "", [], session.model_binding, session.work_dir, None, session.model_config),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    if action == "reject":
        async def reject_stream():
            sse = _sse_formatter(session, task_id)
            yield sse("status_change", {
                "taskId": task_id,
                "status": "done",
                "statusReasonCode": "REJECTED",
            })
            yield sse("done", {})
        return StreamingResponse(reject_stream(), media_type="text/event-stream", headers=SSE_HEADERS)

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
            sse = _sse_formatter(session, task_id)
            yield sse("plan", {"plan_id": f"plan-{task_id}", "steps": steps})
            yield sse("status_change", {"taskId": task_id, "status": "pending_review"})
            yield sse("done", {})
        return StreamingResponse(modify_stream(), media_type="text/event-stream", headers=SSE_HEADERS)

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
    principal = current_principal()
    for task_id in task_ids:
        session = _TASK_SESSIONS.get(task_id)
        if session is not None:
            if not _principal_can_access(principal, session.owner_sub):
                continue
            info = _checkpoint_public(session)
        else:
            data = ckpt.load_checkpoint(task_id)
            if data and not _principal_can_access(
                principal, _checkpoint_owner(data)
            ):
                continue
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
    forbid = _stamp_or_check_session(session)
    if forbid:
        return JSONResponse(
            {"ok": False, "taskId": task_id, "code": forbid["code"],
             "message": forbid["message"], "events": [], "windowExpired": False},
            status_code=forbid["status"],
        )
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
    if session is None:
        return JSONResponse({"taskId": task_id, "cancelled": False, "reason": "TASK_NOT_FOUND"})
    # 服务化按人隔离：先判归属，再暴露任务是否可取消
    forbid = _stamp_or_check_session(session)
    if forbid:
        return JSONResponse(
            {"ok": False, "taskId": task_id, "code": forbid["code"],
             "message": forbid["message"], "cancelled": False},
            status_code=forbid["status"],
        )
    if session.agent_task is None:
        return JSONResponse({"taskId": task_id, "cancelled": False, "reason": "TASK_NOT_FOUND"})
    if session.agent_task.done():
        return JSONResponse({"taskId": task_id, "cancelled": False, "reason": "TASK_ALREADY_FINISHED"})
    session.cancelled = True
    # 成因码由调用方带（Rust 的预算拦停带 BUDGET_TASK_EXCEEDED，界面点「停止」不带）；
    # 不在码表里的值不参与判定，按用户手动取消记账——取消本身绝不因一个说明字段而失败。
    reason = str(payload.get("reason") or "").strip().upper()
    session.cancel_reason = reason if reason in CANCEL_REASONS else ""
    session.agent_task.cancel()
    return JSONResponse({
        "taskId": task_id,
        "cancelled": True,
        "statusReasonCode": session.cancel_reason or "USER_CANCELLED",
    })


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
    # 先按 base64 长度估算，避免把超大请求整体解码进内存
    if not isinstance(data_b64, str) or len(data_b64) > MAX_UPLOAD_BYTES * 4 // 3 + 8:
        return JSONResponse(
            {"ok": False, "code": "FILE_TOO_LARGE", "message": f"附件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限"},
            status_code=413,
        )
    try:
        raw = base64.b64decode(data_b64)
    except Exception:
        return JSONResponse(
            {"ok": False, "code": "VALIDATION_ERROR", "message": "base64 内容无效"}, status_code=400
        )
    if len(raw) > MAX_UPLOAD_BYTES:
        return JSONResponse(
            {"ok": False, "code": "FILE_TOO_LARGE", "message": f"附件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限"},
            status_code=413,
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
async def files_content(request: Request, path: str = ""):
    """产物预览：返回文件文本内容（限 2MB，文本类文件），供前端点击产物打开预览"""
    p = Path(path)
    if not path or not p.exists() or not p.is_file():
        return JSONResponse({"ok": False, "message": "文件不存在或已移动"}, status_code=404)
    # 非桌面壳（服务化/匿名直连）只允许读引擎工作目录与数据根，
    # 避免通过本地端口读取任意本机文件（如 ~/.ssh、浏览器配置）。
    if not _is_desktop_local(request) and not _path_within(p, _engine_data_roots()):
        return JSONResponse(
            {"ok": False, "code": "FILE_ACCESS_DENIED", "message": "该路径不在引擎数据目录内，拒绝读取"},
            status_code=403,
        )
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
    """意识目录：数据根由 `data_paths` 统一解析（与 Rust `awareness::dir()` 同规则），
    两条通道因此读写同一份文件"""
    return dp.awareness_main_dir()


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


# ── 规则事实抽取（D-35 第二阶段） ──────────────────────────────
#
# 不额外调用模型：从本轮最终回复里抽少量可独立理解的陈述句，带
# subject/predicate/object 与置信度。同 (subject, predicate) 的新取值由 Rust
# 做冲突软退休；抽取失败只是少记一条，不影响任务结果。

_FACT_CONNECTORS = ("采用", "使用", "位于", "属于", "需要", "支持", "决定", "约定", "是", "为")
_FACT_SUBJECT_BLOCK = (
    "本轮", "任务", "目标", "结论", "结果", "以上", "如下", "下面", "我们", "你们", "他们",
    "这个", "那个", "这里", "那里", "本次", "当前",
)
_FACT_SUBJECT_PREFIX = ("这", "那", "它", "他", "她", "我", "你", "本")


def _fact_sentence(raw) -> str:
    """去 Markdown 标记并压成单行；空串/过短/过长会在调用处丢弃"""
    text = " ".join(str(raw or "").split()).strip()
    return text.lstrip("#>*-•0123456789.、)） ").strip()


def _split_fact_verb(text: str):
    """找最早的谓词连接词，返回 (主语, 谓词, 宾语)；「X 的 Y 是 Z」拆成 X / Y是 / Z"""
    best = None
    for conn in _FACT_CONNECTORS:
        idx = text.find(conn)
        if idx <= 0:
            continue
        if best is None or idx < best[0]:
            best = (idx, conn)
    if best is None:
        return None
    idx, conn = best
    left = text[:idx].strip()
    right = text[idx + len(conn):].strip()
    if not left or not right:
        return None
    if "的" in left:
        subject, attr = left.rsplit("的", 1)
        subject, attr = subject.strip(), attr.strip()
        if len(subject) >= 2 and 1 <= len(attr) <= 16:
            return subject, attr + conn, right
    return left, conn, right


def _fact_subject_ok(subject: str) -> bool:
    subject = subject.strip()
    if not (2 <= len(subject) <= 40):
        return False
    if any(subject.startswith(prefix) for prefix in _FACT_SUBJECT_PREFIX):
        return False
    if any(word in subject for word in _FACT_SUBJECT_BLOCK):
        return False
    if re.fullmatch(r"[\d\s./:_-]+", subject):
        return False
    if subject.startswith(("http", "www", "file:")):
        return False
    return True


def _fact_object_ok(obj: str) -> bool:
    obj = obj.strip()
    return 2 <= len(obj) <= 120 and not obj.startswith(("以下", "如下"))


def _fact_kind(sentence: str) -> str:
    if any(word in sentence for word in ("偏好", "喜欢", "习惯", "倾向", "希望", "不希望")):
        return "preference"
    if any(word in sentence for word in ("决定", "确定", "选定", "约定", "统一")):
        return "decision"
    return "fact"


def _extract_fact_candidates(text, max_facts: int = 4) -> list[dict]:
    """从最终回复抽事实候选；只做保守的句式匹配，宁可少记也不臆造。

    显式「主体：谓词 取值」置信度 0.65，普通「主体谓词取值」0.55；
    凭据过滤在调用方做，便于把「跳过几条」如实播报。
    """
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for chunk in re.split(r"[。；;！!？?\n]+", str(text or "")):
        sentence = _fact_sentence(chunk)
        if not (6 <= len(sentence) <= 160):
            continue
        if "```" in sentence or sentence.startswith(("$", "Traceback", "Error:", "错误：")):
            continue
        parsed = None
        colon = re.match(
            r"^(?P<s>[^：:，,。；;]{2,40})[：:]\s*(?P<rest>.{2,140})$", sentence
        )
        if colon:
            subject = colon.group("s").strip()
            verb = _split_fact_verb(colon.group("rest").strip())
            if verb and _fact_subject_ok(subject) and _fact_object_ok(verb[2]):
                parsed = (subject, verb[1], verb[2], 0.65)
        if parsed is None:
            verb = _split_fact_verb(sentence)
            if verb and _fact_subject_ok(verb[0]) and _fact_object_ok(verb[2]):
                parsed = (verb[0], verb[1], verb[2], 0.55)
        if parsed is None:
            continue
        subject, predicate, obj, confidence = parsed
        key = (subject.lower(), predicate.lower(), obj.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "content": sentence,
            "kind": _fact_kind(sentence),
            "source": "auto",
            "subject": subject,
            "predicate": predicate,
            "object": obj,
            "confidence": confidence,
        })
        if len(out) >= max_facts:
            break
    return out


# ── 模型事实抽取（D-35 第三阶段，可选） ───────────────────────
#
# 规则抽取覆盖常规陈述；用户打开「模型辅助抽取」后，任务收尾再跑一次结构化
# 抽取，补上规则漏掉的隐含事实。默认关闭：多一次模型调用意味着额外成本与延迟，
# 且本轮结论会再次发给模型（隐私模式与脱敏档位照常生效）。任何失败都只降级，
# 不影响任务结果，也不阻断规则抽取。

MODEL_MEMORY_MAX_FACTS = 4
MODEL_MEMORY_PROMPT = (
    "你是长期记忆抽取器。阅读「任务目标」和「本轮结论」，只抽取可长期复用、"
    "脱离当前对话也能独立理解的事实（客户偏好、项目约定、关键决策、稳定配置）。\n"
    "规则：\n"
    "1. 不记录一次性步骤、临时路径、运行日志、报错堆栈和本轮产物清单；\n"
    "2. 不记录任何凭据、密钥、令牌、密码、Cookie；\n"
    "3. 每条一句话，主语明确，不使用「它 / 这个 / 上面」等指代；\n"
    f"4. 最多 {MODEL_MEMORY_MAX_FACTS} 条，宁缺毋滥；\n"
    "5. kind 只能是 task / fact / preference / decision；\n"
    "6. subject / predicate / object 尽量填：主体、属性、取值（可为空串）。\n"
    "只输出 JSON 数组，不要解释、不要 Markdown 代码块。字段：\n"
    '[{"content":"...","kind":"fact","subject":"...","predicate":"...",'
    '"object":"...","confidence":0.7}]'
)


def _model_memory_extract_enabled(runtime: dict | None, privacy: bool = False) -> bool:
    """模型抽取只在桌面记忆库、自动记忆可用且用户显式打开时执行。"""
    if privacy:
        return False
    enabled, _reason = _auto_memory_decision(runtime, privacy=False)
    if not enabled:
        return False
    block = (runtime or {}).get("memory")
    return isinstance(block, dict) and bool(block.get("modelExtract"))


def _parse_model_facts(raw, max_facts: int = MODEL_MEMORY_MAX_FACTS) -> list[dict]:
    """解析模型返回的 JSON 数组；只保留字段完整的条目，解析失败返回空列表。"""
    text = str(raw or "")
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in data[: max_facts * 2]:
        if not isinstance(item, dict):
            continue
        content = _one_line(item.get("content") or "", 300)
        if len(content) < 6:
            continue
        key = content.lower()
        if key in seen:
            continue
        seen.add(key)
        kind = str(item.get("kind") or "fact").strip().lower()
        if kind not in ("task", "fact", "preference", "decision"):
            kind = "fact"
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.6
        out.append({
            "content": content,
            "kind": kind,
            "source": "auto",
            "subject": _one_line(item.get("subject") or "", 80),
            "predicate": _one_line(item.get("predicate") or "", 80),
            "object": _one_line(item.get("object") or "", 300),
            "confidence": max(0.0, min(1.0, confidence)),
        })
        if len(out) >= max_facts:
            break
    return out


def _merge_memory_candidates(
    base: list[dict], extra: list[dict], cap: int = 8
) -> tuple[list[dict], int, int]:
    """合并规则与模型候选：按内容与三元组去重、过滤凭据；返回 (列表, 新增, 跳过)。"""
    out = list(base or [])
    seen = {str(c.get("content") or "").strip().lower() for c in out}
    triples = set()
    for c in out:
        triple = (
            str(c.get("subject") or "").strip().lower(),
            str(c.get("predicate") or "").strip().lower(),
            str(c.get("object") or "").strip().lower(),
        )
        if all(triple):
            triples.add(triple)
    added = 0
    skipped = 0
    for candidate in extra or []:
        content = _one_line(candidate.get("content") or "", 300)
        if not content:
            continue
        key = content.lower()
        if key in seen:
            continue
        triple = (
            str(candidate.get("subject") or "").strip().lower(),
            str(candidate.get("predicate") or "").strip().lower(),
            str(candidate.get("object") or "").strip().lower(),
        )
        if all(triple) and triple in triples:
            continue
        if _looks_like_secret(content):
            skipped += 1
            continue
        if len(out) >= cap:
            break
        merged = dict(candidate)
        merged["content"] = content
        out.append(merged)
        seen.add(key)
        if all(triple):
            triples.add(triple)
        added += 1
    return out, added, skipped


async def _model_fact_candidates(
    goal: str,
    final_text: str,
    *,
    model_binding: str | None = None,
    model_config: dict | None = None,
    pii_mode: str = "off",
    on_usage=None,
    timeout_s: float = 20.0,
) -> tuple[list[dict], str]:
    """一次非流式调用抽取事实；返回 (候选, 失败说明)。失败不抛出、不回滚任务。"""
    text = str(final_text or "").strip()
    if len(text) < 40:
        return [], "结论过短，未运行模型抽取"
    try:
        model, formatter = build_model_and_formatter(
            model_binding, model_config, stream=False,
            on_usage=on_usage, pii_mode=pii_mode,
        )
        from agentscope.message import Msg
        prompt = (
            f"{MODEL_MEMORY_PROMPT}\n\n【任务目标】\n{_one_line(goal, 300)}"
            f"\n\n【本轮结论】\n{text[:3000]}"
        )
        formatted = await formatter.format(
            [Msg(name="user", content=prompt, role="user")]
        )
        resp = await asyncio.wait_for(model(formatted), timeout=timeout_s)
        raw = _text_of(getattr(resp, "content", ""))
    except asyncio.TimeoutError:
        return [], f"模型抽取超时（>{int(timeout_s)}s），已保留规则抽取"
    except Exception as exc:
        return [], f"模型抽取未生效：{_one_line(exc, 120)}（已保留规则抽取）"
    facts = _parse_model_facts(raw)
    if not facts:
        return [], "模型抽取没有产出可用事实（已保留规则抽取）"
    return facts, ""


def _auto_memory_candidates(runtime: dict | None, goal: str, final_text: str,
                            artifacts: list, privacy: bool = False) -> tuple[list[dict], str]:
    """桌面记忆库模式：任务摘要 + 规则事实候选，去重与落库由 Rust 完成。

    返回 (候选列表, 可见说明)。写入结果由 Rust 在落库后统一播报。
    """
    enabled, reason = _auto_memory_decision(runtime, privacy=privacy)
    if not enabled:
        return [], reason
    out: list[dict] = []
    content = _auto_memory_content(goal, final_text, artifacts)
    summary_skipped = False
    if content:
        if _looks_like_secret(content):
            # 目标或结论里带凭据时连摘要一起放弃，但要把「为什么这一轮没记住」说清楚
            summary_skipped = True
        else:
            out.append({
                "content": content, "kind": "task", "source": "auto", "confidence": 0.5,
            })
    skipped = 0
    for fact in _extract_fact_candidates(final_text):
        if _looks_like_secret(fact["content"]):
            skipped += 1
            continue
        if any(candidate["content"] == fact["content"] for candidate in out):
            continue
        out.append(fact)
    notes: list[str] = []
    if summary_skipped:
        notes.append("本轮摘要疑似包含凭据，已跳过自动记忆（避免密钥落盘）")
    if skipped:
        notes.append(f"{skipped} 条事实疑似包含凭据，已跳过自动记忆（避免密钥落盘）")
    return out, "；".join(notes)


def _auto_memory_candidate(runtime: dict | None, goal: str, final_text: str,
                           artifacts: list, privacy: bool = False) -> tuple[dict | None, str]:
    """兼容旧调用：只取第一条候选（任务摘要）。新链路用 `_auto_memory_candidates`。"""
    candidates, note = _auto_memory_candidates(
        runtime, goal, final_text, artifacts, privacy=privacy
    )
    return (candidates[0] if candidates else None), note


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
    """模型条目文件：与意识目录同一数据根（`data_paths`），测试可整体隔离到临时目录"""
    return dp.models_config_file()


# 桌面端拉起引擎时下发属主标记：模型配置的唯一权威是桌面库 `model_configs`，
# 引擎此时只读不写，避免在引擎数据根下长出第二份 models.json。
MODEL_STORE_OWNER_ENV = "TT_ENGINE_MODEL_STORE"


def _model_store_owned_by_desktop() -> bool:
    raw = str(os.environ.get(MODEL_STORE_OWNER_ENV) or "").strip().lower()
    return raw in ("desktop", "1", "true", "yes", "on")


def _model_store_desktop_owned_response() -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "code": "MODEL_STORE_DESKTOP_OWNED",
            "message": "桌面端模型配置以本机数据库为准，请在模型中心修改；引擎 models.json 只服务浏览器直连调试",
        },
        status_code=409,
    )


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

    桌面通道另有 Rust `model_usage` 表（按 usage 事件持久化）；本接口读引擎台账，
    浏览器直连、IM、CLI、定时任务的用量只有这里有。台账已落盘，重启后重放恢复，
    响应里的 `persist` 如实披露落盘文件、待合并增量与最近一次写入失败。
    JWT 非管理员只能看到自己的用量。"""
    p = period if period in ("daily", "monthly", "total") else "total"
    subject = _usage_scope(sub)
    return JSONResponse({
        "ok": True,
        "period": p,
        "subject": subject,
        "rows": USAGE_LEDGER.aggregate(p, subject),
        "persist": USAGE_LEDGER.persist_status(),
    })


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
            "persist": USAGE_LEDGER.persist_status(),
        }
    )


@app.get("/api/usage/export")
async def usage_export_get(period: str = "total", sub: str = ""):
    """按「用户 × 模型 × 日 × 来源」导出台账明细，供上报服务端或运维对账使用。

    为什么保留来源维度：桌面 `model_usage` 与引擎台账会覆盖同一批调用，合并时必须只
    排除明确来自桌面通道（`desktop`）的那部分，其余来源桌面根本没记，必须原样补入；
    未带来源标记的调用记为 `unknown`（或由 `TT_ENGINE_USAGE_ORIGIN_DEFAULT` 声明默认来源），
    单列披露，不做静默合并。JWT 非管理员只能导出自己的用量。
    """
    p = period if period in ("daily", "monthly", "total") else "total"
    subject = _usage_scope(sub)
    rows = USAGE_LEDGER.daily_rows(p, subject)
    unattributed = sum(int(row["tokensUsed"]) for row in rows if row["origin"] == us.ORIGIN_UNKNOWN)
    return JSONResponse({
        "ok": True,
        "period": p,
        "subject": subject,
        "schemaVersion": us.LEDGER_SCHEMA_VERSION,
        "rows": rows,
        "unattributedTokens": unattributed,
        "persist": USAGE_LEDGER.persist_status(),
    })


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
    if _model_store_owned_by_desktop():
        return _model_store_desktop_owned_response()
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
    if _model_store_owned_by_desktop():
        return _model_store_desktop_owned_response()
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
    if _model_store_owned_by_desktop():
        return _model_store_desktop_owned_response()
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
    if _model_store_owned_by_desktop():
        return _model_store_desktop_owned_response()
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
    if _model_store_owned_by_desktop():
        return _model_store_desktop_owned_response()
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
    # 服务化用户不允许在服务器桌面上弹窗口；匿名直连只允许打开引擎数据目录
    if current_principal() is not None:
        return JSONResponse(
            {"ok": False, "code": "FILE_OPEN_DENIED", "message": "服务化模式不允许打开服务器上的文件夹"},
            status_code=403,
        )
    raw = str(payload.get("path") or "").strip() or str(_awareness_home())
    p = Path(raw).expanduser()
    if not _is_desktop_local(request) and not _path_within(p, _engine_data_roots()):
        return JSONResponse(
            {"ok": False, "code": "FILE_ACCESS_DENIED", "message": "该路径不在引擎数据目录内，拒绝打开"},
            status_code=403,
        )
    if p.exists() and not p.is_dir():
        return JSONResponse(
            {"ok": False, "code": "VALIDATION_ERROR", "message": "只能打开文件夹，不能直接打开文件"},
            status_code=400,
        )
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


@app.post("/api/im/{kind}/login/start")
async def im_login_start(kind: str):
    """扫码登录第一步（个人微信）：生成二维码。二维码内容原样给前端渲染，
    登录令牌只在确认那一帧由引擎落盘，绝不经过这条响应。"""
    try:
        payload = await im_channels.runtime_of(kind).login_start()
    except Exception as exc:
        return _im_error(exc)
    return JSONResponse({"ok": True, **payload})


@app.post("/api/im/{kind}/login/poll")
async def im_login_poll(kind: str, request: Request):
    """扫码登录第二步：查一次状态；确认时凭据落盘并起连接（响应里没有 token）"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "code": "VALIDATION_ERROR", "message": "无效 JSON"}, status_code=400)
    ticket = str(payload.get("ticket") or "").strip()
    if not ticket:
        return JSONResponse(
            {"ok": False, "code": "IM_LOGIN_TICKET_MISSING", "message": "未提供二维码票据"},
            status_code=400,
        )
    try:
        result = await im_channels.runtime_of(kind).login_poll(ticket, str(payload.get("verifyCode") or ""))
    except Exception as exc:
        return _im_error(exc)
    return JSONResponse({"ok": True, **result})


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


# ── 工单执行体端点（D-48.7）──────────────────────────────────────
# 薄包装：读请求体 → 调 ticket_worker_service → 原样回它给的字典。业务口径（槽位、保序、
# 幂等、已读落账、看门狗、重启补查、耐久收件箱）都在那一层和它的 190 项自测里；这里再写
# 一遍就是两份真相，改一处漏一处。
#
# 本机门槛（`_ticket_desktop_only`）：worker 回合会在工单目录里**起真实进程**（run_shell）。
# 桌面壳发的唤醒是用户在自己机器上点的；服务化模式下带着账号 JWT / 静态服务令牌的远程调用方
# 能投一条唤醒，等于把远程执行开了个口子。所以写面要么来自桌面壳（本机令牌），要么引擎根本没
# 配服务凭据（未配置时 `_authenticate_engine_request` 回 (None, "", False)，那正是桌面/本机开发
# 形态）。查询面（status/state/history/inbox/outbox/plan）不设这道门槛——它们不起进程。
DEFAULT_GATEWAY_TOKEN_WAIT_SECONDS = 15.0
TICKET_OUTBOX_LIMIT_MAX = 64
TICKET_HISTORY_LIMIT_MAX = 500

#: 工单回合的用量归属：后台线程没有请求上下文，`current_principal()` 在那里是空的。
#: 记「最近一次带着登录身份调用工单写面的人」，比静默落到 local 更贴近事实；桌面版没有
#: 身份时仍落 local（与 /api/chat 的既有口径一致）。
_TICKET_USAGE_OWNER: dict[str, str] = {}


def _ticket_error(code: str, message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, "message": message}, status_code=status)


async def _ticket_payload(request: Request) -> tuple[dict[str, Any], JSONResponse | None]:
    """取 JSON 请求体：无效 JSON / 顶层不是对象一律 400，不猜成空对象继续往下投。"""
    try:
        payload = await request.json()
    except Exception:
        return {}, _ticket_error("VALIDATION_ERROR", "无效 JSON")
    if not isinstance(payload, dict):
        return {}, _ticket_error("VALIDATION_ERROR", f"请求体顶层要是对象（是 {type(payload).__name__}）")
    return payload, None


def _ticket_desktop_only(request: Request) -> JSONResponse | None:
    principal = current_principal()
    if _is_desktop_local(request) or principal is None:
        return None
    return JSONResponse(
        {
            "ok": False,
            "code": "TICKET_WORKER_DESKTOP_ONLY",
            "message": (
                "工单执行体会在工单目录里起真实进程：唤醒、补查、改装配这类写操作只接受桌面壳"
                "（本机令牌）发起；当前请求带着远程身份，请改用查询面"
            ),
        },
        status_code=403,
    )


def _ticket_required(payload: dict[str, Any], *names: str) -> JSONResponse | None:
    """必填字段只做「有没有」的裁决，语义（origin 合法吗、这个工单能不能投）留给服务层。"""
    missing = [name for name in names if not str(payload.get(name) or "").strip()]
    if missing:
        return _ticket_error(
            "TICKET_FIELD_MISSING",
            f"缺少必填字段：{'、'.join(missing)}（工单端点不猜默认值，猜出来的 ticketId 会投到别人那一单）",
        )
    return None


def _ticket_int(payload: dict[str, Any], name: str, default: int, *, low: int, high: int) -> tuple[int, JSONResponse | None]:
    raw = payload.get(name)
    if raw is None or str(raw).strip() == "":
        return default, None
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default, _ticket_error("TICKET_FIELD_INVALID", f"{name} 要是一个整数（收到 {raw!r}）")
    if value < low or value > high:
        return default, _ticket_error("TICKET_FIELD_INVALID", f"{name} 超出可用范围 {low}–{high}（收到 {value}）")
    return value, None


#: 工作区准备规格**不经 HTTP 暴露**（缺陷 D-48.8）：`repo` 类是「在桌面机器上按某个仓建
#: worktree」，`command` 类更直接就是「起一条任意命令」。把它交给请求体，等于让远程主体挑
#: 目录、挑进程——写面那道「只对本机」的闸门挡的是消息内容，不是「谁来指定执行体在哪份代码上干活」。
#: 出现即拒，不静默忽略：静默会让配的人以为自己的 worktree 已经站上了，而执行体其实只在空目录里干活。
TICKET_PREPARE_BODY_KEYS = ("prepare", "prepareSpec", "prepareFn", "workspacePrepare", "worktree")


def _ticket_prepare_refused(payload: dict[str, Any]) -> JSONResponse | None:
    hit = [name for name in TICKET_PREPARE_BODY_KEYS if name in payload]
    if not hit:
        return None
    return _ticket_error(
        "TICKET_FIELD_INVALID",
        f"{'、'.join(hit)} 不经请求体设置：工单工作区的准备规格只认进程内 configure(prepare=…) "
        f"与环境变量 {tws.PREPARE_ENV_VAR}（本机桌面壳自己设）。准备动作会在桌面机器上建目录、"
        "跑 git 甚至跑一条任意命令，把它交给 HTTP 等于让远程主体挑目录。"
        "这条请求没有改装配，也没有投递任何唤醒。",
    )


def _ticket_remember_owner() -> None:
    principal = current_principal() or {}
    sub = str(principal.get("sub") or "").strip()
    if not sub:
        return
    _TICKET_USAGE_OWNER["sub"] = sub
    _TICKET_USAGE_OWNER["org"] = str(principal.get("org") or "").strip()


def _ticket_usage_hook(prices: dict | None) -> Callable[[dict[str, Any]], None]:
    """通道 → 用量台账：工单回合的 tokens 与耗时记进同一份账（origin=ticket-worker）。

    不接这一条，「今天用了多少」会把执行体那一整路算成零——模型真花了 token，只是没人记账。
    台账写失败只出声不抛：用量记不上不该让已经花掉的调用变成一次失败回合。"""

    def on_usage(usage: dict[str, Any]) -> None:
        try:
            prompt = int(usage.get("promptTokens") or 0)
            completion = int(usage.get("completionTokens") or 0)
            cost_micros, currency = cost_micros_of(prices, prompt, completion)
            latency_raw = usage.get("latencyMs")
            USAGE_LEDGER.add(
                str(usage.get("model") or "unknown"),
                prompt,
                completion,
                latency_ms=int(latency_raw) if latency_raw is not None else None,
                cost_micros=cost_micros,
                currency=currency,
                priced=cost_micros is not None,
                sub=_TICKET_USAGE_OWNER.get("sub"),
                org=_TICKET_USAGE_OWNER.get("org"),
                origin=tmc.USAGE_ORIGIN,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[engine][tickets] 工单回合用量没记上（不影响已完成的调用）：{type(exc).__name__}: {exc}", flush=True)

    return on_usage


def _ticket_gateway_token_provider(model_config: Any) -> Callable[[], str] | None:
    """网关模式的换票件（每轮现取短期令牌）；非网关模式返回 None，交给通道按配置自己判定。

    `GatewayTokenProvider.get()` 是协程（换票走引擎的 HTTP 会话），worker 回合是同步线程，
    所以这里跨循环回投。拿不到循环（没走 lifespan 启动）或换票超时都**如实抛**：吞成空字符串
    会让通道报「换取结果为空」，把「接线方式不对」说成「你登录失效了」。"""
    try:
        gateway = _gateway_of(model_config if isinstance(model_config, dict) else None)
    except RuntimeError:
        # 网关配置不完整：resolve_model_config 会给出 mode=none 与同一句理由，这里不重复报
        return None
    if gateway is None:
        return None
    provider = _gateway_provider(gateway)

    def provide() -> str:
        loop = _TICKET_LOOP
        if loop is None:
            raise RuntimeError("网关模式换票要有引擎事件循环：这套装配不是在 lifespan 里建的，无法跨循环换短期令牌")
        future = asyncio.run_coroutine_threadsafe(provider.get(), loop)
        try:
            return str(future.result(DEFAULT_GATEWAY_TOKEN_WAIT_SECONDS) or "")
        except (TimeoutError, asyncio.TimeoutError) as exc:
            future.cancel()
            raise RuntimeError(f"网关令牌 {DEFAULT_GATEWAY_TOKEN_WAIT_SECONDS} 秒没换到，本轮不起回合") from exc

    return provide


async def _ticket_call(fn: Callable[[], Any]) -> JSONResponse:
    """把一次服务调用放到工作线程并包成响应。

    这一层里 `run_outbox` 会同步等通道回话（最长 30 秒一条）、`submit` 要建目录并落耐久箱，
    直接在事件循环里跑会把整个引擎按停；异常一律回中文理由而不是 500 页面——失败也说明白了，
    比让调用方猜「到底投进去没有」强。"""
    try:
        result = await asyncio.to_thread(fn)
    except Exception as exc:  # noqa: BLE001
        return _ticket_error("TICKET_WORKER_ERROR", f"工单执行体调用失败：{type(exc).__name__}: {exc}", 500)
    if not isinstance(result, dict):
        return _ticket_error("TICKET_WORKER_ERROR", f"执行体返回的不是字典（是 {type(result).__name__}）", 500)
    try:
        body = tws.json_ready(result)
    except (TypeError, ValueError) as exc:
        # 回不出去的字典在这里撞上，比在 JSONResponse 里撞上便宜：那时盘上事实已经落了
        return _ticket_error("TICKET_RESULT_UNSERIALIZABLE", f"执行体结果无法作为 JSON 返回：{type(exc).__name__}: {exc}", 500)
    return JSONResponse(body)


@app.get("/api/tickets/status")
async def tickets_status():
    """服务面板：模型模式 / 槽位 / 在飞回合 / 队列与收件箱条数 / 通道是否接线（不含密钥）。"""
    return await _ticket_call(lambda: _ticket_runtime().status())


@app.post("/api/tickets/config")
async def tickets_config(request: Request):
    """配置这套装配：模型（推给活通道，不重建）、并发口径、提示词目标、默认回话会话。

    并发口径（槽位 / 并行回合数）与数据根是构造期事实，改了会重建并当场说明为什么；模型改了
    只推。换票件与用量回调由引擎在这里现造——它们是「跑在引擎里」才有的东西，装配层不该认识
    `gateway_token` 和 `UsageLedger`。登录态换了要重新推一次（换票件按 access token 摘要缓存）。

    这里**没有**准备规格（在哪个仓起 worktree / 跑哪条准备命令）的口：那会在桌面机器上建目录、
    跑 git 甚至跑一条任意命令，只认进程内 `configure(prepare=…)` 与环境变量
    `TT_TICKET_WORKER_PREPARE`；请求体里出现 `prepare` / `prepareFn` 之类字段当场 400，不静默忽略。
    """
    denied = _ticket_desktop_only(request)
    if denied is not None:
        return denied
    payload, err = await _ticket_payload(request)
    if err is not None:
        return err
    refused = _ticket_prepare_refused(payload)
    if refused is not None:
        return refused
    kwargs: dict[str, Any] = {}
    if "modelConfig" in payload:
        model_config = payload.get("modelConfig")
        if model_config is not None and not isinstance(model_config, dict):
            return _ticket_error("TICKET_FIELD_INVALID", "modelConfig 要是一个对象（或省略表示不改模型）")
        kwargs["model_config"] = model_config if isinstance(model_config, dict) else {}
        kwargs["token_provider"] = _ticket_gateway_token_provider(model_config)
        kwargs["on_usage"] = _ticket_usage_hook(_prices_of(model_config))
    for key, json_key, bounds in (
        ("agent_slots", "agentSlots", (1, 16)),
        ("human_extra", "humanExtraSlots", (0, 8)),
        ("max_parallel_turns", "maxParallelTurns", (1, 16)),
        ("max_followup_turns", "maxFollowupTurns", (1, 64)),
        ("max_tool_rounds", "maxToolRounds", (1, 32)),
    ):
        if json_key in payload:
            value, verr = _ticket_int(payload, json_key, 0, low=bounds[0], high=bounds[1])
            if verr is not None:
                return verr
            kwargs[key] = value
    for key, names in (("goal", ("goal",)), ("default_session_key", ("defaultSessionKey", "default_session_key"))):
        for name in names:
            if name in payload:
                kwargs[key] = str(payload.get(name) or "")
    if not kwargs:
        return _ticket_error(
            "TICKET_CONFIG_EMPTY",
            "没有要改的项：modelConfig / agentSlots / goal / defaultSessionKey 至少给一个（空请求不该悄悄重建装配）",
        )
    _ticket_remember_owner()
    return await _ticket_call(lambda: tws.configure(**kwargs))


@app.post("/api/tickets/wakes")
async def tickets_wake(request: Request):
    """投一条唤醒（工单执行体的唯一入口）：槽位与模型通道都在投递之前裁决。

    返回 `turn-scheduled` 只表示「这条已经进收件箱、起了或等着起一轮」，不表示模型已经回话；
    回合结果去 /state 与 /history 看。被拒（槽满、模型没配）的原话就在这条响应里，那条唤醒
    哪儿也没去。
    """
    denied = _ticket_desktop_only(request)
    if denied is not None:
        return denied
    payload, err = await _ticket_payload(request)
    if err is not None:
        return err
    missing = _ticket_required(payload, "ticketId", "message", "assignmentId")
    if missing is not None:
        return missing
    refused = _ticket_prepare_refused(payload)
    if refused is not None:
        return refused
    arrived_at, aerr = _ticket_int(payload, "arrivedAtMs", 0, low=0, high=2 ** 62)
    if aerr is not None:
        return aerr
    runtime = _ticket_runtime()
    _ticket_remember_owner()
    return await _ticket_call(
        lambda: runtime.submit(
            ticket_id=str(payload.get("ticketId") or "").strip(),
            message=str(payload.get("message") or ""),
            assignment_id=str(payload.get("assignmentId") or "").strip(),
            title=str(payload.get("title") or ""),
            origin=str(payload.get("origin") or "human"),
            prev_assignment_id=str(payload.get("prevAssignmentId") or ""),
            arrived_at_ms=arrived_at,
        )
    )


@app.get("/api/tickets/state")
async def tickets_state(ticketId: str = ""):  # noqa: N803  （FastAPI 查询参数按前端约定的驼峰名）
    """一单现在什么样：worker / 领取 / 在飞作业 / 未读唤醒 / 对外状态（waiting|working|…）。"""
    if not ticketId.strip():
        return _ticket_error("TICKET_FIELD_MISSING", "缺少必填字段：ticketId（状态查询要指明查哪一单）")
    runtime = _ticket_runtime()
    return await _ticket_call(lambda: runtime.state(ticketId.strip()))


@app.get("/api/tickets/history")
async def tickets_history(ticketId: str = "", limit: int = 50):  # noqa: N803
    """回合历史：每轮一条（第几轮、消费了几条唤醒、怎么收的口）。"""
    if limit < 1 or limit > TICKET_HISTORY_LIMIT_MAX:
        return _ticket_error("TICKET_FIELD_INVALID", f"limit 超出可用范围 1–{TICKET_HISTORY_LIMIT_MAX}（收到 {limit}）")
    runtime = _ticket_runtime()
    return await _ticket_call(lambda: runtime.history(ticketId, limit=limit))


@app.post("/api/tickets/kick")
async def tickets_kick(request: Request):
    """手动推进一轮（把收件箱里压着的、或滞留的唤醒带走）。

    `prevAssignmentId` 是给「领取已经结束、要借旧一轮上下文」的那条路用的；不带就是就地踢
    当前活领取。
    """
    denied = _ticket_desktop_only(request)
    if denied is not None:
        return denied
    payload, err = await _ticket_payload(request)
    if err is not None:
        return err
    missing = _ticket_required(payload, "ticketId")
    if missing is not None:
        return missing
    runtime = _ticket_runtime()
    _ticket_remember_owner()
    return await _ticket_call(
        lambda: runtime.kick(str(payload.get("ticketId") or "").strip(), prev_assignment_id=str(payload.get("prevAssignmentId") or ""))
    )


@app.get("/api/tickets/resume/plan")
async def tickets_resume_plan(
    ticketId: str = "",  # noqa: N803
    prevAssignmentId: str = "",  # noqa: N803
    candidateWakes: str = "",  # noqa: N803
):
    """重启补查的只读一面：哪些唤醒从没进过会话历史、哪些已读、哪些箱里还压着。

    不动盘、不起回合，所以不设本机门槛。`candidateWakes` 是 JSON 数组字符串（服务端工单消息
    表里的候选）；不填就只用耐久收件箱那份磁盘凭据。
    """
    if not ticketId.strip() or not prevAssignmentId.strip():
        return _ticket_error("TICKET_FIELD_MISSING", "缺少必填字段：ticketId、prevAssignmentId（补查要知道补的是哪一单的哪一轮）")
    candidates: list[Any] = []
    if candidateWakes.strip():
        try:
            parsed = json.loads(candidateWakes)
        except Exception as exc:
            return _ticket_error("TICKET_FIELD_INVALID", f"candidateWakes 不是合法 JSON 数组：{type(exc).__name__}: {exc}")
        if not isinstance(parsed, list):
            return _ticket_error("TICKET_FIELD_INVALID", f"candidateWakes 顶层要是数组（是 {type(parsed).__name__}）")
        candidates = parsed
    runtime = _ticket_runtime()
    return await _ticket_call(
        lambda: runtime.plan(ticketId.strip(), prev_assignment_id=prevAssignmentId.strip(), candidate_wakes=candidates)
    )


@app.post("/api/tickets/resume")
async def tickets_resume(request: Request):
    """重启补查的写一面：把没喂进会话的唤醒投给**新**领取并起一轮。

    只投未读、先投完整批再起轮；一条也没投出去时会如实说（槽满）。旧领取的上下文只借不结论。
    """
    denied = _ticket_desktop_only(request)
    if denied is not None:
        return denied
    payload, err = await _ticket_payload(request)
    if err is not None:
        return err
    missing = _ticket_required(payload, "ticketId", "prevAssignmentId", "newAssignmentId")
    if missing is not None:
        return missing
    candidates = payload.get("candidateWakes")
    if candidates is not None and not isinstance(candidates, list):
        return _ticket_error("TICKET_FIELD_INVALID", f"candidateWakes 要是数组（是 {type(candidates).__name__}）")
    runtime = _ticket_runtime()
    _ticket_remember_owner()
    return await _ticket_call(
        lambda: runtime.resume_wakes(
            str(payload.get("ticketId") or "").strip(),
            prev_assignment_id=str(payload.get("prevAssignmentId") or "").strip(),
            new_assignment_id=str(payload.get("newAssignmentId") or "").strip(),
            candidate_wakes=candidates or [],
            title=str(payload.get("title") or ""),
            origin=str(payload.get("origin") or "human"),
        )
    )


@app.get("/api/tickets/inbox")
async def tickets_inbox(ticketId: str = ""):  # noqa: N803
    """耐久收件箱：投递即落盘的那份日志；给 ticketId 时同时看这一单还欠着哪几条。"""
    runtime = _ticket_runtime()
    return await _ticket_call(lambda: runtime.inbox_state(ticketId))


@app.get("/api/tickets/outbox")
async def tickets_outbox():
    """外发队列快照（含 statusCounts / 保序组 / 加载错误）。注意 records 是条数不是列表。"""
    runtime = _ticket_runtime()
    return await _ticket_call(lambda: runtime.outbox_state())


@app.post("/api/tickets/outbox/run")
async def tickets_outbox_run(request: Request):
    """手动推一把已提交的外发（退避到点的、保序该放行下一块的）。

    周期泵在 lifespan 里也在做同一件事；这个端点是给「刚配好通道，想立刻把欠的发出去」用的，
    不是唯一出路——手动推和自动推走的是同一个 `run_pending`，不存在两条会分道的口径。
    """
    denied = _ticket_desktop_only(request)
    if denied is not None:
        return denied
    payload, err = await _ticket_payload(request)
    if err is not None:
        return err
    limit, lerr = _ticket_int(payload, "limit", 16, low=1, high=TICKET_OUTBOX_LIMIT_MAX)
    if lerr is not None:
        return lerr
    runtime = _ticket_runtime()
    return await _ticket_call(lambda: runtime.run_outbox(limit=limit))


@app.post("/api/tickets/outbox/retry")
async def tickets_outbox_retry(request: Request):
    """把一条终态失败的外发改回待办（重试的那次仍走同一把幂等键，不会二次送达）。"""
    denied = _ticket_desktop_only(request)
    if denied is not None:
        return denied
    payload, err = await _ticket_payload(request)
    if err is not None:
        return err
    missing = _ticket_required(payload, "recordId")
    if missing is not None:
        return missing
    runtime = _ticket_runtime()
    return await _ticket_call(lambda: runtime.retry_outbox(str(payload.get("recordId") or "").strip()))


@app.post("/api/tickets/outbox/resolve")
async def tickets_outbox_resolve(request: Request):
    """保序组的逃生口：挡住后面那条要么 skip 放行，要么按原样重发。"""
    denied = _ticket_desktop_only(request)
    if denied is not None:
        return denied
    payload, err = await _ticket_payload(request)
    if err is not None:
        return err
    missing = _ticket_required(payload, "recordId", "action")
    if missing is not None:
        return missing
    runtime = _ticket_runtime()
    return await _ticket_call(
        lambda: runtime.resolve_outbox_block(str(payload.get("recordId") or "").strip(), str(payload.get("action") or "").strip())
    )


# ── #164 分片注册面（本文件唯一的路由挂载点）─────────────────────
# 各窗口新增引擎端点写进 engine/routes_<domain>.py，不要再打开本文件。
# 顺序即优先级：上面 61 条既有路由先注册，车道后挂；同名路径由既有路由赢，
# 而「被既有通配吞掉」「越界占用别人前缀」「车道内重名」都会在 attach() 里直接抛，
# 不带病启动（D-07.4 真机演练里那条通配吞掉 revoked.json 的缺陷，mock 永远测不出来）。
ROUTE_LANES = (
    routes_knowledge,
    routes_artifact,
    routes_workspace,
    routes_exec,
    routes_connect,
    routes_orchestrate,
    routes_voice,
)

ROUTE_LANE_REPORT = rln.attach(app, sys.modules[__name__], (lane.__name__ for lane in ROUTE_LANES))


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
    # D-28：先把主线程与工作线程的钩子挂上，再进 uvicorn——启动阶段自己炸了也得留现场。
    # 事件循环那一半在 lifespan 里补（构造期拿不到循环对象），本函数幂等。
    _crash_telemetry_bootstrap()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
