/**
 * 通天办公 - 安全与合规模块
 * 
 * 功能:
 * - 数据分级检测
 * - 出域控制开关
 * - 审计日志查看与导出
 * - 沙箱策略配置
 * - 合规概览（硬开关 / 模型可达性 / 最近分级留痕的聚合视图）
 */

import React, { useState, useEffect } from 'react';
import {
  Box,
  Typography,
  Paper,
  Tabs,
  Tab,
  Alert,
  useTheme,
} from '@mui/material';
import {
  Security as SecurityIcon,
  DataUsage as DataUsageIcon,
  NetworkCheck as NetworkCheckIcon,
  History as HistoryIcon,
  Policy as PolicyIcon,
  VerifiedUser as ComplianceIcon,
} from '@mui/icons-material';
import DataClassification from '../../components/DataClassification';
import DomainControl from '../../components/DomainControl';
import AuditLog from '../../components/AuditLog';
import SandboxPolicy from '../../components/SandboxPolicy';
import CompliancePanel from '../../components/CompliancePanel';
import { useSecurityStore, type DomainControlConfig } from '../../store/securityStore';
import { useWorkspaceStore } from '../../store/workspaceStore';
import { errorTextOf } from '../../../shared/error-messages';

interface TabPanelProps {
  children?: React.ReactNode;
  index: number;
  value: number;
}

const TabPanel: React.FC<TabPanelProps> = ({ children, value, index, ...other }) => (
  <div
    role="tabpanel"
    hidden={value !== index}
    id={`security-tabpanel-${index}`}
    aria-labelledby={`security-tab-${index}`}
    {...other}
  >
    {value === index && <Box sx={{ pt: 3 }}>{children}</Box>}
  </div>
);

