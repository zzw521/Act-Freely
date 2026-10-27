/**
 * 通天办公 - 侧边栏导航组件（轻快多彩风格 v4）
 *
 * 设计要点：
 * - 柔和渐变底色 + 每项导航独立彩色图标块（蓝/紫/粉/橙/绿…）
 * - 新任务为渐变色主 CTA，强化视觉焦点
 * - 清晰的分组标题（工作区 / 任务），选中态彩色高亮
 * - 底部用户卡片化 + 套餐彩色胶囊
 * - hover/active 均有明确反馈，微动效过渡
 */

import React, { useState, useEffect, useMemo } from 'react';
import {
  Box,
  Drawer,
  Typography,
  IconButton,
  Tooltip,
  Menu,
  Divider,
  CircularProgress,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogContentText,
  DialogActions,
  Button,
  TextField,
  InputAdornment,
  useTheme,
  useMediaQuery,
} from '@mui/material';
import SearchIcon from '@mui/icons-material/Search';
import AddIcon from '@mui/icons-material/Add';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import KeyboardArrowUpIcon from '@mui/icons-material/KeyboardArrowUp';
import ConstructionIcon from '@mui/icons-material/Construction';
import BoltIcon from '@mui/icons-material/Bolt';
import LinkIcon from '@mui/icons-material/Link';
import ScheduleIcon from '@mui/icons-material/Schedule';
import ForumOutlinedIcon from '@mui/icons-material/ForumOutlined';
import BusinessIcon from '@mui/icons-material/Business';
import TuneIcon from '@mui/icons-material/Tune';
import SettingsIcon from '@mui/icons-material/Settings';
import MenuBookIcon from '@mui/icons-material/MenuBook';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import LogoutIcon from '@mui/icons-material/Logout';
import LoginIcon from '@mui/icons-material/Login';
import ChevronLeftIcon from '@mui/icons-material/ChevronLeft';
import ArchiveIcon from '@mui/icons-material/Archive';
import { useNavigate, useLocation } from 'react-router-dom';
import { useWorkbenchStore } from '../../store/workbenchStore';
import { useImChannelStore, imChannelMeta } from '../../stores/imChannelStore';
import { useAuthStore } from '../../stores/useAuthStore';
import { useBrandingStore } from '../../stores/useBrandingStore';
import { useAppStore } from '../../stores/useAppStore';
import { useAppSettings } from '../../lib/use-app-settings';
import { systemAPI } from '../../lib/api';
import { useShallow } from 'zustand/react/shallow';
import { errorTextOf } from '../../../shared/error-messages';
import { navPalette, type NavPaletteKey } from '../../theme/tokens';
import { useI18n } from '../../i18n';
import brandLogo from '../../assets/brand-logo.png';

// ── 导航项定义 ──────────────────────────────────────────────────────

interface NavItem {
  id: string;
  label: string;
  path: string;
  icon: React.ElementType;
  /** 导航专属配色 */
  color: NavPaletteKey;
  /** 占位能力：禁用 + tooltip「即将上线」（占位清单规范） */
  disabled?: boolean;
}

// 能力管理子项（专家套件/技能/连接器，定时任务与 IM 频道独立展示）
// 浏览器 AI+、终端 AI+、电脑操控、远程桌面属于执行能力配置，统一收进「设置 → 执行能力」；
// 专家团队作为「专家套件」的一个页签展示，不再单独占导航位。
const extensionItems: NavItem[] = [
  { id: 'expert-suites', label: '专家套件', path: '/expert-suites', icon: ConstructionIcon, color: 'amber' },
  { id: 'knowledge', label: '知识库', path: '/knowledge', icon: MenuBookIcon, color: 'blue' },
  { id: 'skills', label: '技能', path: '/extensions?tab=skills', icon: BoltIcon, color: 'purple' },
  { id: 'connector', label: '连接器', path: '/extensions?tab=connectors', icon: LinkIcon, color: 'cyan' },
];

// 用户菜单项（带配色）
const userMenuItems = [
  { id: 'team', label: '项目协作', icon: BusinessIcon, color: 'blue' as NavPaletteKey, path: '/collaboration' },
  { id: 'prefs', label: '偏好设置', icon: TuneIcon, color: 'teal' as NavPaletteKey, path: '/settings' },
  { id: 'settings', label: '设置', icon: SettingsIcon, color: 'slate' as NavPaletteKey, path: '/settings' },
  { id: 'guide', label: '新人指南', icon: MenuBookIcon, color: 'green' as NavPaletteKey },
  { id: 'about', label: '关于我们', icon: InfoOutlinedIcon, color: 'cyan' as NavPaletteKey },
  { id: 'logout', label: '退出登录', icon: LogoutIcon, color: 'slate' as NavPaletteKey },
];

