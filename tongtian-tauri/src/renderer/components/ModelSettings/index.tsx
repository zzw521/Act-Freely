/**
 * 通天办公 - 模型设置组件
 *
 * 数据源统一走 `api.model.*`，不再读写 localStorage 的 tt-custom-models：
 * - 桌面端：Rust SQLite + 系统密钥库（读取视图不含明文密钥）
 * - 浏览器直连：引擎 models.json（同一份清单，CRUD 真实落盘）
 * - Mock：页面内存台账（如实说明刷新即重置）
 *
 * 编辑已存模型时 API Key 留空 = 保留原密钥；输入新值才替换。
 * 保存/删除/设为默认后清空对话模型缓存，下一轮对话立即生效。
 */

import React, { useCallback, useEffect, useState } from 'react';
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
  Alert,
  Chip,
  CircularProgress,
  LinearProgress,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import AddIcon from '@mui/icons-material/Add';
import EditOutlinedIcon from '@mui/icons-material/EditOutlined';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import VisibilityIcon from '@mui/icons-material/Visibility';
import VisibilityOffIcon from '@mui/icons-material/VisibilityOff';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import StarOutlineIcon from '@mui/icons-material/StarOutline';
import FolderOpenOutlinedIcon from '@mui/icons-material/FolderOpenOutlined';
import BarChartOutlinedIcon from '@mui/icons-material/BarChartOutlined';
import ArrowUpwardIcon from '@mui/icons-material/ArrowUpward';
import ArrowDownwardIcon from '@mui/icons-material/ArrowDownward';
import { useNavigate } from 'react-router-dom';
import { type ModelConfig, ModelSource, type ModelTestResp } from '../../../shared/types';
import LocalModelDetect from '../LocalModelDetect';
import { api } from '../../lib/api';
import { resolveEngineBaseUrl } from '../../lib/engine-endpoint';
import { engineFetch } from '../../lib/engine-http';
import { errorTextOf } from '../../../shared/error-messages';
import {
  MODEL_SOURCE_LABELS,
  type ModelStoreInfo,
  invalidateChatModels,
  isLocalModelUrl,
  modelSourceLabel,
  modelStoreInfo,
} from '../../lib/chat-models';

// ── 旧版缓存迁移 ────────────────────────────────────────────────

/** 旧版本把模型（含明文 Key）存在这里；迁移成功后删除 */
const LEGACY_STORAGE_KEY = 'tt-custom-models';

/** 避免 React StrictMode 双挂载重复导入 */
let legacyMigrationRan = false;

interface MigrationResult {
  imported: number;
  failed: number;
  skipped: number;
}

/**
 * 把旧版本 localStorage 中的真实条目导入当前数据源。
 * 旧版演示种子没有接口地址（baseUrl 为空）或模型名是占位 Auto，直接跳过；
 * 有任一导入失败则保留旧缓存，避免用户数据凭空消失。
 */
async function migrateLegacyModels(existing: ModelConfig[]): Promise<MigrationResult> {
  const result: MigrationResult = { imported: 0, failed: 0, skipped: 0 };
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(LEGACY_STORAGE_KEY);
  } catch {
    return result;
  }
  if (!raw) return result;

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    // 内容损坏：保留原样，不擅自删除
    result.failed += 1;
    return result;
  }
  if (!Array.isArray(parsed)) {
    result.failed += 1;
    return result;
  }

  const seen = new Set(
    existing.map((m) => `${m.name}|${m.modelName}|${(m.baseUrl ?? '').trim()}`),
  );

  for (const item of parsed) {
    if (!item || typeof item !== 'object') {
      result.skipped += 1;
      continue;
    }
    const rec = item as Record<string, unknown>;
    const name = String(rec.name ?? '').trim();
    const baseUrl = String(rec.baseUrl ?? '').trim();
    const modelName = String(rec.modelName ?? '').trim();
    const apiKey = String(rec.apiKey ?? '').trim();
    // 旧版演示种子：没有真实端点，或服务端模型名仍是占位 Auto
    if (!name || !baseUrl || !modelName || modelName.toLowerCase() === 'auto') {
      result.skipped += 1;
      continue;
    }
    const fingerprint = `${name}|${modelName}|${baseUrl}`;
    if (seen.has(fingerprint)) {
      result.skipped += 1;
      continue;
    }
    try {
      await api.model.create({
        name,
        source: ModelSource.CUSTOM,
        baseUrl,
        modelName,
        contextWindow: 4096,
        isDefault: false,
        agentBinding: '',
        forceLocal: false,
        ...(apiKey ? { apiKey } : {}),
      } as never);
      seen.add(fingerprint);
      result.imported += 1;
    } catch {
      result.failed += 1;
    }
  }

  // 只有全部导入成功才清理旧缓存；有失败时留给用户重试
  if (result.failed === 0) {
    try {
      localStorage.removeItem(LEGACY_STORAGE_KEY);
    } catch {
      // localStorage 不可用：保留也无妨
    }
  }
  return result;
}

