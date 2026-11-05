# tongtian-gateway

通天办公的模型网关（模型数据面）。对客户端暴露 OpenAI、Anthropic、Gemini 三套协议面，向上游接厂商与内网模型，负责组织级模型策略、配额与用量结算。

- 当前里程碑：**M8.1 已收口**——M1–M5 全部收口，M6.1–M6.8 已落地，M7.1–M7.4 完成真实部署演练；G5 12/12、G6 12/12、G7 9/9、G8 13/13、G9 16/16、G10 17/17（[docs/m1-acceptance.md](docs/m1-acceptance.md)、[docs/m2-acceptance.md](docs/m2-acceptance.md)、[docs/m3-acceptance.md](docs/m3-acceptance.md)、[docs/m4-acceptance.md](docs/m4-acceptance.md)、[docs/m5-acceptance.md](docs/m5-acceptance.md)、[docs/m6-acceptance.md](docs/m6-acceptance.md)）通过，M7 演练 52/52（[docs/m7-drill.md](docs/m7-drill.md)）。M6 为**跨主机多副本 + Redis 高可用**：Postgres 共享账本（配额/审计链/outbox）、TPM 限流、部门级管理角色、Redis Sentinel 与 TLS；范围与决策见 [M6-PLAN.md](M6-PLAN.md)。M7 不改功能，只把 [docs/security-review.md](docs/security-review.md) §4 里三条“上线前联调”的残余风险做成可复跑的证据；M8.1 收口 M7.3 实测到的“库侧懒连接”——账本在启动期校验，连不上或版本过高直接拒绝启动（`tests/test_m8_startup_ledger.py`）。
- 契约唯一来源：[CONTRACT.md](CONTRACT.md)。
- M1 任务拆分与顺序：[M1-PLAN.md](M1-PLAN.md)；M2：[M2-PLAN.md](M2-PLAN.md)。
- T0.1 已完成：LiteLLM 锁定 `1.106.6`，结论与适配规则见 [docs/spike-litellm.md](docs/spike-litellm.md)。
- T0.2 已完成：适配层接口与假实现见 `app/adapters/`，测试 `tests/test_adapters.py`。
- T0.3 已完成：换票接口、网关令牌 claims、JWKS 与刷新策略见 [CONTRACT.md §3.1](CONTRACT.md)。
- T0.4 已完成：契约增补 C2–C9（定价、ETag、用量 origin、流式错误、管理 API、服务令牌、逐协议能力）与变更记录见 [CONTRACT.md §16](CONTRACT.md)。
- T0.5 已完成：测试脚手架统一到 `tests/support.py`，契约与适配层测试共用同一套环境引导与假上游。
- T1.1–T1.10 已完成：SQLite 账本、AES-GCM 密钥、provider 管理、SSRF 校验、LiteLLM 适配器、OpenAI 协议面、配额、用量、启动自检与假上游错误面。
- T2.1–T2.11 已完成：Anthropic / Gemini 协议翻译、流式事件、count tokens、模型列表、中途错误、客户端取消、工具多轮与视觉；官方 SDK 冒烟见 `scripts/smoke_sdks.py`。
- T3.10–T3.13（网关侧）已完成：JWKS 缓存 10 分钟、未知 `kid` 强制刷新（节流 30 秒）、RS256 claims 校验、控制面策略拉取（ETag/304、SQLite 缓存、stale 宽限）。
- T4.1–T4.4（网关侧）已完成：用量后台上报器（200 条/批、5 秒间隔、指数退避、毒批次跳过）、按契约 6.2 的批量体与服务令牌鉴权、`GET /gateway/v1/admin/usage` 聚合、`PATCH /gateway/v1/admin/quotas/{principal}` 当月额度调整（写审计、立即生效）。
- T5.1–T5.8 已完成：连接/响应/流式空闲三层超时、同 provider 有限重试、SSRF/DNS 请求期复检、安全复查（[docs/security-review.md](docs/security-review.md)）、LiteLLM 启动预热、压测（`scripts/loadtest.py`）、部署运维手册（[docs/deploy.md](docs/deploy.md)）与 G5 验收编排。
- M2.1–M2.4 已完成：管理面 mTLS（代理终结 + 受信代理头）、按证书 DN/IP 的进程内令牌桶限流、`MTLS_REQUIRED`、生产默认开启/显式关闭告警、控制面客户端证书、G6 验收与文档回填。
- M3.1–M3.6 已完成：CLI 网关 API Key（`ttg_`）签发/列表/吊销、端点级 scopes 与模型白名单、控制面 `/ui/` 一次性明文展示、G7 验收。
- M4.1–M4.7 已完成：管理角色（admin/operator/auditor，读/写分离，角色变更吊销会话）、两端审计哈希链（分页/验链/NDJSON 导出）、网关审计 Webhook（outbox + HMAC 外送）、per-key RPM/月配额与 Key 轮换、G8 验收与文档回填。
- M5.1–M5.8 已完成：同一主机多个 worker 共享一份 SQLite；Redis 承载管理面/per-key 令牌桶、配额在途预留与并发计数（Lua 原子 + Redis `TIME()`，失败关闭 503 `SHARED_STATE_UNAVAILABLE`）；策略新版本经 Redis Pub/Sub 通知其他 worker 清缓存；审计 outbox 用 `BEGIN IMMEDIATE` 租约避免重复投递；`TT_GATEWAY_WORKERS>1` 强制要求 Redis；G9 验收与文档回填。
- M6.1–M6.8 已完成：数据库方言层（SQLite / Postgres，迁移带 advisory lock、拒绝更高 schema 版本）；`TT_GATEWAY_DB_URL` 共享账本使配额、审计哈希链与 outbox 跨主机一致；主体/Key 两档 TPM 限流（预留成本、结算退回、Redis 故障失败关闭）；scoped operator 按部门收窄管理面；Redis Sentinel 主从发现与 `rediss://` TLS；G10 验收与文档回填。
- M7.1–M7.4 已完成（不改产品代码）：真实 nginx 终结 TLS/mTLS + 真实证书链与 CRL（吊销/过期/未知签发者在代理侧被拒，合法证书主体落审计 `actor`，伪造校验头被对端绑定挡住）；真 1 主 2 从 + 3 个 Sentinel 的杀主与 `docker pause` 阻塞切主（进程不重启重连，限流桶随复制落到新主）；真实 Postgres 并发首启迁移、最小权限应用角色、`pg_dump`/`pg_restore` 恢复到独立库并核对 `headHash` 与用量；演练脚本 `scripts/m7_drill.py`，报告 [docs/m7-drill.md](docs/m7-drill.md)，结论回填 [docs/deploy.md](docs/deploy.md)（含 §13.1 上线前检查表）与 [docs/security-review.md](docs/security-review.md)。
- M8.1 已完成（改产品代码）：收口 M7.3 演练暴露的“库侧懒连接”——`enforce_ledger()` 在 lifespan 里先连库 + 跑幂等迁移再放流量，SQLite 打不开、Postgres 不可达、缺 psycopg、迁移失败或库里 schema 版本高于程序支持值一律拒绝启动；启动错误只取驱动异常第一行并抹掉 `://user:pass@`、`password=` 与 DSN 原文，日志与健康页仍不含凭据。`tests/test_m8_startup_ledger.py`（14 项，含真实 uvicorn 进程拒起），并以此镜像重跑 M7 全段 52/52。
- 控制面：`../tongtian-server`（账号、组织/部门、市场、聚合用量）。两个服务不共享数据库。

