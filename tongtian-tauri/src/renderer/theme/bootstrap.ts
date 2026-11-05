/**
 * 通天办公 - 主题首帧引导（D-25）
 *
 * 把偏好档位翻成 <html> 上的 data-* 属性，并给出环境底色。
 * ThemedApp 的同步写在 effect 里（渲染之后），暗色偏好会先白一下；
 * CSP 只允许 'self'，index.html 塞不进内联脚本，所以入口模块先跑一次 applyDocumentTheme。
 */

export type Mode = 'light' | 'dark';

/** 设置里的中文档位 → CSS 属性值（属性值用英文，避免选择器里的中文转义） */
export const UI_STYLE_ATTR: Record<string, string> = {
  默认: 'default',
  玻璃: 'glass',
  经典: 'classic',
  羊皮纸: 'parchment',
};

export const CHAT_FONT_ATTR: Record<string, string> = {
  无衬线: 'sans',
  衬线: 'serif',
};

export const DENSITY_ATTR: Record<string, string> = {
  标准: 'standard',
  紧凑: 'compact',
  宽松: 'roomy',
};

export function resolveMode(pref: string): Mode {
  if (pref === '暗色') return 'dark';
  if (pref === '亮色') return 'light';
  // 跟随系统
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : 'light';
}

/** 环境底色：皮肤不只是改组件表面，页面留白与工作台底也要一起变，否则像贴了一张半透明纸 */
export function bodyBackground(mode: Mode, style: string): string {
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

export interface DocumentThemeInput {
  mode: Mode;
  uiStyle: string;
  density: string;
  chatFont: string;
}

/** 把四个档位写到 <html> 上并铺好底色（effect 里每次变化都调它，首帧由入口调一次） */
export function applyDocumentTheme({ mode, uiStyle, density, chatFont }: DocumentThemeInput): void {
  const root = document.documentElement;
  root.setAttribute('data-theme', mode);
  root.setAttribute('data-ui-style', UI_STYLE_ATTR[uiStyle] ?? 'default');
  root.setAttribute('data-density', DENSITY_ATTR[density] ?? 'standard');
  root.setAttribute('data-chat-font', CHAT_FONT_ATTR[chatFont] ?? 'sans');
  document.body.style.background = bodyBackground(mode, uiStyle);
}
