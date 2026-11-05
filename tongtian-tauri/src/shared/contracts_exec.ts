// ============================================================
// src/shared/contracts_exec.ts — exec 车道的通道预留段
// ============================================================
// 归属：W4（#165 D-14.5 / #150 D-48 prepare_fn 与按剩余窗口取页 / #159 D-12 更新回路 / 沙箱、写暂存审批与人格模板）。
// 口径：`exec:xxx` 的冒号后半段即 Rust 侧 dispatch 的 method 名。

export const IPC_LANE_EXEC = {
  channel: 'lane:exec',
  command: 'lane_exec',
} as const;

/**
 * D-12 应用更新回路（#159）：`exec:update_*` 十五条通道。
 *
 * 值必须与 `src-tauri/src/app_update/mod.rs` 的 `METHODS` 逐字同源，
 * 由 `tests/unit/shared/exec-lane-update-contract.test.ts` 双向核对：
 * 少一条 = 渲染层拿不到这个方法；多一条 = 车道会回 `IPC_LANE_NOT_IMPLEMENTED`，
 * 而那条回话说不出「为什么这个动作不存在」，界面只能按失败处理。
 */
export const IPC_CHANNELS_EXEC = {
  UPDATE_DESCRIBE: 'exec:update_describe',
  UPDATE_CHANNEL_GET: 'exec:update_channel_get',
  UPDATE_CHANNEL_SET: 'exec:update_channel_set',
  UPDATE_TRUST_LIST: 'exec:update_trust_list',
  UPDATE_TRUST_ADD: 'exec:update_trust_add',
  UPDATE_TRUST_REMOVE: 'exec:update_trust_remove',
  UPDATE_CHECK: 'exec:update_check',
  UPDATE_DOWNLOAD: 'exec:update_download',
  UPDATE_STATUS: 'exec:update_status',
  UPDATE_INSTALL_PLAN: 'exec:update_install_plan',
  UPDATE_INSTALL: 'exec:update_install',
  UPDATE_ROLLBACK_PLAN: 'exec:update_rollback_plan',
  UPDATE_ROLLBACK: 'exec:update_rollback',
  UPDATE_IMPORT_LOCAL: 'exec:update_import_local',
  UPDATE_LEDGER: 'exec:update_ledger',
} as const satisfies Record<string, string>;

// ============================================================
// D-03.5 写暂存（#161）：引擎 HTTP 面 `/api/staging/*` 的响应形状
// ============================================================
// 这一族**不走 IPC**：桌面渲染层用 engine-endpoint.ts 的 engineFetch 直连本机引擎
// （令牌由壳注入），所以这里只登记「回话长什么样」，不新增 channel 名。
// 字段名与 engine/write_staging_runtime.py 的 meta / 回话逐字对齐，改一边就要改另一边；
// 自测 tests/integration/engine_write_staging_selftest.py 按 Python 侧的键集反向核对。

/** 档位：off = 照旧直接写盘；staged = 文本写入先进暂存区，批准 diff 后才合并。 */
export type WriteStagingMode = 'off' | 'staged';

/** 一条暂存改动（`/api/staging/pending` 的 items，也是 `/api/staging/change` 的主体）。 */
export interface StagingChange {
  changeId: string;
  taskId: string;
  taskSlug: string;
  /** 解析后的绝对路径：合并只认它，界面展示用 displayPath。 */
  targetPath: string;
  displayPath: string;
  /** 暂存时绑定的工作目录；`apply` 靠它重判位置，没有就是空串。 */
  workDir: string;
  tool: 'write_text_file' | 'insert_text_file' | string;
  status: 'pending' | 'stale' | string;
  createdAtMs: number;
  updatedAtMs: number;
  /** 同一目标被重复写了几次（只留最后一条待审阅，不是叠成几条）。 */
  supersededCount: number;
  baseExists: boolean;
  baseSha: string;
  baseSize: number;
  baseMtimeMs: number;
  newSize: number;
  newSha: string;
  /** false = 有一侧不是文本，界面按「整份替换」审阅。 */
  diffAvailable: boolean;
  diffNote: string;
  diffLines: number;
  diffAdded: number;
  diffRemoved: number;
  diffTruncated: boolean;
  /** 以下四件由 `_refresh_state` 每次读盘现算，不在 meta 里存第二份事实。 */
  targetChanged: boolean;
  targetChangeNote: string;
  /** 只有 pending 且未过期才 true；界面据此决定「批准」按钮亮不亮。 */
  approvable: boolean;
}

