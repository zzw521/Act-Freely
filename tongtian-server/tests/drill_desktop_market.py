#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D-07.4 真机演练的服务端侧：起一个真的 tongtian-server 并把演练内容播种好。

这不是自动化测试的一部分，而是**演练台**：它起的是一条真实的 HTTP 服务，
桌面客户端（`src-tauri/src/market`）对着它走「列表 → 安装 → 检查更新 → 升级 → 撤销 → 卸载」。
用真实服务而不是本机假服务，是因为要验的正是「服务端索引适配层给的形状，客户端收不收」
——摘要口径、绝对地址、撤销清单能不能一次拉全，这些在本机 mock 上永远测不出来。

用法（一个终端）：

    python tests/drill_desktop_market.py --port 8731 --done-file /tmp/tt-drill.done

它会打印一行 `DRILL_READY skillUrl=... suiteUrl=...`。另一个终端跑：

    TT_MARKET_DRILL_SKILL_URL=http://127.0.0.1:8731/api/v1/desktop/skills.json \\
    TT_MARKET_DRILL_SUITE_URL=http://127.0.0.1:8731/api/v1/desktop/suites.json \\
    bash ../scripts/run-rust-tests.sh real_server_market_drill -- --ignored

演练内容（正文与客户端里的常量必须逐字节一致，见 `DRILL_*`）：

