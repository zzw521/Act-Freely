/**
 * 安全与治理 · 本地后端（无 Rust 通道共用实现）
 *
 * 桌面应用（联调/生产）走 Rust command，Rust 为权威实现；
 * 本模块仅服务于两条非 Rust 通道，保证 Mock 开关规范下安全页可自测：
 * - 开发 Mock 模式（VITE_MOCK 未关闭）
 * - 浏览器直连引擎模式（VITE_MOCK=false，引擎侧无治理存储）
 *
 * 分级判定复用 `shared/sensitivity-rules.ts`（与 Rust `security::classify` 同源）；
 * 出域控制/沙箱策略/审计日志/工作环境用量为进程内内存态，刷新即重置，不触碰任何真实数据。
 * 工作环境用量在桌面端由 Rust `sandbox_env_usage` 实测磁盘，浏览器直连引擎由引擎实测，
 * 这里只兜底无后端通道的 Mock 快照。
 */

import { classifyText } from '../../shared/sensitivity-rules';
import type {
  ComplianceCheckResp,
  CompliancePatch,
  ComplianceStatus,
  SandboxEnvClearResult,
  SandboxEnvUsage,
} from '../../shared/contracts';

interface LocalDomainControl {
  enabled: boolean;
  mode: 'whitelist' | 'blacklist';
  allowedDomains: string[];
  blockedDomains: string[];
  forceLocalForConfidential: boolean;
  hardDisableExternalAPI: boolean;
  updatedAtMs: number;
}

interface LocalAuditEntry {
  id: string;
  timestamp: string;
  action: string;
  severity: 'error' | 'warning' | 'info' | 'debug';
  user?: string;
  resource?: string;
  details?: string;
  read?: boolean;
}

/** 未配置过时的默认值（与 Rust `policy::default_domain_control` 语义一致） */
const DEFAULT_DOMAIN_CONTROL: LocalDomainControl = {
  enabled: false,
  mode: 'whitelist',
  allowedDomains: ['localhost', '127.0.0.1'],
  blockedDomains: [],
  forceLocalForConfidential: true,
  hardDisableExternalAPI: false,
  updatedAtMs: 0,
};

let domainControl: LocalDomainControl | null = null;
/** 沙箱策略按 workspaceId 隔离（与 Rust settings_kv key 布局一致） */
const sandboxPolicies: Record<string, Record<string, unknown>> = {};

/**
 * 工作环境用量：本模块（Mock / 浏览器直连兜底通道）没有磁盘访问能力。
 *
 * 这里曾返回一份写死的「1.2 GB / 1240 个文件」演示快照，界面把它当成实测数字显示 ——
 * 已改成如实报 supported=false + 全 0 并带原因：数字要么真，要么不显示。
 * 真实占用由 Rust `security::environment::usage_in` 或引擎
 * `/api/sandbox/environment/usage` 磁盘实测给出。
 */
function mockEnvUsage(): SandboxEnvUsage {
  return {
    supported: false,
    bytes: 0,
    files: 0,
    unreadable: 0,
    workspaceCount: 0,
    workspaces: [],
    rootPath: '',
    envDirName: 'environment',
    note: '浏览器预览环境读不到磁盘，占用统计与清理请在桌面版查看',
  };
}

/** 起始审计样本：无。审计页只列本次会话真实发生的动作（登录、模型测试、策略保存、清理等） */
const auditLogs: LocalAuditEntry[] = [];

/** 条目自增序号（React key 用，从 0 起算） */
let auditSeq = 0;

function record(
  action: string,
  severity: LocalAuditEntry['severity'],
  resource: string,
  details: string,
): void {
  auditSeq += 1;
  auditLogs.unshift({
    id: `local-audit-${auditSeq}`,
    timestamp: new Date().toISOString(),
    action,
    severity,
    user: '本机用户',
    resource,
    details,
    read: false,
  });
}

/** 裸命令名（与 Rust `policy::normalize_command` 同规则） */
const COMMAND_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/;

