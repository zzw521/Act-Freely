/**
 * file:preview 正文提取 · 契约与三通道接线测试（任务 #19）
 *
 * 锁定四层，任一侧漂移即失败：
 * 1. Rust `file::office`：手写 ZIP（STORE/DEFLATE + CRC 校验）、OOXML 段落/表格/幻灯片抽取、
 *    PDF 页数与文档信息、体积上限、失败一律带 FILE_CONVERSION_FAILED 原因（绝不静默空正文）
 * 2. Rust `file::service`：非文本预览走抽取、附件敏感度按正文判定（不再只看文件名）
 * 3. 渲染层三通道：tauri-api 真命令 / Mock 与引擎直连如实说明能力边界（不再有「下批落地」）
 * 4. 错误码 parity：Rust 抛出的 FILE_* 码在 ErrorCode 与文案表均已登记
 *
 * 另走一遍真实分发：Mock 通道（window.tongtianAPI → fileAPI.preview）与
 * 引擎直连通道（stub fetch → engineAPI.invoke('file:preview')），确认页面拿到的是
 * 「有正文 / 明确拒绝原因」，而不是假正文。
 */

import { describe, it, expect, beforeAll, afterEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ErrorCode } from '../../../src/shared/contracts';
import { errorMessageOf } from '../../../src/shared/error-messages';

// node 测试环境无 window/localStorage，本地权威层（安全/技能/MCP…）在导入期即访问存储
(globalThis as Record<string, unknown>).window =
  (globalThis as Record<string, unknown>).window ?? {};
const kv = new Map<string, string>();
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => kv.get(k) ?? null,
  setItem: (k: string, v: string) => void kv.set(k, v),
  removeItem: (k: string) => void kv.delete(k),
  clear: () => kv.clear(),
};
(globalThis as Record<string, unknown>).window.localStorage = (globalThis as Record<string, unknown>).localStorage;

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
}

