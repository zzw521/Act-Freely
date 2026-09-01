/**
 * 通天办公 - 模型设置组件 (高保真原型)
 *
 * 设计稿还原:
 * - 左卡「自定义模型」: 说明 + 本地配置文件路径链接
 * - 右卡「已保存模型」: 添加模型按钮 + 模型列表（编辑/删除）
 * - 「添加模型」弹窗: 提供方 / API Key / 模型名称，黑色保存按钮
 */

import React, { useState } from 'react';
import {
  Box,
  Typography,
  Button,
  IconButton,
  Dialog,
  Select,
  MenuItem,
  TextField,
  InputAdornment,
  Tooltip,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import AddIcon from '@mui/icons-material/Add';
import EditOutlinedIcon from '@mui/icons-material/EditOutlined';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import VisibilityIcon from '@mui/icons-material/Visibility';
import VisibilityOffIcon from '@mui/icons-material/VisibilityOff';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';

// ── 类型与常量 ──────────────────────────────────────────────────

interface SavedModel {
  id: string;
  name: string;
  provider: string;
  apiKey: string;
  modelName: string;
  source: string;
}

const STORAGE_KEY = 'tt-custom-models';

const PROVIDERS = [
  '腾讯云 Token Plan',
  'OpenAI',
  'DeepSeek',
  'Moonshot（Kimi）',
  '智谱 GLM',
  '通用 Token Plan（个人版）',
];

const MODEL_NAMES = ['Auto', 'miMo-v2.5-pro', 'qwen3.6-plus', 'deepseek-chat', 'gpt-5'];

const seedModels: SavedModel[] = [
  {
    id: 'm-1',
    name: 'MiMo-V2.5-Pro',
    provider: '腾讯云 Token Plan',
    apiKey: '',
    modelName: 'Auto',
    source: '自定义',
  },
  {
    id: 'm-2',
    name: 'qwen3.6-plus',
    provider: '通用 Token Plan（个人版）',
    apiKey: '',
    modelName: 'Auto',
    source: '自定义',
  },
];

function loadModels(): SavedModel[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch {
    // ignore
  }
  return seedModels;
}

function saveModels(models: SavedModel[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(models));
  } catch {
    // ignore
  }
}

// ── 通用小组件 ──────────────────────────────────────────────────

/** 下拉选择器（带箭头） */
const SettingSelect: React.FC<{
  value: string;
  options: string[];
  onChange: (v: string) => void;
  minWidth?: number;
}> = ({ value, options, onChange, minWidth = 140 }) => (
  <Select
    value={value}
    onChange={(e) => onChange(e.target.value)}
    size="small"
    IconComponent={KeyboardArrowDownIcon}
    sx={{
      minWidth,
      borderRadius: '10px',
      fontSize: '13px',
      color: '#111827',
      bgcolor: '#FFFFFF',
      '& .MuiOutlinedInput-notchedOutline': { borderColor: '#E5E7EB' },
      '&:hover .MuiOutlinedInput-notchedOutline': { borderColor: '#D1D5DB' },
      '& .MuiSelect-select': { py: 1, pl: 1.5, pr: 4 },
    }}
  >
    {options.map((opt) => (
      <MenuItem key={opt} value={opt} sx={{ fontSize: '13px' }}>
        {opt}
      </MenuItem>
    ))}
  </Select>
);

// ── 添加/编辑模型弹窗 ─────────────────────────────────────────────

interface ModelDialogProps {
  open: boolean;
  editing: SavedModel | null;
  onClose: () => void;
  onSave: (model: SavedModel) => void;
}

