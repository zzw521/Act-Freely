"""共享状态层（M5）：Redis 令牌桶、在途配额预留与策略失效通知。

只有配置 `TT_GATEWAY_REDIS_URL` 时启用；未启用时所有方法都不会被调用，
单 worker 行为与 M1–M4 完全一致。Redis 故障一律抛 `SharedStateError`，
由调用方转成 `503 SHARED_STATE_UNAVAILABLE`（失败关闭，不静默降级）。
"""

from __future__ import annotations

import os
import threading
from typing import Callable
from urllib.parse import urlsplit

try:  # 未配置 Redis 的部署不强制安装依赖
    import redis
except ImportError:  # pragma: no cover - requirements 已锁定 redis
    redis = None  # type: ignore[assignment]

TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now
end
if now < ts then
  ts = now
end
tokens = math.min(capacity, tokens + (now - ts) * rate)
local retry = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  retry = (1 - tokens) / rate
end
redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', key, 600)
return tostring(retry)
"""

RESERVE_LUA = """
local resK = KEYS[1]
local actK = KEYS[2]
local kresK = KEYS[3]
local rid = ARGV[1]
local tokens = tonumber(ARGV[2])
local used = tonumber(ARGV[3])
local monthly = tonumber(ARGV[4])
local concurrency = tonumber(ARGV[5])
local keyUsed = tonumber(ARGV[6])
local keyMonthly = tonumber(ARGV[7])
local ttl = tonumber(ARGV[8])
local reserved = tonumber(redis.call('HGET', resK, '__total') or '0')
if monthly > 0 and used + reserved + tokens > monthly then
  return {1, used + reserved, monthly}
end
local active = tonumber(redis.call('GET', actK) or '0')
if concurrency > 0 and active >= concurrency then
  return {2, active, concurrency}
end
local keyReserved = 0
if kresK ~= '' then
  keyReserved = tonumber(redis.call('HGET', kresK, '__total') or '0')
end
if keyMonthly > 0 and keyUsed + keyReserved + tokens > keyMonthly then
  return {3, keyUsed + keyReserved, keyMonthly}
end
redis.call('HSET', resK, rid, tokens)
redis.call('HINCRBY', resK, '__total', tokens)
redis.call('EXPIRE', resK, ttl)
redis.call('INCR', actK)
redis.call('EXPIRE', actK, ttl)
if kresK ~= '' then
  redis.call('HSET', kresK, rid, tokens)
  redis.call('HINCRBY', kresK, '__total', tokens)
  redis.call('EXPIRE', kresK, ttl)
end
return {0}
"""

RELEASE_LUA = """
local resK = KEYS[1]
local actK = KEYS[2]
local kresK = KEYS[3]
local rid = ARGV[1]
local tokens = tonumber(ARGV[2])
local keyTokens = tonumber(ARGV[3])
local removed = redis.call('HDEL', resK, rid)
if removed == 1 then
  local total = redis.call('HINCRBY', resK, '__total', -tokens)
  if tonumber(total) < 0 then
    redis.call('HSET', resK, '__total', 0)
  end
  local active = redis.call('DECR', actK)
  if tonumber(active) <= 0 then
    redis.call('DEL', actK)
  end
end
if kresK ~= '' and removed == 1 then
  local kremoved = redis.call('HDEL', kresK, rid)
  if kremoved == 1 then
    local ktotal = redis.call('HINCRBY', kresK, '__total', -keyTokens)
    if tonumber(ktotal) < 0 then
      redis.call('HSET', kresK, '__total', 0)
    end
  end
