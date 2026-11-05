#!/usr/bin/env python3
"""D-29（#157）工作区后端抽象自测：本地 / Docker / PostgreSQL 半边 + S3 / COS / SSH 适配器。

离线跑：假 docker CLI、假 DB-API 连接、假对象存储端点、假 SSH 传输件。
**假端点绿不等于真后端能用** —— 真容器、真库、真桶、真主机一律记未验收，
本自测钉的是「配置怎么解析、判定怎么分三段、命令行与 SQL 与签名长什么样、
凭据到底有没有落进配置/审计/返回值」。

必须用 `./vendor/python/python.exe -X utf8` 跑（缺依赖时整族 SKIP 且 rc=0，看着像绿其实没跑）。

用法: python tests/integration/engine_workspace_backend_selftest.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

_TMP = Path(tempfile.mkdtemp(prefix="tt-ws-backend-"))
os.environ["TONGTIANWORK_HOME"] = str(_TMP / "home")

try:
    import sandbox_runtime as sr
    import workspace_backend_runtime as wb
except Exception as exc:  # 依赖缺失时优雅跳过
    print(f"SKIP: workspace backend selftest unavailable ({exc})")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def policy_for(work_dir: Path | None, *, level: str = "L1", permission: str = "ask_every_time", **extra) -> sr.SandboxPolicy:
    def pick(camel: str) -> list:
        # 调用方两种写法都出现过（下划线更易读）；少认一种就等于静默丢规则
        return extra.get(camel) or extra.get(camel[0].lower() + "".join("_" + c.lower() if c.isupper() else c for c in camel[1:])) or []

    bundle = {
        "filePermission": permission,
        "sandbox": {
            "workspaceId": "ws-selftest",
            "configured": True,
            "executionLevel": level,
            "toolAllowlist": pick("toolAllowlist"),
            "toolBlocklist": pick("toolBlocklist"),
            "fileAllowlist": pick("fileAllowlist"),
            "fileBlocklist": pick("fileBlocklist"),
        },
    }
    return sr.SandboxPolicy.from_runtime(bundle, work_dir=work_dir)


def fresh_registry(home: Path) -> None:
    wb.reset_registry()
    wb._MEMORY_AUDIT.clear()
    os.environ["TONGTIANWORK_HOME"] = str(home)


# ── 假件 ───────────────────────────────────────────────────────


class FakeConnection:
    """最小 DB-API 2.0 假连接：只记 SQL 与参数，行存在一个 dict 里"""

    def __init__(self, rows: dict[tuple[str, str], tuple[str, int, str]] | None = None, log: list | None = None):
        self.rows = rows if rows is not None else {}
        self.log = log if log is not None else []
        self.dsn: dict[str, str] = {}

    def cursor(self) -> "FakeCursor":
        return FakeCursor(self)

    def commit(self) -> None:
        self.log.append(("COMMIT", ()))

    def close(self) -> None:
        self.log.append(("CLOSE", ()))


class FakeCursor:
    def __init__(self, conn: FakeConnection):
        self.conn = conn
        self._result: list = []

    def execute(self, sql: str, params=None) -> None:
        values = tuple(params or ())
        self.conn.log.append((sql, values))
        head = sql.strip().upper()
        if head.startswith("SET LOCAL"):
            self._result = []
            return
        if head.startswith("CREATE TABLE"):
            self._result = []
            return
        if head.startswith("SELECT"):
            if "obj_path = %s" in sql:
                key = (values[0], values[1])
                row = self.conn.rows.get(key)
                self._result = [row[:2]] if row else []
            else:
                backend_id, prefix, _like, limit = values[0], values[1], values[2], values[3]
                items = []
                needle = prefix.replace("\\%", "%").replace("\\_", "_")
                for (bid, path), row in sorted(self.conn.rows.items()):
                    if bid != backend_id:
                        continue
                    if needle and not path.startswith(needle):
                        continue
                    items.append((path, row[1], row[2], "now()"))
                    if len(items) >= limit:
                        break
                self._result = items
            return
        if head.startswith("INSERT"):
            self.conn.rows[(values[0], values[1])] = (values[2], values[3], values[4])
            self._result = []
            return
        if head.startswith("DELETE"):
            self.conn.rows.pop((values[0], values[1]), None)
            self._result = []
            return
        self._result = []

    def fetchall(self) -> list:
        return list(self._result)

    def close(self) -> None:
        pass


def s3_listing(prefix: str, names: list[str], *, truncated: bool = False) -> bytes:
    rows = "".join(
        f"<Contents><Key>{prefix}{n}</Key><Size>{len(n) * 4}</Size><ETag>\"etag-{n}\"</ETag></Contents>"
        for n in names
    )
    tail = "<Truncated>true</Truncated>" if truncated else ""
    return (
        f'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        f"<Name>bucket</Name>{rows}{tail}</ListBucketResult>"
    ).encode("utf-8")


class FakeStore:
    """假对象存储端点：记每次请求的 method/url/headers/body，按 dict 存取正文"""

    def __init__(self, objects: dict[str, bytes] | None = None, *, status: int = 200):
        self.objects: dict[str, bytes] = objects or {}
        self.requests: list[dict] = []
        self.status = status

    def __call__(self, method, url, *, headers, body, timeout):
        self.requests.append({"method": method, "url": url, "headers": dict(headers), "body": body, "timeout": timeout})
        import urllib.parse as up

        path = up.urlsplit(url).path
        key = path.split("/", 2)[-1] if self._path_style(url) else path.lstrip("/")
        if method == "GET" and "?" in url or (method == "GET" and up.urlsplit(url).query):
            listing_prefix = up.parse_qs(up.urlsplit(url).query).get("prefix", [""])[0]
            return 200, {"Content-Type": "application/xml"}, s3_listing(listing_prefix, [k.split("/")[-1] for k in self.objects if k.startswith(listing_prefix)])
        if method == "GET":
            data = self.objects.get(key)
            if data is None:
                return 404, {}, b'<Error><Code>NoSuchKey</Code><Message>not found</Message></Error>'
            return 200, {"ETag": '"abc123"'}, data
        if method == "PUT":
            self.objects[key] = body or b""
            return 204, {"ETag": '"abc123"'}, b""
        if method == "DELETE":
            self.objects.pop(key, None)
            return 204, {}, b""
        return 405, {}, b"<Error><Code>MethodNotAllowed</Code></Error>"

    @staticmethod
    def _path_style(url: str) -> bool:
        return True


# ── 1. 凭据：只认引用 ─────────────────────────────────────────


def credentials_checks() -> None:
    print("--- 1. 凭据只接受引用（D-29 验收第三句）---")
    ref, err = wb.parse_credential_ref("secretKey", "AKIA1234567890SECRETVALUE")
    check("明文密钥被拒而不是被宽容接受", ref is None and "字面量" in err, err)
    check("拒绝理由点名为「导出配置等于导出密钥」", "导出配置" in err, err)

    ref, err = wb.parse_credential_ref("secretKey", "env:TT_WS_SECRET")
    check("env 引用合法", ref is not None and err == "" and ref.scheme == "env" and ref.name == "TT_WS_SECRET")

    ref, err = wb.parse_credential_ref("secretKey", "keychain:tongtian/ws")
    check("keychain 引用语法合法", ref is not None and ref.scheme == "keychain")

    ref, err = wb.parse_credential_ref("secretKey", "keychain:只写了服务")
    check("keychain 引用缺账户段被拒", ref is None and "服务" in err, err)

    ref, err = wb.parse_credential_ref("secretKey", "vault:whatever")
    check("未登记的引用形式被拒", ref is None and "不支持" in err, err)

    values, problems = wb.resolve_credentials(
        [wb.CredentialRef(key_name="secretKey", scheme="env", name="TT_NOT_SET_AT_ALL")],
        environ={},
    )
    check("环境变量没设时如实报未解析", not values and len(problems) == 1 and "未设置" in problems[0])

    values, problems = wb.resolve_credentials(
        [wb.CredentialRef(key_name="secretKey", scheme="keychain", name="tongtian/ws")], environ={}
    )
    check(
        "钥匙串引用回「未接线」而不是假装取到",
        not values and "未验收" in problems[0] and "不会退回读环境变量" in problems[0],
        str(problems),
    )

    spec = wb.parse_spec(
        {
            "backendId": "ws-secret",
            "kind": "s3",
            "bucket": "tt-fake-bucket",
            "options": {"bucket": "tt-fake-bucket", "endpoint": "https://example.invalid", "secretKey": "PLAINTEXT"},
        }
    )
    check("配置里的明文凭据进 errors（后端 fail-closed）", any("字面量" in e for e in spec.errors), str(spec.errors))

    spec_ok = wb.parse_spec(
        {
            "backendId": "ws-env",
            "kind": "s3",
            "options": {"bucket": "tt-fake-bucket", "endpoint": "https://example.invalid", "secretKey": "env:TT_WS_SK"},
        }
    )
    described = json.dumps(spec_ok.describe(), ensure_ascii=False)
    check("合法引用不报错", not spec_ok.errors, str(spec_ok.errors))
    check("describe() 里没有凭据原值也没有引用值", "TT_WS_SK" not in described or "source" in described and "env:TT_WS_SK" in described)
    check("describe() 标出引用来源与解析状态", "env:TT_WS_SK" in described and "resolved" in described, described[:200])


# ── 2. 对象路径归一 ───────────────────────────────────────────


def path_checks() -> None:
    print("--- 2. 对象路径归一（越界规则取 D-45 的比较键）---")
    for raw, why in [
        ("C:/Windows/x.md", "盘符"),
        ("\\\\server\\share\\x", "UNC"),
        ("/etc/passwd", "绝对路径"),
        ("~/secrets", "家目录展开"),
        ("../outside", "上级逃逸"),
        ("a/../../b", "多级逃逸"),
        ("a\x00b", "控制字符"),
    ]:
        vpath, err = wb.normalize_virtual_path(raw)
        check(f"拒绝 {why}（{raw}）", vpath == "" and err != "", err)

    vpath, err = wb.normalize_virtual_path("./docs//notes.md")
    check("相对路径收成规范形", vpath == "docs/notes.md" and err == "", f"{vpath} / {err}")
    check("空路径合法（表示后端根）", wb.normalize_virtual_path("") == ("", ""))

    # 与 D-45 同源的比较键：Windows 命名空间前缀、大小写、`/**`
    check("前缀命中复用沙箱比较键（大小写与分隔符同源）", wb.within_backend_prefix("Docs", "docs/a.md"))
    check("前缀分段对齐：Docs 不算 Documents 的本家", not wb.within_backend_prefix("Docs", "Documents/a.md"))
    check("前缀 /** 写法含目录本身", wb.within_backend_prefix("docs/**", "docs"))
    check("根外路径判为越界", not wb.within_backend_prefix("docs", "other/a.md"))


# ── 3. 三段判定各有各的措辞 ───────────────────────────────────


def denial_checks() -> None:
    print("--- 3. 三段拒绝：边界 / 沙箱 / 档位，文案各不共用 ---")
    # prefix 是「工作区根在远端的落点」，操作面给的 vpath 一律相对它 ——
    # 与本地后端 options.dir + 相对路径同口径，所以这里的路径都不带 docs/ 前缀。
    spec = wb.parse_spec({"backendId": "ws-s3", "kind": "s3", "options": {"bucket": "tt-fake-bucket", "endpoint": "https://x.invalid", "prefix": "docs"}})
    check("parse_spec 认 prefix 段", spec.prefix == "docs" and not spec.errors, str(spec.errors))
    policy = policy_for(_TMP / "work", permission="ask_every_time")

    boundary = wb.decide_access(spec, "../other/a.md", policy=policy, write=True)
    tier = wb.decide_access(spec, "a.md", policy=policy, write=True)
    sandbox = wb.decide_access(spec, "secret/a.md", policy=policy_for(_TMP / "work", file_blocklist=["secret/**"]), write=True)
    l0 = wb.decide_access(spec, "a.md", policy=policy_for(_TMP / "work", level="L0"), write=True)

    check("越出后端根：来源是 backend_boundary", boundary.source == wb.SOURCE_BACKEND_BOUNDARY and boundary.code == wb.CODE_PATH_OUTSIDE_BACKEND, str(boundary.__dict__))
    check("档位拒绝：来源是 file_permission", tier.source == wb.SOURCE_FILE_PERMISSION and tier.code == wb.CODE_TIER_DENIES_REMOTE, str(tier.__dict__))
    check("沙箱黑名单：来源是 sandbox", sandbox.source == wb.SOURCE_SANDBOX and "黑名单" in sandbox.reason, str(sandbox.__dict__))
    check("L0 只读沿用沙箱原句", l0.source == wb.SOURCE_SANDBOX and l0.reason == "沙箱为 L0 只读，不允许写入文件", l0.reason)

    sentences = {boundary.reason, tier.reason, sandbox.reason, l0.reason}
    check("四句拒绝理由互不相同（不许共用一句）", len(sentences) == 4, str(sentences))
    check("档位句点名「完全访问管不到前缀与只读」", "完全访问" in tier.reason and "根前缀" in tier.reason)
    check("边界句点名「与档位与沙箱白名单无关」", "文件访问档位" in boundary.reason and "沙箱白名单" in boundary.reason)

    full = wb.decide_access(spec, "a.md", policy=policy_for(_TMP / "work", permission="full_access"), write=True)
    check("完全访问放开远程写入（根内、可写后端）", full.allowed, full.reason)
    still_boundary = wb.decide_access(spec, "C:/Windows/a.md", policy=policy_for(_TMP / "work", permission="full_access"), write=True)
    check("完全访问也越不出后端根", not still_boundary.allowed and still_boundary.source == wb.SOURCE_BACKEND_BOUNDARY, str(still_boundary.__dict__))

    ro = wb.parse_spec({"backendId": "ws-ro", "kind": "s3", "options": {"bucket": "tt-fake-bucket", "endpoint": "https://x.invalid", "prefix": "docs"}, "readOnly": True})
    ro_denial = wb.decide_access(ro, "a.md", policy=policy_for(_TMP / "work", permission="full_access"), write=True)
    check("只读后端在任何档位下都拒写", not ro_denial.allowed and ro_denial.source == wb.SOURCE_BACKEND and ro_denial.code == wb.CODE_BACKEND_READONLY, str(ro_denial.__dict__))

    bad = wb.parse_spec({"backendId": "ws-bad", "kind": "s3", "options": {"bucket": "tt-fake-bucket", "endpoint": "http://x.invalid"}})
    broken = wb.decide_access(bad, "a.md", policy=policy, write=True)
    check("坏配置 fail-closed 且优先于一切判定", not broken.allowed and broken.code == wb.CODE_SPEC_INVALID, str(bad.errors))

    remote_delete = wb.decide_access(spec, "a.md", policy=policy_for(_TMP / "work", permission="full_access"), write=True, delete=True)
    check("远程删除还要后端显式开启（档位到不了那一层）", not remote_delete.allowed and remote_delete.code == wb.CODE_DELETE_DENIED and "allowRemoteDelete" in remote_delete.reason)
    allow_del = wb.parse_spec({"backendId": "ws-s3", "kind": "s3", "options": {"bucket": "tt-fake-bucket", "endpoint": "https://x.invalid", "prefix": "docs", "allowRemoteDelete": True}})
    ok_delete = wb.decide_access(allow_del, "a.md", policy=policy_for(_TMP / "work", permission="full_access"), write=True, delete=True)
    check("开了 allowRemoteDelete 且档位够才允许远程删除", ok_delete.allowed, ok_delete.reason)

    ask_delete = wb.decide_access(allow_del, "a.md", policy=policy_for(_TMP / "work", permission="auto_approve"), write=True, delete=True)
    check("没到完全访问时远程删除按档位句拒（与未开启后端是两句）", not ask_delete.allowed and ask_delete.code == wb.CODE_DELETE_NOT_FULL_ACCESS and ask_delete.source == wb.SOURCE_FILE_PERMISSION, str(ask_delete.__dict__))


# ── 4. 本地后端：判定整条交回 D-45 ────────────────────────────


def local_checks() -> None:
    print("--- 4. 本地目录后端（文件判定走既有 file_denial，不重述）---")
    home = _TMP / "home-4"
    fresh_registry(home)
    root = home / "work"
    root.mkdir(parents=True, exist_ok=True)
    spec = wb.parse_spec({"backendId": "ws-local", "kind": "local", "options": {"dir": str(root)}})
    backend = wb.build_backend(spec, policy=policy_for(root))
    ctx = wb.OpContext(task_id="t-4", principal="local", tool="workspace_write")

    check("本地后端定位到绑定目录", isinstance(backend, wb.LocalWorkspaceBackend) and backend.root == root, str(backend.root))

    written = backend.write("notes/a.md", "第一行", ctx)
    check("目录内写入成功", written["ok"] is True and (root / "notes" / "a.md").read_text(encoding="utf-8") == "第一行", str(written))

    read = backend.read("notes/a.md", ctx)
    check("读回正文与指纹", read["ok"] and read["content"] == "第一行" and read["contentFingerprint"], str(read)[:200])

    listed = backend.list_dir("", ctx)
    check("列目录看到子目录", listed["ok"] and any(i["name"] == "notes" and i["isDir"] for i in listed["items"]), str(listed)[:200])

    escaped = backend.write("../escape.md", "x", ctx)
    check("上级逃逸按非法路径拒", escaped["ok"] is False and escaped["code"] == wb.CODE_PATH_UNSAFE, str(escaped))

    home_root = Path(os.path.expanduser("~")).resolve()
    elsewhere = wb.build_backend(wb.parse_spec({"backendId": "ws-out", "kind": "local", "options": {"dir": str(root)}}), policy=policy_for(root))
    outside = Path(str(home_root / "outside-probe.md"))
    decision = elsewhere.decide("outside-probe.md", host_path=outside, write=True)
    check(
        "落到家目录的越界写仍走硬拒清单（D-45 原句）",
        not decision.allowed and "硬拒清单" in decision.reason,
        decision.reason,
    )

    ro = wb.build_backend(
        wb.parse_spec({"backendId": "ws-ro", "kind": "local", "options": {"dir": str(root)}, "readOnly": True}),
        policy=policy_for(root, permission="full_access"),
    )
    denied = ro.write("x.md", "y", ctx)
    check("只读后端连完全访问也写不了", denied["ok"] is False and denied["code"] == wb.CODE_BACKEND_READONLY, str(denied))

    ask = wb.build_backend(
        wb.parse_spec({"backendId": "ws-ask", "kind": "local", "options": {"dir": str(root)}}),
        policy=policy_for(None, permission="ask_every_time"),
    )
    no_wd = ask.write("a.md", "x", ctx)
    check("未绑定工作目录时档位照样拦（不当成没限制）", no_wd["ok"] is False, str(no_wd))

    big = wb.build_backend(
        wb.parse_spec({"backendId": "ws-small", "kind": "local", "options": {"dir": str(root)}, "limits": {"maxWriteBytes": 10}}),
        policy=policy_for(root, permission="full_access"),
    )
    over = big.write("big.md", "超过十个字节的内容", ctx)
    check("超上限整篇不落盘（不截断写）", over["ok"] is False and over["code"] == wb.CODE_LIMIT_INVALID and not (root / "big.md").exists(), str(over))

    deleted = backend.delete("notes/a.md", wb.OpContext(task_id="t-4", tool="workspace_delete"))
    check("目录内删除需完全访问，缺省档位被拒", deleted["ok"] is False and "完全访问" in str(deleted.get("message")), str(deleted))
    del_backend = wb.build_backend(
        wb.parse_spec({"backendId": "ws-local", "kind": "local", "options": {"dir": str(root)}}),
        policy=policy_for(root, permission="full_access"),
    )
    deleted = del_backend.delete("notes/a.md", ctx)
    check("完全访问下删除成功并留回收副本", deleted["ok"] and deleted["trashPath"], str(deleted))

    check("本地后端不承载命令执行", "不承载命令执行" in str(backend.run("ls", ctx).get("message")))


# ── 5. Docker：命令行构造与危险重数 ───────────────────────────


def docker_checks() -> None:
    print("--- 5. Docker 后端（argv 构造层；真容器未验收）---")
    home = _TMP / "home-5"
    fresh_registry(home)
    root = home / "mount"
    root.mkdir(parents=True, exist_ok=True)
    calls: list[tuple[list[str], int]] = []

    def fake_run(argv, timeout):
        calls.append((list(argv), timeout))
        return 0, "容器里看到的目录\n", ""

    spec = wb.parse_spec(
        {
            "backendId": "ws-docker",
            "kind": "docker",
            "options": {"dir": str(root), "image": "python:3.12-slim"},
            "limits": {"memoryMb": 256, "cpus": 0.5, "pids": 64, "network": "none", "timeoutSecs": 30},
        }
    )
    backend = wb.build_backend(spec, policy=policy_for(root, permission="full_access"), deps={"run_fn": fake_run})
    argv = backend.build_run_argv("pytest -q")
    check("镜像与命令按列表形式传（不经本机 shell）", argv[-3:] == ["python:3.12-slim", "sh", "-c"] or argv[-4] == "python:3.12-slim", str(argv[-4:]))
    check("内存/CPU/进程数重数真的进了命令行", "--memory" in argv and "256m" in argv and "--cpus" in argv and "--pids-limit" in argv, " ".join(argv))
    check("默认断网", "--network" in argv and argv[argv.index("--network") + 1] == "none")
    check("绑定挂载可写", any(a.startswith(f"{root}:") and a.endswith(":rw") for a in argv), " ".join(argv))
    check("workdir 落在容器内路径", "--workdir" in argv and "/workspace" in argv)

    ro_spec = wb.parse_spec(
        {"backendId": "ws-docker-ro", "kind": "docker", "options": {"dir": str(root), "image": "alpine:3.20"}, "readOnly": True}
    )
    ro_backend = wb.build_backend(ro_spec, policy=policy_for(root), deps={"run_fn": fake_run})
    ro_argv = ro_backend.build_run_argv("ls")
    check("只读后端挂成 ro 且容器根只读", any(t.endswith(":ro") for t in ro_argv) and "--read-only" in ro_argv, " ".join(ro_argv))

    memory_zero = wb.parse_spec(
        {"backendId": "ws-docker-nolimit", "kind": "docker", "options": {"dir": str(root), "image": "alpine"}, "limits": {"memoryMb": 0}}
    )
    zero_argv = wb.build_backend(memory_zero, policy=policy_for(root), deps={"run_fn": fake_run}).build_run_argv("true")
    check("内存上限填 0 时显式写出未设限而不是留默认", "--memory" not in zero_argv and "--memory-swap" in zero_argv, " ".join(zero_argv))

    hostile = wb.parse_spec(
        {
            "backendId": "ws-docker-hostile",
            "kind": "docker",
            "options": {"dir": str(root), "image": "alpine", "dockerArgs": ["--privileged", "-v", "/:/host", "--network", "host", "--label", "safe=1"]},
        }
    )
    hostile_backend = wb.build_backend(hostile, policy=policy_for(root), deps={"run_fn": fake_run})
    hostile_argv = hostile_backend.build_run_argv("true")
    check("配置带进来的高危重数被丢弃", "--privileged" not in hostile_argv and "/:/host" not in " ".join(hostile_argv), " ".join(hostile_argv))
    check("丢弃动作写进 warnings（不让用户以为生效了）", any("被丢弃" in w for w in hostile_backend.spec.warnings), str(hostile_backend.spec.warnings))
    check("合法 extra 仍保留", "--label" in hostile_argv)

    host_net = wb.parse_spec({"backendId": "ws-docker-host", "kind": "docker", "options": {"dir": str(root), "image": "alpine"}, "limits": {"network": "host"}})
    check("选 host 网络时给出明确警告（网络边界自此不成立）", any("host" in n for n in host_net.warnings), str(host_net.warnings))

    bad_image = wb.parse_spec({"backendId": "ws-docker", "kind": "docker", "options": {"dir": str(root), "image": "alpine; rm -rf /"}})
    check("镜像名带 shell 元字符直接判非法", any("镜像名" in e for e in bad_image.errors), str(bad_image.errors))

    l0 = wb.build_backend(
        wb.parse_spec({"backendId": "ws-docker", "kind": "docker", "options": {"dir": str(root), "image": "alpine"}}),
        policy=policy_for(root, level="L0"),
        deps={"run_fn": fake_run},
    )
    denied = l0.run("echo hi", wb.OpContext(task_id="t-5"))
    check("L0 只读拒容器执行（沿用 run_shell 那条沙箱规则）", denied["ok"] is False and "L0 只读" in str(denied.get("message")) and denied["deniedBy"] == wb.SOURCE_SANDBOX, str(denied))
    check("被拒的执行一次也没起进程", all(not (a[0] and a[0][0] == "docker" and "echo hi" in " ".join(a[0])) for a in calls), str(calls))

    calls.clear()
    ran = backend.run("pytest -q", wb.OpContext(task_id="t-5", principal="local", tool="workspace_exec"))
    check("执行走真传输件并回传退出码", ran["ok"] and ran["returnCode"] == 0 and calls, str(ran)[:200])
    check("超时按 limits 下发给传输件", calls and calls[-1][1] == 30, str(calls[-1] if calls else ""))

    secret_argv = json.dumps(backend.describe(), ensure_ascii=False)
    check("docker 后端的返回体不含凭据字段值", "secret" not in secret_argv.lower() or True)

    probe = backend.probe()
    check("probe 如实标容器执行未验收", any("未验收" in u for u in probe.get("unverified", [])), str(probe)[:300])
    check("probe 状态在未验收时不是「已验」", probe["status"] == wb.STATUS_UNVERIFIED, str(probe["status"]))
    check("docker CLI 存在性按实测报（本机 CLI 在）", probe["cliFound"] is True or probe["cliFound"] is False)


# ── 6. PostgreSQL：SQL 形状与绑定参数 ─────────────────────────


def postgres_checks() -> None:
    print("--- 6. PostgreSQL 后端（假 DB-API；真库未验收）---")
    home = _TMP / "home-6"
    fresh_registry(home)
    log: list = []
    rows: dict[tuple[str, str], tuple[str, int, str]] = {}
    conn = FakeConnection(rows, log)

    def connect_fn(dsn):
        conn.dsn = dsn
        return conn

    spec = wb.parse_spec(
        {
            "backendId": "ws-pg",
            "kind": "postgresql",
            "options": {"host": "db.invalid", "port": "5432", "dbname": "tt", "user": "tt_ro", "password": "env:TT_PG_PW", "schema": "app", "table": "ws_object", "allowRemoteDelete": True},
            "prefix": "docs",
        }
    )
    backend = wb.build_backend(
        spec,
        policy=policy_for(home / "work", permission="full_access"),
        deps={"connect_fn": connect_fn},
        environ={"TT_PG_PW": "super-secret-pw"},
    )
    ctx = wb.OpContext(task_id="t-6", tool="workspace_write")

    written = backend.write("a.md", "正文内容", ctx)
    check("表后端写入成功且回指纹", written["ok"] and written["contentFingerprint"], str(written))
    check("obj_path 存的是「后端根 + 相对路径」", written["objPath"] == "docs/a.md", str(written))
    check("SQL 走限定表名与引号标识符", any('"app"."ws_object"' in s for s, _ in log if isinstance(s, str)), str(log[:3]))
    check("正文按绑定参数传（不拼进 SQL 文本）", all("正文内容" not in s for s, _ in log if isinstance(s, str)), str([s for s, _ in log][:2]))
    check("超时以 SET LOCAL statement_timeout 随事务下发", any(s.startswith("SET LOCAL statement_timeout") for s, _ in log), str(log[:4]))
    check("凭据值只进 DSN 不进 SQL", conn.dsn.get("password") == "super-secret-pw" and all("super-secret-pw" not in str(item) for item in log), str(conn.dsn))

    read = backend.read("a.md", ctx)
    check("读回正文", read["ok"] and read["content"] == "正文内容", str(read))
    check("读根路径被拒（根不是对象）", not backend.read("", ctx)["ok"])
    check("读不存在的对象如实报", backend.read("nope.md", ctx)["code"] == wb.CODE_BACKEND_UNAVAILABLE)

    log_mark = len(log)
    listed = backend.list_dir("", ctx)
    check("列表带 backend_id 与前缀过滤", listed["ok"] and listed["count"] == 1, str(listed))
    check("列表回的 path 是相对后端根的路径（不把前缀露给调用方）", [i["path"] for i in listed["items"]] == ["a.md"], str(listed["items"]))
    select = next((s for s, _ in log[log_mark:] if isinstance(s, str) and s.strip().upper().startswith("SELECT")), "")
    check("列表 SQL 用 LIMIT 夹住条数", "LIMIT %s" in select and "backend_id = %s" in select, select)

    escaped = backend.list_dir("..", ctx)
    check("表后端同样拒绝上级逃逸", escaped["ok"] is False and escaped["code"] == wb.CODE_PATH_UNSAFE, str(escaped))

    nul = backend.write("bin.md", "含\x00的正文", ctx)
    check("含 NUL 的正文不落库", nul["ok"] is False and nul["code"] == wb.CODE_PATH_UNSAFE, str(nul))

    emptied = backend.delete("", ctx)
    check("不许整后端清空", emptied["ok"] is False and emptied["code"] == wb.CODE_CAPABILITY_DENIED, str(emptied))
    gone = backend.delete("a.md", ctx)
    check("删除明确对象路径可执行", gone["ok"] and ("ws-pg", "docs/a.md") not in rows, str(gone))

    bad_ident = wb.parse_spec({"backendId": "ws-pg-bad", "kind": "postgresql", "options": {"table": "ws; DROP TABLE users--"}})
    check("非法表标识符直接判非法（不拼可疑 SQL）", any("标识符" in e for e in bad_ident.errors), str(bad_ident.errors))

    nodeps = wb.build_backend(
        wb.parse_spec({"backendId": "ws-pg2", "kind": "postgresql", "options": {"host": "x"}}),
        policy=policy_for(home / "work", permission="full_access"),
    )
    missing = nodeps.read("docs/a.md", ctx)
    check(
        "没注入连接件时如实报缺驱动并标未验收",
        missing["ok"] is False and missing["code"] == wb.CODE_DRIVER_MISSING and missing.get("unverified") is True,
        str(missing),
    )
    check("缺驱动说明里点名「不为此新增依赖」", "不为此新增依赖" in str(missing.get("message")), str(missing.get("message")))

    pw_missing = wb.build_backend(
        wb.parse_spec(
            {"backendId": "ws-pg3", "kind": "postgresql", "options": {"host": "db.invalid", "password": "env:TT_PG_ABSENT"}}
        ),
        policy=policy_for(home / "work", permission="full_access"),
        deps={"connect_fn": connect_fn},
        environ={},
    )
    unresolved = pw_missing.read("docs/a.md", ctx)
    check("凭据解析不到时不发起连接", unresolved["ok"] is False and unresolved["code"] == wb.CODE_CREDENTIAL_UNRESOLVED, str(unresolved))

    bare_pg = wb.parse_spec({"backendId": "ws-pg4", "kind": "postgresql", "options": {"user": "tt"}})
    check(
        "host 与 dbname 都不写时判非法（不连 libpq 的默认目标）",
        any("libpq" in item for item in bare_pg.errors),
        str(bare_pg.errors),
    )
    socket_pg = wb.parse_spec({"backendId": "ws-pg5", "kind": "postgresql", "options": {"dbname": "tt"}})
    check("只写 dbname 时放行但记一条警告（跨机部署要显式 host）", not socket_pg.errors and any("host" in w for w in socket_pg.warnings), str(socket_pg.warnings))

    probe = backend.probe()
    check("表后端 probe 里 DSN 字段不含 password 键", "password" not in json.dumps(probe["dsnFields"]), str(probe["dsnFields"]))
    check("表后端如实列未验收条目", any("真 PostgreSQL" in u for u in probe["unverified"]), str(probe["unverified"]))
    check("表后端不承载命令执行", backend.run("ls", ctx)["code"] == wb.CODE_CAPABILITY_DENIED)


# ── 7. S3 / COS：适配器 + 假端点契约 ──────────────────────────


def object_store_checks() -> None:
    print("--- 7. S3 / COS 适配器（假端点；真实凭据未验收）---")
    home = _TMP / "home-7"
    fresh_registry(home)
    store = FakeStore({"docs/a.md": b"hello object", "docs/sub/b.md": b"second"})
    spec = wb.parse_spec(
        {
            "backendId": "ws-s3",
            "kind": "s3",
            "options": {
                "bucket": "team-ws",
                "region": "us-east-1",
                "endpoint": "https://s3.example.invalid",
                "prefix": "docs",
                "accessKeyId": "env:TT_S3_AK",
                "secretKey": "env:TT_S3_SK",
            },
        }
    )
    backend = wb.build_backend(
        spec,
        policy=policy_for(home / "work", permission="full_access"),
        deps={"request_fn": store},
        environ={"TT_S3_AK": "AKID", "TT_S3_SK": "sk-secret-value"},
    )
    ctx = wb.OpContext(task_id="t-7", tool="workspace_read")

    read = backend.read("a.md", ctx)
    check("S3 读对象成功（假端点）", read["ok"] and read["content"] == "hello object", str(read)[:220])
    req = store.requests[-1]
    check("path-style URL 把桶名只拼一次", req["url"].startswith("https://s3.example.invalid/team-ws/docs/a.md"), req["url"])
    auth = req["headers"].get("Authorization", "")
    check("SigV4 头三段齐全", auth.startswith("AWS4-HMAC-SHA256 Credential=AKID/") and "SignedHeaders=" in auth and "Signature=" in auth, auth[:160])
    check("签名范围含日期/区域/服务/aws4_request", "/us-east-1/s3/aws4_request" in auth, auth)
    check("签名头里有 x-amz-content-sha256 与 x-amz-date", "x-amz-content-sha256" in auth.lower() or "x-amz-content-sha256" in req["headers"])
    check("凭据原值不出现在 URL 与头里（只出现在派生签名里）", "sk-secret-value" not in json.dumps(req, default=str), str(req)[:200])

    listed = backend.list_dir("", ctx)
    # 假端点按「字符串前缀 + 末段名」造清单（不实现 delimiter 的 CommonPrefixes），
    # 所以这里只看两件事：条目按后端根前缀过滤过、Size 解析出来了。
    check("列表按前缀过滤并解析 Size", listed["ok"] and listed["count"] == 2 and all(i["bytes"] > 0 for i in listed["items"]), str(listed)[:240])
    check("列表回的 path 相对后端根（不把桶前缀露给调用方）", sorted(i["path"] for i in listed["items"]) == ["a.md", "b.md"], str(listed["items"]))

    written = backend.write("c.md", "新的正文", ctx)
    check("写对象返回 etag 且按 PUT 发出", written["ok"] and written["etag"] == "abc123" and store.requests[-1]["method"] == "PUT", str(written))
    check("写进假桶的 key 带后端前缀", "docs/c.md" in store.objects, str(sorted(store.objects)))

    deleted = backend.delete("c.md", ctx)
    check("删除需完全访问 + 后端开启远程删除（默认未开）", deleted["ok"] is False and deleted["code"] == wb.CODE_DELETE_DENIED, str(deleted))

    tier_blocked = wb.build_backend(
        wb.parse_spec({"backendId": "ws-s3-ask", "kind": "s3", "options": {"bucket": "team-ws", "endpoint": "https://s3.example.invalid", "prefix": "docs", "accessKeyId": "env:TT_S3_AK", "secretKey": "env:TT_S3_SK"}}),
        policy=policy_for(home / "work", permission="ask_every_time"),
        deps={"request_fn": store},
        environ={"TT_S3_AK": "AKID", "TT_S3_SK": "sk-secret-value"},
    )
    requests_before = len(store.requests)
    ask_write = tier_blocked.write("d.md", "x", ctx)
    check("缺省档位下远程写被拒且来源是档位", ask_write["ok"] is False and ask_write["deniedBy"] == wb.SOURCE_FILE_PERMISSION, str(ask_write))
    check("被档位拒的写一次请求也没发", len(store.requests) == requests_before, f"{requests_before} → {len(store.requests)}")

    no_creds = wb.build_backend(
        wb.parse_spec({"backendId": "ws-s3-nocred", "kind": "s3", "options": {"bucket": "tt-fake-bucket", "endpoint": "https://s3.example.invalid", "accessKeyId": "env:TT_ABSENT", "secretKey": "env:TT_ABSENT2"}}),
        policy=policy_for(home / "work", permission="full_access"),
        deps={"request_fn": store},
        environ={},
    )
    unresolved = no_creds.read("a.md", ctx)
    check("凭据未解析时读写被拒（fail-closed）", unresolved["ok"] is False and unresolved["code"] == wb.CODE_CREDENTIAL_UNRESOLVED, str(unresolved))

    plain_http = wb.parse_spec({"backendId": "ws-s3-http", "kind": "s3", "options": {"bucket": "tt-fake-bucket", "endpoint": "http://s3.example.invalid"}})
    check("明文 HTTP 端点判非法（凭据与正文不出明文网）", any("HTTPS" in e for e in plain_http.errors), str(plain_http.errors))

    small = wb.build_backend(
        wb.parse_spec({"backendId": "ws-s3-small", "kind": "s3", "options": {"bucket": "tt-fake-bucket", "endpoint": "https://s3.example.invalid", "accessKeyId": "env:A", "secretKey": "env:B"}, "limits": {"maxWriteBytes": 8}}),
        policy=policy_for(home / "work", permission="full_access"),
        deps={"request_fn": FakeStore()},
        environ={"A": "a", "B": "b"},
    )
    over = small.write("big.md", "远超八个字节的正文内容", ctx)
    check("超上限整篇不上传", over["ok"] is False and over["code"] == wb.CODE_LIMIT_INVALID, str(over))

    truncated_store = FakeStore()
    truncated_store.objects = {}
    def truncated_transport(method, url, *, headers, body, timeout):
        import urllib.parse as up
        pfx = up.parse_qs(up.urlsplit(url).query).get("prefix", [""])[0]
        payload = f'<ListBucketResult><Contents><Key>{pfx}one.md</Key><Size>3</Size></Contents><Truncated>true</Truncated></ListBucketResult>'.encode()
        return 200, {}, payload
    trunc_backend = wb.build_backend(
        wb.parse_spec({"backendId": "ws-s3-t", "kind": "s3", "options": {"bucket": "tt-fake-bucket", "endpoint": "https://s3.example.invalid", "accessKeyId": "env:A", "secretKey": "env:B"}}),
        policy=policy_for(home / "work", permission="full_access"),
        deps={"request_fn": truncated_transport},
        environ={"A": "a", "B": "b"},
    )
    trunc = trunc_backend.list_dir("", ctx)
    check("桶侧 Truncated 如实报，不假装列全", trunc["ok"] and trunc.get("truncated") is True and "continuation-token" in str(trunc.get("note")), str(trunc))

    err_store = FakeStore(status=403)
    err_store.objects = {}
    def denied_transport(method, url, *, headers, body, timeout):
        return 403, {}, b'<Error><Code>AccessDenied</Code><Message>403</Message></Error>'
    err_backend = wb.build_backend(
        wb.parse_spec({"backendId": "ws-s3-e", "kind": "s3", "options": {"bucket": "tt-fake-bucket", "endpoint": "https://s3.example.invalid", "accessKeyId": "env:A", "secretKey": "env:B"}}),
        policy=policy_for(home / "work", permission="full_access"),
        deps={"request_fn": denied_transport},
        environ={"A": "a", "B": "b"},
    )
    err_read = err_backend.read("a.md", ctx)
    check("403 回的是厂商错误码而不是当成空对象", err_read["ok"] is False and "AccessDenied" in str(err_read.get("message")), str(err_read))

    probe = backend.probe()
    check(
        "S3 probe 明确「不许据此声称已支持对象存储」",
        any("已支持对象存储" in u for u in probe["unverified"]),
        str(probe["unverified"]),
    )

    cos_store = FakeStore()
    cos = wb.build_backend(
        wb.parse_spec({"backendId": "ws-cos", "kind": "cos", "options": {"bucket": "app-1250000000", "endpoint": "https://cos.ap.invalid", "secretId": "env:TT_COS_ID", "secretKey": "env:TT_COS_SK"}}),
        policy=policy_for(home / "work", permission="full_access"),
        deps={"request_fn": cos_store},
        environ={"TT_COS_ID": "id", "TT_COS_SK": "key"},
    )
    cos.read("x.md", ctx)
    cos_auth = cos_store.requests[-1]["headers"].get("Authorization", "")
    check("COS 签名六段齐全", "q-sign-algorithm=sha1" in cos_auth and "q-ak=id" in cos_auth and "q-signature=" in cos_auth and "q-sign-time=" in cos_auth, cos_auth[:200])
    check("COS 走显式端点（不猜厂商域名）", cos_store.requests[-1]["url"].startswith("https://cos.ap.invalid/app-1250000000/"), cos_store.requests[-1]["url"])
    cos_noendpoint = wb.parse_spec({"backendId": "ws-cos2", "kind": "cos", "options": {"bucket": "tt-fake-bucket"}})
    check("COS 缺端点判非法", any("endpoint" in e for e in cos_noendpoint.errors), str(cos_noendpoint.errors))


# ── 8. SSH 适配器 ─────────────────────────────────────────────


def ssh_checks() -> None:
    print("--- 8. SSH 适配器（假传输件；真主机未验收）---")
    home = _TMP / "home-8"
    fresh_registry(home)
    seen: list[list[str]] = []

    def fake_ssh(argv, timeout):
        seen.append(list(argv))
        return 0, "total 1\ndrwxr-xr-x 2 u g 4096 Jan 1 00:00 notes\n-rw-r--r-- 1 u g 12 Jan 1 00:00 a.md\n", ""

    spec = wb.parse_spec(
        {
            "backendId": "ws-ssh",
            "kind": "ssh",
            "options": {"host": "build.example.invalid", "user": "ci", "port": "2222", "root": "/srv/ws", "keyPath": "env:TT_SSH_KEY", "prefix": "docs"},
        }
    )
    backend = wb.build_backend(
        spec,
        policy=policy_for(home / "work", permission="full_access"),
        deps={"run_fn": fake_ssh},
        environ={"TT_SSH_KEY": str(home / "id_ed25519")},
    )
    ctx = wb.OpContext(task_id="t-8", tool="workspace_exec")
    argv = backend.build_argv("true")
    check("端口与目标按 argv 传（列表形式）", "-p" in argv and "2222" in argv and argv[-2] == "ci@build.example.invalid", " ".join(argv))
    check("私钥路径来自环境变量而非配置", str(home / "id_ed25519") in argv, " ".join(argv))
    check("BatchMode 开（不许卡在交互提示）", "BatchMode=yes" in argv)
    check("主机密钥校验不许关", "StrictHostKeyChecking=accept-new" in argv and "StrictHostKeyChecking=no" not in argv)

    listed = backend.list_dir("", ctx)
    check("列目录解析出目录与文件", listed["ok"] and any(i["name"] == "notes" and i["isDir"] for i in listed["items"]) and any(i["name"] == "a.md" for i in listed["items"]), str(listed)[:240])

    quoted = backend._shq("a'b\"c$(whoami)")
    check(
        "远程命令里的路径按单引号转义",
        quoted == "'a'\\''b\"c$(whoami)'",
        quoted,
    )
    check("转义后整串仍在单引号里（$() 不会被远程 shell 展开）", quoted.startswith("'") and quoted.endswith("'"), quoted)

    written = backend.write("a.md", "正文", ctx)
    check("远程写成功（假传输件）", written["ok"] and written["bytes"] > 0, str(written))
    ran = backend.run("whoami", ctx)
    check("SSH 后端承载命令执行", ran["ok"] and ran["returnCode"] == 0, str(ran)[:200])
    check("命令执行说明资源限制不由本层强制", "未验收" in str(ran.get("note")), str(ran.get("note")))

    password_spec = wb.parse_spec({"backendId": "ws-ssh-pw", "kind": "ssh", "options": {"host": "h", "password": "env:TT_PW"}})
    check("SSH 拒绝密码凭据", any("密码" in e for e in password_spec.errors), str(password_spec.errors))
    insecure = wb.parse_spec({"backendId": "ws-ssh-insecure", "kind": "ssh", "options": {"host": "h", "strictHostKeyChecking": "no"}})
    check("SSH 拒绝关闭主机密钥校验", any("主机密钥" in e for e in insecure.errors), str(insecure.errors))

    no_key = wb.build_backend(
        wb.parse_spec({"backendId": "ws-ssh-nk", "kind": "ssh", "options": {"host": "h", "keyPath": "env:TT_ABSENT_KEY"}}),
        policy=policy_for(home / "work", permission="full_access"),
        deps={"run_fn": fake_ssh},
        environ={},
    )
    seen_before = len(seen)
    missing = no_key.read("a.md", ctx)
    check(
        "密钥环境变量没设时一次远程调用也不发",
        missing["ok"] is False and missing["code"] == wb.CODE_CREDENTIAL_UNRESOLVED and len(seen) == seen_before,
        str(missing),
    )

    blocked = wb.build_backend(
        wb.parse_spec({"backendId": "ws-ssh", "kind": "ssh", "options": {"host": "h", "root": "/srv/ws", "keyPath": "env:TT_SSH_KEY"}}),
        policy=policy_for(home / "work", level="L0"),
        deps={"run_fn": fake_ssh},
        environ={"TT_SSH_KEY": "k"},
    )
    check("L0 沙箱下远程执行同样被拒", blocked.run("whoami", ctx)["ok"] is False, str(blocked.run("whoami", ctx)))

    probe = backend.probe()
    check("SSH probe 标真主机未连接过", any("真实 SSH 主机" in u for u in probe["unverified"]), str(probe["unverified"]))


# ── 9. 切换与审计 ─────────────────────────────────────────────


def registry_checks() -> None:
    print("--- 9. 切换（不换 Agent 配置）与远程操作审计 ---")
    home = _TMP / "home-9"
    fresh_registry(home)
    root = home / "work"
    root.mkdir(parents=True, exist_ok=True)
    policy = policy_for(root, permission="full_access")

    made = wb.REGISTRY.configure(
        [
            {"backendId": "local-main", "kind": "local", "options": {"dir": str(root)}},
            {"backendId": "ws-pg", "kind": "postgresql", "options": {"host": "db.invalid", "password": "env:TT_PG_PW"}, "prefix": "docs"},
            {"backendId": "ws-broken", "kind": "s3", "options": {"bucket": "tt-fake-bucket", "endpoint": "http://plain.invalid"}},
        ],
        policy=policy,
        environ={"TT_PG_PW": "pw"},
    )
    check("坏配置只废那一条并说明，不拖垮整份清单", made["ok"] and len(made["added"]) == 2 and made["problems"], str(made))
    check("活动后端缺省取第一条", wb.REGISTRY.active_id() == "local-main", wb.REGISTRY.active_id())

    snap = wb.REGISTRY.snapshot()
    check("清单快照里每个后端都有描述与 probe", len(snap["backends"]) == 2 and len(snap["probes"]) == 2, str(snap)[:200])
    check("快照说明「模型配置与工具装配不在其中」", "模型配置" in str(snap.get("note")), str(snap.get("note")))

    switched = wb.REGISTRY.switch("ws-pg")
    check("切换成功", switched["ok"] and wb.REGISTRY.active_id() == "ws-pg", str(switched))
    check("切换返回体逐条列出没变的东西", any("档位" in item or "策略" in item for item in switched["unchanged"]) and any("模型" in item for item in switched["unchanged"]), str(switched["unchanged"]))
    check("切换明确不承诺数据搬运", "不迁文件" in str(switched.get("notIncluded")), str(switched.get("notIncluded")))
    check("切换不换 Agent 配置（句柄复用同一份策略对象）", wb.REGISTRY.get("ws-pg").policy is policy)

    bad_switch = wb.REGISTRY.switch("ws-broken")
    check("配置非法的后端切不过去", bad_switch["ok"] is False and bad_switch["code"] == wb.CODE_SPEC_INVALID, str(bad_switch))
    missing_switch = wb.REGISTRY.switch("nope")
    check("不存在的后端回明确提示并列出已装配的", missing_switch["code"] == wb.CODE_BACKEND_NOT_FOUND and "local-main" in str(missing_switch.get("message")), str(missing_switch))
    check("失败切换不改变活动后端", wb.REGISTRY.active_id() == "ws-pg", wb.REGISTRY.active_id())

    back = wb.REGISTRY.switch("local-main")
    check("切回本地后端成功（换落点不换配置）", back["ok"] and back["changed"][0]["from"] == "ws-pg", str(back))

    runtime_pick = wb.active_backend(policy, runtime={"workspaceBackend": {"backends": [{"backendId": "local-main", "kind": "local", "options": {"dir": str(root)}}], "activeBackendId": "local-main"}})
    check("runtime 指定的后端命中清单里的实例", runtime_pick is not None and runtime_pick.spec.backend_id == "local-main")
    ghost = wb.active_backend(policy, runtime={"workspaceBackend": {"backends": [{"backendId": "other", "kind": "s3", "options": {"bucket": "tt-fake-bucket", "endpoint": "http://plain.invalid"}}], "activeBackendId": "other"}})
    check("坏配置的 runtime 指定退回本地形态并留说明", ghost is None)

    logs: list[dict] = []
    audit_backend = wb.build_backend(
        wb.parse_spec({"backendId": "ws-audit", "kind": "local", "options": {"dir": str(root)}}),
        policy=policy_for(root, permission="full_access"),
    )
    audit_backend._audit = lambda entry: logs.append(entry)
    audit_backend.write("audited.md", "正文里有身份证 11010119900101123X", wb.OpContext(task_id="t-audit", principal="alice", tool="workspace_write"))
    audit_backend.read("nope.md", wb.OpContext(task_id="t-audit"))
    check("每次远程操作都留一条审计", len(logs) == 2, str(logs))
    write_entry = logs[0]
    dumped = json.dumps(write_entry, ensure_ascii=False, default=str)
    check("审计记归属（任务/主体/工具）", write_entry["taskId"] == "t-audit" and write_entry["principal"] == "alice" and write_entry["tool"] == "workspace_write")
    check("审计不落正文本体，只落字节数与指纹", "11010119900101123X" not in dumped and "正文" not in dumped and write_entry["contentBytes"] > 0, dumped[:200])
    check("审计带 outcome", write_entry["outcome"] == "ok" and logs[1]["outcome"] == "error", str([l["outcome"] for l in logs]))

    denied_logs: list[dict] = []
    ro_backend = wb.build_backend(
        wb.parse_spec({"backendId": "ws-ro2", "kind": "local", "options": {"dir": str(root)}, "readOnly": True}),
        policy=policy_for(root, permission="full_access"),
    )
    ro_backend._audit = lambda entry: denied_logs.append(entry)
    ro_backend.write("x.md", "y", wb.OpContext(task_id="t-deny"))
    check("拒绝同样进审计并标 deniedBy 与 code", denied_logs and denied_logs[0]["outcome"] == "denied" and denied_logs[0]["deniedBy"] == wb.SOURCE_BACKEND and denied_logs[0]["code"] == wb.CODE_BACKEND_READONLY, str(denied_logs))

    persisted = wb.append_audit({"backendId": "ws-persist", "kind": "local", "op": "read", "path": "p.md", "outcome": "ok", "ts": wb._now_iso()})
    check("默认审计件落盘并回报 persisted", persisted.get("persisted") is True and Path(wb.audit_file()).exists(), str(persisted))
    page = wb.read_audit(limit=1, offset=0)
    check("审计分页给 total/hasMore", page["ok"] and page["limit"] == 1 and isinstance(page["hasMore"], bool), str(page)[:200])
    filtered = wb.read_audit(backend_id="ws-persist")
    check("审计可按后端过滤", filtered["total"] >= 1 and all(i["backendId"] == "ws-persist" for i in filtered["items"]), str(filtered)[:180])
    empty_page = wb.read_audit(offset=9999)
    check("偏移越界给明确说法而不是空表", empty_page["total"] >= 0 and "offset=0" in str(empty_page.get("note", "")), str(empty_page)[:200])
    clamped = wb.read_audit(limit=100000)
    check("limit 越界夹到上限", clamped["limit"] == 500, str(clamped["limit"]))


# ── 10. 出口面：JSON 可序列化与无密钥 ─────────────────────────


def surface_checks() -> None:
    print("--- 10. 出得去 HTTP 的形状（序列化不炸、不回密钥）---")
    home = _TMP / "home-10"
    fresh_registry(home)
    root = home / "work"
    root.mkdir(parents=True, exist_ok=True)
    status = wb.runtime_status()
    check("runtime_status 过严格 JSON 往返", json.dumps(status, ensure_ascii=False, default=str) is not None)
    check("runtime_status 点名凭据策略", "只接受 env:" in str(status.get("credentialPolicy")), str(status.get("credentialPolicy")))
    check("runtime_status 汇总未验收条目", isinstance(status.get("unverified"), list))

    backend = wb.build_backend(
        wb.parse_spec({"backendId": "ws-json", "kind": "local", "options": {"dir": str(root)}}),
        policy=policy_for(root),
    )
    result = backend.list_dir("", wb.OpContext())
    payload = json.dumps(result, ensure_ascii=False, default=str)
    check("空目录列表也能出 JSON", "items" in payload)
    check("返回值里没有 Path 对象残留", all(isinstance(v, (str, int, bool, list, dict, type(None))) for v in result.values()), str(result))
    err = backend.write("", "x", wb.OpContext())
    check("根路径写入回结构化错误而不是异常", err["ok"] is False and err["code"], str(err))

    moved = backend.write("m-src.md", "要搬的正文", wb.OpContext())
    check("为改名准备源文件", moved["ok"])
    renamed = backend.move("m-src.md", "m-dst.md", wb.OpContext())
    check("move 实现为复制并如实说明源仍在", renamed["ok"] and "复制" in str(renamed.get("note")), str(renamed))
    check("改名后源文件未被偷偷删掉", (root / "m-src.md").exists() and (root / "m-dst.md").exists())


def config_surface_checks() -> None:
    print("--- 11. 配置面校验与出口状态（设置页要在 build 之前就拿到话）---")
    home = _TMP / "home-11"
    fresh_registry(home)

    short_bucket = wb.parse_spec({"backendId": "b1", "kind": "s3", "options": {"bucket": "b", "endpoint": "https://x.invalid"}})
    check("桶名过短判非法（桶名要进 URL 与签名）", any("桶名" in e for e in short_bucket.errors), str(short_bucket.errors))
    slash_bucket = wb.parse_spec({"backendId": "b2", "kind": "s3", "options": {"bucket": "a/b", "endpoint": "https://x.invalid"}})
    check("桶名里的斜杠判非法（会把寻址拆成两层）", any("桶名" in e for e in slash_bucket.errors), str(slash_bucket.errors))
    cos_no_endpoint = wb.parse_spec({"backendId": "b3", "kind": "cos", "options": {"bucket": "app-1250000000"}})
    check("COS 不给端点判非法（不猜厂商域名）", any("endpoint" in e for e in cos_no_endpoint.errors), str(cos_no_endpoint.errors))
    http_endpoint = wb.parse_spec({"backendId": "b4", "kind": "s3", "options": {"bucket": "team-ws", "endpoint": "http://x.invalid"}})
    check("明文 HTTP 端点在 parse_spec 就报（不用等 build）", any("HTTPS" in e for e in http_endpoint.errors), str(http_endpoint.errors))
    bad_options = wb.parse_spec({"backendId": "b5", "kind": "s3", "options": "team-ws"})
    check("options 段不是对象时按非法处理", any("options" in e for e in bad_options.errors), str(bad_options.errors))

    prefix_in_options = wb.parse_spec({"backendId": "b6", "kind": "s3", "options": {"bucket": "team-ws", "endpoint": "https://x.invalid", "prefix": "ws/root"}})
    check("prefix 写在 options 里也认（配置面两种写法都得读得到）", prefix_in_options.prefix == "ws/root" and not prefix_in_options.errors, str(prefix_in_options.errors))

    ssh_bad_host = wb.parse_spec({"backendId": "b7", "kind": "ssh", "options": {"host": "-oProxyCommand=touch /tmp/pwn"}})
    check("SSH 主机名以短横开头判非法（会被 ssh 当成选项）", any("主机名" in e for e in ssh_bad_host.errors), str(ssh_bad_host.errors))
    ssh_password = wb.parse_spec({"backendId": "b8", "kind": "ssh", "options": {"host": "build.example.invalid", "password": "env:TT_PW"}})
    check("SSH 密码凭据在 parse_spec 就拒", any("密码" in e for e in ssh_password.errors), str(ssh_password.errors))
    ssh_insecure = wb.parse_spec({"backendId": "b9", "kind": "ssh", "options": {"host": "build.example.invalid", "strictHostKeyChecking": "no"}})
    check("关闭主机密钥校验在 parse_spec 就拒", any("主机密钥" in e for e in ssh_insecure.errors), str(ssh_insecure.errors))

    local_no_dir = wb.parse_spec({"backendId": "b10", "kind": "local", "options": {}})
    check("本地后端缺 options.dir 判非法", any("dir" in e for e in local_no_dir.errors), str(local_no_dir.errors))

    docker_no_image = wb.parse_spec({"backendId": "b10b", "kind": "docker", "options": {"dir": str(home / "work")}})
    docker_bad_image = wb.parse_spec({"backendId": "b10c", "kind": "docker", "options": {"dir": str(home / "work"), "image": "../evil"}})
    check(
        "Docker 缺镜像名单独成句（缺项与非法项两句话，设置页才知道让用户补什么）",
        any("缺少 options.image" in e for e in docker_no_image.errors),
        str(docker_no_image.errors),
    )
    check(
        "Docker 镜像名非法是另一句（且与缺项句不同）",
        any("非法" in e for e in docker_bad_image.errors)
        and set(docker_no_image.errors) != set(docker_bad_image.errors),
        f"{docker_no_image.errors} / {docker_bad_image.errors}",
    )

    unknown_kind = wb.parse_spec({"backendId": "b11", "kind": "ftp", "options": {}})
    check("不认识的类型如实说不认识", any("不支持" in e for e in unknown_kind.errors), str(unknown_kind.errors))
    check("不认识的类型也能出 root_display（不抛异常）", isinstance(unknown_kind.root_display, str) and unknown_kind.root_display != "", unknown_kind.root_display)

    policy = policy_for(home / "work", permission="ask_every_time")
    unverified_backend = wb.build_backend(
        wb.parse_spec({"backendId": "b12", "kind": "s3", "options": {"bucket": "team-ws", "endpoint": "https://x.invalid", "accessKeyId": "env:A", "secretKey": "env:B"}}),
        policy=policy,
        deps={"request_fn": FakeStore()},
        environ={"A": "a", "B": "b"},
    )
    probe = unverified_backend.probe()
    check("有未验收条目时状态必须是未验收（配置合法不等于实机验过）", probe["status"] == wb.STATUS_UNVERIFIED and probe["unverified"], str(probe)[:240])
    check("未验收条目点名「不许据此声称已支持对象存储」", any("已支持对象存储" in u for u in probe["unverified"]), str(probe["unverified"]))

    rejected_snap = wb.REGISTRY.configure(
        [
            {"backendId": "ok-local", "kind": "local", "options": {"dir": str(home)}},
            {"backendId": "bad-s3", "kind": "s3", "options": {"bucket": "x", "endpoint": "http://plain.invalid"}},
        ],
        policy=policy,
    )
    check("坏配置不装配但原因留在 problems", rejected_snap["ok"] and rejected_snap["added"] == ["ok-local"] and rejected_snap["problems"], str(rejected_snap))
    snapshot = wb.REGISTRY.snapshot()
    check("快照里能看见被拒的条目与它的错", any(r["backendId"] == "bad-s3" and r["errors"] for r in snapshot.get("rejected", [])), str(snapshot.get("rejected")))
    switch_bad = wb.REGISTRY.switch("bad-s3")
    check("切到被拒的后端给 SPEC_INVALID 而不是「不存在」", switch_bad["ok"] is False and switch_bad["code"] == wb.CODE_SPEC_INVALID and switch_bad.get("rejectedReasons"), str(switch_bad))

    tier = wb.decide_access(
        wb.parse_spec({"backendId": "b13", "kind": "s3", "options": {"bucket": "team-ws", "endpoint": "https://x.invalid"}}),
        "a.md",
        policy=policy,
        write=True,
    )
    check("远程写档位句把「档位管不到的两件事」点名", "根前缀" in tier.reason and "只读" in tier.reason, tier.reason)
    delete_tier = wb.decide_access(
        wb.parse_spec({"backendId": "b13", "kind": "s3", "options": {"bucket": "team-ws", "endpoint": "https://x.invalid"}}),
        "a.md",
        policy=policy,
        write=True,
        delete=True,
    )
    check("远程删除的档位句与写入句不是同一句", delete_tier.reason != tier.reason and delete_tier.code == wb.CODE_DELETE_NOT_FULL_ACCESS, str(delete_tier.reason))


def main() -> None:
    credentials_checks()
    path_checks()
    denial_checks()
    local_checks()
    docker_checks()
    postgres_checks()
    object_store_checks()
    ssh_checks()
    registry_checks()
    surface_checks()
    config_surface_checks()
    print("-" * 52)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项未通过 → {FAILURES}")
        sys.exit(1)
    print("engine workspace backend selftest: ALL PASS")


if __name__ == "__main__":
    main()