/** GET /api/staging/pending */
export interface StagingPendingResponse {
  ok: boolean;
  observed: boolean;
  items: StagingChange[];
  total: number;
  truncated: boolean;
  staleCount: number;
  /** 坏记录的处理结果（改名留证 + 一句原因），非空就要摊给用户。 */
  loadErrors: string[];
  stagingDir: string;
  note: string;
}

/** GET /api/staging/change —— 详情比列表多 diff 正文与待写入内容。 */
export interface StagingChangeDetail extends StagingChange {
  ok: boolean;
  diffText: string;
  newContent: string;
  newContentTextual: boolean;
  newContentNote?: string;
  newSizeOnDisk?: number;
  diffReadError?: string;
  reason?: string;
}

/** GET /api/staging/status */
export interface StagingStatusResponse {
  ok: boolean;
  mode: WriteStagingMode;
  modeLabel: string;
  staged: boolean;
  /** 给用户看得见的一句说明（含非法档位那次）。 */
  note: string;
  /** 档位取值非法时的原因；合法值与缺省都是空串。 */
  error: string;
  pendingTotal: number;
  staleCount: number;
  limits: { contentBytes: number; diffLines: number; pendingPerTask: number };
  /** 档位取自哪一级（会话 runtime / 缺省），界面要说清「这次不是按你那轮的档位报的」。 */
  policySource: string;
  stagingDir: string;
}

/** 一次决定的落账（GET /api/staging/decisions）：只有指纹与字节数，绝不含内容本体。 */
export interface StagingDecisionRow {
  outcome: 'applied' | 'rejected' | 'stale' | 'denied' | string;
  changeId: string;
  targetPath: string;
  taskId?: string;
  baseSha?: string;
  newSha?: string;
  writtenBytes?: number;
  reason?: string;
  loggedAtMs: number;
  persisted: boolean;
  persistError?: string;
}

/** GET /api/staging/decisions */
export interface StagingDecisionsResponse {
  ok: boolean;
  items: StagingDecisionRow[];
  total: number;
  truncated: boolean;
  reason?: string;
}

/** POST /api/staging/apply —— 唯一会真正写用户文件的动作。 */
export interface StagingApplyResponse {
  ok: boolean;
  changeId: string;
  written: boolean;
  /** 业务拒绝（过期 / 找不到 / 位置越界 / 指纹不符）都回 200，界面按这句说人话。 */
  reason: string;
  note: string;
  targetPath?: string;
  displayPath?: string;
  writtenBytes?: number;
  /** 覆盖前留档的代价或「这次没留档」的明说；产物版本历史没点亮时也有一句。 */
  historyNote?: string;
}

/** POST /api/staging/reject */
export interface StagingRejectResponse {
  ok: boolean;
  changeId: string;
  reason: string;
  note?: string;
  /** 拒绝路径的特征：目标文件从头到尾没被碰过。 */
  targetUntouched?: boolean;
  targetExistedBefore?: boolean;
}

/** POST /api/staging/discard */
export interface StagingDiscardResponse {
  ok: boolean;
  dropped: number;
  changeIds: string[];
  failed: string[];
  note: string;
}

/** 引擎 JSONResponse 的错误面（_fail）：业务拒绝走各自动作自己的 ok=false + reason。 */
export interface StagingErrorEnvelope {
  ok: false;
  code: string;
  message: string;
}

