#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""G5 合并验收：G3 + G4 + 压测冒烟 + 生产模式启动，并写出 docs/m1-acceptance.md。

G3/G4/压测都作为子进程运行（它们各自起真实服务），生产模式检查在本进程内编排：
控制面（RS256/JWKS）+ 网关（TT_GATEWAY_ENV=production）+ 假上游，完成一次真实对话，
同时验证 /docs 关闭、错误管理令牌被拒、缺主密钥时拒绝启动、日志无密钥明文。

用法: python scripts/acceptance.py
"""

from __future__ import annotations

import platform
import re
import subprocess
import sys
import tempfile
import time
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

REPORT_PATH = GATEWAY_ROOT / "docs" / "m1-acceptance.md"
PYTHON = g3.venv_python(GATEWAY_ROOT)


@dataclass
class Step:
    name: str
    ok: bool
    seconds: float
    detail: str
    output: str = field(default="", repr=False)


def _tail(text: str, lines: int = 12) -> str:
    rows = [row for row in text.strip().splitlines() if row.strip()]
    return "\n".join(rows[-lines:])


def run_script(
    name: str, args: list[str], *, pass_pattern: str = r"^\s+OK\s"
) -> Step:
    began = time.perf_counter()
    try:
        proc = subprocess.run(
            [PYTHON, *args],
            cwd=str(GATEWAY_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        seconds = time.perf_counter() - began
        text = (exc.stdout or "") + (exc.stderr or "")
        return Step(name, False, seconds, "超时（900 秒）", str(text))
    seconds = time.perf_counter() - began
    stdout = proc.stdout or ""
    passes = len(re.findall(pass_pattern, stdout, re.MULTILINE))
    fails = len(re.findall(r"^\s+(?:NG|FAIL)\s", stdout, re.MULTILINE))
    ok = proc.returncode == 0
    detail = f"退出码 {proc.returncode}；通过 {passes} 项，失败 {fails} 项"
    return Step(name, ok, seconds, detail, output=stdout + (proc.stderr or ""))


def _production_env(control_plane: str, data_dir: Path) -> dict[str, str]:
    return g3._clean_env(
        {
            "TT_GATEWAY_ENV": "production",
            "TT_GATEWAY_JWT_MODE": "rs256",
            "TT_GATEWAY_JWKS_URL": f"{control_plane}/.well-known/jwks.json",
            "TT_GATEWAY_CONTROL_PLANE_URL": control_plane,
            "TT_GATEWAY_SERVICE_TOKEN": g4.SERVICE_TOKEN,
            "TT_GATEWAY_ADMIN_TOKEN": g4.ADMIN_TOKEN,
            "TT_GATEWAY_ID": "gw-g5-prod",
            "TT_GATEWAY_DATA_DIR": str(data_dir),
            "TT_GATEWAY_MASTER_KEY": g3.MASTER_KEY,
            "TT_GATEWAY_MASTER_KEY_VERSION": "1",
            "TT_GATEWAY_ALLOW_PRIVATE_PROVIDERS": "1",
            "TT_GATEWAY_UPSTREAM_RETRIES": "1",
            # G5 保持 M1 语义：管理面免 mTLS、免限流（启动会打印两条告警）。
            # M2 默认值（mTLS=proxy、RPM=120）由 G6 单独验收。
            "TT_GATEWAY_MTLS_MODE": "off",
            "TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE": "0",
        }
    )


def start_production_gateway(
    port: int, *, control_plane: str, data_dir: Path, log_path: Path
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
        _production_env(control_plane, data_dir),
        log_path,
    )
    g3.wait_ready(f"http://127.0.0.1:{port}", "/health", "生产模式网关")
    return process


def production_checks() -> list[Step]:
    """真实控制面 + 生产模式网关 + 假上游；所有断言逐条返回。"""
    steps: list[Step] = []
    temp = Path(tempfile.mkdtemp(prefix="tt-g5-prod-"))
    upstream = g3.FakeUpstream("openai").start()
    server_port = g3.free_port()
    gateway_port = g3.free_port()
    server_base = f"http://127.0.0.1:{server_port}"
    gateway_base = f"http://127.0.0.1:{gateway_port}"
    server = None
    gateway = None
    try:
        began = time.perf_counter()
        server = g4.start_server(server_port, temp / "server", temp / "server.log", gateway_base)
        gateway = start_production_gateway(
            gateway_port,
            control_plane=server_base,
            data_dir=temp / "gateway",
            log_path=temp / "gateway.log",
        )
        steps.append(
            Step(
                "生产模式启动（rs256 + JWKS + master key）",
                True,
                time.perf_counter() - began,
                "控制面与网关均就绪",
            )
        )

        for path in ("/docs", "/redoc", "/openapi.json"):
            status, _, _ = g3.http(gateway_base, "GET", path)
            steps.append(
                Step(f"生产模式关闭 {path}", status == 404, 0.0, f"HTTP {status}")
            )

        before = len(g3.FAILURES)
        began = time.perf_counter()
        _, _, access = g4.setup_control_plane(
            server_base, gateway_base, f"{upstream.base_url}/v1"
        )
        new_failures = g3.FAILURES[before:]
        steps.append(
            Step(
                "控制面：注册/组织/策略/provider 与密钥（经管理代理）",
                not new_failures,
                time.perf_counter() - began,
                "全部断言通过" if not new_failures else "；".join(new_failures),
            )
        )

        began = time.perf_counter()
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
        steps.append(
            Step(
                "生产模式 RSA/JWKS 令牌完成一次对话",
                status == 200 and content == "你好",
                time.perf_counter() - began,
                f"HTTP {status}，content={content!r}",
            )
        )

        status, _, _ = g3.http(
            gateway_base,
            "GET",
            "/gateway/v1/admin/health",
            headers={"X-TT-Admin-Token": "wrong-" + g4.ADMIN_TOKEN},
        )
        steps.append(
            Step("生产模式错误管理令牌被拒", status == 401, 0.0, f"HTTP {status}")
        )

        logs = gateway.text() + server.text()
        candidates = {
            "管理令牌": g4.ADMIN_TOKEN,
            "服务令牌": g4.SERVICE_TOKEN,
            "主密钥": g3.MASTER_KEY,
            "provider 密钥": g4.PROVIDER_SECRET,
        }
        leaked = [name for name, value in candidates.items() if value and value in logs]
        steps.append(
            Step(
                "生产模式日志无管理令牌/服务令牌/主密钥/provider 密钥",
                not leaked,
                0.0,
                f"泄漏：{'、'.join(leaked)}" if leaked else "未发现明文",
            )
        )
    finally:
        if gateway is not None:
            gateway.stop()
        if server is not None:
            server.stop()
        upstream.stop()
    return steps


def missing_var_check() -> Step:
    """生产环境缺主密钥：进程必须退出、日志点名变量。"""
    temp = Path(tempfile.mkdtemp(prefix="tt-g5-boot-"))
    env = g3._clean_env(
        {
            "TT_GATEWAY_ENV": "production",
            "TT_GATEWAY_JWT_MODE": "rs256",
            "TT_GATEWAY_JWKS_URL": "http://127.0.0.1:9/.well-known/jwks.json",
            "TT_GATEWAY_ADMIN_TOKEN": "g5-admin-token-" + "x" * 16,
            "TT_GATEWAY_DATA_DIR": str(temp / "data"),
            # 故意不配 TT_GATEWAY_MASTER_KEY
        }
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
        return Step("生产模式缺主密钥拒绝启动", False, time.perf_counter() - began, "进程未退出")
    text = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode != 0 and "无法启动" in text and "TT_GATEWAY_MASTER_KEY" in text
    return Step(
        "生产模式缺主密钥拒绝启动",
        ok,
        time.perf_counter() - began,
        f"退出码 {proc.returncode}；错误信息{'包含' if ok else '不含'}变量名",
        output=text,
    )


def _loadtest_metrics(output: str) -> list[str]:
    rows = []
    for line in output.splitlines():
        row = line.strip()
        if row.startswith(("非流式:", "流式:", "非流式 ", "流式 ")):
            rows.append(row)
    return rows


def write_report(steps: list[Step], loadtest_output: str) -> None:
    total = len(steps)
    passed = sum(1 for step in steps if step.ok)
    verdict = "通过" if passed == total else "未通过"
    lines = [
        "# M1 验收报告（G5）",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 运行环境：{platform.platform()} / Python {platform.python_version()} / 单副本单 worker",
        f"- 结论：**G5 {verdict}**（{passed}/{total} 个步骤通过）",
        "",
        "## 步骤",
        "",
        "| 步骤 | 结果 | 耗时 | 说明 |",
        "|---|---|---|---|",
    ]
    for step in steps:
        mark = "通过" if step.ok else "失败"
        detail = step.detail.replace("|", "\\|")
        lines.append(f"| {step.name} | {mark} | {step.seconds:.1f}s | {detail} |")
    metrics = _loadtest_metrics(loadtest_output)
    lines.extend(["", "## 压测快照（G5 冒烟参数）", ""])
    if metrics:
        lines.append("```text")
        lines.extend(metrics)
        lines.append("```")
    else:
        lines.append("压测未产出统计行，见下方失败输出。")
    lines.extend(
        [
            "",
            "## 验收口径",
            "",
            "- G0–G4 回归：G3（控制面 + 网关 + 假上游，RS256/JWKS/部门/换 kid）与",
            "  G4（用量上报、幂等、合并视图、配额 429）作为子进程重跑，退出码为 0 才算通过。",
            "- 生产模式：`TT_GATEWAY_ENV=production` 下必须用 RS256 + JWKS 完成一次真实对话；",
            "  `/docs`、`/redoc`、`/openapi.json` 关闭；管理面拒绝错误令牌；",
            "  缺 `TT_GATEWAY_MASTER_KEY` 时进程拒绝启动并点名变量；日志不含任何密钥明文。",
            "- 压测冒烟：`scripts/loadtest.py --concurrency 10 --requests 40`，验证零错误、",
            "  账本与上游请求数一致、日志无 SQLite 锁与未处理异常（完整门槛见 deploy.md §11）。",
            "",
            "## 说明与残余",
            "",
            "- 假上游固定 20ms 延迟，压测数字用于回归而非容量承诺；生产容量需用真实 provider 重跑。",
            "- 生产模式用例的 JWKS 走 `http://127.0.0.1`（启动自检允许回环例外）；线上必须是 https。",
            "- M1 仍是单副本 SQLite；多副本前需先替换配额与用量缓冲的实现（见 M1-PLAN §11）。",
            "",
        ]
    )
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    steps: list[Step] = []

    print("[1/4] G3 验收（控制面 + 网关 + 假上游）")
    g3_step = run_script("G3 验收", [str(SCRIPTS_DIR / "g3_acceptance.py")])
    steps.append(g3_step)
    print(_tail(g3_step.output))

    print("\n[2/4] G4 验收（用量与运营）")
    g4_step = run_script("G4 验收", [str(SCRIPTS_DIR / "g4_acceptance.py")])
    steps.append(g4_step)
    print(_tail(g4_step.output))

    print("\n[3/4] 压测冒烟")
    load_step = run_script(
        "压测冒烟（10 并发 / 40 请求）",
        [str(SCRIPTS_DIR / "loadtest.py"), "--concurrency", "10", "--requests", "40"],
        pass_pattern=r"^\s+OK\s",
    )
    steps.append(load_step)
    print(_tail(load_step.output))

    print("\n[4/4] 生产模式启动与拒绝启动")
    for step in production_checks():
        steps.append(step)
        print(("PASS " if step.ok else "FAIL ") + step.name + f" — {step.detail}")
    boot = missing_var_check()
    steps.append(boot)
    print(("PASS " if boot.ok else "FAIL ") + boot.name + f" — {boot.detail}")

    write_report(steps, load_step.output)
    print(f"\n报告已写入 {REPORT_PATH}")

    failed = [step for step in steps if not step.ok]
    if failed:
        print(f"\nG5 未通过：{len(failed)} 个步骤失败")
        for step in failed:
            print(f"  - {step.name}：{step.detail}")
            excerpt = _tail(step.output, 20)
            if excerpt:
                print(excerpt)
        return 1
    print("\nG5 验收通过：G3/G4 回归、压测冒烟与生产模式启动全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
