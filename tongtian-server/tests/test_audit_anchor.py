#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""D-27 审计签名锚点、可选远端归档与合规报告导出。

覆盖三件哈希链本身做不到的事：
1) 锚点能抓住「删行后把后续链整体重算」——逐行验链会放过它，锚点不会；
2) 远端归档把锚点推给外部存证方，本机的假归档端会独立验签再收货；
3) 合规报告把链状态、锚点状态、期内活动、敏感动作与「本报告证明不了什么」汇成
   JSON 与 Markdown 两种口径。

用法: python tests/test_audit_anchor.py
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
# 本文件的 `http()` 辅助函数会占用 `http` 这个名字，所以这里直接导入类而不是走 http.server 属性链
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="tt-anchor-")
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)
os.environ["TT_SERVER_DATA_DIR"] = _TMP
os.environ["TT_SERVER_JWT_SECRET"] = "anchor-test-secret"
os.environ["TT_SERVER_ENV"] = "development"
os.environ["TT_ALLOW_REGISTRATION"] = "true"
os.environ["TT_SEED_DEMO"] = "false"
os.environ["TT_ADMIN_EMAIL"] = "admin@test.local"
os.environ["TT_ADMIN_PASSWORD"] = "admin-secret-123"
os.environ["TT_ADMIN_NICKNAME"] = "测试管理员"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import uvicorn  # noqa: E402

from app import audit_anchor  # noqa: E402  （测试里临时压缩单次推送上限）
from app.audit_chain import GENESIS, digest  # noqa: E402  （「本机攻击者」拿得到源码）
from app.config import settings  # noqa: E402
from app.keys import key_store  # noqa: E402  （拿同一把私钥签「别的类型」的签名做重放）
from app.main import app  # noqa: E402

_passed = 0
CHAIN_LEN = 64
ARCHIVE_TOKEN = "archive-test-token"


def check(name: str, cond: bool, detail: str = ""):
    global _passed
    if not cond:
        print(f"  ✗ {name} {detail}")
        raise AssertionError(name)
    _passed += 1
    print(f"  ✓ {name}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORT = free_port()
BASE = f"http://127.0.0.1:{PORT}"
_opener = urllib.request.build_opener()


def _headers(message) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in message.items()}


