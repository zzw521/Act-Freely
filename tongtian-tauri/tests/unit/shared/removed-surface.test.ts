/**
 * P2 死代码清理回归（2026-10-20）
 *
 * 台账 P2-12/13 的处置：没有 UI 消费方的查询命令直接删除，未挂路由的页面目录删除。
 * 这些删除不能靠「下次盘点再发现」——本文件从源码侧锁定：
 * - Rust 注册表不再出现 agent_stop_workspace / settings_set / task_state / quota_status /
 *   lease_info / lease_takeover；
 * - 前端契约不再暴露 TASK_STATE / QUOTA_STATUS / LEASE_INFO / LEASE_TAKEOVER；
 * - 五个未挂路由的页面目录保持删除状态，App 不再懒加载 SkillMarket；
 * - 真实消费的 chat_cancel / event_replay / settings_set_many 仍在注册表里。
 */

import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const rustLib = () => read('../../../src-tauri/src/lib.rs');
const contracts = () => read('../../../src/shared/contracts.ts');
const app = () => read('../../../src/renderer/App.tsx');

const REMOVED_COMMANDS = [
  'agent_stop_workspace',
  'settings_set',
  'task_state',
  'quota_status',
  'lease_info',
  'lease_takeover',
] as const;

const REMOVED_CHANNELS = ['TASK_STATE', 'QUOTA_STATUS', 'LEASE_INFO', 'LEASE_TAKEOVER'] as const;

const REMOVED_PAGES = [
  'AuditView',
  'AwarenessPage',
  'SkillMarket',
  'TemplateMarket',
  'ConnectorCenter',
] as const;

describe('P2 死代码清理', () => {
  it('Rust 不再注册无人调用的命令，真实消费的三条链路仍在', () => {
    const src = rustLib();
    for (const name of REMOVED_COMMANDS) {
      expect(src, `${name} 不应再有命令定义`).not.toContain(`async fn ${name}(`);
      expect(src, `${name} 不应再出现在注册表`).not.toMatch(new RegExp(`\\b${name},`));
    }
    for (const name of ['chat_cancel', 'event_replay', 'settings_set_many']) {
      expect(src, `${name} 是界面真实消费的入口`).toContain(`async fn ${name}(`);
    }
  });

  it('前端契约不再暴露无 UI 消费的查询通道', () => {
    const src = contracts();
    for (const key of REMOVED_CHANNELS) {
      expect(src, `${key} 应已删除`).not.toContain(`${key}:`);
    }
    expect(src).toContain("CHAT_CANCEL: 'chat:cancel'");
    expect(src).toContain("EVENT_REPLAY: 'event:replay'");
  });

  it('未挂路由的页面目录保持删除，App 不再懒加载 SkillMarket', () => {
    for (const page of REMOVED_PAGES) {
      const dir = fileURLToPath(new URL(`../../../src/renderer/pages/${page}`, import.meta.url));
      expect(existsSync(dir), `${page} 目录应已删除`).toBe(false);
    }
    expect(app()).not.toContain("import('./pages/SkillMarket')");
    // 旧入口仍以重定向保留，避免用户收藏的地址 404
    expect(app()).toContain('<Navigate to="/extensions?tab=skills" replace />');
    expect(app()).toContain('<Navigate to="/settings?s=awareness" replace />');
  });

  it('connector 死链路与 agent_status 不再注册或暴露', () => {
    const src = rustLib();
    for (const name of ['connector_list', 'connector_authorize', 'connector_revoke', 'agent_status']) {
      expect(src, `${name} 不应再有命令定义`).not.toMatch(new RegExp(`async fn ${name}\\(`));
      expect(src, `${name} 不应再出现在注册表`).not.toMatch(new RegExp(`\\b${name},`));
    }
    const contractSrc = contracts();
    for (const key of ['CONNECTOR_LIST', 'CONNECTOR_AUTHORIZE', 'CONNECTOR_REVOKE', 'SYSTEM_HEALTH']) {
      expect(contractSrc, `${key} 应已删除`).not.toMatch(new RegExp(`\\b${key}:`));
    }
    expect(contractSrc).toContain("SYSTEM_INFO: 'system:info'");
  });

  it('Electron 旧壳目录已删除，QwenPaw 参考树与全局类型契约保留', () => {
    for (const dir of ['main', 'preload']) {
      const path = fileURLToPath(new URL(`../../../src/${dir}`, import.meta.url));
      expect(existsSync(path), `src/${dir} 应已删除`).toBe(false);
    }
    expect(existsSync(fileURLToPath(new URL('../../../src/shared/tongtian-api.d.ts', import.meta.url)))).toBe(true);
    expect(existsSync(fileURLToPath(new URL('../../../src/agent', import.meta.url)))).toBe(true);
  });
});