// ============================================================
// D-42 人格模板（#161）：引擎 HTTP 面 `/api/persona/*` 的响应形状
// ============================================================
// 同样不走 IPC：清单与草稿都从本机引擎的 HTTP 面取。真正保存专家的仍然是现有的
// `expert_save` 那一次调用——本族只负责「点保存之前长什么样、会不会被拒」。
// 字段名与 engine/persona_runtime.py 的导出形状逐字对齐；
// 自测 tests/integration/engine_persona_templates_selftest.py 按 Python 侧键集反向核对。

/** 一套人格模板：`persona_runtime._tpl` 的导出形状（行为风格已折进 prompt，不另设字段）。 */
export interface PersonaTemplate {
  templateId: string;
  mbti: string;
  name: string;
  group: string;
  dimensions: string;
  summary: string;
  duty: string;
  style: string[];
  collaboration: string;
  prompt: string;
}

/** 与 Rust `expert/mod.rs` 同源的同一组上限（数字只在 persona_runtime 一处维护）。 */
export interface PersonaLimits {
  id: number;
  /** Rust 用的是独立常量 `MAX_SKILL_ID_LEN`，与 `MAX_ID_LEN` 当前同值但不共享。 */
  skillId: number;
  name: number;
  description: number;
  prompt: number;
  skills: number;
  experts: number;
}

/** GET /api/persona/templates；分组传错时 ok=false + reason，并把可用分组回出来。 */
export interface PersonaTemplatesResponse {
  ok: boolean;
  group: string;
  groups: string[];
  templates: PersonaTemplate[];
  total: number;
  limits?: PersonaLimits;
  reason?: string;
  note?: string;
}

/** GET /api/persona/template?templateId=（查不到时把全部模板号回出来，别让界面空转） */
export interface PersonaTemplateResponse {
  ok: boolean;
  template: Partial<PersonaTemplate>;
  reason?: string;
  templateIds?: string[];
}

/** 一份可编辑的专家草稿：字段就是 Rust `Expert` 的那几个，没有 `personalityStyle`。 */
export interface PersonaExpertDraft {
  id: string;
  name: string;
  description: string;
  prompt: string;
  sourceTemplate: string;
}

/** POST /api/persona/draft —— `saved` 恒为 false：保存是界面上另一次调用，不占注册表名额。 */
export interface PersonaDraftResponse {
  ok: boolean;
  reason: string;
  note: string;
  /** 归一化提醒（如「标识会按小写保存」）：与 problems 分开，提醒不算拒绝。 */
  notes: string[];
  templateId: string;
  expert: Partial<PersonaExpertDraft>;
  limits: PersonaLimits;
  problems?: string[];
  saved: boolean;
}

/** POST /api/persona/validate：把 Rust 会拒的理由提前摊给界面。 */
export interface PersonaValidateResponse {
  ok: boolean;
  problems: string[];
  notes: string[];
  limits: PersonaLimits;
  reason: string;
  note: string;
}

/** GET /api/persona/status —— `unverified` 是本族自己承认还没接完的那一半。 */
export interface PersonaStatusResponse {
  ok: boolean;
  count: number;
  groups: Record<string, number>;
  templateIds: string[];
  limits: PersonaLimits;
  promptBudgetSource: string;
  unverified: string[];
  note: string;
}


// ============================================================
// D-28 可选遥测（#141）：`/api/exec/telemetry/*` 的响应形状
// ============================================================
// 与写暂存同一套口径：**不走 IPC**，渲染层用 engineFetch 直连本机引擎。
// 这一族的形状里最值得钉住的是 `payload`：它就是 flush 交给出口的那份数组，
// 界面显示的预览和真要发出去的东西必须同源，否则「看得见将发送什么」只是一句装饰。
// 键集由 tests/integration/engine_telemetry_crash_selftest.py 按实际回话双向核对。

