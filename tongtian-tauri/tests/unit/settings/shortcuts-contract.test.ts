/**
 * 应用内快捷键契约测试（任务 #31）
 *
 * 设置页「快捷键」原本是一张写死的静态表：9 条组合里除输入框自己处理的
 * Enter/Shift+Enter 之外没有任何 keydown 监听，「点击录制」框也没有 onClick，
 * 侧边栏/任务监控的 store 动作是死代码。本文件从逻辑 + 源码两侧锁定链路为真：
 * - 能力清单 SHORTCUT_ACTIONS 是唯一权威（页面按它分组渲染，注册中心按它派发）
 * - 组合解析/匹配精确到修饰键，改键有形状校验与撞车拦截
 * - 输入框内打字不被裸键/Shift 组合劫持，未注册的能力不吞按键
 * - 改键落 settings_kv 的 shortcuts 台账（与 Rust 同一权威，刷新不丢）
 * - 页面不再出现写死的组合表，也不留按了没反应的「点击录制」装饰入口
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import {
  SHORTCUT_ACTIONS,
  comboFromEvent,
  dispatchShortcut,
  displayCombo,
  effectiveCombos,
  findConflicts,
  formatCombo,
  isEditableTarget,
  matchCombo,
  matchShortcut,
  parseCombo,
  shouldSkipInEditor,
  validateOverride,
} from '../../../src/renderer/lib/shortcuts';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const shortcutsLib = () => read('../../../src/renderer/lib/shortcuts.ts');
const hook = () => read('../../../src/renderer/lib/use-app-shortcuts.ts');
const contracts = () => read('../../../src/shared/contracts.ts');
const alignmentApi = () => read('../../../src/renderer/lib/alignment-api.ts');
const appStore = () => read('../../../src/renderer/stores/useAppStore.ts');
const mainLayout = () => read('../../../src/renderer/components/Layout/MainLayout.tsx');
const workbench = () => read('../../../src/renderer/pages/Workbench/index.tsx');
const settingsPage = () => read('../../../src/renderer/pages/Settings/index.tsx');

/** 能力清单里可由应用注册的那些（scope=in-app） */
const IN_APP = SHORTCUT_ACTIONS.filter((a) => a.scope === 'in-app');
const BUILTIN = SHORTCUT_ACTIONS.filter((a) => a.scope === 'builtin');

// ── 组合解析与展示 ────────────────────────────────────────────────

describe('组合解析 parseCombo', () => {
  it('解析「Ctrl+主键」，符号键与反斜杠按字面保留', () => {
    expect(parseCombo('Ctrl+,')).toEqual({ ctrl: true, shift: false, alt: false, meta: false, key: ',' });
    expect(parseCombo('Ctrl+\\')!.key).toBe('\\');
    expect(parseCombo('Ctrl+/')!.key).toBe('/');
  });

  it('修饰键别名归一，大小写不敏感', () => {
    expect(parseCombo('cmd+shift+space')).toEqual({
      ctrl: false,
      shift: true,
      alt: false,
      meta: true,
      key: 'Space',
    });
    expect(parseCombo('CONTROL+A')!.ctrl).toBe(true);
    expect(parseCombo('Option+A')!.alt).toBe(true);
  });

  it('形状不合法一律拒绝：空串、只有修饰键、重复修饰键、主键后还有内容', () => {
    expect(parseCombo('')).toBeNull();
    expect(parseCombo('   ')).toBeNull();
    expect(parseCombo('Ctrl')).toBeNull();
    expect(parseCombo('Ctrl+')).toBeNull();
    expect(parseCombo('Ctrl+Ctrl+A')).toBeNull();
    expect(parseCombo('Ctrl+A+B')).toBeNull();
    expect(parseCombo(null)).toBeNull();
    expect(parseCombo(undefined)).toBeNull();
  });

  it('单键组合合法（输入框自建组合 Enter / Shift+Enter 走这条路）', () => {
    expect(parseCombo('Enter')).toEqual({ ctrl: false, shift: false, alt: false, meta: false, key: 'Enter' });
    expect(parseCombo('Shift+Enter')!.shift).toBe(true);
  });
});

