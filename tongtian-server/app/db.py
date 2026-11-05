"""SQLite 持久化：账号、会话、市场条目、版本、收藏、项目协作与工单。

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
    org_id         TEXT NOT NULL DEFAULT '',
    quota_json     TEXT NOT NULL DEFAULT '{}',
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
    revoked_at_ms   INTEGER NOT NULL DEFAULT 0,
    revoked_reason  TEXT NOT NULL DEFAULT '',
    revoked_by      TEXT NOT NULL DEFAULT '',
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

-- 用量上报（缺陷 D-43）：桌面与引擎各自按「设备 × 来源 × 用户 × 模型 × 日 × 来源通道」
-- 累计后上报，服务端按主键覆盖，同一批数据重复上报不会重复计数；同一账号的多台设备
-- 各有自己的 device_id，互不覆盖。只写本库，不读取桌面 tongtian.db 与引擎台账文件。
CREATE TABLE IF NOT EXISTS usage_rollup (
    source            TEXT NOT NULL,
    device_id         TEXT NOT NULL DEFAULT '',
    sub               TEXT NOT NULL,
    org_id            TEXT NOT NULL DEFAULT '',
    dept_id           TEXT NOT NULL DEFAULT '',
    model_name        TEXT NOT NULL,
    day               TEXT NOT NULL,
    origin            TEXT NOT NULL DEFAULT '',
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    calls             INTEGER NOT NULL DEFAULT 0,
    priced_calls      INTEGER NOT NULL DEFAULT 0,
    unpriced_calls    INTEGER NOT NULL DEFAULT 0,
    latency_samples   INTEGER NOT NULL DEFAULT 0,
    latency_ms_total  INTEGER NOT NULL DEFAULT 0,
    costs_json        TEXT NOT NULL DEFAULT '{}',
    last_used_at_ms   INTEGER NOT NULL DEFAULT 0,
    reported_at_ms    INTEGER NOT NULL,
    PRIMARY KEY (source, device_id, sub, model_name, day, origin)
);
CREATE INDEX IF NOT EXISTS idx_usage_rollup_sub_day ON usage_rollup(sub, day);
-- idx_usage_rollup_org_dept 不在建表脚本里声明：旧库的 usage_rollup 没有 dept_id，
-- executescript 会早于增量补列失败（no such column: dept_id）。该索引由 init_db
-- 在补列之后创建，新库与旧库的最终索引一致。

-- 网关用量幂等键（M1.4）：每条网关事件一行，只留判重与取数的最小字段；
-- 30 天后清理，清理前的重放一律判重，不会重复计数。
CREATE TABLE IF NOT EXISTS gateway_usage_events (
    gateway_id        TEXT NOT NULL,
    request_id        TEXT NOT NULL,
    batch_id          TEXT NOT NULL DEFAULT '',
    tenant_id         TEXT NOT NULL,
    dept_id           TEXT NOT NULL DEFAULT '',
    sub               TEXT NOT NULL,
    model             TEXT NOT NULL,
    day               TEXT NOT NULL,
    status            TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost_micros       INTEGER NOT NULL DEFAULT 0,
    currency          TEXT NOT NULL DEFAULT '',
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    received_at_ms    INTEGER NOT NULL,
    PRIMARY KEY (gateway_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_gateway_usage_received
    ON gateway_usage_events(received_at_ms);

-- 管理操作审计：只记动作与对象；密钥、令牌类字段在写入前脱敏。
CREATE TABLE IF NOT EXISTS admin_audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ms        INTEGER NOT NULL,
    actor_uid    TEXT NOT NULL,
    actor_role   TEXT NOT NULL DEFAULT 'admin',
    action       TEXT NOT NULL,
    object       TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    before_json  TEXT NOT NULL DEFAULT '{}',
    after_json   TEXT NOT NULL DEFAULT '{}',
    prev_hash    TEXT NOT NULL DEFAULT '',
    entry_hash   TEXT NOT NULL DEFAULT '',
    status       INTEGER NOT NULL DEFAULT 200
);
CREATE INDEX IF NOT EXISTS idx_admin_audit_at ON admin_audit(at_ms DESC);

-- 组织与部门（M1.3）：用户可属于多个部门，is_primary=1 是换票默认部门。
CREATE TABLE IF NOT EXISTS orgs (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    created_at_ms  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS departments (
    org_id         TEXT NOT NULL,
    id             TEXT NOT NULL,
    name           TEXT NOT NULL,
    created_at_ms  INTEGER NOT NULL,
    PRIMARY KEY (org_id, id),
    FOREIGN KEY (org_id) REFERENCES orgs(id)
);
CREATE INDEX IF NOT EXISTS idx_departments_org ON departments(org_id);

CREATE TABLE IF NOT EXISTS user_departments (
    uid            TEXT NOT NULL,
    org_id         TEXT NOT NULL,
    dept_id        TEXT NOT NULL,
    is_primary     INTEGER NOT NULL DEFAULT 0,
    created_at_ms  INTEGER NOT NULL,
    PRIMARY KEY (uid, org_id, dept_id),
    FOREIGN KEY (uid) REFERENCES users(uid) ON DELETE CASCADE,
    FOREIGN KEY (org_id, dept_id) REFERENCES departments(org_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_departments_dept ON user_departments(org_id, dept_id);

-- 网关策略：按组织保存 PolicyDocument，version 每次写入自增。
CREATE TABLE IF NOT EXISTS gateway_configs (
    org_id         TEXT PRIMARY KEY,
    document_json  TEXT NOT NULL,
    version        TEXT NOT NULL,
    updated_at_ms  INTEGER NOT NULL
);

-- 工单层（缺陷 D-47，对标 DSH Super-agent 的六个实体）：把「一件被授权的工作」建成
-- 一等公民，而不是聊天流里靠人推断的一段话。六张表都挂在项目上，每次状态迁移都往
-- project_events 写一条 agent.* 事件，复用 D-06 已验证的 SSE 与游标语义，不新开一条流。
-- stage 用 CHECK 约束钉死：状态机由服务端保证，客户端拿不到"任意状态"这个口子。
CREATE TABLE IF NOT EXISTS agent_tickets (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    title          TEXT NOT NULL,
    detail         TEXT NOT NULL DEFAULT '',
    stage          TEXT NOT NULL DEFAULT 'input_undelivered'
                   CHECK (stage IN ('input_undelivered', 'queued', 'working', 'awaiting_answer',
                                    'awaiting_review', 'idle', 'blocked', 'closed')),
    intensity      TEXT NOT NULL DEFAULT 'explicit'
                   CHECK (intensity IN ('explicit', 'inferred')),
    created_by     TEXT NOT NULL,
    -- 来源留痕：这张工单是谁、从哪儿开出来的（人主动交代 / 候选转工单）。
    created_from   TEXT NOT NULL DEFAULT 'manual',
    version        INTEGER NOT NULL DEFAULT 1,
    closed_at_ms   INTEGER NOT NULL DEFAULT 0,
    closed_reason  TEXT NOT NULL DEFAULT '',
    created_at_ms  INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agent_tickets_project
    ON agent_tickets(project_id, updated_at_ms DESC);
-- 「等你的工单」是跨项目的：只按"需要人"的三种 stage 查，健康态不占版面。
CREATE INDEX IF NOT EXISTS idx_agent_tickets_stage
    ON agent_tickets(stage, updated_at_ms DESC);

-- 授权书（Mandate）不可变：要改范围就吊销旧的、插一条新 seq 的，绝不 UPDATE 正文。
-- 生效授权用部分唯一索引钉住"一张工单同时只有一份"，吊销后自然归零。
CREATE TABLE IF NOT EXISTS agent_mandates (
    id              TEXT PRIMARY KEY,
    ticket_id       TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    seq             INTEGER NOT NULL DEFAULT 1,
    brief           TEXT NOT NULL,
    scope           TEXT NOT NULL DEFAULT '',
    out_of_scope    TEXT NOT NULL DEFAULT '',
    intensity       TEXT NOT NULL DEFAULT 'explicit'
                    CHECK (intensity IN ('explicit', 'inferred')),
    -- 授权依据来自哪一层：D-46 的策略版本与判定行（可空——人主动交代的没有策略版本）
    source_kind     TEXT NOT NULL DEFAULT '',
    source_ref      TEXT NOT NULL DEFAULT '',
    policy_version  INTEGER NOT NULL DEFAULT 0,
    -- 正文摘要：续派前重校验用的就是它，改一个字都对不上
    digest          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    created_by      TEXT NOT NULL,
    created_at_ms   INTEGER NOT NULL,
    revoked_at_ms   INTEGER NOT NULL DEFAULT 0,
    revoked_by      TEXT NOT NULL DEFAULT '',
    revoke_reason   TEXT NOT NULL DEFAULT '',
    UNIQUE (ticket_id, seq),
    FOREIGN KEY (ticket_id) REFERENCES agent_tickets(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_mandates_active
    ON agent_mandates(ticket_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_agent_mandates_project ON agent_mandates(project_id, status);

-- 执行体绑定（Assignment）：一次"谁在哪台机器的哪个目录里跑这份授权"。
-- 工单级隔离落在 workspace_kind/workspace_ref：isolated 表示这一单有自己的目录，
-- 不与其它工单共享；recovery_note 是重启后带给新执行体的恢复注记；
-- read_steps_json 记「哪一步已经把会话消息读进历史」，避免重放已完成动作。
CREATE TABLE IF NOT EXISTS agent_assignments (
    id               TEXT PRIMARY KEY,
    ticket_id        TEXT NOT NULL,
    project_id       TEXT NOT NULL,
    mandate_id       TEXT NOT NULL,
    seq              INTEGER NOT NULL DEFAULT 1,
    worker_id        TEXT NOT NULL DEFAULT '',
    holder_uid       TEXT NOT NULL,
    workspace_kind   TEXT NOT NULL DEFAULT 'isolated'
                     CHECK (workspace_kind IN ('shared', 'isolated')),
    workspace_ref    TEXT NOT NULL DEFAULT '',
    checkpoint_ref   TEXT NOT NULL DEFAULT '',
    machine          TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running', 'finished', 'dropped', 'lost')),
    recovery_note    TEXT NOT NULL DEFAULT '',
    read_steps_json  TEXT NOT NULL DEFAULT '[]',
    started_at_ms    INTEGER NOT NULL,
    ended_at_ms      INTEGER NOT NULL DEFAULT 0,
    exit_reason      TEXT NOT NULL DEFAULT '',
    UNIQUE (ticket_id, seq),
    FOREIGN KEY (ticket_id) REFERENCES agent_tickets(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agent_assignments_ticket
    ON agent_assignments(ticket_id, seq DESC);

-- 结构化报告（Report）六态，与 DSH 同口径；状态迁移只由这里的 kind 决定，
-- 服务端不从 summary 自由文本里猜"是不是在等人"。handed_back 是"超出范围交回"，
-- 部分唯一索引保证同一工单只交回一次（交回之后由人决定改授权还是收口）。
CREATE TABLE IF NOT EXISTS agent_reports (
    id             TEXT PRIMARY KEY,
    ticket_id      TEXT NOT NULL,
    project_id     TEXT NOT NULL,
    assignment_id  TEXT NOT NULL DEFAULT '',
    kind           TEXT NOT NULL
                   CHECK (kind IN ('progress', 'waiting', 'ready', 'blocked', 'failed', 'dropped')),
    summary        TEXT NOT NULL,
    detail         TEXT NOT NULL DEFAULT '',
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    handed_back    INTEGER NOT NULL DEFAULT 0,
    context_seq    INTEGER NOT NULL DEFAULT 0,
    created_by     TEXT NOT NULL,
    created_at_ms  INTEGER NOT NULL,
    FOREIGN KEY (ticket_id) REFERENCES agent_tickets(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_reports_handback
    ON agent_reports(ticket_id) WHERE handed_back = 1;
CREATE INDEX IF NOT EXISTS idx_agent_reports_ticket
    ON agent_reports(ticket_id, created_at_ms, id);

-- 待人工（Attention）：结构化的一问一答，不是"从报告里读出来的情绪"。
-- 同一工单同时只留一个开放问题，避免"等人"这件事本身变成噪声。
CREATE TABLE IF NOT EXISTS agent_attentions (
    id             TEXT PRIMARY KEY,
    ticket_id      TEXT NOT NULL,
    project_id     TEXT NOT NULL,
    report_id      TEXT NOT NULL DEFAULT '',
    kind           TEXT NOT NULL CHECK (kind IN ('answer', 'review')),
    question       TEXT NOT NULL,
    reason         TEXT NOT NULL DEFAULT '',
    options_json   TEXT NOT NULL DEFAULT '[]',
    status         TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open', 'answered', 'withdrawn')),
    answer         TEXT NOT NULL DEFAULT '',
    answered_by    TEXT NOT NULL DEFAULT '',
    answered_at_ms INTEGER NOT NULL DEFAULT 0,
    created_at_ms  INTEGER NOT NULL,
    FOREIGN KEY (ticket_id) REFERENCES agent_tickets(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_attentions_open
    ON agent_attentions(ticket_id) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_agent_attentions_project
    ON agent_attentions(project_id, status);

-- 订阅（Subscription）：工单订一条会话/任务，后续讨论作为上下文扇出给在跑的执行体。
-- 这一条路绕过准入、不给新授权——它只补充视野；游标 last_fanned_seq 只前进。
CREATE TABLE IF NOT EXISTS agent_subscriptions (
    id                TEXT PRIMARY KEY,
    ticket_id         TEXT NOT NULL,
    project_id        TEXT NOT NULL,
    target_kind       TEXT NOT NULL DEFAULT 'thread'
                      CHECK (target_kind IN ('thread', 'task')),
    target_ref        TEXT NOT NULL,
    last_fanned_seq   INTEGER NOT NULL DEFAULT 0,
    created_by        TEXT NOT NULL,
    created_at_ms     INTEGER NOT NULL,
    closed_at_ms      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (ticket_id, target_kind, target_ref),
    FOREIGN KEY (ticket_id) REFERENCES agent_tickets(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agent_subscriptions_project
    ON agent_subscriptions(project_id, closed_at_ms);

-- 模型档位服务端供给（缺陷 D-44）：档位表按作用域一行一条当前版，历史只追加不覆盖，
-- 客户端「只接受递增版本」的判定靠 revision 单调；回滚是「把旧内容重新发布成新版本」。
-- 两张都是新表，旧卷升级时走 CREATE TABLE IF NOT EXISTS 整表新建，索引引用不到缺列，
-- 所以索引可以直接声明在这里——usage_rollup 那类「给旧表补列」的索引才必须挪到 init_db。
CREATE TABLE IF NOT EXISTS model_tier_tables (
    scope_kind     TEXT NOT NULL,
    scope_id       TEXT NOT NULL DEFAULT '',
    revision       INTEGER NOT NULL DEFAULT 1,
    document_json  TEXT NOT NULL,
    signature      TEXT NOT NULL DEFAULT '',
    signing_kid    TEXT NOT NULL DEFAULT '',
    digest         TEXT NOT NULL DEFAULT '',
    updated_at_ms  INTEGER NOT NULL,
    updated_by     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (scope_kind, scope_id)
);

CREATE TABLE IF NOT EXISTS model_tier_revisions (
    scope_kind     TEXT NOT NULL,
    scope_id       TEXT NOT NULL DEFAULT '',
    revision       INTEGER NOT NULL,
    document_json  TEXT NOT NULL,
    signature      TEXT NOT NULL DEFAULT '',
    signing_kid    TEXT NOT NULL DEFAULT '',
    digest         TEXT NOT NULL DEFAULT '',
    action         TEXT NOT NULL DEFAULT 'publish',
    actor          TEXT NOT NULL DEFAULT '',
    created_at_ms  INTEGER NOT NULL,
    PRIMARY KEY (scope_kind, scope_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_tier_revisions_scope
    ON model_tier_revisions(scope_kind, scope_id, revision DESC);

-- 审计签名锚点（缺陷 D-27）：哈希链能证明「行与行相连」，证明不了「整段链没被重算」。
-- 锚点把 (首行, 末行, 条目数, 链头) 用服务端私钥签掉存起来，只追加不修改；
-- archive_* 三列记录远端归档的真实状态，未配置远端时恒为 'local'，不谎报已归档。
-- 同样是新表 ⇒ 索引可以直接写在 SCHEMA 里（见上面 model_tier_tables 的说明）。
CREATE TABLE IF NOT EXISTS audit_anchors (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    at_ms          INTEGER NOT NULL,
    from_id        INTEGER NOT NULL,
    to_id          INTEGER NOT NULL,
    entries        INTEGER NOT NULL,
    head_hash      TEXT NOT NULL,
    signature      TEXT NOT NULL DEFAULT '',
    signing_kid    TEXT NOT NULL DEFAULT '',
    actor          TEXT NOT NULL DEFAULT '',
    archive_state  TEXT NOT NULL DEFAULT 'local'
                   CHECK (archive_state IN ('local', 'remote', 'failed', 'skipped')),
    archive_ref    TEXT NOT NULL DEFAULT '',
    archive_error  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_anchors_at ON audit_anchors(at_ms DESC);

-- 组织两级配额（缺陷 D-23）：组织总额在上、成员级在下（成员级 = 个人显式覆盖 > 本表 member_* 默认 > 角色缺省）。
-- 所有额度列 0 = 不限制（与引擎 `_quota_error` 同一口径），不是「零额度」。
-- `enforce` 缺省 0 ⇒ 旧卷升级后**不改变任何签发结果**，只多了一张空表；管理员显式打开才收敛声明、拒发令牌。
-- 整表新建 ⇒ 索引可以直接写在 SCHEMA 里（同 model_tier_tables / audit_anchors 的理由：不存在给旧表补列的时序问题）。
CREATE TABLE IF NOT EXISTS org_quotas (
    org_id                  TEXT PRIMARY KEY,
    monthly_tokens          INTEGER NOT NULL DEFAULT 0,
    concurrent_tasks        INTEGER NOT NULL DEFAULT 0,
    member_monthly_tokens   INTEGER NOT NULL DEFAULT 0,
    member_concurrent_tasks INTEGER NOT NULL DEFAULT 0,
    enforce                 INTEGER NOT NULL DEFAULT 0,
    updated_at_ms           INTEGER NOT NULL DEFAULT 0,
    updated_by              TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_org_quotas_enforce ON org_quotas(enforce);

-- 写侧幂等键（缺陷 D-19 / 卡 #154 刀 B）：客户端带 `Idempotency-Key` 的写请求，网络重试不再重复副作用。
-- 一张全新的表 ⇒ 索引同批建在 SCHEMA 里（同 org_quotas / audit_anchors 的理由：不存在给旧表补列的时序问题）。
-- `state` 只有 'in_progress' 与 'complete' 两值：前者是**租约**（`expires_at_ms` 很短，进程被杀也不会永久占键），
-- 后者是回执原文缓存（`response_json`），到期由 `idempotency.sweep()` 删除。
-- 旧卷升级后这张表是空的 ⇒ 行为逐字节等于升级前（不带键的请求走原路径，一律不落这里）。
CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope         TEXT NOT NULL,
    actor         TEXT NOT NULL,
    idem_key      TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    state         TEXT NOT NULL,
    status        INTEGER NOT NULL DEFAULT 0,
    response_json TEXT NOT NULL DEFAULT '',
    created_at_ms INTEGER NOT NULL DEFAULT 0,
    expires_at_ms INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, actor, idem_key)
);
CREATE INDEX IF NOT EXISTS idx_idempotency_expiry ON idempotency_keys(expires_at_ms);
"""

