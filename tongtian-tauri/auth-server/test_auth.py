#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auth-server 全链路冒烟测试（标准库实现，无 pytest/httpx 依赖）

覆盖：注册→回跳带 code → exchange 换 token → refresh 轮换 → logout 吊销；
以及错误分支：重复注册、密码错误、state 不匹配、非法 code、非法回调地址。

用法: python auth-server/test_auth.py
"""

import json
import os
import sys

# Windows GBK 控制台兼容：强制 UTF-8 输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# 测试隔离：临时 DB + 独立密钥（必须在 import server 前设置）
_tmp_dir = tempfile.mkdtemp(prefix="tt-auth-test-")
os.environ["TT_AUTH_DB"] = os.path.join(_tmp_dir, "auth-test.db")
os.environ["TT_AUTH_JWT_SECRET"] = "test-secret"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

PORT = 18299
BASE = f"http://127.0.0.1:{PORT}"
CALLBACK = "http://127.0.0.1:19999/callback"

_passed = 0


def check(name: str, cond: bool, detail: str = ""):
    global _passed
    if not cond:
        print(f"  ✗ {name} {detail}")
        raise AssertionError(name)
    _passed += 1
    print(f"  ✓ {name}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 302 不自动跟随，便于断言 Location


_opener = urllib.request.build_opener(NoRedirect)


def _call(req):
    """统一返回 (status, headers, body_bytes)；HTTPError(含 302)不当异常处理"""
    try:
        resp = _opener.open(req, timeout=5)
        return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def get(path: str):
    return _call(urllib.request.Request(f"{BASE}{path}"))


def post_form(path: str, form: dict):
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}", data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    return _call(req)


def post_json(path: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers={"Content-Type": "application/json"})
    return _call(req)


def parse_callback(location: str) -> dict:
    query = urllib.parse.urlsplit(location).query
    return {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}


def run():
    # ── 启动服务（后台线程） ──
    import uvicorn

    config = uvicorn.Config(server.app, host="127.0.0.1", port=PORT, log_level="error")
    uv_server = uvicorn.Server(config)
    thread = threading.Thread(target=uv_server.run, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            status, _, _ = get("/health")
            if status == 200:
                break
        except Exception:
            pass
        time.sleep(0.1)
    else:
        raise RuntimeError("auth-server 启动超时")
    print("auth-server 已启动（测试端口", PORT, "）")

    email = f"user{int(time.time())}@test.local"
    state = "st-12345"

    # ── 1. 健康检查与页面 ──
    status, _, body = get("/health")
    check("health ok", status == 200 and json.loads(body)["ok"] is True)
    check("登录页可访问", get("/auth/login?state=x&redirect_uri=")[0] == 200)
    check("注册页可访问", get("/auth/register")[0] == 200)

    # ── 2. 注册 → 302 回跳携带 code/state ──
    status, headers, _ = post_form(
        "/api/auth/register",
        {"nickname": "测试用户", "email": email, "password": "secret6", "confirm": "secret6",
         "state": state, "redirect_uri": CALLBACK},
    )
    check("注册成功返回 302", status == 302, f"实际 {status}")
    params = parse_callback(headers["Location"])
    check("回跳携带 code/state", bool(params.get("code")) and params.get("state") == state)
    code = params["code"]

    # ── 3. 重复注册被拒 ──
    status, _, _ = post_form("/api/auth/register",
                             {"nickname": "x", "email": email, "password": "secret6", "confirm": "secret6",
                              "state": state, "redirect_uri": CALLBACK})
    check("重复注册被拒(400)", status == 400, f"实际 {status}")

    # ── 4. 非法回调地址被拒 ──
    status, _, _ = post_form("/api/auth/register",
                             {"nickname": "x", "email": "evil@test.local", "password": "secret6", "confirm": "secret6",
                              "state": state, "redirect_uri": "http://evil.example.com/cb"})
    check("非法回调地址被拒(400)", status == 400, f"实际 {status}")

    # ── 5. exchange：state 不匹配被拒 ──
    status, _, _ = post_json("/api/auth/exchange", {"code": code, "state": "wrong-state"})
    check("state 不匹配被拒(400)", status == 400, f"实际 {status}")

    # ── 6. exchange：正确 code/state 换 token ──
    status, _, body = post_json("/api/auth/exchange", {"code": code, "state": state})
    check("exchange 成功", status == 200)
    body = json.loads(body)
    check("返回 profile 三件套", all(body["profile"].get(k) for k in ("uid", "nickname", "email")))
    check("accessToken 可验签", server.jwt_decode(body["accessToken"]) is not None)
    refresh1 = body["refreshToken"]

    # ── 7. 一次性码：重复 exchange 被拒 ──
    status, _, _ = post_json("/api/auth/exchange", {"code": code, "state": state})
    check("code 一次性使用(400)", status == 400, f"实际 {status}")

    # ── 8. 登录：密码错误被拒 ──
    status, _, _ = post_form("/api/auth/login",
                             {"email": email, "password": "wrong-pw", "state": state, "redirect_uri": CALLBACK})
    check("密码错误被拒(401)", status == 401, f"实际 {status}")

    # ── 9. 登录：正确密码 → 302 ──
    status, headers, _ = post_form("/api/auth/login",
                                   {"email": email, "password": "secret6", "state": state, "redirect_uri": CALLBACK})
    check("登录成功 302", status == 302, f"实际 {status}")
    code2 = parse_callback(headers["Location"])["code"]
    status, _, body = post_json("/api/auth/exchange", {"code": code2, "state": state})
    check("登录后 exchange 成功", status == 200 and json.loads(body)["ok"] is True)

    # ── 10. refresh 轮换：新 token 生效，旧 token 吊销 ──
    status, _, body = post_json("/api/auth/refresh", {"refreshToken": refresh1})
    body = json.loads(body)
    check("refresh 轮换成功", status == 200 and body["ok"] is True and body["refreshToken"] != refresh1)
    refresh2 = body["refreshToken"]
    status, _, _ = post_json("/api/auth/refresh", {"refreshToken": refresh1})
    check("旧 refresh 已吊销(401)", status == 401, f"实际 {status}")

    # ── 11. logout：吊销后 refresh 失败 ──
    status, _, body = post_json("/api/auth/logout", {"refreshToken": refresh2})
    check("logout ok", status == 200 and json.loads(body)["ok"] is True)
    status, _, _ = post_json("/api/auth/refresh", {"refreshToken": refresh2})
    check("logout 后 refresh 被拒(401)", status == 401, f"实际 {status}")

    # ── 12. logout 幂等 ──
    status, _, body = post_json("/api/auth/logout", {"refreshToken": refresh2})
    check("logout 幂等", status == 200 and json.loads(body)["ok"] is True)

    print(f"\n全部通过：{_passed} 项断言")
    uv_server.should_exit = True


if __name__ == "__main__":
    run()
