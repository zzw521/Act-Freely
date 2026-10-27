/**
 * 品牌外观状态（侧边栏与设置页共用）。
 *
 * 默认值先按「任我行」渲染，挂载后向 Rust / Mock 拉取真实值：
 * 自定义过的安装重启后直接显示上次的名称与图标，不需要用户再操作。
 */

import { create } from 'zustand';
import type { BrandingCustomizeReq, BrandingInfo } from '../../shared/contracts';
import { DEFAULT_BRANDING, customizeBranding, getBranding } from '../lib/branding-api';

interface BrandingState {
  info: BrandingInfo;
  /** 是否已完成首次读取 */
  loaded: boolean;
  loading: boolean;
  error: string | null;

  load: () => Promise<void>;
  customize: (req: BrandingCustomizeReq) => Promise<BrandingInfo>;
}

export const useBrandingStore = create<BrandingState>()((set, get) => ({
  info: { ...DEFAULT_BRANDING },
  loaded: false,
  loading: false,
  error: null,

  load: async () => {
    const { loaded, loading } = get();
    if (loaded || loading) return;
    set({ loading: true });
    try {
      const info = await getBranding();
      set({ info, loaded: true, loading: false, error: null });
    } catch (e) {
      set({ loading: false, error: e instanceof Error ? e.message : String(e) });
    }
  },

  customize: async (req) => {
    const info = await customizeBranding(req);
    set({ info, loaded: true, error: null });
    return info;
  },
}));

/** 当前显示名称（拉取前即为默认名，不会闪空） */
export function useBrandName(): string {
  return useBrandingStore((s) => s.info.name);
}
