/**
 * 通天办公 - 专家套件页
 *
 * 专家套件是面向角色/行业的工具套件，安装后其指令会并入对话框的 / 候选。
 * 指令正文（manifest 的 content/prompt/body/instructions、显式 file，或
 * commands/<name>.md、prompts/<name>.md 约定文件）随 runtime 下发引擎，
 * 选中该指令或模型调用 use_skill 时按需进入上下文；缺正文时页面与执行流都会如实说明。
 * 页面结构（对齐设计稿）：
 * - 顶部：标题 + 说明 + 精选行业横幅
 * - 套件广场 / 已安装 页签
 * - 行业分类筛选（全部/行政/HR/财务…）+ 推荐/最新排序
 * - 套件卡片：图标、名称、作者、简介、技能/指令/数据连接统计、版本、安装按钮
 *
 * 数据来源口径：
 * - 「已安装」页签来自套件注册表（桌面端为 suites/<id>/manifest.json，浏览器通道为本地台账），
 *   统计数字与版本全部取包内声明，页面不再自造 installed 标志。
 * - 「套件广场」优先展示用户配置的在线市场索引；未配置索引时，开发阶段回落到本地示例目录
 *   并明确标注「示例」，联调阶段为空。示例条目没有下载地址，安装按钮会如实说明。
 *
 * Mock 开关规范：
 * - 开发阶段（VITE_MOCK !== 'false'）：未配置市场源时展示本地示例目录，安装/卸载走真实本地通道
 * - 联调阶段（VITE_MOCK === 'false'）：未配置市场源时广场为空，引导配置在线市场或上传 .zip
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Box,
  Typography,
  Snackbar,
  Alert,
  Tooltip,
  Button,
  Menu,
  Dialog,
  DialogTitle,
  DialogContent,
  TextField,
  CircularProgress,
  Divider,
} from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import ChatBubbleOutlineIcon from '@mui/icons-material/ChatBubbleOutline';
import UploadFileIcon from '@mui/icons-material/UploadFile';
import CloudUploadOutlinedIcon from '@mui/icons-material/CloudUploadOutlined';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import ContentCopyIcon from '@mui/icons-material/ContentCopy';
import CheckCircleOutlineIcon from '@mui/icons-material/CheckCircleOutline';
import ErrorOutlineIcon from '@mui/icons-material/ErrorOutline';
import { useNavigate, useSearchParams } from 'react-router-dom';
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome';
import AccountBalanceIcon from '@mui/icons-material/AccountBalance';
import GavelIcon from '@mui/icons-material/Gavel';
import InsightsIcon from '@mui/icons-material/Insights';
import PaletteIcon from '@mui/icons-material/Palette';
import LocalShippingIcon from '@mui/icons-material/LocalShipping';
import CampaignIcon from '@mui/icons-material/Campaign';
import WorkIcon from '@mui/icons-material/Work';
import GroupsIcon from '@mui/icons-material/Groups';
import BadgeIcon from '@mui/icons-material/Badge';
import PublicIcon from '@mui/icons-material/Public';
import CodeIcon from '@mui/icons-material/Code';
import AnalyticsIcon from '@mui/icons-material/Analytics';
import { marketAPI, pluginAPI, suiteAPI } from '../../lib/api';
import { bytesToBase64 } from '../../lib/suite-zip';
import { errorTextOf } from '../../../shared/error-messages';
import type {
  InstalledSuite,
  PluginTemplate,
  PluginValidation,
  SuiteRootInfo,
} from '../../../shared/contracts';
import MarketBrowser from '../../components/MarketBrowser';
import ExpertsPage from '../Experts';

// ── 对话创建套件的预置提示词 ──────────────────────────────

const SUITE_CREATE_PROMPT = '帮我创建一套专属的工作套件。我可以提供我的工作 SOP、业务文档或者岗位日常。';

// ── 数据模型 ──────────────────────────────────────────────────────

/** 广场示例目录项（远端市场接入前的本地策展目录，不含安装状态） */
interface CatalogSuite {
  id: string;
  name: string;
  author: string;
  desc: string;
  category: string;
  skillCount: number;
  commandCount: number;
  dataLinkCount: number;
  version: string;
  /** 策展推荐次序（仅示例目录提供；不是真实安装量，界面不称「热度」） */
  hot: number;
  /** 发布时间戳（最新排序用） */
  publishedAt: number;
  icon: React.ElementType;
  iconColor: string;
  /** market = 在线市场索引条目；sample = 开发阶段本地示例（无下载地址） */
  source?: 'market' | 'sample';
}

/** 卡片渲染模型：广场示例与已安装注册表统一到此结构 */
interface SuiteCardView {
  key: string;
  /** 套件注册表标识（卸载与 / 指令归属用） */
  id: string;
  name: string;
  author: string;
  desc: string;
  category: string;
  skillCount: number;
  commandCount: number;
  dataLinkCount: number;
  version: string;
  hot: number;
  publishedAt: number;
  icon: React.ElementType;
  iconColor: string;
  /** 是否已在本地注册表里（真实读取，不是示例标志位） */
  installed: boolean;
  /** manifest.json 是否可解析；false 时提示重新安装 */
  readable: boolean;
  /** 套件目录 / 本地台账标识，仅已安装行有值 */
  path: string;
  /** 套件目录内文件数（仅已安装行有真实值） */
  fileCount: number;
  /** 套件指令名（对话输入 / 时的候选，仅已安装行有真实值） */
  commandNames: string[];
  source: 'market' | 'sample' | 'installed';
}

const CATEGORIES = [
  '全部', '行政', 'HR', '财务', '律师', '市场', '销售',
  '产研', '运营', '数据分析', '供应链', '设计', '政务公共',
];

// ── Mock 数据（开发阶段广场示例目录） ─────────────────────────────

const mockEnabled = import.meta.env.VITE_MOCK !== 'false';
const now = Date.now();