/** 同意状态的读数（`consent_state()` 四个分支的键并集；缺省就是 on=false）。 */
export interface TelemetryConsentState {
  on: boolean;
  atMs: number;
  via: string;
  schema: string;
  installationId: string;
  /** absent / corrupt / invalid / file：关闭态也要说清为什么是关闭。 */
  source: string;
  reason: string;
  /** 仅 corrupt 分支：文件读不出来时给异常名与改名后的文件名。 */
  loadError?: string;
  movedTo?: string;
}

/** 出口登记状态：没有出口时所有外发动作都如实回「未发送」。 */
export interface TelemetrySinkState {
  configured: boolean;
  endpoint: string;
  label: string;
  note: string;
}

/** 一条将要发出去的事件：只有信封 + 白名单字段，没有内容本体。 */
export interface TelemetryEventPayload {
  eventId: string;
  kind: string;
  atMs: number;
  installationId: string;
  fields: Record<string, string | number | boolean>;
}

/** GET /api/exec/telemetry/describe —— 口径本身：类别、逐类字段、确认词、上限。 */
export interface TelemetryDescribeResponse {
  ok: boolean;
  /** 缺省关闭这件事写进契约，界面不许把它读成「没读到所以是开」。 */
  defaultOn: boolean;
  kinds: string[];
  allowedFields: Record<string, string[]>;
  fieldVocab: Record<string, string[]>;
  neverCollected: string[];
  confirmWord: string;
  limits: { maxBufferedEvents: number; flushBatch: number; maxValueChars: number; previewLimit: number };
  schema: string;
  paths: { dir: string; consent: string; events: string; decisions: string };
  sink: TelemetrySinkState;
  note: string;
  /** 403（远程身份）与 500 才有这两个键。 */
  code?: string;
  message?: string;
}

/** GET /api/exec/telemetry/status —— 开没开、缓冲几条、计数器、有没有出口。 */
export interface TelemetryStatusResponse {
  ok: boolean;
  enabled: boolean;
  consent: TelemetryConsentState;
  /** 只给安装标识的后 8 位：够人核对，不够别人拼出完整标识。 */
  installationIdTail: string;
  buffered: number;
  badLines: number;
  byKind: Record<string, number>;
  bufferPath: string;
  consentPath: string;
  limits: { maxBufferedEvents: number; flushBatch: number; maxValueChars: number };
  counters: { droppedByCap: number; flushedEvents: number; lastFlushAtMs: number; lastAttemptReason: string };
  sink: TelemetrySinkState;
  confirmWord: string;
  note: string;
  code?: string;
  message?: string;
}

/** GET /api/exec/telemetry/preview —— **将发送内容的逐字预览**。 */
export interface TelemetryPreviewResponse {
  ok: boolean;
  enabled: boolean;
  count: number;
  shown: number;
  badLines: number;
  limit: number;
  payload: TelemetryEventPayload[];
  destination: TelemetrySinkState;
  kinds: string[];
  allowedFields: Record<string, string[]>;
  note: string;
  code?: string;
  message?: string;
}

/** GET /api/exec/telemetry/decisions —— 同意侧的本地审计（只在本机读）。 */
export interface TelemetryDecisionsResponse {
  ok: boolean;
  count: number;
  returned: number;
  decisions: Array<Record<string, string | number>>;
  loadErrors: number;
  note: string;
  code?: string;
  message?: string;
}

/** POST /api/exec/telemetry/consent —— 开要确认词，关不要；关会顺手删缓冲。 */
export interface TelemetryConsentResponse {
  ok: boolean;
  changed: boolean;
  /** 改完之后的状态：被拒时它还是原值，界面按它回显开关，别按请求回显。 */
  on: boolean;
  atMs?: number;
  installationId?: string;
  note?: string;
  reason?: string;
  eventsRemoved?: number;
  bufferDeleteFailed?: boolean;
  code?: string;
  message?: string;
}

