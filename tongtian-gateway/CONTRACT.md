# 通天办公 · 模型网关契约（M0）

- 契约版本：`gateway-contract.v1`
- 状态：`gateway-contract.v1` 已冻结；M1 增补均为加法修订，见 §16 变更记录
- 适用方：桌面客户端、引擎、CLI；控制面 `tongtian-server`；网关 `tongtian-gateway`
- 不在本契约内：账号注册、组织/部门管理、市场、协作、计费、对话内容存储

本文是网关与外部世界的唯一接口约定。网关内部换实现、换 provider、换底座，都不应改变本文的字段和错误码。

---

## 0. M0 范围

M0 只做契约与骨架，不接真实模型：

| 项 | 状态 |
| --- | --- |
| 契约字段、错误码、用量事件、策略文档 | 本文冻结 |
| JSON Schema | `schemas/*.schema.json` 由 `scripts/export_schemas.py` 生成 |
| `/health`、`/gateway/v1/capabilities` | 已实现；`features` 只声明已实测能力 |
| OpenAI / Anthropic / Gemini 三个协议面 | M1.2 起三面均可用：翻译、流式、工具、视觉与 count tokens |
| 三套假上游（OpenAI/Anthropic/Gemini） | 已实现，供契约测试使用 |
| JWT 校验 | M0 仅开发态 HS256；RS256/JWKS 与换票接口在 M1 实现，协议见 §3.1 |
| 真实 provider 调用、配额落库、用量上报 | M1 起，任务拆分见 [M1-PLAN.md](M1-PLAN.md) |

M1 会按 §15 的待定项做**加法**修订（换票接口、价格字段、新增错误码、服务令牌），
字段名与错误码的既有语义不变；每处增补在本文对应小节落地后，同步更新本节状态。

---

## 1. 平面与职责

```text
桌面客户端 / 引擎 / CLI
        │  OpenAI / Anthropic / Gemini 协议
        ▼
  tongtian-gateway（模型数据面）
        │  策略、用量、健康
        ▼
  tongtian-server（控制面）
        │
        ▼
  模型厂商 / 内网模型（vLLM、Ollama、Azure、百炼、方舟、腾讯云…）
```

| 平面 | 拥有 | 不拥有 |
| --- | --- | --- |
| 客户端 | 用户界面、本地模型配置、BYOK 密钥、策略缓存 | 厂商密钥、组织策略 |
| 引擎 | 任务执行、工具调用、检查点 | 账号、组织、市场 |
| 网关 | 厂商密钥、路由、故障转移、限流、实时配额、原始用量 | 用户资料、组织关系、市场、协作 |
| 控制面 | 用户、组织、部门、策略分配、模型目录、聚合用量、管理 UI | 厂商密钥、prompt 内容 |

**硬约束**：网关与控制面不共享数据库，只走本文定义的 HTTP 契约；网关默认不落 prompt 和 response。

---

## 2. 版本与能力发现

### 2.1 能力发现

`GET /gateway/v1/capabilities`

无需鉴权（仅限内网），返回网关支持的协议、端点和能力。控制面与客户端据此决定展示哪些功能。下面是 M1 完成后的样例；M0 实际返回的逐协议能力与顶层 `features` 全为 `false`。

```json
{
  "contractVersion": "gateway-contract.v1",
  "gatewayVersion": "0.1.0",
  "gatewayId": "gw-acme-01",
  "protocols": [
    {
      "name": "openai",
      "endpoints": ["/v1/chat/completions", "/v1/embeddings", "/v1/models"],
      "stream": true,
      "tools": true,
      "vision": true,
      "embeddings": true,
      "testedAt": "2026-11-05"
    },
    {
      "name": "anthropic",
      "endpoints": ["/v1/messages", "/v1/messages/count_tokens"],
      "stream": true,
      "tools": true,
      "vision": true,
      "embeddings": false,
      "testedAt": "2026-11-05"
    },
    {
      "name": "gemini",
      "endpoints": [
        "/v1beta/models/{model}:generateContent",
        "/v1beta/models/{model}:streamGenerateContent",
        "/v1beta/models/{model}:countTokens",
        "/v1beta/models"
      ],
      "stream": true,
      "tools": true,
      "vision": true,
      "embeddings": false,
      "testedAt": "2026-11-05",
      "note": "嵌入未纳入 M1 协议面"
    }
  ],
  "features": {
    "stream": true,
    "tools": true,
    "vision": true,
    "embeddings": true,
    "countTokens": true,
    "promptCaching": false
  },
  "providers": ["azure_openai", "bailian", "volcengine_ark", "tencent_hunyuan", "anthropic", "gemini", "openai_compatible", "vllm", "ollama"],
  "limits": {
    "maxRequestBytes": 33554432,
    "maxOutputTokens": 8192,
    "upstreamTimeoutSeconds": 120,
    "streamIdleTimeoutSeconds": 120
  },
  "policyVersion": "2026-10-30T12:00:00Z#7"
}
```

规则：

- `protocols` / `features` 只声明**已经测过**的能力，不按上游“支持”就对外承诺。
- 顶层 `features` 是所有协议面逐项能力的逻辑并集；每个 `protocols[]` 条目另带 `stream/tools/vision/embeddings` 四个布尔值，表示该协议面自己的实测结果。客户端应按协议面取能力，不要用顶层值推断某一协议支持什么。
- 逐协议能力通过一次端到端测试后置 `true`，并在 `testedAt` 写该次通过日期（ISO 8601）；`note` 可选，最多 200 字，只写限制或未测原因。
- M0 骨架阶段三个协议面只有路由占位，顶层 `features` 与逐协议能力全部为 `false`；每接通并测通一项后置 `true`。
- `policyVersion` 可为 `null`（控制面不可达且无缓存时）。
- 新增字段必须向后兼容；删除或改语义必须提升 `contractVersion`。

### 2.2 版本策略

- 路径带 `v1`，`contractVersion` 用 `gateway-contract.v1`。
- 控制面与网关通过 `capabilities` 协商；不支持的能力不展示，不报错。
- 客户端遇到未知字段应忽略，不因新增字段失败。

---

## 3. 鉴权

### 3.1 桌面 / 引擎：控制面 JWT + 网关短票

网关不接受控制面的日常访问令牌（当前为 HS256、`aud=tongtian-engine`），只接受控制面换发的 RS256 短票（`aud=tongtian-gateway`）。控制面现有令牌与引擎验签流程保持不变，避免打断引擎服务模式。

#### 3.1.1 流转

1. 用户在控制面登录，拿到 access token（HS256，默认 15 分钟）与 refresh token。
2. 客户端需要访问网关时，用 access token 调用换票接口（3.1.2），拿到网关短票（RS256，≤15 分钟）。
3. 客户端把短票放进 `Authorization: Bearer`、`x-api-key` 或 `x-goog-api-key`（按协议面选择，见 §3.2）。
4. 短票过期后重新换票；网关不提供 refresh。access token 也过期时，由持有 refresh token 的一方先走现有 `/api/v1/auth/refresh`。
5. 换票不延长会话、不写新 session，也不改变 access token 的会话状态。

个人版与 BYOK 不走这条链：客户端直连厂商，见 §3.3。

#### 3.1.2 换票接口

`POST {controlPlaneUrl}/api/v1/auth/gateway-token`

请求头：

- `Authorization: Bearer <access token>`（必填）
- `Content-Type: application/json`

请求体可省略（等同 `{}`）：

```json
{ "dept": "dept-sales" }
```

`dept` 可选，必须是字符串。省略时取用户主部门；显式指定时必须是该用户所属部门，否则 403。个人账号（无组织）与未分配部门的账号不得换票。

成功响应 `200`，并带 `Cache-Control: no-store`：

```json
{
  "ok": true,
  "token": "eyJhbGciOiJSUzI1NiIsImtpZCI6Ims...",
  "tokenType": "Bearer",
  "expiresIn": 900,
  "expiresAt": 1761826500,
  "tenant": "acme",
  "dept": "dept-sales"
}
```

- `expiresIn` 是服务端剩余秒数；客户端按「收到响应的本地时刻 + expiresIn」计时，`expiresAt` 仅用于排障。
- 响应不含 refresh token；错误信封沿用控制面 `{"ok": false, "code", "message"}`。

控制面失败码：

| HTTP | code | 场景 |
| --- | --- | --- |
| 400 | `INVALID_REQUEST` | 请求体不是 JSON 对象，或 `dept` 不是字符串 |
| 401 | `AUTH_REQUIRED` | 缺少令牌、签名无效、不是 access 令牌、会话已吊销、账号不存在 |
| 401 | `TOKEN_EXPIRED` | access token 已过期 |
| 403 | `FORBIDDEN` | 指定部门不在成员关系内、未分配部门、个人账号无租户 |
| 429 | `RATE_LIMITED` | 换票频率超限，带 `Retry-After` |
| 503 | `AUTH_UNAVAILABLE` | 控制面签名密钥不可用 |

#### 3.1.3 网关令牌 claims

请求头：`alg=RS256`、`typ=JWT`（可省略）、`kid` 必填且必须存在于 JWKS。不读取 `jku/jwk/x5u/x5c`；令牌总长超过 8 KiB 直接拒绝。

