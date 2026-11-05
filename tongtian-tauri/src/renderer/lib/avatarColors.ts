/**
 * 通天办公 - 头像色板（D-25 对比度）
 *
 * 头像字母只有 14px 加粗，按 WCAG 1.4.3 属于普通文字，得守住 4.5:1。
 * 原来的 500 档（#84CC16 白字 1.98:1、#06B6D4 2.43:1）压不住，所以：
 * - 色板只留同色相的 600/700 档，每一支配白字都 ≥4.5:1；
 * - 老用户在设置里存过 500 档，这里映射到同色相的深一档，不然改色板等于把他们的头像留在不合格的那支上；
 * - 字色不写死白色，统一走 inkOn()，将来往色板里加浅色也不会静默退化。
 */

import { inkOn } from './a11y';

/** 可选头像底色（与 Settings → 个人资料 的色板一一对应） */
export const AVATAR_COLORS = [
  '#2563EB', '#0F766E', '#B45309', '#DC2626', '#7C3AED', '#BE185D',
  '#0E7490', '#4D7C0F', '#C2410C', '#4338CA', '#111827',
] as const;

/** 历史 500 档 → 同色相 600/700 档 */
const legacyAvatarColors: Record<string, string> = {
  '#3B82F6': '#2563EB',
  '#14B8A6': '#0F766E',
  '#F59E0B': '#B45309',
  '#EF4444': '#DC2626',
  '#8B5CF6': '#7C3AED',
  '#EC4899': '#BE185D',
  '#06B6D4': '#0E7490',
  '#84CC16': '#4D7C0F',
  '#F97316': '#C2410C',
  '#6366F1': '#4338CA',
};

/** 把已保存的头像色收敛到合格档；无法识别（含渐变）时原样返回 */
export function normalizeAvatarColor(raw: string | null | undefined): string {
  if (!raw) return '';
  return legacyAvatarColors[raw.trim().toUpperCase()] ?? raw;
}

/** 头像字母的字色 */
export function avatarInk(background: string | null | undefined): string {
  return inkOn(normalizeAvatarColor(background));
}
