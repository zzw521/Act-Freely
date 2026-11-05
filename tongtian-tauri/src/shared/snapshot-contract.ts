// ============================================================
// src/shared/snapshot-contract.ts — D-13 应用快照与浏览器上下文接力的**引擎 HTTP 契约**
// ============================================================
// 归属：W3（#140）。这里是渲染层与 `engine/routes_workspace.py` 的 /api/snapshot/* 一族
// 共用的形状与码表；**不是 IPC 契约**——本族经引擎 HTTP 直连（渲染层 engineFetch），
// workspace 车道的 IPC 仍留在 contracts_workspace.ts 的空预留段里等 W4 交棒。
//
// 三条不许越过的口径（与引擎侧 snapshot_runtime.py / snapshot_relay.py 文件头同源）：
// 1. 渲染层**不截屏**：正文与图片由调用方交进来（真机采集在桌面壳里，未验收）；
// 2. 正文只活在引擎进程内存里：本模块不缓存、不落 localStorage、不写进任何日志；
// 3. 结论码与拒绝来源标签只有**一份事实源**（引擎），这里只做镜像，并由
//    tests/unit/snapshot 反向钉住漂移。

export const SNAPSHOT_ENDPOINTS = {
  status: '/api/snapshot/status',
  capture: '/api/snapshot/capture',
  pending: '/api/snapshot/pending',
  preview: '/api/snapshot/preview',
  confirm: '/api/snapshot/confirm',
  cancel: '/api/snapshot/cancel',
  decisions: '/api/snapshot/decisions',
  relayStatus: '/api/snapshot/relay/status',
  relayGrant: '/api/snapshot/relay/grant',
  relayDecide: '/api/snapshot/relay/decide',
  relayTabs: '/api/snapshot/relay/tabs',
  relayRevoke: '/api/snapshot/relay/revoke',
} as const;

/** 分级三档（与 Rust classify.rs 与引擎 snapshot_runtime.py 同一套字面量） */
export type SnapshotSensitivity = 'public' | 'internal' | 'confidential';

export const SENSITIVITY_LABELS: Record<SnapshotSensitivity, string> = {
  public: '未检出敏感',
  internal: '内部敏感',
  confidential: '机密',
};

/** 草稿状态机：待确认 = 还没上送，这是界面最需要说清的一格 */
export type SnapshotStatus = 'pending' | 'confirmed' | 'cancelled' | 'expired' | 'withheld';

export const STATUS_LABELS: Record<SnapshotStatus, string> = {
  pending: '待确认（尚未上送）',
  confirmed: '已确认并生成上送载荷',
  cancelled: '已取消（草稿已销毁）',
  expired: '已过期（草稿已销毁）',
  withheld: '被拦下（机密级不上送）',
};

/** 快照族结论码（镜像 engine/snapshot_runtime.py 的 CODE_*） */
export const SNAPSHOT_CODES = {
  nothingCaptured: 'SNAPSHOT_NOTHING_CAPTURED',
  textTooLarge: 'SNAPSHOT_TEXT_TOO_LARGE',
  imageTooLarge: 'SNAPSHOT_IMAGE_TOO_LARGE',
  imageFormat: 'SNAPSHOT_IMAGE_FORMAT_UNSUPPORTED',
  notFound: 'SNAPSHOT_NOT_FOUND',
  expired: 'SNAPSHOT_EXPIRED',
  confidential: 'SNAPSHOT_CONFIDENTIAL_WITHHELD',
  internalNeedsAck: 'SNAPSHOT_INTERNAL_NEEDS_ACK',
  redactionBlocked: 'SNAPSHOT_REDACTION_BLOCKED',
  badBase64: 'SNAPSHOT_IMAGE_BASE64_INVALID',
  limitInvalid: 'SNAPSHOT_LIMIT_INVALID',
  paramMissing: 'SNAPSHOT_PARAM_MISSING',
} as const;

