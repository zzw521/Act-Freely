/**
 * 通天办公 - 技能加载（已安装 + 内置 + 已安装套件指令）
 *
 * 对话输入框「/调用技能」与魔棒按钮共用此模块：
 * - 已安装技能：skill:list 读取运行目录 skills/<id>/SKILL.md（桌面端真实扫描），
 *   被禁用的技能不出现在可调用清单中
 * - 内置技能：随应用内置的固定能力清单（浏览器通道无落盘能力时的可见来源）
 * - 套件指令：suite:list 读已安装套件的 plugin.json commands
 *   （兑现套件页「在对话框中输入 / 即可使用」，指令名冲突时先声明者胜出）
 * - 读取失败时降级为仅内置清单，不展示假技能
 */

import { skillAPI, suiteAPI } from './api';
import type { InstalledSkill, InstalledSuite } from '../../shared/contracts';

export interface SkillInfo {
  id: string;
  name: string;
  description?: string;
  /** 技能来源：内置（随应用）/ 已安装（用户安装或 SKILL.md 注入）/ 套件指令 */
  source: 'builtin' | 'installed' | 'suite';
}

/** 内置技能清单（随应用内置，随版本演进） */
export const BUILTIN_SKILLS: SkillInfo[] = [
  { id: 'local-cli', name: 'local-cli', description: '本地 CLI 与系统查询', source: 'builtin' },
  { id: 'doc-convert', name: 'doc-convert', description: 'Markdown / Word / Excel / CSV 互转', source: 'builtin' },
  { id: 'weekly-report', name: 'weekly-report', description: '汇总本周工作，自动生成周报', source: 'builtin' },
  { id: 'data-analysis', name: 'data-analysis', description: '汇总数据、生成图表与统计摘要', source: 'builtin' },
  { id: 'file-organize', name: 'file-organize', description: '按规则整理目录与批量重命名', source: 'builtin' },
];

/** 已安装技能 → 可调用清单（禁用项剔除） */
export function toInstalledSkillInfos(raw: InstalledSkill[]): SkillInfo[] {
  return raw
    .filter((s) => s.enabled && Boolean(s.name))
    .map((s) => ({
      id: s.id,
      name: s.name,
      description: s.description ?? undefined,
      source: 'installed' as const,
    }));
}

/**
 * 加载全部可用技能：已安装（skill:list，仅启用）+ 套件指令（suite:list）+ 内置。
 * 同名时前者优先（已安装 > 套件指令 > 内置）；任一读取失败只降级，不展示假条目。
 */
export async function loadAllSkills(): Promise<SkillInfo[]> {
  let installed: SkillInfo[] = [];
  try {
    installed = toInstalledSkillInfos(await skillAPI.list());
  } catch {
    installed = [];
  }
  let suiteCommands: SkillInfo[] = [];
  try {
    suiteCommands = toSuiteCommandInfos(await suiteAPI.list());
  } catch {
    suiteCommands = [];
  }
  const taken = new Set(installed.map((s) => s.name));
  const suiteRows = suiteCommands.filter((s) => {
    if (taken.has(s.name)) return false;
    taken.add(s.name);
    return true;
  });
  const builtins = BUILTIN_SKILLS.filter((s) => !taken.has(s.name));
  return [...installed, ...suiteRows, ...builtins];
}

/**
 * 已安装套件 → 可调用指令清单。
 * 不可读（manifest.json 损坏）的套件不产出指令；同名指令只保留先出现的一条
 * （suite:list 按安装时间倒序，即最近安装的套件胜出）。
 */
export function toSuiteCommandInfos(suites: InstalledSuite[]): SkillInfo[] {
  const seen = new Set<string>();
  const out: SkillInfo[] = [];
  for (const s of suites) {
    if (!s.readable || !Array.isArray(s.commands)) continue;
    for (const c of s.commands) {
      const name = (c?.name ?? '').trim();
      if (!name || seen.has(name)) continue;
      seen.add(name);
      out.push({
        id: `suite:${s.id}:${name}`,
        name,
        description: c.description ? `${c.description}（来自套件「${s.name}」）` : `套件「${s.name}」指令`,
        source: 'suite',
      });
    }
  }
  return out;
}

/** 引用块与选择菜单的来源标识样式（三档来源各一色，避免套件指令被显示成技能） */
export const SKILL_SOURCE_META: Record<
  SkillInfo['source'],
  {
    label: string;
    blockLabel: string;
    bg: string;
    activeBg: string;
    border: string;
    activeBorder: string;
    fg: string;
    /** 图标底色（实心） */
    solid: string;
    /** 引用块内文字色 */
    deep: string;
  }
> = {
  builtin: {
    label: '内置',
    blockLabel: '内置技能',
    bg: '#E7F8EF',
    activeBg: '#C7EFDC',
    border: 'rgba(16, 185, 129, 0.28)',
    activeBorder: 'rgba(16, 185, 129, 0.55)',
    fg: '#059669',
    solid: '#10B981',
    deep: '#047857',
  },
  installed: {
    label: '已安装',
    blockLabel: '已安装技能',
    bg: '#F1EBFF',
    activeBg: '#E2D5FB',
    border: 'rgba(139, 92, 246, 0.28)',
    activeBorder: 'rgba(139, 92, 246, 0.55)',
    fg: '#8B5CF6',
    solid: '#8B5CF6',
    deep: '#6D28D9',
  },
  suite: {
    label: '套件指令',
    blockLabel: '专家套件指令',
    bg: '#FFF3E0',
    activeBg: '#FFE3C2',
    border: 'rgba(245, 158, 11, 0.32)',
    activeBorder: 'rgba(245, 158, 11, 0.6)',
    fg: '#B45309',
    solid: '#F59E0B',
    deep: '#B45309',
  },
};
