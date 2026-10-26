/**
 * 引擎 HTTP 端点解析（P1-5）
 *
 * 桌面端：向 Rust 询问当前工作空间的真实端口（`engine:endpoint`），
 * 第二个 workspace 的实例可能分配在 18001，前端不得再写死 127.0.0.1:18000。
 * 网页直连：没有多实例可查，使用 VITE_ENGINE_URL / 缺省 18000；
 * 生产构建由引擎静态托管时默认同源（容器/服务化无需再写死 localhost）。
 *
 * 只读动作（预览文件、打开配置目录）不在这里拉起引擎：引擎未运行时
 * 如实抛 `ENGINE_NOT_RUNNING`，由调用方展示原因。
 */

import { IPC_CHANNELS } from '../../shared/contracts';
import type { EngineEndpoint } from '../../shared/contracts';
import { getActiveWorkspaceId } from '../store/workspaceStore';

/**
 * 解析网页直连地址（纯函数，便于离线验证）：
 * 1. 显式 VITE_ENGINE_URL 优先（空串表示同源相对路径）；
 * 2. 生产构建未配置时用页面 origin，兼容静态托管与容器；
 * 3. 开发/测试仍回落到引擎缺省端口。
 */
export function resolveEngineDirectUrl(args: {
  configured?: string;
  dev: boolean;
  origin?: string;
}): string {
  const configured = (args.configured ?? '').trim();
  if (configured) return configured.replace(/\/+$/, '');
  if (args.configured !== undefined && args.origin) return args.origin;
  if (!args.dev && args.origin && /^https?:\/\//i.test(args.origin)) return args.origin;
  return 'http://127.0.0.1:18000';
}

const pageOrigin = typeof window !== 'undefined' ? window.location?.origin : undefined;

/** 网页直连引擎地址（VITE_ENGINE_URL 可覆盖；生产构建默认同源） */
export const ENGINE_DIRECT_URL: string = resolveEngineDirectUrl({
  configured: import.meta.env.VITE_ENGINE_URL,
  dev: Boolean(import.meta.env.DEV),
  origin: pageOrigin,
});

function isTauriRuntime(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

function portOf(baseUrl: string): number {
  try {
    const url = new URL(baseUrl);
    return Number(url.port) || (url.protocol === 'https:' ? 443 : 80);
  } catch {
    return 0;
  }
}

/** 解析指定（缺省为当前）工作空间的引擎端点 */
export async function resolveEngineEndpoint(workspaceId?: string): Promise<EngineEndpoint> {
  const ws = workspaceId?.trim() || getActiveWorkspaceId();
  if (!isTauriRuntime()) {
    const baseUrl = ENGINE_DIRECT_URL.replace(/\/+$/, '');
    return { workspaceId: ws, baseUrl, port: portOf(baseUrl), status: 'ready' };
  }
  const api = typeof window !== 'undefined' ? window.tongtianAPI : undefined;
  if (!api) {
    throw new Error('IPC_CHANNEL_UNHANDLED: tongtianAPI 未注入，无法解析引擎端点');
  }
  const view = (await api.invoke(IPC_CHANNELS.ENGINE_ENDPOINT, ws)) as EngineEndpoint | null;
  if (!view || typeof view.baseUrl !== 'string' || !view.baseUrl) {
    throw new Error('ENGINE_NOT_RUNNING: Rust 未返回可用的引擎端点');
  }
  return { ...view, baseUrl: view.baseUrl.replace(/\/+$/, '') };
}

/** 解析指定（缺省为当前）工作空间的引擎基地址 */
export async function resolveEngineBaseUrl(workspaceId?: string): Promise<string> {
  return (await resolveEngineEndpoint(workspaceId)).baseUrl;
}
