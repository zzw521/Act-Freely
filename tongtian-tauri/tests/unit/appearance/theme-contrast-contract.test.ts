/**
 * 皮肤对比度契约（D-25 · WCAG 1.4.3 / 1.4.11）
 *
 * 钉住三件事：
 * 1. globals.css 里 8 个配色组合（4 套皮肤 × 亮暗）的文字四档，对该组合全部 7 张表面都 ≥4.5:1，
 *    且逐档单调（正文比辅助文字更黑/更亮），不会出现「四档其实三档」。
 * 2. 焦点环、链接、强调色（当文字/图标用的那几支）在各自皮肤下达到阈值。
 * 3. tokens.ts 里给组件用的色板（navPalette / textSafePalette / darkTextSafePalette / darkAccent）
 *    实算过，且 theme.ts 真的按亮暗换了档，「选中态」在半透明底上也不会退化成看不见的字。
 * 4. 头像色板：头像字母只有 14px 加粗，按 WCAG 属普通文字（≥4.5:1），所以每一支可选底色
 *    都要和 inkOn() 选出来的字色成对算过；历史上存过 500 档的用户由 normalizeAvatarColor 收敛。
 *
 * 半透明表面（玻璃皮肤）不是拿 rgba 字面值直接算，而是先按同块的 --tt-bg 合成，
 * 因为 CSS 里 `background: rgba(...)` 压在的正是那一层的底色。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  densityScales,
  darkAccent,
  darkContrastText,
  darkTextSafePalette,
  navPalette,
  textSafePalette,
  withAlpha,
} from '../../../src/renderer/theme/tokens';
import { composite, contrast, parseColor, round, type Rgba } from './wcag';
import { AVATAR_COLORS, avatarInk, normalizeAvatarColor } from '../../../src/renderer/lib/avatarColors';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const CSS_PATH = '../../../src/renderer/styles/globals.css';

interface TokenBlock {
  /** 逗号分开的选择器，每个都是 :root 或 html[data-x='y'] 形式 */
  selectors: string[];
  decls: Map<string, string>;
  order: number;
}

/** 解析出「只声明 CSS 变量」的顶层规则块，忽略 @media/@layer/组件选择器 */
function tokenBlocks(text: string): TokenBlock[] {
  const clean = text.replace(/\/\*[\s\S]*?\*\//g, '');
  const out: TokenBlock[] = [];
  let i = 0;
  let order = 0;
  while (i < clean.length) {
    const open = clean.indexOf('{', i);
    if (open < 0) break;
    /* 选择器取这一段最后一行：前面的 `@tailwind base;` 之类的语句行不算选择器 */
    const sel = clean
      .slice(i, open)
      .trim()
      .split('\n')
      .pop()!
      .trim();
    let depth = 1;
    let j = open + 1;
    while (j < clean.length && depth > 0) {
      if (clean[j] === '{') depth++;
      else if (clean[j] === '}') depth--;
      j++;
    }
    const body = clean.slice(open + 1, j - 1);
    if (!sel.startsWith('@') && !sel.includes(' ')) {
      const decls = new Map<string, string>();
      for (const m of body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) decls.set(m[1], m[2].trim());
      if (decls.size > 0 && /^(:root|html\[[^)]+\])$/.test(sel)) {
        out.push({ selectors: sel.split(',').map((s) => s.trim()), decls, order: order++ });
      }
    }
    i = j;
  }
  return out;
}

/** 选择器里要求的 data-* 条件；:root 视为无条件（属性名去掉 data- 前缀，与 resolve 的 env 对齐） */
function requires(selector: string): Array<[string, string]> {
  const attrs = [...selector.matchAll(/\[data-([\w-]+)=['"]?([^'"\]]+)['"]?\]/g)];
  return attrs.map((m) => [m[1], m[2]] as [string, string]);
}

type Mode = 'light' | 'dark';
type Style = 'default' | 'glass' | 'classic' | 'parchment';

