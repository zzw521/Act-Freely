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
  UsageStats,
  ChatStepPreview,
  AttachmentMeta,
} from '../../shared/types';
import type { AuthLoginResp, AuthStatus } from '../../shared/contracts';

// ── 辅助函数 ─────────────────────────────────────────────────

function getAPI() {
  const api = window.tongtianAPI;
  if (!api) {
    throw new Error('tongtianAPI is not available. Are you running inside Electron?');
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

  /** 获取模型用量统计 */
  usage(): Promise<UsageStats[]> {
    return getAPI().invoke(IPC_CHANNELS.MODEL_USAGE) as Promise<UsageStats[]>;
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

  /** 批量预览（docx/xlsx/pdf 返回转换未落地提示，不静默） */
  preview(filePaths: string[], workspaceId?: string): Promise<unknown[]> {
    return getAPI().invoke('file:preview', filePaths, workspaceId) as Promise<unknown[]>;
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
  auth: authAPI,
  system: systemAPI,
  p0: p0API,
};

export default api;
