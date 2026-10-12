"""
通天办公 - AgentScope 引擎服务

基于 agentscope ReActAgent 的自研引擎，实现与 Rust AgentBridge 的 HTTP/SSE 契约：
- GET  /health            → 健康检查 + 版本信息
- POST /api/chat          → 执行任务，流式输出 SSE 事件（plan/step_*/deliver/done/error）
- GET  /api/events/replay → 事件重放（三层架构演进 P0：seq + 重放窗口 ≥10min）
- POST /api/chat/cancel   → 取消任务（cancelled 事件 + done）
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
import inspect
import os
import shutil
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
        # 安全工作环境开关（true 且未自选工作目录时，任务落在 environment/{taskId} 隔离目录）
        self.secure_env: bool = False
        # 本任务自定义模型配置（models.json 条目，approve 重新执行时沿用）
        self.model_config: dict | None = None
        # 此前轮次对话历史（两阶段协议下 plan 阶段带上来，approve 执行时沿用）
        self.history: list = []

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


# ── 模型用量台账（真实 token 采集） ────────────────────────────
#
# 权威持久化在 Rust 侧 model_usage 表（按 usage 事件记账，见 bridge/agent.rs）；
# 本台账服务于浏览器直连模式（无 Rust）与计划阶段用量展示，进程重启即清零。

def _utc_day(ms: int | None = None) -> str:
    """UTC 日期 yyyy-mm-dd（与 Rust 台账同口径：跨日按 UTC 分桶）"""
    ts = time.time() if ms is None else ms / 1000
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _utc_month(ms: int | None = None) -> str:
    return _utc_day(ms)[:7]


class UsageLedger:
    """进程内用量台账：按「模型 × 日」累计 prompt/completion token 与调用次数"""

    def __init__(self) -> None:
        # key = (model_name, day) → row
        self.rows: dict[tuple[str, str], dict] = {}

    def add(self, model_name: str, prompt_tokens: int, completion_tokens: int) -> dict:
        name = (model_name or "").strip() or "unknown"
        day = _utc_day()
        row = self.rows.get((name, day))
        if row is None:
            row = {
                "modelName": name,
                "day": day,
                "promptTokens": 0,
                "completionTokens": 0,
                "calls": 0,
                "lastUsedAtMs": 0,
            }
            self.rows[(name, day)] = row
        row["promptTokens"] += max(0, int(prompt_tokens))
        row["completionTokens"] += max(0, int(completion_tokens))
        row["calls"] += 1
        row["lastUsedAtMs"] = int(time.time() * 1000)
        return row

    def aggregate(self, period: str) -> list[dict]:
        """按周期聚合为「模型一行」（daily=今日 / monthly=本月 / total=全部）"""
        if period == "daily":
            match = lambda day: day == _utc_day()  # noqa: E731
        elif period == "monthly":
            month = _utc_month()
            match = lambda day: day.startswith(month)  # noqa: E731
        else:  # total（含非法值降级：引擎侧不阻断只读查询）
            match = lambda day: True  # noqa: E731

        agg: dict[str, dict] = {}
        for (name, day), row in self.rows.items():
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
                }
                agg[name] = cur
            cur["promptTokens"] += row["promptTokens"]
            cur["completionTokens"] += row["completionTokens"]
            cur["tokensUsed"] += row["promptTokens"] + row["completionTokens"]
            cur["calls"] += row["calls"]
            cur["lastUsedAtMs"] = max(cur["lastUsedAtMs"], row["lastUsedAtMs"])
        return sorted(agg.values(), key=lambda r: r["tokensUsed"], reverse=True)

    def _match(self, period: str):
        if period == "daily":
            day = _utc_day()
            return lambda d: d == day
        if period == "monthly":
            month = _utc_month()
            return lambda d: d.startswith(month)
        return lambda d: True

    def days(self, period: str) -> list[dict]:
        """按 UTC 日分桶的趋势点（升序），口径与 aggregate 同一周期过滤"""
        match = self._match(period)
        buckets: dict[str, dict] = {}
        for (name, day), row in self.rows.items():
            if not match(day):
                continue
            cur = buckets.get(day)
            if cur is None:
                cur = {"day": day, "tokensUsed": 0, "calls": 0, "models": 0, "_names": set()}
                buckets[day] = cur
            cur["tokensUsed"] += row["promptTokens"] + row["completionTokens"]
            cur["calls"] += row["calls"]
            cur["_names"].add(name)
        out = []
        for day in sorted(buckets):
            point = buckets[day]
            point["models"] = len(point.pop("_names"))
            out.append(point)
        return out

    def totals(self, period: str, models: list[dict] | None = None) -> dict:
        """周期汇总（不含延迟/成本：台账未采集延迟与单价，避免伪造数字）"""
        rows = models if models is not None else self.aggregate(period)
        return {
            "promptTokens": sum(r["promptTokens"] for r in rows),
            "completionTokens": sum(r["completionTokens"] for r in rows),
            "tokensUsed": sum(r["tokensUsed"] for r in rows),
            "calls": sum(r["calls"] for r in rows),
            "activeModels": len(rows),
            "lastUsedAtMs": max((r["lastUsedAtMs"] for r in rows), default=0),
        }


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
用户目标：{goal}"""


