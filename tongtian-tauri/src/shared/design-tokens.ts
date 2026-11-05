/**
 * 设计令牌登记表与产物联动（缺陷 D-32 · 任务 #151）
 *
 * 这份文件**不含任何颜色值**。颜色的唯一来源仍然是 `src/renderer/styles/globals.css`
 * 里的 `--tt-*` 声明块——本文件只做三件与「值」无关的事：
 *
 * 1. **登记令牌名**（`TT_TOKENS` / `TT_TOKEN_GROUPS`）：给组件库一个「拼错就报错」的面，
 *    也让「这个令牌属于哪一档」有地方问。由 `tests/unit/design/` 与 CSS 逐名对齐。
 * 2. **按属性组合解析**（`resolveTokens`）：把 globals.css 的选择器块当成层叠表来跑，
 *    复现「皮肤亮块与暗色基座同特异性」那条坑——解析器只认「条件全中才算匹配、后写的赢」，
 *    不自己发明优先级。
 * 3. **产出可嵌入单文件产物的样式段**（`themeSwitchableStyleCss` / `resolvedTokenBlockCss` /
 *    `DESIGN_SNIPPETS`）：设计模式产物是一段独立 HTML，它读不到应用的 CSS 文件，
 *    过去只能把颜色抄进产物里——抄进去的那一刻就有了第二个颜色来源。
 *    这里的做法是**搬运声明**而不是抄值：产物里嵌的还是 globals.css 原文的那批声明块，
 *    产物于是与应用共用同一份令牌，切主题只改 `<html>` 上的 data-* 属性。
 *
 * 边界（不许被读成已做）：产物**生成侧尚未接线**——写出单文件 HTML 的是引擎/桌面那半边，
 * 不在本窗口领地，所以这里交付的是「拿来即用」的函数与片段，实机产物是否真的联动仍属未验收。
 */

// ── 1. 令牌登记表：只有名字，没有值 ──────────────────────────

export type TtTokenName =
  | 'bg'
  | 'surface'
  | 'surface-2'
  | 'surface-3'
  | 'border'
  | 'border-soft'
  | 'text-1'
  | 'text-2'
  | 'text-3'
  | 'text-4'
  | 'focus'
  | 'link'
  | 'accent'
  | 'accent-violet'
  | 'danger'
  | 'success'
  | 'warning'
  | 'info'
  | 'teal'
  | 'danger-fill'
  | 'teal-fill'
  | 'accent-fill'
  | 'accent-fill-hover'
  | 'warning-fill'
  | 'chip-neutral'
  | 'chip-info'
  | 'chip-success'
  | 'chip-teal'
  | 'chip-warning'
  | 'chip-danger'
  | 'chip-accent'
  | 'cta-fill'
  | 'cta-fill-hover'
  | 'cta-ink'
  | 'shadow-card'
  | 'sidebar-bg'
  | 'chat-assistant-bg'
  | 'chat-process-bg'
  | 'chat-code-bg'
  | 'density-scale'
  | 'row-pad-y'
  | 'row-pad-x'
  | 'gap'
  | 'scrollbar'
  | 'chat-font';

export const TT_TOKENS: readonly TtTokenName[] = [
  'bg',
  'surface',
  'surface-2',
  'surface-3',
  'border',
  'border-soft',
  'text-1',
  'text-2',
  'text-3',
  'text-4',
  'focus',
  'link',
  'accent',
  'accent-violet',
  'danger',
  'success',
  'warning',
  'info',
  'teal',
  'danger-fill',
  'teal-fill',
  'accent-fill',
  'accent-fill-hover',
  'warning-fill',
  'chip-neutral',
  'chip-info',
  'chip-success',
  'chip-teal',
  'chip-warning',
  'chip-danger',
  'chip-accent',
  'cta-fill',
  'cta-fill-hover',
  'cta-ink',
  'shadow-card',
  'sidebar-bg',
  'chat-assistant-bg',
  'chat-process-bg',
  'chat-code-bg',
  'density-scale',
  'row-pad-y',
  'row-pad-x',
  'gap',
  'scrollbar',
  'chat-font',
];