/** 接力族结论码（镜像 engine/snapshot_relay.py 的 CODE_*，值即引擎回话里的 code） */
export const RELAY_CODES = {
  dailyProfile: 'RELAY_DAILY_BROWSER_PROFILE',
  profileNotMatchingSession: 'RELAY_PROFILE_NOT_MATCHING_SESSION',
  noSession: 'RELAY_NO_CONTROLLED_SESSION',
  policyDisabled: 'RELAY_BROWSER_POLICY_DISABLED',
  notGranted: 'RELAY_NOT_GRANTED',
  hostNotGranted: 'RELAY_HOST_NOT_IN_GRANT',
  workspaceMismatch: 'RELAY_WORKSPACE_MISMATCH',
  grantExpired: 'RELAY_GRANT_EXPIRED',
  grantRevoked: 'RELAY_GRANT_REVOKED',
  grantNotFound: 'RELAY_GRANT_NOT_FOUND',
  tabsOverflow: 'RELAY_TAB_LIMIT',
  hostsOverflow: 'RELAY_HOST_LIMIT',
  badUrl: 'RELAY_URL_REJECTED',
  wantInvalid: 'RELAY_WANT_INVALID',
} as const;

/**
 * 接力拒绝的四类来源：引擎回话里的 `deniedBy` 就是这些 snake_case 值，
 * 界面按它选文案；`deniedByLabel` 由引擎同批发回，两边各说一句、不许并成一句。
 */
export type RelayDenialSource = 'controlled_boundary' | 'grant' | 'browser_policy' | 'session';

export const RELAY_DENIAL_LABELS: Record<RelayDenialSource, string> = {
  controlled_boundary: '受控浏览器边界（不碰日常浏览器）',
  grant: '本次接力授权（工作区 / 域 / 有效期 / 撤销）',
  browser_policy: '浏览器档位',
  session: '受控会话现场（浏览器没在跑就没有可接力的东西）',
};

/**
 * 一条命中项。键名是 `kind` 不是 `type`——引擎镜像发的是 `kind`
 * （`src-tauri/src/security/classify.rs` 的 finding 里那枚叫 `type`，两处不同源，
 * 界面读的是引擎这份，别按 Rust 的键名找）。
 */
export interface SnapshotFinding {
  kind: string;
  pattern: string;
  severity: string;
  description: string;
}

export interface SnapshotClassification {
  sensitivity: SnapshotSensitivity;
  sensitivityLabel: string;
  confidence: number;
  findings: SnapshotFinding[];
  summary: string;
  pii: boolean;
  forceLocal: boolean;
  secretHits: number;
}

export interface SnapshotDraftRow {
  draftId: string;
  taskId: string;
  appLabel: string;
  origin: string;
  status: SnapshotStatus;
  statusLabel: string;
  createdAt: number;
  expiresAt: number;
  hasImage: boolean;
  imageMime: string;
  imageBytes: number;
  textChars: number;
  classification: SnapshotClassification;
  outcomeCode: string;
  outcomeNote: string;
}

/** 预览载荷：界面展示的就是要发出去的那一段，逐字相等由引擎保证并自测钉住 */
export interface SnapshotPreview extends SnapshotDraftRow {
  /** 采集端点自带这一层成没成的布尔；草稿行本身不带（清单里全是已存在的草稿） */
  ok: boolean;
  preview: string;
  previewChars: number;
  truncated: boolean;
  uplinkLimit: number;
  redactionMode: string;
  redactedCounts: Record<string, number>;
  imageIncluded: boolean;
  imageNote: string;
  wouldUplink: boolean;
  needsAck: boolean;
  ttlMinutes: number;
  storageNote: string;
}

/** 确认后的上送载荷：本模块只产出 contextBlocks，通道选择不经过这里 */
export interface SnapshotUplinkPayload {
  ok: boolean;
  draftId: string;
  taskId: string;
  appLabel: string;
  contextBlocks: Array<Record<string, unknown>>;
  textChars: number;
  truncated: boolean;
  redactionMode: string;
  redactedCounts: Record<string, number>;
  imageIncluded: boolean;
  forceLocal: boolean;
  sensitivity: SnapshotSensitivity;
  routeNote: string;
  repeatConfirm: boolean;
  note: string;
  destroyed: boolean;
}

