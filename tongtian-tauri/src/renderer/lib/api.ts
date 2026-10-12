/**
 * 通天办公 - 类型安全的 IPC API 封装层
 *
 * 将 window.tongtianAPI 的通用 invoke/on 接口封装为
 * 具有完整类型签名的业务方法，供渲染进程各组件调用。
 */

import { IPC_CHANNELS } from '../../shared/contracts';
import type {
  ChatSubmitReq,
  ChatReviewReq,
  ChatControlReq,
  ChatEvent,
  ModelConfig,
  ModelTestReq,
  ModelTestResp,
  ChatStepPreview,
  AttachmentMeta,
} from '../../shared/types';
import type {
  AuthLoginResp,
  AuthStatus,
  CompliancePatch,
  ComplianceCheckResp,
  ComplianceStatus,
  ComplianceUpdateResp,
  FilePreview,
  InstalledSkill,
  InstalledSuite,
  MCPTool,
  McpAddReq,
  McpServer,
  ModelUsageView,
  SandboxEnvClearResult,
  SandboxEnvUsage,
  SkillInstallReq,
  SkillRootInfo,
  SuiteInstallReq,
  SuiteRootInfo,
  UsagePeriod,
  UsageReport,
  UsageStats,
  WorkspaceInfo,
} from '../../shared/contracts';

// ── 辅助函数 ─────────────────────────────────────────────────

function getAPI() {
  const api = window.tongtianAPI;
  if (!api) {
    throw new Error('tongtianAPI is not available. 请从桌面版通天办公或已注入通道的页面打开');
  }
  return api;
}

// ── Chat API ─────────────────────────────────────────────────

/** 引擎服务地址（VITE_ENGINE_URL 可覆盖，默认引擎缺省端口） */
const ENGINE_URL: string = import.meta.env.VITE_ENGINE_URL ?? 'http://127.0.0.1:18000';

export const chatAPI = {
  /** 提交对话目标，启动 Agent 流程 */
  submit(req: ChatSubmitReq): Promise<{ taskId: string }> {
    return getAPI().chatSubmit(req) as Promise<{ taskId: string }>;
  },

  /** 审阅 Plan（两阶段协议：approve → 执行流 / reject → done(REJECTED) / modify → 重新计划） */
  review(taskId: string, req: ChatReviewReq): Promise<{ ok: boolean; status: string }> {
    return getAPI().chatReview(taskId, req.action, req.patch as unknown[]);
  },

  /** 控制任务（暂停 / 继续 / 回滚） */
  control(req: ChatControlReq): Promise<{ ok: boolean; status: string }> {
    return getAPI().chatControl(req);
  },

  /** 取消任务（转移 #3/#8/#19/#23/#28） */
  cancel(taskId: string, workspaceId?: string): Promise<{ taskId: string; cancelled: boolean }> {
    return getAPI().chatCancel(taskId, workspaceId);
  },

  /** 获取历史对话 */
  history(taskId?: string): Promise<unknown[]> {
    void taskId;
    return getAPI().chatHistory();
  },

  /**
   * 订阅流式 Chat 事件。
   * 返回取消订阅函数。
   */
  onEvent(callback: (event: ChatEvent) => void): () => void {
    return getAPI().onChatEvent((raw) => {
      callback(raw as ChatEvent);
    });
  },

  /**
   * 附件内容真实上传（对话接口）。
   * 优先走 IPC shim；否则直连引擎 /api/files/upload；
   * 引擎不可达且处于开发阶段 Mock 模式时降级为本地模拟 meta（Mock 开关规范）。
   */
  async fileUpload(req: { fileName: string; dataBase64: string; mime?: string }): Promise<AttachmentMeta> {
    const apiObj = (window as unknown as {
      tongtianAPI?: { chatFileUpload?: (r: { fileName: string; dataBase64: string; mime?: string }) => Promise<AttachmentMeta> };
    }).tongtianAPI;
    if (apiObj?.chatFileUpload) return apiObj.chatFileUpload(req);

    const sizeBytes = Math.floor(req.dataBase64.length * 0.75);
    try {
      const resp = await fetch(`${ENGINE_URL}/api/files/upload`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req),
      });
      if (!resp.ok) throw new Error(`上传失败（HTTP ${resp.status}）`);
      const data = (await resp.json()) as AttachmentMeta & { ok?: boolean; message?: string };
      if (data.ok === false) throw new Error(data.message ?? '上传失败');
      return data;
    } catch (e) {
      // 开发阶段 Mock 降级：引擎未启动时不阻断对话流程
      if (import.meta.env.VITE_MOCK !== 'false') {
        return {
          fileName: req.fileName,
          stagedPath: `/mock/uploads/attachments/${req.fileName}`,
          sizeBytes,
          mime: req.mime ?? 'application/octet-stream',
          sensitivity: 'public',
        };
      }
      throw e;
    }
  },
};

