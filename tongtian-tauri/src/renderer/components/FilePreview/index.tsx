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
import { Dialog, DialogTitle, DialogContent, Box, Typography, CircularProgress, IconButton } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import Markdown from 'react-markdown';
import { ENGINE_DIRECT_URL } from '../../lib/engine-api';

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
  const [state, setState] = useState<'loading' | 'ok' | 'error'>('loading');
  const [data, setData] = useState<FileData | null>(null);
  const [errMsg, setErrMsg] = useState('');

  useEffect(() => {
    if (!path) return;
    let cancelled = false;
    setState('loading');
    setData(null);
    (async () => {
      try {
        const resp = await fetch(`${ENGINE_DIRECT_URL}/api/files/content?path=${encodeURIComponent(path)}`);
        const json = (await resp.json()) as FileData & { ok?: boolean; message?: string };
        if (cancelled) return;
        if (!resp.ok || json.ok === false) {
          throw new Error(json.message || `打开失败（HTTP ${resp.status}）`);
        }
        setData({ fileName: json.fileName, mime: json.mime, content: json.content });
        setState('ok');
      } catch (e) {
        if (cancelled) return;
        setErrMsg(e instanceof Error ? e.message : String(e));
        setState('error');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [path]);

  return (
    <Dialog open={Boolean(path)} onClose={onClose} maxWidth="md" fullWidth PaperProps={{ sx: { borderRadius: '16px', height: '80vh' } }}>
      <DialogTitle sx={{ display: 'flex', alignItems: 'center', pb: 1 }}>
        <Typography sx={{ fontSize: '15px', fontWeight: 700, flexGrow: 1, wordBreak: 'break-all' }}>
          {data?.fileName ?? (path ? path.split(/[/\\]/).pop() : '文件预览')}
        </Typography>
        <IconButton size="small" onClick={onClose} sx={{ color: '#9CA3AF' }}>
          <CloseIcon sx={{ fontSize: 18 }} />
        </IconButton>
      </DialogTitle>
      <DialogContent sx={{ pt: 0, display: 'flex', flexDirection: 'column' }}>
        {state === 'loading' && (
          <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 1.5 }}>
            <CircularProgress size={20} sx={{ color: '#8B5CF6' }} />
            <Typography sx={{ fontSize: '13px', color: '#9CA3AF' }}>正在打开文件…</Typography>
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
              <Box className="markdown-body" sx={{ flex: 1, overflow: 'auto', color: '#334155', pb: 2 }}>
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
                  background: '#F8FAFC',
                  fontSize: '12.5px',
                  lineHeight: 1.6,
                  color: '#334155',
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
