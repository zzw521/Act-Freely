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

export default function CoreLoopCompletionReport() {
  return (
    <Stack gap={20}>
      <Stack gap={6}>
        <H1>通天办公 · 核心闭环补齐 完成报告</H1>
        <Text tone="secondary">
          依据《核心闭环补齐实施计划》在 tongtian-tauri 落地占位清单第一批 P0：存储地基（SQLite+keyring）、
          ModelHub 五件套、Workbench 两阶段审阅/控制/历史真实化、文件能力与附件上传。
          Rust command 由 11 个扩展至 26 个，全部假实现消除，Mock 双轨合规。
        </Text>
      </Stack>

      <Grid columns={4} gap={12}>
        <Stat value="26" label="Tauri command（11 → 26）" tone="success" />
        <Stat value="86" label="Rust 测试通过" tone="success" />
        <Stat value="110" label="TS 测试通过（含集成）" tone="success" />
        <Stat value="26" label="引擎 SSE 自测项 PASS" tone="success" />
      </Grid>

      <Divider />

      <H2>成果摘要</H2>
      <Table
        headers={['任务流', '落地内容', '状态']}
        rows={[
          ['流 1 · 存储地基', 'rusqlite(bundled) + keyring + uuid；store 模块（db/migrate/vault/settings/tasks）；版本化迁移幂等；密钥不落库', '完成'],
          ['流 2 · ModelHub', 'model_list/add/update/delete/test/set_default 六件套；连通性探测（10s 超时 + availableModels）；列表卡片按 id 从 keyring 补齐密钥', '完成'],
          ['流 3 · Workbench 闭环', '引擎两阶段协议（stage=plan → review approve/reject/modify → execute；control pause/resume/rollback_to）；Rust 前置态校验；tasks 表持久化 + chat_history', '完成'],
          ['流 4 · 文件与附件', 'file_list/read/preview 白名单校验（2MB 上限、非文本显式提示）；file_stage_attachment（格式/大小校验 + 敏感度检测）；ChatInput 附件卡片与三拒绝路径；FilePanel 浏览/预览', '完成'],
          ['流 5 · 收尾验收', 'mock-agent 两阶段端点；集成测试 9 项；占位清单滚动更新；init.sh/init.ps1 输出更新；浏览器 Mock 全链路走查', '完成'],
        ]}
      />

      <H2>关键实施步骤</H2>
      <Timeline
        events={[
          {
            title: '存储地基',
            description: 'Cargo 依赖（rusqlite/keyring/uuid/tauri-plugin-dialog）→ store 模块五件套 → lib.rs setup 装配 Store',
            tone: 'success',
          },
          {
            title: 'ModelHub 端到端',
            description: 'service.rs CRUD + model_test → 6 个 command 注册 → ModelHub 页面真实接线（增删改查/测试/编辑回填）',
            tone: 'success',
          },
          {
            title: '两阶段协议贯通',
            description: 'engine/server.py plan/review/control 端点 → Rust chat_review/chat_control/chat_history（14 态前置校验）→ chat_submit 改 stage=plan + tasks 落库',
            tone: 'success',
          },
          {
            title: '前端承接',
            description: 'workbenchStore 审阅/控制/历史/附件 actions；PlanView 提交修改按钮；Workbench 暂停/继续按钮；ChatInput 附件上传（dialog 插件）；FilePanel 真实浏览预览',
            tone: 'success',
          },
          {
            title: '验收与修复',
            description: '浏览器走查发现并修复两处两阶段 UI 缺陷（approve 旧禁用条件、plan 阶段 done 误判完成）；Mock 全链路复验通过',
            tone: 'success',
          },
        ]}
      />

      <H2>主要变更文件</H2>
      <CollapsibleSection title="Rust 侧（src-tauri）" defaultOpen>
        <Table
          headers={['文件', '变更']}
          rows={[
            ['Cargo.toml', '+rusqlite(bundled)/keyring/uuid/tauri-plugin-dialog'],
            ['src/store/{mod,db,migrate,vault,settings,tasks}.rs', '新建：SQLite 连接/版本化迁移/密钥保管/设置 kv/任务持久化（37 项内联测试）'],
            ['src/model/{mod,service}.rs', '新建：ModelConfig CRUD + 连通性探测 + keyring 联动（10 项测试）'],
            ['src/file/{mod,service}.rs', '新建：白名单目录列举/读取/预览 + 附件暂存（8 项测试）'],
            ['src/lib.rs', 'command 11→26：chat_review/chat_control/chat_history/model_* 六件套/settings_get/set/file_* 四件套；chat_submit 两阶段 + 附件透传；tasks 落库挂接'],
            ['src/bridge/agent.rs', 'send_message_with_attachments + stream_chat 附件 body'],
            ['capabilities/default.json', '+dialog:default 权限'],
          ]}
        />
      </CollapsibleSection>
      <CollapsibleSection title="Python 引擎与脚本" defaultOpen>
        <Table
          headers={['文件', '变更']}
          rows={[
            ['engine/server.py', '两阶段协议：stage=plan 计划生成（LLM+兜底）、/api/chat/review（approve/reject/modify）、/api/chat/control（pause/resume/rollback_to 含 rolled_back 事件语义）'],
            ['scripts/mock-agent.py', 'stage=plan 分支 + /api/chat/review + /api/chat/control 全真模拟（与真实引擎契约对齐）'],
            ['tests/integration/engine_sse_selftest.py', '扩展两阶段自测（共 26 项）'],
            ['tests/integration/mock-agent.integration.test.ts', '新增两阶段集成用例（plan/approve/reject/modify/control）'],
          ]}
        />
      </CollapsibleSection>
      <CollapsibleSection title="前端（src/renderer + src/shared）">
        <Table
          headers={['文件', '变更']}
          rows={[
            ['src/shared/types.ts · contracts.ts', 'AttachmentMeta 契约；ChatSubmitReq.attachments；ModelTestReq.id'],
            ['src/renderer/lib/api.ts', 'chatAPI.review(taskId,req)/control/cancel/history 真实化；新增 fileAPI（list/read/preview）'],
            ['src/renderer/lib/tauri-api.ts', 'model/file/settings/chat:history channel → snake_case command 映射；chatReview/chatControl/chatHistory/fileStageAttachment 真实 invoke'],
            ['src/renderer/lib/mock-api.ts', '两阶段协议全真模拟（plan/review/control）+ 模型内存 CRUD + 文件/附件/历史 Mock'],
            ['src/renderer/store/workbenchStore.ts', 'reviewPlan（approve/reject/modify 状态联动）、controlTask（pause/resume/rollback）、loadHistory、submitGoal(attachments)、plan 阶段 done 语义修正'],
            ['src/renderer/components/ChatInput', '附件上传：dialog 选择、格式/数量校验、卡片列表、暂存失败标红重试、随提交上送'],
            ['src/renderer/components/PlanView', '「提交修改」入口（modify）；approve 禁用条件修正'],
            ['src/renderer/pages/Workbench', '暂停/继续按钮、挂载加载历史、handleSend 附件传递'],
            ['src/renderer/pages/ModelHub', '5 处 TODO 全部替换为真实调用（含编辑回填/删除/测试）'],
            ['src/renderer/pages/FilePanel', '目录浏览/面包屑/文本预览/非文本显式提示'],
            ['src/preload/index.d.ts', 'TongtianAPI 新契约类型'],
          ]}
        />
      </CollapsibleSection>

      <H2>验证证据</H2>
      <Table
        headers={['验证项', '方式', '结果']}
        rows={[
          ['Rust 单测', 'cargo test --lib', '86 通过 / 0 失败（store 37 + model 10 + file 8 + 既有 31）'],
          ['TS 单测 + 集成', 'npx vitest run（6 文件）', '110 通过（含 mock-agent 两阶段集成 9 项）'],
          ['引擎契约自测', 'engine_sse_selftest.py', '26 项 PASS（含 plan/review/control 全分支）'],
          ['类型检查', 'npx tsc --noEmit', '零错误'],
          ['浏览器 Mock 数据链路', 'browser-use evaluate_script', 'phase1: queued→planning→plan→pending_review→done；phase2(approve): executing→step_*→quota_warning→deliver→done；历史=1；模型 CRUD ±1；文件=2；附件暂存 OK'],
          ['浏览器 UI 端到端', '快捷指令→计划面板→批准→执行→完成', 'approveEnabled=true；执行中暂停按钮出现；最终「已完成」；15 事件渲染'],
          ['缺陷修复复验', 'plan 阶段 done 语义 + approve 禁用条件', '修复后待审阅态正确、批准后执行流贯通'],
        ]}
      />

      <Callout tone="success" title="最终结果">
        计划五大任务流全部完成并通过验收：占位清单第一批 P0 全绿（Workbench/ModelHub/FilePanel 升级为「已实现」，
        附件上传落地），假实现清零，Mock 开关合规（VITE_MOCK 下全部新能力可脱离 Rust/LLM 演示）。
        剩余第二批 P1（workspace_*/skill_*/mcp_*/sandbox_*/schedule/audit）已在占位清单中滚动记录。
      </Callout>

      <Text tone="secondary" size="small">
        说明：本次浏览器取证期间截图工具持续超时，未生成截图文件，报告以脚本执行结果与 a11y 快照为证据，不含图片。
        依据：核心闭环补齐实施计划_task-495.md · docs/spec/spec-通天办公-占位能力清单.md（已同步更新）
      </Text>
    </Stack>
  );
}
