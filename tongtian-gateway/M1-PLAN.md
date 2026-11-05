# M1 实施计划：网关可用（真实上游 + RS256/JWKS + SQLite 配额与用量）

- 状态：**已完成**（M1.0–M1.5 全部完成并通过 G0–G5；G5 验收报告见 [docs/m1-acceptance.md](docs/m1-acceptance.md)，加固结果见 §14）
- 上游依据：[CONTRACT.md](CONTRACT.md)（`gateway-contract.v1`）
- 范围：`tongtian-gateway` 从骨架走到「桌面/引擎经网关真实对话」+「控制面看到用量」+「部门配额生效」
- 不在范围：计费开票、SSO/SCIM、多副本/Redis、rerank/audio、公网 SaaS

---

## 0. M1 的完成定义（一句话）

一个团队用户登录控制面 → 客户端拿到网关令牌 → 用 `auto` 别名流式对话成功（桌面、OpenAI SDK、Claude Code、Gemini CLI 四条路径至少各跑通一次）→ 控制面管理台 10 秒内看到这条用量 → 部门月配额超限时返回 `429 QUOTA_TOKENS_EXCEEDED` → 网关以 `TT_GATEWAY_ENV=production` 启动且只认 RS256/JWKS → 全程不落 prompt/response。

---

## 1. M1 锁定的决策

| # | 决策 | 理由 | 备选（不采用） |
| --- | --- | --- | --- |
| D1 | 控制面**现有 HS256 令牌不动**；新增 `POST /api/v1/auth/gateway-token` 换发 RS256 短期令牌（`aud=tongtian-gateway`，TTL 15 分钟且不超过 access token 剩余寿命） | 引擎服务模式（`tongtian-tauri/engine/server.py`）按 HS256 校验 `aud=tongtian-engine`，全局改 RS256 会直接打断引擎 | 全局 RS256（需同时改引擎、Tauri、部署文档，M1 风险过大） |
| D2 | 网关只信 RS256 + JWKS；HS256 仅限 development 且显式开启 | 契约 3.1 | 网关同时信两种算法（攻击面变大） |
| D3 | 供应商密钥只存网关，AES-GCM 加密，主密钥来自 `TT_GATEWAY_MASTER_KEY` | 契约 10、12 | 控制面代管密钥（违反数据归属） |
| D4 | LiteLLM 作为 **SDK**，外面套我们自己的 FastAPI；不用 LiteLLM Proxy | 保留单副本 + SQLite；避免 Proxy 自带的 user/team/key 模型与 Postgres | LiteLLM Proxy（部署与权限模型冲突） |
| D5 | 策略文档由控制面持有，网关只缓存；新增 `gateway_configs` 表按 `org_id` 存整份 JSON | 契约 5、10 | 网关自建策略编辑器（两套真相） |
| D6 | 网关 API Key 由网关签发与校验，控制面 UI 通过代理调用 | 契约 3.2、9 | 控制面存 Key 哈希（跨服务不一致） |
| D7 | 控制面新增 `source=gateway` / `origin=gateway` 用量；网关是经网关调用的唯一账本 | 契约 6.2、10 | 桌面与网关各记一遍再对账（必然重复计数） |
| D8 | 管理台复用控制面 `/ui/`，新增「模型网关」页签；浏览器永远拿不到网关管理令牌 | 用户要求「能复用复用」 | 网关自带完整后台（多一套登录与样式） |
| D9 | 价格写在策略的模型条目里（每 1M token），用 `Decimal` 计算 micros | 契约 5.2 已留 `priceVersion` | 网关内置价目表（改价要发版） |
| D10 | 先 OpenAI 面跑通再接 Anthropic/Gemini 面 | 三条链路共用内部规范化事件，先验证主路径 | 三面并行（翻译层错误难定位） |

### 1.1 仍需你拍板（默认按上表执行）

1. **网关管理鉴权**：M1 继续用 `X-TT-Admin-Token` 共享密钥 + 控制面服务端代理，够不够？（默认：够；M2 已补管理面 mTLS 与限流，见 [M2-PLAN.md](M2-PLAN.md)）
2. **部门生效方式**：一个令牌一个 `dept`，多部门靠重新取票或 `X-TT-Department`（默认：按契约）。
3. **默认嵌入模型**：M1 示例用 Azure `text-embedding-3-small`，实际部署由策略指定（默认：不写死）。

---

## 2. 里程碑与顺序

依赖关系：`M1.0 → M1.1 → M1.2`，`M1.3` 可与 `M1.2` 并行（不同仓库），`M1.4` 依赖 `M1.1 + M1.3`，`M1.5` 收尾。

| 阶段 | 内容 | 人日 | 前置 | 出口门槛（Gate） |
| --- | --- | --- | --- | --- |
| M1.0 | 决策落地：LiteLLM 版本钉死、令牌流、契约增补、测试脚手架 | 1–2 | — | G0：spike 脚本能经 LiteLLM 打到假上游并拿到 usage |
| M1.1 ✅ | 网关核心：SQLite、密钥加密、provider 管理、OpenAI 面全能力、配额、用量落库 | 4–6 | G0 | G1：OpenAI 非流式/流式/工具/视觉/嵌入全过契约测试 |
| M1.2 ✅ | 协议面：Anthropic、Gemini、countTokens、取消与中途错误、Claude Code / Gemini CLI 冒烟 | 5–7 | G1 | G2：两个 CLI 各完成一次真实对话（可接假上游）；未装 CLI 时用官方 SDK 替代 |
| M1.3 ✅ | 身份与策略：控制面 RSA/JWKS、换票接口、org/dept 表、策略下发、网关 JWKS 校验与缓存、引擎接线 | 4–6 | — | G3：无 HS256 也能跑通端到端鉴权 |
| M1.4 ✅ | 用量与运营：上报器、控制面接收与合并视图、部门报表、引擎 `origin=gateway`、管理台页签（原计划里的「Key 代理」未做，见 §7.4 延后项） | 4–6 | G1、G3 | G4：管理台 10 秒内看到用量，重复上报不重复计数 |
| M1.5 ✅ | 加固与交付：SSRF/DNS、重试与超时、安全复查、压测、部署文档、验收脚本 | 3–5 | G2、G4 | G5：`scripts/acceptance.py` 12/12 通过，生产模式启动（见 §14） |

合计 **21–32 人日**：两人并行约 3–4 周，单人约 5–7 周。

---

## 3. M1.0 决策与探针（1–2 天）

### 3.1 任务

| ID | 任务 | 产出 |
| --- | --- | --- |
| T0.1 | ✅ 2026-10-30：LiteLLM 版本探针，跑通 13/13（含三家假上游的文本、流式、工具、视觉、嵌入） | 锁定 `litellm==1.106.6`；结论见 [docs/spike-litellm.md](docs/spike-litellm.md) |
| T0.2 | ✅ 2026-10-30：适配层接口定稿：`ChatAdapter.stream()/complete()`、`EmbeddingAdapter.embed()`，输入输出都用内部规范化结构；假实现覆盖工具/思考/流式中断/嵌入 | `app/adapters/base.py`、`app/adapters/fake.py`、`tests/test_adapters.py`（85 项通过） |
| T0.3 | ✅ 2026-10-30：令牌流定稿（换票请求/响应、claims 表、JWKS 与轮换、网关侧校验顺序、客户端刷新策略） | 契约 §3.1 增补 |
| T0.4 | ✅ 2026-10-30：契约增补 C2–C9 全部落入契约，schema 与示例同步，新增 §16 变更记录 | 契约 v1 加法修订 |
| T0.5 | ✅ 2026-10-30：测试脚手架抽到 `tests/support.py`（环境引导、uvicorn 线程、`http()`、SSE 解析、假上游启停、令牌工具），两套现有测试复用；147 + 85 项通过 | 后续套件统一入口 |

### 3.2 G0 验收

