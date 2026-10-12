/**
 * 网络代理设置的本地权威实现（Mock / 浏览器直连引擎通道）
 *
 * 与 Rust `src-tauri/src/proxy.rs` 保持同一口径：同样的三档模式、同样的地址校验规则、
 * 同样把本地回环并入忽略清单。浏览器无法设置系统代理，因此这里只负责「算出应当生效的
 * 配置并把不合法输入如实报错」，供设置页在保存前给出即时反馈。
 */

import type { ProxyConfigSnapshot, ProxyStatus } from '../../shared/contracts';

export const PROXY_MODE_SYSTEM = '跟随系统';
export const PROXY_MODE_CUSTOM = '自定义';
export const PROXY_MODE_DISABLED = '不使用';

/** 无论何种模式都直连的地址（引擎与本地模型的通信底线，与 Rust ALWAYS_BYPASS 一致） */
export const PROXY_ALWAYS_BYPASS = ['127.0.0.1', 'localhost', '::1'];

const SUPPORTED_SCHEMES = ['http', 'https', 'socks5', 'socks5h', 'socks4', 'socks'];

export interface ProxyInput {
  proxyMode?: string;
  proxyUrl?: string;
  proxyBypass?: string;
}

export type ProxySpec = ProxyConfigSnapshot;

/** 设置里可能出现的代理键（与 Rust proxy::is_related_key 对应） */
export const PROXY_SETTING_KEYS = ['proxyMode', 'proxyUrl', 'proxyBypass'];

/** 本批设置是否触及代理配置（触及则需整组校验） */
export function touchesProxySetting(keys: Iterable<string>): boolean {
  for (const key of keys) if (PROXY_SETTING_KEYS.includes(key)) return true;
  return false;
}

/** 从任意设置对象取字符串口径（非字符串按空处理，与 Rust decode_value 后取 as_str 一致） */
function strOf(input: Record<string, unknown>, key: string): string {
  const v = input[key];
  return typeof v === 'string' ? v : '';
}

/** 由设置推导生效配置；自定义模式地址非法时返回 error（不落库、不静默降级） */
export function computeProxySpec(input: Record<string, unknown>): { spec: ProxySpec } | { error: string } {
  const mode = parseProxyMode(strOf(input, 'proxyMode'));
  if (mode !== PROXY_MODE_CUSTOM) {
    return { spec: { mode, url: null, bypass: [] } };
  }
  const url = validateProxyUrl(strOf(input, 'proxyUrl'));
  if ('error' in url) return url;
  return { spec: { mode, url: url.url, bypass: parseProxyBypass(strOf(input, 'proxyBypass')) } };
}

/** 归一模式标签（兼容英文口径，与 Rust parse_mode 一致） */
export function parseProxyMode(raw?: string): ProxyConfigSnapshot['mode'] {
  const v = (raw ?? '').trim();
  if (v === PROXY_MODE_CUSTOM || v === 'custom' || v === 'Custom' || v === '手动') return PROXY_MODE_CUSTOM;
  if ([PROXY_MODE_DISABLED, '直连', 'off', 'none', 'disabled', 'Disabled'].includes(v)) return PROXY_MODE_DISABLED;
  return PROXY_MODE_SYSTEM;
}

/** 校验并归一代理地址（须带协议与非空主机，与 Rust validate_url 一致） */
export function validateProxyUrl(raw?: string): { url: string } | { error: string } {
  const url = (raw ?? '').trim();
  if (!url) return { error: 'PROXY_URL_REQUIRED: 未填写代理地址' };
  const sep = url.indexOf('://');
  if (sep < 0) return { error: 'PROXY_INVALID_URL: 代理地址需带协议，如 http://127.0.0.1:7890' };
  const scheme = url.slice(0, sep);
  if (!SUPPORTED_SCHEMES.includes(scheme)) {
    return { error: `PROXY_INVALID_URL: 不支持的代理协议 ${scheme}，仅支持 http / https / socks5 / socks4` };
  }
  const host = url.slice(sep + 3).replace(/\/+$/, '');
  if (!host) return { error: 'PROXY_INVALID_URL: 代理地址缺少主机与端口，如 http://127.0.0.1:7890' };
  const invalid = validateAuthority(host);
  if (invalid) return { error: invalid };
  return { url: `${scheme}://${host}` };
}

const ERR_V6 = 'PROXY_INVALID_URL: IPv6 地址需用方括号包裹';

