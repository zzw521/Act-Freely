/**
 * 终端 AI+ / ACP 契约测试（#77）
 *
 * 锁定五件事：
 * - 11 个通道在 contracts、Tauri 适配、Rust invoke_handler 三处一致；
 * - 引擎只拿桌面壳注入的终端令牌，ACP 配置随 runtime 下发且坏配置按未启用处理；
 * - ACP 引擎侧：仅在 ready 时注册 delegate_coding_task，fs 限制目录、terminal/* 拒绝、默认 deny；
 * - TERMINAL_* / ACP_* 错误码都有中文文案；
 * - 浏览器/Mock 通道如实返回「没有本机 shell」，页面与设置消费真实能力视图。
 */

import { beforeAll, beforeEach, describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ErrorCode, IPC_CHANNELS } from '../../../src/shared/contracts';
import { errorMessageOf } from '../../../src/shared/error-messages';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const contracts = () => read('../../../src/shared/contracts.ts');
const rustTerminal = () => read('../../../src-tauri/src/terminal.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const rustRuntime = () => read('../../../src-tauri/src/bridge/runtime.rs');
const rustAgent = () => read('../../../src-tauri/src/bridge/agent.rs');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const alignmentLocal = () => read('../../../src/renderer/lib/alignment-local.ts');
const terminalPage = () => read('../../../src/renderer/pages/Terminal/index.tsx');
const settingsPage = () => read('../../../src/renderer/pages/Settings/index.tsx');
const engineServer = () => read('../../../engine/server.py');
const engineAcp = () => read('../../../engine/acp_client.py');
const appRoutes = () => read('../../../src/renderer/App.tsx');
const sidebar = () => read('../../../src/renderer/components/Layout/Sidebar.tsx');

describe('终端 AI+ / ACP 通道与 command 接线', () => {
  it('通道名固定', () => {
    expect(IPC_CHANNELS.TERMINAL_GET_POLICY).toBe('terminal:getPolicy');
    expect(IPC_CHANNELS.TERMINAL_SET_POLICY).toBe('terminal:setPolicy');
    expect(IPC_CHANNELS.TERMINAL_CAPABILITIES).toBe('terminal:capabilities');
    expect(IPC_CHANNELS.TERMINAL_OPEN).toBe('terminal:open');
    expect(IPC_CHANNELS.TERMINAL_INPUT).toBe('terminal:input');
    expect(IPC_CHANNELS.TERMINAL_READ).toBe('terminal:read');
    expect(IPC_CHANNELS.TERMINAL_CLOSE).toBe('terminal:close');
    expect(IPC_CHANNELS.TERMINAL_AI).toBe('terminal:ai');
    expect(IPC_CHANNELS.ACP_GET_CONFIG).toBe('acp:getConfig');
    expect(IPC_CHANNELS.ACP_SET_CONFIG).toBe('acp:setConfig');
    expect(IPC_CHANNELS.ACP_STATUS).toBe('acp:status');
  });

  it('Tauri 适配映射到真实 command，Rust 已注册', () => {
    const api = tauriApi();
    for (const [channel, command] of [
      ['terminal:getPolicy', 'terminal_get_policy'],
      ['terminal:setPolicy', 'terminal_set_policy'],
      ['terminal:capabilities', 'terminal_capabilities'],
      ['terminal:open', 'terminal_open'],
      ['terminal:input', 'terminal_input'],
      ['terminal:read', 'terminal_read'],
      ['terminal:close', 'terminal_close'],
      ['terminal:ai', 'terminal_ai'],
      ['acp:getConfig', 'acp_get_config'],
      ['acp:setConfig', 'acp_set_config'],
      ['acp:status', 'acp_status'],
    ]) {
      expect(api).toContain(`channel === '${channel}'`);
      expect(api).toContain(`invoke('${command}'`);
      expect(rustLib()).toContain(`terminal::${command}`);
    }
  });

  it('runtime 下发 acp 段，终端令牌只经进程环境注入', () => {
    expect(rustRuntime()).toContain('"acp": crate::terminal::acp_block(store)');
    expect(rustAgent()).toContain('.env("TT_TERMINAL_TOKEN"');
    expect(rustAgent()).toContain('crate::terminal::engine_token()');
    const engine = engineServer();
    expect(engine).toContain('x-tt-terminal-token');
    expect(engine).toContain('TT_TERMINAL_TOKEN');
    expect(rustTerminal()).toContain('.header("X-TT-Terminal-Token", engine_token())');
    // 渲染层没有读取令牌的通道：能力视图只回开关与原因
    expect(rustTerminal()).not.toContain('"terminalToken"');
  });

  it('终端执行页收进设置执行能力分区，旧路由重定向', () => {
    expect(appRoutes()).toContain('<Route path="/terminal" element={<Navigate to="/settings?s=terminal-ai" replace />} />');
    expect(sidebar()).not.toContain("path: '/terminal'");
    const settings = settingsPage();
    expect(settings).toContain("{ id: 'terminal-ai', label: '终端 AI+'");
    expect(settings).toContain('return <TerminalPanel embedded />;');
    // 设置里另有 ACP 配置分区，两者不能混成同一个 id
    expect(settings).toContain("{ id: 'terminal', label: 'ACP 与终端'");
  });
});

describe('ACP 引擎侧边界', () => {
  it('只在 ready 时把 delegate_coding_task 装进父 Agent', () => {
    const server = engineServer();
    expect(server).toContain('acp = acpm.AcpRuntime.from_runtime(');
    expect(server).toContain('if acp is not None and acp.ready:');
    expect(server).toContain('acp.install(toolkit');
    const client = engineAcp();
    expect(client).toContain('def delegate_coding_task');
    // ready 之前不注册：安装入口只在 AcpRuntime 上
    expect(client).toContain('def install(');
  });

  it('fs 限制在工作目录，terminal/* 与未知方法拒绝', () => {
    const engine = engineAcp();
    expect(engine).toContain('def _resolve_path');
    expect(engine).toContain('path.relative_to(self._root())');
    expect(engine).toContain('ACP_PATH_DENIED');
    expect(engine).toContain('method.startswith("terminal/")');
    expect(engine).toContain('ACP_TERMINAL_UNSUPPORTED');
    expect(engine).toContain('ACP_METHOD_NOT_FOUND');
  });

  it('权限默认拒绝，子进程环境剔除令牌与密钥', () => {
    const engine = engineAcp();
    expect(engine).toContain('self.permission = "deny"');
    expect(engine).toContain('me.permission = str(block.get("permission") or "deny")');
    expect(engine).toContain('("TOKEN" in k.upper() or "SECRET" in k.upper())');
    // 超时/取消杀整棵进程树，不留孤儿
    expect(engine).toContain('taskkill", "/T", "/F"');
    expect(engine).toContain('os.killpg(os.getpgid(proc.pid), 9)');
  });

  it('Rust 配置坏值时按未启用处理，不把空命令交给引擎', () => {
    const rust = rustTerminal();
    expect(rust).toContain('pub fn acp_block');
    expect(rust).toContain('ACP_COMMAND_MISSING');
    expect(rust).toContain('"enabled": false');
    expect(rust).toContain('"permission": cfg.permission');
  });
});

describe('终端 / ACP 错误码都有中文文案', () => {
  it('接口与 Rust 抛出的 TERMINAL_* / ACP_* 全部可映射', () => {
    const codes = new Set<string>();
    for (const m of contracts().matchAll(/\b((?:TERMINAL|ACP)_[A-Z][A-Z_]*)\s*=\s*'/g)) {
      codes.add(m[1]);
    }
    for (const m of rustTerminal().matchAll(/"((?:TERMINAL|ACP)_[A-Z][A-Z_]*)"/g)) {
      codes.add(m[1]);
    }
    expect(codes.size).toBeGreaterThanOrEqual(28);
    for (const code of codes) {
      const text = errorMessageOf(code);
      expect(text).not.toContain(code);
      expect(text.length).toBeGreaterThan(4);
    }
    expect(ErrorCode.TERMINAL_UNSUPPORTED).toBe('TERMINAL_UNSUPPORTED');
    expect(ErrorCode.ACP_PATH_DENIED).toBe('ACP_PATH_DENIED');
    expect(ErrorCode.TERMINAL_FAILED).toBe('TERMINAL_FAILED');
  });
});

describe('浏览器/Mock 通道的终端能力视图', () => {
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

  it('能力视图如实为不可用，不假装有本机 shell', async () => {
    const caps = await a.getTerminalCapabilities();
    expect(caps.pipe).toBe(false);
    expect(caps.enabled).toBe(false);
    expect(caps.persisted).toBe(false);
    expect(caps.maxSessions).toBe(0);
    expect(caps.engineRunning).toBe(false);
    expect(caps.platform).toBe('browser');
    expect(caps.reason).toContain('没有本机 shell 进程');
    expect(caps.acp.enabled).toBe(false);
    expect(caps.acp.error).toContain('只在桌面版');
  });

  it('终端操作与 ACP 写入如实报错，不静默成功', async () => {
    await expect(a.setTerminalEnabled(true)).rejects.toThrow(/TERMINAL_UNSUPPORTED/);
    await expect(a.openTerminal('C:/tmp')).rejects.toThrow(/TERMINAL_UNSUPPORTED/);
    await expect(a.writeTerminal('s', 'ls')).rejects.toThrow(/TERMINAL_UNSUPPORTED/);
    await expect(a.readTerminal('s', 0)).rejects.toThrow(/TERMINAL_UNSUPPORTED/);
    await expect(a.closeTerminal('s')).rejects.toThrow(/TERMINAL_UNSUPPORTED/);
    await expect(a.suggestTerminalCommand('s', '怎么查端口')).rejects.toThrow(
      /TERMINAL_UNSUPPORTED/,
    );
    await expect(
      a.setAcpConfig({ enabled: true, command: 'codex acp', permission: 'allow', timeoutSeconds: 600 }),
    ).rejects.toThrow(/TERMINAL_UNSUPPORTED/);
    expect(alignmentLocal()).toContain('openLocalTerminal');
    expect(alignmentLocal()).toContain('setLocalAcpConfig');
  });

  it('ACP 服务端状态如实说明没有本机 Python 与引擎', async () => {
    const status = await a.getAcpStatus();
    expect(status.enabled).toBe(false);
    expect(status.command).toBeNull();
    expect(status.engineRunning).toBe(false);
    expect(status.python).toBeNull();
    expect(status.reason).toContain('没有本机 Python');
  });
});

describe('页面与设置消费真实能力视图', () => {
  it('终端页读取能力、开会话、读输出与 AI 建议', () => {
    const src = terminalPage();
    for (const fn of [
      'getTerminalCapabilities',
      'openTerminal',
      'writeTerminal',
      'readTerminal',
      'closeTerminal',
      'suggestTerminalCommand',
    ]) {
      expect(src).toContain(fn);
    }
    expect(src).toContain('data-testid="terminal-status"');
    expect(src).toContain('data-testid="terminal-open"');
    expect(src).toContain('data-testid="terminal-close"');
    expect(src).toContain('data-testid="terminal-output"');
    expect(src).toContain("send(cmd.command, 'ai')");
  });

  it('终端页保留诚实边界：无 TTY、AI 不执行、子 Agent 不继承', () => {
    const src = terminalPage();
    expect(src).toContain('管道模式没有 TTY');
    expect(src).toContain('AI 只读取最近输出并给建议，永不自行写入');
    expect(src).toContain('子 Agent 不继承终端能力');
  });

  it('设置页提供显式开关、ACP 配置与服务端状态', () => {
    const src = settingsPage();
    expect(src).toContain('data-testid="terminal-enable-switch"');
    for (const fn of ['getTerminalCapabilities', 'setTerminalEnabled', 'getAcpConfig', 'setAcpConfig', 'getAcpStatus']) {
      expect(src).toContain(fn);
    }
    expect(src).toContain('外部 Agent 启动命令');
    expect(src).toContain('委派审计：');
    expect(src).toContain('fs 读写限制在委派指定目录');
    expect(src).toContain('开启终端或修改 ACP 配置都会写入安全审计');
  });
});
