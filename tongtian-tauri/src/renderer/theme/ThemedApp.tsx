/**
 * 通天办公 - 响应式主题容器
 *
 * 订阅偏好设置中的「主题亮暗」（亮色/暗色/跟随系统）与「界面风格」「对话字体」：
 * - 亮暗与系统配色：实时切换 MUI 主题
 * - 界面风格：写 data-ui-style，四套皮肤的 CSS 变量与环境底色随之切换
 * - 对话字体：写 data-chat-font，只影响对话正文/输入，不改变设置页字体
 */

import React, { useEffect, useMemo, useState } from 'react';
import { ThemeProvider } from '@mui/material/styles';
import CssBaseline from '@mui/material/CssBaseline';
import { createAppTheme } from './theme';
import { usePreferenceStore } from '../stores/usePreferenceStore';
import { useAppSettings } from '../lib/use-app-settings';

type Mode = 'light' | 'dark';

function resolveMode(pref: string): Mode {
  if (pref === '暗色') return 'dark';
  if (pref === '亮色') return 'light';
  // 跟随系统
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : 'light';
}

/** 设置里的中文档位 → CSS 属性值（属性值用英文，避免选择器里的中文转义） */
const UI_STYLE_ATTR: Record<string, string> = {
  默认: 'default',
  玻璃: 'glass',
  经典: 'classic',
  羊皮纸: 'parchment',
};

const CHAT_FONT_ATTR: Record<string, string> = {
  无衬线: 'sans',
  衬线: 'serif',
};

/** 环境底色：皮肤不只是改组件表面，页面留白与工作台底也要一起变，否则像贴了一张半透明纸 */
function bodyBackground(mode: Mode, style: string): string {
  if (style === '玻璃') {
    return mode === 'dark'
      ? 'linear-gradient(135deg, #0b1220 0%, #101a2e 48%, #1a1024 100%)'
      : 'linear-gradient(135deg, #eef7f6 0%, #f6f8ff 48%, #fdf2f8 100%)';
  }
  if (style === '经典') {
    return mode === 'dark' ? '#111827' : '#f0f0f0';
  }
  if (style === '羊皮纸') {
    return mode === 'dark'
      ? 'radial-gradient(circle at 18% 12%, #2d241a 0%, #241d16 55%, #1e1812 100%)'
      : 'radial-gradient(circle at 18% 12%, #fbf3e3 0%, #f3e8d2 55%, #eddfc4 100%)';
  }
  return mode === 'dark' ? '#0F172A' : '#F7F8FA';
}

const ThemedApp: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const themePref = usePreferenceStore((s) => s.theme);
  const [mode, setMode] = useState<Mode>(() => resolveMode(themePref));
  const [appSettings] = useAppSettings();

  const uiStyle = (appSettings.uiStyle as string) ?? '默认';
  const chatFont = (appSettings.chatFont as string) ?? '无衬线';

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

  // 同步根节点配色（body 背景随主题与皮肤切换）
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', mode);
    document.documentElement.setAttribute('data-ui-style', UI_STYLE_ATTR[uiStyle] ?? 'default');
    document.documentElement.setAttribute('data-chat-font', CHAT_FONT_ATTR[chatFont] ?? 'sans');
    document.body.style.background = bodyBackground(mode, uiStyle);
  }, [mode, uiStyle, chatFont]);

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      {children}
    </ThemeProvider>
  );
};

export default ThemedApp;
