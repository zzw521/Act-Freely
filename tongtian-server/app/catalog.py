"""市场类型与分类定义（服务端是分类口径的事实来源）。"""

from __future__ import annotations

TYPE_SPECS: dict[str, dict] = {
    "skill": {
        "plural": "skills",
        "label": "技能",
        "default_icon": "🧠",
        "categories": ("效率", "文档", "数据", "开发", "运营", "内容", "其他"),
    },
    "suite": {
        "plural": "suites",
        "label": "专家套件",
        "default_icon": "🧰",
        "categories": (
            "行政",
            "HR",
            "财务",
            "律师",
            "市场",
            "销售",
            "产研",
            "运营",
            "数据分析",
            "供应链",
            "设计",
            "政务公共",
            "其他",
        ),
    },
    "mcp": {
        "plural": "mcp",
        "label": "MCP 插件",
        "default_icon": "🔌",
        "categories": ("数据", "开发", "办公", "搜索", "通讯", "其他"),
    },
}

PLURAL_TO_TYPE = {spec["plural"]: item_type for item_type, spec in TYPE_SPECS.items()}


def type_of_plural(plural: str) -> str | None:
    return PLURAL_TO_TYPE.get(plural)


def categories_of(item_type: str) -> tuple[str, ...]:
    return tuple(TYPE_SPECS[item_type]["categories"])


def normalize_category(item_type: str, raw: str | None) -> str:
    value = (raw or "").strip()
    if not value:
        return "其他"
    return value if value in categories_of(item_type) else "其他"
