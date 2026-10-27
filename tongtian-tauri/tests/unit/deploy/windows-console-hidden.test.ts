/**
 * 安装版不弹控制台窗口
 *
 * 桌面端是 GUI 子系统，从它拉起控制台程序（python / tasklist / powershell / reg /
 * taskkill）必须带 CREATE_NO_WINDOW：引擎常驻时窗口会一直挂着，存活轮询每 10 秒闪一次。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

describe('Windows 子进程窗口抑制', () => {
  it('提供统一的 hide_console 助手并定义 CREATE_NO_WINDOW', () => {
    const src = read('../../../src-tauri/src/win_console.rs');
    expect(src).toContain('CREATE_NO_WINDOW: u32 = 0x0800_0000');
    expect(src).toContain('pub fn hide_console(');
    expect(src).toContain('pub fn hide_console_async(');
  });

  it.each([
    ['src-tauri/src/bridge/agent.rs', 2],
    ['src-tauri/src/bridge/python_env.rs', 2],
    ['src-tauri/src/mcp/mod.rs', 1],
    ['src-tauri/src/task/keep_awake.rs', 1],
    ['src-tauri/src/system/autostart.rs', 1],
    ['src-tauri/src/browser/mod.rs', 1],
  ])('%s 的控制台子进程都抑制窗口', (rel, least) => {
    const src = read(`../../../${rel}`);
    const calls = (src.match(/crate::win_console::hide_console(_async)?\(/g) ?? []).length;
    expect(calls).toBeGreaterThanOrEqual(least);
  });
});
