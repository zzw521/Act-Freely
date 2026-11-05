/**
 * 断点续跑的「将重放哪些步骤」判定层（纯函数，D-15）。
 *
 * 运行时语义（引擎与 Rust harness 一致，是这里全部推断的前提）：
 * - 检查点快照在**每次模型调用之前**采集，续跑回到最后一次快照，不是回到用户点的那一步；
 * - 只有登记进副作用台账（工具名 + 输入指纹）且已成功的调用会被跳过；只读与未知工具不入台账，
 *   续跑时会重新执行一次；
 * - 失败的步骤没有被登记过，因此一定会重做 —— 这正是需要向用户确认的那一次。
 *
 * 界面只能按步骤顺序近似（拿不到工具参数，无法复算指纹），所以每一条"不会重做"
 * 都必须带上"按指纹判定"的口径，不能写成保证。
 */

import type { ChatEvent } from './types';

/** 与 engine/checkpoint_runtime.py 的三张表一致（由契约测试锁住，不靠注释维持） */
export const READ_ONLY_TOOLS = ['list_directory', 'view_text_file', 'read_document', 'use_skill'] as const;
export const SIDE_EFFECT_TOOLS = [
  'write_text_file',
  'insert_text_file',
  'delete_text_file',
  'generate_pptx',
  'generate_xlsx',
  'generate_docx',
  'spawn_subagents',
] as const;
export const SIDE_EFFECT_KEYWORDS = [
  'write', 'create', 'delete', 'remove', 'move', 'rename', 'upload',
  'send', 'execute', 'insert', 'append', 'save', 'export', 'publish',
  'shell', 'command', 'run',
] as const;

export type ReplayRisk = 'readonly' | 'side_effect' | 'unknown';

/**
 * 保守判定：可能改变工作目录或外部状态的一律按副作用处理。
 * 未知工具（含未登记的 MCP 工具）算 unknown，界面按需要确认对待。
 */
export function toolReplayRisk(tool: string): ReplayRisk {
  const name = (tool || '').trim();
  if (!name) return 'unknown';
  if ((READ_ONLY_TOOLS as readonly string[]).includes(name)) return 'readonly';
  if ((SIDE_EFFECT_TOOLS as readonly string[]).includes(name)) return 'side_effect';
  const lowered = name.toLowerCase();
  return SIDE_EFFECT_KEYWORDS.some((kw) => lowered.includes(kw)) ? 'side_effect' : 'unknown';
}

const TOOL_CALL_DESC = /^调用工具:\s*(.+)$/;

/** 步骤对应的工具名：优先事件自带的 tool，其次从中文描述里取（Rust 转发会丢掉 tool 与 stepType） */
export function stepToolName(event: { tool?: string; description?: string }): string {
  const direct = (event.tool ?? '').trim();
  if (direct) return direct;
  const matched = TOOL_CALL_DESC.exec((event.description ?? '').trim());
  return matched ? matched[1].trim() : '';
}

export type ReplayStepState = 'done' | 'failed' | 'running' | 'unknown';

export interface ReplayStep {
  stepId: string;
  /** 步骤序号（本轮执行顺序，0 起） */
  order: number;
  /** 界面显示名：工具名，或内部步骤的描述 */
  label: string;
  tool: string;
  /** other = 引擎/harness 的内部步骤（上下文注入、检查点说明等），不计入重放承诺 */
  kind: 'tool' | 'other';
  risk: ReplayRisk;
  state: ReplayStepState;
  /** 是否就是本次续跑的锚点步骤 */
  anchor: boolean;
  willReplay: boolean;
  note: string;
}

export interface ReplayPlan {
  steps: ReplayStep[];
  anchorStepId: string;
  /** 已确认不会重做的步骤数（写进台账的已完成副作用工具） */
  skippedCount: number;
  /** 会重新执行的工具步骤数 */
  replayCount: number;
  /** 需要用户确认才会续跑：会重放的工具里有副作用或未知类别 */
  needsConfirm: boolean;
  confirmTools: string[];
  summaryZh: string;
  /** 判定依据与不确定性，界面原样展示，不做二次加工 */
  caveatZh: string;
}

const RISK_LABEL: Record<ReplayRisk, string> = {
  readonly: '只读',
  side_effect: '会改变文件或外部状态',
  unknown: '未登记的工具类别',
};

export function replayRiskLabel(risk: ReplayRisk): string {
  return RISK_LABEL[risk];
}

