/**
 * 模式画布的产物选择（纯逻辑，便于单测）
 *
 * 每种模式只认自己那一类交付物：设计看 HTML 原型，幻灯片看 pptx/HTML 稿，
 * 写作看 Markdown/文本。没有匹配产物时画布如实空着，不拿无关文件或假内容填充。
 */

import type { FileOp, WorkMode } from '../../shared/types';
import { FileOpStatus } from '../../shared/types';

export type CanvasKind = 'html' | 'markdown' | 'text' | 'external';

/** 各模式可上画布的产物扩展名（小写，不含点） */
const MODE_CANVAS_EXTS: Record<Exclude<WorkMode, 'general'>, string[]> = {
  design: ['html', 'htm'],
  slides: ['pptx', 'html', 'htm'],
  writing: ['md', 'markdown', 'txt'],
};

export const MODE_CANVAS_LABELS: Record<WorkMode, string> = {
  general: '任务监控',
  design: '设计画布',
  slides: '幻灯片画布',
  writing: '写作画布',
};

export const MODE_CANVAS_EMPTY: Record<Exclude<WorkMode, 'general'>, string> = {
  design: '当前会话还没有 HTML 原型。设计模式生成的单文件页面会在这里预览。',
  slides: '当前会话还没有幻灯片产物。生成的 .pptx 或 HTML 稿会在这里出现。',
  writing: '当前会话还没有 Markdown 文档。写作模式产出的长文会在这里预览。',
};

function extOf(path: string): string {
  const name = path.split(/[/\\]/).pop() ?? '';
  const dot = name.lastIndexOf('.');
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : '';
}

/** 扩展名 → 画布渲染方式；非文本产物交给系统默认应用 */
export function canvasKindOf(path: string): CanvasKind {
  const ext = extOf(path);
  if (ext === 'html' || ext === 'htm') return 'html';
  if (ext === 'md' || ext === 'markdown') return 'markdown';
  if (ext === 'txt') return 'text';
  return 'external';
}

/**
 * 按模式挑选可预览产物，保持交付顺序并去重。
 * 只收成功写出的文件：执行中/失败/回滚的产物没有可信内容可展示。
 */
export function pickCanvasArtifacts(mode: WorkMode, fileOps: FileOp[]): FileOp[] {
  if (mode === 'general') return [];
  const exts = MODE_CANVAS_EXTS[mode];
  const seen = new Set<string>();
  const picked: FileOp[] = [];
  for (const op of fileOps) {
    if (op.status !== FileOpStatus.SUCCESS) continue;
    const path = (op.srcPath ?? '').trim();
    if (!path || !exts.includes(extOf(path))) continue;
    const key = path.replace(/\\/g, '/');
    if (seen.has(key)) continue;
    seen.add(key);
    picked.push(op);
  }
  return picked;
}
