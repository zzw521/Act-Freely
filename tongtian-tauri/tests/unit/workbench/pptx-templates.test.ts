/**
 * 幻灯片模板库契约
 *
 * 锁住三件事：
 * - 数据侧：内置主题 id/配色前后端一致，浏览器直连不假装有导入模板
 * - 设置页：列表/选择/导入/删除都落到真实台账，浏览器通道如实禁用导入
 * - 输入框：只在幻灯片模式出现模板选择，选择写回同一份 app settings
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  BUILTIN_PPTX_TEMPLATES,
  DEFAULT_PPTX_TEMPLATE_ID,
  browserPptxTemplates,
  findBuiltinPptxTemplate,
} from '../../../src/shared/pptx-templates';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const settingsPage = () => read('../../../src/renderer/pages/Settings/index.tsx');
const chatInput = () => read('../../../src/renderer/components/ChatInput/index.tsx');
const pptxHook = () => read('../../../src/renderer/lib/use-pptx-templates.ts');

describe('幻灯片模板共享契约', () => {
  it('内置主题 id 唯一、默认 id 存在且主题字段完整', () => {
    const ids = BUILTIN_PPTX_TEMPLATES.map((t) => t.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids).toContain(DEFAULT_PPTX_TEMPLATE_ID);
    for (const tpl of BUILTIN_PPTX_TEMPLATES) {
      expect(tpl.builtin).toBe(true);
      expect(tpl.available).toBe(true);
      expect(tpl.theme?.background).toMatch(/^#[0-9A-Fa-f]{6}$/);
      expect(tpl.theme?.accent).toMatch(/^#[0-9A-Fa-f]{6}$/);
      expect(tpl.theme?.titleSize).toBeGreaterThan(0);
      expect(tpl.theme?.bodySize).toBeGreaterThan(0);
    }
  });

  it('浏览器直连只下发内置主题；选中导入模板时退回默认并说明原因', () => {
    const builtin = browserPptxTemplates('warm-earth');
    expect(builtin.templates.every((t) => t.builtin)).toBe(true);
    expect(builtin.selectedId).toBe('warm-earth');
    expect(builtin.notes).toBeUndefined();

    const imported = browserPptxTemplates('imported-1760000000000');
    expect(imported.selectedId).toBe(DEFAULT_PPTX_TEMPLATE_ID);
    expect(imported.notes?.[0]).toContain('只在桌面版可用');
    expect(findBuiltinPptxTemplate('imported-x').id).toBe(DEFAULT_PPTX_TEMPLATE_ID);
  });

  it('前端内置主题与 Rust 台账的 id/配色一致', () => {
    const rust = read('../../../src-tauri/src/pptx/mod.rs');
    expect(rust).toContain(`pub const DEFAULT_TEMPLATE_ID: &str = "${DEFAULT_PPTX_TEMPLATE_ID}";`);
    expect(rust).toContain('pub const SELECTED_KEY: &str = "pptxTemplateId";');
    for (const tpl of BUILTIN_PPTX_TEMPLATES) {
      expect(rust).toContain(`id: "${tpl.id}".to_string()`);
      expect(rust).toContain(`name: "${tpl.name}".to_string()`);
      expect(rust).toContain(`"${tpl.theme!.accent}"`);
      expect(rust).toContain(`"${tpl.theme!.background}"`);
    }
  });
});

describe('设置页模板管理', () => {
  it('模板区使用共享台账 hook，并列出选择/导入/删除三个真实动作', () => {
    const src = settingsPage();
    expect(src).toContain('usePptxTemplates()');
    expect(src).toContain('data-pptx-template={tpl.id}');
    expect(src).toContain('await importFile(picked)');
    expect(src).toContain('await select(id)');
    expect(src).toContain('await remove(pendingDelete.id)');
    expect(src).toContain("filters: [{ name: 'PowerPoint 模板', extensions: ['pptx'] }]");
  });

  it('导入只在桌面端可用，浏览器通道给出明确原因而不是假按钮', () => {
    const src = settingsPage();
    expect(src).toContain('浏览器直连没有本机文件访问能力，请在桌面版导入模板');
    expect(src).toContain('disabled={!desktop || importing || loading}');
  });

  it('删除走二次确认，并说明文件移入 .trash/ 而非物理删除', () => {
    const src = settingsPage();
    expect(src).toContain('删除幻灯片模板');
    expect(src).toContain('已移入回收目录（.trash/）');
    expect(src).toContain('文件移到模板目录的 .trash/ 下（不会直接删除）');
  });

  it('文件缺失的模板如实标注且不能选中', () => {
    const src = settingsPage();
    expect(src).toContain('文件不可用');
    expect(src).toContain('disabled={!tpl.available || Boolean(busyId)}');
    expect(src).toContain("tpl.error || '模板文件已不存在，请重新导入'");
  });
});

describe('输入框幻灯片模板选择', () => {
  it('仅在幻灯片模式出现，且只列可用模板', () => {
    const src = chatInput();
    const pickerIdx = src.indexOf('data-pptx-picker');
    expect(pickerIdx).toBeGreaterThan(-1);
    const guardIdx = src.lastIndexOf("{activeMode === 'slides' && (", pickerIdx);
    expect(guardIdx).toBeGreaterThan(-1);
    expect(src).toContain('const pptxOptions = pptx.templates.filter((t) => t.available);');
    expect(src).toContain('usePptxTemplates({ active: activeMode === \'slides\' })');
  });

  it('选择写回 app settings；导入入口留在设置页，输入框不重复摆按钮', () => {
    const src = chatInput();
    const hook = pptxHook();
    expect(src).toContain('await pptx.select(id)');
    expect(hook).toContain('updateAppSettings({ pptxTemplateId: id }, { strict: desktop })');
    expect(src).not.toContain('导入 .pptx');
  });

  it('浏览器直连在输入框里如实说明只有内置主题', () => {
    expect(chatInput()).toContain("tr('浏览器直连仅内置主题')");
  });
});
