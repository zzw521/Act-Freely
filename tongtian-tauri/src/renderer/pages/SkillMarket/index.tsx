/**
 * 通天办公 - 技能市场页面
 * 
 * 功能:
 * - 技能浏览与搜索
 * - 技能安装/卸载
 * - 技能详情查看
 */

import React from 'react';
import { Box, Typography, Paper, useTheme } from '@mui/material';
import { Extension as ExtensionIcon } from '@mui/icons-material';

const SkillMarket: React.FC = () => {
  const theme = useTheme();

  return (
    <Box>
      <Typography variant="h5" sx={{ fontWeight: 700, marginBottom: theme.spacing(3) }}>
        技能市场
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
        <ExtensionIcon sx={{ fontSize: 64, color: theme.palette.primary.main, marginBottom: theme.spacing(2) }} />
        <Typography variant="h6" sx={{ fontWeight: 600, marginBottom: theme.spacing(1) }}>
          技能市场
        </Typography>
        <Typography variant="body2" color="text.secondary">
          浏览、安装和管理 AI 技能插件，扩展助手能力
        </Typography>
      </Paper>
    </Box>
  );
};

export default SkillMarket;
