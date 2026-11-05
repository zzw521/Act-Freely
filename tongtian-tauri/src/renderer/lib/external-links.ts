/**
 * 外部链接出口：网页里的 http(s)/mailto 链接一律交给系统浏览器。
 *
 * Tauri 的 webview 默认允许站内导航；若直接跟随外链，远程页面会留在持有
 * IPC 能力的窗口里。这里在捕获阶段拦下外链并调用系统 opener，
 * 浏览器模式下退回 window.open（Mock/联调仍能正常跳转）。
 */

import { isTauri } from './tauri-api';

let nativeWindowOpen: typeof window.open | null = null;

function externalUrl(raw: string): URL | null {
  try {
    const url = new URL(raw, window.location.href);
    if (url.protocol !== 'http:' && url.protocol !== 'https:' && url.protocol !== 'mailto:') {
      return null;
    }
    if (url.protocol !== 'mailto:' && url.origin === window.location.origin) {
      return null;
    }
    return url;
  } catch {
    return null;
  }
}

async function openExternal(url: string): Promise<void> {
  if (isTauri()) {
    try {
      const { invoke } = await import('@tauri-apps/api/core');
      await invoke('plugin:opener|open_url', { url });
      return;
    } catch {
      /* 插件不可用时退回下面的 window.open */
    }
  }
  const opener = nativeWindowOpen ?? window.open.bind(window);
  opener(url, '_blank', 'noopener,noreferrer');
}

/** 全局安装一次（main.tsx 启动时调用） */
export function installExternalLinkGuard(): void {
  if (typeof document === 'undefined' || nativeWindowOpen) return;
  nativeWindowOpen = window.open.bind(window);

  document.addEventListener(
    'click',
    (event) => {
      if (event.defaultPrevented || event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const target = event.target as HTMLElement | null;
      const anchor = target?.closest?.('a[href]') as HTMLAnchorElement | null;
      if (!anchor) return;
      const href = anchor.getAttribute('href') ?? '';
      if (!href || href.startsWith('#')) return;
      const url = externalUrl(href);
      if (!url) return;
      event.preventDefault();
      void openExternal(url.toString());
    },
    true,
  );

  window.open = ((url?: string | URL, target?: string, features?: string) => {
    const href = typeof url === 'string' ? url : url?.toString() ?? '';
    if (href) {
      const external = externalUrl(href);
      if (external) {
        void openExternal(external.toString());
        return null;
      }
    }
    return nativeWindowOpen?.(url as string, target, features) ?? null;
  }) as typeof window.open;
}