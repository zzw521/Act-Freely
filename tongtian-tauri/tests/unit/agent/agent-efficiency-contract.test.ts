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

  it('MCP 工具按需载入：搜索工具与 lazy 装配同进同出', () => {
    const server = engineServer();
    expect(server).toContain('tsr.install(toolkit)');
    expect(server).toContain('sys_prompt += tsr.system_hint(mcp_count)');
    expect(server).toContain('lazy=bool(getattr(agent, "_tt_lazy_mcp", False))');
    const mcp = engineMcp();
    expect(mcp).toContain('group_name = ts.ensure_group(toolkit, spec, index)');
    expect(mcp).toContain('group_name=group_name');
    const search = engineSearch();
    expect(search).toContain('active=False');
    expect(search).toContain('toolkit.update_tool_groups(to_activate, active=True)');
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