describe('组合格式化与展示', () => {
  it('formatCombo 按 Ctrl/Cmd/Alt/Shift 固定顺序输出紧凑串', () => {
    expect(
      formatCombo({ ctrl: false, shift: true, alt: true, meta: true, key: 'Space' }),
    ).toBe('Cmd+Alt+Shift+Space');
    expect(formatCombo(parseCombo('shift+ctrl+a')!)).toBe('Ctrl+Shift+a');
  });

  it('displayCombo 展开为页面口径，字母主键恢复大写，非法组合返回空串而不是原文', () => {
    expect(displayCombo('Ctrl+,')).toBe('Ctrl + ,');
    expect(displayCombo('Ctrl+Shift+Space')).toBe('Ctrl + Shift + Space');
    expect(displayCombo('Alt+S')).toBe('Alt + S');
    expect(displayCombo('Ctrl+n')).toBe('Ctrl + N'); // 存储口径小写，展示口径大写
    expect(displayCombo('Ctrl')).toBe('');
    expect(displayCombo('')).toBe('');
  });

  it('键名归一：空格统一为 Space，esc 统一为 Escape，长键名保留', () => {
    expect(comboFromEvent({ key: ' ', ctrlKey: true })).toBe('Ctrl+Space');
    expect(matchCombo({ key: ' ', ctrlKey: true }, 'Ctrl+Space')).toBe(true);
    expect(parseCombo('Ctrl+space')!.key).toBe('Space');
    expect(parseCombo('Ctrl+esc')!.key).toBe('Escape');
    expect(parseCombo('Ctrl+tab')!.key).toBe('Tab');
    expect(parseCombo('Ctrl+F12')!.key).toBe('F12');
  });
});

// ── 事件 → 组合，与精确匹配 ──────────────────────────────────────

describe('comboFromEvent', () => {
  it('只按下修饰键时返回 null（录制要等主键）', () => {
    expect(comboFromEvent({ key: 'Control', ctrlKey: true })).toBeNull();
    expect(comboFromEvent({ key: 'Shift', shiftKey: true })).toBeNull();
    expect(comboFromEvent({ key: 'Meta', metaKey: true })).toBeNull();
  });

  it('组合键按存储口径输出', () => {
    expect(comboFromEvent({ key: ',', ctrlKey: true })).toBe('Ctrl+,');
    expect(comboFromEvent({ key: ' ', ctrlKey: true, shiftKey: true })).toBe('Ctrl+Shift+Space');
    expect(comboFromEvent({ key: 'Enter', shiftKey: true })).toBe('Shift+Enter');
    expect(comboFromEvent({ key: 'Tab', ctrlKey: true })).toBe('Ctrl+Tab');
  });
});

describe('matchCombo 修饰键逐项一致', () => {
  it('多按一个修饰键就不算命中', () => {
    expect(matchCombo({ key: 'g', ctrlKey: true }, 'Ctrl+G')).toBe(true);
    expect(matchCombo({ key: 'g', ctrlKey: true, shiftKey: true }, 'Ctrl+G')).toBe(false);
    expect(matchCombo({ key: 'g', ctrlKey: true, altKey: true }, 'Ctrl+G')).toBe(false);
    expect(matchCombo({ key: 'g' }, 'Ctrl+G')).toBe(false);
  });

  it('Cmd 与 Ctrl 不互换', () => {
    expect(matchCombo({ key: 'n', metaKey: true }, 'Ctrl+N')).toBe(false);
    expect(matchCombo({ key: 'n', metaKey: true }, 'Cmd+N')).toBe(true);
  });

  it('主键大小写不敏感，纯修饰键事件永不命中', () => {
    expect(matchCombo({ key: 'N', ctrlKey: true }, 'Ctrl+n')).toBe(true);
    expect(matchCombo({ key: 'Control', ctrlKey: true }, 'Ctrl')).toBe(false);
    expect(matchCombo({ key: 'a', ctrlKey: true }, '')).toBe(false);
    expect(matchCombo({ key: 'a', ctrlKey: true }, 'Ctrl')).toBe(false);
  });
});

