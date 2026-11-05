# T0.1 结论：LiteLLM 版本探针

- 日期：2026-10-30
- 结论：**G0 通过**，M1 锁定 `litellm==1.106.6`、`mcp==2.3.0`、`aiohttp==3.14.5`
- 证据：[spike-litellm.json](spike-litellm.json)（由 `scripts/spike_litellm.py` 生成）
- 范围：只打本机假上游，未访问任何真实 provider

## 1. 环境

| 项 | 值 |
| --- | --- |
| OS | Windows 10.0.26300（x64） |
| Python | 3.13.3 |
| pydantic | 2.14.1 |
| fastapi | 0.136.3 |
| uvicorn | 0.49.0 |
| LiteLLM | 1.106.6 |
| openai（LiteLLM 传递依赖） | 2.54.0 |
| mcp | 2.3.0 |
| aiohttp | 3.14.5 |
| `pip check` | 无冲突 |
| LiteLLM 导入耗时 | 3.2–3.9 秒（冷启动） |

## 2. 安装阻塞与处理

阿里云 PyPI 镜像当前缺两个 LiteLLM 需要的版本，官方 PyPI 正常：

| 包 | 镜像最高 | LiteLLM 要求 | 处理 |
| --- | --- | --- | --- |
| `aiohttp` | 3.13.3 | `>=3.14.2` | 从 `https://pypi.org/simple` 装 `3.14.5` |
| `mcp` | 1.26.0 | `>=2.2,<3` | 从官方源装 `2.3.0` |

复现命令（Windows）：

```bash
.venv/Scripts/python.exe -m pip install "aiohttp==3.14.5" "mcp==2.3.0" --index-url https://pypi.org/simple
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe scripts/spike_litellm.py --json docs/spike-litellm.json
```

部署含义：内网/离线环境要么同步这两个包到私有镜像，要么在构建阶段允许访问官方 PyPI。**只配阿里云镜像会在 `pip install` 阶段直接失败**，不是运行时才报错。

## 3. 用例结果

13/13 通过，必测项无失败：

| 用例 | 结果 | 耗时 | 说明 |
| --- | --- | --- | --- |
| `openai.chat.nonstream` | PASS | ~0.9s（首次调用含冷启动） | 文本 + usage |
| `openai.chat.stream` | PASS | ~150ms | `stream_options.include_usage=true` 拿到 usage |
| `openai.chat.tools` | PASS | ~550ms（首次含 mcp 导入） | `tool_calls` 归一化正确 |
| `openai.chat.tools.stream` | PASS | ~12ms | 流式参数拼接正确 |
| `openai.chat.vision` | PASS | ~7ms | `image_url` 原样到达上游 |
| `openai.embeddings` | PASS | ~8ms | 向量与 prompt usage |
| `openai.token_counter` | PASS | ~1ms | 本地 tokenizer 可用 |
| `anthropic.chat.nonstream` | PASS | ~50ms | 文本 + usage |
| `anthropic.chat.stream` | PASS | ~15ms | usage 需 `stream_chunk_builder` |
| `anthropic.chat.tools` | PASS | ~8ms | `tool_use` → `tool_calls` |
| `gemini.chat.nonstream` | PASS | ~13ms | 原生 `generateContent` |
| `gemini.chat.stream` | PASS | ~14ms | 原生 `streamGenerateContent?alt=sse` |
| `gemini.chat.tools` | PASS | ~6ms | `functionCall` → `tool_calls` |

另外，升级依赖后 M0 的 `tests/test_contract.py` 仍全部通过。

## 4. M1 适配层必须遵守的规则

这些是探针发现的、文档里不明显的坑，直接决定 `app/adapters/litellm_adapter.py` 的写法：

1. **延迟导入**：`import litellm` 要 3.2–3.9 秒。只在适配器首次使用时导入，或在网关启动时预热；不要让 `app/main.py`、`tests/test_contract.py` 在模块导入阶段拉 LiteLLM。
2. **工具调用依赖 `mcp`**：LiteLLM 在带 `tools` 的请求路径上会 import `mcp`，缺失时直接 `ModuleNotFoundError`，与是否真的用 MCP 无关。`mcp>=2.2` 才有其引用的 `CacheableResult`；1.x 会 `ImportError`。
3. **流式 usage 分两种取法**：
   - OpenAI：请求带 `stream_options={"include_usage": True}`，最终 usage 在 `choices=[]` 的单独一帧里；LiteLLM 会把它挂到 chunk 上。
   - Anthropic / Gemini：流式 chunk 的 `usage` 为 `None`，必须在流结束后调 `litellm.stream_chunk_builder(chunks)` 取 `usage`（最后一个 chunk 的 `_hidden_params["usage"]` 也可读）。**不能只读 chunk.usage，否则配额结算会把这两家记成 0。**
4. **Gemini 的 `api_base` 要带 `/v1beta`**：只给 `http://host:port` 会请求 `/models/...` 而 404；给 `http://host:port/v1beta` 后路径正确。LiteLLM 走 HTTPX 原生协议，不依赖 `google-genai` SDK。
5. **Anthropic 会注入默认 `max_tokens=4096`**：网关必须每次显式传 `min(请求值, 策略上限, 模型上限)`，不能依赖 LiteLLM 默认值，否则部门/模型的输出上限失效。
6. **工具归一化可用**：三家都能把上游工具调用转成 OpenAI 形态的 `tool_calls`；Gemini 缺 id 时会生成合成 id，Anthropic 的 `input_json_delta` 会被拼成完整 arguments。M1 只需做反向翻译（OpenAI 入参 → Anthropic/Gemini 入参）。
7. **视觉**：OpenAI 面 `image_url` 原样透传；Anthropic/Gemini 的图片互转尚未在探针覆盖，M1.2 必须补测。
8. **版本读取**：LiteLLM 没有 `__version__` 属性，用 `importlib.metadata.version("litellm")`。
9. **静音**：导入后设 `litellm.suppress_debug_info = True`、`litellm.set_verbose = False`；环境变量 `LITELLM_LOCAL_MODEL_COST_MAP=True`、`LITELLM_TELEMETRY=False`，避免启动时拉远端成本表或打 provider 广告。
10. **成本不依赖 LiteLLM**：假模型没有价目，`completion_cost` 不可作为账本；网关按策略里的 `pricing` 用 `Decimal` 算 micros，LiteLLM 只提供 token 数。

## 5. 对 M1 计划的影响

| 计划项 | 调整 |
| --- | --- |
| T1.5 适配器 | 增加 `stream_chunk_builder` 取 usage；`mcp` 依赖；Gemini base 带 `/v1beta` |
| T1.8 用量落库 | 流式 Anthropic/Gemini 必须等 builder 结果再结算，不能零记账 |
| T1.9 启动自检 | 增加 LiteLLM 版本检查与冷启动耗时日志 |
| 部署 | `aiohttp`/`mcp` 需官方源或私有镜像同步；Docker 构建要显式处理 |
| 风险 | LiteLLM 升级需重跑本探针；`mcp` 2.x 引入 `httpx2` 等新依赖，注意供应链 |

## 6. 复现

```bash
cd tongtian-gateway
.venv/Scripts/python.exe scripts/spike_litellm.py --json docs/spike-litellm.json
```

脚本只启动 `tests/fake_upstreams.py` 的三个本机假上游，退出码非 0 表示有必测项失败。
