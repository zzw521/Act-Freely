/**
 * 通天办公 - 设置页面 (千问对标整改版)
 *
 * 三分组导航（对齐 QwenWork 系统设置结构）:
 * - 通用: 偏好设置/个人资料/系统设置/语音输入/应用快照/快捷键/意识/更新应用/已归档
 * - 扩展与集成: 工作台/模型与用量
 * - 高级设置: 安全工作环境/实验特性
 * 支持 ?s=xxx 深度链接
 */

import React, { useState, useEffect, useRef } from 'react';
import { Box, Typography, IconButton, Switch, Slider, TextField, Button, Chip, CircularProgress, Dialog, DialogTitle, DialogContent, DialogContentText, DialogActions, Tooltip } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import ContentCopyIcon from '@mui/icons-material/ContentCopy';
import PaletteOutlinedIcon from '@mui/icons-material/PaletteOutlined';
import PersonOutlineIcon from '@mui/icons-material/PersonOutline';
import SettingsOutlinedIcon from '@mui/icons-material/SettingsOutlined';
import MicNoneOutlinedIcon from '@mui/icons-material/MicNoneOutlined';
import CameraAltOutlinedIcon from '@mui/icons-material/CameraAltOutlined';
import KeyboardOutlinedIcon from '@mui/icons-material/KeyboardOutlined';
import PsychologyOutlinedIcon from '@mui/icons-material/PsychologyOutlined';
import SystemUpdateAltOutlinedIcon from '@mui/icons-material/SystemUpdateAltOutlined';
import BrandingWatermarkOutlinedIcon from '@mui/icons-material/BrandingWatermarkOutlined';
import ImageOutlinedIcon from '@mui/icons-material/ImageOutlined';
import ArchiveOutlinedIcon from '@mui/icons-material/ArchiveOutlined';
import DashboardOutlinedIcon from '@mui/icons-material/DashboardOutlined';
import MemoryOutlinedIcon from '@mui/icons-material/MemoryOutlined';
import ShieldOutlinedIcon from '@mui/icons-material/ShieldOutlined';
import ScienceOutlinedIcon from '@mui/icons-material/ScienceOutlined';
import AddIcon from '@mui/icons-material/Add';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import StyleOutlinedIcon from '@mui/icons-material/StyleOutlined';
import MenuBookOutlinedIcon from '@mui/icons-material/MenuBookOutlined';
import HistoryOutlinedIcon from '@mui/icons-material/HistoryOutlined';
import FolderOpenOutlinedIcon from '@mui/icons-material/FolderOpenOutlined';
import TerminalOutlinedIcon from '@mui/icons-material/TerminalOutlined';
import LanguageOutlinedIcon from '@mui/icons-material/LanguageOutlined';
import MonitorOutlinedIcon from '@mui/icons-material/MonitorOutlined';
import DesktopWindowsOutlinedIcon from '@mui/icons-material/DesktopWindowsOutlined';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { useWorkbenchStore } from '../../store/workbenchStore';
import { usePreferenceStore } from '../../stores/usePreferenceStore';
import { useAuthStore } from '../../stores/useAuthStore';
import { useBrandingStore } from '../../stores/useBrandingStore';
import { useAppStore } from '../../stores/useAppStore';
import ModelSettings from '../../components/ModelSettings';
import BrowserUsePanel from '../../pages/BrowserUsePanel';
import TerminalPanel from '../../pages/Terminal';
import ComputerUsePanel from '../../pages/ComputerUsePanel';
import RemoteDesktopPanel from '../../pages/RemoteDesktopPanel';
import { getSettings, setSettings, getProxyStatus, getAutostartStatus, getSystemPermissionStatus, openSystemPermissionSettings, getAwareness, updateAwareness, listMemories, addMemory, deleteMemory, getImPreferences, setImPreferences, getTerminalCapabilities, setTerminalEnabled, getAcpConfig, setAcpConfig, getAcpStatus } from '../../lib/alignment-api';
import { parseProxyMode } from '../../lib/proxy-local';
import { permissionLabel } from '../../lib/permissions-local';
import {
  ensureNotificationPermission,
  notificationPermission,
} from '../../lib/notify-local';
import {
  badgeCapability,
  clearTaskbarUnread,
  refreshTaskbarBadge,
  type BadgeCapability,
} from '../../lib/badge-local';
import {
  SHORTCUT_ACTIONS,
  comboFromEvent,
  displayCombo,
  effectiveCombos,
  validateOverride,
  type ShortcutAction,
  type ShortcutOverrides,
} from '../../lib/shortcuts';
import { registeredShortcutIds } from '../../lib/use-app-shortcuts';
import { authAPI, sandboxAPI, systemAPI } from '../../lib/api';
import { isTauri } from '../../lib/tauri-api';
import { fmtAwarenessUpdatedAt } from '../../lib/awareness-local';
import { errorTextOf } from '../../../shared/error-messages';
import { useAppSettings, WORKBENCH_MODE_DEFS } from '../../lib/use-app-settings';
import { usePptxTemplates } from '../../lib/use-pptx-templates';
import type { PptxTemplate } from '../../../shared/pptx-templates';
import type { AuthServerProbe } from '../../../shared/contracts';
import { resolveEngineBaseUrl } from '../../lib/engine-endpoint';
import { engineFetch } from '../../lib/engine-http';
import { useI18n } from '../../i18n';
import brandLogo from '../../assets/brand-logo.png';
import type { AppSettingsData, AcpConfigState, AcpServerStatus, AutostartStatus, AwarenessView, AwarenessPatch, AwarenessFileEntry, AwarenessFileName, AwarenessProfile, AwarenessStyleId, BrandingInfo, ImPreferences, MemoryEntry, PermissionState, PiiOutboundMode, ProxyStatus, SandboxEnvUsage, SystemCapability, SystemPermissionStatus, SystemSandboxView, TerminalCapabilities, ToolApprovalMode } from '../../../shared/contracts';

// ── 三分组分区定义 ─────────────────────────────────────────────────

interface Section {
  id: string;
  label: string;
  icon: React.ElementType;
}

interface SectionGroup {
  group: string;
  sections: Section[];
}

const sectionGroups: SectionGroup[] = [
  {
    group: '通用',
    sections: [
      { id: 'preferences', label: '偏好设置', icon: PaletteOutlinedIcon },
      { id: 'profile', label: '个人资料', icon: PersonOutlineIcon },
      { id: 'system', label: '系统设置', icon: SettingsOutlinedIcon },
      { id: 'voice', label: '语音输入', icon: MicNoneOutlinedIcon },
      { id: 'snapshot', label: '应用快照', icon: CameraAltOutlinedIcon },
      { id: 'shortcuts', label: '快捷键', icon: KeyboardOutlinedIcon },
      { id: 'awareness', label: '意识', icon: PsychologyOutlinedIcon },
      { id: 'branding', label: '品牌外观', icon: BrandingWatermarkOutlinedIcon },
      { id: 'update', label: '更新应用', icon: SystemUpdateAltOutlinedIcon },
      { id: 'archive', label: '已归档', icon: ArchiveOutlinedIcon },
    ],
  },
  {
    group: '扩展与集成',
    sections: [
      { id: 'workbench', label: '工作台', icon: DashboardOutlinedIcon },
      { id: 'models', label: '模型与用量', icon: MemoryOutlinedIcon },
    ],
  },
  {
    group: '执行能力',
    sections: [
      { id: 'browser-use', label: '浏览器 AI+', icon: LanguageOutlinedIcon },
      { id: 'terminal-ai', label: '终端 AI+', icon: TerminalOutlinedIcon },
      { id: 'computer-use', label: '电脑操控', icon: MonitorOutlinedIcon },
      { id: 'remote-desktop', label: '远程桌面', icon: DesktopWindowsOutlinedIcon },
    ],
  },
  {
    group: '高级设置',
    sections: [
      { id: 'sandbox', label: '安全工作环境', icon: ShieldOutlinedIcon },
      { id: 'terminal', label: 'ACP 与终端', icon: TerminalOutlinedIcon },
      { id: 'labs', label: '实验特性', icon: ScienceOutlinedIcon },
    ],
  },
];

const allSections = sectionGroups.flatMap((g) => g.sections);

const tealSwitch = {
  '& .MuiSwitch-switchBase.Mui-checked': { color: '#14B8A6' },
  '& .MuiSwitch-switchBase.Mui-checked + .MuiSwitch-track': { backgroundColor: '#14B8A6' },
};

// ── 通用小组件 ─────────────────────────────────────────────────────

/** 尚未接入运行时的能力统一用这个标签，避免「能点、能保存、实际不动」 */
const UnavailableTag: React.FC = () => {
  const { t: tr } = useI18n();
  return (
  <Box
    component="span"
    sx={{
      px: 0.75,
      py: 0.1,
      borderRadius: '6px',
      bgcolor: 'rgba(107, 114, 128, 0.12)',
      color: 'var(--tt-text-3)',
      fontSize: '10.5px',
      fontWeight: 600,
      flexShrink: 0,
    }}
  >
    {tr('尚未提供')}
  </Box>
  );
};

const SettingRow: React.FC<{
  label: string;
  sub?: string;
  control: React.ReactNode;
  unavailable?: boolean;
}> = ({ label, sub, control, unavailable }) => (
  <Box
    sx={{
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      bgcolor: 'var(--tt-surface)',
      border: '1px solid var(--tt-border)',
      borderRadius: '14px',
      px: 2.5,
      py: 2,
      mb: 1.5,
    }}
  >
    <Box sx={{ minWidth: 0, pr: 2 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
        <Typography sx={{ fontSize: '14px', fontWeight: 500, color: 'var(--tt-text-1)' }}>{label}</Typography>
        {unavailable && <UnavailableTag />}
      </Box>
      {sub && (
        <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mt: 0.25 }}>{sub}</Typography>
      )}
    </Box>
    {control}
  </Box>
);

const PrefSelect: React.FC<{
  value: string;
  options: string[];
  onChange: (v: string) => void;
  disabled?: boolean;
}> = ({ value, options, onChange, disabled }) => {
  const { t: tr } = useI18n();
  return (
  <Box
    component="select"
    value={value}
    disabled={disabled}
    onChange={(e: React.ChangeEvent<HTMLSelectElement>) => onChange(e.target.value)}
    sx={{
      appearance: 'none',
      minWidth: 120,
      px: 1.5,
      py: 0.85,
      pr: 4,
      borderRadius: '10px',
      border: '1px solid var(--tt-border)',
      bgcolor: 'var(--tt-surface)',
      fontSize: '13px',
      color: 'var(--tt-text-1)',
      cursor: disabled ? 'not-allowed' : 'pointer',
      opacity: disabled ? 0.55 : 1,
      outline: 'none',
      backgroundImage:
        "url(\"data:image/svg+xml;charset=UTF-8,%3csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%239CA3AF'%3e%3cpath d='M7.41 8.59 12 13.17l4.59-4.58L18 10l-6 6-6-6 1.41-1.41z'/%3e%3c/svg%3e\")",
      backgroundRepeat: 'no-repeat',
      backgroundPosition: 'right 8px center',
      backgroundSize: '18px',
      '&:hover': { borderColor: '#D1D5DB' },
      '&:focus': { borderColor: '#14B8A6' },
    }}
  >
    {options.map((opt) => (
      <option key={opt} value={opt}>
        {tr(opt)}
      </option>
    ))}
  </Box>
  );
};

const PanelTitle: React.FC<{ title: string; desc: string }> = ({ title, desc }) => {
  const { t: tr } = useI18n();
  return (
    <Box sx={{ mb: 3 }}>
      <Typography sx={{ fontSize: '18px', fontWeight: 700, color: 'var(--tt-text-1)' }}>{tr(title)}</Typography>
      <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-4)', mt: 0.5 }}>{tr(desc)}</Typography>
    </Box>
  );
};

/** 真实设置持久化 hook：已下沉到 lib/use-app-settings（设置页与输入框共用，保证口径一致） */

// ── 分区一：偏好设置（对齐 QwenWork 10 项） ─────────────────────────

const PreferencesPanel: React.FC = () => {
  const { t: tr } = useI18n();
  const prefs = usePreferenceStore();
  const [appSettings, updateAppSettings] = useAppSettings();
  const language = (appSettings.language as string) ?? '中文';
  const uiStyle = (appSettings.uiStyle as string) ?? '默认';
  const chatFont = (appSettings.chatFont as string) ?? '无衬线';
  const fontSize = (appSettings.chatFontSize as number) ?? 14;
  const chatWidth = (appSettings.chatWidth as string) ?? '标准';
  const previewMode = (appSettings.previewMode as string) ?? '应用内预览';
  // 缺省即开启：老版本设置里没有这个键时，提示词建议应按默认开启而不是静默关闭
  const promptSuggestion = appSettings.promptSuggestion !== false;
  const expandTools = Boolean(appSettings.expandToolCalls);

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="偏好设置" desc="个性化界面外观与对话体验" />

      <SettingRow
        label={tr('语言')}
        sub={tr('English 已覆盖侧边栏、工作台、输入框、对话与偏好设置；其余功能页仍显示中文原文')}
        control={<PrefSelect value={language} options={['中文', 'English']} onChange={(v) => updateAppSettings({ language: v })} />}
      />
      <SettingRow label={tr('主题亮暗')} control={<PrefSelect value={prefs.theme} options={['亮色', '暗色', '跟随系统']} onChange={(v) => prefs.set({ theme: v })} />} />
      <SettingRow
        label={tr('界面风格')}
        sub={tr('四套皮肤即时生效；皮肤同时适配亮色与暗色主题')}
        control={<PrefSelect value={uiStyle} options={['默认', '玻璃', '经典', '羊皮纸']} onChange={(v) => updateAppSettings({ uiStyle: v })} />}
      />
      <SettingRow
        label={tr('对话字体')}
        sub={tr('只改变对话正文与输入框，不影响设置页与导航字体')}
        control={<PrefSelect value={chatFont} options={['无衬线', '衬线']} onChange={(v) => updateAppSettings({ chatFont: v })} />}
      />

      <SettingRow
        label={tr('对话字号')}
        control={
          <Box sx={{ width: 180, display: 'flex', alignItems: 'center', gap: 1 }}>
            <Slider
              size="small"
              min={12}
              max={20}
              value={fontSize}
              onChange={(_, v) => updateAppSettings({ chatFontSize: v as number })}
              sx={{ color: '#14B8A6' }}
            />
            <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', width: 36 }}>{fontSize}px</Typography>
          </Box>
        }
      />

      <SettingRow label={tr('对话宽度')} control={<PrefSelect value={chatWidth} options={['标准', '宽', '全宽']} onChange={(v) => updateAppSettings({ chatWidth: v })} />} />
      <SettingRow
        label={tr('预览方式')}
        sub={
          previewMode === '系统默认程序'
            ? tr('点击产物直接用系统默认应用打开；浏览器/引擎直连没有该通道时会如实提示并改为应用内预览')
            : tr('点击产物在应用内打开文本、Markdown 与网页预览')
        }
        control={<PrefSelect value={previewMode} options={['应用内预览', '系统默认程序']} onChange={(v) => updateAppSettings({ previewMode: v })} />}
      />
      <SettingRow
        label={tr('提示词建议')}
        sub={tr('输入框为空时展示与当前工作模式匹配的几条起步建议，点击只回填、不直接发送')}
        control={
          <Switch
            checked={promptSuggestion}
            onChange={(e) => updateAppSettings({ promptSuggestion: e.target.checked })}
            sx={tealSwitch}
          />
        }
      />
      <SettingRow
        label={tr('默认展开工具调用')}
        sub={tr('新消息的执行过程默认展开；仍可点击每一轮的标题手动折叠')}
        control={
          <Switch
            checked={expandTools}
            onChange={(e) => updateAppSettings({ expandToolCalls: e.target.checked })}
            sx={tealSwitch}
          />
        }
      />
      <ImToolProcessSetting />
    </Box>
  );
};

/**
 * 「在 IM 频道中展示工具调用执行过程」。
 *
 * 这个开关的生效方在引擎：IM 回贴的正文是引擎拼的（`server.py::_im_reply_of`），
 * 所以这里读的是引擎 `im_preferences.json` 里的当前值，写的也是它。
 * 引擎没跑或保存失败时开关保持灰着并说明原因 —— 之前它只往 SQLite 存了个布尔，
 * 没有任何消费方，副标题承诺的「展示执行过程」从来不会发生。
 */
