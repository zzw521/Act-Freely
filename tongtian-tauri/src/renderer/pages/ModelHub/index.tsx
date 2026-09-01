/**
 * 通天办公 - 模型中心页面
 * 
 * 功能:
 * - 模型列表展示与搜索
 * - 模型配置管理 (增删改查)
 * - 模型连通性测试
 * - 本地/云端模型切换
 */

import React, { useState, useEffect } from 'react';
import {
  Box,
  Typography,
  Paper,
  Grid,
  Card,
  CardContent,
  CardActions,
  Button,
  IconButton,
  Chip,
  TextField,
  InputAdornment,
  Dialog,
  Alert,
  LinearProgress,
  Tooltip,
  useTheme,
  Tabs,
  Tab,
} from '@mui/material';
import {
  Add as AddIcon,
  Search as SearchIcon,
  Cloud as CloudIcon,
  Computer as ComputerIcon,
  CheckCircle as CheckCircleIcon,
  Error as ErrorIcon,
  Refresh as RefreshIcon,
  Edit as EditIcon,
  Delete as DeleteIcon,
} from '@mui/icons-material';
import ModelForm from '../../components/ModelForm';
import ModelStatsPanel from '../../components/ModelStatsPanel';
import { useModelStore } from '../../stores';
import { useModelUsageStore } from '../../stores/useModelUsageStore';
import type { ModelConfig as StoreModelConfig } from '../../stores/useModelStore';
import { ModelConfig, ModelSource, ModelTestReq, ModelTestResp } from '../../../shared/types';
import { errorMessageOf } from '../../../shared/error-messages';
import api from '../../lib/api';
import { useShallow } from 'zustand/react/shallow';

/** shared ModelConfig → store 展示模型映射 */
function toStoreModel(m: ModelConfig): StoreModelConfig {
  return {
    id: m.id,
    name: m.name,
    provider: m.source === ModelSource.OLLAMA ? 'local' : 'cloud',
    modelIdentifier: m.modelName,
    endpoint: m.baseUrl,
    maxContextLength: m.contextWindow,
    supportsToolCalls: true,
    supportsVision: false,
  };
}

