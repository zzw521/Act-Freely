/**
 * 通天办公 - 偏好设置状态管理
 *
 * 持久化到 localStorage，原型阶段本地生效
 */

import { create } from 'zustand';

// ── 状态接口 ─────────────────────────────────────────────────

export interface PreferenceState {
  theme: string;
  frostedGlass: boolean;
  chatFontSize: string;
  chatWidth: string;
  previewMode: string;
  promptSuggestions: boolean;
  set: (patch: Partial<Omit<PreferenceState, 'set'>>) => void;
}

const STORAGE_KEY = 'tt-preferences';

const defaults = {
  theme: '亮色',
  frostedGlass: true,
  chatFontSize: '中',
  chatWidth: '超宽',
  previewMode: '新窗口',
  promptSuggestions: true,
};

function loadInitial() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return { ...defaults, ...JSON.parse(raw) };
  } catch {
    // ignore
  }
  return defaults;
}

export const usePreferenceStore = create<PreferenceState>((set, get) => ({
  ...loadInitial(),
  set: (patch) => {
    set(patch);
    const { theme, frostedGlass, chatFontSize, chatWidth, previewMode, promptSuggestions } = get();
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ theme, frostedGlass, chatFontSize, chatWidth, previewMode, promptSuggestions }),
      );
    } catch {
      // ignore
    }
  },
}));
