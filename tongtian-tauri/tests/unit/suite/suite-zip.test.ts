/**
 * 专家套件 · 套件包解析单元测试（任务 #37）
 *
 * 浏览器通道没有列目录与写盘能力，但必须把用户上传的同一个 .zip 解析出与桌面端
 * （Rust `suite::install_in`）一致的结果，否则「已安装」列表与输入框 / 候选会两头对不上。
 * 因此这里逐条对齐 Rust 的口径：标识规则、manifest 定位、commands 三种写法、
 * 技能数取大、路径安全拒绝、以及不损坏时不谎报成功。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  SuitePackageError,
  base64ToBytes,
  bytesToBase64,
  crc32,
  parseSuitePackage,
  parseSuitePackageFromBase64,
  suiteIdOf,
} from '../../../src/renderer/lib/suite-zip';
import { buildZip, bytesToBase64Native, crc32Of, demoPackage } from './zip-fixture';

function readSource(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

function manifestJson(fields: Record<string, unknown>): string {
  return JSON.stringify({ name: 'demo-suite', ...fields });
}

async function parseOne(entries: { path: string; content: string; deflate?: boolean }[]) {
  return parseSuitePackage(buildZip(entries));
}

async function expectIncompatible(promise: Promise<unknown>): Promise<string> {
  try {
    await promise;
  } catch (e) {
    expect(e).toBeInstanceOf(SuitePackageError);
    const msg = (e as Error).message;
    expect(msg.startsWith('SUITE_INCOMPATIBLE: ')).toBe(true);
    return msg;
  }
  throw new Error('预期解析失败，实际成功');
}

// ── 跨语言标识与校验和 ────────────────────────────────────

describe('crc32 · 与夹具独立实现及 Rust crc32fast 同结果', () => {
  it('标准校验和已知答案', () => {
    expect(crc32(new TextEncoder().encode('123456789'))).toBe(0xcbf43926);
    expect(crc32(new TextEncoder().encode(''))).toBe(0);
  });

  it('中文名的哈希与 Rust suite_id_of 的已知答案一致', () => {
    // Rust: assert_eq!(suite_id_of("金融行业套件"), "suite-ac1b217b")
    expect(crc32(new TextEncoder().encode('金融行业套件'))).toBe(0xac1b217b);
    expect(crc32(new TextEncoder().encode('法务合同审查包'))).toBe(0x184f497e);
  });

  it('与测试夹具的独立查表实现互证', () => {
    const bytes = new TextEncoder().encode('{"name":"金融行业套件"}');
    expect(crc32(bytes)).toBe(crc32Of(bytes));
  });
});

describe('suiteIdOf · 与 Rust 同规则', () => {
  it('纯 ASCII 名称转写为 slug', () => {
    expect(suiteIdOf('Prompt Design Studio')).toBe('prompt-design-studio');
    expect(suiteIdOf('  Finance-Suite  ')).toBe('finance-suite');
  });

  it('含非 ASCII 走定长哈希，中文展示名保留在 manifest 里', () => {
    expect(suiteIdOf('金融行业套件')).toBe('suite-ac1b217b');
    expect(suiteIdOf('金融行业套件')).toBe(suiteIdOf('金融行业套件'));
    expect(suiteIdOf('法律合规套件')).toBe('suite-5fce929d');
  });

  it('无法转写为合法标识（纯符号 / 超 64 字符）时回退哈希', () => {
    expect(suiteIdOf('!!!')).toMatch(/^suite-[0-9a-f]{8}$/);
    expect(suiteIdOf('a'.repeat(65))).toMatch(/^suite-[0-9a-f]{8}$/);
    expect(suiteIdOf('b'.repeat(64))).toBe('b'.repeat(64));
  });
});

// ── base64 往返 ──────────────────────────────────────────

describe('base64 往返（IPC 传字节）', () => {
  it('与 Buffer 实现互证，且容忍 data URL 前缀与换行', () => {
    const bytes = demoPackage().zip;
    expect(bytesToBase64(bytes)).toBe(bytesToBase64Native(bytes));
    const b64 = bytesToBase64(bytes);
    expect(base64ToBytes(`data:application/zip;base64,${b64}`).length).toBe(bytes.length);
    expect(base64ToBytes(`${b64.slice(0, 8)}\n${b64.slice(8)}\r\n`).length).toBe(bytes.length);
  });

  it('空串与非法 base64 都如实拒绝，不返回空包继续安装', () => {
    expect(() => base64ToBytes('')).toThrowError(/^SUITE_INCOMPATIBLE/);
    expect(() => base64ToBytes('@@@not-base64@@@')).toThrowError(/^SUITE_INCOMPATIBLE/);
  });
});

// ── manifest 定位与字段解析 ───────────────────────────────

describe('parseSuitePackage · 正常包', () => {
  it('一级目录 plugin.json + DEFLATE 条目：字段全部来自包内声明', async () => {
    const { suite, suiteId, fileNames } = await parseOne([
      {
        path: '.qoder-plugin/plugin.json',
        content: manifestJson({
          version: 'v1.4.0',
          author: '@行政百宝箱',
          category: '行政',
          description: '会议纪要',
          commands: [{ name: 'meeting-minutes', description: '整理会议纪要' }, 'notice-draft'],
        }),
        deflate: true,
      },
      { path: 'README.md', content: '# 说明\n' },
    ]);
    expect(suiteId).toBe('demo-suite');
    expect(suite).toMatchObject({
      id: 'demo-suite',
      name: 'demo-suite',
      version: 'v1.4.0',
      author: '@行政百宝箱',
      category: '行政',
      description: '会议纪要',
      fileCount: 3, // 包内 2 个文件 + 归一后的 manifest.json
      readable: true,
      enabled: true,
      installedAtMs: 0,
      path: '',
    });
    expect(suite.commands).toEqual([
      { name: 'meeting-minutes', description: '整理会议纪要' },
      { name: 'notice-draft', description: null },
    ]);
    expect(fileNames).toEqual(['.qoder-plugin/plugin.json', 'README.md']);
  });

  it('根目录 plugin.json（STORE）同样识别', async () => {
    const { suiteId } = await parseOne([{ path: 'plugin.json', content: manifestJson({}) }]);
    expect(suiteId).toBe('demo-suite');
  });

  it('缺省字段用如实占位而不是编造：版本「未标注版本」，作者/分类为 null', async () => {
    const { suite } = await parseOne([{ path: 'plugin.json', content: '{"name":"裸包套件"}' }]);
    expect(suite.version).toBe('未标注版本');
    expect(suite.author).toBeNull();
    expect(suite.category).toBeNull();
    expect(suite.description).toBeNull();
    expect(suite.commands).toEqual([]);
    expect(suite.name).toBe('裸包套件');
    expect(suite.id).toBe('suite-3be86fe5');
  });

  it('commands 兼容对象映射写法（与 Rust parse_commands 同口径）', async () => {
    const { suite } = await parseOne([
      { path: 'plugin.json', content: manifestJson({ commands: { 'weekly-report': '生成周报' } }) },
    ]);
    expect(suite.commands).toEqual([{ name: 'weekly-report', description: '生成周报' }]);
  });

  it('commands 里的空串与无名对象被丢弃', async () => {
    const { suite } = await parseOne([
      { path: 'plugin.json', content: manifestJson({ commands: ['', '  ', { description: '无名称' }] }) },
    ]);
    expect(suite.commands).toEqual([]);
  });

  it('技能数取「包内声明」与「SKILL.md 个数」的大者', async () => {
    const declared = await parseOne([
      { path: 'plugin.json', content: manifestJson({ skills: ['a', 'b', ' ', { name: 'c' }] }) },
      { path: 'skills/x/SKILL.md', content: '# x\n' },
    ]);
    expect(declared.suite.skillCount).toBe(3); // 声明 3 个有效 > 1 个 SKILL.md
    const files = await parseOne([
      { path: 'plugin.json', content: manifestJson({ skills: ['a'] }) },
      { path: 'skills/x/SKILL.md', content: '# x\n' },
      { path: 'skills/y/SKILL.md', content: '# y\n' },
    ]);
    expect(files.suite.skillCount).toBe(2);
    const dataLinks = await parseOne([
      { path: 'plugin.json', content: manifestJson({ dataLinks: [{ name: 'crm' }, 'erp', 3] }) },
    ]);
    expect(dataLinks.suite.dataLinkCount).toBe(2);
  });

  it('套件包目录项不计入文件数', async () => {
    const { suite } = await parseOne([
      { path: 'plugin.json', content: manifestJson({}) },
      { path: 'assets/', content: '' },
    ]);
    expect(suite.fileCount).toBe(2);
  });

  it('反斜杠路径归一为正斜杠后再判安全', async () => {
    const msg = await expectIncompatible(
      parseOne([
        { path: 'plugin.json', content: manifestJson({}) },
        { path: 'a\\..\\b.json', content: '{}' },
      ]),
    );
    expect(msg).toContain('a/../b.json');
  });
});

// ── 拒绝路径：不产出半成品套件 ────────────────────────────

describe('parseSuitePackage · 非法包一律拒绝', () => {
  it('缺少 plugin.json', async () => {
    const msg = await expectIncompatible(
      parseOne([{ path: 'README.md', content: '# 只有说明\n' }]),
    );
    expect(msg).toContain('plugin.json');
  });

  it('plugin.json 位于两层目录之外不识别（与 Rust 深度上限一致）', async () => {
    await expectIncompatible(
      parseOne([{ path: 'pkg/inner/plugin.json', content: manifestJson({}) }]),
    );
  });

  it('plugin.json 不是合法 JSON', async () => {
    const msg = await expectIncompatible(parseOne([{ path: 'plugin.json', content: '{name:' }]));
    expect(msg).toContain('不是合法 JSON');
  });

  it('name 缺失或为空白都拒绝', async () => {
    await expectIncompatible(parseOne([{ path: 'plugin.json', content: '{"version":"v1"}' }]));
    await expectIncompatible(parseOne([{ path: 'plugin.json', content: '{"name":"   "}' }]));
  });

  it('越界与绝对路径条目拒绝，不给可落盘的假成功', async () => {
    for (const bad of ['../evil.txt', '/etc/passwd', 'a/../../b.txt', 'C:/Windows/x']) {
      const msg = await expectIncompatible(
        parseOne([{ path: 'plugin.json', content: manifestJson({}) }, { path: bad, content: 'x' }]),
      );
      expect(msg).toContain('不安全路径');
    }
  });

  it('不是 zip / 文件过小 / 目录损坏都给出可定位的原因', async () => {
    await expectIncompatible(parseSuitePackage(new TextEncoder().encode('这不是压缩包')));
    const truncated = demoPackage().zip.slice(0, 8);
    await expectIncompatible(parseSuitePackage(truncated));
    const broken = buildZip([{ path: 'plugin.json', content: manifestJson({}) }]);
    new DataView(broken.buffer).setUint32(0, 0xdeadbeef, true); // 本地头签名破坏
    await expectIncompatible(parseSuitePackage(broken));
  });

  it('条目数与字节上限与 Rust 同档（常量对齐即口径不漂移）', () => {
    const src = readSource('../../../src/renderer/lib/suite-zip.ts');
    const rust = readSource('../../../src-tauri/src/suite/mod.rs');
    expect(src).toContain('const MAX_ENTRIES = 256');
    expect(src).toContain('const MAX_MANIFEST_BYTES = 256 * 1024');
    expect(rust).toContain('const MAX_ENTRIES: usize = 256;');
    expect(rust).toContain('const MAX_MANIFEST_BYTES: u64 = 256 * 1024;');
    expect(rust).toContain('const MAX_ID_LEN: usize = 64;');
  });
});

// ── base64 入口 ───────────────────────────────────────────

describe('parseSuitePackageFromBase64', () => {
  it('与字节入口得到同一套件', async () => {
    const { zip, zipBase64 } = demoPackage('供应链协同套件');
    const a = await parseSuitePackage(zip);
    const b = await parseSuitePackageFromBase64(zipBase64);
    expect(b).toEqual(a);
    expect(b.suiteId).toBe('suite-5b56b7ed');
    expect(b.suite.name).toBe('供应链协同套件');
  });
});
