// tests/unit/design/design-tokens-contract.test.ts — 令牌登记表与产物联动（D-32 · #151）
//
// 这份是**真跑**的：直接 import src/shared/design-tokens，把仓库里真实的 globals.css 喂进去。
// 钉的四件事：
// 1. 登记表与 globals.css 逐名对齐（多一个少一个都红）；
// 2. 解析器复现「皮肤亮块与暗色基座同特异性、后写的赢」那条坑，八套配色都能解析满 45 个令牌；
// 3. 搬进产物的样式**只搬运、不发明**：输出里每个值都能在 globals.css 原文里找到，
//    而 globals.css 里那些「不是令牌值」的颜色（Markdown 代码块的 #E2E8F0 那类）一条都不许跟进去；
// 4. 档位属性名与 theme/bootstrap.ts 写的 data-* 同源——那边改了这边不改，产物就切不动主题。

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  DEFAULT_PREFS,
  DESIGN_SNIPPETS,
  MODE_STABLE_TOKENS,
  MODE_INVARIANT_EXEMPTIONS,
  THEME_ATTR_KEYS,
  THEME_ATTR_VALUES,
  THEME_MATRIX,
  TT_TOKENS,
  TT_TOKEN_GROUPS,
  TT_VAR_PREFIX,
  designArtifactCss,
  designArtifactDocument,
  parseTokenRules,
  resolvedTokenBlockCss,
  resolveTokens,
  snippetTokenNames,
  stripCssComments,
  themeSwitchableStyleCss,
  tokensStuckBetweenModes,
  unexpectedModeInvariants,
  tokenVar,
  unresolvedTokens,
  type TtTokenName,
  DesignTokenError,
} from '../../../src/shared/design-tokens';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const CSS = read('../../../src/renderer/styles/globals.css');
const TOKEN_SOURCE = read('../../../src/shared/design-tokens.ts');
const BOOTSTRAP = read('../../../src/renderer/theme/bootstrap.ts');

/** globals.css 里以声明形式出现的令牌（`--tt-x:` 在行首缩进后） */
function declaredTokens(css: string): string[] {
  return [...new Set([...css.matchAll(/^\s*(--tt-[\w-]+)\s*:/gm)].map((m) => m[1].slice(TT_VAR_PREFIX.length)))].sort();
}

/**
 * 亮暗同值的合法名单 = 结构性档位（MODE_STABLE_TOKENS）+ 实心填充档。
 * 填充档底上写的是白字，#4338CA/#0F766E/#B45309 压白字本来就 ≥4.5:1，翻亮反而糊
 * （globals.css 的填充档注释就是这个理由）。
 */
const INVARIANT_FILLS = ['accent-fill', 'accent-fill-hover', 'teal-fill', 'warning-fill'];

/** 亮暗同值的完整豁免名单（结构性 + 填充档）：名单之外漏一个暗色档就红 */
const INVARIANT_ALL = [...MODE_STABLE_TOKENS, ...INVARIANT_FILLS];

/** 随明暗必须变的令牌数：登记表减掉上面那批豁免 */
const MODE_VARYING_COUNT = TT_TOKENS.length - MODE_STABLE_TOKENS.length - INVARIANT_FILLS.length;

