/**
 * 任务控制面契约：只保留「开始 / 发送」与「终止」。
 *
 * 用户侧不再有暂停/继续入口；运行中由输入框主按钮直接终止。
 * 暂停状态仍留在冻结的 14 态状态机里（历史任务数据、预算暂停、契约测试都在用），
 * 这里只断言控制面不再暴露它，避免把状态定义误删。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const SRC = resolve(__dirname, '../../../src');
const read = (rel: string) => readFileSync(resolve(SRC, rel), 'utf8').replace(/\r\n/g, '\n');
const readRepo = (rel: string) =>
  readFileSync(resolve(__dirname, '../../..', rel), 'utf8').replace(/\r\n/g, '\n');

describe('任务控制面：只有终止，不再有暂停/继续', () => {
  it('工作台不再渲染暂停/继续按钮，运行中由输入框主按钮停止', () => {
    const page = read('renderer/pages/Workbench/index.tsx');
    expect(page).not.toContain('PauseCircleOutlineIcon');
    expect(page).not.toContain('canPause');
    // 只允许断点续跑（canResumeCheckpoint），不允许暂停后的「继续」
    expect(page).not.toMatch(/\bcanResume\b/);
    expect(page).not.toContain('controlTask');
    expect(page).toContain('onStop={() => void cancelTask()}');
    // 非运行但可取消的状态（待审阅/预算暂停）仍保留终止入口
    expect(page).toContain('canCancel && !runningTask');
  });

  it('store、API 与 IPC 通道不再暴露 chat:control', () => {
    expect(read('shared/contracts.ts')).not.toContain('CHAT_CONTROL');
    expect(read('renderer/store/workbenchStore.ts')).not.toContain('controlTask:');
    expect(read('renderer/lib/api.ts')).not.toContain('chatControl');
    expect(read('renderer/lib/engine-api.ts')).not.toContain('chatControl');
    expect(read('renderer/lib/tauri-api.ts')).not.toContain('chatControl');
    expect(read('shared/types.ts')).not.toContain('ChatControlReq');
  });

  it('Rust 不再提供 chat_control command，引擎不再有控制端点与暂停闸门', () => {
    const rs = readRepo('src-tauri/src/lib.rs');
    expect(rs).not.toContain('async fn chat_control(');
    expect(rs).not.toContain('chat_control,');
    const server = readRepo('engine/server.py');
    expect(server).not.toContain('/api/chat/control');
    expect(server).not.toContain('pause_event');
    expect(server).not.toContain('rolled_back');
  });

  it('暂停状态仍留在冻结状态机（历史数据与预算暂停不受影响）', () => {
    const types = read('shared/types.ts');
    expect(types).toContain("PAUSED = 'paused'");
    expect(types).toContain("BUDGET_PAUSED = 'budget_paused'");
    const sm = read('shared/task-state-machine.ts');
    expect(sm).toContain("event: 'pause'");
    expect(sm).toContain("event: 'resume'");
  });
});
