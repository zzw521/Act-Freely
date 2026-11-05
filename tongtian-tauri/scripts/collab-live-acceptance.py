#!/usr/bin/env python3
"""D-06 协作实时推送 · 双人真实验收驱动

起一个**真实运行**的账号服务（本地一次性数据目录），备好两个账号、一个项目、一个任务，
然后调用桌面端生产用的同一条 Rust 订阅代码（`collab::watch::run_watch`）跑三条验收：

  ① 负责人写入评论 → 成员端 1 秒内看到（量墙钟延迟，不是量轮询间隔）
  ② 成员端断线期间两人各写一条 → 按「已交付游标」重连后一条不丢、不重、不乱序
  ③ 成员被移出项目 → 真实服务端只发 `closed(membership)`，订阅就此终止不再重连

跑法（需要 tongtian-server 的依赖环境与 Rust 工具链）：

    python tongtian-tauri/scripts/collab-live-acceptance.py

对应测试：`src-tauri/src/collab/watch.rs::tests::live_two_person_sync_reconnect_and_membership`
（标记 `#[ignore]`，本脚本会带上 `--ignored` 跑它）

凭据边界：两个 access token 只经环境变量交给 cargo 子进程，不打印、不落盘；账号密码
是本机演练用的一次性值，服务数据写在系统临时目录的一次性目录里，退出即删——不碰任何
既有服务数据目录。
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
TAURI_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(TAURI_DIR)
SERVER_DIR = os.path.join(REPO_ROOT, "tongtian-server")
TEST_NAME = "collab::watch::tests::live_two_person_sync_reconnect_and_membership"

# Windows 控制台默认 GBK，中文日志会乱码、个别符号直接抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def ensure_server_dependencies() -> None:
    """用账号服务自己的虚拟环境跑（缺 uvicorn 时自动切过去）"""
    try:
        import uvicorn  # noqa: F401

        return
    except ImportError:
        pass
    for candidate in (
        os.path.join(SERVER_DIR, ".venv", "Scripts", "python.exe"),
        os.path.join(SERVER_DIR, ".venv", "bin", "python"),
    ):
        if os.path.exists(candidate):
            print(f"[驱动] 改用账号服务的环境重跑：{candidate}")
            os.execv(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]])
    sys.exit(
        "缺少 uvicorn，也没找到 tongtian-server/.venv；请先按项目说明装好服务端依赖再跑本脚本"
    )


ensure_server_dependencies()

import uvicorn  # noqa: E402

DATA_DIR = tempfile.mkdtemp(prefix="tt-d06-live-")
atexit.register(shutil.rmtree, DATA_DIR, ignore_errors=True)

# 一次性本地演练：数据目录、签发密钥都是这次的，退出即删，不影响任何既有服务
os.environ.update(
    {
        "TT_SERVER_DATA_DIR": DATA_DIR,
        "TT_SERVER_ENV": "development",
        "TT_SERVER_JWT_SECRET": "d06-live-drill-secret",
        "TT_ALLOW_REGISTRATION": "true",
        "TT_SEED_DEMO": "false",
    }
)
sys.path.insert(0, SERVER_DIR)
from app.main import app  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"


def http(method: str, path: str, body: dict | None = None, token: str | None = None):
    """返回 (status, data)；只用于本地演练服务，非 2xx 不抛（验收要看得见的状态码）"""
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8") or "{}"
        try:
            return err.code, json.loads(raw)
        except json.JSONDecodeError:
            return err.code, {"raw": raw}


def start_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        try:
            status, _ = http("GET", "/health")
            if status == 200:
                return server
        except Exception:
            pass
        time.sleep(0.1)
    sys.exit("账号服务没能在 20 秒内起来")


def must(condition: bool, label: str, detail: str = "") -> None:
    if not condition:
        sys.exit(f"[驱动] {label} 失败：{detail}")
    print(f"[驱动] {label} [OK]")


def build_fixture() -> dict:
    """两个账号 + 一个项目 + 一个任务；返回订阅测试需要的环境变量"""
    stamp = int(time.time() * 1000)
    password = "drill-secret-123"
    status, owner = http(
        "POST",
        "/api/v1/auth/register",
        {"email": f"d06-owner-{stamp}@test.local", "password": password, "nickname": "验收负责人"},
    )
    must(status == 201, "注册负责人账号", json.dumps(owner, ensure_ascii=False))
    status, member = http(
        "POST",
        "/api/v1/auth/register",
        {"email": f"d06-member-{stamp}@test.local", "password": password, "nickname": "验收成员"},
    )
    must(status == 201, "注册成员账号", json.dumps(member, ensure_ascii=False))
    owner_token, member_token = owner["accessToken"], member["accessToken"]

    status, body = http("POST", "/api/v1/projects", {"name": "D-06 双人实时验收"}, token=owner_token)
    must(status == 201, "创建演练项目", json.dumps(body, ensure_ascii=False))
    project_id = body["project"]["id"]

    status, body = http(
        "POST",
        f"/api/v1/projects/{project_id}/invites",
        {"email": member["profile"]["email"], "role": "member"},
        token=owner_token,
    )
    must(status == 201, "生成邀请", json.dumps(body, ensure_ascii=False))
    status, body = http(
        "POST", "/api/v1/projects/invites/accept", {"token": body["inviteCode"]}, token=member_token
    )
    must(status == 200 and body["project"]["myRole"] == "member", "成员加入项目", json.dumps(body))

    status, body = http(
        "POST", f"/api/v1/projects/{project_id}/tasks", {"title": "验收任务"}, token=owner_token
    )
    must(status == 201, "创建任务", json.dumps(body, ensure_ascii=False))
    task_id = body["task"]["id"]

    return {
        "TT_LIVE_COLLAB_BASE": BASE,
        "TT_LIVE_COLLAB_PROJECT": project_id,
        "TT_LIVE_COLLAB_TASK": task_id,
        "TT_LIVE_COLLAB_OWNER_TOKEN": owner_token,
        "TT_LIVE_COLLAB_MEMBER_TOKEN": member_token,
    }


def run_acceptance(env_extra: dict) -> int:
    bash = shutil.which("bash")
    if not bash:
        sys.exit("[驱动] 找不到 bash，无法调用 scripts/run-rust-tests.sh（Windows 请在 Git Bash 里跑）")
    cmd = [
        bash,
        os.path.join(TAURI_DIR, "scripts", "run-rust-tests.sh"),
        TEST_NAME,
        "--",
        "--ignored",
        "--nocapture",
        "--test-threads=1",
    ]
    # 令牌只进子进程环境，不进命令行、不进日志
    return subprocess.run(cmd, cwd=os.path.join(TAURI_DIR, "src-tauri"), env={**os.environ, **env_extra}).returncode


def main() -> int:
    print(f"[驱动] 账号服务：{BASE}（数据目录 {DATA_DIR}，一次性）")
    start_server()
    env_extra = build_fixture()
    print(f"[驱动] 演练项目 {env_extra['TT_LIVE_COLLAB_PROJECT']} · 任务 {env_extra['TT_LIVE_COLLAB_TASK']}")
    code = run_acceptance(env_extra)
    if code == 0:
        print("[驱动] 双人真实验收通过：1 秒内可见、断线补齐、被移出即终止三条都成立")
    else:
        print(f"[驱动] 双人真实验收未通过（cargo 退出码 {code}），别把这条记成已验证")
    return code


if __name__ == "__main__":
    sys.exit(main())
