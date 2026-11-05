"""M1.1 网关核心：DB、密钥、Provider、SSRF、配额、用量、启动自检与 HTTP 集成。"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import socket
import sqlite3
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import support  # noqa: E402
from tests.support import (  # noqa: E402
    ADMIN_TOKEN,
    MASTER_KEY,
    admin_headers,
    bearer,
    check,
    configure_openai_provider,
    error_code,
    http,
    parse_sse,
    principal,
    report,
    start_fake_upstreams,
    start_gateway,
    stop_fake_upstreams,
    stop_gateway,
)

from app.adapters.base import Usage
from app.config import settings
from app.contract import UsageEvent
from app.db import SCHEMA_VERSION, Database
from app.errors import GatewayError
from app.providers_store import ProviderStore
from app.quota_sqlite import SqliteQuota
from app.secrets import Secrets
from app.startup import enforce_startup, validate_startup
from app.upstream import validate_provider_url, verify_target_url
from app.usage_store import UsageStore, compute_cost

BASE64_KEY_2 = "MWYyZDRjNWU2YTdiOGM5ZDBlMWYyYTNiNGM1ZDZlN2Y="


def _tmp_db() -> tuple[Path, Database]:
    path = Path(tempfile.mkdtemp(prefix="tt-gateway-core-")) / "gateway.db"
    return path, Database(path)


def _script(name: str):
    import importlib.util

    target = Path(__file__).resolve().parent.parent / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"tt_script_{target.stem}", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---- T1.1 SQLite ----

def run_db_tests() -> None:
    path, db = _tmp_db()
    db.ensure()
    rows = db.query(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row["name"] for row in rows}
    for expected in (
        "providers",
        "provider_keys",
        "usage_events",
        "quota_counters",
        "quota_overrides",
        "audit_log",
        "schema_version",
    ):
        check(f"DB 建表 {expected}", expected in tables)

    version = db.query("SELECT version FROM schema_version")
    check(
        f"DB schema_version={SCHEMA_VERSION}",
        len(version) == 1 and int(version[0]["version"]) == SCHEMA_VERSION,
    )
    journal = db.query("PRAGMA journal_mode")
    check("DB WAL 模式", str(journal[0][0]).lower() == "wal")

    again = Database(path)
    again.ensure()
    version = again.query("SELECT version FROM schema_version")
    check(
        "DB 重复启动幂等",
        len(version) == 1 and int(version[0]["version"]) == SCHEMA_VERSION,
    )
    again.close()

    with db.tx() as conn:
        conn.execute(
            "INSERT INTO audit_log(at_ms, actor, action, object) VALUES (1, 'a', 'x', 'o')"
        )
    check("DB 事务提交", len(db.query("SELECT * FROM audit_log")) == 1)

    try:
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO audit_log(at_ms, actor, action, object) VALUES (2, 'a', 'y', 'o')"
            )
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    check("DB 事务回滚", len(db.query("SELECT * FROM audit_log")) == 1)

    with db.tx() as conn:
        conn.execute(
            "INSERT INTO audit_log(at_ms, actor, action, object) VALUES (3, 'a', 'outer', 'o')"
        )
        try:
            with db.tx() as inner:
                inner.execute(
                    "INSERT INTO audit_log(at_ms, actor, action, object) "
                    "VALUES (4, 'a', 'inner', 'o')"
                )
                raise RuntimeError("inner")
        except RuntimeError:
            pass
        conn.execute(
            "INSERT INTO audit_log(at_ms, actor, action, object) VALUES (5, 'a', 'after', 'o')"
        )
    actions = [row["action"] for row in db.query("SELECT action FROM audit_log ORDER BY id")]
    check(
        "DB 嵌套 SAVEPOINT 只回滚内层",
        actions == ["x", "outer", "after"],
        str(actions),
    )
    db.close()

    future = Path(tempfile.mkdtemp(prefix="tt-gateway-core-")) / "future.db"
    raw = sqlite3.connect(str(future))
    raw.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    raw.execute("INSERT INTO schema_version(version) VALUES (99)")
    raw.commit()
    raw.close()
    try:
        Database(future).connect()
        raised = None
    except RuntimeError as exc:
        raised = str(exc)
    check("DB 拒绝高版本 schema", raised is not None and "99" in (raised or ""))


# ---- T1.2 密钥 ----

def run_secrets_tests() -> None:
    secrets = Secrets.from_settings(settings)
    check("密钥环可用", secrets.available and secrets.current_version == 1)

    encrypted = secrets.encrypt("sk-abcdefgh1234")
    check("密钥 last4", encrypted.last4 == "1234")
    check(
        "密钥加解密往返",
        secrets.decrypt(encrypted.ciphertext, encrypted.nonce, encrypted.key_version)
        == "sk-abcdefgh1234",
    )

    tampered = bytearray(encrypted.ciphertext)
    tampered[0] ^= 0xFF
    try:
        secrets.decrypt(bytes(tampered), encrypted.nonce, encrypted.key_version)
        raised = None
    except ValueError as exc:
        raised = str(exc)
    check("密钥密文篡改被拒", raised is not None and "解密失败" in (raised or ""))

    tampered_nonce = bytearray(encrypted.nonce)
    tampered_nonce[0] ^= 0xFF
    try:
        secrets.decrypt(encrypted.ciphertext, bytes(tampered_nonce), encrypted.key_version)
        raised = None
    except ValueError as exc:
        raised = str(exc)
    check("密钥 nonce 篡改被拒", raised is not None)

    try:
        secrets.decrypt(encrypted.ciphertext, encrypted.nonce, 9)
        raised = None
    except ValueError as exc:
        raised = str(exc)
    check("密钥缺少版本被拒", raised is not None and "缺少主密钥版本" in (raised or ""))

    rotated = secrets.with_rotation(2, BASE64_KEY_2)
    new_secret = rotated.encrypt("sk-new-secret-9876")
    check("轮换后新密文使用新版本", new_secret.key_version == 2)
    check(
        "轮换后旧密文仍可读",
        rotated.decrypt(encrypted.ciphertext, encrypted.nonce, 1) == "sk-abcdefgh1234",
    )
    check(
        "轮换后新密文可读",
        rotated.decrypt(new_secret.ciphertext, new_secret.nonce, 2)
        == "sk-new-secret-9876",
    )
    try:
        secrets.decrypt(new_secret.ciphertext, new_secret.nonce, 2)
        raised = None
    except ValueError as exc:
        raised = str(exc)
    check("旧密钥环无法解新版本", raised is not None)

    disabled = Secrets.disabled()
    check("未配置主密钥 available=False", not disabled.available)
    try:
        disabled.encrypt("sk-anything")
        raised = None
    except RuntimeError as exc:
        raised = str(exc)
    check("未配置主密钥拒绝加密", raised is not None and "MASTER_KEY" in (raised or ""))


# ---- T1.3 Provider 与密钥 ----

def run_provider_store_tests() -> None:
    _path, db = _tmp_db()
    secrets = Secrets.from_settings(settings)
    store = ProviderStore(db, secrets, allow_private_global=True)

    created = store.create_provider(
        {
            "id": "openai_compatible",
            "baseUrl": "http://127.0.0.1:9/v1",
            "label": "本地假上游",
            "timeoutSeconds": 30,
        }
    )
    check("Provider 创建", created["configured"] and created["enabled"])
    check("Provider 公共视图无密钥", "secret" not in json.dumps(created))

    try:
        store.create_provider({"id": "openai_compatible", "baseUrl": "http://127.0.0.1:9/v1"})
        raised = None
    except GatewayError as exc:
        raised = exc.code
    check("Provider 重复创建被拒", raised == "INVALID_REQUEST")

    secret = "sk-provider-secret-abcd"
    key = store.add_key("openai_compatible", label="主密钥", secret=secret)
    check("密钥创建返回明文一次", key["secret"] == secret and key["last4"] == "abcd")
    published = store.get_provider("openai_compatible")
    check(
        "Provider 列表只暴露 last4",
        published["keys"][0]["last4"] == "abcd"
        and secret not in json.dumps(published),
    )

    row = db.query("SELECT ciphertext, last4 FROM provider_keys")[0]
    check(
        "DB 中不存明文",
        bytes(row["ciphertext"]) != secret.encode() and row["last4"] == "abcd",
    )
    audits = db.query("SELECT before_json, after_json FROM audit_log")
    audit_text = json.dumps([dict(item) for item in audits], ensure_ascii=False)
    check("审计日志不含明文", secret not in audit_text)
    check(
        "审计日志记录动作",
        len(db.query("SELECT * FROM audit_log WHERE action='provider.key.create'")) == 1,
    )

    rotated = store.rotate_key("openai_compatible", key["id"], secret="sk-new-secret-wxyz")
    check("密钥轮换返回新密钥", rotated["key"]["last4"] == "wxyz")
    check("密钥轮换标记旧密钥", rotated["previousKeyId"] == key["id"])
    active = store.active_key("openai_compatible")
    check("轮换后使用新密钥", active.secret == "sk-new-secret-wxyz")
    keys = store.get_provider("openai_compatible")["keys"]
    check(
        "旧密钥已停用",
        len(keys) == 2 and sum(1 for item in keys if item["active"]) == 1,
    )

    store.disable_key("openai_compatible", rotated["key"]["id"])
    check("停用密钥幂等", store.disable_key("openai_compatible", rotated["key"]["id"])["keyId"] == rotated["key"]["id"])
    check(
        "无可用密钥回 503",
        error_code(lambda: store.active_key("openai_compatible"))
        == "PROVIDER_UNAVAILABLE",
    )
    check(
        "未知 provider 密钥被拒",
        error_code(lambda: store.add_key("nope", label="x", secret="sk-1"))
        == "INVALID_REQUEST",
    )
    check(
        "密钥 weight 越界被拒",
        error_code(
            lambda: store.add_key("openai_compatible", label="x", secret="sk-2", weight=0)
        )
        == "INVALID_REQUEST",
    )
    check(
        "密钥空值被拒",
        error_code(
            lambda: store.add_key("openai_compatible", label="x", secret="")
        )
        == "INVALID_REQUEST",
    )
    check(
        "请求期未知 provider 回 503",
        error_code(
            lambda: store.resolve_target("ghost", model="m", upstream_model="m")
        )
        == "PROVIDER_UNAVAILABLE",
    )
    db.close()

    _path2, db2 = _tmp_db()
    strict = ProviderStore(db2, secrets, allow_private_global=False)
    check(
        "非本地 provider 禁止 allowPrivate",
        error_code(
            lambda: strict.create_provider(
                {"id": "openai_compatible", "baseUrl": "http://127.0.0.1:9/v1", "allowPrivate": True}
            )
        )
        == "INVALID_REQUEST",
    )
    local = strict.create_provider(
        {"id": "ollama", "baseUrl": "http://127.0.0.1:11434", "allowPrivate": True}
    )
    check("本地 provider 允许 allowPrivate", local["allowPrivate"] is True)
    check(
        "timeout 越界被拒",
        error_code(
            lambda: strict.update_provider("ollama", {"timeoutSeconds": 0})
        )
        == "INVALID_REQUEST",
    )
    db2.close()


# ---- T1.4 SSRF 与上游校验 ----

def run_upstream_tests() -> None:
    check(
        "公网地址通过",
        validate_provider_url("http://8.8.8.8/v1", allow_private=False)
        == "http://8.8.8.8/v1",
    )
    for label, url in (
        ("非 http(s)", "ftp://8.8.8.8/v1"),
        ("带用户名密码", "http://user:pass@8.8.8.8/v1"),
        ("端口非法", "http://8.8.8.8:notaport/v1"),
        ("metadata 域名", "http://metadata.google.internal/latest"),
        ("metadata 链路本地", "http://169.254.169.254/latest/meta-data"),
        ("回环", "http://127.0.0.1:8000/v1"),
        ("私网 10/8", "http://10.0.0.5/v1"),
        ("私网 192.168/16", "http://192.168.1.10/v1"),
        ("未指定地址", "http://0.0.0.0/v1"),
        ("IPv6 回环", "http://[::1]:8000/v1"),
    ):
        check(
            f"SSRF 拦截 {label}",
            error_code(
                lambda url=url: validate_provider_url(url, allow_private=False)
            )
            == "INVALID_REQUEST",
        )

    check(
        "allowPrivate 放行回环",
        validate_provider_url("http://127.0.0.1:8000/v1", allow_private=True)
        == "http://127.0.0.1:8000/v1",
    )
    check(
        "metadata 即使 allowPrivate 也拦截",
        error_code(
            lambda: validate_provider_url(
                "http://169.254.169.254/latest", allow_private=True
            )
        )
        == "INVALID_REQUEST",
    )
    check(
        "空地址被拒",
        error_code(lambda: validate_provider_url("", allow_private=True))
        == "INVALID_REQUEST",
    )

    verify_target_url("http://127.0.0.1:9/v1", allow_private=True)
    check(
        "请求期私网地址回 503",
        error_code(
            lambda: verify_target_url("http://127.0.0.1:9/v1", allow_private=False)
        )
        == "PROVIDER_UNAVAILABLE",
    )
    check(
        "请求期 metadata 地址回 503",
        error_code(
            lambda: verify_target_url(
                "http://169.254.169.254/latest", allow_private=True
            )
        )
        == "PROVIDER_UNAVAILABLE",
    )


# ---- T1.7 配额 ----

def run_quota_tests() -> None:
    _path, db = _tmp_db()
    quota = SqliteQuota(db)

    key = "acme:dept-a:u1"
    limits = {"monthlyTokens": 1000, "concurrency": 2}
    first = quota.reserve(key, limits, 100)
    check("配额预留后剩余减少", quota.remaining(key, limits) == 900)
    second = quota.reserve(key, limits, 200)
    check("预约叠加", quota.remaining(key, limits) == 700)
    check(
        "并发超限被拒",
        error_code(lambda: quota.reserve(key, limits, 100))
        == "QUOTA_CONCURRENCY_EXCEEDED",
    )
    quota.release(second)
    check("释放后恢复剩余", quota.remaining(key, limits) == 900)
    quota.settle(first, 50)
    check("结算写入已用量", quota.remaining(key, limits) == 950)

    monthly_key = "acme:dept-b:u2"
    monthly = {"monthlyTokens": 100, "concurrency": 10}
    reserved = quota.reserve(monthly_key, monthly, 80)
    check(
        "月配额不足被拒",
        error_code(lambda: quota.reserve(monthly_key, monthly, 30))
        == "QUOTA_TOKENS_EXCEEDED",
    )
    quota.release(reserved)
    again = quota.reserve(monthly_key, monthly, 30)
    quota.settle(again, 25)
    check("结算后剩余按实际用量", quota.remaining(monthly_key, monthly) == 75)
    quota.settle(again, 500)
    check("重复结算被忽略", quota.remaining(monthly_key, monthly) == 75)

    check(
        "负预留被拒",
        error_code(lambda: quota.reserve(monthly_key, monthly, -1))
        == "INVALID_REQUEST",
    )
    check(
        "负结算被拒",
        error_code(lambda: quota.settle(again, -1)) == "INVALID_REQUEST",
    )

    legacy = quota.reserve("acme::legacy", monthly, 10)
    check("空部门主体可预留", quota.remaining("acme::legacy", monthly) == 90)
    quota.release(legacy)

    snapshot = quota.snapshot()
    check(
        "配额快照含主体与周期",
        any(item["principal"] == monthly_key and item["period"] for item in snapshot),
    )
    db.close()


# ---- T1.8 用量与计价 ----

def run_usage_tests() -> None:
    pricing = {
        "currency": "USD",
        "inputPer1M": "1.00",
        "outputPer1M": "2.00",
        "cachedInputPer1M": "0.50",
        "reasoningAsOutput": True,
    }
    cost = compute_cost(pricing, Usage(prompt_tokens=1000, completion_tokens=500))
    check("成本计算 micros", cost.micros == 2000 and cost.amount == "0.002000")

    cached = compute_cost(
        pricing, Usage(prompt_tokens=1000, completion_tokens=500, cached_tokens=200)
    )
    check("缓存 token 单独计价", cached.micros == 1900, cached.amount)

    no_reasoning = compute_cost(
        {**pricing, "reasoningAsOutput": False},
        Usage(prompt_tokens=0, completion_tokens=100, reasoning_tokens=50),
    )
    check("reasoning 不计输出", no_reasoning.micros == 100)

    zero = compute_cost(None, Usage(prompt_tokens=10, completion_tokens=10))
    check("无定价记 0", zero.amount == "0" and zero.micros == 0)

    _path, db = _tmp_db()
    store = UsageStore(db)
    event = UsageEvent(
        gatewayId="gw-test-01",
        requestId="req-ledger-1",
        tenantId="acme",
        departmentId="dept-sales",
        userId="u-1",
        protocol="openai",
        endpoint="/v1/chat/completions",
        modelAlias="auto",
        model="fake-chat",
        provider="openai_compatible",
        promptTokens=12,
        completionTokens=8,
        totalTokens=20,
        cost="0.000028",
        currency="USD",
        priceVersion="test-2026-10",
        latencyMs=5,
        status="ok",
        stream=True,
        estimated=False,
        startedAt=1000,
        finishedAt=1010,
    )
    check("用量首次落库", store.record(event) is True)
    check("用量同主键幂等", store.record(event) is False)
    check("用量计数", store.count() == 1 and store.count_pending() == 1)

    stored = store.get("req-ledger-1")
    check(
        "用量回读字段",
        stored is not None
        and stored.cost == "0.000028"
        and stored.totalTokens == 20
        and stored.stream
        and stored.modelAlias == "auto",
    )
    check("用量待上传", len(store.pending()) == 1)
    check("标记上传", store.mark_uploaded(["req-ledger-1"], 2000) == 1)
    check("上传后队列清空", store.count_pending() == 0)

    aggregate = store.aggregate_by_model()
    check(
        "按模型聚合",
        len(aggregate) == 1
        and aggregate[0]["requests"] == 1
        and aggregate[0]["costMicros"] == 28,
    )

    store.record(
        event.model_copy(
            update={
                "requestId": "req-ledger-2",
                "promptTokens": 5,
                "completionTokens": 0,
                "totalTokens": 5,
                "status": "cancelled",
                "finishedAt": 1020,
            }
        )
    )
    rebuild_quota = _script("rebuild_quota.py")
    preview = rebuild_quota.rebuild(_path, dry_run=True)
    check(
        "重建配额预演不写库",
        preview == [("1970-01", "acme", "dept-sales", "u-1", 25)]
        and db.query("SELECT * FROM quota_counters") == [],
        preview,
    )
    written = rebuild_quota.rebuild(_path, dry_run=False)
    rows = db.query(
        "SELECT period, tenant, dept, sub, used_tokens FROM quota_counters"
    )
    check(
        "按账本重建配额计数器",
        written == preview
        and len(rows) == 1
        and rows[0]["used_tokens"] == 25
        and rows[0]["period"] == "1970-01",
        [tuple(row) for row in rows],
    )
    db.close()


# ---- T1.9 启动自检 ----

def run_startup_tests() -> None:
    secrets = Secrets.from_settings(settings)

    missing = dataclasses.replace(
        settings,
        environment="production",
        jwt_mode="rs256",
        jwks_url="",
        control_plane_url="",
        admin_token="",
        master_key="",
        master_keys={},
    )
    report_missing = validate_startup(missing, Secrets.disabled())
    joined = "；".join(report_missing.errors)
    check("生产环境缺配置拒绝启动", not report_missing.ok)
    for name in (
        "TT_GATEWAY_JWKS_URL",
        "TT_GATEWAY_MASTER_KEY",
        "TT_GATEWAY_ADMIN_TOKEN",
    ):
        check(f"生产自检点名 {name}", name in joined, joined)
    try:
        enforce_startup(missing, Secrets.disabled())
        raised = None
    except RuntimeError as exc:
        raised = str(exc)
    check(
        "启动失败信息点名变量",
        raised is not None and "网关无法启动" in raised and "TT_GATEWAY_ADMIN_TOKEN" in raised,
    )

    wrong_mode = dataclasses.replace(
        missing,
        jwt_mode="hs256-dev",
        jwks_url="https://control/.well-known/jwks.json",
        admin_token="prod-admin",
        master_key=MASTER_KEY,
        master_keys={},
        allow_private_providers=False,
    )
    report_mode = validate_startup(wrong_mode, secrets)
    check(
        "生产禁止 hs256-dev",
        any("TT_GATEWAY_JWT_MODE=rs256" in item for item in report_mode.errors),
    )

    ready = dataclasses.replace(wrong_mode, jwt_mode="rs256")
    report_ready = validate_startup(ready, secrets)
    check("生产 RS256/JWKS 齐全可启动", report_ready.ok, report_ready.errors)
    insecure_jwks = dataclasses.replace(
        ready, jwks_url="http://control/.well-known/jwks.json"
    )
    check(
        "生产拒绝非 https 的 JWKS",
        not validate_startup(insecure_jwks, secrets).ok,
    )
    needs_service_token = dataclasses.replace(
        ready, control_plane_url="https://control", service_token=""
    )
    check(
        "生产控制面模式要求服务令牌",
        any(
            "TT_GATEWAY_SERVICE_TOKEN" in item
            for item in validate_startup(needs_service_token, secrets).errors
        ),
    )
    private_ready = dataclasses.replace(ready, allow_private_providers=True)
    check(
        "生产 allowPrivate 给出告警",
        any(
            "ALLOW_PRIVATE_PROVIDERS" in item
            for item in validate_startup(private_ready, secrets).warnings
        ),
    )

    dev = dataclasses.replace(
        settings,
        environment="development",
        jwt_mode="hs256-dev",
        admin_token=ADMIN_TOKEN,
        master_key=MASTER_KEY,
        master_keys={},
        allow_private_providers=False,
    )
    report_dev = validate_startup(dev, secrets)
    check("开发环境可启动", report_dev.ok)
    check(
        "开发环境提示 HS256 风险",
        any("开发态 HS256" in item for item in report_dev.warnings),
    )

    dev_no_admin = dataclasses.replace(dev, admin_token="")
    check(
        "开发环境缺管理令牌拒绝启动",
        not validate_startup(dev_no_admin, secrets).ok,
    )


# ---- T1.6 OpenAI 协议面 + T1.7 并发 + T1.8 账本（HTTP 集成） ----

def run_gateway_tests(upstream) -> None:
    from app import state

    status, headers, body = http(
        "POST",
        "/v1/chat/completions",
        headers=bearer(),
        body={
            "model": "fake-chat",
            "messages": [{"role": "user", "content": "你好"}],
        },
    )
    check("对话 200", status == 200 and body["choices"][0]["message"]["content"] == "你好")
    request_id = headers.get("x-tt-request-id", "")
    check(
        "对话响应头",
        headers.get("x-tt-model") == "fake-chat"
        and headers.get("x-tt-request-id") == request_id
        and headers.get("x-tt-quota-remaining", "").isdigit(),
    )

    event = state.usage_store.get(request_id)
    check(
        "成功请求写入账本",
        event is not None
        and event.status == "ok"
        and event.totalTokens == 20
        and event.cost == "0.000028"
        and not event.estimated,
        None if event is None else event.model_dump_json(),
    )
    check(
        "账本不保存提示词",
        event is not None and "你好" not in event.model_dump_json(),
    )

    status, headers, body = http(
        "POST",
        "/v1/chat/completions",
        headers=bearer(),
        body={
            "model": "fake-chat",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 2048,
        },
    )
    check(
        "max_tokens 超部门上限 400",
        status == 400 and body["error"]["code"] == "INVALID_REQUEST",
        str(body),
    )

    status, headers, body = http(
        "POST",
        "/v1/chat/completions",
        headers=bearer(),
        body={
            "model": "fake-chat",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    )
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    events = parse_sse(text)
    payloads = []
    for item in events:
        if item["data"] == "[DONE]":
            payloads.append("[DONE]")
            continue
        if item["data"]:
            payloads.append(json.loads(item["data"]))
    contents = "".join(
        delta.get("content") or ""
        for payload in payloads
        if isinstance(payload, dict)
        for choice in payload.get("choices", [])
        for delta in [choice.get("delta") or {}]
    )
    usage_frames = [
        payload
        for payload in payloads
        if isinstance(payload, dict) and payload.get("usage")
    ]
    check("流式 200 且以 [DONE] 收尾", status == 200 and payloads[-1] == "[DONE]")
    check("流式内容完整", contents == "你好", contents)
    check(
        "include_usage 输出 usage 帧",
        len(usage_frames) == 1
        and usage_frames[0]["usage"]["total_tokens"] == 20
        and usage_frames[0]["choices"] == [],
    )
    stream_id = headers.get("x-tt-request-id", "")
    stream_event = state.usage_store.get(stream_id)
    check(
        "流式用量落库",
        stream_event is not None and stream_event.stream and stream_event.status == "ok",
    )

    status, _, body = http(
        "POST",
        "/v1/chat/completions",
        headers=bearer(),
        body={
            "model": "fake-chat",
            "messages": [{"role": "user", "content": "天气"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "查询天气",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    },
                }
            ],
        },
    )
    check(
        "工具调用映射",
        status == 200
        and body["choices"][0]["finish_reason"] == "tool_calls"
        and body["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
        == "get_weather",
        str(body),
    )

    status, _, body = http(
        "POST",
        "/v1/chat/completions",
        headers=bearer(),
        body={
            "model": "fake-chat",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,iVBORw0KGgo=",
                            },
                        },
                    ],
                }
            ],
        },
    )
    last_body = json.dumps(upstream.records[-1]["body"], ensure_ascii=False)
    check("视觉请求 200", status == 200, str(body))
    check("视觉 part 透传到上游", "image_url" in last_body, last_body[:400])

    status, headers, body = http(
        "POST",
        "/v1/chat/completions",
        headers=bearer(),
        body={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
    )
    check(
        "auto 回退到可用模型",
        status == 200
        and headers.get("x-tt-model") == "fake-chat"
        and headers.get("x-tt-model-alias") == "auto",
        f"{status} {headers} {body}",
    )

    status, _, body = http(
        "POST",
        "/v1/chat/completions",
        headers=bearer(),
        body={"model": "missing-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    check(
        "未配置 provider 返回 503",
        status == 503 and body["error"]["code"] == "PROVIDER_UNAVAILABLE",
        str(body),
    )

    for upstream_status, http_status, code in (
        (429, 429, "UPSTREAM_RATE_LIMITED"),
        (401, 502, "PROVIDER_AUTH_FAILED"),
        (500, 502, "UPSTREAM_ERROR"),
    ):
        upstream.fail_status = upstream_status
        try:
            status, headers, body = http(
                "POST",
                "/v1/chat/completions",
                headers=bearer(),
                body={
                    "model": "fake-chat",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        finally:
            upstream.fail_status = None
        check(
            f"上游 {upstream_status} 映射 {code}",
            status == http_status and body["error"]["code"] == code,
            f"{status} {body}",
        )
        failed = state.usage_store.get(headers.get("x-tt-request-id", ""))
        check(
            f"上游 {upstream_status} 失败也落账本",
            failed is not None
            and failed.status == "error"
            and failed.errorCode == code
            and failed.totalTokens == 0,
            failed.model_dump_json() if failed else "无事件",
        )

    status, _, body = http(
        "POST",
        "/v1/embeddings",
        headers=bearer(),
        body={"model": "embedding-default", "input": ["一", "二"]},
    )
    check(
        "嵌入 200 且向量数匹配",
        status == 200 and len(body["data"]) == 2 and body["usage"]["prompt_tokens"] == 10,
        str(body),
    )

    status, _, body = http("GET", "/gateway/v1/admin/health", headers=admin_headers())
    check(
        "管理健康检查",
        status == 200
        and body["providersConfigured"] >= 1
        and body["usageQueue"] >= 1,
        str(body),
    )
    status, _, body = http("GET", "/gateway/v1/admin/quotas", headers=admin_headers())
    check(
        "管理配额快照",
        status == 200 and isinstance(body["quotas"], list) and body["resetAt"],
        str(body),
    )
    check(
        "配额快照的 period 是当月（resetAt 指向下月 1 日，不能拿它截出月份）",
        body.get("period") == datetime.now(timezone.utc).strftime("%Y-%m")
        and body.get("resetAt", "") > body.get("period", "") + "-31",
        f"period={body.get('period')} resetAt={body.get('resetAt')}",
    )

    upstream.delay_seconds = 0.6
    try:
        def call(_index: int):
            return http(
                "POST",
                "/v1/chat/completions",
                headers=bearer(),
                body={
                    "model": "fake-chat",
                    "messages": [{"role": "user", "content": "并发"}],
                },
                timeout=20,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(call, range(10)))
    finally:
        upstream.delay_seconds = 0.0
    successes = [item for item in results if item[0] == 200]
    rejected = [
        item
        for item in results
        if item[0] == 429
        and item[2].get("error", {}).get("code") == "QUOTA_CONCURRENCY_EXCEEDED"
    ]
    check(
        "并发 10 对上限 4 只放行 4",
        len(successes) == 4 and len(rejected) == 6,
        f"成功 {len(successes)} 拒绝 {len(rejected)}",
    )

    host, port = "127.0.0.1", int(support.BASE.rsplit(":", 1)[1])
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Content-Type: application/json\r\n"
            b"Connection: close\r\n\r\n"
            b"5\r\nhello\r\n0\r\n\r\n"
        )
        raw = b""
        while True:
            block = sock.recv(65536)
            if not block:
                break
            raw += block
    status_line = raw.split(b"\r\n", 1)[0].decode("latin-1")
    check(
        "分块请求体直接 413（Content-Length 缺失无法预判体积）",
        " 413 " in status_line and b"PAYLOAD_TOO_LARGE" in raw,
        status_line,
    )


def main() -> int:
    run_db_tests()
    run_secrets_tests()
    run_provider_store_tests()
    run_upstream_tests()
    run_quota_tests()
    run_usage_tests()
    run_startup_tests()

    upstreams = start_fake_upstreams(("openai",))
    server = start_gateway()
    try:
        configure_openai_provider(upstreams["openai"])
        run_gateway_tests(upstreams["openai"])
    finally:
        stop_gateway(server)
        stop_fake_upstreams(upstreams)
    return report("全部核心测试通过")


if __name__ == "__main__":
    raise SystemExit(main())
