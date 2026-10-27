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
  CheckpointInfo,
  ModelConfig,
  ModelTestReq,
  ModelTestResp,
  LocalDetectResp,
  ChatStepPreview,
  AttachmentMeta,
} from '../../shared/types';
import { isTauri } from './tauri-api';
import { resolveEngineBaseUrl } from './engine-endpoint';
import { engineFetch } from './engine-http';
import type {
  AuthLoginResp,
  AuthServerProbe,
  AuthStatus,
  CollabCommentResp,
  CollabInvite,
  CollabInviteCreatedResp,
  CollabProjectListResp,
  CollabRole,
  CollabSnapshot,
  CollabStatus,
  CollabTaskPatch,
  CollabTaskResp,
  CompliancePatch,
  ComplianceCheckResp,
  ComplianceStatus,
  ComplianceUpdateResp,
  ExpertInfo,
  ExpertSaveReq,
  FilePreview,
  InstalledSkill,
  InstalledSuite,
  KnowledgeAddResult,
  KnowledgeBase,
  KnowledgeSearchResult,
  MarketList,
  MarketSkillItem,
  MarketSourceConfig,
  MarketSourcePatch,
  MarketSuiteItem,
  MarketTrustAddReq,
  MarketTrustList,
  MarketTrustRemoveReq,
  MCPTool,
  McpAddReq,
  McpServer,
  ModelUsageView,
  SandboxEnvClearResult,
  SandboxEnvUsage,
  SkillInstallReq,
  SkillRootInfo,
  PluginTemplate,
  PluginValidateReq,
  PluginValidation,
  SuiteInstallReq,
  SuiteRootInfo,
  UsagePeriod,
  UsageReport,
  UsageStats,
  WorkspaceCapabilities,
  WorkspaceInfo,
} from '../../shared/contracts';
import type { PptxTemplatesState } from '../../shared/pptx-templates';

// ── 辅助函数 ─────────────────────────────────────────────────

function getAPI() {
  const api = window.tongtianAPI;
  if (!api) {
    throw new Error('tongtianAPI is not available. 请从桌面版任我行或已注入通道的页面打开');
  }
  return api;
}

// ── Chat API ─────────────────────────────────────────────────

