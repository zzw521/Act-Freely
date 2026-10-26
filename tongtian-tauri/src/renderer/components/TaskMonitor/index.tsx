/**
 * 通天办公 - 任务监控面板（设计图对齐版 · 右侧栏）
 *
 * 折叠分区：待办（计划步骤 + 审阅操作）/ 产物 / 默认工作目录 / 技能 / MCP 连接器
 * 数据源：workbenchStore 的 plan / fileOps；技能与连接器取三通道真实台账
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert, Box, Button, Chip, Collapse, IconButton, Snackbar, Tooltip, Typography } from '@mui/material';
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
import RefreshIcon from '@mui/icons-material/Refresh';
import type { Plan, FileOp, WorkMode } from '../../../shared/types';
import { PlanStatus, StepStatus } from '../../../shared/types';
import type { InstalledSkill, McpServer } from '../../../shared/contracts';
import { mcpAPI, skillAPI } from '../../lib/api';
import ResizeHandle from '../Layout/ResizeHandle';
import FilePreview from '../FilePreview';
import { useAppSettings } from '../../lib/use-app-settings';
import { openArtifact } from '../../lib/artifact-open';
import { useI18n } from '../../i18n';

/** 面板宽度常量 */
const MONITOR_WIDTH_NORMAL = 300;
const MONITOR_WIDTH_WIDE = 420;

interface TaskMonitorProps {
  plan: Plan | null;
  fileOps: FileOp[];
  workDir?: string;
  onReview?: (action: 'approve' | 'reject' | 'modify') => void;
  /** 隐藏面板（由工作台提供重新展开入口） */
  onHide?: () => void;
  /** 当前工作台模式：非通用模式下右侧出现「画布」分页 */
  mode?: WorkMode;
  /** 画布内容（由工作台注入，这里只负责分页与面板 chrome） */
  canvas?: React.ReactNode;
  /** 画布分页标题（设计画布 / 幻灯片画布 / 写作画布） */
  canvasLabel?: string;
}

/**
 * 技能与连接器台账。
 * 计数只来自真实清单：读取中/读取失败都不给数字，避免把「未知」显示成 3 和 2。
 */
