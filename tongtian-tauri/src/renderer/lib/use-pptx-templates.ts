/**
 * 幻灯片模板台账（设置页 / 工作台输入框共用）
 *
 * 桌面端以 Rust 台账为准（内置主题 + 本机导入的 .pptx）；浏览器直连没有本机
 * 目录访问能力，只展示内置主题，并如实标注导入不可用。
 * 选中项落 app settings（桌面端即 settings_kv），Rust runtime 与引擎消费同一个 id。
 */

import { useCallback, useEffect, useState } from 'react';
import {
  BUILTIN_PPTX_TEMPLATES,
  DEFAULT_PPTX_TEMPLATE_ID,
  findBuiltinPptxTemplate,
  type PptxTemplate,
  type PptxTemplatesState,
} from '../../shared/pptx-templates';
import { errorTextOf } from '../../shared/error-messages';
import { isTauri } from './tauri-api';
import { pptxAPI } from './api';
import { useAppSettings } from './use-app-settings';

export interface UsePptxTemplates {
  desktop: boolean;
  loading: boolean;
  error: string;
  templates: PptxTemplate[];
  selectedId: string;
  /** 模板目录（浏览器通道为空串） */
  root: string;
  notes: string[];
  select: (id: string) => Promise<void>;
  importFile: (path: string) => Promise<PptxTemplatesState>;
  remove: (id: string) => Promise<PptxTemplatesState>;
  refresh: () => Promise<void>;
}

/**
 * @param active 为 false 时不扫盘（输入框只在幻灯片模式下才需要模板清单）
 */
export function usePptxTemplates(options?: { active?: boolean }): UsePptxTemplates {
  const [appSettings, updateAppSettings] = useAppSettings();
  const desktop = isTauri();
  const active = options?.active ?? true;
  const settingId =
    (appSettings.pptxTemplateId as string | undefined)?.trim() || DEFAULT_PPTX_TEMPLATE_ID;
  const [state, setState] = useState<PptxTemplatesState | null>(null);
  const [loading, setLoading] = useState(desktop && active);
  const [error, setError] = useState('');

  const refresh = useCallback(async () => {
    if (!active) return;
    if (!desktop) {
      const selected = findBuiltinPptxTemplate(settingId);
      setState({
        templates: BUILTIN_PPTX_TEMPLATES,
        selectedId: selected.id,
        selected,
        defaultId: DEFAULT_PPTX_TEMPLATE_ID,
        root: '',
        notes: [],
      });
      setLoading(false);
      setError('');
      return;
    }
    setLoading(true);
    try {
      const next = await pptxAPI.list();
      setState(next);
      setError('');
    } catch (e) {
      setError(errorTextOf(e, '模板列表加载失败'));
    } finally {
      setLoading(false);
    }
    // settingId 变化不需要重新扫盘：选择本身由 select 直接更新本地 state
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [desktop, active]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const templates = state?.templates ?? (desktop ? [] : BUILTIN_PPTX_TEMPLATES);
  const selectedId = desktop
    ? state?.selectedId ?? settingId
    : (templates.find((t) => t.id === settingId)?.id ?? DEFAULT_PPTX_TEMPLATE_ID);

  const select = useCallback(
    async (id: string) => {
      const exists = (state?.templates ?? templates).some(
        (t) => t.id === id && t.available,
      );
      if (!exists) throw new Error(`PPTX_TEMPLATE_NOT_FOUND: 模板 ${id} 不存在或不可用`);
      await updateAppSettings({ pptxTemplateId: id }, { strict: desktop });
      setState((prev) =>
        prev
          ? { ...prev, selectedId: id, selected: prev.templates.find((t) => t.id === id) }
          : prev,
      );
    },
    [state?.templates, templates, updateAppSettings, desktop],
  );

  const importFile = useCallback(
    async (path: string) => {
      const next = await pptxAPI.importFile(path);
      setState(next);
      // 导入不自动切换选中项：用户仍明确点一次「使用此模板」
      return next;
    },
    [],
  );

  const remove = useCallback(
    async (id: string) => {
      const next = await pptxAPI.remove(id);
      setState(next);
      // Rust 在删除的是选中模板时会写回默认 id；这里同步设置缓存，避免界面显示旧值
      if (next.selectedId !== settingId) {
        await updateAppSettings({ pptxTemplateId: next.selectedId });
      }
      return next;
    },
    [settingId, updateAppSettings],
  );

  return {
    desktop,
    loading,
    error,
    templates,
    selectedId,
    root: state?.root ?? '',
    notes: state?.notes ?? [],
    select,
    importFile,
    remove,
    refresh,
  };
}
