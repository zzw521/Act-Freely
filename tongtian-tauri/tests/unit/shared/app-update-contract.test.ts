/**
 * #159 D-12 更新回路的跨语言契约（W4 领地）
 *
 * 钉三件事，全是「改一边忘了改另一边」会真出事的形状：
 *   1. `exec:update_*` 的通道名 == Rust `METHODS` == `dispatch_in` 的 match 分支；
 *      少一边就是界面调到不存在的方法（车道回 `IPC_LANE_NOT_IMPLEMENTED`）或方法没通道可用。
 *   2. Rust 侧回话里摊出来的键，必须在本文件的 TypeScript 形状里对得上——
 *      更新回路是「按字段决定按钮亮不亮」的代码，字段名漂了就等于界面在读一个不存在的判定。
 *   3. `app_update/**` 抛出的每个 `UPDATE_*` 错误码都得在 `error-messages.ts` 里有中文文案；
 *      这条与 `error-surface-contract.test.ts` 同口径，但那一份是全仓扫描，这里额外把
 *      「谁抛的」钉到模块上：新增一个更新错误码却忘了登记文案时，报错要指向更新回路自己。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { IPC_CHANNELS_EXEC, IPC_LANE_EXEC } from '../../../src/shared/contracts_exec';
import { ERROR_MESSAGE_CODES } from '../../../src/shared/error-messages';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const modRs = () => read('../../../src-tauri/src/app_update/mod.rs');
const manifestRs = () => read('../../../src-tauri/src/app_update/manifest.rs');
const execRs = () => read('../../../src-tauri/src/ipc/exec.rs');

/** 取 `pub const METHODS: &[&str] = &[ ... ];` 里的字符串字面量 */
function rustMethods(): string[] {
  const body = modRs().split('pub const METHODS')[1] ?? '';
  const block = body.slice(0, body.indexOf('];'));
  return [...block.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
}

/** 取一个 `fn name(...) -> Value { json!({ ... }) }` 里的顶层键（只到第一个 `})` 结束） */
function jsonKeysOf(source: string, fnName: string): string[] {
  const at = source.indexOf(`fn ${fnName}(`);
  expect(at, `找不到 ${fnName}：Rust 侧改过函数名就要同步本用例`).toBeGreaterThanOrEqual(0);
  const start = source.indexOf('json!({', at);
  const body = source.slice(start, source.indexOf('\n}', start));
  const keys = [...body.matchAll(/"([A-Za-z][A-Za-z0-9_]*)"\s*:/g)].map((m) => m[1]);
  // 两侧都要有内容才比：解析失败会产出两个空数组，那样「相等」是假的
  expect(keys.length, `${fnName} 的 json! 没解析出键（写法变了？）`).toBeGreaterThan(3);
  return keys;
}

/** 取 TS `interface X { ... }` 的字段名 */
function tsFieldsOf(interfaceName: string): string[] {
  const src = read('../../../src/shared/contracts_exec.ts');
  const at = src.indexOf(`export interface ${interfaceName} {`);
  expect(at, `contracts_exec.ts 里没有 ${interfaceName}`).toBeGreaterThanOrEqual(0);
  // 逐字符找配对的右花括号：接口里有嵌套对象字面量，不能按第一个 `}` 截断
  let depth = 0;
  let end = -1;
  for (let i = at; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') {
      depth--;
      if (depth === 0) {
        end = i;
        break;
      }
    }
  }
  const body = src.slice(at, end);
  const fields = [...body.matchAll(/\n\s{2}([A-Za-z][A-Za-z0-9_]*)(\?)?:/g)].map((m) => m[1]);
  expect(end, `${interfaceName} 的花括号没配上`).toBeGreaterThan(at);
  expect(fields.length, `${interfaceName} 一个字段也没解析出来`).toBeGreaterThan(3);
  return fields;
}