- 探针脚本对三个假上游（OpenAI/Anthropic/Gemini）各返回一次合法响应与 usage。
- 若 LiteLLM 在 3.13 上不可用：**立即停**，改用「自写 httpx 适配 + 只借 LiteLLM 的 tokenizer/成本表」，此变更需回到本文修订，不擅自换方向。

### 3.3 T0.1 结果（已完成）

- 锁定：`litellm==1.106.6`、`mcp==2.3.0`、`aiohttp==3.14.5`；`pip check` 无冲突。
- 阻塞：阿里云镜像缺 `aiohttp>=3.14.2` 与 `mcp>=2.2`，需官方源或私有镜像同步。
- 适配层硬规则（详见 [docs/spike-litellm.md](docs/spike-litellm.md) §4）：
  1. LiteLLM 延迟导入（冷启动 3.2–3.9 秒）；
  2. 带 `tools` 的请求会 import `mcp`，必须随包锁定；
  3. Anthropic/Gemini 流式 usage 只能从 `litellm.stream_chunk_builder(chunks)` 取；
  4. Gemini `api_base` 必须带 `/v1beta`；
  5. Anthropic 默认注入 `max_tokens=4096`，网关必须每次显式传策略上限。

### 3.4 T0.2 结果（已完成）

- 内部结构统一为 OpenAI 形态消息 + 规范化流事件，协议面只做翻译，不感知上游差异。
- 错误分层：构造请求不合法抛 `ValueError`（协议层转 `INVALID_REQUEST`）；上游失败抛 `AdapterError`（仅允许 7 个适配器错误码）；流开始后的失败用 `StreamError` 收尾，不再抛异常。
- `Usage` 独立于上游：`cached` 是 prompt 子集、`reasoning` 是 completion 子集、`estimated` 标记估算，总量恒为 prompt+completion。
- `ModelTarget` 的 `api_key`/`extra_headers`/`extra_params` 不进 repr；`AdapterError.message` 统一过 `sanitize_error_message`。
- 工具调用参数保持 JSON 文本（`ToolCall.arguments`），流式增量由上游适配器负责拼装；协议层只消费 `ToolCallStart/Delta/End`。
- 假适配器行为可配置（正文分片、思考、工具调用、延迟、开始前失败、流中第 N 片后失败），协议层与配额结算的测试不再依赖真实上游。

---

## 4. M1.1 网关核心（4–6 天，已完成）

### 4.1 新增/修改文件

```text
app/
  db.py            新增：gateway.db 连接、WAL、schema 版本、迁移
  secrets.py       新增：AES-GCM 加解密、主密钥轮换、last4
  providers_store.py 新增：provider 与密钥的增删改查（管理 API 用）
  adapters/
    base.py        新增：ChatAdapter / EmbeddingAdapter 协议
    litellm_adapter.py 新增：LiteLLM 实现（延迟 import）
    fake.py        新增：测试用假适配器
  upstream.py      新增：SSRF 校验、DNS 解析、超时、重试、取消
  quota_sqlite.py  新增：SQLite 版预留/结算（替换内存实现）
  usage_store.py   新增：事件落库、聚合、待上报查询
  admin.py         改：真实 provider/密钥/配额接口
  protocols/openai.py 新增：从 protocols.py 拆出并实现
  main.py          改：启动自检、后台任务、生产门槛
  config.py        改：新增 JWKS/服务令牌/DB/加密相关配置
```

### 4.2 数据模型（网关 `gateway.db`）

```sql
schema_version(version INTEGER NOT NULL);

providers(
  id TEXT PRIMARY KEY, type TEXT NOT NULL, base_url TEXT NOT NULL,
  timeout_seconds INTEGER NOT NULL DEFAULT 120,
  allow_private INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
);

provider_keys(
  id TEXT PRIMARY KEY, provider_id TEXT NOT NULL REFERENCES providers(id),
  ciphertext BLOB NOT NULL, nonce BLOB NOT NULL, key_version INTEGER NOT NULL,
  last4 TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  created_at_ms INTEGER NOT NULL, rotated_at_ms INTEGER NOT NULL DEFAULT 0
);

usage_events(
  gateway_id TEXT NOT NULL, request_id TEXT NOT NULL,
  tenant TEXT NOT NULL, dept TEXT NOT NULL, sub TEXT NOT NULL,
  api_key_id TEXT NOT NULL DEFAULT '',
  protocol TEXT NOT NULL, endpoint TEXT NOT NULL,
  model_alias TEXT NOT NULL, model TEXT NOT NULL, provider TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL,
  cached_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL,
  cost_micros INTEGER NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT '',
  price_version TEXT NOT NULL DEFAULT '',
  latency_ms INTEGER NOT NULL, status TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '',
  stream INTEGER NOT NULL, estimated INTEGER NOT NULL DEFAULT 0,
  started_at_ms INTEGER NOT NULL, finished_at_ms INTEGER NOT NULL,
  uploaded_at_ms INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (gateway_id, request_id)
);
CREATE INDEX idx_usage_pending ON usage_events(uploaded_at_ms, finished_at_ms);

quota_counters(
  period TEXT NOT NULL, tenant TEXT NOT NULL, dept TEXT NOT NULL, sub TEXT NOT NULL,
  used_tokens INTEGER NOT NULL DEFAULT 0, updated_at_ms INTEGER NOT NULL,
  PRIMARY KEY (period, tenant, dept, sub)
);

policy_cache(
  tenant TEXT PRIMARY KEY, document_json TEXT NOT NULL,
  policy_version TEXT NOT NULL, etag TEXT NOT NULL DEFAULT '',
  fetched_at_ms INTEGER NOT NULL
);

api_keys(
  key_id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL,
  tenant TEXT NOT NULL, dept TEXT NOT NULL, sub TEXT NOT NULL,
  scopes_json TEXT NOT NULL, model_allowlist_json TEXT NOT NULL DEFAULT '[]',
  expires_at_ms INTEGER NOT NULL DEFAULT 0, revoked_at_ms INTEGER NOT NULL DEFAULT 0,
  created_at_ms INTEGER NOT NULL
);

audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at_ms INTEGER NOT NULL,
  actor TEXT NOT NULL, action TEXT NOT NULL, object TEXT NOT NULL,
  before_json TEXT NOT NULL DEFAULT '{}', after_json TEXT NOT NULL DEFAULT '{}'
);
```

规则：

- 单进程单 worker；写事务统一 `BEGIN IMMEDIATE` + `busy_timeout`。
- `usage_events` 是事实来源；`quota_counters` 可由事件重建（提供 `scripts/rebuild_quota.py`）。
- 不存 prompt/response；表里没有任何正文字段。

### 4.3 任务拆分

