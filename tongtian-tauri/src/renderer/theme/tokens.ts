/**
 * 通天办公 - 设计令牌 (Design Tokens)
 * 
 * 设计规范 v2.0 (参考 AionUi 风格)
 * - 色彩系统: 品牌绿 (#16A34A) + 中性灰 + 语义色
 * - 圆角: 16px (大卡片) / 12px (中组件) / 10px (小组件)
 * - 阴影: 低饱和度柔和阴影
 * - 字体: PingFang SC / Hiragino Sans GB / Microsoft YaHei
 */

// ── 色彩系统 ──────────────────────────────────────────────────────

/**
 * 主色调 - 品牌绿 (#16A34A)
 * 用于主要操作按钮、链接、高亮元素（AionUi 风格）
 */
export const primaryColors = {
  50: '#F0FDF4',
  100: '#DCFCE7',
  200: '#BBF7D0',
  300: '#86EFAC',
  400: '#4ADE80',
  500: '#22C55E',
  600: '#16A34A',
  700: '#15803D',
  800: '#166534',
  900: '#14532D',
} as const;

/**
 * 辅助色 - 天空蓝 (#0EA5E9)
 * 用于信息提示、次要操作、链接
 */
export const secondaryColors = {
  50: '#F0F9FF',
  100: '#E0F2FE',
  200: '#BAE6FD',
  300: '#7DD3FC',
  400: '#38BDF8',
  500: '#0EA5E9',
  600: '#0284C7',
  700: '#0369A1',
  800: '#075985',
  900: '#0C4A6E',
} as const;

/**
 * 强调色 - 暖琥珀 (#F59E0B)
 * 用于警告、待办、需要关注的元素
 */
export const accentColors = {
  50: '#FFFBEB',
  100: '#FEF3C7',
  200: '#FDE68A',
  300: '#FCD34D',
  400: '#FBBF24',
  500: '#F59E0B',
  600: '#D97706',
  700: '#B45309',
  800: '#92400E',
  900: '#78350F',
} as const;

/**
 * 语义色彩
 */
export const semanticColors = {
  success: '#22C55E',
  warning: '#F59E0B',
  error: '#EF4444',
  info: '#0EA5E9',
  // 中性灰阶（偏冷的石板灰，更现代）
  gray: {
    50: '#F8FAFC',
    100: '#F1F5F9',
    200: '#E2E8F0',
    300: '#CBD5E1',
    400: '#94A3B8',
    500: '#64748B',
    600: '#475569',
    700: '#334155',
    800: '#1E293B',
    900: '#0F172A',
  },
  // 背景色
  surface: '#FFFFFF',
  background: '#F7F8FA',
  sidebar: '#F2F4F7',
  overlay: 'rgba(15, 23, 42, 0.45)',
} as const;

// ── 导航多彩配色系统（轻快多彩风格）──────────────────────────────

/**
 * 导航项专属色板：每项一组「亮色 / 暗色」可用的颜色
 * - bg: 亮色下图标色块的柔和底色
 * - main: 亮色下色块填充（选中态的实心块）
 * - activeBg: 亮色下选中态行背景
 * - text: 亮色下的文字/线性图标色（同色相加深，压在柔和底上 ≥4.5:1）
 * - darkMain: 暗色下的文字与图标色（同色相提亮；暗色底用半透明同色相，不用这里的实色块）
 *
 * 原来只有 main 一档：亮色下 #10B981 这类中间亮度压不住 4.5:1 的文字对比，
 * 暗色下更是直接把同一支中间色压在深色底上（最低 2.2:1），换肤等于没换。
 *
 * text 一律取同色相 700 档，唯独 amber 提到 800 档：#A16207 压在最浅的表面色
 * #F3F4F6 上只有 4.47:1，差的那 0.03 没法靠调底色绕开（羊皮纸也要用同一支）。
 */
