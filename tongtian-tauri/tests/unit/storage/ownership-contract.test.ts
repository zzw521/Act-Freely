/**
 * 存储归属契约（缺陷 D-43b）
 *
 * 三套存储各有写入方：桌面 SQLite `tongtian.db` 只由 Rust 主进程打开，服务端
 * `data/market.db` 只由服务端进程打开，引擎只写自己数据根下的文件（检查点、意识
 * 目录、models.json、IM 凭据、知识索引、用量台账）。此前只有文档约定，没有测试拦
 * 截：谁都能顺手跨库直连，接远程工作区或多租户部署时才会暴露。
 *
 * 本文件从源码层面把边界钉死，并且是**全量扫描**而不是白名单几个文件——新增的
 * 跨库访问必须让这里先红。文档（PRD 6.5）与代码同源，也在本文件校验。
 */

import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

function dir(rel: string): string {
  return fileURLToPath(new URL(rel, import.meta.url));
}

/** 递归列出某目录下的源码文件（跳过依赖与构建产物） */
function sources(absDir: string, extensions: string[]): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(absDir)) {
    if (entry === 'node_modules' || entry === '__pycache__' || entry === 'dist' || entry === '.venv') continue;
    const full = join(absDir, entry);
    if (statSync(full).isDirectory()) {
      out.push(...sources(full, extensions));
    } else if (extensions.some((ext) => entry.endsWith(ext))) {
      out.push(full);
    }
  }
  return out;
}

const engineFiles = () => sources(dir('../../../engine'), ['.py']);
const serverFiles = () => sources(dir('../../../../tongtian-server/app'), ['.py']);
const rustFiles = () => sources(dir('../../../src-tauri/src'), ['.rs']);
const rendererFiles = () => sources(dir('../../../src'), ['.ts', '.tsx']);

/** 找出包含任一模式的文件（返回可定位的短路径），用于给出能直接照做的失败信息 */
function offenders(files: string[], patterns: RegExp[]): string[] {
  return files
    .filter((file) => {
      const text = readFileSync(file, 'utf8');
      return patterns.some((pattern) => pattern.test(text));
    })
    .map((file) => file.replace(/\\/g, '/').split('/').slice(-2).join('/'));
}

/**
 * 「打开某张库」的判定看字符串字面量的**文件名**，不看裸文件名：
 * 注释与文档需要提到别家的库名（`data_paths.py` 就在解释引擎不许碰谁），提到别家
 * 库名也不等于越界——`market.db` 里就含 `t.db` 这三个字符。只有把库名写成路径
 * 字面量，才说明真的在拼这条路径。
 */
function dbNamesIn(file: string): string[] {
  const text = readFileSync(file, 'utf8');
  // 生产代码优先：Rust 测试夹具用临时库名，不代表跨库直连
  const production = text.split('#[cfg(test)]')[0];
  return [...production.matchAll(/["']([^"']*\.(?:db|jsonl|json))["']/g)].map((m) =>
    m[1].replace(/\\/g, '/').split('/').pop() ?? m[1]
  );
}

/** 找出真的把某个库文件名当路径字面量使用的文件（返回可定位的短路径） */
function opensDb(files: string[], names: string[]): string[] {
  return files
    .filter((file) => dbNamesIn(file).some((name) => names.includes(name)))
    .map((file) => file.replace(/\\/g, '/').split('/').slice(-2).join('/'));
}

const DESKTOP_DB_FILES = ['tongtian.db', 't.db'];
const SERVER_DB_FILES = ['market.db'];