| claim | 类型 | 必填 | 含义 | 网关行为 |
| --- | --- | --- | --- | --- |
| `sub` | string | 是 | 用户 UID | 配额主体之一 |
| `sid` | string | 是 | 控制面会话 ID | 仅审计；M1 不做内省 |
| `tenant` | string | 是 | 组织/租户 | 策略与配额键 |
| `dept` | string | 是 | 当前生效部门 | 部门策略与配额键 |
| `depts` | string[] | 是 | 可切换部门，必须包含 `dept` | `X-TT-Department` 只能取其中值 |
| `roles` | string[] | 是 | 非空，如 `user`/`admin`/`guest` | `guest` 走访客策略；`admin` 不自动获得管理 API 权限 |
| `type` | string | 是 | 固定 `access` | 其他值拒绝 |
| `iss` | string | 是 | `tongtian-auth` | 不等于配置值拒绝 |
| `aud` | string 或 string[] | 是 | 必须包含 `tongtian-gateway` | 不含拒绝 |
| `iat` | number | 是 | 签发时间（秒） | 比网关时间超前 60 秒以上拒绝 |
| `exp` | number | 是 | 过期时间（秒），`≤ iat + 900` | 过期返回 `TOKEN_EXPIRED` |
| `nbf` | number | 否 | 生效时间（秒） | 未到（含 60 秒余量）拒绝 |
| `jti` | string | 否 | 审计 ID | 忽略 |
| `quota` | object | 否 | 软提示 | 忽略；硬配额以网关策略为准 |
| `org` | string | 否 | 旧字段，等同 `tenant` | 仅当 `tenant` 缺失时回退；两者冲突则拒绝 |
| `role` | string | 否 | 旧字段，单角色 | 仅当 `roles` 缺失时回退；两者冲突则拒绝 |

补充规则：

- `depts` 缺失时按 `[dept]` 处理；存在但不含 `dept` 视为令牌非法。
- `exp` 严格校验，不设宽限；`iat/nbf` 允许 60 秒时钟偏差。
- 网关令牌有效期 `exp = min(now + 900, access token 的 exp)`，保证不会比换票凭据活得更久。
- 部门切换：重新换票，或带 `X-TT-Department`（仅当 `depts` 包含该值，否则 403）。

#### 3.1.4 JWKS

- 位置：`{controlPlaneUrl}/.well-known/jwks.json`，公开、无鉴权。
- 网关配置：`TT_GATEWAY_JWKS_URL` 可覆盖；生产环境必须是 `https://`（本机回环调试除外）。
- 响应体：

```json
{
  "keys": [
    {
      "kty": "RSA",
      "kid": "k-7f3a9c21d4e8",
      "use": "sig",
      "alg": "RS256",
      "n": "base64url...",
      "e": "AQAB"
    }
  ]
}
```

- 只接受 `kty=RSA`、`alg` 为 `RS256` 或缺失、`use` 为 `sig` 或缺失的键；响应体上限 64 KiB。
- 网关缓存 10 分钟；遇到未知 `kid` 立即刷新一次（两次刷新间隔不小于 30 秒）。
- 刷新失败但缓存仍可用时继续服务；无可用缓存时返回 `503 AUTH_UNAVAILABLE`。
- 轮换：控制面至少保留旧公钥 24 小时并允许同时发布多把；网关缓存窗口 10 分钟，轮换重叠期不得短于 30 分钟。

#### 3.1.5 网关校验顺序与失败码

1. 从 `Authorization: Bearer`、`x-api-key`、`x-goog-api-key` 取令牌；取不到 → `AUTH_REQUIRED`。
2. `ttg_` 开头走网关 API Key 路径（§3.2）。
3. 解析 header：必须 `alg=RS256`、有 `kid`；`none`、`HS256`、其他算法一律拒绝 → `AUTH_REQUIRED`。
4. 按 `kid` 取公钥；JWKS 不可达且无缓存 → `AUTH_UNAVAILABLE`。
5. 验签 → 失败 `AUTH_REQUIRED`。
6. 校验 claims：`iss/aud/type` → `AUTH_REQUIRED`；`exp` → `TOKEN_EXPIRED`；`tenant/dept/roles` 缺失或冲突 → `AUTH_REQUIRED`。
7. `X-TT-Department` 越权 → `FORBIDDEN`。

401 响应带 `WWW-Authenticate: Bearer`；错误消息不回显令牌内容或具体 claim 值。

#### 3.1.6 客户端刷新策略

- 缓存换票结果到 `expiresIn - 120` 秒，提前 2 分钟重换。
- 收到 `TOKEN_EXPIRED`：先确认 access token 是否有效；有效则重新换票，同一请求最多重试一次。
- 收到 `AUTH_UNAVAILABLE`：指数退避重试，不强制重新登录。
- 收到 `FORBIDDEN`：不重试。
- 并发请求共享同一次换票（single-flight），避免过期瞬间打爆控制面。
- 短票只存内存或系统钥匙串，不写日志、不写配置文件。

#### 3.1.7 开发态 HS256

- `TT_GATEWAY_JWT_MODE=hs256-dev`，密钥 `TT_GATEWAY_DEV_HS256_SECRET`（与控制面开发密钥一致）。
- 仅在 `TT_GATEWAY_ENV=development` 生效，启动时打印警告；生产启动直接拒绝。
- 开发态令牌需自行带 `aud=tongtian-gateway`；控制面日常 access token（`aud=tongtian-engine`）不能直接当网关令牌用，联调时走换票接口或测试脚本签发。

### 3.2 CLI：网关 API Key

Claude Code、Gemini CLI 等工具不擅长做 OAuth 刷新，使用网关 API Key：

- 格式：`ttg_<keyId>_<secret>`；`keyId` 12 位，`secret` 32 字节 base64url。
- 请求头：
  - `Authorization: Bearer ttg_...`（通用）
  - `x-api-key: ttg_...`（Anthropic 客户端）
  - `x-goog-api-key: ttg_...`（Gemini 客户端）
- 存储：只存 `keyId`、`sha256(secret)`、`scopes`、`modelAllowlist`、`rateLimitRpm`、`rateLimitTpm`、`quotaMonthlyTokens`、`expiresAt`、`rotatedAt`、`revokedAt`；明文只在创建与轮换时返回一次。
- scopes：`chat`、`embeddings`、`models`、`messages`、`gemini`，按端点校验（见 §9.2）；可选模型白名单，匹配请求里的 `model`（别名或具体模型名）。
- 绑定：`sub`、`tenant`、`dept`；角色固定为 `member`；`X-TT-Department` 只能等于绑定部门。用量与配额按绑定主体计算，用量事件带 `apiKeyId`。
- per-key 限额（M4）：`rateLimitRpm` 按 Key 令牌桶限流，`quotaMonthlyTokens` 与部门月度配额同时生效；两者为 `0` 表示不限。
- per-key TPM（M6）：`rateLimitTpm` 按 Key 分钟级 token 桶限流（见 §7.6），与主体（部门）TPM 桶同时生效；`0` 表示不限。
- 轮换（M4）：同一 `keyId` 更新 secret，旧 secret 立即失效，无重叠期；`rotatedAt` 记录最近轮换时间。
- 吊销与过期：网关不缓存 Key，每次请求查本地 SQLite，吊销与过期从下一次请求起立即生效（优于 30 秒上限）。
- 校验失败统一返回 `AUTH_REQUIRED`，不区分“不存在 / 已吊销 / 已过期”，避免把 Key 状态当成枚举预言机。

### 3.3 BYOK

- BYOK 不经过网关：客户端直接调厂商，密钥只存本机钥匙串。
- 网关**拒绝**任何请求体或请求头里携带的厂商密钥。
- BYOK 用量由客户端上报控制面，属软配额；硬配额只对网关模型生效。

---

## 4. 协议面

三个协议面共用同一套路由、配额、用量和错误码；差异只在请求/响应/流式事件的翻译层。

### 4.1 OpenAI 兼容

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/chat/completions` | 对话，支持流式、tools、vision、JSON mode |
| POST | `/v1/embeddings` | 向量 |
| GET | `/v1/models` | 当前主体可用模型与别名 |

请求头：

| 头 | 必填 | 说明 |
| --- | --- | --- |
| `Authorization` | 是 | `Bearer <JWT 或 ttg_ Key>` |
| `X-TT-Request-Id` | 否 | 客户端生成；缺省网关生成 |
| `X-TT-Department` | 否 | 切换部门，需在 `depts` 内 |
| `X-TT-Project` | 否 | 项目 id，仅用于用量归属 |

响应头（所有协议面通用）：

| 头 | 说明 |
| --- | --- |
| `X-TT-Request-Id` | 本次请求 id |
| `X-TT-Model` | 实际调用的模型 id |
| `X-TT-Model-Alias` | 客户端请求的别名（如 `auto`） |
| `X-TT-Policy-Version` | 本次使用的策略版本 |
| `X-TT-Quota-Remaining` | 当月剩余 token（未知时不返回） |
| `X-TT-Quota-Reset` | 配额重置时间（ISO 8601 UTC） |
| `X-TT-Policy-Stale` | `1` 表示控制面不可达，使用最后可用策略 |

流式：

- `Content-Type: text/event-stream`
- 每个事件 `data: <json>\n\n`，结束 `data: [DONE]\n\n`
- 网关透传上游增量，但 usage 以网关结算为准。

`GET /v1/models` 返回 OpenAI 列表结构，`data[]` 只含当前主体可用项：

```json
{
  "object": "list",
  "data": [
    {
      "id": "auto",
      "object": "model",
      "owned_by": "tongtian",
      "tt": {
        "alias": true,
        "chain": ["azure-gpt-4o", "bailian-qwen-max"],
        "capabilities": ["chat", "tools", "vision"]
      }
    }
  ]
}
```

### 4.2 Anthropic Messages（Claude Code）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/messages` | 对话，支持流式、tools、vision |
| POST | `/v1/messages/count_tokens` | 估算输入 token |