| ID | 任务 | 结果 |
| --- | --- | --- |
| T1.1 | `db.py`：连接、WAL、迁移、`tx()`；`schema_version` 从 1 开始 | ✅ 2026-10-30：空目录自动建库，旧库重复启动不报错；版本未知直接拒绝 |
| T1.2 | `secrets.py`：AES-GCM；`TT_GATEWAY_MASTER_KEY`（base64 32 字节）；密文带 `keyVersion` | ✅ 2026-10-30：加解密、篡改失败、轮换后旧密文可读、缺版本拒绝 |
| T1.3 | provider 管理：增改查、启停、密钥新增/轮换/禁用；明文仅创建响应返回一次 | ✅ 2026-10-30：管理 API 契约测试通过；列表/DB/审计只有 `last4` |
| T1.4 | `upstream.py`：URL 校验（scheme、禁止 userinfo、私网/回环/metadata 黑名单）、DNS 解析、超时、连接错误映射 | ✅ 2026-10-30：覆盖 `127.0.0.1`、`169.254.169.254`（allowPrivate 也拦）、`10/8`、`192.168/16`、`::1`；仅 vLLM/Ollama 可开 `allowPrivate` |
| T1.5 | `adapters/litellm_adapter.py`：OpenAI 兼容 / Azure / Anthropic / Gemini 的参数映射；流式统一成内部事件；Anthropic/Gemini 流式结束后用 `stream_chunk_builder` 取 usage；Gemini base 带 `/v1beta` | ✅ 2026-10-30：三家流式都能拿到非零 usage；`response.chunks` 区分上游真 usage 与本地合成；密钥不进日志/异常 |
| T1.6 | OpenAI 面：`/v1/chat/completions`（含 `stream`）、`/v1/embeddings`、`/v1/models`；tools、vision 透传 | ✅ 2026-10-30：HTTP 集成覆盖 200/400/401/403/429/502/503；流式、工具、视觉、嵌入与别名回退全过 |
| T1.7 | `quota_sqlite.py`：按月预留、并发计数、结算、释放；`X-TT-Quota-Remaining/Reset` | ✅ 2026-10-30：10 并发对上限 4 恰好 4 成功、6 个 `QUOTA_CONCURRENCY_EXCEEDED`；`scripts/rebuild_quota.py` 可按账本重建 |
| T1.8 | `usage_store.py`：请求结束写事件（成功/失败/取消）；`estimated` 标记；价格计算；Anthropic/Gemini 流式必须等 builder 结果再结算 | ✅ 2026-10-30：`Decimal` 计价 6 位小数；上游直接报错记 0 用量，已产生输出后中断才按估算记账 |
| T1.9 | 启动自检：production 必须有 RS256/JWKS、master key、admin token，缺一即退出 | ✅ 2026-10-30：缺项逐条点名；`hs256-dev` 在 production 被拒 |
| T1.10 | 假上游补齐 OpenAI 错误响应（400/401/429/500、流中断开） | ✅ 2026-10-30：错误映射与断流不伪造结束帧的测试通过 |

### 4.4 G1 验收

- 关闭 development 模式后，OpenAI SDK（`openai` Python 包）用 `base_url=http://127.0.0.1:8001/v1` 完成：普通对话、流式、工具调用、图片输入、embedding。
- 同一 `requestId` 重放不会产生两条 `usage_events`。
- 杀掉进程重启，配额与用量不丢。

### 4.5 T1.x 结果（已完成）

- 适配层新增两条硬规则（补充 [docs/spike-litellm.md](docs/spike-litellm.md) §4）：
  1. 流是否真的结束看 `CustomStreamWrapper.received_finish_reason / intermittent_finish_reason`——LiteLLM 会在上游断流时补一个 `finish_reason=stop` 的合成帧，不能直接信；
  2. usage 是否上游真实上报看 `response.chunks`，返回给调用的 chunk 里那份可能是 `stream_chunk_builder` 本地合成的。
- 结算口径：上游直接报错（401/429/5xx/400）不估算 token，只写 `status=error` 的 0 用量事件；流已开始或请求已取消，才按已产生的输出估算并标记 `estimated`。
- 配额并发计数仍在进程内，重启丢弃未结算预留；月度已用是 SQLite 事实，可由 `usage_events` 重建。
- G1 的两条进程级验收（真实 OpenAI SDK 冒烟、杀进程重启不丢数据）留到 M1.4 与整体联调一起做，M1.1 先用假上游覆盖同一路径。

---

## 5. M1.2 协议面（已完成）

### 5.1 任务

| ID | 任务 | 验收 |
| --- | --- | --- |
| T2.1 | 内部规范化事件定稿：`start / text_delta / thinking_delta / tool_call_start / tool_call_delta / tool_call_end / usage / stop / error` | 单测：OpenAI 流 → 内部事件 → 三种编码器往返 |
| T2.2 | Anthropic 非流式 `/v1/messages`：`system`、`content` blocks、`tool_use`/`tool_result`、`stop_reason`、`usage` | 与官方 schema 对比字段齐全 |
| T2.3 | Anthropic 流式：`message_start` → `content_block_start/delta/stop` → `message_delta` → `message_stop`；`input_json_delta` | Claude Code 冒烟通过 |
| T2.4 | `/v1/messages/count_tokens` | 返回 `input_tokens`；无 tokenizer 时标记估算 |
| T2.5 | Gemini 非流式/流式：`contents/parts/functionCall`、`usageMetadata`、`finishReason` | Gemini CLI 冒烟通过 |
| T2.6 | Gemini `:countTokens`、`/v1beta/models` | 模型列表只列策略允许项 |
| T2.7 | 中途错误：流已开始后上游失败，按协议发原生 error 事件再结束；事件落库 `status=error` | 假上游在第一个 chunk 后断开，三种协议都不挂起 |
| T2.8 | 客户端取消：`asyncio.CancelledError` → 取消上游 → 按已知 usage 结算并标记 `cancelled` | 100 次取消无泄漏（并发计数归零） |
| T2.9 | 工具多轮：客户端回传 tool_result 后再请求，messages 形态转换正确 | OpenAI↔Anthropic↔Gemini 三种入参各一例 |
| T2.10 | 视觉输入：URL 与 base64 两种形态在三种协议间转换 | 三种协议各一例，图片大小限制生效 |

### 5.2 G2 验收

- `claude` CLI 指向网关（`ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN=ttg_...`）完成一次带工具的对话。
- `gemini` CLI 指向网关（`GOOGLE_GEMINI_BASE_URL` + `GEMINI_API_KEY=ttg_...`）完成一次对话。
- 两个 CLI 的用量各在 `usage_events` 留一条记录，`protocol` 分别为 `anthropic`/`gemini`。

> 注：真实 CLI 冒烟需要对应工具已安装；未安装时以「官方 SDK + 同协议请求」作为替代证据，并在验收记录里注明。

### 5.3 T2.x 结果（已完成）

| 任务 | 结果 | 证据 |
| --- | --- | --- |
| T2.1 规范化事件与编码器 | 内部 `StreamEvent` 直接驱动三套编码器：OpenAI chunk、Anthropic 命名事件、Gemini data 帧 | `tests/test_protocols.py` 的编码器往返用例 |
| T2.2–T2.4 Anthropic | `/v1/messages` 支持 `system`、文本/图片、`tool_use`/`tool_result`、`stop_reason`、`usage`；流式按 `message_start → content_block_* → message_delta → message_stop` 输出，工具参数走 `input_json_delta`；`count_tokens` 无原生 tokenizer，返回估算并带 `X-TT-Estimated: 1` | 协议测试 + 官方 Anthropic SDK 冒烟 |
| T2.5–T2.6 Gemini | `contents/parts`（text、inlineData、fileData、functionCall、functionResponse）、`systemInstruction`、`functionDeclarations`、`toolConfig`、`generationConfig` 全部翻译；响应输出 `candidates`/`usageMetadata`/`finishReason`；`:countTokens` 估算；`/v1beta/models` 只列策略允许项 | 协议测试 + 官方 Google GenAI SDK 冒烟 |
| T2.7 中途错误 | 假上游吐完首个 chunk 后断开：三种协议都在已开始流后发一帧原生错误事件，不发正常结束帧；用量 `status=error`、`estimated=true` | `tests/test_protocols.py` 中途错误用例 |
| T2.8 客户端取消 | `GeneratorExit`/`CancelledError` → 按已产生内容估算结算，`status=cancelled`、`errorCode=CLIENT_CANCELLED`；100 次取消后并发与预留归零 | `tests/test_protocols.py` 取消用例 |
| T2.9 工具多轮 | Anthropic `tool_result`、Gemini `functionResponse`（无 id，按名字配回 `call_<n>`）都能回放成内部 tool 消息 | 协议测试检查上游请求体 |
| T2.10 视觉 | Anthropic base64/url、Gemini inlineData/fileData 均转换成内部图片 part；单图解码后 20MB 上限，超出 413 | 协议测试（含上限用例） |

G2 证据：本机未安装 `claude` / `gemini` CLI，改用官方 SDK 作为替代证据——
`python scripts/smoke_sdks.py` 用 `anthropic` 与 `google-genai` SDK 指向网关，
完成 Anthropic 工具回合、工具多轮、流式，以及 Gemini 非流式、流式；
`usage_events` 分别留下 `protocol=anthropic` / `protocol=gemini` 的记录。

