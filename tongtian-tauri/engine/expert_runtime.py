"""专家团队运行时（多专家与每专家模型绑定）

Rust 每次提交把注册表随 `runtime.experts` 下发：

    runtime.experts = {
        "persisted": true,
        "items": [
            {"id", "name", "description", "prompt", "modelId", "modelName",
             "skills": [str], "modelConfig"?: {...}, "modelError"?: str}
        ],
        "error"?: str,                       # 注册表损坏时的原因
    }

本模块只做三件事：

1. 归一化注册表：缺字段/格式错误都显式报错，不返回空列表假装「没有专家」；
2. 负责人专家：给出 system prompt 追加段与真正生效的模型配置；
3. 子 Agent 路由：按 id 或名称解析派发目标，模型不可用时拒绝派发而不是静默换模型。

密钥（modelConfig.apiKey）只存在于本机引擎内存，不写日志、不进事件流。
"""

from __future__ import annotations

from typing import Any

MAX_PROMPT_CHARS = 8000

_ITEM_FIELDS = ("id", "name", "description", "prompt")


def _s(value: Any) -> str:
    return str(value or "").strip()


def normalize_registry(raw: Any) -> tuple[list[dict], str | None]:
    """runtime.experts → (items, error)；任何结构问题都返回原因，不静默清空。"""
    if raw is None:
        return [], None
    if not isinstance(raw, dict):
        return [], "专家注册表格式非法（runtime.experts 不是对象）"
    err = _s(raw.get("error"))
    if err:
        return [], f"专家注册表不可读：{err}"
    items_raw = raw.get("items")
    if items_raw is None:
        return [], None
    if not isinstance(items_raw, list):
        return [], "专家注册表格式非法（items 不是数组）"

    items: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(items_raw, 1):
        if not isinstance(item, dict):
            return [], f"专家注册表第 {i} 项不是对象"
        missing = [f for f in _ITEM_FIELDS if not _s(item.get(f))]
        if missing:
            return [], f"专家注册表第 {i} 项缺少 {'/'.join(missing)}"
        eid = _s(item.get("id")).lower()
        if eid in seen:
            return [], f"专家注册表存在重复 id：{eid}"
        seen.add(eid)
        prompt = _s(item.get("prompt"))
        if len(prompt) > MAX_PROMPT_CHARS:
            prompt = prompt[:MAX_PROMPT_CHARS] + "\n…（角色提示词过长，已截断）"
        skills: list[str] = []
        for raw_skill in item.get("skills") or []:
            skill = _s(raw_skill)
            if skill and skill not in skills:
                skills.append(skill)
        model_config = item.get("modelConfig")
        items.append({
            "id": eid,
            "name": _s(item.get("name")),
            "description": _s(item.get("description")),
            "prompt": prompt,
            "modelId": _s(item.get("modelId")),
            "modelName": _s(item.get("modelName")),
            "modelConfig": dict(model_config) if isinstance(model_config, dict) else None,
            "modelError": _s(item.get("modelError")),
            "modelSecretsStripped": bool(item.get("modelSecretsStripped")),
            "skills": skills,
        })
    return items, None


def find_expert(items: list[dict], ref: Any) -> dict | None:
    """按 id（优先）或名称解析专家；大小写不敏感，名称去空格。"""
    key = _s(ref)
    if not key:
        return None
    lowered = key.lower()
    for item in items:
        if item["id"] == lowered:
            return item
    for item in items:
        if item["name"] == key or item["name"].lower() == lowered:
            return item
    return None


def available_line(items: list[dict]) -> str:
    if not items:
        return "（当前没有可用专家）"
    return "、".join(f"{e['name']}（{e['id']}）" for e in items)


def model_config_of(expert: dict | None) -> tuple[dict | None, str | None]:
    """专家绑定的模型配置；绑定存在但不可用时返回原因（调用方必须失败关闭）。"""
    if not expert:
        return None, None
    err = _s(expert.get("modelError"))
    if err:
        return None, f"专家「{expert['name']}」绑定的模型不可用：{err}"
    if expert.get("modelSecretsStripped"):
        return None, (
            f"专家「{expert['name']}」绑定的模型密钥只存在于提交请求中，"
            "从磁盘检查点恢复时无法重建（请从桌面端重新提交）"
        )
    cfg = expert.get("modelConfig")
    if isinstance(cfg, dict) and cfg:
        return cfg, None
    if _s(expert.get("modelId")):
        return None, (
            f"专家「{expert['name']}」绑定了模型 {expert['modelId']}，"
            "但本次请求没有下发可用的模型配置（密钥只在桌面端重新装配）"
        )
    return None, None


def model_note(expert: dict | None) -> str:
    if not expert:
        return ""
    if _s(expert.get("modelId")):
        name = _s(expert.get("modelName")) or expert["modelId"]
        return f"专家「{expert['name']}」绑定模型：{name}"
    return f"专家「{expert['name']}」未绑定模型，沿用本轮对话模型"


def lead_system_hint(expert: dict | None) -> str:
    """负责人专家的 system prompt 追加段；模型/工具/安全边界仍优先于角色设定。"""
    if not expert:
        return ""
    return (
        f"\n\n## 本轮负责人专家：{expert['name']}\n"
        f"职责：{expert['description']}\n"
        "角色要求：\n"
        f"{expert['prompt']}\n"
        "按该专家身份组织结论与工作方式；但工具权限、出站脱敏、审批与安全边界"
        "始终优先于角色设定，不得以专家身份为由绕过。"
    )


def child_role_prompt(expert: dict | None) -> str:
    if not expert:
        return ""
    return (
        f"\n\n## 专家身份：{expert['name']}\n"
        f"职责：{expert['description']}\n"
        "角色要求：\n"
        f"{expert['prompt']}\n"
        "你只负责被明确指派的子任务，不扩展到其他职责；仍须遵守子 Agent 的工具与安全约束。"
    )


def filter_skills(all_skills: list[dict], expert: dict | None) -> tuple[list[dict], list[str]]:
    """按专家技能白名单过滤；未启用/已删除的配置技能返回为缺失项，供调用方摊开。"""
    if not expert or not expert.get("skills"):
        return list(all_skills), []
    wanted = set(expert["skills"])
    kept = [s for s in all_skills if str(s.get("id") or "") in wanted]
    kept_ids = {str(s.get("id") or "") for s in kept}
    missing = [sid for sid in expert["skills"] if sid not in kept_ids]
    return kept, missing


def expert_notes(expert: dict | None, missing_skills: list[str] | None = None) -> list[str]:
    """子 Agent 结果里如实说明「用了哪个专家、模型从哪来、少了哪些技能」。"""
    if not expert:
        return []
    notes = [model_note(expert)]
    if missing_skills:
        notes.append(
            "专家配置的技能当前未启用或已删除：" + "、".join(missing_skills)
            + "（本次只带可用技能执行）"
        )
    return notes
