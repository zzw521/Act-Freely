/**
 * 通天办公 - 安全与治理状态（PRD §4.7）
 *
 * 三通道统一走 `window.tongtianAPI.invoke`：
 * - Tauri 容器 → Rust command（authority：分级/策略/审计均落 SQLite）
 * - 浏览器直连引擎 → 前端本地实现（`lib/security-local.ts`）
 * - 开发 Mock → 同上（Mock 开关规范：无后端也可自测）
 *
 * 出域控制/沙箱策略的边界校验在 Rust 侧完成，前端只做形状适配与展示。
 */

import { create } from 'zustand';
import { IPC_CHANNELS } from '../../shared/contracts';
import type {
  ComplianceDomainControl,
  CompliancePatch,
  ComplianceStatus,
  ComplianceUpdateResp,
} from '../../shared/contracts';
import type { LocalScanResult } from '../../shared/sensitivity-rules';
import type { AuditLogEntry, SandboxPolicy } from '../../shared/types';

/** 出域控制配置（直接复用契约类型，避免与 Rust `policy::DomainControl` 双份字段漂移） */
export type DomainControlConfig = ComplianceDomainControl;

export interface AuditFilters {
  /** 动作过滤（对齐 Rust AuditFilters.action，兼容旧 `type` 命名） */
  type?: string;
  severity?: string;
  dateRange?: string;
  limit?: number;
}

export interface AuditExportResult {
  path: string;
  count: number;
  format: string;
}

interface DomainControlView {
  config: DomainControlConfig;
  persisted: boolean;
}

interface SecurityState {
  // 数据分级
  lastScanResult: LocalScanResult | null;

  // 出域控制
  domainControl: DomainControlConfig | null;
  /** 是否已落库过（false 表示当前展示的是语义默认值） */
  domainControlPersisted: boolean;
  domainControlLoading: boolean;

  // 审计日志
  auditLogs: AuditLogEntry[];
  logLoading: boolean;
  logError: string | null;

  // 沙箱策略
  sandboxPolicy: SandboxPolicy | null;
  policyLoading: boolean;

  // 合规聚合视图（与出域控制同一份权威，仅只读展示 + 硬开关写入）
  compliance: ComplianceStatus | null;
  complianceLoading: boolean;
  complianceError: string | null;

  // Actions
  scanData: (text: string) => Promise<LocalScanResult>;
  loadDomainControl: () => Promise<void>;
  updateDomainControl: (config: DomainControlConfig) => Promise<DomainControlConfig>;
  loadCompliance: () => Promise<void>;
  updateCompliance: (patch: CompliancePatch) => Promise<ComplianceUpdateResp>;
  loadAuditLogs: (filters?: AuditFilters) => Promise<void>;
  exportAuditLogs: (format: 'csv' | 'json') => Promise<AuditExportResult>;
  saveSandboxPolicy: (policy: SandboxPolicy) => Promise<SandboxPolicy>;
  loadSandboxPolicy: (workspaceId: string) => Promise<SandboxPolicy | null>;
  reset: () => void;
}

function getAPI() {
  const api = window.tongtianAPI;
  if (!api) {
    throw new Error('SECURITY_API_UNAVAILABLE: tongtianAPI 未注入，无法调用安全治理接口');
  }
  return api;
}

/** 审计过滤器 → Rust `AuditFilters`（`type` 归一为 `action`） */
function toRustFilters(filters?: AuditFilters) {
  if (!filters) return null;
  return {
    action: filters.type,
    severity: filters.severity,
    dateRange: filters.dateRange,
    limit: filters.limit,
  };
}