function migrationNote(r: MigrationResult): string {
  if (r.failed > 0) {
    return `从旧版本缓存导入 ${r.imported} 个模型，${r.failed} 个失败；旧缓存已保留，可稍后重试或手动添加。`;
  }
  const skipped = r.skipped > 0 ? `，跳过 ${r.skipped} 个无法使用的占位条目` : '';
  return `已从旧版本缓存导入 ${r.imported} 个模型并清理旧缓存${skipped}。`;
}

// ── 通用小组件 ──────────────────────────────────────────────────

const SOURCE_OPTIONS = Object.values(ModelSource).map((value) => ({
  value,
  label: MODEL_SOURCE_LABELS[value] ?? value,
}));

const CURRENCY_SYMBOLS: Record<string, string> = { CNY: '¥', USD: '$', EUR: '€' };

/** 单价摘要：未配置时明确写「未配置」，不显示成 0 元 */
function priceSummary(m: ModelConfig): string | null {
  if (m.priceInputPerM == null || m.priceOutputPerM == null) return null;
  const cur = (m.priceCurrency || 'CNY').toUpperCase();
  const sym = CURRENCY_SYMBOLS[cur] ?? `${cur} `;
  return `输入 ${sym}${m.priceInputPerM}/M · 输出 ${sym}${m.priceOutputPerM}/M`;
}

const SectionLabel: React.FC<{ children: React.ReactNode; required?: boolean }> = ({
  children,
  required,
}) => (
  <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#374151', mb: 1 }}>
    {children}
    {required && <Box component="span" sx={{ color: '#EF4444', ml: 0.5 }}>*</Box>}
  </Typography>
);

// ── 添加/编辑模型弹窗 ─────────────────────────────────────────────

interface ModelDialogProps {
  open: boolean;
  editing: ModelConfig | null;
  models: ModelConfig[];
  onClose: () => void;
  onSaved: () => Promise<void>;
}

