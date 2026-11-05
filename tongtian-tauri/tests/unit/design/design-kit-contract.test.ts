// tests/unit/design/design-kit-contract.test.ts — 组件库与令牌/产物同形（D-32 · 任务 #151）
//
// 本仓库的单元测试没有 jsdom/React 渲染环境（vitest environment: 'node'），
// 所以这里分两种钉法，各钉自己钉得动的那一半：
// - **真跑**：`src/shared/design-tokens` 是纯模块，`DESIGN_SNIPPETS` 与 `KIT_CHIP_TONES`
//   是模块级数据，直接 import 拿最终字符串比对（这两份没有 JSX，node 环境跑得动）；
// - **源码字面量**：`components/DesignKit` 里 JSX 的那批样式按字面量钉，
//   钉的是「用的是哪支令牌、形状写了多少」，不是像素。真实渲染仍未验收。
//
// 三条口径：① 颜色只有令牌一个来源；② 柔和底与文字成对；③ 组件与产物片段是同一张脸
// （圆角、字号、描边虚实逐项对齐；内边距两边机制不同，只要求都吃密度令牌）。

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  DESIGN_SNIPPETS,
  DEFAULT_PREFS,
  THEME_MATRIX,
  TT_TOKENS,
  TT_TOKEN_GROUPS,
  TT_VAR_PREFIX,
  resolveTokens,
} from '../../../src/shared/design-tokens';
import { contrast } from '../appearance/wcag';
import * as DesignKitModule from '../../../src/renderer/components/DesignKit';
import { KIT_CARD_SX, KIT_CHIP_TONES } from '../../../src/renderer/components/DesignKit';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const kit = () => read('../../../src/renderer/components/DesignKit/index.tsx');
const panel = () => read('../../../src/renderer/components/AppSnapshot/index.tsx');
const globals = read('../../../src/renderer/styles/globals.css');

function stripComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '')
    .replace(/[ \t]+\/\/[^\n]*$/gm, '');
}

/** 组件里出现过的每一个令牌名（`tokenRef('x')` 与直接写的 `var(--tt-x)` 都算） */
function tokensUsedInKit(): string[] {
  const src = kit();
  const names = new Set<string>();
  for (const m of src.matchAll(/tokenRef\('([\w-]+)'\)/g)) names.add(m[1]);
  for (const m of src.matchAll(/var\(--tt-([\w-]+)\)/g)) names.add(m[1]);
  return [...names].sort();
}

/** 片段里某个选择器块的内容：`DESIGN_SNIPPETS[name]` 是原样 CSS，按类名切出来 */
function snippetRule(name: string, selector: string): string {
  const cssText = DESIGN_SNIPPETS[name];
  const start = cssText.indexOf(selector);
  expect(start, `片段 ${name} 里没有 ${selector}`).toBeGreaterThanOrEqual(0);
  const open = cssText.indexOf('{', start);
  const close = cssText.indexOf('}', open);
  return cssText.slice(open + 1, close);
}

/** 从组件源码里某个组件的定义处截一段（拿 `export const X` 到下一个 `export const`） */
function componentBlock(source: string, exportName: string): string {
  const start = source.indexOf(`export const ${exportName}`);
  expect(start, `DesignKit 里找不到 ${exportName}`).toBeGreaterThanOrEqual(0);
  const next = source.indexOf('\nexport const', start + 1);
  return source.slice(start, next < 0 ? source.length : next);
}

const REGISTRY = new Set<string>(TT_TOKENS);
const DECLARED = new Set(
  [...globals.matchAll(/^\s*(--tt-[\w-]+)\s*:/gm)].map((m) => m[1].slice(TT_VAR_PREFIX.length)),
);

