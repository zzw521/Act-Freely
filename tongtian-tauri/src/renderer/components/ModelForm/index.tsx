import React, { useState } from 'react';
import {
  Box,
  Card,
  CardContent,
  CardActions,
  Button,
  TextField,
  MenuItem,
  InputAdornment,
  Typography,
  Chip,
  Alert,
  LinearProgress,
  IconButton,
  Tooltip,
  Divider,
  Stack,
} from '@mui/material';
import VisibilityIcon from '@mui/icons-material/Visibility';
import VisibilityOffIcon from '@mui/icons-material/VisibilityOff';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import CancelIcon from '@mui/icons-material/Cancel';
import RefreshIcon from '@mui/icons-material/Refresh';
import DeleteIcon from '@mui/icons-material/Delete';
import EditIcon from '@mui/icons-material/Edit';
import SettingsIcon from '@mui/icons-material/Settings';
import { ModelConfig, ModelSource, ModelTestReq, ModelTestResp } from '../../../shared/types';
import { errorTextOf } from '../../../shared/error-messages';

interface ModelFormProps {
  initialData?: Partial<ModelConfig>;
  onSubmit: (data: Partial<ModelConfig>) => void;
  onTest: (data: ModelTestReq) => Promise<ModelTestResp>;
  onCancel?: () => void;
  isSubmitting?: boolean;
}

