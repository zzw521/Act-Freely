"""长期记忆工具（D-35 第二阶段：memory_search / memory_get / memory_save）

数据归属固定在桌面库：Rust 在提交任务时把「当前项目可见的活跃记忆」放进
`runtime.memory.catalog`，本模块只在这份目录上检索/读取；写入不直接碰 SQLite，
而是通过 `memory_save` 事件交回 Rust 去重、判冲突、按待审开关落库。

这样三端各守一条边：
- 引擎：拥有模型与工具，但看不到桌面库路径，也不写 MEMORY.md（桌面模式）
- Rust：拥有存储与复核，不猜模型想记什么
- 浏览器直连：没有 Rust 通道，不注册这些工具（避免假成功）
"""

import re

MAX_TOOL_LIMIT = 10
MAX_SAVE_CHARS = 1000
MAX_SUBJECT_CHARS = 80
MAX_PREDICATE_CHARS = 80
MAX_OBJECT_CHARS = 300
MIN_SCORE = 0.25
SOURCE_BOOST = {"manual": 0.08, "reflection": 0.08, "auto": 0.0}
KINDS = ("task", "fact", "preference", "decision")

# 与 server.py 的自动记忆同一套线索：写入前先挡住明显凭据
_SECRET_HINTS = (
    "sk-", "akia", "ghp_", "xoxb-", "xoxp-", "bearer ",
    "api_key=", "apikey=", "api-key=", "password=", "passwd=",
    "secret=", "token=", "-----begin",
)


def in_desktop_mode(runtime) -> bool:
    block = (runtime or {}).get("memory")
    return isinstance(block, dict) and block.get("store") == "desktop"


def _catalog(runtime) -> list[dict]:
    block = (runtime or {}).get("memory")
    if not isinstance(block, dict):
        return []
    raw = block.get("catalog")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        out.append({
            "id": str(item.get("id") or ""),
            "content": content,
            "kind": str(item.get("kind") or "fact"),
            "source": str(item.get("source") or "manual"),
            "scope": str(item.get("scope") or "global"),
            "updatedAt": str(item.get("updatedAt") or ""),
            "subject": str(item.get("subject") or ""),
            "predicate": str(item.get("predicate") or ""),
            "object": str(item.get("object") or ""),
            "taskId": str(item.get("taskId") or ""),
            "taskTitle": str(item.get("taskTitle") or ""),
        })
    return out


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return 0x3400 <= code <= 0x4DBF or 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF


def _tokens(text: str) -> set[str]:
    """与 Rust / runtime_context 同口径：ASCII 词（≥2 字符）+ 中文二元组"""
    lowered = str(text or "").lower()
    out: set[str] = set()
    word = ""
    cjk: list[str] = []

    def flush_word() -> None:
        nonlocal word
        if len(word) >= 2:
            out.add(word)
        word = ""

    def flush_cjk() -> None:
        if len(cjk) == 1:
            out.add(cjk[0])
        elif len(cjk) > 1:
            for i in range(len(cjk) - 1):
                out.add(cjk[i] + cjk[i + 1])
        cjk.clear()

    for ch in lowered:
        if _is_cjk(ch):
            flush_word()
            cjk.append(ch)
            continue
        flush_cjk()
        if ch.isascii() and (ch.isalnum() or ch in "._+-"):
            word += ch
        else:
            flush_word()
    flush_word()
    flush_cjk()
    return out


def search(runtime, query: str, limit: int = 5) -> tuple[list[dict], int]:
    """在 runtime 目录内检索，返回 (命中条目, 可见总数)。没有命中就返回空。"""
    entries = _catalog(runtime)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(limit, MAX_TOOL_LIMIT))
    query_tokens = _tokens(query)
    if not query_tokens:
        rows = sorted(entries, key=lambda e: (e["updatedAt"], e["id"]), reverse=True)
        return rows[:limit], len(entries)
    scored: list[tuple[float, dict]] = []
    for entry in entries:
        hits = query_tokens & _tokens(entry["content"])
        if not hits:
            # 结构化事实的 subject/object 也算命中面，避免正文写法与提问不一致时漏检
            hits = query_tokens & _tokens(
                f"{entry['subject']} {entry['predicate']} {entry['object']}"
            )
        if not hits:
            continue
        score = len(hits) / len(query_tokens) + SOURCE_BOOST.get(entry["source"], 0.0)
        if score < MIN_SCORE:
            continue
        scored.append((score, entry))
    scored.sort(key=lambda item: (item[0], item[1]["updatedAt"]), reverse=True)
    return [entry for _, entry in scored[:limit]], len(entries)


