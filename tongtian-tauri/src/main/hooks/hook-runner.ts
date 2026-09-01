// ============================================================
// src/main/hooks/hook-runner.ts — HookRunner 引擎
// 千问对标整改 spec §5.1（对齐 QwenWork Hooks 事件模型）
//
// 语义：
// - 确定性执行：事件命中即执行脚本，不受模型影响
// - stdin 传 JSON，exit 0=成功 / 2=阻塞（仅 PreToolUse/Stop/SubagentStop）
// - matcher：不填或 * 匹配全部；"A|B" 多值；支持正则
// ============================================================

import { spawn } from 'child_process';
import Database from '../store/db';
import Logger from '../util/logger';
import type {
  HookAggregate,
  HookConfigStore,
  HookEventName,
  HookInput,
  HookMatcherGroup,
  HookResult,
} from '../../shared/contracts';

const logger = Logger.getInstance();

/** 支持 exit 2 阻塞语义的事件 */
const BLOCKABLE_EVENTS: ReadonlySet<HookEventName> = new Set([
  'PreToolUse',
  'Stop',
  'SubagentStop',
]);

export const ALL_HOOK_EVENTS: HookEventName[] = [
  'SessionStart', 'SessionEnd', 'UserPromptSubmit',
  'PreToolUse', 'PostToolUse', 'PostToolUseFailure',
  'Stop', 'SubagentStart', 'SubagentStop',
  'PreCompact', 'Notification', 'PermissionRequest',
];

// ── matcher 匹配 ────────────────────────────────────────────

/**
 * matcher 写法：不填/"*"=全部；"Bash"=精确；"Write|Edit"=多值；正则表达式
 * target 为事件附加的匹配字段值（如 tool_name）
 */
export function matchHook(matcher: string | undefined, target: string): boolean {
  if (!matcher || matcher.trim() === '' || matcher.trim() === '*') return true;
  const trimmed = matcher.trim();

  // 多值：A|B（先按精确多值处理；含正则元字符时按正则处理）
  if (trimmed.includes('|')) {
    const parts = trimmed.split('|').map((p) => p.trim()).filter(Boolean);
    const allLiteral = parts.every((p) => !/[*+?^$.[\](){}\\]/.test(p));
    if (allLiteral) return parts.includes(target);
  }

  // 精确匹配
  if (trimmed === target) return true;

  // 正则匹配
  try {
    return new RegExp(`^(?:${trimmed})$`).test(target);
  } catch {
    return false;
  }
}

/** 提取事件的 matcher 目标字段 */
function getMatchTarget(event: HookEventName, input: HookInput): string {
  switch (event) {
    case 'PreToolUse':
    case 'PostToolUse':
    case 'PostToolUseFailure':
    case 'PermissionRequest':
      return String(input.tool_name ?? '');
    case 'SessionStart':
      return String(input.source ?? '');
    case 'SessionEnd':
      return String(input.reason ?? '');
    case 'SubagentStart':
    case 'SubagentStop':
      return String(input.agent_type ?? '');
    case 'PreCompact':
      return String(input.trigger ?? '');
    case 'Notification':
      return String(input.notification_type ?? '');
    default:
      return '';
  }
}

// ── 脚本执行 ────────────────────────────────────────────────

function expandHome(cmd: string): string {
  const home = process.env.USERPROFILE || process.env.HOME || '';
  if (cmd.startsWith('~/') || cmd === '~') return cmd.replace(/^~/, home);
  return cmd;
}

