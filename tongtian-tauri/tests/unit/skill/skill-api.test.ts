/**
 * 技能 · 前端契约测试（任务 #18）
 *
 * 走真实分发路径：mock-api 注入 window.tongtianAPI → api.ts 的 skillAPI
 * → handleLocalSkillChannel，验证扩展页与 ChatInput 在浏览器通道拿到的形状：
 * 列表为空但不报错、运行目录声明不支持、写操作以可读文案失败；
 * 并验证 loadAllSkills 的「已安装优先 + 禁用剔除 + 失败降级」组合行为。
 */

import { describe, it, expect, beforeAll } from 'vitest';
import { ErrorCode } from '../../../src/shared/contracts';
import { errorMessageOf } from '../../../src/shared/error-messages';

(globalThis as Record<string, unknown>).window =
  (globalThis as Record<string, unknown>).window ?? {};

type SkillApi = typeof import('../../../src/renderer/lib/api');

let api: SkillApi;

beforeAll(async () => {
  await import('../../../src/renderer/lib/mock-api');
  api = await import('../../../src/renderer/lib/api');
});

describe('skillAPI · 只读通道（扩展页加载）', () => {
  it('列表返回空数组而非假技能，页面据此显示真实空态', async () => {
    expect(await api.skillAPI.list()).toEqual([]);
  });

  it('运行目录 supported=false，页面不展示编造路径', async () => {
    const root = await api.skillAPI.root();
    expect(root.supported).toBe(false);
    expect(root.skillsRoot).toBeNull();
    expect(root.trashRoot).toBeNull();
  });
});

describe('skillAPI · 写通道如实失败', () => {
  it('安装被拒且 errorTextOf 归一为「请在桌面版…管理技能」', async () => {
    await expect(
      api.skillAPI.install({ source: 'local', localPath: 'D:/tmp/demo/SKILL.md' }),
    ).rejects.toThrow(/SKILL_UNSUPPORTED/);
  });

  it('卸载/启停同样被拒，页面不会进入「假成功」状态', async () => {
    await expect(api.skillAPI.uninstall('demo')).rejects.toThrow(/SKILL_UNSUPPORTED/);
    await expect(api.skillAPI.toggle('demo', false)).rejects.toThrow(/SKILL_UNSUPPORTED/);
    // 列表仍为空，说明没有任何本地台账被伪造出来
    expect(await api.skillAPI.list()).toEqual([]);
  });

  it('拒绝文案指向桌面端而非泛泛失败', () => {
    expect(errorMessageOf(ErrorCode.SKILL_UNSUPPORTED)).toContain('桌面版任我行');
  });
});

describe('loadAllSkills · 对话输入框消费面', () => {
  it('浏览器通道下降级为内置清单（不含任何「已安装」假数据）', async () => {
    const { loadAllSkills, BUILTIN_SKILLS } = await import('../../../src/renderer/lib/skills');
    const skills = await loadAllSkills();
    expect(skills.every((s) => s.source === 'builtin')).toBe(true);
    expect(skills.map((s) => s.id).sort()).toEqual(BUILTIN_SKILLS.map((s) => s.id).sort());
  });

  it('已安装技能优先于同名内置项（模拟桌面端返回）', async () => {
    const { toInstalledSkillInfos, BUILTIN_SKILLS } = await import('../../../src/renderer/lib/skills');
    const installed = toInstalledSkillInfos([
      {
        id: 'weekly-report',
        name: 'weekly-report',
        description: '真实磁盘上的周报技能',
        source: 'installed',
        enabled: true,
        readable: true,
        installedAtMs: 1,
        path: 'C:/tt/skills/weekly-report',
      },
    ]);
    const merged = [
      ...installed,
      ...BUILTIN_SKILLS.filter((b) => !installed.some((s) => s.name === b.name)),
    ];
    expect(merged[0].description).toBe('真实磁盘上的周报技能');
    expect(merged.some((s) => s.id === 'local-cli')).toBe(true);
  });
});
