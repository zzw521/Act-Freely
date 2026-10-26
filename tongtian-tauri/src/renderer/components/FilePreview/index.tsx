/**
 * 通天办公 - 产物文件预览弹窗
 *
 * 点击交付物路径打开：向引擎 /api/files/content 拉取文件内容并内嵌预览：
 * - text/html  → iframe 渲染
 * - markdown   → Markdown 渲染
 * - 其他文本   → 等宽文本
 * 加载/错误状态显式呈现，不静默失败。
 */

import React, { useEffect, useState } from 'react';
import { Dialog, DialogTitle, DialogContent, Box, Typography, CircularProgress, IconButton, Tooltip, Alert } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import OpenInNewIcon from '@mui/icons-material/OpenInNew';
import Markdown from 'react-markdown';
import { resolveEngineBaseUrl } from '../../lib/engine-endpoint';
import { engineFetch } from '../../lib/engine-http';
import { fileAPI } from '../../lib/api';
import { errorTextOf } from '../../../shared/error-messages';
import { useI18n } from '../../i18n';

interface FilePreviewProps {
  /** 文件绝对路径（null 时关闭） */
  path: string | null;
  onClose: () => void;
}

interface FileData {
  fileName: string;
  mime: string;
  content: string;
}

const FilePreview: React.FC<FilePreviewProps> = ({ path, onClose }) => {
  const { t: tr } = useI18n();
  const [state, setState] = useState<'loading' | 'ok' | 'error'>('loading');
  const [data, setData] = useState<FileData | null>(null);
  const [errMsg, setErrMsg] = useState('');
  /** 用系统默认应用打开的失败说明（与预览失败分开：预览不了不等于文件打不开） */
  const [openErr, setOpenErr] = useState('');

  useEffect(() => {
    if (!path) return;
    let cancelled = false;
    setState('loading');
    setData(null);
    setOpenErr('');
    (async () => {
      try {
        // 按当前工作空间解析真实端口：第二个实例可能在 18001，不能写死 18000
        const baseUrl = await resolveEngineBaseUrl();
        const resp = await engineFetch(`${baseUrl}/api/files/content?path=${encodeURIComponent(path)}`);
        const json = (await resp.json()) as FileData & { ok?: boolean; message?: string };
        if (cancelled) return;
        if (!resp.ok || json.ok === false) {
          throw new Error(json.message || tr('打开失败（HTTP {status}）', { status: resp.status }));
        }
        setData({ fileName: json.fileName, mime: json.mime, content: json.content });
        setState('ok');
      } catch (e) {
        if (cancelled) return;
        setErrMsg(errorTextOf(e, tr('打开文件失败')));
        setState('error');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [path]);

  /** 用系统默认应用打开：docx/pdf/pptx/图片等此弹窗预览不了的产物走这里 */
  const handleOpenExternal = async () => {
    if (!path) return;
    setOpenErr('');
    try {
      await fileAPI.openExternal(path);
    } catch (e) {
      setOpenErr(errorTextOf(e, tr('打开失败')));
    }
  };

  return (
    <Dialog open={Boolean(path)} onClose={onClose} maxWidth="md" fullWidth PaperProps={{ sx: { borderRadius: '16px', height: '80vh' } }}>
      <DialogTitle sx={{ display: 'flex', alignItems: 'center', pb: 1 }}>
        <Typography sx={{ fontSize: '15px', fontWeight: 700, flexGrow: 1, wordBreak: 'break-all' }}>
          {data?.fileName ?? (path ? path.split(/[/\\]/).pop() : tr('文件预览'))}
        </Typography>
        <Tooltip title={tr('用系统默认应用打开')}>
          <span>
            <IconButton
              size="small"
              onClick={() => void handleOpenExternal()}
              disabled={!path}
              sx={{ color: '#64748B', mr: 0.5 }}
            >
              <OpenInNewIcon sx={{ fontSize: 18 }} />
            </IconButton>
          </span>
        </Tooltip>
        <IconButton size="small" onClick={onClose} sx={{ color: '#9CA3AF' }}>
          <CloseIcon sx={{ fontSize: 18 }} />
        </IconButton>
      </DialogTitle>
      <DialogContent sx={{ pt: 0, display: 'flex', flexDirection: 'column' }}>
        {openErr && (
          <Alert severity="error" sx={{ mb: 1, fontSize: '12.5px' }} onClose={() => setOpenErr('')}>
            {openErr}
          </Alert>
        )}
        {state === 'loading' && (
          <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 1.5 }}>
            <CircularProgress size={20} sx={{ color: '#8B5CF6' }} />
            <Typography sx={{ fontSize: '13px', color: '#9CA3AF' }}>{tr('正在打开文件…')}</Typography>
          </Box>
        )}
        {state === 'error' && (
          <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <Typography sx={{ fontSize: '13px', color: '#DC2626' }}>{errMsg}</Typography>
          </Box>
        )}
        {state === 'ok' && data && (
          <>
            {data.mime === 'text/html' ? (
              <Box
                component="iframe"
                srcDoc={data.content}
                sandbox=""
                sx={{ flex: 1, border: 'none', borderRadius: '10px', background: '#fff' }}
                title={data.fileName}
              />
            ) : data.mime === 'text/markdown' ? (
              <Box className="markdown-body" sx={{ flex: 1, overflow: 'auto', color: 'var(--tt-text-2)', pb: 2 }}>
                <Markdown>{data.content}</Markdown>
              </Box>
            ) : (
              <Box
                component="pre"
                sx={{
                  flex: 1,
                  overflow: 'auto',
                  m: 0,
                  p: 1.5,
                  borderRadius: '10px',
                  background: 'var(--tt-chat-code-bg)',
                  fontSize: '12.5px',
                  lineHeight: 1.6,
                  color: 'var(--tt-text-2)',
                  fontFamily: "'SF Mono', Consolas, monospace",
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {data.content}
              </Box>
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
};

export default FilePreview;
