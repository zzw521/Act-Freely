/**
 * 设计模式组件库（缺陷 D-32 · 任务 #151）
 *
 * 这一层解决的不是「好看」，是**同一个形状在十几个文件里各写一遍**：
 * 面板底、区块标题、柔和底 chip、空态说明、字段行——以前每个页面自己抄一份 sx，
 * 抄的时候连令牌一起抄，于是「换肤跟着变」这件事有 N 个落点，改一个漏 N-1 个。
 *
 * 三条口径：
 * 1. **颜色只有一个来源**：这里的每一条样式都是 `var(--tt-*)`，没有一个十六进制。
 *    令牌名从 `../../../shared/design-tokens` 的登记表取，拼错当场抛错而不是静默变成透明。
 * 2. **密度也联动**：内边距与行距读 `--tt-row-pad-*` / `--tt-gap`，所以设置页把界面
 *    调成「紧凑」时，组件库的产物与应用本身收放一致，不靠组件自己猜档位。
 * 3. **形状与单文件产物同源**：`DESIGN_SNIPPETS` 是同一批类名的 CSS 片段，
 *    设计模式产出的独立 HTML 用它 + 同一批令牌声明，两边才是同一种长相。
 *
 * 未做的部分如实写着：**产物生成侧还没接这条线**（写 HTML 的那半边不在本窗口领地），
 * 所以「产物切主题」目前是可用件、未接线、未验收。
 */

import React from 'react';
import { Box, Typography } from '@mui/material';
import { TT_VAR_PREFIX, tokenRef, type TtTokenName } from '../../../shared/design-tokens';

/**
 * 柔和底 chip 的七档色调。
 *
 * 每一档都是**成对**的：底用 `--tt-chip-*`、字用同色相的语义令牌。
 * 单独换其中一边就会在某个皮肤下糊掉——本机按 `:root` 亮档实算，浅底压近白字
 * （`--tt-chip-*` 配 `--tt-cta-ink` / `--tt-surface`）只有 **1.03–1.16:1**，等于看不见。
 * 所以两半写死在同一条记录里。
 */
export const KIT_CHIP_TONES = {
  neutral: { bg: 'chip-neutral', fg: 'text-2' },
  info: { bg: 'chip-info', fg: 'info' },
  success: { bg: 'chip-success', fg: 'success' },
  warning: { bg: 'chip-warning', fg: 'warning' },
  danger: { bg: 'chip-danger', fg: 'danger' },
  teal: { bg: 'chip-teal', fg: 'teal' },
  accent: { bg: 'chip-accent', fg: 'accent' },
} as const satisfies Record<string, { bg: TtTokenName; fg: TtTokenName }>;

export type KitChipTone = keyof typeof KIT_CHIP_TONES;

export const KitChip: React.FC<{
  children: React.ReactNode;
  tone?: KitChipTone;
  /** 图标类调用方直接塞进来，组件不猜语义 */
  icon?: React.ReactNode;
  title?: string;
}> = ({ children, tone = 'neutral', icon, title }) => (
  <Box
    component="span"
    title={title}
    sx={{
      display: 'inline-flex',
      alignItems: 'center',
      gap: 0.5,
      px: 1,
      py: 0.35,
      borderRadius: '8px',
      background: tokenRef(KIT_CHIP_TONES[tone].bg),
      color: tokenRef(KIT_CHIP_TONES[tone].fg),
      fontSize: '11.5px',
      lineHeight: 1.5,
    }}
  >
    {icon}
    {children}
  </Box>
);

/**
 * 面板底的那四条属性。单独把它拿出来是一份**可以被 Box 直接用的规范值**：
 * 已有页面里那些 `<Box sx={{ bgcolor, border, borderRadius, p }}>` 抄的都是这四条，
 * 抄一次多一个落点——放在这里，改圆角与底色就只有这一处。
 */
export const KIT_CARD_SX = {
  bgcolor: tokenRef('surface'),
  border: `1px solid ${tokenRef('border')}`,
  borderRadius: '14px',
  p: 2,
} as const;

