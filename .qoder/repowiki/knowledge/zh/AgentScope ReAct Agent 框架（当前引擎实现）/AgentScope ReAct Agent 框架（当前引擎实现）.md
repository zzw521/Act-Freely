---
kind: external_dependency
name: AgentScope ReAct Agent 框架（当前引擎实现）
slug: agentscope
category: external_dependency
category_hints:
    - framework_behavior
    - sdk_real_api
scope:
    - '**'
source_files:
    - tongtian-tauri/engine/server.py
    - tongtian-tauri/.env.example
---

### 通天办公的 AI 推理引擎
- 角色：项目当前的 AI 推理与工具调用能力由 AgentScope 提供，通过自研 FastAPI 引擎服务暴露 HTTP/SSE 契约给 Rust 桥接层
- 集成点：`tongtian-tauri/engine/server.py` 使用 `ReActAgent`、`DashScopeChatModel`/`OpenAIChatModel`、`Toolkit`、`InMemoryMemory` 等核心组件
- 稳定用法：通过 `.env` 配置 `TT_PROVIDER`（dashscope/openai_compat）、`TT_MODEL`、`TT_API_KEY`、`TT_BASE_URL` 切换模型供应商；无 Key 时优雅降级返回 ENGINE_CONFIG_ERROR
- 架构约束：Rust 侧仅依赖 HTTP/SSE 契约（GET /health + POST /api/chat），不直接绑定 qwenpaw 包名；三档引擎开关（mock/agentscope/qwenpaw）通过环境变量 TT_ENGINE 控制
- 注意：AgentScope 仅为过渡期实现，目标态将完全用 Rust 重实现，该依赖将在 crates/agent 落地后移除