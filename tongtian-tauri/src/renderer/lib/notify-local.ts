/**
 * 桌面通知与提示音（设置页系统分区的消费方）
 *
 * 「Agent 等待回复或完成工作时发送系统通知」这句承诺此前没有实现：设置项只落库，
 * 没有任何事件点会去问系统、也没有任何一处调用 Notification。本模块是那个消费方。
 *
 * 通道口径：
 * - 桌面端与浏览器都用 WebView 的 Notification API（无需新增 Rust 依赖，也不假装
 *   有原生通知能力）；权限被系统拒绝时如实返回原因，界面据此提示，而不是静默不发。
 * - 提示音用 Web Audio 合成，不引音频素材；被自动播放策略挡住时同样回报真相。
 * - 窗口正前台时不打扰（用户已经在看这条消息了），这是系统通知的通行约定。
 */

import type { ChatEvent } from '../../shared/types';
import { detectOs } from './permissions-local';

/**
 * 哪些任务事件值得给用户一次系统级反馈（设置页「桌面通知/声音通知」的触发口径）。
 * deliver 之后还会再来 done，只在 deliver 提醒一次；等待审阅与失败各自一次。
 */
export function noticeKindFor(event: ChatEvent): NoticeKind | null {
  if (event.type === 'deliver') return 'delivered';
  if (event.type === 'error') return 'failed';
  if (event.type === 'status_change') {
    const status = (event as { status?: string }).status;
    if (status === 'pending_review') return 'await_review';
    if (status === 'failed') return 'failed';
  }
  return null;
}

/** 同一任务的同类提醒只发一次（重试、断线重放都不该重复轰炸用户） */
const claimed = new Set<string>();

export function claimNoticeKey(key: string): boolean {
  if (claimed.has(key)) return false;
  claimed.add(key);
  return true;
}

/** 测试与登出用：清空已提醒记录 */
export function resetNoticeClaims(): void {
  claimed.clear();
}

export type NoticeKind = 'delivered' | 'await_review' | 'failed';

/**
 * 任务栏未读角标的平台能力。
 * Tauri 的 setBadgeCount 在 Windows 上明确不支持（系统只提供图标覆盖，且要另配图片），
 * 所以这里如实报能力，让设置页显示成只读说明，而不是一个拨了没用的开关。
 */
export function badgeSupport(os: string = detectOs()): { supported: boolean; os: string; why: string } {
  if (os === 'macos') {
    return { supported: true, os, why: 'macOS 可在程序图标上显示未读数量' };
  }
  if (os === 'windows') {
    return {
      supported: false,
      os,
      why: 'Windows 任务栏不支持未读计数（系统只提供图标覆盖），此项当前无法真实生效',
    };
  }
  return { supported: false, os, why: '当前平台的任务栏不支持未读计数' };
}

export interface NoticeResult {
  /** 是否真的向系统投递了通知 */
  notified: boolean;
  /** 是否真的播出了提示音 */
  soundPlayed: boolean;
  /** 没做到的原因（界面据此如实提示，不写「已发送」） */
  reason?: string;
}

const TITLE: Record<NoticeKind, string> = {
  delivered: '任务已完成',
  await_review: '等你确认执行计划',
  failed: '任务执行失败',
};

/** 系统通知能力与权限（unsupported = 当前 WebView 不提供 Notification） */
export function notificationPermission(): NotificationPermission | 'unsupported' {
  if (typeof window === 'undefined' || typeof window.Notification === 'undefined') return 'unsupported';
  return window.Notification.permission;
}

/**
 * 打开「桌面通知」时的权限申请：只有系统弹窗授权后才算开成功。
 * 被拒绝时不抛错，返回最终权限，由调用方显示原因（拒绝是用户的正当选择）。
 */
