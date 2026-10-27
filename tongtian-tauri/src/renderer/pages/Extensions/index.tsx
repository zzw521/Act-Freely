/**
 * 通天办公 - 扩展页面（设计图对齐版）
 *
 * 智能体能力由两部分构成：技能、连接器
 * - 连接器：内置工具（随应用内置）+ MCP 连接器（mcp_* 通道：登记落 mcp_servers，
 *   「连接」由 Rust 发起真实 JSON-RPC 握手，失败会显示服务器返回的原因）
 * - 技能：运行目录 `{data_home}/skills/<id>/SKILL.md` 真实扫描（skill_list），
 *   支持本地安装 / 启停 / 卸载（卸载移入回收目录，不物理删除）；
 *   浏览器通道无落盘能力，写操作如实拒绝（SKILL_UNSUPPORTED）
 *
 * 标签页由 URL 查询参数驱动：/extensions?tab=skills|connectors
 */

import React, { useEffect, useState } from 'react';
import { Box, Button, Tab, Tabs, Tooltip, Typography, Snackbar, Alert, Menu, Dialog, DialogTitle, DialogContent, DialogActions, Select, MenuItem, TextField, IconButton, Switch } from '@mui/material';
import FolderOutlinedIcon from '@mui/icons-material/FolderOutlined';
import DescriptionOutlinedIcon from '@mui/icons-material/DescriptionOutlined';
import TerminalIcon from '@mui/icons-material/Terminal';
import SearchIcon from '@mui/icons-material/Search';
import InsightsIcon from '@mui/icons-material/Insights';
import PublicIcon from '@mui/icons-material/Public';
import LinkIcon from '@mui/icons-material/Link';
import ScheduleIcon from '@mui/icons-material/Schedule';
import AddIcon from '@mui/icons-material/Add';
import StarIcon from '@mui/icons-material/Star';
import ChatBubbleOutlineIcon from '@mui/icons-material/ChatBubbleOutline';
import UploadFileIcon from '@mui/icons-material/UploadFile';
import CloudUploadOutlinedIcon from '@mui/icons-material/CloudUploadOutlined';
import EditOutlinedIcon from '@mui/icons-material/EditOutlined';
import ContentPasteIcon from '@mui/icons-material/ContentPaste';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import { useSearchParams, useNavigate } from 'react-router-dom';
import type { BuiltinTool, BuiltinToolList, InstalledSkill, McpAddReq, McpServer, SkillRootInfo } from '../../../shared/contracts';
import { errorTextOf } from '../../../shared/error-messages';
import { mcpAPI, skillAPI, systemAPI } from '../../lib/api';
import { parseMcpJsonConfig, TRANSPORT_LABEL_TEXT } from '../../lib/mcp-local';
import { isTauri } from '../../lib/tauri-api';
import MarketBrowser from '../../components/MarketBrowser';

// ── 对话创建技能的预置提示词（create-skill 流程，落盘目录与 skill:list 扫描目录同源） ──

/** 用运行目录拼出真实落盘路径；通道未上报目录时如实描述位置 */
function skillCreatePrompt(skillsRoot: string | null): string {
  const target = skillsRoot
    ? `将技能保存到 ${skillsRoot}/（每个技能一个子目录，内含 SKILL.md）`
    : '将技能保存到任我行运行目录下的 skills/ 子目录（每个技能一个子目录，内含 SKILL.md）';
  return `帮我使用 create-skill 创建技能。请检测当前运行环境，${target}。首先问我技能应该做什么。`;
}

// ── 连接器（MCP 服务器）手动配置模型 ───────────────────

type McpServerType = 'STDIO' | 'SSE' | 'STREAMABLE_HTTP';

interface KvPair {
  key: string;
  value: string;
}

/** JSON 导入弹窗的占位示例 */
const JSON_IMPORT_PLACEHOLDER = `// You can use either format:
// STDIO example:
{
  "mcpServers": {
    "my-server": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem"]
    }
  }
}
// SSE / Streamable HTTP example:
{
  "mcpServers": {
    "my-server": {
      "url": "http://localhost:8080/sse"
    }
  }
}`;

type TabKey = 'skills' | 'connectors';

// ── 内置工具（清单来自引擎注册表 / 同源声明值，见 shared/builtin-tools.ts）──
// 这里只保留图标映射：原先写死的五张卡片里有三张对应的能力在引擎里根本没有注册，
// 属于假宣传，已删除（清单见 shared/builtin-tools.ts 与契约测试）。
const TOOL_ICONS: Record<string, typeof FolderOutlinedIcon> = {
  view_text_file: SearchIcon,
  write_text_file: DescriptionOutlinedIcon,
  insert_text_file: InsightsIcon,
};
const fallbackToolIcon = FolderOutlinedIcon;

// ── 连接器市场目录（仅作浏览入口：点「登记」打开手动配置弹窗，真实登记走 mcp_add_server） ──

const CONNECTORS = [
  { id: 'browser', name: '浏览器自动化', category: '系统级', desc: '连接 Puppeteer 浏览器，实现网页自动化、截图与数据抓取。', icon: PublicIcon },
  { id: 'fs', name: '本地文件系统', category: '系统级', desc: '允许 AI 在指定目录内安全地读写文件。', icon: FolderOutlinedIcon },
  { id: 'slack', name: 'Slack', category: 'IM', desc: '发送消息、管理频道，与 Slack 工作区交互。', icon: ScheduleIcon },
  { id: 'web', name: 'Web 请求', category: '数据', desc: '让 AI 可以发起 HTTP 请求并获取网页内容。', icon: LinkIcon },
  { id: 'pg', name: 'PostgreSQL', category: '数据', desc: '连接 PostgreSQL 数据库执行只读查询。', icon: InsightsIcon },
  { id: 'github', name: 'GitHub', category: '开发', desc: '读取仓库、Issue、PR，与 GitHub API 交互。', icon: TerminalIcon },
];

