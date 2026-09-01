# 通天办公 · 技术规格说明书 (SPEC)

> **文档版本**：v2.1
> **更新日期**：2026-08-05
> **负责人**：架构组
> **关联 PRD**：`通天办公-PRD.html` v1.3（增强版）
> **关联设计**：`tongtian-office-mvp-design.md` v2.1
> **架构演进**：`docs/spec/spec-通天办公-qwenpaw-改造方案.md` v2.0（Electron → Tauri 架构演进方案）
> **配套文档**：`spec-通天办公-云端与账号体系.md` v1.0、`spec-通天办公-占位能力清单.md` v1.0、`docs/changes/workbench-interaction/spec.md` v1.0

> **v2.0 重大变更**：基础架构由「Electron + Node/TS 主进程即后端」整体切换为「**Tauri 2.x + Rust 全栈核心**」。Agent 引擎、Model Hub、文件处理、存储、Skills、MCP、沙箱全部以 Rust 实现；Python 仅作为可选 sidecar 承载个别辅助工具。前端 React 技术栈保持不变。
>
> **v2.1 变更摘要**：① 新增 §4.4 本地数据目录布局（安装目录 + 运行目录双目录契约）；② 明确 AgentScope/QwenPaw 仅作设计思路参考、不作为运行时依赖，python-bridge 职责收窄为纯辅助工具；③ 新增 §6.5 设计原创性与合规原则；④ 新增 §10 云端版本架构预留。

---

## 目录