/**
 * 令牌按用途归组。分组的意义在「同类必须成组引用」：
 * 柔和底（chip-*）与它的文字档（text-* / 语义色）是一对，拆开引用就会在换肤时糊成一片。
 */
export const TT_TOKEN_GROUPS: Record<string, readonly TtTokenName[]> = {
  surface: ['bg', 'surface', 'surface-2', 'surface-3', 'sidebar-bg', 'chat-assistant-bg', 'chat-process-bg', 'chat-code-bg'],
  border: ['border', 'border-soft'],
  text: ['text-1', 'text-2', 'text-3', 'text-4'],
  navigation: ['focus', 'link'],
  accent: ['accent', 'accent-violet'],
  semanticText: ['danger', 'success', 'warning', 'info', 'teal'],
  semanticFill: ['danger-fill', 'teal-fill', 'accent-fill', 'accent-fill-hover', 'warning-fill'],
  chip: ['chip-neutral', 'chip-info', 'chip-success', 'chip-teal', 'chip-warning', 'chip-danger', 'chip-accent'],
  cta: ['cta-fill', 'cta-fill-hover', 'cta-ink'],
  elevation: ['shadow-card'],
  density: ['density-scale', 'row-pad-y', 'row-pad-x', 'gap', 'scrollbar'],
  typography: ['chat-font'],
};

export const TT_VAR_PREFIX = '--tt-';

/** 取 `var(--tt-x)` 的引用串；登记表里没有的名字当场报错，而不是让界面拿到一条空声明 */
export function tokenVar(name: TtTokenName | string): string {
  if (!(TT_TOKENS as readonly string[]).includes(name)) {
    throw new DesignTokenError(`令牌 ${name} 不在登记表里：拼错的名字不会自己变成颜色`);
  }
  return `var(${TT_VAR_PREFIX}${name})`;
}

/** 同上，但用于 sx 里已经写好的字符串（不做校验，只拼引用），保留给测试与静态片段用 */
export function tokenRef(name: TtTokenName): string {
  return `var(${TT_VAR_PREFIX}${name})`;
}

export class DesignTokenError extends Error {
  constructor(message: string) {
    super(`DESIGN_TOKEN_INVALID: ${message}`);
    this.name = 'DesignTokenError';
  }
}

// ── 2. 主题档位：与 theme/bootstrap.ts 写在 <html> 上的属性同源 ───

export type ThemeMode = 'light' | 'dark';

export interface ThemePrefs {
  mode: ThemeMode;
  /** default / glass / classic / parchment —— CSS 侧的属性值，不是设置页的中文档 */
  uiStyle: string;
  /** standard / compact / roomy */
  density: string;
  /** sans / serif */
  chatFont: string;
}

/** globals.css 的声明块靠这四个属性选人；名字写错解析器就整块跳过（表现为「换肤没反应」） */
export const THEME_ATTR_KEYS = {
  mode: 'theme',
  uiStyle: 'ui-style',
  density: 'density',
  chatFont: 'chat-font',
} as const;

/** 设置页的中档名 → CSS 属性值（与 theme/bootstrap.ts 的三张映射表同源，由测试钉住） */
export const THEME_ATTR_VALUES: Record<keyof ThemePrefs, Record<string, string>> = {
  mode: { 亮色: 'light', 暗色: 'dark' },
  uiStyle: { 默认: 'default', 玻璃: 'glass', 经典: 'classic', 羊皮纸: 'parchment' },
  density: { 标准: 'standard', 紧凑: 'compact', 宽松: 'roomy' },
  chatFont: { 无衬线: 'sans', 衬线: 'serif' },
};

/** 默认档：与 globals.css 的 `:root` 一致（不随模式变的那一档就是这里） */
export const DEFAULT_PREFS: ThemePrefs = { mode: 'light', uiStyle: 'default', density: 'standard', chatFont: 'sans' };

/** 八套配色（亮暗 × 四皮肤）——产物嵌入与解析都要覆盖到这些组合 */
export const THEME_MATRIX: ThemePrefs[] = (['light', 'dark'] as ThemeMode[]).flatMap((mode) =>
  ['default', 'glass', 'classic', 'parchment'].map((uiStyle) => ({ ...DEFAULT_PREFS, mode, uiStyle })),
);

