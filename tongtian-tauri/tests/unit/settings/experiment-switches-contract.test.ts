/**
 * 五个实验开关的真实归属（禁止「存了但不生效」）
 *
 * 实验特性原先只是把布尔值写进 settings，渲染层一个都没消费。现在：
 * - collapse / generativeUI 在 ChatStream 真实生效
 * - filter 在 Sidebar 真实过滤（标题 + 创建时间）
 * - popWindow 接通独立任务窗口（Rust 建窗 + 会话恢复 + 弹窗态隐藏侧栏）
 * - browserRelay 没有执行链路，设置页禁用并写明原因
 * - 实验区里的「分享」不再弹「即将上线」，改为复制真实产物路径
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const chatStream = () => read('../../../src/renderer/components/ChatStream/index.tsx');
const sidebar = () => read('../../../src/renderer/components/Layout/Sidebar.tsx');
const settings = () => read('../../../src/renderer/pages/Settings/index.tsx');

describe('实验开关有真实消费方', () => {
  it('collapse / expandToolCalls：共享「默认展开」判定，手动切换优先', () => {
    const src = chatStream();
    expect(src).toContain('const expandToolCalls = Boolean(appSettings.expandToolCalls);');
    expect(src).toContain('const defaultProcessOpen = experiments.collapse === false || expandToolCalls;');
    expect(src).toContain('const processOpen = openTurns[turnIdx] ?? defaultProcessOpen;');
  });

  it('generativeUI：html 代码块进沙箱 iframe，且不带 allow-same-origin', () => {
    const src = chatStream();
    expect(src).toContain('const generativeUI = Boolean(experiments.generativeUI);');
    expect(src).toContain("lang === 'html' || lang === 'htm'");
    expect(src).toContain('srcDoc={code}');
    // 沙箱必须隔离父页面与本机存储：属性值只能是这两项（注释里出现同名字样不算）
    const sandbox = /sandbox="([^"]+)"/.exec(src)?.[1] ?? '';
    expect(sandbox).toBe('allow-scripts allow-forms');
  });

  it('filter：按标题与创建时间过滤侧栏任务，不存在的字段不猜', () => {
    const src = sidebar();
    expect(src).toContain('const filterEnabled = Boolean(experiments.filter);');
    expect(src).toContain("const [taskRange, setTaskRange] = useState<'all' | '7d' | '30d'>('all');");
    expect(src).toContain('c.createdAt < since');
    expect(src).toContain("(c.title || '新任务').toLowerCase().includes(q)");
    expect(src).toContain('visibleConversations.slice(0, 20)');
  });

  it('popWindow 接通独立窗口；browserRelay 如实禁用并写明缺什么', () => {
    const src = settings();
    expect(src).toContain('disabled={Boolean(d.unavailable)}');
    expect(src).toContain('（暂未提供）');
    expect(src).toContain('需安装配套浏览器扩展');
    expect(src).toContain('不是第二个应用进程');
    expect(src).toContain('独立窗口仅桌面版可用');
    // 独立窗口的真实链路：Rust 建窗命令已注册，IPC 已转发，前端头部入口可达
    expect(read('../../../src-tauri/src/system/task_window.rs')).toContain('pub async fn open_task_window');
    expect(read('../../../src-tauri/src/lib.rs')).toContain('system::task_window::open_task_window');
    expect(read('../../../src/renderer/lib/tauri-api.ts')).toContain("invoke('open_task_window'");
    const workbench = read('../../../src/renderer/pages/Workbench/index.tsx');
    expect(workbench).toContain("searchParams.get('conv')");
    expect(workbench).toContain('systemAPI.openTaskWindow');
    // 弹窗态隐藏侧边栏，只保留工作台
    expect(read('../../../src/renderer/components/Layout/MainLayout.tsx')).toContain("get('pop') === '1'");
  });

  it('实验区的分享动作复制真实产物路径，不再承诺未上线的分享', () => {
    const src = chatStream();
    expect(src).not.toContain('分享功能即将上线');
    expect(src).toContain('const copyArtifactPaths = (evts: ChatEvent[]) => {');
    expect(src).toContain("flatMap((e) => e.artifacts ?? [])");
    expect(src).toContain("tr('已复制 {count} 个产出文件路径，可直接粘贴发送', { count: paths.length })");
  });
});
