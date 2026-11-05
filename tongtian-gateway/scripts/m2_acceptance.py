#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G6 验收：管理面 mTLS 与限流（M2），并写出 docs/m2-acceptance.md。

全部走真实进程与真实 HTTP：
- G5 回归作为子进程重跑（G3/G4/压测/生产模式）；
- 生产模式默认值：mTLS=proxy + 管理面限流 120 RPM（本脚本用 6 RPM / burst 2 压测）；
- 回环模拟 nginx：只有来自 127.0.0.1 且带 `X-TT-Client-Verify: SUCCESS` 的请求才被采信；
- 显式关闭实例：`TT_GATEWAY_MTLS_MODE=off` + `TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE=0`，
  启动打印告警，管理面免证书、免限流；
- 非法配置拒绝启动：错误 mTLS 模式、空/非法受信代理。

用法: python scripts/m2_acceptance.py
"""

from __future__ import annotations

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

REPORT_PATH = GATEWAY_ROOT / "docs" / "m2-acceptance.md"
PYTHON = g3.venv_python(GATEWAY_ROOT)

ADMIN_RPM = 6
ADMIN_BURST = 2
DN_RATE = "CN=g6-rate"
DN_ISOLATED = "CN=g6-isolated"
DN_AUDIT = "CN=g6-audit"
DN_WRONG_TOKEN = "CN=g6-wrong-token"
DN_VALID = "CN=g6-valid"
PROVIDER_SECRET = "sk-g6-fake-provider-secret"


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
    dn: str | None = None,
    verify: str | None = None,
    token: str = g3.ADMIN_TOKEN,
    method: str = "GET",
    body: dict | None = None,
):
    headers = {"X-TT-Admin-Token": token}
    if verify is not None:
        headers["X-TT-Client-Verify"] = verify
    if dn is not None:
        headers["X-TT-Client-DN"] = dn
    return g3.http(base, method, path, headers=headers, body=body)


def _mtls_admin(base: str, dn: str) -> dict[str, str]:
    return {
        "X-TT-Admin-Token": g3.ADMIN_TOKEN,
        "X-TT-Client-Verify": "SUCCESS",
        "X-TT-Client-DN": dn,
    }


def production_env(control_plane: str, data_dir: Path, **overrides: str) -> dict[str, str]:
    values = {
        "TT_GATEWAY_ENV": "production",
        "TT_GATEWAY_JWT_MODE": "rs256",
        "TT_GATEWAY_JWKS_URL": f"{control_plane}/.well-known/jwks.json",
        "TT_GATEWAY_CONTROL_PLANE_URL": control_plane,
        "TT_GATEWAY_SERVICE_TOKEN": g4.SERVICE_TOKEN,
        "TT_GATEWAY_ADMIN_TOKEN": g3.ADMIN_TOKEN,
        "TT_GATEWAY_ID": "gw-g6-prod",
        "TT_GATEWAY_DATA_DIR": str(data_dir),
        "TT_GATEWAY_MASTER_KEY": g3.MASTER_KEY,
        "TT_GATEWAY_MASTER_KEY_VERSION": "1",
        "TT_GATEWAY_ALLOW_PRIVATE_PROVIDERS": "1",
        "TT_GATEWAY_UPSTREAM_RETRIES": "1",
        "TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE": str(ADMIN_RPM),
        "TT_GATEWAY_ADMIN_RATE_LIMIT_BURST": str(ADMIN_BURST),
        "TT_GATEWAY_TRUSTED_PROXIES": "127.0.0.1,::1",
        # 不设置 TT_GATEWAY_MTLS_MODE：走生产默认 proxy
    }
    values.update(overrides)
    return g3._clean_env(values)


def start_gateway(
    port: int, *, control_plane: str, data_dir: Path, log_path: Path, env: dict[str, str]
) -> g3.Process:
    process = g3.Process(
        [
            g3.venv_python(GATEWAY_ROOT),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            "--no-access-log",
        ],
        GATEWAY_ROOT,
        env,
        log_path,
    )
    g3.wait_ready(f"http://127.0.0.1:{port}", "/health", "网关")
    return process


def prepare_control_plane(server_base: str, gateway_base: str) -> dict[str, str]:
    """注册用户、分配组织、保存策略；返回 admin / access 票据。"""
    status, _, body = g3.http(
        server_base,
        "POST",
        "/api/v1/auth/register",
        body={"email": g4.USER_EMAIL, "password": g4.USER_PASSWORD, "nickname": "G6 User"},
    )
    payload = body if isinstance(body, dict) else {}
    uid = str((payload.get("profile") or {}).get("uid") or "")
    g3.check("注册用户", status == 201 and bool(uid), f"{status} {body}")

    status, _, body = g3.http(
        server_base,
        "POST",
        "/api/v1/auth/login",
        body={"email": g4.ADMIN_EMAIL, "password": g4.ADMIN_PASSWORD},
    )
    admin_token = (body or {}).get("accessToken", "") if isinstance(body, dict) else ""
    g3.check("管理员登录", status == 200 and bool(admin_token), f"{status} {body}")

    status, _, body = g3.http(
        server_base,
        "PATCH",
        f"/api/v1/admin/users/{uid}",
        headers={"Authorization": f"Bearer {admin_token}"},
        body={"org": g4.TENANT},
    )
    g3.check("把用户分配到组织 acme", status == 200, f"{status} {body}")

    status, _, body = g3.http(
        server_base,
        "POST",
        "/api/v1/auth/login",
        body={"email": g4.USER_EMAIL, "password": g4.USER_PASSWORD},
    )
    access = (body or {}).get("accessToken", "") if isinstance(body, dict) else ""
    g3.check("用户登录", status == 200 and bool(access), f"{status} {body}")

    status, _, body = g3.http(
        server_base,
        "PUT",
        f"/api/v1/admin/gateway/policy/{g4.TENANT}",
        headers={"Authorization": f"Bearer {admin_token}"},
        body=g3.policy_document(gateway_base),
    )
    g3.check("保存租户策略", status == 200, f"{status} {body}")
    return {"admin": admin_token, "access": access}


def check_mtls_required(gateway_base: str) -> None:
    status, _, body = admin(gateway_base, "/gateway/v1/admin/health", verify=None, dn=None)
    code = (body or {}).get("code") if isinstance(body, dict) else None
    g3.check(
        "缺少代理校验头 → 401 MTLS_REQUIRED",
        status == 401 and code == "MTLS_REQUIRED",
        f"{status} {body}",
    )

    status, _, body = admin(
        gateway_base, "/gateway/v1/admin/health", verify="FAILED", dn="CN=forged"
    )
    code = (body or {}).get("code") if isinstance(body, dict) else None
    g3.check(
        "代理判定证书失败 → 401 MTLS_REQUIRED",
        status == 401 and code == "MTLS_REQUIRED",
        f"{status} {body}",
    )


def check_token_after_mtls(gateway_base: str) -> None:
    status, _, body = admin(
        gateway_base,
        "/gateway/v1/admin/health",
        dn=DN_WRONG_TOKEN,
        verify="SUCCESS",
        token="wrong-" + g3.ADMIN_TOKEN,
    )
    code = (body or {}).get("code") if isinstance(body, dict) else None
    g3.check(
        "mTLS 通过但管理令牌错误 → 401 AUTH_REQUIRED",
        status == 401 and code == "AUTH_REQUIRED",
        f"{status} {body}",
    )

    status, _, body = admin(
        gateway_base, "/gateway/v1/admin/health", dn=DN_VALID, verify="SUCCESS"
    )
    g3.check(
        "mTLS 通过 + 正确管理令牌 → 200",
        status == 200 and isinstance(body, dict) and body.get("ok") is True,
        f"{status} {body}",
    )


def check_rate_limit(gateway_base: str) -> None:
    statuses = []
    retry_after = ""
    for _ in range(ADMIN_BURST + 1):
        status, headers, body = admin(
            gateway_base, "/gateway/v1/admin/health", dn=DN_RATE, verify="SUCCESS"
        )
        statuses.append(status)
        if status == 429:
            retry_after = headers.get("retry-after", "")
    g3.check(
        f"同一证书 DN 第 {ADMIN_BURST + 1} 次请求 → 429 RATE_LIMITED",
        statuses[:ADMIN_BURST] == [200] * ADMIN_BURST
        and statuses[-1] == 429
        and retry_after.isdigit()
        and int(retry_after) >= 1,
        f"statuses={statuses} Retry-After={retry_after!r}",
    )

    status, _, body = admin(
        gateway_base, "/gateway/v1/admin/health", dn=DN_ISOLATED, verify="SUCCESS"
    )
    g3.check(
        "不同证书 DN 独立计桶 → 200",
        status == 200,
        f"{status} {body}",
    )


def register_provider(gateway_base: str, upstream_url: str) -> None:
    headers = _mtls_admin(gateway_base, DN_AUDIT)
    status, _, body = g3.http(
        gateway_base,
        "POST",
        "/gateway/v1/admin/providers",
        headers=headers,
        body={
            "id": "openai_compatible",
            "label": "g6-fake",
            "baseUrl": upstream_url,
            "timeoutSeconds": 30,
            "allowPrivate": True,
        },
    )
    g3.check("mTLS 下创建 provider", status in (200, 201), f"{status} {body}")

    status, _, body = g3.http(
        gateway_base,
        "POST",
        "/gateway/v1/admin/providers/openai_compatible/keys",
        headers=headers,
        body={"label": "g6-key", "secret": PROVIDER_SECRET},
    )
    g3.check("mTLS 下写入 provider 密钥", status == 201, f"{status} {body}")


def check_audit_actor(data_dir: Path) -> None:
    db_path = data_dir / "gateway.db"
    row = None
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            with sqlite3.connect(str(db_path)) as conn:
                row = conn.execute(
                    "SELECT actor FROM audit_log WHERE action='provider.create' "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
        except sqlite3.Error:
            row = None
        if row:
            break
        time.sleep(0.1)
    actor = row[0] if row else ""
    g3.check(
        "审计操作人回落到证书主体",
        actor == DN_AUDIT,
        f"actor={actor!r}",
    )


def chat_once(server_base: str, gateway_base: str, access: str) -> None:
    ticket = g4.exchange_gateway_token(server_base, access)
    status, _, body = g3.http(
        gateway_base,
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {ticket}"},
        body={
            "model": "auto",
            "messages": [{"role": "user", "content": "你好"}],
            "max_tokens": 64,
        },
    )
    content = ""
    if isinstance(body, dict):
        choices = body.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""
    g3.check("协议面仍走 RS256 并完成一次对话", status == 200 and content == "你好",
             f"{status} {body}")


def check_default_warnings(log_text: str) -> None:
    g3.check(
        "生产默认值不打印“未启用 mTLS/限流”告警",
        "未启用 mTLS" not in log_text and "未启用限流" not in log_text,
        _tail(log_text, 12),
    )


def check_off_instance(gateway_base: str, log_text: str) -> None:
    for warning in ("未启用 mTLS", "未启用限流"):
        g3.check(
            f"显式关闭时打印告警：{warning}",
            warning in log_text,
            _tail(log_text, 12),
        )

    status, _, body = admin(gateway_base, "/gateway/v1/admin/health", verify=None, dn=None)
    g3.check(
        "mTLS=off 时免证书可访问管理面",
        status == 200 and isinstance(body, dict) and body.get("ok") is True,
        f"{status} {body}",
    )

    statuses = [
        admin(gateway_base, "/gateway/v1/admin/health")[0]
        for _ in range(ADMIN_BURST + 3)
    ]
    g3.check(
        "限流=0 时连续请求不受限",
        statuses == [200] * (ADMIN_BURST + 3),
        f"statuses={statuses}",
    )


def boot_check(name: str, overrides: dict[str, str], expected: str) -> Step:
    temp = Path(tempfile.mkdtemp(prefix="tt-g6-boot-"))
    env = production_env(
        "http://127.0.0.1:9", temp / "data", **overrides
    )
    began = time.perf_counter()
    try:
        proc = subprocess.run(
            [
                g3.venv_python(GATEWAY_ROOT),
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(g3.free_port()),
            ],
            cwd=str(GATEWAY_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return Step(name, False, time.perf_counter() - began, "进程未退出")
    text = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode != 0 and expected in text
    return Step(
        name,
        ok,
        time.perf_counter() - began,
        f"退出码 {proc.returncode}；{'点名' if ok else '未点名'} {expected}",
        output=_tail(text, 12),
    )


def g5_regression() -> Step:
    began = time.perf_counter()
    try:
        proc = subprocess.run(
            [PYTHON, str(SCRIPTS_DIR / "acceptance.py")],
            cwd=str(GATEWAY_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
        )
    except subprocess.TimeoutExpired as exc:
        seconds = time.perf_counter() - began
        text = (exc.stdout or "") + (exc.stderr or "")
        return Step("G5 回归（G3/G4/压测/生产模式）", False, seconds, "超时（1800 秒）", _tail(str(text)))
    seconds = time.perf_counter() - began
    stdout = proc.stdout or ""
    passes = len(re.findall(r"^\s+OK\s", stdout, re.MULTILINE))
    fails = len(re.findall(r"^\s+(?:NG|FAIL)\s", stdout, re.MULTILINE))
    ok = proc.returncode == 0
    return Step(
        "G5 回归（G3/G4/压测/生产模式）",
        ok,
        seconds,
        f"退出码 {proc.returncode}；通过 {passes} 项，失败 {fails} 项",
        stdout + (proc.stderr or ""),
    )


def section(name: str, fn, *args) -> Step:
    """把一组 g3.check 收敛成一个步骤：任一断言失败即失败。"""
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


def run_production_defaults() -> list[Step]:
    steps: list[Step] = []
    temp = Path(tempfile.mkdtemp(prefix="tt-g6-prod-"))
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
        credentials = prepare_control_plane(server_base, gateway_base)
        gateway = start_gateway(
            gateway_port,
            control_plane=server_base,
            data_dir=temp / "gateway",
            log_path=temp / "gateway.log",
            env=production_env(server_base, temp / "gateway"),
        )
        steps.append(
            Step(
                "生产默认模式启动（mTLS=proxy + 限流 6 RPM）",
                True,
                time.perf_counter() - began,
                "控制面、策略与网关均就绪",
            )
        )
        steps.append(
            section(
                "生产默认 mTLS 生效（协议面继续 RS256）",
                lambda: (
                    check_mtls_required(gateway_base),
                    check_token_after_mtls(gateway_base),
                ),
            )
        )
        steps.append(
            section("管理面按证书 DN 限流", check_rate_limit, gateway_base)
        )
        steps.append(
            section(
                "mTLS 下登记 provider 并校验审计主体",
                lambda: (
                    register_provider(gateway_base, f"{upstream.base_url}/v1"),
                    check_audit_actor(temp / "gateway"),
                ),
            )
        )
        steps.append(
            section(
                "RS256/JWKS 对话不受管理面 mTLS 影响",
                chat_once,
                server_base,
                gateway_base,
                credentials["access"],
            )
        )
        steps.append(
            section(
                "生产默认值无 mTLS/限流关闭告警",
                check_default_warnings,
                gateway.text(),
            )
        )
    finally:
        if gateway is not None:
            gateway.stop()
        if server is not None:
            server.stop()
        upstream.stop()
    return steps


def run_off_instance() -> list[Step]:
    steps: list[Step] = []
    temp = Path(tempfile.mkdtemp(prefix="tt-g6-off-"))
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
        gateway = start_gateway(
            gateway_port,
            control_plane=server_base,
            data_dir=temp / "gateway",
            log_path=temp / "gateway.log",
            env=production_env(
                server_base,
                temp / "gateway",
                TT_GATEWAY_MTLS_MODE="off",
                TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE="0",
            ),
        )
        steps.append(
            Step(
                "显式关闭实例启动",
                True,
                time.perf_counter() - began,
                "mTLS=off、限流=0",
            )
        )
        steps.append(
            section(
                "显式关闭行为与告警",
                check_off_instance,
                gateway_base,
                gateway.text(),
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
        "# M2 验收报告（G6：管理面 mTLS 与限流）",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 运行环境：{platform.platform()} / Python {platform.python_version()} / 单副本单 worker",
        f"- 结论：**G6 {verdict}**（{passed}/{total} 个步骤通过）",
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
            "- 范围：仅管理面 `/gateway/v1/admin/*`；协议面（OpenAI/Anthropic/Gemini）继续使用 RS256 JWT。",
            "- mTLS 由反向代理终结：nginx `ssl_verify_client on` 校验证书后写入",
            "  `X-TT-Client-Verify` / `X-TT-Client-DN`；网关只采信",
            "  `TT_GATEWAY_TRUSTED_PROXIES` 内对端的这两个头，且 uvicorn 禁用 proxy headers。",
            "- 本脚本用回环地址模拟 nginx（直接注入校验头），验证的是网关侧信任边界与行为；",
            "  真实 TLS 握手、证书链与吊销需在部署环境用 nginx 联调。",
            "- 限流按证书 DN（已验证）或对端 IP 分桶，进程内令牌桶；429 携带 `Retry-After`。",
            "- 生产默认值：mTLS=proxy、管理面限流 120 RPM（本脚本压到 6 RPM / burst 2）；",
            "  可通过 `TT_GATEWAY_MTLS_MODE=off`、`TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE=0`",
            "  显式关闭，启动会打印告警。G5 回归显式关闭以保留 M1 语义。",
            "",
            "## 说明与残余",
            "",
            "- 令牌桶在进程内，仅适用于单副本；多副本前需替换为 Redis 等共享计数。",
            "- 网关必须只监听回环/内网地址，并由 nginx 唯一入口转发管理面；",
            "  否则客户端可绕过代理直接访问管理面。",
            "- 受信代理名单必须覆盖 nginx 的真实出口地址；名单为空或非法时拒绝启动。",
            "- 管理面 mTLS 不覆盖协议面；协议面鉴权仍以 RS256 JWT 与策略为准。",
            "",
        ]
    )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    steps: list[Step] = []

    print("[1/3] G5 回归（G3/G4/压测/生产模式）")
    g5_step = g5_regression()
    steps.append(g5_step)
    print(_tail(g5_step.output))

    print("\n[2/3] 生产默认 mTLS + 限流")
    for step in run_production_defaults():
        steps.append(step)
        print(("PASS " if step.ok else "FAIL ") + step.name + f" — {step.detail}")

    print("\n[3/3] 显式关闭与非法配置")
    for step in run_off_instance():
        steps.append(step)
        print(("PASS " if step.ok else "FAIL ") + step.name + f" — {step.detail}")

    for name, overrides, expected in (
        ("非法 mTLS 模式拒绝启动", {"TT_GATEWAY_MTLS_MODE": "bogus"}, "TT_GATEWAY_MTLS_MODE"),
        (
            "proxy 模式受信代理为空拒绝启动",
            {"TT_GATEWAY_MTLS_MODE": "proxy", "TT_GATEWAY_TRUSTED_PROXIES": ","},
            "TT_GATEWAY_TRUSTED_PROXIES",
        ),
        (
            "proxy 模式受信代理非法拒绝启动",
            {
                "TT_GATEWAY_MTLS_MODE": "proxy",
                "TT_GATEWAY_TRUSTED_PROXIES": "not-a-network",
            },
            "TT_GATEWAY_TRUSTED_PROXIES",
        ),
    ):
        step = boot_check(name, overrides, expected)
        steps.append(step)
        print(("PASS " if step.ok else "FAIL ") + step.name + f" — {step.detail}")

    write_report(steps)
    print(f"\n报告已写入 {REPORT_PATH}")

    failed = [step for step in steps if not step.ok]
    if failed:
        print(f"\nG6 未通过：{len(failed)} 个步骤失败")
        for step in failed:
            print(f"  - {step.name}：{step.detail}")
            excerpt = _tail(step.output)
            if excerpt:
                print(excerpt)
        return 1
    print("\nG6 验收通过：G5 回归、生产默认 mTLS + 限流、显式关闭与非法配置全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
