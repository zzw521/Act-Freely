/**
 * 推荐案例 / 任务模板的封面与提示词约定
 *
 * 封面是随包资源（已裁掉生成水印的 WebP），提示词要写清交付物、结构、字段与示例数据口径，
 * 不能退回一句「帮我做一个…」。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const workbench = () => read('../../../src/renderer/pages/Workbench/index.tsx');

const COVERS = [
  'homestay',
  'influencer-dashboard',
  'ecommerce-dashboard',
  'monthly-review',
  'course-site',
  'ai-course-landing',
  'product-visuals',
  'sales-report',
  'dunhuang-article',
];

describe('推荐案例与任务模板', () => {
  it('九条模板都引用随包封面图，WebP 资源真实存在', () => {
    const src = workbench();
    expect((src.match(/prompt: '/g) ?? []).length).toBe(9);
    for (const name of COVERS) {
      expect(src).toContain(`../../assets/showcase/${name}.webp`);
      const file = fileURLToPath(new URL(`../../../src/renderer/assets/showcase/${name}.webp`, import.meta.url));
      expect(statSync(file).size).toBeGreaterThan(15_000);
      expect(readFileSync(file).subarray(0, 4).toString('ascii')).toBe('RIFF');
    }
  });

  it('提示词交代交付物、结构或字段，而不是一句话目标', () => {
    const src = workbench();
    expect(src).toContain('输出单文件 HTML');
    expect(src).toContain('（.xlsx）');
    expect(src).toContain('（.docx）');
    expect(src).toContain('示例数据');
    // 卡片不再依赖纯渐变色和手写文字颜色
    expect(src).not.toContain('titleColor');
    expect(src).not.toContain('subtitleColor');
  });

  it('推荐案例和模板弹窗都渲染真实封面，并压暗底部保证文字可读', () => {
    const src = workbench();
    expect(src).toContain('src={item.cover}');
    expect(src).toContain('src={t.cover}');
    expect(src).toContain("objectFit: 'cover'");
    expect(src).toContain("linear-gradient(180deg, rgba(2, 6, 23, 0) 42%");
    expect(src).toContain("linear-gradient(180deg, rgba(2, 6, 23, 0) 38%");
  });
});
