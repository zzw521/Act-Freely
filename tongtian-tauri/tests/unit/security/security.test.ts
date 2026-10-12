/**
 * 安全与治理单元测试（useSecurityStore + Mock/本地通道 + 分级规则）
 *
 * vitest 环境为 node（无 window），此处手动 polyfill 后动态导入
 * mock-api 垫片与 store，覆盖真实分发路径：
 * - 分级检测（前端同源规则表，与 Rust security::classify 判定顺序一致）
 * - 出域控制持久化语义（首次读 default + persisted:false；保存后归一化回写）
 * - 沙箱策略边界校验（越界抛真实错误码）与「未配置返回 null」契约
 * - 审计日志查询过滤/导出
 */

import { describe, it, expect, beforeAll } from 'vitest';

(globalThis as Record<string, unknown>).window = (globalThis as Record<string, unknown>).window ?? {};

type WindowAPI = { tongtianAPI?: { invoke: (channel: string, ...args: unknown[]) => Promise<unknown> } };
const win = globalThis.window as WindowAPI;

let useSecurityStore: typeof import('../../../src/renderer/store/securityStore').useSecurityStore;
let classifyText: typeof import('../../../src/shared/sensitivity-rules').classifyText;

beforeAll(async () => {
  await import('../../../src/renderer/lib/mock-api');
  useSecurityStore = (await import('../../../src/renderer/store/securityStore')).useSecurityStore;
  classifyText = (await import('../../../src/shared/sensitivity-rules')).classifyText;
});

// ── 分级规则：本地检测（Mock 通道） ────────────────────────────

describe('classifyText：数据分级判定', () => {
  it('18 位身份证号判为 confidential 且标记 PII/强制本地', () => {
    const r = classifyText('客户身份证 11010119900307123X');
    expect(r.sensitivity).toBe('confidential');
    expect(r.pii).toBe(true);
    expect(r.force_local).toBe(true);
    expect(r.confidence).toBeCloseTo(0.95);
  });

  it('手机号与邮箱判为 confidential/PII', () => {
    expect(classifyText('联系方式 13800138000').pii).toBe(true);
    expect(classifyText('邮箱 zhangsan@example.com').pii).toBe(true);
  });

  it('商业关键词（合同）判为 confidential 但不置 PII', () => {
    const r = classifyText('这是采购合同正文');
    expect(r.sensitivity).toBe('confidential');
    expect(r.pii).toBe(false);
    expect(r.force_local).toBe(true);
  });

  it('内部标记判为 internal，置信度低于高危档', () => {
    const r = classifyText('内部资料，仅限内部传阅');
    expect(r.sensitivity).toBe('internal');
    expect(r.confidence).toBeCloseTo(0.7);
  });

  it('普通文本判为 public', () => {
    const r = classifyText('今天天气不错');
    expect(r.sensitivity).toBe('public');
    expect(r.findings).toHaveLength(0);
  });

  // 黄金样例：与 Rust `security::classify::tests::test_golden_mixed_sample` 逐字对齐，
  // 守护「每串数字只出一类结论」（身份证尾字母不得再被判为银行卡）。
  it('混合样例命中集合与 Rust 权威实现一致', () => {
    const r = classifyText('客户身份证 11010119900307123X，联系方式 13800138000，附采购合同一份');
    expect(r.findings.map((f) => f.pattern)).toEqual([
      '18 位身份证号',
      '11 位手机号',
      '身份证',
      '合同',
    ]);
    expect(r.sensitivity).toBe('confidential');
    expect(r.confidence).toBeCloseTo(0.95);
    expect(r.force_local).toBe(true);
  });

  it('分组银行卡号（空格分隔）识别为银行卡', () => {
    const r = classifyText('卡号 6222 0212 3456 7890 已核对');
    expect(r.findings.map((f) => f.pattern)).toContain('16-19 位银行卡号');
    expect(r.pii).toBe(true);
  });

  it('重复调用同一文本结果稳定（无 RegExp lastIndex 漂移）', () => {
    const text = '身份证 11010119900307123X';
    expect(classifyText(text).sensitivity).toBe(classifyText(text).sensitivity);
  });
});

// ── 出域控制：持久化语义 ──────────────────────────────────────

describe('useSecurityStore：出域控制', () => {
  it('首次读取返回默认配置且标记未落库', async () => {
    const { config, persisted } = (await win.tongtianAPI!.invoke(
      'security:getDomainControl',
    )) as { config: { mode: string; enabled: boolean }; persisted: boolean };
    expect(persisted).toBe(false);
    expect(config.mode).toBe('whitelist');
    expect(config.enabled).toBe(false);
  });

  it('保存后归一化域名（小写去空白）并标记已落库', async () => {
    const store = useSecurityStore.getState();
    const stored = await store.updateDomainControl({
      enabled: true,
      mode: 'whitelist',
      allowedDomains: ['  API.Example.COM ', ''],
      blockedDomains: [],
      forceLocalForConfidential: true,
      hardDisableExternalAPI: false,
    });
    expect(stored.allowedDomains).toEqual(['api.example.com']);
    expect(useSecurityStore.getState().domainControlPersisted).toBe(true);

    const reloaded = await useSecurityStore.getState().loadDomainControl();
    void reloaded;
    expect(useSecurityStore.getState().domainControl?.allowedDomains).toEqual(['api.example.com']);
  });

  it('非法模式被拒绝（真实错误码）', async () => {
    await expect(
      win.tongtianAPI!.invoke('security:setDomainControl', { mode: 'bogus' }),
    ).rejects.toThrow(/POLICY_INVALID_MODE/);
  });
});