/** 面板底：卡片容器与区块外壳共用的那一层，历史上在四个页面各抄过一遍 */
export const KitCard: React.FC<{
  children: React.ReactNode;
  /** 区块标题走 KitSectionTitle 时用默认的上下留白即可；列表容器可以关掉 */
  tight?: boolean;
  sx?: Record<string, unknown>;
}> = ({ children, tight, sx }) => (
  <Box
    sx={{
      ...KIT_CARD_SX,
      ...(tight ? { p: 1.5 } : {}),
      ...sx,
    }}
  >
    {children}
  </Box>
);

/** 区块标题 + 一句说明：说明文字存在的意义是「这一段的口径」，不是装饰 */
export const KitSectionTitle: React.FC<{
  title: React.ReactNode;
  hint?: React.ReactNode;
  right?: React.ReactNode;
}> = ({ title, hint, right }) => (
  <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1, mb: 1.25 }}>
    <Box sx={{ flex: 1, minWidth: 0 }}>
      <Typography sx={{ fontSize: '13px', fontWeight: 700, color: tokenRef('text-1') }}>{title}</Typography>
      {hint ? (
        <Typography sx={{ fontSize: '12px', color: tokenRef('text-4'), mt: 0.25, lineHeight: 1.6 }}>{hint}</Typography>
      ) : null}
    </Box>
    {right ? <Box sx={{ flexShrink: 0 }}>{right}</Box> : null}
  </Box>
);

/** 空态：说清楚「为什么是空的」和「下一步做什么」，不留一句「暂无数据」 */
export const KitEmptyNote: React.FC<{
  title: string;
  lines: string[];
  icon?: React.ReactNode;
}> = ({ title, lines, icon }) => (
  <Box
    sx={{
      border: `1px dashed ${tokenRef('border')}`,
      borderRadius: '14px',
      p: `calc(${tokenRef('gap')} * 2)`,
    }}
  >
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
      {icon ? <Box sx={{ fontSize: 15, color: tokenRef('text-4') }}>{icon}</Box> : null}
      <Typography sx={{ fontSize: '13.5px', fontWeight: 600, color: tokenRef('text-2') }}>{title}</Typography>
    </Box>
    {lines.map((line) => (
      <Typography
        key={line}
        sx={{ fontSize: '12px', color: tokenRef('text-4'), mt: 0.75, lineHeight: 1.7 }}
      >
        {line}
      </Typography>
    ))}
  </Box>
);

/** 字段行：标签弱一档、值强一档，读屏按顺序念出来仍然是「标签 值」 */
export const KitFieldRow: React.FC<{ label: string; value: React.ReactNode }> = ({ label, value }) => (
  <Box sx={{ mt: 0.75 }}>
    <Typography component="span" sx={{ fontSize: '11.5px', color: tokenRef('text-4'), mr: 0.75 }}>
      {label}
    </Typography>
    <Typography component="span" sx={{ fontSize: '12px', color: tokenRef('text-2'), wordBreak: 'break-word' }}>
      {value}
    </Typography>
  </Box>
);

/** 列表行：行内边距走密度令牌，紧凑/宽松档在这里是真的生效，不是只改字号 */
export const KitRow: React.FC<{
  children: React.ReactNode;
  divider?: boolean;
  align?: 'center' | 'flex-start';
  /** 只用于外边距一类的补充；内边距与底色一律由本组件给 */
  sx?: Record<string, unknown>;
}> = ({ children, divider, align = 'center', sx }) => (
  <Box
    sx={{
      display: 'flex',
      alignItems: align === 'center' ? 'center' : 'flex-start',
      gap: tokenRef('gap'),
      padding: `${tokenRef('row-pad-y')} ${tokenRef('row-pad-x')}`,
      borderBottom: divider ? `1px solid ${tokenRef('border-soft')}` : undefined,
      ...sx,
    }}
  >
    {children}
  </Box>
);
