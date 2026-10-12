/**
 * 通天办公 - 主布局组件（设计图对齐版）
 *
 * 结构：Sidebar + 全高内容区（无全局 TopBar，页面自带头部）
 * Workbench 三栏布局（聊天区 + 任务监控）由页面自身组织
 *
 * Suspense 边界仅包裹 Outlet：懒加载页面挂起时只影响内容区，
 * 侧边栏不会被卸载重建（避免弹窗锚点失效、页签状态丢失）
 */

import React, { Suspense, useState } from 'react';
import {
  Alert,
  Box,
  CircularProgress,
  Snackbar,
  useMediaQuery,
  useTheme,
} from '@mui/material';
import { Outlet, useLocation } from 'react-router-dom';
import Sidebar from './Sidebar';
import ResizeHandle from './ResizeHandle';
import TaskSearchDialog from '../TaskSearchDialog';
import { useAppStore } from '../../stores/useAppStore';
import { useAppShortcuts } from '../../lib/use-app-shortcuts';

/** 懒加载页面占位 */
const PageLoader: React.FC = () => (
  <Box sx={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '400px' }}>
    <CircularProgress size={40} />
  </Box>
);

const MainLayout: React.FC = () => {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('md'));
  // 窄屏默认收起可手动展开；桌面端折叠状态放在 store，Ctrl+\ 快捷键与折叠按钮共用同一份
  const [open, setOpen] = useState(true);
  const sidebarCollapsed = useAppStore((s) => s.sidebarCollapsed);
  const toggleSidebar = useAppStore((s) => s.toggleSidebar);
  // 侧边栏宽度：右边界长按拖拽调宽（200~400）
  const [sidebarWidth, setSidebarWidth] = useState(240);
  const location = useLocation();
  // Workbench（/）采用全高三栏布局，无外层 padding；其他页面保留内边距
  const isWorkbench = location.pathname === '/';
  // 全局通知：各页 `showNotification` 写入 store，由此处统一渲染（否则调用方点了没反应）
  const notification = useAppStore((s) => s.notification);
  const closeNotification = useAppStore((s) => s.closeNotification);
  // 全局任务搜索（Ctrl + G）：挂在布局层，任何页面都能拉起
  const taskSearchOpen = useAppStore((s) => s.taskSearchOpen);
  const setTaskSearchOpen = useAppStore((s) => s.setTaskSearchOpen);
  // 应用内快捷键在此统一注册（设置页快捷键分区展示的就是这份清单）
  useAppShortcuts();

  const handleDrawerToggle = () => {
    if (isMobile) setOpen(!open);
    else toggleSidebar();
  };

  // 窄屏使用临时抽屉，桌面端跟随 store 折叠状态
  const drawerOpen = isMobile ? open : !sidebarCollapsed;

  return (
    <Box sx={{ display: 'flex', height: '100vh', overflow: 'hidden', background: theme.palette.background.default }}>
      {/* 侧边栏（右边界可拖拽调宽） */}
      <Box sx={{ position: 'relative', height: '100%', flexShrink: 0 }}>
        <Sidebar open={drawerOpen} onDrawerToggle={handleDrawerToggle} panelWidth={sidebarWidth} />
        {drawerOpen && !isMobile && (
          <ResizeHandle
            side="left"
            width={sidebarWidth}
            min={200}
            max={400}
            onWidthChange={setSidebarWidth}
          />
        )}
      </Box>

      {/* 内容区域（全高，页面自带头部与内边距） */}
      <Box
        component="main"
        sx={{
          flexGrow: 1,
          display: 'flex',
          flexDirection: 'column',
          minWidth: 0,
          height: '100vh',
          overflow: 'hidden',
          background: theme.palette.background.default,
        }}
      >
        {isWorkbench ? (
          <Suspense fallback={<PageLoader />}>
            <Outlet />
          </Suspense>
        ) : (
          <Box sx={{ flex: 1, minHeight: 0, overflow: 'auto', padding: theme.spacing(2, 3) }}>
            <Suspense fallback={<PageLoader />}>
              <Outlet />
            </Suspense>
          </Box>
        )}
      </Box>

      {/* 全局通知条：notification 为 null 时不渲染 */}
      <Snackbar
        open={Boolean(notification?.open)}
        autoHideDuration={4000}
        onClose={closeNotification}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert
          severity={notification?.severity ?? 'info'}
          variant="filled"
          onClose={closeNotification}
          sx={{ fontSize: 13 }}
        >
          {notification?.message ?? ''}
        </Alert>
      </Snackbar>

      {/* 全局任务搜索面板：Ctrl + G 拉起，Enter 切到命中的会话 */}
      <TaskSearchDialog open={taskSearchOpen} onClose={() => setTaskSearchOpen(false)} />
    </Box>
  );
};

export default MainLayout;
