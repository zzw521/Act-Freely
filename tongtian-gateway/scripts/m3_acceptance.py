#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G7 验收：CLI 网关 API Key（M3），并写出 docs/m3-acceptance.md。

全部走真实进程与真实 HTTP：
- 先以子进程重跑 `tests/test_api_keys.py`；
- 控制面 + 网关按生产默认（RS256 + JWKS、mTLS=proxy、控制面策略）启动；
- 管理面用受信代理头签发 / 列表 / 吊销 Key；
- 同一 Key 打 OpenAI / Anthropic / Gemini 三协议面，覆盖 scopes、模型白名单、
  部门切换、用量记账、吊销即时生效与过期；
- 审计只落绑定与状态，不落明文与哈希。

用法: python scripts/m3_acceptance.py
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCRIPTS_DIR = Path(__file__).resolve().parent
GATEWAY_ROOT = SCRIPTS_DIR.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import g3_acceptance as g3  # noqa: E402
import g4_acceptance as g4  # noqa: E402
import m2_acceptance as m2  # noqa: E402

REPORT_PATH = GATEWAY_ROOT / "docs" / "m3-acceptance.md"
PYTHON = g3.venv_python(GATEWAY_ROOT)

DN = "CN=g7-acceptance"
TENANT = "acme"
DEPT = "dept-default"
SUB = "u-g7"
ALL_SCOPES = ["chat", "embeddings", "models", "messages", "gemini"]


@dataclass
class Step:
    name: str
    ok: bool
    seconds: float
    detail: str
    output: str = field(default="", repr=False)


def _tail(text: str, lines: int = 20) -> str:
    rows = [row for row in text.strip().splitlines() if row.strip()]
    return "\n".join(rows[-lines:])


def admin(
    base: str,
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
):
    return g3.http(
        base,
        method,
        path,
        headers={
            "X-TT-Admin-Token": g3.ADMIN_TOKEN,
            "X-TT-Client-Verify": "SUCCESS",
            "X-TT-Client-DN": DN,
        },
        body=body,
    )


def error_code(body) -> str:
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    if isinstance(error, dict):
        details = error.get("details") or [{}]
        return (
            error.get("tt_code")
            or (details[0].get("code") if isinstance(details[0], dict) else "")
            or error.get("code")
            or ""
        )
    return str(body.get("code") or "")


def create_key(
    base: str,
    *,
    label: str,
    scopes: list[str],
    allowlist: list[str] | None = None,
    expires_at: int | None = None,
    dept: str = DEPT,
    sub: str = SUB,
) -> dict:
    payload = {
        "label": label,
        "tenant": TENANT,
        "dept": dept,
        "sub": sub,
        "scopes": scopes,
    }
    if allowlist is not None:
        payload["modelAllowlist"] = allowlist
    if expires_at is not None:
        payload["expiresAt"] = expires_at
    status, _, body = admin(base, "/gateway/v1/admin/api-keys", method="POST", body=payload)
    key = body.get("key") if isinstance(body, dict) else None
    g3.check(
        f"签发 API Key（{label}）",
        status == 201
        and isinstance(key, dict)
        and re.fullmatch(r"ttg_[0-9a-f]{12}_[A-Za-z0-9_-]{43}", key.get("secret", ""))
        is not None,
        f"{status} {body}",
    )
    return key or {}


def chat(base: str, token: str, model: str = "auto", department: str | None = None):
    headers = {"Authorization": f"Bearer {token}"}
    if department is not None:
        headers["X-TT-Department"] = department
    return g3.http(
        base,
        "POST",
        "/v1/chat/completions",
        headers=headers,
        body={
            "model": model,
            "messages": [{"role": "user", "content": "你好"}],
            "max_tokens": 64,
        },
    )


def message_text(body) -> str:
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices") or []
    if not choices:
        return ""
    return str((choices[0].get("message") or {}).get("content") or "")


