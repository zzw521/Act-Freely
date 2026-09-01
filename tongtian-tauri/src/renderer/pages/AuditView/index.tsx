/**
 * 通天办公 - 审计日志页面
 * 
 * 功能:
 * - 操作日志查看
 * - 日志筛选与搜索
 * - 安全审计追踪
 */

import React from 'react';
import { Box, Typography, Paper, useTheme } from '@mui/material';
import { Assessment as AuditIcon } from '@mui/icons-material';

const AuditView: React.FC = () => {
  const theme = useTheme();

  return (
    <Box>
      <Typography variant="h5" sx={{ fontWeight: 700, marginBottom: theme.spacing(3) }}>
        审计日志
      </Typography>

      <Paper
        elevation={0}
        sx={{
          padding: theme.spacing(6),
          textAlign: 'center',
          border: `1px solid ${theme.palette.divider}`,
          borderRadius: '16px',
        }}
      >
        <AuditIcon sx={{ fontSize: 64, color: theme.palette.primary.main, marginBottom: theme.spacing(2) }} />
        <Typography variant="h6" sx={{ fontWeight: 600, marginBottom: theme.spacing(1) }}>
          审计日志
        </Typography>
        <Typography variant="body2" color="text.secondary">
          查看系统操作日志，追踪安全事件和合规记录
        </Typography>
      </Paper>
    </Box>
  );
};

export default AuditView;
