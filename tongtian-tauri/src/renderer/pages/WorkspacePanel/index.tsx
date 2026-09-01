/**
 * 通天办公 - 工作区管理页面
 * 
 * 功能:
 * - 工作区列表
 * - 工作区创建/切换/删除
 * - 工作区配置
 */

import React from 'react';
import { Box, Typography, Paper, useTheme } from '@mui/material';
import { Folder as FolderIcon } from '@mui/icons-material';

const WorkspacePanel: React.FC = () => {
  const theme = useTheme();

  return (
    <Box>
      <Typography variant="h5" sx={{ fontWeight: 700, marginBottom: theme.spacing(3) }}>
        工作区
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
        <FolderIcon sx={{ fontSize: 64, color: theme.palette.primary.main, marginBottom: theme.spacing(2) }} />
        <Typography variant="h6" sx={{ fontWeight: 600, marginBottom: theme.spacing(1) }}>
          工作区管理
        </Typography>
        <Typography variant="body2" color="text.secondary">
          创建和管理工作区，组织项目文件和对话上下文
        </Typography>
      </Paper>
    </Box>
  );
};

export default WorkspacePanel;
