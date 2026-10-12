/**
 * 应用内快捷键注册中心（设置页「快捷键」的真实后端）
 *
 * 快捷键页原本只是一张静态表：9 条组合里除了输入框自己处理的 Enter/Shift+Enter，
 * 没有任何地方监听 keydown，「点击录制」也没有 onClick。本模块把这条链路补齐为真：
 *
 * - `SHORTCUT_ACTIONS` 是唯一的能力清单（默认组合、分组、是否可由用户改键）
 * - 用户改键落在设置台账 `shortcuts` 字段（与 Rust settings_kv 同一权威，刷新不丢）
 * - `matchCombo` 精确匹配修饰键组合，`isEditableTarget` + `shouldSkipInEditor` 保证在输入框里打字不会误触发
 * - 冲突（两条能力撞同一组合）在保存前就能算出来，不会静默覆盖
 *
 * 纯逻辑、不依赖 DOM，便于在 node 环境下做单元测试。
 */

/** in-app：本应用注册并执行；builtin：由输入框自身处理，不经过这里 */
export type ShortcutScope = 'in-app' | 'builtin';

export interface ShortcutAction {
  id: string;
  label: string;
  group: string;
  /** 出厂默认组合；null = 该能力尚未提供，不注册快捷键 */
  defaultCombo: string | null;
  scope: ShortcutScope;
  /** 不可改键 / 未注册的原因（页面如实展示，不伪造可录制状态） */
  note?: string;
}

/** 能力清单即展示顺序（设置页按 group 分组渲染） */
export const SHORTCUT_ACTIONS: readonly ShortcutAction[] = [
  { id: 'openSettings', label: '打开设置', group: '通用', defaultCombo: 'Ctrl+,', scope: 'in-app' },
  { id: 'toggleSidebar', label: '切换侧边栏', group: '通用', defaultCombo: 'Ctrl+\\', scope: 'in-app' },
  { id: 'toggleTaskMonitor', label: '切换任务监控', group: '通用', defaultCombo: 'Ctrl+/', scope: 'in-app' },
  { id: 'cycleTask', label: '快速切换任务', group: '任务', defaultCombo: 'Ctrl+Tab', scope: 'in-app' },
  { id: 'newTask', label: '创建新任务', group: '任务', defaultCombo: 'Ctrl+N', scope: 'in-app' },
  { id: 'searchAllTasks', label: '搜索全部任务', group: '任务', defaultCombo: 'Ctrl+G', scope: 'in-app' },
  { id: 'searchInTask', label: '在当前任务中搜索', group: '任务', defaultCombo: 'Ctrl+F', scope: 'in-app' },
  {
    id: 'sendMessage',
    label: '发送消息',
    group: '会话',
    defaultCombo: 'Enter',
    scope: 'builtin',
    note: '由输入框自身处理，不可改键',
  },
  {
    id: 'newline',
    label: '插入换行',
    group: '会话',
    defaultCombo: 'Shift+Enter',
    scope: 'builtin',
    note: '由输入框自身处理，不可改键',
  },
];

/** 用户改键台账：action id → 组合字符串 */
export type ShortcutOverrides = Record<string, string>;

interface ComboParts {
  ctrl: boolean;
  shift: boolean;
  alt: boolean;
  meta: boolean;
  key: string;
}

const MOD_ALIASES: Record<string, keyof Omit<ComboParts, 'key'>> = {
  ctrl: 'ctrl',
  control: 'ctrl',
  shift: 'shift',
  alt: 'alt',
  option: 'alt',
  meta: 'meta',
  cmd: 'meta',
  command: 'meta',
  super: 'meta',
  win: 'meta',
};

/** 键名归一：空格统一为 Space，其余长键名保留（Tab/Enter/Escape/ArrowUp…） */
function normalizeKey(raw: string): string {
  if (raw === ' ') return 'Space'; // 事件侧空格键：trim 后会变成空串，必须先判
  const t = raw.trim();
  if (!t) return '';
  if (t.toLowerCase() === 'space') return 'Space';
  if (t.length === 1) return t.toLowerCase();
  const lower = t.toLowerCase();
  if (lower === 'esc') return 'Escape';
  return t.charAt(0).toUpperCase() + t.slice(1);
}

