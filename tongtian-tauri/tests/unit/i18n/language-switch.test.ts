/**
 * 中英文切换（核心界面）
 *
 * 语言值来自共享 app settings；`I18nProvider` 订阅它并提供 `t()`。
 * 词典以中文原文为 key，未收录时回退中文而不是空白/key 泄漏。
 * 本文件锁定：词条完整、占位符不丢、核心界面确实走翻译函数、覆盖范围如实说明。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { EN_DICT } from '../../../src/renderer/i18n/en';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const placeholders = (text: string): string[] =>
  [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();

describe('英文词典', () => {
  it('核心界面词条齐全', () => {
    for (const key of [
      '通天办公',
      '新任务',
      '设置',
      '偏好设置',
      '语言',
      '界面风格',
      '对话字体',
      '对话宽度',
      '预览方式',
      '提示词建议',
      '默认展开工具调用',
      '任务监控',
      '文件预览',
      '正在执行：{desc}…',
      '执行过程（{done} 个步骤完成，{failed} 个失败）',
    ]) {
      expect(EN_DICT[key], `missing translation for ${key}`).toBeTruthy();
    }
  });

  it('每条译文非空且不残留中文', () => {
    for (const [zh, en] of Object.entries(EN_DICT)) {
      expect(en.trim(), `empty translation for ${zh}`).not.toBe('');
      expect(/[\u4e00-\u9fff]/.test(en), `untranslated Chinese in “${zh}” → “${en}”`).toBe(false);
    }
  });

  it('占位符与中文原文一一对应，插值参数不会丢', () => {
    for (const [zh, en] of Object.entries(EN_DICT)) {
      expect(placeholders(en), `placeholder mismatch for ${zh}`).toEqual(placeholders(zh));
    }
  });
});

describe('运行时接入', () => {
  const provider = () => read('../../../src/renderer/i18n/index.tsx');
  const main = () => read('../../../src/renderer/main.tsx');

  it('English / en 都识别，未设置或其它值回退中文', () => {
    const src = provider();
    expect(src).toContain("return value === 'English' || value === 'en' ? 'en' : 'zh';");
  });

  it('provider 订阅共享设置；缺失词条回退中文原文', () => {
    const src = provider();
    expect(src).toContain('useAppSettings()');
    expect(src).toContain('EN_DICT[zh] ?? zh');
    expect(src).toContain('useMemo<I18nValue>');
    expect(main()).toContain('<I18nProvider>');
  });

  it('核心界面都使用 useI18n（而不是各写一套）', () => {
    for (const rel of [
      '../../../src/renderer/components/Layout/Sidebar.tsx',
      '../../../src/renderer/pages/Workbench/index.tsx',
      '../../../src/renderer/components/ChatInput/index.tsx',
      '../../../src/renderer/components/ChatStream/index.tsx',
      '../../../src/renderer/components/TaskMonitor/index.tsx',
      '../../../src/renderer/components/FilePreview/index.tsx',
      '../../../src/renderer/pages/Settings/index.tsx',
    ]) {
      const src = read(rel);
      expect(src, `${rel} does not use useI18n`).toContain('useI18n()');
      expect(src).toContain("{ t: tr } = useI18n()");
    }
  });

  it('设置页如实说明只覆盖核心界面（不冒充整站翻译）', () => {
    const src = read('../../../src/renderer/pages/Settings/index.tsx');
    expect(src).toContain(
      "English 已覆盖侧边栏、工作台、输入框、对话与偏好设置；其余功能页仍显示中文原文",
    );
  });
});
