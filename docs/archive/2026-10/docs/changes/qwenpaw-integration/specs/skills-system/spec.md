# Skills System — Skills 技能系统（QwenPaw 复用增强）

> **能力域**：`skills-system`  
> **类型**：改造（MODIFIED）  
> **上游 SPEC**：`spec-通天办公.md` §3.6  
> **改造策略**：复用 QwenPaw Skills 系统（SKILL.md 格式 + 加载机制），通天办公侧新增 SkillMarket UI 和安全审计

---

## 概述

QwenPaw 已实现完整的 Skills 系统（SKILL.md + skill.py + 市场），通天办公直接复用其格式和加载机制，通过 QwenPaw Bridge 管理技能的安装/卸载/启用/禁用。通天办公侧新增 SkillMarket UI 页面和安装前安全审计能力。

---

## MODIFIED Requirements

### Requirement: Skill 文件格式兼容

系统 SHALL 兼容 QwenPaw 的 SKILL.md 格式，同时扩展通天办公特有的元数据字段。

#### Scenario: 加载 QwenPaw 格式 Skill
- **WHEN** 安装一个标准 QwenPaw SKILL.md 格式的技能
- **THEN** SkillLoader SHALL 成功解析其元数据（name/version/description/tags/requires），并注册到 SkillRegistry

#### Scenario: 通天办公扩展元数据
- **WHEN** 技能 SKILL.md 包含通天办公扩展字段（如 `tongtian:category`、`tongtian:ui-hint`）
- **THEN** SkillLoader SHALL 解析扩展字段并存储到 `metadata` 中，供 SkillMarket UI 展示

### Requirement: Skill 安装与管理

系统 SHALL 通过 QwenPaw Bridge 管理技能的安装/卸载/启用/禁用，安装前 SHALL 执行安全审计。

#### Scenario: 从本地路径安装技能
- **WHEN** 用户通过 SkillMarket 选择本地路径安装技能
- **THEN** 系统 SHALL 通过 Bridge 调用 QwenPaw 的 Skill 加载接口，解析 SKILL.md，执行安全审计（检查恶意代码/网络请求/文件越权），审计通过后注册到 SkillRegistry

#### Scenario: 启用/禁用技能
- **WHEN** 用户在 SkillMarket 切换技能启用状态
- **THEN** 系统 SHALL 通过 Bridge 通知 QwenPaw 更新技能集，后续 Agent 执行时 SHALL 仅调用已启用的技能

### Requirement: Skill 安全审计

系统 SHALL 在技能安装前自动执行安全审计，检查恶意代码、未授权网络请求、文件越权访问等风险。

#### Scenario: 审计通过
- **WHEN** 安全审计未发现风险
- **THEN** 系统 SHALL 允许安装，标记审计结果为 `passed`

#### Scenario: 审计失败
- **WHEN** 安全审计发现恶意代码或高风险操作
- **THEN** 系统 SHALL 阻止安装，返回 `SKILL_AUDIT_FAILED` 错误，展示具体风险项

---

## ADDED Requirements

### Requirement: SkillMarket UI

系统 SHALL 提供 SkillMarket 页面，支持技能浏览、搜索、标签筛选、安装/卸载/启用/禁用操作。

#### Scenario: 浏览和搜索技能
- **WHEN** 用户进入 SkillMarket 页面
- **THEN** 系统 SHALL 展示已安装技能卡片网格，支持按名称搜索和按标签筛选

#### Scenario: 一键安装市场技能
- **WHEN** 用户在市场列表中点击「安装」
- **THEN** 系统 SHALL 下载技能包，执行安全审计，审计通过后自动安装并刷新列表

---

## API 定义

### IPC Channel（保持不变）

| Channel | 方向 | 请求类型 | 响应类型 |
|---------|------|----------|----------|
| `skill:list` | R→M | `void` | `Skill[]` |
| `skill:install` | R→M | `SkillInstallReq` | `Skill` |
| `skill:uninstall` | R→M | `{ skillId: string }` | `{ ok: boolean }` |
| `skill:enable` | R→M | `{ skillId: string; enabled: boolean }` | `{ ok: boolean }` |
| `skill:update` | R→M | `{ skillId: string }` | `Skill` |

### 改造点

- 内部实现从通天办公自研 SkillLoader 改为通过 QwenPaw Bridge 调用 QwenPaw 的 Skill 管理接口
- 安全审计复用 QwenPaw 的 Skill 扫描能力，通天办公侧增加 UI 展示

---

## 数据模型

复用 `spec-通天办公.md` §4.3 中的 `skills` 表结构，无变更。
