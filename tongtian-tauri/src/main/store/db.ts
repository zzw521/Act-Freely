// ============================================================
// src/main/store/db.ts — SQLite Database (Singleton)
// ============================================================

import path from 'path';
import fs from 'fs';
import { app } from 'electron';

// ── better-sqlite3 with mock fallback ────────────────────────

interface DatabaseLike {
  exec(sql: string): void;
  prepare(sql: string): StatementLike;
  close(): void;
  pragma(name: string, options?: { simple: boolean }): unknown;
}

interface StatementLike {
  run(...params: unknown[]): { changes: number; lastInsertRowid: number | bigint };
  get(...params: unknown[]): unknown;
  all(...params: unknown[]): unknown[];
}

let BetterSqlite3Ctor: new (filename: string, options?: Record<string, unknown>) => DatabaseLike;

try {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const mod = require('better-sqlite3');
  BetterSqlite3Ctor = mod.default ?? mod;
} catch {
  // Mock fallback for environments where native module is unavailable
  const createNoopStatement = (): StatementLike => ({
    run: () => ({ changes: 0, lastInsertRowid: 0 }),
    get: () => undefined,
    all: () => [],
  });

  BetterSqlite3Ctor = class MockDatabase implements DatabaseLike {
    exec(_sql: string): void { /* noop */ }
    prepare(_sql: string): StatementLike { return createNoopStatement(); }
    close(): void { /* noop */ }
    pragma(_name: string, _options?: { simple: boolean }): unknown { return 0; }
  } as unknown as new (filename: string) => DatabaseLike;
}

// ── Paths ────────────────────────────────────────────────────

function getDataDir(): string {
  try {
    return path.join(app.getPath('userData'), 'data');
  } catch {
    // Fallback when app is not ready (e.g. during tests)
    const home = process.env.APPDATA
      || process.env.HOME
      || path.join(process.env.USERPROFILE || '', '.tongtian');
    return path.join(home, '.tongtian');
  }
}

function getDbPath(): string {
  return path.join(getDataDir(), 'data.db');
}

// ── DDL ──────────────────────────────────────────────────────

const SCHEMA_VERSION = 2;

const TABLES_SQL = `
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'idle',
  user_id TEXT NOT NULL,
  plan_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  attachments_json TEXT DEFAULT '{}',
  ts TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS file_ops (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  op TEXT NOT NULL,
  src_path TEXT NOT NULL,
  dst_path TEXT,
  backup_path TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS file_backups (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  file_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  backup_path TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS model_configs (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  source TEXT NOT NULL,
  base_url TEXT NOT NULL,
  api_key_encrypted TEXT NOT NULL,
  model_name TEXT NOT NULL,
  context_window INTEGER DEFAULT 4096,
  is_default INTEGER DEFAULT 0,
  agent_binding TEXT DEFAULT '',
  force_local INTEGER DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_logs (
  id TEXT PRIMARY KEY,
  timestamp TEXT NOT NULL DEFAULT (datetime('now')),
  event TEXT NOT NULL,
  actor TEXT NOT NULL,
  details_json TEXT DEFAULT '{}',
  task_id TEXT,
  model_id TEXT
);

CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  agent_config_json TEXT NOT NULL DEFAULT '{}',
  model_binding TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  memory_store_id TEXT NOT NULL,
  skill_set_json TEXT DEFAULT '[]',
  mcp_tools_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS skills (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  source TEXT NOT NULL,
  skill_path TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER DEFAULT 1,
  installed_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mcp_servers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  transport TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  auth_type TEXT NOT NULL DEFAULT 'none',
  auth_config_json TEXT DEFAULT '{}',
  tool_whitelist_json TEXT DEFAULT '[]',
  tool_blacklist_json TEXT DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'disconnected',
  last_connected_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sandbox_policies (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  execution_level TEXT NOT NULL DEFAULT 'L0',
  file_allowlist_json TEXT DEFAULT '[]',
  file_blocklist_json TEXT DEFAULT '[]',
  tool_allowlist_json TEXT DEFAULT '[]',
  tool_blocklist_json TEXT DEFAULT '[]',
  max_cpu_percent INTEGER DEFAULT 50,
  max_memory_mb INTEGER DEFAULT 512,
  max_disk_write_mbps REAL DEFAULT 10.0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schedule_tasks (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  cron_expression TEXT NOT NULL,
  goal TEXT NOT NULL,
  model_binding TEXT DEFAULT '',
  enabled INTEGER DEFAULT 1,
  last_run_at TEXT,
  last_status TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS model_usage (
  id TEXT PRIMARY KEY,
  model_id TEXT NOT NULL REFERENCES model_configs(id),
  workspace_id TEXT REFERENCES workspaces(id),
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  success INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── 千问对标整改新增表（spec §6.2） ──

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS awareness_profiles (
  id TEXT PRIMARY KEY DEFAULT 'global',
  mode INTEGER DEFAULT 1,
  auto_memory INTEGER DEFAULT 1,
  style TEXT NOT NULL DEFAULT 'professional',
  custom_prompt TEXT DEFAULT '',
  work_manual TEXT DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memory_entries (
  id TEXT PRIMARY KEY,
  content TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'auto',
  session_id TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS connector_states (
  connector_id TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  installed INTEGER DEFAULT 0,
  auth_status TEXT NOT NULL DEFAULT 'none',
  server_ref TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS archived_tasks (
  task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL,
  archived_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS hook_configs (
  id TEXT PRIMARY KEY DEFAULT 'global',
  hooks_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
`;

