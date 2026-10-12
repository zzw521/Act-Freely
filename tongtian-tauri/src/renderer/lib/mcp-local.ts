/**
 * MCP 连接器 · 本地通道实现（Mock 与浏览器直连引擎共用）
 *
 * 桌面端以 SQLite `mcp_servers` 为事实来源，由 Rust `mcp` 模块发起真实握手；
 * 浏览器通道既不能拉起子进程，也不该假装自己持有凭据，因此本层：
 * - 配置台账落 `localStorage`（`tt:mcp-servers`），刷新不丢已登记的连接器
 * - 校验口径与 Rust `mcp::normalize_*` 逐条同错误码
 * - 「连接」如实抛 `MCP_UNSUPPORTED`：不伪造 connected 状态，也不伪造工具清单
 * - 与 Rust 同样的脱敏边界：视图只暴露 env/header 的键名，页面拿不到值
 */

import type { MCPTool, McpAddReq, McpServer, McpStatus, McpTransport } from '../../shared/contracts';

export const TRANSPORT_STDIO: McpTransport = 'stdio';
export const TRANSPORT_STREAMABLE_HTTP: McpTransport = 'streamable_http';
export const TRANSPORT_SSE: McpTransport = 'sse';

export const STATUS_OFFLINE: McpStatus = 'offline';
export const STATUS_CONNECTED: McpStatus = 'connected';
export const STATUS_FAILED: McpStatus = 'failed';

/** 传输方式下拉标签 → 落库取值（与 Rust normalize_transport 同一份接受集） */
export const TRANSPORT_LABELS: Record<string, McpTransport> = {
  stdio: TRANSPORT_STDIO,
  sse: TRANSPORT_SSE,
  streamable_http: TRANSPORT_STREAMABLE_HTTP,
  streamablehttp: TRANSPORT_STREAMABLE_HTTP,
  http: TRANSPORT_STREAMABLE_HTTP,
};

export const TRANSPORT_LABEL_TEXT: Record<McpTransport, string> = {
  stdio: 'STDIO',
  streamable_http: 'Streamable HTTP',
  sse: 'SSE',
};

const DEFAULT_TIMEOUT_SECS = 60;
const MAX_TIMEOUT_SECS = 600;
const MAX_NAME_LEN = 60;
const KEY_SERVERS = 'tt:mcp-servers';

// ── 输入规范化（错误码与 Rust / error-messages 对齐） ──────────

export function normalizeTransport(raw?: string | null): McpTransport {
  const v = String(raw ?? '')
    .trim()
    .toLowerCase()
    .replace(/-/g, '_');
  const hit = TRANSPORT_LABELS[v];
  if (hit) return hit;
  if (!v) return TRANSPORT_STDIO;
  throw new Error(`MCP_INVALID_TRANSPORT: 不支持的传输类型「${v}」，仅支持 stdio / streamable_http / sse`);
}

export function normalizeName(raw: string): string {
  const name = String(raw ?? '').trim();
  if (!name) throw new Error('MCP_INVALID_NAME: 连接器名称不能为空');
  if (name.length > MAX_NAME_LEN) {
    throw new Error(`MCP_INVALID_NAME: 连接器名称过长（不超过 ${MAX_NAME_LEN} 字）`);
  }
  return name;
}

export function normalizeUrl(raw: string): string {
  const url = String(raw ?? '').trim();
  if (!url) throw new Error('MCP_INVALID_URL: 服务器地址不能为空');
  if (!url.startsWith('http://') && !url.startsWith('https://')) {
    throw new Error('MCP_INVALID_URL: 服务器地址必须以 http:// 或 https:// 开头');
  }
  if (/\s/.test(url)) throw new Error('MCP_INVALID_URL: 服务器地址不能包含空格');
  return url;
}

export function normalizeCommand(raw: string): string {
  const cmd = String(raw ?? '').trim();
  if (!cmd) throw new Error('MCP_INVALID_COMMAND: stdio 连接器必须填写启动命令');
  if (/[\r\n]/.test(cmd)) throw new Error('MCP_INVALID_COMMAND: 启动命令不能包含换行');
  return cmd;
}

