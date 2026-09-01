/**
 * 通天办公 - 任务监控面板（设计图对齐版 · 右侧栏）
 *
 * 折叠分区：待办（计划步骤 + 审阅操作）/ 产物 / 默认工作目录 / 技能 / MCP 连接器
 * 数据源：workbenchStore 的 plan / fileOps；技能与连接器计数为占位（P1 接入）
 */

import React, { useEffect, useState } from 'react';
import { Box, Button, Chip, Collapse, IconButton, Tooltip, Typography } from '@mui/material';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import PendingIcon from '@mui/icons-material/Pending';
import CancelIcon from '@mui/icons-material/Cancel';
import FolderOutlinedIcon from '@mui/icons-material/FolderOutlined';
import BoltIcon from '@mui/icons-material/Bolt';
import LinkIcon from '@mui/icons-material/Link';
import DescriptionOutlinedIcon from '@mui/icons-material/DescriptionOutlined';
import OpenInFullIcon from '@mui/icons-material/OpenInFull';
import CloseFullscreenIcon from '@mui/icons-material/CloseFullscreen';
import ViewSidebarIcon from '@mui/icons-material/ViewSidebar';
import type { Plan, FileOp } from '../../../shared/types';
import { PlanStatus, StepStatus } from '../../../shared/types';
import ResizeHandle from '../Layout/ResizeHandle';
import FilePreview from '../FilePreview';

/** 面板宽度常量 */
const MONITOR_WIDTH_NORMAL = 300;
const MONITOR_WIDTH_WIDE = 420;

interface TaskMonitorProps {
  plan: Plan | null;
  fileOps: FileOp[];
  workDir?: string;
  skillCount?: number;
  mcpCount?: number;
  onReview?: (action: 'approve' | 'reject' | 'modify') => void;
  /** 隐藏面板（由工作台提供重新展开入口） */
  onHide?: () => void;
}

/** 折叠分区头（defaultOpen 后续变真时自动展开，如计划步骤到达） */
const Section: React.FC<{
  icon: React.ReactNode;
  title: string;
  badge?: number;
  defaultOpen?: boolean;
  children: React.ReactNode;
}> = ({ icon, title, badge, defaultOpen = false, children }) => {
  const [open, setOpen] = useState(defaultOpen);
  useEffect(() => {
    if (defaultOpen) setOpen(true);
  }, [defaultOpen]);
  return (
    <Box sx={{ borderBottom: '1px solid #F3F4F6' }}>
      <Box
        onClick={() => setOpen(!open)}
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 1,
          px: 2,
          py: 1.25,
          cursor: 'pointer',
          '&:hover': { background: 'rgba(15, 23, 42, 0.02)' },
        }}
      >
        {icon}
        <Typography sx={{ fontSize: '13px', fontWeight: 500, color: 'var(--tt-text-2)', flexGrow: 1 }}>
          {title}
        </Typography>
        {badge != null && (
          <Box
            sx={{
              minWidth: 20,
              height: 20,
              borderRadius: '10px',
              background: 'var(--tt-surface-3)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              px: 0.75,
            }}
          >
            <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-3)', fontWeight: 600 }}>{badge}</Typography>
          </Box>
        )}
        <KeyboardArrowDownIcon
          sx={{
            fontSize: 16,
            color: 'var(--tt-text-4)',
            transform: open ? 'rotate(180deg)' : 'none',
            transition: 'transform 0.2s ease',
          }}
        />
      </Box>
      <Collapse in={open}>
        <Box sx={{ px: 2, pb: 1.5 }}>{children}</Box>
      </Collapse>
    </Box>
  );
};

/** 步骤状态图标 */
const stepIcon = (status: StepStatus) => {
  switch (status) {
    case StepStatus.SUCCESS:
      return <CheckCircleIcon sx={{ fontSize: 15, color: '#10B981' }} />;
    case StepStatus.FAILED:
      return <CancelIcon sx={{ fontSize: 15, color: '#EF4444' }} />;
    case StepStatus.RUNNING:
      return <PendingIcon sx={{ fontSize: 15, color: '#F59E0B' }} />;
    default:
      return <PendingIcon sx={{ fontSize: 15, color: '#D1D5DB' }} />;
  }
};

