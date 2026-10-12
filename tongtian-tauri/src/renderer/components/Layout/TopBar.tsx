/**
 * 通天办公 - 顶部栏组件 (AionUi 风格 v2)
 *
 * 功能:
 * - 页面标题（跟随路由）
 * - 合规硬开关指示（读取 compliance:status，点击写入同一份出域控制策略）
 * - 工作区切换入口
 * - 设置入口
 *
 * 注意：当前 MainLayout 采用「Sidebar + 页面自带头部」结构，未挂载本组件；
 *      开关状态一律来自合规通道，不再使用本地假状态。
 */

import React, { useEffect, useState } from 'react';
import {
  Box,
  Typography,
  IconButton,
  Tooltip,
  MenuItem,
  Menu,
  useTheme,
  Stack,
} from '@mui/material';
import {
  Settings as SettingsIcon,
  FolderOpen as WorkspaceIcon,
  KeyboardArrowDown as ArrowDownIcon,
} from '@mui/icons-material';
import { useNavigate, useLocation } from 'react-router-dom';
import { useSecurityStore } from '../../store/securityStore';
import { errorTextOf } from '../../../shared/error-messages';

// ── 路由标题映射 ──────────────────────────────────────────────────────

const routeTitles: Record<string, string> = {
  '/': '工作台',
  '/model-hub': '模型中心',
  '/security': '安全合规',
  '/skill-market': '技能市场',
  '/workspace': '工作区',
  '/files': '文件管理',
  '/schedule': '定时任务',
  '/settings': '设置',
};

// ── TopBar 组件 ──────────────────────────────────────────────────────

interface TopBarProps {
  open: boolean;
  onDrawerToggle: () => void;
  currentWorkspace?: string;
}

