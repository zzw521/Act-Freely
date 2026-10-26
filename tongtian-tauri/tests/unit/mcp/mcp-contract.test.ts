/**
 * MCP 连接器 · 契约与接线测试（任务 #20）
 *
 * 三条链路都锁定在源码层面，任一侧漂移即失败：
 * 1. Rust `mcp` 模块：入参/出参字段、脱敏（只出键名）、SQL 参数化、V6 建表、command 注册
 * 2. 渲染层三通道：tauri-api 的 6 个分支、Mock 与引擎直连通道的本地权威接管、api.ts 的 mcpAPI
 * 3. 错误码 parity：Rust 抛出的 MCP_* 码必须在 ErrorCode 枚举与文案表里都有登记
 *
 * 另外走一遍真实分发（mock-api 注入 window.tongtianAPI → mcpAPI → handleLocalMcpChannel），
 * 确认扩展页在浏览器通道拿到的是「登记成功 / 握手如实拒绝」，而不是占位 toast。
 */

import { describe, it, expect, beforeAll, vi } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { ErrorCode, IPC_CHANNELS } from '../../../src/shared/contracts';
import { errorMessageOf } from '../../../src/shared/error-messages';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
}

const rustMcp = () => read('../../../src-tauri/src/mcp/mod.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const rustMigrate = () => read('../../../src-tauri/src/store/migrate.rs');
const rustOauth = () => read('../../../src-tauri/src/mcp/oauth.rs');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const engineApi = () => read('../../../src/renderer/lib/engine-api.ts');
const libApi = () => read('../../../src/renderer/lib/api.ts');
const extensionsPage = () => read('../../../src/renderer/pages/Extensions/index.tsx');

/** 递归收集 src-tauri/src 下字符串字面量中的 MCP_* 错误码 */
function rustMcpCodes(): string[] {
  const root = fileURLToPath(new URL('../../../src-tauri/src', import.meta.url));
  const found = new Set<string>();
  const walk = (dir: string) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const p = join(dir, entry.name);
      if (entry.isDirectory()) walk(p);
      else if (entry.name.endsWith('.rs')) {
        for (const m of readFileSync(p, 'utf8').matchAll(/(?<=")MCP_[A-Z][A-Z_]*/g)) {
          found.add(m[0]);
        }
      }
    }
  };
  walk(root);
  return [...found].sort();
}

describe('Rust 侧契约：注册表结构与脱敏边界', () => {
  it('AddServer 字段与渲染层 McpAddReq 同名（camelCase 反序列化）', () => {
    const src = rustMcp();
    const body = src.slice(src.indexOf('pub struct AddServer'));
    expect(src).toContain('#[serde(rename_all = "camelCase")]');
    for (const field of ['name', 'transport', 'command', 'args', 'url', 'env', 'headers', 'timeout_secs']) {
      expect(body).toContain(`pub ${field}`);
    }
  });

  it('ServerRow 只带 env_keys/header_keys，凭据值不进入 IPC', () => {
    const src = rustMcp();
    const body = src.slice(src.indexOf('pub struct ServerRow'), src.indexOf('pub struct ToolRow'));
    expect(body).toContain('pub env_keys: Vec<String>');
    expect(body).toContain('pub header_keys: Vec<String>');
    expect(body).not.toMatch(/pub env: /);
    expect(body).not.toMatch(/pub headers: /);
  });

  it('SQL 全参数化，命令不经 Shell（子进程参数以数组传递）', () => {
    const src = rustMcp();
    expect(src).toContain('rusqlite::params!');
    expect(src).not.toMatch(/execute\(\s*format!/);
    expect(src).not.toMatch(/query\w*\(\s*format!/);
    expect(src).toContain('pub fn spawn_plan(command: &str, args: &[String])');
    expect(src).not.toContain('sh -c');
  });

  it('V6 迁移增量建 mcp_servers 表（不改存量表）', () => {
    const src = rustMigrate();
    expect(src).toMatch(/CREATE TABLE IF NOT EXISTS mcp_servers/);
    expect(src).toMatch(/server_id\s+TEXT PRIMARY KEY/);
    expect(src).toMatch(/tools_json\s+TEXT NOT NULL/);
    // 增量原则：本表相关语句里不得出现 DROP/清空存量数据的写法
    expect(src).not.toMatch(/DROP TABLE/i);
    expect(src).not.toMatch(/DELETE FROM mcp_servers/i);
  });

  it('八个 command 均已注册进 invoke_handler（前端 invoke 不会落到未注册通道）', () => {
    const lib = rustLib();
    for (const cmd of [
      'mcp_list_servers',
      'mcp_add_server',
      'mcp_remove_server',
      'mcp_connect',
      'mcp_disconnect',
      'mcp_list_tools',
      'mcp_oauth_authorize',
      'mcp_oauth_revoke',
    ]) {
      expect(lib).toContain(`mcp::${cmd}`);
    }
    // 重复注册会让 tauri 编译期报错
    expect(lib.match(/mcp::mcp_list_servers/g)).toHaveLength(1);
  });

  it('连接失败也落库留原因，不把失败伪装成未验证态', () => {
    const src = rustMcp();
    expect(src).toContain('STATUS_FAILED');
    expect(src).toMatch(/record_probe\([\s\S]*STATUS_FAILED/);
    expect(src).toContain('audit::SEV_WARNING');
  });
});

describe('远程 MCP OAuth：公共客户端 PKCE 与令牌边界', () => {
  it('只做授权码 + S256 PKCE，不保存 client secret', () => {
    const src = rustOauth();
    expect(src).toContain('.append_pair("code_challenge_method", "S256")');
    expect(src).toContain('code_challenge(&verifier)');
    // 公共客户端形态：请求体里不得出现 client_secret
    expect(src).not.toContain('client_secret');
  });

  it('授权端点只允许 HTTPS 或回环 HTTP，并拒绝 URL 内嵌凭据与非公网解析', () => {
    const src = rustOauth();
    expect(src).toContain('"https" => Ok(url)');
    expect(src).toContain('"http" if host_is_loopback(&url) => Ok(url)');
    expect(src).toContain('url.username()');
    expect(src).toContain('ensure_resolved_public');
    expect(src).toContain('ip_is_public');
  });

  it('回调校验 state，等待有上限，换 token 不跟随重定向且限制响应体积', () => {
    const src = rustOauth();
    expect(src).toContain('CALLBACK_TIMEOUT_SECS');
    expect(src).toContain('MCP_OAUTH_STATE_MISMATCH');
    expect(src).toContain('redirect(reqwest::redirect::Policy::none())');
    expect(src).toContain('MAX_TOKEN_BYTES');
  });

  it('令牌只进系统凭据库：不落 SQLite、不进 ServerRow/IPC', () => {
    const src = rustOauth();
    expect(src).toContain('mcp-oauth:{server_id}');
    expect(src).toContain('store.vault()');
    const mcp = rustMcp();
    const row = mcp.slice(mcp.indexOf('pub struct ServerRow'), mcp.indexOf('pub struct ToolRow'));
    expect(row).toContain('pub auth: Option<oauth::OAuthConfig>');
    expect(row).not.toMatch(/access_token|refresh_token/);
  });

  it('V10 增量迁移只新增 auth_json 列（不动存量表与数据）', () => {
    const src = rustMigrate();
    expect(src).toMatch(/ALTER TABLE mcp_servers ADD COLUMN auth_json TEXT NOT NULL DEFAULT '\{\}'/);
    expect(src).not.toMatch(/DROP TABLE|DELETE FROM mcp_servers/i);
    expect(src).not.toMatch(/UPDATE mcp_servers/i);
  });

  it('运行时只注入 Authorization 头；缺令牌或令牌过期时不下发', () => {
    const src = rustMcp();
    expect(src).toContain('oauth::bearer_header(&token)');
    expect(src).toContain('oauth::runtime_authorized(store, &r.server_id, now)');
    expect(src).toContain('MCP_OAUTH_REQUIRED');
    expect(src).toContain('MCP_OAUTH_REJECTED');
  });

  it('审计只记录连接器名与授权主机，不含令牌或查询串', () => {
    const src = rustMcp();
    expect(src).toContain('ACTION_CONNECTOR_AUTH');
    expect(src).toContain('oauth::endpoint_host(&cfg.authorization_url)');
    expect(src).not.toMatch(/format!\([^)]*access_token/);
  });
});

describe('渲染层三通道接线', () => {
  it('tauri-api：8 个 mcp:* 通道映射到对应 Rust command', () => {
    const src = tauriApi();
    expect(src).toContain("if (channel === 'mcp:listServers') return invoke('mcp_list_servers')");
    expect(src).toContain("if (channel === 'mcp:addServer') return invoke('mcp_add_server', { req: args[0] })");
    for (const [channel, command] of [
      ['mcp:removeServer', 'mcp_remove_server'],
      ['mcp:connect', 'mcp_connect'],
      ['mcp:disconnect', 'mcp_disconnect'],
      ['mcp:listTools', 'mcp_list_tools'],
      ['mcp:oauthAuthorize', 'mcp_oauth_authorize'],
      ['mcp:oauthRevoke', 'mcp_oauth_revoke'],
    ]) {
      expect(src).toContain(`'${channel}': '${command}'`);
    }
    expect(src).toContain("invoke(mcpCommand, { serverId: req.serverId ?? req.id ?? '' })");
  });

  it('Mock 与引擎直连通道都交给同一份本地权威实现', () => {
    expect(mockApi()).toContain("import { handleLocalMcpChannel } from './mcp-local'");
    expect(mockApi()).toContain('const local = handleLocalMcpChannel(channel, args)');
    expect(engineApi()).toContain("import { handleLocalMcpChannel } from './mcp-local'");
    expect(engineApi()).toContain('const mcp = handleLocalMcpChannel(channel, args)');
  });

  it('api.ts 暴露 mcpAPI，且入参用 IPC_CHANNELS 常量（不再散落字符串）', () => {
    const src = libApi();
    expect(src).toContain('export const mcpAPI = {');
    expect(src).toContain('mcp: mcpAPI,');
    for (const key of [
      'MCP_LIST_SERVERS',
      'MCP_ADD_SERVER',
      'MCP_REMOVE_SERVER',
      'MCP_CONNECT',
      'MCP_DISCONNECT',
      'MCP_LIST_TOOLS',
      'MCP_OAUTH_AUTHORIZE',
      'MCP_OAUTH_REVOKE',
    ]) {
      expect(src).toContain(`IPC_CHANNELS.${key}`);
    }
  });

  it('IPC 通道名与 Rust command 一一对应（无死常量）', () => {
    expect(IPC_CHANNELS.MCP_LIST_SERVERS).toBe('mcp:listServers');
    expect(IPC_CHANNELS.MCP_ADD_SERVER).toBe('mcp:addServer');
    expect(IPC_CHANNELS.MCP_REMOVE_SERVER).toBe('mcp:removeServer');
    expect(IPC_CHANNELS.MCP_CONNECT).toBe('mcp:connect');
    expect(IPC_CHANNELS.MCP_DISCONNECT).toBe('mcp:disconnect');
    expect(IPC_CHANNELS.MCP_LIST_TOOLS).toBe('mcp:listTools');
    expect(IPC_CHANNELS.MCP_OAUTH_AUTHORIZE).toBe('mcp:oauthAuthorize');
    expect(IPC_CHANNELS.MCP_OAUTH_REVOKE).toBe('mcp:oauthRevoke');
  });
});

describe('扩展页连接器页签真实化', () => {
  it('不再保留「即将上线（MCP 接入 P2）」占位文案', () => {
    const src = extensionsPage();
    expect(src).not.toContain('即将上线');
    expect(src).not.toContain('MCP 接入 P2');
  });

  it('连接器页签消费 mcpAPI 六个方法与握手状态', () => {
    const src = extensionsPage();
    for (const call of [
      'mcpAPI.list()',
      'mcpAPI.add(',
      'mcpAPI.connect(',
      'mcpAPI.disconnect(',
      'mcpAPI.remove(',
      'mcpAPI.oauthAuthorize(',
      'mcpAPI.oauthRevoke(',
    ]) {
      expect(src).toContain(call);
    }
    expect(src).toContain('const [mcpServers, setMcpServers] = useState<McpServer[]>([])');
    expect(src).not.toContain('useState<Record<string, boolean>>({ fs: true })');
    expect(src).toContain('上次连接失败：');
    expect(src).toContain('TRANSPORT_LABEL_TEXT');
  });

  it('凭据只回显键名，并明确值留在本机', () => {
    const src = extensionsPage();
    // stdio 展示环境变量键名、远程展示请求头键名，另一侧不展示
    expect(src).toContain("s.transport === 'stdio' ? s.envKeys : s.headerKeys");
    expect(src).toContain('值仅存本机，不回显');
    // 默认行留空，避免把示例 API_KEY / Bearer token 当成真实配置登记
    expect(src).toContain("useState<KvPair[]>([{ key: '', value: '' }])");
    expect(src).not.toContain("key: 'API_KEY', value: ''");
    expect(src).not.toContain("key: 'Authorization', value: 'Bearer your-token'");
  });

  it('OAuth 与显式 Authorization 头互斥：表单拦截并禁用提交', () => {
    const src = extensionsPage();
    expect(src).toContain('oauthHeaderConflict');
    expect(src).toContain('它会覆盖 OAuth 自动附加的令牌');
    expect(src).toContain('!oauthHeaderConflict');
  });

  it('OAuth：配置在弹窗登记、卡片显示授权状态、令牌只存系统凭据库', () => {
    const src = extensionsPage();
    for (const field of ['mcpAuthEnabled', 'mcpAuthUrl', 'mcpTokenUrl', 'mcpClientId', 'mcpScopes']) {
      expect(src).toContain(field);
    }
    expect(src).toContain("s.authStatus === 'authorized'");
    expect(src).toContain("s.authStatus === 'expired'");
    expect(src).toContain('OAuth 未授权');
    expect(src).toContain('令牌仅存系统凭据库');
    // 浏览器通道不持有令牌：授权/撤销入口如实禁用并说明去桌面版
    expect(src).toContain('!desktopChannel');
    expect(src).toContain('浏览器通道无法保存 OAuth 令牌');
    // 页面不得渲染任何令牌字段
    expect(src).not.toMatch(/accessToken|refreshToken|access_token|refresh_token/);
  });

  it('移除连接器需二次确认（不做无提示的破坏性删除）', () => {
    const src = extensionsPage();
    expect(src).toContain('setServerToRemove(s)');
    expect(src).toContain('移除连接器登记');
    expect(src).toContain('handleConfirmRemoveMcp');
  });
});

describe('错误码 parity：Rust 抛出的 MCP_* 码全部有归属', () => {
  const emitted = rustMcpCodes();

  it('Rust 源码扫描命中错误码（防止路径配置失效导致空集合假通过）', () => {
    expect(emitted.length).toBeGreaterThan(0);
  });

  it.each(emitted.map((c) => [c, c]))('Rust 抛出码 %s 既是枚举成员也有文案', (_name, code) => {
    expect(Object.values(ErrorCode)).toContain(code);
    expect(errorMessageOf(code)).not.toContain('发生错误（');
  });
});

describe('浏览器通道端到端分发（mock-api → mcpAPI → 本地台账）', () => {
  type McpApi = typeof import('../../../src/renderer/lib/api');

  let api: McpApi;

  beforeAll(async () => {
    const store = new Map<string, string>();
    (globalThis as Record<string, unknown>).localStorage = {
      getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
      setItem: (k: string, v: string) => void store.set(k, String(v)),
      removeItem: (k: string) => void store.delete(k),
      clear: () => store.clear(),
    };
    (globalThis as Record<string, unknown>).window =
      (globalThis as Record<string, unknown>).window ?? {};
    vi.resetModules();
    await import('../../../src/renderer/lib/mock-api');
    api = await import('../../../src/renderer/lib/api');
  });

  it('列表初始为空数组而非假连接器', async () => {
    expect(await api.mcpAPI.list()).toEqual([]);
  });

  it('add → list → disconnect → remove 全链路走真实台账', async () => {
    const row = await api.mcpAPI.add({
      name: '本地台账',
      transport: 'STDIO',
      command: 'node demo.js',
      env: { API_KEY: 'sk-ipc-contract' },
    });
    expect(row.status).toBe('offline');
    expect(row.envKeys).toEqual(['API_KEY']);
    expect(JSON.stringify(row)).not.toContain('sk-ipc-contract');

    const listed = await api.mcpAPI.list();
    expect(listed.map((r) => r.serverId)).toEqual([row.serverId]);

    const off = await api.mcpAPI.disconnect(row.serverId);
    expect(off.status).toBe('offline');
    expect(await api.mcpAPI.tools(row.serverId)).toEqual([]);

    expect(await api.mcpAPI.remove(row.serverId)).toMatchObject({ name: '本地台账' });
    expect(await api.mcpAPI.list()).toEqual([]);
  });

  it('连接请求以可读文案被拒（页面走 errorTextOf，不进入假成功）', async () => {
    const row = await api.mcpAPI.add({ name: '握手', command: 'node h.js' });
    await expect(api.mcpAPI.connect(row.serverId)).rejects.toThrow(/MCP_UNSUPPORTED/);
    expect(await api.mcpAPI.list()).toHaveLength(1);
    await api.mcpAPI.remove(row.serverId);
  });

  it('OAuth 配置可登记；浏览器通道授权/撤销如实拒绝且不伪造令牌', async () => {
    const row = await api.mcpAPI.add({
      name: '远端 OAuth',
      transport: 'streamable_http',
      url: 'https://a.com/mcp',
      auth: {
        authorizationUrl: 'https://auth.example.com/authorize',
        tokenUrl: 'https://auth.example.com/token',
        clientId: 'tt-desktop',
        scopes: ['openid'],
      },
    });
    expect(row.authStatus).toBe('none');
    expect(row.auth?.clientId).toBe('tt-desktop');
    await expect(api.mcpAPI.oauthAuthorize(row.serverId)).rejects.toThrow(/MCP_OAUTH_UNSUPPORTED/);
    await expect(api.mcpAPI.oauthRevoke(row.serverId)).rejects.toThrow(/MCP_OAUTH_UNSUPPORTED/);
    expect(JSON.stringify(await api.mcpAPI.list())).not.toMatch(/accessToken|refreshToken/);
    await api.mcpAPI.remove(row.serverId);
  });

  it('重名登记被拒并透出后端原因（不会静默覆盖既有配置）', async () => {
    const first = await api.mcpAPI.add({ name: 'dup', command: 'node a.js' });
    await expect(api.mcpAPI.add({ name: 'dup', command: 'node b.js' })).rejects.toThrow(
      /MCP_SERVER_EXISTS/,
    );
    const rows = await api.mcpAPI.list();
    expect(rows).toHaveLength(1);
    expect(rows[0].command).toBe('node a.js');
    await api.mcpAPI.remove(first.serverId);
  });
});
