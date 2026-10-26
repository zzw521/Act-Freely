// tests/unit/bridge/runtime-contract.test.ts — 执行面输入契约（能力台账 P0-1/P0-2）
//
// 「安装技能 / 改意识 / 传附件」过去只落到 SQLite 和磁盘，引擎收到的仍是同一句裸提问。
// 这条链跨三端，字段名一跳丢一跳就退化成假生效，所以用源码字面量把三端钉在一起：
//   渲染层附件对象 → Rust `bridge/runtime.rs` 装配 → 引擎 `runtime_context.py` 消费
//   → `engine/server.py` 进 prompt / 进用户消息 / 注册 use_skill
// 引擎侧的实收内容由 tests/integration/engine_runtime_selftest.py 用假模型验证。

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const rustRuntime = () => read('../../../src-tauri/src/bridge/runtime.rs');
const rustAgent = () => read('../../../src-tauri/src/bridge/agent.rs');
const rustBridgeMod = () => read('../../../src-tauri/src/bridge/mod.rs');
const rustSkill = () => read('../../../src-tauri/src/skill/mod.rs');
const rustSuite = () => read('../../../src-tauri/src/suite/mod.rs');
const rustMcp = () => read('../../../src-tauri/src/mcp/mod.rs');
const engineCtx = () => read('../../../engine/runtime_context.py');
const engineMcp = () => read('../../../engine/mcp_runtime.py');
const engineServer = () => read('../../../engine/server.py');
const chatInput = () => read('../../../src/renderer/components/ChatInput/index.tsx');
const sharedTypes = () => read('../../../src/shared/types.ts');

describe('装配端：Rust 把磁盘事实打包成 runtime + attachments', () => {
  it('提交时真的装配并塞进请求体（不是只存库）', () => {
    const agent = rustAgent();
    expect(rustBridgeMod()).toContain('pub mod runtime;');
    // 组装 runtime 时必须带上本次目标 workspace：沙箱策略与文件边界按 workspace 取
    expect(agent).toContain('super::runtime::build(store.as_deref(), &ws, &raw)');
    expect(agent).toContain('map.insert("runtime".into(), rt.clone());');
    expect(agent).toContain('overrides.runtime = Some(bundle.runtime);');
    expect(agent).toContain('stream_chat(app, task_id, goal, port, bundle.attachments, overrides, p0)');
    // 附件读盘是阻塞 IO，必须在 spawn_blocking 里做，不能占住 async 线程
    expect(agent).toContain('tokio::task::spawn_blocking');
  });

  it('runtime 带意识与启用技能，技能带正文（引擎拿不到 SKILL.md 路径）', () => {
    const src = rustRuntime();
    expect(src).toContain('"awareness": awareness_block(awareness_dir)');
    expect(src).toContain('let (skills, suite_notes) = skills_block(skills_base, store);');
    expect(src).toContain('"skills": skills');
    expect(src).toContain('skill::enabled_with_body(skills_base, &disabled)');
    for (const key of ['"id"', '"name"', '"description"', '"content"']) {
      expect(src, `技能条目缺 ${key}`).toContain(key);
    }
    expect(rustSkill()).toContain('pub fn enabled_with_body(base: &Path, disabled: &[String]) -> Vec<Value>');
    expect(rustSkill()).toContain('obj.insert("content".to_string(), Value::String(body));');
  });

  it('套件指令正文进入 runtime.skills，与技能同路消费', () => {
    const src = rustRuntime();
    expect(src).toContain('suite::runtime_commands(skills_base)');
    expect(src).toContain('"suiteNotes": suite_notes');
    expect(src).toContain('"kind": "skill"');
    // 已安装技能优先：同名套件指令跳过并留说明
    expect(src).toContain('本轮以已安装技能为准');

    const suite = rustSuite();
    expect(suite).toContain('pub fn runtime_commands(base: &Path) -> RuntimeCommands');
    expect(suite).toContain('"kind": "command"');
    expect(suite).toContain('&["content", "prompt", "body", "instructions"]');
    expect(suite).toContain('format!("commands/{segment}.md")');
    expect(suite).toContain('format!("prompts/{segment}.md")');
    // 显式 file 必须过 safe_relative，不能让套件包借正文路径读穿数据目录
    expect(suite).toContain('safe_relative(raw_file)');
  });

  it('意识读取失败带原因，禁用清单来自 SQLite 而不是引擎自己猜', () => {
    const src = rustRuntime();
    expect(src).toContain('json!({ "error": e })');
    expect(src).toContain('store.map(skill::disabled_ids).unwrap_or_default()');
  });

  it('附件正文抽取覆盖文本/容器/图片：图片原样 base64 下发，能力判定留给引擎', () => {
    const src = rustRuntime();
    expect(src).toContain('office::extract(&mime, &bytes)');
    expect(src).toContain('service::mime_of(&path)');
    expect(src).toContain('json!({ "text": cap_body(text) })');
    expect(src).toContain('json!({ "text": cap_body(ex.content) })');
    expect(src).toContain('base64::engine::general_purpose::STANDARD.encode(&bytes)');
    expect(src).toContain('"data": data');
    expect(src).toContain('能否解析取决于当前模型通道的视觉能力');
  });

  it('静默少给被禁止：每种失败都有专属文案', () => {
    const src = rustRuntime();
    for (const msg of [
      '未提供暂存路径',
      '暂存文件读不到',
      '暂存路径不是一个文件',
      '正文未进入模型',
      '附件内容为空',
      '正文抽取为空',
      '正文过长',
    ]) {
      expect(src, `缺失败文案：${msg}`).toContain(msg);
    }
  });
});

