/**
 * 通天办公 - 设置页面 (千问对标整改版)
 *
 * 三分组导航（对齐 QwenWork 系统设置结构）:
 * - 通用: 偏好设置/个人资料/系统设置/语音输入/应用快照/快捷键/意识/更新应用/已归档
 * - 扩展与集成: 工作台/模型与用量
 * - 高级设置: 安全工作环境/实验特性
 * 支持 ?s=xxx 深度链接
 */

import React, { useState, useEffect } from 'react';
import { Box, Typography, IconButton, Switch, Slider } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import PaletteOutlinedIcon from '@mui/icons-material/PaletteOutlined';
import PersonOutlineIcon from '@mui/icons-material/PersonOutline';
import SettingsOutlinedIcon from '@mui/icons-material/SettingsOutlined';
import MicNoneOutlinedIcon from '@mui/icons-material/MicNoneOutlined';
import CameraAltOutlinedIcon from '@mui/icons-material/CameraAltOutlined';
import KeyboardOutlinedIcon from '@mui/icons-material/KeyboardOutlined';
import PsychologyOutlinedIcon from '@mui/icons-material/PsychologyOutlined';
import SystemUpdateAltOutlinedIcon from '@mui/icons-material/SystemUpdateAltOutlined';
import ArchiveOutlinedIcon from '@mui/icons-material/ArchiveOutlined';
import DashboardOutlinedIcon from '@mui/icons-material/DashboardOutlined';
import MemoryOutlinedIcon from '@mui/icons-material/MemoryOutlined';
import ShieldOutlinedIcon from '@mui/icons-material/ShieldOutlined';
import ScienceOutlinedIcon from '@mui/icons-material/ScienceOutlined';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { usePreferenceStore } from '../../stores/usePreferenceStore';
import ModelSettings from '../../components/ModelSettings';
import { getSettings, setSettings } from '../../lib/alignment-api';
import { useAppSettings, WORKBENCH_MODE_DEFS } from '../../lib/use-app-settings';
import type { AppSettingsData } from '../../../shared/contracts';

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
    group: '高级设置',
    sections: [
      { id: 'sandbox', label: '安全工作环境', icon: ShieldOutlinedIcon },
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

const SettingRow: React.FC<{ label: string; sub?: string; control: React.ReactNode }> = ({
  label,
  sub,
  control,
}) => (
  <Box
    sx={{
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      bgcolor: '#FFFFFF',
      border: '1px solid #E5E7EB',
      borderRadius: '14px',
      px: 2.5,
      py: 2,
      mb: 1.5,
    }}
  >
    <Box sx={{ minWidth: 0, pr: 2 }}>
      <Typography sx={{ fontSize: '14px', fontWeight: 500, color: '#111827' }}>{label}</Typography>
      {sub && (
        <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mt: 0.25 }}>{sub}</Typography>
      )}
    </Box>
    {control}
  </Box>
);

const PrefSelect: React.FC<{ value: string; options: string[]; onChange: (v: string) => void }> = ({
  value,
  options,
  onChange,
}) => (
  <Box
    component="select"
    value={value}
    onChange={(e: React.ChangeEvent<HTMLSelectElement>) => onChange(e.target.value)}
    sx={{
      appearance: 'none',
      minWidth: 120,
      px: 1.5,
      py: 0.85,
      pr: 4,
      borderRadius: '10px',
      border: '1px solid #E5E7EB',
      bgcolor: '#FFFFFF',
      fontSize: '13px',
      color: '#111827',
      cursor: 'pointer',
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
        {opt}
      </option>
    ))}
  </Box>
);

const PanelTitle: React.FC<{ title: string; desc: string }> = ({ title, desc }) => (
  <Box sx={{ mb: 3 }}>
    <Typography sx={{ fontSize: '18px', fontWeight: 700, color: '#111827' }}>{title}</Typography>
    <Typography sx={{ fontSize: '13px', color: '#9CA3AF', mt: 0.5 }}>{desc}</Typography>
  </Box>
);

