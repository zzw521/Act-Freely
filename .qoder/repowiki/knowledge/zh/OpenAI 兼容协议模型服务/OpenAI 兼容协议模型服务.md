---
kind: external_dependency
name: OpenAI 兼容协议模型服务
slug: openai-compatible-api
category: external_dependency
category_hints:
    - vendor_identity
    - auth_protocol
scope:
    - '**'
source_files:
    - tongtian-tauri/.env.example
---

### 多模型供应商统一接入
- 角色：通过 OpenAI 兼容协议接入各类模型服务，包括 DeepSeek、Kimi、本地 Ollama 等
- 灵活性：同一套代码可同时支持云端 API 和本地推理服务，无需修改引擎逻辑
- 使用场景：企业内网部署、隐私敏感场景、成本优化需求；作为 DashScope 的备选方案
- 兼容性：AgentScope 的 OpenAIChatModel 自动适配不同供应商的细微差异