export const navPalette = {
  blue: { bg: '#E8F1FF', main: '#3B82F6', activeBg: '#E8F1FF', text: '#1D4ED8', darkMain: '#93C5FD' },
  purple: { bg: '#F1EBFF', main: '#8B5CF6', activeBg: '#F1EBFF', text: '#6D28D9', darkMain: '#C4B5FD' },
  pink: { bg: '#FFEDF5', main: '#EC4899', activeBg: '#FFEDF5', text: '#BE185D', darkMain: '#F9A8D4' },
  orange: { bg: '#FFF1E2', main: '#F97316', activeBg: '#FFF1E2', text: '#C2410C', darkMain: '#FDBA74' },
  green: { bg: '#E7F8EF', main: '#10B981', activeBg: '#E7F8EF', text: '#047857', darkMain: '#6EE7B7' },
  cyan: { bg: '#E4F7FB', main: '#06B6D4', activeBg: '#E4F7FB', text: '#0E7490', darkMain: '#67E8F9' },
  teal: { bg: '#E3F6F4', main: '#14B8A6', activeBg: '#E3F6F4', text: '#0F766E', darkMain: '#5EEAD4' },
  amber: { bg: '#FFF6DE', main: '#F59E0B', activeBg: '#FFF6DE', text: '#92400E', darkMain: '#FCD34D' },
  slate: { bg: '#EEF2F7', main: '#64748B', activeBg: '#EEF2F7', text: '#475569', darkMain: '#CBD5E1' },
} as const;

export type NavPaletteKey = keyof typeof navPalette;

