/**
 * 通天办公 - 扩展页面（设计图对齐版）
 *
 * 智能体能力由两部分构成：技能、连接器
 * - 连接器：内置工具（随应用内置）+ MCP 市场/已安装（P2 接 connector_*）
 * - 技能：SKILL.md 注入（P1 接 skill_list，当前 Mock 数据）
 *
 * 标签页由 URL 查询参数驱动：/extensions?tab=skills|connectors
 */

import React, { useEffect, useState } from 'react';
import { Box, Button, Tab, Tabs, Tooltip, Typography, Snackbar, Alert, Menu, Dialog, DialogTitle, DialogContent, Select, MenuItem, TextField, IconButton } from '@mui/material';
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

// ── 对话创建技能的预置提示词（create-skill 流程，保存目录与设计目录对应） ──

const SKILL_CREATE_PROMPT =
  '帮我使用 create-skill 创建技能。请先检测当前运行环境，将技能保存到正确路径：虚拟机环境下保存到 /root/.tongtianwork/skills/，否则保存到 ~/.tongtianwork/skills/。首先问我技能应该做什么。';

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

/** 原始 tongtianAPI（skill:list 等通用 channel 走 invoke） */
function rawInvoke(channel: string): Promise<unknown> {
  const apiObj = (window as unknown as { tongtianAPI?: { invoke: (c: string) => Promise<unknown> } }).tongtianAPI;
  if (!apiObj) return Promise.resolve([]);
  return apiObj.invoke(channel);
}

type TabKey = 'skills' | 'connectors';

// ── 内置工具（随应用内置） ──────────────────────────────

const BUILTIN_TOOLS = [
  { id: 'file-ops', name: '文件管理', desc: '查看、整理、转换本地文件（Markdown 转 Word、Excel 转 CSV 等）', icon: FolderOutlinedIcon },
  { id: 'doc-ops', name: '文档处理', desc: '读取、编写、生成各类文档', icon: DescriptionOutlinedIcon },
  { id: 'shell', name: '命令执行', desc: '在本地运行 Shell / CMD / PowerShell 命令', icon: TerminalIcon },
  { id: 'sys-query', name: '系统查询', desc: '查看系统信息、目录内容、环境状态等', icon: SearchIcon },
  { id: 'data-ops', name: '数据分析', desc: '汇总数据、生成图表与统计摘要', icon: InsightsIcon },
];

// ── 连接器市场（P2 接入 connector_*，当前展示 + 占位安装） ──

const CONNECTORS = [
  { id: 'browser', name: '浏览器自动化', category: '系统级', desc: '连接 Puppeteer 浏览器，实现网页自动化、截图与数据抓取。', icon: PublicIcon },
  { id: 'fs', name: '本地文件系统', category: '系统级', desc: '允许 AI 在指定目录内安全地读写文件。', icon: FolderOutlinedIcon },
  { id: 'slack', name: 'Slack', category: 'IM', desc: '发送消息、管理频道，与 Slack 工作区交互。', icon: ScheduleIcon },
  { id: 'web', name: 'Web 请求', category: '数据', desc: '让 AI 可以发起 HTTP 请求并获取网页内容。', icon: LinkIcon },
  { id: 'pg', name: 'PostgreSQL', category: '数据', desc: '连接 PostgreSQL 数据库执行只读查询。', icon: InsightsIcon },
  { id: 'github', name: 'GitHub', category: '开发', desc: '读取仓库、Issue、PR，与 GitHub API 交互。', icon: TerminalIcon },
];

interface SkillItem {
  id: string;
  name: string;
  description?: string;
  injected?: boolean;
}

/** 缺省技能数据（Tauri 模式 skill_list 未落地时兜底展示，标注示例） */
const FALLBACK_SKILLS: SkillItem[] = [
  { id: 'local-cli', name: 'local-cli', description: '本地 CLI 与系统查询', injected: true },
];

