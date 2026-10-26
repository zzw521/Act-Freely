/**
 * 通天办公 - 工作区管理页面
 *
 * 桌面端以 `{data_home}/workspaces/*` 目录为事实来源（Rust workspace_list/create），
 * 浏览器与 Mock 通道走同名口径的本地台账。当前工作区决定任务提交到哪个引擎实例，
 * 以及文件/沙箱策略按哪个空间隔离。不提供删除：删除会毁掉工作区文件。
 */

import React, { useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Paper,
  Stack,
  TextField,
  Tooltip,
  Typography,
  useTheme,
} from '@mui/material';
import FolderIcon from '@mui/icons-material/Folder';
import AddIcon from '@mui/icons-material/Add';
import RefreshIcon from '@mui/icons-material/Refresh';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import { useWorkspaceStore } from '../../store/workspaceStore';
import { workspaceAPI } from '../../lib/api';
import type { WorkspaceCapabilities } from '../../../shared/contracts';
import { errorTextOf } from '../../../shared/error-messages';

const sourceLabel = (caps: WorkspaceCapabilities): string => {
  switch (caps.dataRootSource) {
    case 'env':
      return `环境变量 ${caps.dataRootVariable}`;
    case 'appdata':
      return `默认（${caps.dataRootVariable}）`;
    case 'home':
      return `默认（${caps.dataRootVariable}）`;
    case 'cwd':
      return '当前工作目录';
    case 'browser':
      return '浏览器 localStorage';
    default:
      return caps.dataRootSource;
  }
};

