/**
 * 专家套件 · 前端通道与消费面契约测试（任务 #37）
 *
 * 走真实分发路径：mock-api 注入 window.tongtianAPI → api.ts 的 suiteAPI
 * → handleLocalSuiteChannel → suite-zip 解析，验证套件页与输入框在浏览器通道拿到的形状；
 * 再扫描 Rust command 注册、三通道接线与页面文案，确保「本地化安装 + / 可用」不是口号。
 */

import { describe, it, expect, beforeAll, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { IPC_CHANNELS, ErrorCode } from '../../../src/shared/contracts';
import { errorMessageOf } from '../../../src/shared/error-messages';
import { bytesToBase64Native, demoPackage } from './zip-fixture';

function src(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const store = new Map<string, string>();
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
  setItem: (k: string, v: string) => void store.set(k, String(v)),
  removeItem: (k: string) => void store.delete(k),
  clear: () => store.clear(),
  key: (i: number) => [...store.keys()][i] ?? null,
  get length() {
    return store.size;
  },
};
(globalThis as Record<string, unknown>).window =
  (globalThis as Record<string, unknown>).window ?? {};

type SuiteApi = typeof import('../../../src/renderer/lib/api');
type Skills = typeof import('../../../src/renderer/lib/skills');

let api: SuiteApi;
let skills: Skills;

beforeAll(async () => {
  await import('../../../src/renderer/lib/mock-api');
  api = await import('../../../src/renderer/lib/api');
  skills = await import('../../../src/renderer/lib/skills');
});

beforeEach(() => {
  store.clear();
});

// ── 通道形状（套件页加载） ─────────────────────────────────

describe('suiteAPI · 浏览器通道如实降级', () => {
  it('运行目录 supported=false，页面提示「不写盘」而不是编造路径', async () => {
    const root = await api.suiteAPI.root();
    expect(root).toEqual({ supported: false, suitesRoot: null, trashRoot: null });
  });

  it('未安装时列表为空数组，页面渲染真实空态', async () => {
    expect(await api.suiteAPI.list()).toEqual([]);
  });
});

describe('suiteAPI · 安装即真实生效', () => {
  it('上传合法套件包 → 列表立刻回读得到解析出的指令与统计', async () => {
    const row = await api.suiteAPI.install({
      fileName: 'admin.zip',
      zipBase64: bytesToBase64Native(demoPackage().zip),
    });
    expect(row.id).toBe('admin-assist');
    const rows = await api.suiteAPI.list();
    expect(rows).toHaveLength(1);
    expect(rows[0]).toEqual(row);
    expect(JSON.parse(store.get('tt:suites')!)).toHaveLength(1);
  });

  it('非法包以可读文案失败，列表保持原状（不会进入假成功）', async () => {
    await expect(
      api.suiteAPI.install({ fileName: 'bad.zip', zipBase64: bytesToBase64Native(new TextEncoder().encode('x')) }),
    ).rejects.toThrowError(/^SUITE_INCOMPATIBLE/);
    expect(await api.suiteAPI.list()).toEqual([]);
    const text = errorMessageOf(ErrorCode.SUITE_INCOMPATIBLE);
    expect(text).toContain('plugin.json');
  });

  it('重复安装被拒且不改写既有套件', async () => {
    const first = await api.suiteAPI.install({ zipBase64: demoPackage().zipBase64 });
    await expect(api.suiteAPI.install({ zipBase64: demoPackage().zipBase64 })).rejects.toThrowError(
      /^SUITE_INSTALL_FAILED/,
    );
    const rows = await api.suiteAPI.list();
    expect(rows).toHaveLength(1);
    expect(rows[0].installedAtMs).toBe(first.installedAtMs);
  });

  it('卸载未知标识报 SUITE_NOT_FOUND，已安装的不受影响', async () => {
    await api.suiteAPI.install({ zipBase64: demoPackage('alpha-suite').zipBase64 });
    await expect(api.suiteAPI.uninstall('ghost')).rejects.toThrowError(/^SUITE_NOT_FOUND/);
    expect(await api.suiteAPI.list()).toHaveLength(1);
    const res = await api.suiteAPI.uninstall('alpha-suite');
    expect(res.trashPath).toContain('suites_removed');
    expect(await api.suiteAPI.list()).toEqual([]);
  });
});

// ── 消费面：输入框 / 候选（兑现套件页说明） ────────────────

describe('loadAllSkills · 已安装套件指令进入 / 候选', () => {
  it('浏览器通道无套件时只降级为内置清单', async () => {
    const list = await skills.loadAllSkills();
    expect(list.every((s) => s.source === 'builtin')).toBe(true);
  });

  it('安装套件后 / 候选出现指令，标识带套件归属', async () => {
    await api.suiteAPI.install({ zipBase64: demoPackage().zipBase64 });
    const list = await skills.loadAllSkills();
    const suiteRows = list.filter((s) => s.source === 'suite');
    expect(suiteRows.map((s) => s.name)).toEqual(['meeting-minutes', 'notice-draft']);
    expect(suiteRows[0].id).toBe('suite:admin-assist:meeting-minutes');
    expect(suiteRows[0].description).toContain('来自套件「admin-assist」');
    // 内置清单仍然完整保留
    expect(list.filter((s) => s.source === 'builtin')).toHaveLength(skills.BUILTIN_SKILLS.length);
  });

  it('指令与内置同名时套件优先，内置同名项被让位', async () => {
    store.set(
      'tt:suites',
      JSON.stringify([
        {
          id: 'ops',
          name: '运营日常套件',
          version: 'v1.0.0',
          commands: [{ name: 'weekly-report', description: '按套件模板出周报' }],
          readable: true,
          enabled: true,
          skillCount: 0,
          dataLinkCount: 0,
          fileCount: 2,
          installedAtMs: 5,
          path: 'local://suites/ops',
        },
      ]),
    );
    const list = await skills.loadAllSkills();
    const hit = list.filter((s) => s.name === 'weekly-report');
    expect(hit).toHaveLength(1);
    expect(hit[0].source).toBe('suite');
  });

  it('manifest 损坏的套件不产出指令（页面与输入框都不显示空壳）', () => {
    const infos = skills.toSuiteCommandInfos([
      {
        id: 'broken',
        name: '坏掉的套件',
        version: '未标注版本',
        commands: [{ name: 'ghost', description: null }],
        readable: false,
        enabled: true,
        skillCount: 0,
        dataLinkCount: 0,
        fileCount: 0,
        installedAtMs: 1,
        path: '',
      },
    ]);
    expect(infos).toEqual([]);
  });

  it('跨套件同名指令只保留最近安装的一条', () => {
    const mk = (id: string, name: string, at: number) => ({
      id,
      name,
      version: 'v1',
      commands: [{ name: 'dup', description: null }],
      readable: true,
      enabled: true,
      skillCount: 0,
      dataLinkCount: 0,
      fileCount: 1,
      installedAtMs: at,
      path: '',
    });
    // suite:list 按安装时间倒序，故先出现者（最近更新）胜出
    const infos = skills.toSuiteCommandInfos([mk('b', '后装的', 2), mk('a', '先装的', 1)]);
    expect(infos).toEqual([
      { id: 'suite:b:dup', name: 'dup', description: '套件「后装的」指令', source: 'suite' },
    ]);
  });

  it('三档来源各有独立标识文案与配色', () => {
    expect(skills.SKILL_SOURCE_META.builtin.blockLabel).toBe('内置技能');
    expect(skills.SKILL_SOURCE_META.installed.blockLabel).toBe('已安装技能');
    expect(skills.SKILL_SOURCE_META.suite.label).toBe('套件指令');
    expect(skills.SKILL_SOURCE_META.suite.blockLabel).toBe('专家套件指令');
    const colors = new Set(['builtin', 'installed', 'suite'].map((k) => (skills.SKILL_SOURCE_META as never)[k]?.solid));
    expect(colors.size).toBe(3);
  });
});

// ── 三通道接线与页面接线（源码契约） ───────────────────────

describe('通道接线契约', () => {
  it('四条套件通道登记在 IPC 契约里', () => {
    expect(IPC_CHANNELS.SUITE_ROOT).toBe('suite:root');
    expect(IPC_CHANNELS.SUITE_LIST).toBe('suite:list');
    expect(IPC_CHANNELS.SUITE_INSTALL).toBe('suite:install');
    expect(IPC_CHANNELS.SUITE_UNINSTALL).toBe('suite:uninstall');
  });

  it('桌面通道把四条通道映射到 Rust command', () => {
    const tauriApi = src('../../../src/renderer/lib/tauri-api.ts');
    expect(tauriApi).toContain("if (channel === 'suite:root') return invoke('suite_root')");
    expect(tauriApi).toContain("if (channel === 'suite:list') return invoke('suite_list')");
    expect(tauriApi).toContain("invoke('suite_install', { req: args[0] })");
    expect(tauriApi).toContain("invoke('suite_uninstall', { suiteId:");
  });

  it('Rust 侧四个 command 均已注册到 invoke_handler', () => {
    const lib = src('../../../src-tauri/src/lib.rs');
    for (const name of ['suite_root', 'suite_list', 'suite_install', 'suite_uninstall']) {
      expect(lib, `lib.rs 缺少 suite::${name}`).toContain(`suite::${name},`);
    }
    expect(src('../../../src-tauri/src/suite/mod.rs')).toContain('pub async fn suite_install');
  });

  it('Mock 与引擎直连通道都先走本地套件实现', () => {
    for (const rel of ['../../../src/renderer/lib/mock-api.ts', '../../../src/renderer/lib/engine-api.ts']) {
      const s = src(rel);
      expect(s).toContain("from './suite-local'");
      expect(s).toMatch(
        /const (local|suite) = handleLocalSuiteChannel\(channel, args\);\s*\n\s*if \(\1\.handled\) return \1\.value;/,
      );
    }
  });

  it('api.ts 暴露 suiteAPI 四个方法', () => {
    const s = src('../../../src/renderer/lib/api.ts');
    expect(s).toContain('export const suiteAPI');
    for (const ch of ['SUITE_ROOT', 'SUITE_LIST', 'SUITE_INSTALL', 'SUITE_UNINSTALL']) {
      expect(s).toContain(`IPC_CHANNELS.${ch}`);
    }
  });
});

describe('套件页与输入框接线契约', () => {
  const page = src('../../../src/renderer/pages/ExpertSuite/index.tsx');
  const input = src('../../../src/renderer/components/ChatInput/index.tsx');

  it('套件页读注册表并真实安装/卸载，占位口号已清除', () => {
    expect(page).toContain('await suiteAPI.list()');
    expect(page).toContain('await suiteAPI.root()');
    expect(page).toContain('await suiteAPI.install({ fileName: file.name, zipBase64: bytesToBase64(bytes) })');
    expect(page).toContain('await suiteAPI.uninstall(s.id)');
    expect(page).toContain('suiteAPI');
    expect(page).not.toContain('安装能力即将上线');
    expect(page).not.toContain('本地安装流程即将上线');
    expect(page).not.toContain('「让通天办公帮我创建」能力即将上线');
    // 广场示例条目没有下载地址时如实说明，并引导配置市场源或上传
    expect(page).toContain('本地示例条目，没有下载地址');
    expect(page).toContain("import MarketBrowser from '../../components/MarketBrowser'");
    expect(page).toContain('await marketAPI.installSuite(s.id)');
  });

  it('套件页不再自造安装标志位，安装状态来自注册表匹配', () => {
    expect(page).not.toContain('installed: false');
    expect(page).toContain('installedIds.has(s.id) || installedNames.has(s.name)');
    expect(page).toContain('已安装 ${installedRows.length}');
  });

  it('套件页错误提示走全局文案字典，不把 SUITE_* 裸码丢给用户', () => {
    expect(page).toContain("import { errorTextOf } from '../../../shared/error-messages'");
    expect(page).toContain("errorTextOf(e, '套件操作失败，请重试')");
    // toMessage 只保留字典归一后的文案，不再原样透出 Error.message
    expect(page).not.toContain('return e instanceof Error ? e.message : String(e);');
  });

  it('输入框引用块按三档来源取样式，套件指令不再冒充技能', () => {
    expect(input).toContain("import { loadAllSkills, SKILL_SOURCE_META, type SkillInfo } from '../../lib/skills'");
    expect(input).toContain('SKILL_SOURCE_META[referencedSkill.source]');
    expect(input).toContain('SKILL_SOURCE_META[s.source]');
    expect(input).not.toContain("source === 'installed' ?");
    // 套件指令与技能走同一条消费路径：发送时带上 /名称 前缀
    expect(input).toContain('/${referencedSkill.name} ');
  });
});