## 能力

| 能力 | 状态 |
| --- | --- |
| `GET /health`、`GET /gateway/v1/capabilities` | 可用，`features` 只声明已实测能力 |
| OpenAI `/v1/chat/completions`（流式、工具、视觉） | 可用，adapter 走 LiteLLM SDK |
| OpenAI `/v1/embeddings`、`/v1/models` | 可用，模型列表按部门白名单过滤 |
| Anthropic `/v1/messages`（流式、工具、视觉）、`/v1/messages/count_tokens` | 可用，`anthropic-version` 必填 |
| Gemini `generateContent` / `streamGenerateContent` / `countTokens` / `/v1beta/models` | 可用，模型列表按部门白名单过滤 |
| 中途错误与客户端取消 | 流开始后按协议发原生错误帧；取消按已产生内容估算结算 |
| 鉴权 | RS256 + JWKS（生产强制）；`hs256-dev` 仅限内网联调；跨部门用 `X-TT-Department` 切换；CLI 用 `ttg_` API Key |
| CLI API Key | 管理 API 签发/列表/吊销/轮换；明文只回一次，库中只存 `sha256(secret)`；按端点校验 scopes 与模型白名单，绑定 `tenant/dept/sub`；per-key `rateLimitRpm` 与 `quotaMonthlyTokens` 独立生效；无缓存，吊销、过期与轮换下一次请求立即生效 |
| 策略解析（别名链、部门白名单、输出上限） | 可用；配置控制面时按租户拉取，否则读 `TT_GATEWAY_POLICY_FILE` 或内置默认策略 |
| 策略缓存 | 内存 + SQLite 双层、TTL 300 秒、`If-None-Match`/304、stale 宽限 24 小时、`X-TT-Policy-Stale` |
| 配额 | SQLite 已结算用量 + Redis 在途预留/并发计数（单 worker 时仍用进程内状态）；`X-TT-Quota-Remaining/Reset` |
| Provider 与密钥 | 管理 API 增改查、启停、密钥轮换；AES-GCM 加密，明文只回一次，列表只有 `last4` |
| SSRF 防护 | 私网/回环/link-local/metadata 默认拒绝，仅 vLLM/Ollama 可开 `allowPrivate` |
| 用量 | `usage_events` 落库（成功/失败/取消）、`Decimal` 计价、待上报查询；不存 prompt/response |
| 用量上报 | 可用：后台线程每 5 秒推一批（单批最多 200 条）到 `POST /api/v1/gateway/usage`，控制面按 `(gatewayId, requestId)` 判重；失败指数退避重试，`400/409/413/415/422` 跳过不堵队列，事件始终留在本地账本可追溯 |
| 管理 API（用量/配额） | `GET /gateway/v1/admin/usage` 按租户/部门/用户/模型/日聚合；`PATCH /gateway/v1/admin/quotas/{principal}` 调当月额度（`tenant:dept:*` 为部门级），立即生效并写审计 |
| 管理角色（M4） | 控制面代理下发 `X-TT-Admin-Roles`：读 = admin/operator/auditor，写 = admin/operator；审计员可查审计与用量但不能改；无该头时按 admin 处理（直连共享令牌即 root） |
| 审计（M4） | `audit_log` 哈希链：分页读取、`/audit/verify` 验链、`/audit/export` NDJSON（带 `X-TT-Audit-Head`）；写操作与 Key 生命周期动作全部入链，不落明文与哈希 |
| 审计外送（M4） | `audit_outbox` 与审计同事务落库，后台每批推送；`HMAC-SHA256(secret, f"{timestamp}.{body}")`，头 `X-TT-Audit-Timestamp` / `X-TT-Audit-Signature` / `X-TT-Gateway-Id`；2xx 成功、4xx（除 408/429）跳过、5xx/429/网络错误指数退避；`health.auditWebhook` 展示积压与最近成功时间 |
| 管理面 mTLS | 生产默认 `TT_GATEWAY_MTLS_MODE=proxy`：只采信受信代理写入的 `X-TT-Client-Verify: SUCCESS` / `X-TT-Client-DN`；缺失或失败 `401 MTLS_REQUIRED`。协议面不受影响 |
| 管理面限流 | 按证书 DN（已验证）或对端 IP 分桶的令牌桶；单 worker 在进程内，多 worker 与 per-key 限流一起走 Redis 共享；生产默认 120 RPM / burst 40，超限 `429 RATE_LIMITED` + `Retry-After`；`0` 关闭 |
| 多 worker 共享状态（M5） | 同机 2–64 个 worker 共享同一 `TT_GATEWAY_DATA_DIR` 与 Redis 前缀；`health.sharedState` 披露 `mode/connected/workers/pid/policySubscriber`；Redis 不可用时限流与配额路径失败关闭（503 `SHARED_STATE_UNAVAILABLE`），不静默放开 |
| 跨主机多副本（M6） | 配置 `TT_GATEWAY_DB_URL`（Postgres）后，配额已结算量、审计哈希链与 outbox 落共享账本；各副本同一 `TT_GATEWAY_ID`、独立 `TT_GATEWAY_DATA_DIR`；迁移在 advisory lock 内只应用一次，拒绝更高 schema 版本；`TT_GATEWAY_CROSS_HOST=1` 强制要求 Postgres + 共享状态 |
| TPM 限流（M6） | 主体桶按策略 `quota.tpm`、Key 桶按 `rateLimitTpm`；单次成本 = `max(1, 输入估算) + 输出上限`，在途预留、结算退回未用部分；容量上限 60000，超出 `429 RATE_LIMITED`，Redis 故障失败关闭 |
| 部门级管理角色（M6） | `operator` 可带 `X-TT-Admin-Depts` / `X-TT-Admin-Tenant` 收窄范围：Key 列表/签发/轮换/吊销、配额与用量按部门过滤，越权 403；provider 写与审计读仍要求全局 admin/auditor |
| Redis 高可用（M6） | `TT_GATEWAY_REDIS_SENTINEL_MASTER` + `TT_GATEWAY_REDIS_SENTINEL_URLS` 经 Sentinel 发现主节点，主从切换后自动重连；`rediss://` 或 `TT_GATEWAY_REDIS_TLS=1` 上 TLS（支持自定义 CA 与双向认证）；`health.sharedState.mode` 披露 `memory/redis/sentinel` |
| 真实部署演练（M7） | 不改功能，用真实 nginx（TLS/mTLS + CRL）、真实 1 主 2 从 + 3 Sentinel、真实 Postgres 16（并发首启迁移、最小权限角色、`pg_dump`/`pg_restore`）复验部署侧假设；`scripts/m7_drill.py`，报告 `docs/m7-drill.md`，检查表见 `docs/deploy.md §13.1` |
| 账本启动门槛（M8.1） | lifespan 内先连库 + 幂等迁移再放流量：SQLite 打不开、Postgres 不可达、缺 psycopg、库里 schema 版本过高都拒起并点名后端与版本；启动错误先抹掉 `://user:pass@`、`password=` 与 DSN 原文再落日志；`tests/test_m8_startup_ledger.py` |