/** 真实设置持久化 hook：已下沉到 lib/use-app-settings（设置页与输入框共用，保证口径一致） */

// ── 分区一：偏好设置（对齐 QwenWork 10 项） ─────────────────────────

const PreferencesPanel: React.FC = () => {
  const prefs = usePreferenceStore();
  const [appSettings, updateAppSettings] = useAppSettings();
  const language = (appSettings.language as string) ?? '中文';
  const uiStyle = (appSettings.uiStyle as string) ?? '默认';
  const chatFont = (appSettings.chatFont as string) ?? '无衬线';
  const fontSize = (appSettings.chatFontSize as number) ?? 14;
  const expandTools = Boolean(appSettings.expandToolCalls);
  const imToolProcess = Boolean(appSettings.imShowToolProcess);

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="偏好设置" desc="个性化界面外观与对话体验" />

      <SettingRow label="语言" sub="修改后立即切换" control={<PrefSelect value={language} options={['中文', 'English']} onChange={(v) => updateAppSettings({ language: v })} />} />
      <SettingRow label="主题亮暗" control={<PrefSelect value={prefs.theme} options={['亮色', '暗色', '跟随系统']} onChange={(v) => prefs.set({ theme: v })} />} />
      <SettingRow label="界面风格" control={<PrefSelect value={uiStyle} options={['默认', '玻璃', '经典', '羊皮纸']} onChange={(v) => updateAppSettings({ uiStyle: v })} />} />
      <SettingRow label="对话字体" control={<PrefSelect value={chatFont} options={['无衬线', '衬线']} onChange={(v) => updateAppSettings({ chatFont: v })} />} />

      <SettingRow
        label="对话字号"
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
            <Typography sx={{ fontSize: '12px', color: '#6B7280', width: 36 }}>{fontSize}px</Typography>
          </Box>
        }
      />

      <SettingRow label="对话宽度" control={<PrefSelect value={prefs.chatWidth} options={['标准', '宽', '全宽']} onChange={(v) => prefs.set({ chatWidth: v })} />} />
      <SettingRow label="预览方式" sub="如何预览生成的文件（图片、Markdown）" control={<PrefSelect value={prefs.previewMode} options={['应用内预览', '系统默认程序']} onChange={(v) => prefs.set({ previewMode: v })} />} />
      <SettingRow label="提示词建议" sub="AI 完成回复后自动生成下一步建议，需新开会话生效" control={<Switch checked={prefs.promptSuggestions} onChange={(e) => prefs.set({ promptSuggestions: e.target.checked })} sx={tealSwitch} />} />
      <SettingRow label="默认展开工具调用" sub="新建展示的工具块默认展开，可随时手动收起" control={<Switch checked={expandTools} onChange={(e) => updateAppSettings({ expandToolCalls: e.target.checked })} sx={tealSwitch} />} />
      <SettingRow label="在 IM 频道中展示工具调用执行过程" sub="关闭则 IM 中只显示最终回复" control={<Switch checked={imToolProcess} onChange={(e) => updateAppSettings({ imShowToolProcess: e.target.checked })} sx={tealSwitch} />} />
    </Box>
  );
};

// ── 分区二：个人资料 ───────────────────────────────────────────────

