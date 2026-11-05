/**
 * WCAG 2.x 对比度算尺（供 tests/unit/appearance 下的契约测试共用）
 *
 * 为什么不是「肉眼看着够」：本仓库有 4 套皮肤 × 亮暗 = 8 个配色组合，
 * 且玻璃皮肤的表面是半透明的（rgba），同一个文字色在不同组合下的对比度能差 2 倍。
 * 所有取值都由这两个测试实算，改色时如果低于阈值会直接红。
 *
 * 阈值口径（WCAG 2.1 AA）：
 * - 1.4.3 正文（<18.66px 或加粗 <24px）：≥4.5:1
 * - 1.4.11 非文本（图形、控件、焦点环）：≥3:1
 */

export interface Rgba {
  r: number;
  g: number;
  b: number;
  a: number;
}

/** #rgb / #rrggbb / rgb() / rgba() → Rgba；无法解析返回 null（渐变、var() 等） */
export function parseColor(input: string): Rgba | null {
  const text = input.trim();
  const hex = text.match(/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/);
  if (hex) {
    const h = hex[1];
    const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h;
    return {
      r: parseInt(full.slice(0, 2), 16),
      g: parseInt(full.slice(2, 4), 16),
      b: parseInt(full.slice(4, 6), 16),
      a: 1,
    };
  }
  const fn = text.match(/^rgba?\(([^)]+)\)$/i);
  if (fn) {
    const parts = fn[1].split(',').map((p) => Number(p.trim()));
    if (parts.length < 3 || parts.some((n) => Number.isNaN(n))) return null;
    const [r, g, b, a] = parts as [number, number, number, number?];
    return { r, g, b, a: a === undefined ? 1 : a };
  }
  return null;
}

/** 半透明前景压在不透明底上：合成出实际渲染色的 RGB（结果 a=1） */
export function composite(fg: Rgba, bg: Rgba): Rgba {
  if (fg.a >= 1) return { ...fg, a: 1 };
  const over = (f: number, b: number) => f * fg.a + b * (1 - fg.a);
  return { r: over(fg.r, bg.r), g: over(fg.g, bg.g), b: over(fg.b, bg.b), a: 1 };
}

function channelLuminance(v: number): number {
  const s = v / 255;
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

export function luminance(c: Rgba): number {
  return (
    0.2126 * channelLuminance(c.r) + 0.7152 * channelLuminance(c.g) + 0.0722 * channelLuminance(c.b)
  );
}

/** 前景/背景对比度（1:1 到 21:1） */
export function contrast(foreground: string, background: string): number {
  const fg = parseColor(foreground);
  const bg = parseColor(background);
  if (!fg || !bg) throw new Error(`无法解析颜色：${foreground} / ${background}`);
  const solidBg = composite(bg, { r: 255, g: 255, b: 255, a: 1 });
  const solidFg = composite(fg, solidBg);
  const l1 = luminance(solidFg);
  const l2 = luminance(solidBg);
  const [hi, lo] = l1 >= l2 ? [l1, l2] : [l2, l1];
  return (hi + 0.05) / (lo + 0.05);
}

export const round = (n: number): string => n.toFixed(2);