const WorkspacePanel: React.FC = () => {
  const theme = useTheme();
  const {
    workspaces,
    activeId,
    loading,
    error,
    refresh,
    create,
    setActive,
  } = useWorkspaceStore();
  const [newId, setNewId] = useState('');
  const [creating, setCreating] = useState(false);
  const [actionError, setActionError] = useState('');
  const [caps, setCaps] = useState<WorkspaceCapabilities | null>(null);

  const loadCaps = React.useCallback(() => {
    workspaceAPI
      .capabilities()
      .then(setCaps)
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    refresh().catch(() => undefined);
    loadCaps();
  }, [refresh, loadCaps]);

  const handleCreate = async () => {
    const id = newId.trim();
    if (!id || creating) return;
    setCreating(true);
    setActionError('');
    try {
      await create(id);
      setNewId('');
    } catch (e) {
      setActionError(errorTextOf(e, '创建工作空间失败'));
    } finally {
      setCreating(false);
    }
  };

  return (
    <Box>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: theme.spacing(3) }}>
        <Typography variant="h5" sx={{ fontWeight: 700 }}>
          工作区
        </Typography>
        <Typography sx={{ fontSize: 12.5, color: 'text.secondary' }}>
          每个工作区有独立的引擎实例与文件目录；当前工作区决定新任务提交到哪里
        </Typography>
        <Box sx={{ flexGrow: 1 }} />
        <Button
          size="small"
          startIcon={loading ? <CircularProgress size={14} /> : <RefreshIcon />}
          onClick={() => {
            void refresh().catch(() => undefined);
            loadCaps();
          }}
          disabled={loading}
          sx={{ textTransform: 'none', fontSize: 12.5 }}
        >
          刷新
        </Button>
      </Box>

      {(error || actionError) && (
        <Alert severity="error" sx={{ mb: 2, borderRadius: '10px' }} onClose={() => setActionError('')}>
          {actionError || errorTextOf(error, '工作空间清单读取失败')}
        </Alert>
      )}

      {/* 后端抽象与数据根：只展示真实可用的后端，未提供的写明原因 */}
      <Paper
        data-testid="workspace-capabilities"
        elevation={0}
        sx={{
          p: 2,
          mb: 2.5,
          border: `1px solid ${theme.palette.divider}`,
          borderRadius: '14px',
        }}
      >
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1.25, flexWrap: 'wrap' }}>
          <Typography sx={{ fontSize: 13.5, fontWeight: 700 }}>后端与数据根</Typography>
          {caps && (
            <>
              <Chip
                size="small"
                label={`当前后端：${caps.backends.find((b) => b.id === caps.activeBackend)?.label ?? caps.activeBackend}`}
                sx={{ height: 22, fontSize: 11, bgcolor: '#ECFDF5', color: '#047857' }}
              />
              <Chip
                size="small"
                label={`数据根来源：${sourceLabel(caps)}`}
                sx={{ height: 22, fontSize: 11, bgcolor: '#EFF6FF', color: '#1D4ED8' }}
              />
              <Chip
                size="small"
                label={caps.deleteSupported ? '支持删除' : '不提供删除'}
                sx={{ height: 22, fontSize: 11, bgcolor: '#FEF2F2', color: '#B91C1C' }}
              />
            </>
          )}
        </Box>

        {caps ? (
          <>
            <Stack spacing={0.75} sx={{ mb: 1.25 }}>
              {caps.backends.map((b) => (
                <Box key={b.id} sx={{ display: 'flex', alignItems: 'flex-start', gap: 1 }}>
                  <Chip
                    size="small"
                    label={b.supported ? '可用' : '未提供'}
                    sx={{
                      height: 20,
                      fontSize: 10.5,
                      flexShrink: 0,
                      bgcolor: b.supported ? '#ECFDF5' : '#F3F4F6',
                      color: b.supported ? '#047857' : '#6B7280',
                    }}
                  />
                  <Typography sx={{ fontSize: 12.5, color: 'text.secondary' }}>
                    <strong>{b.label}</strong>：{b.supported ? b.isolation : b.reason}
                  </Typography>
                </Box>
              ))}
            </Stack>
            <Typography sx={{ fontSize: 12, color: 'text.secondary', wordBreak: 'break-all' }}>
              数据根：{caps.dataRoot}
              {caps.dataRootSource !== 'browser' && (caps.dataRootExists ? '' : '（目录尚未创建，首次使用时自动创建）')}
            </Typography>
            <Typography data-testid="workspace-custom-root" sx={{ fontSize: 12, color: 'text.secondary', mt: 0.5 }}>
              {caps.customRoot.supported
                ? `自定义数据根：设置环境变量 ${caps.customRoot.variable} 后${caps.customRoot.requiresRestart ? '重启应用' : ''}生效。${caps.customRoot.reason}`
                : caps.customRoot.reason}
            </Typography>
          </>
        ) : (
          <Typography sx={{ fontSize: 12, color: 'text.secondary' }}>能力读取中…</Typography>
        )}
      </Paper>

      {/* 创建工作区：标识即目录名，校验由后端按同一口径完成 */}
      <Paper
        elevation={0}
        sx={{
          p: 2,
          mb: 2.5,
          border: `1px solid ${theme.palette.divider}`,
          borderRadius: '14px',
          display: 'flex',
          alignItems: 'center',
          gap: 1.5,
          flexWrap: 'wrap',
        }}
      >
        <Typography sx={{ fontSize: 13.5, fontWeight: 600 }}>新建工作区</Typography>
        <TextField
          size="small"
          value={newId}
          onChange={(e) => setNewId(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void handleCreate();
          }}
          placeholder="标识：小写字母 / 数字 / 下划线 / 连字符"
          sx={{
            width: 300,
            '& .MuiOutlinedInput-root': { borderRadius: '10px', fontSize: 13 },
          }}
        />
        <Button
          variant="contained"
          size="small"
          disableElevation
          startIcon={<AddIcon />}
          onClick={() => void handleCreate()}
          disabled={creating || !newId.trim()}
          sx={{ textTransform: 'none', borderRadius: '10px', bgcolor: '#111827', '&:hover': { bgcolor: '#1F2937' } }}
        >
          {creating ? '创建中…' : '创建并切换'}
        </Button>
        <Typography sx={{ fontSize: 12, color: 'text.secondary', width: '100%' }}>
          标识会作为工作区目录名；创建幂等，重复创建不会覆盖已有目录。
        </Typography>
      </Paper>

      {workspaces.length === 0 && !loading ? (
        <Paper
          elevation={0}
          sx={{
            p: 5,
            textAlign: 'center',
            border: `1px solid ${theme.palette.divider}`,
            borderRadius: '16px',
          }}
        >
          <FolderIcon sx={{ fontSize: 48, color: theme.palette.primary.main, mb: 1 }} />
          <Typography sx={{ fontSize: 14, fontWeight: 600 }}>还没有工作空间</Typography>
          <Typography sx={{ fontSize: 12.5, color: 'text.secondary', mt: 0.5 }}>
            在上方输入标识创建第一个工作空间。
          </Typography>
        </Paper>
      ) : (
        <Box
          sx={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))',
            gap: 2,
          }}
        >
          {workspaces.map((w) => {
            const active = w.id === activeId;
            return (
              <Paper
                key={w.id}
                elevation={0}
                sx={{
                  p: 2.25,
                  borderRadius: '14px',
                  border: `1px solid ${active ? '#8B5CF6' : theme.palette.divider}`,
                  boxShadow: active ? '0 4px 16px rgba(139, 92, 246, 0.12)' : 'none',
                }}
              >
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1 }}>
                  <FolderIcon sx={{ fontSize: 20, color: active ? '#8B5CF6' : '#6B7280' }} />
                  <Typography sx={{ fontSize: 14.5, fontWeight: 700, wordBreak: 'break-all' }}>
                    {w.name || w.id}
                  </Typography>
                  {active && (
                    <Chip
                      size="small"
                      icon={<CheckCircleIcon sx={{ fontSize: 14 }} />}
                      label="当前"
                      sx={{ height: 22, fontSize: 11, bgcolor: '#F5F3FF', color: '#6D28D9', '& .MuiChip-icon': { color: '#6D28D9' } }}
                    />
                  )}
                  <Box sx={{ flexGrow: 1 }} />
                  <Chip
                    size="small"
                    label={w.running ? '引擎运行中' : w.exists ? '未运行' : '目录未创建'}
                    sx={{
                      height: 22,
                      fontSize: 11,
                      bgcolor: w.running ? '#ECFDF5' : '#F3F4F6',
                      color: w.running ? '#047857' : '#6B7280',
                    }}
                  />
                </Box>
                <Typography sx={{ fontSize: 12, color: 'text.secondary', wordBreak: 'break-all' }}>
                  {w.rootPath}
                </Typography>
                <Typography sx={{ fontSize: 12, color: 'text.secondary', mt: 0.5 }}>
                  {w.createdAtMs > 0 ? `创建于 ${new Date(w.createdAtMs).toLocaleString('zh-CN')}` : '尚未创建目录'}
                  {' · '}
                  {w.entryCount} 个条目
                  {w.port ? ` · 端口 ${w.port}` : ''}
                </Typography>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 1.5 }}>
                  <Button
                    size="small"
                    variant={active ? 'outlined' : 'contained'}
                    disableElevation
                    onClick={() => setActive(w.id)}
                    disabled={active}
                    sx={{
                      textTransform: 'none',
                      borderRadius: '9px',
                      fontSize: 12.5,
                      ...(active
                        ? {}
                        : { bgcolor: '#111827', '&:hover': { bgcolor: '#1F2937' } }),
                    }}
                  >
                    {active ? '当前工作区' : '切换到此处'}
                  </Button>
                  <Tooltip title="工作区删除会同时删掉文件与任务记录，因此不提供删除；如需清理请在文件管理器中处理">
                    <Typography sx={{ fontSize: 11.5, color: 'text.secondary', cursor: 'help' }}>
                      不支持删除
                    </Typography>
                  </Tooltip>
                </Box>
              </Paper>
            );
          })}
        </Box>
      )}

      <Stack direction="row" sx={{ mt: 2.5 }}>
        <Typography sx={{ fontSize: 12, color: 'text.secondary' }}>
          切换只改变新任务的提交目标，不会移动或删除任何已有文件。
        </Typography>
      </Stack>
    </Box>
  );
};

export default WorkspacePanel;
