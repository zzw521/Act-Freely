/**
 * 通天办公 - IM 频道页
 *
 * 数据真值只有一个来源：引擎 `im:channels`（桌面端由 Rust 代理 `/api/im/*`，
 * 浏览器引擎直连问同一个接口）。所以这里不存在「前端支持列表」——
 * 引擎注册了哪个通道、有没有实现长连接，界面就怎么显示。
 *
 * 绑定语义与引擎一致：提交凭据 → 引擎拿真实接口校验（Slack 走 auth.test +
 * apps.connections.open 两次往返）→ 通过才落盘并起连接，失败什么都不写。
 * 因此界面上没有「测试连接」这种独立假按钮，也没有需要额外承诺的绑定入口。
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogContent,
  DialogTitle,
  Divider,
  IconButton,
  Link,
  Snackbar,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import ChatIcon from '@mui/icons-material/Chat';
import SendIcon from '@mui/icons-material/Send';
import ChatBubbleIcon from '@mui/icons-material/ChatBubble';
import ForumIcon from '@mui/icons-material/Forum';
import HubIcon from '@mui/icons-material/Hub';
import CloseIcon from '@mui/icons-material/Close';
import OpenInNewIcon from '@mui/icons-material/OpenInNew';
import RefreshIcon from '@mui/icons-material/Refresh';
import SmartToyIcon from '@mui/icons-material/SmartToy';
import { QRCodeSVG } from 'qrcode.react';
import { errorTextOf } from '../../../shared/error-messages';
import type { ImChannelState, ImChannelView, ImLoginPoll } from '../../../shared/contracts';
import {
  configureImChannel,
  forgetImChannel,
  pollImLogin,
  startImChannel,
  startImLogin,
  stopImChannel,
} from '../../lib/alignment-api';
import { imChannelMeta, useImChannelStore } from '../../stores/imChannelStore';
import { getActiveWorkspaceId } from '../../store/workspaceStore';

// ── 状态展示 ────────────────────────────────────────────────────────

const STATE_META: Record<ImChannelState, { label: string; fg: string; bg: string }> = {
  not_configured: { label: '未绑定', fg: 'var(--tt-text-3)', bg: 'var(--tt-chip-neutral)' },
  idle: { label: '待启动', fg: 'var(--tt-text-3)', bg: 'var(--tt-chip-neutral)' },
  connecting: { label: '连接中', fg: 'var(--tt-warning)', bg: 'var(--tt-chip-warning)' },
  connected: { label: '已连接', fg: 'var(--tt-success)', bg: 'var(--tt-chip-success)' },
  reconnecting: { label: '重连中', fg: 'var(--tt-warning)', bg: 'var(--tt-chip-warning)' },
  failed: { label: '连接失败', fg: 'var(--tt-danger)', bg: 'var(--tt-chip-danger)' },
  stopped: { label: '已停止', fg: 'var(--tt-text-2)', bg: 'var(--tt-border-soft)' },
};

/** 通道图标按 kind 上色；引擎新增通道时落到中性图标，不需要前端同步改表 */
const ICONS: Record<string, { icon: React.ElementType; bg: string }> = {
  dingtalk: { icon: ChatIcon, bg: '#1677FF' },
  feishu: { icon: SendIcon, bg: '#3370FF' },
  lark: { icon: SendIcon, bg: '#00B578' },
  wechat: { icon: ChatBubbleIcon, bg: '#07C160' },
  wecom: { icon: ForumIcon, bg: '#2B7FFF' },
  yuanbao: { icon: SmartToyIcon, bg: '#0EA5E9' },
  slack: { icon: HubIcon, bg: '#611F69' },
};

/** 引擎 account 里的值是服务端下发的原始类型：裸布尔会被 React 直接丢掉，界面就成了「有标签没值」 */
function accountText(value: unknown): string {
  if (typeof value === 'boolean') return value ? '是' : '否';
  return String(value ?? '');
}

/** 引擎 account 里已知的账号事实字段名；未收录的键按原名展示，不做翻译式承诺 */
const ACCOUNT_LABELS: Record<string, string> = {
  team: '工作区',
  botUser: '机器人',
  appId: 'App ID',
  apiBase: '接口地址',
  appKey: 'AppKey（脱敏）',
  tokenExpireIn: 'Token 有效秒',
  botId: 'BotID（脱敏）',
  wsUrl: '长连接地址',
  heartbeatSeconds: '心跳秒',
  oneConnectionPerBot: '单连接限制',
  subscribeTopic: '订阅 Topic',
  subscribeEvent: '订阅事件',
  connectionId: '连接标识（脱敏）',
  pingIntervalSeconds: '保活间隔秒',
  directMessageOnly: '仅单聊',
  protocol: '接入协议',
  tokenSource: '令牌来源',
};

