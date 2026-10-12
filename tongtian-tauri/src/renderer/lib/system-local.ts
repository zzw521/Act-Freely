/**
 * 开机自启动的本地实现（浏览器直连 / Mock 通道）
 *
 * 与代理状态同一口径：非桌面环境拿不到系统启动项，就如实报
 * `supported=false, enabled=false`，绝不把「设置里勾了」当成「已登记到系统」。
 * 桌面端由 Rust 读注册表给事实（见 src-tauri/src/system/autostart.rs）。
 */

import type { AutostartStatus } from '../../shared/contracts';

// 与 Rust 侧一致的值名/键名常量（界面「查看启动项位置」用同一份，不各写一套）
export const AUTOSTART_VALUE_NAME = 'TongTianOffice';
export const AUTOSTART_RUN_KEY = 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run';

/** 浏览器/Mock 态：只有意愿（requested），没有任何系统级登记可言 */
export function localAutostart(input: Record<string, unknown>): AutostartStatus {
  return {
    supported: false,
    requested: isTruthySetting(input.autoStart),
    enabled: false,
    command: null,
    currentCommand: '',
    stale: false,
    valueName: AUTOSTART_VALUE_NAME,
    runKey: AUTOSTART_RUN_KEY,
  };
}

/** 与 Rust `setting_bool` 同口径：兼容 JSON 布尔与带/不带引号的裸字符串两种历史存法 */
function isTruthySetting(raw: unknown): boolean {
  if (raw === true) return true;
  if (typeof raw !== 'string') return false;
  return raw.trim().replace(/^"+|"+$/g, '') === 'true';
}
