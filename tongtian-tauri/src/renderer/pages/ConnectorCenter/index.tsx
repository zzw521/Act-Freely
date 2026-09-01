/**
 * 通天办公 - 连接器中心 (对标 QwenWork 连接器)
 *
 * 设计:
 * - 页头说明（连接器默认全部关闭，授权后生效）
 * - Tab: 内置连接器 / 集成市场
 * - 连接器卡片: 图标+名称+说明+状态徽标+连接/断开
 * - 授权流: 「+」→ 权限说明弹窗 → 等待连接 → 已连接
 * - Mock 开关: 开发阶段市场数据为本地清单（遵循开发规范）
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
  Box,
  Typography,
  Tabs,
  Tab,
  Switch,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
} from '@mui/material';
import LanguageIcon from '@mui/icons-material/Language';
import DesktopWindowsIcon from '@mui/icons-material/DesktopWindows';
import HubIcon from '@mui/icons-material/Hub';
import StorefrontIcon from '@mui/icons-material/Storefront';
import { listConnectors, authorizeConnector, revokeConnector } from '../../lib/alignment-api';

// ── 连接器数据（开发阶段 Mock，联调阶段替换为远端市场 API） ──────────

type AuthStatus = 'none' | 'pending' | 'authorized';

interface ConnectorDef {
  id: string;
  name: string;
  desc: string;
  icon: React.ElementType;
  category: 'builtin' | 'market';
  permissions: string[];
}

const builtinConnectors: ConnectorDef[] = [
  {
    id: 'builtin_browser',
    name: '浏览器',
    desc: '控制 Chromium 内核浏览器进行网页操作、数据提取与表单自动填写',
    icon: LanguageIcon,
    category: 'builtin',
    permissions: ['读取浏览器标签页状态', '执行点击/输入/滚动操作', '提取页面结构化数据'],
  },
  {
    id: 'sys_apps',
    name: '系统应用',
    desc: '读写系统日历、邮件、备忘录与通讯录数据',
    icon: DesktopWindowsIcon,
    category: 'builtin',
    permissions: ['读取日历日程', '读取/发送邮件', '读写备忘录', '读取通讯录'],
  },
  {
    id: 'm365',
    name: 'Microsoft 365',
    desc: '读写 Outlook 邮件、日历、待办事项、联系人与 OneDrive 文件',
    icon: HubIcon,
    category: 'builtin',
    permissions: ['Outlook 邮件读写', '日历读写', '待办事项管理', 'OneDrive 文件访问'],
  },
];

const marketConnectors: ConnectorDef[] = [
  { id: 'dingtalk', name: '钉钉', desc: '连接钉钉中的工作信息与服务', icon: HubIcon, category: 'market', permissions: ['消息读取', '日程', '审批', '待办'] },
  { id: 'feishu', name: '飞书', desc: '连接飞书文档、日历与消息', icon: HubIcon, category: 'market', permissions: ['消息读取', '文档', '日历'] },
  { id: 'notion', name: 'Notion', desc: '读写 Notion 页面与数据库', icon: HubIcon, category: 'market', permissions: ['页面读写', '数据库查询'] },
  { id: 'slack', name: 'Slack', desc: '发送消息与读取频道内容', icon: HubIcon, category: 'market', permissions: ['消息发送', '频道读取'] },
  { id: 'figma', name: 'Figma', desc: '读取设计稿与评论', icon: HubIcon, category: 'market', permissions: ['设计稿读取'] },
  { id: 'google_calendar', name: 'Google 日历', desc: '读写 Google 日历日程', icon: HubIcon, category: 'market', permissions: ['日程读写'] },
];

// ── 状态徽标 ──────────────────────────────────────────────────────

const StatusBadge: React.FC<{ status: AuthStatus }> = ({ status }) => {
  const map: Record<AuthStatus, { label: string; color: string; bg: string }> = {
    none: { label: '未连接', color: '#6B7280', bg: '#F3F4F6' },
    pending: { label: '等待连接', color: '#D97706', bg: '#FEF3C7' },
    authorized: { label: '已连接', color: '#0D9488', bg: '#E6F7F4' },
  };
  const s = map[status];
  return (
    <Box sx={{ px: 1, py: 0.3, borderRadius: '6px', bgcolor: s.bg }}>
      <Typography sx={{ fontSize: '11px', fontWeight: 600, color: s.color }}>{s.label}</Typography>
    </Box>
  );
};

// ── 连接器卡片 ────────────────────────────────────────────────────

const ConnectorCard: React.FC<{
  def: ConnectorDef;
  status: AuthStatus;
  onConnect: () => void;
  onRevoke: () => void;
}> = ({ def, status, onConnect, onRevoke }) => {
  const Icon = def.icon;
  return (
    <Box
      sx={{
        display: 'flex',
        alignItems: 'flex-start',
        gap: 1.5,
        bgcolor: '#FFFFFF',
        border: '1px solid #E5E7EB',
        borderRadius: '14px',
        p: 2,
        mb: 1.5,
      }}
    >
      <Box
        sx={{
          width: 40,
          height: 40,
          borderRadius: '10px',
          bgcolor: '#F0FDFA',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          flexShrink: 0,
        }}
      >
        <Icon sx={{ fontSize: 20, color: '#0D9488' }} />
      </Box>
      <Box sx={{ flexGrow: 1, minWidth: 0 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <Typography sx={{ fontSize: '14px', fontWeight: 600, color: '#111827' }}>{def.name}</Typography>
          <StatusBadge status={status} />
        </Box>
        <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mt: 0.5 }}>{def.desc}</Typography>
      </Box>
      {status === 'authorized' ? (
        <Button size="small" sx={{ color: '#EF4444', fontSize: '12px' }} onClick={onRevoke}>
          断开
        </Button>
      ) : (
        <Button
          size="small"
          variant="contained"
          disabled={status === 'pending'}
          onClick={onConnect}
          sx={{
            bgcolor: '#111827',
            fontSize: '12px',
            px: 2,
            '&:hover': { bgcolor: '#1F2937' },
          }}
        >
          {status === 'pending' ? '等待连接…' : '+ 连接'}
        </Button>
      )}
    </Box>
  );
};

// ── 主页面 ────────────────────────────────────────────────────────

const ConnectorCenter: React.FC = () => {
  const [tab, setTab] = useState<'builtin' | 'market'>('builtin');
  const [statuses, setStatuses] = useState<Record<string, AuthStatus>>({});
  const [authTarget, setAuthTarget] = useState<ConnectorDef | null>(null);

  // 从持久层加载连接器授权状态（Electron 走 sqlite，浏览器回退 localStorage）
  useEffect(() => {
    listConnectors()
      .then((list) => {
        const map: Record<string, AuthStatus> = {};
        list.forEach((c) => {
          map[c.connectorId] = c.authStatus;
        });
        setStatuses(map);
      })
      .catch(() => setStatuses({}));
  }, []);

  const handleAuthorize = useCallback(() => {
    if (!authTarget) return;
    const id = authTarget.id;
    const category = authTarget.category;
    setStatuses((s) => ({ ...s, [id]: 'pending' }));
    setAuthTarget(null);
    // 持久化授权状态（真实 IPC）
    authorizeConnector(id, category)
      .then(() => setStatuses((s) => ({ ...s, [id]: 'authorized' })))
      .catch(() => setStatuses((s) => ({ ...s, [id]: 'none' })));
  }, [authTarget]);

  const defs = tab === 'builtin' ? builtinConnectors : marketConnectors;

  return (
    <Box sx={{ height: 'calc(100vh - 104px)', overflowY: 'auto' }}>
      <Typography sx={{ fontSize: '20px', fontWeight: 700, color: '#111827' }}>连接器</Typography>
      <Typography sx={{ fontSize: '13px', color: '#9CA3AF', mt: 0.5 }}>
        连接器是通天办公与外部系统之间的桥梁。所有连接器默认关闭，仅在你明确完成授权后才会获取相应访问权限。
      </Typography>

      <Tabs
        value={tab}
        onChange={(_, v) => setTab(v)}
        sx={{
          mt: 2,
          minHeight: 36,
          '& .MuiTab-root': { fontSize: '13px', minHeight: 36, py: 0.5 },
          '& .Mui-selected': { color: '#0D9488' },
          '& .MuiTabs-indicator': { backgroundColor: '#0D9488' },
        }}
      >
        <Tab value="builtin" label="内置连接器" icon={<DesktopWindowsIcon sx={{ fontSize: 16 }} />} iconPosition="start" />
        <Tab value="market" label="集成市场" icon={<StorefrontIcon sx={{ fontSize: 16 }} />} iconPosition="start" />
      </Tabs>

      <Box sx={{ mt: 2, maxWidth: 860 }}>
        {defs.map((def) => (
          <ConnectorCard
            key={def.id}
            def={def}
            status={statuses[def.id] ?? 'none'}
            onConnect={() => setAuthTarget(def)}
            onRevoke={() => {
              setStatuses((s) => ({ ...s, [def.id]: 'none' }));
              revokeConnector(def.id).catch(() => undefined);
            }}
          />
        ))}
      </Box>

      {/* 授权确认弹窗 */}
      <Dialog open={Boolean(authTarget)} onClose={() => setAuthTarget(null)} maxWidth="xs" fullWidth>
        <DialogTitle sx={{ fontSize: '16px', fontWeight: 700 }}>
          连接「{authTarget?.name}」
        </DialogTitle>
        <DialogContent>
          <Typography sx={{ fontSize: '13px', color: '#6B7280', mb: 1.5 }}>
            授权后，通天办公将获取以下权限：
          </Typography>
          {authTarget?.permissions.map((p) => (
            <Box key={p} sx={{ display: 'flex', alignItems: 'center', gap: 1, py: 0.5 }}>
              <Box sx={{ width: 6, height: 6, borderRadius: '50%', bgcolor: '#14B8A6' }} />
              <Typography sx={{ fontSize: '13px', color: '#374151' }}>{p}</Typography>
            </Box>
          ))}
          <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mt: 1.5 }}>
            你可以随时在此页面断开连接并撤销授权。
          </Typography>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setAuthTarget(null)} sx={{ color: '#6B7280', fontSize: '13px' }}>
            取消
          </Button>
          <Button
            variant="contained"
            onClick={handleAuthorize}
            sx={{ bgcolor: '#111827', fontSize: '13px', '&:hover': { bgcolor: '#1F2937' } }}
          >
            确认授权
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

export default ConnectorCenter;
