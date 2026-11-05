/**
 * 「等你的工单」页面契约（缺陷 D-47.5）
 *
 * 这一页是 D-47 工单层在桌面上的唯一人类入口，页面对不对不重要，
 * 重要的是它有没有悄悄越出「人只做四个动作」的口径。钉住四件事：
 * - 路由与侧边栏已接线（点得到才算入口，lazy import 与 Route 成对出现）。
 * - 页面只调人类侧 api（回答/验收/改授权/叫停 + 两个只读）；报告、交回、
 *   领取、结束绑定、已读落账是执行体的动作，出现在人页上就是替执行体记账。
 * - 展示文案全部来自服务端视图字段（stageLabel/kindLabel/intensityLabel），
 *   页面里不许有 stage/kind → 中文的第二套字典（颜色归属不算文案）。
 * - 错误一律经 errorTextOf 换成「为什么被拒、下一步做什么」；不绕过 api 层
 *   直接 invoke，也不碰 IPC_CHANNELS 常量（通道是桥接层的事，页面不认识）。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const read = (rel: string) =>
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');

const page = () => read('../../../src/renderer/pages/AgentTickets/index.tsx');

describe('等你的工单页面（D-47.5）', () => {
  it('路由与侧边栏都已接线：入口点得到，且只有一条 /tickets 路由', () => {
    const app = read('../../../src/renderer/App.tsx');
    expect(app).toContain("const AgentTickets = lazy(() => import('./pages/AgentTickets'));");
    expect(app).toContain('<Route path="/tickets" element={<AgentTickets />} />');
    expect(app.match(/path="\/tickets"/g)?.length).toBe(1);

    const sidebar = read('../../../src/renderer/components/Layout/Sidebar.tsx');
    expect(sidebar).toContain("{ id: 'tickets', label: '等你的工单', path: '/tickets', icon: InboxIcon, color: 'amber' }");
  });

  it('只调人类侧动作与两个只读；执行体的五个动作不在页面上', () => {
    const src = page();
    for (const method of [
      'api.collab.ticketMyTickets',
      'api.collab.ticketDetail',
      'api.collab.ticketAnswer',
      'api.collab.ticketAccept',
      'api.collab.ticketAmend',
      'api.collab.ticketStop',
    ]) {
      expect(src).toContain(method);
    }
    // 领取/报告/交回/结束绑定/已读落账是 worker 侧命令；建单与订阅属于项目页与研判侧
    for (const forbidden of [
      'ticketClaim',
      'ticketReport',
      'ticketHandback',
      'ticketEndAssignment',
      'ticketMarkRead',
      'ticketCreate',
      'ticketSubscribe',
      'ticketList',
    ]) {
      expect(src).not.toContain(forbidden);
    }
  });

  it('叫停是唯一撤权、改范围是追加式：口径文案在页面上说清楚', () => {
    const src = page();
    expect(src).toContain('停止是唯一的撤权动作');
    expect(src).toContain('修订授权（追加新版，旧版留痕）');
    expect(src).toContain('退回重做（授权不变）');
    expect(src).toContain('绕过准入、不产生新授权、不改 stage');
  });

  it('文案全从服务端视图取：页面没有 stage/kind → 中文的第二套字典', () => {
    const src = page();
    expect(src).toContain('view.stageLabel');
    expect(src).toContain('attention.kindLabel');
    expect(src).toContain('detail.mandate.intensityLabel');
    expect(src).toContain('report.kindLabel');
    // stage 键只允许映射到颜色（CSS 变量/rgba），不允许映射到中文文案
    expect(src).not.toMatch(/awaiting_(answer|review):\s*'(?![rv]a?)/);
  });

  it('错误经 errorTextOf；页面不绕过 api 层直接 invoke，也不引用通道常量', () => {
    const src = page();
    expect(src).toContain("import { errorTextOf } from '../../../shared/error-messages'");
    expect(src).toContain('errorTextOf(err,');
    expect(src).not.toContain('IPC_CHANNELS');
    expect(src).not.toContain('window.tongtianAPI');
    expect(src).not.toMatch(/\binvoke\(/);
    // 读不到通道时如实显示原因（浏览器/Mock 的 COLLAB_UNSUPPORTED 由 collab-local 给出）
    expect(src).toContain('setUnavailable(errorTextOf(err,');
  });
});