const ModelForm: React.FC<ModelFormProps> = ({
  initialData,
  onSubmit,
  onTest,
  onCancel,
  isSubmitting,
}) => {
  const [formData, setFormData] = useState({
    name: initialData?.name || '',
    source: initialData?.source || ModelSource.OPENAI_COMPAT,
    baseUrl: initialData?.baseUrl || '',
    // 读取视图只给 hasApiKey，不回传密钥或旧式密文字段
    apiKey: initialData?.hasApiKey ? '●●●●●●●●' : '',
    modelName: initialData?.modelName || '',
    contextWindow: initialData?.contextWindow || 4096,
    isDefault: initialData?.isDefault || false,
    agentBinding: initialData?.agentBinding || '',
    forceLocal: initialData?.forceLocal || false,
  });

  const [showApiKey, setShowApiKey] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<ModelTestResp | null>(null);
  const [error, setError] = useState<string | null>(null);

  const sources = Object.values(ModelSource).map((source) => ({
    value: source,
    label: getModelSourceLabel(source),
  }));

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onSubmit(formData);
  };

  const handleTest = async () => {
    if (!formData.baseUrl || !formData.modelName) {
      setError('请填写 BaseURL 和模型名称');
      return;
    }

    setTesting(true);
    setError(null);
    setTestResult(null);

    try {
      const result = await onTest({
        baseUrl: formData.baseUrl,
        apiKey: formData.apiKey === '●●●●●●●●' ? '' : formData.apiKey,
        modelName: formData.modelName,
        contextWindow: formData.contextWindow,
      });
      setTestResult(result);
    } catch (err) {
      setError(`测试失败: ${errorTextOf(err)}`);
    } finally {
      setTesting(false);
    }
  };

  return (
    <Box component="form" onSubmit={handleSubmit}>
      {/* 头部标题 */}
      <Typography variant="h6" fontWeight="bold" sx={{ mb: 3 }}>
        {initialData?.id ? '✏️ 编辑模型' : '➕ 添加模型'}
      </Typography>

      {/* 测试结果显示 */}
      {testResult && (
        <Alert
          severity={testResult.ok ? 'success' : 'error'}
          action={
            <IconButton
              size="small"
              onClick={() => setTestResult(null)}
            >
              <CancelIcon fontSize="inherit" />
            </IconButton>
          }
          sx={{ mb: 2 }}
        >
          {testResult.ok ? (
            <>
              <strong>连接成功!</strong> 延迟: {testResult.latencyMs}ms, 模型: {testResult.model}
            </>
          ) : (
            <>
              <strong>连接失败:</strong> {testResult.error}
            </>
          )}
        </Alert>
      )}

      {error && (
        <Alert severity="error" onClose={() => setError(null)} sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}

      <Stack spacing={2}>
        {/* 模型名称 */}
        <TextField
          fullWidth
          label="模型名称"
          value={formData.name}
          onChange={(e) => setFormData({ ...formData, name: e.target.value })}
          placeholder="例如：我的自定义模型"
          required
          helperText="用户自定义的显示名称"
        />

        {/* 模型源类型 */}
        <TextField
          fullWidth
          select
          label="模型源类型"
          value={formData.source}
          onChange={(e) => setFormData({ ...formData, source: e.target.value as ModelSource })}
          helperText="选择模型的来源类型"
        >
          {sources.map((option) => (
            <MenuItem key={option.value} value={option.value}>
              {option.label}
            </MenuItem>
          ))}
        </TextField>

        {/* BaseURL */}
        <TextField
          fullWidth
          label="Base URL"
          value={formData.baseUrl}
          onChange={(e) => setFormData({ ...formData, baseUrl: e.target.value })}
          placeholder="https://api.openai.com/v1"
          required
          helperText="模型 API 的端点地址"
        />

        {/* API Key */}
        <TextField
          fullWidth
          label="API Key"
          type={showApiKey ? 'text' : 'password'}
          value={formData.apiKey}
          onChange={(e) => setFormData({ ...formData, apiKey: e.target.value })}
          placeholder="sk-..."
          InputProps={{
            endAdornment: (
              <InputAdornment position="end">
                <Tooltip title={showApiKey ? '隐藏' : '显示'}>
                  <IconButton
                    size="small"
                    onClick={() => setShowApiKey(!showApiKey)}
                  >
                    {showApiKey ? <VisibilityOffIcon fontSize="small" /> : <VisibilityIcon fontSize="small" />}
                  </IconButton>
                </Tooltip>
              </InputAdornment>
            ),
          }}
          helperText="API 密钥将加密存储，不会明文保存"
        />

        {/* 模型名称（服务端） */}
        <TextField
          fullWidth
          label="服务端模型名称"
          value={formData.modelName}
          onChange={(e) => setFormData({ ...formData, modelName: e.target.value })}
          placeholder="gpt-4, qwen-plus, 等"
          required
          helperText="API 调用的模型标识符"
        />

        {/* 上下文窗口大小 */}
        <TextField
          fullWidth
          type="number"
          label="上下文窗口 (tokens)"
          value={formData.contextWindow}
          onChange={(e) => setFormData({ ...formData, contextWindow: parseInt(e.target.value) || 4096 })}
          inputProps={{ min: 512, max: 128000 }}
          helperText="模型支持的最大 token 数"
        />

        {/* 选项 */}
        <Stack direction="row" spacing={2}>
          <Chip
            label="🌐 设为全局默认"
            clickable
            color={formData.isDefault ? 'primary' : 'default'}
            onClick={() => setFormData({ ...formData, isDefault: !formData.isDefault })}
            sx={{ flex: 1 }}
          />
          <Chip
            label="🔒 强制本地模式"
            clickable
            color={formData.forceLocal ? 'secondary' : 'default'}
            onClick={() => setFormData({ ...formData, forceLocal: !formData.forceLocal })}
            sx={{ flex: 1 }}
            title="敏感任务强制使用本地模型"
          />
        </Stack>

        {/* 智能体绑定（可选） */}
        <TextField
          fullWidth
          label="智能体绑定（可选）"
          value={formData.agentBinding}
          onChange={(e) => setFormData({ ...formData, agentBinding: e.target.value })}
          placeholder="绑定到特定智能体 ID"
          helperText="留空表示全局可用"
        />
      </Stack>

      {/* 操作按钮 */}
      <Divider sx={{ my: 3 }} />
      <Box sx={{ display: 'flex', gap: 2, justifyContent: 'flex-end' }}>
        {onCancel && (
          <Button onClick={onCancel} variant="outlined">
            取消
          </Button>
        )}
        <Button
          onClick={handleTest}
          disabled={!formData.baseUrl || !formData.modelName || testing}
          startIcon={testing ? <LinearProgress sx={{ width: 20 }} /> : <RefreshIcon />}
          variant="outlined"
        >
          {testing ? '测试中...' : '连通性测试'}
        </Button>
        <Button
          type="submit"
          variant="contained"
          disabled={isSubmitting || !formData.name || !formData.baseUrl || !formData.modelName}
          startIcon={<CheckCircleIcon />}
        >
          {initialData?.id ? '更新模型' : '添加模型'}
        </Button>
      </Box>
    </Box>
  );
};

// 模型源类型标签映射
const getModelSourceLabel = (source: ModelSource): string => {
  const labels: Record<ModelSource, string> = {
    [ModelSource.OPENAI_COMPAT]: 'OpenAI 兼容',
    [ModelSource.OLLAMA]: 'Ollama (本地)',
    [ModelSource.PRESET_QWEN]: '通义千问（预置）',
    [ModelSource.PRESET_DEEPSEEK]: 'DeepSeek（预置）',
    [ModelSource.PRESET_OPENAI]: 'OpenAI（预置）',
    [ModelSource.CUSTOM]: '自定义',
  };
  return labels[source] || source;
};

export default ModelForm;
