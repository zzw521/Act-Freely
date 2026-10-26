#!/usr/bin/env node
/**
 * 通天办公 · 任务 CLI（无第三方依赖，Node >= 18）
 *
 * 直接调用本地/服务化引擎：
 * - health  查看引擎版本与存活
 * - chat    提交任务并流式打印 SSE 事件（plan/step/deliver/done/error）
 * - doctor  自检地址、令牌来源与服务化边界
 *
 * 安全约束：
 * - 令牌只能来自环境变量 TT_ENGINE_TOKEN，不接受命令行参数，避免进入 shell 历史与进程列表；
 * - 只支持 http/https，URL 中不得内嵌用户名/密码；
 * - 非回环地址且未设置令牌时，doctor 明确警告，但仍会如实探测（不伪造成功）。
 *
 * 服务化模式下引擎的桌面专属能力不可用：终端 AI+、电脑操控、浏览器 AI+、远程桌面、
 * 应用内 Hook/审计视图都依赖 Tauri 桌面壳；CLI 不提供替代实现。
 */

const DEFAULT_URL = process.env.TT_ENGINE_URL || 'http://127.0.0.1:18000';
const TOKEN = (process.env.TT_ENGINE_TOKEN || '').trim();

function usage() {
  console.log(`通天办公任务 CLI

用法:
  node scripts/tt-cli.mjs health [--url http://127.0.0.1:18000] [--json]
  node scripts/tt-cli.mjs chat "任务描述" [--work-dir <dir>] [--task-id <id>] [--plan] [--json]
  node scripts/tt-cli.mjs doctor [--url ...]

环境变量:
  TT_ENGINE_URL     引擎地址（默认 ${DEFAULT_URL}）
  TT_ENGINE_TOKEN   服务化令牌；只从环境变量读取，不接受 --token
`);
}

function fail(message, code = 2) {
  console.error(message);
  process.exit(code);
}

function parseArgs(argv) {
  const opts = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--help' || arg === '-h') opts.help = true;
    else if (arg === '--json') opts.json = true;
    else if (arg === '--plan') opts.plan = true;
    else if (arg === '--url') opts.url = argv[++i];
    else if (arg === '--work-dir') opts.workDir = argv[++i];
    else if (arg === '--task-id') opts.taskId = argv[++i];
    else if (arg === '--token' || arg.startsWith('--token=')) {
      fail('凭据不要进命令行：请设置环境变量 TT_ENGINE_TOKEN（不会进入 shell 历史与进程列表）');
    } else if (arg.startsWith('--')) {
      fail(`未知参数 ${arg}（--help 查看用法）`);
    } else {
      opts._.push(arg);
    }
  }
  return opts;
}

function resolveBase(opts) {
  const raw = String(opts.url || DEFAULT_URL).trim();
  let url;
  try {
    url = new URL(raw);
  } catch {
    fail(`引擎地址不合法：${raw}`);
  }
  if (!['http:', 'https:'].includes(url.protocol)) {
    fail(`只支持 http/https 引擎地址，收到：${url.protocol}`);
  }
  if (url.username || url.password) {
    fail('凭据不要写进 URL：请设置环境变量 TT_ENGINE_TOKEN');
  }
  url.pathname = url.pathname.replace(/\/+$/, '');
  url.search = '';
  url.hash = '';
  return url;
}

function headers(extra = {}) {
  const out = { ...extra };
  if (TOKEN) out.authorization = `Bearer ${TOKEN}`;
  return out;
}

function isLoopback(url) {
  return ['127.0.0.1', 'localhost', '::1', '[::1]'].includes(url.hostname);
}

async function readJson(res) {
  const text = await res.text();
  try {
    return JSON.parse(text);
  } catch {
    return { raw: text };
  }
}

function apiError(res, body) {
  const code = body?.code ? `[${body.code}] ` : '';
  const message = body?.message || body?.raw || `HTTP ${res.status}`;
  if (res.status === 401) {
    return `${code}${message}\n提示：服务化模式请设置 TT_ENGINE_TOKEN；桌面模式请确认引擎由应用启动。`;
  }
  return `${code}${message}`;
}

async function fetchEngine(url, path, init = {}) {
  const target = `${url.origin}${url.pathname.replace(/\/+$/, '')}${path}`;
  try {
    return await fetch(target, {
      ...init,
      headers: headers(init.headers || {}),
    });
  } catch (err) {
    fail(
      `无法连接引擎 ${url.href}：${err instanceof Error ? err.message : String(err)}\n` +
        '请先启动桌面应用或 `python engine/server.py`，或用 --url / TT_ENGINE_URL 指定地址。',
      1,
    );
  }
}

async function cmdHealth(opts) {
  const url = resolveBase(opts);
  const res = await fetchEngine(url, '/health');
  const body = await readJson(res);
  if (!res.ok) fail(apiError(res, body), 1);
  if (opts.json) {
    console.log(JSON.stringify(body));
    return 0;
  }
  console.log(`engine: ${body.status} v${body.version} (${body.engine}, api ${body.api_version})`);
  console.log(`address: ${url.href}`);
  console.log(`env_api_key: ${body.env_api_key ? 'yes' : 'no'}`);
  return 0;
}