- `drill-skill`：1.0.0 与 1.1.0 两版都在，索引应给 1.1.0 —— 用来验安装与一键升级；
- `drill-revoked`：1.1.0 已撤销，索引应回落到 1.0.0 并在 revokedVersions 里声明 1.1.0；
- `drill-gone`：唯一版本已撤销，**不该出现在索引里**，客户端装不上；
- `drill-suite`：套件 zip，用来验 zipUrl 复用下载路由与「包摘要」口径。
"""

import argparse
import hashlib
import io
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile

# Windows 控制台默认 GBK，`✓` 这类字符会直接把 print 炸掉（第一轮演练就死在这）。
# 演练台的输出是给人看的日志，不是协议，用 UTF-8 + replace 兜住即可。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DRILL_OLD_BODY = (
    "---\nname: drill-skill\ndescription: 市场真机演练\ncategory: 效率\n---\n\n"
    "# 演练技能 旧版\n\n这一版用来被一键升级换掉。\n"
)
DRILL_NEW_BODY = (
    "---\nname: drill-skill\ndescription: 市场真机演练\ncategory: 效率\n---\n\n"
    "# 演练技能 新版\n\n升级后盘上应该是这段，且摘要与索引一致。\n"
)
DRILL_REVOKED_V1 = (
    "---\nname: drill-revoked\ndescription: 撤销演练\ncategory: 效率\n---\n\n# 撤销演练 1.0.0\n"
)
DRILL_REVOKED_V2 = (
    "---\nname: drill-revoked\ndescription: 撤销演练\ncategory: 效率\n---\n\n# 撤销演练 1.1.0 已被撤销\n"
)
DRILL_GONE_BODY = (
    "---\nname: drill-gone\ndescription: 全撤演练\ncategory: 效率\n---\n\n# 全撤演练 1.0.0\n"
)
DRILL_SUITE_NAME = "Drill Suite"

PUBLISHER_EMAIL = "drill-pub@desktop.local"
ADMIN_EMAIL = "drill-admin@desktop.local"
SECRET = "drill-secret-123"


def encode_multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----tt" + secrets.token_hex(12)
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )
    for key, (filename, content, content_type) in files.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
        )
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class Drill:
    def __init__(self, base: str):
        self.base = base

    def call(self, method: str, path: str, *, json_body=None, data: bytes = None,
             content_type: str | None = None, token: str | None = None):
        headers: dict[str, str] = {}
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif data is not None:
            body = data
            headers["Content-Type"] = content_type or "application/octet-stream"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=30)
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                return exc.code, {"raw": raw[:200].decode("utf-8", "replace")}

    def expect(self, name: str, status: int, body: dict, wanted: int):
        if status != wanted:
            raise SystemExit(f"{name} 失败：HTTP {status} {body}")
        print(f"  ✓ {name}")


def skill_zip(item_id: str, version: str, body: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", body)
        archive.writestr("references/note.md", f"# {item_id} {version}\n")
    return buffer.getvalue()


def suite_zip() -> bytes:
    manifest = {
        "name": DRILL_SUITE_NAME,
        "version": "1.0.0",
        "description": "真机演练用的最小套件包。",
        "author": "@演练发布者",
        "category": "效率",
        "commands": [{"name": "drill-command", "description": "演练指令"}],
        "skills": [{"name": "drill-inner"}],
        "dataLinks": [{"name": "本地工作目录"}],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("drill-suite/plugin.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr("drill-suite/README.md", "# Drill Suite\n")
        archive.writestr("drill-suite/skills/drill-inner/SKILL.md",
                         "---\nname: drill-inner\n---\n内层技能\n")
    return buffer.getvalue()


def seed(base: str) -> None:
    drill = Drill(base)
    print("\n[播种演练内容]")
    status, body = drill.call("POST", "/api/v1/auth/register", json_body={
        "email": PUBLISHER_EMAIL, "password": SECRET, "nickname": "演练发布者"})
    if status not in (201, 409):
        raise SystemExit(f"发布者注册失败：{status} {body}")
    status, body = drill.call("POST", "/api/v1/auth/login", json_body={
        "email": PUBLISHER_EMAIL, "password": SECRET})
    drill.expect("发布者登录", status, body, 200)
    publisher = body["accessToken"]
    status, body = drill.call("POST", "/api/v1/auth/login", json_body={
        "email": ADMIN_EMAIL, "password": SECRET})
    drill.expect("管理员登录", status, body, 200)
    admin = body["accessToken"]

    def publish(item_id: str, version: str, body_text: str, first: bool):
        data = skill_zip(item_id, version, body_text)
        path = "/api/v1/skills" if first else f"/api/v1/skills/{item_id}/versions"
        # 版本号必须显式给：演练正文里没有 front-matter `version:`，服务端 `_resolve_version`
        # 对新版本拿不到声明值就直接 VERSION_REQUIRED（首轮演练就是死在这里）。
        payload, ctype = encode_multipart(
            {"summary": f"{item_id} 演练条目", "changelog": version, "version": version},
            {"package": (f"{item_id}.zip", data, "application/zip")},
        )
        status, resp = drill.call("POST", path, data=payload, content_type=ctype, token=publisher)
        drill.expect(f"发布 {item_id}@{version}", status, resp, 201)

    publish("drill-skill", "1.0.0", DRILL_OLD_BODY, True)
    publish("drill-skill", "1.1.0", DRILL_NEW_BODY, False)
    publish("drill-revoked", "1.0.0", DRILL_REVOKED_V1, True)
    publish("drill-revoked", "1.1.0", DRILL_REVOKED_V2, False)
    publish("drill-gone", "1.0.0", DRILL_GONE_BODY, True)

    payload, ctype = encode_multipart(
        {"changelog": "首次发布"},
        {"package": ("drill-suite.zip", suite_zip(), "application/zip")},
    )
    status, resp = drill.call("POST", "/api/v1/suites", data=payload, content_type=ctype, token=publisher)
    drill.expect("发布 drill-suite", status, resp, 201)
    print(f"    套件条目标识：{resp['item']['id']}")

    status, resp = drill.call("POST", "/api/v1/admin/items/skill/drill-revoked/versions/1.1.0/revoke",
                              json_body={"reason": "演练撤销：正文有误"}, token=admin)
    drill.expect("撤销 drill-revoked@1.1.0", status, resp, 200)
    status, resp = drill.call("POST", "/api/v1/admin/items/skill/drill-gone/versions/1.0.0/revoke",
                              json_body={"reason": "演练全撤"}, token=admin)
    drill.expect("撤销 drill-gone@1.0.0（唯一版本）", status, resp, 200)

    # 自检：索引与撤销清单先自己看一眼，别让客户端去发现明显的问题
    status, skills = drill.call("GET", "/api/v1/desktop/skills.json")
    drill.expect("桌面技能索引可读", status, skills, 200)
    listed = {row["id"]: row for row in skills.get("skills", [])}
    for item_id, want_version in (("drill-skill", "1.1.0"), ("drill-revoked", "1.0.0")):
        if listed.get(item_id, {}).get("version") != want_version:
            raise SystemExit(f"索引版本不对：{item_id} → {listed.get(item_id)}")
    if "drill-gone" in listed:
        raise SystemExit("全撤条目仍出现在索引里，适配层的撤销过滤没生效")
    status, revoked = drill.call("GET", "/api/v1/desktop/revoked.json")
    drill.expect("桌面撤销清单可读", status, revoked if isinstance(revoked, dict) else {}, 200)
    print(f"    撤销清单 {len(revoked)} 条（裸数组）")
    print(
        "    摘要口径：drill-skill 索引给的是 "
        f"{listed['drill-skill']['sha256'][:12]}…，SKILL.md 字节摘要是 "
        f"{hashlib.sha256(DRILL_NEW_BODY.encode('utf-8')).hexdigest()[:12]}…"
    )
    if listed["drill-skill"]["sha256"] != hashlib.sha256(DRILL_NEW_BODY.encode("utf-8")).hexdigest():
        raise SystemExit("索引摘要与新版正文不一致：客户端必然 MARKET_HASH_MISMATCH")


def wait_for_done(path: str, seconds: int) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if os.path.exists(path):
            return
        time.sleep(0.5)
    raise SystemExit(f"等演练收尾信号超时（{seconds}s）：{path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="D-07.4 市场真机演练台")
    parser.add_argument("--port", type=int, default=8731)
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--done-file", required=True, help="客户端跑完后由外部 touch 这个文件")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    data_dir = args.data_dir or os.path.join(
        os.environ.get("TEMP", "/tmp"), f"tt-market-drill-{os.getpid()}"
    )
    os.makedirs(data_dir, exist_ok=True)
    os.environ["TT_SERVER_DATA_DIR"] = os.path.abspath(data_dir)
    os.environ["TT_SERVER_JWT_SECRET"] = "drill-local-secret-not-for-production"
    os.environ["TT_SERVER_ENV"] = "development"
    os.environ["TT_ALLOW_REGISTRATION"] = "true"
    os.environ["TT_SEED_DEMO"] = "false"
    os.environ["TT_ADMIN_EMAIL"] = ADMIN_EMAIL
    os.environ["TT_ADMIN_PASSWORD"] = SECRET
    os.environ["TT_ADMIN_NICKNAME"] = "演练管理员"
    if os.path.exists(args.done_file):
        os.remove(args.done_file)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import uvicorn

    from app.main import app

    config = uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{args.port}"
    for _ in range(150):
        try:
            if urllib.request.urlopen(base + "/health", timeout=2).status == 200:
                break
        except Exception:
            pass
        time.sleep(0.1)
    else:
        raise SystemExit("服务端启动超时")

    seed(base)
    print(
        f"DRILL_READY skillUrl={base}/api/v1/desktop/skills.json "
        f"suiteUrl={base}/api/v1/desktop/suites.json dataDir={os.path.abspath(data_dir)}"
    )
    sys.stdout.flush()
    try:
        wait_for_done(args.done_file, args.timeout)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        print("演练台已关闭")


if __name__ == "__main__":
    main()
