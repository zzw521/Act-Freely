/**
 * 通天办公 - 定时任务页面（真实调度版）
 *
 * 结构：
 * - 头部：标题 + 说明 + 「通过对话创建」「+ 新建定时任务」双入口
 * - 提示条：定时任务仅在电脑保持唤醒时运行 + 保持系统唤醒开关（真实生效/如实提示）
 * - 推荐示例：三张示例卡片（点击即预填创建弹窗）
 * - 我的定时任务 / 执行记录 双页签（真实数据 + 空态）
 * - 新建/编辑定时任务弹窗：任务名称 / 工作空间（选择或就地新建）/ 计划时间（频率 + 时间 + 星期）/ 任务描述
 *
 * 数据通道（Mock 开关规范）：
 * - Tauri 容器：Rust command 落 SQLite，Rust 扫描线程到点真实执行
 * - 浏览器直连引擎 / 开发 Mock：前端本地台账（`lib/schedule-local.ts`），立即执行记明确标注的模拟记录
 * - 界面按本地时区录入与呈现，落库前统一换算为 UTC（与 Rust 口径一致）
 */

import React, { useEffect, useState } from 'react';
import {
  Box,
  Typography,
  Button,
  Switch,
  Dialog,
  DialogTitle,
  DialogContent,
  Select,
  MenuItem,
  TextField,
  Snackbar,
  Alert,
} from '@mui/material';
import ChatBubbleOutlineIcon from '@mui/icons-material/ChatBubbleOutline';
import ScheduleOutlinedIcon from '@mui/icons-material/ScheduleOutlined';
import AccessTimeIcon from '@mui/icons-material/AccessTime';
import FolderOutlinedIcon from '@mui/icons-material/FolderOutlined';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import EditOutlinedIcon from '@mui/icons-material/EditOutlined';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import { useNavigate } from 'react-router-dom';
import type { ScheduledTask, ScheduleFreq, ScheduleRunStatus, WorkspaceInfo } from '../../../shared/contracts';
import { errorTextOf } from '../../../shared/error-messages';
import { workspaceAPI } from '../../lib/api';
import {
  SCHEDULE_FREQS,
  WEEKDAY_LABELS,
  describeSchedule,
  formatLocalDateTime,
  freqLabel,
  localToUtcTime,
  utcToLocalTime,
} from '../../lib/schedule-local';
import { useScheduleStore } from '../../stores/useScheduleStore';

// ── 对话式创建的预置提示词 ──────────────────────────────────────────

const SCHEDULE_CREATE_PROMPT = '我要创建一个定时任务，每【时间间隔】执行【具体任务】';

// ── 推荐示例（对齐设计图；频率/时刻与调度模型同口径） ────────────────

interface Recommend {
  id: string;
  title: string;
  desc: string;
  schedule: string;
  freq: ScheduleFreq;
  time: string;
  weekday?: number;
}

const RECOMMENDS: Recommend[] = [
  {
    id: 'brand-monitor',
    title: '品牌舆情监控',
    desc: '我要创建一个定时任务，每【天】执行【搜索【品牌名】品牌近 24 小时舆情，汇总正负面情绪与热点话题】',
    schedule: '每日下午 2 点',
    freq: 'daily',
    time: '14:00',
  },
  {
    id: 'movie-daily',
    title: '每日电影推荐',
    desc: '我要创建一个定时任务，每【天】执行【给我推荐一部公认的经典电影（评分 8.5 以上），附推荐理由】',
    schedule: '每日晚 8 点',
    freq: 'daily',
    time: '20:00',
  },
  {
    id: 'stock-monitor',
    title: '金融股票监控',
    desc: '我要创建一个定时任务，每【周】执行【帮我查询【英伟达股票】今日的走势与关键新闻摘要】',
    schedule: '每周一早 8 点',
    freq: 'weekly',
    time: '08:00',
    weekday: 1,
  },
];

const RUN_STATUS_LABELS: Record<ScheduleRunStatus, string> = {
  running: '执行中',
  succeeded: '已成功',
  failed: '已失败',
  blocked: '已阻断',
};

