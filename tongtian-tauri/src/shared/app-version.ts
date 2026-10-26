/**
 * 应用版本号的单一来源。
 *
 * 桌面通道的事实来源是 Rust `system_info`（返回 `env!("CARGO_PKG_VERSION")`）；
 * 浏览器 / Mock / 引擎直连通道没有 Rust，用构建期由 vite 从 package.json 注入的常量。
 * 此前 mock-api 写死 '0.1.0'、engine-api 写死 '0.3.0' —— 同一个应用在不同通道显示两个版本，
 * 属于典型的假数据，收口到这里。
 */

declare const __TT_APP_VERSION__: string | undefined;

/** 构建期注入的版本号；非 vite 环境（node 单测）返回开发标记，不编一个像真的版本 */
export function appVersion(): string {
  return typeof __TT_APP_VERSION__ === 'string' && __TT_APP_VERSION__
    ? __TT_APP_VERSION__
    : 'dev（未经 vite 构建注入）';
}