// ── Model API ────────────────────────────────────────────────

export const modelAPI = {
  /** 获取全部模型配置 */
  list(): Promise<ModelConfig[]> {
    return getAPI().invoke(IPC_CHANNELS.MODEL_LIST) as Promise<ModelConfig[]>;
  },

  /** 创建模型配置 */
  create(config: Omit<ModelConfig, 'id' | 'createdAt' | 'updatedAt'>): Promise<ModelConfig> {
    return getAPI().invoke(IPC_CHANNELS.MODEL_CREATE, config) as Promise<ModelConfig>;
  },

  /** 更新模型配置 */
  update(id: string, patch: Partial<ModelConfig>): Promise<ModelConfig> {
    return getAPI().invoke(IPC_CHANNELS.MODEL_UPDATE, { id, ...patch }) as Promise<ModelConfig>;
  },

  /** 删除模型配置 */
  remove(id: string): Promise<void> {
    return getAPI().invoke(IPC_CHANNELS.MODEL_DELETE, { id }) as Promise<void>;
  },

  /** 测试模型连通性 */
  test(req: ModelTestReq): Promise<ModelTestResp> {
    return getAPI().invoke(IPC_CHANNELS.MODEL_TEST, req) as Promise<ModelTestResp>;
  },

  /** 设置默认模型 */
  setDefault(id: string): Promise<void> {
    return getAPI().invoke(IPC_CHANNELS.MODEL_SET_DEFAULT, { id }) as Promise<void>;
  },

  /** 获取模型用量统计（真实台账；period: daily/monthly/total） */
  usage(period: UsagePeriod = 'monthly'): Promise<ModelUsageView> {
    return getAPI().invoke(IPC_CHANNELS.MODEL_USAGE, period) as Promise<ModelUsageView>;
  },
};

// ── File API（核心闭环补齐：白名单内文件能力） ─────────────

export interface FileItem {
  name: string;
  path: string;
  isDirectory: boolean;
  size: number;
  modifiedAt: string;
}

export const fileAPI = {
  /** 列出目录（空串缺省为工作区文件根；越界返回 FILE_ACCESS_DENIED） */
  list(dirPath: string, workspaceId?: string): Promise<FileItem[]> {
    return getAPI().invoke(IPC_CHANNELS.FILE_BROWSE, dirPath, workspaceId) as Promise<FileItem[]>;
  },

  /** 读取文本文件（2MB 上限） */
  read(filePath: string, workspaceId?: string): Promise<{ path: string; content: string }> {
    return getAPI().invoke('file:read', filePath, workspaceId) as Promise<{ path: string; content: string }>;
  },

  /** 批量预览（txt/md/csv/json 直读；docx/xlsx/pptx 抽正文；pdf 给页数与文档信息） */
  preview(filePaths: string[], workspaceId?: string): Promise<FilePreview[]> {
    return getAPI().invoke('file:preview', filePaths, workspaceId) as Promise<FilePreview[]>;
  },
};

// ── Workspace API（工作空间：桌面端以磁盘目录为权威，浏览器通道为本地台账） ──

