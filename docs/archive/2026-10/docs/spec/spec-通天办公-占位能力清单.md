# 通天办公 · 占位能力清单（tongtian-tauri 现状盘点）

> **文档版本**：v1.0
> **盘点日期**：2026-08-05
> **盘点范围**：`tongtian-tauri/src/renderer/pages/*`、`components/*`、`lib/*` 与 `src-tauri` 已注册 command
> **关联 SPEC**：`docs/spec/spec-通天办公.md` v2.1 §5.1（Tauri Command 清单）
> **用途**：作为能力实现排期依据；所有「纯占位」能力在 UI 上必须显式禁用并标注，禁止静默无响应

---

## 1. 盘点方法与现状分级

**现状分级定义**：

| 分级 | 定义 | UI 处置要求 |
|------|------|-------------|
| 已实现 | 前端调用 + 后端 command 均落地，端到端可用 | 正常展示 |
| 部分实现 | 链路部分打通（如仅 Mock 数据、或后端返回占位） | 标注「测试数据」或功能受限提示 |
| 纯占位 | 仅 UI 外壳，无后端能力；或前端 fallback 静默返回空 | 必须禁用态 + tooltip「即将上线」，禁止静默 |

**根因结论**（与 SPEC 对照）：SPEC §5.1 定义 32 个 command。经两轮迭代后 Rust 侧已注册 26 个：
- 基础 5 个：`system_info`、`agent_status`、`agent_start_workspace`、`agent_stop_workspace`、`chat_submit`（已改两阶段 stage=plan + 附件透传）
- 三层架构演进 P0 6 个：`chat_cancel`、`task_state`、`quota_status`、`lease_info`、`lease_takeover`、`event_replay`
- 核心闭环补齐 15 个：`chat_review`、`chat_control`、`chat_history`、`model_list/add/update/delete/test/set_default`、`settings_get/set`、`file_list/read/preview`、`file_stage_attachment`

仍缺：`workspace_*`（6 个）、`skill_*`（5 个）、`mcp_*`（4 个）、`agent_delegate`、`sandbox_*`（2 个）及 schedule/audit command 族（第二批 P1）。前端 `tauri-api.ts` 以 `CHANNEL_FALLBACK` 空数组与 `createProxiedAPI` 兜底未落地能力。

---

## 2. 页面级盘点

| 页面 | 现状 | 缺失 command / 后端模块 | 优先级 | 处置建议 |
|------|:----:|------------------------|:----:|----------|
| Workbench（对话工作台） | 已实现 | 两阶段协议全通：`chat_submit(stage=plan)`/`chat_review`/`chat_control`/`chat_history`/`chat_cancel`；附件上传 `file_stage_attachment`；暂停/继续/终止/提交修改按钮已接入 | — | 正常展示 |
| ModelHub（模型管理） | 已实现 | `model_list/add/update/delete/test/set_default` 全部接入（SQLite 持久化 + keyring 密钥） | — | 正常展示 |
| FilePanel（文件面板） | 已实现 | `file_list`、`file_read`、`file_preview`（白名单校验，docx/xlsx/pdf 显式提示转换未落地） | — | 正常展示 |
| Settings（设置） | 部分实现 | `settings_get`、`settings_set` 后端已落地；前端表单接线待第二批 | P1 | 后端可用 |
| SkillMarket（技能市场） | 纯占位 | `skill_list/install/uninstall/enable/update`（`crates/skills`） | P1 | 列表兜底为空数组，需空态 + 说明 |
| WorkspacePanel（多工作区） | 纯占位 | `workspace_create/list/switch/destroy/suspend/resume`（`crates/workspace`）；仅 `agent_start/stop_workspace` 桥接存在 | P1 | 当前仅单工作区可用，需标注 |
| Security（安全/沙箱） | 纯占位 | `sandbox_get_policy/update_policy`（`crates/sandbox`） | P1 | 策略表单禁用保存 |
| ScheduleView（定时任务） | 纯占位 | 定时任务 command 族（SPEC 未列全，需补契约；`tokio-cron-scheduler`） | P1 | 列表 fallback 空数组 |
| AuditView（审计日志） | 纯占位 | 审计查询 command（`audit_logs` 表已在 DDL；需查询 command） | P1 | 同上 |
| HooksManager（钩子管理） | 纯占位 | hooks 契约 SPEC 未定义（需补设计） | P2 | 禁用 + 即将上线 |
| AwarenessPage（记忆/感知） | 纯占位 | 记忆系统 command（SPEC §4 QwenPaw 处置中列为 P2；`crates/agent` 记忆子模块） | P2 | 当前 Mock 条目需标注「示例数据」 |
| ConnectorCenter（连接器中心） | 纯占位 | 连接器市场属云端能力（见云端与账号体系 spec Phase C） | P2 | 本地 Mock 清单需标注 |
| IMChannel（IM 渠道） | 纯占位 | IM 集成（演进方案列为 P2，Webhook/Bot API） | P2 | Mock 消息流需标注「示例数据」 |
| ComputerUsePanel（桌面控制） | 纯占位 | 桌面控制（`enigo`，SPEC §3.4 P1；契约未定义） | P1 | 禁用操作按钮 |
| ArchiveView（归档） | 纯占位 | `workspace` 归档能力（`crates/workspace::archive`） | P2 | 依赖 WorkspacePanel |

