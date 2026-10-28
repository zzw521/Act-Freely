/**
 * 安全工作环境通道契约测试（任务 #26/#27）
 *
 * 设置页的「当前占用 1.2 GB」原本是一行写死的文案，「清理释放空间」是个没有
 * 任何 onClick 的装饰性 Box；「启用安全工作环境」开关也只落 settings_kv，没有
 * 任何一个执行链路读它。本文件从源码 + 行为两侧锁定这条链路是真的：
 * - Rust：security::environment 走磁盘实测，清理范围锁死在 workspaces/{id}/environment
 * - 引擎：/api/sandbox/environment/{usage,clear} 同源，secureEnv 决定是否隔离执行
 * - 契约：两个通道常量都有真实消费方，Rust/引擎抛出的 SANDBOX_ENV_* 码都有文案
 * - 通道：Tauri 分支 / 引擎直连路由 / Mock 快照分支
 * - 页面：SandboxPanel 挂载即拉真实用量，0 字节禁用清理，确认后刷新
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ErrorCode, IPC_CHANNELS } from '../../../src/shared/contracts';
import { errorMessageOf, errorTextOf } from '../../../src/shared/error-messages';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
}

const rustEnv = () => read('../../../src-tauri/src/security/environment.rs');
const rustMod = () => read('../../../src-tauri/src/security/mod.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const rustAgent = () => read('../../../src-tauri/src/bridge/agent.rs');
const contracts = () => read('../../../src/shared/contracts.ts');
const sharedTypes = () => read('../../../src/shared/types.ts');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const engineApi = () => read('../../../src/renderer/lib/engine-api.ts');
const securityLocal = () => read('../../../src/renderer/lib/security-local.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const libApi = () => read('../../../src/renderer/lib/api.ts');
const settingsPage = () => read('../../../src/renderer/pages/Settings/index.tsx');
const workbenchStore = () => read('../../../src/renderer/store/workbenchStore.ts');
const engineServer = () => read('../../../engine/server.py');

/** 提取 Rust environment.rs 与引擎 server.py 中抛出的 SANDBOX_ENV_* 错误码 */
function rustEnvCodes(): string[] {
  const CODE = /(?<=")SANDBOX_ENV_[A-Z][A-Z_]*/g;
  const found = new Set<string>();
  for (const m of rustEnv().matchAll(CODE)) found.add(m[0]);
  for (const m of engineServer().matchAll(CODE)) found.add(m[0]);
  return [...found].sort();
}

// ── Rust 权威实现 ────────────────────────────────────────────────

describe('Rust 工作环境磁盘事实', () => {
  it('用量与清理都按传入 base 计算，便于测试注入临时目录', () => {
    const src = rustEnv();
    expect(src).toContain('pub fn usage_in(base: &Path) -> Value');
    expect(src).toContain('pub fn clear_in(base: &Path) -> Result<Value, String>');
    expect(src).toContain('pub fn env_dir_in(base: &Path, workspace_id: &str) -> PathBuf');
  });

  it('统计不跟随符号链接，读不动的条目单独计数而非中断', () => {
    const src = rustEnv();
    expect(src).toContain('symlink_metadata');
    expect(src).toContain('pub unreadable: u64');
    expect(src).toContain('"unreadable": total.unreadable,');
  });

  it('清理路径经守卫校验：只允许 workspaces/{id}/environment 三段后缀', () => {
    const src = rustEnv();
    expect(src).toContain('fn guard_env_path(base: &Path, target: &Path) -> Result<PathBuf, String>');
    expect(src).toContain('SANDBOX_ENV_PATH_OUT_OF_SCOPE');
    expect(src).toContain('SANDBOX_ENV_BASE_MISSING');
    expect(src).toContain('SANDBOX_ENV_PATH_INVALID');
    // 逐个工作空间取真实路径，越界即拒绝
    expect(src).toContain('guard_env_path(base, &dir)');
  });

  it('释放量按清理前后差值计算，单个目录被占用不会中断整体清理', () => {
    const body = rustEnv().split('pub fn clear_in')[1].split('pub fn first_failure')[0];
    expect(body).toContain('saturating_sub');
    expect(body).toContain('"failed"');
    // 不能因为一个条目失败就早退（早期实现用 ? 传播，会让整批清理半途而废）
    expect(body).not.toMatch(/remove_dir_all\([^)]*\)\?/);
  });

  it('两个 command 均已注册，清理成功与失败都留审计', () => {
    const lib = rustLib();
    expect(lib).toContain('security::sandbox_env_usage,');
    expect(lib).toContain('security::sandbox_env_clear,');
    const mod = rustMod();
    expect(mod).toContain('pub async fn sandbox_env_usage');
    expect(mod).toContain('pub async fn sandbox_env_clear');
    expect(mod).toContain('environment::usage_in(&python_env::base_dir())');
    expect(mod).toContain('environment::clear_in(&python_env::base_dir())');
    expect(mod).toContain('environment::first_failure');
    expect(mod).toContain('audit::record_quiet(');
    expect(mod).toContain('pub mod environment;');
  });

  it('secureEnv 由 Rust 自读设置并随每次提交下发（开关不再是写空的设置项）', () => {
    const lib = rustLib();
    expect(lib).toContain('fn secure_env_enabled(store: &store::Store) -> bool');
    expect(lib).toContain('store::settings::get(store, "secureEnv")');
    expect(lib.match(/secure_env: secure_env_enabled\(&services\.store\)/g)?.length).toBeGreaterThanOrEqual(3);
    expect(rustAgent()).toContain('pub secure_env: bool');
    // secureEnv 由 chat_request_body 装配，入参取自 p0.secure_env（随每次提交下发）
    expect(rustAgent()).toContain('"secureEnv": secure_env');
    expect(rustAgent()).toContain('p0.secure_env,');
    expect(rustAgent()).toContain('p0.privacy,');
  });
});

