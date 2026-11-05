#!/usr/bin/env python3
"""
引擎「网关模式」自测（T3.14–T3.16）

锁定五类事实：
1. 模型配置里的 gateway 段被识别为网关模式：base_url 指向网关，
   api_key 必须是「每次请求现取」的换票函数，而不是静态密钥；
2. 换票缓存：到期前 2 分钟刷新；并发只换一次；401 后 invalidate 强制重换；
3. 网关返回 401 时只重试一次，且流式已吐出内容后绝不重试拼接；
4. 换票错误只带稳定错误码，响应体/令牌不进错误文案，repr 也不含令牌；
5. 网关模式不启用引擎侧故障转移（路由与降级归控制面策略）。

不发起外部网络请求：换票走本地回环假账号服务；模型用假实现替换。

用法: python tests/integration/engine_gateway_selftest.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

_TMP_HOME = tempfile.mkdtemp(prefix="tt-gateway-")
os.environ["TONGTIANWORK_HOME"] = _TMP_HOME

try:
    from agentscope.model import ChatModelBase

    import gateway_token as gwt
    import server as engine_server
except Exception as exc:  # 依赖缺失时优雅跳过
    print(f"SKIP: engine gateway selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def run_async(coro):
    return asyncio.run(coro)


class Fake401(Exception):
    status_code = 401


class FakeExchange:
    """可编排的换票实现：按顺序返回令牌/异常，记录调用参数。"""

    def __init__(self, script, now_fn=None):
        self.script = list(script)
        self.calls = []
        self.now_fn = now_fn or time.time

    async def __call__(self, base, access_token, dept):
        self.calls.append((base, access_token, dept))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            return item(self.now_fn())
        return item


class StubProvider:
    def __init__(self):
        self.invalidations = 0

    def invalidate(self):
        self.invalidations += 1


class FlakyModel(ChatModelBase):
    """按脚本返回结果/抛异常/吐 chunk 的假模型。"""

    def __init__(self, script, stream=False):
        super().__init__(model_name="gateway-model", stream=stream)
        self.script = list(script)
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        self.calls += 1
        item = self.script.pop(0) if self.script else RuntimeError("脚本已用尽")
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, list):
            async def gen():
                for chunk in item:
                    if isinstance(chunk, BaseException):
                        raise chunk
                    yield chunk
            return gen()
        return item


def test_config_parsing() -> None:
    cfg = {
        "modelName": "auto",
        "gateway": {
            "baseUrl": " http://127.0.0.1:8001/v1/ ",
            "controlPlaneUrl": " http://127.0.0.1:8000/ ",
            "dept": " dept-eng ",
        },
        "fallbacks": [{"modelName": "backup", "baseUrl": "https://x/v1"}],
    }
    gateway = engine_server._gateway_of(cfg)
    check(
        "配置：网关段去掉空白与结尾斜杠，保留部门",
        gateway == {
            "baseUrl": "http://127.0.0.1:8001/v1",
            "controlPlaneUrl": "http://127.0.0.1:8000",
            "dept": "dept-eng",
            "accessToken": "",
        },
        str(gateway),
    )
    check("配置：网关模式优先于自定义 baseUrl", engine_server._model_kind(cfg) == "gateway")
    check(
        "配置：网关模式不启用引擎侧转移",
        engine_server._usable_fallbacks(cfg, "gateway") == [],
    )
    error = None
    try:
        engine_server._gateway_of({"gateway": {"baseUrl": "http://gw/v1"}})
    except RuntimeError as exc:  # noqa: PERF203
        error = str(exc)
    check(
        "配置：网关段缺控制面地址时明确报错，不静默直连",
        bool(error) and "controlPlaneUrl" in error,
        str(error),
    )


def test_provider_cache() -> None:
    now = [1000.0]
    exchange = FakeExchange([lambda t: gwt.ExchangedToken("gw-1", t + 900, "acme", "dept-eng")], now_fn=lambda: now[0])
    provider = gwt.GatewayTokenProvider(
        "http://127.0.0.1:8000/", "access-1", "dept-eng",
        exchange=exchange, now=lambda: now[0],
    )

    first = run_async(provider.get())
    second = run_async(provider.get())
    check(
        "换票：首次换票后命中缓存（只请求一次）",
        first == "gw-1" and second == "gw-1" and len(exchange.calls) == 1,
        f"calls={len(exchange.calls)}",
    )
    check(
        "换票：请求带控制面地址、access token 与部门",
        exchange.calls[0] == ("http://127.0.0.1:8000", "access-1", "dept-eng"),
        str(exchange.calls[0]),
    )

    now[0] = 900 + 1000 - gwt.REFRESH_BEFORE_SECONDS - 1  # 尚在刷新窗口外
    run_async(provider.get())
    check("换票：距到期仍 >2 分钟时继续命中缓存", len(exchange.calls) == 1, str(len(exchange.calls)))

    now[0] = 900 + 1000 - gwt.REFRESH_BEFORE_SECONDS + 1  # 进入刷新窗口
    run_async(provider.get())
    check("换票：到期前 2 分钟内自动换新", len(exchange.calls) == 2, str(len(exchange.calls)))

    provider.invalidate()
    run_async(provider.get())
    check("换票：invalidate 后强制重换（401 路径）", len(exchange.calls) == 3, str(len(exchange.calls)))

    missing = None
    try:
        run_async(
            gwt.GatewayTokenProvider(
                "http://127.0.0.1:8000", "", exchange=exchange, now=lambda: now[0]
            ).get()
        )
    except gwt.GatewayTokenError as exc:  # noqa: PERF203
        missing = exc.code
    check("换票：没有 access token 时返回 GATEWAY_TOKEN_MISSING", missing == "GATEWAY_TOKEN_MISSING", str(missing))

    bad = None
    try:
        gwt.GatewayTokenProvider("ftp://gw", "access-1", exchange=exchange)
    except gwt.GatewayTokenError as exc:  # noqa: PERF203
        bad = exc.code
    check("换票：非 http/https 控制面地址被拒绝", bad == "GATEWAY_CONFIG_INVALID", str(bad))


def test_single_flight() -> None:
    now = [1000.0]

    async def slow_exchange(base, access_token, dept):
        await asyncio.sleep(0.02)
        return gwt.ExchangedToken("gw-concurrent", now[0] + 900)

    provider = gwt.GatewayTokenProvider(
        "http://127.0.0.1:8000", "access-1", exchange=slow_exchange, now=lambda: now[0]
    )

    async def gather():
        return await asyncio.gather(*[provider.get() for _ in range(6)])

    tokens = run_async(gather())
    check(
        "换票：并发请求共用一次换票（single-flight）",
        tokens == ["gw-concurrent"] * 6 and provider.cached_expires_at == 1900.0,
        str(tokens),
    )


def test_token_repr_no_secret() -> None:
    token = gwt.ExchangedToken("super-secret-gateway-token", 2000.0)
    check("令牌：repr 不包含令牌本体", "super-secret" not in repr(token), repr(token))

    error = gwt.GatewayTokenError("GATEWAY_TOKEN_REJECTED", "换取被拒绝（HTTP 401）")
    check("令牌：错误文案不包含令牌", "super-secret" not in str(error), str(error))


class _ExchangeHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    response_body = b""
    response_status = 200

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        type(self).calls.append(
            {
                "path": self.path,
                "authorization": self.headers.get("authorization") or "",
                "body": body,
            }
        )
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(type(self).response_body)))
        self.end_headers()
        self.wfile.write(type(self).response_body)

    def log_message(self, *args):  # 静默：测试输出只留断言
        pass


def test_http_exchange() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ExchangeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    now = time.time()
    try:
        _ExchangeHandler.response_status = 200
        _ExchangeHandler.response_body = (
            b'{"ok":true,"token":"gw-http-1","tokenType":"Bearer",'
            b'"expiresIn":900,"tenant":"acme","dept":"dept-eng"}'
        )
        result = gwt._exchange_sync(base, "access-http", "dept-eng")
        check(
            "HTTP 换票：解析令牌/租户/部门并带上 Bearer 与部门",
            result.token == "gw-http-1"
            and result.tenant == "acme"
            and result.dept == "dept-eng"
            and abs(result.expires_at - (now + 900)) < 30
            and _ExchangeHandler.calls[-1]["path"] == "/api/v1/auth/gateway-token"
            and _ExchangeHandler.calls[-1]["authorization"] == "Bearer access-http"
            and '"dept": "dept-eng"' in _ExchangeHandler.calls[-1]["body"],
            str(_ExchangeHandler.calls[-1]),
        )

        _ExchangeHandler.response_status = 401
        _ExchangeHandler.response_body = b'{"ok":false,"code":"AUTH_REQUIRED"}'
        rejected = None
        try:
            gwt._exchange_sync(base, "access-http", None)
        except gwt.GatewayTokenError as exc:  # noqa: PERF203
            rejected = exc
        check(
            "HTTP 换票：401 映射为 GATEWAY_TOKEN_REJECTED 且带稳定错误码",
            rejected is not None
            and rejected.code == "GATEWAY_TOKEN_REJECTED"
            and "AUTH_REQUIRED" in str(rejected)
            and "access-http" not in str(rejected),
            str(rejected),
        )

        _ExchangeHandler.response_status = 500
        _ExchangeHandler.response_body = b"upstream exploded"
        unavailable = None
        try:
            gwt._exchange_sync(base, "access-http", None)
        except gwt.GatewayTokenError as exc:  # noqa: PERF203
            unavailable = exc
        check(
            "HTTP 换票：500 映射为 GATEWAY_TOKEN_UNAVAILABLE，不回显响应体",
            unavailable is not None
            and unavailable.code == "GATEWAY_TOKEN_UNAVAILABLE"
            and "exploded" not in str(unavailable),
            str(unavailable),
        )

        _ExchangeHandler.response_status = 200
        _ExchangeHandler.response_body = b"not-json"
        malformed = None
        try:
            gwt._exchange_sync(base, "access-http", None)
        except gwt.GatewayTokenError as exc:  # noqa: PERF203
            malformed = exc
        check("HTTP 换票：非 JSON 响应映射为 BAD_RESPONSE", malformed is not None and malformed.code == "GATEWAY_TOKEN_BAD_RESPONSE", str(malformed))

        _ExchangeHandler.response_status = 200
        _ExchangeHandler.response_body = b'{"ok":true,"expiresIn":900}'
        no_token = None
        try:
            gwt._exchange_sync(base, "access-http", None)
        except gwt.GatewayTokenError as exc:  # noqa: PERF203
            no_token = exc
        check("HTTP 换票：响应缺 token 视为 BAD_RESPONSE", no_token is not None and no_token.code == "GATEWAY_TOKEN_BAD_RESPONSE", str(no_token))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_retry_model_non_stream() -> None:
    provider = StubProvider()
    inner = FlakyModel([Fake401("token expired"), {"content": "ok"}])
    wrapper = engine_server.GatewayAuthRetryModel(inner, provider)
    result = run_async(wrapper())
    check(
        "重试：非流式 401 → invalidate 后重试一次成功",
        result == {"content": "ok"} and inner.calls == 2 and provider.invalidations == 1,
        f"result={result} calls={inner.calls} invalidations={provider.invalidations}",
    )

    provider = StubProvider()
    inner = FlakyModel([Fake401("expired"), Fake401("still expired")])
    error = None
    try:
        run_async(engine_server.GatewayAuthRetryModel(inner, provider)())
    except Fake401 as exc:  # noqa: PERF203
        error = exc
    check(
        "重试：连续 401 只重试一次，随后原样抛出",
        isinstance(error, Fake401) and inner.calls == 2 and provider.invalidations == 1,
        f"calls={inner.calls} invalidations={provider.invalidations}",
    )

    provider = StubProvider()
    inner = FlakyModel([RuntimeError("upstream 500")])
    error = None
    try:
        run_async(engine_server.GatewayAuthRetryModel(inner, provider)())
    except RuntimeError as exc:  # noqa: PERF203
        error = exc
    check(
        "重试：非 401 错误不触发换票重试",
        str(error) == "upstream 500" and inner.calls == 1 and provider.invalidations == 0,
        f"calls={inner.calls} invalidations={provider.invalidations}",
    )


def test_retry_model_stream() -> None:
    provider = StubProvider()
    inner = FlakyModel([[Fake401("expired")], ["a", "b"]], stream=True)
    wrapper = engine_server.GatewayAuthRetryModel(inner, provider)

    async def collect():
        agen = await wrapper()
        return [chunk async for chunk in agen]

    texts = run_async(collect())
    check(
        "重试：流式首块前 401 → 重换令牌后重新生成",
        texts == ["a", "b"] and inner.calls == 2 and provider.invalidations == 1,
        f"texts={texts} calls={inner.calls} invalidations={provider.invalidations}",
    )

    provider = StubProvider()
    inner = FlakyModel([["half", Fake401("expired")]], stream=True)
    wrapper = engine_server.GatewayAuthRetryModel(inner, provider)

    async def collect_partial():
        agen = await wrapper()
        seen = []
        error = None
        try:
            async for chunk in agen:
                seen.append(chunk)
        except Fake401 as exc:  # noqa: PERF203
            error = exc
        return seen, error

    seen, error = run_async(collect_partial())
    check(
        "重试：已吐出内容后 401 不重试，避免拼接两段生成",
        seen == ["half"] and isinstance(error, Fake401) and inner.calls == 1 and provider.invalidations == 0,
        f"seen={seen} calls={inner.calls} invalidations={provider.invalidations}",
    )


def test_build_wiring() -> None:
    engine_server._reset_gateway_providers()
    captured = {}

    def fake_build(name, api_key, base_url, kind, stream):
        captured.update(
            {"name": name, "api_key": api_key, "base_url": base_url, "kind": kind, "stream": stream}
        )
        return FlakyModel([{"content": "gw-ok"}], stream=stream), object()

    original = engine_server._build_chat_model
    engine_server._build_chat_model = fake_build

    token = engine_server._CURRENT_ACCESS_TOKEN.set("access-from-request")
    try:
        cfg = {
            "modelName": "auto",
            "gateway": {
                "baseUrl": "http://127.0.0.1:8001/v1",
                "controlPlaneUrl": "http://127.0.0.1:8000",
                "dept": "dept-eng",
            },
        }
        model, _formatter = engine_server.build_model_and_formatter(model_config=cfg, stream=True)
        again, _ = engine_server.build_model_and_formatter(model_config=cfg, stream=True)
        provider = engine_server._gateway_provider(engine_server._gateway_of(cfg))
    finally:
        engine_server._build_chat_model = original
        engine_server._CURRENT_ACCESS_TOKEN.reset(token)

    check(
        "装配：网关模式按网关 base_url 与 kind=gateway 构造模型",
        captured["kind"] == "gateway"
        and captured["base_url"] == "http://127.0.0.1:8001/v1"
        and captured["name"] == "auto",
        str({k: captured[k] for k in ("kind", "base_url", "name")}),
    )
    check(
        "装配：api_key 是每请求现取的换票函数，不是静态字符串",
        callable(captured["api_key"]) and not isinstance(captured["api_key"], str),
        str(type(captured["api_key"])),
    )
    check(
        "装配：外层仍挂用量采集，内层是网关 401 重试包装",
        isinstance(model, engine_server.UsageTrackingModel)
        and isinstance(model._inner, engine_server.GatewayAuthRetryModel)
        and isinstance(again, engine_server.UsageTrackingModel),
        f"{type(model)} inner={type(getattr(model, '_inner', None))}",
    )
    check(
        "装配：网关调用的台账来源固定为 gateway（合并视图据此去重）",
        model._origin == "gateway" and again._origin == "gateway",
        f"{getattr(model, '_origin', None)} / {getattr(again, '_origin', None)}",
    )
    check(
        "装配：同一主体跨轮复用同一个换票缓存",
        model._inner._provider is provider and again._inner._provider is provider,
        f"{id(model._inner._provider)} vs {id(provider)}",
    )


def test_gateway_skips_direct_key_requirement() -> None:
    engine_server._reset_gateway_providers()
    captured = {}

    def fake_build(name, api_key, base_url, kind, stream):
        captured["kind"] = kind
        return FlakyModel([{"content": "ok"}], stream=stream), object()

    original = engine_server._build_chat_model
    engine_server._build_chat_model = fake_build
    token = engine_server._CURRENT_ACCESS_TOKEN.set("access-from-request")
    try:
        cfg = {
            "modelName": "auto",
            "gateway": {"baseUrl": "http://127.0.0.1:8001/v1", "controlPlaneUrl": "http://127.0.0.1:8000"},
        }
        engine_server.build_model_and_formatter(model_config=cfg, stream=False)
    finally:
        engine_server._build_chat_model = original
        engine_server._CURRENT_ACCESS_TOKEN.reset(token)
        engine_server._reset_gateway_providers()
    check("装配：网关模式无需 models.json 里的 apiKey 也能装配", captured.get("kind") == "gateway", str(captured))


def main() -> None:
    try:
        test_config_parsing()
        test_provider_cache()
        test_single_flight()
        test_token_repr_no_secret()
        test_http_exchange()
        test_retry_model_non_stream()
        test_retry_model_stream()
        test_build_wiring()
        test_gateway_skips_direct_key_requirement()
    finally:
        engine_server._reset_gateway_providers()
        shutil.rmtree(_TMP_HOME, ignore_errors=True)

    print("-" * 50)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine gateway selftest: ALL PASS")


if __name__ == "__main__":
    main()
