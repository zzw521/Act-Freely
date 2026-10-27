/**
 * 打包资源契约：引擎必须以整目录随包分发
 *
 * 背景：安装包曾只映射 engine/server.py 与 requirements.txt，其余顶层模块
 * （im_channels 等）全部漏带，安装后引擎启动即 ModuleNotFoundError，
 * 桌面端只能看到「健康检查超时（30s）」这种与真因无关的报错。
 * 因此这里锁死资源配置形态，防止再退回逐文件映射。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const projectRoot = fileURLToPath(new URL('../../../', import.meta.url));

function readJson<T>(rel: string): T {
  return JSON.parse(readFileSync(new URL(rel, import.meta.url), 'utf8')) as T;
}

type TauriConf = {
  bundle: { resources: Record<string, string>; useLocalToolsDir?: boolean };
};

const conf = readJson<TauriConf>('../../../src-tauri/tauri.conf.json');
const resources = conf.bundle.resources;

describe('tauri.conf.json 打包资源', () => {
  it('引擎整目录随包（不是逐文件映射）', () => {
    expect(resources['../engine']).toBe('engine');
  });

  it('不再出现按引擎文件逐个映射的条目', () => {
    const engineFiles = Object.keys(resources).filter((src) =>
      /\/engine\/[^/]+\.py$/.test(src),
    );
    expect(engineFiles).toEqual([]);
  });

  it('保留随包 Python 与 mock agent 脚本', () => {
    expect(resources['../vendor/python']).toBe('python');
    expect(resources['../scripts/mock-agent.py']).toBe('scripts/mock-agent.py');
    expect(conf.bundle.useLocalToolsDir).toBe(true);
  });

  it('引擎目录里被 server.py 导入的同级模块确实存在', () => {
    const engineDir = `${projectRoot}engine`;
    const present = new Set(
      readdirSync(engineDir)
        .filter((name) => name.endsWith('.py'))
        .map((name) => name.slice(0, -3)),
    );
    const server = readFileSync(new URL('../../../engine/server.py', import.meta.url), 'utf8');
    const imported = [...server.matchAll(/^\s*(?:import|from)\s+([a-z_][a-z0-9_]*)/gm)]
      .map((m) => m[1])
      .filter((name) => present.has(name));
    expect(imported.length).toBeGreaterThan(0);
    // 整目录映射天然覆盖这些模块；此断言同时保证模块没被误删到别处
    expect(imported).toContain('im_channels');
  });
});