function printEvent(event, data, opts) {
  if (opts.json) {
    console.log(JSON.stringify({ event, data }));
    return;
  }
  const text = (value) => String(value ?? '').trim();
  switch (event) {
    case 'status_change':
      if (data.status) console.log(`[status] ${data.status}`);
      break;
    case 'plan': {
      console.log('[plan]');
      const steps = Array.isArray(data.steps) ? data.steps : [];
      steps.forEach((step, i) => {
        console.log(`  ${i + 1}. ${text(step.description || step.title || step.step_id || step.id)}`);
      });
      break;
    }
    case 'step_start':
      console.log(`[step] ${text(data.description || data.step_id)}`);
      break;
    case 'step_done': {
      const out = text(data.output);
      if (out) console.log(`[done] ${out}`);
      break;
    }
    case 'step_progress':
      if (data.message) console.log(`[progress] ${text(data.message)}`);
      break;
    case 'deliver': {
      const finalText = text(data.final_text);
      if (finalText) console.log(`\n${finalText}\n`);
      const artifacts = Array.isArray(data.artifacts) ? data.artifacts : [];
      if (artifacts.length) {
        console.log('[artifacts]');
        artifacts.forEach((p) => console.log(`  ${p}`));
      }
      break;
    }
    case 'usage':
      if (data.usage) {
        const { promptTokens, completionTokens, model } = data.usage;
        console.log(
          `[usage] ${model || 'model'} prompt=${promptTokens ?? '?'} completion=${completionTokens ?? '?'}`,
        );
      }
      break;
    case 'error':
      console.error(`[error] ${data.code ? `[${data.code}] ` : ''}${text(data.message)}`);
      break;
    case 'checkpoint':
      if (data.resumable) console.log('[checkpoint] 可从检查点继续');
      break;
    case 'done':
      console.log('[done] 任务结束');
      break;
    default:
      if (data.message) console.log(`[${event}] ${text(data.message)}`);
      break;
  }
}

async function cmdChat(opts) {
  const goal = opts._.slice(1).join(' ').trim();
  if (!goal) fail('用法: tt-cli chat "任务描述"（--help 查看参数）', 2);
  const url = resolveBase(opts);
  const payload = {
    message: goal,
    task_id: opts.taskId || `cli-${Date.now()}`,
  };
  if (opts.workDir) payload.workDir = opts.workDir;
  if (opts.plan) payload.stage = 'plan';

  const res = await fetchEngine(url, '/api/chat', {
    method: 'POST',
    headers: { 'content-type': 'application/json', accept: 'text/event-stream' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    fail(apiError(res, await readJson(res)), 1);
  }
  if (!res.body) fail('引擎返回了非流式响应，无法读取事件', 1);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let hadError = false;
  let finished = false;

  const handleBlock = (block) => {
    if (!block.trim() || block.trimStart().startsWith(':')) return false;
    let event = 'message';
    const dataLines = [];
    for (const line of block.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim();
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return false;
    let data = {};
    try {
      data = JSON.parse(dataLines.join('\n'));
    } catch {
      data = { message: dataLines.join('\n') };
    }
    printEvent(event, data, opts);
    if (event === 'error') hadError = true;
    return event === 'done';
  };

  while (!finished) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx = buffer.indexOf('\n\n');
    while (idx !== -1) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      if (handleBlock(block)) {
        finished = true;
        break;
      }
      idx = buffer.indexOf('\n\n');
    }
  }
  if (!finished && buffer.trim()) handleBlock(buffer);
  if (!finished) {
    await reader.cancel().catch(() => undefined);
    console.error('[warn] 事件流在 done 之前结束（可能是网络中断）；任务是否仍在执行需查看引擎状态');
    hadError = true;
  }
  return hadError ? 1 : 0;
}

async function cmdDoctor(opts) {
  const url = resolveBase(opts);
  const loopback = isLoopback(url);
  console.log(`[config] url=${url.href}`);
  console.log(`[config] token=${TOKEN ? '已从 TT_ENGINE_TOKEN 读取（不显示）' : '未设置'}`);
  console.log(`[config] bind=${loopback ? '回环地址（本机）' : '非回环地址（服务化/内网）'}`);
  if (!loopback && !TOKEN) {
    console.log('[warn] 非回环地址未设置 TT_ENGINE_TOKEN：引擎会拒绝启动或请求将被 401 拒绝');
  }
  if (!loopback && url.protocol !== 'https:') {
    console.log('[warn] 非回环地址使用 http：请置于可信反向代理/隔离网络，不要直接暴露公网');
  }

  const res = await fetchEngine(url, '/health');
  const body = await readJson(res);
  if (!res.ok) {
    console.log(`[health] FAIL ${apiError(res, body)}`);
    return 1;
  }
  console.log(`[health] OK v${body.version} (${body.engine})`);
  console.log('[cli] 可用命令：health / chat / doctor');
  console.log(
    '[boundary] 服务化实例不提供桌面专属能力：终端 AI+、电脑操控、浏览器 AI+、远程桌面、应用内 Hook/审计视图',
  );
  return 0;
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help || opts._.length === 0) {
    usage();
    return opts.help ? 0 : 2;
  }
  const command = opts._[0];
  if (command === 'health') return cmdHealth(opts);
  if (command === 'chat') return cmdChat(opts);
  if (command === 'doctor') return cmdDoctor(opts);
  fail(`未知命令 ${command}（--help 查看用法）`, 2);
}

main()
  .then((code) => {
    process.exitCode = code ?? 0;
  })
  .catch((err) => {
    fail(err instanceof Error ? err.message : String(err), 1);
  });
