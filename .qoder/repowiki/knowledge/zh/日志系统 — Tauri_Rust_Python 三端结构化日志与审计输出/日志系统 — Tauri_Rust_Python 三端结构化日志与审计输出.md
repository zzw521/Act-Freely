---
kind: logging_system
name: 日志系统 — Tauri/Rust/Python 三端结构化日志与审计输出
category: logging_system
scope:
    - '**'
source_files:
    - tongtian-tauri/src/main/util/logger.ts
    - tongtian-tauri/src-tauri/src/bridge/agent.rs
    - tongtian-tauri/src/agent/constant.py
    - tongtian-tauri/src/renderer/components/AuditLog/index.tsx
    - tongtian-tauri/src/agent/agents/react_agent.py
---

本项目的日志系统横跨三个运行时（Tauri 主进程 TypeScript、Rust 后端、Python Agent 引擎），采用分层设计：前端主进程使用自研结构化 JSON 日志，Rust 侧通过标准输出桥接 Python 子进程日志，Python 层统一使用 stdlib logging。同时提供独立的前端审计日志 UI。

### 1. 使用的框架与工具
- **TypeScript 主进程**：自研 `src/main/util/logger.ts` 单例 Logger，无第三方依赖，输出结构化 JSON 行。
- **Rust 后端**：使用 `println!` / `eprintln!` 直接打印到 stdout/stderr，配合 `pump_logs` 异步泵管道转发。
- **Python Agent 引擎**：各模块通过 `import logging; logger = logging.getLogger(__name__)` 获取命名 logger，未引入 loguru/structlog 等第三方库。
- **前端渲染进程**：未发现集中式日志封装，调试主要依赖浏览器控制台。

### 2. 核心文件与包
- `tongtian-tauri/src/main/util/logger.ts` — 结构化 JSON 日志器（单例）
- `tongtian-tauri/src-tauri/src/bridge/agent.rs` — Rust 侧日志泵与子进程 stdout/stderr 转发
- `tongtian-tauri/src/agent/constant.py` — Python 侧日志级别环境变量 `QWENPAW_LOG_LEVEL`
- `tongtian-tauri/src/renderer/components/AuditLog/index.tsx` — 审计日志查看组件
- 各 Python 模块如 `react_agent.py`、`adbpg_client.py`、`proactive_responder.py` 等均通过 `logging.getLogger(__name__)` 记录日志

### 3. 架构与约定
- **结构化字段**：每条日志为 JSON 行，包含 `timestamp`（ISO 8601）、`level`（debug/info/warn/error）、`message`、可选 `module`（模块名）、可选 `data`（任意键值对）。
- **日志级别控制**：
  - TypeScript 侧通过 `LOG_LEVEL` 环境变量控制最小级别，默认 `debug`。
  - Python 侧通过 `QWENPAW_LOG_LEVEL` 环境变量控制。
- **敏感信息脱敏**：`logger.ts` 内置正则规则自动脱敏 API Key、Bearer Token、邮箱、手机号等 PII，替换为 `[REDACTED]`。
- **日志轮转与清理**：按天拆分文件（`tongtian-YYYY-MM-DD.log`），保留 7 天，启动时清理过期文件。
- **输出目标**：生产环境写入用户数据目录下的 `logs/` 文件夹；开发模式额外输出到 `console.debug/info/warn/error`。
- **Rust → Python 日志桥接**：Rust 通过 `spawn` 启动 Python 子进程，分别读取 stdout/stderr 并通过 `pump_logs` 异步转发到应用事件流，stderr 前缀 `[agent:{workspace_id}]`。
- **审计日志**：前端提供独立的 AuditLog 组件，用于展示操作审计记录，与运行日志分离。

### 4. 约定与约束
- **所有模块必须通过各自语言的 logger 接口记录日志**，禁止直接使用 `print()` / `console.log()`（Python 侧部分旧代码仍在使用，但新模块遵循 `logging.getLogger(__name__)` 约定）。
- **日志中不得明文包含密钥、Token、邮箱、手机号等敏感信息**，由 logger 自动脱敏机制保障。
- **日志级别必须从环境变量读取**，禁止硬编码级别字符串。
- **日志文件按天滚动且自动清理**，开发者无需手动维护磁盘空间。
- **Rust 侧仅通过 `println!`/`eprintln!` 输出诊断信息**，不引入 tracing 等重型框架，保持二进制体积最小化。
- **Python 侧未启用结构化日志格式**，各模块使用默认的 `logging` 格式，与 Rust 的 JSON 结构不一致，属于跨语言边界限制。