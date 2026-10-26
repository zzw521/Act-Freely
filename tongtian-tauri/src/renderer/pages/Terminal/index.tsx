/**
 * 通天办公 - 终端 AI+
 *
 * 事实来源：
 * - 开关/平台/shell/会话上限来自 `terminal:capabilities`（Rust 或本地权威实现）
 * - 输出来自引擎真实 shell 的增量读取；AI 只根据最近输出给建议，永不自行执行
 * - 输入分两类留痕：manual=用户键入，ai=用户确认执行的 AI 建议（Rust 侧写审计）
 *
 * 不发生的事也写清楚：未开启、引擎未运行、浏览器/Mock 通道、shell 已退出时，
 * 相应入口置灰并给出原因，不用「保存成功」冒充「现在能用」。
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  TextField,
  Typography,
} from '@mui/material';
import TerminalIcon from '@mui/icons-material/Terminal';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import StopIcon from '@mui/icons-material/Stop';
import ContentCopyIcon from '@mui/icons-material/ContentCopy';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import RefreshIcon from '@mui/icons-material/Refresh';
import { useNavigate } from 'react-router-dom';
import {
  closeTerminal,
  getTerminalCapabilities,
  openTerminal,
  readTerminal,
  suggestTerminalCommand,
  writeTerminal,
} from '../../lib/alignment-api';
import type {
  TerminalAiResult,
  TerminalCapabilities,
  TerminalSuggestionCommand,
} from '../../../shared/contracts';
import { errorTextOf } from '../../../shared/error-messages';
import { useAppStore } from '../../stores/useAppStore';
import { getActiveWorkspaceId, useWorkspaceStore } from '../../store/workspaceStore';

const MAX_OUTPUT_CHARS = 200_000;
const POLL_MS = 700;

const stripAnsi = (text: string): string =>
  text.replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, '');

const appendCapped = (prev: string, chunk: string): string => {
  const next = prev + chunk;
  return next.length <= MAX_OUTPUT_CHARS ? next : next.slice(next.length - MAX_OUTPUT_CHARS);
};

const riskLabel: Record<TerminalAiResult['suggestion']['risk'], string> = {
  low: '低风险',
  medium: '中等风险',
  high: '高风险',
  unknown: '风险未知',
};

const riskColor: Record<TerminalAiResult['suggestion']['risk'], string> = {
  low: '#047857',
  medium: '#B45309',
  high: '#B91C1C',
  unknown: '#6B7280',
};

interface TerminalPanelProps {
  /** 嵌入设置页时由外层滚动，不再占满整屏 */
  embedded?: boolean;
}

