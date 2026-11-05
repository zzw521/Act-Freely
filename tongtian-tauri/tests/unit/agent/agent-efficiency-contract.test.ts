/**
 * 上下文与工具效率契约测试（P1-1）
 *
 * 这三项设置（单轮工具循环上限 / 上下文压缩 / 连接器工具按需载入）横跨
 * 设置页 → Rust settings_kv → runtime.agentPolicy → 引擎策略解析 → AgentScope 构造，
 * 任何一跳丢键都会退化成「控件能点、执行不变」。这里用源码字面量把链路钉住；
 * 引擎侧的真实行为由 tests/integration/engine_efficiency_selftest.py 验证。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const contracts = () => read('../../../src/shared/contracts.ts');
const sharedTypes = () => read('../../../src/shared/types.ts');
const alignment = () => read('../../../src/renderer/lib/alignment-api.ts');
const engineApi = () => read('../../../src/renderer/lib/engine-api.ts');
const store = () => read('../../../src/renderer/store/workbenchStore.ts');
const settingsPage = () => read('../../../src/renderer/pages/Settings/index.tsx');
const rustRuntime = () => read('../../../src-tauri/src/bridge/runtime.rs');
const engineServer = () => read('../../../engine/server.py');
const engineBudget = () => read('../../../engine/context_budget.py');
const engineSearch = () => read('../../../engine/tool_search.py');
const engineMcp = () => read('../../../engine/mcp_runtime.py');
const engineCheckpoint = () => read('../../../engine/checkpoint_runtime.py');
const requirements = () => read('../../../engine/requirements.txt');

describe('设置项三端同名字段', () => {
  it('AppSettingsData 与默认值都包含三项，缺省口径与引擎一致', () => {
    for (const key of ['agentMaxIters', 'agentAutoCompress', 'agentLazyMcpTools']) {
      expect(contracts()).toContain(key);
      expect(alignment()).toContain(key);
    }
    expect(contracts()).toContain('agentMaxIters: number');
    expect(contracts()).toContain('agentAutoCompress: boolean');
    expect(contracts()).toContain('agentLazyMcpTools: boolean');
    expect(alignment()).toContain('agentMaxIters: 8');
    expect(alignment()).toContain('agentAutoCompress: true');
    expect(alignment()).toContain('agentLazyMcpTools: true');
  });

  it('设置页把三项接到真实控件（不是只展示说明）', () => {
    const page = settingsPage();
    expect(page).toContain('updateAppSettings({ agentMaxIters:');
    expect(page).toContain('updateAppSettings({ agentAutoCompress:');
    expect(page).toContain('updateAppSettings({ agentLazyMcpTools:');
    expect(page).toContain('上下文与工具效率');
    // 说明必须写清代价，不做「开了就变快」的承诺
    expect(page).toContain('额外调用一次模型');
    expect(page).toContain('search_tools');
  });
});

describe('提交链路：浏览器直连与桌面端都下发策略', () => {
  it('ChatSubmitReq 有 agentPolicy 类型，引擎直连把它放进请求体', () => {
    expect(sharedTypes()).toContain('export interface AgentPolicy');
    expect(sharedTypes()).toContain('agentPolicy?: AgentPolicy');
    expect(engineApi()).toContain('agentPolicy: req.agentPolicy');
  });

  it('工作台提交与重新生成都带当前设置值', () => {
    const src = store();
    const hits = src.split('agentPolicy: {').length - 1;
    expect(hits).toBeGreaterThanOrEqual(2);
    expect(src).toContain('maxIters: settings?.agentMaxIters');
    expect(src).toContain('autoCompress: settings?.agentAutoCompress');
    expect(src).toContain('lazyMcpTools: settings?.agentLazyMcpTools');
  });

  it('Rust 从 settings_kv 读三项并随 runtime 下发', () => {
    const src = rustRuntime();
    expect(src).toContain('"agentMaxIters"');
    expect(src).toContain('"agentAutoCompress"');
    expect(src).toContain('"agentLazyMcpTools"');
    expect(src).toContain('"agentPolicy": agent_policy_block(store)');
    // 越界不能静默吞掉：归一结果要带 notes
    expect(src).toContain('block["notes"] = json!(notes)');
  });

  it('引擎接受顶层 agentPolicy，但不覆盖 runtime 里的权威值', () => {
    const src = engineServer();
    expect(src).toContain('agent_policy_raw = payload.get("agentPolicy")');
    expect(src).toContain('if not isinstance(rt.get("agentPolicy"), dict)');
  });
});

describe('本地 harness 同口径压缩（D-40 已补齐）', () => {
  const rustCompress = () => read('../../../src-tauri/src/harness/compress.rs');
  const rustRunner = () => read('../../../src-tauri/src/harness/runner.rs');
  const rustModel = () => read('../../../src-tauri/src/harness/model.rs');
  const rustCheckpoint = () => read('../../../src-tauri/src/harness/checkpoint.rs');
  const engineSelftest = () => read('../../integration/engine_efficiency_selftest.py');

  it('动态预留常量与引擎逐字对应（两侧不许各自漂移）', () => {
    const py = engineBudget();
    const rs = rustCompress();
    expect(py).toContain('COMPRESSION_RESERVE_FRACTION = 0.05');
    expect(py).toContain('COMPRESSION_RESERVE_MIN_TOKENS = 4096');
    expect(rs).toContain('pub const RESERVE_FRACTION: f64 = 0.05;');
    expect(rs).toContain('pub const RESERVE_MIN_TOKENS: u64 = 4096;');
    // 同一个百万窗口：Python 自测与 Rust 单测必须算出同一个触发点
    expect(py).toContain('window - max(');
    expect(rs).toContain('saturating_sub(reserve_of(effective))');
    expect(engineSelftest()).toContain('950_000');
    expect(rs).toContain('950_000');
    // 转移链按最小窗口：两侧都要有这条约束
    expect(rs).toContain('min_fallback_window');
    expect(py).toContain('def fallback_window');
  });

  it('设置开关、模型窗口与 runtime 真的进 harness', () => {
    const rs = rustCompress();
    expect(rs).toContain('.get("agentPolicy")');
    expect(rs).toContain('.get("autoCompress")');
    expect(rs).toContain('model_config.get("contextWindow")');
    const runner = rustRunner();
    expect(runner).toContain('compress::policy(&snap.runtime, &snap.model_config)');
    expect(runner).toContain('compress::estimate_tokens(&messages, cp.last_prompt_tokens)');
    expect(runner).toContain('compress::apply_summary(&messages');
    // 计数优先用端点回报的 prompt_tokens，缺了才粗估，且口径要写进说明
    expect(runner).toContain('cp.last_prompt_tokens = out.prompt_tokens');
    expect(rs).toContain('上一次调用回报的真实 prompt_tokens');
    expect(rs).toContain('字符粗估（端点未回报 usage，未计工具 schema）');
  });

  it('摘要调用不带工具，失败只说明不中断', () => {
    const model = rustModel();
    expect(model).toContain('pub async fn oneshot(');
    expect(model).toContain('call_once(ep, messages, &[], cancel).await');
    const runner = rustRunner();
    expect(runner).toContain('model::oneshot(');
    expect(runner).toContain('.ok()');
    expect(runner).toContain('本轮未能完成压缩');
    // 压缩那一次调用也要计量，不能白烧 token 不记账
    expect(runner).toContain('em.usage(&outcome).await');
  });

  it('压缩不拆散工具配对，且检查点带得走计数', () => {
    const rs = rustCompress();
    expect(rs).toContain('has_tool_calls(&messages[groups[last].0])');
    expect(rs).toContain('KEEP_RECENT_TURNS: usize = 3');
    // 摘要只有一份：再次压缩会把旧摘要并入
    expect(rs).toContain('SUMMARY_MARK');
    const cp = rustCheckpoint();
    expect(cp).toContain('pub last_prompt_tokens: u64');
    expect(cp).toContain('pub compressed_messages: u64');
    expect(cp).toContain('"compressedMessages"');
  });
});

describe('引擎执行事实：策略真的进 ReActAgent', () => {
  it('迭代上限与压缩配置来自策略对象', () => {
    const src = engineServer();
    // 故障转移链里窗口更小时，策略按最小窗口计算；其余情况 policy_config 就是 model_config
    expect(src).toContain('policy = cb.resolve(runtime, policy_config)');
    expect(src).toContain('fallback_window = cb.fallback_window(model_config)');
    expect(src).toContain('policy_config = {**model_config, "contextWindow": fallback_window}');
    expect(src).toContain('max_iters=policy.max_iters');
    expect(src).toContain('compression_config=compression');
    expect(src).toContain('compression, compression_error = cb.build_compression(policy, model_name)');
  });

  it('压缩异常降级为可见说明，不中断任务', () => {
    const src = engineServer();
    expect(src).toContain('agent._tt_compression_error');
    expect(src).toContain('agent.compression_config.enable = False');
    expect(src).toContain('ctx-compress-error');
  });

  it('MCP 工具按需载入：搜索工具与 lazy 装配同进同出，且按工具粒度加载', () => {
    const server = engineServer();
    expect(server).toContain('tsr.install(toolkit)');
    expect(server).toContain('sys_prompt += tsr.system_hint(mcp_count, mcp_tools)');
    expect(server).toContain('lazy=bool(getattr(agent, "_tt_lazy_mcp", False))');
    // 是否按需由 Rust 探针回传的工具清单决定，提示词、说明文案、实际注册同一口径
    expect(server).toContain('mcp_tools = tsr.declared_tool_count(runtime)');
    expect(server).toContain('lazy_mcp = tsr.should_lazy(runtime, policy.lazy_mcp_tools)');
    const mcp = engineMcp();
    expect(mcp).toContain('group_name = ts.ensure_group(toolkit, spec, index)');
    expect(mcp).toContain('group_name=group_name');
    const search = engineSearch();
    expect(search).toContain('active=False');
    // 只把检索命中的工具移进「已载入」组；整组一次性激活的旧口径必须消失
    expect(search).toContain('def equip(toolkit, names: list[str]) -> list[str]:');
    expect(search).toContain('tool.group = EQUIPPED_GROUP');
    expect(search).toContain('MAX_ACTIVATED_TOOLS = 6');
    expect(search).not.toContain('update_tool_groups(to_activate, active=True)');
    // 未激活分组的错误提示会点名 reset_equipped_tools：该工具必须真实存在
    expect(search).toContain('reset_equipped_tools');
    // 分组名要能被 AgentScope 做成 pydantic 字段（不能带冒号/连字符）
    expect(search).toContain('_UNSAFE_GROUP_CHARS');
  });

  it('token 计数包装真的继承 TokenCounterBase（pydantic 校验所需）', () => {
    const src = engineBudget();
    expect(src).toContain('class SafeTokenCounter(TokenCounterBase)');
    expect(src).toContain('return await self._inner.count(messages, tools=tools, **kwargs)');
    expect(src).toContain('self.last_error');
    // 依赖显式声明，缺包时压缩不可用会如实说明
    expect(requirements()).toContain('tiktoken==0.12.0');
  });

  it('检查点快照排除被压缩原文，恢复时上下文不翻倍', () => {
    expect(engineCheckpoint()).toContain('get_memory(exclude_mark="compressed")');
    expect(engineCheckpoint()).toContain('except TypeError:');
  });
});

describe('线程内换模型不丢历史（D-40 第 3 项）', () => {
  const rustProvider = () => read('../../../src-tauri/src/harness/provider.rs');
  const rustPrompt = () => read('../../../src-tauri/src/harness/prompt.rs');
  const rustLib = () => read('../../../src-tauri/src/lib.rs');
  const rustRunner = () => read('../../../src-tauri/src/harness/runner.rs');
  const rustModel = () => read('../../../src-tauri/src/harness/model.rs');
  const rustCp = () => read('../../../src-tauri/src/harness/checkpoint.rs');
  const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
  const workbench = () => read('../../../src/renderer/pages/Workbench/index.tsx');

  it('历史带上口径与引擎逐字一致（最近 20 条 / 单条 600 字 / 最多 40 条）', () => {
    const py = engineServer();
    const provider = rustProvider();
    const prompt = rustPrompt();
    expect(py).toContain('for h in (history or [])[-20:]');
    expect(py).toContain('if len(content) > 600');
    expect(py).toContain('for item in raw[-40:]');
    expect(prompt).toContain('const MAX_HISTORY_ITEMS: usize = 20;');
    expect(prompt).toContain('const MAX_HISTORY_ITEM_CHARS: usize = 600;');
    expect(provider).toContain('pub const MAX_CANONICAL_HISTORY_ITEMS: usize = 40;');
    expect(provider).toContain('pub fn encode_history(');
    expect(prompt).toContain(
      'provider::encode_history(history, MAX_HISTORY_ITEMS, MAX_HISTORY_ITEM_CHARS)',
    );
    // 截断标记两侧同字：回放与压缩都认得同一份口径
    expect(provider).toContain('…（已截断）');
    expect(py).toContain('"…（已截断）"');
  });

  it('换模型一路传到执行面：previousModelId 不再被静默丢弃', () => {
    expect(sharedTypes()).toContain('previousModelId?: string');
    expect(tauriApi()).toContain('previousModelId: req.previousModelId');
    const lib = rustLib();
    expect(lib).toContain('previous_model_id: Option<String>');
    expect(lib).toContain('slot.set_previous_model(prev)');
    const src = store();
    expect(src).toContain('previousModelId: conv.turnModels[turnIndex - 1]?.modelId');
    expect(src).toContain('previousModelId: conv.turnModels[turnIdx - 1]?.modelId');
    const runner = rustRunner();
    expect(runner).toContain('"harness-model-switch"');
    expect(runner).toContain('provider::switch_note(');
    expect(runner).toContain('provider::resume_switch_note(');
    // 历史被改写/没带上的部分逐条摊出来，不许静默消失
    expect(runner).toContain('prompt::history_encoding(&snap.history)');
    expect(runner).toContain('ctx-history-');
  });

  it('不猜模型能力：画像只取本机配置与端点自己的回答', () => {
    const provider = rustProvider();
    expect(provider).toContain('pub fn profile_of(cfg: &Value) -> Profile');
    // 有一方是环境变量兜底时不编造「切换」
    expect(provider).toContain('if !a.known || !b.known');
    expect(provider).toContain('pub fn validate_tools(specs: &[Value]) -> (Vec<Value>, Vec<String>)');
    expect(provider).toContain('pub const TOOL_NAME_MAX_CHARS: usize = 64;');
    expect(provider).toContain('pub fn no_tools_note(');
  });

  it('端点拒收工具：先跑完整条容灾链，退纯文本必须留下事实', () => {
    const model = rustModel();
    expect(model).toContain('fn tool_schema_rejection(err: &ModelError) -> Option<String>');
    expect(model).toContain('async fn call_degraded(');
    expect(model).toContain('out.tools_degraded = Some(');
    const runner = rustRunner();
    expect(runner).toContain('"harness-tools-degraded"');
    expect(runner).toContain('本轮没有执行任何文件读写');
    expect(runner).toContain('let (specs, mut schema_notes) = provider::validate_tools(&tools);');
    expect(runner).toContain('provider::no_tools_note(&profile)');
  });

  it('检查点记住产出它的模型，续跑入口据此说明', () => {
    const cp = rustCp();
    expect(cp).toContain('pub model_id: String');
    expect(cp).toContain('pub model_display: String');
    expect(cp).toContain('"producedByModel"');
    expect(cp).toContain('"producedByModelId"');
    expect(sharedTypes()).toContain('producedByModelId?: string');
    const page = workbench();
    expect(page).toContain('checkpointModelNote');
    expect(page).toContain('checkpoint?.producedByModelId');
  });
});

describe('检查点按执行体分目录（D-40 第 4 项）', () => {
  const rustCp = () => read('../../../src-tauri/src/harness/checkpoint.rs');
  const rustLib = () => read('../../../src-tauri/src/lib.rs');
  const rustRunner = () => read('../../../src-tauri/src/harness/runner.rs');
  const workbench = () => read('../../../src/renderer/pages/Workbench/index.tsx');

  it('目录布局：agents/<scope>/<taskId 哈希>.json，id 不许变成路径', () => {
    const cp = rustCp();
    expect(cp).toContain('pub const AGENTS_DIR: &str = "agents";');
    expect(cp).toContain('pub const DEFAULT_SCOPE: &str = "leader";');
    expect(cp).toContain('pub const MEMBER_PREFIX: &str = "member";');
    expect(cp).toContain('pub fn dir_in(base: &Path, scope: &str) -> PathBuf');
    expect(cp).toContain('base.join(AGENTS_DIR).join(sanitize_scope(scope))');
    expect(cp).toContain('pub fn file_in(base: &Path, scope: &str, task_id: &str) -> PathBuf');
    // 文件名仍只由 taskId 的散列决定：执行体的区分体现在目录，两个执行体跑同一任务各存一份
    expect(cp).toContain("format!(\"{:x}.json\", hasher.finalize())");
    // 消毒只留 ASCII 字母数字与 _ -，路径分隔符与 `..` 一律进不了目录名
    expect(cp).toContain('pub fn sanitize_scope(raw: &str) -> String');
    expect(cp).toContain('if ch.is_ascii_alphanumeric() || ch == \'_\' || ch == \'-\'');
    expect(cp).toContain('const SCOPE_MAX_CHARS: usize = 64;');
  });

  it('负责人与成员各有各的名字，中文 id 也不塌进同一个目录', () => {
    const cp = rustCp();
    expect(cp).toContain('pub fn leader_scope(expert_id: Option<&str>) -> String');
    expect(cp).toContain('pub fn member_scope(member_id: &str) -> String');
    // id 单独消毒后再拼前缀：整串消毒会把「leader-中文」压成「leader」，两个专家就撞了
    expect(cp).toContain('format!("{DEFAULT_SCOPE}-{}", scope_tag(id))');
    expect(cp).toContain('format!("{MEMBER_PREFIX}-{}", scope_tag(member_id))');
    expect(cp).toContain('fn scope_tag(raw: &str) -> String');
    expect(cp).toContain('fn short_hash(trimmed: &str) -> String');
  });

  it('成员快照隔离：按 scope 取，绝不串读；按任务恢复先取负责人族', () => {
    const cp = rustCp();
    expect(cp).toContain('pub fn load_scoped(base: &Path, scope: &str, task_id: &str) -> Option<Checkpoint>');
    expect(cp).toContain('pub fn scopes(base: &Path) -> Vec<String>');
    expect(cp).toContain('pub fn list_in(base: &Path, scope: &str) -> Vec<Checkpoint>');
    expect(cp).toContain('pub fn delete_in(base: &Path, scope: &str, task_id: &str)');
    expect(cp).toContain('fn is_leader_family(scope: &str) -> bool');
    expect(cp).toContain('found.sort_by(|a, b| is_leader_family(b).cmp(&is_leader_family(a)));');
    // 删负责人那份不能顺手删掉成员的：删除按 scope 走
    expect(cp).toContain('pub fn save_in(base: &Path, cp: &Checkpoint) -> Result<(), String>');
    expect(cp).toContain('data.agent_scope = cp.scope();');
  });

  it('升级前的扁平检查点仍读得到，并按自己的归属搬一次家', () => {
    const cp = rustCp();
    expect(cp).toContain('fn file_of(base: &Path, task_id: &str) -> PathBuf');
    const loader = cp.slice(cp.indexOf('pub fn load_in('), cp.indexOf('pub fn load(task_id'));
    expect(loader).toContain('let legacy = file_of(base, task_id);');
    expect(loader).toContain('if save_in(base, &migrated).is_ok()');
    expect(loader).toContain('delete_path(&legacy);');
    expect(cp).toContain('migrated.agent_scope = DEFAULT_SCOPE.to_string();');
  });

  it('归属随提交下发、随运行盖章，且不覆盖盘上恢复的快照', () => {
    const lib = rustLib();
    expect(lib).toContain('fn stamp_expert(runtime: &mut Value, expert_id: Option<&str>)');
    expect(lib).toContain('stamp_expert(&mut runtime, expert_id.as_deref());');
    const runner = rustRunner();
    expect(runner).toContain('cp.agent_scope = agent_scope_for(&cp.agent_scope, &snap.runtime);');
    expect(runner).toContain('fn agent_scope_for(cp_scope: &str, runtime: &Value) -> String');
    expect(runner).toContain('if !cp_scope.trim().is_empty()');
    expect(runner).toContain('checkpoint::leader_scope(');
    expect(runner).toContain('.get("expertId")');
    // 恢复入口只按任务找，不重读此刻的专家选择：续跑沿用中断前那个执行体的目录
    expect(lib).toContain('harness::checkpoint::load(task_id)');
  });

  it('界面上说得出这份快照是谁的：agentScope 一路到续跑入口', () => {
    const cp = rustCp();
    expect(cp).toContain('"agentScope": self.scope()');
    expect(sharedTypes()).toContain('agentScope?: string');
    const page = workbench();
    expect(page).toContain('checkpointOwnerNote');
    expect(page).toContain("checkpoint?.agentScope ?? ''");
    expect(page).toContain("if (!owner || owner === 'leader') return '';");
  });
});
