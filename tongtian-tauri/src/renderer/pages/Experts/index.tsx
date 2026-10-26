/**
 * 通天办公 - 专家团队（多专家运行时）
 *
 * 事实来源：
 * - 注册表落在桌面端 `settings_kv.experts.registry`（Rust 读写），浏览器直连/Mock 通道没有
 *   权威存储，页面如实说明并禁用写操作，不展示示例专家；
 * - 绑定的模型只在保存/列表时做可用性解析，接口不返回密钥；
 * - 删除只移除注册表条目，不动任何任务、会话与文件。
 *
 * 运行时行为：
 * - 负责人专家：本轮由该专家的角色提示词组织结论，绑定模型优先于对话里选的模型；
 * - 子 Agent：主 Agent 按职责描述派发，子任务使用专家绑定的模型与技能白名单。
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Box,
  Button,
  Checkbox,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  FormControlLabel,
  IconButton,
  MenuItem,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import GroupsIcon from '@mui/icons-material/Groups';
import AddIcon from '@mui/icons-material/Add';
import EditOutlinedIcon from '@mui/icons-material/EditOutlined';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import RefreshIcon from '@mui/icons-material/Refresh';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import type { ExpertInfo } from '../../../shared/contracts';
import type { ModelConfig } from '../../../shared/types';
import { errorTextOf } from '../../../shared/error-messages';
import { expertsAPI, modelAPI } from '../../lib/api';
import { loadAllSkills, type SkillInfo } from '../../lib/skills';
import { isTauri } from '../../lib/tauri-api';
import { useAppStore } from '../../stores/useAppStore';
import { useWorkbenchStore } from '../../store/workbenchStore';

const NAME_MAX = 40;
const DESCRIPTION_MAX = 300;
const PROMPT_MAX = 8000;
const SKILLS_MAX = 20;

interface ExpertsPageProps {
  /** 作为专家套件页的页签嵌入时，由外层容器负责滚动 */
  embedded?: boolean;
}

