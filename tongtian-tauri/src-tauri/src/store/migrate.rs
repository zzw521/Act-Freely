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
    // ── V9：Hook 执行面放行台账（hook_grants） ──
    // 增量变更：仅新增表，hook_configs（V5）与其存量配置不动。
    // 配置里写了命令 ≠ 允许在真实任务里拉起进程：每条命令首次执行前需在 Hooks 页明确放行，
    // 放行按命令原文（去首尾空白）记录，撤销放行只删这一条授权、不删配置。
    r#"
    CREATE TABLE IF NOT EXISTS hook_grants (
        command       TEXT PRIMARY KEY,                       -- 与 hook_configs 里同一条命令原文一致
        event         TEXT NOT NULL,                          -- 放行时所属事件（仅用于界面回显）
        granted_at_ms INTEGER NOT NULL,
        run_count     INTEGER NOT NULL DEFAULT 0,             -- 真实任务里实际执行过多少次
        last_run_at_ms INTEGER
    );
    "#,
    // ── V10：远程 MCP 连接器 OAuth 配置（令牌仍只存系统凭据库） ──
    // 增量变更：仅给 mcp_servers 追加一列，V1~V9 表与存量行不受影响。
    // auth_json 只存授权地址 / token 地址 / client_id / scopes，不存令牌或 client secret。
    r#"
    ALTER TABLE mcp_servers ADD COLUMN auth_json TEXT NOT NULL DEFAULT '{}';
    "#,
    // ── V11：定时任务执行完成推送 IM（只加列，存量任务默认不推送） ──
    // 增量变更：notify_channel 为空的存量任务行为完全不变；推送目标只存会话标识，
    // 凭据仍只在引擎侧配置与系统凭据库中。
    r#"
    ALTER TABLE scheduled_tasks ADD COLUMN notify_channel TEXT NOT NULL DEFAULT '';
    ALTER TABLE scheduled_tasks ADD COLUMN notify_target TEXT NOT NULL DEFAULT '';
    "#,
    // ── V12：模型计费单价、故障转移链、调用延迟与成本台账（P1 成本/延迟 + 路由） ──
    // 增量变更：model_configs/model_usage 只加列，存量行保持原值（单价 NULL=未配置，
    // 延迟 0 样本=旧数据无耗时事实）；成本单独建表，按币种分桶，避免混币种相加。
    r#"
    ALTER TABLE model_configs ADD COLUMN price_input_per_m REAL;             -- 每 100 万输入 token 单价；NULL=未配置
    ALTER TABLE model_configs ADD COLUMN price_output_per_m REAL;            -- 每 100 万输出 token 单价；NULL=未配置
    ALTER TABLE model_configs ADD COLUMN price_currency TEXT NOT NULL DEFAULT 'CNY';
    ALTER TABLE model_configs ADD COLUMN fallback_ids TEXT NOT NULL DEFAULT '[]'; -- 有序模型 id 数组（JSON）

    ALTER TABLE model_usage ADD COLUMN latency_ms_total INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE model_usage ADD COLUMN latency_samples INTEGER NOT NULL DEFAULT 0;

    CREATE TABLE IF NOT EXISTS model_usage_cost (
        model_name   TEXT NOT NULL,
        day          TEXT NOT NULL,                    -- UTC yyyy-mm-dd
        currency     TEXT NOT NULL,                    -- CNY / USD …（按调用时配置的币种）
        cost_micros  INTEGER NOT NULL DEFAULT 0,       -- 1 货币单位 = 1,000,000 micros
        priced_calls INTEGER NOT NULL DEFAULT 0,       -- 计入成本的调用次数（与未计价调用分开）
        PRIMARY KEY (model_name, day, currency)
    );
    CREATE INDEX IF NOT EXISTS idx_model_usage_cost_day ON model_usage_cost (day);
    "#,
    // ── V13：会话归属工作区（侧边栏按项目看） ──
    // 增量变更：只给 chat_messages 追加一列 + 索引，V1~V12 的存量行统一落到 default，
    // 与 tasks.workspace_id 的缺省口径一致；不重建表、不改主键。
    r#"
    ALTER TABLE chat_messages ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'default';
    CREATE INDEX IF NOT EXISTS idx_chat_messages_workspace ON chat_messages (workspace_id, ts_ms DESC);
    "#,
    // ── V14：跨任务长期记忆（D-35 第一阶段：结构化存储 + 去重 + 按项目检索） ──
    // 增量变更：只新增表与索引。content_hash 与归属组成唯一键，重复任务合并为一行
    // 并累加 occurrences，而不是继续往 MEMORY.md 里追加重复行。
    // 来源（task_id / conv_id）留在行内，记忆可追溯到具体任务；删除只删这一行。
    r#"
    CREATE TABLE IF NOT EXISTS memories (
        id            TEXT PRIMARY KEY,
        scope         TEXT NOT NULL DEFAULT 'global',   -- global / workspace
        workspace_id  TEXT NOT NULL DEFAULT '',         -- scope=workspace 时为工作区 id；global 为空串
        kind          TEXT NOT NULL DEFAULT 'fact',     -- task / fact / preference / decision
        content       TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        source        TEXT NOT NULL DEFAULT 'manual',   -- auto / manual / reflection
        conv_id       TEXT,
        task_id       TEXT,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        last_used_at  TEXT,
        use_count     INTEGER NOT NULL DEFAULT 0,
        occurrences   INTEGER NOT NULL DEFAULT 1,
        UNIQUE (scope, workspace_id, content_hash)
    );
    CREATE INDEX IF NOT EXISTS idx_memories_scope_time ON memories (scope, workspace_id, updated_at DESC);
    "#,
    // ── V15：记忆复核、冲突软退休与结构化事实（D-35 第二阶段） ──
    // 增量变更：只给 memories 追加列与索引，V14 存量行默认全部 active，
    // 原有「写入即生效」行为不变；status=pending 只会在用户打开「写入前待审」后出现。
    // subject/predicate/object 是 AtomCard 等价物的最小字段：事实冲突时旧行不物理删除，
    // 只置为 archived 并记 superseded_by，证据链仍可追溯。
    r#"
    ALTER TABLE memories ADD COLUMN status TEXT NOT NULL DEFAULT 'active';   -- active / pending / archived
    ALTER TABLE memories ADD COLUMN reviewed_at TEXT;
    ALTER TABLE memories ADD COLUMN superseded_by TEXT;
    ALTER TABLE memories ADD COLUMN subject TEXT NOT NULL DEFAULT '';
    ALTER TABLE memories ADD COLUMN predicate TEXT NOT NULL DEFAULT '';
    ALTER TABLE memories ADD COLUMN object TEXT NOT NULL DEFAULT '';
    ALTER TABLE memories ADD COLUMN confidence REAL NOT NULL DEFAULT 1;
    CREATE INDEX IF NOT EXISTS idx_memories_status ON memories (status, scope, workspace_id, updated_at DESC);
    "#,
    // ── V16：常驻事件入口与授权层（D-46 第一段：账本 + 幂等游标 + 版本化策略 + 判定留痕） ──
    // 增量变更：只新增四张 sa_ 前缀表，不动任何存量表；本段只做「记账与判定」，
    // 不产生任何自动执行，因此这里没有工单表、没有副作用队列（那是 D-47 / D-48 的位置）。
    //
    // 语义对齐 dsh-superagent-prototype 的四条纪律（证据见 docs/Super-agent-对标DSH.md 附录 D）：
    // - sa_sources：每个事件入口实例自己持读取位置（cursor 只前进），enabled 默认 0，
    //   装好不等于开跑；重启后从库里的游标续读，因此历史事件不会被重放成新工作。
    // - sa_triggers：事件是不可变事实，UNIQUE (source_id, external_id) 是幂等单位，
    //   同一条事件重复投递（断线补拉重叠、重连重放）只落一行。
    //   正文按上限截断存本地，不写日志；来源提供的 actor / kind / 是否点名是判定输入，
    //   正文只参与规则匹配，不当指令。
    // - sa_policy：策略是一等公民且版本化，一次变更一行，digest 用于「内容没变不新增版本」；
    //   accept_mode 三档 off / observe / auto，默认 observe（研判照做、零自动执行）。
    // - sa_decisions：判定与研判留痕，UNIQUE (trigger_id, policy_version) 保证
    //   同一策略下不重复判定；策略变更后允许按新版本重判，旧判定不删、可追溯。
    r#"
    CREATE TABLE IF NOT EXISTS sa_sources (
        id             TEXT PRIMARY KEY,
        kind           TEXT NOT NULL,                        -- project_event / schedule / condition / workspace_file / im_inbound
        instance       TEXT NOT NULL DEFAULT '',             -- 同一 kind 下的具体实例（项目 id / 目录 / 会话）
        label          TEXT NOT NULL DEFAULT '',
        enabled        INTEGER NOT NULL DEFAULT 0,           -- 默认关闭：登记不等于开跑
        cursor         INTEGER NOT NULL DEFAULT 0,           -- 读取位置，只前进
        config         TEXT NOT NULL DEFAULT '{}',           -- 监听范围等实例级配置（不含任何凭据）
        last_error     TEXT,
        created_at_ms  INTEGER NOT NULL,
        updated_at_ms  INTEGER NOT NULL,
        UNIQUE (kind, instance)
    );
    CREATE INDEX IF NOT EXISTS idx_sa_sources_enabled ON sa_sources (enabled, kind);
    CREATE TABLE IF NOT EXISTS sa_triggers (
        id              TEXT PRIMARY KEY,
        source_id       TEXT NOT NULL,
        external_id     TEXT NOT NULL,                       -- 上游给的事件 id（项目事件用 seq）
        kind            TEXT NOT NULL DEFAULT '',            -- 上游事件类型，如 task.commented
        text            TEXT NOT NULL,
        actor           TEXT NOT NULL DEFAULT '',            -- 来源声明的发起人（可信字段）
        thread_key      TEXT NOT NULL DEFAULT '',            -- 同一会话/线程键，研判串行化用
        addressed_agent INTEGER NOT NULL DEFAULT 0,          -- 来源事实：是否点名/回复了本应用
        source_seq      INTEGER NOT NULL DEFAULT 0,
        arrived_at_ms   INTEGER NOT NULL,
        payload         TEXT NOT NULL DEFAULT '{}',          -- 原始结构（截断后），供研判与界面回看
        UNIQUE (source_id, external_id)
    );
    CREATE INDEX IF NOT EXISTS idx_sa_triggers_arrived ON sa_triggers (arrived_at_ms DESC);
    CREATE INDEX IF NOT EXISTS idx_sa_triggers_thread ON sa_triggers (thread_key, arrived_at_ms DESC);
    CREATE TABLE IF NOT EXISTS sa_policy (
        version        INTEGER PRIMARY KEY,
        doc            TEXT NOT NULL,                        -- JSON：accept_mode + 每入口许可名单 + 规则
        digest         TEXT NOT NULL,                        -- doc 规范化后的 sha256，变更判定用
        source         TEXT NOT NULL DEFAULT 'default',      -- default / user / import
        created_at_ms  INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sa_decisions (
        id              TEXT PRIMARY KEY,
        trigger_id      TEXT NOT NULL,
        policy_version  INTEGER NOT NULL,
        strength        TEXT NOT NULL,                       -- explicit / inferred / not_admitted
        outcome         TEXT NOT NULL,                       -- admitted / would_accept / not_admitted / mode_off
        would_accept    INTEGER NOT NULL DEFAULT 0,          -- observe 模式的阳性只记这一列，不执行
        basis           TEXT NOT NULL DEFAULT '',
        matched_rules   TEXT NOT NULL DEFAULT '[]',
        reason          TEXT NOT NULL DEFAULT '',
        decided_at_ms   INTEGER NOT NULL,
        UNIQUE (trigger_id, policy_version)
    );
    CREATE INDEX IF NOT EXISTS idx_sa_decisions_time ON sa_decisions (decided_at_ms DESC);
    "#,
    // ── V17：判定行加上第二层（模型研判）的留痕列（D-46 §实现方案第 4 条：gate 与研判分离）──
    // 增量变更：只给 sa_decisions 加列，存量判定行保持原值（空串 = 这一条没做过模型研判）。
    // 为什么加列而不是新表：一条判定最多跟一份研判结论（同一 trigger + 同一策略版本），
    // 拆两张表就要靠 join 才能回答「这条候选的研判结论是什么」，而界面与导出都要成对读它们。
    // 模型研判只写结论与依据，不写工单、不写执行状态——那仍然是 D-47 / D-48 的位置。
    r#"
    ALTER TABLE sa_decisions ADD COLUMN assess_status TEXT NOT NULL DEFAULT '';   -- ''=未研判；库里只会出现 done / failed（blocked 与 unavailable 只进巡检摘要，不写库）
    ALTER TABLE sa_decisions ADD COLUMN assess_verdict TEXT NOT NULL DEFAULT '';   -- worth / not_worth / unsure
    ALTER TABLE sa_decisions ADD COLUMN assess_reason TEXT NOT NULL DEFAULT '';    -- 模型给的理由（截断）
    ALTER TABLE sa_decisions ADD COLUMN assess_model TEXT NOT NULL DEFAULT '';     -- 用哪个模型判的（可复核）
    ALTER TABLE sa_decisions ADD COLUMN assess_tokens INTEGER NOT NULL DEFAULT 0;  -- 这次研判的 token 合计（计量口径）
    ALTER TABLE sa_decisions ADD COLUMN assess_error TEXT NOT NULL DEFAULT '';     -- 失败原因，中文，不含凭据
    ALTER TABLE sa_decisions ADD COLUMN assessed_at_ms INTEGER NOT NULL DEFAULT 0; -- 判定时刻（也当第二层的当日预算分子用）
    CREATE INDEX IF NOT EXISTS idx_sa_decisions_assessed ON sa_decisions (assessed_at_ms DESC);
    "#,
    // ── V18：把「候选排在哪个策略版本的研判队列里」从 gate 的判定版本里分出来 ──
    // 为什么要多这一列：`policy_version` 的含义是「这一条判定依据哪一版策略给出」，它是留痕，
    // 任何时候都不该被改写——改了就没人能回答「当时是按什么规则判的」。但第二层的队列在 V17
    // 直接挂在 `policy_version` 上，于是策略一升版，老候选就和当前版本对不上号，
    // 既不会被视为「已判」，也永远不会再被研判：它们孤悬在账本里，只是界面上看不见。
    // DSH 的口径是「策略变更 → 在跑的研判作废、候选重排队」，这里用队列键实现同一件事：
    // 升版时只挪 `assess_queue_version`，判定版本原样留着。
    // 已经判过的行（done / failed）不重排：结论留着可追溯，重判等于为同一个判断再花一次模型钱。
    // 增量变更：只加列并回填存量行的队列键，不动任何判定与研判结论。
    r#"
    ALTER TABLE sa_decisions ADD COLUMN assess_queue_version INTEGER NOT NULL DEFAULT 0;  -- 这一行现在排在哪个策略版本的研判队列里（与 policy_version 分开：前者是待办，后者是留痕）
    ALTER TABLE sa_decisions ADD COLUMN requeued_at_ms INTEGER NOT NULL DEFAULT 0;        -- 因策略升版被重排的时刻；0 = 从没被重排过
    UPDATE sa_decisions SET assess_queue_version = policy_version WHERE assess_queue_version = 0;
    CREATE INDEX IF NOT EXISTS idx_sa_decisions_assess_queue ON sa_decisions (assess_queue_version, assess_status);
    "#,
    // ── V19：定时任务补「间隔」与「一次性」两种调度（D-30） ──
    // 增量变更：只给 scheduled_tasks 追加两列，V1~V18 表与存量行不受影响。
    // 存量行的两列都是 NULL：四种周期频率（hourly/daily/weekly/monthly）本来就不读它们，
    // `next_run_at_ms` 的算法也未变，旧任务行为完全一致。
    // 为什么不复用 time_of_day 表达间隔：把「每 2 小时」写成 hourly 会让界面显示一个
    // 并不会按它触发的时刻；一次性任务同理，绝对时刻必须由调度器与界面读同一份事实。
    // 一次性任务的「执行后自动停用」写在 task::schedule::advance_next（触发前就地停用），
    // 不新增列：enabled=0 + next_run_at_ms=NULL 已经是这件事的完整事实。
    r#"
    ALTER TABLE scheduled_tasks ADD COLUMN interval_minutes INTEGER;   -- 仅 freq=interval；分钟数（1..10080）
    ALTER TABLE scheduled_tasks ADD COLUMN run_at_ms INTEGER;          -- 仅 freq=once；UTC 毫秒绝对时刻
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
                 'chat_feedback','hook_grants','model_usage_cost','memories',\
                 'sa_sources','sa_triggers','sa_policy','sa_decisions')",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 22);
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

    /// 增量升级：V11 老库升级到 V12 后，存量模型/用量数据必须完好，计价字段为「未配置」而非 0
    #[test]
    fn test_v12_upgrade_preserves_existing_rows() {
        let conn = mem_conn();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);",
        )
        .unwrap();
        // 模拟只跑过 V1~V11 的历史库
        for sql in MIGRATIONS.iter().take(11) {
            conn.execute_batch(sql).unwrap();
        }
        let versions: Vec<String> = (1..=11).map(|v| format!("({v}, datetime('now'))")).collect();
        conn.execute_batch(&format!(
            "INSERT INTO schema_version (version, applied_at) VALUES {};",
            versions.join(", ")
        ))
        .unwrap();
        conn.execute(
            "INSERT INTO model_configs (id, name, source, model_name, created_at, updated_at) \
             VALUES ('m-v11', 'V11 存量模型', 'openai_compat', 'qwen-plus', '2026-05-01', '2026-05-01')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO model_usage (model_name, day, prompt_tokens, completion_tokens, calls, last_used_at_ms) \
             VALUES ('qwen-plus', '2026-05-02', 88, 99, 7, 2000)",
            [],
        )
        .unwrap();

        run(&conn).unwrap();

        let (name, price_in, price_out, currency, fallbacks): (
            String,
            Option<f64>,
            Option<f64>,
            String,
            String,
        ) = conn
            .query_row(
                "SELECT name, price_input_per_m, price_output_per_m, price_currency, fallback_ids \
                 FROM model_configs WHERE id='m-v11'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?)),
            )
            .unwrap();
        assert_eq!(name, "V11 存量模型", "存量模型不得被清理");
        assert!(price_in.is_none() && price_out.is_none(), "存量模型单价必须是「未配置」，不能伪造成 0");
        assert_eq!(currency, "CNY");
        assert_eq!(fallbacks, "[]", "存量模型默认无故障转移链");

        let (tokens, latency_total, latency_samples): (i64, i64, i64) = conn
            .query_row(
                "SELECT prompt_tokens, latency_ms_total, latency_samples FROM model_usage \
                 WHERE model_name='qwen-plus' AND day='2026-05-02'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .unwrap();
        assert_eq!(tokens, 88, "存量用量台账不得被清理");
        assert_eq!((latency_total, latency_samples), (0, 0), "旧数据没有耗时样本，不能补零平均");

        let costs: i64 = conn
            .query_row("SELECT COUNT(*) FROM model_usage_cost", [], |r| r.get(0))
            .unwrap();
        assert_eq!(costs, 0, "新表 model_usage_cost 初始应为空（不伪造历史成本）");
        let max_v: i64 = conn
            .query_row("SELECT MAX(version) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(max_v, MIGRATIONS.len() as i64);
    }

    /// 增量升级：V15 老库升级到 V16 后，存量记忆与会话必须完好，四张 sa_ 表为空
    #[test]
    fn test_v16_upgrade_preserves_existing_rows() {
        let conn = mem_conn();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);",
        )
        .unwrap();
        // 模拟只跑过 V1~V15 的历史库
        for sql in MIGRATIONS.iter().take(15) {
            conn.execute_batch(sql).unwrap();
        }
        let versions: Vec<String> = (1..=15).map(|v| format!("({v}, datetime('now'))")).collect();
        conn.execute_batch(&format!(
            "INSERT INTO schema_version (version, applied_at) VALUES {};",
            versions.join(", ")
        ))
        .unwrap();
        conn.execute(
            "INSERT INTO memories (id, scope, workspace_id, kind, content, content_hash, source, created_at, updated_at) \
             VALUES ('mem-v15', 'global', '', 'fact', '存量事实', 'h-v15', 'manual', '2026-11-01', '2026-11-01')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO chat_messages (conv_id, turn_index, role, task_id, content, ts_ms) \
             VALUES ('conv-v15', 0, 'user', 'task-v15', '存量提问', 1000)",
            [],
        )
        .unwrap();

        run(&conn).unwrap();

        let content: String = conn
            .query_row("SELECT content FROM memories WHERE id='mem-v15'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(content, "存量事实", "存量长期记忆不得被清理");
        let msgs: i64 = conn
            .query_row("SELECT COUNT(*) FROM chat_messages WHERE conv_id='conv-v15'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(msgs, 1, "存量会话正文不得被清理");
        for table in ["sa_sources", "sa_triggers", "sa_policy", "sa_decisions"] {
            let rows: i64 = conn
                .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |r| r.get(0))
                .unwrap();
            assert_eq!(rows, 0, "新表 {table} 初始应为空（不伪造历史事件与判定）");
        }
        // 游标与幂等键必须在建表时就有，否则升级后的老库会重复接单
        let mut stmt = conn
            .prepare(
                "SELECT name FROM pragma_table_info('sa_triggers') UNION ALL \
                 SELECT name FROM pragma_table_info('sa_sources')",
            )
            .unwrap();
        let cols: Vec<String> = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        for need in ["external_id", "source_id", "cursor"] {
            assert!(cols.iter().any(|c| c == need), "缺关键列 {need}：{cols:?}");
        }
        let max_v: i64 = conn
            .query_row("SELECT MAX(version) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(max_v, MIGRATIONS.len() as i64);
    }

    /// 增量升级：V16 老库（判定行还没有第二层留痕列）升到 V17 后，存量判定必须完好，
    /// 新列以「未研判」的空值出现——不能把历史判定当成已经做过模型研判。
    #[test]
    fn test_v17_adds_assessment_columns_without_touching_existing_decisions() {
        let conn = mem_conn();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);",
        )
        .unwrap();
        for sql in MIGRATIONS.iter().take(16) {
            conn.execute_batch(sql).unwrap();
        }
        let versions: Vec<String> = (1..=16).map(|v| format!("({v}, datetime('now'))")).collect();
        conn.execute_batch(&format!(
            "INSERT INTO schema_version (version, applied_at) VALUES {};",
            versions.join(", ")
        ))
        .unwrap();
        conn.execute(
            "INSERT INTO sa_decisions (id, trigger_id, policy_version, strength, outcome, \
             would_accept, basis, matched_rules, reason, decided_at_ms) \
             VALUES ('dec-v16', 'trg-1', 1, 'inferred', 'would_accept', 1, '规则 review', '[]', \
             '命中启用规则', 1730000000000)",
            [],
        )
        .unwrap();

        run(&conn).unwrap();

        let reason: String = conn
            .query_row("SELECT reason FROM sa_decisions WHERE id='dec-v16'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(reason, "命中启用规则", "加列不得动存量判定");
        let (status, verdict, tokens, assessed_at): (String, String, i64, i64) = conn
            .query_row(
                "SELECT assess_status, assess_verdict, assess_tokens, assessed_at_ms \
                 FROM sa_decisions WHERE id='dec-v16'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
            )
            .unwrap();
        assert_eq!((status.as_str(), verdict.as_str()), ("", ""), "历史判定初始必须是未研判");
        assert_eq!((tokens, assessed_at), (0, 0));
        let mut stmt = conn
            .prepare("SELECT name FROM pragma_table_info('sa_decisions')")
            .unwrap();
        let cols: Vec<String> = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        for need in [
            "assess_status",
            "assess_verdict",
            "assess_reason",
            "assess_model",
            "assess_tokens",
            "assess_error",
            "assessed_at_ms",
        ] {
            assert!(cols.iter().any(|c| c == need), "缺关键列 {need}：{cols:?}");
        }
        let max_v: i64 = conn
            .query_row("SELECT MAX(version) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(max_v, MIGRATIONS.len() as i64);
    }

    /// 增量升级：V17 老库（候选队列直接挂在判定版本上）升到 V18 后，
    /// 队列键回填成各自的 `policy_version`，而判定版本、研判结论、留痕时刻一律不动。
    /// 这一条测的是「分列不能改历史」：升版重排队是运行期动作，不是迁移期动作。
    #[test]
    fn test_v18_splits_queue_key_from_gate_version_without_rewriting_history() {
        let conn = mem_conn();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);",
        )
        .unwrap();
        for sql in MIGRATIONS.iter().take(17) {
            conn.execute_batch(sql).unwrap();
        }
        let versions: Vec<String> = (1..=17).map(|v| format!("({v}, datetime('now'))")).collect();
        conn.execute_batch(&format!(
            "INSERT INTO schema_version (version, applied_at) VALUES {};",
            versions.join(", ")
        ))
        .unwrap();
        // 三种存量行：待研判的阳性、已判完的阳性、阴性留痕
        conn.execute_batch(
            "INSERT INTO sa_decisions (id, trigger_id, policy_version, strength, outcome, \
             would_accept, basis, matched_rules, reason, decided_at_ms) \
             VALUES \
             ('dec-pending', 'trg-1', 3, 'explicit', 'would_accept', 1, '点名指派', '[]', '直接指派', 1730000001000), \
             ('dec-judged',  'trg-2', 2, 'inferred', 'would_accept', 1, '规则 review', '[]', '命中启用规则', 1730000002000), \
             ('dec-nope',    'trg-3', 1, 'not_admitted', 'not_admitted', 0, '', '[]', '名单外', 1730000003000); \
             UPDATE sa_decisions SET assess_status='done', assess_verdict='worth', \
                    assess_reason='上下文里正开着', assess_model='某模型', assess_tokens=1234, \
                    assessed_at_ms=1730000009000 WHERE id='dec-judged';",
        )
        .unwrap();

        run(&conn).unwrap();

        // 存量行的队列键回填成自己的判定版本，重排时刻保持 0（迁移不制造「已经重排过」的假象）
        let mut stmt = conn
            .prepare(
                "SELECT id, policy_version, assess_queue_version, requeued_at_ms, assess_status, \
                        assess_verdict, assess_tokens, reason FROM sa_decisions ORDER BY id",
            )
            .unwrap();
        let rows = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, i64>(2)?,
                    r.get::<_, i64>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, String>(5)?,
                    r.get::<_, i64>(6)?,
                    r.get::<_, String>(7)?,
                ))
            })
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert_eq!(rows.len(), 3);
        for (id, gate_version, queue_version, requeued, status, verdict, tokens, reason) in rows {
            assert_eq!(
                queue_version, gate_version,
                "存量行 {id} 的队列键要回填成自己的判定版本"
            );
            assert_eq!(requeued, 0, "迁移不该把存量候选标成已重排：{id}");
            match id.as_str() {
                "dec-judged" => {
                    assert_eq!((status.as_str(), verdict.as_str()), ("done", "worth"));
                    assert_eq!(tokens, 1234, "已花的 token 不能被迁移抹掉");
                    assert_eq!(reason, "命中启用规则");
                }
                "dec-pending" => {
                    assert_eq!(status, "", "待研判的行升级后仍是未研判");
                    assert_eq!(reason, "直接指派");
                }
                _ => {
                    assert_eq!(status, "");
                    assert_eq!(reason, "名单外", "阴性留痕同样不许动");
                }
            }
        }
        let indexed: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_sa_decisions_assess_queue'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(indexed, 1, "队列查询要有索引：策略版本 × 是否已判");
        assert_eq!(
            conn.query_row("SELECT MAX(version) FROM schema_version", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            MIGRATIONS.len() as i64
        );
    }

    /// 增量升级：V18 老库（定时任务还没有「间隔/一次性」两列）升到 V19 后，存量任务与
    /// 存量执行记录必须原样可用。两列以 NULL 出现而不是 0：四种周期频率本来就不读它们，
    /// 而 0 会被读成「每 0 分钟」这种既非法又看起来像配置的值。
    /// 迁移也**不许**顺手重算任何老任务的 `next_run_at_ms` 或启停状态。
    #[test]
    fn test_v19_adds_schedule_columns_without_touching_existing_tasks() {
        let conn = mem_conn();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);",
        )
        .unwrap();
        // 模拟只跑过 V1~V18 的历史库
        for sql in MIGRATIONS.iter().take(18) {
            conn.execute_batch(sql).unwrap();
        }
        let versions: Vec<String> = (1..=18).map(|v| format!("({v}, datetime('now'))")).collect();
        conn.execute_batch(&format!(
            "INSERT INTO schema_version (version, applied_at) VALUES {};",
            versions.join(", ")
        ))
        .unwrap();
        conn.execute(
            "INSERT INTO scheduled_tasks (schedule_id, name, description, freq, time_of_day, \
             weekday, enabled, workspace_id, created_at_ms, updated_at_ms, next_run_at_ms, \
             last_run_at_ms, last_status) \
             VALUES ('sched-old', '每日舆情', '存量任务', 'daily', '08:00', NULL, 1, 'brand', \
             1000, 1000, 2000, 900, 'succeeded')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO schedule_runs (run_id, schedule_id, task_id, started_at_ms, \
             finished_at_ms, status, summary) \
             VALUES ('run-old', 'sched-old', 'task-old', 900, 950, 'succeeded', '存量执行')",
            [],
        )
        .unwrap();

        run(&conn).unwrap();

        let mut stmt = conn
            .prepare(
                "SELECT name, freq, time_of_day, enabled, next_run_at_ms, last_status, \
                        interval_minutes, run_at_ms FROM scheduled_tasks \
                 WHERE schedule_id = 'sched-old'",
            )
            .unwrap();
        let row = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, i64>(3)?,
                    r.get::<_, i64>(4)?,
                    r.get::<_, String>(5)?,
                    r.get::<_, Option<i64>>(6)?,
                    r.get::<_, Option<i64>>(7)?,
                ))
            })
            .unwrap()
            .next()
            .unwrap()
            .unwrap();
        assert_eq!(row.0, "每日舆情", "存量任务名不得被迁移改写");
        assert_eq!(
            (row.1.as_str(), row.2.as_str(), row.3),
            ("daily", "08:00", 1),
            "频率/时刻/启停三件事原样保留"
        );
        assert_eq!(row.4, 2000, "存量下次执行时间不得被迁移重算");
        assert_eq!(row.5, "succeeded", "上次执行状态是历史事实，迁移无权刷新");
        assert_eq!(row.6, None, "老任务的间隔列必须是 NULL，不能是 0");
        assert_eq!(row.7, None, "老任务的一次性时刻列必须是 NULL");

        let runs: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM schedule_runs WHERE run_id = 'run-old'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(runs, 1, "存量执行记录不得被升级清掉");
        let max_v: i64 = conn
            .query_row("SELECT MAX(version) FROM schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(max_v, MIGRATIONS.len() as i64);
    }

    #[test]
    fn test_run_idempotent() {
        let conn = mem_conn();
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
