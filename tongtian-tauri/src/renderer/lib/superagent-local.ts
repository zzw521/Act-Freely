/**
 * Super-agent 主动候选 · 本地通道实现（Mock 与浏览器直连引擎共用）
 *
 * 影子模式需要两样浏览器通道拿不到的东西：
 * - 事件账本与准入策略：只在桌面主进程的本机库里（四张表由 Rust 独占读写）
 * - 常驻巡检：随桌面应用存活的后台循环，浏览器标签页关掉就没有了
 *
 * 因此这里对每个 `superagent:*` 通道如实拒绝，**不编造候选清单**，也不在前端建一份
 * 「看起来像账本」的本地台账——影子模式的价值恰恰是每条判定都能回看到本机留痕。
 * 对齐 `collab-local.ts` 的处理方式（能力缺失就说缺失，不伪装成可用）。
 */

export interface LocalSuperagentResult {
  handled: boolean;
  value?: unknown;
}

/** 一句话把「为什么这里看不到数据」说清楚，界面原样透出即可 */
export const SUPERAGENT_DESKTOP_ONLY_MESSAGE =
  'SUPERAGENT_DESKTOP_ONLY: 主动候选的事件账本与后台巡检只在桌面版本机运行，请在桌面版任我行中查看这一页';

export function handleLocalSuperagentChannel(channel: string): LocalSuperagentResult {
  if (!channel.startsWith('superagent:')) return { handled: false };
  return { handled: true, value: Promise.reject(new Error(SUPERAGENT_DESKTOP_ONLY_MESSAGE)) };
}
