/**
 * 技能 · 浏览器通道实现与 Rust 契约测试（任务 #18）
 *
 * 浏览器通道（Mock 与直连引擎共用）没有列目录与写盘能力，因此契约是「如实拒绝」：
 * - skill:list 返回空列表（不返回示例技能掩盖空态）
 * - skill:root supported=false（不编造运行目录）
 * - 安装/卸载/启停抛 SKILL_UNSUPPORTED，由 errorTextOf 归一为可读文案
 *
 * 同时扫描 Rust `skill` 模块源码，保证两侧错误码与「不破坏存量数据」的
 * 落盘策略（卸载移入回收目录、安装拒绝覆盖）不漂移。
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ErrorCode } from '../../../src/shared/contracts';
import { errorMessageOf, errorTextOf } from '../../../src/shared/error-messages';

type LocalSkillModule = typeof import('../../../src/renderer/lib/skill-local');

/** 读取 Rust 技能模块源码（契约扫描用） */
function rustSkillSource(): string {
  return readFileSync(
    fileURLToPath(new URL('../../../src-tauri/src/skill/mod.rs', import.meta.url)),
    'utf8',
  );
}

function rustLibSource(): string {
  return readFileSync(
    fileURLToPath(new URL('../../../src-tauri/src/lib.rs', import.meta.url)),
    'utf8',
  );
}

async function freshModule(): Promise<LocalSkillModule> {
  vi.resetModules();
  return import('../../../src/renderer/lib/skill-local');
}

let mod: LocalSkillModule;

beforeEach(async () => {
  mod = await freshModule();
});

// ── 通道分发器 ─────────────────────────────────────────────

describe('handleLocalSkillChannel · 只读通道如实降级', () => {
  it('skill:list 返回空列表且 handled=true（页面据此渲染真实空态）', () => {
    expect(mod.handleLocalSkillChannel('skill:list', [])).toEqual({ handled: true, value: [] });
  });

  it('skill:root 声明不支持且不给编造路径', () => {
    const res = mod.handleLocalSkillChannel('skill:root', []);
    expect(res.handled).toBe(true);
    expect(res.value).toEqual({ supported: false, skillsRoot: null, trashRoot: null });
  });

  it('未登记通道 handled=false，交回上层继续路由', () => {
    expect(mod.handleLocalSkillChannel('chat:history', [])).toEqual({ handled: false });
  });
});

describe('handleLocalSkillChannel · 写通道一律拒绝', () => {
  const mutations: Array<[string, unknown[]]> = [
    ['skill:install', [{ source: 'local', localPath: 'D:/tmp/demo/SKILL.md' }]],
    ['skill:uninstall', [{ skillId: 'demo' }]],
    ['skill:toggle', [{ skillId: 'demo', enabled: false }]],
  ];

  it.each(mutations)('%s 抛裸错误码前缀，便于 errorTextOf 归一', (channel, args) => {
    let msg = '';
    try {
      mod.handleLocalSkillChannel(channel, args);
    } catch (e) {
      msg = e instanceof Error ? e.message : String(e);
    }
    expect(msg.startsWith('SKILL_UNSUPPORTED')).toBe(true);
    expect(errorMessageOf('SKILL_UNSUPPORTED')).toContain('桌面版');
  });

  it('拒绝文案指向可操作动作，并保留通道诊断细节', () => {
    try {
      mod.handleLocalSkillChannel('skill:install', []);
      expect.unreachable();
    } catch (e) {
      const text = errorTextOf(e);
      expect(text).toContain(errorMessageOf(ErrorCode.SKILL_UNSUPPORTED));
      expect(text).toContain('浏览器通道无落盘能力');
    }
  });
});

// ── 与 Rust 侧的错误码契约 ─────────────────────────────────

