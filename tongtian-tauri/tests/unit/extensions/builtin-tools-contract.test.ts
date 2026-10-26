/**
 * 内置工具清单真实性契约（任务 #45）
 *
 * 「扩展 → 内置工具」原先写死五张卡片，其中「命令执行」「系统查询」「数据分析」
 * 在引擎里根本没有注册任何对应函数 —— 界面向用户宣传了应用做不到的能力
 * （「命令执行：在本地运行 Shell / CMD / PowerShell 命令」尤其危险）。
 *
 * 现在清单只有一处事实来源（引擎 Toolkit 注册表），三处声明必须逐条对齐：
 *   engine/server.py 的 register_tool_function(...)
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
const rustTools = () => read('../../../src-tauri/src/bridge/tools.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const page = () => read('../../../src/renderer/pages/Extensions/index.tsx');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const engineApi = () => read('../../../src/renderer/lib/engine-api.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const libApi = () => read('../../../src/renderer/lib/api.ts');

/**
 * 模式作用域工具：只在对应工作台模式下注册，不属于「常驻内置工具」清单。
 * generate_pptx 仅幻灯片模式出现（见下方专项断言），UI 的常驻清单不列它。
 */
const MODE_SCOPED_TOOLS = ['generate_pptx'] as const;

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
  it('引擎注册表就是三个文件工具，且没有别的', () => {
    expect(engineRegisteredTools().sort()).toEqual([
      'insert_text_file',
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
    const body = engine().split('def build_toolkit')[1].split('\ndef ')[0];
    // 必须挂在 if slides: 分支下，不能在函数体顶层无条件注册
    expect(body).toMatch(/if slides:\s*\n\s*toolkit\.register_tool_function\(generate_pptx\)/);
    const topLevel = body
      .split('\n')
      .filter((line) => /^    toolkit\.register_tool_function\(/.test(line))
      .join('\n');
    expect(topLevel).not.toContain('generate_pptx');
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
