# Model Hub — 模型管理中心

> **能力域**：`model-hub`  
> **类型**：新增（ADDED）  
> **上游 SPEC**：`spec-通天办公.md` §3.2 + `spec-通天办公-qwenpaw-改造方案.md` §7.1  
> **优先级**：P0

---

## 概述

Model Hub 在 QwenPaw 已有的 14+ 供应商适配器基础上，新增统一模型管理 UI、模型路由策略、降级与熔断、成本监控、能力评测和企业出域合规能力。通天办公侧通过 IPC 调用 Model Hub，Model Hub 内部委托 QwenPaw 的供应商适配器完成实际模型调用。

---

## ADDED Requirements

### Requirement: 统一模型管理 UI

系统 SHALL 提供 ModelHub 页面，支持模型的接入、切换、绑定工作区、设为默认等操作，所有模型配置 SHALL 持久化到 SQLite `model_configs` 表。

#### Scenario: 添加自定义模型
- **WHEN** 用户在 ModelHub 页面填写模型名称、Base URL、API Key、模型标识并点击「添加」
- **THEN** 系统 SHALL 先执行连通性自测（healthCheck），成功后将配置加密（API Key 使用 AES-256-GCM）存入数据库，并在模型列表中显示

#### Scenario: 模型连通性自测
- **WHEN** 用户点击「测试连接」
- **THEN** 系统 SHALL 向目标模型发送测试请求，返回延迟（latencyMs）、可用模型列表或错误信息

#### Scenario: 绑定模型到工作区
- **WHEN** 用户在工作区设置中选择绑定模型
- **THEN** 系统 SHALL 更新 `model_configs.agent_binding` 字段，该工作区后续对话 SHALL 优先使用绑定模型

### Requirement: 模型路由策略

系统 SHALL 实现三层路由策略：敏感度检测 → 智能体绑定 → 全局默认，并在主模型不可用时自动降级至备用模型。

#### Scenario: 敏感数据强制本地模型
- **WHEN** 任务内容被检测为 CONFIDENTIAL 级别且存在本地模型
- **THEN** 路由器 SHALL 自动选择本地 Ollama 模型，并记录审计日志 `route:sensitive_forced_local`

#### Scenario: 主模型超时自动降级
- **WHEN** 主模型调用 P95 超过 8s 或连续 3 次失败
- **THEN** 路由器 SHALL 自动切换至备用模型列表中的下一个可用模型，并通知用户「已切换至备用模型」

#### Scenario: 全部模型不可用
- **WHEN** 所有配置的模型均不可用
- **THEN** 系统 SHALL 暂停当前任务，抛出 `ALL_MODELS_UNAVAILABLE` 错误，并提示用户检查网络连接或添加新模型

### Requirement: 降级与熔断机制

系统 SHALL 实现熔断器模式，当模型错误率超过阈值时触发熔断，冷却期后自动恢复。

#### Scenario: 错误率触发熔断
- **WHEN** 某模型 5 分钟内错误率超过 15%
- **THEN** 系统 SHALL 触发熔断（状态变为 OPEN），该模型 3 分钟内不再被路由选中，并记录审计事件 `circuit.break`

#### Scenario: 限流指数退避
- **WHEN** 模型返回 HTTP 429（限流）
- **THEN** 系统 SHALL 按指数退避重试（1s→2s→4s→8s，最多 4 次），若全部重试失败则切换至备用模型

### Requirement: 成本与用量监控

系统 SHALL 记录每次模型调用的 token 用量、延迟和成本，并提供统计面板。

#### Scenario: 记录 token 用量
- **WHEN** 每次模型调用完成
- **THEN** Observer SHALL 记录 `promptTokens`、`completionTokens`、`latencyMs`，并累计到日/周/月统计

#### Scenario: 查看用量统计
- **WHEN** 用户在 ModelHub 页面查看用量统计
- **THEN** 系统 SHALL 展示按模型/按工作区/按时间维度的 token 用量和估算成本

### Requirement: 模型能力评测（P2）

系统 SHALL 提供标准评测套件，对已接入模型进行能力评测并生成能力雷达图。

#### Scenario: 执行评测套件
- **WHEN** 用户在 ModelHub 选择模型并点击「评测」
- **THEN** 系统 SHALL 运行预设评测题目（代码生成/文本理解/数学推理/中文理解/指令遵循），评分后生成五维雷达图

---

## API 定义

### IPC Channel

| Channel | 方向 | 请求类型 | 响应类型 |
|---------|------|----------|----------|
| `model:list` | R→M | `void` | `ModelConfig[]` |
| `model:add` | R→M | `ModelConfig` | `ModelConfig` |
| `model:update` | R→M | `ModelConfig` | `ModelConfig` |
| `model:delete` | R→M | `{ id: string }` | `{ ok: boolean }` |
| `model:test` | R→M | `ModelTestReq` | `ModelTestResp` |
| `model:usage` | R→M | `{ period: 'day' \| 'week' \| 'month' }` | `UsageStats[]` |

### Request/Response DTOs

```typescript
interface ModelTestReq {
  baseUrl: string;
  apiKey: string;
  modelName: string;
  contextWindow: number;
}

interface ModelTestResp {
  ok: boolean;
  latencyMs: number;
  model: string;
  error?: string;
  availableModels?: string[];
}

interface UsageStats {
  modelId: string;
  modelName: string;
  totalPromptTokens: number;
  totalCompletionTokens: number;
  totalCalls: number;
  avgLatencyMs: number;
  estimatedCost: number;
}
```

### Error Codes

| 错误码 | 说明 |
|--------|------|
| `CONNECTION_TIMEOUT` | 模型连接超时 |
| `INVALID_API_KEY` | API Key 无效 |
| `MODEL_NOT_FOUND` | 模型不存在 |
| `ALL_MODELS_UNAVAILABLE` | 所有模型不可用 |
| `CIRCUIT_BREAKER_OPEN` | 熔断器已开启 |

---

## 数据模型

复用 `spec-通天办公.md` §4.3 中的 `model_configs` 表结构，新增用量统计字段：

```sql
-- 模型用量统计表（新增）
CREATE TABLE model_usage (
  id TEXT PRIMARY KEY,
  model_id TEXT NOT NULL REFERENCES model_configs(id),
  workspace_id TEXT REFERENCES workspaces(id),
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  success INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_model_usage_model ON model_usage(model_id);
CREATE INDEX idx_model_usage_date ON model_usage(created_at);
```