---

## 6. M1.3 身份与策略（4–6 天）

### 6.1 控制面（`tongtian-server`）

| ID | 任务 | 文件 | 验收 |
| --- | --- | --- | --- |
| T3.1 | RSA 密钥管理：启动生成/加载 `{data_dir}/keys/jwt-rs256-<kid>.pem`，保留上一把用于轮换 | `app/keys.py`（新） | 重启不换 `kid`；轮换后新旧令牌在宽限期内都可验 |
| T3.2 | `GET /.well-known/jwks.json`（公开，无鉴权） | `app/auth.py` / `app/main.py` | 返回 `kty/kid/use/alg/n/e` |
| T3.3 | `POST /api/v1/auth/gateway-token`：入参现 access token；出参 RS256 令牌 | `app/auth.py` | claims 见 §6.3；HS256 令牌无效/过期时 401 |
| T3.4 | 组织/部门表：`orgs`、`departments`、`user_departments`；存量用户回填默认组织与默认部门 | `app/db.py` | 迁移可重复执行；存量用户 `dept` 非空 |
| T3.5 | 用户管理 API 扩展：分配部门、主部门、部门列表；`/api/v1/admin/users` 返回 `depts` | `app/auth.py` | PATCH 后重新取票生效 |
| T3.6 | `gateway_configs(org_id, document_json, version, updated_at_ms)` + 管理端读写 | `app/db.py`、`app/gateway_policy.py`（新） | 保存时用 `PolicyDocument` 校验；版本号自增 |
| T3.7 | `GET /api/v1/gateway/policy?tenant=`：服务令牌鉴权，`ETag`/`304` | `app/gateway_policy.py` | 服务令牌错误 401；`If-None-Match` 命中 304 |
| T3.8 | 服务令牌：`TT_GATEWAY_SERVICE_TOKEN`（控制面侧配置），常量时间比较 | `app/config.py` | 缺失时该路由 503 而不是放行 |
| T3.9 | 依赖新增 `cryptography`（RS256 签名与 JWKS） | `requirements.txt` | 干净 venv 可安装 |

### 6.2 网关

| ID | 任务 | 文件 | 验收 |
| --- | --- | --- | --- |
| T3.10 | `app/jwks.py`：拉取、缓存 10 分钟、未知 `kid` 强制刷新、失败降级、`AUTH_UNAVAILABLE` | 新 | 控制面断开且缓存有效仍可验票；无缓存返回 503 |
| T3.11 | `auth.py` 改：RS256 优先；claims 校验 `iss/aud/exp/type`；`tenant/dept/depts/roles`（兼容 `org/role`） | 改 | 旧 claim 形态、部门越权、算法混淆（`alg=none`）全部拒绝 |
| T3.12 | `policy_client.py`：从控制面拉策略、ETag、SQLite 缓存、TTL 300s、stale grace 24h、`X-TT-Policy-Stale` | 新 | 控制面停机 1 小时后仍服务并带 stale 头；超过 24h 返回 503 |
| T3.13 | 生产门槛改为：RS256 + JWKS URL + master key + admin token 齐全 | `main.py` | 缺项退出并打印具体变量名 |

### 6.3 网关令牌（契约增补）

```json
{
  "sub": "u_123",
  "sid": "s_abc",
  "tenant": "acme",
  "dept": "dept-sales",
  "depts": ["dept-sales", "dept-eng"],
  "roles": ["user"],
  "org": "acme",
  "role": "user",
  "iss": "tongtian-auth",
  "aud": "tongtian-gateway",
  "type": "access",
  "iat": 1761825600,
  "exp": 1761826500
}
```

- 签名：RS256；`kid` 指向当前 JWKS 公钥。
- 有效期：15 分钟，且 `exp = min(now + 900, access token 的 exp)`；不提供 refresh，过期后客户端用原 access token 重新换票。
- `aud` 只给 `tongtian-gateway`，引擎不接受该令牌（`aud` 不匹配）。
- 控制面不因换票延长会话，不写新 session。
- 换票请求体可带 `{"dept": "..."}`（缺省取主部门）；完整协议、JWKS 轮换与失败码见契约 §3.1。

### 6.4 引擎/客户端接线

| ID | 任务 | 文件 | 验收 |
| --- | --- | --- | --- |
| T3.14 | 引擎模型配置支持「网关模式」：`base_url={gateway}/v1`，密钥用换票得到的令牌 | `tongtian-tauri/engine/server.py`（模型构建处） | 配置后对话走网关 |
| T3.15 | 换票与缓存：TTL 到期前 2 分钟刷新；401 时强制刷新一次 | 引擎侧 | 连续对话 20 分钟无 401 |
| T3.16 | 令牌只存内存/钥匙串，不写日志、不写配置文件 | 引擎侧 | 日志扫描无 `eyJ` 明文 |

### 6.5 G3 验收

- 关掉开发态 HS256，桌面经网关完成一次对话。
- 伪造 `tenant` 或把 `dept` 改成不在 `depts` 里的值 → 403。
- 控制面重启换 `kid` → 网关在未知 `kid` 时刷新 JWKS 并继续服务。

### 6.6 T3.x 结果（已完成）

| 任务 | 结果 | 证据 |
| --- | --- | --- |
| T3.1 RSA 密钥管理 | `{data_dir}/keys/jwt-rs256-<kid>.pem` + `index.json`；重启复用当前 `kid`，`TT_SERVER_JWT_KEY_MAX_AGE_SECONDS` 到期轮换，JWKS 保留新旧两把（`MAX_KEYS=2`） | 控制面 `tests/test_identity.py`「轮换后 JWKS 同时保留新旧公钥」「轮换前的网关票仍能验签」 |
| T3.2 JWKS | `GET /.well-known/jwks.json` 公开无鉴权，`Cache-Control: max-age=300`，只含 `kty/kid/use/alg/n/e` | 同上「JWKS 200 且包含公钥」「公钥字段齐全且只含公开参数」 |
| T3.3 换票 | `POST /api/v1/auth/gateway-token` 只收 HS256 access token；claims 按 §6.3；过期/非 access/会话吊销/无组织/无部门分别 401/403；`no-store` | 同上换票用例（基本路径、拒绝矩阵、个人账号） |
| T3.4–T3.5 组织/部门 | `orgs`/`departments`/`user_departments` 迁移，存量用户回填 `dept-default`；个人账号无组织；`PATCH /api/v1/admin/users/{uid}` 支持 `org`/`depts`/`primaryDept`，改完重新登录生效 | 同上「分配组织后默认部门成员」「清空部门后成员为空」「用户列表带部门与主部门」 |
| T3.6–T3.8 策略存储与分发 | `gateway_configs` 按组织存 `PolicyDocument`，版本号自增；`GET /api/v1/gateway/policy` 常量时间校验服务令牌，`ETag`/`304`，未配置服务令牌 → 503 | 同上「保存时保留签发时间」「ETag 等于策略版本」「控制面未配置服务令牌 → 503」 |
| T3.9 cryptography | 控制面与网关 `requirements.txt` 均带 `cryptography`，干净 venv 可直接安装 | 两个服务测试 venv 实跑 |
| T3.10 JWKS 缓存 | 10 分钟 TTL、未知 `kid` 立即刷新（两次刷新间隔 ≥30 秒）、刷新失败继续用旧钥、无缓存 `AUTH_UNAVAILABLE` | 网关 `tests/test_identity.py`「未知 kid 触发 JWKS 刷新」「JWKS 不可达时仍可用缓存公钥」「未知 kid 且 JWKS 不可用」，含 `JwksClient` 单元用例 |
| T3.11 RS256 校验 | `alg=none`/HS256/缺 `kid`/过期/`exp−iat>900`/`tenant` 与 `org` 冲突/`dept` 不在 `depts`/缺 `dept`/缺 `roles`/非 access/受众或签发方不符全部拒绝；`X-TT-Department` 越权 403 | 同上 11 组拒绝用例与部门切换用例 |
| T3.12 策略客户端 | ETag 条件拉取 + 304、SQLite 双层缓存、TTL 300 秒、stale 宽限 24 小时并回 `X-TT-Policy-Stale: 1`；控制面明确 404（无该租户策略）→ 403 `FORBIDDEN`，不吃旧缓存 | 同上「策略条件请求命中 304」「策略版本更新后生效」「控制面不可达时使用宽限缓存」「未知租户被拒绝」「控制面明确无策略时 403」 |
| T3.13 启动门槛 | 生产要求 `rs256` + JWKS + master key + admin token（配置控制面时还要服务令牌），缺项退出并点名变量 | `app/startup.py` + 现有 `test_core.py` 启动自检用例 |
| T3.14–T3.16 引擎网关模式 | `modelConfig.gateway = {baseUrl, controlPlaneUrl, dept, accessToken?}`；OpenAI SDK 每次请求调用 `api_key` 取票；按 `(controlPlaneUrl, dept, sha256(access))` 复用换票缓存；到期前 2 分钟刷新；401 时强制重换一次（流式仅在未出首个 chunk 时重试）；令牌仅存内存、`repr` 脱敏、错误信息只带固定码 | 引擎 `tests/integration/engine_gateway_selftest.py`（缓存窗口、单飞、401 重试、装配复用、日志无令牌） |

