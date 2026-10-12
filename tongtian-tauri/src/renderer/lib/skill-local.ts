/**
 * 技能 · 本地通道实现（Mock 与浏览器直连引擎共用）
 *
 * 桌面端以运行目录 `{data_home}/skills/<id>/SKILL.md` 为事实来源（Rust `skill::list_in`）；
 * 浏览器通道既不能列目录，也不能写盘，因此：
 * - `skill:list` 如实返回空列表（不返回示例技能，页面据此渲染真实空态）
 * - 安装/卸载/启停一律抛 `SKILL_UNSUPPORTED`（沿用 Hook 试运行在浏览器通道降级的先例）
 * 内置技能清单由 `lib/skills.ts` 独立提供，不走本通道。
 */

const UNSUPPORTED =
  'SKILL_UNSUPPORTED: 浏览器通道无落盘能力';

export interface LocalSkillResult {
  handled: boolean;
  value?: unknown;
}

/** 技能写操作通道（仅桌面端有真实落盘能力） */
const MUTATION_CHANNELS = new Set(['skill:install', 'skill:uninstall', 'skill:toggle']);

/** 技能通道本地实现：未命中返回 handled=false */
export function handleLocalSkillChannel(channel: string, args: unknown[]): LocalSkillResult {
  switch (channel) {
    case 'skill:root':
      // 浏览器通道无落盘能力：不编造运行目录，supported=false 由页面展示为「仅桌面端可用」
      void args;
      return { handled: true, value: { supported: false, skillsRoot: null, trashRoot: null } };
    case 'skill:list':
      return { handled: true, value: [] };
    case 'skill:install':
    case 'skill:uninstall':
    case 'skill:toggle':
      void args;
      throw new Error(UNSUPPORTED);
    default:
      return { handled: false };
  }
}
