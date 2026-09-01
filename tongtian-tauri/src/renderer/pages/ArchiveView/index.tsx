/**
 * 通天办公 - 归档页 (对标 QwenWork 已归档)
 *
 * 设计:
 * - 归档任务列表（标题/归档时间/所属工作区）
 * - 行操作: 恢复 / 彻底删除（二次确认）
 * - 右上角「全部删除」（需输入确认词 DELETE）
 */

import React, { useState, useEffect } from 'react';
import {
  Box,
  Typography,
  Button,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  TextField,
} from '@mui/material';
import ArchiveIcon from '@mui/icons-material/Archive';
import RestoreIcon from '@mui/icons-material/Restore';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import { listArchived, unarchiveTask, deleteArchivedTask, clearArchived } from '../../lib/alignment-api';

interface ArchiveRecord {
  taskId: string;
  title: string;
  workspaceName: string;
  archivedAt: string;
}

const ArchiveView: React.FC = () => {
  const [records, setRecords] = useState<ArchiveRecord[]>([]);
  const [deleteTarget, setDeleteTarget] = useState<ArchiveRecord | null>(null);
  const [clearOpen, setClearOpen] = useState(false);
  const [confirmText, setConfirmText] = useState('');

  // 从持久层加载归档列表（真实 IPC）
  useEffect(() => {
    listArchived()
      .then((list) =>
        setRecords(
          list.map((r) => ({
            taskId: r.taskId,
            title: r.title,
            workspaceName: r.workspaceId,
            archivedAt: r.archivedAt,
          })),
        ),
      )
      .catch(() => setRecords([]));
  }, []);

  const handleDelete = () => {
    if (!deleteTarget) return;
    const id = deleteTarget.taskId;
    setRecords((rs) => rs.filter((r) => r.taskId !== id));
    setDeleteTarget(null);
    deleteArchivedTask(id).catch(() => undefined);
  };

  const handleClearAll = () => {
    if (confirmText !== 'DELETE') return;
    clearArchived('DELETE')
      .then((ok) => {
        if (ok) setRecords([]);
      })
      .catch(() => undefined);
    setClearOpen(false);
    setConfirmText('');
  };

  return (
    <Box sx={{ height: 'calc(100vh - 104px)', overflowY: 'auto', pr: 1 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.5 }}>
        <ArchiveIcon sx={{ fontSize: 21, color: '#0D9488' }} />
        <Typography sx={{ fontSize: '20px', fontWeight: 700, color: '#111827' }}>已归档</Typography>
        <Box sx={{ flexGrow: 1 }} />
        {records.length > 0 && (
          <Button
            size="small"
            startIcon={<DeleteOutlineIcon sx={{ fontSize: 15 }} />}
            onClick={() => setClearOpen(true)}
            sx={{ color: '#EF4444', fontSize: '12px' }}
          >
            全部删除
          </Button>
        )}
      </Box>
      <Typography sx={{ fontSize: '13px', color: '#9CA3AF', mb: 2.5 }}>
        归档的任务可在此恢复或彻底删除。彻底删除不可撤销。
      </Typography>

      <Box sx={{ maxWidth: 860 }}>
        {records.length === 0 && (
          <Box sx={{ border: '1px dashed #E5E7EB', borderRadius: '14px', p: 6, textAlign: 'center' }}>
            <Typography sx={{ fontSize: '14px', color: '#6B7280' }}>暂无归档任务</Typography>
          </Box>
        )}
        {records.map((r) => (
          <Box
            key={r.taskId}
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 1.5,
              bgcolor: '#FFFFFF',
              border: '1px solid #E5E7EB',
              borderRadius: '14px',
              px: 2,
              py: 1.5,
              mb: 1,
            }}
          >
            <Box sx={{ flexGrow: 1, minWidth: 0 }}>
              <Typography sx={{ fontSize: '13.5px', fontWeight: 600, color: '#111827' }} noWrap>
                {r.title}
              </Typography>
              <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mt: 0.25 }}>
                {r.workspaceName} · 归档于 {r.archivedAt}
              </Typography>
            </Box>
            <Button
              size="small"
              startIcon={<RestoreIcon sx={{ fontSize: 15 }} />}
              onClick={() => {
                setRecords((rs) => rs.filter((x) => x.taskId !== r.taskId));
                unarchiveTask(r.taskId).catch(() => undefined);
              }}
              sx={{ color: '#0D9488', fontSize: '12px' }}
            >
              恢复
            </Button>
            <Button
              size="small"
              startIcon={<DeleteOutlineIcon sx={{ fontSize: 15 }} />}
              onClick={() => setDeleteTarget(r)}
              sx={{ color: '#EF4444', fontSize: '12px' }}
            >
              删除
            </Button>
          </Box>
        ))}
      </Box>

      {/* 单条删除确认 */}
      <Dialog open={Boolean(deleteTarget)} onClose={() => setDeleteTarget(null)} maxWidth="xs">
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>彻底删除该任务？</DialogTitle>
        <DialogContent>
          <Typography sx={{ fontSize: '13px', color: '#6B7280' }}>
            「{deleteTarget?.title}」将被彻底删除，此操作不可撤销。
          </Typography>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setDeleteTarget(null)} sx={{ color: '#6B7280', fontSize: '13px' }}>
            取消
          </Button>
          <Button onClick={handleDelete} sx={{ color: '#EF4444', fontSize: '13px', fontWeight: 600 }}>
            彻底删除
          </Button>
        </DialogActions>
      </Dialog>

      {/* 全部删除强确认 */}
      <Dialog open={clearOpen} onClose={() => setClearOpen(false)} maxWidth="xs">
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>清空全部归档？</DialogTitle>
        <DialogContent>
          <Typography sx={{ fontSize: '13px', color: '#6B7280', mb: 1.5 }}>
            将彻底删除全部 {records.length} 条归档任务，不可撤销。请输入 DELETE 确认。
          </Typography>
          <TextField
            size="small"
            fullWidth
            placeholder="DELETE"
            value={confirmText}
            onChange={(e) => setConfirmText(e.target.value)}
            sx={{ '& .MuiInputBase-input': { fontSize: '13px' } }}
          />
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button
            onClick={() => {
              setClearOpen(false);
              setConfirmText('');
            }}
            sx={{ color: '#6B7280', fontSize: '13px' }}
          >
            取消
          </Button>
          <Button
            disabled={confirmText !== 'DELETE'}
            onClick={handleClearAll}
            sx={{
              color: confirmText === 'DELETE' ? '#EF4444' : '#D1D5DB',
              fontSize: '13px',
              fontWeight: 600,
            }}
          >
            确认清空
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

export default ArchiveView;
