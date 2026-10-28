---
kind: error_handling
name: 错误处理体系：统一错误码、结构化异常与 SSE 事件流
category: error_handling
scope:
    - '**'
source_files:
    - tongtian-tauri/src/shared/contracts.ts
    - tongtian-tauri/src/shared/error-messages.ts
    - tongtian-tauri/src/agent/exceptions.py
    - tongtian-tauri/src-tauri/src/lib.rs
    - tongtian-tauri/src-tauri/src/bridge/sse.rs
    - tongtian-tauri/src/main/agent/engine.ts
    - tongtian-tauri/src-tauri/src/model/service.rs
---

本项目的错误处理贯穿 Tauri (Rust) + TypeScript (Electron/Renderer) + Python Agent 引擎三层，采用「统一错误码枚举 + 结构化异常类 + SSE 事件流」的架构，确保跨语言、跨进程的错误可追踪、可展示。