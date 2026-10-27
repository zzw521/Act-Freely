/**
 * 通天办公 - 工作台页面（设计图对齐版）
 *
 * 三栏布局（侧边栏由 MainLayout/Sidebar 提供）：
 * - 中间聊天区：头部（任务标题 + 状态徽标 + 时间）+ 消息流 + 输入区
 * - 右侧任务监控：TaskMonitor（待办/产物/工作目录/技能/MCP）
 *
 * 两阶段协议：submit → plan → pending_review（TaskMonitor 待办区批准）→ 执行 → 完成
 */

import React, { useEffect, useCallback } from 'react';
import { useSearchParams, useNavigate } from 'react-router-dom';
import { Box, Chip, Snackbar, Alert, Tooltip, Typography, Dialog, DialogTitle, DialogContent, IconButton, Select, MenuItem } from '@mui/material';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import HourglassEmptyIcon from '@mui/icons-material/HourglassEmpty';
import PauseCircleOutlineIcon from '@mui/icons-material/PauseCircleOutline';
import CancelOutlinedIcon from '@mui/icons-material/CancelOutlined';
import RefreshIcon from '@mui/icons-material/Refresh';
import OpenInNewIcon from '@mui/icons-material/OpenInNew';
import ChatStream from '../../components/ChatStream';
import ChatInput from '../../components/ChatInput';
import Panda from '../../components/Panda';
import TaskMonitor from '../../components/TaskMonitor';
import ModeCanvas from '../../components/ModeCanvas';
import TaskFindBar from '../../components/TaskFindBar';
import ViewSidebarIcon from '@mui/icons-material/ViewSidebar';
import { useWorkbenchStore } from '../../store/workbenchStore';
import { useAppSettings } from '../../lib/use-app-settings';
import { useWorkMode } from '../../lib/use-work-mode';
import { MODE_CANVAS_LABELS } from '../../lib/mode-canvas';
import { systemAPI } from '../../lib/api';
import { isTauri } from '../../lib/tauri-api';
import { useAppStore } from '../../stores/useAppStore';
import { useAuthStore } from '../../stores/useAuthStore';
import { useWorkspaceStore } from '../../store/workspaceStore';
import { useShallow } from 'zustand/react/shallow';
import { TaskStatus, StepStatus } from '../../../shared/types';
import { errorTextOf } from '../../../shared/error-messages';
import { useI18n } from '../../i18n';
import coverHomestay from '../../assets/showcase/homestay.webp';
import coverInfluencerDashboard from '../../assets/showcase/influencer-dashboard.webp';
import coverEcommerceDashboard from '../../assets/showcase/ecommerce-dashboard.webp';
import coverMonthlyReview from '../../assets/showcase/monthly-review.webp';
import coverCourseSite from '../../assets/showcase/course-site.webp';
import coverAiCourseLanding from '../../assets/showcase/ai-course-landing.webp';
import coverProductVisuals from '../../assets/showcase/product-visuals.webp';
import coverSalesReport from '../../assets/showcase/sales-report.webp';
import coverDunhuangArticle from '../../assets/showcase/dunhuang-article.webp';

/** 对话宽度：偏好设置「对话宽度」真实生效（标准/宽/全宽） */
function resolveChatMaxWidth(chatWidth: string): number | string {
  if (chatWidth === '全宽') return '100%';
  if (chatWidth === '宽') return 1100;
  return 860; // 标准
}

/** 任务状态徽标（对齐设计图：绿色「已完成 N 个步骤」等） */
const StatusBadge: React.FC<{
  taskStatus: TaskStatus | null;
  isLoading: boolean;
  stepCount: number;
  doneSteps: number;
}> = ({ taskStatus, isLoading, stepCount, doneSteps }) => {
  const { t: tr } = useI18n();
  if (isLoading || [TaskStatus.EXECUTING, TaskStatus.PLANNING, TaskStatus.QUEUED, TaskStatus.DELIVERING, TaskStatus.RETRY_PENDING].includes(taskStatus as TaskStatus)) {
    return (
      <Chip
        size="small"
        icon={<HourglassEmptyIcon sx={{ fontSize: 14 }} />}
        label={tr(taskStatus === TaskStatus.QUEUED ? '排队中' : '进行中')}
        sx={{ bgcolor: '#FEF3C7', color: '#B45309', fontSize: '12px', '& .MuiChip-icon': { color: '#B45309' } }}
      />
    );
  }
  switch (taskStatus) {
    case TaskStatus.PENDING_REVIEW:
      return <Chip size="small" label={tr('待审阅')} sx={{ bgcolor: '#E0F2FE', color: '#0369A1', fontSize: '12px' }} />;
    case TaskStatus.PAUSED:
      return (
        <Chip
          size="small"
          icon={<PauseCircleOutlineIcon sx={{ fontSize: 14 }} />}
          label={tr('已暂停')}
          sx={{ bgcolor: '#F3F4F6', color: 'var(--tt-text-3)', fontSize: '12px', '& .MuiChip-icon': { color: 'var(--tt-text-3)' } }}
        />
      );
    case TaskStatus.BUDGET_PAUSED:
      return <Chip size="small" label={tr('预算暂停')} sx={{ bgcolor: '#FEE2E2', color: '#B91C1C', fontSize: '12px' }} />;
    case TaskStatus.CANCELLED:
      return (
        <Chip
          size="small"
          icon={<CancelOutlinedIcon sx={{ fontSize: 14 }} />}
          label={tr('已取消')}
          sx={{ bgcolor: '#F3F4F6', color: 'var(--tt-text-3)', fontSize: '12px', '& .MuiChip-icon': { color: 'var(--tt-text-3)' } }}
        />
      );
    case TaskStatus.FAILED:
      return <Chip size="small" label={tr('失败')} sx={{ bgcolor: '#FEE2E2', color: '#B91C1C', fontSize: '12px' }} />;
    case TaskStatus.DONE:
      return (
        <Chip
          size="small"
          icon={<CheckCircleIcon sx={{ fontSize: 14 }} />}
          label={tr('已完成 {count} 个步骤', { count: doneSteps })}
          sx={{ bgcolor: '#E6F7F4', color: '#0D9488', fontSize: '12px', '& .MuiChip-icon': { color: '#0D9488' } }}
        />
      );
    default:
      return stepCount > 0 ? null : <Chip size="small" label={tr('新任务')} sx={{ bgcolor: '#F3F4F6', color: 'var(--tt-text-3)', fontSize: '12px' }} />;
  }
};

