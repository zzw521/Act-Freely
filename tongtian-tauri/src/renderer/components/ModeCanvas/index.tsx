/**
 * 工作台模式画布（设计 / 幻灯片 / 写作）
 *
 * 只展示本轮真实交付的产物：
 * - HTML 原型 → 沙箱 iframe（脚本可在沙箱内运行，拿不到宿主同源权限）
 * - Markdown / 文本 → 只读预览（当前没有写回文件的通道，不摆一个存不了的编辑器）
 * - .pptx → 应用内无法渲染，交给系统默认应用并如实回显失败
 * 没有匹配产物时按模式给空白画布，不伪造示例内容。
 */

import React, { useEffect, useMemo, useState } from 'react';
import { Alert, Box, Button, Chip, CircularProgress, IconButton, Tooltip, Typography } from '@mui/material';
import OpenInNewIcon from '@mui/icons-material/OpenInNew';
import RefreshIcon from '@mui/icons-material/Refresh';
import DesignServicesOutlinedIcon from '@mui/icons-material/DesignServicesOutlined';
import SlideshowOutlinedIcon from '@mui/icons-material/SlideshowOutlined';
import ArticleOutlinedIcon from '@mui/icons-material/ArticleOutlined';
import Markdown from 'react-markdown';
import type { FileOp, WorkMode } from '../../../shared/types';
import { fileAPI } from '../../lib/api';
import { errorTextOf } from '../../../shared/error-messages';
import { canvasKindOf, pickCanvasArtifacts, MODE_CANVAS_EMPTY } from '../../lib/mode-canvas';
import { useI18n } from '../../i18n';

interface ModeCanvasProps {
  mode: WorkMode;
  fileOps: FileOp[];
}

function fileNameOf(op: FileOp): string {
  return op.fileName ?? op.srcPath.split(/[/\\]/).pop() ?? op.srcPath;
}

/** 多轮交付同名文件时带上父目录，避免一排一模一样的「成果报告.md」 */
function chipLabelOf(op: FileOp, all: FileOp[]): string {
  const base = fileNameOf(op);
  if (all.filter((a) => fileNameOf(a) === base).length <= 1) return base;
  const parent = op.srcPath.replace(/\\/g, '/').split('/').slice(-2, -1)[0];
  return parent ? `${parent}/${base}` : base;
}

const EMPTY_ICON: Record<Exclude<WorkMode, 'general'>, React.ReactNode> = {
  design: <DesignServicesOutlinedIcon sx={{ fontSize: 30, color: '#94A3B8' }} />,
  slides: <SlideshowOutlinedIcon sx={{ fontSize: 30, color: '#94A3B8' }} />,
  writing: <ArticleOutlinedIcon sx={{ fontSize: 30, color: '#94A3B8' }} />,
};

