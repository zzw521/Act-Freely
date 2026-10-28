/**
 * 通天办公 - 右侧任务面板
 *
 * 浮层卡片式布局（对齐 Octop 右栏）：面板头（标题 / 放大 / 关闭）+ 胶囊分页 + 卡片式分区。
 * 折叠分区：待办（计划步骤 + 审阅操作）/ 产物 / 默认工作目录 / 技能 / MCP 连接器
 * 数据源：workbenchStore 的 plan / fileOps；技能与连接器取三通道真实台账
 *
 * 产物点击后在面板内打开预览（不弹模态、不离开任务），可返回任务页或关闭整个面板。
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert, Box, Button, Chip, Collapse, IconButton, Snackbar, Tooltip, Typography } from '@mui/material';
import ArrowBackIcon from '@mui/icons-material/ArrowBack';
import BoltIcon from '@mui/icons-material/Bolt';
import CancelIcon from '@mui/icons-material/Cancel';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import CloseFullscreenIcon from '@mui/icons-material/CloseFullscreen';
import CloseIcon from '@mui/icons-material/Close';
import DescriptionOutlinedIcon from '@mui/icons-material/DescriptionOutlined';
import FolderOutlinedIcon from '@mui/icons-material/FolderOutlined';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import LinkIcon from '@mui/icons-material/Link';
import OpenInFullIcon from '@mui/icons-material/OpenInFull';
import OpenInNewIcon from '@mui/icons-material/OpenInNew';
import PendingIcon from '@mui/icons-material/Pending';
import RefreshIcon from '@mui/icons-material/Refresh';
import type { Plan, FileOp, WorkMode } from '../../../shared/types';
import { PlanStatus, StepStatus } from '../../../shared/types';
import type { InstalledSkill, McpServer } from '../../../shared/contracts';
import { fileAPI, mcpAPI, skillAPI } from '../../lib/api';
import { errorTextOf } from '../../../shared/error-messages';
import ArtifactCard from '../ArtifactCard';
import { FilePreviewBody, useFileContent } from '../FilePreview';
import ResizeHandle from '../Layout/ResizeHandle';
import { useI18n } from '../../i18n';

/** 面板宽度常量（预览需要更宽的视口） */
const MONITOR_WIDTH_NORMAL = 320;
const MONITOR_WIDTH_WIDE = 480;

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
  /** 面板内预览的产物路径：与消息流产物点击共用同一份状态 */
  previewPath: string | null;
  /** 退出面板内预览，回到任务页 */
  onPreviewClose: () => void;
  /** 点击产物：交给工作台按「预览方式」分流后写回 previewPath */
  onArtifactOpen: (path: string) => void;
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