const ModelDialog: React.FC<ModelDialogProps> = ({ open, editing, models, onClose, onSaved }) => {
  const isEdit = Boolean(editing?.id);
  const [name, setName] = useState('');
  const [source, setSource] = useState<ModelSource>(ModelSource.OPENAI_COMPAT);
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [modelName, setModelName] = useState('');
  const [contextWindow, setContextWindow] = useState(4096);
  const [priceInput, setPriceInput] = useState('');
  const [priceOutput, setPriceOutput] = useState('');
  const [currency, setCurrency] = useState('CNY');
  const [fallbackIds, setFallbackIds] = useState<string[]>([]);
  const [showKey, setShowKey] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ModelTestResp | null>(null);
  const [error, setError] = useState<string | null>(null);

  // 打开时重置为当前条目（空密钥代表保留原密钥）
  useEffect(() => {
    if (!open) return;
    setName(editing?.name ?? '');
    setSource(editing?.source ?? ModelSource.OPENAI_COMPAT);
    setBaseUrl(editing?.baseUrl ?? '');
    setApiKey('');
    setModelName(editing?.modelName ?? '');
    setContextWindow(editing?.contextWindow || 4096);
    setPriceInput(editing?.priceInputPerM != null ? String(editing.priceInputPerM) : '');
    setPriceOutput(editing?.priceOutputPerM != null ? String(editing.priceOutputPerM) : '');
    setCurrency(editing?.priceCurrency || 'CNY');
    setFallbackIds((editing?.fallbackIds ?? []).slice());
    setShowKey(false);
    setTestResult(null);
    setError(null);
    setSaving(false);
    setTesting(false);
  }, [open, editing]);

  const keyHint = editing?.hasApiKey
    ? '已保存密钥：留空保持不变，输入新值可替换'
    : isLocalModelUrl(baseUrl)
      ? '本地端点可留空；如服务端要求鉴权再填写'
      : '密钥只写入当前数据源，模型清单不会回传明文';

  const fallbackCandidates = models.filter(
    (m) => m.id !== editing?.id && !fallbackIds.includes(m.id),
  );

  const moveFallback = (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= fallbackIds.length) return;
    const next = [...fallbackIds];
    [next[index], next[target]] = [next[target], next[index]];
    setFallbackIds(next);
  };

  const handleTest = async () => {
    const base = baseUrl.trim();
    const model = modelName.trim();
    if (!base || !model) {
      setError('请先填写接口地址与服务端模型名');
      return;
    }
    setTesting(true);
    setError(null);
    setTestResult(null);
    try {
      const result = await api.model.test({
        baseUrl: base,
        apiKey: apiKey.trim(),
        modelName: model,
        contextWindow,
        // 编辑时后端可用 id 补齐已存密钥（留空=沿用）
        id: editing?.id,
      });
      setTestResult(result);
    } catch (e) {
      setError(errorTextOf(e, '连接测试失败'));
    } finally {
      setTesting(false);
    }
  };

  const handleSave = async () => {
    const trimmedName = name.trim();
    const trimmedBase = baseUrl.trim();
    const trimmedModel = modelName.trim();
    if (!trimmedName || !trimmedBase || !trimmedModel) {
      setError('请填写模型名称、接口地址与服务端模型名');
      return;
    }
    if (!/^https?:\/\//i.test(trimmedBase)) {
      setError('接口地址必须以 http:// 或 https:// 开头');
      return;
    }
    const hasInputPrice = priceInput.trim() !== '';
    const hasOutputPrice = priceOutput.trim() !== '';
    if (hasInputPrice !== hasOutputPrice) {
      setError('输入与输出单价必须同时填写，或同时留空表示未配置');
      return;
    }
    if (hasInputPrice) {
      const pin = Number(priceInput);
      const pout = Number(priceOutput);
      if (!Number.isFinite(pin) || !Number.isFinite(pout) || pin < 0 || pout < 0) {
        setError('单价必须是非负数字（0 表示免费）');
        return;
      }
    }
    const normalizedCurrency = currency.trim().toUpperCase() || 'CNY';
    if (!/^[A-Z]{3}$/.test(normalizedCurrency)) {
      setError('币种必须是 3 位字母代码，例如 CNY / USD');
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const payload: Record<string, unknown> = {
        name: trimmedName,
        source,
        baseUrl: trimmedBase,
        modelName: trimmedModel,
        contextWindow: Number(contextWindow) || 4096,
        agentBinding: editing?.agentBinding ?? '',
        forceLocal: editing?.forceLocal ?? false,
        // 两项都空 = 未配置（后端不按 0 元计价）；显式传对象才能清空已存单价
        prices: {
          inputPerM: hasInputPrice ? Number(priceInput) : null,
          outputPerM: hasOutputPrice ? Number(priceOutput) : null,
          currency: normalizedCurrency,
        },
        fallbackIds,
      };
      // 留空 = 不传 apiKey（保留原密钥）；输入新值才替换
      if (apiKey.trim()) payload.apiKey = apiKey.trim();
      if (isEdit && editing) {
        await api.model.update(editing.id, payload as never);
      } else {
        await api.model.create(payload as never);
      }
      invalidateChatModels();
      await onSaved();
      onClose();
    } catch (e) {
      setError(errorTextOf(e, '保存模型失败'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={saving ? undefined : onClose}
      maxWidth="sm"
      fullWidth
      PaperProps={{ sx: { borderRadius: '16px', p: 3, maxHeight: '90vh', overflowY: 'auto' } }}
    >
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', mb: 2.5 }}>
        <Box>
          <Typography sx={{ fontSize: '17px', fontWeight: 700, color: '#111827' }}>
            {isEdit ? '编辑模型' : '添加模型'}
          </Typography>
          <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mt: 0.5 }}>
            仅支持 OpenAI 兼容协议 API，保存后下一轮对话生效
          </Typography>
        </Box>
        <IconButton size="small" onClick={onClose} disabled={saving} sx={{ color: '#9CA3AF' }}>
          <CloseIcon sx={{ fontSize: 20 }} />
        </IconButton>
      </Box>

      {error && (
        <Alert severity="error" onClose={() => setError(null)} sx={{ mb: 2, fontSize: '12px' }}>
          {error}
        </Alert>
      )}

      {testResult && (
        <Alert
          severity={testResult.ok ? 'success' : 'error'}
          onClose={() => setTestResult(null)}
          sx={{ mb: 2, fontSize: '12px' }}
        >
          {testResult.ok
            ? `连接成功，延迟 ${testResult.latencyMs}ms（${testResult.model}）`
            : errorTextOf(testResult.error, '连接失败')}
        </Alert>
      )}

      <Box sx={{ mb: 2 }}>
        <SectionLabel required>模型名称</SectionLabel>
        <TextField
          fullWidth
          size="small"
          placeholder="用于在列表中识别，例如 通义千问 Plus"
          value={name}
          onChange={(e) => setName(e.target.value)}
          InputProps={{ sx: { borderRadius: '10px', fontSize: '13px' } }}
        />
      </Box>

      <Box sx={{ mb: 2 }}>
        <SectionLabel>来源</SectionLabel>
        <Select
          value={source}
          onChange={(e) => setSource(e.target.value as ModelSource)}
          fullWidth
          size="small"
          IconComponent={KeyboardArrowDownIcon}
          sx={{
            borderRadius: '10px',
            fontSize: '13px',
            color: '#111827',
            '& .MuiOutlinedInput-notchedOutline': { borderColor: '#E5E7EB' },
            '& .MuiSelect-select': { py: 1.1, pl: 1.5, pr: 4 },
          }}
        >
          {SOURCE_OPTIONS.map((option) => (
            <MenuItem key={option.value} value={option.value} sx={{ fontSize: '13px' }}>
              {option.label}
            </MenuItem>
          ))}
        </Select>
      </Box>

      <Box sx={{ mb: 2 }}>
        <SectionLabel required>接口地址</SectionLabel>
        <TextField
          fullWidth
          size="small"
          placeholder="https://api.example.com/v1"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          InputProps={{
            sx: { borderRadius: '10px', fontSize: '13px', fontFamily: 'Consolas, monospace' },
          }}
        />
        <Typography sx={{ fontSize: '11px', color: '#9CA3AF', mt: 0.5 }}>
          OpenAI 兼容 baseUrl；本地 Ollama 通常是 http://127.0.0.1:11434/v1
        </Typography>
      </Box>

      <Box sx={{ mb: 2 }}>
        <SectionLabel>API Key</SectionLabel>
        <TextField
          fullWidth
          size="small"
          type={showKey ? 'text' : 'password'}
          placeholder={editing?.hasApiKey ? '已保存（留空保持不变）' : '输入你的 API Key'}
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          InputProps={{
            sx: { borderRadius: '10px', fontSize: '13px' },
            endAdornment: (
              <InputAdornment position="end">
                <IconButton size="small" onClick={() => setShowKey(!showKey)} sx={{ color: '#9CA3AF' }}>
                  {showKey ? <VisibilityOffIcon sx={{ fontSize: 18 }} /> : <VisibilityIcon sx={{ fontSize: 18 }} />}
                </IconButton>
              </InputAdornment>
            ),
          }}
        />
        <Typography sx={{ fontSize: '11px', color: '#9CA3AF', mt: 0.5 }}>{keyHint}</Typography>
      </Box>

      <Box sx={{ display: 'flex', gap: 2, mb: 2.5 }}>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <SectionLabel required>服务端模型名</SectionLabel>
          <TextField
            fullWidth
            size="small"
            placeholder="例如 qwen-plus 或 gpt-4o"
            value={modelName}
            onChange={(e) => setModelName(e.target.value)}
            InputProps={{ sx: { borderRadius: '10px', fontSize: '13px' } }}
          />
        </Box>
        <Box sx={{ width: 150, flexShrink: 0 }}>
          <SectionLabel>上下文窗口</SectionLabel>
          <TextField
            fullWidth
            size="small"
            type="number"
            value={contextWindow}
            onChange={(e) => setContextWindow(parseInt(e.target.value, 10) || 4096)}
            inputProps={{ min: 512, max: 2000000 }}
            InputProps={{ sx: { borderRadius: '10px', fontSize: '13px' } }}
          />
        </Box>
      </Box>

      {/* 计费单价：两项成对填写；留空 = 未配置，不按 0 元统计成本 */}
      <Box sx={{ display: 'flex', gap: 1.5, mb: 0.5 }}>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <SectionLabel>输入单价 / 百万 token</SectionLabel>
          <TextField
            fullWidth
            size="small"
            type="number"
            placeholder="留空 = 未配置"
            value={priceInput}
            onChange={(e) => setPriceInput(e.target.value)}
            inputProps={{ min: 0, step: 'any' }}
            InputProps={{ sx: { borderRadius: '10px', fontSize: '13px' } }}
          />
        </Box>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <SectionLabel>输出单价 / 百万 token</SectionLabel>
          <TextField
            fullWidth
            size="small"
            type="number"
            placeholder="留空 = 未配置"
            value={priceOutput}
            onChange={(e) => setPriceOutput(e.target.value)}
            inputProps={{ min: 0, step: 'any' }}
            InputProps={{ sx: { borderRadius: '10px', fontSize: '13px' } }}
          />
        </Box>
        <Box sx={{ width: 96, flexShrink: 0 }}>
          <SectionLabel>币种</SectionLabel>
          <TextField
            fullWidth
            size="small"
            value={currency}
            onChange={(e) => setCurrency(e.target.value.toUpperCase().slice(0, 3))}
            inputProps={{ maxLength: 3 }}
            InputProps={{ sx: { borderRadius: '10px', fontSize: '13px' } }}
          />
        </Box>
      </Box>
      <Typography sx={{ fontSize: '11px', color: '#9CA3AF', mb: 2 }}>
        单价用于成本台账；留空表示未配置，不会按 0 元估算。0 表示确实免费。
      </Typography>

      {/* 故障转移链：主模型失败且未输出内容时按顺序切换 */}
      <Box sx={{ mb: 2.5 }}>
        <SectionLabel>故障转移链（可选，最多 3 个）</SectionLabel>
        <Typography sx={{ fontSize: '11px', color: '#9CA3AF', mb: 1, lineHeight: 1.6 }}>
          主模型调用失败且尚未输出内容时，按顺序切换到下一个模型；需与主模型使用同一种接入方式（自定义端点）。
        </Typography>
        {fallbackIds.map((id, index) => {
          const target = models.find((m) => m.id === id);
          return (
            <Box
              key={id}
              sx={{
                display: 'flex',
                alignItems: 'center',
                gap: 0.5,
                border: '1px solid #E5E7EB',
                borderRadius: '10px',
                pl: 1.25,
                pr: 0.5,
                py: 0.5,
                mb: 0.75,
              }}
            >
              <Box component="span" sx={{ fontSize: '12px', color: '#9CA3AF', width: 16 }}>
                {index + 1}
              </Box>
              <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                <Typography sx={{ fontSize: '13px', color: '#111827' }} noWrap>
                  {target?.name || (target ? target.modelName : `已失效目标 ${id}`)}
                </Typography>
                {target && (
                  <Typography sx={{ fontSize: '11px', color: '#9CA3AF' }} noWrap>
                    {target.modelName}
                  </Typography>
                )}
              </Box>
              <IconButton
                size="small"
                disabled={index === 0}
                onClick={() => moveFallback(index, -1)}
                sx={{ color: '#9CA3AF' }}
              >
                <ArrowUpwardIcon sx={{ fontSize: 16 }} />
              </IconButton>
              <IconButton
                size="small"
                disabled={index === fallbackIds.length - 1}
                onClick={() => moveFallback(index, 1)}
                sx={{ color: '#9CA3AF' }}
              >
                <ArrowDownwardIcon sx={{ fontSize: 16 }} />
              </IconButton>
              <IconButton
                size="small"
                onClick={() => setFallbackIds(fallbackIds.filter((v) => v !== id))}
                sx={{ color: '#9CA3AF', '&:hover': { color: '#EF4444' } }}
              >
                <DeleteOutlineIcon sx={{ fontSize: 16 }} />
              </IconButton>
            </Box>
          );
        })}
        {fallbackIds.length < 3 && fallbackCandidates.length > 0 && (
          <Select
            value=""
            displayEmpty
            size="small"
            onChange={(e) => setFallbackIds([...fallbackIds, String(e.target.value)])}
            IconComponent={KeyboardArrowDownIcon}
            sx={{
              width: '100%',
              borderRadius: '10px',
              fontSize: '13px',
              color: '#374151',
              '& .MuiOutlinedInput-notchedOutline': { borderColor: '#E5E7EB' },
              '& .MuiSelect-select': { py: 1, pl: 1.5, pr: 4 },
            }}
          >
            <MenuItem value="" disabled sx={{ fontSize: '13px', color: '#9CA3AF' }}>
              添加备用模型…
            </MenuItem>
            {fallbackCandidates.map((m) => (
              <MenuItem key={m.id} value={m.id} sx={{ fontSize: '13px' }}>
                {m.name || m.modelName} · {m.modelName}
              </MenuItem>
            ))}
          </Select>
        )}
        {fallbackIds.length === 0 && fallbackCandidates.length === 0 && (
          <Typography sx={{ fontSize: '11px', color: '#9CA3AF' }}>
            需要先保存至少两个模型，才能配置故障转移。
          </Typography>
        )}
        {fallbackIds.length >= 3 && (
          <Typography sx={{ fontSize: '11px', color: '#9CA3AF' }}>已达上限 3 个。</Typography>
        )}
      </Box>

      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 1.5 }}>
        <Button
          onClick={handleTest}
          disabled={testing || saving}
          startIcon={testing ? <CircularProgress size={14} /> : undefined}
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
          {testing ? '测试中…' : '测试连接'}
        </Button>
        <Box sx={{ display: 'flex', gap: 1 }}>
          <Button
            onClick={onClose}
            disabled={saving}
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
            disabled={saving}
            startIcon={saving ? <CircularProgress size={14} sx={{ color: '#fff' }} /> : undefined}
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
            {saving ? '保存中…' : '保存'}
          </Button>
        </Box>
      </Box>
    </Dialog>
  );
};

