/**
 * 断点续跑前端契约（检查点事件的消费方）
 *
 * 引擎在任务收尾前下发 checkpoint 事件，Rust 只透传，真正的续跑入口在渲染层。
 * 这里锁定三件事：
 * - 检查点状态按会话保存，提交新目标 / 重新生成时清空（旧检查点不能套到新任务上）
 * - `resumeTaskFromStep` 只在「检查点属于该轮任务且可恢复、任务未在跑、锚点确实属于这一轮」
 *   时发恢复请求；失败时保留检查点与入口，把引擎给的真实原因显示出来
 * - 界面入口：失败步骤与最后执行到的工具步骤挂得出按钮，点击先进重放预览，
 *   预览里有写操作必须勾选才发起（D-15）
 * - 三通道与 Rust/引擎的接线：Mock 不伪造恢复成功（只有 [test:failstep] 演示剧本有快照），
 *   Rust 只允许已结束任务续跑，引擎不可用时把 code/message 带到界面
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { useWorkbenchStore, type ConversationState } from '../../../src/renderer/store/workbenchStore';
import { TaskStatus, type CheckpointInfo } from '../../../src/shared/types';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const storeSrc = () => read('../../../src/renderer/store/workbenchStore.ts');
const pageSrc = () => read('../../../src/renderer/pages/Workbench/index.tsx');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const rustAgent = () => read('../../../src-tauri/src/bridge/agent.rs');
const rustSse = () => read('../../../src-tauri/src/bridge/sse.rs');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const engineServer = () => read('../../../engine/server.py');

const checkpointOf = (over: Partial<CheckpointInfo> = {}): CheckpointInfo => ({
  type: 'checkpoint',
  taskId: 'task-1',
  resumable: true,
  reason: '执行中断：模型超时',
  savedAt: '2026-10-22T10:00:00.000Z',
  scope: 'disk',
  snapshotMessages: 4,
  sideEffects: 2,
  ...over,
});

function makeConv(over: Partial<ConversationState> = {}): ConversationState {
  return {
    id: 'conv-1',
    title: '整理周报',
    createdAt: 1,
    messages: [],
    events: [],
    turnEvents: [[]],
    taskTurnMap: { 'task-1': 0 },
    regeneratingTurn: null,
    turnModels: {},
    feedbackByTurn: {},
    plan: null,
    fileOps: [],
    taskId: 'task-1',
    taskStatus: TaskStatus.FAILED,
    taskMeta: {},
    quotaSnapshot: null,
    checkpoint: checkpointOf(),
    isLoading: false,
    error: null,
    ...over,
  };
}

type ReviewCall = [string, string, unknown];

function installWindow(chatReview: (...args: ReviewCall) => Promise<unknown>) {
  (globalThis as Record<string, unknown>).window = {
    tongtianAPI: { chatReview },
  };
}

function seed(over: Partial<ConversationState> = {}) {
  const conv = makeConv(over);
  useWorkbenchStore.setState({
    convs: { [conv.id]: conv },
    convOrder: [conv.id],
    activeId: conv.id,
    conversations: [{ id: conv.id, title: conv.title, createdAt: conv.createdAt, isLoading: conv.isLoading }],
  });
  return conv;
}

beforeEach(() => {
  useWorkbenchStore.setState({
    convs: {},
    convOrder: [],
    activeId: null,
    conversations: [],
    taskId: null,
    isLoading: false,
    error: null,
    checkpoint: null,
  });
  installWindow(async () => ({ ok: true, status: 'executing' }));
});

// ── 恢复动作（行为） ─────────────────────────────────────────

describe('resumeTaskFromStep：只对属于当前任务的可恢复检查点发起恢复', () => {
  it('可恢复检查点 → 同一 taskId 走 review resume，不新增对话轮次', async () => {
    seed();
    const chatReview = vi.fn(async () => ({ ok: true, status: 'executing' }));
    installWindow(chatReview);

    await useWorkbenchStore.getState().resumeTaskFromStep(0);

    expect(chatReview).toHaveBeenCalledTimes(1);
    expect(chatReview.mock.calls[0]).toEqual(['task-1', 'resume', undefined]);
    // 恢复期间保持 loading：后续事件继续路由到这个会话
    expect(useWorkbenchStore.getState().isLoading).toBe(true);
    expect(useWorkbenchStore.getState().convs['conv-1'].isLoading).toBe(true);
    // 恢复被接受：终态锁放开，任务回到执行中（否则徽标一直停在「失败」）
    expect(useWorkbenchStore.getState().convs['conv-1'].taskStatus).toBe(TaskStatus.EXECUTING);
    // 同一 taskId、仍是第 1 轮，不另起对话
    expect(useWorkbenchStore.getState().convs['conv-1'].taskId).toBe('task-1');
    expect(useWorkbenchStore.getState().convs['conv-1'].turnEvents).toHaveLength(1);
  });

  it('检查点属于别的任务 / 不可恢复 / 正在执行 / 无任务 → 都不发请求', async () => {
    const chatReview = vi.fn(async () => ({ ok: true, status: 'executing' }));
    installWindow(chatReview);

    seed({ checkpoint: checkpointOf({ taskId: 'task-other' }) });
    await useWorkbenchStore.getState().resumeTaskFromStep(0);

    seed({ checkpoint: checkpointOf({ resumable: false }) });
    await useWorkbenchStore.getState().resumeTaskFromStep(0);

    seed({ isLoading: true });
    await useWorkbenchStore.getState().resumeTaskFromStep(0);

    // 本轮既没有 taskId 也没有轮次归属：没有任何可续跑的任务号
    seed({ taskId: null, taskTurnMap: {} });
    await useWorkbenchStore.getState().resumeTaskFromStep(0);

    seed({ checkpoint: null });
    await useWorkbenchStore.getState().resumeTaskFromStep(0);

    expect(chatReview).not.toHaveBeenCalled();
    const last = useWorkbenchStore.getState().convs['conv-1'];
    expect(last.checkpoint).toBeNull();
    expect(last.isLoading).toBe(false);
  });

  it('锚点步骤不在本轮事件流里 → 不发请求，并说清原因', async () => {
    const chatReview = vi.fn(async () => ({ ok: true, status: 'executing' }));
    installWindow(chatReview);
    seed({ turnEvents: [[{ type: 'step_start', stepId: 'step-1', stepType: 'file_read', description: '调用工具: view_text_file', tool: 'view_text_file' } as never]] });

    await useWorkbenchStore.getState().resumeTaskFromStep(0, 'step-99');

    expect(chatReview).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().convs['conv-1'].error).toContain('该步骤不在本轮执行记录里');
    // 锚点在本轮时正常发起
    await useWorkbenchStore.getState().resumeTaskFromStep(0, 'step-1');
    expect(chatReview).toHaveBeenCalledTimes(1);
  });

  it('后端回了 ok:false（没起来）→ 退出 loading、说明不可用，检查点保留可再试', async () => {
    seed();
    installWindow(async () => ({ ok: false, status: 'CHECKPOINT_UNAVAILABLE' }));

    await useWorkbenchStore.getState().resumeTaskFromStep(0);

    const conv = useWorkbenchStore.getState().convs['conv-1'];
    expect(conv.isLoading).toBe(false);
    expect(conv.error).toContain('CHECKPOINT_UNAVAILABLE');
    expect(conv.checkpoint?.resumable).toBe(true);
  });

  it('恢复没启动成功 → 结束 loading、显示引擎原因，并保留检查点让用户可重试', async () => {
    seed();
    installWindow(async () => {
      throw 'CHECKPOINT_UNAVAILABLE: 检查点只保存在引擎进程内存里，引擎重启后无法恢复';
    });

    await useWorkbenchStore.getState().resumeTaskFromStep(0);

    const conv = useWorkbenchStore.getState().convs['conv-1'];
    expect(conv.isLoading).toBe(false);
    // 裸字符串拒绝正是 Rust Err(String) 的形态：D-20 后必须映射成中文原因，裸码不外露
    expect(conv.error).toContain('本地还没有可恢复的检查点');
    expect(conv.error).not.toContain('CHECKPOINT_UNAVAILABLE');
    expect(conv.error).toContain('引擎重启');
    // 入口与检查点都还在：用户可以修好环境后再点一次
    expect(conv.checkpoint?.resumable).toBe(true);
    expect(useWorkbenchStore.getState().error).toBe(conv.error);
  });
});

// ── 检查点状态的生命周期（源码契约） ─────────────────────────

describe('store：检查点按会话保存并在新任务时清空', () => {
  it('checkpoint 事件只更新当前会话的检查点，不影响其他会话', () => {
    const src = storeSrc();
    expect(src).toContain("case 'checkpoint':");
    // 检查点自带 taskId，归属轮次由 taskTurnMap 反查：续跑入口只挂在这一轮
    expect(src).toContain('checkpoint: event,');
    expect(src).toContain(
      'checkpointTurn: event.taskId ? conv.taskTurnMap[event.taskId] ?? turnIdx : turnIdx,',
    );
    // 检查点属于会话状态，顶层字段只是镜像
    expect(src).toContain('checkpoint: conv.checkpoint,');
    expect(src).toContain('checkpointTurn: conv.checkpointTurn,');
  });

  it('提交新目标与重新生成最后一轮都清空旧检查点（不把旧现场套到新任务）', () => {
    const src = storeSrc();
    const submit = src.split('submitGoal: async (')[1].split('regenerateTurn: async (')[0];
    expect(submit).toContain('checkpoint: null,');
    const regen = src.split('regenerateTurn: async (')[1].split('resumeTaskFromStep: async (')[0];
    expect(regen).toContain('checkpoint: null,');
    // 轮次归属同样要清空，否则续跑入口会挂到已经没有快照的轮次
    expect(regen).toContain('checkpointTurn: null,');
  });

  it('恢复入口受 isLoading / taskId / resumable 三重约束', () => {
    const src = storeSrc();
    const resume = src.split('resumeTaskFromStep: async (')[1].split('\n    },')[0];
    expect(resume).toContain('if (conv.isLoading) {');
    expect(resume).toContain('if (!cp?.resumable || cp.taskId !== taskId) {');
    expect(resume).toContain("await api.chat.review(taskId, { action: 'resume' });");
    expect(resume).toContain('patchConv(conv.id, { isLoading: true, error: null, taskMeta: {} });');
    expect(resume).toContain("errorTextOf(err, '从检查点恢复失败')");
    // 恢复被接受要放开终态锁；明确 ok:false 时退回可重试
    expect(resume).toContain('taskStatus: TaskStatus.EXECUTING');
    expect(resume).toContain('result.ok === false');
  });
});

// ── 界面入口 ────────────────────────────────────────────────

describe('工作台：续跑按钮只在检查点可恢复时出现', () => {
  it('可见性只看检查点收尾状态，与 14 态「继续」互不影响', () => {
    const src = pageSrc();
    expect(src).toContain('checkpoint?.resumable && taskId && checkpoint.taskId === taskId');
    expect(src).toContain('const canResumeCheckpoint');
    expect(src).toContain("tr('从检查点继续')");
    // 头部按钮不直接发起：先开重放预览
    expect(src).toContain('onClick={() => setResumePreviewOpen(true)}');
    expect(src).toContain('<ResumeReplayDialog');
    expect(src).toContain('anchorStepId={defaultReplayAnchor(checkpointTurnEvents)}');
    expect(src).toContain('void resumeTaskFromStep(checkpointTurn, anchorStepId)');
    // 提示里要说明真实的跳过口径（不是「所有已完成工具都不重做」）与持久化边界
    expect(src).toContain('已成功的写入类工具按台账不重做，只读与未登记工具会再执行一次');
    expect(src).toContain('点击后先列出将要重新执行的步骤，含写操作需勾选确认');
    expect(src).toContain("checkpoint?.scope === 'disk'");
    expect(src).toContain('检查点已保存到本机磁盘，刷新或引擎重启后仍可继续');
    expect(src).toContain('检查点只保存在本次引擎进程内，引擎重启后无法恢复');
  });

  it('续跑入口按轮次归属下发：只有检查点那一轮挂得到按钮', () => {
    const src = pageSrc();
    expect(src).toContain('resumeTurn={canResumeCheckpoint ? checkpointTurn : null}');
    expect(src).toContain('onResumeStep={(turnIdx, stepId) => void resumeTaskFromStep(turnIdx, stepId)}');
  });
});

describe('消息流：单步重试入口的挂载范围', () => {
  const streamSrc = () => read('../../../src/renderer/components/ChatStream/index.tsx');

  it('只有失败步骤与最后执行到的工具步骤挂得出入口（快照不在任意一步之前）', () => {
    const src = streamSrc();
    const fn = src.split('const resumeAnchors = (')[1].split('\n  const renderResumeAction')[0];
    expect(fn).toContain('if (!onResumeStep || resumeTurn !== turnIdx || running) return null;');
    expect(fn).toContain('if (e.stepId && stepToolName(e)) {');
    expect(fn).toContain("if (status === 'failed') anchors.add(id);");
    expect(fn).toContain('if (lastToolStep) anchors.add(lastToolStep);');
  });

  it('按钮文案区分「重试此步」与「从此步继续」，点击先进预览而不是直接发起', () => {
    const src = streamSrc();
    expect(src).toContain("{retry ? tr('重试此步') : tr('从此步继续')}");
    expect(src).toContain('const retry = ctx.state.get(stepId) === \'failed\';');
    expect(src).toContain('onClick={() => setResumeTarget({ turnIdx, stepId })}');
    expect(src).not.toContain('onResumeStep?.(target.turnIdx, target.stepId)');
  });

  it('预览由共享组件承担，发起前不重复实现一份判定', () => {
    const src = streamSrc();
    expect(src).toContain("import ResumeReplayDialog from '../ResumeReplayDialog';");
    expect(src).toContain('<ResumeReplayDialog');
    expect(src).toContain('if (target) onResumeStep?.(target.turnIdx, anchorStepId);');
  });
});

describe('重放预览组件：先摊开清单，写操作要勾选', () => {
  const dialogSrc = () => read('../../../src/renderer/components/ResumeReplayDialog/index.tsx');

  it('清单、汇总、不确定性口径都直接展示判定层给的文案', () => {
    const src = dialogSrc();
    expect(src).toContain('buildReplayPlan(events, anchorStepId)');
    expect(src).toContain('plan?.summaryZh');
    expect(src).toContain('plan?.caveatZh');
    expect(src).toContain('confirmWarningZh(plan.confirmTools)');
    expect(src).toContain("step.kind === 'other'");
    expect(src).toContain('replayRiskLabel(step.risk)');
  });

  it('需要确认时勾选才放行，且每次打开都要重新勾选', () => {
    const src = dialogSrc();
    expect(src).toContain('disabled={Boolean(plan?.needsConfirm) && !acknowledged}');
    expect(src).toContain('if (open) setAcknowledged(false);');
    expect(src).toContain('onConfirm(plan.anchorStepId)');
  });

  it('只在打开时构建计划：消息流每次重渲染都不必复算', () => {
    expect(dialogSrc()).toContain('const plan = open ? buildReplayPlan(events, anchorStepId) : null;');
  });
});

// ── 刷新恢复入口（跨进程） ───────────────────────────────────

describe('刷新恢复：从磁盘查询检查点入口', () => {
  it('hydrateProcess 按最后一轮任务查询并在同一会话上恢复入口', () => {
    const src = storeSrc();
    const hydrate = src.split('const hydrateProcess = async (')[1].split('return {')[0];
    expect(hydrate).toContain('api.chat');
    expect(hydrate).toContain('.checkpoints([latestTaskId])');
    expect(hydrate).toContain('checkpoint:');
    // 实时事件已写入的检查点优先；否则用查询结果（不可恢复时自然不显示）
    expect(hydrate).toContain('finalConv.checkpoint ??');
  });

  it('三通道都实现了查询：Tauri command / 引擎直连 / Mock 只认演示剧本', () => {
    const tauri = read('../../../src/renderer/lib/tauri-api.ts');
    expect(tauri).toContain("invoke('chat_checkpoint_query'");
    const engine = read('../../../src/renderer/lib/engine-api.ts');
    expect(engine).toContain('/api/chat/checkpoint/query');
    // Mock 依然不伪造：只有走过 [test:failstep] 剧本的任务才有快照，普通任务返回空
    const mock = mockApi();
    expect(mock).toContain('chatCheckpointQuery: async (taskIds: string[] = [])');
    expect(mock).toContain('taskIds.filter((id) => mockCheckpoints.has(id))');
  });

  it('Rust 命令批量去重并限制 200 条，查询失败不编造入口', () => {
    const src = rustLib();
    const cmd = src.split('async fn chat_checkpoint_query(')[1].split('async fn chat_cancel(')[0];
    expect(cmd).toContain('task_ids: Vec<String>');
    expect(cmd).toContain('!ids.contains(&id)');
    expect(cmd).toContain('ids.len() >= 200');
    expect(cmd).toContain('agent::query_checkpoints');
    const agent = rustAgent();
    expect(agent).toContain('/api/chat/checkpoint/query');
    expect(agent).toContain('ENGINE_NOT_RUNNING: 引擎未启动，无法查询检查点');
  });

  it('引擎查询只返回摘要，且运行中的任务不可续跑', () => {
    const src = engineServer();
    expect(src).toContain('@app.post("/api/chat/checkpoint/query")');
    expect(src).toContain('ckpt.public_info(task_id, data)');
    expect(src).toContain('session.agent_task is not None and not session.agent_task.done()');
    expect(src).toContain('info["resumable"] = False');
  });
});

// ── 三通道与引擎/Rust 接线 ───────────────────────────────────

describe('通道接线：Mock 不伪造恢复成功', () => {
  it('Mock resume 明确回报 CHECKPOINT_UNAVAILABLE，不假装恢复', () => {
    const src = mockApi();
    const block = src.split("if (action === 'resume') {")[1].split("if (action === 'approve')")[0];
    expect(block).toContain("code: 'CHECKPOINT_UNAVAILABLE'");
    expect(block).toContain("return { ok: false, status: 'CHECKPOINT_UNAVAILABLE' };");
    expect(block).toContain('Mock 模式没有真实引擎的检查点');
  });

  it('Rust 只允许已结束任务从检查点恢复，启动失败回到原状态', () => {
    const review = rustLib().split('async fn chat_review(')[1].split('async fn chat_cancel(')[0];
    expect(review).toContain('"resume" => {');
    // 执行中/待审阅等状态不能从检查点恢复（运行中的任务只能终止）
    expect(review).toContain('status::DONE | status::FAILED | status::CANCELLED');
    expect(review).toContain('let previous_status = record.status.clone();');
    expect(review).toContain('&previous_status,');
    expect(review).toContain('CHECKPOINT_RESUME_FAILED');
  });

  it('引擎 409/404 的 code/message 会带到界面，不只显示 HTTP 状态码', () => {
    const src = rustAgent();
    expect(src).toContain('let code = field("code");');
    expect(src).toContain('let message = field("message");');
    expect(src).toContain('format!("BRIDGE_CHAT_HTTP_{status}: {detail}")');
  });

  it('SSE 映射保留 checkpoint 事件并把 snake_case 计数转成 camelCase', () => {
    const src = rustSse();
    expect(src).toContain('"checkpoint",');
    expect(src).toContain('"checkpoint" => {');
    expect(src).toContain('count(&["skippedDuplicates", "skipped_duplicates"])');
    expect(src).toContain('str_field(&["captureError", "capture_error"])');
  });

  it('引擎 resume 走同一 task 的执行流；没有检查点时返回 409 与原因', () => {
    const src = engineServer();
    expect(src).toContain('if action == "resume":');
    expect(src).toContain('session.checkpoint.unavailable_reason()');
    expect(src).toContain('"code": "CHECKPOINT_UNAVAILABLE"');
    expect(src).toContain('session.model_config, resume=True,');
    // 先发 checkpoint 再发 done，前端收到即可决定是否显示入口
    expect(src).toContain('_safe_put(queue, "checkpoint", checkpoint.final_event())');
  });
});