describe('消费端：引擎按同一套字段名取值', () => {
  it('runtime 的 awareness/skills 键名与 Rust 一致', () => {
    const ctx = engineCtx();
    expect(ctx).toContain('get("awareness")');
    expect(ctx).toContain('get("skills")');
    expect(ctx).toContain('block.get("mode")');
    expect(ctx).toContain('block.get("files")');
    for (const key of ['"id"', '"name"', '"description"', '"content"']) {
      expect(ctx, `技能条目没读 ${key}`).toContain(`item.get(${key})`);
    }
    // 套件指令带 kind/source/error，引擎据此区分措辞并摊出缺正文原因
    for (const key of ['"kind"', '"source"', '"error"']) {
      expect(ctx, `条目没读 ${key}`).toContain(`item.get(${key})`);
    }
    expect(ctx).toContain('get("suiteNotes")');
  });

  it('attachments 的 fileName/text/error/note 键名与 Rust 一致', () => {
    const ctx = engineCtx();
    for (const key of ['fileName', 'error', 'text', 'note']) {
      expect(ctx, `附件没读 ${key}`).toContain(`get("${key}")`);
    }
  });

  it('/api/chat 收下 runtime 与 attachments 并存进会话态（approve 重跑沿用）', () => {
    const src = engineServer();
    expect(src).toContain('payload.get("runtime")');
    expect(src).toContain('session.runtime = runtime_raw');
    expect(src).toContain('payload.get("attachments")');
    expect(src).toContain('session.attachments = rc.normalize_attachments(attachments_raw)');
    expect(src).toContain('self.runtime: dict = {}');
    expect(src).toContain('self.attachments: list = []');
  });

  it('技能进 system prompt 并注册 use_skill；正文靠工具取，不整篇塞 prompt', () => {
    const src = engineServer();
    expect(src).toContain('def build_skill_tool(skills: list)');
    expect(src).toContain('toolkit.register_tool_function(build_skill_tool(skills))');
    expect(src).toContain('runtime=session.runtime');
    expect(src).toContain('rc.skill_body_of(skills, skill_id)');
    const ctx = engineCtx();
    expect(ctx).toContain('def compose_system_prompt');
    expect(ctx).toContain('def skill_body_of');
  });

  it('附件正文同时进 plan 阶段与执行阶段的用户消息', () => {
    const src = engineServer();
    expect(src).toContain('images_inlined=plan_vision');
    expect(src).toContain('images_inlined=vision');
    expect(src).toContain('goal, session.runtime, session.attachments, images_inlined=vision');
    expect(src).toContain('async def _generate_plan(goal: str, context: str = "", mode: str = "general",');
  });

  it('没能进入模型的材料作为步骤事件摊出来', () => {
    const src = engineServer();
    expect(src).toContain('"stepType": "context"');
    // 记忆按本轮目标检索，说明列表也要沿用同一 query，否则「本轮没注入」的播报会失真
    expect(src).toContain('rc.context_notes(session.runtime, goal)');
    // 事件名必须是既有白名单里的，否则 Rust 侧 adapt_to_chat_event 会整条丢掉
    const sse = read('../../../src-tauri/src/bridge/sse.rs');
    expect(sse).toContain('"step_start",');
    expect(sse).toContain('"step_done",');
  });
});

