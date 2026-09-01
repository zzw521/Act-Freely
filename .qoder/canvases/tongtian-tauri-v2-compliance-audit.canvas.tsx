import {
  Callout,
  Divider,
  Grid,
  H1,
  H2,
  Stack,
  Stat,
  Table,
  Tag,
  Text,
} from 'qoder/canvas';

export default function TongtianTauriV2ComplianceAudit() {
  return (
    <Stack gap={20}>
      <Stack gap={8}>
        <H1>tongtian-tauri vs SPEC v2.0 符合性审计报告</H1>
        <Text tone="secondary">
          审计对象：d:\qoder\tontianwork\tongtian-tauri 全量代码；基准：docs/spec/spec-通天办公.md v2.0（Tauri 2.x + Rust 全栈核心，Python 仅按需辅助）。
        </Text>
      </Stack>

      <Grid columns={4} gap={16}>
        <Stat value="~25%" label="总体符合度" tone="warning" />
        <Stat value="7" label="重大架构偏差" tone="danger" />
        <Stat value="11" label="改进建议（P0-P2）" />
        <Stat value="18" label="已审计关键文件" />
      </Grid>

      <Divider />

      <H2>成果摘要</H2>
      <Text>
        对 tongtian-tauri 的 Rust 后端（Cargo/lib.rs/bridge 四模块）、Tauri 配置（tauri.conf.json/capabilities）、前端接入层（tauri-api.ts/mock-api.ts）、工程脚本（init.sh/init.ps1/run-dev.ps1）与测试现状完成逐项核对。结论：当前工程是合格的「Tauri 壳 + Python 引擎桥接过渡版」，距 v2.0 目标态（Rust 全栈核心、9 crate workspace）尚处于演进方案阶段 0 之前。
      </Text>

      <H2>关键步骤</H2>
      <Table
        headers={['步骤', '审计内容', '方法']}
        rows={[
          ['1. Rust 后端审计', 'Cargo.toml、lib.rs、bridge/{agent,python_env,sse,mod}.rs', '逐文件通读 + 与 SPEC §3/§5 契约比对'],
          ['2. Tauri 配置审计', 'tauri.conf.json、capabilities/default.json、gen/schemas', '与 SPEC §6.4/§7 比对（CSP、bundle、updater）'],
          ['3. 前端接入层审计', 'tauri-api.ts、mock-api.ts、vite.config.ts、package.json', 'command/event 契约与类型同步核对'],
          ['4. 工程规范审计', 'init.sh、init.ps1、run-dev.ps1、测试文件扫描', '对照开发规范（Mock 开关/init 脚本/测试覆盖）'],
          ['5. 状态复核', '发现 agent.rs 已更新（TT_ENGINE 三模式）后重新核验', '以当前工作区状态为准修正结论'],
        ]}
      />

      <H2>审计范围文件清单</H2>
      <Table
        headers={['层面', '文件', '结论']}
        rows={[
          ['Rust 后端', 'src-tauri/Cargo.toml、src/{lib,main}.rs、src/bridge/*.rs（4 个）', '单 crate 无 workspace；Agent 仍经 Python 子进程'],
          ['Tauri 配置', 'tauri.conf.json、capabilities/default.json', 'CSP 关闭（高危）；capability 最小化（达标）'],
          ['前端', 'package.json、vite.config.ts、src/renderer/lib/{tauri-api,mock-api}.ts', '技术栈符合；契约封装仍按旧 channel 模式'],
          ['脚本', 'init.sh、init.ps1、run-dev.ps1', '幂等/降级逻辑达标；Mock 开关命名分裂'],
          ['测试', '全仓扫描 *.test.ts / #[cfg(test)]', '仅 python_env.rs 5 个单测；前端零测试'],
        ]}
      />

      <Divider />

      <H2>验证证据（主要发现）</H2>
      <Table
        headers={['类别', '发现', '证据位置']}
        rows={[
          ['符合', 'Tauri 2.x 壳 + capability 默认拒绝制 + init 脚本幂等 + release 优化', 'Cargo.toml / default.json / init.sh'],
          ['偏差-架构', '无 Cargo workspace 与 9 crate；Agent 引擎为 Python 子进程（默认自研 AgentScope 引擎，TT_ENGINE 可切 qwenpaw/mock），非 v2.0 要求的 Rust 原生', 'agent.rs L129-165'],
          ['偏差-契约', '仅 5 个 command（SPEC 要求 32 个）；事件名 chat-event 而非 agent://event；载荷 camelCase；无 AppError/ErrorCode', 'lib.rs / agent.rs L33'],
          ['偏差-安全', 'csp: null 完全关闭；无 keyring/tracing/审计；子进程管理粗糙', 'tauri.conf.json L24-26'],
          ['偏差-规范', 'Mock 开关三套命名（TT_AGENT_MOCK/VITE_MOCK/MOCK_ENABLED）；chat_review/chat_control 前端假实现', 'agent.rs / init.sh / tauri-api.ts L55-56'],
          ['偏差-测试', '仅 python_env.rs 存在 5 个单元测试；核心 SSE 解析/状态机无覆盖；前端无 vitest 用例', 'python_env.rs L198-239 / 全仓扫描'],
        ]}
        rowTone={[undefined, 'danger', 'danger', 'danger', 'warning', 'warning']}
      />

      <H2>改进建议（优先级排序）</H2>
      <Table
        headers={['优先级', '建议', '对应 SPEC']}
        rows={[
          ['P0', '启用 CSP 严格策略；落地 AppError/ErrorCode 统一错误结构；去除 chat_review/chat_control 假实现；统一 Mock 开关为 MOCK_ENABLED', '§6.4 / §5.3'],
          ['P1', 'src-tauri 改造为 Cargo workspace，先落 crates/storage（rusqlite+keyring）与 crates/model-hub；事件/载荷对齐（agent://event + snake_case + ts-rs）；现有 Python 桥接收敛为 crates/python-bridge 过渡形态', '§3.0 / §3.2 / §5.1'],
          ['P2', '补 SSE 解析/健康巡检/端口分配单测与前端 vitest；归档 src/main、src/agent 旧资产；productName 改「通天办公」；配置 updater', '§8 / §7'],
        ]}
      />

      <Callout tone="warning" title="最终结论">
        <Stack gap={6}>
          <Text>
            tongtian-tauri 当前形态 = Tauri 2.x 壳（符合）+ Python Agent 引擎桥接（v1.x 模式，与 v2.0 目标态方向相反但已向自研 AgentScope 引擎演进）。
          </Text>
          <Text>
            建议按《Electron → Tauri 架构演进方案》§5 的阶段划分执行 /apex-sdd:plan 任务分解：先 P0 安全项与 crates/storage，再逐 crate 替换 Python 桥接，最终达成 Rust 全栈核心目标态。
          </Text>
        </Stack>
      </Callout>

      <Stack gap={4}>
        <Tag tone="info">SPEC v2.0 合规审计</Tag>
        <Text tone="secondary" size="small">
          审计时间：2026-08-05 · 只读审计任务，未修改任何代码，无运行时截图。
        </Text>
      </Stack>
    </Stack>
  );
}
