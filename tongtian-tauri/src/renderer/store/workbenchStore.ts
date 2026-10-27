/**
 * 通天办公 - 工作台状态管理（多会话并行版）
 *
 * 支持多个独立对话并行：
 * - 每个会话（Conversation）持有独立的 消息/事件/计划/任务状态
 * - 「新任务」创建新会话；历史会话可随时切回；运行中的会话不互相干扰
 * - 全局事件监听按 taskId 路由到对应会话（并行执行互不覆盖）
 * - 顶层字段镜像当前激活会话，页面组件无需改动
 */

import { create } from 'zustand';
import type {
  Message,
  ChatEvent,
  Plan,
  Step,
  FileOp,
  ChatStepPreview,
  WaitReason,
  WorkMode,
  CheckpointInfo,
} from '../../shared/types';
import { Role, PlanStatus, StepStatus, FileOpType, FileOpStatus, TaskStatus } from '../../shared/types';
import type { QuotaSnapshot } from '../../shared/contracts';
import { errorMessageOf, errorTextOf } from '../../shared/error-messages';
import { archiveTask, getSettings, listArchived } from '../lib/alignment-api';
import {
  feedbackOfTurns,
  fetchConversationMessages,
  fetchConversations,
  groupTurns,
  recordConversationMessage,
  replayProcessEvents,
  restoredTurnEvents,
  restoredUserMessages,
  saveFeedback,
  taskTurnMapOf,
  titleOf,
} from '../lib/conversation-store';
import type { RestoredTurn } from '../lib/conversation-store';
import { api } from '../lib/api';
import { chatModelSelectionOf, modelBaseUrl } from '../lib/chat-models';
import { browserPptxTemplates } from '../../shared/pptx-templates';
import { getActiveWorkspaceId } from './workspaceStore';
import { useAppStore } from '../stores/useAppStore';
import {
  claimNoticeKey,
  noticeKindFor,
  notifyTaskEvent,
  type NoticeKind,
} from '../lib/notify-local';
import { markTaskUnread } from '../lib/badge-local';
import { captureCollabDeliver, captureCollabFailure, completeCollabRun } from '../lib/collab-run';

// ── 状态接口 ─────────────────────────────────────────────────

/** 任务元数据视图（14 态增量字段，全可选） */
export interface TaskMetaView {
  waitReason?: WaitReason;
  queuePosition?: number;
  statusReasonCode?: string;
  retryCount?: number;
}

/** 单个会话的独立状态 */
export interface ConversationState {
  id: string;
  /** 会话标题（首个目标截取） */
  title: string;
  createdAt: number;
  messages: Message[];
  events: ChatEvent[];
  /** 按对话轮次分组的事件流 */
  turnEvents: ChatEvent[][];
  /** taskId → 轮次索引映射（支持指定轮次重新生成） */
  taskTurnMap: Record<string, number>;
  /** 正在重新生成的轮次索引（无则 null） */
  regeneratingTurn: number | null;
  /** 本轮/本会话使用的模式（重新生成时沿用，避免通用模式重跑幻灯片任务） */
  workMode?: WorkMode;
  /**
   * 轮次 → 当轮提交的模型选择。重新生成回放的是这一轮当初的模型，
   * 不是当前输入框里后来切换的选择；空对象表示当轮用 Auto（不覆盖后端默认）。
   * 只活在本次会话状态里：刷新后旧轮次没有记录，回退到当前选择。
   */
  turnModels: Record<number, { modelBinding?: string; modelId?: string }>;
  /**
   * 轮次 → 当轮负责人专家 id（重新生成沿用；空表示通用助手）。
   * 与 turnModels 同生命周期：只活在本次会话状态里。
   */
  turnExperts: Record<number, string>;
  /**
   * 轮次 → 回复评价（点赞/点踩互斥，未评价的轮次不出现在表里）。
   * 按 (会话, 轮次) 归属，与 Rust chat_feedback 同一口径：重新生成同一轮不会换键，
   * 切会话也不串轮。
   */
  feedbackByTurn: Record<number, 'up' | 'down'>;
  plan: Plan | null;
  fileOps: FileOp[];
  taskId: string | null;
  taskStatus: TaskStatus | null;
  taskMeta: TaskMetaView;
  quotaSnapshot: QuotaSnapshot | null;
  /**
   * 最近一次任务收尾时的检查点状态。resumable=true 才显示「从检查点继续」；
   * 检查点默认落盘（scope=disk），刷新或引擎重启后仍可恢复；
   * 落盘失败时 scope=same-process，会如实告知本次进程外不可恢复。
   */
  checkpoint: CheckpointInfo | null;
  isLoading: boolean;
  error: string | null;
  /**
   * 由正文库重建、尚未补齐过程明细的会话（刷新后首次切到时按任务回放事件流补齐）。
   * 事件流只留存 24h，超窗的会话保持「正文 + 无过程明细」，此处不做补齐假数据。
   */
  pendingProcess?: boolean;
}

/** 会话摘要（侧边栏列表用） */
export interface ConversationSummary {
  id: string;
  title: string;
  createdAt: number;
  isLoading: boolean;
}

interface WorkbenchState {
  // ── 会话管理 ──
  convs: Record<string, ConversationState>;
  convOrder: string[];
  activeId: string | null;
  /** 侧边栏展示用会话摘要 */
  conversations: ConversationSummary[];

  // ── 当前激活会话的镜像（页面组件直接消费） ──
  messages: Message[];
  events: ChatEvent[];
  turnEvents: ChatEvent[][];
  plan: Plan | null;
  fileOps: FileOp[];
  taskId: string | null;
  taskStatus: TaskStatus | null;
  taskMeta: TaskMetaView;
  quotaSnapshot: QuotaSnapshot | null;
  checkpoint: CheckpointInfo | null;
  isLoading: boolean;
  error: string | null;

  /** 历史任务列表（chat_history，保留兼容） */
  historyTasks: unknown[];
  /** 当前激活会话正在重新生成的轮次（镜像） */
  regeneratingTurn: number | null;
  /** 当前激活会话的轮次评价表（镜像） */
  feedbackByTurn: Record<number, 'up' | 'down'>;

