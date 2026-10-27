/**
 * 桌面通知/提示音/任务栏角标消费方测试（任务 #40）
 *
 * 设置页这三行此前只写 settings_kv，没有任何一处代码去问系统、也没有一次 Notification
 * 调用 —— 「Agent 等待回复或完成工作时发送系统通知」是纯粹的假承诺。本文件锁住：
 * - 触发口径：哪些事件该提醒（deliver/等待审阅/失败），重复事件不轰炸
 * - 真实投递：授权到位才发；窗口在前台时不打扰；未授权/不支持都如实回报原因
 * - 提示音：只在完成时响，被自动播放策略挡住时不算成功
 * - 能力边界：Windows 用图标叠加、macOS 用 Dock 计数，其他平台明确不支持；
 *   开关绑定真实能力，关闭时下发 0，没有「拨了没反应」的假开关
 * - 页面：开通知必须走权限申请，拿不到授权不把开关拨成「已开」
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import type { ChatEvent } from '../../../src/shared/types';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const storeSrc = () => read('../../../src/renderer/store/workbenchStore.ts');
const settingsPage = () => read('../../../src/renderer/pages/Settings/index.tsx');
const notifySrc = () => read('../../../src/renderer/lib/notify-local.ts');

type NotifyMod = typeof import('../../../src/renderer/lib/notify-local');

/** 最小 WebView 环境：可控的 Notification 权限 + 可控状态的 AudioContext */
function installWebview(opts: {
  permission?: NotificationPermission;
  hasNotification?: boolean;
  audioState?: 'running' | 'suspended';
  requestResult?: NotificationPermission;
}): { posted: Array<{ title: string; body: string }> } {
  const posted: Array<{ title: string; body: string }> = [];
  const win = globalThis as unknown as Record<string, unknown>;
  const w: Record<string, unknown> = {};
  if (opts.hasNotification !== false) {
    const permission = opts.permission ?? 'default';
    function FakeNotification(this: unknown, title: string, init?: { body?: string }) {
      posted.push({ title, body: init?.body ?? '' });
    }
    Object.assign(FakeNotification, {
      permission,
      requestPermission: (cb?: (p: NotificationPermission) => void) => {
        const result = opts.requestResult ?? 'granted';
        (FakeNotification as unknown as { permission: NotificationPermission }).permission = result;
        cb?.(result);
        return Promise.resolve(result);
      },
    });
    w.Notification = FakeNotification;
  }
  const audio = { resumed: 0, state: opts.audioState ?? 'running' };
  w.AudioContext = function FakeAudioContext(this: Record<string, unknown>) {
    Object.assign(this, {
      currentTime: 0,
      resume: () => {
        audio.resumed += 1;
        // 真机上 resume 是异步的：本次同步判断仍是被挡住状态
        return Promise.resolve().then(() => {
          audio.state = 'running';
        });
      },
      destination: {},
      createOscillator: () => ({
        type: '',
        frequency: { setValueAtTime: () => {} },
        // connect 必须可链式：实现里是 osc.connect(gain).connect(destination)
        connect: () => ({ connect: () => {} }),
        start: () => {},
        stop: () => {},
      }),
      createGain: () => ({
        gain: { setValueAtTime: () => {}, exponentialRampToValueAtTime: () => {} },
        connect: () => ({ connect: () => {} }),
      }),
    });
    // state 要实时读：resume() 之后判断的必须是新状态，不能是构造时的快照
    Object.defineProperty(this, 'state', { get: () => audio.state });
    return this;
  };
  win.window = w;
  return { posted };
}

async function loadNotify(): Promise<NotifyMod> {
  vi.resetModules();
  return import('../../../src/renderer/lib/notify-local');
}

beforeEach(() => {
  installWebview({});
});

// ── 触发口径 ─────────────────────────────────────────────────────

