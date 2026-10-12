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

  it('六个 command 均已注册进 invoke_handler（前端 invoke 不会落到未注册通道）', () => {
    const lib = rustLib();
    for (const cmd of [
      'mcp_list_servers',
      'mcp_add_server',
      'mcp_remove_server',
      'mcp_connect',
      'mcp_disconnect',
      'mcp_list_tools',
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

describe('渲染层三通道接线', () => {
  it('tauri-api：6 个 mcp:* 通道映射到对应 Rust command', () => {
    const src = tauriApi();
    expect(src).toContain("if (channel === 'mcp:listServers') return invoke('mcp_list_servers')");
    expect(src).toContain("if (channel === 'mcp:addServer') return invoke('mcp_add_server', { req: args[0] })");
    for (const [channel, command] of [
      ['mcp:removeServer', 'mcp_remove_server'],
      ['mcp:connect', 'mcp_connect'],
      ['mcp:disconnect', 'mcp_disconnect'],
      ['mcp:listTools', 'mcp_list_tools'],
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
    expect(src).toContain('s.envKeys.length');
    expect(src).toContain('值仅存本机，不回显');
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
