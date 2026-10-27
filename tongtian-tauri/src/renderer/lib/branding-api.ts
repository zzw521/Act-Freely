/**
 * 品牌外观 API：读取当前显示名/图标，提交单次自定义。
 *
 * - 桌面端（Tauri）：`branding_get` / `branding_customize` 落 settings_kv，
 *   并即时改写窗口标题、任务栏与托盘图标；
 * - 浏览器 / 引擎直连：没有系统窗口与托盘，返回默认品牌并标明 platform=browser，
 *   设置页据此禁用修改入口（不假装能改图标）。
 */

import { IPC_CHANNELS } from '../../shared/contracts';
import type { BrandingCustomizeReq, BrandingInfo } from '../../shared/contracts';

interface TongtianAPILike {
  invoke: (channel: string, ...args: unknown[]) => Promise<unknown>;
}

function getAPI(): TongtianAPILike | undefined {
  if (typeof window === 'undefined') return undefined;
  return (window as unknown as { tongtianAPI?: TongtianAPILike }).tongtianAPI;
}

export const DEFAULT_BRANDING: BrandingInfo = {
  defaultName: '任我行',
  attribution: '北京通天云计算有限公司',
  name: '任我行',
  iconDataUrl: null,
  customized: false,
  customizedAt: null,
  platform: 'browser',
};

function normalize(raw: unknown): BrandingInfo {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return { ...DEFAULT_BRANDING };
  const r = raw as Partial<BrandingInfo>;
  return {
    defaultName: typeof r.defaultName === 'string' ? r.defaultName : DEFAULT_BRANDING.defaultName,
    attribution: typeof r.attribution === 'string' ? r.attribution : DEFAULT_BRANDING.attribution,
    name: typeof r.name === 'string' && r.name.trim() ? r.name : DEFAULT_BRANDING.name,
    iconDataUrl: typeof r.iconDataUrl === 'string' ? r.iconDataUrl : null,
    customized: r.customized === true,
    customizedAt: typeof r.customizedAt === 'number' ? r.customizedAt : null,
    platform: r.platform === 'desktop' || r.platform === 'mock' ? r.platform : 'browser',
    applied: r.applied,
    warnings: Array.isArray(r.warnings) ? r.warnings.filter((w) => typeof w === 'string') : undefined,
  };
}

export async function getBranding(): Promise<BrandingInfo> {
  const api = getAPI();
  if (!api) return { ...DEFAULT_BRANDING };
  try {
    return normalize(await api.invoke(IPC_CHANNELS.BRANDING_GET));
  } catch {
    return { ...DEFAULT_BRANDING };
  }
}

export async function customizeBranding(req: BrandingCustomizeReq): Promise<BrandingInfo> {
  const api = getAPI();
  if (!api) {
    throw new Error('BRAND_UNSUPPORTED: 当前通道没有桌面窗口与托盘，无法修改应用名称或图标');
  }
  return normalize(await api.invoke(IPC_CHANNELS.BRANDING_CUSTOMIZE, req));
}