describe('令牌登记表与 globals.css 同源', () => {
  it('登记的名与 CSS 里声明的名逐一对齐（多一个少一个都算漂移）', () => {
    const declared = declaredTokens(CSS);
    expect([...TT_TOKENS].sort()).toEqual(declared);
    expect(declared.length).toBeGreaterThan(40);
  });

  it('分组不重不漏：并集正好是登记表，一个令牌只属于一个组', () => {
    const all = Object.values(TT_TOKEN_GROUPS).flat();
    expect(all.length).toBe(new Set(all).size);
    expect([...all].sort()).toEqual([...TT_TOKENS].sort());
  });

  it('登记表里没有颜色值：这份文件出现十六进制就是第二个颜色来源', () => {
    const code = stripJsComments(TOKEN_SOURCE);
    expect(code).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(code).not.toMatch(/\brgba?\(/);
    expect(code).not.toMatch(/\bhsl\(/);
  });

  it('拼错的令牌名当场报错，而不是静默变成透明', () => {
    expect(tokenVar('surface')).toBe('var(--tt-surface)');
    expect(() => tokenVar('surfaece')).toThrow(DesignTokenError);
    expect(() => tokenVar('surfaece')).toThrow('拼错的名字不会自己变成颜色');
  });
});

describe('层叠解析：八套配色都解析得出完整一张表', () => {
  it('亮暗 × 四皮肤共八套，每套都覆盖登记表的每一个令牌', () => {
    expect(THEME_MATRIX).toHaveLength(8);
    for (const prefs of THEME_MATRIX) {
      expect(unresolvedTokens(CSS, prefs), JSON.stringify(prefs)).toEqual([]);
    }
  });

  it('暗色不是「亮色档 + 猜」：随模式变的那批令牌每套皮肤都得跟着翻', () => {
    const mustFlip: TtTokenName[] = [
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
      'chip-neutral',
      'chip-info',
      'chip-success',
      'chip-warning',
      'chip-danger',
      'cta-fill',
      'cta-ink',
    ];
    for (const uiStyle of ['default', 'glass', 'classic', 'parchment']) {
      const light = resolveTokens(CSS, { ...DEFAULT_PREFS, uiStyle });
      const dark = resolveTokens(CSS, { ...DEFAULT_PREFS, mode: 'dark', uiStyle });
      const stuck = mustFlip.filter((name) => light[name] === dark[name]);
      expect(stuck, `${uiStyle} 皮肤下这些令牌亮暗同值：${stuck.join(', ')}`).toEqual([]);
    }
  });

  it('玻璃皮肤的半透明面底按原文搬过去，不被解析器改写成实心', () => {
    const glass = resolveTokens(CSS, { ...DEFAULT_PREFS, uiStyle: 'glass' });
    expect(glass['surface']).toMatch(/^rgba\(/);
    expect(themeSwitchableStyleCss(CSS)).toContain(`--tt-surface: ${glass['surface']}`);
  });

  it('八套配色里取值始终不变的只有结构性档位与实心填充档', () => {
    /** 这条是「谁可以不翻」的名单：除了这十个，任何令牌亮暗同值都是漏了暗色档。 */
    expect(INVARIANT_FILLS.every((name) => /-fill(-hover)?$/.test(name)), '豁免名单只能是填充档').toBe(true);
    const expected = [...INVARIANT_ALL].sort();

    const stable = TT_TOKENS.filter((name) => {
      const values = new Set(THEME_MATRIX.map((prefs) => resolveTokens(CSS, prefs)[name]));
      return values.size === 1;
    }).sort();
    expect(stable).toEqual(expected);
    expect(MODE_VARYING_COUNT).toBe(TT_TOKENS.length - stable.length);

    // 逐套皮肤单独比一次：漏档在「这一套皮肤」上就要看得见，不能被别的皮肤平均掉
    for (const uiStyle of ['default', 'glass', 'classic', 'parchment']) {
      const extra = [...MODE_INVARIANT_EXEMPTIONS[uiStyle]];
      expect(tokensStuckBetweenModes(CSS, uiStyle), `${uiStyle} 皮肤有不该同值的令牌`).toEqual(
        [...INVARIANT_ALL, ...extra].sort(),
      );
      expect(unexpectedModeInvariants(CSS, uiStyle), `${uiStyle} 皮肤漏了暗色档`).toEqual([]);
      // 按皮肤给的豁免不许是空头的：名单里每一条都得在真实 CSS 里真的不翻
      for (const name of extra) {
        expect(tokensStuckBetweenModes(CSS, uiStyle), `豁免名单里的 ${name} 其实会翻`).toContain(name);
      }
    }
  });

  it('密度与对话字体按属性档位走，不吃明暗', () => {
    const compact = resolveTokens(CSS, { ...DEFAULT_PREFS, density: 'compact' });
    const roomy = resolveTokens(CSS, { ...DEFAULT_PREFS, density: 'roomy' });
    expect(compact['row-pad-y']).not.toBe(roomy['row-pad-y']);
    expect(resolveTokens(CSS, { ...DEFAULT_PREFS, mode: 'dark' })['gap']).toBe(DEFAULT_TOKEN_GAP());
    expect(resolveTokens(CSS, { ...DEFAULT_PREFS, chatFont: 'serif' })['chat-font']).toMatch(/serif/);
  });

  it('选择器条件不全中就不匹配：`:root` 之外只认 html[data-*] 的声明块', () => {
    const rules = parseTokenRules(CSS);
    for (const rule of rules) {
      expect(rule.selector === ':root' || rule.selector.startsWith('html[data-')).toBe(true);
    }
    // Markdown 代码块那种「值里带 --tt- 引用」的组件规则不该被当成令牌块
    expect(rules.some((rule) => rule.selector.includes('markdown'))).toBe(false);
  });
});

describe('搬进产物：只搬运，不发明', () => {
  const emitted = themeSwitchableStyleCss(CSS);
  const source = stripCssComments(CSS);

  it('嵌进产物的每条声明都能在 globals.css 原文里找到', () => {
    for (const rule of parseTokenRules(CSS)) {
      for (const [name, value] of Object.entries(rule.declarations)) {
        expect(source, `${TT_VAR_PREFIX}${name}: ${value} 不是原文里的值`).toContain(`${TT_VAR_PREFIX}${name}: ${value}`);
      }
    }
  });

  it('非令牌的颜色一条都不跟进去（Markdown 代码块那些写死的底属于组件规则）', () => {
    for (const stray of ['#e2e8f0', '#22c55e', 'rgba(34, 197, 94', 'rgba(100, 116, 139']) {
      expect(emitted.toLowerCase(), `${stray} 逃进了产物样式`).not.toContain(stray.toLowerCase());
    }
    expect(emitted).not.toContain('backdrop-filter');
    expect(emitted).not.toContain('linear-gradient(135deg');
  });

  it('产物样式覆盖八套皮肤：切主题靠改 <html> 属性，不需要第二份值', () => {
    expect(emitted).toContain("html[data-theme='dark']");
    for (const style of ['glass', 'classic', 'parchment']) {
      expect(emitted).toContain(`html[data-theme='dark'][data-ui-style='${style}']`);
      expect(emitted).toContain(`html[data-ui-style='${style}']`);
    }
    expect(designArtifactCss(CSS)).toContain('.tt-card');
  });

  it('只钉一档时用最终值表：登记表里有的都在，且没有多余的档', () => {
    const block = resolvedTokenBlockCss(CSS, { ...DEFAULT_PREFS, mode: 'dark', uiStyle: 'parchment' });
    for (const name of TT_TOKENS) expect(block).toContain(`${TT_VAR_PREFIX}${name}:`);
    // 只输出一块最终值，不再有第二份皮肤块：产物靠属性切肤的那条路在这里被钉成「钉死一档」
    expect(block.split(':root {').length - 1).toBe(1);
    expect(block).not.toContain('html[data-');
  });
});

describe('组件片段：引用登记表，不引用色值', () => {
  it('片段里出现的令牌名全部在登记表里', () => {
    const registry = new Set<string>(TT_TOKENS);
    for (const name of snippetTokenNames()) {
      expect(registry.has(name), `片段引用了没登记的 ${name}`).toBe(true);
    }
  });

  it('片段里没有任何硬编码颜色：写一个就绕开了令牌层', () => {
    const joined = Object.values(DESIGN_SNIPPETS).join('\n');
    expect(joined).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(joined).not.toMatch(/\brgba?\(/);
  });

  it('柔和底与文字成对：chip-* 底必定配上同一段里的文字令牌', () => {
    for (const [name, cssText] of Object.entries(DESIGN_SNIPPETS)) {
      if (!cssText.includes(`${TT_VAR_PREFIX}chip-`)) continue;
      expect(cssText, `${name} 只有底没有字`).toMatch(/color:\s*var\(--tt-/);
    }
  });

  it('片段的内边距走密度令牌：产物跟着界面密度一起收放', () => {
    expect(DESIGN_SNIPPETS.card).toContain(`${TT_VAR_PREFIX}gap`);
    expect(DESIGN_SNIPPETS.rowPadding).toContain(`${TT_VAR_PREFIX}row-pad-y`);
    expect(DESIGN_SNIPPETS.rowPadding).toContain(`${TT_VAR_PREFIX}row-pad-x`);
    expect(DESIGN_SNIPPETS.emptyNote).toContain(`${TT_VAR_PREFIX}gap`);
  });
});

describe('档位属性与 theme/bootstrap.ts 同源', () => {
  it('四个属性名就是 bootstrap 写到 <html> 上的那四个', () => {
    for (const key of Object.values(THEME_ATTR_KEYS)) {
      expect(BOOTSTRAP, `bootstrap.ts 不写 data-${key} 了`).toContain(`data-${key}`);
    }
  });

  it('皮肤/密度/字体的档名映射与 bootstrap 的三张表逐条相等', () => {
    for (const [constName, table] of [
      ['UI_STYLE_ATTR', THEME_ATTR_VALUES.uiStyle],
      ['DENSITY_ATTR', THEME_ATTR_VALUES.density],
      ['CHAT_FONT_ATTR', THEME_ATTR_VALUES.chatFont],
    ] as const) {
      const block = new RegExp(`export const ${constName}: Record<string, string> = \\{([\\s\\S]*?)\\};`).exec(BOOTSTRAP);
      expect(block, `${constName} 的形状变了`).toBeTruthy();
      const theirs = [...block![1].matchAll(/(\S+):\s*'([^']+)'/g)].map((m) => `${m[1]}=${m[2]}`).sort();
      const mine = Object.entries(table).map(([zh, en]) => `${zh}=${en}`).sort();
      expect(mine, `${constName} 与登记表不一致`).toEqual(theirs);
    }
  });

  it('明暗档名与 resolveMode 的回话一致', () => {
    expect(THEME_ATTR_VALUES.mode['暗色']).toBe('dark');
    expect(THEME_ATTR_VALUES.mode['亮色']).toBe('light');
    expect(BOOTSTRAP).toContain("if (pref === '暗色') return 'dark'");
  });
});

describe('单文件产物外壳', () => {
  it('带上档位属性、令牌声明与片段，标题与正文按 HTML 转义', () => {
    const html = designArtifactDocument({
      appCss: CSS,
      prefs: { ...DEFAULT_PREFS, mode: 'dark', uiStyle: 'parchment' },
      title: '看板 <试点>',
      body: '<div class="tt-card">正文</div>',
    });
    expect(html).toContain('data-theme="dark"');
    expect(html).toContain("data-ui-style='parchment'".replace(/'/g, '"'));
    expect(html).toContain('看板 &lt;试点&gt;');
    expect(html).toContain('.tt-card');
    expect(html).toContain("html[data-theme='dark'][data-ui-style='parchment']");
    expect(html.indexOf('data-theme="dark"')).toBeLessThan(html.indexOf('.tt-card'));
  });

  it('切一档主题就是一份新的产物：两档的产物只差档位属性，不差颜色值', () => {
    const light = designArtifactDocument({ appCss: CSS, prefs: DEFAULT_PREFS, title: 'a', body: '' });
    const dark = designArtifactDocument({
      appCss: CSS,
      prefs: { ...DEFAULT_PREFS, mode: 'dark' },
      title: 'a',
      body: '',
    });
    // 嵌的是同一批声明块，所以两份的 <style> 内容一致，差的是 <html> 上的档位
    expect(dark).toContain('data-theme="dark"');
    expect(light).toContain('data-theme="light"');
    expect(dark.split('<style>')[1].split('</style>')[0]).toBe(light.split('<style>')[1].split('</style>')[0]);
  });

  it('残缺的样式表不当产物发出去：暗色基座整块没了，产物当场报错', () => {
    /**
     * 「缺令牌」在这种 CSS 上查不出来——`:root` 把 45 个都声明了一遍，删掉暗色块后
     * 暗色档照样解析得到，只是解析成了亮色的值。所以产物侧真正拦得住的是
     * 「亮暗同值」：漏档的表现就是那份名单变长。
     *
     * 删除要锚在行首：globals.css 的注释里也写着 `html[data-theme='dark']`
     * （讲同特异性那条坑的那一段），不限定行首就会从注释里开始吃，删掉的不止暗色基座这一块。
     */
    const crippled = CSS.replace(/^\s*html\[data-theme='dark'\]\s*\{[\s\S]*?\n\}/m, '');
    expect(crippled, '暗色基座没被删掉，这份「残缺样式」是假的').not.toContain('--tt-chat-assistant-bg: #1e293b');
    expect(crippled).toContain('--tt-chat-assistant-bg');
    /**
     * 产物侧的守卫按名字豁免（结构性档位 + `*-fill` / `*-fill-hover`），所以这里点名的是
     * 「除这批之外登记表的每一个」；`--tt-cta-fill` 那种「豁免名单里但真实会翻」的令牌
     * 由上面那条逐皮肤名单核对兜住——它一旦不翻就会进不了那十个的名单。
     */
    const suspicious = TT_TOKENS.filter(
      (name) => !MODE_STABLE_TOKENS.includes(name) && !/-fill(-hover)?$/.test(name),
    ).sort();
    expect(unexpectedModeInvariants(crippled), '删掉暗色基座后，该翻的令牌一个都没翻').toEqual(suspicious);
    expect(() =>
      designArtifactDocument({ appCss: crippled, prefs: { ...DEFAULT_PREFS, mode: 'dark' }, title: 'a', body: '' }),
    ).toThrow(DesignTokenError);
    expect(() =>
      designArtifactDocument({ appCss: crippled, prefs: { ...DEFAULT_PREFS, mode: 'dark' }, title: 'a', body: '' }),
    ).toThrow('亮暗同值（暗色档漏了）');
    // 完整的 globals.css 走同一条路不该报错：豁免名单之外没有令牌不翻
    expect(() =>
      designArtifactDocument({ appCss: CSS, prefs: { ...DEFAULT_PREFS, mode: 'dark' }, title: 'a', body: '' }),
    ).not.toThrow();
  });
});

function DEFAULT_TOKEN_GAP(): string {
  return resolveTokens(CSS, DEFAULT_PREFS).gap;
}

function stripJsComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '')
    .replace(/[ \t]+\/\/[^\n]*$/gm, '');
}
