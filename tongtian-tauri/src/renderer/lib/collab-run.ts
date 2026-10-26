/**
 * 协作任务的本机执行绑定与结果回报
 *
 * 用户在协作页点「在本机执行」后，记下会话 → 项目/任务的绑定；
 * 本轮任务 done 时把最终回复摘要与产物**文件名**回报给协作服务。
 * 回报失败不假装成功：进入待回报队列，协作页可见并允许重试。
 *
 * 只在 localStorage 存项目/任务标识与版本号，不存任何令牌或对话正文。
 */

import { errorTextOf } from '../../shared/error-messages';
import { api } from './api';

const BINDING_KEY = 'tt.collab.bindings.v1';
const PENDING_KEY = 'tt.collab.pendingReports.v1';

export interface CollabRunBinding {
  conversationId: string;
  projectId: string;
  projectName: string;
  taskId: string;
  taskTitle: string;
  /** 标记 doing 后的任务版本，回报时用于乐观并发校验 */
  version: number;
  startedAtMs: number;
}

export interface CollabReportInput {
  projectId: string;
  taskId: string;
  version: number;
  summary: string;
  artifacts: string[];
  machine: string;
  status: 'review' | 'done';
}

export interface CollabPendingReport {
  id: string;
  conversationId: string;
  projectId: string;
  projectName: string;
  taskTitle: string;
  input: CollabReportInput;
  error: string;
  createdAtMs: number;
}

type BindingMap = Record<string, CollabRunBinding>;

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function writeJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* 存储不可用时仅保留内存态，不阻断任务 */
  }
}

function bindings(): BindingMap {
  return readJson<BindingMap>(BINDING_KEY, {});
}

function writeBindings(map: BindingMap): void {
  writeJson(BINDING_KEY, map);
}

export function bindCollabRun(binding: CollabRunBinding): void {
  // 新绑定时清掉同一会话上一次执行留下的暂存，避免串轮
  delivered.delete(binding.conversationId);
  failed.delete(binding.conversationId);
  writeBindings({ ...bindings(), [binding.conversationId]: binding });
}

export function collabBindingOf(conversationId: string): CollabRunBinding | null {
  return bindings()[conversationId] ?? null;
}

export function clearCollabBinding(conversationId: string): void {
  const map = bindings();
  if (!(conversationId in map)) return;
  delete map[conversationId];
  writeBindings(map);
}

export function pendingReports(): CollabPendingReport[] {
  return readJson<CollabPendingReport[]>(PENDING_KEY, []).sort(
    (a, b) => b.createdAtMs - a.createdAtMs,
  );
}

export function discardPendingReport(id: string): void {
  writeJson(
    PENDING_KEY,
    pendingReports().filter((item) => item.id !== id),
  );
}

function enqueuePending(item: CollabPendingReport): void {
  const rows = pendingReports().filter((row) => row.id !== item.id);
  writeJson(PENDING_KEY, [item, ...rows].slice(0, 20));
}

// 本轮 deliver 的正文与产物，等 done 时再回报（done 事件本身不带内容）
const delivered = new Map<string, { summary: string; artifacts: string[] }>();
// 本轮是否出现过错误事件：先 deliver 后报错（如收尾 Hook 失败）也不回报成功
const failed = new Set<string>();

export function captureCollabDeliver(
  conversationId: string,
  summary: string,
  artifacts: string[],
): void {
  if (!collabBindingOf(conversationId)) return;
  delivered.set(conversationId, { summary, artifacts });
}

export function captureCollabFailure(conversationId: string): void {
  if (!collabBindingOf(conversationId)) return;
  failed.add(conversationId);
}

const basename = (path: string) => path.replace(/\\/g, '/').split('/').pop() ?? path;

let machineCache = '';

async function machineLabel(): Promise<string> {
  if (machineCache) return machineCache;
  try {
    const info = await api.system.info();
    const platform = String(info.platform ?? '').trim();
    const arch = String(info.arch ?? '').trim();
    if (platform) machineCache = [platform, arch].filter(Boolean).join('-').slice(0, 60);
  } catch {
    /* 拿不到系统信息时退回平台串，不伪造主机名 */
  }
  if (!machineCache) machineCache = (navigator.platform || 'unknown').slice(0, 60);
  return machineCache;
}

const reporting = new Set<string>();

/**
 * 本轮任务 done 时调用：有绑定且本轮真的产出才回报。
 * 失败/取消也会发 done 但没有 deliver —— 此时不回报，任务保持「进行中」等人处理，
 * 绝不把失败写成「已完成」；成功但回报失败则进待回报队列。
 */
export async function completeCollabRun(conversationId: string): Promise<void> {
  const binding = collabBindingOf(conversationId);
  if (!binding || reporting.has(conversationId)) return;
  reporting.add(conversationId);
  const deliver = delivered.get(conversationId);
  delivered.delete(conversationId);
  const failedThisRun = failed.delete(conversationId);
  if (!deliver || failedThisRun) {
    clearCollabBinding(conversationId);
    reporting.delete(conversationId);
    return;
  }
  const input: CollabReportInput = {
    projectId: binding.projectId,
    taskId: binding.taskId,
    version: binding.version,
    summary: (deliver.summary.trim() || '任务已完成（模型未返回文字摘要）').slice(0, 4000),
    artifacts: deliver.artifacts.map(basename).filter(Boolean).slice(0, 20),
    machine: await machineLabel(),
    status: 'review',
  };
  try {
    await api.collab.reportResult(input);
    clearCollabBinding(conversationId);
  } catch (err) {
    enqueuePending({
      id: `cr-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      conversationId,
      projectId: binding.projectId,
      projectName: binding.projectName,
      taskTitle: binding.taskTitle,
      input,
      error: errorTextOf(err, '结果回报失败'),
      createdAtMs: Date.now(),
    });
    clearCollabBinding(conversationId);
  } finally {
    reporting.delete(conversationId);
  }
}

/** 重试一条待回报记录；成功即从队列移除 */
export async function retryPendingReport(id: string): Promise<{ ok: boolean; error?: string }> {
  const row = pendingReports().find((item) => item.id === id);
  if (!row) return { ok: false, error: '该待回报记录已不存在' };
  try {
    await api.collab.reportResult(row.input);
    discardPendingReport(id);
    return { ok: true };
  } catch (err) {
    const message = errorTextOf(err, '结果回报失败');
    writeJson(
      PENDING_KEY,
      pendingReports().map((item) => (item.id === id ? { ...item, error: message } : item)),
    );
    return { ok: false, error: message };
  }
}

/** 标记任务为进行中，返回新版本号用于回报 */
export async function markTaskDoing(
  projectId: string,
  taskId: string,
  version: number,
): Promise<number> {
  const resp = await api.collab.updateTask(projectId, taskId, { version, status: 'doing' });
  return resp.task.version;
}
