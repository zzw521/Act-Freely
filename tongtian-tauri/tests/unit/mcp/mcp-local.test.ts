/**
 * MCP 连接器 · 本地通道实现单元测试（任务 #20）
 *
 * vitest 环境为 node（无 window/localStorage），此处注入 Map 版 localStorage 垫片后
 * 以 `vi.resetModules()` 动态导入，使每个用例拿到干净的模块级台账。
 *
 * 覆盖：
 * - 输入规范化与 Rust `mcp::normalize_*` 逐条同错误码（传输/名称/地址/命令/超时/键值对）
 * - 登记落台账的默认态（offline、无工具、不伪造已连接）与重名拒绝
 * - 脱敏边界：序列化结果只含 env/header 的键名，不含值
 * - 移除/断开/工具清单的未知标识与空标识拒绝
 * - 「连接」在浏览器通道如实拒绝（不把未验证显示成已连接）
 * - JSON 配置导入解析（社区 mcpServers 形态 + 非法输入）
 * - 通道分发器路由与未命中兜底；localStorage 持久化、hydrate、损坏数据降级、无存储可用
 */

import { describe, it, expect, vi } from 'vitest';
import { ErrorCode } from '../../../src/shared/contracts';
import { errorMessageOf, errorTextOf } from '../../../src/shared/error-messages';

type LocalMcpModule = typeof import('../../../src/renderer/lib/mcp-local');

const KEY = 'tt:mcp-servers';

let currentStore = new Map<string, string>();

/** Map 版 localStorage 垫片（node 环境无原生实现） */
function installStorage(): Map<string, string> {
  const store = new Map<string, string>();
  const shim = {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
  };
  (globalThis as Record<string, unknown>).localStorage = shim;
  (globalThis as Record<string, unknown>).window =
    (globalThis as Record<string, unknown>).window ?? {};
  currentStore = store;
  return store;
}

function uninstallStorage() {
  delete (globalThis as Record<string, unknown>).localStorage;
}

/** 重新加载模块，得到与当前 localStorage 同步的干净台账 */
async function freshModule(): Promise<LocalMcpModule> {
  vi.resetModules();
  return import('../../../src/renderer/lib/mcp-local');
}

async function freshWithStorage(): Promise<LocalMcpModule> {
  installStorage().clear();
  return freshModule();
}

/** 断言抛出的错误以指定错误码开头（与 Rust `Err("CODE: detail")` 同形态） */
function expectCode(fn: () => unknown, code: string) {
  let msg = '';
  try {
    fn();
  } catch (e) {
    msg = String((e as Error).message ?? e);
  }
  expect(msg).toBeTruthy();
  expect(msg.startsWith(code)).toBe(true);
}

const BASE = 1_700_000_000_000;

describe('输入规范化：与 Rust mcp::normalize_* 同错误码', () => {
  it('传输方式接受社区写法并归一为落库取值', async () => {
    const m = await freshWithStorage();
    expect(m.normalizeTransport('')).toBe('stdio');
    expect(m.normalizeTransport(undefined)).toBe('stdio');
    expect(m.normalizeTransport('STDIO')).toBe('stdio');
    expect(m.normalizeTransport('SSE')).toBe('sse');
    expect(m.normalizeTransport('STREAMABLE_HTTP')).toBe('streamable_http');
    expect(m.normalizeTransport('Streamable-HTTP')).toBe('streamable_http');
    expect(m.normalizeTransport('http')).toBe('streamable_http');
    expectCode(() => m.normalizeTransport('websocket'), 'MCP_INVALID_TRANSPORT');
  });

  it('名称去空白、拒绝空值与超长（Rust 同样上限 60）', async () => {
    const m = await freshWithStorage();
    expect(m.normalizeName('  钉钉文档  ')).toBe('钉钉文档');
    expectCode(() => m.normalizeName('   '), 'MCP_INVALID_NAME');
    expectCode(() => m.normalizeName('a'.repeat(61)), 'MCP_INVALID_NAME');
  });

  it('地址必须 http(s) 且无空格', async () => {
    const m = await freshWithStorage();
    expect(m.normalizeUrl(' https://mcp.example.com/mcp ')).toBe('https://mcp.example.com/mcp');
    expectCode(() => m.normalizeUrl(''), 'MCP_INVALID_URL');
    expectCode(() => m.normalizeUrl('ftp://127.0.0.1'), 'MCP_INVALID_URL');
    expectCode(() => m.normalizeUrl('http://a b'), 'MCP_INVALID_URL');
  });

  it('stdio 命令非空且不含换行（换行会被当成额外参数注入）', async () => {
    const m = await freshWithStorage();
    expect(m.normalizeCommand(' npx -y demo ')).toBe('npx -y demo');
    expectCode(() => m.normalizeCommand(''), 'MCP_INVALID_COMMAND');
    expectCode(() => m.normalizeCommand('node a.js\nrm -rf /'), 'MCP_INVALID_COMMAND');
  });

  it('超时缺省 60 秒、上限 600 秒（与 Rust normalize_timeout 同界）', async () => {
    const m = await freshWithStorage();
    expect(m.normalizeTimeout(undefined)).toBe(60);
    expect(m.normalizeTimeout(0)).toBe(60);
    expect(m.normalizeTimeout(-3)).toBe(60);
    expect(m.normalizeTimeout(9999)).toBe(600);
    expect(m.normalizeTimeout(30)).toBe(30);
  });

  it('参数数组丢弃空白项，键值对丢弃空键名', async () => {
    const m = await freshWithStorage();
    expect(m.normalizeArgs(['a', '  ', undefined as unknown as string, 'b'])).toEqual(['a', 'b']);
    expect(m.normalizeArgs(null)).toEqual([]);
    expect(m.normalizeMap({ A: '1', '  ': 'x', B: '' })).toEqual({ A: '1', B: '' });
  });
});