const ModelDialog: React.FC<ModelDialogProps> = ({ open, editing, onClose, onSave }) => {
  const [provider, setProvider] = useState(editing?.provider ?? PROVIDERS[0]);
  const [apiKey, setApiKey] = useState(editing?.apiKey ?? '');
  const [modelName, setModelName] = useState(editing?.modelName ?? 'Auto');
  const [showKey, setShowKey] = useState(false);

  // 打开时重置为编辑值
  React.useEffect(() => {
    if (open) {
      setProvider(editing?.provider ?? PROVIDERS[0]);
      setApiKey(editing?.apiKey ?? '');
      setModelName(editing?.modelName ?? 'Auto');
      setShowKey(false);
    }
  }, [open, editing]);

  const handleSave = () => {
    onSave({
      id: editing?.id ?? `m-${Date.now()}`,
      name: editing?.name ?? modelName === 'Auto' ? provider : modelName,
      provider,
      apiKey,
      modelName,
      source: '自定义',
    });
    onClose();
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      maxWidth="sm"
      fullWidth
      PaperProps={{ sx: { borderRadius: '16px', p: 3 } }}
    >
      {/* 标题栏 */}
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', mb: 3 }}>
        <Box>
          <Typography sx={{ fontSize: '17px', fontWeight: 700, color: '#111827' }}>
            {editing ? '编辑模型' : '添加模型'}
          </Typography>
          <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mt: 0.5 }}>
            仅支持 OpenAI 兼容协议 API
          </Typography>
        </Box>
        <IconButton size="small" onClick={onClose} sx={{ color: '#9CA3AF' }}>
          <CloseIcon sx={{ fontSize: 20 }} />
        </IconButton>
      </Box>

      {/* 提供方 */}
      <Box sx={{ mb: 2.5 }}>
        <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 1 }}>
          <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#374151' }}>提供方</Typography>
          <Typography sx={{ fontSize: '12px', color: '#9CA3AF', cursor: 'pointer', '&:hover': { color: '#6B7280' } }}>
            查看文档
          </Typography>
        </Box>
        <Select
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
          fullWidth
          size="small"
          IconComponent={KeyboardArrowDownIcon}
          sx={{
            borderRadius: '10px',
            fontSize: '13px',
            color: '#111827',
            '& .MuiOutlinedInput-notchedOutline': { borderColor: '#E5E7EB' },
            '& .MuiSelect-select': { py: 1.25, pl: 1.5, pr: 4 },
          }}
        >
          {PROVIDERS.map((p) => (
            <MenuItem key={p} value={p} sx={{ fontSize: '13px' }}>
              {p}
            </MenuItem>
          ))}
        </Select>
      </Box>

      {/* API Key */}
      <Box sx={{ mb: 2.5 }}>
        <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#374151', mb: 1 }}>API Key</Typography>
        <TextField
          fullWidth
          size="small"
          type={showKey ? 'text' : 'password'}
          placeholder="输入你的 API Key"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          InputProps={{
            sx: { borderRadius: '10px', fontSize: '13px', py: 0.25 },
            endAdornment: (
              <InputAdornment position="end">
                <IconButton size="small" onClick={() => setShowKey(!showKey)} sx={{ color: '#9CA3AF' }}>
                  {showKey ? <VisibilityOffIcon sx={{ fontSize: 18 }} /> : <VisibilityIcon sx={{ fontSize: 18 }} />}
                </IconButton>
              </InputAdornment>
            ),
          }}
        />
      </Box>

      {/* 模型名称 */}
      <Box sx={{ mb: 3.5 }}>
        <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#374151', mb: 1 }}>模型名称</Typography>
        <Select
          value={modelName}
          onChange={(e) => setModelName(e.target.value)}
          fullWidth
          size="small"
          IconComponent={KeyboardArrowDownIcon}
          sx={{
            borderRadius: '10px',
            fontSize: '13px',
            color: '#111827',
            '& .MuiOutlinedInput-notchedOutline': { borderColor: '#E5E7EB' },
            '& .MuiSelect-select': { py: 1.25, pl: 1.5, pr: 4 },
          }}
        >
          {MODEL_NAMES.map((m) => (
            <MenuItem key={m} value={m} sx={{ fontSize: '13px' }}>
              {m}
            </MenuItem>
          ))}
        </Select>
      </Box>

      {/* 操作按钮 */}
      <Box sx={{ display: 'flex', justifyContent: 'flex-end', gap: 1.5 }}>
        <Button
          onClick={onClose}
          sx={{
            borderRadius: '10px',
            px: 2.5,
            color: '#374151',
            border: '1px solid #E5E7EB',
            fontSize: '13px',
            fontWeight: 600,
            '&:hover': { background: '#F9FAFB', border: '1px solid #D1D5DB' },
          }}
        >
          取消
        </Button>
        <Button
          variant="contained"
          onClick={handleSave}
          sx={{
            borderRadius: '10px',
            px: 2.5,
            bgcolor: '#111827',
            color: '#fff',
            fontSize: '13px',
            fontWeight: 600,
            '&:hover': { bgcolor: '#1F2937' },
          }}
        >
          保存
        </Button>
      </Box>
    </Dialog>
  );
};

// ── ModelSettings 主组件 ──────────────────────────────────────────

