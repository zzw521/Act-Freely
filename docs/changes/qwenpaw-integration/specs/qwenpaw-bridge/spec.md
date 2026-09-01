# QwenPaw Bridge — 引擎桥接集成

> **能力域**：`qwenpaw-bridge`  
> **类型**：新增（ADDED）  
> **上游 SPEC**：`spec-通天办公-qwenpaw-改造方案.md` §5  
> **优先级**：P0

---

## 概述

QwenPaw Bridge 是通天办公 Electron 主进程与 QwenPaw Python 引擎之间的桥梁层。它负责以子进程方式管理 QwenPaw 实例的生命周期，通过 HTTP API / stdio 进行通信，实现工作区级别的进程隔离。

---

## ADDED Requirements

### Requirement: QwenPaw 子进程生命周期管理

系统 SHALL 通过 `child_process.spawn` 启动 QwenPaw Python 子进程，并管理其完整生命周期（启动 → 就绪 → 忙碌 → 停止/异常）。每个工作区对应一个独立的 QwenPaw 子进程实例。

#### Scenario: 启动工作区实例
- **WHEN** 用户创建新工作区或恢复已暂停的工作区
- **THEN** 系统 SHALL spawn 一个 QwenPaw Python 子进程，分配唯一 HTTP 端口，等待健康检查通过后标记状态为 `ready`

#### Scenario: 停止工作区实例
- **WHEN** 用户销毁或暂停工作区
- **THEN** 系统 SHALL 向 QwenPaw 子进程发送优雅关闭信号（SIGTERM），等待最多 5s 后强制终止（SIGKILL），并释放端口资源

#### Scenario: 子进程异常崩溃
- **WHEN** QwenPaw 子进程意外退出（exit code ≠ 0）
- **THEN** 系统 SHALL 将工作区状态标记为 `error`，记录崩溃日志，并通过 IPC 通知渲染进程展示错误提示

### Requirement: QwenPaw HTTP API 通信

系统 SHALL 通过 HTTP API 与 QwenPaw 子进程通信，支持 SSE 流式响应。所有请求 SHALL 包含超时控制和重试机制。

#### Scenario: 发送用户消息并接收流式响应
- **WHEN** 用户通过 Workbench 发送对话消息
- **THEN** Bridge SHALL 向 QwenPaw 实例发送 `POST /api/chat`，解析 SSE 事件流（plan / step_start / step_done / done），并通过 IPC 逐事件推送至渲染进程

#### Scenario: 调用 QwenPaw 工具
- **WHEN** Agent 执行过程中需要调用工具
- **THEN** Bridge SHALL 发送 `POST /api/tools/{tool_name}`，传入参数并接收 `ToolResult`，超时时间 SHALL 为 30s

#### Scenario: 健康检查
- **WHEN** 工作区实例启动后或周期性（每 10s）
- **THEN** Bridge SHALL 发送 `GET /health`，若连续 3 次失败则标记实例为不可用

### Requirement: QwenPaw 版本管理与适配

系统 SHALL 通过 API 适配层隔离 QwenPaw 内部实现细节，支持版本锁定（v1.1.x）和向上兼容。

#### Scenario: API 版本不兼容检测
- **WHEN** QwenPaw 子进程启动时返回的 API 版本不在兼容范围内
- **THEN** 系统 SHALL 记录警告日志，并通过 IPC 通知用户「QwenPaw 引擎版本不兼容，请升级或降级」

#### Scenario: 适配层屏蔽上游变更
- **WHEN** QwenPaw 上游 API 发生 breaking change
- **THEN** 仅需修改 Bridge 适配层代码，通天办公上层模块 SHALL 无需任何改动

### Requirement: QwenPaw 配置注入

系统 SHALL 在启动 QwenPaw 子进程时，根据工作区配置自动生成并注入配置文件（模型绑定、技能集、MCP 工具集、沙箱策略等）。

#### Scenario: 按工作区配置启动
- **WHEN** 启动工作区实例时传入 `WorkspaceConfig`
- **THEN** Bridge SHALL 生成对应的 QwenPaw 配置文件（JSON），设置工作目录为 `~/.tongtian/workspaces/{workspaceId}/`，并作为启动参数传入

#### Scenario: 动态更新配置
- **WHEN** 用户在工作区运行时修改模型绑定或技能集
- **THEN** Bridge SHALL 通过 `POST /api/config` 热更新 QwenPaw 配置，无需重启子进程

---

## API 定义

### QwenPawBridge 接口

```typescript
class QwenPawBridge {
  async startWorkspace(workspaceId: string, config: WorkspaceConfig): Promise<QwenPawInstance>;
  async stopWorkspace(workspaceId: string): Promise<void>;
  async sendMessage(workspaceId: string, message: string): AsyncIterable<AgentEvent>;
  async callTool(workspaceId: string, tool: string, params: object): Promise<ToolResult>;
  async getStatus(workspaceId: string): Promise<WorkspaceStatus>;
  async healthCheck(workspaceId: string): Promise<{ ok: boolean; latencyMs: number }>;
}
```

### Request/Response DTOs

```typescript
interface QwenPawInstance {
  workspaceId: string;
  process: ChildProcess;
  httpPort: number;
  status: 'starting' | 'ready' | 'busy' | 'stopped' | 'error';
  config: WorkspaceConfig;
  startedAt: Date;
}

interface AgentEvent {
  type: 'plan' | 'step_start' | 'step_progress' | 'step_done' | 'step_failed' | 'deliver' | 'done' | 'error';
  data: Record<string, unknown>;
}

interface ToolResult {
  success: boolean;
  output: string;
  error?: string;
  durationMs: number;
}
```

### Error Codes

| 错误码 | 说明 |
|--------|------|
| `BRIDGE_START_FAILED` | QwenPaw 子进程启动失败 |
| `BRIDGE_HEALTH_CHECK_FAILED` | 健康检查连续失败 |
| `BRIDGE_API_TIMEOUT` | API 调用超时 |
| `BRIDGE_VERSION_INCOMPATIBLE` | API 版本不兼容 |
| `BRIDGE_PORT_CONFLICT` | 端口冲突 |

---

## 数据模型

无独立持久化表。QwenPaw 实例状态为运行时内存状态，工作区元数据存储在 `workspaces` 表中（由 workspace 能力域管理）。
