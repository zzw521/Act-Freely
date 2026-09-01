/**
 * 通天办公 - 拖拽调宽手柄（面板边界长按拖动）
 *
 * 用于左侧导航栏右边界与右侧任务监控左边界，
 * 鼠标长按左右拖动调整面板宽度（受 min/max 约束）。
 */

import React, { useCallback } from 'react';
import { Box } from '@mui/material';

interface ResizeHandleProps {
  width: number;
  min: number;
  max: number;
  /** handle 所在面板：left=左侧面板右边界（向右拖加宽）；right=右侧面板左边界（向左拖加宽） */
  side: 'left' | 'right';
  onWidthChange: (width: number) => void;
  /** 拖拽结束回调（可选） */
  onResizeEnd?: () => void;
}

const ResizeHandle: React.FC<ResizeHandleProps> = ({ width, min, max, side, onWidthChange, onResizeEnd }) => {
  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      const startX = e.clientX;
      const startWidth = width;

      const onMove = (ev: MouseEvent) => {
        const delta = ev.clientX - startX;
        // 左侧面板向右拖加宽；右侧面板向左拖加宽
        const next = side === 'left' ? startWidth + delta : startWidth - delta;
        onWidthChange(Math.max(min, Math.min(max, next)));
      };
      const onUp = () => {
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        onResizeEnd?.();
      };

      document.body.style.cursor = 'col-resize';
      document.body.style.userSelect = 'none';
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
    },
    [width, min, max, side, onWidthChange, onResizeEnd],
  );

  return (
    <Box
      onMouseDown={handleMouseDown}
      sx={{
        position: 'absolute',
        top: 0,
        bottom: 0,
        ...(side === 'left' ? { right: -3 } : { left: -3 }),
        width: 6,
        cursor: 'col-resize',
        zIndex: 20,
        display: 'flex',
        alignItems: 'stretch',
        justifyContent: 'center',
        '&::before': {
          content: '""',
          width: 2,
          background: 'transparent',
          transition: 'background 0.15s ease',
        },
        '&:hover::before, &:active::before': {
          background: 'rgba(99, 102, 241, 0.45)',
        },
      }}
    />
  );
};

export default ResizeHandle;
