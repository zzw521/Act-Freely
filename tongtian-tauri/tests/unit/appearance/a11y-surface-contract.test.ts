/**
 * 键盘路径 / 读屏名字 / 焦点可见的表面契约（D-25 · WCAG 2.1.1 / 2.4.7 / 4.1.2 / 2.4.1）
 *
 * 这一份是「源码级」验收：不跑浏览器，但把最容易回退的几类结构性缺陷钉死——
 * 1. 没有可读名字的 IconButton（读屏只会念「按钮」）；
 * 2. 用 Box/div 拼出来、只绑了 onClick 的可点表面（鼠标能点，键盘 Tab 都到不了）；
 * 3. 没有名字的 Dialog（打开后读屏不知道自己在哪个弹窗里）；
 * 4. `outline: none` 把焦点环抹掉（唯一豁免：复合控件写 `焦点环由容器承担`，
 *    且同文件必须真有容器侧的 --tt-focus / --tt-teal 环，见「豁免不是后门」那条用例）；
 *    复合控件内层那条环不是靠组件里 `outline: none` 抹的——全局规则特异性 (3,0,0)
 *    （:is 跟最 specific 的参数走），Emotion 单类名打不过它；靠 globals.css 里的
 *    `data-tt-ring` 点名排除，见「点名排除不是后门」那条用例；
 * 5. 跳转链接、全局焦点环、减动效、密度链路这些一次性写好就容易被误删的东西；
 * 6. 写在 `color:` 槽位上的硬编码十六进制正文色——它不随皮肤变，所以要求它在
 *    亮色最浅表面与暗色三档表面上都 ≥4.5:1（同一段样式里写死的实心底色除外）；
 * 7. 反向的一半：不随皮肤翻的浅色实心「面」（卡片 / 面板 / 状态色块 / 状态色板里的 bg 字段）。
 *    底不跟皮肤翻，子元素文字却用 var(--tt-text-*)，暗色下就是白卡压浅字（实测 1.1:1）。
 *    这条按「左边最近的样式键」认角色，整文件扫（含三元与模块级色板），亮度 ≥0.7 一律记账；
 *    渲染外部内容本体的地方（生成的网页、文档、画布、品牌底板）在样式块里写 `亮色孤岛` 注释豁免。
 * 8. 第 7 项的同一条路上反过来的那一半：把「主操作底」写死成中性近黑（#111827 这类，
 *    三通道彼此相差 ≤12 且亮度 ≤0.12）。它在亮色下是对的，暗色下这颗按钮就化成页面底；
 *    带彩度的深色品牌底（靛蓝 #4338CA、紫 #6D28D9）与本机画面画布（#0B1220）是刻意不翻的，
 *    不在这条的口径里——它们配的是白字，两种模式都读得出来。改用 --tt-cta-fill/-ink 令牌。
 * 9. 第 8 条看不见的那一类：`linear-gradient` 面底。前面几条只认实心十六进制，渐变整条被跳过，
 *    于是「靛蓝→青」这种两端都配白字的按钮一直漏网（#06B6D4 压白字实测 2.43:1）。
 *    这条要求渐变里每个色标对白字都 ≥4.5:1——文字压在色标最亮那一档上，只看第一档等于没看。
 *    页面/皮肤背景（globals.css、theme/bootstrap.ts）配的是随皮肤变的令牌字，不在这条口径里；
 *    品牌字标（WebkitBackgroundClip: text，渐变就是字本身，属 WCAG 的 Logotype 豁免）与
 *    「封面渐变只当兜底、文字压在暗色遮罩上」的推荐案例卡（样式块里写 `遮罩` 注释）跳过。
 *
 * 2-9 项的 ratchet 现在上限全是 0：历史尾巴曾记在 BASELINE 里，随无人引用的死代码顶栏整文件删除后归零，
 * 任何一类重新出现一处就红。将来若确有正当豁免（如渲染外部内容本体的底板），在源码里写注释标记放行，
 * 而不是把 BASELINE 改大。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { EN_DICT } from '../../../src/renderer/i18n/en';
import { contrast, luminance, parseColor } from './wcag';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const RENDERER_DIR = fileURLToPath(new URL('../../../src/renderer', import.meta.url));

function tsxFiles(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry === 'dist') continue;
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) {
      /* theme/ 是色板本体（tokens.ts 里的浅底是「配了深色文字的调色板」，
         由 theme-contrast-contract 按数值实算），表面契约只看组件与数据映射 */
      if (path.relative(RENDERER_DIR, full).split(path.sep).join('/') === 'theme') continue;
      tsxFiles(full, out);
    } else if (/\.tsx$|\.ts$/.test(entry)) out.push(full);
  }
  return out;
}

