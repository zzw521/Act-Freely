/**
 * 通天办公 - 消息流组件 (按对话轮次渲染 v3)
 *
 * - 用户消息: 右对齐品牌绿气泡
 * - 助手回复: 紧跟在对应轮次的用户消息下方（多轮顺序正确）
 * - 思考与工具调用（执行过程）: 属于模型过程，内嵌在通天助手回复气泡内，默认折叠可展开
 * - 交付物: 同样内嵌在该轮助手回复中
 */

import React, { useRef, useEffect, useState } from 'react';
import {
  Box,
  Typography,
  Avatar,
  Chip,
  LinearProgress,
  Collapse,
  CircularProgress,
  IconButton,
  Tooltip,
  Snackbar,
  Alert,
} from '@mui/material';
import SmartToyIcon from '@mui/icons-material/SmartToy';
import BuildIcon from '@mui/icons-material/Build';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';
import Inventory2Icon from '@mui/icons-material/Inventory2';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import ThumbUpOutlinedIcon from '@mui/icons-material/ThumbUpOutlined';
import ThumbDownOutlinedIcon from '@mui/icons-material/ThumbDownOutlined';
import ContentCopyIcon from '@mui/icons-material/ContentCopy';
import ShareIcon from '@mui/icons-material/Share';
import RefreshIcon from '@mui/icons-material/Refresh';
import Markdown from 'react-markdown';
import { Message, Role, ChatEvent, StepType } from '../../../shared/types';
import FilePreview from '../FilePreview';
import { useAppSettings } from '../../lib/use-app-settings';
import { openArtifact } from '../../lib/artifact-open';
import { useI18n } from '../../i18n';

// re-export for external usage of event types
export type { ChatEvent } from '../../../shared/types';

/** 评价表默认值（模块级常量，避免每次渲染新建对象） */
const EMPTY_FEEDBACK: Record<number, 'up' | 'down'> = {};

/**
 * 生成式 UI 沙箱预览。
 * sandbox 故意不带 allow-same-origin：脚本可以跑，但拿不到父页面 DOM、localStorage 与本机入口。
 */
const SandboxedHtml: React.FC<{ code: string }> = ({ code }) => {
  const { t: tr } = useI18n();
  return (
  <Box
    sx={{
      my: 1.25,
      border: '1px solid var(--tt-border-soft)',
      borderRadius: '10px',
      overflow: 'hidden',
      bgcolor: '#fff',
    }}
  >
    <Box
      sx={{
        px: 1.25,
        py: 0.5,
        bgcolor: 'var(--tt-surface-2)',
        borderBottom: '1px solid var(--tt-border-soft)',
        display: 'flex',
        alignItems: 'center',
        gap: 0.75,
        flexWrap: 'wrap',
      }}
    >
      <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-3)' }}>{tr('生成式 UI · 沙箱内渲染')}</Typography>
      <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>
        {tr('脚本可运行，但无法访问本机数据')}
      </Typography>
    </Box>
    <iframe
      title={tr('生成式 UI 预览')}
      sandbox="allow-scripts allow-forms"
      srcDoc={code}
      style={{ display: 'block', width: '100%', height: 420, border: 0, background: '#fff' }}
    />
  </Box>
  );
};

interface ChatStreamProps {
  messages: Message[];
  /** 当前轮事件（保留兼容） */
  events: ChatEvent[];
  /** 按对话轮次分组的事件流（第 N 条用户消息对应第 N 组） */
  turnEvents?: ChatEvent[][];
  isLoading?: boolean;
  /** 正在重新生成的轮次索引 */
  regeneratingTurn?: number | null;
  /** 重新生成回调（真实重跑原目标） */
  onRegenerate?: (turnIdx: number) => void;
  /**
   * 轮次 → 回复评价（会话状态提供，组件不再本地记账）：
   * 切会话不串轮，刷新/重启后按正文库恢复。
   */
  feedback?: Record<number, 'up' | 'down'>;
  /** 评价回调（kind 为 null 即取消评价），落库失败时 reject */
  onFeedback?: (turnIdx: number, kind: 'up' | 'down' | null) => Promise<void>;
  /** Ctrl+F 会话内查找当前定位到的轮次（0 基，与 data-turn 一致） */
  highlightTurn?: number | null;
}

