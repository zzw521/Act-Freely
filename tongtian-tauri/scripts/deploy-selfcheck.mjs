#!/usr/bin/env node
/**
 * 通天办公 · 服务化部署自检（无第三方依赖，Node >= 18）
 *
 * 给部署人员在目标主机上跑一遍，回答一个问题：这套「反向代理 + 引擎」是否真的可用。
 * 每一项失败都输出可定位的中文原因和修法，不会把半截结果当成成功。
 *
 * 检查项：
 *   1. 地址与令牌来源（令牌只从环境变量读，拒绝 --token）
 *   2. GET  /health 免令牌可达
 *   3. 静态首页由引擎同端口托管（--web-dir 生效、没被反代吃掉）
 *   4. 令牌门槛：无令牌访问 /api/chat 必须 401 ENGINE_UNAUTHORIZED
 *   5. 带令牌的 plan 事件流：首包 ≤2s、事件齐全、seq 单调
 *   6. 攒包判定：用执行流按到达节奏判断（plan 流一次成型，判不出缓冲）
 *   7. 断线重连：GET /api/events/replay 能按 from_seq 补齐
 *   8. 可选（--run-task）：真实完成一次对话，直到 done
 *
 * 用法：
 *   TT_ENGINE_TOKEN='<部署方令牌>' node scripts/deploy-selfcheck.mjs --url https://内网地址
 *
 * 退出码：0 全部通过；1 有检查失败；2 用法或参数错误。
 */

const DEFAULT_FIRST_BYTE_MS = 2000;

function usage() {
  console.log(`通天办公服务化部署自检

用法:
  node scripts/deploy-selfcheck.mjs [--url https://host[:port]] [--json]
                                    [--work-dir <引擎内工作目录>]
                                    [--run-task]            # 额外真实完成一次对话（会消耗模型额度）
                                    [--task-seconds <n>]    # 上一次的等待上限，默认 240
                                    [--allow-http]          # 明确接受 http（仅回环/可信内网）

环境变量:
  TT_ENGINE_URL    引擎地址（默认 http://127.0.0.1:18000）
  TT_ENGINE_TOKEN  引擎凭据；可以是静态令牌，也可以是账号服务签发的 access token
                   只从环境变量读取，不接受 --token（避免进入 shell 历史与进程列表）
`);
}

function parseArgs(argv) {
  const opts = { runTask: false, taskSeconds: 240 };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a === '--help' || a === '-h') opts.help = true;
    else if (a === '--json') opts.json = true;
    else if (a === '--allow-http') opts.allowHttp = true;
    else if (a === '--run-task') opts.runTask = true;
    else if (a === '--url') opts.url = argv[++i];
    else if (a === '--work-dir') opts.workDir = argv[++i];
    else if (a === '--task-seconds') opts.taskSeconds = Number(argv[++i]);
    else if (a === '--token' || a.startsWith('--token=')) {
      die('凭据不要进命令行：请设置环境变量 TT_ENGINE_TOKEN', 2);
    } else if (a.startsWith('--')) {
      die(`未知参数 ${a}（--help 查看用法）`, 2);
    } else {
      die(`多余参数 ${a}（--help 查看用法）`, 2);
    }
  }
  return opts;
}

function die(message, code = 1) {
  console.error(message);
  process.exit(code);
}

const RESULTS = [];

function check(name, ok, detail, hint) {
  RESULTS.push({ name, ok: Boolean(ok), detail: detail || '', hint: ok ? '' : hint || '' });
  const mark = ok ? 'PASS' : 'FAIL';
  console.log(`[${mark}] ${name}${detail ? ` — ${detail}` : ''}`);
  if (!ok && hint) console.log(`       修法：${hint}`);
}

function resolveBase(raw) {
  let url;
  try {
    url = new URL(String(raw).trim());
  } catch {
    die(`引擎地址不合法：${raw}`, 2);
  }
  if (!['http:', 'https:'].includes(url.protocol)) die(`只支持 http/https，收到：${url.protocol}`, 2);
  if (url.username || url.password) die('凭据不要写进 URL：请设置环境变量 TT_ENGINE_TOKEN', 2);
  url.pathname = url.pathname.replace(/\/+$/, '');
  url.search = '';
  url.hash = '';
  return url;
}

const isLoopback = (url) => ['127.0.0.1', 'localhost', '::1', '[::1]'].includes(url.hostname);

function api(pathname) {
  const base = process.env.__TT_BASE;
  return `${base}${pathname}`;
}

function authHeaders(extra = {}, token = process.env.TT_ENGINE_TOKEN) {
  const out = { ...extra };
  if (token) out['x-tt-engine-token'] = token;
  return out;
}