const ProfilePanel: React.FC = () => {
  const avatarColors = ['#3B82F6', '#14B8A6', '#F59E0B', '#EF4444', '#8B5CF6', '#EC4899', '#06B6D4', '#84CC16', '#F97316', '#6366F1', '#111827'];
  const [color, setColor] = useState('#3B82F6');

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="个人资料" desc="管理头像与账户信息" />

      <SettingRow
        label="头像"
        sub="自定义背景色与图标"
        control={
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5 }}>
            <Box sx={{ display: 'flex', gap: 0.5 }}>
              {avatarColors.slice(0, 6).map((c) => (
                <Box
                  key={c}
                  onClick={() => setColor(c)}
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
              <Typography sx={{ color: '#fff', fontSize: '15px', fontWeight: 600 }}>张</Typography>
            </Box>
          </Box>
        }
      />

      <SettingRow
        label="账户 UID"
        sub="仅供查看和复制"
        control={
          <Typography sx={{ fontSize: '13px', color: '#6B7280', fontFamily: 'Consolas, monospace' }}>
            tt-8f3a2c91d4
          </Typography>
        }
      />

      <SettingRow
        label="订阅"
        sub="个人免费版"
        control={
          <Box sx={{ px: 1.75, py: 0.5, borderRadius: '8px', bgcolor: '#111827', cursor: 'pointer' }}>
            <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#fff' }}>查看套餐与定价</Typography>
          </Box>
        }
      />

      <SettingRow
        label="退出登录"
        sub="在此设备退出登录，可随时重新登录"
        control={
          <Box sx={{ px: 1.75, py: 0.5, borderRadius: '8px', border: '1px solid #FCA5A5', cursor: 'pointer' }}>
            <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#EF4444' }}>退出登录</Typography>
          </Box>
        }
      />
    </Box>
  );
};

// ── 分区三：系统设置 ───────────────────────────────────────────────

const SystemPanel: React.FC = () => {
  const [appSettings, updateAppSettings] = useAppSettings();
  const autoStart = Boolean(appSettings.autoStart);
  const keepAwake = appSettings.keepAwake !== false;
  const notify = appSettings.desktopNotify !== false;
  const badge = appSettings.badgeUnread !== false;
  const sound = Boolean(appSettings.soundNotify);
  const privacyMode = Boolean(appSettings.privacyMode);
  const proxyMode = (appSettings.proxyMode as string) ?? '跟随系统';

  const permissions = [
    { name: '完全磁盘访问权限', purpose: '读写项目文件等操作', status: '已授权' },
    { name: '录屏与系统录音', purpose: '屏幕内容识别、反馈截图', status: '未授权' },
    { name: '辅助功能', purpose: '监听全局快捷键，快捷唤起', status: '已授权' },
    { name: '麦克风', purpose: '语音输入等功能', status: '未授权' },
    { name: '自动化', purpose: '与系统应用协作（提醒/日历/备忘录）', status: '无需授权' },
    { name: '通知', purpose: '任务完成、消息提醒', status: '已授权' },
    { name: '位置服务', purpose: '处理与地理位置相关的任务', status: '无需授权' },
  ];

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="系统设置" desc="开机自启动、保持唤醒、通知等系统级选项" />

      <SettingRow label="开机自启动" sub="登录电脑时自动启动通天办公（真实生效）" control={<Switch checked={autoStart} onChange={(e) => updateAppSettings({ autoStart: e.target.checked })} sx={tealSwitch} />} />
      <SettingRow label="保持系统唤醒" sub="防止电脑在 Agent 工作时自动进入睡眠（真实生效）" control={<Switch checked={keepAwake} onChange={(e) => updateAppSettings({ keepAwake: e.target.checked })} sx={tealSwitch} />} />
      <SettingRow label="桌面通知" sub="Agent 等待回复或完成工作时发送系统通知" control={<Switch checked={notify} onChange={(e) => updateAppSettings({ desktopNotify: e.target.checked })} sx={tealSwitch} />} />
      <SettingRow label="任务栏未读状态" sub="在任务栏展示任务未读红点与数量" control={<Switch checked={badge} onChange={(e) => updateAppSettings({ badgeUnread: e.target.checked })} sx={tealSwitch} />} />
      <SettingRow label="声音通知" sub="Agent 完成工作时播放提示音" control={<Switch checked={sound} onChange={(e) => updateAppSettings({ soundNotify: e.target.checked })} sx={tealSwitch} />} />
      <SettingRow label="网络代理" control={<PrefSelect value={proxyMode} options={['跟随系统', '自定义']} onChange={(v) => updateAppSettings({ proxyMode: v })} />} />

      <Typography sx={{ fontSize: '14px', fontWeight: 700, color: '#111827', mt: 3, mb: 1.5 }}>系统权限</Typography>
      {permissions.map((p) => (
        <SettingRow
          key={p.name}
          label={p.name}
          sub={p.purpose}
          control={
            p.status === '无需授权' ? (
              <Typography sx={{ fontSize: '12px', color: '#9CA3AF' }}>无需授权</Typography>
            ) : p.status === '已授权' ? (
              <Box sx={{ px: 1.25, py: 0.4, borderRadius: '6px', bgcolor: '#E6F7F4' }}>
                <Typography sx={{ fontSize: '11.5px', fontWeight: 600, color: '#0D9488' }}>已授权</Typography>
              </Box>
            ) : (
              <Box sx={{ px: 1.5, py: 0.45, borderRadius: '8px', bgcolor: '#111827', cursor: 'pointer' }}>
                <Typography sx={{ fontSize: '11.5px', fontWeight: 600, color: '#fff' }}>去授权</Typography>
              </Box>
            )
          }
        />
      ))}

      <Typography sx={{ fontSize: '14px', fontWeight: 700, color: '#111827', mt: 3, mb: 1.5 }}>数据共享</Typography>
      <SettingRow
        label="隐私模式"
        sub="你的数据不会被用于产品改进，仅用于提供基本服务"
        control={<Switch checked={privacyMode} onChange={(e) => updateAppSettings({ privacyMode: e.target.checked })} sx={tealSwitch} />}
      />
    </Box>
  );
};

