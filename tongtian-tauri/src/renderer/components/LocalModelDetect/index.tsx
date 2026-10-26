/**
 * 零配置本地模型：检测本机 Ollama 并一键加入模型清单。
 *
 * 检测结果如实显示三种状态：
 * - supported=false：当前通道没有本机探测能力（浏览器/Mock），不假装探测过
 * - available=false：真实探测过，但服务没起来或响应不合法，给出原因与处理建议
 * - available=true：列出已下载模型，重复项标记「已添加」，不重复写入
 */

import React, { useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Typography,
} from '@mui/material';
import SearchIcon from '@mui/icons-material/Search';
import AddIcon from '@mui/icons-material/Add';
import CheckCircleOutlineIcon from '@mui/icons-material/CheckCircleOutline';
import { type LocalDetectResp, type LocalModelInfo, ModelSource } from '../../../shared/types';
import { errorTextOf } from '../../../shared/error-messages';
import { api } from '../../lib/api';

export interface LocalModelExisting {
  baseUrl: string;
  modelName: string;
}

interface LocalModelDetectProps {
  existing: LocalModelExisting[];
  onAdded: () => Promise<void> | void;
}

function formatSize(bytes: number): string {
  if (!bytes) return '大小未知';
  const gb = bytes / 1024 / 1024 / 1024;
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  return `${Math.max(1, Math.round(bytes / 1024 / 1024))} MB`;
}

function isAdded(existing: LocalModelExisting[], baseUrl: string, modelName: string): boolean {
  const base = baseUrl.trim().replace(/\/+$/, '');
  return existing.some(
    (m) => m.baseUrl.trim().replace(/\/+$/, '') === base && m.modelName.trim() === modelName,
  );
}

const LocalModelDetect: React.FC<LocalModelDetectProps> = ({ existing, onAdded }) => {
  const [detecting, setDetecting] = useState(false);
  const [result, setResult] = useState<LocalDetectResp | null>(null);
  const [adding, setAdding] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState('');

  const handleDetect = async () => {
    setDetecting(true);
    setError(null);
    setNotice('');
    setResult(null);
    try {
      setResult(await api.model.detectLocal());
    } catch (e) {
      setError(errorTextOf(e, '检测本机 Ollama 失败'));
    } finally {
      setDetecting(false);
    }
  };

  const handleAdd = async (model: LocalModelInfo) => {
    if (!result) return;
    const becomesDefault = existing.length === 0;
    setAdding(model.name);
    setError(null);
    setNotice('');
    try {
      await api.model.create({
        name: `Ollama · ${model.name}`,
        source: ModelSource.OLLAMA,
        baseUrl: result.openaiBaseUrl,
        modelName: model.name,
        contextWindow: 4096,
        isDefault: becomesDefault,
        agentBinding: '',
        forceLocal: true,
      } as never);
      await onAdded();
      setNotice(
        becomesDefault
          ? `已添加「${model.name}」并设为默认模型`
          : `已添加「${model.name}」`,
      );
    } catch (e) {
      setError(errorTextOf(e, '添加本地模型失败'));
    } finally {
      setAdding(null);
    }
  };

  const busy = detecting || adding !== null;

  return (
    <Box sx={{ bgcolor: '#FFFFFF', border: '1px solid #E5E7EB', borderRadius: '14px', p: 3, mb: 2 }}>
      <Box sx={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 2 }}>
        <Box sx={{ minWidth: 0 }}>
          <Typography sx={{ fontSize: '15px', fontWeight: 600, color: '#111827' }}>
            本地模型（Ollama）
          </Typography>
          <Typography sx={{ fontSize: '12px', color: '#6B7280', mt: 0.5, lineHeight: 1.7 }}>
            自动检测本机已下载的模型。检测只访问本机回环地址，不会出网；连不上会如实说明，不会假装检测成功。
          </Typography>
        </Box>
        <Button
          onClick={() => void handleDetect()}
          disabled={busy}
          startIcon={
            detecting ? <CircularProgress size={14} /> : <SearchIcon sx={{ fontSize: 17 }} />
          }
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
          {detecting ? '检测中…' : '检测本机 Ollama'}
        </Button>
      </Box>

      {error && (
        <Alert severity="error" onClose={() => setError(null)} sx={{ mt: 2, fontSize: '12px' }}>
          {error}
        </Alert>
      )}
      {notice && (
        <Alert severity="success" onClose={() => setNotice('')} sx={{ mt: 2, fontSize: '12px' }}>
          {notice}
        </Alert>
      )}

      {result && !result.supported && (
        <Alert severity="info" sx={{ mt: 2, fontSize: '12px' }}>
          {errorTextOf(result.error, '当前运行通道无法探测本机 Ollama')}
          。仍可在「添加模型」中手动填写接口地址与模型名。
        </Alert>
      )}

      {result && result.supported && !result.available && (
        <Alert severity="warning" sx={{ mt: 2, fontSize: '12px' }}>
          {errorTextOf(result.error, '未检测到本机 Ollama 服务')}
          。请确认 Ollama 已安装并正在运行（默认 {result.endpoint}），然后重新检测。
        </Alert>
      )}

      {result && result.available && (
        <Box sx={{ mt: 2 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
            <CheckCircleOutlineIcon sx={{ fontSize: 16, color: '#16A34A' }} />
            <Typography sx={{ fontSize: '12px', color: '#166534' }}>
              已连接 {result.endpoint}（{result.latencyMs}ms）
            </Typography>
            <Chip
              label={`${result.models.length} 个模型`}
              size="small"
              sx={{ height: 18, fontSize: '10px', bgcolor: '#DCFCE7', color: '#166534' }}
            />
          </Box>

          {result.models.length === 0 ? (
            <Alert severity="info" sx={{ mt: 1.5, fontSize: '12px' }}>
              Ollama 已运行，但还没有下载模型。先在终端执行 `ollama pull qwen2.5:7b`，再重新检测。
            </Alert>
          ) : (
            <Box sx={{ mt: 1 }}>
              {result.models.map((m) => {
                const added = isAdded(existing, result.openaiBaseUrl, m.name);
                return (
                  <Box
                    key={m.name}
                    sx={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 1.5,
                      py: 1.2,
                      borderTop: '1px solid #F3F4F6',
                    }}
                  >
                    <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                      <Typography
                        sx={{
                          fontSize: '13.5px',
                          fontWeight: 600,
                          color: '#111827',
                          fontFamily: 'Consolas, monospace',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }}
                        title={m.name}
                      >
                        {m.name}
                      </Typography>
                      <Typography sx={{ fontSize: '11px', color: '#9CA3AF' }}>
                        {formatSize(m.sizeBytes)} · 上下文窗口默认 4096，可在模型编辑中调整
                      </Typography>
                    </Box>
                    {added ? (
                      <Chip
                        label="已添加"
                        size="small"
                        sx={{ height: 22, fontSize: '11px', bgcolor: '#F3F4F6', color: '#6B7280' }}
                      />
                    ) : (
                      <Button
                        size="small"
                        onClick={() => void handleAdd(m)}
                        disabled={busy}
                        startIcon={
                          adding === m.name ? (
                            <CircularProgress size={12} />
                          ) : (
                            <AddIcon sx={{ fontSize: 15 }} />
                          )
                        }
                        sx={{ borderRadius: '8px', fontSize: '12px', fontWeight: 600, flexShrink: 0 }}
                      >
                        {adding === m.name ? '添加中…' : '添加'}
                      </Button>
                    )}
                  </Box>
                );
              })}
            </Box>
          )}
        </Box>
      )}
    </Box>
  );
};

export default LocalModelDetect;
