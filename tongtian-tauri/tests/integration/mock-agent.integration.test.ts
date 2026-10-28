// tests/integration/mock-agent.integration.test.ts — Mock Agent SSE 集成测试（流 H2/H3）
//
// 端到端验证三层架构演进 P0 的 SSE 契约（黄金 fixture 同源）：
// - 单调 seq + SSE id 行（`id: {task_id}:{seq}`）
// - 重放端点（/api/events/replay，seq > from_seq）
// - 取消端点（/api/chat/cancel）
// - 突发 500 事件吞吐与 seq 无丢失（[test:burst]）
//
// 依赖本机 python；不可用时自动跳过（describe.skipIf），不阻塞 CI。

import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { spawn, execFileSync, type ChildProcess } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const PROJECT_ROOT = path.resolve(__dirname, '../..');
const MOCK_AGENT = path.join(PROJECT_ROOT, 'scripts/mock-agent.py');
const PORT = 18910; // 避开 18000-19000 运行时端口段
const BASE = `http://127.0.0.1:${PORT}`;

// mock agent 会在 --work-dir 下落盘 sessions/。早期直接传 PROJECT_ROOT，每跑一次测试就往源码树
// 里塞一条假会话（开发规范要求测试数据隔离），故改用临时目录，结束时清理。
const WORK_DIR = mkdtempSync(path.join(os.tmpdir(), 'tt-mock-agent-'));

