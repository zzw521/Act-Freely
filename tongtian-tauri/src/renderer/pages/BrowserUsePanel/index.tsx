/**
 * 通天办公 - 浏览器 AI+（受控浏览器会话）
 *
 * 事实来源：
 * - 能力/浏览器/网关/会话状态来自 `browserUse:capabilities`（Rust 或本地权威实现）
 * - 档位读写 settings_kv 的 browserUsePolicy，与设置页共用同一键
 * - 启动/结束只作用于本应用启动的受控浏览器；独立配置目录，不碰用户日常浏览器
 *
 * 不发生的事也要写清楚：没有装 Edge/Chrome、网关未启动、档位禁用时按钮置灰并说明原因，
 * 不用「保存成功」冒充「现在能用」。
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  Box,
  Button,
  ButtonBase,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  Typography,
} from '@mui/material';
import LanguageIcon from '@mui/icons-material/Language';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import RadioButtonUncheckedIcon from '@mui/icons-material/RadioButtonUnchecked';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import StopIcon from '@mui/icons-material/Stop';
import OpenInBrowserIcon from '@mui/icons-material/OpenInBrowser';
import VisibilityIcon from '@mui/icons-material/Visibility';
import TouchAppIcon from '@mui/icons-material/TouchApp';
import RefreshIcon from '@mui/icons-material/Refresh';
import {
  getBrowserUseCapabilities,
  setBrowserUsePolicy,
  startBrowserUseSession,
  stopBrowserUseSession,
} from '../../lib/alignment-api';
import type {
  BrowserUseCapabilities,
  BrowserUsePolicy,
} from '../../../shared/contracts';
import { errorTextOf } from '../../../shared/error-messages';
import { useAppStore } from '../../stores/useAppStore';

const policies: { id: BrowserUsePolicy; label: string; desc: string }[] = [
  {
    id: 'ask_every_time',
    label: '每次询问（默认）',
    desc: 'AI 每次打开页面、快照或操作前都会弹出系统确认框；取消或超时即拒绝本次动作',
  },
  {
    id: 'auto',
    label: '自动执行',
    desc: 'AI 直接操作页面，不再逐次确认；所有动作仍写入审计，建议仅在可信任务中短期使用',
  },
  {
    id: 'disabled',
    label: '禁用',
    desc: '不注册浏览器工具，AI 无法打开或操作任何网页',
  },
];

const timeline = [
  { icon: OpenInBrowserIcon, title: '打开页面', desc: '在受控浏览器中打开 http/https 地址' },
  { icon: VisibilityIcon, title: '读取快照', desc: '读取页面文字与可交互元素序号' },
  { icon: TouchAppIcon, title: '点击 / 输入', desc: '按序号执行真实鼠标与键盘操作' },
  { icon: RefreshIcon, title: '重新确认', desc: '页面变化后重新快照，再决定下一步' },
];

const limits = [
  '仅桌面版且本机安装 Microsoft Edge 或 Google Chrome；浏览器、Mock 通道没有受控浏览器进程',
  '只允许 http/https：file、javascript、data、chrome、devtools 等协议在引擎与桌面壳两侧都会被拒绝',
  '独立配置目录：不读取你日常浏览器的 Cookie 与登录态；需要登录的站点要在受控窗口里单独登录',
  '下载一律拒绝；密码框不接受 AI 输入，需要用户亲自填写',
  '页面理解走 DOM 快照，不依赖视觉模型；网页截图只在当前模型支持图片输入时注册',
  '单任务上限：操作 80 次、快照 40 次、截图 20 次；子 Agent 不继承浏览器控制能力',
  '审计只记 URL 路径（去掉查询串与片段）、元素标签与次数；输入正文、页面正文与截图内容不写入审计',
  '验证码、短信验证与人脸识别仍需人工完成；支付、删除等不可逆操作请先人工确认',
];

const warnings = [
  '自动执行档位下，AI 的页面操作不会逐次征求同意，请在可信任务中短期使用',
  '网页可能包含私信、订单等敏感信息；受控窗口登录后，模型能看到当前页面内容',
  '提交表单、发送消息、下单等操作可能不可撤销，运行前请确认页面与任务范围',
];

const policyLabel = (id: BrowserUsePolicy): string =>
  policies.find((p) => p.id === id)?.label ?? id;

interface BrowserUsePanelProps {
  /** 嵌入设置页时由外层滚动，不再占满整屏 */
  embedded?: boolean;
}

