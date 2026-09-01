# MCP Integration — MCP 集成（QwenPaw 复用增强）

> **能力域**：`mcp-integration`  
> **类型**：改造（MODIFIED）  
> **上游 SPEC**：`spec-通天办公.md` §3.7  
> **改造策略**：复用 QwenPaw MCP 客户端（stdio/sse/streamable-http 三传输），通天办公侧新增管理 UI 和安全模型

---

## 概述

QwenPaw 已实现完整的 MCP 客户端（支持 stdio/sse/streamable-http 三种传输），通天办公直接复用其 MCP 连接管理和工具调用能力。通天办公侧新增 MCP 服务器管理 UI、per-workspace 工具白名单/黑名单和调用频率限制。

---

## MODIFIED Requirements

### Requirement: MCP 服务器连接管理

系统 SHALL 通过 QwenPaw Bridge 管理 MCP 服务器的连接/断开/健康检查，支持 stdio/sse/streamable-http 三种传输方式。

#### Scenario: 连接 MCP 服务器
- **WHEN** 用户在设置中配置 MCP 服务器并点击「连接」
- **THEN** 系统 SHALL 通过 Bridge 调用 QwenPaw 的 MCP 连接接口，完成协议握手，获取工具列表，更新状态为 `connected`

#### Scenario: 断开 MCP 服务器
- **WHEN** 用户点击「断开」
- **THEN** 系统 SHALL 通过 Bridge 调用 QwenPaw 的断开接口，优雅关闭连接，更新状态为 `disconnected`

#### Scenario: 连接异常自动重连
- **WHEN** MCP 连接意外断开
- **THEN** QwenPaw SHALL 自动重连（指数退避，最多 3 次），通天办公侧更新状态并在失败后通知用户

### Requirement: MCP 工具调用

系统 SHALL 通过 QwenPaw Bridge 调用 MCP 工具，自动将 MCP 工具注册为 Agent 可调用的 StepType。

#### Scenario: Agent 调用 MCP 工具
- **WHEN** Agent 执行过程中需要使用 MCP 工具
- **THEN** 系统 SHALL 通过 Bridge 调用 `callTool`，传入 serverId/toolName/params，接收结果并记录到 Step.result

#### Scenario: 工具调用超时
- **WHEN** MCP 工具调用超过 30s
- **THEN** 系统 SHALL 标记调用失败，返回 `MCP_SERVER_TIMEOUT` 错误，通知用户

### Requirement: MCP 安全模型

系统 SHALL 实现 MCP 工具的安全控制，包括 per-workspace 工具白名单/黑名单、调用频率限制和参数校验。

#### Scenario: 工具白名单检查
- **WHEN** Agent 尝试调用 MCP 工具
- **THEN** 系统 SHALL 检查该工具是否在当前工作区的白名单内（白名单为空表示全部允许），不在白名单则返回 `MCP_TOOL_NOT_ALLOWED`

#### Scenario: 调用频率限制
- **WHEN** 某工具调用频率超过限制（默认 60 次/分钟）
- **THEN** 系统 SHALL 拒绝调用，返回 `MCP_RATE_LIMITED` 错误，并启动冷却期

---

## ADDED Requirements

### Requirement: MCP 服务器管理 UI

系统 SHALL 在设置页面提供 MCP 服务器的添加/编辑/删除/测试连接功能。

#### Scenario: 添加 MCP 服务器
- **WHEN** 用户在设置中填写 MCP 服务器配置（名称、传输方式、端点、认证方式）
- **THEN** 系统 SHALL 保存配置并尝试连接，连接成功后在列表中显示

#### Scenario: 测试连通性
- **WHEN** 用户点击「测试」
- **THEN** 系统 SHALL 通过 `mcp:test` IPC 测试连通性，返回是否成功和延迟

---

## API 定义

### IPC Channel（保持不变）

| Channel | 方向 | 请求类型 | 响应类型 |
|---------|------|----------|----------|
| `mcp:connect` | R→M | `MCPServer` | `MCPServer` |
| `mcp:disconnect` | R→M | `{ serverId: string }` | `{ ok: boolean }` |
| `mcp:list-tools` | R→M | `{ serverId: string }` | `MCPTool[]` |
| `mcp:test` | R→M | `MCPServer` | `{ ok: boolean; latencyMs: number }` |

### 改造点

- 内部实现从通天办公自研 MCPClient 改为通过 QwenPaw Bridge 调用 QwenPaw 的 MCP 管理接口
- 安全模型（白名单/频率限制）在通天办公侧实现，作为 Bridge 调用前的前置检查

---

## 数据模型

复用 `spec-通天办公.md` §4.3 中的 `mcp_servers` 表结构，无变更。