/**
 * POST /api/exec/telemetry/flush —— 没有出口时 `delivered=false` 且一条也不删。
 *
 * 这里**故意不声明** `endpoint` 与 `note`：那两个键只在「进程内登记过出口、出口确认收下」
 * 那一支才出现，而今天本仓库没有任何遥测接收端，HTTP 这条路径永远走不到那一支。
 * 声明了就是给界面留一句可以写「已发送到 …」的谎话素材（#141 的自测会按实际回话键集
 * 双向核对，多写的字段会被判红）。真接上出口那天，连同那一支的实测一起补进来。
 */
export interface TelemetryFlushResponse {
  ok: boolean;
  sent: number;
  delivered: boolean;
  remaining: number;
  attempted?: number;
  reason?: string;
  preview?: TelemetryEventPayload[];
  code?: string;
  message?: string;
}

/** POST /api/exec/telemetry/discard —— 只清缓冲，不改同意状态。 */
export interface TelemetryDiscardResponse {
  ok: boolean;
  removed: number;
  reason: string;
  code?: string;
  message?: string;
}

// ============================================================
// D-28 崩溃现场（#141）：`/api/exec/crash/*` 的响应形状
// ============================================================
// 这一族经手的是「本机崩过什么」，所以整族只对桌面壳开放；导出物是**副本**，
// 删原件是另一条显式动作，且不接受 `reportId="*"` 的整批删除。

/** 钩子装配状态：界面应当能读出「循环那一半装没装上」。 */
export interface CrashHookState {
  sysExcepthook: boolean;
  loopHandler: boolean;
  installError: string;
}

/** 进程内观察者的读数（装配现场登记的「谁想知道崩了」）。 */
export interface CrashRecorderState {
  registered: boolean;
  notified: number;
  error: string;
}

/** 现场里的运行环境：只到「哪台机器什么版本」，不含用户名与机器名。 */
export interface CrashRuntimeInfo {
  python: string;
  implementation: string;
  platform: string;
  pid: number;
  frozen: boolean;
}

/** 列表里的一条（不含 traceback 正文；读不出来的那条只有 reason）。 */
export interface CrashReportRow {
  reportId: string;
  atMs: number;
  atText: string;
  category: string;
  excType: string;
  excText: string;
  note: string;
  times: number;
  traceBytes: number;
  traceSha256: string;
  truncated: boolean;
  redactedKinds: string[];
  context: Record<string, string | number | boolean>;
  exportedAtMs: number;
}

/** GET /api/exec/crash/describe —— 留存口径本身。 */
export interface CrashDescribeResponse {
  ok: boolean;
  dir: string;
  maxReports: number;
  maxTracebackBytes: number;
  maxNoteChars: number;
  dedupeWindowMs: number;
  dedupeLookback: number;
  categories: string[];
  contextFields: string[];
  redactionKinds: string[];
  hooks: CrashHookState;
  recorder: CrashRecorderState;
  note: string;
  code?: string;
  message?: string;
}

/** GET /api/exec/crash/status —— 「崩过几份、有几份还没导出」。 */
export interface CrashStatusResponse {
  ok: boolean;
  count: number;
  pendingExport: number;
  newestAtMs: number;
  dir: string;
  note: string;
  hooks: CrashHookState;
  code?: string;
  message?: string;
}

/** GET /api/exec/crash/list */
export interface CrashListResponse {
  ok: boolean;
  count: number;
  returned: number;
  limit: number;
  loadErrors: number;
  reports: CrashReportRow[];
  hasMore: boolean;
  dir: string;
  code?: string;
  message?: string;
}

/** GET /api/exec/crash/detail —— 唯一带 traceback 正文的回话（已脱敏）。 */
export interface CrashDetailResponse extends CrashReportRow {
  ok: boolean;
  runtime: CrashRuntimeInfo;
  categoryDeclared: string;
  traceback?: string;
  /** 找不到（刚被删掉）时的中文说明：流程内的正常结果，不是服务故障。 */
  reason?: string;
  code?: string;
  message?: string;
}