const SOURCES = tsxFiles(RENDERER_DIR).map((f) => ({
  rel: path.relative(RENDERER_DIR, f).split(path.sep).join('/'),
  text: readFileSync(f, 'utf8').replace(/\r\n/g, '\n'),
}));

/** 取从 `<` 开始的完整 JSX 开标签（配平 {} 与 ()，跳过字符串） */
function openTag(text: string, startIdx: number): string | null {
  let i = startIdx + 1;
  let depthBrace = 0;
  let depthParen = 0;
  let quote: string | null = null;
  for (; i < text.length; i++) {
    const ch = text[i];
    if (quote) {
      if (ch === '\\') i++;
      else if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === '`') quote = ch;
    else if (ch === '{') depthBrace++;
    else if (ch === '}') depthBrace--;
    else if (ch === '(') depthParen++;
    else if (ch === ')') depthParen--;
    else if (ch === '>' && depthBrace === 0 && depthParen === 0) return text.slice(startIdx, i + 1);
  }
  return null;
}

const lineOf = (text: string, index: number): number => text.slice(0, index).split('\n').length;

/** 图形控件：它们的 color 属于 1.4.11（3:1），不按正文的 4.5:1 要求 */
const GRAPHIC_OWNER = /(^|\.)(Icon|IconButton|([A-Z][\w]*Icon)|CircularProgress|LinearProgress|Slider|Switch|Progress|Sparkline)$/;

/** 亮色最浅的表面与暗色三档表面：写死的正文色必须在两侧都读得出来，否则换肤必糊 */
const LIGHT_SURFACE = '#F3F4F6';
const DARK_SURFACES = ['#0F172A', '#1E293B', '#24324A'];

/** 表达式自己按亮暗分支取色（isDarkNav / mode === 'dark'），不受这条约束 */
const DARK_AWARE = /(isDark|darkMode|mode === 'dark'|dark \?|darkMode)/;

/** 取 `color:` 槽位里的表达式（配平引号与括号，遇到 , ; } 结束），多行三元也能拿全 */
function colorSlots(text: string): { expr: string; index: number }[] {
  const out: { expr: string; index: number }[] = [];
  for (const m of text.matchAll(/(?<![\w-])color:\s*/g)) {
    const start = m.index + m[0].length;
    let i = start;
    let round = 0;
    let square = 0;
    let brace = 0;
    let quote: string | null = null;
    for (; i < text.length && i - start < 400; i++) {
      const ch = text[i];
      if (quote) {
        if (ch === '\\') i++;
        else if (ch === quote) quote = null;
        continue;
      }
      if (ch === '"' || ch === "'" || ch === '`') quote = ch;
      else if (ch === '(') round++;
      else if (ch === ')') {
        if (!round) break;
        round--;
      } else if (ch === '[') square++;
      else if (ch === ']') {
        if (!square) break;
        square--;
      } else if (ch === '{') brace++;
      else if (ch === '}') {
        if (!brace) break;
        brace--;
      } else if ((ch === ',' || ch === ';') && !round && !square && !brace) break;
    }
    out.push({ expr: text.slice(start, i), index: m.index });
  }
  return out;
}

/** 近白：那是实心色块上的白字，底色由同一段样式里的 bgcolor 决定，不按页面表面算 */
function isNearWhite(hex: string): boolean {
  const c = parseColor(hex);
  return !!c && luminance(c) > 0.85;
}

type SlotRole = 'fill' | 'text' | 'border';

/**
 * 按「左边最近的样式键」判断一个十六进制干什么用：bgcolor/background/bg 是面底，
 * color/fg 是文字，border 与 outline 是描边，渐变里的算装饰不记账。
 *
 * 只按 `sx={{ }}` 整块扫会漏掉两类真实写法：三元的另一个分支
 * （`bgcolor: ok ? '#ECFDF5' : '#F3F4F6'`）和模块级色板（状态→前景/底 的映射表）。
 * 实扫就是这么漏掉 246 处浅底深字、暗色下一片白卡的。
 */
