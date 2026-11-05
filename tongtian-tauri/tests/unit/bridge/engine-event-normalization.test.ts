/**
 * 引擎直连通道的事件字段归一（D-15 前置）。
 *
 * 引擎 SSE 的 data 是 snake_case，渲染层契约是 camelCase；桌面端有 Rust 改写，
 * 浏览器直连时没人改。后果不是报错而是「匹配不上」：步骤找不到、计划卡片空、
 * 续跑取不到步骤身份。这里锁住归一后的形状，以及不破坏已有 camelCase 载荷。
 */

import { describe, it, expect } from 'vitest';
import { normalizeEngineEvent } from '../../../src/renderer/lib/engine-events';

describe('normalizeEngineEvent：步骤与计划键名', () => {
  it('step_start 的 step_id 变成 stepId，tool/description 原样保留', () => {
    const out = normalizeEngineEvent({
      type: 'step_start',
      step_id: 'step-3',
      stepType: 'tool_call',
      tool: 'write_text_file',
      description: '调用工具: write_text_file',
      seq: 12,
      taskId: 'task-1',
    });
    expect(out.stepId).toBe('step-3');
    expect(out).not.toHaveProperty('step_id');
    expect(out.tool).toBe('write_text_file');
    expect(out.description).toBe('调用工具: write_text_file');
  });

  it('step_done / step_failed 同样归一（续跑要看步骤状态就靠这两个事件）', () => {
    expect(normalizeEngineEvent({ type: 'step_done', step_id: 'step-2', output: 'ok' }).stepId).toBe('step-2');
    expect(normalizeEngineEvent({ type: 'step_failed', step_id: 'step-2', error: '超时' }).stepId).toBe('step-2');
  });

  it('plan 事件的 plan_id 与步骤 id/order 补齐成契约形状', () => {
    const out = normalizeEngineEvent({
      type: 'plan',
      plan_id: 'plan-task-1',
      steps: [
        { step_id: 'step-1', type: 'llm_call', description: '分析并处理' },
        { type: 'file_write', description: '产出交付物' },
      ],
    });
    expect(out.planId).toBe('plan-task-1');
    expect(out).not.toHaveProperty('plan_id');
    const steps = out.steps as Array<Record<string, unknown>>;
    expect(steps[0]).toMatchObject({ id: 'step-1', order: 1, type: 'llm_call' });
    // 引擎漏给 id 时按序号补，步骤仍然可被引用到
    expect(steps[1]).toMatchObject({ id: 'step-2', order: 2 });
  });

  it('已是 camelCase 的载荷（桌面端转发后）原样通过，不重复改名', () => {
    const out = normalizeEngineEvent({ type: 'step_start', stepId: 'step-9', description: 'x' });
    expect(out.stepId).toBe('step-9');
    expect(out).not.toHaveProperty('step_id');
  });

  it('不改动无关字段，也不修改入参', () => {
    const input = { type: 'deliver', taskId: 't', summary: '完成', artifacts: ['a.md'], final_text: '正文' };
    const out = normalizeEngineEvent(input);
    expect(out).toEqual(input);
    expect(out).not.toBe(input);
  });

  it('steps 不是数组时原样带出，不抛异常', () => {
    expect(normalizeEngineEvent({ type: 'plan', steps: null }).steps).toBeNull();
    expect(normalizeEngineEvent({ type: 'plan', steps: 'oops' }).steps).toBe('oops');
  });
});
