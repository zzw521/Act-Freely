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
import { Box, CircularProgress, useMediaQuery, useTheme } from '@mui/material';
import { Outlet, useLocation } from 'react-router-dom';
import Sidebar from './Sidebar';
import ResizeHandle from './ResizeHandle';

/** 懒加载页面占位 */
const PageLoader: React.FC = () => (
  <Box sx={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '400px' }}>
    <CircularProgress size={40} />
  </Box>
);

const MainLayout: React.FC = () => {
  const theme = useTheme();
  const isMobile = useMediaQuery(theme.breakpoints.down('md'));
  // 桌面端侧边栏常驻展开（对齐设计图）；仅窄屏默认收起可手动展开
  const [open, setOpen] = useState(true);
  // 侧边栏宽度：右边界长按拖拽调宽（200~400）
  const [sidebarWidth, setSidebarWidth] = useState(240);
  const location = useLocation();
  // Workbench（/）采用全高三栏布局，无外层 padding；其他页面保留内边距
  const isWorkbench = location.pathname === '/';

  const handleDrawerToggle = () => {
    setOpen(!open);
  };

  // 窄屏使用临时抽屉，桌面端永久展开
  const drawerOpen = isMobile ? open : true;

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
    </Box>
  );
};

export default MainLayout;
