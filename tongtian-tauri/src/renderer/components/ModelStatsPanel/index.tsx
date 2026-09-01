/**
 * 通天办公 - 模型用量统计面板
 * 
 * 功能:
 * - 显示模型 Token 使用情况
 * - 可视化进度条展示
 * - 限额管理
 * - 使用统计信息
 */

import React from 'react';
import {
  Card,
  CardContent,
  Typography,
  LinearProgress,
  Box,
  Grid,
  Chip,
  Tooltip,
  Divider,
} from '@mui/material';
import {
  Storage as StorageIcon,
  Timeline as TimelineIcon,
  Warning as WarningIcon,
} from '@mui/icons-material';

// ── 类型定义 ──────────────────────────────────────────────────────

export interface ModelUsageStats {
  modelId: string;
  modelName: string;
  tokensUsed: number;
  tokensLimit?: number; // undefined 表示未设置限额
  usagePercent: number;
  lastUsedAt?: Date;
  period: 'daily' | 'monthly' | 'total';
}

interface ModelStatsPanelProps {
  stats: ModelUsageStats;
  onSetLimit?: (modelId: string) => void;
}

// ── 组件实现 ──────────────────────────────────────────────────────

const ModelStatsPanel: React.FC<ModelStatsPanelProps> = ({ stats, onSetLimit }) => {
  // 计算使用状态颜色
  const getProgressColor = () => {
    if (stats.usagePercent >= 90) return 'error';
    if (stats.usagePercent >= 70) return 'warning';
    return 'primary';
  };

  // 格式化数字
  const formatNumber = (num: number): string => {
    if (num >= 1000000) {
      return `${(num / 1000000).toFixed(1)}M`;
    }
    if (num >= 1000) {
      return `${(num / 1000).toFixed(1)}K`;
    }
    return num.toString();
  };

  return (
    <Card
      sx={{
        height: '100%',
        borderRadius: '12px',
        transition: 'all 0.2s',
        '&:hover': {
          boxShadow: 4,
          transform: 'translateY(-2px)',
        },
      }}
    >
      <CardContent>
        {/* 标题行 */}
        <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
          <Typography variant="h6" sx={{ fontWeight: 600 }}>
            {stats.modelName}
          </Typography>
          <Chip
            label={stats.period === 'daily' ? '今日' : stats.period === 'monthly' ? '本月' : '总计'}
            size="small"
            color="primary"
            variant="outlined"
          />
        </Box>

        {/* 进度条 */}
        <Box sx={{ mb: 2 }}>
          <Box sx={{ display: 'flex', justifyContent: 'space-between', mb: 1 }}>
            <Typography variant="body2" color="text.secondary">
              Token 使用量
            </Typography>
            <Typography variant="body2" color="text.secondary">
              {formatNumber(stats.tokensUsed)} / {stats.tokensLimit ? formatNumber(stats.tokensLimit) : '∞'}
            </Typography>
          </Box>
          
          <Tooltip title={`${stats.usagePercent.toFixed(1)}%`}>
            <LinearProgress
              variant={stats.tokensLimit ? 'determinate' : 'indeterminate'}
              value={stats.usagePercent}
              color={getProgressColor()}
              sx={{
                height: '8px',
                borderRadius: '4px',
                backgroundColor: '#e0e0e0',
              }}
            />
          </Tooltip>
        </Box>

        {/* 警告提示 */}
        {stats.usagePercent >= 90 && (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 2, p: 1, bgcolor: '#fff3cd', borderRadius: '4px' }}>
            <WarningIcon color="warning" fontSize="small" />
            <Typography variant="body2" color="warning.dark">
              用量已接近限额，请及时升级套餐
            </Typography>
          </Box>
        )}

        <Divider sx={{ my: 2 }} />

        {/* 统计信息 */}
        <Grid container spacing={2}>
          <Grid item xs={6}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
              <StorageIcon fontSize="small" color="action" />
              <Box>
                <Typography variant="caption" color="text.secondary" display="block">
                  已使用
                </Typography>
                <Typography variant="body2" sx={{ fontWeight: 600 }}>
                  {formatNumber(stats.tokensUsed)}
                </Typography>
              </Box>
            </Box>
          </Grid>

          <Grid item xs={6}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
              <TimelineIcon fontSize="small" color="action" />
              <Box>
                <Typography variant="caption" color="text.secondary" display="block">
                  最后使用
                </Typography>
                <Typography variant="body2" sx={{ fontWeight: 600 }}>
                  {stats.lastUsedAt
                    ? new Date(stats.lastUsedAt).toLocaleDateString('zh-CN')
                    : '从未'}
                </Typography>
              </Box>
            </Box>
          </Grid>
        </Grid>

        {/* 操作按钮 */}
        {onSetLimit && !stats.tokensLimit && (
          <Box sx={{ mt: 2 }}>
            <Typography
              variant="body2"
              color="primary"
              sx={{ cursor: 'pointer', '&:hover': { textDecoration: 'underline' } }}
              onClick={() => onSetLimit(stats.modelId)}
            >
              设置使用限额
            </Typography>
          </Box>
        )}
      </CardContent>
    </Card>
  );
};

export default ModelStatsPanel;
