"""网络代理生效链路自测（Rust 注入的代理变量 → 引擎 httpx 实际路由）

链路：设置页保存 proxyMode/proxyUrl/proxyBypass → Rust `proxy::publish` →
`bridge::agent::start_workspace` spawn 引擎时注入 `env_vars()` →
引擎内 `httpx.AsyncClient()`（trust_env 默认 True）解析。

本脚本验证最后一环的真实行为：在 127.0.0.1 起一个假代理与一个假引擎，
观察 httpx 到底把请求发给了谁。全程不访问任何真实网络、不读写业务数据。

运行：PYTHONIOENCODING=utf-8 python tests/integration/engine_proxy_env_selftest.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

CHILD = Path(__file__).with_name("_proxy_env_child.py")
PROXY_NAMES = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]

PASS, FAIL = 0, 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label} {detail}")


class Recorder:
    """127.0.0.1 上的最小 HTTP 服务：记录收到的请求行，固定回一个响应"""

    def __init__(self, response: bytes) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self.lines: list[str] = []
        self._response = response
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            with conn:
                data = conn.recv(4096)
                first = data.decode("latin1").splitlines()[0] if data else ""
                if first:
                    self.lines.append(first)
                conn.sendall(self._response)

    def close(self) -> None:
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass


def run_child(mode: str, proxy_url: str, engine_url: str, ambient_proxy: str | None) -> dict:
    """干净环境下启动子进程；ambient_proxy 模拟「引擎继承了外部代理变量」"""
    env: dict[str, str] = {}
    # 只保留运行 python 必需的系统变量，避免把开发者本机代理带进测试
    for key in ("SystemRoot", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC"):
        if key in os.environ:
            env[key] = os.environ[key]
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    if ambient_proxy:
        for name in PROXY_NAMES:
            env[name] = ambient_proxy
    out = subprocess.run(
        [sys.executable, str(CHILD), mode, proxy_url, engine_url],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    if out.returncode != 0:
        raise RuntimeError(f"子进程({mode})失败: {out.stderr.strip()[:500]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def main() -> int:
    print("== 引擎侧代理解析（httpx 实际路由，全部走 127.0.0.1） ==")

    proxy = Recorder(b"HTTP/1.1 502 Bad Gateway\r\ncontent-length: 0\r\nconnection: close\r\n\r\n")
    engine = Recorder(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
    proxy_url = f"http://127.0.0.1:{proxy.port}"
    engine_url = f"http://127.0.0.1:{engine.port}/health"

    try:
        # 1) 跟随系统：不注入任何代理变量 → 请求不得经过假代理，本机引擎直连
        proxy.lines.clear()
        engine.lines.clear()
        r = run_child("system", proxy_url, engine_url, ambient_proxy=None)
        check("跟随系统：没有代理变量时不产生代理路由", proxy.lines == [], str(proxy.lines))
        check("跟随系统：引擎地址仍直连", engine.lines == ["GET /health HTTP/1.1"], str(engine.lines))
        # 外网探测必须是「真结果」：要么连不上（DNS/网络错误），要么被本机网络
        # 拦成错误码（部分企业网络会把未知域名应答成 5xx）。两种都算如实；
        # 唯独不能是成功应答 —— 那说明请求被我们的假代理接管了（配合上一条 proxy.lines==[] 双重锁）。
        remote = r["remote"]
        check(
            "跟随系统：外网探测未被假代理接管，也没被伪装成正常",
            "error" in remote or int(remote.get("status") or 0) >= 400,
            str(remote),
        )
        check("跟随系统：httpx trust_env 生效", r["trust_env"] is True, str(r["trust_env"]))

        # 2) 继承外部代理：引擎没被注入但环境里本来就有代理
        proxy.lines.clear()
        engine.lines.clear()
        r = run_child("inherited", proxy_url, engine_url, ambient_proxy=proxy_url)
        check(
            "继承环境代理：外网请求被假代理收到（绝对 URI 请求行）",
            any(line.startswith("GET http://remote.test/") for line in proxy.lines),
            str(proxy.lines),
        )

        # 3) 自定义：注入代理地址 + 回环 bypass
        proxy.lines.clear()
        engine.lines.clear()
        custom_r = run_child("custom", proxy_url, engine_url, ambient_proxy=None)
        check(
            "自定义：外网请求走用户填写的代理",
            any(line.startswith("GET http://remote.test/") for line in proxy.lines),
            str(proxy.lines),
        )
        check(
            "自定义：六个代理名写入同一地址（与 Rust env_vars 同形状）",
            all(custom_r["env"].get(n) == proxy_url for n in PROXY_NAMES),
            str(custom_r["env"]),
        )
        check(
            "自定义：NO_PROXY = 用户忽略项 + 本地回环",
            custom_r["env"]["NO_PROXY"] == "*.corp.internal,127.0.0.1,localhost,::1",
            str(custom_r["env"]),
        )
        check(
            "自定义：桌面端↔引擎（127.0.0.1）仍直连，未被代理拦截",
            engine.lines == ["GET /health HTTP/1.1"] and not any(str(engine.port) in line for line in proxy.lines),
            f"engine={engine.lines} proxy={proxy.lines}",
        )
        check("自定义：请求确实拿到引擎响应（链路未被破坏）", custom_r["engine"].get("status") == 200, str(custom_r["engine"]))

        # 4) 不使用：环境里本有代理，注入空值 + NO_PROXY=* 必须把它压掉
        proxy.lines.clear()
        engine.lines.clear()
        r = run_child("disabled", proxy_url, engine_url, ambient_proxy=proxy_url)
        check(
            "不使用：清空继承来的代理，外网请求不再经过代理",
            proxy.lines == [],
            str(proxy.lines),
        )
        check("不使用：引擎地址仍直连", engine.lines == ["GET /health HTTP/1.1"], str(engine.lines))
        # 与「跟随系统」同一口径：连不上或被本机网络拦成错误码都算如实，被接管成 2xx 才算坏
        disabled_remote = r["remote"]
        check(
            "不使用：外网探测未被接管，也没被伪装成正常",
            "error" in disabled_remote or int(disabled_remote.get("status") or 0) >= 400,
            str(disabled_remote),
        )
    finally:
        proxy.close()
        engine.close()

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