const ModelHub: React.FC = () => {
  const theme = useTheme();
  const [searchQuery, setSearchQuery] = useState('');
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingModel, setEditingModel] = useState<Partial<ModelConfig> | undefined>();
  const [testing, setTesting] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<0 | 1>(0); // 0=模型列表, 1=用量统计

  // 从 store 获取状态
  const { models, status, error, setModels, removeModel, setError } = useModelStore(
    useShallow((state) => ({
      models: state.models,
      status: state.status,
      error: state.error,
      setModels: state.setModels,
      removeModel: state.removeModel,
      setError: state.setError,
    }))
  );

  // 原始 shared ModelConfig 缓存（编辑回填用）
  const [rawModels, setRawModels] = useState<ModelConfig[]>([]);

  // 用量统计 store
  const { stats, period, setPeriod, loadUsageStats } = useModelUsageStore(
    useShallow((state) => ({
      stats: state.stats,
      period: state.period,
      setPeriod: state.setPeriod,
      loadUsageStats: state.loadUsageStats,
    }))
  );

  // 加载模型列表
  useEffect(() => {
    loadModels();
    loadUsageStats();
  }, []);

  const loadModels = async () => {
    try {
      // ModelHub 五件套：model_list 真实接入（SQLite 持久化）
      const list = await api.model.list();
      setRawModels(list);
      setModels(list.map(toStoreModel));
    } catch (err) {
      setError(`加载模型列表失败: ${err}`);
    }
  };

  // 过滤模型
  const filteredModels = models.filter(
    (m) =>
      m.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      m.modelIdentifier.toLowerCase().includes(searchQuery.toLowerCase())
  );

  // 打开新增对话框
  const handleAdd = () => {
    setEditingModel(undefined);
    setDialogOpen(true);
  };

  // 打开编辑对话框（回填 shared ModelConfig）
  const handleEdit = (model: StoreModelConfig) => {
    const raw = rawModels.find((m) => m.id === model.id);
    setEditingModel(raw ?? { id: model.id, name: model.name });
    setDialogOpen(true);
  };

  // 提交表单（新增/更新真实接入；API Key 仅存 keyring 不落库）
  const handleSubmit = async (data: Partial<ModelConfig> & { apiKey?: string }) => {
    try {
      const apiKey = data.apiKey === '●●●●●●●●' ? undefined : data.apiKey;
      if (editingModel?.id) {
        await api.model.update(editingModel.id, {
          name: data.name,
          source: data.source,
          baseUrl: data.baseUrl,
          apiKey,
          modelName: data.modelName,
          contextWindow: data.contextWindow,
          agentBinding: data.agentBinding,
        } as never);
      } else {
        await api.model.create({
          name: data.name ?? '',
          source: data.source ?? ModelSource.OPENAI_COMPAT,
          baseUrl: data.baseUrl ?? '',
          apiKey,
          modelName: data.modelName ?? '',
          contextWindow: data.contextWindow ?? 4096,
          isDefault: data.isDefault ?? false,
          agentBinding: data.agentBinding ?? '',
          forceLocal: false,
        } as never);
      }
      setDialogOpen(false);
      await loadModels();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(errorMessageOf(msg.split(':')[0]) + (msg.includes(':') ? `：${msg.split(':').slice(1).join(':')}` : ''));
    }
  };

  // 测试连通性（已存模型：后端从 keyring 补齐密钥）
  const handleTest = async (modelId: string) => {
    setTesting(modelId);
    try {
      const result = await api.model.test({
        baseUrl: '',
        apiKey: '',
        modelName: '',
        contextWindow: 0,
        id: modelId,
      });
      if (!result.ok) {
        setError(errorMessageOf((result.error ?? '').split(':')[0]) + (result.error ? `：${result.error.split(':').slice(1).join(':')}` : ''));
      } else {
        setError(null);
      }
    } catch (err) {
      setError(`测试失败: ${err}`);
    } finally {
      setTesting(null);
    }
  };

  // 删除模型（连同 keyring 密钥）
  const handleDelete = async (id: string) => {
    if (!confirm('确认删除该模型?')) return;
    try {
      await api.model.remove(id);
      removeModel(id);
      setRawModels((prev) => prev.filter((m) => m.id !== id));
    } catch (err) {
      setError(`删除模型失败: ${err}`);
    }
  };

  return (
    <Box>
      {/* 标题栏 */}
      <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 3 }}>
        <Typography variant="h5" sx={{ fontWeight: 700 }}>
          模型中心
        </Typography>
        <Button
          variant="contained"
          startIcon={<AddIcon />}
          onClick={handleAdd}
          sx={{ borderRadius: '8px' }}
          disabled={activeTab !== 0}
        >
          新增模型
        </Button>
      </Box>

      {/* 标签页切换 */}
      <Paper sx={{ mb: 3, overflow: 'hidden' }}>
        <Tabs
          value={activeTab}
          onChange={(_, newValue) => setActiveTab(newValue)}
          sx={{
            '& .MuiTab-root': {
              textTransform: 'none',
              fontWeight: 600,
            },
          }}
        >
          <Tab label="模型列表" />
          <Tab label="用量统计" />
        </Tabs>
      </Paper>

      {/* 错误提示 */}
      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* 搜索框 */}
      {activeTab === 0 && (
        <Paper sx={{ mb: 3, p: 2, borderRadius: '12px' }}>
          <TextField
            fullWidth
            placeholder="搜索模型名称或标识符..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            InputProps={{
              startAdornment: (
                <InputAdornment position="start">
                  <SearchIcon />
                </InputAdornment>
              ),
            }}
            sx={{ '& .MuiOutlinedInput-root': { borderRadius: '8px' } }}
          />
        </Paper>
      )}

      {/* 用量统计周期选择器 */}
      {activeTab === 1 && (
        <Paper sx={{ mb: 3, p: 2, borderRadius: '12px' }}>
          <Box sx={{ display: 'flex', gap: 2, alignItems: 'center' }}>
            <Typography variant="body2" color="text.secondary">
              统计周期:
            </Typography>
            <Chip
              label="今日"
              clickable
              color={period === 'daily' ? 'primary' : 'default'}
              onClick={() => setPeriod('daily')}
            />
            <Chip
              label="本月"
              clickable
              color={period === 'monthly' ? 'primary' : 'default'}
              onClick={() => setPeriod('monthly')}
            />
            <Chip
              label="总计"
              clickable
              color={period === 'total' ? 'primary' : 'default'}
              onClick={() => setPeriod('total')}
            />
          </Box>
        </Paper>
      )}

      {/* 模型列表 / 用量统计 */}
      <Grid container spacing={2}>
        {activeTab === 0 ? (
          /* 模型列表 */
          models.length === 0 ? (
            <Grid item xs={12}>
              <Paper sx={{ p: 6, textAlign: 'center', borderRadius: '16px' }}>
                <CloudIcon sx={{ fontSize: 64, color: theme.palette.text.secondary, mb: 2 }} />
                <Typography variant="h6" color="text.secondary">
                  暂无模型
                </Typography>
                <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                  点击右上角"新增模型"添加第一个模型
                </Typography>
              </Paper>
            </Grid>
          ) : (
            filteredModels.map((model) => (
              <Grid item xs={12} md={6} lg={4} key={model.id}>
                <Card
                  sx={{
                    height: '100%',
                    display: 'flex',
                    flexDirection: 'column',
                    borderRadius: '12px',
                    transition: 'all 0.2s',
                    '&:hover': {
                      boxShadow: theme.shadows[4],
                      transform: 'translateY(-2px)',
                    },
                  }}
                >
                  <CardContent sx={{ flex: 1 }}>
                    <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', mb: 2 }}>
                      <Box>
                        <Typography variant="h6" sx={{ fontWeight: 600, mb: 1 }}>
                          {model.name}
                        </Typography>
                        <Chip
                          icon={model.provider === 'local' ? <ComputerIcon /> : <CloudIcon />}
                          label={model.provider === 'local' ? '本地模型' : '云端模型'}
                          color={model.provider === 'local' ? 'primary' : 'secondary'}
                          size="small"
                        />
                      </Box>
                      {status === 'connected' && (
                        <Tooltip title="已连接">
                          <CheckCircleIcon color="success" />
                        </Tooltip>
                      )}
                    </Box>

                    <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
                      模型标识: {model.modelIdentifier}
                    </Typography>

                    {model.endpoint && (
                      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                        端点: {model.endpoint}
                      </Typography>
                    )}

                    <Box sx={{ display: 'flex', gap: 1, mt: 2 }}>
                      {model.supportsToolCalls && (
                        <Chip label="工具调用" size="small" variant="outlined" />
                      )}
                      {model.supportsVision && (
                        <Chip label="视觉" size="small" variant="outlined" />
                      )}
                    </Box>
                  </CardContent>

                  <CardActions sx={{ p: 2, pt: 0 }}>
                    <Button
                      size="small"
                      startIcon={testing === model.id ? <RefreshIcon className="animate-spin" /> : <CheckCircleIcon />}
                      onClick={() => handleTest(model.id)}
                      disabled={testing !== null}
                    >
                      {testing === model.id ? '测试中...' : '测试'}
                    </Button>
                    <Box sx={{ flex: 1 }} />
                    <Tooltip title="编辑">
                      <IconButton size="small" onClick={() => handleEdit(model)}>
                        <EditIcon />
                      </IconButton>
                    </Tooltip>
                    <Tooltip title="删除">
                      <IconButton size="small" color="error" onClick={() => handleDelete(model.id)}>
                        <DeleteIcon />
                      </IconButton>
                    </Tooltip>
                  </CardActions>
                </Card>
              </Grid>
            ))
          )
        ) : (
          /* 用量统计 */
          stats.length === 0 ? (
            <Grid item xs={12}>
              <Paper sx={{ p: 6, textAlign: 'center', borderRadius: '16px' }}>
                <Typography variant="h6" color="text.secondary">
                  暂无用量数据
                </Typography>
                <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                  使用模型后将显示用量统计
                </Typography>
              </Paper>
            </Grid>
          ) : (
            stats.map((stat) => (
              <Grid item xs={12} md={6} lg={4} key={stat.modelId}>
                <ModelStatsPanel stats={stat} />
              </Grid>
            ))
          )
        )}
      </Grid>

      {/* 新增/编辑对话框 */}
      <Dialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        maxWidth="md"
        fullWidth
        PaperProps={{ sx: { borderRadius: '16px' } }}
      >
        <ModelForm
          initialData={editingModel}
          onSubmit={handleSubmit}
          onTest={async (data: ModelTestReq): Promise<ModelTestResp> => {
            // 表单内测试：直接透传（后端真实探测 /chat/completions）
            return api.model.test({ ...data, id: editingModel?.id });
          }}
          onCancel={() => setDialogOpen(false)}
        />
      </Dialog>
    </Box>
  );
};

export default ModelHub;
