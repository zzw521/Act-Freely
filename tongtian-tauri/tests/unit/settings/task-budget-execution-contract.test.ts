/**
 * 单任务级预算执行点契约（缺陷 D-14.1 / D-14.2）
 *
 * 这一条缺陷的旧状态是「界面上能填预算，但没有任何地方按任务拦」——
 * 只有台账、没有执行点，等于没做。本文件把「执行点确实在执行面上」钉成源码契约：
 * - 计量点：引擎与内置 harness 共用 `persist_and_meter`，按 task_id 累计并判跨越
 * - 拦停点 A（内置执行体）：`run_loop` 轮首在下一轮模型调用之前检查
 * - 拦停点 B（Python 引擎）：`stream_post` 读到上限即断流并让引擎 cancel
 * - 状态：只转「命中」的那一个任务（转移 #17），且不重复甩 status_change
 * - 唤回：approve 拒绝超预算任务；resume 接受 budget_paused，但仍超预算/日额度满时拒绝
 * - 文案：不承诺自动恢复（必须人工调高 + 点「继续」）
 *
 * 断言用源码子串与先后顺序：退回半成品（只剩台账、或只在一条执行面上拦）会直接红。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

/** 取 from 之后、to 之前（或文件尾）的那一段；找不到起点直接红，不静默放过 */
function slice(src: string, from: string, to?: string): string {
  const i = src.indexOf(from);
  expect(i, `源码里找不到「${from}」，接线改名要同步这里`).toBeGreaterThanOrEqual(0);
  const j = to ? src.indexOf(to, i + from.length) : -1;
  return j < 0 ? src.slice(i) : src.slice(i, j);
}

