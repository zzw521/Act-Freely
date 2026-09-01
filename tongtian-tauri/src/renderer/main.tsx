import React from 'react';
import ReactDOM from 'react-dom/client';
// Mock 垫片：无后端时注入模拟 tongtianAPI（开发自测用，需在 App 加载前执行）
import './lib/mock-api';
import App from './App';
import ThemedApp from './theme/ThemedApp';
import './styles/globals.css';

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('Root element not found');
}

const root = ReactDOM.createRoot(rootElement);
root.render(
  <React.StrictMode>
    <ThemedApp>
      <App />
    </ThemedApp>
  </React.StrictMode>
);