function runHookCommand(command: string, inputJson: string, timeoutSec: number): Promise<HookResult> {
  return new Promise((resolve) => {
    const shellCmd = expandHome(command);
    const isWin = process.platform === 'win32';
    const child = spawn(isWin ? 'cmd' : '/bin/sh', isWin ? ['/c', shellCmd] : ['-c', shellCmd], {
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });

    let stdout = '';
    let stderr = '';
    let timedOut = false;

    const timer = setTimeout(() => {
      timedOut = true;
      try {
        if (process.platform === 'win32' && child.pid) {
          // Windows 下需 taskkill /T 杀整个进程树，否则 cmd 的子进程会继续运行
          spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true });
        } else {
          child.kill('SIGKILL');
        }
      } catch { /* ignore */ }
    }, Math.max(1, timeoutSec) * 1000);

    child.stdout.on('data', (d: Buffer) => { stdout += d.toString(); });
    child.stderr.on('data', (d: Buffer) => { stderr += d.toString(); });

    child.on('error', (err) => {
      clearTimeout(timer);
      resolve({ exitCode: 1, stdout, stderr: stderr || String(err), blocked: false, timedOut });
    });

    child.on('close', (code) => {
      clearTimeout(timer);
      const exitCode = timedOut ? 1 : (code ?? 1);
      resolve({ exitCode, stdout, stderr, blocked: false, timedOut });
    });

    // stdin 传 JSON（对齐 QwenWork 契约）
    child.stdin.write(inputJson);
    child.stdin.end();
  });
}

// ── HookRunner ──────────────────────────────────────────────

class HookRunner {
  private static instance: HookRunner | null = null;

  static getInstance(): HookRunner {
    if (!HookRunner.instance) HookRunner.instance = new HookRunner();
    return HookRunner.instance;
  }

  // ── 配置读写 ─────────────────────────────────────────────

  getConfig(): HookConfigStore {
    try {
      const db = Database.getInstance().getDb();
      const row = db.prepare("SELECT hooks_json FROM hook_configs WHERE id = 'global'").get() as
        | { hooks_json: string }
        | undefined;
      const parsed = row ? JSON.parse(row.hooks_json) : {};
      return { hooks: parsed.hooks ?? parsed };
    } catch (err) {
      logger.error('hooks config load failed', 'hooks', { error: String(err) });
      return { hooks: {} };
    }
  }

  setConfig(config: HookConfigStore): void {
    const hooksJson = JSON.stringify(config.hooks ?? {});
    const db = Database.getInstance().getDb();
    db.prepare(
      `INSERT INTO hook_configs (id, hooks_json, updated_at) VALUES ('global', ?, datetime('now'))
       ON CONFLICT(id) DO UPDATE SET hooks_json = excluded.hooks_json, updated_at = datetime('now')`,
    ).run(hooksJson);
    logger.info('hooks config saved', 'hooks', { events: Object.keys(config.hooks ?? {}).join(',') });
  }

  // ── 事件触发 ─────────────────────────────────────────────

  /** 触发事件，顺序执行命中的 hooks，返回聚合结果 */
  async emit(event: HookEventName, input: HookInput): Promise<HookAggregate> {
    const config = this.getConfig();
    const groups = config.hooks[event];
    if (!groups || groups.length === 0) return { blocked: false, results: [] };

    const target = getMatchTarget(event, input);
    const inputJson = JSON.stringify(input);
    const blockable = BLOCKABLE_EVENTS.has(event);
    const results: HookResult[] = [];
    let blocked = false;
    let blockReason: string | undefined;

    for (const group of groups) {
      if (!matchHook(group.matcher, target)) continue;
      for (const hook of group.hooks ?? []) {
        if (hook.type !== 'command' || !hook.command) continue;
        const result = await runHookCommand(hook.command, inputJson, hook.timeout ?? 60);
        if (blockable && result.exitCode === 2) {
          result.blocked = true;
          blocked = true;
          blockReason = blockReason ?? result.stderr.trim();
        }
        results.push(result);
        logger.info('hook executed', 'hooks', {
          event,
          command: hook.command,
          exitCode: result.exitCode,
          timedOut: result.timedOut,
        });
      }
    }

    return { blocked, blockReason, results };
  }

  /** 试运行单个 hook（hooks:test IPC 用） */
  async testRun(event: HookEventName, input: HookInput, command: string, timeoutSec = 60): Promise<HookResult> {
    return runHookCommand(command, JSON.stringify({ ...input, hook_event_name: event }), timeoutSec);
  }
}

export default HookRunner;
export { matchHook as matchHookRule };
