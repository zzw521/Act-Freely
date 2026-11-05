// tests/unit/snapshot/snapshot-api.test.ts — #140 渲染层快照客户端的行为契约
//
// 用假 fetch 打真接线：URL/方法/请求体形状、失败回话怎么变成界面能说的话。
// 引擎侧的判定与闸由 tests/integration/engine_snapshot_routes_selftest.py 验，
// 这里不重跑那 90 项；**假 fetch 绿不等于真机可用**——桌面壳的截屏与快捷键仍未接线。

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

const {
  getSnapshotStatus,
  captureSnapshot,
  listPendingSnapshots,
  getSnapshotPreview,
  confirmSnapshot,
  cancelSnapshot,
  getSnapshotDecisions,
  getRelayStatus,
  grantRelay,
  decideRelay,
  registerRelayTabs,
  revokeRelay,
  snapshotUnverifiedViews,
  isSnapshotApiError,
  isDesktopOnlyRejection,
  SnapshotApiError,
} = await import('../../../src/renderer/lib/snapshot-api');

const { RELAY_DENIAL_LABELS, labelOf } = await import('../../../src/shared/snapshot-contract');

type Recorded = { url: string; method: string; body: Record<string, unknown> | null };

const calls: Recorded[] = [];
let reply: { status: number; body: unknown } = { status: 200, body: { ok: true } };

function body(): Record<string, unknown> {
  return { ok: true };
}

const fakeFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input);
  calls.push({
    url,
    method: (init?.method ?? 'GET').toUpperCase(),
    body: typeof init?.body === 'string' ? (JSON.parse(init.body) as Record<string, unknown>) : null,
  });
  const payload = reply.body ?? body();
  return {
    ok: reply.status >= 200 && reply.status < 300,
    status: reply.status,
    json: async () => payload,
  } as unknown as Response;
});