function roleAt(text: string, index: number): SlotRole | null {
  const lineStart = text.lastIndexOf('\n', index) + 1;
  const prefix = text.slice(lineStart, index).replace(/\s+/g, ' ');
  if (/gradient\s*\(/.test(prefix)) return null;
  const hits = [
    ...prefix.matchAll(
      /(bgcolor|backgroundColor|background|borderColor|borderBottom|borderTop|borderLeft|borderRight|border|outline|color|fg|bg)\s*:/g,
    ),
  ];
  if (!hits.length) return null;
  const last = hits[hits.length - 1];
  if (/[;,{}]/.test(prefix.slice((last.index ?? 0) + last[0].length))) return null;
  const key = last[1];
  if (/^border|^outline/.test(key)) return 'border';
  if (key === 'color' || key === 'fg') return 'text';
  return 'fill';
}

/** 某行上下 6 行内有没有豁免标记（渲染外部内容本体的「亮色孤岛」） */
function exemptWindow(text: string, index: number): string {
  const line = lineOf(text, index);
  const lines = text.split('\n');
  return lines.slice(Math.max(0, line - 7), line + 6).join('\n');
}

/** 暗色皮肤上的「白卡片」门槛：亮度 ≥0.7 的实心底（浅粉彩也算，不只近白） */
const PALE = 0.7;

/**
 * 「主操作底」被写死成中性近黑的门槛：亮度 ≤0.12 且三通道彼此相差 ≤12。
 * 加彩度上限是为了放过刻意的深色品牌底（靛蓝/紫）与本机画面画布——它们配白字，
 * 两种模式都读得出来；而 #111827 这种中性黑在暗色皮肤里跟页面底几乎同色。
 */
function isNeutralDarkFill(hex: string): boolean {
  const c = parseColor(hex);
  if (!c || luminance(c) > 0.12) return false;
  const [hi, lo] = [Math.max(c.r, c.g, c.b), Math.min(c.r, c.g, c.b)];
  return hi - lo <= 12;
}

/**
 * 从 `<Name …>` 开标签下标走到该元素的闭合处，返回子树结束下标（拿不到返回 -1）。
 * 自闭合算「没有子树」；嵌套同名元素按深度配平，注释与字符串里的假标签不在这条的口径里。
 */
function subtreeEnd(text: string, tagStart: number): number {
  const open = openTag(text, tagStart);
  if (!open) return -1;
  const name = /^<([\w.]+)/.exec(open)?.[1];
  if (!name) return -1;
  const after = tagStart + open.length;
  if (open.endsWith('/>')) return after;
  const re = new RegExp(`<(/?)${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?:[^<>*]|"[^"]*"|'[^']*')*>`, 'g');
  re.lastIndex = after;
  let depth = 1;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text))) {
    if (m[1] === '/') {
      depth--;
      if (depth === 0) return m.index;
    } else if (!m[0].endsWith('/>')) {
      depth++;
    }
  }
  return -1;
}

/** 从 `{` 下标开始取配平的块源码（跳过字符串），用于按 sx/style 整块分析 */
function balancedBlock(text: string, openIdx: number): string | null {
  let i = openIdx;
  let depth = 0;
  let quote: string | null = null;
  for (; i < text.length; i++) {
    const ch = text[i];
    if (quote) {
      if (ch === '\\') i++;
      else if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === '`') quote = ch;
    else if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) return text.slice(openIdx, i + 1);
    }
  }
  return null;
}

interface Findings {
  unnamedIconButton: Map<string, number>;
  deadClickSurface: Map<string, number>;
  unnamedDialog: Map<string, number>;
  outlineNone: Map<string, number>;
  lowContrastTextColor: Map<string, number>;
  hardFillWithTokenText: Map<string, number>;
  lightIslandSurface: Map<string, number>;
  darkIslandFill: Map<string, number>;
  darkIslandWithTokenText: Map<string, number>;
  whiteInkOnGradient: Map<string, number>;
}

function bump(map: Map<string, number>, key: string): void {
  map.set(key, (map.get(key) ?? 0) + 1);
}

