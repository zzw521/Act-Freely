/**
 * 通天办公 - 技能加载（已安装 + 内置）
 *
 * 对话输入框「/调用技能」与魔棒按钮共用此模块：
 * - 已安装技能：通过 skill:list 通道读取（桌面端为真实 SKILL.md 注入列表）
 * - 内置技能：随应用内置的固定能力清单（兜底展示，Mock 阶段同样可见）
 * - Mock 开关规范：VITE_MOCK=false 时仅展示 skill:list 真实结果 + 内置清单
 */

export interface SkillInfo {
  id: string;
  name: string;
  description?: string;
  /** 技能来源：内置（随应用）/ 已安装（用户安装或 SKILL.md 注入） */
  source: 'builtin' | 'installed';
}

/** 内置技能清单（随应用内置，随版本演进） */
export const BUILTIN_SKILLS: SkillInfo[] = [
  { id: 'local-cli', name: 'local-cli', description: '本地 CLI 与系统查询', source: 'builtin' },
  { id: 'doc-convert', name: 'doc-convert', description: 'Markdown / Word / Excel / CSV 互转', source: 'builtin' },
  { id: 'weekly-report', name: 'weekly-report', description: '汇总本周工作，自动生成周报', source: 'builtin' },
  { id: 'data-analysis', name: 'data-analysis', description: '汇总数据、生成图表与统计摘要', source: 'builtin' },
  { id: 'file-organize', name: 'file-organize', description: '按规则整理目录与批量重命名', source: 'builtin' },
];

/**
 * 加载全部可用技能：已安装（skill:list）+ 内置。
 * 同名时已安装优先；读取失败降级为仅内置清单。
 */
export async function loadAllSkills(): Promise<SkillInfo[]> {
  let installed: SkillInfo[] = [];
  try {
    const apiObj = (window as unknown as { tongtianAPI?: { invoke?: (channel: string) => Promise<unknown> } }).tongtianAPI;
    const raw = (await apiObj?.invoke?.('skill:list')) as Array<{ id?: string; name?: string; description?: string }> | undefined;
    if (Array.isArray(raw)) {
      installed = raw
        .filter((s) => Boolean(s?.name))
        .map((s, i) => ({
          id: s.id ?? `installed-${i}`,
          name: s.name as string,
          description: s.description,
          source: 'installed' as const,
        }));
    }
  } catch {
    installed = [];
  }
  const installedNames = new Set(installed.map((s) => s.name));
  const builtins = BUILTIN_SKILLS.filter((s) => !installedNames.has(s.name));
  return [...installed, ...builtins];
}
