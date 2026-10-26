/**
 * 轻量界面中英切换（不引入 i18n 依赖）
 *
 * 设计取舍：
 * - 中文原文即词典 key，`t('偏好设置')` 在中文下原样返回；
 *   英文缺词时回退中文原文，不会出现空白或 key 泄漏。
 * - 语言存 appSettings.language，设置页修改后所有订阅方立即重渲染。
 * - 只翻译渲染层静态文案；引擎/后端返回的任务内容与错误原文保持原语言。
 * - 变量用 `{name}` 占位，避免中英文语序差异时拼接字符串。
 */

import React, { createContext, useContext, useMemo } from 'react';
import { useAppSettings } from '../lib/use-app-settings';
import { EN_DICT } from './en';

export type UiLanguage = 'zh' | 'en';

interface I18nValue {
  lang: UiLanguage;
  /** 语言对应的 BCP 47 locale（时间/数字格式化用） */
  locale: string;
  /** 中文原文 → 当前语言文案；vars 替换 `{name}` 占位 */
  t: (zh: string, vars?: Record<string, string | number>) => string;
}

const I18nContext = createContext<I18nValue>({
  lang: 'zh',
  locale: 'zh-CN',
  t: (zh) => zh,
});

export function languageOf(value: unknown): UiLanguage {
  return value === 'English' || value === 'en' ? 'en' : 'zh';
}

function interpolate(template: string, vars?: Record<string, string | number>): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (whole, key: string) =>
    Object.prototype.hasOwnProperty.call(vars, key) ? String(vars[key]) : whole,
  );
}

export const I18nProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [appSettings] = useAppSettings();
  const lang = languageOf(appSettings.language);
  const value = useMemo<I18nValue>(() => {
    const locale = lang === 'en' ? 'en-US' : 'zh-CN';
    return {
      lang,
      locale,
      t: (zh, vars) => interpolate(lang === 'en' ? EN_DICT[zh] ?? zh : zh, vars),
    };
  }, [lang]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
};

export function useI18n(): I18nValue {
  return useContext(I18nContext);
}