describe('登记：真实台账 + 默认未连接 + 凭据脱敏', () => {
  it('stdio 登记后为 offline、工具清单为空、不伪造已连接', async () => {
    const m = await freshWithStorage();
    const row = m.addLocalServer({ name: '文件系统', command: 'npx -y server-fs' }, BASE);
    expect(row.serverId.startsWith('mcp-')).toBe(true);
    expect(row.transport).toBe('stdio');
    expect(row.url).toBe('');
    expect(row.status).toBe('offline');
    expect(row.connected).toBe(false);
    expect(row.tools).toEqual([]);
    expect(row.timeoutSecs).toBe(60);
    expect(row.createdAtMs).toBe(BASE);
    expect(row.updatedAtMs).toBe(BASE);
    expect(row.lastError).toBeUndefined();
    expect(m.listLocalServers().length).toBe(1);
  });

  it('远程登记必须填地址，stdio 才校验命令（与 Rust 分支一致）', async () => {
    const m = await freshWithStorage();
    const row = m.addLocalServer(
      { name: '远端', transport: 'streamable_http', url: 'https://a.com/mcp', command: 'ignored' },
      BASE,
    );
    expect(row.transport).toBe('streamable_http');
    expect(row.url).toBe('https://a.com/mcp');
    expectCode(
      () => m.addLocalServer({ name: '缺地址', transport: 'sse' }, BASE),
      'MCP_INVALID_URL',
    );
    expectCode(
      () => m.addLocalServer({ name: '缺命令', transport: 'stdio', url: 'https://a.com' }, BASE),
      'MCP_INVALID_COMMAND',
    );
    // 被拒的两次登记不得留下脏行
    expect(m.listLocalServers().map((r) => r.name)).toEqual(['远端']);
  });

  it('序列化只暴露 env/header 键名，连接器密钥值不出现在返回结构里', async () => {
    const m = await freshWithStorage();
    const row = m.addLocalServer(
      {
        name: '带密钥',
        command: 'node server.js',
        env: { API_KEY: 'sk-local-secret' },
        headers: { Authorization: 'Bearer leaked-token' },
      },
      BASE,
    );
    expect(row.envKeys).toEqual(['API_KEY']);
    expect(row.headerKeys).toEqual(['Authorization']);
    const wire = JSON.stringify(row);
    expect(wire).not.toContain('sk-local-secret');
    expect(wire).not.toContain('leaked-token');
  });

  it('重名拒绝且不覆盖既有配置（Rust MCP_SERVER_EXISTS 同口径）', async () => {
    const m = await freshWithStorage();
    const first = m.addLocalServer({ name: 'dup', command: 'node a.js' }, BASE);
    expectCode(() => m.addLocalServer({ name: 'dup', command: 'node b.js' }, BASE + 1000), 'MCP_SERVER_EXISTS');
    const after = m.listLocalServers();
    expect(after.length).toBe(1);
    expect(after[0].command).toBe('node a.js');
    expect(after[0].createdAtMs).toBe(first.createdAtMs);
  });

  it('env/header 值含换行直接拒绝（避免注入出额外的键值条目）', async () => {
    const m = await freshWithStorage();
    expectCode(
      () => m.addLocalServer({ name: 'bad', command: 'node a.js', env: { A: 'x\nY: 1' } }, BASE),
      'MCP_INVALID_HEADER',
    );
    expectCode(
      () =>
        m.addLocalServer(
          { name: 'bad2', command: 'node a.js', headers: { A: 'x\rY' } },
          BASE,
        ),
      'MCP_INVALID_HEADER',
    );
    expect(m.listLocalServers()).toEqual([]);
  });

  it('列表按名称字典序，便于稳定渲染', async () => {
    const m = await freshWithStorage();
    for (const name of ['zeta', 'alpha', 'mid']) {
      m.addLocalServer({ name, command: `node ${name}.js` }, BASE);
    }
    expect(m.listLocalServers().map((r) => r.name)).toEqual(['alpha', 'mid', 'zeta']);
  });
});

