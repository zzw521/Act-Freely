# Schedule Cron — 定时任务管理（QwenPaw 复用增强）

> **能力域**：`schedule-cron`  
> **类型**：改造（MODIFIED）  
> **上游 SPEC**：`spec-通天办公.md` §3.4  
> **改造策略**：复用 QwenPaw Cron + 日程能力，通天办公侧新增 ScheduleView UI

---

## 概述

QwenPaw 已实现 Cron + 日程 + 日历视图能力，通天办公直接复用其定时任务引擎，通过 QwenPaw Bridge 管理定时任务的创建/编辑/删除/启停。通天办公侧新增 ScheduleView UI 页面。

---

## MODIFIED Requirements

### Requirement: 定时任务管理

系统 SHALL 通过 QwenPaw Bridge 管理定时任务的创建/编辑/删除/启停，支持 Cron 表达式和自然语言描述。

#### Scenario: 创建定时任务
- **WHEN** 用户在 ScheduleView 中创建新定时任务（设置 Cron 表达式、任务目标、使用模型）
- **THEN** 系统 SHALL 通过 Bridge 调用 QwenPaw 的 Cron 接口创建定时任务，持久化到 SQLite

#### Scenario: 启停定时任务
- **WHEN** 用户切换定时任务的启用状态
- **THEN** 系统 SHALL 通过 Bridge 调用 QwenPaw 的 Cron 启停接口

#### Scenario: 定时触发执行
- **WHEN** Cron 表达式触发时间到达
- **THEN** QwenPaw SHALL 自动创建 Task 并按配置的模型执行任务，执行结果记录到任务历史

---

## ADDED Requirements

### Requirement: ScheduleView UI

系统 SHALL 提供 ScheduleView 页面，以日历视图和列表视图展示定时任务，支持创建/编辑/删除/启停操作。

#### Scenario: 日历视图展示
- **WHEN** 用户进入 ScheduleView 页面
- **THEN** 系统 SHALL 以日历视图展示定时任务的时间安排，高亮有任务执行的日期

#### Scenario: 列表视图展示
- **WHEN** 用户切换到列表视图
- **THEN** 系统 SHALL 以表格形式展示所有定时任务（名称、Cron 表达式、下次执行时间、状态、操作按钮）

---

## API 定义

### IPC Channel（新增）

| Channel | 方向 | 请求类型 | 响应类型 |
|---------|------|----------|----------|
| `schedule:list` | R→M | `void` | `ScheduleTask[]` |
| `schedule:create` | R→M | `ScheduleCreateReq` | `ScheduleTask` |
| `schedule:update` | R→M | `ScheduleUpdateReq` | `ScheduleTask` |
| `schedule:delete` | R→M | `{ id: string }` | `{ ok: boolean }` |
| `schedule:toggle` | R→M | `{ id: string; enabled: boolean }` | `{ ok: boolean }` |

### Request/Response DTOs

```typescript
interface ScheduleTask {
  id: string;
  name: string;
  cronExpression: string;
  goal: string;            // 任务目标（自然语言）
  modelBinding?: string;   // 使用的模型
  enabled: boolean;
  nextRunAt: string;       // 下次执行时间
  lastRunAt?: string;
  lastStatus?: 'success' | 'failed' | 'running';
}

interface ScheduleCreateReq {
  name: string;
  cronExpression: string;
  goal: string;
  modelBinding?: string;
}
```

---

## 数据模型

```sql
-- 定时任务表（新增）
CREATE TABLE schedule_tasks (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  cron_expression TEXT NOT NULL,
  goal TEXT NOT NULL,
  model_binding TEXT DEFAULT '',
  enabled INTEGER DEFAULT 1,
  last_run_at TEXT,
  last_status TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_schedule_enabled ON schedule_tasks(enabled);
```