beforeEach(() => {
  calls.length = 0;
  reply = { status: 200, body: { ok: true } };
  vi.stubGlobal('fetch', fakeFetch);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function lastCall(): Recorded {
  return calls[calls.length - 1];
}

function pathOf(call: Recorded): string {
  return new URL(call.url).pathname;
}

describe('快照客户端：路径与方法', () => {
  it('七条读面走 GET，五条写面走 POST，路径逐条落在 /api/snapshot 下', async () => {
    await getSnapshotStatus();
    await listPendingSnapshots();
    await getSnapshotPreview('d-1');
    await getSnapshotDecisions();
    await getRelayStatus();
    await captureSnapshot({ text: '字' });
    await confirmSnapshot({ draftId: 'd-1' });
    await cancelSnapshot({ draftId: 'd-1' });
    await grantRelay({ workspaceId: 'ws', hosts: ['example.com'] });
    await decideRelay({ grantId: 'g-1' });
    await registerRelayTabs({ grantId: 'g-1', tabs: [] });
    await revokeRelay({ grantId: 'g-1' });

    expect(calls.map((c) => `${c.method} ${pathOf(c)}`)).toEqual([
      'GET /api/snapshot/status',
      'GET /api/snapshot/pending',
      'GET /api/snapshot/preview',
      'GET /api/snapshot/decisions',
      'GET /api/snapshot/relay/status',
      'POST /api/snapshot/capture',
      'POST /api/snapshot/confirm',
      'POST /api/snapshot/cancel',
      'POST /api/snapshot/relay/grant',
      'POST /api/snapshot/relay/decide',
      'POST /api/snapshot/relay/tabs',
      'POST /api/snapshot/relay/revoke',
    ]);
  });

  it('待确认清单默认不带 status：只列待确认那几份是引擎的缺省，界面不替它改成全量', async () => {
    reply = { status: 200, body: { ok: true, total: 0, items: [], statusFilter: 'pending', note: '' } };
    await listPendingSnapshots({ session: 's-1' });
    expect(lastCall().url).toContain('session=s-1');
    expect(lastCall().url).not.toContain('status=');

    await listPendingSnapshots({ session: 's-1', status: 'all' });
    expect(lastCall().url).toContain('status=all');
  });

  it('draftId 走查询串并做转义，不塞进路径', async () => {
    reply = { status: 200, body: { ok: true, draftId: 'a/b c', preview: '正文', classification: {} } };
    await getSnapshotPreview('a/b c');
    expect(new URL(lastCall().url).searchParams.get('draftId')).toBe('a/b c');
  });
});

describe('快照客户端：请求体形状与「不留正文」', () => {
  it('采集体只带引擎认的六个键，不夹带 includeImage 这类确认期开关', async () => {
    reply = { status: 200, body: { ok: true, draftId: 'd-1', preview: '今天调了三个页面的间距', classification: { sensitivity: 'public' } } };
    const row = await captureSnapshot({ text: '今天调了三个页面的间距', appLabel: '设计稿', session: 's-1' });
    expect(Object.keys(lastCall().body as Record<string, unknown>).sort()).toEqual(
      ['appLabel', 'imageBase64', 'imageMime', 'runtime', 'session', 'text'].sort(),
    );
    // 预览原样交给调用方：界面不重排、不截断、不另拼一份
    expect(row.preview).toBe('今天调了三个页面的间距');
  });

  it('确认体把两个闸的开关显式带上（缺省 false），引擎才不会被「界面没传」当成「用户勾了」', async () => {
    reply = {
      status: 200,
      body: { ok: true, draftId: 'd-1', contextBlocks: [], destroyed: true, note: '已生成上送载荷' },
    };
    await confirmSnapshot({ draftId: 'd-1' });
    expect(lastCall().body).toMatchObject({
      draftId: 'd-1',
      includeImage: false,
      acknowledgeInternal: false,
      session: '',
    });
    await confirmSnapshot({ draftId: 'd-1', acknowledgeInternal: true });
    expect(lastCall().body).toMatchObject({ acknowledgeInternal: true });
  });

  it('want 有缺省值：判定不许靠「猜要什么」通过', async () => {
    reply = { status: 200, body: { ok: true, allowed: true, code: '', message: '', deniedBy: '', deniedByLabel: '', grantId: 'g', host: '' } };
    await decideRelay({ grantId: 'g-1' });
    expect(lastCall().body).toMatchObject({ want: 'tabs', grantId: 'g-1' });
    await decideRelay({ grantId: 'g-1', want: 'login' });
    expect(lastCall().body).toMatchObject({ want: 'login' });
  });

  it('客户端不留正文副本：源里没有持久层与日志出口', async () => {
    const { readFileSync } = await import('node:fs');
    const { fileURLToPath } = await import('node:url');
    const raw = readFileSync(
      fileURLToPath(new URL('../../../src/renderer/lib/snapshot-api.ts', import.meta.url)),
      'utf8',
    ).replace(/\r\n/g, '\n');
    // 文件头写的正是「不写 localStorage、不进日志」：连注释一起扫会把「不做」扫成「做了」
    const src = raw
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/^\s*\/\/.*$/gm, '')
      .replace(/[ \t]+\/\/[^\n]*$/gm, '');
    for (const needle of ['localStorage', 'sessionStorage', 'document.cookie', 'console.log']) {
      expect(src, `不该出现 ${needle}`).not.toContain(needle);
    }
  });
});

describe('快照客户端：引擎的拒绝怎么回到界面', () => {
  it('机密级拦下时保住 code 与引擎原句，不改写成「参数有误」', async () => {
    reply = {
      status: 403,
      body: { ok: false, code: 'SNAPSHOT_CONFIDENTIAL_WITHHELD', message: '机密级快照不上送：没有绕过开关' },
    };
    const err = await confirmSnapshot({ draftId: 'd-1' }).catch((e) => e);
    expect(isSnapshotApiError(err)).toBe(true);
    expect(err).toBeInstanceOf(SnapshotApiError);
    expect(err.code).toBe('SNAPSHOT_CONFIDENTIAL_WITHHELD');
    expect(err.message).toBe('SNAPSHOT_CONFIDENTIAL_WITHHELD: 机密级快照不上送：没有绕过开关');
    expect(err.httpStatus).toBe(403);
  });

  it('远程身份的准入拒绝单独识别（不是「快照这一族没开启」那种含糊话）', async () => {
    reply = {
      status: 403,
      body: { ok: false, code: 'WS_DESKTOP_ONLY', message: '快照只对本机桌面壳开放（sub=remote）' },
    };
    const err = await getSnapshotStatus().catch((e) => e);
    expect(isDesktopOnlyRejection(err)).toBe(true);
    expect(err.message).toContain('只对本机桌面壳开放');
  });

  it('接力判定被拒时保住「谁拒的」：deniedBy 与标签一起回，界面按它选文案', async () => {
    reply = {
      status: 403,
      body: {
        ok: false,
        allowed: false,
        code: 'RELAY_DAILY_BROWSER_PROFILE',
        message: '这条授权指向日常浏览器目录：接力不碰它',
        deniedBy: 'controlled_boundary',
        deniedByLabel: '受控浏览器边界（不碰日常浏览器）',
        grantId: '',
        host: '',
      },
    };
    const err = await decideRelay({ grantId: 'g-1' }).catch((e) => e);
    expect(err.deniedBy).toBe('controlled_boundary');
    expect(err.deniedByLabel).toBe('受控浏览器边界（不碰日常浏览器）');
    expect(labelOf(RELAY_DENIAL_LABELS, err.deniedBy)).toBe(err.deniedByLabel);
  });

  it('引擎没应答时如实说「引擎不在」，不抛裸 TypeError', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new TypeError('fetch failed');
    }));
    const err = await getSnapshotStatus().catch((e) => e);
    expect(isSnapshotApiError(err)).toBe(true);
    expect(err.code).toBe('SNAPSHOT_ENGINE_UNREACHABLE');
    expect(err.httpStatus).toBe(0);
    expect(err.message).toContain('引擎没有应答');
  });

  it('非 JSON 的 5xx 回话也给出可读原因，不把 undefined 端上去', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 502, json: async () => (undefined as unknown) }) as unknown as Response),
    );
    const err = await listPendingSnapshots().catch((e) => e);
    expect(err.code).toBe('SNAPSHOT_HTTP_502');
    expect(err.message).toContain('HTTP 502');
  });

  it('ok:false 但 HTTP 200 也算失败（引擎两条族都这么回）', async () => {
    reply = { status: 200, body: { ok: false, code: 'SNAPSHOT_PARAM_MISSING', message: '缺少必填参数：draftId' } };
    const err = await cancelSnapshot({ draftId: '' as unknown as string }).catch((e) => e);
    expect(err.code).toBe('SNAPSHOT_PARAM_MISSING');
    expect(err.message).toContain('缺少必填参数');
  });
});

