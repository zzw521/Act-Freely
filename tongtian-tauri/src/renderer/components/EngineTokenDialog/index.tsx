/**
 * 服务化引擎的令牌输入框
 *
 * 仅在网页通道且引擎返回 401 时出现：静态托管的页面没有预置令牌，
 * 也不能把令牌编进构建产物。输入后只写当前标签页的 sessionStorage，
 * 关闭标签页即失效；不写 localStorage、不进 URL。
 */

import React, { useState, useSyncExternalStore } from 'react';
import {
  Alert,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  TextField,
  Typography,
} from '@mui/material';
import {
  dismissEngineUnauthorized,
  getEngineToken,
  isEngineUnauthorized,
  setEngineToken,
  subscribeEngineUnauthorized,
} from '../../lib/engine-http';

const EngineTokenDialog: React.FC = () => {
  const visible = useSyncExternalStore(
    subscribeEngineUnauthorized,
    isEngineUnauthorized,
    () => false,
  );
  const [token, setToken] = useState('');
  const [error, setError] = useState('');
  const hasSavedToken = getEngineToken().length > 0;

  const submit = () => {
    const value = token.trim();
    if (!value) {
      setError('令牌不能为空');
      return;
    }
    setEngineToken(value);
    setToken('');
    setError('');
    // 重新加载，让首屏已经失败的查询用新令牌重跑；不把令牌留在 React 状态里
    window.location.reload();
  };

  return (
    <Dialog open={visible} maxWidth="sm" fullWidth onClose={dismissEngineUnauthorized}>
      <DialogTitle sx={{ fontSize: 16, fontWeight: 700 }}>引擎需要服务令牌</DialogTitle>
      <DialogContent>
        <Typography sx={{ fontSize: 13, color: 'text.secondary', mb: 1.5 }}>
          当前页面由服务化引擎托管，已启用 TT_ENGINE_TOKEN。令牌只保存在本标签页的
          sessionStorage，关闭标签页即失效；不会写入 localStorage、构建产物或 URL。
        </Typography>
        <Alert severity="warning" sx={{ mb: 1.5, fontSize: 12 }}>
          {hasSavedToken
            ? '本标签页已保存的令牌被引擎拒绝（HTTP 401），请更换后再试。'
            : '引擎拒绝了请求（HTTP 401）。请输入部署方设置的引擎令牌。'}
        </Alert>
        <TextField
          autoFocus
          fullWidth
          size="small"
          type="password"
          label="服务令牌"
          value={token}
          onChange={(e) => {
            setToken(e.target.value);
            setError('');
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') submit();
          }}
          error={Boolean(error)}
          helperText={error || '令牌来自引擎环境变量 TT_ENGINE_TOKEN，不要写进 URL 或命令行参数。'}
        />
        <Typography sx={{ fontSize: 12, color: 'text.secondary', mt: 1.5 }}>
          选择「稍后」后，本次页面会话不再自动弹窗；刷新页面可重新输入。服务化模式下
          终端、电脑操控、浏览器 AI+、远程桌面、应用内 Hook 与审计视图等桌面专属能力不可用。
        </Typography>
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={dismissEngineUnauthorized} sx={{ textTransform: 'none' }}>
          稍后
        </Button>
        <Button variant="contained" onClick={submit} disabled={!token.trim()} sx={{ textTransform: 'none' }}>
          保存并重试
        </Button>
      </DialogActions>
    </Dialog>
  );
};

export default EngineTokenDialog;