const ExpertsPage: React.FC<ExpertsPageProps> = ({ embedded = false }) => {
  const desktop = isTauri();
  const showNotification = useAppStore((s) => s.showNotification);

  const [experts, setExperts] = useState<ExpertInfo[]>([]);
  const [loading, setLoading] = useState(desktop);
  const [loadError, setLoadError] = useState('');

  const [models, setModels] = useState<ModelConfig[]>([]);
  const [skills, setSkills] = useState<SkillInfo[]>([]);

  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<ExpertInfo | null>(null);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [prompt, setPrompt] = useState('');
  const [modelId, setModelId] = useState('');
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  const [deleteTarget, setDeleteTarget] = useState<ExpertInfo | null>(null);
  const [deleting, setDeleting] = useState(false);

  const refresh = useCallback(async () => {
    if (!desktop) {
      setLoading(false);
      setExperts([]);
      return;
    }
    setLoading(true);
    try {
      const list = await expertsAPI.list();
      setExperts(Array.isArray(list) ? list : []);
      setLoadError('');
    } catch (err) {
      setExperts([]);
      setLoadError(errorTextOf(err, '专家注册表读取失败'));
    } finally {
      setLoading(false);
    }
  }, [desktop]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 模型与技能只在桌面通道读取：编辑时用来绑定模型、限定技能白名单
  useEffect(() => {
    if (!desktop) return;
    let cancelled = false;
    modelAPI
      .list()
      .then((list) => {
        if (!cancelled) setModels(Array.isArray(list) ? list : []);
      })
      .catch(() => {
        if (!cancelled) setModels([]);
      });
    loadAllSkills()
      .then((list) => {
        if (!cancelled) setSkills(list);
      })
      .catch(() => {
        if (!cancelled) setSkills([]);
      });
    return () => {
      cancelled = true;
    };
  }, [desktop]);

  const modelOptions = useMemo(
    () => models.map((m) => ({ id: m.id, label: `${m.name || m.modelName}（${m.modelName}）` })),
    [models],
  );

  const openCreate = () => {
    setEditing(null);
    setName('');
    setDescription('');
    setPrompt('');
    setModelId('');
    setSelectedSkills([]);
    setEditorOpen(true);
  };

  const openEdit = (expert: ExpertInfo) => {
    setEditing(expert);
    setName(expert.name);
    setDescription(expert.description);
    setPrompt(expert.prompt);
    setModelId(expert.modelId ?? '');
    setSelectedSkills(expert.skills ?? []);
    setEditorOpen(true);
  };

  const toggleSkill = (skillId: string) => {
    setSelectedSkills((prev) => {
      if (prev.includes(skillId)) return prev.filter((s) => s !== skillId);
      if (prev.length >= SKILLS_MAX) {
        showNotification(`单个专家最多选择 ${SKILLS_MAX} 个技能`, 'warning');
        return prev;
      }
      return [...prev, skillId];
    });
  };

  const submitEditor = async () => {
    if (saving || !desktop) return;
    if (!name.trim()) {
      showNotification('专家名称不能为空', 'warning');
      return;
    }
    if (!description.trim()) {
      showNotification('职责描述不能为空：主 Agent 靠它决定把什么子任务交给该专家', 'warning');
      return;
    }
    if (!prompt.trim()) {
      showNotification('角色提示词不能为空', 'warning');
      return;
    }
    setSaving(true);
    try {
      const saved = await expertsAPI.save({
        id: editing?.id ?? null,
        name: name.trim(),
        description: description.trim(),
        prompt: prompt.trim(),
        modelId: modelId.trim() || null,
        skills: selectedSkills,
      });
      if (editing) {
        setExperts((prev) => prev.map((e) => (e.id === saved.id ? saved : e)));
      } else {
        setExperts((prev) => [...prev, saved]);
      }
      showNotification(`专家「${saved.name}」已保存`, 'success');
      setEditorOpen(false);
    } catch (err) {
      showNotification(errorTextOf(err, '专家保存失败'), 'error');
    } finally {
      setSaving(false);
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget || deleting) return;
    setDeleting(true);
    try {
      await expertsAPI.remove(deleteTarget.id);
      setExperts((prev) => prev.filter((e) => e.id !== deleteTarget.id));
      // 对话里正选着这个专家：清掉选择，避免下一轮提交必报 EXPERT_NOT_FOUND
      if (useWorkbenchStore.getState().chatExpertId === deleteTarget.id) {
        useWorkbenchStore.getState().setChatExpertId('');
      }
      showNotification(`专家「${deleteTarget.name}」已删除（任务与会话不受影响）`, 'success');
      setDeleteTarget(null);
    } catch (err) {
      showNotification(errorTextOf(err, '专家删除失败'), 'error');
    } finally {
      setDeleting(false);
    }
  };

  const modelLabelOf = (expert: ExpertInfo) => {
    if (expert.modelError) return `绑定模型不可用：${expert.modelError}`;
    if (expert.modelName) return `模型：${expert.modelName}`;
    return '沿用对话中选择的模型';
  };

  return (
    <Box
      sx={{
        height: embedded ? 'auto' : 'calc(100vh - 104px)',
        overflowY: embedded ? 'visible' : 'auto',
        pr: embedded ? 0 : 1,
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        {!embedded && (
          <>
            <GroupsIcon sx={{ fontSize: 22, color: '#8B5CF6' }} />
            <Typography sx={{ fontSize: '20px', fontWeight: 700, color: 'var(--tt-text-1)' }}>
              专家团队
            </Typography>
          </>
        )}
        {loading && <CircularProgress size={16} sx={{ color: '#8B5CF6' }} />}
        <Box sx={{ flexGrow: 1 }} />
        <Tooltip title="刷新专家列表">
          <span>
            <IconButton size="small" onClick={() => void refresh()} disabled={!desktop || loading}>
              <RefreshIcon sx={{ fontSize: 18 }} />
            </IconButton>
          </span>
        </Tooltip>
        <Button
          size="small"
          variant="contained"
          startIcon={<AddIcon />}
          onClick={openCreate}
          disabled={!desktop}
          sx={{ textTransform: 'none', borderRadius: '8px', bgcolor: '#7C3AED', '&:hover': { bgcolor: '#6D28D9' } }}
        >
          新建专家
        </Button>
      </Box>
      <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-3)', mt: 0.5 }}>
        为每个专家定义职责、角色提示词、绑定模型与可用技能。对话里选为负责人时，由该专家组织结论；
        主 Agent 拆出的子任务会按职责派发给对应专家，各自使用自己的模型和技能白名单。
      </Typography>

      {!desktop && (
        <Box
          sx={{
            mt: 1.5,
            p: 1.5,
            borderRadius: '12px',
            border: '1px solid rgba(217, 119, 6, 0.35)',
            bgcolor: 'rgba(217, 119, 6, 0.08)',
            display: 'flex',
            gap: 1,
          }}
        >
          <WarningAmberIcon sx={{ fontSize: 18, color: '#D97706', mt: 0.1 }} />
          <Box>
            <Typography sx={{ fontSize: '13px', fontWeight: 700, color: '#B45309' }}>
              当前是浏览器直连通道，专家团队不可用
            </Typography>
            <Typography sx={{ fontSize: '12.5px', color: '#92400E', mt: 0.25 }}>
              专家注册表保存在桌面端本机数据库，运行时的模型密钥也只由桌面端解析。
              浏览器通道保留本页作为能力说明，不显示示例专家，也不接受新建或修改。
            </Typography>
          </Box>
        </Box>
      )}

      {desktop && loadError && (
        <Box
          sx={{
            mt: 1.5,
            p: 1.5,
            borderRadius: '12px',
            border: '1px solid rgba(239, 68, 68, 0.3)',
            bgcolor: 'rgba(239, 68, 68, 0.06)',
            display: 'flex',
            gap: 1,
          }}
        >
          <WarningAmberIcon sx={{ fontSize: 18, color: '#DC2626', mt: 0.1 }} />
          <Typography sx={{ fontSize: '12.5px', color: '#B91C1C' }}>{loadError}</Typography>
        </Box>
      )}

      {desktop && !loading && experts.length === 0 && !loadError && (
        <Box sx={{ mt: 2, border: '1px dashed #E5E7EB', borderRadius: '14px', p: 4, textAlign: 'center' }}>
          <GroupsIcon sx={{ fontSize: 36, color: '#DDD6FE' }} />
          <Typography sx={{ fontSize: '14px', fontWeight: 600, color: 'var(--tt-text-2)', mt: 1 }}>
            还没有专家
          </Typography>
          <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-4)', mt: 0.5 }}>
            先建一个，例如「财务分析师」「代码审查员」「合同审阅律师」，再在对话里选择负责人
          </Typography>
          <Button
            size="small"
            variant="outlined"
            startIcon={<AddIcon />}
            onClick={openCreate}
            sx={{ mt: 1.5, textTransform: 'none', borderRadius: '8px' }}
          >
            新建专家
          </Button>
        </Box>
      )}

      {desktop && experts.length > 0 && (
        <Box
          sx={{
            mt: 2,
            display: 'grid',
            gridTemplateColumns: { xs: '1fr', md: 'repeat(2, minmax(0, 1fr))' },
            gap: 1.5,
          }}
        >
          {experts.map((expert) => (
            <Box
              key={expert.id}
              sx={{
                p: 1.75,
                borderRadius: '14px',
                border: expert.modelError ? '1px solid rgba(239, 68, 68, 0.35)' : '1px solid var(--tt-border-soft)',
                bgcolor: 'var(--tt-surface)',
                display: 'flex',
                flexDirection: 'column',
                gap: 0.75,
              }}
            >
              <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 0.75 }}>
                <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                  <Typography sx={{ fontSize: '15px', fontWeight: 700, color: 'var(--tt-text-1)' }}>
                    {expert.name}
                  </Typography>
                  <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)', mt: 0.1 }}>
                    {expert.id}
                  </Typography>
                </Box>
                <Tooltip title="编辑专家">
                  <IconButton size="small" onClick={() => openEdit(expert)} sx={{ color: 'var(--tt-text-3)' }}>
                    <EditOutlinedIcon sx={{ fontSize: 17 }} />
                  </IconButton>
                </Tooltip>
                <Tooltip title="删除专家（只删注册表条目）">
                  <IconButton
                    size="small"
                    onClick={() => setDeleteTarget(expert)}
                    sx={{ color: 'var(--tt-text-3)', '&:hover': { color: '#EF4444' } }}
                  >
                    <DeleteOutlineIcon sx={{ fontSize: 17 }} />
                  </IconButton>
                </Tooltip>
              </Box>

              <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-2)' }}>
                {expert.description}
              </Typography>

              <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, alignItems: 'center' }}>
                <Chip
                  size="small"
                  label={modelLabelOf(expert)}
                  sx={{
                    fontSize: '11px',
                    height: 22,
                    bgcolor: expert.modelError
                      ? 'rgba(239, 68, 68, 0.08)'
                      : expert.modelName
                        ? 'rgba(14, 165, 233, 0.08)'
                        : 'rgba(107, 114, 128, 0.08)',
                    color: expert.modelError ? '#B91C1C' : expert.modelName ? '#0369A1' : '#6B7280',
                  }}
                />
                {expert.skills.length === 0 ? (
                  <Chip
                    size="small"
                    label="技能不限制"
                    sx={{ fontSize: '11px', height: 22, bgcolor: 'rgba(107, 114, 128, 0.08)', color: '#6B7280' }}
                  />
                ) : (
                  expert.skills.map((sid) => (
                    <Chip
                      key={sid}
                      size="small"
                      label={skills.find((s) => s.id === sid)?.name ?? sid}
                      sx={{ fontSize: '11px', height: 22, bgcolor: 'rgba(124, 58, 237, 0.08)', color: '#6D28D9' }}
                    />
                  ))
                )}
              </Box>

              <Box
                sx={{
                  mt: 0.25,
                  p: 1,
                  borderRadius: '8px',
                  bgcolor: 'rgba(124, 58, 237, 0.03)',
                  border: '1px solid rgba(124, 58, 237, 0.12)',
                  maxHeight: 96,
                  overflowY: 'auto',
                }}
              >
                <Typography sx={{ fontSize: '11px', fontWeight: 600, color: '#7C3AED', mb: 0.25 }}>
                  角色提示词
                </Typography>
                <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-3)', whiteSpace: 'pre-wrap' }}>
                  {expert.prompt}
                </Typography>
              </Box>
            </Box>
          ))}
        </Box>
      )}

      {desktop && (
        <Box sx={{ display: 'flex', gap: 0.5, mt: 2 }}>
          <InfoOutlinedIcon sx={{ fontSize: 15, color: 'var(--tt-text-4)', mt: 0.1 }} />
          <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>
            编辑专家不会影响正在运行的任务；绑定模型被删除或缺少密钥时，卡片会标红，提交时会直接报错而不是静默换模型。
          </Typography>
        </Box>
      )}

      {/* 新建 / 编辑专家 */}
      <Dialog
        open={editorOpen}
        onClose={() => !saving && setEditorOpen(false)}
        PaperProps={{ sx: { borderRadius: '14px', minWidth: 560, maxWidth: 720 } }}
      >
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>
          {editing ? `编辑专家「${editing.name}」` : '新建专家'}
        </DialogTitle>
        <DialogContent>
          <TextField
            autoFocus
            fullWidth
            size="small"
            label="名称"
            value={name}
            onChange={(e) => setName(e.target.value.slice(0, NAME_MAX))}
            placeholder="例如：财务分析师"
            helperText={`${name.length}/${NAME_MAX}`}
            sx={{ mt: 0.5, '& .MuiInputBase-root': { fontSize: '13px' } }}
          />
          <TextField
            fullWidth
            multiline
            minRows={2}
            maxRows={4}
            size="small"
            label="职责描述（主 Agent 靠它派发子任务）"
            value={description}
            onChange={(e) => setDescription(e.target.value.slice(0, DESCRIPTION_MAX))}
            placeholder="例如：负责财报口径核对、成本结构拆解与异常波动归因，输出可复核的计算过程"
            helperText={`${description.length}/${DESCRIPTION_MAX}`}
            sx={{ mt: 1.5, '& .MuiInputBase-root': { fontSize: '13px' } }}
          />
          <TextField
            fullWidth
            multiline
            minRows={5}
            maxRows={10}
            size="small"
            label="角色提示词"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value.slice(0, PROMPT_MAX))}
            placeholder="你是财务分析师……先核对口径，再给出计算过程与结论"
            helperText={`${prompt.length}/${PROMPT_MAX}`}
            sx={{ mt: 1.5, '& .MuiInputBase-root': { fontSize: '13px' } }}
          />
          <TextField
            select
            fullWidth
            size="small"
            label="绑定模型"
            value={modelId}
            onChange={(e) => setModelId(e.target.value)}
            helperText="绑定后该专家优先使用此模型；留空则沿用对话中选择的模型"
            sx={{ mt: 1.5, '& .MuiInputBase-root': { fontSize: '13px' } }}
          >
            <MenuItem value="" sx={{ fontSize: '13px' }}>
              不绑定（沿用对话中选择的模型）
            </MenuItem>
            {modelOptions.map((m) => (
              <MenuItem key={m.id} value={m.id} sx={{ fontSize: '13px' }}>
                {m.label}
              </MenuItem>
            ))}
          </TextField>

          <Box sx={{ mt: 1.5 }}>
            <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: 'var(--tt-text-2)' }}>
              技能白名单（{selectedSkills.length}/{SKILLS_MAX}）
            </Typography>
            <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.1 }}>
              不勾选表示不限制；勾选后该专家（含其子任务）只能调用所选技能。
            </Typography>
            <Box
              sx={{
                mt: 0.75,
                maxHeight: 168,
                overflowY: 'auto',
                border: '1px solid var(--tt-border-soft)',
                borderRadius: '10px',
                p: 0.5,
              }}
            >
              {skills.length === 0 ? (
                <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', p: 1 }}>
                  暂无可选技能
                </Typography>
              ) : (
                skills.map((skill) => (
                  <FormControlLabel
                    key={`${skill.source}:${skill.id}`}
                    control={
                      <Checkbox
                        size="small"
                        checked={selectedSkills.includes(skill.id)}
                        onChange={() => toggleSkill(skill.id)}
                      />
                    }
                    label={
                      <Box>
                        <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-1)' }}>
                          {skill.name}
                        </Typography>
                        {skill.description && (
                          <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>
                            {skill.description}
                          </Typography>
                        )}
                      </Box>
                    }
                    sx={{ display: 'flex', alignItems: 'flex-start', mx: 0, my: 0.1, '& .MuiCheckbox-root': { pt: 0.5 } }}
                  />
                ))
              )}
            </Box>
          </Box>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setEditorOpen(false)} disabled={saving} sx={{ color: 'var(--tt-text-3)', fontSize: '13px' }}>
            取消
          </Button>
          <Button
            onClick={() => void submitEditor()}
            disabled={saving || !name.trim() || !description.trim() || !prompt.trim()}
            variant="contained"
            sx={{ fontSize: '13px', bgcolor: '#7C3AED', '&:hover': { bgcolor: '#6D28D9' } }}
          >
            {saving ? '保存中…' : editing ? '保存' : '创建'}
          </Button>
        </DialogActions>
      </Dialog>

      {/* 删除专家 */}
      <Dialog
        open={Boolean(deleteTarget)}
        onClose={() => !deleting && setDeleteTarget(null)}
        PaperProps={{ sx: { borderRadius: '14px', minWidth: 380 } }}
      >
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>删除专家</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ fontSize: '13px', color: 'var(--tt-text-2)' }}>
            删除「{deleteTarget?.name}」只会移除这条专家配置：正在运行和已完成的任务、会话记录、
            产出物都不受影响；之后提交时不能再选择该专家。
          </DialogContentText>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setDeleteTarget(null)} disabled={deleting} sx={{ color: 'var(--tt-text-3)', fontSize: '13px' }}>
            取消
          </Button>
          <Button
            onClick={() => void confirmDelete()}
            disabled={deleting}
            variant="contained"
            sx={{ fontSize: '13px', bgcolor: '#EF4444', '&:hover': { bgcolor: '#DC2626' } }}
          >
            {deleting ? '删除中…' : '确认删除'}
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

export default ExpertsPage;
