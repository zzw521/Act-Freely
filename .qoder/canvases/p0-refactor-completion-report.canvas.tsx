import {
  Callout,
  CollapsibleSection,
  Divider,
  Grid,
  H1,
  H2,
  Stack,
  Stat,
  Table,
  Text,
  Timeline,
} from 'qoder/canvas';

export default function P0RefactorCompletionReport() {
  return (
    <Stack gap={20}>
      <Stack gap={6}>
        <H1>通天办公 · 三层架构演进 P0 改造完成报告</H1>
        <Text tone="secondary">
          依据 docs/changes/agent-platform-evolution 需求分析报告，在 tongtian-tauri 落地桌面端侧 6 项 P0
          契约与机制（14 态状态机 / 9 错误码 / lease 租约 / SSE seq 重放与 24h 持久化 / 四档配额闸门 /
          传输适配层），契约先行 + 附加式扩展，现有 5 个 command 签名零破坏。
        </Text>
      </Stack>

      <Grid columns={4} gap={12}>
        <Stat value="53" label="Rust 测试通过（默认 feature）" tone="success" />
        <Stat value="105" label="TS 测试通过（含集成）" tone="success" />
        <Stat value="13" label="引擎 SSE 自测项 PASS" tone="success" />
        <Stat value="11" label="Rust command（5 → 11）" />
      </Grid>

      <Divider />

      <H2>成果摘要</H2>
      <Table
        headers={['P0 项', '落地内容', '状态']}
        rows={[
          ['14 态状态机', '35 条合法转移纯函数表 + wait_reason 子态 + retry guard；Rust 任务注册表承接', '完成'],
          ['9 个云端错误码', 'BUDGET_EXHAUSTED / QUEUE_TIMEOUT / LEASE_LOST / FILE_REJECTED / SYNC_CONFLICT / PAYMENT_GRACE / DATA_TIER_BLOCKED / INJECTION_BLOCKED / QUOTA_EXCEEDED + UI 文案表', '完成'],
          ['lease 租约协议', '心跳 10s / 租约 30s / 超时回收 60s / 空闲释放 5min；单 interval 扫描；禁止抢占', '完成'],
          ['SSE seq + 重放', '引擎单调 seq + id 行；Rust 解析/去重/gap 修复；断线重放补齐；事件 JSONL 24h 持久化', '完成'],
          ['配额预算闸门', '70/85/95/100 四档，降级→排队→阻断；100% 新提交 QUOTA_EXCEEDED、运行中转 budget_paused', '完成'],
          ['传输适配层', 'TransportProvider 三实现（Tauri / Mock / BFF 占位），云端切换契约零改动', '完成'],
        ]}
      />

      <H2>关键实施步骤</H2>
      <Timeline
        events={[
          {
            title: '流 A · 契约冻结',
            description: 'types.ts 14 态 / WaitReason / seq / TaskMeta；contracts.ts 9 错误码、6 新 channel、LeaseInfo / QuotaSnapshot；契约快照门禁测试',
            tone: 'success',
          },
          {
            title: '流 B · 14 态状态机',
            description: 'task-state-machine.ts（35 转移）+ Rust TaskRegistry + chat_cancel / task_state command',
            tone: 'success',
          },
          {
            title: '流 C · Python 引擎',
            description: 'seq + id 行、生产时记录（RecordingQueue 保障断线零丢失）、/api/events/replay、/api/chat/cancel、批量 drain、keepalive',
            tone: 'success',
          },
          {
            title: '流 D · Rust 可靠性管道',
            description: 'sse.rs 解析 id/seq（TT_ENABLE_SEQ feature 包裹）；agent.rs 独立 emit 任务 + 去重 + gap 重放 + 断线补齐；event_store.rs 异步批量刷盘',
            tone: 'success',
          },
          {
            title: '流 E/F · 租约与配额',
            description: 'lease.rs 单 interval 扫描（LEASE_LOST → executing→queued）；quota.rs 本地台账 + budget_paused 联动（转移 #17）',
            tone: 'success',
          },
          {
            title: '流 G · 前端承接',
            description: 'transport.ts / event-stream-guard.ts / tauri-api + mock-api 全真模拟 / Workbench 配额预警条 + 状态成因 + 取消按钮',
            tone: 'success',
          },
          {
            title: '流 H · 集成验收',
            description: 'mock-agent.py burst/broken/quota 触发器；500 事件突发 seq 零丢失集成测试；黄金 SSE fixture 双侧同源；引擎自测脚本',
            tone: 'success',
          },
        ]}
      />

      <H2>主要变更文件</H2>
      <CollapsibleSection title="共享契约与纯逻辑层（src/shared）" defaultOpen>
        <Table
          headers={['文件', '变更']}
          rows={[
            ['types.ts', 'TaskStatus 14 态、WaitReason、ChatEvent seq/taskId、TaskMeta'],
            ['contracts.ts', '9 错误码、6 新 IPC channel、LeaseInfo / LeaseTakeoverResp / QuotaSnapshot、TaskSummary.meta'],
            ['task-state-machine.ts', '新建：35 条转移表 + IllegalTransitionError + 子态管理'],
            ['quota-gate.ts', '新建：四档评估 / 动作链 / 档位去抖纯函数'],
            ['error-messages.ts', '新建：错误码 → 用户文案映射（未知码降级）'],
          ]}
        />
      </CollapsibleSection>
      <CollapsibleSection title="Rust 侧（src-tauri）" defaultOpen>
        <Table
          headers={['文件', '变更']}
          rows={[
            ['Cargo.toml', '新增 tt-enable-seq feature（默认开启，回滚保险）'],
            ['src/lib.rs', 'P0Services 装配 + 6 个新 command + 租约扫描循环 + event_replay'],
            ['src/bridge/sse.rs', 'id 行 / seq 双通道解析、status_change / quota_warning 适配、黄金 fixture 测试'],
            ['src/bridge/agent.rs', 'P0Context、独立 emit 任务（背压）、seq 去重 / gap 重放、断线补齐、cancel_task、budget_paused 联动'],
            ['src/bridge/event_store.rs', '新建：JSONL 异步批量刷盘 + 双上限重放窗口 + 24h TTL'],
            ['src/task/registry.rs · lease.rs · quota.rs · mod.rs', '新建：任务注册表 / 租约管理器 / 配额闸门（含 21 项内联测试）'],
          ]}
        />
      </CollapsibleSection>
      <CollapsibleSection title="Python 引擎与前端（engine / src/renderer）">
        <Table
          headers={['文件', '变更']}
          rows={[
            ['engine/server.py', 'TaskSession + RecordingQueue + seq/id 行 + replay/cancel 端点 + 批量 drain + keepalive'],
            ['src/renderer/lib/transport.ts', '新建：TransportProvider 三实现 + 环境探测'],
            ['src/renderer/lib/event-stream-guard.ts', '新建：per-task seq 去重 / gap 检测 / status_change 幂等'],
            ['src/renderer/lib/tauri-api.ts', '6 个新 command 映射 + seq 守卫接入'],
            ['src/renderer/lib/mock-api.ts', '14 态流 / seq / quota_warning / cancel 全真模拟'],
            ['src/renderer/lib/api.ts', 'p0API 附加式封装（NOT_IMPLEMENTED 降级）'],
            ['src/renderer/store/workbenchStore.ts', 'status_change / quota_warning 分发、taskMeta / quotaSnapshot、cancelTask'],
            ['src/renderer/pages/Workbench/index.tsx', '配额预警条、14 态状态文案、取消按钮'],
          ]}
        />
      </CollapsibleSection>
      <CollapsibleSection title="测试与脚本（tests / scripts）">
        <Table
          headers={['文件', '变更']}
          rows={[
            ['tests/unit/shared/contract.test.ts', '契约快照门禁（含 ChatEvent 双定义一致性类型断言）'],
            ['tests/unit/agent/state-machine-14.test.ts', '35 转移逐条 + 非法转移 + retry guard + 子态'],
            ['tests/unit/model/quota-gate.test.ts', '四档边界 / 动作链 / 去抖 / 错误码联动'],
            ['tests/unit/shared/error-messages.test.ts', '9 云端错误码文案全覆盖 + 降级展示'],
            ['tests/unit/bridge/event-guard.test.ts', 'seq 去重 / gap / 幂等 / wrap'],
            ['tests/integration/mock-agent.integration.test.ts', '真实拉起 mock-agent：burst 501 事件 seq 连续、replay 过滤、cancel'],
            ['tests/integration/engine_sse_selftest.py', '引擎 13 项契约自测（seq/id/重放/取消/Last-Event-ID）'],
            ['scripts/mock-agent.py', 'seq/id 行 + replay/cancel 端点 + burst/broken/quota 触发器'],
            ['vitest.config.ts', '新建：测试基建（unit + integration）'],
          ]}
        />
      </CollapsibleSection>

      <H2>验证证据</H2>
      <Table
        headers={['验证项', '方式', '结果']}
        rows={[
          ['Rust 单测', 'cargo test --lib（默认 feature / --no-default-features 两态）', '53 通过 / 51 通过，0 失败'],
          ['TS 单测 + 集成', 'npx vitest run（6 个测试文件，含真实拉起 mock-agent）', '105 通过'],
          ['引擎 SSE 自测', 'python tests/integration/engine_sse_selftest.py', '13 项 PASS'],
          ['类型检查', 'npx tsc --noEmit', '零错误'],
          ['可靠性验收（事件丢失=0）', '集成测试 burst 501 事件 seq 1..501 连续无重复 + replay from_seq 过滤', '通过'],
          ['Mock 开关（开发规范）', '浏览器实测 VITE_MOCK 模式：8 个 P0 方法、seq 1-10 事件流、取消按钮、状态栏', '通过'],
        ]}
      />

      <Callout tone="success" title="最终结果">
        计划文件 8 条任务流（A-H）与全部验收要点落地完成：契约冻结（快照门禁）、SSE 断线重放事件丢失=0、
        状态机 35 转移全覆盖、配额四档与 budget_paused 全链路、Mock 开关合规。Rust command 由 5 个扩展至
        11 个且存量签名零破坏；云端切换仅需替换 TransportProvider，契约零改动。
      </Callout>

      <Stack gap={4}>
        <Text tone="secondary" size="small">
          说明：本次浏览器取证期间截图工具持续超时（5 次），未生成截图文件，故本报告不含图片；
          UI 验证以浏览器 a11y 快照与交互脚本结果为证据。
        </Text>
        <Text tone="secondary" size="small">
          依据：三层架构P0改造计划_task-495.md · docs/changes/agent-platform-evolution/requirements-analysis-report.md
        </Text>
      </Stack>
    </Stack>
  );
}
