"""调度层：接收标准化消息 → 匹配技能 → 加载工具 → 复用存量 Agent 主循环 → 回填结果。

主循环仍是 `engine/server.py::build_agent`（ReActAgent + 既有 Toolkit / 沙箱 /
审批 / 上下文策略），本层只做三件事：
1. 把技能包翻译成运行时数据（技能正文、工具白名单、agentPolicy、MCP 服务器清单）；
2. 复用 `mcp_runtime.build_client` 建连、按技能白名单注册 MCP 工具；
3. 把 Agent 结果转成 `AgentReply`，并维护按会话隔离的轻量历史。
"""

from __future__ import annotations

import importlib
import time
import traceback
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import PlatformConfig, load_config
from .mcp_catalog import McpCatalog, McpCatalogError, McpClientPool
from .message import AgentMessage, AgentReply
from .skills import Skill, SkillRegistry

MAX_HISTORY_MESSAGES = 10


def _tool_names(toolkit) -> set[str]:
    tools = getattr(toolkit, "tools", None)
    return set(tools.keys()) if isinstance(tools, dict) else set()


def apply_tool_whitelist(toolkit, skill: Skill) -> tuple[list[str], list[str]]:
    """把技能白名单落到已注册的内置工具上；返回 (保留, 移除)。"""
    kept: list[str] = []
    removed: list[str] = []
    for name in sorted(_tool_names(toolkit)):
        if skill.tool_allowed(name):
            kept.append(name)
        else:
            try:
                toolkit.remove_tool_function(name, allow_not_exist=True)
            except Exception:
                pass
            removed.append(name)
    return kept, removed


