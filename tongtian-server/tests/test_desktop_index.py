#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面端市场索引适配层测试（D-07.4，标准库实现，无 pytest 依赖）。

覆盖：
- 索引形状：扁平数组，signaturePolicy / publishers / revokedVersions / revokedUrl 齐全；
  一份发布都没有时也要给出合法空数组（桌面端首次拉索引就是这条路）；
- 技能 sha256 是「zip 里那份 SKILL.md 的字节」摘要，不是 zip 摘要（给错就每次装都失配）；
- 技能产物路由回到的字节与索引摘要逐字节一致；撤销版本 410、未知版本 404、非法版本 400；
- 套件 sha256 是 zip 摘要，zipUrl 复用控制面下载路由且下载得到同一只包；
- sizeBytes 语义：技能给正文大小、套件给包大小（客户端拿它比自己的上限，给错会整份索引被拒）；
- 撤销回落：最新版本被撤后索引回落到未撤销版本，且 revokedVersions 如实声明；
- 产物取不到的条目不进索引（而不是给一条必然失败的下载），且不拖垮整份索引；
- 路由优先级：/api/v1/desktop/revoked.json 不被 /api/v1/{plural}/{item_id} 吞掉；
- 撤销清单是裸数组、不分页，能一次看全（控制面分页会让第 21 条之后再也禁不掉）。