const EmptyState: React.FC<{ mode: Exclude<WorkMode, 'general'> }> = ({ mode }) => {
  const { t: tr } = useI18n();
  const text = (
    <>
      <Box sx={{ mb: 1 }}>{EMPTY_ICON[mode]}</Box>
      <Typography sx={{ fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-2)', mb: 0.5 }}>
        {tr(MODE_CANVAS_EMPTY[mode])}
      </Typography>
      <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', lineHeight: 1.6 }}>
        {tr('产物生成后会在这里自动出现。')}
      </Typography>
    </>
  );
  if (mode === 'design') {
    return (
      <Box
        sx={{
          flex: 1,
          minHeight: 0,
          overflow: 'auto',
          p: 2,
          backgroundImage: 'radial-gradient(#D1D5DB 1px, transparent 1px)',
          backgroundSize: '18px 18px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        <Box sx={{ textAlign: 'center', maxWidth: 280, p: 2.5, borderRadius: '14px', border: '1px dashed #CBD5E1', bgcolor: 'rgba(255, 255, 255, 0.9)' }}>
          {text}
        </Box>
      </Box>
    );
  }
  if (mode === 'slides') {
    return (
      <Box sx={{ flex: 1, minHeight: 0, overflow: 'auto', p: 2, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <Box
          sx={{
            width: '100%',
            maxWidth: 420,
            aspectRatio: '16 / 9',
            borderRadius: '10px',
            border: '1px dashed #CBD5E1',
            bgcolor: '#fff',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            textAlign: 'center',
            p: 2,
          }}
        >
          <Box sx={{ maxWidth: 280 }}>{text}</Box>
        </Box>
      </Box>
    );
  }
  return (
    <Box sx={{ flex: 1, minHeight: 0, overflow: 'auto', p: 1.5 }}>
      <Box
        sx={{
          minHeight: '100%',
          borderRadius: '10px',
          border: '1px dashed #CBD5E1',
          bgcolor: '#fff',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          textAlign: 'center',
          p: 3,
        }}
      >
        <Box sx={{ maxWidth: 280 }}>{text}</Box>
      </Box>
    </Box>
  );
};

const ModeCanvas: React.FC<ModeCanvasProps> = ({ mode, fileOps }) => {
  const { t: tr } = useI18n();
  const artifacts = useMemo(() => pickCanvasArtifacts(mode, fileOps), [mode, fileOps]);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [loadKey, setLoadKey] = useState(0);
  const [loaded, setLoaded] = useState<{
    path: string;
    status: 'loading' | 'ok' | 'error';
    content: string;
    error: string;
  } | null>(null);
  const [openErr, setOpenErr] = useState('');

  // 产物清单变化时保留仍存在的选择，否则落到最新一件（交付顺序的末尾）
  useEffect(() => {
    setSelectedPath((prev) => {
      if (prev && artifacts.some((a) => a.srcPath === prev)) return prev;
      return artifacts.length > 0 ? artifacts[artifacts.length - 1].srcPath : null;
    });
  }, [artifacts]);

  const selected = artifacts.find((a) => a.srcPath === selectedPath) ?? null;
  const kind = selected ? canvasKindOf(selected.srcPath) : null;

  useEffect(() => {
    if (!selected || kind === 'external') {
      setLoaded(null);
      return;
    }
    const path = selected.srcPath;
    let cancelled = false;
    setLoaded({ path, status: 'loading', content: '', error: '' });
    fileAPI
      .read(path)
      .then((r) => {
        if (!cancelled) setLoaded({ path, status: 'ok', content: r.content, error: '' });
      })
      .catch((e) => {
        if (!cancelled) {
          setLoaded({
            path,
            status: 'error',
            content: '',
            error: errorTextOf(e, tr('读取产物失败')),
          });
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.srcPath, kind, loadKey]);

  const handleOpenExternal = async () => {
    if (!selected) return;
    setOpenErr('');
    try {
      await fileAPI.openExternal(selected.srcPath);
    } catch (e) {
      setOpenErr(errorTextOf(e, tr('系统默认程序未能打开该文件')));
    }
  };

  const current = selected && loaded?.path === selected.srcPath ? loaded : null;
  const isLoading = Boolean(selected && kind !== 'external' && (!current || current.status === 'loading'));

  return (
    <Box sx={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      {/* 画布头：当前产物 + 只读说明 + 系统打开/刷新 */}
      <Box sx={{ px: 2, py: 1, borderBottom: '1px solid #F3F4F6', display: 'flex', alignItems: 'center', gap: 0.5 }}>
        <Typography
          noWrap
          title={selected?.srcPath}
          sx={{ flexGrow: 1, minWidth: 0, fontSize: '12px', fontWeight: 600, color: 'var(--tt-text-2)' }}
        >
          {selected ? fileNameOf(selected) : tr('暂无产物')}
        </Typography>
        {selected && kind !== 'external' && (
          <Tooltip title={tr('只读预览：画布不提供改写并保存文件的能力')}>
            <Chip
              size="small"
              label={tr('只读')}
              sx={{ height: 20, fontSize: '10.5px', bgcolor: 'var(--tt-surface-3)', color: 'var(--tt-text-3)' }}
            />
          </Tooltip>
        )}
        {selected && (
          <Tooltip title={tr('用系统默认应用打开')}>
            <IconButton size="small" sx={{ color: '#9CA3AF' }} onClick={() => void handleOpenExternal()}>
              <OpenInNewIcon sx={{ fontSize: 16 }} />
            </IconButton>
          </Tooltip>
        )}
        {selected && kind !== 'external' && (
          <Tooltip title={tr('刷新内容')}>
            <IconButton size="small" sx={{ color: '#9CA3AF' }} onClick={() => setLoadKey((k) => k + 1)}>
              <RefreshIcon sx={{ fontSize: 16 }} />
            </IconButton>
          </Tooltip>
        )}
      </Box>

      {/* 多件同类产物：切换查看，不把其他产物藏起来 */}
      {artifacts.length > 1 && (
        <Box sx={{ display: 'flex', gap: 0.5, px: 2, py: 0.75, overflowX: 'auto', borderBottom: '1px solid #F3F4F6' }}>
          {artifacts.map((a) => {
            const active = a.srcPath === selected?.srcPath;
            return (
              <Chip
                key={a.srcPath}
                size="small"
                label={chipLabelOf(a, artifacts)}
                title={a.srcPath}
                onClick={() => setSelectedPath(a.srcPath)}
                sx={{
                  flexShrink: 0,
                  height: 22,
                  fontSize: '11px',
                  bgcolor: active ? 'rgba(20, 184, 166, 0.10)' : 'transparent',
                  color: active ? '#0D9488' : 'var(--tt-text-3)',
                  border: `1px solid ${active ? 'rgba(20, 184, 166, 0.45)' : 'var(--tt-border)'}`,
                }}
              />
            );
          })}
        </Box>
      )}

      {openErr && (
        <Alert severity="error" sx={{ m: 1.5, mb: 0, py: 0, fontSize: '12px' }} onClose={() => setOpenErr('')}>
          {openErr}
        </Alert>
      )}

      {!selected ? (
        mode === 'general' ? null : (
          <EmptyState mode={mode} />
        )
      ) : kind === 'external' ? (
        <Box sx={{ flex: 1, minHeight: 0, overflow: 'auto', display: 'flex', alignItems: 'center', justifyContent: 'center', p: 2 }}>
          <Box sx={{ textAlign: 'center', maxWidth: 280 }}>
            <SlideshowOutlinedIcon sx={{ fontSize: 32, color: '#94A3B8', mb: 1 }} />
            <Typography sx={{ fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-2)', wordBreak: 'break-all' }}>
              {fileNameOf(selected)}
            </Typography>
            <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', mt: 0.75, lineHeight: 1.6 }}>
              {tr('演示文稿是二进制文件，应用内无法渲染预览。')}
            </Typography>
            <Button
              size="small"
              variant="outlined"
              onClick={() => void handleOpenExternal()}
              sx={{ mt: 1.5, borderRadius: '8px', fontSize: '12px', textTransform: 'none' }}
            >
              {tr('用系统默认应用打开')}
            </Button>
          </Box>
        </Box>
      ) : isLoading ? (
        <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 1 }}>
          <CircularProgress size={18} sx={{ color: '#14B8A6' }} />
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>{tr('正在读取产物…')}</Typography>
        </Box>
      ) : current?.status === 'error' ? (
        <Box sx={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 1, p: 2 }}>
          <Typography sx={{ fontSize: '12.5px', color: '#DC2626', textAlign: 'center' }}>{current.error}</Typography>
          <Button
            size="small"
            variant="outlined"
            onClick={() => setLoadKey((k) => k + 1)}
            sx={{ borderRadius: '8px', fontSize: '12px', textTransform: 'none' }}
          >
            {tr('重试')}
          </Button>
        </Box>
      ) : kind === 'html' ? (
        <Box
          component="iframe"
          srcDoc={current?.content ?? ''}
          sandbox="allow-scripts allow-forms"
          title={fileNameOf(selected)}
          sx={{ flex: 1, minHeight: 0, border: 'none', background: '#fff' }}
        />
      ) : kind === 'markdown' ? (
        <Box className="markdown-body" sx={{ flex: 1, minHeight: 0, overflow: 'auto', px: 2, py: 1.5, color: 'var(--tt-text-2)' }}>
          <Markdown>{current?.content ?? ''}</Markdown>
        </Box>
      ) : (
        <Box
          component="pre"
          sx={{
            flex: 1,
            minHeight: 0,
            overflow: 'auto',
            m: 0,
            p: 1.5,
            fontSize: '12px',
            lineHeight: 1.6,
            color: 'var(--tt-text-2)',
            fontFamily: "'SF Mono', Consolas, monospace",
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            bgcolor: 'var(--tt-chat-code-bg)',
          }}
        >
          {current?.content ?? ''}
        </Box>
      )}
    </Box>
  );
};

export default ModeCanvas;
