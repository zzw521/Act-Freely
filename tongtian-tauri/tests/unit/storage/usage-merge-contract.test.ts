/**
 * 用量合并口径契约（缺陷 D-43c）
 *
 * D-43 要求「同一批用量对外只提供一个合并视图，两处口径一致且不重复计数」。
 * 引擎导出（`/api/usage/export`）、服务端入库与合并视图（`tongtian-server/app/usage.py`）、
 * 桌面台账（Rust `model::usage`）与前端换算（`usage-local`）四处各自算一遍，
 * 只要字段名、来源清单或取整方式漂移，数值就会在用户眼前对不上——而这类漂移
 * 在单一层的测试里全是绿的。跨层数值一致性由
 * `tests/integration/engine_usage_report_selftest.py` 真实跑通，本文件守住的是
 * 「形状与口径」：新增字段没同步、改了取整、换了去重规则，这里先红。
 */

import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
}

const engineServer = () => read('../../../engine/server.py');
const usageStore = () => read('../../../engine/usage_store.py');
const rustUsage = () => read('../../../src-tauri/src/model/usage.rs');
const usageLocal = () => read('../../../src/renderer/lib/usage-local.ts');
const contracts = () => read('../../../src/shared/contracts.ts');
const serverUsage = () => read('../../../../tongtian-server/app/usage.py');
const serverMain = () => read('../../../../tongtian-server/app/main.py');
const serverDb = () => read('../../../../tongtian-server/app/db.py');
const serverTests = () => read('../../../../tongtian-server/tests/test_server.py');
const pipelineSelftest = () =>
  read('../../integration/engine_usage_report_selftest.py');

/** 取 `def daily_rows(...)` 里 `out.append({...})` 的顶层键（嵌套字典的键不算） */
function exportRowKeys(src: string): string[] {
  const at = src.indexOf('def daily_rows(');
  const marker = src.slice(at).indexOf('out.append({');
  if (at < 0 || marker < 0) return [];
  let i = at + marker + 'out.append('.length;
  let depth = 0;
  const keys: string[] = [];
  for (; i < src.length; i += 1) {
    const ch = src[i];
    if (ch === '{' || ch === '[') {
      depth += 1;
    } else if (ch === '}' || ch === ']') {
      depth -= 1;
      if (depth === 0) break;
    } else if (depth === 1 && ch === '"') {
      const key = /^"(\w+)":/.exec(src.slice(i, i + 40));
      if (key) keys.push(key[1]);
    }
  }
  return keys;
}

/** 取 Pydantic 模型（`class X(BaseModel):` 到下一个类）的字段名 */
function pyFields(src: string, className: string): string[] {
  const header = `class ${className}(BaseModel):`;
  const start = src.indexOf(header);
  if (start < 0) return [];
  const body = src.slice(start + header.length).split('\nclass ')[0];
  return [...body.matchAll(/^ {4}(\w+):/gm)].map((m) => m[1]);
}

/** 取 Python 来源元组：既接受字面量，也接受 `ORIGIN_XXX` 常量引用 */
function originTuple(src: string, name: string): string[] {
  const consts = new Map(
    [...src.matchAll(/^(ORIGIN_\w+) = "([^"]+)"/gm)].map((m) => [m[1], m[2]] as const)
  );
  const body = src.split(`${name} = (`)[1]?.split(')')[0] ?? '';
  return [...body.matchAll(/[A-Za-z_][A-Za-z0-9_]*|"[^"]+"/g)].map((m) => {
    const token = m[0];
    return token.startsWith('"') ? token.slice(1, -1) : (consts.get(token) ?? token);
  });
}

/** 取 DDL 主键列与 ON CONFLICT 目标列 */
function pkColumns(src: string, table: string): string[] {
  const body = src.split(`CREATE TABLE IF NOT EXISTS ${table}`)[1]?.split(');')[0] ?? '';
  const line = body.split('\n').find((l) => l.trim().startsWith('PRIMARY KEY'));
  return line ? [...line.matchAll(/(\w+)/g)].map((m) => m[1]).slice(2) : [];
}

function conflictTarget(src: string): string[] {
  const body = src.split('ON CONFLICT(')[1]?.split(')')[0] ?? '';
  return body.split(',').map((item) => item.trim()).filter(Boolean);
}