/** 给 `<html>` 用的 data-* 属性（产物与界面走同一套属性名，切换机制因此是同一个） */
export function themeDataAttributes(prefs: ThemePrefs): Record<string, string> {
  return {
    'data-theme': prefs.mode === 'dark' ? 'dark' : 'light',
    'data-ui-style': prefs.uiStyle,
    'data-density': prefs.density,
    'data-chat-font': prefs.chatFont,
  };
}

// ── 3. 层叠解析：把 globals.css 的声明块当成可重放的规则表 ───────

export interface TokenRule {
  /** 原选择器文本，例如 `html[data-theme='dark'][data-ui-style='glass']` */
  selector: string;
  /** 该块声明的令牌（不含 `--tt-` 前缀）→ 原文的值 */
  declarations: Record<string, string>;
  /** 选择器里的 data-* 条件；空数组＝`:root`，任何档位都命中 */
  conditions: Array<{ key: string; value: string }>;
}

const BLOCK_RE = /([^{}]+)\{([^{}]*)\}/g;
const CONDITION_RE = /\[data-([\w-]+)\s*=\s*(['"])([^'"]*)\2\]/g;
const DECLARATION_RE = /(--tt-[\w-]+)\s*:\s*([^;]+)/g;

/** 去掉注释：注释里出现的示例选择器不能被当成规则 */
export function stripCssComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, '');
}

/**
 * 取出 globals.css 里所有**令牌声明块**（按声明块解析，不碰其它规则）。
 *
 * `@layer base` 这类带嵌套的块会被外层正则跳过（外层含 `{` 时内层才匹配得上），
 * 而里面没有 `--tt-` 声明，本来也不该进产物。
 */
export function parseTokenRules(css: string): TokenRule[] {
  const rules: TokenRule[] = [];
  for (const match of stripCssComments(css).matchAll(BLOCK_RE)) {
    /**
     * `BLOCK_RE` 的 selector 捕获组吃的是「上一个匹配结尾到这一个 `{`」的全部文本，
     * 所以文件头的 `@tailwind base; @tailwind components;` 会连在第一个声明块前面。
     * 不切掉它，`:root {}` 会被当成以 `@` 开头的 at-rules 而整块丢弃 —— 于是
     * 「只在 `:root` 里声明过一次」的令牌（--tt-warning-fill、--tt-accent-fill-hover、
     * 密度与字体的标准档）在解析结果里凭空消失，八套配色全都不完整。
     */
    const tail = match[1];
    const selector = tail
      .slice(Math.max(tail.lastIndexOf(';'), tail.lastIndexOf('}')) + 1)
      .trim()
      .replace(/\s+/g, '');
    const body = match[2];
    if (!body.includes(TT_VAR_PREFIX)) continue;
    if (selector.startsWith('@') || selector.includes(',')) continue;
    const declarations: Record<string, string> = {};
    for (const decl of body.matchAll(DECLARATION_RE)) {
      declarations[decl[1].slice(TT_VAR_PREFIX.length)] = decl[2].trim();
    }
    if (!Object.keys(declarations).length) continue;
    const conditions: Array<{ key: string; value: string }> = [];
    if (selector !== ':root') {
      // 只接受 `html[...]` 这种皮肤块：别的写法说明 CSS 结构变了，宁可少解析也不猜
      if (!/^html(\[data-[\w-]+=['"][^'"]+['"]\])+$/.test(selector)) continue;
      for (const cond of selector.matchAll(CONDITION_RE)) {
        conditions.push({ key: cond[1], value: cond[3] });
      }
    }
    rules.push({ selector, declarations, conditions });
  }
  return rules;
}

/** 选择器条件是否全中：`html[data-theme='dark'][data-ui-style='glass']` 要两者都匹配 */
function ruleMatches(rule: TokenRule, prefs: ThemePrefs): boolean {
  const actual: Record<string, string> = {
    [THEME_ATTR_KEYS.mode]: prefs.mode,
    [THEME_ATTR_KEYS.uiStyle]: prefs.uiStyle,
    [THEME_ATTR_KEYS.density]: prefs.density,
    [THEME_ATTR_KEYS.chatFont]: prefs.chatFont,
  };
  return rule.conditions.every((condition) => actual[condition.key] === condition.value);
}

/**
 * 按层叠顺序重放，得到某档主题下每个令牌的最终值。
 *
 * 这里**不引入特异性**：globals.css 的注释写明皮肤亮块与暗色基座同特异性、靠书写顺序决胜，
 * 所以解析器同样只按「后写的赢」。哪天 CSS 真的按特异性排了，这里会先解析错、测试会先红。
 */
export function resolveTokens(css: string, prefs: ThemePrefs): Record<string, string> {
  const resolved: Record<string, string> = {};
  for (const rule of parseTokenRules(css)) {
    if (!ruleMatches(rule, prefs)) continue;
    Object.assign(resolved, rule.declarations);
  }
  return resolved;
}

/** 某个组合下「登记表里有、解析结果里没有」的令牌：等于产物嵌了一份会空心的表 */
export function unresolvedTokens(css: string, prefs: ThemePrefs): string[] {
  const resolved = resolveTokens(css, prefs);
  return TT_TOKENS.filter((name) => !(name in resolved));
}

/**
 * 设计上就与明暗无关的结构性档位（密度、对话字体、滚动条）。
 *
 * 「亮暗同值」的合法名单不止这几个：`--tt-*-fill` 那批实心填充档刻意不翻
 * （底上写的是白字，翻亮就糊，理由见 globals.css 的填充档注释）。所以这里只登记
 * 「结构性」那一半，产物完整性检查用它；真正的「谁可以不翻」由
 * tokensStuckBetweenModes 从 CSS 现算，测试负责把名单和理由对上号。
 */
export const MODE_STABLE_TOKENS: readonly TtTokenName[] = [
  'density-scale',
  'row-pad-y',
  'row-pad-x',
  'gap',
  'scrollbar',
  'chat-font',
];

/**
 * 同一套皮肤下「亮暗解析出同一个值」的令牌。
 *
 * 换肤漏档的表现就是这份名单变长：结构性那六个和填充档出现在这里是预期，
 * 其余任何一个（尤其 --tt-text-* / --tt-surface / --tt-chip-*）都是暗色档没给，
 * 亮色值会被直接带到暗色皮肤上——文字压白卡那种糊就是这么来的。
 * 产物嵌的是同一批声明块，所以这里查出的问题在产物里和应用里一模一样。
 */
export function tokensStuckBetweenModes(css: string, uiStyle = 'default'): string[] {
  const light = resolveTokens(css, { ...DEFAULT_PREFS, mode: 'light', uiStyle });
  const dark = resolveTokens(css, { ...DEFAULT_PREFS, mode: 'dark', uiStyle });
  return TT_TOKENS.filter((name) => name in light && name in dark && light[name] === dark[name]).sort();
}

// ── 4. 给单文件产物用的样式段 ────────────────────────────────

function emitRule(rule: TokenRule): string {
  const lines = Object.entries(rule.declarations).map(([name, value]) => `  ${TT_VAR_PREFIX}${name}: ${value};`);
  return `${rule.selector} {\n${lines.join('\n')}\n}`;
}

/**
 * 全部声明块原样搬运（选择器保留）。产物把 `<html>` 的 data-* 一改就换肤——
 * 与应用本身同一个机制，没有第二套值，也不需要脚本。
 */
export function themeSwitchableStyleCss(css: string): string {
  return parseTokenRules(css).map(emitRule).join('\n');
}

/** 钉死一档主题时用这个：只输出该档解析后的最终值（适合导出成图片/静态稿） */
export function resolvedTokenBlockCss(css: string, prefs: ThemePrefs): string {
  const resolved = resolveTokens(css, prefs);
  const lines = TT_TOKENS.filter((name) => name in resolved).map(
    (name) => `  ${TT_VAR_PREFIX}${name}: ${resolved[name]};`,
  );
  return `:root {\n${lines.join('\n')}\n}`;
}

/**
 * 组件片段：与 DesignKit 的 React 组件同形，供独立 HTML 直接 `<div class="tt-card">` 用。
 *
 * 这里**只允许出现 `var(--tt-*)` 引用**——写进一个十六进制就等于绕开令牌层，
 * 产物与应用从此各变各的肤。密度档位也走令牌，所以产物跟着「界面密度」一起收放。
 *
 * 能成对钉住的字面量（圆角、字号、描边虚实）与 `components/DesignKit` 一一对应，
 * 由 tests/unit/design/design-kit-contract.test.ts 钉：产物里的区块标题写 18px
 * 而应用里是 13px，两边就不是同一张脸。
 * 内边距钉不了字面量——React 侧的 `p: 2` 走 `theme.spacing`（按密度缩放），
 * CSS 侧没有 spacing，所以这里一律写成 `var(--tt-gap)` 的等价值（标准档 12px 时
 * 算出来就是 3px / 8px 那几个数），钉的是「两边都随密度收放」而不是某个像素。
 * 任何一段片段里出现裸 px 的 padding 就是自己钉了一档，换紧凑档它会装死。
 */
export const DESIGN_SNIPPETS: Record<string, string> = {
  card: `.tt-card {
  background: ${tokenRef('surface')};
  border: 1px solid ${tokenRef('border')};
  border-radius: 14px;
  padding: var(${TT_VAR_PREFIX}gap);
  box-shadow: ${tokenRef('shadow-card')};
}`,
  sectionTitle: `.tt-section-title {
  font-size: 13px;
  font-weight: 700;
  color: ${tokenRef('text-1')};
}
.tt-section-hint {
  font-size: 12px;
  color: ${tokenRef('text-4')};
}`,
  chip: `.tt-chip {
  display: inline-flex;
  align-items: center;
  gap: calc(var(${TT_VAR_PREFIX}gap) / 3);
  padding: calc(var(${TT_VAR_PREFIX}gap) / 4) calc(var(${TT_VAR_PREFIX}gap) * 2 / 3);
  border-radius: 8px;
  font-size: 11.5px;
  background: ${tokenRef('chip-neutral')};
  color: ${tokenRef('text-2')};
}`,
  statusChip: `.tt-chip-info { background: ${tokenRef('chip-info')}; color: ${tokenRef('info')}; }
.tt-chip-success { background: ${tokenRef('chip-success')}; color: ${tokenRef('success')}; }
.tt-chip-warning { background: ${tokenRef('chip-warning')}; color: ${tokenRef('warning')}; }
.tt-chip-danger { background: ${tokenRef('chip-danger')}; color: ${tokenRef('danger')}; }
.tt-chip-teal { background: ${tokenRef('chip-teal')}; color: ${tokenRef('teal')}; }
.tt-chip-accent { background: ${tokenRef('chip-accent')}; color: ${tokenRef('accent')}; }`,
  emptyNote: `.tt-empty {
  border: 1px dashed ${tokenRef('border')};
  border-radius: 14px;
  padding: calc(var(${TT_VAR_PREFIX}gap) * 2);
}
.tt-empty-title {
  font-size: 13.5px;
  font-weight: 600;
  color: ${tokenRef('text-2')};
}
.tt-empty-line {
  font-size: 12px;
  color: ${tokenRef('text-4')};
  line-height: 1.7;
}`,
  fieldRow: `.tt-field-label {
  font-size: 11.5px;
  color: ${tokenRef('text-4')};
}
.tt-field-value {
  font-size: 12px;
  color: ${tokenRef('text-2')};
  word-break: break-word;
}`,
  rowPadding: `.tt-row {
  padding: var(${TT_VAR_PREFIX}row-pad-y) var(${TT_VAR_PREFIX}row-pad-x);
}`,
};

/** 片段里引用到的令牌名（嵌进产物前用来核对「一个都不缺」） */
export function snippetTokenNames(): string[] {
  const names = new Set<string>();
  for (const cssText of Object.values(DESIGN_SNIPPETS)) {
    for (const match of cssText.matchAll(new RegExp(`${TT_VAR_PREFIX}([\\w-]+)`, 'g'))) {
      names.add(match[1]);
    }
  }
  return [...names].sort();
}

/** 拼成可以直接进 `<style>` 的一整段：令牌声明块 + 组件片段 */
export function designArtifactCss(css: string): string {
  return [themeSwitchableStyleCss(css), Object.values(DESIGN_SNIPPETS).join('\n')].join('\n');
}

function escapeHtml(text: string): string {
  return text.replace(/[&<>"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[ch] as string);
}

/**
 * 每套皮肤里「亮暗同值是这套皮肤的设定」的额外名单。
 *
 * 判定必须按皮肤走：默认皮肤漏了暗色档，不会因为羊皮纸没漏而变得能看。
 * 也正因如此，豁免只能按皮肤给，否则要么放过真缺陷，要么误杀刻意的不翻。
 * 经典皮肤把 `--tt-shadow-card` 定成 `none`，而它的暗色块没有重声明这条
 * ——亮暗都是「不打阴影」，是设计设定，不是漏档。
 */
export const MODE_INVARIANT_EXEMPTIONS: Record<string, readonly string[]> = {
  default: [],
  glass: [],
  classic: ['shadow-card'],
  parchment: [],
};

/**
 * 允许亮暗同值的令牌：结构性那六个（密度/字体/滚动条本就不吃明暗）+
 * 实心填充档及其悬停档（底上写的是白字，翻亮就糊，见 globals.css 的填充档注释）+
 * 上面那份按皮肤给的名单。除此之外任何一个不翻，就是暗色档漏了。
 */
function mayStayModeInvariant(name: string, uiStyle: string): boolean {
  const exempt = MODE_INVARIANT_EXEMPTIONS[uiStyle] ?? [];
  return (MODE_STABLE_TOKENS as readonly string[]).includes(name) || exempt.includes(name) || /-fill(-hover)?$/.test(name);
}

/** 亮暗同值、且不在允许名单里的令牌：皮肤链路真正要拦的是这一份 */
export function unexpectedModeInvariants(css: string, uiStyle = 'default'): string[] {
  return tokensStuckBetweenModes(css, uiStyle).filter((name) => !mayStayModeInvariant(name, uiStyle));
}

/**
 * 单文件产物外壳。
 *
 * 嵌的是 globals.css 的原样声明块，所以这里只查两件事：
 * 1. 令牌齐不齐——缺一个就是一份会空心的样式；
 * 2. 暗色档跟没跟上——把漏档的样式嵌进产物，就是让产物与应用各糊各的。
 * 两个都不过关就当场报错，而不是发出一份「看着正常、换肤全糊」的半成品。
 */
export function designArtifactDocument(input: {
  appCss: string;
  prefs: ThemePrefs;
  title: string;
  body: string;
}): string {
  const missing = TT_TOKENS.filter((name) => !(name in resolveTokens(input.appCss, input.prefs)));
  const stable = missing.filter((name) => !MODE_STABLE_TOKENS.includes(name));
  if (stable.length) {
    throw new DesignTokenError(`产物缺 ${stable.length} 个会随模式变的令牌：${stable.join(', ')}`);
  }
  const stuck = unexpectedModeInvariants(input.appCss, input.prefs.uiStyle);
  if (stuck.length) {
    throw new DesignTokenError(
      `产物的样式表有 ${stuck.length} 个令牌亮暗同值（暗色档漏了）：${stuck.join(', ')}`,
    );
  }
  const attrs = Object.entries(themeDataAttributes(input.prefs))
    .map(([key, value]) => `${key}="${escapeHtml(value)}"`)
    .join(' ');
  return [
    '<!DOCTYPE html>',
    `<html lang="zh-CN" ${attrs}>`,
    '<head>',
    '<meta charset="utf-8" />',
    '<meta name="viewport" content="width=device-width, initial-scale=1" />',
    `<title>${escapeHtml(input.title)}</title>`,
    '<style>',
    designArtifactCss(input.appCss),
    `body { background: ${tokenRef('bg')}; color: ${tokenRef('text-1')}; font-family: ${tokenRef('chat-font')}; margin: 0; padding: var(${TT_VAR_PREFIX}gap); }`,
    '</style>',
    '</head>',
    '<body>',
    input.body,
    '</body>',
    '</html>',
  ].join('\n');
}