/** 沙箱策略边界校验（与 Rust `policy::set_sandbox` 同边界，越界抛真实错误码） */
function validateSandboxPolicy(p: Record<string, unknown>): void {
  const level = (p.executionLevel as string) ?? 'L1';
  if (!['L0', 'L1', 'L2'].includes(level)) {
    throw new Error('POLICY_INVALID_LEVEL: 沙箱执行级别非法（仅 L0/L1/L2）');
  }
  if (!String(p.workspaceId ?? '').trim()) {
    throw new Error('POLICY_INVALID_WORKSPACE: 工作区标识为空');
  }
  const cpu = Number(p.maxCpuPercent);
  const mem = Number(p.maxMemoryMB);
  const disk = Number(p.maxDiskWriteMbps);
  if (!(cpu >= 1 && cpu <= 100) || !(mem >= 64 && mem <= 32768) || !(disk >= 1 && disk <= 10000)) {
    throw new Error(
      'POLICY_INVALID_RANGE: 沙箱资源上限越界（CPU 1-100 / 内存 64-32768MB / 写盘 1-10000MB/s）',
    );
  }
  // 受控执行/联网授权：与 Rust `policy::set_sandbox` 同边界（缺省按空数组，同 serde default）
  const commands = p.execAllowlist ?? [];
  if (!Array.isArray(commands) || commands.length > 64) {
    throw new Error('POLICY_INVALID_COMMAND: 命令白名单必须是不超过 64 项的数组');
  }
  const badCommand = commands.find(
    (c) => typeof c !== 'string' || !COMMAND_NAME_RE.test(c.trim()),
  );
  if (badCommand !== undefined) {
    throw new Error(
      `POLICY_INVALID_COMMAND: 命令「${String(badCommand)}」非法（仅裸命令名，不能带路径或 shell 元字符）`,
    );
  }
  const hosts = p.networkAllowlist ?? [];
  if (!Array.isArray(hosts) || hosts.length > 64) {
    throw new Error('POLICY_INVALID_HOST: 联网白名单必须是不超过 64 项的数组');
  }
  const badHost = hosts.find((h) => normalizeDomains([h]).length === 0);
  if (badHost !== undefined) {
    throw new Error(`POLICY_INVALID_HOST: 主机「${String(badHost)}」非法`);
  }
}

function normalizeDomains(list: unknown): string[] {
  if (!Array.isArray(list)) return [];
  return list
    .map((d) => String(d).trim().toLowerCase())
    // 与 Rust `policy::normalize_domain` 同规则：用户常直接粘贴 https://host/v1，按主机名匹配
    .map((d) => d.split('://').pop() ?? d)
    .map((d) => d.split('/')[0])
    .map((d) => d.split(':')[0])
    .filter((d) => d.length > 0 && !d.includes(' '));
}

/** 本地回环主机（与 Rust `policy::LOCAL_HOSTS` 同源：本地通道始终放行） */
const LOCAL_HOSTS = ['localhost', '127.0.0.1', '::1', '0.0.0.0'];

function hostOf(url: string): string {
  return normalizeDomains([url])[0] ?? '';
}

/**
 * 出域判定（与 Rust `policy::eval_domain_allowed` 同口径）。
 * 放行返回 null，拒绝返回 `CODE: 原因`（错误码与桌面端一致，前端文案映射同源）。
 */
function evalDomainAllowed(cfg: LocalDomainControl, url: string): string | null {
  const host = hostOf(url);
  if (!host) return `POLICY_INVALID_URL: 无法解析主机名（${url}）`;
  if (cfg.hardDisableExternalAPI && !LOCAL_HOSTS.includes(host)) {
    return `POLICY_EXTERNAL_API_DISABLED: 外部 API 已被策略禁用（${host}）`;
  }
  if (!cfg.enabled || LOCAL_HOSTS.includes(host)) return null;
  if (cfg.mode === 'whitelist') {
    return cfg.allowedDomains.includes(host)
      ? null
      : `POLICY_DOMAIN_NOT_ALLOWED: ${host} 不在出域白名单内`;
  }
  return cfg.blockedDomains.includes(host)
    ? `POLICY_DOMAIN_BLOCKED: ${host} 已被出域黑名单拦截`
    : null;
}