const rustMod = () => read('../../../src-tauri/src/file/mod.rs');
const rustOffice = () => read('../../../src-tauri/src/file/office.rs');
const rustService = () => read('../../../src-tauri/src/file/service.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const engineApi = () => read('../../../src/renderer/lib/engine-api.ts');
const libApi = () => read('../../../src/renderer/lib/api.ts');
const filePanel = () => read('../../../src/renderer/pages/FilePanel/index.tsx');

// ── 1. Rust 侧：正文抽取契约 ────────────────────────────────

describe('Rust file::office：容器解析与诚实性', () => {
  it('office 模块已挂到 file 命名空间（否则 service 调不到）', () => {
    expect(rustMod()).toContain('pub mod office;');
  });

  it('ZIP 读取同时支持 STORE 与 DEFLATE，并校验 CRC', () => {
    const src = rustOffice();
    expect(src).toContain('SIG_EOCD');
    expect(src).toContain('crc32fast::hash');
    expect(src).toContain('DeflateDecoder::new(raw)');
    expect(src).toContain('CRC 不匹配');
    // 不依赖 zip crate：解压失败也要给出原因
    expect(src).toContain('压缩包解压失败');
  });

  it('对不可信压缩包设上限，防解压炸弹与越界读', () => {
    const src = rustOffice();
    expect(src).toContain('const MAX_ENTRY_BYTES');
    expect(src).toContain('拒绝解压');
    expect(src).toContain('压缩包数据越界');
    expect(src).toContain('压缩包本地头损坏');
  });

  it('三种 OOXML 各有抽取路径，表格按行拼接', () => {
    const src = rustOffice();
    for (const fn of ['fn docx_text', 'fn xlsx_text', 'fn pptx_text', 'fn pdf_preview']) {
      expect(src).toContain(fn);
    }
    expect(src).toContain('"w:tc"');
    expect(src).toContain('shared_strings');
    expect(src).toContain('slide_no');
  });

  it('失败与不支持必须显式说明，禁止空正文冒充成功', () => {
    const src = rustOffice();
    expect(src).toContain('FILE_CONVERSION_FAILED: {}');
    expect(src).toContain('旧版二进制 Office 格式');
    expect(src).toContain('不支持预览的格式');
    expect(src).toContain('MAX_PREVIEW_CHARS');
    expect(src).toContain('预览仅展示前');
    // supported 只由正文是否为真决定
    expect(src).toContain('let supported = !content.trim().is_empty();');
  });
});

describe('Rust file::service：预览与附件分级接线', () => {
  it('非文本文件走正文抽取，且有大小上限', () => {
    const src = rustService();
    expect(src).toContain('fn preview_document');
    expect(src).toContain('office::extract');
    expect(src).toContain('const MAX_PREVIEW_BYTES');
    expect(src).toContain('FILE_TOO_LARGE');
  });

  it('附件敏感度按抽取正文判定（二进制不再只看文件名）', () => {
    const src = rustService();
    expect(src).toContain('fn classify_sample');
    expect(src).toContain('classify_sample(&dest, mime)');
    expect(src).toContain('super::office::extract(mime, &bytes)');
    expect(src).toContain('SAMPLE_CHARS');
  });

  it('mime_of 覆盖 pptx 与旧版格式，供 Rust 侧分流', () => {
    const src = rustService();
    for (const ext of ['"pptx"', '"docx"', '"xlsx"', '"pdf"', '"doc"', '"xls"']) {
      expect(src).toContain(ext);
    }
  });

  it('command 注册齐全（file_preview 在 generate_handler 列表内）', () => {
    const src = rustLib();
    for (const cmd of ['file_list,', 'file_read,', 'file_preview,', 'file_stage_attachment']) {
      expect(src).toContain(cmd);
    }
  });
});

// ── 2. 渲染层三通道接线 ────────────────────────────────────

describe('渲染层三通道：file:preview 不再有假实现', () => {
  it('tauri-api 三个文件通道映射到真实 command 与 camelCase 入参', () => {
    const src = tauriApi();
    expect(src).toContain("invoke('file_list',");
    expect(src).toContain("invoke('file_read',");
    expect(src).toContain("invoke('file_preview',");
    expect(src).toContain('filePaths: args[0] as string[]');
  });

  it('Mock 通道不再声明「下批落地」，改为如实标注通道能力', () => {
    const src = mockApi();
    expect(src).not.toContain('下批落地');
    expect(src).toContain('FILE_UNSUPPORTED');
    expect(src).toContain('supported: true');
  });

  it('引擎直连通道路由 file:* 通道，不再静默返回空列表', () => {
    const src = engineApi();
    expect(src).toContain('async function handleEngineFileChannel');
    expect(src).toContain("channel === 'file:read' || channel === 'file:preview' || channel === 'file:browse'");
    expect(src).toContain('fetchEngineFile');
    expect(src).not.toContain('下批落地');
  });

  it('api.ts 的 preview 返回 FilePreview（含 supported/note）', () => {
    const src = libApi();
    expect(src).toContain('FilePreview');
    expect(src).toContain("invoke('file:preview', filePaths, workspaceId)");
  });

  it('文件页真实渲染正文与拒绝原因，面包屑可逐级回退', () => {
    const src = filePanel();
    expect(src).not.toContain('下批落地');
    expect(src).toContain('errorTextOf(p.note');
    expect(src).toContain('FilePreview');
    // 面包屑保存完整路径而非只有名称，点击可回到该层级
    expect(src).toMatch(/pathStack.*\{ name: string; path: string \}/s);
    expect(src).toContain('loadDir(target.path)');
  });
});

// ── 3. 错误码 parity ───────────────────────────────────────

describe('错误码 parity：Rust 抛出的 FILE_* 码前端均已登记', () => {
  const codes = [
    ErrorCode.FILE_NOT_FOUND,
    ErrorCode.FILE_ACCESS_DENIED,
    ErrorCode.FILE_READ_FAILED,
    ErrorCode.FILE_WRITE_FAILED,
    ErrorCode.FILE_CONVERSION_FAILED,
    ErrorCode.FILE_TOO_LARGE,
    ErrorCode.FILE_BACKUP_FAILED,
    ErrorCode.FILE_UNSUPPORTED,
  ];

  it.each(codes.map((c) => [c as string, c]))('%s 有用户文案', (_name, code) => {
    expect(errorMessageOf(code)).not.toContain('发生错误（');
  });

  it('Rust 源码抛出的 FILE_* 码全部在 ErrorCode 枚举内', () => {
    const emitted = new Set<string>();
    for (const src of [rustOffice(), rustService(), rustLib()]) {
      for (const m of src.matchAll(/(?<=")FILE_[A-Z][A-Z_]*/g)) emitted.add(m[0]);
    }
    expect(emitted.size).toBeGreaterThan(0);
    const known = new Set<string>(Object.values(ErrorCode));
    for (const code of emitted) expect(known.has(code)).toBe(true);
  });
});

// ── 4. 真实分发 ────────────────────────────────────────────

describe('Mock 通道真实分发：fileAPI.preview 形状与拒绝语义', () => {
  let fileAPI: typeof import('../../../src/renderer/lib/api').fileAPI;

  beforeAll(async () => {
    (globalThis as Record<string, unknown>).window =
      (globalThis as Record<string, unknown>).window ?? {};
    const store = new Map<string, string>();
    (globalThis as Record<string, unknown>).localStorage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    };
    vi.resetModules();
    await import('../../../src/renderer/lib/mock-api');
    const mod = await import('../../../src/renderer/lib/api');
    fileAPI = mod.fileAPI;
  });

  it('文本类给真实正文并标记 supported', async () => {
    const [p] = await fileAPI.preview(['notes/示例.md']);
    expect(p.supported).toBe(true);
    expect(p.content).toContain('示例');
    expect(p.note).toBeUndefined();
  });

  it('Office/PDF 不编造正文，note 指向桌面版', async () => {
    const [p] = await fileAPI.preview(['report.docx']);
    expect(p.supported).toBe(false);
    expect(p.content).toBe('');
    expect(p.note).toContain('FILE_UNSUPPORTED');
  });

  it('目录浏览在 Mock 通道仍返回可渲染条目', async () => {
    const list = await fileAPI.list('');
    expect(Array.isArray(list)).toBe(true);
    expect(list.length).toBeGreaterThan(0);
  });
});

describe('引擎直连通道真实分发：stub fetch 后的 file:preview', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const stub = (handler: (url: string) => { ok: boolean; status: number; body: unknown }) => {
    vi.stubGlobal('fetch', vi.fn(async (input: unknown) => {
      const url = String(input);
      const r = handler(url);
      return { ok: r.ok, status: r.status, json: async () => r.body };
    }));
  };

  it('引擎能读的文本 → supported true 且带真实正文', async () => {
    stub(() => ({ ok: true, status: 200, body: { ok: true, content: '# 周报\n正文', mime: 'text/markdown' } }));
    const { engineAPI } = await import('../../../src/renderer/lib/engine-api');
    const rows = (await engineAPI.invoke('file:preview', ['a.md'])) as Array<{
      supported: boolean;
      content: string;
      note?: string;
    }>;
    expect(rows[0]).toMatchObject({ path: 'a.md', supported: true });
    expect(rows[0].content).toContain('周报');
  });

  it('引擎返回二进制拒绝 → note 带 FILE_CONVERSION_FAILED 与原因', async () => {
    stub((url) => ({
      ok: false,
      status: 415,
      body: { ok: false, message: '该文件是二进制，引擎侧无法解析正文' },
    }));
    const { engineAPI } = await import('../../../src/renderer/lib/engine-api');
    const rows = (await engineAPI.invoke('file:preview', ['x.pdf'])) as Array<{
      supported: boolean;
      note: string;
    }>;
    expect(rows[0].supported).toBe(false);
    expect(rows[0].note).toContain('FILE_CONVERSION_FAILED');
    expect(rows[0].note).toContain('桌面版');
  });

  it('引擎侧 HTTP 失败且无 message → 抛 FILE_READ_FAILED，不静默空正文', async () => {
    stub(() => ({ ok: false, status: 500, body: {} }));
    const { engineAPI } = await import('../../../src/renderer/lib/engine-api');
    await expect(engineAPI.invoke('file:read', ['boom.txt'])).rejects.toThrow(/FILE_READ_FAILED/);
  });

  it('file:browse 引擎无接口 → 抛 FILE_UNSUPPORTED（不伪装空目录）', async () => {
    stub(() => ({ ok: true, status: 200, body: {} }));
    const { engineAPI } = await import('../../../src/renderer/lib/engine-api');
    await expect(engineAPI.invoke('file:browse', [''])).rejects.toThrow(/FILE_UNSUPPORTED/);
  });
});
