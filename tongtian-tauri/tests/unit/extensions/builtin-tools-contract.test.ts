/**
 * 内置工具清单真实性契约（任务 #45）
 *
 * 「扩展 → 内置工具」原先写死五张卡片，其中「命令执行」「系统查询」「数据分析」
 * 在引擎里根本没有注册任何对应函数 —— 界面向用户宣传了应用做不到的能力
 * （「命令执行：在本地运行 Shell / CMD / PowerShell 命令」尤其危险）。
 *
 * 现在清单只有一处事实来源（引擎 Toolkit 注册表），三处声明必须逐条对齐：
 *   engine/server.py 的 register_tool_function(...)（常驻六件）
 *   engine/toolkit_lanes.py + engine/toolkit_<domain>.py（模式作用域件与授权件：#164 接缝）
 *   src-tauri/src/bridge/tools.rs 的 BUILTIN_TOOLS
 *   src/shared/builtin-tools.ts 的 DECLARED_BUILTIN_TOOLS
 * 本文件同时锁住通道接线与页面渲染，防止又退回写死的卡片。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { IPC_CHANNELS } from '../../../src/shared/contracts';
import { DECLARED_BUILTIN_TOOLS } from '../../../src/shared/builtin-tools';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const engine = () => read('../../../engine/server.py');
const toolkitLanes = () => read('../../../engine/toolkit_lanes.py');
const toolkitArtifact = () => read('../../../engine/toolkit_artifact.py');
const toolkitExec = () => read('../../../engine/toolkit_exec.py');
const rustTools = () => read('../../../src-tauri/src/bridge/tools.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const page = () => read('../../../src/renderer/pages/Extensions/index.tsx');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const engineApi = () => read('../../../src/renderer/lib/engine-api.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const libApi = () => read('../../../src/renderer/lib/api.ts');

/**
 * 条件注册工具：只在对应工作台模式下注册，不属于「常驻内置工具」清单。
 * generate_pptx 仅幻灯片模式出现；generate_xlsx / generate_docx（D-33）仅通用与写作模式出现。
 * 这三件都不进常驻清单——本文件末尾要求 harness 为清单里的每个工具给出实现，
 * 而 Office 产物生成在 Rust 侧没有实现，写进去就是界面上多一条空头承诺。
 */
const MODE_SCOPED_TOOLS = ['generate_pptx', 'generate_xlsx', 'generate_docx'] as const;

/** 引擎里真正常驻注册的工具函数名（排除模式作用域工具） */
function engineRegisteredTools(): string[] {
  const body = engine().split('def build_toolkit')[1].split('\ndef ')[0];
  return [...body.matchAll(/register_tool_function\((\w+)\)/g)]
    .map((m) => m[1])
    .filter((id) => !(MODE_SCOPED_TOOLS as readonly string[]).includes(id));
}