/** 闸门留痕（允许与拒绝都记，与 Rust `compliance::record_gate` 同动作名） */
function recordGate(verdict: 'allowed' | 'blocked', sensitivity: string, host: string, detail: string): void {
  const summary = host
    ? `合规闸门 ${verdict}：等级=${sensitivity} 端点=${host}`
    : `合规闸门 ${verdict}：等级=${sensitivity}；端点未提供`;
  record('compliance_check', verdict === 'blocked' ? 'warning' : 'info', verdict, `${summary}；${detail}`);
}

/**
 * 提交前合规闸门（与 Rust `security::compliance::check` 同判定顺序）：
 * 先端点出域（硬开关/白名单/黑名单），再内容分级（机密本地化）。
 * 端点未知时只留痕分级，不臆断是否出域。
 */
function complianceCheck(text: string, baseUrl: unknown): ComplianceCheckResp {
  const cfg = currentDomainControl();
  const scan = classifyText(text);
  const url = typeof baseUrl === 'string' ? baseUrl.trim() : '';
  const host = url ? hostOf(url) : '';
  const local = host !== '' && LOCAL_HOSTS.includes(host);

  if (url) {
    const denied = evalDomainAllowed(cfg, url);
    if (denied) {
      recordGate('blocked', scan.sensitivity, host, denied);
      throw new Error(denied);
    }
  }

  if (scan.force_local && cfg.enabled && cfg.forceLocalForConfidential && host && !local) {
    const err = `COMPLIANCE_DATA_LEAK: 内容判定为${scan.sensitivity}，机密本地化已开启，禁止发往外部端点（${host}）`;
    recordGate('blocked', scan.sensitivity, host, err);
    throw new Error(err);
  }

  recordGate('allowed', scan.sensitivity, host, scan.summary);
  return {
    allowed: true,
    sensitivity: scan.sensitivity,
    forceLocal: scan.force_local,
    hardSwitchEnabled: cfg.hardDisableExternalAPI,
    endpointKnown: host !== '',
    endpointHost: host,
    endpointLocal: local,
  };
}

/** 本地通道处理结果：`handled=false` 表示 channel 不属于安全治理，交调用方兜底 */
export interface LocalSecurityResult {
  handled: boolean;
  value?: unknown;
}

/** 当前出域控制（未配置过时返回语义默认值副本，与 Rust `default_domain_control` 同源） */
function currentDomainControl(): LocalDomainControl {
  return domainControl ? { ...domainControl } : { ...DEFAULT_DOMAIN_CONTROL };
}

/**
 * 合规状态聚合（与 Rust `security::compliance::status` 同形状）。
 * 非桌面通道没有模型台账，`allowlistEvaluated` 恒为 false —— 空名单表示「未评估」，
 * 而不是「全部模型被拦」，前端据此不画假的红/绿清单。
 */
function complianceStatus(): ComplianceStatus {
  const cfg = currentDomainControl();
  const last = auditLogs.find((l) => l.action === 'data_check');
  return {
    hardSwitchEnabled: cfg.hardDisableExternalAPI,
    whitelistModelIds: [],
    blockedModelIds: [],
    allowlistEvaluated: false,
    domainControl: {
      enabled: cfg.enabled,
      mode: cfg.mode,
      allowedDomains: [...cfg.allowedDomains],
      blockedDomains: [...cfg.blockedDomains],
      forceLocalForConfidential: cfg.forceLocalForConfidential,
      hardDisableExternalAPI: cfg.hardDisableExternalAPI,
    },
    persisted: domainControl !== null,
    lastDataCheck: last
      ? {
          sensitivity: last.resource ?? '',
          severity: last.severity,
          timestamp: last.timestamp,
          summary: last.details,
        }
      : null,
  };
}

/** patch 是否包含任何字段（空 patch 只读不写，与 Rust 一致，不产生审计噪声） */
function patchTouches(raw: Partial<CompliancePatch>): boolean {
  return (
    raw.hardSwitchEnabled !== undefined ||
    raw.enabled !== undefined ||
    raw.mode !== undefined ||
    raw.allowedDomains !== undefined ||
    raw.blockedDomains !== undefined ||
    raw.forceLocalForConfidential !== undefined
  );
}