**生产启动门槛**：`TT_GATEWAY_ENV=production` 要求 `TT_GATEWAY_JWT_MODE=rs256`、`TT_GATEWAY_JWKS_URL`、`TT_GATEWAY_MASTER_KEY`、`TT_GATEWAY_ADMIN_TOKEN` 齐全；配置了控制面地址时还要求 `TT_GATEWAY_SERVICE_TOKEN`；`TT_GATEWAY_MTLS_MODE=proxy` 时受信代理名单不能为空；`TT_GATEWAY_WORKERS>1` 时必须有可连通的 `TT_GATEWAY_REDIS_URL`（缺配置或 PING 失败都拒绝启动）；`TT_GATEWAY_CROSS_HOST=1` 时还要求 `TT_GATEWAY_DB_URL` 与 Sentinel/Redis 共享状态。**账本同样在启动期校验**（M8.1）：SQLite 打不开、Postgres 连不上、缺 psycopg 或库里 schema 版本高于程序支持值都拒绝启动，日志点名后端与版本但不回显 DSN/口令——不再出现“进程健康、首个请求才 500”。缺项或非法值直接退出并点名变量名；仅 mTLS/限流显式关闭时打印告警。

## 目录

```text
app/
  config.py         环境变量与运行参数
  contract.py       能力/策略/用量事件的 Pydantic 模型（JSON Schema 来源）
  db.py             SQLite/Postgres 方言层、WAL、迁移（advisory lock、版本拒绝）、tx()
  secrets.py        AES-GCM 加解密、主密钥轮换、last4
  providers_store.py provider 与密钥的增删改查
  upstream.py       URL 校验、DNS、私网/metadata 黑名单、错误映射
  errors.py         统一错误码与三种协议的错误信封
  auth.py           RS256（优先）/ HS256 JWT、claims 校验、部门切换、`ttg_` API Key 入口
  api_keys_store.py API Key 签发、列表、吊销、轮换与校验（只存 sha256(secret)，无缓存）
  api_key_limits.py per-key 每分钟令牌桶与 TPM 桶
  roles.py          管理角色解析（read/write 两档）
  jwks.py           JWKS 拉取与缓存、未知 kid 强刷、失败降级
  policy.py         策略加载与快照
  policy_client.py  控制面策略拉取：ETag、SQLite 缓存、stale 宽限、Pub/Sub 失效通知
  shared_state.py   Redis 共享状态：令牌桶/预留/并发 Lua、策略订阅、Sentinel/TLS、失败关闭
  routing.py        别名链、部门白名单、输出上限解析
  providers.py      provider 能力矩阵
  quota_sqlite.py   已结算账本（SQLite/Postgres）+ Redis/进程内在途预留与并发计数
  usage_store.py    用量事件落库、聚合、待上报查询、计价
  usage_uploader.py 后台线程批量上报控制面：幂等批次、指数退避、毒批次跳过
  startup.py        启动自检、生产门槛与账本启动校验
  service.py        请求编排：路由、回退、配额、结算
  state.py          进程内共享状态装配
  adapters/         ChatAdapter / EmbeddingAdapter 与 LiteLLM 实现
  protocols/        三套协议面：OpenAI / Anthropic / Gemini 翻译与流式编码
  admin.py          管理 API（健康、provider 与密钥、CLI API Key、用量聚合、配额调整、审计查询/验链/导出）
  audit.py          审计写入、分页、验链与 NDJSON 导出（写入同事务入 outbox）
  audit_chain.py    哈希链规范化与摘要（与存储解耦，供回填复用）
  audit_outbox.py   审计外送：批量推送、HMAC 签名、重试与健康状态
  mtls.py           管理面 mTLS：受信代理判断、证书主体解析
  ratelimit.py      管理面令牌桶（按证书 DN / 对端 IP）
  main.py           FastAPI 装配：启动预热 LiteLLM、体积上限、安全响应头
schemas/            由 scripts/export_schemas.py 生成的 JSON Schema
docs/               deploy.md（部署运维）、security-review.md（安全复查）、m1–m6-acceptance.md（G5–G10）、spike-litellm.md
scripts/            export_schemas.py、spike_litellm.py、rebuild_quota.py、g3_acceptance.py、g4_acceptance.py、smoke_sdks.py、loadtest.py、acceptance.py、m2_acceptance.py、m3_acceptance.py、m4_acceptance.py、m5_acceptance.py、m6_acceptance.py
tests/              stdlib 测试：契约、适配层、核心、协议面、身份与策略、运营、加固、管理面 mTLS/限流、API Key、M4 角色/审计/外送/治理、M5 多进程 SQLite/共享状态/outbox 租约、M6 Postgres/Sentinel/TPM/部门角色、假上游
```

