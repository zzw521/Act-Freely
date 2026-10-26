/**
 * 远程桌面契约测试（#78）
 *
 * 锁定四件事：
 * - 5 个通道在 contracts、Tauri 适配、Rust invoke_handler 三处一致；
 * - Rust 会话边界真实存在：10 分钟上限、≥700ms 帧间隔、开始必须过 gate、只看不控；
 * - REMOTE_* 错误码都有中文文案；
 * - 浏览器/Mock 通道如实返回「没有本机桌面执行器」，页面/路由可达且写明边界。
 */

import { beforeAll, beforeEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ErrorCode, IPC_CHANNELS } from '../../../src/shared/contracts';
import { errorMessageOf } from '../../../src/shared/error-messages';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const contracts = () => read('../../../src/shared/contracts.ts');
const rustRemote = () => read('../../../src-tauri/src/remote/mod.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const rustAudit = () => read('../../../src-tauri/src/security/audit.rs');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const alignmentLocal = () => read('../../../src/renderer/lib/alignment-local.ts');
const remotePage = () => read('../../../src/renderer/pages/RemoteDesktopPanel/index.tsx');
const appRoutes = () => read('../../../src/renderer/App.tsx');
const sidebar = () => read('../../../src/renderer/components/Layout/Sidebar.tsx');
const settingsPage = () => read('../../../src/renderer/pages/Settings/index.tsx');

describe('远程桌面通道与 command 接线', () => {
  it('通道名固定', () => {
    expect(IPC_CHANNELS.REMOTE_DESKTOP_CAPABILITIES).toBe('remote:capabilities');
    expect(IPC_CHANNELS.REMOTE_DESKTOP_STATUS).toBe('remote:status');
    expect(IPC_CHANNELS.REMOTE_DESKTOP_START).toBe('remote:start');
    expect(IPC_CHANNELS.REMOTE_DESKTOP_FRAME).toBe('remote:frame');
    expect(IPC_CHANNELS.REMOTE_DESKTOP_STOP).toBe('remote:stop');
  });

  it('Tauri 适配映射到真实 command，Rust 已注册状态与命令', () => {
    const api = tauriApi();
    for (const [channel, command] of [
      ['remote:capabilities', 'remote_desktop_capabilities'],
      ['remote:status', 'remote_desktop_status'],
      ['remote:start', 'remote_desktop_start'],
      ['remote:frame', 'remote_desktop_frame'],
      ['remote:stop', 'remote_desktop_stop'],
    ]) {
      expect(api).toContain(`channel === '${channel}'`);
      expect(api).toContain(`invoke('${command}'`);
      expect(rustLib()).toContain(`remote::${command}`);
    }
    expect(rustLib()).toContain('remote::new_state()');
  });

  it('审计动作已登记，且只记会话生命周期', () => {
    expect(rustAudit()).toContain('ACTION_REMOTE_SESSION');
    expect(rustRemote()).toContain('ACTION_REMOTE_SESSION');
    expect(rustRemote()).toContain('开始本机桌面实时画面');
    expect(rustRemote()).toContain('结束本机桌面实时画面');
    expect(rustRemote()).toContain('远程桌面会话到期自动结束');
  });
});

describe('会话边界由 Rust 强制', () => {
  it('10 分钟上限与 ≥700ms 帧间隔是常量而非文案', () => {
    const rust = rustRemote();
    expect(rust).toContain('pub const SESSION_MAX_MS: u64 = 10 * 60 * 1000;');
    expect(rust).toContain('pub const FRAME_MIN_INTERVAL_MS: u64 = 700;');
    expect(rust).toContain('if now >= session.expires_at_ms');
    expect(rust).toContain('FrameDecision::Throttled');
    expect(rust).toContain('FrameDecision::Expired');
  });

  it('开始必须过电脑操控 gate，禁用档位直接拒绝', () => {
    const rust = rustRemote();
    expect(rust).toContain('computer::read_policy(&p0.store)');
    expect(rust).toContain('if policy == "disabled"');
    expect(rust).toContain('computer::gate(&policy_for_gate, &description)');
    expect(rust).toContain('REMOTE_DISABLED');
  });

  it('会话结束后在途帧不会复活会话，截屏期间结束则该帧丢弃', () => {
    const rust = rustRemote();
    expect(rust).toContain('(s.id == session_id, is_active(s, now2))');
    expect(rust).toContain('let stopped = {');
    expect(rust).toContain('return Err(REMOTE_NO_SESSION.into());');
  });

  it('只看不控、不跨主机：能力视图明说，不提供输入通道', () => {
    const rust = rustRemote();
    expect(rust).toContain('"supported": false');
    expect(rust).toContain('跨主机转发需要中继服务与主机配对，尚未提供');
    expect(rust).not.toContain('remote_desktop_input');
    expect(rust).not.toContain('mouse_event');
    expect(rust).not.toContain('keybd_event');
  });
});

describe('REMOTE_* 错误码都有中文文案', () => {
  it('接口与 Rust 抛出的码全部可映射', () => {
    const codes = new Set<string>();
    for (const m of contracts().matchAll(/\b(REMOTE_[A-Z][A-Z_]*)\s*=\s*'/g)) {
      codes.add(m[1]);
    }
    for (const m of rustRemote().matchAll(/"((?:REMOTE)_[A-Z][A-Z_]*)"/g)) {
      codes.add(m[1]);
    }
    expect(codes.size).toBeGreaterThanOrEqual(4);
    for (const code of codes) {
      const text = errorMessageOf(code);
      expect(text).not.toContain(code);
      expect(text.length).toBeGreaterThan(6);
    }
    expect(ErrorCode.REMOTE_UNSUPPORTED).toBe('REMOTE_UNSUPPORTED');
    expect(ErrorCode.REMOTE_SESSION_EXPIRED).toBe('REMOTE_SESSION_EXPIRED');
  });
});

describe('浏览器/Mock 通道的远程桌面能力视图', () => {
  (globalThis as Record<string, unknown>).window =
    (globalThis as Record<string, unknown>).window ?? {};

  type AlignmentApi = typeof import('../../../src/renderer/lib/alignment-api');
  let a: AlignmentApi;

  beforeAll(async () => {
    await import('../../../src/renderer/lib/mock-api');
    a = await import('../../../src/renderer/lib/alignment-api');
  });

  beforeEach(async () => {
    const local = await import('../../../src/renderer/lib/alignment-local');
    local.resetLocalAlignment();
  });

  it('能力视图如实为不可用，跨主机明确不支持', async () => {
    const caps = await a.getRemoteDesktopCapabilities();
    expect(caps.supported).toBe(false);
    expect(caps.localView).toBe(false);
    expect(caps.platform).toBe('browser');
    expect(caps.frameIntervalMs).toBe(700);
    expect(caps.sessionMaxMs).toBe(600000);
    expect(caps.reason).toContain('没有系统截屏能力');
    expect(caps.crossHost.supported).toBe(false);
    expect(caps.crossHost.reason).toContain('尚未提供');
  });

  it('开启与取帧如实报错，结束是幂等的未在运行', async () => {
    await expect(a.startRemoteDesktop()).rejects.toThrow(/REMOTE_UNSUPPORTED/);
    await expect(a.fetchRemoteDesktopFrame()).rejects.toThrow(/REMOTE_UNSUPPORTED/);
    const stop = await a.stopRemoteDesktop();
    expect(stop.active).toBe(false);
    expect(stop.frames).toBe(0);
    expect(stop.session.active).toBe(false);
    const status = await a.getRemoteDesktopStatus();
    expect(status.active).toBe(false);
    expect(status.expiresAtMs).toBe(0);
  });

  it('本地实现与 Rust 同口径常量', () => {
    const local = alignmentLocal();
    expect(local).toContain('REMOTE_SESSION_MAX_MS = 10 * 60 * 1000');
    expect(local).toContain('REMOTE_FRAME_INTERVAL_MS = 700');
  });
});

describe('页面与导航可达', () => {
  it('远程桌面作为设置执行能力分区可达，旧路由重定向', () => {
    expect(appRoutes()).toContain('<Route path="/remote-desktop" element={<Navigate to="/settings?s=remote-desktop" replace />} />');
    expect(sidebar()).not.toContain("path: '/remote-desktop'");
    const settings = settingsPage();
    expect(settings).toContain("{ id: 'remote-desktop', label: '远程桌面'");
    expect(settings).toContain('return <RemoteDesktopPanel />;');
  });

  it('页面消费真实能力视图并写明边界', () => {
    const src = remotePage();
    for (const fn of [
      'getRemoteDesktopCapabilities',
      'getRemoteDesktopStatus',
      'startRemoteDesktop',
      'fetchRemoteDesktopFrame',
      'stopRemoteDesktop',
      'getComputerUseCapabilities',
    ]) {
      expect(src).toContain(fn);
    }
    expect(src).toContain('只看不控');
    expect(src).toContain('不跨主机');
    expect(src).toContain('会话上限 10 分钟');
    expect(src).toContain('画面只在当前窗口内存中显示');
    // 离开页面即结束会话，画面不在不可见窗口里继续截取
    expect(src).toContain('void stopRemoteDesktop()');
  });
});
