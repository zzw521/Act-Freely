/**
 * 发送 / 停止共用一个主按钮
 *
 * 运行中主按钮显示方形停止键并真正调用 cancelTask；空闲时是向上箭头发送。
 * 这里锁住接线，避免退回「图标变了但点击仍是发送」的死按钮。
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { useWorkbenchStore, type ConversationState } from '../../../src/renderer/store/workbenchStore';
import { useAppStore } from '../../../src/renderer/stores/useAppStore';
import { TaskStatus } from '../../../src/shared/types';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const chatInput = () => read('../../../src/renderer/components/ChatInput/index.tsx');
const workbench = () => read('../../../src/renderer/pages/Workbench/index.tsx');

describe('ChatInput 主按钮：发送与停止同键位', () => {
  it('接收 stoppable，并在任务运行中走停止而不是发送', () => {
    const src = chatInput();
    expect(src).toMatch(/stoppable\?:\s*boolean/);
    expect(src).toMatch(/if\s*\(stoppable\)\s*\{\s*onStop\?\.\(\);\s*return;\s*\}/);
    expect(src).toContain('onClick={handlePrimaryAction}');
    expect(src).not.toContain('onClick={handleSend}');
  });

  it('箭头与方形停止图标随状态切换', () => {
    const src = chatInput();
    expect(src).toContain("stoppable ? <StopIcon sx={{ fontSize: 18 }} /> : <ArrowUpwardIcon sx={{ fontSize: 18 }} />");
    expect(src).toContain("stoppable ? tr('停止任务')");
  });

  it('运行中由任务状态决定，而不是只看 loading 或计划是否收尾', () => {
    const src = workbench();
    expect(src).toContain('const runningTask =');
    expect(src).toContain('TaskStatus.QUEUED,');
    expect(src).toContain('TaskStatus.PLANNING,');
    expect(src).toContain('TaskStatus.PENDING_REVIEW,');
    expect(src).toContain('TaskStatus.EXECUTING,');
    // 计划对象只用于展示步骤；拿它兜停止态会让已结束的任务永远显示方块键
    expect(src).not.toContain('planActive');
    const wired = (src.match(/stoppable=\{runningTask\}/g) ?? []).length;
    expect(wired).toBe(2);
    const stops = (src.match(/onStop=\{\(\) => void cancelTask\(\)\}/g) ?? []).length;
    expect(stops).toBe(2);
  });

  it('取消后的收尾 done 不改写为已完成，计划流的 done 也不被当成任务收尾', () => {
    const src = read('../../../src/renderer/store/workbenchStore.ts');
    expect(src).toContain(
      'conv.taskStatus === TaskStatus.CANCELLED || conv.taskStatus === TaskStatus.FAILED',
    );
    // 引擎 done 只是事件流结束：计划流收尾时任务仍要进入审阅/执行
    expect(src).toContain('const preExecution =');
    expect(src).toContain('taskStatus: terminal || preExecution ? conv.taskStatus : TaskStatus.DONE');
  });

  it('错误事件落到 failed，迟到计划不复活已终态任务', () => {
    const src = read('../../../src/renderer/store/workbenchStore.ts');
    expect(src).toContain(
      'conv.taskStatus === TaskStatus.CANCELLED ? conv.taskStatus : TaskStatus.FAILED',
    );
    expect(src).toMatch(
      /case 'plan': \{[\s\S]*?if \(conv\.taskStatus === TaskStatus\.CANCELLED \|\| conv\.taskStatus === TaskStatus\.FAILED\) \{\s*return base;\s*\}/,
    );
  });

  it('后端判定任务已终态时，界面要退出停止态并给出提示', () => {
    const src = read('../../../src/renderer/store/workbenchStore.ts');
    expect(src).toContain('const settled = result.status ? toTaskStatus(result.status) : null;');
    expect(src).toContain("showNotification('任务已经结束，无需终止', 'info')");
    // Rust 要把真实终态带回渲染层，否则前端只能停在停止键上反复取消
    expect(read('../../../src-tauri/src/lib.rs')).toContain('"status": record.status,');
  });

  it('Mock 取消后不再派发后续事件', () => {
    const src = read('../../../src/renderer/lib/mock-api.ts');
    const guarded = (src.match(/if \(!task\.cancelled\) fire\(\)/g) ?? []).length;
    expect(guarded).toBe(2);
  });
});

function makeConv(over: Partial<ConversationState> = {}): ConversationState {
  return {
    id: 'conv-1',
    title: '测试任务',
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
    taskStatus: TaskStatus.EXECUTING,
    taskMeta: {},
    quotaSnapshot: null,
    checkpoint: null,
    isLoading: true,
    error: null,
    ...over,
  };
}

function seedConv(over: Partial<ConversationState> = {}) {
  const conv = makeConv(over);
  useWorkbenchStore.setState({
    convs: { [conv.id]: conv },
    convOrder: [conv.id],
    activeId: conv.id,
    conversations: [{ id: conv.id, title: conv.title, createdAt: conv.createdAt, isLoading: conv.isLoading }],
  });
  return conv;
}

function installCancel(chatCancel: (taskId: string) => Promise<unknown>) {
  (globalThis as Record<string, unknown>).window = { tongtianAPI: { chatCancel } };
}

describe('cancelTask：点停止必须改变界面状态', () => {
  beforeEach(() => {
    useWorkbenchStore.setState({
      convs: {},
      convOrder: [],
      activeId: null,
      conversations: [],
      taskId: null,
      isLoading: false,
      error: null,
    });
  });

  it('引擎接受取消 → 立即落 cancelled 并释放停止态', async () => {
    installCancel(async () => ({ taskId: 'task-1', cancelled: true }));
    seedConv();

    await useWorkbenchStore.getState().cancelTask();

    const conv = useWorkbenchStore.getState().convs['conv-1'];
    expect(conv.taskStatus).toBe(TaskStatus.CANCELLED);
    expect(conv.isLoading).toBe(false);
    expect(conv.taskMeta.statusReasonCode).toBe('USER_CANCELLED');
  });

  it('任务早已结束 → 用后端返回的终态退出停止态，并提示无需终止', async () => {
    installCancel(async () => ({ taskId: 'task-1', cancelled: false, status: 'done' }));
    seedConv();

    await useWorkbenchStore.getState().cancelTask();

    const conv = useWorkbenchStore.getState().convs['conv-1'];
    expect(conv.taskStatus).toBe(TaskStatus.DONE);
    expect(conv.isLoading).toBe(false);
    expect(useAppStore.getState().notification?.message).toBe('任务已经结束，无需终止');
  });
});
