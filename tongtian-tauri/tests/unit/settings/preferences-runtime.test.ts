/**
 * 偏好设置的运行时归属（禁止「能点、能保存、实际不动」）
 *
 * 语言 / 界面风格 / 对话字体 / 对话宽度 / 预览方式 / 提示词建议 / 工具块默认展开
 * 都已从禁用占位改为真实消费方。本文件逐项锁定「设置写哪里 → 谁在读」，
 * 防止后续改动把消费方删掉、只留下一个会保存的开关。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const settings = () => read('../../../src/renderer/pages/Settings/index.tsx');
const themedApp = () => read('../../../src/renderer/theme/ThemedApp.tsx');
const globalsCss = () => read('../../../src/renderer/styles/globals.css');
const workbench = () => read('../../../src/renderer/pages/Workbench/index.tsx');
const chatInput = () => read('../../../src/renderer/components/ChatInput/index.tsx');
const chatStream = () => read('../../../src/renderer/components/ChatStream/index.tsx');
const filePreview = () => read('../../../src/renderer/components/FilePreview/index.tsx');

describe('界面风格与对话字体', () => {
  it('设置值写进共享 app settings，不在组件 state 里自生自灭', () => {
    const src = settings();
    expect(src).toContain('updateAppSettings({ uiStyle: v })');
    expect(src).toContain('updateAppSettings({ chatFont: v })');
    expect(src).toContain('const uiStyle = (appSettings.uiStyle as string) ?? \'默认\';');
    expect(src).toContain('const chatFont = (appSettings.chatFont as string) ?? \'无衬线\';');
  });

  it('四套皮肤映射为根节点 data-ui-style，CSS 用属性选择器真正换肤', () => {
    const tsx = themedApp();
    for (const [stored, attr] of [
      ['默认', 'default'],
      ['玻璃', 'glass'],
      ['经典', 'classic'],
      ['羊皮纸', 'parchment'],
    ]) {
      expect(tsx).toContain(`${stored}: '${attr}'`);
    }
    expect(tsx).toContain("document.documentElement.setAttribute('data-ui-style'");
    const css = globalsCss();
    for (const attr of ['glass', 'classic', 'parchment']) {
      expect(css).toContain(`html[data-ui-style='${attr}']`);
    }
    // 暗色下皮肤不能失效
    expect(css).toContain("html[data-theme='dark'][data-ui-style='glass']");
  });

  it('衬线字体写 data-chat-font，只作用于对话正文与输入', () => {
    const tsx = themedApp();
    expect(tsx).toContain("无衬线: 'sans'");
    expect(tsx).toContain("衬线: 'serif'");
    expect(tsx).toContain("document.documentElement.setAttribute('data-chat-font'");
    const css = globalsCss();
    expect(css).toContain("html[data-chat-font='serif']");
    expect(css).toContain('--tt-chat-font');
  });
});

describe('对话宽度', () => {
  it('标准 / 宽 / 全宽三档在欢迎页与会话态都参与布局', () => {
    const src = workbench();
    expect(src).toContain("if (chatWidth === '全宽') return '100%';");
    expect(src).toContain("if (chatWidth === '宽') return 1100;");
    expect(src).toContain('return 860; // 标准');
    // 两个 ChatInput 挂载点都要读同一份设置
    expect(src.match(/appSettings\.chatWidth/g)?.length).toBeGreaterThanOrEqual(2);
    expect(src.match(/resolveChatMaxWidth\(chatWidth\)/g)?.length).toBeGreaterThanOrEqual(2);
  });
});

describe('提示词建议', () => {
  it('缺省开启；仅空输入、无附件、未引用技能且非生成中时展示', () => {
    const src = chatInput();
    expect(src).toContain('const promptSuggestionEnabled = appSettings.promptSuggestion !== false;');
    expect(src).toContain('const suggestions = PROMPT_SUGGESTIONS[activeMode] ?? PROMPT_SUGGESTIONS.general;');
    for (const gate of [
      'promptSuggestionEnabled &&',
      '!loading &&',
      "value.trim() === '' &&",
      'attachments.length === 0 &&',
      '!referencedSkill',
    ]) {
      expect(src).toContain(gate);
    }
  });

  it('点击建议只回填输入框，不直接发送', () => {
    const src = chatInput();
    expect(src).toContain('setValue(tr(s));');
    expect(src).toContain('inputRef.current?.focus()');
    const chip = src.split('suggestions.map((s) =>')[1].split('</Chip>')[0];
    expect(chip).not.toContain('onSend');
  });
});

describe('预览方式', () => {
  it('应用内预览返回路径；系统默认程序交给 fileAPI，失败回退并说明', () => {
    const src = read('../../../src/renderer/lib/artifact-open.ts');
    expect(src).toContain("if (previewMode !== '系统默认程序')");
    expect(src).toContain('await fileAPI.openExternal(path)');
    expect(src).toContain("已改为应用内预览");
  });

  it('对话流与任务栏的产物都按设置分流，系统应用失败时回退应用内预览', () => {
    const wb = workbench();
    expect(wb).toContain("const previewMode = (appSettings.previewMode as string) ?? '应用内预览';");
    expect(wb).toContain('await openArtifact(path, previewMode)');
    // 消息流与任务栏点击产物都回到工作台同一份预览状态（面板内预览，不再各挂一个弹窗）
    expect(chatStream()).toContain('onArtifactOpen');
    expect(wb).toContain('setArtifactPreview(result.previewPath)');
    expect(wb).toContain('setMonitorOpen(true)');
    expect(filePreview()).toContain('fileAPI.openExternal(path)');
  });
});