const ExtensionsPage: React.FC = () => {
  const navigate = useNavigate();
  // 浏览器直连通道没有系统凭据库，无法持有 OAuth 令牌（授权按钮如实禁用）
  const desktopChannel = isTauri();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = (searchParams.get('tab') as TabKey) || 'connectors';
  const [skills, setSkills] = useState<InstalledSkill[]>([]);
  const [skillsLoaded, setSkillsLoaded] = useState(false);
  const [skillsError, setSkillsError] = useState<string | null>(null);
  const [skillRoot, setSkillRoot] = useState<SkillRootInfo | null>(null);
  const [marketOpen, setMarketOpen] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  // MCP 连接器登记台账（mcp:listServers 的真实结果：桌面端读 SQLite，浏览器通道读本地台账）
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [mcpLoaded, setMcpLoaded] = useState(false);
  const [mcpError, setMcpError] = useState<string | null>(null);
  const [mcpBusyId, setMcpBusyId] = useState<string | null>(null);
  // 移除确认（只删除这一条连接器登记，不动任务/审计等存量数据）
  const [serverToRemove, setServerToRemove] = useState<McpServer | null>(null);
  // 支持 URL 指定连接器子页签（侧边栏专家套件入口 → sub=builtin）
  const [connectorTab, setConnectorTab] = useState<'builtin' | 'market' | 'installed'>(() => {
    const sub = searchParams.get('sub');
    return sub === 'market' || sub === 'installed' ? sub : 'builtin';
  });

  const refreshSkills = React.useCallback(async () => {
    try {
      const list = await skillAPI.list();
      setSkills(Array.isArray(list) ? list : []);
      setSkillsError(null);
    } catch (e) {
      // 如实反映读取失败，不再用示例数据掩盖空态
      setSkills([]);
      setSkillsError(errorTextOf(e, '技能列表读取失败'));
    } finally {
      setSkillsLoaded(true);
    }
  }, []);

  // 技能列表 + 运行目录（桌面端扫描 {data_home}/skills/*/SKILL.md，浏览器通道为空）
  useEffect(() => {
    void refreshSkills();
    skillAPI
      .root()
      .then(setSkillRoot)
      .catch(() => setSkillRoot(null));
  }, [refreshSkills]);

  /**
   * 内置工具清单：向通道要真实注册表（桌面端引擎在跑时是实测值，
   * 引擎直连读 GET /api/tools，演示态给与引擎同源的声明清单）。
   * 读失败就显示失败原因，不再退回一组写死的卡片。
   */
  const [builtin, setBuiltin] = useState<BuiltinToolList | null>(null);
  const [builtinError, setBuiltinError] = useState<string | null>(null);
  useEffect(() => {
    void (async () => {
      try {
        setBuiltin(await systemAPI.builtinTools());
        setBuiltinError(null);
      } catch (e) {
        setBuiltin(null);
        setBuiltinError(errorTextOf(e, '内置工具清单读取失败'));
      }
    })();
  }, []);
  const builtinCount = builtin?.count ?? 0;

  const refreshMcp = React.useCallback(async () => {
    try {
      const list = await mcpAPI.list();
      setMcpServers(Array.isArray(list) ? list : []);
      setMcpError(null);
    } catch (e) {
      // 如实反映读取失败，不用示例连接器掩盖空态
      setMcpServers([]);
      setMcpError(errorTextOf(e, '连接器列表读取失败'));
    } finally {
      setMcpLoaded(true);
    }
  }, []);

  // MCP 连接器登记台账（mcp_servers / 本地台账；无登记时页面渲染真实空态）
  useEffect(() => {
    void refreshMcp();
  }, [refreshMcp]);

  const switchTab = (next: TabKey) => {
    setSearchParams({ tab: next });
  };

  // ── 技能「+ 添加」：下拉菜单 + 安装弹窗状态 ──
  const [skillAddAnchor, setSkillAddAnchor] = useState<null | HTMLElement>(null);
  const [skillUploadOpen, setSkillUploadOpen] = useState(false);
  const [skillSourcePath, setSkillSourcePath] = useState<string | null>(null);
  const [skillInstalling, setSkillInstalling] = useState(false);
  // 卸载确认（破坏性操作：目录移入 skills_removed 回收目录，不物理删除）
  const [skillToRemove, setSkillToRemove] = useState<InstalledSkill | null>(null);
  const [skillBusyId, setSkillBusyId] = useState<string | null>(null);

  // 通过对话创建技能：跳转新任务对话窗口并预置 create-skill 提示词（落盘目录取真实运行目录）
  const handleCreateSkillByChat = () => {
    setSkillAddAnchor(null);
    navigate(`/?prompt=${encodeURIComponent(skillCreatePrompt(skillRoot?.skillsRoot ?? null))}`);
  };

  const openSkillUploadDialog = () => {
    setSkillAddAnchor(null);
    setSkillSourcePath(null);
    setSkillUploadOpen(true);
  };

  /** 桌面端通过原生对话框选择 SKILL.md 或含 SKILL.md 的技能目录 */
  const handlePickSkillSource = async (directory: boolean) => {
    if (!isTauri()) {
      setToast(errorTextOf('SKILL_UNSUPPORTED'));
      return;
    }
    try {
      const { open } = await import('@tauri-apps/plugin-dialog');
      const picked = await open({
        multiple: false,
        directory,
        title: directory ? '选择包含 SKILL.md 的技能目录' : '选择技能的 SKILL.md 文件',
        filters: directory ? undefined : [{ name: 'SKILL.md', extensions: ['md', 'markdown'] }],
      });
      if (typeof picked === 'string' && picked.trim()) setSkillSourcePath(picked);
    } catch (e) {
      setToast(errorTextOf(e, '文件选择失败'));
    }
  };

  /** 安装技能：本地 SKILL.md 复制到 skills/{id}/（已存在则拒绝覆盖） */
  const handleInstallSkill = async () => {
    if (!skillSourcePath || skillInstalling) return;
    setSkillInstalling(true);
    try {
      const s = await skillAPI.install({ source: 'local', localPath: skillSourcePath });
      setSkillUploadOpen(false);
      setSkillSourcePath(null);
      await refreshSkills();
      const warned = s.scan?.level === 'warn' ? s.scan.findings.length : 0;
      setToast(
        warned > 0
          ? `技能「${s.name}」安装完成，安全扫描有 ${warned} 项提醒，请在技能列表查看`
          : `技能「${s.name}」安装完成`,
      );
    } catch (e) {
      setToast(errorTextOf(e, '技能安装失败'));
    } finally {
      setSkillInstalling(false);
    }
  };

  /** 启用/禁用技能：只改状态位，技能文件保持不动 */
  const handleToggleSkill = async (s: InstalledSkill, enabled: boolean) => {
    setSkillBusyId(s.id);
    try {
      await skillAPI.toggle(s.id, enabled);
      setSkills((prev) => prev.map((item) => (item.id === s.id ? { ...item, enabled } : item)));
      setToast(enabled ? `技能「${s.name}」已启用` : `技能「${s.name}」已禁用`);
    } catch (e) {
      setToast(errorTextOf(e, '技能状态更新失败'));
    } finally {
      setSkillBusyId(null);
    }
  };

  /** 确认卸载：目录移出运行目录进入回收目录 */
  const handleConfirmUninstall = async () => {
    const target = skillToRemove;
    if (!target || skillBusyId === target.id) return;
    setSkillBusyId(target.id);
    try {
      await skillAPI.uninstall(target.id);
      setSkills((prev) => prev.filter((item) => item.id !== target.id));
      setSkillToRemove(null);
      setToast(`技能「${target.name}」已卸载（目录移入回收目录，可人工找回）`);
    } catch (e) {
      setToast(errorTextOf(e, '技能卸载失败'));
    } finally {
      setSkillBusyId(null);
    }
  };

  // ── 连接器「+ 添加」：手动配置 / JSON 导入 ──
  const [connAddAnchor, setConnAddAnchor] = useState<null | HTMLElement>(null);
  const [mcpOpen, setMcpOpen] = useState(false);
  const [mcpType, setMcpType] = useState<McpServerType>('STDIO');
  const [mcpName, setMcpName] = useState('my-mcp-server');
  const [mcpCommand, setMcpCommand] = useState('npx -y @modelcontextprotocol/server-filesystem');
  const [mcpUrl, setMcpUrl] = useState('http://localhost:8080/sse');
  // 默认留空行：占位符只做提示，避免把示例密钥当成真实配置登记
  const [envVars, setEnvVars] = useState<KvPair[]>([{ key: '', value: '' }]);
  const [headers, setHeaders] = useState<KvPair[]>([{ key: '', value: '' }]);
  const [mcpTimeout, setMcpTimeout] = useState('60');
  // OAuth（仅 Streamable HTTP）：公共客户端 + PKCE，令牌由 Rust 存系统凭据库
  const [mcpAuthEnabled, setMcpAuthEnabled] = useState(false);
  const [mcpAuthUrl, setMcpAuthUrl] = useState('');
  const [mcpTokenUrl, setMcpTokenUrl] = useState('');
  const [mcpClientId, setMcpClientId] = useState('');
  const [mcpScopes, setMcpScopes] = useState('');
  const [jsonOpen, setJsonOpen] = useState(false);
  const [jsonText, setJsonText] = useState('');
  const [mcpSaving, setMcpSaving] = useState(false);

  /** 打开手动配置弹窗（市场卡片入口会预填连接器名称） */
  const openMcpDialog = (name?: string) => {
    setConnAddAnchor(null);
    if (name) setMcpName(name);
    setMcpOpen(true);
  };

  // 手动配置必填校验：名称 + （STDIO 命令 / 远程地址）+（启用 OAuth 时三项授权参数）
  const oauthDraft = mcpType === 'STREAMABLE_HTTP' && mcpAuthEnabled;
  // 显式 Authorization 会覆盖 OAuth 自动附加的令牌，登记前拦下
  const oauthHeaderConflict =
    !!oauthDraft &&
    headers.some((h) => h.key.trim().toLowerCase() === 'authorization' && h.value.trim().length > 0);
  const oauthDraftValid =
    !oauthDraft ||
    (mcpAuthUrl.trim().length > 0 &&
      mcpTokenUrl.trim().length > 0 &&
      mcpClientId.trim().length > 0 &&
      !oauthHeaderConflict);
  const mcpValid =
    mcpName.trim().length > 0 &&
    (mcpType === 'STDIO' ? mcpCommand.trim().length > 0 : mcpUrl.trim().length > 0) &&
    oauthDraftValid;

  /** 环境变量/请求头键值对 → 映射（空键名丢弃；值只交给后端，不回显） */
  const kvPairsToMap = (pairs: KvPair[]): Record<string, string> => {
    const out: Record<string, string> = {};
    pairs.forEach((p) => {
      const k = p.key.trim();
      if (k) out[k] = p.value;
    });
    return out;
  };

  const mcpReqFromDialog = (): McpAddReq => ({
    name: mcpName.trim(),
    transport: mcpType === 'STDIO' ? 'stdio' : mcpType === 'SSE' ? 'sse' : 'streamable_http',
    command: mcpCommand.trim(),
    url: mcpUrl.trim(),
    // stdio 用环境变量、远程用请求头；另一侧不会被消费，不写入免得卡片显示假凭据项
    env: mcpType === 'STDIO' ? kvPairsToMap(envVars) : {},
    headers: mcpType === 'STDIO' ? {} : kvPairsToMap(headers),
    timeoutSecs: Number(mcpTimeout) || undefined,
    ...(oauthDraft
      ? {
          auth: {
            authorizationUrl: mcpAuthUrl.trim(),
            tokenUrl: mcpTokenUrl.trim(),
            clientId: mcpClientId.trim(),
            scopes: mcpScopes
              .split(/[\s,;]+/)
              .map((s) => s.trim())
              .filter(Boolean),
          },
        }
      : {}),
  });

  // 提交手动配置：登记落 mcp_servers（重名/校验失败由后端拒绝并显示原因）
  const handleAddMcp = async () => {
    if (!mcpValid || mcpSaving) return;
    setMcpSaving(true);
    try {
      const row = await mcpAPI.add(mcpReqFromDialog());
      setMcpOpen(false);
      await refreshMcp();
      setConnectorTab('installed');
      setToast(`连接器「${row.name}」已登记，点击「连接」完成握手`);
    } catch (e) {
      setToast(errorTextOf(e, '连接器登记失败'));
    } finally {
      setMcpSaving(false);
    }
  };

  // JSON 导入：解析社区配置并逐个登记（只解析结构，不执行命令也不联网）
  const handleImportJson = async () => {
    const text = jsonText.trim();
    if (!text || mcpSaving) return;
    let reqs: McpAddReq[];
    try {
      reqs = parseMcpJsonConfig(text);
    } catch (e) {
      setToast(errorTextOf(e, 'JSON 格式无效，请检查后重试'));
      return;
    }
    setMcpSaving(true);
    const failed: string[] = [];
    let added = 0;
    for (const req of reqs) {
      try {
        await mcpAPI.add(req);
        added += 1;
      } catch (e) {
        failed.push(`${req.name}：${errorTextOf(e, '登记失败')}`);
      }
    }
    setMcpSaving(false);
    if (added > 0) {
      setJsonOpen(false);
      setJsonText('');
      await refreshMcp();
      setConnectorTab('installed');
    }
    setToast(
      failed.length
        ? `${added} 个连接器已登记，${failed.length} 个被拒绝 —— ${failed[0]}`
        : `${added} 个连接器已登记，点击「连接」完成握手`,
    );
  };

  /** 连接：后端按配置真实握手，失败时把服务器返回的原因留在卡片上 */
  const handleConnectMcp = async (s: McpServer) => {
    setMcpBusyId(s.serverId);
    try {
      const row = await mcpAPI.connect(s.serverId);
      setMcpServers((prev) => prev.map((item) => (item.serverId === row.serverId ? row : item)));
      setToast(`连接器「${row.name}」已连接（${row.tools.length} 个工具）`);
    } catch (e) {
      setToast(errorTextOf(e, '连接器握手失败'));
      await refreshMcp();
    } finally {
      setMcpBusyId(null);
    }
  };

  /** 断开：清除已验证状态与工具缓存，配置保留可随时再连 */
  const handleDisconnectMcp = async (s: McpServer) => {
    setMcpBusyId(s.serverId);
    try {
      const row = await mcpAPI.disconnect(s.serverId);
      setMcpServers((prev) => prev.map((item) => (item.serverId === row.serverId ? row : item)));
      setToast(`连接器「${row.name}」已断开，配置保留`);
    } catch (e) {
      setToast(errorTextOf(e, '连接器断开失败'));
    } finally {
      setMcpBusyId(null);
    }
  };

  /** OAuth 授权：打开系统浏览器走公共客户端 + PKCE，令牌由 Rust 存系统凭据库 */
  const handleAuthorizeMcp = async (s: McpServer) => {
    setMcpBusyId(s.serverId);
    try {
      const row = await mcpAPI.oauthAuthorize(s.serverId);
      setMcpServers((prev) => prev.map((item) => (item.serverId === row.serverId ? row : item)));
      setToast(`连接器「${row.name}」已授权，点击「连接」完成握手`);
    } catch (e) {
      setToast(errorTextOf(e, 'OAuth 授权未完成'));
      await refreshMcp();
    } finally {
      setMcpBusyId(null);
    }
  };

  /** 撤销授权：清除系统凭据库里的令牌并断开连接，配置本身保留 */
  const handleRevokeMcp = async (s: McpServer) => {
    setMcpBusyId(s.serverId);
    try {
      const row = await mcpAPI.oauthRevoke(s.serverId);
      setMcpServers((prev) => prev.map((item) => (item.serverId === row.serverId ? row : item)));
      setToast(`连接器「${row.name}」的授权已撤销，本地令牌已清除`);
    } catch (e) {
      setToast(errorTextOf(e, '撤销授权失败'));
    } finally {
      setMcpBusyId(null);
    }
  };

  /** 确认移除：只删这一条登记（不影响任务与审计存量数据） */
  const handleConfirmRemoveMcp = async () => {
    const target = serverToRemove;
    if (!target) return;
    setMcpBusyId(target.serverId);
    try {
      await mcpAPI.remove(target.serverId);
      setServerToRemove(null);
      await refreshMcp();
      setToast(`连接器「${target.name}」的登记已移除`);
    } catch (e) {
      setToast(errorTextOf(e, '连接器移除失败'));
    } finally {
      setMcpBusyId(null);
    }
  };

  const installedCount = mcpServers.length;

  return (
    <Box sx={{ maxWidth: 1080, mx: 'auto' }}>
      {/* 页头 */}
      <Typography variant="h5" sx={{ fontWeight: 700 }}>扩展</Typography>
      <Typography sx={{ fontSize: '13px', color: '#6B7280', mt: 0.5, mb: 2 }}>
        智能体能力由两部分构成：技能、连接器（含内置工具与 MCP）
      </Typography>

      {/* 两标签 */}
      <Tabs
        value={tab}
        onChange={(_, v) => switchTab(v as TabKey)}
        sx={{
          borderBottom: '1px solid #EEF0F2',
          mb: 3,
          '& .MuiTab-root': { textTransform: 'none', fontSize: '14px', fontWeight: 600 },
        }}
      >
        <Tab value="skills" label={`技能 (${skills.length})`} />
        <Tab value="connectors" label={`连接器 (${builtinCount + installedCount})`} />
      </Tabs>

      {/* ── 技能 ── */}
      {tab === 'skills' && (
        <Box>
          <Alert severity="info" sx={{ mb: 2, fontSize: '12.5px' }}>
            技能从本机运行目录扫描；可上传本地技能、通过对话创建，或在配置索引源后从在线市场安装。
            市场下载会校验 SHA-256，来源与版本记入审计日志。
          </Alert>
          <Box sx={{ display: 'flex', justifyContent: 'flex-end', gap: 1, mb: 2 }}>
            <Button
              variant="outlined"
              startIcon={<PublicIcon />}
              onClick={() => setMarketOpen(true)}
              sx={{
                borderRadius: '10px',
                textTransform: 'none',
                fontSize: '13px',
                fontWeight: 600,
                px: 2,
                borderColor: '#E5E7EB',
                color: '#374151',
                '&:hover': { borderColor: '#4F46E5', color: '#4F46E5', bgcolor: 'rgba(79, 70, 229, 0.04)' },
              }}
            >
              在线市场
            </Button>
            {/* 「+ 添加」：对话创建 / 上传技能 */}
            <Button
              variant="contained"
              startIcon={<AddIcon />}
              onClick={(e) => setSkillAddAnchor(e.currentTarget)}
              sx={{
                bgcolor: '#111827',
                borderRadius: '10px',
                textTransform: 'none',
                fontSize: '13px',
                fontWeight: 600,
                px: 2,
                '&:hover': { bgcolor: '#4F46E5' },
              }}
            >
              添加
            </Button>
          </Box>

          {/* 添加方式下拉：对话创建 / 上传技能 */}
          <Menu
            anchorEl={skillAddAnchor}
            open={Boolean(skillAddAnchor)}
            onClose={() => setSkillAddAnchor(null)}
            anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
            transformOrigin={{ vertical: 'top', horizontal: 'right' }}
            PaperProps={{
              sx: {
                width: 260,
                mt: 0.75,
                borderRadius: '14px',
                border: '1px solid rgba(99, 102, 241, 0.10)',
                boxShadow: '0 12px 40px rgba(30, 41, 59, 0.14)',
                p: 0.75,
              },
            }}
          >
            <Box
              onClick={handleCreateSkillByChat}
              sx={{
                display: 'flex',
                gap: 1.25,
                px: 1.25,
                py: 1.25,
                borderRadius: '10px',
                cursor: 'pointer',
                transition: 'background 0.15s ease',
                '&:hover': { background: 'rgba(99, 102, 241, 0.06)' },
              }}
            >
              <Box
                sx={{
                  width: 30,
                  height: 30,
                  borderRadius: '9px',
                  background: '#E8F1FF',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  flexShrink: 0,
                }}
              >
                <ChatBubbleOutlineIcon sx={{ fontSize: 16, color: '#3B82F6' }} />
              </Box>
              <Box sx={{ minWidth: 0 }}>
                <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#1F2937' }}>通过对话创建</Typography>
                <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF', mt: 0.25 }}>
                  创建个性化技能，将保存在本地使用
                </Typography>
              </Box>
            </Box>
            <Box
              onClick={openSkillUploadDialog}
              sx={{
                display: 'flex',
                gap: 1.25,
                px: 1.25,
                py: 1.25,
                borderRadius: '10px',
                cursor: 'pointer',
                transition: 'background 0.15s ease',
                '&:hover': { background: 'rgba(99, 102, 241, 0.06)' },
              }}
            >
              <Box
                sx={{
                  width: 30,
                  height: 30,
                  borderRadius: '9px',
                  background: '#F1EBFF',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  flexShrink: 0,
                }}
              >
                <UploadFileIcon sx={{ fontSize: 16, color: '#8B5CF6' }} />
              </Box>
              <Box sx={{ minWidth: 0 }}>
                <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#1F2937' }}>上传技能</Typography>
                <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF', mt: 0.25 }}>
                  从本地 SKILL.md 安装，仅落地到运行目录
                </Typography>
              </Box>
            </Box>
          </Menu>

          {/* 安装技能弹窗（本地 SKILL.md 增量安装，桌面端原生对话框选择） */}
          <Dialog
            open={skillUploadOpen}
            onClose={() => setSkillUploadOpen(false)}
            PaperProps={{ sx: { width: 460, borderRadius: '16px', p: 1 } }}
          >
            <DialogTitle sx={{ fontSize: '16px', fontWeight: 700, pb: 1 }}>安装技能</DialogTitle>
            <DialogContent sx={{ pt: '8px !important' }}>
              {/* 来源选择区：点击唤起原生对话框 */}
              <Box
                onClick={() => void handlePickSkillSource(false)}
                sx={{
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                  gap: 1,
                  py: 3.5,
                  borderRadius: '12px',
                  border: '1.5px dashed #D1D5DB',
                  background: '#FAFBFC',
                  cursor: 'pointer',
                  transition: 'border-color 0.15s ease',
                  '&:hover': { borderColor: '#8B5CF6' },
                }}
              >
                <CloudUploadOutlinedIcon sx={{ fontSize: 32, color: skillSourcePath ? '#8B5CF6' : '#9CA3AF' }} />
                <Typography
                  sx={{
                    fontSize: '13px',
                    color: skillSourcePath ? '#4C1D95' : '#6B7280',
                    fontWeight: skillSourcePath ? 600 : 400,
                    px: 2,
                    textAlign: 'center',
                    wordBreak: 'break-all',
                  }}
                >
                  {skillSourcePath ?? '点击选择技能的 SKILL.md 文件'}
                </Typography>
                {isTauri() && (
                  <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF' }}>
                    也可安装技能目录：
                    <Box
                      component="span"
                      onClick={(e: React.MouseEvent) => {
                        e.stopPropagation();
                        void handlePickSkillSource(true);
                      }}
                      sx={{ color: '#6366F1', cursor: 'pointer', ml: 0.5 }}
                    >
                      选择包含 SKILL.md 的目录
                    </Box>
                  </Typography>
                )}
              </Box>

              {/* 文件要求说明（与 Rust 安装校验同口径，不夸大能力） */}
              <Box sx={{ mt: 2 }}>
                <Typography sx={{ fontSize: '12px', color: '#6B7280', lineHeight: 1.7 }}>
                  文件要求：
                </Typography>
                <Typography sx={{ fontSize: '12px', color: '#9CA3AF', lineHeight: 1.7 }}>
                  · 仅支持 SKILL.md 单文件，或内含 SKILL.md 的技能目录（暂不支持 .zip）
                </Typography>
                <Typography sx={{ fontSize: '12px', color: '#9CA3AF', lineHeight: 1.7 }}>
                  · 技能标识取自来源目录名，重名技能需先卸载（安装不会覆盖既有技能）
                </Typography>
                <Typography sx={{ fontSize: '12px', color: '#9CA3AF', lineHeight: 1.7 }}>
                  · 安装目录：{skillRoot?.skillsRoot ?? '（桌面版任我行运行目录下的 skills/）'}
                </Typography>
                {!isTauri() && (
                  <Typography sx={{ fontSize: '12px', color: '#B45309', lineHeight: 1.7, mt: 0.5 }}>
                    浏览器预览通道不支持技能增删，请在桌面版任我行中管理技能
                  </Typography>
                )}
              </Box>

              {/* 安装按钮：未选择来源时禁用 */}
              <Box sx={{ display: 'flex', justifyContent: 'flex-end', mt: 2.5, mb: 1 }}>
                <Button
                  variant="contained"
                  disabled={!skillSourcePath || skillInstalling}
                  onClick={() => void handleInstallSkill()}
                  sx={{
                    bgcolor: skillSourcePath ? '#111827' : '#E5E7EB',
                    color: skillSourcePath ? '#fff' : '#9CA3AF',
                    borderRadius: '10px',
                    textTransform: 'none',
                    fontSize: '13px',
                    fontWeight: 600,
                    px: 3,
                    '&:hover': { bgcolor: '#4F46E5' },
                    '&.Mui-disabled': { bgcolor: '#E5E7EB', color: '#9CA3AF' },
                  }}
                >
                  {skillInstalling ? '安装中…' : '安装'}
                </Button>
              </Box>
            </DialogContent>
          </Dialog>

          {/* 卸载确认弹窗（目录移入回收目录，可人工找回） */}
          <Dialog
            open={Boolean(skillToRemove)}
            onClose={() => setSkillToRemove(null)}
            PaperProps={{ sx: { width: 420, borderRadius: '16px', p: 1 } }}
          >
            <DialogTitle sx={{ fontSize: '16px', fontWeight: 700 }}>卸载技能</DialogTitle>
            <DialogContent sx={{ pt: '8px !important' }}>
              <Typography sx={{ fontSize: '13px', color: '#4B5563', lineHeight: 1.7 }}>
                确认卸载「{skillToRemove?.name}」？技能目录会被移出运行目录并放入回收目录
                {skillRoot?.trashRoot ? `（${skillRoot.trashRoot}）` : ''}，不会物理删除，可随时人工找回。
              </Typography>
            </DialogContent>
            <DialogActions sx={{ px: 3, pb: 2 }}>
              <Button onClick={() => setSkillToRemove(null)} sx={{ textTransform: 'none', fontSize: '13px' }}>
                取消
              </Button>
              <Button
                variant="contained"
                color="error"
                disabled={skillBusyId === skillToRemove?.id}
                onClick={() => void handleConfirmUninstall()}
                sx={{ textTransform: 'none', fontSize: '13px', borderRadius: '10px', px: 2.5 }}
              >
                {skillBusyId === skillToRemove?.id ? '卸载中…' : '确认卸载'}
              </Button>
            </DialogActions>
          </Dialog>

          {skillsError && (
            <Alert severity="error" sx={{ mb: 2, fontSize: '13px' }} onClose={() => setSkillsError(null)}>
              {skillsError}
            </Alert>
          )}

          {skillsLoaded && skills.length === 0 && !skillsError ? (
            <Box sx={{ p: 4, textAlign: 'center' }}>
              <Typography sx={{ fontSize: '13px', color: '#9CA3AF' }}>
                运行目录下暂无技能（{skillRoot?.skillsRoot ?? 'skills/'}）
              </Typography>
              <Typography sx={{ fontSize: '12px', color: '#B0B7C3', mt: 0.75 }}>
                点击右上角「添加」，通过对话创建或直接安装本地 SKILL.md
              </Typography>
            </Box>
          ) : (
            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
              {skills.map((s) => (
                <Box
                  key={s.id}
                  sx={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 2,
                    border: '1px solid #EEF0F2',
                    borderRadius: '12px',
                    p: 2,
                    background: '#FFFFFF',
                  }}
                >
                  <Box
                    sx={{
                      width: 40,
                      height: 40,
                      borderRadius: '10px',
                      background: 'linear-gradient(135deg, #34D399 0%, #06B6D4 100%)',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      flexShrink: 0,
                    }}
                  >
                    <StarIcon sx={{ fontSize: 20, color: '#fff' }} />
                  </Box>
                  <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                      <Typography sx={{ fontSize: '14px', fontWeight: 600, color: '#111827' }}>{s.name}</Typography>
                      {!s.enabled && (
                        <Box
                          sx={{
                            px: 1,
                            py: 0.2,
                            borderRadius: '6px',
                            background: '#F3F4F6',
                          }}
                        >
                          <Typography sx={{ fontSize: '11px', fontWeight: 600, color: '#6B7280' }}>已禁用</Typography>
                        </Box>
                      )}
                      {!s.readable && (
                        <Box sx={{ px: 1, py: 0.2, borderRadius: '6px', background: '#FEF3C7' }}>
                          <Tooltip title="SKILL.md 读取失败或超出大小上限，请卸载后重新安装">
                            <Typography sx={{ fontSize: '11px', fontWeight: 600, color: '#B45309' }}>
                              不可读
                            </Typography>
                          </Tooltip>
                        </Box>
                      )}
                      {s.scan && s.scan.level !== 'clean' && (
                        <Tooltip
                          title={
                            <Box sx={{ whiteSpace: 'pre-line', fontSize: '12px', lineHeight: 1.6 }}>
                              {s.scan.summary}
                              {s.scan.findings.length > 0
                                ? '\n' + s.scan.findings.map((f) => `· ${f.message}`).join('\n')
                                : ''}
                            </Box>
                          }
                        >
                          <Box
                            sx={{
                              px: 1,
                              py: 0.2,
                              borderRadius: '6px',
                              background: s.scan.level === 'block' ? '#FEE2E2' : '#FEF3C7',
                            }}
                          >
                            <Typography
                              sx={{
                                fontSize: '11px',
                                fontWeight: 600,
                                color: s.scan.level === 'block' ? '#B91C1C' : '#B45309',
                              }}
                            >
                              {s.scan.level === 'block' ? '安全风险' : `提醒 ${s.scan.findings.length}`}
                            </Typography>
                          </Box>
                        </Tooltip>
                      )}
                    </Box>
                    <Typography sx={{ fontSize: '12px', color: '#6B7280', mt: 0.25 }}>
                      {s.description ?? '（SKILL.md 未填写描述）'}
                    </Typography>
                    <Tooltip title={s.path}>
                      <Typography
                        sx={{
                          fontSize: '11px',
                          color: '#B0B7C3',
                          mt: 0.25,
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                          maxWidth: 520,
                        }}
                      >
                        {s.path}
                      </Typography>
                    </Tooltip>
                  </Box>
                  <Tooltip title={s.enabled ? '禁用后对话中不可调用，技能文件保持不动' : '启用后可在对话中调用'}>
                    <span>
                      <Switch
                        checked={s.enabled}
                        disabled={
                          !isTauri() ||
                          skillBusyId === s.id ||
                          (s.scan?.level === 'block' && !s.enabled)
                        }
                        onChange={(_, checked) => void handleToggleSkill(s, checked)}
                      />
                    </span>
                  </Tooltip>
                  <Tooltip title={isTauri() ? '卸载（移入回收目录）' : '浏览器通道不支持卸载'}>
                    <span>
                      <IconButton
                        size="small"
                        disabled={!isTauri() || skillBusyId === s.id}
                        onClick={() => setSkillToRemove(s)}
                      >
                        <DeleteOutlineIcon sx={{ fontSize: 18, color: '#9CA3AF' }} />
                      </IconButton>
                    </span>
                  </Tooltip>
                </Box>
              ))}
            </Box>
          )}

          {/* 在线技能市场：索引可配置，安装走 Rust 下载校验后落盘 */}
          <MarketBrowser
            open={marketOpen}
            kind="skill"
            onClose={() => setMarketOpen(false)}
            onInstalled={() => {
              void refreshSkills();
              setToast('技能已从在线市场安装到运行目录');
            }}
          />
        </Box>
      )}

      {/* ── 连接器 ── */}
      {tab === 'connectors' && (
        <Box>
          {/* 标题 + 右上角「+ 添加」 */}
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
            <Typography sx={{ fontSize: '16px', fontWeight: 600, color: '#111827', flexGrow: 1 }}>
              连接你的应用，释放生产力
            </Typography>
            <Button
              variant="contained"
              startIcon={<AddIcon />}
              onClick={(e) => setConnAddAnchor(e.currentTarget)}
              sx={{
                bgcolor: '#111827',
                borderRadius: '10px',
                textTransform: 'none',
                fontSize: '13px',
                fontWeight: 600,
                px: 2,
                flexShrink: 0,
                '&:hover': { bgcolor: '#4F46E5' },
              }}
            >
              添加
            </Button>
          </Box>

          {/* 添加方式下拉：手动填写配置 / 粘贴 JSON 配置 */}
          <Menu
            anchorEl={connAddAnchor}
            open={Boolean(connAddAnchor)}
            onClose={() => setConnAddAnchor(null)}
            anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
            transformOrigin={{ vertical: 'top', horizontal: 'right' }}
            PaperProps={{
              sx: {
                width: 220,
                mt: 0.75,
                borderRadius: '12px',
                border: '1px solid rgba(99, 102, 241, 0.10)',
                boxShadow: '0 12px 40px rgba(30, 41, 59, 0.14)',
                p: 0.75,
              },
            }}
          >
            {([
              {
                key: 'manual',
                label: '手动填写配置',
                icon: EditOutlinedIcon,
                iconBg: '#E8F1FF',
                iconColor: '#3B82F6',
                onClick: () => {
                  setConnAddAnchor(null);
                  setMcpOpen(true);
                },
              },
              {
                key: 'json',
                label: '粘贴 JSON 配置',
                icon: ContentPasteIcon,
                iconBg: '#F1EBFF',
                iconColor: '#8B5CF6',
                onClick: () => {
                  setConnAddAnchor(null);
                  setJsonText('');
                  setJsonOpen(true);
                },
              },
            ] as const).map((opt) => {
              const Icon = opt.icon;
              return (
                <Box
                  key={opt.key}
                  onClick={opt.onClick}
                  sx={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 1.25,
                    px: 1.25,
                    py: 1,
                    borderRadius: '10px',
                    cursor: 'pointer',
                    transition: 'background 0.15s ease',
                    '&:hover': { background: 'rgba(99, 102, 241, 0.06)' },
                  }}
                >
                  <Box
                    sx={{
                      width: 28,
                      height: 28,
                      borderRadius: '8px',
                      background: opt.iconBg,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      flexShrink: 0,
                    }}
                  >
                    <Icon sx={{ fontSize: 15, color: opt.iconColor }} />
                  </Box>
                  <Typography sx={{ fontSize: '13px', fontWeight: 500, color: '#1F2937' }}>{opt.label}</Typography>
                </Box>
              );
            })}
          </Menu>

          {/* 添加 MCP 服务器弹窗（手动配置） */}
          <Dialog
            open={mcpOpen}
            onClose={() => setMcpOpen(false)}
            PaperProps={{ sx: { width: 520, borderRadius: '16px', p: 1 } }}
          >
            <DialogTitle sx={{ pb: 0.5 }}>
              <Typography sx={{ fontSize: '16px', fontWeight: 700 }}>添加 MCP 服务器</Typography>
              <Typography sx={{ fontSize: '12.5px', color: '#9CA3AF', mt: 0.25 }}>
                手动配置新的 MCP 服务器连接
              </Typography>
            </DialogTitle>
            <DialogContent sx={{ pt: '12px !important' }}>
              {/* 服务器类型 */}
              <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>服务器类型</Typography>
              <Select
                value={mcpType}
                onChange={(e) => setMcpType(e.target.value as McpServerType)}
                fullWidth
                size="small"
                sx={{ mb: 2, fontSize: '13px', borderRadius: '10px' }}
              >
                <MenuItem value="STDIO">STDIO</MenuItem>
                <MenuItem value="SSE">SSE</MenuItem>
                <MenuItem value="STREAMABLE_HTTP">Streamable HTTP</MenuItem>
              </Select>

              {/* 服务器名称 */}
              <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>
                服务器名称 <Box component="span" sx={{ color: '#EF4444' }}>*</Box>
              </Typography>
              <TextField
                value={mcpName}
                onChange={(e) => setMcpName(e.target.value)}
                fullWidth
                size="small"
                sx={{ mb: 2, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
              />

              {mcpType === 'STDIO' ? (
                <>
                  {/* 命令（STDIO） */}
                  <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>
                    命令 <Box component="span" sx={{ color: '#EF4444' }}>*</Box>
                  </Typography>
                  <TextField
                    value={mcpCommand}
                    onChange={(e) => setMcpCommand(e.target.value)}
                    fullWidth
                    size="small"
                    multiline
                    minRows={3}
                    helperText="粘贴完整命令，如 npx -y @modelcontextprotocol/server-filesystem"
                    FormHelperTextProps={{ sx: { fontSize: '11.5px', color: '#9CA3AF', mx: 0 } }}
                    sx={{ mb: 2, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
                  />

                  {/* 环境变量（可选） */}
                  <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>环境变量（可选）</Typography>
                  {envVars.map((v, i) => (
                    <Box key={i} sx={{ display: 'flex', gap: 1, mb: 1 }}>
                      <TextField
                        value={v.key}
                        onChange={(e) => setEnvVars(envVars.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)))}
                        placeholder="API_KEY"
                        size="small"
                        sx={{ flex: 1, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
                      />
                      <TextField
                        value={v.value}
                        onChange={(e) => setEnvVars(envVars.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))}
                        placeholder="your-api-key"
                        size="small"
                        sx={{ flex: 1.2, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
                      />
                      <IconButton size="small" onClick={() => setEnvVars(envVars.filter((_, j) => j !== i))} sx={{ color: '#9CA3AF' }}>
                        <DeleteOutlineIcon sx={{ fontSize: 17 }} />
                      </IconButton>
                    </Box>
                  ))}
                  <Typography
                    onClick={() => setEnvVars([...envVars, { key: '', value: '' }])}
                    sx={{ fontSize: '12.5px', color: '#4F46E5', cursor: 'pointer', mb: 2, '&:hover': { textDecoration: 'underline' } }}
                  >
                    + 添加变量
                  </Typography>
                </>
              ) : (
                <>
                  {/* 服务器地址（SSE / Streamable HTTP） */}
                  <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>
                    服务器地址 <Box component="span" sx={{ color: '#EF4444' }}>*</Box>
                  </Typography>
                  <TextField
                    value={mcpUrl}
                    onChange={(e) => setMcpUrl(e.target.value)}
                    fullWidth
                    size="small"
                    placeholder={mcpType === 'SSE' ? 'http://localhost:8080/sse' : 'http://localhost:8080/mcp'}
                    sx={{ mb: 2, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
                  />

                  {/* Headers（可选） */}
                  <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>Headers（可选）</Typography>
                  {headers.map((h, i) => (
                    <Box key={i} sx={{ display: 'flex', gap: 1, mb: 1 }}>
                      <TextField
                        value={h.key}
                        onChange={(e) => setHeaders(headers.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)))}
                        placeholder="Authorization"
                        size="small"
                        sx={{ flex: 1, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
                      />
                      <TextField
                        value={h.value}
                        onChange={(e) => setHeaders(headers.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))}
                        placeholder="Bearer your-token"
                        size="small"
                        sx={{ flex: 1.2, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
                      />
                      <IconButton size="small" onClick={() => setHeaders(headers.filter((_, j) => j !== i))} sx={{ color: '#9CA3AF' }}>
                        <DeleteOutlineIcon sx={{ fontSize: 17 }} />
                      </IconButton>
                    </Box>
                  ))}
                  <Typography
                    onClick={() => setHeaders([...headers, { key: '', value: '' }])}
                    sx={{ fontSize: '12.5px', color: '#4F46E5', cursor: 'pointer', mb: 2, '&:hover': { textDecoration: 'underline' } }}
                  >
                    + 添加 Header
                  </Typography>

                  {/* OAuth（仅 Streamable HTTP）：公共客户端 + PKCE，令牌不进配置文件 */}
                  {mcpType === 'STREAMABLE_HTTP' && (
                    <Box sx={{ mb: 2, p: 1.5, border: '1px solid #EEF0F2', borderRadius: '12px', background: '#FAFBFC' }}>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                        <Switch
                          size="small"
                          checked={mcpAuthEnabled}
                          onChange={(e) => setMcpAuthEnabled(e.target.checked)}
                        />
                        <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#374151' }}>
                          启用 OAuth 授权
                        </Typography>
                      </Box>
                      <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF', lineHeight: 1.6 }}>
                        公共客户端 + PKCE，无需 client_secret。授权与刷新令牌由桌面端保存在系统凭据库，不写入配置文件，也不会回显。
                      </Typography>
                      {oauthHeaderConflict && (
                        <Typography sx={{ fontSize: '11.5px', color: '#DC2626', lineHeight: 1.6, mt: 0.5 }}>
                          请移除上方 Authorization 请求头：它会覆盖 OAuth 自动附加的令牌。
                        </Typography>
                      )}
                      {mcpAuthEnabled && (
                        <Box sx={{ mt: 1.5 }}>
                          <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>
                            授权地址 <Box component="span" sx={{ color: '#EF4444' }}>*</Box>
                          </Typography>
                          <TextField
                            value={mcpAuthUrl}
                            onChange={(e) => setMcpAuthUrl(e.target.value)}
                            fullWidth
                            size="small"
                            placeholder="https://auth.example.com/oauth/authorize"
                            sx={{ mb: 1.5, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
                          />
                          <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>
                            Token 地址 <Box component="span" sx={{ color: '#EF4444' }}>*</Box>
                          </Typography>
                          <TextField
                            value={mcpTokenUrl}
                            onChange={(e) => setMcpTokenUrl(e.target.value)}
                            fullWidth
                            size="small"
                            placeholder="https://auth.example.com/oauth/token"
                            sx={{ mb: 1.5, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
                          />
                          <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>
                            client_id <Box component="span" sx={{ color: '#EF4444' }}>*</Box>
                          </Typography>
                          <TextField
                            value={mcpClientId}
                            onChange={(e) => setMcpClientId(e.target.value)}
                            fullWidth
                            size="small"
                            placeholder="tongtian-desktop"
                            sx={{ mb: 1.5, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
                          />
                          <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>Scopes（可选）</Typography>
                          <TextField
                            value={mcpScopes}
                            onChange={(e) => setMcpScopes(e.target.value)}
                            fullWidth
                            size="small"
                            placeholder="openid profile，空格或逗号分隔"
                            helperText="授权与 Token 地址需为 HTTPS；本机服务（localhost / 127.0.0.1）允许 HTTP。"
                            FormHelperTextProps={{ sx: { fontSize: '11.5px', color: '#9CA3AF', mx: 0 } }}
                            sx={{ '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
                          />
                        </Box>
                      )}
                    </Box>
                  )}
                </>
              )}

              {/* 超时时间（可选） */}
              <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>超时时间（可选）</Typography>
              <TextField
                value={mcpTimeout}
                onChange={(e) => setMcpTimeout(e.target.value)}
                fullWidth
                size="small"
                type="number"
                inputProps={{ min: 1, max: 600 }}
                helperText="连接、工具列表获取及工具调用的超时时间，单位为秒（1-600）。留空使用默认值（60秒）。"
                FormHelperTextProps={{ sx: { fontSize: '11.5px', color: '#9CA3AF', mx: 0 } }}
                sx={{ mb: 2, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
              />

              {/* 添加按钮：必填未满足时禁用 */}
              <Button
                fullWidth
                variant="contained"
                disabled={!mcpValid || mcpSaving}
                onClick={handleAddMcp}
                sx={{
                  mt: 1,
                  mb: 1,
                  py: 1.1,
                  bgcolor: mcpValid ? '#111827' : '#E5E7EB',
                  borderRadius: '10px',
                  textTransform: 'none',
                  fontSize: '14px',
                  fontWeight: 700,
                  '&:hover': { bgcolor: '#4F46E5' },
                  '&.Mui-disabled': { bgcolor: '#E5E7EB', color: '#9CA3AF' },
                }}
              >
                {mcpSaving ? '登记中…' : '添加'}
              </Button>
            </DialogContent>
          </Dialog>

          {/* 通过 JSON 导入弹窗 */}
          <Dialog
            open={jsonOpen}
            onClose={() => setJsonOpen(false)}
            PaperProps={{ sx: { width: 520, borderRadius: '16px', p: 1 } }}
          >
            <DialogTitle sx={{ pb: 0.5 }}>
              <Typography sx={{ fontSize: '16px', fontWeight: 700 }}>通过 JSON 导入</Typography>
              <Typography sx={{ fontSize: '12.5px', color: '#9CA3AF', mt: 0.25 }}>请粘贴您的配置 JSON</Typography>
            </DialogTitle>
            <DialogContent sx={{ pt: '12px !important' }}>
              <TextField
                value={jsonText}
                onChange={(e) => setJsonText(e.target.value)}
                fullWidth
                multiline
                minRows={10}
                placeholder={JSON_IMPORT_PLACEHOLDER}
                sx={{
                  '& .MuiOutlinedInput-root': {
                    fontSize: '12px',
                    fontFamily: 'SFMono-Regular, Consolas, monospace',
                    background: '#FAFBFC',
                  },
                }}
              />
              {/* 导入按钮：未粘贴内容时禁用 */}
              <Button
                fullWidth
                variant="contained"
                disabled={!jsonText.trim() || mcpSaving}
                onClick={handleImportJson}
                sx={{
                  mt: 2,
                  mb: 1,
                  py: 1.1,
                  bgcolor: jsonText.trim() ? '#111827' : '#E5E7EB',
                  borderRadius: '10px',
                  textTransform: 'none',
                  fontSize: '14px',
                  fontWeight: 700,
                  '&:hover': { bgcolor: '#4F46E5' },
                  '&.Mui-disabled': { bgcolor: '#E5E7EB', color: '#9CA3AF' },
                }}
              >
                {mcpSaving ? '导入中…' : '导入'}
              </Button>
            </DialogContent>
          </Dialog>

          {/* 内置工具 / 市场 / 已安装 */}
          <Box sx={{ display: 'flex', gap: 2.5, mt: 1.5, mb: 2.5, borderBottom: '1px solid #F3F4F6' }}>
            {([
              ['builtin', `内置工具 ${builtinCount}`],
              ['market', '市场'],
              ['installed', `已安装 ${installedCount}`],
            ] as const).map(([key, label]) => (
              <Box
                key={key}
                onClick={() => setConnectorTab(key)}
                sx={{
                  pb: 1,
                  cursor: 'pointer',
                  borderBottom: connectorTab === key ? '2px solid #111827' : '2px solid transparent',
                }}
              >
                <Typography
                  sx={{
                    fontSize: '13px',
                    fontWeight: connectorTab === key ? 600 : 400,
                    color: connectorTab === key ? '#111827' : '#6B7280',
                  }}
                >
                  {label}
                </Typography>
              </Box>
            ))}
          </Box>

          {/* 内置工具：清单来自引擎注册表（或同源声明值），不可卸载 */}
          {connectorTab === 'builtin' && (
            builtinError ? (
              <Typography sx={{ fontSize: '12.5px', color: '#B91C1C', mb: 1.5 }}>{builtinError}</Typography>
            ) : !builtin ? (
              <Typography sx={{ fontSize: '12.5px', color: '#9CA3AF', mb: 1.5 }}>正在读取内置工具清单…</Typography>
            ) : (
              <>
                <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mb: 1.5 }}>
                  {builtin.note ?? (builtin.source === 'engine'
                    ? '清单读自运行中引擎的工具注册表'
                    : '按与引擎同源的声明清单显示')}
                </Typography>
                <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 2 }}>
              {builtin.tools.map((t: BuiltinTool) => {
                const Icon = TOOL_ICONS[t.id] ?? fallbackToolIcon;
                return (
                  <Box
                    key={t.id}
                    sx={{
                      border: '1px solid #EEF0F2',
                      borderRadius: '14px',
                      p: 2,
                      background: '#FFFFFF',
                      display: 'flex',
                      flexDirection: 'column',
                      gap: 1,
                    }}
                  >
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                      <Icon sx={{ fontSize: 18, color: '#374151' }} />
                      <Typography sx={{ fontSize: '14px', fontWeight: 600, color: '#111827' }}>{t.name}</Typography>
                      <Box sx={{ flexGrow: 1 }} />
                      <Typography sx={{ fontSize: '11px', color: '#9CA3AF' }}>内置</Typography>
                    </Box>
                    <Typography sx={{ fontSize: '12px', color: '#6B7280', lineHeight: 1.6, minHeight: 38 }}>
                      {t.desc}
                    </Typography>
                    <Box sx={{ display: 'flex', justifyContent: 'flex-end', mt: 0.5 }}>
                      <Box sx={{ px: 1.5, py: 0.5, borderRadius: '8px', background: '#E6F7F4' }}>
                        <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#0D9488' }}>已内置</Typography>
                      </Box>
                    </Box>
                  </Box>
                );
              })}
                </Box>
              </>
            )
          )}

          {/* 市场目录：点开即打开手动配置弹窗并预填名称（市场自动安装未接入，不伪造「已安装」） */}
          {connectorTab === 'market' && (
            <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mb: 1.5 }}>
              目录内连接器需在下方弹窗填写真实启动命令或服务地址后登记；登记完成即可点击「连接」握手取工具清单。
            </Typography>
          )}
          {connectorTab !== 'builtin' && connectorTab !== 'installed' && (
          <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 2 }}>
            {CONNECTORS.map((c) => {
              const Icon = c.icon;
              return (
                <Box
                  key={c.id}
                  sx={{
                    border: '1px solid #EEF0F2',
                    borderRadius: '14px',
                    p: 2,
                    background: '#FFFFFF',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 1,
                  }}
                >
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Icon sx={{ fontSize: 18, color: '#374151' }} />
                    <Typography sx={{ fontSize: '14px', fontWeight: 600, color: '#111827' }}>{c.name}</Typography>
                    <Box sx={{ flexGrow: 1 }} />
                    <Typography sx={{ fontSize: '11px', color: '#9CA3AF' }}>{c.category}</Typography>
                  </Box>
                  <Typography sx={{ fontSize: '12px', color: '#6B7280', lineHeight: 1.6, minHeight: 38 }}>
                    {c.desc}
                  </Typography>
                  <Box sx={{ display: 'flex', justifyContent: 'flex-end', mt: 0.5 }}>
                    <Button
                      size="small"
                      variant="contained"
                      onClick={() => openMcpDialog(c.name)}
                      sx={{
                        bgcolor: '#111827',
                        borderRadius: '8px',
                        textTransform: 'none',
                        fontSize: '12px',
                        px: 2,
                        '&:hover': { bgcolor: '#1F2937' },
                      }}
                    >
                      登记
                    </Button>
                  </Box>
                </Box>
              );
            })}
          </Box>
          )}

          {/* 已安装 = 真实登记在册的 MCP 连接器（mcp_servers / 本地台账），含握手状态与工具数 */}
          {connectorTab === 'installed' && mcpServers.length > 0 && (
            <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 2 }}>
              {mcpServers.map((s) => (
                <Box
                  key={s.serverId}
                  sx={{
                    border: '1px solid #EEF0F2',
                    borderRadius: '14px',
                    p: 2,
                    background: '#FFFFFF',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 1,
                  }}
                >
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    <TerminalIcon sx={{ fontSize: 18, color: '#374151' }} />
                    <Typography sx={{ fontSize: '14px', fontWeight: 600, color: '#111827' }}>{s.name}</Typography>
                    <Box sx={{ flexGrow: 1 }} />
                    <Typography sx={{ fontSize: '11px', color: '#9CA3AF' }}>
                      {TRANSPORT_LABEL_TEXT[s.transport] ?? s.transport}
                    </Typography>
                  </Box>
                  <Typography sx={{ fontSize: '12px', color: '#6B7280', lineHeight: 1.6 }}>
                    {s.transport === 'stdio'
                      ? `启动命令：${s.command}${s.args.length ? ` ${s.args.join(' ')}` : ''}`
                      : `服务地址：${s.url}`}
                  </Typography>
                  {s.status === 'connected' ? (
                    <Typography sx={{ fontSize: '12px', color: '#0D9488', fontWeight: 600 }}>
                      已连接 · {s.tools.length} 个工具
                    </Typography>
                  ) : s.status === 'failed' ? (
                    <Typography sx={{ fontSize: '12px', color: '#DC2626', lineHeight: 1.6 }}>
                      上次连接失败：{s.lastError || '未知原因'}
                    </Typography>
                  ) : (
                    <Typography sx={{ fontSize: '12px', color: '#9CA3AF' }}>
                      未连接（配置已保存，握手后才会显示工具清单）
                    </Typography>
                  )}
                  {/* stdio 消耗环境变量、远程消耗请求头，另一侧不展示避免造成假凭据项 */}
                  {(s.transport === 'stdio' ? s.envKeys : s.headerKeys).length > 0 && (
                    <Typography sx={{ fontSize: '11px', color: '#9CA3AF' }}>
                      凭据项：{(s.transport === 'stdio' ? s.envKeys : s.headerKeys).join('、')}（值仅存本机，不回显）
                    </Typography>
                  )}
                  {s.auth && (
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}>
                      <Box
                        sx={{
                          px: 1,
                          py: 0.25,
                          borderRadius: '6px',
                          background:
                            s.authStatus === 'authorized' ? '#ECFDF5' : s.authStatus === 'expired' ? '#FFFBEB' : '#F3F4F6',
                        }}
                      >
                        <Typography
                          sx={{
                            fontSize: '11px',
                            fontWeight: 600,
                            color:
                              s.authStatus === 'authorized' ? '#0D9488' : s.authStatus === 'expired' ? '#B45309' : '#6B7280',
                          }}
                        >
                          {s.authStatus === 'authorized'
                            ? 'OAuth 已授权'
                            : s.authStatus === 'expired'
                              ? 'OAuth 令牌已过期'
                              : 'OAuth 未授权'}
                        </Typography>
                      </Box>
                      <Typography sx={{ fontSize: '11px', color: '#9CA3AF' }}>
                        {s.authStatus === 'none'
                          ? '连接前需在系统浏览器完成授权'
                          : s.authStatus === 'expired'
                            ? '连接时将尝试自动刷新，失败需重新授权'
                            : '令牌仅存系统凭据库'}
                      </Typography>
                    </Box>
                  )}
                  {s.auth && !desktopChannel && (
                    <Typography sx={{ fontSize: '11px', color: '#B45309', lineHeight: 1.6 }}>
                      浏览器通道无法保存 OAuth 令牌，授权与连接请在桌面版任我行中完成。
                    </Typography>
                  )}
                  <Box sx={{ display: 'flex', justifyContent: 'flex-end', alignItems: 'center', gap: 1, mt: 0.5, flexWrap: 'wrap' }}>
                    {s.auth && s.authStatus !== 'none' && (
                      <Button
                        size="small"
                        variant="text"
                        disabled={mcpBusyId === s.serverId || !desktopChannel}
                        onClick={() => void handleRevokeMcp(s)}
                        sx={{ borderRadius: '8px', textTransform: 'none', fontSize: '11.5px', minWidth: 0, color: '#9CA3AF' }}
                      >
                        撤销授权
                      </Button>
                    )}
                    {s.auth && s.authStatus === 'expired' && s.status !== 'connected' && (
                      <Button
                        size="small"
                        variant="outlined"
                        disabled={mcpBusyId === s.serverId || !desktopChannel}
                        onClick={() => void handleAuthorizeMcp(s)}
                        sx={{ borderRadius: '8px', textTransform: 'none', fontSize: '12px' }}
                      >
                        重新授权
                      </Button>
                    )}
                    {s.auth && s.authStatus === 'none' ? (
                      <Button
                        size="small"
                        variant="contained"
                        disabled={mcpBusyId === s.serverId || !desktopChannel}
                        onClick={() => void handleAuthorizeMcp(s)}
                        sx={{
                          bgcolor: '#111827',
                          borderRadius: '8px',
                          textTransform: 'none',
                          fontSize: '12px',
                          px: 2,
                          '&:hover': { bgcolor: '#1F2937' },
                        }}
                      >
                        {mcpBusyId === s.serverId ? '等待浏览器授权…' : '授权'}
                      </Button>
                    ) : s.status === 'connected' ? (
                      <Button
                        size="small"
                        variant="outlined"
                        disabled={mcpBusyId === s.serverId}
                        onClick={() => void handleDisconnectMcp(s)}
                        sx={{ borderRadius: '8px', textTransform: 'none', fontSize: '12px' }}
                      >
                        断开
                      </Button>
                    ) : (
                      <Button
                        size="small"
                        variant="contained"
                        disabled={mcpBusyId === s.serverId}
                        onClick={() => void handleConnectMcp(s)}
                        sx={{
                          bgcolor: '#111827',
                          borderRadius: '8px',
                          textTransform: 'none',
                          fontSize: '12px',
                          px: 2,
                          '&:hover': { bgcolor: '#1F2937' },
                        }}
                      >
                        {mcpBusyId === s.serverId ? '握手中…' : '连接'}
                      </Button>
                    )}
                    <IconButton
                      size="small"
                      onClick={() => setServerToRemove(s)}
                      sx={{ color: '#9CA3AF', '&:hover': { color: '#DC2626' } }}
                    >
                      <DeleteOutlineIcon sx={{ fontSize: 16 }} />
                    </IconButton>
                  </Box>
                </Box>
              ))}
            </Box>
          )}
          {connectorTab === 'installed' && mcpLoaded && mcpServers.length === 0 && (
            <Typography sx={{ fontSize: '13px', color: '#9CA3AF', p: 4, textAlign: 'center' }}>
              {mcpError ?? '暂无已登记的 MCP 连接器，点击「+ 添加」手动配置或用 JSON 导入'}
            </Typography>
          )}

          {/* 移除连接器确认（只删这一条登记，不动任务与审计存量数据） */}
          <Dialog open={!!serverToRemove} onClose={() => setServerToRemove(null)} maxWidth="xs" fullWidth>
            <DialogTitle sx={{ fontSize: '16px', fontWeight: 700 }}>移除连接器登记</DialogTitle>
            <DialogContent>
              <Typography sx={{ fontSize: '13px', color: '#6B7280', lineHeight: 1.7 }}>
                将移除「{serverToRemove?.name}」的连接配置{serverToRemove?.status === 'connected' ? '（当前处于已连接状态）' : ''}。
                {serverToRemove?.auth ? '该连接器保存在系统凭据库中的 OAuth 令牌也会一并清除。' : ''}
                历史任务与审计记录不受影响，之后可用相同配置重新登记。
              </Typography>
            </DialogContent>
            <DialogActions sx={{ px: 3, pb: 2 }}>
              <Button onClick={() => setServerToRemove(null)} sx={{ textTransform: 'none', fontSize: '13px' }}>
                取消
              </Button>
              <Button
                variant="contained"
                color="error"
                disabled={mcpBusyId === serverToRemove?.serverId}
                onClick={() => void handleConfirmRemoveMcp()}
                sx={{ textTransform: 'none', fontSize: '13px', borderRadius: '8px' }}
              >
                确认移除
              </Button>
            </DialogActions>
          </Dialog>
        </Box>
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

export default ExtensionsPage;
