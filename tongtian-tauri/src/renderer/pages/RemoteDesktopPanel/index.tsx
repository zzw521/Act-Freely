/**
 * 通天办公 - 远程桌面（本机实时画面）
 *
 * 事实来源：
 * - 平台/帧间隔/会话上限/跨主机边界来自 `remote:capabilities`
 * - 会话状态来自 Rust（显式开启后签发 10 分钟会话，到期自动结束）
 * - 画面来自 Rust 截屏，只在内存中回传当前窗口，不落盘
 *
 * 不发生的事也写清楚：只看不控、不做跨主机转发、浏览器/Mock 通道没有执行器；
 * 不把「会话开启」说成「可以远程操作另一台电脑」。
 */

import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Paper,
  Stack,
  Typography,
  useTheme,
} from '@mui/material';
import DesktopWindowsIcon from '@mui/icons-material/DesktopWindows';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import StopIcon from '@mui/icons-material/Stop';
import RefreshIcon from '@mui/icons-material/Refresh';
import VisibilityIcon from '@mui/icons-material/Visibility';
import LockOutlinedIcon from '@mui/icons-material/LockOutlined';
import DevicesOtherIcon from '@mui/icons-material/DevicesOther';
import {
  fetchRemoteDesktopFrame,
  getComputerUseCapabilities,
  getRemoteDesktopCapabilities,
  getRemoteDesktopStatus,
  startRemoteDesktop,
  stopRemoteDesktop,
} from '../../lib/alignment-api';
import type {
  ComputerUseCapabilities,
  RemoteDesktopCapabilities,
  RemoteDesktopSession,
} from '../../../shared/contracts';
import { errorTextOf } from '../../../shared/error-messages';

const POLL_FALLBACK_MS = 750;

const EMPTY_SESSION: RemoteDesktopSession = {
  active: false,
  startedAtMs: 0,
  expiresAtMs: 0,
  remainingMs: 0,
  frames: 0,
  frameIntervalMs: 700,
  sessionMaxMs: 10 * 60 * 1000,
};

const policyLabel = (policy: ComputerUseCapabilities['policy'] | undefined): string => {
  switch (policy) {
    case 'auto':
      return '自动执行（开启时不再逐次确认）';
    case 'disabled':
      return '禁用（无法开启）';
    case 'ask_every_time':
      return '每次询问（开启时弹系统确认框）';
    default:
      return '读取中…';
  }
};

const formatRemaining = (ms: number): string => {
  const sec = Math.max(0, Math.ceil(ms / 1000));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
};

const boundaryNotes = [
  '只看不控：本页面没有键鼠注入通道，不能通过它操作本机；AI 操控仍走「电脑操控」的逐次授权链路',
  '不跨主机：当前版本不提供跨机器画面转发；远程转发需要中继服务与主机配对，尚未提供',
  '会话上限 10 分钟：到期自动结束，继续查看必须重新开启并再次确认',
  '画面只在当前窗口内存中显示，不写盘、不进审计；审计只记开始、结束、到期与帧数',
];