请求头：

| 头 | 必填 | 说明 |
| --- | --- | --- |
| `x-api-key` 或 `Authorization: Bearer` | 是 | JWT 或 `ttg_` Key |
| `anthropic-version` | 是 | 透传/校验，接受 `2023-06-01` 等；缺失返回 400 |
| `anthropic-beta` | 否 | 透传或按能力丢弃 |

请求字段映射：

| Anthropic | 处理 |
| --- | --- |
| `system` | 顶层 system，转成内部 system message |
| `max_tokens` | 必填；不得超过策略 `maxOutputTokens` |
| `messages[].content` | 文本、图片、`tool_use`、`tool_result` 转内部格式 |
| `tools` / `tool_choice` | 转 OpenAI tools；回程再转 `tool_use` |
| `stop_sequences` | 映射到内部 stop |
| `thinking` | 上游支持则映射；不支持则丢弃并记录 |
| `metadata` | 透传可识别字段，其余丢弃 |
| `cache_control` | 上游支持则保留；不支持则丢弃，不报错 |

流式事件（网关必须按顺序输出）：

```text
event: message_start
data: {"type":"message_start","message":{"id":"msg_...","type":"message","role":"assistant","model":"...","content":[],"stop_reason":null,"usage":{"input_tokens":12,"output_tokens":0}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好"}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":8}}

event: message_stop
data: {"type":"message_stop"}
```

工具调用时，`content_block_start` 的 `content_block.type` 为 `tool_use`，参数通过 `input_json_delta` 增量输出。结束原因是 `tool_use` 时，客户端会回传 `tool_result`。

`count_tokens` 响应：

```json
{"input_tokens": 128}
```

若上游没有原生 count 接口，用 tokenizer 估算，并在响应头加 `X-TT-Estimated: 1`。

图片输入：`base64` 与 `url` 两种 `source` 都支持；单张图片解码后不得超过 20MB，超出返回 `413 PAYLOAD_TOO_LARGE`。

错误信封：

```json
{"type":"error","error":{"type":"invalid_request_error","message":"..."}}
```

### 4.3 Gemini（Gemini CLI）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1beta/models/{model}:generateContent` | 非流式 |
| POST | `/v1beta/models/{model}:streamGenerateContent?alt=sse` | 流式 |
| POST | `/v1beta/models/{model}:countTokens` | 估算 token |
| GET | `/v1beta/models` | 模型列表 |

请求头：

| 头 | 必填 | 说明 |
| --- | --- | --- |
| `x-goog-api-key` 或 `Authorization: Bearer` | 是 | JWT 或 `ttg_` Key |
| `Content-Type` | 是 | `application/json` |

请求字段映射：

| Gemini | 处理 |
| --- | --- |
| `contents[]` | `role=user/model` 转内部消息 |
| `systemInstruction` | 转 system message |
| `tools[].functionDeclarations` | 转 OpenAI tools |
| `toolConfig` | 映射 tool_choice |
| `generationConfig` | temperature/topP/topK/maxOutputTokens/stopSequences |
| `safetySettings` | 上游支持则透传，否则丢弃 |

响应：

```json
{
  "candidates": [
    {
      "content": {"role": "model", "parts": [{"text": "你好"}]},
      "finishReason": "STOP"
    }
  ],
  "usageMetadata": {
    "promptTokenCount": 12,
    "candidatesTokenCount": 8,
    "totalTokenCount": 20
  }
}
```

流式：SSE，每个 `data:` 是一段上述 JSON 的增量；函数调用在 `parts[].functionCall`。

`contents[].parts[]` 映射：

| part | 处理 |
| --- | --- |
| `text` | 文本；历史回合里 `thought: true` 的 part 丢弃 |
| `inlineData` | base64 图片，转内部图片 part |
| `fileData` | 图片 URL，仅接受 `image/*` |
| `functionCall` | 工具调用；无 id，网关按顺序生成 `call_<n>` |
| `functionResponse` | 工具结果，按 `name` 配回对应 `call_<n>` |

单张图片解码后不得超过 20MB，超出返回 `413 PAYLOAD_TOO_LARGE`。

`GET /v1beta/models` 只列当前主体可用的对话模型与别名：

```json
{
  "models": [
    {
      "name": "models/auto",
      "displayName": "auto",
      "supportedGenerationMethods": ["generateContent", "streamGenerateContent", "countTokens"],
      "inputTokenLimit": 128000,
      "outputTokenLimit": 16384
    }
  ]
}
```

错误信封：

```json
{"error":{"code":400,"message":"...","status":"INVALID_ARGUMENT"}}
```

### 4.4 协议面共同规则

- 请求体上限：默认 32MB（图片 base64 占大头），超限 413。写请求（`POST`/`PUT`/`PATCH`）必须带 `Content-Length`；带 `Transfer-Encoding: chunked` 的写请求无法预判体积，直接返回 `413 PAYLOAD_TOO_LARGE` 并关闭连接。
- 输出 token 上限：策略 `maxOutputTokens`，请求超过时按策略截断或 400；默认拒绝（400），不静默截断。
- 超时：连接 10 秒、上游整段响应 120 秒、流式两帧之间的空闲 120 秒（均可用 `TT_GATEWAY_CONNECT_TIMEOUT_SECONDS` / `TT_GATEWAY_UPSTREAM_TIMEOUT_SECONDS` / `TT_GATEWAY_STREAM_IDLE_TIMEOUT_SECONDS` 调整）。超时按 `UPSTREAM_TIMEOUT`（504）处理；流式已下发的内容保留，按估算结算。
- 重试：网关只在**同一 provider 目标内**重试限流（`UPSTREAM_RATE_LIMITED`）、上游不可用（`PROVIDER_UNAVAILABLE`）与上游超时（`UPSTREAM_TIMEOUT`）；默认重试 1 次，`TT_GATEWAY_UPSTREAM_RETRIES` 可设 0–5（0 关闭）。上游给 `Retry-After` 时按其等待，否则按 `0.2s × 2^n` 指数退避并加 0–50ms 抖动，单次等待不超过 5 秒。鉴权失败、其他 4xx、以及已向客户端下发首个流式事件之后的失败不重试；跨 provider 的别名回退是独立的策略行为，不占重试次数。
- 客户端断开：取消上游请求，按已知或估算 token 结算，不记为零。
- 错误消息不得回显 prompt 内容。
- 所有响应带 `X-TT-Request-Id`；上游 request id 仅进日志元数据。

### 4.5 流式错误事件

- 开始前失败（尚未返回 200）：按 §8 的普通错误信封回包。
- 开始后失败（已返回 200 和至少一个正常事件）：按协议发**一帧错误事件**后关闭连接；不再发正常结束帧（OpenAI 不发 `[DONE]`，Anthropic 不发 `message_stop`，Gemini 不发带 `finishReason` 的终帧）。
- 已产生 usage 的仍写用量事件，`status=error`、`errorCode` 为统一码；usage 来源不明时 `estimated=true`。
- 客户端主动断开不写错误事件，只记 `CLIENT_CANCELLED`，并按已知或估算 usage 结算。

OpenAI（SSE）：

```text
data: {"error":{"message":"上游模型服务不可用","type":"api_error","param":null,"code":"PROVIDER_UNAVAILABLE"}}

```

Anthropic（SSE，带 `event` 名）：

```text
event: error
data: {"type":"error","error":{"type":"overloaded_error","message":"上游模型服务不可用","tt_code":"PROVIDER_UNAVAILABLE"}}

```

Gemini（SSE）：

```text
data: {"error":{"code":503,"message":"上游模型服务不可用","status":"UNAVAILABLE","details":[{"@type":"type.googleapis.com/tongtian.GatewayError","code":"PROVIDER_UNAVAILABLE"}]}}

```

---

## 5. 策略契约

### 5.1 拉取

`GET {controlPlaneUrl}/api/v1/gateway/policy?tenant={tenant}`

- 鉴权：网关服务令牌（`Authorization: Bearer <gateway service token>`）或 mTLS；仅内网。
- 响应：`PolicyDocument`（见下），`Cache-Control: no-store`。
- `policyVersion` 是策略变更的唯一版本号；每次内容变化必须生成新值，不能复用旧值。
- `ETag` 值为带双引号的 `policyVersion`，例如 `ETag: "2026-10-30T12:00:00Z#7"`；`If-None-Match` 使用相同格式。
- 未变化返回 `304`（无响应体）；变化返回 `200` + 完整文档；缺失或格式非法的 `If-None-Match` 按未命中处理。
- 网关缓存 TTL 默认 300 秒；到期后带 `If-None-Match` 条件请求；超过 stale grace（默认 24 小时）仍取不到则返回 `503 POLICY_STALE`。
- 控制面不可达但缓存未过期：继续服务，响应头 `X-TT-Policy-Stale: 1`。
- 控制面明确返回 `404`（该租户未配置策略）：永久拒绝，返回 `403 FORBIDDEN`，不使用旧缓存。

### 5.2 文档结构

