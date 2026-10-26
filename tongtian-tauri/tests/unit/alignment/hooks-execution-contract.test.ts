/**
 * Hook 执行面契约测试（能力台账 P0-3 / 缺口 G6）
 *
 * 这条链在整改前只到「落库 + 试运行」为止：12 类事件、exit 2 阻塞都只是配置和文案，
 * 真实任务里一次都没触发过。本文件把「配置 → 放行 → 引擎触发 → 本机执行」的四段接线钉住，
 * 任何一段被改回半成品都会红：
 * - 通道：contracts.ts 声明的每个 hooks:* 都在桌面桥、本地通道、Rust command 三处有落点
 * - 执行权：shell 只在 Rust `run_command`，引擎只发事件、绝不自己拉起进程
 * - 触发点：Rust FIRED_EVENTS 的每个事件在引擎源码里有真实 fire 调用，UNFIRED 的不许假触发
 * - 放行：未放行不执行但如实回报；删除配置即作废放行（两侧同口径）
 * - 文案：页面不得再宣称「事件触发则脚本必执行」
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { IPC_CHANNELS } from '../../../src/shared/contracts';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const localApi = () => read('../../../src/renderer/lib/alignment-local.ts');
const alignApi = () => read('../../../src/renderer/lib/alignment-api.ts');
const page = () => read('../../../src/renderer/pages/HooksManager/index.tsx');
const rustMod = () => read('../../../src-tauri/src/align/mod.rs');
const rustHooks = () => read('../../../src-tauri/src/align/hooks.rs');
const rustGate = () => read('../../../src-tauri/src/bridge/hook_gate.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const engineRuntime = () => read('../../../engine/hooks_runtime.py');
const engineServer = () => read('../../../engine/server.py');
const engineSubagent = () => read('../../../engine/subagent_runtime.py');

/** 提取 Rust `pub const NAME: &[&str] = &[...]` 成员 */
function rustStrArray(src: string, name: string): string[] {
  const re = new RegExp(`pub const ${name}:[^=]+=\\s*&?\\[([\\s\\S]*?)\\];`);
  const body = src.match(re)?.[1];
  if (!body) throw new Error(`Rust 源码未找到常量数组 ${name}`);
  return [...body.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
}

/** 契约里声明的全部 hooks:* 通道（新增通道自动进入下面的对齐断言） */
function declaredHookChannels(): string[] {
  return Object.values(IPC_CHANNELS).filter((c) => c.startsWith('hooks:'));
}

describe('通道三处对齐：contracts → 桌面桥 → 本地通道 → Rust command', () => {
  it('每个 hooks:* 都有桌面桥分支、本地通道分支和已注册的 Rust command', () => {
    const src = rustLib();
    expect(declaredHookChannels().length).toBeGreaterThanOrEqual(6);
    for (const ch of declaredHookChannels()) {
      const cmd = `hooks_${ch.split(':')[1]}`;
      expect(src, `Rust 未注册 ${cmd}`).toContain(`align::${cmd},`);
      const bridge = tauriApi();
      expect(bridge, `桌面桥未映射 ${ch}`).toContain(`channel === '${ch}'`);
      expect(bridge, `桌面桥未调用 ${cmd}`).toContain(`invoke('${cmd}'`);
      expect(localApi(), `本地通道未实现 ${ch}`).toContain(`case '${ch}'`);
    }
  });

  it('放行/撤销/概览三条通道在渲染层各有消费方', () => {
    const src = alignApi();
    expect(src).toContain('IPC_CHANNELS.HOOKS_GRANT');
    expect(src).toContain('IPC_CHANNELS.HOOKS_REVOKE');
    expect(src).toContain('IPC_CHANNELS.HOOKS_STATUS');
    const pageSrc = page();
    expect(pageSrc).toContain('grantHook(');
    expect(pageSrc).toContain('revokeHook(');
    expect(pageSrc).toContain('getHooksStatus(');
  });
});

describe('执行权归属：shell 只在 Rust 网关，引擎只发事件', () => {
  it('引擎侧不拉起任何进程，只 POST 到回环网关', () => {
    const src = engineRuntime();
    for (const forbidden of ['subprocess', 'os.system', 'Popen', 'shell=True']) {
      expect(src, `引擎不该出现 ${forbidden}`).not.toContain(forbidden);
    }
    expect(src).toContain('x-tt-hook-token');
  });

  it('放行的命令与「试运行」共用同一个 run_command（不允许各写一套执行逻辑）', () => {
    const src = rustHooks();
    expect(src.split('pub fn run_command')[1].split('\n}\n')[0]).toContain('Command::new');
    expect(rustMod()).toContain('hooks::run_command');
    expect(rustGate()).toContain('hooks::fire');
  });

  it('网关只绑回环、校验 token、只认 POST /hook，且在 Tauri 异步运行时里起循环', () => {
    const src = rustGate();
    expect(src).toContain('TcpListener::bind("127.0.0.1:0")');
    expect(src).toContain('uuid::Uuid::new_v4()');
    expect(src).toContain('x-tt-hook-token');
    expect(src).toContain('HOOK_GATE_UNAUTHORIZED');
    expect(src).toContain('method != "POST" || path != "/hook"');
    expect(src).toContain('MAX_BODY_BYTES: usize = 64 * 1024');
    expect(src).toContain('Connection: close');
    // setup 闭包里没有 tokio reactor，裸 tokio::spawn 会让打包版启动即 panic
    expect(src).toContain('tauri::async_runtime::spawn');
    expect(src).not.toContain('tokio::spawn(');
  });
});

/** 引擎里是否存在对某事件的真实 fire 调用（允许换行书写） */
function firesEvent(src: string, ev: string): boolean {
  return new RegExp(`\\.fire\\(\\s*(?:\\n\\s*)?"${ev}"`).test(src);
}

describe('触发点事实：Rust 声明的 fired 事件在引擎里真有 fire 调用', () => {
  it('非工具事件由执行链路触发，工具事件由 Toolkit 中间件触发', () => {
    const server = engineServer();
    const runtime = engineRuntime();
    const fired = rustStrArray(rustHooks(), 'FIRED_EVENTS');
    expect(fired).toHaveLength(10);
    for (const ev of fired) {
      if (ev === 'PreToolUse') {
        expect(firesEvent(runtime, ev), `中间件未触发 ${ev}`).toBe(true);
      } else if (ev === 'PostToolUse' || ev === 'PostToolUseFailure') {
        // 这两个按工具返回分流，选择逻辑与随后的上报必须都在
        expect(runtime, `中间件未区分 ${ev}`).toContain(`"${ev}"`);
        expect(runtime).toContain('runner.fire(event');
      } else if (ev === 'SubagentStart' || ev === 'SubagentStop') {
        expect(firesEvent(engineSubagent(), ev), `子 Agent 运行时未触发 ${ev}`).toBe(true);
      } else {
        expect(firesEvent(server, ev), `执行链路未触发 ${ev}`).toBe(true);
      }
    }
  });

  it('没有触发点的 2 类事件不许在引擎里假触发', () => {
    const calls = `${engineServer()}\n${engineRuntime()}\n${engineSubagent()}`;
    const unfired = rustStrArray(rustHooks(), 'UNFIRED_EVENTS');
    expect(unfired).toEqual(['Notification', 'PermissionRequest']);
    for (const ev of unfired) {
      expect(firesEvent(calls, ev), `${ev} 没有真实触发点，不许出现 fire 调用`).toBe(false);
    }
  });

  it('PreToolUse 阻断语义贯通：exit 2 → 工具不执行 → 模型看到阻断说明', () => {
    const runtime = engineRuntime();
    expect(runtime).toContain('blocked');
    expect(runtime).toContain('<system-info>本次工具调用被用户配置的 PreToolUse Hook 阻断');
    expect(rustHooks()).toContain('BLOCKABLE_EVENTS');
  });
});

describe('放行台账：配置 ≠ 执行，删除配置即撤权', () => {
  it('未放行的命令不执行，但如实回报原因与提示', () => {
    const fire = rustHooks().split('pub fn fire')[1].split('\n}\n')[0];
    expect(fire).toContain('is_granted');
    expect(fire).toContain('run_command');
    expect(fire).toContain('尚未放行');
    expect(rustHooks()).toContain('"未放行"');
  });

  it('保存配置时作废已删除命令的放行（Rust 与本地台账同口径）', () => {
    expect(rustHooks()).toContain('pub fn prune_grants');
    expect(rustMod()).toContain('hooks::prune_grants');
    const local = localApi();
    expect(local).toContain('const keep = configuredHookCommands(hooksLedger)');
    expect(local).toContain('revoked');
  });

  it('撤销放行只删授权，配置原样保留（两侧都不许顺手删配置）', () => {
    const revoke = rustMod().split('pub async fn hooks_revoke')[1].split('\n}\n')[0];
    expect(revoke).toContain('hooks::revoke');
    expect(revoke).not.toContain('hook_configs');
    const local = localApi().split('export function revokeLocalHooks')[1].split('\n}\n')[0];
    expect(local).toContain('hookGrantLedger.filter');
    expect(local).not.toContain('KEY_HOOKS');
  });
});

describe('页面诚实口径', () => {
  it('不再宣称「事件触发则脚本必执行」，改为说明需放行', () => {
    const src = page();
    expect(src).not.toContain('脚本必执行');
    expect(src).toContain('待放行');
    expect(src).toContain('无触发点');
    expect(src).toContain('放行');
  });

  it('试运行是唯一会立即拉起进程的地方，页面要写明；未放行则真实任务不执行', () => {
    const src = page();
    expect(src).toContain('试运行会立即在本机执行该命令');
    expect(src).toContain('真实任务不会执行');
    expect(src).toContain('未放行：配置已保存，但真实任务不会执行该命令');
  });
});