describe('移除 / 断开 / 工具清单', () => {
  it('移除只删这一条登记，其余配置保持不动', async () => {
    const m = await freshWithStorage();
    const a = m.addLocalServer({ name: 'a', command: 'node a.js' }, BASE);
    m.addLocalServer({ name: 'b', command: 'node b.js' }, BASE);
    const res = m.removeLocalServer(a.serverId);
    expect(res).toEqual({ ok: true, serverId: a.serverId, name: 'a' });
    expect(m.listLocalServers().map((r) => r.name)).toEqual(['b']);
  });

  it('未知标识报 MCP_SERVER_NOT_FOUND，空标识报 MCP_INVALID_ID', async () => {
    const m = await freshWithStorage();
    expectCode(() => m.removeLocalServer('ghost'), 'MCP_SERVER_NOT_FOUND');
    expectCode(() => m.disconnectLocalServer(''), 'MCP_INVALID_ID');
    expectCode(() => m.toolsOfLocalServer(undefined), 'MCP_INVALID_ID');
  });

  it('断开清除状态与工具缓存但保留配置（幂等，不伪造会话）', async () => {
    const m = await freshWithStorage();
    const row = m.addLocalServer({ name: 'keep', command: 'node k.js', url: 'x' }, BASE);
    const after = m.disconnectLocalServer(row.serverId, BASE + 5000);
    expect(after.status).toBe('offline');
    expect(after.connected).toBe(false);
    expect(after.tools).toEqual([]);
    expect(after.command).toBe('node k.js');
    expect(after.updatedAtMs).toBe(BASE + 5000);
    expect(m.disconnectLocalServer(row.serverId, BASE + 9000).status).toBe('offline');
  });

  it('未连接时工具清单为空数组（不返回示例工具）', async () => {
    const m = await freshWithStorage();
    const row = m.addLocalServer({ name: 't', command: 'node t.js' }, BASE);
    expect(m.toolsOfLocalServer(row.serverId)).toEqual([]);
  });
});

describe('浏览器通道的「连接」如实拒绝', () => {
  it('握手被拒为 MCP_UNSUPPORTED，且文案指向桌面版', async () => {
    const m = await freshWithStorage();
    const row = m.addLocalServer({ name: 'fs', command: 'node fs.js' }, BASE);
    let msg = '';
    try {
      m.connectLocalServer(row.serverId);
    } catch (e) {
      msg = String((e as Error).message);
    }
    expect(msg.startsWith('MCP_UNSUPPORTED')).toBe(true);
    expect(msg).toContain('fs');
    expect(errorTextOf(msg, '连接器握手失败')).toContain('桌面');
    // 拒绝不应把状态改成已连接
    expect(m.listLocalServers()[0].status).toBe('offline');
  });

  it('MCP_UNSUPPORTED 与 MCP_INVALID_CONFIG 均有用户文案（不得裸奔）', async () => {
    await freshWithStorage();
    expect(errorMessageOf(ErrorCode.MCP_UNSUPPORTED)).not.toContain('发生错误（');
    expect(errorMessageOf(ErrorCode.MCP_INVALID_CONFIG)).toContain('JSON');
  });
});