const BrowserUsePanel: React.FC<BrowserUsePanelProps> = ({ embedded = false }) => {
  const [caps, setCaps] = useState<BrowserUseCapabilities | null>(null);
  const [loadError, setLoadError] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<BrowserUsePolicy | null>(null);
  const [confirmAuto, setConfirmAuto] = useState(false);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const showNotification = useAppStore((s) => s.showNotification);

  const refresh = useCallback(async () => {
    try {
      const next = await getBrowserUseCapabilities();
      setCaps(next);
      setLoadError('');
    } catch (err) {
      setCaps(null);
      setLoadError(errorTextOf(err, '浏览器 AI+ 能力读取失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const applyPolicy = useCallback(
    async (next: BrowserUsePolicy) => {
      setSaving(next);
      try {
        await setBrowserUsePolicy(next);
        await refresh();
        showNotification(`浏览器档位已切换为「${policyLabel(next)}」`, 'success');
      } catch (err) {
        showNotification(errorTextOf(err, '浏览器档位保存失败'), 'error');
        await refresh();
      } finally {
        setSaving(null);
      }
    },
    [refresh, showNotification],
  );

  const selectPolicy = (next: BrowserUsePolicy) => {
    if (!caps?.supported || saving !== null || next === caps.policy) return;
    if (next === 'auto') {
      setConfirmAuto(true);
      return;
    }
    void applyPolicy(next);
  };

  const startSession = async () => {
    setStarting(true);
    try {
      const res = await startBrowserUseSession();
      showNotification(
        res.launched
          ? `受控浏览器已启动（端口 ${res.port ?? '—'}）`
          : '受控浏览器已在运行',
        'success',
      );
      await refresh();
    } catch (err) {
      showNotification(errorTextOf(err, '受控浏览器启动失败'), 'error');
      await refresh();
    } finally {
      setStarting(false);
    }
  };

  const stopSession = async () => {
    setStopping(true);
    try {
      const res = await stopBrowserUseSession();
      showNotification(res.stopped ? '受控浏览器已结束' : '没有需要结束的受控浏览器', 'success');
      await refresh();
    } catch (err) {
      showNotification(errorTextOf(err, '结束受控浏览器失败'), 'error');
      await refresh();
    } finally {
      setStopping(false);
    }
  };

  const ready = Boolean(
    caps &&
      caps.supported &&
      caps.gate &&
      caps.policy !== 'disabled' &&
      !caps.policyError,
  );
  const canStart = Boolean(caps?.supported && !caps.policyError && caps.policy !== 'disabled');
  const startDisabledReason = !caps
    ? loadError || '浏览器能力尚未读取'
    : !caps.supported
      ? caps.reason
      : caps.policyError
        ? caps.reason
        : caps.policy === 'disabled'
          ? '当前档位为「禁用」，切换后才能启动受控浏览器'
          : !caps.gate
            ? caps.reason
            : '';
  const activePolicy = caps?.policy ?? 'ask_every_time';
  const savingBusy = saving !== null;

  return (
    <Box
      sx={{
        height: embedded ? 'auto' : 'calc(100vh - 104px)',
        overflowY: embedded ? 'visible' : 'auto',
        pr: embedded ? 0 : 1,
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <LanguageIcon sx={{ fontSize: 22, color: '#059669' }} />
        <Typography sx={{ fontSize: '20px', fontWeight: 700, color: 'var(--tt-text-1)' }}>浏览器 AI+</Typography>
        {loading && <CircularProgress size={16} sx={{ color: '#059669' }} />}
      </Box>
      <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-3)', mt: 0.5 }}>
        受控浏览器执行器：AI 在本应用启动的独立浏览器里打开网页、读取页面并按序号点击或输入。
        每一步都按档位放行并写入审计，不接触你日常浏览器的登录态。
      </Typography>

      <Box sx={{ maxWidth: 860, mt: 2 }}>
        {/* 能力状态：由 Rust/本地权威实现给出，不用设置里的档位冒充「现在能用」 */}
        <Box
          data-testid="browser-use-status"
          sx={{
            p: 1.75,
            borderRadius: '14px',
            border: ready ? '1px solid rgba(5, 150, 105, 0.35)' : '1px solid rgba(217, 119, 6, 0.35)',
            bgcolor: ready ? 'rgba(5, 150, 105, 0.07)' : 'rgba(217, 119, 6, 0.08)',
          }}
        >
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}>
            {ready ? (
              <CheckCircleIcon sx={{ fontSize: 18, color: '#059669' }} />
            ) : (
              <WarningAmberIcon sx={{ fontSize: 18, color: '#D97706' }} />
            )}
            <Typography sx={{ fontSize: '13.5px', fontWeight: 700, color: ready ? '#047857' : '#B45309' }}>
              {ready ? '执行器可用' : '执行器不可用'}
            </Typography>
            {caps && (
              <Box sx={{ px: 1, py: 0.2, borderRadius: '6px', bgcolor: 'rgba(107, 114, 128, 0.12)' }}>
                <Typography sx={{ fontSize: '11px', fontWeight: 600, color: '#6B7280' }}>
                  {caps.browser || caps.platform}
                </Typography>
              </Box>
            )}
            {caps?.running && (
              <Box sx={{ px: 1, py: 0.2, borderRadius: '6px', bgcolor: 'rgba(5, 150, 105, 0.12)' }}>
                <Typography sx={{ fontSize: '11px', fontWeight: 600, color: '#047857' }}>
                  会话运行中 · 端口 {caps.port ?? '—'}
                </Typography>
              </Box>
            )}
          </Box>
          <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-2)', mt: 0.75, lineHeight: 1.7 }}>
            {loading
              ? '正在读取本机浏览器能力…'
              : loadError
                ? loadError
                : ready
                  ? '打开、快照、点击、输入与滚动均已接通；每次调用按当前档位确认并写入审计。'
                  : caps?.reason || '执行器当前不可用'}
          </Typography>
          {caps && (
            <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', mt: 0.5 }}>
              页面快照{caps.features.snapshot ? '可用' : '不可用'} · 截图
              {caps.features.screenshot ? '可用' : '不可用'} · 本机网关
              {caps.gate ? '已启动' : '未启动'} · {caps.persisted ? '档位已保存' : '当前为默认档位'}
            </Typography>
          )}
          {caps?.profileDir && (
            <Typography
              sx={{
                fontSize: '11.5px',
                color: 'var(--tt-text-4)',
                mt: 0.5,
                wordBreak: 'break-all',
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
              }}
            >
              独立配置目录：{caps.profileDir}
            </Typography>
          )}
        </Box>

        {/* 执行策略：真实写入 settings_kv 的 browserUsePolicy */}
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 3, mb: 1 }}>
          <Typography sx={{ fontSize: '16px', fontWeight: 700, color: 'var(--tt-text-1)' }}>执行策略</Typography>
          <Box sx={{ px: 1, py: 0.2, borderRadius: '6px', bgcolor: 'rgba(5, 150, 105, 0.12)' }}>
            <Typography sx={{ fontSize: '11px', fontWeight: 600, color: '#047857' }}>
              当前：{policyLabel(activePolicy)}
            </Typography>
          </Box>
          {savingBusy && <CircularProgress size={14} sx={{ color: '#059669' }} />}
        </Box>
        <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3)', mb: 1.5 }}>
          {caps?.supported
            ? '档位保存后立即生效：任务进行中切换也会按最新档位执行。'
            : '当前通道没有受控浏览器，档位开关无法生效，因此不可修改。'}
        </Typography>
        {policies.map((p) => {
          const selected = activePolicy === p.id;
          const disabled = !caps?.supported || savingBusy;
          return (
            <ButtonBase
              key={p.id}
              data-testid={`browser-use-policy-${p.id}`}
              role="radio"
              aria-checked={selected}
              aria-disabled={disabled || selected}
              disabled={disabled}
              onClick={() => selectPolicy(p.id)}
              sx={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'flex-start',
                textAlign: 'left',
                width: '100%',
                gap: 1.5,
                bgcolor: 'var(--tt-surface)',
                border: selected ? '1.5px solid #10B981' : '1px solid var(--tt-border)',
                borderRadius: '14px',
                px: 2,
                py: 1.75,
                mb: 1,
                opacity: disabled && !selected ? 0.6 : 1,
                cursor: disabled ? 'default' : 'pointer',
                '&:hover': disabled ? {} : { borderColor: '#10B981' },
              }}
            >
              {selected ? (
                <CheckCircleIcon sx={{ fontSize: 18, color: '#059669' }} />
              ) : (
                <RadioButtonUncheckedIcon sx={{ fontSize: 18, color: '#D1D5DB' }} />
              )}
              <Box>
                <Typography sx={{ fontSize: '13.5px', fontWeight: 600, color: selected ? '#047857' : 'var(--tt-text-1)' }}>
                  {p.label}
                </Typography>
                <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', mt: 0.25 }}>{p.desc}</Typography>
              </Box>
            </ButtonBase>
          );
        })}

        {/* 受控浏览器会话：启动/结束只作用于本应用启动的那个进程 */}
        <Box
          sx={{
            mt: 3,
            p: 2,
            borderRadius: '14px',
            border: '1px solid var(--tt-border)',
            bgcolor: 'var(--tt-surface)',
          }}
        >
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
            <Typography sx={{ fontSize: '16px', fontWeight: 700, color: 'var(--tt-text-1)' }}>受控浏览器会话</Typography>
            <Button
              data-testid="browser-use-start"
              size="small"
              variant="contained"
              startIcon={
                starting ? <CircularProgress size={14} sx={{ color: '#fff' }} /> : <PlayArrowIcon />
              }
              disabled={!canStart || starting || Boolean(caps?.running)}
              onClick={() => void startSession()}
              sx={{ textTransform: 'none', bgcolor: '#059669', '&:hover': { bgcolor: '#047857' } }}
            >
              {starting ? '正在启动…' : caps?.running ? '已在运行' : '启动受控浏览器'}
            </Button>
            <Button
              data-testid="browser-use-stop"
              size="small"
              variant="outlined"
              startIcon={stopping ? <CircularProgress size={14} /> : <StopIcon />}
              disabled={!caps?.running || stopping}
              onClick={() => void stopSession()}
              sx={{ textTransform: 'none' }}
            >
              {stopping ? '正在结束…' : '结束会话'}
            </Button>
          </Box>
          <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3)', mt: 1, lineHeight: 1.7 }}>
            {startDisabledReason ||
              (caps?.running
                ? `受控浏览器正在运行（端口 ${caps.port ?? '—'}）。AI 通过本机网关连接这个窗口，结束会话只会关闭本应用启动的进程。`
                : '启动后会打开一个空白受控窗口；任务需要时 AI 会在其中打开页面。它使用独立配置目录，不影响你日常浏览器的标签页与登录态。')}
          </Typography>
        </Box>

        {/* 执行过程透明度（按真实行为描述） */}
        <Typography sx={{ fontSize: '16px', fontWeight: 700, color: 'var(--tt-text-1)', mt: 3, mb: 0.75 }}>
          执行过程透明度
        </Typography>
        <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3)', mb: 1.5 }}>
          工具会要求模型先快照再按序号操作，页面变化后重新快照；以下步骤由模型按提示执行，不是界面伪造的进度。
        </Typography>
        <Box sx={{ display: 'flex', gap: 1.5, flexWrap: 'wrap', mb: 1 }}>
          {timeline.map((t, i) => {
            const Icon = t.icon;
            return (
              <Box
                key={t.title}
                sx={{
                  flex: '1 1 170px',
                  bgcolor: 'var(--tt-surface)',
                  border: '1px solid var(--tt-border)',
                  borderRadius: '14px',
                  p: 1.75,
                }}
              >
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, mb: 0.75 }}>
                  <Box
                    sx={{
                      width: 26,
                      height: 26,
                      borderRadius: '50%',
                      bgcolor: '#ECFDF5',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                    }}
                  >
                    <Icon sx={{ fontSize: 15, color: '#059669' }} />
                  </Box>
                  <Typography sx={{ fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-1)' }}>
                    {i + 1}. {t.title}
                  </Typography>
                </Box>
                <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', lineHeight: 1.5 }}>{t.desc}</Typography>
              </Box>
            );
          })}
        </Box>

        {/* 能力边界：未接入的部分如实列出 */}
        <Typography sx={{ fontSize: '16px', fontWeight: 700, color: 'var(--tt-text-1)', mt: 3, mb: 0.75 }}>
          能力边界
        </Typography>
        <Box
          sx={{
            bgcolor: 'var(--tt-surface)',
            border: '1px solid var(--tt-border)',
            borderRadius: '14px',
            p: 2,
            mb: 1,
          }}
        >
          {limits.map((item) => (
            <Typography key={item} sx={{ fontSize: '12.5px', color: 'var(--tt-text-2)', mb: 0.75, lineHeight: 1.65 }}>
              · {item}
            </Typography>
          ))}
        </Box>

        {/* 风险警示 */}
        <Box
          sx={{
            mt: 3,
            mb: 2,
            bgcolor: '#FFFBEB',
            border: '1px solid rgba(217, 119, 6, 0.25)',
            borderRadius: '14px',
            p: 2,
          }}
        >
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, mb: 1 }}>
            <WarningAmberIcon sx={{ fontSize: 17, color: '#D97706' }} />
            <Typography sx={{ fontSize: '13px', fontWeight: 700, color: '#B45309' }}>使用注意</Typography>
          </Box>
          {warnings.map((w) => (
            <Typography key={w} sx={{ fontSize: '12px', color: '#92400E', mb: 0.75, lineHeight: 1.6 }}>
              · {w}
            </Typography>
          ))}
        </Box>
      </Box>

      <Dialog open={confirmAuto} onClose={() => setConfirmAuto(false)} maxWidth="xs" fullWidth>
        <DialogTitle sx={{ fontSize: '16px', fontWeight: 700 }}>启用「自动执行」？</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ fontSize: '13px', lineHeight: 1.8 }}>
            启用后，AI 在任务中会直接打开页面、点击与输入，不再逐次弹出确认框。
            所有动作仍会写入审计，但无法在执行前阻止；建议仅在可信任务中短期使用，用完后切回「每次询问」。
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmAuto(false)} sx={{ textTransform: 'none' }}>
            取消
          </Button>
          <Button
            variant="contained"
            color="warning"
            sx={{ textTransform: 'none' }}
            onClick={() => {
              setConfirmAuto(false);
              void applyPolicy('auto');
            }}
          >
            确认启用
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

export default BrowserUsePanel;
