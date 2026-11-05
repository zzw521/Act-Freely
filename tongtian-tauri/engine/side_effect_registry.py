"""#164 副作用工具名单的唯一落表处（W4 领地）：四张视图同源。

原先「哪些工具算副作用」散在四处，各自一份字面量，语义还不完全一样：
- ``src/shared/step-replay.ts`` 的 ``SIDE_EFFECT_TOOLS``：步骤重放里把这一轮标成「有写盘/外发」；
- ``engine/checkpoint_runtime.py`` 的 ``_SIDE_EFFECT_TOOLS``：模型调用前快照 + 幂等台账；
- ``engine/sandbox_runtime.py`` 的 ``MUTATING_TOOLS``：L0 只读档位要拒绝的写盘面；
- ``engine/tool_approval.py`` 的 ``SENSITIVE_TOOLS``：逐工具审批的默认敏感名单。

四张表今天**成员并不完全相同**（下面如实记着差异）。这个模块不打算一次性把它们并成一张，
因为合并成员会改运行时行为（审批弹窗、L0 拒绝、台账条目都跟着变），那是各卡自己的验收范围；
它做的是「成员只有一个来源」：任何一处想加/减工具，都得回到这张表上改，
四个消费方各自派生，漂移就地被 tests/unit/workbench/step-replay.test.ts
与 tests/integration/engine_route_lanes_selftest.py 判红。

新工具怎么进来：写进 ``docs/并行实施/<卡号>.md`` 分片，由 W4 落表；车道文件里不各自开名单。
"""

from __future__ import annotations

# 重放与检查点两处今天成员一致：会改工作目录或外部状态、且能从参数判定「做过一次」的工具。
REPLAY = (
    "write_text_file",
    "insert_text_file",
    "delete_text_file",
    "generate_pptx",
    "generate_xlsx",
    "generate_docx",
    "spawn_subagents",
)
CHECKPOINT = REPLAY

# 沙箱的写盘面：与上面同源的部分之外，还要盖住电脑操控（L0 只读连点一下鼠标都算越界），
# 但不含子 Agent 派发——那是编排面上的事，不经过文件边界判定。
SANDBOX_MUTATING = (
    "write_text_file",
    "insert_text_file",
    "generate_pptx",
    "generate_xlsx",
    "generate_docx",
    "computer_action",
    "delete_text_file",
)

# 审批名单比前两张宽：命令执行、通用 shell、联网取回都必须在执行前问一次人。
# 注意运行时真正生效的门是 tool_approval.needs_approval()（非只读即需审批），
# 本表目前是名单自述 + 界面文案的来源，这条口径由 engine_office_tools_selftest 记着。
APPROVAL_SENSITIVE = (
    "write_text_file",
    "insert_text_file",
    "delete_text_file",
    "generate_pptx",
    "generate_xlsx",
    "generate_docx",
    "run_command",
    "run_shell",
    "fetch_url",
    "spawn_subagents",
)

# 名称兜底判定：未登记的 MCP / 新工具按名字里的动词算副作用（宁多记一条台账，不漏记一次写盘）。
KEYWORDS = (
    "write", "create", "delete", "remove", "move", "rename", "upload",
    "send", "execute", "insert", "append", "save", "export", "publish",
    "shell", "command", "run",
)

VIEWS = {
    "replay": REPLAY,
    "checkpoint": CHECKPOINT,
    "sandbox_mutating": SANDBOX_MUTATING,
    "approval_sensitive": APPROVAL_SENSITIVE,
}

# 已登记的成员差异（合并之前必须逐条评估，改这里等于改运行时行为）：
# - computer_action：在沙箱写盘面，不在检查点台账与重放表；
# - spawn_subagents：在检查点与重放，不在沙箱写盘面；
# - run_command / run_shell / fetch_url：只在审批名单（沙箱另有 EXECUTION_TOOLS，
#   检查点与重放靠 KEYWORDS 里的 shell/command/run 命中）。
# 只读名单本表不管，留在各消费方：checkpoint_runtime._READ_ONLY_TOOLS（四件）
# 与 tool_approval.READ_ONLY_TOOLS（六件，多 search_knowledge / read_knowledge_document）
# 语义不同——前者是「重放无害」，后者是「不必弹窗」，合并会让知识检索多弹一次确认框。
# 这两张只读表由 tests/unit/workbench/step-replay.test.ts 逐字钉住渲染层侧的同源。
DIVERGENCES_NOTED = True


def view(name: str) -> frozenset:
    """按视图名取派生集合；写错名字就抛，不做「取不到就当空」的乐观降级。"""
    try:
        return frozenset(VIEWS[name])
    except KeyError:
        raise KeyError(
            f"未登记的副作用视图 {name!r}（可选：{', '.join(VIEWS)}）"
        ) from None


def keywords() -> tuple:
    return KEYWORDS
