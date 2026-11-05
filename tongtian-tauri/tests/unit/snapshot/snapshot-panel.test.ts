// tests/unit/snapshot/snapshot-panel.test.ts — #140 快照面板的口径与令牌约束
//
// 这个仓库的单元测试没有 jsdom/React 渲染环境（vitest environment: 'node'），
// 界面这一层按本仓既有约定用**源码字面量**钉：钉的是「页面上的话是谁说的」
// 「用的是哪套令牌」，不是像素。真实渲染与交互仍未验收。

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

/** 去掉注释再扫：文件头的「任务 #140」里的数字串会撞上色值正则，别让它替代码说话 */
function stripComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '')
    .replace(/[ \t]+\/\/[^\n]*$/gm, '');
}

const panel = () => read('../../../src/renderer/components/AppSnapshot/index.tsx');
const page = () => read('../../../src/renderer/pages/AppSnapshot/index.tsx');
const globals = () => read('../../../src/renderer/styles/globals.css');

/** 组件里用到的每一个 `var(--tt-*)` 都必须在全局样式里声明过 */
function usedTokens(src: string): string[] {
  return [...new Set([...src.matchAll(/var\((--tt-[a-z0-9-]+)\)/g)].map((m) => m[1]))].sort();
}

describe('快照面板：话只从引擎与共享契约来', () => {
  it('标签取自共享镜像，不在组件里另立一份中文表', () => {
    const src = panel();
    expect(src).toContain("labelOf(SENSITIVITY_LABELS");
    expect(src).toContain("labelOf(STATUS_LABELS");
    expect(src).toContain("labelOf(RELAY_DENIAL_LABELS");
    // 引擎那五句状态标签不许在界面里被重写一遍（重写就是第二个事实源）
    expect(src).not.toContain("pending: '待确认");
    expect(src).not.toContain('confirmed:');
  });

  it('错误文案用引擎原句，代码单独摆（不互相覆盖）', () => {
    const src = panel();
    expect(src).toContain('isSnapshotApiError');
    expect(src).toContain('return errorTextOf(error, fallback)');
    expect(src).toContain('<Chip size="small" label={error.code} />');
  });

  it('面板说清三件边界：不截屏、正文不留副本、接力不碰浏览器', () => {
    const src = panel();
    expect(src).toContain('本面板不截屏');
    expect(src).toContain('正文只活在引擎进程内存里');
    expect(src).toContain('只判定，不操作浏览器');
    expect(src).toContain('清单不含正文');
  });

  it('机密级那句不含糊：没有绕过开关，截图勾选也改不了结论', () => {
    const src = panel();
    expect(src).toContain('机密级没有绕过开关');
    expect(src).toContain('勾选「带上截图」改不了结论');
    // 未验收清单是转述引擎的，不是界面自己发明的
    expect(src).toContain('snapshotUnverifiedViews');
    expect(src).toContain('未验收 · {item}');
  });

  it('确认与取消都把正文视图撤下来：正文不留在面板里', () => {
    const src = panel();
    expect(src.match(/setPreview\(null\)/g)?.length).toBeGreaterThanOrEqual(2);
    expect(src.match(/setDraftText\(''\)/g)?.length).toBeGreaterThanOrEqual(2);
  });

  it('面板只经共享客户端访问引擎，不自己发请求、不碰 IPC', () => {
    const src = panel();
    expect(src).toContain("from '../../lib/snapshot-api'");
    expect(src).not.toContain('engineFetch');
    expect(src).not.toContain('fetch(');
    expect(src).not.toContain('tongtianAPI');
    expect(src).not.toContain('.invoke(');
  });
});

describe('快照面板：只用既有令牌，不新增第二套颜色来源', () => {
  it('组件里没有硬编码色值', () => {
    const src = stripComments(panel());
    expect(src).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(src).not.toMatch(/\brgba?\(/);
    // 颜色一律经令牌或 MUI 主题，不出现内联十六进制/函数色
    expect(src).not.toMatch(/color:\s*'#/);
    expect(src).not.toMatch(/bgcolor:\s*'#/);
  });

  it('用到的每一个 --tt-* 令牌都在全局样式里声明过', () => {
    const declared = new Set([...globals().matchAll(/^\s*(--tt-[a-z0-9-]+):/gm)].map((m) => m[1]));
    const used = [...usedTokens(panel()), ...usedTokens(page())];
    expect(used.length).toBeGreaterThan(0);
    for (const token of used) {
      expect(declared.has(token), `globals.css 里没有 ${token}，界面不该凭空引用`).toBe(true);
    }
  });

  it('分级配色交给组件库的成对色调，面板自己不写色调', () => {
    const src = panel();
    // 三档各映射到组件库的一个色调（底与字成对由 DesignKit 给）
    expect(src).toContain("public: 'success'");
    expect(src).toContain("internal: 'warning'");
    expect(src).toContain("confidential: 'danger'");
    expect(src).toContain('sensitivityTone(');
    // 反向钉：映射表里不许再出现颜色令牌名——出现了就是「档→色」的第二个落点
    const mapping = /const SENSITIVITY_TONE[\s\S]*?\n\};/.exec(src);
    expect(mapping, '档位→色调的映射表找不到了').toBeTruthy();
    expect(mapping![0]).not.toContain('--tt-');
    // 柔和底全部经组件库：面板里一个 chip 令牌都不该剩
    expect(usedTokens(src).filter((token) => token.startsWith('--tt-chip')).length).toBe(0);
    // 面板底也不再自己抄形状：整块用组件库的 KitCard，连 `panelSx` 这个别名都不留
    expect(src).toContain('<KitCard>');
    expect(src).not.toContain('panelSx');
    expect(src).toContain('<KitFieldRow');
    expect(src).toContain('<KitRow');
    expect(src).toContain("from '../DesignKit'");
  });
});

describe('快照页面壳：只做装配，不改口径', () => {
  it('工作空间来自 store，会话过滤由用户填——页面不猜「你在跟哪个任务说话」', () => {
    const src = page();
    expect(src).toContain('getActiveWorkspaceId()');
    expect(src).toContain('<AppSnapshot workspaceId={workspaceId}');
    expect(src).not.toContain('taskId =');
  });

  it('页面壳如实写明挂载位不在本卡片的领地内', () => {
    const src = page();
    expect(src).toContain('路由注册位由界面归属方决定');
    expect(stripComments(src)).not.toContain('react-router');
    expect(stripComments(src)).not.toContain('createBrowserRouter');
  });

  it('页面壳不重复面板里的口径，也不自己发请求', () => {
    const src = stripComments(page());
    expect(src).not.toContain('engineFetch');
    expect(src).not.toContain('fetch(');
    expect(src).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
  });
});