  // ── UI 状态 ──
  planVisible: boolean;
  filePanelOpen: boolean;
  /**
   * 输入框里选中的模型（选项 id，'auto' 为缺省）。
   * 放在 store 而不是 ChatInput 内部：欢迎页与对话态各挂一个 ChatInput 实例，
   * 首轮发送后组件重挂，写在实例 state 里的选择会被无声重置成 Auto，
   * 于是第二轮起悄悄用默认模型跑（用户以为还在所选档位）。
   */
  chatModelId: string;
  /**
   * 输入框里选中的负责人专家（注册表 id，'' = 通用助手）。
   * 与模型选择同理放 store：欢迎页/对话态 ChatInput 重挂时不能丢。
   */
  chatExpertId: string;
  /**
   * 输入框里选中的工作台模式（null = 尚未初始化，按设置页默认值落定）。
   * 与模型选择同理放在 store：欢迎页/对话态两处 ChatInput 重挂时不能把模式静默重置，
   * 否则第二轮起会退回通用模式却仍显示原来的选中项。
   */
  workMode: WorkMode | null;

  // ── Actions ──
  /** 新任务：创建并激活新会话（当前为空会话则复用） */
  newConversation: () => void;
  /** 切换到指定会话 */
  switchConversation: (id: string) => void;
  /**
   * 归档会话：向 task:archive 写归档标记，侧边栏不再展示。
   * 任务本体（tasks 表行、会话目录文件）不动，归档页可恢复。
   * 失败时抛错，由调用方提示（不静默移除会话）。
   */
  archiveConversation: (id: string) => Promise<void>;
  submitGoal: (goal: string, attachments?: import('../../shared/types').AttachmentMeta[], modelBinding?: string, workDir?: string, modelId?: string, mode?: WorkMode, expertId?: string) => Promise<void>;
  /** 重新生成指定轮次的回复（重置该轮事件流后重新提交原目标） */
  regenerateTurn: (turnIdx: number) => Promise<void>;
  /**
   * 给某轮回复点赞/点踩（kind 为 null 时取消评价）。
   * 评价按 (会话, 轮次) 落库：桌面端写 SQLite chat_feedback，Mock/浏览器写本地台账。
   * 落库失败时抛错，由调用方提示（界面不留下一个没存住的选中态）。
   */
  setTurnFeedback: (convId: string, turnIdx: number, kind: 'up' | 'down' | null) => Promise<void>;
  reviewPlan: (action: 'approve' | 'reject' | 'modify', patch?: Partial<Step>[]) => Promise<void>;
  /** 从中断前最近一次模型调用前的检查点继续（同一 taskId，不新增对话轮次） */
  resumeTask: () => Promise<void>;
  controlTask: (action: 'pause' | 'resume' | 'rollback_to', stepId?: string) => Promise<void>;
  cancelTask: () => Promise<void>;
  loadHistory: () => Promise<void>;
  /** 按正文库重建会话显示（设置页「运行修复」的真实动作），返回各分类条数 */
  repairConversationDisplay: () => Promise<{
    rebuilt: number;
    keptRunning: number;
    keptUnsaved: number;
    total: number;
  }>;
  togglePlanPanel: () => void;
  toggleFilePanel: () => void;
  /** 记住输入框选中的模型（跨 ChatInput 实例重挂不丢） */
  setChatModelId: (id: string) => void;
  /** 记住输入框选中的负责人专家（'' = 通用助手） */
  setChatExpertId: (id: string) => void;
  /** 记住输入框选中的工作台模式（跨 ChatInput 实例重挂不丢） */
  setWorkMode: (mode: WorkMode) => void;
  clearError: () => void;
  reset: () => void;
}

/** wait_reason / 状态成因的 UI 文案（未知值降级展示，防白屏） */
const WAIT_REASON_TEXT: Record<string, string> = {
  rate_limited: '模型繁忙，退避重试中',
  circuit_break: '模型熔断，切换备用模型中',
  quota_wait: '配额排队中',
  budget_wait: '预算暂停，恢复后自动继续',
  lease_wait: '等待执行租约',
  user_pause: '用户暂停',
  pending_review_ttl: '审阅超期',
};

export function waitReasonText(reason?: WaitReason | string | null): string | null {
  if (!reason) return null;
  return WAIT_REASON_TEXT[reason] ?? `等待中（${reason}）`;
}

/** 14 态字符串 → TaskStatus 安全映射（未知值返回 null，防白屏） */
function toTaskStatus(status: string): TaskStatus | null {
  const values = Object.values(TaskStatus) as string[];
  return values.includes(status) ? (status as TaskStatus) : null;
}

// ── 辅助 ──────────────────────────────────────────────────

let convSeq = 0;

function emptyConv(): ConversationState {
  convSeq += 1;
  return {
    id: `conv-${Date.now()}-${convSeq}`,
    title: '新任务',
    createdAt: Date.now(),
    messages: [],
    events: [],
    turnEvents: [],
    taskTurnMap: {},
    regeneratingTurn: null,
    turnModels: {},
    turnExperts: {},
    feedbackByTurn: {},
    plan: null,
    fileOps: [],
    taskId: null,
    taskStatus: null,
    taskMeta: {},
    quotaSnapshot: null,
    checkpoint: null,
    isLoading: false,
    error: null,
  };
}

/** 会话 → 顶层镜像字段 */
function mirrorOf(c: ConversationState | undefined) {
  const empty = emptyConv();
  const conv = c ?? empty;
  return {
    messages: conv.messages,
    events: conv.events,
    turnEvents: conv.turnEvents,
    regeneratingTurn: conv.regeneratingTurn,
    feedbackByTurn: conv.feedbackByTurn,
    plan: conv.plan,
    fileOps: conv.fileOps,
    taskId: conv.taskId,
    taskStatus: conv.taskStatus,
    taskMeta: conv.taskMeta,
    quotaSnapshot: conv.quotaSnapshot,
    checkpoint: conv.checkpoint,
    isLoading: conv.isLoading,
    error: conv.error,
  };
}

function summariesOf(convs: Record<string, ConversationState>, order: string[]): ConversationSummary[] {
  return order
    .map((id) => convs[id])
    .filter((c) => c && c.messages.length > 0)
    .map((c) => ({ id: c.id, title: c.title, createdAt: c.createdAt, isLoading: c.isLoading }));
}

/** 正文库轮次 → 交付物面板条目（与 applyEvent('deliver') 同一构造口径） */
function restoredFileOps(turns: RestoredTurn[]): FileOp[] {
  const out: FileOp[] = [];
  for (const t of turns) {
    t.artifacts.forEach((path, i) => {
      out.push({
        id: `fop-${t.taskId || 'restored'}-${i}`,
        taskId: t.taskId,
        op: FileOpType.WRITE,
        srcPath: path,
        fileName: path.split(/[/\\]/).pop(),
        status: FileOpStatus.SUCCESS,
      });
    });
  }
  return out;
}

/**
 * 正文库 → 会话状态（刷新/重启后重建侧边栏与对话正文）。
 * 过程明细此刻不补齐：事件流按任务回放，等到首次切进该会话再做，避免启动时打一堆回放。
 */
