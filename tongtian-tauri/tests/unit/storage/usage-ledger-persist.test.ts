/**
 * 用量台账落盘与来源标记契约（缺陷 D-43a）
 *
 * 引擎侧的用量曾经只在进程内字典累计，「重启清零」是写进注释的事实；配额硬停与
 * 计费因此没有连续依据。现在引擎按「来源 × 用户 × 模型 × 日」追加落盘，本文件守住
 * 三件事：
 * - 落盘真的发生且状态对外可见（不能只在内存里累计却声称已持久化）；
 * - 数据来源可归属：浏览器直连自己声明来源，平台层按消息通道标记，桌面未标记时
 *   由部署方声明默认值——合并桌面台账时只排除明确属于桌面的部分，不静默丢数据；
 * - 引擎新增的 `origins` / `persist` 字段不破坏前端既有解析（同一段台账，两种读法）。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

(globalThis as Record<string, unknown>).window =
  (globalThis as Record<string, unknown>).window ?? {};

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
}

const engineServer = () => read('../../../engine/server.py');
const usageStore = () => read('../../../engine/usage_store.py');
const dataPaths = () => read('../../../engine/data_paths.py');
const engineHttp = () => read('../../../src/renderer/lib/engine-http.ts');

describe('引擎用量台账落盘', () => {
  it('台账按增量日志落盘，重启后重放恢复', () => {
    const src = engineServer();
    expect(src).toContain('self._log = log or us.UsageDeltaLog()');
    expect(src).toContain('def _ensure_loaded(self)');
    expect(src).toContain('self._log.append({"sub": subject, "model": name, "day": day}, delta)');
    expect(src).toContain('if self._log.should_compact():');
    expect(usageStore()).toContain('LEDGER_SCHEMA_VERSION = 1');
    // 落盘是「追加 + 快照合并」，不是每次全量覆盖，符合存储只做增量变更的约束
    expect(usageStore()).toContain('open(path, "a", encoding="utf-8")');
    expect(usageStore()).toContain('os.replace(tmp, path)');
  });

  it('旧的「重启清零」口径已经删除，不再对外宣称内存台账是易失的', () => {
    const src = engineServer();
    expect(src).not.toContain('引擎重启清零');
    expect(src).not.toContain('进程内用量台账');
    expect(src).not.toContain('权威持久化在 Rust 侧');
  });

  it('落盘失败不阻断记账，但一定通过 persist 状态披露', () => {
    const store = usageStore();
    expect(store).toContain('def _note_error(self, message: str)');
    expect(store).toContain('"lastError": self.last_error');
    const src = engineServer();
    for (const endpoint of ['@app.get("/api/usage")', '@app.get("/api/usage/report")',
      '@app.get("/api/usage/export")']) {
      expect(src).toContain(endpoint);
    }
    expect((src.match(/"persist": USAGE_LEDGER\.persist_status\(\)/g) ?? []).length).toBe(3);
  });

  it('来源标记：请求头优先、默认值兜底、未知不静默', () => {
    const store = usageStore();
    expect(store).toContain('ORIGIN_HEADER = "x-tt-client"');
    expect(store).toContain('DEFAULT_ORIGIN_ENV = "TT_ENGINE_USAGE_ORIGIN_DEFAULT"');
    expect(store).toContain('def default_origin() -> str:');
    // 无法识别一律收敛为 unknown：宁可被单列披露，也不误判成桌面已记账而丢弃
    expect(store).toContain('value if value in USAGE_ORIGINS else ORIGIN_UNKNOWN');
    const src = engineServer();
    expect(src).toContain('us.set_usage_origin(request.headers.get(us.ORIGIN_HEADER))');
    expect(src).toContain('def daily_rows(self, period: str = "total"');
    expect(src).toContain('"unattributedTokens": unattributed');
    // 平台层（IM / CLI / 定时）不经 HTTP 中间件，来源按消息通道标记
    expect(read('../../../engine/agent_platform/dispatcher.py')).toContain(
      'us.set_usage_origin(message.channel)'
    );
  });

  it('浏览器直连通道自己声明来源，合并时不会被当成桌面用量剔除', () => {
    expect(engineHttp()).toContain("headers.set('x-tt-client', 'web')");
  });
});

describe('引擎数据根只有一个解析处', () => {
  it('TONGTIANWORK_HOME / TT_DATA_HOME 只在 data_paths.py 里解析', () => {
    const files = [
      'server.py',
      'checkpoint_runtime.py',
      'im_channels.py',
      'usage_store.py',
      'agent_platform/config.py',
      'agent_platform/dispatcher.py',
    ];
    for (const name of files) {
      const src = read(`../../../engine/${name}`);
      expect(src, `${name} 又自己算了一遍数据根`).not.toMatch(/environ\["(TONGTIANWORK_HOME|TT_DATA_HOME)"\]/);
      expect(src).not.toMatch(/os\.environ\.get\("(TONGTIANWORK_HOME|TT_DATA_HOME)"/);
    }
    expect(dataPaths()).toContain('HOME_ENV = "TONGTIANWORK_HOME"');
    expect(dataPaths()).toContain('LEGACY_HOME_ENV = "TT_DATA_HOME"');
  });

  it('检查点、意识目录、models.json、IM 凭据、用量台账都从这里取路径', () => {
    expect(read('../../../engine/checkpoint_runtime.py')).toContain('dp.checkpoints_dir()');
    const src = engineServer();
    expect(src).toContain('return dp.awareness_main_dir()');
    expect(src).toContain('return dp.models_config_file()');
    expect(dataPaths()).toContain('def usage_ledger_file() -> Path:');
    expect(read('../../../engine/im_channels.py')).toContain('return dp.im_config_file()');
  });
});

describe('引擎新增字段不破坏前端解析', () => {
  it('extractUsageRows 忽略 origins，只取既有口径字段', async () => {
    const usage = await import('../../../src/renderer/lib/usage-local');
    const [row] = usage.extractUsageRows([
      {
        modelName: 'qwen-plus', promptTokens: 20, completionTokens: 10, calls: 2,
        lastUsedAtMs: 7, origins: [{ origin: 'web', tokensUsed: 30, calls: 2 }],
      },
    ]);
    expect(row.tokensUsed).toBe(30);
    expect(row.calls).toBe(2);
    expect(row).not.toHaveProperty('origins');
  });

  it('extractReport 带着 persist 块仍然自洽（趋势合计 == 明细合计）', async () => {
    const usage = await import('../../../src/renderer/lib/usage-local');
    const rep = usage.extractReport(
      {
        ok: true,
        period: 'daily',
        generatedAtMs: 111,
        days: [{ day: '2026-10-29', tokensUsed: 30, calls: 2, models: 1 }],
        models: [{ modelName: 'qwen-plus', promptTokens: 20, completionTokens: 10, calls: 2 }],
        persist: { schemaVersion: 1, enabled: true, rows: 1, droppedLines: 0, lastError: '' },
      },
      'daily',
      222
    );
    expect(rep.totals.tokensUsed).toBe(30);
    expect(rep.days[0].tokensUsed).toBe(30);
  });
});
