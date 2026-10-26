/**
 * 内置工具声明清单（TS 侧唯一一份）。
 *
 * 事实来源是随包引擎的 Toolkit 注册表（`engine/server.py` 的 `build_toolkit`，
 * 经 `GET /api/tools` 读取）。这里那份是**声明值**：引擎没启动时（桌面端未拉起实例、
 * 浏览器演示态）界面也要能回答「这个应用给了模型哪些工具」。
 *
 * 三处清单（本文件、Rust `bridge::tools::BUILTIN_TOOLS`、引擎 register_tool_function）
 * 由 tests/unit/extensions/builtin-tools-contract.test.ts 逐条比对，漂移即测试红。
 *
 * 只列确实注册了的能力 —— 界面上原先的「命令执行 / 系统查询 / 数据分析」三张卡片
 * 在引擎里没有任何对应工具，属于假宣传，已删除。
 */

import type { BuiltinTool } from './contracts';

export const DECLARED_BUILTIN_TOOLS: BuiltinTool[] = [
  { id: 'view_text_file', name: '读取文件', desc: '读取工作目录内的文本文件（按行返回内容）', source: 'builtin' },
  { id: 'write_text_file', name: '写入文件', desc: '在工作目录内创建或覆盖文本文件', source: 'builtin' },
  { id: 'insert_text_file', name: '修改文件', desc: '在工作目录内按行插入或替换文本内容', source: 'builtin' },
];

/** 声明清单视图（source 标明这不是实测值） */
export function declaredBuiltinToolList(source = 'declared') {
  return {
    source,
    count: DECLARED_BUILTIN_TOOLS.length,
    tools: DECLARED_BUILTIN_TOOLS,
    note: '引擎未运行，按与引擎同源维护的声明清单显示（构建期契约测试锁定）',
  };
}
