/**
 * 通天办公 - 意识页 (对标 QwenWork 意识/记忆)
 *
 * 设计:
 * - 协作风格: 风格选择 + 自定义提示词
 * - 工作手册: Markdown 编辑区（跨会话长期上下文）
 * - 自动记忆: 开关 + 记忆条目列表（auto/manual/reflection）+ 手动添加
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
  Box,
  Typography,
  Switch,
  Button,
  TextField,
  Chip,
} from '@mui/material';
import PsychologyIcon from '@mui/icons-material/Psychology';
import AddIcon from '@mui/icons-material/Add';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import {
  getAwareness,
  updateAwareness,
  listMemories,
  addMemory,
  deleteMemory,
} from '../../lib/alignment-api';
import type { MemoryEntry } from '../../../shared/contracts';

const tealSwitch = {
  '& .MuiSwitch-switchBase.Mui-checked': { color: '#14B8A6' },
  '& .MuiSwitch-switchBase.Mui-checked + .MuiSwitch-track': { backgroundColor: '#14B8A6' },
};

// ── 协作风格选项 ──────────────────────────────────────────────────

const styles = [
  { id: 'professional', label: '专业严谨', desc: '正式用语，结构化输出' },
  { id: 'friendly', label: '轻松友好', desc: '自然对话，适度表情符号' },
  { id: 'concise', label: '简洁高效', desc: '直奔主题，最少冗余' },
  { id: 'custom', label: '自定义', desc: '完全按自定义提示词' },
];

// ── 记忆条目模型（开发阶段 Mock，联调阶段走 awareness IPC） ────────

// ── 记忆条目模型（类型来自 shared/contracts，持久化走 alignment-api） ────

const initialMemories: MemoryEntry[] = [];

const sourceLabel: Record<string, { label: string; color: string }> = {
  auto: { label: '自动', color: '#0D9488' },
  manual: { label: '手动', color: '#2563EB' },
  reflection: { label: '反思', color: '#D97706' },
};

// ── 区块标题 ──────────────────────────────────────────────────────

const SectionTitle: React.FC<{ title: string; sub: string }> = ({ title, sub }) => (
  <Box sx={{ mb: 1.5 }}>
    <Typography sx={{ fontSize: '16px', fontWeight: 700, color: '#111827' }}>{title}</Typography>
    <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mt: 0.25 }}>{sub}</Typography>
  </Box>
);

// ── 主页面 ────────────────────────────────────────────────────────

const AwarenessPage: React.FC = () => {
  const [mode, setMode] = useState(true);
  const [autoMemory, setAutoMemory] = useState(true);
  const [style, setStyle] = useState('professional');
  const [customPrompt, setCustomPrompt] = useState('');
  const [manual, setManual] = useState('');
  const [memories, setMemories] = useState<MemoryEntry[]>(initialMemories);
  const [newMemory, setNewMemory] = useState('');
  const [saving, setSaving] = useState(false);

  // 从持久层加载意识配置与记忆（真实 IPC）
  useEffect(() => {
    getAwareness()
      .then((p) => {
        setMode(p.mode);
        setAutoMemory(p.autoMemory);
        setStyle(p.style);
        setCustomPrompt(p.customPrompt);
        setManual(p.workManual);
      })
      .catch(() => undefined);
    listMemories()
      .then(setMemories)
      .catch(() => undefined);
  }, []);

  const handleAddMemory = useCallback(() => {
    const content = newMemory.trim();
    if (!content) return;
    setNewMemory('');
    addMemory(content)
      .then((entry) => setMemories((ms) => [entry, ...ms]))
      .catch(() => undefined);
  }, [newMemory]);

  return (
    <Box sx={{ height: 'calc(100vh - 104px)', overflowY: 'auto', pr: 1 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <PsychologyIcon sx={{ fontSize: 22, color: '#0D9488' }} />
        <Typography sx={{ fontSize: '20px', fontWeight: 700, color: '#111827' }}>意识</Typography>
        <Box sx={{ flexGrow: 1 }} />
        <Typography sx={{ fontSize: '13px', color: '#6B7280', mr: 1 }}>意识模式</Typography>
        <Switch size="small" checked={mode} onChange={(e) => { setMode(e.target.checked); updateAwareness({ mode: e.target.checked }).catch(() => undefined); }} sx={tealSwitch} />
      </Box>
      <Typography sx={{ fontSize: '13px', color: '#9CA3AF', mt: 0.5 }}>
        定制协作风格与工作手册，赋予通天办公跨会话记忆与技能进化能力。
      </Typography>

      <Box sx={{ maxWidth: 860, mt: 3, opacity: mode ? 1 : 0.45, pointerEvents: mode ? 'auto' : 'none' }}>
        {/* 区块一：协作风格 */}
        <SectionTitle title="协作风格" sub="影响 Agent 的语气、详略与输出结构，作为 system prompt 前置层注入" />
        <Box sx={{ display: 'flex', gap: 1.5, flexWrap: 'wrap', mb: 2 }}>
          {styles.map((s) => (
            <Box
              key={s.id}
              onClick={() => {
                setStyle(s.id);
                updateAwareness({ style: s.id as 'professional' | 'friendly' | 'concise' | 'custom' }).catch(() => undefined);
              }}
              sx={{
                px: 2,
                py: 1.25,
                borderRadius: '12px',
                border: style === s.id ? '1.5px solid #14B8A6' : '1px solid #E5E7EB',
                bgcolor: style === s.id ? '#F0FDFA' : '#FFFFFF',
                cursor: 'pointer',
                minWidth: 150,
              }}
            >
              <Typography sx={{ fontSize: '13px', fontWeight: 600, color: style === s.id ? '#0D9488' : '#111827' }}>
                {s.label}
              </Typography>
              <Typography sx={{ fontSize: '11px', color: '#9CA3AF', mt: 0.25 }}>{s.desc}</Typography>
            </Box>
          ))}
        </Box>
        {style === 'custom' && (
          <TextField
            fullWidth
            multiline
            minRows={3}
            placeholder="输入自定义协作风格提示词…"
            value={customPrompt}
            onChange={(e) => setCustomPrompt(e.target.value)}
            sx={{ mb: 2, '& .MuiInputBase-input': { fontSize: '13px' } }}
          />
        )}

        {/* 区块二：工作手册 */}
        <SectionTitle title="工作手册" sub="沉淀你的工作规范、偏好与常用流程（Markdown），作为跨会话长期上下文" />
        <TextField
          fullWidth
          multiline
          minRows={6}
          value={manual}
          onChange={(e) => setManual(e.target.value)}
          sx={{
            mb: 1,
            '& .MuiInputBase-input': { fontSize: '13px', fontFamily: 'Consolas, monospace' },
          }}
        />
        <Button
          size="small"
          variant="contained"
          disabled={saving}
          onClick={() => {
            setSaving(true);
            updateAwareness({ workManual: manual, customPrompt })
              .catch(() => undefined)
              .finally(() => setSaving(false));
          }}
          sx={{ bgcolor: '#111827', fontSize: '12px', mb: 3, '&:hover': { bgcolor: '#1F2937' } }}
        >
          {saving ? '保存中…' : '保存工作手册'}
        </Button>

        {/* 区块三：自动记忆 */}
        <Box sx={{ display: 'flex', alignItems: 'center', mb: 1.5 }}>
          <Box sx={{ flexGrow: 1 }}>
            <Typography sx={{ fontSize: '16px', fontWeight: 700, color: '#111827' }}>自动记忆</Typography>
            <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mt: 0.25 }}>
              Agent 在会话中自动提取值得记住的事实，并周期性产出反思经验
            </Typography>
          </Box>
          <Switch size="small" checked={autoMemory} onChange={(e) => { setAutoMemory(e.target.checked); updateAwareness({ autoMemory: e.target.checked }).catch(() => undefined); }} sx={tealSwitch} />
        </Box>

        <Box sx={{ display: 'flex', gap: 1.5, mb: 2 }}>
          <TextField
            size="small"
            fullWidth
            placeholder="手动添加一条记忆…"
            value={newMemory}
            onChange={(e) => setNewMemory(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleAddMemory()}
            sx={{ '& .MuiInputBase-input': { fontSize: '13px' } }}
          />
          <Button
            size="small"
            startIcon={<AddIcon sx={{ fontSize: 16 }} />}
            onClick={handleAddMemory}
            sx={{ bgcolor: '#111827', color: '#fff', fontSize: '12px', '&:hover': { bgcolor: '#1F2937' } }}
          >
            添加
          </Button>
        </Box>

        {memories.map((m) => {
          const src = sourceLabel[m.source];
          return (
            <Box
              key={m.id}
              sx={{
                display: 'flex',
                alignItems: 'flex-start',
                gap: 1.5,
                bgcolor: '#FFFFFF',
                border: '1px solid #E5E7EB',
                borderRadius: '14px',
                px: 2,
                py: 1.5,
                mb: 1,
              }}
            >
              <Chip
                label={src.label}
                size="small"
                sx={{ bgcolor: `${src.color}1A`, color: src.color, fontSize: '11px', fontWeight: 600, height: 22 }}
              />
              <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                <Typography sx={{ fontSize: '13px', color: '#374151' }}>{m.content}</Typography>
                <Typography sx={{ fontSize: '11px', color: '#9CA3AF', mt: 0.25 }}>{m.createdAt}</Typography>
              </Box>
              <DeleteOutlineIcon
                sx={{ fontSize: 17, color: '#9CA3AF', cursor: 'pointer', '&:hover': { color: '#EF4444' } }}
                onClick={() => {
                  setMemories((ms) => ms.filter((x) => x.id !== m.id));
                  deleteMemory(m.id).catch(() => undefined);
                }}
              />
            </Box>
          );
        })}
      </Box>
    </Box>
  );
};

export default AwarenessPage;