G3 证据（`python scripts/g3_acceptance.py`，约 1 分钟，跨三个真实进程）：

1. **关掉 HS256 完成对话**：网关以 `TT_GATEWAY_JWT_MODE=rs256` 启动，控制面 HS256 access token 直连网关 → 401；换票拿到 RS256 票后，`/v1/chat/completions` 非流式与流式均经假上游完成（`choices[0].message.content == "你好"`、流以 `data: [DONE]` 收尾）。
2. **越权**：篡改票内 `tenant` → 签名校验失败 401（RS256 下伪造 claim 无法通过验签，比 403 更早拒绝）；`X-TT-Department: dept-sales`（用户只属于 `dept-default`）→ 403 `FORBIDDEN`；切到已加入的 `dept-default` → 200；有效签名但控制面无策略的租户 → 403 `FORBIDDEN`。
3. **换 kid 继续服务**：控制面重启触发密钥轮换，新票 `kid` 变化；网关在未知 `kid` 时刷新 JWKS 后新票照常对话，旧票在宽限期内仍可用。未知 `kid` 刷新受 30 秒节流约束，验收脚本显式等待该窗口。

两侧日志（`--no-access-log`）扫描无 `eyJ` 明文，符合 T3.16「令牌不写日志」。

---

## 7. M1.4 用量与运营（4–6 天）

### 7.1 网关

| ID | 任务 | 验收 |
| --- | --- | --- |
| T4.1 | `usage_uploader.py`：后台任务，200 条 / 5 秒，指数退避，失败保留 30 天 | 控制面停机 10 分钟，恢复后全部补报且无重复 |
| T4.2 | 上报体按契约 6.2；服务令牌鉴权；`batchId` 幂等 | 同一批重放两次，控制面 `duplicates` 正确 |
| T4.3 | `GET /gateway/v1/admin/usage`：按租户/部门/用户/模型/日聚合 | 与 `usage_events` 手算一致 |
| T4.4 | `PATCH /gateway/v1/admin/quotas/{principal}`：调整当月额度（写审计） | 调整后立即生效 |

### 7.2 控制面

| ID | 任务 | 文件 | 验收 |
| --- | --- | --- | --- |
| T4.5 | `POST /api/v1/gateway/usage`：服务令牌；`(gateway_id, request_id)` 幂等；按日聚合进 `usage_rollup`（`source=gateway`、`origin=gateway`） | 重放不重复计数；非法行整批拒绝 |
| T4.6 | `gateway_usage_events` 表只留幂等键与最小字段，保留 30 天清理任务 | 清理后旧批次重放仍判重（保留键到 30 天） |
| T4.7 | `merged_view` 扩展：把 `origin=gateway` 的 desktop/engine 行排除，计入 gateway 行；新增 `gatewayRows`、`gatewayDedupedRows` | 与契约口径一致；现有 `desktopRows/engineRows` 不回归 |
| T4.8 | 部门用量报表：`GET /api/v1/admin/usage/departments?period=` | 按部门汇总，与用户明细相加一致 |
| T4.9 | 引擎上报处标记 `origin=gateway`（经网关的调用） | 合并视图不重复计数 |
| T4.10 | 管理代理：`/api/v1/admin/gateway/*` → 网关管理 API；浏览器不接触 admin token；操作写审计 | 未登录 401、非管理员 403 |
| T4.11 | `/ui/` 新增「模型网关」页签：健康、provider、密钥（新增后只显示 last4）、配额、用量 | 手工走查四种角色视角 |
| T4.12 | `/ui/` 新增「组织与部门」页签：部门增改、成员分配 | 分配后重新登录 claims 正确 |

### 7.3 G4 验收

- 一次网关对话后，控制面管理台在 10 秒内出现该条用量（含 model、tokens、latency、cost）。
- 同一批用量上报 3 次，总量不变。
- 桌面经网关的调用在合并视图只算一次；BYOK 直连调用照常由客户端上报。
- 部门月额度调到 1 万 token，超限后返回 `429 QUOTA_TOKENS_EXCEEDED`，响应头带重置时间。

### 7.4 T4.x 结果（已完成）

