"""代理环境变量解析子进程（由 engine_proxy_env_selftest.py 调用）

按 Rust `proxy::ProxySpec::env_vars()` 的三种输出口径改写环境，然后用
`httpx.AsyncClient()`（引擎 providers 的默认构造，trust_env=True）访问两个地址：
- remote：外网域名（不解析，只有真的走代理才会被本地假代理收到）
- engine：127.0.0.1 上的假引擎，模拟桌面端 ↔ 引擎的本机通信

全程只与 127.0.0.1 收发，不访问任何真实网络。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

PROXY_NAMES = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]
REMOTE_URL = "http://remote.test/a"


def apply_mode(mode: str, proxy_url: str) -> None:
    """复刻 Rust env_vars 的注入结果（父进程已通过 env 传入 ambient 代理）"""
    if mode == "system":
        # 跟随系统：Rust 不注入任何变量，子进程仅继承
        return
    if mode == "custom":
        for name in PROXY_NAMES:
            os.environ[name] = proxy_url
        # 用户忽略项 + ALWAYS_BYPASS（本地回环始终直连）
        os.environ["NO_PROXY"] = "*.corp.internal,127.0.0.1,localhost,::1"
        os.environ["no_proxy"] = os.environ["NO_PROXY"]
        return
    if mode == "disabled":
        for name in PROXY_NAMES:
            os.environ[name] = ""
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        return
    if mode == "inherited":
        return
    raise SystemExit(f"unknown mode: {mode}")


async def probe(engine_url: str) -> dict:
    import httpx

    async with httpx.AsyncClient() as client:
        result: dict = {"trust_env": bool(client.trust_env)}
        for key, url in (("remote", REMOTE_URL), ("engine", engine_url)):
            try:
                resp = await client.get(url, timeout=3.0)
                result[key] = {"status": resp.status_code, "body": resp.text}
            except Exception as exc:  # noqa: BLE001 - 需要观察任何失败形态
                result[key] = {"error": type(exc).__name__}
        return result


def main() -> None:
    mode = sys.argv[1]
    proxy_url = sys.argv[2]
    engine_url = sys.argv[3]
    apply_mode(mode, proxy_url)
    result = asyncio.run(probe(engine_url))
    result["mode"] = mode
    result["env"] = {name: os.environ.get(name) for name in PROXY_NAMES + ["NO_PROXY"]}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
