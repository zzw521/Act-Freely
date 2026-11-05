/**
 * 渲染层的应用快照 / 浏览器上下文接力客户端（#140 · D-13 · workspace 车道）
 *
 * 走的是**引擎 HTTP**，不是 IPC：`/api/snapshot/*` 这 12 条挂在 workspace 车道上
 * （`engine/routes_workspace.py`），渲染层用 `engineFetch` 直连本机引擎。
 * `contracts_workspace.ts` 里的 IPC 预留段仍是空的，桌面壳那半边等 W4 交棒后再接，
 * 本文件不新增、也不假装存在任何 IPC 命令。
 *
 * 三条与引擎同源的口径，写代码时不许越过：
 * 1. **这里不截屏**。`captureSnapshot` 的正文与图片由调用方交进来（真实屏幕采集在
 *    桌面壳里，全局快捷键与 OS 捕获均未验收）；本文件只负责把采集物递给引擎。
 * 2. **正文只活在引擎进程内存里**。本模块不缓存预览文本、不写 localStorage、
 *    不进日志——没有模块级的正文变量，`getSnapshotPreview` 拿到什么就原样交给调用方。
 * 3. **结论码与标签只有一份事实源**（引擎）。`src/shared/snapshot-contract.ts` 是镜像，
 *    漂移由 `tests/unit/snapshot` 反向钉住；界面按 `code` 选文案，不自己拼句子。
 *
 * 错误口径：引擎回 `{ok:false, code, message}` 时抛 `SnapshotApiError`，
 * `message` 用引擎原句（那些句子是给这台机器上的人看的，不是给人翻译的错误码），
 * 接力判定额外带 `deniedBy` / `deniedByLabel`，供界面区分「谁拒的」。
 */

import {
  SNAPSHOT_ENDPOINTS,
  type PendingSnapshotList,
  type RelayDecideInput,
  type RelayGrantInput,
  type RelayGrantResult,
  type RelayStatusView,
  type RelayTabInput,
  type SnapshotCancelResult,
  type SnapshotCaptureInput,
  type SnapshotConfirmInput,
  type SnapshotDecisionsView,
  type SnapshotPreview,
  type SnapshotStatusView,
  type SnapshotUplinkPayload,
  snapshotErrorMessage,
} from '../../shared/snapshot-contract';
import { resolveEngineBaseUrl } from './engine-endpoint';
import { engineFetch } from './engine-http';

/** 引擎这一族的失败回话：保住 code / message / 拒绝来源，不把三段压成一句话 */
export class SnapshotApiError extends Error {
  readonly code: string;
  readonly httpStatus: number;
  readonly deniedBy: string;
  readonly deniedByLabel: string;

  constructor(args: { code: string; message: string; httpStatus: number; deniedBy?: string; deniedByLabel?: string }) {
    super(`${args.code}: ${args.message}`);
    this.name = 'SnapshotApiError';
    this.code = args.code;
    this.httpStatus = args.httpStatus;
    this.deniedBy = args.deniedBy ?? '';
    this.deniedByLabel = args.deniedByLabel ?? '';
  }
}

export function isSnapshotApiError(error: unknown): error is SnapshotApiError {
  return error instanceof SnapshotApiError;
}

