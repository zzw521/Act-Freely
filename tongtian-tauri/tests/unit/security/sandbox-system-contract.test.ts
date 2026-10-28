/**
 * 系统级沙箱设置契约（D-03 Windows AppContainer）
 *
 * 关闭开关时必须真的保存 `enabled:false`：曾经 tauri-api 按位置读参数，
 * 把 `{ enabled,network }` 对象当成 true，于是界面提示「已关闭」而设置仍是开启，
 * 只有重启才会暴露。本文件从行为与源码两侧锁住这条链路。
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn(async () => ({ ok: true })) }));
vi.mock('@tauri-apps/api/event', () => ({ listen: vi.fn(async () => () => {}) }));

import { invoke } from '@tauri-apps/api/core';
import { tauriAPI } from '../../../src/renderer/lib/tauri-api';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
}

const tauriApiSrc = () => read('../../../src/renderer/lib/tauri-api.ts');
const apiSrc = () => read('../../../src/renderer/lib/api.ts');
const settingsSrc = () => read('../../../src/renderer/pages/Settings/index.tsx');
const policySrc = () => read('../../../src-tauri/src/security/policy.rs');
const securityModSrc = () => read('../../../src-tauri/src/security/mod.rs');
const libSrc = () => read('../../../src-tauri/src/lib.rs');

describe('系统沙箱设置通道', () => {
  beforeEach(() => {
    vi.mocked(invoke).mockClear();
  });

  it('关闭时把 enabled:false 原样送到 Rust，而不是被对象真值转成 true', async () => {
    await tauriAPI.invoke('sandbox:systemSet', { enabled: false, network: false });
    expect(invoke).toHaveBeenCalledWith('sandbox_system_set', { enabled: false, network: false });
  });

  it('开启并允许出网时两个参数分别透传', async () => {
    await tauriAPI.invoke('sandbox:systemSet', { enabled: true, network: true });
    expect(invoke).toHaveBeenCalledWith('sandbox_system_set', { enabled: true, network: true });
  });

  it('渲染层按对象传参，适配层按对象读取（两侧一致才不会静默反写）', () => {
    expect(apiSrc()).toMatch(/SANDBOX_SYSTEM_SET,\s*\{\s*enabled,\s*network\s*\}/);
    const handler = tauriApiSrc().slice(tauriApiSrc().indexOf("channel === 'sandbox:systemSet'"));
    expect(handler).toMatch(/req\.enabled/);
    expect(handler).toMatch(/req\.network/);
  });
});

describe('Rust 侧保存与生效', () => {
  it('不支持平台拒绝启用，不落一份永不生效的开启状态', () => {
    expect(policySrc()).toMatch(/config\.enabled && !crate::sandbox::platform_supported\(\)/);
    expect(policySrc()).toMatch(/POLICY_UNSUPPORTED/);
  });

  it('启用前先真实创建 AppContainer profile，再落库并更新运行时开关', () => {
    const body = securityModSrc().slice(securityModSrc().indexOf('pub async fn sandbox_system_set'));
    expect(body).toMatch(/if enabled \{\s*crate::sandbox::prepare\(network\)\?;\s*\}/);
    expect(body).toMatch(/set_active\(Some\(stored\.clone\(\)\)\.filter\(\|c\| c\.enabled\)\)/);
    expect(body).toMatch(/"sandbox:system"/);
  });

  it('启动时从持久化设置恢复，读取失败按未启用处理', () => {
    expect(libSrc()).toMatch(/get_system_sandbox\(&services\.store\)/);
    expect(libSrc()).toMatch(/Some\(cfg\)\.filter\(\|c\| c\.enabled && sandbox::platform_supported\(\)\)/);
  });

  it('设置页把「请求」与「生效」分开显示，平台不支持时禁用开关', () => {
    const src = settingsSrc();
    expect(src).toMatch(/系统隔离（Windows AppContainer）/);
    expect(src).toMatch(/disabled=\{!sysStatus\?\.supported \|\| systemSaving\}/);
    expect(src).toMatch(/disabled=\{!sysEnabled \|\| systemSaving\}/);
    expect(src).toMatch(/已生效/);
    expect(src).toMatch(/未生效/);
  });
});