def get(runtime, memory_id: str) -> tuple[dict | None, str]:
    """按 id（或唯一前缀）读取一条记忆；读不到时返回原因，不编造内容。"""
    target = str(memory_id or "").strip()
    if not target:
        return None, "记忆标识为空"
    entries = _catalog(runtime)
    exact = [e for e in entries if e["id"] == target]
    if exact:
        return exact[0], ""
    prefix = [e for e in entries if e["id"].startswith(target)]
    if len(prefix) == 1:
        return prefix[0], ""
    if len(prefix) > 1:
        preview = "、".join(e["id"] for e in prefix[:5])
        return None, f"标识前缀「{target}」匹配多条记忆（{preview}…），请给完整 id"
    return None, (
        f"当前项目可见的活跃记忆里没有「{target}」"
        "（待审、已归档或属于其他项目的记忆不在检索面内）"
    )


def save_payload(content, kind: str = "fact", subject: str = "",
                 predicate: str = "", object: str = "") -> tuple[dict | None, str]:
    """校验并规格化一条待写入记忆；错误直接返回给模型，不产生半截写入。"""
    text = " ".join(str(content or "").split())
    if not text:
        return None, "MEMORY_CONTENT_EMPTY: 记忆内容不能为空"
    if len(text) > MAX_SAVE_CHARS:
        return None, f"MEMORY_CONTENT_TOO_LONG: 记忆最多 {MAX_SAVE_CHARS} 字（当前 {len(text)} 字）"
    lowered = text.lower()
    if any(hint in lowered for hint in _SECRET_HINTS):
        return None, "MEMORY_SECRET_BLOCKED: 内容疑似包含凭据，已拒绝写入记忆"
    kind = str(kind or "fact").strip()
    if kind not in KINDS:
        return None, f"MEMORY_INVALID_KIND: 记忆类型「{kind}」非法，仅支持 {' / '.join(KINDS)}"
    fields = {
        "subject": (" ".join(str(subject or "").split()), MAX_SUBJECT_CHARS, "主体"),
        "predicate": (" ".join(str(predicate or "").split()), MAX_PREDICATE_CHARS, "属性"),
        "object": (" ".join(str(object or "").split()), MAX_OBJECT_CHARS, "取值"),
    }
    normalized: dict[str, str] = {}
    for key, (value, limit, label) in fields.items():
        if len(value) > limit:
            return None, f"MEMORY_INVALID_FACT: 事实字段「{label}」最多 {limit} 字"
        normalized[key] = value
    return {
        "content": text,
        "kind": kind,
        "source": "auto",
        # 工具写入是模型明确判断后的动作，置信度高于规则抽取、低于人工录入
        "confidence": 0.8,
        **normalized,
    }, ""


def system_hint(runtime) -> str:
    review = bool(((runtime or {}).get("memory") or {}).get("review"))
    review_note = "当前开启「写入前待审」，memory_save 的内容需用户确认后才会进入后续召回。" if review else ""
    return (
        "\n\n长期记忆：本轮已按目标注入相关条目。需要更多背景时调用 memory_search，"
        "已知记忆 id 时调用 memory_get；重要且可复用的事实可用 memory_save 主动保存。"
        "memory_save 会去重、拒绝疑似凭据，不能保存运行时错误堆栈或临时中间结果。"
        + review_note
    )


