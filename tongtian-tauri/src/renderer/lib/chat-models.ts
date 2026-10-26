/**
 * 对话模型清单：输入框与设置页共用同一份数据源
 *
 * 三种运行通道各自有权威方，但渲染层只认 `api.model.list()` 一个入口：
 * - 桌面端：Rust SQLite + keyring（`model:*` command）
 * - 浏览器直连：引擎 `models.json`（`/api/models`）
 * - Mock：页面内存台账
 *
 * 清单里永远没有明文 apiKey（只有 `hasApiKey`）。对话提交只传 modelId，
 * 由权威方在服务端解析端点与密钥；这里额外缓存 baseUrl 仅用于提交前的
 * 合规出域判定（非机密数据，且判定必须与真实调用同源）。
 */

import type { ModelConfig } from '../../shared/types';
import { api } from './api';
import { isTauri } from './tauri-api';
import { resolveEngineBaseUrl } from './engine-endpoint';
import { engineFetch } from './engine-http';

export type ModelStoreKind = 'desktop' | 'engine' | 'mock';

/** 当前模型配置的权威数据源（仅用于界面如实说明来源） */
export function modelStoreKind(): ModelStoreKind {
  if (isTauri()) return 'desktop';
  return import.meta.env.VITE_MOCK === 'false' ? 'engine' : 'mock';
}

export interface ModelStoreInfo {
  kind: ModelStoreKind;
  title: string;
  detail: string;
  /** 引擎直连时的真实配置文件路径（桌面端密钥在系统密钥库，没有可展示的文件） */
  path?: string;
}

/**
 * 数据源说明：设置页如实告诉用户模型与密钥存在哪里。
 * Mock 通道只存在内存，刷新即回到初始台账，不能宣称已持久化。
 */
export async function modelStoreInfo(): Promise<ModelStoreInfo> {
  const kind = modelStoreKind();
  if (kind === 'desktop') {
    return {
      kind,
      title: '本机 SQLite + 系统密钥库',
      detail: '模型条目存在本机数据库，API Key 只写入系统密钥库；读取接口仅返回「是否已配置」。',
    };
  }
  if (kind === 'mock') {
    return {
      kind,
      title: 'Mock 内存台账',
      detail: '当前为前端 Mock 模式，模型只存在页面内存，刷新后恢复初始数据，不会写入磁盘。',
    };
  }
  let path = '';
  try {
    const baseUrl = await resolveEngineBaseUrl();
    const resp = await engineFetch(`${baseUrl}/api/models`);
    const json = (await resp.json()) as { path?: unknown };
    if (typeof json?.path === 'string') path = json.path;
  } catch {
    // 引擎未启动时只给类别说明，不编造路径
  }
  return {
    kind,
    title: '引擎 models.json',
    detail: '模型条目与 API Key 保存在本机模型配置文件，读取接口不回传明文密钥。',
    path: path || undefined,
  };
}

/** 本地端点（免密钥）：与 Rust/引擎的 is_local 判定保持同一口径 */
export function isLocalModelUrl(url?: string): boolean {
  const host = (url ?? '').trim().toLowerCase();
  return (
    host.startsWith('http://localhost') ||
    host.startsWith('http://127.0.0.1') ||
    host.startsWith('http://[::1]') ||
    host.startsWith('https://localhost') ||
    host.startsWith('https://127.0.0.1') ||
    host.startsWith('https://[::1]')
  );
}

/** 模型源标识 → 中文说明（与 Rust / 引擎的 source 枚举一致） */
export const MODEL_SOURCE_LABELS: Record<string, string> = {
  openai_compat: 'OpenAI 兼容',
  ollama: '本地 Ollama',
  preset_qwen: '通义千问',
  preset_deepseek: 'DeepSeek',
  preset_openai: 'OpenAI',
  custom: '自定义',
};

export function modelSourceLabel(source?: string): string {
  return MODEL_SOURCE_LABELS[source ?? ''] ?? source ?? '自定义';
}

/**
 * 输入框内置模型档位（与界面共用一份，避免提交与展示各写一套 binding）。
 * `auto` 故意不带 binding：由后端按默认模型解析。
 */
export interface BuiltinChatModel {
  id: string;
  name: string;
  desc: string;
  color: string;
  binding?: string;
}

export const BUILTIN_CHAT_MODELS: BuiltinChatModel[] = [
  { id: 'auto', name: 'Auto Mode', desc: '使用默认模型（未设置时按引擎配置）', color: '#6366F1' },
  { id: 'qwen-max', name: 'Qwen3.8-Max', desc: '千问最强模型', color: '#8B5CF6', binding: 'qwen3.8-max' },
  { id: 'advanced', name: '高级', desc: '擅长复杂任务', color: '#3B82F6', binding: 'qwen3.7-max' },
  { id: 'basic', name: '基础', desc: '日常任务够用', color: '#10B981', binding: 'qwen-plus' },
  { id: 'economy', name: '经济', desc: '适合简单任务', color: '#F59E0B', binding: 'qwen-flash' },
];

/**
 * 输入框选择 → 提交参数：Auto 不覆盖；内置档位传 binding；其余按已保存模型 id 传。
 * 重新生成时用当轮保存的选择回放，不能拿当前输入框的选择冒充当轮模型。
 */
export function chatModelSelectionOf(id?: string): { modelBinding?: string; modelId?: string } {
  const key = (id ?? '').trim();
  if (!key || key === 'auto') return {};
  const preset = BUILTIN_CHAT_MODELS.find((m) => m.id === key);
  if (preset) return preset.binding ? { modelBinding: preset.binding } : {};
  return { modelId: key };
}

let cache: ModelConfig[] = [];
let cacheAt = 0;
const CACHE_TTL_MS = 5000;

/** 读取模型清单（短缓存：输入框与合规判定共用，写操作后 invalidate） */
export async function loadChatModels(force = false): Promise<ModelConfig[]> {
  const now = Date.now();
  if (!force && cache.length > 0 && now - cacheAt < CACHE_TTL_MS) return cache;
  const list = await api.model.list();
  cache = Array.isArray(list) ? list : [];
  cacheAt = now;
  return cache;
}

/** 已缓存的模型（同步读；未命中返回 undefined） */
export function cachedModel(id?: string): ModelConfig | undefined {
  if (!id) return undefined;
  return cache.find((m) => m.id === id);
}

/**
 * 所选模型的接口地址（仅用于合规闸门的出域判定）。
 * 读不到清单时返回 undefined：闸门按「端点未知」只做分级留痕，不臆断出域。
 */
export async function modelBaseUrl(id?: string): Promise<string | undefined> {
  if (!id) return undefined;
  let hit = cachedModel(id);
  if (!hit) {
    try {
      await loadChatModels(true);
    } catch {
      return undefined;
    }
    hit = cachedModel(id);
  }
  return hit?.baseUrl?.trim() || undefined;
}

/** 模型 CRUD 后清缓存，下一轮对话读到最新清单 */
export function invalidateChatModels(): void {
  cache = [];
  cacheAt = 0;
}
