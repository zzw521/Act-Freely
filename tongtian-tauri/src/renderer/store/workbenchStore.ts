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
} from '../../shared/types';
import { Role, PlanStatus, StepStatus, FileOpType, FileOpStatus, TaskStatus } from '../../shared/types';
import type { QuotaSnapshot } from '../../shared/contracts';
import { errorMessageOf } from '../../shared/error-messages';
import { api } from '../lib/api';

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
  plan: Plan | null;
  fileOps: FileOp[];
  taskId: string | null;
  taskStatus: TaskStatus | null;
  taskMeta: TaskMetaView;
  quotaSnapshot: QuotaSnapshot | null;
  isLoading: boolean;
  error: string | null;
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
  isLoading: boolean;
  error: string | null;

  /** 历史任务列表（chat_history，保留兼容） */
  historyTasks: unknown[];
  /** 当前激活会话正在重新生成的轮次（镜像） */
  regeneratingTurn: number | null;

  // ── UI 状态 ──
  planVisible: boolean;
  filePanelOpen: boolean;

  // ── Actions ──
  /** 新任务：创建并激活新会话（当前为空会话则复用） */
  newConversation: () => void;
  /** 切换到指定会话 */
  switchConversation: (id: string) => void;
  submitGoal: (goal: string, attachments?: import('../../shared/types').AttachmentMeta[], modelBinding?: string, workDir?: string, modelConfig?: { baseUrl: string; apiKey: string; modelName: string }) => Promise<void>;
  /** 重新生成指定轮次的回复（重置该轮事件流后重新提交原目标） */
  regenerateTurn: (turnIdx: number) => Promise<void>;
  reviewPlan: (action: 'approve' | 'reject' | 'modify', patch?: Partial<Step>[]) => Promise<void>;
  controlTask: (action: 'pause' | 'resume' | 'rollback_to', stepId?: string) => Promise<void>;
  cancelTask: () => Promise<void>;
  loadHistory: () => Promise<void>;
  togglePlanPanel: () => void;
  toggleFilePanel: () => void;
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
    plan: null,
    fileOps: [],
    taskId: null,
    taskStatus: null,
    taskMeta: {},
    quotaSnapshot: null,
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
    plan: conv.plan,
    fileOps: conv.fileOps,
    taskId: conv.taskId,
    taskStatus: conv.taskStatus,
    taskMeta: conv.taskMeta,
    quotaSnapshot: conv.quotaSnapshot,
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

function applyEvent(conv: ConversationState, event: ChatEvent, turnIdx: number): Partial<ConversationState> | null {
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
      // 免审阅：计划产出后自动批准直接进入执行
      const plan = buildPlanFromEvent(event.planId, event.steps, conv.taskId ?? '');
      const approved: Plan = { ...plan, status: PlanStatus.APPROVED, reviewed: true };
      if (conv.taskId) {
        api.chat
          .review(conv.taskId, { planId: plan.id, action: 'approve' })
          .catch(() => undefined);
      }
      return { ...base, plan: approved, isLoading: true, taskStatus: TaskStatus.EXECUTING };
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
      return { ...base, ...regenPatch, isLoading: false, taskStatus: TaskStatus.DONE };
    }

    case 'status_change': {
      const status = toTaskStatus(event.status);
      const meta: TaskMetaView = {
        waitReason: event.waitReason ?? undefined,
        queuePosition: event.queuePosition,
        statusReasonCode: event.statusReasonCode,
      };
      const releaseLoading =
        status === TaskStatus.BUDGET_PAUSED ||
        status === TaskStatus.CANCELLED ||
        status === TaskStatus.FAILED ||
        status === TaskStatus.DONE;
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

    case 'heartbeat':
      return null; // 心跳仅保活，不产生 UI 变更

    case 'error':
      return { ...base, isLoading: false, error: errorMessageOf(event.code) };

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
      const patch = applyEvent(target, event, turnIdx);
      if (patch) patchConv(target.id, patch);
    });
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
    },

    // ── 提交目标 ──────────────────────────────────────────────

    submitGoal: async (goal: string, attachments?: import('../../shared/types').AttachmentMeta[], modelBinding?: string, workDir?: string, modelConfig?: { baseUrl: string; apiKey: string; modelName: string }) => {
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
        isLoading: true,
        error: null,
        title: conv.messages.length === 0 ? goal.slice(0, 20) : conv.title,
      });

      try {
        const { taskId } = await api.chat.submit({
          taskId: get().convs[convId].taskId ?? undefined,
          goal,
          attachments,
          modelBinding,
          workDir,
          modelConfig,
          history: history.length > 0 ? history : undefined,
        });
        // 记录 taskId → 轮次映射（后续事件精确归档到本轮）
        const latest = get().convs[convId];
        patchConv(convId, {
          taskId,
          taskTurnMap: { ...latest.taskTurnMap, [taskId]: latest.turnEvents.length - 1 },
        });
      } catch (err: unknown) {
        patchConv(convId, {
          isLoading: false,
          error: err instanceof Error ? err.message : '提交任务失败',
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
        ...(isLast ? { plan: null, fileOps: [], taskMeta: {}, isLoading: true, error: null, taskStatus: null } : {}),
      });

      try {
        const { taskId } = await api.chat.submit({ goal });
        patchConv(conv.id, {
          taskId,
          taskTurnMap: { ...get().convs[conv.id].taskTurnMap, [taskId]: turnIdx },
        });
      } catch (err: unknown) {
        patchConv(conv.id, {
          regeneratingTurn: null,
          error: err instanceof Error ? err.message : '重新生成失败',
          ...(isLast ? { isLoading: false } : {}),
        });
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
        patchConv(conv.id, { error: err instanceof Error ? err.message : '审阅失败' });
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
        patchConv(conv.id, { error: err instanceof Error ? err.message : '控制任务失败' });
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
        }
      } catch (err: unknown) {
        patchConv(conv.id, { error: err instanceof Error ? err.message : '取消任务失败' });
      }
    },

    // ── 历史任务加载（保留兼容） ──────────────────────────────

    loadHistory: async () => {
      try {
        const list = await api.chat.history();
        set({ historyTasks: list });
      } catch {
        // 历史加载失败不阻断主流程
      }
    },

    // ── UI 切换 ───────────────────────────────────────────────

    togglePlanPanel: () => set((s) => ({ planVisible: !s.planVisible })),
    toggleFilePanel: () => set((s) => ({ filePanelOpen: !s.filePanelOpen })),

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
  };
});