function useCapabilityLedger() {
  const [skills, setSkills] = useState<InstalledSkill[] | null>(null);
  const [servers, setServers] = useState<McpServer[] | null>(null);
  const [failed, setFailed] = useState<{ skills: boolean; servers: boolean }>({
    skills: false,
    servers: false,
  });

  const reload = useCallback(async () => {
    const [s, m] = await Promise.allSettled([skillAPI.list(), mcpAPI.list()]);
    setSkills(s.status === 'fulfilled' ? s.value : null);
    setServers(m.status === 'fulfilled' ? m.value : null);
    setFailed({ skills: s.status !== 'fulfilled', servers: m.status !== 'fulfilled' });
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { skills, servers, failed, reload };
}

/** 清单区底部：读取中 / 失败重试，都是真实状态而非占位文案 */
const LedgerFooter: React.FC<{
  loading: boolean;
  failed: boolean;
  onRetry: () => void;
  failedText: string;
}> = ({ loading, failed, onRetry, failedText }) => {
  const { t: tr } = useI18n();
  if (failed) {
    return (
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, mt: 0.5 }}>
        <Typography sx={{ fontSize: '12px', color: '#B45309' }}>{failedText}</Typography>
        <Box
          onClick={onRetry}
          sx={{ display: 'flex', alignItems: 'center', gap: 0.25, cursor: 'pointer' }}
        >
          <RefreshIcon sx={{ fontSize: 13, color: '#0D9488' }} />
          <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#0D9488' }}>{tr('重试')}</Typography>
        </Box>
      </Box>
    );
  }
  if (loading) {
    return (
      <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mt: 0.5 }}>{tr('正在读取清单…')}</Typography>
    );
  }
  return null;
};

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
  onReview,
  onHide,
  mode,
  canvas,
  canvasLabel,
}) => {
  const { t: tr } = useI18n();
  const steps = plan?.steps ?? [];
  const doneCount = steps.filter((s) => s.status === StepStatus.SUCCESS).length;
  const awaitingReview = plan?.status === PlanStatus.PENDING_REVIEW && !plan.reviewed;
  const navigate = useNavigate();
  const { skills, servers, failed, reload } = useCapabilityLedger();
  // 面板宽度：放大/还原快捷切换 + 左边界长按拖拽调宽（260~560）
  const hasCanvas = Boolean(canvas);
  // 画布需要更宽的视口才看得清，首次带画布打开时默认用宽版
  const [width, setWidth] = useState(hasCanvas ? MONITOR_WIDTH_WIDE : MONITOR_WIDTH_NORMAL);
  const isWide = width >= MONITOR_WIDTH_WIDE;
  // 「任务 / 画布」分页：非通用模式默认落在画布；待审阅必须回到任务页，批准按钮不能被藏住
  const [tab, setTab] = useState<'task' | 'canvas'>(hasCanvas ? 'canvas' : 'task');
  const prevMode = useRef(mode);
  useEffect(() => {
    if (!hasCanvas) setTab('task');
  }, [hasCanvas]);
  useEffect(() => {
    if (prevMode.current === mode) return;
    prevMode.current = mode;
    if (hasCanvas) {
      setTab('canvas');
      // 用户未手动调宽时，进入画布模式顺手给出宽版
      setWidth((w) => (w === MONITOR_WIDTH_NORMAL ? MONITOR_WIDTH_WIDE : w));
    }
  }, [mode, hasCanvas]);
  useEffect(() => {
    if (awaitingReview) setTab('task');
  }, [awaitingReview]);
  // 产物文件预览（方式与设置页「预览方式」同源）
  const [appSettings] = useAppSettings();
  const previewMode = (appSettings.previewMode as string) ?? '应用内预览';
  const [previewPath, setPreviewPath] = useState<string | null>(null);
  const [openNotice, setOpenNotice] = useState('');

  const handleOpenArtifact = async (path: string) => {
    const result = await openArtifact(path, previewMode);
    if (result.error) setOpenNotice(result.error);
    if (result.previewPath) setPreviewPath(result.previewPath);
  };

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
      <Box sx={{ display: 'flex', alignItems: 'center', px: 2, py: 1.5, pb: hasCanvas ? 0.75 : 1.5 }}>
        <Typography sx={{ fontSize: '14px', fontWeight: 600, color: 'var(--tt-text-1)', flexGrow: 1 }}>
          {tab === 'canvas' && canvasLabel ? tr(canvasLabel) : tr('任务监控')}
        </Typography>
        <Tooltip title={isWide ? tr('还原面板宽度') : tr('放大面板宽度')}>
          <IconButton
            size="small"
            sx={{ color: '#9CA3AF' }}
            onClick={() => setWidth(isWide ? MONITOR_WIDTH_NORMAL : MONITOR_WIDTH_WIDE)}
          >
            {isWide ? <CloseFullscreenIcon sx={{ fontSize: 16 }} /> : <OpenInFullIcon sx={{ fontSize: 15 }} />}
          </IconButton>
        </Tooltip>
        <Tooltip title={tr('隐藏任务监控')}>
          <IconButton size="small" sx={{ color: '#9CA3AF' }} onClick={() => onHide?.()}>
            <ViewSidebarIcon sx={{ fontSize: 17 }} />
          </IconButton>
        </Tooltip>
      </Box>

      {/* 模式分页：通用模式只有任务页，不显示无意义的单选 */}
      {hasCanvas && (
        <Box sx={{ display: 'flex', gap: 0.75, px: 2, pb: 1 }}>
          {([
            { id: 'task' as const, label: tr('任务') },
            { id: 'canvas' as const, label: canvasLabel ? tr(canvasLabel) : tr('画布') },
          ]).map((item) => {
            const active = tab === item.id;
            return (
              <Box
                key={item.id}
                component="button"
                type="button"
                onClick={() => setTab(item.id)}
                aria-pressed={active}
                sx={{
                  flex: 1,
                  py: 0.6,
                  borderRadius: '8px',
                  border: `1px solid ${active ? 'rgba(20, 184, 166, 0.45)' : 'var(--tt-border)'}`,
                  bgcolor: active ? 'rgba(20, 184, 166, 0.08)' : 'transparent',
                  color: active ? '#0D9488' : 'var(--tt-text-3)',
                  fontSize: '12px',
                  fontWeight: active ? 600 : 500,
                  fontFamily: 'inherit',
                  cursor: 'pointer',
                  transition: 'all 0.15s ease',
                  '&:hover': { borderColor: 'rgba(20, 184, 166, 0.45)' },
                }}
              >
                {item.label}
              </Box>
            );
          })}
        </Box>
      )}

      {tab === 'canvas' && canvas ? (
        <Box sx={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
          {canvas}
        </Box>
      ) : (
      <Box sx={{ flex: 1, overflowY: 'auto' }}>
        {/* 待办（计划步骤） */}
        <Section
          icon={<FolderOutlinedIcon sx={{ fontSize: 16, color: 'var(--tt-text-3)' }} />}
          title={tr('待办')}
          badge={steps.length > 0 ? steps.length : undefined}
          defaultOpen={steps.length > 0}
        >
          {steps.length === 0 ? (
            <Typography sx={{ fontSize: '12px', color: '#9CA3AF' }}>{tr('暂无计划步骤')}</Typography>
          ) : (
            <>
              <Typography sx={{ fontSize: '11px', color: '#9CA3AF', mb: 0.75 }}>
                {tr('已完成 {done}/{total}', { done: doneCount, total: steps.length })}
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
                    {tr('批准并执行')}
                  </Button>
                  <Button
                    size="small"
                    variant="outlined"
                    color="error"
                    onClick={() => onReview('reject')}
                    sx={{ fontSize: '12px', borderRadius: '8px' }}
                  >
                    {tr('拒绝')}
                  </Button>
                </Box>
              )}
            </>
          )}
        </Section>

        {/* 产物 */}
        <Section
          icon={<DescriptionOutlinedIcon sx={{ fontSize: 16, color: 'var(--tt-text-3)' }} />}
          title={tr('产物')}
          badge={fileOps.length > 0 ? fileOps.length : undefined}
          defaultOpen={fileOps.length > 0}
        >
          {fileOps.length === 0 ? (
            <Typography sx={{ fontSize: '12px', color: '#9CA3AF' }}>{tr('暂无产物')}</Typography>
          ) : (
            fileOps.map((f) => (
              <Tooltip
                key={f.id}
                title={tr(previewMode === '系统默认程序' ? '点击用系统默认程序打开' : '点击在应用内预览')}
                placement="top"
              >
                <Box
                  onClick={() => void handleOpenArtifact(f.srcPath)}
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
          title={tr('默认工作目录')}
        >
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', wordBreak: 'break-all' }}>
            {workDir || tr('未设置（选择工作目录后生效）')}
          </Typography>
        </Section>

        {/* 技能（真实台账：{data_home}/skills 目录扫描结果） */}
        <Section
          icon={<BoltIcon sx={{ fontSize: 16, color: 'var(--tt-text-3)' }} />}
          title={tr('技能')}
          badge={skills?.length}
        >
          {skills && skills.length === 0 ? (
            <Box
              onClick={() => navigate('/extensions?tab=skills')}
              sx={{ display: 'flex', alignItems: 'center', gap: 0.5, cursor: 'pointer' }}
            >
              <Typography sx={{ fontSize: '12px', color: '#9CA3AF' }}>{tr('尚未安装技能，')}</Typography>
              <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#0D9488' }}>{tr('去技能页安装')}</Typography>
            </Box>
          ) : skills ? (
            <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
              {skills.map((s) => (
                <Tooltip key={s.id} title={s.description ?? s.name}>
                  <Chip
                    size="small"
                    label={s.readable ? s.name : tr('{name}（需重装）', { name: s.name })}
                    onClick={() => navigate('/extensions?tab=skills')}
                    sx={{
                      maxWidth: '100%',
                      fontSize: '11.5px',
                      height: 22,
                      bgcolor: s.enabled ? '#E6F7F4' : '#F3F4F6',
                      color: s.enabled ? '#0D9488' : '#6B7280',
                    }}
                  />
                </Tooltip>
              ))}
            </Box>
          ) : (
            <Typography sx={{ fontSize: '12px', color: '#9CA3AF' }}>{tr('技能清单暂不可用')}</Typography>
          )}
          <LedgerFooter
            loading={!skills && !failed.skills}
            failed={failed.skills}
            onRetry={() => void reload()}
            failedText={tr('技能清单读取失败')}
          />
        </Section>

        {/* MCP 连接器（真实台账：mcp_servers 登记 + 握手状态） */}
        <Section
          icon={<LinkIcon sx={{ fontSize: 16, color: 'var(--tt-text-3)' }} />}
          title={tr('MCP 连接器')}
          badge={servers?.length}
        >
          {servers && servers.length === 0 ? (
            <Box
              onClick={() => navigate('/extensions?tab=connectors')}
              sx={{ display: 'flex', alignItems: 'center', gap: 0.5, cursor: 'pointer' }}
            >
              <Typography sx={{ fontSize: '12px', color: '#9CA3AF' }}>{tr('尚未登记连接器，')}</Typography>
              <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#0D9488' }}>{tr('去连接器页添加')}</Typography>
            </Box>
          ) : servers ? (
            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.25 }}>
              {servers.map((s) => (
                <Box key={s.serverId} sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                  <Typography
                    sx={{ fontSize: '12px', color: 'var(--tt-text-2)', flexGrow: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                  >
                    {s.name}
                  </Typography>
                  <Typography
                    sx={{
                      fontSize: '11px',
                      fontWeight: 600,
                      color:
                        s.status === 'connected'
                          ? '#0D9488'
                          : s.status === 'failed'
                            ? '#B45309'
                            : '#9CA3AF',
                    }}
                  >
                    {s.status === 'connected'
                      ? tr('已连接')
                      : s.status === 'failed'
                        ? tr('连接失败')
                        : tr('未连接')}
                  </Typography>
                </Box>
              ))}
            </Box>
          ) : (
            <Typography sx={{ fontSize: '12px', color: '#9CA3AF' }}>{tr('连接器清单暂不可用')}</Typography>
          )}
          <LedgerFooter
            loading={!servers && !failed.servers}
            failed={failed.servers}
            onRetry={() => void reload()}
            failedText={tr('连接器清单读取失败')}
          />
        </Section>
      </Box>
      )}

      {/* 系统默认程序打开失败提示（已回退应用内预览，不静默） */}
      <Snackbar
        open={Boolean(openNotice)}
        autoHideDuration={4000}
        onClose={() => setOpenNotice('')}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert severity="warning" variant="filled" onClose={() => setOpenNotice('')} sx={{ fontSize: '12.5px' }}>
          {openNotice}
        </Alert>
      </Snackbar>

      {/* 产物文件预览弹窗 */}
      <FilePreview path={previewPath} onClose={() => setPreviewPath(null)} />
    </Box>
  );
};

export default TaskMonitor;
