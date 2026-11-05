"""M6.5 自测：部门级管理角色（X-TT-Admin-Depts / X-TT-Admin-Tenant）。

scoped operator 只能列出/签发/轮换/吊销本部门 Key，只能看/调本部门配额与用量；
provider 写、审计读取、越界请求一律 403；admin/auditor 与未下发范围的 operator 保持全局。

用法: python tests/test_m6_admin_scope.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.support import (  # noqa: E402
    admin_headers,
    check,
    configure_openai_provider,
    http,
    report,
    start_fake_upstreams,
    start_gateway,
    stop_fake_upstreams,
    stop_gateway,
)

CHAT_PATH = "/v1/chat/completions"
BASE_KEY = {
    "label": "scope-key",
    "tenant": "acme",
    "dept": "dept-sales",
    "sub": "u-scope",
    "scopes": ["chat", "models"],
    "modelAllowlist": [],
    "expiresAt": None,
}


def scoped(depts="dept-sales", tenant="acme", role="operator") -> dict[str, str]:
    headers = admin_headers()
    headers["X-TT-Admin-Roles"] = role
    if depts is not None:
        headers["X-TT-Admin-Depts"] = depts
    if tenant is not None:
        headers["X-TT-Admin-Tenant"] = tenant
    return headers


def create_key(headers: dict[str, str], **overrides) -> tuple[int, dict]:
    payload = dict(BASE_KEY)
    payload.update(overrides)
    status, _, body = http(
        "POST", "/gateway/v1/admin/api-keys", headers=headers, body=payload
    )
    return status, body


def need_key(headers: dict[str, str], **overrides) -> dict:
    status, body = create_key(headers, **overrides)
    if status != 201:
        raise RuntimeError(f"创建 API Key 失败：{status} {body}")
    return body["key"]


def chat(token: str) -> int:
    status, _, _ = http(
        "POST",
        CHAT_PATH,
        headers={"Authorization": f"Bearer {token}"},
        body={
            "model": "basic",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    return status


def error_code(body) -> str:
    if not isinstance(body, dict):
        return ""
    if body.get("code"):
        return str(body["code"])
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or "")
    return ""


def list_keys(headers: dict[str, str]) -> list[dict]:
    status, _, body = http("GET", "/gateway/v1/admin/api-keys", headers=headers)
    if status != 200:
        raise RuntimeError(f"读取 Key 列表失败：{status} {body}")
    return body["keys"]


def test_key_scope(sales_key: dict, eng_key: dict) -> None:
    global_admin = admin_headers()
    scoped_sales = scoped("dept-sales")

    keys = list_keys(scoped_sales)
    ids = {item["id"] for item in keys}
    check(
        "列表只返回本部门 Key",
        sales_key["id"] in ids
        and eng_key["id"] not in ids
        and all(item["dept"] == "dept-sales" for item in keys),
        json.dumps(sorted(ids)),
    )

    status, body = create_key(scoped_sales, dept="dept-eng", sub="u-scope-2")
    check(
        "越界部门不能签发 Key",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {json.dumps(body, ensure_ascii=False)[:200]}",
    )
    status, body = create_key(scoped_sales, tenant="globex", dept="dept-sales")
    check(
        "越界租户不能签发 Key",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {json.dumps(body, ensure_ascii=False)[:200]}",
    )
    status, body = create_key(scoped_sales, dept="dept-sales", sub="u-scope-3")
    check("本部门可以签发 Key", status == 201, f"{status} {body}")
    extra_id = body["key"]["id"]

    status, _, body = http(
        "POST",
        f"/gateway/v1/admin/api-keys/{eng_key['id']}/rotate",
        headers=scoped_sales,
    )
    check(
        "越界 Key 不能轮换",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )
    status, _, body = http(
        "DELETE", f"/gateway/v1/admin/api-keys/{eng_key['id']}", headers=scoped_sales
    )
    check(
        "越界 Key 不能吊销",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )
    check("被拒的吊销没有影响 Key", chat(eng_key["secret"]) == 200)

    status, _, body = http(
        "POST",
        f"/gateway/v1/admin/api-keys/{extra_id}/rotate",
        headers=scoped_sales,
    )
    check(
        "本部门 Key 可以轮换",
        status == 200 and body["key"].get("secret", "").startswith("ttg_"),
        f"{status} {body}",
    )

    status, _, body = http(
        "DELETE", f"/gateway/v1/admin/api-keys/{extra_id}", headers=scoped_sales
    )
    check("本部门 Key 可以吊销", status == 200 and body["ok"] is True, f"{status} {body}")

    keys = list_keys(global_admin)
    check(
        "admin 仍能看到全部 Key",
        sales_key["id"] in {item["id"] for item in keys}
        and eng_key["id"] in {item["id"] for item in keys},
        json.dumps([item["id"] for item in keys]),
    )


def test_quota_scope() -> None:
    scoped_sales = scoped("dept-sales")
    status, _, body = http(
        "PATCH",
        "/gateway/v1/admin/quotas/acme:dept-eng:u-1",
        headers=scoped_sales,
        body={"monthlyTokens": 1000},
    )
    check(
        "越界部门不能调配额",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )
    status, _, body = http(
        "PATCH",
        "/gateway/v1/admin/quotas/acme:dept-sales:u-1",
        headers=scoped_sales,
        body={"monthlyTokens": 123456},
    )
    check(
        "本部门可以调配额",
        status == 200 and body.get("effectiveMonthlyTokens") == 123456,
        f"{status} {body}",
    )
    status, _, body = http(
        "PATCH",
        "/gateway/v1/admin/quotas/globex:dept-sales:u-1",
        headers=scoped_sales,
        body={"monthlyTokens": 1000},
    )
    check(
        "越界租户不能调配额",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )

    status, _, body = http("GET", "/gateway/v1/admin/quotas", headers=scoped_sales)
    rows = body.get("quotas", []) + body.get("overrides", [])
    check(
        "配额快照只含本部门",
        status == 200
        and rows
        and all(
            row["principal"].split(":")[1] == "dept-sales"
            and row["principal"].split(":")[0] == "acme"
            for row in rows
        ),
        json.dumps(rows, ensure_ascii=False)[:400],
    )


def test_usage_scope() -> None:
    status, _, body = http(
        "GET",
        "/gateway/v1/admin/usage?dept=dept-eng",
        headers=scoped("dept-sales"),
    )
    check(
        "越界部门不能查用量",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )
    status, _, body = http(
        "GET",
        "/gateway/v1/admin/usage?tenant=globex&dept=dept-sales",
        headers=scoped("dept-sales"),
    )
    check(
        "越界租户不能查用量",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )

    status, _, body = http("GET", "/gateway/v1/admin/usage", headers=scoped("dept-sales"))
    check(
        "单部门范围缺省自动按本部门过滤",
        status == 200
        and body.get("rows")
        and all(
            row["departmentId"] == "dept-sales" and row["tenantId"] == "acme"
            for row in body["rows"]
        ),
        f"{status} {json.dumps(body.get('rows', []), ensure_ascii=False)[:300]}",
    )

    multi = scoped("dept-sales,dept-eng")
    status, _, body = http("GET", "/gateway/v1/admin/usage", headers=multi)
    check(
        "多部门范围必须显式指定 dept",
        status == 400 and error_code(body) == "INVALID_REQUEST",
        f"{status} {body}",
    )
    status, _, body = http("GET", "/gateway/v1/admin/usage?dept=dept-eng", headers=multi)
    check(
        "多部门范围可按指定部门查询",
        status == 200
        and body.get("rows")
        and all(row["departmentId"] == "dept-eng" for row in body["rows"]),
        f"{status} {json.dumps(body.get('rows', []), ensure_ascii=False)[:300]}",
    )


def test_static_resources() -> None:
    scoped_sales = scoped("dept-sales")
    status, _, _ = http("GET", "/gateway/v1/admin/providers", headers=scoped_sales)
    check("部门范围可读 provider", status == 200)
    status, _, _ = http("GET", "/gateway/v1/admin/health", headers=scoped_sales)
    check("部门范围可读健康", status == 200)

    status, _, body = http(
        "POST",
        "/gateway/v1/admin/providers",
        headers=scoped_sales,
        body={"id": "scope-provider", "baseUrl": "https://api.example.com"},
    )
    check(
        "部门范围不能写 provider",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )
    status, _, body = http("GET", "/gateway/v1/admin/audit", headers=scoped_sales)
    check(
        "部门范围不能读审计",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )
    status, _, body = http(
        "GET", "/gateway/v1/admin/audit/export", headers=scoped_sales
    )
    check(
        "部门范围不能导出审计",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )


def test_role_semantics(eng_key: dict) -> None:
    admin_with_depts = scoped("dept-sales", role="admin")
    keys = list_keys(admin_with_depts)
    check(
        "admin 带部门头仍是全局",
        eng_key["id"] in {item["id"] for item in keys},
        json.dumps([item["id"] for item in keys]),
    )

    auditor = scoped("dept-sales", role="auditor")
    keys = list_keys(auditor)
    check(
        "auditor 带部门头仍是全局",
        eng_key["id"] in {item["id"] for item in keys},
        json.dumps([item["id"] for item in keys]),
    )
    status, _, body = http("GET", "/gateway/v1/admin/audit", headers=auditor)
    check("auditor 可读审计", status == 200, f"{status} {body}")

    no_scope = scoped(None, None)
    keys = list_keys(no_scope)
    check(
        "operator 不带部门头保持全局",
        eng_key["id"] in {item["id"] for item in keys},
        json.dumps([item["id"] for item in keys]),
    )

    invalid = scoped("*,x y,a:b", role="operator")
    keys = list_keys(invalid)
    check(
        "非法部门项被忽略后视为未下发",
        eng_key["id"] in {item["id"] for item in keys},
        json.dumps([item["id"] for item in keys]),
    )

    mixed = scoped("***,dept-sales", role="operator")
    keys = list_keys(mixed)
    ids = {item["id"] for item in keys}
    check(
        "合法项保留、非法项丢弃",
        eng_key["id"] not in ids
        and all(item["dept"] == "dept-sales" for item in keys),
        json.dumps(sorted(ids)),
    )


def main() -> int:
    upstreams = start_fake_upstreams(kinds=("openai",))
    server = None
    try:
        server = start_gateway()
        configure_openai_provider(upstreams["openai"])
        sales_key = need_key(admin_headers(), label="sales-key")
        eng_key = need_key(
            admin_headers(), label="eng-key", dept="dept-eng", sub="u-scope-eng"
        )
        check("销售部 Key 可正常调用", chat(sales_key["secret"]) == 200)
        check("研发部 Key 可正常调用", chat(eng_key["secret"]) == 200)

        test_key_scope(sales_key, eng_key)
        test_quota_scope()
        test_usage_scope()
        test_static_resources()
        test_role_semantics(eng_key)
    finally:
        if server is not None:
            stop_gateway(server)
        stop_fake_upstreams(upstreams)
    return report("M6.5 部门级管理角色自测通过")


if __name__ == "__main__":
    raise SystemExit(main())