const timeOfText = (ms: number) =>
  new Date(ms).toLocaleString('zh-CN', { hour12: false }).replace(/\//g, '-');

/** 扫码登录弹窗的本地状态；二维码内容只存在内存里，关掉弹窗就丢 */
interface QrLoginState {
  kind: string;
  label: string;
  ticket: string;
  content: string;
  status: string;
  message: string;
  needVerifyCode: boolean;
}

// ── 页面组件 ──────────────────────────────────────────────────────

const IMChannel: React.FC = () => {
  const { channels, sessions, loading, loaded, error, loadSessions: loadSessionsRaw } =
    useImChannelStore(
      useShallow((s) => ({
        channels: s.channels,
        sessions: s.channelSessions,
        loading: s.loading,
        loaded: s.loaded,
        error: s.error,
        loadSessions: s.loadSessions,
      })),
    );

  // IM 数据只能由引擎进程提供，而引擎默认等到首次提交任务才拉起：
  // 直接打开本页时先确保当前工作空间的引擎在跑，否则页面只剩「引擎未运行」。
  const loadSessions = useCallback(
    () => loadSessionsRaw(getActiveWorkspaceId()),
    [loadSessionsRaw],
  );

  const [openKind, setOpenKind] = useState<string | null>(null);
  const [form, setForm] = useState<Record<string, string>>({});
  const [savingError, setSavingError] = useState('');
  const [busyKind, setBusyKind] = useState('');
  const [toast, setToast] = useState<{ text: string; severity: 'success' | 'info' } | null>(null);
  const [qr, setQr] = useState<QrLoginState | null>(null);
  const [verifyCode, setVerifyCode] = useState('');
  const verifyRef = useRef('');
  const pollTimer = useRef<number | null>(null);
  const pollRef = useRef<(kind: string, ticket: string) => void>(() => undefined);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  const stopPolling = useCallback(() => {
    if (pollTimer.current !== null) {
      window.clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  // 扫码状态只能问引擎（引擎持有二维码票据与服务端会话），前端不猜「扫没扫到」
  const pollQr = useCallback(
    async (kind: string, ticket: string) => {
      try {
        const res: ImLoginPoll = await pollImLogin(kind, ticket, verifyRef.current);
        if (res.status === 'confirmed') {
          stopPolling();
          setQr(null);
          setVerifyCode('');
          verifyRef.current = '';
          await loadSessions();
          setToast({ text: '已扫码绑定并连接', severity: 'success' });
          return;
        }
        const message =
          res.needVerifyCode && verifyRef.current
            ? '数字不正确，请重新输入手机微信显示的数字'
            : res.message;
        setQr((prev) =>
          prev && prev.ticket === ticket
            ? { ...prev, status: res.status, message, needVerifyCode: Boolean(res.needVerifyCode) }
            : prev,
        );
        // 需要配对码时停下等用户输入；继续空轮询只会把同一个状态再问一遍
        if (res.needVerifyCode || ['expired', 'verify_code_blocked', 'binded_redirect'].includes(res.status)) {
          stopPolling();
          return;
        }
        pollTimer.current = window.setTimeout(() => pollRef.current(kind, ticket), 1200);
      } catch (err) {
        stopPolling();
        setQr((prev) =>
          prev && prev.ticket === ticket
            ? { ...prev, status: 'failed', message: errorTextOf(err, '扫码状态查询失败') }
            : prev,
        );
      }
    },
    [loadSessions, stopPolling],
  );

  useEffect(() => {
    pollRef.current = (kind, ticket) => {
      void pollQr(kind, ticket);
    };
    return () => {
      stopPolling();
    };
  }, [pollQr, stopPolling]);

  const beginLogin = async (c: ImChannelView) => {
    setBusyKind(c.kind);
    setSavingError('');
    try {
      const res = await startImLogin(c.kind);
      setVerifyCode('');
      verifyRef.current = '';
      setQr({
        kind: c.kind,
        label: c.label,
        ticket: res.ticket,
        content: res.qrContent,
        status: 'wait',
        message: '等待扫码',
        needVerifyCode: false,
      });
      stopPolling();
      void pollQr(c.kind, res.ticket);
    } catch (err) {
      setToast({ text: errorTextOf(err, '生成二维码失败'), severity: 'info' });
    } finally {
      setBusyKind('');
    }
  };

  const submitVerifyCode = () => {
    if (!qr) return;
    verifyRef.current = verifyCode.trim();
    stopPolling();
    void pollQr(qr.kind, qr.ticket);
  };

  const closeQr = () => {
    stopPolling();
    setQr(null);
    setVerifyCode('');
    verifyRef.current = '';
  };

  // 数据刷新由常驻的侧边栏统一负责（见 components/Layout/Sidebar.tsx）：
  // 引擎持有长连接但不向界面推送事件，两处各自轮询会变成每 4s 问两遍同一个接口。

  const openChannel = useMemo(
    () => channels.find((c) => c.kind === openKind) ?? null,
    [channels, openKind],
  );

  const beginConfigure = (c: ImChannelView) => {
    setForm({});
    setSavingError('');
    setOpenKind(c.kind);
  };

  const submitConfigure = async () => {
    if (!openChannel) return;
    setBusyKind(openChannel.kind);
    setSavingError('');
    try {
      // 空输入不提交：引擎按「补丁叠加现值」合并，留空即沿用已登记的那一项
      const payload: Record<string, string> = {};
      Object.entries(form).forEach(([key, value]) => {
        if (value.trim()) payload[key] = value.trim();
      });
      const account = await configureImChannel(openChannel.kind, payload);
      setOpenKind(null);
      // 绑定即起连接，界面要立刻问一次引擎，否则卡片停在绑定前的旧状态
      await loadSessions();
      const who = account.team || account.botUser || '';
      setToast({ text: `已绑定并连接${who ? `：${who}` : ''}`, severity: 'success' });
    } catch (err) {
      setSavingError(errorTextOf(err, '绑定失败'));
    } finally {
      setBusyKind('');
    }
  };

  const runAction = async (kind: string, action: 'start' | 'stop' | 'forget') => {
    setBusyKind(kind);
    try {
      if (action === 'start') await startImChannel(kind);
      if (action === 'stop') await stopImChannel(kind);
      if (action === 'forget') {
        await forgetImChannel(kind);
        setOpenKind(null);
      }
      await loadSessions();
      setToast({
        text:
          action === 'start'
            ? '已请求建立长连接，状态以引擎返回为准'
            : action === 'stop'
              ? '已断开长连接，凭据保留'
              : '已解绑该频道；IM 会话历史保留在本地',
        severity: 'info',
      });
    } catch (err) {
      setToast({ text: errorTextOf(err, '操作失败'), severity: 'info' });
    } finally {
      setBusyKind('');
    }
  };

  const engineDown = !loading && loaded && error !== '';

  return (
    <Box sx={{ maxWidth: 860, mx: 'auto' }}>
      <Typography variant="h5" sx={{ fontWeight: 700, color: 'var(--tt-text-1)' }}>IM 频道</Typography>
      <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-3, #6B7280)', mt: 0.5, mb: 2 }}>
        把任我行接进 IM：机器人收到消息后在本地发起任务，结果回到原会话。
        凭据与会话台账只存在本机引擎数据目录，长连接由本地引擎进程持有，不上传云端。
      </Typography>

      <Alert severity="info" sx={{ mb: 2, fontSize: '12.5px', alignItems: 'flex-start' }}>
        Slack / 钉钉 / 企业微信 / 飞书 / 个人微信 / 腾讯元宝 的协议实现只在本地假服务端做过双向对拍，
        尚未用真实凭据做端到端验证；真实服务端是否可用，以绑定后引擎返回的状态为准。
        飞书首次绑定要等官方 SDK 冷启动，可能需要 8~33 秒，期间停在「连接中」属正常现象。
        个人微信走腾讯官方 iLink 机器人协议（手机扫码授权），只支持单聊、机器人不能主动发起会话，
        用户 24 小时没说话后回贴窗口失效；不使用任何非官方微信协议。
      </Alert>

      <Box sx={{ display: 'flex', justifyContent: 'flex-end', mb: 1.5 }}>
        <Button
          size="small"
          startIcon={loading ? <CircularProgress size={14} /> : <RefreshIcon sx={{ fontSize: 16 }} />}
          onClick={() => void loadSessions()}
          disabled={loading}
          sx={{ textTransform: 'none', fontSize: '13px', color: 'var(--tt-text-2, #4B5563)' }}
        >
          刷新状态
        </Button>
      </Box>

      {engineDown && (
        <Alert
          severity="warning"
          sx={{ mb: 2, fontSize: '13px', alignItems: 'flex-start' }}
          action={
            <Button size="small" onClick={() => void loadSessions()} sx={{ textTransform: 'none', fontSize: '12.5px' }}>
              重试
            </Button>
          }
        >
          {errorTextOf(error, 'IM 频道状态读取失败')}
        </Alert>
      )}

      {loading && channels.length === 0 && (
        <Alert severity="info" sx={{ mb: 2, fontSize: '13px' }}>
          正在读取频道状态；首次打开会先拉起本地引擎，可能需要几秒。
        </Alert>
      )}

      {!loading && !engineDown && loaded && channels.length === 0 && (
        <Alert severity="info" sx={{ mb: 2, fontSize: '13px' }}>
          引擎当前没有注册任何 IM 通道。
        </Alert>
      )}

      {/* 通道清单：完全来自引擎注册表，未实现长连接的通道也在列表里，但为禁用态 */}
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
        {channels.map((c) => {
          const meta = imChannelMeta(c.kind, c.label);
          const visual = ICONS[c.kind] ?? { icon: HubIcon, bg: meta.color };
          const Icon = visual.icon;
          const state = STATE_META[c.state];
          const busy = busyKind === c.kind;
          return (
            <Box
              key={c.kind}
              sx={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: 2,
                border: '1px solid var(--tt-border-soft, var(--tt-border))',
                borderRadius: '14px',
                p: 2,
                background: 'var(--tt-surface, #FFFFFF)',
                opacity: c.supported ? 1 : 0.72,
              }}
            >
              <Box
                sx={{
                  width: 40,
                  height: 40,
                  borderRadius: '10px',
                  background: c.supported ? visual.bg : 'var(--tt-text-4, #9CA3AF)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  flexShrink: 0,
                }}
              >
                <Icon sx={{ fontSize: 22, color: '#FFFFFF' }} />
              </Box>

              <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                  <Typography sx={{ fontSize: '14px', fontWeight: 600, color: 'var(--tt-text-1)' }}>{c.label}</Typography>
                  <Chip
                    label={c.supported ? state.label : '尚未接入'}
                    size="small"
                    sx={{
                      height: 20,
                      fontSize: '11.5px',
                      fontWeight: 600,
                      color: c.supported ? state.fg : 'var(--tt-text-3)',
                      background: c.supported ? state.bg : 'var(--tt-bg, #F3F4F6)',
                    }}
                  />
                  {c.configured && c.state === 'connected' && c.connectedAtMs && (
                    <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-3, #6B7280)' }}>
                      自 {timeOfText(c.connectedAtMs)}
                    </Typography>
                  )}
                  {c.reconnects > 0 && (
                    <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-3, #6B7280)' }}>
                      重连 {c.reconnects} 次
                    </Typography>
                  )}
                </Box>

                {!c.supported && (
                  <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3, #6B7280)', mt: 0.5 }}>
                    {c.unsupportedReason}
                  </Typography>
                )}

                {c.supported && c.configured && Object.keys(c.account).length > 0 && (
                  <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', mt: 0.75 }}>
                    {Object.entries(c.account).map(([key, value]) => (
                      <Typography key={key} sx={{ fontSize: '12px', color: 'var(--tt-text-3, #6B7280)' }}>
                        {ACCOUNT_LABELS[key] ?? key}：{accountText(value)}
                      </Typography>
                    ))}
                  </Box>
                )}

                {c.supported && !c.configured && (
                  <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3, #6B7280)', mt: 0.5 }}>
                    {c.setupNote}
                  </Typography>
                )}

                {c.lastError && (
                  <Typography sx={{ fontSize: '12px', color: 'var(--tt-danger)', mt: 0.75 }}>
                    最近错误：{errorTextOf(`${c.lastError.code}: ${c.lastError.detail}`, c.lastError.code)}
                    （{timeOfText(c.lastError.atMs)}）
                  </Typography>
                )}

                {c.sessions > 0 && (
                  <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3, #6B7280)', mt: 0.5 }}>
                    本频道会话 {c.sessions} 个
                  </Typography>
                )}
              </Box>

              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexShrink: 0 }}>
                {!c.supported ? (
                  <Tooltip title={c.unsupportedReason} placement="left">
                    <span>
                      <Button size="small" disabled sx={{ textTransform: 'none', fontSize: '13px' }}>
                        配置
                      </Button>
                    </span>
                  </Tooltip>
                ) : (
                  <>
                    {c.configured && (c.state === 'connected' || c.state === 'connecting' || c.state === 'reconnecting') ? (
                      <Button
                        size="small"
                        variant="outlined"
                        disabled={busy}
                        onClick={() => void runAction(c.kind, 'stop')}
                        sx={{ textTransform: 'none', fontSize: '13px', borderColor: 'var(--tt-border, #E5E7EB)' }}
                      >
                        {busy ? '处理中' : '断开'}
                      </Button>
                    ) : (
                      <Button
                        size="small"
                        variant="outlined"
                        disabled={busy || !c.configured}
                        onClick={() => void runAction(c.kind, 'start')}
                        sx={{ textTransform: 'none', fontSize: '13px', borderColor: 'var(--tt-border, #E5E7EB)' }}
                      >
                        {busy ? '处理中' : '启动连接'}
                      </Button>
                    )}
                    <Button
                      size="small"
                      variant="contained"
                      disableElevation
                      disabled={busy}
                      onClick={() => (c.loginMode === 'qr' ? void beginLogin(c) : beginConfigure(c))}
                      sx={{
                        textTransform: 'none',
                        fontSize: '13px',
                        fontWeight: 600,
                        borderRadius: '10px',
                        bgcolor: 'var(--tt-cta-fill)',
                        '&:hover': { bgcolor: 'var(--tt-cta-fill-hover)' },
                      }}
                    >
                      {busy
                        ? '处理中'
                        : c.loginMode === 'qr'
                          ? c.configured
                            ? '重新扫码'
                            : '扫码绑定'
                          : c.configured
                            ? '改凭据'
                            : '配置凭据'}
                    </Button>
                    {c.configured && (
                      <Button
                        size="small"
                        disabled={busy}
                        onClick={() => void runAction(c.kind, 'forget')}
                        sx={{ textTransform: 'none', fontSize: '13px', color: 'var(--tt-text-3, #6B7280)' }}
                      >
                        解绑
                      </Button>
                    )}
                  </>
                )}
              </Box>
            </Box>
          );
        })}
      </Box>

      {/* IM 发起的会话：引擎台账真实记录，没有就如实空 */}
      <Box sx={{ mt: 4 }}>
        <Typography sx={{ fontSize: '14px', fontWeight: 700, mb: 1, color: 'var(--tt-text-1)' }}>IM 发起的会话</Typography>
        <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3, #6B7280)', mb: 1.5 }}>
          从 IM 收到的消息会在本地发起任务，入站原文与回贴内容写进引擎侧的会话台账。
        </Typography>
        {sessions.length === 0 ? (
          <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-3, #6B7280)', py: 2 }}>
            还没有来自 IM 的消息。绑定并连上频道后，从会话里 @机器人即可触发任务。
          </Typography>
        ) : (
          <Box sx={{ border: '1px solid var(--tt-border-soft, var(--tt-border))', borderRadius: '14px', overflow: 'hidden' }}>
            {sessions.map((s, i) => {
              const meta = imChannelMeta(s.source, channels.find((c) => c.kind === s.source)?.label);
              return (
                <Box key={s.id}>
                  {i > 0 && <Divider />}
                  <Box sx={{ px: 2, py: 1.5, display: 'flex', alignItems: 'center', gap: 1.5 }}>
                    <Box
                      component="span"
                      sx={{ width: 7, height: 7, borderRadius: '50%', background: meta.color, flexShrink: 0 }}
                    />
                    <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                      <Typography sx={{ fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-1)' }}>
                        {s.title || '（无标题）'}
                      </Typography>
                      <Typography
                        sx={{
                          fontSize: '11.5px',
                          color: 'var(--tt-text-3, #6B7280)',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }}
                      >
                        {meta.label} · {timeOfText(s.updatedAt)}
                      </Typography>
                    </Box>
                    <Chip
                      label={`${s.messageCount} 条消息`}
                      size="small"
                      sx={{ height: 20, fontSize: '11px', bgcolor: 'var(--tt-bg, #F3F4F6)' }}
                    />
                  </Box>
                </Box>
              );
            })}
          </Box>
        )}
      </Box>

      {/* 扫码绑定：二维码内容由引擎生成，前端只负责画出来；bot_token 从不经过界面 */}
      {qr && (
        <Dialog
          open
          onClose={closeQr}
          PaperProps={{ sx: { width: 400, borderRadius: '16px', p: 1, position: 'relative' } }}
        >
          <IconButton
            aria-label="close"
            onClick={closeQr}
            sx={{ position: 'absolute', right: 10, top: 10, color: 'var(--tt-text-4)', zIndex: 1 }}
          >
            <CloseIcon sx={{ fontSize: 18 }} />
          </IconButton>
          <DialogTitle sx={{ pb: 0.5 }}>
            <Typography sx={{ fontSize: '16px', fontWeight: 700, color: 'var(--tt-text-1)' }}>
              扫码绑定 {qr.label}
            </Typography>
            <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3, #6B7280)', mt: 0.5, lineHeight: 1.7 }}>
              用手机微信扫码并按提示确认。二维码只在本地引擎内存里生成，关闭弹窗即失效；
              确认后登录凭据由引擎直接落盘（0600），界面不显示也不回传。
            </Typography>
          </DialogTitle>
          <DialogContent sx={{ pt: '12px !important' }}>
            {qr.status === 'failed' || qr.status === 'expired' || qr.status === 'verify_code_blocked' || qr.status === 'binded_redirect' ? (
              <Alert
                severity={qr.status === 'failed' || qr.status === 'verify_code_blocked' ? 'error' : 'warning'}
                sx={{ fontSize: '12.5px', alignItems: 'flex-start', mb: 1 }}
              >
                {qr.message}
              </Alert>
            ) : (
              <Box sx={{ display: 'flex', justifyContent: 'center', my: 1 }}>
                <Box
                  sx={{
                    p: 1.5,
                    // 亮色孤岛：二维码必须固定白底，深色皮肤下也要能被扫码器识别
                    bgcolor: '#FFFFFF',
                    border: '1px solid var(--tt-border, #E5E7EB)',
                    borderRadius: '12px',
                  }}
                >
                  <QRCodeSVG value={qr.content} size={200} level="M" />
                </Box>
              </Box>
            )}

            <Typography
              sx={{
                fontSize: '13px',
                textAlign: 'center',
                color: qr.status === 'scaned' || qr.status === 'need_verifycode' ? 'var(--tt-warning)' : 'var(--tt-text-2)',
              }}
            >
              {qr.message}
            </Typography>

            {qr.needVerifyCode && (
              <Box sx={{ display: 'flex', gap: 1, mt: 2 }}>
                <TextField
                  size="small"
                  fullWidth
                  autoFocus
                  value={verifyCode}
                  onChange={(e) => setVerifyCode(e.target.value.replace(/\D/g, ''))}
                  placeholder="手机微信上显示的数字"
                  sx={{ '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
                />
                <Button
                  variant="contained"
                  disableElevation
                  disabled={!verifyCode.trim()}
                  onClick={submitVerifyCode}
                  sx={{
                    bgcolor: 'var(--tt-cta-fill)',
                    borderRadius: '10px',
                    textTransform: 'none',
                    fontSize: '13px',
                    fontWeight: 600,
                    '&:hover': { bgcolor: 'var(--tt-cta-fill-hover)' },
                  }}
                >
                  提交
                </Button>
              </Box>
            )}

            <Box sx={{ display: 'flex', justifyContent: 'flex-end', gap: 1, mt: 2, mb: 1 }}>
              {['expired', 'verify_code_blocked', 'failed', 'binded_redirect'].includes(qr.status) && (
                <Button
                  variant="outlined"
                  size="small"
                  disabled={busyKind === qr.kind}
                  onClick={() => {
                    const target = channels.find((c) => c.kind === qr.kind);
                    if (target) void beginLogin(target);
                  }}
                  sx={{ textTransform: 'none', fontSize: '13px', borderColor: 'var(--tt-border, #E5E7EB)' }}
                >
                  重新生成
                </Button>
              )}
              <Button
                onClick={closeQr}
                sx={{ textTransform: 'none', fontSize: '13px', color: 'var(--tt-text-2, #4B5563)' }}
              >
                关闭
              </Button>
            </Box>
          </DialogContent>
        </Dialog>
      )}

      {/* 凭据弹窗：字段清单由引擎下发，界面不写死「哪个 IM 要什么」 */}
      {openChannel && (
        <Dialog
          open
          onClose={() => setOpenKind(null)}
          PaperProps={{ sx: { width: 540, borderRadius: '16px', p: 1, position: 'relative' } }}
        >
          <IconButton
            aria-label="close"
            onClick={() => setOpenKind(null)}
            sx={{ position: 'absolute', right: 10, top: 10, color: 'var(--tt-text-4)', zIndex: 1 }}
          >
            <CloseIcon sx={{ fontSize: 18 }} />
          </IconButton>
          <DialogTitle sx={{ pb: 0.5 }}>
            <Typography sx={{ fontSize: '16px', fontWeight: 700, color: 'var(--tt-text-1)' }}>配置 {openChannel.label}</Typography>
            <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3, #6B7280)', mt: 0.5, lineHeight: 1.7 }}>
              {openChannel.setupNote}
            </Typography>
            {openChannel.setupUrl && (
              <Link
                href={openChannel.setupUrl}
                target="_blank"
                rel="noreferrer"
                sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.5, fontSize: '12.5px', mt: 0.75 }}
              >
                打开 {openChannel.label} 应用后台
                <OpenInNewIcon sx={{ fontSize: 14 }} />
              </Link>
            )}
          </DialogTitle>
          <DialogContent sx={{ pt: '12px !important' }}>
            {openChannel.fields.map((f) => (
              <Box key={f.key} sx={{ mb: 2 }}>
                <Typography sx={{ fontSize: '12.5px', fontWeight: 600, mb: 0.75 }}>
                  {f.label}
                  {f.required && <Box component="span" sx={{ color: 'var(--tt-danger)' }}> *</Box>}
                  {f.present && (
                    <Box component="span" sx={{ color: 'var(--tt-text-3, #9CA3AF)', fontWeight: 400, ml: 1 }}>
                      已登记
                    </Box>
                  )}
                </Typography>
                <TextField
                  type="password"
                  value={form[f.key] ?? ''}
                  onChange={(e) => setForm((prev) => ({ ...prev, [f.key]: e.target.value }))}
                  fullWidth
                  size="small"
                  autoComplete="off"
                  placeholder={f.present ? '留空即沿用已登记的值' : f.placeholder}
                  sx={{ '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
                  helperText={f.note}
                />
                {f.helpUrl && (
                  <Link href={f.helpUrl} target="_blank" rel="noreferrer" sx={{ fontSize: '11.5px' }}>
                    查看这一项在哪里获取
                  </Link>
                )}
              </Box>
            ))}

            {savingError && (
              <Alert severity="error" sx={{ mb: 1.5, fontSize: '12.5px', alignItems: 'flex-start' }}>
                {savingError}
              </Alert>
            )}

            <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-3, #9CA3AF)', lineHeight: 1.7, mb: 1.5 }}>
              凭据只写入本机引擎数据目录（0600 权限），接口与日志一律脱敏，不会回显明文。
              保存时引擎会真实调用 {openChannel.label} 的接口校验，校验不通过不会落盘，也不会起连接。
            </Typography>

            <Box sx={{ display: 'flex', justifyContent: 'flex-end', gap: 1, mt: 1, mb: 1 }}>
              <Button
                onClick={() => setOpenKind(null)}
                sx={{ textTransform: 'none', fontSize: '13px', color: 'var(--tt-text-2, #4B5563)' }}
              >
                取消
              </Button>
              <Button
                variant="contained"
                disableElevation
                disabled={busyKind === openChannel.kind}
                onClick={() => void submitConfigure()}
                sx={{
                  bgcolor: 'var(--tt-cta-fill)',
                  borderRadius: '10px',
                  textTransform: 'none',
                  fontSize: '13px',
                  fontWeight: 700,
                  px: 3,
                  '&:hover': { bgcolor: 'var(--tt-cta-fill-hover)' },
                }}
              >
                {busyKind === openChannel.kind ? '校验中…' : '校验并绑定'}
              </Button>
            </Box>
          </DialogContent>
        </Dialog>
      )}

      <Snackbar open={!!toast} autoHideDuration={4000} onClose={() => setToast(null)} anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity={toast?.severity ?? 'info'} onClose={() => setToast(null)} sx={{ fontSize: '13px' }}>
          {toast?.text}
        </Alert>
      </Snackbar>
    </Box>
  );
};

export default IMChannel;
