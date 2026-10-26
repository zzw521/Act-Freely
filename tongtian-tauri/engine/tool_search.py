"""MCP 工具按需载入（P1 工具效率）

连接器可能有几十个工具。全部注册进 function schema 会一直占着上下文，
而多数任务只会用到其中一两个。这里不删除工具，而是把它们放进**未激活的工具组**：

* 工具仍然注册，handler 仍然有效，只是默认不出现在模型看到的工具清单里；
* 模型先调用 `search_tools` 按关键词搜索；
* 命中后激活对应连接器的工具组，下一轮模型就能看到并直接调用。

刻意只对 MCP 工具生效：内置文件工具、技能、知识库、搜索等是任务的常规入口，
藏起来只会让模型多绕一步。搜索是词面匹配（ASCII 词 + 中文二元组），
不是语义检索；没命中就如实告诉模型有哪些分组，不做猜测。
"""

from __future__ import annotations

import re

MAX_QUERY_CHARS = 200
MAX_RESULTS = 12
MAX_ACTIVATED_GROUPS = 3
MAX_GROUP_SAMPLES = 3
MAX_GROUPS_IN_MISS = 8

_ASCII_TOKEN = re.compile(r"[0-9a-z][0-9a-z._+-]*")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
# AgentScope 会把 reset_equipped_tools 的工具组名做成 pydantic 字段，
# 冒号、连字符这类字符会让 create_model 直接报错，分组名必须是标识符。
_UNSAFE_GROUP_CHARS = re.compile(r"[^0-9a-zA-Z_]+")


def _tokens(text: str) -> set[str]:
    lowered = (text or "").lower()
    out = {w for w in _ASCII_TOKEN.findall(lowered) if len(w) >= 2}
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def mcp_server_count(runtime: dict | None) -> int:
    """与 mcp_runtime.normalize_servers 同一条判据：只数有 serverId 的条目。"""
    block = (runtime or {}).get("mcp") if isinstance(runtime, dict) else None
    if not isinstance(block, list):
        return 0
    return len(
        [s for s in block if isinstance(s, dict) and str(s.get("serverId") or "").strip()]
    )


def ensure_group(toolkit, spec: dict, index: int) -> str:
    """为连接器建一个未激活的工具组，返回真实组名（重复时加后缀）。"""
    server_id = str(spec.get("serverId") or "").strip()
    name = str(spec.get("name") or server_id or f"连接器 {index + 1}").strip()
    safe_id = _UNSAFE_GROUP_CHARS.sub("_", server_id).strip("_")
    base = f"mcp_{safe_id}" if safe_id else f"mcp_server_{index + 1}"
    group = base
    suffix = 2
    groups = getattr(toolkit, "groups", {}) or {}
    while group in groups:
        group = f"{base}_{suffix}"
        suffix += 1
    toolkit.create_tool_group(
        group,
        description=f"连接器「{name}」的工具（按需载入）",
        active=False,
    )
    return group


def _tool_description(tool) -> str:
    try:
        schema = tool.json_schema or {}
        return str((schema.get("function") or {}).get("description") or "")
    except Exception:  # noqa: BLE001
        return ""


def _group_label(toolkit, group_name: str) -> str:
    group = (getattr(toolkit, "groups", {}) or {}).get(group_name)
    return str(getattr(group, "description", "") or group_name)


def _score(query: str, query_tokens: set[str], tool, toolkit) -> int:
    name = str(getattr(tool, "name", "") or "").lower()
    description = _tool_description(tool).lower()
    group_text = _group_label(toolkit, getattr(tool, "group", "")).lower()
    text = f"{name} {description} {group_text}"
    score = 0
    if query and query in name:
        score += 6
    if query and query in text:
        score += 3
    hits = sum(1 for token in query_tokens if token in text)
    if hits:
        score += hits * 2 + int(10 * hits / max(1, len(query_tokens)))
    return score


def search(toolkit, query: str, limit: int = MAX_RESULTS) -> dict:
    """在 Toolkit 里按词面匹配工具；返回命中、已激活分组与说明。"""
    tools = getattr(toolkit, "tools", {}) or {}
    groups = getattr(toolkit, "groups", {}) or {}
    cleaned = " ".join(str(query or "").split())[:MAX_QUERY_CHARS]
    lowered = cleaned.lower()
    tokens = _tokens(cleaned)

    matches: list[dict] = []
    for name, tool in tools.items():
        if name in ("search_tools", "reset_equipped_tools"):
            continue
        group = str(getattr(tool, "group", "basic") or "basic")
        active = group == "basic" or bool(getattr(groups.get(group), "active", False))
        score = _score(lowered, tokens, tool, toolkit)
        if score <= 0:
            continue
        matches.append({
            "name": str(getattr(tool, "name", name) or name),
            "description": _tool_description(tool),
            "group": group,
            "groupLabel": _group_label(toolkit, group) if group != "basic" else "内置工具",
            "active": active,
            "score": score,
        })

    matches.sort(key=lambda m: (-m["score"], m["name"]))
    to_activate: list[str] = []
    for item in matches:
        if item["active"] or item["group"] in to_activate:
            continue
        to_activate.append(item["group"])
        if len(to_activate) >= MAX_ACTIVATED_GROUPS:
            break
    if to_activate:
        toolkit.update_tool_groups(to_activate, active=True)

    visible = [
        m for m in matches
        if m["active"] or m["group"] in to_activate
    ][: max(1, min(int(limit or MAX_RESULTS), MAX_RESULTS))]

    inactive_groups = [
        (name, group)
        for name, group in groups.items()
        if not getattr(group, "active", False)
    ]
    return {
        "query": cleaned,
        "matches": visible,
        "activatedGroups": to_activate,
        "inactiveGroups": inactive_groups,
        "totalMatches": len(matches),
    }