export const useSecurityStore = create<SecurityState>((set) => ({
  lastScanResult: null,

  domainControl: null,
  domainControlPersisted: false,
  domainControlLoading: false,

  auditLogs: [],
  logLoading: false,
  logError: null,

  sandboxPolicy: null,
  policyLoading: false,

  compliance: null,
  complianceLoading: false,
  complianceError: null,

  /** 数据分级检测：Rust `security_scan_data`（权威）或本地同源规则表 */
  scanData: async (text: string) => {
    const result = (await getAPI().invoke(IPC_CHANNELS.SECURITY_SCAN_DATA, text)) as LocalScanResult;
    set({ lastScanResult: result });
    return result;
  },

  loadDomainControl: async () => {
    set({ domainControlLoading: true });
    try {
      const view = (await getAPI().invoke(
        IPC_CHANNELS.SECURITY_GET_DOMAIN_CONTROL,
      )) as DomainControlView;
      set({
        domainControl: view?.config ?? null,
        domainControlPersisted: Boolean(view?.persisted),
        domainControlLoading: false,
      });
    } catch (err) {
      set({ domainControlLoading: false });
      throw err;
    }
  },

  /** 保存出域控制：以后端归一化结果为准（域名小写去重等） */
  updateDomainControl: async (config) => {
    const view = (await getAPI().invoke(
      IPC_CHANNELS.SECURITY_SET_DOMAIN_CONTROL,
      config,
    )) as DomainControlView;
    const stored = view?.config ?? config;
    set({ domainControl: stored, domainControlPersisted: true });
    // 出域控制与合规视图共用同一份权威，保存后同步聚合视图，避免两处展示打架
    await useSecurityStore.getState().loadCompliance();
    return stored;
  },

  /**
   * 读取合规聚合视图。
   * 失败时记录 complianceError 而不抛出：安全页已有各自的错误提示通道，
   * 这里必须让调用方看到失败，而不是静默回落到「一切正常」。
   */
  loadCompliance: async () => {
    set({ complianceLoading: true });
    try {
      const status = (await getAPI().invoke(IPC_CHANNELS.COMPLIANCE_STATUS)) as ComplianceStatus;
      set({
        compliance: status ?? null,
        complianceLoading: false,
        complianceError: null,
        // 出域控制摘要与合规视图同源：一并回写，避免两处展示打架
        ...(status
          ? {
              domainControl: status.domainControl,
              domainControlPersisted: Boolean(status.persisted),
            }
          : null),
      });
    } catch (err) {
      set({ complianceLoading: false, complianceError: `加载合规状态失败: ${err}` });
    }
  },

  /** 更新合规策略（硬开关等）：以后端回读的聚合状态为准，并把变更写回出域控制 */
  updateCompliance: async (patch) => {
    const resp = (await getAPI().invoke(
      IPC_CHANNELS.COMPLIANCE_UPDATE,
      patch,
    )) as ComplianceUpdateResp;
    const status = resp?.status ?? null;
    set({
      compliance: status,
      complianceError: null,
      domainControl: status?.domainControl ?? useSecurityStore.getState().domainControl,
      domainControlPersisted: Boolean(status?.persisted),
    });
    return resp;
  },

  loadAuditLogs: async (filters) => {
    set({ logLoading: true, logError: null });
    try {
      const logs = (await getAPI().invoke(
        IPC_CHANNELS.AUDIT_QUERY,
        toRustFilters(filters),
      )) as AuditLogEntry[];
      set({ auditLogs: logs ?? [], logLoading: false });
    } catch (err) {
      set({ logError: `加载日志失败: ${err}`, logLoading: false });
      throw err;
    }
  },

  exportAuditLogs: async (format) => {
    const result = (await getAPI().invoke(
      IPC_CHANNELS.AUDIT_EXPORT,
      format,
      toRustFilters(),
    )) as AuditExportResult;
    // 导出会写一条 file_write 审计，刷新列表保持一致
    await useSecurityStore.getState().loadAuditLogs();
    return result;
  },

  saveSandboxPolicy: async (policy) => {
    set({ policyLoading: true });
    try {
      const stored = (await getAPI().invoke(
        IPC_CHANNELS.SANDBOX_UPDATE_POLICY,
        policy,
      )) as SandboxPolicy;
      set({ sandboxPolicy: stored ?? policy, policyLoading: false });
      return stored ?? policy;
    } catch (err) {
      set({ policyLoading: false });
      throw err;
    }
  },

  /** 读取沙箱策略：未配置返回 null（由表单展示默认值，不伪造已保存数据） */
  loadSandboxPolicy: async (workspaceId) => {
    set({ policyLoading: true });
    try {
      const policy = (await getAPI().invoke(
        IPC_CHANNELS.SANDBOX_GET_POLICY,
        workspaceId,
      )) as SandboxPolicy | null;
      set({ sandboxPolicy: policy ?? null, policyLoading: false });
      return policy ?? null;
    } catch (err) {
      set({ policyLoading: false });
      throw err;
    }
  },

  reset: () => {
    set({
      lastScanResult: null,
      domainControl: null,
      domainControlPersisted: false,
      domainControlLoading: false,
      auditLogs: [],
      logLoading: false,
      logError: null,
      sandboxPolicy: null,
      policyLoading: false,
      compliance: null,
      complianceLoading: false,
      complianceError: null,
    });
  },
}));