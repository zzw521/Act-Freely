/**
 * 通天办公 - 对话输入组件 (高保真原型 v3)
 *
 * 设计稿还原:
 * - 大圆角白色输入容器
 * - 顶部占位文本区
 * - 底部工具栏: 加号/图片/链接/技能 + 「通用」下拉 | 高级 + 麦克风 + 发送
 * - Enter 发送 / Shift+Enter 换行
 */

import React, { useState, useRef, useCallback, useEffect } from 'react';
import {
  Box,
  IconButton,
  TextField,
  Tooltip,
  Typography,
  Menu,
  MenuItem,
  Alert,
  Chip,
  Popover,
} from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import AttachFileIcon from '@mui/icons-material/AttachFile';
import FolderOutlinedIcon from '@mui/icons-material/FolderOutlined';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import MicIcon from '@mui/icons-material/Mic';
import ArrowUpwardIcon from '@mui/icons-material/ArrowUpward';
import StopIcon from '@mui/icons-material/Stop';
import CheckIcon from '@mui/icons-material/Check';
import type { AttachmentMeta } from '../../../shared/types';
import { isTauri } from '../../lib/tauri-api';
import { chatAPI } from '../../lib/api';
import { loadAllSkills, type SkillInfo } from '../../lib/skills';
import { useAppSettings, WORKBENCH_MODE_DEFS } from '../../lib/use-app-settings';
import { useNavigate } from 'react-router-dom';

interface ChatInputProps {
  /** 发送消息回调（两阶段协议：附件暂存后随提交上送；modelBinding 为选中模型） */
  onSend: (text: string, attachments?: AttachmentMeta[], modelBinding?: string) => void;
  /** 是否处于加载状态（思考/流式生成中） */
  loading?: boolean;
  /** 占位符文本 */
  placeholder?: string;
  /** 最大行数（超过后出现滚动条） */
  maxRows?: number;
  /** 初始输入文本（如从其他页面携带提示词跳转新任务） */
  initialText?: string;
  /** 当前工作目录（会话内容与产出物存放根目录） */
  workDir?: string;
  /** 选择工作目录回调（真实目录选择） */
  onSelectWorkDir?: (dir: string) => void;
}

// 附件约束（交互补设计 §3.3；含图片格式，原「插入图片」已合并到添加文件）
const ATTACH_EXTS = ['docx', 'xlsx', 'pdf', 'csv', 'txt', 'md', 'png', 'jpg', 'jpeg', 'gif', 'webp'];
const ATTACH_MAX_COUNT = 10;

/** 文件名 → 扩展名 */
function extOf(name: string): string {
  const i = name.lastIndexOf('.');
  return i >= 0 ? name.slice(i + 1).toLowerCase() : '';
}

// 模式选项来自设置页「工作台」分区（启用模式 + 默认模式，见 lib/use-app-settings）

/** 读取本地文件内容为 base64（浏览器模式真实上传用） */
function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result as string;
      resolve(result.slice(result.indexOf(',') + 1));
    };
    reader.onerror = () => reject(reader.error ?? new Error('读取文件失败'));
    reader.readAsDataURL(file);
  });
}

// 内置模型（对话模型选择；modelId 为提交时绑定给引擎的真实模型名）
// 实测：qwen3.7-plus 名称不可用 → 基础用 qwen-plus；qwen3.7-flash 免费额度用尽 → 经济用 qwen-flash
const BUILTIN_MODELS = [
  { id: 'auto', name: 'Auto Mode', desc: '自动选择最合适的模型', color: '#6366F1', modelId: '' },
  { id: 'qwen-max', name: 'Qwen3.8-Max', desc: '千问最强模型 · 1.1x', color: '#8B5CF6', modelId: 'qwen3.8-max' },
  { id: 'advanced', name: '高级', desc: '性能均衡，擅长复杂任务 · 1x', color: '#3B82F6', modelId: 'qwen3.7-max' },
  { id: 'basic', name: '基础', desc: '高性价比，胜任日常任务 · 0.25x', color: '#10B981', modelId: 'qwen-plus' },
  { id: 'economy', name: '经济', desc: '经济实惠，适合简单任务 · 0.1x', color: '#F59E0B', modelId: 'qwen-flash' },
];

