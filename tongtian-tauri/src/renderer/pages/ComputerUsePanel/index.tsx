/**
 * 通天办公 - 电脑操控面板 (对标 QwenWork 电脑操控)
 *
 * 设计:
 * - 执行策略三档: 每次询问(默认)/自动执行/禁用
 * - 权限引导: 截屏/辅助功能权限状态卡
 * - 透明度展示规范说明（四段式时间线）
 * - 风险警示区
 */

import React, { useState, useEffect } from 'react';
import { Box, Typography } from '@mui/material';
import MonitorIcon from '@mui/icons-material/Monitor';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import RadioButtonUncheckedIcon from '@mui/icons-material/RadioButtonUnchecked';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import PhotoCameraIcon from '@mui/icons-material/PhotoCamera';
import DescriptionIcon from '@mui/icons-material/Description';
import TouchAppIcon from '@mui/icons-material/TouchApp';
import VerifiedIcon from '@mui/icons-material/Verified';
import { getComputerUsePolicy, setComputerUsePolicy } from '../../lib/alignment-api';
import type { ComputerUsePolicy } from '../../../shared/contracts';
import { errorTextOf } from '../../../shared/error-messages';
import { api } from '../../lib/api';
import { useAppStore } from '../../stores/useAppStore';

type Policy = ComputerUsePolicy;

const policies: { id: Policy; label: string; desc: string }[] = [
  { id: 'ask_every_time', label: '每次询问（默认）', desc: 'AI 每次需要操控桌面时都会先征求你的确认' },
  { id: 'auto', label: '自动执行', desc: 'AI 直接执行桌面操作，无需逐次确认' },
  { id: 'disabled', label: '禁用', desc: '完全关闭电脑操控功能' },
];

// 四段式透明度时间线
const timeline = [
  { icon: PhotoCameraIcon, title: '截屏', desc: 'AI 截取当前屏幕画面来了解界面状态' },
  { icon: DescriptionIcon, title: '操作描述', desc: '每一步操作前说明即将做什么' },
  { icon: TouchAppIcon, title: '实际操作', desc: '执行点击、输入、滚动等动作' },
  { icon: VerifiedIcon, title: '结果确认', desc: '操作后再次截屏确认结果是否符合预期' },
];

const warnings = [
  '部分操作不可撤销——AI 发送消息、删除文件等操作可能无法撤回，高风险场景建议使用「每次询问」',
  '屏幕内容会被截取——屏幕上可见的任何内容（包括敏感信息）都可能被捕获，运行前请关闭含密码或私密数据的窗口',
  '网络操作谨慎——AI 可代你在已登录应用中发送邮件、提交表单，请对此类操作保持警惕',
  '验证码与二步验证需人工介入——AI 无法完成 CAPTCHA、短信验证码、人脸识别',
];

/**
 * 系统权限引导（按真实平台事实呈现，不伪造授权状态）
 */
type PermissionState = 'ready' | 'manual' | 'unknown';

interface PermissionGuide {
  id: string;
  label: string;
  desc: string;
  state: PermissionState;
}

function permissionGuide(platform: string): PermissionGuide[] {
  const p = platform.toLowerCase();
  const isWindows = p === 'windows' || p === 'win32';
  const isMac = p === 'macos' || p === 'darwin';
  const state: PermissionState = isWindows ? 'ready' : isMac ? 'manual' : 'unknown';
  const note = (macText: string, winText: string) =>
    isWindows ? winText : isMac ? macText : '未在桌面应用内运行时无法探测系统权限';
  return [
    {
      id: 'screen',
      label: '截屏权限',
      desc: note(
        '需在「系统设置 › 隐私与安全性 › 屏幕录制」勾选通天办公后重启应用',
        'Windows 截屏无需系统授权，可直接使用',
      ),
      state,
    },
    {
      id: 'access',
      label: '辅助功能权限',
      desc: note(
        '需在「系统设置 › 隐私与安全性 › 辅助功能」勾选通天办公后重启应用',
        'Windows 模拟键鼠无需系统授权，可直接使用',
      ),
      state,
    },
  ];
}

const PERMISSION_BADGE: Record<PermissionState, { label: string; color: string; bg: string }> = {
  ready: { label: '无需授权', color: '#0D9488', bg: '#E6F7F4' },
  manual: { label: '需手动勾选', color: '#D97706', bg: '#FEF3C7' },
  unknown: { label: '未知', color: '#6B7280', bg: '#F3F4F6' },
};