const RUN_STATUS_COLORS: Record<ScheduleRunStatus, { fg: string; bg: string }> = {
  running: { fg: '#1D4ED8', bg: 'rgba(37, 99, 235, 0.10)' },
  succeeded: { fg: '#047857', bg: 'rgba(5, 150, 105, 0.10)' },
  failed: { fg: '#B91C1C', bg: 'rgba(220, 38, 38, 0.10)' },
  blocked: { fg: '#B45309', bg: 'rgba(217, 119, 6, 0.12)' },
};

// ── 页面组件 ──────────────────────────────────────────────────────

const ScheduleView: React.FC = () => {
  const navigate = useNavigate();
  const [tab, setTab] = useState<'tasks' | 'records'>('tasks');
  const [toast, setToast] = useState<{ text: string; severity: 'info' | 'success' | 'error' } | null>(null);

  const tasks = useScheduleStore((s) => s.tasks);
  const runs = useScheduleStore((s) => s.runs);
  const keepAwake = useScheduleStore((s) => s.keepAwake);
  const tasksLoading = useScheduleStore((s) => s.tasksLoading);
  const runsLoading = useScheduleStore((s) => s.runsLoading);
  const create = useScheduleStore((s) => s.create);
  const update = useScheduleStore((s) => s.update);
  const toggle = useScheduleStore((s) => s.toggle);
  const remove = useScheduleStore((s) => s.remove);
  const runNow = useScheduleStore((s) => s.runNow);
  const loadKeepAwake = useScheduleStore((s) => s.loadKeepAwake);
  const setKeepAwake = useScheduleStore((s) => s.setKeepAwake);

  // 新建/编辑定时任务弹窗状态（editingScheduleId 为空即新建）
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingScheduleId, setEditingScheduleId] = useState<string | null>(null);
  const [taskName, setTaskName] = useState('');
  const [freq, setFreq] = useState<ScheduleFreq>('daily');
  const [time, setTime] = useState('09:00');
  const [weekday, setWeekday] = useState(1);
  const [taskDesc, setTaskDesc] = useState('');
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // 工作空间（PRD「选择工作空间」）：桌面端由 Rust 以磁盘目录为权威
  const [workspaces, setWorkspaces] = useState<WorkspaceInfo[]>([]);
  const [workspaceId, setWorkspaceId] = useState('default');
  const [newWorkspace, setNewWorkspace] = useState(false);
  const [newWorkspaceId, setNewWorkspaceId] = useState('');
  const [workspaceError, setWorkspaceError] = useState<string | null>(null);

  useEffect(() => {
    workspaceAPI
      .list()
      .then((list) => {
        setWorkspaces(list);
        setWorkspaceId((cur) => (list.some((w) => w.id === cur) ? cur : list[0]?.id ?? 'default'));
      })
      .catch((err: unknown) => setWorkspaceError(errorTextOf(err, '工作空间读取失败')));
  }, []);

  const upsertWorkspace = (ws: WorkspaceInfo) => {
    setWorkspaces((prev) => {
      const i = prev.findIndex((w) => w.id === ws.id);
      if (i < 0) return [...prev, ws].sort((a, b) => a.id.localeCompare(b.id));
      const next = [...prev];
      next[i] = ws;
      return next;
    });
  };

  const handleCreateWorkspace = async () => {
    const id = newWorkspaceId.trim();
    if (!id) return;
    setWorkspaceError(null);
    try {
      const created = await workspaceAPI.create(id);
      upsertWorkspace(created);
      setWorkspaceId(created.id);
      setNewWorkspace(false);
      setNewWorkspaceId('');
    } catch (err) {
      setWorkspaceError(errorTextOf(err, '工作空间创建失败'));
    }
  };

  const selectedWorkspace = workspaces.find((w) => w.id === workspaceId);

  useEffect(() => {
    void useScheduleStore.getState().loadTasks();
    void useScheduleStore.getState().loadRuns();
    void loadKeepAwake();
  }, [loadKeepAwake]);

  const openCreateDialog = (preset?: Partial<{ name: string; desc: string; freq: ScheduleFreq; time: string; weekday: number; workspaceId: string }>) => {
    setEditingScheduleId(null);
    setTaskName(preset?.name ?? '');
    setTaskDesc(preset?.desc ?? '');
    setFreq(preset?.freq ?? 'daily');
    setTime(preset?.time ?? '09:00');
    setWeekday(preset?.weekday ?? 1);
    setWorkspaceId(preset?.workspaceId ?? workspaceId);
    setNewWorkspace(false);
    setNewWorkspaceId('');
    setWorkspaceError(null);
    setFormError(null);
    setDialogOpen(true);
  };

  /** 编辑已有任务：时刻按本地时区回显（存储为 UTC），工作空间沿用任务归属 */
  const openEditDialog = (t: ScheduledTask) => {
    setEditingScheduleId(t.scheduleId);
    setTaskName(t.name);
    setTaskDesc(t.description ?? '');
    setFreq(t.freq);
    setTime(utcToLocalTime(t.timeOfDay));
    setWeekday(t.weekday ?? 1);
    setWorkspaceId(t.workspaceId || 'default');
    setNewWorkspace(false);
    setNewWorkspaceId('');
    setWorkspaceError(null);
    setFormError(null);
    setDialogOpen(true);
  };

  // 通过对话创建：跳转新任务对话窗口并预置提示词
  const handleCreateByChat = () => {
    navigate(`/?prompt=${encodeURIComponent(SCHEDULE_CREATE_PROMPT)}`);
  };

  const canSave = taskName.trim().length > 0 && taskDesc.trim().length > 0;

  const handleSave = async () => {
    if (!canSave || saving) return;
    setSaving(true);
    setFormError(null);
    const plan = {
      name: taskName.trim(),
      description: taskDesc.trim(),
      freq,
      // 界面本地时刻 → UTC（Rust 侧按 UTC 存储与计算）
      timeOfDay: localToUtcTime(time),
      weekday: freq === 'weekly' ? weekday : null,
      workspaceId,
    };
    try {
      if (editingScheduleId) {
        const updated = await update({ scheduleId: editingScheduleId, ...plan });
        setDialogOpen(false);
        setToast({ text: `定时任务「${updated.name}」已更新`, severity: 'success' });
      } else {
        const created = await create(plan);
        setDialogOpen(false);
        setToast({ text: `定时任务「${created.name}」已创建`, severity: 'success' });
      }
    } catch (err) {
      setFormError(errorTextOf(err, editingScheduleId ? '定时任务更新失败' : '定时任务创建失败'));
    } finally {
      setSaving(false);
    }
  };

  const handleToggle = async (scheduleId: string, enabled: boolean) => {
    try {
      await toggle(scheduleId, enabled);
    } catch (err) {
      setToast({ text: errorTextOf(err, '定时任务状态更新失败'), severity: 'error' });
    }
  };

  const handleRunNow = async (scheduleId: string, name: string) => {
    try {
      await runNow(scheduleId);
      setTab('records');
      setToast({ text: `已触发「${name}」执行，结果将写入执行记录`, severity: 'info' });
    } catch (err) {
      setToast({ text: errorTextOf(err, '触发执行失败'), severity: 'error' });
    }
  };

  const handleDelete = async (scheduleId: string, name: string) => {
    try {
      await remove(scheduleId);
      setToast({ text: `已删除定时任务「${name}」`, severity: 'info' });
    } catch (err) {
      setToast({ text: errorTextOf(err, '删除定时任务失败'), severity: 'error' });
    }
  };

  const handleKeepAwake = async (enabled: boolean) => {
    try {
      const status = await setKeepAwake(enabled);
      if (enabled && !status.active) {
        setToast({
          text: status.supported ? '保持系统唤醒未能生效，请检查系统权限' : '当前平台暂不支持保持系统唤醒',
          severity: 'error',
        });
      }
    } catch (err) {
      setToast({ text: errorTextOf(err, '保持系统唤醒设置失败'), severity: 'error' });
    }
  };

  const taskNameOf = (scheduleId: string) =>
    tasks.find((t) => t.scheduleId === scheduleId)?.name ?? scheduleId;

  return (
    <Box sx={{ maxWidth: 1080, mx: 'auto' }}>
      {/* 页头 + 双入口 */}
      <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 2 }}>
        <Box sx={{ flexGrow: 1, minWidth: 0 }}>
          <Typography variant="h5" sx={{ fontWeight: 700 }}>定时任务</Typography>
          <Typography sx={{ fontSize: '13px', color: '#6B7280', mt: 0.5 }}>
            按计划自动执行任务，也可随时手动触发。在任意对话中描述你想定期做的事，即可快速创建
          </Typography>
        </Box>
        <Box sx={{ display: 'flex', gap: 1.25, flexShrink: 0 }}>
          <Button
            variant="outlined"
            startIcon={<ChatBubbleOutlineIcon />}
            onClick={handleCreateByChat}
            sx={{
              color: '#374151',
              borderColor: '#E5E7EB',
              borderRadius: '10px',
              textTransform: 'none',
              fontSize: '13px',
              fontWeight: 600,
              '&:hover': { borderColor: '#9CA3AF', background: 'rgba(15, 23, 42, 0.03)' },
            }}
          >
            通过对话创建
          </Button>
          <Button
            variant="contained"
            onClick={() => openCreateDialog()}
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
            + 新建定时任务
          </Button>
        </Box>
      </Box>

      {/* 提示条：保持系统唤醒 */}
      <Box
        sx={{
          mt: 2.5,
          px: 2,
          py: 1,
          borderRadius: '12px',
          background: 'rgba(59, 130, 246, 0.06)',
          border: '1px solid rgba(59, 130, 246, 0.15)',
          display: 'flex',
          alignItems: 'center',
          gap: 1,
        }}
      >
        <InfoOutlinedIcon sx={{ fontSize: 16, color: '#3B82F6' }} />
        <Box sx={{ flexGrow: 1, minWidth: 0 }}>
          <Typography sx={{ fontSize: '12.5px', color: '#1E40AF' }}>
            定时任务仅在电脑保持唤醒时运行
          </Typography>
          {keepAwake?.requested && !keepAwake.active && (
            <Typography sx={{ fontSize: '11.5px', color: '#B45309', mt: 0.25 }}>
              {keepAwake.supported
                ? '唤醒约束未生效，到点执行可能被系统休眠中断'
                : '当前平台不支持保持系统唤醒，请自行关闭系统休眠'}
            </Typography>
          )}
        </Box>
        <Typography sx={{ fontSize: '12.5px', color: '#4B5563' }}>保持系统唤醒</Typography>
        <Switch
          size="small"
          checked={Boolean(keepAwake?.requested)}
          onChange={(e) => void handleKeepAwake(e.target.checked)}
          inputProps={{ 'aria-label': '保持系统唤醒' }}
          sx={{
            '& .MuiSwitch-switchBase.Mui-checked': { color: '#3B82F6' },
            '& .MuiSwitch-switchBase.Mui-checked + .MuiSwitch-track': { backgroundColor: '#3B82F6' },
          }}
        />
      </Box>

      {/* 推荐示例 */}
      <Typography sx={{ fontSize: '13.5px', fontWeight: 700, color: '#111827', mt: 3, mb: 1.25 }}>
        推荐示例
      </Typography>
      <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 1.5 }}>
        {RECOMMENDS.map((r) => (
          <Box
            key={r.id}
            onClick={() =>
              openCreateDialog({ name: r.title, desc: r.desc, freq: r.freq, time: r.time, weekday: r.weekday })
            }
            sx={{
              border: '1px solid #EEF0F2',
              borderRadius: '14px',
              p: 2,
              background: '#FFFFFF',
              cursor: 'pointer',
              display: 'flex',
              flexDirection: 'column',
              gap: 1,
              transition: 'box-shadow 0.18s ease, transform 0.15s ease',
              '&:hover': { boxShadow: '0 6px 20px rgba(15, 23, 42, 0.07)', transform: 'translateY(-1px)' },
            }}
          >
            <Typography sx={{ fontSize: '13.5px', fontWeight: 700, color: '#111827' }}>{r.title}</Typography>
            <Typography
              sx={{
                fontSize: '12px',
                color: '#6B7280',
                lineHeight: 1.6,
                flexGrow: 1,
                display: '-webkit-box',
                WebkitLineClamp: 3,
                WebkitBoxOrient: 'vertical',
                overflow: 'hidden',
              }}
            >
              {r.desc}
            </Typography>
            <Box
              sx={{
                alignSelf: 'flex-start',
                display: 'flex',
                alignItems: 'center',
                gap: 0.5,
                px: 1,
                py: 0.35,
                borderRadius: '8px',
                background: '#F3F4F6',
              }}
            >
              <AccessTimeIcon sx={{ fontSize: 13, color: '#6B7280' }} />
              <Typography sx={{ fontSize: '11.5px', color: '#4B5563' }}>{r.schedule}</Typography>
            </Box>
          </Box>
        ))}
      </Box>

      {/* 我的定时任务 / 执行记录 */}
      <Box sx={{ display: 'flex', gap: 3, mt: 3.5, borderBottom: '1px solid #F3F4F6' }}>
        {([
          ['tasks', '我的定时任务'],
          ['records', '执行记录'],
        ] as const).map(([key, label]) => (
          <Box
            key={key}
            onClick={() => setTab(key)}
            sx={{
              pb: 1,
              cursor: 'pointer',
              borderBottom: tab === key ? '2px solid #111827' : '2px solid transparent',
            }}
          >
            <Typography sx={{ fontSize: '13.5px', fontWeight: tab === key ? 700 : 500, color: tab === key ? '#111827' : '#6B7280' }}>
              {label}
            </Typography>
          </Box>
        ))}
      </Box>

      {/* 列表 / 空态 */}
      {tab === 'tasks' && tasks.length > 0 && (
        <Box sx={{ mt: 2, display: 'flex', flexDirection: 'column', gap: 1.25 }}>
          {tasks.map((t) => (
            <Box
              key={t.scheduleId}
              sx={{
                border: '1px solid #EEF0F2',
                borderRadius: '14px',
                background: '#FFFFFF',
                px: 2,
                py: 1.5,
                display: 'flex',
                alignItems: 'flex-start',
                gap: 2,
                opacity: t.enabled ? 1 : 0.62,
              }}
            >
              <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                  <Typography sx={{ fontSize: '13.5px', fontWeight: 700, color: '#111827' }}>
                    {t.name}
                  </Typography>
                  <Box
                    sx={{
                      px: 0.75,
                      py: 0.2,
                      borderRadius: '6px',
                      background: '#F3F4F6',
                      display: 'flex',
                      alignItems: 'center',
                      gap: 0.4,
                    }}
                  >
                    <AccessTimeIcon sx={{ fontSize: 12, color: '#6B7280' }} />
                    <Typography sx={{ fontSize: '11.5px', color: '#4B5563' }}>
                      {describeSchedule(t.freq, t.timeOfDay, t.weekday)}
                    </Typography>
                  </Box>
                  {t.lastStatus && (
                    <Box
                      sx={{
                        px: 0.75,
                        py: 0.2,
                        borderRadius: '6px',
                        background: RUN_STATUS_COLORS[t.lastStatus].bg,
                      }}
                    >
                      <Typography sx={{ fontSize: '11.5px', color: RUN_STATUS_COLORS[t.lastStatus].fg }}>
                        上次{RUN_STATUS_LABELS[t.lastStatus]}
                      </Typography>
                    </Box>
                  )}
                </Box>
                <Typography
                  sx={{
                    fontSize: '12px',
                    color: '#6B7280',
                    mt: 0.5,
                    display: '-webkit-box',
                    WebkitLineClamp: 2,
                    WebkitBoxOrient: 'vertical',
                    overflow: 'hidden',
                  }}
                >
                  {t.description || '（无描述）'}
                </Typography>
                <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF', mt: 0.5 }}>
                  {t.enabled
                    ? t.nextRunAtMs
                      ? `下次执行：${formatLocalDateTime(t.nextRunAtMs)}`
                      : '已启用（等待排期）'
                    : '已暂停'}
                  {t.lastRunAtMs ? ` · 上次执行：${formatLocalDateTime(t.lastRunAtMs)}` : ''}
                </Typography>
              </Box>

              <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, flexShrink: 0 }}>
                <Button
                  size="small"
                  startIcon={<PlayArrowIcon sx={{ fontSize: 15 }} />}
                  onClick={() => void handleRunNow(t.scheduleId, t.name)}
                  sx={{
                    color: '#4B5563',
                    border: '1px solid #E5E7EB',
                    borderRadius: '8px',
                    textTransform: 'none',
                    fontSize: '12px',
                    px: 1.25,
                    minWidth: 0,
                    '&:hover': { borderColor: '#9CA3AF', background: 'rgba(15, 23, 42, 0.03)' },
                  }}
                >
                  立即执行
                </Button>
                <Button
                  size="small"
                  startIcon={<EditOutlinedIcon sx={{ fontSize: 15 }} />}
                  onClick={() => openEditDialog(t)}
                  sx={{
                    color: '#4B5563',
                    border: '1px solid #E5E7EB',
                    borderRadius: '8px',
                    textTransform: 'none',
                    fontSize: '12px',
                    px: 1.25,
                    minWidth: 0,
                    '&:hover': { borderColor: '#9CA3AF', background: 'rgba(15, 23, 42, 0.03)' },
                  }}
                >
                  编辑
                </Button>
                <Button
                  size="small"
                  startIcon={<DeleteOutlineIcon sx={{ fontSize: 15 }} />}
                  onClick={() => void handleDelete(t.scheduleId, t.name)}
                  sx={{
                    color: '#B91C1C',
                    border: '1px solid #FEE2E2',
                    borderRadius: '8px',
                    textTransform: 'none',
                    fontSize: '12px',
                    px: 1.25,
                    minWidth: 0,
                    '&:hover': { borderColor: '#FCA5A5', background: 'rgba(220, 38, 38, 0.05)' },
                  }}
                >
                  删除
                </Button>
                <Switch
                  size="small"
                  checked={t.enabled}
                  onChange={(e) => void handleToggle(t.scheduleId, e.target.checked)}
                  inputProps={{ 'aria-label': `启用 ${t.name}` }}
                  sx={{
                    '& .MuiSwitch-switchBase.Mui-checked': { color: '#3B82F6' },
                    '& .MuiSwitch-switchBase.Mui-checked + .MuiSwitch-track': { backgroundColor: '#3B82F6' },
                  }}
                />
              </Box>
            </Box>
          ))}
        </Box>
      )}

      {tab === 'records' && runs.length > 0 && (
        <Box sx={{ mt: 2, display: 'flex', flexDirection: 'column', gap: 1 }}>
          {runs.map((r) => (
            <Box
              key={r.runId}
              sx={{
                border: '1px solid #EEF0F2',
                borderRadius: '12px',
                background: '#FFFFFF',
                px: 2,
                py: 1.25,
                display: 'flex',
                alignItems: 'center',
                gap: 1.5,
              }}
            >
              <Box
                sx={{
                  px: 0.9,
                  py: 0.25,
                  borderRadius: '6px',
                  background: RUN_STATUS_COLORS[r.status].bg,
                  flexShrink: 0,
                }}
              >
                <Typography sx={{ fontSize: '11.5px', fontWeight: 600, color: RUN_STATUS_COLORS[r.status].fg }}>
                  {RUN_STATUS_LABELS[r.status]}
                </Typography>
              </Box>
              <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#111827' }}>
                  {taskNameOf(r.scheduleId)}
                </Typography>
                <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF', mt: 0.25 }}>
                  {formatLocalDateTime(r.startedAtMs)}
                  {r.finishedAtMs ? ` · 耗时 ${Math.max(0, Math.round((r.finishedAtMs - r.startedAtMs) / 1000))}s` : ''}
                  {r.taskId ? ` · 任务 ${r.taskId.slice(0, 12)}` : ''}
                </Typography>
                {r.summary && (
                  <Typography sx={{ fontSize: '11.5px', color: '#6B7280', mt: 0.4 }}>{r.summary}</Typography>
                )}
              </Box>
            </Box>
          ))}
        </Box>
      )}

      {((tab === 'tasks' && tasks.length === 0) || (tab === 'records' && runs.length === 0)) && (
        <Box
          sx={{
            mt: 2.5,
            py: 7,
            borderRadius: '16px',
            border: '1px dashed #E5E7EB',
            background: '#FBFCFD',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            gap: 1,
          }}
        >
          <ScheduleOutlinedIcon sx={{ fontSize: 40, color: '#C4CAD4' }} />
          <Typography sx={{ fontSize: '14px', fontWeight: 600, color: '#374151' }}>
            {tab === 'tasks'
              ? tasksLoading
                ? '正在加载定时任务…'
                : '暂无定时任务'
              : runsLoading
                ? '正在加载执行记录…'
                : '暂无执行记录'}
          </Typography>
          <Typography sx={{ fontSize: '12.5px', color: '#9CA3AF' }}>
            {tab === 'tasks' ? '创建定时任务来自动化执行周期性 AI 代理工作' : '定时任务运行后将在此展示执行结果'}
          </Typography>
          {tab === 'tasks' && (
            <Button
              variant="contained"
              onClick={() => openCreateDialog()}
              sx={{
                mt: 1,
                bgcolor: '#111827',
                borderRadius: '10px',
                textTransform: 'none',
                fontSize: '13px',
                fontWeight: 600,
                px: 2,
                '&:hover': { bgcolor: '#4F46E5' },
              }}
            >
              + 新建定时任务
            </Button>
          )}
        </Box>
      )}

      {/* 新建定时任务弹窗 */}
      <Dialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        PaperProps={{ sx: { width: 560, borderRadius: '16px', p: 1 } }}
      >
        <DialogTitle sx={{ pb: 0.5 }}>
          <Typography sx={{ fontSize: '16px', fontWeight: 700 }}>
            {editingScheduleId ? '编辑定时任务' : '新建定时任务'}
          </Typography>
          <Typography sx={{ fontSize: '12.5px', color: '#9CA3AF', mt: 0.25 }}>
            {editingScheduleId
              ? '修改计划后将按当前时刻重新计算下次执行时间'
              : '按计划自动执行任务，也可随时手动触发。在任意对话中描述你想定期做的事，即可快速创建'}
          </Typography>
        </DialogTitle>
        <DialogContent sx={{ pt: '12px !important' }}>
          {/* 任务名称 */}
          <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>任务名称</Typography>
          <TextField
            value={taskName}
            onChange={(e) => setTaskName(e.target.value)}
            fullWidth
            size="small"
            placeholder="描述你的任务"
            inputProps={{ 'aria-label': '任务名称' }}
            sx={{ mb: 2, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
          />

          {/* 工作空间 */}
          <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>
            工作空间
          </Typography>
          {newWorkspace ? (
            <Box sx={{ display: 'flex', gap: 1, mb: 0.75, alignItems: 'center' }}>
              <TextField
                value={newWorkspaceId}
                onChange={(e) => setNewWorkspaceId(e.target.value)}
                fullWidth
                size="small"
                autoFocus
                placeholder="新工作空间标识（小写字母、数字、下划线、连字符）"
                inputProps={{ 'aria-label': '新工作空间标识' }}
                sx={{ '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
              />
              <Button
                size="small"
                variant="outlined"
                disabled={newWorkspaceId.trim().length === 0}
                onClick={handleCreateWorkspace}
                sx={{ flexShrink: 0, textTransform: 'none', fontSize: '13px' }}
              >
                创建
              </Button>
              <Button
                size="small"
                variant="text"
                onClick={() => {
                  setNewWorkspace(false);
                  setNewWorkspaceId('');
                  setWorkspaceError(null);
                }}
                sx={{ flexShrink: 0, textTransform: 'none', fontSize: '13px', color: '#6B7280' }}
              >
                取消
              </Button>
            </Box>
          ) : (
            <Box sx={{ display: 'flex', gap: 1, mb: 0.75, alignItems: 'center' }}>
              <Select
                value={workspaceId}
                onChange={(e) => {
                  setWorkspaceId(e.target.value as string);
                  setWorkspaceError(null);
                }}
                size="small"
                sx={{ flexGrow: 1, fontSize: '13px', borderRadius: '10px' }}
                inputProps={{ 'aria-label': '工作空间' }}
              >
                {workspaces.map((w) => (
                  <MenuItem key={w.id} value={w.id} sx={{ fontSize: '13px' }}>
                    {w.id}
                    {w.running ? '（运行中）' : !w.exists ? '（未落地）' : ''}
                  </MenuItem>
                ))}
              </Select>
              <Button
                size="small"
                variant="text"
                onClick={() => setNewWorkspace(true)}
                sx={{ flexShrink: 0, textTransform: 'none', fontSize: '13px', color: '#0D9488' }}
              >
                ＋ 新建
              </Button>
            </Box>
          )}
          <Box sx={{ display: 'flex', gap: 0.5, alignItems: 'center', mb: 2 }}>
            <FolderOutlinedIcon sx={{ fontSize: 13, color: '#9CA3AF' }} />
            <Typography sx={{ fontSize: '11.5px', color: workspaceError ? '#DC2626' : '#9CA3AF' }}>
              {workspaceError ??
                (selectedWorkspace
                  ? `${selectedWorkspace.rootPath}${selectedWorkspace.exists ? '' : ' · 目录尚未创建，首次执行时落地'}`
                  : `运行目录/workspaces/${workspaceId}`)}
            </Typography>
          </Box>

          {/* 计划时间 */}
          <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>计划时间</Typography>
          <Box sx={{ display: 'flex', gap: 1, mb: 2, alignItems: 'center' }}>
            <Select
              value={freq}
              onChange={(e) => setFreq(e.target.value as ScheduleFreq)}
              size="small"
              inputProps={{ 'aria-label': '执行频率' }}
              sx={{ width: 130, fontSize: '13px', borderRadius: '10px' }}
            >
              {SCHEDULE_FREQS.map((f) => (
                <MenuItem key={f} value={f}>{freqLabel(f)}</MenuItem>
              ))}
            </Select>
            {freq === 'weekly' && (
              <Select
                value={weekday}
                onChange={(e) => setWeekday(Number(e.target.value))}
                size="small"
                inputProps={{ 'aria-label': '星期' }}
                sx={{ width: 110, fontSize: '13px', borderRadius: '10px' }}
              >
                {Object.entries(WEEKDAY_LABELS).map(([value, label]) => (
                  <MenuItem key={value} value={Number(value)}>{label}</MenuItem>
                ))}
              </Select>
            )}
            <TextField
              value={time}
              onChange={(e) => setTime(e.target.value)}
              type="time"
              size="small"
              inputProps={{ 'aria-label': '执行时刻' }}
              sx={{ width: 140, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
            />
            <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF' }}>
              {freq === 'hourly' ? '按分钟触发（时字段忽略）' : '按本地时间'}
            </Typography>
          </Box>

          {/* 任务描述 */}
          <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>
            让通天办公帮你做什么...
          </Typography>
          <TextField
            value={taskDesc}
            onChange={(e) => setTaskDesc(e.target.value)}
            fullWidth
            multiline
            minRows={4}
            placeholder="让通天办公帮你做什么..."
            inputProps={{ 'aria-label': '任务描述' }}
            sx={{ mb: 1.5, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
          />

          {formError && (
            <Alert severity="error" sx={{ mb: 1.5, fontSize: '12.5px' }}>{formError}</Alert>
          )}

          {/* 工作目录 + 高级 */}
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 2 }}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, color: '#6B7280' }}>
              <FolderOutlinedIcon sx={{ fontSize: 15 }} />
              <Typography sx={{ fontSize: '12.5px' }}>默认工作目录</Typography>
            </Box>
            <Box sx={{ flexGrow: 1 }} />
            <Typography sx={{ fontSize: '12.5px', color: '#6B7280' }}>到点自动批准执行</Typography>
          </Box>

          {/* 取消 / 保存 */}
          <Box sx={{ display: 'flex', justifyContent: 'flex-end', gap: 1, mb: 1 }}>
            <Button
              onClick={() => setDialogOpen(false)}
              sx={{
                color: '#4B5563',
                border: '1px solid #E5E7EB',
                borderRadius: '10px',
                textTransform: 'none',
                fontSize: '13px',
                fontWeight: 600,
                px: 2.5,
                '&:hover': { background: 'rgba(15, 23, 42, 0.03)' },
              }}
            >
              取消
            </Button>
            <Button
              variant="contained"
              disabled={!canSave || saving}
              onClick={() => void handleSave()}
              sx={{
                bgcolor: canSave ? '#111827' : '#E5E7EB',
                borderRadius: '10px',
                textTransform: 'none',
                fontSize: '13px',
                fontWeight: 700,
                px: 3,
                '&:hover': { bgcolor: '#4F46E5' },
                '&.Mui-disabled': { bgcolor: '#E5E7EB', color: '#9CA3AF' },
              }}
            >
              {saving ? (editingScheduleId ? '保存中…' : '创建中…') : editingScheduleId ? '保存修改' : '创建任务'}
            </Button>
          </Box>
        </DialogContent>
      </Dialog>

      {/* 结果提示（成功/失败/说明，不静默） */}
      <Snackbar
        open={!!toast}
        autoHideDuration={3500}
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

export default ScheduleView;