const SecurityPage: React.FC = () => {
  const theme = useTheme();
  const [tabIndex, setTabIndex] = useState(0);
  const [error, setError] = useState<string | null>(null);

  // Security store
  const {
    domainControl,
    domainControlPersisted,
    auditLogs,
    logLoading,
    sandboxPolicy,
    scanData,
    loadDomainControl,
    updateDomainControl,
    loadCompliance,
    loadAuditLogs,
    exportAuditLogs,
    saveSandboxPolicy,
    loadSandboxPolicy,
  } = useSecurityStore();

  // 沙箱策略按当前工作空间隔离：与任务提交、引擎端点使用同一个 workspace
  const workspaceId = useWorkspaceStore((s) => s.activeId);
  const refreshWorkspaces = useWorkspaceStore((s) => s.refresh);

  // 首次进入加载出域控制/合规状态/审计日志（真实后端或本地 Mock）
  useEffect(() => {
    void refreshWorkspaces().catch((e) =>
      setError(`加载工作空间失败: ${errorTextOf(e)}`),
    );
    void Promise.all([
      loadDomainControl().catch((e) => setError(`加载出域控制失败: ${errorTextOf(e)}`)),
      loadCompliance(),
      loadAuditLogs().catch((e) => setError(`加载审计日志失败: ${errorTextOf(e)}`)),
    ]);
  }, [refreshWorkspaces, loadDomainControl, loadCompliance, loadAuditLogs]);

  // 切换工作空间后重新读取该空间的沙箱策略
  useEffect(() => {
    void loadSandboxPolicy(workspaceId).catch((e) =>
      setError(`加载沙箱策略失败: ${errorTextOf(e)}`),
    );
  }, [workspaceId, loadSandboxPolicy]);

  // 切换标签页（审计页每次进入都重新拉取，避免展示过期日志；合规概览同理）
  const handleTabChange = (event: React.SyntheticEvent, newValue: number) => {
    setTabIndex(newValue);
    setError(null);
    if (newValue === 2) {
      void loadAuditLogs().catch((e) => setError(`加载审计日志失败: ${errorTextOf(e)}`));
    }
    if (newValue === 4) {
      void loadCompliance();
    }
  };

  // 数据分级检测
  const handleScanData = async (text: string) => {
    try {
      setError(null);
      return await scanData(text);
    } catch (err) {
      setError(`数据分级检测失败: ${errorTextOf(err)}`);
      throw err;
    }
  };

  // 出域控制保存（后端归一化结果回写 store）
  const handleDomainControlSave = async (config: DomainControlConfig): Promise<void> => {
    try {
      setError(null);
      await updateDomainControl(config);
    } catch (err) {
      setError(`更新出域控制配置失败: ${errorTextOf(err)}`);
      throw err;
    }
  };

  // 导出审计日志
  const handleExportLogs = async (format: 'csv' | 'json'): Promise<void> => {
    try {
      setError(null);
      await exportAuditLogs(format);
    } catch (err) {
      setError(`导出日志失败: ${errorTextOf(err)}`);
      throw err;
    }
  };

  // 保存沙箱策略
  const handleSaveSandboxPolicy = async (policy: any): Promise<void> => {
    try {
      setError(null);
      await saveSandboxPolicy(policy);
    } catch (err) {
      setError(`保存沙箱策略失败: ${errorTextOf(err)}`);
      throw err;
    }
  };

  return (
    <Box>
      {/* 标题栏 */}
      <Box sx={{ mb: 3 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mb: 1 }}>
          <SecurityIcon sx={{ fontSize: 32, color: theme.palette.primary.main }} />
          <Typography variant="h5" sx={{ fontWeight: 700 }}>
            安全与合规
          </Typography>
        </Box>
        <Typography variant="body2" color="text.secondary">
          管理数据安全策略、出域控制、审计日志和沙箱配置
        </Typography>
      </Box>

      {/* 错误提示 */}
      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* 标签页导航 */}
      <Paper sx={{ borderRadius: '12px', overflow: 'hidden' }}>
        <Tabs
          value={tabIndex}
          onChange={handleTabChange}
          variant="scrollable"
          scrollButtons="auto"
          sx={{
            borderBottom: 1,
            borderColor: 'divider',
            px: 2,
            bgcolor: theme.palette.mode === 'dark' ? 'background.paper' : 'grey.50',
          }}
        >
          <Tab
            icon={<DataUsageIcon />}
            iconPosition="start"
            label="数据分级"
            sx={{ textTransform: 'none', minHeight: 64 }}
          />
          <Tab
            icon={<NetworkCheckIcon />}
            iconPosition="start"
            label="出域控制"
            sx={{ textTransform: 'none', minHeight: 64 }}
          />
          <Tab
            icon={<HistoryIcon />}
            iconPosition="start"
            label="审计日志"
            sx={{ textTransform: 'none', minHeight: 64 }}
          />
          <Tab
            icon={<PolicyIcon />}
            iconPosition="start"
            label="沙箱策略"
            sx={{ textTransform: 'none', minHeight: 64 }}
          />
          <Tab
            icon={<ComplianceIcon />}
            iconPosition="start"
            label="合规概览"
            sx={{ textTransform: 'none', minHeight: 64 }}
          />
        </Tabs>

        {/* 数据分级 */}
        <TabPanel value={tabIndex} index={0}>
          <Box sx={{ p: 3 }}>
            <DataClassification onScan={handleScanData} />
          </Box>
        </TabPanel>

        {/* 出域控制 */}
        <TabPanel value={tabIndex} index={1}>
          <Box sx={{ p: 3 }}>
            <DomainControl
              // 配置为异步加载：key 随归一化配置变化重挂载，保证表单拿到已落库值
              key={JSON.stringify(domainControl ?? {})}
              initialConfig={domainControl ?? undefined}
              onSave={handleDomainControlSave}
            />
            {!domainControlPersisted && (
              <Alert severity="info" sx={{ mt: 2 }}>
                当前展示的是默认配置，尚未保存到本地。开启出域控制并保存后生效。
              </Alert>
            )}
          </Box>
        </TabPanel>

        {/* 审计日志 */}
        <TabPanel value={tabIndex} index={2}>
          <Box sx={{ p: 3 }}>
            <AuditLog
              logs={auditLogs}
              onExport={handleExportLogs}
              onRefresh={() => loadAuditLogs()}
              isLoading={logLoading}
            />
          </Box>
        </TabPanel>

        {/* 沙箱策略 */}
        <TabPanel value={tabIndex} index={3}>
          <Box sx={{ p: 3 }}>
            <SandboxPolicy
              key={`${workspaceId}-${sandboxPolicy ? 'persisted' : 'default'}`}
              workspaceId={workspaceId}
              initialPolicy={sandboxPolicy || undefined}
              onSave={handleSaveSandboxPolicy}
            />
          </Box>
        </TabPanel>

        {/* 合规概览：硬开关 + 出域摘要 + 模型可达性 + 最近分级留痕（同源聚合，无独立存储） */}
        <TabPanel value={tabIndex} index={4}>
          <Box sx={{ p: 3 }}>
            <CompliancePanel />
          </Box>
        </TabPanel>
      </Paper>
    </Box>
  );
};

export default SecurityPage;