## 本地运行

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt     # macOS / Linux
.venv/Scripts/python scripts/export_schemas.py
.venv/Scripts/python tests/test_core.py
.venv/Scripts/python tests/test_ops.py        # M1.4 上报器 / 管理用量 / 配额
.venv/Scripts/python tests/test_hardening.py  # M1.5 超时 / 重试 / SSRF
.venv/Scripts/python tests/test_m2_admin.py    # M2 管理面 mTLS / 限流
.venv/Scripts/python tests/test_api_keys.py    # M3 CLI API Key
.venv/Scripts/python tests/test_m4_roles.py     # M4 管理角色
.venv/Scripts/python tests/test_m4_audit.py     # M4 审计哈希链与导出
.venv/Scripts/python tests/test_m4_webhook.py   # M4 审计外送
.venv/Scripts/python tests/test_m4_governance.py  # M4 per-key 限流/配额/轮换
.venv/Scripts/python tests/test_m5_schema.py      # M5 多进程迁移 / outbox 租约列
.venv/Scripts/python tests/test_m5_shared.py      # M5 Redis 令牌桶/预留/策略通知（需 Redis）
.venv/Scripts/python tests/test_m5_outbox.py      # M5 审计 outbox 租约
.venv/Scripts/python tests/test_m6_postgres.py    # M6 Postgres 迁移/审计链/配额（需 Postgres）
.venv/Scripts/python tests/test_m6_sentinel.py    # M6 Sentinel 发现与 TLS（需 Redis）
.venv/Scripts/python tests/test_m6_tpm.py         # M6 TPM 预留/结算（需 Redis）
.venv/Scripts/python tests/test_m6_admin_scope.py # M6 部门级管理角色
.venv/Scripts/python tests/test_m8_startup_ledger.py # M8 账本启动门槛（拒起 + 脱敏）
.venv/Scripts/python -m app.main
```

默认监听 `127.0.0.1:8001`。能力发现：

```bash
curl http://127.0.0.1:8001/gateway/v1/capabilities
```

开发令牌需自行用控制面同款密钥签发；测试脚本里有生成示例。

接入控制面（RS256 + 策略下发）的最小环境：

```bash
export TT_GATEWAY_JWT_MODE=rs256
export TT_GATEWAY_CONTROL_PLANE_URL=http://127.0.0.1:8000   # JWKS 自动推导为 /.well-known/jwks.json
export TT_GATEWAY_SERVICE_TOKEN=<与控制面一致的服务令牌>
export TT_GATEWAY_ADMIN_TOKEN=<管理 API 令牌>
export TT_GATEWAY_MASTER_KEY=<base64 32 字节>
```

## 配置

见 [.env.example](.env.example)。关键项：

- `TT_GATEWAY_JWT_MODE`：`rs256`（生产）或 `hs256-dev`（内网联调）。
- `TT_GATEWAY_JWKS_URL`：RS256 公钥地址；未填写时由 `TT_GATEWAY_CONTROL_PLANE_URL` 推导。
- `TT_GATEWAY_CONTROL_PLANE_URL` / `TT_GATEWAY_SERVICE_TOKEN`：两者齐备才启用控制面对接——按租户拉策略（ETag/304），同时把用量批量上报到 `POST /api/v1/gateway/usage`；未配置时只读本地策略文件，用量留在 `usage_events`。
- `TT_GATEWAY_USAGE_BATCH_SIZE` / `TT_GATEWAY_USAGE_FLUSH_SECONDS`：上报节奏（默认 200 条/批、5 秒一轮）；控制面不可达时指数退避（最长 300 秒）后重试，不丢事件。
- `TT_GATEWAY_POLICY_TTL_SECONDS` / `TT_GATEWAY_POLICY_STALE_GRACE_SECONDS`：策略缓存 TTL（默认 300 秒）与 stale 宽限（默认 24 小时）。
- `TT_GATEWAY_WORKERS`：同机 worker 数（1–64，默认 1）。`>1` 时必须同时配置 Redis，且所有 worker 使用同一 `TT_GATEWAY_DATA_DIR`、同一 `TT_GATEWAY_ID`；裸机用 `python -m app.main`（内部走 uvicorn `--workers`），容器用 `--workers "$TT_GATEWAY_WORKERS"`。
- `TT_GATEWAY_REDIS_URL`：`redis://` 或 `rediss://`；只从环境变量读取，不写日志、不进配置文件。多 worker 缺该项或 PING 失败都会拒绝启动。
- `TT_GATEWAY_REDIS_PREFIX`：键前缀（默认 `ttgw`，`[A-Za-z0-9._-]{1,32}`）；同机多套网关实例必须区分。
- `TT_GATEWAY_REDIS_TIMEOUT_MS`：Redis 命令超时（100–5000，默认 500）；超时按不可用处理，限流/配额路径失败关闭。
- `TT_GATEWAY_REDIS_PASSWORD` / `TT_GATEWAY_REDIS_DB`：Redis 认证与库号（URL 已带时以 URL 为准）。
- `TT_GATEWAY_REDIS_SENTINEL_MASTER` / `TT_GATEWAY_REDIS_SENTINEL_URLS`：经 Sentinel 发现主节点（地址列表 `host:port,host:port`）；两者需同时配置，生产至少 3 个 Sentinel。`TT_GATEWAY_REDIS_SENTINEL_PASSWORD` 为 Sentinel 自身认证。
- `TT_GATEWAY_REDIS_TLS` / `_TLS_CA` / `_TLS_CERT` / `_TLS_KEY`：Redis TLS；URL 用 `rediss://` 会自动开启，启用 TLS 时 URL 必须是 `rediss://`；CA 文件必填，CERT/KEY 成对则双向认证。
- `TT_GATEWAY_DB_URL`：`postgresql://` / `postgres://` 共享账本；留空用本地 SQLite。连接串只从环境变量读取，不写日志、不进健康响应。
- `TT_GATEWAY_CROSS_HOST`：`1` 时要求 Postgres + Sentinel/Redis 共享状态；各副本同一 `TT_GATEWAY_ID`、各自独立 `TT_GATEWAY_DATA_DIR`。
- `TT_GATEWAY_DEV_HS256_SECRET`：HS256 模式下必须与控制面开发密钥一致。
- `TT_GATEWAY_ADMIN_TOKEN`：管理 API 的 `X-TT-Admin-Token`。
- `TT_GATEWAY_POLICY_FILE`：未配置控制面且不填该文件时使用内置演示策略（租户 `acme`，部门 `dept-sales` / `dept-eng`）。
- `TT_GATEWAY_MASTER_KEY`：厂商密钥加密主密钥，base64 编码的 32 字节：

  ```bash
  python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"
  ```

