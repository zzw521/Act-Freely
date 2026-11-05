"""服务端无头管理面 CLI（缺陷 D-37 的服务端半边）。

桌面上有界面，服务端没有：无头 Linux、容器里、自动化脚本要做的六件事——看服务活没活、
看数据根坏没坏、管账号/部门与档位、配组织两级配额并出预算裁决、出审计合规件、
把数据根整份备份下来并可核对恢复——今天只能手工拼 curl。本模块把这些收成一个稳定命令面：
`python -m app.cli <组> <动作> [...] [--json]`。

三条硬口径：

1. **凭据只从环境变量进**（`TT_SERVER_TOKEN` 或 `TT_SERVER_ADMIN_EMAIL` + `TT_SERVER_ADMIN_PASSWORD`）。
   命令行里出现 `--token` / `--password`，或 URL 里带用户名密码，一律直接拒绝并说明原因——
   argv 会进 shell 历史与进程列表，凭据一旦出现在那里就不属于「本机这一次运行」。
2. **`--json` 是机器面**：一次调用一个 JSON 对象，形状固定为
   `{ok, contractVersion, command, data, problems, exitCode}`，脚本可以稳定解析；
   人读面只往 stdout 打字，错误走 stderr。退出码 `0` 成功 / `1` 操作失败 / `2` 用法错误。
3. **本地面不假设服务在跑**：`doctor` 直接只读打开数据根（`file:...?mode=ro`），
   服务起没起、甚至依赖没装全都能出结论；只有明确需要服务的命令才走 HTTP。
4. **覆盖数据根要显式确认**：`backup restore` 不带 `--yes` 直接拒；带了也先验包、
   覆盖前留 `.pre-restore` 副本、清掉残留 WAL。私钥默认不进备份包，进了就在输出与清单里
   点名它是机密——「备份包 + 库文件同机存放」正好是本仓库最担心的那种泄漏面。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

CLI_CONTRACT_VERSION = "tt-server-cli.v1"
DEFAULT_URL_ENV = "TT_SERVER_URL"
FALLBACK_URL = "http://127.0.0.1:8000"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

# 这几个参数名一旦出现在命令行就说明凭据要走错地方了；不是「未知参数」，要单独拒。
SECRET_FLAGS = ("--token", "--password", "--authorization", "--secret", "--bearer")


class CliError(Exception):
    """操作失败：带稳定 code 与中文原因，`--json` 的 problems 里就是它。

    `data` 允许「失败但也把查到的东西交出去」——巡检类命令（如 `backup verify`）
    既要让退出码说「不干净」，又不能让运维看不到到底哪里不干净。
    """

    def __init__(
        self,
        code: str,
        detail: str,
        exit_code: int = EXIT_FAILED,
        data: Any | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.exit_code = exit_code
        self.data = data


class UsageError(Exception):
    """用法错误：参数写错、命令不存在——与服务无关，单独一档退出码。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class _Parser(argparse.ArgumentParser):
    """argparse 默认把原因打到 stderr 再 sys.exit(2)：那样 `--json` 的 detail 只剩一句「见上面」。

    改成抛 UsageError，真实原因（缺哪个参数、哪个命令不认识）直接进信封，脚本能稳定读到；
    `--help` 走的是 `exit()`，不经过这里，仍然正常返回 0。
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(f"参数不合法：{message}")


# ── 输出信封 ────────────────────────────────────────────────


def envelope(command: str, data: Any, problems: list[dict[str, str]], exit_code: int) -> dict:
    return {
        "ok": not problems and exit_code == EXIT_OK,
        "contractVersion": CLI_CONTRACT_VERSION,
        "command": command,
        "data": data,
        "problems": problems,
        "exitCode": exit_code,
    }


def render(result: dict[str, Any], as_json: bool) -> str:
    if as_json:
        # default=str 是兜底：路径、时间戳这类对象不该因为某个新加的字段让整个命令崩掉。
        return json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    lines: list[str] = []
    data = result["data"]
    if isinstance(data, dict) and data:
        for key, value in data.items():
            lines.append(f"{key}: {_plain(value)}")
    elif isinstance(data, list):
        for item in data:
            lines.append(f"- {_plain(item)}")
    elif data not in (None, "", {}, []):
        lines.append(_plain(data))
    for problem in result["problems"]:
        lines.append(f"✗ [{problem['code']}] {problem['detail']}")
    if not lines:
        lines.append("ok")
    return "\n".join(lines)


def _plain(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


# ── 环境与凭据 ──────────────────────────────────────────────


def base_url(raw: str) -> str:
    """规范化服务地址；只接受 http/https 且不许在 URL 里塞凭据。"""
    value = (raw or os.environ.get(DEFAULT_URL_ENV) or FALLBACK_URL).strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https"):
        raise UsageError(f"只支持 http/https 服务地址，收到：{parsed.scheme or '(空)'}")
    if not parsed.netloc:
        raise UsageError(f"服务地址不合法：{value}")
    if parsed.username or parsed.password:
        raise UsageError("凭据不要写进 URL：请设置环境变量 TT_SERVER_TOKEN")
    if parsed.query or parsed.fragment:
        raise UsageError("服务地址不要带查询串或锚点，请用 --url 指定根地址")
    return value


class Session:
    """一次运行内的凭据与请求封装：令牌只在内存里，不进 argv、不进输出、不进日志。"""

    def __init__(self, url: str, timeout: float) -> None:
        self.url = base_url(url)
        self.timeout = timeout
        self._token = ""
        self.source = ""

    def set_token(self, token: str, source: str) -> None:
        self._token = (token or "").strip()
        self.source = source

    @property
    def token(self) -> str:
        return self._token

    def describe(self) -> dict[str, Any]:
        """输出里只说「令牌从哪来、有没有」，永远不回显内容。"""
        return {
            "url": self.url,
            "authSource": self.source or "none",
            "tokenPresent": bool(self._token),
            "timeoutSeconds": self.timeout,
        }

    def login_from_env(self) -> bool:
        email = (os.environ.get("TT_SERVER_ADMIN_EMAIL") or "").strip()
        password = os.environ.get("TT_SERVER_ADMIN_PASSWORD") or ""
        if not email or not password:
            return False
        body = self.request("POST", "/api/v1/auth/login", json_body={"email": email, "password": password})
        access = str(body.get("accessToken") or "")
        if not access:
            raise CliError("NO_ACCESS_TOKEN", "登录响应里没有 accessToken")
        # 刻意丢掉 refreshToken：管理面不需要续期会话，多留一份就多一处泄漏面。
        self.set_token(access, "env-login")
        return True

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        expect_json: bool = True,
    ) -> Any:
        query = ""
        if params:
            pairs = []
            for key, value in params.items():
                if value is None or value == "":
                    continue
                pairs.append((key, str(value)))
            if pairs:
                query = "?" + urllib.parse.urlencode(pairs)
        data = None
        headers = {"Accept": "application/json"}
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            f"{self.url}{path}{query}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                if not expect_json:
                    return {"status": int(response.status), "text": raw.decode("utf-8", "replace")}
                return _loads(raw)
        except urllib.error.HTTPError as exc:
            # 只取服务端的 code/message；异常对象本身可能带请求信息，绝不整体打印它。
            detail = _http_error_detail(exc)
            raise CliError(
                str(detail.get("code") or f"HTTP_{exc.code}"),
                str(detail.get("message") or f"HTTP {exc.code}"),
            ) from None
        except (urllib.error.URLError, OSError) as exc:
            raise CliError(
                "SERVER_UNREACHABLE",
                f"无法连接 {self.url}：{exc.__class__.__name__}；"
                f"请确认服务已启动或用 --url / {DEFAULT_URL_ENV} 指定地址",
            ) from None


def _loads(raw: bytes) -> Any:
    text = (raw or b"").decode("utf-8", "replace")
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text[:2000]}


def _http_error_detail(exc: urllib.error.HTTPError) -> dict[str, str]:
    body = _loads(exc.read()) if hasattr(exc, "read") else {}
    if isinstance(body, dict):
        code = str(body.get("code") or "")
        message = str(body.get("message") or body.get("detail") or "")
        if code or message:
            return {"code": code or f"HTTP_{exc.code}", "message": message or f"HTTP {exc.code}"}
    return {"code": f"HTTP_{exc.code}", "message": f"HTTP {exc.code}"}


# ── 本地只读面：doctor ──────────────────────────────────────


def open_read_only(path: Any) -> sqlite3.Connection:
    """只读打开：路径做 URI 编码（Windows 的盘符冒号必须转义），服务在跑也读得到已提交的数据。"""
    uri = f"file:{urllib.parse.quote(os.path.abspath(str(path)).replace(os.sep, '/'))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def doctor_data_root() -> tuple[dict[str, Any], list[dict[str, str]]]:
    """直接读数据根出结论：不要求服务在跑，也不写任何东西。"""
    from .config import settings

    problems: list[dict[str, str]] = []
    data_dir = os.path.abspath(str(settings.data_dir))
    db_path = os.path.abspath(str(settings.db_path))
    info: dict[str, Any] = {
        "dataDir": data_dir,
        "dbPath": db_path,
        "dbExists": os.path.exists(db_path),
        "env": settings.environment,
        "production": bool(settings.production),
        # 只报「配了没有」，配置值本身（JWT 密钥、令牌、密码）一个都不打印。
        "jwtSecretConfigured": bool((settings.jwt_secret or "").strip()),
        "registrationEnabled": bool(settings.allow_registration),
        "gatewayProxyConfigured": bool(
            (settings.gateway_admin_url or "").strip() and (settings.gateway_admin_token or "").strip()
        ),
        "archiveConfigured": bool((settings.audit_archive_url or "").strip()),
    }
    if not os.path.isdir(data_dir):
        problems.append({"code": "DATA_DIR_MISSING", "detail": f"数据目录不存在：{data_dir}"})
        return info, problems
    if not info["dbExists"]:
        problems.append({"code": "DB_MISSING", "detail": f"数据库文件不存在：{db_path}"})
    else:
        try:
            conn = open_read_only(db_path)
        except sqlite3.Error as exc:
            problems.append({"code": "DB_UNREADABLE", "detail": f"只读打开失败：{exc}"})
            conn = None
        if conn is not None:
            try:
                info["sqliteIntegrity"] = str(
                    conn.execute("PRAGMA integrity_check").fetchone()[0]
                )
                if info["sqliteIntegrity"] != "ok":
                    problems.append(
                        {"code": "DB_INTEGRITY", "detail": f"integrity_check：{info['sqliteIntegrity']}"}
                    )
                info["journalMode"] = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
                tables = {
                    str(row["name"])
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                expected = ("users", "admin_audit", "audit_anchors", "model_tier_tables")
                missing = [name for name in expected if name not in tables]
                info["tables"] = sorted(tables)
                info["missingTables"] = missing
                if missing:
                    problems.append(
                        {"code": "SCHEMA_INCOMPLETE", "detail": f"缺少表：{', '.join(missing)}"}
                    )
                info["rowCounts"] = {
                    name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                    for name in expected
                    if name in tables
                }
                if "admin_audit" in tables:
                    from .audit import verify_report

                    report = verify_report(conn)
                    info["auditChain"] = {
                        key: report[key]
                        for key in ("entries", "headHash", "brokenAt", "breakCount", "legacyRows", "ok")
                    }
                    if not report["ok"]:
                        problems.append(
                            {
                                "code": "AUDIT_CHAIN_BROKEN",
                                "detail": _chain_problem_detail(report),
                            }
                        )
                if "audit_anchors" in tables:
                    from .audit_anchor import MAX_ANCHORS_PAGE, verify_all
                    from .keys import peek_store

                    # 用目录里已有的公钥验，绝不因为「想验一下」就把密钥库生成了出来
                    store = peek_store(settings.jwt_key_dir)
                    total = int(conn.execute("SELECT COUNT(*) AS n FROM audit_anchors").fetchone()[0])
                    if store is None:
                        info["anchors"] = {
                            "anchors": total,
                            "untrusted": None,
                            "signatureChecked": False,
                            "statement": "这台机器上没有可用的公钥：锚点签名未核验",
                        }
                        if total:
                            problems.append(
                                {
                                    "code": "ANCHOR_SIGNATURE_UNCHECKED",
                                    "detail": f"{total} 个锚点的签名没有核过：没有公钥时"
                                    "「链自洽」只能说明这份库内部一致，不能证明历史没被整体重算",
                                }
                            )
                    else:
                        anchors = verify_all(conn, limit=MAX_ANCHORS_PAGE, store=store)
                        info["anchors"] = {
                            "anchors": anchors["anchors"],
                            "untrusted": anchors["untrusted"],
                            "statement": anchors["statement"],
                        }
                        if anchors["untrusted"]:
                            problems.append(
                                {
                                    "code": "ANCHOR_UNTRUSTED",
                                    "detail": f"{anchors['untrusted']} 个锚点与当前链不一致",
                                }
                            )
                    if not total and int(info["auditChain"]["entries"]) > 0:
                        # 空链没锚点不是问题（还没有东西可归档）；一旦有行又没锚点才是缺口。
                        problems.append(
                            {
                                "code": "NO_ANCHOR",
                                "detail": f"链上已有 {info['auditChain']['entries']} 行却还没有锚点："
                                          "历史无法证明未被整体重算",
                            }
                        )
            finally:
                conn.close()
    try:
        from .keys import peek_keys

        # 用只读的 peek_keys 而不是 key_store()：后者会在空目录里 mkdir + 生成私钥，
        # 到点还会顺手轮换——一次「体检」不该把旧锚点和旧档位表变成验不过。
        peeked = peek_keys(settings.jwt_key_dir)
        info["signingKey"] = {
            "currentKid": peeked["currentKid"],
            "keyCount": int(peeked["keyCount"]),
            "kids": peeked["kids"],
            "keyDir": str(settings.jwt_key_dir),
        }
        if int(peeked["keyCount"]) < 1:
            problems.append(
                {"code": "NO_SIGNING_KEY", "detail": "签名密钥不可用，档位与锚点都签不出"}
            )
        elif not peeked["currentKid"]:
            problems.append(
                {
                    "code": "SIGNING_KEY_NO_CURRENT",
                    "detail": f"密钥目录里有 {peeked['keyCount']} 把钥但清单没指出当前 kid："
                    "签发会失败，请检查 keys/index.json",
                }
            )
    except Exception as exc:  # noqa: BLE001 - 自检阶段任何失败都要如实报，不许抛栈
        problems.append(
            {"code": "KEY_STORE_UNREADABLE", "detail": f"密钥库读不出来：{exc.__class__.__name__}"}
        )
    try:
        info["diskFreeBytes"] = int(shutil.disk_usage(data_dir).free)
    except OSError:
        info["diskFreeBytes"] = -1
    return info, problems


def _chain_problem_detail(report: dict[str, Any]) -> str:
    from .audit import BREAK_REASONS

    if report["truncated"]:
        return f"审计行数超过单次巡检上限 {report['scanLimit']}，本次只检查了前缀"
    first = (report["breaks"] or [{}])[0]
    reason = BREAK_REASONS.get(str(first.get("reason")), "未知原因")
    return f"链在行 {first.get('id')} 处断开：{reason}"


# ── 命令实现 ────────────────────────────────────────────────


def cmd_health(session: Session, args: argparse.Namespace) -> dict[str, Any]:
    body = session.request("GET", "/health")
    return {"service": body.get("service"), "version": body.get("version"), "timeMs": body.get("timeMs")}


def cmd_whoami(session: Session, args: argparse.Namespace) -> dict[str, Any]:
    body = session.request("GET", "/api/v1/auth/me")
    profile = body.get("profile") or {}
    return {
        "uid": profile.get("uid"),
        "email": profile.get("email"),
        "role": profile.get("role"),
        "org": profile.get("org"),
        "depts": profile.get("depts"),
        "auth": session.describe(),
    }


def cmd_users(session: Session, args: argparse.Namespace) -> dict[str, Any]:
    if args.action == "list":
        body = session.request(
            "GET",
            "/api/v1/admin/users",
            params={"q": args.query, "page": args.page, "pageSize": args.page_size},
        )
        return {
            "total": body.get("total"),
            "page": body.get("page"),
            "pageSize": body.get("pageSize"),
            "hasMore": body.get("hasMore"),
            "users": body.get("users") or [],
        }
    if args.action in ("set-role", "set-org", "set-quota"):
        patch: dict[str, Any] = {}
        if args.action == "set-role":
            patch["role"] = args.role
        if args.action == "set-org":
            patch["org"] = args.org
        if args.action == "set-quota":
            # QuotaIn 只有这两个字段；extra="forbid"，多塞一个整张请求就被拒。
            # 两个字段都不给默认值：服务端对 concurrentTasks 的下限是 1，
            # 把「没填」当成 0 发出去只会收到一句 INVALID_QUOTA，运维看不出是自己没填。
            quota: dict[str, int] = {}
            if args.monthly_tokens is not None:
                quota["monthlyTokens"] = int(args.monthly_tokens)
            if args.concurrent_tasks is not None:
                quota["concurrentTasks"] = int(args.concurrent_tasks)
            if not quota:
                raise UsageError(
                    "set-quota 至少要给 --monthly-tokens 或 --concurrent-tasks 之一"
                    "（清除某项限额请显式传值：monthlyTokens 可为 0，concurrentTasks 最小 1）"
                )
            patch["quota"] = quota
        body = session.request("PATCH", f"/api/v1/admin/users/{args.uid}", json_body=patch)
        # 如实带上这条：改组织/部门/配额/角色会让旧令牌立刻失效，脚本必须知道。
        return {
            "profile": body.get("profile"),
            "sessionsRevoked": body.get("sessionsRevoked"),
        }
    if args.action == "revoke-sessions":
        body = session.request("POST", f"/api/v1/admin/users/{args.uid}/revoke-sessions")
        return {"revoked": body.get("revoked")}
    raise UsageError(f"未知的 users 动作：{args.action}")


def cmd_departments(session: Session, args: argparse.Namespace) -> dict[str, Any]:
    if args.action == "list":
        body = session.request("GET", "/api/v1/admin/departments", params={"org": args.org})
        return {"departments": body.get("departments") or []}
    if args.action == "create":
        if not args.org:
            raise UsageError("创建部门必须给 --org")
        body = session.request(
            "POST",
            "/api/v1/admin/departments",
            json_body={"org": args.org, "id": args.dept_id, "name": args.name or ""},
        )
        return {"department": body.get("department") or body}
    if args.action == "rename":
        if not args.org:
            raise UsageError("改名必须给 --org")
        body = session.request(
            "PATCH",
            f"/api/v1/admin/departments/{urllib.parse.quote(args.org)}/{urllib.parse.quote(args.dept_id)}",
            json_body={"name": args.name},
        )
        return {"department": body.get("department") or body}
    raise UsageError(f"未知的 depts 动作：{args.action}")


def cmd_orgs(session: Session, args: argparse.Namespace) -> dict[str, Any]:
    action = args.action
    if action == "list":
        body = session.request("GET", "/api/v1/admin/orgs", params={"period": args.period})
        return {
            "contractVersion": body.get("contractVersion"),
            "period": body.get("period"),
            "total": body.get("total"),
            "orgs": body.get("orgs") or [],
        }
    if action == "create":
        body = session.request(
            "POST", "/api/v1/admin/orgs", json_body={"org": args.org, "name": args.name or ""}
        )
        return {
            "contractVersion": body.get("contractVersion"),
            "created": body.get("created"),
            "org": body.get("org"),
            "name": body.get("name"),
        }
    if action == "quota":
        if args.quota_action == "get":
            body = session.request(
                "GET", f"/api/v1/admin/orgs/{urllib.parse.quote(args.org)}/quota"
            )
            return {
                "contractVersion": body.get("contractVersion"),
                "org": body.get("org"),
                "quota": body.get("quota"),
                "statement": body.get("statement"),
            }
        if args.quota_action == "set":
            patch: dict[str, Any] = {}
            for flag, key in (
                ("monthly_tokens", "monthlyTokens"),
                ("concurrent_tasks", "concurrentTasks"),
                ("member_monthly_tokens", "memberMonthlyTokens"),
                ("member_concurrent_tasks", "memberConcurrentTasks"),
            ):
                value = getattr(args, flag, None)
                if value is not None:
                    # 0 是有意义的值（= 不限制），不许用 `or` 把它当「没填」丢掉
                    patch[key] = int(value)
            enforce = getattr(args, "enforce", None)
            if enforce is not None:
                patch["enforce"] = enforce == "on"
            if not patch:
                raise UsageError(
                    "orgs quota set 至少要给 --monthly-tokens / --concurrent-tasks / "
                    "--member-monthly-tokens / --member-concurrent-tasks / --enforce 之一"
                    "（0 = 不限制，与引擎判定同一口径）"
                )
            body = session.request(
                "PUT", f"/api/v1/admin/orgs/{urllib.parse.quote(args.org)}/quota", json_body=patch
            )
            return {
                "contractVersion": body.get("contractVersion"),
                "org": body.get("org"),
                "quota": body.get("quota"),
                # 拦停开关翻转会不会把人踢下线，运维必须看得见这两个数
                "sessionsRevoked": body.get("sessionsRevoked"),
                "activeSessions": body.get("activeSessions"),
                "statement": body.get("statement"),
                "next": body.get("next"),
            }
        raise UsageError(f"未知的 quota 动作：{args.quota_action}（可用：get / set）")
    if action == "budget":
        body = session.request(
            "GET",
            f"/api/v1/admin/orgs/{urllib.parse.quote(args.org)}/budget",
            params={"period": args.period},
        )
        budget = body.get("budget") or {}
        return {
            "contractVersion": body.get("contractVersion"),
            "org": body.get("org"),
            "period": body.get("period"),
            "quota": body.get("quota"),
            "budget": budget,
            "usage": body.get("usage"),
            "attribution": body.get("attribution"),
            "disclosed": body.get("disclosed") or [],
            "statement": (
                f"限额 {budget.get('limit')}，已用 {budget.get('used')}，"
                f"剩余 {budget.get('remaining')}，判定 {budget.get('verdict')}"
            ),
        }
    if action == "members":
        body = session.request(
            "GET", f"/api/v1/admin/orgs/{urllib.parse.quote(args.org)}/members"
        )
        return {
            "contractVersion": body.get("contractVersion"),
            "org": body.get("org"),
            "total": body.get("total"),
            "members": body.get("members") or [],
            "note": body.get("note"),
        }
    raise UsageError(f"未知的 orgs 动作：{action}")


def cmd_tiers(session: Session, args: argparse.Namespace) -> dict[str, Any]:
    # list / verify / effective 这三个动作的作用域是服务端算的，命令行里没有位置参数，
    # 直接 args.scope_kind 会 AttributeError；缺省按全局作用域走。
    kind = getattr(args, "scope_kind", "") or "global"
    ident = getattr(args, "scope_id", "") or "-"
    path = f"/api/v1/admin/model-tiers/{urllib.parse.quote(kind)}/{urllib.parse.quote(ident)}"
    if args.action == "list":
        body = session.request("GET", "/api/v1/admin/model-tiers", params={"limit": args.limit})
        return {
            "contractVersion": body.get("contractVersion"),
            "tiers": body.get("tiers"),
            "tables": body.get("tables") or [],
        }
    if args.action == "verify":
        body = session.request("GET", "/api/v1/admin/model-tiers/verify")
        return {
            "tables": body.get("tables"),
            "untrusted": body.get("untrusted"),
            "items": body.get("items") or [],
        }
    if args.action == "effective":
        body = session.request("GET", "/api/v1/model-tiers/effective")
        return {"revision": body.get("revision"), "document": body.get("document") or body}
    if args.action == "get":
        params: dict[str, Any] = {}
        if args.revision is not None:
            params["revision"] = args.revision
        if args.revisions:
            params["revisions"] = args.revisions
        body = session.request("GET", path, params=params)
        return {"table": body}
    if args.action == "publish":
        document = _read_json_file(args.from_file)
        body = session.request("PUT", path, json_body=document)
        return {
            "changed": body.get("changed"),
            "revision": body.get("revision"),
            "digest": body.get("digest"),
        }
    if args.action == "revert":
        if args.revision is None:
            raise UsageError("revert 必须给 --revision")
        body = session.request(
            "POST", f"{path}/revert", json_body={"revision": int(args.revision)}
        )
        return {
            "revision": body.get("revision"),
            "digest": body.get("digest"),
            "revertToRevision": body.get("revertToRevision"),
        }
    if args.action == "withdraw":
        body = session.request("DELETE", path)
        return {
            "withdrawn": body.get("withdrawn"),
            "clientBehavior": body.get("clientBehavior"),
        }
    raise UsageError(f"未知的 tiers 动作：{args.action}")


def _read_json_file(path: str) -> Any:
    """文档从文件读，不从命令行传：档位表正文很长，塞进 argv 会进进程列表与历史。"""
    if not path:
        raise UsageError("publish 必须给 --from-file <路径>")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise UsageError(f"文件不存在：{path}") from None
    except json.JSONDecodeError as exc:
        raise UsageError(f"{path} 不是合法 JSON：{exc}") from None
    except OSError as exc:
        raise CliError("FILE_UNREADABLE", f"读不出 {path}：{exc}") from None


def cmd_audit(session: Session, args: argparse.Namespace) -> dict[str, Any]:
    action = args.action
    if action == "verify":
        return {"chain": session.request("GET", "/api/v1/admin/audit/verify")}
    if action == "list":
        body = session.request(
            "GET", "/api/v1/admin/audit", params={"after": args.after, "limit": args.limit}
        )
        return {
            "entries": body.get("entries") or [],
            "nextAfter": body.get("nextAfter"),
            "headHash": body.get("headHash"),
        }
    if action == "export":
        text = session.request(
            "GET",
            "/api/v1/admin/audit/export",
            params={"after": args.after, "limit": args.limit},
            expect_json=False,
        )
        return _write_or_count(args.out, str(text.get("text") or ""))
    if action == "compliance":
        body = session.request("GET", "/api/v1/admin/audit/compliance", params={"days": args.days})
        if args.brief:
            return {
                "trustworthy": (body.get("verdict") or {}).get("trustworthy"),
                "statement": (body.get("verdict") or {}).get("statement"),
                "errors": [i for i in body.get("findings") or [] if i.get("level") == "error"],
                "warnings": [i for i in body.get("findings") or [] if i.get("level") == "warn"],
            }
        return {"report": body}
    if action == "compliance-export":
        text = session.request(
            "GET",
            "/api/v1/admin/audit/compliance/export",
            params={"days": args.days},
            expect_json=False,
        )
        return _write_or_count(args.out, str(text.get("text") or ""))
    if action == "anchors":
        body = session.request("GET", "/api/v1/admin/audit/anchors", params={"limit": args.limit})
        return {"anchors": body.get("anchors") or [], "archive": body.get("archive")}
    if action == "anchor-verify":
        body = session.request("GET", "/api/v1/admin/audit/anchor/verify", params={"limit": args.limit})
        return {
            "anchors": body.get("anchors"),
            "untrusted": body.get("untrusted"),
            "statement": body.get("statement"),
            "items": body.get("items") or [],
        }
    if action == "anchor-create":
        body = session.request("POST", "/api/v1/admin/audit/anchor")
        anchor = body.get("anchor") or {}
        result: dict[str, Any] = {
            "seq": anchor.get("seq"),
            "fromId": anchor.get("fromId"),
            "toId": anchor.get("toId"),
            "entries": anchor.get("entries"),
            "headHash": anchor.get("headHash"),
            "digest": anchor.get("digest"),
            "archive": body.get("archive"),
        }
        # 签名串默认不打：输出常被重定向进日志，而签名 + 库文件同时外泄就等于把伪造材料寄出去。
        if args.print_signature:
            result["signature"] = anchor.get("signature")
        return result
    if action == "archive":
        body = session.request(
            "POST",
            "/api/v1/admin/audit/anchor/archive",
            json_body=None if args.seq is None else {"seq": int(args.seq)},
        )
        return {
            "pushed": body.get("pushed"),
            "archiveState": body.get("archiveState"),
            "archiveRef": body.get("archiveRef"),
            "archiveError": body.get("archiveError"),
            "reason": body.get("reason"),
        }
    if action == "archive-status":
        body = session.request(
            "GET", "/api/v1/admin/audit/archive/status", params={"limit": args.limit}
        )
        return {
            "archive": body.get("archive"),
            "byState": body.get("byState"),
            "recent": body.get("recent") or [],
        }
    raise UsageError(f"未知的 audit 动作：{action}")


def _write_or_count(out: str, text: str) -> dict[str, Any]:
    if not out:
        lines = [line for line in text.splitlines() if line.strip()]
        return {"bytes": len(text.encode("utf-8")), "lines": len(lines), "preview": lines[:3]}
    try:
        directory = os.path.dirname(os.path.abspath(out))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError as exc:
        raise CliError("FILE_UNWRITABLE", f"写不出 {out}：{exc}") from None
    return {"written": os.path.abspath(out), "bytes": len(text.encode("utf-8"))}


# ── 起服务（无头运维的「启」） ──────────────────────────────

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def cmd_serve(session: Session, args: argparse.Namespace) -> dict[str, Any]:
    """前台把服务端拉起来：无头机器上 `python -m app.cli serve` 就是安装与启停的全部所需。

    「停」不在这里：让一个能带令牌远程调用「停」的入口存在，等于多开一处可被滥用的
    拒绝服务面；容器 stop / systemctl stop / Ctrl-C 才是这一步的正确做法。
    """
    host = (args.host or "127.0.0.1").strip()
    if host not in LOOPBACK_HOSTS and not args.allow_public:
        raise UsageError(
            f"要绑 {host} 就得显式加 --allow-public：非回环地址一绑，这台机器上的"
            "市场、账号与审计面就进了局域网；默认只允许 127.0.0.1/localhost/::1"
        )
    # `or` 会把 --port 0 悄悄变成 8000：运维以为起了，其实监听在另一个端口上
    port = int(args.port if args.port is not None else 8000)
    if port < 1 or port > 65535:
        raise UsageError(f"端口需在 1–65535 之间，收到：{port}")
    try:
        import uvicorn

        from .config import settings as serve_settings
        from .main import app
    except ImportError as exc:
        raise CliError(
            "SERVE_UNAVAILABLE",
            f"起服务需要 fastapi/uvicorn 等依赖，当前导入失败：{exc}；"
            "请在服务端环境里运行（tongtian-server/.venv）",
        ) from None

    started = time.monotonic()
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=args.log_level,
        reload=False,
    )
    server = uvicorn.Server(config)
    # 阻塞到收到退出信号；uvicorn 自己处理 SIGINT/SIGTERM 的优雅停机
    server.run()
    return {
        "host": host,
        "port": port,
        "logLevel": args.log_level,
        "runtimeSeconds": round(time.monotonic() - started, 1),
        "stopped": True,
        "dataDir": str(serve_settings.data_dir),
    }


# ── 数据根备份与恢复 ────────────────────────────────────────

BACKUP_CONTRACT_VERSION = "tt-server-backup.v1"
BACKUP_DB_NAME = "market.db"
BACKUP_MANIFEST_NAME = "manifest.json"
SUPPORTED_BACKUP_CONTRACTS = (BACKUP_CONTRACT_VERSION,)

# 清单逐表记行数的表：这几张是「谁是谁、能干到哪、留了什么证据」的底账，恢复对不对就看它们。
# 其余表只记总数——清单不该变成全库镜像，那样备份包自己就成了一处需要保密的副本。
BACKUP_LEDGER_TABLES = (
    "users",
    "sessions",
    "orgs",
    "departments",
    "user_departments",
    "admin_audit",
    "audit_anchors",
    "model_tier_tables",
    "model_tier_revisions",
    # 组织配额是「谁能花多少」的底账：恢复后不对，整个两级裁决就是空的
    "org_quotas",
)


def _sha256_file(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(256 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _snapshot_into(db_path: str, target_path: str) -> int:
    """用 SQLite 在线备份 API 拷一致快照：服务在跑也拿得到「已提交」的那个状态。

    不用文件级拷贝：WAL 模式下主文件与 `-wal` 分两段拷会拿回一个「半新不旧」的库，
    那种备份恢复出来比不备份更危险。
    """
    source = open_read_only(db_path)
    try:
        target = sqlite3.connect(target_path)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
    finally:
        source.close()
    return int(os.path.getsize(target_path))


def _snapshot_facts(db_path: str) -> dict[str, Any]:
    """读一份库出事实：只读、不建目录、不碰密钥库。"""
    conn = open_read_only(db_path)
    try:
        tables = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        facts: dict[str, Any] = {
            "integrity": str(conn.execute("PRAGMA integrity_check").fetchone()[0]),
            "tableCount": len(tables),
            "rowCounts": {},
        }
        for name in BACKUP_LEDGER_TABLES:
            if name in tables:
                facts["rowCounts"][name] = int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
        if "admin_audit" in tables:
            from .audit import verify_report

            report = verify_report(conn)
            facts["auditChain"] = {
                key: report[key]
                for key in ("entries", "headHash", "brokenAt", "breakCount", "legacyRows", "ok")
            }
        if "audit_anchors" in tables:
            total = int(conn.execute("SELECT COUNT(*) AS n FROM audit_anchors").fetchone()[0])
            top = (
                conn.execute(
                    "SELECT seq, head_hash, signing_kid, signature FROM audit_anchors "
                    "ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                if total
                else None
            )
            facts["anchors"] = {
                "count": total,
                "newestSeq": int(top["seq"]) if top is not None else None,
                "newestHeadHash": str(top["head_hash"]) if top is not None else "",
                "newestKid": str(top["signing_kid"]) if top is not None else "",
                # 只把签名本身摘要进清单：验签要的是公钥，而公钥属于目标机的密钥库。
                # 「备份包里的锚点是否可信」这句话只能由恢复后的 anchor-verify 来说。
                "newestSignature": hashlib.sha256(
                    str(top["signature"] or "").encode("utf-8")
                ).hexdigest()[:16]
                if top is not None
                else "",
                "signatureChecked": False,
            }
        return facts
    finally:
        conn.close()


def _load_manifest(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise CliError("BACKUP_MANIFEST_INVALID", f"{path} 不是合法 JSON：{exc}") from None
    except OSError as exc:
        raise CliError("BACKUP_MANIFEST_UNREADABLE", f"读不出清单 {path}：{exc}") from None
    if not isinstance(payload, dict):
        raise CliError("BACKUP_MANIFEST_INVALID", f"{path} 的内容不是对象")
    return payload


def cmd_backup(session: Session, args: argparse.Namespace) -> dict[str, Any]:
    from .config import settings

    if args.action == "create":
        return _backup_create(settings, args)
    if args.action == "verify":
        return _backup_verify(args.package)
    if args.action == "restore":
        return _backup_restore(settings, args)
    raise UsageError(f"未知的 backup 动作：{args.action}")


def _backup_create(settings: Any, args: argparse.Namespace) -> dict[str, Any]:
    db_path = os.path.abspath(str(settings.db_path))
    if not os.path.exists(db_path):
        raise CliError("BACKUP_NO_DB", f"数据根里没有库文件：{db_path}——备份一个空目录没有意义")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    default_out = os.path.join(os.path.abspath(str(settings.data_dir)), "backups", f"tt-backup-{stamp}")
    directory = os.path.abspath(args.out or default_out)
    manifest_path = os.path.join(directory, BACKUP_MANIFEST_NAME)
    if os.path.exists(manifest_path) and not args.force:
        raise CliError(
            "BACKUP_DIR_TAKEN",
            f"{directory} 已经是一份备份包（有 {BACKUP_MANIFEST_NAME}）：换个目录或明确加 --force 覆盖",
        )
    os.makedirs(directory, exist_ok=True)

    snapshot_path = os.path.join(directory, BACKUP_DB_NAME)
    staging = snapshot_path + ".writing"
    try:
        _snapshot_into(db_path, staging)
        os.replace(staging, snapshot_path)
    except (sqlite3.Error, OSError) as exc:
        try:
            os.remove(staging)
        except OSError:
            pass
        raise CliError(
            "BACKUP_CREATE_FAILED",
            f"快照写不出：{exc.__class__.__name__}: {str(exc)[:200]}",
        ) from None

    digest, size = _sha256_file(snapshot_path)
    facts = _snapshot_facts(snapshot_path)
    warnings: list[str] = []
    files = [{"name": BACKUP_DB_NAME, "bytes": size, "sha256": digest}]

    key_dir = os.path.abspath(str(settings.jwt_key_dir))
    if args.include_keys:
        if os.path.isdir(key_dir):
            packed = os.path.join(directory, "keys")
            shutil.copytree(key_dir, packed, dirs_exist_ok=True)
            for name in sorted(os.listdir(packed)):
                full = os.path.join(packed, name)
                if os.path.isfile(full):
                    item_digest, item_size = _sha256_file(full)
                    files.append({"name": f"keys/{name}", "bytes": item_size, "sha256": item_digest})
            warnings.append(
                "包里含 RS256 私钥：这份备份包本身等于全部签名能力，按机密对待，"
                "不要与数据库文件分开各存一份还都放在同一台机器上"
            )
        else:
            warnings.append(f"要求带密钥但目录不存在：{key_dir}（包里没有 keys/）")
    else:
        warnings.append(
            "包里没有签名私钥：恢复后旧锚点与旧档位表在新密钥库下验不过，"
            "这是「证据还在但没法自证」的状态——要么把密钥目录单独备份，"
            "要么恢复后重新签发锚点（新锚点只对之后的历史作证）"
        )

    manifest = {
        "contractVersion": BACKUP_CONTRACT_VERSION,
        "createdAtMs": int(time.time() * 1000),
        "createdUtc": stamp,
        "env": settings.environment,
        "source": {"dataDir": os.path.abspath(str(settings.data_dir)), "dbPath": db_path},
        "files": files,
        "keysIncluded": bool(args.include_keys and os.path.isdir(os.path.join(directory, "keys"))),
        "tableCount": facts.get("tableCount"),
        "rowCounts": facts.get("rowCounts") or {},
        "auditChain": facts.get("auditChain") or {},
        "anchors": facts.get("anchors") or {},
        "integrity": facts.get("integrity"),
        "restoreNotes": [
            "恢复前先跑 verify；restore 会再跑一次，坏包不放行",
            "恢复要在服务停下来以后做：覆盖打开中的库文件在 Linux 上不报错，"
            "但服务端已持有的句柄会继续读到旧内容",
        ],
    }
    try:
        with open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
    except OSError as exc:
        raise CliError("BACKUP_CREATE_FAILED", f"清单写不出：{exc}") from None

    return {
        "package": directory,
        "manifest": manifest_path,
        "db": snapshot_path,
        "bytes": size,
        "sha256": digest,
        "keysIncluded": manifest["keysIncluded"],
        "auditChain": manifest["auditChain"],
        "rowCounts": manifest["rowCounts"],
        "anchors": manifest["anchors"],
        "warnings": warnings,
    }


def _backup_verify(raw_package: str) -> dict[str, Any]:
    directory = os.path.abspath(str(raw_package or ""))
    manifest_path = os.path.join(directory, BACKUP_MANIFEST_NAME)
    if not os.path.exists(manifest_path):
        raise CliError(
            "BACKUP_PACKAGE_MISSING",
            f"{directory} 里没有 {BACKUP_MANIFEST_NAME}：不是本工具产出的备份包",
        )
    manifest = _load_manifest(manifest_path)
    if str(manifest.get("contractVersion")) not in SUPPORTED_BACKUP_CONTRACTS:
        raise CliError(
            "BACKUP_CONTRACT_UNSUPPORTED",
            f"备份包口径 {manifest.get('contractVersion')} 本版本读不了"
            f"（支持：{', '.join(SUPPORTED_BACKUP_CONTRACTS)}）",
            data={"package": directory, "manifest": manifest},
        )

    findings: list[str] = []
    checked: list[dict[str, Any]] = []
    for item in manifest.get("files") or []:
        name = str(item.get("name") or "")
        path = os.path.join(directory, name)
        if not os.path.exists(path):
            findings.append(f"{name} 不在包里")
            continue
        digest, size = _sha256_file(path)
        wanted_digest = str(item.get("sha256") or "")
        wanted_bytes = int(item.get("bytes") or -1)
        matched = digest == wanted_digest and size == wanted_bytes
        checked.append({"name": name, "bytes": size, "sha256": digest, "matches": matched})
        if not matched:
            findings.append(
                f"{name} 与清单不符：清单 {wanted_bytes} 字节 /摘要 ……{wanted_digest[-8:]}，"
                f"实际 {size} 字节 /摘要 ……{digest[-8:]}"
            )

    db_path = os.path.join(directory, BACKUP_DB_NAME)
    facts: dict[str, Any] = {}
    if os.path.exists(db_path):
        facts = _snapshot_facts(db_path)
        if facts.get("integrity") != "ok":
            findings.append(f"库文件自检不干净：{facts.get('integrity')}")
        for name, count in (manifest.get("rowCounts") or {}).items():
            got = (facts.get("rowCounts") or {}).get(name)
            if got != int(count):
                findings.append(f"{name} 行数对不上：清单 {count}，实际 {got}")
        want_chain = manifest.get("auditChain") or {}
        got_chain = facts.get("auditChain") or {}
        if want_chain:
            if not got_chain:
                findings.append("备份包里没有 admin_audit 表，无法比对链头")
            else:
                if str(got_chain.get("headHash")) != str(want_chain.get("headHash")):
                    findings.append(
                        f"链头与清单不一致：清单 ……{str(want_chain.get('headHash'))[-12:]}，"
                        f"实际 ……{str(got_chain.get('headHash'))[-12:]}（包里的审计库被换过或截断过）"
                    )
                if got_chain.get("ok") is not True:
                    findings.append("备份包里的链自身就不自洽：这份不能当干净基线")
        want_anchors = manifest.get("anchors") or {}
        got_anchors = facts.get("anchors") or {}
        if want_anchors and want_anchors.get("count"):
            if not got_anchors:
                findings.append("备份包里没有 audit_anchors 表，但清单记着有锚点")
            else:
                if int(got_anchors.get("count") or 0) != int(want_anchors.get("count") or 0):
                    findings.append(
                        f"锚点数量对不上：清单 {want_anchors.get('count')}，实际 {got_anchors.get('count')}"
                    )
                if str(got_anchors.get("newestSignature")) != str(want_anchors.get("newestSignature")):
                    findings.append("最新锚点的签名与清单不符（证据被换过）")
    elif not any(str(item.get("name")) == BACKUP_DB_NAME for item in manifest.get("files") or []):
        findings.append(f"包里既没有 {BACKUP_DB_NAME}，清单也没记它")

    data = {
        "package": directory,
        "manifest": manifest,
        "contractVersion": manifest.get("contractVersion"),
        "createdAtMs": manifest.get("createdAtMs"),
        "files": checked,
        "rowCounts": (facts or {}).get("rowCounts") or {},
        "auditChain": (facts or {}).get("auditChain") or {},
        "anchors": (facts or {}).get("anchors") or {},
        "integrity": (facts or {}).get("integrity"),
        "keysIncluded": bool(manifest.get("keysIncluded")),
        "findings": findings,
        # 一句话说清这次核验覆盖到哪：验的是「包与自己清单一致 + 快照里的链自洽」，
        # 不含锚点签名可信性——那要目标机的公钥才能判。
        "statement": (
            "包内容、行数与链头同清单一致，快照内审计链自洽（未核验锚点签名可信性）"
            if not findings
            else f"{len(findings)} 项与清单不一致"
        ),
    }
    if findings:
        detail = "；".join(findings[:5])
        if len(findings) > 5:
            detail += f"（共 {len(findings)} 项）"
        raise CliError("BACKUP_VERIFY_FAILED", detail, data=data)
    return data


def _backup_restore(settings: Any, args: argparse.Namespace) -> dict[str, Any]:
    if not args.yes:
        raise UsageError(
            "restore 必须显式加 --yes：这一步会用备份包覆盖数据根里的库文件，覆盖完不可回退"
        )
    # 坏包不放行：verify 内部失败会带 data 抛出，运维直接看到到底哪里不一致
    data = _backup_verify(args.package)
    directory = str(data["package"])

    target_dir = os.path.abspath(str(args.to or settings.data_dir))
    target_db = os.path.join(target_dir, BACKUP_DB_NAME)
    if os.path.exists(target_db) and os.path.getsize(target_db) > 0 and not args.allow_overwrite:
        raise CliError(
            "BACKUP_TARGET_EXISTS",
            f"{target_db} 已有非空库文件：确认要覆盖请加 --allow-overwrite"
            "（覆盖前会先在旁边留一份 .pre-restore 副本）",
            data={"target": target_db, "package": directory},
        )

    safety = ""
    warnings: list[str] = []
    try:
        os.makedirs(target_dir, exist_ok=True)
        if os.path.exists(target_db):
            safety = f"{target_db}.pre-restore-{int(time.time())}"
            shutil.copy2(target_db, safety)
        staging = f"{target_db}.restoring-{int(time.time())}"
        shutil.copy2(os.path.join(directory, BACKUP_DB_NAME), staging)
        os.replace(staging, target_db)
        # 旧的 WAL/SHM 必须清掉：它们记的是覆盖前那批页，留着会把更新的数据「复活」到新库上
        for suffix in ("-wal", "-shm"):
            stale = target_db + suffix
            if os.path.exists(stale):
                os.remove(stale)
    except (OSError, shutil.Error) as exc:
        raise CliError(
            "BACKUP_RESTORE_FAILED",
            f"恢复失败：{exc.__class__.__name__}: {str(exc)[:200]}。"
            "服务端可能正持有这个库——请先停服务再恢复",
            data={"target": target_db, "safetyCopy": safety},
        ) from None

    keys_restored = False
    packed_keys = os.path.join(directory, "keys")
    if os.path.isdir(packed_keys):
        if args.with_keys:
            live_keys = os.path.abspath(str(settings.jwt_key_dir))
            shutil.copytree(packed_keys, live_keys, dirs_exist_ok=True)
            keys_restored = True
            warnings.append(f"已把包里的密钥目录覆盖到 {live_keys}：这是私钥，确认这台机器可信")
        else:
            warnings.append(
                "包里有 keys/ 但没给 --with-keys：只恢复了库。"
                "旧锚点与旧档位表在当前密钥库下可能验不过，需要连密钥一起恢复请加 --with-keys"
            )
            warnings.append("想核对是不是密钥换了而不是数据被改了：用目标机现有公钥跑 audit anchor-verify")

    restored = _snapshot_facts(target_db)
    manifest = data["manifest"]
    restored_counts = restored.get("rowCounts") or {}
    mismatched = [
        f"{name}: 清单 {count}，恢复后 {restored_counts.get(name)}"
        for name, count in (manifest.get("rowCounts") or {}).items()
        if restored_counts.get(name) != int(count)
    ]
    chain_want = str((manifest.get("auditChain") or {}).get("headHash") or "")
    chain_got = str((restored.get("auditChain") or {}).get("headHash") or "")
    if chain_want and chain_got and chain_want != chain_got:
        mismatched.append(f"链头与清单不一致：清单 ……{chain_want[-12:]}，恢复后 ……{chain_got[-12:]}")
    if mismatched:
        raise CliError(
            "BACKUP_RESTORE_MISMATCH",
            "恢复后的库与清单不符：" + "；".join(mismatched[:5]),
            data={
                "target": target_db,
                "safetyCopy": safety,
                "rowCounts": restored.get("rowCounts"),
                "findings": mismatched,
            },
        )
    warnings.append(
        "恢复完成请重起服务端再跑 doctor：库是这份包的内容，但签名密钥没恢复的话锚点验不过"
    )
    return {
        "restored": target_db,
        "safetyCopy": safety,
        "keysRestored": keys_restored,
        "package": directory,
        "rowCounts": restored.get("rowCounts"),
        "auditChain": restored.get("auditChain"),
        "anchors": restored.get("anchors"),
        "integrity": restored.get("integrity"),
        "warnings": warnings,
    }


# ── 参数解析 ────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    # 全局参数同时挂在顶层和每个子命令上：`--json` 放前放后都要认，
    # 因为脚本作者写的是 `app.cli doctor --json`，不是 `app.cli --json doctor`。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default="", help=f"服务地址（默认取 {DEFAULT_URL_ENV}，缺省 {FALLBACK_URL}）")
    common.add_argument("--timeout", type=float, default=10.0, help="单次请求超时（秒）")
    common.add_argument("--json", action="store_true", help="输出机器可读 JSON（一个对象，形状固定）")
    # 顶层用 _Parser，argparse 会把同一个类传给所有子解析器，用法错误才会统一成信封
    parser = _Parser(
        prog="python -m app.cli",
        description="通天办公服务端管理面（无头可用）",
        epilog=(
            "凭据只从环境变量读取：TT_SERVER_TOKEN，或 TT_SERVER_ADMIN_EMAIL + TT_SERVER_ADMIN_PASSWORD。\n"
            "不接受 --token / --password：命令行会进 shell 历史与进程列表。"
        ),
        parents=[common],
    )
    sub = parser.add_subparsers(dest="group", required=True)

    health = sub.add_parser("health", help="服务存活与版本", parents=[common])
    health.set_defaults(handler=cmd_health, needs_token=False)

    serve = sub.add_parser("serve", help="前台拉起服务端（默认只绑回环地址）", parents=[common])
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--log-level",
        dest="log_level",
        default="info",
        choices=("critical", "error", "warning", "info", "debug", "trace"),
    )
    serve.add_argument(
        "--allow-public",
        dest="allow_public",
        action="store_true",
        help="确认允许把服务绑到非回环地址",
    )
    serve.set_defaults(handler=cmd_serve, needs_token=False)

    doctor = sub.add_parser(
        "doctor", help="本机自检：数据根、库完整性、表、审计链、锚点、密钥", parents=[common]
    )
    doctor.set_defaults(handler=None, needs_token=False)

    whoami = sub.add_parser("whoami", help="当前令牌对应的账号", parents=[common])
    whoami.set_defaults(handler=cmd_whoami, needs_token=True)

    users = sub.add_parser("users", help="账号：列表、角色、组织、配额、撤销会话", parents=[common])
    users_sub = users.add_subparsers(dest="action", required=True)
    list_users = users_sub.add_parser("list", help="分页列账号", parents=[common])
    list_users.add_argument("--query", default="")
    list_users.add_argument("--page", type=int, default=1)
    list_users.add_argument("--page-size", dest="page_size", type=int, default=20)
    role = users_sub.add_parser("set-role", help="改角色（旧会话会立刻失效）", parents=[common])
    role.add_argument("uid")
    role.add_argument("role", choices=("user", "operator", "auditor", "admin"))
    org = users_sub.add_parser("set-org", help="改所属组织（旧会话会立刻失效）", parents=[common])
    org.add_argument("uid")
    org.add_argument("--org", default="", required=True)
    quota = users_sub.add_parser("set-quota", help="改个人配额（组织级配额见 orgs）", parents=[common])
    quota.add_argument("uid")
    quota.add_argument("--monthly-tokens", dest="monthly_tokens", type=int, default=None)
    quota.add_argument("--concurrent-tasks", dest="concurrent_tasks", type=int, default=None)
    revoke = users_sub.add_parser("revoke-sessions", help="撤销该账号全部在用的会话", parents=[common])
    revoke.add_argument("uid")
    users.set_defaults(needs_token=True)

    depts = sub.add_parser("depts", help="部门：列表、建、改名", parents=[common])
    depts_sub = depts.add_subparsers(dest="action", required=True)
    depts_list = depts_sub.add_parser("list", help="列部门（可限定组织）", parents=[common])
    depts_list.add_argument("--org", default="")
    depts_create = depts_sub.add_parser("create", help="建部门", parents=[common])
    depts_create.add_argument("dept_id")
    depts_create.add_argument("--org", default="")
    depts_create.add_argument("--name", default="")
    depts_rename = depts_sub.add_parser("rename", help="改部门名", parents=[common])
    depts_rename.add_argument("dept_id")
    depts_rename.add_argument("--org", default="")
    depts_rename.add_argument("--name", default="", required=True)
    depts.set_defaults(needs_token=True)

    orgs = sub.add_parser(
        "orgs", help="组织与两级配额：建、配额、预算裁决、成员落点", parents=[common]
    )
    orgs_sub = orgs.add_subparsers(dest="action", required=True)
    orgs_list = orgs_sub.add_parser("list", help="列组织（含配额与当期已用）", parents=[common])
    orgs_list.add_argument(
        "--period", default="monthly", choices=("daily", "monthly", "total")
    )
    orgs_create = orgs_sub.add_parser("create", help="建组织或改名", parents=[common])
    orgs_create.add_argument("org")
    orgs_create.add_argument("--name", default="")
    orgs_quota = orgs_sub.add_parser("quota", help="读/写组织配额", parents=[common])
    orgs_quota_sub = orgs_quota.add_subparsers(dest="quota_action", required=True)
    orgs_quota_get = orgs_quota_sub.add_parser("get", help="读组织配额", parents=[common])
    orgs_quota_get.add_argument("org")
    orgs_quota_get.set_defaults(handler=cmd_orgs)
    orgs_quota_set = orgs_quota_sub.add_parser(
        "set", help="写组织配额（0 = 不限制，与引擎判定同口径）", parents=[common]
    )
    orgs_quota_set.add_argument("org")
    orgs_quota_set.add_argument("--monthly-tokens", dest="monthly_tokens", type=int, default=None)
    orgs_quota_set.add_argument(
        "--concurrent-tasks", dest="concurrent_tasks", type=int, default=None
    )
    orgs_quota_set.add_argument(
        "--member-monthly-tokens", dest="member_monthly_tokens", type=int, default=None
    )
    orgs_quota_set.add_argument(
        "--member-concurrent-tasks", dest="member_concurrent_tasks", type=int, default=None
    )
    orgs_quota_set.add_argument(
        "--enforce", default=None, choices=("on", "off"),
        help="on 才让组织预算真正参与签发裁决；off 只报表",
    )
    orgs_quota_set.set_defaults(handler=cmd_orgs)
    orgs_budget = orgs_sub.add_parser("budget", help="一页裁决：额度、已用、剩余、超售", parents=[common])
    orgs_budget.add_argument("org")
    orgs_budget.add_argument("--period", default="monthly", choices=("daily", "monthly", "total"))
    orgs_members = orgs_sub.add_parser(
        "members", help="每个成员下次签发会拿到的两个数与来源", parents=[common]
    )
    orgs_members.add_argument("org")
    orgs.set_defaults(needs_token=True)

    tiers = sub.add_parser("tiers", help="模型档位：发布、回看、回滚、验签", parents=[common])
    tiers_sub = tiers.add_subparsers(dest="action", required=True)
    tiers_list = tiers_sub.add_parser("list", help="列当前各作用域的表", parents=[common])
    tiers_list.add_argument("--limit", type=int, default=50)
    tiers_verify = tiers_sub.add_parser("verify", help="逐张复算摘要并验签", parents=[common])
    tiers_effective = tiers_sub.add_parser("effective", help="当前令牌该拿到的档位表", parents=[common])
    tiers_get = tiers_sub.add_parser("get", help="取某作用域的表（可指定历史版本）", parents=[common])
    tiers_get.add_argument("scope_kind", nargs="?", default="global")
    tiers_get.add_argument("scope_id", nargs="?", default="-")
    tiers_get.add_argument("--revision", type=int, default=None)
    tiers_get.add_argument("--revisions", type=int, default=0, help="顺带返回最近 N 个历史版本")
    for name, help_text in (
        ("publish", "从 JSON 文件发布档位表"),
        ("revert", "把历史某版重新发布成新版本"),
        ("withdraw", "撤销该作用域，客户端回落上一层"),
    ):
        item = tiers_sub.add_parser(name, help=help_text, parents=[common])
        item.add_argument("scope_kind", nargs="?", default="global")
        item.add_argument("scope_id", nargs="?", default="-")
        if name == "publish":
            item.add_argument("--from-file", dest="from_file", default="", required=True)
        if name == "revert":
            item.add_argument("--revision", type=int, default=None, required=True)
    tiers.set_defaults(needs_token=True)

    audit = sub.add_parser("audit", help="审计：验链、翻页、导出、合规报告、锚点与归档", parents=[common])
    audit_sub = audit.add_subparsers(dest="action", required=True)
    audit_sub.add_parser("verify", help="全链复算", parents=[common])
    for name in ("list", "export"):
        item = audit_sub.add_parser(name, help="按 id 游标翻页" if name == "list" else "NDJSON 导出", parents=[common])
        item.add_argument("--after", type=int, default=0)
        item.add_argument("--limit", type=int, default=100)
        if name == "export":
            item.add_argument("--out", default="", help="写出到文件；不给就只报条数与预览")
    for name in ("compliance", "compliance-export"):
        item = audit_sub.add_parser(
            name,
            help="合规报告 JSON" if name == "compliance" else "合规报告 Markdown 下载",
            parents=[common],
        )
        item.add_argument("--days", type=int, default=30)
        if name == "compliance":
            item.add_argument("--brief", action="store_true", help="只给结论与问题清单")
        else:
            item.add_argument("--out", default="")
    for name in ("anchors", "anchor-verify", "archive-status"):
        item = audit_sub.add_parser(name, help="列锚点" if name == "anchors" else "归档状态", parents=[common])
        item.add_argument("--limit", type=int, default=20)
    anchor_create = audit_sub.add_parser("anchor-create", help="给当前全链打签名锚点", parents=[common])
    anchor_create.add_argument(
        "--print-signature",
        dest="print_signature",
        action="store_true",
        help="把签名串打到输出（默认不打：输出常被重定向进日志）",
    )
    archive = audit_sub.add_parser("archive", help="把一个锚点推到远端归档", parents=[common])
    archive.add_argument("--seq", type=int, default=None, help="缺省推最新一个锚点")
    audit.set_defaults(needs_token=True)

    backup = sub.add_parser(
        "backup", help="数据根备份：建包、验包、恢复（纯本地，不需要令牌）", parents=[common]
    )
    backup_sub = backup.add_subparsers(dest="action", required=True)
    backup_create = backup_sub.add_parser("create", help="给数据根建一份一致快照", parents=[common])
    backup_create.add_argument("--out", default="", help="备份目录（缺省落在数据根的 backups/ 下）")
    backup_create.add_argument(
        "--include-keys",
        dest="include_keys",
        action="store_true",
        help="把 RS256 私钥目录一起打进包（这份包从此等同于全部签名能力）",
    )
    backup_create.add_argument("--force", action="store_true", help="目标目录已是备份包时覆盖")
    backup_verify = backup_sub.add_parser("verify", help="复算摘要、行数与链头", parents=[common])
    backup_verify.add_argument("package")
    backup_restore = backup_sub.add_parser("restore", help="用备份包覆盖数据根的库", parents=[common])
    backup_restore.add_argument("package")
    backup_restore.add_argument("--to", default="", help="目标数据根（缺省当前数据根）")
    backup_restore.add_argument("--yes", action="store_true", help="确认这一步会覆盖数据根")
    backup_restore.add_argument(
        "--allow-overwrite", dest="allow_overwrite", action="store_true", help="目标已有非空库时仍然覆盖"
    )
    backup_restore.add_argument(
        "--with-keys", dest="with_keys", action="store_true", help="连包里的 keys/ 一起恢复（含私钥）"
    )
    backup.set_defaults(needs_token=False)

    # 处理器按「组」挂一遍，而不是在每个动作上手写一遍：漏挂的动作会带着 0 个 handler
    # 走到运行期才报「未实现的命令」，而新增动作的人不会记得去补第 N 处 set_defaults。
    for group_sub, group_handler in (
        (users_sub, cmd_users),
        (depts_sub, cmd_departments),
        (orgs_sub, cmd_orgs),
        (tiers_sub, cmd_tiers),
        (audit_sub, cmd_audit),
        (backup_sub, cmd_backup),
    ):
        for item in group_sub.choices.values():
            item.set_defaults(handler=group_handler)
    return parser


def resolve_token(session: Session) -> None:
    token = (os.environ.get("TT_SERVER_TOKEN") or "").strip()
    if token:
        session.set_token(token, "env-token")
        return
    if session.login_from_env():
        return
    raise UsageError(
        "缺少凭据：请设置 TT_SERVER_TOKEN，或 TT_SERVER_ADMIN_EMAIL + TT_SERVER_ADMIN_PASSWORD"
    )


def reject_secret_flags(argv: list[str]) -> None:
    for item in argv:
        name = item.split("=", 1)[0]
        if name in SECRET_FLAGS:
            raise UsageError(
                f"凭据不要进命令行（{name}）：请设置环境变量 TT_SERVER_TOKEN，"
                "或 TT_SERVER_ADMIN_EMAIL + TT_SERVER_ADMIN_PASSWORD——命令行会进 shell 历史与进程列表"
            )


def _emit(result: dict[str, Any], as_json: bool) -> None:
    text = render(result, as_json)
    if result["exitCode"] == EXIT_OK:
        print(text)
    else:
        print(text, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in raw
    command = "usage"
    try:
        reject_secret_flags(raw)
        try:
            parsed_args = build_parser().parse_args(raw)
        except SystemExit as exc:
            # 只剩 `--help` 会走到这里（用法错误已被 _Parser 改成 UsageError）。
            # 正常退出原样放行；万一是非零退出，也翻成同一个信封。
            if exc.code in (0, None):
                raise
            raise UsageError("参数不合法：命令或选项无法解析") from None
        args = parsed_args
        action = getattr(args, "action", None)
        # `orgs quota set` 是两段动作，command 要回显到叶子，否则脚本看到的
        # "orgs quota" 分不清读的还是写的
        nested = getattr(args, "quota_action", None)
        if action and nested:
            action = f"{action} {nested}"
        command = f"{args.group} {action}" if action else args.group
        session = Session(args.url, max(0.5, float(args.timeout)))

        if args.group == "doctor":
            info, problems = doctor_data_root()
            info["auth"] = session.describe()
            info["checkedAtMs"] = int(time.time() * 1000)
            code = EXIT_OK if not problems else EXIT_FAILED
            _emit(envelope(command, info, problems, code), as_json)
            return code

        if args.needs_token:
            resolve_token(session)
        handler = getattr(args, "handler", None)
        if handler is None:
            raise UsageError(f"未实现的命令：{command}")
        _emit(envelope(command, handler(session, args), [], EXIT_OK), as_json)
        return EXIT_OK
    except UsageError as exc:
        problems = [{"code": "USAGE", "detail": str(exc)}]
        _emit(envelope(command, None, problems, EXIT_USAGE), as_json)
        return EXIT_USAGE
    except CliError as exc:
        problems = [{"code": exc.code, "detail": exc.detail}]
        _emit(envelope(command, exc.data, problems, exc.exit_code), as_json)
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001
        # 兜底：机器面承诺「一次调用一个 JSON 对象」，任何意外都不许把裸 traceback 当输出。
        # 只报异常类型与一行原因，栈留在退出码与 code 里让维护者去复现。
        problems = [
            {
                "code": "CLI_INTERNAL",
                "detail": f"命令内部异常：{exc.__class__.__name__}: {str(exc)[:200]}",
            }
        ]
        _emit(envelope(command, None, problems, EXIT_FAILED), as_json)
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
