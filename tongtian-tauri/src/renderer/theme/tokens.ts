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
 * 导航项专属色板：每项一个「软底色 + 主色」组合
 * - bg: 图标色块的柔和底色
 * - main: 图标与选中态文字颜色
 * - activeBg: 选中态行背景
 */
export const navPalette = {
  blue: { bg: '#E8F1FF', main: '#3B82F6', activeBg: '#E8F1FF' },
  purple: { bg: '#F1EBFF', main: '#8B5CF6', activeBg: '#F1EBFF' },
  pink: { bg: '#FFEDF5', main: '#EC4899', activeBg: '#FFEDF5' },
  orange: { bg: '#FFF1E2', main: '#F97316', activeBg: '#FFF1E2' },
  green: { bg: '#E7F8EF', main: '#10B981', activeBg: '#E7F8EF' },
  cyan: { bg: '#E4F7FB', main: '#06B6D4', activeBg: '#E4F7FB' },
  teal: { bg: '#E3F6F4', main: '#14B8A6', activeBg: '#E3F6F4' },
  amber: { bg: '#FFF6DE', main: '#F59E0B', activeBg: '#FFF6DE' },
  slate: { bg: '#EEF2F7', main: '#64748B', activeBg: '#EEF2F7' },
} as const;

export type NavPaletteKey = keyof typeof navPalette;

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
