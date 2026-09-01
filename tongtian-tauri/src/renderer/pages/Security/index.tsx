/**
 * 通天办公 - 安全与合规模块
 * 
 * 功能:
 * - 数据分级检测
 * - 出域控制开关
 * - 审计日志查看与导出
 * - 沙箱策略配置
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
} from '@mui/icons-material';
import DataClassification from '../../components/DataClassification';
import DomainControl from '../../components/DomainControl';
import AuditLog from '../../components/AuditLog';
import SandboxPolicy from '../../components/SandboxPolicy';
import { useSecurityStore } from '../../store/securityStore';

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
    lastScanResult,
    domainControlEnabled,
    auditLogs,
    logLoading,
    sandboxPolicy,
    scanData,
    updateDomainControl,
    loadAuditLogs,
    exportAuditLogs,
    saveSandboxPolicy,
    loadSandboxPolicy,
  } = useSecurityStore();

  // 加载审计日志和沙箱策略
  useEffect(() => {
    loadAuditLogs();
    loadSandboxPolicy('default-workspace');
  }, []);

  // 切换标签页
  const handleTabChange = (event: React.SyntheticEvent, newValue: number) => {
    setTabIndex(newValue);
    setError(null);
  };

  // 数据分级检测
  const handleScanData = async (text: string): Promise<any> => {
    try {
      setError(null);
      const result = await scanData(text);
      console.log('数据分级结果:', result);
      return result;
    } catch (err) {
      setError(`数据分级检测失败: ${err}`);
      throw err;
    }
  };

  // 出域控制保存
  const handleDomainControlSave = async (config: any): Promise<void> => {
    try {
      setError(null);
      updateDomainControl(config);
    } catch (err: any) {
      setError(`更新出域控制配置失败: ${err}`);
      throw err;
    }
  };

  // 导出审计日志
  const handleExportLogs = async (format: 'csv' | 'json'): Promise<void> => {
    try {
      setError(null);
      await exportAuditLogs(format);
    } catch (err: any) {
      setError(`导出日志失败: ${err}`);
      throw err;
    }
  };

  // 保存沙箱策略
  const handleSaveSandboxPolicy = async (policy: any): Promise<void> => {
    try {
      setError(null);
      await saveSandboxPolicy(policy);
    } catch (err: any) {
      setError(`保存沙箱策略失败: ${err}`);
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
            <DomainControl onSave={handleDomainControlSave} />
          </Box>
        </TabPanel>

        {/* 审计日志 */}
        <TabPanel value={tabIndex} index={2}>
          <Box sx={{ p: 3 }}>
            <AuditLog logs={auditLogs} onExport={handleExportLogs} isLoading={logLoading} />
          </Box>
        </TabPanel>

        {/* 沙箱策略 */}
        <TabPanel value={tabIndex} index={3}>
          <Box sx={{ p: 3 }}>
            <SandboxPolicy
              workspaceId="default-workspace"
              initialPolicy={sandboxPolicy || undefined}
              onSave={handleSaveSandboxPolicy}
            />
          </Box>
        </TabPanel>
      </Paper>
    </Box>
  );
};

export default SecurityPage;
