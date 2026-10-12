/**
 * 专家套件 · 套件包（.zip）解析
 *
 * 浏览器通道（Mock / 引擎直连）没有 Rust 的解包能力，但上传的仍是同一个 .zip 包，
 * 因此这里按 ZIP 规范自行读取：EOCD → 中央目录 → 本地头 → STORE 直取 / DEFLATE 解压。
 * 与 Rust `suite::install_in` 保持同一判定口径：
 * - plugin.json 必须位于根目录或单层目录内，且含非空 name；
 * - 标识归一规则同源（ASCII 名转 slug，非 ASCII 名用 CRC-32 哈希）；
 * - 只认相对路径，绝对路径 / 盘符 / `..` 一律拒绝；
 * - 差异：本模块不校验条目 CRC（桌面端由 Rust 校验），解析失败即拒绝安装。
 */

import type { InstalledSuite, SuiteCommand } from '../../shared/contracts';

/** plugin.json 体积上限（与 Rust 同档，256KB） */
const MAX_MANIFEST_BYTES = 256 * 1024;
/** 套件包条目数上限（与 Rust 同档） */
const MAX_ENTRIES = 256;
/** 整包解压后上限（与 Rust 同档，32MB） */
const MAX_TOTAL_BYTES = 32 * 1024 * 1024;
/** 套件标识最大长度（与 Rust 同档） */
const MAX_ID_LEN = 64;

export interface ParsedSuitePackage {
  suiteId: string;
  /** plugin.json 归一后的套件视图（path/installedAtMs 由台账补全） */
  suite: InstalledSuite;
  /** 包内相对路径清单（安装态台账据此展示文件数） */
  fileNames: string[];
}

/** 套件包解析失败（错误码与 Rust 侧同名，前端文案可统一处理） */
export class SuitePackageError extends Error {}

function incompatible(reason: string): never {
  throw new SuitePackageError(`SUITE_INCOMPATIBLE: ${reason}`);
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

/** 标准 CRC-32（与 crc32fast 同多项式，跨语言得到同一哈希） */
export function crc32(bytes: Uint8Array): number {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i += 1) {
    c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  }
  return (c ^ 0xffffffff) >>> 0;
}

/** 展示名 → 套件标识（与 Rust `suite::suite_id_of` 同规则，含 64 字符上限回退哈希） */
export function suiteIdOf(rawName: string): string {
  const name = rawName.trim();
  if (/^[\x00-\x7f]*$/.test(name)) {
    const slug = slugifyAscii(name);
    if (slug && slug.length <= MAX_ID_LEN) return slug;
  }
  return `suite-${crc32(textToBytes(name)).toString(16).padStart(8, '0')}`;
}

/** 小写化，非 [a-z0-9_] 的连续字符归一为单个 -（与 Rust `skill::slugify` 同算法） */
function slugifyAscii(raw: string): string | null {
  let out = '';
  let pendingDash = false;
  for (const c of raw.trim()) {
    if (/^[a-z0-9_]$/.test(c)) {
      if (pendingDash) {
        out += '-';
        pendingDash = false;
      }
      out += c;
    } else if (/^[A-Z]$/.test(c)) {
      if (pendingDash) {
        out += '-';
        pendingDash = false;
      }
      out += c.toLowerCase();
    } else {
      pendingDash = out.length > 0;
    }
  }
  const id = out.replace(/^-+/, '').replace(/-+$/, '');
  if (!id || id.length > MAX_ID_LEN) return null;
  return id;
}