/**
 * 取消回话：引擎把**整条草稿行**原样退回，再叠上销毁计数。
 *
 * 之所以不是只回四个键：界面取消后要立刻把这一行改状态、并把「正文销毁了多少字节」
 * 摆在同一张卡上——只回 draftId 就得再拉一次清单，两次读之间清单还可能变。
 */
export interface SnapshotCancelResult extends SnapshotDraftRow {
  ok: boolean;
  destroyed: boolean;
  hadUplink: boolean;
  cancelNote: string;
}

export interface RelayDecisionView {
  ok: boolean;
  allowed: boolean;
  code: string;
  message: string;
  deniedBy: string;
  deniedByLabel: string;
  grantId: string;
  host: string;
  grant?: RelayGrantRow;
}

/**
 * 一条接力的标签页行：引擎只留 `host`，**不回 url**。
 * 登记的输入面收完整 URL（`RelayTabInput.url`），落进现场时被剥成域名——
 * 查询串里的令牌就是这么挡在接力之外的。界面对这一行不许再找 `url`。
 */
export interface RelayTabRow {
  tabId: string;
  host: string;
  title: string;
  active: boolean;
}

export interface RelayGrantRow {
  grantId: string;
  workspaceId: string;
  profileFingerprint: string;
  hosts: string[];
  taskIds: string[];
  tabCount: number;
  tabs: RelayTabRow[];
  createdAt: number;
  expiresAt: number;
  expired: boolean;
  revoked: boolean;
  reuseLogin: boolean;
  reuseTabs: boolean;
  note: string;
  /** 撤销那句：为什么撤、什么时候撤（没撤过就是空文本，不是缺失的键） */
  revokeNote: string;
}

/** 快照那本账的条目：只有长度、指纹、分级与结论，正文一概不落 */
export interface SnapshotDecisionRow {
  ts: number;
  action: string;
  ok: boolean;
  draftId: string;
  taskId: string;
  appLabel: string;
  origin: string;
  status: SnapshotStatus;
  sensitivity: SnapshotSensitivity;
  pii: boolean;
  forceLocal: boolean;
  withheld: boolean;
  hitPatterns: string[];
  redactionMode: string;
  privacyMode: string;
  textBytes: number;
  textFingerprint: string;
  imageBytes: number;
  /** 销毁时记下的「清掉了多少」：与被清掉的正文本身无关，只有字节数与指纹 */
  destroyedTextBytes?: number;
  destroyedTextFingerprint?: string;
  destroyedImageBytes?: number;
  [key: string]: unknown;
}

/** 接力那本账的条目：域名、配置目录指纹与结论；没有 Cookie、没有令牌、没有完整路径 */
export interface RelayDecisionRow {
  ts: number;
  action: string;
  allowed: boolean;
  code: string;
  reason: string;
  deniedBy: string;
  grantId: string;
  host: string;
  workspaceId: string;
  profileFingerprint: string;
  [key: string]: unknown;
}

export interface PagedSnapshotDecisions {
  total: number;
  items: SnapshotDecisionRow[];
  hasMore: boolean;
  note: string;
}

export interface PagedRelayDecisions {
  total: number;
  items: RelayDecisionRow[];
  hasMore: boolean;
  note: string;
}

/** 引擎回话里的 `{value,label}` 二列表（分级表、状态表、拒绝来源表都是这个形状） */
export interface LabeledValue {
  value: string;
  label: string;
}

/** `/api/snapshot/status` 里的快照族口径（引擎 `SnapshotRegistry.status()` 的回话） */
export interface SnapshotRegistryStatus {
  drafts: number;
  pending: number;
  ttlMinutes: number;
  maxPendingDrafts: number;
  maxUplinkTextChars: number;
  maxImageBytes: number;
  imageMimeWhitelist: string[];
  sensitivityLevels: LabeledValue[];
  statuses: LabeledValue[];
  codes: string[];
  unverified: string[];
  note: string;
}