- `TT_GATEWAY_MASTER_KEY_VERSION` / `TT_GATEWAY_MASTER_KEYS`：轮换时把新密钥写进 `MASTER_KEY` 并调大版本号，旧版本留在 `MASTER_KEYS`（JSON）里用于解密历史密文。
- `TT_GATEWAY_ALLOW_PRIVATE_PROVIDERS`：只有本地 vLLM/Ollama 需要，生产环境会给出告警。
- `TT_GATEWAY_CONNECT_TIMEOUT_SECONDS` / `TT_GATEWAY_UPSTREAM_TIMEOUT_SECONDS` / `TT_GATEWAY_STREAM_IDLE_TIMEOUT_SECONDS`：连接、上游整段响应、流式两帧空闲的超时，默认 10 / 120 / 120 秒。
- `TT_GATEWAY_UPSTREAM_RETRIES`：同一 provider 内的有限重试次数（默认 1，0 关闭），只重试限流/不可用/超时，尊重 `Retry-After`，流式开始下发后不再重试。
- `TT_GATEWAY_MAX_REQUEST_BYTES`：请求体上限（默认 32MB）；写请求必须带 `Content-Length`，`Transfer-Encoding: chunked` 直接 413。
- `TT_GATEWAY_MTLS_MODE`：管理面 mTLS，`proxy`（生产默认，需反代写入 `X-TT-Client-Verify`/`X-TT-Client-DN`）或 `off`。
- `TT_GATEWAY_TRUSTED_PROXIES`：受信反向代理 IP/CIDR（默认 `127.0.0.1,::1`）；proxy 模式为空或非法会拒绝启动。
- `TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE` / `TT_GATEWAY_ADMIN_RATE_LIMIT_BURST`：管理面限流（生产默认 120 RPM、桶 40；`0` 关闭）。按证书 DN 或对端 IP 分桶，超限 429 + `Retry-After`。
- `TT_GATEWAY_AUDIT_WEBHOOK_URL` / `TT_GATEWAY_AUDIT_WEBHOOK_SECRET`：审计外送接收端与 HMAC 密钥（至少 16 位）；两者齐备才启用，URL 不得含账号密码。
- `TT_GATEWAY_AUDIT_WEBHOOK_INTERVAL_SECONDS` / `_BATCH_SIZE` / `_TIMEOUT_SECONDS`：外送节奏与超时（默认 10 秒、50 条、10 秒），语义见 [docs/deploy.md §9.1](docs/deploy.md)。

