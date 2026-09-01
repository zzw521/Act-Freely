/**
 * 通天办公 - 应用设置持久化 hook（共享）
 *
 * 从主进程 settings:get 加载，变更时 settings:set 写回
 * （Tauri 下落地 sqlite 并触发系统行为；Mock 环境内存兜底）。
 * 设置页与 ChatInput（工作台模式选择）等共用此 hook，保证口径一致。
 */

import { useState, useEffect } from 'react';
import { getSettings, setSettings } from './alignment-api';
import type { AppSettingsData } from '../../shared/contracts';

export function useAppSettings(): [Partial<AppSettingsData>, (patch: Partial<AppSettingsData>) => void] {
  const [data, setData] = useState<Partial<AppSettingsData>>({});
  useEffect(() => {
    getSettings().then(setData).catch(() => undefined);
  }, []);
  const update = (patch: Partial<AppSettingsData>) => {
    setData((d) => ({ ...d, ...patch }));
    setSettings(patch).catch(() => undefined);
  };
  return [data, update];
}

// ── 工作台模式定义（设置页「工作台」分区与输入框模式选择共用） ──

export const WORKBENCH_MODE_DEFS = [
  { id: 'general', name: '通用', desc: '与 AI 助手对话（默认）' },
  { id: 'design', name: '设计', desc: '使用画布进行设计' },
  { id: 'slides', name: '幻灯片', desc: '使用 AI Slides 制作演示文稿' },
  { id: 'writing', name: '写作', desc: '撰写、编辑和润色文档' },
] as const;
