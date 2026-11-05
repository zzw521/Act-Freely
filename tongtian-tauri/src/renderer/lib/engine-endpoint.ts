/**
 * 引擎 HTTP 端点解析（P1-5）
 *
 * 桌面端：向 Rust 询问当前工作空间的真实端口（`engine:endpoint`），
 * 第二个 workspace 的实例可能分配在 18001，前端不得再写死 127.0.0.1:18000。
 * 网页直连：没有多实例可查，使用 VITE_ENGINE_URL / 缺省 18000；
 * 生产构建由引擎静态托管时默认同源（容器/服务化无需再写死 localhost）。
 *
 * 只读动作（预览文件、打开配置目录）不在这里拉起引擎：引擎未运行时
 * 如实抛 `ENGINE_NOT_RUNNING`，由调用方展示原因。界面据此给出**用户主动**的
 * 「启动引擎并重试」入口（见 `components/FilePreview`），不在背后顺手起进程。
 */

import { IPC_CHANNELS } from '../../shared/contracts';
import type { EngineEndpoint } from '../../shared/contracts';
import { rawErrorText } from '../../shared/error-messages';
import { getActiveWorkspaceId } from '../store/workspaceStore';
import { rememberDesktopEngineToken } from './engine-http';

/**
 * 这个错误是不是「该工作空间的引擎没在跑」。
 *
 * 只按码前缀判，不碰文案：Rust 侧 `Err(String)` 到 JS 是裸字符串
 * `ENGINE_NOT_RUNNING: 工作空间「default」的引擎尚未启动`，而 `resolveEngineEndpoint`
 * 自己抛的是 Error 实例——`rawErrorText` 已把两种形态归一。
 * 调用方（预览面板）用它决定要不要给「启动引擎并重试」，别拿中文文案当分支条件。
 */
export function isEngineNotRunningError(err: unknown): boolean {
  return rawErrorText(err).startsWith('ENGINE_NOT_RUNNING');
}

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
  // 桌面壳注入的本机令牌：后续 engineFetch 直连引擎时自动携带
  rememberDesktopEngineToken(view.token);
  return { ...view, baseUrl: view.baseUrl.replace(/\/+$/, '') };
}

/** 解析指定（缺省为当前）工作空间的引擎基地址 */
export async function resolveEngineBaseUrl(workspaceId?: string): Promise<string> {
  return (await resolveEngineEndpoint(workspaceId)).baseUrl;
}

/** 正在进行的引擎启动（按 workspace 去重；命令本身也幂等，这里只消除并发窗口） */
let pendingStart: Promise<void> | null = null;
let pendingStartWs = '';

/**
 * 确保指定（缺省为当前）工作空间的引擎已启动。
 *
 * 只给两类地方用：数据只能由引擎进程提供、又不能等用户先发一条消息的页面（IM 频道），
 * 以及**用户主动点**的补救入口（产物预览失败时的「启动引擎并重试」）——
 * 只读动作本身仍然不在取数路径里顺手起进程，起不起由人决定。
 * 命令侧幂等：实例已在跑时直接复用，不重复起进程。
 * 浏览器直连模式由外部进程托管，这里不做启动，交给调用方如实报不可达。
 */
export async function ensureEngineStarted(workspaceId?: string): Promise<void> {
  if (!isTauriRuntime()) return;
  const api = typeof window !== 'undefined' ? window.tongtianAPI : undefined;
  if (!api) {
    throw new Error('IPC_CHANNEL_UNHANDLED: tongtianAPI 未注入，无法启动引擎');
  }
  const ws = workspaceId?.trim() || getActiveWorkspaceId();

  // 同一个工作空间的并发调用共用一次启动：React 开发态会双跑 effect，
  // 两个请求同时进 Rust 会各起一个进程（实例表要等进程起来才写入，早到的复用检查看不到）。
  if (pendingStart && pendingStartWs === ws) return pendingStart;
  const start = api
    .invoke(IPC_CHANNELS.ENGINE_START, ws)
    .then(() => undefined)
    .finally(() => {
      // 只清掉自己：不同 workspace 的启动可能并行，别把后一个的在途状态误清
      if (pendingStart === start) {
        pendingStart = null;
        pendingStartWs = '';
      }
    });
  pendingStart = start;
  pendingStartWs = ws;
  return start;
}