/**
 * 组合字符串 → 结构化修饰键 + 主键。
 * 返回 null 表示形状不合法（空串、只有修饰键、重复修饰键、多余段）。
 */
export function parseCombo(raw: string | null | undefined): ComboParts | null {
  const text = (raw ?? '').trim();
  if (!text) return null;
  const parts: ComboParts = { ctrl: false, shift: false, alt: false, meta: false, key: '' };
  const tokens = text.split('+').map((t) => t.trim()).filter((t) => t !== '');
  if (!tokens.length) return null;
  for (let i = 0; i < tokens.length; i += 1) {
    const mod = MOD_ALIASES[tokens[i].toLowerCase()];
    const isLast = i === tokens.length - 1;
    if (mod && !isLast) {
      if (parts[mod]) return null; // 同一修饰键写两次
      parts[mod] = true;
      continue;
    }
    if (mod && isLast) return null; // 只有修饰键，按不下
    if (!isLast) return null; // 主键后面还有内容（如 Ctrl+A+B）
    const key = normalizeKey(tokens[i]);
    if (!key) return null;
    parts.key = key;
  }
  if (!parts.key) return null;
  return parts;
}

/** 结构化 → 存储口径（紧凑，如 'Ctrl+Shift+Space'） */
export function formatCombo(parts: ComboParts): string {
  const mods: string[] = [];
  if (parts.ctrl) mods.push('Ctrl');
  if (parts.meta) mods.push('Cmd');
  if (parts.alt) mods.push('Alt');
  if (parts.shift) mods.push('Shift');
  return [...mods, parts.key].join('+');
}

/** 存储口径 → 页面展示口径（'Ctrl+,' → 'Ctrl + ,'；字母主键恢复大写） */
export function displayCombo(raw: string | null | undefined): string {
  const parts = parseCombo(raw);
  if (!parts) return '';
  const mods: string[] = [];
  if (parts.ctrl) mods.push('Ctrl');
  if (parts.meta) mods.push('Cmd');
  if (parts.alt) mods.push('Alt');
  if (parts.shift) mods.push('Shift');
  const key = /^[a-z]$/.test(parts.key) ? parts.key.toUpperCase() : parts.key;
  return [...mods, key].join(' + ');
}

/** 事件的最小形状（不引 DOM 类型，node 测试里可直接造对象） */
export interface KeyEventLike {
  key: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
  target?: unknown;
}

/** 纯修饰键按下时没有主键，不参与匹配 */
const MODIFIER_KEYS = new Set(['Control', 'Shift', 'Alt', 'Meta', 'OS']);

/** 事件 → 存储口径组合；只按修饰键返回 null */
export function comboFromEvent(e: KeyEventLike): string | null {
  if (!e.key || MODIFIER_KEYS.has(e.key)) return null;
  return formatCombo({
    ctrl: Boolean(e.ctrlKey),
    shift: Boolean(e.shiftKey),
    alt: Boolean(e.altKey),
    meta: Boolean(e.metaKey),
    key: normalizeKey(e.key),
  });
}

/** 修饰键必须逐项一致：Ctrl+G 不能在 Ctrl+Shift+G 时误触发 */
export function matchCombo(e: KeyEventLike, combo: string | null | undefined): boolean {
  const parts = parseCombo(combo);
  if (!parts) return false;
  if (MODIFIER_KEYS.has(e.key)) return false;
  return (
    Boolean(e.ctrlKey) === parts.ctrl &&
    Boolean(e.shiftKey) === parts.shift &&
    Boolean(e.altKey) === parts.alt &&
    Boolean(e.metaKey) === parts.meta &&
    normalizeKey(e.key) === parts.key
  );
}

/** 事件目标的最小形状 */
export interface TargetLike {
  tagName?: string;
  isContentEditable?: boolean;
  type?: string;
}

/** 正在输入文本的位置（含富文本与下拉） */
export function isEditableTarget(target: TargetLike | null | undefined): boolean {
  if (!target) return false;
  const tag = (target.tagName ?? '').toUpperCase();
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag === 'INPUT') {
    const type = (target.type ?? 'text').toLowerCase();
    return !['checkbox', 'radio', 'range', 'button', 'submit', 'reset', 'file', 'color'].includes(type);
  }
  return Boolean(target.isContentEditable);
}

