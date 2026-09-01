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
import { Box, Chip, Snackbar, Alert, Tooltip, Typography, Dialog, DialogTitle, DialogContent, IconButton } from '@mui/material';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import HourglassEmptyIcon from '@mui/icons-material/HourglassEmpty';
import PauseCircleOutlineIcon from '@mui/icons-material/PauseCircleOutline';
import CancelOutlinedIcon from '@mui/icons-material/CancelOutlined';
import RefreshIcon from '@mui/icons-material/Refresh';
import ChatStream from '../../components/ChatStream';
import ChatInput from '../../components/ChatInput';
import TaskMonitor from '../../components/TaskMonitor';
import ViewSidebarIcon from '@mui/icons-material/ViewSidebar';
import { useWorkbenchStore } from '../../store/workbenchStore';
import { useShallow } from 'zustand/react/shallow';
import { TaskStatus, StepStatus } from '../../../shared/types';

/** 任务状态徽标（对齐设计图：绿色「已完成 N 个步骤」等） */
const StatusBadge: React.FC<{
  taskStatus: TaskStatus | null;
  isLoading: boolean;
  stepCount: number;
  doneSteps: number;
}> = ({ taskStatus, isLoading, stepCount, doneSteps }) => {
  if (isLoading || [TaskStatus.EXECUTING, TaskStatus.PLANNING, TaskStatus.QUEUED, TaskStatus.DELIVERING, TaskStatus.RETRY_PENDING].includes(taskStatus as TaskStatus)) {
    return (
      <Chip
        size="small"
        icon={<HourglassEmptyIcon sx={{ fontSize: 14 }} />}
        label={taskStatus === TaskStatus.QUEUED ? '排队中' : '进行中'}
        sx={{ bgcolor: '#FEF3C7', color: '#B45309', fontSize: '12px', '& .MuiChip-icon': { color: '#B45309' } }}
      />
    );
  }
  switch (taskStatus) {
    case TaskStatus.PENDING_REVIEW:
      return <Chip size="small" label="待审阅" sx={{ bgcolor: '#E0F2FE', color: '#0369A1', fontSize: '12px' }} />;
    case TaskStatus.PAUSED:
      return (
        <Chip
          size="small"
          icon={<PauseCircleOutlineIcon sx={{ fontSize: 14 }} />}
          label="已暂停"
          sx={{ bgcolor: '#F3F4F6', color: '#6B7280', fontSize: '12px', '& .MuiChip-icon': { color: '#6B7280' } }}
        />
      );
    case TaskStatus.BUDGET_PAUSED:
      return <Chip size="small" label="预算暂停" sx={{ bgcolor: '#FEE2E2', color: '#B91C1C', fontSize: '12px' }} />;
    case TaskStatus.CANCELLED:
      return (
        <Chip
          size="small"
          icon={<CancelOutlinedIcon sx={{ fontSize: 14 }} />}
          label="已取消"
          sx={{ bgcolor: '#F3F4F6', color: '#6B7280', fontSize: '12px', '& .MuiChip-icon': { color: '#6B7280' } }}
        />
      );
    case TaskStatus.FAILED:
      return <Chip size="small" label="失败" sx={{ bgcolor: '#FEE2E2', color: '#B91C1C', fontSize: '12px' }} />;
    case TaskStatus.DONE:
      return (
        <Chip
          size="small"
          icon={<CheckCircleIcon sx={{ fontSize: 14 }} />}
          label={`已完成 ${doneSteps} 个步骤`}
          sx={{ bgcolor: '#E6F7F4', color: '#0D9488', fontSize: '12px', '& .MuiChip-icon': { color: '#0D9488' } }}
        />
      );
    default:
      return stepCount > 0 ? null : <Chip size="small" label="新任务" sx={{ bgcolor: '#F3F4F6', color: '#6B7280', fontSize: '12px' }} />;
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
const SHOWCASE_POOL = [
  {
    title: '民宿网站官网',
    subtitle: '景点 公寓 营地 旅程',
    prompt: '帮我设计一个民宿网站官网原型，包含景点、公寓、营地、旅程等板块',
    background: 'linear-gradient(160deg, #93C5FD 0%, #2563EB 100%)',
    titleColor: '#FFFFFF',
    subtitleColor: 'rgba(255, 255, 255, 0.75)',
  },
  {
    title: '达人投放管理看板',
    subtitle: 'Excel · 仪表盘',
    prompt: '帮我创建一个达人投放管理看板的 Excel 仪表盘',
    background: 'linear-gradient(160deg, #F9FAFB 0%, #E5E7EB 100%)',
    titleColor: '#111827',
    subtitleColor: '#6B7280',
  },
  {
    title: '电商看板构建',
    subtitle: 'GMV 8,436,550',
    prompt: '帮我构建一个电商数据看板，展示 GMV 等核心指标',
    background: 'linear-gradient(160deg, #4B5563 0%, #111827 100%)',
    titleColor: '#FFFFFF',
    subtitleColor: '#34D399',
  },
  {
    title: '月度经营复盘',
    subtitle: '数据报表 · 同环比',
    prompt: '帮我生成一份月度店铺经营复盘报告，含 GMV、订单量、客单价等核心指标的同环比分析',
    background: 'linear-gradient(160deg, #F59E0B 0%, #D97706 100%)',
    titleColor: '#FFFFFF',
    subtitleColor: 'rgba(255, 255, 255, 0.75)',
  },
  {
    title: '课程资源网站生成',
    subtitle: '数据结构 与 算法',
    prompt: '帮我生成一个课程资源网站原型，包含课程介绍、大纲、学分学时与作业示例等板块',
    background: 'linear-gradient(160deg, #8B5CF6 0%, #6D28D9 100%)',
    titleColor: '#FFFFFF',
    subtitleColor: 'rgba(255, 255, 255, 0.75)',
  },
  {
    title: 'AI 副业课落地',
    subtitle: '30 天 从 0 到接单',
    prompt: '帮我制作一个 AI 副业课落地页，包含卖点、价格与报名引导',
    background: 'linear-gradient(160deg, #FB923C 0%, #EA580C 100%)',
    titleColor: '#FFFFFF',
    subtitleColor: 'rgba(255, 255, 255, 0.75)',
  },
  {
    title: '商品视觉五连套图',
    subtitle: '主图 · 详情页',
    prompt: '帮我设计一套电商商品视觉五连套图，含主图与详情页排版',
    background: 'linear-gradient(160deg, #EC4899 0%, #BE185D 100%)',
    titleColor: '#FFFFFF',
    subtitleColor: 'rgba(255, 255, 255, 0.75)',
  },
  {
    title: '本周销售战报',
    subtitle: '数据汇总 · 表格',
    prompt: '帮我汇总本周销售数据，生成一份销售战报表格',
    background: 'linear-gradient(160deg, #10B981 0%, #047857 100%)',
    titleColor: '#FFFFFF',
    subtitleColor: 'rgba(255, 255, 255, 0.75)',
  },
  {
    title: '敦煌艺术科普长文',
    subtitle: '沉浸式 · 写作',
    prompt: '帮我撰写一篇《敦煌艺术与丝路文明》沉浸式科普长文',
    background: 'linear-gradient(160deg, #0EA5E9 0%, #0369A1 100%)',
    titleColor: '#FFFFFF',
    subtitleColor: 'rgba(255, 255, 255, 0.75)',
  },
];

/** 空态欢迎区（问候 + 输入框 + 推荐案例（换一换） + 模板引导区） */
const WelcomeView: React.FC<{
  onSend: (text: string, attachments?: import('../../../shared/types').AttachmentMeta[], modelBinding?: string) => void;
  loading: boolean;
  /** 外部携带的初始提示词（如专家套件「通过对话创建」跳转） */
  initialText?: string;
  /** 当前工作目录 */
  workDir: string;
  /** 选择工作目录回调 */
  onSelectWorkDir: (dir: string) => void;
}> = ({ onSend, loading, initialText, workDir, onSelectWorkDir }) => {
  const navigate = useNavigate();
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
    <Box sx={{ maxWidth: 860, mx: 'auto', mt: '4vh', px: 2 }}>
      {/* Logo + 问候 */}
      <Box sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', mb: 3 }}>
        <Box
          sx={{
            width: 52,
            height: 52,
            borderRadius: '16px',
            background: 'linear-gradient(135deg, #34D399 0%, #06B6D4 100%)',
            boxShadow: '0 8px 24px rgba(6, 182, 212, 0.3)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            mb: 2,
          }}
        >
          <Typography sx={{ color: '#fff', fontSize: '22px', fontWeight: 700 }}>通</Typography>
        </Box>
        <Typography sx={{ fontSize: '26px', fontWeight: 700, color: '#111827', letterSpacing: '-0.01em' }}>
          {getGreeting()}，张先生
        </Typography>
        <Typography sx={{ fontSize: '15px', color: '#6B7280', mt: 0.75, mb: 3 }}>有什么需要我搞定的？</Typography>

        {/* 输入框（紧跟问候区，设计图对齐） */}
        <Box sx={{ width: '100%' }}>
          <ChatInput onSend={onSend} loading={loading} initialText={initialText} workDir={workDir} onSelectWorkDir={onSelectWorkDir} />
        </Box>
      </Box>

      {/* 推荐案例（换一换） */}
      <Box sx={{ mt: 5 }}>
        <Typography sx={{ fontSize: '14px', fontWeight: 600, color: '#374151', mb: 1.5 }}>推荐案例</Typography>
        <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 2 }}>
          {showcases.map((item) => (
            <Box
              key={item.title}
              onClick={() => onSend(item.prompt)}
              sx={{
                position: 'relative',
                height: 150,
                borderRadius: '16px',
                background: item.background,
                cursor: 'pointer',
                overflow: 'hidden',
                boxShadow: '0 2px 10px rgba(15, 23, 42, 0.08)',
                transition: 'all 0.2s ease',
                '&:hover': { transform: 'translateY(-3px)', boxShadow: '0 10px 28px rgba(15, 23, 42, 0.14)' },
              }}
            >
              <Box sx={{ position: 'absolute', left: 0, bottom: 0, p: 2 }}>
                <Typography sx={{ fontSize: '15px', fontWeight: 600, color: item.titleColor }}>
                  {item.title}
                </Typography>
                <Typography sx={{ fontSize: '12px', color: item.subtitleColor, mt: 0.25 }}>
                  {item.subtitle}
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
            <Typography className="swap-text" sx={{ fontSize: '12.5px', color: '#6B7280', transition: 'color 0.15s ease' }}>
              换一换
            </Typography>
            <RefreshIcon className="swap-text" sx={{ fontSize: 15, color: '#6B7280', transition: 'color 0.15s ease' }} />
          </Box>
        </Box>
      </Box>

      {/* 模板引导区：通过模版开启第一个任务 → 打开模板弹窗 */}
      <Box
        onClick={() => setTemplatesOpen(true)}
        sx={{
          mt: 4,
          mb: 2,
          p: 3,
          borderRadius: '16px',
          background: 'linear-gradient(120deg, #F5F7FB 0%, #EEF2FA 100%)',
          border: '1px solid #E7EBF3',
          display: 'flex',
          alignItems: 'center',
          gap: 3,
          cursor: 'pointer',
          transition: 'box-shadow 0.18s ease, transform 0.15s ease',
          '&:hover': { boxShadow: '0 8px 24px rgba(79, 70, 229, 0.10)', transform: 'translateY(-1px)' },
        }}
      >
        <Box sx={{ flexGrow: 1, minWidth: 0 }}>
          <Typography sx={{ fontSize: '16px', fontWeight: 700, color: '#111827' }}>
            通过模版，开启你的第一个任务
          </Typography>
          <Typography sx={{ fontSize: '12.5px', color: '#6B7280', mt: 0.5 }}>
            海量高品质任务模板，一键复制，快速发起任务。
          </Typography>
          <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#4F46E5', mt: 1.25 }}>
            浏览全部模板 →
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
              <Typography sx={{ fontSize: '16px', fontWeight: 800, color: '#9CA3AF' }}>{c.label}</Typography>
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
          <Typography sx={{ fontSize: '16px', fontWeight: 700 }}>任务模板</Typography>
          <Typography sx={{ fontSize: '12.5px', color: '#9CA3AF', mt: 0.25 }}>
            海量高品质任务模板，点击即可复制提示词，快速发起任务。
          </Typography>
        </DialogTitle>
        <DialogContent sx={{ pt: '12px !important' }}>
          <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 2, pb: 1 }}>
            {SHOWCASE_POOL.map((t) => (
              <Box
                key={t.title}
                onClick={() => useTemplate(t.prompt)}
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
                <Box sx={{ position: 'absolute', left: 0, bottom: 0, p: 1.5 }}>
                  <Typography sx={{ fontSize: '13.5px', fontWeight: 600, color: t.titleColor }}>{t.title}</Typography>
                  <Typography sx={{ fontSize: '11px', color: t.subtitleColor, mt: 0.25 }}>{t.subtitle}</Typography>
                </Box>
              </Box>
            ))}
          </Box>
        </DialogContent>
      </Dialog>
    </Box>
  );
};

