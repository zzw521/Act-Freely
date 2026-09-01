/**
 * 通天办公 - 应用根组件
 *
 * 路由配置:
 * - 使用 HashRouter (Electron 兼容)
 * - MainLayout 作为根路由
 * - 千问对标整改新增: /connectors /hooks /im /archive /awareness /computer-use
 *
 * 注意：Suspense 边界下沉到 MainLayout 内容区（仅包裹 Outlet），
 * 避免懒加载页面挂起时整个布局（含侧边栏）被卸载重建，
 * 导致侧边栏弹窗锚点失效、状态丢失。
 */

import React, { lazy } from 'react';
import { HashRouter, Routes, Route, Navigate } from 'react-router-dom';
import MainLayout from './components/Layout/MainLayout';

// ── 懒加载页面组件 ──────────────────────────────────────────────────

const Workbench = lazy(() => import('./pages/Workbench'));
const ModelHub = lazy(() => import('./pages/ModelHub'));
const SecurityPage = lazy(() => import('./pages/Security'));
const ExtensionsPage = lazy(() => import('./pages/Extensions'));
const ExpertSuitePage = lazy(() => import('./pages/ExpertSuite'));
const SkillMarket = lazy(() => import('./pages/SkillMarket'));
const WorkspacePanel = lazy(() => import('./pages/WorkspacePanel'));
const FilePanel = lazy(() => import('./pages/FilePanel'));
const ScheduleView = lazy(() => import('./pages/ScheduleView'));
const Settings = lazy(() => import('./pages/Settings'));
const ConnectorCenter = lazy(() => import('./pages/ConnectorCenter'));
const HooksManager = lazy(() => import('./pages/HooksManager'));
const AwarenessPage = lazy(() => import('./pages/AwarenessPage'));
const ComputerUsePanel = lazy(() => import('./pages/ComputerUsePanel'));
const IMChannel = lazy(() => import('./pages/IMChannel'));
const ArchiveView = lazy(() => import('./pages/ArchiveView'));

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
          {/* 旧路由兼容：技能/连接器重定向到扩展页对应标签 */}
          <Route path="/skill-market" element={<Navigate to="/extensions?tab=skills" replace />} />
          <Route path="/connectors" element={<Navigate to="/extensions?tab=connectors" replace />} />
          <Route path="/workspace" element={<WorkspacePanel />} />
          <Route path="/files" element={<FilePanel />} />
          <Route path="/schedule" element={<ScheduleView />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/hooks" element={<HooksManager />} />
          <Route path="/awareness" element={<AwarenessPage />} />
          <Route path="/computer-use" element={<ComputerUsePanel />} />
          <Route path="/im" element={<IMChannel />} />
          <Route path="/archive" element={<ArchiveView />} />
        </Route>
      </Routes>
    </HashRouter>
  );
};

export default App;
