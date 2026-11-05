/**
 * 文件访问档位的落点：会话台账 与 新任务页草稿
 *
 * 实机验证发现的洞：新任务页此时还没有会话，`setFilePermission` 因为取不到 activeId 直接 return。
 * 于是用户在输入框里读完红色警告、点了「我已了解，启用完全访问」，界面一档不动、台账一个键不写，
 * 第一条消息又回落到全局默认——确认了个寂寞。这里把两条路径钉成行为断言：
 * 有会话写台账，没会话进草稿，草稿在第一条消息建会话时落台账；
 * 没带确认时刻的「完全访问」任何一条路径都得降级，界面不能显示假的放开状态。
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { useWorkbenchStore, type ConversationState } from '../../../src/renderer/store/workbenchStore';

const LEDGER_KEY = 'tt:conversationFilePermission';

/** Map 版 localStorage 垫片（vitest 环境为 node，没有原生实现） */
function installStorage(): Map<string, string> {
  const data = new Map<string, string>();
  (globalThis as Record<string, unknown>).localStorage = {
    getItem: (k: string) => (data.has(k) ? data.get(k)! : null),
    setItem: (k: string, v: string) => void data.set(k, String(v)),
    removeItem: (k: string) => void data.delete(k),
    clear: () => data.clear(),
    key: (i: number) => Array.from(data.keys())[i] ?? null,
    get length() {
      return data.size;
    },
  };
  return data;
}

function ledgerOf(data: Map<string, string>): Record<string, { mode: string; confirmedAtMs?: number }> {
  const raw = data.get(LEDGER_KEY);
  return raw ? JSON.parse(raw) : {};
}

function makeConv(id: string): ConversationState {
  return {
    id,
    title: '档位用例',
    createdAt: Date.now(),
    messages: [],
    events: [],
    turnEvents: [[]],
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
    workspaceId: 'default',
    checkpoint: null,
    checkpointTurn: null,
    isLoading: false,
    error: null,
  } as unknown as ConversationState;
}

function seedConv(id: string) {
  const conv = makeConv(id);
  useWorkbenchStore.setState({
    convs: { [id]: conv },
    convOrder: [id],
    activeId: id,
    filePermission: undefined,
    draftFilePermission: null,
  });
  return conv;
}

describe('有会话：档位进该会话的台账，界面镜像跟着台账实际认可的值', () => {
  let data: Map<string, string>;
  beforeEach(() => {
    data = installStorage();
  });

  it('确认过的完全访问：落台账并带确认时刻', () => {
    const conv = seedConv('conv-full-ok');
    useWorkbenchStore.getState().setFilePermission('full_access', 1_700_000_000_000);
    expect(ledgerOf(data)[conv.id]).toEqual({ mode: 'full_access', confirmedAtMs: 1_700_000_000_000 });
    expect(useWorkbenchStore.getState().convs[conv.id].filePermission).toBe('full_access');
  });

  it('没确认时刻的完全访问：台账与界面都回每次询问，不放开的假状态', () => {
    const conv = seedConv('conv-full-noack');
    useWorkbenchStore.getState().setFilePermission('full_access');
    expect(ledgerOf(data)[conv.id]).toEqual({ mode: 'ask_every_time' });
    expect(useWorkbenchStore.getState().convs[conv.id].filePermission).toBe('ask_every_time');
  });

  it('自动审批不需要确认时刻，直接生效', () => {
    const conv = seedConv('conv-auto');
    useWorkbenchStore.getState().setFilePermission('auto_approve');
    expect(ledgerOf(data)[conv.id].mode).toBe('auto_approve');
    expect(useWorkbenchStore.getState().convs[conv.id].filePermission).toBe('auto_approve');
  });

  it('非法值不放大权限', () => {
    const conv = seedConv('conv-garbage');
    useWorkbenchStore.getState().setFilePermission('everything' as never, 1);
    expect(ledgerOf(data)[conv.id].mode).toBe('ask_every_time');
  });
});

describe('新任务页（还没有会话）：选择先进草稿，不静默丢弃', () => {
  let data: Map<string, string>;
  beforeEach(() => {
    data = installStorage();
    useWorkbenchStore.setState({
      convs: {},
      convOrder: [],
      activeId: null,
      filePermission: undefined,
      draftFilePermission: null,
    });
  });

  it('确认过的完全访问：草稿与输入框镜像同时放开', () => {
    useWorkbenchStore.getState().setFilePermission('full_access', 1_700_000_000_001);
    const s = useWorkbenchStore.getState();
    expect(s.draftFilePermission).toEqual({ mode: 'full_access', confirmedAtMs: 1_700_000_000_001 });
    expect(s.filePermission).toBe('full_access');
    // 此刻还没有会话，不该凭空写出一个键
    expect(ledgerOf(data)).toEqual({});
  });

  it('没确认时刻的完全访问：草稿同样降级，界面不显示「已经放开」', () => {
    useWorkbenchStore.getState().setFilePermission('full_access');
    const s = useWorkbenchStore.getState();
    expect(s.draftFilePermission!.mode).toBe('ask_every_time');
    expect(s.filePermission).toBe('ask_every_time');
  });

  it('自动审批在欢迎页也是直接生效', () => {
    useWorkbenchStore.getState().setFilePermission('auto_approve');
    expect(useWorkbenchStore.getState().draftFilePermission!.mode).toBe('auto_approve');
    expect(useWorkbenchStore.getState().filePermission).toBe('auto_approve');
  });

  it('新建会话即作废草稿：草稿不会漂进下一条会话', () => {
    useWorkbenchStore.getState().setFilePermission('full_access', 1_700_000_000_002);
    useWorkbenchStore.getState().newConversation();
    const s = useWorkbenchStore.getState();
    expect(s.draftFilePermission).toBeNull();
    expect(s.filePermission).toBeUndefined();
    expect(ledgerOf(data)).toEqual({});
  });

  it('切进别的会话同样作废草稿，档位以那条会话自己的记录为准', () => {
    useWorkbenchStore.getState().setFilePermission('full_access', 1_700_000_000_003);
    const other = makeConv('conv-other');
    useWorkbenchStore.setState((st) => ({
      convs: { ...st.convs, [other.id]: other },
      convOrder: [other.id, ...st.convOrder],
    }));
    useWorkbenchStore.getState().switchConversation(other.id);
    const s = useWorkbenchStore.getState();
    expect(s.draftFilePermission).toBeNull();
    expect(s.filePermission).toBeUndefined();
  });
});