export function normalizeTimeout(raw?: number | null): number {
  const v = Number(raw);
  if (!Number.isFinite(v) || v <= 0) return DEFAULT_TIMEOUT_SECS;
  return Math.min(Math.floor(v), MAX_TIMEOUT_SECS);
}

export function normalizeArgs(raw?: string[] | null): string[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((a) => String(a ?? '')).filter((a) => a.trim() !== '');
}

/** 键值对归一：丢弃空键名（与 Rust normalize_map 同口径） */
export function normalizeMap(raw?: Record<string, string> | null): Record<string, string> {
  const out: Record<string, string> = {};
  Object.entries(raw ?? {}).forEach(([k, v]) => {
    if (String(k ?? '').trim() === '') return;
    out[k] = String(v ?? '');
  });
  return out;
}

/** 值含换行会被注入成额外的环境变量/请求头条目，与 Rust 一样直接拒绝 */
function assertSecretFreeMap(map: Record<string, string>, label: string): void {
  if (Object.values(map).some((v) => /[\r\n]/.test(v))) {
    throw new Error(`MCP_INVALID_HEADER: ${label}不能包含换行`);
  }
}

// ── 台账读写 ─────────────────────────────────────────────────

interface LocalServerRow {
  serverId: string;
  name: string;
  transport: McpTransport;
  command: string;
  args: string[];
  url: string;
  env: Record<string, string>;
  headers: Record<string, string>;
  timeoutSecs: number;
  status: McpStatus;
  lastError?: string;
  tools: MCPTool[];
  probedAtMs?: number;
  createdAtMs: number;
  updatedAtMs: number;
}

let ledger: LocalServerRow[] = [];
let seq = 0;

function storage(): Storage | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage : null;
  } catch {
    return null;
  }
}

function hydrate(): void {
  try {
    const rows = JSON.parse(storage()?.getItem(KEY_SERVERS) ?? '[]') as LocalServerRow[];
    if (Array.isArray(rows)) {
      ledger = rows
        .map((r) => ({
          serverId: String(r?.serverId ?? ''),
          name: String(r?.name ?? ''),
          transport: (r?.transport ?? TRANSPORT_STDIO) as McpTransport,
          command: String(r?.command ?? ''),
          args: Array.isArray(r?.args) ? r.args.map((a) => String(a)) : [],
          url: String(r?.url ?? ''),
          env: normalizeMap(r?.env),
          headers: normalizeMap(r?.headers),
          timeoutSecs: normalizeTimeout(r?.timeoutSecs),
          status: (r?.status ?? STATUS_OFFLINE) as McpStatus,
          lastError: r?.lastError ? String(r.lastError) : undefined,
          tools: Array.isArray(r?.tools) ? r.tools : [],
          probedAtMs: Number.isFinite(r?.probedAtMs) ? r.probedAtMs : undefined,
          createdAtMs: Number(r?.createdAtMs) || 0,
          updatedAtMs: Number(r?.updatedAtMs) || 0,
        }))
        .filter((r) => r.serverId && r.name);
    }
  } catch {
    ledger = [];
  }
}

function persist(): void {
  try {
    storage()?.setItem(KEY_SERVERS, JSON.stringify(ledger));
  } catch {
    /* 存储不可用时本次操作结果仍然有效 */
  }
}

hydrate();

function toView(row: LocalServerRow): McpServer {
  return {
    serverId: row.serverId,
    name: row.name,
    transport: row.transport,
    command: row.command,
    args: row.args,
    url: row.url,
    envKeys: Object.keys(row.env).sort(),
    headerKeys: Object.keys(row.headers).sort(),
    timeoutSecs: row.timeoutSecs,
    status: row.status,
    ...(row.lastError ? { lastError: row.lastError } : {}),
    tools: row.tools,
    connected: row.status === STATUS_CONNECTED,
    ...(row.probedAtMs ? { probedAtMs: row.probedAtMs } : {}),
    createdAtMs: row.createdAtMs,
    updatedAtMs: row.updatedAtMs,
  };
}