const COMBOS: Array<{ name: string; mode: Mode; style: Style }> = [
  { name: '默认·亮色', mode: 'light', style: 'default' },
  { name: '默认·暗色', mode: 'dark', style: 'default' },
  { name: '玻璃·亮色', mode: 'light', style: 'glass' },
  { name: '玻璃·暗色', mode: 'dark', style: 'glass' },
  { name: '经典·亮色', mode: 'light', style: 'classic' },
  { name: '经典·暗色', mode: 'dark', style: 'classic' },
  { name: '羊皮纸·亮色', mode: 'light', style: 'parchment' },
  { name: '羊皮纸·暗色', mode: 'dark', style: 'parchment' },
];

/** 按 CSS 层叠（特异性优先，其次源码顺序）算出某个组合下每个变量的最终取值 */
function resolve(blocks: TokenBlock[], mode: Mode, style: Style): Map<string, string> {
  const env: Record<string, string> = {
    theme: mode === 'dark' ? 'dark' : 'light',
    'ui-style': style,
    density: 'standard',
    'chat-font': 'sans',
  };
  const matching = blocks
    .filter((b) =>
      b.selectors.some((sel) => {
        const req = requires(sel);
        if (sel !== ':root' && !sel.startsWith('html[')) return false;
        return req.every(([attr, value]) => env[attr] === value);
      }),
    )
    /* 特异性：属性选择器数量多的胜出；平手时后面写的覆盖前面 */
    .sort((a, b) => {
      const sa = Math.max(...a.selectors.map((s) => requires(s).length));
      const sb = Math.max(...b.selectors.map((s) => requires(s).length));
      return sa !== sb ? sa - sb : a.order - b.order;
    });
  const vars = new Map<string, string>();
  for (const block of matching) for (const [k, v] of block.decls) vars.set(k, v);
  return vars;
}

const SURFACE_VARS = [
  '--tt-bg',
  '--tt-surface',
  '--tt-surface-2',
  '--tt-surface-3',
  '--tt-chat-assistant-bg',
  '--tt-chat-process-bg',
  '--tt-chat-code-bg',
  /* chip 底（徽章/选中行/状态块）：组件里以前写死 #F3F4F6、#DCFCE7 这类亮色浅底，
     配 var(--tt-text-*) 的文字翻到暗色就是浅字压浅底（实测 1.3:1）。换成令牌后，
     底本身也得能被四档文字与语义色压得住，所以一起进表面清单。 */
  '--tt-chip-neutral',
  '--tt-chip-info',
  '--tt-chip-success',
  '--tt-chip-teal',
  '--tt-chip-warning',
  '--tt-chip-danger',
  '--tt-chip-accent',
] as const;

const TEXT_VARS = ['--tt-text-1', '--tt-text-2', '--tt-text-3', '--tt-text-4'] as const;

/** 语义色里「承载文字」的那一档：报错/成功/警告正文用它们，不用 500 档的原色 */
const SEMANTIC_TEXT_VARS = ['--tt-danger', '--tt-success', '--tt-warning', '--tt-info', '--tt-teal'] as const;

const WHITE: Rgba = { r: 255, g: 255, b: 255, a: 1 };
const BLACK: Rgba = { r: 0, g: 0, b: 0, a: 1 };

/** 把变量值解析成不透明实色：半透明先压在该组合的 --tt-bg 上 */
function solid(vars: Map<string, string>, name: string, backdrop: string): string {
  const raw = vars.get(name);
  expect(raw, `${name} 未在任何皮肤块中声明`).toBeTruthy();
  const parsed = parseColor(raw!);
  expect(parsed, `${name}: ${raw} 不是可计算的颜色（渐变/var() 需另案处理）`).toBeTruthy();
  if (parsed!.a >= 1) return raw!;
  const bg = parseColor(backdrop);
  expect(bg, `${name} 的背底 ${backdrop} 不可解析`).toBeTruthy();
  const over = composite(parsed!, bg!);
  return `rgb(${Math.round(over.r)}, ${Math.round(over.g)}, ${Math.round(over.b)})`;
}