def _fallback_plan(goal: str) -> list:
    """无模型/生成失败时的确定性兜底计划（降级不阻断）"""
    return [
        {"step_id": "step-1", "type": "llm_call", "description": f"分析并处理：{goal[:60]}"},
        {"step_id": "step-2", "type": "file_write", "description": "产出交付物并总结"},
    ]


async def _generate_plan(goal: str) -> tuple[list, dict | None]:
    """单次 LLM 调用生成计划 steps；任何异常/解析失败均回退兜底计划。
    返回 (steps, usage)：usage 为该次计划生成的真实 token 用量（缺省 None）。"""
    captured: list = []

    def _capture(prompt: int, completion: int, model_name: str) -> None:
        captured.append({
            "promptTokens": prompt,
            "completionTokens": completion,
            "model": model_name,
        })

    try:
        # 计划需按完整响应解析 JSON，故以非流式调用（流式下 await model(...) 得到的是
        # 异步生成器，取不到 content，此前恒回退兜底计划）
        model, formatter = build_model_and_formatter(stream=False, on_usage=_capture)
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
                    return norm, (captured[-1] if captured else None)
    except Exception as exc:
        print(f"[engine] plan 生成失败，回退兜底计划: {exc}", flush=True)
    return _fallback_plan(goal), (captured[-1] if captured else None)


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
    """阶段一：计划生成流（plan + [usage] + status_change(pending_review) + done）"""
    sse = _sse_formatter(session, task_id)
    if not goal:
        yield sse("error", {"code": "EMPTY_GOAL", "message": "任务描述为空"})
        yield sse("done", {})
        return
    steps, plan_usage = await _generate_plan(goal)
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