/** 快照这一族只对本机桌面壳开放；远程身份拿到的是 WS_DESKTOP_ONLY，界面要单独说一句 */
export function isDesktopOnlyRejection(error: unknown): boolean {
  return isSnapshotApiError(error) && error.code === 'WS_DESKTOP_ONLY';
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

async function callSnapshot<T>(
  path: string,
  workspaceId: string | undefined,
  init?: RequestInit,
): Promise<T> {
  const base = await resolveEngineBaseUrl(workspaceId);
  let resp: Response;
  try {
    resp = await engineFetch(`${base}${path}`, init);
  } catch {
    throw new SnapshotApiError({
      code: 'SNAPSHOT_ENGINE_UNREACHABLE',
      message: '引擎没有应答：快照与接力都活在引擎进程的内存里，引擎不在就没有可确认的草稿',
      httpStatus: 0,
    });
  }
  const body = (await resp.json().catch(() => null)) as Record<string, unknown> | null;
  if (!resp.ok || !body || body.ok === false) {
    const code = str(body?.code) || `SNAPSHOT_HTTP_${resp.status}`;
    throw new SnapshotApiError({
      code,
      message: snapshotErrorMessage(body, `引擎快照接口失败（HTTP ${resp.status}）`),
      httpStatus: resp.status,
      deniedBy: str(body?.deniedBy),
      deniedByLabel: str(body?.deniedByLabel),
    });
  }
  return body as T;
}

const jsonPost = (body: Record<string, unknown>): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

function qs(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '' && value !== null) search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

// ── 快照流水线：触发 → 预览 → 确认 / 取消 ──────────────────────

/** 一次读完两族口径（上限、状态机、分级表、拒绝来源、结论码、未验收清单） */
export function getSnapshotStatus(workspaceId?: string): Promise<SnapshotStatusView> {
  return callSnapshot<SnapshotStatusView>(SNAPSHOT_ENDPOINTS.status, workspaceId);
}

/**
 * 递交一份采集物并立刻拿回预览。
 *
 * 返回的就是「确认后会发出去的那一段」——引擎用同一个函数产出预览与上送载荷，
 * 界面不需要（也不应该）自己再拼一份预览。正文只在这一次调用栈里存在。
 */
export function captureSnapshot(input: SnapshotCaptureInput, workspaceId?: string): Promise<SnapshotPreview> {
  return callSnapshot<SnapshotPreview>(
    SNAPSHOT_ENDPOINTS.capture,
    workspaceId,
    jsonPost({
      text: input.text ?? '',
      imageBase64: input.imageBase64 ?? '',
      imageMime: input.imageMime ?? '',
      appLabel: input.appLabel ?? '',
      session: input.session ?? '',
      runtime: input.runtime ?? {},
    }),
  );
}

/**
 * 待确认清单：只有元数据（长度、指纹、分级），**没有正文**。
 * 引擎默认只列 pending；要连已确认/已取消/已过期的墓碑一起看，显式传 `status: 'all'`。
 */
export function listPendingSnapshots(
  args: { session?: string; status?: string } = {},
  workspaceId?: string,
): Promise<PendingSnapshotList> {
  return callSnapshot<PendingSnapshotList>(
    `${SNAPSHOT_ENDPOINTS.pending}${qs({ session: args.session, status: args.status })}`,
    workspaceId,
  );
}

/** 上送前可见的那一眼（唯一会回正文的读面；调用方拿完即弃，本模块不留副本） */
export function getSnapshotPreview(draftId: string, workspaceId?: string): Promise<SnapshotPreview> {
  return callSnapshot<SnapshotPreview>(
    `${SNAPSHOT_ENDPOINTS.preview}${qs({ draftId })}`,
    workspaceId,
  );
}

/** 确认上送：机密级在这里被硬拦，没有绕过开关；内部敏感必须显式 acknowledgeInternal */
export function confirmSnapshot(input: SnapshotConfirmInput, workspaceId?: string): Promise<SnapshotUplinkPayload> {
  return callSnapshot<SnapshotUplinkPayload>(
    SNAPSHOT_ENDPOINTS.confirm,
    workspaceId,
    jsonPost({
      draftId: input.draftId,
      includeImage: Boolean(input.includeImage),
      acknowledgeInternal: Boolean(input.acknowledgeInternal),
      session: input.session ?? '',
    }),
  );
}

/** 取消即销毁：正文与图片一起没，之后再确认也不会把它捞回来 */
export function cancelSnapshot(
  input: { draftId: string; reason?: string },
  workspaceId?: string,
): Promise<SnapshotCancelResult> {
  return callSnapshot<SnapshotCancelResult>(
    SNAPSHOT_ENDPOINTS.cancel,
    workspaceId,
    jsonPost({ draftId: input.draftId, reason: input.reason ?? '' }),
  );
}

/** 决策审计（快照 + 接力两条账并排，都只有长度、指纹、域名与结论） */
export function getSnapshotDecisions(
  args: { limit?: number; offset?: number; draftId?: string; grantId?: string; action?: string } = {},
  workspaceId?: string,
): Promise<SnapshotDecisionsView> {
  return callSnapshot<SnapshotDecisionsView>(
    `${SNAPSHOT_ENDPOINTS.decisions}${qs({
      limit: args.limit,
      offset: args.offset,
      draftId: args.draftId,
      grantId: args.grantId,
      action: args.action,
    })}`,
    workspaceId,
  );
}

// ── 浏览器上下文接力：授权 → 判定 → 登记 → 撤销 ────────────────

/** 当前有哪些接力授权、各自授给了哪些域，以及四类拒绝来源的标签表 */
export function getRelayStatus(workspaceId?: string): Promise<RelayStatusView> {
  return callSnapshot<RelayStatusView>(
    `${SNAPSHOT_ENDPOINTS.relayStatus}${qs({ workspaceId })}`,
    workspaceId,
  );
}

/**
 * 开一次接力授权（按工作区 + 域）。
 *
 * 指向日常浏览器目录的授权开不出来；这里不枚举标签页、不读 Cookie、不发起 CDP——
 * 现场由调用方交上来，浏览器动作归 W5 的 `/api/browser` 车道。
 */
export function grantRelay(input: RelayGrantInput, workspaceId?: string): Promise<RelayGrantResult> {
  return callSnapshot<RelayGrantResult>(
    SNAPSHOT_ENDPOINTS.relayGrant,
    workspaceId ?? input.workspaceId,
    jsonPost({
      workspaceId: input.workspaceId,
      hosts: input.hosts ?? [],
      session: input.session ?? '',
      profileDir: input.profileDir ?? '',
      runtime: input.runtime ?? {},
    }),
  );
}

/** 这次能不能复用已授权会话里的标签页 / 登录态（want 只认 tabs / login / url） */
export function decideRelay(input: RelayDecideInput, workspaceId?: string): Promise<RelayGrantResult> {
  return callSnapshot<RelayGrantResult>(
    SNAPSHOT_ENDPOINTS.relayDecide,
    workspaceId ?? input.workspaceId,
    jsonPost({
      grantId: input.grantId,
      want: input.want ?? 'tabs',
      url: input.url ?? '',
      workspaceId: input.workspaceId ?? '',
      session: input.session ?? '',
      runtime: input.runtime ?? {},
    }),
  );
}

/** 把调用方交上来的标签页登记进这条授权（URL 查询串会被剥掉，令牌不进接力现场） */
export function registerRelayTabs(
  input: { grantId: string; tabs: RelayTabInput[] },
  workspaceId?: string,
): Promise<RelayGrantResult> {
  return callSnapshot<RelayGrantResult>(
    SNAPSHOT_ENDPOINTS.relayTabs,
    workspaceId,
    jsonPost({ grantId: input.grantId, tabs: input.tabs ?? [] }),
  );
}

/** 撤销：按一条授权撤，或按工作区整体撤；立即生效，不看剩余有效期 */
export function revokeRelay(
  input: { grantId?: string; workspaceId?: string; reason?: string },
  workspaceId?: string,
): Promise<RelayGrantResult> {
  return callSnapshot<RelayGrantResult>(
    SNAPSHOT_ENDPOINTS.relayRevoke,
    workspaceId,
    jsonPost({
      grantId: input.grantId ?? '',
      workspaceId: input.workspaceId ?? '',
      reason: input.reason ?? '',
    }),
  );
}

/**
 * 页面要的「这屏说人话」摘要：只从引擎回话里取，前端不另立口径。
 * 未验收清单必须原样带出去——把「适配器写完了」说成「真机可用」是本车道最容易造的假账。
 */
export function snapshotUnverifiedViews(view: SnapshotStatusView): { snapshot: string[]; relay: string[] } {
  return {
    snapshot: view?.snapshot?.unverified ?? [],
    relay: view?.relay?.unverified ?? [],
  };
}
