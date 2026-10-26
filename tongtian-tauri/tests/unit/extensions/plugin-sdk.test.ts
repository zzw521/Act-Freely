/**
 * 插件 SDK · 契约测试
 *
 * 覆盖三层：
 * 1. 桌面端命令注册与通道映射（Rust lib.rs / tauri-api.ts 字面契约）
 * 2. 浏览器通道如实拒绝（不伪造模板或预检结果）
 * 3. scripts/plugin-sdk.mjs 的真实文件行为：init → validate → pack
 *    （pack 产物按 ZIP 中央目录解析，确认是安装器可读的 STORE 包）
 */

import { describe, it, expect } from 'vitest';
import { readFileSync, existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { ErrorCode } from '../../../src/shared/contracts';
import { errorMessageOf } from '../../../src/shared/error-messages';
import { handleLocalSuiteChannel } from '../../../src/renderer/lib/suite-local';

const REPO = fileURLToPath(new URL('../../../', import.meta.url));
const SCRIPT = path.join(REPO, 'scripts', 'plugin-sdk.mjs');

function src(rel: string): string {
  return readFileSync(path.join(REPO, rel), 'utf8').replace(/\r\n/g, '\n');
}

function run(args: string[], cwd: string) {
  return spawnSync(process.execPath, [SCRIPT, ...args], { cwd, encoding: 'utf8' });
}

function tempDir(): string {
  return mkdtempSync(path.join(tmpdir(), 'tt-plugin-sdk-'));
}

/** 读 ZIP 中央目录：返回名称与方法（0=STORE，8=DEFLATE） */
function readZipEntries(buf: Buffer): { name: string; method: number }[] {
  let eocd = -1;
  for (let i = buf.length - 22; i >= 0; i -= 1) {
    if (buf.readUInt32LE(i) === 0x06054b50) {
      eocd = i;
      break;
    }
  }
  expect(eocd).toBeGreaterThanOrEqual(0);
  const count = buf.readUInt16LE(eocd + 10);
  let p = buf.readUInt32LE(eocd + 16);
  const out: { name: string; method: number }[] = [];
  for (let k = 0; k < count; k += 1) {
    expect(buf.readUInt32LE(p)).toBe(0x02014b50);
    const method = buf.readUInt16LE(p + 10);
    const nameLen = buf.readUInt16LE(p + 28);
    const extraLen = buf.readUInt16LE(p + 30);
    const commentLen = buf.readUInt16LE(p + 32);
    const name = buf.subarray(p + 46, p + 46 + nameLen).toString('utf8');
    out.push({ name, method });
    p += 46 + nameLen + extraLen + commentLen;
  }
  return out;
}

describe('插件 SDK · 桌面端接线契约', () => {
  it('Rust 注册预检与模板命令', () => {
    const lib = src('src-tauri/src/lib.rs');
    expect(lib).toContain('mod plugin;');
    expect(lib).toContain('plugin::plugin_validate');
    expect(lib).toContain('plugin::plugin_template');
  });

  it('渲染层通道映射到真实命令（不是前端假模板）', () => {
    const tauri = src('src/renderer/lib/tauri-api.ts');
    expect(tauri).toContain("channel === 'plugin:validate'");
    expect(tauri).toContain("invoke('plugin_validate'");
    expect(tauri).toContain("channel === 'plugin:template'");
    expect(tauri).toContain("invoke('plugin_template'");
  });

  it('错误码有中文文案，说明为什么浏览器通道不可用', () => {
    expect(errorMessageOf(ErrorCode.PLUGIN_UNSUPPORTED)).toContain('桌面版');
    expect(errorMessageOf(ErrorCode.PLUGIN_INVALID_PACKAGE)).toContain('plugin.json');
  });
});

describe('插件 SDK · 浏览器通道如实降级', () => {
  it('预检拒绝并带 PLUGIN_UNSUPPORTED，不返回假的 ok', async () => {
    const res = handleLocalSuiteChannel('plugin:validate', [{ fileName: 'x.zip', zipBase64: '' }]);
    expect(res.handled).toBe(true);
    await expect(res.value as Promise<unknown>).rejects.toThrowError(/^PLUGIN_UNSUPPORTED/);
  });

  it('模板同样拒绝：浏览器读不到内置模板文件', async () => {
    const res = handleLocalSuiteChannel('plugin:template', [{ name: '套件' }]);
    await expect(res.value as Promise<unknown>).rejects.toThrowError(/^PLUGIN_UNSUPPORTED/);
  });
});

describe('plugin-sdk.mjs · init / validate / pack 真实生效', () => {
  it('init 生成四个文件并替换套件名；重复 init 拒绝覆盖', () => {
    const dir = tempDir();
    try {
      const first = run(['init', dir, '--name', '演示套件'], REPO);
      expect(first.status).toBe(0);
      const manifest = JSON.parse(readFileSync(path.join(dir, 'plugin.json'), 'utf8'));
      expect(manifest.name).toBe('演示套件');
      expect(existsSync(path.join(dir, 'commands', 'hello.md'))).toBe(true);
      expect(existsSync(path.join(dir, 'skills', 'example', 'SKILL.md'))).toBe(true);
      const readme = readFileSync(path.join(dir, 'README.md'), 'utf8');
      expect(readme).toContain('演示套件');
      expect(readme).not.toContain('{{name}}');

      const second = run(['init', dir], REPO);
      expect(second.status).toBe(1);
      expect(second.stderr).toContain('不覆盖');
      expect(JSON.parse(readFileSync(path.join(dir, 'plugin.json'), 'utf8')).name).toBe('演示套件');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('validate 读真实正文来源；--json 输出与桌面端字段同名', () => {
    const dir = tempDir();
    try {
      run(['init', dir, '--name', '校验套件'], REPO);
      const res = run(['validate', dir, '--json'], REPO);
      expect(res.status).toBe(0);
      const out = JSON.parse(res.stdout);
      expect(out.ok).toBe(true);
      expect(out.manifest.name).toBe('校验套件');
      expect(out.commands).toHaveLength(1);
      expect(out.commands[0]).toMatchObject({ name: 'hello', bodySource: 'file', error: null });
      expect(out.commands[0].bodyChars).toBeGreaterThan(0);
      expect(out.skillFiles).toEqual(['skills/example/SKILL.md']);
      expect(out.warnings).toEqual([]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('坏指令名阻断；缺正文与不执行字段给 warning，不冒充通过也不误杀', () => {
    const dir = tempDir();
    try {
      writeFileSync(
        path.join(dir, 'plugin.json'),
        JSON.stringify({
          name: '问题包',
          commands: [{ name: '../escape' }, { name: 'nodody' }],
          mcpServers: { x: {} },
        }),
      );
      const res = run(['validate', dir, '--json'], REPO);
      expect(res.status).toBe(1);
      const out = JSON.parse(res.stdout);
      expect(out.ok).toBe(false);
      expect(out.errors.join('\n')).toContain('路径分隔符');
      expect(out.warnings.join('\n')).toContain('没有正文');
      expect(out.warnings.join('\n')).toContain('不会被执行');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it('pack 产出安装器可读的 STORE ZIP；校验不过时不生成文件', () => {
    const dir = tempDir();
    const outZip = path.join(dir, 'dist.zip');
    try {
      run(['init', dir, '--name', '打包套件'], REPO);
      const ok = run(['pack', dir, '-o', outZip], REPO);
      expect(ok.status).toBe(0);
      const entries = readZipEntries(readFileSync(outZip));
      expect(entries.map((e) => e.name).sort()).toEqual([
        'README.md',
        'commands/hello.md',
        'plugin.json',
        'skills/example/SKILL.md',
      ]);
      expect(entries.every((e) => e.method === 0)).toBe(true);

      const bad = tempDir();
      writeFileSync(path.join(bad, 'plugin.json'), JSON.stringify({ version: '1.0.0' }));
      const badZip = path.join(bad, 'bad.zip');
      const failed = run(['pack', bad, '-o', badZip], REPO);
      expect(failed.status).toBe(1);
      expect(failed.stderr).toContain('校验未通过');
      expect(existsSync(badZip)).toBe(false);
      rmSync(bad, { recursive: true, force: true });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
