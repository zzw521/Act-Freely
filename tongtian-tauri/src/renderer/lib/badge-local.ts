/**
 * 任务栏未读角标（设置页「任务栏未读状态」的消费方）
 *
 * 事实口径：
 * - Windows：Rust 用窗口图标叠加（overlay icon）画红底数字，系统不支持数字角标；
 * - macOS：Rust 用系统 Dock 徽标计数；
 * - 浏览器 / 其他平台：明确不支持，不假装有角标。
 *
 * 累计规则与桌面通知一致：任务到了「需要用户回来」的节点（完成/待审阅/失败），
 * 且窗口不在前台时记一条未读；窗口回到前台立即清零。开关关闭时不下发角标。
 */

import { IPC_CHANNELS } from '../../shared/contracts';
import { getSettings } from './alignment-api';
import { detectOs } from './permissions-local';

export interface BadgeCapability {
  supported: boolean;
  platform: string;
  mode: 'overlay' | 'count' | 'none' | string;
  detail: string;
}

/** 浏览器等非 Tauri 通道：按平台事实给出能力，且 supported=false（没有下发通道） */
function localCapability(): BadgeCapability {
  const os = detectOs();
  return {
    supported: false,
    platform: os,
    mode: 'none',
    detail:
      os === 'web' || os === 'unknown'
        ? '当前为浏览器预览，任务栏角标仅在桌面版可用'
        : '当前运行通道无法写入系统任务栏，角标仅在桌面版可用',
  };
}

let capabilityPromise: Promise<BadgeCapability> | null = null;

export function badgeCapability(): Promise<BadgeCapability> {
  if (capabilityPromise) return capabilityPromise;
  capabilityPromise = (async () => {
    const api = typeof window !== 'undefined' ? window.tongtianAPI : undefined;
    if (!api) return localCapability();
    try {
      const raw = await api.invoke(IPC_CHANNELS.BADGE_SUPPORT);
      if (raw && typeof raw === 'object' && typeof (raw as BadgeCapability).supported === 'boolean') {
        return raw as BadgeCapability;
      }
    } catch {
      // 通道存在但命令失败：回落为不支持，界面会显示原因而不是假开关
    }
    return localCapability();
  })();
  return capabilityPromise;
}

/** 未读任务集合（按任务去重，避免同一任务多次事件把数量刷高） */
const unreadTasks = new Set<string>();
/** 最近一次成功下发的数量：相同数量不重复 IPC */
let lastSent: number | null = null;

export function taskbarUnreadCount(): number {
  return unreadTasks.size;
}

async function pushBadge(): Promise<void> {
  const capability = await badgeCapability();
  if (!capability.supported) return;
  const api = typeof window !== 'undefined' ? window.tongtianAPI : undefined;
  if (!api) return;
  const settings = await getSettings().catch(() => null);
  // 未设置过视为开启（与 AppSettingsData 默认值一致）
  const enabled = settings?.badgeUnread !== false;
  const count = enabled ? unreadTasks.size : 0;
  if (count === lastSent) return;
  try {
    await api.invoke(IPC_CHANNELS.BADGE_SET, count);
    lastSent = count;
  } catch {
    // 角标失败不影响任务本身；设置页的能力说明仍会如实展示
  }
}

/** 记一个未读任务（无 taskId 时不累计，避免把系统通知类事件算成任务） */
export function markTaskUnread(taskId: string | null | undefined): void {
  if (!taskId) return;
  unreadTasks.add(taskId);
  void pushBadge();
}

/** 清除全部未读（窗口回到前台 / 用户关闭开关） */
export function clearTaskbarUnread(): void {
  if (unreadTasks.size === 0) {
    if (lastSent !== 0) void pushBadge();
    return;
  }
  unreadTasks.clear();
  void pushBadge();
}

/** 设置开关变化后重新下发（关闭时清空，开启时把当前累计补上） */
export function refreshTaskbarBadge(): void {
  void pushBadge();
}

/**
 * 窗口回到前台时清零。
 * 同时监听 focus 与 visibilitychange：Windows 上最小化恢复只触发前者，
 * 切标签页/被遮挡时后者更可靠；两个都接住不会重复下发（同数量会被 lastSent 拦掉）。
 */
export function installBadgeFocusReset(): () => void {
  if (typeof window === 'undefined') return () => undefined;
  const onFocus = () => clearTaskbarUnread();
  const onVisible = () => {
    if (document.visibilityState === 'visible') clearTaskbarUnread();
  };
  window.addEventListener('focus', onFocus);
  document.addEventListener('visibilitychange', onVisible);
  return () => {
    window.removeEventListener('focus', onFocus);
    document.removeEventListener('visibilitychange', onVisible);
  };
}

/** 测试用：重置模块内累计状态 */
export function resetBadgeStateForTest(): void {
  unreadTasks.clear();
  lastSent = null;
  capabilityPromise = null;
}
