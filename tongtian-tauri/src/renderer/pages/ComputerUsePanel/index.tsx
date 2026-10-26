/**
 * 通天办公 - 电脑操控（Windows 桌面执行器）
 *
 * 事实来源：
 * - 能力/平台/网关/原因来自 `computerUse:capabilities`（Rust 或本地权威实现）
 * - 档位读写 settings_kv 的 computerUsePolicy，与设置页共用同一键
 * - 截屏自检由 Rust 按当前档位确认后执行，图片只回传页面内存，不落盘
 *
 * 不发生的事也要写清楚：非 Windows、网关未启动、档位禁用时按钮置灰并说明原因，
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
import MonitorIcon from '@mui/icons-material/Monitor';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import RadioButtonUncheckedIcon from '@mui/icons-material/RadioButtonUnchecked';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import PhotoCameraIcon from '@mui/icons-material/PhotoCamera';
import DescriptionIcon from '@mui/icons-material/Description';
import TouchAppIcon from '@mui/icons-material/TouchApp';
import VerifiedIcon from '@mui/icons-material/Verified';
import {
  captureComputerUseScreen,
  getComputerUseCapabilities,
  setComputerUsePolicy,
} from '../../lib/alignment-api';
import type {
  ComputerUseCapabilities,
  ComputerUseCapture,
  ComputerUsePolicy,
} from '../../../shared/contracts';
import { errorTextOf } from '../../../shared/error-messages';
import { useAppStore } from '../../stores/useAppStore';

const policies: { id: ComputerUsePolicy; label: string; desc: string }[] = [
  {
    id: 'ask_every_time',
    label: '每次询问（默认）',
    desc: 'AI 每次截屏或操作前都会弹出系统确认框；取消或超时即拒绝本次动作',
  },
  {
    id: 'auto',
    label: '自动执行',
    desc: 'AI 直接截屏与操作，不再逐次确认；所有动作仍写入审计，建议仅在可信任务中短期使用',
  },
  {
    id: 'disabled',
    label: '禁用',
    desc: '不注册截屏与操控工具，AI 完全无法观察或操作桌面',
  },
];

const timeline = [
  { icon: PhotoCameraIcon, title: '截屏', desc: 'AI 截取屏幕画面来了解界面状态' },
  { icon: DescriptionIcon, title: '操作描述', desc: '每一步操作前说明即将做什么' },
  { icon: TouchAppIcon, title: '实际操作', desc: '执行点击、输入、滚动等动作' },
  { icon: VerifiedIcon, title: '结果确认', desc: '操作后再次截屏确认结果是否符合预期' },
];

const limits = [
  '仅 Windows 桌面版：macOS / Linux 与浏览器、Mock 通道没有执行器，页面会显示具体原因',
  '需要视觉模型：当前模型不支持图片输入时，截屏与操控工具不会注册',
  '只有截屏 + 键鼠注入：没有 OCR、无障碍树或浏览器自动化，非视觉界面无法理解',
  '锁屏、UAC 安全桌面、以管理员权限运行的目标窗口可能无法截屏或接收输入',
  '子 Agent 不继承电脑操控工具，只有主 Agent 能调用',
  '单任务上限：截屏 20 次、操作 60 次；Win 键组合与任务管理器快捷键会被拒绝',
  '输入正文不写入审计（只记字符数），但屏幕上已显示的敏感内容可能被模型看到',
];

const warnings = [
  '自动执行档位下，AI 的每一步操作都不会再征求同意，请在可信任务中短期使用',
  '屏幕内容可能包含密码、私信等敏感信息，运行前请关闭相关窗口',
  '部分操作不可撤销——发送消息、提交表单、删除文件等可能无法撤回',
  '验证码与二步验证仍需人工介入，AI 无法完成 CAPTCHA、短信验证码或人脸识别',
];

const policyLabel = (id: ComputerUsePolicy): string =>
  policies.find((p) => p.id === id)?.label ?? id;

function featureText(value: boolean | undefined): string {
  return value ? '可用' : '不可用';
}

interface ComputerUsePanelProps {
  /** 嵌入设置页时由外层滚动，不再占满整屏 */
  embedded?: boolean;
}

