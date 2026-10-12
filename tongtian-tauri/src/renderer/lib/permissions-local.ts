/**
 * 系统权限清单的本地权威实现（Mock / 浏览器直连引擎通道）
 *
 * 与 Rust `src-tauri/src/security/permissions.rs` 同表同口径：同一份能力清单、
 * 同一套「当前平台有没有这个开关」的判定。区别只在两处真实边界：
 *
 * 1. 状态只问系统能回答的项（通知走 `Notification.permission`，麦克风走
 *    Permissions API）。问不到的一律 `unknown`，页面据此显示「需在系统设置中管理」，
 *    绝不再写死「已授权」。
 * 2. 浏览器无法拉起系统设置面板，所以 `openSupported: false`，
 *    「去授权」如实抛 `PERMISSION_UNSUPPORTED`。
 */

import type {
  PermissionState,
  SystemCapability,
  SystemPermissionStatus,
} from '../../shared/contracts';

interface CapabilitySpec {
  key: string;
  label: string;
  purpose: string;
  probe: 'notification' | 'microphone' | null;
  macosPane: string | null;
  windowsPane: string | null;
  /** 当前平台没有这个面板时的如实解释 */
  noPaneNote: string;
}

/** 展示顺序与文案口径由这张表唯一决定（与 Rust CAPABILITIES 一一对应） */
export const CAPABILITIES: readonly CapabilitySpec[] = [
  {
    key: 'disk',
    label: '完全磁盘访问权限',
    purpose: '读写项目文件等操作',
    probe: null,
    macosPane: 'x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles',
    windowsPane: null,
    noPaneNote: 'Windows 无此开关，文件读写跟随当前用户权限',
  },
  {
    key: 'screen',
    label: '录屏与系统录音',
    purpose: '屏幕内容识别、反馈截图',
    probe: null,
    macosPane: 'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture',
    windowsPane: null,
    noPaneNote: 'Windows 无系统级录屏开关，由应用在采集时申请',
  },
  {
    key: 'accessibility',
    label: '辅助功能',
    purpose: '监听全局快捷键，快捷唤起',
    probe: null,
    macosPane: 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility',
    windowsPane: null,
    noPaneNote: 'Windows 全局快捷键无需授权',
  },
  {
    key: 'microphone',
    label: '麦克风',
    purpose: '语音输入等功能',
    probe: 'microphone',
    macosPane: 'x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone',
    windowsPane: 'ms-settings:privacy-microphone',
    noPaneNote: '当前平台请在系统设置中管理麦克风权限',
  },
  {
    key: 'automation',
    label: '自动化',
    purpose: '与系统应用协作（提醒/日历/备忘录）',
    probe: null,
    macosPane: 'x-apple.systempreferences:com.apple.preference.security?Privacy_Automation',
    windowsPane: null,
    noPaneNote: 'Windows 无自动化授权中心，按用户权限直接调用',
  },
  {
    key: 'notifications',
    label: '通知',
    purpose: '任务完成、消息提醒',
    probe: 'notification',
    macosPane: 'x-apple.systempreferences:com.apple.preference.security?Privacy_Notifications',
    windowsPane: 'ms-settings:notifications',
    noPaneNote: '当前平台请在系统设置中管理通知权限',
  },
  {
    key: 'location',
    label: '位置服务',
    purpose: '处理与地理位置相关的任务',
    probe: null,
    macosPane: 'x-apple.systempreferences:com.apple.preference.security?Privacy_LocationServices',
    windowsPane: 'ms-settings:privacy-location',
    noPaneNote: '当前平台请在系统设置中管理位置权限',
  },
];

/** 与 Rust `std::env::consts::OS` 对齐的平台标签（macos / windows / 其余按 linux） */
export function detectOs(ua?: string): string {
  const s = (ua ?? (typeof navigator !== 'undefined' ? navigator.userAgent : '')).toLowerCase();
  if (/mac os x|macintosh|darwin/.test(s)) return 'macos';
  if (/windows|win32|wow32/.test(s)) return 'windows';
  return 'linux';
}