const RemoteDesktopPanel: React.FC = () => {
  const theme = useTheme();
  const [caps, setCaps] = useState<RemoteDesktopCapabilities | null>(null);
  const [computer, setComputer] = useState<ComputerUseCapabilities | null>(null);
  const [session, setSession] = useState<RemoteDesktopSession>(EMPTY_SESSION);
  const [frameSrc, setFrameSrc] = useState('');
  const [frameSize, setFrameSize] = useState<{ width: number; height: number } | null>(null);
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState('');
  const [, setTick] = useState(0);
  const pollTimer = useRef<number | null>(null);
  const polling = useRef(false);
  const mounted = useRef(true);

  const clearPollTimer = useCallback(() => {
    if (pollTimer.current !== null) {
      window.clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  const stopPolling = useCallback(() => {
    polling.current = false;
    clearPollTimer();
  }, [clearPollTimer]);

  const pollFrame = useCallback(async () => {
    if (!polling.current) return;
    let keepGoing = true;
    try {
      const f = await fetchRemoteDesktopFrame();
      // 结束会话后仍可能有在途帧返回：以 polling 开关为准，避免画面“复活”
      if (!mounted.current || !polling.current) return;
      setSession(f.session);
      if (!f.throttled && f.imageBase64) {
        setFrameSrc(`data:${f.mime ?? 'image/png'};base64,${f.imageBase64}`);
        if (typeof f.width === 'number' && typeof f.height === 'number') {
          setFrameSize({ width: f.width, height: f.height });
        }
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.startsWith('REMOTE_SESSION_EXPIRED') || msg.startsWith('REMOTE_NO_SESSION')) {
        keepGoing = false;
        setSession(EMPTY_SESSION);
        setFrameSrc('');
        setFrameSize(null);
      }
      if (mounted.current) setError(errorTextOf(e, '远程桌面画面读取失败'));
    } finally {
      if (keepGoing && polling.current && mounted.current) {
        pollTimer.current = window.setTimeout(() => void pollFrame(), POLL_FALLBACK_MS);
      }
    }
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [cap, current, computerCaps] = await Promise.all([
        getRemoteDesktopCapabilities(),
        getRemoteDesktopStatus(),
        getComputerUseCapabilities().catch(() => null),
      ]);
      if (!mounted.current) return;
      setCaps(cap);
      setComputer(computerCaps);
      setSession(current);
      setError('');
      if (current.active) {
        stopPolling();
        polling.current = true;
        void pollFrame();
      } else {
        stopPolling();
        setFrameSrc('');
        setFrameSize(null);
      }
    } catch (e) {
      if (mounted.current) setError(errorTextOf(e, '远程桌面能力读取失败'));
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, [pollFrame, stopPolling]);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => {
      mounted.current = false;
      stopPolling();
      // 离开页面即结束会话：画面不应在用户看不到的窗口里继续被截取
      void stopRemoteDesktop().catch(() => undefined);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!session.active) return undefined;
    const timer = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(timer);
  }, [session.active]);

  const handleStart = async () => {
    if (starting || session.active) return;
    setStarting(true);
    setError('');
    try {
      const r = await startRemoteDesktop();
      if (!mounted.current) return;
      setSession(r.session);
      setFrameSrc('');
      setFrameSize(null);
      polling.current = true;
      void pollFrame();
    } catch (e) {
      if (mounted.current) setError(errorTextOf(e, '远程桌面开启失败'));
      // 确认被取消或档位禁用后，重新读一次状态，避免页面停在「已开启」的错觉里
      void getRemoteDesktopStatus()
        .then((s) => mounted.current && setSession(s))
        .catch(() => undefined);
    } finally {
      if (mounted.current) setStarting(false);
    }
  };

  const handleStop = async () => {
    if (stopping) return;
    setStopping(true);
    stopPolling();
    try {
      const r = await stopRemoteDesktop();
      if (!mounted.current) return;
      setSession(r.session);
      setFrameSrc('');
      setFrameSize(null);
    } catch (e) {
      if (mounted.current) setError(errorTextOf(e, '结束远程桌面会话失败'));
    } finally {
      if (mounted.current) setStopping(false);
    }
  };

  const supported = Boolean(caps?.supported);
  const policyDisabled = computer?.policy === 'disabled';
  const canStart = supported && !policyDisabled;
  const remainingMs = session.active
    ? Math.max(0, session.expiresAtMs - Date.now())
    : 0;

  return (
    <Box>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: theme.spacing(3), flexWrap: 'wrap' }}>
        <Typography variant="h5" sx={{ fontWeight: 700 }}>
          远程桌面
        </Typography>
        <Typography sx={{ fontSize: 12.5, color: 'text.secondary' }}>
          本机桌面的受控实时画面：显式开启、10 分钟到期、只看不控
        </Typography>
        <Box sx={{ flexGrow: 1 }} />
        <Button
          size="small"
          startIcon={loading ? <CircularProgress size={14} /> : <RefreshIcon />}
          onClick={() => void refresh()}
          disabled={loading}
          sx={{ textTransform: 'none', fontSize: 12.5 }}
        >
          刷新
        </Button>
      </Box>

      {error && (
        <Alert severity="error" sx={{ mb: 2, borderRadius: '10px' }} onClose={() => setError('')}>
          {error}
        </Alert>
      )}

      {caps && !caps.supported && (
        <Alert severity="info" sx={{ mb: 2, borderRadius: '10px' }}>
          {caps.reason || '当前平台没有桌面截屏执行器，远程桌面不可用'}
        </Alert>
      )}

      {caps?.supported && policyDisabled && (
        <Alert severity="warning" sx={{ mb: 2, borderRadius: '10px' }}>
          电脑操控档位为「禁用」，远程桌面无法开启；请先在「电脑操控」页调整档位。
        </Alert>
      )}

      <Paper
        elevation={0}
        sx={{
          p: 2,
          mb: 2.5,
          border: `1px solid ${theme.palette.divider}`,
          borderRadius: '14px',
        }}
      >
        <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', rowGap: 1, mb: 1.5 }}>
          <Chip
            size="small"
            icon={<DesktopWindowsIcon sx={{ fontSize: 14 }} />}
            label={caps ? (caps.localView ? '本机画面可用' : '本机画面不可用') : '能力读取中'}
            sx={{
              height: 24,
              fontSize: 11.5,
              bgcolor: caps?.localView ? '#ECFDF5' : '#F3F4F6',
              color: caps?.localView ? '#047857' : '#6B7280',
              '& .MuiChip-icon': { color: 'inherit' },
            }}
          />
          <Chip
            size="small"
            label={`平台：${caps?.platform ?? '读取中'}`}
            sx={{ height: 24, fontSize: 11.5, bgcolor: '#F3F4F6', color: '#4B5563' }}
          />
          <Chip
            size="small"
            label={`会话上限：${Math.round((caps?.sessionMaxMs ?? 600000) / 60000)} 分钟`}
            sx={{ height: 24, fontSize: 11.5, bgcolor: '#F5F3FF', color: '#6D28D9' }}
          />
          <Chip
            size="small"
            label={`帧间隔：≥${caps?.frameIntervalMs ?? 700}ms`}
            sx={{ height: 24, fontSize: 11.5, bgcolor: '#EFF6FF', color: '#1D4ED8' }}
          />
          <Chip
            size="small"
            icon={<LockOutlinedIcon sx={{ fontSize: 14 }} />}
            label="只看不控"
            sx={{
              height: 24,
              fontSize: 11.5,
              bgcolor: '#FFF7ED',
              color: '#C2410C',
              '& .MuiChip-icon': { color: 'inherit' },
            }}
          />
        </Stack>

        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
          <Button
            variant="contained"
            size="small"
            disableElevation
            startIcon={starting ? <CircularProgress size={14} sx={{ color: '#fff' }} /> : <PlayArrowIcon />}
            onClick={() => void handleStart()}
            disabled={!canStart || starting || session.active}
            sx={{
              textTransform: 'none',
              borderRadius: '10px',
              bgcolor: '#111827',
              '&:hover': { bgcolor: '#1F2937' },
              '&.Mui-disabled': { bgcolor: '#E5E7EB', color: '#9CA3AF' },
            }}
          >
            {session.active ? '会话进行中' : starting ? '等待确认…' : '开启本机画面'}
          </Button>
          <Button
            variant="outlined"
            size="small"
            startIcon={stopping ? <CircularProgress size={14} /> : <StopIcon />}
            onClick={() => void handleStop()}
            disabled={!session.active || stopping}
            sx={{ textTransform: 'none', borderRadius: '10px', fontSize: 12.5 }}
          >
            结束会话
          </Button>
          <Typography sx={{ fontSize: 12.5, color: 'text.secondary' }}>
            截屏档位：{policyLabel(computer?.policy)}
          </Typography>
          {session.active && (
            <Chip
              size="small"
              label={`剩余 ${formatRemaining(remainingMs)} · 已取 ${session.frames} 帧`}
              sx={{ height: 24, fontSize: 11.5, bgcolor: '#ECFDF5', color: '#047857' }}
            />
          )}
        </Box>
        <Typography sx={{ fontSize: 12, color: 'text.secondary', mt: 1 }}>
          开启时会按当前电脑操控档位确认（「每次询问」档位弹出系统确认框）；取消确认即不会开始。
        </Typography>
      </Paper>

      <Paper
        elevation={0}
        sx={{
          p: 1.5,
          mb: 2.5,
          border: `1px solid ${theme.palette.divider}`,
          borderRadius: '16px',
          bgcolor: '#0B1220',
          minHeight: 300,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          position: 'relative',
          overflow: 'hidden',
        }}
      >
        {frameSrc ? (
          <Box
            component="img"
            src={frameSrc}
            alt="本机桌面实时画面"
            sx={{
              maxWidth: '100%',
              maxHeight: '70vh',
              borderRadius: '10px',
              display: 'block',
              boxShadow: '0 10px 40px rgba(0,0,0,0.35)',
            }}
          />
        ) : (
          <Box sx={{ textAlign: 'center', px: 3, py: 6 }}>
            <VisibilityIcon sx={{ fontSize: 46, color: '#334155', mb: 1 }} />
            <Typography sx={{ fontSize: 14, fontWeight: 600, color: '#CBD5E1' }}>
              {session.active ? '正在等待第一帧画面…' : '会话未开启，暂无画面'}
            </Typography>
            <Typography sx={{ fontSize: 12, color: '#64748B', mt: 0.5 }}>
              {supported
                ? '点击「开启本机画面」并完成确认后，画面只在这里实时显示'
                : '当前通道没有本机桌面截屏能力'}
            </Typography>
          </Box>
        )}
        {frameSrc && (
          <Box
            sx={{
              position: 'absolute',
              left: 12,
              bottom: 12,
              px: 1,
              py: 0.35,
              borderRadius: '6px',
              bgcolor: 'rgba(15, 23, 42, 0.72)',
              display: 'flex',
              alignItems: 'center',
              gap: 0.75,
            }}
          >
            <LockOutlinedIcon sx={{ fontSize: 12, color: '#94A3B8' }} />
            <Typography sx={{ fontSize: 10.5, color: '#CBD5E1' }}>
              仅本窗口可见 · 不写盘
              {frameSize ? ` · ${frameSize.width}×${frameSize.height}` : ''}
            </Typography>
          </Box>
        )}
      </Paper>

      <Paper
        elevation={0}
        sx={{
          p: 2,
          border: `1px solid ${theme.palette.divider}`,
          borderRadius: '14px',
        }}
      >
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1 }}>
          <DevicesOtherIcon sx={{ fontSize: 18, color: '#6B7280' }} />
          <Typography sx={{ fontSize: 13.5, fontWeight: 700 }}>能力边界</Typography>
          <Chip
            size="small"
            label={caps?.crossHost.supported ? '跨主机可用' : '跨主机未提供'}
            sx={{
              height: 22,
              fontSize: 11,
              bgcolor: caps?.crossHost.supported ? '#ECFDF5' : '#FEF2F2',
              color: caps?.crossHost.supported ? '#047857' : '#B91C1C',
            }}
          />
        </Box>
        <Stack component="ul" spacing={0.75} sx={{ m: 0, pl: 2.5 }}>
          {boundaryNotes.map((note) => (
            <Typography component="li" key={note} sx={{ fontSize: 12.5, color: 'text.secondary' }}>
              {note}
            </Typography>
          ))}
        </Stack>
        {caps && !caps.crossHost.supported && (
          <Typography sx={{ fontSize: 12, color: 'text.secondary', mt: 1.25 }}>
            {caps.crossHost.reason}
          </Typography>
        )}
      </Paper>
    </Box>
  );
};

export default RemoteDesktopPanel;