async def _run_execution(session: "TaskSession", task_id: str, goal: str, replay_from: list, model_binding: str | None = None, work_dir: "Path | None" = None, history: list | None = None, model_config: dict | None = None):
    """阶段二：执行流（status_change(executing) → Agent 事件 → deliver/done）。
    支持 pause/resume 门控与 rollback_to 语义；model_binding 为对话内选中的模型档位；
    work_dir 为前端选择的工作目录（工具操作与产物落盘均限定其中）；
    history 为此前轮次对话历史（保障连续对话上下文）。
    启用安全工作环境且未自选目录时，执行落在 environment/{taskId} 隔离目录。"""
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

    # 构建 Agent（模型配置错误在此暴露；模型档位随对话绑定；工具限定在工作目录；model_config 支持自定义模型真实调用）
    # 队列先建：用量回调需要随模型调用即时产出 usage 事件（Rust 侧据此记账）
    queue = RecordingQueue(session)

    def _on_usage(prompt_tokens: int, completion_tokens: int, model_name: str) -> None:
        _safe_put(queue, "usage", {
            "usage": {
                "promptTokens": prompt_tokens,
                "completionTokens": completion_tokens,
                "model": model_name,
            },
        })

    try:
        agent = build_agent(model_binding, eff_work_dir, model_config, on_usage=_on_usage)
    except Exception as exc:
        yield sse("error", {"code": "ENGINE_CONFIG_ERROR", "message": str(exc)})
        yield sse("done", {})
        return

    yield sse("status_change", {"taskId": task_id, "status": "executing"})

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
            if isolated:
                # 隔离目录可被整体清理，产物先归档进会话目录再上报
                artifacts = _preserve_artifacts_in_session(
                    session.session_dir, eff_work_dir, artifacts
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


def build_model_and_formatter(model_override: str | None = None, model_config: dict | None = None,
                              stream: bool = True, on_usage=None):
    """按 .env 配置构建模型与 formatter（支持 DashScope 与 OpenAI 兼容端点）；
    model_override 非空时覆盖模型名（对话内模型档位绑定）；
    model_config 为前端传入的自定义模型配置（models.json，含 baseUrl/apiKey/modelName），
    存在 baseUrl 时走 OpenAI 兼容端点真实调用自定义模型；
    stream=False 用于「一次性取值」场景（计划生成按完整响应解析 JSON）；
    on_usage(prompt_tokens, completion_tokens, model_name) 在每次真实模型调用后回调"""
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
            stream=stream,
            client_kwargs={"base_url": custom_base},
        )
        return _with_usage_tracking(model, on_usage), OpenAIChatFormatter()

    provider = os.environ.get("TT_PROVIDER", "dashscope").lower()

    if provider == "dashscope":
        if not api_key:
            # 引导去设置页配自定义模型（models.json 随请求 modelConfig 下发）；安装版没有
            # "项目根 .env" 这个概念，.env 只是开发/内网联调时的兜底通道。
            raise RuntimeError("未配置可用模型：请在「设置 → 模型与用量」中添加模型（或配置 TT_API_KEY 兜底）")
        # 联网搜索：调研类任务可获取实时网络信息（TT_ENABLE_SEARCH=false 可关闭）
        enable_search = os.environ.get("TT_ENABLE_SEARCH", "true").lower() != "false"
        model = DashScopeChatModel(
            model_name=model_name,
            api_key=api_key,
            stream=stream,
            enable_search=enable_search,
        )
        return _with_usage_tracking(model, on_usage), DashScopeChatFormatter()

    # OpenAI 兼容协议（OpenAI / DeepSeek / Kimi / 本地 Ollama 等）
    base_url = os.environ.get("TT_BASE_URL", "")
    model = OpenAIChatModel(
        model_name=model_name,
        api_key=api_key or "EMPTY",
        stream=stream,
        client_kwargs={"base_url": base_url} if base_url else {},
    )
    return _with_usage_tracking(model, on_usage), OpenAIChatFormatter()


try:
    from agentscope.model import ChatModelBase
except Exception:  # agentscope 缺失时降级为本地基类，仅影响用量采集包装
    class ChatModelBase:  # type: ignore[no-redef]
        """agentscope 缺失时的占位基类（model_name/stream 语义一致）"""

        def __init__(self, model_name: str = "", stream: bool = True) -> None:
            self.model_name = model_name
            self.stream = stream