// ── 引擎侧实现 ───────────────────────────────────────────────────

describe('引擎工作环境端点与隔离执行', () => {
  it('用量与清理两个端点齐备，口径与 Rust 一致（bytes/files/workspaceCount）', () => {
    const src = engineServer();
    expect(src).toContain('@app.get("/api/sandbox/environment/usage")');
    expect(src).toContain('@app.post("/api/sandbox/environment/clear")');
    for (const key of ['"bytes"', '"files"', '"workspaceCount"', '"envDirName"', '"rootPath"']) {
      expect(src).toContain(key);
    }
  });

  it('清理只删 environment/ 内的内容，符号链接一律跳过', () => {
    const body = engineServer().split('@app.post("/api/sandbox/environment/clear")')[1]
      .split('@app.post("/api/files/open-folder")')[0];
    expect(body).toContain('SANDBOX_ENV_CLEAR_SKIPPED_SYMLINK');
    expect(body).toContain('root = env_root()');
    // 删除范围限定在 scandir(root) 的条目，不递归触碰 WORK_DIR 其他子目录
    expect(body).toContain('entries = list(os.scandir(root))');
  });

  it('隔离执行落在 environment/{task_id}，产物先归档进会话目录再允许清理', () => {
    const src = engineServer();
    expect(src).toContain('def _task_work_dir(task_id: str) -> Path');
    expect(src).toContain('def _preserve_artifacts_in_session');
    const run = src.split('def _run_execution')[1].split('\n@app.')[0];
    expect(run).toContain('isolated = work_dir is None and session.secure_env');
    expect(run).toContain('_task_work_dir(task_id) if isolated else WORK_DIR');
    expect(run).toContain('if isolated:');
  });

  it('/api/chat 读取 secureEnv 并写入会话（approve 重跑沿用）', () => {
    const src = engineServer();
    expect(src).toContain('self.secure_env: bool = False');
    expect(src).toContain('session.secure_env = bool(payload.get("secureEnv"))');
  });
});

// ── 契约与三通道 ─────────────────────────────────────────────────