const ComputerUsePanel: React.FC = () => {
  const [policy, setPolicy] = useState<Policy>('ask_every_time');
  /** 系统权限引导按真实平台呈现（不做假授权状态点亮） */
  const [platform, setPlatform] = useState<string>('');
  const showNotification = useAppStore((s) => s.showNotification);

  // 从持久层加载操控策略（真实 IPC）
  useEffect(() => {
    getComputerUsePolicy()
      .then((p) => setPolicy(p))
      .catch((err: unknown) =>
        showNotification(errorTextOf(err, '操控策略加载失败'), 'error'),
      );
    api.system
      .info()
      .then((info) => setPlatform(String(info.platform ?? '')))
      .catch(() => setPlatform(''));
  }, [showNotification]);

  const handlePolicyChange = (p: Policy) => {
    setPolicy(p);
    setComputerUsePolicy(p).catch((err: unknown) => {
      // 档位未被接受时回到原选择，避免界面显示与持久层不一致
      getComputerUsePolicy()
        .then(setPolicy)
        .catch(() => undefined);
      showNotification(errorTextOf(err, '操控策略保存失败'), 'error');
    });
  };

  const permissions = permissionGuide(platform);

  return (
    <Box sx={{ height: 'calc(100vh - 104px)', overflowY: 'auto', pr: 1 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <MonitorIcon sx={{ fontSize: 22, color: '#0D9488' }} />
        <Typography sx={{ fontSize: '20px', fontWeight: 700, color: '#111827' }}>电脑操控</Typography>
      </Box>
      <Typography sx={{ fontSize: '13px', color: '#9CA3AF', mt: 0.5 }}>
        让通天办公直接操作你电脑上的应用程序。执行全程透明可见。
      </Typography>

      <Box sx={{ maxWidth: 760, mt: 3 }}>
        {/* 执行策略 */}
        <Typography sx={{ fontSize: '16px', fontWeight: 700, color: '#111827', mb: 1.5 }}>执行策略</Typography>
        {policies.map((p) => {
          const selected = policy === p.id;
          return (
            <Box
              key={p.id}
              onClick={() => handlePolicyChange(p.id)}
              sx={{
                display: 'flex',
                alignItems: 'center',
                gap: 1.5,
                bgcolor: '#FFFFFF',
                border: selected ? '1.5px solid #14B8A6' : '1px solid #E5E7EB',
                borderRadius: '14px',
                px: 2,
                py: 1.75,
                mb: 1,
                cursor: 'pointer',
              }}
            >
              {selected ? (
                <CheckCircleIcon sx={{ fontSize: 18, color: '#0D9488' }} />
              ) : (
                <RadioButtonUncheckedIcon sx={{ fontSize: 18, color: '#D1D5DB' }} />
              )}
              <Box>
                <Typography sx={{ fontSize: '13.5px', fontWeight: 600, color: selected ? '#0D9488' : '#111827' }}>
                  {p.label}
                </Typography>
                <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mt: 0.25 }}>{p.desc}</Typography>
              </Box>
            </Box>
          );
        })}

        {/* 权限引导 */}
        <Typography sx={{ fontSize: '16px', fontWeight: 700, color: '#111827', mt: 3, mb: 1.5 }}>系统权限</Typography>
        {permissions.map((perm) => (
          <Box
            key={perm.id}
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 1.5,
              bgcolor: '#FFFFFF',
              border: '1px solid #E5E7EB',
              borderRadius: '14px',
              px: 2,
              py: 1.75,
              mb: 1,
            }}
          >
            <Box sx={{ flexGrow: 1 }}>
              <Typography sx={{ fontSize: '13.5px', fontWeight: 600, color: '#111827' }}>{perm.label}</Typography>
              <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mt: 0.25 }}>{perm.desc}</Typography>
            </Box>
            {(() => {
              const badge = PERMISSION_BADGE[perm.state];
              return (
                <Box sx={{ px: 1.25, py: 0.4, borderRadius: '6px', bgcolor: badge.bg, flexShrink: 0 }}>
                  <Typography sx={{ fontSize: '11.5px', fontWeight: 600, color: badge.color }}>
                    {badge.label}
                  </Typography>
                </Box>
              );
            })()}
          </Box>
        ))}

        {/* 透明度展示规范 */}
        <Typography sx={{ fontSize: '16px', fontWeight: 700, color: '#111827', mt: 3, mb: 1.5 }}>
          执行过程透明度
        </Typography>
        <Box sx={{ display: 'flex', gap: 1.5, flexWrap: 'wrap', mb: 1 }}>
          {timeline.map((t, i) => {
            const Icon = t.icon;
            return (
              <Box
                key={t.title}
                sx={{
                  flex: '1 1 160px',
                  bgcolor: '#FFFFFF',
                  border: '1px solid #E5E7EB',
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
                  <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#111827' }}>
                    {i + 1}. {t.title}
                  </Typography>
                </Box>
                <Typography sx={{ fontSize: '12px', color: '#6B7280', lineHeight: 1.5 }}>{t.desc}</Typography>
              </Box>
            );
          })}
        </Box>

        {/* 风险警示区 */}
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
    </Box>
  );
};

export default ComputerUsePanel;