// ── 输入框内的让位规则 ────────────────────────────────────────────

describe('isEditableTarget', () => {
  it('文本输入位置判定为编辑态，按钮类/复选类 input 不算', () => {
    expect(isEditableTarget({ tagName: 'TEXTAREA' })).toBe(true);
    expect(isEditableTarget({ tagName: 'textarea' })).toBe(true);
    expect(isEditableTarget({ tagName: 'INPUT', type: 'text' })).toBe(true);
    expect(isEditableTarget({ tagName: 'INPUT' })).toBe(true); // type 省略默认 text
    expect(isEditableTarget({ tagName: 'INPUT', type: 'checkbox' })).toBe(false);
    expect(isEditableTarget({ tagName: 'INPUT', type: 'RANGE' })).toBe(false);
    expect(isEditableTarget({ tagName: 'SELECT' })).toBe(true);
    expect(isEditableTarget({ tagName: 'DIV', isContentEditable: true })).toBe(true);
    expect(isEditableTarget({ tagName: 'DIV' })).toBe(false);
    expect(isEditableTarget(null)).toBe(false);
    expect(isEditableTarget(undefined)).toBe(false);
  });
});

describe('shouldSkipInEditor', () => {
  it('带 Ctrl/Cmd 的组合在输入时仍生效，裸键与 Shift 组合让位给文本', () => {
    expect(shouldSkipInEditor('Ctrl+F')).toBe(false);
    expect(shouldSkipInEditor('Cmd+N')).toBe(false);
    expect(shouldSkipInEditor('Enter')).toBe(true);
    expect(shouldSkipInEditor('Shift+Enter')).toBe(true);
    expect(shouldSkipInEditor('K')).toBe(true);
    expect(shouldSkipInEditor('Ctrl')).toBe(true); // 非法组合：保守跳过
    expect(shouldSkipInEditor(null)).toBe(true);
  });
});

// ── 生效组合、冲突与改键校验 ─────────────────────────────────────

describe('effectiveCombos', () => {
  it('没有改键时输出全部出厂组合，且不含输入框自建项', () => {
    const combos = effectiveCombos();
    expect(Object.keys(combos).sort()).toEqual(IN_APP.map((a) => a.id).sort());
    expect(combos.openSettings).toBe('Ctrl+,');
    expect(combos.toggleTaskMonitor).toBe('Ctrl+/');
    for (const a of BUILTIN) expect(combos[a.id]).toBeUndefined();
  });

  it('改键优先于默认；非法改键回落默认而不是留下坏组合', () => {
    const combos = effectiveCombos({ newTask: 'Cmd+Shift+N', toggleSidebar: 'Ctrl' });
    expect(combos.newTask).toBe('Cmd+Shift+n');
    expect(combos.toggleSidebar).toBe('Ctrl+\\');
  });

  it('输出统一为紧凑存储口径（用户录成 ctrl + shift + f 也能匹配）', () => {
    expect(effectiveCombos({ searchInTask: 'ctrl + shift + f' }).searchInTask).toBe('Ctrl+Shift+f');
  });
});

describe('findConflicts', () => {
  it('同一组合被两条能力占用才算冲突，大小写视为同一组合', () => {
    expect(findConflicts({ newTask: 'Ctrl+N', openSettings: 'Ctrl+,' })).toEqual([]);
    expect(findConflicts({ newTask: 'Ctrl+N', openSettings: 'ctrl+n' })).toEqual([
      { combo: 'ctrl+n', ids: ['newTask', 'openSettings'] },
    ]);
  });
});

describe('validateOverride', () => {
  it('形状不合法时给出可操作的提示', () => {
    expect(validateOverride('newTask', 'Ctrl', effectiveCombos())).toMatch('修饰键');
    expect(validateOverride('newTask', '', effectiveCombos())).toMatch('修饰键');
  });

  it('撞车时点名占用方，合法改键返回 null', () => {
    const combos = effectiveCombos();
    expect(validateOverride('newTask', combos.openSettings, combos)).toBe(
      '该组合已被「打开设置」占用，请换一个',
    );
    expect(validateOverride('newTask', 'Cmd+Shift+N', combos)).toBeNull();
  });

  it('改回自己当前的组合不算撞自己', () => {
    const combos = effectiveCombos();
    expect(validateOverride('newTask', combos.newTask, combos)).toBeNull();
  });
});