const ExtensionsPage: React.FC = () => {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = (searchParams.get('tab') as TabKey) || 'connectors';
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [skillsLoaded, setSkillsLoaded] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [installed, setInstalled] = useState<Record<string, boolean>>({ fs: true });
  // 支持 URL 指定连接器子页签（侧边栏专家套件入口 → sub=builtin）
  const [connectorTab, setConnectorTab] = useState<'builtin' | 'market' | 'installed'>(() => {
    const sub = searchParams.get('sub');
    return sub === 'market' || sub === 'installed' ? sub : 'builtin';
  });

  // 技能列表（skill:list；未落地时兜底示例数据）
  useEffect(() => {
    (async () => {
      try {
        const list = (await rawInvoke('skill:list')) as unknown as SkillItem[];
        setSkills(Array.isArray(list) && list.length > 0 ? list : FALLBACK_SKILLS);
      } catch {
        setSkills(FALLBACK_SKILLS);
      } finally {
        setSkillsLoaded(true);
      }
    })();
  }, []);

  const switchTab = (next: TabKey) => {
    setSearchParams({ tab: next });
  };

  // ── 技能「+ 添加」：下拉菜单 + 上传弹窗状态 ──
  const [skillAddAnchor, setSkillAddAnchor] = useState<null | HTMLElement>(null);
  const [skillUploadOpen, setSkillUploadOpen] = useState(false);
  const [skillUploadFile, setSkillUploadFile] = useState<File | null>(null);
  const [skillDragOver, setSkillDragOver] = useState(false);

  // 通过对话创建技能：跳转新任务对话窗口并预置 create-skill 提示词
  const handleCreateSkillByChat = () => {
    setSkillAddAnchor(null);
    navigate(`/?prompt=${encodeURIComponent(SKILL_CREATE_PROMPT)}`);
  };

  // 上传技能：选择/拖拽 .zip 或 SKILL.md
  const handlePickSkillFile = (file: File | undefined | null) => {
    if (!file) return;
    const name = file.name.toLowerCase();
    if (!name.endsWith('.zip') && name !== 'skill.md') {
      setToast('仅支持包含 SKILL.md 的 .zip 压缩包或 SKILL.md 文件');
      return;
    }
    setSkillUploadFile(file);
  };

  // 安装上传的技能（Mock 开关：开发阶段本地模拟，联调阶段接本地安装流程）
  const handleInstallSkillUpload = () => {
    if (!skillUploadFile) return;
    if (mockEnabled) {
      setSkillUploadOpen(false);
      setSkillUploadFile(null);
      setToast(`「${skillUploadFile.name}」校验通过，将安装到 skills 目录（本地安装流程即将上线）`);
      return;
    }
    // 联调阶段：真实技能安装接口接入位（校验 SKILL.md 后安装到 skills 目录）
    setSkillUploadOpen(false);
    setSkillUploadFile(null);
  };

  /** Mock 开关（技能上传安装的开发阶段模拟） */
  const mockEnabled = import.meta.env.VITE_MOCK !== 'false';

  // ── 连接器「+ 添加」：手动配置 / JSON 导入 ──
  const [connAddAnchor, setConnAddAnchor] = useState<null | HTMLElement>(null);
  const [mcpOpen, setMcpOpen] = useState(false);
  const [mcpType, setMcpType] = useState<McpServerType>('STDIO');
  const [mcpName, setMcpName] = useState('my-mcp-server');
  const [mcpCommand, setMcpCommand] = useState('npx -y @modelcontextprotocol/server-filesystem');
  const [mcpUrl, setMcpUrl] = useState('http://localhost:8080/sse');
  const [envVars, setEnvVars] = useState<KvPair[]>([{ key: 'API_KEY', value: '' }]);
  const [headers, setHeaders] = useState<KvPair[]>([{ key: 'Authorization', value: 'Bearer your-token' }]);
  const [mcpTimeout, setMcpTimeout] = useState('60');
  const [jsonOpen, setJsonOpen] = useState(false);
  const [jsonText, setJsonText] = useState('');

  // 手动配置必填校验：名称 + （STDIO 命令 / 远程地址）
  const mcpValid =
    mcpName.trim().length > 0 && (mcpType === 'STDIO' ? mcpCommand.trim().length > 0 : mcpUrl.trim().length > 0);

  // 提交手动配置（Mock 开关：开发阶段本地模拟，联调阶段接 MCP 注册流程）
  const handleAddMcp = () => {
    if (!mcpValid) return;
    setMcpOpen(false);
    setToast(`「${mcpName.trim()}」配置校验通过，连接器添加流程即将上线（MCP 接入 P2）`);
  };

  // JSON 导入：解析校验后提交
  const handleImportJson = () => {
    const text = jsonText.trim();
    if (!text) return;
    try {
      JSON.parse(text);
      setJsonOpen(false);
      setJsonText('');
      setToast('JSON 配置校验通过，连接器导入流程即将上线（MCP 接入 P2）');
    } catch {
      setToast('JSON 格式无效，请检查后重试');
    }
  };

  const installedCount = Object.values(installed).filter(Boolean).length;

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
        <Tab value="connectors" label={`连接器 (${BUILTIN_TOOLS.length + installedCount})`} />
      </Tabs>

      {/* ── 技能 ── */}
      {tab === 'skills' && (
        <Box>
          <Box sx={{ display: 'flex', justifyContent: 'flex-end', mb: 2 }}>
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
              onClick={() => {
                setSkillAddAnchor(null);
                setSkillUploadFile(null);
                setSkillUploadOpen(true);
              }}
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
                  导入技能文件，仅本地化安装
                </Typography>
              </Box>
            </Box>
          </Menu>

          {/* 上传技能弹窗 */}
          <Dialog
            open={skillUploadOpen}
            onClose={() => setSkillUploadOpen(false)}
            PaperProps={{ sx: { width: 460, borderRadius: '16px', p: 1 } }}
          >
            <DialogTitle sx={{ fontSize: '16px', fontWeight: 700, pb: 1 }}>安装技能</DialogTitle>
            <DialogContent sx={{ pt: '8px !important' }}>
              {/* 拖拽/点击上传区 */}
              <Box
                component="label"
                onDragOver={(e) => {
                  e.preventDefault();
                  setSkillDragOver(true);
                }}
                onDragLeave={() => setSkillDragOver(false)}
                onDrop={(e) => {
                  e.preventDefault();
                  setSkillDragOver(false);
                  handlePickSkillFile(e.dataTransfer.files?.[0]);
                }}
                sx={{
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                  gap: 1,
                  py: 4,
                  borderRadius: '12px',
                  border: skillDragOver ? '1.5px dashed #8B5CF6' : '1.5px dashed #D1D5DB',
                  background: skillDragOver ? 'rgba(139, 92, 246, 0.04)' : '#FAFBFC',
                  cursor: 'pointer',
                  transition: 'border-color 0.15s ease, background 0.15s ease',
                  '&:hover': { borderColor: '#8B5CF6' },
                }}
              >
                <CloudUploadOutlinedIcon sx={{ fontSize: 32, color: skillUploadFile ? '#8B5CF6' : '#9CA3AF' }} />
                <Typography sx={{ fontSize: '13px', color: skillUploadFile ? '#4C1D95' : '#6B7280', fontWeight: skillUploadFile ? 600 : 400 }}>
                  {skillUploadFile ? skillUploadFile.name : '拖放 .zip 或 SKILL.md 文件，或点击选择'}
                </Typography>
                <input
                  type="file"
                  accept=".zip,.md"
                  hidden
                  onChange={(e) => handlePickSkillFile(e.target.files?.[0])}
                />
              </Box>

              {/* 文件要求说明 */}
              <Box sx={{ mt: 2 }}>
                <Typography sx={{ fontSize: '12px', color: '#6B7280', lineHeight: 1.7 }}>
                  文件要求：
                </Typography>
                <Typography sx={{ fontSize: '12px', color: '#9CA3AF', lineHeight: 1.7 }}>
                  · 包含 SKILL.md 文件的 .zip 压缩包
                </Typography>
                <Typography sx={{ fontSize: '12px', color: '#9CA3AF', lineHeight: 1.7 }}>
                  · 或直接插入 SKILL.md 文件
                </Typography>
                <Typography sx={{ fontSize: '12px', color: '#9CA3AF', lineHeight: 1.7 }}>
                  · 安装目录与设计目录对应：~/.tongtianwork/skills/（虚拟机环境为 /root/.tongtianwork/skills/）
                </Typography>
              </Box>

              {/* 安装按钮：未选择文件时禁用 */}
              <Box sx={{ display: 'flex', justifyContent: 'flex-end', mt: 2.5, mb: 1 }}>
                <Button
                  variant="contained"
                  disabled={!skillUploadFile}
                  onClick={handleInstallSkillUpload}
                  sx={{
                    bgcolor: skillUploadFile ? '#111827' : '#E5E7EB',
                    color: skillUploadFile ? '#fff' : '#9CA3AF',
                    borderRadius: '10px',
                    textTransform: 'none',
                    fontSize: '13px',
                    fontWeight: 600,
                    px: 3,
                    '&:hover': { bgcolor: '#4F46E5' },
                    '&.Mui-disabled': { bgcolor: '#E5E7EB', color: '#9CA3AF' },
                  }}
                >
                  安装
                </Button>
              </Box>
            </DialogContent>
          </Dialog>

          {skillsLoaded && skills.length === 0 ? (
            <Typography sx={{ fontSize: '13px', color: '#9CA3AF', p: 4, textAlign: 'center' }}>
              暂无技能，技能市场即将上线
            </Typography>
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
                    <Typography sx={{ fontSize: '14px', fontWeight: 600, color: '#111827' }}>{s.name}</Typography>
                    {s.description && (
                      <Typography sx={{ fontSize: '12px', color: '#6B7280', mt: 0.25 }}>{s.description}</Typography>
                    )}
                  </Box>
                  {s.injected && (
                    <Box
                      sx={{
                        px: 1.25,
                        py: 0.4,
                        borderRadius: '8px',
                        background: '#E6F7F4',
                        border: '1px solid rgba(20, 184, 166, 0.25)',
                      }}
                    >
                      <Typography sx={{ fontSize: '11px', fontWeight: 600, color: '#0D9488' }}>已注入</Typography>
                    </Box>
                  )}
                </Box>
              ))}
            </Box>
          )}
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
                disabled={!mcpValid}
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
                添加
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
                disabled={!jsonText.trim()}
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
                导入
              </Button>
            </DialogContent>
          </Dialog>

          {/* 内置工具 / 市场 / 已安装 */}
          <Box sx={{ display: 'flex', gap: 2.5, mt: 1.5, mb: 2.5, borderBottom: '1px solid #F3F4F6' }}>
            {([
              ['builtin', `内置工具 ${BUILTIN_TOOLS.length}`],
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

          {/* 内置工具：随应用内置，不可卸载 */}
          {connectorTab === 'builtin' && (
            <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 2 }}>
              {BUILTIN_TOOLS.map((t) => {
                const Icon = t.icon;
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
          )}

          {/* MCP 连接器卡片网格 */}
          {connectorTab !== 'builtin' && (
          <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 2 }}>
            {(connectorTab === 'market'
              ? CONNECTORS
              : CONNECTORS.filter((c) => installed[c.id])
            ).map((c) => {
              const Icon = c.icon;
              const isInstalled = !!installed[c.id];
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
                    {isInstalled ? (
                      <Box sx={{ px: 1.5, py: 0.5, borderRadius: '8px', background: '#E6F7F4' }}>
                        <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#0D9488' }}>已安装</Typography>
                      </Box>
                    ) : (
                      <Button
                        size="small"
                        variant="contained"
                        onClick={() => setToast(`「${c.name}」安装能力即将上线（MCP 接入 P2）`)}
                        sx={{
                          bgcolor: '#111827',
                          borderRadius: '8px',
                          textTransform: 'none',
                          fontSize: '12px',
                          px: 2,
                          '&:hover': { bgcolor: '#1F2937' },
                        }}
                      >
                        安装
                      </Button>
                    )}
                  </Box>
                </Box>
              );
            })}
          </Box>
          )}
          {connectorTab === 'installed' && installedCount === 0 && (
            <Typography sx={{ fontSize: '13px', color: '#9CA3AF', p: 4, textAlign: 'center' }}>
              暂无已安装连接器
            </Typography>
          )}
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
