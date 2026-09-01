/**
 * 通天办公 - 定时任务页面（设计图对齐版）
 *
 * 结构：
 * - 头部：标题 + 说明 + 「通过对话创建」「+ 新建定时任务」双入口
 * - 提示条：定时任务仅在电脑保持唤醒时运行 + 保持系统唤醒开关
 * - 推荐示例：三张示例卡片（点击即预填创建弹窗）
 * - 我的定时任务 / 执行记录 双页签 + 空态
 * - 新建定时任务弹窗：任务名称 / 计划时间（频率 + 时间）/ 任务描述
 *
 * Mock 开关规范：
 * - 开发阶段（VITE_MOCK !== 'false'）：创建仅本地模拟提示
 * - 联调阶段（VITE_MOCK === 'false'）：预留真实调度注册接口位
 */

import React, { useState } from 'react';
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
import { useNavigate } from 'react-router-dom';

// ── 对话式创建的预置提示词 ──────────────────────────────────────────

const SCHEDULE_CREATE_PROMPT = '我要创建一个定时任务，每【时间间隔】执行【具体任务】';

const mockEnabled = import.meta.env.VITE_MOCK !== 'false';

// ── 推荐示例（对齐设计图） ──────────────────────────────────────────

interface Recommend {
  id: string;
  title: string;
  desc: string;
  schedule: string;
  freq: string;
  time: string;
}

const RECOMMENDS: Recommend[] = [
  {
    id: 'brand-monitor',
    title: '品牌舆情监控',
    desc: '我要创建一个定时任务，每【天】执行【搜索【品牌名】品牌近 24 小时舆情，汇总正负面情绪与热点话题】',
    schedule: '每日下午 2 点',
    freq: '每天',
    time: '14:00',
  },
  {
    id: 'movie-daily',
    title: '每日电影推荐',
    desc: '我要创建一个定时任务，每【天】执行【给我推荐一部公认的经典电影（评分 8.5 以上），附推荐理由】',
    schedule: '每日晚 8 点',
    freq: '每天',
    time: '20:00',
  },
  {
    id: 'stock-monitor',
    title: '金融股票监控',
    desc: '我要创建一个定时任务，每【工作日】执行【帮我查询【英伟达股票】今日的走势与关键新闻摘要】',
    schedule: '工作日早 8 点',
    freq: '每周',
    time: '08:00',
  },
];

const FREQ_OPTIONS = ['每天', '每周', '每月', '每小时'];

// ── 页面组件 ──────────────────────────────────────────────────────

const ScheduleView: React.FC = () => {
  const navigate = useNavigate();
  const [tab, setTab] = useState<'tasks' | 'records'>('tasks');
  const [keepAwake, setKeepAwake] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  // 新建定时任务弹窗状态
  const [dialogOpen, setDialogOpen] = useState(false);
  const [taskName, setTaskName] = useState('');
  const [freq, setFreq] = useState('每天');
  const [time, setTime] = useState('09:00');
  const [taskDesc, setTaskDesc] = useState('');

  const openCreateDialog = (preset?: Partial<{ name: string; desc: string; freq: string; time: string }>) => {
    setTaskName(preset?.name ?? '');
    setTaskDesc(preset?.desc ?? '');
    setFreq(preset?.freq ?? '每天');
    setTime(preset?.time ?? '09:00');
    setDialogOpen(true);
  };

  // 通过对话创建：跳转新任务对话窗口并预置提示词
  const handleCreateByChat = () => {
    navigate(`/?prompt=${encodeURIComponent(SCHEDULE_CREATE_PROMPT)}`);
  };

  // 保存（Mock 开关：开发阶段本地模拟，联调阶段接真实调度注册）
  const canSave = taskName.trim().length > 0 && taskDesc.trim().length > 0;
  const handleSave = () => {
    if (!canSave) return;
    setDialogOpen(false);
    if (mockEnabled) {
      setToast(`定时任务「${taskName.trim()}」已创建（开发阶段 Mock，${freq} ${time} 执行）`);
      return;
    }
    // 联调阶段：真实调度注册接口接入位
    setToast(`定时任务「${taskName.trim()}」创建请求已提交`);
  };

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
        <Typography sx={{ fontSize: '12.5px', color: '#1E40AF', flexGrow: 1 }}>
          定时任务仅在电脑保持唤醒时运行
        </Typography>
        <Typography sx={{ fontSize: '12.5px', color: '#4B5563' }}>保持系统唤醒</Typography>
        <Switch
          size="small"
          checked={keepAwake}
          onChange={(e) => setKeepAwake(e.target.checked)}
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
            onClick={() => openCreateDialog({ name: r.title, desc: r.desc, freq: r.freq, time: r.time })}
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

      {/* 空态 */}
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
          {tab === 'tasks' ? '暂无定时任务' : '暂无执行记录'}
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

      {/* 新建定时任务弹窗 */}
      <Dialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        PaperProps={{ sx: { width: 560, borderRadius: '16px', p: 1 } }}
      >
        <DialogTitle sx={{ pb: 0.5 }}>
          <Typography sx={{ fontSize: '16px', fontWeight: 700 }}>新建定时任务</Typography>
          <Typography sx={{ fontSize: '12.5px', color: '#9CA3AF', mt: 0.25 }}>
            按计划自动执行任务，也可随时手动触发。在任意对话中描述你想定期做的事，即可快速创建
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
            sx={{ mb: 2, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
          />

          {/* 计划时间 */}
          <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#374151', mb: 0.75 }}>计划时间</Typography>
          <Box sx={{ display: 'flex', gap: 1, mb: 2 }}>
            <Select
              value={freq}
              onChange={(e) => setFreq(e.target.value)}
              size="small"
              sx={{ width: 140, fontSize: '13px', borderRadius: '10px' }}
            >
              {FREQ_OPTIONS.map((f) => (
                <MenuItem key={f} value={f}>{f}</MenuItem>
              ))}
            </Select>
            <TextField
              value={time}
              onChange={(e) => setTime(e.target.value)}
              type="time"
              size="small"
              sx={{ width: 140, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
            />
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
            sx={{ mb: 1.5, '& .MuiOutlinedInput-root': { fontSize: '13px' } }}
          />

          {/* 工作目录 + 高级 */}
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 2 }}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, color: '#6B7280' }}>
              <FolderOutlinedIcon sx={{ fontSize: 15 }} />
              <Typography sx={{ fontSize: '12.5px' }}>选择工作目录</Typography>
            </Box>
            <Box sx={{ flexGrow: 1 }} />
            <Typography sx={{ fontSize: '12.5px', color: '#6B7280', cursor: 'pointer' }}>高级 ▾</Typography>
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
              disabled={!canSave}
              onClick={handleSave}
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
              保存
            </Button>
          </Box>
        </DialogContent>
      </Dialog>

      {/* 占位提示 toast（不静默） */}
      <Snackbar open={!!toast} autoHideDuration={3000} onClose={() => setToast(null)} anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}>
        <Alert severity="info" onClose={() => setToast(null)} sx={{ fontSize: '13px' }}>
          {toast}
        </Alert>
      </Snackbar>
    </Box>
  );
};

export default ScheduleView;
