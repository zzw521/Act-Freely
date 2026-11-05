"""管理角色：平台管理员 / 网关运维 / 审计员 / 普通用户。

角色是 JWT 签发时的快照；改角色必须吊销会话，让旧令牌立即失效。
网关管理面的读写分离只认这三种管理角色。
"""

from __future__ import annotations

from typing import Any

ADMIN = "admin"
OPERATOR = "operator"
AUDITOR = "auditor"
USER = "user"

ROLE_ORDER = (USER, AUDITOR, OPERATOR, ADMIN)
MANAGEMENT_ROLES = frozenset({ADMIN, OPERATOR, AUDITOR})
WRITE_ROLES = frozenset({ADMIN, OPERATOR})

ROLE_LABELS = {
    ADMIN: "平台管理员",
    OPERATOR: "网关运维",
    AUDITOR: "审计员",
    USER: "普通用户",
}


def normalize_role(value: Any) -> str:
    role = value.strip().lower() if isinstance(value, str) else ""
    return role if role in ROLE_LABELS else ""


def role_of(user: Any) -> str:
    raw = user.get("role") if isinstance(user, dict) else None
    return normalize_role(raw) or USER


def role_label(role: str) -> str:
    return ROLE_LABELS.get(normalize_role(role), ROLE_LABELS[USER])


def is_staff(role: str) -> bool:
    return normalize_role(role) in MANAGEMENT_ROLES


def can_write_gateway(role: str) -> bool:
    return normalize_role(role) in WRITE_ROLES