describe('globals.css 八套配色的文字对比度', () => {
  const blocks = tokenBlocks(read(CSS_PATH));

  it('块数与解析正常（4 套皮肤各有亮暗两块 + 默认两块）', () => {
    expect(blocks.length).toBeGreaterThanOrEqual(8);
    for (const combo of COMBOS) {
      const vars = resolve(blocks, combo.mode, combo.style);
      expect(vars.size, `${combo.name} 没解析出变量`).toBeGreaterThan(20);
    }
  });

  for (const combo of COMBOS) {
    it(`${combo.name}：文字四档对全部表面 ≥4.5:1`, () => {
      const vars = resolve(blocks, combo.mode, combo.style);
      const bg = solid(vars, '--tt-bg', vars.get('--tt-bg')!);
      for (const surface of SURFACE_VARS) {
        const over = solid(vars, surface, bg);
        for (const text of TEXT_VARS) {
          const fg = solid(vars, text, over);
          const c = contrast(fg, over);
          expect(
            c,
            `${combo.name} ${text}(${fg}) on ${surface}(${over}) = ${round(c)}:1 < 4.5:1`,
          ).toBeGreaterThanOrEqual(4.5);
        }
      }
    });

    it(`${combo.name}：四档逐档单调，不做「三档假分层」`, () => {
      const vars = resolve(blocks, combo.mode, combo.style);
      const bg = solid(vars, '--tt-bg', vars.get('--tt-bg')!);
      const over = solid(vars, '--tt-surface', bg);
      const ratios = TEXT_VARS.map((t) => contrast(solid(vars, t, over), over));
      for (let i = 1; i < ratios.length; i++) {
        expect(
          ratios[i] <= ratios[i - 1] + 0.02,
          `${combo.name} 第 ${i + 1} 档比上一档更显眼了：${ratios.map(round).join(' → ')}`,
        ).toBe(true);
        /* 相邻两档至少差 0.4：肉眼要看得出「这是次要文字」而不是「这是同档抖了一下」 */
        expect(
          ratios[i - 1] - ratios[i],
          `${combo.name} 第 ${i} 与第 ${i + 1} 档没有层次：${ratios.map(round).join(' → ')}`,
        ).toBeGreaterThanOrEqual(0.4);
      }
    });

    it(`${combo.name}：焦点环 ≥3:1（1.4.11），跳转链接边框同色`, () => {
      const vars = resolve(blocks, combo.mode, combo.style);
      const bg = solid(vars, '--tt-bg', vars.get('--tt-bg')!);
      /* --tt-teal 也是焦点信号：输入卡聚焦时贴着边沿长实心一圈它（蓝环跟卡片打架，
         用户反馈后换的色）。当焦点指示用就得按 1.4.11 的 ≥3:1 算，不能只当品牌色放行。 */
      for (const token of ['--tt-focus', '--tt-teal']) {
        for (const surface of SURFACE_VARS) {
          const over = solid(vars, surface, bg);
          const focus = solid(vars, token, over);
          const c = contrast(focus, over);
          expect(c, `${combo.name} ${token} ${focus} on ${surface} = ${round(c)}:1`).toBeGreaterThanOrEqual(3);
        }
      }
    });

    it(`${combo.name}：语义色当文字用（报错/成功/警告文案）≥4.5:1`, () => {
      const vars = resolve(blocks, combo.mode, combo.style);
      const bg = solid(vars, '--tt-bg', vars.get('--tt-bg')!);
      for (const token of SEMANTIC_TEXT_VARS) {
        for (const surface of SURFACE_VARS) {
          const over = solid(vars, surface, bg);
          const fg = solid(vars, token, over);
          const c = contrast(fg, over);
          expect(c, `${combo.name} ${token}(${fg}) on ${surface}(${over}) = ${round(c)}:1`).toBeGreaterThanOrEqual(4.5);
        }
      }
    });

    it(`${combo.name}：链接与品牌强调色按正文用 ≥4.5:1`, () => {
      const vars = resolve(blocks, combo.mode, combo.style);
      const bg = solid(vars, '--tt-bg', vars.get('--tt-bg')!);
      for (const token of ['--tt-link', '--tt-accent', '--tt-accent-violet']) {
        for (const surface of ['--tt-bg', '--tt-surface', '--tt-surface-2', '--tt-surface-3']) {
          const over = solid(vars, surface, bg);
          const fg = solid(vars, token, over);
          const c = contrast(fg, over);
          expect(c, `${combo.name} ${token}(${fg}) on ${surface}(${over}) = ${round(c)}:1`).toBeGreaterThanOrEqual(4.5);
        }
      }
    });
  }

  it('经典皮肤暗色自己声明四档文字（否则黑字压黑底 1.0:1）', () => {
    const classicDark = blocks.find((b) => b.selectors.some((s) => /data-theme='dark'.*data-ui-style='classic'|data-ui-style='classic'.*data-theme='dark'/.test(s)));
    expect(classicDark, '缺少 html[data-theme=dark][data-ui-style=classic] 块').toBeTruthy();
    for (const t of TEXT_VARS) expect(classicDark!.decls.has(t), `经典暗色未覆盖 ${t}`).toBe(true);
  });
});