const MOCK_SUITES: CatalogSuite[] = [
  {
    id: 'finance-suite', name: '金融行业套件', author: '@任我行官方', category: '财务',
    desc: '风控、研报与合规流程一键接入对话；用套件固化常用模板与数据源。',
    skillCount: 12, commandCount: 6, dataLinkCount: 3, version: 'v2.1.0',
    hot: 980, publishedAt: now - 2 * 86400e3, icon: AccountBalanceIcon, iconColor: '#10B981',
  },
  {
    id: 'legal-labor', name: '企业劳动用工专家套件', author: '@明白律师', category: '律师',
    desc: '劳动合同审查、用工风险评估、劳动争议应对指引，覆盖企业用工全场景。',
    skillCount: 8, commandCount: 4, dataLinkCount: 1, version: 'v1.3.2',
    hot: 860, publishedAt: now - 5 * 86400e3, icon: GavelIcon, iconColor: '#8B5CF6',
  },
  {
    id: 'prompt-studio', name: 'prompt-design-studio', author: '@Zhanganren', category: '产研',
    desc: '提示词设计工坊：把备课、命题、评价设计中的具体需求变成一份可直接提交给通用 AI 的专业提示语模板。',
    skillCount: 1, commandCount: 0, dataLinkCount: 0, version: 'v1.1.0',
    hot: 720, publishedAt: now - 1 * 86400e3, icon: CodeIcon, iconColor: '#06B6D4',
  },
  {
    id: 'agent-workshop', name: '智能体设计工坊', author: '@世外智慧教育 AI应用研究院', category: '产研',
    desc: '引导式四步工坊（选题、设计、生成、测试），帮助教师把教学痛点转化为可用的智能体方案。',
    skillCount: 3, commandCount: 2, dataLinkCount: 0, version: 'v1.1.0',
    hot: 680, publishedAt: now - 3 * 86400e3, icon: AutoAwesomeIcon, iconColor: '#F59E0B',
  },
  {
    id: 'market-growth', name: '市场增长套件', author: '@增长黑客社区', category: '市场',
    desc: '投放文案生成、活动复盘与竞品速览，市场人的日常增长工具箱。',
    skillCount: 6, commandCount: 5, dataLinkCount: 2, version: 'v1.0.4',
    hot: 540, publishedAt: now - 8 * 86400e3, icon: CampaignIcon, iconColor: '#EC4899',
  },
  {
    id: 'sales-copilot', name: '销售作战套件', author: '@销冠实验室', category: '销售',
    desc: '客户画像速写、拜访纪要整理、跟进话术建议，让每次跟进更有准备。',
    skillCount: 5, commandCount: 3, dataLinkCount: 1, version: 'v0.9.1',
    hot: 490, publishedAt: now - 12 * 86400e3, icon: WorkIcon, iconColor: '#F97316',
  },
  {
    id: 'hr-hire', name: 'HR 招聘套件', author: '@人力资源数字化联盟', category: 'HR',
    desc: 'JD 生成、简历初筛要点、面试问题清单，一站式招聘提效。',
    skillCount: 7, commandCount: 4, dataLinkCount: 1, version: 'v1.2.0',
    hot: 450, publishedAt: now - 6 * 86400e3, icon: BadgeIcon, iconColor: '#3B82F6',
  },
  {
    id: 'ops-daily', name: '运营日常套件', author: '@运营星球', category: '运营',
    desc: '日报周报自动生成、活动数据速读、用户反馈归类，运营琐事一键搞定。',
    skillCount: 9, commandCount: 6, dataLinkCount: 2, version: 'v1.5.0',
    hot: 430, publishedAt: now - 4 * 86400e3, icon: GroupsIcon, iconColor: '#14B8A6',
  },
  {
    id: 'data-insight', name: '数据分析套件', author: '@数洞科技', category: '数据分析',
    desc: 'SQL 助手、图表解读与异常归因，把数据变成可行动的结论。',
    skillCount: 10, commandCount: 5, dataLinkCount: 4, version: 'v2.0.1',
    hot: 410, publishedAt: now - 10 * 86400e3, icon: AnalyticsIcon, iconColor: '#6366F1',
  },
  {
    id: 'supply-chain', name: '供应链协同套件', author: '@链上智库', category: '供应链',
    desc: '采购比价、库存预警解读与物流跟踪摘要，供应链信息一站汇总。',
    skillCount: 6, commandCount: 3, dataLinkCount: 3, version: 'v1.0.0',
    hot: 300, publishedAt: now - 15 * 86400e3, icon: LocalShippingIcon, iconColor: '#84CC16',
  },
  {
    id: 'design-brand', name: '品牌设计套件', author: '@设计驱动营', category: '设计',
    desc: '品牌调性梳理、海报文案与评审意见整理，设计协作更顺滑。',
    skillCount: 4, commandCount: 2, dataLinkCount: 0, version: 'v0.8.0',
    hot: 260, publishedAt: now - 20 * 86400e3, icon: PaletteIcon, iconColor: '#F43F5E',
  },
  {
    id: 'gov-affairs', name: '政务公共服务套件', author: '@智慧城市研究院', category: '政务公共',
    desc: '政策解读速览、办事指南整理与公告起草辅助，公共服务场景提效。',
    skillCount: 5, commandCount: 4, dataLinkCount: 1, version: 'v1.1.2',
    hot: 220, publishedAt: now - 18 * 86400e3, icon: PublicIcon, iconColor: '#0EA5E9',
  },
  {
    id: 'admin-assist', name: '行政事务套件', author: '@行政百宝箱', category: '行政',
    desc: '会议纪要、通知公告与物资盘点清单，行政事务标准化输出。',
    skillCount: 6, commandCount: 5, dataLinkCount: 1, version: 'v1.4.0',
    hot: 380, publishedAt: now - 7 * 86400e3, icon: GroupsIcon, iconColor: '#A855F7',
  },
  {
    id: 'fin-report', name: '财务报表助手', author: '@财会小站', category: '财务',
    desc: '报表口径核对、异常科目提示与附注草稿生成，财务月结更从容。',
    skillCount: 5, commandCount: 3, dataLinkCount: 2, version: 'v1.0.6',
    hot: 350, publishedAt: now - 9 * 86400e3, icon: InsightsIcon, iconColor: '#059669',
  },
];

/** 已安装套件的图标按分类取用，未覆盖分类回落套件主色 */
const CATEGORY_ICON: Record<string, { icon: React.ElementType; color: string }> = {
  财务: { icon: AccountBalanceIcon, color: '#10B981' },
  律师: { icon: GavelIcon, color: '#8B5CF6' },
  产研: { icon: CodeIcon, color: '#06B6D4' },
  市场: { icon: CampaignIcon, color: '#EC4899' },
  销售: { icon: WorkIcon, color: '#F97316' },
  HR: { icon: BadgeIcon, color: '#3B82F6' },
  运营: { icon: GroupsIcon, color: '#14B8A6' },
  数据分析: { icon: AnalyticsIcon, color: '#6366F1' },
  供应链: { icon: LocalShippingIcon, color: '#84CC16' },
  设计: { icon: PaletteIcon, color: '#F43F5E' },
  政务公共: { icon: PublicIcon, color: '#0EA5E9' },
  行政: { icon: InsightsIcon, color: '#A855F7' },
};

/** 在线市场索引 → 广场卡片（条目字段全部来自索引，不补造安装量） */
async function loadMarketCatalog(): Promise<{
  items: CatalogSuite[];
  configured: boolean;
  error: string | null;
}> {
  try {
    const list = await marketAPI.listSuites();
    if (!list.supported || !list.configured) {
      return { items: [], configured: false, error: null };
    }
    const items: CatalogSuite[] = list.items.map((item) => {
      const meta = CATEGORY_ICON[item.category ?? ''];
      return {
        id: item.id,
        name: item.name,
        author: item.author ?? '未标注作者',
        desc: item.description ?? '索引未填写简介',
        category: item.category ?? '',
        skillCount: item.skillCount ?? 0,
        commandCount: item.commandCount ?? 0,
        dataLinkCount: item.dataLinkCount ?? 0,
        version: item.version ?? '',
        hot: 0,
        publishedAt: item.publishedAtMs ?? 0,
        icon: meta?.icon ?? AutoAwesomeIcon,
        iconColor: meta?.color ?? '#F59E0B',
        source: 'market',
      };
    });
    return { items, configured: true, error: null };
  } catch (e) {
    return { items: [], configured: true, error: errorTextOf(e, '套件市场读取失败') };
  }
}