const INDEXES_SQL = `
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id);
CREATE INDEX IF NOT EXISTS idx_file_ops_task ON file_ops(task_id);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_logs(event);
CREATE INDEX IF NOT EXISTS idx_audit_workspace ON audit_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_audit_model ON audit_logs(model_id);
CREATE INDEX IF NOT EXISTS idx_workspaces_status ON workspaces(status);
CREATE INDEX IF NOT EXISTS idx_skills_source ON skills(source);
CREATE INDEX IF NOT EXISTS idx_skills_enabled ON skills(enabled);
CREATE INDEX IF NOT EXISTS idx_mcp_servers_status ON mcp_servers(status);
CREATE INDEX IF NOT EXISTS idx_sandbox_policies_workspace ON sandbox_policies(workspace_id);
CREATE INDEX IF NOT EXISTS idx_model_usage_model ON model_usage(model_id);
CREATE INDEX IF NOT EXISTS idx_model_usage_date ON model_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_schedule_enabled ON schedule_tasks(enabled);
CREATE INDEX IF NOT EXISTS idx_memory_source ON memory_entries(source);
CREATE INDEX IF NOT EXISTS idx_archived_workspace ON archived_tasks(workspace_id);
`;

// ── Database Class ───────────────────────────────────────────

class Database {
  private static instance: Database | null = null;
  private db: DatabaseLike;
  private dbPath: string;

  private constructor() {
    this.dbPath = getDbPath();
    const dir = path.dirname(this.dbPath);
    fs.mkdirSync(dir, { recursive: true });

    this.db = new BetterSqlite3Ctor(this.dbPath, {
      // WAL mode for better concurrent read performance
    });

    this.init();
  }

  private init(): void {
    // Enable WAL mode and foreign keys
    this.db.pragma('journal_mode = WAL');
    this.db.pragma('foreign_keys = ON');

    // Create all tables
    this.db.exec(TABLES_SQL);
    this.db.exec(INDEXES_SQL);

    // Handle schema version
    const row = this.db.prepare('SELECT version FROM schema_version LIMIT 1').get() as
      { version: number } | undefined;

    if (!row) {
      this.db.prepare('INSERT INTO schema_version (version) VALUES (?)').run(SCHEMA_VERSION);
    } else if (row.version < SCHEMA_VERSION) {
      this.migrate(row.version);
    }
  }

  private migrate(fromVersion: number): void {
    // v1 → v2：千问对标整改新增表（CREATE TABLE IF NOT EXISTS 幂等，重新执行 DDL 即可）
    if (fromVersion < 2) {
      this.db.exec(TABLES_SQL);
      this.db.exec(INDEXES_SQL);
    }
    this.db.prepare('UPDATE schema_version SET version = ?').run(SCHEMA_VERSION);
  }

  static getInstance(): Database {
    if (!Database.instance) {
      Database.instance = new Database();
    }
    return Database.instance;
  }

  getDb(): DatabaseLike {
    return this.db;
  }

  getDbPath(): string {
    return this.dbPath;
  }

  close(): void {
    if (this.db) {
      this.db.close();
      Database.instance = null;
    }
  }

  /** Reset singleton (for testing only) */
  static resetInstance(): void {
    if (Database.instance) {
      Database.instance.close();
    }
    Database.instance = null;
  }
}

export default Database;
export type { DatabaseLike, StatementLike };
