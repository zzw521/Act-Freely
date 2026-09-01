/**
 * 通天办公 - IM 频道页（设计图对齐版）
 *
 * 结构：
 * - 页头：标题 + 说明（配置信息仅存本地，不上传云端）
 * - 平台列表：钉钉/飞书/Lark/微信/企业微信/Slack/WhatsApp，各带「配置」入口
 * - 配置弹窗：
 *   - 扫码类（钉钉/飞书/Lark/微信/企业微信/WhatsApp）：二维码占位 + 刷新
 *   - 钉钉/企业微信：快捷绑定（推荐）/ 手动配置 双模式
 *   - Slack：Bot Token / App-Level Token 表单 + 准入策略（配对/开放）
 *
 * Mock 开关规范：
 * - 开发阶段（VITE_MOCK !== 'false'）：二维码为本地占位渲染，保存仅模拟提示
 * - 联调阶段（VITE_MOCK === 'false'）：预留真实渠道接入位（二维码下发/Token 校验）
 */

import React, { useMemo, useState } from 'react';
import {
  Box,
  Typography,
  Button,
  Dialog,
  DialogTitle,
  DialogContent,
  TextField,
  Snackbar,
  Alert,
  IconButton,
} from '@mui/material';
import ChatIcon from '@mui/icons-material/Chat';
import SendIcon from '@mui/icons-material/Send';
import ChatBubbleIcon from '@mui/icons-material/ChatBubble';
import ForumIcon from '@mui/icons-material/Forum';
import HubIcon from '@mui/icons-material/Hub';
import WhatsAppIcon from '@mui/icons-material/WhatsApp';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import CloseIcon from '@mui/icons-material/Close';

const mockEnabled = import.meta.env.VITE_MOCK !== 'false';

// ── 平台模型 ──────────────────────────────────────────────────────

type ChannelId = 'dingtalk' | 'feishu' | 'lark' | 'wechat' | 'wecom' | 'slack' | 'whatsapp';

interface ChannelDef {
  id: ChannelId;
  name: string;
  desc: string;
  icon: React.ElementType;
  iconColor: string;
  iconBg: string;
}

const CHANNELS: ChannelDef[] = [
  { id: 'dingtalk', name: '钉钉', desc: '通过钉钉机器人接收并回复用户消息', icon: ChatIcon, iconColor: '#FFFFFF', iconBg: '#1677FF' },
  { id: 'feishu', name: '飞书', desc: '通过飞书机器人接收并回复用户消息', icon: SendIcon, iconColor: '#FFFFFF', iconBg: '#3370FF' },
  { id: 'lark', name: 'Lark', desc: '通过 Lark 机器人接收并回复用户消息', icon: SendIcon, iconColor: '#FFFFFF', iconBg: '#00B578' },
  { id: 'wechat', name: '微信', desc: '通过微信机器人接收并回复用户消息', icon: ChatBubbleIcon, iconColor: '#FFFFFF', iconBg: '#07C160' },
  { id: 'wecom', name: '企业微信', desc: '通过企业微信机器人接收并回复用户消息', icon: ForumIcon, iconColor: '#FFFFFF', iconBg: '#2B7FFF' },
  { id: 'slack', name: 'Slack', desc: '通过 Socket Mode 接收和回复 Slack 消息', icon: HubIcon, iconColor: '#FFFFFF', iconBg: '#611F69' },
  { id: 'whatsapp', name: 'WhatsApp', desc: '通过 WhatsApp 多设备协议接收并回复用户消息', icon: WhatsAppIcon, iconColor: '#FFFFFF', iconBg: '#25D366' },
];