// ── 沙箱策略：边界校验与空值契约 ───────────────────────────────

describe('useSecurityStore：沙箱策略', () => {
  it('未配置的工作区返回 null（不伪造已保存数据）', async () => {
    const policy = await useSecurityStore.getState().loadSandboxPolicy('ws-untouched');
    expect(policy).toBeNull();
    expect(useSecurityStore.getState().sandboxPolicy).toBeNull();
  });

  it('保存后可按工作区读回，且工作区间隔离', async () => {
    const policy = {
      workspaceId: 'ws-a',
      executionLevel: 'L2' as const,
      fileAllowlist: ['D:/work/**'],
      fileBlocklist: [],
      toolAllowlist: [],
      toolBlocklist: [],
      maxCpuPercent: 80,
      maxMemoryMB: 2048,
      maxDiskWriteMbps: 20,
    };
    await useSecurityStore.getState().saveSandboxPolicy(policy);
    const back = await useSecurityStore.getState().loadSandboxPolicy('ws-a');
    expect(back?.executionLevel).toBe('L2');
    expect(back?.maxMemoryMB).toBe(2048);
    expect(await useSecurityStore.getState().loadSandboxPolicy('ws-b')).toBeNull();
  });

  it('CPU 越界抛 POLICY_INVALID_RANGE', async () => {
    await expect(
      win.tongtianAPI!.invoke('sandbox:updatePolicy', {
        workspaceId: 'ws-x',
        executionLevel: 'L1',
        maxCpuPercent: 0,
        maxMemoryMB: 512,
        maxDiskWriteMbps: 10,
      }),
    ).rejects.toThrow(/POLICY_INVALID_RANGE/);
  });

  it('非法执行级别抛 POLICY_INVALID_LEVEL', async () => {
    await expect(
      win.tongtianAPI!.invoke('sandbox:updatePolicy', {
        workspaceId: 'ws-x',
        executionLevel: 'L9',
        maxCpuPercent: 50,
        maxMemoryMB: 512,
        maxDiskWriteMbps: 10,
      }),
    ).rejects.toThrow(/POLICY_INVALID_LEVEL/);
  });

  it('空工作区标识抛 POLICY_INVALID_WORKSPACE', async () => {
    await expect(
      win.tongtianAPI!.invoke('sandbox:updatePolicy', {
        workspaceId: '  ',
        executionLevel: 'L1',
        maxCpuPercent: 50,
        maxMemoryMB: 512,
        maxDiskWriteMbps: 10,
      }),
    ).rejects.toThrow(/POLICY_INVALID_WORKSPACE/);
  });
});

// ── 审计日志：查询与导出 ──────────────────────────────────────

describe('useSecurityStore：审计日志', () => {
  it('查询返回种子日志且按动作过滤有效', async () => {
    await useSecurityStore.getState().loadAuditLogs();
    expect(useSecurityStore.getState().auditLogs.length).toBeGreaterThan(0);
    expect(useSecurityStore.getState().logError).toBeNull();

    const filtered = (await win.tongtianAPI!.invoke('audit:query', { type: 'sandbox_violation' })) as Array<{
      action: string;
    }>;
    expect(filtered.length).toBeGreaterThan(0);
    expect(filtered.every((l) => l.action === 'sandbox_violation')).toBe(true);
  });

  it('分级检测会留审计痕迹（配置变更动作）', async () => {
    const before = ((await win.tongtianAPI!.invoke('audit:query')) as unknown[]).length;
    await useSecurityStore.getState().scanData('内部文件：采购合同');
    const after = ((await win.tongtianAPI!.invoke('audit:query')) as unknown[]).length;
    expect(after).toBe(before + 1);
  });

  it('导出返回落盘路径与条数，并追加一条 file_write 审计', async () => {
    const result = await useSecurityStore.getState().exportAuditLogs('csv');
    expect(result.format).toBe('csv');
    expect(result.path.endsWith('.csv')).toBe(true);
    expect(result.count).toBeGreaterThan(0);
    const writes = (await win.tongtianAPI!.invoke('audit:query', { type: 'file_write' })) as unknown[];
    expect(writes.length).toBeGreaterThan(0);
  });

  it('新增审计条目 id 与种子条目不冲突（React key 唯一）', async () => {
    const logs = (await win.tongtianAPI!.invoke('audit:query')) as Array<{ id: string }>;
    const ids = logs.map((l) => l.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it('不支持的导出格式抛 AUDIT_EXPORT_FORMAT_UNSUPPORTED', async () => {
    await expect(win.tongtianAPI!.invoke('audit:export', 'xml')).rejects.toThrow(
      /AUDIT_EXPORT_FORMAT_UNSUPPORTED/,
    );
  });
});