/** #RRGGBB → rgba()：暗色皮肤上用同色相半透明底，避免浅底色块翻成刺眼白斑 */
export function withAlpha(hex: string, alpha: number): string {
  const h = hex.replace('#', '');
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

// ── 圆角系统 ──────────────────────────────────────────────────────

export const borderRadius = {
  none: '0',
  sm: '6px',
  md: '10px',
  lg: '12px',      // 中组件圆角
  xl: '16px',      // 大卡片圆角
  '2xl': '20px',
  full: '9999px',
} as const;

// ── 阴影系统 ──────────────────────────────────────────────────────

export const shadows = {
  none: 'none',
  sm: '0 1px 3px rgba(15,23,42,.05)',
  md: '0 2px 8px rgba(15,23,42,.06)',
  lg: '0 4px 16px rgba(15,23,42,.07)',     // 标准卡片阴影
  xl: '0 8px 28px rgba(15,23,42,.09)',
  '2xl': '0 12px 48px rgba(15,23,42,.12)',
  inner: 'inset 0 1px 3px rgba(15,23,42,.04)',
} as const;

// ── 间距系统 ──────────────────────────────────────────────────────

export const spacing = {
  0: '0',
  1: '4px',
  2: '8px',
  3: '12px',
  4: '16px',
  5: '20px',
  6: '24px',
  8: '32px',
  10: '40px',
  12: '48px',
  16: '64px',
} as const;

// ── 字体系统 ──────────────────────────────────────────────────────

export const fontFamily = {
  sans: [
    'PingFang SC',
    'Hiragino Sans GB',
    'Microsoft YaHei',
    '-apple-system',
    'BlinkMacSystemFont',
    'Segoe UI',
    'Roboto',
    'Helvetica Neue',
    'Arial',
    'sans-serif',
  ].join(', '),
  mono: [
    'SF Mono',
    'SFMono-Regular',
    'Consolas',
    'Liberation Mono',
    'Menlo',
    'Courier New',
    'monospace',
  ].join(', '),
};

export const fontSize = {
  xs: ['12px', { lineHeight: '16px' }],
  sm: ['14px', { lineHeight: '20px' }],
  base: ['16px', { lineHeight: '24px' }],
  lg: ['18px', { lineHeight: '28px' }],
  xl: ['20px', { lineHeight: '28px' }],
  '2xl': ['24px', { lineHeight: '32px' }],
  '3xl': ['30px', { lineHeight: '36px' }],
  '4xl': ['36px', { lineHeight: '44px' }],
} as const;

export const fontWeight = {
  normal: '400',
  medium: '500',
  semibold: '600',
  bold: '700',
} as const;

// ── 断点系统 (响应式) ────────────────────────────────────────────

export const breakpoints = {
  sm: '640px',
  md: '768px',
  lg: '1024px',
  xl: '1280px',
  '2xl': '1536px',
} as const;

// ── 无障碍与密度（D-25）────────────────────────────────────────

/**
 * 承载文字的 main 一律取色阶 700 档：品牌 600 档（#16A34A）压白字只有 3.30:1，
 * 达不到 WCAG 1.4.3 的 4.5:1；700 档（#15803D）有 5.02:1，色相只深一档。
 * 600 档保留给图形、描边、选中底色这类不承载正文的用途（1.4.11 只要求 3:1）。
 * 每个数值的对比度都由 tests/unit/appearance/theme-contrast-contract.test.ts 实算复核。
 */
export const textSafePalette = {
  primary: { main: '#15803D', light: '#16A34A', dark: '#166534' },
  secondary: { main: '#0369A1', light: '#0284C7', dark: '#075985' },
  success: { main: '#15803D', light: '#16A34A', dark: '#166534' },
  warning: { main: '#B45309', light: '#D97706', dark: '#92400E' },
  error: { main: '#DC2626', light: '#EF4444', dark: '#B91C1C' },
  info: { main: '#0369A1', light: '#0284C7', dark: '#075985' },
} as const;

/** 暗色下压在 --tt-surface(#1E293B) 上要 readable 的强调色 */
export const darkAccent = {
  link: '#4ADE80',
  selectedText: '#86EFAC',
} as const;

/**
 * 暗色下的同一份色板：700 档压深底只有 2.9–3.6:1（#15803D on #1E293B 实测 2.92:1），
 * 「color=success 的绿字」「红色报错」翻到暗色就读不清；这里取同色相的 300–400 档，
 * 在四套暗色皮肤的全部表面上重新 ≥4.5:1。
 * main 与 globals.css html[data-theme='dark'] 的 --tt-success/info/warning/danger 同一档，
 * 两套词汇（CSS 变量与 MUI palette）不分叉，组件用哪边翻深色都一样。
 */
export const darkTextSafePalette = {
  primary: { main: '#4ADE80', light: '#86EFAC', dark: '#22C55E' },
  secondary: { main: '#7DD3FC', light: '#BAE6FD', dark: '#38BDF8' },
  success: { main: '#4ADE80', light: '#86EFAC', dark: '#22C55E' },
  warning: { main: '#FCD34D', light: '#FDE68A', dark: '#FBBF24' },
  error: { main: '#FCA5A5', light: '#FECACA', dark: '#F87171' },
  info: { main: '#7DD3FC', light: '#BAE6FD', dark: '#38BDF8' },
} as const;

/**
 * 暗色的 main 是浅色，实心按钮/填充徽标上的文字不能再给白色（#FCA5A5 上的白字只有 1.5:1），
 * 改成深底页的同族深色。CSS 令牌那边另有 --tt-*-fill 一档继续走「深底 + 白字」，
 * 两条路各自达标（MUI 侧由 theme-contrast-contract 的暗色组实算）。
 */
export const darkContrastText = '#0F172A';

/** 界面密度：theme.spacing 的因子，MUI 侧所有 sx 的 p/m/gap 一并缩放 */
export const densityScales = {
  标准: 1,
  紧凑: 0.85,
  宽松: 1.15,
} as const;

export type Density = keyof typeof densityScales;

// ── 过渡动画 ──────────────────────────────────────────────────────

export const transitions = {
  fast: '150ms ease-in-out',
  normal: '200ms ease-in-out',
  slow: '300ms ease-in-out',
} as const;

export const transitionProperty = {
  colors: 'color, background-color, border-color',
  shadows: 'box-shadow',
  opacity: 'opacity',
  transform: 'transform',
} as const;