/** 时段问候 */
function getGreeting(): string {
  const h = new Date().getHours();
  if (h < 6) return '凌晨好';
  if (h < 9) return '早上好';
  if (h < 12) return '上午好';
  if (h < 14) return '中午好';
  if (h < 18) return '下午好';
  return '晚上好';
}

// 推荐案例池（每批 3 个，「换一换」循环切换；点击直接提交对应 prompt）
// 提示词写清交付物、结构、字段与示例数据口径，避免只给一句笼统目标。
const SHOWCASE_POOL = [
  {
    title: '民宿网站官网',
    subtitle: '景点 公寓 营地 旅程',
    cover: coverHomestay,
    background: 'linear-gradient(160deg, #93C5FD 0%, #2563EB 100%)',
    prompt: '设计一个高端民宿品牌官网的完整首页原型：首屏用大幅山湖视觉搭配品牌主张与「立即预订」按钮；下方依次为房型推荐（3 个房型卡片，含面积、可住人数、价格）、周边景点、营地与旅程四条产品线、房客评价、预订须知与页脚。风格温暖自然、留白充足，主色为奶油白与森林绿，适配桌面和移动端。输出单文件 HTML（内联 CSS，可直接在浏览器打开），占位图用 CSS 渐变或内联 SVG，不引用外部链接。',
  },
  {
    title: '达人投放管理看板',
    subtitle: 'Excel · 仪表盘',
    cover: coverInfluencerDashboard,
    background: 'linear-gradient(160deg, #F9FAFB 0%, #E5E7EB 100%)',
    prompt: '创建一份达人投放管理 Excel 看板（.xlsx），包含「达人台账」「投放明细」「月度汇总」三个工作表。字段至少覆盖达人名称、平台、内容形式、粉丝量、曝光量、点击量、互动量、转化数、投放成本、CPM、CPE、ROI。月度汇总按平台和月份透视，配 KPI 卡片与折线图、柱状图。指标用公式联动，示例数据 20 行并在表头注明「示例数据，请替换」。',
  },
  {
    title: '电商看板构建',
    subtitle: 'Excel · 核心指标',
    cover: coverEcommerceDashboard,
    background: 'linear-gradient(160deg, #4B5563 0%, #111827 100%)',
    prompt: '构建一份电商经营数据看板（.xlsx），覆盖 GMV、订单量、客单价、退款率、毛利率五项核心指标，按日、周、月三个维度汇总。包含渠道对比、品类销售占比、TOP10 商品三个分析区，配公式和图表联动。示例数据 30 天并标注为示例，指标口径在单独说明页写清楚。',
  },
  {
    title: '月度经营复盘',
    subtitle: '数据报表 · 同环比',
    cover: coverMonthlyReview,
    background: 'linear-gradient(160deg, #F59E0B 0%, #D97706 100%)',
    prompt: '生成一份月度店铺经营复盘报告（.docx）：先给本月核心指标总览（GMV、订单量、客单价、退款率、毛利率），再做环比与同比分析、渠道和品类拆解、TOP 商品盘点，最后给出问题诊断与下月行动项。用表格和要点呈现，写清数据口径，示例数据需明确标注为示例。',
  },
  {
    title: '课程资源网站生成',
    subtitle: '数据结构 与 算法',
    cover: coverCourseSite,
    background: 'linear-gradient(160deg, #8B5CF6 0%, #6D28D9 100%)',
    prompt: '生成一个《数据结构与算法》课程资源网站原型：包含课程介绍、章节大纲（数组、链表、栈与队列、树、图、排序、查找）、每章知识点、复杂度速查表、作业示例和在线练习入口。支持章节锚点跳转与代码块高亮样式。输出单文件 HTML（内联 CSS/JS，可直接打开），代码示例用 Python 或 JavaScript。',
  },
  {
    title: 'AI 副业课落地',
    subtitle: '30 天 从 0 到接单',
    cover: coverAiCourseLanding,
    background: 'linear-gradient(160deg, #FB923C 0%, #EA580C 100%)',
    prompt: '制作一个「AI 副业 30 天从 0 到接单」课程落地页：包含痛点开场、4 周 12 节课程大纲、讲师与学员案例、交付物清单、价格与权益对比、常见问题、报名表单和行动按钮。风格明亮可信，主色橙色配米白，移动端优先。输出单文件 HTML（内联 CSS，可直接打开）。',
  },
  {
    title: '商品视觉五连套图',
    subtitle: '主图 · 详情页',
    cover: coverProductVisuals,
    background: 'linear-gradient(160deg, #EC4899 0%, #BE185D 100%)',
    prompt: '设计一套电商商品视觉五连套图方案，包含主图 1 张、场景图 2 张、卖点图 1 张、详情长图 1 张。统一色系与字体：主图突出商品和核心利益点，场景图展示使用情境，卖点图用图标加短句，详情长图覆盖参数、对比和售后。输出可直接用于设计的 HTML 预览稿（内联 CSS，标注每张图尺寸）。',
  },
  {
    title: '本周销售战报',
    subtitle: '数据汇总 · 表格',
    cover: coverSalesReport,
    background: 'linear-gradient(160deg, #10B981 0%, #047857 100%)',
    prompt: '汇总本周销售数据并生成销售战报（.xlsx）：按日期、渠道、品类、销售员四个维度统计订单数、销售额、客单价和退货额；包含本周与上周环比、目标完成率、TOP5 销售员、TOP10 商品，用条件格式突出异常值。示例数据标注为示例，统计口径写清楚。',
  },
  {
    title: '敦煌艺术科普长文',
    subtitle: '沉浸式 · 写作',
    cover: coverDunhuangArticle,
    background: 'linear-gradient(160deg, #0EA5E9 0%, #0369A1 100%)',
    prompt: '撰写一篇《敦煌艺术与丝路文明》科普长文：从莫高窟开凿历史讲起，覆盖壁画题材、彩塑风格、飞天形象演变、藏经洞发现与文物流散、丝路贸易与多元文化交融。分 5 个小节，以要点式段落展开，年代、人物、洞窟编号必须准确可查，文末附 5 条延伸阅读。面向普通读者，语言生动但不虚构史实。',
  },
];

