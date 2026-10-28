---
kind: external_dependency
name: Tauri 2.x 桌面应用框架
slug: tauri
category: external_dependency
category_hints:
    - vendor_identity
    - client_constraint
scope:
    - '**'
source_files:
    - tongtian-tauri/src-tauri/Cargo.toml
    - tongtian-tauri/package.json
    - tongtian-tauri/src-tauri/tauri.conf.json
---

### 桌面应用壳层
- 角色：替代 Electron 的桌面应用框架，使用系统 WebView（Windows 下为 WebView2）而非内置 Chromium，安装包从 ~90MB 降至 ~10-15MB
- 集成点：`src-tauri/` 目录包含 Rust 后端（Cargo workspace）、capabilities 权限配置、tauri.conf.json 窗口配置
- 关键特性：使用 `tauri-plugin-opener`、`tauri-plugin-dialog` 扩展系统能力；通过 `@tauri-apps/api` 在 TypeScript 前端调用 Rust command
- 开发模式：Vite 渲染进程运行在 localhost:7173，Rust 后端通过 tauri dev 自动热重载