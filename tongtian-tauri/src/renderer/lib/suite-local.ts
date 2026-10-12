/**
 * 专家套件 · 本地通道实现（Mock 与浏览器直连引擎共用）
 *
 * 桌面端以 `{data_home}/suites/<id>/manifest.json` 为事实来源（Rust `suite::list_in`）；
 * 浏览器通道不能写盘，但可以解析上传的同一个 .zip 包（见 `suite-zip.ts`），
 * 因此这里维护一份 localStorage 台账：
 * - `suite:root` 如实回报 supported=false（无磁盘目录，页面据此提示「浏览器台账不写盘」）
 * - `suite:list / install / uninstall` 全部真实生效于台账本身：同一个包在两端得到同一标识，
 *   指令、技能数、文件数口径一致，页面与输入框候选拿到的都是解析出来的真实内容
 * - 台账只保存归一后的套件清单（不保存包内文件字节），因此不写盘、也不谎称写盘
 */

import type { InstalledSuite, SuiteInstallReq } from '../../shared/contracts';
import { SuitePackageError, parseSuitePackageFromBase64 } from './suite-zip';

const LEDGER_KEY = 'tt:suites';

export interface LocalSuiteResult {
  handled: boolean;
  value?: unknown;
}

/** 台账读写（损坏的 JSON 视为空台账，读取失败不阻断页面） */
function readLedger(): InstalledSuite[] {
  try {
    const raw = localStorage.getItem(LEDGER_KEY);
    const parsed = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(parsed) ? (parsed as InstalledSuite[]) : [];
  } catch {
    return [];
  }
}

function writeLedger(rows: InstalledSuite[]): void {
  try {
    localStorage.setItem(LEDGER_KEY, JSON.stringify(rows));
  } catch {
    throw new Error('SUITE_INSTALL_FAILED: 套件台账写入失败（本地存储已满或被禁用）');
  }
}

/** 已安装套件：按安装时间倒序（与 Rust list_in 同序） */
export function listLocalSuites(): InstalledSuite[] {
  return [...readLedger()].sort((a, b) => (b.installedAtMs ?? 0) - (a.installedAtMs ?? 0));
}

/** 安装：解析 .zip 后追加台账（同名套件已存在时拒绝，不覆盖既有条目） */
export async function installLocalSuite(req: SuiteInstallReq): Promise<InstalledSuite> {
  const zipBase64 = (req?.zipBase64 ?? '').trim();
  if (!zipBase64) {
    throw new SuitePackageError('SUITE_INCOMPATIBLE: 未提供套件包内容（请上传 .zip）');
  }
  const { suite } = await parseSuitePackageFromBase64(zipBase64);
  const rows = readLedger();
  const existed = rows.find((r) => r.id === suite.id);
  if (existed) {
    throw new Error(
      `SUITE_INSTALL_FAILED: 套件「${existed.name}」已安装，请先卸载后再安装（不覆盖既有套件）`,
    );
  }
  // 浏览器台账没有磁盘目录：如实标注来源，页面据此提示不写盘
  const row: InstalledSuite = {
    ...suite,
    installedAtMs: Date.now(),
    path: `local://suites/${suite.id}`,
  };
  writeLedger([...rows, row]);
  return row;
}

/** 卸载：从台账移除（包内文件本就不落盘，移除即不再可调用） */
export function uninstallLocalSuite(rawId: string): { ok: boolean; removed: string; trashPath: string } {
  const id = (rawId ?? '').trim();
  const rows = readLedger();
  if (!rows.some((r) => r.id === id)) {
    throw new Error(`SUITE_NOT_FOUND: 套件台账中没有：${id || '(空标识)'}`);
  }
  writeLedger(rows.filter((r) => r.id !== id));
  return { ok: true, removed: id, trashPath: `local://suites_removed/${id}` };
}

/** 套件通道本地实现：未命中返回 handled=false（安装为异步解析，value 可能是 Promise） */
export function handleLocalSuiteChannel(channel: string, args: unknown[]): LocalSuiteResult {
  switch (channel) {
    case 'suite:root':
      // 浏览器通道无落盘能力：不编造运行目录，supported=false 由页面提示「仅本地台账」
      void args;
      return { handled: true, value: { supported: false, suitesRoot: null, trashRoot: null } };
    case 'suite:list':
      void args;
      return { handled: true, value: listLocalSuites() };
    case 'suite:install':
      return {
        handled: true,
        value: installLocalSuite((args[0] ?? {}) as SuiteInstallReq),
      };
    case 'suite:uninstall': {
      const req = (args[0] ?? {}) as { suiteId?: string; id?: string };
      return { handled: true, value: uninstallLocalSuite(req.suiteId ?? req.id ?? '') };
    }
    default:
      return { handled: false };
  }
}
