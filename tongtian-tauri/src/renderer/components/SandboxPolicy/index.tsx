import React, { useState, useEffect } from 'react';
import {
  Box,
  Card,
  CardContent,
  Typography,
  TextField,
  MenuItem,
  Button,
  Switch,
  Chip,
  Alert,
  Divider,
  Stack,
  FormControlLabel,
  Slider,
  Paper,
  IconButton,
  Tooltip,
  Grid,
} from '@mui/material';
import SaveIcon from '@mui/icons-material/Save';
import SecurityIcon from '@mui/icons-material/Security';
import TuneIcon from '@mui/icons-material/Tune';
import AddIcon from '@mui/icons-material/Add';
import DeleteIcon from '@mui/icons-material/Delete';
import { SandboxPolicy } from '../../../shared/types';
import { errorTextOf } from '../../../shared/error-messages';

interface SandboxPolicyFormProps {
  workspaceId: string;
  initialPolicy?: Partial<SandboxPolicy>;
  onSave: (policy: SandboxPolicy) => Promise<void>;
  onCancel?: () => void;
}

const SandboxPolicyForm: React.FC<SandboxPolicyFormProps> = ({
  workspaceId,
  initialPolicy,
  onSave,
  onCancel,
}) => {
  const [policy, setPolicy] = useState<SandboxPolicy>({
    workspaceId,
    executionLevel: initialPolicy?.executionLevel || 'L1',
    fileAllowlist: initialPolicy?.fileAllowlist || [],
    fileBlocklist: initialPolicy?.fileBlocklist || [],
    toolAllowlist: initialPolicy?.toolAllowlist || [],
    toolBlocklist: initialPolicy?.toolBlocklist || [],
    maxCpuPercent: initialPolicy?.maxCpuPercent || 50,
    maxMemoryMB: initialPolicy?.maxMemoryMB || 512,
    maxDiskWriteMbps: initialPolicy?.maxDiskWriteMbps || 10,
  });

  const [newAllowlistPath, setNewAllowlistPath] = useState('');
  const [newBlocklistPath, setNewBlocklistPath] = useState('');
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);

  const handleSave = async () => {
    try {
      setSaving(true);
      setMessage(null);
      await onSave(policy);
      setMessage({ type: 'success', text: '策略配置已保存' });
    } catch (err) {
      setMessage({ type: 'error', text: `保存失败: ${errorTextOf(err)}` });
    } finally {
      setSaving(false);
    }
  };

  const handleAddAllowlist = () => {
    if (!newAllowlistPath.trim()) return;
    if (!policy.fileAllowlist.includes(newAllowlistPath.trim())) {
      setPolicy({
        ...policy,
        fileAllowlist: [...policy.fileAllowlist, newAllowlistPath.trim()],
      });
    }
    setNewAllowlistPath('');
  };

  const handleRemoveAllowlist = (path: string) => {
    setPolicy({
      ...policy,
      fileAllowlist: policy.fileAllowlist.filter(p => p !== path),
    });
  };

  const handleAddBlocklist = () => {
    if (!newBlocklistPath.trim()) return;
    if (!policy.fileBlocklist.includes(newBlocklistPath.trim())) {
      setPolicy({
        ...policy,
        fileBlocklist: [...policy.fileBlocklist, newBlocklistPath.trim()],
      });
    }
    setNewBlocklistPath('');
  };

  const handleRemoveBlocklist = (path: string) => {
    setPolicy({
      ...policy,
      fileBlocklist: policy.fileBlocklist.filter(p => p !== path),
    });
  };

  const getLevelLabel = (level: string): string => {
    const labels: Record<string, string> = {
      L0: 'L0 - 只读模式',
      L1: 'L1 - 标准权限',
      L2: 'L2 - 完全权限',
    };
    return labels[level] || level;
  };

  const getLevelDescription = (level: string): string => {
    const descriptions: Record<string, string> = {
      L0: '仅允许读取文件，不能写入或修改任何内容',
      L1: '允许在授权目录内读写，受沙箱策略控制',
      L2: '完全文件系统访问权限（慎用）',
    };
    return descriptions[level] || '';
  };

  return (
    <Box>
      <Typography variant="h6" fontWeight="bold" sx={{ mb: 2 }}>
        🔐 沙箱策略配置
      </Typography>

      {message && (
        <Alert severity={message.type} onClose={() => setMessage(null)} sx={{ mb: 2 }}>
          {message.text}
        </Alert>
      )}

      <Card variant="outlined" sx={{ mb: 2 }}>
        <CardContent>
          {/* 执行权限等级 */}
          <Typography variant="subtitle2" fontWeight="medium" sx={{ mb: 1 }}>
            执行权限等级
          </Typography>
          <Grid container spacing={2} sx={{ mb: 2 }}>
            {['L0', 'L1', 'L2'].map((level) => (
              <Grid item xs={12} md={4} key={level}>
                <Paper
                  elevation={policy.executionLevel === level ? 3 : 0}
                  sx={{
                    p: 2,
                    border: 2,
                    borderRadius: 2,
                    borderColor: policy.executionLevel === level ? 'primary.main' : 'divider',
                    cursor: 'pointer',
                    transition: 'all 0.2s',
                    '&:hover': {
                      borderColor: 'primary.light',
                    },
                  }}
                  onClick={() => setPolicy({ ...policy, executionLevel: level as any })}
                >
                  <Typography variant="body2" fontWeight="medium" color="primary.main">
                    {getLevelLabel(level)}
                  </Typography>
                  <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5, display: 'block' }}>
                    {getLevelDescription(level)}
                  </Typography>
                </Paper>
              </Grid>
            ))}
          </Grid>

          <Divider sx={{ my: 2 }} />

          {/* 资源限制 */}
          <Typography variant="subtitle2" fontWeight="medium" sx={{ mb: 1 }}>
            ⚡ 资源配额限制
          </Typography>

          <Box sx={{ mb: 2 }}>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              CPU 使用率上限: {policy.maxCpuPercent}%
            </Typography>
            <Slider
              value={policy.maxCpuPercent}
              onChange={(_, value) => setPolicy({ ...policy, maxCpuPercent: value as number })}
              min={10}
              max={100}
              step={5}
              valueLabelDisplay="auto"
              sx={{ mb: 1 }}
            />
          </Box>

          <Box sx={{ mb: 2 }}>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              内存上限: {policy.maxMemoryMB} MB
            </Typography>
            <Slider
              value={policy.maxMemoryMB}
              onChange={(_, value) => setPolicy({ ...policy, maxMemoryMB: value as number })}
              min={128}
              max={8192}
              step={128}
              valueLabelDisplay="auto"
              sx={{ mb: 1 }}
            />
          </Box>

          <Box sx={{ mb: 2 }}>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              磁盘写入速率: {policy.maxDiskWriteMbps} MB/s
            </Typography>
            <Slider
              value={policy.maxDiskWriteMbps}
              onChange={(_, value) => setPolicy({ ...policy, maxDiskWriteMbps: value as number })}
              min={1}
              max={100}
              step={1}
              valueLabelDisplay="auto"
              sx={{ mb: 1 }}
            />
          </Box>

          <Divider sx={{ my: 2 }} />

          {/* 文件白名单 */}
          <Typography variant="subtitle2" fontWeight="medium" sx={{ mb: 1 }}>
            ✅ 文件访问白名单
          </Typography>
          <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
            <TextField
              fullWidth
              size="small"
              placeholder="输入允许访问的路径（如：C:/Users/.../Documents）"
              value={newAllowlistPath}
              onChange={(e) => setNewAllowlistPath(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleAddAllowlist()}
            />
            <Button
              variant="contained"
              onClick={handleAddAllowlist}
              disabled={!newAllowlistPath.trim()}
              startIcon={<AddIcon />}
              sx={{ minWidth: 100 }}
            >
              添加
            </Button>
          </Stack>

          <Stack spacing={0.5} sx={{ mb: 2 }}>
            {policy.fileAllowlist.length === 0 ? (
              <Typography variant="caption" color="text.secondary" sx={{ fontStyle: 'italic' }}>
                暂无白名单规则，请添加允许访问的路径
              </Typography>
            ) : (
              policy.fileAllowlist.map((path) => (
                <Chip
                  key={path}
                  label={path}
                  variant="outlined"
                  size="small"
                  onDelete={() => handleRemoveAllowlist(path)}
                  sx={{ m: 0.25 }}
                />
              ))
            )}
          </Stack>

          <Divider sx={{ my: 2 }} />

          {/* 文件黑名单 */}
          <Typography variant="subtitle2" fontWeight="medium" sx={{ mb: 1 }}>
            ❌ 文件访问黑名单
          </Typography>
          <Stack direction="row" spacing={1} sx={{ mb: 2 }}>
            <TextField
              fullWidth
              size="small"
              placeholder="输入禁止访问的路径（如：C:/Windows）"
              value={newBlocklistPath}
              onChange={(e) => setNewBlocklistPath(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleAddBlocklist()}
            />
            <Button
              variant="outlined"
              onClick={handleAddBlocklist}
              disabled={!newBlocklistPath.trim()}
              startIcon={<AddIcon />}
              sx={{ minWidth: 100 }}
            >
              添加
            </Button>
          </Stack>

          <Stack spacing={0.5}>
            {policy.fileBlocklist.length === 0 ? (
              <Typography variant="caption" color="text.secondary" sx={{ fontStyle: 'italic' }}>
                暂无黑名单规则，请添加禁止访问的路径
              </Typography>
            ) : (
              policy.fileBlocklist.map((path) => (
                <Chip
                  key={path}
                  label={path}
                  variant="outlined"
                  color="error"
                  size="small"
                  onDelete={() => handleRemoveBlocklist(path)}
                  sx={{ m: 0.25 }}
                />
              ))
            )}
          </Stack>
        </CardContent>
      </Card>

      {/* 操作按钮 */}
      <Box sx={{ display: 'flex', gap: 2, justifyContent: 'flex-end' }}>
        {onCancel && (
          <Button onClick={onCancel} variant="outlined">
            取消
          </Button>
        )}
        <Button
          onClick={handleSave}
          disabled={saving}
          startIcon={<SaveIcon />}
          variant="contained"
        >
          {saving ? '保存中...' : '保存策略'}
        </Button>
      </Box>
    </Box>
  );
};

export default SandboxPolicyForm;