/** 注册表行 → 卡片视图（数字全部取套件包声明，不再由页面补写） */
function toInstalledCard(s: InstalledSuite): SuiteCardView {
  const meta = CATEGORY_ICON[s.category ?? ''];
  return {
    key: `installed:${s.id}`,
    id: s.id,
    name: s.name,
    author: s.author ?? '本地套件',
    desc: s.description ?? (s.readable ? '套件包未填写简介' : 'manifest.json 解析失败，套件内容不可用'),
    category: s.category ?? '',
    skillCount: s.skillCount,
    commandCount: s.commands.length,
    dataLinkCount: s.dataLinkCount,
    version: s.version,
    hot: 0,
    publishedAt: s.installedAtMs,
    icon: meta?.icon ?? AutoAwesomeIcon,
    iconColor: meta?.color ?? '#F59E0B',
    installed: true,
    readable: s.readable,
    path: s.path,
    fileCount: s.fileCount,
    commandNames: s.commands.map((c) => c.name),
    source: 'installed',
  };
}

/** 通道错误转文案：走全局错误码字典，保留诊断原因但不把 SUITE_* 裸码丢给用户 */
function toMessage(e: unknown): string {
  return errorTextOf(e, '套件操作失败，请重试');
}

// ── 页面组件 ──────────────────────────────────────────────────────

type SuiteView = 'plaza' | 'installed' | 'experts';

