#!/usr/bin/env python3
"""
上下文与工具效率自测（P1 上下文效率）

锁住三件会直接改变执行事实的事：
1. 单轮工具循环上限 / 上下文压缩阈值来自 runtime.agentPolicy + modelConfig.contextWindow，
   非法值要按边界执行并留下用户可见说明；
2. AgentScope 压缩配置真的能构造，token 计数失败只跳过压缩、不炸整轮任务；
3. MCP 工具按需载入：未激活分组不进 function schema，search_tools 命中后**只把被检索到的
   那几个**搬进已激活组（D-40 第 5 项验收：30 个工具只加载命中的 schema）；
   工具总数未过阈值时不绕搜索这一步，直接载入；
4. 检查点快照排除被压缩的原始消息，恢复时上下文不会翻倍。

离线运行；agentscope/mcp 缺失时输出 SKIP 并以 0 退出。
用法: python tests/integration/engine_efficiency_selftest.py
"""

import asyncio
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
FIXTURE = str(PROJECT_ROOT / "tests" / "integration" / "fixtures" / "mcp_echo_server.py")

import checkpoint_runtime as cr
import context_budget as cb
import mcp_runtime as mr
import tool_search as ts

try:
    from agentscope.tool import Toolkit
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: agentscope Toolkit unavailable ({exc})")
    sys.exit(0)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


# ── 1. 策略解析与可见说明 ──────────────────────────────────────

def policy_checks() -> None:
    print("--- 1. agentPolicy 归一与说明 ---")
    policy = cb.resolve(None, None)
    check("缺省 8 轮", policy.max_iters == 8, str(policy))
    check("缺省开启压缩", policy.auto_compress is True)
    check("缺省按需载入 MCP", policy.lazy_mcp_tools is True)
    check("缺省窗口 8192", policy.context_window == 8192)
    check("缺省阈值 4096（窗口8192减下限预留4096）", policy.trigger_threshold == 4096, str(policy.trigger_threshold))
    text = " ".join(policy.notes())
    check("说明里摊出窗口是估算值", "8192" in text and "估算" in text, text)

    runtime = {"agentPolicy": {"maxIters": 12, "autoCompress": False, "lazyMcpTools": False}}
    policy = cb.resolve(runtime, {"contextWindow": 32000})
    check("设置值生效", (policy.max_iters, policy.auto_compress, policy.lazy_mcp_tools) == (12, False, False), str(policy))
    check("阈值按模型窗口计算", policy.trigger_threshold == 27904, str(policy.trigger_threshold))
    wide = cb.resolve(None, {"contextWindow": 1_000_000})
    check(
        "百万窗口按动态预留触发（90%+，不再固定 60%）",
        wide.trigger_threshold == 950_000 and wide.compression_reserve() == 50_000,
        str(wide.trigger_threshold),
    )
    check("模型窗口被标记为真实值", policy.context_window_from_model is True)
    text = " ".join(policy.notes())
    check("关闭压缩时说明风险", "压缩已关闭" in text and "可能被模型拒绝" in text, text)
    check("关闭按需载入时不出现按需说明", "按需载入" not in text, text)

    runtime = {
        "agentPolicy": {
            "maxIters": 99,
            "autoCompress": "maybe",
            "notes": ["Rust 侧：设置值超出范围，已按 32 执行。"],
        }
    }
    policy = cb.resolve(runtime, {"contextWindow": 100})
    check("越界上限按 32 执行", policy.max_iters == 32, str(policy.max_iters))
    check("非布尔压缩开关按默认开启", policy.auto_compress is True)
    check("非法窗口回退 8192", (policy.context_window, policy.context_window_from_model) == (8192, False))
    text = " ".join(policy.notes())
    check("越界说明可见", "超出允许范围" in text, text)
    check("非布尔说明可见", "不是布尔值" in text, text)
    check("非法窗口说明可见", "不可用" in text, text)
    check("Rust 侧说明被保留", "Rust 侧" in text, text)

    value, note = cb.normalize_max_iters("many")
    check("非整数上限回退默认并说明", value == 8 and "不是整数" in note, f"{value} {note}")
    value, note = cb.normalize_max_iters("12")
    check("数字字符串可用", value == 12 and note == "")
    value, note = cb.normalize_max_iters(None)
    check("缺省不制造噪声", value == 8 and note == "")

    text = " ".join(cb.resolve(None, None).notes(mcp_server_count=2))
    check("有连接器时说明按需载入", "2 个连接器" in text and "search_tools" in text, text)