/** 当前平台真实可达的设置面板（与 Rust pane_for 同口径） */
export function paneFor(os: string, spec: CapabilitySpec): string | null {
  if (os === 'macos') return spec.macosPane;
  if (os === 'windows') return spec.windowsPane;
  return null;
}

/**
 * 系统真实回答 → 展示文案。
 * 只有这三种状态存在；`unknown`（问不到）返回 null，由页面显示如实解释，
 * 绝不再补一个「已授权 / 无需授权」糊过去。
 */
export function permissionLabel(state: PermissionState): string | null {
  if (state === 'granted') return '已授权';
  if (state === 'denied') return '已拒绝';
  if (state === 'prompt') return '待授权';
  return null;
}

/** Rust view_of 的 TS 等价：有面板时不塞解释文案，避免页面「未授权 + 一段辩解」 */
export function viewOf(os: string, spec: CapabilitySpec): SystemCapability {
  const pane = paneFor(os, spec);
  return {
    key: spec.key,
    label: spec.label,
    purpose: spec.purpose,
    probe: spec.probe,
    pane,
    note: pane ? null : spec.noPaneNote,
  };
}

/** 浏览器 Notification 授权态 → 统一状态；问不到返回 null */
function probeNotification(): PermissionState | null {
  if (typeof Notification === 'undefined') return null;
  switch (Notification.permission) {
    case 'granted':
      return 'granted';
    case 'denied':
      return 'denied';
    case 'default':
      return 'prompt';
    default:
      return null;
  }
}

/** Permissions API 查询麦克风；浏览器不认这项或权限被拒时返回 null */
async function probeMicrophone(): Promise<PermissionState | null> {
  try {
    if (typeof navigator === 'undefined' || !navigator.permissions?.query) return null;
    const r = await navigator.permissions.query({ name: 'microphone' as PermissionName });
    if (r.state === 'granted' || r.state === 'denied' || r.state === 'prompt') return r.state;
    return null;
  } catch {
    return null;
  }
}

/** 只对系统能真实回答的项发起探测，其余不进 states */
export async function probePermissionStates(
  items: SystemCapability[],
): Promise<Record<string, PermissionState>> {
  const states: Record<string, PermissionState> = {};
  await Promise.all(
    items.filter((item) => item.probe).map(async (item) => {
      const s =
        item.probe === 'notification'
          ? probeNotification()
          : await probeMicrophone();
      if (s) states[item.key] = s;
    }),
  );
  return states;
}

/**
 * 本地通道的权限视图。
 * `openSupported: false` —— 浏览器不能拉起系统设置，页面据此把「去授权」
 * 收成一句解释而不是假按钮。
 */
export async function localSystemPermissionStatus(): Promise<SystemPermissionStatus> {
  const os = detectOs();
  const items = CAPABILITIES.map((spec) => viewOf(os, spec));
  return { os, items, states: await probePermissionStates(items), openSupported: false };
}

/** 浏览器/Mock/引擎直连共用的拒绝文案（桌面版才有系统设置入口） */
const UNSUPPORTED = 'PERMISSION_UNSUPPORTED: 浏览器通道无系统设置入口';

/** 「去授权」在非桌面通道的统一拒绝，页面按 errorTextOf 展示文案 */
export function openSystemPermissionUnsupported(): never {
  throw new Error(UNSUPPORTED);
}

export interface LocalPermissionResult {
  handled: boolean;
  value?: unknown;
}

/** 系统权限通道本地实现：未命中返回 handled=false（写法与 skill-local 一致） */
export function handleLocalPermissionChannel(
  channel: string,
  args: unknown[],
): LocalPermissionResult {
  switch (channel) {
    case 'system:permission:status':
      return { handled: true, value: localSystemPermissionStatus() };
    case 'system:permission:open':
      // 既不能改系统权限，也不能打开系统设置
      void args;
      return openSystemPermissionUnsupported();
    default:
      return { handled: false };
  }
}