describe('JSON 配置导入解析', () => {
  const stdioJson = JSON.stringify({
    mcpServers: {
      '文件服务': { command: 'npx', args: ['-y', 'server-fs'], env: { API_KEY: 'sk-import-secret' } },
      '远端服务': { type: 'streamable_http', url: 'https://a.com/mcp', headers: { Authorization: 'Bearer t' } },
    },
  });

  it('mcpServers 映射形态：逐条解析出可登记的请求', async () => {
    const m = await freshWithStorage();
    const reqs = m.parseMcpJsonConfig(stdioJson);
    expect(reqs.length).toBe(2);
    expect(reqs[0]).toMatchObject({
      name: '文件服务',
      transport: 'stdio',
      command: 'npx',
      args: ['-y', 'server-fs'],
    });
    expect(reqs[1]).toMatchObject({ name: '远端服务', transport: 'streamable_http' });
  });

  it('解析结果可直接登记，密钥留在台账、清单只给键名', async () => {
    const m = await freshWithStorage();
    for (const req of m.parseMcpJsonConfig(stdioJson)) m.addLocalServer(req, BASE);
    const rows = m.listLocalServers();
    expect(rows.map((r) => r.name).sort()).toEqual(['文件服务', '远端服务']);
    expect(JSON.stringify(rows)).not.toContain('sk-import-secret');
    expect(rows.find((r) => r.name === '文件服务')?.envKeys).toEqual(['API_KEY']);
  });

  it('数组形态与单对象形态同样支持', async () => {
    const m = await freshWithStorage();
    const arr = m.parseMcpJsonConfig(
      JSON.stringify([{ name: 'a', command: 'node a.js' }, { name: 'b', url: 'https://b.com/mcp' }]),
    );
    expect(arr.map((r) => r.name)).toEqual(['a', 'b']);
    expect(arr[1].transport).toBe('streamable_http');
    const one = m.parseMcpJsonConfig(JSON.stringify({ name: 'solo', command: 'node s.js' }));
    expect(one).toHaveLength(1);
  });

  it('servers 数组形态兼容，且忽略无名条目', async () => {
    const m = await freshWithStorage();
    const reqs = m.parseMcpJsonConfig(
      JSON.stringify({ servers: [{ command: 'node a.js', name: 'x' }, { command: 'orphan' }] }),
    );
    expect(reqs.map((r) => r.name)).toEqual(['x']);
  });

  it('非法输入逐条拒绝：空文本/坏 JSON/非对象/无连接器', async () => {
    const m = await freshWithStorage();
    expectCode(() => m.parseMcpJsonConfig('   '), 'MCP_INVALID_CONFIG');
    expectCode(() => m.parseMcpJsonConfig('{oops'), 'MCP_INVALID_CONFIG');
    expectCode(() => m.parseMcpJsonConfig('123'), 'MCP_INVALID_CONFIG');
    expectCode(() => m.parseMcpJsonConfig('{"mcpServers": {"": { "command": "x" }}}'), 'MCP_INVALID_CONFIG');
  });
});

describe('通道分发器路由', () => {
  it('六个 mcp:* 通道均由本地权威接管，其他通道交回上层', async () => {
    const m = await freshWithStorage();
    const add = m.handleLocalMcpChannel('mcp:addServer', [
      { name: 'routed', command: 'node r.js' },
    ]);
    expect(add.handled).toBe(true);
    const row = add.value as { serverId: string };
    expect((m.handleLocalMcpChannel('mcp:listServers', []).value as unknown[]).length).toBe(1);
    expect((m.handleLocalMcpChannel('mcp:listTools', [{ serverId: row.serverId }]).value as unknown[])).toEqual([]);
    expect(m.handleLocalMcpChannel('mcp:disconnect', [{ id: row.serverId }]).handled).toBe(true);
    expectCode(() => m.handleLocalMcpChannel('mcp:connect', [{ serverId: row.serverId }]), 'MCP_UNSUPPORTED');
    expectCode(
      () => m.handleLocalMcpChannel('mcp:removeServer', [{ serverId: 'ghost' }]),
      'MCP_SERVER_NOT_FOUND',
    );
    expect(m.handleLocalMcpChannel('skill:list', []).handled).toBe(false);
  });

  it('未命中通道返回 handled=false，不会被误吞成空结果', async () => {
    const m = await freshWithStorage();
    expect(m.handleLocalMcpChannel('settings:get', [])).toEqual({ handled: false });
  });
});

describe('台账持久化', () => {
  it('登记写入 localStorage，模块重载后 hydrate 恢复', async () => {
    const store = installStorage();
    const m = await freshModule();
    m.addLocalServer({ name: 'persist', command: 'node p.js', env: { K: 'v' } }, BASE);
    expect(JSON.parse(store.get(KEY) ?? '[]')).toHaveLength(1);

    const reloaded = await freshModule();
    const rows = reloaded.listLocalServers();
    expect(rows.map((r) => r.name)).toEqual(['persist']);
    expect(rows[0].envKeys).toEqual(['K']);
    expect(rows[0].status).toBe('offline');

    reloaded.removeLocalServer(rows[0].serverId);
    expect((await freshModule()).listLocalServers()).toEqual([]);
  });

  it('损坏台账降级为空，不影响后续登记', async () => {
    installStorage().set(KEY, '{not json');
    const m = await freshModule();
    expect(m.listLocalServers()).toEqual([]);
    expect(m.addLocalServer({ name: 'ok', command: 'node ok.js' }, BASE).name).toBe('ok');
  });

  it('无 localStorage（隐私模式）时内存台账仍可用', async () => {
    uninstallStorage();
    const m = await freshModule();
    const row = m.addLocalServer({ name: 'memory', command: 'node m.js' }, BASE);
    expect(m.listLocalServers().map((r) => r.serverId)).toEqual([row.serverId]);
    installStorage();
  });
});