/** 扫码类弹窗文案（Slack 为表单弹窗，单独处理） */
const QR_DIALOGS: Record<Exclude<ChannelId, 'slack'>, {
  title: string;
  subtitle: string;
  /** 是否展示「快捷绑定（推荐）/ 手动配置」双模式 */
  dualMode?: boolean;
  refreshLabel: string;
  /** 刷新按钮是否黑色实心（微信为黑色） */
  darkRefresh?: boolean;
}> = {
  dingtalk: { title: '配置钉钉', subtitle: '在钉钉中扫码完成应用注册', dualMode: true, refreshLabel: '刷新二维码' },
  feishu: { title: '配置飞书', subtitle: '使用飞书扫描下方二维码，自动创建并配置机器人', refreshLabel: '刷新二维码' },
  lark: { title: '配置 Lark', subtitle: '使用 Lark 扫描下方二维码，自动创建并配置机器人', refreshLabel: '刷新二维码' },
  wechat: { title: '扫码登录', subtitle: '请使用微信扫描下方二维码完成连接', refreshLabel: '重新生成', darkRefresh: true },
  wecom: { title: '配置企业微信', subtitle: '选择一种方式连接企业微信机器人', dualMode: true, refreshLabel: '刷新二维码' },
  whatsapp: {
    title: '扫码登录 WhatsApp',
    subtitle: '打开手机 WhatsApp → 设置 → 已关联的设备 → 关联设备，扫描下方二维码完成登录。',
    refreshLabel: '刷新二维码',
  },
};

// ── 二维码占位组件（Mock：确定性伪随机图案，联调阶段替换为真实二维码） ──

const FakeQr: React.FC<{ seed: string; size?: number }> = ({ seed, size = 180 }) => {
  const cells = useMemo(() => {
    // 简单确定性伪随机（xorshift 变体），同一 seed 图案稳定
    let h = 0;
    for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) >>> 0;
    const rand = () => {
      h ^= h << 13;
      h ^= h >>> 17;
      h ^= h << 5;
      h >>>= 0;
      return h / 4294967295;
    };
    const n = 25;
    const grid: boolean[][] = [];
    for (let r = 0; r < n; r++) {
      grid.push([]);
      for (let c = 0; c < n; c++) grid[r].push(rand() > 0.55);
    }
    // 三个定位角
    const finder = (r0: number, c0: number) => {
      for (let r = 0; r < 7; r++) {
        for (let c = 0; c < 7; c++) {
          const edge = r === 0 || r === 6 || c === 0 || c === 6;
          const core = r >= 2 && r <= 4 && c >= 2 && c <= 4;
          grid[r0 + r][c0 + c] = edge || core;
        }
      }
    };
    finder(0, 0);
    finder(0, 18);
    finder(18, 0);
    return grid;
  }, [seed]);

  const n = cells.length;
  const cell = size / n;
  return (
    <Box
      sx={{
        width: size,
        height: size,
        border: '1px solid #EEF0F2',
        borderRadius: '12px',
        p: 1,
        background: '#fff',
        flexShrink: 0,
      }}
    >
      <svg width={size - 8} height={size - 8} viewBox={`0 0 ${size - 8} ${size - 8}`}>
        {cells.map((row, r) =>
          row.map((on, c) =>
            on ? (
              <rect key={`${r}-${c}`} x={c * ((size - 8) / n)} y={r * ((size - 8) / n)} width={(size - 8) / n} height={(size - 8) / n} fill="#111827" />
            ) : null,
          ),
        )}
      </svg>
    </Box>
  );
};

// ── 页面组件 ──────────────────────────────────────────────────────

