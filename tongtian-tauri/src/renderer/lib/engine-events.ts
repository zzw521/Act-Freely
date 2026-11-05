/**
 * 引擎 SSE 事件载荷 → 渲染层 ChatEvent 契约的字段归一。
 *
 * 桌面端由 Rust（bridge/sse.rs）改写键名，浏览器直连引擎时没人做这件事：
 * 引擎的步骤与计划事件一直是 snake_case（step_id / plan_id），
 * 而渲染层按 camelCase 取值 —— 后果不是报错，是「匹配不上」：
 * plan.steps[].id 为 undefined、step_done/step_failed 找不到对应步骤、
 * 审阅卡片与单步续跑都拿不到步骤身份。这里补上直连通道缺的那一层。
 */

const STEP_ID_KEYS = ['step_id', 'stepId'] as const;

function firstString(...values: unknown[]): string | undefined {
  for (const v of values) {
    if (typeof v === 'string' && v) return v;
  }
  return undefined;
}

/** 计划步骤：引擎给 {step_id,type,description}，契约要 {id,order,type,description} */
function normalizePlanSteps(raw: unknown): unknown {
  if (!Array.isArray(raw)) return raw;
  return raw.map((item, i) => {
    if (item === null || typeof item !== 'object') return item;
    const step = item as Record<string, unknown>;
    const id = firstString(step.step_id, step.id) ?? `step-${i + 1}`;
    return { ...step, id, order: typeof step.order === 'number' ? step.order : i + 1 };
  });
}

export function normalizeEngineEvent(data: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = { ...data };

  const stepId = firstString(...STEP_ID_KEYS.map((k) => out[k]));
  if (stepId) {
    out.stepId = stepId;
    delete out.step_id;
  }

  const planId = firstString(out.plan_id, out.planId);
  if (planId) {
    out.planId = planId;
    delete out.plan_id;
  }
  if (out.steps !== undefined) out.steps = normalizePlanSteps(out.steps);

  return out;
}
