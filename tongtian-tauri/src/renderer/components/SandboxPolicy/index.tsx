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
  Dialog,
  DialogTitle,
  DialogContent,
  DialogContentText,
  DialogActions,
} from '@mui/material';
import SaveIcon from '@mui/icons-material/Save';
import SecurityIcon from '@mui/icons-material/Security';
import TuneIcon from '@mui/icons-material/Tune';
import AddIcon from '@mui/icons-material/Add';
import DeleteIcon from '@mui/icons-material/Delete';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import { SandboxPolicy } from '../../../shared/types';
import { errorTextOf } from '../../../shared/error-messages';
import { isTauri } from '../../lib/tauri-api';

/** 裸命令名（与 Rust `policy::normalize_command` 同规则） */
const COMMAND_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$/;

/** 主机名归一化（与 Rust `policy::normalize_domain` 同规则：允许粘贴完整 URL） */
function normalizeHost(raw: string): string {
  const s = raw.trim().toLowerCase();
  const noScheme = s.includes('://') ? s.split('://').pop() ?? '' : s;
  return noScheme.split('/')[0].split(':')[0].trim();
}

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
    execEnabled: initialPolicy?.execEnabled ?? false,
    execAllowlist: initialPolicy?.execAllowlist ?? [],
    networkEnabled: initialPolicy?.networkEnabled ?? false,
    networkAllowlist: initialPolicy?.networkAllowlist ?? [],
  });

  const [newAllowlistPath, setNewAllowlistPath] = useState('');
  const [newBlocklistPath, setNewBlocklistPath] = useState('');
  const [newToolAllow, setNewToolAllow] = useState('');
  const [newToolBlock, setNewToolBlock] = useState('');
  const [newCommand, setNewCommand] = useState('');
  const [newHost, setNewHost] = useState('');
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null);
  // 浏览器直连通道不会把沙箱策略下发给引擎（执行授权只在桌面版生效），
  // 这里禁用开关并说明，避免「在预览里点了开启」被当成真的放行。
  const desktop = isTauri();

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

  const handleSaveClick = () => {
    if (policy.execEnabled && policy.execAllowlist.length === 0) {
      setMessage({ type: 'error', text: '已开启受控命令执行，但命令白名单为空：请先添加至少一个命令，或关闭该能力' });
      return;
    }
    if (policy.networkEnabled && policy.networkAllowlist.length === 0) {
      setMessage({ type: 'error', text: '已开启联网获取，但主机白名单为空：请先添加至少一个主机，或关闭该能力' });
      return;
    }
    const newlyGranted =
      (policy.execEnabled && !initialPolicy?.execEnabled)
      || (policy.networkEnabled && !initialPolicy?.networkEnabled);
    if (newlyGranted) {
      setConfirmOpen(true);
      return;
    }
    void handleSave();
  };

  const handleAddCommand = () => {
    const name = newCommand.trim().toLowerCase();
    if (!COMMAND_NAME_RE.test(name)) {
      setMessage({
        type: 'error',
        text: '命令名必须是裸命令名（字母/数字/下划线/连字符/点/加号），不能带路径、空格或 shell 元字符',
      });
      return;
    }
    if (!policy.execAllowlist.includes(name)) {
      setPolicy({ ...policy, execAllowlist: [...policy.execAllowlist, name] });
    }
    setNewCommand('');
    setMessage(null);
  };

  const handleRemoveCommand = (name: string) => {
    setPolicy({ ...policy, execAllowlist: policy.execAllowlist.filter(c => c !== name) });
  };

  const handleAddHost = () => {
    const host = normalizeHost(newHost);
    if (!host || /\s/.test(host)) {
      setMessage({ type: 'error', text: '主机名不能为空，也不能包含空格' });
      return;
    }
    if (!policy.networkAllowlist.includes(host)) {
      setPolicy({ ...policy, networkAllowlist: [...policy.networkAllowlist, host] });
    }
    setNewHost('');
    setMessage(null);
  };

  const handleRemoveHost = (host: string) => {
    setPolicy({ ...policy, networkAllowlist: policy.networkAllowlist.filter(h => h !== host) });
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

  const handleAddToolAllow = () => {
    const name = newToolAllow.trim();
    if (!name) return;
    if (!policy.toolAllowlist.includes(name)) {
      setPolicy({ ...policy, toolAllowlist: [...policy.toolAllowlist, name] });
    }
    setNewToolAllow('');
  };

  const handleRemoveToolAllow = (name: string) => {
    setPolicy({ ...policy, toolAllowlist: policy.toolAllowlist.filter(t => t !== name) });
  };

  const handleAddToolBlock = () => {
    const name = newToolBlock.trim();
    if (!name) return;
    if (!policy.toolBlocklist.includes(name)) {
      setPolicy({ ...policy, toolBlocklist: [...policy.toolBlocklist, name] });
    }
    setNewToolBlock('');
  };

  const handleRemoveToolBlock = (name: string) => {
    setPolicy({ ...policy, toolBlocklist: policy.toolBlocklist.filter(t => t !== name) });
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

          {/* 工具白名单：由引擎在工具调用点执行，白名单非空时其余工具一律拒绝 */}
          <Typography variant="subtitle2" fontWeight="medium" sx={{ mb: 0.5 }}>
            🧰 工具访问白名单
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
            留空表示不限制工具；填写后仅列出的工具可调用。内置工具：view_text_file、write_text_file、insert_text_file，
            可用 * 通配（如 mcp__*）。
          </Typography>
          <Stack direction="row" spacing={1} sx={{ mb: 1 }}>
            <TextField
              fullWidth
              size="small"
              placeholder="输入允许调用的工具名（如 view_text_file）"
              value={newToolAllow}
              onChange={(e) => setNewToolAllow(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleAddToolAllow()}
            />
            <Button
              variant="contained"
              onClick={handleAddToolAllow}
              disabled={!newToolAllow.trim()}
              startIcon={<AddIcon />}
              sx={{ minWidth: 100 }}
            >
              添加
            </Button>
          </Stack>
          <Stack spacing={0.5} sx={{ mb: 2 }}>
            {policy.toolAllowlist.length === 0 ? (
              <Typography variant="caption" color="text.secondary" sx={{ fontStyle: 'italic' }}>
                暂无工具白名单规则
              </Typography>
            ) : (
              policy.toolAllowlist.map((name) => (
                <Chip
                  key={name}
                  label={name}
                  variant="outlined"
                  size="small"
                  onDelete={() => handleRemoveToolAllow(name)}
                  sx={{ m: 0.25 }}
                />
              ))
            )}
          </Stack>

          <Divider sx={{ my: 2 }} />

          {/* 工具黑名单 */}
          <Typography variant="subtitle2" fontWeight="medium" sx={{ mb: 0.5 }}>
            🚫 工具访问黑名单
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
            黑名单优先于白名单；命中的工具在引擎侧直接拒绝，并写入审计日志。
          </Typography>
          <Stack direction="row" spacing={1} sx={{ mb: 1 }}>
            <TextField
              fullWidth
              size="small"
              placeholder="输入禁止调用的工具名（如 write_text_file）"
              value={newToolBlock}
              onChange={(e) => setNewToolBlock(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleAddToolBlock()}
            />
            <Button
              variant="outlined"
              onClick={handleAddToolBlock}
              disabled={!newToolBlock.trim()}
              startIcon={<AddIcon />}
              sx={{ minWidth: 100 }}
            >
              添加
            </Button>
          </Stack>
          <Stack spacing={0.5}>
            {policy.toolBlocklist.length === 0 ? (
              <Typography variant="caption" color="text.secondary" sx={{ fontStyle: 'italic' }}>
                暂无工具黑名单规则
              </Typography>
            ) : (
              policy.toolBlocklist.map((name) => (
                <Chip
                  key={name}
                  label={name}
                  variant="outlined"
                  color="error"
                  size="small"
                  onDelete={() => handleRemoveToolBlock(name)}
                  sx={{ m: 0.25 }}
                />
              ))
            )}
          </Stack>

          <Divider sx={{ my: 2 }} />

          {/* 受控执行与联网：默认关闭；开启后引擎只注册白名单内的工具，每次调用写审计 */}
          <Typography variant="subtitle2" fontWeight="medium" sx={{ mb: 0.5 }}>
            🖥️ 受控执行与联网（默认关闭）
          </Typography>
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
            开启后引擎才会注册对应工具；未开启或白名单为空时模型拿不到工具。命令以当前用户身份、
            在任务工作目录内以 argv 方式执行（不经 shell）；联网只允许白名单公网主机，
            本机/内网/云元数据地址一律拒绝。每次调用都会写入审计日志。
          </Typography>
          {!desktop && (
            <Alert severity="info" sx={{ mb: 1.5 }}>
              浏览器预览通道不会把沙箱策略下发给引擎，执行/联网授权仅在桌面版生效；
              这里的开关在预览中不可操作。
            </Alert>
          )}

          <FormControlLabel
            control={
              <Switch
                checked={policy.execEnabled}
                disabled={!desktop}
                onChange={(e) => setPolicy({ ...policy, execEnabled: e.target.checked })}
              />
            }
            label="允许模型运行命令（仅在白名单内）"
          />
          {policy.execEnabled && (
            <Box sx={{ mt: 1, mb: 2 }}>
              <Stack direction="row" spacing={1} sx={{ mb: 1 }}>
                <TextField
                  fullWidth
                  size="small"
                  placeholder="裸命令名（如 python、git、node）"
                  value={newCommand}
                  onChange={(e) => setNewCommand(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleAddCommand()}
                />
                <Button
                  variant="contained"
                  onClick={handleAddCommand}
                  disabled={!newCommand.trim()}
                  startIcon={<AddIcon />}
                  sx={{ minWidth: 100 }}
                >
                  添加
                </Button>
              </Stack>
              {policy.execAllowlist.length === 0 ? (
                <Typography variant="caption" color="text.secondary" sx={{ fontStyle: 'italic' }}>
                  命令白名单为空，保存会被拒绝；请添加命令或关闭该能力
                </Typography>
              ) : (
                policy.execAllowlist.map((name) => (
                  <Chip
                    key={name}
                    label={name}
                    variant="outlined"
                    size="small"
                    onDelete={() => handleRemoveCommand(name)}
                    sx={{ m: 0.25 }}
                  />
                ))
              )}
            </Box>
          )}

          <FormControlLabel
            control={
              <Switch
                checked={policy.networkEnabled}
                disabled={!desktop}
                onChange={(e) => setPolicy({ ...policy, networkEnabled: e.target.checked })}
              />
            }
            label="允许模型联网获取（仅在白名单内）"
          />
          {policy.networkEnabled && (
            <Box sx={{ mt: 1, mb: 2 }}>
              <Stack direction="row" spacing={1} sx={{ mb: 1 }}>
                <TextField
                  fullWidth
                  size="small"
                  placeholder="主机名（如 api.example.com，可粘贴完整 URL）"
                  value={newHost}
                  onChange={(e) => setNewHost(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleAddHost()}
                />
                <Button
                  variant="contained"
                  onClick={handleAddHost}
                  disabled={!newHost.trim()}
                  startIcon={<AddIcon />}
                  sx={{ minWidth: 100 }}
                >
                  添加
                </Button>
              </Stack>
              {policy.networkAllowlist.length === 0 ? (
                <Typography variant="caption" color="text.secondary" sx={{ fontStyle: 'italic' }}>
                  主机白名单为空，保存会被拒绝；请添加主机或关闭该能力
                </Typography>
              ) : (
                policy.networkAllowlist.map((host) => (
                  <Chip
                    key={host}
                    label={host}
                    variant="outlined"
                    size="small"
                    onDelete={() => handleRemoveHost(host)}
                    sx={{ m: 0.25 }}
                  />
                ))
              )}
            </Box>
          )}

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
          onClick={handleSaveClick}
          disabled={saving}
          startIcon={<SaveIcon />}
          variant="contained"
        >
          {saving ? '保存中...' : '保存策略'}
        </Button>
      </Box>

      {/* 新增执行/联网授权必须显式确认：点开关只是意愿，确认后才真正保存并下发 */}
      <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <WarningAmberIcon color="warning" />
          确认授予执行能力
        </DialogTitle>
        <DialogContent>
          <DialogContentText component="div">
            保存后，模型在本工作区的任务中可以：
            <Box component="ul" sx={{ mt: 1, pl: 3 }}>
              {policy.execEnabled && (
                <li>
                  运行这些命令：
                  <strong>{policy.execAllowlist.join('、') || '（空）'}</strong>
                  。命令以当前用户身份真实执行，可读写该用户有权限的文件；
                  白名单只限制命令名，不限制命令本身做什么。
                </li>
              )}
              {policy.networkEnabled && (
                <li>
                  访问这些主机：
                  <strong>{policy.networkAllowlist.join('、') || '（空）'}</strong>
                  。请求从本机真实发出；白名单只限制主机名，不限制请求内容。
                </li>
              )}
            </Box>
            每次调用都会写入审计日志。请只添加你信任且确实需要的命令与主机。
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmOpen(false)}>取消</Button>
          <Button
            variant="contained"
            color="warning"
            onClick={() => {
              setConfirmOpen(false);
              void handleSave();
            }}
          >
            确认授权并保存
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

export default SandboxPolicyForm;