// ── 分区四：语音输入 ───────────────────────────────────────────────

const VoicePanel: React.FC = () => {
  const [appSettings, updateAppSettings] = useAppSettings();
  const enabled = Boolean(appSettings.voiceEnabled);
  const [mode, setMode] = useState('组合键');

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="语音输入" desc="全局语音输入快捷键与转录设置" />
      <SettingRow label="启用语音输入" sub="按一次开始录音，再按一次停止并转录" control={<Switch checked={enabled} onChange={(e) => updateAppSettings({ voiceEnabled: e.target.checked })} sx={tealSwitch} />} />
      <SettingRow label="快捷键模式" sub="选择如何触发语音录制" control={<PrefSelect value={mode} options={['单按键', '组合键']} onChange={setMode} />} />
      <SettingRow
        label="触发按键"
        sub="所选按键仅在单独按下时触发，正常组合键操作不受影响"
        control={
          <Box sx={{ px: 1.5, py: 0.6, borderRadius: '8px', border: '1px dashed #D1D5DB', cursor: 'pointer' }}>
            <Typography sx={{ fontSize: '12px', color: '#6B7280' }}>Ctrl + Shift + Space（点击录制）</Typography>
          </Box>
        }
      />
    </Box>
  );
};

// ── 分区五：应用快照 ───────────────────────────────────────────────

const SnapshotPanel: React.FC = () => {
  const [appSettings, updateAppSettings] = useAppSettings();
  const enabled = Boolean(appSettings.snapshotEnabled);

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="应用快照" desc="捕获最前方应用的截图与可读文本，作为对话上下文" />
      <SettingRow label="启用应用快照" control={<Switch checked={enabled} onChange={(e) => updateAppSettings({ snapshotEnabled: e.target.checked })} sx={tealSwitch} />} />
      <SettingRow
        label="全局快捷键"
        control={
          <Box sx={{ px: 1.5, py: 0.6, borderRadius: '8px', border: '1px dashed #D1D5DB', cursor: 'pointer' }}>
            <Typography sx={{ fontSize: '12px', color: '#6B7280' }}>Alt + S（点击录制）</Typography>
          </Box>
        }
      />
      <Box sx={{ bgcolor: '#FFFBEB', border: '1px solid rgba(217,119,6,0.25)', borderRadius: '12px', p: 1.75 }}>
        <Typography sx={{ fontSize: '12px', color: '#92400E', lineHeight: 1.6 }}>
          隐私提示：快照内容将作为上下文上送模型，受数据分级策略约束——检测为机密级的内容不会上送云端模型。
        </Typography>
      </Box>
    </Box>
  );
};

