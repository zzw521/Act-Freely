/**
 * 通天办公 - 应用设置持久化 hook（共享）
 *
 * 从主进程 settings:get 加载，变更时 settings:set 写回
 * （Tauri 下落地 sqlite 并触发系统行为；Mock 环境内存兜底）。
 * 设置页与 ChatInput（工作台模式选择）等共用同一份模块级缓存，
 * 任一处修改会立即通知所有已挂载的消费方。
 */

import { useSyncExternalStore } from 'react';
import { getAppSettingsSnapshot, subscribeAppSettings, updateAppSettings } from './app-settings-store';
import type { AppSettingsData } from '../../shared/contracts';

export type UpdateAppSettings = (
  patch: Partial<AppSettingsData>,
  options?: { strict?: boolean },
) => Promise<void>;

export function useAppSettings(): [Partial<AppSettingsData>, UpdateAppSettings] {
  const data = useSyncExternalStore(subscribeAppSettings, getAppSettingsSnapshot, getAppSettingsSnapshot);
  return [data, updateAppSettings];
}

// ── 工作台模式定义（设置页「工作台」分区与输入框模式选择共用） ──

export const WORKBENCH_MODE_DEFS = [
  { id: 'general', name: '通用', desc: '与 AI 助手对话（默认）' },
  { id: 'design', name: '设计', desc: '生成可在浏览器打开的单文件 HTML/CSS 原型' },
  { id: 'slides', name: '幻灯片', desc: '生成真实 .pptx 演示文稿（模型需支持工具调用）' },
  { id: 'writing', name: '写作', desc: '撰写、编辑和润色文档，长文落地为 Markdown' },
] as const;
