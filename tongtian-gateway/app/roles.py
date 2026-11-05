"""管理面角色：控制面代理通过 `X-TT-Admin-Roles` 下发。

read 动作允许 admin/operator/auditor；write 动作只允许 admin/operator。
没有该请求头时按 admin 处理：直连管理 API 的共享令牌本身即 root。
"""

from __future__ import annotations

from typing import Mapping

ADMIN = "admin"
OPERATOR = "operator"
AUDITOR = "auditor"

READ_ROLES = frozenset({ADMIN, OPERATOR, AUDITOR})
WRITE_ROLES = frozenset({ADMIN, OPERATOR})
_RANK = {AUDITOR: 1, OPERATOR: 2, ADMIN: 3}
MAX_SCOPE_ITEM = 128


def parse_roles(headers: Mapping[str, str]) -> tuple[str, ...]:
    raw = ""
    for key, value in headers.items():
        if key.lower() == "x-tt-admin-roles":
            raw = value or ""
            break
    roles: list[str] = []
    for item in raw.replace(" ", ",").split(","):
        role = item.strip().lower()
        if role in READ_ROLES and role not in roles:
            roles.append(role)
    return tuple(roles)


def strongest(roles: tuple[str, ...]) -> str:
    return max(roles, key=lambda role: _RANK.get(role, 0), default=AUDITOR)


def allows(roles: tuple[str, ...], action: str) -> bool:
    if action == "read":
        return any(role in READ_ROLES for role in roles)
    return any(role in WRITE_ROLES for role in roles)


def _scope_item(value: str) -> str:
    if not value or len(value) > MAX_SCOPE_ITEM:
        return ""
    if ":" in value or "*" in value:
        return ""
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return ""
    return value


def parse_depts(headers: Mapping[str, str]) -> tuple[str, ...] | None:
    """`X-TT-Admin-Depts`：部门范围，逗号分隔；空/全非法返回 None（全局）。"""
    raw = ""
    for key, value in headers.items():
        if key.lower() == "x-tt-admin-depts":
            raw = value or ""
            break
    depts: list[str] = []
    for item in raw.split(","):
        dept = _scope_item(item.strip())
        if dept and dept not in depts:
            depts.append(dept)
    return tuple(depts) or None


def parse_tenant(headers: Mapping[str, str]) -> str:
    """`X-TT-Admin-Tenant`：部门范围所属租户；缺失/非法返回空串（仅按部门匹配）。"""
    for key, value in headers.items():
        if key.lower() == "x-tt-admin-tenant":
            return _scope_item((value or "").strip())
    return ""