/** POST /api/exec/crash/export —— 副本 + 指纹，原件不删。 */
export interface CrashExportResponse {
  ok: boolean;
  path?: string;
  bytes?: number;
  sha256?: string;
  reports?: string[];
  count?: number;
  note?: string;
  unreadable?: string[];
  reason?: string;
  code?: string;
  message?: string;
}

/** POST /api/exec/crash/delete —— 指名道姓；`*` 一律拒。 */
export interface CrashDeleteResponse {
  ok: boolean;
  reportId?: string;
  removed?: boolean;
  reason?: string;
  code?: string;
  message?: string;
}

// ============================================================
// D-12 应用更新回路（#159）：exec:update_* 十五个方法的回话形状
// ============================================================
// 实现面：`src-tauri/src/app_update/mod.rs`（流程）+ `app_update/manifest.rs`（清单格式与验签）。
// 这一族走 exec 车道的 `lane_exec`，不动 lib.rs 的命令清单，也不动 contracts.ts。
//
// 三条读代码前先要记住的口径：
// - **`ok:false` 是业务拒绝，不是服务故障**：没配渠道、没给确认词、版本不比本机新，
//   都属正常路径；Rust 侧不会把它们写成车道失败留痕，界面要把 `reason` 原样说给用户。
// - **验签先于下载**：`update_check` 就已经逐条判过签名，`update_download` 只补哈希复核；
//   所以界面上「可更新」与「能装」是两件事——本机锚点为空时前者可能为真、后者一定为假。
// - **应用不自动退出**：`update_install` / `update_rollback` 只启动安装器并落 pending，
//   结算发生在下次启动（`settle_at_startup`），`quitNote` 那句必须原样上界面。

/** 安装包类型：与 `manifest.rs` 的 `KINDS` 同源。 */
export type UpdateArtifactKind = 'nsis' | 'msi';

/** 闭合状态表：与 `mod.rs` 的 `STATES` 同源，不许有第七个值。 */
export type UpdateState =
  | 'empty'
  | 'staged'
  | 'install-pending'
  | 'installed'
  | 'rolled-back'
  | 'install-failed';

/** 结算动作：升级与回滚走同一条替换路径，但回滚只认本机留存的那一份旧包。 */
export type UpdateAction = 'upgrade' | 'rollback';

/** 一把被本机信任的发布公钥；`source=embedded` 的那份删不掉。 */
export interface UpdateAnchorView {
  publisherId: string;
  publicKey: string;
  /** 公钥的 sha256 指纹（64 位十六进制）：换钥后会变，界面拿它当人可读的标识。 */
  fingerprint: string;
  fingerprintShort: string;
  source: 'embedded' | 'local';
  removable: boolean;
}

/** 本机更新渠道配置（数据根下的 `channel.json`）。 */
export interface UpdateChannelView {
  configured: boolean;
  enabled: boolean;
  url: string;
  channel: string;
  updatedAtMs: number;
  /** 读盘时非空的说明（文件坏过、被改名留证等），空串表示这次读得干净。 */
  loadNote: string;
}

/** 已下载并核对过哈希、暂存在本机的安装包。 */
export interface UpdateStagedRow {
  version: string;
  /** 暂存区里的文件名：产品名 + 版本 + 平台 + 架构 + 哈希前 12 位，名字本身带内容绑定。 */
  file: string;
  sha256: string;
  sizeBytes: number;
  kind: string;
  platform: string;
  arch: string;
  publisherId: string;
  /** false = 手工导入的本地包（没有清单也没有签名），界面必须标成「未签名」。 */
  signed: boolean;
  origin: string;
  stagedAtMs: number;
}

/** 回滚材料：上一个版本装完之后还留在暂存区的那一份包。 */
export interface UpdateRetainedRow {
  version: string;
  file: string;
  sha256: string;
  sizeBytes: number;
  kind: string;
  keptAtMs: number;
}