describe('实心填充（按钮/徽标）上的白字 ≥4.5:1', () => {
  const blocks = tokenBlocks(read(CSS_PATH));
  const FILL_VARS = [
    '--tt-danger-fill',
    '--tt-teal-fill',
    '--tt-accent-fill',
    '--tt-accent-fill-hover',
    '--tt-warning-fill',
  ] as const;
  for (const combo of COMBOS) {
    it(`${combo.name}`, () => {
      const vars = resolve(blocks, combo.mode, combo.style);
      const bg = solid(vars, '--tt-bg', vars.get('--tt-bg')!);
      for (const fill of FILL_VARS) {
        const over = solid(vars, fill, bg);
        /* fill 档只在 :root 与暗色块里声明，皮肤未覆盖时回落到默认块 */
        const c = contrast('#FFFFFF', over);
        expect(c, `${combo.name} ${fill}(${over}) 上的白字 = ${round(c)}:1`).toBeGreaterThanOrEqual(4.5);
      }
    });
  }
});

describe('主操作按钮（--tt-cta-fill / -hover / -ink）成对换色', () => {
  const blocks = tokenBlocks(read(CSS_PATH));
  const CTA_VARS = ['--tt-cta-fill', '--tt-cta-fill-hover', '--tt-cta-ink'] as const;

  /* 这颗按钮是唯一「底会翻面」的填充：亮色黑底白字，暗色浅底深字。
     组件里漏写 color 时靠 MUI palette 的 contrastText 兜底，所以令牌必须跟
     palette 的 contrastText 同档——下面第二条把它钉住，改色板时不会静默错位。 */
  for (const combo of COMBOS) {
    it(`${combo.name}：墨压在主操作底与 hover 底上都 ≥4.5:1，按钮对页面边界 ≥3:1`, () => {
      const vars = resolve(blocks, combo.mode, combo.style);
      const bg = solid(vars, '--tt-bg', vars.get('--tt-bg')!);
      for (const v of CTA_VARS) expect(vars.has(v), `${combo.name} 缺少 ${v}`).toBe(true);
      const ink = solid(vars, '--tt-cta-ink', bg);
      for (const fillName of ['--tt-cta-fill', '--tt-cta-fill-hover'] as const) {
        const over = solid(vars, fillName, bg);
        const c = contrast(ink, over);
        expect(c, `${combo.name} ${ink} on ${fillName}(${over}) = ${round(c)}:1`).toBeGreaterThanOrEqual(4.5);
        /* 1.4.11：按钮是个可定位的控件，主操作底不能跟页面底糊成一块 */
        for (const surface of ['--tt-bg', '--tt-surface', '--tt-surface-2']) {
          const page = solid(vars, surface, bg);
          const edge = contrast(over, page);
          expect(
            edge,
            `${combo.name} ${fillName}(${over}) 对 ${surface}(${page}) = ${round(edge)}:1`,
          ).toBeGreaterThanOrEqual(3);
        }
      }
    });
  }

  it('主操作墨色与 MUI palette 的 contrastText 同档（组件漏写 color 时才不会错位）', () => {
    const light = resolve(blocks, 'light', 'default');
    const dark = resolve(blocks, 'dark', 'default');
    expect(solid(light, '--tt-cta-ink', solid(light, '--tt-bg', light.get('--tt-bg')!)).toUpperCase()).toBe('#FFFFFF');
    expect(solid(dark, '--tt-cta-ink', solid(dark, '--tt-bg', dark.get('--tt-bg')!)).toUpperCase()).toBe(
      darkContrastText.toUpperCase(),
    );
  });
});

