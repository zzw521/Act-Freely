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
  CircularProgress,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogContentText,
  DialogActions,
  Button,
} from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import AttachFileIcon from '@mui/icons-material/AttachFile';
import FolderOutlinedIcon from '@mui/icons-material/FolderOutlined';
import ShieldOutlinedIcon from '@mui/icons-material/ShieldOutlined';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import MicIcon from '@mui/icons-material/Mic';
import ArrowUpwardIcon from '@mui/icons-material/ArrowUpward';
import StopIcon from '@mui/icons-material/Stop';
import CheckIcon from '@mui/icons-material/Check';
import GroupsIcon from '@mui/icons-material/Groups';
import type { AttachmentMeta, FilePermissionMode, ModelConfig, WorkMode } from '../../../shared/types';
import type { ExpertInfo } from '../../../shared/contracts';
import { isTauri } from '../../lib/tauri-api';
import { getActiveWorkspaceId } from '../../store/workspaceStore';
import { chatAPI, expertsAPI } from '../../lib/api';
import {
  BUILTIN_CHAT_MODELS,
  loadChatModels,
  modelSourceLabel,
  type BuiltinChatModel,
} from '../../lib/chat-models';
import { loadAllSkills, SKILL_SOURCE_META, type SkillInfo } from '../../lib/skills';
import { useAppSettings } from '../../lib/use-app-settings';
import { useWorkMode, MODE_PLACEHOLDERS } from '../../lib/use-work-mode';
import {
  FILE_PERMISSION_DEFS,
  DEFAULT_FILE_PERMISSION,
  filePermissionDef,
  normalizeFileMode,
} from '../../lib/file-permission';
import { usePptxTemplates } from '../../lib/use-pptx-templates';
import { useWorkbenchStore } from '../../store/workbenchStore';
import { useI18n } from '../../i18n';
import Panda from '../Panda';
import { useNavigate } from 'react-router-dom';
import { errorTextOf } from '../../../shared/error-messages';
import { anchorMenuProps, clickableProps } from '../../lib/a11y';

interface ChatInputProps {
  /** 发送消息回调（两阶段协议：附件暂存后随提交上送；modelBinding 为内置档位；
   *  modelId 为已保存模型 id，端点与密钥由持有数据源的后端解析；
   *  expertId 为本轮负责人专家，专家绑定的模型优先于 modelBinding/modelId） */
  onSend: (
    text: string,
    attachments?: AttachmentMeta[],
    modelBinding?: string,
    modelId?: string,
    mode?: WorkMode,
    expertId?: string,
  ) => void;
  /** 是否处于加载状态（思考/流式生成中） */
  loading?: boolean;
  /** 任务是否可停止（排队/规划/执行中）：为真时主按钮变方形停止键，与发送共用同一个位置 */
  stoppable?: boolean;
  /** 停止当前任务 */
  onStop?: () => void;
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
  /** 本轮文件访问档位：AI 在真实目录里创建/更新/删除文件放开到哪一档 */
  filePermission?: FilePermissionMode;
  /** 切换档位回调；切到「完全访问」时会带上用户读完红色警告并确认的时刻 */
  onFilePermissionChange?: (mode: FilePermissionMode, confirmedAtMs?: number) => void;
  /** 对话态紧凑模式：收起提示词建议，避免和消息流争夺空间 */
  compact?: boolean;
  /** 空态主视觉：坐在输入卡上沿、下半身被卡片遮住的吉祥物（不参与点击） */
  mascot?: React.ReactNode;
}

// 附件约束（交互补设计 §3.3；含图片格式，原「插入图片」已合并到添加文件）
const ATTACH_EXTS = ['docx', 'xlsx', 'pdf', 'csv', 'txt', 'md', 'png', 'jpg', 'jpeg', 'gif', 'webp'];
const ATTACH_MAX_COUNT = 10;
// 与 Rust 暂存上限一致：图片 10MB（要转 base64 随请求下发），文档 50MB
const ATTACH_MAX_BYTES = 50 * 1024 * 1024;
const IMAGE_MAX_BYTES = 10 * 1024 * 1024;
const IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp'];

/** 超大文件就地拒绝，避免整份读入内存后才失败 */
function oversizedLabel(name: string, size: number): string | null {
  const limit = IMAGE_EXTS.includes(extOf(name)) ? IMAGE_MAX_BYTES : ATTACH_MAX_BYTES;
  if (size <= limit) return null;
  return `${name}（超出 ${Math.round(limit / (1024 * 1024))}MB 上限）`;
}

/** 文件名 → 扩展名 */
function extOf(name: string): string {
  const i = name.lastIndexOf('.');
  return i >= 0 ? name.slice(i + 1).toLowerCase() : '';
}

// 模式选项来自设置页「工作台」分区（启用模式 + 默认模式，见 lib/use-app-settings）

/**
 * 空输入时的起步建议（设置页「提示词建议」的真实消费方）。
 * 只回填输入框、不直接发送：用户仍可改词后再提交。
 */
const PROMPT_SUGGESTIONS: Record<WorkMode, string[]> = {
  general: ['帮我总结这份文档的要点', '把这段内容整理成可执行的待办清单', '对比两个方案并给出选择建议'],
  design: ['做一个产品落地页原型', '设计一个数据看板页面', '把这张截图还原成可交互 HTML'],
  slides: ['把这份材料做成 8 页汇报幻灯片', '生成一份季度复盘 PPT', '做一份产品发布会演示稿'],
  writing: ['把这篇文章改得更简洁', '写一份本周项目周报', '润色这段产品介绍'],
};

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

// 内置模型（对话模型选择；binding 为提交时绑定给引擎的真实模型名）
// 档位只描述能力定位，不标价格倍率：倍率会随百炼计费调整而变化，写死在界面上就是编数字
/** 模型选项（内置档位 + 数据源中的已保存模型） */
type ModelOption = BuiltinChatModel & {
  /** 已保存模型 id（提交只传 id，密钥不出渲染层） */
  savedId?: string;
  /** 仅用于提交前的合规出域判定 */
  baseUrl?: string;
};

const BUILTIN_MODELS: ModelOption[] = BUILTIN_CHAT_MODELS;