function restoredConv(convId: string, title: string, createdAtMs: number, turns: RestoredTurn[]): ConversationState {
  const base = emptyConv();
  return {
    ...base,
    id: convId,
    title,
    createdAt: createdAtMs || base.createdAt,
    messages: restoredUserMessages(turns),
    turnEvents: restoredTurnEvents(turns),
    taskTurnMap: taskTurnMapOf(turns),
    feedbackByTurn: feedbackOfTurns(turns),
    fileOps: restoredFileOps(turns),
    taskId: turns.length ? turns[turns.length - 1].taskId : null,
    pendingProcess: true,
  };
}

/** 从 ChatStepPreview 构建 Plan 骨架 */
function buildPlanFromEvent(planId: string, steps: ChatStepPreview[], taskId: string): Plan {
  return {
    id: planId,
    taskId,
    status: PlanStatus.PENDING_REVIEW,
    reviewed: false,
    steps: steps.map((s) => ({
      id: s.id,
      planId,
      name: s.description,
      type: s.type,
      input: {},
      status: StepStatus.PENDING,
      rollbackable: false,
    })),
  };
}

// ── 全局事件路由（按 taskId 分发到对应会话，支持并行） ─────────

let routerRegistered = false;

/** 正在补齐过程明细的会话（避免同一会话并发回放） */
const hydrating = new Set<string>();

/**
 * 自动批准（免审阅）的时序保护：同一任务只批准一次。
 * 计划事件比 Rust 把任务写成 pending_review 更早到达，立刻批准会被状态校验拒绝，
 * 所以对「还没到待审阅」做短退避重试；其他错误直接上报，不静默失败。
 */
const autoApproveTasks = new Set<string>();

/** 自动批准最终失败时回写会话；applyEvent 在 store 闭包外，由 store 创建后注入 */
let reportAutoApproveFailure: (convId: string, message: string) => void = (_convId, message) => {
  useAppStore.getState().showNotification(message, 'error');
};

/** Rust 拒绝批准的唯一时序原因：任务还没被写成 pending_review */
const APPROVE_NOT_READY = /仅 pending_review 可批准/;
/** 已经到终态的任务不再重试（取消/失败后迟到的批准没有意义） */
const APPROVE_TASK_SETTLED = /当前 (cancelled|failed|done)/;

/**
 * 免审阅自动批准：计划生成完就自动批准进入执行。
 * Rust 的 chat_review 只接受 pending_review，而计划事件先于该状态到达，
 * 这里用有限次退避重试抹平这段时序差，保证任务真的进入执行并产出回复。
 */
export async function approvePlanWhenReady(
  taskId: string,
  onFailure: (message: string) => void,
  review: (id: string) => Promise<unknown> = (id) =>
    api.chat.review(id, { planId: '', action: 'approve' }),
): Promise<void> {
  if (autoApproveTasks.has(taskId)) return;
  autoApproveTasks.add(taskId);
  const maxAttempts = 20;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      await review(taskId);
      return;
    } catch (err) {
      const raw = typeof err === 'string' ? err : err instanceof Error ? err.message : '';
      const retriable = APPROVE_NOT_READY.test(raw) && !APPROVE_TASK_SETTLED.test(raw);
      if (!retriable || attempt === maxAttempts) {
        onFailure(errorTextOf(err, '自动批准失败'));
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 150));
    }
  }
}

/**
 * 以隐私模式提交的任务 id。
 * 隐私模式在提交时生效、对本轮整个生命周期有效：即使用户在任务运行中关掉开关，
 * 本轮回复也不会补写进正文库（避免「关掉开关才落盘」这种半隐私状态）。
 */
const privacyTasks = new Set<string>();

/**
 * 任务事件 → 系统通知/提示音（设置页两个开关的消费方）。
 * 触发口径与去重都在 notify-local，页面与测试共用同一份规则。
 */
async function notifyForEvent(kind: NoticeKind, title: string, taskId: string | null) {
  if (!claimNoticeKey(`${taskId ?? title}:${kind}`)) return;
  const s = await getSettings().catch(() => null);
  const focused = typeof document === 'undefined' ? false : document.hasFocus();
  // 任务栏未读与桌面通知/声音相互独立：只要窗口不在前台就累计未读，
  // 由设置页的「任务栏未读状态」开关在 badge-local 里决定是否真正下发。
  if (!focused) markTaskUnread(taskId);
  notifyTaskEvent(kind, s?.privacyMode ? '任务' : title, {
    desktopNotify: s?.desktopNotify !== false,
    soundNotify: Boolean(s?.soundNotify),
    focusCheck: () => focused,
  });
}

