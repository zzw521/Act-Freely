/**
 * PII 出站脱敏 + 逐工具审批契约测试（任务 #75）
 *
 * 这两项最容易变成「设置里能选、运行时没接」的假承诺。本文件从源码两侧锁定：
 * - 设置契约：两个档位进 AppSettingsData 与默认值，设置页写的是真键
 * - 桌面链路：Rust runtime 装配 privacy/toolApproval，本机网关启动并逐次读档位
 * - 浏览器直连：piiOutboundMode 随提交与重新生成下发，引擎白名单校验
 * - 引擎执行面：模型包装真正处理 Msg、block 不回落兜底计划、审批装在
 *   沙箱/Hooks 之后、网关不可用时中止而不是绕过
 * - 诚实边界：设置页写明图片/IM/浏览器外发不覆盖，审计标签可读
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ErrorCode } from '../../../src/shared/contracts';
import { errorMessageOf } from '../../../src/shared/error-messages';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const contracts = () => read('../../../src/shared/contracts.ts');
const sharedTypes = () => read('../../../src/shared/types.ts');
const alignmentApi = () => read('../../../src/renderer/lib/alignment-api.ts');
const settingsPage = () => read('../../../src/renderer/pages/Settings/index.tsx');
const engineApi = () => read('../../../src/renderer/lib/engine-api.ts');
const workbenchStore = () => read('../../../src/renderer/store/workbenchStore.ts');
const auditLog = () => read('../../../src/renderer/components/AuditLog/index.tsx');
const rustRuntime = () => read('../../../src-tauri/src/bridge/runtime.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const rustGate = () => read('../../../src-tauri/src/bridge/tool_gate.rs');
const rustPii = () => read('../../../src-tauri/src/security/pii.rs');
const rustApproval = () => read('../../../src-tauri/src/security/tool_approval.rs');
const server = () => read('../../../engine/server.py');
const privacyGuard = () => read('../../../engine/privacy_guard.py');
const toolApproval = () => read('../../../engine/tool_approval.py');
const subagent = () => read('../../../engine/subagent_runtime.py');

// ── 设置契约 ────────────────────────────────────────────────────

describe('设置项契约', () => {
  it('两个档位有单一类型来源，并进入 AppSettingsData', () => {
    expect(sharedTypes()).toContain("export type PiiOutboundMode = 'off' | 'mask' | 'block';");
    expect(sharedTypes()).toContain('piiOutboundMode?: PiiOutboundMode;');
    expect(contracts()).toContain("export type { PiiOutboundMode } from './types';");
    expect(contracts()).toContain("export type ToolApprovalMode = 'off' | 'sensitive' | 'all';");
    const data = contracts().split('export interface AppSettingsData')[1].split('\n}')[0];
    expect(data).toContain('piiOutboundMode: PiiOutboundMode;');
    expect(data).toContain('toolApprovalMode: ToolApprovalMode;');
  });

  it('默认关闭，不回退到会改变既有行为的档位', () => {
    const defaults = alignmentApi().split('const DEFAULT_SETTINGS')[1].split('};')[0];
    expect(defaults).toContain("piiOutboundMode: 'off',");
    expect(defaults).toContain("toolApprovalMode: 'off',");
  });

  it('设置页写的是真键，而不是只改本地展示', () => {
    const page = settingsPage();
    expect(page).toContain('updateAppSettings({ piiOutboundMode: PII_VALUES[v]');
    expect(page).toContain('updateAppSettings({ toolApprovalMode: TOOL_APPROVAL_VALUES[v]');
    expect(page).toContain("关闭: 'off',");
    expect(page).toContain("脱敏后发送: 'mask',");
    expect(page).toContain("命中即中止: 'block',");
    expect(page).toContain("敏感工具: 'sensitive',");
    expect(page).toContain("所有工具: 'all',");
  });

  it('边界文案如实写明不覆盖图片、IM、浏览器与连接器外发', () => {
    const page = settingsPage();
    expect(page).toContain('脱敏只覆盖本应用发起任务时发往模型的文本');
    expect(page).toContain('图片附件、浏览器/连接器/脚本自身的外发');
    expect(page).toContain('IM 频道任务不经过这里');
    expect(page).toContain('工具审批仅桌面端生效');
    expect(page).toContain('网关不可用会中止任务而不是跳过审批');
  });

  it('审计页有可读标签', () => {
    expect(auditLog()).toContain("tool_approval: '🛂 工具审批'");
  });

  it('错误码与用户文案齐全，不把裸码丢给用户', () => {
    for (const code of [
      'PII_MODE_INVALID',
      'PRIVACY_CONFIG_UNAVAILABLE',
      'PRIVACY_OUTBOUND_BLOCKED',
      'TOOL_APPROVAL_INVALID_POLICY',
      'TOOL_APPROVAL_UNAVAILABLE',
      'TOOL_APPROVAL_GUARD_FAILED',
      'TOOL_APPROVAL_DENIED_BY_USER',
      'TOOL_APPROVAL_CONFIRM_UNAVAILABLE',
    ]) {
      expect(ErrorCode[code as keyof typeof ErrorCode]).toBe(code);
      expect(errorMessageOf(code), `${code} 缺少文案`).not.toBe(code);
    }
  });
});

// ── 浏览器直连提交 ──────────────────────────────────────────────

describe('浏览器直连提交链路', () => {
  it('/api/chat 请求体带 piiOutboundMode', () => {
    expect(engineApi()).toContain('piiOutboundMode?: PiiOutboundMode;');
    const body = engineApi().split("postSSE('/api/chat', {")[1].split('});')[0];
    expect(body).toContain('piiOutboundMode: req.piiOutboundMode,');
  });

  it('首次提交、重新生成与改写提问都从设置读取，不在 store 里写死 off', () => {
    const src = workbenchStore();
    // 三条提交路径：新任务、重新生成、改写提问后重跑
    expect(src.split('piiOutboundMode: settings?.piiOutboundMode,').length).toBe(4);
    const regen = src.split('regenerateTurn: async (')[1].split('resumeTask: async (')[0];
    expect(regen).toContain('const settings = await getSettings()');
    expect(regen).toContain('piiOutboundMode: settings?.piiOutboundMode,');
  });

  it('引擎拒绝未知档位而不是静默按关闭处理', () => {
    const src = server();
    expect(src).toContain('pii_raw = payload.get("piiOutboundMode")');
    expect(src).toContain('pii_value not in pg.MODES');
    expect(src).toContain('"code": "PII_MODE_INVALID"');
    expect(src).toContain('session.pii_mode = pii_value');
  });
});

// ── 桌面运行时与网关 ────────────────────────────────────────────

describe('桌面运行时装配', () => {
  it('runtime bundle 同时下发 privacy 与 toolApproval', () => {
    const src = rustRuntime();
    expect(src).toContain('"privacy": privacy_block(store),');
    expect(src).toContain('"toolApproval": tool_approval_block(store),');
    expect(src).toContain('fn privacy_block(store: Option<&Store>) -> Value');
    expect(src).toContain('fn tool_approval_block(store: Option<&Store>) -> Value');
    // 档位非法要带 error 返回，不能悄悄当默认值
    expect(rustRuntime()).toContain('"error"');
  });

  it('本机审批网关在 setup 启动，token 只随 runtime 下发', () => {
    expect(rustLib()).toContain('bridge::tool_gate::start(Arc::clone(&services));');
    expect(rustGate()).toContain('x-tt-tool-token');
    expect(rustGate()).toContain('tool_approval::read_mode(&services.store)');
  });

  it('每次请求重读档位，关闭后立即拒绝', () => {
    const gate = rustGate();
    expect(gate).toContain('if mode == "off"');
    expect(gate).toContain('TOOL_APPROVAL_DISABLED');
    expect(rustApproval()).toContain('pub const POLICY_KEY: &str = "toolApprovalMode";');
    expect(rustPii()).toContain('pub const POLICY_KEY: &str = "piiOutboundMode";');
  });

  it('确认框与审计都先脱敏 PII/密钥，审计再遮住用户名路径', () => {
    const gate = rustGate();
    expect(gate).toContain('fn describe_for_user(tool: &str, summary: &str) -> (String, String)');
    expect(gate).toContain('classify::redact(summary)');
    expect(gate).toContain('classify::redact_paths(&description)');
    expect(gate).toContain("audit::ACTION_TOOL_APPROVAL");
  });
});

// ── 引擎执行面 ──────────────────────────────────────────────────

describe('引擎执行面接线', () => {
  it('模型包装在最外层，覆盖计划与执行阶段的每次调用', () => {
    const src = server();
    expect(src).toContain('return pg.wrap_model(model, pii_mode, on_redaction=on_redaction), formatter');
    expect(src).toContain('pii_mode = pg.from_runtime(session.runtime, session.pii_mode)');
    expect(src).toContain('except pg.PrivacyBlocked');
  });

  it('block 档命中中止任务，不回落到兜底计划', () => {
    const src = server();
    const plan = src.split('async def _generate_plan')[1].split('async def ')[0];
    const privacy = plan.indexOf('except pg.PrivacyBlocked');
    const generic = plan.indexOf('except Exception');
    expect(privacy).toBeGreaterThan(0);
    expect(generic).toBeGreaterThan(privacy);
    expect(src).toContain('"code": "PRIVACY_OUTBOUND_BLOCKED"');
    expect(src).toContain('end_reason = "privacy_blocked"');
  });

  it('审批中间件装在 Hooks 之后，拒绝后不再执行真实动作', () => {
    const src = server();
    const run = src.split('async def _run_execution')[1].split('\n@app.')[0];
    expect(run.indexOf('hr.install(agent.toolkit, hook_runner)')).toBeGreaterThan(-1);
    expect(run.indexOf('tool_approval.install(')).toBeGreaterThan(
      run.indexOf('hr.install(agent.toolkit, hook_runner)'),
    );
    expect(toolApproval()).toContain('async for chunk in await next_handler(**kwargs)');
    expect(toolApproval()).toContain('工具「{name}」未执行');
  });

  it('网关不可用或档位读不出来时中止任务，不静默放行', () => {
    const src = server();
    expect(src).toContain('if tool_approval.runtime_present and tool_approval.error:');
    expect(src).toContain('"code": "TOOL_APPROVAL_UNAVAILABLE"');
    expect(src).toContain('"code": "TOOL_APPROVAL_GUARD_FAILED"');
    // 档位保持原值，避免 enabled 被降成 false 后绕过审批
    expect(toolApproval()).toContain('mode=mode,');
    expect(toolApproval()).not.toContain('mode=mode if not error or mode == "off" else "off"');
  });

  it('子 Agent 也装审批，隐私中止不会被 gather 吞掉', () => {
    const src = subagent();
    expect(src).toContain('if self.approval is not None:');
    expect(src).toContain('self.approval.install(toolkit, self.emit, task_id=self.task_id)');
    expect(src).toContain('if isinstance(item, PrivacyBlocked):');
    expect(src).toContain('raise item');
    expect(src).toContain('if isinstance(exc, PrivacyBlocked):');
  });

  it('脱敏真正遍历 Msg 对象，并在弹框前脱敏工具摘要', () => {
    expect(privacyGuard()).toContain('if hasattr(value, "content") and hasattr(value, "role"):');
    expect(privacyGuard()).toContain('clone = copy.copy(value)');
    expect(toolApproval()).toContain('summary = pg.redact(summarize_input(');
  });
});