const ExpertSuitePage: React.FC = () => {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [catalog, setCatalog] = useState<CatalogSuite[]>([]);
  const [catalogSource, setCatalogSource] = useState<'market' | 'sample' | 'none'>('none');
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [installedRows, setInstalledRows] = useState<InstalledSuite[]>([]);
  const [rootInfo, setRootInfo] = useState<SuiteRootInfo | null>(null);
  const [marketOpen, setMarketOpen] = useState(false);
  const [view, setView] = useState<SuiteView>(() => {
    const tab = searchParams.get('tab');
    return tab === 'installed' || tab === 'experts' ? tab : 'plaza';
  });
  const [category, setCategory] = useState('全部');
  const [sort, setSort] = useState<'hot' | 'latest'>('hot');
  const [toast, setToast] = useState<{ severity: 'info' | 'success' | 'error'; text: string } | null>(null);
  const [installing, setInstalling] = useState(false);

  useEffect(() => {
    const tab = searchParams.get('tab');
    if (tab === 'installed' || tab === 'experts') {
      setView(tab);
    } else if (tab === null) {
      setView('plaza');
    }
  }, [searchParams]);

  const changeView = (next: SuiteView) => {
    setView(next);
    setSearchParams(next === 'plaza' ? {} : { tab: next }, { replace: true });
  };

  // 「+ 添加」下拉与上传弹窗状态
  const [addAnchor, setAddAnchor] = useState<null | HTMLElement>(null);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // 插件 SDK：起始模板 + 安装前预检（预检只读，不落盘）
  const [sdkOpen, setSdkOpen] = useState(false);
  const [sdkName, setSdkName] = useState('我的套件');
  const [sdkTemplate, setSdkTemplate] = useState<PluginTemplate | null>(null);
  const [sdkTemplateBusy, setSdkTemplateBusy] = useState(false);
  const [sdkTemplateError, setSdkTemplateError] = useState<string | null>(null);
  const [sdkFile, setSdkFile] = useState<File | null>(null);
  const [sdkValidation, setSdkValidation] = useState<PluginValidation | null>(null);
  const [sdkValidateBusy, setSdkValidateBusy] = useState(false);
  const [sdkValidateError, setSdkValidateError] = useState<string | null>(null);
  const sdkInputRef = useRef<HTMLInputElement | null>(null);

  /** 重新读取套件注册表（安装/卸载后调用，页面状态以注册表为准） */
  const refreshInstalled = useCallback(async () => {
    try {
      setInstalledRows(await suiteAPI.list());
    } catch (e) {
      setToast({ severity: 'error', text: `读取已安装套件失败：${toMessage(e)}` });
    }
    try {
      setRootInfo(await suiteAPI.root());
    } catch {
      setRootInfo(null);
    }
  }, []);

  /** 广场：优先真实市场；未配置时开发阶段回落示例目录，并明确标注来源 */
  const refreshCatalog = useCallback(async () => {
    const res = await loadMarketCatalog();
    if (res.items.length > 0) {
      setCatalog(res.items);
      setCatalogSource('market');
      setCatalogError(null);
      return;
    }
    if (res.configured) {
      // 已配置但读取失败 / 索引为空：不拿示例条目冒充真实市场
      setCatalog([]);
      setCatalogSource('none');
      setCatalogError(res.error);
      return;
    }
    setCatalog(mockEnabled ? MOCK_SUITES : []);
    setCatalogSource(mockEnabled ? 'sample' : 'none');
    setCatalogError(null);
  }, []);

  useEffect(() => {
    void refreshCatalog();
    void refreshInstalled();
  }, [refreshCatalog, refreshInstalled]);

  const installedCards = useMemo(() => installedRows.map(toInstalledCard), [installedRows]);
  const installedIds = useMemo(() => new Set(installedRows.map((s) => s.id)), [installedRows]);
  const installedNames = useMemo(() => new Set(installedRows.map((s) => s.name)), [installedRows]);

  const cards = useMemo<SuiteCardView[]>(() => {
    if (view === 'installed') return installedCards;
    return catalog.map((s) => ({
      key: `catalog:${s.id}`,
      id: s.id,
      name: s.name,
      author: s.author,
      desc: s.desc,
      category: s.category,
      skillCount: s.skillCount,
      commandCount: s.commandCount,
      dataLinkCount: s.dataLinkCount,
      version: s.version,
      hot: s.hot,
      publishedAt: s.publishedAt,
      icon: s.icon,
      iconColor: s.iconColor,
      // 安装状态取注册表真实读取（套件包标识或展示名命中即视为已安装）
      installed: installedIds.has(s.id) || installedNames.has(s.name),
      readable: true,
      path: '',
      fileCount: 0,
      commandNames: [],
      source: s.source ?? 'sample',
    }));
  }, [view, catalog, installedCards, installedIds, installedNames]);

  const filtered = useMemo(() => {
    let list = cards;
    if (category !== '全部') list = list.filter((s) => s.category === category);
    return [...list].sort((a, b) =>
      view === 'installed'
        ? b.publishedAt - a.publishedAt // 注册表按安装时间倒序，热度对本地套件无意义
        : sort === 'hot'
          ? b.hot - a.hot
          : b.publishedAt - a.publishedAt,
    );
  }, [cards, view, category, sort]);

  /** 广场安装：市场条目走真实下载 + SHA-256 校验；示例条目如实说明没有下载地址 */
  const handleInstallFromPlaza = async (s: SuiteCardView) => {
    if (s.source !== 'market') {
      setToast({
        severity: 'info',
        text: `「${s.name}」是本地示例条目，没有下载地址；请用「市场源」配置索引，或「添加 → 上传套件」安装本地 .zip`,
      });
      return;
    }
    if (installing) return;
    setInstalling(true);
    try {
      const row = await marketAPI.installSuite(s.id);
      await refreshInstalled();
      await refreshCatalog();
      setView('installed');
      setToast({
        severity: 'success',
        text: `已从在线市场安装「${row.name}」${row.version}：${row.commands.length} 个指令已进入 / 候选`,
      });
    } catch (e) {
      setToast({ severity: 'error', text: toMessage(e) });
    } finally {
      setInstalling(false);
    }
  };

  const handleUninstall = async (s: SuiteCardView) => {
    try {
      const res = await suiteAPI.uninstall(s.id);
      await refreshInstalled();
      setToast({ severity: 'success', text: `已卸载「${s.name}」，套件目录移入回收位置：${res.trashPath}` });
    } catch (e) {
      setToast({ severity: 'error', text: toMessage(e) });
    }
  };

  // 通过对话创建：跳转新任务对话窗口并预置提示词
  const handleCreateByChat = () => {
    setAddAnchor(null);
    navigate(`/?prompt=${encodeURIComponent(SUITE_CREATE_PROMPT)}`);
  };

  // 上传创建：选择/拖拽 zip 文件
  const handlePickFile = (file: File | undefined | null) => {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.zip')) {
      setToast({ severity: 'error', text: '仅支持 .zip 格式的套件包' });
      return;
    }
    setUploadFile(file);
  };

  /** 真实安装：套件包字节 → base64 → 本地通道（桌面端落盘，浏览器通道维护本地台账） */
  const runInstall = async (file: File) => {
    setInstalling(true);
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      const row = await suiteAPI.install({ fileName: file.name, zipBase64: bytesToBase64(bytes) });
      setUploadOpen(false);
      setUploadFile(null);
      if (fileInputRef.current) fileInputRef.current.value = '';
      await refreshInstalled();
      setView('installed');
      setToast({
        severity: 'success',
        text: `已安装「${row.name}」${row.version}：${row.commands.length} 个指令已进入 / 候选，正文随本轮提交下发引擎`,
      });
    } catch (e) {
      setToast({ severity: 'error', text: toMessage(e) });
    } finally {
      setInstalling(false);
    }
  };

  const handleInstallUpload = () => {
    if (!uploadFile || installing) return;
    void runInstall(uploadFile);
  };

  const runTemplate = async () => {
    setSdkTemplateBusy(true);
    setSdkTemplateError(null);
    try {
      setSdkTemplate(await pluginAPI.template(sdkName));
    } catch (e) {
      setSdkTemplate(null);
      setSdkTemplateError(toMessage(e));
    } finally {
      setSdkTemplateBusy(false);
    }
  };

  const openSdk = () => {
    setAddAnchor(null);
    setSdkOpen(true);
    if (!sdkTemplate && !sdkTemplateBusy) void runTemplate();
  };

  /** 预检只读：失败（如浏览器通道不支持）如实展示，不冒充通过 */
  const runValidate = async (file: File) => {
    setSdkValidateBusy(true);
    setSdkValidateError(null);
    setSdkValidation(null);
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      setSdkValidation(
        await pluginAPI.validate({ fileName: file.name, zipBase64: bytesToBase64(bytes) }),
      );
    } catch (e) {
      setSdkValidateError(toMessage(e));
    } finally {
      setSdkValidateBusy(false);
    }
  };

  const handleSdkPick = (file: File | undefined | null) => {
    if (!file) return;
    if (!file.name.toLowerCase().endsWith('.zip')) {
      setToast({ severity: 'error', text: '仅支持 .zip 格式的插件包' });
      return;
    }
    setSdkFile(file);
    void runValidate(file);
  };

  const copySdkFile = async (path: string, content: string) => {
    try {
      await navigator.clipboard.writeText(content);
      setToast({ severity: 'success', text: `已复制 ${path}` });
    } catch {
      setToast({ severity: 'error', text: '复制失败：当前环境不允许访问剪贴板，请手动选中复制' });
    }
  };

  /** 预检通过后把同一个文件带到安装弹窗，避免重复选择 */
  const useCheckedPackage = () => {
    if (!sdkFile) return;
    setUploadFile(sdkFile);
    setSdkOpen(false);
    setUploadOpen(true);
  };

  return (
    <Box sx={{ maxWidth: 1080, mx: 'auto' }}>
      {/* 页头 + 右上角「+ 添加」 */}
      <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 2 }}>
        <Box sx={{ flexGrow: 1, minWidth: 0 }}>
          <Typography variant="h5" sx={{ fontWeight: 700 }}>专家套件</Typography>
          <Typography sx={{ fontSize: '13px', color: '#6B7280', mt: 0.5 }}>
            专家套件是面向角色/行业的工具套件，本地化安装后其指令与正文会随对话提交进入模型执行面；
            专家团队也在本页管理，用来定义负责人与子 Agent 的角色、模型和技能白名单。
          </Typography>
        </Box>
        {view !== 'experts' && (
          <Button
            variant="contained"
            startIcon={<AddIcon />}
            onClick={(e) => setAddAnchor(e.currentTarget)}
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
        )}
      </Box>

      {view !== 'experts' && (
        <Alert severity="info" sx={{ mt: 1.5, fontSize: '12.5px' }}>
          已安装套件的指令会出现在输入框的 / 候选；选中后该指令正文随本轮提交下发引擎。
          正文来源优先取 manifest 里的 content/prompt/body/instructions 或 file，
          也可在套件包内放 commands/&lt;指令名&gt;.md；两者都没有时执行流会明确提示「未提供正文」。
          包内任意位置的 SKILL.md 会作为技能进入执行面。
        </Alert>
      )}

      {/* 添加方式下拉：对话创建 / 上传创建 */}
      <Menu
        anchorEl={addAnchor}
        open={Boolean(addAnchor)}
        onClose={() => setAddAnchor(null)}
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
          onClick={handleCreateByChat}
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
              创建个性化套件，将保存在本地使用
            </Typography>
          </Box>
        </Box>
        <Box
          onClick={() => {
            setAddAnchor(null);
            setUploadFile(null);
            if (fileInputRef.current) fileInputRef.current.value = '';
            setUploadOpen(true);
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
            <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#1F2937' }}>上传套件</Typography>
            <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF', mt: 0.25 }}>
              导入专家套件，仅本地化安装
            </Typography>
          </Box>
        </Box>
        <Box
          onClick={openSdk}
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
              background: '#E6F7F4',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexShrink: 0,
            }}
          >
            <CodeIcon sx={{ fontSize: 16, color: '#0D9488' }} />
          </Box>
          <Box sx={{ minWidth: 0 }}>
            <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#1F2937' }}>插件 SDK</Typography>
            <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF', mt: 0.25 }}>
              生成起始模板，安装前预检插件包
            </Typography>
          </Box>
        </Box>
      </Menu>

      {/* 上传套件弹窗 */}
      <Dialog
        open={uploadOpen}
        onClose={() => {
          if (installing) return;
          setUploadOpen(false);
          setUploadFile(null);
          if (fileInputRef.current) fileInputRef.current.value = '';
        }}
        PaperProps={{ sx: { width: 460, borderRadius: '16px', p: 1 } }}
      >
        <DialogTitle sx={{ fontSize: '16px', fontWeight: 700, pb: 1 }}>上传套件</DialogTitle>
        <DialogContent sx={{ pt: '8px !important' }}>
          {/* 拖拽/点击上传区 */}
          <Box
            component="label"
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              handlePickFile(e.dataTransfer.files?.[0]);
            }}
            sx={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              gap: 1,
              py: 4,
              borderRadius: '12px',
              border: dragOver ? '1.5px dashed #8B5CF6' : '1.5px dashed #D1D5DB',
              background: dragOver ? 'rgba(139, 92, 246, 0.04)' : '#FAFBFC',
              cursor: 'pointer',
              transition: 'border-color 0.15s ease, background 0.15s ease',
              '&:hover': { borderColor: '#8B5CF6' },
            }}
          >
            <CloudUploadOutlinedIcon sx={{ fontSize: 32, color: uploadFile ? '#8B5CF6' : '#9CA3AF' }} />
            <Typography sx={{ fontSize: '13px', color: uploadFile ? '#4C1D95' : '#6B7280', fontWeight: uploadFile ? 600 : 400 }}>
              {uploadFile ? uploadFile.name : '点击或拖拽 .zip 文件到此处'}
            </Typography>
            <input
              ref={fileInputRef}
              type="file"
              accept=".zip"
              hidden
              onChange={(e) => handlePickFile(e.target.files?.[0])}
            />
          </Box>

          {/* 格式要求说明（与本地安装通道的校验口径一致） */}
          <Box sx={{ mt: 2 }}>
            <Typography sx={{ fontSize: '12px', color: '#6B7280', lineHeight: 1.7 }}>
              要求：
            </Typography>
            <Typography sx={{ fontSize: '12px', color: '#9CA3AF', lineHeight: 1.7 }}>
              · ZIP 在根目录或一级目录下含 plugin.json（如 .qoder-plugin/plugin.json）
            </Typography>
            <Typography sx={{ fontSize: '12px', color: '#9CA3AF', lineHeight: 1.7 }}>
              · plugin.json 需含 &apos;name&apos; 字段，commands/skills/dataLinks 可选
            </Typography>
            <Typography sx={{ fontSize: '12px', color: '#9CA3AF', lineHeight: 1.7 }}>
              · 指令正文：commands 项里的 content/prompt/body/instructions 或 file，
              也可放 commands/&lt;指令名&gt;.md
            </Typography>
            <Typography sx={{ fontSize: '12px', color: '#9CA3AF', lineHeight: 1.7 }}>
              · 技能：包内任意位置的 SKILL.md 会被扫描为技能（最多 50 个）
            </Typography>
            {rootInfo?.supported ? (
              <Typography sx={{ fontSize: '12px', color: '#9CA3AF', lineHeight: 1.7 }}>
                · 安装位置：{rootInfo.suitesRoot}（卸载移入 {rootInfo.trashRoot}，不物理删除）
              </Typography>
            ) : (
              <Typography sx={{ fontSize: '12px', color: '#9CA3AF', lineHeight: 1.7 }}>
                · 当前为浏览器通道：套件包解析与本会话台账可用，不写磁盘目录
              </Typography>
            )}
          </Box>

          {/* 安装按钮：未选择文件时禁用 */}
          <Box sx={{ display: 'flex', justifyContent: 'flex-end', mt: 2.5, mb: 1 }}>
            <Button
              variant="contained"
              disabled={!uploadFile || installing}
              onClick={handleInstallUpload}
              sx={{
                bgcolor: uploadFile && !installing ? '#111827' : '#E5E7EB',
                color: uploadFile && !installing ? '#fff' : '#9CA3AF',
                borderRadius: '10px',
                textTransform: 'none',
                fontSize: '13px',
                fontWeight: 600,
                px: 3,
                '&:hover': { bgcolor: '#4F46E5' },
                '&.Mui-disabled': { bgcolor: '#E5E7EB', color: '#9CA3AF' },
              }}
            >
              {installing ? '安装中…' : '安装'}
            </Button>
          </Box>
        </DialogContent>
      </Dialog>

      {/* 插件 SDK：起始模板 + 安装前预检（只读，不改动已安装套件） */}
      <Dialog
        open={sdkOpen}
        onClose={() => {
          if (sdkTemplateBusy || sdkValidateBusy) return;
          setSdkOpen(false);
        }}
        PaperProps={{ sx: { width: 720, maxWidth: 'calc(100vw - 48px)', borderRadius: '16px', p: 1 } }}
      >
        <DialogTitle sx={{ fontSize: '16px', fontWeight: 700, pb: 1 }}>插件 SDK</DialogTitle>
        <DialogContent sx={{ pt: '8px !important' }}>
          <Typography sx={{ fontSize: '12.5px', color: '#6B7280', lineHeight: 1.7 }}>
            安装前把套件包读懂：模板给出可直接修改的起始文件；预检只读、不落盘、不改动已安装套件。
            本应用只消费 commands 与包内 SKILL.md，清单里的 mcpServers / hooks / agents 等字段会被列出但不会执行。
          </Typography>

          {/* 1. 起始模板 */}
          <Box sx={{ mt: 2 }}>
            <Typography sx={{ fontSize: '13px', fontWeight: 700, color: '#111827' }}>
              1. 起始模板
            </Typography>
            <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', mt: 1 }}>
              <TextField
                size="small"
                label="套件名"
                value={sdkName}
                onChange={(e) => setSdkName(e.target.value)}
                sx={{ flexGrow: 1, '& .MuiInputBase-input': { fontSize: '13px' } }}
              />
              <Button
                variant="outlined"
                onClick={() => void runTemplate()}
                disabled={sdkTemplateBusy}
                sx={{ textTransform: 'none', fontSize: '12.5px', borderRadius: '9px', flexShrink: 0 }}
              >
                {sdkTemplateBusy ? '生成中…' : '生成模板'}
              </Button>
            </Box>
            {sdkTemplateError && (
              <Alert severity="error" sx={{ mt: 1, fontSize: '12.5px' }}>{sdkTemplateError}</Alert>
            )}
            {sdkTemplate && (
              <Box sx={{ mt: 1.25 }}>
                {sdkTemplate.notes.map((n) => (
                  <Typography key={n} sx={{ fontSize: '11.5px', color: '#9CA3AF', lineHeight: 1.7 }}>
                    · {n}
                  </Typography>
                ))}
                {sdkTemplate.files.map((f) => (
                  <Box
                    key={f.path}
                    sx={{ mt: 1, border: '1px solid #EEF0F2', borderRadius: '10px', overflow: 'hidden' }}
                  >
                    <Box
                      sx={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 1,
                        px: 1.25,
                        py: 0.6,
                        background: '#FAFBFC',
                        borderBottom: '1px solid #EEF0F2',
                      }}
                    >
                      <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#374151', flexGrow: 1, fontFamily: 'monospace' }}>
                        {f.path}
                      </Typography>
                      <Tooltip title="复制此文件内容">
                        <Box
                          onClick={() => void copySdkFile(f.path, f.content)}
                          sx={{ display: 'flex', alignItems: 'center', gap: 0.4, cursor: 'pointer', color: '#4F46E5' }}
                        >
                          <ContentCopyIcon sx={{ fontSize: 14 }} />
                          <Typography sx={{ fontSize: '11.5px' }}>复制</Typography>
                        </Box>
                      </Tooltip>
                    </Box>
                    <Box
                      component="pre"
                      sx={{
                        m: 0,
                        px: 1.25,
                        py: 1,
                        maxHeight: 150,
                        overflow: 'auto',
                        fontSize: '11.5px',
                        lineHeight: 1.6,
                        color: '#374151',
                        background: '#FFFFFF',
                        whiteSpace: 'pre-wrap',
                        wordBreak: 'break-all',
                      }}
                    >
                      {f.content}
                    </Box>
                  </Box>
                ))}
              </Box>
            )}
          </Box>

          <Divider sx={{ my: 2 }} />

          {/* 2. 安装前预检 */}
          <Box>
            <Typography sx={{ fontSize: '13px', fontWeight: 700, color: '#111827' }}>
              2. 安装前预检
            </Typography>
            <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF', lineHeight: 1.7, mt: 0.5 }}>
              读取 ZIP 目录与 plugin.json，逐条说明指令正文来自哪里、哪些字段不会执行、哪些问题会阻断安装。
            </Typography>
            <Box
              component="label"
              sx={{
                mt: 1,
                display: 'flex',
                alignItems: 'center',
                gap: 1,
                px: 1.5,
                py: 1.5,
                borderRadius: '12px',
                border: '1.5px dashed #D1D5DB',
                background: '#FAFBFC',
                cursor: sdkValidateBusy ? 'default' : 'pointer',
                transition: 'border-color 0.15s ease',
                '&:hover': { borderColor: sdkValidateBusy ? '#D1D5DB' : '#0D9488' },
              }}
            >
              {sdkValidateBusy ? (
                <CircularProgress size={18} sx={{ color: '#0D9488' }} />
              ) : (
                <CloudUploadOutlinedIcon sx={{ fontSize: 22, color: sdkFile ? '#0D9488' : '#9CA3AF' }} />
              )}
              <Typography sx={{ fontSize: '12.5px', color: sdkFile ? '#0F766E' : '#6B7280', flexGrow: 1 }}>
                {sdkValidateBusy
                  ? '正在读取插件包…'
                  : sdkFile
                    ? `${sdkFile.name}（点击可重新选择）`
                    : '点击选择要预检的 .zip'}
              </Typography>
              <input
                ref={sdkInputRef}
                type="file"
                accept=".zip"
                hidden
                disabled={sdkValidateBusy}
                onChange={(e) => handleSdkPick(e.target.files?.[0])}
              />
            </Box>

            {sdkValidateError && (
              <Alert severity="error" sx={{ mt: 1, fontSize: '12.5px' }}>{sdkValidateError}</Alert>
            )}

            {sdkValidation && (
              <Box sx={{ mt: 1.25 }}>
                <Alert
                  severity={sdkValidation.installable ? 'success' : 'error'}
                  sx={{ fontSize: '12.5px' }}
                  icon={sdkValidation.installable ? <CheckCircleOutlineIcon /> : <ErrorOutlineIcon />}
                >
                  {sdkValidation.installable
                    ? `可安装：${sdkValidation.manifest.name ?? '未命名'}${sdkValidation.manifest.version ? ` ${sdkValidation.manifest.version}` : ''}`
                    : `存在 ${sdkValidation.errors.length} 个阻断项，修正后才能安装`}
                  {' · '}
                  {sdkValidation.commands.length} 条指令 · {sdkValidation.skillFiles.length} 个技能文件 ·{' '}
                  {sdkValidation.fileCount} 个文件 / {(sdkValidation.totalBytes / 1024).toFixed(1)} KB
                  {sdkValidation.pluginId ? ` · 标识 ${sdkValidation.pluginId}` : ''}
                </Alert>

                {sdkValidation.errors.length > 0 && (
                  <Box sx={{ mt: 1 }}>
                    {sdkValidation.errors.map((e) => (
                      <Typography key={e} sx={{ fontSize: '12px', color: '#B91C1C', lineHeight: 1.7 }}>
                        · {e}
                      </Typography>
                    ))}
                  </Box>
                )}
                {sdkValidation.warnings.length > 0 && (
                  <Box sx={{ mt: 1 }}>
                    {sdkValidation.warnings.map((w) => (
                      <Typography key={w} sx={{ fontSize: '12px', color: '#B45309', lineHeight: 1.7 }}>
                        · {w}
                      </Typography>
                    ))}
                  </Box>
                )}

                {sdkValidation.commands.length > 0 && (
                  <Box sx={{ mt: 1.25, border: '1px solid #EEF0F2', borderRadius: '10px', overflow: 'hidden' }}>
                    {sdkValidation.commands.map((c, i) => {
                      const sourceLabel =
                        c.bodySource === 'content'
                          ? '清单 content'
                          : c.bodySource === 'file'
                            ? '清单 file'
                            : c.bodySource === 'convention'
                              ? '约定文件'
                              : '未提供正文';
                      return (
                        <Box
                          key={`${c.name}-${i}`}
                          sx={{
                            px: 1.25,
                            py: 0.8,
                            borderTop: i === 0 ? 'none' : '1px solid #F3F4F6',
                            display: 'flex',
                            gap: 1,
                            alignItems: 'baseline',
                          }}
                        >
                          <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#111827', minWidth: 90 }}>
                            /{c.name}
                          </Typography>
                          <Typography
                            sx={{
                              fontSize: '11.5px',
                              color: c.bodySource === 'missing' ? '#B45309' : '#6B7280',
                              flexShrink: 0,
                            }}
                          >
                            {sourceLabel} · {c.bodyChars} 字
                          </Typography>
                          <Typography
                            sx={{
                              fontSize: '11.5px',
                              color: c.error ? '#B91C1C' : '#9CA3AF',
                              flexGrow: 1,
                              minWidth: 0,
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                            }}
                          >
                            {c.error ?? c.preview}
                          </Typography>
                        </Box>
                      );
                    })}
                  </Box>
                )}

                {sdkValidation.skillFiles.length > 0 && (
                  <Typography sx={{ fontSize: '11.5px', color: '#6B7280', mt: 1, lineHeight: 1.7 }}>
                    技能文件：{sdkValidation.skillFiles.join('、')}
                  </Typography>
                )}
                {sdkValidation.ignoredFields.length > 0 && (
                  <Typography sx={{ fontSize: '11.5px', color: '#B45309', mt: 0.5, lineHeight: 1.7 }}>
                    不会执行的清单字段：{sdkValidation.ignoredFields.join(' / ')}
                  </Typography>
                )}
              </Box>
            )}
          </Box>

          <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mt: 2.5, mb: 1, gap: 1 }}>
            <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF' }}>
              预检不写盘；安装仍由同一套校验拒绝坏包。
            </Typography>
            <Box sx={{ display: 'flex', gap: 1, flexShrink: 0 }}>
              <Button
                onClick={() => setSdkOpen(false)}
                disabled={sdkTemplateBusy || sdkValidateBusy}
                sx={{ textTransform: 'none', fontSize: '13px', borderRadius: '10px' }}
              >
                关闭
              </Button>
              <Button
                variant="contained"
                disabled={!sdkValidation?.installable}
                onClick={useCheckedPackage}
                sx={{
                  bgcolor: sdkValidation?.installable ? '#111827' : '#E5E7EB',
                  color: sdkValidation?.installable ? '#fff' : '#9CA3AF',
                  borderRadius: '10px',
                  textTransform: 'none',
                  fontSize: '13px',
                  fontWeight: 600,
                  px: 2.5,
                  '&:hover': { bgcolor: '#4F46E5' },
                  '&.Mui-disabled': { bgcolor: '#E5E7EB', color: '#9CA3AF' },
                }}
              >
                用此包继续安装
              </Button>
            </Box>
          </Box>
        </DialogContent>
      </Dialog>

      {/* 精选横幅 */}
      <Box
        sx={{
          mt: 2.5,
          mb: 3,
          p: 2.5,
          borderRadius: '16px',
          background: 'linear-gradient(120deg, rgba(16, 185, 129, 0.10) 0%, rgba(6, 182, 212, 0.10) 60%, rgba(139, 92, 246, 0.08) 100%)',
          border: '1px solid rgba(16, 185, 129, 0.18)',
          display: 'flex',
          alignItems: 'center',
          gap: 2,
        }}
      >
        <Box
          sx={{
            width: 44,
            height: 44,
            borderRadius: '12px',
            background: 'linear-gradient(135deg, #10B981 0%, #06B6D4 100%)',
            boxShadow: '0 4px 12px rgba(16, 185, 129, 0.3)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
          }}
        >
          <AccountBalanceIcon sx={{ color: '#fff', fontSize: 22 }} />
        </Box>
        <Box sx={{ flexGrow: 1, minWidth: 0 }}>
          <Typography sx={{ fontSize: '15px', fontWeight: 700, color: '#065F46' }}>金融行业</Typography>
          <Typography sx={{ fontSize: '12.5px', color: '#4B5563', mt: 0.25 }}>
            风控、研报与合规流程一键接入对话；用套件固化常用模板与数据源。
          </Typography>
        </Box>
        <Tooltip title="跳到新任务对话，预置套件创建提示词">
          <Box
            onClick={handleCreateByChat}
            sx={{
              px: 1.75,
              py: 0.85,
              borderRadius: '10px',
              background: '#FFFFFF',
              border: '1px solid rgba(16, 185, 129, 0.25)',
              display: 'flex',
              alignItems: 'center',
              gap: 0.5,
              cursor: 'pointer',
              flexShrink: 0,
              transition: 'box-shadow 0.15s ease, transform 0.15s ease',
              '&:hover': { boxShadow: '0 4px 12px rgba(16, 185, 129, 0.18)', transform: 'translateY(-1px)' },
            }}
          >
            <AddIcon sx={{ fontSize: 15, color: '#059669' }} />
            <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#059669', whiteSpace: 'nowrap' }}>
              让任我行帮我创建
            </Typography>
          </Box>
        </Tooltip>
      </Box>

      {/* 套件广场 / 已安装（已安装数量取注册表真实条数） */}
      <Box sx={{ display: 'flex', gap: 3, borderBottom: '1px solid #F3F4F6' }}>
        {([
          ['plaza', `${catalogSource === 'market' ? '在线市场' : '示例目录'} ${catalog.length}`],
          ['installed', `已安装 ${installedRows.length}`],
          ['experts', '专家团队'],
        ] as const).map(([key, label]) => (
          <Box
            key={key}
            onClick={() => changeView(key)}
            sx={{
              pb: 1,
              cursor: 'pointer',
              borderBottom: view === key ? '2px solid #111827' : '2px solid transparent',
            }}
          >
            <Typography sx={{ fontSize: '13.5px', fontWeight: view === key ? 700 : 500, color: view === key ? '#111827' : '#6B7280' }}>
              {label}
            </Typography>
          </Box>
        ))}
        <Box sx={{ flexGrow: 1 }} />
        {/* 排序：仅广场示例目录有意义，已安装页签固定按安装时间倒序。
            「推荐」按策展次序排，不叫「热门」—— 我们没有真实安装量数据，别装作有 */}
        {view === 'plaza' && (
          <Box sx={{ display: 'flex', gap: 1, pb: 0.5, alignItems: 'center' }}>
            <Button
              size="small"
              startIcon={<PublicIcon sx={{ fontSize: 15 }} />}
              onClick={() => setMarketOpen(true)}
              sx={{
                textTransform: 'none',
                fontSize: '12px',
                color: '#4F46E5',
                minWidth: 0,
                px: 1,
              }}
            >
              市场源
            </Button>
            {([
              ['hot', '推荐'],
              ['latest', '最新'],
            ] as const).map(([key, label]) => (
              <Box
                key={key}
                onClick={() => setSort(key)}
                sx={{
                  px: 1.25,
                  py: 0.35,
                  borderRadius: '8px',
                  cursor: 'pointer',
                  background: sort === key ? 'rgba(99, 102, 241, 0.10)' : 'transparent',
                  transition: 'background 0.15s ease',
                  '&:hover': { background: sort === key ? 'rgba(99, 102, 241, 0.10)' : 'rgba(99, 102, 241, 0.05)' },
                }}
              >
                <Typography sx={{ fontSize: '12px', fontWeight: sort === key ? 700 : 500, color: sort === key ? '#4F46E5' : '#6B7280' }}>
                  {label}
                </Typography>
              </Box>
            ))}
          </Box>
        )}
        {view === 'installed' && rootInfo?.supported && (
          <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF', pb: 0.75, alignSelf: 'flex-end' }}>
            目录：{rootInfo.suitesRoot}
          </Typography>
        )}
      </Box>

      {/* 分类筛选：仅套件广场有意义 */}
      {view === 'plaza' && (
        <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', mt: 2, mb: 2 }}>
          {CATEGORIES.map((c) => {
            const active = category === c;
            return (
              <Box
                key={c}
                onClick={() => setCategory(c)}
                sx={{
                  px: 1.5,
                  py: 0.45,
                  borderRadius: '999px',
                  cursor: 'pointer',
                  background: active ? '#111827' : '#FFFFFF',
                  border: active ? '1px solid #111827' : '1px solid #E5E7EB',
                  transition: 'all 0.15s ease',
                  '&:hover': { borderColor: active ? '#111827' : '#9CA3AF' },
                }}
              >
                <Typography sx={{ fontSize: '12px', fontWeight: active ? 600 : 400, color: active ? '#fff' : '#4B5563' }}>
                  {c}
                </Typography>
              </Box>
            );
          })}
        </Box>
      )}

      {/* 广场来源如实标注：在线市场 / 本地示例 / 未配置 */}
      {view === 'plaza' && catalogError && (
        <Alert severity="error" sx={{ mt: 1.5, fontSize: '12.5px' }}>
          {catalogError}
        </Alert>
      )}
      {view === 'plaza' && !catalogError && catalogSource === 'market' && (
        <Alert severity="success" sx={{ mt: 1.5, fontSize: '12.5px' }}>
          条目来自你配置的在线套件市场索引；安装会下载套件包并按索引声明的 SHA-256 校验，校验失败不会产生半成品。
        </Alert>
      )}
      {view === 'plaza' && !catalogError && catalogSource === 'sample' && (
        <Alert severity="info" sx={{ mt: 1.5, fontSize: '12.5px' }}>
          这里是本地示例目录，不是在线套件市场；示例条目没有下载地址。可在「市场源」配置在线索引，或用「添加 → 上传套件」导入本地 .zip。
        </Alert>
      )}
      {view === 'plaza' && !catalogError && catalogSource === 'none' && (
        <Alert severity="info" sx={{ mt: 1.5, fontSize: '12.5px' }}>
          尚未配置在线套件市场索引：点「市场源」填写索引地址，或用「添加 → 上传套件」导入本地 .zip。
        </Alert>
      )}

      {/* 套件卡片列表 / 专家团队 */}
      {view === 'experts' ? (
        <Box sx={{ mt: 2 }}>
          <ExpertsPage embedded />
        </Box>
      ) : filtered.length === 0 ? (
        <Box sx={{ p: 6, textAlign: 'center' }}>
          <Typography sx={{ fontSize: '13px', color: '#9CA3AF' }}>
            {view === 'installed'
              ? '暂无已安装套件，点右上「添加 → 上传套件」导入本地 .zip'
              : catalogSource === 'market'
                ? '在线市场索引里没有匹配的套件，可切换分类或刷新市场源'
                : catalogSource === 'sample'
                  ? '示例目录里没有匹配的套件；可在「市场源」配置在线索引，或用「添加 → 上传套件」安装本地 .zip'
                  : '尚未配置在线套件市场索引；点「市场源」填写地址，或用「添加 → 上传套件」安装本地 .zip'}
          </Typography>
          {view === 'installed' && installedRows.length === 0 && !rootInfo?.supported && (
            <Typography sx={{ fontSize: '12px', color: '#B5BAC1', mt: 0.75 }}>
              当前为浏览器通道：安装的套件记在本地台账，不写磁盘目录
            </Typography>
          )}
        </Box>
      ) : (
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
          {filtered.map((s) => {
            const Icon = s.icon;
            return (
              <Box
                key={s.key}
                sx={{
                  display: 'flex',
                  gap: 2,
                  border: '1px solid #EEF0F2',
                  borderRadius: '14px',
                  p: 2,
                  background: '#FFFFFF',
                  transition: 'box-shadow 0.18s ease, transform 0.15s ease',
                  '&:hover': { boxShadow: '0 6px 20px rgba(15, 23, 42, 0.07)', transform: 'translateY(-1px)' },
                }}
              >
                {/* 图标 */}
                <Box
                  sx={{
                    width: 44,
                    height: 44,
                    borderRadius: '12px',
                    background: `${s.iconColor}1A`,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    flexShrink: 0,
                  }}
                >
                  <Icon sx={{ fontSize: 22, color: s.iconColor }} />
                </Box>

                {/* 主体 */}
                <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                  <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1 }}>
                    <Typography sx={{ fontSize: '14.5px', fontWeight: 700, color: '#111827' }}>{s.name}</Typography>
                    <Typography sx={{ fontSize: '12px', color: '#9CA3AF' }}>{s.author}</Typography>
                  </Box>
                  <Typography
                    sx={{
                      fontSize: '12.5px',
                      color: '#6B7280',
                      mt: 0.5,
                      lineHeight: 1.6,
                      display: '-webkit-box',
                      WebkitLineClamp: 2,
                      WebkitBoxOrient: 'vertical',
                      overflow: 'hidden',
                    }}
                  >
                    {s.desc}
                  </Typography>
                  {/* 统计行：已安装行的数字来自磁盘实测；广场示例行的技能数/热度是策展占位，
                      不能装作实测值，改为如实标出来源缺失 */}
                  <Box sx={{ display: 'flex', gap: 1.5, mt: 1, flexWrap: 'wrap' }}>
                    {s.source !== 'sample' ? (
                      <>
                        <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF' }}>{s.skillCount} 个技能</Typography>
                        {s.commandCount > 0 && (
                          <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF' }}>{s.commandCount} 个指令</Typography>
                        )}
                        {s.dataLinkCount > 0 && (
                          <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF' }}>{s.dataLinkCount} 个数据连接</Typography>
                        )}
                        {s.source === 'market' && (
                          <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF' }}>
                            在线市场 · 数字取索引声明
                          </Typography>
                        )}
                      </>
                    ) : (
                      <Typography sx={{ fontSize: '11.5px', color: '#B45309' }}>
                        示例目录 · 未接入远端套件源
                      </Typography>
                    )}
                    {s.source === 'installed' && s.readable && s.commandNames.length > 0 && (
                      <Typography sx={{ fontSize: '11.5px', color: '#B45309' }}>
                        输入 {s.commandNames.slice(0, 3).map((n) => `/${n}`).join(' ')}
                        {s.commandNames.length > 3 ? ' …' : ''} 可直接调用
                      </Typography>
                    )}
                    {s.source === 'installed' && s.readable && (
                      <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF' }}>
                        {s.fileCount} 个文件 · 安装于 {new Date(s.publishedAt).toLocaleString()}
                      </Typography>
                    )}
                  </Box>
                  {!s.readable && (
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, mt: 1 }}>
                      <WarningAmberIcon sx={{ fontSize: 14, color: '#F59E0B' }} />
                      <Typography sx={{ fontSize: '11.5px', color: '#B45309' }}>
                        套件目录 {s.id} 的 manifest.json 读不出，请卸载后重新上传套件包
                      </Typography>
                    </Box>
                  )}
                </Box>

                {/* 版本 + 安装/卸载 */}
                <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', justifyContent: 'space-between', flexShrink: 0, gap: 1 }}>
                  {/* 版本号：已安装行读自 plugin.json，市场行读自索引；示例目录的版本是占位，不显示 */}
                  {s.source !== 'sample' && s.version && (
                    <Typography sx={{ fontSize: '11px', color: '#9CA3AF' }}>{s.version}</Typography>
                  )}
                  {s.installed ? (
                    s.source === 'installed' ? (
                      <Tooltip title="卸载：套件目录移入回收位置，不物理删除">
                        <Box
                          onClick={() => void handleUninstall(s)}
                          sx={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: 0.5,
                            px: 1.25,
                            py: 0.5,
                            borderRadius: '8px',
                            border: '1px solid #EEF0F2',
                            cursor: 'pointer',
                            transition: 'all 0.15s ease',
                            '&:hover': { borderColor: '#FCA5A5', background: '#FEF2F2' },
                          }}
                        >
                          <DeleteOutlineIcon sx={{ fontSize: 15, color: '#6B7280' }} />
                          <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#6B7280' }}>卸载</Typography>
                        </Box>
                      </Tooltip>
                    ) : (
                      <Box sx={{ px: 1.5, py: 0.5, borderRadius: '8px', background: '#E6F7F4' }}>
                        <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#0D9488' }}>已安装</Typography>
                      </Box>
                    )
                  ) : (
                    <Tooltip
                      title={
                        s.source === 'market'
                          ? '从在线市场下载并安装（SHA-256 校验通过后才落盘）'
                          : '示例条目没有下载地址，点击可查看安装方式'
                      }
                    >
                      <Box
                        onClick={() => void handleInstallFromPlaza(s)}
                        sx={{
                          width: 30,
                          height: 30,
                          borderRadius: '9px',
                          background: '#111827',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'center',
                          cursor: 'pointer',
                          transition: 'background 0.15s ease, transform 0.12s ease',
                          '&:hover': { background: '#4F46E5', transform: 'scale(1.06)' },
                        }}
                      >
                        <AddIcon sx={{ fontSize: 17, color: '#fff' }} />
                      </Box>
                    </Tooltip>
                  )}
                </Box>
              </Box>
            );
          })}
        </Box>
      )}

      {/* 在线套件市场：索引可配置，安装由 Rust 下载校验后走本地套件安装 */}
      <MarketBrowser
        open={marketOpen}
        kind="suite"
        onClose={() => setMarketOpen(false)}
        onInstalled={() => {
          void refreshInstalled();
          void refreshCatalog();
        }}
      />

      {/* 结果提示 toast（安装/卸载的真实回执与拒绝原因，不静默） */}
      <Snackbar
        open={!!toast}
        autoHideDuration={toast?.severity === 'error' ? 6500 : 3500}
        onClose={() => setToast(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert severity={toast?.severity ?? 'info'} onClose={() => setToast(null)} sx={{ fontSize: '13px' }}>
          {toast?.text}
        </Alert>
      </Snackbar>
    </Box>
  );
};

export default ExpertSuitePage;
