/**
 * 通天办公 - 无障碍语义小工具（D-25）
 *
 * 渲染层里有相当多的「可点表面」不是 <button>：MUI 的 Box 上挂 onClick。
 * 读屏念不出它是控件，键盘也根本走不到（2.1.1 / 4.1.2）。
 * 与其在每一处手写 role + tabIndex + 按键处理（一定会有人漏掉其中一个），
 * 统一从这里取。
 */

import type React from 'react';

const WHITE_INK = '#FFFFFF';
const DARK_INK = '#0F172A';

function srgbChannelLuminance(v: number): number {
  const s = v / 255;
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

function relativeLuminance(r: number, g: number, b: number): number {
  return 0.2126 * srgbChannelLuminance(r) + 0.7152 * srgbChannelLuminance(g) + 0.0722 * srgbChannelLuminance(b);
}

function contrastRatio(l1: number, l2: number): number {
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
}

/**
 * 色块上该用哪支字色：白或深，取 WCAG 对比度更高的那一支（1.4.3）。
 *
 * 头像底色是用户自己选的，写死 '#fff' 的话一半的档位只有 2:1；
 * 拿不准的输入（渐变、var()）回退白字，那种底色由调用方自己保证是深色。
 */
export function inkOn(background: string): string {
  const hex = background.trim().match(/^#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$/);
  if (!hex) return WHITE_INK;
  const h = hex[1].length === 3 ? hex[1].split('').map((c) => c + c).join('') : hex[1];
  const l = relativeLuminance(parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16));
  const [lw, ld] = [contrastRatio(1, l), contrastRatio(l, relativeLuminance(15, 23, 42))];
  return ld > lw ? DARK_INK : WHITE_INK;
}

export interface ClickableProps extends React.HTMLAttributes<HTMLElement> {
  /** 控件没有可见文字时才需要传；有文字时名字来自内容本身 */
  'aria-label'?: string;
}

/**
 * 把非按钮的可点表面变成键盘可达、有名字的按钮。
 *
 * 空格键要阻止默认行为：否则按下去先滚一屏再触发，看起来像「按了没反应」。
 */
export function clickableProps(onActivate: () => void, label?: string): ClickableProps {
  return {
    role: 'button',
    tabIndex: 0,
    ...(label ? { 'aria-label': label } : {}),
    onKeyDown: (event: React.KeyboardEvent) => {
      if (event.key === 'Enter' || event.key === ' ' || event.key === 'Spacebar') {
        event.preventDefault();
        event.stopPropagation();
        onActivate();
      }
    },
  };
}

/** 展开/收起一行：状态要能被读屏念出来，不能只靠一个箭头朝向（1.4.1 只用颜色/形状传信息） */
export function disclosureProps(expanded: boolean, onToggle: () => void, label: string): ClickableProps {
  return {
    ...clickableProps(onToggle, label),
    'aria-expanded': expanded,
  };
}

export interface AnchorMenuProps {
  role: 'button';
  tabIndex: number;
  'aria-haspopup': 'menu';
  'aria-expanded'?: boolean;
  'aria-label': string;
  onClick: (event: React.MouseEvent<HTMLElement>) => void;
  onKeyDown: (event: React.KeyboardEvent<HTMLElement>) => void;
}

/**
 * 点开挂在自身上的菜单/浮层（锚点就是这个元素本身）。
 *
 * 不能复用 clickableProps：那条路径拿不到事件，菜单会失去锚定位置，
 * 键盘打开时要么飘到屏幕角落，要么直接报错。
 */
export function anchorMenuProps(
  onOpen: (target: HTMLElement) => void,
  label: string,
  expanded?: boolean,
): AnchorMenuProps {
  return {
    role: 'button',
    tabIndex: 0,
    'aria-haspopup': 'menu',
    ...(expanded === undefined ? {} : { 'aria-expanded': expanded }),
    'aria-label': label,
    onClick: (event) => onOpen(event.currentTarget),
    onKeyDown: (event) => {
      if (event.key === 'Enter' || event.key === ' ' || event.key === 'Spacebar') {
        event.preventDefault();
        onOpen(event.currentTarget);
      }
    },
  };
}
