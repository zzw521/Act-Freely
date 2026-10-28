# Agent Engine — Agent 引擎（QwenPaw 复用改造）

> **能力域**：`agent-engine`  
> **类型**：改造（MODIFIED）  
> **上游 SPEC**：`spec-通天办公.md` §3.1  
> **改造策略**：复用 QwenPaw ReAct 引擎，通天办公侧保留状态机编排和 IPC 接口

---

## 概述

原 SPEC 设计了完整的 Agent 引擎（Planner / Executor / 状态机），改造后核心 ReAct 循环委托给 QwenPaw，通天办公侧保留任务状态机、Plan 审阅流程、中断/回滚机制和 IPC 接口层。QwenPaw Bridge 负责将通天办公的任务指令翻译为 QwenPaw API 调用。

---

## MODIFIED Requirements

### Requirement: Agent 任务生命周期状态机

系统 SHALL 维护任务状态机（idle → planning → pending_review → executing → delivering → done/failed），状态流转通过 QwenPaw Bridge 驱动。

#### Scenario: 提交任务进入 planning
- **WHEN** 用户通过 `chat:submit` 提交自然语言目标
- **THEN** 系统 SHALL 创建 Task 记录（状态 idle），通过 Bridge 向 QwenPaw 发送消息，QwenPaw 返回 Plan 后状态变为 `pending_review`

#### Scenario: 用户批准 Plan 进入 executing
- **WHEN** 用户在 Workbench 审阅 Plan 后点击「批准」
- **THEN** 系统 SHALL 将 Plan 状态标记为 `approved`，Task 状态变为 `executing`，通过 Bridge 通知 QwenPaw 开始执行

#### Scenario: 执行完成进入 delivering
- **WHEN** QwenPaw 所有 Step 执行完毕并推送 `done` 事件
- **THEN** 系统 SHALL 汇总产物（FileOp 列表），Task 状态变为 `delivering`，展示交付物后标记为 `done`

### Requirement: Plan 审阅与修改

系统 SHALL 支持用户对 Agent 生成的 Plan 进行批准、拒绝或修改操作。

#### Scenario: 用户修改 Plan 步骤
- **WHEN** 用户在 PlanView 中拖拽排序、删除或修改步骤参数
- **THEN** 系统 SHALL 将修改后的 Plan 通过 `chat:review` (action=modify) 发送给 QwenPaw，QwenPaw SHALL 按修改后的 Plan 执行

#### Scenario: 用户拒绝 Plan
- **WHEN** 用户点击「拒绝」
- **THEN** 系统 SHALL 将 Task 状态标记为 `done`（不执行），记录拒绝原因

### Requirement: 中断与回滚机制

系统 SHALL 支持任务执行中的暂停、继续和回滚操作。回滚通过 QwenPaw 的文件备份机制实现。

#### Scenario: 暂停执行
- **WHEN** 用户点击「暂停」
- **THEN** 系统 SHALL 通过 Bridge 通知 QwenPaw 暂停执行，Task 状态变为 paused

#### Scenario: 回滚到指定步骤
- **WHEN** 用户选择回滚到某个 Step
- **THEN** 系统 SHALL 通过 Bridge 调用 QwenPaw 的回滚接口，QwenPaw 从备份目录恢复文件，通天办公侧更新 Step 状态为 `rolled_back`

---

## ADDED Requirements

### Requirement: QwenPaw 事件流适配

系统 SHALL 将 QwenPaw 的 SSE 事件流适配为通天办公的 `ChatEvent` 类型，通过 IPC 推送至渲染进程。

#### Scenario: SSE 事件转换为 ChatEvent
- **WHEN** QwenPaw 推送 SSE 事件（plan / step_start / step_done 等）
- **THEN** Bridge SHALL 将其转换为对应的 `ChatEvent` 类型，通过 IPC 推送至渲染进程

---

## API 定义

### IPC Channel（保持不变）

| Channel | 方向 | 请求类型 | 响应类型 |
|---------|------|----------|----------|
| `chat:submit` | R→M | `ChatSubmitReq` | `ChatEvent[]` (流式) |
| `chat:review` | R→M | `ChatReviewReq` | `{ ok: boolean }` |
| `chat:control` | R→M | `ChatControlReq` | `{ ok: boolean }` |
| `chat:history` | R→M | `{ limit?: number }` | `TaskSummary[]` |

### 改造点

- `chat:submit` 内部实现从直接调用 LLM 改为通过 QwenPaw Bridge 发送消息
- `chat:review` 内部实现从本地状态机驱动改为通过 Bridge 通知 QwenPaw
- `chat:control` 的 rollback 操作改为通过 Bridge 调用 QwenPaw 回滚接口

---

## 数据模型

复用 `spec-通天办公.md` §4.3 中的 `tasks`、`messages`、`file_ops` 表结构，无变更。