const TopBar: React.FC<TopBarProps> = ({
  currentWorkspace = '默认工作区',
}) => {
  const theme = useTheme();
  const navigate = useNavigate();
  const location = useLocation();

  const title = routeTitles[location.pathname] ?? '通天办公';

  // 合规硬开关：与合规概览/出域控制共用同一份策略，不维护本地副本
  const compliance = useSecurityStore((s) => s.compliance);
  const loadCompliance = useSecurityStore((s) => s.loadCompliance);
  const updateCompliance = useSecurityStore((s) => s.updateCompliance);
  const [switching, setSwitching] = useState(false);
  const [switchError, setSwitchError] = useState<string | null>(null);

  useEffect(() => {
    void loadCompliance();
  }, [loadCompliance]);

  const known = compliance != null;
  const hardSwitchOn = Boolean(compliance?.hardSwitchEnabled);

  const toggleHardSwitch = async () => {
    if (switching) return;
    try {
      setSwitching(true);
      setSwitchError(null);
      await updateCompliance({ hardSwitchEnabled: !hardSwitchOn });
    } catch (err) {
      setSwitchError(errorTextOf(err, '合规策略更新失败'));
    } finally {
      setSwitching(false);
    }
  };

  // 工作区菜单
  const [anchorEl, setAnchorEl] = React.useState<null | HTMLElement>(null);
  const openMenu = Boolean(anchorEl);

  return (
    <Box
      sx={{
        height: 56,
        flexShrink: 0,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        px: 3,
        background: theme.palette.background.paper,
        borderBottom: `1px solid ${theme.palette.divider}`,
      }}
    >
      {/* 左侧：页面标题 + 工作区切换 */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, minWidth: 0 }}>
        <Typography sx={{ fontSize: '16px', fontWeight: 600, color: '#0F172A' }}>
          {title}
        </Typography>

        {/* 工作区切换器 */}
        <Box
          onClick={(e) => setAnchorEl(e.currentTarget)}
          sx={{
            display: 'flex',
            alignItems: 'center',
            gap: 0.5,
            px: 1.25,
            py: 0.5,
            borderRadius: '8px',
            cursor: 'pointer',
            background: openMenu ? 'rgba(15, 23, 42, 0.05)' : 'transparent',
            transition: 'background 0.15s ease',
            '&:hover': {
              background: 'rgba(15, 23, 42, 0.05)',
            },
          }}
        >
          <WorkspaceIcon sx={{ fontSize: 16, color: '#64748B' }} />
          <Typography sx={{ fontSize: '13px', color: '#64748B', fontWeight: 500 }}>
            {currentWorkspace}
          </Typography>
          <ArrowDownIcon sx={{ fontSize: 16, color: '#94A3B8' }} />
        </Box>
      </Box>

      {/* 右侧：模式指示 + 操作按钮 */}
      <Stack direction="row" spacing={1} alignItems="center">
        {/* 合规模式指示：点击切换硬开关（禁用所有外部模型 API） */}
        <Tooltip
          title={
            switchError ??
            (known
              ? hardSwitchOn
                ? '合规模式：外部模型 API 已被硬开关拦截，点击恢复正常模式'
                : '正常模式：点击开启合规模式（一键禁用所有外部模型 API）'
              : '合规状态尚未加载，暂不可切换')
          }
        >
          <Box
            role="button"
            aria-pressed={hardSwitchOn}
            onClick={() => (known && !switching ? void toggleHardSwitch() : undefined)}
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 0.75,
              px: 1.25,
              py: 0.5,
              borderRadius: '999px',
              cursor: known && !switching ? 'pointer' : 'default',
              background: !known
                ? 'rgba(100, 116, 139, 0.1)'
                : hardSwitchOn
                  ? 'rgba(245, 158, 11, 0.1)'
                  : 'rgba(34, 197, 94, 0.1)',
            }}
          >
            <Box
              sx={{
                width: 6,
                height: 6,
                borderRadius: '50%',
                backgroundColor: !known ? '#94A3B8' : hardSwitchOn ? '#F59E0B' : '#22C55E',
              }}
            />
            <Typography
              sx={{
                fontSize: '12px',
                fontWeight: 600,
                color: !known ? '#64748B' : hardSwitchOn ? '#B45309' : '#15803D',
              }}
            >
              {switching
                ? '切换中…'
                : !known
                  ? '合规状态未知'
                  : hardSwitchOn
                    ? '合规模式'
                    : '正常模式'}
            </Typography>
          </Box>
        </Tooltip>

        {/* 设置入口 */}
        <Tooltip title="设置">
          <IconButton
            size="small"
            onClick={() => navigate('/settings')}
            sx={{ color: '#64748B', '&:hover': { color: '#0F172A' } }}
          >
            <SettingsIcon fontSize="small" />
          </IconButton>
        </Tooltip>

        {/* 用户头像 */}
        <Tooltip title="用户">
          <Box
            sx={{
              width: 30,
              height: 30,
              borderRadius: '50%',
              background: 'linear-gradient(135deg, #22C55E 0%, #0EA5E9 100%)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              cursor: 'pointer',
              boxShadow: '0 1px 4px rgba(15, 23, 42, 0.15)',
            }}
          >
            <Typography sx={{ color: '#fff', fontSize: '12px', fontWeight: 700 }}>U</Typography>
          </Box>
        </Tooltip>
      </Stack>

      {/* 工作区选择菜单 */}
      <Menu
        anchorEl={anchorEl}
        open={openMenu}
        onClose={() => setAnchorEl(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        transformOrigin={{ vertical: 'top', horizontal: 'left' }}
        PaperProps={{ sx: { minWidth: 200, mt: 0.5 } }}
      >
        <MenuItem onClick={() => { setAnchorEl(null); navigate('/workspace'); }}>
          管理所有工作区
        </MenuItem>
        <MenuItem selected onClick={() => setAnchorEl(null)}>
          默认工作区（当前）
        </MenuItem>
      </Menu>
    </Box>
  );
};

export default TopBar;