/**
 * 主机与端口形状校验，与 Rust validate_authority 同口径。
 * 协议前缀合法但整体不可路由的地址（尾随第二个协议、路径、端口非数字）若放行，
 * 设置页会谎称「已保存并生效」，而实际出网只会静默超时。
 */
function validateAuthority(raw: string): string | null {
  if (raw.includes('://')) return 'PROXY_INVALID_URL: 只能有一个协议前缀';
  if (/\s/.test(raw) || /[/?#]/.test(raw)) return 'PROXY_INVALID_URL: 主机部分不能含空格或路径';
  // 允许 http://user:pass@proxy:8080，只校验凭据之后的 authority
  const authority = raw.split('@').pop() ?? '';
  let port: string | null = null;
  if (authority.startsWith('[')) {
    const end = authority.indexOf(']');
    if (end < 0 || end === 1) return ERR_V6;
    const tail = authority.slice(end + 1);
    if (tail) {
      if (!tail.startsWith(':')) return ERR_V6;
      port = tail.slice(1);
    }
  } else {
    const idx = authority.lastIndexOf(':');
    if (idx >= 0) {
      const hostErr = validateHost(authority.slice(0, idx));
      if (hostErr) return hostErr;
      port = authority.slice(idx + 1);
    } else {
      const hostErr = validateHost(authority);
      if (hostErr) return hostErr;
    }
  }
  if (port !== null) {
    if (!/^\d+$/.test(port)) return 'PROXY_INVALID_URL: 端口需是数字';
    const n = Number(port);
    if (n < 1 || n > 65535) return 'PROXY_INVALID_URL: 端口需在 1-65535 之间';
  }
  return null;
}

function validateHost(host: string): string | null {
  if (host.includes(':')) return ERR_V6;
  if (!host || !/^[A-Za-z0-9._-]+$/.test(host)) return 'PROXY_INVALID_URL: 主机名含非法字符';
  return null;
}

/** 忽略主机解析（逗号/空格/分号/换行分隔，去空去重） */
export function parseProxyBypass(raw?: string): string[] {
  const out: string[] = [];
  for (const token of (raw ?? '').split(/[,; \n]/)) {
    const item = token.trim().replace(/^,+|,+$/g, '');
    if (!item) continue;
    if (!out.includes(item)) out.push(item);
  }
  return out;
}

/** 用户附加项 + 本地回环（去重保序） */
export function effectiveBypass(bypass: string[]): string[] {
  const out: string[] = [];
  for (const item of [...bypass, ...PROXY_ALWAYS_BYPASS]) {
    if (!out.includes(item)) out.push(item);
  }
  return out;
}

/** 注入引擎子进程的代理环境变量（与 Rust env_vars 同语义；浏览器预览不生效） */
export function proxyEnvOf(spec: ProxySpec): Record<string, string> {
  const names = ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'];
  if (spec.mode === PROXY_MODE_SYSTEM) return {};
  if (spec.mode === PROXY_MODE_DISABLED) {
    return Object.fromEntries([...names.map((n) => [n, ''] as const), ['NO_PROXY', '*'] as const, ['no_proxy', '*'] as const]);
  }
  const url = spec.url ?? '';
  const noProxy = effectiveBypass(spec.bypass).join(',');
  return Object.fromEntries([
    ...names.map((n) => [n, url] as const),
    ['NO_PROXY', noProxy] as const,
    ['no_proxy', noProxy] as const,
  ]);
}

/**
 * 设置批量写入前的代理整组校验（Mock / 浏览器通道）。
 * 与 Rust `settings_set_many` 落库前调用 `proxy::spec_from_patch` 同口径：
 * 自定义却没填地址 → 抛出 `PROXY_*: 详情`，整批拒绝，不留半个生效配置。
 */
export function assertProxyPatchAcceptable(
  current: Record<string, unknown>,
  patch: Record<string, unknown>,
): void {
  if (!touchesProxySetting(Object.keys(patch))) return;
  const computed = computeProxySpec({ ...current, ...patch });
  if ('error' in computed) throw new Error(computed.error);
}

/** 浏览器通道状态：无法控制系统代理，如实标注为本地推导值 */
export function localProxyStatus(input: Record<string, unknown>): ProxyStatus {
  const computed = computeProxySpec(input);
  const spec: ProxySpec = 'spec' in computed ? computed.spec : { mode: PROXY_MODE_SYSTEM, url: null, bypass: [] };
  return {
    active: spec,
    persisted: 'spec' in computed ? spec : null,
    effectiveBypass: effectiveBypass(spec.bypass),
    // 浏览器没有常驻引擎子进程，代理变化不涉及重启
    staleEngines: 0,
  };
}
