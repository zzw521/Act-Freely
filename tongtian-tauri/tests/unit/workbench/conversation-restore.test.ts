/**
 * 会话正文恢复测试（刷新/重启不丢对话的前端数据层）
 *
 * vitest 环境为 node（无 window/DOM），此处手动 polyfill localStorage 后动态导入模块，
 * 覆盖三条通道里的两条非 Rust 通道（Mock / 浏览器直连引擎）与恢复组装纯函数：
 * - 台账读写与 Rust `chat_messages` 同口径（会话键 + 轮次号 + 角色，同键覆盖）
 * - 恢复组装（轮次分组、标题、事件分组、任务映射）
 * - 事件重放过滤（只取步骤类，plan 会触发自动批准故不重放）
 * - 通道分发（handleLocalConversationChannel）
 * 桌面通道由 Rust command 落库，测试只验证渲染层不再另存影子台账（不双写）。
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

type StoreMod = typeof import('../../../src/renderer/lib/conversation-store');
type LocalMod = typeof import('../../../src/renderer/lib/conversation-local');

const SRC = resolve(__dirname, '../../../src');
const read = (rel: string) => readFileSync(resolve(SRC, rel), 'utf8').replace(/\r\n/g, '\n');

function installFakeStorage(): Map<string, string> {
  const store = new Map<string, string>();
  (globalThis as Record<string, unknown>).localStorage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
  };
  // 浏览器宿主：无 __TAURI_INTERNALS__，即非桌面运行时（走本地台账）
  (globalThis as Record<string, unknown>).window = {};
  return store;
}

let store: StoreMod;
let localChannel: LocalMod;

beforeEach(async () => {
  installFakeStorage();
  vi.resetModules();
  store = await import('../../../src/renderer/lib/conversation-store');
  localChannel = await import('../../../src/renderer/lib/conversation-local');
});

const row = (
  convId: string,
  turnIndex: number,
  role: 'user' | 'assistant',
  content: string,
  taskId = `task-${convId}-${turnIndex}`,
  extra: Partial<{ artifacts: string[]; tsMs: number }> = {}
) => ({
  convId,
  turnIndex,
  role,
  taskId,
  content,
  artifacts: extra.artifacts ?? [],
  tsMs: extra.tsMs ?? turnIndex * 1000,
});

// ── 本地台账 ────────────────────────────────────────────────

describe('conversation-store：本地台账与 Rust 同口径', () => {
  it('同会话同轮同角色覆盖，不累积多个版本', () => {
    store.recordConversationMessage(row('conv-1', 0, 'assistant', '第一版回复', 'task-1'));
    store.recordConversationMessage(row('conv-1', 0, 'assistant', '第二版回复', 'task-2'));
    const rows = store.localMessages('conv-1');
    expect(rows).toHaveLength(1);
    expect(rows[0].content).toBe('第二版回复');
    expect(rows[0].taskId).toBe('task-2');
  });

  it('正文按轮次升序、同轮 user 先于 assistant（与 Rust ORDER BY 一致）', () => {
    store.recordConversationMessage(row('conv-1', 1, 'user', '第二轮提问'));
    store.recordConversationMessage(row('conv-1', 0, 'assistant', '首轮回复'));
    store.recordConversationMessage(row('conv-1', 0, 'user', '首轮提问'));
    store.recordConversationMessage(row('conv-1', 1, 'assistant', '第二轮回复'));
    expect(
      store.localMessages('conv-1').map((r) => [r.turnIndex, r.role] as [number, string])
    ).toEqual([[0, 'user'], [0, 'assistant'], [1, 'user'], [1, 'assistant']]);
  });

  it('会话列表：标题取首轮提问、按最近活跃倒序、turn_count 去重轮次', () => {
    store.recordConversationMessage(row('conv-a', 0, 'user', 'A 的首轮提问', 'task-a-0', { tsMs: 1000 }));
    store.recordConversationMessage(row('conv-a', 0, 'assistant', 'A 的回复', 'task-a-0', { tsMs: 2000 }));
    store.recordConversationMessage(row('conv-b', 0, 'user', 'B 的首轮', 'task-b-0', { tsMs: 3000 }));
    store.recordConversationMessage(row('conv-b', 1, 'user', 'B 的第二轮', 'task-b-1', { tsMs: 4000 }));
    const convs = store.localConversations();
    expect(convs.map((c) => c.convId)).toEqual(['conv-b', 'conv-a']);
    expect(convs[0].turnCount).toBe(2);
    expect(convs[0].lastTaskId).toBe('task-b-1');
    expect(convs[0].createdAtMs).toBe(3000);
    expect(convs[0].updatedAtMs).toBe(4000);
    // 标题取首轮提问而不是最新一轮
    expect(convs[1].firstGoal).toBe('A 的首轮提问');
  });

  it('长提问截到 60 字，limit 生效', () => {
    store.recordConversationMessage(
      row('conv-a', 0, 'user', '通'.repeat(80), 'task-a', { tsMs: 1000 })
    );
    store.recordConversationMessage(row('conv-b', 0, 'user', 'B', 'task-b', { tsMs: 2000 }));
    const convs = store.localConversations(1);
    expect(convs).toHaveLength(1);
    expect(convs[0].convId).toBe('conv-b');
    store.recordConversationMessage(row('conv-c', 0, 'user', '通'.repeat(80), 'task-c', { tsMs: 500 }));
    const all = store.localConversations(50);
    expect(all.find((c) => c.convId === 'conv-c')?.firstGoal).toBe('通'.repeat(60));
  });

  it('空正文与空会话键不落库（不写占位行）', () => {
    store.recordConversationMessage(row('conv-1', 0, 'user', '   '));
    store.recordConversationMessage(row('', 0, 'user', '有内容但没会话键'));
    expect(store.localConversations()).toHaveLength(0);
  });

  it('会话之间互不串（按 convId 隔离读取）', () => {
    store.recordConversationMessage(row('conv-a', 0, 'user', 'A 的提问'));
    store.recordConversationMessage(row('conv-b', 0, 'user', 'B 的提问'));
    expect(store.localMessages('conv-a')).toHaveLength(1);
    expect(store.localMessages('conv-missing')).toHaveLength(0);
  });

  it('刷新后恢复：写入本地存储的台账在模块重新加载后仍在', async () => {
    store.recordConversationMessage(row('conv-1', 0, 'user', '刷新前的提问', 'task-1', { tsMs: 1000 }));
    store.recordConversationMessage(row('conv-1', 0, 'assistant', '刷新前的回复', 'task-1', { tsMs: 2000 }));
    vi.resetModules();
    const reloaded = await import('../../../src/renderer/lib/conversation-store');
    const convs = reloaded.localConversations();
    expect(convs).toHaveLength(1);
    expect(convs[0].firstGoal).toBe('刷新前的提问');
    expect(reloaded.localMessages('conv-1').map((r) => r.content)).toEqual([
      '刷新前的提问',
      '刷新前的回复',
    ]);
  });

  it('桌面运行时不双写：SQLite 是正文权威，渲染层跳过本地台账', () => {
    (globalThis as Record<string, unknown>).window = { __TAURI_INTERNALS__: {} };
    store.recordConversationMessage(row('conv-1', 0, 'user', '桌面端提问'));
    expect(store.localConversations()).toHaveLength(0);
  });

  it('resetConversationLedger 只清本地台账', () => {
    store.recordConversationMessage(row('conv-1', 0, 'user', '提问'));
    store.resetConversationLedger();
    expect(store.localConversations()).toHaveLength(0);
  });
});

// ── 恢复组装（纯函数） ──────────────────────────────────────

describe('conversation-store：恢复组装', () => {
  it('groupTurns 按轮次合并，缺失一侧留空且不丢可见内容', () => {
    const turns = store.groupTurns([
      row('c', 1, 'user', '第二轮提问', 'task-2', { tsMs: 3000 }),
      row('c', 0, 'user', '首轮提问', 'task-1', { tsMs: 1000 }),
      row('c', 0, 'assistant', '首轮回复', 'task-1', { artifacts: ['周报.md'], tsMs: 2000 }),
    ]);
    expect(turns.map((t) => t.turnIndex)).toEqual([0, 1]);
    expect(turns[0]).toMatchObject({
      taskId: 'task-1',
      userContent: '首轮提问',
      assistantContent: '首轮回复',
      artifacts: ['周报.md'],
      tsMs: 2000,
    });
    // 只有提问的一轮照样保留（回复尚未产出/未落库）
    expect(turns[1].assistantContent).toBe('');
    expect(turns[1].artifacts).toEqual([]);
  });

  it('titleOf 截 20 字，空标题回落「新任务」', () => {
    expect(store.titleOf('帮我整理本周的品牌舆情并生成周报，包含同比数据')).toBe('帮我整理本周的品牌舆情并生成周报，包含同');
    expect(store.titleOf('   ')).toBe('新任务');
  });

  it('restoredUserMessages：一轮一条用户气泡（ChatStream 按用户消息切轮次）', () => {
    const turns = store.groupTurns([
      row('c', 0, 'user', '首轮提问', 'task-1', { tsMs: 1000 }),
      row('c', 0, 'assistant', '首轮回复', 'task-1', { tsMs: 2000 }),
      row('c', 2, 'assistant', '只有回复的轮次', 'task-9', { tsMs: 9000 }),
    ]);
    const msgs = store.restoredUserMessages(turns);
    expect(msgs).toHaveLength(1);
    expect(msgs[0]).toMatchObject({ role: 'user', content: '首轮提问', taskId: 'task-1' });
  });

  it('restoredTurnEvents：正文合成 deliver 事件，过程明细按任务挂到本轮', () => {
    const turns = store.groupTurns([
      row('c', 0, 'user', '首轮提问', 'task-1', { tsMs: 1000 }),
      row('c', 0, 'assistant', '首轮回复', 'task-1', { artifacts: ['报告.md'], tsMs: 2000 }),
      row('c', 1, 'user', '第二轮提问', 'task-2', { tsMs: 3000 }),
    ]);
    const groups = store.restoredTurnEvents(turns, {
      'task-1': [{ type: 'step_done', stepId: 's1', output: 'ok' } as never],
    });
    expect(groups).toHaveLength(2);
    expect(groups[0].map((e) => e.type)).toEqual(['step_done', 'deliver']);
    const deliver = groups[0][1] as { type: string; final_text: string; artifacts: string[]; taskId: string };
    expect(deliver).toMatchObject({
      type: 'deliver',
      taskId: 'task-1',
      final_text: '首轮回复',
      artifacts: ['报告.md'],
    });
    // 没有回复正文也没有产物的轮次不合成假 deliver
    expect(groups[1]).toEqual([]);
  });

  it('taskTurnMapOf 给出 taskId → 轮次（重新生成/事件路由定位用）', () => {
    const turns = store.groupTurns([
      row('c', 0, 'user', '首轮', 'task-1'),
      row('c', 1, 'user', '第二轮', 'task-2'),
    ]);
    expect(store.taskTurnMapOf(turns)).toEqual({ 'task-1': 0, 'task-2': 1 });
  });

  it('replayProcessEvents 只取步骤类，plan/status_change 不重放', () => {
    const events = store.replayProcessEvents({
      events: [
        { seq: 1, kind: 'status_change', data: { status: 'executing' } },
        { seq: 2, kind: 'plan', data: { plan_id: 'p-1', steps: [] } },
        { seq: 3, kind: 'step_start', data: { step_id: 's1', description: '解析目标' } },
        { seq: 4, kind: 'step_progress', data: { stepId: 's1', message: '过半' } },
        { seq: 5, kind: 'step_done', data: { step_id: 's1', output: '完成' } },
        { seq: 6, kind: 'step_failed', data: { step_id: 's2', error: ' boom' } },
        { seq: 7, kind: 'deliver', data: { final_text: '正文走库里取' } },
      ],
    });
    expect(events.map((e) => e.type)).toEqual([
      'step_start',
      'step_progress',
      'step_done',
      'step_failed',
    ]);
    expect(events[0]).toMatchObject({ stepId: 's1', description: '解析目标', seq: 3 });
    expect(events[2]).toMatchObject({ stepId: 's1', output: '完成' });
    // 脏返回体（非事件流形状）返回空，不抛错打断启动
    expect(store.replayProcessEvents(undefined)).toEqual([]);
    expect(store.replayProcessEvents({ events: 'oops' })).toEqual([]);
  });

  it('兜底解析：字段缺失/类型漂移的行丢弃，不组装半条假消息', () => {
    const rows = store.toMessageRows([
      { convId: 'c', turnIndex: 0, role: 'user', content: 'ok', taskId: 't', tsMs: 1 },
      { convId: '', turnIndex: 0, role: 'user', content: '没会话键' },
      { convId: 'c', turnIndex: 1, role: 'system', content: '未知角色' },
      { convId: 'c', turnIndex: 2, role: 'assistant', content: 42 },
      null,
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0].content).toBe('ok');
    expect(store.toMessageRows({ not: 'array' })).toEqual([]);
    expect(store.toConversationRows([{ convId: 'c', firstGoal: '提问' }])).toEqual([
      { convId: 'c', firstGoal: '提问', turnCount: 0, createdAtMs: 0, updatedAtMs: 0, lastTaskId: '' },
    ]);
  });
});

// ── 通道分发（Mock / 浏览器直连引擎共用） ────────────────────

describe('conversation-local：非 Rust 通道分发', () => {
  it('chat:conversations 与 chat:messages 命中本地台账', () => {
    store.recordConversationMessage(row('conv-1', 0, 'user', '首轮提问', 'task-1', { tsMs: 1000 }));
    store.recordConversationMessage(row('conv-1', 0, 'assistant', '首轮回复', 'task-1', { tsMs: 2000 }));
    const list = localChannel.handleLocalConversationChannel('chat:conversations', [50]);
    expect(list.handled).toBe(true);
    expect(list.value).toHaveLength(1);
    const msgs = localChannel.handleLocalConversationChannel('chat:messages', ['conv-1']);
    expect(msgs.handled).toBe(true);
    expect((msgs.value as { content: string }[]).map((r) => r.content)).toEqual([
      '首轮提问',
      '首轮回复',
    ]);
  });

  it('会话标识为空 → CHAT_INVALID_CONVERSATION（与 Rust 同错误码）', () => {
    expect(() => localChannel.handleLocalConversationChannel('chat:messages', [''])).toThrow(
      /CHAT_INVALID_CONVERSATION/
    );
  });

  it('未知通道不接管（交由其他本地实现或兜底）', () => {
    expect(localChannel.handleLocalConversationChannel('chat:history', []).handled).toBe(false);
  });
});

// ── 回复评价（点赞/点踩） ──────────────────────────────────────

describe('conversation-store：回复评价按 (会话, 轮次) 持久化', () => {
  const seed = (convId = 'conv-1', turn = 0) => {
    store.recordConversationMessage(row(convId, turn, 'user', `${convId} 第 ${turn} 轮提问`, `task-${convId}-${turn}`, { tsMs: turn * 1000 + 1 }));
    store.recordConversationMessage(row(convId, turn, 'assistant', `${convId} 第 ${turn} 轮回复`, `task-${convId}-${turn}`, { tsMs: turn * 1000 + 2 }));
  };

  it('写入评价落在本轮 assistant 行，改评覆盖不累积', () => {
    seed();
    store.setLocalFeedback('conv-1', 0, 'up');
    let rows = store.localMessages('conv-1');
    expect(rows.find((r) => r.role === 'user')?.feedback).toBeUndefined();
    expect(rows.find((r) => r.role === 'assistant')?.feedback).toBe('up');
    store.setLocalFeedback('conv-1', 0, 'down');
    rows = store.localMessages('conv-1');
    expect(rows).toHaveLength(2);
    expect(rows.find((r) => r.role === 'assistant')?.feedback).toBe('down');
  });

  it('取消评价只清评价，正文行保留（与 Rust 删 chat_feedback 不动 chat_messages 一致）', () => {
    seed();
    store.setLocalFeedback('conv-1', 0, 'up');
    store.setLocalFeedback('conv-1', 0, null);
    const rows = store.localMessages('conv-1');
    expect(rows).toHaveLength(2);
    expect(rows.find((r) => r.role === 'assistant')?.feedback).toBeUndefined();
    expect(rows.find((r) => r.role === 'assistant')?.content).toBe('conv-1 第 0 轮回复');
  });

  it('没有回复正文的轮次不可评价（CHAT_FEEDBACK_TURN_MISSING，与 Rust 同错误码）', () => {
    seed();
    expect(() => store.setLocalFeedback('conv-1', 7, 'up')).toThrow(/CHAT_FEEDBACK_TURN_MISSING/);
    expect(() => store.setLocalFeedback('conv-missing', 0, 'up')).toThrow(/CHAT_FEEDBACK_TURN_MISSING/);
    // 只有提问、尚无回复的轮次同样不可评价
    store.recordConversationMessage(row('conv-2', 0, 'user', '还没回复', 'task-conv-2-0'));
    expect(() => store.setLocalFeedback('conv-2', 0, 'up')).toThrow(/CHAT_FEEDBACK_TURN_MISSING/);
  });

  it('重新生成覆盖本轮正文时评价随轮次保留（与 Rust 主键语义一致）', () => {
    seed();
    store.setLocalFeedback('conv-1', 0, 'up');
    store.recordConversationMessage(row('conv-1', 0, 'assistant', '第二版回复', 'task-new', { tsMs: 9000 }));
    const assistant = store.localMessages('conv-1').find((r) => r.role === 'assistant');
    expect(assistant?.content).toBe('第二版回复');
    expect(assistant?.feedback).toBe('up');
  });

  it('会话之间互不串', () => {
    seed('conv-1', 0);
    seed('conv-2', 0);
    store.setLocalFeedback('conv-2', 0, 'down');
    expect(store.localMessages('conv-1').find((r) => r.role === 'assistant')?.feedback).toBeUndefined();
    expect(store.localMessages('conv-2').find((r) => r.role === 'assistant')?.feedback).toBe('down');
  });

  it('刷新后评价仍在：写入 localStorage 并在模块重载后读回', async () => {
    const ledger = installFakeStorage();
    seed();
    store.setLocalFeedback('conv-1', 0, 'up');
    expect(ledger.get('tt:chatMessages')).toContain('"feedback":"up"');
    vi.resetModules();
    const reloaded: StoreMod = await import('../../../src/renderer/lib/conversation-store');
    expect(reloaded.localMessages('conv-1').find((r) => r.role === 'assistant')?.feedback).toBe('up');
  });

  it('saveFeedback：非桌面通道写本地台账，不碰 Rust', async () => {
    seed();
    const invoke = vi.fn(async () => ({ ok: true }));
    (globalThis as Record<string, unknown>).window = { tongtianAPI: { invoke } };
    await store.saveFeedback('conv-1', 0, 'up');
    expect(invoke).not.toHaveBeenCalled();
    expect(store.localMessages('conv-1').find((r) => r.role === 'assistant')?.feedback).toBe('up');
  });

  it('saveFeedback：桌面通道以 Rust 为权威，参数为会话键 + 轮次 + 评价', async () => {
    seed();
    const invoke = vi.fn(async () => ({ ok: true }));
    (globalThis as Record<string, unknown>).window = {
      __TAURI_INTERNALS__: {},
      tongtianAPI: { invoke },
    };
    await store.saveFeedback('conv-1', 0, 'down');
    expect(invoke).toHaveBeenCalledWith('chat:feedback', 'conv-1', 0, 'down');
    // 桌面端渲染层不另存影子台账
    expect(store.localMessages('conv-1').find((r) => r.role === 'assistant')?.feedback).toBeUndefined();
    // 取消评价传 null（Rust 侧删除该行评价）
    await store.saveFeedback('conv-1', 0, null);
    expect(invoke).toHaveBeenLastCalledWith('chat:feedback', 'conv-1', 0, null);
  });

  it('saveFeedback：桌面落库失败如实抛出，供界面回滚', async () => {
    seed();
    (globalThis as Record<string, unknown>).window = {
      __TAURI_INTERNALS__: {},
      tongtianAPI: { invoke: vi.fn(async () => { throw new Error('CHAT_FEEDBACK_SAVE_FAILED: disk'); }) },
    };
    await expect(store.saveFeedback('conv-1', 0, 'up')).rejects.toThrow(/CHAT_FEEDBACK_SAVE_FAILED/);
  });

  it('saveFeedback：会话键为空直接拒绝', async () => {
    await expect(store.saveFeedback('', 0, 'up')).rejects.toThrow(/CHAT_INVALID_CONVERSATION/);
  });

  it('通道分发：chat:feedback 命中本地台账，非法值与坏轮次被拒', () => {
    seed();
    const res = localChannel.handleLocalConversationChannel('chat:feedback', ['conv-1', 0, 'up']);
    expect(res.handled).toBe(true);
    expect(res.value).toEqual({ ok: true });
    expect(store.localMessages('conv-1').find((r) => r.role === 'assistant')?.feedback).toBe('up');
    expect(() => localChannel.handleLocalConversationChannel('chat:feedback', ['conv-1', 0, 'meh'])).toThrow(
      /CHAT_FEEDBACK_INVALID_VALUE/
    );
    expect(() => localChannel.handleLocalConversationChannel('chat:feedback', ['conv-1', -1, 'up'])).toThrow(
      /CHAT_FEEDBACK_INVALID_VALUE/
    );
    expect(() => localChannel.handleLocalConversationChannel('chat:feedback', ['', 0, 'up'])).toThrow(
      /CHAT_INVALID_CONVERSATION/
    );
    // 空值即取消评价，与 Rust 的 Option<String>=None 同语义
    expect(localChannel.handleLocalConversationChannel('chat:feedback', ['conv-1', 0, null]).handled).toBe(true);
    expect(store.localMessages('conv-1').find((r) => r.role === 'assistant')?.feedback).toBeUndefined();
  });
});

describe('恢复组装：评价随正文一起恢复', () => {
  it('groupTurns 带出本轮评价，feedbackOfTurns 汇总成界面用的表', () => {
    const rows = store.toMessageRows([
      { convId: 'c', turnIndex: 0, role: 'user', content: '提问 0', taskId: 't0', tsMs: 1 },
      { convId: 'c', turnIndex: 0, role: 'assistant', content: '回复 0', taskId: 't0', artifacts: [], tsMs: 2, feedback: 'up' },
      { convId: 'c', turnIndex: 1, role: 'assistant', content: '回复 1', taskId: 't1', artifacts: [], tsMs: 3, feedback: '' },
    ]);
    const turns = store.groupTurns(rows);
    expect(turns.map((t) => t.feedback)).toEqual(['up', null]);
    expect(store.feedbackOfTurns(turns)).toEqual({ 0: 'up' });
  });

  it('评价字段类型漂移按未评价处理，不丢整行', () => {
    const rows = store.toMessageRows([
      { convId: 'c', turnIndex: 0, role: 'assistant', content: '回复', taskId: 't', artifacts: [], tsMs: 1, feedback: 'awesome' },
      { convId: 'c', turnIndex: 1, role: 'assistant', content: '回复', taskId: 't', artifacts: [], tsMs: 2, feedback: 42 },
    ]);
    expect(rows).toHaveLength(2);
    expect(rows.every((r) => r.feedback === undefined)).toBe(true);
  });
});

// ── 三通道接线（源码契约断言） ───────────────────────────────

describe('会话恢复接线：三条通道 + 提交/落库参数', () => {
  it('桌面通道映射到 Rust command，并转发会话键与轮次', () => {
    const src = read('renderer/lib/tauri-api.ts');
    expect(src).toContain("if (channel === 'chat:conversations') {");
    expect(src).toContain("return invoke('chat_conversations', { limit: args[0] as number | undefined });");
    expect(src).toContain("if (channel === 'chat:messages') {");
    expect(src).toContain("return invoke('chat_messages', {");
    expect(src).toContain('conversationId: args[0] as string,');
    expect(src).toContain('conversationId: req.conversationId');
    expect(src).toContain('turnIndex: req.turnIndex');
  });

  it('Mock 与引擎直连通道都接管会话正文通道', () => {
    expect(read('renderer/lib/mock-api.ts')).toContain('handleLocalConversationChannel(channel, args)');
    expect(read('renderer/lib/engine-api.ts')).toContain('handleLocalConversationChannel(channel, args)');
  });

  it('Mock 交付事件带回复正文（与引擎/Rust deliver 同字段）', () => {
    expect(read('renderer/lib/mock-api.ts')).toContain('final_text: `已按目标');
  });

  it('store：提交与重新生成都携带会话键/轮次，正文两侧都落库', () => {
    const src = read('renderer/store/workbenchStore.ts');
    expect(src).toContain('conversationId: convId,');
    expect(src).toContain('turnIndex,');
    expect(src).toContain('conversationId: conv.id, turnIndex: turnIdx');
    expect(src).toContain("role: 'user',");
    expect(src).toContain("role: 'assistant',");
    // 启动重建会话：会话列表 + 正文 + 归档过滤
    expect(src).toContain('await fetchConversations()');
    expect(src).toContain('await fetchConversationMessages(r.convId, 200)');
    expect(src).toContain('const archived = await listArchived().catch(() => []);');
    expect(src).toContain('pendingProcess: true');
    // 过程明细只在首次切到该会话时回放（事件流 24h 窗口）
    expect(src).toContain('if (conv.pendingProcess) void hydrateProcess(id);');
    expect(src).toContain('.eventReplay(t.taskId)');
  });

  it('契约：IPC 通道常量与行类型就绪', () => {
    const src = read('shared/contracts.ts');
    expect(src).toContain("CHAT_CONVERSATIONS: 'chat:conversations'");
    expect(src).toContain("CHAT_MESSAGES: 'chat:messages'");
    expect(src).toContain('export interface ChatMessageRow');
    expect(src).toContain('export interface ConversationRow');
    // 评价通道与行上的评价字段（界面恢复用）
    expect(src).toContain("CHAT_FEEDBACK: 'chat:feedback'");
    expect(src).toContain("feedback?: '' | 'up' | 'down';");
  });

  it('评价接线：桌面通道映射 Rust command，store 收归会话级状态，界面不再本地记账', () => {
    const tauri = read('renderer/lib/tauri-api.ts');
    expect(tauri).toContain("if (channel === 'chat:feedback') {");
    expect(tauri).toContain("return invoke('chat_feedback', {");
    expect(tauri).toContain('turnIndex: args[1] as number,');

    // Rust 侧 command 存在且注册进 invoke_handler
    const rs = readFileSync(resolve(__dirname, '../../../src-tauri/src/lib.rs'), 'utf8').replace(/\r\n/g, '\n');
    expect(rs).toContain('async fn chat_feedback(');
    expect(rs).toContain('store::messages::set_feedback(');
    expect(rs).toMatch(/\n\s+chat_messages,\n\s+chat_feedback,\n/);

    const wb = read('renderer/store/workbenchStore.ts');
    // 评价归属会话（切会话不串轮），并从正文库恢复
    expect(wb).toContain('feedbackByTurn: Record<number, \'up\' | \'down\'>;');
    expect(wb).toContain('feedbackByTurn: feedbackOfTurns(turns)');
    expect(wb).toContain('feedbackByTurn: conv.feedbackByTurn,');
    expect(wb).toContain('await saveFeedback(convId, turnIdx, kind);');
    // 落库失败回滚，界面不留一个没存住的选中态
    expect(wb).toContain('const before = conv.feedbackByTurn[turnIdx] ?? null;');

    const stream = read('renderer/components/ChatStream/index.tsx');
    // 组件不再自己 useState 记账（否则切会话会串轮、刷新即丢）
    expect(stream).not.toContain("useState<Record<number, 'up' | 'down' | null>>");
    expect(stream).toContain('onFeedback?: (turnIdx: number, kind: \'up\' | \'down\' | null) => Promise<void>;');
    expect(stream).toContain('await onFeedback(turnIdx, feedback[turnIdx] === kind ? null : kind);');

    const page = read('renderer/pages/Workbench/index.tsx');
    expect(page).toContain('feedback={feedbackByTurn}');
    expect(page).toContain('await setTurnFeedback(activeId, turnIdx, kind);');
  });
});
