/**
 * 零配置本地模型（Ollama）契约测试
 *
 * 这条能力最容易变成「按钮点了、界面说已连接，其实没探测」：
 * - Rust：只接受回环地址，真实请求 /api/tags；连不上/对面不是 Ollama 时 available=false
 * - 引擎直连：同一口径，由引擎代探，强制不走代理
 * - 通道：桌面端走 Tauri command，浏览器直连走引擎，Mock 如实说不支持
 * - 前端：检测结果分三态展示；添加时写入 ollama 来源、本地免密钥、重复项不重复写
 */

import { beforeAll, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const rustLocal = () => read('../../../src-tauri/src/model/local.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const engine = () => read('../../../engine/server.py');
const types = () => read('../../../src/shared/types.ts');
const contracts = () => read('../../../src/shared/contracts.ts');
const errorMessages = () => read('../../../src/shared/error-messages.ts');
const tauri = () => read('../../../src/renderer/lib/tauri-api.ts');
const engineApi = () => read('../../../src/renderer/lib/engine-api.ts');
const mock = () => read('../../../src/renderer/lib/mock-api.ts');
const component = () => read('../../../src/renderer/components/LocalModelDetect/index.tsx');
const settings = () => read('../../../src/renderer/components/ModelSettings/index.tsx');
const hub = () => read('../../../src/renderer/pages/ModelHub/index.tsx');

describe('Rust：只探测本机回环，失败如实返回', () => {
  it('探测地址只允许回环，默认 11434，可带 /v1', () => {
    const src = rustLocal();
    expect(src).toContain('DEFAULT_OLLAMA_BASE');
    expect(src).toContain('"http://127.0.0.1:11434"');
    expect(src).toContain('"localhost" | "127.0.0.1" | "::1"');
    expect(src).toContain('LOCAL_MODEL_INVALID');
    expect(src).toContain('自动检测只支持本机回环地址');
  });

  it('真实请求 /api/tags，超时 3s，强制直连', () => {
    const src = rustLocal();
    expect(src).toContain('format!("{root}/api/tags")');
    expect(src).toContain('PROBE_TIMEOUT: Duration = Duration::from_secs(3)');
    expect(src).toContain('crate::proxy::loopback_client()');
    expect(src).toContain('LOCAL_MODEL_NOT_RUNNING');
  });

  it('models 字段缺失不算 Ollama；不可达不伪报 available', () => {
    const src = rustLocal();
    expect(src).toContain('models: Option<Vec<OllamaTag>>');
    expect(src).toContain('返回内容没有 models 数组');
    expect(src).toContain('available: false');
    expect(src).toMatch(/Ok\(models\)\s*=>\s*\{\s*resp\.available = true;/);
  });

  it('command 已注册到 invoke_handler', () => {
    expect(rustLib()).toContain('async fn model_detect_local');
    expect(rustLib()).toContain('model_detect_local,');
    expect(rustLib()).toContain('model::local::detect');
  });
});

describe('引擎直连：同一口径的真实探测', () => {
  it('只接受回环 + 根或 /v1，拒绝远端与无关路径', () => {
    const src = engine();
    expect(src).toContain('OLLAMA_DEFAULT_BASE');
    expect(src).toContain('_OLLAMA_LOOPBACK_HOSTS');
    expect(src).toContain('自动检测只支持本机回环地址');
    expect(src).toContain('地址只接受服务根或 /v1 结尾');
  });

  it('强制不走代理，真实 GET /api/tags，失败带原因', () => {
    const src = engine();
    expect(src).toContain('urllib.request.ProxyHandler({})');
    expect(src).toContain('f"{root}/api/tags"');
    expect(src).toContain('LOCAL_MODEL_NOT_RUNNING');
    expect(src).toContain('返回内容没有 models 数组');
  });

  it('端点存在且在线程池执行阻塞探测', () => {
    const src = engine();
    expect(src).toContain('@app.post("/api/models/detect-local")');
    expect(src).toContain('asyncio.to_thread(_probe_ollama_root, root)');
    expect(src).toContain('_model_error_response(err, status_code=400)');
  });
});

describe('跨端契约：类型、通道与错误码', () => {
  it('共享类型描述 supported/available 两种失败语义', () => {
    const src = types();
    expect(src).toContain('interface LocalDetectResp');
    for (const field of [
      'supported: boolean',
      'available: boolean',
      'endpoint: string',
      'openaiBaseUrl: string',
      'latencyMs: number',
      'models: LocalModelInfo[]',
      'error?: string',
    ]) {
      expect(src).toContain(field);
    }
  });

  it('IPC 通道与错误码已登记，文案不裸奔', () => {
    expect(contracts()).toContain("MODEL_DETECT_LOCAL: 'model:detectLocal'");
    for (const code of [
      'LOCAL_MODEL_UNSUPPORTED',
      'LOCAL_MODEL_NOT_RUNNING',
      'LOCAL_MODEL_INVALID',
    ]) {
      expect(contracts()).toContain(code);
      expect(errorMessages()).toContain(`[ErrorCode.${code}]`);
    }
  });

  it('Tauri / 引擎直连 / Mock 三通道都有映射', () => {
    expect(tauri()).toContain("invoke('model_detect_local', { baseUrl:");
    expect(engineApi()).toContain('/api/models/detect-local');
    expect(mock()).toContain("if (channel === 'model:detectLocal')");
    expect(mock()).toContain('LOCAL_MODEL_UNSUPPORTED');
    expect(mock()).toContain('supported: false');
  });
});

describe('前端：三态展示与一键加入模型清单', () => {
  it('检测结果区分不支持 / 未运行 / 已连接', () => {
    const src = component();
    expect(src).toContain('!result.supported');
    expect(src).toContain('!result.available');
    expect(src).toContain('result.available &&');
    expect(src).toContain('当前运行通道无法探测本机 Ollama');
    expect(src).toContain('请确认 Ollama 已安装并正在运行');
    expect(src).toContain('Ollama 已运行，但还没有下载模型');
  });

  it('添加时写入 ollama 来源、免密钥、本地标记，默认位随清单空否决定', () => {
    const src = component();
    expect(src).toContain('api.model.detectLocal()');
    expect(src).toContain('source: ModelSource.OLLAMA');
    expect(src).toContain('baseUrl: result.openaiBaseUrl');
    expect(src).toContain('modelName: model.name');
    expect(src).toContain('forceLocal: true');
    expect(src).toContain('isDefault: becomesDefault');
    expect(src).not.toMatch(/apiKey:\s*['"]/);
  });

  it('重复项标记已添加，不重复写入', () => {
    const src = component();
    expect(src).toContain('function isAdded');
    expect(src).toContain('const added = isAdded(existing, result.openaiBaseUrl, m.name);');
    expect(src).toContain('label="已添加"');
  });

  it('设置页与模型中心都挂载了检测面板', () => {
    expect(settings()).toContain('<LocalModelDetect existing={models} onAdded={refresh} />');
    expect(hub()).toContain('<LocalModelDetect existing={rawModels} onAdded={loadModels} />');
  });
});

// ── 行为：Mock 通道不假装探测过本机 ─────────────────────────────

(globalThis as Record<string, unknown>).window =
  (globalThis as Record<string, unknown>).window ?? {};
const win = globalThis.window as {
  tongtianAPI?: { invoke: (channel: string, ...args: unknown[]) => Promise<unknown> };
};

beforeAll(async () => {
  await import('../../../src/renderer/lib/mock-api');
});

describe('Mock 通道', () => {
  it('detectLocal 返回 supported=false 与明确原因', async () => {
    const resp = (await win.tongtianAPI!.invoke('model:detectLocal', '')) as {
      supported: boolean;
      available: boolean;
      models: unknown[];
      error?: string;
    };
    expect(resp.supported).toBe(false);
    expect(resp.available).toBe(false);
    expect(resp.models).toEqual([]);
    expect(resp.error).toContain('LOCAL_MODEL_UNSUPPORTED');
  });
});