let usage: typeof import('../../../src/renderer/lib/usage-local');

beforeAll(async () => {
  usage = await import('../../../src/renderer/lib/usage-local');
});

describe('上报报文形状：引擎导出 ↔ 服务端入参 ↔ 前端契约', () => {
  it('引擎导出行的键与服务端上报入参字段完全同名', () => {
    const exported = exportRowKeys(engineServer());
    const accepted = pyFields(serverUsage(), 'UsageRowIn');
    expect(exported.length).toBeGreaterThan(10);
    // 两边必须一字不差：任何一侧改名或漏字段，上报就会被 extra_forbidden 拒收
    expect(accepted.slice().sort()).toEqual(exported.slice().sort());
    // 桌面台账用的展示名不参与服务端合并（服务端没有模型配置，不伪造展示名）
    expect(accepted).not.toContain('displayName');
  });

  it('合并视图的输出字段与前端契约同名同义', () => {
    const view = serverUsage();
    // 汇总（UsageTotals）
    for (const field of [
      'promptTokens', 'completionTokens', 'tokensUsed', 'calls', 'activeModels',
      'lastUsedAtMs', 'avgLatencyMs', 'latencySamples', 'costs', 'pricedCalls', 'unpricedCalls',
    ]) {
      expect(view, `服务端汇总缺少契约字段 ${field}`).toContain(`"${field}"`);
    }
    // 报表三块与 UsageReport 同名
    for (const block of ['totals', 'models', 'days', 'period', 'generatedAtMs']) {
      expect(view).toContain(`"${block}"`);
    }
    expect(contracts()).toContain('export interface UsageTotals');
    expect(contracts()).toContain('export interface UsageDayPoint');
    // 服务端多出的来源披露块：契约里没有，页面也就无从伪造
    for (const disclosure of [
      'desktopRows', 'engineRows', 'engineDedupedRows', 'engineDedupedTokens',
      'gatewayRows', 'gatewayDedupedRows', 'gatewayDedupedTokens',
      'unattributedTokens', 'unattributedCalls', 'reporters',
    ]) {
      expect(view).toContain(`"${disclosure}"`);
    }
  });

  it('上报入参一律非负、周期非法拒绝（不静默降级）', () => {
    const view = serverUsage();
    expect(view).toContain('ge=0');
    expect(view).toContain('USAGE_INVALID_PERIOD');
    expect(view).toContain('USAGE_TOKENS_MISMATCH');
    expect(view).toContain('USAGE_INVALID_SOURCE');
    expect(view).toContain('USAGE_SCHEMA_UNSUPPORTED');
    // tokensUsed 由服务端按 prompt+completion 判定，不与来值各算一套
    expect(view).toContain('row.tokensUsed is not None and row.tokensUsed != tokens');
  });
});

describe('来源清单与合并规则', () => {
  it('引擎与服务端的来源清单一字不差', () => {
    const engine = originTuple(usageStore(), 'USAGE_ORIGINS');
    const server = originTuple(serverUsage(), 'ORIGINS');
    expect(engine).toEqual([
      'desktop', 'web', 'im', 'cli', 'schedule', 'gateway', 'unknown',
    ]);
    expect(server).toEqual(engine);
  });

  it('只有引擎侧标为 desktop 的样本才排除，经网关的客户端副本同样排除', () => {
    const view = serverUsage();
    expect(view).toContain('source == SOURCE_ENGINE and origin == ORIGIN_DESKTOP');
    // 经网关的调用以网关账本为准，引擎/桌面那份是同一请求的副本
    expect(view).toContain('source != SOURCE_GATEWAY and origin == ORIGIN_GATEWAY');
    // 未标记不静默丢弃：计入总量并单列
    expect(view).toContain('origin == ORIGIN_UNKNOWN');
    expect(view).toContain('unattributedTokens');
    // 引擎导出保持全来源，去重只在合并视图做
    expect(engineServer().split('def daily_rows(')[1].split('def tokens_used')[0])
      .not.toContain('ORIGIN_DESKTOP');
  });

  it('桌面行统一写 desktop 来源，服务端行按设备各存一份', () => {
    const view = serverUsage();
    expect(view).toContain('if source == SOURCE_DESKTOP:');
    expect(view).toContain('return ORIGIN_DESKTOP');
    expect(conflictTarget(view)).toEqual(pkColumns(serverDb(), 'usage_rollup'));
    expect(serverDb()).toContain('device_id');
  });

  it('上报与查询都按 JWT 到人，普通用户不能越界', () => {
    const view = serverUsage();
    // 上报逐行校验 sub；查询直接取 JWT 里的 uid，调用方无从指定别人的 sub
    expect(view.match(/_assert_scope\(user, sub\)/g)?.length).toBe(1);
    expect(view).toContain('raise ApiError(403, "USAGE_SCOPE_DENIED"');
    expect(view).toContain('sub = _clean_id(str(user.get("uid") or ""), "sub")');
    expect(view).not.toMatch(/def api_usage_me\([^)]*sub: str/);
    expect(view).toContain('def api_usage_user(');
    expect(view).toContain('Depends(require_admin)');
    expect(view).toContain('merged_view(conn, target, p, now_ms())');
    expect(view).toContain('merged_view(conn, sub, p, now_ms())');
  });
});

