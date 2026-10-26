/**
 * 多专家运行时契约测试（任务 #76 / #81 / #82）
 *
 * 锁住「选专家 → 提交 → Rust 受理 → 引擎按专家执行」这条链，防止出现
 * 「页面能选专家、提交时被静默丢掉」的假承诺：
 * - 对话内选择寄存在 store（欢迎页与对话态 ChatInput 重挂不丢）
 * - 三个通道的提交体都带 expertId；桌面端只传注册表 id，密钥仍留在 Rust
 * - 专家注册表只在有权威存储的通道可用；浏览器/Mock 明确拒绝而不是编假专家
 * - 引擎把 runtime.experts 当注册表本体读取，缺失/删除/模型不可用都失败关闭
 * - 专家绑定模型优先于对话模型；子 Agent 未绑定时沿用本轮对话模型
 * - 检查点不写专家密钥，恢复时如实说明无法重建
 */

import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const chatInput = () => read('../../../src/renderer/components/ChatInput/index.tsx');
const workbench = () => read('../../../src/renderer/pages/Workbench/index.tsx');
const expertsPage = () => read('../../../src/renderer/pages/Experts/index.tsx');
const store = () => read('../../../src/renderer/store/workbenchStore.ts');
const api = () => read('../../../src/renderer/lib/api.ts');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const engineApi = () => read('../../../src/renderer/lib/engine-api.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const contracts = () => read('../../../src/shared/contracts.ts');
const sharedTypes = () => read('../../../src/shared/types.ts');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const rustAgent = () => read('../../../src-tauri/src/bridge/agent.rs');
const engineServer = () => read('../../../engine/server.py');

// ── 对话内选择透传 ──────────────────────────────────────────────

describe('对话内专家选择一路透传', () => {
  it('ChatInput 把专家选择寄存 store，发送时作为第 6 项交给页面', () => {
    const input = chatInput();
    expect(input).toContain('const chatExpertId = useWorkbenchStore((s) => s.chatExpertId);');
    expect(input).toContain('const setChatExpertId = useWorkbenchStore((s) => s.setChatExpertId);');
    expect(input).toContain('chatExpertId || undefined,');
    expect(input).toContain(
      '}, [value, loading, staging, attachments, referencedSkill, selectedModel, onSend, activeMode, chatExpertId]);',
    );
    // 清理失效选择：专家被删后不能带着旧 id 提交出必然失败的任务
    expect(input).toContain('if (current && !list.some((e) => e.id === current)) setChatExpertId(\'\');');
    // 绑定模型不可用的专家在菜单里禁用并说明原因
    expect(input).toContain('disabled={Boolean(e.modelError)}');
  });

  it('Workbench 把 expertId 作为第 7 项交给 submitGoal', () => {
    const page = workbench();
    expect(page).toContain('expertId?: string,');
    expect(page).toContain(
      'submitGoal(text, attachments, modelBinding, workDir || undefined, modelId, mode, expertId);',
    );
  });

  it('store 提交体带 expertId，并按轮记录专家供重新生成回放', () => {
    const src = store();
    expect(src).toContain('expertId: expertId?.trim() || undefined,');
    expect(src).toContain('[turnIndex]: expertId?.trim() || \'\',');
    expect(src).toContain('const expertId = conv.turnExperts[turnIdx] || undefined;');
    // 重新生成提交体里确实带上了当轮专家
    const regen = src.split('regenerateTurn: async (')[1].split('resumeTask: async (')[0];
    expect(regen).toContain('expertId,');
    // 选择本身也放 store：欢迎页 → 对话态组件重挂时不能丢
    expect(src).toContain("chatExpertId: '',");
    expect(src).toContain('setChatExpertId: (id: string) => set({ chatExpertId: id }),');
    const types = sharedTypes();
    const req = types.split('export interface ChatSubmitReq')[1].split('\n}')[0];
    expect(req).toContain('expertId?: string;');
  });

  it('桌面通道把 expertId 传给 chat_submit；Rust 只接收 id 并先校验注册表', () => {
    const body = tauriApi().split("invoke('chat_submit'")[1].split("}) as Promise<{ taskId: string }>")[0];
    expect(body).toContain('expertId: req.expertId,');
    const rust = rustLib();
    const fn = rust.split('async fn chat_submit(')[1].split(') -> Result<Value, String>')[0];
    expect(fn).toContain('expert_id: Option<String>');
    // 受理阶段失败关闭：不存在/绑定模型不可用都不留下幽灵任务
    expect(rust).toContain('if expert::get(&p0.store, &id)?.is_none() {');
    expect(rust).toContain('EXPERT_NOT_FOUND: 负责人专家 {id} 不存在，请刷新专家团队');
    expect(rust).toContain('expert::lead_model_config(&p0.store, &id)?;');
    const agent = rustAgent();
    expect(agent).toContain('pub expert_id: Option<String>,');
    expect(agent).toContain('if let Some(eid) = &self.expert_id {');
    expect(agent).toContain('map.insert("expertId".into(), json!(eid));');
  });

  it('浏览器直连把 expertId 写进 /api/chat 请求体（没有注册表时由引擎明确拒绝）', () => {
    const body = engineApi().split("postSSE('/api/chat', {")[1].split('});')[0];
    expect(body).toContain('expertId: req.expertId,');
    expect(engineApi()).toContain('expertId?: string;');
  });
});

// ── 注册表通道边界 ──────────────────────────────────────────────

describe('专家注册表只在有权威存储的通道可用', () => {
  it('Tauri 通道把 experts:list/save/delete 映射到三个 command', () => {
    const src = tauriApi();
    expect(src).toContain("if (channel === 'experts:list') return invoke('experts_list');");
    expect(src).toContain("if (channel === 'experts:save') return invoke('experts_save', { req: args[0] ?? {} });");
    expect(src).toContain("return invoke('experts_delete', {");
    const apiSrc = api();
    expect(apiSrc).toContain('getAPI().invoke(IPC_CHANNELS.EXPERTS_LIST)');
    expect(apiSrc).toContain('getAPI().invoke(IPC_CHANNELS.EXPERTS_SAVE, req)');
    expect(apiSrc).toContain('getAPI().invoke(IPC_CHANNELS.EXPERTS_DELETE, { expertId })');
  });

  it('浏览器直连与 Mock 通道都明确拒绝 experts:*，不编假专家', () => {
    for (const src of [engineApi(), mockApi()]) {
      expect(src).toContain("if (channel.startsWith('experts:')) {");
      expect(src).toContain('EXPERTS_UNSUPPORTED');
    }
    expect(mockApi()).toContain('Mock 模式没有专家注册表');
    expect(engineApi()).toContain('专家团队需要桌面版注册表');
  });

  it('ExpertInfo 不暴露密钥，绑定模型不可用时带原因', () => {
    const contract = contracts();
    const info = contract.split('export interface ExpertInfo')[1].split('\n}')[0];
    expect(info).toContain('modelError: string | null;');
    expect(info).not.toMatch(/apiKey\s*\??:/);
    expect(info).not.toContain('modelConfig');
    const save = contract.split('export interface ExpertSaveReq')[1].split('\n}')[0];
    expect(save).toContain('modelId?: string | null;');
    expect(save).not.toMatch(/apiKey\s*\??:/);
  });
});

// ── 专家团队页面 ────────────────────────────────────────────────

describe('专家团队页面', () => {
  it('专家团队并入专家套件页签，旧路由重定向且侧边栏不再单列', () => {
    const app = read('../../../src/renderer/App.tsx');
    expect(app).toContain('<Route path="/experts" element={<Navigate to="/expert-suites?tab=experts" replace />} />');
    expect(app).not.toContain("import('./pages/Experts')");
    const sidebar = read('../../../src/renderer/components/Layout/Sidebar.tsx');
    expect(sidebar).not.toContain("path: '/experts'");
    const suites = read('../../../src/renderer/pages/ExpertSuite/index.tsx');
    expect(suites).toContain("import ExpertsPage from '../Experts';");
    expect(suites).toContain("['experts', '专家团队']");
    expect(suites).toContain("return tab === 'installed' || tab === 'experts' ? tab : 'plaza';");
    expect(suites).toContain('changeView');
    expect(suites).toContain('<ExpertsPage embedded />');
    expect(expertsPage()).toContain('embedded?: boolean');
  });

  it('页面调用真实注册表 API；浏览器通道如实说明不可用并禁用写操作', () => {
    const src = expertsPage();
    expect(src).toContain('const desktop = isTauri();');
    expect(src).toContain('当前是浏览器直连通道，专家团队不可用');
    expect(src).toContain('disabled={!desktop}');
    expect(src).toContain('await expertsAPI.save({');
    expect(src).toContain('await expertsAPI.remove(deleteTarget.id);');
    // 模型/技能下拉来自真实清单，不是手填 id
    expect(src).toContain('modelAPI');
    expect(src).toContain('loadAllSkills()');
    expect(src).toContain('绑定模型不可用：${expert.modelError}');
    expect(src).toContain("return '沿用对话中选择的模型';");
    // 技能白名单按引擎认的 id 存，不能用展示名：否则运行时过滤会把技能全部漏掉
    expect(src).toContain('checked={selectedSkills.includes(skill.id)}');
    expect(src).toContain('onChange={() => toggleSkill(skill.id)}');
    expect(src).not.toContain('toggleSkill(skill.name)');
  });

  it('删除专家只动注册表；删掉当前选中项时同步清空对话内选择', () => {
    const src = expertsPage();
    expect(src).toContain('useWorkbenchStore.getState().setChatExpertId(\'\');');
    expect(src).toContain('删除只移除注册表条目');
  });
});

// ── 引擎执行面 ──────────────────────────────────────────────────

describe('引擎按专家执行', () => {
  it('runtime.experts 是注册表本体：三个消费点都先取 block 再归一化', () => {
    const src = engineServer();
    expect(src).toContain('def _experts_block(session: "TaskSession") -> Any:');
    expect(src).toContain('return runtime.get("experts")');
    // 负责人解析、执行流、/api/chat 受理三处都不能把整包 runtime 当注册表
    expect(src).toContain('experts, registry_error = er.normalize_registry(_experts_block(session))');
    expect(src).toContain('experts, experts_error = er.normalize_registry(_experts_block(session))');
    const chat = src.split('expert_ref = str(payload.get("expertId") or "").strip()')[1].split('attachments_raw')[0];
    expect(chat).toContain('er.normalize_registry(_experts_block(session))');
  });

  it('/api/chat 对缺失/不存在/模型不可用的专家明确报错，不静默退回通用助手', () => {
    const src = engineServer();
    expect(src).toContain('"code": "EXPERT_UNAVAILABLE"');
    expect(src).toContain('"code": "EXPERT_NOT_FOUND"');
    expect(src).toContain('本次请求没有携带专家注册表');
    expect(src).toContain('负责人专家 {expert_ref} 不存在，请刷新专家团队');
    // 新一轮没选专家时清空旧选择，身份不能跨轮延续
    expect(src).toContain('session.expert_id = None');
  });

  it('专家绑定模型优先于对话模型；子 Agent 未绑定沿用本轮对话模型', () => {
    const src = engineServer();
    expect(src).toContain('def _effective_model_settings(');
    expect(src).toContain('if expert_config:');
    const child = src.split('def _make_child_agent(')[1].split('subagent_runner = sar.SubagentRunner(')[0];
    expect(child).toContain('_effective_model_settings(');
    expect(child).toContain('model_binding, model_config, expert_config');
    // 不能继承负责人的绑定：这里只能出现本轮 model_config，不能出现 lead_config
    expect(child).not.toContain('lead_config');
  });

  it('检查点记录 expertId、剥离专家密钥，恢复时失败关闭', () => {
    const src = engineServer();
    expect(src).toContain('"expertId": session.expert_id or "",');
    expect(src).toContain('session.expert_id = str(context.get("expertId") or "").strip() or None');
    expect(src).toContain('clean["modelSecretsStripped"] = True');
    expect(src).toContain('_EXPERT_SECRET_STRIPPED_NOTE');
  });
});

// ── 行为：Mock 通道拒绝而不是假装有专家 ─────────────────────────

type WindowAPI = {
  tongtianAPI?: {
    invoke: (channel: string, ...args: unknown[]) => Promise<unknown>;
  };
};
(globalThis as Record<string, unknown>).window =
  (globalThis as Record<string, unknown>).window ?? {};
const win = globalThis.window as WindowAPI;

beforeAll(async () => {
  await import('../../../src/renderer/lib/mock-api');
});

describe('Mock 通道专家能力', () => {
  it('列出/保存/删除专家都拒绝并说明要去桌面版或引擎直连', async () => {
    await expect(win.tongtianAPI!.invoke('experts:list')).rejects.toThrow('EXPERTS_UNSUPPORTED');
    await expect(win.tongtianAPI!.invoke('experts:save', {})).rejects.toThrow('EXPERTS_UNSUPPORTED');
    await expect(win.tongtianAPI!.invoke('experts:delete', { expertId: 'x' })).rejects.toThrow(
      'EXPERTS_UNSUPPORTED',
    );
  });
});