describe('跨库直连禁令', () => {
  it('引擎源码不打开桌面库', () => {
    expect(opensDb(engineFiles(), DESKTOP_DB_FILES)).toEqual([]);
  });

  it('服务端源码不打开桌面库', () => {
    expect(opensDb(serverFiles(), DESKTOP_DB_FILES)).toEqual([]);
  });

  it('桌面（Rust 与渲染层）不直写服务端库', () => {
    expect(opensDb([...rustFiles(), ...rendererFiles()], SERVER_DB_FILES)).toEqual([]);
  });

  it('引擎不读写服务端数据目录', () => {
    // 引擎只经 API 与服务端交互（用量、introspect），拿到别人的库文件就是越界
    expect(offenders(engineFiles(), [/TT_SERVER_DATA_DIR/])).toEqual([]);
    expect(opensDb(engineFiles(), SERVER_DB_FILES)).toEqual([]);
  });

  it('渲染层不直接打开任何 SQLite 文件', () => {
    expect(offenders(rendererFiles(), [/sqlite3\.connect/, /rusqlite/, /Database\.open/])).toEqual([]);
  });

  it('启动脚本不再拉起已退役的 auth-server（避免生成第二份账号库 auth.db）', () => {
    for (const script of ['init.ps1', 'init.sh', 'run-dev.ps1']) {
      const text = readFileSync(dir(`../../../${script}`), 'utf8');
      expect(text, script).not.toContain('auth-server');
    }
  });
});