describe('平均耗时三处同口径：一律向下取整', () => {
  it('Rust / 引擎 / 服务端用整除，前端用 Math.floor', () => {
    expect(rustUsage()).toContain('latency_total / latency_samples');
    expect(rustUsage()).toContain('weighted_latency / t.latency_samples');
    const engine = engineServer();
    expect(engine).toContain('total // samples if samples else None');
    expect(engine).toContain('weighted // samples if samples else None');
    expect(serverUsage()).toContain('total // samples if samples else None');
    const local = usageLocal();
    expect(local).toContain('Math.floor(r.latencyMsTotal / r.latencySamples)');
    expect(local).toContain('Math.floor(p.latencyMsTotal / p.latencySamples)');
    expect(local).toContain('Math.floor(weightedLatency / totals.latencySamples!)');
    // 四舍五入会让同一份台账在不同通道差出 1ms，报表对不上账
    expect(local).not.toMatch(/Math\.round\((weightedLatency|p\.latencyMsTotal|r\.latencyMsTotal)/);
    expect(engine).not.toMatch(/avgLatencyMs"\] = round\(/);
  });

  it('汇总按已取整的模型均值加权（三处同一算法）', () => {
    expect(rustUsage()).toContain('r.avg_latency_ms.unwrap_or(0) * r.latency_samples');
    expect(usageLocal()).toContain('(r.avgLatencyMs ?? 0) * samples');
    expect(serverUsage()).toContain('weighted_latency += int(item["avgLatencyMs"] or 0)');
  });

  it('行为：3 个样本共 1000ms 得 333，不是 334', () => {
    const now = Date.UTC(2026, 9, 8, 12);
    usage.resetLocalUsage();
    usage.recordLocalUsage('qwen-odd', 10, 5, { atMs: now, latencyMs: 333 });
    usage.recordLocalUsage('qwen-odd', 10, 5, { atMs: now, latencyMs: 333 });
    usage.recordLocalUsage('qwen-odd', 10, 5, { atMs: now, latencyMs: 334 });
    const point = usage.localDayPoints('daily', now)[0];
    expect(point.latencySamples).toBe(3);
    expect(point.avgLatencyMs).toBe(333);
    const view = usage.localReportView('daily', now);
    expect(view.totals.avgLatencyMs).toBe(333);
    usage.resetLocalUsage();
  });
});

describe('服务端接入与归属', () => {
  it('路由已注册且先于市场通配路由', () => {
    const main = serverMain();
    expect(main).toContain('from .usage import router as usage_router');
    expect(main.indexOf('include_router(usage_router)')).toBeLessThan(
      main.indexOf('include_router(market_router)')
    );
  });

  it('建表只做增量，服务端不打开别家的库', () => {
    const db = serverDb();
    expect(db).toContain('CREATE TABLE IF NOT EXISTS usage_rollup');
    expect(db).not.toMatch(/DROP TABLE/i);
    const view = serverUsage();
    // 用量走服务端自己的连接，不引入 sqlite3 直连、也不引用桌面库/引擎台账
    expect(view).not.toContain('import sqlite3');
    expect(view).not.toMatch(/["'][^"']*\.db["']/);
    expect(view).not.toMatch(/["'][^"']*\.jsonl["']/);
    expect(view).toContain('with tx() as conn');
  });

  it('PRD 6.5 写的是真实端点与真实披露字段', () => {
    const section = read('../../../../docs/PRD-任我行.md').split('### 6.5 存储归属')[1].split('\n## 7')[0];
    expect(section).toContain('POST /api/v1/usage/report');
    expect(section).toContain('GET /api/v1/usage/me');
    expect(section).toContain('unattributedTokens');
    expect(section).toContain('重复上报不重复计数');
    expect(section).toContain('服务端不读桌面库与引擎台账文件');
  });

  it('端到端自测把两处对齐：引擎导出直接喂服务端', () => {
    const selftest = pipelineSelftest();
    expect(selftest).toContain('daily_rows("total", sub)');
    expect(selftest).toContain('/api/v1/usage/report');
    expect(selftest).toContain('/api/v1/usage/me');
    expect(selftest).toContain('client_side_totals');
    expect(selftest).toContain('model_level_rows');
    expect(selftest).toContain('重复上报不重复计数');
    expect(selftest).toContain('重启后导出台账与重启前一致');
  });

  it('服务端测试覆盖去重、幂等与越权', () => {
    const tests = serverTests();
    expect(tests).toContain('def test_usage_report(ctx)');
    expect(tests).toContain('test_usage_report(ctx)');
    for (const name of [
      '桌面为主：引擎 desktop 行不重复计数',
      '重复上报幂等（累计值覆盖，不重复计数）',
      '不能替他人上报',
      '多设备各算一份，不互相覆盖',
      '无用量时返回零值而不是伪造第一名/耗时/成本',
    ]) {
      expect(tests).toContain(name);
    }
  });
});

describe('桌面上报客户端（D-43 客户端部分）', () => {
  const upload = () => read('../../../src-tauri/src/model/usage_upload.rs');
  const rustLib = () => read('../../../src-tauri/src/lib.rs');
  const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
  const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
  const panel = () => read('../../../src/renderer/components/UsageOverviewPanel/index.tsx');

  it('报文与端点对齐服务端：source/deviceId/schemaVersion 与逐行字段', () => {
    const src = upload();
    expect(src).toContain('/api/v1/usage/report');
    expect(src).toContain('"source": "desktop"');
    expect(src).toContain('"deviceId":');
    expect(src).toContain('"schemaVersion": SCHEMA_VERSION');
    expect(src).toContain('SCHEMA_VERSION: i64 = 1');
    expect(src).toContain('origin: "desktop"');
    expect(src).toContain('tokens_used: prompt + completion');
    expect(src).toContain('bearer_auth(token)');
    // 只上报自己的台账，不把库文件或路径交给服务端
    expect(src).not.toContain('tongtian.db');
    expect(src).not.toContain('std::fs::read');
  });

  it('按人上报、设备 id 稳定、分批且 401 刷新一次', () => {
    const src = upload();
    expect(src).toContain('USAGE_UPLOAD_LOGIN_REQUIRED');
    expect(src).toContain('.get("uid")');
    expect(src).toContain('DEVICE_KEY');
    expect(src).toContain('BATCH_SIZE: usize = 500');
    expect(src).toContain('rows.chunks(BATCH_SIZE)');
    expect(src).toContain('USAGE_UPLOAD_AUTH_EXPIRED');
    expect(src).toContain('force_access_token(store)');
  });

  it('命令、通道、Tauri 映射与启动/手动入口全部接通', () => {
    expect(rustLib()).toContain('async fn usage_upload(');
    expect(rustLib()).toContain('usage_upload,');
    expect(rustLib()).toContain('model::usage_upload::upload_session(&store)');
    const contractsSrc = read('../../../src/shared/contracts.ts');
    expect(contractsSrc).toContain("USAGE_UPLOAD: 'usage:upload'");
    expect(contractsSrc).toContain('export interface UsageUploadResult');
    expect(tauriApi()).toContain("channel === 'usage:upload'");
    expect(tauriApi()).toContain("invoke('usage_upload')");
    expect(mockApi()).toContain("channel === 'usage:upload'");
    // Mock 不假装上报成功
    expect(mockApi()).toContain('uploaded: false');
    expect(panel()).toContain('api.usage.upload()');
    expect(panel()).toContain('上报用量');
  });
});
