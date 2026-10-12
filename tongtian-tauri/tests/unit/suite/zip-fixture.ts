/**
 * 专家套件测试夹具：手写最小 ZIP 容器（STORE + DEFLATE）
 *
 * 不引入压缩库依赖，按 ZIP 规范逐字段拼字节，供套件包解析与本地台账用例复用。
 * CRC 采用独立实现的查表法，避免用被测模块自己的 crc32 生成夹具（否则校验和用例形同自证）。
 */

import { deflateRawSync } from 'node:zlib';

export interface FixtureEntry {
  path: string;
  content: string;
  /** true 用 DEFLATE（method=8），默认 STORE（method=0） */
  deflate?: boolean;
}

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let i = 0; i < 256; i += 1) {
    let c = i;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[i] = c >>> 0;
  }
  return table;
})();

/** 标准 CRC-32（与 Rust crc32fast / node zlib.crc32 同结果） */
export function crc32Of(bytes: Uint8Array): number {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i += 1) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

export function buildZip(entries: FixtureEntry[]): Uint8Array {
  const locals: Uint8Array[] = [];
  const centrals: Uint8Array[] = [];
  let offset = 0;

  for (const e of entries) {
    const name = new TextEncoder().encode(e.path);
    const data = new TextEncoder().encode(e.content);
    const method = e.deflate ? 8 : 0;
    const body = e.deflate ? new Uint8Array(deflateRawSync(data)) : data;
    const crc = crc32Of(data);

    const local = new Uint8Array(30);
    const lv = new DataView(local.buffer);
    lv.setUint32(0, 0x04034b50, true);
    lv.setUint16(4, 20, true);
    lv.setUint16(8, method, true);
    lv.setUint32(14, crc, true);
    lv.setUint32(18, body.length, true);
    lv.setUint32(22, data.length, true);
    lv.setUint16(26, name.length, true);
    locals.push(local, name, body);

    const central = new Uint8Array(46);
    const cv = new DataView(central.buffer);
    cv.setUint32(0, 0x02014b50, true);
    cv.setUint16(4, 20, true);
    cv.setUint16(6, 20, true);
    cv.setUint16(10, method, true);
    cv.setUint32(16, crc, true);
    cv.setUint32(20, body.length, true);
    cv.setUint32(24, data.length, true);
    cv.setUint16(28, name.length, true);
    cv.setUint32(42, offset, true);
    centrals.push(central, name);

    offset += local.length + name.length + body.length;
  }

  const centralStart = offset;
  let centralSize = 0;
  for (const c of centrals) centralSize += c.length;

  const eocd = new Uint8Array(22);
  const ev = new DataView(eocd.buffer);
  ev.setUint32(0, 0x06054b50, true);
  ev.setUint16(8, entries.length, true);
  ev.setUint16(10, entries.length, true);
  ev.setUint32(12, centralSize, true);
  ev.setUint32(16, centralStart, true);

  return concat([...locals, ...centrals, eocd]);
}

function concat(parts: Uint8Array[]): Uint8Array {
  const total = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(total);
  let at = 0;
  for (const p of parts) {
    out.set(p, at);
    at += p.length;
  }
  return out;
}

export function bytesToBase64Native(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString('base64');
}

/** 一个合法套件包：一级目录 plugin.json（DEFLATE）+ 两个技能文件 + 一条 SKILL.md */
export function demoPackage(name = 'admin-assist'): { zip: Uint8Array; zipBase64: string } {
  const manifest = JSON.stringify({
    name,
    version: 'v1.4.0',
    author: '@行政百宝箱',
    category: '行政',
    description: '会议纪要、通知公告与物资盘点清单',
    commands: [
      { name: 'meeting-minutes', description: '整理会议纪要' },
      'notice-draft',
    ],
    skills: ['doc-polish'],
    dataLinks: [{ name: 'okr' }],
  });
  const zip = buildZip([
    { path: '.qoder-plugin/plugin.json', content: manifest, deflate: true },
    { path: 'skills/doc-polish/SKILL.md', content: '# doc-polish\n润色既有文稿。\n' },
    { path: 'README.md', content: '# 使用说明\n' },
  ]);
  return { zip, zipBase64: bytesToBase64Native(zip) };
}
