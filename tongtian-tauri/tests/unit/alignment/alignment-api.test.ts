/**
 * 对齐整改通道 · 前端契约测试（任务 #16）
 *
 * 走真实分发路径：mock-api 注入 window.tongtianAPI → alignment-api 的
 * 「有 IPC 走 IPC，无 IPC 走本地权威」分支 → handleLocalAlignmentChannel。
 * 覆盖四个页面（/hooks、/extensions?tab=connectors、/archive、/computer-use）
 * 消费的全部整改接口，验证页面拿到的形状与错误语义，而非只验本地函数。
 */

import { describe, it, expect, beforeAll, beforeEach } from 'vitest';

(globalThis as Record<string, unknown>).window =
  (globalThis as Record<string, unknown>).window ?? {};

type AlignmentApi = typeof import('../../../src/renderer/lib/alignment-api');

let a: AlignmentApi;

beforeAll(async () => {
  await import('../../../src/renderer/lib/mock-api');
  a = await import('../../../src/renderer/lib/alignment-api');
});

beforeEach(async () => {
  const local = await import('../../../src/renderer/lib/alignment-local');
  local.resetLocalAlignment();
});

// ── Hooks 页 ─────────────────────────────────────────────────

describe('alignment-api · Hooks（页面消费面）', () => {
  it('未配置时返回空 hooks，页面据此渲染空态而非假配置', async () => {
    expect(await a.getHooks()).toEqual({ hooks: {} });
  });

  it('保存后读回同一份配置（含 matcher 与 timeout）', async () => {
    const cfg = {
      hooks: {
        PreToolUse: [{ matcher: 'Bash', hooks: [{ type: 'command' as const, command: 'echo check', timeout: 5 }] }],
        Stop: [{ hooks: [{ type: 'command' as const, command: 'echo stop' }] }],
      },
    };
    await a.setHooks(cfg);
    expect(await a.getHooks()).toEqual(cfg);
  });

  it('非法事件名保存被拒，原配置保持不变', async () => {
    await a.setHooks({ hooks: { Notification: [{ hooks: [{ command: 'echo n' }] }] } });
    await expect(a.setHooks({ hooks: { Typo: [{ hooks: [{ command: 'x' }] }] } })).rejects.toThrow(
      /HOOK_INVALID_EVENT/,
    );
    expect(await a.getHooks()).toEqual({
      hooks: { Notification: [{ hooks: [{ command: 'echo n' }] }] },
    });
  });

  it('试运行在浏览器通道如实抛错，不返回假 exitCode 0', async () => {
    await expect(a.testHook('Stop', 'echo hi')).rejects.toThrow(/HOOK_TEST_UNSUPPORTED/);
  });

  it('执行面概览初始为空态，并如实标注「当前通道不会执行」', async () => {
    const s = await a.getHooksStatus();
    expect(s).toMatchObject({ configured: 0, mountedEvents: [], ungranted: [], grants: [] });
    expect(s.firedEvents).toHaveLength(10);
    expect(s.unfiredEvents).toHaveLength(2);
    expect(s.gateError).toContain('不会执行');
  });

  it('配置 → 未放行 → 放行 → 撤销：页面读到的状态逐跳变化', async () => {
    await a.setHooks({ hooks: { PreToolUse: [{ hooks: [{ command: 'echo guard' }] }] } });
    expect((await a.getHooksStatus()).ungranted).toEqual(['echo guard']);

    await a.grantHook('PreToolUse', 'echo guard');
    const granted = await a.getHooksStatus();
    expect(granted.ungranted).toEqual([]);
    expect(granted.grants).toHaveLength(1);
    expect(granted.grants[0]).toMatchObject({ command: 'echo guard', event: 'PreToolUse', runCount: 0 });

    await a.revokeHook('echo guard');
    const revoked = await a.getHooksStatus();
    expect(revoked.grants).toEqual([]);
    // 撤权 ≠ 删配置：配置必须原样还在
    expect((await a.getHooks()).hooks.PreToolUse).toBeDefined();
  });

  it('无触发点事件被概览点名，页面才不会宣称「配了就会跑」', async () => {
    await a.setHooks({ hooks: { Notification: [{ hooks: [{ command: 'echo s' }] }] } });
    const s = await a.getHooksStatus();
    expect(s.mountedEvents).toEqual(['Notification']);
    expect(s.mountedUnfired).toEqual(['Notification']);
  });

  it('保存时已从配置删除的命令，其放行一并作废并回报', async () => {
    await a.setHooks({ hooks: { Stop: [{ hooks: [{ command: 'echo a' }] }] } });
    await a.grantHook('Stop', 'echo a');
    const res = await a.setHooks({ hooks: {} });
    expect(res).toMatchObject({ ok: true, events: 0, revoked: ['echo a'] });
    expect((await a.getHooksStatus()).grants).toEqual([]);
  });

  it('放行非法事件名如实抛错（避免留下永远用不上的授权记录）', async () => {
    await expect(a.grantHook('Typo' as never, 'echo x')).rejects.toThrow(/HOOK_INVALID_EVENT/);
  });
});

// ── 归档页 ───────────────────────────────────────────────────

describe('alignment-api · 任务归档（页面消费面）', () => {
  it('归档→列表→取消归档全链路，字段形状满足列表渲染', async () => {
    const item = await a.archiveTask('t-1', { title: '写周报', workspaceId: 'ws-a' });
    expect(item).toMatchObject({ taskId: 't-1', title: '写周报', workspaceId: 'ws-a' });
    expect(item.archivedAt).toMatch(/^\d{4}-\d{2}-\d{2}T/);

    expect((await a.listArchived()).map((r) => r.taskId)).toEqual(['t-1']);
    await a.unarchiveTask('t-1');
    expect(await a.listArchived()).toEqual([]);
  });

  it('彻底删除与清空遵循确认语义：无确认词不清空且不抛穿页面', async () => {
    await a.archiveTask('t-1', { title: 'A' });
    await a.archiveTask('t-2', { title: 'B' });

    expect(await a.clearArchived('')).toBe(false);
    expect(await a.listArchived()).toHaveLength(2);

    await a.deleteArchivedTask('t-1');
    expect((await a.listArchived()).map((r) => r.taskId)).toEqual(['t-2']);

    expect(await a.clearArchived('DELETE')).toBe(true);
    expect(await a.listArchived()).toEqual([]);
  });
});

// ── 电脑操控页 ───────────────────────────────────────────────

describe('alignment-api · 电脑操控策略（页面消费面）', () => {
  it('默认档为「每次询问」，切换后刷新不丢（同会话读回一致）', async () => {
    expect(await a.getComputerUsePolicy()).toBe('ask_every_time');
    await a.setComputerUsePolicy('auto');
    expect(await a.getComputerUsePolicy()).toBe('auto');
  });

  it('非法档位被拒，页面不会静默写入无效策略', async () => {
    await expect(a.setComputerUsePolicy('always' as never)).rejects.toThrow(
      /COMPUTER_USE_INVALID_POLICY/,
    );
    expect(await a.getComputerUsePolicy()).toBe('ask_every_time');
  });
});