const ImToolProcessSetting: React.FC = () => {
  const { t: tr } = useI18n();
  const [prefs, setPrefs] = useState<ImPreferences | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  const load = React.useCallback(async () => {
    try {
      setPrefs(await getImPreferences());
      setError('');
    } catch (e) {
      setError(errorTextOf(e, tr('IM 频道偏好读取失败')));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = async (enabled: boolean) => {
    setSaving(true);
    setError('');
    try {
      setPrefs(await setImPreferences({ showToolProcess: enabled }));
    } catch (e) {
      setError(errorTextOf(e, enabled ? tr('IM 执行过程开关未能保存') : tr('IM 执行过程开关未能取消')));
    } finally {
      setSaving(false);
    }
  };

  const enabled = Boolean(prefs?.showToolProcess);
  const sub = error
    ? error
    : !prefs
      ? tr('正在读取引擎里的 IM 频道偏好…')
      : enabled
        ? tr('IM 回复会在最终答复后附本次真实调用过的工具步骤')
        : tr('IM 只回复最终结果；长连接与回贴都在本地引擎里，引擎未运行时无法修改');

  return (
    <SettingRow
      label={tr('在 IM 频道中展示工具调用执行过程')}
      sub={sub}
      control={
        <Switch
          checked={enabled}
          disabled={!prefs || saving}
          onChange={(e) => void toggle(e.target.checked)}
          sx={tealSwitch}
        />
      }
    />
  );
};

// ── 分区二：个人资料 ───────────────────────────────────────────────

const ProfilePanel: React.FC = () => {
  const avatarColors = ['#3B82F6', '#14B8A6', '#F59E0B', '#EF4444', '#8B5CF6', '#EC4899', '#06B6D4', '#84CC16', '#F97316', '#6366F1', '#111827'];
  const [appSettings, updateAppSettings] = useAppSettings();
  const color = (appSettings.avatarColor as string) ?? '#3B82F6';
  const setColor = (c: string) => updateAppSettings({ avatarColor: c });

  // 账号登录态（网页验证登录 · Loopback 方案）
  const { loggedIn, profile, mock: authMock, busy: authBusy, init: authInit, login: authLogin, logout: authLogout } = useAuthStore();
  const showNotification = useAppStore((s) => s.showNotification);
  const [logoutConfirmOpen, setLogoutConfirmOpen] = useState(false);

  // 账号服务连通性：默认综合服务端（127.0.0.1:8000）；Mock 登录不连服务
  // 浏览器通道没有桌面侧网络能力，不把默认 mock=true 误显示成「已选 Mock 登录」
  const mockMode = authMock && isTauri();
  const [serverProbe, setServerProbe] = useState<AuthServerProbe | null>(null);
  const [probeBusy, setProbeBusy] = useState(false);
  const probeServer = React.useCallback(async () => {
    if (!isTauri()) {
      setServerProbe({
        baseUrl: 'http://127.0.0.1:8000',
        reachable: false,
        service: null,
        version: null,
        error: '浏览器模式没有桌面侧的账号服务网络能力，请在桌面版验证真实登录',
      });
      return;
    }
    setProbeBusy(true);
    try {
      setServerProbe(await authAPI.serverProbe());
    } catch (e) {
      setServerProbe({
        baseUrl: '',
        reachable: false,
        service: null,
        version: null,
        error: errorTextOf(e, '账号服务探测失败'),
      });
    } finally {
      setProbeBusy(false);
    }
  }, []);

  useEffect(() => {
    void authInit();
  }, [authInit]);

  useEffect(() => {
    void probeServer();
  }, [probeServer]);

  const handleLogin = async () => {
    const ok = await authLogin();
    if (ok) {
      const nick = useAuthStore.getState().profile?.nickname ?? '';
      showNotification(`登录成功${nick ? `：${nick}` : ''}`, 'success');
    } else {
      showNotification(useAuthStore.getState().error ?? '登录失败，请重试', 'error');
    }
  };

  const handleLogout = async () => {
    setLogoutConfirmOpen(false);
    const ok = await authLogout();
    showNotification(ok ? '已退出登录' : '退出失败，请重试', ok ? 'success' : 'error');
  };

  const avatarChar = loggedIn ? profile?.nickname?.charAt(0) || '客' : '客';

  const copyUid = async () => {
    const uid = profile?.uid;
    if (!uid) return;
    try {
      await navigator.clipboard.writeText(uid);
      showNotification('UID 已复制', 'success');
    } catch {
      showNotification('复制失败：当前环境不允许访问剪贴板', 'error');
    }
  };

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="个人资料" desc="管理头像与账户信息" />

      <SettingRow
        label="头像"
        sub="选择头像背景色；头像图标取昵称首字，暂不支持上传自定义图片"
        control={
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
            <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, maxWidth: 170, justifyContent: 'flex-end' }}>
              {avatarColors.map((c) => (
                <Box
                  key={c}
                  onClick={() => setColor(c)}
                  title={c}
                  sx={{
                    width: 18,
                    height: 18,
                    borderRadius: '50%',
                    bgcolor: c,
                    cursor: 'pointer',
                    border: color === c ? '2px solid #111827' : '2px solid transparent',
                  }}
                />
              ))}
            </Box>
            <Box
              sx={{
                width: 36,
                height: 36,
                borderRadius: '50%',
                bgcolor: color,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <Typography sx={{ color: '#fff', fontSize: '15px', fontWeight: 600 }}>{avatarChar}</Typography>
            </Box>
          </Box>
        }
      />

      <SettingRow
        label="账户 UID"
        sub={loggedIn ? profile?.email ?? '仅供查看和复制' : '未登录，登录后可查看'}
        control={
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
            <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-3)', fontFamily: 'Consolas, monospace' }}>
              {loggedIn ? profile?.uid ?? '-' : '未登录'}
            </Typography>
            <Tooltip title={loggedIn ? '复制 UID' : '登录后可复制'}>
              <span>
                <IconButton
                  size="small"
                  disabled={!loggedIn || !profile?.uid}
                  onClick={() => void copyUid()}
                  sx={{ color: 'var(--tt-text-3)' }}
                >
                  <ContentCopyIcon sx={{ fontSize: 16 }} />
                </IconButton>
              </span>
            </Tooltip>
          </Box>
        }
      />

      <SettingRow
        label="订阅"
        sub={
          loggedIn
            ? `当前套餐：${profile?.plan ?? '个人免费版'}。当前版本未接入在线订阅与升级，套餐变更需在网页端账户中心办理`
            : '登录后显示当前套餐；当前版本未接入在线订阅与升级入口'
        }
        control={
          <Box sx={{ px: 1.5, py: 0.5, borderRadius: '8px', border: '1px solid var(--tt-border)', bgcolor: 'var(--tt-surface-2)' }}>
            <Typography sx={{ fontSize: '12px', fontWeight: 600, color: 'var(--tt-text-3)' }}>
              {profile?.plan ?? '个人免费版'}
            </Typography>
          </Box>
        }
      />

      <SettingRow
        label="账号服务"
        sub={
          mockMode
            ? '当前为 Mock 登录（TT_AUTH_MOCK=true），不连接服务端；关闭后可用综合服务端账号登录'
            : serverProbe
              ? `${serverProbe.baseUrl}${serverProbe.reachable ? ` · ${serverProbe.service ?? '综合服务端'}${serverProbe.version ? ` v${serverProbe.version}` : ''}` : ' · 未连接'}`
              : '正在探测综合服务端…'
        }
        control={
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <Box
              sx={{
                width: 8,
                height: 8,
                borderRadius: '50%',
                bgcolor: mockMode ? '#F59E0B' : serverProbe?.reachable ? '#10B981' : '#EF4444',
              }}
            />
            <Typography sx={{ fontSize: '12px', fontWeight: 600, color: 'var(--tt-text-3)' }}>
              {mockMode ? 'Mock' : probeBusy ? '检测中…' : serverProbe?.reachable ? '已连接' : '未连接'}
            </Typography>
            {!mockMode && (
              <Box
                onClick={() => !probeBusy && void probeServer()}
                sx={{
                  px: 1.25,
                  py: 0.4,
                  borderRadius: '8px',
                  border: '1px solid var(--tt-border)',
                  cursor: probeBusy ? 'not-allowed' : 'pointer',
                  opacity: probeBusy ? 0.6 : 1,
                }}
              >
                <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)' }}>重新检测</Typography>
              </Box>
            )}
          </Box>
        }
      />

      {!mockMode && serverProbe && !serverProbe.reachable && serverProbe.error && (
        <Box
          sx={{
            mb: 1.5,
            px: 2.5,
            py: 1.25,
            borderRadius: '12px',
            bgcolor: 'rgba(239,68,68,0.06)',
            border: '1px solid rgba(239,68,68,0.2)',
          }}
        >
          <Typography sx={{ fontSize: '12px', color: '#B91C1C', lineHeight: 1.6 }}>
            {serverProbe.error}
          </Typography>
        </Box>
      )}

      {loggedIn ? (
        <SettingRow
          label="退出登录"
          sub="在此设备退出登录，可随时重新登录"
          control={
            <Box
              onClick={() => !authBusy && setLogoutConfirmOpen(true)}
              sx={{ px: 1.75, py: 0.5, borderRadius: '8px', border: '1px solid #FCA5A5', cursor: authBusy ? 'not-allowed' : 'pointer', opacity: authBusy ? 0.6 : 1 }}
            >
              <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#EF4444' }}>{authBusy ? '退出中…' : '退出登录'}</Typography>
            </Box>
          }
        />
      ) : (
        <SettingRow
          label="登录"
          sub={
            mockMode
              ? 'Mock 登录：直接生成模拟账号，不连接服务端'
              : isTauri()
                ? '在浏览器完成综合服务端账号验证，登录成功后自动返回桌面端'
                : '浏览器通道没有账号能力，请在桌面版完成真实登录'
          }
          control={
            <Box
              onClick={() => !authBusy && void handleLogin()}
              sx={{ px: 1.75, py: 0.5, borderRadius: '8px', background: 'linear-gradient(120deg, #6366F1 0%, #8B5CF6 100%)', cursor: authBusy ? 'not-allowed' : 'pointer', opacity: authBusy ? 0.6 : 1 }}
            >
              <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#fff' }}>{authBusy ? '登录中…' : '登录 / 注册'}</Typography>
            </Box>
          }
        />
      )}

      {/* 退出登录确认弹窗 */}
      <Dialog open={logoutConfirmOpen} onClose={() => setLogoutConfirmOpen(false)} PaperProps={{ sx: { borderRadius: '14px', minWidth: 320 } }}>
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>退出登录</DialogTitle>
        <DialogContent>
          <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-2)' }}>
            确定要在此设备退出登录吗？退出后本地任务与会话数据仍会保留，可随时重新登录。
          </Typography>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setLogoutConfirmOpen(false)} sx={{ color: 'var(--tt-text-3)', fontSize: '13px' }}>取消</Button>
          <Button onClick={() => void handleLogout()} disabled={authBusy} variant="contained" sx={{ fontSize: '13px', bgcolor: '#EF4444', '&:hover': { bgcolor: '#DC2626' } }}>退出登录</Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

// ── 分区三：系统设置 ───────────────────────────────────────────────

/**
 * 网络代理设置行。
 * 真生效链路：保存 → Rust settings_set_many（落库前整组校验）→ proxy::publish →
 * ① Rust 出网（模型连通性/MCP/账号）按新配置重建 client；② 新启动的引擎子进程注入
 * HTTP_PROXY/HTTPS_PROXY/NO_PROXY。已运行的引擎仍是旧代理，故如实提示需重启。
 */
const ProxySetting: React.FC = () => {
  const [mode, setMode] = useState('跟随系统');
  const [url, setUrl] = useState('');
  const [bypass, setBypass] = useState('');
  const [status, setStatus] = useState<ProxyStatus | null>(null);
  const [error, setError] = useState('');
  const [tip, setTip] = useState('');
  const [saving, setSaving] = useState(false);

  const load = React.useCallback(async () => {
    const s = await getSettings().catch(() => null);
    if (s) {
      setMode(parseProxyMode(typeof s.proxyMode === 'string' ? s.proxyMode : ''));
      setUrl(typeof s.proxyUrl === 'string' ? s.proxyUrl : '');
      setBypass(typeof s.proxyBypass === 'string' ? s.proxyBypass : '');
    }
    try {
      setStatus(await getProxyStatus());
      setError('');
    } catch (e) {
      setError(errorTextOf(e, '代理生效状态读取失败'));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const needUrl = mode === '自定义' && url.trim() === '';

  const handleSave = async () => {
    setSaving(true);
    setError('');
    setTip('');
    try {
      // strict：Rust 校验失败必须可见（默认 setSettings 会为引擎直连场景吞掉异常）
      await setSettings({ proxyMode: mode, proxyUrl: url.trim(), proxyBypass: bypass.trim() }, { strict: true });
      setStatus(await getProxyStatus());
      setTip(mode === '自定义' ? `已保存并生效：${url.trim()}` : '已保存并生效');
    } catch (e) {
      setError(errorTextOf(e, '网络代理设置保存失败'));
    } finally {
      setSaving(false);
    }
  };

  const active = status?.active;
  const activeText =
    active === undefined
      ? '正在读取当前生效配置…'
      : active.mode === '自定义'
        ? `当前生效：自定义 ${active.url ?? ''}（本机回环始终直连）`
        : active.mode === '不使用'
          ? '当前生效：不使用代理（忽略系统与引擎环境中的代理变量）'
          : '当前生效：跟随系统（由本机网络配置决定）';

  return (
    <Box
      sx={{
        bgcolor: 'var(--tt-surface)',
        border: '1px solid var(--tt-border)',
        borderRadius: '14px',
        px: 2.5,
        py: 2,
        mb: 1.5,
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 2 }}>
        <Box sx={{ minWidth: 0 }}>
          <Typography sx={{ fontSize: '14px', fontWeight: 500, color: 'var(--tt-text-1)' }}>网络代理</Typography>
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mt: 0.25 }}>{activeText}</Typography>
        </Box>
        <PrefSelect
          value={mode}
          options={['跟随系统', '自定义', '不使用']}
          onChange={(v) => {
            setMode(v);
            setTip('');
            setError('');
          }}
        />
      </Box>

      {mode === '自定义' && (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, mt: 2 }}>
          <TextField
            size="small"
            label="代理地址"
            placeholder="http://127.0.0.1:7890"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            inputProps={{ 'aria-label': '代理地址' }}
            sx={{ '& .MuiOutlinedInput-input': { fontSize: '13px' }, '& .MuiFormLabel-root': { fontSize: '13px' } }}
          />
          <TextField
            size="small"
            label="忽略主机（可选，逗号分隔）"
            placeholder="*.corp.internal, 10.0.0.0/8"
            value={bypass}
            onChange={(e) => setBypass(e.target.value)}
            inputProps={{ 'aria-label': '忽略代理的主机清单' }}
            sx={{ '& .MuiOutlinedInput-input': { fontSize: '13px' }, '& .MuiFormLabel-root': { fontSize: '13px' } }}
          />
          <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>
            支持 http / https / socks5 / socks4；127.0.0.1、localhost、::1 始终直连，不会被代理拦截
            {status && status.effectiveBypass.length > 0 ? `（当前直连：${status.effectiveBypass.join('、')}）` : ''}
          </Typography>
        </Box>
      )}

      {(error || tip || (status && status.staleEngines > 0)) && (
        <Typography
          sx={{
            fontSize: '12px',
            mt: 1.5,
            color: error ? '#EF4444' : tip ? '#0D9488' : '#D97706',
          }}
        >
          {error || tip || `${status?.staleEngines} 个运行中的引擎仍在使用旧代理，重启该工作空间后切换`}
        </Typography>
      )}

      <Box sx={{ display: 'flex', justifyContent: 'flex-end', mt: 1.5 }}>
        <Box
          onClick={() => !saving && !needUrl && void handleSave()}
          sx={{
            px: 1.75,
            py: 0.5,
            borderRadius: '8px',
            background: saving || needUrl ? 'var(--tt-surface-2, #F3F4F6)' : 'linear-gradient(120deg, #14B8A6 0%, #0EA5E9 100%)',
            opacity: saving || needUrl ? 0.6 : 1,
            cursor: saving || needUrl ? 'not-allowed' : 'pointer',
          }}
        >
          <Typography sx={{ fontSize: '12px', fontWeight: 600, color: saving || needUrl ? 'var(--tt-text-3)' : '#fff' }}>
            {saving ? '保存中…' : needUrl ? '请填写代理地址' : '保存并生效'}
          </Typography>
        </Box>
      </Box>
    </Box>
  );
};

/** 权限状态单元：只有系统真实回答过的项才用彩色标签，其余是一句解释 */
type PermissionTone = 'granted' | 'denied' | 'muted';

const PERMISSION_TONE: Record<PermissionTone, { color: string; bg: string }> = {
  granted: { color: '#0D9488', bg: '#E6F7F4' },
  denied: { color: '#B91C1C', bg: '#FEF2F2' },
  muted: { color: 'var(--tt-text-4)', bg: 'transparent' },
};

const PERMISSION_STATE_TONE: Record<PermissionState, PermissionTone> = {
  granted: 'granted',
  denied: 'denied',
  prompt: 'muted',
  unknown: 'muted',
};

interface PermissionCell {
  text: string;
  tone: PermissionTone;
  /** 当前平台有没有可达的授权入口 */
  actionable: boolean;
}

/**
 * 状态 → 展示单元。
 * 问不到状态时绝不补一个「已授权」：平台没有这个开关就用 Rust 给的如实解释，
 * 有开关但代码问不到则显示「需在系统设置中管理」。
 */
function permissionCellOf(item: SystemCapability, state: PermissionState | undefined): PermissionCell {
  const hasPane = item.pane !== null;
  const label = state === undefined ? null : permissionLabel(state);
  if (state && label) {
    return { text: label, tone: PERMISSION_STATE_TONE[state], actionable: hasPane };
  }
  if (item.note) return { text: item.note, tone: 'muted', actionable: false };
  return { text: '需在系统设置中管理', tone: 'muted', actionable: hasPane };
}

/**
 * 系统权限区。
 * 清单由桌面端 Rust 按当前平台给出（`security::permissions::CAPABILITIES`），
 * 状态只取系统能真实回答的探测（通知 / 麦克风），「去授权」拉起对应隐私设置面板。
 */
