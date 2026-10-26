/**
 * 通天办公 - 应用根组件
 *
 * 路由配置:
 * - 使用 HashRouter (Electron 兼容)
 * - MainLayout 作为根路由
 * - 千问对标整改新增: /connectors /hooks /im /archive /awareness
 * - Octop 对标新增: /browser-use（受控浏览器会话，现并入设置「执行能力」）
 * - 执行能力（浏览器 AI+/终端 AI+/电脑操控/远程桌面）收进设置分区，旧路由保留重定向
 * - 专家团队并入专家套件页（?tab=experts），不再单独占导航位
 *
 * 注意：Suspense 边界下沉到 MainLayout 内容区（仅包裹 Outlet），
 * 避免懒加载页面挂起时整个布局（含侧边栏）被卸载重建，
 * 导致侧边栏弹窗锚点失效、状态丢失。
 */

import React, { lazy } from 'react';
import { HashRouter, Routes, Route, Navigate } from 'react-router-dom';
import MainLayout from './components/Layout/MainLayout';
import EngineTokenDialog from './components/EngineTokenDialog';

// ── 懒加载页面组件 ──────────────────────────────────────────────────

const Workbench = lazy(() => import('./pages/Workbench'));
const ModelHub = lazy(() => import('./pages/ModelHub'));
const SecurityPage = lazy(() => import('./pages/Security'));
const ExtensionsPage = lazy(() => import('./pages/Extensions'));
const ExpertSuitePage = lazy(() => import('./pages/ExpertSuite'));
const KnowledgePage = lazy(() => import('./pages/Knowledge'));
const WorkspacePanel = lazy(() => import('./pages/WorkspacePanel'));
const FilePanel = lazy(() => import('./pages/FilePanel'));
const ScheduleView = lazy(() => import('./pages/ScheduleView'));
const Settings = lazy(() => import('./pages/Settings'));
const HooksManager = lazy(() => import('./pages/HooksManager'));
const IMChannel = lazy(() => import('./pages/IMChannel'));
const ArchiveView = lazy(() => import('./pages/ArchiveView'));
const Collaboration = lazy(() => import('./pages/Collaboration'));

// ── App 组件 ──────────────────────────────────────────────────────────

const App: React.FC = () => {
  return (
    <HashRouter>
      <Routes>
        <Route element={<MainLayout />}>
          <Route path="/" element={<Workbench />} />
          <Route path="/model-hub" element={<ModelHub />} />
          <Route path="/security" element={<SecurityPage />} />
          <Route path="/extensions" element={<ExtensionsPage />} />
          <Route path="/expert-suites" element={<ExpertSuitePage />} />
          {/* 专家团队已并入专家套件页的「专家团队」页签 */}
          <Route path="/experts" element={<Navigate to="/expert-suites?tab=experts" replace />} />
          <Route path="/knowledge" element={<KnowledgePage />} />
          {/* 旧路由兼容：技能/连接器重定向到扩展页对应标签 */}
          <Route path="/skill-market" element={<Navigate to="/extensions?tab=skills" replace />} />
          <Route path="/connectors" element={<Navigate to="/extensions?tab=connectors" replace />} />
          <Route path="/workspace" element={<WorkspacePanel />} />
          <Route path="/files" element={<FilePanel />} />
          <Route path="/schedule" element={<ScheduleView />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/hooks" element={<HooksManager />} />
          {/* 意识内容已合并进设置页意识分区（?s=awareness），独立意识页移除 */}
          <Route path="/awareness" element={<Navigate to="/settings?s=awareness" replace />} />
          {/* 执行能力已收进设置页「执行能力」分区，旧路由保留重定向 */}
          <Route path="/computer-use" element={<Navigate to="/settings?s=computer-use" replace />} />
          <Route path="/browser-use" element={<Navigate to="/settings?s=browser-use" replace />} />
          <Route path="/terminal" element={<Navigate to="/settings?s=terminal-ai" replace />} />
          <Route path="/remote-desktop" element={<Navigate to="/settings?s=remote-desktop" replace />} />
          <Route path="/im" element={<IMChannel />} />
          <Route path="/archive" element={<ArchiveView />} />
          {/* 跨用户/跨机器项目协作（桌面端 Rust → 账号服务） */}
          <Route path="/collaboration" element={<Collaboration />} />
        </Route>
      </Routes>
      {/* 服务化模式：引擎返回 401 时输入令牌（仅网页通道，桌面版不会触发） */}
      <EngineTokenDialog />
    </HashRouter>
  );
};

export default App;