end
return 1
"""

TPM_CONSUME_LUA = """
local n = tonumber(ARGV[1])
local cost = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local tokens = {}
local rates = {}
local retry = 0
for i = 1, n do
  local capacity = tonumber(ARGV[2 + (i - 1) * 2 + 1])
  local rate = tonumber(ARGV[2 + (i - 1) * 2 + 2])
  local data = redis.call('HMGET', KEYS[i], 'tokens', 'ts')
  local value = tonumber(data[1])
  local ts = tonumber(data[2])
  if value == nil then
    value = capacity
    ts = now
  end
  if now < ts then
    ts = now
  end
  value = math.min(capacity, value + (now - ts) * rate)
  tokens[i] = value
  rates[i] = rate
end
for i = 1, n do
  if tokens[i] < cost then
    local wait = (cost - tokens[i]) / rates[i]
    if wait > retry then
      retry = wait
    end
  end
end
for i = 1, n do
  local next_tokens = tokens[i]
  if retry == 0 then
    next_tokens = math.max(0, tokens[i] - cost)
  end
  redis.call('HSET', KEYS[i], 'tokens', tostring(next_tokens), 'ts', tostring(now))
  redis.call('EXPIRE', KEYS[i], 600)
end
return tostring(retry)
"""

TPM_REFUND_LUA = """
local n = tonumber(ARGV[1])
local amount = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
for i = 1, n do
  local capacity = tonumber(ARGV[2 + (i - 1) * 2 + 1])
  local rate = tonumber(ARGV[2 + (i - 1) * 2 + 2])
  if redis.call('EXISTS', KEYS[i]) == 1 then
    local data = redis.call('HMGET', KEYS[i], 'tokens', 'ts')
    local value = tonumber(data[1])
    local ts = tonumber(data[2])
    if value == nil then
      value = capacity
      ts = now
    end
    if now < ts then
      ts = now
    end
    value = math.min(capacity, value + (now - ts) * rate)
    value = math.min(capacity, value + amount)
    redis.call('HSET', KEYS[i], 'tokens', tostring(value), 'ts', tostring(now))
    redis.call('EXPIRE', KEYS[i], 600)
  end
