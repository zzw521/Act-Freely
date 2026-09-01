import React from 'react';
import ReactDOM from 'react-dom/client';
import { ThemeProvider } from '@mui/material/styles';
import CssBaseline from '@mui/material/CssBaseline';
// Mock 垫片：无后端时注入模拟 tongtianAPI（开发自测用，需在 App 加载前执行）
import './lib/mock-api';
import App from './App';
import { lightThemeResponsive } from './theme/theme';
import './styles/globals.css';

const rootElement = document.getElementById('root');
if (!rootElement) {
  throw new Error('Root element not found');
}

const root = ReactDOM.createRoot(rootElement);
root.render(
  <React.StrictMode>
    <ThemeProvider theme={lightThemeResponsive}>
      <CssBaseline />
      <App />
    </ThemeProvider>
  </React.StrictMode>
);