export const workspaceAPI = {
  /** 列出工作空间（default 恒在；含 Agent 运行态） */
  list(): Promise<WorkspaceInfo[]> {
    return getAPI().invoke(IPC_CHANNELS.WORKSPACE_LIST) as Promise<WorkspaceInfo[]>;
  },

  /** 创建工作空间（幂等；标识非法返回 WORKSPACE_INVALID_ID） */
  create(workspaceId: string): Promise<WorkspaceInfo> {
    return getAPI().invoke(IPC_CHANNELS.WORKSPACE_CREATE, { workspaceId }) as Promise<WorkspaceInfo>;
  },
};

// ── Skill API（技能：桌面端以 {data_home}/skills/*/SKILL.md 为事实来源） ──

export const skillAPI = {
  /** 技能运行目录（桌面端返回真实路径；浏览器通道 supported=false） */
  root(): Promise<SkillRootInfo> {
    return getAPI().invoke(IPC_CHANNELS.SKILL_ROOT) as Promise<SkillRootInfo>;
  },

  /** 列出已安装技能（含禁用状态；浏览器通道返回空列表） */
  list(): Promise<InstalledSkill[]> {
    return getAPI().invoke(IPC_CHANNELS.SKILL_LIST) as Promise<InstalledSkill[]>;
  },

  /** 从本地目录/文件安装技能（市场与 URL 来源当前不支持，如实返回 SKILL_UNSUPPORTED） */
  install(req: SkillInstallReq): Promise<InstalledSkill> {
    return getAPI().invoke(IPC_CHANNELS.SKILL_INSTALL, req) as Promise<InstalledSkill>;
  },

  /** 卸载技能（目录移入 skills_removed 回收目录，不物理删除） */
  uninstall(skillId: string): Promise<{ removed: string; trashPath: string }> {
    return getAPI().invoke(IPC_CHANNELS.SKILL_UNINSTALL, { skillId }) as Promise<{
      removed: string;
      trashPath: string;
    }>;
  },

  /** 启用/禁用技能（禁用清单落 settings_kv，技能文件保持不动） */
  toggle(skillId: string, enabled: boolean): Promise<{ ok: boolean; id: string; enabled: boolean }> {
    return getAPI().invoke(IPC_CHANNELS.SKILL_TOGGLE, { skillId, enabled }) as Promise<{
      ok: boolean;
      id: string;
      enabled: boolean;
    }>;
  },
};

// ── 专家套件 API（桌面端落 suites/；浏览器通道走本地台账） ──

export const suiteAPI = {
  /** 套件运行目录（桌面端返回真实路径；浏览器通道 supported=false） */
  root(): Promise<SuiteRootInfo> {
    return getAPI().invoke(IPC_CHANNELS.SUITE_ROOT) as Promise<SuiteRootInfo>;
  },

  /** 列出已安装套件（以 manifest.json / 本地台账为事实来源） */
  list(): Promise<InstalledSuite[]> {
    return getAPI().invoke(IPC_CHANNELS.SUITE_LIST) as Promise<InstalledSuite[]>;
  },

  /** 安装套件包（.zip 字节 base64；plugin.json 校验不过即拒绝，不产生半成品） */
  install(req: SuiteInstallReq): Promise<InstalledSuite> {
    return getAPI().invoke(IPC_CHANNELS.SUITE_INSTALL, req) as Promise<InstalledSuite>;
  },

  /** 卸载套件（桌面端移入 suites_removed 回收目录，不物理删除） */
  uninstall(suiteId: string): Promise<{ removed: string; trashPath: string }> {
    return getAPI().invoke(IPC_CHANNELS.SUITE_UNINSTALL, { suiteId }) as Promise<{
      removed: string;
      trashPath: string;
    }>;
  },
};

// ── MCP 连接器 API（配置落 mcp_servers；连接由 Rust 发起真实握手） ──