describe('Rust skill 模块 · 错误码契约', () => {
  const emitted = [...new Set(rustSkillSource().match(/(?<=")SKILL_[A-Z][A-Z_]*/g) ?? [])].sort();

  it('源码扫描命中错误码（防止路径配置失效导致空集合假通过）', () => {
    expect(emitted.length).toBeGreaterThan(0);
  });

  it.each(emitted.map((c) => [c, c]))('Rust 抛出码 %s 已登记用户文案', (code) => {
    expect(errorMessageOf(code)).not.toContain('发生错误（');
  });

  it('安装来源解析拒绝形态不完整的输入（zip 未接入时不假装成功）', () => {
    const src = rustSkillSource();
    expect(src).toContain('SKILL_INCOMPATIBLE: 仅支持 SKILL.md 单文件安装');
    expect(src).toContain('SKILL_INCOMPATIBLE: 目录');
    expect(src).toContain('SKILL_NOT_FOUND: 技能文件不存在');
  });
});

// ── 存量数据保护：不覆盖、不物理删除 ───────────────────────

describe('Rust skill 模块 · 数据保护策略', () => {
  it('安装同名技能时拒绝覆盖，而非改写既有 SKILL.md', () => {
    const src = rustSkillSource();
    expect(src).toContain('已存在，请先卸载后再安装（不覆盖既有技能）');
    // 生产路径不得对技能目录做物理删除（temp 清理只出现在 tests 内）
    const prod = src.slice(0, src.indexOf('#[cfg(test)]'));
    expect(prod).not.toContain('remove_dir(');
    expect(prod).not.toContain('remove_dir_all(');
  });

  it('卸载把目录 rename 进 skills_removed 回收目录', () => {
    const src = rustSkillSource();
    expect(src).toContain('base.join("skills_removed")');
    expect(src).toContain('std::fs::rename(&dir, &trash)');
  });

  it('禁用清单落 settings_kv，不新增表也不触碰技能文件', () => {
    const src = rustSkillSource();
    expect(src).toContain('const DISABLED_KEY: &str = "skill.disabled";');
    expect(src).toContain('settings::set(');
  });
});

// ── command 注册 ──────────────────────────────────────────

describe('Rust command 与前端通道对齐', () => {
  it('五个技能 command 均已注册到 invoke_handler', () => {
    const lib = rustLibSource();
    for (const name of ['skill_root', 'skill_list', 'skill_install', 'skill_uninstall', 'skill_toggle']) {
      expect(lib, `lib.rs 缺少 skill::${name}`).toContain(`skill::${name},`);
    }
  });

  it('三条前端通道都路由到对应 command', async () => {
    const tauriApi = readFileSync(
      fileURLToPath(new URL('../../../src/renderer/lib/tauri-api.ts', import.meta.url)),
      'utf8',
    );
    expect(tauriApi).toContain("if (channel === 'skill:root') return invoke('skill_root')");
    expect(tauriApi).toContain("if (channel === 'skill:list') return invoke('skill_list')");
    expect(tauriApi).toContain("invoke('skill_install', { req: args[0] })");
    expect(tauriApi).toContain("invoke('skill_uninstall'");
    expect(tauriApi).toContain("invoke('skill_toggle'");
  });
});

// ── skills.ts 消费面 ──────────────────────────────────────

describe('toInstalledSkillInfos：禁用技能不可调用', () => {
  it('剔除 enabled=false 的技能', async () => {
    const { toInstalledSkillInfos } = await import('../../../src/renderer/lib/skills');
    const raw = [
      {
        id: 'weekly-report',
        name: 'weekly-report',
        description: '生成周报',
        source: 'installed' as const,
        enabled: true,
        readable: true,
        installedAtMs: 1,
        path: 'C:/tt/skills/weekly-report',
      },
      {
        id: 'draft',
        name: 'draft',
        description: null,
        source: 'installed' as const,
        enabled: false,
        readable: true,
        installedAtMs: 2,
        path: 'C:/tt/skills/draft',
      },
    ];
    expect(toInstalledSkillInfos(raw)).toEqual([
      { id: 'weekly-report', name: 'weekly-report', description: '生成周报', source: 'installed' },
    ]);
  });
});
