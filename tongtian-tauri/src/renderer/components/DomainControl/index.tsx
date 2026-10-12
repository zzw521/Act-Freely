import React, { useState } from 'react';
import {
  Box,
  Card,
  CardContent,
  Typography,
  Switch,
  TextField,
  Button,
  Alert,
  Divider,
  Stack,
  Chip,
  IconButton,
  Tooltip,
  FormControlLabel,
  FormLabel,
  RadioGroup,
  Radio,
  Paper,
} from '@mui/material';
import SaveIcon from '@mui/icons-material/Save';
import RefreshIcon from '@mui/icons-material/Refresh';
import WarningIcon from '@mui/icons-material/Warning';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import BlockIcon from '@mui/icons-material/Block';
import { errorTextOf } from '../../../shared/error-messages';
import type { DomainControlConfig } from '../../store/securityStore';

interface DomainControlProps {
  initialConfig?: Partial<DomainControlConfig>;
  onSave: (config: DomainControlConfig) => Promise<void>;
  onReset?: () => Promise<void>;
}

const DomainControl: React.FC<DomainControlProps> = ({
  initialConfig,
  onSave,
  onReset,
}) => {
  const [config, setConfig] = useState<DomainControlConfig>({
    enabled: initialConfig?.enabled ?? false,
    mode: initialConfig?.mode ?? 'whitelist',
    allowedDomains: initialConfig?.allowedDomains ?? ['localhost', '127.0.0.1'],
    blockedDomains: initialConfig?.blockedDomains ?? [],
    forceLocalForConfidential: initialConfig?.forceLocalForConfidential ?? true,
    hardDisableExternalAPI: initialConfig?.hardDisableExternalAPI ?? false,
  });

  const [newDomain, setNewDomain] = useState('');
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

  const handleSave = async () => {
    try {
      setSaving(true);
      setMessage(null);
      await onSave(config);
      setMessage({ type: 'success', text: '配置已保存' });
    } catch (err) {
      setMessage({ type: 'error', text: `保存失败: ${errorTextOf(err)}` });
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    if (!onReset) return;

    try {
      setLoading(true);
      setMessage(null);
      await onReset();
      setMessage({ type: 'success', text: '已重置为默认配置' });
    } catch (err) {
      setMessage({ type: 'error', text: `重置失败: ${errorTextOf(err)}` });
    } finally {
      setLoading(false);
    }
  };

  const handleAddDomain = () => {
    if (!newDomain.trim()) return;

    if (config.mode === 'whitelist') {
      if (!config.allowedDomains.includes(newDomain.trim())) {
        setConfig({
          ...config,
          allowedDomains: [...config.allowedDomains, newDomain.trim()],
        });
      }
    } else {
      if (!config.blockedDomains.includes(newDomain.trim())) {
        setConfig({
          ...config,
          blockedDomains: [...config.blockedDomains, newDomain.trim()],
        });
      }
    }
    setNewDomain('');
  };

  const handleRemoveDomain = (domain: string, isWhitelist: boolean) => {
    if (isWhitelist) {
      setConfig({
        ...config,
        allowedDomains: config.allowedDomains.filter(d => d !== domain),
      });
    } else {
      setConfig({
        ...config,
        blockedDomains: config.blockedDomains.filter(d => d !== domain),
      });
    }
  };

  const getModeLabel = (mode: 'whitelist' | 'blacklist'): string => {
    return mode === 'whitelist' ? '🟢 白名单模式' : '🔴 黑名单模式';
  };

  return (
    <Box>
      {/* 核心开关 */}
      <Card 
        variant="outlined" 
        sx={{ 
          mb: 2, 
          borderColor: config.enabled ? 'primary.main' : 'divider',
          borderWidth: 2,
        }}
      >
        <CardContent>
          <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <Box>
              <Typography variant="h6" fontWeight="bold">
                🛡️ 出域控制总开关
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                控制敏感数据是否允许发送到外部 API
              </Typography>
            </Box>
            <Switch
              checked={config.enabled}
              onChange={(e) => setConfig({ ...config, enabled: e.target.checked })}
            />
          </Box>
        </CardContent>
      </Card>

      {/* 错误模式警告 */}
      {config.hardDisableExternalAPI && (
        <Alert
          severity="warning"
          icon={<WarningIcon />}
          action={
            <Tooltip title="当前处于严格数据保护模式">
              <Chip
                label="🔒 严格保护模式"
                size="small"
                color="secondary"
                sx={{ fontWeight: 'medium' }}
              />
            </Tooltip>
          }
          sx={{ mb: 2 }}
        >
          <strong>硬开关已开启：</strong>所有外部 API 调用已被禁用，仅允许本地模型处理所有数据。
        </Alert>
      )}

      {message && (
        <Alert
          severity={message.type}
          onClose={() => setMessage(null)}
          sx={{ mb: 2 }}
        >
          {message.text}
        </Alert>
      )}

      {/* 控制策略 */}
      {config.enabled && (
        <Card variant="outlined" sx={{ mb: 2 }}>
          <CardContent>
            <Typography variant="subtitle2" fontWeight="medium" sx={{ mb: 2 }}>
              ⚙️ 控制策略
            </Typography>

            {/* 模式选择 */}
            <FormControlComponent Label="域名过滤模式" sx={{ mb: 2 }}>
              <RadioGroup
                value={config.mode}
                onChange={(e) => setConfig({ ...config, mode: e.target.value as 'whitelist' | 'blacklist' })}
              >
                <Stack spacing={1}>
                  <Paper
                    elevation={config.mode === 'whitelist' ? 2 : 0}
                    sx={{
                      p: 2,
                      border: 2,
                      borderRadius: 1,
                      borderColor: config.mode === 'whitelist' ? 'primary.main' : 'divider',
                      cursor: 'pointer',
                    }}
                  >
                    <FormControlLabel
                      value="whitelist"
                      control={<Radio />}
                      label={
                        <Box>
                          <Typography variant="body2" fontWeight="medium">
                            🟢 白名单模式（推荐）
                          </Typography>
                          <Typography variant="caption" color="text.secondary">
                            仅允许列表中的域名接收数据
                          </Typography>
                        </Box>
                      }
                      sx={{ margin: 0 }}
                    />
                  </Paper>
                  <Paper
                    elevation={config.mode === 'blacklist' ? 2 : 0}
                    sx={{
                      p: 2,
                      border: 2,
                      borderRadius: 1,
                      borderColor: config.mode === 'blacklist' ? 'primary.main' : 'divider',
                      cursor: 'pointer',
                    }}
                  >
                    <FormControlLabel
                      value="blacklist"
                      control={<Radio />}
                      label={
                        <Box>
                          <Typography variant="body2" fontWeight="medium">
                            🔴 黑名单模式
                          </Typography>
                          <Typography variant="caption" color="text.secondary">
                            阻止列表中的域名接收数据
                          </Typography>
                        </Box>
                      }
                      sx={{ margin: 0 }}
                    />
                  </Paper>
                </Stack>
              </RadioGroup>
            </FormControlComponent>

            <Divider sx={{ my: 2 }} />

            {/* 域名列表管理 */}
            <Typography variant="subtitle2" sx={{ mb: 1 }}>
              {config.mode === 'whitelist' ? '✅ 允许的域名' : '❌ 阻止的域名'}
            </Typography>
            
            <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
              <TextField
                fullWidth
                size="small"
                placeholder="输入域名（如：api.example.com）"
                value={newDomain}
                onChange={(e) => setNewDomain(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleAddDomain()}
              />
              <Button
                variant="contained"
                onClick={handleAddDomain}
                disabled={!newDomain.trim()}
                sx={{ minWidth: 80 }}
              >
                添加
              </Button>
            </Stack>

            <Stack spacing={0.5} sx={{ mb: 2 }}>
              {(config.mode === 'whitelist' ? config.allowedDomains : config.blockedDomains).map((domain: string) => (
                <Chip
                  key={domain}
                  label={domain}
                  variant="outlined"
                  size="small"
                  onDelete={() => handleRemoveDomain(domain, config.mode === 'whitelist')}
                  sx={{ m: 0.25 }}
                />
              ))}
              {(config.mode === 'whitelist' ? config.allowedDomains : config.blockedDomains).length === 0 && (
                <Typography variant="caption" color="text.secondary" sx={{ fontStyle: 'italic' }}>
                  暂无记录，请添加域名
                </Typography>
              )}
            </Stack>

            <Divider sx={{ my: 2 }} />

            {/* 高级选项 */}
            <Typography variant="subtitle2" sx={{ mb: 1 }}>
              🔐 高级选项
            </Typography>

            <FormControlLabel
              control={
                <Switch
                  checked={config.forceLocalForConfidential}
                  onChange={(e) => setConfig({ ...config, forceLocalForConfidential: e.target.checked })}
                />
              }
              label={
                <Box>
                  <Typography variant="body2" fontWeight="medium">
                    强制机密数据使用本地模型
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    检测到 PII/机密信息时自动切换到 Ollama 等本地模型
                  </Typography>
                </Box>
              }
              sx={{ mt: 1 }}
            />

            <FormControlLabel
              control={
                <Switch
                  checked={config.hardDisableExternalAPI}
                  onChange={(e) => setConfig({ ...config, hardDisableExternalAPI: e.target.checked })}
                />
              }
              label={
                <Box>
                  <Typography variant="body2" fontWeight="medium" color="error.main">
                    硬开关：一键禁用所有外部 API
                  </Typography>
                  <Typography variant="caption" color="text.secondary">
                    企业合规强制要求，完全切断外网连接
                  </Typography>
                </Box>
              }
            />
          </CardContent>
        </Card>
      )}

      {/* 操作按钮 */}
      <Box sx={{ display: 'flex', gap: 2, justifyContent: 'flex-end' }}>
        {onReset && (
          <Button
            onClick={handleReset}
            disabled={loading}
            startIcon={loading ? <RefreshIcon /> : <RefreshIcon />}
            variant="outlined"
          >
            重置默认
          </Button>
        )}
        <Button
          onClick={handleSave}
          disabled={saving || !config.enabled}
          startIcon={<SaveIcon />}
          variant="contained"
        >
          {saving ? '保存中...' : '保存配置'}
        </Button>
      </Box>
    </Box>
  );
};

// 辅助组件：带标签的表单控件
interface FormControlComponentProps {
  Label: string;
  children: React.ReactNode;
  sx?: object;
}

const FormControlComponent: React.FC<FormControlComponentProps> = ({ Label, children, sx }) => (
  <Box sx={sx}>
    <FormLabel component="legend">{Label}</FormLabel>
    {children}
  </Box>
);

export default DomainControl;