// ── 命中检测 ──────────────────────────────────────────────────────

describe('matchShortcut', () => {
  const combos = effectiveCombos();

  it('按清单命中已登记组合', () => {
    expect(matchShortcut({ key: ',', ctrlKey: true }, combos, false)).toBe('openSettings');
    expect(matchShortcut({ key: '\\', ctrlKey: true }, combos, false)).toBe('toggleSidebar');
    expect(matchShortcut({ key: 'Tab', ctrlKey: true }, combos, false)).toBe('cycleTask');
  });

  it('输入框内：Ctrl 组合照常命中，裸键/Shift 组合不劫持', () => {
    expect(matchShortcut({ key: 'n', ctrlKey: true }, combos, true)).toBe('newTask');
    expect(matchShortcut({ key: 'Enter' }, combos, true)).toBeNull();
  });

  it('未登记（未注册或用户未改键）的组合不返回 id', () => {
    expect(matchShortcut({ key: 'p', ctrlKey: true }, combos, false)).toBeNull();
    expect(matchShortcut({ key: 'n', ctrlKey: true }, {}, false)).toBeNull();
  });

  it('清单里 scope=builtin 的能力永不参与应用注册', () => {
    const withBuiltin = { ...combos, sendMessage: 'Enter', newline: 'Shift+Enter' };
    expect(matchShortcut({ key: 'Enter' }, withBuiltin, false)).not.toBe('sendMessage');
  });
});

// ── 注册中心：未注册的能力不吞按键 ───────────────────────────────

describe('dispatchShortcut', () => {
  it('命中已注册能力才拦截事件并执行', () => {
    let prevented = 0;
    let stopped = 0;
    let called = '';
    const combos = effectiveCombos();
    const handlers = { openSettings: () => (called = 'openSettings') };
    const e = {
      key: ',',
      ctrlKey: true,
      preventDefault: () => (prevented += 1),
      stopPropagation: () => (stopped += 1),
    };
    expect(dispatchShortcut(e, combos, handlers, { tagName: 'BODY' })).toBe(true);
    expect(called).toBe('openSettings');
    expect(prevented).toBe(1);
    expect(stopped).toBe(1);
  });

  it('命中但未注册（能力尚未提供）时放行，不动事件', () => {
    let prevented = 0;
    const combos = effectiveCombos();
    const e = { key: 'g', ctrlKey: true, preventDefault: () => (prevented += 1) };
    expect(dispatchShortcut(e, combos, {}, { tagName: 'BODY' })).toBe(false);
    expect(prevented).toBe(0);
  });

  it('输入框内打字既不执行也不拦截', () => {
    let called = 0;
    const combos = effectiveCombos({ searchInTask: 'F' });
    const e = { key: 'f', preventDefault: () => undefined };
    expect(dispatchShortcut(e, combos, { searchInTask: () => (called += 1) }, { tagName: 'INPUT', type: 'text' })).toBe(false);
    expect(called).toBe(0);
  });

  it('Ctrl 组合在输入框内照常生效（搜索/新任务不能因焦点在文本框而失灵）', () => {
    let called = 0;
    const combos = effectiveCombos();
    const e = { key: 'n', ctrlKey: true, preventDefault: () => undefined };
    expect(dispatchShortcut(e, combos, { newTask: () => (called += 1) }, { tagName: 'TEXTAREA' })).toBe(true);
    expect(called).toBe(1);
  });
});

describe('已注册能力清单', () => {
  it('与处理函数表一致，且都是清单里的 in-app 能力', async () => {
    const { registeredShortcutIds } = await import('../../../src/renderer/lib/use-app-shortcuts');
    const ids = registeredShortcutIds();
    const inAppIds = IN_APP.map((a) => a.id);
    for (const id of ids) expect(inAppIds).toContain(id);
    expect(new Set(ids).size).toBe(ids.length);
    // 搜索/查找已落地为真实能力，必须出现在注册表里
    expect(ids).toContain('searchAllTasks');
    expect(ids).toContain('searchInTask');
    // 清单里所有 in-app 能力都真的有处理函数，不设按了没反应的装饰项
    expect([...ids].sort()).toEqual([...inAppIds].sort());
  });
});

