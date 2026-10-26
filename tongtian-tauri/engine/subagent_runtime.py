"""本地子 Agent 运行时（多 Agent 第一层）

父 Agent 通过 `spawn_subagents` 把可独立完成的子任务交给同进程内的子 Agent：

- 每个子 Agent 有独立的 ReActAgent 与记忆，看不到父 Agent 的对话细节；
- 共享同一个工作目录、启用技能、沙箱策略与 Hooks，写出的文件仍由父任务统一审计；
- 子 Agent 的开始/结束和每次工具调用都进入父任务事件流，结果汇总回父 Agent 整合；
- 子 Agent 不能再派生子 Agent，单任务最多 4 个、最多 3 路并行。

边界：这些 Agent 都在本机同一进程里，没有跨用户、跨电脑的项目协作或消息中继。
本模块不负责 Hooks 脚本执行（仍由 hooks_runtime 交给 Rust 本机网关），也不自行拉进程。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import expert_runtime as expert_rt

MAX_SUBAGENTS_PER_TASK = 4
MAX_CONCURRENCY = 3
MAX_ITERS = 6
MAX_INSTRUCTION_CHARS = 4000
MAX_CONTEXT_CHARS = 4000
MAX_RESULT_CHARS = 4000
MAX_TOTAL_RESULT_CHARS = 16000
MAX_CONTINUATIONS = 1
TIMEOUT_SECONDS = 300.0
AGENT_TYPE = "general-purpose"

SUBAGENT_PROMPT = """你是“通天办公”的子 Agent，只负责一个被明确指定的子任务。