| 任务 | 结果 | 证据 |
| --- | --- | --- |
| T4.1 上报器 | `app/usage_uploader.py`：后台线程每 `TT_GATEWAY_USAGE_FLUSH_SECONDS`（默认 5 秒）推一批，单批最多 `TT_GATEWAY_USAGE_BATCH_SIZE`（200）条；失败指数退避 1→300 秒，重试同一批复用 `batchId`；`400/409/413/415/422` 判为确定性拒绝，跳过并记 `lastError`，其余（网络错误、401/403/404/405/429/5xx）保留重试；未配置控制面或服务令牌时不启动 | 网关 `tests/test_ops.py`「控制面不可达时不上报成功」「事件留在本地队列」「同一批重试复用 batchId」「控制面恢复后补发」「网络异常不丢事件」「坏批次被跳过」「跳过后队列可继续前进」「未启用不启动线程」；10 分钟长停机未在 M1.5 专项重跑（压测聚焦请求路径），上线前按 `docs/deploy.md` 的「上报积压」监控项演练一次 |
| T4.2 批量体与鉴权 | `{contractVersion, gatewayId, batchId, events[]}`，事件即契约 §6.1；`Authorization: Bearer TT_GATEWAY_SERVICE_TOKEN`，与控制面拉策略同一个令牌、常量时间比较 | 同上「上报端点指向控制面」「上报用服务令牌」「事件标 origin=gateway」「负载带网关标识/批次标识」 |
| T4.3 管理用量聚合 | `GET /gateway/v1/admin/usage?tenant=&dept=&sub=&model=&period=&limit=`，按 用户×模型×日×币种 聚合，返回 `rows` + `totals`（含 `costs`、`avgLatencyMs`）；`period` 非法 400 | 同上「调用次数/prompt/completion/total/成本与手算一致」「按 用户/模型/日/币种 分组」「部门/用户/模型过滤」「无数据时为空」「用量查询需要管理令牌」 |
| T4.4 配额调整 | `PATCH /gateway/v1/admin/quotas/{tenant:dept:sub}`，`*` 表示部门级；写 `quota_overrides` 与审计、立即生效；`monthlyTokens=null` 恢复策略值；个人覆盖优先于部门级；响应回 `effectiveMonthlyTokens/usedTokens/remainingTokens/resetAt/updatedBy`，`period` 为当月（不能用 `resetAt` 截月份） | 同上「部门级额度调整成功」「下调后请求被拒（立即生效）」「未设个人覆盖的成员仍受部门级限制」「清除部门覆盖成功」「配额调整写审计」，加 `tests/test_core.py`「配额快照的 period 是当月」 |
| T4.5 控制面接收 | `POST /api/v1/gateway/usage`：`(gateway_id, request_id)` `ON CONFLICT DO NOTHING` 判重，增量并入 `usage_rollup`（`source=gateway`、`device_id=gatewayId`、`origin=gateway`），成本按币种累加 micros；任一行不合法整批 400，不写半批 | 控制面 `tests/test_gateway_usage.py`（35 项）「首批入库 accepted=3」「第 N 次重放 duplicates=3」「重放三次总量不变」「批内重复 requestId 只计一次」「非法批不写入半批」「未知契约版本整批拒绝」「控制面未配置服务令牌 503」 |
| T4.6 判重键保留 | `gateway_usage_events` 只留幂等键与最小计量字段；每次入库顺带清理，每小时最多一次，保留 `TT_GATEWAY_USAGE_RETENTION_DAYS`（默认 30 天） | 同上「超过保留期的判重键被清理」「保留期内的判重键保留」「清理后重放旧请求仍判重」 |
| T4.7 合并视图 | `merged_view` 计入 `source=gateway` 行（`gatewayRows`），排除任何非网关来源里 `origin=gateway` 的客户端副本（`gatewayDedupedRows/Tokens`）；引擎 `origin=desktop` 的既有去重规则不变 | 同上「合并视图计入网关行」「客户端 origin=gateway 行被排除且披露」「引擎 desktop 行仍按原规则去重」「网关行数量不受客户端上报影响」；`tests/test_server.py` 原有合并视图用例未回归 |
| T4.8 部门报表 | `GET /api/v1/admin/usage/departments?period=&org=`，按 `(org, dept)` 汇总，行过滤与逐用户明细同一套代码路径；未分配部门显示「未分配」 | 同上「部门汇总与用户明细同口径」「部门相加等于用户明细之和」「非管理员 403」 |
| T4.9 引擎来源标记 | 网关模式下 `UsageTrackingModel` 固定 `origin=gateway`，引擎台账里经网关的调用带该标记，上报后被合并视图排除 | 引擎 `tests/integration/engine_gateway_selftest.py`「装配：网关调用的台账来源固定为 gateway（合并视图据此去重）」 |
| T4.10 管理代理 | `/api/v1/admin/gateway/*` → 网关 `/gateway/v1/admin/*`：`require_admin` 挡在转发前，服务端补 `X-TT-Admin-Token` 与 `X-TT-Admin-Actor`；固定目标主机，拒绝路径穿越；请求体 2MB 上限；网关错误信封原样透传；响应 `Cache-Control: no-store`；写操作落审计（密钥字段脱敏为 `***`）；未配置地址/令牌 503，连接失败 502，超时 504 | 控制面 `tests/test_gateway_admin.py`（28 项）「未登录 401」「非管理员 403」「响应不回显管理令牌」「密钥请求体确实转发到网关」「写操作落一条审计」「读操作不写审计」「旧令牌已失效」 |
| T4.11 模型网关页签 | `/ui/` 管理员「模型网关」：健康与上报队列、provider 新建/启停、密钥新增（明文只出现一次，列表只有 `last4`）、配额快照与当月调整、网关本地用量、租户策略读写；新建 provider 后同时刷新健康计数 | 真实浏览器走查：管理员可见并可完成新建 provider → 写密钥 → 调部门额度全链路；非管理员无该入口、直连 `#/admin` 被路由挡回；密钥明文只在创建响应内出现 |
| T4.12 组织与部门页签 | `/ui/` 管理员「组织与部门」：部门新建/改名、成员分配与移出；改名后成员提示同步刷新；分配后页面明确提示需重新登录 | 真实浏览器走查：把成员分配进 `dept-eng` 后重新登录，换票 claims 含 `depts=["dept-default","dept-eng"]` 且 `dept` 为主部门 |

G4 证据（`python scripts/g4_acceptance.py`，约 40 秒，真实控制面 + 真实网关 + 假上游三个进程，43 项断言）：

1. **10 秒内出用量**：非流式对话完成后轮询 `GET /api/v1/usage/me`，实测 **5.0 秒**（默认 5 秒 flush 节奏）出现该条用量，`sources.gatewayRows=1`，模型行 `fake-chat` 带 `promptTokens=12`、`completionTokens=8`、`calls=1`、`latencySamples=1` 与 `avgLatencyMs`，`costs=[{"currency":"USD","micros":28}]`（与策略价 1.00/2.00 每 1M 手算一致）。
2. **同批上报 3 次总量不变**：同一 `batchId`/`requestId` 连推三次 → `accepted=1,duplicates=0` 后两次 `accepted=0,duplicates=1`；混合批次（1 旧 + 1 新）只计入新行，重放后全部判重；`/api/v1/usage/users/{sub}` 的 `tokensUsed` 停在 25、`calls` 停在 2。
3. **合并视图只算一次**：客户端把同一次网关调用按 `origin=gateway` 再报一遍 → 总量不变，`gatewayDedupedRows=1`/`gatewayDedupedTokens=20`；BYOK 直连行（`source=desktop`，40+10）照常计入，`desktopRows=1`、总量 +50；部门报表逐部门相加等于逐用户明细之和。
4. **部门额度超限**：经管理代理把 `acme:dept-default:*` 当月额度调到 10000（`effectiveMonthlyTokens=10000`，立即生效，小请求照常 200）；随后 7000 字中文 + `max_tokens=4096` 的预留（约 11096 token）触发 `429 QUOTA_TOKENS_EXCEEDED`，响应头 `X-TT-Quota-Reset` 指向下月 1 日 UTC；被拒绝的调用在本地账本与控制面都不产生用量。

两侧日志（`--no-access-log`）扫描无网关管理令牌、服务令牌、provider 密钥、主密钥与 `eyJ` 明文；密钥只在创建响应里出现一次，列表仅 `last4`。

**M1.4 未做的延后项（原写在里程碑行里的「Key 代理」）**：§7.2 没有为它拆任务，G4 也不依赖，因此本节按契约 §3.2 与决策 D6 明确记账——网关 API Key（`ttg_<keyId>_<secret>`）的签发与吊销在 M1 尚未实现；**M3 已补齐（见 [M3-PLAN.md](M3-PLAN.md) 与 [docs/m3-acceptance.md](docs/m3-acceptance.md)）**，以下为当时的记账：

- `POST /gateway/v1/admin/api-keys`、`DELETE /gateway/v1/admin/api-keys/{key_id}` 通过管理令牌校验后返回 `501 NOT_IMPLEMENTED`；`api_keys` 表已随 T1.1 迁移建好，但没有仓储与校验实现。
- 三协议面的鉴权入口对 `ttg_` 前缀直接拒绝（`AUTH_REQUIRED`「网关 API Key 尚未启用（留待后续里程碑）」），不会误把 Key 当 JWT 解。
- 需要 Key 的 CLI（Claude Code / Gemini CLI）在 M1 用控制面换发的短期 RS256 令牌跑通；`ttg_` Key 属长期凭据，涉及哈希存储、scopes、模型白名单与吊销传播，与 §11 的「细粒度 RBAC」一起留待后续里程碑（M2 已聚焦管理面 mTLS 与限流）。
- 控制面的网关管理代理（T4.10）已覆盖运营侧真正需要的读用量、调配额、provider 与密钥轮换，M1.4 的运营闭环不依赖 Key 代理。

---

## 8. 契约增补清单（M1 开工前先改 CONTRACT.md）

均是**加法**，`contractVersion` 仍为 `gateway-contract.v1`；旧客户端忽略未知字段。