用法: python tests/test_desktop_index.py
"""

import atexit
import hashlib
import io
import json
import os
import secrets
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="tt-desktop-index-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TT_SERVER_DATA_DIR"] = _TMP
os.environ["TT_SERVER_JWT_SECRET"] = "desktop-index-test-secret"
os.environ["TT_SERVER_ENV"] = "development"
os.environ["TT_ALLOW_REGISTRATION"] = "true"
os.environ["TT_SEED_DEMO"] = "false"
os.environ["TT_ADMIN_EMAIL"] = "admin@desktop.local"
os.environ["TT_ADMIN_PASSWORD"] = "admin-secret-123"
os.environ["TT_ADMIN_NICKNAME"] = "演练管理员"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

_passed = 0


def check(name: str, cond: bool, detail: str = ""):
    global _passed
    if not cond:
        print(f"  ✗ {name} {detail}")
        raise AssertionError(name)
    _passed += 1
    print(f"  ✓ {name}")


def payload(body: bytes) -> dict:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(NoRedirect)


def send(method: str, path: str, *, body: bytes | None = None, headers: dict | None = None):
    request = urllib.request.Request(BASE + path, data=body, headers=headers or {}, method=method)
    try:
        response = _opener.open(request, timeout=30)
        return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def http(method: str, path: str, *, json_body: dict | None = None, token: str | None = None):
    headers: dict[str, str] = {}
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return send(method, path, body=data, headers=headers)


def get(path: str):
    return http("GET", path)


def get_url(url: str):
    """按索引里给的完整地址取产物：适配层的价值就在这条地址必须真的可取。"""
    try:
        response = _opener.open(urllib.request.Request(url), timeout=30)
        return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def index(plural: str) -> dict:
    status, _, body = get(f"/api/v1/desktop/{plural}.json")
    if status != 200:
        raise AssertionError(f"桌面索引 {plural} 返回 {status}: {payload(body)}")
    return payload(body)


def rows_by_id(plural: str) -> dict[str, dict]:
    return {row["id"]: row for row in index(plural).get(plural, [])}


def encode_multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----tt" + secrets.token_hex(12)
    chunks: list[bytes] = []
    for key, value in fields.items():
        if value is None:
            continue
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


def upload(token: str, path: str, data: bytes, fields: dict | None = None):
    body, content_type = encode_multipart(
        fields or {}, {"package": ("package.zip", data, "application/zip")}
    )
    return send(
        "POST",
        path,
        body=body,
        headers={"Content-Type": content_type, "Authorization": f"Bearer {token}"},
    )


def make_zip(files: dict[str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def skill_body(item_id: str, version: str) -> bytes:
    """桌面端真正落盘的就是这份字节；中文与 emoji 决定了摘要只能按字节算。"""
    return (
        "---\n"
        f"name: {item_id}\n"
        "description: 桌面索引演练技能\n"
        f"version: {version}\n"
        "category: 效率\n"
        "tags: [演练]\n"
        "---\n\n"
        f"# {item_id} {version}\n\n正文里带中文与 emoji ✅，摘要必须按字节算。\n"
    ).encode("utf-8")


def skill_package(item_id: str, version: str) -> tuple[bytes, bytes]:
    """返回 (zip 字节, SKILL.md 字节)：测试要自己核对这两个摘要不是同一个东西。"""
    return make_zip({"SKILL.md": skill_body(item_id, version), "references/note.md": "# 备注\n"}), skill_body(item_id, version)


def publish_skill(token: str, item_id: str, data: bytes, **fields) -> dict:
    status, _, body = upload(token, "/api/v1/skills", data, fields)
    if status != 201:
        raise AssertionError(f"技能 {item_id} 发布失败 {status}: {payload(body)}")
    return payload(body)["item"]


def publish_skill_version(token: str, item_id: str, data: bytes, **fields) -> dict:
    status, _, body = upload(token, f"/api/v1/skills/{item_id}/versions", data, fields)
    if status != 201:
        raise AssertionError(f"技能 {item_id} 发版失败 {status}: {payload(body)}")
    return payload(body)["item"]


def publish_suite(token: str, data: bytes, **fields) -> dict:
    status, _, body = upload(token, "/api/v1/suites", data, fields)
    if status != 201:
        raise AssertionError(f"套件发布失败 {status}: {payload(body)}")
    return payload(body)["item"]


def revoke(token: str, item_type: str, item_id: str, version: str, reason: str = "演练撤销"):
    return http(
        "POST",
        f"/api/v1/admin/items/{item_type}/{item_id}/versions/{version}/revoke",
        json_body={"reason": reason},
        token=token,
    )


def unrevoke(token: str, item_type: str, item_id: str, version: str):
    return http(
        "POST",
        f"/api/v1/admin/items/{item_type}/{item_id}/versions/{version}/unrevoke",
        token=token,
    )


def package_path_of(item_type: str, item_id: str, version: str) -> str | None:
    conn = sqlite3.connect(os.path.join(_TMP, "market.db"))
    try:
        row = conn.execute(
            "SELECT package_path FROM item_versions "
            "WHERE item_type = ?1 AND item_id = ?2 AND version = ?3",
            (item_type, item_id, version),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def login(email: str, password: str) -> str:
    status, _, body = http(
        "POST", "/api/v1/auth/login", json_body={"email": email, "password": password}
    )
    if status != 200:
        raise AssertionError(f"登录失败 {status}: {payload(body)}")
    return payload(body)["accessToken"]


def register(email: str, nickname: str) -> str:
    status, _, body = http(
        "POST",
        "/api/v1/auth/register",
        json_body={"email": email, "password": "secret123", "nickname": nickname},
    )
    if status != 201:
        raise AssertionError(f"注册失败 {status}: {payload(body)}")
    return login(email, "secret123")


def start_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            status, _, _ = get("/health")
            if status == 200:
                return server, thread
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("服务端启动超时")


# ── 用例 ─────────────────────────────────────────────────────


def test_empty_shape():
    print("\n[桌面索引：一份都没发布时的形状]")
    for plural in ("skills", "suites"):
        doc = index(plural)
        check(
            f"{plural} 顶层键齐全",
            set(doc) == {"signaturePolicy", "publishers", plural, "revokedVersions", "revokedUrl"},
            str(sorted(doc)),
        )
        check(f"{plural} 空清单是数组", doc[plural] == [], str(doc[plural]))
        check(f"{plural} 撤销声明为空", doc["revokedVersions"] == [])
        check(f"{plural} 签名策略可选", doc["signaturePolicy"] == "optional")
        check(f"{plural} 不冒充发布者", doc["publishers"] == [])
        check(f"{plural} 撤销地址是绝对 URL", doc["revokedUrl"].startswith(f"{BASE}/"), doc["revokedUrl"])
    status, _, body = get("/api/v1/desktop/revoked.json")
    check("空撤销清单返回裸数组", status == 200 and json.loads(body) == [], str(status))


def test_skills_index(ctx):
    print("\n[桌面索引：技能形状与摘要口径]")
    item_id = "desktop-digest"
    zip_bytes, md_bytes = skill_package(item_id, "1.0.0")
    item = publish_skill(ctx["publisher"], item_id, zip_bytes, summary="桌面索引演练技能")
    check("发布返回同一标识", item["id"] == item_id, item["id"])

    doc = index("skills")
    rows = {row["id"]: row for row in doc["skills"]}
    check("索引含演练技能", item_id in rows, str(list(rows)))
    entry = rows[item_id]
    check(
        "必填字段完整",
        all(k in entry for k in ("id", "name", "version", "sha256", "skillMdUrl", "sizeBytes")),
        json.dumps(entry, ensure_ascii=False),
    )
    check("技能不给 zipUrl", "zipUrl" not in entry)
    check("版本为最新发布版", entry["version"] == "1.0.0", entry["version"])
    check("有可展示的 name", bool(entry["name"]), entry["name"])
    check(
        "摘要 64 位小写十六进制",
        len(entry["sha256"]) == 64 and all(c in "0123456789abcdef" for c in entry["sha256"]),
        entry["sha256"],
    )
    check(
        "技能摘要 = SKILL.md 字节摘要",
        entry["sha256"] == hashlib.sha256(md_bytes).hexdigest(),
        f"索引 {entry['sha256']} 正文 {hashlib.sha256(md_bytes).hexdigest()}",
    )
    check("技能摘要 ≠ zip 摘要", entry["sha256"] != hashlib.sha256(zip_bytes).hexdigest())
    check("sizeBytes 是正文大小", entry["sizeBytes"] == len(md_bytes), str(entry["sizeBytes"]))
    check(
        "产物地址是绝对 URL",
        entry["skillMdUrl"].startswith(f"{BASE}/api/v1/desktop/skills/"),
        entry["skillMdUrl"],
    )

    status, headers, fetched = get_url(entry["skillMdUrl"])
    check("SKILL.md 产物 200", status == 200, str(status))
    check("产物字节与发布时逐字节一致", fetched == md_bytes)
    check("产物摘要自证与索引一致", hashlib.sha256(fetched).hexdigest() == entry["sha256"])
    check("产物响应头带摘要", headers.get("X-Checksum-Sha256") == entry["sha256"])
    check("返回的是正文不是 zip", not fetched.startswith(b"PK"))


def test_skill_versions_and_revocation(ctx):
    print("\n[桌面索引：版本回落与撤销声明]")
    item_id = "desktop-revoke"
    zip_v1, md_v1 = skill_package(item_id, "1.0.0")
    zip_v2, md_v2 = skill_package(item_id, "1.1.0")
    publish_skill(ctx["publisher"], item_id, zip_v1, summary="撤销演练技能")
    publish_skill_version(ctx["publisher"], item_id, zip_v2)

    entry = rows_by_id("skills")[item_id]
    check("索引给最新版本", entry["version"] == "1.1.0", entry["version"])
    check("摘要随最新版本走", entry["sha256"] == hashlib.sha256(md_v2).hexdigest())
    status, _, _ = get(f"/api/v1/desktop/skills/{item_id}/1.0.0/SKILL.md")
    check("旧版本产物仍可按版本取到", status == 200, str(status))

    status, _, body = revoke(ctx["admin"], "skill", item_id, "1.1.0")
    check("管理员撤销最新版本", status == 200, payload(body).get("message", ""))

    doc = index("skills")
    entry = rows_by_id("skills")[item_id]
    check("索引回落到未撤销版本", entry["version"] == "1.0.0", entry["version"])
    check("回落后的摘要也回落", entry["sha256"] == hashlib.sha256(md_v1).hexdigest())
    declared = [row for row in doc["revokedVersions"] if row["id"] == item_id]
    check(
        "顶层 revokedVersions 声明被撤版本",
        [r["version"] for r in declared] == ["1.1.0"],
        json.dumps(declared, ensure_ascii=False),
    )
    check("撤销声明带类型与原因", all(r["kind"] == "skill" and r["reason"] for r in declared))

    status, _, body = get(f"/api/v1/desktop/skills/{item_id}/1.1.0/SKILL.md")
    check("已撤销版本产物 410", status == 410 and payload(body).get("code") == "ITEM_REVOKED", str(status))
    status, _, body = get(f"/api/v1/desktop/skills/{item_id}/9.9.9/SKILL.md")
    check("未知版本产物 404", status == 404 and payload(body).get("code") == "VERSION_NOT_FOUND", str(status))
    status, _, body = get(f"/api/v1/desktop/skills/{item_id}/1.0/SKILL.md")
    check("非法版本产物 400", status == 400 and payload(body).get("code") == "INVALID_VERSION", str(status))
    status, _, body = get("/api/v1/desktop/skills/bad%24id/1.0.0/SKILL.md")
    check("非法标识产物 400", status == 400 and payload(body).get("code") == "INVALID_ITEM_ID", str(status))

    # 全撤之后条目从索引消失，但撤销声明仍在——桌面端靠它把本机已装的旧版本禁用
    revoke(ctx["admin"], "skill", item_id, "1.0.0", "演练全撤")
    doc = index("skills")
    check("全部撤销后不再进索引", item_id not in rows_by_id("skills"))
    check(
        "全部撤销仍保留两条撤销声明",
        sorted(r["version"] for r in doc["revokedVersions"] if r["id"] == item_id) == ["1.0.0", "1.1.0"],
    )
    status, _, body = get(f"/api/v1/desktop/skills/{item_id}/1.0.0/SKILL.md")
    check("全撤后产物 410", status == 410 and payload(body).get("code") == "ITEM_REVOKED", str(status))

    for version in ("1.0.0", "1.1.0"):
        unrevoke(ctx["admin"], "skill", item_id, version)
    check("恢复后重新进索引", rows_by_id("skills")[item_id]["version"] == "1.1.0")


def test_suites_index(ctx):
    print("\n[桌面索引：套件产物与摘要]")
    display_name = "桌面演练套件"
    manifest = {
        "name": display_name,
        "version": "1.0.0",
        "description": "验证套件 zip 摘要口径的演练包。",
        "author": "@演练发布者",
        "category": "律师",
        "commands": [{"name": "demo-command", "description": "演练指令"}],
        "skills": [{"name": "demo-skill"}],
        "dataLinks": [{"name": "本地工作目录"}],
    }
    zip_bytes = make_zip(
        {
            "desktop-suite/plugin.json": json.dumps(manifest, ensure_ascii=False),
            "desktop-suite/README.md": "# 桌面演练套件\n",
            "desktop-suite/skills/demo-skill/SKILL.md": "---\nname: demo-skill\n---\n正文\n",
        }
    )
    # 套件条目 ID 由服务端从展示名派生（中文名走校验和分支），测试按返回值取而不是猜
    item_id = publish_suite(ctx["publisher"], zip_bytes, changelog="首次发布")["id"]

    entry = rows_by_id("suites")[item_id]
    check("套件索引含演练套件", entry["id"] == item_id)
    check("套件名取展示名", entry["name"] == display_name, entry["name"])
    check("套件不给 skillMdUrl", "skillMdUrl" not in entry)
    check(
        "zipUrl 复用控制面下载路由",
        entry["zipUrl"].startswith(f"{BASE}/api/v1/suites/{item_id}/download?version="),
        entry["zipUrl"],
    )
    check("套件摘要 = zip 摘要", entry["sha256"] == hashlib.sha256(zip_bytes).hexdigest())
    check("套件 sizeBytes = 包大小", entry["sizeBytes"] == len(zip_bytes), str(entry["sizeBytes"]))

    status, headers, fetched = get_url(entry["zipUrl"])
    check("zipUrl 直接可取包", status == 200, str(status))
    check("取回包与发布字节一致", fetched == zip_bytes)
    check("取回包摘要与索引一致", hashlib.sha256(fetched).hexdigest() == entry["sha256"])
    check("下载路由带版本头", headers.get("X-Item-Version") == "1.0.0", str(headers.get("X-Item-Version")))


def test_broken_package_skipped(ctx):
    print("\n[桌面索引：产物取不到就不列出来]")
    item_id = "desktop-broken"
    data, _ = skill_package(item_id, "1.0.0")
    publish_skill(ctx["publisher"], item_id, data, summary="包缺失演练")
    check("缺失前列在索引里", item_id in rows_by_id("skills"))

    rel = package_path_of("skill", item_id, "1.0.0")
    check("包路径可查", rel is not None, str(rel))
    os.remove(os.path.join(_TMP, rel.replace("/", os.sep)))

    doc = index("skills")
    check("包缺失后不再进索引", item_id not in {row["id"] for row in doc["skills"]})
    check("整份索引不是一坏全坏", len(doc["skills"]) > 0, str(len(doc["skills"])))
    status, _, body = get(f"/api/v1/desktop/skills/{item_id}/1.0.0/SKILL.md")
    check("缺失包的产物路由 410", status == 410 and payload(body).get("code") == "PACKAGE_MISSING", str(status))


def test_revoked_document(ctx):
    print("\n[桌面索引：撤销清单一次给全]")
    # 造 25 条撤销记录：控制面 /api/v1/revoked 每页 20 条，而桌面端只会拉 revokedUrl 一次。
    # 把 revokedUrl 直接指过去，第 21 条之后的已撤销版本就再也禁不掉——这条断言钉住那个事实。
    item_ids = [f"bulk-revoke-{i:02d}" for i in range(25)]
    for item_id in item_ids:
        data, _ = skill_package(item_id, "1.0.0")
        publish_skill(ctx["publisher"], item_id, data, summary="批量撤销演练")
        revoke(ctx["admin"], "skill", item_id, "1.0.0", "批量演练")

    status, _, body = get("/api/v1/desktop/revoked.json")
    check("撤销清单 200", status == 200, str(status))
    doc = json.loads(body.decode("utf-8"))
    check("撤销清单是裸数组", isinstance(doc, list), type(doc).__name__)
    target = set(item_ids)
    hit = [row for row in doc if row["id"] in target]
    check("撤销清单不被分页截断", len(hit) == 25, str(len(hit)))
    check(
        "清单字段与客户端别名对齐",
        all({"kind", "id", "version", "reason"} <= set(row) for row in hit),
        json.dumps(hit[0], ensure_ascii=False) if hit else "空",
    )
    check("被撤版本不进索引", not (target & set(rows_by_id("skills"))), str(sorted(target & set(rows_by_id("skills")))))

    control = payload(get("/api/v1/revoked")[2])
    check(
        "对照事实：控制面分页确实会漏",
        control.get("total", 0) >= 25 and len(control.get("items", [])) <= 20,
        f"total={control.get('total')} 本页={len(control.get('items', []))}",
    )

    for item_id in item_ids:
        unrevoke(ctx["admin"], "skill", item_id, "1.0.0")


def test_route_precedence():
    print("\n[桌面索引：路由优先级]")
    status, _, body = get("/api/v1/desktop/revoked.json")
    check("撤销清单不被 {plural}/{item_id} 吞掉", status == 200, str(status))
    check("撤销清单仍是裸数组（没被索引路由当成清单返回）", isinstance(json.loads(body), list))
    status, _, body = get("/api/v1/desktop/suites.json")
    check("套件索引同样不被吞掉", status == 200 and isinstance(payload(body).get("suites"), list), str(status))
    # 桌面端只有技能与套件两类：其余路径要么明确不存在，要么被市场通配抢走当条目 ID。
    # 这里钉住「不被抢走」——被抢走时客户端看到 400 INVALID_ITEM_ID，比 404 更难查。
    for path in ("/api/v1/desktop/mcps.json", "/api/v1/desktop/nonsense.json"):
        status, _, body = get(path)
        check(
            f"{path} 不被市场通配吞掉",
            status == 404 and payload(body).get("code") != "INVALID_ITEM_ID",
            f"{status} {payload(body).get('code')}",
        )


def run():
    server, thread = start_server()
    try:
        ctx = {
            "publisher": register("desktop-pub@test.local", "演练发布者"),
            "admin": login("admin@desktop.local", "admin-secret-123"),
        }
        test_empty_shape()
        test_skills_index(ctx)
        test_skill_versions_and_revocation(ctx)
        test_suites_index(ctx)
        test_broken_package_skipped(ctx)
        test_revoked_document(ctx)
        test_route_precedence()
        print(f"\n全部通过：{_passed} 项断言")
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    run()
