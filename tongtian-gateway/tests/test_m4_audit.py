"""M4.3 自测：网关审计哈希链、分页、验链、角色读取与 NDJSON 导出。

用法: python tests/test_m4_audit.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import support  # noqa: E402
from tests.support import admin_headers, check, http, report, start_gateway, stop_gateway  # noqa: E402

from app.state import db  # noqa: E402

BASE_PATH = "/gateway/v1/admin/audit"
CHAIN_LEN = 64


def role_headers(role: str) -> dict[str, str]:
    return {**admin_headers(), "X-TT-Admin-Roles": role}


def audit_get(path: str = "", *, role: str = "admin", query: str = ""):
    return http("GET", f"{BASE_PATH}{path}{query}", headers=role_headers(role))


def make_audit_rows() -> None:
    status, _, body = http(
        "POST",
        "/gateway/v1/admin/providers",
        headers=admin_headers(),
        body={
            "id": "openai_compatible",
            "label": "审计测试",
            "baseUrl": "http://127.0.0.1:9/v1",
            "timeoutSeconds": 30,
            "allowPrivate": True,
        },
    )
    check("准备：创建 provider", status == 201, f"{status} {body}")

    status, _, body = http(
        "POST",
        "/gateway/v1/admin/providers/openai_compatible/keys",
        headers=admin_headers(),
        body={"label": "audit-key", "secret": "sk-audit-test"},
    )
    check("准备：添加密钥", status == 201, f"{status} {body}")

    status, _, body = http(
        "PATCH",
        "/gateway/v1/admin/quotas/acme:dept-sales:*",
        headers=role_headers("operator"),
        body={"monthlyTokens": 123456},
    )
    check("准备：运维调整额度", status == 200, f"{status} {body}")

    status, _, body = http(
        "POST",
        "/gateway/v1/admin/api-keys",
        headers=admin_headers(),
        body={
            "label": "audit-cli",
            "tenant": "acme",
            "dept": "dept-sales",
            "sub": "u-audit",
            "scopes": ["chat"],
        },
    )
    check("准备：签发 API Key", status == 201, f"{status} {body}")


def main() -> int:
    server = start_gateway()
    try:
        make_audit_rows()

        print("\n[分页：按 id 升序 + 链字段]")
        status, _, body = audit_get(query="?after=0&limit=100")
        data = body
        entries = data.get("entries") or []
        check("审计可读且 ok", status == 200 and data.get("ok") is True, f"{status} {json.dumps(data, ensure_ascii=False)[:200]}")
        check("至少 4 条审计", len(entries) >= 4, str(len(entries)))
        check(
            "条目含链字段与前后值",
            all(
                len(str(entry.get("entryHash") or "")) == CHAIN_LEN
                and len(str(entry.get("prevHash") or "")) == CHAIN_LEN
                and "before" in entry
                and "after" in entry
                for entry in entries
            ),
            json.dumps(entries[:1], ensure_ascii=False),
        )
        check(
            "id 升序且 prevHash 串联",
            all(
                entries[index]["prevHash"] == entries[index - 1]["entryHash"]
                for index in range(1, len(entries))
            ),
        )
        check(
            "角色写入审计",
            {entry["actorRole"] for entry in entries} >= {"admin", "operator"},
            str({entry["actorRole"] for entry in entries}),
        )
        check(
            "动作覆盖 provider / 额度 / key",
            {"provider.create", "provider.key.create", "quota.update", "api_key.create"}
            <= {entry["action"] for entry in entries},
            str({entry["action"] for entry in entries}),
        )
        check(
            "响应头带链头",
            data.get("headHash") == entries[-1]["entryHash"]
            and data.get("legacyRows") == 0,
            json.dumps({k: data.get(k) for k in ("headHash", "legacyRows")}),
        )

        cursor = entries[1]["id"]
        status, _, body = audit_get(query=f"?after={cursor}&limit=1")
        page = body
        check(
            "after 游标只返回更晚的行",
            status == 200
            and len(page.get("entries") or []) == 1
            and page["entries"][0]["id"] > cursor,
            str(body)[:200],
        )
        status, _, body = audit_get(query="?limit=0")
        check("limit 越界 400", status == 400, f"{status} {body}")

        print("\n[验链：正常与篡改]")
        status, _, body = audit_get("/verify")
        result = body
        check(
            "正常链 ok 且 brokenAt 为空",
            status == 200
            and result.get("ok") is True
            and result.get("brokenAt") is None
            and result.get("entries") == len(entries),
            json.dumps(result),
        )

        print("\n[导出：NDJSON]")
        status, headers, body = audit_get("/export", query="?limit=1000")
        text = body
        lines = [line for line in text.splitlines() if line.strip()]
        exported = [json.loads(line) for line in lines]
        check(
            "导出为 NDJSON",
            status == 200
            and headers.get("content-type", "").startswith("application/x-ndjson")
            and len(exported) == len(entries),
            f"{status} {headers.get('Content-Type')} {len(exported)}",
        )
        check(
            "导出末条与链头一致",
            headers.get("x-tt-audit-head") == exported[-1]["entryHash"]
            and headers.get("x-tt-audit-next") == str(exported[-1]["id"]),
            str(dict(headers)),
        )
        check(
            "导出内容不含明文密钥",
            "sk-audit-test" not in text and "ttg_" not in text,
            text[:200],
        )

        print("\n[角色：审计员可读、不可写]")
        status, _, body = audit_get(role="auditor")
        check("审计员读审计", status == 200 and body.get("ok") is True, f"{status}")
        status, _, body = audit_get("/verify", role="auditor")
        check("审计员验链", status == 200, f"{status}")
        status, _, body = audit_get("/export", role="auditor")
        check(
            "审计员导出",
            status == 200 and text.startswith("{"),
            f"{status}",
        )
        status, _, body = http(
            "POST",
            "/gateway/v1/admin/api-keys",
            headers=role_headers("auditor"),
            body={"label": "no", "tenant": "acme", "dept": "d", "sub": "u", "scopes": ["chat"]},
        )
        check("审计员写 API Key → 403", status == 403, f"{status} {body}")

        print("\n[篡改：定位到首条异常行]")
        first_id = entries[0]["id"]
        with db.tx() as conn:
            conn.execute(
                "UPDATE audit_log SET object='tampered' WHERE id=?", (first_id,)
            )
        status, _, body = audit_get("/verify")
        tampered = body
        check(
            "篡改后 HTTP 仍 200 且 ok=false",
            status == 200 and tampered.get("ok") is False,
            f"{status} {body}",
        )
        check(
            "brokenAt 指向被篡改的行",
            tampered.get("brokenAt") == first_id,
            json.dumps(tampered),
        )
    finally:
        stop_gateway(server)
    return report("M4 审计哈希链自测通过")


if __name__ == "__main__":
    raise SystemExit(main())