# ── 2. 压缩配置与 token 计数兜底 ───────────────────────────────

def compression_checks() -> None:
    print("--- 2. AgentScope 压缩配置 ---")
    policy = cb.resolve({"agentPolicy": {"maxIters": 8}}, {"contextWindow": 32000})
    config, error = cb.build_compression(policy, "qwen-max")
    check("压缩配置可构造", config is not None and error == "", error)
    if config is not None:
        check("压缩已启用", config.enable is True)
        check("阈值随窗口下发", config.trigger_threshold == 27904, str(config.trigger_threshold))
        check("保留最近 3 条", config.keep_recent == 3, str(config.keep_recent))
        check("计数器被安全包装", type(config.agent_token_counter).__name__ == "SafeTokenCounter", type(config.agent_token_counter).__name__)

    disabled = cb.resolve({"agentPolicy": {"autoCompress": False}}, None)
    config, error = cb.build_compression(disabled, "qwen-max")
    check("关闭压缩时不构造配置", config is None and error == "", f"{config} {error}")

    class BrokenCounter:
        async def count(self, messages, tools=None, **kwargs):
            raise RuntimeError("tokenizer 缺表")

    safe = cb.build_token_counter(BrokenCounter())
    got = asyncio.run(safe.count([]))
    check("计数失败返回 0 而不是抛错", got == 0, str(got))
    check("计数失败留下原因", "缺表" in safe.last_error, safe.last_error)


# ── 2b. 真实压缩：摘要写回、原文标记、无 metadata 时不动上下文 ────

def compression_behavior_checks() -> None:
    print("--- 2b. AgentScope 真实压缩行为 ---")
    from agentscope.agent import ReActAgent
    from agentscope.formatter import DashScopeChatFormatter
    from agentscope.memory import InMemoryMemory
    from agentscope.message import Msg
    from agentscope.model import ChatModelBase, ChatResponse

    SUMMARY = {
        "task_overview": "整理华东销售数据",
        "current_state": "已读取附件并核对字段",
        "important_discoveries": "口径差异出在日期字段",
        "next_steps": "生成对比表并交付",
        "context_to_preserve": "用户要求周五前交付",
    }

    class SummaryModel(ChatModelBase):
        """返回带 metadata 的摘要；metadata 缺失时 AgentScope 会跳过压缩。"""

        def __init__(self, with_metadata: bool = True):
            super().__init__(model_name="fake-summary-model", stream=False)
            self.with_metadata = with_metadata
            self.calls = 0

        async def __call__(self, messages, *args, **kwargs):
            self.calls += 1
            return ChatResponse(
                content=[{"type": "text", "text": "摘要正文"}],
                metadata=dict(SUMMARY) if self.with_metadata else None,
            )

    class BigCounter:
        async def count(self, messages, tools=None, **kwargs):
            return 10**9

    async def build_agent(with_metadata: bool):
        memory = InMemoryMemory()
        for i in range(6):
            await memory.add(
                Msg("user" if i % 2 == 0 else "assistant", f"历史消息 {i}", "user" if i % 2 == 0 else "assistant"),
            )
        policy = cb.resolve(None, None)
        config, error = cb.build_compression(policy, "qwen-max")
        assert config is not None, error
        # 用固定超阈值计数器替代真实 tokenizer：本段验证压缩流程，不验证分词精度
        config.agent_token_counter = cb.build_token_counter(BigCounter())
        model = SummaryModel(with_metadata)
        agent = ReActAgent(
            name="效率自测",
            sys_prompt="你是被测助手",
            model=model,
            formatter=DashScopeChatFormatter(),
            memory=memory,
        )
        agent.compression_config = config
        return agent, memory, model

    async def scenario() -> None:
        agent, memory, model = await build_agent(with_metadata=True)
        await agent._compress_memory_if_needed()
        check("压缩真的调用了一次摘要模型", model.calls == 1, str(model.calls))
        summary_kept = await memory.get_memory(exclude_mark="compressed", prepend_summary=False)
        check("被压缩的原文退出后续上下文", len(summary_kept) == 3, str([m.content for m in summary_kept]))
        check("保留的是最近 3 条", all("历史消息" in str(m.content) for m in summary_kept)
              and "历史消息 3" in str(summary_kept[0].content), str([m.content for m in summary_kept]))
        with_summary = await memory.get_memory(prepend_summary=True)
        joined = "\n".join(str(m.content) for m in with_summary)
        check("摘要按模板写回记忆", "整理华东销售数据" in joined and "周五前交付" in joined, joined[:300])
        check("摘要作为一条消息插在上下文最前", "整理华东销售数据" in str(with_summary[0].content))

        # 模型没有返回结构化 metadata：AgentScope 只记警告，不改上下文
        agent, memory, model = await build_agent(with_metadata=False)
        await agent._compress_memory_if_needed()
        untouched = await memory.get_memory(prepend_summary=False)
        check("无 metadata 时不标记任何原文", len(untouched) == 6, str(len(untouched)))
        check("无 metadata 时不写摘要", memory._compressed_summary == "", memory._compressed_summary)

    asyncio.run(scenario())


