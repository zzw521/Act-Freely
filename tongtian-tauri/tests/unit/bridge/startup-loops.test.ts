/**
 * 启动期后台循环的运行时归属（安装版启动即 panic 的回归锁）
 *
 * 现象：打包后的应用双击就没了。抓 stderr 是
 *   thread 'main' panicked at src/bridge/event_store.rs:277:
 *   there is no reactor running, must be called from the context of a Tokio 1.x runtime
 * 根因：Tauri 的 setup 回调跑在主线程，那里没有 tokio reactor；事件存储的 TTL 清理循环
 * 用了裸 `tokio::spawn`，而其余几条启动循环（租约扫描 / 定时任务扫描 / 刷盘）都走
 * `tauri::async_runtime::spawn`。cargo test 里每条单测自带 runtime，所以全绿也照样炸 —— 这类
 * 代码只能靠「启动路径的归属」来锁，不能靠单测。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const eventStore = () => read('../../../src-tauri/src/bridge/event_store.rs');
const lib = () => read('../../../src-tauri/src/lib.rs');

describe('setup 里派生的循环必须挂在 tauri async_runtime 上', () => {
  it('TTL 清理循环不再用裸 tokio::spawn', () => {
    const fn = eventStore().split('pub fn spawn_ttl_cleanup_loop')[1].split('#[cfg(test)]')[0];
    expect(fn).toContain('tauri::async_runtime::spawn(async move');
    expect(fn).not.toMatch(/(?<!tauri::async_runtime::)\btokio::spawn\(/);
  });

  it('启动循环的入口都经 async_runtime（新增循环别绕开）', () => {
    const setup = lib().split('.setup(')[1].split('.run(')[0];
    const directSpawns = setup.match(/\btokio::spawn\(/g) ?? [];
    expect(directSpawns, 'setup 闭包里不允许出现裸 tokio::spawn').toEqual([]);
    expect(setup.match(/tauri::async_runtime::spawn\(/g)?.length).toBeGreaterThanOrEqual(2);
    // 事件存储两条循环：刷盘 + TTL 清理
    expect(setup).toContain('event_store_start(&events);');
    expect(setup).toContain('spawn_ttl_cleanup_loop(Arc::clone(&events));');
  });

  it('刷盘循环在 async_runtime 上下文里派生（start_flusher 内部的 tokio::spawn 才成立）', () => {
    const start = lib().split('fn event_store_start')[1].split('\n}\n')[0];
    expect(start).toContain('tauri::async_runtime::spawn');
    expect(start).toContain('store.start_flusher().await.ok();');
  });
});