const SystemPermissionSection: React.FC = () => {
  const [view, setView] = useState<SystemPermissionStatus | null>(null);
  const [error, setError] = useState('');
  const [tip, setTip] = useState('');
  const [pending, setPending] = useState('');

  const load = React.useCallback(async () => {
    try {
      setView(await getSystemPermissionStatus());
      setError('');
    } catch (e) {
      setError(errorTextOf(e, '系统权限状态读取失败'));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleOpen = async (item: SystemCapability) => {
    if (pending || item.pane === null) return;
    setPending(item.key);
    setError('');
    setTip('');
    try {
      await openSystemPermissionSettings(item.key);
      setTip(`已打开系统设置中的「${item.label}」页面，授权后点右上「重新读取」更新状态`);
    } catch (e) {
      setError(errorTextOf(e, `${item.label}：授权入口打开失败`));
    } finally {
      setPending('');
    }
  };

  return (
    <Box>
      <Box sx={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', mt: 3, mb: 1.5 }}>
        <Typography sx={{ fontSize: '14px', fontWeight: 700, color: 'var(--tt-text-1)' }}>系统权限</Typography>
        <Box
          onClick={() => !pending && void load()}
          sx={{ px: 1.25, py: 0.35, borderRadius: '8px', border: '1px solid var(--tt-border)', cursor: 'pointer' }}
        >
          <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-3)' }}>重新读取</Typography>
        </Box>
      </Box>

      {!view && !error ? (
        <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-4)', mb: 1 }}>正在读取系统权限状态…</Typography>
      ) : null}

      {view?.items.map((item) => {
        const cell = permissionCellOf(item, view.states[item.key]);
        const tone = PERMISSION_TONE[cell.tone];
        return (
          <SettingRow
            key={item.key}
            label={item.label}
            sub={item.purpose}
            control={
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, justifyContent: 'flex-end' }}>
                {cell.tone === 'muted' ? (
                  <Typography sx={{ fontSize: '12px', color: tone.color, textAlign: 'right', maxWidth: 260 }}>
                    {cell.text}
                  </Typography>
                ) : (
                  <Box sx={{ px: 1.25, py: 0.4, borderRadius: '6px', bgcolor: tone.bg }}>
                    <Typography sx={{ fontSize: '11.5px', fontWeight: 600, color: tone.color }}>{cell.text}</Typography>
                  </Box>
                )}
                {cell.actionable ? (
                  <Box
                    onClick={() => void handleOpen(item)}
                    sx={{
                      px: 1.5,
                      py: 0.45,
                      borderRadius: '8px',
                      bgcolor: 'var(--tt-text-1)',
                      cursor: pending ? 'default' : 'pointer',
                      opacity: pending ? 0.6 : 1,
                    }}
                  >
                    <Typography sx={{ fontSize: '11.5px', fontWeight: 600, color: '#fff' }}>
                      {pending === item.key ? '打开中…' : '去授权'}
                    </Typography>
                  </Box>
                ) : null}
              </Box>
            }
          />
        );
      })}

      {view && !view.openSupported ? (
        <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 1 }}>
          当前是浏览器预览通道：通知与麦克风的状态已向网页问过，其余项问不到；且无法打开系统隐私设置，请在桌面版任我行中完成授权。
        </Typography>
      ) : null}

      {(error || tip) && (
        <Typography sx={{ fontSize: '12px', mt: 1, color: error ? '#EF4444' : '#0D9488' }}>
          {error || tip}
        </Typography>
      )}
    </Box>
  );
};

/**
 * 开机自启动。
 * 开关取的是「系统启动项里到底有没有本程序」这个事实（Windows 注册表回读），
 * 不是设置页里勾选的意愿：写入失败、被安全软件清掉、启动项还指向旧构建路径，
 * 这几种情况都必须显示出来，否则用户会以为下次开机真的会自动启动。
 */
const AutoStartSetting: React.FC = () => {
  const [appSettings] = useAppSettings();
  const [status, setStatus] = useState<AutostartStatus | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  // 启动项的期望命令随后台常驻变化（是否带 --hidden）：变了就重新回读，stale 才判得准
  const background = Boolean(appSettings.runInBackground);

  const load = React.useCallback(async () => {
    try {
      setStatus(await getAutostartStatus());
      setError('');
    } catch (e) {
      setError(errorTextOf(e, '系统启动项状态读取失败'));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, background]);

  const toggle = async (enabled: boolean) => {
    setSaving(true);
    setError('');
    try {
      // strict：Rust 写启动项后会回读校验，失败原样抛出（不能让开关亮着其实没写入）
      await setSettings({ autoStart: enabled }, { strict: true });
    } catch (e) {
      setError(errorTextOf(e, enabled ? '开机自启动未能写入' : '开机自启动未能取消'));
    } finally {
      setSaving(false);
    }
    await load();
  };

  const supported = status?.supported ?? false;
  const enabled = Boolean(status?.enabled);
  const sub = error
    ? error
    : !status
      ? '正在读取系统启动项…'
      : !supported
        ? '当前环境无法管理系统启动项，请在桌面版中设置'
        : enabled
          ? status.stale
            ? `启动项仍指向旧路径：${status.command ?? ''}（关闭再开启可更新为当前程序）`
            : `已登记到系统启动项：${status.command ?? ''}`
          : status.requested
            ? '设置里开着，但系统启动项里没有本程序（可能被安全软件清理），请重新开启'
            : '登录电脑时自动启动任我行（写入当前用户的系统启动项）';

  return (
    <SettingRow
      label="开机自启动"
      sub={sub}
      control={
        <Switch
          checked={enabled}
          disabled={!supported || saving}
          onChange={(e) => void toggle(e.target.checked)}
          sx={tealSwitch}
        />
      }
    />
  );
};

/**
 * 后台常驻。
 * 事实来源是托盘是否真的创建成功（system:info.backgroundResident）：托盘不可用时
 * 关窗就会退出，「继续运行」只是空话，因此开关置灰而不是让用户勾一个无效意愿。
 */
const BackgroundResidentSetting: React.FC = () => {
  const [appSettings, updateAppSettings] = useAppSettings();
  const [resident, setResident] = useState<boolean | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let alive = true;
    systemAPI
      .info()
      .then((info) => {
        if (alive) setResident(Boolean(info.backgroundResident));
      })
      .catch(() => {
        // 浏览器/引擎直连没有托盘进程：如实按不支持处理，不假装能常驻
        if (alive) setResident(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const requested = Boolean(appSettings.runInBackground);

  const toggle = async (enabled: boolean) => {
    setSaving(true);
    setError('');
    try {
      // strict：写入失败要回滚并显示，不能让开关停在「开了但没生效」
      await updateAppSettings({ runInBackground: enabled }, { strict: true });
    } catch (e) {
      setError(errorTextOf(e, enabled ? '后台常驻未能开启' : '后台常驻未能关闭'));
    } finally {
      setSaving(false);
    }
  };

  const supported = resident === true;
  const sub = error
    ? error
    : resident === null
      ? '正在确认系统托盘能力…'
      : !supported
        ? '当前环境没有可用的系统托盘，关闭窗口仍会退出程序'
        : requested
          ? '已开启：关闭主窗口后进程驻留托盘，定时任务与进行中的任务继续运行；从托盘菜单可退出'
          : '关闭主窗口即退出程序（定时任务随之停止）；开启后可驻留系统托盘继续运行';

  return (
    <SettingRow
      label="后台常驻"
      sub={sub}
      control={
        <Switch
          checked={requested && supported}
          disabled={!supported || saving}
          onChange={(e) => void toggle(e.target.checked)}
          sx={tealSwitch}
        />
      }
    />
  );
};

/**
 * 桌面通知 / 任务栏未读 / 声音通知。
 * 这三行此前只把布尔值写进设置，没有任何调用点 —— 副标题承诺的「等待回复或完成时提醒」
 * 从来不会发生。现在通知与提示音由 workbenchStore 的事件路由真实发出：
 * 开启通知必须先拿到系统授权，授权被拒不把开关拨成「已开」；
 * 任务栏未读角标在 Windows 走图标叠加、macOS 走 Dock 计数，其余通道由能力接口
 * 回报 supported=false，开关置灰并显示具体原因，不假装能开。
 */
const NotificationSettings: React.FC = () => {
  const [appSettings, updateAppSettings] = useAppSettings();
  const [permission, setPermission] = useState<NotificationPermission | 'unsupported' | null>(null);
  const [error, setError] = useState('');
  const notify = appSettings.desktopNotify !== false;
  const sound = Boolean(appSettings.soundNotify);
  const badgeUnread = appSettings.badgeUnread !== false;
  const [badge, setBadge] = useState<BadgeCapability | null>(null);

  useEffect(() => {
    setPermission(notificationPermission());
    void badgeCapability().then(setBadge);
  }, []);

  const toggleBadge = (on: boolean) => {
    updateAppSettings({ badgeUnread: on });
    if (on) refreshTaskbarBadge();
    else clearTaskbarUnread();
  };

  const toggleNotify = async (enabled: boolean) => {
    setError('');
    if (!enabled) {
      updateAppSettings({ desktopNotify: false });
      return;
    }
    const p = await ensureNotificationPermission();
    setPermission(p);
    if (p !== 'granted') {
      setError(
        p === 'unsupported'
          ? '当前环境不提供系统通知能力，无法开启'
          : '系统未授权通知，请在系统设置中允许任我行发送通知后再开',
      );
      // 没拿到授权就不把开关写成「已开」：界面必须和系统事实一致
      return;
    }
    updateAppSettings({ desktopNotify: true });
  };

  const granted = permission === 'granted';
  const notifySub = error
    ? error
    : notify && granted
      ? '任务等你确认计划、完成或失败时发系统通知（窗口在前台时不打扰）'
      : notify
        ? '任务等你确认计划、完成或失败时发系统通知（需系统授权，开启时会询问）'
        : '已关闭：任务事件不再发系统通知';

  return (
    <>
      <SettingRow
        label="桌面通知"
        sub={notifySub}
        control={
          <Switch
            checked={notify && granted}
            onChange={(e) => void toggleNotify(e.target.checked)}
            sx={tealSwitch}
          />
        }
      />
      <SettingRow
        label="任务栏未读状态"
        sub={badge ? badge.detail : '正在确认当前平台的任务栏未读能力…'}
        control={
          <Switch
            checked={badgeUnread && Boolean(badge?.supported)}
            disabled={!badge?.supported}
            onChange={(e) => toggleBadge(e.target.checked)}
            sx={tealSwitch}
          />
        }
      />
      <SettingRow
        label="声音通知"
        sub={
          sound
            ? '任务完成时播一声短提示音（页面被挡住声音时不会响，会如实提示）'
            : '已关闭：任务完成不播放提示音'
        }
        control={
          <Switch
            checked={sound}
            onChange={(e) => updateAppSettings({ soundNotify: e.target.checked })}
            sx={tealSwitch}
          />
        }
      />
    </>
  );
};

const PII_OPTIONS = ['关闭', '脱敏后发送', '命中即中止'];
const PII_LABELS: Record<PiiOutboundMode, string> = {
  off: '关闭',
  mask: '脱敏后发送',
  block: '命中即中止',
};
const PII_VALUES: Record<string, PiiOutboundMode> = {
  关闭: 'off',
  脱敏后发送: 'mask',
  命中即中止: 'block',
};

const TOOL_APPROVAL_OPTIONS = ['关闭', '敏感工具', '所有工具'];
const TOOL_APPROVAL_LABELS: Record<ToolApprovalMode, string> = {
  off: '关闭',
  sensitive: '敏感工具',
  all: '所有工具',
};
const TOOL_APPROVAL_VALUES: Record<string, ToolApprovalMode> = {
  关闭: 'off',
  敏感工具: 'sensitive',
  所有工具: 'all',
};

const SystemPanel: React.FC = () => {
  const [appSettings, updateAppSettings] = useAppSettings();
  const keepAwake = appSettings.keepAwake !== false;
  const privacyMode = Boolean(appSettings.privacyMode);
  // 存量里可能有非法值：界面显示为关闭，但引擎读到的仍是原值并会显式提示
  const piiMode: PiiOutboundMode =
    appSettings.piiOutboundMode === 'mask' || appSettings.piiOutboundMode === 'block'
      ? appSettings.piiOutboundMode
      : 'off';
  const toolApprovalMode: ToolApprovalMode =
    appSettings.toolApprovalMode === 'sensitive' || appSettings.toolApprovalMode === 'all'
      ? appSettings.toolApprovalMode
      : 'off';

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="系统设置" desc="开机自启动、保持唤醒、通知等系统级选项" />

      <AutoStartSetting />
      <BackgroundResidentSetting />
      <SettingRow label="保持系统唤醒" sub="防止电脑在 Agent 工作时自动进入睡眠（真实生效）" control={<Switch checked={keepAwake} onChange={(e) => updateAppSettings({ keepAwake: e.target.checked })} sx={tealSwitch} />} />
      <NotificationSettings />
      <ProxySetting />

      <SystemPermissionSection />

      <Typography sx={{ fontSize: '14px', fontWeight: 700, color: 'var(--tt-text-1)', mt: 3, mb: 1.5 }}>数据共享</Typography>
      <SettingRow
        label="隐私模式"
        sub={
          privacyMode
            ? '已开启：新任务的目标与回复不写入本地会话库，引擎不写会话记录与自动记忆，通知不显示任务标题'
            : '开启后新任务不落正文、不写可回放事件、不写自动记忆；任务运行中关闭也按提交时的开关执行'
        }
        control={
          <Switch
            checked={privacyMode}
            onChange={(e) => updateAppSettings({ privacyMode: e.target.checked })}
            sx={tealSwitch}
          />
        }
      />
      <Box sx={{ mb: 2, px: 0.25 }}>
        <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', lineHeight: 1.7 }}>
          边界：隐私模式只影响本地留存，不改模型出站——需要模型看不到敏感信息时，用下方的「PII 出站脱敏」；
          产物文件仍写入工作目录以便使用；仅作用于本应用此后发起的任务，不影响 IM 频道消息与已保存的历史会话。
        </Typography>
      </Box>

      <SettingRow
        label="PII 出站脱敏"
        sub={
          piiMode === 'block'
            ? '已开启：模型调用前检测到身份证号、手机号、银行卡号、邮箱或明文密钥会直接中止任务'
            : piiMode === 'mask'
              ? '已开启：身份证号、手机号、银行卡号、邮箱与明文密钥在发给模型前替换为掩码'
              : '关闭：任务内容按原样发给你选择的模型服务'
        }
        control={
          <PrefSelect
            value={PII_LABELS[piiMode]}
            options={PII_OPTIONS}
            onChange={(v) => updateAppSettings({ piiOutboundMode: PII_VALUES[v] ?? 'off' })}
          />
        }
      />

      <SettingRow
        label="工具执行审批"
        sub={
          toolApprovalMode === 'all'
            ? '除浏览器与电脑操控（已有逐操作确认）外，每次工具调用前弹系统确认'
            : toolApprovalMode === 'sensitive'
              ? '写文件、执行命令、联网、派发子 Agent 等有副作用的工具调用前弹系统确认'
              : '关闭：工具调用不弹确认，模型直接执行'
        }
        control={
          <PrefSelect
            value={TOOL_APPROVAL_LABELS[toolApprovalMode]}
            options={TOOL_APPROVAL_OPTIONS}
            onChange={(v) => updateAppSettings({ toolApprovalMode: TOOL_APPROVAL_VALUES[v] ?? 'off' })}
          />
        }
      />
      <Box sx={{ mb: 2, px: 0.25 }}>
        <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', lineHeight: 1.7 }}>
          边界：脱敏只覆盖本应用发起任务时发往模型的文本——图片附件、浏览器/连接器/脚本自身的外发、
          IM 频道任务不经过这里；工具审批仅桌面端生效（浏览器直连引擎时没有本机确认网关），
          开启后网关不可用会中止任务而不是跳过审批；放行与否都写入安全审计。
        </Typography>
      </Box>
    </Box>
  );
};

// ── 分区四：语音输入 ───────────────────────────────────────────────

/**
 * 全局触发按键的展示行。
 * 语音转录、应用快照这两项能力本身尚未提供，OS 级全局快捷键也就无从注册，
 * 所以这里如实显示为只读的出厂组合，而不是一个按了没反应的「点击录制」框。
 */
const GlobalTriggerRow: React.FC<{ label: string; combo: string; sub?: string }> = ({ label, combo, sub }) => (
  <SettingRow
    label={label}
    sub={sub ?? '该能力的快捷键随功能一起提供，当前未注册'}
    control={
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, justifyContent: 'flex-end' }}>
        <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>未注册</Typography>
        <Box sx={{ px: 1.5, py: 0.6, borderRadius: '8px', border: '1px solid var(--tt-border)', bgcolor: 'var(--tt-surface-2)', minWidth: 108, textAlign: 'center' }}>
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', fontFamily: 'Consolas, monospace' }}>
            {displayCombo(combo)}
          </Typography>
        </Box>
      </Box>
    }
  />
);

const VoicePanel: React.FC = () => {
  const [appSettings, updateAppSettings] = useAppSettings();
  const enabled = Boolean(appSettings.voiceEnabled);
  const mode = (appSettings.voiceMode as string) ?? '组合键';
  const setMode = (v: string) => updateAppSettings({ voiceMode: v });

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="语音输入" desc="全局语音输入快捷键与转录设置" />
      <SettingRow
        label="启用语音输入"
        sub="语音输入（录音与转录）尚未提供"
        unavailable
        control={<Switch disabled checked={enabled} sx={tealSwitch} />}
      />
      <SettingRow
        label="快捷键模式"
        sub="触发方式随语音输入一起提供"
        unavailable
        control={<PrefSelect disabled value={mode} options={['单按键', '组合键']} onChange={setMode} />}
      />
      <GlobalTriggerRow label="触发按键" combo="Ctrl+Shift+Space" sub="所选按键仅在单独按下时触发，正常组合键操作不受影响；转录能力提供后生效" />
    </Box>
  );
};

// ── 分区五：应用快照 ───────────────────────────────────────────────