## 测试

全部是 stdlib 脚本，不依赖 pytest/httpx：

- `tests/test_contract.py`：健康、能力发现、三种协议错误信封、令牌过期/伪造、部门切换、策略别名链、用量模型与 JSON Schema。
- `tests/test_adapters.py`：适配层接口与假实现的契约。
- `tests/test_core.py`：M1.1 全部后端能力——DB 迁移与事务、密钥加解密与轮换、provider 管理、SSRF 黑名单、配额并发/结算/重建、用量与计价、启动自检，以及 OpenAI 面的 HTTP 集成（200/400/429/502/503、流式、工具、视觉、嵌入、并发上限）。
- `tests/test_litellm_adapter.py`：LiteLLM 适配器对三家假上游的参数映射、流式事件、usage 来源与错误映射；密钥不出现在响应与异常里。
- `tests/test_protocols.py`：三协议编码器往返、Anthropic/Gemini 非流式与流式、count tokens、模型列表、中途错误、100 次取消、工具多轮与视觉大小限制。
- `tests/test_identity.py`：M1.3 身份与策略——RS256 验签（算法混淆、过期、TTL 上限、tenant/org 冲突、dept 越界全部拒绝）、未知 `kid` 触发 JWKS 刷新、JWKS 不可达降级、部门切换 403、策略 ETag/304、stale 头、未知租户 403、`JwksClient`/`PolicyClient` 单元行为。
- `tests/test_ops.py`：M1.4 网关侧自测（85 项）——上报器批次组装与 `batchId` 复用、退避与毒批次跳过、`GET /gateway/v1/admin/usage` 聚合与手算对账、`PATCH /quotas/{principal}` 立即生效、个人覆盖优先于部门级、清除后回落策略值、超限 429 带 `X-TT-Quota-Reset`。
- `tests/test_hardening.py`：M1.5 加固——连接/总超时拆分、`Retry-After` 与退避上限、同 provider 重试与不重试边界、流式空闲超时、SSRF/DNS 请求期复检、重试后用量只记一条。
- `tests/test_m2_admin.py`：M2 管理面——受信代理判断与失败关闭、证书主体解析、令牌桶、启动校验、401 `MTLS_REQUIRED`、429 + `Retry-After`、按 DN 隔离、审计主体回落证书 DN（31 项）。
- `tests/test_api_keys.py`：M3 CLI API Key——签发校验、明文一次性与哈希落库、列表不含明文/哈希、吊销幂等、scopes 与三种协议信封、模型白名单（别名/具体模型）、部门切换 403、用量 `apiKeyId`、过期与无缓存吊销（45 项）。
- `tests/test_m4_roles.py`：M4 管理角色——角色头解析与优先级、审计员只读、运维可写、未知/空角色 403、多角色取最强、无角色头按 admin（18 项）。
- `tests/test_m4_audit.py`：M4 审计哈希链——写入与动作主体、分页游标、验链、篡改定位、NDJSON 导出与头哈希、审计员读/写边界（23 项）。
- `tests/test_m4_webhook.py`：M4 审计外送——假接收端验签、网关 ID、500 重试与恢复、4xx 跳过、健康状态、不含密钥（15 项）。
- `tests/test_m4_governance.py`：M4 Key 治理——`rateLimitRpm`/`quotaMonthlyTokens` 校验与生效、令牌桶按时间恢复、月度计数器、轮换同 keyId 与旧 secret 立即失效、审计员不能轮换（30 项）。
- `tests/test_m5_schema.py`：M5 迁移与并发启动——schema v6 的 outbox 租约列/索引、WAL 重试、两个进程同时迁移只执行一次（9 项）。
- `tests/test_m5_shared.py`：M5 Redis 共享状态——Lua 令牌桶按 Redis 时间恢复、预留/释放、Key 月配额、策略 Pub/Sub、Redis 不可达时失败关闭（19 项；默认连 `redis://127.0.0.1:6391/0`，可用 `TT_GATEWAY_REDIS_URL` 覆盖，连不上自动跳过）。
- `tests/test_m5_outbox.py`：M5 审计 outbox 租约——并发 flush 只投递一次、有效租约内不重复、过期租约可被接管、成功后清空租约（7 项）。
- `tests/test_m6_postgres.py`：M6 Postgres 后端——拒绝更高 schema、并发启动只迁移一次、跨连接审计链连续、outbox 认领互斥、账本去重与配额共享（21 项；默认连 `127.0.0.1:55432`，可用 `TT_GATEWAY_TEST_PG_URL` 覆盖，连不上跳过）。
- `tests/test_m6_sentinel.py`：M6 Sentinel 与 TLS——假 Sentinel 发现、主从切换、`rediss://` 证书校验、非法组合拒绝启动（23 项；可用 `TT_GATEWAY_REDIS_URL` 覆盖）。
- `tests/test_m6_tpm.py`：M6 TPM——主体/Key 两档桶、预留与退回、容量上限、Redis 故障失败关闭（27 项）。
- `tests/test_m6_admin_scope.py`：M6 部门角色——列表/签发/轮换/吊销与配额/用量的部门收窄、越权 403、provider 写与审计读边界（32 项）。
- `tests/test_m8_startup_ledger.py`：M8 账本启动门槛——健康账本放行、schema 版本过高拒起且不改写回、SQLite 路径不可用拒起、Postgres 不可达拒起（无 psycopg 时跳过）、错误消息只留第一行并抹掉 DSN/口令、真实 uvicorn 进程退出码非 0 且日志不含凭据（14 项）。
- `scripts/g3_acceptance.py`（跨服务验收，约 1 分钟）：拉起真实控制面与网关进程 + 假上游，完成「换票 → 对话（流式/非流式）→ HS256 被拒 → 篡改 tenant 401 / 越权部门 403 → 控制面重启换 kid 后网关刷新 JWKS 继续服务」，并扫描两侧日志确认无 JWT 明文。
- `scripts/g4_acceptance.py`（跨服务验收，约 40 秒）：真实三进程 + 假上游，验证 G4 四条——对话后 10 秒内控制面出现带 model/tokens/latency/cost 的用量、同一批上报 3 次总量不变、经网关调用在合并视图只算一次（BYOK 直连照常计入）、部门月额度调到 1 万 token 后超限 429 `QUOTA_TOKENS_EXCEEDED` 且响应头带重置时间；末尾扫描两侧日志确认无管理令牌/服务令牌/provider 密钥/主密钥明文。
- `scripts/smoke_sdks.py`（可选）：用官方 `anthropic` / `google-genai` SDK 以同协议请求完成 G2 替代冒烟；SDK 未安装时自动跳过。
- `scripts/loadtest.py`（压测，手工）：默认 50 并发 / 250 请求，输出 p50/p95/p99 与净开销；`--concurrency 10 --requests 40` 作为 G5 冒烟。
- `scripts/acceptance.py`（G5 验收，约 1.5 分钟）：重跑 G3/G4 子进程与压测冒烟，并在生产模式（RS256 + JWKS + master key）下启动网关，检查 `/docs` 关闭、错误管理令牌 401、日志无密钥、缺主密钥拒绝启动；报告写入 `docs/m1-acceptance.md`。
- `scripts/m2_acceptance.py`（G6 验收，约 1.5 分钟）：G5 回归 + 生产默认 mTLS/限流 + 显式关闭 + 非法配置拒绝启动；报告写入 `docs/m2-acceptance.md`。
- `scripts/m3_acceptance.py`（G7 验收，约 30 秒）：`tests/test_api_keys.py` + 生产模式端到端（RS256、mTLS、控制面策略）——签发/列表、三协议面 scope 与白名单、用量 `apiKeyId`、部门绑定、吊销即时生效、过期与审计脱敏；报告写入 `docs/m3-acceptance.md`。
- `scripts/m4_acceptance.py`（G8 验收，约 2 分钟）：两端 5 个 M4 用例 + 生产配置端到端（RS256、控制面策略、审计外送）——经代理的角色读写矩阵、改角色吊销会话、两端审计链验链/导出、Webhook HMAC 逐条复算、per-key RPM/月配额/轮换，并扫描日志与外送载荷确认无管理令牌/Webhook 密钥/provider 密钥/Key 明文；报告写入 `docs/m4-acceptance.md`。
- `scripts/m5_acceptance.py`（G9 验收，约 40 秒 + Redis）：同一数据目录启动两个网关进程（各自 `TT_GATEWAY_WORKERS=2` 语义、共享 SQLite/Redis）——共享限流/月配额/并发计数、`CLIENT PAUSE` 下失败关闭与恢复、B 的 TTL=300s 时经 Pub/Sub 立即切策略版本、两端审计链一致与 outbox 恰好一次、管理面令牌桶共享、`workers>1` 缺 Redis/连不上拒绝启动；报告写入 `docs/m5-acceptance.md`。默认自动起一个带密码的 `redis:7-alpine` 容器，也可用 `M5_REDIS_URL` 指向现成 Redis。
- `scripts/m6_acceptance.py`（G10 验收，约 3–4 分钟 + Postgres/Redis）：先跑 M6/M5 单元套件，再以两个独立 uvicorn 进程（同一 `TT_GATEWAY_ID`、共享 Postgres + Redis）验证并发首次迁移、跨进程 RPM/月配额/并发计数/TPM、审计链与 outbox 恰好一次、敏感值不落日志；随后用真实 Redis 主从 + 假 Sentinel 验证主从切换后两进程恢复且共享桶保留；报告写入 `docs/m6-acceptance.md`。默认自动起 `postgres:16-alpine` 与 `redis:7-alpine`，也可用 `M6_PG_URL` / `M6_REDIS_URL` 指向现成实例。
- `scripts/m7_drill.py`（M7 部署演练，约 6–8 分钟 + Docker）：不改产品代码，只在隔离 Docker 网络里用真实组件复验部署侧假设，分三段——`--only tls`（19 步：nginx 终结 TLS + 演练 CA/CRL，吊销·过期·未知签发者证书在代理侧被拒，证书主体落审计 `actor`，伪造校验头被对端绑定挡住，网关不发布宿主端口）、`--only sentinel`（17 步：1 主 2 从 + 3 个 Sentinel，杀主与 `docker pause` 阻塞下的失败关闭与自动重连，限流桶随复制落到新主）、`--only postgres`（16 步：并发首启迁移只应用一次、最小权限应用角色、provider/Key/对话写入共享账本、`pg_dump -Fc` → `pg_restore --exit-on-error` 到独立库后 `headHash` 与用量逐字段一致、schema 版本过高时容器直接退出（M8.1 的启动门槛）、提示词与凭据不落盘）；报告写入 `docs/m7-drill.md`，`--keep` 保留容器与生成的 PKI/备份。