function scan(): Findings {
  const found: Findings = {
    unnamedIconButton: new Map(),
    deadClickSurface: new Map(),
    unnamedDialog: new Map(),
    outlineNone: new Map(),
    lowContrastTextColor: new Map(),
    hardFillWithTokenText: new Map(),
    lightIslandSurface: new Map(),
    darkIslandFill: new Map(),
    darkIslandWithTokenText: new Map(),
    whiteInkOnGradient: new Map(),
  };
  for (const { rel, text } of SOURCES) {
    for (const m of text.matchAll(/<IconButton[\s\n]/g)) {
      const tag = openTag(text, m.index);
      if (!tag) continue;
      if (!/aria-label|aria-labelledby|\{\.\.\./.test(tag)) bump(found.unnamedIconButton, rel);
    }
    for (const m of text.matchAll(/<(Box|div|span|Typography)[^>]*?onClick\s*=/gs)) {
      const tag = openTag(text, m.index);
      if (!tag) continue;
      if (/component=["']button["']/.test(tag)) continue;
      /* 容器托管键盘的列表项/菜单项/页签：有自己的 role 语义，不要求各自带 onKeyDown */
      if (/role=["'](option|menuitem|tab|treeitem|checkbox|switch|radio)["']/.test(tag)) continue;
      const usesHelper = /\.\.\.\s*(clickableProps|disclosureProps|anchorMenuProps)/.test(tag);
      const hasRole = usesHelper || /role=["']button["']/.test(tag);
      const hasKey = usesHelper || /onKeyDown|onKeyPress|onKeyUp/.test(tag);
      if (!(hasRole && hasKey)) bump(found.deadClickSurface, rel);
    }
    for (const m of text.matchAll(/<Dialog[\s\n]/g)) {
      const tag = openTag(text, m.index);
      if (!tag) continue;
      if (/aria-label|ModalProps/.test(tag)) continue;
      const body = text.slice(m.index, m.index + 3000);
      if (!/<DialogTitle|role=["']dialog["']/.test(body)) bump(found.unnamedDialog, rel);
    }
    for (const m of text.matchAll(/outline:\s*'?none'?(?![\s\S]{0,80}:focus-visible)/g)) {
      /* 豁免「焦点环由容器承担」：复合控件（输入卡）里环画在内层文本控件上会落在
         容器里面，成一层「框中框」。这类只允许把环搬到容器外沿，不许直接抹掉——
         同文件必须真的有一条容器侧的 --tt-focus / --tt-teal 环，由下方「豁免不是后门」用例核对。 */
      if (exemptWindow(text, m.index).includes('焦点环由容器承担')) continue;
      bump(found.outlineNone, rel + ':' + lineOf(text, m.index));
    }
    for (const { expr, index } of colorSlots(text)) {
      const lits = [...expr.matchAll(/'(#[0-9a-fA-F]{6})'/g)].map((x) => x[1]);
      if (!lits.length) continue;
      if (DARK_AWARE.test(expr) || DARK_AWARE.test(text.slice(Math.max(0, index - 200), index + 200))) continue;
      /* 底板刻意不翻（亮色孤岛=外部内容白纸，暗色孤岛=画面/代码深色底）时，
         文字与底是成对钉死的，不按「两种皮肤表面都能读」这条算 */
      const window_ = exemptWindow(text, index);
      if (window_.includes('亮色孤岛') || window_.includes('暗色孤岛')) continue;
      const seg = text.slice(0, index);
      /* 只算真正作用在文字上的样式字面量；对象字面量里的图标色/主题色由成对色测试与皮肤块覆盖 */
      if (!/(?:\bsx|\bstyle)=\{\{/.test(seg.slice(seg.lastIndexOf('<')))) continue;
      const tags = [...seg.matchAll(/<([A-Za-z][\w.]*)[\s\n/>]/g)];
      const owner = tags.length ? tags[tags.length - 1][1] : '';
      if (owner === '' || GRAPHIC_OWNER.test(owner)) continue;
      /* 同一段样式里写死的实心底（bgcolor/background）：文字压在它上面，而不是页面表面 */
      const around = text.slice(Math.max(0, index - 500), index + 500);
      const fills = [
        ...around.matchAll(/(?:bgcolor|backgroundColor|\bbackground):\s*'(#[0-9a-fA-F]{6})'/g),
      ].map((x) => x[1]);
      const surfaces = fills.length ? fills : [LIGHT_SURFACE, ...DARK_SURFACES];
      for (const lit of new Set(lits)) {
        if (isNearWhite(lit)) continue;
        const worst = Math.min(...surfaces.map((s) => contrast(lit, s)));
        if (worst < 4.5) {
          bump(found.lowContrastTextColor, rel + ':' + lineOf(text, index) + ' ' + lit);
        }
      }
    }
    /* 写死的实心底（不透明十六进制）配随皮肤变的文字令牌：底不跟着翻，暗色下浅色令牌压浅色底必糊 */
    for (const m of text.matchAll(/\b(?:sx|style)=\{\{/g)) {
      const openIdx = text.indexOf('{', m.index);
      const block = openIdx < 0 ? null : balancedBlock(text, openIdx);
      if (!block) continue;
      const fills = [...block.matchAll(/(?:bgcolor|backgroundColor|\bbackground):\s*'(#[0-9a-fA-F]{6})'/g)].map((x) => x[1]);
      if (!fills.length) continue;
      if (!/(?<![\w-])color:\s*[^,}]*var\(--tt-/.test(block)) continue;
      bump(found.hardFillWithTokenText, rel + ':' + lineOf(text, m.index) + ' ' + [...new Set(fills)].join('/'));
    }
    /* 全文件按角色扫底：浅粉彩/近白的实心「面」不跟皮肤翻，就是暗色下的白卡片。
       文字槽位不在这条里判（品牌图标色会被误伤），那一侧仍走上面 colorSlots 的检查——
       状态色板的 fg/bg 配对由这条从「底」那一侧兜住：底一改成令牌，配不上皮肤的浅底先进账。 */
    for (const m of text.matchAll(/#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b/g)) {
      if (roleAt(text, m.index) !== 'fill') continue;
      const lit = '#' + m[1];
      const c = parseColor(lit);
      if (!c || luminance(c) < PALE) continue;
      if (exemptWindow(text, m.index).includes('亮色孤岛')) continue;
      bump(found.lightIslandSurface, rel + ':' + lineOf(text, m.index) + ' ' + lit);
    }
    /* 同一趟扫描的「中性近黑面底」：那是没跟着皮肤翻的主操作底，暗色下按钮化成页面底 */
    for (const m of text.matchAll(/#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b/g)) {
      if (roleAt(text, m.index) !== 'fill') continue;
      if (!isNeutralDarkFill('#' + m[1])) continue;
      if (exemptWindow(text, m.index).includes('暗色孤岛')) continue;
      bump(found.darkIslandFill, rel + ':' + lineOf(text, m.index) + ' #' + m[1]);
    }
    /* 深色底板（亮度 ≤0.12，含带彩度的 #0B1220 那类「刻意不翻」的画面底板）里，
       文字必须跟底板一起钉死：用 var(--tt-text-*) 的话亮色档给的是深灰，压在近黑底上 2.9:1。 */
    for (const m of text.matchAll(/#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b/g)) {
      if (roleAt(text, m.index) !== 'fill') continue;
      const c = parseColor('#' + m[1]);
      if (!c || luminance(c) > 0.12) continue;
      const tagStart = text.lastIndexOf('<', m.index);
      if (tagStart < 0) continue;
      const end = subtreeEnd(text, tagStart);
      if (end < 0) continue;
      if (!/color:\s*['"]var\(--tt-text/.test(text.slice(m.index, end))) continue;
      bump(found.darkIslandWithTokenText, rel + ':' + lineOf(text, m.index) + ' #' + m[1]);
    }
    /* 第 9 条：渐变面底对白字逐档体检。var(--x, #HEX) 里的兜底色先抹掉，那是令牌不是底。
       页面/皮肤背景在 styles/*.css 与 theme/ 里，本来就不在这份扫描的范围（见 tsxFiles）。 */
    const lines = text.split('\n');
    lines.forEach((line, i) => {
      if (!line.includes('linear-gradient')) return;
      const stops = [...new Set((line.replace(/var\([^)]*\)/g, '').match(/#[0-9a-fA-F]{6}\b/g) ?? []).filter((h) => h.toUpperCase() !== '#FFFFFF'))];
      if (!stops.length) return;
      /* 只有真的配着白字才判：不配白字的封面/装饰渐变归皮肤链路那条 */
      const near = lines.slice(Math.max(0, i - 2), i + 15).join('\n');
      if (!/color:\s*['"]#(fff|FFF|ffffff|FFFFFF)['"]/.test(near)) return;
      if (/WebkitBackgroundClip|遮罩|亮色孤岛|暗色孤岛/.test(near)) return;
      const worst = Math.min(...stops.map((h) => contrast('#FFFFFF', h)));
      if (worst >= 4.5) return;
      const weakest = stops.reduce((a, b) => (contrast('#FFFFFF', a) <= contrast('#FFFFFF', b) ? a : b));
      bump(found.whiteInkOnGradient, `${rel}:${i + 1} ${weakest}`);
    });
  }
  return found;
}

/**
 * 历史尾巴。原来的 8 条全在 `components/Layout/TopBar.tsx` 里——那份死代码（只有 Layout/index.ts
 * 再导出，没有任何页面渲染它）已整文件删除，所以这里全部记 0：任何一条重新出现都是新增缺陷。
 */
const BASELINE: Record<keyof Findings, Record<string, number>> = {
  unnamedIconButton: {},
  deadClickSurface: {},
  unnamedDialog: {},
  outlineNone: {},
  lowContrastTextColor: {},
  hardFillWithTokenText: {},
  /* 剩下的全是「渲染外部内容本体」的亮色孤岛：沙箱网页预览、文档预览、生成式画布、
     侧栏品牌底板图标。它们显示的是别人写的内容（默认白纸），跟着皮肤翻反而不对。 */
  lightIslandSurface: {},
  /* 主操作底已全部走 --tt-cta-fill/-ink，源码里不再有写死的中性近黑面底 */
  darkIslandFill: {},
  darkIslandWithTokenText: {},
  /* 渐变面底逐档配白字 ≥4.5:1；白字在附近 15 行内的才算。 */
  whiteInkOnGradient: {},
};

function assertRatchet(name: keyof Findings, found: Map<string, number>): void {
  const allowed = BASELINE[name];
  const offenders: string[] = [];
  for (const [key, count] of found) {
    const cap = allowed[key] ?? 0;
    if (count > cap) offenders.push(`${key}：${count} 处（上限 ${cap}）`);
  }
  expect(
    offenders,
    `${name} 出现了新增缺陷：\n  ${offenders.join('\n  ')}\n` +
      `  修法见 lib/a11y.ts 的 clickableProps/disclosureProps/anchorMenuProps 与 tokens.ts 的文字档位。`,
  ).toEqual([]);
}

describe('可点表面与读屏名字（ratchet）', () => {
  const found = scan();

  it('没有新增缺名字的 IconButton', () => assertRatchet('unnamedIconButton', found.unnamedIconButton));
  it('没有新增键盘到不了的可点表面', () => assertRatchet('deadClickSurface', found.deadClickSurface));
  it('每个 Dialog 都有名字（aria-label / aria-labelledby / DialogTitle）', () =>
    assertRatchet('unnamedDialog', found.unnamedDialog));
  it('没有抹掉焦点环的 outline: none', () => assertRatchet('outlineNone', found.outlineNone));
  it('正文位没有「亮暗两种表面都读不出」的硬编码色', () =>
    assertRatchet('lowContrastTextColor', found.lowContrastTextColor));

  it('写死的实心底不配随皮肤变的文字令牌', () =>
    assertRatchet('hardFillWithTokenText', found.hardFillWithTokenText));

  it('没有「暗色下变成白卡片」的写死近白面底（豁免要在块里写 亮色孤岛 注释）', () =>
    assertRatchet('lightIslandSurface', found.lightIslandSurface));

  it('主操作底不许写死中性近黑（暗色下按钮化成页面底）', () =>
    assertRatchet('darkIslandFill', found.darkIslandFill));

  it('写死的深色底板里不跟着皮肤变的文字要用浅色档（不能用 --tt-text-*）', () =>
    assertRatchet('darkIslandWithTokenText', found.darkIslandWithTokenText));

  it('配白字的渐变底每一档都得压得住白字（#06B6D4 那类 2.43:1 的青色挡在尾巴上不行）', () =>
    assertRatchet('whiteInkOnGradient', found.whiteInkOnGradient));

  it('「焦点环由容器承担」的豁免不是后门：用了标记的文件必须真的有容器侧的环', () => {
    const users = SOURCES.filter(({ text }) => text.includes('焦点环由容器承担'));
    for (const { rel, text } of users) {
      // 标记只允许配合一条**容器自己**的焦点环：由 focused 状态驱动的 outline / boxShadow，
      // 或 :focus-within 块里的环。只抹内层环、容器上没有替代信号（哪怕同文件别处有环），
      // 都算拿豁免当后门。
      const containerRing =
        /(?:outline|boxShadow):\s*focused\b[\s\S]{0,80}var\(--tt-(?:focus|teal)\)/.test(text) ||
        /focus-within[\s\S]{0,200}?(?:outline|boxShadow):[\s\S]{0,60}var\(--tt-(?:focus|teal)\)/.test(text);
      expect(containerRing, `${rel} 抹掉了内层焦点环，却没有把环搬到容器自己身上`).toBe(true);
    }
  });

  it('全局焦点环的 data-tt-ring 点名排除写在规则里，且两条环都带上', () => {
    const src = read('../../../src/renderer/styles/globals.css');
    // 这条排除是「内层不再叠第二圈」的唯一有效手段：全局规则特异性 (3,0,0)
    // （:is 跟最 specific 的参数 [tabindex]:not([tabindex='-1']) 走，再加 :focus-visible），
    // 组件里 Emotion 单类名的 outline: none 只有 (2,1,0)，压根压不住它。
    const carve = `:not([data-tt-ring='container'] *, [data-tt-ring='self']):focus-visible`;
    expect(src.split(carve).length - 1, '主规则与 glass 规则都要有点名排除').toBe(2);
    // 排除不许退化成「整条规则删掉」：主规则仍要覆盖文本控件与可聚焦元素
    expect(src).toMatch(/\[tabindex\]:not\(\[tabindex='-1'\]\)/);
    expect(src).toMatch(/:is\(\s*\n?\s*a,\s*\n\s*button,\s*\n\s*input,/);
  });

  it('用了 data-tt-ring 的文件必须真的画出自己那条环（排除不是去掉环）', () => {
    const containerUsers = SOURCES.filter(({ text }) => /data-tt-ring\b[\s\S]{0,12}container/.test(text));
    expect(containerUsers.length, '输入卡这条容器排除不能再退回去').toBeGreaterThan(0);
    for (const { rel, text } of containerUsers) {
      const containerRing =
        /(?:outline|boxShadow):\s*focused\b[\s\S]{0,80}var\(--tt-(?:focus|teal)\)/.test(text) ||
        /focus-within[\s\S]{0,200}?(?:outline|boxShadow):[\s\S]{0,60}var\(--tt-(?:focus|teal)\)/.test(text);
      expect(containerRing, `${rel} 让全局环排除了内部控件，容器自己却没有环（焦点不可见）`).toBe(true);
    }
    const selfUsers = SOURCES.filter(({ text }) => /data-tt-ring\b[\s\S]{0,12}self/.test(text));
    expect(selfUsers.length, '自带环的小控件至少有一处在用这条').toBeGreaterThan(0);
    for (const { rel, text } of selfUsers) {
      expect(text, `${rel} 标了 self 却没自己画 :focus-visible 环`).toMatch(
        /:focus-visible['"]?\s*:\s*\{[^}]*outline:/,
      );
    }
  });

  it('尾巴清零：九类缺陷在全仓源码里各 0 处（历史尾巴随 TopBar 死代码一起没了）', () => {
    const nonEmpty = Object.entries(found)
      .map(([name, map]) => [name, [...map.values()].reduce((a, b) => a + b, 0)] as const)
      .filter(([, count]) => count > 0)
      .map(([name, count]) => `${name}=${count}`);
    expect(nonEmpty).toEqual([]);
  });

  it('死代码顶栏已整文件删除，Layout 不再导出第二套页面头部', () => {
    expect(read('../../../src/renderer/components/Layout/index.ts')).not.toContain('TopBar');
  });
});

describe('共享 a11y 原语', () => {
  const a11y = () => read('../../../src/renderer/lib/a11y.ts');

  it('导出三个 helper，且空格键被 preventDefault（否则按空格会滚页面）', () => {
    const src = a11y();
    for (const fn of ['clickableProps', 'disclosureProps', 'anchorMenuProps']) {
      expect(src, `缺少 ${fn}`).toContain(`export function ${fn}`);
    }
    expect(src).toMatch(/event\.key === ' '/);
    expect(src).toMatch(/event\.preventDefault\(\)/);
    expect(src).toMatch(/aria-expanded/);
    expect(src).toMatch(/aria-haspopup/);
  });

  it('菜单类 helper 保留 event.currentTarget（键盘打开也能锚定）', () => {
    expect(a11y()).toMatch(/onOpen\(event\.currentTarget\)/);
  });
});

describe('跳转链接、焦点环与减动效', () => {
  const layout = () => read('../../../src/renderer/components/Layout/MainLayout.tsx');
  const css = () => read('../../../src/renderer/styles/globals.css');

  it('MainLayout 第一下 Tab 能跳过侧栏，落点在 <main id="tt-main">', () => {
    const src = layout();
    expect(src).toMatch(/className="tt-skip-link"\s*\n?\s*href="#tt-main"|href="#tt-main"/);
    expect(src).toMatch(/id="tt-main"/);
  });

  it('.tt-skip-link 平时移出屏幕、:focus 时回到 0', () => {
    const src = css();
    const block = src.match(/\.tt-skip-link\s*\{([\s\S]*?)\}/);
    expect(block, '缺少 .tt-skip-link 样式').toBeTruthy();
    expect(block![1]).toMatch(/top:\s*-80px/);
    expect(src).toMatch(/\.tt-skip-link:focus\s*\{[\s\S]{0,60}top:\s*0/);
  });

  it('全局焦点环覆盖 role=button/菜单项/列表项/页签与可聚焦元素', () => {
    const src = css();
    const rule = src.match(/:is\(([\s\S]*?)\):focus-visible\s*\{([\s\S]*?)\}/);
    expect(rule, '缺少全局 :focus-visible 规则').toBeTruthy();
    for (const sel of ["[role='button']", "[role='menuitem']", "[role='option']", "[role='tab']", "[tabindex]:not([tabindex='-1'])"]) {
      expect(rule![1], `焦点环没覆盖 ${sel}`).toContain(sel);
    }
    expect(rule![2]).toMatch(/outline:\s*2px solid var\(--tt-focus\)/);
    expect(rule![2]).toMatch(/outline-offset:\s*2px/);
  });

  it('系统要求减动效时全局复位', () => {
    const src = css();
    const media = src.match(/@media \(prefers-reduced-motion: reduce\)\s*\{([\s\S]*?)\n\}/);
    expect(media, '缺少 prefers-reduced-motion 复位').toBeTruthy();
    expect(media![1]).toMatch(/animation-iteration-count:\s*1\s*!important/);
    expect(media![1]).toMatch(/transition-duration:\s*0\.01ms\s*!important/);
  });

  it('界面用中文还是英文时，<html lang> 跟着变（读屏选发音靠它）', () => {
    const provider = read('../../../src/renderer/i18n/index.tsx');
    expect(provider).toMatch(/documentElement\.lang\s*=\s*lang === 'en' \? 'en' : 'zh-CN'/);
    expect(read('../../../src/renderer/index.html')).toMatch(/<html lang="zh-CN">/);
  });
});

describe('深浅色与密度链路完整', () => {
  it('四套皮肤都有暗色块', () => {
    const css = read('../../../src/renderer/styles/globals.css');
    for (const style of ['glass', 'classic', 'parchment']) {
      expect(
        css,
        `html[data-theme='dark'][data-ui-style='${style}'] 缺失：亮色块会直接压到暗色上`,
      ).toMatch(new RegExp(`html\\[data-theme='dark'\\]\\[data-ui-style='${style}'\\]`));
    }
  });

  it('密度：MUI spacing 因子 + CSS 变量 + 组件尺寸三处都接线', () => {
    expect(read('../../../src/renderer/theme/theme.ts')).toMatch(/spacing:\s*4\s*\*\s*scale/);
    const bootstrap = read('../../../src/renderer/theme/bootstrap.ts');
    expect(bootstrap).toMatch(/data-density/);
    expect(bootstrap).toMatch(/DENSITY_ATTR/);
    const css = read('../../../src/renderer/styles/globals.css');
    for (const attr of ['compact', 'roomy']) {
      expect(css).toMatch(new RegExp(`html\\[data-density='${attr}'\\]`));
    }
  });

  it('亮色皮肤块里每个随模式变的变量，暗色皮肤块都得重新声明', () => {
    /* html[data-ui-style='classic'] 与 html[data-theme='dark'] 同特异性、又写在它后面：
       亮色块新覆盖一个变量而暗色块没跟，暗色就会继承到亮色档（实测链接绿 #166534 压深底 1.6:1）。 */
    const css = read('../../../src/renderer/styles/globals.css');
    const body = (sel: string) => {
      const m = css.match(new RegExp(`html\\[${sel}\\]\\s*\\{([\\s\\S]*?)\\n\\}`));
      return m ? m[1] : '';
    };
    const modeVars = [
      '--tt-bg', '--tt-surface', '--tt-surface-2', '--tt-surface-3',
      '--tt-text-1', '--tt-text-2', '--tt-text-3', '--tt-text-4',
      '--tt-focus', '--tt-link', '--tt-accent', '--tt-accent-violet',
      '--tt-danger', '--tt-success', '--tt-warning', '--tt-info', '--tt-teal',
      '--tt-chip-neutral', '--tt-chip-info', '--tt-chip-success', '--tt-chip-teal',
      '--tt-chip-warning', '--tt-chip-danger', '--tt-chip-accent',
      '--tt-cta-fill', '--tt-cta-fill-hover', '--tt-cta-ink',
    ];
    for (const style of ['glass', 'classic', 'parchment']) {
      const light = body(`data-ui-style='${style}'`);
      const dark = body(`data-theme='dark'\\]\\[data-ui-style='${style}'`);
      expect(light, `缺少 html[data-ui-style='${style}'] 亮色块`).toBeTruthy();
      for (const v of modeVars) {
        if (!light.includes(v + ':')) continue;
        expect(dark, `亮色块覆盖了 ${v}，但暗色 ${style} 块没跟着重新声明`).toContain(v + ':');
      }
    }
  });

  it('表面契约扫到了 .ts 数据映射（色板放在 lib/stores 里也算数）', () => {
    const rels = SOURCES.map((s) => s.rel);
    expect(rels).toContain('lib/skills.ts');
    expect(rels).toContain('stores/imChannelStore.ts');
    expect(rels.filter((r) => r.startsWith('theme/'))).toEqual([]);
  });

  it('文字四档与语义色在两套模式下都各自声明（不靠猜）', () => {
    const css = read('../../../src/renderer/styles/globals.css');
    const dark = css.match(/html\[data-theme='dark'\]\s*\{([\s\S]*?)\n\}/);
    expect(dark, '暗色基础块缺失').toBeTruthy();
    for (const token of [
      '--tt-text-1',
      '--tt-text-2',
      '--tt-text-3',
      '--tt-text-4',
      '--tt-focus',
      '--tt-link',
      '--tt-accent',
      '--tt-danger',
      '--tt-success',
      '--tt-warning',
      '--tt-info',
      '--tt-teal',
      '--tt-danger-fill',
    ]) {
      expect(dark![1], `暗色块没有覆盖 ${token}`).toContain(token);
    }
  });
});

describe('读屏标签可翻译', () => {
  it('每个 tr(中文) 字面量都在英文词典里（缺键会静默回退中文，英文用户读不到标签）', () => {
    const missing: string[] = [];
    for (const { rel, text } of SOURCES) {
      for (const m of text.matchAll(/\btr\(\s*'((?:[^'\\]|\\.)*)'/g)) {
        const key = m[1].replace(/\\'/g, "'");
        if (!Object.prototype.hasOwnProperty.call(EN_DICT, key)) missing.push(`${rel}:${lineOf(text, m.index)} ${key}`);
      }
    }
    expect(missing, `以下 tr() 键不在 EN_DICT：\n  ${missing.join('\n  ')}`).toEqual([]);
  });
});
