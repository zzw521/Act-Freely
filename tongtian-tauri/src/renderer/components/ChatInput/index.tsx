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
import GroupsIcon from '@mui/icons-material/Groups';
import type { AttachmentMeta, ModelConfig, WorkMode } from '../../../shared/types';
import type { ExpertInfo } from '../../../shared/contracts';
import { isTauri } from '../../lib/tauri-api';
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
import { usePptxTemplates } from '../../lib/use-pptx-templates';
import { useWorkbenchStore } from '../../store/workbenchStore';
import { useI18n } from '../../i18n';
import { useNavigate } from 'react-router-dom';
import { errorTextOf } from '../../../shared/error-messages';

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
  placeholder = '描述任务，输入 /调用技能',
  maxRows = 8,
  initialText,
  workDir,
  onSelectWorkDir,
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
    color: '#0EA5E9',
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
        setAttachError(tr('打开文件对话框失败：{msg}', { msg: String(e) }));
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
        setAttachError(tr('打开文件夹对话框失败：{msg}', { msg: String(e) }));
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
  }, [value, loading, staging, attachments, referencedSkill, selectedModel, onSend, activeMode, chatExpertId]);

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
  // 只在「空输入、无附件、未引用技能、非生成中」时给建议，避免和用户已写内容抢空间
  const showSuggestions =
    promptSuggestionEnabled &&
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
    {/* 工作台模式胶囊组：设置页只启用一个模式时不出现，避免单选装饰 */}
    {enabledModeDefs.length > 1 && (
      <Box
        role="group"
        aria-label={tr('工作台模式')}
        sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75, mb: 1, px: 0.25 }}
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
                  bgcolor: selected ? '#14B8A6' : 'transparent',
                  color: selected ? '#fff' : 'var(--tt-text-3)',
                  border: `1px solid ${selected ? '#14B8A6' : 'var(--tt-border)'}`,
                  transition: 'all 0.15s ease',
                  '&:hover': {
                    bgcolor: selected ? '#0D9488' : 'rgba(20, 184, 166, 0.06)',
                    borderColor: '#14B8A6',
                  },
                  '& .MuiChip-label': { px: 1.25 },
                }}
              />
            </Tooltip>
          );
        })}
      </Box>
    )}
    {/* 幻灯片模式模板选择：只列可用模板；导入通道在设置页，避免输入框里再堆一个假的导入按钮 */}
    {activeMode === 'slides' && (
      <Box sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 0.75, mb: 1, px: 0.25 }}>
        <Tooltip title={tr('幻灯片模式按所选模板生成 .pptx；可在「设置 → 工作台 → 幻灯片模板」导入本机模板')}>
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>{tr('模板')}</Typography>
        </Tooltip>
        <Box
          component="select"
          data-pptx-picker=""
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
            outline: 'none',
            '&:hover': { borderColor: '#14B8A6' },
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
        {pptxError && <Typography sx={{ fontSize: '12px', color: '#EF4444' }}>{pptxError}</Typography>}
        {!pptx.desktop && (
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>{tr('浏览器直连仅内置主题')}</Typography>
        )}
      </Box>
    )}
    <Box
      sx={{
        bgcolor: 'var(--tt-surface)',
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
          {referencedSkill && (() => {
            const active = Boolean(skillBlockAnchor); // 点击展开时才变色
            const meta = SKILL_SOURCE_META[referencedSkill.source];
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
                  background: active ? meta.activeBg : meta.bg,
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
          sx={{ flex: 1, minWidth: 0 }}
          />
        </Box>
      </Box>

      {/* 提示词建议：设置页开关控制；点击只回填，不替用户发送 */}
      {showSuggestions && (
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
                '&:hover': { color: '#0D9488', borderColor: '#5EEAD4', bgcolor: 'rgba(20, 184, 166, 0.06)' },
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
        {/* 左侧：附加操作 + 负责人专家 */}
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.25 }}>
          <Tooltip title={tr('添加内容（文件 / 文件夹 / 技能）')}>
            <IconButton
              size="small"
              sx={{ color: 'var(--tt-text-3)', width: 28, height: 28 }}
              onClick={(e) => setAddContentAnchor(e.currentTarget)}
            >
              <AddIcon sx={{ fontSize: 17 }} />
            </IconButton>
          </Tooltip>

          {/* 负责人专家：只在实际有专家时出现；本轮提示词与模型按专家执行 */}
          {experts.length > 0 && (
            <Tooltip title={tr('选择负责人专家（本轮由该专家组织结论，可绑定模型）')}>
              <Box
                onClick={(e) => setExpertAnchor(e.currentTarget)}
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
                  '&:hover': { borderColor: '#0EA5E9', background: 'rgba(14, 165, 233, 0.04)' },
                }}
              >
                <GroupsIcon sx={{ fontSize: 14, color: selectedExpert ? '#0EA5E9' : 'var(--tt-text-4)' }} />
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
              onClick={(e) => setModelAnchor(e.currentTarget)}
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

          {/* 发送按钮 */}
          <Tooltip title={canSend ? tr('发送 (Enter)') : loading ? tr('生成中…') : tr('请输入内容')}>
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
                background: SKILL_SOURCE_META[referencedSkill.source].bg,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                flexShrink: 0,
              }}
            >
              <AutoAwesomeIcon
                sx={{ fontSize: 15, color: SKILL_SOURCE_META[referencedSkill.source].solid }}
              />
            </Box>
            <Box sx={{ flexGrow: 1, minWidth: 0 }}>
              <Typography sx={{ fontSize: '13.5px', fontWeight: 700, color: '#111827' }}>
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
                '&:hover': { borderColor: '#8B5CF6', color: '#8B5CF6' },
              }}
            >
              <Typography sx={{ fontSize: '12px', fontWeight: 600, color: 'var(--tt-text-2)' }}>{tr('重新选择')}</Typography>
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
              <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#EF4444' }}>{tr('取消引用')}</Typography>
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
              <AutoAwesomeIcon sx={{ fontSize: 14, color: SKILL_SOURCE_META[s.source].solid }} />
            </Box>
            <Box sx={{ flexGrow: 1, minWidth: 0 }}>
              <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#1F2937' }}>/{s.name}</Typography>
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
              <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#1F2937' }}>{tr(m.name)}</Typography>
              <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)', mt: 0.15 }}>{tr(m.desc)}</Typography>
            </Box>
            {selectedModel.id === m.id && <CheckIcon sx={{ fontSize: 16, color: '#8B5CF6' }} />}
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
                  <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#1F2937' }}>{m.name}</Typography>
                  <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)', mt: 0.15, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{m.desc}</Typography>
                </Box>
                {selectedModel.id === m.id && <CheckIcon sx={{ fontSize: 16, color: '#8B5CF6' }} />}
              </Box>
            ))}
          </>
        )}

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
            {!chatExpertId && <CheckIcon sx={{ fontSize: 16, color: '#0EA5E9' }} />}
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
              {e.id === chatExpertId && <CheckIcon sx={{ fontSize: 16, color: '#0EA5E9' }} />}
            </Box>
            <Box sx={{ minWidth: 0 }}>
              <Typography sx={{ fontSize: '13px', fontWeight: 500 }}>{e.name}</Typography>
              <Typography
                sx={{
                  fontSize: '11px',
                  color: e.modelError ? '#DC2626' : 'var(--tt-text-4)',
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

          {/* 输入框下方：选择工作目录（真实目录） + 免责提示 */}
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mt: 0.75, px: 0.5 }}>
        <Tooltip title={workDir || tr('选择会话内容与产出物的存放目录')} placement="top">
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
              {workDir ? workDir.split(/[\\/]/).filter(Boolean).slice(-2).join('/') : tr('选择工作目录')}
            </Typography>
          </Box>
        </Tooltip>
        <Typography sx={{ fontSize: '12px', color: '#C0C4CA' }}>{tr('内容由 AI 生成，请注意核实')}</Typography>
      </Box>
    </Box>
  );
};

export default ChatInput;