describe('路径常量单一来源', () => {
  it('引擎数据根只在 data_paths.py 里解析', () => {
    const files = engineFiles().filter((file) => !file.replace(/\\/g, '/').endsWith('engine/data_paths.py'));
    const drifted = files.filter((file) => {
      const text = readFileSync(file, 'utf8');
      return /environ\.get\(\s*["'](TONGTIANWORK_HOME|TT_DATA_HOME)["']\s*\)/.test(text)
        || /environ\[\s*["'](TONGTIANWORK_HOME|TT_DATA_HOME)["']\s*\]/.test(text);
    });
    expect(drifted.map((f) => f.replace(/\\/g, '/').split('/').slice(-2).join('/'))).toEqual([]);
  });

  it('生产代码里只有属主引用桌面库文件：存储层开库、沙箱层拒绝访问', () => {
    const named = opensDb(rustFiles(), ['tongtian.db']).sort();
    expect(named).toEqual(['sandbox/win.rs', 'store/mod.rs']);
    // 渲染层只走 IPC，不出现库文件名
    expect(opensDb(rendererFiles(), DESKTOP_DB_FILES)).toEqual([]);
  });

  it('引擎台账与检查点都落在引擎数据根之下', () => {
    const paths = readFileSync(dir('../../../engine/data_paths.py'), 'utf8');
    expect(paths).toContain('def engine_home() -> Path:');
    expect(paths).toContain('USAGE_LEDGER_FILENAME = "model-usage.jsonl"');
    expect(paths).toContain('def usage_ledger_file() -> Path:');
    expect(paths).toContain('def checkpoints_dir() -> Path:');
  });
});

describe('项目维度只落在两处元数据', () => {
  it('会话表用增量迁移补 workspace_id，并在 SQL 内过滤', () => {
    const migration = readFileSync(dir('../../../src-tauri/src/store/migrate.rs'), 'utf8');
    expect(migration).toContain("ALTER TABLE chat_messages ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'default'");
    expect(migration).toContain('idx_chat_messages_workspace');
    const messages = readFileSync(dir('../../../src-tauri/src/store/messages.rs'), 'utf8');
    expect(messages).toContain('WHERE (?1 IS NULL OR c.workspace_id = ?1)');
    // 助手行必须沿用本轮归属，不能各自写 default
    expect(messages).toContain('workspace_id,\n            content,');
  });

  it('知识库归属在 Rust 侧过滤，引擎只认下发的列表', () => {
    const knowledge = readFileSync(dir('../../../src-tauri/src/knowledge/mod.rs'), 'utf8');
    expect(knowledge).toContain('fn normalize_scope');
    expect(knowledge).toContain('pub fn runtime_bases(root: &Path, workspace_id: &str)');
    expect(knowledge).toContain('v.scope == SCOPE_GLOBAL');
    const runtime = readFileSync(dir('../../../src-tauri/src/bridge/runtime.rs'), 'utf8');
    expect(runtime).toContain('knowledge::runtime_bases(&knowledge_root, workspace_id)');
    // 引擎不自行扫目录挑库（否则 Rust 的过滤形同虚设）
    const engine = readFileSync(dir('../../../engine/knowledge_runtime.py'), 'utf8');
    expect(engine).toContain('block.get("bases")');
    expect(engine).not.toContain('listdir');
    expect(engine).not.toContain('scandir');
  });
});

describe('长期记忆库只由桌面 Rust 写入', () => {
  it('V14 迁移建 memories 表，按内容哈希去重', () => {
    const migration = readFileSync(dir('../../../src-tauri/src/store/migrate.rs'), 'utf8');
    expect(migration).toContain('CREATE TABLE IF NOT EXISTS memories');
    expect(migration).toContain('UNIQUE (scope, workspace_id, content_hash)');
    expect(migration).toContain('idx_memories_scope_time');
  });

  it('存储层校验归属，并在 SQL 内按项目过滤', () => {
    const store = readFileSync(dir('../../../src-tauri/src/store/memories.rs'), 'utf8');
    expect(store).toContain('pub fn retrieve');
    expect(store).toContain("m.scope = 'global' OR m.workspace_id = ?1");
    expect(store).toContain('MEMORY_INVALID_SCOPE');
    // 旧 MEMORY.md 只导入一次，不删不改原文件
    expect(store).toContain('memories.legacy_imported');
  });

  it('提交时由 Rust 检索并下发命中条目，引擎不自行挑选', () => {
    const runtime = readFileSync(dir('../../../src-tauri/src/bridge/runtime.rs'), 'utf8');
    expect(runtime).toContain('crate::store::memories::retrieve');
    expect(runtime).toContain('"store": "desktop"');
    const engine = readFileSync(dir('../../../engine/runtime_context.py'), 'utf8');
    expect(engine).toContain('desktop_memory_mode');
    expect(engine).toContain('compose_memory_store');
    // 桌面模式下文件里的结构化行不再重复注入
    expect(engine).toContain('include_entries=not store_memory');
  });

  it('引擎只上报候选，落库与命令注册都在 Rust', () => {
    const server = readFileSync(dir('../../../engine/server.py'), 'utf8');
    expect(server).toContain('_auto_memory_candidate');
    expect(server).toContain('memory_candidate');
    const agent = readFileSync(dir('../../../src-tauri/src/bridge/agent.rs'), 'utf8');
    expect(agent).toContain('memories::persist_candidate');
    const lib = readFileSync(dir('../../../src-tauri/src/lib.rs'), 'utf8');
    expect(lib).toContain('awareness::memory_list');
    expect(lib).toContain('memories::import_legacy');
  });
});

describe('长期记忆第二阶段：复核、事实与导出导入（D-35）', () => {
  it('V15 增量迁移补状态与事实三元组，不重建表', () => {
    const migration = readFileSync(dir('../../../src-tauri/src/store/migrate.rs'), 'utf8');
    expect(migration).toContain("ALTER TABLE memories ADD COLUMN status TEXT NOT NULL DEFAULT 'active'");
    expect(migration).toContain('ADD COLUMN superseded_by TEXT');
    expect(migration).toContain("ALTER TABLE memories ADD COLUMN subject TEXT NOT NULL DEFAULT ''");
    expect(migration).toContain('ADD COLUMN confidence REAL NOT NULL DEFAULT 1');
    expect(migration).toContain('idx_memories_status');
    // 第二阶段只做增量列，不重写 memories 建表语句
    const v15 = migration.split('V15')[1] ?? '';
    expect(v15).not.toContain('DROP TABLE');
    expect(v15).not.toContain('DELETE FROM memories');
  });

  it('待审条目默认不进召回，确认 / 驳回 / 退休 / 恢复都走 Rust', () => {
    const store = readFileSync(dir('../../../src-tauri/src/store/memories.rs'), 'utf8');
    expect(store).toContain('pub fn set_status');
    expect(store).toContain('REVIEW_ACTIONS');
    expect(store).toContain('MEMORY_STATUS_INVALID');
    // 召回只取生效行，待审与归档不得混入
    expect(store).toContain('status: Some(STATUS_ACTIVE)');
    expect(store).toContain('pub fn retrieve');
  });

  it('结构化事实冲突软退休：旧事实归档并记 superseded_by，不物理删除', () => {
    const store = readFileSync(dir('../../../src-tauri/src/store/memories.rs'), 'utf8');
    expect(store).toContain('fn resolve_conflicts');
    expect(store).toContain('superseded_by');
    expect(store).toContain('MEMORY_SUPERSEDED');
    // 实体页按 subject 聚合，不新增实体表（记忆这一侧始终只有 `memories` 一张表）
    expect(store).toContain('pub fn entities');
    expect(store).toContain('EntityPage');
  });

  it('引擎记忆工具只在桌面模式注册，且只读下发的目录、不碰桌面库', () => {
    const tools = readFileSync(dir('../../../engine/memory_tools.py'), 'utf8');
    expect(tools).toContain('memory_search');
    expect(tools).toContain('memory_get');
    expect(tools).toContain('memory_save');
    expect(tools).toContain('block.get("store") != "desktop"');
    // 引擎不自行开库：只读 runtime.memory.catalog，写入靠事件交回 Rust
    expect(tools).not.toContain('sqlite3');
    expect(tools).not.toContain('tongtian.db');
    const runtime = readFileSync(dir('../../../src-tauri/src/bridge/runtime.rs'), 'utf8');
    expect(runtime).toContain('"catalog"');
    expect(runtime).toContain('review_enabled');
  });

  it('memory_save 与多候选都经由 Rust 去重落库，隐私任务一律跳过', () => {
    const agent = readFileSync(dir('../../../src-tauri/src/bridge/agent.rs'), 'utf8');
    expect(agent).toContain('"memory_save"');
    expect(agent).toContain('"memory_candidates"');
    expect(agent).toContain('!p0.privacy');
    const server = readFileSync(dir('../../../engine/server.py'), 'utf8');
    expect(server).toContain('_auto_memory_candidates');
    expect(server).toContain('_extract_fact_candidates');
  });

  it('导出 / 导入 / 压缩命令注册在 lib.rs，导入前强制备份', () => {
    const lib = readFileSync(dir('../../../src-tauri/src/lib.rs'), 'utf8');
    for (const command of [
      'awareness::memory_review',
      'awareness::memory_entities',
      'awareness::memory_export',
      'awareness::memory_import',
      'awareness::memory_compact',
    ]) {
      expect(lib).toContain(command);
    }
    const store = readFileSync(dir('../../../src-tauri/src/store/memories.rs'), 'utf8');
    expect(store).toContain('pub fn export_to');
    expect(store).toContain('pub fn import_json');
    expect(store).toContain('pub fn compact');
    expect(store).toContain('EXPORT_FORMAT: &str = "tongtian.memories"');
    expect(store).toContain('COMPACT_KEEP_PER_SCOPE');
  });

  it('渲染层只走 IPC：管理能力经 Tauri 命令，浏览器直连明确拒绝', () => {
    const api = readFileSync(dir('../../../src/renderer/lib/alignment-api.ts'), 'utf8');
    expect(api).toContain('reviewMemory');
    expect(api).toContain('exportMemories');
    expect(api).toContain('importMemories');
    expect(api).toContain('compactMemories');
    expect(api).toContain('supportsMemoryManagement');
    const tauri = readFileSync(dir('../../../src/renderer/lib/tauri-api.ts'), 'utf8');
    expect(tauri).toContain('awareness:memoryReview');
    expect(tauri).toContain('awareness:memoryExport');
    const engine = readFileSync(dir('../../../src/renderer/lib/engine-api.ts'), 'utf8');
    expect(engine).toContain('MEMORY_DESKTOP_ONLY');
    // Mock 通道独立实现，便于设置页在浏览器里自测
    const mock = readFileSync(dir('../../../src/renderer/lib/awareness-local.ts'), 'utf8');
    expect(mock).toContain('MEMORY_EXPORT_FORMAT');
    expect(mock).toContain('backupLocalMemories');
  });
});

describe('长期记忆第三阶段：模型抽取、文档对账与迁移包（D-35）', () => {
  it('模型抽取开关存在意识配置里，默认关闭，并经运行时下发', () => {
    const awareness = readFileSync(dir('../../../src-tauri/src/awareness/mod.rs'), 'utf8');
    expect(awareness).toContain('read_bool("memoryModelExtract", false)');
    expect(awareness).toContain('pub fn model_extract_enabled');
    // 读取失败按关闭处理：意识配置损坏不能变成多发一次模型调用
    expect(awareness).toContain('load_profile(dir).memory_model_extract');
    const runtime = readFileSync(dir('../../../src-tauri/src/bridge/runtime.rs'), 'utf8');
    expect(runtime).toContain('model_extract_enabled');
    expect(runtime).toContain('"modelExtract"');
  });

  it('引擎模型抽取受四重门控，失败只降级不影响任务', () => {
    const server = readFileSync(dir('../../../engine/server.py'), 'utf8');
    expect(server).toContain('def _model_memory_extract_enabled');
    expect(server).toContain('def _parse_model_facts');
    expect(server).toContain('def _merge_memory_candidates');
    expect(server).toContain('async def _model_fact_candidates');
    expect(server).toContain('block.get("modelExtract")');
    // 隐私 / 关闭自动记忆 / 非桌面记忆库都在启用函数里短路
    expect(server).toContain('_auto_memory_decision(runtime, privacy=False)');
    // 超时与异常都返回空候选 + 说明，不抛出
    expect(server).toContain('asyncio.TimeoutError');
    expect(server).toContain('已保留规则抽取');
    // 合并后再交给 Rust，引擎仍不碰桌面库
    expect(server).toContain('_merge_memory_candidates(');
    expect(server).toContain('secret_skipped');
    expect(server).not.toContain('tongtian.db');
  });

  it('MEMORY.md 对账只导入不覆盖，命令由 Rust 注册', () => {
    const store = readFileSync(dir('../../../src-tauri/src/store/memories.rs'), 'utf8');
    const sync = store.split('pub fn sync_doc')[1].split('pub fn export_value')[0];
    expect(sync).toContain('MEMORY.md');
    expect(sync).toContain('SCOPE_GLOBAL');
    // 对账只读文件：不得出现写文件或删除记忆
    expect(sync).not.toContain('fs::write');
    expect(sync).not.toContain('DELETE FROM memories');
    const awareness = readFileSync(dir('../../../src-tauri/src/awareness/mod.rs'), 'utf8');
    expect(awareness).toContain('pub async fn memory_doc_sync');
    const lib = readFileSync(dir('../../../src-tauri/src/lib.rs'), 'utf8');
    expect(lib).toContain('awareness::memory_doc_sync');
  });

  it('.hmpkg 是带 manifest 与 sha256 校验的 ZIP，导入复用强制备份', () => {
    const store = readFileSync(dir('../../../src-tauri/src/store/memories.rs'), 'utf8');
    expect(store).toContain('PACKAGE_FORMAT: &str = "tongtian.hmpkg"');
    expect(store).toContain('PACKAGE_MAX_BYTES');
    expect(store).toContain('pub fn export_package');
    expect(store).toContain('pub fn import_package');
    expect(store).toContain('"manifest.json"');
    expect(store).toContain('"memories.json"');
    expect(store).toContain('"sha256:{checksum}"');
    expect(store).toContain('迁移包校验失败');
    // 复用现有 Office zip 读取，不新增 zip 依赖
    expect(store).toContain('crate::file::office::zip_entries');
    // 通过校验后仍走 import_json，备份与去重语义不分叉
    expect(store).toContain('import_json(store, memories_text, now_iso, backup_dir)');
    const lib = readFileSync(dir('../../../src-tauri/src/lib.rs'), 'utf8');
    expect(lib).toContain('awareness::memory_export_package');
    expect(lib).toContain('awareness::memory_import_package');
  });

  it('渲染层三个新能力经 IPC；浏览器直连拒绝，Mock 通道如实标注本地路径', () => {
    const contracts = readFileSync(dir('../../../src/shared/contracts.ts'), 'utf8');
    expect(contracts).toContain("AWARENESS_MEMORY_DOC_SYNC: 'awareness:memoryDocSync'");
    expect(contracts).toContain("AWARENESS_MEMORY_EXPORT_PACKAGE: 'awareness:memoryExportPackage'");
    expect(contracts).toContain("AWARENESS_MEMORY_IMPORT_PACKAGE: 'awareness:memoryImportPackage'");
    const api = readFileSync(dir('../../../src/renderer/lib/alignment-api.ts'), 'utf8');
    expect(api).toContain('syncMemoryDoc');
    expect(api).toContain('exportMemoryPackage');
    expect(api).toContain('importMemoryPackage');
    const tauri = readFileSync(dir('../../../src/renderer/lib/tauri-api.ts'), 'utf8');
    expect(tauri).toContain("invoke('memory_doc_sync')");
    expect(tauri).toContain("invoke('memory_export_package')");
    expect(tauri).toContain("invoke('memory_import_package'");
    const engine = readFileSync(dir('../../../src/renderer/lib/engine-api.ts'), 'utf8');
    expect(engine).toContain("case 'awareness:memoryDocSync':");
    expect(engine).toContain("case 'awareness:memoryImportPackage':");
    const mock = readFileSync(dir('../../../src/renderer/lib/awareness-local.ts'), 'utf8');
    expect(mock).toContain("MEMORY_PACKAGE_FORMAT = 'tongtian.hmpkg'");
    expect(mock).toContain('exportLocalMemoryPackage');
    expect(mock).toContain('importLocalMemoryPackage');
    // Mock 不伪装成桌面文件
    expect(mock).toContain('localStorage://');
    const page = readFileSync(dir('../../../src/renderer/pages/Settings/index.tsx'), 'utf8');
    expect(page).toContain('模型辅助抽取');
    expect(page).toContain('与 MEMORY.md 对账');
  });
});

/** 桌面 Rust 里属于 Super-agent 账本那部分的源文件（`superagent/` 整个模块） */
function superagentFiles(): string[] {
  return rustFiles().filter((file) => /[/\\]superagent[/\\]/.test(file));
}

describe('Super-agent 事件账本（D-46）归属与零执行', () => {
  const LEDGER_TABLES = /sa_(sources|triggers|policy|decisions)\b/;

  it('引擎、服务端与渲染层都不碰四张 sa_ 表', () => {
    for (const [name, files] of [
      ['引擎', engineFiles()],
      ['服务端', serverFiles()],
      ['渲染层', rendererFiles()],
    ] as const) {
      expect(offenders(files, [LEDGER_TABLES]), `${name} 不得直接读写桌面事件账本`).toEqual([]);
    }
  });

  it('桌面里只有存储层与账本模块提到 sa_ 表名（别的模块不写这条 SQL）', () => {
    const named = rustFiles()
      .filter((file) => LEDGER_TABLES.test(readFileSync(file, 'utf8')))
      .map((file) => file.replace(/\\/g, '/').split('/').slice(-2).join('/'))
      .sort();
    expect(named).toEqual([
      'store/migrate.rs',
      'superagent/gate.rs',
      'superagent/ledger.rs',
      'superagent/policy.rs',
      'superagent/sources.rs',
    ]);
  });

  it('事件源模块只提表名不写 SQL：所有账本读写都走 ledger（一处口径）', () => {
    // sources.rs 的文档注释要解释游标与 last_error 落在哪张表，提到表名是必要的；
    // 但生产代码里出现 INSERT/UPDATE 就是第二套写库路径，D-43 的教训不允许。
    const sources = readFileSync(dir('../../../src-tauri/src/superagent/sources.rs'), 'utf8');
    const production = sources.split('#[cfg(test)]')[0];
    expect([...production.matchAll(/(?:INSERT INTO|UPDATE)\s+sa_/gi)].map((m) => m[0])).toEqual([]);
    // 生产代码也不许自己 SELECT：读一律靠 ledger 的行映射函数
    expect([...production.matchAll(/SELECT[\s\S]{0,80}?FROM\s+sa_/gi)].map((m) => m[0])).toEqual([]);
  });

  it('账本与 gate 模块里没有任何执行入口：影子模式是结构上的，不是靠约定', () => {
    const files = superagentFiles();
    expect(files.length, 'superagent 模块应有源文件').toBeGreaterThan(0);
    // 起任务只有 `bridge::agent` 这一条路；开进程只有 `Command::new`。两者都不许出现在账本里。
    expect(offenders(files, [/bridge::agent/, /Command::new/, /start_workspace/])).toEqual([]);
  });

  it('账本写入路径自带截断，超预算的载荷只留标记', () => {
    const ledger = readFileSync(dir('../../../src-tauri/src/superagent/ledger.rs'), 'utf8');
    expect(ledger).toContain('MAX_PAYLOAD_CHARS');
    expect(ledger).toContain('MAX_TRIGGER_TEXT_CHARS');
    expect(ledger).toContain('"truncated"');
    // 判定要留痕但不抄正文：理由与依据各自截断
    expect(ledger).toContain('MAX_REASON_CHARS');
  });

  it('自动接单在策略层就被拒绝，回落只可能更保守', () => {
    const policy = readFileSync(dir('../../../src-tauri/src/superagent/policy.rs'), 'utf8');
    expect(policy).toContain('pub fn assert_mode_allowed');
    expect(policy).toContain('DEFAULT_MODE: &str = MODE_OBSERVE');
    const ledger = readFileSync(dir('../../../src-tauri/src/superagent/ledger.rs'), 'utf8');
    // 库里存着的历史 auto 在读出时降级，不信任存储
    expect(ledger).toContain('MODE_OBSERVE');
  });
});

describe('PRD 6.5 与代码同源', () => {
  const prd = () => readFileSync(dir('../../../../docs/PRD-任我行.md'), 'utf8');

  it('存储归属表写的是真实库名与真实数据根', () => {
    const section = prd().split('### 6.5 存储归属')[1].split('\n## 7')[0];
    expect(section).toContain('`{TONGTIAN_HOME}/tongtian.db`');
    expect(section).not.toContain('`{TONGTIAN_HOME}/t.db`');
    expect(section).toContain('data/market.db');
    // 引擎数据根与 Rust 不同名，文档必须写清楚，否则运维会去错目录排障
    expect(section).toContain('TONGTIANWORK_HOME');
    expect(section).toContain('usage/model-usage.jsonl');
  });

  it('引擎用量已落盘的口径写进文档，不再描述成重启清零', () => {
    const section = prd().split('### 6.5 存储归属')[1].split('\n## 7')[0];
    expect(section).not.toContain('引擎侧目前只在进程内累计，重启即丢');
    expect(section).toContain('见缺陷 D-43');
  });

  it('6.2 本地数据写的是真实库名、真实表数量口径与单一事实来源', () => {
    const section = prd().split('### 6.2 本地数据')[1].split('### 6.3')[0];
    expect(section).toContain('`{TONGTIAN_HOME}/tongtian.db`');
    expect(section).not.toContain('t.db');
    // 文档口径 = `test_run_creates_tables` 清点到的表数减去 `schema_version`（迁移表不算业务表）。
    // 加迁移时两个数字一起改，否则「文档说 21 张、库里有 22 张」这种漂移没人拦。
    expect(section).toContain('21 张业务表');
    expect(section).not.toContain('17 张业务表');
    const migrate = readFileSync(dir('../../../src-tauri/src/store/migrate.rs'), 'utf8');
    expect(migrate).toContain('assert_eq!(count, 22)');
    expect(migrate).toContain("'sa_sources','sa_triggers','sa_policy','sa_decisions'");
    // 知识索引是派生缓存、模型配置以桌面库为准，两句话必须在文档里
    expect(section).toContain('派生缓存');
    expect(section).toContain('model_configs');
  });

  it('6.5 写入了模型配置与账号库的单一事实来源规则', () => {
    const section = prd().split('### 6.5 存储归属')[1].split('\n## 7')[0];
    expect(section).toContain('TT_ENGINE_MODEL_STORE');
    expect(section).toContain('MODEL_STORE_DESKTOP_OWNED');
    expect(section).toContain('auth-server');
  });

  it('6.2/6.5 写清项目维度与长期记忆库的归属口径', () => {
    const section62 = prd().split('### 6.2 本地数据')[1].split('### 6.3')[0];
    const section65 = prd().split('### 6.5 存储归属')[1].split('\n## 7')[0];
    expect(section62).toContain('workspace_id');
    expect(section62).toContain('workspace:<id>');
    // 检查点不是记忆：文档不能把它说成跨任务沉淀
    expect(section62).toContain('**不是**跨任务记忆');
    // 长期记忆库是 memories 表，文档必须与实际存储同口径
    expect(section62).toContain('memories');
    expect(section62).toContain('`scope` + `workspace_id`');
    expect(section65).toContain('meta.json.scope');
    expect(section65).toContain('memory_candidate');
    expect(section65).not.toContain('不新增第二份记忆库');
  });

  it('6.2/6.5 写清第二阶段的待审、软退休与导出导入路径', () => {
    const section62 = prd().split('### 6.2 本地数据')[1].split('### 6.3')[0];
    const section65 = prd().split('### 6.5 存储归属')[1].split('\n## 7')[0];
    // 待审队列与冲突软退休是召回口径的一部分，必须和存储层同口径
    expect(section62).toContain('pending');
    expect(section62).toContain('superseded_by');
    expect(section62).toContain('`subject`');
    expect(section62).toContain('实体页');
    // 导出 / 备份路径必须写真实目录，排障才不会去错地方
    expect(section65).toContain('memory_candidates');
    expect(section65).toContain('memory_save');
    expect(section65).toContain('{TONGTIAN_HOME}/exports/memories-');
    expect(section65).toContain('{TONGTIAN_HOME}/backups/');
    expect(section65).toContain('tongtian.db');
    expect(section65).toContain('memory_search');
  });

  it('6.2/6.5 写清第三阶段的模型抽取开关、文档对账与迁移包', () => {
    const section62 = prd().split('### 6.2 本地数据')[1].split('### 6.3')[0];
    const section65 = prd().split('### 6.5 存储归属')[1].split('\n## 7')[0];
    // 模型抽取默认关闭、受隐私与自动记忆门控，文档必须写清代价与外发面
    expect(section62).toContain('memoryModelExtract');
    expect(section62).toContain('模型辅助抽取');
    expect(section62).toContain('MEMORY.md');
    expect(section65).toContain('modelExtract');
    expect(section65).toContain('20s 超时');
    // 迁移包必须写真实扩展名、校验方式与落盘路径
    expect(section65).toContain('tongtian.hmpkg');
    expect(section65).toContain('.hmpkg');
    expect(section65).toContain('sha256');
    expect(section65).toContain('{TONGTIAN_HOME}/exports/memories-');
    expect(section65).toContain('MEMORY_DESKTOP_ONLY');
  });

  it('集成自测脚本的真实位置写进缺陷文档', () => {
    const defect = readFileSync(dir('../../../../docs/缺陷与实现计划-任我行.md'), 'utf8');
    // 实际目录是 tongtian-tauri/tests/integration，文档里写 engine/tests/... 会指到不存在的路径
    expect(defect).not.toContain('engine/tests/integration/');
    expect(defect).toContain('tests/integration/engine_d02_service_selftest.py');
  });
});
