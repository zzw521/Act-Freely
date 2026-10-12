/**
 * 会话内查找条（快捷键 Ctrl + F）
 *
 * 在当前会话的对话内容里查找，命中后滚动定位到那一轮并高亮：
 * 匹配口径与 Ctrl+G 全局搜索一致（同一份 task-search 纯逻辑），
 * 没有命中就显示 0 处，不做「无匹配也高亮」的假动作。
 */

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Box, IconButton, InputBase, Typography } from '@mui/material';
import KeyboardArrowUpIcon from '@mui/icons-material/KeyboardArrowUp';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import CloseIcon from '@mui/icons-material/Close';
import type { ChatEvent, Message } from '../../../shared/types';
import { findMatchingTurns, turnTexts } from '../../lib/task-search';

interface TaskFindBarProps {
  messages: Message[];
  turnEvents?: ChatEvent[][];
  onClose: () => void;
  /** 当前定位到的轮次（null 表示无高亮） */
  onActiveTurn: (turn: number | null) => void;
}

const TaskFindBar: React.FC<TaskFindBarProps> = ({ messages, turnEvents, onClose, onActiveTurn }) => {
  const [query, setQuery] = useState('');
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const turns = useMemo(() => turnTexts(messages, turnEvents), [messages, turnEvents]);
  const matches = useMemo(() => findMatchingTurns(turns, query), [turns, query]);

  const jump = (turn: number | null) => {
    onActiveTurn(turn);
    if (turn == null) return;
    const el = document.querySelector(`[data-turn="${turn}"]`);
    el?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  };

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // 查询变化：回到第一个命中；无命中清掉高亮
  useEffect(() => {
    setCursor(0);
    jump(matches.length ? matches[0] : null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [matches.join('|')]);

  // 关闭时清除高亮，避免残留一个指向旧会话的描边
  useEffect(() => () => onActiveTurn(null), [onActiveTurn]);

  const step = (dir: 1 | -1) => {
    if (!matches.length) return;
    const next = (cursor + dir + matches.length) % matches.length;
    setCursor(next);
    jump(matches[next]);
  };

  return (
    <Box
      sx={{
        // 作为消息流上方的独立一行：高度由布局自动让出，不会压住首轮提问，滚动时也不会跟着跑
        alignSelf: 'flex-end',
        mb: 1,
        display: 'flex',
        alignItems: 'center',
        gap: 0.5,
        px: 1,
        py: 0.5,
        borderRadius: '10px',
        border: '1px solid var(--tt-border)',
        bgcolor: 'var(--tt-surface-1, #fff)',
        boxShadow: '0 6px 18px rgba(15, 23, 42, 0.12)',
      }}
    >
      <InputBase
        inputRef={inputRef}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            step(e.shiftKey ? -1 : 1);
          } else if (e.key === 'Escape') {
            e.preventDefault();
            onClose();
          }
        }}
        placeholder="在当前任务中搜索"
        inputProps={{ 'aria-label': '在当前任务中搜索', style: { fontSize: 13, width: 168 } }}
      />
      <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-3)', minWidth: 52, textAlign: 'center' }}>
        {query.trim() ? (matches.length ? `${cursor + 1} / ${matches.length} 处` : '0 处') : `${turns.length} 轮对话`}
      </Typography>
      <IconButton size="small" onClick={() => step(-1)} disabled={!matches.length} sx={{ color: 'var(--tt-text-3)' }}>
        <KeyboardArrowUpIcon sx={{ fontSize: 16 }} />
      </IconButton>
      <IconButton size="small" onClick={() => step(1)} disabled={!matches.length} sx={{ color: 'var(--tt-text-3)' }}>
        <KeyboardArrowDownIcon sx={{ fontSize: 16 }} />
      </IconButton>
      <IconButton size="small" onClick={onClose} sx={{ color: 'var(--tt-text-3)' }}>
        <CloseIcon sx={{ fontSize: 15 }} />
      </IconButton>
    </Box>
  );
};

export default TaskFindBar;
