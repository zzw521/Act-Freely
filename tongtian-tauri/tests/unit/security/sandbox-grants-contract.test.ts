/**
 * 受控命令执行/联网契约测试（执行面 P1）
 *
 * 这条能力最容易变成「开关点了、提示词写了、其实没约束」：
 * - Rust：授权字段默认关闭、命令名/主机归一化与边界校验；通用 shell 依赖命令执行；
 *   runtime 原样下发
 * - 引擎：未授权/白名单为空不注册工具；argv 执行不经 shell，通用 shell 另行显式授权；
 *   工作目录固定；超时/输出上限；环境变量裁剪；联网白名单 + 公网地址 + 重定向逐跳复核
 * - 审计：成功/失败/超时/拒绝都发 tool_audit，Rust 归一动作码落库；
 *   审计 target 不带 URL 凭据/查询串，命令参数不原样入库
 * - 前端：显式开关 + 空白名单拒绝保存 + 新增授权二次确认 + 浏览器通道如实禁用
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');
}

const rustPolicy = () => read('../../../src-tauri/src/security/policy.rs');
const rustRuntime = () => read('../../../src-tauri/src/bridge/runtime.rs');
const rustAudit = () => read('../../../src-tauri/src/security/audit.rs');
const rustSse = () => read('../../../src-tauri/src/bridge/sse.rs');
const rustAgent = () => read('../../../src-tauri/src/bridge/agent.rs');
const engineServer = () => read('../../../engine/server.py');
const engineGrants = () => read('../../../engine/tool_grants.py');
// #164 接缝：授权件的条件注册搬进 exec 车道，开关在 toolkit_lanes.SWITCH_BOARD 里声明
const engineToolkitLanes = () => read('../../../engine/toolkit_lanes.py');
const engineToolkitExec = () => read('../../../engine/toolkit_exec.py');
const sharedTypes = () => read('../../../src/shared/types.ts');
const sandboxForm = () => read('../../../src/renderer/components/SandboxPolicy/index.tsx');
const auditLog = () => read('../../../src/renderer/components/AuditLog/index.tsx');
const securityLocal = () => read('../../../src/renderer/lib/security-local.ts');

describe('Rust：授权字段默认关闭且边界归一化', () => {
  it('执行、通用 shell、联网字段都存在且 serde 缺省为关闭/空', () => {
    const src = rustPolicy();
    for (const field of [
      'exec_enabled', 'exec_allowlist', 'shell_enabled', 'network_enabled', 'network_allowlist',
    ]) {
      expect(src).toContain(field);
    }
    expect(src).toContain('pub exec_enabled: bool');
    expect(src).toContain('pub shell_enabled: bool');
    expect(src).toContain('pub network_enabled: bool');
    // 老策略 JSON 没有这些键时必须默认关闭，不能因为反序列化失败而放开
    expect(src.match(/#\[serde\(default\)\]/g)?.length).toBeGreaterThanOrEqual(5);
  });

  it('通用 shell 必须建立在命令执行授权之上，只开 shell 拒绝保存', () => {
    const src = rustPolicy();
    expect(src).toContain('if policy.shell_enabled && !policy.exec_enabled');
    expect(src).toContain('POLICY_INVALID_SHELL');
    expect(src).toContain('shell_enabled: policy.shell_enabled');
    // 旧策略行没有 shellEnabled 键：按关闭处理，不能判成损坏
    expect(src).toContain('remove("shellEnabled")');
    expect(src).toContain('!read.shell_enabled');
  });

  it('命令名只接受裸名称，拒绝路径与 shell 元字符', () => {
    const src = rustPolicy();
    expect(src).toContain('fn normalize_command');
    expect(src).toContain('is_ascii_alphanumeric');
    expect(src).toContain("matches!(c, '_' | '-' | '.' | '+')");
    expect(src).toContain('POLICY_INVALID_COMMAND');
    expect(src).toContain('MAX_EXEC_COMMANDS');
  });

  it('主机白名单复用域名归一化，非法主机拒绝', () => {
    const src = rustPolicy();
    expect(src).toContain('normalize_domains');
    expect(src).toContain('POLICY_INVALID_HOST');
    expect(src).toContain('MAX_NETWORK_HOSTS');
  });

  it('runtime sandbox 段真实下发全部授权字段（默认关闭 + 存储值）', () => {
    const src = rustRuntime();
    expect(src).toContain('"execEnabled": false');
    expect(src).toContain('"shellEnabled": false');
    expect(src).toContain('"networkEnabled": false');
    expect(src).toContain('"execAllowlist": p.exec_allowlist');
    expect(src).toContain('"shellEnabled": p.shell_enabled');
    expect(src).toContain('"networkAllowlist": p.network_allowlist');
  });
});

describe('引擎：未授权不注册，执行与联网都被约束', () => {
  it('默认 toolkit 不含执行/联网工具，授权条件满足才 install', () => {
    const src = engineGrants();
    // D-08（#144）：过去这三条条件直接写在 install 的 if 里，界面想显示「这个扩展这一轮
    // 到底能不能用」就得再抄一份——两处各抄一套迟早各说一套。现在条件搬进
    // registration_plan() 单点，install 只读结论。三条条件一条没少：
    //   命令执行 = 用户授权 ∧ 白名单非空；通用 shell = 命令执行授权 ∧ shell 授权；联网 = 授权 ∧ 白名单非空。
    expect(src).toContain('def registration_plan(self) -> dict:');
    expect(src).toContain('if not self.exec_enabled:');
    expect(src).toContain('elif not self.exec_allowlist:');
    expect(src).toContain('elif not self.shell_enabled:');
    expect(src).toContain('if not self.network_enabled:');
    expect(src).toContain('elif not self.network_allowlist:');
    expect(src).toContain('plan = self.registration_plan()');
    expect(src).toContain('if plan["run_command"]["registered"]:');
    expect(src).toContain('if plan["run_shell"]["registered"]:');
    expect(src).toContain('if plan["fetch_url"]["registered"]:');
    // D-08 新增的一条：系统隔离已启用但容器起不来 ⇒ 执行面同样不注册（联网走进程内 urllib，不受影响）
    expect(src).toContain('exec_blocked = container["enabled"] and not container["ready"]');
    expect(src).toContain('command = item(False, container["reason"])');
    expect(src).toContain('shell = item(False, container["reason"])');
    expect(src).toContain('net = item(True, "")');
    // 「不注册」必须是真的不进工具面，而不是注册进去再拒绝
    expect(src).toContain('toolkit.register_tool_function');
    // install 里不许再各写一份开关判定（判定只有一处，界面与注册读同一份）
    const installBody = src.slice(src.indexOf('def install('), src.indexOf('def _exec_tool('));
    expect(installBody).toContain('self.registration_plan()');
    expect(installBody).not.toContain('self.exec_allowlist');
    expect(installBody).not.toContain('self.shell_enabled');
    expect(installBody).not.toContain('self.network_allowlist');
    // server 侧只有显式传入 grants 才会调用 install
    const server = engineServer();
    expect(server).toContain('if grants is not None:');
    // #164：注册动作在 exec 车道里，开关（grants）仍在 toolkit_lanes.SWITCH_BOARD 单点声明；
    // 判据不变——没有 grants 就没有工具，锁的是同一条「未授权不注册」。
    expect(engineToolkitExec()).toContain('if ctx.grants is not None:');
    expect(engineToolkitExec()).toContain(
      'ctx.grants.install(ctx.toolkit, ctx.base, emit=ctx.emit, task_id=ctx.task_id)',
    );
    expect(engineToolkitLanes()).toContain('"exec": ("grants",)');
    expect(server).not.toContain('grants.install(toolkit, base, emit=emit, task_id=task_id)');
  });

  it('命令执行：argv 数组、shell=False、固定工作目录、超时与输出上限', () => {
    const src = engineGrants();
    // D-08：argv 的组装从调用点搬进 container_wrap（未启用隔离时逐字等价于原来的
    // `[executable, *args]`）。这条测试要钉的从来不是那个字面量，而是三件事：
    // ①调用点把**程序与参数分开**交出（不是拼好的字符串）；
    // ②组装出来的是列表，元素不被改写；③交给 Popen 时 shell=False。
    expect(src).toContain('argv, container_note = container_wrap(executable, args, str(base))');
    expect(src).toContain('argv = [program, *[str(a) for a in args]]');
    expect(src).toContain('return [exe, *launcher_argv], note');
    expect(src).toContain('shell=False');
    expect(src).toContain('_execute_limited(argv, str(base), timeout)');
    // 容器起不来 ⇒ 当次拒绝，不许降级成宿主权限的裸跑
    expect(src).toContain('except sl.SandboxUnavailable as exc:');
    expect(src).toContain('为避免以宿主权限执行已拒绝运行');
    expect(src).toContain('cwd=cwd');
    expect(src).toContain('MAX_EXEC_TIMEOUT = 120');
    expect(src).toContain('MAX_EXEC_OUTPUT_CHARS = 8000');
    expect(src).toContain('timeout = min(max(timeout, 1), MAX_EXEC_TIMEOUT)');
    // 有 CREATE_NO_WINDOW，避免 Windows 弹黑框
    expect(src).toContain('CREATE_NO_WINDOW');
  });

  it('通用 shell：双重授权、固定工作目录、超时/长度上限、审计只记哈希', () => {
    const src = engineGrants();
    // 必须同时开启 exec 与 shell；壳侧漏传 shellEnabled 时按关闭处理
    expect(src).toContain('shell_enabled=block.get("shellEnabled") is True');
    expect(src).toContain('用户未授权命令执行（受控执行默认关闭），通用 shell 也不可用');
    expect(src).toContain('用户未授权通用 shell（默认关闭）');
    expect(src).toContain('def _shell_target(command: str) -> str:');
    expect(src).toContain('hashlib.sha256');
    // 命令正文不进审计，只进长度与摘要
    expect(src).toContain('f"shell（{len(raw)} 字符，sha256:{digest}）"');
    expect(src).toContain('"run_shell", target');
    expect(src).not.toContain('"run_shell", raw');
    expect(src).not.toContain('f"run_shell：{raw}');
    // Windows 交给 cmd，其他平台交给 /bin/sh；工作目录仍是任务目录。
    // Windows 这一支必须把整条命令行拼成**字符串**交给 cmd：给 Popen 一个列表时
    // Python 按 MSVCRT 规则把 `"` 转义成 `\"`，而 cmd.exe 不吃这套转义、原样收到
    // 反斜杠——带引号的命令会静默空跑（`python -c "print(42)"` 退出码 0、无输出）。
    // D-08 接线时最容易把这个坑重新挖开（套进容器后交出去的是启动器的真 argv 列表），
    // 所以除了钉字符串形状，还要钉「容器那一支用 --raw-command-line 保住同一份命令行」。
    expect(src).toContain("list2cmdline([shell, '/d', '/s', '/c'])");
    expect(src).toContain('tail = f\'"{raw}"\'');
    expect(src).toMatch(/argv: object = f".*list2cmdline.*\{tail\}/);
    expect(src).not.toContain('argv = [shell, "/d", "/s", "/c", raw]');
    expect(src).toContain('shell = "/bin/sh"');
    expect(src).toContain('argv = [shell, "-c", raw]');
    expect(src).toContain('_execute_limited(argv, str(base), timeout)');
    // 不进容器时 argv 原样交出（字符串），进容器时逐字把同一条命令行带进去
    expect(src).toContain('return plain, ""');
    expect(src).toContain('shell_args = ["/d", "/s", "/c", tail]');
    expect(src).toContain('raw_command_line=raw_line');
    expect(src).toContain('raw_line = True');
    expect(src).toContain('raw_line = False');
    expect(src).toContain('MAX_SHELL_COMMAND_CHARS = 8000');
    expect(src).toContain('timeout = min(max(timeout, 1), MAX_EXEC_TIMEOUT)');
    expect(src).toContain('shell 命令超过');
    // 主会话的 shell 禁睡等（DSH 口径「等待是排队，不是轮询」）：判定**借**执行体那张表，
    // 钉的是调用形状（注释里也出现函数名，所以只钉带实参的那一行）与面向模型的口径。
    expect(src).toContain('sleep_label = twa.find_sleep_pattern(raw)');
    expect(src).toContain('等待靠排队不靠轮询');
    expect(src).toContain('shell 里禁用睡等');
  });

  it('通用 shell 进入逐次确认，L0 只读沙箱拒绝所有执行工具', () => {
    const approval = read('../../../engine/tool_approval.py');
    expect(approval).toContain('"run_shell"');
    expect(approval).toContain('shell 命令：');
    const sandbox = read('../../../engine/sandbox_runtime.py');
    expect(sandbox).toContain('EXECUTION_TOOLS = frozenset({"run_command", "run_shell"})');
    expect(sandbox).toContain('if self.execution_level == "L0" and name in EXECUTION_TOOLS:');
  });

  it('子进程环境变量按白名单裁剪，不继承引擎秘密', () => {
    const src = engineGrants();
    expect(src).toContain('ENV_KEEP');
    expect(src).toContain('if k.upper() in ENV_KEEP');
    // 注释与实现都不能允许任意 *_KEY/*_TOKEN 透传
    expect(src).not.toContain('"*_KEY"');
    expect(src).not.toContain('"*_TOKEN"');
  });

  it('系统资源沙箱：Windows Job Object 约束整棵进程树，Unix 用 rlimit 尽力而为', () => {
    const src = engineGrants();
    expect(src).toContain('EXEC_MEMORY_LIMIT_MB = 2048');
    expect(src).toContain('EXEC_PROCESS_LIMIT = 64');
    expect(src).toContain('CreateJobObjectW');
    expect(src).toContain('SetInformationJobObject');
    expect(src).toContain('AssignProcessToJobObject');
    expect(src).toContain('JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE');
    expect(src).toContain('_CREATE_SUSPENDED');
    // 先挂 Job 再恢复：避免「命令已跑起来才挂进去」的竞态
    expect(src).toContain('NtResumeProcess');
    expect(src).toContain('TerminateJobObject');
    // Unix 只有内存上限 + 进程组终止；进程数上限不假装等价
    expect(src).toContain('RLIMIT_AS');
    expect(src).toContain('os.killpg(proc.pid, signal.SIGKILL)');
    expect(src).toContain('进程数上限在 Unix 不做');
    // 限制没生效时必须如实说明，并写进审计 detail
    expect(src).toContain('系统资源限制未生效');
    expect(src).toContain("limits={'degraded' if limits_note else 'on'}");
    // 提示词也要按平台说明真实边界
    expect(src).toContain('Job Object 约束');
    expect(src).toContain('内存上限');
  });

  it('联网：只允许 http/https、拒绝 userinfo、精确主机匹配、DNS 全量公网校验', () => {
    const src = engineGrants();
    expect(src).toContain('ALLOWED_SCHEMES = ("http", "https")');
    expect(src).toContain('parts.username or parts.password');
    expect(src).toContain('if host not in self.network_allowlist:');
    expect(src).toContain('blocked = [a for a in addrs if not _is_public_ip(a)]');
    expect(src).toContain('ip.is_private');
    expect(src).toContain('ip.is_loopback');
    expect(src).toContain('ip.is_link_local');
    expect(src).toContain('ip.is_reserved');
  });

  it('重定向逐跳复核且限制次数，最终地址再兜底校验', () => {
    const src = engineGrants();
    expect(src).toContain('class _CheckedRedirect');
    expect(src).toContain('max_redirections = MAX_REDIRECTS');
    expect(src).toContain('self._validate(newurl)');
    expect(src).toContain('validate(final_url)');
  });

  it('响应按字节与字符双重截断，非文本内容拒绝', () => {
    const src = engineGrants();
    expect(src).toContain('MAX_FETCH_BYTES = 2 * 1024 * 1024');
    expect(src).toContain('MAX_FETCH_CHARS = 20000');
    expect(src).toContain('resp.read(MAX_FETCH_BYTES + 1)');
    expect(src).toContain('TEXT_CONTENT_PREFIXES');
    expect(src).toContain('不是文本');
  });
});

describe('引擎接线：授权解析、提示词与可见说明', () => {
  it('server 解析 runtime.sandbox 并把说明并入上下文', () => {
    const src = engineServer();
    expect(src).toContain('import tool_grants as tg');
    expect(src).toContain('grants = tg.ToolGrantPolicy.from_runtime(session.runtime)');
    expect(src).toContain('*sandbox.notes(), *grants.notes()');
    // 未授权时 build_agent 不追加工具提示
    expect(src).toContain('if grants is not None:');
    expect(src).toContain('sys_prompt += grants.system_hint()');
  });

  it('只有父 Agent 获得授权工具，子 Agent 不继承 shell/联网面', () => {
    const src = engineServer();
    const child = src.split('def _make_child_agent')[1].split('subagent_runner = sar.SubagentRunner')[0];
    expect(child).not.toContain('grants=grants');
    expect(child).not.toContain('emit=lambda');
    const parent = src.split('agent = build_agent(')[1].split('except Exception as exc:')[0];
    expect(parent).toContain('grants=grants');
    expect(parent).toContain('task_id=task_id');
  });

  it('每次调用都发 tool_audit，审计不含凭据/参数原文', () => {
    const src = engineGrants();
    expect(src).toContain('emit("tool_audit"');
    expect(src).toContain('_audit_url');
    expect(src).toContain('用户信息、查询串与片段可能带凭据');
    expect(src).toContain('参数里可能被模型塞入凭据');
    // 超时也必须有审计，而不是只返回错误文本
    expect(src).toContain('"timeout", detail');
  });
});

describe('审计链路：引擎事件 → Rust 动作码', () => {
  it('两个动作码已登记，SSE 事件类型放行', () => {
    expect(rustAudit()).toContain('pub const ACTION_EXEC_RUN: &str = "exec_run";');
    expect(rustAudit()).toContain('pub const ACTION_NETWORK_FETCH: &str = "network_fetch";');
    expect(rustSse()).toContain('"tool_audit"');
  });

  it('Rust 按工具名归一动作码，拒绝/失败/超时降为 warning', () => {
    const src = rustAgent();
    expect(src).toContain('ev.kind == "tool_audit"');
    expect(src).toContain('ACTION_EXEC_RUN');
    expect(src).toContain('ACTION_NETWORK_FETCH');
    // 通用 shell 与受控命令执行共用动作码，但审计入口和落库规则一致
    expect(src).toContain('tool == "run_shell"');
    expect(src).toContain('matches!(outcome, "ok" | "")');
    expect(src).toContain('SEV_WARNING');
    // 落库走统一脱敏入口，而非直接拼 SQL
    expect(src).toContain('audit::record_quiet');
  });

  it('审计页把新动作显示成中文标签', () => {
    const src = auditLog();
    expect(src).toContain("exec_run: '💻 命令执行'");
    expect(src).toContain("network_fetch: '🌐 联网获取'");
  });
});

describe('前端：显式授权与诚实边界', () => {
  it('SandboxPolicy 类型包含执行、通用 shell 与联网授权字段', () => {
    const src = sharedTypes();
    for (const field of [
      'execEnabled', 'execAllowlist', 'shellEnabled', 'networkEnabled', 'networkAllowlist',
    ]) {
      expect(src).toContain(field);
    }
  });

  it('表单默认关闭、空白名单拒绝保存、新增授权二次确认', () => {
    const src = sandboxForm();
    expect(src).toContain('execEnabled: initialPolicy?.execEnabled ?? false');
    expect(src).toContain('networkEnabled: initialPolicy?.networkEnabled ?? false');
    expect(src).toContain('shellEnabled: (initialPolicy?.execEnabled ? initialPolicy?.shellEnabled : false) ?? false');
    expect(src).toContain('命令白名单为空：请先添加至少一个命令');
    expect(src).toContain('主机白名单为空：请先添加至少一个主机');
    expect(src).toContain('setConfirmOpen(true)');
    expect(src).toContain('确认授权并保存');
    expect(src).toContain('每次调用都会写入审计日志');
  });

  it('通用 shell 开关标注高风险，关掉命令执行时同步关闭', () => {
    const src = sandboxForm();
    expect(src).toContain('允许通用 shell（高风险）');
    expect(src).toContain('shellEnabled: e.target.checked ? policy.shellEnabled : false');
    expect(src).toContain('通用 shell 依赖命令执行授权');
    // 确认框必须把「任意命令」的风险说清楚，而不是只写 shell
    expect(src).toContain('执行任意 shell 命令');
    expect(src).toContain('npm install &amp;&amp; npm run build');
  });

  it('浏览器通道禁用执行/联网开关并说明原因', () => {
    const src = sandboxForm();
    expect(src).toContain('const desktop = isTauri();');
    expect(src).toContain('disabled={!desktop}');
    expect(src).toContain('执行/联网授权仅在桌面版生效');
  });

  it('命令名与主机在前端先做同边界归一化', () => {
    const src = sandboxForm();
    expect(src).toContain('const COMMAND_NAME_RE');
    expect(src).toContain('function normalizeHost');
    expect(src).toContain('不能带路径、空格或 shell 元字符');
  });
});

describe('本地 Mock 通道同边界（浏览器预览不假装能授权）', () => {
  let handle: (channel: string, args: unknown[]) => { handled: boolean; value?: unknown };

  beforeEach(async () => {
    vi.resetModules();
    const mod = await import('../../../src/renderer/lib/security-local');
    handle = mod.handleLocalSecurityChannel;
  });

  function call(channel: string, ...args: unknown[]): any {
    const res = handle(channel, args);
    expect(res.handled, `${channel} 应由本地安全实现接管`).toBe(true);
    return res.value;
  }

  const base = {
    workspaceId: 'ws-grants',
    executionLevel: 'L1',
    fileAllowlist: [],
    fileBlocklist: [],
    toolAllowlist: [],
    toolBlocklist: [],
    maxCpuPercent: 50,
    maxMemoryMB: 512,
    maxDiskWriteMbps: 10,
    execEnabled: false,
    execAllowlist: [],
    shellEnabled: false,
    networkEnabled: false,
    networkAllowlist: [],
  };

  it('保存时归一化命令与主机，并按真实值回读（含通用 shell）', () => {
    const saved = call('sandbox:updatePolicy', {
      ...base,
      execEnabled: true,
      execAllowlist: [' Python ', 'git', 'python'],
      shellEnabled: true,
      networkEnabled: true,
      networkAllowlist: ['https://API.Example.com/v1', 'example.com:443'],
    });
    expect(saved.execAllowlist).toEqual(['python', 'git']);
    expect(saved.shellEnabled).toBe(true);
    expect(saved.networkAllowlist).toEqual(['api.example.com', 'example.com']);
    expect(call('sandbox:getPolicy', 'ws-grants').execEnabled).toBe(true);
    // 旧的本地策略（无授权字段）仍可读取，不伪造为已开启
    expect(call('sandbox:getPolicy', 'ws-missing')).toBeNull();
  });

  it('只开通用 shell 不开命令执行被拒（POLICY_INVALID_SHELL）', () => {
    expect(() => call('sandbox:updatePolicy', {
      ...base,
      shellEnabled: true,
    })).toThrow(/POLICY_INVALID_SHELL/);
  });

  it('带路径命令被拒（POLICY_INVALID_COMMAND）', () => {
    expect(() => call('sandbox:updatePolicy', {
      ...base,
      execAllowlist: ['C:\\Windows\\System32\\cmd.exe'],
    })).toThrow(/POLICY_INVALID_COMMAND/);
  });

  it('空主机被拒（POLICY_INVALID_HOST）', () => {
    expect(() => call('sandbox:updatePolicy', {
      ...base,
      networkAllowlist: ['   '],
    })).toThrow(/POLICY_INVALID_HOST/);
  });

  it('授权保存会写审计，且记录白名单项数而非内容（含通用 shell）', () => {
    call('sandbox:updatePolicy', {
      ...base,
      execEnabled: true,
      execAllowlist: ['python'],
      shellEnabled: true,
      networkEnabled: true,
      networkAllowlist: ['example.com'],
    });
    const logs = call('audit:query', [{ action: 'config_change', limit: 5 }]);
    expect(logs.length).toBeGreaterThan(0);
    expect(logs[0].details).toContain('命令执行=开启（1 项）');
    expect(logs[0].details).toContain('通用shell=开启');
    expect(logs[0].details).toContain('联网=开启（1 项）');
  });
});