- [1. 技术架构总览](#1-技术架构总览)
- [2. 技术选型对比与推荐](#2-技术选型对比与推荐)
- [3. 模块详细设计](#3-模块详细设计)
  - [3.1 Agent 引擎](#31-agent-引擎-cratesagent)
  - [3.2 Model Hub](#32-model-hub-cratesmodel-hub)
  - [3.3 文件处理](#33-文件处理-cratesfile-ops)
  - [3.4 自动化引擎](#34-自动化引擎后续版本)
  - [3.5 多智能体工作区](#35-多智能体工作区-cratesworkspace)
  - [3.6 Skills 技能系统](#36-skills-技能系统-cratesskills)
  - [3.7 MCP 集成架构](#37-mcp-集成架构-cratesmcp)
  - [3.8 本地执行沙箱](#38-本地执行沙箱-cratessandbox)
- [4. 数据模型](#4-数据模型)
- [5. API 与接口契约](#5-api-与接口契约)
- [6. 安全架构](#6-安全架构)
- [7. 部署与打包方案](#7-部署与打包方案)
- [8. 测试策略](#8-测试策略)
- [9. 非功能指标](#9-非功能指标)
- [10. 云端版本架构预留](#10-云端版本架构预留)

---

## 1. 技术架构总览

### 1.1 整体架构图

```
┌─────────────────────────────────────────────────────────┐
│                  Tauri 2.x 桌面壳                        │
│  ┌──────────────────────┐  ┌──────────────────────────┐ │
│  │  WebView 前端 (UI)    │  │   Rust Core (后端核心)     │ │
│  │                      │  │                          │ │
│  │ React 18 + MUI       │  │  ┌────────────────────┐  │ │
│  │ Tailwind + Zustand   │  │  │  Agent 引擎         │  │ │
│  │                      │  │  │  Planner/Executor   │  │ │
│  │  Pages:              │  │  └─────────┬──────────┘  │ │
│  │  - Workbench         │  │            │             │ │
│  │  - ModelHub          │←─┼─ invoke ───┤             │ │
│  │  - Settings          │  │  + events  │             │ │
│  │                      │  │  ┌─────────┴──────────┐  │ │
│  │  Components:         │  │  │  Model Hub          │  │ │
│  │  - PlanView          │  │  │  Adapter/Router/    │  │ │
│  │  - ChatStream        │  │  │  Observer           │  │ │
│  │  - FilePanel         │  │  └─────────┬──────────┘  │ │
│  │  - ModelForm         │  │            │             │ │
│  └──────────────────────┘  │  ┌─────────┴──────────┐  │ │
│                            │  │  文件处理 (file-ops) │  │ │
│                            │  │  Read/Write/        │  │ │
│                            │  │  Convert/Organize   │  │ │
│                            │  └────────────────────┘  │ │
│                            │                          │ │
│                            │  ┌────────────────────┐  │ │
│                            │  │  存储层 (storage)    │  │ │
│                            │  │  Vault (keyring)    │  │ │
│                            │  │  DB (rusqlite)      │  │ │
│                            │  └────────────────────┘  │ │
│                            │                          │ │
│                            │  ┌────────────────────┐  │ │
│                            │  │ python-bridge      │  │ │
│                            │  │ (可选 Python 辅助    │  │ │
│                            │  │  sidecar, 按需拉起) │  │ │
│                            │  └────────────────────┘  │ │
│                            └──────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
          │                    │
          ▼                    ▼
   ┌──────────┐        ┌──────────────┐
   │  Ollama  │        │  云端模型 API  │
   │ (本地模型)│        │ Qwen/DeepSeek │
   └──────────┘        │ OpenAI/自定义  │
                        └──────────────┘
```

### 1.2 分层职责

| 层级 | 技术栈 | 职责 |
|------|--------|------|
| **桌面壳** | Tauri 2.x (Rust) | 窗口管理、系统托盘、原生对话框、capability 权限控制、自动更新 |
| **WebView 前端** | React 18 + TypeScript + MUI + Tailwind + Zustand | UI 渲染、用户交互、状态管理 |
| **Rust 核心（后端）** | Rust 2021 edition + tokio 异步运行时 | Agent 引擎、Model Hub、文件处理、工作区、Skills、MCP、沙箱、本地存储 |
| **IPC 桥梁** | Tauri command (`invoke`) + event (`emit/listen`) | 前端 ↔ Rust core 类型安全通信（serde 序列化） |
| **持久化** | rusqlite (SQLite) + keyring (系统密钥库) | 任务/会话存储、API Key 加密存储 |
| **外部模型** | reqwest (HTTPS, OpenAI 兼容协议) | 云端模型 API 调用 |
| **本地模型** | reqwest → Ollama HTTP API | 本地模型推理服务 |
| **Python 辅助（可选）** | python-bridge sidecar（JSON-over-stdio） | 个别 Rust 生态不成熟的辅助工具（复杂文档解析、docx→pdf 等），默认不打包 |

### 1.3 设计原则

1. **本地优先 (Local-first)**：文件操作在 Rust core 完成，敏感内容仅在授权后上送模型
2. **Rust 核心单栈**：后端全部 Rust（Cargo workspace），前端 TS，类型经 `ts-rs` 自动生成共享定义（`shared/` 等价物）
3. **最小权限**：WebView 无 Node/系统 API 访问能力，仅通过 capability 白名单暴露受控 command；Agent 操作默认 L0 层，逐层升级需用户确认
4. **安全默认**：API Key 经系统密钥库加密存储、文件操作前备份、关键操作人工确认、第三方 Skill 安装前自动安全审计
5. **渐进增强**：MVP 优先交付核心闭环（对话 + 文件 + 模型接入），后续扩展 Skills/MCP/多智能体/沙箱三层架构
6. **开放可扩展**：Skills 文件系统标准 + MCP 协议集成，构建开放工具生态
7. **Python 最小化**：不依赖 Python 运行时；仅在个别辅助工具场景按需启用 sidecar，且受 Mock 开关与可选打包控制
8. **自研优先、思路借鉴**：Agent 引擎与全部核心能力由 Rust 自研实现；QwenPaw/AgentScope 等开源项目仅作设计思路参考（架构范式、能力矩阵），**不作为运行时依赖、不复制其代码**（见 §6.5 合规原则）
9. **数据目录契约**：应用遵循「安装目录只读 + 运行目录承载全部用户数据」的双目录布局（见 §4.4），卸载不丢数据、可整体迁移备份

---

## 2. 技术选型对比与推荐

### 2.1 方案定义

v2.0 对基础架构重新评估，候选方案如下：

| 方案 | 桌面壳 | 后端核心 | 模型接入 |
|------|--------|----------|----------|
| **A: Tauri + Rust 全栈（本版选定）** | Tauri 2.x | Rust core（Cargo workspace） | reqwest 直连 API / Ollama HTTP |
| **B: Electron + Node/TS（v1.x 旧架构）** | Electron | Node/TS 主进程 | Node HTTP 客户端 |
| **C: Electron + Python 引擎（QwenPaw 嵌入）** | Electron | Python 子进程引擎 + TS 桥接 | Python 直连 API |

### 2.2 八维度量化评分

| 维度 (权重) | A: Tauri + Rust | B: Electron + Node/TS | C: Electron + Python | 说明 |
|-------------|-----------------|----------------------|----------------------|------|
| **打包体积** (高) | ★★★★★ (~15-25MB) | ★★★☆☆ (~120MB) | ★★☆☆☆ (~170-270MB) | Tauri 无 Chromium 捆绑 |
| **冷启动速度** (高) | ★★★★★ (< 1s) | ★★★☆☆ (~2s) | ★★☆☆☆ (子进程 1-3s) | 原生二进制 + 系统 WebView |
| **内存占用** (高) | ★★★★★ (~100-150MB) | ★★★☆☆ (~300MB) | ★★☆☆☆ (双运行时) | 系统 WebView 复用 |
| **文件处理能力** (高) | ★★★★☆ (calamine/docx-rs 等，个别格式需 sidecar) | ★★★★☆ (mammoth/xlsx/pdf) | ★★★★★ (pandas/openpyxl) | Rust 生态已覆盖核心 5 格式 |
| **安全模型** (高) | ★★★★★ (默认最小 capability + 内存安全) | ★★★☆☆ (需自行收敛 nodeIntegration) | ★★★☆☆ | Tauri 默认拒绝制 |
| **并发性能** (中) | ★★★★★ (tokio async + 无 GIL) | ★★★☆☆ (事件循环) | ★★☆☆☆ (GIL) | Rust 并发模型最优 |
| **开发维护成本** (高) | ★★★☆☆ (Rust 学习曲线) | ★★★★★ (单一 TS 栈) | ★★☆☆☆ (双语言栈) | 以 crate 化模块边界缓解 |
| **桌面集成度** (高) | ★★★★☆ (plugin 生态成熟) | ★★★★★ | ★★★★★ | Tauri 2.x plugin 覆盖托盘/对话框/更新 |
| **加权总分** | **4.4 / 5** | **3.6 / 5** | **2.9 / 5** | |

### 2.3 核心判断

**v1.x 选择 Electron 的三个前提在 v2.0 均已反转：**

1. **「后端逻辑重用 TS 生态」不再成立**：产品核心是模型 API 编排（HTTP 客户端 + 状态机 + 文件管道），Rust 生态（reqwest/tokio/calamine/docx-rs）已可完整覆盖，且性能与内存占用显著更优；
2. **「打包体积与启动速度可接受」不再成立**：企业分发场景对体积（<30MB）与秒开体验提出硬性要求，Tauri 安装包约为 Electron 的 1/6；
3. **「Rust 学习曲线不可承受」已被工程手段缓解**：按 crate 划分模块边界、AI 辅助开发、以及「Agent 逻辑 = 状态机 + HTTP 编排」的低并发复杂度，使 Rust 投入可控。

**「通天办公」的本质仍是模型 API 的编排与调用者，而非模型推理引擎**——这一判断不变，因此无需 Python AI 生态（本地推理由 Ollama 独立承担），Rust 作为编排层的性能与安全性收益成为主导因素。

### 2.4 最终推荐

**方案 A：`Tauri 2.x + Rust 全栈核心`**，理由：

1. **体积与性能**：安装包 ~20MB、冷启动 < 1s、空闲内存 ~120MB，全面优于 Electron 方案；
2. **安全默认**：capability 默认拒绝制 + Rust 内存安全，攻击面显著收敛；
3. **单二进制分发**：Rust core 静态链接，无 Node/Python 运行时依赖，自动更新为单包替换；
4. **并发模型**：tokio 异步运行时天然适配「多工作区并发 + 流式模型调用」场景。

**Python 的定位（辅助而非依赖）**：

```
默认形态：纯 Rust 全栈，无 Python 运行时
    ↓ 当遇到 Rust 生态不成熟的个别工具时
可选形态：+ python-bridge sidecar（JSON-over-stdio 契约）
    适用场景：docx→pdf 高保真转换（LibreOffice headless）、
             复杂版式文档解析、桌面 OCR 等
    约束：按需拉起、独立进程隔离、默认不随安装包分发（用户可选装）
```

### 2.5 Rust 生态 crate 选型表

| 领域 | crate | 版本基线 | 用途 |
|------|-------|----------|------|
| 异步运行时 | `tokio` | 1.x | 全局异步运行时（multi-thread） |
| HTTP 客户端 | `reqwest` | 0.12.x | 模型 API 调用（支持 stream/SSE） |
| 序列化 | `serde` / `serde_json` | 1.x | command/event 载荷、配置 |
| 错误处理 | `thiserror` + `anyhow` | 1.x / 1.x | 库错误枚举 / 应用级错误传播 |
| 日志追踪 | `tracing` + `tracing-subscriber` | 0.1.x | 结构化日志、span 埋点 |
| SQLite | `rusqlite` (bundled) | 0.32.x | 任务/会话/配置持久化 |
| 密钥库 | `keyring` | 3.x | Win Credential Manager / macOS Keychain / Secret Service |
| 加解密 | `aes-gcm` + `argon2` | 0.10.x / 0.5.x | 配置加密、密钥派生 |
| MCP 客户端 | `rmcp` | 0.x | stdio/SSE/streamable-http 三传输 |
| Excel 读 | `calamine` | 0.26.x | xlsx/xls 读取 |
| Excel 写 | `rust_xlsxwriter` | 0.79.x | xlsx 生成 |
| Word | `docx-rs` | 0.4.x | docx 读写 |
| PDF 解析 | `pdf-extract` / `lopdf` | — | pdf 文本提取 / 结构操作 |
| CSV | `csv` | 1.x | csv 读写 |
| Markdown | `pulldown-cmark` | 0.12.x | md 解析 |
| 定时任务 | `tokio-cron-scheduler` | 0.13.x | Cron 调度 |
| 桌面控制 | `enigo` | 0.2.x | 键鼠模拟（P1） |
| 浏览器自动化 | `headless_chrome` | 1.x | CDP 驱动（L1 沙箱） |
| 正则 | `regex` | 1.x | 危险操作扫描、敏感数据检测 |
| 测试 | `mockall` / `wiremock` / `tempfile` | — | 单测 mock / HTTP mock / 临时目录 |
| 前后端类型同步 | `ts-rs` | 10.x | Rust struct → TS 类型自动生成 |

### 2.6 开发环境与初始化脚本规范

**工具链要求**：

- Rust stable（含 clippy/rustfmt）+ Tauri CLI 2.x
- Node.js 20+（仅前端构建，pnpm/npm 均可）
- Windows 需 WebView2 Runtime（Win10 1803+ 默认内置）与 MSVC Build Tools；macOS 需 Xcode CLT；Linux 需 webkit2gtk 等系统库

**初始化脚本规范**（遵循项目开发规范）：工程根目录提供 `init.sh`（并配套 `init.ps1`），职责：

1. **安装依赖**：检测并执行 `cargo fetch` 与 `npm install`，已安装则跳过（幂等）；
2. **启动服务**：按 `MOCK_ENABLED` 环境变量决定模式——`true`（默认，开发阶段）启用模型 API Mock；`false`（联调阶段）调用真实外部接口；执行 `npm run tauri dev`；
3. **输出访问信息**：启动完成后打印前端 dev server 地址、当前运行模式（Mock/真实）、日志目录路径。

**外部接口 Mock 开关规范**（遵循项目开发规范）：

| 依赖 | Mock 实现 | 开关 |
|------|-----------|------|
| 云端模型 API | `wiremock` 本地服务器回放预置响应 | 环境变量 `MOCK_ENABLED=true` + cargo feature `mock` |
| Ollama | 同上（回放 chat/tags 接口） | 同上 |
| MCP 服务器 | 内置 `mock-mcp` 进程（stdio 回声工具） | 同上 |
| Python sidecar | `MockSidecar`（进程内直接返回预置结果） | 同上 |

---

## 3. 模块详细设计

### 3.0 工程结构总览（Cargo workspace）

```
tongtian-office/
├─ src-tauri/
│  ├─ Cargo.toml              # workspace root
│  ├─ tauri.conf.json         # Tauri 配置（bundle/security/capabilities）
│  ├─ capabilities/           # 权限白名单（JSON）
│  ├─ src/                    # app 壳 crate：窗口、command 注册、状态装配
│  │  ├─ main.rs
│  │  ├─ commands/            # #[tauri::command] 按域拆分
│  │  └─ state.rs             # AppState（持有各模块单例）
│  ├─ crates/
│  │  ├─ agent/               # Agent 引擎（规划/执行/状态机）
│  │  ├─ model-hub/           # 接入/路由/观测三层
│  │  ├─ file-ops/            # 文件读写/转换/整理/备份
│  │  ├─ storage/             # rusqlite + keyring vault
│  │  ├─ workspace/           # 多智能体工作区 + ACP
│  │  ├─ skills/              # Skill 加载/注册/审计
│  │  ├─ mcp/                 # MCP 客户端与安全守卫
│  │  ├─ sandbox/             # 三层沙箱/配额/危险操作拦截
│  │  └─ python-bridge/       # 可选 Python sidecar（仅辅助工具，不含任何引擎）
│  └─ migrations/             # SQLite DDL 迁移
├─ src/                       # React 前端（原 renderer）
├─ shared/                    # ts-rs 自动生成的 TS 类型
├─ python-tools/              # 可选 Python 辅助脚本（默认不打包）
└─ init.sh / init.ps1         # 初始化脚本
```

### 3.1 Agent 引擎 (`crates/agent`)

> **实现定位（v2.1 明确）**：Agent 引擎为 Rust 原生实现（状态机 + Plan-and-Execute + ReAct 循环），不引入任何第三方 Agent 框架运行时。QwenPaw/AgentScope 等项目的成熟设计（ReAct 循环、Plan Mode、工具注册、记忆分层）仅作**思路参考**，其代码与依赖不进入本工程；开发期如存在临时 Python 引擎（如 `engine/server.py`），属过渡手段，随本 crate 落地后下线（见演进方案 §4）。

#### 3.1.1 状态机

```
                 ┌─────────┐
    用户输入 ──→ │  idle   │
                 └────┬────┘
                      │ chat_submit
                 ┌────▼────┐
                 │planning │ ← LLM 生成 Plan
                 └────┬────┘
                      │ plan 事件 → 前端审阅
                 ┌────▼──────────┐
                 │pending_review │ ← 等待用户审阅
                 └────┬──────────┘
          ┌───────────┼───────────┐
     approve      reject       modify
          │           │           │
    ┌─────▼─────┐ ┌──▼──┐  ┌─────▼─────┐
    │ executing │ │done │  │ re-plan   │ → 回到 planning
    └─────┬─────┘ └─────┘  └───────────┘
          │
    ┌─────┼─────────┐
    │     │         │
  step  step    pause(用户)
  done  failed     │
    │     │    ┌───▼───┐
    │     │    │paused │
    │     │    └───┬───┘
    │     │   resume│rollback
    │     │    │    │
    └─────┴────┴────┘
          │
    ┌─────▼─────┐
    │delivering │ → 汇总产物
    └─────┬─────┘
          │
    ┌─────▼─────┐
    │   done    │
    └───────────┘
```

状态机以 Rust 枚举 + 显式转移函数实现，非法转移返回 `AgentError::IllegalTransition`：

```rust
// crates/agent/src/state.rs
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, TS)]
#[ts(export)]
pub enum TaskStatus {
    Idle, Planning, PendingReview, Executing,
    Paused, Delivering, Done, Failed, RolledBack,
}

impl TaskStatus {
    /// 合法转移校验；非法转移返回 Err
    pub fn transition(self, event: TaskEvent) -> Result<TaskStatus, AgentError>;
}
```

#### 3.1.2 核心模块职责

| 模块 | 职责 | 关键方法 |
|------|------|----------|
| `engine.rs` | 任务生命周期主循环、状态机驱动 | `submit(goal)`, `review(action)`, `control(action)` |
| `planner.rs` | 调用 LLM 生成结构化 Plan（serde 反序列化 + JSON Schema 校验） | `generate_plan(goal, ctx) -> Result<Plan>` |
| `executor.rs` | 逐步执行 Plan，管理回滚栈（tokio task） | `execute(plan)`, `rollback_to(step_id)` |
| `types.rs` | Task/Plan/Step 类型（`ts-rs` 导出前端类型） | 与 `shared/` TS 类型自动同步 |
| `events.rs` | 事件广播（`tokio::sync::broadcast`） | 订阅 → Tauri event 桥接 |

#### 3.1.3 事件流设计

```rust
// crates/agent/src/events.rs
#[derive(Clone, Serialize, TS)]
#[ts(export)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum AgentEvent {
    Plan { plan: Plan },
    StepStart { step_id: String, name: String },
    StepProgress { step_id: String, chunk: String },
    StepDone { step_id: String, result: String },
    StepFailed { step_id: String, error: String },
    Deliver { artifacts: Vec<FileOp> },
    Done { task_id: String },
    Error { code: String, message: String },
}
```

引擎内部经 `broadcast::Sender<AgentEvent>` 广播；app 壳 crate 订阅后经 `app.emit("agent://event", &event)` 推送至前端（见 §5.1）。

#### 3.1.4 中断与回滚机制

```
执行中 Step[i]:
  - 每个 Step 执行前，若 step.rollbackable = true：
    - 文件操作：创建备份至 .tongtian-backups/{taskId}/{stepId}/
    - 记录 FileOp 到回滚栈（内存 Vec<FileOp> + SQLite 持久化）
  - 用户触发 rollback_to(stepId)：
    1. 向执行 task 发送取消令牌（CancellationToken）
    2. 将当前 Step 标记为 rolled_back
    3. 倒序遍历回滚栈（从当前步向前到目标步）
    4. 对每个 FileOp 执行反向操作（copy backup → original）
    5. 清理回滚栈中已回滚的条目
    6. 恢复至目标步的 pending 状态
```

#### 3.1.5 错误处理

`crates/agent` 对外暴露 `AgentError` 枚举（`thiserror`），app 壳统一转换为前端 `AppError`（见 §5.3）：

```rust
#[derive(Debug, thiserror::Error)]
pub enum AgentError {
    #[error("illegal state transition: {0:?} -> {1:?}")]
    IllegalTransition(TaskStatus, TaskEvent),
    #[error("plan parse failed: {0}")]
    PlanParse(String),
    #[error(transparent)]
    Model(#[from] ModelHubError),
    #[error(transparent)]
    File(#[from] FileOpsError),
    #[error("task cancelled")]
    Cancelled,
}
```

### 3.2 Model Hub (`crates/model-hub`)

#### 3.2.1 三层架构

```
┌──────────────────────────────────────────────────┐
│                    Model Hub                      │
│                                                   │
│  ┌─────────────────┐                              │
│  │  接入层 Adapter   │  统一 trait chat(req)        │
│  │                  │                              │
│  │ OpenAiCompatAdapt│  → 自定义 + 预置云端         │
│  │ OllamaAdapter    │  → 本地 Ollama               │
│  │ PresetAdapter    │  → 内置厂商（需 Key）         │
│  └────────┬────────┘                              │
│           │                                       │
│  ┌────────▼────────┐                              │
│  │  路由层 Router   │  按策略选择 Adapter            │
│  │                  │                              │
│  │ ① 敏感度检测     │  → force_local 强制本地      │
│  │ ② 智能体绑定     │  → agent_binding 优先        │
│  │ ③ 全局默认      │  → 兜底模型                  │
│  │ ④ 降级链        │  → 备用模型列表              │
│  └────────┬────────┘                              │
│           │                                       │
│  ┌────────▼────────┐                              │
│  │  观测层 Observer │  全链路监控                   │
│  │                  │                              │
│  │ Metering         │  → Token 用量/成本统计       │
│  │ Quality          │  → 延迟/错误率/成功率        │
│  │ Audit            │  → 调用日志/审计事件         │
│  └─────────────────┘                              │
└──────────────────────────────────────────────────┘
```

#### 3.2.2 Adapter 接口契约

```rust
// crates/model-hub/src/adapter/mod.rs
#[async_trait]
pub trait ModelAdapter: Send + Sync {
    fn source(&self) -> ModelSource;
    fn display_name(&self) -> &str;

    /// 连通性自测
    async fn health_check(&self) -> HealthResult;

    /// 核心调用接口
    async fn chat(&self, req: ChatRequest) -> Result<ChatResponse, ModelHubError>;

    /// 流式调用（SSE 逐块返回）
    fn chat_stream(
        &self,
        req: ChatRequest,
    ) -> BoxStream<'static, Result<ChatStreamChunk, ModelHubError>>;
}

#[derive(Serialize, Deserialize, TS)]
#[ts(export)]
pub struct ChatRequest {
    pub messages: Vec<ChatMessage>,
    pub model: String,
    pub temperature: Option<f32>,
    pub max_tokens: Option<u32>,
    pub stream: bool,
}

#[derive(Serialize, Deserialize, TS)]
#[ts(export)]
pub struct ChatResponse {
    pub content: String,
    pub usage: TokenUsage,
    pub latency_ms: u64,
    pub model: String,
}

#[derive(Serialize, Deserialize, TS)]
#[ts(export)]
pub struct ChatStreamChunk {
    pub content: String,      // 增量文本
    pub done: bool,
    pub usage: Option<TokenUsage>,
}
```

#### 3.2.3 降级与熔断实现

```rust
// crates/model-hub/src/router.rs (伪代码)
impl ModelRouter {
    async fn route(&self, task: &Task) -> Arc<dyn ModelAdapter> {
        // ① 敏感度检测
        if self.has_sensitive_data(task) && self.local_adapter.is_some() {
            self.observer.log("route:sensitive_forced_local", task.id);
            return self.local_adapter.clone().unwrap();
        }
        // ② 智能体绑定
        if let Some(binding) = self.get_agent_binding(task) {
            return self.get_adapter(&binding);
        }
        // ③ 全局默认
        self.default_adapter.clone()
    }

    async fn call_with_fallback(
        &self,
        req: &ChatRequest,
        adapters: &[Arc<dyn ModelAdapter>],
    ) -> Result<ChatResponse, ModelHubError> {
        for adapter in adapters {
            match timeout(Duration::from_secs(8), adapter.chat(req.clone())).await {
                Ok(Ok(resp)) => {
                    self.observer.record_success(adapter.source(), &resp);
                    return Ok(resp);
                }
                _ => {
                    self.observer.record_failure(adapter.source());
                    if self.should_circuit_break(adapter.source()) {
                        self.circuit_break(adapter.source(), Duration::from_secs(180));
                    }
                    // 继续至下一个备用 adapter
                }
            }
        }
        Err(ModelHubError::AllModelsUnavailable)
    }
}
```

#### 3.2.4 熔断指标（来自 PRD §8.11）

| 事件 | 阈值 | 动作 |
|------|------|------|
| 主模型超时 | P95 > 8s 或连续 3 次 | 切换备用模型 |
| 错误率 | 5 分钟内 > 15% | 降级 + 告警 |
| 限流 (429) | 触发即生效 | 指数退避重试 (1s→2s→4s→8s, 最多 4 次) |
| 全部不可用 | — | 暂停任务 + 通知用户 |

### 3.3 文件处理 (`crates/file-ops`)

#### 3.3.1 格式支持矩阵

| 格式 | 读取 | 写入 | 转换 | 依赖 crate | 备注 |
|------|:--:|:--:|:--:|--------|------|
| `.docx` | ✓ | ✓ | ✓→md/txt；→pdf 经 sidecar | `docx-rs` | docx→pdf 高保真转换经 python-bridge（LibreOffice headless） |
| `.xlsx` | ✓ | ✓ | ✓→csv | `calamine` + `rust_xlsxwriter` | |
| `.pdf` | ✓ | ✓ | ✓→txt；→docx 经 sidecar | `pdf-extract` + `lopdf` | pdf→docx 复杂版式经 python-bridge |
| `.csv` | ✓ | ✓ | ✓→xlsx | `csv` | |
| `.txt/.md` | ✓ | ✓ | ✓ | `std::fs` + `pulldown-cmark` | |

> 标注「经 sidecar」的转换：默认降级为纯文本/基础版式转换（纯 Rust）；用户安装可选 Python 扩展包后自动升级为高保真转换。Mock 开关开启时 sidecar 调用由 `MockSidecar` 返回预置结果。

#### 3.3.2 转换管道

```
源文件 → [Reader] → 中间表示(IR) → [Transformer] → 目标 IR → [Writer] → 目标文件

IR 定义（crates/file-ops/src/ir.rs）：
  DocIr    { paragraphs: Vec<Paragraph>, tables: Vec<Table> }
  SheetIr  { sheets: Vec<Sheet { rows: Vec<Vec<Cell>> }> }
  PlainIr  { text: String }

示例: .xlsx → .csv
  xlsx → calamine read → SheetIr → csv::Writer → .csv

示例: .docx → .md
  docx → docx-rs parse → DocIr → pulldown-cmark emit → .md
```

#### 3.3.3 安全备份机制

```
文件操作前：
  1. 计算文件 SHA256 指纹（sha2 crate）
  2. 复制至 .tongtian-backups/{taskId}/{stepId}/{filename}.{timestamp}.bak
  3. 记录 FileOp { src_path, backup_path, fingerprint }
  4. 执行实际操作
  5. 操作成功 → 保留备份 (24h 后自动清理，由定时任务执行)
  6. 操作失败 → 从备份恢复 → 标记 step failed
```

### 3.4 自动化引擎（后续版本）

| 能力 | 技术方案 | 依赖 | MVP 范围 |
|------|----------|------|:--------:|
| 浏览器自动化 | CDP 驱动 Chromium | `headless_chrome` crate（或 python-bridge 托管 Playwright） | P1 |
| 桌面控制 | 键鼠模拟 | `enigo` crate | P1 |
| 定时任务 | Cron 调度 + sqlite 持久化 | `tokio-cron-scheduler` | P1 |
| IM 集成 | 钉钉/飞书 Webhook + Bot API | reqwest 直连 | P1（首版不做） |

---

### 3.5 多智能体工作区 (`crates/workspace`)

#### 3.5.1 架构设计

通天办公支持在同一 Rust core 进程内运行多个独立的 Agent 工作区实例，每个工作区是一组独立的 tokio task 与隔离状态。

```
┌─────────────────────────────────────────────────────┐
│               WorkspaceManager (单例)                │
│  create / destroy / suspend / resume / list / archive│
├─────────────────────────────────────────────────────┤
│                                                      │
│  ┌─── Workspace A ──────┐  ┌─── Workspace B ──────┐ │
│  │  AgentRunner (独立)   │  │  AgentRunner (独立)   │ │
│  │  ├─ ModelBinding      │  │  ├─ ModelBinding      │ │
│  │  ├─ MemoryStore (隔离) │  │  ├─ MemoryStore (隔离) │
│  │  ├─ SkillSet (独立)    │  │  ├─ SkillSet (独立)    │
│  │  ├─ MCPClient (独立)   │  │  ├─ MCPClient (独立)   │
│  │  └─ TaskQueue (独立)   │  │  └─ TaskQueue (独立)   │
│  └──────────────────────┘  └──────────────────────┘ │
│              ↕ ACP (Agent Comm Protocol)              │
└─────────────────────────────────────────────────────┘
```

#### 3.5.2 WorkspaceManager 接口

```rust
// crates/workspace/src/manager.rs
pub struct WorkspaceManager {
    workspaces: DashMap<String, WorkspaceHandle>,
}

impl WorkspaceManager {
    /// 创建工作区
    pub fn create(&self, config: WorkspaceConfig) -> Result<WorkspaceSummary>;

    /// 销毁工作区（清理资源）
    pub async fn destroy(&self, workspace_id: &str) -> Result<()>;

    /// 挂起工作区（释放 CPU/内存，保留状态）
    pub async fn suspend(&self, workspace_id: &str) -> Result<()>;

    /// 恢复挂起的工作区
    pub async fn resume(&self, workspace_id: &str) -> Result<()>;

    /// 列出所有工作区
    pub fn list(&self, filter: Option<WorkspaceFilter>) -> Vec<WorkspaceSummary>;

    /// 归档已完成的工作区（打包历史数据）
    pub async fn archive(&self, workspace_id: &str) -> Result<PathBuf>;

    /// 获取工作区资源使用情况
    pub fn resource_usage(&self, workspace_id: &str) -> Result<ResourceMetrics>;
}

#[derive(Serialize, Deserialize, TS)]
#[ts(export)]
pub struct WorkspaceConfig {
    pub name: String,
    pub persona: Option<String>,          // 角色描述（Prompt）
    pub model_binding: Option<String>,    // 绑定的 ModelConfig ID
    pub skill_filter: Vec<String>,        // 启用的 Skill ID 列表
    pub mcp_tool_whitelist: Vec<String>,  // 允许调用的 MCP 工具
    pub file_scope: Vec<PathBuf>,         // 文件访问范围（路径白名单）
    pub resource_limits: Option<ResourceLimits>,
}
```

#### 3.5.3 AgentRunner 隔离模型

每个工作区拥有独立的 broadcast 事件总线与状态机实例，确保：
- 工作区 A 的任务执行不会阻塞工作区 B（各自独立 tokio task 与信号量）
- 工作区之间不共享内存状态（除非通过 ACP 显式通信）
- 一个工作区的任务失败被捕获在自身 task 内，不影响其他工作区（tokio task panic 隔离 + `JoinHandle` 监控）

```rust
// crates/workspace/src/runner.rs
pub struct AgentRunner {
    pub workspace_id: String,
    engine: AgentEngine,                          // 独立实例
    task_queue: mpsc::Sender<TaskEnvelope>,       // 工作区级任务队列
    event_bus: broadcast::Sender<AgentEvent>,     // 工作区级事件总线
}

impl AgentRunner {
    pub async fn submit(&self, goal: &str) -> Result<()>;
    pub async fn review(&self, action: ReviewAction) -> Result<()>;
    pub async fn control(&self, action: ControlAction) -> Result<()>;
}
```

#### 3.5.4 Agent 间通信协议 (ACP)

```rust
// crates/workspace/src/acp.rs
#[derive(Serialize, Deserialize, TS)]
#[ts(export)]
pub struct AcpMessage {
    #[serde(rename = "type")]
    pub msg_type: AcpMessageType,  // task_assign | task_result | status_sync | resource_request
    pub from: String,              // 发送方 workspace_id
    pub to: String,                // 接收方 workspace_id (或 "broadcast")
    pub payload: serde_json::Value,
    pub correlation_id: Option<String>,
    pub timestamp: u64,
}

// Leader Agent 任务委派示例
// Workspace「协调者」→ Workspace「财务分析」
// {
//   "type": "task_assign",
//   "from": "coordinator",
//   "to": "financial-analyst",
//   "payload": { "goal": "分析 Q2 销售数据并生成趋势报告" },
//   "correlation_id": "task-001",
//   "timestamp": 1722816000000
// }
```

ACP 消息经 WorkspaceManager 内部路由（进程内 mpsc），不落外部网络；跨进程外部 Agent 委派为后续版本（P2）。

#### 3.5.5 资源配额管理

| 资源 | 默认限制 (每工作区) | 硬限制 | 实现手段 |
|------|---------------------|--------|----------|
| 最大并发 Step 数 | 5 | 10 | tokio `Semaphore` |
| 最大并发模型调用 | 3 | 5 | reqwest 连接池 + `Semaphore` |
| 任务队列深度 | 100 | 500 | 有界 mpsc channel |
| 内存上限 | 256MB | 512MB | 采样监控 + 超限暂停（子进程场景可用 OS job object 硬限制） |

---

### 3.6 Skills 技能系统 (`crates/skills`)

#### 3.6.1 Skill 文件格式规范

遵循行业标准的 Skill 文件系统目录格式（与 v1.x 兼容，格式不变）：

```
my-skill/
├── SKILL.md          # 必须：指令 + 元数据（Skill 的"灵魂"）
│   ---
│   name: 周报生成
│   version: 1.0.0
│   description: 从 CSV/Excel 数据自动生成格式化周报
│   author: tongtian
│   tags: [report, data, office]
│   requires: [filesystem-mcp, llm]
│   ---
│   # 执行流程
│   1. 读取指定目录下的数据文件
│   2. 按日期/维度汇总
│   3. 生成趋势图表
│   4. 组装为 docx 格式周报
│   5. 可选：推送到飞书群
├── scripts/          # 可选：可执行脚本（经沙箱执行）
│   └── summary.py
├── references/       # 可选：参考文档
│   └── template.docx
└── assets/           # 可选：静态资源
    └── logo.png
```

**SKILL.md 元数据 Schema**（frontmatter 解析用 `serde_yaml`）：

```rust
// crates/skills/src/manifest.rs
#[derive(Serialize, Deserialize, TS)]
#[ts(export)]
pub struct SkillMetadata {
    pub name: String,            // 技能名称（唯一标识）
    pub version: String,         // 语义化版本
    pub description: String,     // 一句话描述
    pub author: String,
    pub tags: Vec<String>,
    pub requires: Vec<String>,   // 依赖的 MCP 工具/其他 Skill
    pub steps: Vec<SkillStep>,   // 执行步骤定义
    pub input_schema: Option<serde_json::Value>,   // JSON Schema
    pub output_schema: Option<serde_json::Value>,
}

#[derive(Serialize, Deserialize, TS)]
#[ts(export)]
pub struct SkillStep {
    pub order: u32,
    pub name: String,
    pub tool: String,            // 调用的工具/MCP server
    pub params: serde_json::Value,
    pub on_error: OnError,       // Skip | Retry | Abort
}
```

#### 3.6.2 SkillLoader 与生命周期

```rust
// crates/skills/src/loader.rs
pub struct SkillLoader;

impl SkillLoader {
    /// 扫描技能目录，发现可用 Skill
    pub async fn scan(skill_dirs: &[PathBuf]) -> Result<Vec<SkillManifest>>;

    /// 解析 SKILL.md，提取元数据与步骤
    pub fn parse(skill_path: &Path) -> Result<SkillManifest>;

    /// 安全审计：静态扫描脚本中的恶意代码、网络请求、文件越权
    pub fn audit(skill: &SkillManifest) -> AuditResult;

    /// 校验依赖是否满足（MCP 工具、其他 Skill）
    pub fn validate(skill: &SkillManifest, registry: &SkillRegistry) -> ValidationResult;
}

// crates/skills/src/registry.rs
pub struct SkillRegistry {
    skills: DashMap<String, SkillManifest>,
}

impl SkillRegistry {
    pub fn register(&self, skill: SkillManifest) -> Result<()>;
    pub fn unregister(&self, skill_id: &str) -> Result<()>;
    pub fn get(&self, skill_id: &str) -> Option<SkillManifest>;
    pub fn list(&self, filter: Option<SkillFilter>) -> Vec<SkillManifest>;
    pub fn resolve_dependencies(&self, skill_id: &str) -> Result<Vec<SkillManifest>>;
}
```

#### 3.6.3 Skill 脚本安全隔离

第三方 Skill 的可执行脚本**不在主进程内解释执行**（Rust 无 vm2 等价物），而是经 `crates/sandbox` 的子进程隔离策略执行：

```
隔离策略：
- builtin Skill：受控子进程执行，工作目录限定 Skill 自身目录 + 用户授权路径
- marketplace/custom Skill：
  1. 独立子进程（tokio::process），stdout/stderr 捕获
  2. 工作目录 chroot 化（仅挂载 Skill 目录与临时目录）
  3. 网络访问默认禁止（出站白名单可选放宽）
  4. 执行超时（默认 30s）+ 输出大小限制（默认 10MB）
  5. Windows 侧用 Job Object 限制 CPU/内存；Linux 侧可选 bubblewrap
```

```rust
// crates/skills/src/executor.rs
pub struct SkillSandbox<'a> {
    sandbox_mgr: &'a SandboxManager,
}

#[derive(Serialize, Deserialize, TS)]
#[ts(export)]
pub struct SandboxContext {
    pub id: String,
    pub skill_id: String,
    pub allowed_paths: Vec<PathBuf>,   // 允许访问的路径白名单
    pub network_access: bool,          // 默认 false
    pub timeout_ms: u64,               // 默认 30000
    pub memory_limit_mb: u32,          // 默认 128
}
```

#### 3.6.4 Skill 与 Agent Executor 的集成

StepType 包含 `skill_call` 变体：

```
当 Executor 遇到 type=skill_call 的 Step 时：
1. 从 SkillRegistry 获取 SkillManifest
2. 按 Skill.steps 顺序执行每个子步骤
3. 每个子步骤可能调用 MCP 工具、LLM 或沙箱内脚本
4. 汇总子步骤结果为 Skill 输出
5. 记录到 Step.result
```

#### 3.6.5 技能市场协议（后续版本 P2）

```
REST API 设计（市场服务端，reqwest 消费）
GET    /api/marketplace/skills          // 技能列表（分页/搜索/标签筛选）
GET    /api/marketplace/skills/:id      // 技能详情
POST   /api/marketplace/skills/:id/install  // 一键安装
DELETE /api/marketplace/skills/:id      // 卸载
POST   /api/marketplace/skills/:id/update   // 更新版本
POST   /api/marketplace/skills/publish  // 发布自定义技能
```

---

### 3.7 MCP 集成架构 (`crates/mcp`)

#### 3.7.1 MCPClient 设计

基于 `rmcp`（Rust 官方 MCP SDK）封装：

```rust
// crates/mcp/src/client.rs
pub struct McpClient {
    connections: DashMap<String, McpConnection>,
}

impl McpClient {
    /// 服务发现与连接
    pub async fn connect(&self, config: McpServerConfig) -> Result<McpConnection>;

    /// 断开连接
    pub async fn disconnect(&self, server_id: &str) -> Result<()>;

    /// 列出 MCP 服务器提供的工具
    pub async fn list_tools(&self, server_id: &str) -> Result<Vec<McpTool>>;

    /// 调用 MCP 工具
    pub async fn call_tool(
        &self,
        server_id: &str,
        tool_name: &str,
        params: serde_json::Value,
    ) -> Result<McpToolResult>;

    /// 健康检查
    pub async fn health_check(&self, server_id: &str) -> bool;
}

#[derive(Serialize, Deserialize, TS)]
#[ts(export)]
pub struct McpServerConfig {
    pub name: String,
    pub transport: McpTransport,       // Stdio | Sse | StreamableHttp
    pub endpoint: Option<String>,      // SSE/HTTP 模式需要
    pub command: Option<String>,       // stdio 模式需要
    pub args: Vec<String>,             // stdio 模式需要
    pub auth_type: McpAuthType,        // None | OAuth2 | ApiKey
    pub auth_config: Option<serde_json::Value>,
    pub tool_whitelist: Vec<String>,   // 允许调用的工具列表 (空=全部允许)
    pub tool_blacklist: Vec<String>,   // 禁止调用的工具列表
}
```

#### 3.7.2 MCP 连接生命周期

```
[用户配置] → connect() → initialize(协议握手) → list_tools(获取工具列表)
    → [Agent 执行时] → call_tool(调用工具) → [返回结果]
    → [应用退出] → shutdown(优雅关闭)

异常处理：
- 连接断开：自动重连（指数退避，最多 3 次）
- 工具调用超时：30s 超时（tokio timeout）→ 标记失败 → 通知用户
- 服务器无响应：心跳检测（10s 间隔 ping）→ 3 次失败 → 标记不可用
```

#### 3.7.3 MCP 工具到 Agent 工具链的映射

MCP 工具自动注册为 Agent 可调用的工具：

```rust
// MCP Tool 注册时自动生成对应的工具描述（供 Planner 感知）
fn register_mcp_tool(registry: &AgentToolRegistry, server_id: &str, tool: McpTool) {
    let tool_key = format!("mcp:{server_id}:{}", tool.name);
    registry.register(AgentTool {
        key: tool_key,
        name: tool.name,
        description: tool.description,
        input_schema: tool.input_schema,   // 参数校验
        handler: Arc::new(move |params| {
            mcp_client.call_tool(server_id, &tool.name, params)
        }),
    });
}
```

#### 3.7.4 内置 MCP 服务器

| 服务器 | 传输方式 | 核心工具 | 安全约束 |
|--------|----------|----------|----------|
| `filesystem` | stdio | read_file, write_file, list_dir, search | 路径白名单限定为用户工作目录 |
| `sqlite` | stdio | query, execute, list_tables | 只读模式可选；禁止 DROP/ALTER |
| `web-search` | streamable-http | search, fetch_page | 域名白名单；速率限制 10 req/min |
| `browser` | stdio (CDP) | navigate, click, fill, screenshot | L1 沙箱约束；禁止访问 localhost |

内置服务器以独立 Rust 二进制随包分发（sidecar 形式，经 stdio 与主进程通信）。

#### 3.7.5 MCP 安全模型

```rust
// crates/mcp/src/security.rs
pub struct McpSecurityGuard {
    rate_limiter: RateLimiter,
}

impl McpSecurityGuard {
    /// MCP 服务器信任校验（防中间人）
    pub fn verify_trust(&self, config: &McpServerConfig) -> SignatureResult;

    /// 工具调用参数 JSON Schema 校验（jsonschema crate）
    pub fn validate_params(&self, tool: &McpTool, params: &serde_json::Value) -> ValidationResult;

    /// 调用频率限制（per-workspace, per-tool）
    pub fn check_rate_limit(&self, workspace_id: &str, tool_name: &str) -> RateLimitStatus;

    /// 工具白名单/黑名单检查
    pub fn is_tool_allowed(&self, workspace_id: &str, tool_name: &str) -> bool;
}

#[derive(Serialize, Deserialize, TS)]
#[ts(export)]
pub struct RateLimitConfig {
    pub max_calls_per_minute: u32,  // 默认 60 次/分钟
    pub max_calls_per_hour: u32,    // 默认 1000 次/小时
    pub burst_limit: u32,           // 突发上限（默认 10）
    pub cooldown_ms: u64,           // 超限冷却时间（默认 30000ms）
}

// 信任校验策略
// - stdio 传输：验证命令路径是否在受信任目录内
// - SSE/HTTP 传输：验证 TLS 证书（rustls）+ 可选 API Key 签名
// - 首次连接：提示用户确认服务器身份指纹
```

---

### 3.8 本地执行沙箱 (`crates/sandbox`)

#### 3.8.1 三层执行架构

```
┌─────────────────────────────────────────┐
│            SandboxManager                │
│  根据 Step 危险等级自动选择执行层级        │
├─────────────────────────────────────────┤
│                                          │
│  L2 · 系统沙箱 (OS 级隔离)               │
│  ├─ 任意代码执行、系统配置修改             │
│  ├─ Windows: AppContainer + Job Object  │
│  │  Linux: bubblewrap/namespace          │
│  │  macOS: sandbox-exec                  │
│  ├─ 网络策略可控；用户确认后才能升级到此层  │
│                                          │
│  L1 · 浏览器沙箱 (CDP/headless_chrome)   │
│  ├─ 网页导航/表单/抓取                   │
│  ├─ 无痕模式、Cookie/Storage 隔离        │
│  └─ 域名白名单约束                       │
│                                          │
│  L0 · 文件沙箱 (Rust core 内)            │
│  ├─ 文件读写/转换/整理                   │
│  ├─ 路径白名单 + 操作前备份               │
│  └─ 默认执行层                           │
└─────────────────────────────────────────┘
```

#### 3.8.2 SandboxManager 接口

```rust
// crates/sandbox/src/manager.rs
pub struct SandboxManager { /* ... */ }

impl SandboxManager {
    /// 根据 Step 类型判断所需执行层级
    pub fn assess_level(&self, step: &Step) -> SandboxLevel;

    /// 在当前层级执行 Step
    pub async fn execute(&self, step: &Step, level: SandboxLevel) -> Result<StepResult>;

    /// 升级执行层级（需用户确认）
    pub async fn escalate(&self, step: &Step, from: SandboxLevel, to: SandboxLevel) -> Result<bool>;

    /// 检查资源配额是否超限
    pub fn check_quota(&self, workspace_id: &str) -> QuotaStatus;
}
```

#### 3.8.3 危险操作拦截器

```rust
// crates/sandbox/src/guard.rs
pub struct DangerousOperationGuard {
    patterns: Vec<(Regex, DangerLevel)>,
}

impl DangerousOperationGuard {
    // 预定义危险模式
    // r"rm\s+-rf\s+/"          → Critical
    // r"format\s+[A-Z]:"       → Critical
    // r"(?i)reg\s+(add|delete)" → Critical
    // r"del\s+/f\s+/s\s+/q"    → High
    // r"shutdown\s+/[sr]"      → High
    // r"chmod\s+777"           → Medium

    /// 扫描命令/代码中的危险操作
    pub fn scan(&self, command: &str) -> Vec<DangerousOpAlert>;

    /// 用户确认（经 Tauri dialog plugin 弹窗，前端确认后回执）
    pub async fn require_confirmation(&self, alerts: &[DangerousOpAlert]) -> bool;
}
```

#### 3.8.4 资源配额控制器

```rust
// crates/sandbox/src/quota.rs
#[derive(Serialize, Deserialize, TS)]
#[ts(export)]
pub struct ResourceLimits {
    pub cpu: CpuLimit,         // max_percent 默认 50, burst_seconds
    pub memory: MemoryLimit,   // max_mb 默认 256/工作区, swap_mb
    pub disk: DiskLimit,       // max_write_mbps 默认 50, max_total_mb
    pub network: NetworkLimit, // allow_external, max_concurrent_requests,
                               // domain_whitelist, domain_blacklist
}

pub struct QuotaController { /* ... */ }

impl QuotaController {
    pub fn check(&self, limits: &ResourceLimits) -> QuotaStatus;
    pub fn enforce(&self, limits: &ResourceLimits); // 超限时触发降级/暂停
    pub fn report(&self, workspace_id: &str) -> ResourceReport;
}
```

---

## 4. 数据模型

### 4.1 实体关系图 (ER)

```
┌──────────┐       ┌──────────┐       ┌──────────┐
│   Task   │1─────1│   Plan   │1─────*│   Step   │
└────┬─────┘       └──────────┘       └──────────┘
     │
     │1─────*┌──────────┐
     └───────│ Message  │
             └──────────┘
     │
     │1─────*┌──────────┐
     └───────│  FileOp  │
             └──────────┘

┌──────────────┐       ┌──────────────┐
│ ModelConfig  │*─────*│    Task      │ (绑定关系)
└──────────────┘       └──────────────┘

┌──────────────┐       ┌──────────────┐
│  Workspace   │1─────*│    Task      │ (工作区包含多个任务)
└──────┬───────┘       └──────────────┘
       │
       │1─────1┌──────────────┐
       └───────│SandboxPolicy │ (每个工作区一个沙箱策略)
               └──────────────┘
       │
       │*─────*┌──────────────┐
       └───────│    Skill     │ (工作区可启用多个 Skill)
               └──────────────┘
       │
       │*─────*┌──────────────┐
       └───────│  MCPServer   │ (工作区可关联多个 MCP 服务器)
               └──────────────┘

┌──────────────┐
│ AuditLog     │ (独立实体，关联 Task/ModelConfig/Workspace)
└──────────────┘
```

### 4.2 核心实体定义

实体以 Rust struct 定义（serde + ts-rs 导出前端类型），存放于 `crates/storage/src/entities.rs`：

```rust
// === Task（任务） ===
pub struct Task {
    pub id: String,            // UUID v4
    pub title: String,         // 从 goal 自动摘要
    pub status: TaskStatus,    // idle→planning→...→done/failed
    pub user_id: String,       // 本地用户标识
    pub workspace_id: String,  // 所属工作区
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

// === Plan（执行计划） ===
pub struct Plan {
    pub id: String,
    pub task_id: String,
    pub steps: Vec<Step>,
    pub status: PlanStatus,    // Draft→PendingReview→Approved→Executing→Done
    pub reviewed: bool,
}

// === Step（执行步骤） ===
pub struct Step {
    pub id: String,
    pub plan_id: String,
    pub name: String,          // 人类可读的步骤名
    #[serde(rename = "type")]
    pub step_type: StepType,   // llm_call | file_read | file_write | file_convert |
                               // file_organize | user_confirm | skill_call | mcp_tool |
                               // workspace_delegate
    pub input: serde_json::Value,
    pub status: StepStatus,    // pending→running→success/failed/skipped/rolled_back
    pub result: String,        // 步骤输出
    pub rollbackable: bool,    // 是否支持回滚
}

// === ModelConfig（模型配置） ===
pub struct ModelConfig {
    pub id: String,
    pub name: String,          // 用户自定义显示名
    pub source: ModelSource,   // openai_compat | ollama | preset_*
    pub base_url: String,      // API 端点
    // ⚠️ apiKey 不落 struct 明文字段：持久化时经 keyring + AES-GCM 加密，
    //    运行时由 Vault 解密至内存（Zeroizing<String>），绝不序列化
    pub model_name: String,    // 模型标识
    pub context_window: u32,   // 上下文窗口大小
    pub is_default: bool,      // 是否为全局默认
    pub agent_binding: String, // 绑定的智能体 ID (空=未绑定)
    pub force_local: bool,     // 是否强制本地/私有通道
}

// === Message（消息） ===
pub struct Message {
    pub id: String,
    pub task_id: String,
    pub role: Role,            // user | assistant | system | tool
    pub content: String,
    pub attachments: serde_json::Value,
    pub ts: DateTime<Utc>,
}

// === FileOp（文件操作记录） ===
pub struct FileOp {
    pub id: String,
    pub task_id: String,
    pub op: FileOpType,        // read | write | convert | organize
    pub src_path: String,      // 源文件路径
    pub dst_path: String,      // 目标文件路径
    pub backup_path: String,   // 备份路径（rollback 用）
    pub status: FileOpStatus,  // pending→running→success/failed/rolled_back
}

// === AuditLog（审计日志） ===
pub struct AuditLog {
    pub id: String,
    pub timestamp: DateTime<Utc>,
    pub event: String,         // model.call | file.write | route.switch | circuit.break | config.change
    pub actor: String,         // user | system
    pub details: serde_json::Value, // 脱敏后的上下文
    pub task_id: Option<String>,
    pub model_id: Option<String>,
}

// === Workspace（多智能体工作区） ===
pub struct Workspace {
    pub id: String,
    pub name: String,          // 工作区名称（如「财务分析」「创意文案」）
    pub agent_config: AgentConfig, // Agent 角色/提示词配置
    pub model_binding: String, // 绑定的模型 ID（独立模型选择）
    pub status: WorkspaceStatus,   // Active | Suspended | Archived
    pub memory_store_id: String,   // 独立记忆空间 ID
    pub skill_set: Vec<String>,    // 启用的 Skill ID 列表
    pub mcp_tools: Vec<String>,    // 启用的 MCP 工具白名单
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

// === Skill（技能） ===
pub struct Skill {
    pub id: String,
    pub name: String,
    pub version: String,       // 语义化版本 (semver)
    pub source: SkillSource,   // Builtin | Marketplace | Custom
    pub skill_path: String,    // 技能目录绝对路径
    pub metadata: SkillMetadataDb, // 从 SKILL.md 解析
    pub enabled: bool,
    pub installed_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

// === McpServer（MCP 服务器连接） ===
pub struct McpServer {
    pub id: String,
    pub name: String,
    pub transport: McpTransport, // Stdio | Sse | StreamableHttp
    pub endpoint: String,        // 连接端点（命令或 URL）
    pub auth_type: McpAuthType,  // None | OAuth2 | ApiKey
    pub tool_whitelist: Vec<String>,
    pub tool_blacklist: Vec<String>,
    pub status: McpServerStatus, // Connected | Disconnected | Error
    pub last_connected_at: Option<DateTime<Utc>>,
}

// === SandboxPolicy（沙箱策略） ===
pub struct SandboxPolicy {
    pub workspace_id: String,
    pub execution_level: SandboxLevel, // L0 | L1 | L2
    pub file_allowlist: Vec<String>,
    pub file_blocklist: Vec<String>,
    pub tool_allowlist: Vec<String>,
    pub tool_blocklist: Vec<String>,   // 禁止执行的模式（如 rm -rf /）
    pub max_cpu_percent: u32,
    pub max_memory_mb: u32,
    pub max_disk_write_mbps: f64,
}
```

### 4.3 SQLite 表结构（Rust core 持久化，rusqlite bundled）

DDL 存放于 `src-tauri/migrations/`，按版本号顺序执行：

```sql
-- 任务表
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'idle',
  user_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL DEFAULT 'default',
  plan_json TEXT,        -- Plan 序列化为 JSON
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 消息表
CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  attachments_json TEXT DEFAULT '{}',
  ts TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 文件操作记录表
CREATE TABLE file_ops (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  op TEXT NOT NULL,
  src_path TEXT NOT NULL,
  dst_path TEXT,
  backup_path TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 模型配置表（API Key 经 keyring 加密，表内仅存引用标识）
CREATE TABLE model_configs (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  source TEXT NOT NULL,
  base_url TEXT NOT NULL,
  api_key_ref TEXT NOT NULL,   -- keyring 条目引用名（真实 Key 在系统密钥库）
  model_name TEXT NOT NULL,
  context_window INTEGER DEFAULT 4096,
  is_default INTEGER DEFAULT 0,
  agent_binding TEXT DEFAULT '',
  force_local INTEGER DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 审计日志表
CREATE TABLE audit_logs (
  id TEXT PRIMARY KEY,
  timestamp TEXT NOT NULL DEFAULT (datetime('now')),
  event TEXT NOT NULL,
  actor TEXT NOT NULL,
  details_json TEXT DEFAULT '{}',
  task_id TEXT,
  model_id TEXT
);

CREATE INDEX idx_messages_task ON messages(task_id);
CREATE INDEX idx_file_ops_task ON file_ops(task_id);
CREATE INDEX idx_audit_timestamp ON audit_logs(timestamp);
CREATE INDEX idx_audit_event ON audit_logs(event);

-- 工作区表
CREATE TABLE workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  agent_config_json TEXT NOT NULL DEFAULT '{}',
  model_binding TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',          -- active | suspended | archived
  memory_store_id TEXT NOT NULL,
  skill_set_json TEXT DEFAULT '[]',
  mcp_tools_json TEXT DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 技能表
CREATE TABLE skills (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version TEXT NOT NULL,
  source TEXT NOT NULL,              -- builtin | marketplace | custom
  skill_path TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER DEFAULT 1,
  installed_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- MCP 服务器表
CREATE TABLE mcp_servers (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  transport TEXT NOT NULL,           -- stdio | sse | streamable-http
  endpoint TEXT NOT NULL,
  auth_type TEXT NOT NULL DEFAULT 'none',
  auth_config_ref TEXT DEFAULT '',   -- 认证凭据在 keyring 中的引用名
  tool_whitelist_json TEXT DEFAULT '[]',
  tool_blacklist_json TEXT DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'disconnected',
  last_connected_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 沙箱策略表
CREATE TABLE sandbox_policies (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  execution_level TEXT NOT NULL DEFAULT 'L0',  -- L0 | L1 | L2
  file_allowlist_json TEXT DEFAULT '[]',
  file_blocklist_json TEXT DEFAULT '[]',
  tool_allowlist_json TEXT DEFAULT '[]',
  tool_blocklist_json TEXT DEFAULT '[]',
  max_cpu_percent INTEGER DEFAULT 50,
  max_memory_mb INTEGER DEFAULT 512,
  max_disk_write_mbps REAL DEFAULT 10.0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_workspaces_status ON workspaces(status);
CREATE INDEX idx_skills_source ON skills(source);
CREATE INDEX idx_skills_enabled ON skills(enabled);
CREATE INDEX idx_mcp_servers_status ON mcp_servers(status);
CREATE INDEX idx_sandbox_policies_workspace ON sandbox_policies(workspace_id);
```

### 4.4 本地数据目录布局（双目录契约，v2.1 新增）

应用采用「安装目录 + 运行目录」双目录结构：安装目录只读、随版本更新；全部用户数据集中于运行目录，卸载不丢失、可整体迁移备份。

```
安装目录（Program Files / Applications，只读）
└─ 应用二进制、前端静态资源、内置 MCP 服务器二进制、图标资源

运行目录（默认 %USERPROFILE%\.tongtian-office，可由环境变量 TONGTIAN_HOME 覆盖）
├─ bin/                       # 可选扩展二进制：Python 运行时（python-build-standalone）、
│                             #  用户安装的工具（LibreOffice headless 等）
├─ skills/                    # 已安装技能（目录格式见 §3.6.1）
│  ├─ builtin/                #  内置技能（随包复制，版本号目录）
│  ├─ marketplace/            #  市场安装
│  └─ custom/                 #  用户自建
├─ mcp/                       # MCP 服务器配置（servers.json）与本地服务器存放
├─ workspaces/
│  └─ {workspace-id}/
│     ├─ memory/              # 记忆：MEMORY.md（长期）+ daily/{date}.md（每日）
│     ├─ files/               # 工作区文件集
│     │  └─ attachments/{task-id}/   # 任务附件暂存（见 Workbench 交互补设计 §3）
│     └─ artifacts/           # 任务产物
├─ backups/                   # 文件操作前备份（原 .tongtian-backups 迁入，24h 自动清理）
├─ db/
│  └─ tongtian.db             # SQLite 主库（§4.3 DDL）
├─ logs/                      # 结构化日志（tracing JSON，见 §9.4）
└─ config/
   └─ settings.json           # 应用设置（敏感值仅存 keyring，不落此目录明文）
```

**实现约束**：

1. 路径解析统一经 `crates/storage::paths` 模块（读取 `TONGTIAN_HOME` → 默认 `%USERPROFILE%\.tongtian-office`），禁止各模块自行拼接
2. 首次启动时创建全部子目录（幂等）；目录缺失时按默认重建，不视为错误
3. 跨平台：macOS/Linux 默认 `~/.tongtian-office`，结构一致
4. 日志/备份/数据库路径不得硬编码旧位置（`%APPDATA%` 旧路径仅做一次性迁移读取）

---

## 5. API 与接口契约

### 5.1 Tauri Command 清单

前端经 `@tauri-apps/api/core` 的 `invoke()` 调用；命名遵循 `domain_action` 蛇形规范，在 `src-tauri/src/commands/` 下按域拆分模块并注册。流式/异步推送经 Tauri event 通道（前端 `listen()`）。

| Command | 方向 | 请求类型 | 响应类型 | 说明 |
|---------|------|----------|----------|------|
| `chat_submit` | WebView→Rust | `ChatSubmitReq` | `{ task_id }` + 事件流 | 发起新任务（事件经 `agent://event` 推送） |
| `chat_review` | WebView→Rust | `ChatReviewReq` | `{ ok: boolean }` | 审阅计划 |
| `chat_control` | WebView→Rust | `ChatControlReq` | `{ ok: boolean }` | 暂停/继续/回滚 |
| `chat_history` | WebView→Rust | `{ limit?: number }` | `TaskSummary[]` | 获取历史任务 |
| `model_list` | WebView→Rust | `void` | `ModelConfig[]` | 获取所有模型配置 |
| `model_add` | WebView→Rust | `ModelConfig` | `ModelConfig` | 添加自定义模型 |
| `model_update` | WebView→Rust | `ModelConfig` | `ModelConfig` | 更新模型配置 |
| `model_delete` | WebView→Rust | `{ id: string }` | `{ ok: boolean }` | 删除模型配置 |
| `model_test` | WebView→Rust | `ModelTestReq` | `ModelTestResp` | 连通性自测 |
| `file_list` | WebView→Rust | `{ dir_path: string }` | `FileInfo[]` | 列出目录文件 |
| `file_read` | WebView→Rust | `{ file_path: string }` | `{ content: string }` | 读取文件内容 |
| `file_preview` | WebView→Rust | `{ file_paths: string[] }` | `FilePreview[]` | 批量预览文件 |
| `settings_get` | WebView→Rust | `void` | `AppSettings` | 获取应用设置 |
| `settings_set` | WebView→Rust | `Partial<AppSettings>` | `AppSettings` | 更新应用设置 |
| `workspace_create` | WebView→Rust | `WorkspaceCreateReq` | `Workspace` | 创建工作区 |
| `workspace_list` | WebView→Rust | `void` | `Workspace[]` | 列出所有工作区 |
| `workspace_switch` | WebView→Rust | `{ workspace_id: string }` | `{ ok: boolean }` | 切换当前活跃工作区 |
| `workspace_destroy` | WebView→Rust | `{ workspace_id: string }` | `{ ok: boolean }` | 销毁（归档）工作区 |
| `workspace_suspend` | WebView→Rust | `{ workspace_id: string }` | `{ ok: boolean }` | 暂停工作区 |
| `workspace_resume` | WebView→Rust | `{ workspace_id: string }` | `{ ok: boolean }` | 恢复已暂停的工作区 |
| `skill_list` | WebView→Rust | `void` | `Skill[]` | 列出所有已安装技能 |
| `skill_install` | WebView→Rust | `SkillInstallReq` | `Skill` | 安装技能（marketplace 或本地路径） |
| `skill_uninstall` | WebView→Rust | `{ skill_id: string }` | `{ ok: boolean }` | 卸载技能 |
| `skill_enable` | WebView→Rust | `{ skill_id: string; enabled: boolean }` | `{ ok: boolean }` | 启用/禁用技能 |
| `skill_update` | WebView→Rust | `{ skill_id: string }` | `Skill` | 更新技能到最新版本 |
| `mcp_connect` | WebView→Rust | `McpServer` | `McpServer` | 连接 MCP 服务器 |
| `mcp_disconnect` | WebView→Rust | `{ server_id: string }` | `{ ok: boolean }` | 断开 MCP 服务器 |
| `mcp_list_tools` | WebView→Rust | `{ server_id: string }` | `McpTool[]` | 列出 MCP 服务器可用工具 |
| `mcp_test` | WebView→Rust | `McpServer` | `{ ok: boolean; latency_ms: number }` | 测试 MCP 服务器连通性 |
| `agent_delegate` | WebView→Rust | `AgentDelegateReq` | `AgentDelegateResp` | Agent 间任务委派（ACP） |
| `sandbox_get_policy` | WebView→Rust | `{ workspace_id: string }` | `SandboxPolicy` | 获取工作区沙箱策略 |
| `sandbox_update_policy` | WebView→Rust | `SandboxPolicy` | `SandboxPolicy` | 更新沙箱策略 |

**Event 通道（Rust→WebView，`app.emit` 推送）**：

| Event | 载荷 | 说明 |
|-------|------|------|
| `agent://event` | `AgentEvent`（见 §3.1.3） | 任务生命周期流式事件 |
| `workspace://status` | `{ workspace_id, status }` | 工作区状态变更 |
| `mcp://status` | `{ server_id, status }` | MCP 连接状态变更 |
| `app://update` | `{ version, status }` | 自动更新进度 |

### 5.2 关键消息类型

以下类型均经 `ts-rs` 自动生成 TS 定义（输出至 `shared/`），前端直接引用：

```rust
// === chat_submit ===
pub struct ChatSubmitReq {
    pub task_id: Option<String>,  // 新任务不传，续接任务传入
    pub goal: String,             // 用户自然语言目标
    pub model_binding: Option<String>,
}

// === chat_review ===
pub struct ChatReviewReq {
    pub plan_id: String,
    pub action: ReviewAction,         // Approve | Reject | Modify
    pub patch: Option<Vec<StepPatch>>,// Modify 时提供修改后的 steps
}

// === chat_control ===
pub struct ChatControlReq {
    pub task_id: String,
    pub action: ControlAction,        // Pause | Resume | RollbackTo
    pub step_id: Option<String>,      // RollbackTo 时必传
}

// === model_test ===
pub struct ModelTestReq {
    pub base_url: String,
    pub api_key: String,              // 仅在自测时使用，不入存储
    pub model_name: String,
    pub context_window: u32,
}

pub struct ModelTestResp {
    pub ok: bool,
    pub latency_ms: u64,
    pub model: String,
    pub error: Option<String>,        // 失败时返回错误码
    pub available_models: Option<Vec<String>>, // 404 时返回可用模型列表
}

// === workspace_create ===
pub struct WorkspaceCreateReq {
    pub name: String,
    pub agent_config: AgentConfigCreate, // role + system_prompt
    pub model_binding: Option<String>,
    pub skill_ids: Option<Vec<String>>,
    pub mcp_server_ids: Option<Vec<String>>,
}

// === agent_delegate (ACP) ===
pub struct AgentDelegateReq {
    pub from_workspace_id: String,
    pub to_workspace_id: String,
    pub message_type: AcpMessageType, // TaskAssign | StatusSync
    pub payload: DelegatePayload,     // task_goal / context / artifacts
}

pub struct AgentDelegateResp {
    pub accepted: bool,
    pub delegate_task_id: Option<String>,
    pub reason: Option<String>,
}

// === skill_install ===
pub struct SkillInstallReq {
    pub source: SkillInstallSource,   // Marketplace | Local | Url
    pub skill_id: Option<String>,
    pub local_path: Option<PathBuf>,
    pub url: Option<String>,
}

// === MCP 工具描述 ===
pub struct McpTool {
    pub name: String,
    pub description: String,
    pub input_schema: serde_json::Value,  // JSON Schema
}
```

OpenAPI 风格的关键 command 契约示例（供契约测试与文档使用）：

```yaml
# chat_submit
command: chat_submit
request:
  goal: string (required)
  task_id: string (optional)
  model_binding: string (optional)
response:
  task_id: string
events: agent://event (stream of AgentEvent)
errors: [TASK_ALREADY_RUNNING, ALL_MODELS_UNAVAILABLE, PLAN_REVIEW_REQUIRED]

# model_test
command: model_test
request: { base_url, api_key, model_name, context_window }
response: { ok, latency_ms, model, error?, available_models? }
errors: [CONNECTION_TIMEOUT, INVALID_API_KEY, MODEL_NOT_FOUND, DNS_RESOLVE_FAILED]
```

### 5.3 错误码规范

统一错误结构，command 返回 `Result<T, AppError>`，Tauri 自动序列化为前端可解析的错误对象：

```rust
#[derive(Debug, Serialize, TS)]
#[ts(export)]
pub struct AppError {
    pub code: ErrorCode,
    pub message: String,         // 人类可读
    pub details: Option<serde_json::Value>,
}

#[derive(Debug, Serialize, TS)]
#[ts(export)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ErrorCode {
    // 模型相关
    ConnectionTimeout,
    InvalidApiKey,
    ModelNotFound,
    DnsResolveFailed,
    AllModelsUnavailable,
    CircuitBreakerOpen,

    // 文件相关
    FileNotFound,
    FormatNotSupported,
    FileLocked,
    PermissionDenied,
    DiskFull,
    BackupFailed,
    RollbackFailed,

    // 任务相关
    TaskNotFound,
    PlanReviewRequired,
    StepIrreversible,
    TaskAlreadyRunning,

    // 配置相关
    VaultWriteFailed,
    InvalidConfig,

    // 工作区相关
    WorkspaceNotFound,
    WorkspaceQuotaExceeded,
    WorkspaceAlreadyRunning,

    // Skills 相关
    SkillNotFound,
    SkillParseError,
    SkillAuditFailed,
    SkillVersionMismatch,

    // MCP 相关
    McpConnectionFailed,
    McpToolNotAllowed,
    McpServerTimeout,
    McpRateLimited,

    // 沙箱相关
    SandboxViolation,
    SandboxQuotaExceeded,
    DangerousOperationBlocked,

    // Agent 间通信
    AcpMessageFailed,
    AcpTaskRejected,
}
```

各 crate 错误（`AgentError`/`ModelHubError`/`FileOpsError` 等）在 app 壳 crate 统一 `impl From<...> for AppError` 映射。

---

## 6. 安全架构

### 6.1 威胁模型

| 威胁 | 影响 | 缓解措施 |
|------|------|----------|
| API Key 明文泄露 | 凭证被盗用，产生费用 | 系统密钥库存储（keyring：Win Credential Manager / macOS Keychain）；SQLite 仅存引用名；运行时解密至 `Zeroizing<String>`；绝不明文落盘 |
| 敏感文件外发至云端 | 数据泄露、合规违规 | 数据分级检测；出域白名单；硬开关禁用外部 API |
| WebView XSS/注入 | command 权限被劫持 | Tauri 默认隔离（无 Node API）；严格 CSP；command 参数 serde 强类型校验 |
| Agent 误操作 | 文件损坏、错误外发 | 操作前备份；关键操作人工确认；沙箱隔离工作目录 |
| command 越权调用 | 未授权调用 Rust core 能力 | capability 白名单仅注册必要 command；参数校验 + 路径规范化防穿越 |
| 第三方 Skill 恶意代码 | 窃取数据、破坏系统 | 安装前静态扫描；子进程沙箱隔离执行（见 §3.6.3）；代码签名校验 |
| MCP 工具滥用 | 通过 MCP 服务器越权操作 | 工具白/黑名单 (per-workspace)；调用频率限制；参数 JSON Schema 校验 |
| 工作区间数据泄露 | Agent A 读取 Agent B 的敏感数据 | 文件系统路径隔离；独立记忆空间；网络策略按工作区分隔；ACP 仅进程内路由 |
| 沙箱逃逸 | L2 层代码突破隔离 | OS 级隔离（AppContainer/bubblewrap/sandbox-exec）；最小权限；网络出站白名单 |
| 资源耗尽攻击 | Agent 消耗过量 CPU/内存/磁盘 | 每工作区资源配额；超限自动降级或暂停；全局资源监控告警 |

### 6.2 密钥生命周期

```
创建 ──→ keyring 写入系统密钥库 ──→ 运行时读取解密(Zeroizing 内存) ──→ 进程退出自动清除
                                      │
                                      ├── SQLite 仅存引用名 (api_key_ref)
                                      ├── 绝不明文落盘
                                      ├── 绝不进入 .env 真实值
                                      ├── 绝不在日志中打印
                                      └── 导出时提示确认
```

### 6.3 数据分级与出域控制

```rust
// crates/model-hub/src/compliance.rs
pub enum DataSensitivity {
    Public,       // 可任意上送
    Internal,     // 仅白名单模型
    Confidential, // 仅本地模型
}

// 检测规则（可配置，regex）
// r"身份证|护照|营业执照"     → Confidential
// r"合同|协议|薪资|报价"     → Confidential
// r"内部|机密|绝密"           → Internal
```

### 6.4 Tauri 安全配置

```jsonc
// src-tauri/tauri.conf.json 关键安全配置
{
  "app": {
    "security": {
      "csp": "default-src 'self'; connect-src ipc: http://ipc.localhost https://*.aliyuncs.com; img-src 'self' asset: https://asset.localhost",
      "dangerousDisableAssetCspModification": false
    }
  }
}
```

```jsonc
// src-tauri/capabilities/default.json（默认拒绝制，显式白名单）
{
  "identifier": "default",
  "windows": ["main"],
  "permissions": [
    "core:default",
    "dialog:allow-open",
    "dialog:allow-save",
    "dialog:allow-message",
    "notification:default",
    "updater:default"
    // 不授予 fs/shell 等通用 plugin 权限：文件与子进程操作全部经
    // 自定义 command 在 Rust 侧受控执行（路径白名单 + 沙箱策略）
  ]
}
```

### 6.5 设计原创性与合规原则（v2.1 新增）

本产品在设计过程中参考了同类办公智能体产品（千问办公、WorkBuddy 等）的**功能范畴与交互范式**，为确保不侵犯他人知识产权，确立以下强制原则：

| 原则 | 要求 |
|------|------|
| **代码零复制** | 不反编译、不抓取、不复制任何同类产品的源码、前端资源、接口报文；全部实现从自有 PRD/spec 推导 |
| **素材零复制** | 图标、插画、字体、文案均为原创或合法授权；不采用同类产品的美术资产 |
| **视觉差异化** | 保持自有设计体系（清新蓝 #3FA7F4 / 薄荷绿 #2ED3B7、自有布局与组件风格），不模仿对方版式组合与品牌元素 |
| **名称与商标避让** | 产品名、Logo、功能命名不与竞品构成近似；上线前进行商标检索 |
| **独立创作证据链** | PRD/spec/变更记录（docs/ 全部版本历史）持续归档，作为独立创作证据 |
| **开源引用合规** | 引用开源项目（AgentScope 思路、rmcp、calamine 等）严格履行其许可证义务（声明/保留归属）；仅借鉴 QwenPaw/AgentScope 的设计思路，不搬运其代码 |
| **接口不攀附** | 不调用同类产品私有/未公开 API；对外集成仅用官方公开接口（如钉钉/飞书开放平台） |

**差异化竞争点**（区别于竞品的自有能力）：企业出域合规硬开关、数据分级强制本地、三层沙箱与资源配额、自定义模型路由与降级熔断、双目录可迁移数据布局。

---

## 7. 部署与打包方案

### 7.1 tauri-bundler 配置

```jsonc
// src-tauri/tauri.conf.json 打包关键配置
{
  "productName": "通天办公",
  "identifier": "com.tongtian.office",
  "bundle": {
    "active": true,
    "targets": ["nsis", "msi", "app", "dmg", "deb", "rpm"],
    "icon": ["icons/icon.ico", "icons/icon.icns", "icons/icon.png"],
    "windows": {
      "nsis": { "installMode": "currentUser" }
    },
    "resources": [
      "resources/builtin-mcp/*"   // 内置 MCP 服务器二进制
    ],
    "createUpdaterArtifacts": true
  }
}
```

### 7.2 构建产物大小估算

| 组件 | 大小 |
|------|------|
| Rust core 二进制 (release, strip) | ~12-18MB |
| WebView 运行时 | 0（系统自带 WebView2/WebKit） |
| 前端静态资源 (bundled) | ~2MB |
| 内置 MCP 服务器二进制 | ~8MB |
| **总计** | **~25MB** (NSIS 压缩后 ~12MB) |
| （可选）Python 扩展包 | +~40MB（用户可选装，不计入基础包） |

对比 v1.x Electron 方案（~127MB / 压缩后 ~55MB），基础包体积下降约 78%。

### 7.3 自动更新

```
tauri-plugin-updater 方案:
  - 检查更新：每次启动 + 每 4h 定时（updater.check()）
  - 全量包下载 + 签名校验（minisign 公钥内置）
  - 静默下载 + 用户确认后重启安装
  - 端点：https://update.tongtian.office/{target}/{arch}/{version}
```

### 7.4 Python 辅助 sidecar 分发策略

```
默认：不打包 Python 运行时（纯 Rust 形态）
可选扩展包（应用内「扩展中心」触发下载）：
  1. python-bridge 探测系统 Python (>=3.10)，或下载 python-build-standalone (~30MB)
  2. 安装辅助依赖（如 python-docx / pdf 处理库）到应用数据目录 venv
  3. 能力升级：docx→pdf / pdf→docx 高保真转换解锁
  4. 受 MOCK_ENABLED 开关控制：Mock 模式下 sidecar 调用由 MockSidecar 拦截
```

---

## 8. 测试策略

### 8.1 测试金字塔

```
        ┌───────┐
        │  E2E  │  5%  — 端到端：完整用户场景
        ├───────┤
        │ Contract│ 10% — 契约测试：command/event schema 一致性
        ├───────────┤
        │  Integration│ 15% — 集成测试：crate 间协作
        ├───────────────┤
        │     Unit       │ 70% — 单元测试：cargo test
        └───────────────┘
```

### 8.2 各层测试要求

#### 单元测试 (Unit Test，cargo test)

| crate/模块 | 覆盖目标 | 关键测试点 |
|------|----------|-----------|
| `model-hub/adapter/*` | ≥90% | 协议适配、超时重试、错误码映射（wiremock 模拟） |
| `model-hub/router.rs` | ≥90% | 路由决策树（6 个分支全覆盖）、熔断状态机 |
| `agent/planner.rs` | ≥85% | Plan 生成正确性、格式校验、非法 JSON 容错 |
| `agent/executor.rs` | ≥85% | Step 执行顺序、回滚逻辑、取消令牌并发安全 |
| `agent/state.rs` | ≥95% | 状态机全部合法/非法转移枚举 |
| `file-ops/convert.rs` | ≥85% | 5 种格式互转正确性、异常格式处理 |
| `storage/vault.rs` | ≥90% | 加密/解密循环、keyring mock 兼容 |
| `storage/crypto.rs` | ≥90% | AES-GCM 加解密、argon2 密钥派生 |
| `mcp/security.rs` | ≥90% | 白黑名单、限流窗口、参数校验 |
| `sandbox/guard.rs` | ≥95% | 危险模式全量命中/误报测试 |

工具链：`mockall`（trait mock）、`wiremock`（HTTP mock）、`tempfile`（文件测试隔离）。分支覆盖率目标 ≥ 80%（cargo-llvm-cov 度量）。

#### 契约测试 (Contract Test)

- command 请求/响应类型一致性：Rust 侧 `ts-rs` 导出 TS 类型与前端引用类型编译对齐
- `shared/` 生成类型与 `commands/*.rs` 签名一致性（CI 中 `cargo test contract` 验证）
- ModelConfig 序列化/反序列化循环
- Mock 开关验证：`MOCK_ENABLED=true` 时全部外部依赖可被本地实现替换（遵循开发规范）

#### 端到端测试 (E2E Test)

| 场景 | 步骤 | 验收 |
|------|------|------|
| 自定义模型接入 | 填表 → 自测 → 保存 → 设为默认 | 模型出现在选择列表，对话可切换 |
| 文件整理 | 准备 10 个混合文件 → 一句话指令 → 审阅计划 → 执行 | 文件按类型归类，备份可查 |
| 模型降级 | 主模型超时 → 自动切换备用 → 继续执行 | 任务不中断，用户收到切换通知 |
| 中断回滚 | 执行中点击暂停 → 回滚到 step 2 → 文件恢复 | 文件内容恢复至操作前，FileOp 状态正确 |
| 出域硬开关 | 开启硬开关 → 尝试调用云端模型 → 被阻断 | 提示「数据保护模式」，仅本地模型可用 |

### 8.3 测试基础设施

- **Rust 测试**：`cargo test`（单测 + 集成）；`cargo-llvm-cov` 覆盖率；`clippy` 门禁（`-D warnings`）
- **前端测试**：Vitest（组件/store 逻辑）
- **E2E 框架**：tauri-driver (WebDriver) 驱动真实窗口；模型 API 用 wiremock 预置回放
- **Mock 策略**（遵循开发规范，开发阶段默认开启）：
  - 模型 API：`MOCK_ENABLED=true` + cargo feature `mock`，wiremock 回放预置响应
  - sidecar：MockSidecar 进程内拦截
  - 文件系统：tempfile 临时目录隔离
- **CI**：`cargo clippy + cargo test + vitest run`；Windows/macOS/Linux 三平台矩阵构建

---

## 9. 非功能指标

### 9.1 性能基线

| 指标 | 目标值 | 测量方法 |
|------|--------|----------|
| 应用冷启动 | < 1s | 进程启动 → 首屏渲染完成 |
| 首屏渲染 | < 0.8s | FP (First Paint) |
| 模型调用 P95 | < 3s (云端) / < 1s (本地) | Observer 上报 |
| 大文件读取 (10MB xlsx) | < 1s（calamine 解析） | 读取 + 解析完成 |
| 大文件转换 (10MB pdf→txt) | < 3s | 全管道执行时间 |
| 内存占用 (空闲) | < 150MB | 任务管理器 |
| 内存占用 (执行中) | < 400MB | 峰值 |
| 安装包体积 | < 15MB (压缩后) | 构建产物 |

### 9.2 兼容性矩阵

| 平台 | WebView 依赖 | 最低版本 | 推荐版本 |
|------|----------|----------|----------|
| Windows | WebView2 Runtime（默认内置） | Windows 10 (Build 1803+) | Windows 11 |
| macOS | WebKit（系统自带） | macOS 11 (Big Sur) | macOS 14+ |
| Linux | webkit2gtk 4.1 | Ubuntu 20.04+（后续支持） | Ubuntu 22.04+ |

### 9.3 稳定性要求

| 指标 | 目标 |
|------|------|
| Agent 任务完成率（一次交付） | ≥ 85% |
| 崩溃率 | < 0.3% per session（Rust 无 GC 暂停/无未捕获 JS 异常） |
| 文件操作准确率 | ≥ 98% |
| 回滚成功率 | ≥ 99% |

### 9.4 可观测性

- **结构化日志**：`tracing` JSON 格式，分级 `debug/info/warn/error`，写入运行目录 `logs/`（§4.4，默认 `%USERPROFILE%\.tongtian-office\logs\`）
- **审计事件**：模型调用、文件写、路由切换、配置变更（audit_logs 表）
- **性能埋点**：tracing span 记录任务耗时、模型延迟、文件操作耗时
- **脱敏规则**：API Key、文件路径中的用户名、PII 内容自动替换为 `[REDACTED]`（tracing layer 过滤器）

---

## 10. 云端版本架构预留（v2.1 新增）

桌面端为产品本体（local-first 不变）；网页版与账号体系为扩展形态，完整设计见《通天办公 · 云端与账号体系》（`spec-通天办公-云端与账号体系.md`）。本节仅定义桌面侧必须预留的架构约束：

### 10.1 前端传输适配层

```
React 前端（同一套代码）
    │ 统一 tongtianAPI 接口（§5 契约不变）
    ├─ tauri-api.ts   → Tauri invoke/event（桌面形态，现状）
    ├─ http-api.ts    → REST + SSE（Web 形态，Phase B 新增）
    └─ mock-api.ts    → 本地 Mock（开发形态，现状）
```

前端业务代码只依赖统一接口，不感知传输层；运行环境探测决定注入哪个实现。

### 10.2 command 可用性维度

§5.1 全部 command 需标注可用性维度，作为 Web 端降级依据：

| 维度 | 含义 | 示例 |
|------|------|------|
| `local-only` | 依赖本地文件系统/系统能力，Web 端不可用 | file_list/read、sandbox 执行、桌面控制 |
| `cloud-ok` | 可经云端服务等价提供 | chat/model/skill/mcp/workspace 管理类 |
| `degraded` | Web 端可用但行为降级（服务端执行或受限） | 文件转换（服务端）、附件上传（multipart） |

### 10.3 桌面侧预留项

1. 数据层：SQLite 表结构预留 `sync_meta` 扩展（同步游标/冲突标记），DDL 变更在云端 spec 定义
2. 密钥边界：用户模型 API Key 永不上云；云端模型网关使用平台托管 Key，与用户自备 Key 双轨并存
3. 账号接入点：Settings 预留「账号与同步」区块（Phase A 启用），未登录时全部能力本地可用

---

## 附录 A：与现有 Electron 实现的迁移映射

本 SPEC 为架构重构后的目标态设计；现有 `tongtian-office/` 代码（Electron + TS）向目标态的迁移路径见《Electron → Tauri 架构演进方案》（`spec-通天办公-qwenpaw-改造方案.md` v2.0），概要映射：

| SPEC 章节 | 现有代码 (Electron/TS) | 目标代码 (Rust) | 迁移策略 |
|-----------|----------|----------|:----:|
| Agent 引擎 | `src/main/agent/*.ts` | `crates/agent` | 逻辑等价重写 |
| Model Hub | `src/main/model/*.ts` | `crates/model-hub` | 逻辑等价重写 |
| 文件处理 | `src/main/file/*.ts` | `crates/file-ops` | 逻辑等价重写，库替换 |
| 加密存储 | `src/main/store/*.ts` | `crates/storage` | keytar→keyring |
| IPC 接口 | `src/main/ipc/*.ts` + preload | `src-tauri/src/commands/*.rs` | channel→command |
| 共享类型 | `src/shared/*.ts` | ts-rs 自动生成 | 自动同步 |
| 渲染 UI | `src/renderer/*` | `src/*` | 保留，替换 IPC 封装层 |
| 单测 | `tests/unit/**/*.test.ts` | 各 crate `#[cfg(test)]` | 用例等价迁移 |

---

## 附录 B：与 PRD 的追溯矩阵

| PRD 章节 | PRD 需求 | SPEC 对应 |
|----------|----------|-----------|
| §4 功能模块分解 | 7 大子系统 30+ 子功能 | §3 模块详细设计 |
| §5 用户故事 | US-01 ~ US-13 | §8 测试策略 (E2E 场景) |
| §6 交互流程 | §6.1-6.5 五个核心流程 | §3.1 Agent 状态机, §5 command 契约 |
| §7 核心功能需求 | 对话工作台/文件/模型接入 | §3.1 Agent, §3.2 Model Hub, §3.3 文件 |
| §8 模型自定义接入 | 三层接入 + 路由 + 监控 | §3.2 Model Hub 详细设计 |
| §10 技术架构 | 五层架构概要 | §1 技术架构总览 |
| §11 数据安全 | 本地优先/加密/审计 | §6 安全架构 |
| §12 非功能需求 | 性能/兼容/稳定/可扩展 | §9 非功能指标 |
| §16 风险与依赖 | 7 项关键风险 | §6.1 威胁模型, §2 技术选型 |

---

> **下一步**：运行 `/apex-sdd:plan` 将本 SPEC 拆解为有序实现任务。
