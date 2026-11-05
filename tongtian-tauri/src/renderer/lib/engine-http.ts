/**
 * 引擎 HTTP 鉴权（服务化模式）
 *
 * 桌面版由 Rust 直接连引擎，不经过本模块。服务化/容器模式设置
 * `TT_ENGINE_TOKEN` 后，所有 `/api/*` 请求都需要令牌；网页端从
 * `sessionStorage` 取，令牌只在当前标签页有效：
 * - 不写 localStorage，关闭标签页即失效；
 * - 不进构建产物、不进 URL；
 * - 收到 401 时通知界面让用户输入，而不是静默失败。
 *
 * 令牌只由部署方通过环境变量设置，前端不做默认值，也不尝试猜测。
 */

import { IPC_CHANNELS } from '../../shared/contracts';
import type { EngineEndpoint } from '../../shared/contracts';

const TOKEN_KEY = 'tt:engine-token';

function session(): Storage | null {
  try {
    return typeof sessionStorage !== 'undefined' ? sessionStorage : null;
  } catch {
    return null;
  }
}

export function isTauriRuntime(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

export function getEngineToken(): string {
  try {
    return (session()?.getItem(TOKEN_KEY) ?? '').trim();
  } catch {
    return '';
  }
}

export function setEngineToken(token: string): void {
  try {
    session()?.setItem(TOKEN_KEY, token.trim());
  } catch {
    /* 存储不可用时本次输入无法保留，401 会再次提示 */
  }
  unauthorized = false;
  dismissed = false;
  notify();
}

export function clearEngineToken(): void {
  try {
    session()?.removeItem(TOKEN_KEY);
  } catch {
    /* ignore */
  }
}

let unauthorized = false;
let dismissed = false;
const listeners = new Set<() => void>();

function notify(): void {
  listeners.forEach((fn) => fn());
}

export function subscribeEngineUnauthorized(fn: () => void): () => void {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

export function isEngineUnauthorized(): boolean {
  return unauthorized && !dismissed;
}

/** 用户选择「稍后」：本次页面会话不再自动弹窗，刷新后可重新输入 */
export function dismissEngineUnauthorized(): void {
  unauthorized = false;
  dismissed = true;
  notify();
}

function markUnauthorized(): void {
  if (isTauriRuntime() || dismissed || unauthorized) return;
  unauthorized = true;
  notify();
}

/**
 * 桌面壳注入的本机引擎令牌。
 *
 * 桌面模式下引擎只接受带此令牌的 /api 请求（防本机其他进程/网页直接驱动 Agent）。
 * 令牌由 Rust 每次启动随机生成，经 `engine:endpoint` 回给窗口；这里缓存起来供
 * `engineFetch` 同步取用。浏览器直连/服务化模式仍用 sessionStorage 里手工输入的令牌。
 */
let desktopToken = '';
let desktopTokenPending: Promise<string> | null = null;

export function rememberDesktopEngineToken(token: string | null | undefined): void {
  const value = (token ?? '').trim();
  if (value) desktopToken = value;
}

function fetchDesktopToken(): Promise<string> {
  if (!desktopTokenPending) {
    desktopTokenPending = (async () => {
      const api = typeof window !== 'undefined' ? window.tongtianAPI : undefined;
      if (!api) return '';
      try {
        const view = (await api.invoke(IPC_CHANNELS.ENGINE_ENDPOINT, undefined)) as
          | EngineEndpoint
          | null;
        const token = typeof view?.token === 'string' ? view.token : '';
        if (token) desktopToken = token;
        return token;
      } catch {
        // 引擎未启动 → 无令牌可取；请求本身也会如实报不可达
        return '';
      } finally {
        desktopTokenPending = null;
      }
    })();
  }
  return desktopTokenPending;
}

async function requestToken(): Promise<string> {
  if (!isTauriRuntime()) return getEngineToken();
  return desktopToken || (await fetchDesktopToken());
}

/** 引擎请求统一入口：附带调用令牌、声明调用来源，并识别 401 */
export async function engineFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const headers = new Headers(init?.headers);
  const token = await requestToken();
  if (token) headers.set('x-tt-engine-token', token);
  // 来源标记供引擎用量台账分桶（D-43）：与桌面 model_usage 合并时，只有明确属于
  // 桌面通道的样本才会被排除，浏览器直连这部分桌面根本没记，必须原样补入。
  headers.set('x-tt-client', 'web');
  const resp = await fetch(input, { ...init, headers });
  if (resp.status === 401) markUnauthorized();
  return resp;
}
