/**
 * 吉祥物「通通」：五个表情位图可渲染、可关动效、接入点不退回通用图标。
 *
 * 渲染断言用 renderToStaticMarkup（不依赖 DOM），源码断言锁住三个接入点，
 * 避免以后改版又把头像换回机器人图标、把空态换回渐变方块字。
 * vitest 只收集 tests/unit/**\/*.test.ts，所以这里用 createElement 而不是 JSX。
 */

import { describe, it, expect } from 'vitest';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import Panda, { type PandaProps, type PandaState } from '../../../src/renderer/components/Panda/index.tsx';

const STATES: PandaState[] = ['idle', 'thinking', 'working', 'done', 'oops'];

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

function readBytes(rel: string): Buffer {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)));
}

function renderPanda(props: PandaProps): string {
  return renderToStaticMarkup(React.createElement(Panda, props));
}

describe('熊猫「通通」', () => {
  it('五个状态都能渲染，且各自指向独立的表情图', () => {
    const htmls = STATES.map((state) => renderPanda({ state, size: 32 }));
    for (const [i, state] of STATES.entries()) {
      expect(htmls[i]).toContain(`data-panda-state="${state}"`);
      expect(htmls[i]).toContain('<img');
    }
    expect(new Set(htmls).size).toBe(STATES.length);
  });

  it('五个表情资源都是 512×512 的 PNG，透明底贴纸尺寸一致', () => {
    for (const state of STATES) {
      const png = readBytes(`../../../src/renderer/assets/panda/${state}.png`);
      expect([...png.subarray(0, 8)], `${state} 不是 PNG`).toEqual([
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
      ]);
      expect(png.readUInt32BE(16), `${state} 宽度`).toBe(512);
      expect(png.readUInt32BE(20), `${state} 高度`).toBe(512);
      expect(png.length, `${state} 文件过小`).toBeGreaterThan(20_000);
    }
  });

  it('动效可关闭：animated=false 不再挂 tt-panda-* 动画类', () => {
    const on = renderPanda({ state: 'idle', animated: true });
    expect(on).toContain('tt-panda-sway');
    const off = renderPanda({ state: 'idle', animated: false });
    expect(off).not.toContain('tt-panda-sway');
  });

  it('头像与空态主视觉构图分开：full 更高并带脚下软影', () => {
    const head = renderPanda({ size: 120 });
    const full = renderPanda({ size: 120, variant: 'full', ground: true });
    expect(head).toContain('data-panda-variant="head"');
    expect(full).toContain('data-panda-variant="full"');
    expect(full).toContain('radial-gradient(ellipse at center');
    expect(head).not.toContain('radial-gradient(ellipse at center');
  });

  it('装饰用不抢读屏，主视觉才有可读名称', () => {
    expect(renderPanda({})).toContain('aria-hidden="true"');
    expect(renderPanda({ ariaLabel: '通通' })).toContain('aria-label="通通"');
  });

  it('减少动效：所有 tt-panda-* 动画都在 reduced-motion 下关闭', () => {
    const css = read('../../../src/renderer/styles/globals.css');
    const block = css.split('@media (prefers-reduced-motion: reduce)')[1] ?? '';
    for (const cls of ['.tt-panda-sway', '.tt-panda-tilt', '.tt-panda-shake', '.tt-panda-pop']) {
      expect(block, `${cls} 未在 reduced-motion 中关闭`).toContain(cls);
    }
    expect(block).toContain('animation: none !important;');
  });
});

describe('熊猫接入点', () => {
  const stream = () => read('../../../src/renderer/components/ChatStream/index.tsx');
  const workbench = () => read('../../../src/renderer/pages/Workbench/index.tsx');
  const input = () => read('../../../src/renderer/components/ChatInput/index.tsx');

  it('空态主视觉换成熊猫，不再是渐变方块字', () => {
    const src = workbench();
    // 熊猫坐在输入卡上沿（下半身被卡片遮住），由 ChatInput 的 mascot 插槽承载
    expect(src).toContain('mascot={<Panda state="idle" size={128} variant="full" ariaLabel={tr(\'通通\')} />}');
    expect(input()).toContain('{mascot}');
    expect(src).not.toContain("background: 'linear-gradient(135deg, #34D399 0%, #06B6D4 100%)'");
  });

  it('助手头像按本轮流转切换表情，且不再用机器人图标', () => {
    const src = stream();
    expect(src).toContain('<Panda state={pandaState} size={30} />');
    expect(src).toContain("'working'");
    expect(src).toContain("'thinking'");
    expect(src).toContain("'oops'");
    expect(src).not.toContain('SmartToyIcon');
  });

  it('输入框状态灯只由 loading/stoppable 派生', () => {
    expect(input()).toContain("state={stoppable ? 'working' : loading ? 'thinking' : 'idle'}");
  });

  it('交付卡片用举竹子的熊猫，而不是通用图标', () => {
    expect(stream()).toContain('<Panda state="done" size={40} />');
  });
});