// ── 分区六：快捷键 ─────────────────────────────────────────────────

const ShortcutsPanel: React.FC = () => {
  const [appSettings, updateAppSettings] = useAppSettings();
  const quickPickOn = Boolean(appSettings.quickPickEnabled);
  const [qpMode, setQpMode] = useState('组合键');

  const groups: { group: string; items: [string, string][] }[] = [
    {
      group: '通用',
      items: [
        ['打开设置', 'Ctrl + ,'],
        ['切换侧边栏', 'Ctrl + \\'],
        ['切换任务监控', 'Ctrl + /'],
      ],
    },
    {
      group: '任务',
      items: [
        ['快速切换任务', 'Ctrl + Tab'],
        ['创建新任务', 'Ctrl + N'],
        ['搜索全部任务', 'Ctrl + G'],
        ['在当前任务中搜索', 'Ctrl + F'],
      ],
    },
    {
      group: '会话',
      items: [
        ['发送消息', 'Enter 或 Ctrl + Enter'],
        ['插入换行', 'Shift + Enter'],
      ],
    },
  ];

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="快捷键" desc="查看和自定义键盘快捷方式，点击任意条目录制新组合" />

      {groups.map((g) => (
        <Box key={g.group} sx={{ mb: 2.5 }}>
          <Typography sx={{ fontSize: '13px', fontWeight: 700, color: '#6B7280', mb: 1 }}>{g.group}</Typography>
          {g.items.map(([action, combo]) => (
            <SettingRow
              key={action}
              label={action}
              control={
                <Box sx={{ px: 1.5, py: 0.5, borderRadius: '8px', border: '1px solid #E5E7EB', bgcolor: '#F9FAFB', cursor: 'pointer' }}>
                  <Typography sx={{ fontSize: '12px', color: '#374151', fontFamily: 'Consolas, monospace' }}>{combo}</Typography>
                </Box>
              }
            />
          ))}
        </Box>
      ))}

      <Typography sx={{ fontSize: '14px', fontWeight: 700, color: '#111827', mt: 3, mb: 1.5 }}>
        QuickPick 全局快速任务窗
      </Typography>
      <SettingRow label="启用 QuickPick" sub="无需切换主窗口即可随时提交任务" control={<Switch checked={quickPickOn} onChange={(e) => updateAppSettings({ quickPickEnabled: e.target.checked })} sx={tealSwitch} />} />
      <SettingRow label="触发模式" control={<PrefSelect value={qpMode} options={['组合键', '双击修饰键']} onChange={setQpMode} />} />
      <SettingRow
        label="触发按键"
        control={
          <Box sx={{ px: 1.5, py: 0.6, borderRadius: '8px', border: '1px dashed #D1D5DB', cursor: 'pointer' }}>
            <Typography sx={{ fontSize: '12px', color: '#6B7280' }}>Alt + Space（点击录制）</Typography>
          </Box>
        }
      />
    </Box>
  );
};

// ── 分区七：意识 ───────────────────────────────────────────────────