describe('exec:update_* 通道与 Rust 方法表同源', () => {
  it('通道值全部落在 exec: 前缀下，且冒号后半段就是 method 名', () => {
    for (const [key, value] of Object.entries(IPC_CHANNELS_EXEC)) {
      expect(value, `${key} 必须以 exec: 开头`).toMatch(/^exec:/);
      expect(value.slice('exec:'.length), `${key} 的 method 段为空`).not.toBe('');
    }
  });

  it('Rust 的 METHODS 与前端通道一一对应（不多不少）', () => {
    const methods = rustMethods();
    const channels = Object.values(IPC_CHANNELS_EXEC).map((c) => c.slice('exec:'.length));
    expect(methods.length).toBeGreaterThan(0);
    expect(channels.sort()).toEqual([...methods].sort());
    expect(new Set(channels).size, '通道名重复').toBe(channels.length);
  });

  it('dispatch_in 里每个方法都真有分支接住', () => {
    const src = modRs();
    for (const m of rustMethods()) {
      expect(src, `${m} 在方法表里但没有 match 分支`).toContain(`"${m}" =>`);
    }
  });

  it('车道把自己认作 exec，并把非 update_ 的方法继续报未实现', () => {
    const src = execRs();
    expect(src).toContain('pub const LANE: &str = "exec"');
    expect(src).toContain('NOT_IMPLEMENTED');
    // 分流靠方法前缀，与 Rust 侧 METHOD_PREFIX 同一串
    expect(src).toContain('update_');
    expect(IPC_LANE_EXEC.command).toBe('lane_exec');
  });
});

describe('回话形状两侧对齐（Rust 摊什么，TS 就声明什么）', () => {
  it('staged_row 的键与 UpdateStagedRow 完全一致', () => {
    expect(tsFieldsOf('UpdateStagedRow').sort()).toEqual(jsonKeysOf(modRs(), 'staged_row').sort());
  });

  it('retained_row 的键与 UpdateRetainedRow 完全一致', () => {
    expect(tsFieldsOf('UpdateRetainedRow').sort()).toEqual(jsonKeysOf(modRs(), 'retained_row').sort());
  });

  it('anchor_view 的键与 UpdateAnchorView 完全一致', () => {
    expect(tsFieldsOf('UpdateAnchorView').sort()).toEqual(jsonKeysOf(modRs(), 'anchor_view').sort());
  });

  it('artifact_view 的键与 UpdateArtifactView 完全一致', () => {
    expect(tsFieldsOf('UpdateArtifactView').sort()).toEqual(
      jsonKeysOf(manifestRs(), 'artifact_view').sort(),
    );
  });

  it('channel_view 的键与 UpdateChannelView 完全一致', () => {
    expect(tsFieldsOf('UpdateChannelView').sort()).toEqual(jsonKeysOf(modRs(), 'channel_view').sort());
  });

  it('状态闭合表与 TS 的 UpdateState 联合类型同一批值', () => {
    const src = modRs();
    // 常量名 → 实际字符串：`pub const STATE_STAGED: &str = "staged";`
    const declared = [...src.matchAll(/pub const (STATE_[A-Z_]+): &str = "([^"]+)";/g)].map((m) => m[2]);
    const table = src.slice(
      src.indexOf('pub const STATES'),
      src.indexOf('];', src.indexOf('pub const STATES')),
    );
    const inTable = [...table.matchAll(/STATE_[A-Z_]+/g)].map((m) => m[0]);
    expect(declared.length, '闭合状态表里的常量与状态常量条数不一致').toBe(inTable.length);
    const u = read('../../../src/shared/contracts_exec.ts');
    const union = u.slice(u.indexOf('export type UpdateState'), u.indexOf(';', u.indexOf('export type UpdateState')));
    for (const value of declared) {
      expect(union, `状态「${value}」没进 TS 联合类型`).toContain(`'${value}'`);
    }
  });
});

describe('UPDATE_* 错误码全部有中文文案', () => {
  it('源码里出现的每个 UPDATE_ 码都在文案表登记（不靠全仓扫描兜底）', () => {
    const emitted = new Set<string>();
    for (const src of [modRs(), manifestRs()]) {
      for (const m of src.matchAll(/"(UPDATE_[A-Z_]+):/g)) emitted.add(m[1]);
    }
    expect(emitted.size, '一个 UPDATE_ 码都没扫到，八成是路径或写法变了').toBeGreaterThan(8);
    const registered = new Set<string>(ERROR_MESSAGE_CODES);
    const missing = [...emitted].filter((code) => !registered.has(code)).sort();
    expect(missing, `这些更新回路错误码没有中文文案：${missing.join(', ')}`).toEqual([]);
  });

  it('登记的反向核对：文案表里的 UPDATE_ 码确实在源码里抛得出', () => {
    const src = modRs() + manifestRs();
    const orphan = ERROR_MESSAGE_CODES.filter(
      (code) => code.startsWith('UPDATE_') && !src.includes(`"${code}`),
    ).sort();
    expect(orphan, `文案表里有源码从不抛出的码（会误导后来人）：${orphan.join(', ')}`).toEqual([]);
  });

  it('IPC_LANE_NOT_IMPLEMENTED 有文案：车道未实现的方法不能把裸码丢给界面', () => {
    expect(ERROR_MESSAGE_CODES).toContain('IPC_LANE_NOT_IMPLEMENTED');
  });
});