```json
{
  "contractVersion": "gateway-contract.v1",
  "policyVersion": "2026-10-30T12:00:00Z#7",
  "version": "2026-10-30T12:00:00Z#7",
  "tenant": "acme",
  "gateway": {"url": "http://127.0.0.1:8001"},
  "noLog": true,
  "embeddingRoute": "embedding-default",
  "byok": {"allowed": true, "hardQuota": false},
  "models": {
    "azure-gpt-4o": {
      "provider": "azure_openai",
      "upstreamModel": "gpt-4o",
      "capabilities": ["chat", "tools", "vision"],
      "contextWindow": 128000,
      "maxOutputTokens": 16384,
      "priceVersion": "2026-10-01",
      "pricing": {
        "currency": "USD",
        "inputPer1M": "2.50",
        "outputPer1M": "10.00",
        "cachedInputPer1M": "1.25",
        "reasoningAsOutput": true
      }
    },
    "bailian-qwen-max": {
      "provider": "bailian",
      "upstreamModel": "qwen-max",
      "capabilities": ["chat", "tools"],
      "contextWindow": 32768,
      "maxOutputTokens": 8192,
      "priceVersion": "2026-10-01",
      "pricing": {
        "currency": "USD",
        "inputPer1M": "0.40",
        "outputPer1M": "1.20",
        "reasoningAsOutput": true
      }
    }
  },
  "routes": {
    "auto": {"chain": ["azure-gpt-4o", "bailian-qwen-max"], "fallbackAllowed": true},
    "advanced": {"chain": ["azure-gpt-4o"], "fallbackAllowed": false},
    "basic": {"chain": ["bailian-qwen-max"], "fallbackAllowed": true},
    "economy": {"chain": ["bailian-qwen-max"], "fallbackAllowed": false},
    "embedding-default": {"chain": ["azure-text-embedding-3-small"], "fallbackAllowed": false}
  },
  "allowedModels": ["auto", "advanced", "basic", "economy", "embedding-default", "azure-gpt-4o"],
  "quotaDefaults": {
    "monthlyTokens": 2000000,
    "concurrency": 4,
    "rpm": 60,
    "tpm": 60000,
    "maxOutputTokens": 8192
  },
  "departments": {
    "dept-sales": {
      "allowedModels": ["auto", "basic", "embedding-default"],
      "quota": {"monthlyTokens": 500000, "concurrency": 2},
      "byokAllowed": true
    }
  },
  "issuedAt": "2026-10-30T12:00:00Z",
  "expiresAt": "2026-10-30T12:05:00Z"
}
```

策略字段规则：

- `version` 必填时与 `policyVersion` 同值；同时出现且不相等视为策略非法。只给 `policyVersion` 也合法（旧控制面）。
- `priceVersion` 标识定价表版本；定价变化时必须换新值，便于按版本对账。
- `pricing` 可省略（本地/免费模型）：事件 `cost="0"`、`priceVersion=null`，请求照常放行。
- 金额是十进制定点字符串，单位为主币种每 100 万 token；不接受负数、科学计数法或空串。
- `cachedInputPer1M` 省略表示无缓存折扣，缓存 token 按 `inputPer1M` 计。
- `reasoningAsOutput=true`（默认）表示 reasoning token 按 `outputPer1M` 计费；`false` 表示 reasoning 不计费。
- 同一策略内不同模型可使用不同 `currency`；一次请求的成本只按实际使用模型的价格计算，回退后按最终模型重算。

### 5.3 解析规则

1. 客户端请求的 `model` 是别名（在 `routes` 中）：取 `chain`，按顺序过滤掉不在 `allowedModels` 或部门白名单里的模型。
2. 请求的是具体模型（在 `models` 中）：必须在 `allowedModels` 与部门白名单内，否则 `403 MODEL_NOT_ALLOWED`。
3. 别名链为空：`403 MODEL_NOT_ALLOWED`。
4. 故障转移只在 `fallbackAllowed=true` 且错误可重试时发生；记录实际使用的模型。
5. 嵌入请求走 `embeddingRoute`，不允许复用聊天模型；Anthropic 没有 embeddings，必须配独立嵌入 provider。
6. 部门覆盖优先于租户默认；部门未配置的字段回落到 `quotaDefaults`。

---

## 6. 用量事件

### 6.1 事件结构

每个成功或失败的请求产生一条事件。网关本地缓冲，批量推控制面。

```json
{
  "contractVersion": "gateway-contract.v1",
  "gatewayId": "gw-acme-01",
  "requestId": "req_01J...",
  "tenantId": "acme",
  "departmentId": "dept-sales",
  "userId": "u-123",
  "apiKeyId": null,
  "projectId": null,
  "origin": "gateway",
  "protocol": "openai",
  "endpoint": "/v1/chat/completions",
  "modelAlias": "auto",
  "model": "azure-gpt-4o",
  "provider": "azure_openai",
  "promptTokens": 1200,
  "completionTokens": 300,
  "cachedTokens": 0,
  "reasoningTokens": 0,
  "totalTokens": 1500,
  "cost": "0.012300",
  "currency": "USD",
  "priceVersion": "2026-10-01",
  "latencyMs": 2345,
  "status": "ok",
  "errorCode": null,
  "stream": true,
  "estimated": false,
  "startedAt": 1761825600000,
  "finishedAt": 1761825602345
}
```

字段规则：

- `requestId` 由网关生成或接受客户端 `X-TT-Request-Id`；同一请求重试复用同一 id。
- `origin`：`gateway` 表示经网关的模型调用；`engine` / `desktop` 表示客户端直连或 BYOK 的上报。网关发出的事件固定为 `gateway`。
- `apiKeyId`：CLI 网关 API Key 的 id；JWT 请求为 `null`。
- `priceVersion`：实际使用模型的 `priceVersion`；模型未配置定价时为 `null`。
- `totalTokens = promptTokens + completionTokens`；由网关重算，不信任上游总数字段。
- `cachedTokens` 是 `promptTokens` 的子集；`reasoningTokens` 是 `completionTokens` 的子集。网关须保证不越界，越界按上限截断。
- `cachedTokens` / `reasoningTokens` 计入成本，但不重复计入 `totalTokens`。
- `estimated=true` 表示上游未返回 usage，网关用 tokenizer 估算；此时 `promptTokens` / `completionTokens` 可为估算值，实在无法估算时允许缺省（按 0 处理）。禁止把未知 usage 伪造成确定值。
- `cost` 是字符串十进制，保留 6 位小数（四舍五入）；`currency` 默认 `USD`，本地模型可为 `CNY`。
- 成本计算：
  ```text
  billableInput  = promptTokens - cachedTokens
  billableOutput = completionTokens - (reasoningAsOutput ? 0 : reasoningTokens)
  cost = billableInput  * inputPer1M  / 1e6
       + cachedTokens   * (cachedInputPer1M 或 inputPer1M) / 1e6
       + billableOutput * outputPer1M / 1e6
  ```
  未配置 `pricing` 时 `cost="0"`，不阻断请求。
- `status`：`ok` / `error` / `cancelled`；失败请求的 `errorCode` 使用 §8.1 统一码。
- `startedAt` / `finishedAt` 为 epoch 毫秒。
- 不包含 prompt、response、图片、工具参数正文。

### 6.2 批量上报

`POST {controlPlaneUrl}/api/v1/gateway/usage`

```json
{
  "contractVersion": "gateway-contract.v1",
  "gatewayId": "gw-acme-01",
  "batchId": "batch_01J...",
  "events": [ ... ]
}
```

响应：

```json
{"ok": true, "accepted": 120, "duplicates": 3}
```

规则：

- 鉴权：`Authorization: Bearer <TT_GATEWAY_SERVICE_TOKEN>`。控制面用常量时间比较；未配置该令牌时返回 `503 AUTH_UNAVAILABLE`，令牌错误或缺失返回 `401 AUTH_REQUIRED`。
- 服务令牌只用于网关 → 控制面的服务调用（用量上报、策略拉取），不接受用户 access token，也不下发给桌面/CLI。
- 幂等键：`(gatewayId, requestId)`；重复上报不重复计数。
- 批量默认 200 条或 5 秒刷一次；失败退避重试，队列保留 30 天。
- 控制面需要新增 `source=gateway`、`origin=gateway` 的用量来源；网关模型调用以网关账为准，客户端对同一调用的 `engine/desktop` 上报不得重复计入。
- 控制面不可达时网关继续本地缓冲；恢复后按 `requestId` 补报。

---

## 7. 配额

### 7.1 限额

| 限额 | 作用域 | 说明 |
| --- | --- | --- |
| `monthlyTokens` | 用户/部门/Key | 当月 prompt+completion 总量；Key 级见 §7.4 |
| `concurrency` | 用户 | 同时在跑的请求数 |
| `rpm` | Key | 每分钟请求数；M4 起按 Key 的 `rateLimitRpm` 执行，JWT 用户暂无 |
| `tpm` | 用户/Key | 每分钟 token 数；M6 起主体用策略 `tpm`、Key 用 `rateLimitTpm`，见 §7.6 |
| `maxOutputTokens` | 模型/部门 | 单次请求输出上限 |

部门覆盖租户默认；用户级覆盖由控制面下发（M1 可加）。

### 7.2 预留与结算

1. 请求进入：估算输入 token（或按上游 tokenizer），加上 `min(request.max_tokens, policy.maxOutputTokens)` 作为预留。
2. 原子预留：`remaining >= reserve` 才放行；否则 `429 QUOTA_TOKENS_EXCEEDED`。
3. 并发计数 +1；超过 `concurrency` 返回 `429 QUOTA_CONCURRENCY_EXCEEDED`。
4. 请求结束：按实际 usage 结算，释放未用预留；并发计数 -1。
5. 客户端断开或上游超时：按已知 usage 或估算值结算，标记 `estimated=true`，不记为零。
6. 月度边界：UTC 自然月；响应头 `X-TT-Quota-Reset` 给出下次重置时间。

