/**
 * 工作台模式驱动 UI（模式胶囊 / 占位文案 / 右侧画布）
 *
 * 锁定三件事：
 * - 数据侧：每种模式只挑自己那类成功产物，失败/回滚/无关格式不上画布
 * - 源码侧：胶囊组在输入框上方、旧下拉已删除、占位文案随模式变化
 * - 诚实性：没有产物就空着，pptx 不假装能渲染，读取失败可重试；待审阅自动回到任务页
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import type { FileOp } from '../../../src/shared/types';
import { FileOpStatus, FileOpType } from '../../../src/shared/types';
import {
  canvasKindOf,
  pickCanvasArtifacts,
  MODE_CANVAS_EMPTY,
} from '../../../src/renderer/lib/mode-canvas';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const chatInput = () => read('../../../src/renderer/components/ChatInput/index.tsx');
const workbench = () => read('../../../src/renderer/pages/Workbench/index.tsx');
const taskMonitor = () => read('../../../src/renderer/components/TaskMonitor/index.tsx');
const modeCanvas = () => read('../../../src/renderer/components/ModeCanvas/index.tsx');
const workModeHook = () => read('../../../src/renderer/lib/use-work-mode.ts');

function op(srcPath: string, status = FileOpStatus.SUCCESS, id = srcPath): FileOp {
  return {
    id,
    taskId: 'task-1',
    op: FileOpType.WRITE,
    srcPath,
    status,
    fileName: srcPath.split(/[/\\]/).pop(),
  };
}

describe('按模式挑选画布产物', () => {
  it('设计只认 HTML 原型，忽略 Markdown / 幻灯片 / 失败产物', () => {
    const picked = pickCanvasArtifacts('design', [
      op('/work/原型.html'),
      op('/work/说明.md'),
      op('/work/演示.pptx'),
      op('/work/失败.html', FileOpStatus.FAILED),
      op('/work/回滚.htm', FileOpStatus.ROLLED_BACK),
      op('/work/进行中.html', FileOpStatus.RUNNING),
    ]);
    expect(picked.map((p) => p.srcPath)).toEqual(['/work/原型.html']);
  });

  it('幻灯片认 pptx 与 HTML 稿，保持交付顺序', () => {
    const picked = pickCanvasArtifacts('slides', [
      op('/work/汇报.pptx'),
      op('/work/备注.md'),
      op('/work/网页版.html'),
    ]);
    expect(picked.map((p) => p.srcPath)).toEqual(['/work/汇报.pptx', '/work/网页版.html']);
  });

  it('写作认 Markdown / 纯文本，扩展名大小写不敏感', () => {
    const picked = pickCanvasArtifacts('writing', [
      op('/work/周报.md'),
      op('/work/笔记.MARKDOWN'),
      op('/work/导出.txt'),
      op('/work/看板.xlsx'),
    ]);
    expect(picked.map((p) => p.srcPath)).toEqual([
      '/work/周报.md',
      '/work/笔记.MARKDOWN',
      '/work/导出.txt',
    ]);
  });

  it('通用模式没有画布；同一路径重复交付只留一件；空路径忽略', () => {
    expect(pickCanvasArtifacts('general', [op('/work/a.html')])).toEqual([]);
    const picked = pickCanvasArtifacts('design', [
      op('/work/a.html', FileOpStatus.SUCCESS, 'a1'),
      op('/work/a.html', FileOpStatus.SUCCESS, 'a2'),
      op('   '),
    ]);
    expect(picked).toHaveLength(1);
    expect(picked[0].id).toBe('a1');
  });

  it('渲染方式由扩展名决定：HTML / Markdown / 文本 / 外部应用', () => {
    expect(canvasKindOf('C:\\work\\index.html')).toBe('html');
    expect(canvasKindOf('/work/report.MD')).toBe('markdown');
    expect(canvasKindOf('/work/notes.txt')).toBe('text');
    expect(canvasKindOf('/work/deck.pptx')).toBe('external');
  });
});

describe('输入框模式胶囊组', () => {
  it('胶囊组只在新任务（非 compact）出现，设置页只启用一个模式时也不渲染', () => {
    const src = chatInput();
    const capsuleIdx = src.indexOf('工作台模式胶囊组');
    const cardIdx = src.indexOf("bgcolor: 'var(--tt-surface)'", capsuleIdx);
    expect(capsuleIdx).toBeGreaterThan(-1);
    expect(cardIdx).toBeGreaterThan(capsuleIdx);
    expect(src).toContain('{!compact && enabledModeDefs.length > 1 && (');
    expect(src).toContain('data-mode={d.id}');
    expect(src).toContain('aria-pressed={selected}');
    expect(src).toContain('onClick={() => setWorkMode(d.id as WorkMode)}');
  });

  it('工具栏旧下拉与菜单已删除，模式只保留胶囊入口', () => {
    const src = chatInput();
    expect(src).not.toContain('modeAnchor');
    expect(src).not.toContain('modeLabel');
  });

  it('占位文案随模式变化，通用模式保留调用方文案', () => {
    const src = chatInput();
    expect(src).toContain("const modePlaceholder = activeMode === 'general' ? placeholder : MODE_PLACEHOLDERS[activeMode];");
    expect(src).toContain('placeholder={loading ? tr(\'AI 正在处理中…\') : tr(modePlaceholder)}');
    const hook = workModeHook();
    expect(hook).toContain("design: '描述要设计的页面或原型");
    expect(hook).toContain("slides: '描述演示主题与页数");
    expect(hook).toContain("writing: '描述要写的文档");
  });

  it('模式解析与设置页同源，选项仍寄存在 workbenchStore', () => {
    const hook = workModeHook();
    expect(hook).toContain('useAppSettings()');
    expect(hook).toContain('useWorkbenchStore((s) => s.workMode)');
    expect(hook).toContain("const DEFAULT_ENABLED_MODES = ['general', 'design', 'slides'];");
    expect(hook).toContain('if (!workMode || !enabledModes.includes(workMode)) setWorkMode(fallbackMode);');
    expect(chatInput()).toContain('const { activeMode, enabledDefs: enabledModeDefs, setWorkMode } = useWorkMode();');
  });
});

describe('右侧画布接入', () => {
  it('工作台把当前模式与画布传给右侧面板', () => {
    const src = workbench();
    expect(src).toContain('const { activeMode } = useWorkMode();');
    expect(src).toContain('mode={activeMode}');
    expect(src).toContain("canvas={activeMode === 'general' ? undefined : <ModeCanvas mode={activeMode} fileOps={fileOps} />}");
    expect(src).toContain('canvasLabel={activeMode === \'general\' ? undefined : tr(MODE_CANVAS_LABELS[activeMode])}');
  });

  it('任务面板非通用模式默认落在画布页，并能切回任务页', () => {
    const src = taskMonitor();
    expect(src).toContain("const [tab, setTab] = useState<'task' | 'canvas'>(hasCanvas ? 'canvas' : 'task');");
    expect(src).toContain("if (!hasCanvas) setTab('task');");
    expect(src).toContain("if (hasCanvas) {");
    expect(src).toContain("setTab('canvas');");
    expect(src).toContain("tab === 'canvas' && canvasLabel ? tr(canvasLabel) : tr('任务监控')");
    expect(src).toContain("tab === 'canvas' && canvas ? (");
  });

  it('待审阅时自动回到任务页，批准按钮不会被画布挡住', () => {
    const src = taskMonitor();
    const reviewIdx = src.indexOf('if (awaitingReview) setTab(\'task\');');
    expect(reviewIdx).toBeGreaterThan(-1);
    expect(src).toContain('const awaitingReview = plan?.status === PlanStatus.PENDING_REVIEW && !plan.reviewed;');
    // 审阅操作仍在任务页正文里，自动切页后用户能看到批准/拒绝按钮
    expect(src).toContain("{tr('批准并执行')}");
    expect(src).toContain("{tr('拒绝')}");
  });
});

describe('画布内容的诚实性', () => {
  it('HTML 走沙箱 iframe，允许脚本但不给宿主同源权限', () => {
    expect(modeCanvas()).toContain('sandbox="allow-scripts allow-forms"');
    expect(modeCanvas()).toContain('srcDoc={current?.content ?? \'\'}');
    expect(modeCanvas()).not.toContain('allow-same-origin');
  });

  it('内容来自真实 fileAPI.read，加载/失败/重试都有可见状态', () => {
    const src = modeCanvas();
    expect(src).toContain('fileAPI');
    expect(src).toContain('.read(path)');
    expect(src).toContain('正在读取产物…');
    expect(src).toContain("current?.status === 'error'");
    expect(src).toContain('重试');
    expect(src).not.toContain('示例文档');
    expect(src).not.toContain('这是 Mock');
  });

  it('没有产物时按模式显示空白画布，不伪造内容', () => {
    const src = modeCanvas();
    expect(src).toContain('MODE_CANVAS_EMPTY[mode]');
    expect(src).toContain("tr('产物生成后会在这里自动出现。')");
    expect(MODE_CANVAS_EMPTY.design).toContain('当前会话还没有');
    expect(MODE_CANVAS_EMPTY.slides).toContain('当前会话还没有');
    expect(MODE_CANVAS_EMPTY.writing).toContain('当前会话还没有');
  });

  it('pptx 如实说明应用内无法渲染，交给系统默认应用并回显失败', () => {
    const src = modeCanvas();
    expect(src).toContain("kind === 'external'");
    expect(src).toContain('演示文稿是二进制文件，应用内无法渲染预览。');
    expect(src).toContain('fileAPI.openExternal(selected.srcPath)');
    expect(src).toContain('系统默认程序未能打开该文件');
  });

  it('写作画布是只读预览，不摆一个存不了文件的编辑器', () => {
    const src = modeCanvas();
    expect(src).toContain('只读预览：画布不提供改写并保存文件的能力');
    expect(src).not.toContain('<textarea');
    expect(src).not.toContain('contentEditable');
  });
});

describe('欢迎区推荐案例与任务模板', () => {
  it('只在设计模式渲染；通用与幻灯片不显示', () => {
    const src = workbench();
    const guardIdx = src.indexOf("{mode === 'design' && (");
    expect(guardIdx).toBeGreaterThan(-1);
    // 推荐案例、模板入口、全量模板弹窗都在这道条件里
    expect(src.indexOf('推荐案例（换一换）', guardIdx)).toBeGreaterThan(guardIdx);
    expect(src.indexOf('浏览全部模板', guardIdx)).toBeGreaterThan(guardIdx);
    expect(src.indexOf('<Dialog', guardIdx)).toBeGreaterThan(guardIdx);
    // 模式来自工作台当前选择，而不是再读一份本地状态
    expect(src).toContain('mode={activeMode}');
    expect(src).toContain('mode: import(\'../../../shared/types\').WorkMode;');
  });
});