describe('组件色板实算（tokens.ts）', () => {
  const darkSurfaces = {
    '--tt-bg': '#0f172a',
    '--tt-surface': '#1e293b',
    '--tt-surface-2': '#24324a',
    '--tt-surface-3': '#2b3a55',
  };
  const lightSurfaces = {
    '--tt-bg': '#f7f8fa',
    '--tt-surface': '#ffffff',
    '--tt-surface-2': '#f9fafb',
    '--tt-surface-3': '#f3f4f6',
  };

  it('textSafePalette 的 main 承载白字 ≥4.5:1（按钮实心填充用）', () => {
    for (const [name, set] of Object.entries(textSafePalette)) {
      expect(contrast('#FFFFFF', set.main), `${name}.main ${set.main} 放白字不够`).toBeGreaterThanOrEqual(4.5);
      expect(contrast(set.main, '#FFFFFF'), `${name}.main 当文字用不够`).toBeGreaterThanOrEqual(4.5);
    }
  });

  /* MUI 的 palette 只有亮暗两档、不感知皮肤，所以暗色档要按四套暗色皮肤的全部表面算。
     原来两套色板共用 textSafePalette 的 700 档，组件里 color="success" 的绿字翻到暗色
     只剩 2.9:1（#15803D on #1E293B 实扫），报错红字同理；暗色档改走 darkTextSafePalette。 */
  for (const combo of COMBOS.filter((c) => c.mode === 'dark')) {
    it(`${combo.name}：darkTextSafePalette 的 main 当文字用在全部表面上 ≥4.5:1`, () => {
      const cssBlocks = tokenBlocks(read(CSS_PATH));
      const vars = resolve(cssBlocks, 'dark', combo.style);
      const bg = solid(vars, '--tt-bg', vars.get('--tt-bg')!);
      for (const [name, set] of Object.entries(darkTextSafePalette)) {
        for (const surface of SURFACE_VARS) {
          const over = solid(vars, surface, bg);
          expect(
            contrast(set.main, over),
            `${combo.name} palette.${name}.main ${set.main} on ${surface}(${over}) = ${round(
              contrast(set.main, over),
            )}:1`,
          ).toBeGreaterThanOrEqual(4.5);
        }
      }
    });
  }

  it('darkTextSafePalette 的三档做实心底时压得住深色文字（暗色按钮与 hover 态）', () => {
    for (const [name, set] of Object.entries(darkTextSafePalette)) {
      for (const tier of ['main', 'light', 'dark'] as const) {
        expect(
          contrast(darkContrastText, set[tier]),
          `${name}.${tier} ${set[tier]} 上的 ${darkContrastText} = ${round(
            contrast(darkContrastText, set[tier]),
          )}:1`,
        ).toBeGreaterThanOrEqual(4.5);
        /* 描边/图标/进度条这类非文本用途按 1.4.11 要求 ≥3:1，深底页与卡片都要够 */
        for (const surfaceHex of Object.values(darkSurfaces)) {
          expect(
            contrast(set[tier], surfaceHex),
            `${name}.${tier} ${set[tier]} on ${surfaceHex} 作图形用`,
          ).toBeGreaterThanOrEqual(3);
        }
      }
    }
  });

  it('theme.ts 真的按配色模式换档（否则组件里的 color="success" 又回到一支蜡烛照两端）', async () => {
    const { createAppTheme } = await import('../../../src/renderer/theme/theme');
    for (const [name, set] of Object.entries(darkTextSafePalette)) {
      const dark = createAppTheme('dark').palette[name as 'success'];
      expect(dark.main, `暗色 palette.${name}.main 没取暗色档`).toBe(set.main);
      expect(dark.contrastText, `暗色 palette.${name} 还在用白字`).toBe(darkContrastText);
      const light = createAppTheme('light').palette[name as 'success'];
      expect(light.main, `亮色 palette.${name}.main 被暗色档带跑`).toBe(
        textSafePalette[name as 'success'].main,
      );
      expect(light.contrastText).toBe('#FFFFFF');
    }
  });

  it('MUI 的次要文字与 --tt-text-3 同档（两套栈不同档时，实扫会读到 4.48:1 的擦边不合格）', async () => {
    const { createAppTheme } = await import('../../../src/renderer/theme/theme');
    const cssBlocks = tokenBlocks(read(CSS_PATH));
    for (const mode of ['light', 'dark'] as const) {
      const vars = resolve(cssBlocks, mode, 'default');
      expect(
        createAppTheme(mode).palette.text.secondary.toUpperCase(),
        `${mode} 档 MUI text.secondary 与 globals.css 的 --tt-text-3 分叉了`,
      ).toBe(vars.get('--tt-text-3')!.toUpperCase());
    }
  });

  it('navPalette 亮色：text 压在自己的柔和底与行底上都 ≥4.5:1', () => {
    for (const [name, c] of Object.entries(navPalette)) {
      for (const surface of [c.bg, c.activeBg, ...Object.values(lightSurfaces)]) {
        expect(
          contrast(c.text, surface),
          `nav ${name}.text ${c.text} on ${surface} = ${round(contrast(c.text, surface))}:1`,
        ).toBeGreaterThanOrEqual(4.5);
      }
      /* 选中态实心块：图标是白色 */
      expect(contrast('#FFFFFF', c.text), `nav ${name} 选中块上的白色图标`).toBeGreaterThanOrEqual(3);
    }
  });

  it('navPalette 暗色：darkMain 在半透明同色相行底/色块上 ≥4.5:1', () => {
    for (const [name, c] of Object.entries(navPalette)) {
      for (const [surfaceName, surfaceHex] of Object.entries(darkSurfaces)) {
        const bg = parseColor(surfaceHex)!;
        const rowOn = composite(parseColor(withAlpha(c.main, 0.18))!, bg);
        const chipOn = composite(parseColor(withAlpha(c.main, 0.28))!, bg);
        const idleChip = composite(parseColor(withAlpha(c.main, 0.16))!, bg);
        for (const [where, over] of [
          ['行底', rowOn],
          ['选中色块', chipOn],
          ['未选中色块', idleChip],
        ] as const) {
          const rgb = `rgb(${Math.round(over.r)}, ${Math.round(over.g)}, ${Math.round(over.b)})`;
          expect(
            contrast(c.darkMain, rgb),
            `nav ${name}.darkMain ${c.darkMain} 在暗色${where}(${surfaceName}) 上 = ${round(contrast(c.darkMain, rgb))}:1`,
          ).toBeGreaterThanOrEqual(4.5);
        }
      }
    }
  });

  it('darkAccent 在暗色四张表面上都读得清', () => {
    for (const [name, hex] of Object.entries(darkAccent)) {
      for (const [surfaceName, surfaceHex] of Object.entries(darkSurfaces)) {
        expect(contrast(hex, surfaceHex), `darkAccent.${name} on ${surfaceName}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it('withAlpha 输出可被 CSS 与算尺同时解析', () => {
    expect(withAlpha('#3B82F6', 0.18)).toBe('rgba(59, 130, 246, 0.18)');
    expect(parseColor(withAlpha('#3B82F6', 0.5))).toBeTruthy();
  });

  it('密度三档：tokens 的缩放因子与 CSS 变量方向一致', () => {
    expect(Object.keys(densityScales)).toEqual(['标准', '紧凑', '宽松']);
    expect(densityScales['紧凑']).toBeLessThan(densityScales['标准']);
    expect(densityScales['宽松']).toBeGreaterThan(densityScales['标准']);
    const text = read(CSS_PATH);
    for (const [attr, key] of [
      ['compact', '紧凑'],
      ['roomy', '宽松'],
    ] as const) {
      const block = text.match(new RegExp(`html\\[data-density='${attr}'\\]\\s*\\{([^}]*)\\}`));
      expect(block, `globals.css 缺少 data-density='${attr}' 块`).toBeTruthy();
      const scale = Number(block![1].match(/--tt-density-scale:\s*([\d.]+)/)![1]);
      expect(scale, `data-density='${attr}' 与 densityScales['${key}'] 不同步`).toBeCloseTo(
        densityScales[key],
        3,
      );
      expect(block![1]).toMatch(/--tt-row-pad-y:/);
    }
  });
});

describe('头像色板（Settings → 个人资料 可选底色 × 字母字色）', () => {
  it('每一支可选底色配上 inkOn 选出的字色后 ≥4.5:1', () => {
    for (const c of AVATAR_COLORS) {
      const ink = avatarInk(c);
      expect(contrast(ink, c), `${c} 配 ${ink} 只有 ${round(contrast(ink, c))}:1`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it('历史 500 档不再直接用：normalizeAvatarColor 把它们收到同色相的深一档', () => {
    const legacy = ['#3B82F6', '#14B8A6', '#F59E0B', '#EF4444', '#8B5CF6', '#EC4899', '#06B6D4', '#84CC16', '#F97316', '#6366F1'];
    for (const c of legacy) {
      const resolved = normalizeAvatarColor(c);
      expect(resolved, `${c} 没有被收敛`).not.toBe(c);
      const ink = avatarInk(c);
      expect(contrast(ink, resolved), `${c}→${resolved} 配 ${ink}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it('未设置头像色时的默认渐变，两端都压得住白字（渐变按最亮那一档算）', () => {
    const stops = [...read('../../../src/renderer/components/Layout/Sidebar.tsx').matchAll(/background: avatarColor \|\| 'linear-gradient\(([^)]*)\)/g)]
      .flatMap((m) => m[1].match(/#[0-9a-fA-F]{6}/g) ?? []);
    expect(stops.length).toBeGreaterThan(1);
    for (const s of stops) {
      expect(contrast('#FFFFFF', s), `默认头像渐变里的 ${s}`).toBeGreaterThanOrEqual(4.5);
    }
  });
});

describe('黑白底都算得出的极端情形（自检算尺本身）', () => {
  it('#111827 on #ffffff ≈ 17:1，#FFFFFF on #000000 = 21:1', () => {
    expect(contrast('#111827', '#ffffff')).toBeGreaterThan(15);
    expect(contrast('#FFFFFF', '#000000')).toBeCloseTo(21, 0);
    expect(BLACK.a).toBe(1);
    expect(WHITE.r).toBe(255);
  });
});
