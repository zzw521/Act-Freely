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
import { ENGINE_DIRECT_URL } from '../../lib/engine-api';

// ── 类型与常量 ──────────────────────────────────────────────────

interface SavedModel {
  id: string;
  name: string;
  provider: string;
  /** 自定义接口地址（OpenAI 兼容 baseUrl） */
  baseUrl?: string;
  apiKey: string;
  modelName: string;
  source: string;
}

const STORAGE_KEY = 'tt-custom-models';

const PROVIDERS = [
  '自定义 / Custom',
  '腾讯云 Token Plan',
  'OpenAI',
  'DeepSeek',
  'Moonshot（Kimi）',
  '智谱 GLM',
  '通用 Token Plan（个人版）',
];

/** 各提供方默认接口地址（自定义时作为占位提示，OpenAI 兼容 baseUrl） */
const PROVIDER_URLS: Record<string, string> = {
  'OpenAI': 'https://api.openai.com/v1',
  'DeepSeek': 'https://api.deepseek.com/v1',
  'Moonshot（Kimi）': 'https://api.moonshot.cn/v1',
  '智谱 GLM': 'https://open.bigmodel.cn/api/paas/v4',
};

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

/** 自定义模型写入真实配置文件（引擎 ~/.tongtianwork/models.json，best-effort） */
function persistModelsToDisk(models: SavedModel[]) {
  fetch(`${ENGINE_DIRECT_URL}/api/models/config`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ models }),
  }).catch(() => undefined);
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
  const [baseUrl, setBaseUrl] = useState(editing?.baseUrl ?? '');
  const [apiKey, setApiKey] = useState(editing?.apiKey ?? '');
  const [modelName, setModelName] = useState(editing?.modelName ?? '');
  const [showKey, setShowKey] = useState(false);

  // 打开时重置为编辑值
  React.useEffect(() => {
    if (open) {
      setProvider(editing?.provider ?? PROVIDERS[0]);
      setBaseUrl(editing?.baseUrl ?? '');
      setApiKey(editing?.apiKey ?? '');
      setModelName(editing?.modelName ?? '');
      setShowKey(false);
    }
  }, [open, editing]);

  const isCustom = provider === '自定义 / Custom';
  const urlPlaceholder = PROVIDER_URLS[provider] ?? 'https://api.example.com/v1';

  const handleSave = () => {
    onSave({
      id: editing?.id ?? `m-${Date.now()}`,
      name: editing?.name ?? (modelName.trim() || provider),
      provider,
      baseUrl: baseUrl.trim(),
      apiKey,
      modelName: modelName.trim() || 'Auto',
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

      {/* 接口地址：自定义模型的 API 接入地址（OpenAI 兼容 baseUrl） */}
      <Box sx={{ mb: 2.5 }}>
        <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#374151', mb: 1 }}>
          接口地址{isCustom && <Box component="span" sx={{ color: '#EF4444', ml: 0.5 }}>*</Box>}
        </Typography>
        <TextField
          fullWidth
          size="small"
          placeholder={urlPlaceholder}
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          InputProps={{ sx: { borderRadius: '10px', fontSize: '13px', py: 0.25, fontFamily: 'Consolas, monospace' } }}
        />
        <Typography sx={{ fontSize: '11px', color: '#9CA3AF', mt: 0.5 }}>
          {isCustom ? '填写 OpenAI 兼容协议的 API 地址，例如 https://api.example.com/v1' : '不填则使用提供方默认地址'}
        </Typography>
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

      {/* 模型名称：自由输入，支持自定义模型参数值 */}
      <Box sx={{ mb: 3.5 }}>
        <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#374151', mb: 1 }}>模型名称</Typography>
        <TextField
          fullWidth
          size="small"
          placeholder="输入模型参数值，例如 gpt-4o 或 openai/gpt-4o"
          value={modelName}
          onChange={(e) => setModelName(e.target.value)}
          InputProps={{ sx: { borderRadius: '10px', fontSize: '13px', py: 0.25 } }}
        />
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
  // 真实配置文件路径（引擎返回，默认展示规范路径）
  const [configPath, setConfigPath] = useState('%USERPROFILE%\\.tongtianwork\\models.json');
  const [pathMsg, setPathMsg] = useState('');

  // 挂载时从真实配置文件加载（磁盘为权威数据源；Mock 模式无引擎时回退本地）
  React.useEffect(() => {
    let cancelled = false;
    fetch(`${ENGINE_DIRECT_URL}/api/models/config`)
      .then((r) => (r.ok ? r.json() : null))
      .then((json) => {
        if (cancelled || !json || json.ok !== true) return;
        if (json.path) setConfigPath(json.path);
        if (Array.isArray(json.models) && json.models.length > 0) {
          setModels(json.models as SavedModel[]);
          saveModels(json.models as SavedModel[]);
        } else {
          // 磁盘为空：把本地已有模型同步落盘，保证配置文件内容与实际一致
          persistModelsToDisk(loadModels());
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  /** 打开配置文件所在目录 */
  const openConfigFolder = () => {
    const dir = configPath.replace(/[\\/][^\\/]*$/, '');
    fetch(`${ENGINE_DIRECT_URL}/api/files/open-folder`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: dir }),
    })
      .then((r) => r.json())
      .then((json) => {
        if (!json || json.ok !== true) throw new Error((json && json.message) || '打开失败');
        setPathMsg('已在文件管理器打开');
        setTimeout(() => setPathMsg(''), 2500);
      })
      .catch((e) => {
        setPathMsg(e instanceof Error ? e.message : '无可用引擎，无法打开');
        setTimeout(() => setPathMsg(''), 3000);
      });
  };

  const updateModels = (next: SavedModel[]) => {
    setModels(next);
    saveModels(next);
    persistModelsToDisk(next);
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
          <Typography sx={{ fontSize: '13px', mt: 1.5, display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
            <Box
              component="span"
              onClick={openConfigFolder}
              title="点击打开配置文件所在目录"
              sx={{ color: '#0EA5E9', cursor: 'pointer', fontFamily: 'Consolas, monospace', fontSize: '12px', wordBreak: 'break-all', '&:hover': { textDecoration: 'underline' } }}
            >
              {configPath}
            </Box>
            {pathMsg && <Box component="span" sx={{ fontSize: '11px', color: '#0D9488' }}>{pathMsg}</Box>}
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