const IMChannel: React.FC = () => {
  const [configId, setConfigId] = useState<ChannelId | null>(null);
  const [qrSeed, setQrSeed] = useState(0);
  const [bindMode, setBindMode] = useState<'quick' | 'manual'>('quick');
  const [toast, setToast] = useState<string | null>(null);

  // Slack 表单状态
  const [botToken, setBotToken] = useState('');
  const [appToken, setAppToken] = useState('');
  const [policy, setPolicy] = useState<'pairing' | 'open'>('pairing');

  const channel = CHANNELS.find((c) => c.id === configId) ?? null;

  const openConfig = (id: ChannelId) => {
    setConfigId(id);
    setQrSeed((s) => s + 1);
    setBindMode('quick');
    setBotToken('');
    setAppToken('');
    setPolicy('pairing');
  };

  // Slack 保存（Mock 开关：开发阶段本地模拟，联调阶段接 Socket Mode 校验）
  const slackValid = botToken.trim().length > 0 && appToken.trim().length > 0;
  const handleSlackSave = () => {
    if (!slackValid) return;
    setConfigId(null);
    if (mockEnabled) {
      setToast('Slack 配置已保存（开发阶段 Mock，Socket Mode 接入即将上线）');
      return;
    }
    // 联调阶段：真实 Slack Token 校验与接入位
    setToast('Slack 配置保存请求已提交');
  };

  const handleSlackTest = () => {
    if (!slackValid) return;
    setToast(mockEnabled ? '连接测试通过（开发阶段 Mock）' : '正在测试连接…');
  };

  const IconChip: React.FC<{ c: ChannelDef; size?: number }> = ({ c, size = 40 }) => {
    const Icon = c.icon;
    return (
      <Box
        sx={{
          width: size,
          height: size,
          borderRadius: '10px',
          background: c.iconBg,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
        }}
      >
        <Icon sx={{ fontSize: size * 0.55, color: c.iconColor }} />
      </Box>
    );
  };

  return (
    <Box sx={{ maxWidth: 860, mx: 'auto' }}>
      {/* 页头 */}
      <Typography variant="h5" sx={{ fontWeight: 700 }}>IM 频道</Typography>
      <Typography sx={{ fontSize: '13px', color: '#6B7280', mt: 0.5, mb: 3 }}>
        配置 IM 频道，让通天办公接收来自钉钉、飞书等平台的消息。频道配置信息仅存储在本地，不会上传到云端。
      </Typography>

      {/* 平台配置列表 */}
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
        {CHANNELS.map((c) => (
          <Box
            key={c.id}
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 2,
              border: '1px solid #EEF0F2',
              borderRadius: '14px',
              p: 2,
              background: '#FFFFFF',
              transition: 'box-shadow 0.18s ease',
              '&:hover': { boxShadow: '0 4px 16px rgba(15, 23, 42, 0.06)' },
            }}
          >
            <IconChip c={c} />
            <Box sx={{ flexGrow: 1, minWidth: 0 }}>
              <Typography sx={{ fontSize: '14px', fontWeight: 600, color: '#111827' }}>{c.name}</Typography>
              <Typography sx={{ fontSize: '12.5px', color: '#6B7280', mt: 0.25 }}>{c.desc}</Typography>
            </Box>
            <Button
              variant="contained"
              onClick={() => openConfig(c.id)}
              sx={{
                bgcolor: '#111827',
                borderRadius: '10px',
                textTransform: 'none',
                fontSize: '13px',
                fontWeight: 600,
                px: 2.5,
                flexShrink: 0,
                '&:hover': { bgcolor: '#4F46E5' },
              }}
            >
              配置
            </Button>
          </Box>
        ))}
      </Box>

      {/* ── 扫码类配置弹窗 ── */}
      {channel && channel.id !== 'slack' && (() => {
        const cfg = QR_DIALOGS[channel.id as Exclude<ChannelId, 'slack'>];
        return (
          <Dialog open onClose={() => setConfigId(null)} PaperProps={{ sx: { width: 420, borderRadius: '16px', p: 1, position: 'relative' } }}>
            <IconButton
              aria-label="close"
              onClick={() => setConfigId(null)}
              sx={{ position: 'absolute', right: 10, top: 10, color: '#9CA3AF', zIndex: 1 }}
            >
              <CloseIcon sx={{ fontSize: 18 }} />
            </IconButton>
            <DialogTitle sx={{ pb: 0.5, textAlign: 'center' }}>
              <Box sx={{ display: 'flex', justifyContent: 'center', mb: 1 }}>
                <IconChip c={channel} size={44} />
              </Box>
              <Typography sx={{ fontSize: '16px', fontWeight: 700 }}>{cfg.title}</Typography>
              <Typography sx={{ fontSize: '12.5px', color: '#9CA3AF', mt: 0.5, px: 2 }}>{cfg.subtitle}</Typography>
            </DialogTitle>
            <DialogContent sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', pt: '8px !important' }}>
              {/* 双模式：快捷绑定（推荐）/ 手动配置 */}
              {cfg.dualMode && (
                <Box sx={{ width: '100%', mb: 1.5 }}>
                  {([
                    ['quick', '快捷绑定（推荐）'],
                    ['manual', '手动配置'],
                  ] as const).map(([key, label]) => (
                    <Box
                      key={key}
                      onClick={() => setBindMode(key)}
                      sx={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 1,
                        px: 1.5,
                        py: 1,
                        borderRadius: '10px',
                        cursor: 'pointer',
                        border: bindMode === key ? '1.5px solid #111827' : '1.5px solid transparent',
                        background: bindMode === key ? '#FAFBFC' : 'transparent',
                        transition: 'all 0.15s ease',
                        '&:hover': { background: '#FAFBFC' },
                      }}
                    >
                      <Box
                        sx={{
                          width: 16,
                          height: 16,
                          borderRadius: '50%',
                          border: bindMode === key ? '5px solid #111827' : '1.5px solid #D1D5DB',
                          boxSizing: 'border-box',
                        }}
                      />
                      <Typography sx={{ fontSize: '13px', fontWeight: bindMode === key ? 600 : 400, color: '#374151' }}>
                        {label}
                      </Typography>
                    </Box>
                  ))}
                </Box>
              )}

              {/* 二维码（Mock 占位渲染） */}
              <FakeQr seed={`${channel.id}-${qrSeed}`} />
              <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mt: 1.5, textAlign: 'center', px: 1 }}>
                {channel.id === 'wecom' ? '打开企业微信，扫描二维码完成机器人创建' : cfg.subtitle}
              </Typography>

              {/* 刷新按钮 */}
              <Button
                fullWidth={cfg.darkRefresh}
                variant={cfg.darkRefresh ? 'contained' : 'outlined'}
                onClick={() => setQrSeed((s) => s + 1)}
                sx={{
                  mt: 2,
                  mb: 1,
                  borderRadius: '10px',
                  textTransform: 'none',
                  fontSize: '13px',
                  fontWeight: 600,
                  px: 3,
                  ...(cfg.darkRefresh
                    ? { bgcolor: '#111827', '&:hover': { bgcolor: '#4F46E5' } }
                    : { color: '#4B5563', borderColor: '#E5E7EB', '&:hover': { borderColor: '#9CA3AF', background: 'rgba(15,23,42,0.03)' } }),
                }}
              >
                {cfg.refreshLabel}
              </Button>
            </DialogContent>
          </Dialog>
        );
      })()}

      {/* ── Slack 表单配置弹窗 ── */}
      {channel?.id === 'slack' && (
        <Dialog open onClose={() => setConfigId(null)} PaperProps={{ sx: { width: 520, borderRadius: '16px', p: 1, position: 'relative' } }}>
          <IconButton
            aria-label="close"
            onClick={() => setConfigId(null)}
            sx={{ position: 'absolute', right: 10, top: 10, color: '#9CA3AF', zIndex: 1 }}
          >
            <CloseIcon sx={{ fontSize: 18 }} />
          </IconButton>
          <DialogTitle sx={{ pb: 0.5 }}>
            <Typography sx={{ fontSize: '16px', fontWeight: 700 }}>配置 Slack</Typography>
            <Typography sx={{ fontSize: '12.5px', color: '#9CA3AF', mt: 0.25 }}>
              填写 Slack Bot Token 与 App-Level Token 以通过 Socket Mode 接入
            </Typography>
          </DialogTitle>
          <DialogContent sx={{ pt: '12px !important' }}>
            <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>
              Bot Token <Box component="span" sx={{ color: '#EF4444' }}>*</Box>
            </Typography>
            <TextField
              value={botToken}
              onChange={(e) => setBotToken(e.target.value)}
              fullWidth
              size="small"
              placeholder="xoxb-..."
              sx={{ mb: 2, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
            />

            <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>
              App-Level Token <Box component="span" sx={{ color: '#EF4444' }}>*</Box>
            </Typography>
            <TextField
              value={appToken}
              onChange={(e) => setAppToken(e.target.value)}
              fullWidth
              size="small"
              placeholder="xapp-..."
              sx={{ mb: 2.5, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
            />

            {/* 准入策略 */}
            <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>准入策略</Typography>
            {([
              ['pairing', '配对模式', '需配对后才能对话'],
              ['open', '开放模式', '所有人/群可直接对话'],
            ] as const).map(([key, label, sub]) => (
              <Box
                key={key}
                onClick={() => setPolicy(key)}
                sx={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 1,
                  px: 1.5,
                  py: 1.1,
                  mb: 0.75,
                  borderRadius: '10px',
                  cursor: 'pointer',
                  border: policy === key ? '1.5px solid #10B981' : '1.5px solid #EEF0F2',
                  background: policy === key ? 'rgba(16, 185, 129, 0.04)' : '#FFFFFF',
                  transition: 'all 0.15s ease',
                }}
              >
                <Box sx={{ flexGrow: 1 }}>
                  <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#111827' }}>{label}</Typography>
                  <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF', mt: 0.15 }}>{sub}</Typography>
                </Box>
                {policy === key && <CheckCircleIcon sx={{ fontSize: 18, color: '#10B981' }} />}
              </Box>
            ))}
            {policy === 'pairing' && (
              <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF', lineHeight: 1.7, mb: 1 }}>
                保存后，用户私聊机器人或在群中 @机器人时会自动触发配对请求，您可在频道卡片的「配对管理」中点击「允许」。配对以会话为单位：私聊允许后该用户可对话，群聊允许后群内所有成员均可对话。
              </Typography>
            )}

            {/* 测试连接 / 保存 */}
            <Box sx={{ display: 'flex', justifyContent: 'flex-end', gap: 1, mt: 2, mb: 1 }}>
              <Button
                onClick={handleSlackTest}
                disabled={!slackValid}
                sx={{
                  color: slackValid ? '#4B5563' : '#9CA3AF',
                  border: '1px solid #E5E7EB',
                  borderRadius: '10px',
                  textTransform: 'none',
                  fontSize: '13px',
                  fontWeight: 600,
                  px: 2.5,
                  '&:hover': { background: 'rgba(15, 23, 42, 0.03)' },
                  '&.Mui-disabled': { color: '#9CA3AF', borderColor: '#E5E7EB' },
                }}
              >
                测试连接
              </Button>
              <Button
                variant="contained"
                disabled={!slackValid}
                onClick={handleSlackSave}
                sx={{
                  bgcolor: slackValid ? '#111827' : '#E5E7EB',
                  borderRadius: '10px',
                  textTransform: 'none',
                  fontSize: '13px',
                  fontWeight: 700,
                  px: 3,
                  '&:hover': { bgcolor: '#4F46E5' },
                  '&.Mui-disabled': { bgcolor: '#E5E7EB', color: '#9CA3AF' },
                }}
              >
                保存
              </Button>
            </Box>
          </DialogContent>
        </Dialog>
      )}

      {/* 占位提示 toast（不静默） */}
      <Snackbar open={!!toast} autoHideDuration={3000} onClose={() => setToast(null)} anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity="info" onClose={() => setToast(null)} sx={{ fontSize: '13px' }}>
          {toast}
        </Alert>
      </Snackbar>
    </Box>
  );
};

export default IMChannel;