### 7.3 存储（M5：同机多 worker + 共享状态）

- **同机多 worker，共享一份 SQLite**：`BEGIN IMMEDIATE` + `busy_timeout` 保证写串行；
  已结算用量、Key 元数据、策略缓存、审计链仍只在 SQLite，多 worker 看到同一份事实。
- **Redis 承载热状态**（`TT_GATEWAY_REDIS_URL`）：管理面/Key 令牌桶、在途预留与并发计数、
  策略失效通知。Redis 不是事实来源，丢失后可由 SQLite 已结算用量重建（在途预留按超时保守回收）。
- `TT_GATEWAY_WORKERS>1` 时 Redis 必填；未配 Redis 直接拒绝启动。`WORKERS=1` 且未配 Redis
  时行为与 M1–M4 完全一致（进程内实现，零额外依赖）。
- 同机多 worker 共享 `gatewayId`；跨主机多副本在 M6 通过 `TT_GATEWAY_DB_URL` 指向 Postgres 实现（§7.5）。
- 同一实例的多个 worker 共享一份数据库；跨主机副本也共享同一份，`gatewayId` 仍按部署实例配置。
- 计数器表与用量事件表分开；用量事件是事实来源，计数器可重建。
- Redis 不可用（连接/超时/脚本错误）时**失败关闭**：`503 SHARED_STATE_UNAVAILABLE`，
  不退化为进程内计数（否则会突破全局配额）。
- BYOK 直连不经过网关，网关不做硬配额；客户端软告警，控制面按上报统计。

### 7.4 per-key 限额（M4）

- `rateLimitRpm`（0–10000，0 不限）：每个 Key 独立令牌桶，桶容量 `max(1, min(rpm, 60))`；
  超限返回 `429 RATE_LIMITED` 并带 `Retry-After`。
- `quotaMonthlyTokens`（0–10^15，0 不限）：按 UTC 自然月累计 `total_tokens`，与绑定主体的
  部门/用户月度配额**同时**生效，任一不足即 `429 QUOTA_TOKENS_EXCEEDED`。
- 计数表 `api_key_quota_counters(period, key_id, used_tokens)` 与用量事件分开，可由
  `scripts/rebuild_quota.py` 重建；在途预留：单 worker 在进程内，配 Redis 时在 Redis（M5）。
- Key 列表响应附带当前月 `usedTokens`；限额在创建后不可修改，需要调整时轮换或吊销重签。
- `rateLimitTpm`（0–10^9，0 不限，M6）：每个 Key 独立的每分钟 token 桶，与绑定主体的策略
  `tpm` **同时**生效；超限返回 `429 RATE_LIMITED` 并带 `Retry-After`，见 §7.6。

### 7.5 跨主机存储（M6）

- `TT_GATEWAY_DB_URL` 为空时使用 SQLite（`{TT_GATEWAY_DATA_DIR}/gateway.db`），行为与 M5 相同。
- 配置 `postgresql://` / `postgres://` 时改用 Postgres；**进程在启动阶段就连接并迁移 schema**
  （M8.1 的账本门槛：SQLite 打不开、Postgres 连不上、缺 psycopg、迁移失败或库里版本过高，
  一律拒绝启动，日志点名后端与版本，不放“进程健康但账本 500”的状态出去）。
  连接串可能含密码，**只从环境变量读取，不写日志、不在管理 API 回显**；启动错误消息也会
  先抹掉 `://user:pass@`、`password=` 与配置里的 DSN 原文再落日志。
- 两种后端共用同一套表名、字段与计数器语义；差异只在下述并发原语：
  - 迁移与审计链追加在 Postgres 使用事务级 advisory lock，SQLite 使用 `BEGIN IMMEDIATE`；
  - outbox 认领在 Postgres 使用 `FOR UPDATE SKIP LOCKED`，SQLite 使用现有租约；
  - 用量按日/月聚合的日期表达式由数据库方言提供，对外字段不变。
- 跨主机副本必须同时配置共享 Redis；只配 Postgres 不配 Redis 时，在途预留与并发计数无法跨主机，
  启动阶段按 `TT_GATEWAY_WORKERS>1` 或显式跨主机模式拒绝启动。
- 迁移版本高于程序支持版本时拒绝启动；两个副本同时启动只应用一次迁移。
- 审计链仍是全局单链：跨主机追加由 advisory lock 串行化，`audit/verify` 语义不变。

### 7.6 TPM 限流（M6）

- 成本：请求进入时按 `输入估算 + 本次输出上限` 从每分钟 token 桶扣除；输出上限取
  `min(request.max_tokens, 模型/部门 maxOutputTokens)`。
- 桶：主体桶（策略 `tpm`）与 Key 桶（`rateLimitTpm`）独立；任一不足即拒绝，两者同时计数。
- 容量：`max(1, min(tpm, 60000))`；单请求成本大于容量时直接 `429 RATE_LIMITED`。
- 补充速率：`tpm / 60` token/秒；`Retry-After` 为满足本次成本所需秒数（向上取整）。
- 结算：按实际 `total_tokens` 结算后，把 `预留 - 实际` 退回桶（不超过容量）。
- 共享状态：单 worker 无 Redis 时进程内；多 worker/跨主机走 Redis。Redis 不可用返回
  `503 SHARED_STATE_UNAVAILABLE`（失败关闭），与配额预留一致。
- 0 或缺失表示不限制；TPM 不替代月度配额与并发限制，三者同时生效。

### 7.7 Redis 高可用（M6）

- 单实例：`TT_GATEWAY_REDIS_URL=redis://` 或 `rediss://`（TLS）。
- Sentinel：配置 `TT_GATEWAY_REDIS_SENTINEL_MASTER` 与
  `TT_GATEWAY_REDIS_SENTINEL_URLS=host:port,...` 后，客户端通过 Sentinel 发现主节点；
  `TT_GATEWAY_REDIS_SENTINEL_PASSWORD`、`TT_GATEWAY_REDIS_PASSWORD`、`TT_GATEWAY_REDIS_DB`
  可选，TLS 由 `TT_GATEWAY_REDIS_TLS=1` 与 `TT_GATEWAY_REDIS_TLS_CA/CERT/KEY` 控制。
- 主从切换期间，客户端按 `TT_GATEWAY_REDIS_TIMEOUT_MS` 失败关闭；恢复后自动重连。
- 健康页只披露 `mode=single|sentinel`、`tls`、`connected` 与 `lastError`，不回显 URL/密码。

---

## 8. 错误码

### 8.1 统一错误码

| 代码 | HTTP | 含义 | 可重试 |
| --- | --- | --- | --- |
| `AUTH_REQUIRED` | 401 | 缺少/无效凭据 | 否 |
| `MTLS_REQUIRED` | 401 | 管理面要求有效的 mTLS 客户端证书（经受信反向代理校验） | 否 |
| `TOKEN_EXPIRED` | 401 | 令牌过期 | 否（刷新后重试） |
| `FORBIDDEN` | 403 | 无权访问 | 否 |
| `MODEL_NOT_ALLOWED` | 403 | 模型不在策略/部门白名单 | 否 |
| `INVALID_REQUEST` | 400 | 请求格式或参数错误 | 否 |
| `PAYLOAD_TOO_LARGE` | 413 | 请求体超限 | 否 |
| `QUOTA_TOKENS_EXCEEDED` | 429 | 月 token 配额不足 | 否（重置后） |
| `QUOTA_CONCURRENCY_EXCEEDED` | 429 | 并发超限 | 是（退避） |
| `RATE_LIMITED` | 429 | RPM/TPM 超限 | 是 |
| `POLICY_STALE` | 503 | 策略过期且控制面不可达 | 是 |
| `AUTH_UNAVAILABLE` | 503 | JWKS 不可达且无缓存 | 是 |
| `SHARED_STATE_UNAVAILABLE` | 503 | 共享状态（Redis）不可用，无法安全计数/限流 | 是（退避） |
| `PROVIDER_UNAVAILABLE` | 503 | 上游不可用/熔断 | 是 |
| `PROVIDER_AUTH_FAILED` | 502 | 上游鉴权失败（密钥/额度配置问题） | 否 |
| `UPSTREAM_RATE_LIMITED` | 429 | 上游自身限流 | 是 |
| `UPSTREAM_TIMEOUT` | 504 | 上游超时 | 是 |
| `UPSTREAM_ERROR` | 502 | 上游返回错误 | 视情况 |
| `CLIENT_CANCELLED` | 499 | 客户端断开（只记日志，不回包） | 否 |
| `NOT_IMPLEMENTED` | 501 | 该能力尚未实现 | 否 |
| `INTERNAL_ERROR` | 500 | 网关内部错误 | 是 |

### 8.2 OpenAI 信封

```json
{
  "error": {
    "message": "...",
    "type": "rate_limit_error",
    "param": null,
    "code": "QUOTA_TOKENS_EXCEEDED"
  }
}
```

### 8.3 Anthropic 信封

```json
{
  "type": "error",
  "error": {
    "type": "rate_limit_error",
    "message": "..."
  }
}
```

Anthropic 客户端不识别 `code` 字段时，靠 `type` + HTTP 状态；网关仍把统一码放进扩展字段 `error.tt_code`，便于排障（不保证客户端展示）。

### 8.4 Gemini 信封

