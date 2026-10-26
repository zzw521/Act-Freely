/**
 * 未接入能力不得留「能点、能保存、实际不动」的控件（P0 诚实性收口）
 *
 * 电脑操控执行器已在 Windows 桌面版接通；页面必须读真实能力视图、可写策略并支持截屏自检，
 * 同时在非 Windows/浏览器通道如实说明没有执行器，不能拿「保存成功」冒充「现在能用」。
 * 设置页的语音/快照能力本身仍没有运行时，本文件锁定它们统一显示「尚未提供」并禁用。
 * QuickPick 与隐私模式已接通真实链路（应用内快捷键 → 工作台提交；三层落库开关），
 * 改为锁定「可操作且写入口径真实」——不再用禁用控件冒充未完成。
 * 语言/界面风格/对话字体/预览方式/提示词建议/工具块展开已接通真实消费方，
 * 同样锁定可操作与生效设置。
 * 自动记忆已在引擎收尾链路落地，单独在下方锁定「可操作且写入口径真实」。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const settings = () => read('../../../src/renderer/pages/Settings/index.tsx');
const computerUse = () => read('../../../src/renderer/pages/ComputerUsePanel/index.tsx');

describe('电脑操控页读真实能力视图并写真实策略', () => {
  it('不再笼统宣称未接入，也不再用「保存成功」冒充「现在能用」', () => {
    const src = computerUse();
    expect(src).not.toContain('电脑操控执行器尚未接入');
    expect(src).not.toContain('当前版本不会截屏、点击或输入');
    expect(src).toContain('getComputerUseCapabilities');
    expect(src).toContain('执行器可用');
    expect(src).toContain('执行器不可用');
    expect(src).toContain('当前通道没有执行器，档位开关无法生效，因此不可修改');
  });

  it('档位写入 settings 的真实策略，自动执行需二次确认', () => {
    const src = computerUse();
    expect(src).toContain('setComputerUsePolicy');
    expect(src).toContain('computer-use-policy-');
    expect(src).toContain('confirmAuto');
    expect(src).toContain('确认启用');
    expect(src).not.toContain('本机已保存档位：');
  });

  it('截屏自检走真实 command，并写明图片不落盘、审计不记画面', () => {
    const src = computerUse();
    expect(src).toContain('captureComputerUseScreen');
    expect(src).toContain('computer-use-capture');
    expect(src).toContain('图片仅在当前页面内存中预览，不落盘');
    expect(src).toContain('审计只记录截屏尺寸，不记录画面内容');
  });

  it('能力边界如实列出：平台、视觉、无 OCR、子 Agent、上限', () => {
    const src = computerUse();
    for (const boundary of [
      '仅 Windows 桌面版',
      '需要视觉模型',
      '没有 OCR',
      '子 Agent 不继承电脑操控工具',
      '截屏 20 次、操作 60 次',
      'Win 键组合与任务管理器快捷键会被拒绝',
    ]) {
      expect(src).toContain(boundary);
    }
    expect(src).not.toContain('可直接使用');
  });
});

describe('设置页未接入的开关统一禁用并标注', () => {
  it('SettingRow 提供统一的「尚未提供」标签', () => {
    const src = settings();
    expect(src).toContain('const UnavailableTag');
    expect(src).toContain('尚未提供');
    expect(src).toContain('unavailable?: boolean;');
    expect(src).toContain('{unavailable && <UnavailableTag />}');
  });

  it('偏好设置里已接通的项可操作，不再用「暂不生效」搪塞', () => {
    const src = settings();
    expect(src).not.toContain('修改后立即切换');
    for (const stale of [
      '已保存的语言偏好暂不生效',
      '已保存的风格偏好暂不生效',
      '已保存的字体偏好暂不生效',
      '已保存的预览偏好暂不生效',
      '提示词建议尚未提供',
      '工具块默认展开尚未提供',
    ]) {
      expect(src).not.toContain(stale);
    }
    // 六项都写进 app settings（输入框、对话流、主题容器、产物打开处消费）
    expect(src).toContain("options={['中文', 'English']}");
    expect(src).toContain('updateAppSettings({ language: v })');
    expect(src).toContain('updateAppSettings({ uiStyle: v })');
    expect(src).toContain('updateAppSettings({ chatFont: v })');
    expect(src).toContain('updateAppSettings({ previewMode: v })');
    expect(src).toContain('updateAppSettings({ promptSuggestion: e.target.checked })');
    expect(src).toContain('updateAppSettings({ expandToolCalls: e.target.checked })');
    // 语言覆盖范围要如实写清，不能声称整站已翻译
    expect(src).toContain('English 已覆盖侧边栏、工作台、输入框、对话与偏好设置；其余功能页仍显示中文原文');
  });

  it('语音与应用快照仍不可操作，并写明缺什么', () => {
    const src = settings();
    for (const reason of [
      '语音输入（录音与转录）尚未提供',
      '触发方式随语音输入一起提供',
      '应用快照尚未提供，当前不会截取任何屏幕内容',
    ]) {
      expect(src).toContain(reason);
    }
    expect(src).not.toContain('updateAppSettings({ voiceEnabled:');
    expect(src).not.toContain('updateAppSettings({ snapshotEnabled:');
  });

  it('QuickPick 与隐私模式已接通，开关写入的是真实生效设置', () => {
    const src = settings();
    // 旧版禁用理由与假承诺不得回归
    expect(src).not.toContain('QuickPick 全局快速任务窗尚未提供');
    expect(src).not.toContain('触发方式随 QuickPick 一起提供');
    expect(src).not.toContain('隐私模式尚未接入运行时，开关不会改变任何行为');
    // 开关真实写入 app settings，并各自带真实消费说明
    expect(src).toContain('updateAppSettings({ quickPickEnabled:');
    expect(src).toContain('updateAppSettings({ privacyMode:');
    expect(src).toContain('应用内快捷键：仅当通天办公窗口位于前台时生效；系统级全局热键尚未提供');
    expect(src).toContain('新任务的目标与回复不写入本地会话库');
  });

  it('真实生效的设置保持可操作（主题、字号、对话宽度、快捷键）', () => {
    const src = settings();
    expect(src).toContain('onChange={(v) => prefs.set({ theme: v })}');
    expect(src).toContain('updateAppSettings({ chatFontSize: v as number })');
    expect(src).toContain('updateAppSettings({ chatWidth: v })');
    expect(src).toContain('updateAppSettings({ shortcuts: next })');
    expect(src).toContain('onChange={(e) => updateAppSettings({ keepAwake: e.target.checked })}');
  });
});

describe('意识默认模板不再承诺自动写入', () => {
  const awarenessSources = () => [
    settings(),
    read('../../../src/renderer/lib/awareness-local.ts'),
    read('../../../src-tauri/src/awareness/mod.rs'),
    read('../../../engine/server.py'),
  ];

  it('四处（设置页/本地台账/Rust/引擎）默认模板一致且改为手动新增', () => {
    for (const src of awarenessSources()) {
      expect(src).toContain('（暂无记忆条目，可在下方手动新增）');
      expect(src).not.toContain('自动沉淀');
      expect(src).not.toContain('（自动维护中）');
    }
  });

  it('长期记忆空态指向手动新增，自动记忆开关可操作且写入口径可见', () => {
    const src = settings();
    expect(src).toContain('暂无记忆条目，可在下方手动新增');
    expect(src).toContain('onChange={(e) => patchCfg({ autoMemory: e.target.checked })}');
    expect(src).toContain('任务成功后，把目标、结论与产物摘要写入 MEMORY.md');
    expect(src).toContain('疑似凭据不写入，可在下方查看和删除');
  });
});