class UsageTrackingModel(ChatModelBase):
    """模型包装层：把每次真实调用的 token 用量记入台账并回调。

    agentscope 的 ReActAgent 只依赖 `stream` 属性与 `__call__`（含 `stream` 的
    运行时改写），故包装层透传这两者即可，不改动内核行为。

    流式响应的 usage 仅在末尾 chunk 出现（DashScope 末 chunk / OpenAI
    stream_options.include_usage），故按「最后一次非空 usage」计一次，
    避免逐 chunk 重复累计。
    """

    def __init__(self, inner, on_usage=None):
        self._inner = inner  # 必须先于 super().__init__：stream setter 需要它
        super().__init__(model_name=inner.model_name, stream=inner.stream)
        self._on_usage = on_usage

    @property
    def stream(self) -> bool:
        return self._inner.stream

    @stream.setter
    def stream(self, value: bool) -> None:
        self._inner.stream = value

    def _record(self, usage) -> None:
        if usage is None:
            return
        prompt, completion = usage
        row = USAGE_LEDGER.add(self.model_name, prompt, completion)
        if self._on_usage is not None:
            try:
                self._on_usage(prompt, completion, self.model_name)
            except Exception as exc:  # 回调失败不影响模型调用链
                print(f"[engine] usage 回调失败: {exc}", flush=True)
        print(
            f"[engine] usage model={self.model_name} +{prompt}/+{completion} "
            f"累计={row['promptTokens'] + row['completionTokens']} calls={row['calls']}",
            flush=True,
        )

    async def __call__(self, *args, **kwargs):
        res = await self._inner(*args, **kwargs)
        if inspect.isasyncgen(res):
            return self._wrap_stream(res)
        self._record(_usage_of(res))
        return res

    async def _wrap_stream(self, agen):
        last = None
        async for chunk in agen:
            parsed = _usage_of(chunk)
            if parsed is not None:
                last = parsed
            yield chunk
        self._record(last)


def _with_usage_tracking(model, on_usage):
    """统一挂载用量采集（无 agentscope 基类可用时原样返回，保证不阻断）"""
    try:
        return UsageTrackingModel(model, on_usage)
    except Exception as exc:
        print(f"[engine] 用量采集包装跳过: {exc}", flush=True)
        return model


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


def build_agent(model_binding: str | None = None, work_dir: "Path | None" = None, model_config: dict | None = None,
                on_usage=None):
    from agentscope.agent import ReActAgent
    from agentscope.memory import InMemoryMemory

    model, formatter = build_model_and_formatter(model_binding, model_config, on_usage=on_usage)
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
    # 安全工作环境开关（Rust 从设置读取后随提交下发，approve 重跑沿用）
    if payload.get("secureEnv") is not None:
        session.secure_env = bool(payload.get("secureEnv"))
    # 对话历史（保障连续对话）：桌面端两阶段协议下随 plan 请求到达，
    # 存进会话后 approve 的执行流才能预填充；单阶段请求直接用本次传入的
    history = _normalize_history(payload.get("history"))
    if history:
        session.history = history

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


# ── 自定义模型配置文件：真实路径 ~/.tongtianwork/models.json ─────────

def _models_config_path() -> Path:
    return Path.home() / ".tongtianwork" / "models.json"


@app.get("/api/usage")
async def usage_get(period: str = "monthly"):
    """模型用量（真实 token 台账）：daily=今日 / monthly=本月 / total=全部。

    仅供浏览器直连模式读取（无 Rust 后端时的联调通道）；
    Tauri 模式的权威数据在 Rust model_usage 表（按 usage 事件持久化）。
    台账为进程内累计，引擎重启清零。"""
    p = period if period in ("daily", "monthly", "total") else "total"
    return JSONResponse({"ok": True, "period": p, "rows": USAGE_LEDGER.aggregate(p)})


@app.get("/api/usage/report")
async def usage_report_get(period: str = "monthly"):
    """用量报表（按日趋势 + 明细 + 汇总）：与 Rust `usage_report` 同一返回体。"""
    p = period if period in ("daily", "monthly", "total") else "total"
    models = USAGE_LEDGER.aggregate(p)
    return JSONResponse(
        {
            "ok": True,
            "period": p,
            "generatedAtMs": int(time.time() * 1000),
            "days": USAGE_LEDGER.days(p),
            "models": models,
            "totals": USAGE_LEDGER.totals(p, models),
        }
    )


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