export async function ensureNotificationPermission(): Promise<NotificationPermission | 'unsupported'> {
  const current = notificationPermission();
  if (current !== 'default') return current;
  try {
    // 新版规范返回 Promise，旧 WebView 是回调式；两种都接住，避免悬空 Promise
    const granted = await new Promise<boolean>((resolve) => {
      const r = window.Notification.requestPermission((p) => resolve(p === 'granted'));
      if (r && typeof (r as Promise<NotificationPermission>).then === 'function') {
        (r as Promise<NotificationPermission>).then((p) => resolve(p === 'granted'));
      }
    });
    return granted ? 'granted' : notificationPermission();
  } catch {
    // 非安全上下文（http:// 页面上的 WebView）会直接抛：如实报 unsupported 而非 granted
    return 'unsupported';
  }
}

/** 发一条系统通知（不碰权限申请，调用方保证已授权） */
export function postTaskNotice(kind: NoticeKind, body: string): { notified: boolean; reason?: string } {
  if (typeof window === 'undefined' || typeof window.Notification === 'undefined') {
    return { notified: false, reason: '当前环境不支持系统通知' };
  }
  if (window.Notification.permission !== 'granted') {
    return { notified: false, reason: '系统未授权通知，请在系统设置中允许后重试' };
  }
  try {
    new window.Notification(TITLE[kind], { body, tag: `tontian-${kind}` });
    return { notified: true };
  } catch (e) {
    return { notified: false, reason: `系统通知发送失败：${e instanceof Error ? e.message : String(e)}` };
  }
}

let audioCtx: AudioContext | null = null;

/** 完成提示音：Web Audio 合成短音，不依赖音频素材 */
export function playDoneChime(): { soundPlayed: boolean; reason?: string } {
  if (typeof window === 'undefined') return { soundPlayed: false, reason: '当前环境无音频' };
  const Ctor = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) return { soundPlayed: false, reason: '当前环境不支持合成提示音' };
  try {
    audioCtx ??= new Ctor();
    if (audioCtx.state === 'suspended') void audioCtx.resume();
    const t = audioCtx.currentTime;
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = 'sine';
    osc.frequency.setValueAtTime(880, t);
    osc.frequency.setValueAtTime(1320, t + 0.12);
    // 短促且渐出，避免在前台弹窗之外再制造一次刺耳响铃
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.18, t + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.3);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start(t);
    osc.stop(t + 0.32);
    // state 仍为 suspended 说明被自动播放策略挡住：如实说没播响，而不是当作成功
    return audioCtx.state === 'running'
      ? { soundPlayed: true }
      : { soundPlayed: false, reason: '浏览器暂时挡住了声音，需先在页面里操作一次' };
  } catch (e) {
    return { soundPlayed: false, reason: `提示音播放失败：${e instanceof Error ? e.message : String(e)}` };
  }
}

/**
 * 任务事件 → 用户可感知的反馈（系统通知 + 可选提示音）。
 * 两个开关各自独立生效：只开提示音时不发通知，只开通知时不出声。
 */
export function notifyTaskEvent(
  kind: NoticeKind,
  body: string,
  prefs: { desktopNotify: boolean; soundNotify: boolean; focusCheck?: () => boolean },
): NoticeResult {
  const { desktopNotify, soundNotify } = prefs;
  if (!desktopNotify && !soundNotify) return { notified: false, soundPlayed: false };
  const focused = prefs.focusCheck ? prefs.focusCheck() : false;
  let notified = false;
  let reason: string | undefined;
  if (desktopNotify && !focused) {
    const r = postTaskNotice(kind, body);
    notified = r.notified;
    reason = r.reason;
  } else if (desktopNotify && focused) {
    reason = '窗口正在前台，未打扰';
  }
  // 提示音只在「任务完成」时响（等待审阅/失败用通知就够了）
  const chime = soundNotify && kind === 'delivered' && !focused ? playDoneChime() : null;
  return {
    notified,
    soundPlayed: chime?.soundPlayed ?? false,
    reason: chime && !chime.soundPlayed ? chime.reason ?? reason : reason,
  };
}
