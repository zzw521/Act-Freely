/**
 * 断点续跑重放判定（D-15b）
 *
 * 续跑的真实语义决定了界面能说什么、不能说什么：检查点回到最后一次模型调用前，
 * 只有登记进副作用台账且已成功的调用会被跳过，失败的那一次一定重做。
 * 本文件锁住判定本身，并锁住与引擎 Python 三张表的一致性 —— 分类不一致
 * 就意味着界面承诺「不会重做」而引擎其实会重做。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import type { ChatEvent } from '../../../src/shared/types';
import {
  READ_ONLY_TOOLS,
  SIDE_EFFECT_KEYWORDS,
  SIDE_EFFECT_TOOLS,
  buildReplayPlan,
  collectReplaySteps,
  confirmWarningZh,
  defaultReplayAnchor,
  replayRiskLabel,
  stepToolName,
  toolReplayRisk,
  unavailableReasonZh,
} from '../../../src/shared/step-replay';

function start(stepId: string, tool: string, extra?: Partial<ChatEvent>): ChatEvent {
  return {
    type: 'step_start',
    stepId,
    stepType: 'tool_call',
    description: `调用工具: ${tool}`,
    tool,
    ...(extra ?? {}),
  } as ChatEvent;
}

function done(stepId: string): ChatEvent {
  return { type: 'step_done', stepId, output: 'ok' } as ChatEvent;
}

function failed(stepId: string, error = '工具执行超时'): ChatEvent {
  return { type: 'step_failed', stepId, error } as ChatEvent;
}

describe('toolReplayRisk：与引擎同样的保守判定', () => {
  it('只读表内为 readonly，副作用表内为 side_effect', () => {
    expect(toolReplayRisk('view_text_file')).toBe('readonly');
    expect(toolReplayRisk('use_skill')).toBe('readonly');
    expect(toolReplayRisk('write_text_file')).toBe('side_effect');
    expect(toolReplayRisk('generate_pptx')).toBe('side_effect');
    expect(toolReplayRisk('spawn_subagents')).toBe('side_effect');
  });

  it('未登记但命中关键词的按副作用（含大小写与 MCP 命名）', () => {
    expect(toolReplayRisk('run_command')).toBe('side_effect');
    expect(toolReplayRisk('Delete_Row')).toBe('side_effect');
    expect(toolReplayRisk('mcp_send_email')).toBe('side_effect');
    expect(toolReplayRisk('jira_create_issue')).toBe('side_effect');
  });

  it('既不在表内也不命中关键词算 unknown，空名同样算 unknown', () => {
    expect(toolReplayRisk('fetch_url')).toBe('unknown');
    expect(toolReplayRisk('mcp_random_tool')).toBe('unknown');
    expect(toolReplayRisk('   ')).toBe('unknown');
  });

  it('风险类别有中文名，界面不用英文', () => {
    expect(replayRiskLabel('readonly')).toContain('只读');
    expect(replayRiskLabel('side_effect')).toContain('改变');
    expect(replayRiskLabel('unknown')).toContain('未登记');
  });
});

describe('stepToolName：桌面转发丢掉 tool 时的兜底', () => {
  it('优先用事件自带的 tool', () => {
    expect(stepToolName({ tool: 'write_text_file', description: '调用工具: other' })).toBe('write_text_file');
  });

  it('没有 tool 时从「调用工具: X」描述里取', () => {
    expect(stepToolName({ description: '调用工具: insert_text_file' })).toBe('insert_text_file');
  });

  it('两者都没有时返回空串（内部步骤不该被当成工具）', () => {
    expect(stepToolName({ description: '注入技能上下文' })).toBe('');
    expect(stepToolName({})).toBe('');
  });
});

describe('collectReplaySteps：从事件流还原本轮步骤', () => {
  it('step_start 建行，step_done / step_failed 按 stepId 覆盖状态', () => {
    const rows = collectReplaySteps([
      start('step-1', 'view_text_file'),
      done('step-1'),
      start('step-2', 'write_text_file'),
      failed('step-2'),
    ]);
    expect(rows.map((r) => [r.stepId, r.state, r.risk])).toEqual([
      ['step-1', 'done', 'readonly'],
      ['step-2', 'failed', 'side_effect'],
    ]);
  });

  it('找不到对应 step_start 的收尾事件被忽略，不凭空造步骤', () => {
    expect(collectReplaySteps([done('step-9'), failed('step-8')])).toEqual([]);
  });

  it('没有 tool 也没有「调用工具」描述的行标为 other，不计入工具重放', () => {
    const rows = collectReplaySteps([{ type: 'step_start', stepId: 'ctx-1', stepType: 'context', description: '压缩上下文' } as ChatEvent]);
    expect(rows[0].kind).toBe('other');
    expect(rows[0].risk).toBe('unknown');
  });

  it('只有 step_start、还没有收尾的行停在 running', () => {
    expect(collectReplaySteps([start('step-1', 'view_text_file')])[0].state).toBe('running');
  });
});

describe('buildReplayPlan：谁能不重做、谁一定会重做', () => {
  const events = [
    start('step-1', 'view_text_file'),
    done('step-1'),
    start('step-2', 'write_text_file'),
    done('step-2'),
    start('step-3', 'generate_pptx'),
    failed('step-3', '模型中断'),
  ];

  it('锚点前已成功的副作用步骤跳过，只读步骤仍会重放', () => {
    const plan = buildReplayPlan(events, 'step-3');
    const byId = new Map(plan.steps.map((s) => [s.stepId, s]));
    expect(byId.get('step-2')!.willReplay).toBe(false);
    expect(byId.get('step-2')!.note).toContain('不重做');
    expect(byId.get('step-1')!.willReplay).toBe(true);
    expect(byId.get('step-1')!.note).toContain('不改变状态');
    expect(plan.skippedCount).toBe(1);
  });

  it('锚点一定重放，即使它此前已完成（快照回到它之前）', () => {
    const plan = buildReplayPlan(events, 'step-2');
    const anchor = plan.steps.find((s) => s.stepId === 'step-2')!;
    expect(anchor.anchor).toBe(true);
    expect(anchor.willReplay).toBe(true);
    expect(anchor.note).toContain('锚点');
  });

  it('失败步骤不受台账保护，重放并要求确认', () => {
    const plan = buildReplayPlan(events, 'step-3');
    expect(plan.needsConfirm).toBe(true);
    expect(plan.confirmTools).toEqual(['generate_pptx']);
    expect(plan.steps.find((s) => s.stepId === 'step-3')!.note).toContain('没有登记进副作用台账');
  });

  it('只读工具重放不需要确认（不改变状态）', () => {
    const readonlyOnly = [start('step-1', 'view_text_file'), done('step-1'), start('step-2', 'view_text_file'), failed('step-2')];
    const plan = buildReplayPlan(readonlyOnly, 'step-2');
    expect(plan.needsConfirm).toBe(false);
    expect(plan.confirmTools).toEqual([]);
  });

  it('未完成（running）的锚点前步骤按重放处理，不假装跳过', () => {
    const plan = buildReplayPlan([start('step-1', 'write_text_file'), start('step-2', 'write_text_file')], 'step-2');
    expect(plan.steps[0].willReplay).toBe(true);
    expect(plan.steps[0].note).toContain('没有确认完成');
  });

  it('未知类别工具会执行且要求确认', () => {
    const plan = buildReplayPlan([start('step-1', 'fetch_url'), done('step-1')], 'step-1');
    expect(plan.needsConfirm).toBe(true);
    expect(plan.confirmTools).toEqual(['fetch_url']);
  });

  it('内部步骤不进工具计数', () => {
    const plan = buildReplayPlan(
      [
        { type: 'step_start', stepId: 'checkpoint-restore', stepType: 'checkpoint', description: '恢复记忆快照' } as ChatEvent,
        done('checkpoint-restore'),
        start('step-1', 'write_text_file'),
        failed('step-1'),
      ],
      'step-1',
    );
    expect(plan.replayCount).toBe(1);
    expect(plan.steps.find((s) => s.stepId === 'checkpoint-restore')!.note).toContain('引擎内部步骤');
  });

  it('同一工具多次要重做时确认清单只列一次', () => {
    const plan = buildReplayPlan(
      [start('step-1', 'write_text_file'), failed('step-1'), start('step-2', 'write_text_file'), failed('step-2')],
      'step-2',
    );
    expect(plan.confirmTools).toEqual(['write_text_file']);
  });

  it('锚点不存在时退回最后一步，不抛异常', () => {
    const plan = buildReplayPlan(events, 'step-does-not-exist');
    expect(plan.anchorStepId).toBe('step-3');
    expect(plan.steps[2].anchor).toBe(true);
  });

  it('空事件流给出如实的空说明', () => {
    const plan = buildReplayPlan([], 'step-1');
    expect(plan.steps).toEqual([]);
    expect(plan.summaryZh).toBe('本轮没有可展示的步骤记录');
    expect(plan.needsConfirm).toBe(false);
  });

  it('汇总与不确定性口径都带在计划里，界面直接展示', () => {
    const plan = buildReplayPlan(events, 'step-3');
    expect(plan.summaryZh).toContain('1 个已成功的写入类步骤按台账不重做');
    expect(plan.summaryZh).toContain('2 个步骤会重新执行');
    expect(plan.caveatZh).toContain('工具名 + 输入参数指纹');
    expect(plan.caveatZh).toContain('不是回到所选步骤本身');
  });
});

describe('锚点挑选与不可用说明', () => {
  it('默认锚点取最后一个失败步骤', () => {
    const anchor = defaultReplayAnchor([
      start('step-1', 'write_text_file'),
      failed('step-1'),
      start('step-2', 'write_text_file'),
      failed('step-2'),
    ]);
    expect(anchor).toBe('step-2');
  });

  it('没有失败步骤时取最后一个有身份的步骤', () => {
    expect(defaultReplayAnchor([start('step-1', 'view_text_file'), done('step-1')])).toBe('step-1');
    expect(defaultReplayAnchor([])).toBe('');
  });

  it('确认文案点名要重做的工具', () => {
    expect(confirmWarningZh([])).toBe('');
    const text = confirmWarningZh(['write_text_file', 'run_command']);
    expect(text).toContain('write_text_file');
    expect(text).toContain('run_command');
    expect(text).toContain('重复写入');
  });

  it('检查点没有原因时给出可用的解释，不留空白', () => {
    expect(unavailableReasonZh('')).toContain('没有留下可续跑的检查点');
    expect(unavailableReasonZh(undefined)).toContain('没有留下可续跑的检查点');
    expect(unavailableReasonZh('引擎已重启')).toBe('引擎已重启');
  });
});

describe('与引擎 Python 判定表保持一致', () => {
  const py = readFileSync(
    fileURLToPath(new URL('../../../engine/checkpoint_runtime.py', import.meta.url)),
    'utf8',
  ).replace(/\r\n/g, '\n');
  // #164：副作用名单的唯一落表处搬到 engine/side_effect_registry.py，
  // checkpoint_runtime / sandbox_runtime / tool_approval 三处都从它派生，
  // 所以本文件的同源校验改成「渲染层 ↔ 名单表」，再加一条「三个消费方确实只派生不定义」。
  const registry = readFileSync(
    fileURLToPath(new URL('../../../engine/side_effect_registry.py', import.meta.url)),
    'utf8',
  ).replace(/\r\n/g, '\n');

  function braces(src: string, name: string): string[] {
    const block = new RegExp(`${name}\\s*=\\s*\\{([\\s\\S]*?)\\}`).exec(src);
    if (!block) throw new Error(`引擎里找不到 ${name}`);
    return [...block[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
  }

  function tuple(src: string, name: string): string[] {
    const block = new RegExp(`${name}\\s*=\\s*\\(([\\s\\S]*?)\\)`).exec(src);
    if (!block) throw new Error(`引擎里找不到 ${name}`);
    return [...block[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
  }

  it('只读工具表逐字一致（仍留在 checkpoint_runtime，语义是「重放无害」）', () => {
    expect([...READ_ONLY_TOOLS].sort()).toEqual(braces(py, '_READ_ONLY_TOOLS').sort());
  });

  it('副作用工具表逐字一致（名单本体在 side_effect_registry 的 REPLAY）', () => {
    expect([...SIDE_EFFECT_TOOLS].sort()).toEqual(tuple(registry, 'REPLAY').sort());
  });

  it('副作用关键词表逐字一致（顺序不敏感）', () => {
    expect([...SIDE_EFFECT_KEYWORDS].sort()).toEqual(tuple(registry, 'KEYWORDS').sort());
  });

  it('检查点视图就是重放视图，且三处消费方只派生不再自带字面量', () => {
    expect(registry).toMatch(/^CHECKPOINT = REPLAY$/m);
    expect(py).toContain('_SIDE_EFFECT_TOOLS = set(ser.view("checkpoint"))');
    expect(py).toContain('_SIDE_EFFECT_KEYWORDS = ser.keywords()');
    const sandbox = readFileSync(
      fileURLToPath(new URL('../../../engine/sandbox_runtime.py', import.meta.url)),
      'utf8',
    ).replace(/\r\n/g, '\n');
    expect(sandbox).toContain('MUTATING_TOOLS = ser.view("sandbox_mutating")');
    const approval = readFileSync(
      fileURLToPath(new URL('../../../engine/tool_approval.py', import.meta.url)),
      'utf8',
    ).replace(/\r\n/g, '\n');
    expect(approval).toContain('SENSITIVE_TOOLS = ser.view("approval_sensitive")');
    // 派生之后不许再留「自带的成员表」，否则两处成员会各自漂移而 tests 只盯得住一处。
    // 只禁声明式的花括号集合字面量；逐工具摘要里的 `if name == "..."` 判定是另一回事，不在禁列。
    for (const [label, src] of [
      ['checkpoint_runtime', py],
      ['sandbox_runtime', sandbox],
      ['tool_approval', approval],
    ] as const) {
      const declarations = [...src.matchAll(/^\s*_?[A-Z_]+ = (?:set|frozenset)\(\s*\{([\s\S]*?)\}\s*\)/gm)];
      for (const decl of declarations) {
        if (/"(write_text_file|spawn_subagents|generate_pptx)"/.test(decl[1])) {
          throw new Error(`${label} 里 ${decl[0].trim()} 又自带了一份成员表，应改成 ser.view(...)`);
        }
      }
    }
  });

  it('派生视图名必须都在表里登记过（写错视图名要在导入时就炸，而不是静默空集）', () => {
    expect(braces(registry, 'VIEWS').sort()).toEqual([
      'approval_sensitive',
      'checkpoint',
      'replay',
      'sandbox_mutating',
    ]);
    expect(registry).toContain('raise KeyError');
  });

  it('引擎的 is_side_effect_tool 仍是「先只读、再副作用表、最后关键词」', () => {
    const fn = /def is_side_effect_tool[\s\S]*?\n\nclass /.exec(py)?.[0] ?? '';
    expect(fn).toContain('if not tool or tool in _READ_ONLY_TOOLS');
    expect(fn).toContain('if tool in _SIDE_EFFECT_TOOLS');
    expect(fn).toContain('any(keyword in lowered for keyword in _SIDE_EFFECT_KEYWORDS)');
  });

  it('Rust harness 的副作用表是本表的可执行子集（未登记工具在两侧都不算只读）', () => {
    const rs = readFileSync(
      fileURLToPath(new URL('../../../src-tauri/src/harness/tools.rs', import.meta.url)),
      'utf8',
    ).replace(/\r\n/g, '\n');
    expect(rs).toContain('"view_text_file" =>');
    expect(rs).toContain('"write_text_file" =>');
    expect(rs).toContain('side_effect');
  });
});
