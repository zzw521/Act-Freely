# TongTian UI — 通天办公独立 UI

> **能力域**：`tongtian-ui`  
> **类型**：新增（ADDED）  
> **上游 SPEC**：`spec-通天办公-qwenpaw-改造方案.md` §6  
> **优先级**：P0

---

## 概述

通天办公 UI 完全独立于 QwenPaw，基于 React 18 + MUI + Tailwind CSS + Zustand 构建。采用通天办公设计规范（清新蓝 #3FA7F4 + 薄荷绿 #2ED3B7 + 暖琥珀 #F6B73C），包含 Workbench、ModelHub、SkillMarket、WorkspacePanel、FilePanel、ScheduleView、Settings、AuditView 共 8 个核心页面。

---

## ADDED Requirements

### Requirement: 设计规范与主题系统

系统 SHALL 实现统一的设计主题系统，包含色彩、字体、圆角、阴影等设计 Token，所有页面 SHALL 遵循同一主题。

#### Scenario: 主题 Token 应用
- **WHEN** 渲染任意页面组件
- **THEN** 系统 SHALL 使用 MUI ThemeProvider 注入设计 Token：主色 #3FA7F4、辅色 #2ED3B7、强调色 #F6B73C、圆角 16px/10px、阴影 `0 6px 24px rgba(31,42,55,.06)`

#### Scenario: 暗色模式切换
- **WHEN** 用户在设置中切换暗色模式
- **THEN** 系统 SHALL 切换 MUI 暗色主题，所有页面自动适配

### Requirement: Workbench 对话工作台

Workbench SHALL 提供完整的对话交互界面，包含输入框、Plan 预览面板、步骤执行流、交付物展示，通过 IPC 与 QwenPaw Bridge 通信。

#### Scenario: 发起对话
- **WHEN** 用户在输入框输入自然语言目标并提交
- **THEN** Workbench SHALL 通过 `chat:submit` IPC 发送请求，展示加载动画，接收流式事件并实时渲染

#### Scenario: 审阅计划
- **WHEN** Agent 生成 Plan 并推送 `plan` 事件
- **THEN** Workbench SHALL 在 PlanView 面板展示步骤列表，用户可点击「批准」「拒绝」或修改步骤

#### Scenario: 实时步骤进度
- **WHEN** Agent 执行步骤并推送 `step_start` / `step_progress` / `step_done` 事件
- **THEN** Workbench SHALL 在步骤流中实时更新每个步骤的状态和中间输出

### Requirement: ModelHub 模型管理页

ModelHub SHALL 提供模型列表、添加表单、连通性测试、用量统计、评测雷达图等 UI 组件。

#### Scenario: 展示模型列表
- **WHEN** 用户进入 ModelHub 页面
- **THEN** 系统 SHALL 通过 `model:list` IPC 获取所有模型配置，以卡片列表形式展示（名称、供应商、状态、是否默认）

#### Scenario: 添加模型表单
- **WHEN** 用户点击「添加模型」
- **THEN** 系统 SHALL 弹出表单对话框，包含模型名称、供应商类型（OpenAI兼容/Ollama/预置）、Base URL、API Key、模型标识、上下文窗口大小等字段

### Requirement: SkillMarket 技能市场页

SkillMarket SHALL 提供技能浏览、搜索、安装、卸载、启用/禁用等操作界面。

#### Scenario: 浏览已安装技能
- **WHEN** 用户进入 SkillMarket 页面
- **THEN** 系统 SHALL 通过 `skill:list` IPC 获取技能列表，以卡片网格展示（名称、描述、标签、版本、状态）

#### Scenario: 安装新技能
- **WHEN** 用户点击「安装」并选择来源（市场/本地路径/URL）
- **THEN** 系统 SHALL 通过 `skill:install` IPC 安装技能，安装前自动执行安全审计，审计失败则阻止安装并提示原因

### Requirement: WorkspacePanel 工作区管理页

WorkspacePanel SHALL 提供工作区列表、创建、切换、暂停/恢复、归档等操作界面。

#### Scenario: 切换工作区
- **WHEN** 用户在工作区列表中点击切换
- **THEN** 系统 SHALL 通过 `workspace:switch` IPC 切换活跃工作区，Workbench 和侧边栏 SHALL 同步更新为新工作区的上下文

### Requirement: Settings 设置页

Settings SHALL 提供应用设置、频道配置、安全策略、合规开关等操作界面。

#### Scenario: 修改安全设置
- **WHEN** 用户在设置页面修改沙箱策略或合规开关
- **THEN** 系统 SHALL 通过对应 IPC 更新配置，并实时生效

### Requirement: AuditView 审计日志页

AuditView SHALL 提供审计日志的查询、筛选、导出功能。

#### Scenario: 查询审计日志
- **WHEN** 用户在 AuditView 设置时间范围和筛选条件
- **THEN** 系统 SHALL 通过 `audit:list` IPC 查询日志，以表格形式展示（时间、事件类型、操作者、详情）

---

## API 定义

UI 层通过 IPC Channel 与主进程通信，不直接调用 QwenPaw API。所有 IPC Channel 定义见各能力域 spec 文件。

### 页面路由

```typescript
const routes = {
  '/': 'Workbench',
  '/model-hub': 'ModelHub',
  '/skill-market': 'SkillMarket',
  '/workspaces': 'WorkspacePanel',
  '/files': 'FilePanel',
  '/schedule': 'ScheduleView',
  '/settings': 'Settings',
  '/audit': 'AuditView',
};
```

### 状态管理

```typescript
// Zustand Store 划分
useChatStore      // 对话状态（消息列表、Plan、步骤流）
useModelStore     // 模型配置列表、当前选中模型
useWorkspaceStore // 工作区列表、当前活跃工作区
useSkillStore     // 技能列表、安装状态
useSettingsStore  // 应用设置、安全策略
useAuditStore     // 审计日志查询结果
```

---

## 数据模型

无独立数据模型。UI 层数据来自主进程 IPC 响应，状态存储在 Zustand Store 中。