/** 卡片式折叠分区（defaultOpen 后续变真时自动展开，如计划步骤到达） */
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
    <Box
      sx={{
        flexShrink: 0,
        borderRadius: '14px',
        border: '1px solid var(--tt-border-soft)',
        background: 'var(--tt-surface)',
        overflow: 'hidden',
      }}
    >
      <Box
        onClick={() => setOpen(!open)}
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 0.75,
          px: 1.25,
          py: 1,
          cursor: 'pointer',
          '&:hover': { background: 'rgba(15, 23, 42, 0.02)' },
        }}
      >
        {icon}
        <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: 'var(--tt-text-2)', flexGrow: 1 }}>
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
        <Box sx={{ px: 1.25, pb: 1.25 }}>{children}</Box>
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
  previewPath,
  onPreviewClose,
  onArtifactOpen,
}) => {
  const { t: tr } = useI18n();
  const steps = plan?.steps ?? [];
  const doneCount = steps.filter((s) => s.status === StepStatus.SUCCESS).length;
  const awaitingReview = plan?.status === PlanStatus.PENDING_REVIEW && !plan.reviewed;
  const navigate = useNavigate();
  const { skills, servers, failed, reload } = useCapabilityLedger();
  // 面板宽度：放大/还原快捷切换 + 左边界长按拖拽调宽（280~600）
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
  // 产物预览：内容取数与渲染复用 FilePreview，壳换到面板内
  const preview = useFileContent(previewPath);
  // 打开预览时给出宽版，读正文/表格不至于被挤成一条
  useEffect(() => {
    if (previewPath) setWidth((w) => (w < MONITOR_WIDTH_WIDE ? MONITOR_WIDTH_WIDE : w));
  }, [previewPath]);
  // 系统默认程序打开失败提示
  const [openNotice, setOpenNotice] = useState('');
  const handleOpenExternal = async () => {
    if (!previewPath) return;
    try {
      await fileAPI.openExternal(previewPath);
    } catch (e) {
      setOpenNotice(errorTextOf(e, tr('系统默认程序未能打开该文件')));
    }
  };
  const previewName = preview.data?.fileName ?? (previewPath ? previewPath.split(/[/\\]/).filter(Boolean).pop() : '');

  // maxWidth 兜底：最小窗口（960）下不能让面板把对话区挤成一条，
  // 预览态请求的 480 宽在这里被压回，窗口变宽后自动恢复
  return (
    <Box
      sx={{
        position: 'relative',
        width,
        maxWidth: 'max(280px, calc(100% - 380px))',
        flexShrink: 0,
        height: '100%',
        py: 1,
        pr: 1,
      }}
    >
      {/* 左边界拖拽手柄 */}
      <ResizeHandle side="right" width={width} min={280} max={600} onWidthChange={setWidth} />
      <Box
        sx={{
          height: '100%',
          display: 'flex',
          flexDirection: 'column',
          borderRadius: '18px',
          border: '1px solid var(--tt-border-soft)',
          background: 'var(--tt-surface-2)',
          overflow: 'hidden',
        }}
      >
        {/* 面板头：预览态给返回 + 文件名 + 系统打开；常态给标题 + 放大 + 关闭 */}
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, px: 1.5, pt: 1.25, pb: previewPath ? 1.25 : hasCanvas ? 0.75 : 1.25 }}>
          {previewPath ? (
            <>
              <Tooltip title={tr('返回任务')}>
                <IconButton size="small" onClick={onPreviewClose} sx={{ color: 'var(--tt-text-3)' }}>
                  <ArrowBackIcon sx={{ fontSize: 18 }} />
                </IconButton>
              </Tooltip>
              <Typography
                sx={{
                  fontSize: '13.5px',
                  fontWeight: 600,
                  color: 'var(--tt-text-1)',
                  flexGrow: 1,
                  minWidth: 0,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {previewName}
              </Typography>
              <Tooltip title={tr('用系统默认应用打开')}>
                <IconButton size="small" onClick={() => void handleOpenExternal()} sx={{ color: '#9CA3AF' }}>
                  <OpenInNewIcon sx={{ fontSize: 16 }} />
                </IconButton>
              </Tooltip>
              <Tooltip title={tr('关闭面板')}>
                <IconButton size="small" onClick={() => onHide?.()} sx={{ color: '#9CA3AF' }}>
                  <CloseIcon sx={{ fontSize: 17 }} />
                </IconButton>
              </Tooltip>
            </>
          ) : (
            <>
              <Typography
                sx={{
                  fontSize: '13.5px',
                  fontWeight: 600,
                  color: 'var(--tt-text-1)',
                  flexGrow: 1,
                  minWidth: 0,
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
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
              <Tooltip title={tr('关闭面板')}>
                <IconButton size="small" sx={{ color: '#9CA3AF' }} onClick={() => onHide?.()}>
                  <CloseIcon sx={{ fontSize: 17 }} />
                </IconButton>
              </Tooltip>
            </>
          )}
        </Box>

        {/* 模式分页：通用模式只有任务页，不显示无意义的单选；预览态不显示 */}
        {hasCanvas && !previewPath && (
          <Box
            sx={{
              mx: 1.5,
              mb: 1,
              p: 0.4,
              display: 'flex',
              gap: 0.4,
              borderRadius: '999px',
              bgcolor: 'var(--tt-surface-3)',
            }}
          >
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
                    py: 0.55,
                    borderRadius: '999px',
                    border: 'none',
                    bgcolor: active ? 'var(--tt-surface)' : 'transparent',
                    boxShadow: active ? '0 1px 4px rgba(15, 23, 42, 0.10)' : 'none',
                    color: active ? 'var(--tt-text-1)' : 'var(--tt-text-3)',
                    fontSize: '12px',
                    fontWeight: active ? 600 : 500,
                    fontFamily: 'inherit',
                    cursor: 'pointer',
                    transition: 'all 0.15s ease',
                  }}
                >
                  {item.label}
                </Box>
              );
            })}
          </Box>
        )}

        {previewPath ? (
          <Box sx={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', px: 1, pb: 1 }}>
            <Box
              sx={{
                flex: 1,
                minHeight: 0,
                display: 'flex',
                flexDirection: 'column',
                p: 1,
                borderRadius: '14px',
                border: '1px solid var(--tt-border-soft)',
                background: 'var(--tt-surface)',
              }}
            >
              <FilePreviewBody state={preview.state} data={preview.data} errMsg={preview.errMsg} />
            </Box>
          </Box>
        ) : tab === 'canvas' && canvas ? (
          <Box
            sx={{
              flex: 1,
              minHeight: 0,
              mx: 1,
              mb: 1,
              borderRadius: '14px',
              border: '1px solid var(--tt-border-soft)',
              background: 'var(--tt-surface)',
              display: 'flex',
              flexDirection: 'column',
              overflow: 'hidden',
            }}
          >
            {canvas}
          </Box>
        ) : (
          <Box
            sx={{
              flex: 1,
              minHeight: 0,
              overflowY: 'auto',
              px: 1,
              pb: 1,
              display: 'flex',
              flexDirection: 'column',
              gap: 0.75,
            }}
          >
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
                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.75 }}>
                  {fileOps.map((f) => (
                    <ArtifactCard key={f.id} path={f.srcPath} onClick={onArtifactOpen} hint={tr('在面板内预览')} />
                  ))}
                </Box>
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
      </Box>

      {/* 系统默认程序打开失败提示（不静默） */}
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
    </Box>
  );
};

export default TaskMonitor;