引擎侧网关模式（T3.14–T3.16）的模型配置示例：

```json
{
  "modelConfig": {
    "gateway": {
      "baseUrl": "http://127.0.0.1:8001",
      "controlPlaneUrl": "http://127.0.0.1:8000",
      "dept": "dept-sales"
    }
  }
}
```

`accessToken` 可放在 `gateway.accessToken`（仅内存），也可由请求头 `Authorization`/`X-TT-Engine-Token` 透传；引擎按请求换票并缓存，到期前 2 分钟刷新，401 时强制重换一次，令牌不写日志、不落配置文件。

## 部署

单机可跑 1–64 个 worker，共享一份 SQLite 与一个 Redis；跨主机多副本需配置 `TT_GATEWAY_DB_URL`（Postgres）与共享 Redis/Sentinel，并设 `TT_GATEWAY_CROSS_HOST=1`（各副本同一 `TT_GATEWAY_ID`、独立数据目录）。单 worker 不需要 Redis；多 worker 必须配置 Redis，否则拒绝启动。副本启动时会先连库并跑幂等迁移，账本不可用或库里版本高于程序支持值直接拒起（M8.1），因此编排里 `/health` 只当存活探针、`/gateway/v1/admin/health` 当就绪探针。对外必须经 TLS 反向代理。完整上线步骤（环境变量矩阵、Postgres 与 Redis 高可用、主密钥备份与轮换、JWKS 轮换、管理面 mTLS、备份恢复、监控与排障）见 [docs/deploy.md](docs/deploy.md)；安全结论与残余风险见 [docs/security-review.md](docs/security-review.md)。`Dockerfile` 与 `docker-compose.yml` 提供可直接改用的启动方式（compose 的 `shared` profile 会一并起本地 Redis）。生产环境要求 RS256 + JWKS + master key + admin token（以及控制面服务令牌）齐全，缺项启动即退出；管理面默认要求反向代理完成 mTLS 并启用限流。