const ModelSettings: React.FC = () => {
  const [models, setModels] = useState<SavedModel[]>(loadModels);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<SavedModel | null>(null);

  const updateModels = (next: SavedModel[]) => {
    setModels(next);
    saveModels(next);
  };

  const handleSave = (model: SavedModel) => {
    const exists = models.some((m) => m.id === model.id);
    updateModels(exists ? models.map((m) => (m.id === model.id ? model : m)) : [...models, model]);
  };

  const handleDelete = (id: string) => {
    updateModels(models.filter((m) => m.id !== id));
  };

  return (
    <Box>
      {/* 标题区 */}
      <Typography sx={{ fontSize: '18px', fontWeight: 700, color: '#111827' }}>模型</Typography>
      <Typography sx={{ fontSize: '13px', color: '#9CA3AF', mt: 0.5, mb: 3 }}>
        连接并管理你的模型供应商与自定义模型
      </Typography>

      <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1.2fr', gap: 2 }}>
        {/* ── 自定义模型说明卡 ─────────────────────── */}
        <Box
          sx={{
            bgcolor: '#FFFFFF',
            border: '1px solid #E5E7EB',
            borderRadius: '14px',
            p: 3,
          }}
        >
          <Typography sx={{ fontSize: '15px', fontWeight: 600, color: '#111827', mb: 1.5 }}>
            自定义模型
          </Typography>
          <Typography sx={{ fontSize: '13px', color: '#6B7280', lineHeight: 1.8 }}>
            你可以手动编辑本地配置文件来添加自定义模型
          </Typography>
          <Typography sx={{ fontSize: '13px', mt: 1.5 }}>
            <Box
              component="span"
              sx={{ color: '#0EA5E9', cursor: 'pointer', '&:hover': { textDecoration: 'underline' } }}
            >
              %USERPROFILE%\workbuddy\models.json
            </Box>
          </Typography>
          <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mt: 2, lineHeight: 1.7 }}>
            修改完成后重启应用生效，请注意保持 JSON 格式正确
          </Typography>
        </Box>

        {/* ── 已保存模型卡 ─────────────────────────── */}
        <Box
          sx={{
            bgcolor: '#FFFFFF',
            border: '1px solid #E5E7EB',
            borderRadius: '14px',
            p: 3,
          }}
        >
          <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
            <Typography sx={{ fontSize: '15px', fontWeight: 600, color: '#111827' }}>已保存模型</Typography>
            <Button
              startIcon={<AddIcon sx={{ fontSize: 16 }} />}
              onClick={() => {
                setEditing(null);
                setDialogOpen(true);
              }}
              sx={{
                borderRadius: '10px',
                px: 2,
                color: '#374151',
                border: '1px solid #E5E7EB',
                fontSize: '13px',
                fontWeight: 600,
                '&:hover': { background: '#F9FAFB', border: '1px solid #D1D5DB' },
              }}
            >
              添加模型
            </Button>
          </Box>

          {/* 模型列表 */}
          {models.length === 0 ? (
            <Typography sx={{ fontSize: '13px', color: '#9CA3AF', py: 4, textAlign: 'center' }}>
              暂无已保存模型
            </Typography>
          ) : (
            models.map((m) => (
              <Box
                key={m.id}
                sx={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 1.5,
                  py: 1.5,
                  borderTop: '1px solid #F3F4F6',
                }}
              >
                {/* 圆形 + 图标 */}
                <Box
                  sx={{
                    width: 34,
                    height: 34,
                    borderRadius: '50%',
                    border: '1.5px solid #E5E7EB',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    flexShrink: 0,
                    cursor: 'pointer',
                    transition: 'border-color 0.15s ease',
                    '&:hover': { borderColor: '#0D9488' },
                  }}
                >
                  <AddIcon sx={{ fontSize: 16, color: '#6B7280' }} />
                </Box>

                <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                  <Typography sx={{ fontSize: '14px', fontWeight: 600, color: '#111827' }}>{m.name}</Typography>
                  <Typography sx={{ fontSize: '12px', color: '#9CA3AF' }}>{m.source}</Typography>
                </Box>

                <Tooltip title="编辑">
                  <IconButton
                    size="small"
                    onClick={() => {
                      setEditing(m);
                      setDialogOpen(true);
                    }}
                    sx={{ color: '#9CA3AF', '&:hover': { color: '#374151' } }}
                  >
                    <EditOutlinedIcon sx={{ fontSize: 18 }} />
                  </IconButton>
                </Tooltip>
                <Tooltip title="删除">
                  <IconButton
                    size="small"
                    onClick={() => handleDelete(m.id)}
                    sx={{ color: '#9CA3AF', '&:hover': { color: '#EF4444' } }}
                  >
                    <DeleteOutlineIcon sx={{ fontSize: 18 }} />
                  </IconButton>
                </Tooltip>
              </Box>
            ))
          )}
        </Box>
      </Box>

      {/* 添加/编辑弹窗 */}
      <ModelDialog open={dialogOpen} editing={editing} onClose={() => setDialogOpen(false)} onSave={handleSave} />
    </Box>
  );
};

export default ModelSettings;