function applyEvent(
  conv: ConversationState,
  event: ChatEvent,
  turnIdx: number,
  eventTaskId?: string | null,
): Partial<ConversationState> | null {
  // 事件归档到指定轮次分组（越界时夹取到合法范围）
  const idx = Math.max(0, Math.min(turnIdx, conv.turnEvents.length - 1));
  const base: Partial<ConversationState> = {
    events: [...conv.events, event],
    turnEvents:
      conv.turnEvents.length > 0
        ? conv.turnEvents.map((g, i) => (i === idx ? [...g, event] : g))
        : conv.turnEvents,
  };

  switch (event.type) {
    case 'plan': {
      // 取消/失败后仍可能收到迟到的计划事件：只归档事件，不把任务复活成执行中
      if (conv.taskStatus === TaskStatus.CANCELLED || conv.taskStatus === TaskStatus.FAILED) {
        return base;
      }
      // 事件自带 taskId 时优先：提交响应还没回填会话时，不能拿上一轮的任务去批准
      const taskId = eventTaskId ?? conv.taskId;
      const plan = buildPlanFromEvent(event.planId, event.steps, taskId ?? '');
      const approved: Plan = { ...plan, status: PlanStatus.APPROVED, reviewed: true };
      if (taskId) {
        // 免审阅：计划一产出就尝试批准；Rust 要等计划流收尾才落 pending_review，
        // 这段时序差由 approvePlanWhenReady 的重试兜住（失败会写回会话，不静默）
        void approvePlanWhenReady(taskId, (message) => reportAutoApproveFailure(conv.id, message));
      }
      // 计划流自己的 done 紧跟其后：状态必须停在待审阅，
      // 否则 done 会把「已开始执行」误判成任务收尾，停止键和回复都会丢
      return { ...base, plan: approved, isLoading: true, taskStatus: TaskStatus.PENDING_REVIEW };
    }

    case 'step_done': {
      const plan = conv.plan;
      if (plan) {
        const updatedSteps = plan.steps.map((s) =>
          s.id === event.stepId ? { ...s, status: StepStatus.SUCCESS, result: event.output } : s,
        );
        return { ...base, plan: { ...plan, steps: updatedSteps } };
      }
      return base;
    }

    case 'step_failed': {
      const plan = conv.plan;
      if (plan) {
        const updatedSteps = plan.steps.map((s) =>
          s.id === event.stepId ? { ...s, status: StepStatus.FAILED, result: event.error } : s,
        );
        return { ...base, plan: { ...plan, steps: updatedSteps } };
      }
      return base;
    }

    case 'deliver': {
      const newOps: FileOp[] = event.artifacts.map((path, i) => ({
        id: `fop-${conv.taskId ?? 'unknown'}-${i}`,
        taskId: conv.taskId ?? '',
        op: FileOpType.WRITE,
        srcPath: path,
        fileName: path.split(/[/\\]/).pop(),
        status: FileOpStatus.SUCCESS,
      }));
      return { ...base, fileOps: [...conv.fileOps, ...newOps] };
    }

    case 'done': {
      // 重新生成中的轮次完成时清除标记
      const regenPatch = conv.regeneratingTurn != null ? { regeneratingTurn: null } : {};
      // 引擎的 done 只是「本次事件流结束」：计划流结束时任务还要进入待审阅/执行，
      // 此时按终态收尾会把主按钮一直留在停止态（点了只会得到「任务已结束」）
      const preExecution =
        conv.taskStatus === TaskStatus.QUEUED ||
        conv.taskStatus === TaskStatus.PLANNING ||
        conv.taskStatus === TaskStatus.PENDING_REVIEW;
      // 取消/失败后收尾仍可能补发 done：终态不能被改写成「已完成」
      const terminal =
        conv.taskStatus === TaskStatus.CANCELLED || conv.taskStatus === TaskStatus.FAILED;
      return {
        ...base,
        ...regenPatch,
        isLoading: preExecution ? conv.isLoading : false,
        taskStatus: terminal || preExecution ? conv.taskStatus : TaskStatus.DONE,
      };
    }

    case 'status_change': {
      const incoming = toTaskStatus(event.status);
      // 失败/取消后 Rust 仍可能按「流已消费完」补发 pending_review/done：
      // 终态不能被覆盖，否则错误被显示成「已完成」，停止键也会重新出现
      const settled =
        conv.taskStatus === TaskStatus.CANCELLED || conv.taskStatus === TaskStatus.FAILED;
      const status = settled ? conv.taskStatus : incoming;
      const meta: TaskMetaView = {
        waitReason: event.waitReason ?? undefined,
        queuePosition: event.queuePosition,
        statusReasonCode: event.statusReasonCode,
      };
      const releaseLoading =
        incoming === TaskStatus.BUDGET_PAUSED ||
        incoming === TaskStatus.CANCELLED ||
        incoming === TaskStatus.FAILED ||
        incoming === TaskStatus.DONE;
      // 计划流收尾后任务才真正进入待审阅：这是批准能被接受的时机
      const taskId = eventTaskId ?? conv.taskId;
      if (!settled && incoming === TaskStatus.PENDING_REVIEW && taskId) {
        void approvePlanWhenReady(taskId, (message) => reportAutoApproveFailure(conv.id, message));
      }
      return {
        ...base,
        taskStatus: status ?? conv.taskStatus,
        taskMeta: meta,
        ...(releaseLoading ? { isLoading: false } : {}),
      };
    }

    case 'quota_warning': {
      return {
        ...base,
        quotaSnapshot: {
          usedTokens: event.usedTokens,
          quotaTokens: event.quotaTokens,
          tier: event.level,
          action: event.action,
        },
      };
    }

    // 引擎收尾时下发：resumable=true 才能续跑。同一任务续跑成功后会被新的收尾事件覆盖为 false。
    case 'checkpoint':
      return { ...base, checkpoint: event };

    case 'heartbeat':
      return null; // 心跳仅保活，不产生 UI 变更

    case 'error':
      // 引擎事件带 message（多为 `错误码: 详情`）时优先展示详情，否则按 code 映射
      // 错误即本轮执行终止：不落到 failed 会让停止键一直可见，点了也没有任务可取消
      return {
        ...base,
        isLoading: false,
        taskStatus:
          conv.taskStatus === TaskStatus.CANCELLED ? conv.taskStatus : TaskStatus.FAILED,
        error: event.message ? errorTextOf(event.message) : errorMessageOf(event.code),
      };

    default:
      return base;
  }
}

// ── Store 实现 ────────────────────────────────────────────────