/** Rust 声明清单里的工具名 */
function rustDeclaredTools(): string[] {
  const body = rustTools().split('pub const BUILTIN_TOOLS')[1].split('];')[0];
  return [...body.matchAll(/\(\s*"([a-z_]+)"/g)].map((m) => m[1]);
}

describe('三处清单逐条对齐（漂移即红）', () => {
  it('引擎注册表就是六个文件工具，且没有别的', () => {
    expect(engineRegisteredTools().sort()).toEqual([
      'delete_text_file',
      'insert_text_file',
      'list_directory',
      'read_document',
      'view_text_file',
      'write_text_file',
    ]);
  });

  it('Rust 声明清单 == 引擎注册表', () => {
    expect(rustDeclaredTools().sort()).toEqual(engineRegisteredTools().sort());
  });

  it('TS 声明清单 == 引擎注册表（id 与展示名/说明都非空）', () => {
    expect(DECLARED_BUILTIN_TOOLS.map((t) => t.id).sort()).toEqual(engineRegisteredTools().sort());
    for (const t of DECLARED_BUILTIN_TOOLS) {
      expect(t.name.trim(), `${t.id} 缺展示名`).toBeTruthy();
      expect(t.desc.trim(), `${t.id} 缺说明`).toBeTruthy();
      expect(t.source).toBe('builtin');
    }
  });

  it('幻灯片工具只在幻灯片模式注册（不冒充常驻内置工具）', () => {
    // #164 之后条件注册不在 server.py 里内联：开关集中在 toolkit_lanes.SWITCH_BOARD，
    // 注册动作在 toolkit_artifact.register()。契约要锁的仍是同一件事——
    // 「只有挂着模式开关的分支才注册，常驻清单不因此变胖」。
    const body = engine().split('def build_toolkit')[1].split('\ndef ')[0];
    expect(body, 'build_toolkit 里不许再内联注册模式作用域工具').not.toMatch(
      /register_tool_function\((?:generate_\w+|self\._\w+_tool)\)/,
    );
    expect(toolkitArtifact()).toMatch(/if ctx\.slides:\s*\n\s*ctx\.toolkit\.register_tool_function\(ctx\.tool\("generate_pptx"\)\)/);
    expect(toolkitLanes()).toMatch(/"artifact": \("slides", "office"\)/);
  });

  it('办公产物工具只在通用/写作模式注册（同上，且默认装配里不出现）', () => {
    expect(toolkitArtifact()).toMatch(
      /if ctx\.office:\s*\n\s*ctx\.toolkit\.register_tool_function\(ctx\.tool\("generate_xlsx"\)\)\s*\n\s*ctx\.toolkit\.register_tool_function\(ctx\.tool\("generate_docx"\)\)/,
    );
    // 默认值必须是关：build_toolkit 的两处默认调用（/api/tools 与本文件的清单口径）
    // 一旦把 office 缺省打开，常驻清单就会凭空多出两件 Rust 没实现的东西
    expect(engine()).toMatch(/def build_toolkit\([^)]*office: bool = False/);
    expect(engine()).toContain('office=rc.normalize_mode(mode) in ("general", "writing")');
    // 开关全为假时车道必须一件都不注册——这条由运行时判定，不靠测试事后追
    expect(toolkitLanes()).toContain('这会把没有 Rust 实现的能力塞进常驻清单');
    // 授权工具同理：grants 未授权时 exec 车道必须空手
    expect(engine()).toMatch(/if grants is not None:/);
    expect(toolkitExec()).toMatch(/if ctx\.grants is not None:\s*\n\s*ctx\.grants\.install\(/);
    expect(toolkitLanes()).toMatch(/"exec": \("grants",\)/);
  });
});

describe('通道接线：清单要么实测、要么标明是声明值', () => {
  it('通道常量存在且三通道都有真实消费方', () => {
    expect(IPC_CHANNELS.AGENT_BUILTIN_TOOLS).toBe('agent:builtinTools');
    expect(tauriApi()).toContain("if (channel === 'agent:builtinTools') return invoke('agent_builtin_tools');");
    expect(engineApi()).toContain("if (channel === 'agent:builtinTools') return fetchBuiltinTools();");
    expect(engineApi()).toContain('${ENGINE_URL}/api/tools');
    expect(mockApi()).toContain("if (channel === 'agent:builtinTools') return declaredBuiltinToolList('mock');");
    expect(libApi()).toContain('builtinTools(): Promise<import(\'../../shared/contracts\').BuiltinToolList>');
  });

  it('Rust 命令已注册，并在引擎在跑时读实测注册表', () => {
    const lib = rustLib();
    expect(lib).toContain('bridge::tools::agent_builtin_tools,');
    const src = rustTools();
    expect(src).toContain('pub async fn agent_builtin_tools');
    expect(src).toContain('/api/tools');
    expect(src).toContain('"source": "engine"');
    // 引擎没跑时回落到声明清单，并说明来源
    expect(src).toContain('"source": "declared"');
    expect(read('../../../src-tauri/src/bridge/mod.rs')).toContain('pub mod tools;');
  });

  it('引擎端点存在，失败时报错而非返回空清单', () => {
    const src = engine();
    expect(src).toContain('@app.get("/api/tools")');
    expect(src).toContain('toolkit.tools.items()');
    expect(src).toContain('TOOLS_LIST_FAILED');
  });

  it('默认执行面（本地 harness）也实现了清单里的每个工具', () => {
    // 普通对话默认走本地 harness；它少实现一个，界面上的「内置工具」就是空头承诺
    const src = read('../../../src-tauri/src/harness/tools.rs');
    for (const id of DECLARED_BUILTIN_TOOLS.map((t) => t.id)) {
      expect(src, `harness 未声明 ${id}`).toContain(`"name": "${id}"`);
      expect(src, `harness 未分发 ${id}`).toContain(`"${id}" => ToolRun {`);
    }
  });
});

describe('页面不再宣传没有的能力', () => {
  it('写死的五张卡片与三个不存在的能力已删除', () => {
    const src = page();
    expect(src).not.toContain('const BUILTIN_TOOLS = [');
    for (const fake of ['命令执行', '系统查询', '数据分析']) {
      expect(src, `仍在宣传 ${fake}`).not.toContain(fake);
    }
    expect(src).not.toContain('在本地运行 Shell / CMD / PowerShell 命令');
  });

  it('清单来自通道读取，带加载/失败/来源三种状态', () => {
    const src = page();
    expect(src).toContain('await systemAPI.builtinTools()');
    expect(src).toContain("errorTextOf(e, '内置工具清单读取失败')");
    expect(src).toContain('正在读取内置工具清单…');
    expect(src).toContain('builtin.tools.map((t: BuiltinTool)');
    expect(src).toContain('TOOL_ICONS[t.id] ?? fallbackToolIcon');
    expect(src).toContain('{builtin.note ??');
    // 计数也来自清单，不再是数组长度写死
    expect(src).toContain('const builtinCount = builtin?.count ?? 0;');
    expect(src).toContain('内置工具 ${builtinCount}');
  });
});