const TerminalPanel: React.FC<TerminalPanelProps> = ({ embedded = false }) => {
  const navigate = useNavigate();
  const showNotification = useAppStore((s) => s.showNotification);
  const workspaces = useWorkspaceStore((s) => s.workspaces);
  const activeId = useWorkspaceStore((s) => s.activeId);

  const [caps, setCaps] = useState<TerminalCapabilities | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [cwd, setCwd] = useState('');
  const [opening, setOpening] = useState(false);
  const [closing, setClosing] = useState(false);

  const [sessionId, setSessionId] = useState('');
  const [sessionShell, setSessionShell] = useState('');
  const [sessionCwd, setSessionCwd] = useState('');
  const [output, setOutput] = useState('');
  const [running, setRunning] = useState(false);
  const [banner, setBanner] = useState('');
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);

  const [question, setQuestion] = useState('');
  const [aiLoading, setAiLoading] = useState(false);
  const [aiError, setAiError] = useState('');
  const [aiResult, setAiResult] = useState<TerminalAiResult | null>(null);
  const [pendingCommand, setPendingCommand] = useState<TerminalSuggestionCommand | null>(null);

  const offsetRef = useRef(0);
  const pollBusyRef = useRef(false);
  const outputRef = useRef<HTMLDivElement | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await getTerminalCapabilities();
      setCaps(next);
      setLoadError('');
    } catch (err) {
      setCaps(null);
      setLoadError(errorTextOf(err, '终端能力读取失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 默认工作目录：当前工作空间的真实目录；用户仍可改写
  useEffect(() => {
    if (cwd.trim()) return;
    const ws = workspaces.find((w) => w.id === activeId);
    if (ws?.rootPath) setCwd(ws.rootPath);
  }, [workspaces, activeId, cwd]);

  const pipeReady = Boolean(caps?.pipe);
  const enabled = Boolean(caps?.enabled);
  const engineRunning = Boolean(caps?.engineRunning);
  const ready = pipeReady && enabled && engineRunning;
  const newline = caps?.platform === 'windows' ? '\r\n' : '\n';

  const openDisabledReason = !pipeReady
    ? caps?.reason || '当前通道没有本机 shell 进程'
    : !enabled
      ? '终端 AI+ 未启用，请先在「设置 → ACP 与终端」中显式开启'
      : !engineRunning
        ? '本机引擎未运行，无法创建终端会话；请先发起一个任务或重启应用'
        : '';

  // 增量读取：offset 只前进；会话被关闭/引擎重启时停止轮询并说明
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    void (async () => {
      while (!cancelled) {
        await new Promise((resolve) => window.setTimeout(resolve, POLL_MS));
        if (cancelled) return;
        if (pollBusyRef.current) continue;
        pollBusyRef.current = true;
        try {
          const r = await readTerminal(sessionId, offsetRef.current);
          if (cancelled) return;
          offsetRef.current = r.offset;
          setRunning(r.running);
          setSessionCwd(r.cwd || sessionCwd);
          if (r.chunk) {
            const clean = stripAnsi(r.chunk);
            setOutput((prev) => appendCapped(prev, clean));
          }
          if (r.truncated) setBanner('更早的输出已被环形缓冲覆盖，界面只保留最近 20 万字符');
        } catch (err) {
          if (cancelled) return;
          setBanner(errorTextOf(err, '终端输出读取失败'));
          setRunning(false);
          return;
        } finally {
          pollBusyRef.current = false;
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // sessionCwd 只在读取时作为兜底，不参与轮询重建
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId]);

  useEffect(() => {
    const el = outputRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [output]);

  const openSession = async () => {
    if (!ready || opening) return;
    setOpening(true);
    setBanner('');
    try {
      const info = await openTerminal(cwd.trim(), getActiveWorkspaceId() || undefined);
      offsetRef.current = info.offset;
      setSessionId(info.sessionId);
      setSessionShell(info.shell);
      setSessionCwd(info.cwd);
      setOutput('');
      setRunning(true);
      setAiResult(null);
      setAiError('');
      setQuestion('');
      showNotification(`终端已打开（${info.shell}）`, 'success');
    } catch (err) {
      setBanner(errorTextOf(err, '打开终端失败'));
      await refresh();
    } finally {
      setOpening(false);
    }
  };

  const closeSession = async () => {
    if (!sessionId || closing) return;
    setClosing(true);
    try {
      await closeTerminal(sessionId);
      showNotification('终端会话已关闭', 'success');
    } catch (err) {
      showNotification(errorTextOf(err, '关闭终端失败'), 'error');
    } finally {
      setClosing(false);
      setSessionId('');
      setRunning(false);
      setAiResult(null);
      setBanner('');
    }
  };

  const send = async (data: string, source: 'manual' | 'ai'): Promise<boolean> => {
    if (!sessionId || sending) return false;
    setSending(true);
    try {
      await writeTerminal(sessionId, data + newline, source);
      return true;
    } catch (err) {
      setBanner(errorTextOf(err, '写入终端失败'));
      await refresh();
      return false;
    } finally {
      setSending(false);
    }
  };

  const submitInput = async () => {
    if (!sessionId || sending) return;
    if (!running) {
      setBanner('shell 已退出，请关闭会话后重新打开');
      return;
    }
    const text = input;
    setInput('');
    const ok = await send(text, 'manual');
    if (!ok) setInput(text);
  };

  const askAi = async () => {
    if (!sessionId || aiLoading) return;
    if (!question.trim()) {
      setAiError('请先描述要解决的问题');
      return;
    }
    setAiLoading(true);
    setAiError('');
    try {
      const result = await suggestTerminalCommand(sessionId, question.trim());
      setAiResult(result);
    } catch (err) {
      setAiResult(null);
      setAiError(errorTextOf(err, '命令建议生成失败'));
    } finally {
      setAiLoading(false);
    }
  };

  const copyCommand = async (command: string) => {
    try {
      await navigator.clipboard.writeText(command);
      showNotification('命令已复制', 'success');
    } catch {
      showNotification('复制失败，请手动选择命令文本', 'error');
    }
  };

  const confirmExecute = async () => {
    const cmd = pendingCommand;
    setPendingCommand(null);
    if (!cmd) return;
    const ok = await send(cmd.command, 'ai');
    if (ok) showNotification('已把 AI 建议写入终端（来源已留痕）', 'success');
  };

  const statusColor = ready ? '#047857' : '#B45309';
  const statusBg = ready ? 'rgba(5, 150, 105, 0.07)' : 'rgba(217, 119, 6, 0.08)';
  const statusBorder = ready ? '1px solid rgba(5, 150, 105, 0.35)' : '1px solid rgba(217, 119, 6, 0.35)';

  const terminalHint = useMemo(() => {
    if (!pipeReady) return caps?.reason || '当前通道没有本机 shell 进程';
    if (!enabled) return '终端 AI+ 默认关闭；开启前请确认你知道它会用你的系统权限执行命令';
    if (!engineRunning) return '终端会话由本机引擎持有；引擎未运行时无法创建会话';
    if (!sessionId) return '打开终端后，命令会在真实 shell 中执行；AI 只给建议，不会自行输入';
    return running ? '命令在真实 shell 中执行；全屏交互程序（vim/top）不可用' : 'shell 已退出';
  }, [pipeReady, caps, enabled, engineRunning, sessionId, running]);

  return (
    <Box
      sx={{
        height: embedded ? 'auto' : 'calc(100vh - 104px)',
        overflowY: embedded ? 'visible' : 'auto',
        pr: embedded ? 0 : 1,
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <TerminalIcon sx={{ fontSize: 22, color: '#0D9488' }} />
        <Typography sx={{ fontSize: '20px', fontWeight: 700, color: 'var(--tt-text-1)' }}>
          终端 AI+
        </Typography>
        {loading && <CircularProgress size={16} sx={{ color: '#0D9488' }} />}
      </Box>
      <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-3)', mt: 0.5 }}>
        在本机工作目录里打开真实 shell；AI 根据最近输出给出命令建议，只有你逐条确认才会写入终端。
      </Typography>

      <Box sx={{ maxWidth: 960, mt: 2 }}>
        {/* 能力状态：开关、引擎、通道三者都就绪才允许开终端 */}
        <Box
          data-testid="terminal-status"
          sx={{ p: 1.75, borderRadius: '14px', border: statusBorder, bgcolor: statusBg }}
        >
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}>
            {ready ? (
              <CheckCircleIcon sx={{ fontSize: 18, color: '#059669' }} />
            ) : (
              <WarningAmberIcon sx={{ fontSize: 18, color: '#D97706' }} />
            )}
            <Typography sx={{ fontSize: '13.5px', fontWeight: 700, color: statusColor }}>
              {ready ? '终端可用' : '终端不可用'}
            </Typography>
            {caps && (
              <Box sx={{ px: 1, py: 0.2, borderRadius: '6px', bgcolor: 'rgba(107, 114, 128, 0.12)' }}>
                <Typography sx={{ fontSize: '11px', fontWeight: 600, color: '#6B7280' }}>
                  {caps.shell || caps.platform}
                </Typography>
              </Box>
            )}
            {sessionId && (
              <Box sx={{ px: 1, py: 0.2, borderRadius: '6px', bgcolor: 'rgba(13, 148, 136, 0.12)' }}>
                <Typography sx={{ fontSize: '11px', fontWeight: 600, color: '#0F766E' }}>
                  会话 {sessionId.slice(-6)} · {running ? '运行中' : '已退出'}
                </Typography>
              </Box>
            )}
          </Box>
          <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-2)', mt: 0.75, lineHeight: 1.7 }}>
            {loading
              ? '正在读取本机终端能力…'
              : loadError
                ? loadError
                : terminalHint}
          </Typography>
          {caps && (
            <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', mt: 0.5 }}>
              管道模式{caps.pipe ? '可用' : '不可用'} · 会话上限 {caps.maxSessions} · 单次输入{' '}
              {Math.floor(caps.inputLimit / 1024)}KB · 引擎{caps.engineRunning ? '已运行' : '未运行'}
            </Typography>
          )}
          {!pipeReady && !loadError && (
            <Box sx={{ mt: 1 }}>
              <Button
                size="small"
                variant="outlined"
                onClick={() => navigate('/settings?s=terminal')}
                sx={{ textTransform: 'none' }}
              >
                查看「ACP 与终端」设置
              </Button>
            </Box>
          )}
        </Box>

        {/* 工作目录 + 会话开关 */}
        <Box
          sx={{
            mt: 2,
            p: 2,
            borderRadius: '14px',
            border: '1px solid var(--tt-border)',
            bgcolor: 'var(--tt-surface)',
          }}
        >
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
            <TextField
              size="small"
              value={cwd}
              onChange={(e) => setCwd(e.target.value)}
              placeholder="工作目录（绝对路径）"
              disabled={Boolean(sessionId) || opening}
              sx={{ flex: '1 1 320px', '& input': { fontSize: '12.5px', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' } }}
              inputProps={{ 'aria-label': '终端工作目录' }}
            />
            <Button
              size="small"
              variant="outlined"
              startIcon={<RefreshIcon />}
              onClick={() => void refresh()}
              disabled={loading || opening}
              sx={{ textTransform: 'none' }}
            >
              刷新
            </Button>
            {!sessionId ? (
              <Button
                data-testid="terminal-open"
                size="small"
                variant="contained"
                startIcon={opening ? <CircularProgress size={14} sx={{ color: '#fff' }} /> : <PlayArrowIcon />}
                disabled={!ready || opening || !cwd.trim()}
                onClick={() => void openSession()}
                sx={{ textTransform: 'none', bgcolor: '#0D9488', '&:hover': { bgcolor: '#0F766E' } }}
              >
                {opening ? '正在打开…' : '打开终端'}
              </Button>
            ) : (
              <Button
                data-testid="terminal-close"
                size="small"
                variant="outlined"
                startIcon={closing ? <CircularProgress size={14} /> : <StopIcon />}
                disabled={closing}
                onClick={() => void closeSession()}
                sx={{ textTransform: 'none' }}
              >
                {closing ? '正在关闭…' : '关闭会话'}
              </Button>
            )}
          </Box>
          <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3)', mt: 1 }}>
            {openDisabledReason ||
              (sessionId
                ? `当前目录：${sessionCwd || cwd}。会话由本机引擎持有，空闲 30 分钟自动回收。`
                : '打开后可在下方直接输入命令；AI 建议不会自动执行。')}
          </Typography>
        </Box>

        {banner && (
          <Box
            sx={{
              mt: 1.5,
              p: 1.25,
              borderRadius: '10px',
              bgcolor: 'rgba(217, 119, 6, 0.1)',
              border: '1px solid rgba(217, 119, 6, 0.3)',
            }}
          >
            <Typography sx={{ fontSize: '12.5px', color: '#92400E' }}>{banner}</Typography>
          </Box>
        )}

        {/* 输出面板 */}
        <Box
          ref={outputRef}
          data-testid="terminal-output"
          sx={{
            mt: 2,
            height: 340,
            overflowY: 'auto',
            borderRadius: '14px',
            bgcolor: '#0B1220',
            border: '1px solid #1F2937',
            p: 1.5,
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
            fontSize: '12.5px',
            lineHeight: 1.55,
            color: '#E5E7EB',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-all',
          }}
        >
          {output || (
            <Typography sx={{ fontSize: '12.5px', color: '#6B7280', fontFamily: 'inherit' }}>
              {sessionId ? '等待输出…' : '打开终端后，shell 输出会显示在这里。'}
            </Typography>
          )}
        </Box>

        {/* 输入区 */}
        <Box sx={{ display: 'flex', gap: 1, mt: 1.5, alignItems: 'center' }}>
          <TextField
            size="small"
            fullWidth
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void submitInput();
              }
            }}
            placeholder={sessionId ? `输入命令，Enter 发送（${sessionShell || caps?.shell || 'shell'}）` : '请先打开终端'}
            disabled={!sessionId || sending || !running}
            sx={{
              '& input': {
                fontSize: '12.5px',
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
              },
            }}
            inputProps={{ 'aria-label': '终端命令输入' }}
          />
          <Button
            size="small"
            variant="contained"
            disabled={!sessionId || sending || !running}
            onClick={() => void submitInput()}
            sx={{ textTransform: 'none', bgcolor: '#0D9488', '&:hover': { bgcolor: '#0F766E' } }}
          >
            发送
          </Button>
        </Box>
        <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.75 }}>
          管道模式：不支持 vim/top 等全屏交互程序，也没有 TTY。涉及删除、覆盖、提权的命令请先确认再执行。
        </Typography>

        {/* AI 命令建议 */}
        <Box
          sx={{
            mt: 3,
            p: 2,
            borderRadius: '14px',
            border: '1px solid var(--tt-border)',
            bgcolor: 'var(--tt-surface)',
          }}
        >
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, mb: 0.5 }}>
            <AutoAwesomeIcon sx={{ fontSize: 18, color: '#7C3AED' }} />
            <Typography sx={{ fontSize: '16px', fontWeight: 700, color: 'var(--tt-text-1)' }}>
              AI 命令建议
            </Typography>
            <Box sx={{ px: 1, py: 0.2, borderRadius: '6px', bgcolor: 'rgba(124, 58, 237, 0.1)' }}>
              <Typography sx={{ fontSize: '11px', fontWeight: 600, color: '#6D28D9' }}>
                只建议，不执行
              </Typography>
            </Box>
          </Box>
          <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3)', mb: 1.25 }}>
            模型看到 shell 名称、当前目录与最近输出（已去除 ANSI）。命令由你逐条确认后才会写入终端；
            输出可能包含敏感信息，是否发送取决于「PII 出站脱敏」档位。
          </Typography>
          <Box sx={{ display: 'flex', gap: 1, alignItems: 'center' }}>
            <TextField
              size="small"
              fullWidth
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  void askAi();
                }
              }}
              placeholder="例如：这个报错是什么意思？下一步该跑什么命令？"
              disabled={!sessionId || aiLoading}
              inputProps={{ 'aria-label': 'AI 建议问题' }}
              sx={{ '& input': { fontSize: '12.5px' } }}
            />
            <Button
              size="small"
              variant="contained"
              startIcon={aiLoading ? <CircularProgress size={14} sx={{ color: '#fff' }} /> : <AutoAwesomeIcon />}
              disabled={!sessionId || aiLoading || !question.trim()}
              onClick={() => void askAi()}
              sx={{ textTransform: 'none', bgcolor: '#7C3AED', '&:hover': { bgcolor: '#6D28D9' } }}
            >
              {aiLoading ? '正在分析…' : '获取建议'}
            </Button>
          </Box>
          {!sessionId && (
            <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mt: 1 }}>
              打开终端后才能根据当前输出给出建议。
            </Typography>
          )}
          {aiError && (
            <Typography sx={{ fontSize: '12.5px', color: '#B91C1C', mt: 1 }}>{aiError}</Typography>
          )}
          {aiResult && (
            <Box sx={{ mt: 1.5 }}>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                <Typography sx={{ fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-1)' }}>
                  {aiResult.suggestion.summary || '模型没有给出摘要'}
                </Typography>
                <Box
                  sx={{
                    px: 1,
                    py: 0.2,
                    borderRadius: '6px',
                    bgcolor: `${riskColor[aiResult.suggestion.risk]}14`,
                  }}
                >
                  <Typography sx={{ fontSize: '11px', fontWeight: 600, color: riskColor[aiResult.suggestion.risk] }}>
                    {riskLabel[aiResult.suggestion.risk]}
                  </Typography>
                </Box>
              </Box>
              {aiResult.suggestion.parseError && (
                <Typography sx={{ fontSize: '12px', color: '#B45309', mt: 0.75 }}>
                  {aiResult.suggestion.parseError}
                </Typography>
              )}
              {aiResult.redactions.length > 0 && (
                <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.5 }}>
                  本次出站已脱敏：{aiResult.redactions.join('、')} · 模型 {aiResult.model}
                </Typography>
              )}
              {aiResult.suggestion.commands.length === 0 ? (
                <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3)', mt: 1 }}>
                  没有可直接执行的命令建议（模型不确定时不会编造）。
                </Typography>
              ) : (
                <Box sx={{ mt: 1 }}>
                  {aiResult.suggestion.commands.map((cmd, i) => (
                    <Box
                      key={`${i}-${cmd.command}`}
                      sx={{
                        p: 1.25,
                        mb: 1,
                        borderRadius: '10px',
                        border: '1px solid var(--tt-border)',
                        bgcolor: 'var(--tt-surface-2)',
                      }}
                    >
                      <Box
                        component="pre"
                        sx={{
                          m: 0,
                          p: 1,
                          borderRadius: '8px',
                          bgcolor: '#0B1220',
                          color: '#E5E7EB',
                          fontSize: '12px',
                          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                          whiteSpace: 'pre-wrap',
                          wordBreak: 'break-all',
                        }}
                      >
                        {cmd.command}
                      </Box>
                      {cmd.explanation && (
                        <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', mt: 0.75 }}>
                          {cmd.explanation}
                        </Typography>
                      )}
                      <Box sx={{ display: 'flex', gap: 1, mt: 0.75, flexWrap: 'wrap' }}>
                        <Button
                          size="small"
                          variant="outlined"
                          startIcon={<ContentCopyIcon />}
                          onClick={() => void copyCommand(cmd.command)}
                          sx={{ textTransform: 'none' }}
                        >
                          复制
                        </Button>
                        <Button
                          size="small"
                          variant="outlined"
                          disabled={!running}
                          onClick={() => setInput(cmd.command)}
                          sx={{ textTransform: 'none' }}
                        >
                          填入输入框
                        </Button>
                        <Button
                          size="small"
                          variant="contained"
                          color="warning"
                          disabled={!running || sending}
                          onClick={() => setPendingCommand(cmd)}
                          sx={{ textTransform: 'none' }}
                        >
                          执行…
                        </Button>
                      </Box>
                    </Box>
                  ))}
                </Box>
              )}
            </Box>
          )}
        </Box>

        {/* 能力边界 */}
        <Typography sx={{ fontSize: '16px', fontWeight: 700, color: 'var(--tt-text-1)', mt: 3, mb: 0.75 }}>
          能力边界
        </Typography>
        <Box
          sx={{
            bgcolor: 'var(--tt-surface)',
            border: '1px solid var(--tt-border)',
            borderRadius: '14px',
            p: 2,
            mb: 2,
          }}
        >
          {[
            '终端 AI+ 默认关闭，开启是高风险管理动作：命令以你的系统权限执行，应用不会替你判断命令是否安全',
            '管道模式没有 TTY：vim/top/ssh 交互等全屏程序不可用；需要交互输入的命令可能一直等待',
            '输出保留最近 20 万字符；更早内容会被环形缓冲覆盖，界面会提示',
            'AI 只读取最近输出并给建议，永不自行写入；执行必须由你点击确认，来源会写入审计',
            '子 Agent 不继承终端能力；外部程序拿不到终端授权令牌',
            'ACP 委派让模型把编码子任务交给外部 Agent（如 Codex/Claude Code 的 ACP 服务），文件读写限制在该工作目录内',
          ].map((item) => (
            <Typography key={item} sx={{ fontSize: '12.5px', color: 'var(--tt-text-2)', mb: 0.75, lineHeight: 1.65 }}>
              · {item}
            </Typography>
          ))}
        </Box>
      </Box>

      <Dialog open={Boolean(pendingCommand)} onClose={() => setPendingCommand(null)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ fontSize: '16px', fontWeight: 700 }}>执行 AI 建议的命令？</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ fontSize: '13px', lineHeight: 1.8, mb: 1 }}>
            该命令会把以下内容写入当前 shell 并追加回车。请确认命令与当前目录符合你的意图；
            执行来源会记为「AI 建议」并写入安全审计。
          </DialogContentText>
          <Box
            component="pre"
            sx={{
              m: 0,
              p: 1.25,
              borderRadius: '8px',
              bgcolor: '#0B1220',
              color: '#E5E7EB',
              fontSize: '12.5px',
              fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
            }}
          >
            {pendingCommand?.command}
          </Box>
          {pendingCommand?.explanation && (
            <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3)', mt: 1 }}>
              {pendingCommand.explanation}
            </Typography>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setPendingCommand(null)} sx={{ textTransform: 'none' }}>
            取消
          </Button>
          <Button
            variant="contained"
            color="warning"
            onClick={() => void confirmExecute()}
            sx={{ textTransform: 'none' }}
          >
            确认执行
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

export default TerminalPanel;