export const useWorkbenchStore = create<WorkbenchState>((set, get) => {
  /** 更新指定会话；激活会话同步刷新镜像与摘要 */
  const patchConv = (convId: string, patch: Partial<ConversationState>) => {
    set((s) => {
      const conv = s.convs[convId];
      if (!conv) return {};
      const convs = { ...s.convs, [convId]: { ...conv, ...patch } };
      const mirrors = convId === s.activeId ? mirrorOf(convs[convId]) : {};
      return { convs, ...mirrors, conversations: summariesOf(convs, s.convOrder) };
    });
  };

  // 自动批准重试耗尽：把失败原因写回本轮，不能让界面停在停止键上等一个不会来的回复；
  // 已经取消/失败的任务保持原终态，不被迟到的批准错误覆盖
  reportAutoApproveFailure = (convId, message) => {
    const conv = get().convs[convId];
    if (!conv) return;
    if (conv.taskStatus === TaskStatus.CANCELLED || conv.taskStatus === TaskStatus.FAILED) return;
    patchConv(convId, {
      isLoading: false,
      taskStatus: TaskStatus.FAILED,
      error: message,
    });
  };

  const ensureRouter = () => {
    if (routerRegistered) return;
    routerRegistered = true;
    api.chat.onEvent((event: ChatEvent) => {
      const s = get();
      // 部分事件变体未声明 taskId，宽松读取用于路由
      const evTaskId = (event as { taskId?: string }).taskId;
      let target = Object.values(s.convs).find((c) => evTaskId != null && c.taskId != null && c.taskId === evTaskId);
      if (!target) {
        // 提交响应未落 taskId 前的竞态兜底：路由给正在加载的会话
        target = Object.values(s.convs).find((c) => c.isLoading);
      }
      if (!target) return;
      // 定位目标轮次：taskTurnMap 精确命中（支持指定轮次重新生成），否则归入最后一轮
      const turnIdx =
        evTaskId != null && target.taskTurnMap[evTaskId] != null
          ? target.taskTurnMap[evTaskId]
          : target.turnEvents.length - 1;
      if (event.type === 'deliver') {
        // 回复正文落库（Mock / 浏览器直连引擎；桌面端由 Rust 写 SQLite，此处自动跳过）。
        // 隐私任务不写：本轮内容只留在当前内存态。
        const deliver = event as { final_text?: string; summary?: string; artifacts?: string[] };
        if (!privacyTasks.has(evTaskId ?? '')) {
          const content = deliver.final_text?.trim() ? deliver.final_text : deliver.summary ?? '';
          recordConversationMessage({
            convId: target.id,
            turnIndex: turnIdx,
            role: 'assistant',
            taskId: evTaskId ?? '',
            content,
            artifacts: deliver.artifacts ?? [],
            tsMs: Date.now(),
          });
        }
        // 协作任务：先暂存正文与产物，done 时再回报（done 事件不带内容）
        captureCollabDeliver(
          target.id,
          deliver.final_text?.trim() || deliver.summary || '',
          deliver.artifacts ?? [],
        );
      }
      const patch = applyEvent(target, event, turnIdx, evTaskId);
      if (patch) patchConv(target.id, patch);
      // 协作任务出错：本轮不回报成功（失败/取消也会收到 done，不能只看 done）
      if (event.type === 'error') captureCollabFailure(target.id);
      // 协作任务收尾：回报结果摘要与产物文件名；回报失败进待回报队列（不假装已同步）
      if (event.type === 'done') {
        void completeCollabRun(target.id).catch((err) => {
          console.warn('[collab] 结果回报失败，已进入待回报队列', err);
        });
      }
      // 系统通知/提示音：任务到了需要用户回来的节点才响（设置关了就完全静默）
      const notice = noticeKindFor(event);
      if (notice) void notifyForEvent(notice, target.title || '任务', evTaskId ?? null);
      // 本轮已结束：隐私标记不再需要（任务 id 全局唯一，不会复用）
      if (event.type === 'done' && evTaskId) privacyTasks.delete(evTaskId);
    });
  };

  /** 补齐重建会话的过程明细：按本轮任务回放事件流（24h 窗口外如实为空，不造假） */
  const hydrateProcess = async (convId: string) => {
    if (hydrating.has(convId)) return;
    hydrating.add(convId);
    try {
      const conv = get().convs[convId];
      if (!conv?.pendingProcess) return;
      const rows = await fetchConversationMessages(convId, 200);
      const turns = groupTurns(rows);
      const latest = get().convs[convId];
      // 回放期间用户可能已经在这个会话里发起新任务：轮次对不上就说明界面已是最新，不覆盖
      if (!latest || latest.isLoading || latest.turnEvents.length !== turns.length) return;
      const processByTask: Record<string, ChatEvent[]> = {};
      for (const t of turns) {
        if (!t.taskId) continue;
        const resp = await api.p0
          .eventReplay(t.taskId)
          .catch(() => ({ taskId: t.taskId, events: [] as unknown[] }));
        const evts = replayProcessEvents(resp);
        if (evts.length) processByTask[t.taskId] = evts;
      }
      const after = get().convs[convId];
      if (!after || after.isLoading || after.turnEvents.length !== turns.length) return;
      // 检查点已持久化到本机：刷新/重启后按最后一轮任务查一次摘要，恢复「继续」入口
      const latestTaskId = turns[turns.length - 1]?.taskId;
      const checkpointResp = latestTaskId
        ? await api.chat
            .checkpoints([latestTaskId])
            .catch(() => ({ checkpoints: {} as Record<string, CheckpointInfo> }))
        : { checkpoints: {} as Record<string, CheckpointInfo> };
      const finalConv = get().convs[convId];
      if (!finalConv || finalConv.isLoading || finalConv.turnEvents.length !== turns.length) return;
      patchConv(convId, {
        turnEvents: restoredTurnEvents(turns, processByTask),
        taskTurnMap: { ...finalConv.taskTurnMap, ...taskTurnMapOf(turns) },
        // 运行中的任务查询结果不可续跑；实时事件已写入的检查点优先保留
        checkpoint:
          finalConv.checkpoint ??
          (latestTaskId ? checkpointResp.checkpoints[latestTaskId] ?? null : null),
        pendingProcess: false,
      });
    } catch {
      // 过程明细补齐失败不影响正文展示，会话保持「仅正文」
    } finally {
      hydrating.delete(convId);
    }
  };

  return {
    convs: {},
    convOrder: [],
    activeId: null,
    conversations: [],
    ...mirrorOf(undefined),
    historyTasks: [],
    planVisible: false,
    filePanelOpen: true,
    chatModelId: 'auto',
    chatExpertId: '',
    workMode: null,

    // ── 会话管理 ──────────────────────────────────────────────

    newConversation: () => {
      const s = get();
      const active = s.activeId ? s.convs[s.activeId] : undefined;
      // 当前是空会话则复用，避免产生大量空会话
      if (active && active.messages.length === 0 && !active.isLoading) {
        patchConv(active.id, { error: null });
        return;
      }
      const conv = emptyConv();
      set((st) => ({
        convs: { ...st.convs, [conv.id]: conv },
        convOrder: [conv.id, ...st.convOrder],
        activeId: conv.id,
        conversations: summariesOf({ ...st.convs, [conv.id]: conv }, [conv.id, ...st.convOrder]),
        ...mirrorOf(conv),
        planVisible: false,
        error: null,
      }));
    },

    switchConversation: (id: string) => {
      const s = get();
      const conv = s.convs[id];
      if (!conv || id === s.activeId) return;
      set({ activeId: id, ...mirrorOf(conv) });
      // 刷新后重建出来的会话：首次切到时按任务回放事件流补齐「执行过程」明细
      if (conv.pendingProcess) void hydrateProcess(id);
    },

    // ── 归档会话（task:archive 的生产者） ────────────────────

    archiveConversation: async (id: string) => {
      const s = get();
      const conv = s.convs[id];
      if (!conv) return;
      // 会话未提交过任务时以会话 id 为归档键，保证本地通道仍可标记与恢复
      await archiveTask(conv.taskId ?? id, { title: conv.title });

      const nextOrder = s.convOrder.filter((k) => k !== id);
      const nextConvs: Record<string, ConversationState> = {};
      Object.entries(s.convs).forEach(([k, v]) => {
        if (k !== id) nextConvs[k] = v;
      });
      const activeId = s.activeId === id ? null : s.activeId;
      set((st) => ({
        convs: nextConvs,
        convOrder: nextOrder,
        activeId,
        conversations: summariesOf(nextConvs, nextOrder),
        ...mirrorOf(activeId ? nextConvs[activeId] : undefined),
        planVisible: false,
        error: st.error,
      }));
      if (nextOrder.length === 0) get().newConversation();
    },

    // ── 提交目标 ──────────────────────────────────────────────

    submitGoal: async (goal: string, attachments?: import('../../shared/types').AttachmentMeta[], modelBinding?: string, workDir?: string, modelId?: string, mode?: WorkMode, expertId?: string) => {
      ensureRouter();
      let s = get();
      // 无激活会话则先创建（直接进入的兜底路径）
      if (!s.activeId || !s.convs[s.activeId]) {
        get().newConversation();
        s = get();
      }
      const convId = s.activeId as string;

      const userMsg: Message = {
        id: `msg-${Date.now()}-user`,
        taskId: get().convs[convId].taskId ?? '',
        role: Role.USER,
        content: goal,
        ts: new Date(),
      };

      const conv = get().convs[convId];
      // 本轮轮次号（0 起）：正文库与后端按它定位本轮回复，重新生成时覆盖同一轮
      const turnIndex = conv.turnEvents.length;
      const userTs = Date.now();

      // 组装此前轮次对话历史（同会话上下文保持；截断压缩由引擎侧完成）
      const history: Array<{ role: 'user' | 'assistant'; content: string }> = [];
      const userMsgs = conv.messages.filter((m) => m.role === Role.USER);
      userMsgs.forEach((m, i) => {
        const content = typeof m.content === 'string' ? m.content : '';
        if (content) history.push({ role: 'user', content });
        const evts = conv.turnEvents[i] ?? [];
        const deliver = [...evts].reverse().find((e) => e.type === 'deliver');
        const finalText = deliver ? (deliver as { final_text?: string }).final_text : undefined;
        if (finalText) history.push({ role: 'assistant', content: finalText });
      });

      patchConv(convId, {
        messages: [...conv.messages, userMsg],
        events: [],
        // 新一轮对话：新建事件分组，历史轮次保留（多轮流式顺序）
        turnEvents: [...conv.turnEvents, []],
        plan: null,
        fileOps: [],
        taskMeta: {},
        checkpoint: null,
        // 新任务从干净状态开始：上一轮的 failed/cancelled 不能带进这一轮
        taskStatus: null,
        isLoading: true,
        error: null,
        workMode: mode ?? 'general',
        // 记住这一轮提交时的模型选择：重新生成要回放当轮模型，而不是当前输入框的选择
        turnModels: {
          ...conv.turnModels,
          [turnIndex]: {
            modelBinding: modelBinding?.trim() || undefined,
            modelId: modelId?.trim() || undefined,
          },
        },
        // 重新生成要沿用当轮负责人专家，而不是当前输入框里的选择
        turnExperts: {
          ...conv.turnExperts,
          [turnIndex]: expertId?.trim() || '',
        },
        title: conv.messages.length === 0 ? goal.slice(0, 20) : conv.title,
      });

      // 隐私模式：本轮提问与回复都不写本地台账（桌面端由 Rust 同步跳过 SQLite）
      const settings = await getSettings().catch(() => null);
      const privacy = Boolean(settings?.privacyMode);

      // 提问正文先落库（桌面端由 Rust 随任务写入，这里服务 Mock / 浏览器直连引擎）：
      // 即便随后提交失败，刷新后这轮提问仍在会话里，与界面所见一致
      if (!privacy) {
        recordConversationMessage({
          convId,
          turnIndex,
          role: 'user',
          taskId: '',
          content: goal,
          artifacts: [],
          tsMs: userTs,
        });
      }

      try {
        // 提交前合规闸门：判定与真实出站同源，被策略拒绝时不发起任务（错误进 conv.error）。
        // 端点从权威模型清单按 id 解析（渲染层不持有密钥，只借用非机密的 baseUrl 判出域）
        await api.compliance.check(goal, await modelBaseUrl(modelId));
        // 安全工作环境开关：桌面端 Rust 自读 settings_kv，引擎直连由此处随请求下发
        const secureEnv = settings?.secureEnv;
        const { taskId } = await api.chat.submit({
          // 每一轮都是独立任务：浏览器直连也据此新建引擎会话，
          // 避免上一轮选中的模型配置在下一次提交里被沿用
          workspaceId: getActiveWorkspaceId(),
          goal,
          mode,
          attachments,
          modelBinding,
          modelId,
          expertId: expertId?.trim() || undefined,
          workDir,
          secureEnv,
          // 桌面端由 Rust 从设置表装配 runtime；浏览器直连靠这一项把档位带给引擎
          piiOutboundMode: settings?.piiOutboundMode,
          // 同上：桌面端以 runtime.agentPolicy 为准，浏览器直连走请求顶层
          agentPolicy: {
            maxIters: settings?.agentMaxIters,
            autoCompress: settings?.agentAutoCompress,
            lazyMcpTools: settings?.agentLazyMcpTools,
          },
          // 浏览器直连只能带内置主题；桌面端的导入模板由 Rust runtime 下发
          pptxTemplates:
            mode === 'slides' ? browserPptxTemplates(settings?.pptxTemplateId) : undefined,
          history: history.length > 0 ? history : undefined,
          conversationId: convId,
          turnIndex,
        });
        // 记录 taskId → 轮次映射（后续事件精确归档到本轮）
        const latest = get().convs[convId];
        patchConv(convId, {
          taskId,
          taskTurnMap: { ...latest.taskTurnMap, [taskId]: latest.turnEvents.length - 1 },
        });
        // 隐私任务在整个生命周期内都不补写正文（见 privacyTasks 注释）
        if (privacy) privacyTasks.add(taskId);
        // 本轮任务 id 回填正文行（过程明细按它回放）
        if (!privacy) {
          recordConversationMessage({
            convId,
            turnIndex,
            role: 'user',
            taskId,
            content: goal,
            artifacts: [],
            tsMs: userTs,
          });
        }
      } catch (err: unknown) {
        patchConv(convId, {
          isLoading: false,
          error: errorTextOf(err, '提交任务失败'),
        });
      }
    },

    // ── 重新生成指定轮次的回复 ────────────────────────

    regenerateTurn: async (turnIdx) => {
      ensureRouter();
      const s = get();
      const conv = s.activeId ? s.convs[s.activeId] : undefined;
      if (!conv) return;
      const userMsgs = conv.messages.filter((m) => m.role === Role.USER);
      const goal = (userMsgs[turnIdx]?.content as string | undefined) ?? '';
      if (!goal || turnIdx >= conv.turnEvents.length || conv.regeneratingTurn != null) return;

      const isLast = turnIdx === conv.turnEvents.length - 1;
      // 重置该轮事件流；最后一轮同步清理计划/状态，中间轮仅重置事件分组
      patchConv(conv.id, {
        turnEvents: conv.turnEvents.map((g, i) => (i === turnIdx ? [] : g)),
        regeneratingTurn: turnIdx,
        ...(isLast ? { plan: null, fileOps: [], taskMeta: {}, checkpoint: null, isLoading: true, error: null, taskStatus: null } : {}),
      });

      try {
        // 重新生成同样把原文发给模型：合规闸门一并生效
        await api.compliance.check(goal);
        const settings = await getSettings().catch(() => null);
        const secureEnv = settings?.secureEnv;
        const privacy = Boolean(settings?.privacyMode);
        // 当轮有记录就回放当轮模型；刷新后的旧轮次没有记录，按当前选择兜底
        const model = conv.turnModels[turnIdx] ?? chatModelSelectionOf(get().chatModelId);
        // 当轮负责人专家：有记录就沿用；没有记录按通用助手重跑
        const expertId = conv.turnExperts[turnIdx] || undefined;
        // 带原会话键与轮次号提交：后端覆盖的是「同一轮的回复」，而不是新增一轮
        const { taskId } = await api.chat.submit({
          goal,
          // 重新生成沿用原会话模式：幻灯片任务不该在通用模式下重跑成 Markdown
          mode: conv.workMode ?? 'general',
          workspaceId: getActiveWorkspaceId(),
          secureEnv,
          piiOutboundMode: settings?.piiOutboundMode,
          agentPolicy: {
            maxIters: settings?.agentMaxIters,
            autoCompress: settings?.agentAutoCompress,
            lazyMcpTools: settings?.agentLazyMcpTools,
          },
          pptxTemplates:
            conv.workMode === 'slides'
              ? browserPptxTemplates(settings?.pptxTemplateId)
              : undefined,
          modelBinding: model.modelBinding,
          modelId: model.modelId,
          expertId,
          conversationId: conv.id,
          turnIndex: turnIdx,
        });
        patchConv(conv.id, {
          taskId,
          taskTurnMap: { ...get().convs[conv.id].taskTurnMap, [taskId]: turnIdx },
        });
        if (privacy) privacyTasks.add(taskId);
        // 本地通道同样把本轮重新绑到新任务（与 Rust 主键覆盖语义一致）；
        // 隐私模式下不写台账（Rust 侧同样跳过）
        if (!privacy) {
          recordConversationMessage({
            convId: conv.id,
            turnIndex: turnIdx,
            role: 'user',
            taskId,
            content: goal,
            artifacts: [],
            tsMs: Date.now(),
          });
        }
      } catch (err: unknown) {
        patchConv(conv.id, {
          regeneratingTurn: null,
          error: errorTextOf(err, '重新生成失败'),
          ...(isLast ? { isLoading: false } : {}),
        });
      }
    },

    // ── 回复评价（点赞/点踩） ──────────────────────────────

    setTurnFeedback: async (convId: string, turnIdx: number, kind: 'up' | 'down' | null) => {
      const conv = get().convs[convId];
      if (!conv) return;
      const before = conv.feedbackByTurn[turnIdx] ?? null;
      if (before === kind) return;
      const next = { ...conv.feedbackByTurn };
      if (kind) next[turnIdx] = kind;
      else delete next[turnIdx];
      patchConv(convId, { feedbackByTurn: next });
      try {
        await saveFeedback(convId, turnIdx, kind);
      } catch (err: unknown) {
        // 落库失败回滚界面状态，避免留下一个刷新后就消失的选中态
        const rollback = { ...get().convs[convId]?.feedbackByTurn };
        if (before) rollback[turnIdx] = before;
        else delete rollback[turnIdx];
        patchConv(convId, { feedbackByTurn: rollback });
        throw err instanceof Error ? err : new Error(String(err));
      }
    },

    // ── 审阅计划（免审阅模式下一般不触发，保留兼容） ────────────

    reviewPlan: async (action, patch) => {
      const s = get();
      const conv = s.activeId ? s.convs[s.activeId] : undefined;
      const plan = conv?.plan;
      const taskId = conv?.taskId;
      if (!plan || !taskId || !conv) return;

      try {
        await api.chat.review(taskId, { planId: plan.id, action, patch });
        if (action === 'approve') {
          patchConv(conv.id, {
            plan: { ...plan, status: PlanStatus.APPROVED, reviewed: true },
            isLoading: true,
            taskStatus: TaskStatus.EXECUTING,
          });
        } else if (action === 'reject') {
          patchConv(conv.id, {
            plan: { ...plan, status: PlanStatus.REJECTED, reviewed: false },
            isLoading: false,
            taskStatus: TaskStatus.DONE,
            taskMeta: { statusReasonCode: 'REJECTED' },
          });
        } else {
          set({ planVisible: true });
        }
      } catch (err: unknown) {
        patchConv(conv.id, { error: errorTextOf(err, '审阅失败') });
      }
    },

    // ── 从检查点继续（同一 taskId 重跑，不新增对话轮次） ──────────

    resumeTask: async () => {
      const s = get();
      const conv = s.activeId ? s.convs[s.activeId] : undefined;
      const taskId = conv?.taskId;
      if (!conv || !taskId) return;
      // 运行中的任务不能再起一条恢复流；检查点不属于当前任务时不展示/不恢复
      if (conv.isLoading || !conv.checkpoint?.resumable || conv.checkpoint.taskId !== taskId) return;

      patchConv(conv.id, { isLoading: true, error: null, taskMeta: {} });

      try {
        await api.chat.review(taskId, { action: 'resume' });
      } catch (err: unknown) {
        // 恢复没启动成功：保留检查点与入口，把不可恢复的真实原因显示出来
        patchConv(conv.id, { isLoading: false, error: errorTextOf(err, '从检查点恢复失败') });
      }
    },

    // ── 控制任务 ──────────────────────────────────────────────

    controlTask: async (action, stepId) => {
      const s = get();
      const conv = s.activeId ? s.convs[s.activeId] : undefined;
      const taskId = conv?.taskId;
      if (!taskId || !conv) return;

      try {
        await api.chat.control({ taskId, action, stepId });
        if (action === 'pause') {
          patchConv(conv.id, { taskStatus: TaskStatus.PAUSED, taskMeta: { waitReason: 'user_pause' } });
        } else if (action === 'resume') {
          patchConv(conv.id, { taskStatus: TaskStatus.EXECUTING, taskMeta: {}, isLoading: true });
        } else if (action === 'rollback_to') {
          patchConv(conv.id, { taskStatus: TaskStatus.ROLLED_BACK, isLoading: false });
        }
      } catch (err: unknown) {
        patchConv(conv.id, { error: errorTextOf(err, '控制任务失败') });
      }
    },

    // ── 取消任务 ──────────────────────────────────────────────

    cancelTask: async () => {
      const s = get();
      const conv = s.activeId ? s.convs[s.activeId] : undefined;
      const taskId = conv?.taskId;
      if (!taskId || !conv) return;

      try {
        const result = await api.p0.cancel(taskId);
        if (result.cancelled) {
          patchConv(conv.id, {
            isLoading: false,
            taskStatus: TaskStatus.CANCELLED,
            taskMeta: { statusReasonCode: 'USER_CANCELLED' },
          });
          return;
        }
        // 后端判定任务已终态，不会改写状态：界面必须自己退出停止态，
        // 否则按钮一直可点、点下去只是再收到一次 cancelled=false（看起来就是「没反应」）
        const settled = result.status ? toTaskStatus(result.status) : null;
        patchConv(conv.id, {
          isLoading: false,
          ...(settled ? { taskStatus: settled } : {}),
        });
        useAppStore.getState().showNotification('任务已经结束，无需终止', 'info');
      } catch (err: unknown) {
        patchConv(conv.id, { error: errorTextOf(err, '取消任务失败') });
      }
    },

    // ── 历史加载：任务列表（兼容字段）+ 会话重建（刷新/重启不丢对话） ──

    loadHistory: async () => {
      try {
        const list = await api.chat.history();
        set({ historyTasks: list });
      } catch {
        // 历史加载失败不阻断主流程
      }

      const rows = await fetchConversations().catch(() => []);
      if (!rows.length) return;
      // 归档的会话不回任务列表（正文仍在库内，归档页可恢复）
      const archived = await listArchived().catch(() => []);
      const archivedKeys = new Set(archived.map((a) => a.taskId).filter(Boolean));
      const visible = rows.filter(
        (r) => !archivedKeys.has(r.convId) && !(r.lastTaskId && archivedKeys.has(r.lastTaskId))
      );
      const bodies = await Promise.all(
        visible.map(async (r) => {
          try {
            return { row: r, turns: groupTurns(await fetchConversationMessages(r.convId, 200)) };
          } catch {
            return { row: r, turns: [] as RestoredTurn[] };
          }
        })
      );

      const s = get();
      const convs = { ...s.convs };
      const order = [...s.convOrder];
      let changed = false;
      for (const { row, turns } of bodies) {
        // 本次运行已存在的会话（含运行中）以内存态为准，不用库内数据覆盖
        if (convs[row.convId]) continue;
        // 没有任何提问记录的轮次无法作为对话轮渲染（旧版本任务只落了回复）：不硬凑一条假提问
        if (!turns.some((t) => t.userContent)) continue;
        convs[row.convId] = restoredConv(row.convId, titleOf(row.firstGoal), row.createdAtMs, turns);
        if (!order.includes(row.convId)) order.push(row.convId);
        changed = true;
      }
      if (!changed) return;
      // 侧边栏按最近活跃倒序（与会话列表口径一致）
      const sorted = [...order].sort((a, b) => (convs[b]?.createdAt ?? 0) - (convs[a]?.createdAt ?? 0));
      set({ convs, convOrder: sorted, conversations: summariesOf(convs, sorted) });
    },

    // ── UI 切换 ───────────────────────────────────────────────

    togglePlanPanel: () => set((s) => ({ planVisible: !s.planVisible })),
    toggleFilePanel: () => set((s) => ({ filePanelOpen: !s.filePanelOpen })),
    setChatModelId: (id: string) => set({ chatModelId: id }),
    setChatExpertId: (id: string) => set({ chatExpertId: id }),
    setWorkMode: (mode: WorkMode) => set({ workMode: mode }),

    clearError: () => {
      const s = get();
      if (s.activeId && s.convs[s.activeId]) {
        patchConv(s.activeId, { error: null });
      } else {
        set({ error: null });
      }
    },

    reset: () => {
      const conv = emptyConv();
      set({
        convs: { [conv.id]: conv },
        convOrder: [conv.id],
        activeId: conv.id,
        conversations: [],
        ...mirrorOf(conv),
        historyTasks: [],
        planVisible: false,
        filePanelOpen: true,
      });
    },

    /**
     * 修复历史消息显示：把内存里的会话按正文库（chat_messages）重建一遍。
     * 用于「消息看起来缺失/重复」时恢复显示 —— 只重排渲染态，不写库、不改内容。
     * 正在运行的会话不动（事件流还在实时追加，回放反而会打断），
     * 库里查不到的会话也保留（可能是本次运行刚建、正文尚未落库），不做删除。
     */
    repairConversationDisplay: async () => {
      const s = get();
      const rows = await fetchConversations(200).catch(() => null);
      if (!rows) throw new Error('CONVERSATION_STORE_UNAVAILABLE: 会话正文库读取失败，无法重建显示');
      const archived = await listArchived().catch(() => []);
      const archivedKeys = new Set(archived.map((a) => a.taskId).filter(Boolean));
      const convs: Record<string, ConversationState> = {};
      const order: string[] = [];
      let rebuilt = 0;
      let keptRunning = 0;
      for (const row of rows) {
        if (archivedKeys.has(row.convId) || (row.lastTaskId && archivedKeys.has(row.lastTaskId))) continue;
        const existing = s.convs[row.convId];
        if (existing?.isLoading) {
          convs[row.convId] = existing;
          order.push(row.convId);
          keptRunning += 1;
          continue;
        }
        const turns = groupTurns(await fetchConversationMessages(row.convId, 200).catch(() => []));
        if (!turns.some((t) => t.userContent)) continue;
        convs[row.convId] = restoredConv(row.convId, titleOf(row.firstGoal), row.createdAtMs, turns);
        order.push(row.convId);
        rebuilt += 1;
      }
      // 内存里有、库里没有的：保留（本次运行新建或正文未落库），不静默丢弃
      let keptUnsaved = 0;
      for (const c of Object.values(s.convs)) {
        if (convs[c.id]) continue;
        convs[c.id] = c;
        order.push(c.id);
        keptUnsaved += 1;
      }
      const sorted = [...order].sort((a, b) => (convs[b]?.createdAt ?? 0) - (convs[a]?.createdAt ?? 0));
      const activeId = sorted.includes(s.activeId ?? '') ? s.activeId : sorted[0] ?? null;
      set({
        convs,
        convOrder: sorted,
        conversations: summariesOf(convs, sorted),
        activeId,
        ...(activeId && convs[activeId] ? mirrorOf(convs[activeId]) : {}),
      });
      return { rebuilt, keptRunning, keptUnsaved, total: sorted.length };
    },
  };
});
