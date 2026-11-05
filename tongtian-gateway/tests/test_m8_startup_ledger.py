"""M8.1：账本启动门槛——连不上或版本过高就不放进程出去。

M7.3 实测过旧行为：账本连接是懒式的，进程照常启动并对外服务，直到第一个请求才回 500。
本套测试验证 `enforce_ledger()` 将校验提前到启动期，并且错误消息不回显 DSN 与口令。
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support import check, report  # noqa: E402
from app.config import Settings, load_settings  # noqa: E402
from app.db import SCHEMA_VERSION, Database  # noqa: E402
from app.startup import _ledger_error_detail, enforce_ledger  # noqa: E402

PG_PASSWORD = "tt-m8-secret-pw"


def _closed_port() -> int:
    """本机上一个确定没人监听的端口：让 Postgres 连接立刻失败。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _settings(db_url: str = "") -> Settings:
    saved_url = os.environ.get("TT_GATEWAY_DB_URL")
    saved_dir = os.environ.get("TT_GATEWAY_DATA_DIR")
    if db_url:
        os.environ["TT_GATEWAY_DB_URL"] = db_url
    else:
        os.environ.pop("TT_GATEWAY_DB_URL", None)
    os.environ["TT_GATEWAY_DATA_DIR"] = tempfile.mkdtemp(prefix="tt-m8-data-")
    try:
        return load_settings()
    finally:
        for name, value in (
            ("TT_GATEWAY_DB_URL", saved_url),
            ("TT_GATEWAY_DATA_DIR", saved_dir),
        ):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _raise_message(fn) -> str:
    try:
        fn()
    except RuntimeError as exc:
        return str(exc)
    return ""


def test_healthy_ledger(tmp: Path) -> None:
    db = Database(tmp / "ok.db")
    status = enforce_ledger(db, _settings())
    check(
        "健康 SQLite 账本通过启动校验",
        status == {"backend": "sqlite", "connected": True, "schemaVersion": SCHEMA_VERSION},
        str(status),
    )
    db.close()


def test_newer_schema_refused(tmp: Path) -> None:
    path = tmp / "newer.db"
    db = Database(path)
    db.ensure()
    newer = SCHEMA_VERSION + 92
    with db.tx() as conn:
        conn.execute("UPDATE schema_version SET version = ?", (newer,))
    db.close()

    again = Database(path)
    message = _raise_message(lambda: enforce_ledger(again, _settings()))
    check(
        "schema 版本过高时拒绝启动并点名两个版本",
        message.startswith("账本不可用，拒绝启动：backend=sqlite")
        and f"版本 {newer} 高于程序支持的 {SCHEMA_VERSION}" in message,
        message,
    )
    # 用裸连接读：走 Database 会被同一道门槛挡住，无法证明“没改写回”
    raw = sqlite3.connect(str(path), isolation_level=None)
    try:
        stored = int(raw.execute("SELECT version FROM schema_version").fetchone()[0])
    finally:
        raw.close()
    check("拒绝启动不改写库里的版本", stored == newer, f"库里 {stored}，期望 {newer}")
    again.close()


def test_unusable_sqlite_refused(tmp: Path) -> None:
    # 把一个已存在的目录当成库文件：SQLite 打不开
    blocked = tmp / "blocked-dir"
    blocked.mkdir()
    db = Database(blocked)
    message = _raise_message(lambda: enforce_ledger(db, _settings()))
    check(
        "SQLite 打不开时拒绝启动",
        message.startswith("账本不可用，拒绝启动：backend=sqlite"),
        message,
    )
    db.close()


def test_postgres_unreachable_refused(tmp: Path) -> None:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        print("SKIP 未安装 psycopg，跳过 Postgres 启动门槛用例")
        return
    url = f"postgresql://ttgw_app:{PG_PASSWORD}@127.0.0.1:{_closed_port()}/ttgw"
    db = Database(tmp / "unused.db", url=url)
    message = _raise_message(lambda: enforce_ledger(db, _settings(url)))
    check(
        "Postgres 不可达时拒绝启动",
        message.startswith("账本不可用，拒绝启动：backend=postgres"),
        message,
    )
    check("启动错误不回显口令", PG_PASSWORD not in message, message)
    check("启动错误不回显完整 DSN", url not in message, message)
    db.close()


def test_error_detail_redaction() -> None:
    url = f"postgresql://ttgw_app:{PG_PASSWORD}@db.internal:5432/ttgw"
    settings = _settings(url)
    cases = {
        "URL 内嵌口令": (
            f'connection to "{url}" failed',
            url,
            PG_PASSWORD,
        ),
        "关键字形式口令": (
            "connection failed: password=" + PG_PASSWORD + " host=db.internal",
            "password=" + PG_PASSWORD,
            PG_PASSWORD,
        ),
        "只留第一行": (
            "unable to open database file\nTraceback (most recent call last):\n  secret tail",
            "Traceback",
            "",
        ),
    }
    leaked: list[str] = []
    for name, (raw, must_drop, secret) in cases.items():
        cleaned = _ledger_error_detail(raw, settings)
        if must_drop in cleaned:
            leaked.append(f"{name} 未脱敏：{cleaned}")
        if secret and secret in cleaned:
            leaked.append(f"{name} 泄漏口令：{cleaned}")
    check("驱动错误脱敏后不回显连接串/口令/堆栈尾", not leaked, "; ".join(leaked))
    check("脱敏后仍保留可运维的第一行", "unable to open database file" in
          _ledger_error_detail(cases["只留第一行"][0], settings))
    check("超长错误被截断", len(_ledger_error_detail("x" * 900, settings)) <= 200)


def _boot(env: dict[str, str], timeout: int = 90) -> tuple[int, str]:
    """按验收脚本同样的方式起真实进程：lifespan 抛错时 uvicorn 退出码非 0。"""
    proc = subprocess.run(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(_closed_port())],
        cwd=str(ROOT),
        env=_clean_env(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _clean_env(overrides: dict[str, str]) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("TT_")}
    env.update(overrides)
    return env


def test_process_refuses_to_serve(tmp: Path) -> None:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        print("SKIP 未安装 psycopg，跳过真实进程拒绝启动用例")
        return
    url = f"postgresql://ttgw_app:{PG_PASSWORD}@127.0.0.1:{_closed_port()}/ttgw"
    began_env = {
        "TT_GATEWAY_ENV": "development",
        "TT_GATEWAY_DATA_DIR": str(tmp / "proc-data"),
        "TT_GATEWAY_DB_URL": url,
    }
    code, text = _boot(began_env)
    check("账本不可达时真实进程不进入服务", code != 0, f"退出码 {code}")
    check("进程日志点名账本不可用", "账本不可用，拒绝启动" in text, text[-400:])
    check("进程日志不含口令", PG_PASSWORD not in text)
    check("进程日志不含完整 DSN", url not in text)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="tt-m8-ledger-"))
    test_healthy_ledger(tmp)
    test_newer_schema_refused(tmp)
    test_unusable_sqlite_refused(tmp)
    test_postgres_unreachable_refused(tmp)
    test_error_detail_redaction()
    test_process_refuses_to_serve(tmp)
    return report("M8 账本启动门槛自测通过")


if __name__ == "__main__":
    raise SystemExit(main())