DEFAULT_DEPT_ID = "dept-default"
DEFAULT_DEPT_NAME = "默认部门"


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


def _backfill_departments(conn: sqlite3.Connection) -> None:
    """存量组织用户回填默认部门；个人账号（org_id 为空）不建租户。

    网关换票只服务组织用户，个人版直连厂商，因此个人账号没有部门是预期状态。
    """
    stamp = now_ms()
    rows = conn.execute(
        "SELECT uid, org_id FROM users WHERE org_id IS NOT NULL AND org_id != ''"
    ).fetchall()
    for uid, org_id in rows:
        conn.execute(
            "INSERT OR IGNORE INTO orgs (id, name, created_at_ms) VALUES (?1, ?1, ?2)",
            (org_id, stamp),
        )
        conn.execute(
            "INSERT OR IGNORE INTO departments (org_id, id, name, created_at_ms) "
            "VALUES (?1, ?2, ?3, ?4)",
            (org_id, DEFAULT_DEPT_ID, DEFAULT_DEPT_NAME, stamp),
        )
        has_dept = conn.execute(
            "SELECT 1 FROM user_departments WHERE uid = ?1 AND org_id = ?2 LIMIT 1",
            (uid, org_id),
        ).fetchone()
        if has_dept is None:
            conn.execute(
                "INSERT OR IGNORE INTO user_departments "
                "(uid, org_id, dept_id, is_primary, created_at_ms) "
                "VALUES (?1, ?2, ?3, 1, ?4)",
                (uid, org_id, DEFAULT_DEPT_ID, stamp),
            )


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
                # 增量补列：组织归属与配额覆盖；存量用户保持个人空间与默认配额
                user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
                for name, ddl in (
                    ("org_id", "TEXT NOT NULL DEFAULT ''"),
                    ("quota_json", "TEXT NOT NULL DEFAULT '{}'"),
                ):
                    if name not in user_columns:
                        conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_users_org ON users(org_id)")
                # 增量补列：版本撤销；已有版本默认未撤销
                version_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(item_versions)")
                }
                for name, ddl in (
                    ("revoked_at_ms", "INTEGER NOT NULL DEFAULT 0"),
                    ("revoked_reason", "TEXT NOT NULL DEFAULT ''"),
                    ("revoked_by", "TEXT NOT NULL DEFAULT ''"),
                ):
                    if name not in version_columns:
                        conn.execute(f"ALTER TABLE item_versions ADD COLUMN {name} {ddl}")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_versions_revoked "
                    "ON item_versions(revoked_at_ms DESC)"
                )
                # 增量补列：用量行带部门归属（网关行由事件提供，客户端行按上报人的主部门）
                rollup_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(usage_rollup)")
                }
                if "dept_id" not in rollup_columns:
                    conn.execute(
                        "ALTER TABLE usage_rollup ADD COLUMN dept_id TEXT NOT NULL DEFAULT ''"
                    )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_usage_rollup_org_dept "
                    "ON usage_rollup(org_id, dept_id, day)"
                )
                _backfill_departments(conn)
                # 增量补列 + 回填：审计哈希链（旧行按当前内容重建哈希）
                audit_columns = {
                    row["name"] for row in conn.execute("PRAGMA table_info(admin_audit)")
                }
                for name, ddl in (
                    ("actor_role", "TEXT NOT NULL DEFAULT 'admin'"),
                    ("before_json", "TEXT NOT NULL DEFAULT '{}'"),
                    ("after_json", "TEXT NOT NULL DEFAULT '{}'"),
                    ("prev_hash", "TEXT NOT NULL DEFAULT ''"),
                    ("entry_hash", "TEXT NOT NULL DEFAULT ''"),
                ):
                    if name not in audit_columns:
                        conn.execute(f"ALTER TABLE admin_audit ADD COLUMN {name} {ddl}")
                from .audit_chain import backfill as _audit_backfill

                _audit_backfill(conn)
        finally:
            conn.close()
