/**
 * 界面数字与称呼的来源约束（第 1 批假数据清理的回归锁）
 *
 * 规则很简单：屏幕上出现的数字要么有真实来源，要么就别显示。以下四处此前都是编出来的：
 * - 工作台问候语固定写死「，张先生」（未登录也照样喊）
 * - 推荐案例文案称「海量高品质任务模板」（实际是内置固定条数）
 * - 卡片副标题写「GMV 8,436,550」（一个像真数据的假数字）
 * - 模型档位标称 1.1x / 0.25x / 0.1x 价格倍率（无计费依据，且价格会变）
 * 套件广场示例目录的技能数/版本/热度同理由 tests/unit/suite 与本页共同锁住。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const workbench = () => read('../../../src/renderer/pages/Workbench/index.tsx');
const chatInput = () => read('../../../src/renderer/components/ChatInput/index.tsx');
const chatModels = () => read('../../../src/renderer/lib/chat-models.ts');
const suitePage = () => read('../../../src/renderer/pages/ExpertSuite/index.tsx');
const extensionsPage = () => read('../../../src/renderer/pages/Extensions/index.tsx');

describe('称呼与数量必须来自真实来源', () => {
  it('问候语的称呼取登录态，未登录不凭空喊人', () => {
    const src = workbench();
    expect(src).not.toContain('，张先生');
    expect(src).toContain("useAuthStore((s) => (s.loggedIn ? s.profile?.nickname?.trim() ?? '' : ''))");
    expect(src).toContain("{nickname ? tr('，{name}', { name: nickname }) : ''}");
  });

  it('模板数量说真话：显示内置条数而不是「海量」', () => {
    const src = workbench();
    expect(src).not.toContain('海量高品质任务模板');
    expect(src).toContain("tr('内置 {count} 个任务模板，一键复制，快速发起任务。', { count: SHOWCASE_POOL.length })");
  });

  it('卡片副标题不留看起来像真实业务数据的假数字', () => {
    const src = workbench();
    expect(src).not.toMatch(/GMV \d{3}/);
    expect(src).toContain("subtitle: 'Excel · 核心指标'");
  });

  it('模型档位不标价格倍率（价格属时效事实，不写死在界面）', () => {
    const src = chatInput();
    expect(src).not.toMatch(/·\s*[\d.]+x/);
    // 档位定义与提交绑定共用一份（chat-models.ts），避免展示与执行各写一套
    expect(chatModels()).toContain("desc: '适合简单任务'");
    expect(chatModels()).not.toMatch(/·\s*[\d.]+x/);
    expect(src).toContain('档位只描述能力定位，不标价格倍率');
  });
});

describe('套件示例目录不装作真实数据', () => {
  it('广场按来源如实标注：真实市场 / 本地示例 / 未配置', () => {
    const src = suitePage();
    expect(src).toContain('不是在线套件市场');
    // 页签与空态都按 catalogSource 分支，示例目录不冒充在线市场
    expect(src).toContain("catalogSource === 'market' ? '在线市场' : '示例目录'");
    expect(src).not.toContain('套件广场 ${catalog.length}');
    expect(src).toContain('在线市场 · 数字取索引声明');
  });

  it('广场示例行不显示编造的技能数与版本号，市场行数字取索引声明', () => {
    const src = suitePage();
    expect(src).toContain('示例目录 · 未接入远端套件源');
    // 统计行：示例行只有提示，市场/已安装行才渲染数字
    const stats = src.split('统计行')[1].split('版本 + 安装/卸载')[0];
    expect(stats).toContain("s.source !== 'sample' ?");
    expect(stats.match(/\{s\.skillCount\} 个技能/g)?.length).toBe(1);
    const version = src.split('版本 + 安装/卸载')[1].slice(0, 600);
    expect(version).toContain("s.source !== 'sample' && s.version");
    expect(version).toContain('{s.version}');
  });

  it('排序项叫「推荐」不叫「热门」（没有真实安装量数据）', () => {
    const src = suitePage();
    expect(src).toContain("['hot', '推荐']");
    expect(src).not.toContain("['hot', '热门']");
  });

  it('技能页如实说明市场需要配置索引源，下载走 SHA-256 校验', () => {
    const src = extensionsPage();
    expect(src).not.toContain('当前版本没有在线技能市场');
    expect(src).toContain('在配置索引源后从在线市场安装');
    expect(src).toContain('市场下载会校验 SHA-256');
  });

  it('套件指令正文真实进入模型执行面（不再只到候选就停）', () => {
    const src = suitePage();
    expect(src).toContain('选中后该指令正文随本轮提交下发引擎');
    expect(src).toContain('正文随本轮提交下发引擎');
    // 旧版「正文尚未接入」的假承诺已随执行面接通删除
    expect(src).not.toContain('套件包内的指令正文尚未下发引擎');
    expect(src).not.toContain('正文尚未接入模型执行面');
    expect(src).not.toContain('本地化安装后即可在对话框输入 / 调用其指令');
  });
});