| # | 增补 | 位置 |
| --- | --- | --- |
| C1 | ✅ 2026-10-30：`POST /api/v1/auth/gateway-token`：请求/响应、claims、TTL、JWKS 与失败码 | §3.1 |
| C2 | ✅ 2026-10-30：策略 `models.*.pricing`：`currency`、`inputPer1M`、`outputPer1M`、`cachedInputPer1M`、`reasoningAsOutput` | §5.2 |
| C3 | ✅ 2026-10-30：策略顶层 `version`（与 `policyVersion` 同值）与 `ETag` 语义 | §5.1 |
| C4 | ✅ 2026-10-30：用量事件 `origin`、`apiKeyId`、`priceVersion` 可空说明；`estimated` 允许 `usage` 缺失 | §6.1 |
| C5 | ✅ 2026-10-30：错误码新增：`PROVIDER_AUTH_FAILED`（502，不可重试）、`UPSTREAM_RATE_LIMITED`（429，可重试）、`CLIENT_CANCELLED`（499，仅日志） | §8.1 |
| C6 | ✅ 2026-10-30：流式错误事件结构：三种协议各自的 `error` 事件样例 | §4.5 |
| C7 | ✅ 2026-10-30：管理 API 的 provider/密钥请求响应体；`admins` 仍用 `X-TT-Admin-Token` | §9.1 |
| C8 | ✅ 2026-10-30：控制面接收端点鉴权方式：`TT_GATEWAY_SERVICE_TOKEN` | §6.2 |
| C9 | ✅ 2026-10-30：能力发现：M1 完成后 `features` 按实际能力置 `true`，并新增 `protocols.<x>.stream/tools/vision` 的实测说明 | §2.1 |

---

## 9. 测试矩阵

| 层 | 工具 | 覆盖 | 是否进 CI |
| --- | --- | --- | --- |
| 单元 | `unittest`/自写 `check()`（沿用仓库习惯，不引入 pytest） | 密钥加密、配额、路由、价格、SSRF、claims | 是 |
| 契约 | `tests/test_contract.py`（uvicorn 线程 + `urllib`） | 三协议面、错误码、管理 API、用量幂等 | 是 |
| 集成（假上游） | `tests/fake_upstreams.py` | 流式、工具、视觉、断流、429/500、取消 | 是 |
| 协议翻译 | `tests/test_protocols.py` | 编码器往返、Anthropic/Gemini 流式与非流式、中途错误、取消、工具多轮、视觉 | 是 |
| 控制面集成 | `tongtian-server/tests/test_gateway.py`（新） | 换票、JWKS、策略 ETag、用量接收、合并视图 | 是 |
| 真实上游 | `tests/live/`（`TT_LIVE=1` 才跑） | 每家 provider 一条最小对话 + embedding | 否（手工） |
| CLI 冒烟 | `scripts/smoke_cli.sh`；CLI 未装时 `scripts/smoke_sdks.py` | Claude Code、Gemini CLI；官方 SDK 替代 | 手工 |
| 压测 | `scripts/loadtest.py` | 50 并发 / 250 请求基线、流式、p95 网关开销；G5 用 `--concurrency 10 --requests 40` 做冒烟 | 冒烟进 G5（手工） |
| 验收编排 | `scripts/acceptance.py` | G3/G4 子进程回归、压测冒烟、生产模式启动、`/docs` 关闭、错误管理令牌 401、日志无密钥、缺主密钥拒绝启动 | 手工（G5） |

必须新增的回归用例：

1. `alg=none`、HS256 签名冒充 RS256、`kid` 不存在。
2. `X-TT-Department` 不在 `depts` 内。
3. 请求体 33MB → `413`；写请求用 `Transfer-Encoding: chunked` → `413`（无法预判体积）。
4. 上游 401 → `PROVIDER_AUTH_FAILED`，不回显上游密钥。
5. 用量缺失 → `estimated=true` 且 `totalTokens>0`。
6. 客户端取消后并发计数归零。
7. 控制面不可达时策略 stale 与 JWKS 缓存行为。
8. 策略版本回退（控制面恢复到旧版本）网关能识别并接受。

---

## 10. 风险与应对

| 风险 | 概率/影响 | 应对 | 触发信号 |
| --- | --- | --- | --- |
| LiteLLM 与 Python 3.13 / pydantic 2.14 冲突 | 中/高 | M1.0 先钉版本；失败则退到薄 httpx 适配，只借 tokenizer 与成本表 | G0 不通过 |
| 流式工具调用翻译出错（Anthropic `input_json_delta`、Gemini `functionCall`） | 高/中 | 先做「内部事件 → 三编码器」往返单测，再接真实 CLI | CLI 冒烟挂起或 JSON 断裂 |
| 各上游 usage 口径不同（缓存 token、reasoning token 是否计入） | 高/中 | 统一以 `total_tokens = prompt + completion`，缓存/推理单列；与上游对账样例入测试 | 报表与账单差异 > 1% |
| 双令牌（HS256 + RS256）复杂度 | 中/中 | 换票接口独立、TTL 短、失败即回退；不碰引擎现有验签 | 401 集中在换票后 |
| SQLite 写竞争（用量 + 配额） | 中/中 | 单 worker、`BEGIN IMMEDIATE`、批量提交；压测观察 `busy` | 压测 p95 抖动、`database is locked` |
| 用量重复计数 | 中/高 | 网关唯一账本 + 引擎 `origin=gateway` + 幂等键 + 对账脚本 | 合并视图总量 > 网关总量 |
| 国产 provider 兼容差异（百炼/方舟/混元的 OpenAI 兼容层） | 高/中 | 先只声明测过的能力；每家一条 live 用例；不支持的字段显式 400 | 返回体缺 `usage` 或字段名不同 |
| 主密钥丢失导致密钥不可解 | 低/高 | 启动检查 + 部署文档强制备份；密文带版本，支持双密钥轮换 | 解密失败日志 |
| 国内镜像滞后导致装不上依赖 | 高/中 | 构建脚本对 `aiohttp`、`mcp` 显式走官方源；部署文档要求同步私有镜像 | `pip install` 报 `No matching distribution` |
| 管理台复用导致前端膨胀 | 中/低 | 新页签独立模块，按需加载；先只做只读 + provider/配额 | `app.js` 超 1500 行 |

---

## 11. 明确不做（M1 之外）

- 计费、发票、充值、成本中心。
- SSO/OIDC/SAML/SCIM、细粒度 RBAC（只保留 admin/user/guest）。
- 多副本、Redis、Postgres、跨机房。
- rerank、audio、realtime、batch API。
- 公开 SaaS 多租户注册与自助开通。
- 任意 CLI 二进制分发（只做连接器清单）。
- prompt caching 的跨协议保真（上游不支持就透传/忽略，不伪造）。
- 完整模型目录可视化编辑器（M1 允许 JSON 编辑 + 校验）。

---

## 12. 交付物清单

| 交付物 | 位置 |
| --- | --- |
| 网关服务（可生产启动） | `tongtian-gateway/` ✅ |
| 契约 v1 增补与变更记录 | `tongtian-gateway/CONTRACT.md` ✅ |
| 控制面换票/JWKS/策略/用量接口 | `tongtian-server/app/` ✅ |
| 管理台「模型网关」「组织与部门」页签 | `tongtian-server/app/web/` ✅ |
| 引擎网关模式 | `tongtian-tauri/engine/` ✅ |
| 部署文档（含主密钥备份、JWKS 轮换、容量与排障） | `tongtian-gateway/docs/deploy.md` ✅ |
| 安全复查（结论、残余风险） | `tongtian-gateway/docs/security-review.md` ✅ |
| 压测脚本 | `tongtian-gateway/scripts/loadtest.py` ✅ |
| 验收脚本（G5 编排） | `tongtian-gateway/scripts/acceptance.py` ✅ |
| 测试报告 | `tongtian-gateway/docs/m1-acceptance.md` ✅ |

---

## 13. 建议的开工顺序（第一批）