describe('DesignKit：颜色只有一个来源', () => {
  it('组件里没有硬编码颜色（十六进制/函数色/`palette` 取值都不许出现）', () => {
    const src = stripComments(kit());
    expect(src).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(src).not.toMatch(/\brgba?\(/);
    expect(src).not.toMatch(/\bhsl\(/);
    // `sx` 里写 MUI 主题色（palette.primary.main）是另一条绕过令牌的路，同样不许
    expect(src).not.toMatch(/\bpalette\b/);
    expect(src).not.toMatch(/color:\s*'(primary|secondary|error|warning|success|info)'/);
  });

  it('组件引用的每个令牌名都登记过、也在 globals.css 里声明过', () => {
    const used = tokensUsedInKit();
    expect(used.length).toBeGreaterThan(0);
    for (const name of used) {
      expect(REGISTRY.has(name), `登记表里没有 --tt-${name}`).toBe(true);
      expect(DECLARED.has(name), `globals.css 里没有 --tt-${name}`).toBe(true);
    }
  });

  it('色调表用的是柔和底那一组令牌，不引新色名', () => {
    const chipGroup = new Set<string>(TT_TOKEN_GROUPS.chip);
    for (const [tone, pair] of Object.entries(KIT_CHIP_TONES)) {
      expect(chipGroup.has(pair.bg), `${tone} 的底 ${pair.bg} 不在柔和底那一组`).toBe(true);
      expect(REGISTRY.has(pair.fg), `${tone} 的字 ${pair.fg} 没登记`).toBe(true);
      // 成对：一支底必须配一支字，两半写在同一条记录里（拆开写就是第二个事实源）
      expect(pair.bg.startsWith('chip-'), `${tone} 的底不是 chip-*`).toBe(true);
      expect(DECLARED.has(pair.bg)).toBe(true);
      expect(DECLARED.has(pair.fg)).toBe(true);
    }
  });
});

describe('DesignKit：密度与形状跟着令牌走', () => {
  it('行内边距与留白读密度令牌，紧凑档在这里是真的生效', () => {
    const src = kit();
    expect(src).toContain("tokenRef('gap')");
    expect(src).toContain("tokenRef('row-pad-y')");
    expect(src).toContain("tokenRef('row-pad-x')");
    expect(componentBlock(src, 'KitEmptyNote')).toContain('gap');
  });

  it('组件不许自己写档位数字：只允许 spacing 倍数与令牌', () => {
    // 写死 `padding: '12px'` 这类就是「第二套刻度」：紧凑档下它不收放
    const src = stripComments(kit());
    expect(src).not.toMatch(/padding:\s*'?\d+px/);
    expect(src).not.toMatch(/margin(Top|Bottom)?:\s*'?\d+px/);
  });
});

describe('组件与产物片段是同一张脸', () => {
  it('卡片：圆角 14px、实线描边、面板底同一档', () => {
    expect(componentBlock(kit(), 'KIT_CARD_SX')).toContain("borderRadius: '14px'");
    expect(snippetRule('card', '.tt-card')).toContain('border-radius: 14px');
    expect(snippetRule('card', '.tt-card')).toContain(`var(${TT_VAR_PREFIX}surface)`);
    expect(snippetRule('card', '.tt-card')).toContain('1px solid');
  });

  it('chip：8px 圆角 + 11.5px 字号两边一致', () => {
    const block = componentBlock(kit(), 'KitChip');
    expect(block).toContain("borderRadius: '8px'");
    expect(block).toContain("fontSize: '11.5px'");
    expect(snippetRule('chip', '.tt-chip')).toContain('border-radius: 8px');
    expect(snippetRule('chip', '.tt-chip')).toContain('font-size: 11.5px');
  });

  it('区块标题：13px/700 配 12px 说明，产物侧不许更大一号', () => {
    const title = componentBlock(kit(), 'KitSectionTitle');
    expect(title).toContain("fontSize: '13px'");
    expect(title).toContain('fontWeight: 700');
    expect(title).toContain("fontSize: '12px'");
    expect(snippetRule('sectionTitle', '.tt-section-title')).toContain('font-size: 13px');
    expect(snippetRule('sectionTitle', '.tt-section-title')).toContain('font-weight: 700');
    expect(snippetRule('sectionTitle', '.tt-section-hint')).toContain('font-size: 12px');
  });

  it('空态：虚线描边 + 14px 圆角 + 13.5px 标题', () => {
    const block = componentBlock(kit(), 'KitEmptyNote');
    expect(block).toContain('1px dashed');
    expect(block).toContain("borderRadius: '14px'");
    expect(block).toContain("fontSize: '13.5px'");
    expect(snippetRule('emptyNote', '.tt-empty')).toContain('1px dashed');
    expect(snippetRule('emptyNote', '.tt-empty')).toContain('border-radius: 14px');
    expect(snippetRule('emptyNote', '.tt-empty-title')).toContain('font-size: 13.5px');
  });

  it('字段行：标签 11.5px 弱档、值 12px 强档，两边一致', () => {
    const block = componentBlock(kit(), 'KitFieldRow');
    expect(block).toContain("fontSize: '11.5px'");
    expect(block).toContain("fontSize: '12px'");
    expect(snippetRule('fieldRow', '.tt-field-label')).toContain('font-size: 11.5px');
    expect(snippetRule('fieldRow', '.tt-field-value')).toContain('font-size: 12px');
  });

  it('列表行：两边的内边距都来自同一对密度令牌', () => {
    const block = componentBlock(kit(), 'KitRow');
    expect(block).toContain("tokenRef('row-pad-y')");
    expect(block).toContain("tokenRef('row-pad-x')");
    expect(snippetRule('rowPadding', '.tt-row')).toContain(`var(${TT_VAR_PREFIX}row-pad-y)`);
    expect(snippetRule('rowPadding', '.tt-row')).toContain(`var(${TT_VAR_PREFIX}row-pad-x)`);
  });

  it('片段里的每一处 padding 都吃密度令牌，没有裸 px', () => {
    for (const [name, cssText] of Object.entries(DESIGN_SNIPPETS)) {
      for (const m of cssText.matchAll(/(?:^|\n)\s*padding:\s*([^;]+);/g)) {
        expect(m[1], `片段 ${name} 的 padding 自己钉死一档：${m[1]}`).toContain(TT_VAR_PREFIX);
      }
      for (const m of cssText.matchAll(/(?:^|\n)\s*gap:\s*([^;]+);/g)) {
        expect(m[1], `片段 ${name} 的 gap 自己钉死一档：${m[1]}`).toContain(TT_VAR_PREFIX);
      }
    }
  });

  it('片段引用的令牌名同样全在登记表里', () => {
    for (const [name, cssText] of Object.entries(DESIGN_SNIPPETS)) {
      for (const m of cssText.matchAll(/var\((--tt-[\w-]+)\)/g)) {
        const token = m[1].slice(TT_VAR_PREFIX.length);
        expect(REGISTRY.has(token), `片段 ${name} 引用了没登记的 ${m[1]}`).toBe(true);
      }
    }
  });
});

describe('DesignKit 是展示层：不碰数据、不碰行为', () => {
  it('没有事件、没有状态、没有请求、没有 IPC', () => {
    const src = kit();
    for (const banned of [
      'onClick',
      'onKeyDown',
      'useState',
      'useEffect',
      'fetch(',
      'engineFetch',
      'tongtianAPI',
      '.invoke(',
    ]) {
      expect(src, `DesignKit 里出现了 ${banned}`).not.toContain(banned);
    }
    // 只依赖 MUI 的基础容器与文字，不引第二套样式运行时
    expect(src).toContain("from '@mui/material'");
    expect(src).not.toContain('styled(');
    expect(src).not.toContain('createTheme');
  });

  it('柔和底由组件成对给，调用方只选色调名', () => {
    const block = componentBlock(kit(), 'KitChip');
    expect(block).toContain('KIT_CHIP_TONES[tone].bg');
    expect(block).toContain('KIT_CHIP_TONES[tone].fg');
  });

  it('成对不是洁癖：七档在八套组合下都过 AA 普通文字线（≥4.5:1，实算）', () => {
    for (const [tone, pair] of Object.entries(KIT_CHIP_TONES)) {
      const ratios = THEME_MATRIX.map((prefs) => {
        const resolved = resolveTokens(globals, { ...DEFAULT_PREFS, ...prefs });
        return contrast(resolved[pair.fg], resolved[pair.bg]);
      });
      const min = Math.min(...ratios);
      expect(min, `${tone} 档最低对比度 ${min.toFixed(2)} 低于 4.5：八档读数 ${ratios.map((r) => r.toFixed(2)).join(' / ')}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it('反向对照：同一张底换掉字色就看不见（这正是必须成对存的理由）', () => {
    // 浅底压近白字：`--tt-chip-*` 配 `--tt-cta-ink` / `--tt-surface`，实测 1.10–1.16:1
    const resolved = resolveTokens(globals, DEFAULT_PREFS);
    for (const tone of Object.keys(KIT_CHIP_TONES)) {
      const bg = resolved[`chip-${tone}`];
      expect(bg, `globals.css 里没有 --tt-chip-${tone}`).toBeTruthy();
      expect(contrast(resolved['cta-ink'], bg)).toBeLessThan(1.2);
      expect(contrast(resolved['surface'], bg)).toBeLessThan(1.2);
    }
  });

  it('KIT_CARD_SX 是被 KitCard 展开的那份规范值，不是第三处落点', () => {
    const src = kit();
    expect(componentBlock(src, 'KitCard')).toContain('...KIT_CARD_SX');
    expect(componentBlock(src, 'KIT_CARD_SX')).toContain("bgcolor: tokenRef('surface')");
    // 真值里颜色那两条是令牌引用；圆角与 spacing 倍数是形状，本来就该是字面量
    expect(KIT_CARD_SX.bgcolor).toBe(`var(${TT_VAR_PREFIX}surface)`);
    expect(KIT_CARD_SX.border).toBe(`1px solid var(${TT_VAR_PREFIX}border)`);
    expect(KIT_CARD_SX.borderRadius).toBe('14px');
    expect(typeof KIT_CARD_SX.p).toBe('number');
  });

  it('导出面就是这八个：多一个得先有消费者，少一个是拆了别人的落点', () => {
    expect(Object.keys(DesignKitModule).sort()).toEqual(
      [
        'KIT_CARD_SX',
        'KIT_CHIP_TONES',
        'KitCard',
        'KitChip',
        'KitEmptyNote',
        'KitFieldRow',
        'KitRow',
        'KitSectionTitle',
      ].sort(),
    );
  });
});

describe('快照面板是组件库的真实消费者（不是自说自话的库）', () => {
  it('面板用的就是这些件，且不再自己引柔和底令牌', () => {
    const src = panel();
    expect(src).toContain("from '../DesignKit'");
    for (const name of ['KitChip', 'KitSectionTitle', 'KitEmptyNote', 'KitFieldRow', 'KitRow', 'KitCard']) {
      expect(src, `面板没有用 <${name}>`).toContain(`<${name}`);
    }
    expect(src).not.toContain(`${TT_VAR_PREFIX}chip-`);
    expect(src).not.toContain('panelSx');
  });

  it('面板的分级只映射到色调名，颜色留给组件库', () => {
    const src = panel();
    expect(src).toContain('public:');
    expect(src).toContain('KitChipTone');
    const mapping = /const SENSITIVITY_TONE[\s\S]*?\n\};/.exec(stripComments(src));
    expect(mapping, '档位→色调的映射表找不到了').toBeTruthy();
    expect(mapping![0]).not.toContain(TT_VAR_PREFIX);
  });
});
