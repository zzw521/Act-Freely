/**
 * 在线市场 · 本地通道实现（Mock 与浏览器直连引擎共用）
 *
 * 在线市场需要三件事同时成立：拉取远端索引、按索引下载产物、把产物写进本机运行目录。
 * 浏览器通道没有运行目录，也无法绕过 CORS 安全地做完整性校验后的落盘，
 * 因此这里如实回报 supported=false：不编造市场条目、不假装安装成功，
 * 页面据此提示「请到桌面版使用在线市场」。
 *
 * 桌面端由 Rust `market` 模块实现（HTTPS/公网校验、禁重定向、SHA-256、大小上限）。
 */

export interface LocalMarketResult {
  handled: boolean;
  value?: unknown;
}

const UNSUPPORTED = 'MARKET_UNSUPPORTED';

function unsupportedList(): unknown {
  return {
    supported: false,
    configured: false,
    indexUrl: null,
    fetchedAtMs: 0,
    signaturePolicy: null,
    publishers: [],
    items: [],
  };
}

/** 市场通道本地实现：未命中返回 handled=false */
export function handleLocalMarketChannel(channel: string, args: unknown[]): LocalMarketResult {
  void args;
  switch (channel) {
    case 'market:configGet':
      return {
        handled: true,
        value: { supported: false, skillIndexUrl: null, suiteIndexUrl: null },
      };
    case 'market:skillList':
    case 'market:suiteList':
      return { handled: true, value: unsupportedList() };
    case 'market:trustGet':
      return { handled: true, value: { supported: false, publishers: [] } };
    case 'market:configSet':
    case 'market:skillInstall':
    case 'market:suiteInstall':
    case 'market:trustAdd':
    case 'market:trustRemove':
      return {
        handled: true,
        value: Promise.reject(new Error(`${UNSUPPORTED}: 当前通道没有市场下载与落盘能力`)),
      };
    default:
      return { handled: false };
  }
}