/** 从事件流里还原本轮的步骤序列：step_start 建行，step_done/step_failed 覆盖状态 */
export function collectReplaySteps(events: ChatEvent[]): ReplayStep[] {
  const rows: ReplayStep[] = [];
  const byId = new Map<string, ReplayStep>();
  for (const event of events) {
    if (event.type === 'step_start') {
      const tool = stepToolName(event);
      const row: ReplayStep = {
        stepId: event.stepId ?? '',
        order: rows.length,
        label: tool || event.description || event.stepId || '步骤',
        tool,
        kind: tool ? 'tool' : 'other',
        risk: toolReplayRisk(tool),
        state: 'running',
        anchor: false,
        willReplay: true,
        note: '',
      };
      rows.push(row);
      if (row.stepId && !byId.has(row.stepId)) byId.set(row.stepId, row);
      continue;
    }
    if (event.type !== 'step_done' && event.type !== 'step_failed') continue;
    const row = byId.get(event.stepId ?? '');
    if (!row) continue;
    row.state = event.type === 'step_failed' ? 'failed' : 'done';
  }
  return rows;
}

function noteFor(row: ReplayStep): string {
  if (row.kind === 'other') return '引擎内部步骤，不计入工具重放';
  if (row.anchor) {
    return row.state === 'failed'
      ? '失败步骤没有登记进副作用台账，续跑会重新执行这一次调用'
      : '本次续跑的锚点步骤，会重新执行';
  }
  if (row.state === 'failed') return '失败的调用不会被台账跳过，续跑会重新执行';
  if (row.state !== 'done') return '这一步没有确认完成，续跑会重新执行';
  if (row.risk === 'side_effect') return '已成功的副作用调用：按工具名与输入指纹登记在台账里，续跑不重做';
  if (row.risk === 'readonly') return '只读工具不写台账：续跑会再执行一次，但不改变状态';
  return '未登记类别的工具不写台账：续跑会再执行一次';
}

/**
 * 构建重放预览。anchorStepId 决定"从哪一步开始算未完成"：
 * 锚点之前的已完成副作用工具按台账跳过，其余都会重放。
 */
export function buildReplayPlan(events: ChatEvent[], anchorStepId: string): ReplayPlan {
  const steps = collectReplaySteps(events);
  const anchorIndex = steps.findIndex((s) => s.stepId === anchorStepId);
  const effectiveAnchor = anchorIndex >= 0 ? anchorIndex : steps.length - 1;
  let skippedCount = 0;
  let replayCount = 0;
  const confirmTools: string[] = [];

  for (const [i, row] of steps.entries()) {
    row.anchor = i === effectiveAnchor;
    const skipEligible =
      i < effectiveAnchor && row.state === 'done' && row.kind === 'tool' && row.risk === 'side_effect';
    row.willReplay = row.anchor ? true : !skipEligible;
    row.note = noteFor(row);
    if (skipEligible) skippedCount += 1;
    else if (row.kind === 'tool') replayCount += 1;
    if (row.willReplay && row.kind === 'tool' && row.risk !== 'readonly') {
      if (!confirmTools.includes(row.tool)) confirmTools.push(row.tool);
    }
  }

  const anchor = steps[effectiveAnchor];
  const summaryZh =
    steps.length === 0
      ? '本轮没有可展示的步骤记录'
      : `从检查点续跑：${skippedCount} 个已成功的写入类步骤按台账不重做，${replayCount} 个步骤会重新执行`;

  return {
    steps,
    anchorStepId: anchor?.stepId ?? anchorStepId,
    skippedCount,
    replayCount,
    needsConfirm: confirmTools.length > 0,
    confirmTools,
    summaryZh,
    caveatZh:
      '跳过与否由引擎按「工具名 + 输入参数指纹」判定，界面只能按步骤顺序近似；' +
      '续跑回到最后一次模型调用前的快照，不是回到所选步骤本身。',
  };
}

/** 确认框正文里列出的"将要重做的写操作" */
export function confirmWarningZh(tools: string[]): string {
  if (tools.length === 0) return '';
  return `以下工具会重新执行，可能再次改变文件或外部状态：${tools.join('、')}。` + '确认前请留意重复写入、重复提交与重复执行。';
}

/** 检查点不可用时的如实解释（按钮不该承诺一个跑不起来的操作） */
export function unavailableReasonZh(reason: string | undefined): string {
  const text = (reason ?? '').trim();
  return text || '引擎没有留下可续跑的检查点快照（任务可能已正常收尾，或中断发生在首次模型调用之前）。';
}

/** 从事件流里挑默认锚点：最后一个失败步骤，没有则最后一个有身份的步骤 */
export function defaultReplayAnchor(events: ChatEvent[]): string {
  const steps = collectReplaySteps(events);
  const failed = [...steps].reverse().find((s) => s.state === 'failed');
  if (failed) return failed.stepId;
  const last = [...steps].reverse().find((s) => s.stepId);
  return last?.stepId ?? '';
}