const ComputerUsePanel: React.FC<ComputerUsePanelProps> = ({ embedded = false }) => {
  const [caps, setCaps] = useState<ComputerUseCapabilities | null>(null);
  const [loadError, setLoadError] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState<ComputerUsePolicy | null>(null);
  const [confirmAuto, setConfirmAuto] = useState(false);
  const [preview, setPreview] = useState<ComputerUseCapture | null>(null);
  const [capturing, setCapturing] = useState(false);
  const showNotification = useAppStore((s) => s.showNotification);

  const refresh = useCallback(async () => {
    try {
      const next = await getComputerUseCapabilities();
      setCaps(next);
      setLoadError('');
    } catch (err) {
      setCaps(null);
      setLoadError(errorTextOf(err, '电脑操控能力读取失败'));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const applyPolicy = useCallback(
    async (next: ComputerUsePolicy) => {
      setSaving(next);
      try {
        await setComputerUsePolicy(next);
        await refresh();
        showNotification(`电脑操控档位已切换为「${policyLabel(next)}」`, 'success');
      } catch (err) {
        showNotification(errorTextOf(err, '电脑操控档位保存失败'), 'error');
        await refresh();
      } finally {
        setSaving(null);
      }
    },
    [refresh, showNotification],
  );

  const selectPolicy = (next: ComputerUsePolicy) => {
    if (!caps?.supported || saving !== null || next === caps.policy) return;
    if (next === 'auto') {
      setConfirmAuto(true);
      return;
    }
    void applyPolicy(next);
  };

  const runCapture = async () => {
    setCapturing(true);
    try {
      const shot = await captureComputerUseScreen();
      setPreview(shot);
      showNotification(`截屏自检成功：${shot.width}×${shot.height}`, 'success');
    } catch (err) {
      showNotification(errorTextOf(err, '截屏自检失败'), 'error');
    } finally {
      setCapturing(false);
    }
  };

  const ready = Boolean(
    caps &&
      caps.supported &&
      caps.gate &&
      caps.policy !== 'disabled' &&
      !caps.policyError,
  );
  const captureDisabledReason = !caps
    ? loadError || '电脑操控能力尚未读取'
    : !caps.supported
      ? caps.reason
      : caps.policyError
        ? caps.reason
        : caps.policy === 'disabled'
          ? '当前档位为「禁用」，切换后才能截屏自检'
          : !caps.gate
            ? caps.reason
            : '';
  const canCapture = Boolean(caps?.supported && caps.gate && !caps.policyError && caps.policy !== 'disabled');
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
        <MonitorIcon sx={{ fontSize: 22, color: '#0D9488' }} />
        <Typography sx={{ fontSize: '20px', fontWeight: 700, color: 'var(--tt-text-1)' }}>电脑操控</Typography>
        {loading && <CircularProgress size={16} sx={{ color: '#0D9488' }} />}
      </Box>
      <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-3)', mt: 0.5 }}>
        Windows 桌面执行器：AI 可截取屏幕观察界面，并在策略允许时点击、输入或按键。截屏与操作由本机执行并写入审计。
      </Typography>

      <Box sx={{ maxWidth: 820, mt: 2 }}>
        {/* 能力状态：由 Rust/本地权威实现给出，不用设置里的档位冒充「现在能用」 */}
        <Box
          data-testid="computer-use-status"
          sx={{
            p: 1.75,
            borderRadius: '14px',
            border: ready ? '1px solid rgba(13, 148, 136, 0.35)' : '1px solid rgba(217, 119, 6, 0.35)',
            bgcolor: ready ? 'rgba(13, 148, 136, 0.07)' : 'rgba(217, 119, 6, 0.08)',
          }}
        >
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
            {ready ? (
              <CheckCircleIcon sx={{ fontSize: 18, color: '#0D9488' }} />
            ) : (
              <WarningAmberIcon sx={{ fontSize: 18, color: '#D97706' }} />
            )}
            <Typography sx={{ fontSize: '13.5px', fontWeight: 700, color: ready ? '#0D9488' : '#B45309' }}>
              {ready ? '执行器可用' : '执行器不可用'}
            </Typography>
            {caps && (
              <Box sx={{ px: 1, py: 0.2, borderRadius: '6px', bgcolor: 'rgba(107, 114, 128, 0.12)' }}>
                <Typography sx={{ fontSize: '11px', fontWeight: 600, color: '#6B7280' }}>
                  {caps.platform}
                </Typography>
              </Box>
            )}
          </Box>
          <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-2)', mt: 0.75, lineHeight: 1.7 }}>
            {loading
              ? '正在读取本机电脑操控能力…'
              : loadError
                ? loadError
                : ready
                  ? '截屏、点击、输入与按键均已接通；每次调用按当前档位确认并写入审计。'
                  : caps?.reason || '执行器当前不可用'}
          </Typography>
          {caps && (
            <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', mt: 0.5 }}>
              截图{featureText(caps.features.screenshot)} · 键鼠注入{featureText(caps.features.input)} · 本机网关
              {caps.gate ? '已启动' : '未启动'} · {caps.persisted ? '档位已保存' : '当前为默认档位'}
            </Typography>
          )}
        </Box>

        {/* 执行策略：真实写入 settings_kv 的 computerUsePolicy */}
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 3, mb: 1 }}>
          <Typography sx={{ fontSize: '16px', fontWeight: 700, color: 'var(--tt-text-1)' }}>执行策略</Typography>
          <Box sx={{ px: 1, py: 0.2, borderRadius: '6px', bgcolor: 'rgba(13, 148, 136, 0.12)' }}>
            <Typography sx={{ fontSize: '11px', fontWeight: 600, color: '#0D9488' }}>
              当前：{policyLabel(activePolicy)}
            </Typography>
          </Box>
          {savingBusy && <CircularProgress size={14} sx={{ color: '#0D9488' }} />}
        </Box>
        <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3)', mb: 1.5 }}>
          {caps?.supported
            ? '档位保存后立即生效：任务进行中切换也会按最新档位执行。'
            : '当前通道没有执行器，档位开关无法生效，因此不可修改。'}
        </Typography>
        {policies.map((p) => {
          const selected = activePolicy === p.id;
          const disabled = !caps?.supported || savingBusy;
          return (
            <ButtonBase
              key={p.id}
              data-testid={`computer-use-policy-${p.id}`}
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
                border: selected ? '1.5px solid #14B8A6' : '1px solid var(--tt-border)',
                borderRadius: '14px',
                px: 2,
                py: 1.75,
                mb: 1,
                opacity: disabled && !selected ? 0.6 : 1,
                cursor: disabled ? 'default' : 'pointer',
                '&:hover': disabled ? {} : { borderColor: '#14B8A6' },
              }}
            >
              {selected ? (
                <CheckCircleIcon sx={{ fontSize: 18, color: '#0D9488' }} />
              ) : (
                <RadioButtonUncheckedIcon sx={{ fontSize: 18, color: '#D1D5DB' }} />
              )}
              <Box>
                <Typography sx={{ fontSize: '13.5px', fontWeight: 600, color: selected ? '#0D9488' : 'var(--tt-text-1)' }}>
                  {p.label}
                </Typography>
                <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', mt: 0.25 }}>{p.desc}</Typography>
              </Box>
            </ButtonBase>
          );
        })}

        {/* 截屏自检：真实调用 Rust command；每次询问档位会先弹系统确认框 */}
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
            <Typography sx={{ fontSize: '16px', fontWeight: 700, color: 'var(--tt-text-1)' }}>截屏自检</Typography>
            <Button
              data-testid="computer-use-capture"
              size="small"
              variant="contained"
              startIcon={capturing ? <CircularProgress size={14} sx={{ color: '#fff' }} /> : <PhotoCameraIcon />}
              disabled={!canCapture || capturing}
              onClick={() => void runCapture()}
              sx={{ textTransform: 'none', bgcolor: '#0D9488', '&:hover': { bgcolor: '#0F766E' } }}
            >
              {capturing ? '正在截屏…' : '截取当前屏幕'}
            </Button>
          </Box>
          <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3)', mt: 1, lineHeight: 1.7 }}>
            {captureDisabledReason ||
              (activePolicy === 'ask_every_time'
                ? '点击后会先弹出系统确认框；允许后才截屏。图片仅在当前页面内存中预览，不落盘。'
                : '自动执行档位下会直接截屏。图片仅在当前页面内存中预览，不落盘。')}
          </Typography>

          {preview && (
            <Box sx={{ mt: 1.5 }}>
              <Box
                component="img"
                src={`data:${preview.mime};base64,${preview.imageBase64}`}
                alt="电脑操控截屏自检预览"
                sx={{
                  display: 'block',
                  maxWidth: '100%',
                  maxHeight: 420,
                  border: '1px solid var(--tt-border)',
                  borderRadius: '10px',
                }}
              />
              <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', mt: 0.75 }}>
                预览 {preview.width}×{preview.height}
                {preview.scaled
                  ? `（原始 ${preview.originalWidth}×${preview.originalHeight}，已等比降采样）`
                  : ''}
                {' · '}虚拟桌面原点 ({preview.originX}, {preview.originY})
              </Typography>
              <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', mt: 0.25 }}>
                审计只记录截屏尺寸，不记录画面内容；刷新或离开页面后预览消失。
              </Typography>
            </Box>
          )}
        </Box>

        {/* 执行过程透明度（已接入，按真实行为描述） */}
        <Typography sx={{ fontSize: '16px', fontWeight: 700, color: 'var(--tt-text-1)', mt: 3, mb: 0.75 }}>
          执行过程透明度
        </Typography>
        <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3)', mb: 1.5 }}>
          工具会要求模型在每次操作后重新截屏确认结果；以下步骤由模型按提示执行，而不是界面伪造的进度。
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
                  position: 'relative',
                }}
              >
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, mb: 0.75 }}>
                  <Box
                    sx={{
                      width: 26,
                      height: 26,
                      borderRadius: '50%',
                      bgcolor: '#F0FDFA',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                    }}
                  >
                    <Icon sx={{ fontSize: 15, color: '#0D9488' }} />
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
            启用后，AI 在任务中会直接截屏并点击、输入或按键，不再逐次弹出确认框。
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

export default ComputerUsePanel;