export const mcpAPI = {
  /** 列出已登记的连接器（无登记时为空数组；env/header 只暴露键名） */
  list(): Promise<McpServer[]> {
    return getAPI().invoke(IPC_CHANNELS.MCP_LIST_SERVERS) as Promise<McpServer[]>;
  },

  /** 登记连接器（重名拒绝 MCP_SERVER_EXISTS；状态从「未连接」开始） */
  add(req: McpAddReq): Promise<McpServer> {
    return getAPI().invoke(IPC_CHANNELS.MCP_ADD_SERVER, req) as Promise<McpServer>;
  },

  /** 移除连接器登记（只删这一条登记，不动任务与审计存量数据） */
  remove(serverId: string): Promise<{ ok: boolean; serverId: string; name: string }> {
    return getAPI().invoke(IPC_CHANNELS.MCP_REMOVE_SERVER, { serverId }) as Promise<{
      ok: boolean;
      serverId: string;
      name: string;
    }>;
  },

  /** 连接：initialize → initialized → tools/list 的真实往返，失败带原因 */
  connect(serverId: string): Promise<McpServer> {
    return getAPI().invoke(IPC_CHANNELS.MCP_CONNECT, { serverId }) as Promise<McpServer>;
  },

  /** 断开：清除已验证状态与工具缓存，配置保留可随时再连 */
  disconnect(serverId: string): Promise<McpServer> {
    return getAPI().invoke(IPC_CHANNELS.MCP_DISCONNECT, { serverId }) as Promise<McpServer>;
  },

  /** 已缓存的工具清单（未连接时为空数组，不伪造工具） */
  tools(serverId: string): Promise<MCPTool[]> {
    return getAPI().invoke(IPC_CHANNELS.MCP_LIST_TOOLS, { serverId }) as Promise<MCPTool[]>;
  },
};

// ── Compliance API（合规聚合视图：与出域控制/模型台账/审计同一份权威） ──

export const complianceAPI = {
  /** 合规状态：硬开关 + 出域控制摘要 + 按策略评估的模型名单 + 最近数据分级留痕 */
  status(): Promise<ComplianceStatus> {
    return getAPI().invoke(IPC_CHANNELS.COMPLIANCE_STATUS) as Promise<ComplianceStatus>;
  },

  /** 更新合规策略（只改传入字段）；空 patch 只读不写，changed=false */
  update(patch: CompliancePatch): Promise<ComplianceUpdateResp> {
    return getAPI().invoke(IPC_CHANNELS.COMPLIANCE_UPDATE, patch) as Promise<ComplianceUpdateResp>;
  },

  /**
   * 提交前合规闸门：正文分级 + 目标端点出域校验。
   * 判定与真实出站调用同源，被拒时以 `CODE: 原因` 抛错，调用方据此阻断提交。
   */
  check(goal: string, baseUrl?: string): Promise<ComplianceCheckResp> {
    return getAPI().invoke(IPC_CHANNELS.COMPLIANCE_CHECK, goal, baseUrl) as Promise<ComplianceCheckResp>;
  },
};

// ── Sandbox API（安全工作环境：工作环境文件的磁盘事实与清理） ──

export const sandboxAPI = {
  /** 工作环境目录磁盘实测用量（桌面端汇总所有工作空间，引擎直连为引擎自身目录） */
  envUsage(): Promise<SandboxEnvUsage> {
    return getAPI().invoke(IPC_CHANNELS.SANDBOX_ENV_USAGE) as Promise<SandboxEnvUsage>;
  },

  /** 清理工作环境文件，仅删除 workspaces/{id}/environment 内的条目 */
  envClear(): Promise<SandboxEnvClearResult> {
    return getAPI().invoke(IPC_CHANNELS.SANDBOX_ENV_CLEAR) as Promise<SandboxEnvClearResult>;
  },
};

// ── Usage API（用量总览与报表：与 model:usage 同一台账的两种读法） ──

export const usageAPI = {
  /** 周期汇总（顶部总览条；period: daily/monthly/total） */
  stats(period: UsagePeriod = 'monthly'): Promise<UsageStats> {
    return getAPI().invoke(IPC_CHANNELS.USAGE_STATS, period) as Promise<UsageStats>;
  },

  /** 周期报表（按日趋势 + 每模型明细 + 汇总，可复制成文本） */
  report(period: UsagePeriod = 'monthly'): Promise<UsageReport> {
    return getAPI().invoke(IPC_CHANNELS.USAGE_REPORT, period) as Promise<UsageReport>;
  },
};

