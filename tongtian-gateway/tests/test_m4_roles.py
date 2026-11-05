"""M4.2 自测：管理面角色（读 / 写分离）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests import support  # noqa: E402

from app.roles import allows, parse_roles, strongest  # noqa: E402

READ_PATH = "/gateway/v1/admin/health"
PROVIDER_PATH = "/gateway/v1/admin/providers"


def role_headers(role: str) -> dict[str, str]:
    return {**support.admin_headers(), "X-TT-Admin-Roles": role}


def provider_body(provider_id: str) -> dict:
    return {
        "id": provider_id,
        "label": provider_id,
        "baseUrl": "http://127.0.0.1:9/v1",
        "timeoutSeconds": 30,
        "allowPrivate": True,
    }


def test_parse_roles() -> None:
    check = support.check
    check("角色头：逗号分隔", parse_roles({"X-TT-Admin-Roles": "auditor,operator"}) == (
        "auditor",
        "operator",
    ))
    check("角色头：空格分隔", parse_roles({"X-TT-Admin-Roles": "admin auditor"}) == (
        "admin",
        "auditor",
    ))
    check(
        "角色头：未知角色被忽略",
        parse_roles({"X-TT-Admin-Roles": "owner,admin"}) == ("admin",),
    )
    check("角色头：大小写归一", parse_roles({"x-tt-admin-roles": " OPERATOR "}) == ("operator",))
    check("角色优先级：admin 最强", strongest(("auditor", "admin", "operator")) == "admin")
    check("读：审计员允许", allows(("auditor",), "read"))
    check("写：审计员拒绝", not allows(("auditor",), "write"))
    check("写：运维允许", allows(("operator",), "write"))


def test_http_roles() -> None:
    check = support.check

    status, _, body = support.http("GET", READ_PATH, headers=role_headers("auditor"))
    check("审计员可以读健康", status == 200, f"{status} {body}")

    status, _, body = support.http(
        "POST",
        PROVIDER_PATH,
        headers=role_headers("auditor"),
        body=provider_body("openai_compatible"),
    )
    check(
        "审计员写 provider → 403 FORBIDDEN",
        status == 403 and body.get("code") == "FORBIDDEN",
        f"{status} {body}",
    )
    status, _, body = support.http("GET", PROVIDER_PATH, headers=role_headers("auditor"))
    check(
        "被拒的写没有落库",
        not any(item["configured"] for item in body.get("providers", [])),
        str(body),
    )

    status, _, body = support.http("GET", READ_PATH, headers=role_headers("operator"))
    check("运维可以读", status == 200, f"{status} {body}")
    status, _, body = support.http(
        "POST",
        PROVIDER_PATH,
        headers=role_headers("operator"),
        body=provider_body("azure_openai"),
    )
    check("运维可以写", status == 201, f"{status} {body}")

    status, _, _ = support.http("GET", READ_PATH, headers=role_headers("owner"))
    check("未知角色 → 403", status == 403, str(status))

    status, _, _ = support.http("GET", READ_PATH, headers=role_headers(""))
    check("空角色头 → 403", status == 403, str(status))

    status, _, body = support.http(
        "PATCH",
        PROVIDER_PATH + "/azure_openai",
        headers=role_headers("auditor,operator"),
        body={"label": "mixed-role"},
    )
    check("多角色按最强放行写", status == 200, f"{status} {body}")

    status, _, _ = support.http("GET", READ_PATH, headers=support.admin_headers())
    check("不带角色头按 admin 处理（读）", status == 200, str(status))
    status, _, body = support.http(
        "POST",
        PROVIDER_PATH,
        headers=support.admin_headers(),
        body=provider_body("openai_compatible"),
    )
    check("不带角色头按 admin 处理（写）", status == 201, f"{status} {body}")


def main() -> int:
    test_parse_roles()
    server = support.start_gateway()
    try:
        test_http_roles()
    finally:
        support.stop_gateway(server)
    return support.report("M4 管理角色自测通过")


if __name__ == "__main__":
    raise SystemExit(main())