function findRow(serverId: unknown): LocalServerRow {
  const id = String(serverId ?? '').trim();
  if (!id) throw new Error('MCP_INVALID_ID: 缺少 MCP 连接器标识');
  const row = ledger.find((r) => r.serverId === id);
  if (!row) throw new Error(`MCP_SERVER_NOT_FOUND: ${id}`);
  return row;
}

/** 列出已登记的连接器（无记录时为空数组，不伪造） */
export function listLocalServers(): McpServer[] {
  return [...ledger]
    .sort((a, b) => a.name.localeCompare(b.name))
    .map(toView);
}

/** 登记连接器：校验后落台账，重名拒绝（不覆盖既有配置），状态从「未连接」开始 */
export function addLocalServer(req: McpAddReq, nowMs: number = Date.now()): McpServer {
  const name = normalizeName(req.name);
  const transport = normalizeTransport(req.transport);
  const command =
    transport === TRANSPORT_STDIO
      ? normalizeCommand(req.command ?? '')
      : String(req.command ?? '').trim();
  const url = transport === TRANSPORT_STDIO ? '' : normalizeUrl(req.url ?? '');
  const args = normalizeArgs(req.args);
  const env = normalizeMap(req.env);
  const headers = normalizeMap(req.headers);
  assertSecretFreeMap(env, '环境变量');
  assertSecretFreeMap(headers, '请求头');
  const timeoutSecs = normalizeTimeout(req.timeoutSecs);
  if (ledger.some((r) => r.name === name)) {
    throw new Error(`MCP_SERVER_EXISTS: 已存在同名连接器「${name}」`);
  }
  seq += 1;
  const row: LocalServerRow = {
    serverId: `mcp-${nowMs}-${seq}`,
    name,
    transport,
    command,
    args,
    url,
    env,
    headers,
    timeoutSecs,
    status: STATUS_OFFLINE,
    tools: [],
    createdAtMs: nowMs,
    updatedAtMs: nowMs,
  };
  ledger = [...ledger, row];
  persist();
  return toView(row);
}

/** 移除连接器登记（只删这一条，不动其他配置） */
export function removeLocalServer(serverId: unknown): { ok: boolean; serverId: string; name: string } {
  const row = findRow(serverId);
  ledger = ledger.filter((r) => r.serverId !== row.serverId);
  persist();
  return { ok: true, serverId: row.serverId, name: row.name };
}

/** 已缓存的工具清单（未连接时为空数组，不伪造工具） */
export function toolsOfLocalServer(serverId: unknown): MCPTool[] {
  return [...findRow(serverId).tools];
}

/**
 * 断开：清除已验证状态与工具缓存，配置保留可随时再连。
 * 浏览器通道本就没有会话，因此这是幂等的状态复位，而不是「断开了一个不存在的连接」。
 */
export function disconnectLocalServer(serverId: unknown, nowMs: number = Date.now()): McpServer {
  const row = findRow(serverId);
  row.status = STATUS_OFFLINE;
  row.tools = [];
  row.lastError = undefined;
  row.probedAtMs = undefined;
  row.updatedAtMs = nowMs;
  persist();
  return toView(row);
}

/**
 * 连接：浏览器通道无法起 stdio 子进程，也不该把凭据带进跨源请求，
 * 因此如实拒绝——把「未验证」显示成「已连接」是假数据。
 */
export function connectLocalServer(serverId: unknown): McpServer {
  const row = findRow(serverId);
  throw new Error(
    `MCP_UNSUPPORTED: 当前通道未与「${row.name}」握手，请在桌面版通天办公中连接`,
  );
}

// ── JSON 配置导入 ────────────────────────────────────────────

/**
 * 解析社区通用的 MCP 配置 JSON，得到可直接登记的请求列表。
 * 支持 `{ mcpServers: { name: { command, args, env, url, ... } } }`、
 * `{ servers: [...] }` / 数组，以及单个 `{ name, transport, ... }` 对象。
 * 与 Rust 侧一样：不执行命令、不联网，只做结构解析。
 */