async function readJsonSafe(res) {
  const text = await res.text().catch(() => '');
  try {
    return JSON.parse(text);
  } catch {
    return text ? { raw: text.slice(0, 200) } : null;
  }
}

/** 读一条 SSE：返回状态、响应头、事件列表、分块到达时刻与异常说明 */
async function readSse(url, init, { deadlineMs }) {
  const started = Date.now();
  const arrivals = [];
  const events = [];
  const keepalives = { count: 0 };
  let closedByError = null;
  let truncated = false;

  let res;
  try {
    res = await fetch(url, init);
  } catch (err) {
    return { fetchError: err instanceof Error ? err.message : String(err) };
  }
  const headers = {};
  res.headers.forEach((v, k) => {
    headers[k.toLowerCase()] = v;
  });
  if (!res.body) {
    const body = await res.text().catch(() => '');
    let json = null;
    try {
      json = JSON.parse(body);
    } catch {
      /* 非 JSON 响应保留原文 */
    }
    return { status: res.status, headers, body: json || body, noBody: true };
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const timer = setTimeout(() => {
    reader.cancel().catch(() => undefined);
  }, deadlineMs);

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      if (!value || !value.length) continue;
      arrivals.push(Date.now() - started);
      buffer += decoder.decode(value, { stream: true });
      let idx = buffer.indexOf('\n\n');
      while (idx !== -1) {
        const block = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        parseBlock(block, events, keepalives);
        idx = buffer.indexOf('\n\n');
      }
      if (events.some((e) => e.event === 'done' || e.event === 'error')) break;
    }
  } catch (err) {
    closedByError = err instanceof Error ? err.message : String(err);
  } finally {
    clearTimeout(timer);
  }

  if (buffer.trim()) parseBlock(buffer, events, keepalives);
  if (!events.some((e) => ['done', 'error'].includes(e.event))) truncated = true;

  let json = null;
  if (res.status >= 400) {
    // 错误响应可能是普通 JSON（例如 401），SSE 解析拿不到就用原文再试一次
    json = safeJson(events.length ? null : buffer);
  }
  return {
    status: res.status,
    headers,
    events,
    keepalives: keepalives.count,
    arrivals,
    firstByteMs: arrivals.length ? arrivals[0] : null,
    totalMs: Date.now() - started,
    closedByError,
    truncated,
    json,
  };
}

function parseBlock(block, events, keepalives) {
  const text = block.trim();
  if (!text) return;
  if (text.startsWith(':')) {
    keepalives.count += 1;
    return;
  }
  let event = 'message';
  let id = null;
  const dataLines = [];
  for (const line of block.split('\n')) {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('id:')) id = line.slice(3).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
  }
  if (!dataLines.length && event === 'message') return;
  events.push({ event, id, data: safeJson(dataLines.join('\n')) || {} });
}

