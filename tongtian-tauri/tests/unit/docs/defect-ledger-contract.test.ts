/**
 * 缺陷台账契约（D-22）
 *
 * 台账是排期与验收的唯一入口：小节数、总表行数、§1.1 状态矩阵、状态明细四者
 * 必须互相咬合，产品名与模式口径也不能再漂回旧值。此前 44/45 的计数误差和
 * 「本机缺 Rust 工具链」的过期结论都是靠人工发现，这里改为全量扫描拦截。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');

const defectDoc = read('../../../../docs/缺陷与实现计划-任我行.md');
const packageJson = JSON.parse(read('../../../package.json')) as { description: string };
const tauriConf = JSON.parse(read('../../../src-tauri/tauri.conf.json')) as {
  productName: string;
  app?: { windows?: Array<{ title?: string }> };
};
const readme = read('../../../../README.md');

const ids = [...defectDoc.matchAll(/^### (D-\d+)/gm)].map((m) => m[1]);
const tableRows = [...defectDoc.matchAll(/^\| (D-\d+) \| (P\d) \|/gm)].map((m) => ({
  id: m[1],
  level: m[2],
}));

function matrix() {
  const rows = [...defectDoc.matchAll(/^\| (P\d|合计) \| (\d+) \| (\d+) \| (\d+) \| (\d+) \| (\d+) \|$/gm)];
  return Object.fromEntries(
    rows.map((m) => [
      m[1],
      {
        total: Number(m[2]),
        closed: Number(m[3]),
        verify: Number(m[4]),
        doing: Number(m[5]),
        pending: Number(m[6]),
      },
    ]),
  );
}

function statusLine() {
  const match = defectDoc.match(/状态明细：已关闭 ([^；]+)；待验证 ([^；]+)；进行中 ([^。]+)。/);
  expect(match, '§1.1 必须有「状态明细」句').toBeTruthy();
  const split = (raw: string) => raw.split('、').map((s) => s.trim()).filter(Boolean);
  return {
    closed: split(match![1]),
    verify: split(match![2]),
    doing: split(match![3]),
  };
}

describe('缺陷台账：数量与状态自洽（D-22）', () => {
  it('小节与总表都是 48 项，且 ID 一一对应', () => {
    expect(ids.length).toBe(48);
    expect(new Set(ids).size).toBe(48);
    expect(tableRows.length).toBe(48);
    expect(tableRows.map((r) => r.id)).toEqual(ids);
  });

  it('总表按级别的数量与 §1.1 矩阵一致', () => {
    const m = matrix();
    for (const level of ['P0', 'P1', 'P2', 'P3']) {
      expect(tableRows.filter((r) => r.level === level).length, level).toBe(m[level].total);
    }
    expect(m['合计'].total).toBe(48);
    expect(['P0', 'P1', 'P2', 'P3'].reduce((sum, l) => sum + m[l].total, 0)).toBe(48);
  });

  it('状态矩阵各列相加等于总数，状态明细与矩阵计数一致', () => {
    const m = matrix();
    for (const [key, row] of Object.entries(m)) {
      expect(row.closed + row.verify + row.doing + row.pending, key).toBe(row.total);
    }
    const total = m['合计'];
    for (const col of ['closed', 'verify', 'doing', 'pending'] as const) {
      expect(
        ['P0', 'P1', 'P2', 'P3'].reduce((sum, l) => sum + m[l][col], 0),
        `合计.${col}`,
      ).toBe(total[col]);
    }
    const s = statusLine();
    expect(s.closed.length).toBe(total.closed);
    expect(s.verify.length).toBe(total.verify);
    expect(s.doing.length).toBe(total.doing);
    // 明细里出现的 ID 必须都在台账内，且三类互斥
    const listed = [...s.closed, ...s.verify, ...s.doing];
    expect(new Set(listed).size).toBe(listed.length);
    for (const id of listed) expect(ids, id).toContain(id);
    expect(listed.length).toBe(total.closed + total.verify + total.doing);
  });

  it('文件头版本不低于 1.3、日期不早于 2026-10-30，进度段不再声称缺工具链', () => {
    // 版本与日期只钉下限，不钉具体值：钉死成某一天会在下一次台账更新时假红
    const version = defectDoc.match(/\| 文档版本 \| 1\.(\d+) \|/);
    expect(version, '文件头必须有「| 文档版本 | 1.x |」').toBeTruthy();
    expect(Number(version![1]), '文档版本不低于 1.3').toBeGreaterThanOrEqual(3);
    const date = defectDoc.match(/\| 日期 \| (\d{4}-\d{2}-\d{2}) \|/);
    expect(date, '文件头必须有「| 日期 | YYYY-MM-DD |」').toBeTruthy();
    expect(date![1] >= '2026-10-30', `文档日期不早于 2026-10-30，实为 ${date![1]}`).toBe(true);
    // 允许 D-22 小节引用旧结论作为「修正前」的证据，其余位置不得再声称缺工具链
    const stale = defectDoc
      .split('\n')
      .filter((line) => line.includes('工具链'))
      .filter((line) => !line.startsWith('**现状（修正前）**') && !line.startsWith('- 文件头版本'));
    expect(stale, stale.join('\n')).toEqual([]);
  });
});

describe('产品名与模式口径（D-22）', () => {
  it('package.json 与 Tauri 配置都用「任我行」', () => {
    expect(packageJson.description).toContain('任我行');
    expect(packageJson.description).not.toContain('通天办公');
    expect(tauriConf.productName).toBe('任我行');
    // 主窗口由 Rust 创建（配置窗口挂不上导航守卫），标题以 navigation.rs 为准
    expect(tauriConf.app?.windows ?? []).toEqual([]);
    expect(read('../../../src-tauri/src/system/navigation.rs')).toContain('.title("任我行")');
  });

  it('README 写默认三种模式、写作可选', () => {
    expect(readme).toContain('通用 / 设计 / 幻灯片三种默认模式（写作可选）');
    expect(readme).toContain('写作模式可在设置中开启');
  });
});