const SnapshotPanel: React.FC = () => {
  const [appSettings] = useAppSettings();
  const enabled = Boolean(appSettings.snapshotEnabled);

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="应用快照" desc="捕获最前方应用的截图与可读文本，作为对话上下文" />
      <SettingRow
        label="启用应用快照"
        sub="应用快照尚未提供，当前不会截取任何屏幕内容"
        unavailable
        control={<Switch disabled checked={enabled} sx={tealSwitch} />}
      />
      <GlobalTriggerRow label="全局快捷键" combo="Alt+S" />
      <Box sx={{ bgcolor: 'rgba(217,119,6,0.08)', border: '1px solid rgba(217,119,6,0.25)', borderRadius: '12px', p: 1.75 }}>
        <Typography sx={{ fontSize: '12px', color: '#B45309', lineHeight: 1.6 }}>
          能力提供后：快照内容将作为上下文上送模型，受数据分级策略约束——检测为机密级的内容不会上送云端模型。
        </Typography>
      </Box>
    </Box>
  );
};

// ── 分区六：快捷键 ─────────────────────────────────────────────────

/** 快捷键行的真实状态：只有注册中心里挂了处理函数的才算「已注册」 */
function shortcutStatusOf(
  a: ShortcutAction,
  registered: string[],
  quickPickEnabled: boolean,
): { text: string; recordable: boolean; active: boolean } {
  if (a.scope === 'builtin') return { text: '输入框处理', recordable: false, active: true };
  // QuickPick 已实现，但只有开启后才会注册组合键；关闭时仍允许改键
  if (a.id === 'quickPick' && !quickPickEnabled) {
    return { text: '未启用', recordable: true, active: false };
  }
  if (!registered.includes(a.id)) {
    return { text: '未注册（能力尚未提供）', recordable: false, active: false };
  }
  return { text: '已注册', recordable: true, active: true };
}

const ShortcutsPanel: React.FC = () => {
  const [appSettings, updateAppSettings] = useAppSettings();
  const quickPickOn = Boolean(appSettings.quickPickEnabled);
  const qpMode = (appSettings.quickPickMode as string) ?? '组合键';
  const setQpMode = (v: string) => updateAppSettings({ quickPickMode: v });

  const overrides = (appSettings.shortcuts as ShortcutOverrides | undefined) ?? {};
  const combos = effectiveCombos(overrides);
  const registered = registeredShortcutIds({ quickPickEnabled: quickPickOn, quickPickMode: qpMode });
  const [recording, setRecording] = useState('');
  const [notice, setNotice] = useState('');

  const persist = (next: ShortcutOverrides) => updateAppSettings({ shortcuts: next });

  // 录制中：下一次按键即新组合（Esc 取消，撞车当场拒绝并说明被谁占用）
  useEffect(() => {
    if (!recording) return;
    const id = recording;
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.key === 'Escape') {
        setRecording('');
        setNotice('');
        return;
      }
      const combo = comboFromEvent({
        key: e.key,
        ctrlKey: e.ctrlKey,
        shiftKey: e.shiftKey,
        altKey: e.altKey,
        metaKey: e.metaKey,
      });
      if (!combo) return; // 只按下修饰键：继续等主键
      const clash = validateOverride(id, combo, combos);
      if (clash) {
        setNotice(clash);
        return;
      }
      const next: ShortcutOverrides = { ...overrides, [id]: combo };
      persist(next);
      setNotice('');
      setRecording('');
    };
    // 捕获阶段拦在输入框与全局注册中心之前，避免录制时误触发功能
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  }, [recording, combos, overrides]);

  const groupOrder = [...new Set(SHORTCUT_ACTIONS.map((a) => a.group))];

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="快捷键" desc="点击已注册的组合可录制新按键，改键即时生效并保存；按 Esc 取消" />

      {groupOrder.map((group) => (
        <Box key={group} sx={{ mb: 2.5 }}>
          <Typography sx={{ fontSize: '13px', fontWeight: 700, color: 'var(--tt-text-3)', mb: 1 }}>{group}</Typography>
          {SHORTCUT_ACTIONS.filter((a) => a.group === group).map((a) => {
            const status = shortcutStatusOf(a, registered, quickPickOn);
            const combo = combos[a.id] ?? a.defaultCombo ?? '';
            const customized = Boolean(overrides[a.id]);
            const isRecording = recording === a.id;
            return (
              <SettingRow
                key={a.id}
                label={a.label}
                sub={a.note}
                control={
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, justifyContent: 'flex-end' }}>
                    <Typography sx={{ fontSize: '11.5px', color: status.active ? '#0D9488' : 'var(--tt-text-4)' }}>
                      {status.text}
                    </Typography>
                    <Box
                      onClick={status.recordable ? () => { setNotice(''); setRecording(isRecording ? '' : a.id); } : undefined}
                      sx={{
                        px: 1.5,
                        py: 0.5,
                        borderRadius: '8px',
                        border: isRecording ? '1px solid #0D9488' : status.recordable ? '1px dashed #D1D5DB' : '1px solid var(--tt-border)',
                        bgcolor: isRecording ? '#E6F7F4' : 'var(--tt-surface-2)',
                        cursor: status.recordable ? 'pointer' : 'default',
                        minWidth: 108,
                        textAlign: 'center',
                      }}
                    >
                      <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-2)', fontFamily: 'Consolas, monospace' }}>
                        {isRecording ? '按下新组合…' : displayCombo(combo)}
                      </Typography>
                    </Box>
                    {customized ? (
                      <Box
                        onClick={() => {
                          const next: ShortcutOverrides = { ...overrides };
                          delete next[a.id];
                          persist(next);
                          setNotice('');
                        }}
                        sx={{ px: 1, py: 0.4, borderRadius: '8px', cursor: 'pointer' }}
                      >
                        <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>恢复默认</Typography>
                      </Box>
                    ) : null}
                  </Box>
                }
              />
            );
          })}
        </Box>
      ))}

      {notice ? (
        <Typography sx={{ fontSize: '12px', color: '#EF4444', mb: 1.5 }}>{notice}</Typography>
      ) : null}

      <Typography sx={{ fontSize: '14px', fontWeight: 700, color: 'var(--tt-text-1)', mt: 3, mb: 1.5 }}>
        QuickPick 快速任务窗
      </Typography>
      <SettingRow
        label="启用 QuickPick"
        sub={
          quickPickOn
            ? '已启用：按下方组合键打开快速任务窗，输入目标后回车直接提交'
            : '关闭时组合键不会注册（按了没反应），打开后立即生效'
        }
        control={
          <Switch
            checked={quickPickOn}
            onChange={(e) => updateAppSettings({ quickPickEnabled: e.target.checked })}
            sx={tealSwitch}
          />
        }
      />
      <SettingRow
        label="触发模式"
        sub={
          qpMode === '双击修饰键'
            ? '350ms 内连续两次单独按下 Ctrl 打开（中间按了其它键则重新计时）'
            : '按一次下方组合键打开快速任务窗'
        }
        control={<PrefSelect value={qpMode} options={['组合键', '双击修饰键']} onChange={setQpMode} />}
      />
      <SettingRow
        label="触发按键"
        sub="应用内快捷键：仅当任我行窗口位于前台时生效；系统级全局热键尚未提供"
        control={
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, justifyContent: 'flex-end' }}>
            <Typography sx={{ fontSize: '11.5px', color: quickPickOn ? '#0D9488' : 'var(--tt-text-4)' }}>
              {quickPickOn ? '已注册' : '未启用'}
            </Typography>
            <Box sx={{ px: 1.5, py: 0.6, borderRadius: '8px', border: '1px solid var(--tt-border)', bgcolor: 'var(--tt-surface-2)', minWidth: 108, textAlign: 'center' }}>
              <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', fontFamily: 'Consolas, monospace' }}>
                {qpMode === '双击修饰键' ? '双击 Ctrl' : displayCombo(combos.quickPick ?? 'Alt+Space')}
              </Typography>
            </Box>
            <Button
              size="small"
              variant="outlined"
              disabled={!quickPickOn}
              onClick={() => useAppStore.getState().setQuickPickOpen(true)}
              sx={{ fontSize: '11.5px', textTransform: 'none', borderRadius: '8px', minWidth: 0, px: 1.25 }}
            >
              立即试用
            </Button>
          </Box>
        }
      />
    </Box>
  );
};

// ── 分区七：意识 ───────────────────────────────────────────────────

/** 意识存储根目录（运行目录规范：用户数据位于 ~/.tongtianwork） */
const AWARENESS_DIR = '~/.tongtianwork/awareness/main';

interface AwarenessFileDef {
  key: string;
  file: string;
  title: string;
  desc: string;
  icon: React.ReactNode;
  defaultContent: string;
}

/** 协作风格预设（SOUL.md 模板，默认高效极简） */
interface StylePreset {
  id: string;
  name: string;
  tagline: string;
  content: string;
}
const STYLE_PRESETS: StylePreset[] = [
  {
    id: 'efficient',
    name: '高效极简',
    tagline: '用最少的话把事情做好',
    content: `# SOUL.md - 协作风格 - 高效极简

用最少的话把事情做好。

## 核心原则
- **直接给结果。** 代码优于描述。能用一行说清的不用三段。
- **不要复述问题。** 我知道我问了什么，直接回答。
- **默认执行。** 方案明确时直接做，不用每步都确认。
- **不确定时一句话确认。** 不要列出一堆选项让我选，给出你的判断，问我对不对。

## 沟通风格
- 回复尽可能简短
- 跳过寒暄、过渡句和总结性废话
- 代码注释只在逻辑不明显时添加
- 用列表而非段落

## 边界
- 高风险操作前简短确认
- 私密信息不外泄`,
  },
  {
    id: 'thoughtful',
    name: '深思熟虑',
    tagline: '想清楚再动手，做对比做快重要',
    content: `# SOUL.md - 协作风格 - 深思熟虑

想清楚再动手，做对比做快重要。

## 核心原则
- **先理解再行动。** 复杂任务先梳理全貌，避免方向错误返工。
- **说明推理过程。** 重要决策时展示你的思路，让我能判断逻辑是否正确。
- **权衡利弊。** 有多种方案时列出各自优劣，给出你的倾向但让我做最终决定。
- **指出风险。** 看到潜在问题或边界情况时主动提醒，即使我没有问。

## 沟通风格
- 结论先行，再展开细节
- 区分“确定的事实”和“我的判断”
- 必要时画出思路链条
- 不确定的地方明确标注

## 边界
- 不为速度牺牲正确性
- 承认不确定，不编造信息
- 私密信息不外泄`,
  },
  {
    id: 'gentle',
    name: '温和耐心',
    tagline: '像一位好搭档一样，确保我们在同一频道上',
    content: `# SOUL.md - 协作风格 - 温和耐心

像一位好搭档一样，确保我们在同一频道上。

## 核心原则
- **用通俗语言。** 技术概念用日常用语解释，避免不必要的术语堆砌。
- **分步引导。** 复杂任务拆成小步骤，不一次性堆砌所有信息。
- **主动确认意图。** 不确定我想要什么时，先确认再动手，避免做了一堆结果方向不对。
- **做完后说明。** 完成操作后简要说明做了什么和为什么这样做。

## 沟通风格
- 回复有清晰的结构，便于阅读
- 主动提供有用的背景知识
- 语气友好自然，不过度正式
- 犯错时坦然承认并修正

## 边界
- 操作前确认，特别是涉及删除或外部调用时
- 不假设我的技术水平，该解释时就解释
- 私密信息不外泄`,
  },
  {
    id: 'decisive',
    name: '果断执行',
    tagline: '你来判断，你来做，我来验收',
    content: `# SOUL.md - 协作风格 - 果断执行

你来判断，你来做，我来验收。

## 核心原则
- **自己做决定。** 方案有多种时选最优的直接执行，不要列选项等我拍板。
- **只在高风险时才确认。** 日常操作不要问我，只有破坏性操作或架构级变更时才暂停确认。
- **发现问题主动修。** 看到 bug、坏味道或可优化的地方，直接修复，事后告知即可。
- **有观点就说。** 我的方案有问题时直接指出，不要委婉暗示。给明确的建议，不给模棱两可的“你可以考虑...”。

## 沟通风格
- 以行动为主，解释为辅
- 汇报用简短总结：做了什么、结果如何
- 不需要征询式的语气
- 有把握的事情断言，不加“可能”、“也许”

## 边界
- 涉及资金、数据删除、对外发布时必须确认
- 不替我做需要人类判断的商业决策
- 私密信息不外泄`,
  },
];

/** 意识能力模块 ↔ 存储文件映射（每个能力一个 .md 落盘文件） */
const AWARENESS_FILES: AwarenessFileDef[] = [
  {
    key: 'style',
    file: 'SOUL.md',
    title: '协作风格',
    desc: '定义沟通与协作方式',
    icon: <StyleOutlinedIcon sx={{ fontSize: 18, color: '#14B8A6' }} />,
    defaultContent: STYLE_PRESETS[0].content,
  },
  {
    key: 'manual',
    file: 'WORK_MANUAL.md',
    title: '工作手册',
    desc: '规范与行为准则，可自定义',
    icon: <MenuBookOutlinedIcon sx={{ fontSize: 18, color: '#6366F1' }} />,
    defaultContent: '# 工作手册\n\n## 工作规范\n- （在此补充你的规范）\n\n## 常用流程\n- （在此补充常用流程）',
  },
  {
    key: 'profile',
    file: 'USER_PROFILE.md',
    title: '用户画像',
    desc: '记录基本信息、偏好与习惯',
    icon: <PersonOutlineIcon sx={{ fontSize: 18, color: '#F59E0B' }} />,
    defaultContent: '# 用户画像\n\n## 基本信息\n- 称呼：\n\n## 偏好\n- （待补充）\n\n## 习惯\n- （待补充）',
  },
  {
    key: 'memory',
    file: 'MEMORY.md',
    title: '长期记忆',
    desc: '跨会话持久保存重要知识与结论',
    icon: <HistoryOutlinedIcon sx={{ fontSize: 18, color: '#EC4899' }} />,
    defaultContent: '# 长期记忆\n\n（暂无记忆条目，可在下方手动新增）',
  },
];

function fmtAwarenessSize(content: string): string {
  const bytes = new Blob([content]).size;
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;
}