describe('哪些事件该提醒', () => {
  it('完成/等待审阅/失败各提醒一次，过程事件不打扰', async () => {
    const m = await loadNotify();
    const ev = (o: Record<string, unknown>) => o as unknown as ChatEvent;
    expect(m.noticeKindFor(ev({ type: 'deliver' }))).toBe('delivered');
    expect(m.noticeKindFor(ev({ type: 'status_change', status: 'pending_review' }))).toBe('await_review');
    expect(m.noticeKindFor(ev({ type: 'status_change', status: 'failed' }))).toBe('failed');
    expect(m.noticeKindFor(ev({ type: 'error', code: 'X' }))).toBe('failed');
    for (const t of ['step_start', 'step_done', 'usage', 'plan', 'status_change']) {
      const e = ev(t === 'status_change' ? { type: t, status: 'executing' } : { type: t });
      expect(m.noticeKindFor(e), `${t} 不该发通知`).toBeNull();
    }
    // done 紧跟 deliver，再提醒就是响两遍
    expect(m.noticeKindFor(ev({ type: 'done' }))).toBeNull();
  });

  it('同一任务的同类事件去重（重放与重试不重复轰炸）', async () => {
    const m = await loadNotify();
    m.resetNoticeClaims();
    expect(m.claimNoticeKey('task-1:delivered')).toBe(true);
    expect(m.claimNoticeKey('task-1:delivered')).toBe(false);
    expect(m.claimNoticeKey('task-1:await_review')).toBe(true);
    expect(m.claimNoticeKey('task-2:delivered')).toBe(true);
  });

  it('store 的事件路由真的调用它（不是只写在 notify-local 里备用）', () => {
    const src = storeSrc();
    expect(src).toContain('noticeKindFor,');
    expect(src).toContain('const notice = noticeKindFor(event);');
    expect(src).toContain("if (notice) void notifyForEvent(notice, target.title || '任务', evTaskId ?? null);");
    expect(src).toContain('claimNoticeKey(');
    expect(src).toContain('desktopNotify: s?.desktopNotify !== false');
    expect(src).toContain('soundNotify: Boolean(s?.soundNotify)');
  });
});

// ── 真实投递 ─────────────────────────────────────────────────────

describe('系统通知投递', () => {
  it('授权到位才发，未授权如实给出可操作的原因', async () => {
    installWebview({ permission: 'denied' });
    const m = await loadNotify();
    const r = m.postTaskNotice('delivered', '写周报');
    expect(r.notified).toBe(false);
    expect(r.reason).toContain('系统未授权');
  });

  it('granted 时按事件类型给标题，正文是会话标题', async () => {
    const { posted } = installWebview({ permission: 'granted' });
    const m = await loadNotify();
    const r = m.postTaskNotice('await_review', '整理客户名单');
    expect(r.notified).toBe(true);
    expect(posted).toEqual([{ title: '等你确认执行计划', body: '整理客户名单' }]);
  });

  it('没有 Notification API 的环境不算成功（不假装已发送）', async () => {
    installWebview({ hasNotification: false });
    const m = await loadNotify();
    const r = m.postTaskNotice('failed', 'x');
    expect(r).toEqual({ notified: false, reason: '当前环境不支持系统通知' });
    expect(m.notificationPermission()).toBe('unsupported');
  });

  it('开启「桌面通知」时申请权限，拒绝后不再重复弹窗', async () => {
    installWebview({ permission: 'default', requestResult: 'denied' });
    const m = await loadNotify();
    const denied = await m.ensureNotificationPermission();
    expect(denied).toBe('denied');
    // 已是 denied：不会再弹第二次系统窗（规范上也不允许）
    expect(m.notificationPermission()).toBe('denied');
    expect(await m.ensureNotificationPermission()).toBe('denied');
  });

  it('窗口在前台时不打扰，且把原因说清楚', async () => {
    const { posted } = installWebview({ permission: 'granted' });
    const m = await loadNotify();
    const r = m.notifyTaskEvent('delivered', '任务A', {
      desktopNotify: true,
      soundNotify: false,
      focusCheck: () => true,
    });
    expect(r.notified).toBe(false);
    expect(r.reason).toContain('前台');
    expect(posted).toHaveLength(0);
  });

  it('两个开关各自独立：只开声音不发通知，只开通知不出声', async () => {
    const { posted } = installWebview({ permission: 'granted' });
    const m = await loadNotify();
    const soundOnly = m.notifyTaskEvent('delivered', '任务B', {
      desktopNotify: false,
      soundNotify: true,
      focusCheck: () => false,
    });
    expect(soundOnly.notified).toBe(false);
    expect(soundOnly.soundPlayed).toBe(true);
    expect(posted).toHaveLength(0);

    const notifyOnly = m.notifyTaskEvent('delivered', '任务C', {
      desktopNotify: true,
      soundNotify: false,
      focusCheck: () => false,
    });
    expect(notifyOnly.notified).toBe(true);
    expect(notifyOnly.soundPlayed).toBe(false);
    expect(posted).toHaveLength(1);
  });

  it('等待审阅与失败不响提示音（只有完成才响）', async () => {
    installWebview({ permission: 'granted' });
    const m = await loadNotify();
    for (const kind of ['await_review', 'failed'] as const) {
      const r = m.notifyTaskEvent(kind, '任务D', {
        desktopNotify: true,
        soundNotify: true,
        focusCheck: () => false,
      });
      expect(r.soundPlayed, `${kind} 不该出声`).toBe(false);
    }
  });
});

