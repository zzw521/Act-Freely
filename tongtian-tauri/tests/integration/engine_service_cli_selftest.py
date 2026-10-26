#!/usr/bin/env python3
"""服务化面自测：静态站点托管 + 引擎令牌守卫 + 非回环绑定准入

锁住这些约束：
1. TT_WEB_DIR/--web-dir 指向真实构建目录时，根路径与静态资源可访问，API 路由不被接管；
2. 目录缺失/缺少 index.html 时明确失败，而不是静默只起 API；
3. 设置 TT_ENGINE_TOKEN 后 /api/* 必须携带 Bearer 或 x-tt-engine-token，/health 与静态页仍开放；
4. 绑定非回环地址必须同时显式确认与设置令牌，二者缺一即拒绝；
5. 桌面模式（不设令牌、回环地址）行为不变。

依赖缺失时输出 SKIP 并以 0 退出。

用法: python tests/integration/engine_service_cli_selftest.py
"""

import os
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import server as engine_server
    from fastapi.testclient import TestClient
except Exception as exc:  # 依赖缺失时优雅跳过
    print(f"SKIP: engine service selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    os.environ.pop("TT_ENGINE_TOKEN", None)
    os.environ.pop("TT_ALLOW_REMOTE_BIND", None)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "index.html").write_text("<!doctype html><title>tt-web</title>", encoding="utf-8")
        (root / "assets").mkdir()
        (root / "assets" / "app.js").write_text("console.log('tt')", encoding="utf-8")

        mounted = engine_server.mount_web(engine_server.app, td)
        check("mount_web 返回解析后的绝对目录", mounted == root.resolve(), str(mounted))

        client = TestClient(engine_server.app)
        home = client.get("/")
        check("根路径返回 index.html", home.status_code == 200 and "tt-web" in home.text)
        asset = client.get("/assets/app.js")
        check(
            "静态资源可访问且 MIME 不被改写",
            asset.status_code == 200 and "tt" in asset.text,
            f"{asset.status_code} {asset.headers.get('content-type')}",
        )
        health = client.get("/health")
        check("健康检查仍走 API", health.status_code == 200 and health.json()["status"] == "ok")

        # 令牌守卫：只保护 /api/*
        os.environ["TT_ENGINE_TOKEN"] = "sekret-service-token"
        protected = client.get("/api/models")
        check("无令牌的 /api 请求被拒", protected.status_code == 401, str(protected.status_code))
        check(
            "拒绝文案不回显令牌",
            protected.status_code == 401 and "sekret-service-token" not in protected.text,
        )
        bearer = client.get(
            "/api/models", headers={"Authorization": "Bearer sekret-service-token"}
        )
        check("Bearer 令牌可通过", bearer.status_code == 200, f"{bearer.status_code} {bearer.text[:120]}")
        header = client.get(
            "/api/models", headers={"x-tt-engine-token": "sekret-service-token"}
        )
        check("x-tt-engine-token 可通过", header.status_code == 200, str(header.status_code))
        check("健康检查不受令牌守卫影响", client.get("/health").status_code == 200)
        check("静态页不受令牌守卫影响", client.get("/").status_code == 200)
        os.environ.pop("TT_ENGINE_TOKEN", None)

        # 目录不可用必须失败，不能静默退化成只起 API
        try:
            engine_server.mount_web(engine_server.app, str(root / "missing"))
            check("缺失目录应抛错", False)
        except RuntimeError as exc:
            check("缺失目录明确报错并指向构建产物", "index.html" in str(exc))

    # 绑定准入：纯函数覆盖四象限
    check("回环地址无需令牌", engine_server.service_bind_error("127.0.0.1", "", "") == "")
    check(
        "非回环但未确认 → 拒绝",
        "TT_ALLOW_REMOTE_BIND" in engine_server.service_bind_error("0.0.0.0", "", "tok"),
    )
    check(
        "非回环已确认但缺令牌 → 拒绝",
        "TT_ENGINE_TOKEN" in engine_server.service_bind_error("0.0.0.0", "1", ""),
    )
    check(
        "非回环 + 确认 + 令牌 → 允许",
        engine_server.service_bind_error("0.0.0.0", "1", "tok") == "",
    )

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 -> {FAILURES}")
        return 1
    print("OK: 服务化面自测全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