约束:
- 你不能与用户交互，也不能再派生子 Agent；按任务说明独立完成。
- 你看不到父 Agent 的对话历史，信息不足时按现有材料完成，并在汇报里说明缺口。
- 需要读写文件时调用真实文件工具；不要声称完成了未实际执行的操作。
- 完成后用简洁中文汇报：做了什么、写入/修改了哪些文件、结论与未解决项。
"""


def _brief(text: Any, limit: int = 200) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[:limit] + "…"


def _truncate(text: str, limit: int, label: str) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n…（{label}已截断，原文共 {len(text)} 字）", True


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts).strip()


def _response_text(chunks: list[Any]) -> str:
    out: list[str] = []
    for resp in chunks:
        for block in getattr(resp, "content", None) or []:
            text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if text:
                out.append(str(text))
    return "\n".join(out)


def parse_tasks(
    raw: str,
    experts: list[dict] | None = None,
    experts_error: str | None = None,
) -> tuple[list[dict], str | None]:
    """解析并校验模型给的子任务数组；任何一项不合法都整体拒绝，不静默跳过。

    `expert` 字段可选：按 id 或名称指向 runtime.experts 中的专家；指向不存在/
    模型不可用的专家时整体拒绝，不静默退回通用子 Agent（那会假装专家能力生效）。
    """
    try:
        data = json.loads(raw or "[]")
    except Exception as exc:  # noqa: BLE001  模型给坏 JSON 是常见情况，要如实回给模型
        return [], f"tasks_json 不是合法 JSON：{exc}"
    if isinstance(data, dict):
        data = data.get("tasks")
    if not isinstance(data, list) or not data:
        return [], "tasks_json 必须是非空的任务数组"

    out: list[dict] = []
    for i, item in enumerate(data, 1):
        if isinstance(item, str):
            item = {"instruction": item}
        if not isinstance(item, dict):
            return [], f"第 {i} 项不是对象或字符串"
        title = _brief(item.get("title") or f"子任务 {i}", 60)
        instruction = str(item.get("instruction") or item.get("task") or "").strip()
        if not instruction:
            return [], f"第 {i} 项缺少 instruction（子 Agent 看不到父对话，任务说明必须自足）"
        context = str(item.get("context") or "").strip()
        expert_ref = item.get("expert") or item.get("expertId") or item.get("expert_id")
        expert = None
        if _brief(expert_ref, 80):
            if experts_error:
                return [], f"第 {i} 项指定了专家，但专家团队不可用：{experts_error}"
            expert = expert_rt.find_expert(experts or [], expert_ref)
            if expert is None:
                return [], (
                    f"第 {i} 项指定的专家「{_brief(expert_ref, 40)}」不存在。"
                    f"可用专家：{expert_rt.available_line(experts or [])}"
                )
            _, model_err = expert_rt.model_config_of(expert)
            if model_err:
                return [], f"第 {i} 项不能派发：{model_err}"
        out.append({
            "title": title,
            "instruction": instruction,
            "context": context,
            "expert": expert,
        })
    return out, None


class SubagentRunner:
    """把子任务派发给独立 ReActAgent 的运行时。

    make_agent(name, max_iters, expert) 由 server 侧提供：按专家解析出的模型配置
    构建子 Agent，但不再注册 spawn_subagents（杜绝递归派生）。
    """

    def __init__(
        self,
        *,
        make_agent: Callable[[str, int, dict | None], Any],
        emit: Callable[[str, dict], None],
        task_id: str = "",
        goal: str = "",
        work_dir: str = "",
        sandbox: Any = None,
        hooks: Any = None,
        approval: Any = None,
        experts: list[dict] | None = None,
        experts_error: str | None = None,
        available_skill_ids: list[str] | None = None,
    ):
        self.make_agent = make_agent
        self.emit = emit
        self.task_id = task_id
        self.goal = goal
        self.work_dir = work_dir
        self.sandbox = sandbox
        self.hooks = hooks
        self.approval = approval
        self.experts = list(experts or [])
        self.experts_error = experts_error
        self.available_skill_ids = available_skill_ids
        self.spawned = 0
        self._step = 0
        self._sem = asyncio.Semaphore(MAX_CONCURRENCY)

    # ── 提示词与工具 ──────────────────────────────────────────

    def system_hint(self) -> str:
        hint = (
            "\n\n## 本地子 Agent\n"
            f"遇到可以独立完成的子任务时，调用 spawn_subagents 一次性派发（单任务最多 "
            f"{MAX_SUBAGENTS_PER_TASK} 个，最多 {MAX_CONCURRENCY} 路并行）。"
            "每个子 Agent 有自己的上下文和工作目录内的文件工具，但看不到你此前的对话细节，"
            "所以任务说明必须自足；它们不能向用户提问，也不能再派发子 Agent。"
            "子 Agent 的汇报未经验证，必须由你核对后再整合；失败项要如实说明，不能当成已完成。"
        )
        if self.experts:
            lines = [
                "\n\n## 可用专家",
                "派发时在任务对象里用 `expert` 字段填写专家 id（优先）或名称；"
                "不填则使用通用子 Agent。把任务交给职责最匹配的专家，不要为了用专家而硬拆任务。",
            ]
            for expert in self.experts:
                model = expert.get("modelName") or "沿用本轮对话模型"
                lines.append(
                    f"- {expert['name']}（id: {expert['id']}，模型: {model}）："
                    f"{expert['description']}"
                )
            if self.experts_error:
                lines.append(f"- 注意：专家注册表存在问题，专家派发会失败：{self.experts_error}")
            hint += "\n".join(lines)
        elif self.experts_error:
            hint += f"\n\n专家团队当前不可用：{self.experts_error}。不要尝试按专家派发。"
        return hint

    def tool(self):
        async def spawn_subagents(tasks_json: str):
            """把可独立完成的子任务派发给本地子 Agent 并行执行，并汇总结果。

            Args:
                tasks_json (`str`):
                    子任务数组的 JSON 字符串，每个元素形如
                    {"title": "标题", "instruction": "自足的任务说明",
                     "context": "可选补充材料", "expert": "可选专家 id 或名称"}。
                    最多 4 个；子 Agent 看不到当前对话，任务说明必须自足。
            """
            from agentscope.message import TextBlock
            from agentscope.tool import ToolResponse

            tasks, err = parse_tasks(tasks_json, self.experts, self.experts_error)
            if err:
                return ToolResponse(content=[TextBlock(type="text", text=f"错误：{err}")])
            if len(tasks) > MAX_SUBAGENTS_PER_TASK:
                return ToolResponse(content=[TextBlock(
                    type="text",
                    text=(
                        f"错误：单次最多派发 {MAX_SUBAGENTS_PER_TASK} 个子 Agent"
                        f"（本次 {len(tasks)} 个）。请拆成多次调用，或把相近任务合并。"
                    ),
                )])
            if self.spawned + len(tasks) > MAX_SUBAGENTS_PER_TASK:
                return ToolResponse(content=[TextBlock(
                    type="text",
                    text=(
                        f"错误：本任务累计最多派发 {MAX_SUBAGENTS_PER_TASK} 个子 Agent"
                        f"（已派发 {self.spawned} 个，本次请求 {len(tasks)} 个）。"
                        "剩余工作请由你自己完成。"
                    ),
                )])

            self.spawned += len(tasks)
            results = await asyncio.gather(
                *(self._run_one(i, task) for i, task in enumerate(tasks, 1)),
                return_exceptions=True,
            )
            from privacy_guard import PrivacyBlocked

            for item in results:
                if isinstance(item, PrivacyBlocked):
                    # 子 Agent 的出站内容被拦：整轮任务必须一起中止，不能只把它当失败项
                    raise item
            merged = self._merge(results)
            return ToolResponse(content=[TextBlock(type="text", text=merged)])

        return spawn_subagents

    # ── 单个子 Agent ──────────────────────────────────────────

    def _child_prompt(self, task: dict) -> str:
        instruction, cut_i = _truncate(
            task["instruction"], MAX_INSTRUCTION_CHARS, task["title"]
        )
        context, cut_c = _truncate(task["context"], MAX_CONTEXT_CHARS, "补充上下文")
        expert = task.get("expert")
        parts = [
            "总目标（用户本轮请求）：",
            self.goal or "（未提供）",
            "",
            (
                f"你的专家身份：{expert['name']}（{expert['description']}）"
                if expert else "你的身份：通用执行子 Agent"
            ),
            f"你的子任务：{task['title']}",
            instruction,
        ]
        if context:
            parts.extend(["", "补充上下文：", context])
        parts.extend([
            "",
            "执行要求：",
            "- 你只能看到这段说明，看不到父 Agent 的对话；信息不足时按现有材料完成，并在汇报里说明缺口。",
            "- 不要向用户提问，也不要派发新的子 Agent。",
            "- 需要落盘时使用工作目录内的文件工具；多个子任务并行时不要写同一个文件名。",
            "- 完成后用简洁中文汇报：做了什么、写入/修改了哪些文件、结论与未解决项。",
        ])
        if cut_i or cut_c:
            parts.append("- 注意：任务说明或补充上下文过长，已在上文截断。")
        return "\n".join(parts)

    def _install_guards(self, agent: Any, step_id: str, title: str) -> list[str]:
        """子 Agent 也必须走同一套沙箱门禁与 Hooks；装不上要如实说明"""
        notes: list[str] = []
        toolkit = agent.toolkit
        if self.sandbox is not None:
            err = self.sandbox.install(toolkit, self.emit, task_id=self.task_id)
            if err:
                raise RuntimeError(f"子 Agent 沙箱门禁没有装上：{err}")
        if self.hooks is not None:
            from hooks_runtime import install as install_hooks

            if not install_hooks(toolkit, self.hooks):
                notes.append("子 Agent 的 PreToolUse Hook 门禁没有装上，工具调用不会被 Hook 拦截")
        if self.approval is not None:
            err = self.approval.install(toolkit, self.emit, task_id=self.task_id)
            if err:
                notes.append(f"子 Agent 的逐工具审批门禁没有装上：{err}")
        toolkit.register_middleware(self._trace_middleware(step_id, title))
        return notes

    def _trace_middleware(self, parent_step_id: str, title: str):
        """把子 Agent 的每次工具调用摊进父任务事件流（并行时也能看清是谁在做什么）"""
        state = {"seq": 0}

        async def middleware(kwargs: dict, next_handler):
            tool_call = kwargs.get("tool_call") or {}
            name = str(tool_call.get("name") or "")
            state["seq"] += 1
            step_id = f"{parent_step_id}-tool-{state['seq']}"
            self.emit("step_start", {
                "step_id": step_id,
                "stepType": "tool_call",
                "tool": name,
                "description": f"{title}调用工具: {name}",
            })
            chunks: list[Any] = []
            async for resp in await next_handler(**kwargs):
                chunks.append(resp)
                yield resp
            text = _response_text(chunks)
            self.emit("step_done", {
                "step_id": step_id,
                "output": _brief(text or f"工具 {name} 执行完成", 300),
            })

        return middleware

    async def _fire_start(self, step_id: str, task: dict) -> dict:
        if self.hooks is None:
            return {"blocked": False, "notes": [], "results": []}
        try:
            return await self.hooks.fire(
                "SubagentStart",
                payload={
                    "agent_type": AGENT_TYPE,
                    "agentId": step_id,
                    "title": task["title"],
                    "instruction": _brief(task["instruction"], 500),
                    "expert": (task.get("expert") or {}).get("id", ""),
                    "expertName": (task.get("expert") or {}).get("name", ""),
                },
            )
        except Exception as exc:  # noqa: BLE001  Hook 失败不能拖垮子任务，但要说出来
            return {
                "blocked": False,
                "notes": [f"SubagentStart Hook 没有结果：{_brief(type(exc).__name__ + ': ' + str(exc))}"],
                "results": [],
            }

    async def _fire_stop(
        self, step_id: str, reason: str, result_text: str, task: dict | None = None
    ) -> dict:
        if self.hooks is None:
            return {"blocked": False, "notes": [], "results": []}
        expert = (task or {}).get("expert") or {}
        try:
            return await self.hooks.fire(
                "SubagentStop",
                payload={
                    "agent_type": AGENT_TYPE,
                    "agentId": step_id,
                    "reason": reason,
                    "result": _brief(result_text, 1000),
                    "expert": expert.get("id", ""),
                    "expertName": expert.get("name", ""),
                },
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "blocked": False,
                "notes": [f"SubagentStop Hook 没有结果：{_brief(type(exc).__name__ + ': ' + str(exc))}"],
                "results": [],
            }

    async def _execute(self, step_id: str, task: dict) -> dict:
        from agentscope.message import Msg

        start = await self._fire_start(step_id, task)
        notes = [str(n) for n in (start.get("notes") or [])]
        if start.get("blocked"):
            reason = "；".join(notes[:2]) or "SubagentStart Hook 返回了阻断"
            return {
                "ok": False,
                "text": f"子 Agent 没有启动：被 SubagentStart Hook 阻断（{reason}）",
                "notes": notes,
            }

        expert = task.get("expert")
        missing_skills: list[str] = []
        if expert and self.available_skill_ids is not None:
            enabled = set(self.available_skill_ids)
            missing_skills = [s for s in expert.get("skills") or [] if s not in enabled]
        notes.extend(expert_rt.expert_notes(expert, missing_skills))

        agent = self.make_agent(task["title"], MAX_ITERS, expert)
        notes.extend(self._install_guards(agent, step_id, task["title"]))
        msg = Msg(name="user", content=self._child_prompt(task), role="user")
        result = await agent(msg)
        final_text = _text_of(getattr(result, "content", ""))

        continuations = 0
        while True:
            stop = await self._fire_stop(step_id, "completed", final_text, task)
            notes.extend(str(n) for n in (stop.get("notes") or []))
            if not stop.get("blocked"):
                break
            if continuations >= MAX_CONTINUATIONS:
                notes.append(
                    f"SubagentStop Hook 要求继续，但已达到 {MAX_CONTINUATIONS} 次续跑上限"
                )
                break
            continuations += 1
            reason = "；".join(
                str(n) for n in (stop.get("notes") or []) if str(n).strip()
            ) or "Hook 要求继续完善"
            result = await agent(Msg(
                name="user",
                content=f"SubagentStop Hook 要求你继续完成剩余工作：{reason}\n请继续执行并给出最终结果。",
                role="user",
            ))
            final_text = _text_of(getattr(result, "content", ""))
            notes.append(f"按 SubagentStop Hook 续跑了 {continuations} 次")

        return {"ok": True, "text": final_text or "（子 Agent 没有给出文本结论）", "notes": notes}

    async def _run_one(self, index: int, task: dict) -> dict:
        self._step += 1
        step_id = f"subagent-{self._step}"
        expert = task.get("expert") or {}
        who = f"专家「{expert['name']}」" if expert else "子 Agent"
        self.emit("step_start", {
            "step_id": step_id,
            "stepType": "subagent",
            "description": f"{who}「{task['title']}」开始：{_brief(task['instruction'], 160)}",
            "expertId": expert.get("id", ""),
            "expertName": expert.get("name", ""),
        })
        try:
            async with self._sem:
                result = await asyncio.wait_for(
                    self._execute(step_id, task), timeout=TIMEOUT_SECONDS
                )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            result = {
                "ok": False,
                "text": f"子 Agent 超过 {int(TIMEOUT_SECONDS)} 秒未完成，已中止",
                "notes": [],
            }
            stop = await self._fire_stop(step_id, "timeout", result["text"], task)
            result["notes"].extend(str(n) for n in (stop.get("notes") or []))
        except Exception as exc:  # noqa: BLE001  单个子 Agent 失败不拖垮其它并行任务
            from privacy_guard import PrivacyBlocked

            if isinstance(exc, PrivacyBlocked):
                # 出站脱敏为 block 档：这是整个任务的硬中止，不是某个子 Agent 的局部失败
                raise
            result = {
                "ok": False,
                "text": f"子 Agent 执行失败：{type(exc).__name__}: {exc}",
                "notes": [],
            }
            stop = await self._fire_stop(step_id, "error", result["text"], task)
            result["notes"].extend(str(n) for n in (stop.get("notes") or []))

        result = {
            **result,
            "index": index,
            "title": task["title"],
            "stepId": step_id,
            "expert": expert.get("name", "") if expert else "",
        }
        text, cut = _truncate(str(result["text"]), MAX_RESULT_CHARS, "子任务结果")
        result["text"] = text
        if cut:
            result["notes"].append(f"结果超过 {MAX_RESULT_CHARS} 字，已截断")
        output = f"{who}「{task['title']}」完成：{_brief(result['text'], 200)}"
        if not result["ok"]:
            output = f"{who}「{task['title']}」失败：{_brief(result['text'], 200)}"
        self.emit("step_done", {"step_id": step_id, "output": output})
        return result

    def _merge(self, results: list) -> str:
        merged: list[dict] = []
        for i, item in enumerate(results, 1):
            if isinstance(item, BaseException):
                merged.append({
                    "ok": False,
                    "index": i,
                    "title": f"子任务 {i}",
                    "text": f"子 Agent 执行失败：{type(item).__name__}: {item}",
                    "notes": [],
                })
            else:
                merged.append(item)

        ok = sum(1 for r in merged if r.get("ok"))
        lines = [
            f"子任务结果汇总：{ok}/{len(merged)} 个成功（最多 {MAX_CONCURRENCY} 路并行）。",
            "以下是子 Agent 的汇报，未经验证；请核对后再整合，失败项不要当成已完成。",
        ]
        total = 0
        for r in merged:
            expert_label = f" · 专家 {r['expert']}" if r.get("expert") else ""
            block = [
                f"\n### {r['index']}. {r['title']}{expert_label}"
                f"（{'成功' if r.get('ok') else '失败'}）",
                str(r.get("text") or "（无输出）"),
            ]
            if r.get("notes"):
                block.append("说明：" + "；".join(str(n) for n in r["notes"] if str(n).strip()))
            text = "\n".join(block)
            if total + len(text) > MAX_TOTAL_RESULT_CHARS:
                lines.append("\n（其余结果因总长度上限未展开，请缩小任务范围后重试）")
                break
            total += len(text)
            lines.append(text)
        return "\n".join(lines)
