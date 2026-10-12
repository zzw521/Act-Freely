/**
 * 任务搜索 / 会话内查找纯逻辑契约测试（Ctrl+G / Ctrl+F 的真实后端）
 *
 * 覆盖 turnTexts 的轮次口径（与 ChatStream 的 0 基索引、data-turn 锚点一致）、
 * 各类 ChatEvent 的文本提取、大小写与空白不敏感的匹配，以及跨会话搜索的排序、
 * 片段截取与「无命中如实返回空」——不摆示例数据。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { Role, type ChatEvent, type Message } from '../../../src/shared/types';
import {
  findMatchingTurns,
  searchConversations,
  turnTexts,
  type SearchableConversation,
  type SearchableTurn,
} from '../../../src/renderer/lib/task-search';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const user = (content: string, id = 'u1', ts = new Date('2026-10-01T09:00:00Z')): Message => ({
  id,
  taskId: 't1',
  role: Role.USER,
  content,
  ts,
});

const ai = (content: string, id = 'a1', ts = new Date('2026-10-01T09:01:00Z')): Message => ({
  id,
  taskId: 't1',
  role: Role.ASSISTANT,
  content,
  ts,
});

const ev = (o: Record<string, unknown>) => o as unknown as ChatEvent;

function conv(over: Partial<SearchableConversation> & { id: string }): SearchableConversation {
  return {
    title: '新任务',
    createdAt: new Date('2026-10-01T08:00:00Z').getTime(),
    isLoading: false,
    messages: [],
    turnEvents: [],
    ...over,
  };
}

// ── 轮次文本抽取 ──────────────────────────────────────────────

describe('turnTexts', () => {
  it('轮次下标从 0 开始，与 ChatStream 的 turnIdx 同口径', () => {
    const turns = turnTexts(
      [user('第一轮提问', 'u1'), ai('答复', 'a1'), user('第二轮提问', 'u2')],
      [[], []],
    );
    expect(turns.map((t) => t.turn)).toEqual([0, 1]);
    expect(turns[0].text).toContain('第一轮提问');
    expect(turns[1].text).toContain('第二轮提问');
  });

  it('同一轮的提问与助手产出拼在一起', () => {
    const turns = turnTexts([user('写一份周报')], [[ev({ type: 'deliver', final_text: '已生成', summary: '周报摘要' })]]);
    expect(turns).toHaveLength(1);
    expect(turns[0].text).toBe('写一份周报 已生成 周报摘要');
  });

  it('各类事件都能读出文本：计划、步骤起止、进度、失败', () => {
    const turns = turnTexts(
      [user('跑一遍'), ai('占位', 'a1')],
      [
        [
          ev({ type: 'plan', steps: [{ description: '抓取数据' }, { description: '汇总成表' }] }),
          ev({ type: 'step_start', description: '开始抓取' }),
          ev({ type: 'step_progress', message: '已处理 3 条' }),
          ev({ type: 'step_done', output: '产出 rows.xlsx' }),
          ev({ type: 'step_failed', error: '超时失败' }),
        ],
      ],
    );
    expect(turns[0].text).toContain('抓取数据 汇总成表');
    expect(turns[0].text).toContain('开始抓取');
    expect(turns[0].text).toContain('已处理 3 条');
    expect(turns[0].text).toContain('产出 rows.xlsx');
    expect(turns[0].text).toContain('超时失败');
  });

  it('交付物文件名在界面上可见，因此同样参与搜索', () => {
    const turns = turnTexts([user('出个报告')], [[ev({ type: 'deliver', artifacts: ['成果报告.md', '数据摘要.xlsx'] })]]);
    expect(turns[0].text).toContain('成果报告.md');
    expect(findMatchingTurns(turns, '数据摘要')).toEqual([0]);
  });

  it('不贡献文本的事件不会凭空造出轮次，空白轮次如实跳过', () => {
    const turns = turnTexts([user('问题'), user('   ')], [[ev({ type: 'status_change', status: 'running' })], []]);
    expect(turns.map((t) => t.turn)).toEqual([0]);
    expect(turns[0].text).toBe('问题');
  });

  it('事件组比用户消息多时按较长的一侧计数（重新生成会留下空轮次）', () => {
    const turns = turnTexts([user('唯一提问')], [[], [ev({ type: 'step_done', output: '重跑结果' })]]);
    expect(turns.map((t) => t.turn)).toEqual([0, 1]);
    expect(turns[0].text).toBe('唯一提问');
    expect(turns[1].text).toBe('重跑结果');
  });

  it('没有 turnEvents 时只按用户消息出题（避免 undefined 崩）', () => {
    const turns = turnTexts([user('甲'), ai('乙', 'a1'), user('丙')]);
    expect(turns.map((t) => t.text)).toEqual(['甲', '丙']);
  });

  it('压缩正文里的换行与连续空白，便于跨行匹配', () => {
    const turns = turnTexts([user('请\n  分析\n\n销售数据')], []);
    expect(turns[0].text).toBe('请 分析 销售数据');
  });
});

// ── 会话内查找 ────────────────────────────────────────────────

describe('findMatchingTurns', () => {
  const turns: SearchableTurn[] = [
    { turn: 0, text: '分析 Q3 销售数据' },
    { turn: 1, text: '导出 销售 报表' },
    { turn: 2, text: '与搜索无关' },
  ];

  it('大小写不敏感，返回命中的轮次下标', () => {
    expect(findMatchingTurns(turns, 'q3')).toEqual([0]);
    expect(findMatchingTurns(turns, '销售')).toEqual([0, 1]);
  });

  it('查询与正文的多余空白都参与归一（跨行也能命中）', () => {
    expect(findMatchingTurns(turns, '  导出   销售 ')).toEqual([1]);
  });

  it('空查询不返回任何命中，不做「空串匹配一切」的假动作', () => {
    expect(findMatchingTurns(turns, '')).toEqual([]);
    expect(findMatchingTurns(turns, '    ')).toEqual([]);
  });

  it('无命中返回空数组', () => {
    expect(findMatchingTurns(turns, '不存在')).toEqual([]);
  });

  it('空轮次集合不报错', () => {
    expect(findMatchingTurns([], '任意')).toEqual([]);
  });
});

// ── 跨会话搜索 ────────────────────────────────────────────────

describe('searchConversations', () => {
  const older = conv({
    id: 'c-old',
    title: '整理会议纪要',
    createdAt: new Date('2026-09-01T00:00:00Z').getTime(),
    messages: [user('整理会议', 'u', new Date('2026-09-01T00:00:00Z')), ai('完成', 'a', new Date('2026-09-01T00:05:00Z'))],
    turnEvents: [[ev({ type: 'deliver', summary: '纪要已归档' })]],
  });
  const newer = conv({
    id: 'c-new',
    title: '销售数据分析',
    createdAt: new Date('2026-10-01T00:00:00Z').getTime(),
    isLoading: true,
    messages: [user('分析销售数据', 'u2', new Date('2026-10-01T00:00:00Z'))],
    turnEvents: [[ev({ type: 'step_done', output: 'Q3 销售数据 共 120 行，另有 销售 报表' })]],
  });
  const all = [older, newer];

  it('标题命中优先于正文命中，命中项都带上下文片段', () => {
    const hits = searchConversations(all, '销售');
    expect(hits.map((h) => h.convId)).toEqual(['c-new']);
    expect(hits[0].matchedIn).toBe('title');
    expect(hits[0].turns).toEqual([0]);
    expect(hits[0].running).toBe(true);
  });

  it('仅正文命中时标记 content，并给出以命中处为中心的片段', () => {
    const hits = searchConversations(all, 'Q3');
    expect(hits.map((h) => h.convId)).toEqual(['c-new']);
    expect(hits[0].matchedIn).toBe('content');
    expect(hits[0].preview).toContain('Q3');
  });

  it('标题与正文都命中时算标题命中，不算两处', () => {
    const hits = searchConversations([newer], '销售');
    expect(hits[0].matchedIn).toBe('title');
    expect(hits[0].turns).toEqual([0]);
  });

  it('多个正文命中按轮次累积，供查找条显示「X / Y 处」', () => {
    const multi = conv({
      id: 'c-multi',
      title: '空标题无关',
      messages: [user('甲', 'u1'), user('乙', 'u2')],
      turnEvents: [[ev({ type: 'step_done', output: '关键词 出现' })], [ev({ type: 'step_done', output: '又一次 关键词' })]],
    });
    const hits = searchConversations([multi], '关键词');
    expect(hits[0].matchedIn).toBe('content');
    expect(hits[0].turns).toEqual([0, 1]);
  });

  it('空查询返回最近会话视图而不是空列表，按最近活动倒序', () => {
    const hits = searchConversations(all, '');
    expect(hits.map((h) => h.convId)).toEqual(['c-new', 'c-old']);
    expect(hits.every((h) => h.matchedIn === 'title')).toBe(true);
  });

  it('最近更新取最后一条消息的时间，没有消息时退回创建时间', () => {
    const empty = conv({ id: 'c-empty', title: '刚建的任务', createdAt: new Date('2026-08-01T00:00:00Z').getTime() });
    const [hit] = searchConversations([empty], '');
    expect(hit.updatedAt).toBe(empty.createdAt);
    expect(hit.preview).toBe('（尚无回复内容）');
  });

  it('没有匹配就如实返回空数组，不摆示例数据', () => {
    expect(searchConversations(all, '绝对不存在的内容')).toEqual([]);
  });

  it('limit 生效，超出部分截断', () => {
    const many = Array.from({ length: 5 }, (_, i) => conv({ id: `c${i}`, title: `任务${i}` }));
    expect(searchConversations(many, '', 2)).toHaveLength(2);
  });

  it('标题缺省时回落到「新任务」', () => {
    const hit = searchConversations([conv({ id: 'c-x', title: '' })], '')[0];
    expect(hit.title).toBe('新任务');
  });

  it('大小写与空白不敏感地命中英文标题', () => {
    const en = conv({ id: 'c-en', title: 'Deploy Report' });
    expect(searchConversations([en], 'deploy   report').map((h) => h.convId)).toEqual(['c-en']);
  });
});

// ── 源码接线：搜索链路的唯一权威 ──────────────────────────────

describe('搜索链路接线', () => {
  it('全局面板与查找条共用同一份 task-search 逻辑，不各写一套匹配', () => {
    const dialog = read('../../../src/renderer/components/TaskSearchDialog/index.tsx');
    const findBar = read('../../../src/renderer/components/TaskFindBar/index.tsx');
    expect(dialog).toContain("from '../../lib/task-search'");
    expect(findBar).toContain("from '../../lib/task-search'");
    expect(dialog).toContain('searchConversations(');
    expect(findBar).toContain('findMatchingTurns(');
    expect(findBar).toContain('turnTexts(');
    expect(dialog).not.toContain('toLowerCase().includes('); // 不在组件里另起匹配
    expect(findBar).not.toContain('toLowerCase().includes(');
  });

  it('面板搜的是 workbenchStore 里真实存在的会话，打开即切到命中会话', () => {
    const dialog = read('../../../src/renderer/components/TaskSearchDialog/index.tsx');
    expect(dialog).toContain('s.convs');
    expect(dialog).toContain('s.convOrder');
    expect(dialog).toContain('switchConversation(hit.convId)');
    expect(dialog).toContain("navigate('/')");
    expect(dialog).toContain('没有匹配');
    expect(dialog).toContain('还没有任务');
  });

  it('查找条用轮次锚点定位，Enter 向下、Shift+Enter 向上、Esc 关闭', () => {
    const findBar = read('../../../src/renderer/components/TaskFindBar/index.tsx');
    expect(findBar).toContain('scrollIntoView({ behavior: \'smooth\', block: \'center\' })');
    expect(findBar).toContain('step(e.shiftKey ? -1 : 1)');
    expect(findBar).toContain('e.key === \'Escape\'');
    expect(findBar).toContain('0 处');
    expect(findBar).toContain('disabled={!matches.length}');
  });

  it('轮次口径只有一处定义：turn 字段即 data-turn 的值', () => {
    const lib = read('../../../src/renderer/lib/task-search.ts');
    expect(lib).toContain('{ turn: i, text }');
    const stream = read('../../../src/renderer/components/ChatStream/index.tsx');
    expect(stream).toContain('data-turn={currentTurn}');
  });
});