const AwarenessPanel: React.FC = () => {
  /** 意识视图（awareness:get）：画像 + 目录 + 四个能力文件，真实来源在意识目录 */
  const [view, setView] = useState<AwarenessView | null>(null);
  const [memories, setMemories] = useState<MemoryEntry[]>([]);
  const [loadError, setLoadError] = useState('');
  const [saveError, setSaveError] = useState('');

  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [editContent, setEditContent] = useState('');

  // 协作风格选择弹窗（预设 SOUL.md 模板：高效极简/深思熟虑/温和耐心/果断执行）
  const [styleDialogOpen, setStyleDialogOpen] = useState(false);
  const [styleSel, setStyleSel] = useState<AwarenessStyleId>('efficient');
  const [styleContent, setStyleContent] = useState('');

  // 长期记忆条目新增输入
  const [memoryDraft, setMemoryDraft] = useState('');
  const [memoryBusy, setMemoryBusy] = useState(false);

  const cfg = view ?? { mode: true, autoMemory: true, style: 'efficient' as AwarenessStyleId, updatedAt: '', dir: null, files: {} };
  const currentStyleId: string = cfg.style;
  const currentStyle = STYLE_PRESETS.find((s) => s.id === currentStyleId) ?? STYLE_PRESETS[0];

  const [awarenessPath, setAwarenessPath] = useState<string>(AWARENESS_DIR);
  const [openMsg, setOpenMsg] = useState('');

  const reload = React.useCallback(async () => {
    try {
      const [v, rows] = await Promise.all([getAwareness(), listMemories()]);
      setView(v);
      setMemories(rows);
      if (v.dir) setAwarenessPath(v.dir);
      setLoadError('');
    } catch (e) {
      setLoadError(errorTextOf(e, '意识内容读取失败'));
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  /** 画像/能力文件统一经 awareness:update 落盘（桌面 Rust、引擎直连引擎、Mock 本地台账） */
  const applyPatch = async (patch: AwarenessPatch) => {
    try {
      const next = await updateAwareness(patch);
      setView(next);
      setMemories(await listMemories());
      if (next.dir) setAwarenessPath(next.dir);
      setSaveError('');
      return true;
    } catch (e) {
      setSaveError(errorTextOf(e, '意识内容保存失败'));
      return false;
    }
  };

  /** 在系统文件管理器中打开意识目录 */
  const openAwarenessFolder = () => {
    void (async () => {
      try {
        // 意识目录由引擎打开：端口按当前工作空间解析，不写死 18000
        const baseUrl = await resolveEngineBaseUrl();
        const r = await engineFetch(`${baseUrl}/api/files/open-folder`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: awarenessPath }),
        });
        const json = await r.json();
        if (!json || json.ok !== true) throw new Error((json && json.message) || '打开失败');
        setOpenMsg('已在文件管理器打开');
        setTimeout(() => setOpenMsg(''), 2500);
      } catch (e) {
        setOpenMsg(errorTextOf(e, '无可用引擎，无法打开文件夹'));
        setTimeout(() => setOpenMsg(''), 3000);
      }
    })();
  };

  /** 已定制 = 意识目录里的正文与默认模板不同（本地台账不落盘时同样可比对） */
  const getEntry = (def: AwarenessFileDef): AwarenessFileEntry =>
    view?.files?.[def.file as AwarenessFileName] ?? { content: def.defaultContent, updatedAt: '系统默认' };

  const isCustomized = (def: AwarenessFileDef): boolean =>
    Boolean(view?.files?.[def.file as AwarenessFileName]) &&
    getEntry(def).content.trim() !== def.defaultContent.trim();

  const patchCfg = (patch: Partial<Pick<AwarenessProfile, 'mode' | 'autoMemory'>>) => {
    void applyPatch(patch);
  };

  const openFile = (def: AwarenessFileDef) => {
    setEditingKey(def.key);
    setEditContent(getEntry(def).content);
  };

  const openStyleDialog = () => {
    setStyleSel(currentStyleId as AwarenessStyleId);
    setStyleContent(getEntry(AWARENESS_FILES[0]).content);
    setStyleDialogOpen(true);
  };

  const selectStyleTab = (id: string) => {
    setStyleSel(id as AwarenessStyleId);
    const preset = STYLE_PRESETS.find((s) => s.id === id);
    if (preset) {
      // 切到当前已保存风格时保留已定制正文，其余风格加载预设
      setStyleContent(id === currentStyleId ? getEntry(AWARENESS_FILES[0]).content : preset.content);
    }
  };

  /** 风格预设 id 与 SOUL.md 正文一起提交（awareness:update） */
  const saveStyle = () => {
    const content = styleContent;
    const style = styleSel;
    setStyleDialogOpen(false);
    void applyPatch({ style, files: { 'SOUL.md': content } });
  };

  const saveFile = () => {
    if (!editingDef) return;
    const content = editContent;
    setEditingKey(null);
    void applyPatch({ files: { [editingDef.file as AwarenessFileName]: content } });
  };

  /** 重置为默认：把默认模板写回意识目录里的这个文件（其他文件不动） */
  const resetFile = () => {
    if (!editingDef) return;
    const template = editingDef.defaultContent;
    setEditingKey(null);
    void applyPatch({ files: { [editingDef.file as AwarenessFileName]: template } });
  };

  /** 新增一条长期记忆（awareness:memoryAdd，空内容由后端拒绝） */
  const submitMemory = async () => {
    const text = memoryDraft.trim();
    if (!text || memoryBusy) return;
    setMemoryBusy(true);
    try {
      await addMemory(text);
      setMemories(await listMemories());
      setMemoryDraft('');
      setSaveError('');
    } catch (e) {
      setSaveError(errorTextOf(e, '记忆条目保存失败'));
    } finally {
      setMemoryBusy(false);
    }
  };

  /** 删除用户点名的那一条记忆（awareness:memoryDelete） */
  const removeMemory = async (id: string) => {
    if (memoryBusy) return;
    setMemoryBusy(true);
    try {
      await deleteMemory(id);
      setMemories(await listMemories());
      setSaveError('');
    } catch (e) {
      setSaveError(errorTextOf(e, '记忆条目删除失败'));
    } finally {
      setMemoryBusy(false);
    }
  };

  const editingDef = AWARENESS_FILES.find((f) => f.key === editingKey) ?? null;

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="意识" desc="管理会注入每轮对话的协作风格、用户画像、工作手册与长期记忆" />

      {(loadError || saveError) && (
        <Box sx={{ bgcolor: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.3)', borderRadius: '10px', px: 2, py: 1.25, mb: 1.5 }}>
          <Typography sx={{ fontSize: '12px', color: '#EF4444' }}>{loadError || saveError}</Typography>
        </Box>
      )}

      <SettingRow
        label="意识模式"
        sub="启用后，下方能力文件会作为上下文注入每轮对话"
        control={<Switch checked={cfg.mode} onChange={(e) => patchCfg({ mode: e.target.checked })} sx={tealSwitch} />}
      />
      <SettingRow
        label="自动记忆"
        sub="任务成功后，把目标、结论与产物摘要写入 MEMORY.md；疑似凭据不写入，可在下方查看和删除"
        control={<Switch checked={cfg.autoMemory} onChange={(e) => patchCfg({ autoMemory: e.target.checked })} sx={tealSwitch} />}
      />

      <Box sx={{ opacity: cfg.mode ? 1 : 0.45, pointerEvents: cfg.mode ? 'auto' : 'none' }}>
        {/* 意识信息：存储位置 + 能力模块文件 */}
        <Typography sx={{ fontSize: '14px', fontWeight: 600, color: 'var(--tt-text-1)', mt: 2, mb: 1.5 }}>
          意识信息
        </Typography>

        {/* 存储位置 */}
        <Box
          sx={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            bgcolor: 'var(--tt-surface)',
            border: '1px solid var(--tt-border)',
            borderRadius: '14px',
            px: 2.5,
            py: 2,
            mb: 1.5,
          }}
        >
          <Box sx={{ minWidth: 0, pr: 2 }}>
            <Typography sx={{ fontSize: '14px', fontWeight: 500, color: 'var(--tt-text-1)' }}>存储位置</Typography>
            <Typography
              sx={{
                fontSize: '12px',
                color: 'var(--tt-text-4)',
                mt: 0.25,
                fontFamily: 'Consolas, monospace',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {awarenessPath}
            </Typography>
          </Box>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexShrink: 0 }}>
            {openMsg && (
              <Typography sx={{ fontSize: '11px', color: '#0D9488' }}>{openMsg}</Typography>
            )}
            <Box
              onClick={openAwarenessFolder}
              sx={{
                display: 'flex',
                alignItems: 'center',
                gap: 0.5,
                px: 1.5,
                py: 0.6,
                borderRadius: '8px',
                border: '1px solid var(--tt-border)',
                cursor: 'pointer',
                '&:hover': { borderColor: '#14B8A6' },
              }}
            >
              <FolderOpenOutlinedIcon sx={{ fontSize: 14, color: 'var(--tt-text-3)' }} />
              <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-2)' }}>打开文件夹</Typography>
            </Box>
          </Box>
        </Box>

        {/* 能力模块列表：每个能力 ↔ 一个 .md 文件 */}
        {AWARENESS_FILES.map((def) => {
          const entry = getEntry(def);
          const customized = isCustomized(def);
          return (
            <Box
              key={def.key}
              onClick={() => (def.key === 'style' ? openStyleDialog() : openFile(def))}
              sx={{
                display: 'flex',
                alignItems: 'center',
                gap: 1.5,
                bgcolor: 'var(--tt-surface)',
                border: '1px solid var(--tt-border)',
                borderRadius: '14px',
                px: 2.5,
                py: 1.75,
                mb: 1.5,
                cursor: 'pointer',
                transition: 'border-color 0.15s ease',
                '&:hover': { borderColor: '#14B8A6' },
              }}
            >
              <Box
                sx={{
                  width: 34,
                  height: 34,
                  borderRadius: '10px',
                  bgcolor: 'var(--tt-surface-2)',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  flexShrink: 0,
                }}
              >
                {def.icon}
              </Box>
              <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                <Typography sx={{ fontSize: '13.5px', fontWeight: 600, color: 'var(--tt-text-1)' }}>{def.title}</Typography>
                <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mt: 0.25 }}>{def.desc}</Typography>
              </Box>
              <Box sx={{ textAlign: 'right', flexShrink: 0 }}>
                <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', fontFamily: 'Consolas, monospace' }}>
                  {def.key === 'style' ? `${def.file} · ${currentStyle.name}` : def.file} · {fmtAwarenessSize(entry.content)} · {fmtAwarenessUpdatedAt(entry.updatedAt)}
                </Typography>
                <Typography sx={{ fontSize: '11px', color: customized ? '#0D9488' : 'var(--tt-text-4)', mt: 0.25 }}>
                  {customized ? '已定制' : '系统默认'} · 查看 ›
                </Typography>
              </Box>
            </Box>
          );
        })}

        {/* 长期记忆条目 */}
        <Typography sx={{ fontSize: '14px', fontWeight: 600, color: 'var(--tt-text-1)', mt: 2.5, mb: 1 }}>
          长期记忆条目
        </Typography>
        {memories.length === 0 && (
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mb: 1.5 }}>
            暂无记忆条目，可在下方手动新增
          </Typography>
        )}
        {memories.map((m) => (
          <Box
            key={m.id}
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 1.5,
              bgcolor: 'var(--tt-surface)',
              border: '1px solid var(--tt-border)',
              borderRadius: '10px',
              px: 2,
              py: 1.25,
              mb: 0.75,
            }}
          >
            <Box sx={{ flexGrow: 1, minWidth: 0 }}>
              <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-1)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {m.content}
              </Typography>
              <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)', mt: 0.25 }}>
                {m.source} · {fmtAwarenessUpdatedAt(m.createdAt)}
              </Typography>
            </Box>
            <IconButton size="small" onClick={() => void removeMemory(m.id)} disabled={memoryBusy} sx={{ color: 'var(--tt-text-4)', '&:hover': { color: '#EF4444' } }}>
              <DeleteOutlineIcon sx={{ fontSize: 16 }} />
            </IconButton>
          </Box>
        ))}
        <Box sx={{ display: 'flex', gap: 1, mt: 1 }}>
          <TextField
            size="small"
            fullWidth
            placeholder="手动添加一条记忆…"
            value={memoryDraft}
            onChange={(e) => setMemoryDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void submitMemory(); }}
            disabled={memoryBusy}
            sx={{ '& .MuiInputBase-input': { fontSize: '12.5px' } }}
          />
          <Button
            size="small"
            variant="contained"
            onClick={() => void submitMemory()}
            disabled={memoryBusy || !memoryDraft.trim()}
            sx={{ bgcolor: 'var(--tt-text-1)', fontSize: '12px', minWidth: 64 }}
          >
            添加
          </Button>
        </Box>
      </Box>
      <Dialog open={Boolean(editingDef)} onClose={() => setEditingKey(null)} maxWidth="md" fullWidth>
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>
          {editingDef ? `${editingDef.file} · ${editingDef.title}` : ''}
        </DialogTitle>
        <DialogContent sx={{ pt: '8px !important' }}>
          <TextField
            fullWidth
            multiline
            minRows={14}
            value={editContent}
            onChange={(e) => setEditContent(e.target.value)}
            sx={{ '& .MuiInputBase-input': { fontSize: '13px', fontFamily: 'Consolas, monospace', lineHeight: 1.7 } }}
          />
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button size="small" onClick={resetFile} sx={{ color: '#EF4444', fontSize: '12px' }}>
            重置为默认
          </Button>
          <Box sx={{ flexGrow: 1 }} />
          <Button size="small" onClick={() => setEditingKey(null)} sx={{ color: 'var(--tt-text-3)', fontSize: '12px' }}>
            取消
          </Button>
          <Button size="small" variant="contained" onClick={saveFile} sx={{ bgcolor: 'var(--tt-text-1)', fontSize: '12px' }}>
            保存
          </Button>
        </DialogActions>
      </Dialog>

      {/* 协作风格选择弹窗：预设 SOUL.md 模板作为起点，可在此基础上修改 */}
      <Dialog open={styleDialogOpen} onClose={() => setStyleDialogOpen(false)} maxWidth="md" fullWidth>
        <DialogTitle sx={{ pb: 0.5 }}>
          <Typography sx={{ fontSize: '16px', fontWeight: 700, color: 'var(--tt-text-1)' }}>选择协作风格</Typography>
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mt: 0.5 }}>
            选择一种预设风格作为起点，你可以在此基础上修改
          </Typography>
        </DialogTitle>
        <DialogContent sx={{ pt: '12px !important' }}>
          <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', mb: 1.5 }}>
            {STYLE_PRESETS.map((s) => (
              <Box
                key={s.id}
                onClick={() => selectStyleTab(s.id)}
                sx={{
                  px: 1.75,
                  py: 0.75,
                  borderRadius: '999px',
                  border: styleSel === s.id ? '1.5px solid #14B8A6' : '1px solid var(--tt-border)',
                  bgcolor: styleSel === s.id ? 'rgba(20, 184, 166, 0.08)' : 'var(--tt-surface-2)',
                  cursor: 'pointer',
                  transition: 'border-color 0.15s ease',
                }}
              >
                <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: styleSel === s.id ? '#0D9488' : 'var(--tt-text-2)' }}>
                  {s.name}
                </Typography>
              </Box>
            ))}
          </Box>
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mb: 1 }}>
            {STYLE_PRESETS.find((s) => s.id === styleSel)?.tagline ?? ''}
          </Typography>
          <TextField
            fullWidth
            multiline
            minRows={14}
            value={styleContent}
            onChange={(e) => setStyleContent(e.target.value)}
            sx={{ '& .MuiInputBase-input': { fontSize: '13px', fontFamily: 'Consolas, monospace', lineHeight: 1.7 } }}
          />
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button size="small" onClick={() => setStyleDialogOpen(false)} sx={{ color: 'var(--tt-text-3)', fontSize: '12px' }}>
            取消
          </Button>
          <Button size="small" variant="contained" onClick={saveStyle} sx={{ bgcolor: 'var(--tt-text-1)', fontSize: '12px' }}>
            保存
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

// ── 分区：品牌外观（单次自定义名称与图标） ─────────────────────