def anthropic(base: str, token: str, model: str = "auto"):
    return g3.http(
        base,
        "POST",
        "/v1/messages",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
        },
        body={
            "model": model,
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )


def gemini(base: str, token: str, model: str = "auto"):
    return g3.http(
        base,
        "POST",
        f"/v1beta/models/{model}:generateContent",
        headers={"Authorization": f"Bearer {token}"},
        body={
            "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
            "generationConfig": {"maxOutputTokens": 64},
        },
    )


def gemini_text(body) -> str:
    if not isinstance(body, dict):
        return ""
    candidates = body.get("candidates") or [{}]
    parts = (candidates[0].get("content") or {}).get("parts") or [{}]
    return str(parts[0].get("text") or "")


def check_issue_and_list(base: str) -> dict:
    key = create_key(
        base,
        label="g7-all",
        scopes=ALL_SCOPES,
        allowlist=["auto", "fake-chat", "embedding-default"],
    )
    status, _, body = admin(base, "/gateway/v1/admin/api-keys")
    keys = body.get("keys") if isinstance(body, dict) else None
    g3.check("列表返回 200 且包含新 Key", status == 200 and isinstance(keys, list), f"{status} {body}")
    row = next((item for item in keys or [] if item.get("id") == key.get("id")), None)
    g3.check(
        "列表行只含公开字段（无 secret / 哈希）",
        row is not None
        and "secret" not in row
        and "secretHash" not in row
        and "secret_hash" not in row
        and row.get("scopes") == ALL_SCOPES,
        str(row),
    )
    return key