// ── 能力清单本身的口径 ────────────────────────────────────────────

describe('SHORTCUT_ACTIONS', () => {
  it('id 与 label 唯一，展示分组稳定', () => {
    const ids = SHORTCUT_ACTIONS.map((a) => a.id);
    const labels = SHORTCUT_ACTIONS.map((a) => a.label);
    expect(new Set(ids).size).toBe(ids.length);
    expect(new Set(labels).size).toBe(labels.length);
    expect([...new Set(SHORTCUT_ACTIONS.map((a) => a.group))]).toEqual(['通用', '任务', '会话']);
  });

  it('in-app 能力必有可解析的默认组合，builtin 必说明原因', () => {
    for (const a of IN_APP) expect(parseCombo(a.defaultCombo)).not.toBeNull();
    for (const a of BUILTIN) {
      expect(a.note).toBeTruthy();
      expect(shouldSkipInEditor(a.defaultCombo)).toBe(true);
    }
  });

  it('出厂组合互不撞车（否则注册中心会静默失效）', () => {
    expect(findConflicts(effectiveCombos())).toEqual([]);
  });
});

// ── 持久化契约：改键落 settings_kv 台账 ──────────────────────────

describe('shortcuts 设置字段', () => {
  it('契约里声明为 id→组合 台账，默认值为空表', () => {
    expect(contracts()).toContain('shortcuts: Record<string, string>;');
    expect(alignmentApi()).toContain('shortcuts: {},');
  });

  it('设置页通过 updateAppSettings 写入 shortcuts（而非仅本地 state）', () => {
    const page = settingsPage();
    expect(page).toContain("updateAppSettings({ shortcuts: next })");
    expect(page).toMatch(/delete next\[a\.id\];\s*\n\s*persist\(next\);/);
  });
});

// ── 源码接线：不再有装饰性快捷键 ─────────────────────────────────