// ── Sidebar 组件 ──────────────────────────────────────────────────────

const DRAWER_WIDTH = 240;
const DRAWER_WIDTH_COLLAPSED = 68;

interface SidebarProps {
  open: boolean;
  onDrawerToggle: () => void;
  /** 展开宽度（可由 MainLayout 拖拽调节） */
  panelWidth?: number;
}

const Sidebar: React.FC<SidebarProps> = ({ open, onDrawerToggle, panelWidth }) => {
  // t 在本文件里被会话列表项占用（map((t) => ...)），翻译函数用 tr 避免遮蔽
  const { t: tr } = useI18n();
  const theme = useTheme();
  const navigate = useNavigate();
  const location = useLocation();
  const isMobile = useMediaQuery(theme.breakpoints.down('md'));
  const isCollapsed = open === false;

  // 品牌外观（设置页可单次自定义；拉取前先按默认「任我行」渲染）
  const brandName = useBrandingStore((s) => s.info.name);
  const brandIcon = useBrandingStore((s) => s.info.iconDataUrl);

  const [extExpanded, setExtExpanded] = useState(true);
  const [userAnchor, setUserAnchor] = useState<null | HTMLElement>(null);
  /** 退出登录确认弹窗 */
  const [logoutConfirmOpen, setLogoutConfirmOpen] = useState(false);
  /** 任务/频道并排页签：软件端发起 → 任务；IM 发起 → 频道 */
  const [activeTab, setActiveTab] = useState<'tasks' | 'channels'>('tasks');
  /** 应用版本号：向 Rust system_info 要（浏览器通道用构建期注入值），不在前端写死 */
  const [appVersion, setAppVersion] = useState('');
  useEffect(() => {
    void (async () => {
      try {
        const info = await systemAPI.info();
        if (typeof info.version === 'string') setAppVersion(info.version);
      } catch {
        // 读不到就不显示版本号，不回落一个写死的字符串
      }
    })();
  }, []);

  // 账号登录态（网页验证登录 · Loopback 方案）
  const { loggedIn, profile, busy: authBusy, init: authInit, login: authLogin, logout: authLogout } = useAuthStore(
    useShallow((s) => ({
      loggedIn: s.loggedIn,
      profile: s.profile,
      busy: s.busy,
      init: s.init,
      login: s.login,
      logout: s.logout,
    })),
  );
  const showNotification = useAppStore((s) => s.showNotification);

  useEffect(() => {
    void authInit();
  }, [authInit]);

  /** 登录：默认拉起浏览器完成综合服务端验证 + loopback 回调；TT_AUTH_MOCK=true 时模拟直登 */
  const handleLogin = async () => {
    const ok = await authLogin();
    if (ok) {
      const nick = useAuthStore.getState().profile?.nickname ?? '';
      showNotification(nick ? tr('登录成功：{nick}', { nick }) : tr('登录成功'), 'success');
    } else {
      showNotification(useAuthStore.getState().error ?? tr('登录失败，请重试'), 'error');
    }
  };

  /** 退出登录（确认后清理登录态；本地任务/会话数据保留） */
  const handleLogoutConfirm = async () => {
    setLogoutConfirmOpen(false);
    const ok = await authLogout();
    showNotification(ok ? tr('已退出登录') : tr('退出失败，请重试'), ok ? 'success' : 'error');
  };

  // 多会话列表（侧边栏任务区：点击切换会话，支持多会话并行）
  const { conversations, newConversation, switchConversation, archiveConversation } = useWorkbenchStore(
    useShallow((s) => ({
      conversations: s.conversations,
      newConversation: s.newConversation,
      switchConversation: s.switchConversation,
      archiveConversation: s.archiveConversation,
    })),
  );

  /** 归档会话：写归档标记后从任务列表移除（归档页可恢复） */
  const handleArchive = async (id: string, title: string) => {
    try {
      await archiveConversation(id);
      showNotification(tr('已归档：{title}，可在「已归档」恢复', { title: title || tr('未命名任务') }), 'success');
    } catch (err) {
      showNotification(errorTextOf(err, tr('归档失败')), 'error');
    }
  };

  // IM 频道会话列表（频道页签数据源）
  const { channelSessions, channels, loadSessions } = useImChannelStore(
    useShallow((s) => ({
      channelSessions: s.channelSessions,
      channels: s.channels,
      loadSessions: s.loadSessions,
    })),
  );

  // 头像色（个人资料设置真实生效）
  const [appSettings] = useAppSettings();
  const avatarColor = (appSettings.avatarColor as string) ?? '';

  // 对话列表筛选（实验开关）：标题搜索 + 创建时间范围，只影响侧栏展示
  const experiments = (appSettings.experiments as Record<string, boolean> | undefined) ?? {};
  const filterEnabled = Boolean(experiments.filter);
  const [taskQuery, setTaskQuery] = useState('');
  const [taskRange, setTaskRange] = useState<'all' | '7d' | '30d'>('all');
  const visibleConversations = useMemo(() => {
    if (!filterEnabled) return conversations;
    const q = taskQuery.trim().toLowerCase();
    const since =
      taskRange === 'all' ? 0 : Date.now() - (taskRange === '7d' ? 7 : 30) * 24 * 60 * 60 * 1000;
    return conversations.filter((c) => {
      if (since > 0 && c.createdAt < since) return false;
      if (q && !(c.title || '新任务').toLowerCase().includes(q)) return false;
      return true;
    });
  }, [conversations, filterEnabled, taskQuery, taskRange]);

  // 长连接由引擎进程持有，引擎不会往界面推事件，所以「频道」页签的数据只能按固定间隔问。
  // 这里常驻，因此由它统一负责刷新：过渡态（connecting/reconnecting）等状态落定，
  // 已连接时等新的入站消息——不盯后者的话，用户从 IM 会话里 @完机器人，
  // 界面上的会话列表会一直停在空状态，看起来像「频道没接通」。
  const imNeedsPolling = channels.some((c) =>
    ['connected', 'connecting', 'reconnecting'].includes(c.state),
  );
  useEffect(() => {
    void loadSessions();
    if (!imNeedsPolling) return undefined;
    const timer = window.setInterval(() => void loadSessions(), 4000);
    return () => window.clearInterval(timer);
  }, [imNeedsPolling, loadSessions]);

  // 路由变化时关闭个人中心菜单，避免跳转后弹窗锚点失效残留
  useEffect(() => {
    setUserAnchor(null);
  }, [location.pathname, location.search]);

  const handleNavClick = (path: string) => {
    navigate(path);
    if (isMobile) onDrawerToggle();
  };

  // 支持带查询参数的路径匹配（如 /settings?s=models）与新页面路由
  const isNavActive = (path: string) => {
    if (path.includes('?')) {
      return location.pathname + location.search === path;
    }
    return location.pathname === path;
  };

  const width = open ? (panelWidth ?? DRAWER_WIDTH) : DRAWER_WIDTH_COLLAPSED;

  // ── 彩色图标块 ──────────────────────────────────────────

  const IconChip: React.FC<{ color: NavPaletteKey; icon: React.ElementType; size?: number; active?: boolean }> = ({
    color,
    icon: Icon,
    size = 26,
    active = false,
  }) => {
    const c = navPalette[color];
    return (
      <Box
        sx={{
          width: size,
          height: size,
          borderRadius: '8px',
          background: active ? c.main : c.bg,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
          transition: 'background 0.18s ease, transform 0.18s ease',
        }}
      >
        <Icon sx={{ fontSize: size * 0.6, color: active ? '#FFFFFF' : c.main, transition: 'color 0.18s ease' }} />
      </Box>
    );
  };

  // ── 导航行渲染 ────────────────────────────────────────────

  const renderNavItem = (item: NavItem, active: boolean, indent = false) => {
    const c = navPalette[item.color];
    const button = (
      <Box
        key={item.id}
        onClick={() => !item.disabled && handleNavClick(item.path)}
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 1,
          px: 0.75,
          py: 0.6,
          mb: 0.25,
          ml: indent ? 1.5 : 0,
          borderRadius: '10px',
          cursor: item.disabled ? 'not-allowed' : 'pointer',
          opacity: item.disabled ? 0.45 : 1,
          justifyContent: isCollapsed ? 'center' : 'flex-start',
          background: active ? c.activeBg : 'transparent',
          transition: 'background 0.18s ease, transform 0.12s ease',
          '&:hover': item.disabled
            ? {}
            : {
                background: active ? c.activeBg : 'rgba(99, 102, 241, 0.06)',
                '& .nav-icon-chip': { transform: 'scale(1.08)' },
              },
          '& .nav-icon-chip': { transition: 'transform 0.18s ease, background 0.18s ease' },
        }}
      >
        <Box className="nav-icon-chip">
          <IconChip color={item.color} icon={item.icon} active={active} />
        </Box>
        {!isCollapsed && (
          <Typography
            sx={{
              fontSize: '13px',
              fontWeight: active ? 600 : 500,
              color: active ? c.main : '#3F4756',
              transition: 'color 0.15s ease',
            }}
          >
            {tr(item.label)}
          </Typography>
        )}
      </Box>
    );

    // 占位能力：禁用态 + tooltip（占位清单规范）
    if (item.disabled) {
      return (
        <Tooltip
          key={item.id}
          title={tr('{label} · 即将上线', { label: tr(item.label) })}
          placement="right"
        >
          {button}
        </Tooltip>
      );
    }
    return isCollapsed ? (
      <Tooltip key={item.id} title={tr(item.label)} placement="right">
        {button}
      </Tooltip>
    ) : (
      button
    );
  };

  return (
    <Drawer
      variant="permanent"
      open={open}
      onClose={onDrawerToggle}
      ModalProps={{ keepMounted: true }}
      sx={{
        width,
        flexShrink: 0,
        '& .MuiDrawer-paper': {
          width,
          boxSizing: 'border-box',
          border: 'none',
          // 轻快多彩：柔和冷色渐变底 + 右侧细腻投影替代硬边框
          background: 'var(--tt-sidebar-bg)',
          boxShadow: '4px 0 18px rgba(99, 102, 241, 0.05)',
          transition: theme.transitions.create(['width'], {
            easing: theme.transitions.easing.sharp,
            duration: theme.transitions.duration.enteringScreen,
          }),
          overflowX: 'hidden',
        },
      }}
    >
      {/* ── 品牌区 ─────────────────────────────────────── */}
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 0.85,
          px: isCollapsed ? 0 : 2,
          pt: 2,
          pb: 1.5,
          justifyContent: isCollapsed ? 'center' : 'flex-start',
        }}
      >
        {/* Logo：自定义图标优先，否则默认品牌图（白底圆角块保证浅色侧栏上的对比度） */}
        {brandIcon ? (
          <Box
            component="img"
            src={brandIcon}
            alt={brandName}
            sx={{
              width: 28,
              height: 28,
              borderRadius: '9px',
              flexShrink: 0,
              objectFit: 'cover',
              boxShadow: '0 3px 10px rgba(15, 23, 42, 0.18)',
            }}
          />
        ) : (
          <Box
            sx={{
              width: 28,
              height: 28,
              borderRadius: '9px',
              flexShrink: 0,
              bgcolor: '#fff',
              border: '1px solid rgba(15, 23, 42, 0.07)',
              boxShadow: '0 3px 10px rgba(15, 23, 42, 0.12)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
            }}
          >
            <Box
              component="img"
              src={brandLogo}
              alt={brandName}
              sx={{ width: 22, height: 22, objectFit: 'contain', display: 'block' }}
            />
          </Box>
        )}

        {!isCollapsed && (
          <>
            <Typography
              sx={{
                fontSize: '16px',
                fontWeight: 800,
                letterSpacing: '0.02em',
                maxWidth: 132,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                background: 'linear-gradient(90deg, #4F46E5 0%, #7C3AED 60%, #0EA5E9 100%)',
                WebkitBackgroundClip: 'text',
                WebkitTextFillColor: 'transparent',
              }}
            >
              {brandName}
            </Typography>
            <Box
              sx={{
                px: 0.75,
                py: 0.15,
                borderRadius: '999px',
                background: 'linear-gradient(90deg, rgba(139, 92, 246, 0.12), rgba(6, 182, 212, 0.12))',
              }}
            >
              <Typography sx={{ fontSize: '9px', fontWeight: 700, color: '#7C3AED', letterSpacing: '0.08em' }}>
                BETA
              </Typography>
            </Box>
            <Box sx={{ flexGrow: 1 }} />
            <IconButton size="small" onClick={onDrawerToggle} sx={{ color: 'var(--tt-text-4)' }}>
              <ChevronLeftIcon sx={{ fontSize: 18 }} />
            </IconButton>
          </>
        )}
      </Box>

      {/* ── 导航区 ─────────────────────────────────────── */}
      <Box sx={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', px: isCollapsed ? 1 : 1.5 }}>
        {/* 新任务：渐变色主 CTA */}
        {(() => {
          const active = location.pathname === '/';
          const cta = (
            <Box
              onClick={() => {
                newConversation();
                handleNavClick('/');
              }}
              sx={{
                display: 'flex',
                alignItems: 'center',
                gap: 1,
                px: isCollapsed ? 0 : 1.25,
                py: isCollapsed ? 1 : 0.9,
                mb: 1,
                borderRadius: '12px',
                cursor: 'pointer',
                justifyContent: 'center',
                background: active
                  ? 'linear-gradient(120deg, #4F46E5 0%, #7C3AED 55%, #0EA5E9 110%)'
                  : 'linear-gradient(120deg, #6366F1 0%, #8B5CF6 55%, #06B6D4 110%)',
                boxShadow: '0 4px 14px rgba(99, 102, 241, 0.32)',
                transition: 'transform 0.15s ease, box-shadow 0.15s ease, filter 0.15s ease',
                '&:hover': {
                  transform: 'translateY(-1px)',
                  boxShadow: '0 6px 18px rgba(99, 102, 241, 0.4)',
                  filter: 'brightness(1.04)',
                },
                '&:active': { transform: 'translateY(0)', boxShadow: '0 3px 10px rgba(99, 102, 241, 0.3)' },
              }}
            >
              <Box
                sx={{
                  width: 20,
                  height: 20,
                  borderRadius: '7px',
                  background: 'rgba(255, 255, 255, 0.22)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  flexShrink: 0,
                }}
              >
                <AddIcon sx={{ fontSize: 15, color: '#fff' }} />
              </Box>
              {!isCollapsed && (
                <Typography sx={{ fontSize: '13.5px', fontWeight: 700, color: '#fff', letterSpacing: '0.02em' }}>
                  {tr('新任务')}
                </Typography>
              )}
            </Box>
          );
          return isCollapsed ? (
            <Tooltip title={tr('新任务')} placement="right">
              {cta}
            </Tooltip>
          ) : (
            cta
          );
        })()}

        {/* 能力管理分组（专家套件/技能/连接器） */}
        {!isCollapsed && (
          <Box
            onClick={() => setExtExpanded(!extExpanded)}
            sx={{
              display: 'flex',
              alignItems: 'center',
              px: 1,
              py: 0.5,
              mb: 0.5,
              borderRadius: '8px',
              cursor: 'pointer',
              transition: 'background 0.15s ease',
              '&:hover': { background: 'rgba(99, 102, 241, 0.06)' },
            }}
          >
            <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: 'var(--tt-text-2)', flexGrow: 1 }}>
              {tr('能力管理')}
            </Typography>
            <KeyboardArrowDownIcon
              sx={{
                fontSize: 16,
                color: 'var(--tt-text-4)',
                transform: extExpanded ? 'rotate(180deg)' : 'none',
                transition: 'transform 0.2s ease',
              }}
            />
          </Box>
        )}

        {(extExpanded || isCollapsed) &&
          extensionItems.map((item) => renderNavItem(item, isNavActive(item.path), !isCollapsed))}

        {/* 定时任务 / IM 频道：独立于能力管理，顶层展示 */}
        {renderNavItem(
          { id: 'schedule', label: '定时任务', path: '/schedule', icon: ScheduleIcon, color: 'orange' },
          isNavActive('/schedule'),
        )}
        {renderNavItem(
          { id: 'im', label: 'IM 频道', path: '/im', icon: ForumOutlinedIcon, color: 'pink' },
          isNavActive('/im'),
        )}

        {/* 任务/频道并排页签：软件端发起归任务，IM 发起归频道 */}
        {!isCollapsed && (
          <Box sx={{ display: 'flex', gap: 0.5, px: 0.5, pt: 1.25, pb: 0.25 }}>
            {(
              [
                { key: 'tasks', label: tr('任务'), color: '#4F46E5' },
                { key: 'channels', label: tr('频道'), color: '#06B6D4' },
              ] as const
            ).map((tab) => {
              const active = activeTab === tab.key;
              return (
                <Box
                  key={tab.key}
                  onClick={() => setActiveTab(tab.key)}
                  sx={{
                    flex: 1,
                    display: 'flex',
                    flexDirection: 'column',
                    alignItems: 'center',
                    gap: 0.5,
                    py: 0.5,
                    borderRadius: '8px 8px 0 0',
                    cursor: 'pointer',
                    transition: 'background 0.15s ease',
                    '&:hover': { background: active ? 'transparent' : 'rgba(99, 102, 241, 0.05)' },
                  }}
                >
                  <Typography
                    sx={{
                      fontSize: '12.5px',
                      fontWeight: active ? 700 : 500,
                      color: active ? tab.color : '#9AA3B2',
                      transition: 'color 0.15s ease',
                    }}
                  >
                    {tab.label}
                  </Typography>
                  <Box
                    sx={{
                      width: 20,
                      height: 3,
                      borderRadius: '999px',
                      background: active
                        ? `linear-gradient(90deg, ${tab.color}, ${tab.key === 'tasks' ? '#8B5CF6' : '#22D3EE'})`
                        : 'transparent',
                      transition: 'background 0.2s ease',
                    }}
                  />
                </Box>
              );
            })}
          </Box>
        )}

        {/* 对话筛选（实验开关）：标题搜索 + 创建时间范围 */}
        {!isCollapsed && activeTab === 'tasks' && filterEnabled && (
          <Box sx={{ px: 0.5, pt: 0.75, pb: 0.25 }}>
            <TextField
              value={taskQuery}
              onChange={(e) => setTaskQuery(e.target.value)}
              placeholder={tr('搜索任务标题')}
              size="small"
              fullWidth
              InputProps={{
                startAdornment: (
                  <InputAdornment position="start">
                    <SearchIcon sx={{ fontSize: 15, color: 'var(--tt-text-4)' }} />
                  </InputAdornment>
                ),
              }}
              sx={{
                '& .MuiInputBase-root': {
                  fontSize: '12.5px',
                  height: 30,
                  borderRadius: '8px',
                  bgcolor: 'var(--tt-surface)',
                },
                '& fieldset': { borderColor: 'var(--tt-border-soft)' },
              }}
            />
            <Box sx={{ display: 'flex', gap: 0.5, mt: 0.5 }}>
              {(
                [
                  { key: 'all', label: tr('全部') },
                  { key: '7d', label: tr('近 7 天') },
                  { key: '30d', label: tr('近 30 天') },
                ] as const
              ).map((r) => (
                <Box
                  key={r.key}
                  onClick={() => setTaskRange(r.key)}
                  sx={{
                    px: 0.75,
                    py: 0.25,
                    borderRadius: '6px',
                    cursor: 'pointer',
                    fontSize: '11.5px',
                    color: taskRange === r.key ? '#4338CA' : 'var(--tt-text-4)',
                    bgcolor: taskRange === r.key ? 'rgba(79, 70, 229, 0.10)' : 'transparent',
                    fontWeight: taskRange === r.key ? 600 : 400,
                    transition: 'background 0.15s ease, color 0.15s ease',
                  }}
                >
                  {r.label}
                </Box>
              ))}
            </Box>
          </Box>
        )}

        {/* 任务页签：会话列表（多会话并行，点击切换） */}
        {!isCollapsed &&
          activeTab === 'tasks' &&
          (visibleConversations.length > 0 ? (
            visibleConversations.slice(0, 20).map((t) => (
              <Box
                key={t.id}
                onClick={() => {
                  switchConversation(t.id);
                  handleNavClick('/');
                }}
                sx={{
                  mt: 0.25,
                  px: 1,
                  py: 0.65,
                  borderRadius: '8px',
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 0.75,
                  background: 'transparent',
                  borderLeft: '2px solid transparent',
                  transition: 'background 0.15s ease, border-color 0.15s ease',
                  '&:hover': {
                    background: 'rgba(79, 70, 229, 0.06)',
                    borderLeftColor: '#A5B4FC',
                    '& .history-title': { color: '#4338CA' },
                    '& .conv-archive-btn': { opacity: 1 },
                  },
                }}
              >
                {/* 运行中的会话显示转圈指示 */}
                {t.isLoading && (
                  <CircularProgress size={10} sx={{ color: '#22C55E', flexShrink: 0 }} />
                )}
                <Typography
                  className="history-title"
                  sx={{
                    fontSize: '12.5px',
                    color: 'var(--tt-text-2)',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                    minWidth: 0,
                    flexGrow: 1,
                    transition: 'color 0.15s ease',
                  }}
                >
                  {t.title || tr('新任务')}
                </Typography>
                {/* hover 显现归档入口（写入 task:archive 通道） */}
                <Tooltip title={tr('归档')} placement="left">
                  <IconButton
                    className="conv-archive-btn"
                    size="small"
                    onClick={(e) => {
                      e.stopPropagation();
                      void handleArchive(t.id, t.title);
                    }}
                    sx={{
                      opacity: 0,
                      color: 'var(--tt-text-4)',
                      flexShrink: 0,
                      transition: 'opacity 0.15s ease, color 0.15s ease',
                      '&:hover': { color: '#4F46E5', bgcolor: 'transparent' },
                    }}
                  >
                    <ArchiveIcon sx={{ fontSize: 14 }} />
                  </IconButton>
                </Tooltip>
              </Box>
            ))
          ) : (
            <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', px: 1, py: 1 }}>
              {filterEnabled && (taskQuery.trim() !== '' || taskRange !== 'all')
                ? tr('没有符合筛选条件的任务')
                : tr('暂无任务，点击上方「新任务」开始')}
            </Typography>
          ))}

        {/* 频道页签：通过 IM 发起的会话 */}
        {!isCollapsed &&
          activeTab === 'channels' &&
          (channelSessions.length > 0 ? (
            channelSessions.slice(0, 20).map((s) => {
              const meta = imChannelMeta(s.source, channels.find((c) => c.kind === s.source)?.label);
              return (
                <Box
                  key={s.id}
                  onClick={() => handleNavClick('/')}
                  sx={{
                    mt: 0.25,
                    px: 1,
                    py: 0.65,
                    borderRadius: '8px',
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 0.75,
                    background: 'transparent',
                    borderLeft: '2px solid transparent',
                    transition: 'background 0.15s ease, border-color 0.15s ease',
                    '&:hover': {
                      background: 'rgba(6, 182, 212, 0.06)',
                      borderLeftColor: '#67E8F9',
                      '& .history-title': { color: '#0E7490' },
                    },
                  }}
                >
                  {/* 渠道来源小圆点 */}
                  <Tooltip title={meta.label} placement="right">
                    <Box
                      component="span"
                      sx={{
                        width: 7,
                        height: 7,
                        borderRadius: '50%',
                        background: meta.color,
                        flexShrink: 0,
                      }}
                    />
                  </Tooltip>
                  <Typography
                    className="history-title"
                    sx={{
                      fontSize: '12.5px',
                      color: 'var(--tt-text-2)',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                      minWidth: 0,
                      transition: 'color 0.15s ease',
                    }}
                  >
                    {s.title}
                  </Typography>
                </Box>
              );
            })
          ) : (
            <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', px: 1, py: 1 }}>
              {tr('暂无 IM 会话，在「IM 频道」开通渠道后即可接收')}
            </Typography>
          ))}
      </Box>

      {/* ── 底部用户卡片 ────────────────────────────────── */}
      <Box sx={{ p: isCollapsed ? 1 : 1.5 }}>
        <Box
          onClick={(e) => {
            // 未登录：点击直接触发网页验证登录；已登录：打开个人中心菜单
            if (!loggedIn) {
              void handleLogin();
              return;
            }
            setUserAnchor(e.currentTarget);
          }}
          sx={{
            display: 'flex',
            alignItems: 'center',
            gap: 1,
            px: isCollapsed ? 0 : 1.25,
            py: isCollapsed ? 1 : 0.85,
            borderRadius: '14px',
            cursor: 'pointer',
            justifyContent: isCollapsed ? 'center' : 'flex-start',
            background: 'var(--tt-surface)',
            border: '1px solid rgba(99, 102, 241, 0.10)',
            boxShadow: '0 2px 8px rgba(99, 102, 241, 0.06)',
            transition: 'box-shadow 0.18s ease, transform 0.15s ease',
            '&:hover': {
              boxShadow: '0 4px 14px rgba(99, 102, 241, 0.12)',
              transform: 'translateY(-1px)',
            },
          }}
        >
          {/* 头像：设置头像色则用纯色，否则渐变圆 */}
          <Box
            sx={{
              width: 34,
              height: 34,
              borderRadius: '50%',
              background: avatarColor || 'linear-gradient(135deg, #6366F1 0%, #A855F7 100%)',
              boxShadow: avatarColor ? '0 2px 8px rgba(15, 23, 42, 0.2)' : '0 2px 8px rgba(139, 92, 246, 0.3)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexShrink: 0,
            }}
          >
            <Typography sx={{ color: '#fff', fontSize: '14px', fontWeight: 700 }}>
              {loggedIn ? profile?.nickname?.charAt(0) || tr('客') : tr('客')}
            </Typography>
          </Box>

          {!isCollapsed && (
            <>
              <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                <Typography sx={{ fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-1)', lineHeight: 1.35 }}>
                  {loggedIn ? profile?.nickname ?? tr('未命名用户') : tr('未登录')}
                </Typography>
                {/* 套餐彩色胶囊（登录态动态展示；未登录提示点击登录） */}
                <Box
                  component="span"
                  sx={{
                    display: 'inline-block',
                    mt: 0.15,
                    px: 0.75,
                    borderRadius: '999px',
                    background: loggedIn ? 'rgba(16, 185, 129, 0.10)' : 'rgba(99, 102, 241, 0.10)',
                  }}
                >
                  <Typography component="span" sx={{ fontSize: '10px', fontWeight: 600, color: loggedIn ? '#059669' : '#4F46E5', lineHeight: 1.7 }}>
                    {loggedIn ? profile?.plan ?? tr('个人免费版') : authBusy ? tr('登录中…') : tr('点击登录网页验证')}
                  </Typography>
                </Box>
              </Box>
              <KeyboardArrowUpIcon sx={{ fontSize: 18, color: 'var(--tt-text-4)' }} />
            </>
          )}
        </Box>
      </Box>

      {/* ── 个人中心弹窗菜单 ─────────────────────────────── */}
      <Menu
        anchorEl={userAnchor}
        open={Boolean(userAnchor)}
        onClose={() => setUserAnchor(null)}
        anchorOrigin={{ vertical: 'top', horizontal: 'left' }}
        transformOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        PaperProps={{
          sx: {
            width: 248,
            borderRadius: '16px',
            border: '1px solid rgba(99, 102, 241, 0.10)',
            boxShadow: '0 16px 48px rgba(30, 41, 59, 0.16)',
            mb: 0.5,
            p: 1,
            background: 'var(--tt-surface)',
          },
        }}
      >
        {/* 版本信息：这里原来是一个渐变高亮的「升级」胶囊，但没有任何 onClick，
            应用内也不存在购买/升级页面 —— 纯装饰的诱导按钮，已换成真实版本号
            （桌面通道由 Rust system_info 报 CARGO_PKG_VERSION）。 */}
        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', px: 1.5, py: 1 }}>
          <Box>
            <Typography sx={{ fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-1)' }}>
              {loggedIn ? profile?.nickname ?? tr('未命名用户') : tr('未登录')}
            </Typography>
            <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>
              {loggedIn ? profile?.plan ?? tr('个人版') : tr('完成网页验证即可登录')}
            </Typography>
          </Box>
          <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>
            {appVersion ? `v${appVersion}` : tr('版本读取中…')}
          </Typography>
        </Box>

        <Divider sx={{ my: 0.75, borderColor: 'var(--tt-border-soft)' }} />

        {/* 未登录时退出项替换为登录项 */}
        {(loggedIn
          ? userMenuItems
          : userMenuItems.map((it) =>
              it.id === 'logout'
                ? { id: 'login', label: '登录', icon: LoginIcon, color: 'blue' as NavPaletteKey }
                : it,
            )
        ).map((item) => {
          const Icon = item.icon;
          const isLogout = item.id === 'logout';
          const c = navPalette[item.color];
          return (
            <Box
              key={item.id}
              onClick={() => {
                setUserAnchor(null);
                if (item.id === 'logout') {
                  setLogoutConfirmOpen(true);
                  return;
                }
                if (item.id === 'login') {
                  void handleLogin();
                  return;
                }
                if (item.path) navigate(item.path);
              }}
              sx={{
                display: 'flex',
                alignItems: 'center',
                gap: 1.25,
                px: 1.25,
                py: 0.85,
                borderRadius: '10px',
                cursor: 'pointer',
                transition: 'background 0.15s ease',
                '&:hover': {
                  background: isLogout ? 'rgba(239, 68, 68, 0.06)' : 'rgba(99, 102, 241, 0.06)',
                },
              }}
            >
              <Box
                sx={{
                  width: 24,
                  height: 24,
                  borderRadius: '7px',
                  background: isLogout ? 'rgba(239, 68, 68, 0.10)' : c.bg,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                }}
              >
                <Icon sx={{ fontSize: 14, color: isLogout ? '#EF4444' : c.main }} />
              </Box>
              <Typography
                sx={{
                  fontSize: '13px',
                  fontWeight: 500,
                  color: isLogout ? '#EF4444' : '#3F4756',
                  flexGrow: 1,
                }}
              >
                {tr(item.label)}
              </Typography>
            </Box>
          );
        })}
      </Menu>

      {/* ── 退出登录确认弹窗 ─────────────────────── */}
      <Dialog
        open={logoutConfirmOpen}
        onClose={() => setLogoutConfirmOpen(false)}
        PaperProps={{ sx: { borderRadius: '14px', minWidth: 320 } }}
      >
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>{tr('退出登录')}</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ fontSize: '13px', color: 'var(--tt-text-2)' }}>
            {tr('确定要在此设备退出登录吗？退出后本地任务与会话数据仍会保留，可随时重新登录。')}
          </DialogContentText>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setLogoutConfirmOpen(false)} sx={{ color: 'var(--tt-text-3)', fontSize: '13px' }}>
            {tr('取消')}
          </Button>
          <Button
            onClick={() => void handleLogoutConfirm()}
            disabled={authBusy}
            variant="contained"
            sx={{ fontSize: '13px', bgcolor: '#EF4444', '&:hover': { bgcolor: '#DC2626' } }}
          >
            {tr('退出登录')}
          </Button>
        </DialogActions>
      </Dialog>
    </Drawer>
  );
};

export default Sidebar;