end
return 1
"""

RESERVATION_TTL_SECONDS = 3600
POLICY_CHANNEL_SUFFIX = ":policy:events"


class SharedStateError(RuntimeError):
    """Redis 连接/超时/脚本错误；调用方应失败关闭。"""


class SharedState:
    def __init__(
        self,
        redis_url: str = "",
        *,
        prefix: str = "ttgw",
        timeout_ms: int = 500,
        workers: int = 1,
        client_factory: Callable[[str], object] | None = None,
        sentinel_master: str = "",
        sentinel_urls: tuple[tuple[str, int], ...] = (),
        sentinel_password: str = "",
        password: str = "",
        db: int = 0,
        tls: bool = False,
        tls_ca: str = "",
        tls_cert: str = "",
        tls_key: str = "",
    ) -> None:
        self._url = redis_url
        self._prefix = prefix
        self._timeout_ms = timeout_ms
        self._workers = workers
        self._sentinel_master = sentinel_master
        self._tls = bool(tls)
        self._client = None
        self._token_bucket = None
        self._reserve_script = None
        self._release_script = None
        self._tpm_consume_script = None
        self._tpm_refund_script = None
        self._connected = False
        self._last_error: str | None = None
        self._listener: threading.Thread | None = None
        self._listener_stop = threading.Event()
        self._listener_pubsub = None
        self._listener_status = (
            "disabled" if not (redis_url or sentinel_master) else "starting"
        )
        if redis_url or sentinel_master:
            if client_factory is not None:
                client = client_factory(redis_url)
            else:
                if redis is None:
                    raise SharedStateError("缺少 redis 依赖，无法启用共享状态")
                client = self._build_client(
                    password=password,
                    db=db,
                    tls=tls,
                    tls_ca=tls_ca,
                    tls_cert=tls_cert,
                    tls_key=tls_key,
                    sentinel_password=sentinel_password,
                    sentinel_urls=sentinel_urls,
                )
            self._client = client
            self._token_bucket = client.register_script(TOKEN_BUCKET_LUA)
            self._reserve_script = client.register_script(RESERVE_LUA)
            self._release_script = client.register_script(RELEASE_LUA)
            self._tpm_consume_script = client.register_script(TPM_CONSUME_LUA)
            self._tpm_refund_script = client.register_script(TPM_REFUND_LUA)

    def _build_client(
        self,
        *,
        password: str,
        db: int,
        tls: bool,
        tls_ca: str,
        tls_cert: str,
        tls_key: str,
        sentinel_password: str,
        sentinel_urls: tuple[tuple[str, int], ...],
    ):
        timeout = max(0.1, self._timeout_ms / 1000)
        ssl_kwargs: dict = {}
        if tls:
            ssl_kwargs["ssl"] = True
            if tls_ca:
                ssl_kwargs["ssl_ca_certs"] = tls_ca
            if tls_cert:
                ssl_kwargs["ssl_certfile"] = tls_cert
                ssl_kwargs["ssl_keyfile"] = tls_key
        if self._sentinel_master:
            if not sentinel_urls:
                raise SharedStateError("Sentinel 模式缺少 TT_GATEWAY_REDIS_SENTINEL_URLS")
            sentinel_kwargs = {
                "socket_timeout": timeout,
                "socket_connect_timeout": timeout,
                "protocol": 2,
            }
            if sentinel_password:
                sentinel_kwargs["password"] = sentinel_password
            sentinel_kwargs.update(
                {
                    key: value
                    for key, value in ssl_kwargs.items()
                    if key in ("ssl", "ssl_ca_certs", "ssl_certfile", "ssl_keyfile")
                }
            )
            sentinel = redis.Sentinel(
                list(sentinel_urls), sentinel_kwargs=sentinel_kwargs
            )
            return sentinel.master_for(
                self._sentinel_master,
                decode_responses=True,
                socket_timeout=timeout,
                socket_connect_timeout=timeout,
                health_check_interval=30,
                db=db,
                password=password or None,
                protocol=2,
                **ssl_kwargs,
            )
        parsed = urlsplit(self._url)
        kwargs: dict = {
            "decode_responses": True,
            "socket_timeout": timeout,
            "socket_connect_timeout": timeout,
            "health_check_interval": 30,
            "protocol": 2,
        }
        if parsed.password is None and password:
            kwargs["password"] = password
        if parsed.path in ("", "/"):
            kwargs["db"] = db
        kwargs.update(ssl_kwargs)
        if parsed.scheme == "rediss":
            # rediss:// 已由 connection_class 启用 TLS，重复传 ssl 会冲突
            kwargs.pop("ssl", None)
        elif tls:
            raise SharedStateError(
                "启用 TLS 时 TT_GATEWAY_REDIS_URL 必须使用 rediss:// 地址"
            )
        return redis.Redis.from_url(self._url, **kwargs)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def mode(self) -> str:
        if self._sentinel_master and self.enabled:
            return "sentinel"
        return "redis" if self.enabled else "memory"

    def _run(self, operation):
        if self._client is None:
            raise SharedStateError("共享状态未启用")
        try:
            result = operation()
        except Exception as exc:  # noqa: BLE001 - redis 异常类型随版本变化
            self._connected = False
            self._last_error = f"{type(exc).__name__}: {exc}"[:200]
            raise SharedStateError("共享状态不可用") from exc
        self._connected = True
        self._last_error = None
        return result

    def ping(self) -> None:
        self._run(lambda: self._client.ping())

    # ── 令牌桶 ───────────────────────────────────────────────

    def consume(self, scope: str, key: str, *, per_minute: int, burst: int) -> float:
        """`scope` 为 admin|key；返回 0 放行，否则 Retry-After 秒数。"""
        if per_minute <= 0 or burst <= 0:
            return 0.0
        redis_key = f"{self._prefix}:rl:{scope}:{key}"
        raw = self._run(
            lambda: self._token_bucket(
                keys=[redis_key], args=[float(burst), per_minute / 60.0]
            )
        )
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 1.0

    # ── TPM 桶 ───────────────────────────────────────────────

    def _tpm_key(self, scope: str, key: str) -> str:
        return f"{self._prefix}:tpm:{scope}:{key}"

    def tpm_consume(self, buckets, cost: int) -> float:
        """`buckets` 为 (scope, key, capacity, rate)；原子扣减，返回 Retry-After 秒。"""
        keys = [self._tpm_key(scope, key) for scope, key, _, _ in buckets]
        args: list = [len(keys), cost]
        for _, _, capacity, rate in buckets:
            args.extend([capacity, rate])
        raw = self._run(
            lambda: self._tpm_consume_script(keys=keys, args=args)
        )
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 1.0

    def tpm_refund(self, buckets, amount: int) -> None:
        """把未用预留退回桶；桶不存在（已过期/从未使用）时跳过。"""
        keys = [self._tpm_key(scope, key) for scope, key, _, _ in buckets]
        args: list = [len(keys), amount]
        for _, _, capacity, rate in buckets:
            args.extend([capacity, rate])
        self._run(lambda: self._tpm_refund_script(keys=keys, args=args))

    # ── 配额预留 ─────────────────────────────────────────────

    def _res_key(self, tenant: str, dept: str, sub: str, period: str) -> str:
        return f"{self._prefix}:quota:res:{tenant}:{dept}:{sub}:{period}"

    def _key_res_key(self, key_id: str, period: str) -> str:
        return f"{self._prefix}:quota:kres:{key_id}:{period}"

    def _active_key(self, principal_key: str) -> str:
        return f"{self._prefix}:quota:active:{principal_key}"

    def reserve(
        self,
        *,
        reservation_id: str,
        principal_key: str,
        tenant: str,
        dept: str,
        sub: str,
        period: str,
        tokens: int,
        used: int,
        monthly: int,
        concurrency: int,
        api_key_id: str,
        key_used: int,
        key_monthly: int,
    ) -> None:
        kres = self._key_res_key(api_key_id, period) if api_key_id else ""
        result = self._run(
            lambda: self._reserve_script(
                keys=[
                    self._res_key(tenant, dept, sub, period),
                    self._active_key(principal_key),
                    kres,
                ],
                args=[
                    reservation_id,
                    tokens,
                    used,
                    monthly,
                    concurrency,
                    key_used,
                    key_monthly,
                    RESERVATION_TTL_SECONDS,
                ],
            )
        )
        code = int(result[0]) if result else 0
        if code == 1:
            from .errors import GatewayError

            raise GatewayError.of(
                "QUOTA_TOKENS_EXCEEDED",
                f"当月 token 配额不足（已用 {int(result[1])}，"
                f"预留 {tokens}，上限 {int(result[2])}）",
            )
        if code == 2:
            from .errors import GatewayError

            raise GatewayError.of(
                "QUOTA_CONCURRENCY_EXCEEDED",
                f"并发请求数超限（上限 {int(result[2])}）",
            )
        if code == 3:
            from .errors import GatewayError

            raise GatewayError.of(
                "QUOTA_TOKENS_EXCEEDED",
                "API Key 当月 token 配额不足"
                f"（已用 {int(result[1])}，预留 {tokens}，上限 {int(result[2])}）",
            )

    def release(
        self,
        *,
        reservation_id: str,
        principal_key: str,
        tenant: str,
        dept: str,
        sub: str,
        period: str,
        tokens: int,
        api_key_id: str = "",
        key_tokens: int = 0,
    ) -> None:
        kres = self._key_res_key(api_key_id, period) if api_key_id else ""
        self._run(
            lambda: self._release_script(
                keys=[
                    self._res_key(tenant, dept, sub, period),
                    self._active_key(principal_key),
                    kres,
                ],
                args=[reservation_id, tokens, key_tokens],
            )
        )

    def reserved(self, tenant: str, dept: str, sub: str, period: str) -> int:
        raw = self._run(
            lambda: self._client.hget(
                self._res_key(tenant, dept, sub, period), "__total"
            )
        )
        return int(raw or 0)

    def active(self, principal_key: str) -> int:
        raw = self._run(lambda: self._client.get(self._active_key(principal_key)))
        return int(raw or 0)

    def reservation_rows(self, period: str) -> list[dict]:
        """扫描本前缀下当月的预留汇总；仅用于管理面快照。"""
        pattern = f"{self._prefix}:quota:res:*:{period}"
        rows: list[dict] = []
        seen = 0

        def collect() -> list[dict]:
            nonlocal seen
            result: list[dict] = []
            for key in self._client.scan_iter(match=pattern, count=200):
                seen += 1
                if seen > 5000:
                    break
                prefix = f"{self._prefix}:quota:res:"
                principal_key = key[len(prefix) : -(len(period) + 1)]
                total = int(self._client.hget(key, "__total") or 0)
                if total <= 0:
                    continue
                result.append(
                    {
                        "principal": principal_key,
                        "period": period,
                        "committedTokens": 0,
                        "reservedTokens": total,
                        "activeRequests": self.active(principal_key),
                    }
                )
            return result

        rows = self._run(collect)
        return rows

    # ── 策略失效通知 ─────────────────────────────────────────

    @property
    def policy_channel(self) -> str:
        return f"{self._prefix}{POLICY_CHANNEL_SUFFIX}"

    def publish_policy(self, tenant: str) -> bool:
        """发布失效事件；失败时不阻断策略拉取（各 worker 仍有 TTL 兜底）。"""
        if not self.enabled:
            return False
        try:
            self._run(lambda: self._client.publish(self.policy_channel, tenant))
            return True
        except SharedStateError:
            return False

    def start_policy_listener(self, callback: Callable[[str], None]) -> None:
        if not self.enabled or self._listener is not None:
            return
        self._listener_stop.clear()
        self._listener_status = "starting"
        self._listener = threading.Thread(
            target=self._listen_policy,
            args=(callback,),
            name="tt-policy-listener",
            daemon=True,
        )
        self._listener.start()

    def _listen_policy(self, callback: Callable[[str], None]) -> None:
        backoff = 1.0
        while not self._listener_stop.is_set():
            pubsub = None
            try:
                pubsub = self._client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(self.policy_channel)
                self._listener_pubsub = pubsub
                self._listener_status = "listening"
                backoff = 1.0
                while not self._listener_stop.is_set():
                    message = pubsub.get_message(timeout=1.0)
                    if not message:
                        continue
                    if message.get("type") != "message":
                        continue
                    tenant = message.get("data") or "*"
                    try:
                        callback(str(tenant))
                    except Exception:  # noqa: BLE001 - 回调失败不能杀死订阅线程
                        self._last_error = "策略失效回调失败"
            except Exception as exc:  # noqa: BLE001
                self._listener_status = "reconnecting"
                self._last_error = f"{type(exc).__name__}: {exc}"[:200]
                self._listener_stop.wait(backoff)
                backoff = min(30.0, backoff * 2)
            finally:
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception:  # noqa: BLE001
                        pass
                self._listener_pubsub = None
        self._listener_status = "stopped"

    def stop(self) -> None:
        self._listener_stop.set()
        pubsub = self._listener_pubsub
        if pubsub is not None:
            try:
                pubsub.close()
            except Exception:  # noqa: BLE001
                pass
        if self._listener is not None:
            self._listener.join(timeout=3)
            self._listener = None

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "connected": self._connected if self.enabled else True,
            "tls": self._tls,
            "workers": self._workers,
            "pid": os.getpid(),
            "policySubscriber": self._listener_status,
            "lastError": self._last_error,
        }
