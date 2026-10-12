//! 版本化迁移（schema_version 表驱动，幂等）

/// 全部迁移脚本（索引即版本号的声明顺序）
const MIGRATIONS: &[&str] = &[
    // ── V1：核心闭环首批表（model_configs / tasks / settings_kv） ──
    r#"
    CREATE TABLE IF NOT EXISTS model_configs (
        id             TEXT PRIMARY KEY,
        name           TEXT NOT NULL,
        source         TEXT NOT NULL,           -- openai_compat / ollama / preset_* / custom
        base_url       TEXT NOT NULL DEFAULT '',
        model_name     TEXT NOT NULL,
        context_window INTEGER NOT NULL DEFAULT 4096,
        is_default     INTEGER NOT NULL DEFAULT 0,
        agent_binding  TEXT NOT NULL DEFAULT '',
        force_local    INTEGER NOT NULL DEFAULT 0,
        has_api_key    INTEGER NOT NULL DEFAULT 0, -- 密钥本体存 keyring，此处仅引用标记
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS tasks (
        task_id            TEXT PRIMARY KEY,
        workspace_id       TEXT NOT NULL DEFAULT 'default',
        title              TEXT NOT NULL DEFAULT '',
        status             TEXT NOT NULL,
        wait_reason        TEXT,
        retry_count        INTEGER NOT NULL DEFAULT 0,
        status_reason_code TEXT,
        queue_position     INTEGER NOT NULL DEFAULT 0,
        created_at_ms      INTEGER NOT NULL,
        updated_at_ms      INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks (updated_at_ms DESC);

    CREATE TABLE IF NOT EXISTS settings_kv (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    "#,
    // ── V2：安全与治理（PRD §4.7 全链路审计 + 出域控制/沙箱策略持久化） ──
    // 增量变更：仅新增表与索引，不改动 V1 存量表结构，历史数据不受影响。
    r#"
    CREATE TABLE IF NOT EXISTS audit_logs (
        id        TEXT PRIMARY KEY,
        ts_ms     INTEGER NOT NULL,
        action    TEXT NOT NULL,                    -- model_call / file_write / route_switch / config_change …
        severity  TEXT NOT NULL DEFAULT 'info',      -- error / warning / info / debug
        user_name TEXT,                              -- 登录态昵称；未登录为 NULL
        resource  TEXT,                              -- 涉及对象（模型名/文件路径），入库前脱敏
        details   TEXT,                              -- 诊断详情，入库前脱敏
        is_read   INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs (ts_ms DESC);
    CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs (action);

    CREATE TABLE IF NOT EXISTS security_policies (
        key           TEXT PRIMARY KEY,              -- domain_control / sandbox:{workspaceId}
        json          TEXT NOT NULL,
        updated_at_ms INTEGER NOT NULL
    );
    "#,
    // ── V3：模型用量台账（引擎上报的真实 token，按「模型 × UTC 日」累加） ──
    // 增量变更：仅新增表与索引，不改动 V1/V2 存量表结构，历史数据不受影响。
    r#"
    CREATE TABLE IF NOT EXISTS model_usage (
        model_name        TEXT NOT NULL,              -- 引擎实际上报的模型名
        day               TEXT NOT NULL,              -- UTC yyyy-mm-dd（跨日自然分桶）
        prompt_tokens     INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        calls             INTEGER NOT NULL DEFAULT 0,
        last_used_at_ms   INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (model_name, day)
    );
    CREATE INDEX IF NOT EXISTS idx_model_usage_day ON model_usage (day);
    "#,
    // ── V4：定时任务（schedule_* 命令：任务定义 + 执行记录） ──
    // 增量变更：仅新增表与索引，不改动 V1/V2/V3 存量表结构，历史数据不受影响。
    r#"
    CREATE TABLE IF NOT EXISTS scheduled_tasks (
        schedule_id      TEXT PRIMARY KEY,
        name             TEXT NOT NULL,
        description      TEXT NOT NULL DEFAULT '',
        freq             TEXT NOT NULL,                  -- hourly / daily / weekly / monthly
        time_of_day      TEXT NOT NULL,                  -- HH:MM（24 小时制，UTC 口径与用量台账一致）
        weekday          INTEGER,                        -- 1=周一 … 7=周日（仅 weekly 使用）
        enabled          INTEGER NOT NULL DEFAULT 1,
        workspace_id     TEXT NOT NULL DEFAULT 'default',
        created_at_ms    INTEGER NOT NULL,
        updated_at_ms    INTEGER NOT NULL,
        next_run_at_ms   INTEGER,                        -- 下次计划执行时间（禁用时为 NULL）
        last_run_at_ms   INTEGER,
        last_status      TEXT,
        last_task_id     TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_next ON scheduled_tasks (next_run_at_ms);

    CREATE TABLE IF NOT EXISTS schedule_runs (
        run_id         TEXT PRIMARY KEY,
        schedule_id    TEXT NOT NULL,
        task_id        TEXT,
        started_at_ms  INTEGER NOT NULL,
        finished_at_ms INTEGER,
        status         TEXT NOT NULL,                    -- running / succeeded / failed / blocked
        summary        TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_schedule_runs_schedule ON schedule_runs (schedule_id, started_at_ms DESC);
    "#,
    // ── V5：对齐整改通道（Hooks / 连接器授权 / 任务归档） ──
    // 增量变更：仅新增表与索引，不改动 V1~V4 存量表结构，历史数据不受影响。
    r#"
    CREATE TABLE IF NOT EXISTS hook_configs (
        id            TEXT PRIMARY KEY,                   -- 固定 'global'：与渲染层 HookConfigStore 一对一
        hooks_json    TEXT NOT NULL,                      -- Partial<Record<HookEventName, HookMatcherGroup[]>>
        updated_at_ms INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS connector_states (
        connector_id   TEXT PRIMARY KEY,
        category       TEXT NOT NULL DEFAULT 'builtin',   -- builtin / market
        installed      INTEGER NOT NULL DEFAULT 0,
        auth_status    TEXT NOT NULL DEFAULT 'none',      -- none / pending / authorized
        server_ref     TEXT,
        updated_at_ms  INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS archived_tasks (
        task_id         TEXT PRIMARY KEY,
        workspace_id    TEXT NOT NULL DEFAULT 'default',
        archived_at_ms  INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_archived_tasks_at ON archived_tasks (archived_at_ms DESC);
    "#,
    // ── V6：MCP 连接器注册表（mcp_* 通道落地） ──
    // 增量变更：仅新增表，V1~V5 存量表与数据不受影响。
    // env_json / headers_json 只在 Rust 侧使用（注入子进程或请求头），序列化给渲染层时只给键名。
    r#"
    CREATE TABLE IF NOT EXISTS mcp_servers (
        server_id     TEXT PRIMARY KEY,
        name          TEXT NOT NULL UNIQUE,
        transport     TEXT NOT NULL,                           -- stdio / streamable_http / sse
        command       TEXT NOT NULL DEFAULT '',                -- stdio 启动可执行文件
        args_json     TEXT NOT NULL DEFAULT '[]',
        url           TEXT NOT NULL DEFAULT '',
        env_json      TEXT NOT NULL DEFAULT '{}',
        headers_json  TEXT NOT NULL DEFAULT '{}',
        timeout_secs  INTEGER NOT NULL DEFAULT 60,
        status        TEXT NOT NULL DEFAULT 'offline',         -- offline / connected / failed
        last_error    TEXT,
        tools_json    TEXT NOT NULL DEFAULT '[]',              -- 上次成功握手的工具清单缓存
        probed_at_ms  INTEGER,
        created_at_ms INTEGER NOT NULL,
        updated_at_ms INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_mcp_servers_name ON mcp_servers (name);
    "#,
    // ── V7：会话正文持久化（chat_messages） ──
    // 增量变更：仅新增表与索引，V1~V6 存量表与数据不受影响。
    // 事件流（event_store JSONL）只保留 24h，会话正文需要跨重启留存，故独立建表：
    // 一行 = 一轮对话的一个角色发言，主键 (conv_id, turn_index, role) 使「重新生成」
    // 天然覆盖本轮回复（与界面行为一致），不会累积同一轮的多个版本。
    r#"
    CREATE TABLE IF NOT EXISTS chat_messages (
        conv_id        TEXT NOT NULL,
        turn_index     INTEGER NOT NULL,
        role           TEXT NOT NULL,                        -- user / assistant
        task_id        TEXT NOT NULL DEFAULT '',             -- 本轮对应的任务（事件流按它取过程明细）
        content        TEXT NOT NULL,
        artifacts_json TEXT NOT NULL DEFAULT '[]',           -- 交付物文件名清单（assistant 行使用）
        ts_ms          INTEGER NOT NULL,
        PRIMARY KEY (conv_id, turn_index, role)
    );
    CREATE INDEX IF NOT EXISTS idx_chat_messages_conv ON chat_messages (conv_id, turn_index);
    CREATE INDEX IF NOT EXISTS idx_chat_messages_task ON chat_messages (task_id);
    "#,
    // ── V8：回复点赞/点踩持久化（chat_feedback） ──
    // 增量变更：仅新增表与索引，V1~V7 存量表与数据不受影响。
    // 反馈挂在「会话的一轮回复」上而不是消息行本身，因此单独建表、按 (conv_id, turn_index) 主键覆盖；
    // 取消评价 = 删除这一条评价记录（不动正文行），界面与库内口径一致。
    r#"
    CREATE TABLE IF NOT EXISTS chat_feedback (
        conv_id    TEXT NOT NULL,
        turn_index INTEGER NOT NULL,
        value      TEXT NOT NULL,                             -- up / down
        ts_ms      INTEGER NOT NULL,
        PRIMARY KEY (conv_id, turn_index)
    );
    CREATE INDEX IF NOT EXISTS idx_chat_feedback_conv ON chat_feedback (conv_id, turn_index);
    "#,
];

/// 执行所有尚未应用的迁移
pub fn run(conn: &rusqlite::Connection) -> Result<(), String> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        );",
    )
    .map_err(|e| format!("STORE_MIGRATE_FAILED: {e}"))?;

    let current: i64 = conn
        .query_row("SELECT COALESCE(MAX(version), 0) FROM schema_version", [], |r| {
            r.get(0)
        })
        .map_err(|e| format!("STORE_MIGRATE_FAILED: {e}"))?;

    for (i, sql) in MIGRATIONS.iter().enumerate() {
        let version = (i + 1) as i64;
        if version <= current {
            continue; // 已应用，幂等跳过
        }
        conn.execute_batch(sql)
            .map_err(|e| format!("STORE_MIGRATE_V{version}_FAILED: {e}"))?;
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?1, datetime('now'))",
            [version],
        )
        .map_err(|e| format!("STORE_MIGRATE_RECORD_FAILED: {e}"))?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mem_conn() -> rusqlite::Connection {
        rusqlite::Connection::open_in_memory().unwrap()
    }

    #[test]
    fn test_run_creates_tables() {
        let conn = mem_conn();
        run(&conn).unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN \
                 ('model_configs','tasks','settings_kv','schema_version','audit_logs',\
                 'security_policies','model_usage','scheduled_tasks','schedule_runs',\
                 'hook_configs','connector_states','archived_tasks','mcp_servers','chat_messages',\
                 'chat_feedback')",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 15);
    }

    /// 增量升级：老库（仅 V1）升级到 V2 后，存量数据必须完好（开发规范：禁止覆盖存量数据）
    #[test]
    fn test_v2_upgrade_preserves_existing_rows() {
        let conn = mem_conn();
        // 模拟只跑过 V1 的历史库
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);",
        )
        .unwrap();
        conn.execute_batch(MIGRATIONS[0]).unwrap();
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (1, datetime('now'))",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO model_configs (id, name, source, model_name, created_at, updated_at) \
             VALUES ('m-legacy', '存量模型', 'openai_compat', 'qwen-max', '2026-01-01', '2026-01-01')",
            [],
        )
        .unwrap();

        // 升级：只新增表/索引，不清表不重建
        run(&conn).unwrap();

        let name: String = conn
            .query_row("SELECT name FROM model_configs WHERE id='m-legacy'", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(name, "存量模型");
        let max_v: i64 = conn
            .query_row("SELECT MAX(version) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(max_v, MIGRATIONS.len() as i64);
        let audit_rows: i64 = conn
            .query_row("SELECT COUNT(*) FROM audit_logs", [], |r| r.get(0))
            .unwrap();
        assert_eq!(audit_rows, 0);
    }

    /// 增量升级：V2 老库升级到 V3 后，存量模型/审计数据必须完好，且用量表为空
    #[test]
    fn test_v3_upgrade_preserves_existing_rows() {
        let conn = mem_conn();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);",
        )
        .unwrap();
        // 模拟只跑过 V1+V2 的历史库
        conn.execute_batch(MIGRATIONS[0]).unwrap();
        conn.execute_batch(MIGRATIONS[1]).unwrap();
        conn.execute_batch(
            "INSERT INTO schema_version (version, applied_at) VALUES (1, datetime('now')), (2, datetime('now'));",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO model_configs (id, name, source, model_name, created_at, updated_at) \
             VALUES ('m-v2', 'V2 存量模型', 'openai_compat', 'qwen-plus', '2026-02-01', '2026-02-01')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO audit_logs (id, ts_ms, action, severity, is_read) \
             VALUES ('a-v2', 1000, 'model_call', 'info', 0)",
            [],
        )
        .unwrap();

        run(&conn).unwrap();

        let name: String = conn
            .query_row("SELECT name FROM model_configs WHERE id='m-v2'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(name, "V2 存量模型");
        let audit_rows: i64 = conn
            .query_row("SELECT COUNT(*) FROM audit_logs", [], |r| r.get(0))
            .unwrap();
        assert_eq!(audit_rows, 1, "存量审计记录不得被清理");
        let usage_rows: i64 = conn
            .query_row("SELECT COUNT(*) FROM model_usage", [], |r| r.get(0))
            .unwrap();
        assert_eq!(usage_rows, 0);
        let max_v: i64 = conn
            .query_row("SELECT MAX(version) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(max_v, MIGRATIONS.len() as i64);
    }

    /// 增量升级：V3 老库升级到 V4 后，存量用量/审计数据必须完好，且定时任务表为空
    #[test]
    fn test_v4_upgrade_preserves_existing_rows() {
        let conn = mem_conn();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);",
        )
        .unwrap();
        // 模拟只跑过 V1+V2+V3 的历史库
        for i in 0..3 {
            conn.execute_batch(MIGRATIONS[i]).unwrap();
        }
        conn.execute_batch(
            "INSERT INTO schema_version (version, applied_at) VALUES (1, datetime('now')), (2, datetime('now')), (3, datetime('now'));",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO model_usage (model_name, day, prompt_tokens, completion_tokens, calls, last_used_at_ms) \
             VALUES ('qwen-plus', '2026-03-01', 11, 22, 3, 1000)",
            [],
        )
        .unwrap();

        run(&conn).unwrap();

        let tokens: i64 = conn
            .query_row(
                "SELECT prompt_tokens FROM model_usage WHERE model_name='qwen-plus' AND day='2026-03-01'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(tokens, 11, "存量用量台账不得被清理");
        let schedules: i64 = conn
            .query_row("SELECT COUNT(*) FROM scheduled_tasks", [], |r| r.get(0))
            .unwrap();
        assert_eq!(schedules, 0);
        let runs: i64 = conn
            .query_row("SELECT COUNT(*) FROM schedule_runs", [], |r| r.get(0))
            .unwrap();
        assert_eq!(runs, 0);
        let max_v: i64 = conn
            .query_row("SELECT MAX(version) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(max_v, MIGRATIONS.len() as i64);
    }

    /// 增量升级：V4 老库升级到 V5 后，存量定时任务/用量数据必须完好，且新表为空
    #[test]
    fn test_v5_upgrade_preserves_existing_rows() {
        let conn = mem_conn();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);",
        )
        .unwrap();
        // 模拟只跑过 V1~V4 的历史库
        for i in 0..4 {
            conn.execute_batch(MIGRATIONS[i]).unwrap();
        }
        conn.execute_batch(
            "INSERT INTO schema_version (version, applied_at) \
             VALUES (1, datetime('now')), (2, datetime('now')), (3, datetime('now')), (4, datetime('now'));",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO scheduled_tasks (schedule_id, name, freq, time_of_day, enabled, created_at_ms, updated_at_ms) \
             VALUES ('sched-legacy', '存量定时任务', 'daily', '08:00', 1, 1000, 1000)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO model_usage (model_name, day, prompt_tokens, completion_tokens, calls, last_used_at_ms) \
             VALUES ('qwen-plus', '2026-04-01', 44, 55, 6, 1000)",
            [],
        )
        .unwrap();

        run(&conn).unwrap();

        let name: String = conn
            .query_row(
                "SELECT name FROM scheduled_tasks WHERE schedule_id='sched-legacy'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(name, "存量定时任务", "存量定时任务不得被清理");
        let tokens: i64 = conn
            .query_row(
                "SELECT completion_tokens FROM model_usage WHERE model_name='qwen-plus' AND day='2026-04-01'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(tokens, 55, "存量用量台账不得被清理");
        for t in ["hook_configs", "connector_states", "archived_tasks"] {
            let rows: i64 = conn
                .query_row(&format!("SELECT COUNT(*) FROM {t}"), [], |r| r.get(0))
                .unwrap();
            assert_eq!(rows, 0, "新表 {t} 初始应为空");
        }
        let max_v: i64 = conn
            .query_row("SELECT MAX(version) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(max_v, MIGRATIONS.len() as i64);
    }

    /// 增量升级：V6 老库升级到 V7 后，存量连接器/任务数据必须完好，且会话表为空
    #[test]
    fn test_v7_upgrade_preserves_existing_rows() {
        let conn = mem_conn();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);",
        )
        .unwrap();
        // 模拟只跑过 V1~V6 的历史库
        for i in 0..6 {
            conn.execute_batch(MIGRATIONS[i]).unwrap();
        }
        conn.execute_batch(
            "INSERT INTO schema_version (version, applied_at) \
             VALUES (1, datetime('now')), (2, datetime('now')), (3, datetime('now')), \
                     (4, datetime('now')), (5, datetime('now')), (6, datetime('now'));",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO mcp_servers (server_id, name, transport, created_at_ms, updated_at_ms) \
             VALUES ('srv-legacy', '存量连接器', 'stdio', 1000, 1000)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO tasks (task_id, workspace_id, title, status, created_at_ms, updated_at_ms) \
             VALUES ('task-legacy', 'default', '存量任务', 'done', 1000, 1000)",
            [],
        )
        .unwrap();

        run(&conn).unwrap();

        let name: String = conn
            .query_row("SELECT name FROM mcp_servers WHERE server_id='srv-legacy'", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(name, "存量连接器", "存量连接器不得被清理");
        let title: String = conn
            .query_row("SELECT title FROM tasks WHERE task_id='task-legacy'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(title, "存量任务", "存量任务本体不得被清理");
        let msgs: i64 = conn
            .query_row("SELECT COUNT(*) FROM chat_messages", [], |r| r.get(0))
            .unwrap();
        assert_eq!(msgs, 0, "新表 chat_messages 初始应为空");
        let max_v: i64 = conn
            .query_row("SELECT MAX(version) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(max_v, MIGRATIONS.len() as i64);
    }

    /// 增量升级：V7 老库升级到 V8 后，存量会话正文必须完好，且反馈表为空
    #[test]
    fn test_v8_upgrade_preserves_existing_messages() {
        let conn = mem_conn();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);",
        )
        .unwrap();
        // 模拟只跑过 V1~V7 的历史库
        for i in 0..7 {
            conn.execute_batch(MIGRATIONS[i]).unwrap();
        }
        conn.execute_batch(
            "INSERT INTO schema_version (version, applied_at) \
             VALUES (1, datetime('now')), (2, datetime('now')), (3, datetime('now')), \
                     (4, datetime('now')), (5, datetime('now')), (6, datetime('now')), \
                     (7, datetime('now'));",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO chat_messages (conv_id, turn_index, role, task_id, content, ts_ms) \
             VALUES ('conv-legacy', 0, 'user', 'task-legacy', '存量提问', 1000)",
            [],
        )
        .unwrap();

        run(&conn).unwrap();

        let content: String = conn
            .query_row(
                "SELECT content FROM chat_messages WHERE conv_id='conv-legacy'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(content, "存量提问", "存量会话正文不得被清理");
        let feedback: i64 = conn
            .query_row("SELECT COUNT(*) FROM chat_feedback", [], |r| r.get(0))
            .unwrap();
        assert_eq!(feedback, 0, "新表 chat_feedback 初始应为空（不伪造评价）");
        let max_v: i64 = conn
            .query_row("SELECT MAX(version) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(max_v, MIGRATIONS.len() as i64);
    }

    #[test]
    fn test_run_idempotent() {        let conn = mem_conn();
        run(&conn).unwrap();
        run(&conn).unwrap(); // 二次执行不报错、不重复插入版本
        let versions: i64 = conn
            .query_row("SELECT COUNT(*) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(versions, MIGRATIONS.len() as i64);
    }

    #[test]
    fn test_incremental_migration_skips_applied() {
        let conn = mem_conn();
        run(&conn).unwrap();
        let max_v: i64 = conn
            .query_row("SELECT MAX(version) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(max_v, MIGRATIONS.len() as i64);
    }
}