describe('快照客户端：口径原样转述', () => {
  it('未验收清单逐条带出，前端不裁掉也不好话', async () => {
    reply = {
      status: 200,
      body: {
        ok: true,
        snapshot: { unverified: ['本模块不截屏 ⇒ 真机采集未验收'], pending: 0, drafts: 0 },
        relay: { unverified: ['真实受控浏览器会话下的接力行为未验收'], grants: 0 },
        endpoints: [],
        readOnlyPaths: [],
      },
    };
    const view = await getSnapshotStatus();
    const items = snapshotUnverifiedViews(view);
    expect(items.snapshot).toEqual(['本模块不截屏 ⇒ 真机采集未验收']);
    expect(items.relay).toEqual(['真实受控浏览器会话下的接力行为未验收']);
    expect(items.snapshot.join(' ')).toContain('未验收');
  });

  it('撤销要么给授权、要么给工作区：两个都不给时不静默成功', async () => {
    reply = {
      status: 404,
      body: { ok: false, code: 'RELAY_GRANT_NOT_FOUND', message: '撤销要么给 grantId，要么给 workspaceId：两个都不给不撤' },
    };
    const err = await revokeRelay({}).catch((e) => e);
    expect(err.code).toBe('RELAY_GRANT_NOT_FOUND');
    expect(err.message).toContain('两个都不给不撤');
  });

  it('登记标签页只交现场清单：本车道不枚举标签页，客户端也不假装能', async () => {
    reply = { status: 200, body: { ok: true, allowed: true, code: '', message: '已登记 2 个标签页', deniedBy: '', deniedByLabel: '', grantId: 'g-1', host: '', grant: { grantId: 'g-1', tabCount: 2 } } };
    const row = await registerRelayTabs({
      grantId: 'g-1',
      tabs: [
        { tabId: '1', url: 'https://example.com/a?token=secret', title: 'A' },
        { tabId: '2', url: 'https://example.com/b' },
      ],
    });
    expect(lastCall().body).toMatchObject({ grantId: 'g-1' });
    expect((lastCall().body?.tabs as unknown[]).length).toBe(2);
    // 客户端不预先清洗 URL：剥查询串是引擎的活，两边各做一遍就会口径不一
    expect((lastCall().body?.tabs as Record<string, unknown>[])[0].url).toContain('?token=secret');
    expect(row.grant?.tabCount).toBe(2);
  });
});