function pythonAvailable(): boolean {
  try {
    execFileSync('python', ['--version'], { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}

const hasPython = pythonAvailable();

/** 解析 SSE 文本为事件列表（与 sse.rs parse_block 对齐：捕获 id/event/data） */
interface SseEvent {
  id?: string;
  event?: string;
  data?: Record<string, unknown>;
}

function parseSse(text: string): SseEvent[] {
  const events: SseEvent[] = [];
  for (const block of text.split('\n\n')) {
    if (!block.trim()) continue;
    const ev: SseEvent = {};
    for (const line of block.split('\n')) {
      if (line.startsWith('id:')) ev.id = line.slice(3).trim();
      else if (line.startsWith('event:')) ev.event = line.slice(6).trim();
      else if (line.startsWith('data:')) {
        try {
          ev.data = JSON.parse(line.slice(5).trim());
        } catch {
          ev.data = { raw: line.slice(5).trim() };
        }
      }
    }
    if (ev.event || ev.data) events.push(ev);
  }
  return events;
}

async function waitForHealth(timeoutMs = 8000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const resp = await fetch(`${BASE}/health`);
      if (resp.ok) return true;
    } catch {
      /* 未就绪 */
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

describe.skipIf(!hasPython)('Mock Agent SSE 集成（黄金 fixture）', () => {
  let proc: ChildProcess;

  beforeAll(async () => {
    proc = spawn('python', [MOCK_AGENT, '--port', String(PORT), '--work-dir', WORK_DIR], {
      stdio: 'ignore',
    });
    const ready = await waitForHealth();
    expect(ready).toBe(true);
  });

  afterAll(() => {
    proc?.kill();
    rmSync(WORK_DIR, { recursive: true, force: true });
  });

  it('中文目标不会因父进程代码页崩掉请求线程（脚本自带 UTF-8 stdout 兜底）', async () => {
    const src = readFileSync(MOCK_AGENT, 'utf8');
    expect(src).toContain('reconfigure(encoding="utf-8"');
    expect(src).toContain('_force_utf8_stdio()');
    // 未预期异常必须回 500，而不是让连接无声关闭（客户端只会看到 other side closed）
    expect(src).toContain('self._send_json(500,');
    // 真实回归：中文目标 + stage=plan 必须拿到完整计划流
    const resp = await fetch(`${BASE}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: '写一份周报，含本周三件事', task_id: `integ-cjk-${Date.now()}`, stage: 'plan' }),
    });
    expect(resp.status).toBe(200);
    expect(await resp.text()).toContain('event: plan');
  });

  it('突发 500 事件：seq 单调递增且含 id 行（吞吐与丢失=0）', async () => {
    const taskId = `integ-burst-${Date.now()}`;
    const resp = await fetch(`${BASE}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: '[test:burst]', task_id: taskId }),
    });
    expect(resp.status).toBe(200);
    const text = await resp.text();
    const events = parseSse(text);

    // 500 step_progress + 1 done
    expect(events.length).toBe(501);

    // seq 单调递增 1..501，无丢失、无重复
    const seqs = events.map((e) => (e.data?.seq as number) ?? -1);
    for (let i = 0; i < seqs.length; i++) {
      expect(seqs[i]).toBe(i + 1);
    }

    // 每个事件均携带 id 行（`{task_id}:{seq}`）
    for (const ev of events) {
      expect(ev.id).toMatch(new RegExp(`^${taskId}:\\d+$`));
    }

    // 末事件为 done
    expect(events[events.length - 1].event).toBe('done');
  });

  it('重放端点：seq > from_seq 过滤 + 窗口未过期', async () => {
    const taskId = `integ-replay-${Date.now()}`;
    // 先跑一个 burst 任务写入窗口
    await fetch(`${BASE}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: '[test:burst]', task_id: taskId }),
    }).then((r) => r.text());

    const resp = await fetch(`${BASE}/api/events/replay?task_id=${taskId}&from_seq=498`);
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as {
      taskId: string;
      windowExpired: boolean;
      events: Array<{ seq: number; type: string }>;
    };
    expect(body.windowExpired).toBe(false);
    // seq 499/500/501 三条
    expect(body.events.map((e) => e.seq)).toEqual([499, 500, 501]);
    expect(body.events[body.events.length - 1].type).toBe('done');
  });

  it('取消端点：未知任务返回 cancelled=false', async () => {
    const resp = await fetch(`${BASE}/api/chat/cancel`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: 'no-such-task' }),
    });
    expect(resp.status).toBe(200);
    const body = (await resp.json()) as { cancelled: boolean; reason?: string };
    expect(body.cancelled).toBe(false);
    expect(body.reason).toBe('TASK_NOT_FOUND');
  });

  it('取消端点：活跃任务可取消', async () => {
    const taskId = `integ-cancel-${Date.now()}`;
    // 先发起一个正常任务建立会话（同步等待结束）
    await fetch(`${BASE}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'hello', task_id: taskId }),
    }).then((r) => r.text());

    const resp = await fetch(`${BASE}/api/chat/cancel`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId }),
    });
    const body = (await resp.json()) as { cancelled: boolean };
    expect(body.cancelled).toBe(true);
  });

  // ── 两阶段协议（核心闭环补齐 流 5） ──

  it('两阶段：stage=plan 产出 plan→pending_review→done', async () => {
    const taskId = `integ-plan-${Date.now()}`;
    const resp = await fetch(`${BASE}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: '写一份周报', task_id: taskId, stage: 'plan' }),
    });
    expect(resp.status).toBe(200);
    const events = parseSse(await resp.text());
    const types = events.map((e) => e.event);
    expect(types).toContain('plan');
    expect(types).toContain('status_change');
    expect(events[events.length - 1].event).toBe('done');
    // 包含 pending_review 状态事件
    const statuses = events
      .filter((e) => e.event === 'status_change')
      .map((e) => e.data?.status);
    expect(statuses).toContain('pending_review');
  });

  it('两阶段：approve 后执行流 step_*→deliver→done', async () => {
    const taskId = `integ-approve-${Date.now()}`;
    await fetch(`${BASE}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: '执行任务', task_id: taskId, stage: 'plan' }),
    }).then((r) => r.text());

    const resp = await fetch(`${BASE}/api/chat/review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId, action: 'approve' }),
    });
    const events = parseSse(await resp.text());
    const types = events.map((e) => e.event);
    expect(types).toContain('step_start');
    expect(types).toContain('step_done');
    expect(types).toContain('deliver');
    expect(events[events.length - 1].event).toBe('done');
  });

  it('两阶段：reject 产出 done(REJECTED)', async () => {
    const taskId = `integ-reject-${Date.now()}`;
    await fetch(`${BASE}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: '任务', task_id: taskId, stage: 'plan' }),
    }).then((r) => r.text());

    const resp = await fetch(`${BASE}/api/chat/review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId, action: 'reject' }),
    });
    const events = parseSse(await resp.text());
    const sc = events.find((e) => e.event === 'status_change');
    expect(sc?.data?.statusReasonCode).toBe('REJECTED');
    expect(events[events.length - 1].event).toBe('done');
  });

  it('两阶段：modify 重新产出 plan 并应用 patch', async () => {
    const taskId = `integ-modify-${Date.now()}`;
    await fetch(`${BASE}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: '任务', task_id: taskId, stage: 'plan' }),
    }).then((r) => r.text());

    const resp = await fetch(`${BASE}/api/chat/review`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        task_id: taskId,
        action: 'modify',
        patch: [{ id: 'step-2', name: '[修改] 先收集素材' }],
      }),
    });
    const events = parseSse(await resp.text());
    const plan = events.find((e) => e.event === 'plan');
    const steps = (plan?.data?.steps as Array<{ description: string }>) ?? [];
    expect(steps.some((s) => s.description.includes('[修改]'))).toBe(true);
    const statuses = events
      .filter((e) => e.event === 'status_change')
      .map((e) => e.data?.status);
    expect(statuses).toContain('pending_review');
  });

  it('控制端点已移除：pause/rollback_to 不再提供（只有取消）', async () => {
    const taskId = `integ-ctrl-${Date.now()}`;
    await fetch(`${BASE}/api/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: '任务', task_id: taskId, stage: 'plan' }),
    }).then((r) => r.text());

    for (const action of ['pause', 'resume', 'rollback_to']) {
      const resp = await fetch(`${BASE}/api/chat/control`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: taskId, action, step_id: 'step-1' }),
      });
      expect(resp.status).toBe(404);
    }
  });
});
