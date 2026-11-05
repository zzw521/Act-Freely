// tests/unit/snapshot/snapshot-contract.test.ts — #140 快照与接力的跨语言契约
//
// 钉的是「渲染层那套镜像有没有跟上引擎」：结论码表、标签表、端点清单、方法与路径。
// 引擎侧的行为（闸、状态机、审计）由 tests/integration/engine_snapshot_selftest.py 与
// engine_snapshot_routes_selftest.py 验；这里只保证界面不会拿着过期的表说话。
//
// 为什么值得单独钉：这一族的句子是引擎写给本机用户看的，前端一旦自己补一套
// 「不支持」的文案，就会出现「引擎拦了、界面说成没开启」的假账。

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

/**
 * 只留代码，去掉注释。
 * 这些文件头本身就写着「不写 localStorage、不进日志」——连着注释一起扫，
 * 会把「声明了不做」扫成「做了」，那条用例就永远绿得莫名其妙。
 */
function stripComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '')
    .replace(/[ \t]+\/\/[^\n]*$/gm, '');
}

const engineSnap = () => read('../../../engine/snapshot_runtime.py');
const engineRelay = () => read('../../../engine/snapshot_relay.py');
const engineRoutes = () => read('../../../engine/routes_workspace.py');
const rustClassify = () => read('../../../src-tauri/src/security/classify.rs');
const tsContract = () => read('../../../src/shared/snapshot-contract.ts');
const tsApi = () => read('../../../src/renderer/lib/snapshot-api.ts');

const {
  SNAPSHOT_ENDPOINTS,
  SNAPSHOT_CODES,
  RELAY_CODES,
  SENSITIVITY_LABELS,
  STATUS_LABELS,
  RELAY_DENIAL_LABELS,
  SNAPSHOT_ADMISSION_CODE,
  labelOf,
} = await import('../../../src/shared/snapshot-contract');

/** 把 python 里 `NAME = "value"` 那种顶层常量收成一张表 */
function pyConsts(src: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const match of src.matchAll(/^([A-Z][A-Z0-9_]*) = "([^"]*)"$/gm)) {
    out[match[1]] = match[2];
  }
  return out;
}

/** 解析 python 顶层 dict 字面量：键是常量名，值是中文标签 */
function pyDict(src: string, name: string): Record<string, string> {
  const consts = pyConsts(src);
  const start = src.indexOf(`${name} = {`);
  expect(start, `引擎里找不到 ${name} 这张表`).toBeGreaterThanOrEqual(0);
  const block = src.slice(start, src.indexOf('\n}', start));
  const out: Record<string, string> = {};
  for (const match of block.matchAll(/^\s*([A-Z][A-Z0-9_]*): "([^"]*)",?$/gm)) {
    const key = consts[match[1]];
    expect(key, `${name} 的键 ${match[1]} 不是顶层字符串常量`).toBeTruthy();
    out[key as string] = match[2];
  }
  return out;
}

/** 解析 python 顶层 tuple/list 字面量里的字符串项（不含嵌套） */
function pySeq(src: string, name: string): string[] {
  const tuple = src.indexOf(`${name} = (`);
  const list = src.indexOf(`${name} = [`);
  expect(tuple >= 0 || list >= 0, `引擎里找不到 ${name}`).toBe(true);
  const decl = tuple >= 0 ? tuple : list;
  const openChar = tuple >= 0 ? '(' : '[';
  const closeChar = tuple >= 0 ? ')' : ']';
  const open = src.indexOf(openChar, decl);
  const body = src.slice(open + 1, src.indexOf(closeChar, open));
  return [...body.matchAll(/"([^"]+)"/g)].map((m) => m[1]).filter(Boolean);
}

/** 从 `status()` 里的 `"codes": [ CODE_A, CODE_B, ... ]` 收成引擎实际声明的结论码值 */
function statusCodes(src: string): string[] {
  const consts = pyConsts(src);
  const start = src.indexOf('"codes": [');
  expect(start, '引擎状态回话里没有 codes 清单').toBeGreaterThanOrEqual(0);
  const block = src.slice(start, src.indexOf(']', start));
  const names = [...block.matchAll(/^\s*(CODE_[A-Z0-9_]+),$/gm)].map((m) => m[1]);
  const values = names.map((name) => consts[name]);
  // 表里出现「没定义过的常量名」说明引擎自己漏了，先在这里红，别让界面替它兜
  expect(values.some((value) => !value), `${names.join(' / ')} 中有未定义的常量`).toBe(false);
  return values as string[];
}

