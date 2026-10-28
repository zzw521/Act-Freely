# Audit Logging — 审计日志（通天办公增强）

> **能力域**：`audit-logging`  
> **类型**：改造（MODIFIED）  
> **上游 SPEC**：`spec-通天办公.md` §6 / §9.4  
> **改造策略**：通天办公侧实现全量审计，QwenPaw 引擎调用日志通过 Bridge 采集

---

## 概述

QwenPaw 自身有基础日志能力，但不满足企业级审计要求。通天办公侧新增全量审计模块，采集 QwenPaw Bridge 的所有操作事件（模型调用、文件操作、配置变更、合规事件等），持久化到 SQLite `audit_logs` 表，提供查询和导出功能。

---

## MODIFIED Requirements

### Requirement: 全量审计日志采集

系统 SHALL 在以下事件发生时自动记录审计日志，日志来源包括通天办公主进程和 QwenPaw Bridge。

#### Scenario: 记录模型调用事件
- **WHEN** 每次模型 API 调用完成（无论成功/失败）
- **THEN** 系统 SHALL 记录 `model.call` 审计事件，包含模型 ID、token 用量、延迟、数据分级

#### Scenario: 记录文件操作事件
- **WHEN** 文件写入/转换/删除操作完成
- **THEN** 系统 SHALL 记录 `file.write` / `file.convert` / `file.delete` 审计事件，包含文件路径、操作类型、备份路径

#### Scenario: 记录配置变更事件
- **WHEN** 应用设置、模型配置、沙箱策略、合规白名单等发生变更
- **THEN** 系统 SHALL 记录 `config.change` 审计事件，包含变更前后值（脱敏处理）

#### Scenario: 记录合规事件
- **WHEN** 数据分级检测、硬开关切换、合规确认等合规操作发生
- **THEN** 系统 SHALL 记录 `compliance.*` 审计事件

### Requirement: 审计日志查询与导出

系统 SHALL 提供审计日志的查询、筛选和导出功能。

#### Scenario: 按条件查询审计日志
- **WHEN** 管理员设置时间范围、事件类型、工作区等筛选条件
- **THEN** 系统 SHALL 从 `audit_logs` 表查询匹配的日志记录，支持分页

#### Scenario: 导出审计日志为 CSV
- **WHEN** 管理员点击「导出」
- **THEN** 系统 SHALL 将查询结果导出为 CSV 文件，通过原生对话框让用户选择保存位置

---

## ADDED Requirements

### Requirement: 审计日志脱敏

系统 SHALL 在记录审计日志时自动脱敏敏感信息。

#### Scenario: API Key 脱敏
- **WHEN** 审计事件详情中包含 API Key
- **THEN** 系统 SHALL 将 API Key 替换为 `[REDACTED]`

#### Scenario: 文件路径中用户名脱敏
- **WHEN** 文件路径中包含操作系统用户名
- **THEN** 系统 SHALL 将用户名部分替换为 `[USER]`

---

## API 定义

### IPC Channel

| Channel | 方向 | 请求类型 | 响应类型 |
|---------|------|----------|----------|
| `audit:list` | R→M | `AuditQuery` | `AuditLog[]` |
| `audit:export` | R→M | `AuditQuery` | `{ filePath: string }` |
| `audit:count` | R→M | `AuditQuery` | `{ total: number }` |

---

## 数据模型

复用 `spec-通天办公.md` §4.3 中的 `audit_logs` 表结构，新增索引：

```sql
-- 审计日志查询优化索引（新增）
CREATE INDEX idx_audit_workspace ON audit_logs(task_id);
CREATE INDEX idx_audit_model ON audit_logs(model_id);
```
