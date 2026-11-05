import React from 'react';
import ReactDOM from 'react-dom/client';
// Mock 垫片：无后端时注入模拟 tongtianAPI（开发自测用，需在 App 加载前执行）
import './lib/mock-api';
import App from './App';
import ThemedApp from './theme/ThemedApp';
import { applyDocumentTheme, resolveMode } from './theme/bootstrap';
import { usePreferenceStore } from './stores/usePreferenceStore';
import { I18nProvider } from './i18n';
import { installExternalLinkGuard } from './lib/external-links';
import './styles/globals.css';

// 外链统一走系统浏览器：避免远程页面留在持有 IPC 能力的 webview 里
installExternalLinkGuard();

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('Root element not found');
}

// 首帧之前先把存过的亮暗与密度落到 <html>：否则暗色用户每次启动都先白一下。
// 「界面风格 / 对话字体」存在引擎侧，加载后由 ThemedApp 立刻校正。
const bootPrefs = usePreferenceStore.getState();
applyDocumentTheme({
  mode: resolveMode(bootPrefs.theme),
  uiStyle: '默认',
  density: bootPrefs.density,
  chatFont: '无衬线',
});

const root = ReactDOM.createRoot(rootElement);
root.render(
  <React.StrictMode>
    <ThemedApp>
      <I18nProvider>
        <App />
      </I18nProvider>
    </ThemedApp>
  </React.StrictMode>
);