const ChatStream: React.FC<ChatStreamProps> = ({ messages, events, turnEvents, isLoading, regeneratingTurn, onRegenerate, feedback = EMPTY_FEEDBACK, onFeedback, highlightTurn }) => {
  const { t: tr, locale } = useI18n();
  const messagesEndRef = useRef<HTMLDivElement>(null);
  // 每轮执行过程默认折叠，点击展开查看详情
  const [openTurns, setOpenTurns] = useState<Record<number, boolean>>({});
  const [copiedTurn, setCopiedTurn] = useState<number | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  // 产物文件预览
  const [previewPath, setPreviewPath] = useState<string | null>(null);

  // 对话字号、字体与产物打开方式（偏好设置真实生效）
  const [appSettings] = useAppSettings();
  const chatFontSize = (appSettings.chatFontSize as number) ?? 14;
  const previewMode = (appSettings.previewMode as string) ?? '应用内预览';
  // 默认展开工具调用：只影响「初始是否展开」，逐轮手动切换仍然优先
  const expandToolCalls = Boolean(appSettings.expandToolCalls);

  // 实验开关：这两项在渲染层真实消费，其余在设置页如实标注未提供
  const experiments = (appSettings.experiments as Record<string, boolean> | undefined) ?? {};
  // 助手过程默认展开条件：设置项打开，或实验开关 collapse 关掉（默认展开）
  const defaultProcessOpen = experiments.collapse === false || expandToolCalls;
  // 生成式 UI：```html 代码块渲染为沙箱 iframe，而不是显示源码
  const generativeUI = Boolean(experiments.generativeUI);

  /** Markdown 渲染（生成式 UI 开启时接管 html 代码块） */
  const renderMarkdown = (text: string) => (
    <Markdown
      components={
        generativeUI
          ? {
              pre({ children }) {
                if (React.isValidElement(children)) {
                  const props = children.props as { className?: string; children?: React.ReactNode };
                  const lang = /language-([\w-]+)/.exec(props.className ?? '')?.[1]?.toLowerCase();
                  if (lang === 'html' || lang === 'htm') {
                    return <SandboxedHtml code={String(props.children ?? '')} />;
                  }
                }
                return <pre>{children}</pre>;
              },
            }
          : undefined
      }
    >
      {text}
    </Markdown>
  );

  // 自动滚动到底部
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, events, turnEvents]);

  /** 逐轮手动切换：以当前实际展开态取反，兼容「默认展开工具调用」的初始值 */
  const toggleTurn = (turnIdx: number, currentOpen: boolean) => {
    setOpenTurns((prev) => ({ ...prev, [turnIdx]: !currentOpen }));
  };

  /** 产物点击：按「预览方式」分流；系统打开失败时提示并回退应用内 */
  const handleOpenArtifact = async (path: string) => {
    const result = await openArtifact(path, previewMode);
    if (result.error) setActionMsg(result.error);
    if (result.previewPath) setPreviewPath(result.previewPath);
  };

  // ── 回复操作栏 ─────────────────────────────────────────

  /** 点赞/点踩（互斥，再次点击取消）；评价由会话状态持有并落库，失败时提示 */
  const toggleFeedback = async (turnIdx: number, kind: 'up' | 'down') => {
    if (!onFeedback) return;
    try {
      await onFeedback(turnIdx, feedback[turnIdx] === kind ? null : kind);
    } catch (err: unknown) {
      setActionMsg(err instanceof Error ? err.message : tr('评价保存失败'));
    }
  };

  /** 分享产出文件：把本轮真实产物路径复制到剪贴板（不伪造分享链接） */
  const copyArtifactPaths = (evts: ChatEvent[]) => {
    const paths = evts
      .filter((e) => e.type === 'deliver')
      .flatMap((e) => e.artifacts ?? []);
    if (paths.length === 0) {
      setActionMsg(tr('本轮没有可分享的产出文件'));
      return;
    }
    const done = () => setActionMsg(tr('已复制 {count} 个产出文件路径，可直接粘贴发送', { count: paths.length }));
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(paths.join('\n')).then(done).catch(() => setActionMsg(tr('复制失败，请手动选择文本')));
    } else {
      setActionMsg(tr('当前环境不支持剪贴板复制'));
    }
  };

  /** 复制回复内容（回复正文，缺正文时退回交付摘要 + 产物路径） */
  const copyTurn = (turnIdx: number, evts: ChatEvent[]) => {
    const text = evts
      .filter((e) => e.type === 'deliver')
      .map((e) => {
        const summary = (e as { summary?: string }).summary ?? '';
        // 界面气泡显的是 final_text，复制必须与所见一致
        const finalText = (e as { final_text?: string }).final_text ?? '';
        return [finalText || summary, ...(e.artifacts ?? [])].filter(Boolean).join('\n');
      })
      .join('\n')
      .trim() || tr('任务完成');
    const done = () => {
      setCopiedTurn(turnIdx);
      setTimeout(() => setCopiedTurn((c) => (c === turnIdx ? null : c)), 2000);
    };
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => setActionMsg(tr('复制失败，请手动选择文本')));
    } else {
      setActionMsg(tr('当前环境不支持剪贴板复制'));
    }
  };

  // ── 执行过程明细渲染 ──────────────────────────────────────

  const renderEventDetail = (event: ChatEvent, idx: number, running: boolean): React.ReactNode => {
    if (event.type === 'step_start') {
      return (
        <Box key={`evt-${idx}`} sx={{ mb: 1.5 }}>
          {/* 仅运行中显示进度条；步骤完成后不再动画 */}
          {running && (
            <LinearProgress
              sx={{
                height: 4,
                borderRadius: 2,
                bgcolor: 'rgba(34, 197, 94, 0.12)',
                '& .MuiLinearProgress-bar': { bgcolor: '#22C55E', borderRadius: 2 },
              }}
            />
          )}
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', mt: running ? 0.5 : 0 }}>
            {tr('正在执行: {desc}…', { desc: event.description ?? '' })}
          </Typography>
        </Box>
      );
    }

    if (event.type === 'step_progress') {
      return (
        <Box key={`evt-${idx}`} sx={{ mb: 0.5 }}>
          <Typography
            sx={{
              fontSize: '12px',
              color: 'var(--tt-text-3)',
              fontFamily: "'SF Mono', Consolas, monospace",
              bgcolor: 'var(--tt-chat-code-bg)',
              borderRadius: '6px',
              px: 1,
              py: 0.5,
              display: 'inline-block',
            }}
          >
            {event.message}
          </Typography>
        </Box>
      );
    }

    if (event.type === 'step_done') {
      return (
        <Box key={`evt-${idx}`} sx={{ mb: 1.5, display: 'flex', alignItems: 'flex-start', gap: 0.75 }}>
          <CheckCircleIcon sx={{ fontSize: 16, color: '#22C55E', mt: '2px' }} />
          <Box>
            <Typography sx={{ fontSize: '13px', color: '#15803D', fontWeight: 500 }}>{tr('步骤完成')}</Typography>
            {event.output && (
              <Typography sx={{ fontSize: '12px', color: '#94A3B8' }}>{event.output.substring(0, 200)}</Typography>
            )}
          </Box>
        </Box>
      );
    }

    if (event.type === 'step_failed') {
      return (
        <Box key={`evt-${idx}`} sx={{ mb: 1.5, display: 'flex', alignItems: 'flex-start', gap: 0.75 }}>
          <ErrorOutlineIcon sx={{ fontSize: 16, color: '#EF4444', mt: '2px' }} />
          <Typography sx={{ fontSize: '13px', color: '#DC2626' }}>{tr('步骤失败: {error}', { error: event.error ?? '' })}</Typography>
        </Box>
      );
    }

    return null;
  };

  // ── 助手回复块（思考/工具调用 + 交付物内嵌其中） ─────────────

  const renderAssistantTurn = (evts: ChatEvent[], turnIdx: number, running: boolean): React.ReactNode => {
    const processEvents = evts.filter((e) =>
      ['step_start', 'step_progress', 'step_done', 'step_failed'].includes(e.type),
    );
    const delivers = evts.filter((e) => e.type === 'deliver');
    const hasContent = processEvents.length > 0 || delivers.length > 0 || running;
    if (!hasContent) return null;

    const doneCount = processEvents.filter((e) => e.type === 'step_done').length;
    const failedCount = processEvents.filter((e) => e.type === 'step_failed').length;
    const lastStart = [...processEvents].reverse().find((e) => e.type === 'step_start');
    const processRunning = running && failedCount === 0;
    // 显式切换优先，其次是设置项/实验开关决定的默认态
    const processOpen = openTurns[turnIdx] ?? defaultProcessOpen;
    const headerText =
      processRunning && lastStart && lastStart.type === 'step_start'
        ? tr('正在执行：{desc}…', { desc: lastStart.description ?? '' })
        : failedCount > 0
          ? tr('执行过程（{done} 个步骤完成，{failed} 个失败）', { done: doneCount, failed: failedCount })
          : tr('执行过程 · {done} 个步骤已完成', { done: doneCount });

    return (
      <Box
        key={`turn-${turnIdx}`}
        sx={{
          display: 'flex',
          gap: 1.5,
          mb: 2.5,
          alignItems: 'flex-start',
          // 查找命中轮次：描边标出位置，不改布局
          ...(highlightTurn === turnIdx ? { outline: '2px solid #0D9488', outlineOffset: 4, borderRadius: '10px' } : {}),
        }}
      >
        <Avatar
          sx={{ width: 32, height: 32, flexShrink: 0, bgcolor: '#16A34A', boxShadow: '0 2px 8px rgba(22, 163, 74, 0.3)' }}
        >
          <SmartToyIcon sx={{ fontSize: 18 }} />
        </Avatar>
        <Box sx={{ maxWidth: '80%', minWidth: 0, flexGrow: 1 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.5 }}>
            <Typography sx={{ fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-2)' }}>{tr('通天助手')}</Typography>
          </Box>
          <Box
            sx={{
              px: 2,
              py: 1.5,
              borderRadius: '4px 16px 16px 16px',
              bgcolor: 'var(--tt-surface)',
              border: '1px solid #E2E8F0',
              boxShadow: '0 1px 3px rgba(15, 23, 42, 0.04)',
            }}
          >
            {/* 思考与工具调用（模型过程）：内嵌在回复中，默认折叠 */}
            {processEvents.length > 0 && (
              <Box sx={{ mb: delivers.length > 0 || running ? 1.25 : 0 }}>
                <Box
                  onClick={() => toggleTurn(turnIdx, processOpen)}
                  sx={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 1,
                    px: 1.25,
                    py: 0.75,
                    borderRadius: '10px',
                    border: '1px solid #E2E8F0',
                    background: 'var(--tt-surface-2)',
                    cursor: 'pointer',
                    width: 'fit-content',
                    minWidth: 220,
                    maxWidth: '100%',
                    transition: 'background 0.15s ease',
                    '&:hover': { background: 'var(--tt-surface-2)' },
                  }}
                >
                  {processRunning ? (
                    <CircularProgress size={13} sx={{ color: '#22C55E' }} />
                  ) : failedCount > 0 ? (
                    <ErrorOutlineIcon sx={{ fontSize: 14, color: '#EF4444' }} />
                  ) : (
                    <CheckCircleIcon sx={{ fontSize: 14, color: '#22C55E' }} />
                  )}
                  <Typography sx={{ fontSize: '12px', color: processRunning ? '#475569' : '#64748B', flexGrow: 1 }}>
                    {headerText}
                  </Typography>
                  <KeyboardArrowDownIcon
                    sx={{
                      fontSize: 15,
                      color: '#94A3B8',
                      transform: processOpen ? 'rotate(180deg)' : 'none',
                      transition: 'transform 0.2s ease',
                    }}
                  />
                </Box>
                <Collapse in={processOpen}>
                  <Box sx={{ mt: 1, px: 1.5, py: 1, borderRadius: '10px', border: '1px solid var(--tt-border-soft)', background: 'var(--tt-chat-process-bg)' }}>
                    {processEvents.map((e, i) => renderEventDetail(e, i, running))}
                  </Box>
                </Collapse>
              </Box>
            )}

            {/* 交付物（有产物时）/ 文字回复（简单问答无交付物，直接展示最终答案） */}
            {delivers.map((event, idx) => {
              const arts = event.artifacts ?? [];
              const finalText = (event as { final_text?: string }).final_text ?? '';
              if (arts.length === 0) {
                return (
                  <Box key={`deliver-${idx}`} className="markdown-body" sx={{ color: 'var(--tt-text-2)', fontSize: chatFontSize }}>
                    {renderMarkdown(finalText || tr('任务完成'))}
                  </Box>
                );
              }
              return (
                <Box key={`deliver-${idx}`}>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1 }}>
                    <Inventory2Icon sx={{ fontSize: 18, color: '#16A34A' }} />
                    <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#15803D' }}>
                      {tr('任务已完成，这是你的交付物')}
                    </Typography>
                  </Box>
                  {finalText && (
                    <Box className="markdown-body" sx={{ color: 'var(--tt-text-2)', fontSize: chatFontSize, mb: 1 }}>
                      {renderMarkdown(finalText)}
                    </Box>
                  )}
                  <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
                    {arts.map((artifact, artIdx) => (
                      <Tooltip
                        key={`${artifact}-${artIdx}`}
                        title={tr(previewMode === '系统默认程序' ? '点击用系统默认程序打开' : '点击在应用内预览')}
                        placement="top"
                      >
                        <Chip
                          label={artifact}
                          size="small"
                          clickable
                          onClick={() => void handleOpenArtifact(artifact)}
                          sx={{
                            fontSize: '12px',
                            height: 24,
                            bgcolor: '#F0FDF4',
                            color: '#15803D',
                            border: '1px solid #BBF7D0',
                            fontWeight: 500,
                            maxWidth: '100%',
                            '&:hover': { bgcolor: '#DCFCE7', borderColor: '#86EFAC' },
                          }}
                        />
                      </Tooltip>
                    ))}
                  </Box>
                </Box>
              );
            })}

            {/* 思考中（本轮运行中且尚无交付） */}
            {running && delivers.length === 0 && processEvents.length === 0 && (
              <Box sx={{ display: 'flex', gap: 0.5, py: 0.5 }}>
                {[0, 1, 2].map((i) => (
                  <Box
                    key={i}
                    sx={{
                      width: 6,
                      height: 6,
                      borderRadius: '50%',
                      bgcolor: '#22C55E',
                      animation: 'thinkingBounce 1.2s infinite ease-in-out',
                      animationDelay: `${i * 0.15}s`,
                      '@keyframes thinkingBounce': {
                        '0%, 80%, 100%': { transform: 'scale(0.6)', opacity: 0.4 },
                        '40%': { transform: 'scale(1)', opacity: 1 },
                      },
                    }}
                  />
                ))}
              </Box>
            )}
          </Box>

          {/* 回复操作栏：点赞/点踩/复制/分享（仅有文件产出时）/重新生成 */}
          {!running && (
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.25, mt: 0.75 }}>
              <Tooltip title={tr('点赞')}>
                <IconButton size="small" sx={{ width: 26, height: 26 }} onClick={() => toggleFeedback(turnIdx, 'up')}>
                  <ThumbUpOutlinedIcon sx={{ fontSize: 15, color: feedback[turnIdx] === 'up' ? '#16A34A' : '#9CA3AF' }} />
                </IconButton>
              </Tooltip>
              <Tooltip title={tr('点踩')}>
                <IconButton size="small" sx={{ width: 26, height: 26 }} onClick={() => toggleFeedback(turnIdx, 'down')}>
                  <ThumbDownOutlinedIcon sx={{ fontSize: 15, color: feedback[turnIdx] === 'down' ? '#EF4444' : '#9CA3AF' }} />
                </IconButton>
              </Tooltip>
              <Tooltip title={copiedTurn === turnIdx ? tr('已复制') : tr('复制回复')}>
                <IconButton size="small" sx={{ width: 26, height: 26 }} onClick={() => copyTurn(turnIdx, evts)}>
                  <ContentCopyIcon sx={{ fontSize: 14, color: copiedTurn === turnIdx ? '#16A34A' : '#9CA3AF' }} />
                </IconButton>
              </Tooltip>
              {/* 分享：仅在本轮有文件产出时显示；复制真实产物路径，不伪造分享链接 */}
              {delivers.some((d) => (d.artifacts ?? []).length > 0) && (
                <Tooltip title={tr('复制产出文件路径')}>
                  <IconButton size="small" sx={{ width: 26, height: 26 }} onClick={() => copyArtifactPaths(evts)}>
                    <ShareIcon sx={{ fontSize: 15, color: 'var(--tt-text-4)' }} />
                  </IconButton>
                </Tooltip>
              )}
              <Tooltip title={regeneratingTurn === turnIdx ? tr('重新生成中…') : tr('重新生成')}>
                <span>
                  <IconButton
                    size="small"
                    sx={{ width: 26, height: 26 }}
                    disabled={regeneratingTurn != null}
                    onClick={() => onRegenerate?.(turnIdx)}
                  >
                    <RefreshIcon sx={{ fontSize: 15, color: 'var(--tt-text-4)' }} />
                  </IconButton>
                </span>
              </Tooltip>
              {copiedTurn === turnIdx && (
                <Typography sx={{ fontSize: '11px', color: '#16A34A', ml: 0.5 }}>{tr('已复制')}</Typography>
              )}
            </Box>
          )}
        </Box>
      </Box>
    );
  };

  // ── 按轮次组装消息流 ──────────────────────────────────────

  const renderStream = (): React.ReactNode[] => {
    const nodes: React.ReactNode[] = [];
    const userMessages = messages.filter((m) => m.role === Role.USER);
    const lastUserIdx = userMessages.length - 1;
    let turnIdx = -1;

    messages.forEach((msg) => {
      const isUser = msg.role === Role.USER;
      const isTool = msg.role === Role.TOOL;

      if (isUser) {
        turnIdx += 1;
        const currentTurn = turnIdx;
        // 用户消息：右对齐气泡（本轮锚点：即使助手尚未产出节点，Ctrl+F 也能定位到这一轮）
        nodes.push(
          <Box
            key={msg.id}
            data-turn={currentTurn}
            sx={{
              display: 'flex',
              justifyContent: 'flex-end',
              mb: 2.5,
              ...(highlightTurn === currentTurn
                ? { outline: '2px solid #0D9488', outlineOffset: 4, borderRadius: '16px' }
                : {}),
            }}
          >
            <Box sx={{ maxWidth: '75%' }}>
              <Box
                sx={{
                  px: 2,
                  py: 1.25,
                  borderRadius: '16px 16px 4px 16px',
                  background: 'linear-gradient(135deg, #22C55E 0%, #16A34A 100%)',
                  color: '#fff',
                  fontSize: chatFontSize,
                  fontFamily: 'var(--tt-chat-font)',
                  lineHeight: 1.6,
                  boxShadow: '0 2px 8px rgba(22, 163, 74, 0.25)',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {msg.content as string}
              </Box>
              <Typography sx={{ fontSize: '11px', color: '#94A3B8', mt: 0.5, textAlign: 'right' }}>
                {new Date(msg.ts).toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' })}
              </Typography>
            </Box>
          </Box>,
        );

        // 本轮助手回复：紧跟在该用户消息下方（思考/工具调用/交付物内嵌）
        const evts = turnEvents?.[currentTurn] ?? (currentTurn === lastUserIdx ? events : []);
        // 运行中：最后一轮提交执行中，或本轮正在重新生成
        const running =
          (Boolean(isLoading) && currentTurn === lastUserIdx) || regeneratingTurn === currentTurn;
        const turnNode = renderAssistantTurn(evts, currentTurn, running);
        if (turnNode) nodes.push(turnNode);
        return;
      }

      // AI / 工具文本消息：左对齐卡片（保留原样式）
      const isAi = !isTool;
      nodes.push(
        <Box key={msg.id} sx={{ display: 'flex', gap: 1.5, mb: 2.5, alignItems: 'flex-start' }}>
          <Avatar
            sx={{
              width: 32,
              height: 32,
              flexShrink: 0,
              bgcolor: isAi ? '#16A34A' : '#64748B',
              boxShadow: isAi ? '0 2px 8px rgba(22, 163, 74, 0.3)' : 'none',
            }}
          >
            {isAi ? <SmartToyIcon sx={{ fontSize: 18 }} /> : <BuildIcon sx={{ fontSize: 16 }} />}
          </Avatar>

          <Box sx={{ maxWidth: '80%', minWidth: 0 }}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 0.5 }}>
              <Typography sx={{ fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-2)' }}>
                {isAi ? tr('通天助手') : tr('工具调用')}
              </Typography>
              <Typography sx={{ fontSize: '11px', color: '#CBD5E1' }}>
                {new Date(msg.ts).toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' })}
              </Typography>
              {isTool && Boolean(msg.attachments?.['toolName']) && (
                <Chip
                  label={msg.attachments?.['toolName'] as string}
                  size="small"
                  sx={{
                    height: 20,
                    fontSize: '11px',
                    bgcolor: 'rgba(14, 165, 233, 0.1)',
                    color: '#0284C7',
                    fontWeight: 600,
                  }}
                />
              )}
            </Box>

            <Box
              sx={{
                px: 2,
                py: 1.5,
                borderRadius: '4px 16px 16px 16px',
                bgcolor: isAi ? 'var(--tt-chat-assistant-bg)' : 'var(--tt-chat-process-bg)',
                border: '1px solid var(--tt-border)',
                boxShadow: 'var(--tt-shadow-card)',
              }}
            >
              <Box className="markdown-body" sx={{ color: 'var(--tt-text-2)', fontSize: chatFontSize }}>
                {renderMarkdown(msg.content as string)}
              </Box>

              {Array.isArray(msg.attachments?.['files']) && (
                <Box sx={{ mt: 1, display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
                  {(msg.attachments!['files'] as string[]).map((file, idx) => (
                    <Chip
                      key={idx}
                      label={`📎 ${file}`}
                      size="small"
                      variant="outlined"
                      sx={{ fontSize: '11px', height: 22, borderColor: '#E2E8F0', color: 'var(--tt-text-3)' }}
                    />
                  ))}
                </Box>
              )}
            </Box>
          </Box>
        </Box>,
      );
    });

    return nodes;
  };

  // 保留步骤类型映射（供未来扩展）
  const getStepTypeLabel = (type: StepType): string => {
    const labels: Record<string, string> = {
      llm_call: 'AI 调用',
      file_read: '读取文件',
      file_write: '写入文件',
      file_convert: '格式转换',
      file_organize: '文件整理',
      user_confirm: '用户确认',
      skill_call: '技能调用',
      mcp_tool: 'MCP 工具',
      workspace_delegate: '工作区委派',
    };
    return labels[type] || type;
  };
  void getStepTypeLabel;

  return (
    <Box
      sx={{
        display: 'flex',
        flexDirection: 'column',
        pb: 1,
      }}
    >
      {renderStream()}

      {/* 操作栏提示消息（占位能力不静默） */}
      <Snackbar
        open={!!actionMsg}
        autoHideDuration={2500}
        onClose={() => setActionMsg(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert severity="info" onClose={() => setActionMsg(null)} sx={{ fontSize: '13px' }}>
          {actionMsg}
        </Alert>
      </Snackbar>

      {/* 自动滚动锚点 */}
      <div ref={messagesEndRef} />

      {/* 产物文件预览弹窗 */}
      <FilePreview path={previewPath} onClose={() => setPreviewPath(null)} />
    </Box>
  );
};

export default ChatStream;