/**
 * 输入框内是否跳过该组合。
 * 带 Ctrl/Cmd 的（Ctrl+F 搜索、Ctrl+N 新任务）在输入时仍要生效，
 * 裸键与 Shift+ 组合（如 Enter、Shift+Enter、单键）让位给正在输入的内容。
 */
export function shouldSkipInEditor(combo: string | null | undefined): boolean {
  const parts = parseCombo(combo);
  if (!parts) return true;
  return !parts.ctrl && !parts.meta;
}

/** 生效组合：用户改键优先，未改键用默认 */
export function effectiveCombos(overrides?: ShortcutOverrides | null): Record<string, string> {
  const out: Record<string, string> = {};
  for (const action of SHORTCUT_ACTIONS) {
    if (action.scope !== 'in-app' || !action.defaultCombo) continue;
    const custom = overrides?.[action.id];
    const combo = custom && parseCombo(custom) ? custom : action.defaultCombo;
    if (parseCombo(combo)) out[action.id] = formatCombo(parseCombo(combo)!);
  }
  return out;
}

export interface ShortcutConflict {
  combo: string;
  ids: string[];
}

/** 同一组合被两条能力占用（保存改键前就要拦住，避免静默失效） */
export function findConflicts(combos: Record<string, string>): ShortcutConflict[] {
  const byCombo = new Map<string, string[]>();
  for (const [id, combo] of Object.entries(combos)) {
    const key = combo.toLowerCase();
    byCombo.set(key, [...(byCombo.get(key) ?? []), id]);
  }
  return [...byCombo.entries()]
    .filter(([, ids]) => ids.length > 1)
    .map(([combo, ids]) => ({ combo, ids }));
}

/** 改键校验：形状合法 + 不与其它能力相撞；返回 null 表示可以保存 */
export function validateOverride(
  id: string,
  combo: string,
  combos: Record<string, string>,
): string | null {
  if (!parseCombo(combo)) return '快捷键需至少包含一个修饰键与一个主键，如 Ctrl+Shift+Space';
  const clash = Object.entries(combos).find(
    ([other, value]) => other !== id && value.toLowerCase() === combo.toLowerCase(),
  );
  if (clash) {
    const label = SHORTCUT_ACTIONS.find((a) => a.id === clash[0])?.label ?? clash[0];
    return `该组合已被「${label}」占用，请换一个`;
  }
  return null;
}

/** 命中检测：按清单顺序返回第一个匹配的能力 id */
export function matchShortcut(
  e: KeyEventLike,
  combos: Record<string, string>,
  editable: boolean,
): string | null {
  for (const action of SHORTCUT_ACTIONS) {
    if (action.scope !== 'in-app') continue;
    const combo = combos[action.id];
    if (!combo) continue;
    // 输入中只放行带 Ctrl/Cmd 的组合，其余交给文本框
    if (editable && shouldSkipInEditor(combo)) continue;
    if (matchCombo(e, combo)) return action.id;
  }
  return null;
}

/** 派发所需的最小事件形状（不引 DOM 类型，node 测试里可直接造对象） */
export interface ShortcutEventLike extends KeyEventLike {
  preventDefault?: () => void;
  stopPropagation?: () => void;
}

/**
 * 派发一次按键：命中且该能力已注册（有处理函数）→ 拦截默认行为并执行，返回 true。
 * 命中但未注册时返回 false 且**不动事件**，否则会把浏览器或输入框自己的按键也吃掉。
 */
export function dispatchShortcut(
  e: ShortcutEventLike,
  combos: Record<string, string>,
  handlers: Record<string, () => void>,
  target?: TargetLike | null,
): boolean {
  const id = matchShortcut(
    {
      key: e.key,
      ctrlKey: e.ctrlKey,
      shiftKey: e.shiftKey,
      altKey: e.altKey,
      metaKey: e.metaKey,
    },
    combos,
    isEditableTarget(target),
  );
  if (!id) return false;
  const handler = handlers[id];
  if (!handler) return false;
  e.preventDefault?.();
  e.stopPropagation?.();
  handler();
  return true;
}