def install(toolkit, runtime, emit, secret_check=None) -> bool:
    """注册三个记忆工具；非桌面通道返回 False，不制造「有工具但写不进去」的假象。"""
    block = (runtime or {}).get("memory")
    if not isinstance(block, dict) or block.get("store") != "desktop":
        return False
    if not block.get("tooling"):
        return False
    from agentscope.tool import ToolResponse
    from agentscope.message import TextBlock

    def _text(value: str):
        return ToolResponse(content=[TextBlock(type="text", text=value)])

    def _format(entry: dict) -> str:
        head = (
            f"[记忆:{entry['id'] or '?'}]（{entry['kind']} · {entry['source']} · "
            f"{entry['updatedAt'] or '时间未知'}）{entry['content']}"
        )
        if entry["subject"] or entry["predicate"] or entry["object"]:
            head += (
                f"\n  事实：{entry['subject']} / {entry['predicate']} / {entry['object']}"
            )
        if entry["taskTitle"]:
            head += f"\n  来源任务：{entry['taskTitle']}"
        return head

    def memory_search(query: str, limit: int = 5):
        """检索当前项目可见的长期记忆（按目标词面匹配，最多 10 条）。

        只在需要补充背景时调用；本轮 system prompt 已注入最相关的条目，
        不要为了「确认一下」反复检索同一个问题。

        Args:
            query (`str`):
                检索词，使用任务里的关键实体、术语或问题（不要整段粘贴用户原话）。
            limit (`int`, optional):
                返回条数，1–10，缺省 5。
        """
        rows, total = search(runtime, query, limit)
        if not rows:
            return _text(
                f"没有找到与「{str(query or '').strip()}」相关的活跃记忆"
                f"（当前项目可见 {total} 条）。不要臆测记忆内容，也不要重复检索同一问题。"
            )
        lines = [f"找到 {len(rows)} 条（当前项目可见 {total} 条活跃记忆）："]
        lines.extend(_format(row) for row in rows)
        return _text("\n".join(lines))

    def memory_get(memory_id: str):
        """按 id 读取一条长期记忆的完整内容与来源任务。

        Args:
            memory_id (`str`):
                记忆 id，形如 mem-xxxx；允许给唯一前缀。
        """
        entry, reason = get(runtime, memory_id)
        if entry is None:
            return _text(f"错误：{reason}")
        return _text(_format(entry))

    def memory_save(content: str, kind: str = "fact", subject: str = "",
                    predicate: str = "", object: str = ""):
        """把一条重要、可复用的事实写入长期记忆（由桌面端去重与复核）。

        适合：稳定的项目约定、客户偏好、已确认的决策。
        不适合：临时中间结果、工具报错、寒暄、本轮任务摘要（收尾会自动记录）。

        Args:
            content (`str`):
                一句完整、可独立理解的事实，最多 1000 字。
            kind (`str`, optional):
                task / fact / preference / decision，缺省 fact。
            subject (`str`, optional):
                事实主体（项目、客户、人、系统），最多 80 字。
            predicate (`str`, optional):
                主体属性或关系，最多 80 字。
            object (`str`, optional):
                属性取值，最多 300 字。
        """
        payload, reason = save_payload(content, kind, subject, predicate, object)
        if payload is None:
            return _text(f"错误：{reason}")
        if secret_check is not None and secret_check(payload["content"]):
            return _text("错误：MEMORY_SECRET_BLOCKED: 内容疑似包含凭据，已拒绝写入记忆")
        if emit is None:
            return _text(
                "错误：MEMORY_STORE_UNAVAILABLE: 当前通道没有桌面记忆库写入通道，"
                "不能用 memory_save 保存；请把结论留在回复里。"
            )
        emit("memory_save", {**payload, "tool": "memory_save"})
        review = bool(block.get("review"))
        suffix = (
            "已提交为待审记忆，用户确认后才会参与后续召回。"
            if review
            else "已提交桌面记忆库，去重/冲突检查后写入。"
        )
        return _text(f"{suffix}\n内容：{payload['content']}")

    toolkit.register_tool_function(memory_search)
    toolkit.register_tool_function(memory_get)
    toolkit.register_tool_function(memory_save)
    return True
