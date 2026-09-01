/**
 * 通天办公 - 响应式主题容器
 *
 * 订阅偏好设置中的「主题亮暗」（亮色/暗色/跟随系统），实时切换 MUI 主题，
 * 使偏好设置的主题选项真实生效（此前为固定亮色主题，点击无变化）。
 */

import React, { useEffect, useMemo, useState } from 'react';
import { ThemeProvider } from '@mui/material/styles';
import CssBaseline from '@mui/material/CssBaseline';
import { createAppTheme } from './theme';
import { usePreferenceStore } from '../stores/usePreferenceStore';

type Mode = 'light' | 'dark';

function resolveMode(pref: string): Mode {
  if (pref === '暗色') return 'dark';
  if (pref === '亮色') return 'light';
  // 跟随系统
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : 'light';
}

const ThemedApp: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const themePref = usePreferenceStore((s) => s.theme);
  const [mode, setMode] = useState<Mode>(() => resolveMode(themePref));

  // 偏好变化 / 系统配色变化时切换
  useEffect(() => {
    setMode(resolveMode(themePref));
    if (themePref === '跟随系统' && window.matchMedia) {
      const mq = window.matchMedia('(prefers-color-scheme: dark)');
      const handler = (e: MediaQueryListEvent) => setMode(e.matches ? 'dark' : 'light');
      mq.addEventListener?.('change', handler);
      return () => mq.removeEventListener?.('change', handler);
    }
    return undefined;
  }, [themePref]);

  const theme = useMemo(() => createAppTheme(mode), [mode]);

  // 同步根节点配色（body 背景随主题切换）
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', mode);
    document.body.style.backgroundColor = mode === 'dark' ? '#0F172A' : '#F7F8FA';
  }, [mode]);

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      {children}
    </ThemeProvider>
  );
};

export default ThemedApp;