/** 清单里一条产物的形状（不含验签）。 */
export interface UpdateArtifactView {
  platform: string;
  arch: string;
  kind: string;
  url: string;
  sha256: string;
  sizeBytes: number;
  publisherId: string;
  fileExtension: string;
}

/** 验签结果：过了带发布者与指纹，没过带错误码——两种形状不会同时出现。 */
export interface UpdateSignatureView {
  ok: boolean;
  publisherId?: string;
  fingerprintShort?: string;
  source?: string;
  code?: string;
  message: string;
}

/** `update_check` 回的行：清单的一条版本在本机能不能装，以及不能装的全部理由。 */
export interface UpdateCheckRow {
  version: string;
  notes: string;
  newer: boolean;
  revoked: boolean;
  minAppVersion?: string | null;
  minAppVersionOk: boolean;
  artifact: UpdateArtifactView | null;
  /** 形状判定与验签都过了才 true：更旧或已被吊销的版本永远 false，即使签名合法。 */
  installable: boolean;
  reasons: string[];
  /** 只有形状判定放行的条目才补验签结果；其余为 undefined。 */
  signature?: UpdateSignatureView;
}

/** 将要执行的那条安装命令（`update_install_plan` 只给命令，不执行）。 */
export interface UpdateInstallPlanView {
  program: string;
  args: string[];
  elevationNote: string;
  /** 「本机不会自动退出应用」的原文：这是用户唯一需要手工做的一步，必须显示。 */
  quitNote: string;
  action: string;
}

/** `exec:update_describe`：能力自述，界面据此决定放哪些按钮（没有渠道就不放「检查更新」）。 */
export interface UpdateDescribeResponse {
  ok: boolean;
  schema: string;
  signingDomain: string;
  lane: string;
  methodPrefix: string;
  methods: string[];
  appVersion: string;
  engineVersionNote: string;
  host: { platform: string; arch: string };
  kinds: string[];
  platforms: string[];
  arches: string[];
  versionRule: string;
  confirmWord: string;
  importConfirmWord: string;
  limits: {
    manifestBytes: number;
    artifactBytesMax: number;
    artifactBytesMin: number;
    releases: number;
    artifactsPerRelease: number;
    revokedVersions: number;
    stagedFiles: number;
    ledgerLines: number;
    trustedKeys: number;
  };
  states: string[];
  embeddedAnchorCount: number;
  trustedAnchors: UpdateAnchorView[];
  channel: UpdateChannelView;
  paths: {
    dir: string;
    staging: string;
    journal: string;
    ledger: string;
    channel: string;
    trust: string;
  };
  sampleManifest: Record<string, unknown>;
  signatureNote: string;
  rollbackNote: string;
}

/** `exec:update_channel_get` */
export interface UpdateChannelGetResponse {
  ok: boolean;
  channel: UpdateChannelView;
  loadNote: string;
  note: string;
}

/** `exec:update_channel_set`：只写本机文件，不探测可达性，所以回话里没有「已连通」。 */
export interface UpdateChannelSetResponse {
  ok: boolean;
  channel?: UpdateChannelView;
  note?: string;
  reason?: string;
}

/** `exec:update_trust_list` */
export interface UpdateTrustListResponse {
  ok: boolean;
  anchors: UpdateAnchorView[];
  embeddedCount: number;
  loadNote: string;
  note: string;
}

/** `exec:update_trust_add`：同标识换钥匙会被拒（`UPDATE_PUBLISHER_KEY_CONFLICT` 走 Err，不是这里）。 */
export interface UpdateTrustAddResponse {
  ok: boolean;
  added?: boolean;
  anchor?: UpdateAnchorView;
  note?: string;
}

/** `exec:update_trust_remove` */
export interface UpdateTrustRemoveResponse {
  ok: boolean;
  removed?: boolean;
  publisherId?: string;
  remaining?: number;
  note?: string;
  reason?: string;
}