const TaskMonitor: React.FC<TaskMonitorProps> = ({
  plan,
  fileOps,
  workDir,
  skillCount = 3,
  mcpCount = 2,
  onReview,
  onHide,
}) => {
  const steps = plan?.steps ?? [];
  const doneCount = steps.filter((s) => s.status === StepStatus.SUCCESS).length;
  const awaitingReview = plan?.status === PlanStatus.PENDING_REVIEW && !plan.reviewed;
  // 面板宽度：放大/还原快捷切换 + 左边界长按拖拽调宽（260~560）
  const [width, setWidth] = useState(MONITOR_WIDTH_NORMAL);
  const isWide = width >= MONITOR_WIDTH_WIDE;
  // 产物文件预览
  const [previewPath, setPreviewPath] = useState<string | null>(null);

  return (
    <Box
      sx={{
        position: 'relative',
        width,
        flexShrink: 0,
        borderLeft: '1px solid var(--tt-border-soft)',
        background: 'var(--tt-surface)',
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        overflow: 'hidden',
      }}
    >
      {/* 左边界拖拽手柄 */}
      <ResizeHandle side="right" width={width} min={260} max={560} onWidthChange={setWidth} />
      {/* 面板头：标题 + 放大/还原 + 隐藏 */}
      <Box sx={{ display: 'flex', alignItems: 'center', px: 2, py: 1.5 }}>
        <Typography sx={{ fontSize: '14px', fontWeight: 600, color: 'var(--tt-text-1)', flexGrow: 1 }}>
          任务监控
        </Typography>
        <Tooltip title={isWide ? '还原面板宽度' : '放大面板宽度'}>
          <IconButton
            size="small"
            sx={{ color: '#9CA3AF' }}
            onClick={() => setWidth(isWide ? MONITOR_WIDTH_NORMAL : MONITOR_WIDTH_WIDE)}
          >
            {isWide ? <CloseFullscreenIcon sx={{ fontSize: 16 }} /> : <OpenInFullIcon sx={{ fontSize: 15 }} />}
          </IconButton>
        </Tooltip>
        <Tooltip title="隐藏任务监控">
          <IconButton size="small" sx={{ color: '#9CA3AF' }} onClick={() => onHide?.()}>
            <ViewSidebarIcon sx={{ fontSize: 17 }} />
          </IconButton>
        </Tooltip>
      </Box>

      <Box sx={{ flex: 1, overflowY: 'auto' }}>
        {/* 待办（计划步骤） */}
        <Section
          icon={<FolderOutlinedIcon sx={{ fontSize: 16, color: 'var(--tt-text-3)' }} />}
          title="待办"
          badge={steps.length > 0 ? steps.length : undefined}
          defaultOpen={steps.length > 0}
        >
          {steps.length === 0 ? (
            <Typography sx={{ fontSize: '12px', color: '#9CA3AF' }}>暂无计划步骤</Typography>
          ) : (
            <>
              <Typography sx={{ fontSize: '11px', color: '#9CA3AF', mb: 0.75 }}>
                已完成 {doneCount}/{steps.length}
              </Typography>
              {steps.map((s) => (
                <Box key={s.id} sx={{ display: 'flex', alignItems: 'flex-start', gap: 0.75, py: 0.5 }}>
                  {stepIcon(s.status)}
                  <Box sx={{ minWidth: 0 }}>
                    <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-2)', lineHeight: 1.4 }}>
                      {s.name}
                    </Typography>
                    {s.result && (
                      <Typography
                        sx={{
                          fontSize: '11px',
                          color: 'var(--tt-text-4)',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                          maxWidth: 220,
                        }}
                      >
                        {s.result}
                      </Typography>
                    )}
                  </Box>
                </Box>
              ))}
              {/* 两阶段协议审阅操作（pending_review 时显示） */}
              {awaitingReview && onReview && (
                <Box sx={{ display: 'flex', gap: 1, mt: 1 }}>
                  <Button
                    size="small"
                    variant="contained"
                    color="success"
                    onClick={() => onReview('approve')}
                    sx={{ fontSize: '12px', borderRadius: '8px' }}
                  >
                    批准并执行
                  </Button>
                  <Button
                    size="small"
                    variant="outlined"
                    color="error"
                    onClick={() => onReview('reject')}
                    sx={{ fontSize: '12px', borderRadius: '8px' }}
                  >
                    拒绝
                  </Button>
                </Box>
              )}
            </>
          )}
        </Section>

        {/* 产物 */}
        <Section
          icon={<DescriptionOutlinedIcon sx={{ fontSize: 16, color: 'var(--tt-text-3)' }} />}
          title="产物"
          badge={fileOps.length > 0 ? fileOps.length : undefined}
          defaultOpen={fileOps.length > 0}
        >
          {fileOps.length === 0 ? (
            <Typography sx={{ fontSize: '12px', color: '#9CA3AF' }}>暂无产物</Typography>
          ) : (
            fileOps.map((f) => (
              <Tooltip key={f.id} title="点击打开文件" placement="top">
                <Box
                  onClick={() => setPreviewPath(f.srcPath)}
                  sx={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 0.75,
                    py: 0.5,
                    cursor: 'pointer',
                    borderRadius: '6px',
                    '&:hover': { '& .file-name': { color: '#4F46E5' } },
                  }}
                >
                  <DescriptionOutlinedIcon sx={{ fontSize: 14, color: '#9CA3AF' }} />
                  <Typography className="file-name" sx={{ fontSize: '12px', color: 'var(--tt-text-2)', transition: 'color 0.15s ease' }}>
                    {f.fileName ?? f.srcPath}
                  </Typography>
                </Box>
              </Tooltip>
            ))
          )}
        </Section>

        {/* 默认工作目录 */}
        <Section
          icon={<FolderOutlinedIcon sx={{ fontSize: 16, color: 'var(--tt-text-3)' }} />}
          title="默认工作目录"
        >
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', wordBreak: 'break-all' }}>
            {workDir || '未设置（选择工作目录后生效）'}
          </Typography>
        </Section>

        {/* 技能（占位计数，P1 接 skill_list） */}
        <Section
          icon={<BoltIcon sx={{ fontSize: 16, color: 'var(--tt-text-3)' }} />}
          title="技能"
          badge={skillCount}
        >
          <Typography sx={{ fontSize: '12px', color: '#9CA3AF' }}>
            技能市场即将上线，当前为内置技能
          </Typography>
        </Section>

        {/* MCP 连接器（占位计数，P1 接 mcp_list） */}
        <Section
          icon={<LinkIcon sx={{ fontSize: 16, color: 'var(--tt-text-3)' }} />}
          title="MCP 连接器"
          badge={mcpCount}
        >
          <Typography sx={{ fontSize: '12px', color: '#9CA3AF' }}>
            连接器中心即将上线
          </Typography>
        </Section>
      </Box>

      {/* 产物文件预览弹窗 */}
      <FilePreview path={previewPath} onClose={() => setPreviewPath(null)} />
    </Box>
  );
};

export default TaskMonitor;