/** 空态欢迎区（问候 + 输入框 + 推荐案例（换一换） + 模板引导区） */
const WelcomeView: React.FC<{
  onSend: (
    text: string,
    attachments?: import('../../../shared/types').AttachmentMeta[],
    modelBinding?: string,
    modelId?: string,
    mode?: import('../../../shared/types').WorkMode,
    expertId?: string,
  ) => void;
  loading: boolean;
  /** 任务运行中：主按钮变方形停止键（与发送共用输入框右下角） */
  stoppable: boolean;
  /** 停止当前任务 */
  onStop: () => void;
  /** 外部携带的初始提示词（如专家套件「通过对话创建」跳转） */
  initialText?: string;
  /** 当前工作目录 */
  workDir: string;
  /** 选择工作目录回调 */
  onSelectWorkDir: (dir: string) => void;
  /** 当前工作台模式：推荐案例与模板只属于「设计」，通用 / 幻灯片不展示 */
  mode: import('../../../shared/types').WorkMode;
}> = ({ onSend, loading, stoppable, onStop, initialText, workDir, onSelectWorkDir, mode }) => {
  const navigate = useNavigate();
  const { t: tr } = useI18n();
  // 问候语里的称呼取登录态：未登录时不凭空喊一个写死的称呼（原来固定写死一个「张先生」）
  const nickname = useAuthStore((s) => (s.loggedIn ? s.profile?.nickname?.trim() ?? '' : ''));
  // 对话宽度：与设置页同一个 appSettings 数据源（此前设置页写 localStorage、这里读 localStorage，
  // 与 appSettings 各存一份，刷新/换通道后会出现「设置页显示宽、对话仍是标准」）
  const [appSettings] = useAppSettings();
  const chatWidth = (appSettings.chatWidth as string) ?? '标准';
  const chatMaxWidth = resolveChatMaxWidth(chatWidth);
  // 推荐案例分批：每批 3 个，「换一换」循环切换
  const [batch, setBatch] = React.useState(0);
  const batchCount = Math.floor(SHOWCASE_POOL.length / 3);
  const showcases = SHOWCASE_POOL.slice(batch * 3, batch * 3 + 3);
  // 模板弹窗：点击具体模板 → 关闭弹窗并回填提示词到输入框
  const [templatesOpen, setTemplatesOpen] = React.useState(false);
  const useTemplate = (prompt: string) => {
    setTemplatesOpen(false);
    navigate(`/?prompt=${encodeURIComponent(prompt)}`);
  };
  return (
    <Box sx={{ maxWidth: chatMaxWidth, mx: 'auto', mt: '4vh', px: 2 }}>
      {/* 吉祥物 + 问候 */}
      <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', mb: 3 }}>
        <Box sx={{ mb: 1.25, mt: 0.5 }}>
          <Panda state="idle" size={112} variant="full" ground ariaLabel={tr('通通')} />
        </Box>
        <Typography sx={{ fontSize: '26px', fontWeight: 700, color: 'var(--tt-text-1)', letterSpacing: '-0.01em' }}>
          {tr(getGreeting())}
          {nickname ? tr('，{name}', { name: nickname }) : ''}
        </Typography>
        <Typography sx={{ fontSize: '15px', color: 'var(--tt-text-3)', mt: 0.75, mb: 3 }}>{tr('今天想让通通做什么？')}</Typography>

        {/* 输入框（紧跟问候区，设计图对齐） */}
        <Box sx={{ width: '100%' }}>
          <ChatInput onSend={onSend} loading={loading} stoppable={stoppable} onStop={onStop} initialText={initialText} workDir={workDir} onSelectWorkDir={onSelectWorkDir} />
        </Box>
      </Box>

      {/* 推荐案例与任务模板只在「设计」模式出现；通用 / 幻灯片不展示 */}
      {mode === 'design' && (
        <>
      {/* 推荐案例（换一换） */}
      <Box sx={{ mt: 4 }}>
        <Typography sx={{ fontSize: '14px', fontWeight: 600, color: 'var(--tt-text-2)', mb: 1.5 }}>{tr('推荐案例')}</Typography>
        <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 2 }}>
          {showcases.map((item) => (
            <Box
              key={item.title}
              onClick={() => onSend(tr(item.prompt))}
              sx={{
                position: 'relative',
                height: 112,
                borderRadius: '16px',
                background: item.background,
                cursor: 'pointer',
                overflow: 'hidden',
                boxShadow: '0 2px 10px rgba(15, 23, 42, 0.08)',
                transition: 'all 0.2s ease',
                '&:hover': { transform: 'translateY(-3px)', boxShadow: '0 10px 28px rgba(15, 23, 42, 0.14)' },
              }}
            >
              <Box
                component="img"
                src={item.cover}
                alt=""
                loading="lazy"
                sx={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
              />
              <Box
                sx={{
                  position: 'absolute',
                  inset: 0,
                  background: 'linear-gradient(180deg, rgba(2, 6, 23, 0) 42%, rgba(2, 6, 23, 0.66) 100%)',
                }}
              />
              <Box sx={{ position: 'absolute', left: 0, bottom: 0, p: 1.75, zIndex: 1 }}>
                <Typography sx={{ fontSize: '14.5px', fontWeight: 600, color: '#FFFFFF', textShadow: '0 1px 8px rgba(2, 6, 23, 0.45)' }}>
                  {tr(item.title)}
                </Typography>
                <Typography sx={{ fontSize: '12px', color: 'rgba(255, 255, 255, 0.82)', mt: 0.25, textShadow: '0 1px 6px rgba(2, 6, 23, 0.4)' }}>
                  {tr(item.subtitle)}
                </Typography>
              </Box>
            </Box>
          ))}
        </Box>

        {/* 换一换：切换下一批模板 */}
        <Box sx={{ display: 'flex', justifyContent: 'flex-end', mt: 1.5 }}>
          <Box
            onClick={() => setBatch((b) => (b + 1) % batchCount)}
            sx={{ display: 'flex', alignItems: 'center', gap: 0.5, cursor: 'pointer', '&:hover .swap-text': { color: '#4F46E5' } }}
          >
            <Typography className="swap-text" sx={{ fontSize: '12.5px', color: 'var(--tt-text-3)', transition: 'color 0.15s ease' }}>
              {tr('换一换')}
            </Typography>
            <RefreshIcon className="swap-text" sx={{ fontSize: 15, color: 'var(--tt-text-3)', transition: 'color 0.15s ease' }} />
          </Box>
        </Box>
      </Box>

      {/* 模板引导区：通过模版开启第一个任务 → 打开模板弹窗 */}
      <Box
        onClick={() => setTemplatesOpen(true)}
        sx={{
          mt: 3,
          mb: 2,
          p: 2.5,
          borderRadius: '16px',
          // 用主题变量而不是写死浅色：暗色主题下原来的浅底会配上浅字，整块读不出来
          background: 'linear-gradient(120deg, var(--tt-surface-2) 0%, var(--tt-surface) 100%)',
          border: '1px solid var(--tt-border-soft)',
          display: 'flex',
          alignItems: 'center',
          gap: 3,
          cursor: 'pointer',
          transition: 'box-shadow 0.18s ease, transform 0.15s ease',
          '&:hover': { boxShadow: '0 8px 24px rgba(79, 70, 229, 0.10)', transform: 'translateY(-1px)' },
        }}
      >
        <Box sx={{ flexGrow: 1, minWidth: 0 }}>
          <Typography sx={{ fontSize: '16px', fontWeight: 700, color: 'var(--tt-text-1)' }}>
            {tr('通过模版，开启你的第一个任务')}
          </Typography>
          <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-3)', mt: 0.5 }}>
            {tr('内置 {count} 个任务模板，一键复制，快速发起任务。', { count: SHOWCASE_POOL.length })}
          </Typography>
          <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#4F46E5', mt: 1.25 }}>
            {tr('浏览全部模板 →')}
          </Typography>
        </Box>
        {/* 模板卡片堆叠示意 */}
        <Box sx={{ position: 'relative', width: 120, height: 76, flexShrink: 0 }}>
          {[
            { label: 'W', bg: '#FFFFFF', rotate: '-8deg', left: 0, top: 10 },
            { label: 'P', bg: '#FFE9D6', rotate: '2deg', left: 28, top: 4 },
            { label: 'X', bg: '#DCFCE7', rotate: '10deg', left: 58, top: 12 },
          ].map((c) => (
            <Box
              key={c.label}
              sx={{
                position: 'absolute',
                left: c.left,
                top: c.top,
                width: 52,
                height: 62,
                borderRadius: '8px',
                background: c.bg,
                border: '1px solid rgba(15, 23, 42, 0.08)',
                boxShadow: '0 4px 12px rgba(15, 23, 42, 0.08)',
                transform: `rotate(${c.rotate})`,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
              }}
            >
              <Typography sx={{ fontSize: '16px', fontWeight: 800, color: '#64748B' }}>{c.label}</Typography>
            </Box>
          ))}
        </Box>
      </Box>

      {/* 模板弹窗：全量模板，点击回填提示词到输入框 */}
      <Dialog
        open={templatesOpen}
        onClose={() => setTemplatesOpen(false)}
        maxWidth="md"
        fullWidth
        PaperProps={{ sx: { borderRadius: '16px', p: 1, maxHeight: '80vh' } }}
      >
        <DialogTitle sx={{ pb: 0.5 }}>
          <Typography sx={{ fontSize: '16px', fontWeight: 700 }}>{tr('任务模板')}</Typography>
          <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-4)', mt: 0.25 }}>
            {tr('内置 {count} 个任务模板，点击即可复制提示词，快速发起任务。', { count: SHOWCASE_POOL.length })}
          </Typography>
        </DialogTitle>
        <DialogContent sx={{ pt: '12px !important' }}>
          <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 2, pb: 1 }}>
            {SHOWCASE_POOL.map((t) => (
              <Box
                key={t.title}
                onClick={() => useTemplate(tr(t.prompt))}
                sx={{
                  position: 'relative',
                  height: 130,
                  borderRadius: '14px',
                  background: t.background,
                  cursor: 'pointer',
                  overflow: 'hidden',
                  boxShadow: '0 2px 8px rgba(15, 23, 42, 0.08)',
                  transition: 'all 0.2s ease',
                  '&:hover': { transform: 'translateY(-2px)', boxShadow: '0 8px 22px rgba(15, 23, 42, 0.14)' },
                }}
              >
                <Box
                  component="img"
                  src={t.cover}
                  alt=""
                  loading="lazy"
                  sx={{ position: 'absolute', inset: 0, width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                />
                <Box
                  sx={{
                    position: 'absolute',
                    inset: 0,
                    background: 'linear-gradient(180deg, rgba(2, 6, 23, 0) 38%, rgba(2, 6, 23, 0.68) 100%)',
                  }}
                />
                <Box sx={{ position: 'absolute', left: 0, bottom: 0, p: 1.5, zIndex: 1 }}>
                  <Typography sx={{ fontSize: '13.5px', fontWeight: 600, color: '#FFFFFF', textShadow: '0 1px 8px rgba(2, 6, 23, 0.45)' }}>
                    {tr(t.title)}
                  </Typography>
                  <Typography sx={{ fontSize: '11px', color: 'rgba(255, 255, 255, 0.82)', mt: 0.25, textShadow: '0 1px 6px rgba(2, 6, 23, 0.4)' }}>
                    {tr(t.subtitle)}
                  </Typography>
                </Box>
              </Box>
            ))}
          </Box>
        </DialogContent>
      </Dialog>
        </>
      )}
    </Box>
  );
};