const AwarenessPanel: React.FC<{ onNavigate: (path: string) => void }> = ({ onNavigate }) => {
  const [appSettings, updateAppSettings] = useAppSettings();
  const [mode, setMode] = useState(true);
  const [autoMemory, setAutoMemory] = useState(true);

  // 意识开关从主进程 awareness 配置加载
  useEffect(() => {
    import('../../lib/alignment-api').then((api) =>
      api.getAwareness().then((p) => { setMode(p.mode); setAutoMemory(p.autoMemory); }).catch(() => undefined),
    );
  }, []);

  const toggleMode = (v: boolean) => {
    setMode(v);
    import('../../lib/alignment-api').then((api) => api.updateAwareness({ mode: v }).catch(() => undefined));
  };
  const toggleAutoMemory = (v: boolean) => {
    setAutoMemory(v);
    import('../../lib/alignment-api').then((api) => api.updateAwareness({ autoMemory: v }).catch(() => undefined));
  };
  // appSettings/updateAppSettings 预留（意识分区未使用应用级设置）
  void appSettings; void updateAppSettings;

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="意识" desc="跨会话记忆与协作风格个性化" />
      <SettingRow label="意识模式" sub="Agent 角色风格定制与跨会话技能进化" control={<Switch checked={mode} onChange={(e) => toggleMode(e.target.checked)} sx={tealSwitch} />} />
      <SettingRow label="自动记忆" sub="自动提取值得记住的事实并周期性产出反思" control={<Switch checked={autoMemory} onChange={(e) => toggleAutoMemory(e.target.checked)} sx={tealSwitch} />} />
      <Box
        onClick={() => onNavigate('/awareness')}
        sx={{
          mt: 2,
          p: 2.5,
          bgcolor: '#F0FDFA',
          border: '1px solid rgba(20,184,166,0.3)',
          borderRadius: '14px',
          cursor: 'pointer',
          '&:hover': { borderColor: '#14B8A6' },
        }}
      >
        <Typography sx={{ fontSize: '14px', fontWeight: 600, color: '#0D9488' }}>打开意识页 →</Typography>
        <Typography sx={{ fontSize: '12px', color: '#6B7280', mt: 0.5 }}>
          编辑协作风格、工作手册，浏览与管理记忆条目
        </Typography>
      </Box>
    </Box>
  );
};

// ── 分区八：更新应用 ───────────────────────────────────────────────

const UpdatePanel: React.FC = () => (
  <Box sx={{ maxWidth: 860 }}>
    <PanelTitle title="更新应用" desc="检查并获取通天办公新版本" />
    <SettingRow
      label="当前版本"
      sub="v0.1.0 (BETA)"
      control={
        <Box sx={{ px: 1.75, py: 0.5, borderRadius: '8px', bgcolor: '#111827', cursor: 'pointer' }}>
          <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#fff' }}>检查更新</Typography>
        </Box>
      }
    />
  </Box>
);

// ── 分区九：工作台 ─────────────────────────────────────────────────

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

      <Typography sx={{ fontSize: '14px', fontWeight: 700, color: '#111827', mt: 3, mb: 1.5 }}>幻灯片模板</Typography>
      {['商务蓝（内置）', '简约白（内置）', '深色科技（内置）'].map((t) => (
        <SettingRow key={t} label={t} control={<Typography sx={{ fontSize: '12px', color: '#9CA3AF' }}>内置</Typography>} />
      ))}
      <Box sx={{ border: '1px dashed #D1D5DB', borderRadius: '14px', p: 2, textAlign: 'center', cursor: 'pointer' }}>
        <Typography sx={{ fontSize: '13px', color: '#6B7280' }}>+ 导入自定义模板</Typography>
      </Box>
    </Box>
  );
};

// ── 分区十：安全工作环境 ───────────────────────────────────────────

