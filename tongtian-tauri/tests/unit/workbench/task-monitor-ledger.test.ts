/**
 * 右侧任务栏 · 技能与连接器计数真实化（任务 #29）
 *
 * TaskMonitor 原本写死 `skillCount = 3, mcpCount = 2`，而 Workbench 从不传值，
 * 于是任务栏永远显示两个与本机无关的数字，配一行「即将上线」的装饰文案。
 * 本文件从数据与源码两侧锁定：
 * - 数据：计数来自真实台账通道（skill:list / mcp:listServers），浏览器通道给空数组
 * - 源码：默认值与假文案已删除；badge 绑真实长度；读取中/失败都不产出数字
 * - 交互：失败给可点的重试，空态给可点的跳转入口（不是装饰性 Box）
 */

import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

(globalThis as Record<string, unknown>).window =
  (globalThis as Record<string, unknown>).window ?? {};

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const monitor = () => read('../../../src/renderer/components/TaskMonitor/index.tsx');
const workbench = () => read('../../../src/renderer/pages/Workbench/index.tsx');

type SkillApi = typeof import('../../../src/renderer/lib/api');

let api: SkillApi;

beforeAll(async () => {
  await import('../../../src/renderer/lib/mock-api');
  api = await import('../../../src/renderer/lib/api');
});

// ── 数据侧：计数只来自真实台账 ─────────────────────────────────────

describe('任务栏计数的数据源', () => {
  it('浏览器通道下两个清单都如实为空（而不是 3 和 2）', async () => {
    expect(await api.skillAPI.list()).toEqual([]);
    expect(await api.mcpAPI.list()).toEqual([]);
  });

  it('两个清单都是数组，badge 可直接取 length', async () => {
    const [skills, servers] = await Promise.all([api.skillAPI.list(), api.mcpAPI.list()]);
    expect(Array.isArray(skills)).toBe(true);
    expect(Array.isArray(servers)).toBe(true);
    expect(skills.length + servers.length).toBe(0);
  });
});

// ── 源码侧：假默认值不再存在 ───────────────────────────────────────

describe('TaskMonitor 不再编造计数', () => {
  it('写死的默认计数与「即将上线」占位文案已删除', () => {
    const src = monitor();
    expect(src).not.toContain('skillCount = 3');
    expect(src).not.toContain('mcpCount = 2');
    expect(src).not.toContain('skillCount?: number');
    expect(src).not.toContain('mcpCount?: number');
    expect(src).not.toContain('技能市场即将上线，当前为内置技能');
    expect(src).not.toContain('连接器中心即将上线');
    expect(src).not.toContain('计数为占位');
  });

  it('调用方不再需要（也不可能）传入伪造计数', () => {
    expect(workbench()).not.toMatch(/<TaskMonitor[^>]*Count=/);
    expect(monitor()).toContain('const { skills, servers, failed, reload } = useCapabilityLedger();');
  });

  it('计数取真实 length，读取中（null）时不渲染任何数字', () => {
    const src = monitor();
    expect(src).toContain('badge={skills?.length}');
    expect(src).toContain('badge={servers?.length}');
    // null 而非 0：Section 只在 badge != null 时画徽章
    expect(src).toContain('setSkills(s.status === \'fulfilled\' ? s.value : null);');
    expect(src).toContain('setServers(m.status === \'fulfilled\' ? m.value : null);');
  });

  it('两个清单并发取，一个失败不拖累另一个', () => {
    expect(monitor()).toContain('Promise.allSettled([skillAPI.list(), mcpAPI.list()])');
    expect(monitor()).toContain("setFailed({ skills: s.status !== 'fulfilled', servers: m.status !== 'fulfilled' });");
  });
});

// ── 交互侧：状态可见且可操作 ───────────────────────────────────────

describe('任务栏清单区交互', () => {
  it('读取中如实提示，失败给可点重试而不是静默显示 0', () => {
    const src = monitor();
    expect(src).toContain('正在读取清单…');
    expect(src).toContain('技能清单读取失败');
    expect(src).toContain('连接器清单读取失败');
    expect(src).toContain('<LedgerFooter');
    expect(src).toContain('onClick={onRetry}');
    expect(src).toContain('onRetry={() => void reload()}');
  });

  it('空态给真实跳转入口，路由参数与扩展页 tab 口径一致', () => {
    const src = monitor();
    expect(src).toContain("navigate('/extensions?tab=skills')");
    expect(src).toContain("navigate('/extensions?tab=connectors')");
    expect(src).toContain('const navigate = useNavigate();');
    // 扩展页确实以 tab 查询参数驱动，跳转不会落到默认页
    expect(read('../../../src/renderer/pages/Extensions/index.tsx')).toContain(
      "searchParams.get('tab')",
    );
  });

  it('技能与连接器逐项展示真实名称与状态，禁用/需重装如实标注', () => {
    const src = monitor();
    expect(src).toContain("s.readable ? s.name : tr('{name}（需重装）', { name: s.name })");
    expect(src).toContain("s.enabled ? '#E6F7F4' : '#F3F4F6'");
    expect(src).toContain("s.status === 'connected'");
    expect(src).toContain("'已连接'");
    expect(src).toContain("'连接失败'");
    expect(src).toContain("'未连接'");
  });
});