const Workbench: React.FC = () => {
  const { t: tr } = useI18n();
  // 工作目录：会话内容与产出物按对话时间建子目录存放于此
  const [workDir, setWorkDir] = React.useState('');
  // 当前工作空间：任务提交与引擎实例按它选择（切换后新任务进对应实例）
  const {
    workspaces,
    activeId: workspaceId,
    error: workspaceError,
    refresh: refreshWorkspaces,
    setActive: setWorkspace,
  } = useWorkspaceStore(
    useShallow((s) => ({
      workspaces: s.workspaces,
      activeId: s.activeId,
      error: s.error,
      refresh: s.refresh,
      setActive: s.setActive,
    })),
  );
  // 对话宽度：与设置页共用 appSettings（标准/宽/全宽）
  const [appSettings] = useAppSettings();
  const chatWidth = (appSettings.chatWidth as string) ?? '标准';
  const chatMaxWidth = resolveChatMaxWidth(chatWidth);
  // 实验特性「弹出独立窗口」：开关打开且运行在桌面端时，头部出现真实入口
  const popWindowEnabled = Boolean(
    (appSettings.experiments as Record<string, boolean> | undefined)?.popWindow,
  );
  const showNotification = useAppStore((s) => s.showNotification);
  const openInTaskWindow = async () => {
    if (!activeId) return;
    try {
      const r = await systemAPI.openTaskWindow(activeId, taskTitle);
      showNotification(
        r.existing ? '该任务的独立窗口已打开，已切到前台' : '已在新窗口打开当前任务',
        'success',
      );
    } catch (e) {
      showNotification(errorTextOf(e, '打开独立窗口失败'), 'error');
    }
  };
  // 当前工作台模式：输入框胶囊组与右侧画布共用同一份解析
  const { activeMode } = useWorkMode();
  // 右侧面板显隐：状态放在 app store，Ctrl + / 快捷键与头部按钮共用同一份
  const monitorOpen = useAppStore((s) => s.taskMonitorOpen);
  const setMonitorOpen = useAppStore((s) => s.setTaskMonitorOpen);
  // 会话内查找条（Ctrl + F）：显隐由 store 提供，快捷键与查找条共用；高亮轮次由查找条回报
  const findBarOpen = useAppStore((s) => s.findBarOpen);
  const setFindBarOpen = useAppStore((s) => s.setFindBarOpen);
  const [highlightTurn, setHighlightTurn] = React.useState<number | null>(null);
  const {
    messages,
    events,
    turnEvents,
    regeneratingTurn,
    regenerateTurn,
    feedbackByTurn,
    setTurnFeedback,
    activeId,
    plan,
    fileOps,
    isLoading,
    error,
    taskStatus,
    taskId,
    checkpoint,
    submitGoal,
    reviewPlan,
    resumeTask,
    controlTask,
    cancelTask,
    loadHistory,
    clearError,
  } = useWorkbenchStore(
    useShallow((s) => ({
      messages: s.messages,
      events: s.events,
      turnEvents: s.turnEvents,
      regeneratingTurn: s.regeneratingTurn,
      regenerateTurn: s.regenerateTurn,
      feedbackByTurn: s.feedbackByTurn,
      setTurnFeedback: s.setTurnFeedback,
      activeId: s.activeId,
      plan: s.plan,
      fileOps: s.fileOps,
      isLoading: s.isLoading,
      error: s.error,
      taskStatus: s.taskStatus,
      taskId: s.taskId,
      checkpoint: s.checkpoint,
      submitGoal: s.submitGoal,
      reviewPlan: s.reviewPlan,
      resumeTask: s.resumeTask,
      controlTask: s.controlTask,
      cancelTask: s.cancelTask,
      loadHistory: s.loadHistory,
      clearError: s.clearError,
    })),
  );

  // 外部携带的初始提示词（?prompt=，如专家套件「通过对话创建」），读取后从 URL 清除；
  // 独立任务窗口通过 ?conv= 指定要显示的会话，加载历史后切过去。
  const [searchParams, setSearchParams] = useSearchParams();
  const initialPrompt = searchParams.get('prompt') ?? undefined;
  const popupConvId = searchParams.get('conv');
  const isPopupWindow = searchParams.get('pop') === '1';

  // 挂载时加载工作空间清单与历史任务（侧边栏任务列表数据源）
  useEffect(() => {
    refreshWorkspaces().catch(() => undefined);
    void loadHistory().then(() => {
      if (!popupConvId) return;
      const state = useWorkbenchStore.getState();
      if (state.convs[popupConvId]) state.switchConversation(popupConvId);
    });
  }, [refreshWorkspaces, loadHistory, popupConvId]);

  useEffect(() => {
    if (searchParams.get('prompt')) {
      searchParams.delete('prompt');
      setSearchParams(searchParams, { replace: true });
    }
  }, [searchParams, setSearchParams]);

  const hasConversation = messages.length > 0 || events.length > 0 || isLoading;

  // ── 发送消息（支持附件 + 选中模型 + 自定义模型配置 + 工作目录） ──
  const handleSend = useCallback(
    (
      text: string,
      attachments?: import('../../../shared/types').AttachmentMeta[],
      modelBinding?: string,
      modelId?: string,
      mode?: import('../../../shared/types').WorkMode,
      expertId?: string,
    ) => {
      submitGoal(text, attachments, modelBinding, workDir || undefined, modelId, mode, expertId);
    },
    [submitGoal, workDir],
  );

  // ── 计划审阅（approve/reject/modify，转移 #9/#10/#11） ──
  const handlePlanReview = useCallback(
    (action: 'approve' | 'reject' | 'modify', patch?: Parameters<typeof reviewPlan>[1]) => {
      reviewPlan(action, patch);
      if (action === 'approve') {
        // 批准后进入执行，加载历史刷新侧边栏
        loadHistory();
      }
    },
    [reviewPlan, loadHistory],
  );

  // ── 头部信息 ──
  const lastUserMsg = [...messages].reverse().find((m) => m.role === 'user');
  const taskTitle = lastUserMsg ? lastUserMsg.content.slice(0, 20) : tr('新任务');
  const steps = plan?.steps ?? [];
  const doneSteps = steps.filter((s) => s.status === StepStatus.SUCCESS).length;
  const now = new Date();
  const timeStr = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`;

  // 控制按钮可见性（14 态）
  const canPause = taskStatus === TaskStatus.EXECUTING;
  const canResume = taskStatus === TaskStatus.PAUSED;
  // 断点续跑：只看检查点收尾状态（失败/取消/中断后为 true），与 14 态里的「继续」互不影响
  const canResumeCheckpoint = Boolean(
    checkpoint?.resumable && taskId && checkpoint.taskId === taskId,
  ) && !isLoading;
  const canCancel =
    isLoading ||
    [
      TaskStatus.QUEUED,
      TaskStatus.PLANNING,
      TaskStatus.PENDING_REVIEW,
      TaskStatus.EXECUTING,
      TaskStatus.BUDGET_PAUSED,
      TaskStatus.PAUSED,
    ].includes(taskStatus as TaskStatus);
  // 「运行中」以任务状态为准：引擎的 done 只代表一次事件流结束，
  // 计划流收尾后任务仍在待审阅/执行中，不能因为 plan 还挂着就一直显示停止键
  const runningTask =
    isLoading ||
    [
      TaskStatus.QUEUED,
      TaskStatus.PLANNING,
      TaskStatus.PENDING_REVIEW,
      TaskStatus.EXECUTING,
    ].includes(taskStatus as TaskStatus);

  return (
    <Box sx={{ display: 'flex', height: '100vh', bgcolor: 'var(--tt-surface)', overflow: 'hidden' }}>
      {/* ── 中间聊天区 ── */}
      <Box sx={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', position: 'relative' }}>
        {/* 头部：任务标题 + 状态徽标 + 时间 + 控制按钮 */}
        <Box
          sx={{
            display: 'flex',
            alignItems: 'center',
            gap: 1.25,
            px: 3,
            py: 1.5,
            borderBottom: '1px solid #F3F4F6',
          }}
        >
          <Typography sx={{ fontSize: '15px', fontWeight: 600, color: 'var(--tt-text-1)' }}>{taskTitle}</Typography>
          <StatusBadge taskStatus={taskStatus} isLoading={isLoading} stepCount={steps.length} doneSteps={doneSteps} />
          {hasConversation && (
            <Typography sx={{ fontSize: '12px', color: '#C0C4CA' }}>{timeStr}</Typography>
          )}
          <Box sx={{ flexGrow: 1 }} />

          {/* 工作空间切换：新任务提交到该空间的独立引擎实例；执行中禁止切换 */}
          <Tooltip
            title={
              workspaceError
                ? errorTextOf(workspaceError, tr('工作空间清单读取失败'))
                : tr('任务在该工作空间的独立引擎实例中执行')
            }
          >
            <Select
              size="small"
              value={workspaceId}
              onChange={(e) => setWorkspace(String(e.target.value))}
              disabled={isLoading}
              inputProps={{ 'aria-label': tr('切换工作空间') }}
              sx={{
                minWidth: 110,
                height: 28,
                fontSize: 12,
                color: 'var(--tt-text-2)',
                '& .MuiOutlinedInput-notchedOutline': { borderColor: '#E5E7EB' },
                '& .MuiSelect-select': { py: 0.5, pl: 1.25 },
              }}
            >
              {(workspaces.length > 0
                ? workspaces.map((w) => ({ id: w.id, label: w.running ? tr('{id} · 运行中', { id: w.id }) : w.id }))
                : [{ id: workspaceId, label: workspaceId }]
              ).map((w) => (
                <MenuItem key={w.id} value={w.id} sx={{ fontSize: 12 }}>
                  {w.label}
                </MenuItem>
              ))}
            </Select>
          </Tooltip>

          {/* 实验特性 popWindow：把当前任务弹出为独立窗口（仅桌面端） */}
          {popWindowEnabled && isTauri() && !isPopupWindow && (
            <Tooltip title={activeId ? tr('把当前任务弹出为独立窗口') : tr('当前没有可弹出的任务')}>
              <span>
                <IconButton
                  size="small"
                  disabled={!activeId}
                  onClick={() => void openInTaskWindow()}
                  aria-label={tr('在独立窗口打开')}
                  sx={{ color: activeId ? 'var(--tt-text-3)' : 'var(--tt-text-4)' }}
                >
                  <OpenInNewIcon sx={{ fontSize: 17 }} />
                </IconButton>
              </span>
            </Tooltip>
          )}

          {/* 任务控制：暂停/继续/终止 */}
          {canPause && (
            <Tooltip title={tr('暂停任务')}>
              <Box
                component="button"
                onClick={() => controlTask('pause')}
                style={{
                  border: '1px solid #FCD34D',
                  background: '#FFFBEB',
                  color: '#B45309',
                  borderRadius: 999,
                  fontSize: 12,
                  padding: '3px 12px',
                  cursor: 'pointer',
                }}
              >
                {tr('暂停')}
              </Box>
            </Tooltip>
          )}
          {canResume && (
            <Tooltip title={tr('继续执行')}>
              <Box
                component="button"
                onClick={() => controlTask('resume')}
                style={{
                  border: '1px solid #6EE7B7',
                  background: '#ECFDF5',
                  color: '#047857',
                  borderRadius: 999,
                  fontSize: 12,
                  padding: '3px 12px',
                  cursor: 'pointer',
                }}
              >
                {tr('继续')}
              </Box>
            </Tooltip>
          )}
          {canResumeCheckpoint && (
            <Tooltip
              title={
                <Box component="span" sx={{ display: 'block', fontSize: 12, lineHeight: 1.7 }}>
                  {checkpoint?.reason && <Box component="span" sx={{ display: 'block' }}>{checkpoint.reason}</Box>}
                  {checkpoint?.savedAt && (
                    <Box component="span" sx={{ display: 'block' }}>
                      {tr('检查点时间：{time}', { time: checkpoint.savedAt })}
                    </Box>
                  )}
                  <Box component="span" sx={{ display: 'block' }}>
                    {tr('从最近一次模型调用前的检查点继续，已完成的工具调用不会重复执行')}
                  </Box>
                  <Box component="span" sx={{ display: 'block' }}>
                    {checkpoint?.scope === 'disk'
                      ? tr('检查点已保存到本机磁盘，刷新或引擎重启后仍可继续')
                      : tr('检查点只保存在本次引擎进程内，引擎重启后无法恢复')}
                  </Box>
                </Box>
              }
            >
              <Box
                component="button"
                onClick={() => void resumeTask()}
                style={{
                  border: '1px solid #C7D2FE',
                  background: '#EEF2FF',
                  color: '#4338CA',
                  borderRadius: 999,
                  fontSize: 12,
                  padding: '3px 12px',
                  cursor: 'pointer',
                }}
              >
                {tr('从检查点继续')}
              </Box>
            </Tooltip>
          )}
          {/* 运行中由输入框主按钮停止；这里只兜住暂停/待审阅等非运行但可取消的状态 */}
          {canCancel && !runningTask && (
            <Tooltip title={tr('取消任务')}>
              <Box
                component="button"
                onClick={() => cancelTask()}
                style={{
                  border: '1px solid #FCA5A5',
                  background: '#FEF2F2',
                  color: '#DC2626',
                  borderRadius: 999,
                  fontSize: 12,
                  padding: '3px 12px',
                  cursor: 'pointer',
                }}
              >
                {tr('终止')}
              </Box>
            </Tooltip>
          )}
          {/* 任务监控隐藏后：头部右侧重开按钮 */}
          {hasConversation && !monitorOpen && (
            <Tooltip title={tr('展开任务监控')}>
              <IconButton
                size="small"
                onClick={() => setMonitorOpen(true)}
                sx={{
                  color: 'var(--tt-text-3)',
                  border: '1px solid var(--tt-border)',
                  width: 30,
                  height: 30,
                  '&:hover': { bgcolor: 'var(--tt-surface-2)', borderColor: '#8B5CF6', color: '#8B5CF6' },
                }}
              >
                <ViewSidebarIcon sx={{ fontSize: 17 }} />
              </IconButton>
            </Tooltip>
          )}
        </Box>

        {/* 消息流 / 欢迎区（欢迎态输入框内置于 WelcomeView） */}
        <Box sx={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', px: 3, pt: 2 }}>
          {/* 查找条占消息流上方一行：滚动区之外，既不会被滚走，也不靠魔法留白躲开首轮提问 */}
          {hasConversation && findBarOpen && (
            <TaskFindBar
              messages={messages}
              turnEvents={turnEvents}
              onClose={() => setFindBarOpen(false)}
              onActiveTurn={setHighlightTurn}
            />
          )}
          <Box sx={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
            {hasConversation ? (
              <Box sx={{ maxWidth: chatMaxWidth, mx: 'auto' }}>
                <ChatStream
                  messages={messages}
                  events={events}
                  turnEvents={turnEvents}
                  isLoading={isLoading}
                  regeneratingTurn={regeneratingTurn}
                  onRegenerate={regenerateTurn}
                  feedback={feedbackByTurn}
                  onFeedback={async (turnIdx, kind) => {
                    if (!activeId) return;
                    await setTurnFeedback(activeId, turnIdx, kind);
                  }}
                  highlightTurn={highlightTurn}
                />
              </Box>
            ) : (
              <WelcomeView onSend={handleSend} loading={isLoading} stoppable={runningTask} onStop={() => void cancelTask()} initialText={initialPrompt} workDir={workDir} onSelectWorkDir={setWorkDir} mode={activeMode} />
            )}
          </Box>
        </Box>

        {/* 输入区（仅对话态：底部固定） */}
        {hasConversation && (
          <Box sx={{ px: 3, pb: 2, pt: 1 }}>
            <Box sx={{ maxWidth: chatMaxWidth, mx: 'auto' }}>
              <ChatInput onSend={handleSend} loading={isLoading} stoppable={runningTask} onStop={() => void cancelTask()} workDir={workDir} onSelectWorkDir={setWorkDir} compact />
            </Box>
          </Box>
        )}
      </Box>

      {/* ── 右侧任务 / 模式画布（仅对话态；支持隐藏，隐藏后右上角悬浮按钮重开） ── */}
      {hasConversation && monitorOpen && (
        <TaskMonitor
          plan={plan}
          fileOps={fileOps}
          onReview={handlePlanReview}
          workDir={workDir}
          onHide={() => setMonitorOpen(false)}
          mode={activeMode}
          canvas={activeMode === 'general' ? undefined : <ModeCanvas mode={activeMode} fileOps={fileOps} />}
          canvasLabel={activeMode === 'general' ? undefined : tr(MODE_CANVAS_LABELS[activeMode])}
        />
      )}

      {/* 错误提示 */}
      <Snackbar
        open={!!error}
        autoHideDuration={6000}
        onClose={clearError}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert onClose={clearError} severity="error" sx={{ width: '100%' }}>
          {error}
        </Alert>
      </Snackbar>
    </Box>
  );
};

export default Workbench;
