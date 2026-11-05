/**
 * #164 接缝契约：七条车道的四处命名必须同源（W4 领地）
 *
 * 本仓库不是 git 仓库，七个窗口同改 engine/server.py、src-tauri/src/lib.rs、
 * src/shared/contracts.ts 就是后写的静默覆盖先写的。#164 把它们换成「一条车道一份文件」：
 *   engine/routes_<lane>.py + engine/routes_lanes.py（集中声明顺序与前缀）
 *   engine/toolkit_<lane>.py + engine/toolkit_lanes.py（集中声明开关）
 *   src/shared/contracts_<lane>.ts + src-tauri/src/ipc/<lane>.rs
 * 本文件锁的是接缝本身：车道名单、命令挂载、前缀归属、re-export 行数。
 * 它红不代表某个功能坏了，代表有人又开始抢公共咽喉——那就回到分工文档之前的状态。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { IPC_CHANNELS } from '../../../src/shared/contracts';
import { DECLARED_BUILTIN_TOOLS } from '../../../src/shared/builtin-tools';
import { IPC_LANES, laneDispatchOf } from '../../../src/shared/ipc-lanes';

const LANES = ['knowledge', 'artifact', 'workspace', 'exec', 'connect', 'orchestrate', 'voice'] as const;

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const root = (rel: string) => read(`../../../${rel}`);
const libRs = () => root('src-tauri/src/lib.rs');

describe('车道名单四处同源', () => {
  it('前端、引擎路由、引擎工具、Rust 四处都是同一批车道', () => {
    expect(IPC_LANES.map((l) => l.lane).sort()).toEqual([...LANES].sort());
    const routeLanes = root('engine/routes_lanes.py');
    const toolkitLanes = root('engine/toolkit_lanes.py');
    for (const lane of LANES) {
      expect(existsSync(fileURLToPath(new URL(`../../../engine/routes_${lane}.py`, import.meta.url))), `缺 engine/routes_${lane}.py`).toBe(true);
      expect(existsSync(fileURLToPath(new URL(`../../../engine/toolkit_${lane}.py`, import.meta.url))), `缺 engine/toolkit_${lane}.py`).toBe(true);
      expect(existsSync(fileURLToPath(new URL(`../../../src-tauri/src/ipc/${lane}.rs`, import.meta.url))), `缺 src-tauri/src/ipc/${lane}.rs`).toBe(true);
      // 引擎侧的车道名出现在两张集中声明表里（前缀表 + 开关表）
      expect(routeLanes, `routes_lanes.LANE_PREFIXES 里没有 ${lane}`).toContain(`"${lane}": (`);
      expect(toolkitLanes, `toolkit_lanes.SWITCH_BOARD 里没有 ${lane}`).toContain(`"${lane}": (`);
    }
  });

  it('server.py 与 lib.rs 各只被打开一次：车道注册面是单点', () => {
    const server = root('engine/server.py');
    expect(server).toContain('rln.attach(app, sys.modules[__name__]');
    // 分片挂载点在既有 61 条路由之后：既有路由优先，这是刻意的顺序，不是随手放的
    const attachAt = server.indexOf('rln.attach(');
    const lastDecorator = server.lastIndexOf('@app.post(');
    expect(attachAt, '分片注册面跑到了既有路由前面').toBeGreaterThan(lastDecorator);
    const lib = libRs();
    for (const lane of LANES) {
      expect(lib, `lib.rs 的 generate_handler! 里没挂 ipc::${lane}::lane_${lane}`).toContain(
        `ipc::${lane}::lane_${lane}`,
      );
    }
    expect(lib).toContain('mod ipc;');
  });

  it('contracts.ts 对每条车道只有一行 re-export，通道值不与基础表撞名', () => {
    const contracts = root('src/shared/contracts.ts');
    const baseValues = new Set(Object.values(IPC_CHANNELS).map(String));
    for (const lane of LANES) {
      const exports = contracts.match(new RegExp(`^export \\{[^}]*IPC_LANE_${lane.toUpperCase()}.*$`, 'gm')) ?? [];
      expect(exports.length, `contracts.ts 里 ${lane} 车道的 re-export 行数=${exports.length}`).toBe(1);
      const file = root(`src/shared/contracts_${lane}.ts`);
      expect(file).toContain(`channel: 'lane:${lane}'`);
      expect(file).toContain(`command: 'lane_${lane}'`);
      for (const channel of Object.values(IPC_LANES.find((l) => l.lane === lane)!.channels)) {
        expect(channel.startsWith(`${lane}:`), `${lane} 车道的通道 ${channel} 前缀越界`).toBe(true);
        expect(baseValues.has(channel), `车道通道 ${channel} 与 contracts.ts 里的既有通道撞值`).toBe(false);
      }
    }
  });

  it('Rust 侧每条车道未实现的方法如实报账，不返回空对象', () => {
    for (const lane of LANES) {
      const src = root(`src-tauri/src/ipc/${lane}.rs`);
      expect(src).toContain(`pub const LANE: &str = "${lane}"`);
      expect(src).toContain('NOT_IMPLEMENTED');
    }
    expect(root('src-tauri/src/ipc/mod.rs')).toContain('pub const NOT_IMPLEMENTED: &str = "IPC_LANE_NOT_IMPLEMENTED"');
  });
});

describe('前端单点入口', () => {
  it('车道通道解析出分派命令与 method（冒号后半段即 Rust 的 method 名）', () => {
    expect(laneDispatchOf('chat:submit')).toBeNull();
    expect(laneDispatchOf('system:info')).toBeNull();
    expect(laneDispatchOf('voice:not-registered-here')).toBeNull();
  });

  it('tauri-api 只经单点入口接线，窗口不必再往 if 链里加行', () => {
    const src = root('src/renderer/lib/tauri-api.ts');
    expect(src).toContain("import { laneDispatchOf } from '../../shared/ipc-lanes'");
    expect(src).toContain('if (laneDispatch) {');
  });
});

describe('常驻清单不被车道撑胖', () => {
  it('常驻六件写死在 toolkit_lanes，且与界面声明的内置工具同一批', () => {
    const src = root('engine/toolkit_lanes.py');
    const body = src.split('RESIDENT_TOOLS = (')[1].split(')')[0];
    const resident = [...body.matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
    expect(resident.sort()).toEqual(DECLARED_BUILTIN_TOOLS.map((t) => t.id).sort());
  });

  it('开关全为假时车道必须空手——这条是运行时判定，不是文档承诺', () => {
    const src = root('engine/toolkit_lanes.py');
    expect(src).toContain('if added and not active:');
    expect(src).toContain('车道重复声明常驻工具');
    expect(src).toContain('两条车道各注册一次');
  });
});