// ── Auth API（账号与登录态 · 网页验证登录 Loopback 方案） ──
export const authAPI = {
  /** 登录态视图（loggedIn/profile/mock） */
  status(): Promise<AuthStatus> {
    return getAPI().invoke(IPC_CHANNELS.AUTH_STATUS) as Promise<AuthStatus>;
  },

  /** 登录：Mock 直登；真实模式拉起浏览器网页登录 + loopback 回调 */
  login(): Promise<AuthLoginResp> {
    return getAPI().invoke(IPC_CHANNELS.AUTH_LOGIN) as Promise<AuthLoginResp>;
  },

  /** 退出：尽力远端吊销 + 清理 keyring/profile（本地任务/会话保留） */
  logout(): Promise<{ ok: boolean; wasLoggedIn: boolean }> {
    return getAPI().invoke(IPC_CHANNELS.AUTH_LOGOUT) as Promise<{ ok: boolean; wasLoggedIn: boolean }>;
  },
};

// ── System API ───────────────────────────────────────────────

export const systemAPI = {
  /** 获取系统信息 */
  info(): Promise<Record<string, unknown>> {
    return getAPI().invoke(IPC_CHANNELS.SYSTEM_INFO) as Promise<Record<string, unknown>>;
  },

  /** 获取系统健康状态 */
  health(): Promise<{ ok: boolean; details: Record<string, unknown> }> {
    return getAPI().invoke(IPC_CHANNELS.SYSTEM_HEALTH) as Promise<{
      ok: boolean;
      details: Record<string, unknown>;
    }>;
  },
};

// ── 统一导出 ─────────────────────────────────────────────────

// ── P0 API（三层架构演进：取消/任务状态/配额/租约/重放） ───────
// 附加式接入：方法不存在时降级为结构化 NOT_IMPLEMENTED，UI 按禁用态处理

function callIfAvailable<T>(method: string, fallback: T, ...args: unknown[]): Promise<T> {
  const apiObj = getAPI() as unknown as Record<string, unknown>;
  const fn = apiObj[method];
  if (typeof fn === 'function') {
    return (fn as (...a: unknown[]) => Promise<T>)(...args);
  }
  if (import.meta.env.DEV) {
    console.warn(`[api] method not implemented: ${method}`);
  }
  return Promise.resolve(fallback);
}

export const p0API = {
  /** 取消任务（转移 #3/#8/#19/#23/#28 → cancelled） */
  cancel(taskId: string, workspaceId?: string): Promise<{ taskId: string; cancelled: boolean }> {
    return callIfAvailable('chatCancel', { taskId, cancelled: false }, taskId, workspaceId);
  },

  /** 任务 14 态状态（含 wait_reason/重试计数/成因码） */
  taskState(taskId: string): Promise<Record<string, unknown> | null> {
    return callIfAvailable('taskState', null, taskId);
  },

  /** 配额快照（四档预警 70/85/95/100%） */
  quotaStatus(): Promise<Record<string, unknown> | null> {
    return callIfAvailable('quotaStatus', null);
  },

  /** 租约视图 */
  leaseInfo(): Promise<{ holder: string; leases: unknown[] } | null> {
    return callIfAvailable('leaseInfo', null);
  },

  /** 事件重放（24h 持久化，seq > fromSeq） */
  eventReplay(taskId: string, fromSeq = 0): Promise<{ taskId: string; events: unknown[] }> {
    return callIfAvailable('eventReplay', { taskId, events: [] }, taskId, fromSeq);
  },
};

export const api = {
  chat: chatAPI,
  model: modelAPI,
  file: fileAPI,
  workspace: workspaceAPI,
  skill: skillAPI,
  mcp: mcpAPI,
  compliance: complianceAPI,
  sandbox: sandboxAPI,
  usage: usageAPI,
  auth: authAPI,
  system: systemAPI,
  p0: p0API,
};

export default api;
