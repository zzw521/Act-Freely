/**
 * Super-agent 影子段前端契约（缺陷 D-46.5）
 *
 * 这一层锁的是「页面看到的东西只有一份口径，而且它没有动手的能力」：
 *
 * - **三层接线完整**：`shared/contracts.ts` 定通道名 → `renderer/lib/api.ts` 定方法 →
 *   `renderer/lib/tauri-api.ts` 接到 Rust command。任一层漏一个通道，页面就会静默拿到
 *   `IPC_CHANNEL_UNHANDLED` 或 undefined，这里必须先红。
 * - **前端没有开工入口**：`superagentAPI` 暴露的方法是白名单，多一个「采纳并开工」就失败。
 *   影子模式的产物只有清单，要开工由人在会话里正常发起（D-47/D-48 之前的口径）。
 *   第二层（模型研判）在这一条上也不例外：`assessNow` 只问「值不值得开工」并写回判定行，
 *   它不许拿到任何执行能力，也不许在巡检按钮里偷偷花钱（见下面「第二层」那组用例）。
 * - **诚实降级**：Mock 与浏览器直连通道对 `superagent:*` 一律显式拒绝（不返回空清单冒充
 *   「本机确实没事件」），拒绝文案与 `shared/error-messages.ts` 的中文文案同源。
 * - **显示名只有一处生产**：`kindLabel` / `strengthLabel` / `modeLabel` / `statusLabel`
 *   全部由 Rust 给；前端不写第二套翻译，避免出现「同一个入口在两个地方叫不同名字」。
 * - **错误口径（D-20）**：Rust 的 `Err(String)` 到渲染层是裸字符串 `CODE: 中文`，
 *   页面只许用 `errorTextOf`，不许 `instanceof Error` / `String(e)` / `${err}`。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  handleLocalSuperagentChannel,
  SUPERAGENT_DESKTOP_ONLY_MESSAGE,
} from '../../../src/renderer/lib/superagent-local';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const contracts = () => read('../../../src/shared/contracts.ts');
const api = () => read('../../../src/renderer/lib/api.ts');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const engineApi = () => read('../../../src/renderer/lib/engine-api.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const errorMessages = () => read('../../../src/shared/error-messages.ts');
const page = () => read('../../../src/renderer/pages/SuperAgent/index.tsx');
const libRs = () => read('../../../src-tauri/src/lib.rs');
const modRs = () => read('../../../src-tauri/src/superagent/mod.rs');
const app = () => read('../../../src/renderer/App.tsx');
const sidebar = () => read('../../../src/renderer/components/Layout/Sidebar.tsx');

/** 14 条通道：键名、通道值、Rust command 名（三者必须一一对应） */
const CHANNELS: Array<[key: string, channel: string, command: string]> = [
  ['SUPERAGENT_STATS', 'superagent:stats', 'superagent_stats'],
  ['SUPERAGENT_LIST_SOURCES', 'superagent:listSources', 'superagent_list_sources'],
  ['SUPERAGENT_REGISTER_SOURCE', 'superagent:registerSource', 'superagent_register_source'],
  ['SUPERAGENT_SET_SOURCE_ENABLED', 'superagent:setSourceEnabled', 'superagent_set_source_enabled'],
  ['SUPERAGENT_LIST_CANDIDATES', 'superagent:listCandidates', 'superagent_list_candidates'],
  ['SUPERAGENT_LIST_DECISIONS', 'superagent:listDecisions', 'superagent_list_decisions'],
  ['SUPERAGENT_LIST_TRIGGERS', 'superagent:listTriggers', 'superagent_list_triggers'],
  ['SUPERAGENT_GET_TRIGGER', 'superagent:getTrigger', 'superagent_get_trigger'],
  ['SUPERAGENT_GET_POLICY', 'superagent:getPolicy', 'superagent_get_policy'],
  ['SUPERAGENT_SAVE_POLICY', 'superagent:savePolicy', 'superagent_save_policy'],
  ['SUPERAGENT_POLICY_VERSIONS', 'superagent:policyVersions', 'superagent_policy_versions'],
  ['SUPERAGENT_PUMP_NOW', 'superagent:pumpNow', 'superagent_pump_now'],
  ['SUPERAGENT_ASSESS_NOW', 'superagent:assessNow', 'superagent_assess_now'],
  ['SUPERAGENT_EXPORT_CANDIDATES', 'superagent:exportCandidates', 'superagent_export_candidates'],
];