describe('快照契约：TS 镜像 == 引擎事实源', () => {
  it('三档分级标签与引擎逐字相等（不是「相近的中文」）', () => {
    expect(SENSITIVITY_LABELS).toEqual(pyDict(engineSnap(), 'SENSITIVITY_LABELS'));
  });

  it('草稿状态机五个标签与引擎逐字相等', () => {
    expect(STATUS_LABELS).toEqual(pyDict(engineSnap(), 'STATUS_LABELS'));
  });

  it('接力四类拒绝来源与引擎逐字相等，且不因怕被合并而少一类', () => {
    const table = pyDict(engineRelay(), 'SOURCE_LABELS');
    expect(RELAY_DENIAL_LABELS).toEqual(table);
    expect(Object.keys(table)).toHaveLength(4);
    // 四句必须互不相同：并成一句就等于把「谁拒的」这个问题答没了
    expect(new Set(Object.values(table)).size).toBe(4);
  });

  it('快照族结论码与引擎 status() 里那份一一对应，不多不少', () => {
    expect(Object.values(SNAPSHOT_CODES).sort()).toEqual(statusCodes(engineSnap()).sort());
    expect(statusCodes(engineSnap())).toHaveLength(12);
  });

  it('接力族结论码与引擎 status() 里那份一一对应，不多不少', () => {
    expect(Object.values(RELAY_CODES).sort()).toEqual(statusCodes(engineRelay()).sort());
    expect(statusCodes(engineRelay())).toHaveLength(14);
  });

  it('两族码表不混，且都不含后端那族的 WS_ 前缀（准入闸单独一枚）', () => {
    const snapValues = Object.values(SNAPSHOT_CODES);
    const relayValues = Object.values(RELAY_CODES);
    expect(snapValues.filter((code) => code.startsWith('RELAY_'))).toEqual([]);
    expect(relayValues.filter((code) => code.startsWith('SNAPSHOT_'))).toEqual([]);
    expect([...snapValues, ...relayValues].filter((code) => code.startsWith('WS_'))).toEqual([]);
    expect(SNAPSHOT_ADMISSION_CODE).toBe('WS_DESKTOP_ONLY');
    // 那三枚「 advertise 了但没人发」的幽灵码不许回来
    for (const dead of ['SNAPSHOT_NOT_PENDING', 'SNAPSHOT_DESKTOP_ONLY', 'RELAY_DESKTOP_ONLY']) {
      expect([...snapValues, ...relayValues]).not.toContain(dead);
    }
  });

  it('标签页超限与域名超限是两枚码；want 给错是参数错不是没授权', () => {
    expect(RELAY_CODES.tabsOverflow).toBe('RELAY_TAB_LIMIT');
    expect(RELAY_CODES.hostsOverflow).toBe('RELAY_HOST_LIMIT');
    expect(RELAY_CODES.tabsOverflow).not.toBe(RELAY_CODES.hostsOverflow);
    expect(RELAY_CODES.wantInvalid).toBe('RELAY_WANT_INVALID');
    expect(RELAY_CODES.wantInvalid).not.toBe(RELAY_CODES.notGranted);
  });

  it('端点清单与引擎路由声明逐字相等，且方法（GET/POST）也对得上', () => {
    const src = engineRoutes();
    const enginePaths = pySeq(src, 'SNAPSHOT_ENDPOINT_PATHS');
    expect(Object.values(SNAPSHOT_ENDPOINTS).sort()).toEqual(enginePaths.sort());
    expect(enginePaths).toHaveLength(12);

    const readPaths = [...src.matchAll(/@router\.get\("(\/api\/snapshot[^"]*)"\)/g)].map((m) => m[1]);
    const writePaths = [...src.matchAll(/@router\.post\("(\/api\/snapshot[^"]*)"\)/g)].map((m) => m[1]);
    expect([...readPaths, ...writePaths].sort()).toEqual(enginePaths.sort());
    // 读面恰好五条：status / pending / preview / decisions / relay/status
    expect(readPaths).toHaveLength(5);
    expect(readPaths).toContain('/api/snapshot/pending');
    expect(writePaths).toContain('/api/snapshot/capture');
  });

  it('分级三档的字面量与 Rust 分类器同源（引擎是 Rust 的镜像，界面是引擎的镜像）', () => {
    const rust = rustClassify();
    for (const value of Object.keys(SENSITIVITY_LABELS)) {
      expect(rust, `Rust 分类器里没有 ${value} 这一档`).toContain(`pub const ${value.toUpperCase()}: &str = "${value}";`);
    }
    expect(Object.keys(SENSITIVITY_LABELS)).toEqual(['public', 'internal', 'confidential']);
  });

  it('表跟不上引擎时界面回显原值，不留空白', () => {
    expect(labelOf(SENSITIVITY_LABELS, 'public')).toBe('未检出敏感');
    expect(labelOf(STATUS_LABELS, 'pending')).toBe('待确认（尚未上送）');
    expect(labelOf(RELAY_DENIAL_LABELS, 'session')).toBe('受控会话现场（浏览器没在跑就没有可接力的东西）');
    expect(labelOf(SENSITIVITY_LABELS, 'top_secret')).toBe('top_secret');
  });
});

describe('快照客户端：不截屏、不留正文、不新增 IPC', () => {
  it('客户端只走引擎 HTTP，不碰 IPC', () => {
    const src = stripComments(tsApi());
    expect(src).toContain('engineFetch');
    expect(src).not.toContain('tongtianAPI');
    expect(src).not.toContain('.invoke(');
    expect(src).not.toContain('IPC_CHANNELS');
  });

  it('正文不落到任何持久层，也不写日志', () => {
    const src = stripComments(tsApi()) + stripComments(tsContract());
    for (const needle of ['localStorage', 'sessionStorage', 'indexedDB', 'document.cookie']) {
      expect(src, `快照这一族不该碰 ${needle}`).not.toContain(needle);
    }
    expect(src).not.toContain('console.log');
  });

  it('本车道不声明截屏能力，也不声明快捷键', () => {
    const src = tsApi() + tsContract();
    expect(src).not.toContain('已接通');
    // 未验收的句子来自引擎（status().unverified），界面只做转述：这里不出现「已支持」的措辞
    expect(src).not.toMatch(/已支持(对象存储|真机采集|全局快捷键)/);
  });

  it('引擎侧那两族模块没被渲染层的说法反向污染', () => {
    // 界面镜像里的码表若与引擎漂移，前面几条会先红；这里额外钉住「句子只在引擎」
    expect(engineSnap()).toContain('SNAPSHOT_NOTHING_CAPTURED');
    expect(engineRoutes()).toContain('列表不是预览');
  });
});
