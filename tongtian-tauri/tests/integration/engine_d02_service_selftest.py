#!/usr/bin/env python3
"""D-02 服务化用户体系剩余三项验收的自测（假账号服务驱动吊销内省）。

覆盖缺陷文档 §D-02 标注「未满足」的三条验收：

1. 组织级数据隔离：两个用户使用同一引擎，互相看不到对方会话与产物。
   - u-2 不能 review/cancel/replay/查询 u-1 的任务（403 TASK_FORBIDDEN）；
   - owner 本人与管理员放行；桌面/无令牌（principal None）不隔离。
2. 配额硬停：monthlyTokens / concurrentTasks 超限返回明确错误码且不执行任务。
3. 吊销实时内省：引擎向账号服务确认 sid 是否仍有效；被吊销即拒绝（401），
   账号服务不可达时 fail-open 放行（离线与桌面模式行为不变）。

吊销内省用本机假账号服务（http.server）驱动，不依赖真实 tongtian-server。
依赖缺失（无 fastapi/uvicorn）时输出 SKIP 并以 0 退出。

用法: python tests/integration/engine_d02_service_selftest.py
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

# 用量台账自 D-43 起落盘并按月精确累计（`tokens_used`），配额段的断言是「本月正好 1000」。
# 用真实数据根跑会把上一次运行的记录一起读进来（也会把假用量写进本机台账），
# 所以先把数据根指向临时目录——与 engine_usage_ledger_selftest 同一隔离口径。
os.environ["TONGTIANWORK_HOME"] = tempfile.mkdtemp(prefix="tt-d02-service-")

try:
    import server as engine_server
    from fastapi.testclient import TestClient
except Exception as exc:  # 依赖缺失时优雅跳过
    print(f"SKIP: engine D-02 selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


SECRET = "engine-d02-selftest-secret"
INTROSPECT_TOKEN = "engine-d02-introspect-token"


def make_jwt(sub: str, sid: str, *, role: str = "user", org: str = "acme",
             quota: dict | None = None, **overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": sub,
        "sid": sid,
        "role": role,
        "org": org,
        "quota": quota if quota is not None else {},
        "iss": "tongtian-auth",
        "aud": "tongtian-engine",
        "type": "access",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    header = _b64url(b'{"alg":"HS256","typ":"JWT"}')
    body = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(SECRET.encode(), f"{header}.{body}".encode("ascii"), hashlib.sha256).digest()
    return f"{header}.{body}.{_b64url(sig)}"


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def clear_auth_env() -> None:
    for name in (
        "TT_ENGINE_TOKEN", "TT_ENGINE_JWT_SECRET", "TT_AUTH_JWT_SECRET",
        "TT_ENGINE_JWT_ISSUER", "TT_ENGINE_JWT_AUDIENCE", "TT_ENGINE_ALLOWED_USERS",
        "TT_ENGINE_AUDIT_LOG", "TT_ENGINE_INTROSPECT_URL", "TT_ENGINE_INTROSPECT_TOKEN",
    ):
        os.environ.pop(name, None)
    engine_server._INTROSPECT_CACHE.clear()


# ── 假账号服务：按 ACTIVE 表回答 sid 是否有效 ──────────────────────

class _FakeAuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/v1/engine/introspect":
            self.send_error(404)
            return
        token = self.headers.get("x-tt-engine-introspect", "")
        if not hmac.compare_digest(INTROSPECT_TOKEN, token):
            self._json(401, {"detail": "INTROSPECT_UNAUTHORIZED"})
            return
        q = parse_qs(parsed.query)
        sub = (q.get("sub") or [""])[0]
        sid = (q.get("sid") or [""])[0]
        active = ACTIVE_SESSIONS.get(f"{sub}:{sid}", False)
        self._json(200, {"ok": True, "active": active})

    def _json(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):  # 静音默认访问日志
        return


ACTIVE_SESSIONS: dict[str, bool] = {}


def start_fake_auth() -> tuple[ThreadingHTTPServer, str]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAuthHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host, port = srv.server_address[:2]
    return srv, f"http://{host}:{port}"


def seed_session(task_id: str, owner_sub: str | None) -> engine_server.TaskSession:
    session = engine_server.TaskSession(task_id)
    session.owner_sub = owner_sub
    session.owner_org = "acme"
    engine_server._TASK_SESSIONS[task_id] = session
    return session


# ── 1. 组织级数据隔离 ──────────────────────────────────────────

def check_org_isolation(client: TestClient) -> None:
    print("\n[1] 组织级数据隔离（互相看不到对方会话与产物）")
    clear_auth_env()
    os.environ["TT_ENGINE_JWT_SECRET"] = SECRET
    u1 = make_jwt("u-1", "sess-1")
    u2 = make_jwt("u-2", "sess-2")
    admin = make_jwt("admin-1", "sess-admin", role="admin")

    seed_session("task-alice", "u-1")
    # 跨用户访问一律 403，且不泄露对方数据
    replay = client.get("/api/events/replay?task_id=task-alice&from_seq=0", headers=bearer(u2))
    check("他人 replay 被拒 403 TASK_FORBIDDEN",
          replay.status_code == 403 and replay.json().get("code") == "TASK_FORBIDDEN",
          f"{replay.status_code} {replay.text[:120]}")
    cancel = client.post("/api/chat/cancel", json={"task_id": "task-alice"}, headers=bearer(u2))
    check("他人 cancel 被拒 403",
          cancel.status_code == 403 and cancel.json().get("code") == "TASK_FORBIDDEN",
          f"{cancel.status_code}")
    review = client.post("/api/chat/review", json={"task_id": "task-alice", "action": "reject"},
                         headers=bearer(u2))
    check("他人 review 被拒 403",
          review.status_code == 403 and review.json().get("code") == "TASK_FORBIDDEN",
          f"{review.status_code}")
    cq = client.post("/api/chat/checkpoint/query", json={"task_ids": ["task-alice"]}, headers=bearer(u2))
    check("他人 checkpoint 查询看不到该任务",
          "task-alice" not in (cq.json().get("checkpoints") or {}), str(cq.json())[:120])

    # 本人放行：replay 命中自己的会话（返回窗口，非 403）
    own_replay = client.get("/api/events/replay?task_id=task-alice&from_seq=0", headers=bearer(u1))
    check("本人 replay 不被隔离拦截", own_replay.status_code == 200, f"{own_replay.status_code}")
    # 管理员放行
    admin_replay = client.get("/api/events/replay?task_id=task-alice&from_seq=0", headers=bearer(admin))
    check("管理员 replay 不被隔离拦截", admin_replay.status_code == 200, f"{admin_replay.status_code}")
    # 未归属会话（IM/系统任务，owner 为空）任何用户都不被误拦
    seed_session("task-im", None)
    im_replay = client.get("/api/events/replay?task_id=task-im&from_seq=0", headers=bearer(u2))
    check("未归属会话不被隔离误拦", im_replay.status_code == 200, f"{im_replay.status_code}")

    # 纯函数：归属判定
    can = engine_server._principal_can_access
    p1 = {"sub": "u-1", "role": "user", "auth": "jwt"}
    padmin = {"sub": "admin-1", "role": "admin", "auth": "jwt"}
    check("纯函数：桌面 principal None 放行", can(None, "u-1") is True)
    check("纯函数：本人放行", can(p1, "u-1") is True)
    check("纯函数：他人拒绝", can({"sub": "u-9", "role": "user", "auth": "jwt"}, "u-1") is False)
    check("纯函数：管理员放行", can(padmin, "u-1") is True)
    check("纯函数：空 owner 放行", can(p1, "") is True)

    # 首建盖章：新任务由提交人 stamp
    seed_session("task-stamp", None)
    eng = engine_server
    with client:  # 通过中间件注入 principal
        # 直接调用 stamp：先设 contextvar 模拟 u-2 请求
        tok = eng._CURRENT_PRINCIPAL.set({"sub": "u-2", "org": "acme", "role": "user", "auth": "jwt"})
        try:
            s = eng._TASK_SESSIONS["task-stamp"]
            res = eng._stamp_or_check_session(s)
            check("新会话由首建者盖章", res is None and s.owner_sub == "u-2", str(s.owner_sub))
            # 再切到 u-1 访问同一会话应被拒
            eng._CURRENT_PRINCIPAL.set({"sub": "u-1", "org": "acme", "role": "user", "auth": "jwt"})
            res2 = eng._stamp_or_check_session(s)
            check("盖章后跨用户访问被拒", res2 is not None and res2["code"] == "TASK_FORBIDDEN")
        finally:
            eng._CURRENT_PRINCIPAL.reset(tok)
    clear_auth_env()


# ── 2. 配额硬停 ────────────────────────────────────────────────

def check_quota(client: TestClient) -> None:
    print("\n[2] 配额硬停（超限返回错误码且不执行）")
    clear_auth_env()
    os.environ["TT_ENGINE_JWT_SECRET"] = SECRET

    # tokens_used 口径
    engine_server.USAGE_LEDGER.add("m", 500, 500, sub="q-user")  # 1000 tokens 本月
    check("月度用量按用户累计", engine_server.USAGE_LEDGER.tokens_used("q-user", "monthly") == 1000)

    # 月度 token 超限：/api/chat 直接 429，不进入模型执行
    over = make_jwt("q-user", "sess-q", quota={"monthlyTokens": 500, "concurrentTasks": 0})
    res = client.post("/api/chat", json={"message": "写个报告", "task_id": "task-quota-t"}, headers=bearer(over))
    check("月度 token 超限 → 429 QUOTA_TOKENS_EXCEEDED",
          res.status_code == 429 and res.json().get("code") == "QUOTA_TOKENS_EXCEEDED",
          f"{res.status_code} {res.text[:160]}")

    # 并发超限：伪造该用户一个仍在执行的任务
    running = seed_session("task-quota-running", "c-user")

    class _RunningTask:
        def done(self) -> bool:
            return False

    running.agent_task = _RunningTask()
    c_over = make_jwt("c-user", "sess-c", quota={"monthlyTokens": 0, "concurrentTasks": 1})
    res2 = client.post("/api/chat", json={"message": "再来一单", "task_id": "task-quota-c"}, headers=bearer(c_over))
    check("并发超限 → 429 QUOTA_CONCURRENCY_EXCEEDED",
          res2.status_code == 429 and res2.json().get("code") == "QUOTA_CONCURRENCY_EXCEEDED",
          f"{res2.status_code} {res2.text[:160]}")

    # 未超限 + 0/缺失 = 不限制：放行到后续逻辑（这里以不返回 429 判定）
    ok = make_jwt("free-user", "sess-free", quota={"monthlyTokens": 0, "concurrentTasks": 0})
    # 用未知 stage 让请求在真正跑模型前就返回（plan 会触发模型，避开）
    # 这里通过纯函数直接断言放行
    check("0 配额 = 不限制（纯函数放行）",
          engine_server._quota_error({
              "auth": "jwt", "sub": "q-user",
              "quota": {"monthlyTokens": 0, "concurrentTasks": 0},
          }) is None)
    # 桌面/静态（非 jwt）不做硬停
    check("非 JWT 主体不做配额硬停",
          engine_server._quota_error({"auth": "static", "sub": "static", "quota": {"monthlyTokens": 1}}) is None)

    # 清理注入的执行态与用量，避免影响其他用例
    engine_server._TASK_SESSIONS.pop("task-quota-running", None)
    clear_auth_env()


# ── 3. 吊销实时内省（假账号服务） ──────────────────────────────

def check_introspection(client: TestClient) -> None:
    print("\n[3] 吊销实时内省（假账号服务驱动）")
    srv, base = start_fake_auth()
    try:
        clear_auth_env()
        os.environ["TT_ENGINE_JWT_SECRET"] = SECRET

        # 未配置内省：关闭，只按 exp 判定，放行
        ACTIVE_SESSIONS["u-i:sess-active"] = True
        tok = make_jwt("u-i", "sess-active")
        check("内省关闭时未过期令牌放行", client.get("/api/models", headers=bearer(tok)).status_code == 200)

        # 开启内省：sid 有效 → 放行
        os.environ["TT_ENGINE_INTROSPECT_URL"] = base
        os.environ["TT_ENGINE_INTROSPECT_TOKEN"] = INTROSPECT_TOKEN
        engine_server._INTROSPECT_CACHE.clear()
        ACTIVE_SESSIONS["u-i:sess-active"] = True
        check("内省有效 → 放行", client.get("/api/models", headers=bearer(tok)).status_code == 200)

        # 账号服务判定已吊销 → 401（即便 exp 未过期）
        engine_server._INTROSPECT_CACHE.clear()
        ACTIVE_SESSIONS["u-i:sess-active"] = False
        revoked = client.get("/api/models", headers=bearer(tok))
        check("内省吊销 → 401 ENGINE_UNAUTHORIZED",
              revoked.status_code == 401 and revoked.json().get("code") == "ENGINE_UNAUTHORIZED",
              f"{revoked.status_code} {revoked.text[:120]}")

        # 缓存生效：翻回有效但缓存仍是 False → 命中缓存继续拒绝（不重新查网络）
        ACTIVE_SESSIONS["u-i:sess-active"] = True
        cached = client.get("/api/models", headers=bearer(tok))
        check("吊销结果按 TTL 缓存（未清缓存仍拒绝）", cached.status_code == 401, f"{cached.status_code}")

        # 新 sid 无记录：假服务端默认 active=False → 拒绝（生产为 session_not_found）
        fresh = make_jwt("u-i", "sess-unknown")
        engine_server._INTROSPECT_CACHE.clear()
        check("未知会话被内省拒绝", client.get("/api/models", headers=bearer(fresh)).status_code == 401)

        # fail-open：账号服务不可达（指向已关闭端口）→ 放行，不整体拒绝服务
        srv.shutdown()
        srv.server_close()
        os.environ["TT_ENGINE_INTROSPECT_URL"] = "http://127.0.0.1:1"
        engine_server._INTROSPECT_CACHE.clear()
        active_tok = make_jwt("u-i", "sess-any")
        check("账号服务不可达 → fail-open 放行",
              client.get("/api/models", headers=bearer(active_tok)).status_code == 200)
        # 纯函数：无内省地址直接放行
        os.environ.pop("TT_ENGINE_INTROSPECT_URL", None)
        check("纯函数：未配置内省直接放行", engine_server.session_is_active("u-i", "sess-x") is True)
        # 配置内省但缺 sub/sid → 不放行（无法定位会话）
        os.environ["TT_ENGINE_INTROSPECT_URL"] = "http://127.0.0.1:1"
        check("纯函数：配置内省但缺 sub/sid → 不放行",
              engine_server.session_is_active("", "") is False)
        os.environ.pop("TT_ENGINE_INTROSPECT_URL", None)
    finally:
        clear_auth_env()


def main() -> int:
    clear_auth_env()
    client = TestClient(engine_server.app)
    check_org_isolation(client)
    check_quota(client)
    check_introspection(client)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 -> {FAILURES}")
        return 1
    print("OK: D-02 服务化用户体系三项自测全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
