/**
 * 任务 CLI 契约测试（#78 服务化/CLI）
 *
 * 用真实 HTTP 服务验证：health/doctor/chat 的可消费性、SSE 渲染、
 * 令牌只从环境变量读取、401 中文提示，以及与引擎实际请求体一致。
 */

import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { createServer, type IncomingMessage, type Server, type ServerResponse } from 'node:http';
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import type { AddressInfo } from 'node:net';

const CLI = fileURLToPath(new URL('../../../scripts/tt-cli.mjs', import.meta.url));
const TOKEN = 'cli-test-token-9f3a';

interface Captured {
  auth: string;
  body: string;
  url: string;
}

const captured: Captured[] = [];
let server: Server;
let baseUrl = '';

function json(res: ServerResponse, status: number, body: unknown) {
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
}

function handler(req: IncomingMessage, res: ServerResponse) {
  let body = '';
  req.on('data', (c) => {
    body += c;
  });
  req.on('end', () => {
    captured.push({ auth: String(req.headers.authorization || ''), body, url: String(req.url) });
    if (req.url === '/health') {
      json(res, 200, {
        status: 'ok',
        version: 'test-1',
        engine: 'agentscope',
        api_version: '1',
        env_api_key: false,
      });
      return;
    }
    if (req.url === '/api/chat' && req.method === 'POST') {
      if (req.headers.authorization !== `Bearer ${TOKEN}`) {
        json(res, 401, { ok: false, code: 'ENGINE_UNAUTHORIZED', message: '需要引擎令牌' });
        return;
      }
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write('event: status_change\ndata: {"status":"executing"}\n\n');
      res.write(
        'event: deliver\ndata: {"final_text":"报告已生成","artifacts":["C:/out/report.md"]}\n\n',
      );
      res.end('event: done\ndata: {}\n\n');
      return;
    }
    json(res, 404, { ok: false, code: 'NOT_FOUND', message: 'not found' });
  });
}

interface RunResult {
  status: number | null;
  stdout: string;
  stderr: string;
}

/** 异步子进程：服务端就在本进程里，spawnSync 会堵住事件循环导致请求超时 */
function run(args: string[], env: Record<string, string> = {}): Promise<RunResult> {
  return new Promise((resolve) => {
    execFile(
      process.execPath,
      [CLI, ...args],
      {
        encoding: 'utf8',
        timeout: 20000,
        env: {
          ...process.env,
          TT_ENGINE_URL: '',
          TT_ENGINE_TOKEN: '',
          ...env,
        },
      },
      (err, stdout, stderr) => {
        const code = err && typeof err.code === 'number' ? err.code : err ? null : 0;
        resolve({ status: code, stdout: String(stdout ?? ''), stderr: String(stderr ?? '') });
      },
    );
  });
}

beforeAll(async () => {
  server = createServer(handler);
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  baseUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

afterAll(async () => {
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

describe('tt-cli 参数与凭据边界', () => {
  it('--help 列出 health/chat/doctor', async () => {
    const r = await run(['--help']);
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('health');
    expect(r.stdout).toContain('chat');
    expect(r.stdout).toContain('doctor');
  });

  it('拒绝 --token，要求走环境变量', async () => {
    const r = await run(['health', '--token', 'secret']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('凭据不要进命令行');
    expect(r.stderr).not.toContain('secret');
  });

  it('未知命令退出码 2', async () => {
    const r = await run(['nope']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('未知命令');
  });
});

describe('tt-cli 真实 HTTP 行为', () => {
  it('doctor 探测健康并说明服务化边界，不打印令牌', async () => {
    captured.length = 0;
    const r = await run(['doctor'], { TT_ENGINE_URL: baseUrl, TT_ENGINE_TOKEN: TOKEN });
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('[health] OK');
    expect(r.stdout).toContain('不显示');
    expect(r.stdout).toContain('桌面专属能力');
    expect(r.stdout).not.toContain(TOKEN);
    expect(captured[0]?.auth).toBe(`Bearer ${TOKEN}`);
  });

  it('chat 流式渲染 deliver，并携带目标与工作目录', async () => {
    captured.length = 0;
    const r = await run(['chat', '生成一份周报', '--work-dir', 'C:/work/weekly'], {
      TT_ENGINE_URL: baseUrl,
      TT_ENGINE_TOKEN: TOKEN,
    });
    expect(r.status).toBe(0);
    expect(r.stdout).toContain('[status] executing');
    expect(r.stdout).toContain('报告已生成');
    expect(r.stdout).toContain('C:/out/report.md');
    expect(r.stdout).not.toContain(TOKEN);
    const chat = captured.find((c) => c.url === '/api/chat');
    expect(chat?.auth).toBe(`Bearer ${TOKEN}`);
    const payload = JSON.parse(chat?.body ?? '{}');
    expect(payload.message).toBe('生成一份周报');
    expect(payload.workDir).toBe('C:/work/weekly');
    expect(payload.stage).toBeUndefined();
  });

  it('401 时给出中文提示并退出 1', async () => {
    const r = await run(['chat', '你好'], { TT_ENGINE_URL: baseUrl, TT_ENGINE_TOKEN: 'wrong-token' });
    expect(r.status).toBe(1);
    expect(r.stderr).toContain('ENGINE_UNAUTHORIZED');
    expect(r.stderr).toContain('TT_ENGINE_TOKEN');
    expect(r.stderr).not.toContain('wrong-token');
  });

  it('URL 内嵌凭据被拒', async () => {
    const r = await run(['health', '--url', 'http://user:pass@127.0.0.1:1']);
    expect(r.status).toBe(2);
    expect(r.stderr).toContain('凭据不要写进 URL');
    expect(r.stderr).not.toContain('pass');
  });
});
