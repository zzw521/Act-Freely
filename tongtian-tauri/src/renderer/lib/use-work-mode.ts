/**
 * 工作台模式解析（输入框胶囊组 / 右侧画布 / 设置页共用）
 *
 * 口径与设置页一致：workbenchModes 决定可见模式，workbenchDefaultMode 只在
 * 用户尚未选择时生效；默认模式被关掉时退到第一个仍启用的模式。
 * 选择寄存在 workbenchStore：欢迎页与对话态各挂一个 ChatInput，实例 state
 * 会在首轮发送后随重挂丢失。
 */

import { useEffect } from 'react';
import type { WorkMode } from '../../shared/types';
import { useAppSettings, WORKBENCH_MODE_DEFS } from './use-app-settings';
import { useWorkbenchStore } from '../store/workbenchStore';

const DEFAULT_ENABLED_MODES = ['general', 'design', 'slides'];

/** 各模式的输入区占位文案；通用模式沿用调用方传入的 placeholder */
export const MODE_PLACEHOLDERS: Record<WorkMode, string> = {
  general: '',
  design: '描述要设计的页面或原型，例如：做一个产品落地页',
  slides: '描述演示主题与页数，例如：把这份材料做成 8 页汇报',
  writing: '描述要写的文档，例如：写一份本周项目周报',
};

export interface WorkModeState {
  activeMode: WorkMode;
  enabledDefs: (typeof WORKBENCH_MODE_DEFS)[number][];
  setWorkMode: (mode: WorkMode) => void;
}

export function useWorkMode(): WorkModeState {
  const [appSettings] = useAppSettings();
  const workMode = useWorkbenchStore((s) => s.workMode);
  const setWorkMode = useWorkbenchStore((s) => s.setWorkMode);

  const enabledModes = (appSettings.workbenchModes as string[] | undefined) ?? DEFAULT_ENABLED_MODES;
  const enabledDefs = WORKBENCH_MODE_DEFS.filter((d) => enabledModes.includes(d.id));
  // 默认模式兼容存 id（general）或存中文名（通用）两种历史格式
  const rawDefault = (appSettings.workbenchDefaultMode as string) ?? 'general';
  const configuredDefault = (WORKBENCH_MODE_DEFS.find((d) => d.id === rawDefault)?.id ??
    WORKBENCH_MODE_DEFS.find((d) => d.name === rawDefault)?.id ??
    'general') as WorkMode;
  // 配置的默认模式被关掉时，退到第一个仍启用的模式，避免「默认设计但设计已禁用」的循环
  const fallbackMode: WorkMode = enabledModes.includes(configuredDefault)
    ? configuredDefault
    : ((enabledDefs[0]?.id as WorkMode | undefined) ?? 'general');

  const enabledKey = enabledModes.join(',');
  useEffect(() => {
    if (!workMode || !enabledModes.includes(workMode)) setWorkMode(fallbackMode);
    // enabledModes 每次渲染都是新数组，用拼接串作为稳定依赖
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workMode, enabledKey, fallbackMode, setWorkMode]);

  // 设置变更到 effect 落地之间，也不把已被禁用的模式当成当前模式
  const activeMode =
    workMode && enabledModes.includes(workMode) ? workMode : fallbackMode;

  return { activeMode, enabledDefs, setWorkMode };
}
