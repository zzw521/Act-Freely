import { create } from 'zustand';
import { SandboxPolicy, AuditLogEntry, DataSensitivity } from '../../shared/types';

interface SecurityState {
  // 数据分级
  lastScanResult: any | null;
  sensitivityRules: Array<{ pattern: RegExp; level: DataSensitivity; description: string }>;

  // 出域控制
  domainControlEnabled: boolean;
  domainMode: 'whitelist' | 'blacklist';
  allowedDomains: string[];
  blockedDomains: string[];
  forceLocalForConfidential: boolean;
  hardDisableExternalAPI: boolean;

  // 审计日志
  auditLogs: AuditLogEntry[];
  logLoading: boolean;
  logError: string | null;

  // 沙箱策略
  sandboxPolicy: SandboxPolicy | null;
  policyLoading: boolean;

  // Actions
  scanData: (text: string) => Promise<any>;
  updateDomainControl: (config: Partial<SecurityState>) => void;
  loadAuditLogs: (filters?: { type?: string; severity?: string; dateRange?: string }) => Promise<void>;
  exportAuditLogs: (format: 'csv' | 'json') => Promise<void>;
  saveSandboxPolicy: (policy: SandboxPolicy) => Promise<void>;
  loadSandboxPolicy: (workspaceId: string) => Promise<void>;
  reset: () => void;
}

export const useSecurityStore = create<SecurityState>((set, get) => ({
  // 初始状态
  lastScanResult: null,
  sensitivityRules: [
    { pattern: /身份证|护照|营业执照/, level: DataSensitivity.CONFIDENTIAL, description: '个人证件信息' },
    { pattern: /合同|协议|薪资|报价/, level: DataSensitivity.CONFIDENTIAL, description: '商业机密文件' },
    { pattern: /内部|机密|绝密/, level: DataSensitivity.INTERNAL, description: '内部敏感标记' },
  ],

  domainControlEnabled: false,
  domainMode: 'whitelist',
  allowedDomains: ['localhost', '127.0.0.1'],
  blockedDomains: [],
  forceLocalForConfidential: true,
  hardDisableExternalAPI: false,

  auditLogs: [],
  logLoading: false,
  logError: null,

  sandboxPolicy: null,
  policyLoading: false,

  // Actions
  scanData: async (text: string) => {
    try {
      // TODO: 调用 IPC 接口进行数据分级检测
      // const result = await window.electron.security.scanData({ text });
      
      // 临时实现：基于规则匹配
      const rules = get().sensitivityRules;
      let sensitivity: DataSensitivity = DataSensitivity.PUBLIC;
      let confidence = 0.5;
      const findings: any[] = [];

      for (const rule of rules) {
        if (rule.pattern.test(text)) {
          if (rule.level === DataSensitivity.CONFIDENTIAL && sensitivity !== DataSensitivity.CONFIDENTIAL) {
            sensitivity = DataSensitivity.CONFIDENTIAL;
            confidence = 0.9;
            findings.push({
              type: 'sensitive',
              pattern: rule.pattern.toString(),
              severity: 'high',
              description: rule.description,
            });
          } else if (rule.level === DataSensitivity.INTERNAL && sensitivity === DataSensitivity.PUBLIC) {
            sensitivity = DataSensitivity.INTERNAL;
            confidence = 0.75;
          }
        }
      }

      const result = {
        sensitivity,
        confidence,
        findings,
        summary:
          sensitivity === DataSensitivity.CONFIDENTIAL
            ? '检测到高度敏感信息，建议仅使用本地模型处理'
            : sensitivity === DataSensitivity.INTERNAL
            ? '检测到内部敏感内容，建议限制使用范围'
            : '未检测到敏感信息，可正常使用',
      };

      set({ lastScanResult: result });
      return result;
    } catch (err) {
      throw err;
    }
  },

  updateDomainControl: (config) => {
    set({
      ...config,
    });
    
    // TODO: 调用 IPC 接口持久化配置
    // window.electron.security.saveDomainControl(config);
    console.log('[SecurityStore] 出域控制配置已更新:', config);
  },

  loadAuditLogs: async (filters) => {
    try {
      set({ logLoading: true, logError: null });
      
      // TODO: 调用 IPC 接口获取审计日志
      // const logs = await window.electron.security.getAuditLogs({ filters });
      
      // 临时模拟数据
      await new Promise(resolve => setTimeout(resolve, 500));
      const logs: AuditLogEntry[] = [
        {
          id: '1',
          timestamp: new Date(),
          action: 'model_call',
          severity: 'info',
          user: 'admin',
          resource: 'qwen-max',
          details: '成功调用通义千问模型',
          read: false,
        },
      ];

      set({ auditLogs: logs, logLoading: false });
    } catch (err) {
      set({ logError: `加载日志失败: ${err}`, logLoading: false });
      throw err;
    }
  },

  exportAuditLogs: async (format) => {
    try {
      // TODO: 调用 IPC 接口导出日志
      // await window.electron.security.exportAuditLogs({ format });
      console.log(`[SecurityStore] 导出审计日志: ${format}`);
    } catch (err) {
      throw err;
    }
  },

  saveSandboxPolicy: async (policy) => {
    try {
      set({ policyLoading: true });
      
      // TODO: 调用 IPC 接口保存策略
      // await window.electron.security.saveSandboxPolicy({ policy });
      
      set({ sandboxPolicy: policy, policyLoading: false });
      console.log('[SecurityStore] 沙箱策略已保存:', policy);
    } catch (err) {
      set({ policyLoading: false });
      throw err;
    }
  },

  loadSandboxPolicy: async (workspaceId) => {
    try {
      set({ policyLoading: true });
      
      // TODO: 调用 IPC 接口获取策略
      // const policy = await window.electron.security.getSandboxPolicy({ workspaceId });
      
      // 临时默认策略
      const policy: SandboxPolicy = {
        workspaceId,
        executionLevel: 'L1',
        fileAllowlist: ['C:/Users/*/Documents/**'],
        fileBlocklist: ['C:/Windows/**'],
        toolAllowlist: [],
        toolBlocklist: [],
        maxCpuPercent: 50,
        maxMemoryMB: 512,
        maxDiskWriteMbps: 10,
      };

      set({ sandboxPolicy: policy, policyLoading: false });
    } catch (err) {
      set({ policyLoading: false });
      throw err;
    }
  },

  reset: () => {
    set({
      lastScanResult: null,
      domainControlEnabled: false,
      domainMode: 'whitelist',
      allowedDomains: ['localhost', '127.0.0.1'],
      blockedDomains: [],
      forceLocalForConfidential: true,
      hardDisableExternalAPI: false,
      auditLogs: [],
      logLoading: false,
      logError: null,
      sandboxPolicy: null,
      policyLoading: false,
    });
  },
}));