class AgentDispatcher:
    def __init__(
        self,
        config: PlatformConfig | None = None,
        skills: SkillRegistry | None = None,
        catalog: McpCatalog | None = None,
        runner: Callable[[AgentMessage, Skill], Awaitable[AgentReply]] | None = None,
    ):
        self.config = config or load_config()
        self.skills = skills or SkillRegistry.load(self.config)
        self.catalog = catalog or McpCatalog.load(self.config)
        self._runner = runner
        self._history: dict[str, list[dict]] = {}

    # ── 对外入口 ──────────────────────────────────────────────────

    async def handle(self, message: AgentMessage) -> AgentReply:
        started = time.monotonic()
        error = message.validate()
        if error:
            return self._failure(message, "", "INVALID_MESSAGE", error, started)
        if not self.config.enabled:
            return self._failure(
                message, "", "PLATFORM_DISABLED",
                "平台层未启用：设置 TT_PLATFORM=1，或在 platform.yaml 写 enabled: true",
                started,
            )
        self.skills.reload_if_changed()
        skill, why = self.skills.resolve(message.skill, message.text)
        if skill is None:
            return self._failure(message, "", "SKILL_NOT_FOUND", why, started)
        try:
            runner = self._runner or self._run_engine
            reply = await runner(message, skill)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._failure(message, skill.id, "DISPATCH_FAILED", str(exc), started)
        if why and why not in reply.notes:
            reply.notes.append(why)
        reply.duration_ms = int((time.monotonic() - started) * 1000)
        return reply

    # ── 存量引擎适配 ──────────────────────────────────────────────

    async def _run_engine(self, message: AgentMessage, skill: Skill) -> AgentReply:
        server = importlib.import_module("server")
        from agentscope.message import Msg

        work_dir = self._work_dir(message)
        work_dir.mkdir(parents=True, exist_ok=True)

        servers, mcp_notes = self.catalog.servers_for(skill)
        model_config: dict = {}
        if skill.context.context_window is not None:
            model_config["contextWindow"] = skill.context.context_window
        runtime = {
            "skills": [skill.to_runtime_skill()],
            "agentPolicy": skill.context.policy(),
            "mcp": self.catalog.to_runtime_mcp(servers),
        }

        usage: dict = {}

        def _on_usage(prompt_tokens, completion_tokens, model_name, latency_ms=None,
                      cost_micros=None, currency=None, priced=False):
            usage.update({
                "promptTokens": prompt_tokens,
                "completionTokens": completion_tokens,
                "model": model_name,
                "latencyMs": latency_ms,
                "costMicros": cost_micros,
                "costCurrency": currency,
                "priced": priced,
            })

        before = server._snapshot_files(work_dir)
        agent = server.build_agent(
            model_binding=message.model or None,
            work_dir=work_dir,
            model_config=model_config or None,
            on_usage=_on_usage,
            runtime=runtime,
            mode=skill.context.mode,
            extra_prompt=skill.prompt_block(),
            agent_name=skill.name,
            max_iters=skill.context.max_iters,
            query=message.text,
        )
        notes = list(mcp_notes)
        kept, removed = apply_tool_whitelist(agent.toolkit, skill)
        if removed:
            notes.append("按技能白名单未注册工具：" + "、".join(removed))
        if skill.tools_allow:
            notes.append("技能工具白名单：" + "、".join(kept))

        pool = McpClientPool(self.catalog)
        try:
            for spec in servers:
                try:
                    client = await pool.acquire(spec.id)
                    tools = await pool.discover(spec.id)
                except McpCatalogError as exc:
                    notes.append(str(exc))
                    continue
                allowed = [t["name"] for t in tools if skill.mcp_tool_allowed(spec.id, t["name"])]
                skipped = [t["name"] for t in tools if not skill.mcp_tool_allowed(spec.id, t["name"])]
                if not allowed:
                    notes.append(f"连接器「{spec.name}」没有通过技能白名单的工具，未注册")
                    continue
                before_tools = _tool_names(agent.toolkit)
                try:
                    await agent.toolkit.register_mcp_client(
                        client, enable_funcs=allowed, namesake_strategy="skip"
                    )
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"连接器「{spec.name}」工具注册失败：{exc}")
                    continue
                added = sorted(_tool_names(agent.toolkit) - before_tools)
                if not added:
                    notes.append(f"连接器「{spec.name}」没有新增工具（同名工具已存在，未覆盖）")
                    continue
                line = f"连接器「{spec.name}」接入工具：" + "、".join(added)
                shadowed = [name for name in allowed if name not in added]
                if shadowed:
                    line += "；同名未覆盖：" + "、".join(shadowed)
                if skipped:
                    line += "；按技能白名单跳过：" + "、".join(skipped)
                notes.append(line)

            await self._prime_history(agent, message, Msg)
            result = await agent(Msg(name="user", content=message.text, role="user"))
        finally:
            await pool.close()

        text = server._text_of(getattr(result, "content", ""))
        artifacts = server._diff_files(
            work_dir, before, server._snapshot_files(work_dir)
        )
        steps = await self._collect_steps(agent)
        self._remember(message, text)
        return AgentReply(
            message_id=message.message_id,
            route_key=message.route_key,
            channel=message.channel,
            conversation_id=message.conversation_id,
            skill_id=skill.id,
            text=text,
            artifacts=artifacts,
            steps=steps,
            notes=notes,
            usage=usage,
            status="ok",
        )

    # ── 辅助 ─────────────────────────────────────────────────────

    def _work_dir(self, message: AgentMessage) -> Path:
        if message.work_dir:
            return Path(message.work_dir).expanduser()
        return self.config.workspace_dir / (message.conversation_id or "default")

    async def _prime_history(self, agent, message: AgentMessage, Msg) -> None:
        for item in self._history.get(message.route_key, [])[-MAX_HISTORY_MESSAGES:]:
            try:
                await agent.memory.add(Msg(
                    name="user" if item["role"] == "user" else "通天助手",
                    content=item["content"],
                    role=item["role"],
                ))
            except Exception:
                continue

    def _remember(self, message: AgentMessage, text: str) -> None:
        rows = self._history.setdefault(message.route_key, [])
        rows.append({"role": "user", "content": message.text})
        if text:
            rows.append({"role": "assistant", "content": text})
        del rows[:-MAX_HISTORY_MESSAGES]

    async def _collect_steps(self, agent) -> list[str]:
        try:
            memory = await agent.memory.get_memory()
        except Exception:
            return []
        steps: list[str] = []
        for item in memory or []:
            content = getattr(item, "content", None)
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = str(block.get("name") or "").strip()
                        if name:
                            steps.append(name)
        return steps

    def _failure(self, message: AgentMessage, skill_id: str, code: str,
                 detail: str, started: float) -> AgentReply:
        return AgentReply(
            message_id=message.message_id,
            route_key=message.route_key,
            channel=message.channel,
            conversation_id=message.conversation_id,
            skill_id=skill_id,
            error=f"{code}: {detail}" if detail else code,
            status="error",
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def view(self) -> dict:
        return {
            "config": self.config.view(),
            "skills": self.skills.view(),
            "mcp": self.catalog.view(),
        }