describe('契约常量与前端通道', () => {
  it('两个通道常量都存在且被 api 封装消费（无死常量）', () => {
    expect(IPC_CHANNELS.SANDBOX_ENV_USAGE).toBe('sandbox:envUsage');
    expect(IPC_CHANNELS.SANDBOX_ENV_CLEAR).toBe('sandbox:envClear');
    expect(contracts()).toContain("SANDBOX_ENV_USAGE: 'sandbox:envUsage'");
    expect(contracts()).toContain("SANDBOX_ENV_CLEAR: 'sandbox:envClear'");
    expect(libApi()).toContain('IPC_CHANNELS.SANDBOX_ENV_USAGE');
    expect(libApi()).toContain('IPC_CHANNELS.SANDBOX_ENV_CLEAR');
  });

  it('契约类型齐备：用量视图 / 工作空间明细 / 清理结果', () => {
    const src = contracts();
    for (const t of [
      'export interface SandboxEnvWorkspaceUsage',
      'export interface SandboxEnvUsage',
      'export interface SandboxEnvWorkspaceCleared',
      'export interface SandboxEnvClearResult',
    ]) {
      expect(src).toContain(t);
    }
    expect(src).toContain('unreadable?: number');
    expect(src).toContain('failed: Array<{ workspaceId?: string; path?: string; error: string }>');
    expect(sharedTypes()).toContain('secureEnv?: boolean');
  });

  it('Tauri 通道把两个 channel 映射到对应 command', () => {
    const src = tauriApi().replace(/\r\n/g, '\n');
    expect(src).toContain("if (channel === 'sandbox:envUsage') {\n      return invoke('sandbox_env_usage');");
    expect(src).toContain("if (channel === 'sandbox:envClear') {\n      return invoke('sandbox_env_clear');");
  });

  it('引擎直连走 HTTP 端点，失败抛真实错误码而非静默空值', () => {
    const src = engineApi();
    expect(src).toContain('${ENGINE_URL}/api/sandbox/environment/usage');
    expect(src).toContain('${ENGINE_URL}/api/sandbox/environment/clear');
    expect(src).toContain('SANDBOX_ENV_USAGE_FAILED');
    expect(src).toContain('SANDBOX_ENV_CLEAR_FAILED');
    expect(src).toContain("if (channel === 'sandbox:envUsage') return fetchEnvUsage();");
    // 安全通道在本地实现之前拦截，避免落到 Mock 快照
    expect(src.indexOf('sandbox:envUsage')).toBeLessThan(src.indexOf('handleLocalSecurityChannel(channel, args)'));
  });

  it('Mock 分支如实报不支持（不再返回写死的 1.2 GB 快照）', () => {
    const src = securityLocal();
    expect(src).toContain("if (channel === 'sandbox:envUsage')");
    expect(src).toContain("if (channel === 'sandbox:envClear')");
    expect(src).toContain('function mockEnvUsage()');
    // 编造的演示用量必须彻底消失
    expect(src).not.toContain('1_288_490_188');
    expect(src).not.toContain('files: 1240');
    expect(src).toContain("supported: false");
    expect(src).toContain('SANDBOX_ENV_UNSUPPORTED');
    expect(mockApi()).toContain("channel.startsWith('sandbox:')");
  });

  it('api 封装暴露 sandbox 域（envUsage/envClear）', () => {
    const src = libApi();
    expect(src).toContain('export const sandboxAPI');
    expect(src).toContain('envUsage(): Promise<SandboxEnvUsage>');
    expect(src).toContain('envClear(): Promise<SandboxEnvClearResult>');
    expect(src).toContain('sandbox: sandboxAPI,');
  });
});

// ── 错误码收敛 ───────────────────────────────────────────────────

describe('SANDBOX_ENV_* 错误码文案（Rust/引擎抛出的码不得裸奔）', () => {
  it('源码里出现的每个 SANDBOX_ENV_* 码都有可读文案', () => {
    const codes = rustEnvCodes();
    expect(codes).toContain('SANDBOX_ENV_CLEAR_FAILED');
    expect(codes).toContain('SANDBOX_ENV_PATH_OUT_OF_SCOPE');
    for (const code of codes) {
      expect(Object.values(ErrorCode), `${code} 未登记`).toContain(code as ErrorCode);
      expect(errorMessageOf(code), `${code} 缺文案`).not.toContain('发生错误（');
    }
  });

  it('拒绝详情保留在文案后（盘符冒号不会截断诊断信息）', () => {
    const text = errorTextOf(
      'SANDBOX_ENV_PATH_OUT_OF_SCOPE: 仅允许清理 C:\\app\\workspaces 下的工作空间，收到 C:\\Users\\x',
    );
    expect(text).toContain('安全策略拦截');
    expect(text).toContain('C:\\Users\\x');
  });
});

// ── UI 真实消费 ──────────────────────────────────────────────────