# ── 3. MCP 工具按需载入 ────────────────────────────────────────

def _register_sample(toolkit, group: str, name: str, description: str) -> None:
    def sample(query: str = "") -> str:
        return f"{name}:{query}"

    toolkit.register_tool_function(
        sample,
        group_name=group,
        func_name=name,
        func_description=description,
    )


def _schema_names(toolkit) -> set:
    return {s["function"]["name"] for s in toolkit.get_json_schemas()}


def tool_search_checks() -> None:
    print("--- 3. search_tools 与工具组 ---")
    toolkit = Toolkit()
    mail_group = ts.ensure_group(toolkit, {"serverId": "mail", "name": "邮箱"}, 0)
    calendar_group = ts.ensure_group(toolkit, {"serverId": "cal", "name": "日历"}, 1)
    _register_sample(toolkit, mail_group, "send_email", "发送邮件，支持正文与附件")
    _register_sample(toolkit, mail_group, "check_inbox", "查看收件箱列表与未读数")
    _register_sample(toolkit, calendar_group, "create_event", "创建日历日程并邀请参会人")

    names = _schema_names(toolkit)
    check("未激活工具不出现在 schema", "send_email" not in names and "create_event" not in names, str(names))
    check("工具组默认未激活", toolkit.groups[mail_group].active is False)

    result = ts.search(toolkit, "发送邮件")
    matched = {m["name"] for m in result["matches"]}
    check("中文关键词命中邮件工具", "send_email" in matched, str(result))
    check(
        "命中后只载入被检索到的那个工具（不是整组）",
        result["activatedTools"] == ["send_email"]
        and toolkit.tools["send_email"].group == ts.EQUIPPED_GROUP
        and toolkit.groups[mail_group].active is False,
        str(result["activatedTools"]),
    )
    check("激活后 schema 含工具", "send_email" in _schema_names(toolkit))
    check("同组未命中的工具仍不进 schema", "check_inbox" not in _schema_names(toolkit),
          str(_schema_names(toolkit)))
    check("未命中的分组仍未激活", toolkit.groups[calendar_group].active is False)
    check("未命中分组仍可见（供模型换词）", any(name == calendar_group for name, _ in result["inactiveGroups"]), str(result["inactiveGroups"]))

    miss = ts.search(toolkit, "量子纠缠")
    check("无命中不报错", miss["matches"] == [] and miss["activatedGroups"] == [], str(miss))
    miss_text = ts._miss_text(toolkit, miss)
    check("未命中时摊出可用分组", "日历" in miss_text and "create_event" in miss_text, miss_text)

    name = ts.install(toolkit)
    check("search_tools 注册成功", name == "search_tools" and "search_tools" in toolkit.tools)
    check("search_tools 始终可见", "search_tools" in _schema_names(toolkit))
    check("reset_equipped_tools 兼容工具已注册", "reset_equipped_tools" in toolkit.tools)
    reset_schema = next(
        s for s in toolkit.get_json_schemas()
        if s["function"]["name"] == "reset_equipped_tools"
    )
    check(
        "reset 工具按分组名暴露字段",
        calendar_group in reset_schema["function"]["parameters"]["properties"],
        str(reset_schema),
    )
    check("提示词说明不要凭空调用", "不要凭空调用" in ts.system_hint(2), ts.system_hint(2))

    check("连接器计数只认 serverId", ts.mcp_server_count({"mcp": [{"serverId": "a"}, {"name": "x"}, {"serverId": "b"}]}) == 2)
    check("缺连接器清单计 0", ts.mcp_server_count(None) == 0)