def check_audit(data_dir: Path, secret: str) -> None:
    digest = hashlib.sha256(secret.rsplit("_", 1)[-1].encode("utf-8")).hexdigest()
    conn = sqlite3.connect(str(data_dir / "gateway.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT actor, action, object, before_json, after_json FROM audit_log "
        "WHERE action IN ('api_key.create', 'api_key.revoke') ORDER BY id"
    ).fetchall()
    conn.close()
    creates = [row for row in rows if row["action"] == "api_key.create"]
    revokes = [row for row in rows if row["action"] == "api_key.revoke"]
    text = json.dumps([dict(row) for row in rows], ensure_ascii=False)
    g3.check(
        "审计记录 api_key.create / api_key.revoke",
        bool(creates) and bool(revokes) and all(row["actor"] == DN for row in rows),
        f"creates={len(creates)} revokes={len(revokes)}",
    )
    g3.check(
        "审计不含 Key 明文与哈希",
        secret not in text and digest not in text,
        _tail(text, 4),
    )


def check_usage(gateway_base: str, key: dict, request_id: str, data_dir: Path) -> None:
    deadline = time.time() + 5
    usage = None
    while time.time() < deadline:
        conn = sqlite3.connect(str(data_dir / "gateway.db"))
        conn.row_factory = sqlite3.Row
        usage = conn.execute(
            "SELECT tenant, dept, sub, api_key_id, status, total_tokens "
            "FROM usage_events WHERE request_id=?",
            (request_id,),
        ).fetchone()
        quota = conn.execute(
            "SELECT used_tokens FROM quota_counters "
            "WHERE tenant=? AND dept=? AND sub=?",
            (TENANT, DEPT, SUB),
        ).fetchone()
        conn.close()
        if usage and quota:
            break
        time.sleep(0.1)
    g3.check(
        "用量按 Key 绑定主体记账并带 apiKeyId",
        usage is not None
        and usage["api_key_id"] == key["id"]
        and usage["tenant"] == TENANT
        and usage["dept"] == DEPT
        and usage["sub"] == SUB
        and int(usage["total_tokens"]) > 0,
        str(dict(usage) if usage else None),
    )
    g3.check(
        "额度计入绑定主体（tenant:dept:sub）",
        quota is not None and int(quota["used_tokens"]) > 0,
        str(dict(quota) if quota else None),
    )


def check_full_key(base: str, key: dict, data_dir: Path) -> None:
    token = key["secret"]
    status, headers, body = chat(base, token)
    g3.check(
        "OpenAI 对话：全 scope Key 可用",
        status == 200 and message_text(body) == "你好",
        f"{status} {body}",
    )
    request_id = headers.get("x-tt-request-id", "")
    g3.check("响应带 X-TT-Request-Id", bool(request_id), str(headers))
    if request_id:
        check_usage(base, key, request_id, data_dir)

    status, _, body = anthropic(base, token)
    text = ""
    if isinstance(body, dict):
        blocks = body.get("content") or [{}]
        text = str((blocks[0] or {}).get("text") or "")
    g3.check(
        "Anthropic messages：全 scope Key 可用",
        status == 200 and text == "你好",
        f"{status} {body}",
    )

    status, _, body = gemini(base, token)
    g3.check(
        "Gemini generateContent：全 scope Key 可用",
        status == 200 and gemini_text(body) == "你好",
        f"{status} {body}",
    )

    status, _, body = g3.http(
        base,
        "POST",
        "/v1/embeddings",
        headers={"Authorization": f"Bearer {token}"},
        body={"model": "embedding-default", "input": "你好"},
    )
    data = (body or {}).get("data") if isinstance(body, dict) else None
    g3.check(
        "OpenAI embeddings：全 scope Key 可用",
        status == 200 and isinstance(data, list) and bool(data[0].get("embedding")),
        f"{status} {str(body)[:200]}",
    )


def check_scopes(base: str) -> None:
    limited = create_key(base, label="g7-models-only", scopes=["models"])
    token = limited["secret"]

    status, _, body = chat(base, token)
    g3.check(
        "缺 chat scope → 403 FORBIDDEN（OpenAI 信封）",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )

    status, _, body = anthropic(base, token)
    g3.check(
        "缺 messages scope → 403 FORBIDDEN（Anthropic 信封）",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )

    status, _, body = gemini(base, token)
    g3.check(
        "缺 gemini scope → 403 FORBIDDEN（Gemini 信封）",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )

    status, _, body = g3.http(
        base,
        "GET",
        "/v1/models",
        headers={"Authorization": f"Bearer {token}"},
    )
    g3.check(
        "有 models scope → 模型列表可用",
        status == 200 and isinstance(body, dict) and bool(body.get("data")),
        f"{status} {body}",
    )


def check_allowlist(base: str) -> None:
    alias_key = create_key(
        base, label="g7-alias-only", scopes=["chat", "models"], allowlist=["auto"]
    )
    status, _, body = chat(base, alias_key["secret"], "auto")
    g3.check(
        "白名单含别名 auto → auto 可用",
        status == 200 and message_text(body) == "你好",
        f"{status} {body}",
    )
    status, _, body = chat(base, alias_key["secret"], "fake-chat")
    g3.check(
        "白名单不含具体模型 → 403 MODEL_NOT_ALLOWED",
        status == 403 and error_code(body) == "MODEL_NOT_ALLOWED",
        f"{status} {body}",
    )
    status, _, body = g3.http(
        base,
        "GET",
        "/v1/models",
        headers={"Authorization": f"Bearer {alias_key['secret']}"},
    )
    names = [item.get("id") for item in (body or {}).get("data") or []] if isinstance(body, dict) else []
    g3.check(
        "模型列表按 Key 白名单过滤",
        status == 200 and names and all(name in {"auto"} for name in names),
        f"{status} {names}",
    )

    concrete_key = create_key(
        base, label="g7-concrete-only", scopes=["chat"], allowlist=["fake-chat"]
    )
    status, _, body = chat(base, concrete_key["secret"], "auto")
    g3.check(
        "白名单只写具体模型 → 别名 auto 被拒",
        status == 403 and error_code(body) == "MODEL_NOT_ALLOWED",
        f"{status} {body}",
    )
    status, _, body = chat(base, concrete_key["secret"], "fake-chat")
    g3.check(
        "白名单只写具体模型 → 具体模型可用",
        status == 200 and message_text(body) == "你好",
        f"{status} {body}",
    )


def check_revoke(base: str, key: dict) -> None:
    status, _, body = admin(
        base, f"/gateway/v1/admin/api-keys/{key['id']}", method="DELETE"
    )
    revoked_at = (body or {}).get("revokedAt") if isinstance(body, dict) else None
    g3.check(
        "吊销返回 200 与 revokedAt",
        status == 200 and isinstance(revoked_at, int) and revoked_at > 0,
        f"{status} {body}",
    )

    status, _, body = chat(base, key["secret"])
    g3.check(
        "吊销后下一次请求立即 401 AUTH_REQUIRED",
        status == 401 and error_code(body) == "AUTH_REQUIRED",
        f"{status} {body}",
    )

    status, _, body = admin(
        base, f"/gateway/v1/admin/api-keys/{key['id']}", method="DELETE"
    )
    g3.check(
        "重复吊销幂等（返回原 revokedAt）",
        status == 200 and (body or {}).get("revokedAt") == revoked_at,
        f"{status} {body}",
    )

    status, _, body = admin(
        base, "/gateway/v1/admin/api-keys/missing-key", method="DELETE"
    )
    g3.check(
        "吊销不存在的 Key → 400 INVALID_REQUEST",
        status == 400 and error_code(body) == "INVALID_REQUEST",
        f"{status} {body}",
    )


def check_uniform_failure(base: str, key: dict) -> None:
    status, _, body = chat(base, key["secret"])
    invalid_message = (body or {}).get("error", {}).get("message", "") if isinstance(body, dict) else ""
    status2, _, body2 = chat(base, "ttg_000000000000_" + "A" * 43)
    invalid_message2 = (body2 or {}).get("error", {}).get("message", "") if isinstance(body2, dict) else ""
    g3.check(
        "失效 Key 与伪造 Key 返回同一错误（不可枚举）",
        status == 401 and status2 == 401 and invalid_message == invalid_message2,
        f"{invalid_message!r} vs {invalid_message2!r}",
    )


def check_expiry(base: str) -> None:
    key = create_key(
        base,
        label="g7-expiring",
        scopes=["chat"],
        expires_at=int(time.time() * 1000) + 1500,
    )
    status, _, _ = chat(base, key["secret"])
    g3.check("未过期时可用", status == 200, str(status))
    time.sleep(1.9)
    status, _, body = chat(base, key["secret"])
    g3.check(
        "过期后 401 AUTH_REQUIRED",
        status == 401 and error_code(body) == "AUTH_REQUIRED",
        f"{status} {body}",
    )


def check_department_override(base: str, key: dict) -> None:
    status, _, body = chat(base, key["secret"], department="dept-other")
    g3.check(
        "Key 不能切到绑定以外的部门 → 403 FORBIDDEN",
        status == 403 and error_code(body) == "FORBIDDEN",
        f"{status} {body}",
    )


def check_ttg_not_admin(base: str, key: dict) -> None:
    status, _, body = g3.http(
        base,
        "GET",
        "/gateway/v1/admin/health",
        headers={"X-TT-Admin-Token": key["secret"], "X-TT-Client-Verify": "SUCCESS", "X-TT-Client-DN": DN},
    )
    g3.check(
        "ttg_ Key 不能当管理令牌用",
        status == 401 and error_code(body) == "AUTH_REQUIRED",
        f"{status} {body}",
    )


def section(name: str, fn, *args) -> Step:
    before = len(g3.FAILURES)
    began = time.perf_counter()
    detail = ""
    try:
        detail = fn(*args) or ""
    except Exception as exc:  # noqa: BLE001 - 验收要把异常落进报告
        traceback.print_exc()
        g3.FAILURES.append(f"  NG  {name} — {type(exc).__name__}: {exc}")
        detail = f"{type(exc).__name__}: {exc}"
    new_failures = g3.FAILURES[before:]
    return Step(
        name,
        not new_failures,
        time.perf_counter() - began,
        "全部断言通过" if not new_failures else "；".join(new_failures),
    )


def unit_tests() -> Step:
    began = time.perf_counter()
    try:
        proc = subprocess.run(
            [PYTHON, str(GATEWAY_ROOT / "tests" / "test_api_keys.py")],
            cwd=str(GATEWAY_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        return Step(
            "tests/test_api_keys.py",
            False,
            time.perf_counter() - began,
            "超时（600 秒）",
            _tail(str(exc.stdout or "")),
        )
    stdout = proc.stdout or ""
    passes = len(re.findall(r"^\s*(?:OK|PASS|✓)\s", stdout, re.MULTILINE))
    fails = len(re.findall(r"^\s*(?:NG|FAIL|✗)\s", stdout, re.MULTILINE))
    return Step(
        "tests/test_api_keys.py",
        proc.returncode == 0 and fails == 0,
        time.perf_counter() - began,
        f"退出码 {proc.returncode}；通过 {passes} 项，失败 {fails} 项",
        stdout + (proc.stderr or ""),
    )


def run_production_flow() -> list[Step]:
    steps: list[Step] = []
    temp = Path(tempfile.mkdtemp(prefix="tt-g7-"))
    upstream = g3.FakeUpstream("openai").start()
    server_port = g3.free_port()
    gateway_port = g3.free_port()
    server_base = f"http://127.0.0.1:{server_port}"
    gateway_base = f"http://127.0.0.1:{gateway_port}"
    server = None
    gateway = None
    try:
        began = time.perf_counter()
        server = g4.start_server(
            server_port, temp / "server", temp / "server.log", gateway_base
        )
        credentials = m2.prepare_control_plane(server_base, gateway_base)
        policy = g3.policy_document(gateway_base)
        policy["departments"]["dept-default"]["allowedModels"] = [
            "auto",
            "fake-chat",
            "embedding-default",
            "fake-embed",
        ]
        status, _, body = g3.http(
            server_base,
            "PUT",
            f"/api/v1/admin/gateway/policy/{TENANT}",
            headers={"Authorization": f"Bearer {credentials['admin']}"},
            body=policy,
        )
        g3.check(
            "部门策略允许嵌入模型",
            status == 200,
            f"{status} {body}",
        )
        gateway = m2.start_gateway(
            gateway_port,
            control_plane=server_base,
            data_dir=temp / "gateway",
            log_path=temp / "gateway.log",
            env=m2.production_env(
                server_base,
                temp / "gateway",
                TT_GATEWAY_ID="gw-g7-prod",
                TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE="600",
                TT_GATEWAY_ADMIN_RATE_LIMIT_BURST="100",
            ),
        )
        m2.register_provider(gateway_base, f"{upstream.base_url}/v1")
        steps.append(
            Step(
                "生产默认模式启动（RS256 + mTLS=proxy + 控制面策略）",
                True,
                time.perf_counter() - began,
                "控制面、策略、网关与假上游均就绪",
            )
        )

        world: dict = {}

        def issue() -> dict:
            world["key"] = check_issue_and_list(gateway_base)
            return world["key"]

        steps.append(section("签发、列表与一次性明文", issue))

        def flow() -> None:
            key = world["key"]
            check_full_key(gateway_base, key, temp / "gateway")
            check_department_override(gateway_base, key)
            check_ttg_not_admin(gateway_base, key)

        steps.append(section("三协议面鉴权、用量与绑定主体", flow))
        steps.append(section("scopes 端点级校验", check_scopes, gateway_base))
        steps.append(section("模型白名单（别名与具体模型）", check_allowlist, gateway_base))

        def revoke() -> None:
            check_revoke(gateway_base, world["key"])
            check_uniform_failure(gateway_base, world["key"])

        steps.append(section("吊销即时生效与统一失败", revoke))
        steps.append(section("过期 Key 拒绝", check_expiry, gateway_base))
        steps.append(
            section(
                "审计不含明文与哈希",
                check_audit,
                temp / "gateway",
                world["key"]["secret"],
            )
        )
    finally:
        if gateway is not None:
            gateway.stop()
        if server is not None:
            server.stop()
        upstream.stop()
    return steps


def write_report(steps: list[Step]) -> None:
    total = len(steps)
    passed = sum(1 for step in steps if step.ok)
    verdict = "通过" if passed == total else "未通过"
    lines = [
        "# M3 验收报告（G7：CLI 网关 API Key）",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 运行环境：{platform.platform()} / Python {platform.python_version()} / 单副本单 worker",
        f"- 结论：**G7 {verdict}**（{passed}/{total} 个步骤通过）",
        "",
        "## 步骤",
        "",
        "| 步骤 | 结果 | 耗时 | 说明 |",
        "|---|---|---|---|",
    ]
    for step in steps:
        mark = "通过" if step.ok else "失败"
        detail = step.detail.replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {step.name} | {mark} | {step.seconds:.1f}s | {detail} |")
    lines.extend(
        [
            "",
            "## 验收口径",
            "",
            "- Key 格式 `ttg_<keyId12hex>_<secret43base64url>`，库里只存 `sha256(secret)`；",
            "  明文只在 201 响应出现一次，列表不含明文与哈希。",
            "- 管理面按生产默认启动：RS256 + JWKS、mTLS=proxy（回环模拟 nginx 写受信头）、",
            "  控制面下发策略；签发/吊销均经管理 API。",
            "- 三协议面使用同一把 Key：OpenAI chat/embeddings/models、Anthropic messages、",
            "  Gemini generateContent；缺 scope 返回各协议信封的 403 FORBIDDEN。",
            "- 模型白名单匹配请求里的 `model`（别名或具体模型），仍受部门策略约束；",
            "  模型列表按白名单过滤。",
            "- 用量按绑定主体 `tenant:dept:sub` 记账并带 `apiKeyId`；额度沿用部门配额。",
            "- 无进程内缓存：吊销与过期从下一次请求立即生效；失效 Key 与伪造 Key 返回同一",
            "  `AUTH_REQUIRED` 文案，不暴露 Key 状态。",
            "- 审计动作 `api_key.create` / `api_key.revoke`，对象为 keyId，不落明文与哈希。",
            "",
            "## 说明与残余",
            "",
            "- 单副本：吊销立即生效依赖每请求查库；多副本若引入共享缓存需补吊销传播。",
            "- 同一主体的多个 Key 共享部门配额；Key 无独立配额或限流。",
            "- scopes 与白名单不可后改，需要变更时吊销重签。",
            "- 审计操作人来自 `X-TT-Admin-Actor`，可信度取决于控制面（与 M2 相同）。",
            "- 控制面不保存 Key 明文；页面只负责一次性展示，刷新后无法找回。",
            "",
        ]
    )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    steps: list[Step] = []

    print("[1/2] tests/test_api_keys.py")
    unit = unit_tests()
    steps.append(unit)
    print(_tail(unit.output))

    print("\n[2/2] 生产模式端到端")
    for step in run_production_flow():
        steps.append(step)
        print(("PASS " if step.ok else "FAIL ") + step.name + f" — {step.detail}")

    write_report(steps)
    print(f"\n报告已写入 {REPORT_PATH}")

    failed = [step for step in steps if not step.ok]
    if failed:
        print(f"\nG7 未通过：{len(failed)} 个步骤失败")
        for step in failed:
            print(f"  - {step.name}：{step.detail}")
            excerpt = _tail(step.output)
            if excerpt:
                print(excerpt)
        return 1
    print("\nG7 验收通过：API Key 单元测试、签发/鉴权/配额/吊销/过期/审计全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
