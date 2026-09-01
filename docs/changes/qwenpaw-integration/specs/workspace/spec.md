# Workspace — 多智能体工作区管理

> **能力域**：`workspace`  
> **类型**：新增（ADDED）  
> **上游 SPEC**：`spec-通天办公.md` §3.5 + `spec-通天办公-qwenpaw-改造方案.md` §7.3  
> **优先级**：P0（基础管理 MVP，ACP 通信 Phase 2）

---

## 概述

多智能体工作区在 QwenPaw 单实例多 Agent 的基础上，通过为每个工作区启动独立 QwenPaw Python 子进程实现更强的隔离。每个工作区拥有独立的模型绑定、记忆空间、技能集、MCP 工具集和资源配额。

---

## ADDED Requirements

### Requirement: 工作区生命周期管理

系统 SHALL 支持工作区的创建、暂停、恢复、归档和销毁操作，每个操作 SHALL 同步管理对应的 QwenPaw 子进程。

#### Scenario: 创建工作区
- **WHEN** 用户点击「新建工作区」并填写名称、角色描述、选择模型
- **THEN** WorkspaceManager SHALL 在 SQLite 中创建工作区记录，通过 QwenPawBridge 启动对应 Python 子进程，分配独立端口和工作目录

#### Scenario: 暂停工作区
- **WHEN** 用户点击「暂停」
- **THEN** WorkspaceManager SHALL 通过 Bridge 优雅关闭 QwenPaw 子进程（保留工作目录和状态），将工作区状态标记为 `suspended`

#### Scenario: 恢复工作区
- **WHEN** 用户点击「恢复」
- **THEN** WorkspaceManager SHALL 通过 Bridge 重新启动 QwenPaw 子进程，加载原有配置和记忆，恢复至 `active` 状态

#### Scenario: 归档工作区
- **WHEN** 用户点击「归档」
- **THEN** WorkspaceManager SHALL 打包工作区历史数据至归档目录，停止子进程，将状态标记为 `archived`

### Requirement: 工作区资源隔离

每个工作区 SHALL 拥有独立的 AgentRunner、TaskQueue、EventEmitter 实例，确保工作区之间互不干扰。

#### Scenario: 独立任务执行
- **WHEN** 工作区 A 和工作区 B 同时执行任务
- **THEN** 两个工作区的任务 SHALL 在各自独立的 QwenPaw 子进程中并行执行，互不阻塞

#### Scenario: 工作区崩溃隔离
- **WHEN** 工作区 A 的 QwenPaw 子进程崩溃
- **THEN** 工作区 B SHALL 不受影响，继续正常运行；系统 SHALL 通知用户工作区 A 异常并尝试自动重启

### Requirement: 工作区资源配额

系统 SHALL 对每个工作区实施资源配额限制，包括并发 Step 数、内存、并发模型调用数和任务队列深度。

#### Scenario: 并发 Step 数超限
- **WHEN** 工作区并发执行 Step 数达到上限（默认 5）
- **THEN** 新的 Step SHALL 进入等待队列，直到有 Step 完成释放资源

#### Scenario: 任务队列深度超限
- **WHEN** 工作区任务队列达到上限（默认 100）
- **THEN** 系统 SHALL 拒绝新任务提交，返回 `WORKSPACE_QUOTA_EXCEEDED` 错误

### Requirement: Agent 间通信协议 (ACP)（Phase 2）

系统 SHALL 支持工作区之间的 Agent 通信，实现任务委派、结果回传和状态同步。

#### Scenario: 委派子任务
- **WHEN** 工作区 A 的 Agent 需要工作区 B 协助完成子任务
- **THEN** 系统 SHALL 通过 ACP 协议发送 `task_assign` 消息至工作区 B，工作区 B 创建子任务并开始执行

#### Scenario: 子任务结果回传
- **WHEN** 工作区 B 完成委派任务
- **THEN** 系统 SHALL 通过 ACP 发送 `task_result` 消息至工作区 A，工作区 A 的 Agent 接收结果并继续执行

---

## API 定义

### IPC Channel

| Channel | 方向 | 请求类型 | 响应类型 |
|---------|------|----------|----------|
| `workspace:create` | R→M | `WorkspaceCreateReq` | `Workspace` |
| `workspace:list` | R→M | `void` | `Workspace[]` |
| `workspace:switch` | R→M | `{ workspaceId: string }` | `{ ok: boolean }` |
| `workspace:destroy` | R→M | `{ workspaceId: string }` | `{ ok: boolean }` |
| `workspace:suspend` | R→M | `{ workspaceId: string }` | `{ ok: boolean }` |
| `workspace:resume` | R→M | `{ workspaceId: string }` | `{ ok: boolean }` |
| `agent:delegate` | R→M | `AgentDelegateReq` | `AgentDelegateResp` |

### Error Codes

| 错误码 | 说明 |
|--------|------|
| `WORKSPACE_NOT_FOUND` | 工作区不存在 |
| `WORKSPACE_QUOTA_EXCEEDED` | 工作区资源配额超限 |
| `WORKSPACE_ALREADY_RUNNING` | 工作区已在运行 |
| `ACP_MESSAGE_FAILED` | ACP 消息发送失败 |
| `ACP_TASK_REJECTED` | ACP 任务被拒绝 |

---

## 数据模型

复用 `spec-通天办公.md` §4.3 中的 `workspaces` 表结构。