def lazy_threshold_checks() -> None:
    """D-40 第 5 项验收口径：30 个 MCP 工具只加载被检索到的 schema"""
    print("--- 3b. 阈值判定与 30 工具按需载入 ---")
    runtime = {"mcp": [{"serverId": "srv-a", "name": "甲", "tools": [{"name": f"t{i}"} for i in range(30)]}]}
    check("声明工具总数按探测缓存累加", ts.declared_tool_count(runtime) == 30,
          str(ts.declared_tool_count(runtime)))
    check("总数超过阈值才走按需载入", ts.should_lazy(runtime) is True)
    small = {"mcp": [{"serverId": "srv-a", "name": "甲", "tools": [{"name": "t1"}, {"name": "t2"}]}]}
    check("工具不多就不该让模型多绕一轮", ts.should_lazy(small) is False,
          str(ts.declared_tool_count(small)))
    check("策略关掉时永不按需", ts.should_lazy(runtime, False) is False)
    check("没探测到工具清单时按 0 计（宁可直接载入，不藏进搜不到的关键词后面）",
          ts.declared_tool_count({"mcp": [{"serverId": "a", "name": "甲"}]}) == 0)
    check("只认有 serverId 的条目", ts.declared_tool_count({"mcp": [{"tools": [{"name": "x"}]}]}) == 0)

    policy = cb.resolve(None, None)
    text = " ".join(policy.notes(1, 30, True))
    check("按需说明写出工具数与「只载入命中的那几个」",
          "30 个工具" in text and "只载入被检索到的那几个" in text, text)
    text = " ".join(policy.notes(1, 2, False))
    check("没过阈值时说明直接载入而不是照抄按需", "直接载入" in text and "未超过按需阈值" in text, text)

    toolkit = Toolkit()
    group = ts.ensure_group(toolkit, {"serverId": "bulk", "name": "批量连接器"}, 0)
    for i in range(1, 31):
        desc = "批量导出财务报表，按科目汇总" if i == 23 else f"通用演示动作 {i}"
        _register_sample(toolkit, group, f"tt_tool_{i:02d}", desc)
    ts.install(toolkit)

    hidden = {n for n in _schema_names(toolkit) if n.startswith("tt_tool_")}
    check("30 个工具默认一个都不进 schema", hidden == set(), str(hidden))

    result = ts.search(toolkit, "财务报表")
    loaded = {n for n in _schema_names(toolkit) if n.startswith("tt_tool_")}
    check("检索命中后 schema 里只多出被命中的那一个",
          loaded == {"tt_tool_23"} and result["activatedTools"] == ["tt_tool_23"],
          f"{sorted(loaded)} {result['activatedTools']}")
    check("整组没有被顺手激活", toolkit.groups[group].active is False)
    check("命中说明点明只载入了命中的", "只载入" in ts._hit_text(result) or "按检索结果载入" in ts._hit_text(result),
          ts._hit_text(result))

    broad = ts.search(toolkit, "通用演示")
    loaded = {n for n in _schema_names(toolkit) if n.startswith("tt_tool_")}
    check("宽泛命中也只按上限载入，不是整组灌进上下文",
          len(loaded) <= ts.MAX_ACTIVATED_TOOLS + 1 and len(broad["activatedTools"]) <= ts.MAX_ACTIVATED_TOOLS,
          f"{len(loaded)} {len(broad['activatedTools'])}")
    check("命中面太宽时提示换更具体的关键词",
          "更具体" in ts._hit_text(broad), ts._hit_text(broad)[-120:])
    check("内置工具不会被搬进按需载入组",
          toolkit.tools["search_tools"].group == "basic")


