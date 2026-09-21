"""
通天办公 - AgentScope 引擎服务

基于 agentscope ReActAgent 的自研引擎，实现与 Rust AgentBridge 的 HTTP/SSE 契约：
- GET  /health            → 健康检查 + 版本信息
- POST /api/chat          → 执行任务，流式输出 SSE 事件（plan/step_*/deliver/done/error）
- GET  /api/events/replay → 事件重放（三层架构演进 P0：seq + 重放窗口 ≥10min）
- POST /api/chat/cancel   → 取消任务（cancelled 事件 + done）

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
import os
import subprocess
import sys
import time
import traceback
import uuid
from collections import deque
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import json
import uvicorn

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
        # set=运行中；clear=已暂停（control pause/resume 门控）
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        # 执行期间的活动队列（rollback 事件注入点）
        self.exec_queue: "RecordingQueue | None" = None
        # rollback_to 目标（非 None 时 CancelledError 走 rolled_back 语义）
        self.rolled_back: str | None = None
        # 会话子目录（工作目录下按对话时间创建，存会话内容与产出物）
        self.session_dir: Path | None = None
        # 对话内模型档位绑定（approve 重新执行时沿用）
        self.model_binding: str | None = None
        # 本任务工作目录（前端选择的工作目录，approve 重新执行时沿用）
        self.work_dir: Path | None = None
        # 本任务自定义模型配置（models.json 条目，approve 重新执行时沿用）
        self.model_config: dict | None = None

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


# 任务会话注册表（进程内；引擎重启后由 Rust 侧 event_store 兜底）
_TASK_SESSIONS: dict[str, TaskSession] = {}


def _get_or_create_session(task_id: str) -> TaskSession:
    session = _TASK_SESSIONS.get(task_id)
    if session is None:
        session = TaskSession(task_id)
        _TASK_SESSIONS[task_id] = session
    return session


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


# ── 两阶段协议：计划生成（流 3.1，核心闭环补齐） ────────────────

PLAN_PROMPT = """请将用户目标拆解为 2-5 个执行步骤，仅输出 JSON 数组，格式：
[{{"step_id": "step-1", "type": "llm_call", "description": "步骤说明"}}]
type 可选值：llm_call / file_read / file_write / file_convert / user_confirm
用户目标：{goal}"""


def _fallback_plan(goal: str) -> list:
    """无模型/生成失败时的确定性兜底计划（降级不阻断）"""
    return [
        {"step_id": "step-1", "type": "llm_call", "description": f"分析并处理：{goal[:60]}"},
        {"step_id": "step-2", "type": "file_write", "description": "产出交付物并总结"},
    ]


async def _generate_plan(goal: str) -> list:
    """单次 LLM 调用生成计划 steps；任何异常/解析失败均回退兜底计划"""
    try:
        model, formatter = build_model_and_formatter()
        from agentscope.message import Msg
        msgs = [
            Msg(name="user", content=PLAN_PROMPT.format(goal=goal), role="user"),
        ]
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
                    return norm
    except Exception as exc:
        print(f"[engine] plan 生成失败，回退兜底计划: {exc}", flush=True)
    return _fallback_plan(goal)


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


async def _plan_stream(session: "TaskSession", task_id: str, goal: str):
    """阶段一：计划生成流（plan + status_change(pending_review) + done）"""
    sse = _sse_formatter(session, task_id)
    if not goal:
        yield sse("error", {"code": "EMPTY_GOAL", "message": "任务描述为空"})
        yield sse("done", {})
        return
    steps = await _generate_plan(goal)
    session.plan = steps
    session.goal = goal
    yield sse("plan", {"plan_id": f"plan-{task_id}", "steps": steps})
    yield sse("status_change", {"taskId": task_id, "status": "pending_review"})
    yield sse("done", {})


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


async def _run_execution(session: "TaskSession", task_id: str, goal: str, replay_from: list, model_binding: str | None = None, work_dir: "Path | None" = None, history: list | None = None, model_config: dict | None = None):
    """阶段二：执行流（status_change(executing) → Agent 事件 → deliver/done）。
    支持 pause/resume 门控与 rollback_to 语义；model_binding 为对话内选中的模型档位；
    work_dir 为前端选择的工作目录（工具操作与产物落盘均限定其中）；
    history 为此前轮次对话历史（保障连续对话上下文）。"""
    sse = _sse_formatter(session, task_id)
    eff_work_dir = work_dir or WORK_DIR

    for chunk in _replay_prefix(session, task_id, replay_from):
        yield chunk

    if not goal:
        yield sse("error", {"code": "EMPTY_GOAL", "message": "任务描述为空"})
        yield sse("done", {})
        return

    # 构建 Agent（模型配置错误在此暴露；模型档位随对话绑定；工具限定在工作目录；model_config 支持自定义模型真实调用）
    try:
        agent = build_agent(model_binding, eff_work_dir, model_config)
    except Exception as exc:
        yield sse("error", {"code": "ENGINE_CONFIG_ERROR", "message": str(exc)})
        yield sse("done", {})
        return

    yield sse("status_change", {"taskId": task_id, "status": "executing"})

    queue = RecordingQueue(session)
    session.exec_queue = queue
    session.pause_event.set()
    session.rolled_back = None
    attach_hooks(agent, queue)

    from agentscope.message import Msg

    async def run_agent():
        try:
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

            # 执行前快照：执行后 diff 得到真实产出物
            before = _snapshot_files(eff_work_dir)
            msg = Msg(name="user", content=goal, role="user")
            result = await agent(msg)

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
                            _safe_put(queue, "step_start", {
                                "step_id": f"step-{step_i}",
                                "stepType": "tool_call",
                                "description": f"调用工具: {block.get('name', '')}",
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
            _safe_put(queue, "deliver", {
                "summary": "任务完成",
                "artifacts": artifacts,
                "final_text": _text_of(getattr(result, "content", "")),
            })
        except asyncio.CancelledError:
            if session.rolled_back is not None:
                # 回滚路径（control rollback_to）：rolled_back 语义（转移 #22）
                _safe_put(queue, "status_change", {
                    "taskId": task_id,
                    "status": "rolled_back",
                    "statusReasonCode": f"ROLLBACK_TO:{session.rolled_back}",
                })
            else:
                # 取消路径（POST /api/chat/cancel）：cancelled 状态事件后正常收尾
                _safe_put(queue, "status_change", {
                    "taskId": task_id,
                    "status": "cancelled",
                    "statusReasonCode": "USER_CANCELLED",
                })
        except Exception as exc:
            traceback.print_exc()
            _safe_put(queue, "error", {"code": "AGENT_ERROR", "message": str(exc)})
        finally:
            _safe_put(queue, "done", {})

    task = asyncio.create_task(run_agent())
    session.agent_task = task

    last_output = time.monotonic()
    try:
        while True:
            # 暂停门控：paused 期间不再产出事件（事件在队列中积压），保活心跳
            while not session.pause_event.is_set():
                if time.monotonic() - last_output >= KEEPALIVE_INTERVAL_SECONDS:
                    yield ": keepalive\n\n"
                    last_output = time.monotonic()
                await asyncio.sleep(0.3)
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


def build_model_and_formatter(model_override: str | None = None, model_config: dict | None = None):
    """按 .env 配置构建模型与 formatter（支持 DashScope 与 OpenAI 兼容端点）；
    model_override 非空时覆盖模型名（对话内模型档位绑定）；
    model_config 为前端传入的自定义模型配置（models.json，含 baseUrl/apiKey/modelName），
    存在 baseUrl 时走 OpenAI 兼容端点真实调用自定义模型"""
    from agentscope.model import DashScopeChatModel, OpenAIChatModel
    from agentscope.formatter import DashScopeChatFormatter, OpenAIChatFormatter

    cfg = model_config or {}
    custom_base = str(cfg.get("baseUrl") or "").strip()
    custom_key = str(cfg.get("apiKey") or "").strip()
    custom_model = str(cfg.get("modelName") or "").strip()

    model_name = (model_override or "").strip() or custom_model or os.environ.get("TT_MODEL", "qwen-plus")
    api_key = custom_key or os.environ.get("TT_API_KEY", "")

    # 自定义模型（models.json 配置）：OpenAI 兼容端点真实调用
    if custom_base:
        model = OpenAIChatModel(
            model_name=model_name,
            api_key=api_key or "EMPTY",
            stream=True,
            client_kwargs={"base_url": custom_base},
        )
        return model, OpenAIChatFormatter()

    provider = os.environ.get("TT_PROVIDER", "dashscope").lower()

    if provider == "dashscope":
        if not api_key:
            # 引导去设置页配自定义模型（models.json 随请求 modelConfig 下发）；安装版没有
            # "项目根 .env" 这个概念，.env 只是开发/内网联调时的兜底通道。
            raise RuntimeError("未配置可用模型：请在「设置 → 模型与用量」中添加模型（或配置 TT_API_KEY 兜底）")
        # 联网搜索：调研类任务可获取实时网络信息（TT_ENABLE_SEARCH=false 可关闭）
        enable_search = os.environ.get("TT_ENABLE_SEARCH", "true").lower() != "false"
        model = DashScopeChatModel(model_name=model_name, api_key=api_key, stream=True, enable_search=enable_search)
        return model, DashScopeChatFormatter()

    # OpenAI 兼容协议（OpenAI / DeepSeek / Kimi / 本地 Ollama 等）
    base_url = os.environ.get("TT_BASE_URL", "")
    model = OpenAIChatModel(
        model_name=model_name,
        api_key=api_key or "EMPTY",
        stream=True,
        client_kwargs={"base_url": base_url} if base_url else {},
    )
    return model, OpenAIChatFormatter()


def build_toolkit(work_dir: Path):
    """注册文件操作工具（自实现：路径限定在工作目录内，相对路径自动解析到工作目录）"""
    from agentscope.tool import Toolkit, ToolResponse
    from agentscope.message import TextBlock

    base = work_dir.resolve()

    def _resolve(path: str) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = base / p
        return p.resolve()

    def _guard(p: Path) -> str | None:
        """目录越界/不存在检查，返回错误文案或 None"""
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
        err = _guard(p)
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
        err = _guard(p)
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

    toolkit = Toolkit()
    toolkit.register_tool_function(view_text_file)
    toolkit.register_tool_function(write_text_file)
    toolkit.register_tool_function(insert_text_file)
    return toolkit


def build_agent(model_binding: str | None = None, work_dir: "Path | None" = None, model_config: dict | None = None):
    from agentscope.agent import ReActAgent
    from agentscope.memory import InMemoryMemory

    model, formatter = build_model_and_formatter(model_binding, model_config)
    # 注入当前时间：模型涉及日期/“今天、本周”等概念及生成带日期文件名时以此为准
    weekdays = "一二三四五六日"
    now = time.localtime()
    time_hint = (
        f"\n\n当前时间：{time.strftime('%Y-%m-%d %H:%M:%S', now)}，星期{weekdays[now.tm_wday]}。"
        f"涉及日期、时间或\u201c今天/本周/本月\u201d等表述时以此为准；"
        f"生成带日期的文件名时使用该日期。"
    )
    agent = ReActAgent(
        name="通天助手",
        sys_prompt=SYS_PROMPT + time_hint,
        model=model,
        formatter=formatter,
        toolkit=build_toolkit(work_dir or WORK_DIR),
        memory=InMemoryMemory(),
        max_iters=8,
    )
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


# ── FastAPI 应用 ───────────────────────────────────────────────

app = FastAPI(title="tongtian-agent-engine", version=ENGINE_VERSION)

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
    if model_binding:
        session.model_binding = model_binding
    # 自定义模型配置（models.json 条目：baseUrl/apiKey/modelName，存在 baseUrl 时真实调用该端点）
    model_config_raw = payload.get("modelConfig")
    model_config = model_config_raw if isinstance(model_config_raw, dict) else None
    if model_config:
        session.model_config = model_config
    # 前端选择的工作目录：工具操作与产物落盘均限定其中（approve 重跑沿用）
    work_dir_raw = str(payload.get("workDir") or "").strip()
    if work_dir_raw:
        try:
            session.work_dir = Path(work_dir_raw).expanduser()
        except Exception:
            session.work_dir = None

    # 会话子目录：工作目录下按对话时间创建，存会话内容与产出物
    if session.session_dir is None and goal:
        work_dir = str(payload.get("workDir") or payload.get("work_dir") or "").strip()
        try:
            session.session_dir = _create_session_dir(work_dir, goal)
            _log_conversation(session.session_dir, f"# 任务：{goal}")
            _log_conversation(session.session_dir, f"- 对话时间：{time.strftime('%Y-%m-%d %H:%M:%S')}，任务 ID：{task_id}\n")
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

    # 对话历史（保障连续对话）：前端传入此前轮次的 user/assistant 消息列表
    history_raw = payload.get("history") or []
    history = history_raw if isinstance(history_raw, list) else []

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
    if session is None:
        return JSONResponse(
            {"ok": False, "code": "TASK_NOT_FOUND", "taskId": task_id},
            status_code=404,
        )

    sse_headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    sse = _sse_formatter(session, task_id)

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


@app.post("/api/chat/control")
async def chat_control(request: Request):
    """任务控制（转移 #14/#21/#22）：pause/resume 门控执行流；
    rollback_to 终止执行并发 rolled_back 事件（文件级恢复由 Rust 备份模块承接）"""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    task_id = (payload.get("task_id") or "").strip()
    action = (payload.get("action") or "").strip()
    step_id = (payload.get("step_id") or "").strip()
    session = _TASK_SESSIONS.get(task_id)
    if session is None:
        return JSONResponse({"ok": False, "code": "TASK_NOT_FOUND", "taskId": task_id})

    if action == "pause":
        session.pause_event.clear()
        return JSONResponse({"ok": True, "taskId": task_id, "action": "pause"})
    if action == "resume":
        session.pause_event.set()
        return JSONResponse({"ok": True, "taskId": task_id, "action": "resume"})
    if action == "rollback_to":
        # 标记回滚目标后取消执行；run_agent 的 CancelledError 分支发 rolled_back 事件
        session.rolled_back = step_id or "unknown"
        if session.agent_task is not None and not session.agent_task.done():
            session.agent_task.cancel()
        return JSONResponse({"ok": True, "taskId": task_id, "action": "rollback_to", "stepId": step_id})
    return JSONResponse({"ok": False, "code": "VALIDATION_ERROR", "message": f"未知 action: {action}"})


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
- （自动维护中）

## 习惯
- （自动维护中）""",
    "MEMORY.md": """# 长期记忆

（暂无记忆条目，对话中值得记住的事实将自动沉淀于此）""",
}