/** epoch ms → 本地时间（设置页展示「修改机会使用时间」） */
function formatBrandTime(ms: number): string {
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** PNG 文件 → base64（与 Rust `branding_customize` 的入参一致；非 PNG 直接拒绝） */
async function pngFileToBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  const magic = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
  if (bytes.length < 8 || magic.some((b, i) => bytes[i] !== b)) {
    throw new Error('NOT_PNG');
  }
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

/** 品牌标识预览：自定义图标优先，否则展示安装包默认品牌图 */
const BrandMark: React.FC<{ name: string; icon: string | null; size?: number }> = ({
  name,
  icon,
  size = 36,
}) =>
  icon ? (
    <Box
      component="img"
      src={icon}
      alt={name}
      sx={{
        width: size,
        height: size,
        borderRadius: '10px',
        objectFit: 'cover',
        border: '1px solid var(--tt-border-soft)',
        flexShrink: 0,
      }}
    />
  ) : (
    <Box
      sx={{
        width: size,
        height: size,
        borderRadius: '10px',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        flexShrink: 0,
        bgcolor: '#fff',
        border: '1px solid var(--tt-border-soft)',
      }}
    >
      <Box
        component="img"
        src={brandLogo}
        alt={name}
        sx={{ width: '76%', height: '76%', objectFit: 'contain', display: 'block' }}
      />
    </Box>
  );

/**
 * 品牌外观。
 * 约束：安装后只有一次修改机会（Rust 落 `appBrandCustomizedAt` 后即锁定）；
 * 只改窗口标题、任务栏/托盘图标与界面内品牌，快捷方式与应用文件名由安装包决定。
 */
const BrandingPanel: React.FC = () => {
  const { t: tr } = useI18n();
  const info = useBrandingStore((s) => s.info);
  const loaded = useBrandingStore((s) => s.loaded);
  const customize = useBrandingStore((s) => s.customize);

  const [name, setName] = useState(info.name);
  const [iconBase64, setIconBase64] = useState<string | null>(null);
  const [iconPreview, setIconPreview] = useState<string | null>(null);
  const [iconName, setIconName] = useState('');
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<BrandingInfo | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const nameInitialized = useRef(false);

  // 首次读到真实品牌后回填输入框；之后不覆盖用户正在输入的内容
  useEffect(() => {
    if (!loaded || nameInitialized.current) return;
    setName(info.name);
    nameInitialized.current = true;
  }, [loaded, info.name]);

  // 预览用 object URL 在替换/卸载时释放
  useEffect(
    () => () => {
      if (iconPreview) URL.revokeObjectURL(iconPreview);
    },
    [iconPreview],
  );

  const canEdit = info.platform !== 'browser' && !info.customized;
  const trimmed = name.trim();
  const nameChanged = trimmed !== '' && trimmed !== info.name;
  const dirty = nameChanged || Boolean(iconBase64);

  const pickFile = async (file: File | undefined) => {
    if (!file) return;
    setError('');
    if (file.type && file.type !== 'image/png') {
      setError(tr('只支持 PNG 图片，其它格式请先转换'));
      return;
    }
    if (file.size > 4 * 1024 * 1024) {
      setError(tr('图片不能超过 4 MB'));
      return;
    }
    try {
      const b64 = await pngFileToBase64(file);
      setIconBase64(b64);
      setIconName(file.name);
      setIconPreview(URL.createObjectURL(file));
    } catch {
      setError(tr('文件不是有效的 PNG 图片'));
    }
  };

  const clearIcon = () => {
    setIconBase64(null);
    setIconName('');
    setIconPreview(null);
  };

  const submit = async () => {
    if (!canEdit || busy) return;
    setConfirmOpen(false);
    setBusy(true);
    setError('');
    try {
      const next = await customize({
        name: nameChanged ? trimmed : null,
        iconBase64: iconBase64 ?? null,
      });
      setResult(next);
      clearIcon();
    } catch (e) {
      setError(errorTextOf(e, tr('品牌修改失败')));
    } finally {
      setBusy(false);
    }
  };

  const appliedTargets = result
    ? ([
        result.applied?.windowTitle ? tr('窗口标题') : '',
        result.applied?.windowIcon ? tr('任务栏图标') : '',
        result.applied?.trayIcon ? tr('托盘图标') : '',
      ].filter(Boolean) as string[])
    : [];

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="品牌外观" desc="修改应用显示名称与图标；安装后仅有一次修改机会" />

      <SettingRow
        label={tr('当前品牌')}
        sub={info.customized ? tr('已使用自定义品牌') : tr('安装包默认品牌')}
        control={
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.25 }}>
            <BrandMark name={info.name} icon={iconPreview ?? info.iconDataUrl} size={34} />
            <Typography sx={{ fontSize: '14px', fontWeight: 600, color: 'var(--tt-text-1)' }}>
              {info.name}
            </Typography>
          </Box>
        }
      />
      <SettingRow
        label={tr('署名')}
        sub={tr('开发者与版权归属，随安装包发布')}
        control={
          <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-2)' }}>
            {info.attribution}
          </Typography>
        }
      />

      {info.customized ? (
        <>
          <SettingRow
            label={tr('修改机会')}
            sub={
              info.customizedAt
                ? tr('已于 {time} 使用，应用内不再提供第二次修改', {
                    time: formatBrandTime(info.customizedAt),
                  })
                : tr('已使用')
            }
            control={<Typography sx={{ fontSize: '13px', color: 'var(--tt-text-3)' }}>{tr('已使用')}</Typography>}
          />
          {result && (
            <Box
              sx={{
                mt: 1.5,
                p: 2,
                borderRadius: '12px',
                border: '1px solid rgba(20, 184, 166, 0.35)',
                bgcolor: 'rgba(20, 184, 166, 0.08)',
              }}
            >
              <Typography sx={{ fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-1)' }}>
                {appliedTargets.length > 0
                  ? tr('已生效：{targets}', { targets: appliedTargets.join('、') })
                  : tr('界面内品牌已更新，系统窗口不在当前通道')}
              </Typography>
              {result.warnings?.map((w) => (
                <Typography key={w} sx={{ fontSize: '12px', color: 'var(--tt-text-3)', mt: 0.5 }}>
                  {w}
                </Typography>
              ))}
            </Box>
          )}
        </>
      ) : !canEdit ? (
        <SettingRow
          label={tr('修改入口')}
          unavailable
          sub={tr('当前通道没有桌面窗口与托盘，请在 Windows 桌面版中修改')}
          control={<Typography sx={{ fontSize: '13px', color: 'var(--tt-text-3)' }}>—</Typography>}
        />
      ) : (
        <Box
          sx={{
            mt: 2,
            p: 2.5,
            borderRadius: '14px',
            border: '1px solid var(--tt-border)',
            bgcolor: 'var(--tt-surface)',
          }}
        >
          <Typography sx={{ fontSize: '13.5px', fontWeight: 600, color: 'var(--tt-text-1)' }}>
            {tr('应用名称')}
          </Typography>
          <TextField
            size="small"
            fullWidth
            value={name}
            onChange={(e) => setName(e.target.value)}
            inputProps={{ maxLength: 24 }}
            sx={{ mt: 1, maxWidth: 360 }}
          />
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mt: 0.75 }}>
            {tr('1–24 个字符；留空表示保留当前名称')}
          </Typography>

          <Typography sx={{ fontSize: '13.5px', fontWeight: 600, color: 'var(--tt-text-1)', mt: 2.5 }}>
            {tr('应用图标')}
          </Typography>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mt: 1 }}>
            <BrandMark name={trimmed || info.name} icon={iconPreview ?? info.iconDataUrl} size={44} />
            <Button
              size="small"
              variant="outlined"
              startIcon={<ImageOutlinedIcon />}
              disabled={busy}
              onClick={() => fileRef.current?.click()}
              sx={{ textTransform: 'none', borderRadius: '8px' }}
            >
              {tr('选择 PNG 图片')}
            </Button>
            {iconBase64 && (
              <Button size="small" onClick={clearIcon} disabled={busy} sx={{ textTransform: 'none' }}>
                {tr('清除')}
              </Button>
            )}
            <input
              ref={fileRef}
              type="file"
              accept="image/png"
              hidden
              onChange={(e) => {
                const file = e.target.files?.[0];
                e.target.value = '';
                void pickFile(file);
              }}
            />
          </Box>
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mt: 0.75 }}>
            {tr('PNG，16×16 至 1024×1024 像素，不超过 4 MB')}
            {iconName ? ` · ${iconName}` : ''}
          </Typography>

          <Box
            sx={{
              mt: 2,
              p: 1.5,
              borderRadius: '10px',
              border: '1px solid rgba(245, 158, 11, 0.35)',
              bgcolor: 'rgba(245, 158, 11, 0.08)',
            }}
          >
            <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-2)', lineHeight: 1.7 }}>
              {tr('保存后立即生效且不可恢复，应用内不再提供第二次修改。')}
            </Typography>
            <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3)', lineHeight: 1.7, mt: 0.5 }}>
              {tr('生效范围：窗口标题、任务栏/托盘图标与界面内名称；已安装的桌面快捷方式名称与应用文件名不会改变。')}
            </Typography>
          </Box>

          {info.platform === 'mock' && (
            <Typography sx={{ fontSize: '12px', color: '#B45309', mt: 1 }}>
              {tr('Mock 模式：修改只保存在浏览器内存，不写入窗口标题或系统图标')}
            </Typography>
          )}

          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mt: 2 }}>
            <Button
              variant="contained"
              disabled={!dirty || busy}
              onClick={() => setConfirmOpen(true)}
              sx={{
                textTransform: 'none',
                borderRadius: '9px',
                background: 'linear-gradient(90deg, #14B8A6 0%, #06B6D4 100%)',
                '&.Mui-disabled': { background: 'rgba(148, 163, 184, 0.25)' },
              }}
            >
              {busy ? tr('正在应用…') : tr('保存并应用（仅一次）')}
            </Button>
            {!dirty && (
              <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>
                {tr('修改名称或选择图片后可提交')}
              </Typography>
            )}
          </Box>
          {error && (
            <Typography sx={{ fontSize: '12.5px', color: '#DC2626', mt: 1.5 }}>{error}</Typography>
          )}
        </Box>
      )}

      <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mt: 1.5, lineHeight: 1.7 }}>
        {tr('图标与名称保存在本机运行目录，重启后自动恢复；卸载重装不会重置已使用的修改机会。')}
      </Typography>

      <Dialog
        open={confirmOpen}
        onClose={() => !busy && setConfirmOpen(false)}
        PaperProps={{ sx: { borderRadius: '14px', minWidth: 360 } }}
      >
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>
          {tr('确认使用唯一的修改机会？')}
        </DialogTitle>
        <DialogContent>
          <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-2)', lineHeight: 1.8 }}>
            {tr('保存后立即生效，且应用内不再提供第二次修改或恢复默认的入口。')}
          </Typography>
          <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-1)', mt: 1.5 }}>
            {tr('新名称：')}
            {nameChanged ? trimmed : info.name}
          </Typography>
          <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-1)', mt: 0.5 }}>
            {tr('新图标：')}
            {iconBase64 ? tr('已选择 PNG 图片') : tr('保持不变')}
          </Typography>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button
            onClick={() => setConfirmOpen(false)}
            disabled={busy}
            sx={{ color: 'var(--tt-text-3)', fontSize: '13px', textTransform: 'none' }}
          >
            {tr('取消')}
          </Button>
          <Button
            onClick={() => void submit()}
            disabled={busy}
            variant="contained"
            sx={{ fontSize: '13px', textTransform: 'none', background: '#14B8A6' }}
          >
            {tr('确认修改')}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

// ── 分区八：更新应用 ───────────────────────────────────────────────

/**
 * 更新应用。
 * 此前这里把当前版本号写死在界面上，并挂了一个点了没有任何反应的检查更新按钮 ——
 * 应用没有更新渠道，按钮就是假的。现在版本取自 Rust `system_info`
 * （CARGO_PKG_VERSION；浏览器通道用构建期注入的 package.json 版本），
 * 更新方式如实说明为「覆盖安装包」，不再放假按钮。
 */
const UpdatePanel: React.FC = () => {
  const [info, setInfo] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    void (async () => {
      try {
        setInfo(await systemAPI.info());
        setError('');
      } catch (e) {
        setError(errorTextOf(e, '应用信息读取失败'));
      }
    })();
  }, []);

  const rawPlatform = typeof info?.platform === 'string' ? info.platform : '';
  const platformLabel: Record<string, string> = {
    windows: 'Windows',
    macos: 'macOS',
    linux: 'Linux',
    mock: '浏览器预览（Mock 通道）',
    'engine-direct': '浏览器直连引擎',
  };
  const version = typeof info?.version === 'string' ? info.version : '';

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="更新应用" desc="显示当前运行版本；更新由发布的安装包提供" />
      <SettingRow
        label="当前版本"
        sub={
          error
            ? error
            : !info
              ? '正在读取应用信息…'
              : version
                ? `v${version}${platformLabel[rawPlatform] ? ` · ${platformLabel[rawPlatform]}` : ''}`
                : '应用未报告版本号'
        }
        control={
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>未配置更新渠道</Typography>
        }
      />
      <SettingRow
        label="如何更新"
        sub="获取新版安装包（NSIS setup.exe 或 MSI）后直接覆盖安装，本地任务与会话数据不会被清除"
        control={<Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>覆盖安装</Typography>}
      />
    </Box>
  );
};

// ── 分区九：工作台 ─────────────────────────────────────────────────

/**
 * 幻灯片模板台账：内置主题随应用提供，导入的 .pptx 存放在本机模板目录。
 * 选中项写入 app settings（桌面端即 settings_kv），Rust runtime 与引擎读同一个 id。
 */
const PptxTemplatesSection: React.FC = () => {
  const { desktop, loading, error, templates, selectedId, root, notes, select, importFile, remove } =
    usePptxTemplates();
  const [busyId, setBusyId] = useState('');
  const [importing, setImporting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [actionError, setActionError] = useState('');
  const [actionMessage, setActionMessage] = useState('');
  const [pendingDelete, setPendingDelete] = useState<PptxTemplate | null>(null);

  const handleImport = async () => {
    if (!desktop) {
      setActionError('浏览器直连没有本机文件访问能力，请在桌面版导入模板');
      return;
    }
    setActionError('');
    setActionMessage('');
    try {
      const { open } = await import('@tauri-apps/plugin-dialog');
      const picked = await open({
        multiple: false,
        directory: false,
        title: '选择 .pptx 模板',
        filters: [{ name: 'PowerPoint 模板', extensions: ['pptx'] }],
      });
      if (typeof picked !== 'string' || !picked) return;
      setImporting(true);
      const next = await importFile(picked);
      const added = next.templates.find((t) => t.id === next.importedId);
      setActionMessage(
        added
          ? `已导入「${added.name}」，点「使用」后幻灯片模式才会切换`
          : '模板已导入',
      );
    } catch (e) {
      setActionError(errorTextOf(e, '模板导入失败'));
    } finally {
      setImporting(false);
    }
  };

  const handleSelect = async (id: string) => {
    setBusyId(id);
    setActionError('');
    setActionMessage('');
    try {
      await select(id);
    } catch (e) {
      setActionError(errorTextOf(e, '切换模板失败'));
    } finally {
      setBusyId('');
    }
  };

  const confirmDelete = async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    setActionError('');
    setActionMessage('');
    try {
      const name = pendingDelete.name;
      await remove(pendingDelete.id);
      setPendingDelete(null);
      setActionMessage(`模板「${name}」已移入回收目录（.trash/）`);
    } catch (e) {
      setActionError(errorTextOf(e, '删除模板失败'));
    } finally {
      setDeleting(false);
    }
  };

  const banner = actionError || error;
  const info = actionMessage || notes.join('；');

  return (
    <Box>
      <Box sx={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 2, mt: 3, mb: 1.5 }}>
        <Box sx={{ minWidth: 0 }}>
          <Typography sx={{ fontSize: '14px', fontWeight: 700, color: 'var(--tt-text-1)' }}>幻灯片模板</Typography>
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mt: 0.25 }}>
            {desktop
              ? `幻灯片模式生成 .pptx 时使用的版式；内置主题随应用提供，也可导入本机 .pptx 模板（${root || '模板目录不可用'}）`
              : '幻灯片模式生成 .pptx 时使用的版式；浏览器直连只能使用内置主题，导入请在桌面版操作'}
          </Typography>
        </Box>
        <Button
          size="small"
          variant="outlined"
          startIcon={importing ? <CircularProgress size={14} /> : <AddIcon />}
          disabled={!desktop || importing || loading}
          onClick={handleImport}
          sx={{ flexShrink: 0, textTransform: 'none', fontSize: '12px', borderColor: 'var(--tt-border)' }}
        >
          {importing ? '导入中…' : '导入 .pptx'}
        </Button>
      </Box>

      {banner && (
        <Box sx={{ bgcolor: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.3)', borderRadius: '10px', px: 2, py: 1.25, mb: 1.5 }}>
          <Typography sx={{ fontSize: '12px', color: '#EF4444' }}>{banner}</Typography>
        </Box>
      )}
      {info && (
        <Box sx={{ bgcolor: 'rgba(20,184,166,0.08)', border: '1px solid rgba(20,184,166,0.3)', borderRadius: '10px', px: 2, py: 1.25, mb: 1.5 }}>
          <Typography sx={{ fontSize: '12px', color: '#0D9488' }}>{info}</Typography>
        </Box>
      )}

      {loading ? (
        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 1, py: 4 }}>
          <CircularProgress size={16} />
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>正在读取模板…</Typography>
        </Box>
      ) : templates.length === 0 ? (
        <Box sx={{ py: 4, textAlign: 'center' }}>
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>模板目录为空，请重新加载或导入 .pptx</Typography>
        </Box>
      ) : (
        templates.map((tpl) => {
          const active = tpl.id === selectedId;
          const meta = tpl.builtin
            ? tpl.description
            : [
                tpl.size ? formatBytes(tpl.size) : '',
                tpl.importedAt ? `导入于 ${new Date(tpl.importedAt).toLocaleDateString()}` : '',
              ]
                .filter(Boolean)
                .join(' · ');
          return (
            <Box
              key={tpl.id}
              data-pptx-template={tpl.id}
              sx={{
                display: 'flex',
                alignItems: 'center',
                gap: 1.5,
                bgcolor: 'var(--tt-surface)',
                border: `1px solid ${active ? '#14B8A6' : 'var(--tt-border)'}`,
                borderRadius: '14px',
                px: 2.5,
                py: 1.75,
                mb: 1.5,
              }}
            >
              <Box
                sx={{
                  width: 40,
                  height: 40,
                  borderRadius: '10px',
                  border: '1px solid var(--tt-border)',
                  overflow: 'hidden',
                  display: 'flex',
                  flexDirection: 'column',
                  flexShrink: 0,
                }}
              >
                <Box sx={{ height: 9, bgcolor: tpl.theme?.accent ?? '#9CA3AF' }} />
                <Box sx={{ flex: 1, bgcolor: tpl.theme?.background ?? '#FFFFFF' }} />
              </Box>
              <Box sx={{ minWidth: 0, flex: 1 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}>
                  <Typography sx={{ fontSize: '14px', fontWeight: 500, color: 'var(--tt-text-1)' }}>{tpl.name}</Typography>
                  <Chip
                    size="small"
                    label={tpl.builtin ? '内置主题' : '本机导入'}
                    sx={{ height: 20, fontSize: '10.5px', bgcolor: 'rgba(107,114,128,0.12)', color: 'var(--tt-text-3)' }}
                  />
                  {!tpl.available && (
                    <Chip
                      size="small"
                      label="文件不可用"
                      sx={{ height: 20, fontSize: '10.5px', bgcolor: 'rgba(239,68,68,0.1)', color: '#EF4444' }}
                    />
                  )}
                </Box>
                <Typography sx={{ fontSize: '12px', color: tpl.available ? 'var(--tt-text-4)' : '#EF4444', mt: 0.25 }}>
                  {tpl.available ? meta : tpl.error || '模板文件已不存在，请重新导入'}
                </Typography>
              </Box>
              {active ? (
                <Chip
                  size="small"
                  label="使用中"
                  sx={{ flexShrink: 0, height: 24, fontSize: '11.5px', bgcolor: 'rgba(20,184,166,0.12)', color: '#0D9488', fontWeight: 600 }}
                />
              ) : (
                <Button
                  size="small"
                  variant="outlined"
                  disabled={!tpl.available || Boolean(busyId)}
                  onClick={() => handleSelect(tpl.id)}
                  sx={{ flexShrink: 0, minWidth: 64, textTransform: 'none', fontSize: '12px', borderColor: 'var(--tt-border)' }}
                >
                  {busyId === tpl.id ? <CircularProgress size={14} /> : '使用'}
                </Button>
              )}
              {!tpl.builtin && (
                <Tooltip title="删除模板（文件移入模板目录 .trash/）">
                  <span>
                    <IconButton size="small" disabled={deleting} onClick={() => setPendingDelete(tpl)}>
                      <DeleteOutlineIcon sx={{ fontSize: 18 }} />
                    </IconButton>
                  </span>
                </Tooltip>
              )}
            </Box>
          );
        })
      )}

      <Dialog open={Boolean(pendingDelete)} onClose={() => (deleting ? undefined : setPendingDelete(null))}>
        <DialogTitle sx={{ fontSize: '16px', fontWeight: 700 }}>删除幻灯片模板</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ fontSize: '13px' }}>
            将把「{pendingDelete?.name}」从模板列表中移除，文件移到模板目录的 .trash/ 下（不会直接删除）。
            {pendingDelete?.id === selectedId ? '当前正在使用该模板，删除后自动切回内置「商务蓝」。' : ''}
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setPendingDelete(null)} disabled={deleting} sx={{ textTransform: 'none' }}>
            取消
          </Button>
          <Button onClick={confirmDelete} color="error" disabled={deleting} sx={{ textTransform: 'none' }}>
            {deleting ? '删除中…' : '删除'}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