const ChatInput: React.FC<ChatInputProps> = ({
  onSend,
  loading = false,
  stoppable = false,
  onStop,
  placeholder = '描述任务，输入 /调用技能',
  maxRows = 8,
  initialText,
  workDir,
  onSelectWorkDir,
  filePermission,
  onFilePermissionChange,
  compact = false,
  mascot,
}) => {
  const { t: tr } = useI18n();
  const [value, setValue] = useState(initialText ?? '');
  const [focused, setFocused] = useState(false);
  // 模式放 store：欢迎页与对话态各挂一个 ChatInput，首轮发送后组件重挂，
  // 实例 state 里的选择会被无声重置回默认模式（与模型选择同一类陷阱）
  const { activeMode, enabledDefs: enabledModeDefs, setWorkMode } = useWorkMode();
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const navigate = useNavigate();

  // 幻灯片模板：只在幻灯片模式下加载台账，避免每次挂载都扫模板目录。
  // 选择写回 app settings，桌面端由 Rust runtime 下发给引擎。
  const pptx = usePptxTemplates({ active: activeMode === 'slides' });
  const [pptxError, setPptxError] = useState('');
  const [pptxSwitching, setPptxSwitching] = useState(false);

  // ── 模型选择（替换原「添加到队列」占位） ──
  const [modelAnchor, setModelAnchor] = useState<null | HTMLElement>(null);
  // 选项存在 workbenchStore 而非本组件 state：欢迎页 → 对话态切换会让 ChatInput 重挂，
  // 实例内 state 的选择被无声重置回 Auto，后续轮次就不再按用户选的模型执行
  const chatModelId = useWorkbenchStore((s) => s.chatModelId);
  const setChatModelId = useWorkbenchStore((s) => s.setChatModelId);

  // 已保存模型（桌面端 SQLite / 浏览器 models.json / Mock 台账，经同一个 API 读取）。
  // 清单里没有明文密钥；选中后只把 id 交给后端解析端点与密钥。
  const [savedModels, setSavedModels] = useState<ModelConfig[]>([]);
  useEffect(() => {
    let cancelled = false;
    loadChatModels(true)
      .then((list) => {
        if (!cancelled) setSavedModels(list);
      })
      .catch(() => {
        // 读取失败时保留内置档位；不拿空清单冒充「没有已保存模型」
        if (!cancelled) setSavedModels([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const savedOptions: ModelOption[] = savedModels.map((m) => ({
    id: `saved-${m.id}`,
    name: m.name || m.modelName,
    desc: [tr(modelSourceLabel(m.source)), m.modelName, m.isDefault ? tr('默认') : '']
      .filter(Boolean)
      .join(' · '),
    color: 'var(--tt-info)',
    savedId: m.id,
    baseUrl: m.baseUrl?.trim() || undefined,
  }));

  // 清单异步加载，所选 id 不在清单时按 Auto 展示
  const selectedModel =
    [...BUILTIN_MODELS, ...savedOptions].find((m) => m.id === chatModelId) ?? BUILTIN_MODELS[0];

  // ── 负责人专家（注册表在桌面端 Rust；浏览器/Mock 读不到时不显示选择器） ──
  const [experts, setExperts] = useState<ExpertInfo[]>([]);
  const [expertAnchor, setExpertAnchor] = useState<null | HTMLElement>(null);
  // 与模型/模式同理放 store：欢迎页 → 对话态 ChatInput 重挂时选择不能丢
  const chatExpertId = useWorkbenchStore((s) => s.chatExpertId);
  const setChatExpertId = useWorkbenchStore((s) => s.setChatExpertId);
  useEffect(() => {
    let cancelled = false;
    expertsAPI
      .list()
      .then((list) => {
        if (cancelled) return;
        setExperts(list);
        // 选中的专家已被删除：清掉选择，避免下一轮提交必然 EXPERT_NOT_FOUND
        const current = useWorkbenchStore.getState().chatExpertId;
        if (current && !list.some((e) => e.id === current)) setChatExpertId('');
      })
      .catch(() => {
        // 桌面通道不可用时不展示专家选择器；不伪造「没有专家」的结论
        if (!cancelled) setExperts([]);
      });
    return () => {
      cancelled = true;
    };
  }, [setChatExpertId]);
  const selectedExpert = experts.find((e) => e.id === chatExpertId) ?? null;

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

  // 提示词建议开关（缺省开启）与当前模式的建议清单
  const [appSettings] = useAppSettings();
  const promptSuggestionEnabled = appSettings.promptSuggestion !== false;
  const suggestions = PROMPT_SUGGESTIONS[activeMode] ?? PROMPT_SUGGESTIONS.general;

  // ── 附件状态（交互补设计 §3 附件模式；支持真实文件/文件夹读取与上传） ──
  interface PendingAttachment {
    fileName: string;
    srcPath: string;
    staged?: AttachmentMeta;
    /** 文件夹附件的逐文件暂存结果（发送时展开成多个附件） */
    stagedList?: AttachmentMeta[];
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
      setAttachError(tr('单次最多 {count} 个附件', { count: ATTACH_MAX_COUNT }));
      return;
    }
    if (attachments.length + items.length > ATTACH_MAX_COUNT) {
      setAttachError(tr('单次最多 {count} 个附件，已截断', { count: ATTACH_MAX_COUNT }));
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
            setAttachError(tr('不支持的格式：{name}（支持 {exts}）', { name: fileName, exts: ATTACH_EXTS.join('/') }));
            continue;
          }
          next.push({ fileName, srcPath: p, kind: 'file' });
        }
        pushAttachments(next);
      } catch (e) {
        setAttachError(tr('打开文件对话框失败：{msg}', { msg: errorTextOf(e, tr('原因未知')) }));
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
          pushAttachments([{ fileName: tr('{folder}（文件夹）', { folder: dirName }), srcPath: picked, kind: 'folder' }]);
        }
      } catch (e) {
        setAttachError(tr('打开文件夹对话框失败：{msg}', { msg: errorTextOf(e, tr('原因未知')) }));
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
        setAttachError(tr('不支持的格式：{name}（支持 {exts}）', { name: f.name, exts: ATTACH_EXTS.join('/') }));
        continue;
      }
      const tooLarge = oversizedLabel(f.name, f.size);
      if (tooLarge) {
        setAttachError(tr('文件过大：{name}', { name: tooLarge }));
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
      const tooLarge = oversizedLabel(f.name, f.size);
      if (tooLarge) {
        setAttachError(tr('已跳过过大文件：{name}', { name: tooLarge }));
        continue;
      }
      const rel = (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name;
      next.push({ fileName: rel, srcPath: `local://${rel}`, file: f, kind: 'file' });
    }
    if (next.length === 0) {
      setAttachError(tr('「{folder}」内没有可附加的文件（支持 {exts}）', { folder: folderName, exts: ATTACH_EXTS.join('/') }));
      return;
    }
    pushAttachments(next);
  }, [pushAttachments]);

  const removeAttachment = useCallback((idx: number) => {
    setAttachments((prev) => prev.filter((_, i) => i !== idx));
  }, []);

  const handleSend = useCallback(async () => {
    const trimmed = value.trim();
    if (!trimmed || loading || stoppable || staging) return;
    // 附件暂存：浏览器真实文件读内容上传对话接口；Tauri 路径逐个 file_stage_attachment，失败卡片标红可重试
    const metas: AttachmentMeta[] = [];
    if (attachments.length > 0) {
      setStaging(true);
      const apiObj = (window as unknown as {
        tongtianAPI?: {
          fileStageAttachment?: (src: string, taskId?: string, ws?: string) => Promise<AttachmentMeta>;
          fileStageFolder?: (src: string, taskId?: string, ws?: string) => Promise<AttachmentMeta[]>;
        };
      }).tongtianAPI;
      // 同一批附件共用一个暂存目录，并带上当前工作空间：
      // 缺省会落到 default 工作空间，多工作空间下模型拿不到文件
      const batchId = `pending-${Date.now()}`;
      const workspaceId = getActiveWorkspaceId();
      const updated: PendingAttachment[] = [];
      for (const att of attachments) {
        if (att.stagedList) {
          metas.push(...att.stagedList);
          updated.push(att);
          continue;
        }
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
          } else if (att.kind === 'folder') {
            // 文件夹：Rust 递归复制受支持的文件，返回逐文件 meta
            if (!apiObj?.fileStageFolder) {
              throw new Error(tr('当前版本不支持文件夹附件（缺少暂存接口）'));
            }
            const list = await apiObj.fileStageFolder(att.srcPath, batchId, workspaceId);
            if (!Array.isArray(list) || list.length === 0) {
              throw new Error(tr('文件夹内没有可附加的文件'));
            }
            metas.push(...list);
            updated.push({ ...att, stagedList: list, error: undefined });
            continue;
          } else {
            if (!apiObj?.fileStageAttachment) {
              throw new Error(tr('当前版本不支持本地附件暂存（缺少暂存接口）'));
            }
            meta = await apiObj.fileStageAttachment(att.srcPath, batchId, workspaceId);
          }
          metas.push(meta);
          updated.push({ ...att, staged: meta, error: undefined });
        } catch (e) {
          const msg = errorTextOf(e, tr('上传未成功'));
          updated.push({ ...att, error: msg });
          setAttachError(tr('附件上传失败：{name}（{msg}）', { name: att.fileName, msg }));
        }
      }
      setAttachments(updated);
      setStaging(false);
      if (updated.some((a) => a.error)) return; // 存在失败项：不提交，等待重试/移除
    }
    onSend(
      referencedSkill ? `/${referencedSkill.name} ${trimmed}` : trimmed,
      metas.length > 0 ? metas : undefined,
      selectedModel.binding || undefined,
      selectedModel.savedId || undefined,
      activeMode,
      chatExpertId || undefined,
    );
    setValue('');
    setAttachments([]);
    setAttachError(null);
    setReferencedSkill(null);
    setTimeout(() => inputRef.current?.focus(), 50);
  }, [value, loading, stoppable, staging, attachments, referencedSkill, selectedModel, onSend, activeMode, chatExpertId]);

  /** 主按钮：空闲发送，任务运行中停止。两种动作放在同一个键位，图标随状态切换 */
  const handlePrimaryAction = useCallback(() => {
    if (stoppable) {
      onStop?.();
      return;
    }
    void handleSend();
  }, [stoppable, onStop, handleSend]);

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

  // ── 文件访问档位（真实目录里的创建 / 更新 / 删除） ──
  // 档位本体与每会话台账在 store + lib/file-permission 里；这里只负责选择与「完全访问」的红色确认。
  const [permAnchor, setPermAnchor] = useState<null | HTMLElement>(null);
  const [permPending, setPermPending] = useState<FilePermissionMode | null>(null);
  const permMode = normalizeFileMode(filePermission ?? DEFAULT_FILE_PERMISSION);
  const permDef = filePermissionDef(permMode);

  /** 选档：前两档直接生效；「完全访问」先弹红色警告，用户确认后才落 */
  const handlePickFilePermission = useCallback((mode: FilePermissionMode) => {
    setPermAnchor(null);
    if (mode === permMode) return;
    if (filePermissionDef(mode).danger) {
      setPermPending(mode);
      return;
    }
    onFilePermissionChange?.(mode);
  }, [permMode, onFilePermissionChange]);

  /** 确认放开边界：带上确认时刻，台账只认「读过警告」的完全访问 */
  const handleConfirmFullAccess = useCallback(() => {
    const mode = permPending ?? 'full_access';
    setPermPending(null);
    onFilePermissionChange?.(mode, Date.now());
  }, [permPending, onFilePermissionChange]);

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

  const canSend = value.trim().length > 0 && !loading && !stoppable;
  // 只在「空输入、无附件、未引用技能、非生成中」时给建议，避免和用户已写内容抢空间
  const showSuggestions =
    promptSuggestionEnabled &&
    !compact &&
    !loading &&
    value.trim() === '' &&
    attachments.length === 0 &&
    !referencedSkill;
  // 通用模式沿用调用方文案（欢迎区/对话态各自口径）；其他模式按模式给出真实预期
  const modePlaceholder = activeMode === 'general' ? placeholder : MODE_PLACEHOLDERS[activeMode];

  const handlePptxSelect = async (id: string) => {
    setPptxSwitching(true);
    setPptxError('');
    try {
      await pptx.select(id);
    } catch (e) {
      setPptxError(errorTextOf(e, '切换幻灯片模板失败'));
    } finally {
      setPptxSwitching(false);
    }
  };
  const pptxOptions = pptx.templates.filter((t) => t.available);

  return (
    <Box>
    {/* 工作台模式胶囊组：只在新任务（空态）出现；建立任务后输入框不再展示模式选择 */}
    {!compact && enabledModeDefs.length > 1 && (
      <Box
        role="group"
        aria-label={tr('工作台模式')}
        sx={{ position: 'relative', zIndex: 2, display: 'flex', flexWrap: 'wrap', gap: 0.75, mb: 1, px: 0.25 }}
      >
        {enabledModeDefs.map((d) => {
          const selected = d.id === activeMode;
          return (
            <Tooltip key={d.id} title={tr(d.desc)}>
              <Chip
                data-mode={d.id}
                label={tr(d.name)}
                size="small"
                onClick={() => setWorkMode(d.id as WorkMode)}
                aria-pressed={selected}
                sx={{
                  height: 26,
                  borderRadius: '999px',
                  fontSize: '12px',
                  fontWeight: selected ? 600 : 500,
                  bgcolor: selected ? 'var(--tt-teal-fill)' : 'transparent',
                  color: selected ? '#fff' : 'var(--tt-text-3)',
                  border: `1px solid ${selected ? 'var(--tt-teal-fill)' : 'var(--tt-border)'}`,
                  transition: 'all 0.15s ease',
                  '&:hover': {
                    bgcolor: selected ? 'var(--tt-teal-fill)' : 'var(--tt-surface-3)',
                    borderColor: 'var(--tt-teal)',
                  },
                  '& .MuiChip-label': { px: 1.25 },
                }}
              />
            </Tooltip>
          );
        })}
      </Box>
    )}
    {/* 幻灯片模式模板选择已移到输入框下方的目录行之后 */}
    <Box sx={{ position: 'relative' }}>
    {/* 吉祥物：露出头部与上半身，下半身被不透明输入卡遮住 */}
    {mascot && (
      <Box
        aria-hidden="true"
        sx={{
          position: 'absolute',
          left: '50%',
          bottom: 'calc(100% - 62px)',
          transform: 'translateX(-50%)',
          zIndex: 0,
          pointerEvents: 'none',
          userSelect: 'none',
        }}
      >
        {mascot}
      </Box>
    )}
    <Box
      sx={{
        position: 'relative',
        zIndex: 1,
        bgcolor: 'var(--tt-surface)',
        borderRadius: '24px',
        border: `1px solid ${focused ? 'var(--tt-teal)' : '#E5E7EB'}`,
        // 焦点信号画在卡片自己身上（内层 textarea 那条全局环由下方输入框外层 Box 的
        // data-tt-ring="container" 点名排除）：
        // Chromium 对文本控件一律命中 :focus-visible，全局那条环落在卡片里面就是「框中框」。
        // 这里不挂 --tt-focus 的蓝环（跟卡片自己的青绿打架），改成贴着边沿长实心一圈
        // --tt-teal：随皮肤翻，实算对页面底亮 5.15:1（#0f766e vs #f7f8fa）、暗 12.07:1
        //（#5eead4 vs #0f172a），都 ≥3:1（1.4.11）；原先写死的 #14B8A6 只有 2.49:1。
        boxShadow: focused
          ? '0 0 0 2px var(--tt-teal), 0 12px 36px rgba(15, 23, 42, 0.08)'
          : '0 6px 24px rgba(15, 23, 42, 0.05)',
        transition: 'border-color 0.2s ease, box-shadow 0.2s ease',
        px: 2,
        pt: 1.5,
        pb: 1.1,
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
              label={att.error ? tr('{name}（失败）', { name: att.fileName }) : att.fileName}
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
          {/* 状态灯：空闲眨眼、思考托腮、任务运行中戴安全帽。表情只由 props 派生，不落任何可持久化状态 */}
          <Box sx={{ width: 22, height: 22, flexShrink: 0, mt: '3px' }}>
            <Panda size={22} state={stoppable ? 'working' : loading ? 'thinking' : 'idle'} />
          </Box>
          {referencedSkill && (() => {
            const active = Boolean(skillBlockAnchor); // 点击展开时才变色
            const meta = SKILL_SOURCE_META[referencedSkill.source];
            return (
              <Box
                {...anchorMenuProps(
                  (target) => setSkillBlockAnchor(active ? null : target),
                  tr('引用技能：{name}', { name: referencedSkill.name }),
                  active,
                )}
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
                  background: meta.bg,
                  border: `1px solid ${active ? meta.activeBorder : meta.border}`,
                  transition: 'background 0.15s ease, border-color 0.15s ease',
                }}
              >
                <Box
                  sx={{
                    width: 14,
                    height: 14,
                    borderRadius: '4px',
                    background: meta.solid,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                  }}
                >
                  <Typography sx={{ fontSize: '9px', fontWeight: 800, color: '#fff', lineHeight: 1 }}>
                    {referencedSkill.source === 'suite' ? 'S' : 'T'}
                  </Typography>
                </Box>
                <Typography
                  sx={{
                    fontSize: '12.5px',
                    fontWeight: 700,
                    whiteSpace: 'nowrap',
                    color: meta.deep,
                  }}
                >
                  {referencedSkill.name}
                </Typography>
                <KeyboardArrowDownIcon
                  sx={{
                    fontSize: 14,
                    color: meta.fg,
                    transform: active ? 'rotate(180deg)' : 'none',
                    transition: 'transform 0.2s ease',
                  }}
                />
              </Box>
            );
          })()}
          {/* 焦点环由容器承担：全局那条 :is(...):focus-visible 的特异性是 (3,0,0)
              （:is 跟最 specific 的参数 [tabindex]:not([tabindex='-1']) 走），组件里写
              `textarea:focus-visible { outline: none }`（(2,1,0)）压根压不住它——曾经这样写过，
              实机看仍是蓝框。所以改成让全局规则按 data-tt-ring 点名排除：这一层只包输入框，
              卡片里其它控件（+ 号、发送、模式）的焦点环一条不少。容器侧的实心环见上方卡片 sx。 */}
          <Box data-tt-ring="container" sx={{ flex: 1, minWidth: 0 }}>
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
          placeholder={loading ? tr('AI 正在处理中…') : tr(modePlaceholder)}
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
              fontFamily: 'var(--tt-chat-font)',
              lineHeight: 1.6,
              px: 0.5,
              color: 'var(--tt-text-1)',
              '&::placeholder': { color: 'var(--tt-text-4)' },
            },
          }}
          />
          </Box>
        </Box>
      </Box>

      {/* 提示词建议：设置页开关控制；点击只回填，不替用户发送；已有输入时收起避免干扰 */}
      {showSuggestions && !value.trim() && !attachments.length && (
        <Box sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 0.75, mt: 1.1 }}>
          <AutoAwesomeIcon sx={{ fontSize: 14, color: '#14B8A6', flexShrink: 0 }} />
          <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mr: 0.25 }}>{tr('试试这样问')}</Typography>
          {suggestions.map((s) => (
            <Chip
              key={s}
              label={tr(s)}
              size="small"
              onClick={() => {
                setValue(tr(s));
                setTimeout(() => inputRef.current?.focus(), 30);
              }}
              sx={{
                height: 24,
                fontSize: '12px',
                color: 'var(--tt-text-3)',
                bgcolor: 'transparent',
                border: '1px solid var(--tt-border)',
                borderRadius: '999px',
                '&:hover': { color: 'var(--tt-teal)', borderColor: 'var(--tt-teal)', bgcolor: 'rgba(20, 184, 166, 0.06)' },
              }}
            />
          ))}
        </Box>
      )}

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
        {/* 左侧：附加操作 + 文件访问档位 + 负责人专家（窄屏可换行，别把档位挤到卡片外） */}
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.25, flexWrap: 'wrap', minWidth: 0 }}>
          <Tooltip title={tr('添加内容（文件 / 文件夹 / 技能）')}>
            <IconButton
              size="small"
              aria-label={tr('添加内容（文件 / 文件夹 / 技能）')}
              aria-haspopup="menu"
              aria-expanded={Boolean(addContentAnchor)}
              sx={{ color: 'var(--tt-text-3)', width: 28, height: 28 }}
              onClick={(e) => setAddContentAnchor(e.currentTarget)}
            >
              <AddIcon sx={{ fontSize: 17 }} />
            </IconButton>
          </Tooltip>

          {/* 文件访问档位：这一轮 AI 能在真实目录里写到哪、能不能删（完全访问标红）。
              挂在 + 之后、与模型选择同一工具栏：档位是"这轮怎么发"的输入侧设置，
              跟发送动作贴在一起才不容易被忽略（原先在输入卡下方的目录行里，离得远）。 */}
          <Tooltip
            title={tr('{desc}｜当前目录：{dir}', {
              desc: tr(permDef.desc),
              dir: workDir || tr('未绑定，产出落在应用工作目录'),
            })}
            placement="top"
          >
            <Box
              {...anchorMenuProps(
                setPermAnchor,
                tr('文件访问权限：{name}', { name: tr(permDef.name) }),
                Boolean(permAnchor),
              )}
              data-file-permission=""
              // 全局环带 2px 外偏移，套在 8px 圆角的无边框小控件上会浮成一层「套框」，
              // 看着像控件自己带了个蓝底框。这里点名不要全局环（data-tt-ring='self'），
              // 由下方自己的 :focus-visible 承担：环仍 2px、仍用 --tt-focus，只是贴边 inward，
              // 键盘焦点可见（2.4.7）不减。
              data-tt-ring="self"
              sx={{
                display: 'flex',
                alignItems: 'center',
                gap: 0.5,
                px: 0.75,
                py: 0.35,
                borderRadius: '8px',
                cursor: 'pointer',
                transition: 'background 0.15s ease',
                '&:hover': { background: 'rgba(15, 23, 42, 0.04)' },
                '&:focus-visible': { outline: '2px solid var(--tt-focus)', outlineOffset: '-2px' },
              }}
            >
              <ShieldOutlinedIcon
                sx={{ fontSize: 14, color: permDef.danger ? 'var(--tt-danger)' : 'var(--tt-text-4)' }}
              />
              <Typography
                sx={{
                  fontSize: '12px',
                  color: permDef.danger ? 'var(--tt-danger)' : 'var(--tt-text-4)',
                  fontWeight: permDef.danger ? 700 : 400,
                  whiteSpace: 'nowrap',
                }}
              >
                {tr(permDef.name)}
              </Typography>
              <KeyboardArrowDownIcon sx={{ fontSize: 14, color: 'var(--tt-text-4)' }} />
            </Box>
          </Tooltip>

          {/* 负责人专家：只在实际有专家时出现；本轮提示词与模型按专家执行 */}
          {experts.length > 0 && (
            <Tooltip title={tr('选择负责人专家（本轮由该专家组织结论，可绑定模型）')}>
              <Box
                {...anchorMenuProps(
                  setExpertAnchor,
                  tr('选择负责人专家（当前：{name}）', { name: selectedExpert?.name ?? tr('通用助手') }),
                  Boolean(expertAnchor),
                )}
                sx={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 0.4,
                  px: 1,
                  py: 0.4,
                  borderRadius: '999px',
                  border: '1px solid var(--tt-border)',
                  cursor: 'pointer',
                  transition: 'all 0.15s ease',
                  '&:hover': { borderColor: 'var(--tt-info)', background: 'rgba(14, 165, 233, 0.04)' },
                }}
              >
                <GroupsIcon sx={{ fontSize: 14, color: selectedExpert ? 'var(--tt-info)' : 'var(--tt-text-4)' }} />
                <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-2)', fontWeight: 500 }}>
                  {selectedExpert?.name ?? tr('通用助手')}
                </Typography>
                <KeyboardArrowDownIcon sx={{ fontSize: 15, color: 'var(--tt-text-4)' }} />
              </Box>
            </Tooltip>
          )}
        </Box>

        {/* 右侧：选择模型 + 麦克风 + 发送 */}
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, flexShrink: 0 }}>
          {/* 选择模型（原「添加到队列」占位已替换为模型选择能力） */}
          <Tooltip title={tr('选择模型')}>
            <Box
              {...anchorMenuProps(
                setModelAnchor,
                tr('选择模型（当前：{name}）', { name: selectedModel.name }),
                Boolean(modelAnchor),
              )}
              sx={{
                display: 'flex',
                alignItems: 'center',
                gap: 0.5,
                px: 1,
                py: 0.4,
                borderRadius: '999px',
                border: '1px solid var(--tt-border)',
                cursor: 'pointer',
                transition: 'all 0.15s ease',
                '&:hover': { borderColor: 'var(--tt-accent-violet)', background: 'rgba(139, 92, 246, 0.04)' },
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
              <Typography sx={{ fontSize: '12px', fontWeight: 500, color: 'var(--tt-text-2)' }}>{selectedModel.name}</Typography>
              <KeyboardArrowDownIcon sx={{ fontSize: 15, color: 'var(--tt-text-4)' }} />
            </Box>
          </Tooltip>

          {/* 语音输入：录音与转录尚未提供，入口保持禁用而不是留一个点了没反应的按钮 */}
          <Tooltip title={tr('语音输入（录音与转录）尚未提供')}>
            <span>
              <IconButton
                size="small"
                disabled
                aria-label={tr('语音输入（录音与转录）尚未提供')}
                sx={{ color: 'var(--tt-text-3)', width: 30, height: 30 }}
              >
                <MicIcon sx={{ fontSize: 18 }} />
              </IconButton>
            </span>
          </Tooltip>

          {/* 发送 / 停止：同一个按钮，任务运行中换成方形停止键（像录像机的停止键） */}
          <Tooltip title={stoppable ? tr('停止任务') : canSend ? tr('发送 (Enter)') : tr('请输入内容')}>
            <span>
              <IconButton
                onClick={handlePrimaryAction}
                aria-label={stoppable ? tr('停止任务') : tr('发送')}
                disabled={!canSend && !stoppable}
                sx={{
                  width: 34,
                  height: 34,
                  borderRadius: '50%',
                  bgcolor: canSend ? 'var(--tt-teal-fill)' : stoppable ? 'var(--tt-warning-fill)' : 'var(--tt-surface-3)',
                  color: canSend || stoppable ? '#fff' : 'var(--tt-text-4)',
                  boxShadow: canSend ? '0 2px 8px rgba(15, 118, 110, 0.35)' : 'none',
                  transition: 'all 0.2s ease',
                  '&:hover': {
                    bgcolor: canSend ? 'var(--tt-teal-fill)' : stoppable ? 'var(--tt-warning-fill)' : 'var(--tt-surface-3)',
                    filter: canSend || stoppable ? 'brightness(1.08)' : 'none',
                  },
                  '&.Mui-disabled': {
                    bgcolor: stoppable ? 'var(--tt-warning-fill)' : 'var(--tt-surface-3)',
                    color: stoppable ? '#fff' : 'var(--tt-text-4)',
                  },
                }}
              >
                {stoppable ? <StopIcon sx={{ fontSize: 18 }} /> : <ArrowUpwardIcon sx={{ fontSize: 18 }} />}
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
                background: SKILL_SOURCE_META[referencedSkill.source].bg,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
              }}
            >
              <AutoAwesomeIcon
                sx={{ fontSize: 15, color: SKILL_SOURCE_META[referencedSkill.source].fg }}
              />
            </Box>
            <Box sx={{ flexGrow: 1, minWidth: 0 }}>
              <Typography sx={{ fontSize: '13.5px', fontWeight: 700, color: 'var(--tt-text-1)' }}>
                /{referencedSkill.name}
              </Typography>
              <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>
                {SKILL_SOURCE_META[referencedSkill.source].blockLabel}
              </Typography>
            </Box>
          </Box>
          {referencedSkill.description && (
            <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-3)', mt: 1, lineHeight: 1.6 }}>
              {referencedSkill.description}
            </Typography>
          )}
          <Box sx={{ display: 'flex', gap: 1, mt: 1.5 }}>
            <Box
              {...clickableProps(() => {
                setSkillBlockAnchor(null);
                setSkillFilter('');
                setSkillsAnchor(inputWrapRef.current);
              })}
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
                border: '1px solid var(--tt-border)',
                cursor: 'pointer',
                transition: 'all 0.15s ease',
                '&:hover': { borderColor: 'var(--tt-accent-violet)', color: 'var(--tt-accent-violet)' },
              }}
            >
              <Typography sx={{ fontSize: '12px', fontWeight: 600, color: 'var(--tt-text-2)' }}>{tr('重新选择')}</Typography>
            </Box>
            <Box
              {...clickableProps(() => {
                setSkillBlockAnchor(null);
                setReferencedSkill(null);
              })}
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
              <Typography sx={{ fontSize: '12px', fontWeight: 600, color: 'var(--tt-danger)' }}>{tr('取消引用')}</Typography>
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
        <Typography sx={{ fontSize: '11px', fontWeight: 600, color: 'var(--tt-text-4)', px: 1.25, py: 0.5 }}>
          {tr('选择技能（{count}）', { count: filteredSkills.length })}
        </Typography>
        {filteredSkills.length === 0 && (
          <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-4)', px: 1.25, py: 1.5, textAlign: 'center' }}>
            {tr('未找到匹配技能，可到扩展页安装')}
          </Typography>
        )}
        {filteredSkills.map((s) => (
          <Box
            key={s.id}
            {...clickableProps(() => pickSkill(s))}
            role="menuitem"
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
                background: SKILL_SOURCE_META[s.source].bg,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
              }}
            >
              <AutoAwesomeIcon sx={{ fontSize: 14, color: SKILL_SOURCE_META[s.source].fg }} />
            </Box>
            <Box sx={{ flexGrow: 1, minWidth: 0 }}>
              <Typography sx={{ fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-1)' }}>/{s.name}</Typography>
              {s.description && (
                <Typography
                  sx={{
                    fontSize: '11px',
                    color: 'var(--tt-text-4)',
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
                background: SKILL_SOURCE_META[s.source].bg,
              }}
            >
              <Typography sx={{ fontSize: '10px', fontWeight: 600, color: SKILL_SOURCE_META[s.source].fg }}>
                {SKILL_SOURCE_META[s.source].label}
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
        <Typography sx={{ fontSize: '11px', fontWeight: 600, color: 'var(--tt-text-4)', px: 1.25, py: 0.5 }}>
          {tr('内置模型')}
        </Typography>
        {BUILTIN_MODELS.map((m) => (
          <Box
            key={m.id}
            {...clickableProps(() => {
              setChatModelId(m.id);
              setModelAnchor(null);
            })}
            role="menuitem"
            aria-current={chatModelId === m.id ? 'true' : undefined}
            onClick={() => {
              setChatModelId(m.id);
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
              <Typography sx={{ fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-1)' }}>{tr(m.name)}</Typography>
              <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)', mt: 0.15 }}>{tr(m.desc)}</Typography>
            </Box>
            {selectedModel.id === m.id && <CheckIcon sx={{ fontSize: 16, color: 'var(--tt-accent-violet)' }} />}
          </Box>
        ))}

        {/* 已保存模型（数据源由后端持有，选中后按其配置真实调用） */}
        {savedOptions.length > 0 && (
          <>
            <Typography sx={{ fontSize: '11px', fontWeight: 600, color: 'var(--tt-text-4)', px: 1.25, py: 0.5, borderTop: '1px solid var(--tt-border-soft)', mt: 0.5 }}>
              {tr('已保存模型')}
            </Typography>
            {savedOptions.map((m) => (
              <Box
                key={m.id}
                {...clickableProps(() => {
                  setChatModelId(m.id);
                  setModelAnchor(null);
                })}
                role="menuitem"
                aria-current={chatModelId === m.id ? 'true' : undefined}
                onClick={() => {
                  setChatModelId(m.id);
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
                  <Typography sx={{ fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-1)' }}>{m.name}</Typography>
                  <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)', mt: 0.15, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{m.desc}</Typography>
                </Box>
                {selectedModel.id === m.id && <CheckIcon sx={{ fontSize: 16, color: 'var(--tt-accent-violet)' }} />}
              </Box>
            ))}
          </>
        )}

        <Box sx={{ borderTop: '1px solid var(--tt-border-soft)', mt: 0.5, pt: 0.5 }}>
          <Box
            {...clickableProps(() => {
              setModelAnchor(null);
              navigate('/settings?s=models');
            })}
            role="menuitem"
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
                background: 'var(--tt-chip-neutral)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
              }}
            >
              <AddIcon sx={{ fontSize: 15, color: 'var(--tt-text-3)' }} />
            </Box>
            <Typography sx={{ fontSize: '13px', fontWeight: 500, color: 'var(--tt-text-2)' }}>{tr('添加模型')}</Typography>
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
          <AttachFileIcon sx={{ fontSize: 16, color: 'var(--tt-text-3)' }} /> {tr('添加文件')}
        </MenuItem>
        <MenuItem
          onClick={() => {
            setAddContentAnchor(null);
            handleAddFolder();
          }}
          sx={{ fontSize: '13px', gap: 1 }}
        >
          <FolderOutlinedIcon sx={{ fontSize: 16, color: 'var(--tt-text-3)' }} /> {tr('添加文件夹')}
        </MenuItem>
        <MenuItem
          onClick={() => {
            setAddContentAnchor(null);
            setSkillFilter('');
            setSkillsAnchor(inputWrapRef.current);
          }}
          sx={{ fontSize: '13px', gap: 1 }}
        >
          <AutoAwesomeIcon sx={{ fontSize: 16, color: 'var(--tt-text-3)' }} /> {tr('调用技能')}
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

      {/* 负责人专家菜单：空白 id 表示按通用助手执行；模型随专家绑定 */}
      <Menu
        anchorEl={expertAnchor}
        open={Boolean(expertAnchor)}
        onClose={() => setExpertAnchor(null)}
        anchorOrigin={{ vertical: 'top', horizontal: 'left' }}
        transformOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        PaperProps={{ sx: { minWidth: 260, mt: -0.5 } }}
      >
        <MenuItem
          selected={!chatExpertId}
          onClick={() => {
            setChatExpertId('');
            setExpertAnchor(null);
          }}
          sx={{ fontSize: '13px', gap: 1 }}
        >
          <Box sx={{ width: 16, flexShrink: 0 }}>
            {!chatExpertId && <CheckIcon sx={{ fontSize: 16, color: 'var(--tt-info)' }} />}
          </Box>
          <Box sx={{ minWidth: 0 }}>
            <Typography sx={{ fontSize: '13px', fontWeight: 500 }}>{tr('通用助手')}</Typography>
            <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>
              {tr('不指定专家，按当前模型与技能执行')}
            </Typography>
          </Box>
        </MenuItem>
        {experts.map((e) => (
          <MenuItem
            key={e.id}
            selected={e.id === chatExpertId}
            disabled={Boolean(e.modelError)}
            onClick={() => {
              setChatExpertId(e.id);
              setExpertAnchor(null);
            }}
            sx={{ fontSize: '13px', gap: 1, alignItems: 'flex-start' }}
          >
            <Box sx={{ width: 16, flexShrink: 0, pt: 0.25 }}>
              {e.id === chatExpertId && <CheckIcon sx={{ fontSize: 16, color: 'var(--tt-info)' }} />}
            </Box>
            <Box sx={{ minWidth: 0 }}>
              <Typography sx={{ fontSize: '13px', fontWeight: 500 }}>{e.name}</Typography>
              <Typography
                sx={{
                  fontSize: '11px',
                  color: e.modelError ? 'var(--tt-danger)' : 'var(--tt-text-4)',
                  whiteSpace: 'normal',
                }}
              >
                {e.modelError ? tr('绑定模型不可用：{msg}', { msg: e.modelError }) : e.modelName ? tr('模型：{name}', { name: e.modelName }) : tr('沿用当前模型')}
              </Typography>
            </Box>
          </MenuItem>
        ))}
      </Menu>
    </Box>
    </Box>

      {/* 输入框下方：选择工作目录（真实目录）+ 免责提示；文件访问档位已进卡片内工具栏 */}
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mt: 0.75, px: 0.5 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.25, minWidth: 0 }}>
        <Tooltip title={workDir || tr('选择会话内容与产出物的存放目录')} placement="top">
          <Box
            {...clickableProps(handlePickWorkDir, workDir ? tr('更换工作目录') : tr('选择工作目录'))}
            onClick={handlePickWorkDir}
            // 与卡片内工具栏的档位 chip 同一处理：这类控件自己没有边框，
            // 外偏移的全局焦点环会在它外面再画一个框，改为自绘贴边环
            data-tt-ring="self"
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
              '&:focus-visible': { outline: '2px solid var(--tt-focus)', outlineOffset: '-2px' },
            }}
          >
            <FolderOutlinedIcon sx={{ fontSize: 14, color: workDir ? 'var(--tt-accent)' : 'var(--tt-text-4)' }} />
            <Typography
              sx={{
                fontSize: '12px',
                color: workDir ? 'var(--tt-accent)' : 'var(--tt-text-4)',
                fontWeight: workDir ? 600 : 400,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {workDir ? workDir.split(/[\\/]/).filter(Boolean).slice(-2).join('/') : tr('选择工作目录')}
            </Typography>
          </Box>
        </Tooltip>
        </Box>
        <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>{tr('内容由 AI 生成，请注意核实')}</Typography>
      </Box>

      {/* 文件访问档位菜单：三档说明各自写清放开/收紧了什么。
          触发件在卡片内工具栏（+ 之后），菜单本身走 portal，留在这里不影响锚定。 */}
      <Menu
        anchorEl={permAnchor}
        open={Boolean(permAnchor)}
        onClose={() => setPermAnchor(null)}
        anchorOrigin={{ vertical: 'top', horizontal: 'left' }}
        transformOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        PaperProps={{ sx: { width: 330, mb: 0.5, borderRadius: '14px', p: 0.75 } }}
      >
        <Typography sx={{ fontSize: '11px', fontWeight: 600, color: 'var(--tt-text-4)', px: 1.25, py: 0.5 }}>
          {tr('文件访问权限')}
        </Typography>
        {FILE_PERMISSION_DEFS.map((def) => (
          <MenuItem
            key={def.id}
            onClick={() => handlePickFilePermission(def.id)}
            selected={def.id === permMode}
            sx={{ alignItems: 'flex-start', gap: 1, px: 1.25, py: 0.9, borderRadius: '10px' }}
          >
            <Box sx={{ width: 18, flexShrink: 0, pt: 0.25 }}>
              {def.id === permMode && <CheckIcon sx={{ fontSize: 16, color: 'var(--tt-accent)' }} />}
            </Box>
            <Box sx={{ minWidth: 0 }}>
              <Typography
                sx={{
                  fontSize: '13px',
                  fontWeight: 600,
                  color: def.danger ? 'var(--tt-danger)' : 'var(--tt-text-2)',
                }}
              >
                {tr(def.name)}
              </Typography>
              <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)', lineHeight: 1.6 }}>
                {tr(def.desc)}
              </Typography>
            </Box>
          </MenuItem>
        ))}
      </Menu>

      {/* 完全访问：红色警告 + 明确确认，确认时刻随档位一起写进每会话台账 */}
      <Dialog
        open={Boolean(permPending)}
        onClose={() => setPermPending(null)}
        aria-labelledby="file-permission-danger-title"
        maxWidth="xs"
        fullWidth
      >
        <DialogTitle id="file-permission-danger-title" sx={{ fontSize: '16px', fontWeight: 700 }}>
          {tr('启用「完全访问」？')}
        </DialogTitle>
        <DialogContent>
          <DialogContentText component="div" sx={{ fontSize: '13px', lineHeight: 1.8 }}>
            <Box sx={{ color: 'var(--tt-danger)', fontWeight: 700, mb: 1 }}>
              {tr('开启后，AI 在本次会话里可以直接读写工作目录之外的本机路径，并删除文件，不再逐次询问。')}
            </Box>
            <Box sx={{ mb: 1 }}>
              {tr('删除是不可逆操作：文件会先复制进应用回收目录并记下原路径与原因，但找回仍需手动操作。建议只在核对过执行计划的会话里开启，用完切回「每次询问」。')}
            </Box>
            <Box sx={{ color: 'var(--tt-danger)', fontWeight: 600 }}>
              {tr('家目录、系统关键目录与应用数据目录始终硬拒：完全访问放开的是目录边界，不是整台电脑。')}
            </Box>
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setPermPending(null)} sx={{ textTransform: 'none' }}>
            {tr('取消')}
          </Button>
          <Button
            variant="contained"
            onClick={handleConfirmFullAccess}
            sx={{ textTransform: 'none', bgcolor: 'var(--tt-danger-fill)', '&:hover': { bgcolor: '#B91C1C' } }}
          >
            {tr('我已了解，启用完全访问')}
          </Button>
        </DialogActions>
      </Dialog>

      {/* 幻灯片模板：放在工作目录之后；只列可用模板，导入通道在设置页 */}
      {activeMode === 'slides' && (
        <Box sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 0.75, mt: 0.5, px: 0.5 }}>
          <Tooltip title={tr('幻灯片模式按所选模板生成 .pptx；可在「设置 → 工作台 → 幻灯片模板」导入本机模板')}>
            <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>{tr('模板')}</Typography>
          </Tooltip>
          <Box
            component="select"
            data-pptx-picker=""
            aria-label={tr('幻灯片模板')}
            value={pptx.selectedId}
            disabled={pptx.loading || pptxSwitching || pptxOptions.length === 0}
            onChange={(e: React.ChangeEvent<HTMLSelectElement>) => void handlePptxSelect(e.target.value)}
            sx={{
              appearance: 'auto',
              px: 1,
              py: 0.4,
              pr: 0.75,
              borderRadius: '8px',
              border: '1px solid var(--tt-border)',
              bgcolor: 'var(--tt-surface)',
              fontSize: '12px',
              color: 'var(--tt-text-2)',
              cursor: 'pointer',
              '&:hover': { borderColor: 'var(--tt-teal)' },
              '&:disabled': { opacity: 0.55, cursor: 'not-allowed' },
            }}
          >
            {pptxOptions.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
                {t.builtin ? '' : tr('（导入）')}
              </option>
            ))}
          </Box>
          {pptx.loading && <CircularProgress size={12} />}
          {pptxError && <Typography sx={{ fontSize: '12px', color: 'var(--tt-danger)' }}>{pptxError}</Typography>}
          {!pptx.desktop && (
            <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>{tr('浏览器直连仅内置主题')}</Typography>
          )}
        </Box>
      )}
    </Box>
  );
};

export default ChatInput;