describe('MCP 连接器：配置事实 → 引擎工具面', () => {
  it('Rust 只下发握手验证过的连接器，并带上子进程需要的取值', () => {
    const mcp = rustMcp();
    expect(mcp).toContain('pub fn for_runtime(store: &Store) -> Result<Vec<Value>, String>');
    expect(mcp).toContain('.filter(|r| r.connected)');
    for (const key of ['"serverId"', '"transport"', '"command"', '"args"', '"url"', '"env"', '"headers"', '"timeoutSecs"', '"tools"']) {
      expect(mcp, `for_runtime 没下发 ${key}`).toContain(key);
    }
    // 列表页仍然只给键名：密钥不经 IPC 进 WebView
    expect(mcp).toContain('env_keys: json_keys(');

    const src = rustRuntime();
    expect(src).toContain('"mcp": mcp_block(store)');
    expect(src).toContain('mcp::for_runtime(s)');
  });

  it('引擎按同一套 camelCase 键名取值', () => {
    const src = engineMcp();
    expect(src).toContain('get("mcp")');
    for (const key of ['serverId', 'transport', 'command', 'args', 'env', 'url', 'headers', 'timeoutSecs', 'name']) {
      expect(src, `mcp_runtime 没读 ${key}`).toContain(`get("${key}")`);
    }
    expect(src).toContain('MAX_MCP_SERVERS');
  });

  it('工具真的注册进 Toolkit，并在同任务内收尾', () => {
    const src = engineMcp();
    expect(src).toContain('await toolkit.register_mcp_client(');
    expect(src).toContain('StdIOStatefulClient');
    expect(src).toContain('HttpStatelessClient');
    expect(src).toContain('await client.connect()');
    expect(src).toContain('{**os.environ, **env}');
    const server = engineServer();
    expect(server).toContain('mcp_result = await mr.attach(');
    // 按需载入开关必须随 Agent 事实传给连接器装配，不能只写进提示词
    expect(server).toContain('lazy=bool(getattr(agent, "_tt_lazy_mcp", False))');
    expect(server).toContain('mcp_clients = mcp_result["clients"]');
    expect(server).toContain('*mcp_result["notes"]');
    // anyio 的 cancel scope 要求在进入它的任务里退出
    expect(server).toContain('await mr.close_all(mcp_clients)');
  });

  it('连接器工具不污染「内置工具」清单（该端点仍只读内置注册表）', () => {
    const body = engineServer().split('def build_toolkit')[1].split('\ndef ')[0];
    expect(body).not.toContain('register_mcp_client');
    expect(engineServer()).toContain('toolkit = build_toolkit(WORK_DIR)');
    expect(engineServer()).toContain('"source": "mcp" if mcp else "builtin"');
  });
});

describe('渲染层：附件对象字段名与 Rust 取值对齐', () => {
  it('fileName 与 stagedPath 是跨端约定字段', () => {
    expect(sharedTypes()).toContain('stagedPath: string;');
    expect(chatInput()).toContain('{ fileName: att.fileName, stagedPath: att.srcPath');
    const src = rustRuntime();
    expect(src).toContain('.get("fileName")');
    expect(src).toContain('att.get("stagedPath")');
    expect(src).toContain('.get("mime")');
  });
});
