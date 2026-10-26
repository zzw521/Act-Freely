// src/renderer/lib/transport.ts — 传输适配层（流 G1）
//
// 落实主 SPEC §10 传输适配层：桌面端 local-first 不变，云端能力经用户端 BFF 接入。
// 统一 TransportProvider 接口，三种实现：
// - TauriTransport：Tauri command/event（本期主路径，经 window.tongtianAPI 注入）
// - MockTransport：全真模拟（开发阶段 Mock 开关，VITE_MOCK 控制）
// - BffTransport：云端 REST+SSE 骨架占位（Phase B 接用户端 BFF，本期不启用）
//
// 云端切换时仅替换 TransportProvider 实现，契约零改动（seq/14 态/错误码不变）。

import type { ChatEvent, ChatSubmitReq } from '../../shared/types';
import type { QuotaSnapshot } from '../../shared/contracts';

export interface EventReplayItem {
  seq?: number | null;
  ts?: number;
  kind?: string;
  data?: Record<string, unknown>;
}

/** 统一传输契约（三层架构演进 P0：14 态/取消/配额/租约/重放） */
export interface TransportProvider {
  readonly kind: 'tauri' | 'mock' | 'bff';

  chatSubmit(req: ChatSubmitReq): Promise<{ taskId: string; workspaceId?: string; quota?: QuotaSnapshot }>;
  chatCancel(taskId: string, workspaceId?: string): Promise<{ taskId: string; cancelled: boolean }>;

  /** 事件重放（24h 持久化，seq > fromSeq） */
  eventReplay(taskId: string, fromSeq?: number): Promise<{ taskId: string; events: EventReplayItem[] }>;

  /** 订阅事件流（seq 去重由 EventStreamGuard 承接），返回取消函数 */
  onChatEvent(callback: (event: ChatEvent) => void): () => void;

  systemInfo(): Promise<Record<string, unknown>>;
}

// ── 实现 1：Tauri command/event（经 window.tongtianAPI 注入） ──

type WindowAPI = {
  invoke: (channel: string) => Promise<unknown>;
  chatSubmit?: TransportProvider['chatSubmit'];
  chatCancel?: TransportProvider['chatCancel'];
  eventReplay?: TransportProvider['eventReplay'];
  onChatEvent: TransportProvider['onChatEvent'];
  systemInfo?: () => Promise<Record<string, unknown>>;
};

function windowAPI(): WindowAPI {
  const api = (window as unknown as { tongtianAPI?: WindowAPI }).tongtianAPI;
  if (!api) {
    throw new Error('tongtianAPI 未注入（请先导入 renderer/lib/mock-api）');
  }
  return api;
}

export class TauriTransport implements TransportProvider {
  readonly kind: TransportKind;

  constructor(kind: TransportKind = 'tauri') {
    this.kind = kind;
  }

  chatSubmit(req: ChatSubmitReq) {
    return windowAPI().chatSubmit!(req);
  }
  chatCancel(taskId: string, workspaceId?: string) {
    const api = windowAPI();
    return api.chatCancel
      ? api.chatCancel(taskId, workspaceId)
      : Promise.resolve({ taskId, cancelled: false });
  }
  eventReplay(taskId: string, fromSeq = 0) {
    const api = windowAPI();
    return api.eventReplay
      ? api.eventReplay(taskId, fromSeq)
      : (api.invoke('event:replay') as Promise<{ taskId: string; events: EventReplayItem[] }>);
  }
  onChatEvent(callback: (event: ChatEvent) => void) {
    return windowAPI().onChatEvent(callback);
  }
  systemInfo() {
    const api = windowAPI();
    return api.systemInfo ? api.systemInfo() : (api.invoke('system:info') as Promise<Record<string, unknown>>);
  }
}

// ── 实现 2：Mock（开发阶段，同样经 window.tongtianAPI，kind 标识不同） ──

export class MockTransport extends TauriTransport {
  constructor() {
    super('mock');
  }
}

// ── 实现 3：云端 BFF 骨架占位（Phase B 启用） ──

/**
 * 云端传输占位：REST + SSE（带 Last-Event-ID 与 seq 去重），
 * 经用户端 BFF 接入 Agent 平台（前端不得直连平台，ADJ-R1-1）。
 * 本期不启用；接口形状已按 P0 契约定义，届时仅需填充实现。
 */
export class BffTransport implements TransportProvider {
  readonly kind = 'bff' as const;

  constructor(private readonly baseUrl: string = '') {
    void this.baseUrl;
  }

  private notReady(): never {
    throw new Error('NOT_IMPLEMENTED: 云端 BFF 传输将在 Phase B 接入（本期仅本地 local-first）');
  }

  chatSubmit(): never {
    return this.notReady();
  }
  chatCancel(): never {
    return this.notReady();
  }
  eventReplay(): never {
    return this.notReady();
  }
  onChatEvent(): never {
    return this.notReady();
  }
  systemInfo(): never {
    return this.notReady();
  }
}

// ── 运行环境探测与注入 ─────────────────────────────────────

export type TransportKind = 'tauri' | 'mock' | 'bff';

/** 探测当前运行环境（与 mock-api.ts 注入策略一致） */
export function detectTransportKind(): TransportKind {
  if (typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window) {
    return 'tauri';
  }
  const mockDisabled =
    typeof import.meta !== 'undefined' &&
    (import.meta as unknown as { env?: Record<string, string> }).env?.VITE_MOCK === 'false';
  return mockDisabled ? 'bff' : 'mock';
}

let cached: TransportProvider | null = null;

/** 获取当前传输实现（单例缓存） */
export function getTransport(): TransportProvider {
  if (cached) return cached;
  const kind = detectTransportKind();
  cached =
    kind === 'tauri'
      ? new TauriTransport()
      : kind === 'mock'
        ? new MockTransport()
        : new BffTransport();
  return cached;
}

/** 测试/联调注入点 */
export function setTransport(provider: TransportProvider): void {
  cached = provider;
}
