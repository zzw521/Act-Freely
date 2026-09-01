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
import { Box, Typography, Switch } from '@mui/material';
import MonitorIcon from '@mui/icons-material/Monitor';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import RadioButtonUncheckedIcon from '@mui/icons-material/RadioButtonUnchecked';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import PhotoCameraIcon from '@mui/icons-material/PhotoCamera';
import DescriptionIcon from '@mui/icons-material/Description';
import TouchAppIcon from '@mui/icons-material/TouchApp';
import VerifiedIcon from '@mui/icons-material/Verified';
import { getComputerUsePolicy, setComputerUsePolicy, getSettings, setSettings } from '../../lib/alignment-api';

type Policy = 'ask_every_time' | 'auto' | 'disabled';

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

const tealSwitch = {
  '& .MuiSwitch-switchBase.Mui-checked': { color: '#14B8A6' },
  '& .MuiSwitch-switchBase.Mui-checked + .MuiSwitch-track': { backgroundColor: '#14B8A6' },
};

const ComputerUsePanel: React.FC = () => {
  const [policy, setPolicy] = useState<Policy>('ask_every_time');
  const [screenGranted, setScreenGranted] = useState(true);
  const [accessGranted, setAccessGranted] = useState(false);

  // 从持久层加载操控策略（真实 IPC）
  useEffect(() => {
    getComputerUsePolicy()
      .then((p) => setPolicy(p as Policy))
      .catch(() => undefined);
  }, []);

  const handlePolicyChange = (p: Policy) => {
    setPolicy(p);
    setComputerUsePolicy(p).catch(() => undefined);
  };

  const permissions = [
    { id: 'screen', label: '截屏权限', desc: '用于屏幕内容识别与操控感知', granted: screenGranted, onGrant: () => setScreenGranted(true) },
    { id: 'access', label: '辅助功能权限', desc: '用于模拟点击、输入等桌面操作', granted: accessGranted, onGrant: () => setAccessGranted(true) },
  ];

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
            {perm.granted ? (
              <Box sx={{ px: 1.25, py: 0.4, borderRadius: '6px', bgcolor: '#E6F7F4' }}>
                <Typography sx={{ fontSize: '11.5px', fontWeight: 600, color: '#0D9488' }}>已授权</Typography>
              </Box>
            ) : (
              <Box
                onClick={perm.onGrant}
                sx={{
                  px: 1.75,
                  py: 0.5,
                  borderRadius: '8px',
                  bgcolor: '#111827',
                  cursor: 'pointer',
                  '&:hover': { opacity: 0.85 },
                }}
              >
                <Typography sx={{ fontSize: '11.5px', fontWeight: 600, color: '#fff' }}>去授权</Typography>
              </Box>
            )}
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
