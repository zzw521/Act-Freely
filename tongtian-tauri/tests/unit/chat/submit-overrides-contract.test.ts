/**
 * 对话提交覆盖项契约测试（任务 #38）
 *
 * 桌面端曾在 `chat_submit` 入参处把「输入框选的模型档位 / 自定义模型 / 自选工作目录 /
 * 此前轮次历史」静默丢掉：控件在、选择不报错，但引擎收到的体里没有这些键，于是既不
 * 按所选模型调用、也不把产物落进所选目录、多轮上下文同样丢失。本文件从两侧锁定：
 * - 源码契约：渲染层三通道 → Rust command 签名 → 引擎请求体（camelCase）逐段都在
 * - 模型选择只传 id：端点与密钥由持有数据源的后端解析，渲染层不再携带 apiKey
 * - 行为契约：Mock 通道选中档位 / 已保存模型后，用量归属确实跟着选择走
 * - 诚实降级：所选工作目录不存在时拒绝提交（WORK_DIR_INVALID），而不是换个目录默默产出
 */

import { describe, it, expect, beforeEach, beforeAll, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ErrorCode } from '../../../src/shared/contracts';
import { errorMessageOf, errorTextOf } from '../../../src/shared/error-messages';
import { BUILTIN_CHAT_MODELS, chatModelSelectionOf } from '../../../src/renderer/lib/chat-models';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const rustLib = () => read('../../../src-tauri/src/lib.rs');
const rustAgent = () => read('../../../src-tauri/src/bridge/agent.rs');
const contracts = () => read('../../../src/shared/contracts.ts');
const sharedTypes = () => read('../../../src/shared/types.ts');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const engineApi = () => read('../../../src/renderer/lib/engine-api.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const store = () => read('../../../src/renderer/store/workbenchStore.ts');
const chatInput = () => read('../../../src/renderer/components/ChatInput/index.tsx');
const engineServer = () => read('../../../engine/server.py');

// 渲染层对外只传「档位绑定 + 已保存模型 id」，端点与密钥不再离开后端
// filePermission 也在这条链上：对话框选了档、引擎却收到空值，等于「每次询问」形同虚设
const OVERRIDES = ['modelBinding', 'modelId', 'workDir', 'history', 'filePermission'] as const;

// ── 渲染层：三通道都不得丢选择 ──────────────────────────────────

describe('渲染层提交入参透传', () => {
  it('页面选中的模型与工作目录一路进入 store.submitGoal（不是只点亮控件）', () => {
    const page = read('../../../src/renderer/pages/Workbench/index.tsx');
    expect(page).toContain(
      'submitGoal(text, attachments, modelBinding, workDir || undefined, modelId, mode, expertId)',
    );
    const input = chatInput();
    // 档位绑定传真实模型名；已保存模型只传 id，密钥留在数据源一侧
    expect(input).toContain('selectedModel.binding || undefined,');
    expect(input).toContain('selectedModel.savedId || undefined,');
    // 旧版 localStorage 模型台账已退役（设置页只做一次性迁移）
    expect(input).not.toContain('tt-custom-models');
  });

  it('模型选择寄存在 store，不挂在 ChatInput 实例 state 上', () => {
    // 欢迎页与对话态各挂一个 ChatInput：首轮发送后组件重挂，实例 state 会被无声重置回 Auto
    const input = chatInput();
    expect(input).toContain('useWorkbenchStore((s) => s.chatModelId)');
    expect(input).toContain('setChatModelId(m.id)');
    expect(input).not.toContain('useState<ModelOption>(');
    const st = store();
    expect(st).toContain("chatModelId: 'auto',");
    expect(st).toContain('setChatModelId: (id: string) => set({ chatModelId: id }),');
  });

  it('store 把五项组装成 ChatSubmitReq 交给通道（历史为空时不下发而非下发空值）', () => {
    const src = store();
    expect(src).toContain('modelBinding,');
    expect(src).toContain('modelId,');
    expect(src).toContain('workDir,');
    expect(src).toContain('history: history.length > 0 ? history : undefined,');
    expect(src).not.toContain('modelConfig,');
    // 合规判定读权威清单里的 baseUrl（非机密），不再从渲染层配置对象里取
    expect(src).toContain('await api.compliance.check(goal, await modelBaseUrl(modelId));');
    expect(src).toContain("import { chatModelSelectionOf, modelBaseUrl } from '../lib/chat-models';");
  });

  it('Tauri 通道把五项作为 chat_submit 参数传给 Rust', () => {
    const src = tauriApi();
    const invokeBlock = src.split("invoke('chat_submit'")[1].split("}) as Promise<{ taskId: string }>")[0];
    expect(invokeBlock).toBeDefined();
    for (const field of OVERRIDES) {
      expect(invokeBlock, `chat_submit 入参缺少 ${field}`).toContain(`${field}: req.${field},`);
    }
  });

  it('引擎直连通道把五项写进 /api/chat 请求体', () => {
    const src = engineApi();
    const body = src.split("postSSE('/api/chat', {")[1].split('});')[0];
    for (const field of OVERRIDES) {
      expect(body, `/api/chat 请求体缺少 ${field}`).toContain(`${field}: req.${field},`);
    }
  });

  it('工作台模式一路传到引擎：提交 → approve 执行都用同一个 mode', () => {
    const st = store();
    expect(st).toContain('mode,');
    expect(tauriApi()).toContain('mode: req.mode,');
    expect(engineApi()).toContain('mode: req.mode,');
    const engine = engineServer();
    expect(engine).toContain('session.mode = mode_raw');
    expect(engine).toContain('runtime=session.runtime, sandbox=sandbox, mode=session.mode');
  });

  it('Mock 通道按所选模型记账（浏览器直连演示态不谎报默认模型）', () => {
    const src = mockApi();
    expect(src).toContain('model: resolveMockModel(req?.modelBinding, req?.modelId),');
    expect(src).toContain('recordMockUsage(600, 300, task.model)');
    // 归属优先级必须与引擎同序：档位绑定 > 已保存模型 id > 默认
    const resolver = src.split('function resolveMockModel')[1].split('/** Mock 用量记账')[0];
    expect(resolver).toContain("const binding = (modelBinding ?? '').trim();");
    expect(resolver).toContain('const selectedId = (modelId ?? \'\').trim();');
    // id 找不到时如实报错，不拿清单首项冒充用户选择
    expect(resolver).toContain('MODEL_NOT_FOUND');
  });
});

// ── 重新生成沿用当轮模型 ────────────────────────────────────────

describe('重新生成沿用当轮模型', () => {
  it('提交时按轮记录模型选择，重新生成显式下发当轮选择', () => {
    const src = store();
    // 写入：turnIndex → 当轮 binding/id（Auto 记空对象，表示不覆盖后端默认）
    expect(src).toContain('turnModels: {');
    expect(src).toContain('[turnIndex]: {');
    expect(src).toContain('modelBinding: modelBinding?.trim() || undefined,');
    expect(src).toContain('modelId: modelId?.trim() || undefined,');
    const regen = src.split('regenerateTurn: async (')[1].split('resumeTaskFromStep: async (')[0];
    expect(regen).toContain('conv.turnModels[turnIdx] ?? chatModelSelectionOf(get().chatModelId)');
    expect(regen).toContain('modelBinding: model.modelBinding,');
    expect(regen).toContain('modelId: model.modelId,');
  });

  it('输入框与提交共用一份内置档位清单，映射到真实模型名', () => {
    expect(BUILTIN_CHAT_MODELS.map((m) => m.id)).toEqual([
      'auto',
      'qwen-max',
      'advanced',
      'basic',
      'economy',
    ]);
    expect(chatModelSelectionOf('auto')).toEqual({});
    expect(chatModelSelectionOf('')).toEqual({});
    expect(chatModelSelectionOf('economy')).toEqual({ modelBinding: 'qwen-flash' });
    expect(chatModelSelectionOf('saved-model-9')).toEqual({ modelId: 'saved-model-9' });
    const input = chatInput();
    expect(input).toContain('const BUILTIN_MODELS: ModelOption[] = BUILTIN_CHAT_MODELS;');
    // 避免界面自抄一份 binding 后与提交映射漂移
    expect(input).not.toContain("binding: 'qwen-flash'");
  });
});

// ── Rust 桥接：command 签名与引擎请求体 ─────────────────────────

describe('Rust 桥接层覆盖项装配', () => {
  it('chat_submit 接收 modelId 并在受理阶段解析成调用配置（解析失败直接拒绝提交）', () => {
    const fn = rustLib().split('async fn chat_submit(')[1].split(') -> Result<Value, String>')[0];
    for (const param of ['model_binding: Option<String>', 'model_id: Option<String>', 'work_dir: Option<String>', 'history: Option<Vec<Value>>']) {
      expect(fn, `chat_submit 缺少参数 ${param}`).toContain(param);
    }
    expect(fn).not.toContain('model_config: Option<Value>');
    // 收到参数只是前提，装配进 SubmitOverrides 并随提交下发动才算透传
    const body = rustLib().split('async fn chat_submit(')[1].split('\n}\n')[0];
    expect(body).toContain('let model_config = model::service::runtime_config(&p0.store, model_id.as_deref())?;');
    expect(body).toContain('let overrides = agent::SubmitOverrides {');
    expect(body).toContain('model_binding: model_binding.map(|s| s.trim().to_string()).filter(|s| !s.is_empty()),');
    expect(body).toContain('model_config,');
    expect(body).toContain('history: history.unwrap_or_default(),');
    expect(body).toContain('overrides,');
  });

  it('SubmitOverrides 只在「本轮选了」时写键，避免空值清空引擎会话设置', () => {
    const src = rustAgent();
    const apply = src.split('fn apply_to(&self, body: &mut Value)')[1].split('\n}\n')[0];
    for (const key of ['modelBinding', 'modelConfig', 'workDir', 'history']) {
      expect(apply).toContain(`map.insert("${key}".into()`);
    }
    expect(apply).toContain('if let Some(mb) = &self.model_binding');
    expect(apply).toContain('if !self.history.is_empty()');
  });

  it('请求体用引擎读取的 camelCase 键，secureEnv 仍随每次提交下发', () => {
    const body = rustAgent().split('fn chat_request_body(')[1].split('overrides.apply_to(&mut body);')[0];
    for (const key of ['"message"', '"task_id"', '"stage"', '"attachments"', '"secureEnv"']) {
      expect(body).toContain(key);
    }
    expect(body).toContain('"stage": "plan"');
    expect(rustAgent()).toContain('p0.secure_env,');
    expect(rustAgent()).toContain('p0.privacy,');
  });

  it('自选工作目录先校验存在性，拒绝提交而不是换个目录产出', () => {
    const src = rustLib();
    expect(src).toContain('fn validate_work_dir(raw: Option<&str>) -> Result<Option<std::path::PathBuf>, String>');
    expect(src).toContain('WORK_DIR_INVALID: 工作目录不存在或不是目录');
    expect(src).toContain('let work_dir = validate_work_dir(work_dir.as_deref())?;');
  });
});

// ── 契约类型与错误码 ────────────────────────────────────────────

describe('契约单一来源与错误码文案', () => {
  it('ChatSubmitReq 只在 shared/types 定义，contracts 转发（两份定义会漏字段）', () => {
    expect(sharedTypes()).toContain('export interface ChatSubmitReq');
    expect(contracts()).toContain("export type { ChatSubmitReq } from './types';");
    expect(contracts()).not.toContain('export interface ChatSubmitReq {');
    const def = sharedTypes().split('export interface ChatSubmitReq')[1].split('\n}')[0];
    for (const field of OVERRIDES) {
      expect(def, `ChatSubmitReq 缺少字段 ${field}`).toContain(`${field}?:`);
    }
    // 渲染层请求体不得再有 apiKey / modelConfig 字段（说明性注释不算字段）
    expect(def).not.toContain('modelConfig?:');
    expect(def).not.toMatch(/apiKey\s*\??:/);
  });

  it('三通道提交体都不携带 apiKey（旧 localStorage 模型台账已退役）', () => {
    const engineBody = engineApi().split("postSSE('/api/chat', {")[1].split('});')[0];
    expect(engineBody).toContain('modelId: req.modelId,');
    expect(engineBody).not.toMatch(/apiKey\s*[:=]/);
    const tauriBody = tauriApi().split("invoke('chat_submit'")[1].split('}) as Promise<{ taskId: string }>')[0];
    expect(tauriBody).not.toMatch(/apiKey\s*[:=]/);
    expect(tauriBody).not.toContain('modelConfig: req.modelConfig');
  });

  it('WORK_DIR_INVALID 已登记且有可读文案（不把裸码丢给用户）', () => {
    expect(ErrorCode.WORK_DIR_INVALID).toBe('WORK_DIR_INVALID');
    expect(contracts()).toContain("WORK_DIR_INVALID = 'WORK_DIR_INVALID'");
    expect(errorMessageOf('WORK_DIR_INVALID')).toContain('工作目录');
    const text = errorTextOf('WORK_DIR_INVALID: 工作目录不存在或不是目录：D:\\gone');
    expect(text).toContain('任务未提交');
    expect(text).toContain('D:\\gone');
  });
});

// ── 引擎侧消费点 ────────────────────────────────────────────────

describe('引擎读取覆盖项', () => {
  it('/api/chat 按 camelCase 读取四项并存进会话（approve 执行时沿用）', () => {
    const src = engineServer();
    expect(src).toContain('payload.get("modelBinding")');
    // 新契约：浏览器直连只传 modelId，由引擎从 models.json 解析；旧 modelConfig 仍兼容
    expect(src).toContain('payload.get("modelId")');
    expect(src).toContain('payload.get("modelConfig")');
    expect(src).toContain('payload.get("workDir")');
    expect(src).toContain('_normalize_history(payload.get("history"))');
    expect(src).toContain('self.history: list = []');
    // Auto（未选档位/id）按 models.json 的默认条目解析，且缺省不覆盖会话已有配置
    expect(src).toContain('selected, model_err = _models_find(None)');
    expect(src).toContain('session.model_config = model_config');
  });

  it('执行流的历史走 _effective_history（两阶段下 approve 不再重复带）', () => {
    const src = engineServer();
    const run = src.split('async def _run_execution')[1].split('\n@app.')[0];
    expect(run).toContain('history = _effective_history(session, history)');
    const review = src.split('async def chat_review')[1].split('\n@app.')[0];
    expect(review).toContain('session.model_binding, session.work_dir, None, session.model_config');
  });
});

// ── 文件访问档位：对话框 → 会话台账 → 引擎强制 ────────────────────

describe('文件访问档位（每次询问 / 自动审批 / 完全访问）', () => {
  const filePermLib = () => read('../../../src/renderer/lib/file-permission.ts');
  const sandboxRuntime = () => read('../../../engine/sandbox_runtime.py');

  it('档位表只有一份，三档顺序与文案同源', () => {
    const lib = filePermLib();
    expect(lib).toContain("id: 'ask_every_time'");
    expect(lib).toContain("id: 'auto_approve'");
    expect(lib).toContain("id: 'full_access'");
    // 只有完全访问是 danger 档：UI 靠这个标记决定是否走红色确认
    expect(lib).toContain('可以读写工作目录之外的本机路径，并允许删除文件');
    expect(lib.match(/danger: true/g)).toHaveLength(1);
    expect(lib).toContain("DEFAULT_FILE_PERMISSION: FilePermissionMode = 'ask_every_time'");
    // 非法值归一到最保守档，存量脏值不能变成「完全访问」
    expect(lib).toContain('function normalizeFileMode');
    // 没确认过的完全访问读回来就降级：绕过弹窗写入台账的路径放不开边界
    expect(lib).toContain("if (mode === 'full_access' && typeof confirmedAtMs !== 'number')");
    expect(lib).toContain('return filePermissionAfterConfirm(normalizeFileMode(entry.mode), entry.confirmedAtMs)');
  });

  it('对话框有档位选择器，且完全访问必须先过红色确认', () => {
    const input = chatInput();
    expect(input).toContain('data-file-permission');
    expect(input).toContain('FILE_PERMISSION_DEFS');
    expect(input).toContain('file-permission-danger-title');
    expect(input).toContain("bgcolor: 'var(--tt-danger-fill)'");
    // danger 档不落库，只进 pending；确认按钮才带 confirmedAtMs
    expect(input).toContain('if (filePermissionDef(mode).danger)');
    expect(input).toContain('onFilePermissionChange?.(mode, Date.now())');
    // 位置：档位选择器在输入卡内工具栏（+ 之后），不在卡片下方那行里
    const toolbar = input.split('{/* 工具栏')[1].split('{/* 输入框下方')[0];
    expect(toolbar, '档位选择器应挂在卡片内工具栏').toContain('data-file-permission');
  });

  it('焦点环不套框：输入卡的信号贴着卡片自身，小控件的环收进边内', () => {
    const input = chatInput();
    // 文本控件在 Chromium 里一律命中 :focus-visible，全局那条环落在卡片里面就是「框中框」。
    // 关键：全局规则的特异性是 (3,0,0)（:is 跟最 specific 的参数走），组件里写
    // `'& textarea:focus-visible': { outline: 'none' }` 只有 (2,1,0)，压不住它——曾经这样写过，
    // 用户实机看仍然是蓝框。所以现在走 globals.css 的点名排除，不走特异性对抗。
    expect(input).toContain('data-tt-ring="container"');
    expect(input, '不许再用打不赢的特异性对抗全局环').not.toMatch(/textarea:focus-visible['"]?\s*:\s*\{/);
    // 容器环用随皮肤翻的 --tt-teal（实算对页面底：亮 5.15:1、暗 12.07:1，均 ≥3:1）
    expect(input).toContain('0 0 0 2px var(--tt-teal)');
    // 卡片上不许再挂 outline 环：那正是用户嫌的「明显的蓝色框」
    expect(input).not.toContain('outline: focused ?');
    expect(input).not.toContain("outlineOffset: '2px'");
    // 无边框小控件（档位 chip、目录 chip）：环往内收，不再浮出第二个框
    expect(input.match(/outlineOffset: '-2px'/g)).toHaveLength(2);
    expect(input.match(/data-tt-ring="self"/g)).toHaveLength(2);
  });

  it('档位是每会话的，且删除台账与会话归档一起走', () => {
    const st = store();
    expect(st).toContain('filePermission: conv.filePermission');
    expect(st).toContain('getConversationFilePermission(convId) ?? undefined');
    expect(st).toContain('dropConversationFilePermission');
    // 提交时用生效档位（会话 > 全局默认 > 最保守档），不是直接读镜像字段
    expect(st).toContain('resolveFilePermission(convId, settings?.filePermissionDefault)');
    expect(st).toContain('filePermission: effectiveFilePermission,');
    // 重新生成/改写不因全局默认而悄悄升档：没记录回到最保守档
    const regen = st.split('regenerateTurn: async (')[1].split('resumeTaskFromStep: async (')[0];
    expect(regen).toContain("filePermission: conv.filePermission ?? 'ask_every_time'");
    // 新任务页还没有会话：选择先进草稿，第一条消息建会话时才落台账（确认时刻一起带上）
    expect(st).toContain('draftFilePermission: { mode: granted, confirmedAtMs: confirmedAtMs ?? null }');
    expect(st).toContain('const draft = get().draftFilePermission;');
    expect(st).toContain('setConversationFilePermission(convId, draft.mode, { confirmedAtMs: draft.confirmedAtMs });');
    // 草稿只属于「还没会话」的那一刻：新建/切换会话即作废，不会漂进别的会话
    expect(st.split('newConversation: () => {')[1].split('switchConversation:')[0]).toContain(
      'draftFilePermission: null',
    );
    expect(st).toContain('set({ activeId: id, ...mirrorOf(conv), draftFilePermission: null })');
  });

  it('设置页有全局默认档位，同样是红色确认后才落库', () => {
    const page = read('../../../src/renderer/pages/Settings/index.tsx');
    expect(page).toContain('label="文件访问默认档位"');
    expect(page).toContain('normalizeFileMode(appSettings.filePermissionDefault)');
    expect(page).toContain('settings-file-permission-title');
    expect(page).toContain('if (filePermissionDef(mode).danger)');
    // 默认档位必须写明「会话选择优先」，否则用户会以为设置页改了就一定生效
    expect(page).toContain('且优先于这里的默认值');
  });

  it('引擎在受理时校验档位，非法值拒绝而不是当成缺省', () => {
    const engine = engineServer();
    expect(engine).toContain('payload.get("filePermission")');
    expect(engine).toContain('sr.FILE_PERMISSION_MODES');
    expect(engine).toContain('FILE_PERMISSION_INVALID');
    expect(engine).toContain('session.runtime = {**rt, "filePermission": perm_value}');
  });

  it('强制逻辑按目录边界分档，删除只属于完全访问', () => {
    const src = sandboxRuntime();
    expect(src).toContain('def permission_denial');
    expect(src).toContain('delete_requires_full_access');
    expect(src).toContain('outside_boundary');
    expect(src).toContain('hard_deny');
    // 档位先于「未配置沙箱」的提前返回：没有沙箱块也照样受限
    expect(src).toContain('def file_denial');
    expect(src).toContain('DELETING_TOOLS = frozenset({"delete_text_file"})');
    // 每次询问走本机网关；网关不可用时如实说明而不是静默放行
    expect(src).toContain('def ask_user');
    expect(src).toContain('file_permission_ask');
  });

  it('删除工具进引擎注册表与副作用清单，且先复制进回收目录', () => {
    const engine = engineServer();
    expect(engine).toContain('def delete_text_file');
    expect(engine).toContain('_to_trash');
    expect(engine).toContain('file_trash_dir');
    expect(read('../../../engine/data_paths.py')).toContain('FILE_TRASH_DIRNAME');
    // 副作用工具必须登记进副作用清单，否则续跑会重复删。
    // #164 起清单只有一处落表（engine/side_effect_registry.py），checkpoint_runtime 与
    // tool_approval 都从它派生、不再自带成员字面量，所以这里改盯名单本体——
    // 语义没变：delete_text_file 必须在表里。四张视图的同源校验在
    // tests/unit/workbench/step-replay.test.ts 与 tests/integration/engine_route_lanes_selftest.py。
    expect(read('../../../engine/side_effect_registry.py')).toContain('delete_text_file');
    expect(read('../../../src/shared/step-replay.ts')).toContain('delete_text_file');
    // 对用户可见的工具说明同样要有它，不能只在引擎里注册
    expect(read('../../../src/shared/builtin-tools.ts')).toContain('delete_text_file');
  });

  it('Rust 桥接把档位盖进 runtime 并随提交下发（不是只收下就丢掉）', () => {
    const rust = rustLib();
    const fn = rust.split('async fn chat_submit(')[1].split(') -> Result<Value, String>')[0];
    expect(fn).toContain('file_permission: Option<String>');
    const body = rust.split('async fn chat_submit(')[1].split('\n}\n')[0];
    // 显式提交的坏档位拒绝受理；没表态才读全局默认
    expect(body).toContain('security::file_perm::normalize(raw)?');
    expect(body).toContain('security::file_perm::read_default(&p0.store)');
    expect(body).toContain('stamp_file_permission(&mut runtime, &file_permission);');
    expect(body).toContain('file_permission: Some(file_permission),');
    // 「每次询问」要的确认网关在审批档位为 off 时也要下发，否则弹不出框
    expect(rust).toContain('if mode != security::file_perm::DEFAULT_MODE');
    expect(rust).toContain('askedForFilePermission');
    const apply = rustAgent().split('fn apply_to(&self, body: &mut Value)')[1].split('\n}\n')[0];
    expect(apply).toContain('map.insert("filePermission".into()');
    expect(rustAgent()).toContain('pub file_permission: Option<String>,');
  });

  it('回环网关为文件档位开门：审批 off 不再顺手拒掉写/删确认', () => {
    const gate = read('../../../src-tauri/src/bridge/tool_gate.rs');
    expect(gate).toContain('let is_file_ask = kind == "file_permission"');
    expect(gate).toContain('mode == "off" && !is_file_ask');
    // 只有档位真声明「每次询问」才受理，别档来问直接拒（问不出放行，但少一个假框）
    expect(gate).toContain('FILE_PERMISSION_ASK_NOT_REQUIRED');
    expect(gate).toContain('tool_approval::confirm_file_action(&description)');
    expect(gate).toContain('audit::ACTION_FILE_WRITE');
  });

  it('本地 harness 同一套档位：未确认不写、越界拒绝、删除先备份、续跑沿用', () => {
    const tools = read('../../../src-tauri/src/harness/tools.rs');
    const runner = read('../../../src-tauri/src/harness/runner.rs');
    expect(read('../../../src-tauri/src/security/file_perm.rs')).toContain('delete_requires_full_access');
    expect(read('../../../src-tauri/src/security/file_perm.rs')).toContain('outside_boundary');
    expect(read('../../../src-tauri/src/security/file_perm.rs')).toContain('hard_deny');
    // 默认构造是最保守档：新调用方不显式表态也不会拿到放开的一档
    expect(tools).toContain('Self::with_permission(base, skills, file_perm::DEFAULT_MODE)');
    expect(tools).toContain('file_perm::access_denial');
    expect(tools).toContain('Gate::Ask');
    expect(tools).toContain('未经用户确认，未执行');
    expect(tools).toContain('"delete_text_file"');
    expect(tools).toContain('copy_into_trash_dir');
    // runner：确认框在 blocking 线程里弹，拒绝与放行都留审计
    expect(runner).toContain('tool_approval::confirm_file_action');
    expect(runner).toContain('toolbox.run_after_approval(&call.tool, &args)');
    // 档位进检查点，续跑不回到全局默认
    expect(runner).toContain('file_permission: snap');
    expect(read('../../../src-tauri/src/harness/checkpoint.rs')).toContain(
      'pub file_permission: String',
    );
    expect(rustLib()).toContain('cp.meta.file_permission');
  });
});

// ── 行为：Mock 用量归属跟着选择走 ───────────────────────────────

(globalThis as Record<string, unknown>).window = (globalThis as Record<string, unknown>).window ?? {};
type WindowAPI = {
  tongtianAPI?: {
    chatSubmit: (req: Record<string, unknown>) => Promise<{ taskId: string }>;
    chatReview: (taskId: string, action: string, patch?: unknown[]) => Promise<{ ok: boolean; status: string }>;
    onChatEvent?: (callback: (event: unknown) => void) => () => void;
  };
};
const win = globalThis.window as WindowAPI;

let usage: typeof import('../../../src/renderer/lib/usage-local');

beforeAll(async () => {
  await import('../../../src/renderer/lib/mock-api');
  usage = await import('../../../src/renderer/lib/usage-local');
});

beforeEach(() => {
  usage.resetLocalUsage();
});

describe('Mock 通道：选择模型决定用量归属', () => {
  async function runWith(req: Record<string, unknown>) {
    vi.useFakeTimers();
    try {
      const task = await win.tongtianAPI!.chatSubmit({ goal: '整理季度报表', ...req });
      await win.tongtianAPI!.chatReview(task.taskId, 'approve');
      await vi.advanceTimersByTimeAsync(5000);
      return usage.localUsageRows('total');
    } finally {
      vi.useRealTimers();
    }
  }

  it('选择「经济」（qwen-flash，模型清单里没有）→ 用量归到 qwen-flash', async () => {
    const rows = await runWith({ modelBinding: 'qwen-flash' });
    expect(rows.map((r) => r.modelName)).toEqual(['qwen-flash']);
    expect(rows[0].calls).toBe(4);
    // 归属名不得是「当前默认模型」这种含糊文案，用户选了什么就写什么
    expect(rows[0].displayName).toBe('qwen-flash');
  });

  it('选择清单内的自定义模型 → 用其展示名，且不掺杂默认模型', async () => {
    const rows = await runWith({ modelBinding: 'qwen2.5:14b' });
    expect(rows.map((r) => r.modelName)).toEqual(['qwen2.5:14b']);
    expect(rows[0].displayName).toBe('本地 Ollama');
  });

  it('选择已保存模型 id → 按清单解析到该模型的真实服务端名', async () => {
    const rows = await runWith({ modelId: 'mock-model-2' });
    expect(rows.map((r) => r.modelName)).toEqual(['qwen2.5:14b']);
    expect(rows[0].displayName).toBe('本地 Ollama');
  });

  it('已保存模型 id 不存在 → 拒绝提交，不拿默认模型顶替', async () => {
    await expect(
      win.tongtianAPI!.chatSubmit({ goal: '未知模型', modelId: 'no-such-model' }),
    ).rejects.toThrow('MODEL_NOT_FOUND');
  });

  it('未作选择（Auto）→ 归属当前默认模型', async () => {
    const rows = await runWith({ modelBinding: '' });
    expect(rows.map((r) => r.modelName)).toEqual(['qwen-plus']);
  });

  it('所选工作目录决定 Mock 的会话子目录前缀（产出落进所选目录，不再固定缺省目录）', async () => {
    vi.useFakeTimers();
    const events: Array<Record<string, unknown>> = [];
    const off = win.tongtianAPI!.onChatEvent!((e) => events.push(e as unknown as Record<string, unknown>));
    try {
      const task = await win.tongtianAPI!.chatSubmit({ goal: '落盘校验', workDir: 'D:/chosen/dir' });
      await win.tongtianAPI!.chatReview(task.taskId, 'approve');
      await vi.advanceTimersByTimeAsync(5000);
    } finally {
      off();
      vi.useRealTimers();
    }
    const deliver = events.find((e) => e.type === 'deliver') as { artifacts?: string[] } | undefined;
    expect(deliver, 'Mock 执行流应产出 deliver 事件').toBeTruthy();
    expect(deliver!.artifacts!.length).toBeGreaterThan(0);
    expect(deliver!.artifacts![0].startsWith('D:/chosen/dir/')).toBe(true);
    expect(deliver!.artifacts![0]).not.toContain('/mock/workspaces/default/sessions');
  });
});