const WorkbenchModePanel: React.FC = () => {
  const [appSettings, updateAppSettings] = useAppSettings();
  const enabledModes = (appSettings.workbenchModes as string[] | undefined) ?? ['general', 'design', 'slides'];
  const modes: Record<string, boolean> = {
    general: enabledModes.includes('general'),
    design: enabledModes.includes('design'),
    slides: enabledModes.includes('slides'),
    writing: enabledModes.includes('writing'),
  };
  const defaultMode = (appSettings.workbenchDefaultMode as string) ?? '通用';
  // 缺省值与引擎 / Rust 的默认口径一致：8 轮、开启压缩、MCP 工具按需载入
  const maxIters = typeof appSettings.agentMaxIters === 'number' ? appSettings.agentMaxIters : 8;
  const autoCompress = appSettings.agentAutoCompress !== false;
  const lazyMcpTools = appSettings.agentLazyMcpTools !== false;

  const toggleMode = (id: string, on: boolean) => {
    const next = on ? Array.from(new Set([...enabledModes, id])) : enabledModes.filter((m) => m !== id);
    updateAppSettings({ workbenchModes: next });
  };

  // 模式定义与输入框共用（lib/use-app-settings 单一事实来源）
  const defs = WORKBENCH_MODE_DEFS;

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="工作台" desc="配置工作模式与新任务默认模式" />
      {defs.map((d) => (
        <SettingRow
          key={d.id}
          label={d.name}
          sub={d.desc}
          control={<Switch checked={modes[d.id]} onChange={(e) => toggleMode(d.id, e.target.checked)} sx={tealSwitch} />}
        />
      ))}
      <SettingRow label="新任务默认模式" control={<PrefSelect value={defaultMode} options={['通用', '设计', '幻灯片', '写作']} onChange={(v) => updateAppSettings({ workbenchDefaultMode: v })} />} />

      {/* 上下文与工具效率：两项开关都直接改变引擎执行事实（AgentScope 压缩 /
          MCP 工具按需启用），说明里写清代价与失败表现，不做「开了就变快」的承诺 */}
      <Typography sx={{ fontSize: '14px', fontWeight: 700, color: 'var(--tt-text-1)', mt: 3, mb: 1.5 }}>上下文与工具效率</Typography>
      <SettingRow
        label="单轮工具循环上限"
        sub="1–32 轮；达到上限时请求模型总结当前进展并结束本轮，不再继续调用工具"
        control={
          <Box sx={{ width: 220, display: 'flex', alignItems: 'center', gap: 1 }}>
            <Slider
              size="small"
              min={1}
              max={32}
              value={maxIters}
              onChange={(_, v) => updateAppSettings({ agentMaxIters: v as number })}
              sx={{ color: '#14B8A6' }}
            />
            <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', width: 46 }}>{maxIters} 轮</Typography>
          </Box>
        }
      />
      <SettingRow
        label="上下文自动压缩"
        sub="接近模型上下文窗口时先摘要旧消息再继续；摘要会额外调用一次模型，失败只说明原因、不中断任务。窗口值取自「模型与用量」，未填写按 8192 tokens 估算"
        control={
          <Switch
            checked={autoCompress}
            onChange={(e) => updateAppSettings({ agentAutoCompress: e.target.checked })}
            sx={tealSwitch}
          />
        }
      />
      <SettingRow
        label="连接器工具按需载入"
        sub="已连接 MCP 连接器的工具默认不占上下文；模型需要时先调用 search_tools 搜索并启用，再调用具体工具"
        control={
          <Switch
            checked={lazyMcpTools}
            onChange={(e) => updateAppSettings({ agentLazyMcpTools: e.target.checked })}
            sx={tealSwitch}
          />
        }
      />

      <PptxTemplatesSection />
    </Box>
  );
};

// ── 分区十：安全工作环境 ───────────────────────────────────────────

/** 字节数 → 人类可读（磁盘实测值，非估算） */
function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(1)} ${units[unit]}`;
}

const SandboxPanel: React.FC = () => {
  const [appSettings, updateAppSettings] = useAppSettings();
  const enabled = Boolean(appSettings.secureEnv);

  /** 工作环境文件用量：Rust/引擎实测磁盘，未落地时为 0，不是估算值 */
  const [usage, setUsage] = useState<SandboxEnvUsage | null>(null);
  /** 系统级隔离（AppContainer）：配置意图与真实生效状态分开保存 */
  const [systemSandbox, setSystemSandbox] = useState<SystemSandboxView | null>(null);
  const [systemSaving, setSystemSaving] = useState(false);
  const [envMessage, setEnvMessage] = useState('');
  const [envError, setEnvError] = useState('');
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [clearing, setClearing] = useState(false);

  const loadUsage = React.useCallback(async () => {
    try {
      setUsage(await sandboxAPI.envUsage());
      setEnvError('');
    } catch (e) {
      setEnvError(errorTextOf(e, '工作环境用量统计失败'));
    }
  }, []);

  const loadSystem = React.useCallback(async () => {
    try {
      setSystemSandbox(await sandboxAPI.systemGet());
    } catch (e) {
      setEnvError(errorTextOf(e, '系统隔离状态读取失败'));
    }
  }, []);

  useEffect(() => {
    void loadUsage();
    void loadSystem();
  }, [loadUsage, loadSystem]);

  const applySystem = async (enabled: boolean, network: boolean) => {
    setSystemSaving(true);
    try {
      setSystemSandbox(await sandboxAPI.systemSet(enabled, network));
      setEnvMessage(enabled ? '系统隔离已启用，新启动的引擎进程会带上隔离' : '系统隔离已关闭，新启动的引擎进程不再带隔离');
      setEnvError('');
    } catch (e) {
      setEnvError(errorTextOf(e, '系统隔离设置保存失败'));
    } finally {
      setSystemSaving(false);
    }
  };

  const handleClear = async () => {
    setClearing(true);
    try {
      const result = await sandboxAPI.envClear();
      setConfirmOpen(false);
      setEnvMessage(
        `已释放 ${formatBytes(result.freedBytes)}（${result.freedFiles} 个文件），对话记录与工作产物未受影响`,
      );
      if (result.failed.length > 0) {
        setEnvError(
          `${result.failed.length} 个条目未能删除：${errorTextOf(result.failed[0].error, '工作环境文件清理失败')}`,
        );
      } else {
        setEnvError('');
      }
      await loadUsage();
    } catch (e) {
      setEnvError(errorTextOf(e, '工作环境文件清理失败'));
    } finally {
      setClearing(false);
    }
  };

  const usedBytes = usage?.bytes ?? 0;
  // 只有「通道能读磁盘」且「确实有文件」时才允许清理：浏览器预览态不支持，
  // 按钮不能是个点了只弹假回执的装饰
  const canClear = Boolean(usage?.supported) && usedBytes > 0 && !clearing;

  const sysStatus = systemSandbox?.status;
  const sysEnabled = Boolean(systemSandbox?.config.enabled);
  const sysNetwork = Boolean(systemSandbox?.config.network);
  const systemSub = !sysStatus
    ? '正在读取系统隔离状态…'
    : !sysStatus.supported
      ? `${sysStatus.platform} 不支持操作系统级隔离，当前只有策略沙箱：工具调用受工作区边界与白名单约束，进程不受限制`
      : sysEnabled
        ? sysStatus.effective
          ? `已生效（AppContainer · ${sysStatus.profile}）：终端、MCP stdio 与 ACP 子进程以容器令牌运行，写操作限制在工作区，读不到数据根里的会话库与密钥文件${sysNetwork ? '；允许出网' : '；阻断出网（回环与局域网同样不可达）'}`
          : `已请求但未生效：${sysStatus.error ?? 'AppContainer profile 未就绪'}`
        : '未启用：子进程以当前用户权限运行，只有策略沙箱拦截越界工具调用。启用后需重新启动任务才会作用于新引擎进程';

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="安全工作环境" desc="两层隔离：策略沙箱按工作区限制可用工具与路径；系统隔离让真实子进程以 Windows AppContainer 令牌运行。开启前请注意：模型调用仍会把任务内容发送给所选模型服务商" />
      <Box
        sx={{
          display: 'flex',
          gap: 2,
          flexWrap: 'wrap',
          mb: 1.5,
          px: 2,
          py: 1.25,
          borderRadius: '10px',
          bgcolor: 'rgba(15,23,42,0.04)',
        }}
      >
        <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-2)' }}>
          策略沙箱：L0 只读 / L1 受限 / L2 可执行，按工作区策略限制工具与路径
        </Typography>
        <Typography sx={{ fontSize: '12px', color: sysEnabled && sysStatus?.effective ? '#0D9488' : 'var(--tt-text-3)' }}>
          系统隔离：{sysStatus?.supported ? (sysEnabled && sysStatus.effective ? '已生效' : sysEnabled ? '未生效' : '未启用') : '平台不支持'}
        </Typography>
      </Box>
      {(envError || envMessage) && (
        <Box
          sx={{
            bgcolor: envError ? 'rgba(239,68,68,0.08)' : 'rgba(20,184,166,0.08)',
            border: `1px solid ${envError ? 'rgba(239,68,68,0.3)' : 'rgba(20,184,166,0.3)'}`,
            borderRadius: '10px',
            px: 2,
            py: 1.25,
            mb: 1.5,
          }}
        >
          <Typography sx={{ fontSize: '12px', color: envError ? '#EF4444' : '#0D9488' }}>
            {envError || envMessage}
          </Typography>
        </Box>
      )}
      <SettingRow
        label="启用安全工作环境"
        sub="开启且未自选工作目录时，任务文件落在当前工作区的 environment 子目录，并由工作区沙箱策略限制可用工具；已选择工作目录时以所选目录为准。本机应用无法阻止模型出网"
        control={<Switch checked={enabled} onChange={(e) => updateAppSettings({ secureEnv: e.target.checked })} sx={tealSwitch} />}
      />
      <SettingRow
        label="系统隔离（Windows AppContainer）"
        sub={systemSub}
        control={
          <Switch
            checked={sysEnabled}
            disabled={!sysStatus?.supported || systemSaving}
            onChange={(e) => void applySystem(e.target.checked, sysNetwork)}
            sx={tealSwitch}
          />
        }
      />
      <SettingRow
        label="允许沙箱内命令出网"
        sub={
          sysEnabled
            ? sysNetwork
              ? '已允许：容器内的终端、MCP 与 ACP 进程可以访问外网；回环与局域网仍被 AppContainer 网络隔离阻断'
              : '已阻断：容器内进程无法访问外网。需要联网安装依赖或调用在线服务时临时开启'
            : '系统隔离未启用时此项无效'
        }
        control={
          <Switch
            checked={sysNetwork}
            disabled={!sysEnabled || systemSaving}
            onChange={(e) => void applySystem(true, e.target.checked)}
            sx={tealSwitch}
          />
        }
      />
      <SettingRow
        label="清理工作环境文件"
        sub={
          usage === null
            ? envError
              ? '未能统计工作环境文件占用，可点击下方按钮重试'
              : '正在统计工作环境文件占用…'
            : !usage.supported
              ? usage.note ?? '当前环境无法统计工作环境文件占用，请在桌面版查看'
              : `当前占用 ${formatBytes(usage.bytes)}（${usage.files} 个文件，${usage.workspaceCount} 个工作空间）。清理会删除已下载的环境文件，但保留对话记录和工作产物`
        }
        control={
          <Box
            onClick={() => canClear && setConfirmOpen(true)}
            sx={{
              px: 1.75,
              py: 0.5,
              borderRadius: '8px',
              border: '1px solid #FCA5A5',
              cursor: canClear ? 'pointer' : 'not-allowed',
              opacity: canClear ? 1 : 0.5,
            }}
          >
            <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#EF4444' }}>
              {clearing
                ? '清理中…'
                : usage && !usage.supported
                  ? '仅桌面版可用'
                  : usedBytes > 0
                    ? '清理释放空间'
                    : '暂无可清理文件'}
            </Typography>
          </Box>
        }
      />

      {/* 清理确认弹窗 */}
      <Dialog open={confirmOpen} onClose={() => !clearing && setConfirmOpen(false)} PaperProps={{ sx: { borderRadius: '14px', minWidth: 320 } }}>
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>清理工作环境文件</DialogTitle>
        <DialogContent>
          <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-2)' }}>
            将删除 {usage?.workspaceCount ?? 0} 个工作空间下 environment 目录内的 {usage?.files ?? 0} 个文件，
            释放约 {formatBytes(usedBytes)}。对话记录与工作产物不受影响，已跑过的任务需重新准备环境文件。
          </Typography>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setConfirmOpen(false)} disabled={clearing} sx={{ color: 'var(--tt-text-3)', fontSize: '13px' }}>取消</Button>
          <Button
            onClick={() => void handleClear()}
            disabled={clearing}
            variant="contained"
            startIcon={clearing ? <CircularProgress size={14} color="inherit" /> : undefined}
            sx={{ fontSize: '13px', bgcolor: '#EF4444', '&:hover': { bgcolor: '#DC2626' } }}
          >
            确认清理
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

// ── 分区：ACP 与终端 ──────────────────────────────────────────────

const ACP_PERMISSION_OPTIONS = ['拒绝所有权限请求', '自动放行'];
const ACP_PERMISSION_VALUES: Record<string, 'deny' | 'allow'> = {
  拒绝所有权限请求: 'deny',
  自动放行: 'allow',
};
const ACP_PERMISSION_LABELS: Record<string, string> = {
  deny: '拒绝所有权限请求',
  allow: '自动放行',
};

/**
 * 终端 AI+ 与 ACP 双向接入。
 *
 * - 终端开关：显式授权，开启需二次确认；Rust 侧写审计。
 * - ACP 委派（本应用 → 外部编码 Agent）：启动命令只来自这里，模型不能改。
 * - ACP 服务端（外部 IDE/终端 → 本应用）：给出真实启动命令，复制即可在外部使用。
 *
 * 浏览器/Mock 通道没有本机进程：能力视图 pipe=false，所有开关置灰并说明原因。
 */
const TerminalAcpPanel: React.FC = () => {
  const showNotification = useAppStore((s) => s.showNotification);
  const [caps, setCaps] = useState<TerminalCapabilities | null>(null);
  const [acp, setAcp] = useState<AcpConfigState | null>(null);
  const [status, setStatus] = useState<AcpServerStatus | null>(null);
  const [loadError, setLoadError] = useState('');
  const [loading, setLoading] = useState(true);
  const [savingTerminal, setSavingTerminal] = useState(false);
  const [savingAcp, setSavingAcp] = useState(false);
  const [acpError, setAcpError] = useState('');
  const [confirmEnableTerminal, setConfirmEnableTerminal] = useState(false);
  const [form, setForm] = useState({
    enabled: false,
    command: '',
    permission: 'deny' as 'deny' | 'allow',
    timeoutSeconds: '600',
  });

  const applyAcpToForm = (next: AcpConfigState) => {
    setForm({
      enabled: next.enabled,
      command: next.command,
      permission: next.permission === 'allow' ? 'allow' : 'deny',
      timeoutSeconds: String(next.timeoutSeconds),
    });
  };

  const reload = React.useCallback(async () => {
    const [capsRes, acpRes, statusRes] = await Promise.allSettled([
      getTerminalCapabilities(),
      getAcpConfig(),
      getAcpStatus(),
    ]);
    const errors: string[] = [];
    if (capsRes.status === 'fulfilled') setCaps(capsRes.value);
    else errors.push(errorTextOf(capsRes.reason, '终端能力读取失败'));
    if (acpRes.status === 'fulfilled') {
      setAcp(acpRes.value);
      applyAcpToForm(acpRes.value);
    } else {
      errors.push(errorTextOf(acpRes.reason, 'ACP 配置读取失败'));
    }
    if (statusRes.status === 'fulfilled') setStatus(statusRes.value);
    else errors.push(errorTextOf(statusRes.reason, 'ACP 服务端状态读取失败'));
    setLoadError(errors.join('；'));
    setLoading(false);
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const applyTerminal = async (enabled: boolean) => {
    setSavingTerminal(true);
    try {
      await setTerminalEnabled(enabled);
      await reload();
      showNotification(enabled ? '终端 AI+ 已开启' : '终端 AI+ 已关闭', 'success');
    } catch (err) {
      showNotification(errorTextOf(err, '终端开关保存失败'), 'error');
      await reload();
    } finally {
      setSavingTerminal(false);
    }
  };

  const saveAcp = async () => {
    setAcpError('');
    const timeout = Number(form.timeoutSeconds.trim());
    if (form.enabled && !form.command.trim()) {
      setAcpError('启用 ACP 委派前必须填写外部 Agent 启动命令');
      return;
    }
    if (!Number.isInteger(timeout) || timeout < 60 || timeout > 1800) {
      setAcpError('超时需为 60–1800 之间的整数秒');
      return;
    }
    setSavingAcp(true);
    try {
      await setAcpConfig({
        enabled: form.enabled,
        command: form.command.trim(),
        permission: form.permission,
        timeoutSeconds: timeout,
      });
      await reload();
      showNotification('ACP 委派配置已保存，下次任务生效', 'success');
    } catch (err) {
      setAcpError(errorTextOf(err, 'ACP 配置保存失败'));
    } finally {
      setSavingAcp(false);
    }
  };

  const copyServerCommand = async () => {
    if (!status?.command) return;
    try {
      await navigator.clipboard.writeText(status.command);
      showNotification('ACP 服务端命令已复制', 'success');
    } catch {
      showNotification('复制失败：当前环境不允许访问剪贴板', 'error');
    }
  };

  const supported = Boolean(caps?.pipe);

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle
        title="ACP 与终端"
        desc="终端 AI+ 的显式授权，以及 ACP 双向接入：把编码子任务交给外部 Agent，或让外部 IDE 使用本应用 Agent"
      />

      {loading && (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 2 }}>
          <CircularProgress size={16} />
          <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3)' }}>正在读取本机能力…</Typography>
        </Box>
      )}

      {loadError && (
        <Box sx={{ mb: 2, p: 1.25, borderRadius: '10px', bgcolor: 'rgba(217, 119, 6, 0.1)' }}>
          <Typography sx={{ fontSize: '12.5px', color: '#92400E' }}>{loadError}</Typography>
        </Box>
      )}

      <Typography sx={{ fontSize: '14px', fontWeight: 700, color: 'var(--tt-text-1)', mb: 1 }}>
        终端 AI+
      </Typography>
      <SettingRow
        label="启用终端 AI+"
        sub={
          !supported
            ? caps?.reason || '当前通道没有本机 shell 进程'
            : caps?.enabled
              ? '已开启：可以在「终端 AI+」页打开真实 shell；命令以你的系统权限执行'
              : '默认关闭；开启后才能创建 shell 会话与获取命令建议。AI 永不自行执行命令'
        }
        control={
          <Switch
            data-testid="terminal-enable-switch"
            checked={Boolean(caps?.enabled)}
            disabled={!supported || loading || savingTerminal}
            onChange={(e) => {
              if (e.target.checked) setConfirmEnableTerminal(true);
              else void applyTerminal(false);
            }}
            sx={tealSwitch}
          />
        }
      />
      {caps && (
        <Box sx={{ mb: 2, px: 0.25 }}>
          <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', lineHeight: 1.7 }}>
            平台 {caps.platform} · shell {caps.shell || '—'} · 管道模式{caps.pipe ? '可用' : '不可用'} ·
            会话上限 {caps.maxSessions} · 单次输入 {Math.floor(caps.inputLimit / 1024)}KB ·
            引擎{caps.engineRunning ? '已运行' : '未运行'}
          </Typography>
        </Box>
      )}
      {!supported && (
        <Box sx={{ mb: 2, p: 1.5, borderRadius: '12px', bgcolor: 'rgba(217, 119, 6, 0.08)', border: '1px solid rgba(217, 119, 6, 0.3)' }}>
          <Typography sx={{ fontSize: '12.5px', color: '#92400E', lineHeight: 1.7 }}>
            {caps?.reason || '终端 AI+ 只在桌面版可用；浏览器/Mock 通道没有本机进程'}。
            这里的开关不可修改，避免出现「能点、能保存、实际不生效」。
          </Typography>
        </Box>
      )}

      <Typography sx={{ fontSize: '14px', fontWeight: 700, color: 'var(--tt-text-1)', mt: 3, mb: 1 }}>
        ACP 委派（本应用 → 外部编码 Agent）
      </Typography>
      <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3)', mb: 1.5, lineHeight: 1.7 }}>
        配置后，主 Agent 多一个 <code>delegate_coding_task</code> 工具：把一段编码任务交给外部 ACP
        Agent（如 Codex、Claude Code 的 ACP 服务）执行。启动命令只来自这里的设置，模型只能给任务描述与
        工作子目录；文件读写被限制在该子目录内，越界请求会被拒绝。
      </Typography>

      <SettingRow
        label="启用 ACP 委派"
        sub={acp?.error || (form.enabled ? '已启用：下次任务注册委派工具' : '关闭时不注册任何委派工具')}
        control={
          <Switch
            checked={form.enabled}
            disabled={!supported || loading || savingAcp}
            onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))}
            sx={tealSwitch}
          />
        }
      />
      <SettingRow
        label="外部 Agent 启动命令"
        sub="单行命令，例如 codex acp 或 npx -y @zed-industries/claude-code-acp；从 PATH 解析"
        control={
          <TextField
            size="small"
            value={form.command}
            disabled={!supported || loading || savingAcp}
            onChange={(e) => setForm((f) => ({ ...f, command: e.target.value }))}
            placeholder="codex acp"
            sx={{ width: 320, '& input': { fontSize: '12.5px', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' } }}
            inputProps={{ 'aria-label': 'ACP 启动命令' }}
          />
        }
      />
      <SettingRow
        label="外部 Agent 权限请求"
        sub="deny=拒绝它的文件写入/命令等权限请求；allow=自动放行。默认拒绝"
        control={
          <PrefSelect
            value={ACP_PERMISSION_LABELS[form.permission] ?? ACP_PERMISSION_OPTIONS[0]}
            options={ACP_PERMISSION_OPTIONS}
            onChange={(v) => setForm((f) => ({ ...f, permission: ACP_PERMISSION_VALUES[v] ?? 'deny' }))}
            disabled={!supported || loading || savingAcp}
          />
        }
      />
      <SettingRow
        label="单次委派超时（秒）"
        sub="超时会结束外部 Agent 的整个进程树；范围 60–1800"
        control={
          <TextField
            size="small"
            type="number"
            value={form.timeoutSeconds}
            disabled={!supported || loading || savingAcp}
            onChange={(e) => setForm((f) => ({ ...f, timeoutSeconds: e.target.value }))}
            sx={{ width: 120, '& input': { fontSize: '12.5px' } }}
            inputProps={{ min: 60, max: 1800, 'aria-label': 'ACP 超时秒数' }}
          />
        }
      />
      {acpError && (
        <Typography sx={{ fontSize: '12.5px', color: '#B91C1C', mb: 1 }}>{acpError}</Typography>
      )}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 1 }}>
        <Button
          variant="contained"
          size="small"
          disabled={!supported || savingAcp || loading}
          onClick={() => void saveAcp()}
          sx={{ textTransform: 'none', bgcolor: '#0D9488', '&:hover': { bgcolor: '#0F766E' } }}
        >
          {savingAcp ? '正在保存…' : '保存 ACP 配置'}
        </Button>
        {acp?.auditPath && (
          <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', wordBreak: 'break-all' }}>
            委派审计：{acp.auditPath}
          </Typography>
        )}
      </Box>
      <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mb: 2, lineHeight: 1.7 }}>
        边界：配置在任务提交时下发给引擎，运行中的任务不切换；外部 Agent 拿不到本应用的终端令牌、
        API Key 等环境变量；审计只记任务长度、耗时与写文件次数，不记任务正文。
      </Typography>

      <Typography sx={{ fontSize: '14px', fontWeight: 700, color: 'var(--tt-text-1)', mt: 3, mb: 1 }}>
        ACP 服务端（外部 IDE/终端 → 本应用）
      </Typography>
      <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3)', mb: 1.5, lineHeight: 1.7 }}>
        把本应用的 Agent 以 ACP 协议暴露给外部 IDE 或终端：在外部客户端里配置下面的启动命令即可。
        协议适配层只负责会话与提示词转发，模型与执行仍由本机引擎负责；它不加载桌面端的意识、技能、
        专家与 MCP 运行时，因此外部看到的能力比桌面对话更少。
      </Typography>
      <Box
        sx={{
          p: 1.75,
          borderRadius: '14px',
          border: '1px solid var(--tt-border)',
          bgcolor: 'var(--tt-surface)',
          mb: 1.5,
        }}
      >
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1 }}>
          <Typography sx={{ fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-1)' }}>
            启动命令
          </Typography>
          <Button
            size="small"
            variant="outlined"
            startIcon={<ContentCopyIcon />}
            disabled={!status?.command}
            onClick={() => void copyServerCommand()}
            sx={{ textTransform: 'none' }}
          >
            复制
          </Button>
        </Box>
        <Box
          component="pre"
          sx={{
            m: 0,
            p: 1.25,
            borderRadius: '8px',
            bgcolor: '#0B1220',
            color: '#E5E7EB',
            fontSize: '12px',
            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-all',
          }}
        >
          {status?.command || '（当前无法生成启动命令）'}
        </Box>
        <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', mt: 1, lineHeight: 1.7 }}>
          {status?.reason ||
            (status?.engineRunning
              ? `引擎 ${status.engineUrl}。在外部客户端里用上面的命令启动 ACP 服务即可。`
              : '引擎未运行：需先让本应用启动引擎，外部客户端才能接活。')}
        </Typography>
        {status?.note && (
          <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.5, lineHeight: 1.65 }}>
            {status.note}
          </Typography>
        )}
      </Box>

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
          '终端 AI+ 的开关与 ACP 配置分开：关闭终端不影响 ACP 委派，反之亦然',
          'ACP 服务端只接受文本提示词；图片内容与 session/load 未实现，客户端会收到明确错误',
          '外部 Agent 的 fs 读写限制在委派指定目录；终端能力与未知方法一律拒绝',
          '开启终端或修改 ACP 配置都会写入安全审计；拒绝/超时也留痕',
        ].map((item) => (
          <Typography key={item} sx={{ fontSize: '12.5px', color: 'var(--tt-text-2)', mb: 0.75, lineHeight: 1.65 }}>
            · {item}
          </Typography>
        ))}
      </Box>

      <Dialog
        open={confirmEnableTerminal}
        onClose={() => setConfirmEnableTerminal(false)}
        maxWidth="xs"
        fullWidth
      >
        <DialogTitle sx={{ fontSize: '16px', fontWeight: 700 }}>开启终端 AI+？</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ fontSize: '13px', lineHeight: 1.8 }}>
            开启后，你可以（也只能由你主动操作）在本机打开真实 shell。命令以当前用户权限执行，
            应用不会判断命令是否安全；AI 只给建议，执行前需要你逐条确认。所有会话、输入与建议
            都会写入安全审计（不记命令正文）。
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmEnableTerminal(false)} sx={{ textTransform: 'none' }}>
            取消
          </Button>
          <Button
            variant="contained"
            color="warning"
            onClick={() => {
              setConfirmEnableTerminal(false);
              void applyTerminal(true);
            }}
            sx={{ textTransform: 'none' }}
          >
            确认开启
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

// ── 分区十一：实验特性 ─────────────────────────────────────────────

const LabsPanel: React.FC = () => {
  const [appSettings, updateAppSettings] = useAppSettings();
  const experiments = (appSettings.experiments as Record<string, boolean> | undefined) ?? {};
  const feats: Record<string, boolean> = {
    generativeUI: Boolean(experiments.generativeUI),
    popWindow: Boolean(experiments.popWindow),
    browserRelay: Boolean(experiments.browserRelay),
    // 默认折叠：未设置过也视为开启
    collapse: experiments.collapse !== false,
    filter: Boolean(experiments.filter),
  };

  const toggleFeat = (id: string, on: boolean) => {
    updateAppSettings({ experiments: { ...experiments, [id]: on } });
  };

  const defs: Array<{ id: string; name: string; desc: string; unavailable?: string }> = [
    {
      id: 'generativeUI',
      name: '生成式 UI',
      desc: '把对话中的 ```html 代码块渲染为交互式沙箱预览（脚本可运行，但访问不到本机数据）',
    },
    {
      id: 'popWindow',
      name: '弹出独立窗口',
      desc: '把当前任务弹出为独立窗口；窗口加载同一份本地数据，不是第二个应用进程',
      unavailable: isTauri() ? undefined : '独立窗口仅桌面版可用，浏览器预览不会打开新窗口',
    },
    {
      id: 'browserRelay',
      name: '浏览器上下文接力',
      desc: '让 AI 基于你正在浏览的网页继续工作',
      unavailable: '需安装配套浏览器扩展，当前版本未提供',
    },
    {
      id: 'collapse',
      name: '助手过程折叠',
      desc: '默认折叠助手的思考与工具执行过程；关闭后默认展开（仍可手动切换）',
    },
    {
      id: 'filter',
      name: '对话列表筛选',
      desc: '在任务区显示标题搜索与创建时间筛选（近 7 天 / 近 30 天）',
    },
  ];

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="实验特性" desc="抢先体验新功能，部分功能仍在打磨中" />
      {defs.map((d) => (
        <SettingRow
          key={d.id}
          label={d.unavailable ? `${d.name}（暂未提供）` : d.name}
          sub={d.unavailable ? `${d.desc}。${d.unavailable}` : d.desc}
          control={
            <Switch
              checked={feats[d.id]}
              disabled={Boolean(d.unavailable)}
              onChange={(e) => toggleFeat(d.id, e.target.checked)}
              sx={tealSwitch}
            />
          }
        />
      ))}
      <RepairHistoryRow />
    </Box>
  );
};

