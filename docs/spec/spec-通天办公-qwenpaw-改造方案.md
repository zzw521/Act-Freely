# 通天办公 · Electron → Tauri 架构演进方案

> **文档版本**：v2.0
> **更新日期**：2026-08-05
> **负责人**：架构组
> **关联 PRD**：`通天办公-PRD.html` v1.4（增强版，本机资料，未入库）
> **关联 SPEC**：`docs/spec/spec-通天办公.md` v2.0
> **历史版本**：v1.0 为《QwenPaw 改造实施方案》（Electron 壳 + Python 引擎嵌入），该方案随架构切换整体废止，其能力处置决策保留于本文档 §4。

---

## 目录

- [1. 演进背景与收益](#1-演进背景与收益)
- [2. 目标架构](#2-目标架构)
- [3. 现有代码资产盘点与迁移映射](#3-现有代码资产盘点与迁移映射)
- [4. QwenPaw 能力处置决策](#4-qwenpaw-能力处置决策)
- [5. 分阶段迁移路径](#5-分阶段迁移路径)
- [6. 关键技术决策](#6-关键技术决策)
- [7. 风险评估与缓解](#7-风险评估与缓解)

---

## 1. 演进背景与收益

### 1.1 为什么演进

v1.x 架构（Electron 30 + Node/TS 主进程 + QwenPaw Python 引擎嵌入）在 MVP 验证期完成了产品闭环，但暴露出三个结构性问题：

| 问题 | v1.x 现状 | 影响 |
|------|-----------|------|
| **分发体积过大** | Electron 基础包 ~127MB；叠加 QwenPaw Python 运行时后 ~200MB+ | 企业分发场景不可接受（要求 < 30MB） |
| **双运行时维护成本** | Node + Python 双进程模型，QwenPaw Bridge 需管理子进程生命周期、端口、SSE 协议适配 | 集成复杂度高、崩溃面大、上游 breaking change 风险 |
| **资源占用高** | Chromium 内核常驻 ~300MB 内存 | 低配办公机器体验差 |

同时，产品定位已明确：**通天办公是模型 API 的编排者而非推理引擎**——Python AI 生态的核心优势（训练/推理框架）并非刚需，而 Rust 在 HTTP 编排、文件处理、并发安全上的能力已完整覆盖产品需求。

### 1.2 演进收益

| 维度 | v1.x (Electron + Python) | v2.x (Tauri + Rust) | 收益 |
|------|--------------------------|---------------------|------|
| 安装包体积 | ~200MB（压缩 ~90MB） | ~25MB（压缩 ~12MB） | **-85%** |
| 冷启动 | 2-4s（含 Python 子进程预热） | < 1s | **-70%** |
| 空闲内存 | ~350-500MB | ~120-150MB | **-70%** |
| 运行时依赖 | Node 运行时 + Python 运行时 | 无（单二进制 + 系统 WebView） | 分发简化 |
| 安全基线 | 需自行收敛 nodeIntegration / 子进程权限 | capability 默认拒绝制 + 内存安全 | 攻击面收敛 |
| 语言栈 | TS + Python 双栈 | Rust 核心 + TS 前端 | 后端单栈 |

### 1.3 演进原则

1. **业务语义不变**：PRD 功能需求、交互流程、数据模型语义完全保留，仅切换技术载体
2. **契约先行**：前端与后端的接口契约（command/event/类型）先于实现冻结，前端改造量最小化
3. **能力等价迁移**：v1.x 已实现的每个模块在目标架构都有明确归属，不允许能力丢失
4. **渐进替换**：双壳并存过渡，逐模块迁移验证，任何阶段可回退

---

## 2. 目标架构

目标架构完整定义见 `spec-通天办公.md` v2.0 §1，此处仅给出迁移视角的结构概要：

```
┌────────────────────────────────────────────────────────────────┐
│                     通天办公 (Tauri 2.x)                        │
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              WebView 前端 (React 18)                      │  │
│  │  Workbench / ModelHub / SkillMkt / Workspace / Settings  │  │
│  │  MUI + Tailwind + Zustand（保留 v1.x 全部页面资产）        │  │
│  └──────────────────────┬───────────────────────────────────┘  │
│                         │ invoke / listen (@tauri-apps/api)     │
│  ┌──────────────────────┴───────────────────────────────────┐  │
│  │                  Rust Core (Cargo workspace)              │  │
│  │                                                          │  │
│  │  crates/agent        — Agent 引擎（Rust 原生 ReAct/规划） │  │
│  │  crates/model-hub    — Adapter/Router/Observer 三层       │  │
│  │  crates/file-ops     — 文件读写/转换/整理/备份             │  │
│  │  crates/storage      — rusqlite + keyring vault          │  │
│  │  crates/workspace    — 多智能体工作区 + ACP               │  │
│  │  crates/skills       — Skill 加载/注册/审计               │  │
│  │  crates/mcp          — MCP 客户端（rmcp 三传输）          │  │
│  │  crates/sandbox      — 三层沙箱/配额/危险操作拦截          │  │
│  │  crates/python-bridge— 可选 Python sidecar（辅助工具）    │  │
│  └──────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
          │                    │
          ▼                    ▼
   ┌──────────┐        ┌──────────────┐
   │  Ollama  │        │  云端模型 API  │
   └──────────┘        └──────────────┘
```

**与 v1.x QwenPaw 方案的本质区别**：v1.x 中 Agent 引擎、Skills、MCP、记忆等核心能力由 QwenPaw Python 引擎提供（Electron 壳通过 Bridge 调用）；v2.x 中这些能力全部由 Rust crate 自研实现，Python 退化为可选辅助工具（docx→pdf 高保真转换等个别场景）。

---

## 3. 现有代码资产盘点与迁移映射

### 3.1 TypeScript 后端资产（`tongtian-office/src/main/`）

| 现有模块 | 现有文件 | 目标 crate | 迁移策略 | 工作量估计 |
|----------|----------|-----------|----------|:----:|
| Agent 引擎 | `agent/engine.ts`, `planner.ts`, `executor.ts`, `types.ts` | `crates/agent` | 状态机/规划/执行逻辑等价重写为 Rust；事件模型 EventEmitter → tokio broadcast | 3 人周 |
| Model Hub | `model/router.ts`, `observer.ts`, `circuit-breaker.ts` | `crates/model-hub` | 路由决策树/熔断器纯逻辑，等价重写；HTTP 层 fetch → reqwest | 2 人周 |
| 文件处理 | `file/convert.ts`, `backup.ts` | `crates/file-ops` | 管道模型保留；库替换（mammoth→docx-rs、xlsx→calamine、pdf-parse→pdf-extract）；docx→pdf 转 python-bridge | 3 人周 |
| 存储 | `store/*.ts` | `crates/storage` | better-sqlite3 → rusqlite（DDL 兼容）；keytar → keyring | 1 人周 |
| 工作区 | `workspace/manager.ts`, `quota.ts` | `crates/workspace` | WorkspaceManager/配额逻辑等价重写 | 2 人周 |
| IPC 层 | `ipc/*.ts` + `preload/index.ts` | `src-tauri/src/commands/*.rs` | channel 命名映射为 command（见下表）；preload 层废弃 | 1 人周 |
| QwenPaw Bridge | `bridge/agent-bridge.ts`, `http-client.ts`, `sse-parser.ts`, `config-injector.ts`, `version-adapter.ts` | — | **废止**：Python 引擎不再存在；SSE 解析能力并入 model-hub（reqwest stream） | 0（删除） |
| Hook 执行器 | `hooks/hook-runner.ts` | `crates/agent`（子模块） | 并入 Agent 生命周期钩子 | 0.5 人周 |
| 工具 | `util/*.ts` | `crates/storage`/app 壳 | 日志→tracing，加密→aes-gcm | 0.5 人周 |

### 3.2 IPC channel → Tauri command 命名映射

| v1.x channel | v2.x command | 说明 |
|--------------|--------------|------|
| `chat:submit` | `chat_submit` | 载荷类型语义不变（ts-rs 生成） |
| `chat:review` | `chat_review` | 同上 |
| `chat:control` | `chat_control` | 同上 |
| `chat:history` | `chat_history` | 同上 |
| `model:list/add/update/delete/test` | `model_list/add/update/delete/test` | 同上 |
| `file:list/read/preview` | `file_list/read/preview` | 同上 |
| `settings:get/set` | `settings_get/set` | 同上 |
| `workspace:*` | `workspace_*` | 同上 |
| `skill:*` | `skill_*` | 同上 |
| `mcp:*` | `mcp_*` | 同上 |
| `agent:delegate` | `agent_delegate` | 同上 |
| `sandbox:get-policy/update-policy` | `sandbox_get_policy/update_policy` | 同上 |
| 主进程 → 渲染 事件推送 | `agent://event` 等 event 通道 | `webContents.send` → `app.emit` |

### 3.3 前端资产（`src/renderer/`）

| 资产 | 处置 |
|------|------|
| 全部页面（Workbench/ModelHub/Settings 等）与组件 | **保留**，不改业务逻辑 |
| IPC 封装层（`store/`、`lib/` 中的 ipc 调用） | 替换为 `@tauri-apps/api/core` 的 `invoke`/`listen` 封装（唯一必改项） |
| 类型定义（`types.ts`、`global.d.ts`） | 改为引用 ts-rs 生成的 `shared/` 类型 |
| 主题/样式（MUI + Tailwind） | 保留 |

### 3.4 Python 资产（`src/agent/`，QwenPaw 集成产物）

| 资产 | 处置 |
|------|------|
| `agents/react_agent.py`、`agents/acp/`、`agents/memory/`、`agents/skill_system/` | **废止**：能力由 `crates/agent`/`crates/workspace`/`crates/skills` 以 Rust 重实现（处置细节见 §4） |
| `providers/*.py`（14+ 模型供应商适配） | **废止**：Rust 侧收敛为 OpenAI 兼容 + Ollama + 预置三类 Adapter（覆盖全部供应商，协议等价） |
| `runtime/`、`sandbox/`、`tool_calls/`、`token_usage/` | **废止**：对应能力在 `crates/agent`/`crates/sandbox` 重实现 |
| `local_models/`（llama.cpp 管理） | **延后**：本地推理继续由 Ollama 承担；llama.cpp 内嵌列为 P3 可选 |

### 3.5 测试资产迁移（`tests/unit/`）

| 现有测试 | 目标 | 策略 |
|----------|------|------|
| `agent/engine.test.ts`, `state-machine.test.ts`, `event-adapter.test.ts` | `crates/agent` `#[cfg(test)]` | 用例清单等价迁移：状态机全转移表、事件序列断言 |
| `model/router.test.ts`, `circuit-breaker.test.ts`, `observer.test.ts` | `crates/model-hub` | 路由分支/熔断阈值/统计口径用例等价迁移 |
| `bridge/*.test.ts`（5 个） | — | **废止**（Bridge 模块删除）；SSE 解析用例并入 model-hub stream 测试 |
| `file/backup.test.ts`, `convert.test.ts` | `crates/file-ops` | 转换正确性/备份恢复用例等价迁移（样例文件复用） |
| `hooks/hook-runner.test.ts` | `crates/agent` | 钩子时序用例迁移 |
| `workspace/manager.test.ts`, `quota.test.ts` | `crates/workspace` | 生命周期/配额用例迁移 |

**契约基线**：迁移前先运行全量现有测试固化行为基线（golden output），每个 crate 重写后以同一批输入用例回归对比，确保行为等价。

---

## 4. QwenPaw 能力处置决策

v1.x 方案从 QwenPaw 复用的能力，在 v2.x 中的处置如下。**决策基调：弃用 Python 引擎本体，能力以 Rust 自研替代；QwenPaw/AgentScope 仅作设计思路参考（架构范式、能力矩阵），不作为运行时依赖、不复制其代码**（合规原则见 SPEC v2.1 §6.5）；对成熟协议（SKILL.md / MCP）保持格式兼容，保证生态延续。

> **开发期过渡手段定性（v2.1 同步）**：现有 `tongtian-tauri/engine/server.py`（自研 FastAPI 引擎对接 AgentScope）仅为 `crates/agent` 落地前的开发期临时手段，用于提前验证 SSE 契约与前端联调；**不进入目标态架构**，随 `crates/agent` 落地后下线，AgentScope 依赖一并移除。其 SSE 事件语义作为 `agent://event` 的实现参考保留。

| 能力域 | v1.x 来源 (QwenPaw) | v2.x 替代方案 | 兼容性 |
|--------|---------------------|--------------|--------|
| Agent 引擎 (ReAct + Plan Mode) | `react_agent.py` (AgentScope) | `crates/agent`：Rust 原生 Plan-and-Execute + ReAct 循环（tokio 异步）；AgentScope 仅作循环设计思路参考 | 行为等价；Plan/Step 数据结构不变 |
| Skills 系统 | `skill_system/` | `crates/skills`：SKILL.md 格式**完全保留**（frontmatter schema 不变），加载/审计/沙箱执行用 Rust 实现 | 已安装 Skill 目录零改造可用 |
| MCP 客户端 | QwenPaw MCP (Node 子进程) | `crates/mcp`：rmcp 原生 Rust 客户端，stdio/SSE/streamable-http 三传输 | 服务器配置格式兼容 |
| 记忆系统（三层） | `agents/memory/` | `crates/agent` 记忆子模块：长期记忆（运行目录 `workspaces/{id}/memory/MEMORY.md`）+ 每日记忆 + SQLite 检索；语义搜索（向量）列为 P2 | 记忆文件格式兼容 |
| 内置文件工具 | QwenPaw file tools | `crates/file-ops` + 内置 `filesystem` MCP 服务器 | 工具语义一致 |
| 浏览器自动化 | Playwright (Python) | `headless_chrome` crate（CDP）；复杂场景经 python-bridge 托管 Playwright | P1 交付 CDP 基础能力 |
| Shell 执行 | QwenPaw shell tool | `crates/sandbox` 受控子进程（必经危险操作扫描 + L0/L2 分级） | 安全增强 |
| 定时任务 | QwenPaw Cron | `tokio-cron-scheduler` + SQLite 持久化 | 表达式语法兼容（cron 标准） |
| 频道集成（钉钉/飞书等） | QwenPaw 频道适配器 | 暂不迁移：桌面产品首版不做 IM 频道，列为 P2（reqwest 直连 Webhook/Bot API） | — |
| ACP 委派 | `agents/acp/` | `crates/workspace` ACP（进程内消息路由）；外部 Agent 委派列 P2 | 消息 schema 兼容 |
| 四层安全 | QwenPaw security | `crates/sandbox` 三层沙箱 + 配额 + 危险操作守卫（v1.x SPEC §3.8 设计不变） | 策略模型增强 |
| 模型接入（14+ 供应商） | QwenPaw providers | `crates/model-hub` 三类 Adapter（OpenAI 兼容协议覆盖全部供应商 + Ollama + 预置） | 协议等价，配置收敛 |
| 桌面截图 | desktop_screenshot | `xcap` crate（P1） | — |

**成本重估**：v1.x 方案估算「复用 QwenPaw 节省 40-50 人周」；v2.x 以 Rust 自研需投入约 35-45 人周，换取的是 -85% 体积、单二进制分发、无 Python 依赖与长期维护成本下降。该投入经架构组评估为值得。

> **与 SPEC v2.1 的同步说明**：本文档 §4 基调已按 SPEC v2.1 更新（思路参考、不作运行时依赖）；本地数据目录统一遵循 SPEC v2.1 §4.4 双目录契约（工作区数据位于运行目录 `workspaces/{id}/`）。

---

## 5. 分阶段迁移路径

```
阶段 0 · 契约冻结与脚手架 (1 周)
├─ 冻结 command/event 契约与 ts-rs 类型生成流程
├─ 搭建 src-tauri Cargo workspace 骨架（9 个 crate 空壳 + CI）
└─ 固化现有 tests/unit 行为基线（golden output）

阶段 1 · Rust 核心实现 (8 周)
├─ W1-2: crates/storage（rusqlite + keyring）+ crates/model-hub
│        （Adapter 三实现 + Router + Observer + 熔断）
├─ W3-4: crates/file-ops（5 格式读写转换 + 备份回滚）
├─ W5-6: crates/agent（状态机 + Planner + Executor + 事件流）
├─ W7:   crates/sandbox（L0 + 危险守卫 + 配额）
└─ W8:   crates/skills + crates/mcp 基础版

阶段 2 · 前端切换 (3 周)
├─ W1: IPC 封装层替换为 @tauri-apps/api（invoke/listen）
├─ W2: 类型切换至 ts-rs 生成产物；页面联调
└─ W3: E2E 全量回归（tauri-driver）

阶段 3 · 能力补齐与 Electron 下线 (4 周)
├─ W1-2: crates/workspace（多工作区 + ACP）+ python-bridge（可选）
├─ W3:   打包/签名/自动更新链路（tauri-plugin-updater）验证
└─ W4:   Electron 壳与 QwenPaw Python 资产下线归档
```

**双壳并存策略**（阶段 1-2 期间）：前端代码库保持单一来源（`src/`），通过 IPC 适配层（`lib/ipc`）屏蔽壳差异——`ELECTRON_SHELL` / `TAURI_SHELL` 两个适配实现，切换由构建变量控制。任何阶段发现阻塞性问题可回退 Electron 壳继续交付。

**里程碑验收**：

| 里程碑 | 验收标准 |
|--------|----------|
| M1 (阶段 1 完成) | `cargo test` 全绿；模型自测/文件转换/任务闭环三场景 CLI 集成测试通过 |
| M2 (阶段 2 完成) | 前端 E2E 五场景（见 SPEC §8.2）在 Tauri 壳全绿 |
| M3 (阶段 3 完成) | 三平台安装包产出；自动更新链路验证；体积/内存达标（SPEC §9.1） |

---

## 6. 关键技术决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 桌面壳 | Tauri 2.x | 体积/性能/安全三维度领先（SPEC §2.2） |
| Agent 引擎实现 | Rust 原生（不引入第三方 Agent 框架） | Plan-and-Execute 状态机为应用层逻辑，自研可控；避免框架锁定 |
| 异步运行时 | tokio (multi-thread) | 生态事实标准，与 reqwest/rmcp 协同 |
| SQLite 驱动 | rusqlite (bundled) | 同步 API 简单可靠，桌面场景无高并发写；DDL 与 v1.x 兼容 |
| 密钥存储 | keyring crate | 跨平台系统密钥库，直接替代 keytar 语义 |
| MCP 客户端 | rmcp（官方 Rust SDK） | 三传输支持，协议跟进有保障 |
| 文件转换短板 | python-bridge 可选 sidecar | docx→pdf 等 Rust 生态不成熟场景降级方案，默认不打包 |
| 前端框架 | React 18 + MUI + Tailwind（保留） | 页面资产零浪费 |
| 类型同步 | ts-rs | 契约先行，消除手写双端类型漂移 |

---

## 7. 风险评估与缓解

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|:----:|----------|
| Rust 学习曲线拖累进度 | 阶段 1 延期 | 中 | crate 化模块边界降低单模块复杂度；状态机/HTTP 编排为低并发逻辑；关键模块（model-hub/file-ops）先行试点 |
| 文件格式转换 crate 成熟度不足 | docx/pdf 转换准确率不达标 | 中 | 分级策略：纯 Rust 保底（文本级转换）+ python-bridge 高保真增强；转换准确率验收基线 98%（SPEC §9.3）不变，抽样集先行验证 |
| WebView 兼容性差异（Win/macOS/Linux） | 前端渲染/交互异常 | 低 | 前端已在 Chromium 验证；WebKit 差异走 E2E 矩阵覆盖；必要时按平台 CSS 适配 |
| 前端 IPC 层替换引入回归 | 页面功能异常 | 低 | 适配层抽象（§5 双壳并存）；契约测试先行；E2E 全量回归门禁 |
| tauri-driver E2E 生态不如 Playwright 成熟 | 自动化验收能力弱 | 中 | E2E 以 tauri-driver 为主 + 关键路径人工验收兜底；前端组件测试（Vitest）补足覆盖 |
| 自动更新签名/分发链路首次搭建 | M3 延期 | 低 | 阶段 3 W3 专项；静态服务器 + minisign 最简方案先行 |
| QwenPaw 资产废止造成能力遗漏 | 个别功能缺失 | 低 | §4 能力矩阵逐项签核；§3.5 golden baseline 回归对比 |

---

## 附录：与现有 SPEC 的关系

本文档是 `spec-通天办公.md` v2.0 的**实施配套方案**：

- **SPEC v2.0**：定义目标态架构（Tauri + Rust 全栈）与全部模块契约
- **本文档**：定义从现状（Electron + QwenPaw Python）到目标态的迁移路径、资产映射与风险缓解
- **v1.0 QwenPaw 方案**：整体废止；其中 UI 设计规范、Model Hub 增强、三层沙箱、企业合规等「通天办公侧新增」设计已并入 SPEC v2.0 对应章节

---

*下一步：运行 `/apex-sdd:plan` 将本方案分解为实施任务。*
