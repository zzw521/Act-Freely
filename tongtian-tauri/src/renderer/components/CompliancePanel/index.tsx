/**
 * 合规概览（compliance:status / compliance:update 的真实消费方）
 *
 * 展示的是权威数据聚合：硬开关与出域控制（Rust `security::policy`）、
 * 按策略评估的模型名单（Rust `model_configs` 台账）、最近一次数据分级留痕（审计库）。
 * 非桌面通道没有模型台账，此处按 `allowlistEvaluated` 明示「未评估」，不画假的红绿清单。
 */

import React, { useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Chip,
  Divider,
  FormControlLabel,
  Paper,
  Stack,
  Switch,
  Tooltip,
  Typography,
} from '@mui/material';
import RefreshIcon from '@mui/icons-material/Refresh';
import { useSecurityStore } from '../../store/securityStore';
import { errorTextOf } from '../../../shared/error-messages';

const SENSITIVITY_LABELS: Record<string, string> = {
  public: '公开',
  internal: '内部',
  confidential: '机密',
};

const CompliancePanel: React.FC = () => {
  const {
    compliance,
    complianceLoading,
    complianceError,
    loadCompliance,
    updateCompliance,
  } = useSecurityStore();

  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

  const hardSwitchOn = Boolean(compliance?.hardSwitchEnabled);
  const dc = compliance?.domainControl;
  const lastCheck = compliance?.lastDataCheck ?? null;

  const toggleHardSwitch = async () => {
    try {
      setPending(true);
      setMessage(null);
      const resp = await updateCompliance({ hardSwitchEnabled: !hardSwitchOn });
      setMessage({
        type: 'success',
        text: resp?.changed
          ? `硬开关已${!hardSwitchOn ? '开启' : '关闭'}，策略即时生效`
          : '未产生变更（当前已是目标状态）',
      });
    } catch (err) {
      setMessage({ type: 'error', text: `更新合规策略失败: ${errorTextOf(err)}` });
    } finally {
      setPending(false);
    }
  };

  return (
    <Paper variant="outlined" sx={{ p: 2.5, borderRadius: '12px' }}>
      <Stack direction="row" alignItems="center" justifyContent="space-between" spacing={1}>
        <Typography sx={{ fontSize: 15, fontWeight: 650 }}>合规概览</Typography>
        <Button
          size="small"
          startIcon={<RefreshIcon />}
          onClick={() => void loadCompliance()}
          disabled={complianceLoading}
        >
          刷新
        </Button>
      </Stack>
      <Typography sx={{ fontSize: 12.5, color: 'text.secondary', mt: 0.5 }}>
        与出域控制、模型台账、审计留痕同源，任一处变更都会在此反映。
      </Typography>

      {message && (
        <Alert severity={message.type} sx={{ mt: 2 }} onClose={() => setMessage(null)}>
          {message.text}
        </Alert>
      )}
      {complianceError && (
        <Alert severity="error" sx={{ mt: 2 }}>
          {complianceError}
        </Alert>
      )}

      <Divider sx={{ my: 2 }} />

      {/* 硬开关：一键禁用所有外部（云端）模型 API */}
      <Box>
        <Tooltip title="开启后所有外部模型 API 调用都会被策略拦截，仅本地模型可用">
          <FormControlLabel
            control={
              <Switch
                checked={hardSwitchOn}
                onChange={toggleHardSwitch}
                disabled={pending || complianceLoading}
              />
            }
            label={
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                <Typography sx={{ fontSize: 14, fontWeight: 600 }}>
                  硬开关：禁用所有外部模型 API
                </Typography>
                <Chip
                  size="small"
                  label={hardSwitchOn ? '合规模式' : '正常模式'}
                  color={hardSwitchOn ? 'warning' : 'success'}
                  sx={{ fontWeight: 600 }}
                />
              </Box>
            }
          />
        </Tooltip>
      </Box>

      {/* 出域控制摘要 */}
      <Box sx={{ mt: 2 }}>
        <Typography sx={{ fontSize: 13, fontWeight: 600, mb: 0.75 }}>出域控制</Typography>
        <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
          <Chip
            size="small"
            label={`拦截开关：${dc?.enabled ? '已启用' : '未启用'}`}
            color={dc?.enabled ? 'success' : 'default'}
            variant={dc?.enabled ? 'filled' : 'outlined'}
          />
          <Chip
            size="small"
            label={`模式：${dc?.mode === 'blacklist' ? '黑名单放行其余' : '白名单拦截其余'}`}
            variant="outlined"
          />
          <Chip
            size="small"
            label={`白名单 ${dc?.allowedDomains.length ?? 0} 项 / 黑名单 ${dc?.blockedDomains.length ?? 0} 项`}
            variant="outlined"
          />
          <Chip
            size="small"
            label={`机密数据本地化：${dc?.forceLocalForConfidential ? '开' : '关'}`}
            variant="outlined"
          />
        </Stack>
      </Box>

      {/* 模型可达性评估 */}
      <Box sx={{ mt: 2 }}>
        <Typography sx={{ fontSize: 13, fontWeight: 600, mb: 0.75 }}>模型可达性</Typography>
        {compliance?.allowlistEvaluated ? (
          <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
            <Chip
              size="small"
              label={`可达 ${compliance.whitelistModelIds.length} 个`}
              color="success"
              variant="outlined"
            />
            <Chip
              size="small"
              label={`被拦 ${compliance.blockedModelIds.length} 个`}
              color={compliance.blockedModelIds.length > 0 ? 'warning' : 'default'}
              variant="outlined"
            />
            {compliance.blockedModelIds.length > 0 && (
              <Typography sx={{ fontSize: 12, color: 'text.secondary', alignSelf: 'center' }}>
                被拦模型：{compliance.blockedModelIds.join('、')}
              </Typography>
            )}
          </Stack>
        ) : (
          <Typography sx={{ fontSize: 12.5, color: 'text.secondary' }}>
            当前运行环境未接入模型台账，名单未评估（桌面版从模型中心读取后此处会给出可达/被拦明细）。
          </Typography>
        )}
      </Box>

      {/* 最近一次数据分级留痕 */}
      <Box sx={{ mt: 2 }}>
        <Typography sx={{ fontSize: 13, fontWeight: 600, mb: 0.75 }}>最近一次数据分级检测</Typography>
        {lastCheck ? (
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
            <Chip
              size="small"
              label={SENSITIVITY_LABELS[lastCheck.sensitivity] ?? lastCheck.sensitivity}
              color={lastCheck.sensitivity === 'confidential' ? 'error' : 'primary'}
              variant="outlined"
            />
            <Typography sx={{ fontSize: 12.5, color: 'text.secondary' }}>
              {new Date(lastCheck.timestamp).toLocaleString()}
            </Typography>
            {lastCheck.summary && (
              <Typography sx={{ fontSize: 12.5, color: 'text.secondary' }}>
                — {lastCheck.summary}
              </Typography>
            )}
          </Stack>
        ) : (
          <Typography sx={{ fontSize: 12.5, color: 'text.secondary' }}>
            尚无检测记录，在「数据分级」页发起检测后会在此留痕。
          </Typography>
        )}
      </Box>
    </Paper>
  );
};

export default CompliancePanel;