def _awareness_home() -> Path:
    return Path.home() / ".tongtianwork" / "awareness" / "main"


# ── 自定义模型配置文件：真实路径 ~/.tongtianwork/models.json ─────────

def _models_config_path() -> Path:
    return Path.home() / ".tongtianwork" / "models.json"


@app.get("/api/models/config")
async def models_config_get():
    """读取自定义模型配置文件（真实存在，首次访问自动初始化）"""
    p = _models_config_path()
    if not p.exists():
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"models": []}, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            return JSONResponse({"ok": False, "message": f"无法初始化配置文件: {exc}"}, status_code=500)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {"models": []}
    models = data.get("models") if isinstance(data, dict) else None
    return JSONResponse({"ok": True, "path": str(p), "models": models if isinstance(models, list) else []})


@app.post("/api/models/config")
async def models_config_set(request: Request):
    """将自定义模型列表整体写入配置文件（供后续对话真实调用）"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "无效 JSON"}, status_code=400)
    models = payload.get("models")
    if not isinstance(models, list):
        return JSONResponse({"ok": False, "message": "models 必须为数组"}, status_code=400)
    p = _models_config_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"models": models}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        return JSONResponse({"ok": False, "message": f"写入失败: {exc}"}, status_code=500)
    return JSONResponse({"ok": True, "path": str(p)})


@app.get("/api/awareness/load")
async def awareness_load():
    """读取真实意识目录下的能力文件；四个能力文件（协作风格/工作手册/
    用户画像/长期记忆）缺失时以默认模板初始化落盘，保证目录内真实存在"""
    home = _awareness_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    files: dict = {}
    for name in AWARENESS_FILE_NAMES:
        p = home / name
        if not p.exists():
            try:
                p.write_text(AWARENESS_DEFAULTS.get(name, ""), encoding="utf-8")
            except Exception:
                continue
        try:
            files[name] = {
                "content": p.read_text(encoding="utf-8"),
                "updatedAt": time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(p.stat().st_mtime)),
            }
        except Exception:
            continue
    return JSONResponse({"ok": True, "path": str(home), "files": files})


@app.post("/api/awareness/save")
async def awareness_save(request: Request):
    """意识能力文件真实落盘到 ~/.tongtianwork/awareness/main（文件名白名单防穿越）"""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "无效 JSON"}, status_code=400)
    name = Path(str(payload.get("fileName") or "")).name
    if name not in AWARENESS_FILE_NAMES:
        return JSONResponse({"ok": False, "message": f"未知意识文件: {name}"}, status_code=400)
    home = _awareness_home()
    home.mkdir(parents=True, exist_ok=True)
    target = home / name
    target.write_text(str(payload.get("content") or ""), encoding="utf-8")
    return JSONResponse({"ok": True, "path": str(target)})


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


# ── 入口 ───────────────────────────────────────────────────────

def main():
    global WORK_DIR
    parser = argparse.ArgumentParser(description="TongTian Agent Engine (agentscope)")
    parser.add_argument("--port", type=int, default=18000)
    parser.add_argument("--work-dir", type=str, default=".")
    args = parser.parse_args()

    WORK_DIR = Path(args.work_dir).resolve()
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    # 加载项目根 .env（引擎脚本位于 <root>/engine/，向上一级）
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    print(
        f"[engine] tongtian-agent-engine v{ENGINE_VERSION} on 127.0.0.1:{args.port} "
        f"(provider={os.environ.get('TT_PROVIDER', 'dashscope')}, "
        f"model={os.environ.get('TT_MODEL', 'qwen-plus')}, "
        f"key={'yes' if os.environ.get('TT_API_KEY') else 'NO'}, "
        f"work-dir={WORK_DIR})",
        flush=True,
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