```json
{
  "error": {
    "code": 429,
    "message": "...",
    "status": "RESOURCE_EXHAUSTED",
    "details": [{"@type": "type.googleapis.com/tongtian.GatewayError", "code": "QUOTA_TOKENS_EXCEEDED"}]
  }
}
```

### 8.5 映射

| 统一码 | OpenAI type | Anthropic type | Gemini status |
| --- | --- | --- | --- |
| `AUTH_REQUIRED` / `TOKEN_EXPIRED` / `MTLS_REQUIRED` | `authentication_error` | `authentication_error` | `UNAUTHENTICATED` |
| `FORBIDDEN` / `MODEL_NOT_ALLOWED` | `permission_error` | `permission_error` | `PERMISSION_DENIED` |
| `INVALID_REQUEST` / `PAYLOAD_TOO_LARGE` | `invalid_request_error` | `invalid_request_error` | `INVALID_ARGUMENT` |
| `QUOTA_*` / `RATE_LIMITED` | `rate_limit_error` | `rate_limit_error` | `RESOURCE_EXHAUSTED` |
| `PROVIDER_UNAVAILABLE` / `POLICY_STALE` / `AUTH_UNAVAILABLE` / `SHARED_STATE_UNAVAILABLE` | `api_error` | `overloaded_error` | `UNAVAILABLE` |
| `PROVIDER_AUTH_FAILED` | `api_error` | `api_error` | `INTERNAL` |
| `UPSTREAM_RATE_LIMITED` | `rate_limit_error` | `rate_limit_error` | `RESOURCE_EXHAUSTED` |
| `UPSTREAM_TIMEOUT` | `api_error` | `api_error` | `DEADLINE_EXCEEDED` |
| `UPSTREAM_ERROR` | `api_error` | `api_error` | `INTERNAL` |
| `CLIENT_CANCELLED` | `api_error` | `api_error` | `CANCELLED` |
| `NOT_IMPLEMENTED` | `invalid_request_error` | `invalid_request_error` | `UNIMPLEMENTED` |
| `INTERNAL_ERROR` | `api_error` | `api_error` | `INTERNAL` |

---

## 9. 管理 API

仅内网。控制面 `/ui/` 通过服务端代理调用，网关不单独做完整后台。M2 起管理面鉴权为两层，
M4 起增加第三层角色校验：

1. **mTLS 客户端证书**：由反向代理终结 TLS 并校验证书，网关只采信**受信代理**写入的
   `X-TT-Client-Verify: SUCCESS` 与 `X-TT-Client-DN`。生产默认 `TT_GATEWAY_MTLS_MODE=proxy`；
   未通过时返回 `401 MTLS_REQUIRED`。网关自身不做 TLS 终结，且禁用 proxy headers，
   保证 `request.client.host` 不被 `X-Forwarded-For` 改写。
2. **管理令牌**：仍必须带 `X-TT-Admin-Token`（常量时间比较）；缺失或错误返回 `401 AUTH_REQUIRED`。
3. **管理角色（M4）**：控制面代理用 `X-TT-Admin-Roles` 下发调用者角色（`admin` / `operator` /
   `auditor`，逗号分隔）。`read` 动作允许三者，`write` 动作只允许 `admin` / `operator`；
   越权返回 `403 FORBIDDEN`。**没有该头**时按 `admin` 处理（直连管理 API 的共享令牌本身即 root）。
   控制面在转发前已按同一矩阵拦截 `user`，网关侧再校验一次，防止绕过代理。
4. **部门范围（M6）**：控制面代理对 `operator` 额外下发 `X-TT-Admin-Depts`（操作者所属部门，
   逗号分隔）与 `X-TT-Admin-Tenant`（操作者所属组织，用于隔离同名部门）。带该头时，operator
   只能查看/管理这些部门的 Key、配额与用量；越界 `403 FORBIDDEN`。
   `admin` 与 `auditor` 不下发该头，保持全局；不带头的 operator 与 M4 一样是全局运维（兼容直连）。
   `X-TT-Admin-Tenant` 缺失时只按部门名匹配（兼容直连脚本），控制面代理总是成对下发。

管理请求按证书主体（`cert:<DN>`）或对端 IP（`ip:<peer>`）独立限流，超限返回
`429 RATE_LIMITED` 并带 `Retry-After`。生产默认 120 RPM（桶容量 40），可用
`TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE=0` 显式关闭（启动告警）。控制面代理在
`https://` 管理面地址下可用 `TT_GATEWAY_ADMIN_CLIENT_CERT/KEY/CA` 出示客户端证书。