// ── ModelSettings 主组件 ──────────────────────────────────────────

const ModelSettings: React.FC = () => {
  const navigate = useNavigate();
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [store, setStore] = useState<ModelStoreInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState('');
  const [folderMsg, setFolderMsg] = useState('');
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<ModelConfig | null>(null);
  const [deleting, setDeleting] = useState<ModelConfig | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [list, info] = await Promise.all([api.model.list(), modelStoreInfo()]);
    setModels(Array.isArray(list) ? list : []);
    setStore(info);
  }, []);

  // 挂载：读权威清单 → 迁移旧缓存（仅一次）→ 再读一次
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [list, info] = await Promise.all([api.model.list(), modelStoreInfo()]);
        let current = Array.isArray(list) ? list : [];
        if (!cancelled) {
          setModels(current);
          setStore(info);
        }
        if (!legacyMigrationRan) {
          legacyMigrationRan = true;
          const result = await migrateLegacyModels(current);
          if (result.imported > 0 || result.failed > 0) {
            if (result.imported > 0) {
              invalidateChatModels();
              current = await api.model.list();
              if (!cancelled) setModels(Array.isArray(current) ? current : []);
            }
            if (!cancelled) setNotice(migrationNote(result));
          }
        }
      } catch (e) {
        if (!cancelled) setError(errorTextOf(e, '加载模型列表失败'));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  /** 打开引擎配置文件所在目录（仅浏览器直连且拿到真实路径时可用） */
  const openConfigFolder = async () => {
    if (!store?.path) return;
    const dir = store.path.replace(/[\\/][^\\/]*$/, '');
    try {
      const baseUrl = await resolveEngineBaseUrl();
      const resp = await engineFetch(`${baseUrl}/api/files/open-folder`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: dir }),
      });
      const json = (await resp.json().catch(() => ({}))) as { ok?: boolean; message?: string };
      if (!resp.ok || json.ok !== true) throw new Error(json.message || '打开失败');
      setFolderMsg('已在文件管理器打开');
    } catch (e) {
      setFolderMsg(errorTextOf(e, '无法打开配置目录'));
    }
    setTimeout(() => setFolderMsg(''), 3000);
  };

  const handleSetDefault = async (model: ModelConfig) => {
    setBusyId(model.id);
    setError(null);
    try {
      await api.model.setDefault(model.id);
      invalidateChatModels();
      await refresh();
    } catch (e) {
      setError(errorTextOf(e, '设置默认模型失败'));
    } finally {
      setBusyId(null);
    }
  };

  const handleDelete = async () => {
    if (!deleting) return;
    setBusyId(deleting.id);
    setError(null);
    try {
      await api.model.remove(deleting.id);
      invalidateChatModels();
      await refresh();
      setDeleting(null);
    } catch (e) {
      setError(errorTextOf(e, '删除模型失败'));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Box>
      <Box sx={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 2, mb: 3 }}>
        <Box>
          <Typography sx={{ fontSize: '18px', fontWeight: 700, color: '#111827' }}>模型</Typography>
          <Typography sx={{ fontSize: '13px', color: '#9CA3AF', mt: 0.5 }}>
            管理对话可用的模型供应商与自定义模型；token 用量与费用按真实台账统计
          </Typography>
        </Box>
        <Button
          startIcon={<BarChartOutlinedIcon sx={{ fontSize: 17 }} />}
          onClick={() => navigate('/model-hub?tab=usage')}
          sx={{
            flexShrink: 0,
            borderRadius: '10px',
            px: 2,
            color: '#374151',
            border: '1px solid #E5E7EB',
            fontSize: '13px',
            fontWeight: 600,
            '&:hover': { background: '#F9FAFB', border: '1px solid #D1D5DB' },
          }}
        >
          用量统计
        </Button>
      </Box>

      {error && (
        <Alert severity="error" onClose={() => setError(null)} sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}
      {notice && (
        <Alert severity="info" onClose={() => setNotice('')} sx={{ mb: 2 }}>
          {notice}
        </Alert>
      )}

      <LocalModelDetect existing={models} onAdded={refresh} />

      <Box sx={{ display: 'grid', gridTemplateColumns: '1fr 1.2fr', gap: 2 }}>
        {/* ── 数据源说明卡 ─────────────────────── */}
        <Box sx={{ bgcolor: '#FFFFFF', border: '1px solid #E5E7EB', borderRadius: '14px', p: 3 }}>
          <Typography sx={{ fontSize: '15px', fontWeight: 600, color: '#111827', mb: 1.5 }}>
            数据源
          </Typography>
          <Typography sx={{ fontSize: '13px', color: '#374151', fontWeight: 600 }}>
            {store?.title ?? '读取中…'}
          </Typography>
          <Typography sx={{ fontSize: '12px', color: '#6B7280', lineHeight: 1.8, mt: 0.75 }}>
            {store?.detail ?? '正在确认当前运行通道的模型存储位置。'}
          </Typography>

          {store?.path && (
            <Box sx={{ mt: 1.5, display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
              <Box
                component="span"
                onClick={() => void openConfigFolder()}
                title="点击打开配置文件所在目录"
                sx={{
                  color: '#0EA5E9',
                  cursor: 'pointer',
                  fontFamily: 'Consolas, monospace',
                  fontSize: '12px',
                  wordBreak: 'break-all',
                  '&:hover': { textDecoration: 'underline' },
                }}
              >
                {store.path}
              </Box>
              <Tooltip title="打开所在目录">
                <IconButton size="small" onClick={() => void openConfigFolder()} sx={{ color: '#6B7280' }}>
                  <FolderOpenOutlinedIcon sx={{ fontSize: 17 }} />
                </IconButton>
              </Tooltip>
              {folderMsg && <Box component="span" sx={{ fontSize: '11px', color: '#0D9488' }}>{folderMsg}</Box>}
            </Box>
          )}

          <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mt: 2, lineHeight: 1.7 }}>
            在这里添加或修改后，下一轮对话立即生效，无需重启应用。
          </Typography>
        </Box>

        {/* ── 已保存模型卡 ─────────────────────────── */}
        <Box sx={{ bgcolor: '#FFFFFF', border: '1px solid #E5E7EB', borderRadius: '14px', p: 3 }}>
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

          {loading ? (
            <LinearProgress sx={{ borderRadius: 1, mt: 1 }} />
          ) : models.length === 0 ? (
            <Typography sx={{ fontSize: '13px', color: '#9CA3AF', py: 4, textAlign: 'center' }}>
              暂无已保存模型
            </Typography>
          ) : (
            models.map((m) => {
              const local = isLocalModelUrl(m.baseUrl);
              const keyState = m.hasApiKey ? '密钥已配置' : local ? '本地模型' : '未配置密钥';
              const price = priceSummary(m);
              const fallbackNames = (m.fallbackIds ?? [])
                .map((id) => models.find((x) => x.id === id)?.name || id)
                .filter(Boolean);
              return (
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
                  <Box
                    sx={{
                      width: 34,
                      height: 34,
                      borderRadius: '50%',
                      bgcolor: '#E0F2FE',
                      color: '#0369A1',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      flexShrink: 0,
                      fontSize: '14px',
                      fontWeight: 700,
                    }}
                  >
                    {(m.name || m.modelName || '?').slice(0, 1).toUpperCase()}
                  </Box>

                  <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                    <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}>
                      <Typography sx={{ fontSize: '14px', fontWeight: 600, color: '#111827' }}>
                        {m.name || m.modelName}
                      </Typography>
                      {m.isDefault && (
                        <Chip
                          label="默认"
                          size="small"
                          sx={{ height: 18, fontSize: '10px', bgcolor: '#DCFCE7', color: '#166534' }}
                        />
                      )}
                    </Box>
                    <Typography sx={{ fontSize: '12px', color: '#9CA3AF' }}>
                      {modelSourceLabel(m.source)} · {m.modelName} · {keyState}
                    </Typography>
                    {m.baseUrl && (
                      <Typography
                        sx={{
                          fontSize: '11px',
                          color: '#B0B7C3',
                          fontFamily: 'Consolas, monospace',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }}
                        title={m.baseUrl}
                      >
                        {m.baseUrl}
                      </Typography>
                    )}
                    {(price || fallbackNames.length > 0) && (
                      <Box sx={{ display: 'flex', gap: 0.75, flexWrap: 'wrap', mt: 0.4 }}>
                        {price && (
                          <Chip
                            label={price}
                            size="small"
                            sx={{ height: 18, fontSize: '10px', bgcolor: '#F3F4F6', color: '#4B5563' }}
                          />
                        )}
                        {fallbackNames.length > 0 && (
                          <Chip
                            label={`容灾：${fallbackNames.join(' → ')}`}
                            size="small"
                            sx={{ height: 18, fontSize: '10px', bgcolor: '#EFF6FF', color: '#1D4ED8' }}
                          />
                        )}
                      </Box>
                    )}
                  </Box>

                  {busyId === m.id ? (
                    <CircularProgress size={18} sx={{ color: '#9CA3AF', mx: 1 }} />
                  ) : (
                    <>
                      {!m.isDefault && (
                        <Tooltip title="设为默认模型（输入框选 Auto 时使用）">
                          <IconButton
                            size="small"
                            onClick={() => void handleSetDefault(m)}
                            sx={{ color: '#9CA3AF', '&:hover': { color: '#D97706' } }}
                          >
                            <StarOutlineIcon sx={{ fontSize: 18 }} />
                          </IconButton>
                        </Tooltip>
                      )}
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
                          onClick={() => setDeleting(m)}
                          sx={{ color: '#9CA3AF', '&:hover': { color: '#EF4444' } }}
                        >
                          <DeleteOutlineIcon sx={{ fontSize: 18 }} />
                        </IconButton>
                      </Tooltip>
                    </>
                  )}
                </Box>
              );
            })
          )}
        </Box>
      </Box>

      <ModelDialog
        open={dialogOpen}
        editing={editing}
        models={models}
        onClose={() => setDialogOpen(false)}
        onSaved={refresh}
      />

      {/* 删除确认：明确说明会连同密钥一起移除 */}
      <Dialog
        open={Boolean(deleting)}
        onClose={() => (busyId ? undefined : setDeleting(null))}
        maxWidth="xs"
        fullWidth
        PaperProps={{ sx: { borderRadius: '14px', p: 3 } }}
      >
        <Typography sx={{ fontSize: '16px', fontWeight: 700, color: '#111827', mb: 1 }}>
          删除模型
        </Typography>
        <Typography sx={{ fontSize: '13px', color: '#6B7280', lineHeight: 1.7 }}>
          确认删除「{deleting?.name || deleting?.modelName}」？该条目及其在本机保存的密钥会一并移除，其他模型转移链里对它的引用也会自动摘除。
        </Typography>
        <Box sx={{ display: 'flex', justifyContent: 'flex-end', gap: 1.5, mt: 3 }}>
          <Button
            onClick={() => setDeleting(null)}
            disabled={Boolean(busyId)}
            sx={{
              borderRadius: '10px',
              px: 2.5,
              color: '#374151',
              border: '1px solid #E5E7EB',
              fontSize: '13px',
              fontWeight: 600,
            }}
          >
            取消
          </Button>
          <Button
            variant="contained"
            color="error"
            onClick={() => void handleDelete()}
            disabled={Boolean(busyId)}
            sx={{ borderRadius: '10px', px: 2.5, fontSize: '13px', fontWeight: 600 }}
          >
            {busyId ? '删除中…' : '删除'}
          </Button>
        </Box>
      </Dialog>
    </Box>
  );
};

export default ModelSettings;