def _miss_text(toolkit, result: dict) -> str:
    tools = getattr(toolkit, "tools", {}) or {}
    inactive = result["inactiveGroups"][:MAX_GROUPS_IN_MISS]
    if not inactive:
        return (
            "没有找到匹配的工具；当前也没有未载入的连接器分组。"
            "可以换一个关键词，或直接用现有工具完成任务。"
        )
    lines = ["没有找到匹配的工具。当前可按需载入的连接器分组："]
    for group_name, group in inactive:
        sample = [
            str(getattr(tool, "name", name) or name)
            for name, tool in tools.items()
            if str(getattr(tool, "group", "")) == group_name
        ][:MAX_GROUP_SAMPLES]
        label = str(getattr(group, "description", "") or group_name)
        extra = f"，如 {'、'.join(sample)}" if sample else ""
        count = sum(
            1 for tool in tools.values()
            if str(getattr(tool, "group", "")) == group_name
        )
        lines.append(f"- {label}（{count} 个工具{extra}）")
    lines.append("请换一个更贴近工具名或用途的关键词再搜索。")
    return "\n".join(lines)


def _hit_text(result: dict) -> str:
    lines: list[str] = []
    if result["activatedGroups"]:
        lines.append(
            f"已启用 {len(result['activatedGroups'])} 个连接器分组；"
            "下列工具从下一轮起可直接调用："
        )
    else:
        lines.append("下列工具当前已经可用：")
    for item in result["matches"]:
        desc = item["description"].strip().replace("\n", " ")
        if len(desc) > 90:
            desc = desc[:90] + "…"
        suffix = f"：{desc}" if desc else ""
        lines.append(f"- {item['name']}（{item['groupLabel']}）{suffix}")
    if result["totalMatches"] > len(result["matches"]):
        lines.append(
            f"另有 {result['totalMatches'] - len(result['matches'])} 个匹配工具未列出，"
            "可用更具体的关键词缩小范围。"
        )
    return "\n".join(lines)


def install(toolkit) -> str:
    """把 search_tools（和兼容用的 reset_equipped_tools）注册进 basic 组。"""
    from agentscope.message import TextBlock
    from agentscope.tool import ToolResponse

    if "search_tools" in (getattr(toolkit, "tools", {}) or {}):
        return "search_tools"

    def search_tools(query: str):
        """搜索并按需启用连接器工具。

        当需要的工具不在当前工具列表中时，先用本工具按关键词搜索。
        命中后会启用对应连接器分组的全部工具，下一轮即可直接调用。
        搜索只匹配工具名、描述与连接器名，不是语义检索。

        Args:
            query (`str`):
                关键词，例如「发送邮件」「创建 issue」「日历」「图表」。支持中英文。
        """
        result = search(toolkit, query)
        text = _hit_text(result) if result["matches"] else _miss_text(toolkit, result)
        return ToolResponse(content=[TextBlock(type="text", text=text)])

    toolkit.register_tool_function(search_tools)
    _install_reset(toolkit, ToolResponse, TextBlock)
    return "search_tools"


def _install_reset(toolkit, ToolResponse, TextBlock) -> None:
    """兼容 AgentScope 的未激活分组错误提示。

    Toolkit 在调用未激活分组里的工具时会提示「先调用 reset_equipped_tools」；
    这个工具默认并不存在。这里补上同名实现，避免模型按提示操作时撞上「工具不存在」。
    正常路径仍然是 search_tools，reset 只是按分组名直接启用的兜底入口。
    """
    if "reset_equipped_tools" in (getattr(toolkit, "tools", {}) or {}):
        return

    def reset_equipped_tools(**kwargs):
        """按连接器分组启用或停用工具。

        Args:
            **kwargs:
                分组名 → 是否启用。分组名与说明见工具列表；只传需要改变的分组。
        """
        groups = getattr(toolkit, "groups", {}) or {}
        activate = [name for name, on in kwargs.items() if on and name in groups]
        deactivate = [
            name for name, on in kwargs.items()
            if not on and name in groups and name != "basic"
        ]
        if activate:
            toolkit.update_tool_groups(activate, active=True)
        if deactivate:
            toolkit.update_tool_groups(deactivate, active=False)
        lines = []
        if activate:
            lines.append("已启用：" + "、".join(activate))
        if deactivate:
            lines.append("已停用：" + "、".join(deactivate))
        if not lines:
            lines.append("没有改变任何工具分组。")
        return ToolResponse(content=[TextBlock(type="text", text="\n".join(lines))])

    toolkit.register_tool_function(reset_equipped_tools)


def system_hint(server_count: int) -> str:
    return (
        "\n\n## 连接器工具（按需载入）\n"
        f"本轮有 {server_count} 个连接器的工具没有直接放进上下文。"
        "需要这些工具时，先调用 search_tools(query=\"关键词\") 搜索；"
        "命中后对应连接器的工具会在下一轮变为可调用。"
        "不要凭空调用未出现在工具列表里的工具名；"
        "若误调了未载入的工具，按错误提示调用 reset_equipped_tools 启用对应分组。"
    )