/** `/api/snapshot/status` 里的接力族口径（引擎 `RelayRegistry.status()` 的回话） */
export interface RelayRegistryStatus {
  grants: number;
  active: number;
  ttlMinutes: number;
  maxGrants: number;
  maxHostsPerGrant: number;
  maxTabsPerGrant: number;
  denialSources: LabeledValue[];
  codes: string[];
  dailyProfileMarkers: string[];
  unverified: string[];
  note: string;
}

export interface SnapshotStatusView {
  ok: boolean;
  snapshot: SnapshotRegistryStatus;
  relay: RelayRegistryStatus;
  endpoints: string[];
  readOnlyPaths: string[];
}

/** `/api/snapshot/pending`：只有元数据，列表里没有正文键 */
export interface PendingSnapshotList {
  ok: boolean;
  total: number;
  items: SnapshotDraftRow[];
  statusFilter: string;
  note: string;
}

/** 两条账并排给：同一页参数，两本账各自的条目形状不同（谁都不许冒充对方） */
export interface SnapshotDecisionsView {
  ok: boolean;
  snapshot: PagedSnapshotDecisions;
  relay: PagedRelayDecisions;
  note: string;
}

export interface RelayStatusView {
  ok: boolean;
  grants: RelayGrantRow[];
  summary: RelayRegistryStatus;
  denialSources: LabeledValue[];
  note: string;
}

/** 授权/登记/撤销这一类的回话：判定 + 那条授权的最新行（`grant` 键由引擎按需带上） */
export interface RelayGrantResult extends RelayDecisionView {
  grant?: RelayGrantRow;
}

/** 采集入参：正文与图片都由调用方交进来，本车道不截屏 */
export interface SnapshotCaptureInput {
  text: string;
  imageBase64?: string;
  imageMime?: string;
  appLabel?: string;
  session?: string;
  runtime?: Record<string, unknown>;
}

export interface SnapshotConfirmInput {
  draftId: string;
  includeImage?: boolean;
  acknowledgeInternal?: boolean;
  session?: string;
}

export interface RelayGrantInput {
  workspaceId: string;
  hosts: string[];
  session?: string;
  profileDir?: string;
  runtime?: Record<string, unknown>;
}

export type RelayWant = 'tabs' | 'login' | 'url';

export interface RelayDecideInput {
  grantId: string;
  want?: RelayWant;
  url?: string;
  workspaceId?: string;
  session?: string;
  runtime?: Record<string, unknown>;
}

export interface RelayTabInput {
  tabId: string;
  url: string;
  title?: string;
  active?: boolean;
}

/** 引擎回话里的错误形状：code + message 两段都可能有，取到的那一段优先 */
export function snapshotErrorMessage(payload: unknown, fallback: string): string {
  if (payload && typeof payload === 'object') {
    const row = payload as { message?: unknown; reason?: unknown; detail?: unknown };
    for (const candidate of [row.message, row.reason, row.detail]) {
      if (typeof candidate === 'string' && candidate.trim()) return candidate;
    }
  }
  return fallback;
}

/**
 * 准入闸的结论码：快照这一族走的 `_desktop_only` 回的是后端那族的码（WS_ 前缀）。
 * 单列出来，是为了让它不混进 `SNAPSHOT_CODES` / `RELAY_CODES`——那两张表是
 * 「这件事办成了没有」的结论，这枚是「这个身份够不够格问」。
 */
export const SNAPSHOT_ADMISSION_CODE = 'WS_DESKTOP_ONLY';

/**
 * 按值取标签：引擎新增一档分级/状态时，界面宁可回显原值，也不能显示空白。
 * （空白会让人以为「没分级」，而实际是这份表跟不上引擎了。）
 */
export function labelOf(table: Record<string, string>, value: string): string {
  return table[value] ?? value;
}