## 3. 组件级专项（Workbench 输入区三项）

以下三项为交互设计缺失专项，补设计见 `docs/changes/workbench-interaction/spec.md`：

| 能力 | 现状证据 | 缺失后端能力 | 优先级 |
|------|----------|--------------|:----:|
| 引用技能 | `ChatInput` 有技能按钮与「输入 /调用技能」占位文案，点击无响应 | `skill_list` + Planner 的技能注入契约 | P1 |
| 上传文件 | **已实现**：ChatInput 附件按钮接系统对话框（tauri-plugin-dialog），格式/数量校验、卡片列表、`file_stage_attachment` 暂存、随 `chat_submit.attachments` 提交 | — | — |
| 选择工作目录 | 无任何入口 | 目录选择（tauri-plugin-dialog）+ `SandboxPolicy.file_allowlist` 联动 | P1 |

## 4. 接入层问题（`src/renderer/lib/`）

| 问题 | 位置 | 处置 |
|------|------|------|
| `CHANNEL_FALLBACK` 静默返回空数组 | `tauri-api.ts` L19-28 | 过渡保留，但须在调用点转换为「空态 + 未接入提示」，不得假装已加载 |
| `createProxiedAPI` 任意方法兜底 `[]` | `tauri-api.ts` L88-97 | dev 环境输出 console.warn，标明未实现方法名 |
| `chatReview/chatControl` 假实现恒成功 | `tauri-api.ts` | **已修复**：接入真实 `chat_review`/`chat_control` command，前置态校验由 Rust 侧执行 |
| 仍按旧 `domain:action` channel 命名封装 | `api.ts`（`IPC_CHANNELS`） | 迁移至 v2.1 command 命名（`domain_action`），见演进方案 §3.2 |

## 5. 实现顺序建议（与 SPEC 阶段对齐）

```
第一批（P0，核心闭环补齐）——已完成 ✔
├─ store 模块（rusqlite bundled + keyring）           → settings/model/tasks 持久化 ✔
├─ model_list/add/update/delete/test/set_default       → ModelHub 页面通 ✔
├─ file_list/read/preview/file_stage_attachment        → FilePanel 通 + 附件上传 ✔
├─ chat_review/chat_control/chat_history               → Workbench 审阅/暂停/回滚/历史真实化 ✔
└─ ChatInput 上传文件（附件模式，见交互补设计）       ✔

第二批（P1，能力扩展）
├─ crates/skills + skill_*                         → SkillMarket + ChatInput 技能引用
├─ crates/workspace + workspace_*                  → WorkspacePanel + 工作目录选择
├─ crates/sandbox + sandbox_*                      → Security 页
└─ schedule/audit command 族                       → ScheduleView + AuditView

第三批（P2，生态与增值）
├─ 记忆系统 / hooks / 归档
└─ ConnectorCenter / IMChannel（依赖云端 spec Phase C）
```

---

> 本清单随 command 落地滚动更新：每实现一个 command，将对应行移入「已实现」并记录版本。
