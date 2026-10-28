# File Processing — 文件处理（QwenPaw 复用增强）

> **能力域**：`file-processing`  
> **类型**：改造（MODIFIED）  
> **上游 SPEC**：`spec-通天办公.md` §3.3  
> **改造策略**：复用 QwenPaw 内置文件工具（read/write/edit/append + grep/glob），通天办公侧增强格式转换和安全备份

---

## 概述

QwenPaw 已实现基础文件操作工具（read/write/edit/append + grep/glob），通天办公直接复用。在此基础上，通天办公侧增强格式转换能力（docx/xlsx/pdf/csv 互转）、安全备份机制和文件浏览/预览 UI。

---

## MODIFIED Requirements

### Requirement: 基础文件操作

系统 SHALL 通过 QwenPaw Bridge 调用 QwenPaw 的内置文件工具完成 read/write/edit/append/grep/glob 操作。

#### Scenario: 读取文件
- **WHEN** Agent 需要读取文件内容
- **THEN** 系统 SHALL 通过 Bridge 调用 QwenPaw 的 `read_file` 工具，返回文件内容

#### Scenario: 写入文件
- **WHEN** Agent 需要写入文件
- **THEN** 系统 SHALL 先通过 SandboxManager 执行 L0 备份，再通过 Bridge 调用 QwenPaw 的 `write_file` 工具

#### Scenario: 搜索文件内容
- **WHEN** Agent 需要在文件中搜索特定内容
- **THEN** 系统 SHALL 通过 Bridge 调用 QwenPaw 的 `grep` / `glob` 工具

### Requirement: 安全备份机制

系统 SHALL 在所有文件写入操作前执行安全备份，支持操作失败后自动回滚。

#### Scenario: 写入前备份
- **WHEN** 文件写入操作即将执行
- **THEN** 系统 SHALL 计算文件 SHA256 指纹，复制至 `.tongtian-backups/{taskId}/{stepId}/`，记录 FileOp

#### Scenario: 失败后回滚
- **WHEN** 文件写入操作失败
- **THEN** 系统 SHALL 从备份恢复原文件，标记 FileOp 为 `failed`

### Requirement: 格式转换增强

系统 SHALL 在 QwenPaw 文件工具基础上，增强 docx/xlsx/pdf/csv 格式互转能力。格式转换在通天办公主进程中实现（使用 Node.js 库），不依赖 QwenPaw。

#### Scenario: xlsx 转 csv
- **WHEN** Agent 需要将 xlsx 文件转换为 csv
- **THEN** 系统 SHALL 使用 SheetJS 读取 xlsx，使用 papaparse 输出 csv

#### Scenario: pdf 转 docx
- **WHEN** Agent 需要将 pdf 文件转换为 docx
- **THEN** 系统 SHALL 使用 pdf-parse 提取文本，使用 mammoth 生成 docx

---

## ADDED Requirements

### Requirement: FilePanel 文件浏览 UI

系统 SHALL 提供 FilePanel 页面，支持文件目录浏览、文件预览和文件操作。

#### Scenario: 浏览文件目录
- **WHEN** 用户进入 FilePanel 页面
- **THEN** 系统 SHALL 通过 `file:list` IPC 获取目录内容，以树形结构展示文件和文件夹

#### Scenario: 预览文件
- **WHEN** 用户点击文件
- **THEN** 系统 SHALL 通过 `file:preview` IPC 获取文件内容，在右侧面板展示预览（支持 txt/md/csv 文本预览，docx/xlsx/pdf 渲染预览）

---

## API 定义

### IPC Channel（保持不变）

| Channel | 方向 | 请求类型 | 响应类型 |
|---------|------|----------|----------|
| `file:list` | R→M | `{ dirPath: string }` | `FileInfo[]` |
| `file:read` | R→M | `{ filePath: string }` | `{ content: string }` |
| `file:preview` | R→M | `{ filePaths: string[] }` | `FilePreview[]` |

### 改造点

- 基础文件操作从通天办公自研改为通过 QwenPaw Bridge 调用 QwenPaw 内置文件工具
- 格式转换在通天办公主进程实现（Node.js 库），不经过 QwenPaw
- 安全备份在通天办公主进程实现，作为 Bridge 调用前的前置步骤

---

## 数据模型

复用 `spec-通天办公.md` §4.3 中的 `file_ops` 表结构，无变更。
