---
kind: build_system
name: Tauri + Rust + Vite 多语言构建系统
category: build_system
scope:
    - '**'
source_files:
    - tongtian-tauri/package.json
    - tongtian-tauri/vite.config.ts
    - tongtian-tauri/src-tauri/Cargo.toml
    - tongtian-tauri/src-tauri/tauri.conf.json
    - tongtian-tauri/src-tauri/build.rs
    - tongtian-tauri/init.ps1
    - tongtian-tauri/init.sh
    - tongtian-tauri/run-dev.ps1
    - .tools/setup-env.ps1
    - .tools/compile-all.ps1
    - .tools/start-product.ps1
---

本项目采用 **Tauri 2.x + Rust 后端 + React/Vite 前端** 的桌面应用架构，构建系统围绕 Tauri CLI 组织，配合 PowerShell/Shell 脚本完成跨平台开发环境初始化与编译打包。

### 1. 构建系统与工具链
- **前端构建**: 使用 Vite 5 + React 插件，源码位于 `src/renderer`，输出到 `dist`；TypeScript 通过 `tsc --noEmit` 类型检查，测试使用 Vitest 2。
- **Rust 后端**: 基于 Tauri 2，Cargo 工程位于 `src-tauri/`，启用 `staticlib/cdylib/rlib` 三种 crate 类型，release profile 开启 LTO、opt-level=s、strip。
- **Python Sidecar**: Agent 引擎以 Python 进程形式运行，通过 Tauri command/event 与 Rust 桥接。
- **Java 依赖**: `.tools/` 下内置 JDK8 与 Maven，用于项目内其他 Java 服务（bpaas-user/product/order-service）的编译。

### 2. 关键配置文件
- `tongtian-tauri/package.json`: 定义 `dev/build/preview/tauri/typecheck/test` 等 npm scripts。
- `tongtian-tauri/vite.config.ts`: 配置 React 插件、别名 `@shared`/`@renderer`、输出目录 `../../dist`、开发端口 7173。
- `tongtian-tauri/src-tauri/Cargo.toml`: 声明 Tauri 2 依赖、rusqlite/keyring/reqwest 等，启用 `tt-enable-seq` feature 控制 SSE seq 契约。
- `tongtian-tauri/src-tauri/tauri.conf.json`: 配置产品名称、版本、窗口尺寸、安全策略、bundle targets=all、图标集。
- `tongtian-tauri/src-tauri/build.rs`: 调用 `tauri_build::build()` 生成 schema。

### 3. 开发与启动流程
- **Windows 初始化**: `init.ps1` 检测 Node.js/Rust，自动 `npm install`，根据是否安装 Rust 决定走 `npm run tauri dev` 或降级为浏览器 Mock 模式。
- **跨平台初始化**: `init.sh` 提供等效的 Bash 逻辑，支持 `MOCK_ENABLED` 环境变量控制。
- **开发启动**: `run-dev.ps1` 设置 `TT_AGENT_MOCK`/`TT_ENGINE` 环境变量后执行 `npx tauri dev`。
- **Tauri 构建**: `tauri.conf.json` 中 `beforeBuildCommand: "npm run build"`，前端先构建再进入 Rust 打包。

### 4. 测试体系
- 前端单元测试: `vitest run`（`tests/unit` 目录）
- 集成测试: `tests/integration/mock-agent.integration.test.ts`、`engine_sse_selftest.py`
- Rust 库测试: `cargo test --lib`
- 文档规范中约定 M1 里程碑需 `cargo test` 全绿并通过 CLI 集成测试场景。

### 5. 发布与打包
- Tauri 默认 `bundle.targets = "all"`，同时打包 Windows/macOS/Linux 原生安装包。
- Release profile 优化：LTO、单 codegen unit、strip symbols 减小体积。
- 版本号统一在 `package.json` 与 `Cargo.toml` 中同步为 `0.1.0`。

### 6. 约束与约定
- Rust 工具链可选：未安装时自动降级为纯前端 Mock 模式，保证开发可继续。
- Python Agent 引擎通过环境变量 `TT_ENGINE` 切换（agentscope/qwenpaw/mock），无需模型配置即可 Mock 运行。
- 所有构建产物输出到固定目录（Vite → `dist`，Tauri bundle → 默认 `src-tauri/target/release/bundle`），由 `tauri.conf.json` 统一管理。