const ChatInput: React.FC<ChatInputProps> = ({
  onSend,
  loading = false,
  placeholder = '描述任务，输入 /调用技能',
  maxRows = 8,
  initialText,
  workDir,
  onSelectWorkDir,
}) => {
  const [value, setValue] = useState(initialText ?? '');
  const [focused, setFocused] = useState(false);
  const [mode, setMode] = useState('通用');
  const [modeAnchor, setModeAnchor] = useState<null | HTMLElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const navigate = useNavigate();

  // ── 模型选择（替换原「添加到队列」占位） ──
  const [modelAnchor, setModelAnchor] = useState<null | HTMLElement>(null);
  const [selectedModel, setSelectedModel] = useState(BUILTIN_MODELS[0]);

  // ── 技能调用（魔棒按钮 + / 前缀，真实加载已安装+内置技能） ──
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [skillsAnchor, setSkillsAnchor] = useState<null | HTMLElement>(null);
  const [skillFilter, setSkillFilter] = useState('');
  /** 已引用技能：输入框内嵌引用块，点击展开底层详情块 */
  const [referencedSkill, setReferencedSkill] = useState<SkillInfo | null>(null);
  const [skillBlockAnchor, setSkillBlockAnchor] = useState<null | HTMLElement>(null);
  const inputWrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    loadAllSkills().then(setSkills).catch(() => undefined);
  }, []);

  const filteredSkills = skills.filter((s) => {
    if (!skillFilter) return true;
    const q = skillFilter.toLowerCase();
    return s.name.toLowerCase().includes(q) || (s.description ?? '').toLowerCase().includes(q);
  });

  /** 选中技能：生成技能引用块（输入框内不再保留 /文本），发送时拼接上送 */
  const pickSkill = (s: SkillInfo) => {
    setReferencedSkill(s);
    // 移除输入框中已输入的 /筛选词，保留其余内容
    setValue((v) => v.replace(/^\/\S*\s?/, ''));
    setSkillsAnchor(null);
    setSkillFilter('');
    setTimeout(() => inputRef.current?.focus(), 50);
  };

  // 外部提示词变化时同步到输入框（如模板弹窗选择后回填）
  useEffect(() => {
    if (initialText) setValue(initialText);
  }, [initialText]);

  // ── 工作台模式（与设置页口径一致） ──
  const [appSettings] = useAppSettings();
  const enabledModes = (appSettings.workbenchModes as string[] | undefined) ?? ['general', 'design', 'slides'];
  const modeOptions: string[] = WORKBENCH_MODE_DEFS.filter((d) => enabledModes.includes(d.id)).map((d) => d.name);
  // 默认模式兼容存 id（general）或存中文名（通用）两种历史格式
  const rawDefault = (appSettings.workbenchDefaultMode as string) ?? 'general';
  const defaultModeName =
    WORKBENCH_MODE_DEFS.find((d) => d.id === rawDefault)?.name ??
    WORKBENCH_MODE_DEFS.find((d) => d.name === rawDefault)?.name ??
    '通用';
  // 用户手动选择过模式后，不再被默认值覆盖
  const modeTouched = useRef(false);
  useEffect(() => {
    if (!modeTouched.current) setMode(defaultModeName);
  }, [defaultModeName]);
  useEffect(() => {
    if (!modeOptions.includes(mode)) setMode(defaultModeName);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modeOptions.join(','), defaultModeName]);

  // ── 附件状态（交互补设计 §3 附件模式；支持真实文件/文件夹读取与上传） ──
  interface PendingAttachment {
    fileName: string;
    srcPath: string;
    staged?: AttachmentMeta;
    error?: string;
    /** 浏览器模式选中的真实文件（发送时读取内容上传到对话接口） */
    file?: File;
    /** 附件类型：文件 / 文件夹 */
    kind?: 'file' | 'folder';
  }
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [attachError, setAttachError] = useState<string | null>(null);
  const [staging, setStaging] = useState(false);
  const [addContentAnchor, setAddContentAnchor] = useState<null | HTMLElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);

  /** 通用校验与追加 */
  const pushAttachments = useCallback((items: PendingAttachment[]) => {
    if (attachments.length >= ATTACH_MAX_COUNT) {
      setAttachError(`单次最多 ${ATTACH_MAX_COUNT} 个附件`);
      return;
    }
    if (attachments.length + items.length > ATTACH_MAX_COUNT) {
      setAttachError(`单次最多 ${ATTACH_MAX_COUNT} 个附件，已截断`);
    }
    setAttachments((prev) => [...prev, ...items].slice(0, ATTACH_MAX_COUNT));
  }, [attachments.length]);

  /** 添加文件：Tauri 走系统对话框；浏览器走真实文件选择器 */
  const handleAddFiles = useCallback(async () => {
    setAttachError(null);
    if (isTauri()) {
      try {
        const { open } = await import('@tauri-apps/plugin-dialog');
        const picked = await open({
          multiple: true,
          filters: [{ name: '文档', extensions: ATTACH_EXTS }],
        });
        const paths = Array.isArray(picked) ? picked : picked ? [picked] : [];
        const next: PendingAttachment[] = [];
        for (const p of paths as string[]) {
          const fileName = p.split(/[\\/]/).pop() ?? p;
          if (!ATTACH_EXTS.includes(extOf(fileName))) {
            setAttachError(`不支持的格式：${fileName}（支持 ${ATTACH_EXTS.join('/')}）`);
            continue;
          }
          next.push({ fileName, srcPath: p, kind: 'file' });
        }
        pushAttachments(next);
      } catch (e) {
        setAttachError(`打开文件对话框失败：${e}`);
      }
    } else {
      fileInputRef.current?.click();
    }
  }, [pushAttachments]);

  /** 添加文件夹：Tauri 选目录后整体暂存；浏览器真实读取目录内全部文件 */
  const handleAddFolder = useCallback(async () => {
    setAttachError(null);
    if (isTauri()) {
      try {
        const { open } = await import('@tauri-apps/plugin-dialog');
        const picked = await open({ directory: true });
        if (typeof picked === 'string' && picked) {
          const dirName = picked.split(/[\\/]/).filter(Boolean).pop() ?? picked;
          pushAttachments([{ fileName: `${dirName}（文件夹）`, srcPath: picked, kind: 'folder' }]);
        }
      } catch (e) {
        setAttachError(`打开文件夹对话框失败：${e}`);
      }
    } else {
      folderInputRef.current?.click();
    }
  }, [pushAttachments]);

  /** 浏览器：真实文件选择回调 */
  const onFilesPicked = useCallback((fileList: FileList | null) => {
    if (!fileList || fileList.length === 0) return;
    const next: PendingAttachment[] = [];
    for (const f of Array.from(fileList)) {
      if (!ATTACH_EXTS.includes(extOf(f.name))) {
        setAttachError(`不支持的格式：${f.name}（支持 ${ATTACH_EXTS.join('/')}）`);
        continue;
      }
      next.push({ fileName: f.name, srcPath: `local://${f.name}`, file: f, kind: 'file' });
    }
    pushAttachments(next);
  }, [pushAttachments]);

  /** 浏览器：真实目录读取回调（webkitdirectory 返回目录内全部文件） */
  const onFolderPicked = useCallback((fileList: FileList | null) => {
    if (!fileList || fileList.length === 0) return;
    const files = Array.from(fileList);
    const folderName = (files[0] as File & { webkitRelativePath?: string }).webkitRelativePath?.split('/')[0] ?? '本地文件夹';
    const next: PendingAttachment[] = [];
    for (const f of files) {
      if (!ATTACH_EXTS.includes(extOf(f.name))) continue;
      const rel = (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name;
      next.push({ fileName: rel, srcPath: `local://${rel}`, file: f, kind: 'file' });
    }
    if (next.length === 0) {
      setAttachError(`「${folderName}」内没有可附加的文件（支持 ${ATTACH_EXTS.join('/')}）`);
      return;
    }
    pushAttachments(next);
  }, [pushAttachments]);

  const removeAttachment = useCallback((idx: number) => {
    setAttachments((prev) => prev.filter((_, i) => i !== idx));
  }, []);

  const handleSend = useCallback(async () => {
    const trimmed = value.trim();
    if (!trimmed || loading || staging) return;
    // 附件暂存：浏览器真实文件读内容上传对话接口；Tauri 路径逐个 file_stage_attachment，失败卡片标红可重试
    const metas: AttachmentMeta[] = [];
    if (attachments.length > 0) {
      setStaging(true);
      const apiObj = (window as unknown as { tongtianAPI?: { fileStageAttachment?: (src: string, taskId?: string, ws?: string) => Promise<AttachmentMeta> } }).tongtianAPI;
      const updated: PendingAttachment[] = [];
      for (const att of attachments) {
        if (att.staged) {
          metas.push(att.staged);
          updated.push(att);
          continue;
        }
        try {
          let meta: AttachmentMeta;
          if (att.file) {
            // 真实文件：读取内容 → base64 → 上传到对话接口（引擎落盘后返回 AttachmentMeta）
            const dataBase64 = await readFileAsBase64(att.file);
            meta = await chatAPI.fileUpload({
              fileName: att.file.name,
              dataBase64,
              mime: att.file.type || undefined,
            });
          } else {
            meta = apiObj?.fileStageAttachment
              ? await apiObj.fileStageAttachment(att.srcPath)
              : { fileName: att.fileName, stagedPath: att.srcPath, sizeBytes: 0, mime: 'application/octet-stream', sensitivity: 'public' };
          }
          metas.push(meta);
          updated.push({ ...att, staged: meta, error: undefined });
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e);
          updated.push({ ...att, error: msg });
          setAttachError(`附件上传失败：${att.fileName}（${msg}）`);
        }
      }
      setAttachments(updated);
      setStaging(false);
      if (updated.some((a) => a.error)) return; // 存在失败项：不提交，等待重试/移除
    }
    onSend(
      referencedSkill ? `/${referencedSkill.name} ${trimmed}` : trimmed,
      metas.length > 0 ? metas : undefined,
      selectedModel.modelId || undefined,
    );
    setValue('');
    setAttachments([]);
    setAttachError(null);
    setReferencedSkill(null);
    setTimeout(() => inputRef.current?.focus(), 50);
  }, [value, loading, staging, attachments, referencedSkill, selectedModel, onSend]);

  /** 选择工作目录：Tauri 走系统目录对话框；浏览器用 File System Access API */
  const handlePickWorkDir = useCallback(async () => {
    try {
      if (isTauri()) {
        const { open } = await import('@tauri-apps/plugin-dialog');
        const picked = await open({ directory: true });
        if (typeof picked === 'string' && picked) onSelectWorkDir?.(picked);
        return;
      }
      const picker = (window as unknown as { showDirectoryPicker?: () => Promise<{ name: string }> }).showDirectoryPicker;
      if (picker) {
        const dir = await picker.call(window);
        if (dir?.name) onSelectWorkDir?.(dir.name);
      }
    } catch {
      // 用户取消选择，忽略
    }
  }, [onSelectWorkDir]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    },
    [handleSend],
  );

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const canSend = value.trim().length > 0 && !loading;

  return (
    <Box>
    <Box
      sx={{
        bgcolor: '#FFFFFF',
        borderRadius: '20px',
        border: `1px solid ${focused ? '#14B8A6' : '#E5E7EB'}`,
        boxShadow: focused
          ? '0 0 0 3px rgba(20, 184, 166, 0.12), 0 8px 30px rgba(15, 23, 42, 0.06)'
          : '0 2px 12px rgba(15, 23, 42, 0.04)',
        transition: 'border-color 0.2s ease, box-shadow 0.2s ease',
        px: 2,
        pt: 1.75,
        pb: 1.25,
      }}
    >
      {/* 引用块区：附件卡片（输入框上方；技能引用块已改为输入框内嵌） */}
      {attachments.length > 0 && (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75, mb: 1 }}>
          {attachments.map((att, idx) => (
            <Chip
              key={`${att.fileName}-${idx}`}
              size="small"
              icon={att.kind === 'folder' ? <FolderOutlinedIcon sx={{ fontSize: 14 }} /> : <AttachFileIcon sx={{ fontSize: 14 }} />}
              label={att.error ? `${att.fileName}（失败）` : att.fileName}
              color={att.error ? 'error' : 'default'}
              onDelete={() => removeAttachment(idx)}
              sx={{ fontSize: '12px', maxWidth: 260 }}
            />
          ))}
        </Box>
      )}
      {attachError && (
        <Alert severity="error" sx={{ mb: 1, py: 0, fontSize: '12px' }} onClose={() => setAttachError(null)}>
          {attachError}
        </Alert>
      )}

      {/* 文本输入（/ 前缀实时唤起技能选择；技能引用块与文字同行内联） */}
      <Box ref={inputWrapRef}>
        <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 0.75 }}>
          {referencedSkill && (() => {
            const active = Boolean(skillBlockAnchor); // 点击展开时才变色
            const installed = referencedSkill.source === 'installed';
            return (
              <Box
                onClick={(e) => setSkillBlockAnchor(active ? null : e.currentTarget)}
                sx={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 0.5,
                  mt: '3px',
                  px: 1,
                  py: 0.4,
                  borderRadius: '8px',
                  cursor: 'pointer',
                  flexShrink: 0,
                  background: active
                    ? installed ? '#E2D5FB' : '#C7EFDC'
                    : installed ? '#F1EBFF' : '#E7F8EF',
                  border: `1px solid ${active
                    ? installed ? 'rgba(139, 92, 246, 0.55)' : 'rgba(16, 185, 129, 0.55)'
                    : installed ? 'rgba(139, 92, 246, 0.28)' : 'rgba(16, 185, 129, 0.28)'}`,
                  transition: 'background 0.15s ease, border-color 0.15s ease',
                }}
              >
                <Box
                  sx={{
                    width: 14,
                    height: 14,
                    borderRadius: '4px',
                    background: installed ? '#8B5CF6' : '#10B981',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                  }}
                >
                  <Typography sx={{ fontSize: '9px', fontWeight: 800, color: '#fff', lineHeight: 1 }}>T</Typography>
                </Box>
                <Typography
                  sx={{
                    fontSize: '12.5px',
                    fontWeight: 700,
                    whiteSpace: 'nowrap',
                    color: installed ? '#6D28D9' : '#047857',
                  }}
                >
                  {referencedSkill.name}
                </Typography>
                <KeyboardArrowDownIcon
                  sx={{
                    fontSize: 14,
                    color: installed ? '#8B5CF6' : '#10B981',
                    transform: active ? 'rotate(180deg)' : 'none',
                    transition: 'transform 0.2s ease',
                  }}
                />
              </Box>
            );
          })()}
          <TextField
          inputRef={inputRef}
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            const v = e.target.value;
            if (v.startsWith('/')) {
              // / 后第一个 token 作为技能筛选词
              setSkillFilter(v.slice(1).split(/\s/)[0] ?? '');
              setSkillsAnchor((prev) => prev ?? inputWrapRef.current);
            } else {
              setSkillsAnchor(null);
              setSkillFilter('');
            }
          }}
          onKeyDown={handleKeyDown}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          placeholder={loading ? 'AI 正在处理中…' : placeholder}
          disabled={loading}
          multiline
          minRows={1}
          maxRows={maxRows}
          fullWidth
          variant="standard"
          InputProps={{
            disableUnderline: true,
            sx: {
              fontSize: '15px',
              lineHeight: 1.6,
              px: 0.5,
              color: '#111827',
              '&::placeholder': { color: '#9CA3AF' },
            },
          }}
          sx={{ flex: 1, minWidth: 0 }}
          />
        </Box>
      </Box>

      {/* 工具栏（等比缩小 + 窄屏自动换行，防止溢出卡片） */}
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          flexWrap: 'wrap',
          gap: 0.75,
          mt: 1.25,
        }}
      >
        {/* 左侧：附加操作 + 模式选择 */}
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.25 }}>
          <Tooltip title="添加内容（文件 / 文件夹 / 技能）">
            <IconButton
              size="small"
              sx={{ color: '#6B7280', width: 28, height: 28 }}
              onClick={(e) => setAddContentAnchor(e.currentTarget)}
            >
              <AddIcon sx={{ fontSize: 17 }} />
            </IconButton>
          </Tooltip>

          {/* 「通用」模式选择 */}
          <Box
            onClick={(e) => setModeAnchor(e.currentTarget)}
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 0.25,
              ml: 0.5,
              px: 1,
              py: 0.4,
              borderRadius: '999px',
              border: '1px solid #E5E7EB',
              cursor: 'pointer',
              transition: 'all 0.15s ease',
              '&:hover': { borderColor: '#14B8A6', background: 'rgba(20, 184, 166, 0.04)' },
            }}
          >
            <Typography sx={{ fontSize: '12px', color: '#374151', fontWeight: 500 }}>{mode}</Typography>
            <KeyboardArrowDownIcon sx={{ fontSize: 15, color: '#9CA3AF' }} />
          </Box>
        </Box>

        {/* 右侧：选择模型 + 麦克风 + 发送 */}
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, flexShrink: 0 }}>
          {/* 选择模型（原「添加到队列」占位已替换为模型选择能力） */}
          <Tooltip title="选择模型">
            <Box
              onClick={(e) => setModelAnchor(e.currentTarget)}
              sx={{
                display: 'flex',
                alignItems: 'center',
                gap: 0.5,
                px: 1,
                py: 0.4,
                borderRadius: '999px',
                border: '1px solid #E5E7EB',
                cursor: 'pointer',
                transition: 'all 0.15s ease',
                '&:hover': { borderColor: '#8B5CF6', background: 'rgba(139, 92, 246, 0.04)' },
              }}
            >
              <Box
                sx={{
                  width: 7,
                  height: 7,
                  borderRadius: '50%',
                  background: selectedModel.color,
                  flexShrink: 0,
                }}
              />
              <Typography sx={{ fontSize: '12px', fontWeight: 500, color: '#374151' }}>{selectedModel.name}</Typography>
              <KeyboardArrowDownIcon sx={{ fontSize: 15, color: '#9CA3AF' }} />
            </Box>
          </Tooltip>

          {/* 语音输入 */}
          <Tooltip title="语音输入">
            <IconButton size="small" sx={{ color: '#6B7280', width: 30, height: 30 }}>
              <MicIcon sx={{ fontSize: 18 }} />
            </IconButton>
          </Tooltip>

          {/* 发送按钮 */}
          <Tooltip title={canSend ? '发送 (Enter)' : loading ? '生成中…' : '请输入内容'}>
            <span>
              <IconButton
                onClick={handleSend}
                disabled={!canSend && !loading}
                sx={{
                  width: 34,
                  height: 34,
                  borderRadius: '50%',
                  bgcolor: canSend ? '#14B8A6' : loading ? '#F59E0B' : '#E5E7EB',
                  color: canSend || loading ? '#fff' : '#9CA3AF',
                  boxShadow: canSend ? '0 2px 8px rgba(20, 184, 166, 0.35)' : 'none',
                  transition: 'all 0.2s ease',
                  '&:hover': {
                    bgcolor: canSend ? '#0D9488' : loading ? '#D97706' : '#E5E7EB',
                  },
                  '&.Mui-disabled': {
                    bgcolor: loading ? '#F59E0B' : '#E5E7EB',
                    color: loading ? '#fff' : '#9CA3AF',
                  },
                }}
              >
                {loading ? <StopIcon sx={{ fontSize: 18 }} /> : <ArrowUpwardIcon sx={{ fontSize: 18 }} />}
              </IconButton>
            </span>
          </Tooltip>
        </Box>
      </Box>

      {/* 技能引用块底层详情（点击内嵌引用块展开） */}
      {referencedSkill && (
        <Popover
          open={Boolean(skillBlockAnchor)}
          anchorEl={skillBlockAnchor}
          onClose={() => setSkillBlockAnchor(null)}
          anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
          transformOrigin={{ vertical: 'top', horizontal: 'left' }}
          PaperProps={{
            sx: {
              width: 280,
              mt: 0.5,
              borderRadius: '12px',
              border: '1px solid rgba(99, 102, 241, 0.10)',
              boxShadow: '0 10px 32px rgba(30, 41, 59, 0.14)',
              p: 1.5,
            },
          }}
        >
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <Box
              sx={{
                width: 28,
                height: 28,
                borderRadius: '8px',
                background: referencedSkill.source === 'installed' ? '#F1EBFF' : '#E7F8EF',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
              }}
            >
              <AutoAwesomeIcon
                sx={{ fontSize: 15, color: referencedSkill.source === 'installed' ? '#8B5CF6' : '#10B981' }}
              />
            </Box>
            <Box sx={{ flexGrow: 1, minWidth: 0 }}>
              <Typography sx={{ fontSize: '13.5px', fontWeight: 700, color: '#111827' }}>
                /{referencedSkill.name}
              </Typography>
              <Typography sx={{ fontSize: '11px', color: '#9CA3AF' }}>
                {referencedSkill.source === 'installed' ? '已安装技能' : '内置技能'}
              </Typography>
            </Box>
          </Box>
          {referencedSkill.description && (
            <Typography sx={{ fontSize: '12px', color: '#6B7280', mt: 1, lineHeight: 1.6 }}>
              {referencedSkill.description}
            </Typography>
          )}
          <Box sx={{ display: 'flex', gap: 1, mt: 1.5 }}>
            <Box
              onClick={() => {
                setSkillBlockAnchor(null);
                setSkillFilter('');
                setSkillsAnchor(inputWrapRef.current);
              }}
              sx={{
                flex: 1,
                textAlign: 'center',
                py: 0.6,
                borderRadius: '8px',
                border: '1px solid #E5E7EB',
                cursor: 'pointer',
                transition: 'all 0.15s ease',
                '&:hover': { borderColor: '#8B5CF6', color: '#8B5CF6' },
              }}
            >
              <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#374151' }}>重新选择</Typography>
            </Box>
            <Box
              onClick={() => {
                setSkillBlockAnchor(null);
                setReferencedSkill(null);
              }}
              sx={{
                flex: 1,
                textAlign: 'center',
                py: 0.6,
                borderRadius: '8px',
                border: '1px solid rgba(239, 68, 68, 0.3)',
                cursor: 'pointer',
                transition: 'all 0.15s ease',
                '&:hover': { background: 'rgba(239, 68, 68, 0.05)' },
              }}
            >
              <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#EF4444' }}>取消引用</Typography>
            </Box>
          </Box>
        </Popover>
      )}

      {/* 技能选择菜单：已安装 + 内置技能，选中生成内嵌引用块 */}
      <Menu
        anchorEl={skillsAnchor}
        open={Boolean(skillsAnchor)}
        onClose={() => {
          setSkillsAnchor(null);
          setSkillFilter('');
        }}
        anchorOrigin={{ vertical: 'top', horizontal: 'left' }}
        transformOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        PaperProps={{
          sx: {
            width: 300,
            maxHeight: 360,
            mt: -0.5,
            borderRadius: '14px',
            border: '1px solid rgba(99, 102, 241, 0.10)',
            boxShadow: '0 12px 40px rgba(30, 41, 59, 0.14)',
            p: 0.75,
          },
        }}
      >
        <Typography sx={{ fontSize: '11px', fontWeight: 600, color: '#9CA3AF', px: 1.25, py: 0.5 }}>
          选择技能（{filteredSkills.length}）
        </Typography>
        {filteredSkills.length === 0 && (
          <Typography sx={{ fontSize: '12.5px', color: '#9CA3AF', px: 1.25, py: 1.5, textAlign: 'center' }}>
            未找到匹配技能，可到扩展页安装
          </Typography>
        )}
        {filteredSkills.map((s) => (
          <Box
            key={s.id}
            onClick={() => pickSkill(s)}
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 1,
              px: 1.25,
              py: 0.9,
              borderRadius: '10px',
              cursor: 'pointer',
              transition: 'background 0.15s ease',
              '&:hover': { background: 'rgba(99, 102, 241, 0.06)' },
            }}
          >
            <Box
              sx={{
                width: 26,
                height: 26,
                borderRadius: '8px',
                background: s.source === 'installed' ? '#F1EBFF' : '#E7F8EF',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
              }}
            >
              <AutoAwesomeIcon sx={{ fontSize: 14, color: s.source === 'installed' ? '#8B5CF6' : '#10B981' }} />
            </Box>
            <Box sx={{ flexGrow: 1, minWidth: 0 }}>
              <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#1F2937' }}>/{s.name}</Typography>
              {s.description && (
                <Typography
                  sx={{
                    fontSize: '11px',
                    color: '#9CA3AF',
                    mt: 0.15,
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: 'nowrap',
                  }}
                >
                  {s.description}
                </Typography>
              )}
            </Box>
            <Box
              sx={{
                px: 0.75,
                py: 0.2,
                borderRadius: '6px',
                flexShrink: 0,
                background: s.source === 'installed' ? '#F1EBFF' : '#E7F8EF',
              }}
            >
              <Typography sx={{ fontSize: '10px', fontWeight: 600, color: s.source === 'installed' ? '#8B5CF6' : '#059669' }}>
                {s.source === 'installed' ? '已安装' : '内置'}
              </Typography>
            </Box>
          </Box>
        ))}
      </Menu>

      {/* 模型选择菜单：内置模型 + 添加模型入口（跳设置页模型管理） */}
      <Menu
        anchorEl={modelAnchor}
        open={Boolean(modelAnchor)}
        onClose={() => setModelAnchor(null)}
        anchorOrigin={{ vertical: 'top', horizontal: 'right' }}
        transformOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        PaperProps={{
          sx: {
            width: 280,
            mt: -0.5,
            borderRadius: '14px',
            border: '1px solid rgba(99, 102, 241, 0.10)',
            boxShadow: '0 12px 40px rgba(30, 41, 59, 0.14)',
            p: 0.75,
          },
        }}
      >
        <Typography sx={{ fontSize: '11px', fontWeight: 600, color: '#9CA3AF', px: 1.25, py: 0.5 }}>
          内置模型
        </Typography>
        {BUILTIN_MODELS.map((m) => (
          <Box
            key={m.id}
            onClick={() => {
              setSelectedModel(m);
              setModelAnchor(null);
            }}
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 1,
              px: 1.25,
              py: 0.9,
              borderRadius: '10px',
              cursor: 'pointer',
              transition: 'background 0.15s ease',
              '&:hover': { background: 'rgba(99, 102, 241, 0.06)' },
            }}
          >
            <Box
              sx={{
                width: 26,
                height: 26,
                borderRadius: '8px',
                background: `${m.color}1A`,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
              }}
            >
              <Typography sx={{ fontSize: '12px', fontWeight: 700, color: m.color }}>{m.name[0]}</Typography>
            </Box>
            <Box sx={{ flexGrow: 1, minWidth: 0 }}>
              <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#1F2937' }}>{m.name}</Typography>
              <Typography sx={{ fontSize: '11px', color: '#9CA3AF', mt: 0.15 }}>{m.desc}</Typography>
            </Box>
            {selectedModel.id === m.id && <CheckIcon sx={{ fontSize: 16, color: '#8B5CF6' }} />}
          </Box>
        ))}

        <Box sx={{ borderTop: '1px solid #F3F4F6', mt: 0.5, pt: 0.5 }}>
          <Box
            onClick={() => {
              setModelAnchor(null);
              navigate('/settings?s=models');
            }}
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 1,
              px: 1.25,
              py: 0.9,
              borderRadius: '10px',
              cursor: 'pointer',
              transition: 'background 0.15s ease',
              '&:hover': { background: 'rgba(99, 102, 241, 0.06)' },
            }}
          >
            <Box
              sx={{
                width: 26,
                height: 26,
                borderRadius: '8px',
                background: '#F3F4F6',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
              }}
            >
              <AddIcon sx={{ fontSize: 15, color: '#6B7280' }} />
            </Box>
            <Typography sx={{ fontSize: '13px', fontWeight: 500, color: '#374151' }}>添加模型</Typography>
          </Box>
        </Box>
      </Menu>

      {/* 添加内容菜单：文件 / 文件夹（真实读取本地内容） */}
      <Menu
        anchorEl={addContentAnchor}
        open={Boolean(addContentAnchor)}
        onClose={() => setAddContentAnchor(null)}
        anchorOrigin={{ vertical: 'top', horizontal: 'left' }}
        transformOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        PaperProps={{ sx: { minWidth: 160, mt: -0.5 } }}
      >
        <MenuItem
          onClick={() => {
            setAddContentAnchor(null);
            handleAddFiles();
          }}
          sx={{ fontSize: '13px', gap: 1 }}
        >
          <AttachFileIcon sx={{ fontSize: 16, color: '#6B7280' }} /> 添加文件
        </MenuItem>
        <MenuItem
          onClick={() => {
            setAddContentAnchor(null);
            handleAddFolder();
          }}
          sx={{ fontSize: '13px', gap: 1 }}
        >
          <FolderOutlinedIcon sx={{ fontSize: 16, color: '#6B7280' }} /> 添加文件夹
        </MenuItem>
        <MenuItem
          onClick={() => {
            setAddContentAnchor(null);
            setSkillFilter('');
            setSkillsAnchor(inputWrapRef.current);
          }}
          sx={{ fontSize: '13px', gap: 1 }}
        >
          <AutoAwesomeIcon sx={{ fontSize: 16, color: '#6B7280' }} /> 调用技能
        </MenuItem>
      </Menu>

      {/* 浏览器模式：真实文件/文件夹选择器（webkitdirectory 读取目录全部内容） */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept={ATTACH_EXTS.map((e) => `.${e}`).join(',')}
        hidden
        onChange={(e) => {
          onFilesPicked(e.target.files);
          e.target.value = '';
        }}
      />
      <input
        ref={folderInputRef}
        type="file"
        multiple
        hidden
        {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
        onChange={(e) => {
          onFolderPicked(e.target.files);
          e.target.value = '';
        }}
      />

      {/* 模式选择菜单 */}
      <Menu
        anchorEl={modeAnchor}
        open={Boolean(modeAnchor)}
        onClose={() => setModeAnchor(null)}
        anchorOrigin={{ vertical: 'top', horizontal: 'left' }}
        transformOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        PaperProps={{ sx: { minWidth: 140, mt: -0.5 } }}
      >
        {modeOptions.map((opt) => (
          <MenuItem
            key={opt}
            selected={opt === mode}
            onClick={() => {
              modeTouched.current = true;
              setMode(opt);
              setModeAnchor(null);
            }}
            sx={{ fontSize: '13px' }}
          >
            {opt}
          </MenuItem>
        ))}
      </Menu>
    </Box>

          {/* 输入框下方：选择工作目录（真实目录） + 免责提示 */}
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mt: 0.75, px: 0.5 }}>
        <Tooltip title={workDir || '选择会话内容与产出物的存放目录'} placement="top">
          <Box
            onClick={handlePickWorkDir}
            sx={{
              display: 'flex',
              alignItems: 'center',
              gap: 0.5,
              px: 0.75,
              py: 0.35,
              borderRadius: '8px',
              maxWidth: 300,
              cursor: 'pointer',
              transition: 'background 0.15s ease',
              '&:hover': { background: 'rgba(15, 23, 42, 0.04)' },
            }}
          >
            <FolderOutlinedIcon sx={{ fontSize: 14, color: workDir ? '#4F46E5' : '#9CA3AF' }} />
            <Typography
              sx={{
                fontSize: '12px',
                color: workDir ? '#4F46E5' : '#9CA3AF',
                fontWeight: workDir ? 600 : 400,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {workDir ? workDir.split(/[\\/]/).filter(Boolean).slice(-2).join('/') : '选择工作目录'}
            </Typography>
          </Box>
        </Tooltip>
        <Typography sx={{ fontSize: '12px', color: '#C0C4CA' }}>内容由 AI 生成，请注意核实</Typography>
      </Box>
    </Box>
  );
};

export default ChatInput;