def http(method: str, path: str, *, json_body: dict | None = None, token: str | None = None):
    headers = {"Accept": "application/json"}
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        response = _opener.open(request, timeout=30)
        return response.status, _headers(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, _headers(exc.headers or {}), exc.read()


def payload(body: bytes) -> dict:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


def register(email: str, nickname: str) -> dict:
    status, _, body = http(
        "POST",
        "/api/v1/auth/register",
        json_body={"email": email, "password": "secret123", "nickname": nickname},
    )
    data = payload(body)
    if status != 201:
        raise AssertionError(f"注册失败 {status}: {data}")
    return data


def login(email: str, password: str = "secret123") -> str:
    status, _, body = http(
        "POST", "/api/v1/auth/login", json_body={"email": email, "password": password}
    )
    data = payload(body)
    if status != 200:
        raise AssertionError(f"登录失败 {status}: {data}")
    return data["accessToken"]


def start_server():
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            if http("GET", "/health")[0] == 200:
                return server
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("服务端启动超时")


# ── 假归档存证端：独立验签后才收货 ──────────────────────────


def _b64url_decode(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _jwk_to_public_key(jwk: dict):
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

    modulus = int.from_bytes(_b64url_decode(jwk["n"]), "big")
    exponent = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    return RSAPublicNumbers(exponent, modulus).public_key()


def canonical(value: dict) -> str:
    """测试侧独立实现一份规范化：与产品同源就等于没测。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def anchor_digest(fields: dict) -> str:
    return hashlib.sha256(canonical(fields).encode("utf-8")).hexdigest()


class ArchiveSink(BaseHTTPRequestHandler):
    """只认带令牌的 POST，并且先用 JWKS 公钥独立验签再落账。"""

    server_version = "TTFakeArchive/1"

    def do_POST(self):  # noqa: N802 - http.server 约定的方法名
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        auth = str(self.headers.get("authorization") or "")
        if auth != f"Bearer {ARCHIVE_TOKEN}":
            self._reply(401, {"error": "missing or wrong bearer token"})
            return
        if self.path != "/v1/audit-anchors":
            self._reply(404, {"error": "unknown path"})
            return
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._reply(400, {"error": "body is not json"})
            return
        anchor = body.get("anchor") or {}
        fields = {
            "fromId": anchor.get("fromId"),
            "toId": anchor.get("toId"),
            "entries": anchor.get("entries"),
            "headHash": anchor.get("headHash"),
        }
        verdict = self.server.judge(anchor, body)
        if not verdict["ok"]:
            self.server.rejected.append({"reason": verdict["reason"], "anchor": anchor})
            self._reply(422, {"error": verdict["reason"]})
            return
        ref = f"sink-{len(self.server.received) + 1:04d}"
        self.server.received.append(
            {"ref": ref, "anchor": anchor, "rows": body.get("rows"),
             "coverage": body.get("coverage"), "jwksPath": body.get("jwksPath"),
             "raw": raw.decode("utf-8", errors="replace")}
        )
        self._reply(200, {"ref": ref})

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # 静音，别把测试输出搅乱
        pass


class SinkServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, jwks: dict) -> None:
        super().__init__(("127.0.0.1", port), ArchiveSink)
        self.keys = {item["kid"]: _jwk_to_public_key(item) for item in jwks.get("keys") or []}
        self.received: list[dict] = []
        self.rejected: list[dict] = []

    def judge(self, anchor: dict, body: dict) -> dict:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        token = str(anchor.get("signature") or "")
        parts = token.split(".")
        if len(parts) != 3:
            return {"ok": False, "reason": "锚点签名格式不合法"}
        try:
            header = json.loads(_b64url_decode(parts[0]))
            claims = json.loads(_b64url_decode(parts[1]))
            public = self.keys.get(str(header.get("kid")))
            if public is None:
                return {"ok": False, "reason": "签名密钥不在 JWKS 中"}
            public.verify(
                _b64url_decode(parts[2]),
                f"{parts[0]}.{parts[1]}".encode("ascii"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except Exception as exc:  # noqa: BLE001 - 对端只需要给出原因
            return {"ok": False, "reason": f"验签失败：{exc.__class__.__name__}"}
        if claims.get("aud") != "tongtian-audit" or claims.get("typ") != "audit-anchor":
            return {"ok": False, "reason": "签名受众或类型不符"}
        fields = {
            "fromId": anchor.get("fromId"),
            "toId": anchor.get("toId"),
            "entries": anchor.get("entries"),
            "headHash": anchor.get("headHash"),
        }
        for key, value in fields.items():
            if claims.get(key) != value:
                return {"ok": False, "reason": f"锚点字段与签名不符：{key}"}
        if claims.get("digest") != anchor_digest(fields):
            return {"ok": False, "reason": "锚点摘要与签名不符"}
        rows = body.get("rows") or []
        if not rows:
            return {"ok": False, "reason": "锚点没有带上任何审计行"}
        return {"ok": True, "reason": "验签通过"}


# ── 本机攻击者：删一行再把整条链重算 ────────────────────────


def forge_recompute(db_path: str, drop_id: int, restore: dict[int, str]) -> None:
    """模拟「同时持有库文件与源码」的本机用户：抹掉一行，然后从创世哈希重建全链。

    重算之后逐行验链会全盘通过——这正是锚点存在的理由，不是攻击者的成功。
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("DELETE FROM admin_audit WHERE id = ?1", (drop_id,))
        for row_id, action in restore.items():
            conn.execute("UPDATE admin_audit SET action = ?1 WHERE id = ?2", (action, row_id))
        prev = GENESIS
        rows = conn.execute("SELECT * FROM admin_audit ORDER BY id ASC").fetchall()
        for row in rows:
            value = digest(
                prev,
                int(row["at_ms"]),
                str(row["actor_uid"]),
                str(row["actor_role"]),
                str(row["action"]),
                str(row["object"]),
                str(row["before_json"] or "{}"),
                str(row["after_json"] or "{}"),
            )
            conn.execute(
                "UPDATE admin_audit SET prev_hash = ?1, entry_hash = ?2 WHERE id = ?3",
                (prev, value, int(row["id"])),
            )
            prev = value
        conn.commit()
    finally:
        conn.close()


def tamper_action(db_path: str, row_id: int, value: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE admin_audit SET action = ?1 WHERE id = ?2", (value, row_id))
    conn.commit()
    conn.close()


def patch_anchor_row(db_path: str, seq: int, column: str, value: str) -> str:
    """改锚点行的一列，返回原值供恢复。只允许这两列，避免误伤别的字段。"""
    assert column in ("signature", "head_hash")
    conn = sqlite3.connect(db_path)
    previous = str(conn.execute(
        f"SELECT {column} FROM audit_anchors WHERE seq = ?1", (int(seq),)
    ).fetchone()[0])
    conn.execute(f"UPDATE audit_anchors SET {column} = ?1 WHERE seq = ?2", (value, int(seq)))
    conn.commit()
    conn.close()
    return previous


def set_archive(url: str, token: str = "") -> None:
    """frozen dataclass 单例：测试用 object.__setattr__ 覆盖运行期配置。"""
    object.__setattr__(settings, "audit_archive_url", url)
    object.__setattr__(settings, "audit_archive_token", token)


def main() -> None:
    server = start_server()
    sink: SinkServer | None = None
    sink_port = free_port()
    try:
        admin_token = login("admin@test.local", "admin-secret-123")
        admin_uid = str(payload(http("GET", "/api/v1/auth/me", token=admin_token)[2])["profile"]["uid"])

        stamp = int(time.time() * 1000)
        auditor = register(f"auditor-{stamp}@test.local", "审计员")
        operator = register(f"operator-{stamp}@test.local", "运维")
        ordinary = register(f"plain-{stamp}@test.local", "普通用户")
        for target, role in ((auditor, "auditor"), (operator, "operator")):
            status, _, body = http(
                "PATCH",
                f"/api/v1/admin/users/{target['profile']['uid']}",
                json_body={"role": role},
                token=admin_token,
            )
            check(f"提升为{role}", status == 200, f"{status} {body}")
        auditor_token = login(f"auditor-{stamp}@test.local")
        operator_token = login(f"operator-{stamp}@test.local")

        print("\n[还没有锚点]")
        status, _, body = http("GET", "/api/v1/admin/audit/anchor/verify", token=auditor_token)
        data = payload(body)
        check("审计员可读锚点巡检", status == 200 and data.get("anchors") == 0, f"{status} {body[:200]}")
        check("无锚点时如实说明证据不足", "无法证明未被整体重算" in str(data.get("statement")), str(data))
        check("链本身是自洽的", data.get("chain", {}).get("ok") is True, str(data.get("chain"))[:200])

        status, _, body = http("GET", "/api/v1/admin/audit/compliance", token=ordinary["accessToken"])
        check("普通用户读合规报告 403", status == 403 and payload(body).get("code") == "FORBIDDEN", f"{status}")
        status, _, body = http("GET", "/api/v1/admin/audit/compliance?days=0", token=auditor_token)
        check("days 越界 400", status == 400 and payload(body).get("code") == "INVALID_REQUEST", f"{status} {body[:160]}")
        status, _, body = http("GET", "/api/v1/admin/audit/compliance?days=999", token=auditor_token)
        check("days 超上限 400", status == 400, f"{status} {body[:160]}")

        print("\n[签发权限]")
        status, _, body = http("POST", "/api/v1/admin/audit/anchor", token=ordinary["accessToken"])
        check("普通用户签发锚点 403", status == 403, f"{status} {body[:160]}")
        status, _, body = http("POST", "/api/v1/admin/audit/anchor", token=auditor_token)
        check("审计员不能签锚点", status == 403, f"{status} {body[:160]}")
        status, _, body = http("POST", "/api/v1/admin/audit/anchor")
        check("未登录签发锚点 401", status == 401, f"{status}")

        print("\n[锚点签发]")
        status, _, body = http("GET", "/api/v1/admin/audit/verify", token=auditor_token)
        chain_before = payload(body)
        status, _, body = http("POST", "/api/v1/admin/audit/anchor", token=operator_token)
        data = payload(body)
        anchor_1 = data.get("anchor") or {}
        check("运维可签锚点", status == 200 and anchor_1.get("seq") == 1, f"{status} {body[:200]}")
        check("锚点覆盖签发前的全链",
              anchor_1.get("entries") == chain_before["entries"]
              and anchor_1.get("toId") == chain_before["lastId"]
              and anchor_1.get("headHash") == chain_before["headHash"],
              json.dumps(anchor_1, ensure_ascii=False)[:220])
        check("锚点带 RS256 签名与 kid",
              len(str(anchor_1.get("signature") or "").split(".")) == 3
              and bool(anchor_1.get("signingKid")),
              str(anchor_1.get("signingKid")))
        check("未配置远端归档时状态是 local 而非已归档",
              anchor_1.get("archiveState") == "local"
              and data.get("archive", {}).get("enabled") is False,
              json.dumps(data.get("archive"), ensure_ascii=False))

        status, _, body = http("GET", "/api/v1/admin/audit/anchors", token=auditor_token)
        listed = payload(body)
        check("锚点列表可读",
              status == 200 and len(listed.get("anchors") or []) == 1
              and listed["anchors"][0]["headHash"] == anchor_1["headHash"],
              f"{status} {body[:200]}")
        check("列表不回显签名字符串", "signature" not in json.dumps(listed.get("anchors") or []), str(listed)[:200])

        status, _, body = http("GET", "/api/v1/admin/audit/anchor/verify", token=auditor_token)
        verified = payload(body)
        check("单锚点巡检可信",
              verified.get("anchors") == 1 and verified.get("untrusted") == 0
              and verified["items"][0]["chainTrusted"] is True,
              json.dumps(verified, ensure_ascii=False)[:240])
        check("巡检说明全部可信", "全部锚点可信" in str(verified.get("statement")), str(verified.get("statement")))

        print("\n[锚点动作本身入链]")
        status, _, body = http("GET", "/api/v1/admin/audit?after=0&limit=1000", token=admin_token)
        entries = payload(body).get("entries") or []
        anchor_entry = next((e for e in entries if e["action"] == "audit.anchor"), {})
        check("签发锚点写了一条审计", bool(anchor_entry), json.dumps(entries[-1], ensure_ascii=False)[:200])
        check("审计记录签发人与角色",
              anchor_entry.get("actor") == admin_uid or anchor_entry.get("actor") == operator["profile"]["uid"],
              str(anchor_entry.get("actor")))
        check("签发审计带链头与摘要",
              len(str((anchor_entry.get("after") or {}).get("digest") or "")) == CHAIN_LEN
              and (anchor_entry.get("after") or {}).get("entries") == anchor_1["entries"],
              json.dumps(anchor_entry.get("after"), ensure_ascii=False))
        check("角色变更记的是操作人的角色",
              anchor_entry.get("actorRole") in ("operator", "admin"), str(anchor_entry.get("actorRole")))

        print("\n[合规报告]")
        status, _, body = http("GET", "/api/v1/admin/audit/compliance?days=30", token=auditor_token)
        report = payload(body)
        check("审计员可读合规报告", status == 200 and report.get("ok") is True, f"{status}")
        check("报告带合同版本与周期",
              report.get("contractVersion") == "audit-compliance.v1"
              and report["period"]["days"] == 30
              and report["period"]["toMs"] - report["period"]["fromMs"] == 30 * 86400_000,
              json.dumps(report.get("period"), ensure_ascii=False))
        check("有锚点后结论可信", report["verdict"]["trustworthy"] is True, str(report["verdict"]))
        check("活动统计含动作与操作人",
              any(item["action"] == "user.role" for item in report["activity"]["byAction"])
              and any(item["actor"] == admin_uid for item in report["activity"]["byActor"]),
              json.dumps(report["activity"]["byAction"], ensure_ascii=False)[:200])
        check("分桶按 UTC 标注", report["activity"]["dayBasis"] == "UTC", str(report["activity"]["dayBasis"]))
        check("期内行号区间连续", report["activity"]["idSpanCoversPeriod"] is True, json.dumps(report["activity"])[:200])
        check("敏感清单含角色变更",
              any(item["action"] == "user.role" and item["label"] for item in report["sensitive"]["byAction"]),
              json.dumps(report["sensitive"]["byAction"], ensure_ascii=False)[:200])
        check("锚点签发也在敏感清单里",
              any(item["action"] == "audit.anchor" for item in report["sensitive"]["byAction"]),
              json.dumps(report["sensitive"]["byAction"], ensure_ascii=False)[:200])
        check("脱敏口径取自写入侧",
              report["coverage"]["redaction"]["appliedOnWrite"] is True
              and "token" in report["coverage"]["redaction"]["keys"],
              json.dumps(report["coverage"]["redaction"], ensure_ascii=False))
        check("报告点名引擎/桌面本地审计不在范围内",
              any("引擎" in item for item in report["coverage"]["notCovered"]),
              json.dumps(report["coverage"]["notCovered"], ensure_ascii=False))
        check("未配置归档是一条 warn 而不是 error",
              any(item["code"] == "NO_REMOTE_ARCHIVE" and item["level"] == "warn"
                  for item in report["findings"])
              and report["verdict"]["errorCount"] == 0,
              json.dumps(report["findings"], ensure_ascii=False)[:260])

        status, headers, body = http("GET", "/api/v1/admin/audit/compliance/export?days=30", token=auditor_token)
        markdown = body.decode("utf-8")
        check("Markdown 导出 200 且是 text/markdown",
              status == 200 and headers.get("content-type", "").startswith("text/markdown"),
              f"{status} {headers.get('content-type')}")
        check("导出带文件名与链头",
              'attachment; filename="audit-compliance-' in headers.get("content-disposition", "")
              and headers.get("x-tt-audit-head") == report["integrity"]["headHash"],
              str(headers)[:240])
        check("导出头声明可信", headers.get("x-tt-audit-trustworthy") == "true", str(headers)[:200])
        check("导出含六个板块",
              all(text in markdown for text in
                  ("## 一、链条完整性", "## 二、签名锚点与归档", "## 三、期内活动",
                   "## 四、敏感动作", "## 五、问题清单", "## 六、本报告证明不了什么")),
              markdown[:120])
        check("导出写明证明不了什么", "连锚点一起重造" in markdown, markdown[-600:])
        check("导出不含签名明文", "eyJ" not in markdown, markdown[:200])

        print("\n[第二个锚点覆盖增量]")
        status, _, body = http(
            "PATCH", f"/api/v1/admin/users/{ordinary['profile']['uid']}",
            json_body={"role": "auditor"}, token=admin_token,
        )
        check("再造一条审计行", status == 200, f"{status} {body[:160]}")
        status, _, body = http("POST", "/api/v1/admin/audit/anchor", token=operator_token)
        anchor_2 = payload(body).get("anchor") or {}
        check("新锚点覆盖到更新的行",
              anchor_2.get("entries") > anchor_1["entries"]
              and anchor_2.get("toId") > anchor_1["toId"],
              json.dumps(anchor_2, ensure_ascii=False)[:200])
        check("新锚点起点仍是首行", anchor_2.get("fromId") == anchor_1["fromId"], str(anchor_2.get("fromId")))
        status, _, body = http("GET", "/api/v1/admin/audit/anchor/verify", token=auditor_token)
        both = payload(body)
        check("两个锚点同时可信",
              both.get("anchors") == 2 and both.get("untrusted") == 0,
              json.dumps(both, ensure_ascii=False)[:240])

        print("\n[锚点签名本身被动过]")
        db_path = os.path.join(_TMP, "market.db")
        target_seq = int(anchor_2["seq"])
        signature_saved = patch_anchor_row(db_path, target_seq, "signature", admin_token)
        status, _, body = http("GET", "/api/v1/admin/audit/anchor/verify", token=auditor_token)
        forged = payload(body)
        hit = next(
            (item for item in forged.get("items") or [] if item["seq"] == target_seq), {}
        )
        check("换成登录令牌后锚点判不可信",
              hit.get("signatureTrusted") is False and "RS256" in str(hit.get("reason")),
              json.dumps({key: hit.get(key) for key in
                          ("seq", "signatureTrusted", "chainTrusted", "reason")},
                         ensure_ascii=False))
        # 同一把私钥签的「别的类型」的签名：算法与密钥都对，只有受众/类型不对。
        # 客户端手里就有档位表的签名，把它塞进锚点表是最现实的重放路径。
        other_aud = key_store().sign({
            "iss": settings.jwt_issuer, "aud": "tongtian-tiers", "typ": "model-tiers",
            "fromId": 1, "toId": 1, "entries": 1, "headHash": GENESIS,
            "digest": "0" * 64, "iat": int(time.time()),
        })
        patch_anchor_row(db_path, target_seq, "signature", other_aud)
        status, _, body = http("GET", "/api/v1/admin/audit/anchor/verify", token=auditor_token)
        replay = next(
            (item for item in payload(body).get("items") or [] if item["seq"] == target_seq), {}
        )
        check("别类签名重放进锚点被受众挡下",
              replay.get("signatureTrusted") is False and "受众" in str(replay.get("reason")),
              json.dumps({key: replay.get(key) for key in
                          ("signatureTrusted", "reason")}, ensure_ascii=False))
        check("签名不可信时不再比对链（不给假签名找理由）",
              replay.get("chainTrusted") is False and "未比对链" in str(replay.get("reason")),
              str(replay.get("reason")))
        status, _, body = http("GET", "/api/v1/admin/audit/compliance", token=auditor_token)
        forged_report = payload(body)
        check("合规报告把假签名点名成错误级",
              any(item["code"] == "ANCHOR_MISMATCH" and item.get("anchorSeq") == target_seq
                  and item["level"] == "error" for item in forged_report["findings"])
              and forged_report["verdict"]["trustworthy"] is False,
              json.dumps(forged_report["findings"], ensure_ascii=False)[:300])
        check("另一个未被动过的锚点仍然可信",
              sum(1 for item in forged_report["anchors"]["mismatches"]
                  if item["seq"] != target_seq) == 0
              and forged_report["anchors"]["untrusted"] == 1,
              json.dumps(forged_report["anchors"], ensure_ascii=False)[:260])
        patch_anchor_row(db_path, target_seq, "signature", signature_saved)
        head_saved = str(anchor_2["headHash"])
        patch_anchor_row(db_path, target_seq, "head_hash", GENESIS)
        status, _, body = http("GET", "/api/v1/admin/audit/anchor/verify", token=auditor_token)
        swapped = payload(body)
        swapped_hit = next(
            (item for item in swapped.get("items") or [] if item["seq"] == target_seq), {}
        )
        check("改签名保护的字段同样被拒",
              swapped_hit.get("signatureTrusted") is False
              and "headHash" in str(swapped_hit.get("reason")),
              json.dumps(swapped_hit, ensure_ascii=False)[:240])
        patch_anchor_row(db_path, target_seq, "head_hash", head_saved)
        status, _, body = http("GET", "/api/v1/admin/audit/anchor/verify", token=auditor_token)
        restored = payload(body)
        check("恢复签名与字段后重新全部可信",
              restored.get("untrusted") == 0 and restored.get("anchors") == 2,
              json.dumps(restored, ensure_ascii=False)[:240])

        print("\n[逐行篡改：链能抓住]")
        first_id = int(anchor_2["fromId"])
        status, _, body = http("GET", f"/api/v1/admin/audit?after=0&limit=1000", token=auditor_token)
        live_entries = payload(body).get("entries") or []
        original_action = str(
            next((e["action"] for e in live_entries if e["id"] == first_id), "")
        )
        check("取到被篡改行的原动作", bool(original_action), original_action)
        tamper_action(os.path.join(_TMP, "market.db"), first_id, "login")
        status, _, body = http("GET", "/api/v1/admin/audit/verify", token=auditor_token)
        broken = payload(body)
        check("篡改后逐行验链报断点",
              status == 200 and broken.get("ok") is False and broken.get("brokenAt") == first_id,
              f"{status} {json.dumps(broken, ensure_ascii=False)[:200]}")
        check("断点原因是「本行正文被改」",
              broken.get("breaks", [{}])[0].get("reason") == "entry_mismatch",
              json.dumps(broken.get("breaks"), ensure_ascii=False)[:200])
        status, _, body = http("POST", "/api/v1/admin/audit/anchor", token=operator_token)
        refused = payload(body)
        check("链有断点时拒绝出具新锚点",
              status == 409 and refused.get("code") == "AUDIT_CHAIN_BROKEN",
              f"{status} {body[:200]}")
        check("拒绝原因点名行号", str(first_id) in str(refused.get("message")), json.dumps(refused, ensure_ascii=False)[:200])
        status, _, body = http("GET", "/api/v1/admin/audit/compliance", token=auditor_token)
        dirty = payload(body)
        check("合规报告把篡改报成 error",
              dirty["verdict"]["trustworthy"] is False
              and dirty["verdict"]["errorCount"] >= 1
              and any(item["code"] == "CHAIN_BREAK" for item in dirty["findings"]),
              json.dumps(dirty["findings"], ensure_ascii=False)[:240])
        check("合规结论不再说可信", "不可信" in str(dirty["verdict"]["statement"]), str(dirty["verdict"]["statement"]))
        status, headers, body = http("GET", "/api/v1/admin/audit/compliance/export", token=auditor_token)
        check("导出在篡改后声明不可信",
              headers.get("x-tt-audit-trustworthy") == "false"
              and "断点明细" in body.decode("utf-8"),
              str(headers)[:200])

        print("\n[整体重算：只有锚点能抓住]")
        db_path = os.path.join(_TMP, "market.db")
        drop_id = first_id + 1
        status, _, body = http("GET", "/api/v1/admin/audit/verify", token=auditor_token)
        entries_before = payload(body)["entries"]
        forge_recompute(db_path, drop_id, {first_id: original_action})
        status, _, body = http("GET", "/api/v1/admin/audit/verify", token=auditor_token)
        forged = payload(body)
        check("删行并重算后逐行验链看不出来",
            status == 200 and forged.get("ok") is True and forged.get("brokenAt") is None
              and forged.get("entries") == entries_before - 1,
              f"{status} {json.dumps(forged, ensure_ascii=False)[:220]}")
        status, _, body = http("GET", "/api/v1/admin/audit/anchor/verify", token=auditor_token)
        caught = payload(body)
        check("但锚点判定为不可信",
              status == 200 and caught.get("untrusted") == 2,
              json.dumps(caught, ensure_ascii=False)[:260])
        check("不可信原因指向条目数或链头",
              all(("条目数" in str(item.get("reason")) or "链头" in str(item.get("reason")))
                  for item in caught.get("items") or []),
              json.dumps([item.get("reason") for item in caught.get("items") or []], ensure_ascii=False)[:240])
        status, _, body = http("GET", "/api/v1/admin/audit/compliance", token=auditor_token)
        forged_report = payload(body)
        check("合规报告点名锚点与链不一致",
              forged_report["verdict"]["trustworthy"] is False
              and any(item["code"] == "ANCHOR_MISMATCH" for item in forged_report["findings"]),
              json.dumps(forged_report["findings"], ensure_ascii=False)[:240])
        check("不一致的锚点是旧的两个",
              sorted(item["anchorSeq"] for item in forged_report["findings"]
                     if item["code"] == "ANCHOR_MISMATCH") == [1, 2],
              json.dumps(forged_report["findings"], ensure_ascii=False)[:300])
        check("锚点总数与本次比对数一起给出",
              forged_report["anchors"]["anchors"] == 2
              and forged_report["anchors"]["checked"] == 2
              and forged_report["anchors"]["statement"] != "",
              json.dumps(forged_report["anchors"], ensure_ascii=False)[:240])
        check("缺口被合规报告单列为 ID_GAP",
              any(item["code"] == "ID_GAP" for item in forged_report["findings"])
              and forged_report["integrity"]["contiguous"] is False,
              json.dumps(forged_report["findings"], ensure_ascii=False)[:300])
        status, _, body = http("POST", "/api/v1/admin/audit/anchor", token=operator_token)
        new_anchor = payload(body).get("anchor") or {}
        check("重算后仍可给「当前链」出新锚点（锚点只向前作证）",
              status == 200 and new_anchor.get("seq") == 3, f"{status} {body[:200]}")
        status, _, body = http("GET", "/api/v1/admin/audit/anchor/verify", token=auditor_token)
        mixed = payload(body)
        check("新锚点可信、旧锚点仍不可信",
              mixed.get("anchors") == 3 and mixed.get("untrusted") == 2,
              json.dumps(mixed, ensure_ascii=False)[:200])

        print("\n[远端归档：未配置与地址不合法]")
        status, _, body = http("POST", "/api/v1/admin/audit/anchor/archive", json_body={}, token=auditor_token)
        check("审计员不能触发归档", status == 403, f"{status} {body[:160]}")
        status, _, body = http("POST", "/api/v1/admin/audit/anchor/archive", token=operator_token)
        unconfigured = payload(body)
        check("未配置远端时如实说明没归档",
              status == 200 and unconfigured.get("pushed") is False
              and "未配置" in str(unconfigured.get("reason")),
              f"{status} {json.dumps(unconfigured, ensure_ascii=False)[:220]}")
        check("未配置时锚点状态仍是 local", str(unconfigured.get("archiveState")) == "local", str(unconfigured.get("archiveState")))
        set_archive("http://archive.example.invalid")
        status, _, body = http("POST", "/api/v1/admin/audit/anchor/archive", json_body={}, token=operator_token)
        check("非回环 http 地址被拒", status == 400 and payload(body).get("code") == "INVALID_ARCHIVE_URL",
              f"{status} {body[:200]}")
        set_archive("")

        print("\n[远端归档：真实推送与失败重试]")
        status, _, body = http("GET", "/.well-known/jwks.json")
        jwks = payload(body)
        check("JWKS 可读且含当前 kid",
              status == 200 and any(item["kid"] == new_anchor["signingKid"] for item in jwks.get("keys") or []),
              f"{status} {json.dumps(jwks, ensure_ascii=False)[:180]}")
        sink = SinkServer(sink_port, jwks)
        threading.Thread(target=sink.serve_forever, daemon=True).start()
        time.sleep(0.2)
        set_archive(f"http://127.0.0.1:{sink_port}", "wrong-token")
        status, _, body = http("POST", "/api/v1/admin/audit/anchor/archive", json_body={}, token=operator_token)
        check("令牌不对时归档失败并记原因",
              status == 502 and payload(body).get("code") == "AUDIT_ARCHIVE_FAILED",
              f"{status} {body[:200]}")
        status, _, body = http("GET", "/api/v1/admin/audit/archive/status", token=auditor_token)
        failed_state = payload(body)
        check("失败状态落库（没被事务回滚吃掉）",
              status == 200 and failed_state.get("byState", {}).get("failed", 0) >= 1,
              json.dumps(failed_state, ensure_ascii=False)[:240])
        check("失败原因写进锚点行",
              "401" in str(failed_state["recent"][0]["error"]) or "token" in str(failed_state["recent"][0]["error"]).lower(),
              json.dumps(failed_state["recent"][0], ensure_ascii=False)[:200])

        set_archive(f"http://127.0.0.1:{sink_port}", ARCHIVE_TOKEN)
        status, _, body = http("POST", "/api/v1/admin/audit/anchor/archive", json_body={}, token=operator_token)
        pushed = payload(body)
        check("换成正确令牌后归档成功",
              status == 200 and pushed.get("pushed") is True and pushed.get("archiveState") == "remote",
              f"{status} {json.dumps(pushed, ensure_ascii=False)[:220]}")
        check("归档返回外部引用", str(pushed.get("archiveRef") or "").startswith("sink-"), str(pushed)[:200])
        check("推送覆盖全链", pushed.get("coverage") == "full" and pushed.get("rowsPushed") >= 1,
              json.dumps(pushed, ensure_ascii=False)[:200])
        check("对端独立验签通过并落账", len(sink.received) == 1 and not sink.rejected,
              json.dumps(sink.rejected, ensure_ascii=False)[:240])
        delivered = sink.received[0]["anchor"] if sink.received else {}
        check("对端收到的锚点字段与本地一致",
              delivered.get("seq") == new_anchor["seq"]
              and delivered.get("headHash") == new_anchor["headHash"]
              and delivered.get("entries") == new_anchor["entries"],
              json.dumps(delivered, ensure_ascii=False)[:220])
        check("对端被告知去哪里取公钥", sink.received[0]["jwksPath"] == "/.well-known/jwks.json",
              str(sink.received[0]["jwksPath"]))
        rows = sink.received[0]["rows"] or []
        check("推送的行覆盖到锚点末行且带摘要",
              rows and rows[-1]["id"] == new_anchor["toId"]
              and all(len(str(item.get("entryHash") or "")) == CHAIN_LEN for item in rows),
              json.dumps(rows[-1:], ensure_ascii=False)[:200])
        check("推送的行不含任何令牌明文",
              ARCHIVE_TOKEN not in (sink.received[0]["raw"] or "")
              and admin_token not in (sink.received[0]["raw"] or "")
              and operator_token not in (sink.received[0]["raw"] or ""),
              str(sink.received[0]["raw"])[:200])

        print("\n[超过单次推送上限：如实拒推而不是推半条链]")
        limit_saved = audit_anchor.MAX_ARCHIVE_ROWS
        audit_anchor.MAX_ARCHIVE_ROWS = 1
        status, _, body = http("POST", "/api/v1/admin/audit/anchor/archive",
                               json_body={"seq": int(new_anchor["seq"])}, token=operator_token)
        skipped = payload(body)
        check("超过上限时不推送且不报成功",
              status == 200 and skipped.get("pushed") is False
              and skipped.get("archiveState") == "skipped",
              f"{status} {json.dumps(skipped, ensure_ascii=False)[:220]}")
        check("拒推原因点名上限与覆盖行数",
              "上限" in str(skipped.get("reason")) and str(new_anchor["entries"]) in str(skipped.get("reason")),
              str(skipped.get("reason")))
        check("对端没有收到残缺推送", len(sink.received) == 1, str(len(sink.received)))
        status, _, body = http("GET", "/api/v1/admin/audit/compliance", token=auditor_token)
        cap_report = payload(body)
        check("合规报告把 skipped 单列为 warn",
              any(item["code"] == "ARCHIVE_SKIPPED" and item["level"] == "warn"
                  for item in cap_report["findings"]),
              json.dumps(cap_report["findings"], ensure_ascii=False)[:300])
        audit_anchor.MAX_ARCHIVE_ROWS = limit_saved
        status, _, body = http("POST", "/api/v1/admin/audit/anchor/archive",
                               json_body={"seq": int(new_anchor["seq"])}, token=operator_token)
        recovered = payload(body)
        check("恢复上限后可重新归档成功",
              status == 200 and recovered.get("pushed") is True
              and recovered.get("archiveState") == "remote"
              and recovered.get("archiveError") == "",
              f"{status} {json.dumps(recovered, ensure_ascii=False)[:220]}")

        set_archive(f"http://127.0.0.1:{free_port()}")
        status, _, body = http("POST", "/api/v1/admin/audit/anchor/archive",
                               json_body={"seq": int(new_anchor["seq"])}, token=operator_token)
        check("归档端不可达时 502 并记下原因",
              status == 502 and payload(body).get("code") == "AUDIT_ARCHIVE_FAILED",
              f"{status} {body[:200]}")
        status, _, body = http("GET", "/api/v1/admin/audit/anchors", token=auditor_token)
        again = payload(body).get("anchors") or []
        check("不可达后状态退回 failed（外部引用已清）",
              any(item["seq"] == new_anchor["seq"] and item["archiveState"] == "failed"
                  and item["archiveRef"] == "" for item in again),
              json.dumps(again[:2], ensure_ascii=False)[:240])
        set_archive("")

        print("\n[配置恢复后的口径]")
        status, _, body = http("GET", "/api/v1/admin/audit/compliance", token=auditor_token)
        final = payload(body)
        check("撤掉归档配置后又变成 warn",
              any(item["code"] == "NO_REMOTE_ARCHIVE" for item in final["findings"])
              and final["verdict"]["errorCount"] >= 1,
              json.dumps(final["findings"], ensure_ascii=False)[:260])
        check("报告里归档状态分布保留历史",
              final["anchors"]["archiveByState"].get("failed", 0) >= 1,
              json.dumps(final["anchors"]["archiveByState"], ensure_ascii=False))
        check("链本身已经自洽", final["integrity"]["ok"] is True, json.dumps(final["integrity"])[:200])
    finally:
        if sink is not None:
            sink.shutdown()
            sink.server_close()
        server.should_exit = True
    print(f"\n通过 {_passed} 项")


if __name__ == "__main__":
    main()
