/**
 * 通天办公 - 响应式主题容器
 *
 * 订阅偏好设置中的「主题亮暗」「界面密度」与「界面风格」「对话字体」：
 * - 亮暗与系统配色：实时切换 MUI 主题
 * - 界面密度：写 data-density 并重建主题（spacing 因子随之缩放）
 * - 界面风格：写 data-ui-style，四套皮肤的 CSS 变量与环境底色随之切换
 * - 对话字体：写 data-chat-font，只影响对话正文/输入，不改变设置页字体
 *
 * data-* 与底色的落盘动作在 theme/bootstrap.ts，入口首帧也调一次，避免暗色偏好先白一下。
 */

import React, { useEffect, useMemo, useState } from 'react';
import { ThemeProvider } from '@mui/material/styles';
import CssBaseline from '@mui/material/CssBaseline';
import { createAppTheme } from './theme';
import { applyDocumentTheme, resolveMode, type Mode } from './bootstrap';
import { densityScales, type Density } from './tokens';
import { usePreferenceStore } from '../stores/usePreferenceStore';
import { useAppSettings } from '../lib/use-app-settings';

const asDensity = (v: string): Density => (v in densityScales ? (v as Density) : '标准');

const ThemedApp: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const themePref = usePreferenceStore((s) => s.theme);
  const densityPref = usePreferenceStore((s) => s.density);
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

  const density = asDensity(densityPref);
  const theme = useMemo(() => createAppTheme(mode, density), [mode, density]);

  // 同步根节点配色（body 背景随主题与皮肤切换）
  useEffect(() => {
    applyDocumentTheme({ mode, uiStyle, density, chatFont });
  }, [mode, uiStyle, chatFont, density]);

  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      {children}
    </ThemeProvider>
  );
};

export default ThemedApp;
