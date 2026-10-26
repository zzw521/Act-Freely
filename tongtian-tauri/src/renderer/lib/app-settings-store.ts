/**
 * 应用设置的模块级共享缓存。
 *
 * 每个 useAppSettings 实例各自 useState 会导致「设置页改了开关，已挂载的
 * 侧栏 / 对话流没有反应」，必须刷新或重挂才生效。这里让所有消费方订阅
 * 同一份缓存：写入先更新缓存并同步通知，再异步落盘。
 */

import { getSettings, setSettings } from './alignment-api';
import type { AppSettingsData } from '../../shared/contracts';

let cache: Partial<AppSettingsData> = {};
let loaded = false;
let loading: Promise<void> | null = null;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

function ensureLoaded(): void {
  if (loaded || loading) return;
  loading = getSettings()
    .then((data) => {
      // 初次加载返回前可能已有本地修改，本地修改优先，避免被迟到的快照覆盖
      cache = { ...data, ...cache };
      loaded = true;
    })
    .catch(() => {
      // 读取失败时保留本地缓存，设置页仍可继续写入
      loaded = true;
    })
    .finally(() => {
      loading = null;
      emit();
    });
}

export function subscribeAppSettings(listener: () => void): () => void {
  listeners.add(listener);
  ensureLoaded();
  return () => {
    listeners.delete(listener);
  };
}

/** 必须返回同一个对象引用，直到下一次写入（useSyncExternalStore 依赖此约定） */
export function getAppSettingsSnapshot(): Partial<AppSettingsData> {
  return cache;
}

/**
 * 写入设置：先乐观更新共享缓存，再异步落盘。
 * `strict` 用于系统行为开关（自启/后台常驻等）：落盘失败必须回滚并抛出，
 * 否则界面会停在一个「看起来开了、实际没生效」的状态。
 */
export async function updateAppSettings(
  patch: Partial<AppSettingsData>,
  options?: { strict?: boolean },
): Promise<void> {
  const previous = cache;
  cache = { ...cache, ...patch };
  emit();
  try {
    await setSettings(patch, options?.strict ? { strict: true } : undefined);
  } catch (e) {
    if (!options?.strict) return;
    // 只回滚仍是本次写入值的键，避免覆盖并发写入的其他设置
    const records = cache as Record<string, unknown>;
    const back = previous as Record<string, unknown>;
    const patchRecord = patch as Record<string, unknown>;
    const next = { ...records };
    for (const key of Object.keys(patchRecord)) {
      if (Object.is(records[key], patchRecord[key])) {
        if (key in back) next[key] = back[key];
        else delete next[key];
      }
    }
    cache = next as Partial<AppSettingsData>;
    emit();
    throw e;
  }
}
