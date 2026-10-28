---
kind: external_dependency
name: 阿里云 DashScope 大模型 API
slug: dashscope
category: external_dependency
category_hints:
    - vendor_identity
    - auth_protocol
scope:
    - '**'
source_files:
    - tongtian-tauri/.env.example
---

### 通义千问模型服务
- 角色：主要的云端模型供应商，通过 AgentScope 的 DashScopeChatModel 接入
- 认证方式：通过 `TT_API_KEY` 环境变量注入 API Key（格式 sk-xxx），不支持其他认证协议
- 模型支持：常用模型包括 qwen-plus、qwen-max、qwen-turbo 等，通过 `TT_MODEL` 配置
- 使用场景：默认引擎配置，适合通用办公助手场景；当无 Key 时引擎返回 ENGINE_CONFIG_ERROR 而非崩溃
- 替代方案：可通过 `TT_PROVIDER=openai_compat` 切换到任意 OpenAI 兼容端点（DeepSeek/Kimi/Ollama 等）