export const chatAPI = {
  /** 提交对话目标，启动 Agent 流程 */
  submit(req: ChatSubmitReq): Promise<{ taskId: string }> {
    return getAPI().chatSubmit(req) as Promise<{ taskId: string }>;
  },

  /** 审阅 Plan（approve → 执行流 / reject → done(REJECTED) / modify → 重新计划 / resume → 从检查点续跑） */
  review(taskId: string, req: ChatReviewReq): Promise<{ ok: boolean; status: string }> {
    return getAPI().chatReview(taskId, req.action, req.patch as unknown[]);
  },

  /** 控制任务（暂停 / 继续 / 回滚） */
  control(req: ChatControlReq): Promise<{ ok: boolean; status: string }> {
    return getAPI().chatControl(req);
  },

  /** 取消任务（转移 #3/#8/#19/#23/#28） */
  cancel(
    taskId: string,
    workspaceId?: string,
  ): Promise<{ taskId: string; cancelled: boolean; status?: string }> {
    return getAPI().chatCancel(taskId, workspaceId);
  },

  /** 获取历史对话 */
  history(taskId?: string): Promise<unknown[]> {
    void taskId;
    return getAPI().chatHistory();
  },

  /** 批量查询检查点摘要（刷新/重启后恢复续跑入口） */
  checkpoints(
    taskIds: string[],
    workspaceId?: string,
  ): Promise<{ checkpoints: Record<string, CheckpointInfo> }> {
    return getAPI().chatCheckpointQuery(taskIds, workspaceId) as Promise<{
      checkpoints: Record<string, CheckpointInfo>;
    }>;
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
      // 端口按当前工作空间解析（桌面端多实例时可能是 18001）
      const baseUrl = await resolveEngineBaseUrl();
      const resp = await engineFetch(`${baseUrl}/api/files/upload`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(req),
      });
      if (!resp.ok) throw new Error(`上传失败（HTTP ${resp.status}）`);
      const data = (await resp.json()) as AttachmentMeta & { ok?: boolean; message?: string };
      if (data.ok === false) throw new Error(data.message ?? '上传失败');
      return data;
    } catch (e) {
      // 仅浏览器 Mock 通道降级为本地模拟 meta；桌面端必须如实报错（引擎没跑时上传没落盘）
      if (!isTauri() && import.meta.env.VITE_MOCK !== 'false') {
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

/**
 * 单价/转移链在后端是嵌套对象（prices + fallbackIds），前端清单视图是扁平字段；
 * 在这里统一转换。显式传 null 才能清空已存单价（缺省 = 不动）。
 */
function modelWritePayload(config: Partial<ModelConfig>): Record<string, unknown> {
  const { priceInputPerM, priceOutputPerM, priceCurrency, fallbackIds, ...rest } = config;
  const payload: Record<string, unknown> = { ...rest };
  if (
    priceInputPerM !== undefined ||
    priceOutputPerM !== undefined ||
    priceCurrency !== undefined
  ) {
    payload.prices = {
      inputPerM: priceInputPerM ?? null,
      outputPerM: priceOutputPerM ?? null,
      currency: (priceCurrency || 'CNY').toUpperCase(),
    };
  }
  if (fallbackIds !== undefined) payload.fallbackIds = fallbackIds;
  return payload;
}

export const modelAPI = {
  /** 获取全部模型配置 */
  list(): Promise<ModelConfig[]> {
    return getAPI().invoke(IPC_CHANNELS.MODEL_LIST) as Promise<ModelConfig[]>;
  },

  /** 创建模型配置 */
  create(config: Omit<ModelConfig, 'id' | 'createdAt' | 'updatedAt'>): Promise<ModelConfig> {
    return getAPI().invoke(
      IPC_CHANNELS.MODEL_CREATE,
      modelWritePayload(config),
    ) as Promise<ModelConfig>;
  },

  /** 更新模型配置 */
  update(id: string, patch: Partial<ModelConfig>): Promise<ModelConfig> {
    return getAPI().invoke(IPC_CHANNELS.MODEL_UPDATE, {
      id,
      ...modelWritePayload(patch),
    }) as Promise<ModelConfig>;
  },

  /** 删除模型配置 */
  remove(id: string): Promise<void> {
    return getAPI().invoke(IPC_CHANNELS.MODEL_DELETE, { id }) as Promise<void>;
  },

  /** 测试模型连通性 */
  test(req: ModelTestReq): Promise<ModelTestResp> {
    return getAPI().invoke(IPC_CHANNELS.MODEL_TEST, req) as Promise<ModelTestResp>;
  },

  /**
   * 零配置本地模型：真实探测本机 Ollama。
   * 桌面端由 Rust 请求 /api/tags，浏览器直连由引擎代探；Mock 通道如实返回 supported=false。
   */
  detectLocal(baseUrl?: string): Promise<LocalDetectResp> {
    return getAPI().invoke(IPC_CHANNELS.MODEL_DETECT_LOCAL, baseUrl ?? '') as Promise<LocalDetectResp>;
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

  /**
   * 用系统默认应用打开本地产物（桌面端扩展名白名单：文档/图片/HTML/文本）。
   * 浏览器直连与 Mock 通道没有系统默认应用，会如实拒绝，不伪造成功。
   */
  openExternal(filePath: string): Promise<void> {
    return getAPI().invoke('file:openExternal', filePath) as Promise<void>;
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

  /** 后端抽象与数据根事实（容器/SSH 明确未提供） */
  capabilities(): Promise<WorkspaceCapabilities> {
    return getAPI().invoke(IPC_CHANNELS.WORKSPACE_CAPABILITIES) as Promise<WorkspaceCapabilities>;
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

// ── 插件 SDK API（安装前预检；浏览器通道如实拒绝） ──────────────

export const pluginAPI = {
  /** 只读预检 .zip：返回清单摘要、指令正文来源、技能文件与阻断项 */
  validate(req: PluginValidateReq): Promise<PluginValidation> {
    return getAPI().invoke(IPC_CHANNELS.PLUGIN_VALIDATE, req) as Promise<PluginValidation>;
  },

  /** 起始模板（plugin.json / commands / SKILL.md / README） */
  template(name?: string): Promise<PluginTemplate> {
    return getAPI().invoke(IPC_CHANNELS.PLUGIN_TEMPLATE, { name }) as Promise<PluginTemplate>;
  },
};

// ── 本地知识库 API（文档落磁盘，索引由引擎生成） ──

export const knowledgeAPI = {
  /** 列出未删除的知识库（以 knowledge/ 下真实目录为事实来源） */
  list(): Promise<KnowledgeBase[]> {
    return getAPI().invoke(IPC_CHANNELS.KNOWLEDGE_LIST) as Promise<KnowledgeBase[]>;
  },

  /** 新建知识库（重名拒绝，不覆盖已有库） */
  create(name: string, description = ''): Promise<KnowledgeBase> {
    return getAPI().invoke(IPC_CHANNELS.KNOWLEDGE_CREATE, { name, description }) as Promise<KnowledgeBase>;
  },

  /** 改名/改描述/启停（只动元信息，不动文档） */
  update(
    kbId: string,
    patch: { name?: string; description?: string; enabled?: boolean },
  ): Promise<KnowledgeBase> {
    return getAPI().invoke(IPC_CHANNELS.KNOWLEDGE_UPDATE, { kbId, ...patch }) as Promise<KnowledgeBase>;
  },

  /** 软删除：写 deletedAt，目录与文档保留可找回 */
  remove(kbId: string): Promise<{ ok: boolean; id: string; name: string; deletedAt: string }> {
    return getAPI().invoke(IPC_CHANNELS.KNOWLEDGE_DELETE, { kbId }) as Promise<{
      ok: boolean;
      id: string;
      name: string;
      deletedAt: string;
    }>;
  },

  /** 添加文档（复制原文件 + 抽取正文；逐个文件返回成功/失败原因） */
  addDocs(kbId: string, paths: string[]): Promise<KnowledgeAddResult> {
    return getAPI().invoke(IPC_CHANNELS.KNOWLEDGE_ADD_DOCS, { kbId, paths }) as Promise<KnowledgeAddResult>;
  },

  /** 移除文档（移入库内 .trash/，不物理删除） */
  removeDoc(kbId: string, doc: string): Promise<KnowledgeBase> {
    return getAPI().invoke(IPC_CHANNELS.KNOWLEDGE_REMOVE_DOC, { kbId, doc }) as Promise<KnowledgeBase>;
  },

  openDir(kbId?: string): Promise<void> {
    return getAPI().invoke(IPC_CHANNELS.KNOWLEDGE_OPEN_DIR, { kbId: kbId ?? null }) as Promise<void>;
  },

  /**
   * 检索自测：直连引擎 /api/knowledge/search，与 Agent 执行面共用同一份索引。
   * 引擎未运行会如实抛 ENGINE_NOT_RUNNING，不伪造空结果。
   */
  async search(base: KnowledgeBase, query: string, topK = 5): Promise<KnowledgeSearchResult> {
    const baseUrl = await resolveEngineBaseUrl();
    const resp = await engineFetch(`${baseUrl.replace(/\/+$/, '')}/api/knowledge/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dir: base.dir, name: base.name, query, topK }),
    });
    const body = (await resp.json().catch(() => null)) as
      | (KnowledgeSearchResult & { code?: string; message?: string })
      | null;
    if (!resp.ok || !body || body.ok === false) {
      const code = body?.code || 'KNOWLEDGE_SEARCH_FAILED';
      throw new Error(`${code}: ${body?.message || `知识库检索失败（HTTP ${resp.status}）`}`);
    }
    return body;
  },
};

// ── 幻灯片模板库 API（内置主题 + 本机 .pptx 导入） ──

export const pptxAPI = {
  /** 列出内置与导入模板；selectedId 来自 settings_kv（缺省商务蓝） */
  list(): Promise<PptxTemplatesState> {
    return getAPI().invoke(IPC_CHANNELS.PPTX_TEMPLATES_LIST) as Promise<PptxTemplatesState>;
  },

  /** 导入单个 .pptx（复制进模板目录；同名/超大/结构不合法都会被拒绝） */
  importFile(path: string): Promise<PptxTemplatesState> {
    return getAPI().invoke(IPC_CHANNELS.PPTX_TEMPLATE_IMPORT, { path }) as Promise<PptxTemplatesState>;
  },

  /** 删除导入模板（移入模板目录 .trash/，不物理删除；内置模板不可删） */
  remove(templateId: string): Promise<PptxTemplatesState> {
    return getAPI().invoke(IPC_CHANNELS.PPTX_TEMPLATE_DELETE, { templateId }) as Promise<PptxTemplatesState>;
  },
};

// ── 专家团队 API（注册表落 settings_kv；模型密钥不进渲染层） ──

export const expertsAPI = {
  /** 列出专家（含绑定模型名；模型不可用时带 modelError，不隐藏条目） */
  list(): Promise<ExpertInfo[]> {
    return getAPI().invoke(IPC_CHANNELS.EXPERTS_LIST) as Promise<ExpertInfo[]>;
  },

  /** 保存专家（id 缺省新建；更新不存在的 id 直接报错） */
  save(req: ExpertSaveReq): Promise<ExpertInfo> {
    return getAPI().invoke(IPC_CHANNELS.EXPERTS_SAVE, req) as Promise<ExpertInfo>;
  },

  /** 删除专家配置（不动任何历史任务/会话数据） */
  remove(expertId: string): Promise<{ ok: boolean; id: string; name: string }> {
    return getAPI().invoke(IPC_CHANNELS.EXPERTS_DELETE, { expertId }) as Promise<{
      ok: boolean;
      id: string;
      name: string;
    }>;
  },
};

// ── 在线市场 API（索引地址由用户配置；Rust 负责 HTTPS/公网校验、SHA-256 与落盘） ──

export const marketAPI = {
  /** 读取市场索引源配置（浏览器通道 supported=false，如实告知无下载落盘能力） */
  config(): Promise<MarketSourceConfig> {
    return getAPI().invoke(IPC_CHANNELS.MARKET_CONFIG_GET) as Promise<MarketSourceConfig>;
  },

  /** 保存索引地址（传 null 清除；remote 必须 HTTPS，本机回环可用 HTTP） */
  setConfig(patch: MarketSourcePatch): Promise<MarketSourceConfig> {
    return getAPI().invoke(IPC_CHANNELS.MARKET_CONFIG_SET, patch) as Promise<MarketSourceConfig>;
  },

  /** 拉取技能市场索引（未配置返回 configured=false，不伪造条目） */
  listSkills(): Promise<MarketList<MarketSkillItem>> {
    return getAPI().invoke(IPC_CHANNELS.MARKET_SKILL_LIST) as Promise<MarketList<MarketSkillItem>>;
  },

  /** 安装技能：只传 itemId，下载地址由 Rust 重新拉索引取得 */
  installSkill(itemId: string): Promise<InstalledSkill> {
    return getAPI().invoke(IPC_CHANNELS.MARKET_SKILL_INSTALL, { itemId }) as Promise<InstalledSkill>;
  },

  /** 拉取套件市场索引（未配置返回 configured=false） */
  listSuites(): Promise<MarketList<MarketSuiteItem>> {
    return getAPI().invoke(IPC_CHANNELS.MARKET_SUITE_LIST) as Promise<MarketList<MarketSuiteItem>>;
  },

  /** 安装套件：下载、SHA-256 校验后走本地套件安装（zip 路径安全与回收目录语义不变） */
  installSuite(itemId: string): Promise<InstalledSuite> {
    return getAPI().invoke(IPC_CHANNELS.MARKET_SUITE_INSTALL, { itemId }) as Promise<InstalledSuite>;
  },

  /** 读取本机信任的发布者公钥（信任锚只在桌面端本机） */
  trustGet(): Promise<MarketTrustList> {
    return getAPI().invoke(IPC_CHANNELS.MARKET_TRUST_GET) as Promise<MarketTrustList>;
  },

  /** 信任发布者：同一 id 换钥会拒绝，必须先移除旧信任 */
  trustAdd(req: MarketTrustAddReq): Promise<MarketTrustList> {
    return getAPI().invoke(IPC_CHANNELS.MARKET_TRUST_ADD, req) as Promise<MarketTrustList>;
  },

  /** 移除发布者信任（幂等；不存在时原样返回当前列表） */
  trustRemove(req: MarketTrustRemoveReq): Promise<MarketTrustList> {
    return getAPI().invoke(IPC_CHANNELS.MARKET_TRUST_REMOVE, req) as Promise<MarketTrustList>;
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

  /** 发起 OAuth 授权（桌面端拉系统浏览器 + 本机回调；令牌不进渲染层） */
  oauthAuthorize(serverId: string): Promise<McpServer> {
    return getAPI().invoke(IPC_CHANNELS.MCP_OAUTH_AUTHORIZE, { serverId }) as Promise<McpServer>;
  },

  /** 撤销本地授权（清系统凭据库令牌并清连接态） */
  oauthRevoke(serverId: string): Promise<McpServer> {
    return getAPI().invoke(IPC_CHANNELS.MCP_OAUTH_REVOKE, { serverId }) as Promise<McpServer>;
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

  /** 探测综合服务端是否可达（失败原因在 error，不抛错） */
  serverProbe(): Promise<AuthServerProbe> {
    return getAPI().invoke(IPC_CHANNELS.AUTH_SERVER_PROBE) as Promise<AuthServerProbe>;
  },
};

// ── 项目协作 API（跨用户 / 跨机器，桌面端 Rust 代理账号服务） ──
export const collabAPI = {
  /** 协作服务可用性（地址、真实登录态；不含令牌） */
  status(): Promise<CollabStatus> {
    return getAPI().invoke(IPC_CHANNELS.COLLAB_STATUS) as Promise<CollabStatus>;
  },

  /** 我参与的项目列表 */
  listProjects(): Promise<CollabProjectListResp> {
    return getAPI().invoke(IPC_CHANNELS.COLLAB_LIST_PROJECTS) as Promise<CollabProjectListResp>;
  },

  /** 创建项目（创建者自动成为 owner） */
  createProject(name: string, description = ''): Promise<CollabSnapshot> {
    return getAPI().invoke(IPC_CHANNELS.COLLAB_CREATE_PROJECT, { name, description }) as Promise<CollabSnapshot>;
  },

  /** 项目快照（成员 / 任务 / 评论 / 待接受邀请 + seq） */
  detail(projectId: string): Promise<CollabSnapshot> {
    return getAPI().invoke(IPC_CHANNELS.COLLAB_PROJECT_DETAIL, projectId) as Promise<CollabSnapshot>;
  },

  /** 生成邀请码（明文只返回这一次；服务端只存摘要） */
  createInvite(projectId: string, email: string, role: CollabRole): Promise<CollabInviteCreatedResp> {
    return getAPI().invoke(IPC_CHANNELS.COLLAB_CREATE_INVITE, { projectId, email, role }) as Promise<CollabInviteCreatedResp>;
  },

  /** 待接受邀请（不含邀请码） */
  listInvites(projectId: string): Promise<{ ok: true; invites: CollabInvite[] }> {
    return getAPI().invoke(IPC_CHANNELS.COLLAB_LIST_INVITES, projectId) as Promise<{
      ok: true;
      invites: CollabInvite[];
    }>;
  },

  /** 接受邀请（邀请码一次性，且邮箱须与当前账号一致） */
  acceptInvite(token: string): Promise<CollabSnapshot> {
    return getAPI().invoke(IPC_CHANNELS.COLLAB_ACCEPT_INVITE, token) as Promise<CollabSnapshot>;
  },

  updateMemberRole(projectId: string, uid: string, role: CollabRole): Promise<{ ok: true }> {
    return getAPI().invoke(IPC_CHANNELS.COLLAB_UPDATE_MEMBER_ROLE, { projectId, uid, role }) as Promise<{
      ok: true;
    }>;
  },

  /** 移除成员（软移除，历史任务与评论保留） */
  removeMember(projectId: string, uid: string): Promise<{ ok: true; unassignedTaskIds: string[] }> {
    return getAPI().invoke(IPC_CHANNELS.COLLAB_REMOVE_MEMBER, { projectId, uid }) as Promise<{
      ok: true;
      unassignedTaskIds: string[];
    }>;
  },

  /** 转让所有权（仅 owner） */
  transferOwner(projectId: string, uid: string): Promise<{ ok: true }> {
    return getAPI().invoke(IPC_CHANNELS.COLLAB_TRANSFER_OWNER, { projectId, uid }) as Promise<{ ok: true }>;
  },

  leave(projectId: string): Promise<{ ok: true }> {
    return getAPI().invoke(IPC_CHANNELS.COLLAB_LEAVE_PROJECT, projectId) as Promise<{ ok: true }>;
  },

  createTask(
    projectId: string,
    title: string,
    detail = '',
    assigneeUid: string | null = null,
  ): Promise<CollabTaskResp> {
    return getAPI().invoke(IPC_CHANNELS.COLLAB_CREATE_TASK, {
      projectId,
      title,
      detail,
      assigneeUid,
    }) as Promise<CollabTaskResp>;
  },

  /** 修改任务（必须带 version；冲突返回 PROJECT_CONFLICT） */
  updateTask(projectId: string, taskId: string, patch: CollabTaskPatch): Promise<CollabTaskResp> {
    return getAPI().invoke(IPC_CHANNELS.COLLAB_UPDATE_TASK, { projectId, taskId, patch }) as Promise<CollabTaskResp>;
  },

  addComment(projectId: string, taskId: string, body: string): Promise<CollabCommentResp> {
    return getAPI().invoke(IPC_CHANNELS.COLLAB_ADD_COMMENT, { projectId, taskId, body }) as Promise<CollabCommentResp>;
  },

  /** 回报执行结果（仅负责人；摘要 + 产物文件名 + 机器标识） */
  reportResult(input: {
    projectId: string;
    taskId: string;
    version: number;
    summary: string;
    artifacts: string[];
    machine: string;
    status: 'review' | 'done';
  }): Promise<CollabTaskResp> {
    return getAPI().invoke(IPC_CHANNELS.COLLAB_REPORT_RESULT, input) as Promise<CollabTaskResp>;
  },
};

// ── System API ───────────────────────────────────────────────

export const systemAPI = {
  /** 获取系统信息 */
  info(): Promise<Record<string, unknown>> {
    return getAPI().invoke(IPC_CHANNELS.SYSTEM_INFO) as Promise<Record<string, unknown>>;
  },

  /**
   * 内置工具清单：桌面端由 Rust 向运行中的引擎要实测注册表（引擎没跑时回落到
   * 同源声明值），引擎直连通道读 GET /api/tools，演示通道给声明清单。
   * 返回体带 source，界面据此说明这份清单怎么来的。
   */
  builtinTools(): Promise<import('../../shared/contracts').BuiltinToolList> {
    return getAPI().invoke(IPC_CHANNELS.AGENT_BUILTIN_TOOLS) as Promise<
      import('../../shared/contracts').BuiltinToolList
    >;
  },

  /**
   * 把会话弹出为独立任务窗口（popWindow 实验特性）。
   * 仅桌面端有 window 后端；浏览器直连会明确失败而不是静默。
   */
  openTaskWindow(
    conversationId: string,
    title?: string,
  ): Promise<{ label: string; existing: boolean }> {
    return getAPI().invoke(
      IPC_CHANNELS.TASK_OPEN_WINDOW,
      conversationId,
      title ?? null,
    ) as Promise<{ label: string; existing: boolean }>;
  },
};

// ── 统一导出 ─────────────────────────────────────────────────

// ── P0 API（三层架构演进：取消/任务状态/配额/租约/重放） ───────
// 当前运行通道没实现该方法时显式失败：伪造 cancelled=false / null / 空事件
// 会让调用方把「没实现」当成「真的没有」，问题被静默吞掉。

function callRequired<T>(method: string, ...args: unknown[]): Promise<T> {
  const apiObj = getAPI() as unknown as Record<string, unknown>;
  const fn = apiObj[method];
  if (typeof fn === 'function') {
    return (fn as (...a: unknown[]) => Promise<T>)(...args);
  }
  return Promise.reject(
    new Error(`IPC_CHANNEL_UNHANDLED: 当前运行通道未实现 ${method}（已阻止静默空结果）`),
  );
}

export const p0API = {
  /** 取消任务（转移 #3/#8/#19/#23/#28 → cancelled） */
  cancel(
    taskId: string,
    workspaceId?: string,
  ): Promise<{ taskId: string; cancelled: boolean; status?: string }> {
    return callRequired('chatCancel', taskId, workspaceId);
  },

  /**
   * 事件重放（24h 持久化，seq > fromSeq）。
   * 配额闸门与租约管理仍在 Rust 提交/执行链路上生效，但界面没有对应视图，
   * 因此不再暴露无人调用的 taskState / quotaStatus / leaseInfo 查询口。
   */
  eventReplay(taskId: string, fromSeq = 0): Promise<{ taskId: string; events: unknown[] }> {
    return callRequired('eventReplay', taskId, fromSeq);
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
  collab: collabAPI,
  system: systemAPI,
  p0: p0API,
  experts: expertsAPI,
};

export default api;