// ── 提示音 ───────────────────────────────────────────────────────

describe('完成提示音', () => {
  it('被自动播放策略挡住（suspended）时不算播出，并说明原因', async () => {
    installWebview({ audioState: 'suspended' });
    const m = await loadNotify();
    const r = m.playDoneChime();
    expect(r.soundPlayed).toBe(false);
    expect(r.reason).toContain('挡住');
  });

  it('没有 Web Audio 的环境如实报不支持', async () => {
    const win = globalThis as unknown as { window: Record<string, unknown> };
    win.window = {};
    const m = await loadNotify();
    const r = m.playDoneChime();
    expect(r.soundPlayed).toBe(false);
    expect(r.reason).toContain('不支持合成提示音');
  });

  it('提示音走合成短音，不依赖任何音频素材（避免随包资源缺失静默失声）', () => {
    const src = notifySrc();
    expect(src).toContain('createOscillator');
    expect(src).not.toMatch(/new Audio\(|\.mp3|\.wav/);
  });
});

// ── 能力边界与页面 ───────────────────────────────────────────────

describe('任务栏角标的平台边界', () => {
  it('Rust 按平台选择真实通道：Windows 图标叠加、macOS Dock 计数、其他平台明确拒绝', () => {
    const rust = read('../../../src-tauri/src/system/badge.rs');
    expect(rust).toContain('set_overlay_icon');
    expect(rust).toContain('set_badge_count');
    expect(rust).toContain('BADGE_UNSUPPORTED: 当前平台');
    expect(rust).toContain('count.min(99)');
  });

  it('设置页开关绑定真实能力，浏览器通道降级为只读说明', () => {
    const src = settingsPage();
    expect(src).toContain('badgeCapability()');
    expect(src).toContain('checked={badgeUnread && Boolean(badge?.supported)}');
    expect(src).toContain('disabled={!badge?.supported}');
    expect(src).toContain('badge ? badge.detail :');
    expect(src).not.toContain("{badge.supported ? '待接入' : '当前平台不支持'}");
  });

  it('未聚焦才累计、回前台清零，开关关闭时下发 0', () => {
    const src = read('../../../src/renderer/lib/badge-local.ts');
    expect(src).toContain('const count = enabled ? unreadTasks.size : 0;');
    expect(src).toContain('installBadgeFocusReset');
    expect(src).toContain('window.addEventListener(\'focus\', onFocus);');
    expect(storeSrc()).toContain('if (!focused) markTaskUnread(taskId);');
  });

  it('开通知走权限申请，未授权不把开关拨成已开', () => {
    const comp = settingsPage().split('const NotificationSettings')[1].split('const SystemPanel')[0];
    expect(comp).toContain('await ensureNotificationPermission()');
    expect(comp).toContain("if (p !== 'granted')");
    expect(comp).toContain('updateAppSettings({ desktopNotify: true });');
    expect(comp).toContain('checked={notify && granted}');
    expect(comp).toContain('系统未授权通知，请在系统设置中允许任我行发送通知后再开');
  });

  it('旧的三行假开关（只落库不生效）已从页面消失', () => {
    const src = settingsPage();
    expect(src).not.toContain("updateAppSettings({ desktopNotify: e.target.checked })");
    expect(src).not.toContain('Agent 等待回复或完成工作时发送系统通知');
    expect(src).not.toContain('Agent 完成工作时播放提示音');
  });
});