const SandboxPanel: React.FC = () => {
  const [appSettings, updateAppSettings] = useAppSettings();
  const enabled = Boolean(appSettings.secureEnv);

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="安全工作环境" desc="在你的电脑上单独开辟一块独立空间来跑任务——执行更快、更稳定，所有文件和数据都不会离开你的设备" />
      <SettingRow
        label="启用安全工作环境"
        sub="任务在隔离环境中运行，不影响其他文件，也不触及真实系统"
        control={<Switch checked={enabled} onChange={(e) => updateAppSettings({ secureEnv: e.target.checked })} sx={tealSwitch} />}
      />
      <SettingRow
        label="清理工作环境文件"
        sub="当前占用 1.2 GB。清理会删除已下载的环境文件，但保留对话记录和工作产物"
        control={
          <Box sx={{ px: 1.75, py: 0.5, borderRadius: '8px', border: '1px solid #FCA5A5', cursor: 'pointer' }}>
            <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#EF4444' }}>清理释放空间</Typography>
          </Box>
        }
      />
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
    collapse: Boolean(experiments.collapse),
    filter: Boolean(experiments.filter),
  };

  const toggleFeat = (id: string, on: boolean) => {
    updateAppSettings({ experiments: { ...experiments, [id]: on } });
  };

  const defs = [
    { id: 'generativeUI', name: '生成式 UI', desc: '允许 AI 在聊天中渲染交互式 HTML 组件（图表、仪表盘、表单等），新开会话后生效' },
    { id: 'popWindow', name: '弹出独立窗口', desc: '将任务弹出为独立窗口运行，在侧栏右键点击任务即可弹出' },
    { id: 'browserRelay', name: '浏览器上下文接力', desc: '在输入框中选择已打开的浏览器标签页，让 AI 基于当前网页继续工作' },
    { id: 'collapse', name: '助手过程折叠', desc: '调整对话中思考、工具执行和过程说明的折叠力度' },
    { id: 'filter', name: '对话列表筛选', desc: '在侧边栏底部显示筛选按钮，支持按项目、活跃时间过滤对话列表' },
  ];

  return (
    <Box sx={{ maxWidth: 860 }}>
      <PanelTitle title="实验特性" desc="抢先体验新功能，部分功能仍在打磨中" />
      {defs.map((d) => (
        <SettingRow
          key={d.id}
          label={d.name}
          sub={d.desc}
          control={<Switch checked={feats[d.id]} onChange={(e) => toggleFeat(d.id, e.target.checked)} sx={tealSwitch} />}
        />
      ))}
      <SettingRow
        label="修复历史消息显示"
        sub="如果历史对话出现消息缺失或重复，可尝试修复显示（不修改对话内容）"
        control={
          <Box sx={{ px: 1.75, py: 0.5, borderRadius: '8px', border: '1px solid #E5E7EB', cursor: 'pointer' }}>
            <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#374151' }}>运行修复</Typography>
          </Box>
        }
      />
    </Box>
  );
};

// ── Settings 主页面 ───────────────────────────────────────────────

const Settings: React.FC = () => {
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
        return <AwarenessPanel onNavigate={navigate} />;
      case 'update':
        return <UpdatePanel />;
      case 'workbench':
        return <WorkbenchModePanel />;
      case 'models':
        return <ModelSettings />;
      case 'sandbox':
        return <SandboxPanel />;
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
        <Typography sx={{ fontSize: '20px', fontWeight: 700, color: '#111827', flexGrow: 1 }}>
          设置
        </Typography>
        <IconButton size="small" onClick={() => navigate('/')} sx={{ color: '#6B7280' }}>
          <CloseIcon sx={{ fontSize: 22 }} />
        </IconButton>
      </Box>

      {/* 主体：左导航（三分组） + 右内容 */}
      <Box sx={{ flex: 1, minHeight: 0, display: 'flex', gap: 3 }}>
        <Box sx={{ width: 210, flexShrink: 0, overflowY: 'auto' }}>
          {sectionGroups.map((g, gi) => (
            <Box key={g.group} sx={{ mt: gi === 0 ? 0 : 2 }}>
              <Typography sx={{ fontSize: '11px', fontWeight: 700, color: '#9CA3AF', px: 1.5, mb: 0.5, letterSpacing: '0.05em' }}>
                {g.group}
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
                      {sec.label}
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