/** `superagentAPI` 对象正文（从声明处到闭合 `};`），用于白名单与调用面检查 */
function superagentApiBlock(): string {
  const text = api();
  const start = text.indexOf('export const superagentAPI');
  expect(start, 'api.ts 应导出 superagentAPI').toBeGreaterThan(-1);
  const rest = text.slice(start);
  const end = rest.indexOf('\n};');
  expect(end, 'superagentAPI 应有闭合').toBeGreaterThan(-1);
  return rest.slice(0, end);
}

describe('通道表：一个通道只在一个地方定义', () => {
  it('contracts.ts 里恰好登记这 14 条 superagent 通道', () => {
    const text = contracts();
    for (const [key, channel] of CHANNELS) {
      expect(text, `缺通道 ${key}`).toContain(`${key}: '${channel}'`);
    }
    // 别在 IPC_CHANNELS 之外再造通道字面量（类型名用大写 Superagent，不计入）
    expect([...text.matchAll(/'superagent:[^']*'/g)]).toHaveLength(CHANNELS.length);
  });

  it('桌面通道把 14 条全部接到 Rust command，命名与参数对齐', () => {
    const text = tauriApi();
    for (const [key, , command] of CHANNELS) {
      expect(text, `tauri-api 未接 ${command}`).toContain(`invoke('${command}'`);
      expect(text, `tauri-api 未按 ${key} 分发`).toContain(`IPC_CHANNELS.${key}`);
    }
    expect([...text.matchAll(/invoke\('superagent_/g)]).toHaveLength(CHANNELS.length);
    // 列表类命令统一用 limit 可空（缺省由 Rust 决定取多少），登记类命令把 config 原样透传
    expect(text).toContain("superagent_list_candidates', { limit: (args[0] as number | undefined) ?? null }");
    expect(text).toContain('config: req.config ?? {}');
    expect(text).toContain('enabled: req.enabled === true');
  });

  it('Rust 侧 14 条 command 都注册在 generate_handler 里（前端接了但没注册=运行时找不到）', () => {
    const text = libRs();
    for (const [, , command] of CHANNELS) {
      expect(text, `lib.rs 未注册 ${command}`).toContain(`superagent::${command}`);
    }
  });
});

describe('前端方法面：影子模式没有开工入口', () => {
  it('superagentAPI 暴露的方法就是白名单这 15 个（多一个执行类方法即失败）', () => {
    const methods = [...superagentApiBlock().matchAll(/^  (?:async )?([A-Za-z]+)\(/gm)].map(
      (m) => m[1]
    );
    expect(methods).toEqual([
      'stats',
      'listSources',
      'registerSource',
      'setSourceEnabled',
      'listCandidates',
      'listDecisions',
      'listTriggers',
      'getTrigger',
      'getPolicy',
      'savePolicy',
      'listPolicyVersions',
      'pumpNow',
      'assessNow',
      'exportCandidates',
      'onPump',
    ]);
  });

  it('superagentAPI 只调用这 14 条通道，且整组挂在 api.superagent 下', () => {
    const used = [...superagentApiBlock().matchAll(/IPC_CHANNELS\.([A-Z_]+)/g)].map((m) => m[1]);
    expect([...new Set(used)].sort()).toEqual(CHANNELS.map(([key]) => key).sort());
    expect(api()).toContain('superagent: superagentAPI,');
  });

  it('页面只调白名单方法，不出现提交任务/起工作区那类动作', () => {
    const src = page();
    const called = [...src.matchAll(/superagentAPI\.([A-Za-z]+)\(/g)].map((m) => m[1]);
    expect(called.length, '页面应至少调用一条通道').toBeGreaterThan(0);
    const allowed = new Set([
      'stats',
      'listSources',
      'registerSource',
      'setSourceEnabled',
      'listCandidates',
      'listDecisions',
      'listTriggers',
      'getTrigger',
      'getPolicy',
      'savePolicy',
      'listPolicyVersions',
      'pumpNow',
      'assessNow',
      'exportCandidates',
      'onPump',
    ]);
    for (const name of called) expect(allowed.has(name), `页面调了白名单外的 ${name}`).toBe(true);
    // 起任务在别处只有这一条路：这一页不该摸到会话提交或工作区启动
    for (const banned of ['chatAPI', 'submitTask', 'startWorkspace', 'createTask']) {
      expect(src, `页面不得出现 ${banned}`).not.toContain(banned);
    }
  });

  it('自动接单在界面上显示为不可用并写明缺哪一段，而不是留个会报错的开关', () => {
    const src = page();
    expect(src).toContain('自动接单：暂不可用');
    expect(src).toContain('{view.autoBlockedBy}');
    // 档位只有 off / observe 两枚按钮可选（auto 由 Rust 拒绝写入，前端不给入口）
    expect(src).toContain("{(['off', 'observe'] as const).map(");
    expect(src).not.toMatch(/acceptMode:\s*'auto'/);
    // 当前档位显示的是 Rust 给的 modeLabel，不是前端自己拼的
    expect(src).toMatch(/view\.modeLabel|stats\?\.modeLabel/);
  });
});

describe('显示名只有一份口径', () => {
  it('页面消费 Rust 给的 *Label，不自己写第二套翻译函数', () => {
    const src = page();
    for (const label of ['kindLabel', 'statusLabel', 'strengthLabel', 'modeLabel', 'skippedLabel']) {
      expect(src, `页面应消费 ${label}`).toContain(label);
    }
    expect(src).not.toMatch(/function\s+(kindLabel|modeLabel|strengthLabel|statusLabel)\s*\(/);
    // 入口类型清单与可读性都来自策略视图，不在前端硬编码一张 kind→中文 表
    expect(src).toContain('view.sourceKinds');
    expect(src).toContain('item.readable ? ');
    expect(src).not.toMatch(/project_event:\s*'[^']*(事件源|项目事件)'/);
  });

  it('Rust 侧确实生产这些显示名，且事件名只有一条', () => {
    const rs = modRs();
    for (const fn of [
      'pub fn source_kind_label',
      'pub fn strength_label',
      'pub fn mode_label',
      'pub fn pump_status_label',
      'pub fn label_pump_summary',
    ]) {
      expect(rs, `superagent/mod.rs 缺 ${fn}`).toContain(fn);
    }
    expect(rs).toContain('EVENT_SUPERAGENT: &str = "superagent-event"');
    expect(tauriApi()).toContain("subscribeEvent<SuperagentPumpSummary>('superagent-event'");
  });

  it('后台循环与「立即巡检」共用同一份显示名加工（两条路径形状一致）', () => {
    const sources = read('../../../src-tauri/src/superagent/sources.rs');
    expect(sources).toContain('crate::superagent::label_pump_summary');
    expect(modRs()).toContain('label_pump_summary(&summary)');
  });
});

describe('候选清单导出（验收项③）：影子模式的交付物就是那份文件', () => {
  /** mod.rs 的生产代码（测试模块之前），用来区分「导出干了什么」和「测试读回文件验证」 */
  function modProduction(): string {
    const text = modRs();
    const cut = text.indexOf('#[cfg(test)]');
    expect(cut, 'mod.rs 应有测试模块标记').toBeGreaterThan(-1);
    return text.slice(0, cut);
  }

  /** `export_candidates_to` 到它后面那条 command 之间：只包导出的组装过程 */
  function exportAssembly(): string {
    const text = modProduction();
    const start = text.indexOf('pub fn export_candidates_to');
    expect(start, 'mod.rs 应有可测的 export_candidates_to').toBeGreaterThan(-1);
    const rest = text.slice(start);
    const end = rest.indexOf('#[tauri::command]');
    expect(end, 'export_candidates_to 后应跟着导出 command').toBeGreaterThan(-1);
    return rest.slice(0, end);
  }

  it('导出只读账本：不重读工作区、不再研判一次，格式与落点由 Rust 定', () => {
    const rs = modProduction();
    expect(rs).toContain('SUPERAGENT_EXPORT_FORMAT_UNSUPPORTED');
    expect(rs).toContain('crate::store::db::runtime_home().join("exports")');
    // 文件名带时间戳：导出不覆盖上一次那份留痕
    expect(rs).toContain('dir.join(export_file_name(format, now))');
    const assembly = exportAssembly();
    expect(assembly).toContain('ledger::list_candidates(');
    for (const banned of ['read_to_string', 'read_dir', 'pump_once']) {
      expect(assembly, `导出组装不许 ${banned}`).not.toContain(banned);
    }
  });

  it('文件里带着影子模式的声明与 Rust 显示名，界面不自己造第二套口径', () => {
    // 这句话必须写进文件本身：拿走 CSV 的人不该把它误读成「这些活 Agent 已经做了」
    expect(modProduction()).toContain('但没有一条被执行');
    const assembly = exportAssembly();
    expect(assembly).toContain('EXPORT_NOTE');
    expect(assembly).toContain('with_source_kind_label');
    expect(assembly).toContain('with_strength_label');
    expect(assembly).toContain('ledger::effective_policy');
    // 错误码要有中文文案，否则界面显示裸 CODE（D-20）
    const msgs = errorMessages();
    for (const code of ['SUPERAGENT_EXPORT_FORMAT_UNSUPPORTED', 'SUPERAGENT_EXPORT_FAILED']) {
      expect(msgs, `error-messages.ts 缺 ${code} 文案`).toContain(`${code}: '`);
    }
  });

  it('前端只转述路径与条数：不拼路径、不走浏览器下载', () => {
    const src = page();
    expect(src).toContain("handleExport('json')");
    expect(src).toContain("handleExport('csv')");
    expect(src).toContain('导出 JSON');
    expect(src).toContain('导出 CSV');
    expect(src).toContain('{lastExport.path}');
    expect(src).toContain('{lastExport.note}');
    // 路径由 Rust 拼：前端自己拼落点或用浏览器下载兜一层，都是在造第二套口径
    for (const banned of ['createObjectURL', 'TONGTIAN_HOME', "join('exports'"]) {
      expect(src, `页面不得出现 ${banned}`).not.toContain(banned);
    }
    // 方法签名只认这两种格式，前端传不出第三个格式
    expect(api()).toContain("exportCandidates(format: 'json' | 'csv' = 'json')");
    expect(tauriApi()).toContain('format: (args[0] as string | undefined) ?? null');
  });
});

describe('错误口径（D-20）与诚实降级', () => {
  it('页面只用 errorTextOf，不自己拆错误对象', () => {
    const src = page();
    expect(src).toContain('errorTextOf(');
    expect(src).not.toContain('instanceof Error');
    expect(src).not.toMatch(/String\(\s*(e|err|error)\s*\)/);
    expect(src).not.toMatch(/\$\{(e|err|error)\}/);
  });

  it('本地通道对每一条 superagent 通道都显式拒绝，别的通道不接管', async () => {
    for (const [, channel] of CHANNELS) {
      const hit = handleLocalSuperagentChannel(channel);
      expect(hit.handled, `${channel} 应被本地通道接管`).toBe(true);
      await expect(hit.value as Promise<unknown>).rejects.toThrow(/SUPERAGENT_DESKTOP_ONLY/);
    }
    expect(handleLocalSuperagentChannel('awareness:memoryList').handled).toBe(false);
  });

  it('拒绝文案与错误码表同源（改一处必须改两处，否则界面显示裸 CODE）', () => {
    const mapped = errorMessages().match(/SUPERAGENT_DESKTOP_ONLY:\s*'([^']+)'/);
    expect(mapped, 'error-messages.ts 需登记 SUPERAGENT_DESKTOP_ONLY 文案').not.toBeNull();
    const detail = SUPERAGENT_DESKTOP_ONLY_MESSAGE.slice(
      SUPERAGENT_DESKTOP_ONLY_MESSAGE.indexOf(': ') + 2
    );
    expect(detail).toBe(mapped![1]);
  });

  it('引擎直连与 Mock 都挂上本地拒绝（否则会落到 IPC_CHANNEL_UNHANDLED 兜底）', () => {
    for (const [name, src] of [
      ['engine-api.ts', engineApi()],
      ['mock-api.ts', mockApi()],
    ] as const) {
      expect(src, `${name} 应导入 superagent-local`).toContain(
        "import { handleLocalSuperagentChannel } from './superagent-local'"
      );
      expect(src, `${name} 应调用本地拒绝`).toContain('handleLocalSuperagentChannel(channel)');
    }
  });
});

describe('页面可达', () => {
  it('路由与侧栏入口都登记了（写好了但进不去=没有这一页）', () => {
    expect(app()).toContain("const SuperAgent = lazy(() => import('./pages/SuperAgent'))");
    expect(app()).toContain('<Route path="/superagent" element={<SuperAgent />} />');
    const nav = sidebar();
    expect(nav).toContain("path: '/superagent'");
    expect(nav).toContain("label: '主动候选'");
  });

  it('四个页签都在，且读不到台账时说明原因而不是显示空清单', () => {
    const src = page();
    for (const tab of ['主动候选', '事件入口', '准入策略', '判定留痕']) {
      expect(src, `缺页签 ${tab}`).toContain(`'${tab}'`);
    }
    expect(src).toContain('setUnavailable(');
    // 订阅后台巡检摘要：桌面有这条流就跟着刷新
    expect(src).toContain('superagentAPI.onPump(');
  });

  it('登记 ≠ 开启读取 ≠ 授权：三件事在界面上各自说清楚，不合并成一个开关', () => {
    const src = page();
    // 登记弹窗里就声明默认不读取、不授权（否则用户以为登记完就有了）
    expect(src).toContain('登记后新入口默认不读取，也不会自动获得授权');
    expect(src).toContain('要产生候选还需：开启读取 + 在准入策略里授权');
    // 事件入口页脚说明「读取开关」与「策略授权」是两处，取消一个不停另一个
    expect(src).toContain('这里的开关只管「读不读」');
    // 候选为空时解释缺哪一件，而不是只写「暂无数据」
    expect(src).toContain('候选只在同时满足三件事时才会出现');
  });
});

describe('第二层（模型研判）：只问「值不值得开工」，不越权、不重复花钱', () => {
  const assessRs = () => read('../../../src-tauri/src/superagent/assess.rs');
  const ledgerRs = () => read('../../../src-tauri/src/superagent/ledger.rs');
  const migrateRs = () => read('../../../src-tauri/src/store/migrate.rs');

  /** assess.rs 的生产代码（测试模块之前）：区分「这一层做了什么」和「测试怎么验」 */
  function assessProduction(): string {
    const text = assessRs();
    const cut = text.indexOf('#[cfg(test)]');
    expect(cut, 'assess.rs 应有测试模块标记').toBeGreaterThan(-1);
    return text.slice(0, cut);
  }

  it('研判层没有任何执行能力：不碰会话提交、不起工作区、不开进程，也不自己写表', () => {
    const rs = assessProduction();
    for (const banned of ['bridge::agent', 'Command::new', 'start_workspace', 'submit_task', 'create_task']) {
      expect(rs, `assess.rs 不许出现 ${banned}`).not.toContain(banned);
    }
    // 表名只归 ledger 管（ownership-contract 锁死那五个文件）
    expect(rs).not.toMatch(/\bsa_/);
    expect(rs).toContain('ledger::record_assessment(');
  });

  it('一笔回写只发生一次：队列与回写都只认还没判过的行', () => {
    const ledger = ledgerRs();
    // 队列：只捞没判过的，且限定「当前排在哪个版本的研判队列里」这个键
    // （不是 gate 的 policy_version——留痕版本永不改写，见下面重排那组用例）
    expect(ledger).toContain(" AND d.assess_status = '' AND d.assess_queue_version = ?2");
    // 回写：条件更新，抢不到就不算失败，也不算第二次花钱
    expect(ledger).toContain("WHERE id = ?9 AND assess_status = ''");
    // 分子按「判过」算：done 与 failed 都真的花过钱
    expect(ledger).toContain("assess_status IN ('done', 'failed')");
  });

  it('配额阻断与模型不可用只进本轮摘要，不写进判定行', () => {
    const rs = assessProduction();
    expect(rs).toContain('matches!(self.status.as_str(), STATUS_DONE | STATUS_FAILED)');
    expect(rs).toContain('if let Err(code) = quota.check_submit()');
    expect(rs).toContain('tally.unavailable += 1');
    // V17 的建表注释同样要说清这件事，否则后来人以为库里有 blocked
    expect(migrateRs()).toContain('blocked 与 unavailable 只进巡检摘要，不写库');
  });

  it('钱先记再解析：调用一旦发出，token 与用量都入账', () => {
    const rs = assessProduction();
    const ask = rs.slice(rs.indexOf('async fn ask('));
    const metered = ask.indexOf('quota.add_usage(');
    const usage = ask.indexOf('crate::model::usage::record_sample_quiet(');
    const parsed = ask.indexOf('parse_assessment(');
    expect(metered).toBeGreaterThan(-1);
    expect(usage).toBeGreaterThan(-1);
    expect(parsed).toBeGreaterThan(-1);
    expect(metered, 'add_usage 要在解析结论之前').toBeLessThan(parsed);
    expect(usage, '用量落库要在解析结论之前').toBeLessThan(parsed);
  });

  it('预算在策略里、分子在账本里：前端不参与算还有没有钱', () => {
    const rs = assessProduction();
    expect(rs).toContain('doc.assess_cap()');
    expect(rs).toContain('ledger::count_assessed_today(store)');
    expect(rs).toContain('SUPERAGENT_ASSESS_MODEL_MISSING');
    const src = page();
    for (const banned of ['assessCap - ', 'assessedToday +', 'dailyEvalCap -']) {
      expect(src, `页面不得自己算预算（${banned}）`).not.toContain(banned);
    }
  });

  it('手动巡检不花模型钱，只有后台循环与「立即研判」会发起调用', () => {
    const sources = read('../../../src-tauri/src/superagent/sources.rs');
    const start = sources.indexOf('pub async fn pump_once(');
    expect(start).toBeGreaterThan(-1);
    const body = sources.slice(start);
    const pumpBody = body.slice(0, body.indexOf('\n}\n'));
    expect(pumpBody.length).toBeGreaterThan(100);
    expect(pumpBody, 'pump_once 不许调模型研判').not.toContain('assess::run');
    // 循环里由策略预算把关（不是每轮无条件判）
    expect(sources).toContain(
      'summary["assessment"] = crate::superagent::assess::run(&store, &quota, None).await'
    );
    expect(modRs()).toContain('pub async fn superagent_assess_now(');
    expect(libRs()).toContain('superagent::superagent_assess_now');
    expect(page()).toContain('superagentAPI.assessNow()');
    expect(page()).toContain('立即研判存量候选');
  });

  it('研判结论的显示名也只有一处生产：Rust 给 label，页面只消费', () => {
    const mod = modRs();
    expect(mod).toContain('fn with_assess_label');
    expect(mod).toContain('row["assessVerdictLabel"]');
    expect(mod).toContain('row["assessStatusLabel"]');
    expect(assessRs()).toContain('pub fn verdict_label');
    expect(assessRs()).toContain('pub fn status_label');
    const src = page();
    for (const field of [
      'assessStatusLabel',
      'assessVerdictLabel',
      'assessReason',
      'assessModel',
      'assessTokens',
    ]) {
      expect(src, `页面应消费 ${field}`).toContain(field);
    }
    // 前端不写第二套结论翻译（结论词到中文的映射只在 Rust 有一份）
    expect(src).not.toMatch(/assessVerdict === '[a-z_]+' \?/);
    expect(src).not.toMatch(/function\s+(verdictLabel|assessStatusLabel)\s*\(/);
  });

  it('第二层的错误码全部有中文文案（否则界面显示裸 CODE，D-20）', () => {
    const msgs = errorMessages();
    for (const code of [
      'SUPERAGENT_ASSESS_MODEL_MISSING',
      'SUPERAGENT_ASSESS_TIMEOUT',
      'SUPERAGENT_ASSESS_FAILED',
      'SUPERAGENT_ASSESS_WRITE_FAILED',
      'SUPERAGENT_ASSESS_COUNT_FAILED',
      'SUPERAGENT_POLICY_REQUEUE_FAILED',
    ]) {
      expect(msgs, `error-messages.ts 缺 ${code} 文案`).toContain(`${code}:`);
    }
  });

  it('策略里给第二层留了预算入口，且 0 的含义写在界面上', () => {
    const src = page();
    expect(src).toContain('assessMaxPerDay');
    expect(src).toContain('模型研判预算');
    expect(src).toContain('填 0 就是关掉这一层');
    expect(contracts()).toContain('assessMaxPerDay?: number');
    // 摘要面板读 Rust 的字段，不自己数
    for (const key of [
      'assessSummary.attempted',
      'assessSummary.worth',
      'assessSummary.notWorth',
      'assessSummary.skipReason',
    ]) {
      expect(src, `页面应显示 ${key}`).toContain(key);
    }
  });

  it('策略升版只重排待研判的正向候选，gate 的留痕版本一个字不改', () => {
    const ledger = ledgerRs();
    // 重排的三个条件各挡一类不该动的行：没判过 / 阳性 / 不已经在当前队列
    expect(ledger).toContain(
      "WHERE assess_status = '' AND would_accept = 1 AND assess_queue_version <> ?1"
    );
    // 只挪队列键与重排时刻：policy_version 不出现在这条 UPDATE 的 SET 里
    expect(ledger).toContain('SET assess_queue_version = ?1, requeued_at_ms = ?2');
    expect(ledger).not.toMatch(/SET assess_queue_version[^"]*policy_version/);
    // 重排只有一个调用点，且两条分支（新增版本 / 内容没变）都汇到它：幂等，所以内容没变时
    // 正常是 0 条，但上一次保存重排失败或 V17 留下的落后候选能被同一次保存自愈
    expect(ledger.match(/requeue_pending_decisions\(c, version, now_ms\(\)\)\?;/g)).toHaveLength(1);
    expect(ledger).toContain('changed = false;');
    // 新判定落库时把队列键初始化成 gate 版本，否则这一版队列永远是空的
    expect(ledger).toContain('assess_queue_version)');
    // 「还欠几条没问」与取队列读同一个键，界面不会少报；重排过的条数单独给一个数
    expect(ledger).toContain(
      "WHERE would_accept = 1 AND assess_status = '' AND assess_queue_version = ?1"
    );
    expect(ledger).toContain('WHERE requeued_at_ms > 0');
  });

  it('在跑的那轮结论被丢弃时只报数：三态判定在 ledger，作废计数在摘要', () => {
    const ledger = ledgerRs();
    const rs = assessProduction();
    // 回写多带一个「发起这一轮时读到的队列版本」，版本变了这条就不写
    expect(ledger).toContain("WHERE id = ?9 AND assess_status = '' AND assess_queue_version = ?8");
    for (const arm of ['Written', 'AlreadyJudged', 'Superseded']) {
      expect(ledger, `回写三态缺 ${arm}`).toContain(`AssessmentWrite::${arm}`);
    }
    expect(rs).toContain('Ok(ledger::AssessmentWrite::Superseded) => tally.superseded += 1');
    // 作废条数进摘要，且文案如实说明那几次调用的钱已经花掉、不退
    expect(rs).toContain('summary["superseded"]');
    expect(rs).toContain('token 已经花掉，不退');
    // 丢弃结论不等于拿到执行能力：这一层照旧不碰表、不起进程
    expect(rs).not.toMatch(/\bsa_/);
  });

  it('重排与作废的中文只在 Rust 生产一份，页面与导出只转述', () => {
    const mod = modRs();
    const rs = assessProduction();
    const src = page();
    // 候选行上的「改排到哪个版本队列」是一句成品文案，不是前端拼的
    expect(mod).toContain('row["requeueLabel"] = json!');
    expect(mod).toContain('因策略升版改排在 v{queue_version} 的研判队列');
    expect(contracts()).toContain('requeueLabel?: string');
    expect(src).toContain('item.requeueLabel');
    expect(src).not.toMatch(/判定依据 v\$\{|改排在 v/);
    // 保存策略与立即研判的结果文案同样来自 Rust：requeued / requeueNote / superseded
    expect(mod).toContain('"requeued": outcome.requeued');
    expect(mod).toContain('"requeueNote"');
    expect(contracts()).toContain('requeued: number');
    expect(contracts()).toContain('superseded: number');
    expect(src).toContain('outcome.requeueNote');
    expect(src).toContain('summary.superseded');
    expect(src).toContain("['策略重排', `${stats.requeued}`]");
    // 保存之前就讲清后果：留痕不动、存量候选按新版重排、已研判的不重判
    expect(src).toContain('已研判过的不重判（不为同一个判断再花一次钱）');
    // 导出列带上队列版本与重排文案，别人拿走文件也能看出这条被升版重排过
    expect(mod).toContain('"assessQueueVersion",');
    expect(mod).toContain('"requeueLabel",');
    // 作废的结论不算进本轮「写回成功」的数：两个计数各记各的
    expect(rs).toContain('Ok(ledger::AssessmentWrite::Written) => tally.written += 1');
    expect(rs).toContain('if tally.superseded > 0');
  });
});
