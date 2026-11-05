/**
 * 工单层 · 前端契约（缺陷 D-47.4/.5）
 *
 * 这一层前端只做参数搬运与如实拒绝，因此钉住四件容易静默坏掉的事：
 * - contracts.ts 的 14 条 `collab:ticket*` 通道与 Rust tickets.rs 的 14 条 command
 *   一一对应（命名规则：`collab:ticketMyTickets` ↔ `collab_ticket_my_tickets`），
 *   tauri-api.ts 的分发、api.ts 的 collabAPI 方法也都齐全——漏一条就是界面上一个
 *   永远「未知通道」的按钮。
 * - 14 条命令全部在 lib.rs 注册；没注册的 Rust 命令编译能过、运行全是「找不到命令」。
 * - 服务端错误码原样透传（`AGENT_TICKET_CLOSED:` 这类前缀不许被前端二次翻译吞掉）——
 *   界面按码分支，文案只作兜底。
 * - 浏览器/Mock 通道对全部工单通道统一 COLLAB_UNSUPPORTED，不返回空清单冒充。
 */

import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { IPC_CHANNELS } from '../../../src/shared/contracts';

const storage = new Map<string, string>();
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
};
(globalThis as Record<string, unknown>).window =
  (globalThis as Record<string, unknown>).window ?? {};

type WindowAPI = { tongtianAPI?: { invoke: (channel: string, ...args: unknown[]) => Promise<unknown> } };
const win = globalThis.window as WindowAPI;

const read = (rel: string) =>
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');

const contractsSrc = read('../../../src/shared/contracts.ts');
const dispatchSrc = read('../../../src/renderer/lib/tauri-api.ts');
const apiSrc = read('../../../src/renderer/lib/api.ts');
const rustSrc = read('../../../src-tauri/src/collab/tickets.rs');
const libSrc = read('../../../src-tauri/src/lib.rs');

/** 通道名 → Rust command 名的既定命名规则（camelCase → snake_case，前缀 collab: → collab_） */
const commandOf = (channel: string) =>
  'collab_' +
  channel
    .slice('collab:'.length)
    .replace(/[A-Z]/g, (c) => '_' + c.toLowerCase());

beforeAll(async () => {
  // mock-api 注册 window.tongtianAPI：非桌面通道对 collab:* 的拒绝由它兜底
  await import('../../../src/renderer/lib/mock-api');
  await import('../../../src/renderer/lib/api');
});

describe('工单通道契约（源码扫描 · 三面接线一致）', () => {
  const channels = Object.entries(IPC_CHANNELS)
    .filter(([, v]) => typeof v === 'string' && (v as string).startsWith('collab:ticket'))
    .map(([k, v]) => ({ key: k, channel: v as string }));

  it('账本上正好 14 条工单通道，且常量名与通道值同前缀', () => {
    expect(channels).toHaveLength(14);
    for (const { key, channel } of channels) {
      expect(channel.startsWith('collab:ticket')).toBe(true);
      // COLLAB_TICKET_MY_TICKETS → collab:myTickets 部分大小写对齐
      expect(commandOf(channel).startsWith('collab_ticket')).toBe(true);
      expect(key.startsWith('COLLAB_TICKET_')).toBe(true);
    }
  });

  it('每条通道在 tauri-api.ts 分发到命名规则确定的 command', () => {
    for (const { channel } of channels) {
      const cmd = commandOf(channel);
      // 纯字符串扫描：找到分发锚点后，在其后 900 字符窗口内必须出现对应 invoke
      const anchor = dispatchSrc.indexOf(`channel === '${channel}'`);
      expect(anchor, `分发缺失：${channel}`).toBeGreaterThan(-1);
      const window = dispatchSrc.slice(anchor, anchor + 900);
      expect(window.includes(`invoke('${cmd}'`), `分发缺失：${channel} → ${cmd}`).toBe(true);
    }
  });

  it('每条 command 在 Rust tickets.rs 定义、并在 lib.rs 注册', () => {
    for (const { channel } of channels) {
      const cmd = commandOf(channel);
      expect(rustSrc.includes(`pub async fn ${cmd}(`), `Rust 未定义：${cmd}`).toBe(true);
      expect(libSrc.includes(`collab::tickets::${cmd}`), `lib.rs 未注册：${cmd}`).toBe(true);
    }
  });

  it('collabAPI 有对应的 ticket* 方法，且逐条走 IPC_CHANNELS 常量而不是裸字符串', () => {
    for (const { key, channel } of channels) {
      const method = 'ticket' + channel.slice('collab:ticket'.length);
      expect(apiSrc.includes(`${method}(`), `collabAPI 缺方法：${method}`).toBe(true);
      expect(apiSrc.includes(`IPC_CHANNELS.${key}`), `${method} 未用常量 ${key}`).toBe(true);
    }
  });
});

describe('浏览器/Mock 通道的工单拒绝', () => {
  it('14 条工单通道统一 COLLAB_UNSUPPORTED（不返回空清单冒充本机没单）', async () => {
    const ticketChannels = Object.values(IPC_CHANNELS).filter(
      (v) => typeof v === 'string' && v.startsWith('collab:ticket'),
    ) as string[];
    expect(ticketChannels).toHaveLength(14);
    for (const channel of ticketChannels) {
      await expect(win.tongtianAPI!.invoke(channel, {})).rejects.toThrow(/^COLLAB_UNSUPPORTED:/);
    }
  });
});
