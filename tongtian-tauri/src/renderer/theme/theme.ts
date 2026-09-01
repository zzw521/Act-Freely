/**
 * 通天办公 - MUI Theme 配置
 *
 * 设计规范 v2.0（参考 AionUi 风格）
 * - 品牌绿主色 + 石板灰中性色
 * - 柔和阴影、大圆角、低饱和背景
 */

import { createTheme, responsiveFontSizes, type Theme } from '@mui/material/styles';
import { primaryColors, secondaryColors, accentColors, semanticColors } from './tokens';
import type { PaletteMode } from '@mui/material';

// ── 主题创建 ──────────────────────────────────────────────────────

/**
 * 创建主题配置
 * @param mode - 配色模式 ('light' | 'dark')
 */
export function createAppTheme(mode: PaletteMode = 'light'): Theme {
  const isDark = mode === 'dark';

  return createTheme({
    palette: {
      mode,
      primary: {
        main: primaryColors[600],
        light: primaryColors[400],
        dark: primaryColors[700],
        contrastText: '#FFFFFF',
      },
      secondary: {
        main: secondaryColors[500],
        light: secondaryColors[300],
        dark: secondaryColors[700],
        contrastText: '#FFFFFF',
      },
      warning: {
        main: accentColors[500],
        light: accentColors[300],
        dark: accentColors[700],
      },
      error: {
        main: semanticColors.error,
        light: '#FCA5A5',
        dark: '#B91C1C',
      },
      success: {
        main: semanticColors.success,
        light: '#86EFAC',
        dark: '#15803D',
      },
      info: {
        main: semanticColors.info,
        light: '#7DD3FC',
        dark: '#0369A1',
      },
      background: {
        default: isDark ? '#0F172A' : semanticColors.background,
        paper: isDark ? '#1E293B' : semanticColors.surface,
      },
      text: {
        primary: isDark ? '#F1F5F9' : semanticColors.gray[900],
        secondary: isDark ? '#94A3B8' : semanticColors.gray[500],
        disabled: isDark ? '#475569' : semanticColors.gray[300],
      },
      divider: isDark ? 'rgba(148, 163, 184, 0.12)' : semanticColors.gray[200],
    },

    typography: {
      fontFamily: [
        'PingFang SC',
        'Hiragino Sans GB',
        'Microsoft YaHei',
        '-apple-system',
        'BlinkMacSystemFont',
        'Segoe UI',
        'Roboto',
        'sans-serif',
      ].join(','),
      h1: { fontSize: '56px', fontWeight: 700, lineHeight: 1.2, letterSpacing: '-0.02em' },
      h2: { fontSize: '40px', fontWeight: 700, lineHeight: 1.25, letterSpacing: '-0.01em' },
      h3: { fontSize: '32px', fontWeight: 600, lineHeight: 1.3, letterSpacing: '-0.01em' },
      h4: { fontSize: '24px', fontWeight: 600, lineHeight: 1.35 },
      h5: { fontSize: '20px', fontWeight: 600, lineHeight: 1.4 },
      h6: { fontSize: '16px', fontWeight: 600, lineHeight: 1.5 },
      body1: { fontSize: '14px', lineHeight: 1.6 },
      body2: { fontSize: '13px', lineHeight: 1.55 },
      subtitle1: { fontSize: '15px', fontWeight: 500, lineHeight: 1.6 },
      subtitle2: { fontSize: '13px', fontWeight: 500, lineHeight: 1.5 },
      caption: { fontSize: '12px', fontWeight: 400, lineHeight: 1.5 },
      overline: { fontSize: '11px', fontWeight: 600, letterSpacing: '0.08em', textTransform: 'uppercase' },
    },

    shape: {
      borderRadius: 12,
    },

    spacing: 4,

    components: {
      // ── 按钮：胶囊感 + 品牌绿 ─────────────────────────────
      MuiButton: {
        defaultProps: {
          disableElevation: true,
          disableRipple: true,
        },
        styleOverrides: {
          root: {
            textTransform: 'none',
            borderRadius: '10px',
            fontWeight: 600,
            padding: '8px 18px',
            transition: 'all 0.18s ease',
          },
          containedPrimary: {
            boxShadow: '0 1px 2px rgba(22, 163, 74, 0.2)',
            '&:hover': {
              boxShadow: '0 4px 12px rgba(22, 163, 74, 0.28)',
              transform: 'translateY(-1px)',
            },
          },
          sizeLarge: {
            height: '44px',
            fontSize: '15px',
            borderRadius: '12px',
          },
        },
      },

      // ── 图标按钮 ──────────────────────────────────────────
      MuiIconButton: {
        defaultProps: {
          disableRipple: true,
        },
        styleOverrides: {
          root: {
            borderRadius: '10px',
            transition: 'background-color 0.15s ease, color 0.15s ease',
          },
        },
      },

      // ── 卡片：大圆角 + 柔和阴影 + 细边框 ──────────────────
      MuiCard: {
        styleOverrides: {
          root: {
            borderRadius: '16px',
            border: `1px solid ${isDark ? 'rgba(148,163,184,0.12)' : semanticColors.gray[200]}`,
            boxShadow: '0 1px 3px rgba(15,23,42,.05)',
          },
        },
      },

      MuiPaper: {
        styleOverrides: {
          rounded: {
            borderRadius: '16px',
          },
        },
      },

      // ── 输入框 ────────────────────────────────────────────
      MuiTextField: {
        defaultProps: {
          variant: 'outlined',
          size: 'medium',
        },
      },

      MuiOutlinedInput: {
        styleOverrides: {
          root: {
            borderRadius: '12px',
            '&:hover .MuiOutlinedInput-notchedOutline': {
              borderColor: isDark ? 'rgba(148,163,184,0.4)' : semanticColors.gray[300],
            },
            '&.Mui-focused .MuiOutlinedInput-notchedOutline': {
              borderWidth: '1.5px',
            },
          },
        },
      },

      // ── 对话框 ────────────────────────────────────────────
      MuiDialog: {
        styleOverrides: {
          paper: {
            borderRadius: '20px',
            boxShadow: '0 24px 64px rgba(15,23,42,.18)',
          },
        },
      },

      // ── 标签芯片：胶囊形 ──────────────────────────────────
      MuiChip: {
        styleOverrides: {
          root: {
            fontWeight: 500,
            borderRadius: '8px',
          },
        },
      },

      // ── 顶栏：无边框毛玻璃 ────────────────────────────────
      MuiAppBar: {
        defaultProps: {
          elevation: 0,
        },
        styleOverrides: {
          root: {
            borderBottom: `1px solid ${isDark ? 'rgba(148,163,184,0.12)' : semanticColors.gray[200]}`,
            backdropFilter: 'blur(12px)',
          },
        },
      },

      // ── 抽屉 ──────────────────────────────────────────────
      MuiDrawer: {
        styleOverrides: {
          paper: {
            borderRight: `1px solid ${isDark ? 'rgba(148,163,184,0.12)' : semanticColors.gray[200]}`,
          },
        },
      },

      // ── 选项卡 ────────────────────────────────────────────
      MuiTabs: {
        styleOverrides: {
          indicator: {
            backgroundColor: primaryColors[600],
            borderRadius: '2px',
          },
        },
      },

      MuiTab: {
        styleOverrides: {
          root: {
            textTransform: 'none',
            fontWeight: 500,
            minHeight: '44px',
            '&.Mui-selected': {
              color: primaryColors[600],
            },
          },
        },
      },

      // ── 表格 ──────────────────────────────────────────────
      MuiTableCell: {
        styleOverrides: {
          head: {
            fontWeight: 600,
            color: isDark ? '#CBD5E1' : semanticColors.gray[600],
            backgroundColor: isDark ? 'rgba(148,163,184,0.06)' : semanticColors.gray[50],
            borderBottom: `1px solid ${semanticColors.gray[200]}`,
          },
          root: {
            borderBottom: `1px solid ${isDark ? 'rgba(148,163,184,0.1)' : semanticColors.gray[100]}`,
          },
        },
      },

      // ── 提示条 ────────────────────────────────────────────
      MuiAlert: {
        styleOverrides: {
          root: {
            borderRadius: '12px',
          },
          standardSuccess: {
            backgroundColor: isDark ? 'rgba(34,197,94,0.12)' : primaryColors[50],
            color: isDark ? '#86EFAC' : primaryColors[700],
          },
          standardWarning: {
            backgroundColor: isDark ? 'rgba(245,158,11,0.12)' : accentColors[50],
            color: isDark ? '#FCD34D' : accentColors[700],
          },
          standardError: {
            backgroundColor: isDark ? 'rgba(239,68,68,0.12)' : '#FEF2F2',
            color: isDark ? '#FCA5A5' : '#B91C1C',
          },
          standardInfo: {
            backgroundColor: isDark ? 'rgba(14,165,233,0.12)' : secondaryColors[50],
            color: isDark ? '#7DD3FC' : secondaryColors[700],
          },
        },
      },

      // ── 工具提示 ──────────────────────────────────────────
      MuiTooltip: {
        styleOverrides: {
          tooltip: {
            borderRadius: '8px',
            fontSize: '12px',
            backgroundColor: isDark ? '#334155' : semanticColors.gray[800],
          },
        },
      },

      // ── 菜单 ──────────────────────────────────────────────
      MuiMenu: {
        styleOverrides: {
          paper: {
            borderRadius: '14px',
            boxShadow: '0 8px 32px rgba(15,23,42,.14)',
            border: `1px solid ${isDark ? 'rgba(148,163,184,0.12)' : semanticColors.gray[100]}`,
          },
        },
      },

      MuiMenuItem: {
        styleOverrides: {
          root: {
            borderRadius: '8px',
            margin: '2px 8px',
            padding: '8px 12px',
            fontSize: '14px',
            '&.Mui-selected': {
              backgroundColor: isDark ? 'rgba(34,197,94,0.12)' : primaryColors[50],
            },
          },
        },
      },
    },
  });
}

// ── 导出默认主题 ──────────────────────────────────────────────────

export const lightTheme = createAppTheme('light');
export const darkTheme = createAppTheme('dark');

// 自动响应字体大小
export const lightThemeResponsive = responsiveFontSizes(lightTheme);
export const darkThemeResponsive = responsiveFontSizes(darkTheme);
