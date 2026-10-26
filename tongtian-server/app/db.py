"""SQLite 持久化：账号、会话、市场条目、版本、收藏。

只使用增量建表（CREATE TABLE IF NOT EXISTS），不包含任何删表/覆盖存量数据的语句。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from .config import settings

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    uid            TEXT PRIMARY KEY,
    email          TEXT NOT NULL UNIQUE,
    password_hash  TEXT NOT NULL,
    nickname       TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'user',
    plan           TEXT NOT NULL DEFAULT '个人免费版',
    created_at_ms  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT PRIMARY KEY,
    uid            TEXT NOT NULL,
    refresh_hash   TEXT NOT NULL UNIQUE,
    expires_at_ms  INTEGER NOT NULL,
    revoked        INTEGER NOT NULL DEFAULT 0,
    created_at_ms  INTEGER NOT NULL,
    FOREIGN KEY (uid) REFERENCES users(uid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_uid ON sessions(uid);

CREATE TABLE IF NOT EXISTS items (
    item_type      TEXT NOT NULL,
    item_id        TEXT NOT NULL,
    name           TEXT NOT NULL,
    summary        TEXT NOT NULL DEFAULT '',
    description    TEXT NOT NULL DEFAULT '',
    category       TEXT NOT NULL DEFAULT '其他',
    tags_json      TEXT NOT NULL DEFAULT '[]',
    icon           TEXT NOT NULL DEFAULT '',
    author_uid     TEXT NOT NULL,
    featured       INTEGER NOT NULL DEFAULT 0,
    created_at_ms  INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL,
    PRIMARY KEY (item_type, item_id),
    UNIQUE (item_type, name),
    FOREIGN KEY (author_uid) REFERENCES users(uid)
);
CREATE INDEX IF NOT EXISTS idx_items_type_updated ON items(item_type, updated_at_ms DESC);
CREATE INDEX IF NOT EXISTS idx_items_author ON items(author_uid);

CREATE TABLE IF NOT EXISTS item_versions (
    item_type       TEXT NOT NULL,
    item_id         TEXT NOT NULL,
    version         TEXT NOT NULL,
    readme          TEXT NOT NULL DEFAULT '',
    changelog       TEXT NOT NULL DEFAULT '',
    manifest_json   TEXT NOT NULL DEFAULT '{}',
    package_path    TEXT,
    package_size    INTEGER NOT NULL DEFAULT 0,
    package_sha256  TEXT NOT NULL DEFAULT '',
    published_at_ms INTEGER NOT NULL,
    download_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_type, item_id, version),
    FOREIGN KEY (item_type, item_id) REFERENCES items(item_type, item_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_versions_published
    ON item_versions(item_type, item_id, published_at_ms DESC);

CREATE TABLE IF NOT EXISTS stars (
    uid           TEXT NOT NULL,
    item_type     TEXT NOT NULL,
    item_id       TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (uid, item_type, item_id),
    FOREIGN KEY (uid) REFERENCES users(uid) ON DELETE CASCADE,
    FOREIGN KEY (item_type, item_id) REFERENCES items(item_type, item_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_stars_item ON stars(item_type, item_id);

-- 项目协作：成员移除用软删除（removed=1），不删除历史行；任务/评论/事件同样只追加。
CREATE TABLE IF NOT EXISTS projects (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    owner_uid      TEXT NOT NULL,
    created_at_ms  INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL,
    FOREIGN KEY (owner_uid) REFERENCES users(uid)
);
CREATE INDEX IF NOT EXISTS idx_projects_updated ON projects(updated_at_ms DESC);

CREATE TABLE IF NOT EXISTS project_members (
    project_id     TEXT NOT NULL,
    uid            TEXT NOT NULL,
    role           TEXT NOT NULL DEFAULT 'member',
    removed        INTEGER NOT NULL DEFAULT 0,
    joined_at_ms   INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL,
    PRIMARY KEY (project_id, uid),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (uid) REFERENCES users(uid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_project_members_uid ON project_members(uid, removed);

CREATE TABLE IF NOT EXISTS project_invites (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    email          TEXT NOT NULL,
    role           TEXT NOT NULL,
    token_hash     TEXT NOT NULL UNIQUE,
    invited_by     TEXT NOT NULL,
    expires_at_ms  INTEGER NOT NULL,
    accepted_at_ms INTEGER,
    accepted_by    TEXT,
    created_at_ms  INTEGER NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_project_invites_project
    ON project_invites(project_id, accepted_at_ms);

CREATE TABLE IF NOT EXISTS project_tasks (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL,
    title                 TEXT NOT NULL,
    detail                TEXT NOT NULL DEFAULT '',
    status                TEXT NOT NULL DEFAULT 'todo',
    assignee_uid          TEXT,
    created_by            TEXT NOT NULL,
    version               INTEGER NOT NULL DEFAULT 1,
    result_summary        TEXT NOT NULL DEFAULT '',
    result_artifacts_json TEXT NOT NULL DEFAULT '[]',
    result_machine        TEXT NOT NULL DEFAULT '',
    result_by             TEXT,
    result_at_ms          INTEGER,
    created_at_ms         INTEGER NOT NULL,
    updated_at_ms         INTEGER NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_project_tasks_project
    ON project_tasks(project_id, created_at_ms);

CREATE TABLE IF NOT EXISTS project_comments (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    task_id        TEXT,
    author_uid     TEXT NOT NULL,
    body           TEXT NOT NULL,
    created_at_ms  INTEGER NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_project_comments_project
    ON project_comments(project_id, created_at_ms);

CREATE TABLE IF NOT EXISTS project_events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     TEXT NOT NULL,
    actor_uid      TEXT NOT NULL,
    kind           TEXT NOT NULL,
    payload_json   TEXT NOT NULL DEFAULT '{}',
    created_at_ms  INTEGER NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_project_events_project
    ON project_events(project_id, seq);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(settings.db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def tx() -> Iterator[sqlite3.Connection]:
    with _lock:
        conn = get_conn()
        try:
            with conn:
                yield conn
        finally:
            conn.close()


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.packages_dir.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = get_conn()
        try:
            with conn:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(SCHEMA)
                # 增量补列：旧库升级到收藏软删除，不改动已有行
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(stars)")}
                if "active" not in columns:
                    conn.execute("ALTER TABLE stars ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        finally:
            conn.close()
