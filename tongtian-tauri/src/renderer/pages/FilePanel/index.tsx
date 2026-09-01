/**
 * 通天办公 - 文件管理页面（核心闭环补齐 · 流 4）
 *
 * 功能（真实接入 file_list / file_read / file_preview）：
 * - 工作区文件目录浏览（白名单内）
 * - 文本文件预览（txt/md/csv/json，2MB 上限）
 * - docx/xlsx/pdf 显式提示「转换能力下批落地」（不静默）
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Breadcrumbs,
  Button,
  List,
  ListItemButton,
  ListItemIcon,
  ListItemText,
  Paper,
  Typography,
  useTheme,
} from '@mui/material';
import FolderIcon from '@mui/icons-material/Folder';
import InsertDriveFileIcon from '@mui/icons-material/InsertDriveFile';
import NavigateNextIcon from '@mui/icons-material/NavigateNext';
import api from '../../lib/api';
import type { FileItem } from '../../lib/api';

const TEXT_EXT = ['txt', 'md', 'csv', 'json'];

function extOf(name: string): string {
  const i = name.lastIndexOf('.');
  return i >= 0 ? name.slice(i + 1).toLowerCase() : '';
}

const FilePanel: React.FC = () => {
  const theme = useTheme();
  const [items, setItems] = useState<FileItem[]>([]);
  const [currentPath, setCurrentPath] = useState(''); // 空串=工作区文件根
  const [pathStack, setPathStack] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ path: string; content: string } | null>(null);
  const [previewNote, setPreviewNote] = useState<string | null>(null);

  const loadDir = useCallback(async (dirPath: string) => {
    setLoading(true);
    setError(null);
    setPreview(null);
    setPreviewNote(null);
    try {
      const list = await api.file.list(dirPath);
      setItems(list);
      setCurrentPath(dirPath);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadDir('');
  }, [loadDir]);

  const enterDir = (item: FileItem) => {
    setPathStack((prev) => [...prev, item.name]);
    loadDir(item.path);
  };

  const gotoBreadcrumb = (index: number) => {
    // index=-1 表示根目录
    if (index === -1) {
      setPathStack([]);
      loadDir('');
      return;
    }
    // 逐级回退到目标层级：需要祖先路径；简化处理：从当前条目中无法反推，
    // 因此面包屑仅支持回到根（其余层级提示）
    setPathStack([]);
    loadDir('');
  };

  const openFile = async (item: FileItem) => {
    setError(null);
    setPreviewNote(null);
    if (TEXT_EXT.includes(extOf(item.name))) {
      try {
        const res = await api.file.read(item.path);
        setPreview(res);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
      return;
    }
    // 非文本：走 preview 获取显式提示（转换能力下批落地）
    try {
      const previews = (await api.file.preview([item.path])) as Array<{ note?: string }>;
      setPreview(null);
      setPreviewNote(previews[0]?.note ?? '该格式暂不支持预览');
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <Box>
      <Typography variant="h5" sx={{ fontWeight: 700, marginBottom: theme.spacing(2) }}>
        文件管理
      </Typography>

      {/* 面包屑（当前仅支持回到工作区文件根） */}
      <Breadcrumbs separator={<NavigateNextIcon fontSize="small" />} sx={{ mb: 2 }}>
        <Button size="small" onClick={() => gotoBreadcrumb(-1)} sx={{ textTransform: 'none' }}>
          工作区文件
        </Button>
        {pathStack.map((seg, i) => (
          <Typography key={i} variant="body2" color="text.secondary">
            {seg}
          </Typography>
        ))}
      </Breadcrumbs>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}
      {previewNote && (
        <Alert severity="info" sx={{ mb: 2 }} onClose={() => setPreviewNote(null)}>
          {previewNote}
        </Alert>
      )}

      <Box sx={{ display: 'flex', gap: 2 }}>
        {/* 目录列表 */}
        <Paper
          elevation={0}
          sx={{
            flex: 1,
            border: `1px solid ${theme.palette.divider}`,
            borderRadius: '12px',
            overflow: 'hidden',
          }}
        >
          {loading ? (
            <Typography variant="body2" color="text.secondary" sx={{ p: 3 }}>
              加载中…
            </Typography>
          ) : items.length === 0 ? (
            <Box sx={{ p: 4, textAlign: 'center' }}>
              <Typography variant="body2" color="text.secondary">
                暂无文件。可在 Workbench 通过附件上传，或将文件放入工作区 files 目录。
              </Typography>
            </Box>
          ) : (
            <List dense>
              {items.map((item) => (
                <ListItemButton
                  key={item.path}
                  onClick={() => (item.isDirectory ? enterDir(item) : openFile(item))}
                >
                  <ListItemIcon sx={{ minWidth: 34 }}>
                    {item.isDirectory ? (
                      <FolderIcon color="primary" fontSize="small" />
                    ) : (
                      <InsertDriveFileIcon color="action" fontSize="small" />
                    )}
                  </ListItemIcon>
                  <ListItemText
                    primary={item.name}
                    secondary={item.isDirectory ? undefined : `${(item.size / 1024).toFixed(1)} KB`}
                  />
                </ListItemButton>
              ))}
            </List>
          )}
        </Paper>

        {/* 预览面板 */}
        <Paper
          elevation={0}
          sx={{
            flex: 1.4,
            border: `1px solid ${theme.palette.divider}`,
            borderRadius: '12px',
            p: 2,
            minHeight: 320,
            overflow: 'auto',
          }}
        >
          {preview ? (
            <>
              <Typography variant="subtitle2" sx={{ mb: 1 }}>
                {preview.path}
              </Typography>
              <Box
                component="pre"
                sx={{
                  fontSize: '12px',
                  lineHeight: 1.6,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-all',
                  m: 0,
                }}
              >
                {preview.content}
              </Box>
            </>
          ) : (
            <Typography variant="body2" color="text.secondary">
              选择左侧文件查看预览（支持 txt/md/csv/json；Office/PDF 转换能力下批落地）
            </Typography>
          )}
        </Paper>
      </Box>
    </Box>
  );
};

export default FilePanel;
