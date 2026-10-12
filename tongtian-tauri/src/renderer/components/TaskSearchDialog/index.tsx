/**
 * 全局任务搜索面板（快捷键 Ctrl + G）
 *
 * 搜的是 workbenchStore 里真实存在的会话：标题命中优先，正文命中会带上轮次，
 * 回车直接切到该会话。没有匹配就如实说没有，不摆示例数据。
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Box, Dialog, InputBase, Typography, CircularProgress } from '@mui/material';
import SearchIcon from '@mui/icons-material/Search';
import { useWorkbenchStore } from '../../store/workbenchStore';
import { searchConversations, type TaskSearchHit } from '../../lib/task-search';

interface TaskSearchDialogProps {
  open: boolean;
  onClose: () => void;
}

const ROW = 46;

const TaskSearchDialog: React.FC<TaskSearchDialogProps> = ({ open, onClose }) => {
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);

  const convs = useWorkbenchStore((s) => s.convs);
  const convOrder = useWorkbenchStore((s) => s.convOrder);
  const switchConversation = useWorkbenchStore((s) => s.switchConversation);

  const hits = useMemo<TaskSearchHit[]>(
    () => searchConversations(convOrder.map((id) => convs[id]).filter(Boolean), query),
    [convs, convOrder, query],
  );

  useEffect(() => {
    if (open) {
      setQuery('');
      setActive(0);
    }
  }, [open]);

  useEffect(() => setActive(0), [query]);

  const openHit = (hit: TaskSearchHit) => {
    switchConversation(hit.convId);
    navigate('/');
    onClose();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActive((i) => Math.min(i + 1, Math.max(hits.length - 1, 0)));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActive((i) => Math.max(i - 1, 0));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const hit = hits[active];
      if (hit) openHit(hit);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
    }
  };

  useEffect(() => {
    listRef.current?.querySelector(`[data-row="${active}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [active]);

  return (
    <Dialog
      open={open}
      onClose={onClose}
      fullWidth
      maxWidth="sm"
      PaperProps={{ sx: { borderRadius: '14px', overflow: 'hidden' } }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.25, px: 2, py: 1.5, borderBottom: '1px solid var(--tt-border)' }}>
        <SearchIcon sx={{ fontSize: 18, color: 'var(--tt-text-3)' }} />
        <InputBase
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="搜索任务标题或对话内容"
          inputProps={{ 'aria-label': '搜索任务' }}
          sx={{ flex: 1, fontSize: '14px' }}
        />
        <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>↑↓ 选择 · Enter 打开 · Esc 关闭</Typography>
      </Box>

      <Box ref={listRef} sx={{ maxHeight: ROW * 8, overflowY: 'auto' }}>
        {hits.length === 0 ? (
          <Typography sx={{ px: 2, py: 3, fontSize: '13px', color: 'var(--tt-text-3)', textAlign: 'center' }}>
            {query.trim() ? `没有匹配「${query.trim()}」的任务` : '还没有任务，回到工作台提交一个再搜索'}
          </Typography>
        ) : (
          hits.map((hit, i) => (
            <Box
              key={hit.convId}
              data-row={i}
              onClick={() => openHit(hit)}
              sx={{
                display: 'flex',
                alignItems: 'center',
                gap: 1.25,
                px: 2,
                height: ROW,
                cursor: 'pointer',
                bgcolor: i === active ? 'var(--tt-surface-2)' : 'transparent',
                '&:hover': { bgcolor: 'var(--tt-surface-2)' },
              }}
            >
              <Box sx={{ flex: 1, minWidth: 0 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                  <Typography sx={{ fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-1)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {hit.title}
                  </Typography>
                  {hit.running ? (
                    <CircularProgress size={10} sx={{ color: '#22C55E', flexShrink: 0 }} />
                  ) : null}
                </Box>
                <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-3)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {hit.matchedIn === 'content' ? `正文命中 ${hit.turns.length} 处 · ` : ''}
                  {hit.preview}
                </Typography>
              </Box>
              <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)', flexShrink: 0 }}>
                {new Date(hit.updatedAt).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}
              </Typography>
            </Box>
          ))
        )}
      </Box>
    </Dialog>
  );
};

export default TaskSearchDialog;
