import React, { useState, useMemo } from 'react';
import {
  Box,
  Paper,
  Typography,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Chip,
  TextField,
  InputAdornment,
  Button,
  IconButton,
  Tooltip,
  MenuItem,
  FormControl,
  InputLabel,
  Select,
  Stack,
  Card,
  CardContent,
  Pagination,
} from '@mui/material';
import FilterListIcon from '@mui/icons-material/FilterList';
import DownloadIcon from '@mui/icons-material/Download';
import RefreshIcon from '@mui/icons-material/Refresh';
import SearchIcon from '@mui/icons-material/Search';
import VisibilityIcon from '@mui/icons-material/Visibility';
import DeleteIcon from '@mui/icons-material/Delete';
import { AuditLogEntry } from '../../../shared/types';

interface AuditLogProps {
  logs: AuditLogEntry[];
  onExport: (format: 'csv' | 'json') => Promise<void>;
  onDeleteOldLogs?: (days: number) => Promise<void>;
  /** 重新拉取审计日志（不整页刷新） */
  onRefresh?: () => Promise<void>;
  isLoading?: boolean;
}

const AuditLog: React.FC<AuditLogProps> = ({
  logs,
  onExport,
  onDeleteOldLogs,
  onRefresh,
  isLoading,
}) => {
  const [searchQuery, setSearchQuery] = useState('');
  const [filterType, setFilterType] = useState<string>('all');
  const [filterSeverity, setFilterSeverity] = useState<string>('all');
  const [dateRange, setDateRange] = useState<'7d' | '30d' | '90d' | 'all'>('30d');
  const [currentPage, setCurrentPage] = useState(1);
  const itemsPerPage = 20;

  // 过滤和搜索
  const filteredLogs = useMemo(() => {
    let result = [...logs];

    // 按时间范围过滤
    const now = new Date();
    const cutoffDate = new Date();
    switch (dateRange) {
      case '7d':
        cutoffDate.setDate(now.getDate() - 7);
        break;
      case '30d':
        cutoffDate.setDate(now.getDate() - 30);
        break;
      case '90d':
        cutoffDate.setDate(now.getDate() - 90);
        break;
      case 'all':
        cutoffDate.setTime(0);
        break;
    }
    
    result = result.filter(log => new Date(log.timestamp) >= cutoffDate);

    // 按类型过滤
    if (filterType !== 'all') {
      result = result.filter(log => log.action === filterType);
    }

    // 按严重程度过滤
    if (filterSeverity !== 'all') {
      result = result.filter(log => log.severity === filterSeverity);
    }

    // 按搜索关键词过滤
    if (searchQuery.trim()) {
      const query = searchQuery.toLowerCase();
      result = result.filter(log =>
        log.user?.toLowerCase().includes(query) ||
        log.resource?.toLowerCase().includes(query) ||
        log.details?.toLowerCase().includes(query)
      );
    }

    // 按时间倒序
    result.sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime());

    return result;
  }, [logs, searchQuery, filterType, filterSeverity, dateRange]);

  // 分页
  const paginatedLogs = filteredLogs.slice(
    (currentPage - 1) * itemsPerPage,
    currentPage * itemsPerPage
  );

  // 统计信息
  const stats = useMemo(() => {
    const totalLogs = logs.length;
    const errorCount = logs.filter(l => l.severity === 'error').length;
    const warningCount = logs.filter(l => l.severity === 'warning').length;
    const todayCount = logs.filter(l => {
      const logDate = new Date(l.timestamp);
      const today = new Date();
      return logDate.toDateString() === today.toDateString();
    }).length;

    return { totalLogs, errorCount, warningCount, todayCount };
  }, [logs]);

  const getActionLabel = (action: string): string => {
    const labels: Record<string, string> = {
      model_call: '🤖 模型调用',
      file_write: '📝 文件写入',
      file_read: '📖 文件读取',
      route_switch: '🔄 路由切换',
      config_change: '⚙️ 配置变更',
      user_login: '👤 用户登录',
      user_logout: '👋 用户登出',
      skill_install: '📦 技能安装',
      mcp_connect: '🔌 MCP连接',
      sandbox_violation: '⚠️ 沙箱违规',
    };
    return labels[action] || action;
  };

  const getSeverityColor = (severity: string): 'error' | 'warning' | 'info' | 'default' => {
    const colors: Record<string, 'error' | 'warning' | 'info' | 'default'> = {
      error: 'error',
      warning: 'warning',
      info: 'info',
      debug: 'default',
    };
    return colors[severity] || 'info';
  };

  const formatDate = (date: string | Date): string => {
    const d = new Date(date);
    return d.toLocaleString('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    });
  };

  return (
    <Box>
      {/* 统计卡片 */}
      <Stack direction="row" spacing={2} sx={{ mb: 3 }}>
        <Card variant="outlined" sx={{ flex: 1 }}>
          <CardContent>
            <Typography variant="caption" color="text.secondary">
              今日日志
            </Typography>
            <Typography variant="h4" fontWeight="bold" color="primary.main">
              {stats.todayCount}
            </Typography>
          </CardContent>
        </Card>
        <Card variant="outlined" sx={{ flex: 1 }}>
          <CardContent>
            <Typography variant="caption" color="text.secondary">
              总日志数
            </Typography>
            <Typography variant="h4" fontWeight="bold">
              {stats.totalLogs}
            </Typography>
          </CardContent>
        </Card>
        <Card variant="outlined" sx={{ flex: 1 }}>
          <CardContent>
            <Typography variant="caption" color="text.secondary">
              错误数
            </Typography>
            <Typography variant="h4" fontWeight="bold" color="error.main">
              {stats.errorCount}
            </Typography>
          </CardContent>
        </Card>
        <Card variant="outlined" sx={{ flex: 1 }}>
          <CardContent>
            <Typography variant="caption" color="text.secondary">
              警告数
            </Typography>
            <Typography variant="h4" fontWeight="bold" color="warning.main">
              {stats.warningCount}
            </Typography>
          </CardContent>
        </Card>
      </Stack>

      {/* 筛选工具栏 */}
      <Paper variant="outlined" sx={{ p: 2, mb: 2 }}>
        <Stack direction="row" spacing={2} alignItems="center">
          {/* 搜索框 */}
          <TextField
            size="small"
            placeholder="搜索日志内容..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            InputProps={{
              startAdornment: (
                <InputAdornment position="start">
                  <SearchIcon fontSize="small" />
                </InputAdornment>
              ),
            }}
            sx={{ minWidth: 280 }}
          />

          {/* 类型筛选 */}
          <FormControl size="small" sx={{ minWidth: 160 }}>
            <InputLabel>操作类型</InputLabel>
            <Select
              value={filterType}
              label="操作类型"
              onChange={(e) => setFilterType(e.target.value)}
              startAdornment={
                <InputAdornment position="start">
                  <FilterListIcon fontSize="small" />
                </InputAdornment>
              }
            >
              <MenuItem value="all">全部类型</MenuItem>
              <MenuItem value="model_call">模型调用</MenuItem>
              <MenuItem value="file_write">文件写入</MenuItem>
              <MenuItem value="file_read">文件读取</MenuItem>
              <MenuItem value="route_switch">路由切换</MenuItem>
              <MenuItem value="config_change">配置变更</MenuItem>
              <MenuItem value="user_login">用户登录</MenuItem>
            </Select>
          </FormControl>

          {/* 严重程度筛选 */}
          <FormControl size="small" sx={{ minWidth: 140 }}>
            <InputLabel>严重程度</InputLabel>
            <Select
              value={filterSeverity}
              label="严重程度"
              onChange={(e) => setFilterSeverity(e.target.value)}
            >
              <MenuItem value="all">全部</MenuItem>
              <MenuItem value="error">错误</MenuItem>
              <MenuItem value="warning">警告</MenuItem>
              <MenuItem value="info">信息</MenuItem>
            </Select>
          </FormControl>

          {/* 时间范围 */}
          <FormControl size="small" sx={{ minWidth: 140 }}>
            <InputLabel>时间范围</InputLabel>
            <Select
              value={dateRange}
              label="时间范围"
              onChange={(e) => setDateRange(e.target.value as typeof dateRange)}
            >
              <MenuItem value="7d">最近 7 天</MenuItem>
              <MenuItem value="30d">最近 30 天</MenuItem>
              <MenuItem value="90d">最近 90 天</MenuItem>
              <MenuItem value="all">全部</MenuItem>
            </Select>
          </FormControl>

          {/* 导出按钮 */}
          <Button
            variant="outlined"
            startIcon={<DownloadIcon />}
            onClick={() => onExport('csv')}
            disabled={filteredLogs.length === 0}
          >
            导出 CSV
          </Button>
          <Button
            variant="outlined"
            startIcon={<DownloadIcon />}
            onClick={() => onExport('json')}
            disabled={filteredLogs.length === 0}
          >
            导出 JSON
          </Button>

          {/* 刷新按钮 */}
          <Tooltip title="刷新日志">
            <span>
              <IconButton
                onClick={() => void onRefresh?.()}
                disabled={!onRefresh || isLoading}
              >
                <RefreshIcon />
              </IconButton>
            </span>
          </Tooltip>
        </Stack>
      </Paper>

      {/* 日志表格 */}
      <TableContainer component={Paper} variant="outlined" sx={{ mb: 2 }}>
        <Table size="small">
          <TableHead>
            <TableRow sx={{ backgroundColor: 'background.default' }}>
              <TableCell>时间</TableCell>
              <TableCell>严重程度</TableCell>
              <TableCell>操作类型</TableCell>
              <TableCell>用户</TableCell>
              <TableCell>资源</TableCell>
              <TableCell>详情</TableCell>
              <TableCell align="center">操作</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {paginatedLogs.length === 0 ? (
              <TableRow>
                <TableCell colSpan={7} align="center" sx={{ py: 4 }}>
                  <Typography variant="body2" color="text.secondary">
                    暂无日志记录
                  </Typography>
                </TableCell>
              </TableRow>
            ) : (
              paginatedLogs.map((log, idx) => (
                <TableRow
                  key={log.id || idx}
                  sx={{
                    '&:hover': { backgroundColor: 'action.hover' },
                    opacity: log.read ? 1 : 0.7,
                  }}
                >
                  <TableCell sx={{ whiteSpace: 'nowrap', fontSize: '0.8rem' }}>
                    {formatDate(log.timestamp)}
                  </TableCell>
                  <TableCell>
                    <Chip
                      label={log.severity}
                      size="small"
                      color={getSeverityColor(log.severity)}
                      sx={{ fontSize: '0.65rem', height: 20 }}
                    />
                  </TableCell>
                  <TableCell>
                    <Typography variant="body2" fontSize="0.8rem">
                      {getActionLabel(log.action)}
                    </Typography>
                  </TableCell>
                  <TableCell sx={{ fontSize: '0.8rem' }}>
                    {log.user || '-'}
                  </TableCell>
                  <TableCell sx={{ fontSize: '0.8rem', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {log.resource || '-'}
                  </TableCell>
                  <TableCell sx={{ fontSize: '0.8rem', maxWidth: 300, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {log.details || '-'}
                  </TableCell>
                  <TableCell align="center">
                    <Tooltip title="查看详情">
                      <IconButton size="small">
                        <VisibilityIcon fontSize="small" />
                      </IconButton>
                    </Tooltip>
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </TableContainer>

      {/* 分页 */}
      {filteredLogs.length > itemsPerPage && (
        <Box sx={{ display: 'flex', justifyContent: 'center', mt: 2 }}>
          <Pagination
            count={Math.ceil(filteredLogs.length / itemsPerPage)}
            page={currentPage}
            onChange={(_, page) => setCurrentPage(page)}
            color="primary"
            showFirstButton
            showLastButton
          />
        </Box>
      )}
    </Box>
  );
};

export default AuditLog;