/**
 * 修复历史消息显示。
 * 原来是一个 cursor:pointer 但没有任何 onClick 的假按钮。现在真的做事：
 * 按会话正文库（chat_messages）把内存里的会话重建一遍 —— 只重排渲染态，
 * 不写库也不改内容；正在运行的会话与库里查不到的会话都保留并在结果里如实报数。
 */
const RepairHistoryRow: React.FC = () => {
  const [open, setOpen] = useState(false);
  const [running, setRunning] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');

  const run = async () => {
    setRunning(true);
    setError('');
    try {
      const r = await useWorkbenchStore.getState().repairConversationDisplay();
      setMessage(
        `已按正文库重建 ${r.rebuilt} 个会话（运行中的 ${r.keptRunning} 个未动，` +
          `${r.keptUnsaved} 个尚未落库的会话按内存态保留），当前共 ${r.total} 个`,
      );
    } catch (e) {
      setError(errorTextOf(e, '历史消息显示修复失败'));
    } finally {
      setRunning(false);
      setOpen(false);
    }
  };

  return (
    <>
      <SettingRow
        label="修复历史消息显示"
        sub={error || message || '如果历史对话出现消息缺失或重复，可按会话正文库重建显示（不修改对话内容）'}
        control={
          <Box
            onClick={() => !running && setOpen(true)}
            sx={{
              px: 1.75,
              py: 0.5,
              borderRadius: '8px',
              border: '1px solid var(--tt-border)',
              cursor: running ? 'not-allowed' : 'pointer',
              opacity: running ? 0.6 : 1,
            }}
          >
            <Typography sx={{ fontSize: '12px', fontWeight: 600, color: 'var(--tt-text-2)' }}>
              {running ? '重建中…' : '运行修复'}
            </Typography>
          </Box>
        }
      />
      <Dialog open={open} onClose={() => !running && setOpen(false)} PaperProps={{ sx: { borderRadius: '14px', minWidth: 320 } }}>
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>修复历史消息显示</DialogTitle>
        <DialogContent>
          <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-2)' }}>
            将按会话正文库重建对话列表的显示，不修改任何已保存的内容；正在运行的任务不会被打断。
          </Typography>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setOpen(false)} disabled={running} sx={{ color: 'var(--tt-text-3)', fontSize: '13px' }}>
            取消
          </Button>
          <Button
            onClick={() => void run()}
            disabled={running}
            variant="contained"
            startIcon={running ? <CircularProgress size={14} color="inherit" /> : undefined}
            sx={{ fontSize: '13px' }}
          >
            确认修复
          </Button>
        </DialogActions>
      </Dialog>
    </>
  );
};

// ── Settings 主页面 ───────────────────────────────────────────────

const Settings: React.FC = () => {
  const { t: tr } = useI18n();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();

  const initial = searchParams.get('s') ?? 'preferences';
  const [active, setActive] = useState(
    allSections.some((sec) => sec.id === initial) ? initial : 'preferences',
  );

  useEffect(() => {
    const s = searchParams.get('s');
    if (s && allSections.some((sec) => sec.id === s)) {
      setActive(s);
    }
  }, [searchParams]);

  const handleSectionClick = (id: string) => {
    if (id === 'archive') {
      navigate('/archive');
      return;
    }
    setActive(id);
    setSearchParams({ s: id }, { replace: true });
  };

  const renderContent = () => {
    switch (active) {
      case 'preferences':
        return <PreferencesPanel />;
      case 'profile':
        return <ProfilePanel />;
      case 'system':
        return <SystemPanel />;
      case 'voice':
        return <VoicePanel />;
      case 'snapshot':
        return <SnapshotPanel />;
      case 'shortcuts':
        return <ShortcutsPanel />;
      case 'awareness':
        return <AwarenessPanel />;
      case 'branding':
        return <BrandingPanel />;
      case 'update':
        return <UpdatePanel />;
      case 'workbench':
        return <WorkbenchModePanel />;
      case 'models':
        return <ModelSettings />;
      case 'browser-use':
        return <BrowserUsePanel embedded />;
      case 'terminal-ai':
        return <TerminalPanel embedded />;
      case 'computer-use':
        return <ComputerUsePanel embedded />;
      case 'remote-desktop':
        return <RemoteDesktopPanel />;
      case 'sandbox':
        return <SandboxPanel />;
      case 'terminal':
        return <TerminalAcpPanel />;
      case 'labs':
        return <LabsPanel />;
      default:
        return <PreferencesPanel />;
    }
  };

  return (
    <Box sx={{ height: 'calc(100vh - 104px)', display: 'flex', flexDirection: 'column' }}>
      {/* 标题栏 */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 2 }}>
        <Typography sx={{ fontSize: '20px', fontWeight: 700, color: 'var(--tt-text-1)', flexGrow: 1 }}>
          {tr('设置')}
        </Typography>
        <IconButton size="small" onClick={() => navigate('/')} sx={{ color: 'var(--tt-text-3)' }}>
          <CloseIcon sx={{ fontSize: 22 }} />
        </IconButton>
      </Box>

      {/* 主体：左导航（三分组） + 右内容 */}
      <Box sx={{ flex: 1, minHeight: 0, display: 'flex', gap: 3 }}>
        <Box sx={{ width: 210, flexShrink: 0, overflowY: 'auto' }}>
          {sectionGroups.map((g, gi) => (
            <Box key={g.group} sx={{ mt: gi === 0 ? 0 : 2 }}>
              <Typography sx={{ fontSize: '11px', fontWeight: 700, color: 'var(--tt-text-4)', px: 1.5, mb: 0.5, letterSpacing: '0.05em' }}>
                {tr(g.group)}
              </Typography>
              {g.sections.map((sec) => {
                const Icon = sec.icon;
                const isActive = active === sec.id;
                return (
                  <Box
                    key={sec.id}
                    onClick={() => handleSectionClick(sec.id)}
                    sx={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 1.25,
                      px: 1.5,
                      py: 0.9,
                      mb: 0.25,
                      borderRadius: '10px',
                      cursor: 'pointer',
                      background: isActive ? '#E6F7F4' : 'transparent',
                      transition: 'background 0.15s ease',
                      '&:hover': { background: isActive ? '#E6F7F4' : 'rgba(15, 23, 42, 0.04)' },
                    }}
                  >
                    <Icon sx={{ fontSize: 17, color: isActive ? '#0D9488' : '#6B7280' }} />
                    <Typography
                      sx={{
                        fontSize: '13px',
                        fontWeight: isActive ? 600 : 400,
                        color: isActive ? '#0D9488' : '#374151',
                      }}
                    >
                      {tr(sec.label)}
                    </Typography>
                  </Box>
                );
              })}
            </Box>
          ))}
        </Box>

        {/* 右侧内容 */}
        <Box sx={{ flex: 1, minWidth: 0, overflowY: 'auto', pr: 1 }}>{renderContent()}</Box>
      </Box>
    </Box>
  );
};

export default Settings;