/**
 * 处理安全与治理 channel（`security:*` / `compliance:*` / `sandbox:*` / `audit:*`）。
 * 注意 `value` 可能合法为 `null`（如沙箱策略未配置），故用 `handled` 而非空值判断。
 */
export function handleLocalSecurityChannel(channel: string, args: unknown[]): LocalSecurityResult {
  if (channel === 'security:scanData') {
    const result = classifyText(String(args[0] ?? ''));
    // 留痕动作与 Rust `audit::ACTION_DATA_CHECK` 同名，合规状态据此回读最近一次检测
    record(
      'data_check',
      result.sensitivity === 'confidential' ? 'warning' : 'info',
      result.sensitivity,
      `数据分级检测：${result.sensitivity}（${(result.confidence * 100).toFixed(0)}%）`,
    );
    return { handled: true, value: result };
  }

  if (channel === 'security:getDomainControl') {
    return {
      handled: true,
      value: {
        config: domainControl ?? { ...DEFAULT_DOMAIN_CONTROL },
        persisted: domainControl !== null,
      },
    };
  }

  if (channel === 'security:setDomainControl') {
    const raw = (args[0] ?? {}) as Partial<LocalDomainControl>;
    const mode = raw.mode ?? 'whitelist';
    if (!['whitelist', 'blacklist'].includes(mode)) {
      throw new Error('POLICY_INVALID_MODE: 出域控制模式非法（仅 whitelist/blacklist）');
    }
    const stored: LocalDomainControl = {
      enabled: Boolean(raw.enabled),
      mode,
      allowedDomains: normalizeDomains(raw.allowedDomains),
      blockedDomains: normalizeDomains(raw.blockedDomains),
      forceLocalForConfidential: raw.forceLocalForConfidential ?? true,
      hardDisableExternalAPI: Boolean(raw.hardDisableExternalAPI),
      updatedAtMs: Date.now(),
    };
    domainControl = stored;
    record(
      'config_change',
      stored.hardDisableExternalAPI ? 'warning' : 'info',
      'domain_control',
      `出域控制更新：enabled=${stored.enabled} mode=${stored.mode} 白名单 ${stored.allowedDomains.length} 项 黑名单 ${stored.blockedDomains.length} 项 硬开关=${stored.hardDisableExternalAPI}`,
    );
    return { handled: true, value: { config: stored, persisted: true } };
  }

  // ── 合规通道（与出域控制同一份状态：合规概览开关与出域控制表单互相可见） ──
  if (channel === 'compliance:status') {
    return { handled: true, value: complianceStatus() };
  }

  if (channel === 'compliance:update') {
    const raw = (args[0] ?? {}) as Partial<CompliancePatch>;
    const base = currentDomainControl();
    const mode = raw.mode ?? base.mode;
    if (!['whitelist', 'blacklist'].includes(mode)) {
      throw new Error('POLICY_INVALID_MODE: 出域控制模式非法（仅 whitelist/blacklist）');
    }
    if (!patchTouches(raw)) {
      return { handled: true, value: { status: complianceStatus(), changed: false } };
    }
    const stored: LocalDomainControl = {
      enabled: raw.enabled ?? base.enabled,
      mode,
      allowedDomains:
        raw.allowedDomains !== undefined ? normalizeDomains(raw.allowedDomains) : base.allowedDomains,
      blockedDomains:
        raw.blockedDomains !== undefined ? normalizeDomains(raw.blockedDomains) : base.blockedDomains,
      forceLocalForConfidential: raw.forceLocalForConfidential ?? base.forceLocalForConfidential,
      hardDisableExternalAPI: raw.hardSwitchEnabled ?? base.hardDisableExternalAPI,
      updatedAtMs: Date.now(),
    };
    domainControl = stored;
    record(
      'config_change',
      stored.hardDisableExternalAPI ? 'warning' : 'info',
      'compliance',
      `合规策略更新：硬开关=${stored.hardDisableExternalAPI} 出域控制=${stored.enabled} mode=${stored.mode} 白名单 ${stored.allowedDomains.length} 项 黑名单 ${stored.blockedDomains.length} 项`,
    );
    return { handled: true, value: { status: complianceStatus(), changed: true } };
  }

  if (channel === 'compliance:check') {
    return { handled: true, value: complianceCheck(String(args[0] ?? ''), args[1]) };
  }

  if (channel === 'sandbox:getPolicy') {
    const ws = String(args[0] ?? '');
    return { handled: true, value: sandboxPolicies[ws] ?? null };
  }

  if (channel === 'sandbox:updatePolicy') {
    const policy = (args[0] ?? {}) as Record<string, unknown>;
    // 先按原始入参校验（空主机不能被「归一化后为空」静默吞掉），再归一化存储
    validateSandboxPolicy(policy);
    const execAllowlist: string[] = Array.isArray(policy.execAllowlist)
      ? (policy.execAllowlist as unknown[])
          .map((c) => String(c).trim().toLowerCase())
          .filter((c, i, arr) => c.length > 0 && arr.indexOf(c) === i)
      : [];
    const networkAllowlist = normalizeDomains(policy.networkAllowlist);
    const normalized: Record<string, unknown> = {
      ...policy,
      execEnabled: policy.execEnabled === true,
      execAllowlist,
      networkEnabled: policy.networkEnabled === true,
      networkAllowlist,
    };
    const ws = String(normalized.workspaceId);
    sandboxPolicies[ws] = normalized;
    record(
      'config_change',
      normalized.execEnabled || normalized.networkEnabled ? 'warning' : 'info',
      `sandbox:${ws}`,
      `沙箱策略更新：级别=${normalized.executionLevel} CPU≤${normalized.maxCpuPercent}% 内存≤${normalized.maxMemoryMB}MB 写盘≤${normalized.maxDiskWriteMbps}MB/s；`
        + `命令执行=${normalized.execEnabled ? `开启（${execAllowlist.length} 项）` : '关闭'}；`
        + `联网=${normalized.networkEnabled ? `开启（${networkAllowlist.length} 项）` : '关闭'}`,
    );
    return { handled: true, value: sandboxPolicies[ws] };
  }

  if (channel === 'sandbox:envUsage') {
    return { handled: true, value: mockEnvUsage() };
  }

  if (channel === 'sandbox:systemGet') {
    // 浏览器预览没有系统级隔离：如实返回「平台不支持」，不做一个能点亮但毫无作用的开关
    return {
      handled: true,
      value: {
        config: { enabled: false, network: false, updatedAtMs: 0 },
        status: {
          requested: false,
          supported: false,
          effective: false,
          platform: 'browser',
          profile: '',
          network: false,
          profileReady: false,
          error: '浏览器预览环境没有操作系统级沙箱，请在桌面版查看',
        },
      },
    };
  }

  if (channel === 'sandbox:systemSet') {
    throw new Error('SANDBOX_SYSTEM_UNSUPPORTED: 浏览器预览环境无法开启系统级隔离，请在桌面版操作');
  }

  if (channel === 'sandbox:envClear') {
    // 本地通道删不了真实文件，报不支持而不是返回一份「释放了 N 字节」的假回执
    throw new Error('SANDBOX_ENV_UNSUPPORTED: 浏览器预览环境无法清理工作环境文件，请在桌面版操作');
  }

  if (channel === 'audit:query') {
    const filters = (args[0] ?? {}) as {
      type?: string;
      action?: string;
      severity?: string;
      limit?: number;
    };
    const action = filters.action ?? filters.type;
    let list = [...auditLogs];
    if (action) list = list.filter((l) => l.action === action);
    if (filters.severity) list = list.filter((l) => l.severity === filters.severity);
    return { handled: true, value: list.slice(0, filters.limit ?? 500) };
  }

  if (channel === 'audit:export') {
    const format = String(args[0] ?? 'json');
    if (!['csv', 'json'].includes(format)) {
      throw new Error('AUDIT_EXPORT_FORMAT_UNSUPPORTED: 导出格式仅支持 csv/json');
    }
    const count = auditLogs.length;
    record('file_write', 'info', '/exports/audit', `审计日志导出：${format} 共 ${count} 条`);
    return { handled: true, value: { path: `/exports/audit-${Date.now()}.${format}`, count, format } };
  }

  return { handled: false };
}