function textToBytes(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

/** File/base64 → 字节 */
export function base64ToBytes(base64: string): Uint8Array {
  const body = base64.trim().slice(base64.lastIndexOf(',') + 1).replace(/[\r\n\s]/g, '');
  if (!body) incompatible('未收到套件包内容');
  let binary: string;
  try {
    binary = atob(body);
  } catch {
    return incompatible('套件包 base64 解码失败');
  }
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

export function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  // 分块避免超长参数（浏览器/Node 的 String.fromCharCode 实参上限）
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

interface Entry {
  name: string;
  method: number;
  compSize: number;
  uncompSize: number;
  localOffset: number;
}

function u16(view: DataView, at: number): number {
  return view.getUint16(at, true);
}

function u32(view: DataView, at: number): number {
  return view.getUint32(at, true);
}

/** 中央目录 → 条目清单（拒绝 ZIP64 与损坏目录） */
function readEntries(bytes: Uint8Array): Entry[] {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (bytes.length < 22) incompatible('文件过小，不是有效的套件包（.zip）');
  const floor = Math.max(0, bytes.length - (22 + 0xffff));
  let eocd = -1;
  for (let i = bytes.length - 22; i >= floor; i -= 1) {
    if (u32(view, i) === 0x06054b50) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) incompatible('未找到压缩包目录（EOCD），文件可能已损坏或不完整');
  const count = u16(view, eocd + 10);
  const cdOffset = u32(view, eocd + 16);
  if (cdOffset === 0xffffffff || count === 0xffff) incompatible('暂不支持 ZIP64 套件包');
  if (count === 0) incompatible('套件包内没有任何文件');
  if (count > MAX_ENTRIES) incompatible(`套件包条目过多（${count} 项，上限 ${MAX_ENTRIES}）`);

  const entries: Entry[] = [];
  let p = cdOffset;
  const decoder = new TextDecoder('utf-8', { fatal: false });
  for (let k = 0; k < count; k += 1) {
    if (p + 46 > bytes.length || u32(view, p) !== 0x02014b50) {
      incompatible(`套件包目录损坏（第 ${k + 1} 项偏移 ${p} 无效）`);
    }
    const method = u16(view, p + 10);
    const compSize = u32(view, p + 20);
    const uncompSize = u32(view, p + 24);
    const nameLen = u16(view, p + 28);
    const extraLen = u16(view, p + 30);
    const commentLen = u16(view, p + 32);
    const localOffset = u32(view, p + 42);
    if (p + 46 + nameLen > bytes.length) incompatible('套件包目录损坏（文件名越界）');
    const name = decoder
      .decode(bytes.subarray(p + 46, p + 46 + nameLen))
      .replace(/\\/g, '/');
    if (localOffset + 30 > bytes.length || u32(view, localOffset) !== 0x04034b50) {
      incompatible(`套件包本地头损坏（偏移 ${localOffset}）`);
    }
    const lNameLen = u16(view, localOffset + 26);
    const lExtraLen = u16(view, localOffset + 28);
    entries.push({
      name,
      method,
      compSize,
      uncompSize,
      localOffset: localOffset + 30 + lNameLen + lExtraLen,
    });
    p = p + 46 + nameLen + extraLen + commentLen;
  }
  return entries;
}

async function readEntry(bytes: Uint8Array, entry: Entry): Promise<Uint8Array> {
  const start = entry.localOffset;
  const end = start + entry.compSize;
  if (end > bytes.length) incompatible(`套件包内 ${entry.name} 数据越界`);
  const raw = bytes.subarray(start, end);
  if (entry.method === 0) return raw;
  if (entry.method !== 8) incompatible(`暂不支持的压缩算法（method=${entry.method}）：${entry.name}`);
  if (typeof DecompressionStream === 'undefined') {
    incompatible('当前运行环境无法解压 DEFLATE 条目，请在桌面端安装套件');
  }
  if (entry.uncompSize > MAX_TOTAL_BYTES) incompatible(`套件包内 ${entry.name} 解压后超过上限`);
  const stream = new Blob([raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength) as ArrayBuffer])
    .stream()
    .pipeThrough(new DecompressionStream('deflate-raw'));
  const buf = await new Response(stream).arrayBuffer();
  const out = new Uint8Array(buf);
  if (out.length !== entry.uncompSize) {
    incompatible(`套件包内 ${entry.name} 解压后大小与目录不符`);
  }
  return out;
}

/** 相对路径是否可安全落盘（与 Rust `safe_relative` 同判定） */
function isSafeRelative(name: string): boolean {
  const normalized = name.replace(/\\/g, '/');
  if (!normalized || normalized.startsWith('/') || normalized.includes(':')) return false;
  const segs = normalized.split('/').filter((s) => s.length > 0);
  if (!segs.length) return false;
  return segs.every((s) => s !== '..');
}

/** 定位 plugin.json：根目录或单层目录内（与页面「格式要求」同源） */
function findManifestEntry(entries: Entry[]): Entry | undefined {
  return entries.find((e) => {
    const segs = e.name.replace(/^\.\//, '').split('/').filter((s) => s.length > 0);
    return segs.length <= 2 && segs[segs.length - 1]?.toLowerCase() === 'plugin.json';
  });
}

function strField(obj: Record<string, unknown>, key: string): string | undefined {
  const v = obj[key];
  if (typeof v !== 'string') return undefined;
  const t = v.trim();
  return t ? t : undefined;
}

/** commands 兼容三种写法（与 Rust `parse_commands` 同口径） */
function parseCommands(raw: Record<string, unknown>): SuiteCommand[] {
  const out: SuiteCommand[] = [];
  const v = raw.commands;
  if (Array.isArray(v)) {
    for (const it of v) {
      if (typeof it === 'string') {
        const name = it.trim();
        if (name) out.push({ name, description: null });
      } else if (it && typeof it === 'object') {
        const name = strField(it as Record<string, unknown>, 'name');
        if (name) {
          const desc = (it as Record<string, unknown>).description;
          out.push({ name, description: typeof desc === 'string' ? desc : null });
        }
      }
    }
  } else if (v && typeof v === 'object') {
    for (const [name, desc] of Object.entries(v as Record<string, unknown>)) {
      const key = name.trim();
      if (key) out.push({ name: key, description: typeof desc === 'string' ? desc : null });
    }
  }
  return out;
}

function countDeclared(raw: Record<string, unknown>, key: string): number {
  const v = raw[key];
  if (!Array.isArray(v)) return 0;
  return v.filter((it) => {
    if (typeof it === 'string') return it.trim().length > 0;
    if (it && typeof it === 'object') return typeof (it as Record<string, unknown>).name === 'string';
    return false;
  }).length;
}

/**
 * 解析套件包：校验 plugin.json 并产出与 Rust 同口径的已安装视图骨架。
 * 解析失败抛 `SuitePackageError`（SUITE_INCOMPATIBLE / SUITE_NOT_FOUND 前缀）。
 */
export async function parseSuitePackage(bytes: Uint8Array): Promise<ParsedSuitePackage> {
  const entries = readEntries(bytes);
  const manifestEntry = findManifestEntry(entries);
  if (!manifestEntry) incompatible('套件包缺少 plugin.json，应位于根目录或单层目录内');
  const manifestBytes = await readEntry(bytes, manifestEntry!);
  if (manifestBytes.length > MAX_MANIFEST_BYTES) incompatible('plugin.json 超过 256KB 上限');
  let manifest: Record<string, unknown>;
  try {
    manifest = JSON.parse(new TextDecoder().decode(manifestBytes)) as Record<string, unknown>;
  } catch (e) {
    return incompatible(`plugin.json 不是合法 JSON：${(e as Error).message}`);
  }
  const name = strField(manifest, 'name');
  if (!name) incompatible('plugin.json 缺少非空的 name 字段');

  const files = entries.filter((e) => !e.name.endsWith('/'));
  for (const e of files) {
    if (!isSafeRelative(e.name)) incompatible(`套件包内含不安全路径：${e.name}`);
  }
  const skillFiles = files.filter((e) => e.name.split('/').pop() === 'SKILL.md').length;
  const id = suiteIdOf(name!);
  const suite: InstalledSuite = {
    id,
    name: name!,
    version: strField(manifest, 'version') ?? '未标注版本',
    author: strField(manifest, 'author') ?? null,
    category: strField(manifest, 'category') ?? null,
    description: strField(manifest, 'description') ?? null,
    commands: parseCommands(manifest),
    skillCount: Math.max(countDeclared(manifest, 'skills'), skillFiles),
    dataLinkCount: countDeclared(manifest, 'dataLinks'),
    // 台账目录含归一后的 manifest.json，文件数与桌面端口径一致
    fileCount: files.length + 1,
    installedAtMs: 0,
    path: '',
    readable: true,
    enabled: true,
  };
  return { suiteId: id, suite, fileNames: files.map((e) => e.name) };
}

/** base64 套件包 → 已安装视图骨架（浏览器通道安装入口） */
export async function parseSuitePackageFromBase64(zipBase64: string): Promise<ParsedSuitePackage> {
  return parseSuitePackage(base64ToBytes(zipBase64));
}