describe('源码接线', () => {
  it('MainLayout 挂载全局注册中心，并真实消费侧边栏折叠状态', () => {
    const src = mainLayout();
    expect(src).toContain("useAppShortcuts()");
    expect(src).toContain("s.sidebarCollapsed");
    expect(src).toContain("s.toggleSidebar");
  });

  it('Workbench 的任务监控显隐改用 app store（快捷键与按钮共用一份状态）', () => {
    const src = workbench();
    expect(src).toContain("s.taskMonitorOpen");
    expect(src).toContain("s.setTaskMonitorOpen");
    expect(src).toContain("import { useAppStore }");
    expect(src).not.toContain('const [monitorOpen, setMonitorOpen] = React.useState');
  });

  it('app store 提供任务监控开关动作', () => {
    const src = appStore();
    expect(src).toContain('taskMonitorOpen: boolean;');
    expect(src).toContain('taskMonitorOpen: true,');
    expect(src).toContain('toggleTaskMonitor: () => {');
    expect(src).toContain('setTaskMonitorOpen: (open: boolean) => {');
  });

  it('快捷键页渲染自能力清单，不再写死组合表', () => {
    const page = settingsPage();
    expect(page).not.toContain("['打开设置', 'Ctrl + ,']");
    expect(page).not.toContain("['发送消息', 'Enter 或 Ctrl + Enter']");
    expect(page).toContain('SHORTCUT_ACTIONS.filter((a) => a.group === group)');
    expect(page).toContain('effectiveCombos(overrides)');
    expect(page).toContain('registeredShortcutIds()');
  });

  it('录制入口有 onClick，Esc 取消、撞车当场拒绝', () => {
    const page = settingsPage();
    expect(page).toContain('onClick={status.recordable ? () => { setNotice(\'\'); setRecording(isRecording ? \'\' : a.id); } : undefined}');
    expect(page).toContain("if (e.key === 'Escape')");
    expect(page).toContain('validateOverride(id, combo, combos)');
    expect(page).toContain("window.addEventListener('keydown', onKey, true)");
    expect(page).toContain('恢复默认');
  });

  it('未注册能力如实展示状态，不自称可录制', () => {
    const page = settingsPage();
    expect(page).toContain("return { text: '未注册（能力尚未提供）', recordable: false };");
    expect(page).toContain("return { text: '输入框处理', recordable: false };");
    expect(page).toContain('sub={a.note}');
  });

  it('三处「点击录制」装饰入口已收口为只读的全局触发展示', () => {
    const page = settingsPage();
    expect(page).not.toContain('（点击录制）');
    expect([...page.matchAll(/<GlobalTriggerRow/g)].length).toBe(3);
    expect(page).toContain('const GlobalTriggerRow: React.FC<{ label: string; combo: string; sub?: string }>');
  });

  it('注册中心的监听在 bubble 阶段，录制在 capture 阶段抢先', () => {
    expect(hook()).toContain("window.addEventListener('keydown', onKeyDown);");
    expect(shortcutsLib()).toContain('export function dispatchShortcut');
  });

  it('匹配语义只在 shortcuts.ts 一处实现，注册中心只做派发', () => {
    const src = hook();
    expect(src).toContain('dispatchShortcut(');
    expect(src).toContain("from './shortcuts'");
    expect(src).not.toContain('matchCombo('); // 不在 hook 里重复实现匹配
    expect(src).not.toMatch(/e\.key === ','/);
  });

  it('Ctrl+G 面板挂在布局层，Ctrl+F 查找条挂在消息流外层', () => {
    const layout = mainLayout();
    expect(layout).toContain('import TaskSearchDialog');
    expect(layout).toContain('s.taskSearchOpen');
    expect(layout).toContain('s.setTaskSearchOpen');
    expect(layout).toContain('<TaskSearchDialog open={taskSearchOpen}');

    const src = workbench();
    expect(src).toContain('import TaskFindBar');
    expect(src).toContain('s.findBarOpen');
    expect(src).toContain('s.setFindBarOpen');
    expect(src).toContain('highlightTurn={highlightTurn}');
    // 查找条在滚动区之外占一行，滚动消息流时不会被滚走
    expect(src).toContain("display: 'flex', flexDirection: 'column', px: 3, pt: 2");
    expect(src).toContain("<Box sx={{ flex: 1, minHeight: 0, overflow: 'auto' }}>");
    const findBar = read('../../../src/renderer/components/TaskFindBar/index.tsx');
    expect(findBar).toContain("alignSelf: 'flex-end'");
    expect(findBar).not.toContain("position: 'absolute'");

    const store = appStore();
    expect(store).toContain('taskSearchOpen: boolean;');
    expect(store).toContain('findBarOpen: boolean;');
    expect(store).toContain('taskSearchOpen: false,');
    expect(store).toContain('findBarOpen: false,');
    expect(store).toContain('setTaskSearchOpen: (open: boolean) => {');
    expect(store).toContain('setFindBarOpen: (open: boolean) => {');
  });

  it('注册中心把两项搜索能力接到 store 的显隐开关', () => {
    const src = hook();
    expect(src).toContain('searchAllTasks: () => useAppStore.getState().setTaskSearchOpen(true)');
    expect(src).toContain('searchInTask: () => {');
    expect(src).toContain('useAppStore.getState().setFindBarOpen(true)');
  });

  it('会话内高亮锚定到轮次起点，保证无助手节点的那一轮也能定位', () => {
    const stream = read('../../../src/renderer/components/ChatStream/index.tsx');
    expect(stream).toContain('data-turn={currentTurn}');
    expect(stream).toContain('highlightTurn === currentTurn');
    expect(stream).toContain('highlightTurn === turnIdx');
    expect([...stream.matchAll(/data-turn=/g)].length).toBe(1); // 每轮只有一个锚点

    const findBar = read('../../../src/renderer/components/TaskFindBar/index.tsx');
    expect(findBar).toContain('[data-turn="${turn}"]');
    expect(findBar).toContain('onActiveTurn(null)'); // 关闭即清除高亮
  });
});
