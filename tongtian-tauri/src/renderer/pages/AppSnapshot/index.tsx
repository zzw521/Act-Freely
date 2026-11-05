/**
 * 通天办公 - 应用快照与浏览器上下文接力（缺陷 D-13 · 任务 #140）
 *
 * 事实来源：
 * - 分级、上限、状态机、拒绝来源、未验收清单全部读自 `/api/snapshot/status`（引擎），
 *   页面不另立「支持什么」的清单；
 * - 预览文本由引擎产出，与确认后的上送载荷出自同一个函数——页面显示什么就发什么；
 * - 工作空间取 `workspaceStore` 的当前项，会话过滤由用户填写：快照按会话隔离是引擎的事，
 *   页面不猜「你现在正在跟哪个任务说话」。
 *
 * 不发生的事也要写清楚：
 * - 本页面不截屏。真实前台应用识别、OS 屏幕捕获、全局快捷键都在桌面壳里，
 *   workspace 车道的 IPC 未接线 ⇒ 真机采集未验收；
 * - 正文不落盘也不留副本：只在这一次渲染里出现，取消/确认/换草稿时立刻撤下；
 * - 接力只判定，不操作浏览器；开/切/关标签页归 W5 的 `/api/browser` 车道。
 *
 * 挂载：本页面是新文件，路由注册位由界面归属方决定（一行 `<AppSnapshotPage />`），
 * 本卡片不代改他人领地内的路由文件。
 */

import React, { useState } from 'react';
import { Box, TextField, Typography } from '@mui/material';
import AppSnapshot from '../../components/AppSnapshot';
import { getActiveWorkspaceId } from '../../store/workspaceStore';

const AppSnapshotPage: React.FC = () => {
  const [session, setSession] = useState('');
  const workspaceId = getActiveWorkspaceId();

  return (
    <Box sx={{ p: 3, maxWidth: 900, mx: 'auto' }}>
      <Typography variant="h6" sx={{ color: 'var(--tt-text-1)', mb: 0.5 }}>
        应用快照
      </Typography>
      <Typography variant="body2" sx={{ color: 'var(--tt-text-3)', mb: 2 }}>
        工作空间：{workspaceId} · 快照与接力只对本机桌面壳开放；正文只活在引擎进程内存里，
        超过 TTL 或引擎重启即消失。
      </Typography>
      <Box sx={{ mb: 2 }}>
        <TextField
          size="small"
          label="按会话过滤（留空=这个工作空间的全部）"
          value={session}
          onChange={(e) => setSession(e.target.value.trim())}
          sx={{ width: 340 }}
        />
      </Box>
      <AppSnapshot workspaceId={workspaceId} session={session || undefined} />
    </Box>
  );
};

export default AppSnapshotPage;
