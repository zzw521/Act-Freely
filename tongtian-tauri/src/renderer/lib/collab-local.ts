/**
 * 项目协作 · 本地通道实现（Mock 与浏览器直连引擎共用）
 *
 * 协作能力需要两样浏览器通道拿不到的东西：
 * - 账号 refresh token（只在桌面端系统凭据库）
 * - 能直连账号服务并携带 Authorization 头的本机客户端
 *
 * 因此这里如实回报 supported=false / COLLAB_UNSUPPORTED，
 * 不编造项目列表，也不假装任务已同步。桌面端由 Rust `collab` 模块实现。
 */

export interface LocalCollabResult {
  handled: boolean;
  value?: unknown;
}

export function handleLocalCollabChannel(channel: string): LocalCollabResult {
  if (!channel.startsWith('collab:')) return { handled: false };
  if (channel === 'collab:status') {
    return {
      handled: true,
      value: { supported: false, baseUrl: '', serverValid: false, loggedIn: false },
    };
  }
  return {
    handled: true,
    value: Promise.reject(
      new Error('COLLAB_UNSUPPORTED: 当前运行通道无法访问协作服务，请在桌面版通天办公中使用项目协作'),
    ),
  };
}
