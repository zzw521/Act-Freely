/**
 * 通天办公 - 产物文件内容预览
 *
 * 向引擎 /api/files/content 拉取文件内容并按类型渲染：
 * - text/html  → iframe 渲染
 * - markdown   → Markdown 渲染
 * - 其他文本   → 等宽文本
 * 加载/错误状态显式呈现，不静默失败。
 *
 * 内容体与取数 hook 单独导出，供右侧任务栏内嵌预览与弹窗两种壳复用。
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

export interface FilePreviewData {
  fileName: string;
  mime: string;
  content: string;
}

export type FileContentState = 'idle' | 'loading' | 'ok' | 'error';

/** 按路径取文件正文：null 表示未选中文件（idle） */
export function useFileContent(path: string | null) {
  const { t: tr } = useI18n();
  const [state, setState] = useState<FileContentState>('idle');
  const [data, setData] = useState<FilePreviewData | null>(null);
  const [errMsg, setErrMsg] = useState('');

  useEffect(() => {
    if (!path) {
      setState('idle');
      setData(null);
      setErrMsg('');
      return;
    }
    let cancelled = false;
    setState('loading');
    setData(null);
    setErrMsg('');
    (async () => {
      try {
        // 按当前工作空间解析真实端口：第二个实例可能在 18001，不能写死 18000
        const baseUrl = await resolveEngineBaseUrl();
        const resp = await engineFetch(`${baseUrl}/api/files/content?path=${encodeURIComponent(path)}`);
        const json = (await resp.json()) as FilePreviewData & { ok?: boolean; message?: string };
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

  return { state, data, errMsg };
}

/** 内容体：加载 / 错误 / HTML / Markdown / 纯文本。外层负责给高度与留白 */
export const FilePreviewBody: React.FC<{
  state: FileContentState;
  data: FilePreviewData | null;
  errMsg: string;
}> = ({ state, data, errMsg }) => {
  const { t: tr } = useI18n();
  if (state === 'loading' || state === 'idle') {
    return (
      <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 1.5 }}>
        <CircularProgress size={20} sx={{ color: '#8B5CF6' }} />
        <Typography sx={{ fontSize: '13px', color: '#9CA3AF' }}>{tr('正在打开文件…')}</Typography>
      </Box>
    );
  }
  if (state === 'error' || !data) {
    return (
      <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', px: 2 }}>
        <Typography sx={{ fontSize: '13px', color: '#DC2626', textAlign: 'center' }}>{errMsg}</Typography>
      </Box>
    );
  }
  if (data.mime === 'text/html') {
    return (
      <Box
        component="iframe"
        srcDoc={data.content}
        sandbox=""
        sx={{ flex: 1, minHeight: 0, width: '100%', border: 'none', borderRadius: '10px', background: '#fff' }}
        title={data.fileName}
      />
    );
  }
  if (data.mime === 'text/markdown') {
    return (
      <Box className="markdown-body" sx={{ flex: 1, minHeight: 0, overflow: 'auto', color: 'var(--tt-text-2)', pb: 2 }}>
        <Markdown>{data.content}</Markdown>
      </Box>
    );
  }
  return (
    <Box
      component="pre"
      sx={{
        flex: 1,
        minHeight: 0,
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
  );
};

interface FilePreviewProps {
  /** 文件绝对路径（null 时关闭） */
  path: string | null;
  onClose: () => void;
}

/** 独立弹窗壳（保留给需要模态预览的场景） */
const FilePreview: React.FC<FilePreviewProps> = ({ path, onClose }) => {
  const { t: tr } = useI18n();
  const { state, data, errMsg } = useFileContent(path);
  /** 用系统默认应用打开的失败说明（与预览失败分开：预览不了不等于文件打不开） */
  const [openErr, setOpenErr] = useState('');

  useEffect(() => {
    setOpenErr('');
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
        <FilePreviewBody state={state} data={data} errMsg={errMsg} />
      </DialogContent>
    </Dialog>
  );
};

export default FilePreview;