describe('设置页 SandboxPanel 真实化', () => {
  it('写死的 1.2 GB 已删除，改为挂载即拉真实用量', () => {
    const src = settingsPage();
    expect(src).not.toContain('当前占用 1.2 GB');
    expect(src).toContain('sandboxAPI.envUsage()');
    expect(src).toContain('const loadUsage = React.useCallback');
    expect(src).toContain('void loadUsage();');
    expect(src).toContain('formatBytes(usage.bytes)');
  });

  it('清理按钮有真实点击链路：确认弹窗 → envClear → 刷新用量', () => {
    const src = settingsPage();
    expect(src).toContain('sandboxAPI.envClear()');
    expect(src).toContain('setConfirmOpen(true)');
    expect(src).toContain('<Dialog open={confirmOpen}');
    expect(src).toContain('await loadUsage();');
    // MUI 5.16 Button 无 loading 属性，进行中用 startIcon + disabled
    expect(src).toContain('startIcon={clearing ? <CircularProgress size={14} color="inherit" /> : undefined}');
  });

  it('失败可见：清理与统计错误都走 errorTextOf，且有条目未删除时单独提示', () => {
    const src = settingsPage();
    expect(src).toContain("errorTextOf(e, '工作环境文件清理失败')");
    expect(src).toContain("errorTextOf(e, '工作环境用量统计失败')");
    expect(src).toContain('result.failed.length > 0');
  });

  it('没有文件时按钮禁用而非假装可点', () => {
    const src = settingsPage();
    expect(src).toContain('const canClear = Boolean(usage?.supported) && usedBytes > 0 && !clearing;');
    expect(src).toContain('暂无可清理文件');
    // 通道不支持时按钮文案与可点性都如实
    expect(src).toContain("'仅桌面版可用'");
    expect(src).toContain('usage.note ?? ');
  });

  it('Workbench 提交、重新生成与改写提问都把 secureEnv 随请求下发（引擎直连口径）', () => {
    const src = workbenchStore();
    // 三条提交路径共用同一份 settings 快照：secureEnv 与 privacyMode 一起读取
    expect(src.match(/const secureEnv = settings\?\.secureEnv;/g)?.length).toBe(3);
    expect(src.match(/^\s+secureEnv,$/gm)?.length).toBe(3);
    // 三条提交路径都带上当前工作空间：不同 workspace 有独立引擎实例
    expect(src.match(/^\s+workspaceId: getActiveWorkspaceId\(\),$/gm)?.length).toBe(3);
  });
});

// ── 行为：本地通道的能力边界如实反映 ────────────────────────────

async function loadLocal() {
  vi.resetModules();
  return import('../../../src/renderer/lib/security-local');
}

describe('本地 sandbox:env* 通道行为', () => {
  let handle: (channel: string, args: unknown[]) => { handled: boolean; value?: unknown };

  beforeEach(async () => {
    const mod = await loadLocal();
    handle = mod.handleLocalSecurityChannel;
  });

  function call(channel: string, ...args: unknown[]): any {
    const res = handle(channel, args);
    expect(res.handled, `${channel} 应由本地安全实现接管`).toBe(true);
    return res.value;
  }

  it('用量如实报不支持：supported=false 且全为 0，并带可显示的原因', () => {
    const v = call('sandbox:envUsage');
    expect(v.supported).toBe(false);
    expect(v.bytes).toBe(0);
    expect(v.files).toBe(0);
    expect(v.workspaceCount).toBe(0);
    expect(v.workspaces).toEqual([]);
    expect(v.note, '不支持时必须给出原因，否则界面只能瞎猜').toContain('桌面版');
    expect(v.envDirName).toBe('environment');
  });

  it('清理抛 SANDBOX_ENV_UNSUPPORTED，不返回「释放了 N 字节」的假回执', () => {
    expect(() => handle('sandbox:envClear', [])).toThrow(/SANDBOX_ENV_UNSUPPORTED/);
  });

  it('该错误码有登记与可读文案（裸码不丢给用户）', () => {
    expect(Object.values(ErrorCode)).toContain(ErrorCode.SANDBOX_ENV_UNSUPPORTED);
    const text = errorTextOf('SANDBOX_ENV_UNSUPPORTED: 浏览器预览环境无法清理工作环境文件');
    expect(text).toContain('桌面版');
    expect(text).toContain('浏览器预览环境');
  });

  it('审计台账不再有编造的历史样本（清理失败也不留下假的 file_write）', () => {
    expect(() => handle('sandbox:envClear', [])).toThrow(/SANDBOX_ENV_UNSUPPORTED/);
    expect(handle('audit:query', [{ type: 'file_write' }]).value).toEqual([]);
  });
});
