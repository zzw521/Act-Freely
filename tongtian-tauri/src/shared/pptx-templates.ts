/**
 * 幻灯片模板的共享契约（设置页 / 工作台 / 浏览器直连共用）
 *
 * 内置主题与 Rust `src-tauri/src/pptx/mod.rs` 的 id/配色保持一致：
 * 桌面端以 Rust 台账为准，浏览器直连没有 Rust 时用这里的定义兜底。
 * 导入的 .pptx 模板只存在于桌面端磁盘，浏览器通道如实不可用。
 */

export interface PptxTheme {
  background: string;
  foreground: string;
  accent: string;
  muted: string;
  surface: string;
  titleFont: string;
  bodyFont: string;
  titleSize: number;
  bodySize: number;
  /** 页面左右留白（英寸） */
  margin: number;
  /** 是否绘制强调色竖条 */
  accentBar: boolean;
}

export interface PptxTemplate {
  id: string;
  name: string;
  description: string;
  builtin: boolean;
  theme?: PptxTheme;
  /** 导入模板的绝对路径（仅本机引擎消费，界面不展示） */
  path?: string;
  size?: number;
  importedAt?: number;
  available: boolean;
  error?: string;
}

export interface PptxTemplatesState {
  templates: PptxTemplate[];
  selectedId: string;
  selected?: PptxTemplate;
  defaultId: string;
  root: string;
  notes: string[];
  importedId?: string;
  deletedId?: string;
}

/** 引擎只消费这一小份：选中项 + 可选模板清单 + 模板根目录 */
export interface PptxTemplatesPayload {
  root: string;
  selectedId: string;
  templates: PptxTemplate[];
  notes?: string[];
}

export const DEFAULT_PPTX_TEMPLATE_ID = 'business-blue';

export const BUILTIN_PPTX_TEMPLATES: PptxTemplate[] = [
  {
    id: 'business-blue',
    name: '商务蓝',
    description: '白底 + 深蓝强调色，适合季度汇报、方案评审',
    builtin: true,
    available: true,
    theme: {
      background: '#FFFFFF',
      foreground: '#1F2937',
      accent: '#2563EB',
      muted: '#64748B',
      surface: '#EFF6FF',
      titleFont: 'Microsoft YaHei',
      bodyFont: 'Microsoft YaHei',
      titleSize: 40,
      bodySize: 18,
      margin: 0.7,
      accentBar: true,
    },
  },
  {
    id: 'minimal-gray',
    name: '简约灰',
    description: '留白更多、无装饰条，适合内部同步与极简风格',
    builtin: true,
    available: true,
    theme: {
      background: '#FFFFFF',
      foreground: '#111827',
      accent: '#374151',
      muted: '#6B7280',
      surface: '#F3F4F6',
      titleFont: 'Segoe UI',
      bodyFont: 'Microsoft YaHei',
      titleSize: 40,
      bodySize: 18,
      margin: 0.7,
      accentBar: false,
    },
  },
  {
    id: 'warm-earth',
    name: '暖棕',
    description: '米白底 + 棕橙强调色，适合培训、复盘与对外分享',
    builtin: true,
    available: true,
    theme: {
      background: '#FFFBF5',
      foreground: '#3F2E24',
      accent: '#B45309',
      muted: '#8A6A56',
      surface: '#FDF3E7',
      titleFont: 'Georgia',
      bodyFont: 'Microsoft YaHei',
      titleSize: 40,
      bodySize: 18,
      margin: 0.7,
      accentBar: true,
    },
  },
];

export function findBuiltinPptxTemplate(id?: string): PptxTemplate {
  return (
    BUILTIN_PPTX_TEMPLATES.find((t) => t.id === id) ??
    BUILTIN_PPTX_TEMPLATES.find((t) => t.id === DEFAULT_PPTX_TEMPLATE_ID)!
  );
}

/**
 * 浏览器直连的模板载荷：没有模板目录与导入文件，只带内置主题。
 * 选中的是导入模板时退回默认内置主题，并在 notes 里说明原因。
 */
export function browserPptxTemplates(selectedId?: string): PptxTemplatesPayload {
  const wanted = (selectedId ?? '').trim();
  const selected = findBuiltinPptxTemplate(wanted);
  const notes =
    wanted && wanted !== selected.id
      ? [`导入模板「${wanted}」只在桌面版可用，本次使用内置主题「${selected.name}」`]
      : undefined;
  return {
    root: '',
    selectedId: selected.id,
    templates: BUILTIN_PPTX_TEMPLATES,
    notes,
  };
}