export function parseMcpJsonConfig(raw: string): McpAddReq[] {
  const text = String(raw ?? '').trim();
  if (!text) throw new Error('MCP_INVALID_CONFIG: 配置内容为空');
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error('MCP_INVALID_CONFIG: 配置不是合法 JSON');
  }
  const collect = (list: unknown[]): McpAddReq[] =>
    list.map((entry) => toAddReq(entry)).filter(Boolean) as McpAddReq[];
  if (Array.isArray(parsed)) return requireEntries(collect(parsed));
  if (!parsed || typeof parsed !== 'object') {
    throw new Error('MCP_INVALID_CONFIG: 配置需为 JSON 对象');
  }
  const obj = parsed as Record<string, unknown>;
  const mapSource = obj.mcpServers ?? obj.servers;
  if (mapSource && typeof mapSource === 'object' && !Array.isArray(mapSource)) {
    return requireEntries(
      Object.entries(mapSource as Record<string, unknown>).map(
        ([name, entry]) => toAddReq(entry, name),
      ),
    );
  }
  if (Array.isArray(mapSource)) return requireEntries(collect(mapSource));
  return requireEntries([toAddReq(obj)]);
}

/** 解析不出任何可用连接器时如实报错，而不是回空列表让导入「静默成功」 */
function requireEntries(reqs: Array<McpAddReq | null>): McpAddReq[] {
  const list = reqs.filter(Boolean) as McpAddReq[];
  if (!list.length) throw new Error('MCP_INVALID_CONFIG: 配置中未发现可用的连接器');
  return list;
}

function toAddReq(entry: unknown, fallbackName?: string): McpAddReq | null {
  if (!entry || typeof entry !== 'object') return null;
  const e = entry as Record<string, unknown>;
  const name = String(e.name ?? e.key ?? fallbackName ?? '').trim();
  if (!name) return null;
  const type = String(e.type ?? e.transport ?? '').trim();
  const url = String(e.url ?? '').trim();
  const command = String(e.command ?? '').trim();
  const transport =
    type || (!url && command ? 'stdio' : url.includes('/sse') ? 'sse' : 'streamable_http');
  return {
    name,
    transport,
    command,
    args: Array.isArray(e.args) ? e.args.map((a) => String(a)) : [],
    url,
    env: normalizeMap(e.env as Record<string, string>),
    headers: normalizeMap(e.headers as Record<string, string>),
    timeoutSecs: Number.isFinite(Number(e.timeout)) ? Number(e.timeout) : undefined,
  };
}

// ── 通道路由 ─────────────────────────────────────────────────

export interface LocalMcpResult {
  handled: boolean;
  value?: unknown;
}

/** MCP 通道本地实现：未命中返回 handled=false */
export function handleLocalMcpChannel(channel: string, args: unknown[]): LocalMcpResult {
  const payload = (args[0] ?? {}) as Record<string, unknown>;
  const serverId = payload.serverId ?? payload.id;
  switch (channel) {
    case 'mcp:listServers':
      return { handled: true, value: listLocalServers() };
    case 'mcp:addServer':
      return { handled: true, value: addLocalServer(payload as unknown as McpAddReq) };
    case 'mcp:removeServer':
      return { handled: true, value: removeLocalServer(serverId) };
    case 'mcp:connect':
      return { handled: true, value: connectLocalServer(serverId) };
    case 'mcp:disconnect':
      return { handled: true, value: disconnectLocalServer(serverId) };
    case 'mcp:listTools':
      return { handled: true, value: toolsOfLocalServer(serverId) };
    default:
      return { handled: false };
  }
}

/** 测试用：复位本地连接器台账 */
export function resetLocalServers(): void {
  ledger = [];
  seq = 0;
  try {
    storage()?.removeItem(KEY_SERVERS);
  } catch {
    /* ignore */
  }
}
