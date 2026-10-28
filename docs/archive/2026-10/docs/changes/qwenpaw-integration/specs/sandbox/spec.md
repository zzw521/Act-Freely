# Sandbox — 三层沙箱执行环境

> **能力域**：`sandbox`  
> **类型**：新增（ADDED）  
> **上游 SPEC**：`spec-通天办公.md` §3.8 + `spec-通天办公-qwenpaw-改造方案.md` §7.2  
> **优先级**：P0（L0/L1 为 MVP，L2 为 Phase 3）

---

## 概述

三层沙箱在 QwenPaw 基础文件防护之上，新增 L0 文件沙箱、L1 浏览器沙箱、L2 系统沙箱三层执行架构。系统根据 Step 危险等级自动选择执行层级，并提供危险操作拦截器和资源配额控制器。

---

## ADDED Requirements

### Requirement: L0 文件沙箱

系统 SHALL 在文件操作前执行路径白名单校验、操作前备份、操作失败自动回滚。L0 为默认执行层。

#### Scenario: 文件操作前备份
- **WHEN** Agent 执行文件写入/转换/整理操作
- **THEN** 系统 SHALL 先计算文件 SHA256 指纹，复制至 `.tongtian-backups/{taskId}/{stepId}/{filename}.{timestamp}.bak`，记录 FileOp 后再执行实际操作

#### Scenario: 路径白名单校验
- **WHEN** 文件操作的目标路径不在工作区 `fileScope` 白名单内
- **THEN** 系统 SHALL 拒绝操作，抛出 `PERMISSION_DENIED` 错误，并记录审计日志

#### Scenario: 操作失败自动回滚
- **WHEN** 文件操作执行失败（写入中断、格式错误等）
- **THEN** 系统 SHALL 从备份目录恢复原文件，标记 FileOp 状态为 `failed`，并通知用户

### Requirement: L1 浏览器沙箱

系统 SHALL 基于 QwenPaw 的 Playwright browser_use 能力，在无痕模式下执行浏览器自动化操作，实现 Cookie/Storage 隔离和域名白名单约束。

#### Scenario: 无痕模式执行浏览器操作
- **WHEN** Agent 需要执行网页导航/表单/抓取操作
- **THEN** 系统 SHALL 在 Playwright incognito 上下文中执行，操作结束后清除 Cookie 和 Storage

#### Scenario: 域名白名单约束
- **WHEN** 浏览器操作的目标 URL 不在域名白名单内
- **THEN** 系统 SHALL 拦截操作，弹出确认对话框让用户决定是否允许访问

### Requirement: L2 系统沙箱（Phase 3）

系统 SHALL 支持通过 Docker 容器隔离执行任意代码和系统配置修改操作，用户确认后方可升级到此层。

#### Scenario: 升级至 L2 执行
- **WHEN** Agent 需要执行危险命令或任意代码
- **THEN** 系统 SHALL 弹出确认对话框，用户确认后在 Docker 容器中执行，容器配置 seccomp/AppArmor 策略和只读根文件系统

#### Scenario: Docker 不可用降级
- **WHEN** 系统未安装 Docker 或 Docker 未运行
- **THEN** 系统 SHALL 提示用户安装 Docker，并降级至 L0 + 危险操作拦截模式

### Requirement: 危险操作拦截器

系统 SHALL 预定义危险命令模式匹配规则，在执行前扫描并拦截，要求用户确认。

#### Scenario: 拦截 CRITICAL 级别危险操作
- **WHEN** 命令匹配 `rm -rf /`、`format C:`、`reg delete` 等 CRITICAL 模式
- **THEN** 系统 SHALL 阻止执行，弹出红色警告对话框，要求用户输入确认文字（如「我确认执行」）

#### Scenario: 拦截 HIGH 级别危险操作
- **WHEN** 命令匹配 `del /f /s /q`、`shutdown /r` 等 HIGH 模式
- **THEN** 系统 SHALL 弹出确认对话框，用户点击「确认」后方可执行

### Requirement: 资源配额控制器

系统 SHALL 对每个工作区实施 CPU/内存/磁盘/网络资源配额控制，超限时自动降级或暂停。

#### Scenario: 内存超限暂停
- **WHEN** 工作区内存使用超过 `maxMemoryMB` 限制（默认 256MB）
- **THEN** QuotaController SHALL 暂停该工作区的任务执行，释放非关键资源，并通知用户

#### Scenario: 磁盘写入限速
- **WHEN** 工作区磁盘写入速率超过 `maxDiskWriteMBps`（默认 50MB/s）
- **THEN** QuotaController SHALL 对写入操作进行节流，降低写入速率至限制值以下

---

## API 定义

### IPC Channel

| Channel | 方向 | 请求类型 | 响应类型 |
|---------|------|----------|----------|
| `sandbox:get-policy` | R→M | `{ workspaceId: string }` | `SandboxPolicy` |
| `sandbox:update-policy` | R→M | `SandboxPolicy` | `SandboxPolicy` |

### SandboxManager 接口

```typescript
class SandboxManager {
  assessLevel(step: Step): SandboxLevel;
  async execute(step: Step, level: SandboxLevel): Promise<StepResult>;
  async escalate(step: Step, from: SandboxLevel, to: SandboxLevel): Promise<boolean>;
  checkQuota(workspaceId: string): QuotaStatus;
}

class DangerousOperationGuard {
  scan(command: string): DangerousOpAlert[];
  requireConfirmation(alerts: DangerousOpAlert[]): Promise<boolean>;
}

class QuotaController {
  check(limits: ResourceLimits): QuotaStatus;
  enforce(limits: ResourceLimits): void;
  report(workspaceId: string): ResourceReport;
}
```

### Error Codes

| 错误码 | 说明 |
|--------|------|
| `SANDBOX_VIOLATION` | 沙箱策略违规 |
| `SANDBOX_QUOTA_EXCEEDED` | 资源配额超限 |
| `DANGEROUS_OPERATION_BLOCKED` | 危险操作被拦截 |

---

## 数据模型

复用 `spec-通天办公.md` §4.3 中的 `sandbox_policies` 表结构。