/** `exec:update_check`：拒绝与成功共用一个壳，界面先看 `ok` 再读 `rows`。 */
export interface UpdateCheckResponse {
  ok: boolean;
  reason?: string;
  channel?: UpdateChannelView;
  fetchedAtMs?: number;
  manifestBytes?: number;
  manifestChannel?: string;
  publishedAtMs?: number;
  revokedVersions?: string[];
  currentVersion?: string;
  rows?: UpdateCheckRow[];
  installable?: { version: string; artifact: UpdateArtifactView } | null;
  notices?: unknown[];
  trustedAnchors?: UpdateAnchorView[];
  note?: string;
}

/** `exec:update_download` */
export interface UpdateDownloadResponse {
  ok: boolean;
  reason?: string;
  reused?: boolean;
  staged?: UpdateStagedRow;
  /** 复用的那份只回指纹（`publisherId` 缺席）：两份包的发布者可能同名，但这里没有重新摊开过。 */
  signature?: { ok: boolean; publisherId?: string; fingerprintShort: string };
  pruned?: string[];
  journalState?: string;
  note?: string;
}

/** `exec:update_status`：读时会顺手结算一次（懒结算），`settledNow` 说明这次有没有改动状态。 */
export interface UpdateStatusResponse {
  ok: boolean;
  settledNow: boolean;
  appVersion: string;
  state: string;
  /** false = 账本被外部改成表外的值：界面不许按已知状态点亮任何按钮。 */
  stateKnown: boolean;
  stateNotes: string;
  staged: UpdateStagedRow | null;
  pending: {
    version: string;
    file: string;
    action: string;
    previousVersion: string;
    launchedAtMs: number;
  } | null;
  rollback: UpdateRetainedRow | null;
  rollbackAvailable: boolean;
  lastError: string;
  note: string;
  loadNotes: string[];
  channel: UpdateChannelView;
  trustedAnchors: UpdateAnchorView[];
  stagedFiles: UpdateStagedRow[];
  paths: { dir: string; staging: string; journal: string; ledger: string };
  stagingNote: string;
}

/** `exec:update_install_plan` / `exec:update_rollback_plan`：只摊开命令，不动文件。 */
export interface UpdatePlanResponse {
  ok: boolean;
  reason?: string;
  plan?: UpdateInstallPlanView;
  staged?: UpdateStagedRow | null;
  rollback?: UpdateRetainedRow;
  confirmWord?: string;
  rollbackAvailable?: boolean;
  note?: string;
}

/** `exec:update_install` / `exec:update_rollback`：带确认词才会启动安装器。 */
export interface UpdateLaunchResponse {
  ok: boolean;
  launched?: boolean;
  pid?: number;
  action?: string;
  fromVersion?: string;
  toVersion?: string;
  stagedFile?: string;
  plan?: UpdateInstallPlanView;
  rollback?: UpdateRetainedRow;
  journalState?: string;
  note?: string;
  reason?: string;
  confirmWord?: string;
}

/** `exec:update_import_local`：离线环境的手动包入口（确认词是 `LOCAL-PACKAGE`）。 */
export interface UpdateImportResponse {
  ok: boolean;
  reason?: string;
  confirmWord?: string;
  staged?: UpdateStagedRow;
  pruned?: string[];
  note?: string;
}

/** `exec:update_ledger` */
export interface UpdateLedgerResponse {
  ok: boolean;
  rows: { atMs: number; action: string; ok: boolean; code: string; detail: string }[];
  count: number;
  /** 文件里的真实条数（写入侧就按上限截断，所以它最大等于 `limits.ledgerLines`）。 */
  totalLines: number;
  /** true = 因为 `limit` 小于 `totalLines`，这里只回了一部分。 */
  truncated: boolean;
  limit: number;
  badLines: number;
  file: string;
  note: string;
}