# ── 4. attach(lazy=True) 真接入 ───────────────────────────────

async def _lazy_attach() -> None:
    try:
        import mcp  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print(f"[SKIP] 未安装 mcp 依赖（{exc}）")
        return
    toolkit = Toolkit()
    ts.install(toolkit)
    spec = {
        "serverId": "srv-echo",
        "name": "本地回声",
        "transport": "stdio",
        "command": sys.executable,
        "args": [FIXTURE],
        "timeoutSecs": 30,
    }
    result = await mr.attach(toolkit, {"mcp": [spec]}, lazy=True)
    clients = result["clients"]
    try:
        check("lazy attach 未报注册失败", not any("注册失败" in n for n in result["notes"]), str(result["notes"]))
        check("lazy attach 说明按需载入", any("按需载入" in n for n in result["notes"]), str(result["notes"]))
        group = "mcp_srv_echo"
        check("连接器工具组已建立且未激活", group in toolkit.groups and toolkit.groups[group].active is False)
        check("lazy 工具不在 schema", "echo" not in _schema_names(toolkit), str(_schema_names(toolkit)))
        found = ts.search(toolkit, "echo")
        check("搜索命中连接器工具", any(m["name"] == "echo" for m in found["matches"]), str(found))
        check("下一轮 schema 含连接器工具", "echo" in _schema_names(toolkit))
    finally:
        await mr.close_all(clients)


# ── 5. 检查点不重复保存被压缩消息 ──────────────────────────────

def checkpoint_checks() -> None:
    print("--- 5. 检查点快照排除压缩消息 ---")
    from agentscope.memory import InMemoryMemory
    from agentscope.message import Msg

    async def scenario() -> None:
        memory = InMemoryMemory()
        await memory.add(Msg("user", "很早以前的原始消息", "user"), marks="compressed")
        await memory.add(Msg("assistant", "最近的未压缩回复", "assistant"))
        memory._compressed_summary = "这是旧消息的摘要"

        checkpoint = cr.TaskCheckpoint("selftest")
        model = cr.CheckpointedModel(lambda *a, **k: None, checkpoint)
        model.agent = types.SimpleNamespace(memory=memory)
        await model.capture()
        contents = [m["content"] for m in checkpoint.messages]
        check("快照不再包含被压缩原文", not any("很早以前的原始消息" in c for c in contents), str(contents))
        check("快照保留摘要", any("这是旧消息的摘要" in c for c in contents), str(contents))
        check("快照保留未压缩消息", any("最近的未压缩回复" in c for c in contents), str(contents))

        class OldMemory:
            async def get_memory(self):
                return [Msg("user", "旧版 memory", "user")]

        checkpoint = cr.TaskCheckpoint("selftest-old")
        model = cr.CheckpointedModel(lambda *a, **k: None, checkpoint)
        model.agent = types.SimpleNamespace(memory=OldMemory())
        await model.capture()
        check("旧版 get_memory 签名可回退", len(checkpoint.messages) == 1 and checkpoint.capture_error == "", checkpoint.capture_error)

    asyncio.run(scenario())


def main() -> None:
    policy_checks()
    compression_checks()
    compression_behavior_checks()
    tool_search_checks()
    lazy_threshold_checks()
    asyncio.run(_lazy_attach())
    checkpoint_checks()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
