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

/** 引擎请求统一入口：附带服务令牌并识别 401 */
export function engineFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  const token = getEngineToken();
  if (token) headers.set('x-tt-engine-token', token);
  return fetch(input, { ...init, headers }).then((resp) => {
    if (resp.status === 401) markUnauthorized();
    return resp;
  });
}
