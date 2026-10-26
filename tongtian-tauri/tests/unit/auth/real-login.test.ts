/**
 * 登录真实接入综合服务端
 *
 * 目标：桌面端默认不再走本地过渡 auth-server / Mock 直登，而是
 * 连接 tongtian-server（默认 127.0.0.1:8000）完成真实账号验证：
 * - 地址默认值与优先级（环境变量 → settings_kv → 综合服务端）
 * - 登录前探活，服务不可用时快速失败（不把浏览器打开在死地址上空等）
 * - 授权码交换 / 登出吊销走回环直连，避免被系统代理截走
 * - 设置页展示账号服务连通状态，浏览器 Mock 如实报告不可达
 */

import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const service = () => read('../../../src-tauri/src/auth/service.rs');
const authMod = () => read('../../../src-tauri/src/auth/mod.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const collab = () => read('../../../src-tauri/src/collab/mod.rs');
const contracts = () => read('../../../src/shared/contracts.ts');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const api = () => read('../../../src/renderer/lib/api.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const settingsPage = () => read('../../../src/renderer/pages/Settings/index.tsx');
const collaboration = () => read('../../../src/renderer/pages/Collaboration/index.tsx');
const desktopInit = () => read('../../../init.sh');

describe('Rust：默认连接综合服务端并做真实登录', () => {
  it('默认地址是 tongtian-server 的 8000 端口，而不是过渡 auth-server 的 18080', () => {
    const src = service();
    expect(src).toContain('pub const DEFAULT_SERVER_URL: &str = "http://127.0.0.1:8000";');
    expect(src).not.toContain('unwrap_or_else(|_| "http://127.0.0.1:18080"');
    expect(src).toContain('pub fn resolve_server_url(env_value: Option<&str>, stored: Option<&str>) -> String');
    expect(src).toContain('pub const SERVER_SETTING_KEY: &str = "auth.serverUrl";');
  });

  it('Mock 只在显式开启时生效，缺省走真实验证', () => {
    const src = service();
    expect(src).toContain('pub fn mock_from_env(raw: Option<&str>) -> bool');
    expect(src).toContain('Some("true" | "1" | "on" | "yes")');
    expect(src).not.toContain('Err(_) => true');
  });

  it('远程明文 HTTP 被拒绝，回环地址才允许 http', () => {
    const src = service();
    expect(src).toContain('AUTH_SERVER_INSECURE');
    expect(src).toContain('AUTH_SERVER_URL_INVALID');
    expect(src).toContain('pub fn client_for_base(base: &str) -> reqwest::Client');
  });

  it('授权码交换与登出吊销都走回环直连，而不是全局代理 client', () => {
    const exchange = service().split('pub async fn exchange_code')[1].split('pub async fn revoke_remote')[0];
    expect(exchange).toContain('client_for_base(base_url)');
    expect(exchange).not.toContain('crate::proxy::client()');
    const revoke = service().split('pub async fn revoke_remote')[1].split('/// 登录成功持久化')[0];
    expect(revoke).toContain('client_for_base(base_url)');
  });

  it('登录前先探活：综合服务端 /health 不可用则快速失败', () => {
    const src = authMod();
    const realLogin = src.split('async fn real_login')[1].split('/// 退出登录')[0];
    expect(realLogin).toContain('service::ensure_server_ready(&base).await?;');
    // 探活必须在绑定 loopback 端口与打开浏览器之前
    expect(realLogin.indexOf('ensure_server_ready')).toBeLessThan(
      realLogin.indexOf('TcpListener::bind'),
    );
    expect(service()).toContain('pub async fn ensure_server_ready');
    expect(service()).toContain('AUTH_SERVER_MISMATCH');
  });

  it('登录态查询带服务地址，并新增可审计的探活 command', () => {
    expect(authMod()).toContain('"serverUrl": service::auth_server_url(&p0.store)');
    expect(authMod()).toContain('pub async fn auth_server_probe');
    expect(rustLib()).toContain('auth::auth_server_probe');
  });

  it('协作复用同一地址解析（含 settings_kv），不再有硬编码无参调用', () => {
    const src = collab();
    expect(src).toContain('auth_service::auth_server_url(store)');
    expect(src).not.toContain('auth_service::auth_server_url()');
  });
});

describe('前端：账号服务状态与真实登录文案', () => {
  it('IPC 契约、Tauri 映射与 API 封装齐全', () => {
    expect(contracts()).toContain("AUTH_SERVER_PROBE: 'auth:serverProbe'");
    expect(contracts()).toContain('export interface AuthServerProbe');
    expect(contracts()).toContain('serverUrl?: string');
    expect(tauriApi()).toContain("if (channel === 'auth:serverProbe') return invoke('auth_server_probe');");
    expect(api()).toContain('serverProbe(): Promise<AuthServerProbe>');
  });

  it('设置页显示账号服务地址、连通状态与真实/Mock 登录说明', () => {
    const src = settingsPage();
    expect(src).toContain('await authAPI.serverProbe()');
    expect(src).toContain('label="账号服务"');
    expect(src).toContain('重新检测');
    expect(src).toContain('在浏览器完成综合服务端账号验证');
    expect(src).toContain('Mock 登录（TT_AUTH_MOCK=true）');
    expect(src).toContain('请在桌面版验证真实登录');
  });

  it('协作页不再让用户去关一个默认已关闭的开关', () => {
    expect(collaboration()).toContain('默认连接综合服务端 http://127.0.0.1:8000');
    expect(collaboration()).toContain('请确认已启动 tongtian-server');
  });

  it('浏览器 Mock 探测如实报告不可达，不编造连接成功', () => {
    const src = mockApi();
    const branch = src.split("if (channel === 'auth:serverProbe')")[1].split('return null;')[0];
    expect(branch).toContain('reachable: false');
    expect(branch).toContain('浏览器 Mock 模式不连接账号服务');
  });
});

describe('启动脚本：默认真实登录并指向 8000', () => {
  it('TT_AUTH_MOCK 默认 false，地址默认指向综合服务端', () => {
    const src = desktopInit();
    expect(src).toContain('TT_AUTH_MOCK="${TT_AUTH_MOCK:-false}"');
    expect(src).toContain('TT_AUTH_SERVER_URL="${TT_AUTH_SERVER_URL:-http://127.0.0.1:8000}"');
    expect(src).toContain('tongtian-server/init.sh');
    expect(src).not.toContain('auth-server/server.py');
  });
});

// ── 行为：浏览器 Mock 的探活通道 ─────────────────────────────

const storage = new Map<string, string>();
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
};
(globalThis as Record<string, unknown>).window = (globalThis as Record<string, unknown>).window ?? {};

type WindowAPI = { tongtianAPI?: { invoke: (channel: string, ...args: unknown[]) => Promise<unknown> } };
const win = globalThis.window as WindowAPI;

beforeAll(async () => {
  // 注入 Mock 垫片（window.tongtianAPI 缺失时自动挂载 mockAPI）
  await import('../../../src/renderer/lib/mock-api');
});

describe('浏览器 Mock 的账号服务探测', () => {
  it('返回综合服务端默认地址，但如实标记未连接', async () => {
    const probe = (await win.tongtianAPI!.invoke('auth:serverProbe')) as {
      baseUrl: string;
      reachable: boolean;
      service: string | null;
      version: string | null;
      error: string | null;
    };
    expect(probe.baseUrl).toBe('http://127.0.0.1:8000');
    expect(probe.reachable).toBe(false);
    expect(probe.service).toBeNull();
    expect(probe.version).toBeNull();
    expect(probe.error).toContain('浏览器 Mock 模式不连接账号服务');
  });
});