const rustQuota = () => read('../../../src-tauri/src/task/quota.rs');
const rustAgent = () => read('../../../src-tauri/src/bridge/agent.rs');
const rustRunner = () => read('../../../src-tauri/src/harness/runner.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const contracts = () => read('../../../src/shared/contracts.ts');
const types = () => read('../../../src/shared/types.ts');
const quotaGate = () => read('../../../src/shared/quota-gate.ts');
const store = () => read('../../../src/renderer/store/workbenchStore.ts');
const panel = () => read('../../../src/renderer/components/TokenBudgetPanel/index.tsx');
const workbench = () => read('../../../src/renderer/pages/Workbench/index.tsx');
const messages = () => read('../../../src/shared/error-messages.ts');

describe('台账：按任务累计，与日额度分账但同一册', () => {
  it('上限与按任务用量持久化在同一份台账里，且有条数/时长上界', () => {
    const src = rustQuota();
    expect(src).toContain('per_task_limit_tokens: u64');
    expect(src).toContain('pub fn add_task_usage(&self, task_id: &str, tokens: u64) -> TaskBudgetTransition');
    expect(src).toContain('pub fn task_snapshot(&self, task_id: &str) -> TaskBudgetSnapshot');
    expect(src).toContain('pub fn task_budget_blocked(&self, task_id: &str) -> bool');
    expect(src).toContain('pub fn forget_task(&self, task_id: &str)');
    // 记账条数有上界、有过期，否则台账只增不减
    expect(src).toContain('const MAX_TRACKED_TASKS: usize = 500;');
    expect(src).toContain('const TASK_TTL_MS: u64 = 7 * 86_400_000;');
  });

  it('四档判定复用日额度的同一函数，不为单任务另造阈值', () => {
    const of = slice(rustQuota(), 'fn snapshot_of(', '\nfn ');
    expect(of).toContain('evaluate(');
    expect(of).toContain('blocked');
  });

  it('调高上限不自动唤回：只有跨越上限那一刻才产出 Exceeded', () => {
    const add = slice(rustQuota(), 'pub fn add_task_usage', '    pub fn task_budget_blocked');
    expect(add).toContain('TaskBudgetTransition::WithinBudget');
    expect(add).toContain('before >= limit');
    expect(add).toContain('TaskBudgetTransition::Exceeded(snapshot_of(task_id, &g))');
  });
});

describe('计量点：两条执行面共用一处按任务累计', () => {
  it('usage 落账处按任务累计，跨越上限即转本任务为预算暂停', () => {
    const meter = slice(rustAgent(), 'async fn persist_and_meter(', 'pub(crate) async fn pause_running_task');
    expect(meter).toContain('gate.add_task_usage(task_id, tokens)');
    expect(meter).toContain('TaskBudgetTransition::Exceeded(tsnap)');
    expect(meter).toContain('"scope": "task"');
    expect(meter).toContain('pause_running_task(');
    // 日额度那一路仍然要标出归属，界面才知道这不是同一本账
    expect(meter).toContain('"scope": "daily"');
  });

  it('暂停只转活跃态任务，终态与已暂停的不重复甩事件', () => {
    const pause = slice(rustAgent(), 'pub(crate) async fn pause_running_task(', '\n}\n');
    for (const state of ['"queued"', '"planning"', '"executing"', '"delivering"']) {
      expect(pause).toContain(state);
    }
    expect(pause).toContain('crate::task::status::BUDGET_PAUSED');
    expect(pause).toContain('Some("BUDGET_EXHAUSTED")');
    expect(pause).toContain('"stepId": "budget-pause"');
  });

  it('引擎流：读到上限即断流并让引擎取消，且不再补拉事件', () => {
    const src = rustAgent();
    const stream = slice(src, 'async fn stream_post(', '#[cfg(test)]');
    expect(stream).toContain('let mut budget_stopped = false;');
    expect(stream).toContain('if !saw_done && task_budget_blocked(&p0, task_id)');
    expect(stream).toContain('cancel_engine_task(port, task_id, "BUDGET_TASK_EXCEEDED").await;');
    // 补拉会把刚拦住的下一轮又捞回来，必须排除
    expect(stream).toContain('if stream_broken && !saw_done && !budget_stopped');
    expect(stream).toContain('if !stream_broken && !budget_stopped');
    // 拦停函数确实指向引擎的 cancel 端点
    const cancel = slice(src, 'async fn cancel_engine_task(', 'async fn forward_events');
    expect(cancel).toContain('/api/chat/cancel');
    // D-14.5：拦停与用户取消在引擎侧不许再走同一条说法——请求体带成因码
    expect(cancel).toContain('"reason": reason');
  });

  it('内置执行体：轮首检查，连压缩用的模型调用也不发', () => {
    const runner = rustRunner();
    const loop = slice(runner, 'async fn run_loop(', '\n}\n');
    expect(loop).toContain('if em.budget_blocked() {');
    expect(loop).toContain('return (End::BudgetPaused, cp);');
    // 检查点在轮首，早于任何一次模型调用
    expect(loop.indexOf('if em.budget_blocked() {')).toBeLessThan(loop.indexOf('model::stream('));
    const execute = slice(runner, 'pub async fn execute(', 'async fn persist(');
    expect(execute).toContain('End::BudgetPaused => {');
    expect(execute).toContain('cp.mark_final(true, "单任务预算已用满，可从最近一次模型调用前的检查点继续")');
    // 这里钉的是「执行体收尾确实经 agent::pause_running_task 把在跑的任务转暂停」，
    // 参数是按值还是按引用不是不变量（execute 早先 own p0/tx，后来改收引用，
    // 逐字钉borrow写法会让一次纯签名调整把这条契约打成假红）。
    expect(execute).toContain('agent::pause_running_task(&slot.task_id, p0, tx, &reason).await;');
    // 正常收尾的任务当场清计数，别在台账里堆回不来的条目
    expect(execute).toContain('gate.forget_task(&slot.task_id)');
  });
});

describe('唤回（D-14.2）：人工调高后才允许继续', () => {
  it('设置保存与启动都会把单任务预算应用到台账（改后立刻生效）', () => {
    const lib = rustLib();
    expect(lib).toContain('fn budget_per_task_tokens(value: &Value) -> Option<u64>');
    expect(lib).toContain('p0.quota.set_per_task_limit(tokens)');
    expect(lib).toContain('services.quota.set_per_task_limit(tokens)');
    // 读取端仍兼容只写日额度的老设置：缺键不猜数字
    expect(slice(lib, 'fn budget_tokens(', '\n}\n')).toContain('as_u64');
  });

  it('批准执行不接受仍超预算的任务', () => {
    const approve = slice(rustLib(), 'if p0.quota.task_budget_blocked(&task_id) {', '\n            }');
    expect(approve).toContain('BUDGET_TASK_EXCEEDED');
    expect(approve).toContain('不能批准执行');
  });

  it('恢复允许 budget_paused，但预算没调高/日额度仍满时拒绝', () => {
    const resume = slice(rustLib(), 'let paused_by_budget = record.status', 'let previous_status = record.status.clone()');
    expect(resume).toContain('status::DONE | status::FAILED | status::CANCELLED');
    expect(resume).toContain('|| paused_by_budget');
    expect(resume).toContain('if paused_by_budget && p0.quota.task_budget_blocked(&task_id)');
    expect(resume).toContain('if let Err(code) = p0.quota.check_submit()');
    expect(resume).toContain('仍是超预算状态，不能唤回');
    expect(resume).toContain('仅 done/failed/cancelled/预算暂停可恢复');
  });

  it('被预算停下的流收尾时不写成 done：状态留给「预算暂停」', () => {
    const settle = slice(rustLib(), 'fn settle_stream_status(', '\n}\n');
    expect(settle).toContain('status::BUDGET_PAUSED');
    expect(rustLib()).toContain('settle_stream_status(app, services, task_id, cancel_requested, status::DONE)');
  });
});

describe('前端契约：两本账不混，暂停态不被改写', () => {
  it('预算设置对象有单任务档，出厂为不限；事件带 scope', () => {
    const src = contracts();
    expect(slice(src, 'export interface BudgetSettings', '\n}')).toContain('perTaskTokens: number');
    expect(slice(src, 'export const DEFAULT_BUDGET', '};')).toContain('perTaskTokens: 0');
    expect(slice(src, "| { type: 'quota_warning'", '\n')).toContain("scope?: 'daily' | 'task'");
    // 与 shared/types.ts 的镜像保持同一形状，否则 store 拿不到 scope
    expect(slice(types(), "| { type: 'quota_warning'", '\n')).toContain("scope?: 'daily' | 'task'");
    expect(panel()).toContain('perTaskTokens: nums.perTask');
  });

  it('任务档预警不覆盖日额度快照', () => {
    expect(slice(store(), "case 'quota_warning'", "case 'checkpoint'")).toContain(
      "if (event.scope === 'task') return base;",
    );
  });

  it('收尾的 done 不把预算暂停改写成已完成，但要放开 isLoading', () => {
    const done = slice(store(), "case 'done'", "case 'status_change'");
    expect(done).toContain('const pausedByBudget = conv.taskStatus === TaskStatus.BUDGET_PAUSED;');
    expect(done).toContain(
      'taskStatus: terminal || preExecution || pausedByBudget ? conv.taskStatus : TaskStatus.DONE',
    );
    // 拦停后仍要点「继续」：isLoading 卡住的话恢复会被自己的闸门挡住
    expect(done).toContain('isLoading: preExecution ? conv.isLoading : false');
  });

  it('暂停态的继续入口说明「不会自动重置，需调高后点这里」', () => {
    const tip = slice(workbench(), 'taskStatus === TaskStatus.BUDGET_PAUSED &&', '\n                  <Box');
    expect(tip).toContain('单任务预算');
    expect(tip).toContain('模型与用量');
    expect(tip).toContain('不调高点了也会被拒');
  });

  it('面板与错误文案不再说「还没有执行点」，也不承诺自动恢复', () => {
    const src = panel();
    expect(src).not.toContain('还没有执行点');
    expect(src).not.toContain('目前还没有执行点');
    expect(src).toContain("label: '单任务预算（Token）'");
    expect(src).toContain('不会到点自动重置');
    const exhausted = slice(messages(), '[ErrorCode.BUDGET_EXHAUSTED]', '\n');
    expect(exhausted).toContain('继续');
    expect(exhausted).toContain('不会自动');
    // 等待原因同样不许承诺「自动继续」
    expect(slice(store(), 'budget_wait:', '\n', )).toContain('不会自动恢复');
    expect(quotaGate()).toContain('export function judgeTaskBudget');
  });
});