响应沿用管理信封 `{"ok": ...}`，失败为 `{"ok": false, "code": "...", "message": "..."}`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/gateway/v1/admin/health` | provider/策略/队列健康 |
| GET | `/gateway/v1/admin/providers` | provider 列表（不含密钥） |
| POST | `/gateway/v1/admin/providers` | 新增 provider |
| PATCH | `/gateway/v1/admin/providers/{id}` | 修改地址/超时/启用状态 |
| POST | `/gateway/v1/admin/providers/{id}/keys` | 新增密钥，返回一次明文 |
| POST | `/gateway/v1/admin/providers/{id}/keys/{keyId}/rotate` | 轮换 |
| DELETE | `/gateway/v1/admin/providers/{id}/keys/{keyId}` | 禁用/删除 |
| GET | `/gateway/v1/admin/usage` | 按租户/部门/用户/模型查询 |
| GET | `/gateway/v1/admin/quotas` | 配额快照 |
| PATCH | `/gateway/v1/admin/quotas/{principal}` | 调整配额 |
| POST | `/gateway/v1/admin/api-keys` | 为 CLI 创建网关 Key，返回一次明文 |
| GET | `/gateway/v1/admin/api-keys` | Key 列表（不含明文与哈希） |
| POST | `/gateway/v1/admin/api-keys/{keyId}/rotate` | 轮换 Key secret，返回一次明文；旧 secret 立即失效 |
| DELETE | `/gateway/v1/admin/api-keys/{keyId}` | 吊销（幂等） |
| GET | `/gateway/v1/admin/audit?after=&limit=` | 审计条目（按 id 升序游标） |
| GET | `/gateway/v1/admin/audit/verify` | 校验哈希链，返回链头与首个断点 |
| GET | `/gateway/v1/admin/audit/export?after=&limit=` | NDJSON 导出（经控制面代理原样透传） |

角色与动作：

| 动作 | 端点 | admin | operator | auditor |
| --- | --- | --- | --- | --- |
| read | 健康/provider/用量/配额/Key 列表/审计读取与导出 | ✓ | ✓ | ✓ |
| write | provider 与厂商密钥、配额调整、Key 签发/轮换/吊销 | ✓ | ✓ | ✗ |

`GET .../health` 中的 `sharedState` 披露共享状态模式，不含 Redis URL/密码：

```json
{
  "database": {
    "backend": "sqlite | postgres",
    "connected": true,
    "schemaVersion": 7
  },
  "sharedState": {
    "mode": "memory | redis",
    "transport": "single | sentinel",
    "tls": false,
    "connected": true,
    "workers": 2,
    "pid": 12345,
    "policySubscriber": "listening | stopped | disabled",
    "lastError": null
  }
}
```

`workers>1` 时 `mode` 必为 `redis`；`connected=false` 表示当前 worker 与 Redis 失联。
`database.backend=postgres` 表示使用 `TT_GATEWAY_DB_URL`；健康页不回显连接串。
同一份数据库上的多个 worker `gatewayId` 相同、`pid` 不同。

`GET .../health?tenant=<tenant>` 会按该租户读取策略（控制面模式下触发按需拉取），
`policyVersion` / `policyStale` 反映该租户当前生效版本；不带 `tenant` 时仍返回进程本地策略。

规则：

- 密钥只在创建/轮换时返回一次；之后只显示 `last4`。
- 所有管理操作写审计：时间、主体、角色、动作、对象、变更前后（厂商密钥只记 id/last4；网关 Key 只记 id、绑定与 scopes，不记明文与哈希）。
- 审计行带 `prevHash` / `entryHash` 形成哈希链，`audit/verify` 可定位首个被篡改的条目。
- 管理 API 不返回 prompt/response。
- `ttg_` Key 只用于三个协议面，不能当管理令牌使用；能力发现里不声明 Key 管理能力。

### 9.1 Provider 与密钥请求/响应

Provider 对象（GET/POST/PATCH 返回）：

```json
{
  "id": "azure_openai",
  "label": "Azure OpenAI",
  "baseUrl": "https://acme.openai.azure.com",
  "apiVersion": "2024-10-21",
  "timeoutSeconds": 120,
  "allowPrivate": false,
  "enabled": true,
  "configured": true,
  "keys": [
    {
      "id": "pk_01J9Z8...",
      "label": "prod-2026-10",
      "last4": "9f2c",
      "createdAt": 1761825600000,
      "disabledAt": null
    }
  ]
}
```

`POST /gateway/v1/admin/providers`：

```json
{
  "id": "azure_openai",
  "label": "Azure OpenAI",
  "baseUrl": "https://acme.openai.azure.com",
  "apiVersion": "2024-10-21",
  "timeoutSeconds": 120,
  "allowPrivate": false
}
```

- `id` 必须来自 §13 的 provider 矩阵，且未重复；`baseUrl` 接受 SSRF 校验；`allowPrivate=true` 仅对 `vllm` / `ollama` 生效。
- `apiVersion` 仅 Azure 使用；`timeoutSeconds` 默认 120，范围 1–600。

`PATCH /gateway/v1/admin/providers/{id}` 接受上述字段的任意子集，另加 `enabled`；返回同一个 Provider 对象。

`POST /gateway/v1/admin/providers/{id}/keys`：

```json
{"label": "prod-2026-10", "secret": "sk-...", "weight": 100}
```

响应 `201`：

```json
{
  "ok": true,
  "key": {
    "id": "pk_01J9Z8...",
    "label": "prod-2026-10",
    "last4": "9f2c",
    "createdAt": 1761825600000,
    "secret": "sk-..."
  }
}
```

- `secret` 仅在此响应出现一次；请求体与响应体都不得进日志。
- `weight` 可选，1–100，用于同一 provider 多密钥分流；省略为 100。

`POST /gateway/v1/admin/providers/{id}/keys/{keyId}/rotate`：

```json
{"secret": "sk-new...", "graceSeconds": 0}
```

响应：

```json
{
  "ok": true,
  "key": {"id": "pk_new...", "label": "prod-2026-10", "last4": "1a2b", "createdAt": 1761825600000, "secret": "sk-new..."},
  "previousKeyId": "pk_old...",
  "retireAt": 1761825600000
}
```

- 新密钥立即可用；旧密钥立即不再接收新请求，`graceSeconds`（0–300，默认 0）只保证在途请求完成。
- 每个 provider 同时最多一个“当前密钥”，历史密钥只在 `graceSeconds` 内保留。

`DELETE /gateway/v1/admin/providers/{id}/keys/{keyId}` 立即停用，返回 `{"ok": true, "keyId": "...", "disabledAt": 1761825600000}`；重复删除幂等，不杀在途请求。

### 9.2 网关 API Key 请求/响应

`POST /gateway/v1/admin/api-keys`：

```json
{
  "label": "claude-code-laptop",
  "tenant": "acme",
  "dept": "dept-sales",
  "sub": "u-1",
  "scopes": ["chat", "models"],
  "modelAllowlist": ["auto", "chat-a"],
  "rateLimitRpm": 120,
  "rateLimitTpm": 60000,
  "quotaMonthlyTokens": 2000000,
  "expiresAt": 1798761600000
}
```

响应 `201`：

```json
{
  "ok": true,
  "key": {
    "id": "9f2c1a7b4e60",
    "label": "claude-code-laptop",
    "tenant": "acme",
    "dept": "dept-sales",
    "sub": "u-1",
    "scopes": ["chat", "models"],
    "modelAllowlist": ["auto", "chat-a"],
    "rateLimitRpm": 120,
    "rateLimitTpm": 60000,
    "quotaMonthlyTokens": 2000000,
    "expiresAt": 1798761600000,
    "rotatedAt": null,
    "revokedAt": null,
    "createdAt": 1761825600000,
    "usedTokens": 0,
    "secret": "ttg_9f2c1a7b4e60_<43 位 base64url>"
  }
}
```

- `secret` 仅在创建与轮换响应出现一次；请求体与响应体都不得进日志。
- `tenant` / `dept` / `sub` 必填，长度 1–128，不得含 `:`、`*`、空白或控制字符。
- `label` 可选，≤128。
- `scopes` 非空、去重、必须全部来自 `chat` / `embeddings` / `models` / `messages` / `gemini`。
- `modelAllowlist` 可选，≤100 项，每项 1–128；省略或空数组表示只受部门策略约束。
- `rateLimitRpm` 可选，0–10000，省略为 0（不限）。
- `rateLimitTpm` 可选，0–10^9，省略为 0（不限）；语义见 §7.6。
- `quotaMonthlyTokens` 可选，0–10^15，省略为 0（不限）。
- `expiresAt` 可选，整数毫秒且必须大于当前时间；`null` 表示永不过期。

`GET /gateway/v1/admin/api-keys` 返回 `{"ok": true, "keys": [...]}`，字段与创建响应相同但**不含 `secret`**，
另带当前月 `usedTokens`，按创建时间倒序。

`POST /gateway/v1/admin/api-keys/{keyId}/rotate` 返回 `{"ok": true, "key": {...含新 secret...}}`；
`keyId` 不变，`rotatedAt` 更新，旧 secret 立即失效。已吊销或未知 Key 返回 `400 INVALID_REQUEST`。

`DELETE /gateway/v1/admin/api-keys/{keyId}` 返回 `{"ok": true, "keyId": "...", "revokedAt": 1761825700000}`；已吊销的 Key 返回原 `revokedAt`，未知 id 返回 `400 INVALID_REQUEST`。

审计动作：`api_key.create` / `api_key.rotate` / `api_key.revoke`。

协议面 scope 对照：

| 协议面 | 端点 | scope |
| --- | --- | --- |
| OpenAI | `POST /v1/chat/completions` | `chat` |
| OpenAI | `POST /v1/embeddings` | `embeddings` |
| OpenAI | `GET /v1/models` | `models` |
| Anthropic | `POST /v1/messages`、`POST /v1/messages/count_tokens` | `messages` |
| Gemini | `/v1beta/models...`（生成、流式、计数、列表） | `gemini` |

缺少 scope 返回 `403 FORBIDDEN`；请求模型不在白名单返回 `403 MODEL_NOT_ALLOWED`。
`GET /v1/models` 与 Gemini 模型列表按白名单过滤。JWT 主体不受 scopes 与白名单影响。

### 9.3 审计与外送

审计条目（读取/导出/外送同一结构）：

```json
{
  "id": 42,
  "atMs": 1761825700000,
  "actor": "u-1",
  "actorRole": "operator",
  "action": "api_key.rotate",
  "object": "9f2c1a7b4e60",
  "before": {"rotatedAt": null},
  "after": {"rotatedAt": 1761825700000},
  "prevHash": "…64hex…",
  "entryHash": "…64hex…"
}
```

- `GET .../audit?after=0&limit=100` 返回 `{"ok": true, "entries": [...], "nextAfter": 42, "headHash": "...", "legacyRows": 0}`；`limit` 1–1000。
- `GET .../audit/verify` 返回 `{"ok": true, "entries": N, "headHash": "...", "brokenAt": null, "legacyRows": 0}`；链断裂时 `ok=false`、`brokenAt` 为首个断点 id，HTTP 仍为 `200`。
- `GET .../audit/export?after=&limit=` 返回 `application/x-ndjson`，每行一个条目，附 `X-TT-Audit-Head`、`X-TT-Audit-Next` 响应头；控制面代理按原始字节透传。
- `GET .../health` 含 `auditWebhook: {enabled, pending, consecutiveFailures, lastError, lastDeliveredAt}`。

外送（仅当配置 `TT_GATEWAY_AUDIT_WEBHOOK_URL`）：

- 审计与 outbox 同事务落库，后台批量推送；单批 ≤ `TT_GATEWAY_AUDIT_WEBHOOK_BATCH_SIZE`。
- 请求头：`X-TT-Audit-Timestamp`、`X-TT-Audit-Signature: sha256=<hex>`、`X-TT-Gateway-Id`；
  签名串为 `f"{timestamp}.{body}"`，HMAC-SHA256，密钥 `TT_GATEWAY_AUDIT_WEBHOOK_SECRET`。
- 请求体：`{"gatewayId": "...", "sentAtMs": 1761825700000, "events": [<审计条目>]}`。
- 2xx 成功；4xx（除 408/429）确定性拒绝并跳过；5xx/429/网络错误指数退避重试（上限 300 秒）。
- 多 worker 下由 SQLite 租约抢占投递（`lease_owner = gatewayId:pid:uuid`，默认 60 秒）：
  抢到租约的 worker 独占该批，崩溃后租约过期由其他 worker 接手；租约不跨进程序列化，防止重复外送。
- 不保证恰好一次，接收端按 `gatewayId + id` 去重；外送不含明文密钥与 Key secret。
- Postgres 后端下 `FOR UPDATE SKIP LOCKED` 保证同一事件只被一个副本认领；租约字段语义不变。

### 9.4 部门级管理范围（M6）

- 头：`X-TT-Admin-Depts: dept-sales,dept-eng`，可配 `X-TT-Admin-Tenant: acme`（控制面代理成对下发）；
  只对 `operator` 生效，空头/缺失/全非法项视为全局；租户头缺失时只按部门名匹配。
- 列表：
  - `GET /api-keys` 只返回范围内部门；`POST /api-keys` 的 `dept` 与 `tenant` 必须在范围内。
  - `POST /api-keys/{id}/rotate`、`DELETE /api-keys/{id}` 按 Key 的 `dept` 校验，越界 403。
  - `GET /quotas` 只返回范围内部门，`overrides` 同样过滤；`PATCH /quotas/{principal}` 的目标
    部门与租户必须在范围内，`tenant:dept:*` 视为该部门级覆盖。
  - `GET /usage` 的 `dept` 必须在范围内，`tenant` 必须等于范围租户；范围只含一个部门且未给
    `dept` 时自动收窄到该部门，范围含多个部门且未给 `dept` → `400 INVALID_REQUEST`。
- provider 与健康：scoped operator 可读，不可写（写操作 403）；审计读取/导出只对全局角色开放。
- 审计条目记录实际操作人角色，不新增部门字段；部门范围不改变审计链结构。
- 控制面代理在转发前按同一规则拦截，网关再校验一次；直连管理 API（无角色头）仍是全局 root。

---

## 10. 数据归属

| 数据 | 归属 | 说明 |
| --- | --- | --- |
| 用户、组织、部门、成员关系 | 控制面 | 网关只读 JWT claims |
| 管理角色（`admin` / `operator` / `auditor` / `user`） | 控制面 | 签发进 JWT；代理按请求头下发给网关 |
| 模型目录、别名、允许列表、部门配额 | 控制面 | 网关缓存 |
| 厂商密钥 | **仅网关** | AES-GCM 加密；控制面永不存 |
| CLI API Key | **仅网关** | 只存 `sha256(secret)`；明文只在创建/轮换回一次，控制面不存 |
| 实时配额计数器 | 网关 | 控制面展示聚合；含 per-key 月度计数器 |
| 原始用量事件 | 网关 → 控制面 | 异步批量、幂等 |
| 聚合用量、报表 | 控制面 | 管理 UI 读这里 |
| 管理操作审计 | 控制面（策略）+ 网关（密钥/路由） | 两端各自哈希链；UI 合并展示 |
| 审计外送 | 网关 → 外部 SIEM | 仅当配置 Webhook；HMAC 签名、本地 outbox 重试 |
| prompt / response | **默认都不存** | 显式开启才记录，且需管理员确认 |

存储：

- 网关默认 SQLite：`{TT_GATEWAY_DATA_DIR}/gateway.db`；跨主机部署使用 `TT_GATEWAY_DB_URL`
  指向的 Postgres（§7.5），两边表结构与计数器语义一致。
- 跨主机副本共享同一份 Postgres 与 Redis；Redis 只承载热状态，丢失后由数据库重建。
- 控制面自己的 `market.db`；两边不共享文件、不跨库查询。
- 网关日志只记元数据：requestId、tenant、dept、user、model、tokens、latency、status、errorCode；不记正文。
- 数据库连接串、Redis URL、密码只从环境变量读取，不写日志、不在管理 API 回显。

---

## 11. 故障模式

| 故障 | 行为 |
| --- | --- |
| 网关不可达 | 组织模型不可用；客户端回退 BYOK/本地模型；控制面标记“模型入口降级” |
| 控制面不可达 | 网关用缓存策略与 JWKS 继续服务；用量本地缓冲；管理操作只读 |
| 策略过期 | 未超 stale grace：继续用最后可用版本，返回 `X-TT-Policy-Stale: 1`；超过则 503 `POLICY_STALE` |
| JWKS 不可达且无缓存 | 503 `AUTH_UNAVAILABLE` |
| Redis 不可达（多 worker） | 503 `SHARED_STATE_UNAVAILABLE`，失败关闭；不退化为进程内计数 |
| Redis 主节点故障（Sentinel） | 切换窗口内按不可达处理（503）；Sentinel 提升新主后自动重连，无需重启网关 |
| Postgres 不可达（跨主机） | 管理写入与请求记账失败；已在上游执行中的请求按错误结算，恢复后继续 |
| 策略被其他 worker 更新 | 经 Redis Pub/Sub 失效本 worker 内存缓存，下个请求重读共享 SQLite；订阅中断时按 TTL 兜底 |
| 上游 provider 故障 | 按策略链转移；记录实际模型；链耗尽返回 503 `PROVIDER_UNAVAILABLE` |
| 配额计数器丢失 | 从用量事件重建；按日对账；不静默回零 |
| 客户端断开 | 取消上游；按已知或估算 usage 结算；标记 `cancelled` |

---

## 12. 安全要求

- **SSRF**：provider base URL 只能由管理员配置；默认禁止私网/回环/metadata IP；本地 vLLM/Ollama 需显式 `allowPrivate=true`；每次请求重新解析 DNS 并校验。
- **密钥**：AES-GCM 加密存储；主密钥来自 `TT_GATEWAY_MASTER_KEY`；支持轮换；日志/错误/管理 API 永不回显明文。
- **认证**：RS256 + JWKS；校验 `iss/aud/exp/type`；支持内省吊销（可选）；网关 API Key 哈希存储、可吊销。
- **限额**：请求体、header 大小、输出 token、图片大小、超时、并发、RPM/TPM。
- **TLS**：对外必须经反向代理终止 TLS；网关只监听内网。
- **内容**：默认不落 prompt/response；错误消息不回显正文；管理审计只记元数据。
- **依赖**：LiteLLM 锁版本；每月跟安全更新；不把上游企业版代码带进开源发行。

---

## 13. Provider 能力矩阵

| Provider | Chat | Tools | Vision | Embeddings | 备注 |
| --- | --- | --- | --- | --- | --- |
| Azure OpenAI | 是 | 是 | 是 | 是 | deployment 名需别名映射 |
| 阿里百炼 | 是 | 需测 | 是（qwen-vl） | 是（text-embedding-v3） | 兼容模式与原生模式差异需测 |
| 火山方舟 | 是 | 需测 | 是（doubao-vision） | 需测 | 模型为 endpoint id |
| 腾讯云混元 | 是 | 需测 | 需测 | 需测 | OpenAI 兼容入口需确认 |
| Anthropic | 是 | 是 | 是 | 不提供 | 嵌入走独立 provider |
| Gemini | 是 | 是 | 是 | 是 | 原生协议面由网关翻译 |
| vLLM / Ollama | 是 | 模型决定 | 模型决定 | 模型决定 | 按能力声明 |

规则：矩阵里的“是”必须在 M2 有契约测试；“需测”不得对外声明；`capabilities` 只填真实支持项。

---

## 14. M0 验收

- `python tests/test_contract.py` 通过。
- 三套假上游能返回 OpenAI / Anthropic / Gemini 的合法响应和流式事件。
- `/health`、`/gateway/v1/capabilities` 可访问。
- 三个协议面的未实现路由返回对应协议的错误信封，统一码 `NOT_IMPLEMENTED`。
- `schemas/*.schema.json` 由 `scripts/export_schemas.py` 生成且可被 JSON Schema 校验器加载。
- 不发起任何真实 provider 网络请求。

---

## 15. 待定

1. 网关 API Key 的签发入口放在控制面哪个页面。
2. 默认嵌入 provider 与模型。
3. 模型别名清单（`auto/advanced/basic/economy` 的具体链路）。
4. 内网 provider 地址白名单的维护方式（配置文件还是管理 API）。

---

## 16. 变更记录

`contractVersion` 保持 `gateway-contract.v1`；以下均为加法修订，旧客户端忽略未知字段即可。

| 日期 | 变更 | 位置 |
| --- | --- | --- |
| 2026-10-30 | 适配器错误码：`PROVIDER_AUTH_FAILED`、`UPSTREAM_RATE_LIMITED`、`CLIENT_CANCELLED` | §8.1、§8.5 |
| 2026-10-30 | 网关短票：换票接口、claims 表、JWKS、校验顺序、客户端刷新策略 | §3.1 |
| 2026-10-30 | 能力发现：`protocols[]` 增加逐协议 `stream/tools/vision/embeddings`、`testedAt`、`note` | §2.1 |
| 2026-10-30 | 策略：顶层 `version`、`models.*.pricing`、`ETag` 语义 | §5.1、§5.2 |
| 2026-10-30 | 用量：`origin`、`apiKeyId` / `priceVersion` 可空、`estimated` 时允许 usage 缺省、成本公式 | §6.1 |
| 2026-10-30 | 控制面用量接收端点使用 `TT_GATEWAY_SERVICE_TOKEN` 服务令牌 | §6.2 |
| 2026-10-30 | 流式错误事件：三种协议各自的 error 帧样例 | §4.5 |
| 2026-10-30 | 管理 API：provider 与密钥的请求/响应体，继续使用 `X-TT-Admin-Token` | §9.1 |
| 2026-10-30 | 协议面细节：Anthropic 版本头必填、单图 20MB 上限、Gemini `parts`/模型列表形状 | §4.2、§4.3 |
| 2026-10-31 | 协议面共同规则：分块写请求直接 413、超时拆分为连接/整段响应/流式空闲、同 provider 有限重试边界 | §4.4 |
| 2026-10-31 | 管理面 M2：mTLS（经受信代理校验、`MTLS_REQUIRED`）、按证书 DN/IP 限流与 `Retry-After`、控制面客户端证书 | §8.1、§8.5、§9 |
| 2026-10-31 | 网关 API Key（M3）：`ttg_` 签发/列表/吊销、scopes 与模型白名单、无缓存吊销 | §3.2、§9、§9.2 |
| 2026-10-31 | 管理面治理（M4）：admin/operator/auditor 角色与 `X-TT-Admin-Roles`、审计哈希链与 NDJSON 导出、Webhook 外送、per-key RPM/月度配额与轮换 | §3.2、§7.4、§9、§9.2、§10 |
| 2026-10-31 | 共享状态（M5）：同机多 worker + 共享 SQLite、Redis 令牌桶/在途预留/策略失效、`SHARED_STATE_UNAVAILABLE`、outbox 租约、`health.sharedState` | §7.3、§7.4、§8.1、§8.5、§9、§9.3、§11 |
| 2026-10-31 | 跨主机存储（M6）：`TT_GATEWAY_DB_URL` 启用 Postgres、advisory lock 审计链、`SKIP LOCKED` outbox、迁移与故障语义 | §7.3、§7.5、§9.3、§10、§11 |
| 2026-10-31 | TPM（M6）：主体/Key 两级分钟 token 桶、`rateLimitTpm`、结算退回、`RATE_LIMITED` | §7.1、§7.4、§7.6 |
| 2026-10-31 | 部门管理（M6）：`X-TT-Admin-Depts` 收窄 operator 的 Key/配额/用量范围 | §9、§9.4 |
| 2026-10-31 | Redis HA（M6）：Sentinel、TLS、主从切换后的重连与健康披露 | §7.7、§9、§11 |
