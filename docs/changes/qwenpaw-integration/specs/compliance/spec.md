# Compliance — 企业合规守卫

> **能力域**：`compliance`  
> **类型**：新增（ADDED）  
> **上游 SPEC**：`spec-通天办公.md` §6.3 + `spec-通天办公-qwenpaw-改造方案.md` §7.4  
> **优先级**：P0

---

## 概述

企业合规守卫是通天办公侧全新开发的安全能力，QwenPaw 无对应模块。它提供数据分级检测、出域白名单、硬开关、合规告知和全量审计五项核心能力，确保企业数据不外泄、模型调用可追溯。

---

## ADDED Requirements

### Requirement: 数据分级检测

系统 SHALL 在数据发送至模型 API 前，自动检测内容中的数据分级（PUBLIC / INTERNAL / CONFIDENTIAL），并根据分级执行相应策略。

#### Scenario: 检测 CONFIDENTIAL 级别数据
- **WHEN** 用户发送的内容包含身份证号、护照号、合同条款、薪资信息等
- **THEN** ComplianceGuard SHALL 将数据标记为 CONFIDENTIAL，仅允许发送至白名单本地模型，拒绝发送至云端模型

#### Scenario: 检测 INTERNAL 级别数据
- **WHEN** 用户发送的内容包含「内部」「机密」「绝密」等关键词
- **THEN** ComplianceGuard SHALL 将数据标记为 INTERNAL，仅允许发送至白名单模型（含云端白名单）

### Requirement: 出域白名单

系统 SHALL 维护模型出域白名单，仅白名单内的模型可接收 INTERNAL 及以上级别的数据。

#### Scenario: 白名单模型接收敏感数据
- **WHEN** 数据分级为 INTERNAL 且目标模型在白名单内
- **THEN** 系统 SHALL 允许数据发送至该模型

#### Scenario: 非白名单模型被拒绝
- **WHEN** 数据分级为 INTERNAL 且目标模型不在白名单内
- **THEN** 系统 SHALL 拒绝发送，返回 `COMPLIANCE_DATA_BLOCKED` 错误，并记录审计日志

### Requirement: 硬开关（一键禁用外部 API）

系统 SHALL 提供全局硬开关，开启后一键禁用所有外部（云端）模型 API 调用，仅允许本地模型。

#### Scenario: 开启硬开关
- **WHEN** 管理员在设置页面开启「数据保护模式」硬开关
- **THEN** 系统 SHALL 立即阻断所有对云端模型 API 的请求，仅允许本地 Ollama 模型调用，并在 UI 顶部显示「数据保护模式已启用」标识

#### Scenario: 硬开关开启时尝试调用云端模型
- **WHEN** 硬开关已开启，用户尝试使用云端模型对话
- **THEN** 系统 SHALL 阻断调用，提示「数据保护模式已启用，请切换至本地模型」

### Requirement: 合规告知（数据外发确认弹窗）

系统 SHALL 在数据首次发送至外部模型时，弹出合规告知对话框，要求用户确认。

#### Scenario: 首次外发数据确认
- **WHEN** 用户首次向云端模型发送消息（且硬开关未开启）
- **THEN** 系统 SHALL 弹出合规告知对话框，显示「您的对话内容将发送至 [模型名称]（[服务商]）」，用户点击「确认」后方可继续

#### Scenario: 已确认后不再重复弹窗
- **WHEN** 用户已对某模型确认过合规告知
- **THEN** 后续向同一模型发送消息 SHALL 不再弹出确认框（除非配置变更）

### Requirement: 全量审计日志

系统 SHALL 记录所有模型调用、数据流向、token 用量、配置变更的审计日志，支持查询和导出。

#### Scenario: 记录模型调用审计
- **WHEN** 每次模型 API 调用完成
- **THEN** 系统 SHALL 记录审计日志，包含时间戳、模型 ID、token 用量、数据分级、是否成功

#### Scenario: 导出审计日志
- **WHEN** 管理员在 AuditView 页面点击「导出」
- **THEN** 系统 SHALL 将指定时间范围的审计日志导出为 CSV 文件

---

## API 定义

### IPC Channel

| Channel | 方向 | 请求类型 | 响应类型 |
|---------|------|----------|----------|
| `compliance:get-status` | R→M | `void` | `ComplianceStatus` |
| `compliance:set-hard-switch` | R→M | `{ enabled: boolean }` | `{ ok: boolean }` |
| `compliance:get-whitelist` | R→M | `void` | `string[]` |
| `compliance:set-whitelist` | R→M | `string[]` | `{ ok: boolean }` |
| `compliance:check-data` | R→M | `{ content: string }` | `{ sensitivity: DataSensitivity; details: string[] }` |
| `audit:list` | R→M | `AuditQuery` | `AuditLog[]` |
| `audit:export` | R→M | `AuditQuery` | `{ filePath: string }` |

### Request/Response DTOs

```typescript
interface ComplianceStatus {
  hardSwitchEnabled: boolean;
  whitelistModelIds: string[];
  lastDataCheck?: { sensitivity: DataSensitivity; timestamp: Date };
}

interface AuditQuery {
  startTime: string;
  endTime: string;
  eventTypes?: string[];
  workspaceId?: string;
  modelId?: string;
  limit?: number;
  offset?: number;
}
```

### Error Codes

| 错误码 | 说明 |
|--------|------|
| `COMPLIANCE_DATA_BLOCKED` | 数据被合规策略阻断 |
| `COMPLIANCE_HARD_SWITCH_ON` | 硬开关已开启，外部 API 被禁用 |

---

## 数据模型

复用 `spec-通天办公.md` §4.3 中的 `audit_logs` 表结构，新增合规相关事件类型：

```sql
-- 合规事件类型扩展
-- event 字段新增: compliance.data_check | compliance.hard_switch | compliance.whitelist_change | compliance.confirmation
```