const Workbench: React.FC = () => {
  // 工作目录：会话内容与产出物按对话时间建子目录存放于此
  const [workDir, setWorkDir] = React.useState('');
  // 右侧任务监控面板显隐
  const [monitorOpen, setMonitorOpen] = React.useState(true);
  const {
    messages,
    events,
    turnEvents,
    regeneratingTurn,
    regenerateTurn,
    plan,
    fileOps,
    isLoading,
    error,
    taskStatus,
    submitGoal,
    reviewPlan,
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
      plan: s.plan,
      fileOps: s.fileOps,
      isLoading: s.isLoading,
      error: s.error,
      taskStatus: s.taskStatus,
      submitGoal: s.submitGoal,
      reviewPlan: s.reviewPlan,
      controlTask: s.controlTask,
      cancelTask: s.cancelTask,
      loadHistory: s.loadHistory,
      clearError: s.clearError,
    })),
  );

  // 挂载时加载历史任务（侧边栏任务列表数据源）
  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  // 外部携带的初始提示词（?prompt=，如专家套件「通过对话创建」），读取后从 URL 清除；
  // 依赖 searchParams，保证已在当前页时再次携带 prompt（如模板弹窗选择）也能生效
  const [searchParams, setSearchParams] = useSearchParams();
  const initialPrompt = searchParams.get('prompt') ?? undefined;
  useEffect(() => {
    if (searchParams.get('prompt')) {
      searchParams.delete('prompt');
      setSearchParams(searchParams, { replace: true });
    }
  }, [searchParams, setSearchParams]);

  const hasConversation = messages.length > 0 || events.length > 0 || isLoading;

  // ── 发送消息（支持附件 + 选中模型 + 工作目录） ──
  const handleSend = useCallback(
    (text: string, attachments?: import('../../../shared/types').AttachmentMeta[], modelBinding?: string) => {
      submitGoal(text, attachments, modelBinding, workDir || undefined);
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
  const taskTitle = lastUserMsg ? lastUserMsg.content.slice(0, 20) : '新任务';
  const steps = plan?.steps ?? [];
  const doneSteps = steps.filter((s) => s.status === StepStatus.SUCCESS).length;
  const now = new Date();
  const timeStr = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}:${String(now.getSeconds()).padStart(2, '0')}`;

  // 控制按钮可见性（14 态）
  const canPause = taskStatus === TaskStatus.EXECUTING;
  const canResume = taskStatus === TaskStatus.PAUSED;
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

  return (
    <Box sx={{ display: 'flex', height: '100vh', bgcolor: '#FFFFFF', overflow: 'hidden' }}>
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
          <Typography sx={{ fontSize: '15px', fontWeight: 600, color: '#111827' }}>{taskTitle}</Typography>
          <StatusBadge taskStatus={taskStatus} isLoading={isLoading} stepCount={steps.length} doneSteps={doneSteps} />
          {hasConversation && (
            <Typography sx={{ fontSize: '12px', color: '#C0C4CA' }}>{timeStr}</Typography>
          )}
          <Box sx={{ flexGrow: 1 }} />

          {/* 任务控制：暂停/继续/终止 */}
          {canPause && (
            <Tooltip title="暂停任务">
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
                暂停
              </Box>
            </Tooltip>
          )}
          {canResume && (
            <Tooltip title="继续执行">
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
                继续
              </Box>
            </Tooltip>
          )}
          {canCancel && (
            <Tooltip title="取消任务">
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
                终止
              </Box>
            </Tooltip>
          )}
          {/* 任务监控隐藏后：头部右侧重开按钮 */}
          {hasConversation && !monitorOpen && (
            <Tooltip title="展开任务监控">
              <IconButton
                size="small"
                onClick={() => setMonitorOpen(true)}
                sx={{
                  color: '#6B7280',
                  border: '1px solid #E5E7EB',
                  width: 30,
                  height: 30,
                  '&:hover': { bgcolor: '#F8FAFC', borderColor: '#8B5CF6', color: '#8B5CF6' },
                }}
              >
                <ViewSidebarIcon sx={{ fontSize: 17 }} />
              </IconButton>
            </Tooltip>
          )}
        </Box>

        {/* 消息流 / 欢迎区（欢迎态输入框内置于 WelcomeView） */}
        <Box sx={{ flex: 1, minHeight: 0, overflow: 'auto', px: 3, pt: 2 }}>
          {hasConversation ? (
            <Box sx={{ maxWidth: 860, mx: 'auto' }}>
              <ChatStream
                messages={messages}
                events={events}
                turnEvents={turnEvents}
                isLoading={isLoading}
                regeneratingTurn={regeneratingTurn}
                onRegenerate={regenerateTurn}
              />
            </Box>
          ) : (
            <WelcomeView onSend={handleSend} loading={isLoading} initialText={initialPrompt} workDir={workDir} onSelectWorkDir={setWorkDir} />
          )}
        </Box>

        {/* 输入区（仅对话态：底部固定） */}
        {hasConversation && (
          <Box sx={{ px: 3, pb: 2, pt: 1 }}>
            <Box sx={{ maxWidth: 860, mx: 'auto' }}>
              <ChatInput onSend={handleSend} loading={isLoading} workDir={workDir} onSelectWorkDir={setWorkDir} />
            </Box>
          </Box>
        )}
      </Box>

      {/* ── 右侧任务监控（仅对话态；支持隐藏，隐藏后右上角悬浮按钮重开） ── */}
      {hasConversation && monitorOpen && (
        <TaskMonitor plan={plan} fileOps={fileOps} onReview={handlePlanReview} workDir={workDir} onHide={() => setMonitorOpen(false)} />
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
