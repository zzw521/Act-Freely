import React, { useState } from 'react';
import {
  List,
  ListItem,
  ListItemText,
  ListItemIcon,
  Chip,
  Box,
  Typography,
  Paper,
  IconButton,
  Tooltip,
  Divider,
  Collapse,
} from '@mui/material';
import FolderIcon from '@mui/icons-material/Folder';
import InsertDriveFileIcon from '@mui/icons-material/InsertDriveFile';
import DescriptionIcon from '@mui/icons-material/Description';
import TableChartIcon from '@mui/icons-material/TableChart';
import PictureAsPdfIcon from '@mui/icons-material/PictureAsPdf';
import ArticleIcon from '@mui/icons-material/Article';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import CancelIcon from '@mui/icons-material/Cancel';
import RefreshIcon from '@mui/icons-material/Refresh';
import DownloadIcon from '@mui/icons-material/Download';
import { FileOp, FileOpType, FileOpStatus } from '../../../shared/types';

interface FilePanelProps {
  fileOps: FileOp[];
  onDownload?: (fileOp: FileOp) => void;
  onRevealInFolder?: (fileOp: FileOp) => void;
}

const FilePanel: React.FC<FilePanelProps> = ({ fileOps, onDownload, onRevealInFolder }) => {
  const [expandedOps, setExpandedOps] = useState<Set<string>>(new Set());

  const getFileIcon = (fileName?: string, op?: FileOpType): React.ReactNode => {
    if (!fileName && !op) return <InsertDriveFileIcon />;

    const ext = fileName?.split('.').pop()?.toLowerCase();
    
    switch (ext) {
      case 'docx':
      case 'doc':
        return <DescriptionIcon color="primary" />;
      case 'xlsx':
      case 'xls':
        return <TableChartIcon color="success" />;
      case 'pdf':
        return <PictureAsPdfIcon color="error" />;
      case 'csv':
      case 'txt':
      case 'md':
        return <ArticleIcon color="action" />;
      default:
        return <InsertDriveFileIcon />;
    }
  };

  const getOpTypeLabel = (type: FileOpType): string => {
    const labels: Record<FileOpType, string> = {
      [FileOpType.READ]: '📖 读取',
      [FileOpType.WRITE]: '✍️ 写入',
      [FileOpType.CONVERT]: '🔄 转换',
      [FileOpType.ORGANIZE]: '📁 整理',
    };
    return labels[type] || type;
  };

  const getStatusChip = (status: FileOpStatus) => {
    const color = 
      status === FileOpStatus.SUCCESS ? 'success' :
      status === FileOpStatus.FAILED ? 'error' :
      status === FileOpStatus.ROLLED_BACK ? 'default' :
      'warning';

    const icon = 
      status === FileOpStatus.SUCCESS ? <CheckCircleIcon fontSize="small" /> :
      status === FileOpStatus.FAILED ? <CancelIcon fontSize="small" /> :
      status === FileOpStatus.ROLLED_BACK ? <RefreshIcon fontSize="small" /> :
      <RefreshIcon fontSize="small" sx={{ animation: 'spin 1s linear infinite' }} />;

    return (
      <Chip
        label={status}
        size="small"
        color={color as any}
        variant="filled"
        sx={{ fontSize: '0.65rem', height: 20 }}
        icon={icon}
      />
    );
  };

  const formatFileSize = (bytes?: number): string => {
    if (!bytes) return '';
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const handleToggleExpand = (id: string) => {
    const newExpanded = new Set(expandedOps);
    if (newExpanded.has(id)) {
      newExpanded.delete(id);
    } else {
      newExpanded.add(id);
    }
    setExpandedOps(newExpanded);
  };

  const completedOps = fileOps.filter(op => 
    op.status === FileOpStatus.SUCCESS || op.status === FileOpStatus.FAILED
  );
  const runningOps = fileOps.filter(op => 
    op.status === FileOpStatus.PENDING || op.status === FileOpStatus.RUNNING
  );

  if (fileOps.length === 0) {
    return (
      <Paper
        elevation={0}
        sx={{
          p: 4,
          textAlign: 'center',
          bgcolor: 'grey.50',
          border: '1px dashed',
          borderColor: 'divider',
        }}
      >
        <FolderIcon sx={{ fontSize: 48, color: 'text.disabled', mb: 1 }} />
        <Typography variant="body2" color="text.disabled">
          暂无文件操作记录
        </Typography>
      </Paper>
    );
  }

  return (
    <Box>
      {/* 统计信息 */}
      <Box sx={{ display: 'flex', gap: 1, mb: 2, flexWrap: 'wrap' }}>
        <Chip label={`总计: ${fileOps.length}`} size="small" />
        <Chip label={`成功: ${completedOps.filter(op => op.status === FileOpStatus.SUCCESS).length}`} size="small" color="success" variant="outlined" />
        <Chip label={`失败: ${completedOps.filter(op => op.status === FileOpStatus.FAILED).length}`} size="small" color="error" variant="outlined" />
        {runningOps.length > 0 && (
          <Chip label={`进行中: ${runningOps.length}`} size="small" color="warning" />
        )}
      </Box>

      <Divider sx={{ mb: 2 }} />

      {/* 文件操作列表 */}
      <List dense>
        {fileOps.map((op) => (
          <React.Fragment key={op.id}>
            <ListItem
              onClick={() => handleToggleExpand(op.id)}
              sx={{
                cursor: 'pointer',
                borderRadius: 1,
                mb: 0.5,
                bgcolor: expandedOps.has(op.id) ? 'action.selected' : 'transparent',
                '&:hover': { bgcolor: 'action.hover' },
              }}
            >
              <ListItemIcon sx={{ minWidth: 40 }}>
                {getFileIcon(op.fileName, op.op)}
              </ListItemIcon>
              <ListItemText
                primary={
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Typography variant="body2" noWrap sx={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {op.fileName || op.srcPath}
                    </Typography>
                    {getStatusChip(op.status)}
                  </Box>
                }
                secondary={
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Typography variant="caption" color="text.secondary">
                      {getOpTypeLabel(op.op)}
                    </Typography>
                    {op.fileSize && (
                      <Typography variant="caption" color="text.secondary">
                        · {formatFileSize(op.fileSize)}
                      </Typography>
                    )}
                  </Box>
                }
              />
              {onDownload && op.status === FileOpStatus.SUCCESS && op.dstPath && (
                <Tooltip title="下载">
                  <IconButton
                    size="small"
                    onClick={(e) => {
                      e.stopPropagation();
                      onDownload(op);
                    }}
                  >
                    <DownloadIcon fontSize="small" />
                  </IconButton>
                </Tooltip>
              )}
            </ListItem>

            {/* 展开详情 */}
            <Collapse in={expandedOps.has(op.id)} timeout="auto">
              <Paper elevation={0} sx={{ ml: 6, p: 2, mb: 1, bgcolor: 'grey.50' }}>
                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                  <Typography variant="caption" color="text.secondary">
                    <strong>源文件:</strong> {op.srcPath}
                  </Typography>
                  {op.dstPath && (
                    <Typography variant="caption" color="text.secondary">
                      <strong>目标文件:</strong> {op.dstPath}
                    </Typography>
                  )}
                  {op.backupPath && (
                    <Typography variant="caption" color="text.secondary">
                      <strong>备份路径:</strong> {op.backupPath}
                    </Typography>
                  )}
                  {onRevealInFolder && op.dstPath && (
                    <Tooltip title="在文件夹中显示">
                      <IconButton
                        size="small"
                        onClick={() => onRevealInFolder(op)}
                      >
                        <FolderIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  )}
                </Box>
              </Paper>
            </Collapse>

            <Divider variant="inset" component="li" />
          </React.Fragment>
        ))}
      </List>
    </Box>
  );
};

export default FilePanel;
