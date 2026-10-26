/**
 * 电脑操控执行器契约测试（#64）
 *
 * 锁定四件事：
 * - 通道名、Tauri 适配、Rust invoke_handler 三处一致，页面不会对着空气调用；
 * - 浏览器/Mock 通道如实返回「没有执行器」，截屏自检不返回占位图；
 * - Rust 执行器/网关抛出的 COMPUTER_USE_* 码都有中文文案，不裸奔；
 * - 页面消费真实能力视图与自检，并保留能力边界说明。
 */

import { beforeAll, beforeEach, describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ErrorCode, IPC_CHANNELS } from '../../../src/shared/contracts';
import { errorMessageOf } from '../../../src/shared/error-messages';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const rustComputer = () => read('../../../src-tauri/src/computer/mod.rs');
const rustGate = () => read('../../../src-tauri/src/bridge/computer_gate.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const rustRuntime = () => read('../../../src-tauri/src/bridge/runtime.rs');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const alignmentLocal = () => read('../../../src/renderer/lib/alignment-local.ts');
const panel = () => read('../../../src/renderer/pages/ComputerUsePanel/index.tsx');
const engineServer = () => read('../../../engine/server.py');
const engineComputerUse = () => read('../../../engine/computer_use.py');

describe('电脑操控通道与 command 接线', () => {
  it('能力视图与截屏自检的通道名固定', () => {
    expect(IPC_CHANNELS.COMPUTER_USE_CAPABILITIES).toBe('computerUse:capabilities');
    expect(IPC_CHANNELS.COMPUTER_USE_CAPTURE).toBe('computerUse:capture');
  });

  it('Tauri 适配把通道映射到真实 command，Rust 已注册', () => {
    const api = tauriApi();
    expect(api).toContain("channel === 'computerUse:capabilities'");
    expect(api).toContain("invoke('computer_use_capabilities')");
    expect(api).toContain("channel === 'computerUse:capture'");
    expect(api).toContain("invoke('computer_use_capture')");
    expect(rustLib()).toContain('computer::computer_use_capabilities');
    expect(rustLib()).toContain('computer::computer_use_capture');
  });

  it('runtime 随任务下发 computerUse 段，且 token 只在该段出现', () => {
    expect(rustRuntime()).toContain('computer_use_block(store)');
    const block = rustRuntime();
    expect(block).toContain('gateToken');
    expect(block).toContain('gateUrl');
    // 渲染层没有拿 token 的通道：能力视图只回 gate 布尔值
    expect(rustComputer()).not.toContain('"gateToken"');
  });

  it('引擎只在 Windows 执行器就绪时注册两个工具，子 Agent 不继承', () => {
    const server = engineServer();
    expect(server).toContain('cu.ComputerUse.from_runtime');
    const engine = engineComputerUse();
    expect(engine).toContain('screen_capture');
    expect(engine).toContain('computer_action');
    // 子 Agent 的 build_agent 调用不带 computer（只有父 Agent 那一次带）
    const childCall = server.slice(
      server.indexOf('def _make_child_agent'),
      server.indexOf('subagent_runner = sar.SubagentRunner'),
    );
    expect(childCall).toContain('build_agent(');
    expect(childCall).not.toContain('computer=');
  });
});

describe('电脑操控错误码都有中文文案', () => {
  it('Rust 执行器/网关抛出的 COMPUTER_USE_* 全部可映射', () => {
    const codes = new Set<string>();
    for (const src of [rustComputer(), rustGate()]) {
      for (const m of src.matchAll(/(?<=")COMPUTER_USE_[A-Z][A-Z_]*/g)) codes.add(m[0]);
    }
    expect(codes.size).toBeGreaterThanOrEqual(9);
    for (const code of codes) {
      const text = errorMessageOf(code);
      expect(text).not.toContain(code);
      expect(text.length).toBeGreaterThan(4);
    }
    expect(ErrorCode.COMPUTER_USE_UNSUPPORTED).toBe('COMPUTER_USE_UNSUPPORTED');
    expect(ErrorCode.COMPUTER_USE_DENIED_BY_USER).toBe('COMPUTER_USE_DENIED_BY_USER');
  });
});

describe('浏览器/Mock 通道的电脑操控能力视图', () => {
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

  it('能力视图如实为不可用，不用保存的档位冒充可截屏', async () => {
    const caps = await a.getComputerUseCapabilities();
    expect(caps.supported).toBe(false);
    expect(caps.platform).toBe('browser');
    expect(caps.features).toEqual({ screenshot: false, input: false });
    expect(caps.gate).toBe(false);
    expect(caps.policy).toBe('ask_every_time');
    expect(caps.persisted).toBe(false);
    expect(caps.reason).toContain('没有系统截屏与键鼠注入能力');

    await a.setComputerUsePolicy('auto');
    const after = await a.getComputerUseCapabilities();
    expect(after.policy).toBe('auto');
    expect(after.persisted).toBe(true);
    expect(after.supported).toBe(false);
  });

  it('截屏自检在本地通道抛 COMPUTER_USE_UNSUPPORTED，不返回占位图', async () => {
    await expect(a.captureComputerUseScreen()).rejects.toThrow(/COMPUTER_USE_UNSUPPORTED/);
    expect(alignmentLocal()).toContain('captureLocalComputerUseScreen');
  });
});

describe('电脑操控页消费真实能力视图', () => {
  it('读取能力、写策略、调用截屏自检，不再标注整页未接入', () => {
    const src = panel();
    expect(src).toContain('getComputerUseCapabilities');
    expect(src).toContain('setComputerUsePolicy');
    expect(src).toContain('captureComputerUseScreen');
    expect(src).not.toContain('电脑操控执行器尚未接入');
    expect(src).not.toContain('系统权限（接入后适用）');
  });

  it('保留诚实边界：审计不记内容、图片不落盘、危险组合被拒', () => {
    const src = panel();
    expect(src).toContain('审计只记录截屏尺寸，不记录画面内容');
    expect(src).toContain('图片仅在当前页面内存中预览，不落盘');
    expect(src).toContain('Win 键组合与任务管理器快捷键会被拒绝');
    expect(src).toContain('子 Agent 不继承电脑操控工具');
  });
});
