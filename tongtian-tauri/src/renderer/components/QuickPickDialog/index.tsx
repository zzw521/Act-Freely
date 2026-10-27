/**
 * QuickPick 应用内快速任务窗（Alt+Space / 双击 Ctrl）
 *
 * 真实提交：输入目标后直接走 workbenchStore.submitGoal，与工作台输入框同一条链路
 * （合规闸门、模型选择、会话正文落库都一致），提交后跳回工作台查看执行过程。
 *
 * 边界：这是应用内快捷键，只有通天办公窗口位于前台时才会触发；
 * 系统级全局热键需要额外的操作系统注册，当前版本未提供，设置页如实说明。
 */

import React, { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Box, CircularProgress, Dialog, InputBase, Typography } from '@mui/material';
import BoltIcon from '@mui/icons-material/Bolt';
import { useAppStore } from '../../stores/useAppStore';
import { useWorkbenchStore } from '../../store/workbenchStore';
import { useI18n } from '../../i18n';

const QuickPickDialog: React.FC = () => {
  const { t: tr } = useI18n();
  const navigate = useNavigate();
  const open = useAppStore((s) => s.quickPickOpen);
  const setOpen = useAppStore((s) => s.setQuickPickOpen);
  const [text, setText] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // 每次打开都是全新的一次快速录入，不残留上一次未提交的草稿
  useEffect(() => {
    if (!open) return;
    setText('');
    setSubmitting(false);
  }, [open]);

  useEffect(() => {
    if (open) {
      const timer = window.setTimeout(() => inputRef.current?.focus(), 30);
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [open]);

  const close = () => {
    if (submitting) return;
    setOpen(false);
  };

  const submit = async () => {
    const goal = text.trim();
    if (!goal || submitting) return;
    setSubmitting(true);
    // submitGoal 内部已把失败写进会话 error（回工作台即可看到原因），这里只负责跳转
    await useWorkbenchStore.getState().submitGoal(goal);
    setSubmitting(false);
    setOpen(false);
    navigate('/');
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // 中文输入法组词中的回车用于选词，不能当提交
    const composing = (e.nativeEvent as KeyboardEvent).isComposing;
    if (e.key === 'Enter' && !e.shiftKey && !composing) {
      e.preventDefault();
      void submit();
    }
  };

  return (
    <Dialog
      open={open}
      onClose={close}
      fullWidth
      maxWidth="sm"
      PaperProps={{
        sx: {
          borderRadius: '16px',
          overflow: 'hidden',
          border: '1px solid var(--tt-border)',
          background: 'var(--tt-surface)',
        },
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, px: 2, pt: 1.75 }}>
        <Box
          sx={{
            width: 26,
            height: 26,
            borderRadius: '8px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            background: 'linear-gradient(135deg, #6366F1, #22D3EE)',
            flexShrink: 0,
          }}
        >
          <BoltIcon sx={{ fontSize: 16, color: '#fff' }} />
        </Box>
        <Typography sx={{ fontSize: '13px', fontWeight: 700, color: 'var(--tt-text-1)', flexGrow: 1 }}>
          {tr('快速任务')}
        </Typography>
        <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>
          {tr('Enter 提交 · Shift+Enter 换行 · Esc 关闭')}
        </Typography>
      </Box>

      <Box sx={{ px: 2, pt: 1.25, pb: 1.5 }}>
        <Box
          sx={{
            borderRadius: '12px',
            border: '1px solid var(--tt-border)',
            bgcolor: 'var(--tt-surface-2)',
            px: 1.5,
            py: 1.25,
            '&:focus-within': { borderColor: '#6366F1' },
          }}
        >
          <InputBase
            inputRef={inputRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={tr('描述要完成的任务，直接回车提交')}
            multiline
            minRows={3}
            maxRows={8}
            fullWidth
            inputProps={{ 'aria-label': tr('快速任务目标') }}
            sx={{ fontSize: '14px', lineHeight: 1.6, alignItems: 'flex-start' }}
          />
        </Box>

        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 1.25 }}>
          <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', flexGrow: 1, lineHeight: 1.5 }}>
            {tr('应用内快捷键：仅当任我行窗口在前台时生效，系统级全局热键尚未提供')}
          </Typography>
          <Box
            component="button"
            type="button"
            onClick={() => void submit()}
            disabled={!text.trim() || submitting}
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 0.75,
              px: 2,
              py: 0.75,
              borderRadius: '10px',
              border: 'none',
              cursor: !text.trim() || submitting ? 'not-allowed' : 'pointer',
              opacity: !text.trim() || submitting ? 0.55 : 1,
              color: '#fff',
              fontSize: '12.5px',
              fontWeight: 600,
              fontFamily: 'inherit',
              background: 'linear-gradient(135deg, #4F46E5, #06B6D4)',
            }}
          >
            {submitting ? <CircularProgress size={12} sx={{ color: '#fff' }} /> : null}
            {submitting ? tr('提交中…') : tr('提交任务')}
          </Box>
        </Box>
      </Box>
    </Dialog>
  );
};

export default QuickPickDialog;