1. T0.1 LiteLLM 探针（决定后续一切）。
2. T0.4 契约增补（先把 C1/C5 定下来）。
3. T1.1/T1.2/T1.3（库、密钥、provider）。
4. T3.1–T3.3（控制面 RSA/JWKS/换票，可与 3 并行）。
5. T1.6 打通 OpenAI 面第一条真实对话。

前四项完成后再评估剩余排期；任何一项超出预估 50%，回到本文重排，不硬推。

---

## 14. M1.5 加固与交付（3–5 天，已完成）

### 14.1 任务与结果

| ID | 结果 | 证据 |
| --- | --- | --- |
| T5.1–T5.2 超时与重试 | 超时拆三层：连接 `TT_GATEWAY_CONNECT_TIMEOUT_SECONDS`（默认 10 秒）、上游整段响应 `TT_GATEWAY_UPSTREAM_TIMEOUT_SECONDS`（120 秒）、流式两帧之间 `TT_GATEWAY_STREAM_IDLE_TIMEOUT_SECONDS`（120 秒，逐帧 `asyncio.wait_for`）；LiteLLM 自带重试关到 `num_retries=0`，重试由网关控制。重试只在**同一 provider 目标内**、只对 `UPSTREAM_RATE_LIMITED` / `PROVIDER_UNAVAILABLE` / `UPSTREAM_TIMEOUT`；有 `Retry-After` 按响应头等待，否则 `0.2s·2^n` 加 0–50ms 抖动、单次上限 5 秒；次数 `TT_GATEWAY_UPSTREAM_RETRIES`（默认 1，0–5）。鉴权失败与其他 4xx 不重试；流式一旦下发首个事件不再重试（跨 provider 的别名回退是另一条路径，不受此限） | `tests/test_hardening.py`：连接/总超时拆分、`Retry-After` 解析、退避上限、限流重试成功、次数为 0 直接失败、鉴权失败不重试、流式建连失败可重试、空闲超时触发且已下发内容保留、网关层重试后只记一条用量 |
| T5.3 SSRF/DNS 复查 | 建立连接前 `parse_base_url` + `_ip_blocked`（IPv4-mapped 归一化后再判、metadata 主机名、link-local 永远拒绝、私网需 `allowPrivate`）；每次请求前 `verify_target_url` 重新解析，防 DNS 重绑定；解析失败映射 `PROVIDER_UNAVAILABLE` 且不回显地址 | `tests/test_hardening.py`「非 http(s) 协议被拒」「私网/回环/metadata 被拒」「请求期复检把私网映射成 PROVIDER_UNAVAILABLE」；残余风险见 `docs/security-review.md` |
| T5.4 安全复查 | 新增 `docs/security-review.md`（12 项结论）。本轮修掉一个真实问题：写请求用 `Transfer-Encoding: chunked` 可绕过 32MB 上限 → 现在直接 `413 PAYLOAD_TOO_LARGE` 并加 `Connection: close`，补了 raw socket 用例；删除从未接线的 `TT_GATEWAY_NO_LOG`（M1 从不记录内容，留着只会误导） | `tests/test_core.py`「分块请求体直接 413」；`docs/security-review.md` 含 9 条残余风险（重定向、DNS 重绑定、stale 策略、管理面无限流、`X-TT-Admin-Actor` 仅审计、128MB 临时读取、单副本 SQLite、无审计外送、无 mTLS；其中管理面无限流与无 mTLS 已被 M2 处理） |
| T5.5 压测 | `scripts/loadtest.py` 默认 50 并发 / 250 请求。首轮发现 p95 超标，根因是 LiteLLM 冷启动惰性导入（OpenAI 资源树、流式模块、离线分词器、proxy 类型、files 端点，约 0.6–1 秒），50 并发下被放大。修复：`warm_litellm()` 在 lifespan 内用指向 `127.0.0.1:9` 的合成请求（无密钥、不出网）把非流式与流式两条路径各走一遍，启动日志 `LiteLLM 预热完成：<n>ms`（本机约 1650ms） | 修复后零错误：非流式 50/250 p95≈325ms（净开销≈305ms）、流式 50/50 p95≈719ms；G5 冒烟 10/40 非流式 p95=75ms、流式 p95=234ms。假上游固定 20ms 延迟，数字只用于回归 |
| T5.6 部署文档 | `docs/deploy.md`：拓扑、主机准备（内网 DNS、TLS、时钟同步）、生产环境变量全表、主密钥生成/备份/轮换、JWKS 轮换步骤、nginx TLS 示例（`proxy_buffering off`、流式超时）、管理面 CIDR 限制、Compose 与 systemd 启动、SQLite `.backup` 与恢复、监控信号、升级回滚、容量基线、排障表、验收命令 | 文档可直接按节执行；`startup.py` 与文档中的必填项一致 |
| T5.7 验收与 G5 | `scripts/acceptance.py` 串联 12 步：G3 子进程回归（25 项）、G4（51 项）、压测冒烟 10 并发 / 40 请求（9 项）、生产模式启动（rs256 + JWKS + master key）、`/docs` `/redoc` `/openapi.json` 均 404、经管理代理注册组织/provider 与密钥、RS256 票完成一次真实对话、错误管理令牌 401、日志无管理令牌/服务令牌/主密钥/provider 密钥、缺 `TT_GATEWAY_MASTER_KEY` 时退出码 3 且点名变量 | **G5 通过 12/12**，报告 `docs/m1-acceptance.md`：G3 40.6s、G4 22.4s、冒烟 5.9s、生产模式全绿 |
| T5.8 文档回填 | 本计划（状态、里程碑、§9 矩阵、§12 清单、本节）、`README.md`（里程碑、目录、配置项、测试与部署）、`.env.example`（新增超时/重试，移除 `NO_LOG`）、`CONTRACT.md §4.4`（分块 413、超时口径、重试边界） | 见上述文件 |

### 14.2 本轮改动（代码）

- `app/adapters/litellm_adapter.py`：新增 `warm_litellm()`；超时与有限重试（`_retry_after` / `_retry_delay`，`RETRY_BASE_SECONDS=0.2`、`RETRY_MAX_SECONDS=5`）；流式空闲超时。
- `app/main.py`：lifespan 内预热并记录耗时；写请求带 `Transfer-Encoding` 直接 413；其余安全响应头与生产模式文档关闭不变。
- `app/config.py`：新增 `connect_timeout_seconds`、`upstream_retries`（含上下界校验）；移除死配置 `no_log`。
- `tests/test_hardening.py`（新）：超时、重试、SSRF/DNS、空闲超时、重试与用量记账的回归。
- `scripts/loadtest.py`、`scripts/acceptance.py`、`docs/deploy.md`、`docs/security-review.md`、`docs/m1-acceptance.md`。

### 14.3 残余与移交

- **单副本限制不变**：SQLite 配额与用量缓冲仍是进程内实现；多副本前先替换这两块（§11 明确不做，M2 处理）。
- **容量只代表回归**：压测接的是固定 20ms 假上游，真实 provider 的排队与限流需上线前用生产密钥重跑 `scripts/loadtest.py` 并按 p95 设定反向代理超时。
- **SSRF 残余**：LiteLLM 内部跟随 3xx 重定向、解析与连接之间的 DNS 重绑定窗口未完全消除；策略是把 provider `baseUrl` 限定在受控内网地址并出网白名单。
- **生产 JWKS**：启动自检允许 `http://127.0.0.1` 便于本机验收，线上必须 https 且限制出网。
- **M2 已交付**：管理面 mTLS 与管理面限流（见 [M2-PLAN.md](M2-PLAN.md)、[docs/m2-acceptance.md](docs/m2-acceptance.md)）。
- **M3 已交付**：`ttg_` API Key 签发/列表/吊销、端点级 scopes、模型白名单与无缓存吊销（见 [M3-PLAN.md](M3-PLAN.md)、[docs/m3-acceptance.md](docs/m3-acceptance.md)）。
- **仍待后续**：审计日志外送、细粒度 RBAC、多副本共享限流/配额、Key 自动续签与 per-key 配额。
