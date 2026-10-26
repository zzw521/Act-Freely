/**
 * 通天办公 - 本地知识库（RAG）
 *
 * 事实来源：
 * - 知识库列表/文档/正文抽取全部来自 Rust 对 `{data_home}/knowledge/*` 的真实扫描；
 * - 检索自测直连引擎 `/api/knowledge/search`，与 Agent 的 `search_knowledge` 共用同一份索引；
 * - 浏览器直连通道没有本机目录写权限，页面如实说明并禁用写操作，不用演示数据撑场面。
 *
 * 数据保护：
 * - 删除知识库是软删除（目录与文档保留）；
 * - 移除文档移入库内 `.trash/`，不做物理删除。
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  IconButton,
  Switch,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import MenuBookIcon from '@mui/icons-material/MenuBook';
import AddIcon from '@mui/icons-material/Add';
import UploadFileIcon from '@mui/icons-material/UploadFile';
import FolderOpenIcon from '@mui/icons-material/FolderOpen';
import EditOutlinedIcon from '@mui/icons-material/EditOutlined';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import RefreshIcon from '@mui/icons-material/Refresh';
import SearchIcon from '@mui/icons-material/Search';
import DescriptionOutlinedIcon from '@mui/icons-material/DescriptionOutlined';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import type {
  KnowledgeAddResult,
  KnowledgeBase,
  KnowledgeSearchResult,
} from '../../../shared/contracts';
import { errorTextOf } from '../../../shared/error-messages';
import { knowledgeAPI } from '../../lib/api';
import { isTauri } from '../../lib/tauri-api';
import { useAppStore } from '../../stores/useAppStore';

const DOC_FILTERS = [
  { name: '文档', extensions: ['md', 'txt', 'csv', 'json', 'docx', 'xlsx', 'pptx', 'pdf'] },
];

function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTime(iso: string): string {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString('zh-CN', { hour12: false });
}

const KnowledgePage: React.FC = () => {
  const desktop = isTauri();
  const showNotification = useAppStore((s) => s.showNotification);

  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [selectedId, setSelectedId] = useState<string>('');
  const [loading, setLoading] = useState(desktop);
  const [loadError, setLoadError] = useState('');
  const [busyId, setBusyId] = useState<string>('');

  // 新建 / 编辑
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<KnowledgeBase | null>(null);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [saving, setSaving] = useState(false);

  // 删除知识库 / 移除文档确认
  const [deleteTarget, setDeleteTarget] = useState<KnowledgeBase | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [docToRemove, setDocToRemove] = useState<string | null>(null);
  const [removingDoc, setRemovingDoc] = useState(false);

  // 最近一次添加文档的逐文件结果（部分失败也要摊开）
  const [lastAdd, setLastAdd] = useState<KnowledgeAddResult | null>(null);

  // 检索自测
  const [query, setQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [searchResult, setSearchResult] = useState<KnowledgeSearchResult | null>(null);
  const [searchError, setSearchError] = useState('');

  const selected = useMemo(
    () => bases.find((b) => b.id === selectedId) ?? bases[0] ?? null,
    [bases, selectedId],
  );

  const replaceBase = useCallback((next: KnowledgeBase) => {
    setBases((prev) => prev.map((b) => (b.id === next.id ? next : b)));
  }, []);

  const refresh = useCallback(async () => {
    if (!desktop) {
      setLoading(false);
      setBases([]);
      return;
    }
    setLoading(true);
    try {
      const list = await knowledgeAPI.list();
      const safe = Array.isArray(list) ? list : [];
      setBases(safe);
      setLoadError('');
      setSelectedId((prev) => (safe.some((b) => b.id === prev) ? prev : safe[0]?.id ?? ''));
    } catch (err) {
      setBases([]);
      setLoadError(errorTextOf(err, '知识库列表读取失败'));
    } finally {
      setLoading(false);
    }
  }, [desktop]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 切换知识库时清空上一份检索结果，避免看起来像新库命中了旧内容
  useEffect(() => {
    setSearchResult(null);
    setSearchError('');
    setLastAdd(null);
  }, [selectedId]);

  const openCreate = () => {
    setEditing(null);
    setName('');
    setDescription('');
    setEditorOpen(true);
  };

  const openEdit = (kb: KnowledgeBase) => {
    setEditing(kb);
    setName(kb.name);
    setDescription(kb.description);
    setEditorOpen(true);
  };

  const submitEditor = async () => {
    if (saving) return;
    const trimmed = name.trim();
    if (!trimmed) {
      showNotification('知识库名称不能为空', 'warning');
      return;
    }
    setSaving(true);
    try {
      if (editing) {
        const next = await knowledgeAPI.update(editing.id, {
          name: trimmed,
          description: description.trim(),
        });
        replaceBase(next);
        showNotification(`知识库「${next.name}」已更新`, 'success');
      } else {
        const created = await knowledgeAPI.create(trimmed, description.trim());
        setBases((prev) => [...prev, created]);
        setSelectedId(created.id);
        showNotification(`知识库「${created.name}」已创建`, 'success');
      }
      setEditorOpen(false);
    } catch (err) {
      showNotification(errorTextOf(err, '知识库保存失败'), 'error');
    } finally {
      setSaving(false);
    }
  };

  const toggleEnabled = async (kb: KnowledgeBase, enabled: boolean) => {
    setBusyId(kb.id);
    try {
      const next = await knowledgeAPI.update(kb.id, { enabled });
      replaceBase(next);
      showNotification(
        enabled ? `「${next.name}」已启用，将进入 AI 检索面` : `「${next.name}」已停用，不再参与检索`,
        'success',
      );
    } catch (err) {
      showNotification(errorTextOf(err, '知识库状态更新失败'), 'error');
    } finally {
      setBusyId('');
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    try {
      await knowledgeAPI.remove(deleteTarget.id);
      setBases((prev) => prev.filter((b) => b.id !== deleteTarget.id));
      setSelectedId((prev) => (prev === deleteTarget.id ? '' : prev));
      showNotification(`知识库「${deleteTarget.name}」已删除（目录与文档保留在本机）`, 'success');
      setDeleteTarget(null);
    } catch (err) {
      showNotification(errorTextOf(err, '知识库删除失败'), 'error');
    } finally {
      setDeleting(false);
    }
  };

  const addDocuments = async (kb: KnowledgeBase) => {
    if (!desktop) {
      showNotification('浏览器通道没有本机目录访问能力，请在桌面版添加文档', 'warning');
      return;
    }
    try {
      const { open } = await import('@tauri-apps/plugin-dialog');
      const picked = await open({
        multiple: true,
        directory: false,
        title: `选择要加入「${kb.name}」的文档`,
        filters: DOC_FILTERS,
      });
      const paths = Array.isArray(picked) ? picked : picked ? [picked] : [];
      if (paths.length === 0) return;
      setBusyId(kb.id);
      const result = await knowledgeAPI.addDocs(kb.id, paths);
      replaceBase(result.base);
      setLastAdd(result);
      if (result.added.length > 0) {
        showNotification(`已添加 ${result.added.length} 篇文档${result.errors.length ? `，${result.errors.length} 篇失败` : ''}`, result.errors.length ? 'warning' : 'success');
      } else {
        showNotification(result.errors[0] ?? '没有文档被添加', 'error');
      }
    } catch (err) {
      showNotification(errorTextOf(err, '添加文档失败'), 'error');
    } finally {
      setBusyId('');
    }
  };

  const confirmRemoveDoc = async () => {
    if (!selected || !docToRemove || removingDoc) return;
    setRemovingDoc(true);
    try {
      const next = await knowledgeAPI.removeDoc(selected.id, docToRemove);
      replaceBase(next);
      setLastAdd(null);
      showNotification(`文档「${docToRemove}」已移入回收目录（.trash/）`, 'success');
      setDocToRemove(null);
    } catch (err) {
      showNotification(errorTextOf(err, '移除文档失败'), 'error');
    } finally {
      setRemovingDoc(false);
    }
  };

  const openDirectory = async (kb?: KnowledgeBase) => {
    if (!desktop) {
      showNotification('浏览器通道无法打开本机目录，请在桌面版使用', 'warning');
      return;
    }
    try {
      await knowledgeAPI.openDir(kb?.id);
    } catch (err) {
      showNotification(errorTextOf(err, '打开目录失败'), 'error');
    }
  };

  const runSearch = async () => {
    if (!selected || searching) return;
    const text = query.trim();
    if (!text) {
      setSearchError('请输入检索词');
      return;
    }
    setSearching(true);
    setSearchError('');
    setSearchResult(null);
    try {
      const result = await knowledgeAPI.search(selected, text, 5);
      setSearchResult(result);
    } catch (err) {
      setSearchError(errorTextOf(err, '知识库检索失败'));
    } finally {
      setSearching(false);
    }
  };

  return (
    <Box sx={{ height: 'calc(100vh - 104px)', overflowY: 'auto', pr: 1 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <MenuBookIcon sx={{ fontSize: 22, color: '#6366F1' }} />
        <Typography sx={{ fontSize: '20px', fontWeight: 700, color: 'var(--tt-text-1)' }}>
          知识库
        </Typography>
        {loading && <CircularProgress size={16} sx={{ color: '#6366F1' }} />}
        <Box sx={{ flexGrow: 1 }} />
        <Tooltip title="刷新列表">
          <span>
            <IconButton size="small" onClick={() => void refresh()} disabled={!desktop || loading}>
              <RefreshIcon sx={{ fontSize: 18 }} />
            </IconButton>
          </span>
        </Tooltip>
        <Button
          size="small"
          variant="contained"
          startIcon={<AddIcon />}
          onClick={openCreate}
          disabled={!desktop}
          sx={{ textTransform: 'none', borderRadius: '8px', bgcolor: '#4F46E5', '&:hover': { bgcolor: '#4338CA' } }}
        >
          新建知识库
        </Button>
      </Box>
      <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-3)', mt: 0.5 }}>
        把本机文档交给 AI 检索。启用后的知识库会进入任务执行面，模型通过 search_knowledge 取回带出处的片段，
        片段不够时用 read_knowledge_document 读取整篇（可分段续读）；引用格式为「【知识库 / 文档 #序号】」。
      </Typography>

      {!desktop && (
        <Box
          sx={{
            mt: 1.5,
            p: 1.5,
            borderRadius: '12px',
            border: '1px solid rgba(217, 119, 6, 0.35)',
            bgcolor: 'rgba(217, 119, 6, 0.08)',
            display: 'flex',
            gap: 1,
          }}
        >
          <WarningAmberIcon sx={{ fontSize: 18, color: '#D97706', mt: 0.1 }} />
          <Box>
            <Typography sx={{ fontSize: '13px', fontWeight: 700, color: '#B45309' }}>
              当前是浏览器直连通道，知识库不可用
            </Typography>
            <Typography sx={{ fontSize: '12.5px', color: '#92400E', mt: 0.25 }}>
              知识库需要把文档写入本机运行目录并抽取正文，只有桌面版具备该能力。
              浏览器通道保留本页作为能力说明，不显示示例知识库，也不接受新建或添加。
            </Typography>
          </Box>
        </Box>
      )}

      {desktop && loadError && (
        <Box
          sx={{
            mt: 1.5,
            p: 1.5,
            borderRadius: '12px',
            border: '1px solid rgba(239, 68, 68, 0.3)',
            bgcolor: 'rgba(239, 68, 68, 0.06)',
            display: 'flex',
            gap: 1,
          }}
        >
          <WarningAmberIcon sx={{ fontSize: 18, color: '#DC2626', mt: 0.1 }} />
          <Typography sx={{ fontSize: '12.5px', color: '#B91C1C' }}>{loadError}</Typography>
        </Box>
      )}

      {desktop && (
        <Box
          sx={{
            mt: 2,
            display: 'grid',
            gridTemplateColumns: { xs: '1fr', lg: '320px minmax(0, 1fr)' },
            gap: 2,
            alignItems: 'start',
          }}
        >
          {/* 左栏：知识库列表 */}
          <Box>
            {bases.length === 0 && !loading ? (
              <Box sx={{ border: '1px dashed #E5E7EB', borderRadius: '14px', p: 3, textAlign: 'center' }}>
                <MenuBookIcon sx={{ fontSize: 32, color: '#C7D2FE' }} />
                <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-3)', mt: 1 }}>
                  还没有知识库
                </Typography>
                <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mt: 0.5 }}>
                  新建一个库，再把 md / txt / csv / json / docx / xlsx / pptx / pdf 添加进来
                </Typography>
                <Button
                  size="small"
                  variant="outlined"
                  startIcon={<AddIcon />}
                  onClick={openCreate}
                  sx={{ mt: 1.5, textTransform: 'none', borderRadius: '8px' }}
                >
                  新建知识库
                </Button>
              </Box>
            ) : (
              bases.map((kb) => {
                const active = selected?.id === kb.id;
                return (
                  <Box
                    key={kb.id}
                    onClick={() => setSelectedId(kb.id)}
                    sx={{
                      p: 1.5,
                      mb: 1,
                      borderRadius: '12px',
                      cursor: 'pointer',
                      border: active ? '1px solid rgba(99, 102, 241, 0.55)' : '1px solid var(--tt-border-soft)',
                      bgcolor: active ? 'rgba(99, 102, 241, 0.06)' : 'var(--tt-surface)',
                      transition: 'border-color 0.15s ease, background 0.15s ease',
                      '&:hover': { borderColor: 'rgba(99, 102, 241, 0.4)' },
                    }}
                  >
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                      <Typography
                        sx={{
                          fontSize: '13.5px',
                          fontWeight: 700,
                          color: active ? '#4338CA' : 'var(--tt-text-1)',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                          minWidth: 0,
                          flexGrow: 1,
                        }}
                      >
                        {kb.name}
                      </Typography>
                      <Box
                        component="span"
                        sx={{
                          px: 0.75,
                          py: 0.1,
                          borderRadius: '999px',
                          fontSize: '10.5px',
                          fontWeight: 600,
                          flexShrink: 0,
                          bgcolor: kb.enabled ? 'rgba(16, 185, 129, 0.12)' : 'rgba(107, 114, 128, 0.12)',
                          color: kb.enabled ? '#059669' : '#6B7280',
                        }}
                      >
                        {kb.enabled ? '已启用' : '已停用'}
                      </Box>
                    </Box>
                    {kb.description && (
                      <Typography
                        sx={{
                          fontSize: '12px',
                          color: 'var(--tt-text-3)',
                          mt: 0.4,
                          display: '-webkit-box',
                          WebkitLineClamp: 2,
                          WebkitBoxOrient: 'vertical',
                          overflow: 'hidden',
                        }}
                      >
                        {kb.description}
                      </Typography>
                    )}
                    <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.6 }}>
                      {kb.docs.length} 篇文档
                      {kb.missingText.length > 0 ? ` · ${kb.missingText.length} 篇缺少正文` : ''}
                    </Typography>
                  </Box>
                );
              })
            )}
          </Box>

          {/* 右栏：选中库的文档与检索自测 */}
          <Box>
            {!selected ? (
              <Box sx={{ border: '1px solid var(--tt-border-soft)', borderRadius: '14px', p: 3, bgcolor: 'var(--tt-surface)' }}>
                <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-3)' }}>
                  在左侧选择一个知识库查看文档与检索结果。
                </Typography>
              </Box>
            ) : (
              <>
                <Box sx={{ border: '1px solid var(--tt-border-soft)', borderRadius: '14px', p: 2, bgcolor: 'var(--tt-surface)' }}>
                  <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1 }}>
                    <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                      <Typography sx={{ fontSize: '16px', fontWeight: 700, color: 'var(--tt-text-1)' }}>
                        {selected.name}
                      </Typography>
                      <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mt: 0.25, wordBreak: 'break-all' }}>
                        {selected.dir}
                      </Typography>
                    </Box>
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, flexShrink: 0 }}>
                      <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)' }}>启用</Typography>
                      <Tooltip title={selected.enabled ? '停用后不再进入 AI 检索面' : '启用后进入 AI 检索面'}>
                        <span>
                          <Switch
                            size="small"
                            checked={selected.enabled}
                            disabled={busyId === selected.id}
                            onChange={(e) => void toggleEnabled(selected, e.target.checked)}
                          />
                        </span>
                      </Tooltip>
                    </Box>
                  </Box>

                  {selected.description && (
                    <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-2)', mt: 0.75 }}>
                      {selected.description}
                    </Typography>
                  )}

                  <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75, mt: 1.25 }}>
                    <Button
                      size="small"
                      variant="outlined"
                      startIcon={<UploadFileIcon />}
                      onClick={() => void addDocuments(selected)}
                      disabled={busyId === selected.id}
                      sx={{ textTransform: 'none', borderRadius: '8px' }}
                    >
                      添加文档
                    </Button>
                    <Button
                      size="small"
                      variant="outlined"
                      startIcon={<FolderOpenIcon />}
                      onClick={() => void openDirectory(selected)}
                      sx={{ textTransform: 'none', borderRadius: '8px' }}
                    >
                      打开目录
                    </Button>
                    <Button
                      size="small"
                      variant="outlined"
                      startIcon={<EditOutlinedIcon />}
                      onClick={() => openEdit(selected)}
                      sx={{ textTransform: 'none', borderRadius: '8px' }}
                    >
                      编辑
                    </Button>
                    <Button
                      size="small"
                      variant="outlined"
                      color="error"
                      startIcon={<DeleteOutlineIcon />}
                      onClick={() => setDeleteTarget(selected)}
                      sx={{ textTransform: 'none', borderRadius: '8px' }}
                    >
                      删除
                    </Button>
                    {busyId === selected.id && <CircularProgress size={16} sx={{ alignSelf: 'center' }} />}
                  </Box>

                  <Box sx={{ display: 'flex', gap: 1, mt: 1.5 }}>
                    {[
                      { label: '文档总数', value: selected.docs.length, color: '#4F46E5' },
                      {
                        label: '已抽取正文',
                        value: selected.docs.filter((d) => d.hasText).length,
                        color: '#059669',
                      },
                      { label: '缺少正文', value: selected.missingText.length, color: '#D97706' },
                    ].map((stat) => (
                      <Box
                        key={stat.label}
                        sx={{ flex: 1, px: 1.25, py: 0.9, borderRadius: '10px', bgcolor: 'rgba(99, 102, 241, 0.04)' }}
                      >
                        <Typography sx={{ fontSize: '18px', fontWeight: 700, color: stat.color, lineHeight: 1.2 }}>
                          {stat.value}
                        </Typography>
                        <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>{stat.label}</Typography>
                      </Box>
                    ))}
                  </Box>

                  {selected.missingText.length > 0 && (
                    <Box
                      sx={{
                        mt: 1.25,
                        p: 1,
                        borderRadius: '8px',
                        bgcolor: 'rgba(217, 119, 6, 0.08)',
                        border: '1px solid rgba(217, 119, 6, 0.25)',
                      }}
                    >
                      <Typography sx={{ fontSize: '12px', color: '#92400E' }}>
                        以下文档没有可检索正文，AI 检索不到：{selected.missingText.join('、')}
                      </Typography>
                    </Box>
                  )}

                  {lastAdd && (lastAdd.added.length > 0 || lastAdd.errors.length > 0) && (
                    <Box
                      sx={{
                        mt: 1.25,
                        p: 1,
                        borderRadius: '8px',
                        bgcolor: lastAdd.errors.length ? 'rgba(217, 119, 6, 0.06)' : 'rgba(16, 185, 129, 0.06)',
                        border: lastAdd.errors.length ? '1px solid rgba(217, 119, 6, 0.25)' : '1px solid rgba(16, 185, 129, 0.25)',
                      }}
                    >
                      <Typography sx={{ fontSize: '12px', fontWeight: 600, color: lastAdd.errors.length ? '#92400E' : '#047857' }}>
                        最近一次添加：成功 {lastAdd.added.length} 篇
                        {lastAdd.errors.length ? `，失败 ${lastAdd.errors.length} 篇` : ''}
                      </Typography>
                      {lastAdd.errors.map((err) => (
                        <Typography key={err} sx={{ fontSize: '11.5px', color: '#92400E', mt: 0.25 }}>
                          {err}
                        </Typography>
                      ))}
                    </Box>
                  )}
                </Box>

                {/* 文档列表 */}
                <Box sx={{ border: '1px solid var(--tt-border-soft)', borderRadius: '14px', p: 2, bgcolor: 'var(--tt-surface)', mt: 1.5 }}>
                  <Typography sx={{ fontSize: '13.5px', fontWeight: 700, color: 'var(--tt-text-1)' }}>
                    文档
                  </Typography>
                  {selected.docs.length === 0 ? (
                    <Box sx={{ py: 2.5, textAlign: 'center' }}>
                      <DescriptionOutlinedIcon sx={{ fontSize: 28, color: '#C7D2FE' }} />
                      <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3)', mt: 0.5 }}>
                        这个知识库还没有文档
                      </Typography>
                      <Button
                        size="small"
                        startIcon={<UploadFileIcon />}
                        onClick={() => void addDocuments(selected)}
                        sx={{ mt: 0.5, textTransform: 'none' }}
                      >
                        添加文档
                      </Button>
                    </Box>
                  ) : (
                    selected.docs.map((doc) => (
                      <Box
                        key={doc.name}
                        sx={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: 1,
                          py: 0.85,
                          borderBottom: '1px solid var(--tt-border-soft)',
                          '&:last-of-type': { borderBottom: 'none' },
                        }}
                      >
                        <DescriptionOutlinedIcon sx={{ fontSize: 17, color: '#818CF8', flexShrink: 0 }} />
                        <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                          <Typography
                            sx={{
                              fontSize: '12.5px',
                              color: 'var(--tt-text-1)',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                            }}
                          >
                            {doc.name}
                          </Typography>
                          <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>
                            {formatSize(doc.size)} · 更新于 {formatTime(doc.updatedAt)}
                          </Typography>
                        </Box>
                        {doc.hasText ? (
                          <Tooltip title="正文已抽取，可被检索">
                            <CheckCircleIcon sx={{ fontSize: 16, color: '#10B981', flexShrink: 0 }} />
                          </Tooltip>
                        ) : (
                          <Tooltip title="缺少正文，AI 检索不到">
                            <WarningAmberIcon sx={{ fontSize: 16, color: '#D97706', flexShrink: 0 }} />
                          </Tooltip>
                        )}
                        <Tooltip title="移除文档（移入 .trash/）">
                          <IconButton
                            size="small"
                            onClick={() => setDocToRemove(doc.name)}
                            sx={{ color: 'var(--tt-text-4)', '&:hover': { color: '#EF4444' } }}
                          >
                            <DeleteOutlineIcon sx={{ fontSize: 16 }} />
                          </IconButton>
                        </Tooltip>
                      </Box>
                    ))
                  )}
                </Box>

                {/* 检索自测 */}
                <Box sx={{ border: '1px solid var(--tt-border-soft)', borderRadius: '14px', p: 2, bgcolor: 'var(--tt-surface)', mt: 1.5 }}>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                    <SearchIcon sx={{ fontSize: 18, color: '#0EA5E9' }} />
                    <Typography sx={{ fontSize: '13.5px', fontWeight: 700, color: 'var(--tt-text-1)' }}>
                      检索自测
                    </Typography>
                  </Box>
                  <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mt: 0.25 }}>
                    与本库在任务里的检索共用同一份索引；需要本机引擎正在运行。索引按文档变更自动重建。
                  </Typography>
                  <Box sx={{ display: 'flex', gap: 1, mt: 1 }}>
                    <TextField
                      size="small"
                      fullWidth
                      value={query}
                      onChange={(e) => setQuery(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') void runSearch();
                      }}
                      placeholder="输入问题或关键词，例如：差旅住宿标准"
                      sx={{ '& .MuiInputBase-root': { fontSize: '12.5px' } }}
                    />
                    <Button
                      size="small"
                      variant="contained"
                      onClick={() => void runSearch()}
                      disabled={searching}
                      sx={{ textTransform: 'none', borderRadius: '8px', bgcolor: '#0284C7', '&:hover': { bgcolor: '#0369A1' }, flexShrink: 0 }}
                    >
                      {searching ? '检索中…' : '检索'}
                    </Button>
                  </Box>

                  {searchError && (
                    <Box sx={{ mt: 1, p: 1, borderRadius: '8px', bgcolor: 'rgba(239, 68, 68, 0.06)', border: '1px solid rgba(239, 68, 68, 0.25)' }}>
                      <Typography sx={{ fontSize: '12px', color: '#B91C1C' }}>{searchError}</Typography>
                    </Box>
                  )}

                  {searchResult && (
                    <Box sx={{ mt: 1 }}>
                      {searchResult.results.length > 0 ? (
                        searchResult.results.map((hit, idx) => (
                          <Box
                            key={`${hit.baseId}-${hit.doc}-${hit.seq}-${idx}`}
                            sx={{
                              p: 1.25,
                              mt: 0.75,
                              borderRadius: '10px',
                              border: '1px solid var(--tt-border-soft)',
                              bgcolor: 'rgba(14, 165, 233, 0.03)',
                            }}
                          >
                            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                              <Box
                                component="span"
                                sx={{
                                  px: 0.75,
                                  py: 0.1,
                                  borderRadius: '6px',
                                  bgcolor: 'rgba(14, 165, 233, 0.12)',
                                  color: '#0369A1',
                                  fontSize: '11px',
                                  fontWeight: 600,
                                }}
                              >
                                {hit.citation}
                              </Box>
                              <Typography sx={{ fontSize: '10.5px', color: 'var(--tt-text-4)' }}>
                                相关度 {hit.score}
                              </Typography>
                            </Box>
                            <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-2)', mt: 0.5, whiteSpace: 'pre-wrap' }}>
                              {hit.text}
                            </Typography>
                          </Box>
                        ))
                      ) : (
                        <Box sx={{ mt: 0.75, p: 1, borderRadius: '8px', bgcolor: 'rgba(107, 114, 128, 0.06)' }}>
                          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)' }}>
                            没有命中内容。换一个关键词，或确认文档是否已抽取正文。
                          </Typography>
                        </Box>
                      )}
                      {(searchResult.notes ?? []).map((note) => (
                        <Box key={note} sx={{ display: 'flex', gap: 0.5, mt: 0.75 }}>
                          <InfoOutlinedIcon sx={{ fontSize: 14, color: 'var(--tt-text-4)', mt: 0.1 }} />
                          <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>{note}</Typography>
                        </Box>
                      ))}
                    </Box>
                  )}
                </Box>
              </>
            )}
          </Box>
        </Box>
      )}

      {/* 新建 / 编辑知识库 */}
      <Dialog open={editorOpen} onClose={() => !saving && setEditorOpen(false)} PaperProps={{ sx: { borderRadius: '14px', minWidth: 420 } }}>
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>
          {editing ? '编辑知识库' : '新建知识库'}
        </DialogTitle>
        <DialogContent>
          <TextField
            autoFocus
            fullWidth
            size="small"
            label="名称"
            value={name}
            onChange={(e) => setName(e.target.value.slice(0, 60))}
            placeholder="例如：售后制度、产品手册"
            helperText={`${name.length}/60`}
            sx={{ mt: 0.5, '& .MuiInputBase-root': { fontSize: '13px' } }}
          />
          <TextField
            fullWidth
            multiline
            minRows={3}
            maxRows={6}
            size="small"
            label="描述（可选）"
            value={description}
            onChange={(e) => setDescription(e.target.value.slice(0, 500))}
            placeholder="这个库收录什么资料、适合回答哪类问题"
            helperText={`${description.length}/500`}
            sx={{ mt: 1.5, '& .MuiInputBase-root': { fontSize: '13px' } }}
          />
          <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 1 }}>
            支持 md / txt / csv / json / docx / xlsx / pptx / pdf，单篇不超过 32MB，单库最多 500 篇。
          </Typography>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setEditorOpen(false)} disabled={saving} sx={{ color: 'var(--tt-text-3)', fontSize: '13px' }}>
            取消
          </Button>
          <Button
            onClick={() => void submitEditor()}
            disabled={saving || !name.trim()}
            variant="contained"
            sx={{ fontSize: '13px', bgcolor: '#4F46E5', '&:hover': { bgcolor: '#4338CA' } }}
          >
            {saving ? '保存中…' : editing ? '保存' : '创建'}
          </Button>
        </DialogActions>
      </Dialog>

      {/* 删除知识库（软删除） */}
      <Dialog open={Boolean(deleteTarget)} onClose={() => !deleting && setDeleteTarget(null)} PaperProps={{ sx: { borderRadius: '14px', minWidth: 380 } }}>
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>删除知识库</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ fontSize: '13px', color: 'var(--tt-text-2)' }}>
            删除「{deleteTarget?.name}」后，它不再出现在列表，也不会进入 AI 检索面。
            知识库目录与全部文档会保留在本机，可人工找回；不会物理删除文件。
          </DialogContentText>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setDeleteTarget(null)} disabled={deleting} sx={{ color: 'var(--tt-text-3)', fontSize: '13px' }}>
            取消
          </Button>
          <Button
            onClick={() => void confirmDelete()}
            disabled={deleting}
            variant="contained"
            sx={{ fontSize: '13px', bgcolor: '#EF4444', '&:hover': { bgcolor: '#DC2626' } }}
          >
            {deleting ? '删除中…' : '确认删除'}
          </Button>
        </DialogActions>
      </Dialog>

      {/* 移除文档（移入 .trash/） */}
      <Dialog open={Boolean(docToRemove)} onClose={() => !removingDoc && setDocToRemove(null)} PaperProps={{ sx: { borderRadius: '14px', minWidth: 360 } }}>
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>移除文档</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ fontSize: '13px', color: 'var(--tt-text-2)' }}>
            将「{docToRemove}」移入本库的 .trash/ 目录并删除对应索引，AI 随即检索不到它。
            文件不会被物理删除，需要时可从目录中找回。
          </DialogContentText>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setDocToRemove(null)} disabled={removingDoc} sx={{ color: 'var(--tt-text-3)', fontSize: '13px' }}>
            取消
          </Button>
          <Button
            onClick={() => void confirmRemoveDoc()}
            disabled={removingDoc}
            variant="contained"
            sx={{ fontSize: '13px', bgcolor: '#EF4444', '&:hover': { bgcolor: '#DC2626' } }}
          >
            {removingDoc ? '移除中…' : '确认移除'}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

export default KnowledgePage;