function safeJson(text) {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

async function main(opts, url) {
  const token = (process.env.TT_ENGINE_TOKEN || '').trim();
  const taskId = `selfcheck-${Date.now()}`;

  // 1. 来源与边界
  check(
    '令牌来源',
    true,
    token ? '已从 TT_ENGINE_TOKEN 读取（不回显）' : '未设置（回环直连场景可以接受；服务化必须设置）',
  );
  if (!isLoopback(url) && url.protocol !== 'https:' && !opts.allowHttp) {
    check(
      '传输安全',
      false,
      `非回环地址走了明文 http：${url.href}`,
      '请通过 https 访问（反向代理终止 TLS），或确认在隔离内网后用 --allow-http 明确接受',
    );
  } else {
    check('传输安全', true, `${url.protocol}//${url.host}`);
  }

  // 2. /health 免令牌
  let health = null;
  try {
    const res = await fetch(api('/health'));
    health = await readJsonSafe(res);
    check(
      '健康检查 /health',
      res.ok && health && health.status,
      res.ok ? `v${health.version || '?'} engine=${health.engine || '?'}（免令牌）` : `HTTP ${res.status}`,
      '确认引擎进程存活、--web-dir 与端口映射正确，反代不要把 /health 指到别处',
    );
  } catch (err) {
    check('健康检查 /health', false, `无法连接：${err.message}`, '检查地址、端口与防火墙；容器场景先看 `docker logs`');
    return;
  }

  // 3. 静态首页
  try {
    const res = await fetch(api('/'));
    const html = await res.text();
    const isHtml = html.trimStart().toLowerCase().startsWith('<!doctype html');
    check(
      '静态前端托管',
      res.ok && isHtml,
      `HTTP ${res.status}，${isHtml ? 'HTML 首页可达' : '返回内容不是 HTML'}`,
      '引擎需要 --web-dir <dist> 启动；反代的 location / 必须转发到引擎而不是本地 root',
    );
  } catch (err) {
    check('静态前端托管', false, `请求失败：${err.message}`, '同上，先确认 /health 通了再看首页');
  }

  // 4. 令牌门槛
  if (token) {
    try {
      const res = await fetch(api('/api/chat'), {
        method: 'POST',
        headers: { 'content-type': 'application/json', accept: 'text/event-stream' },
        body: JSON.stringify({ message: '自检', task_id: `${taskId}-notoken` }),
      });
      const body = await readJsonSafe(res);
      check(
        '无令牌访问被拒',
        res.status === 401 && body && body.code === 'ENGINE_UNAUTHORIZED',
        `HTTP ${res.status} code=${body && body.code}`,
        '缺失：/api/* 未受令牌保护，服务化不可上线；检查 TT_ENGINE_TOKEN 或 TT_ENGINE_JWT_SECRET 是否注入容器',
      );
    } catch (err) {
      check('无令牌访问被拒', false, `请求失败：${err.message}`, '反代可能吞掉了未授权请求，先看 /health 是否可达');
    }
  } else {
    console.log('[SKIP] 无令牌访问被拒 —— 未设置 TT_ENGINE_TOKEN，无法区分「放开」与「拒绝」');
  }

  if (!token) {
    check('带令牌事件流', false, '未设置 TT_ENGINE_TOKEN', '服务化部署请注入部署方令牌后再跑自检（回环本机除外）');
    return;
  }

  // 5. 带令牌的 plan 事件流
  const payload = { message: '自检：列出工作目录并给出结论', task_id: taskId, stage: 'plan' };
  if (opts.workDir) payload.workDir = opts.workDir;
  const stream = await readSse(
    api('/api/chat'),
    {
      method: 'POST',
      headers: authHeaders({ 'content-type': 'application/json', accept: 'text/event-stream' }),
      body: JSON.stringify(payload),
    },
    { deadlineMs: 60_000 },
  );

  if (stream.fetchError) {
    check('带令牌事件流', false, `无法连接：${stream.fetchError}`, '确认反代上游地址与引擎端口一致');
    return;
  }
  const types = stream.events.map((e) => e.event);
  const seqs = stream.events.map((e) => e.data && e.data.seq).filter((s) => typeof s === 'number');
  const firstOk = typeof stream.firstByteMs === 'number' && stream.firstByteMs <= DEFAULT_FIRST_BYTE_MS;
  check(
    '带令牌事件流（plan）',
    stream.status === 200 && types.includes('plan'),
    `HTTP ${stream.status} 首包 ${stream.firstByteMs}ms 事件=${types.join(',')}`,
    stream.status !== 200
      ? `按状态码定位：401=令牌不符，403=跨用户访问，502/504=反代到不了引擎`
      : stream.truncated || stream.closedByError
        ? '流被中途掐断（没等到 done）：反代空闲超时短于模型静默时长。Nginx 给 /api/chat 置 proxy_read_timeout 3600s，Caddy 置 stream_timeout 0；网关/LB 的空闲超时同样要放开'
        : '没有 plan 事件：看引擎日志里模型配置（models.json 或 TT_PROVIDER/TT_MODEL）是否可用',
  );
  check(
    `SSE 首包 ≤${DEFAULT_FIRST_BYTE_MS}ms`,
    firstOk,
    `${stream.firstByteMs}ms`,
    '先分清是谁慢：直连引擎对比同一任务的到达节奏；反代侧对 /api/chat 关闭 proxy_buffering，并把空闲超时放大到任务级时长',
  );
  check(
    '事件序号单调',
    seqs.length > 0 && seqs.every((s, i) => s === seqs[0] + i),
    `seq=${seqs.join(',')}`,
    '乱序或缺号说明反代改了流（例如重复转发或做了响应缓存），需要直连对比',
  );

  // 6. 攒包判定：plan 事件是模型答完后一次成型的，单看它判不出代理有没有攒包；
  //    只有整条流正常跑完却只有一次到达，才能断定被攒到最后。中途断流另有判据（见上一项）。
  //    真正的按节奏判定放在执行流里（--run-task）。
  const spread = stream.arrivals.length > 1 ? stream.arrivals.at(-1) - stream.arrivals[0] : 0;
  const bursty = stream.arrivals.length <= 1 && spread === 0 && stream.totalMs > 3000;
  if (bursty && !stream.truncated && !stream.closedByError) {
    check(
      'plan 流未被攒到最后下发',
      false,
      `整条流持续 ${stream.totalMs}ms，跑完了却只在 ${stream.firstByteMs}ms 到了一次`,
      '代理把响应攒满了才发：Nginx 对 /api/chat 置 proxy_buffering off，Caddy 置 flush_interval -1（引擎已发 X-Accel-Buffering: no，nginx 会据此自行关闭缓冲）',
    );
  } else {
    console.log('[SKIP] 攒包判定 —— plan 流一次成型不足以判断，下面用 --run-task 的执行流按到达节奏判定');
  }

  // 7. 断线重连
  try {
    const res = await fetch(api(`/api/events/replay?task_id=${encodeURIComponent(taskId)}&from_seq=1`), {
      headers: authHeaders(),
    });
    const body = await readJsonSafe(res);
    const got = Array.isArray(body && body.events) ? body.events.length : 0;
    check(
      '断线重连（事件重放）',
      res.ok && got > 0 && body.windowExpired !== true,
      `HTTP ${res.status} 重放事件 ${got} 条 windowExpired=${body && body.windowExpired}`,
      '重放为空说明任务事件窗口或 task_id 不符；确认反代没有改写 /api/events/ 路径',
    );
  } catch (err) {
    check('断线重连（事件重放）', false, `请求失败：${err.message}`, '同上');
  }

  // 8. 可选：真实完成一次对话（会消耗模型额度，默认不跑）
  if (opts.runTask) {
    const task = `${taskId}-run`;
    const body = { message: '自检：完成一次最短对话并回复“自检完成”', task_id: task };
    if (opts.workDir) body.workDir = opts.workDir;
    const run = await readSse(
      api('/api/chat'),
      {
        method: 'POST',
        headers: authHeaders({ 'content-type': 'application/json', accept: 'text/event-stream' }),
        body: JSON.stringify(body),
      },
      { deadlineMs: opts.taskSeconds * 1000 },
    );
    const runTypes = run.events ? run.events.map((e) => e.event) : [];
    const errored = runTypes.includes('error');
    check(
      `完成一次真实对话（--run-task，上限 ${opts.taskSeconds}s）`,
      run.status === 200 && runTypes.includes('done') && !errored && !run.closedByError,
      `HTTP ${run.status} 事件=${runTypes.join(',') || '无'} keepalive=${run.keepalives ?? 0} 时长=${run.totalMs}ms`
        + (errored ? ` 错误=${JSON.stringify((run.events.find((e) => e.event === 'error') || {}).data)}` : ''),
      errored
        ? '看事件里的错误码：模型配置缺失需要在页面「模型设置」或引擎环境变量里补齐；超时/断流要检查反代空闲超时'
        : '未收到 done：可能是模型超时或反代掐断连接，先看引擎日志再对比直连引擎的结果',
    );

    // 执行流才是攒包判据：引擎一上来就发 status_change，后续事件按模型节奏分批到达；
    // 若一条持续数秒的流只在最后到达一次，就是代理把响应攒满了
    const runSpread = run.arrivals && run.arrivals.length > 1 ? run.arrivals.at(-1) - run.arrivals[0] : 0;
    const progressive = (run.arrivals?.length || 0) >= 2 && runSpread > 1000;
    check(
      '执行流按到达节奏下发（反代未攒包）',
      progressive || (run.totalMs || 0) <= 2000,
      `分块到达 ${run.arrivals?.length ?? 0} 次、跨度 ${runSpread}ms、首包 ${run.firstByteMs}ms、总时长 ${run.totalMs}ms`,
      '一条超过 2 秒的执行流只到达一次说明被攒到最后：Nginx 对 /api/chat 置 proxy_buffering off（引擎已发 X-Accel-Buffering: no，nginx 会据此关闭），Caddy 置 flush_interval -1',
    );
  } else {
    console.log('[SKIP] 完成一次真实对话 —— 需要 --run-task 显式开启（会真实调用所选模型）');
  }
}

const opts = parseArgs(process.argv.slice(2));
if (opts.help) {
  usage();
  process.exit(0);
}
const url = resolveBase(opts.url || process.env.TT_ENGINE_URL || 'http://127.0.0.1:18000');
const base = `${url.origin}${url.pathname}`.replace(/\/+$/, '');
process.env.__TT_BASE = base;

main(opts, url)
  .then(() => {
    const failed = RESULTS.filter((r) => !r.ok);
    const summary = { total: RESULTS.length, passed: RESULTS.length - failed.length, failed: failed.length };
    if (opts.json) {
      console.log(JSON.stringify({ url: url.href, summary, results: RESULTS }, null, 2));
    } else {
      console.log(
        `\n部署自检：${summary.passed}/${summary.total} 项通过${failed.length ? `，${failed.length} 项失败` : ''}`,
      );
      if (failed.length) {
        console.log('失败项：');
        failed.forEach((f) => console.log(`  · ${f.name} — ${f.detail || '无细节'}`));
      } else {
        console.log('结论：健康检查、静态托管、令牌门槛、SSE 首包与重连均按预期工作。');
      }
    }
    process.exitCode = failed.length ? 1 : 0;
  })
  .catch((err) => die(err instanceof Error ? err.message : String(err), 1));
