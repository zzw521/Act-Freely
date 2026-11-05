# 网关安全复查（M1.5 / T5.4，M2、M3、M4、M5、M6、M7、M8 增补）

复查对象：`tongtian-gateway` 当前实现（M1.1–M1.5 + M2.1–M2.4 + M3.1–M3.6 + M4.1–M4.7 + M5.1–M5.8 + M6.1–M6.8 + M7 部署演练 + M8.1 账本启动门槛）。
结论先行：**未发现需要阻断 G5/G6/G7/G8/G9/G10 的问题**；
M1.5 修复 1 项（分块请求体绕过 32MB 上限），M2 为管理面增加 mTLS 与限流，M3 增加 CLI API Key，
M4 增加管理角色、审计哈希链/外送与 per-key 限流/配额/轮换，M5 增加同机多 worker、Redis
共享热状态、失败关闭、策略 Pub/Sub 与 outbox 租约，M6 增加 Postgres 共享账本、跨主机副本、
TPM 限流、部门级管理角色与 Redis Sentinel/TLS；M7 不改功能，只用真实 nginx、真实 1 主 2 从
+ 3 个 Sentinel 与真实 Postgres 16 把 §4 里三条"上线前联调"的残余风险做成可复跑的证据
（`scripts/m7_drill.py`，报告 `docs/m7-drill.md`，52/52 步通过）；M7.3 演练暴露的“库侧懒连接”
由 M8.1 收口（账本校验提前到启动门槛，`tests/test_m8_startup_ledger.py`，并重跑 M7 全段 52/52
确认新门槛不影响三段演练）；其余为已实现项与需在部署侧闭环的残余风险。

复查方式：逐文件走读 + 已有自动化用例（`tests/test_core.py`、`test_protocols.py`、
`test_hardening.py`、`test_ops.py`、`test_m4_*.py`、`test_m5_*.py`、`test_m6_*.py`、G9、G10）
复跑 + M7 容器演练（`scripts/m7_drill.py --only tls|sentinel|postgres`）。安全假设沿用项目约束：**内网/私有化部署优先**；
BYOK 直连不经过网关、密钥只进本机操作系统钥匙串；网关不保存 prompt/response；
provider 密钥明文只在创建/轮换响应出现一次；浏览器永远拿不到网关管理令牌。
M6 追加约束：DB DSN、Redis URL 与密码只从环境变量读取，不落日志、不进管理 API；
健康页只披露后端模式与连通性。

## 1. 结论表

| # | 检查项 | 结论 | 证据 |
|---|---|---|---|
| 1 | 管理面鉴权 | 通过 | mTLS（`app/mtls.py:78`）+ `app/admin.py:42` 常量时间比较；生产强制非空 |
| 2 | 用户令牌校验 | 通过 | `app/auth.py:100` RS256/JWKS；`app/auth.py:163` TTL ≤ 900s |
| 3 | 网关 API Key（`ttg_`） | 通过（M3） | `app/api_keys_store.py:160` 常量时间比较、只存 sha256、无缓存吊销；`tests/test_api_keys.py`、G7 |
| 4 | 厂商密钥存储与回显 | 通过 | AES-GCM + 版本轮换；列表只回 last4 |
| 5 | 密钥脱敏 | 通过 | `app/adapters/base.py:477` 去掉已知密钥与 Authorization/API-Key 形态 |
| 6 | 不落 prompt/response | 通过 | 日志语句审计：只有 request_id、计数与异常类型 |
| 7 | 错误回包 | 通过 | 统一错误码；未处理异常只回 `INTERNAL_ERROR`，堆栈只进日志 |
| 8 | SSRF / 元数据地址 | 通过 | `app/upstream.py:128` 请求期复检；私网需显式开关 |
| 9 | 请求体上限 | **本轮修复** | 原实现只看 Content-Length，分块请求可绕过；`app/main.py:130` 现直接 413 |
| 10 | 安全响应头 | 通过 | `app/main.py:154` nosniff / no-referrer / no-store |
| 11 | 生产启动门槛 | 通过 | `app/startup.py:138` 缺项即拒绝启动并点名变量；`app/startup.py:160` 账本校验（M8.1） |
| 12 | 文档端点 | 通过 | `app/main.py:113` 生产关闭 `/docs`、`/redoc`、`/openapi.json` |
| 13 | 依赖版本 | 通过（人工） | `requirements.txt` 全量钉版本；本机无 pip-audit，见 §5 |
| 14 | 管理面 mTLS（M2） | 通过 | 仅采信受信代理写入的校验头（`app/mtls.py:78`）；uvicorn `proxy_headers=False`（`app/main.py:315`） |
| 15 | 管理面限流（M2/M5） | 通过 | `app/ratelimit.py` 按证书 DN/IP 分桶；单 worker 进程内、多 worker 走 Redis 共享桶；429 + `Retry-After` |
| 16 | CLI API Key 权限边界（M3） | 通过 | `app/protocols/common.py:scoped_principal` 端点级 scope；`app/routing.py:model_visible` 白名单；绑定部门不可切换 |
| 17 | 管理角色与读写分离（M4） | 通过 | `app/roles.py` 读 = admin/operator/auditor、写 = admin/operator；控制面 `X-TT-Admin-Roles` 由服务端会话推导，浏览器不可设置 |
| 18 | 审计哈希链与导出（M4） | 通过 | `app/audit_chain.py` 逐条 `sha256(prev_hash \|\| 字段)`；`/audit/verify` 复算；导出带 `X-TT-Audit-Head/Next` |
| 19 | 审计外送（M4） | 通过 | `app/audit_outbox.py` 同事务落库、后台批次；HMAC-SHA256 签名 + 时间戳头；失败退避，不含明文密钥 |
| 20 | per-key 限流/配额/轮换（M4/M5） | 通过 | `app/api_key_limits.py` 令牌桶；单 worker 进程内、多 worker 走 Redis 共享桶；月度计数落 SQLite；`rotate` 同 keyId 换密、旧密立即 401；`tests/test_m4_governance.py`、G8 |
| 21 | 多 worker 共享状态（M5） | 通过 | `app/shared_state.py` Lua 原子令牌桶/预留/并发；Redis `TIME()` 计时；失败关闭 `503 SHARED_STATE_UNAVAILABLE`（`app/quota_sqlite.py`、`app/ratelimit.py`）；G9 |
| 22 | Redis 凭据与日志（M5） | 通过 | 连接串只读 `TT_GATEWAY_REDIS_URL`；`health.sharedState` 只回模式/连通性；G9 扫描进程日志确认无 URL/密码 |
| 23 | 策略失效传播（M5） | 通过 | 新版本先写共享 SQLite 再发 Pub/Sub；其他 worker 清内存缓存后从库重读；Pub/Sub 丢失回落 TTL；`tests/test_m5_shared.py`、G9 |
| 24 | 审计 outbox 租约（M5） | 通过 | `BEGIN IMMEDIATE` 内认领、`lease_owner=gatewayId:pid:uuid`、过期可接管；`tests/test_m5_outbox.py`、G9 |
| 25 | Postgres 共享账本与迁移（M6） | 通过 | `app/db.py` 方言层；迁移在 `pg_advisory_xact_lock` 内串行、版本单调且拒绝更高版本；`tests/test_m6_postgres.py`、G10 |
| 26 | 跨主机配额/审计/outbox（M6） | 通过 | 已结算用量、审计链、outbox 落 Postgres；跨进程审计 headHash 一致、outbox 租约互斥；G10 |
| 27 | TPM 限流（M6） | 通过 | `app/shared_state.py` TPM 桶；主体按 `quota.tpm`、Key 按 `rateLimitTpm`；预留/退回、Redis 故障 503；`tests/test_m6_tpm.py`、G10 |
| 28 | 部门级管理角色（M6） | 通过 | `app/roles.py` 解析 `X-TT-Admin-Depts`/`X-TT-Admin-Tenant`；列表/签发/轮换/吊销与配额/用量按部门收窄，provider 写与审计读仍要求全局；`tests/test_m6_admin_scope.py`、G10 |
| 29 | Redis Sentinel 与 TLS（M6） | 通过 | `app/shared_state.py` 经 Sentinel 发现主节点；`rediss://` 强制 TLS、CA/双向认证可配；切主后两副本恢复且桶保留；`tests/test_m6_sentinel.py`、G10 |
| 30 | 连接串与健康面（M6） | 通过 | DSN/Redis URL/密码只读环境变量；`health.database`/`health.sharedState` 只回模式/连通性/版本；G10 扫描日志确认无连接串与密码 |
| 31 | 管理面 mTLS 真实链路（M7.1） | 通过（演练） | 真实 nginx 终结 TLS + 演练 CA 签发与 CRL：吊销/过期/未知签发者证书在代理侧即被拒（不进应用），合法证书主体经 `$ssl_client_s_dn` 落审计 `actor`；同网段直连伪造校验头被对端绑定挡住；`scripts/m7_drill.py --only tls` |
| 32 | Redis 高可用真实切主（M7.2） | 通过（演练） | 真 1 主 2 从 + 3 个 Sentinel（quorum=2）：杀主后自动提升新主，两个网关进程不重启重连（RestartCount=0、容器启动时间未变），限流桶随复制落到新主；`docker pause` 阻塞期一律 `503 SHARED_STATE_UNAVAILABLE`，解除后自愈 |
| 33 | Postgres 迁移与备份恢复（M7.3） | 通过（演练） | 两个进程同时首启时迁移在 `pg_advisory_xact_lock` 内只应用一次（`schema_version` 一行）；`pg_dump -Fc` → `pg_restore --exit-on-error` 后第三个进程指向恢复库：审计条数/`headHash`/用量总额与主库逐字段一致，同一主密钥仍能解密 provider 密钥并跑通对话；应用角色非超级用户/不建角色/不建库/不复制 |
| 34 | 账本启动门槛（M8.1） | 通过 | 启动期先连库+迁移再放流量：SQLite 打不开、Postgres 不可达、库里版本高于程序支持值都拒绝启动并点名后端与版本；错误消息与进程日志不回显 DSN/口令；见 `tests/test_m8_startup_ledger.py` |

## 2. 逐项说明

### 2.1 管理面（`/gateway/v1/admin/*`）

- **mTLS（M2）**：TLS 在反向代理终结，nginx 写入 `X-TT-Client-Verify: SUCCESS` 与
  `X-TT-Client-DN`。网关只在 `TT_GATEWAY_MTLS_MODE=proxy`、对端命中
  `TT_GATEWAY_TRUSTED_PROXIES` 且校验值为 `SUCCESS` 时采信这两个头（`app/mtls.py:78`）；
  否则返回 `401 MTLS_REQUIRED`。直连网关无法伪造证书结论。
  M7.1 用真实 nginx + 真实证书链复验：TLS1.2/1.3 握手、被 CRL 吊销/已过期/未知签发者的
  客户端证书都在代理侧被拒（不进应用），无证书访问管理面回 nginx 自己的 401；代理只写校验头
  不设门槛时网关仍失败关闭；同网段非受信主机直连伪造校验头被对端绑定挡住。
- 对端地址是信任边界：uvicorn 禁用 proxy headers（`app/main.py:315`，Dockerfile
  `--no-proxy-headers`），`X-Forwarded-For` 不能改写 `request.client.host`。
- 令牌来自 `X-TT-Admin-Token`，用 `hmac.compare_digest` 比较，避免时序侧信道（`app/admin.py:42`）。
  生产环境启动自检要求非空（`app/startup.py`），开发态才回落到 `dev-admin-token`（`app/config.py`）。
- **限流（M2/M5）**：按已验证证书 DN（`cert:<DN>`）或对端 IP（`ip:<peer>`）分桶，令牌桶位于
  令牌校验之前，令牌爆破同样受限；超限返回 `429 RATE_LIMITED` + `Retry-After`
  （`app/ratelimit.py`）。生产默认 120 RPM / burst 40。单 worker 用进程内桶；多 worker 时
  同一 rateKey 共用 Redis 桶，否则各 worker 各算一份，限流形同虚设。
- 管理面由控制面代持：浏览器只带用户 JWT 调控制面，控制面再带管理令牌访问网关；
  网关管理令牌不下发浏览器（M1.4 的管理台页签即此模型）。控制面经 https 管理面访问时
  可配置客户端证书（`TT_GATEWAY_ADMIN_CLIENT_CERT/KEY/CA`）。
- 操作人优先 `X-TT-Admin-Actor`，其次证书主体，最后 `admin`，仅用于审计表 `audit_log`，
  不参与鉴权；`X-TT-Admin-Actor` 仍可被调用方伪造，可信度取决于控制面（见 §4）。
  代理必须显式 `proxy_set_header X-TT-Admin-Actor "";`：M7.1 实测漏配时客户端伪造值可透传并
  污染审计归因（鉴权不受影响，无管理令牌仍 401）。
- 管理令牌应为高熵随机串，并在反向代理上叠加来源网段与速率限制。

### 2.2 用户鉴权（协议面）

- 生产只接受 RS256：`decode_rs256` 强制 `alg=RS256`、必须有 `kid`，公钥来自控制面 JWKS
  （`app/auth.py:100`），从流程上排除 HS256/RS256 算法混淆。
- claims 校验：`iss`、`aud`、`type=access`、`iat` 不得晚于当前 +60s、`nbf`、`exp`，
  且 `exp-iat ≤ 900s`（`app/auth.py:143-164`）。
- 部门切换 `X-TT-Department` 必须命中令牌里的 `depts`，否则 403（`app/auth.py:_with_department_override`）。
- 令牌长度上限 8KB；`ttg_` 前缀分流到 API Key 校验（见 §2.9），JWT 与 Key 互不兼容。
- 开发态 HS256 仅在 `TT_GATEWAY_ENV != production` 且显式 `TT_GATEWAY_JWT_MODE=hs256-dev` 时可用，
  启动日志会打警告。

### 2.3 厂商密钥

- 主密钥为 base64 编码的 32 字节，AES-GCM，AAD 绑定密钥版本；`TT_GATEWAY_MASTER_KEYS`
  可保留历史版本以支持轮换（`app/secrets.py`）。
- 明文只在 `POST .../keys` 与 `rotate` 的响应里出现一次；列表与审计只存 `last4`。
- 错误信息统一过 `sanitize_error_message`，移除已知密钥字面量与 `authorization/x-api-key` 形态，
  并截断长度；适配器映射异常时把当前 provider key 传入脱敏（`app/adapters/litellm_adapter.py:403`）。
- 下游 `audit_log` 只记 before/after 的结构化字段（id、last4、状态），不含密文与明文。

### 2.4 不落 prompt/response

逐条审计 `logger.*`：均为 request_id、计数、错误类型、耗时；协议层没有把消息体写入日志的语句。
用量台账只有 token 计数、费用、模型与状态，没有内容字段（`app/db.py` 的 `usage_events`）。

### 2.5 SSRF / DNS

- provider `baseUrl` 创建时校验：仅 http/https、禁止 userinfo、禁止元数据主机名、按解析结果
  判断私网/链路本地/组播/保留地址；`allow_private` 需显式开启（内网部署才允许）。
- 每次请求前 `verify_target_url` 复检（`app/upstream.py:128`），覆盖 IPv4 映射 IPv6、十进制 IP
  等写法；失败映射为 `PROVIDER_UNAVAILABLE`，不回显解析细节。
- 假上游用例覆盖：169.254.169.254、IPv6 link-local、IPv4-mapped、file://、元数据域名、
  `2130706433` 等（`tests/test_hardening.py`）。

### 2.6 请求体上限（本轮修复）

- 契约 §4.4：32MB → 413。原实现只读 `Content-Length`，而 `Transfer-Encoding: chunked`
  没有该头，可直接绕过并在解析前把整个请求体读进内存。
- 现在写方法一旦带 `Transfer-Encoding` 就返回 413 + `Connection: close`（`app/main.py:130`），
  不再读取不可预估的正文；`tests/test_core.py` 增加裸 socket 用例锁定行为。
- 保留既有行为：`Content-Length` 超限时最多读 4× 上限再回 413，避免 Windows 上未读完的请求体
  触发 RST 导致客户端收不到错误（瞬时内存上限 128MB，见 §4）。

### 2.7 传输与响应头

- 所有响应加 `X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`；
  协议面另加 `Cache-Control: no-store` 与 `X-TT-Request-Id`（`app/main.py:154-160`）。
- 不启用 CORS（网关只服务桌面端与控制面，不是浏览器直连 API）；如未来需要，必须显式白名单。
- TLS/HSTS 由反向代理承担（见 `docs/deploy.md`）；网关自身默认监听回环。

### 2.8 生产启动门槛

`TT_GATEWAY_ENV=production` 时（`app/startup.py:92`）：

- 必须 `TT_GATEWAY_JWT_MODE=rs256`、`TT_GATEWAY_JWKS_URL`（https，本机回环除外）、
  `TT_GATEWAY_MASTER_KEY`、`TT_GATEWAY_ADMIN_TOKEN`；
- 配了控制面地址就必须有 `TT_GATEWAY_SERVICE_TOKEN`；
- 默认 `TT_GATEWAY_MTLS_MODE=proxy`，此时 `TT_GATEWAY_TRUSTED_PROXIES` 必须非空且可解析；
  非法模式/名单直接拒绝启动；
- 管理面限流默认 120 RPM；`mTLS=off` 或 RPM=0 仅打印告警，允许受信内网显式关闭；
- `TT_GATEWAY_CROSS_HOST=1` 时必须有 `TT_GATEWAY_DB_URL` 与 Sentinel/Redis 共享状态；
  Sentinel 的主节点名与地址列表必须成对出现，TLS 文件必须存在且 URL 为 `rediss://`；
- `allow_private` 仅告警（内网部署需要），但会把风险写进启动日志；
- `/docs`、`/redoc`、`/openapi.json` 关闭。

缺项或非法值时进程直接退出并点名变量，G5/G6 的启动用例分别验证这两类行为。

### 2.9 CLI API Key（M3）

- 格式 `ttg_<keyId12hex>_<secret43base64url>`，`secret` 为 32 字节随机数；库里只存
  `sha256(secret)`，不存明文、不做可逆加密（`app/api_keys_store.py`）。
- 校验失败统一 `AUTH_REQUIRED`（不存在 / 已吊销 / 已过期不区分），未知 keyId 也执行一次
  哈希比较，避免用错误类型或耗时枚举 Key 状态。
- 管理面 `POST/GET/DELETE /gateway/v1/admin/api-keys` 受 mTLS + 管理令牌保护；
  明文只在 201 响应出现一次，列表与审计不含明文与哈希（`tests/test_api_keys.py`、G7）。
- 绑定 `tenant/dept/sub`，角色固定 `member`；`X-TT-Department` 只能等于绑定部门，
  不能借 Key 切换到令牌里的其他部门。
- scopes 固定 `chat/embeddings/models/messages/gemini`，按端点校验；模型白名单匹配请求里的
  `model`（别名或具体模型），仍受部门策略约束；两者都不能后改，变更需吊销重签。
- 没有进程内缓存：每个请求查一次 SQLite 主键，吊销与过期从下一次请求立即生效；
  同机多 worker 共读同一 SQLite，同样无需失效传播（跨主机快照复制才需要事件）。
  用量按绑定主体记账并带 `apiKeyId`。

### 2.10 治理与审计（M4）

- **管理角色（M4.2）**：网关从 `X-TT-Admin-Roles` 读取角色，读动作允许
  admin/operator/auditor，写动作只允许 admin/operator（`app/roles.py`）。该头由控制面代理
  根据**服务端会话**填写，浏览器提交的角色字段不参与透传；因此角色边界实际由控制面的
  `require_gateway_staff` + JWT 保证（`tongtian-server/app/auth.py`）。
- 直连管理 API 时若不带角色头，按 `admin` 处理（`app/admin.py:65-69`）：共享管理令牌本身即 root。
  也就是说 M4 的角色隔离只约束"经控制面代理"的路径，直连网关的持有者仍等价于 admin；
  管理令牌的保管与网络边界仍是第一道防线。
- 角色变更只允许 admin 操作、禁止改自己（`SELF_ROLE_CHANGE`），角色写库后吊销该用户会话，
  旧令牌下一次请求即 401。角色变更本身写 `user.role` 审计。
- **审计哈希链（M4.3）**：每条管理写操作在同一事务内写 `audit_log`，并带上
  `prev_hash` 与本条 `entry_hash`；摘要覆盖 `prev_hash/at_ms/actor/actor_role/action/object/
  before_json/after_json`（`app/audit_chain.py`）。`/audit/verify` 从 genesis 逐条复算并返回
  断链位置；`/audit/export` 返回 NDJSON，响应头给出 `X-TT-Audit-Head` 与 `X-TT-Audit-Next`，
  便于外部留档后续接。存量行在启动时按 id 顺序回填（`backfill`）。
- 链式结构是**防篡改可发现**，不是防删除：能直接改 SQLite 文件的人可以整段重算。真正的
  不可抵赖依赖定期导出与审计外送；数据目录权限按 §4 的处置执行。
- 审计记录只含结构化摘要（id、last4、状态、配额数字），不含 prompt/response、密钥明文、
  管理令牌或哈希；`/audit` 读取不产生新审计（避免自激），可见性只限 admin/operator/auditor。
- **审计外送（M4.4）**：`audit_outbox` 与 `audit_log` 同事务入队，后台线程按
  `TT_GATEWAY_AUDIT_WEBHOOK_INTERVAL_SECONDS` 批量推送。签名材料为
  `timestamp + "." + body`，HMAC-SHA256 放在 `X-TT-Audit-Signature: sha256=...`，另带
  `X-TT-Audit-Timestamp` 与 `X-TT-Gateway-Id`，接收端可据此校验完整性与来源。
  2xx 记成功；4xx（408/429 除外）视为确定性拒绝并跳过，避免毒批次堵队列；5xx/429/网络错误
  按指数退避重试，上限 300s。配置 URL 时必须提供 ≥16 位 secret，否则拒绝启动（`app/config.py:141`）。
  交付语义是**至少一次**：崩溃重发可能产生重复，接收端需按事件 id 幂等去重；时间戳用于
  接收端做重放窗口校验，网关本身不缓存已发批次。
- **per-key 治理（M4.5/M5）**：`rateLimitRpm` 与 `quotaMonthlyTokens` 都允许 0 表示不限；
  RPM 令牌桶容量 `max(1, min(rpm, 60))`，超限 429 `RATE_LIMITED` + `Retry-After`；
  月度 token 配额用 `api_key_counters` 记录已结算量，Redis 记录在途预留，请求前按
  "prompt 估算 + max_tokens"预留、结束后先写 SQLite 再释放预留，超限 429
  `QUOTA_TOKENS_EXCEEDED`。与部门配额并行，取更严者。单 worker 的令牌桶与并发计数仍在
  进程内；多 worker 时必须启用 Redis，否则启动即拒绝（同 §4）。
- **轮换（M4.5）**：`rotate` 保持 `keyId` 不变，生成新 secret 并只在此次响应返回一次，库中
  仅更新 `sha256(secret)` 与 `rotatedAt`；旧 secret 下一次请求即 401，没有重叠窗口。
  scopes、模型白名单与绑定主体仍不可后改，需要变更时吊销重签。轮换与签发、吊销都写审计。

### 2.11 多 worker 与共享状态（M5）

- **边界**：M5 只解决**同一主机**的多个 worker 进程共享一份 SQLite 与一个 Redis。
  `gatewayId` 唯一性、审计哈希链与 SQLite 单写者语义不变；跨主机在 M6 通过 Postgres
  共享账本实现（见 §2.12），SQLite 仍只用于单机或单副本部署。
- **共享热状态**（`app/shared_state.py`）：管理面与 per-key 令牌桶、配额在途预留
  （`quota:res:*` / `quota:kres:*`）、并发计数（`quota:active:*`）都在 Redis；Lua 脚本把
  "检查 + 写入留 + INCR 并发"做成一次原子操作，桶时间取 Redis `TIME()`，不受 worker 时钟漂移影响。
- **失败关闭**：Redis 连接失败、命令超时或返回异常时，`consume`/`reserve` 抛
  `SharedStateError`，由限流与配额路径统一映射为 `503 SHARED_STATE_UNAVAILABLE`
  （OpenAI `api_error`、Anthropic `overloaded_error`、Gemini `UNAVAILABLE`）。宁可不服务，
  也不静默绕过限额；恢复后无需重启。
- **预留与崩溃**：已结算用量在 SQLite（权威、可重建），Redis 只记在途预留。结算顺序是
  先写 SQLite 再释放 Redis；崩溃留下的预留按 1 小时 TTL 回收，偏保守（可能少放行，不会超发）。
- **策略失效**：worker 拉到新 `policyVersion` 后先写共享 `policy_cache`，再向
  `{prefix}:policy:events` 发布租户；其他 worker 收到后只清内存缓存，下一请求从共享 SQLite
  读到新版本。Pub/Sub 丢失时回落 TTL + stale 宽限，语义是"尽快"不是强一致。
- **审计 outbox 租约**：两个 worker 用同一 SQLite，投递前在 `BEGIN IMMEDIATE` 内认领，
  `lease_owner = gatewayId:pid:uuid`、租约至少 60 秒；有效租约内其他 worker 跳过，
  进程崩溃后租约过期可被接管。交付仍是**至少一次**，接收端按 `(gatewayId, id)` 去重。
- **Redis 凭据**：`TT_GATEWAY_REDIS_URL` 只从环境变量读取，不写日志、不进健康响应；
  `health.sharedState` 只暴露 `mode / connected / workers / pid / policySubscriber / lastError`。
  生产要求 Redis 只监听回环/内网并启用 `requirepass` 或 ACL；同机多实例必须用不同
  `TT_GATEWAY_REDIS_PREFIX`，否则会互相 purge 令牌桶与并发计数。

### 2.12 跨主机多副本与 Redis 高可用（M6）

- **共享账本**：配置 `TT_GATEWAY_DB_URL` 后，`usage_events`、`api_keys`、`api_key_counters`、
  `audit_log`、`audit_outbox`、`policy_cache` 等落到 Postgres；配额已结算量、审计哈希链与
  outbox 因此跨副本一致。SQLite 与 Postgres 共用 `app/db.py` 方言层，SQL 占位符由适配器统一
  转换，避免两套实现漂移。
- **迁移并发**：每个新连接都会执行幂等迁移；Postgres 端先 `BEGIN`，再取
  `pg_advisory_xact_lock`，然后在锁内 `CREATE TABLE IF NOT EXISTS schema_version` 与补列。
  并发首次启动只应用一次；库中版本高于程序支持版本时不重放、不改写回。M7.3 实测到旧行为是
  懒连接（进程照常起来、首个请求才 500），M8.1 已把校验挪进启动门槛：`enforce_ledger()` 在
  lifespan 里先 `db.ensure()` 再放流量，连不上、缺 psycopg、迁移失败或版本过高一律拒绝启动，
  日志点名后端与两个版本号，错误消息只取驱动异常第一行并抹掉 `://user:pass@`、`password=`
  与配置的 DSN 原文（`app/startup.py:enforce_ledger`）。
- **TPM 限流**：主体桶容量取策略 `quota.tpm`，Key 桶取 `rateLimitTpm`；单次成本为
  `max(1, prompt 估算) + effective_max_output`，在途预留、结算退回未用部分。容量上限 60000，
  避免单个超大 `max_tokens` 把桶长期占满。Redis 故障时与 RPM 一样失败关闭。
- **部门级管理角色**：`operator` 的 `X-TT-Admin-Depts` / `X-TT-Admin-Tenant` 由控制面按
  服务端会话写入（与 M4 角色头同一信任模型），网关对 Key 列表/签发/轮换/吊销、配额与用量
  按部门 + 租户过滤；越权 403。provider 写与审计读仍要求全局 admin/auditor，避免部门
  管理员看到其他部门审计或改全局 provider。
- **Sentinel 与 TLS**：配置 `TT_GATEWAY_REDIS_SENTINEL_MASTER` + `_URLS` 后由 Sentinel
  发现主节点；主从切换后 redis-py 自动重连。`rediss://` 自动启用 TLS，启用 TLS 时 URL
  必须是 `rediss://`；支持自定义 CA 与双向认证。切片用例（`tests/test_m6_sentinel.py`）用
  真实 Redis 主从 + 假 Sentinel 验证发现协议；M7.2 已改用真 1 主 2 从 + 3 个真 Sentinel
  （quorum=2）演练杀主与 `docker pause` 阻塞：自动提升新主、进程不重启重连、限流桶随复制
  落到新主、阻塞期失败关闭 503。生产仍要求至少 3 个 Sentinel。
- **凭据边界**：DB DSN、Redis URL、密码与 Sentinel 密码只从环境变量注入；`health.database`
  只回 `backend / connected / schemaVersion`，`health.sharedState` 只回模式/连通性；
  G10 扫描两个副本的进程日志，确认没有连接串、密码、管理令牌、服务令牌、主密钥或 Key 明文；
  M7 三段演练各自复扫（代理与网关日志、健康页、容器 Args），并确认唯一提示词标记既不进日志
  也不进 `usage_events`/`audit_log`。注意 `docker inspect` 的 `.Config.Env` 仍能看到注入的
  口令与 DSN，宿主机上查看容器属于敏感操作。
- **部署边界**：各副本必须同一 `TT_GATEWAY_ID`（账本去重与审计归属），数据目录必须独立；
  `TT_GATEWAY_CROSS_HOST=1` 时启动自检强制 Postgres + 共享状态。仍假定单一 Postgres 主库
  与单一 Redis 主从（M7.2 演练的是单主 + 两副本 + Sentinel 自动切主，不含读写分离与分片）。

## 3. 本轮变更

| 变更 | 位置 | 验证 |
|---|---|---|
| 拒绝分块请求体（413） | `app/main.py:130` | `tests/test_core.py` 裸 socket 用例 |
| 启动期预热 LiteLLM（去除首请求 0.6–1s 冷启动，间接降低超时误判） | `app/adapters/litellm_adapter.py:66`，`app/main.py:84` | `scripts/loadtest.py` 非流式 p95 3308ms → 325ms |
| M2 管理面 mTLS：受信代理头、`MTLS_REQUIRED`、禁用 proxy headers | `app/mtls.py`、`app/admin.py:42`、`app/main.py:315`、`Dockerfile` | `tests/test_m2_admin.py`、G6 |
| M2 管理面限流：按 DN/IP 令牌桶、`Retry-After` | `app/ratelimit.py:15`、`app/admin.py:42` | `tests/test_m2_admin.py`、G6 |
| M2 控制面管理代理客户端证书 | `tongtian-server/app/gateway_admin.py` | `tongtian-server/tests/test_gateway_admin.py` |
| M3 CLI API Key：签发/列表/吊销、`sha256` 存储、无缓存吊销 | `app/api_keys_store.py`、`app/admin.py`、`app/auth.py` | `tests/test_api_keys.py`、G7 |
| M3 端点级 scopes 与模型白名单、绑定部门 | `app/protocols/common.py`、`app/routing.py`、三协议路由 | `tests/test_api_keys.py`、G7 |
| M3 控制面 `/ui/` 签发/吊销与一次性明文 | `tongtian-server/app/web/index.html`、`app.js` | 浏览器实测（签发 → 复制 → 吊销），通用代理 `tests/test_gateway_admin.py` |
| M4 管理角色与代理读写分离（`X-TT-Admin-Roles`） | `app/roles.py`、`app/admin.py:65`、`tongtian-server/app/gateway_admin.py` | `tests/test_m4_roles.py`、`tongtian-server/tests/test_m4_audit.py`、G8 |
| M4 控制面角色变更（PATCH role、禁止自改、吊销会话） | `tongtian-server/app/auth.py` | `tongtian-server/tests/test_m4_audit.py`、G8 |
| M4 审计哈希链、校验、NDJSON 导出 | `app/audit_chain.py`、`app/audit.py`、`app/admin.py` | `tests/test_m4_audit.py`、G8 |
| M4 审计外送：outbox 同事务、HMAC 签名、退避 | `app/audit_outbox.py`、`app/db.py`、`app/config.py:139` | `tests/test_m4_webhook.py`、G8 |
| M4 per-key RPM/月度配额、轮换 | `app/api_keys_store.py`、`app/api_key_limits.py`、`app/quota_sqlite.py` | `tests/test_m4_governance.py`、G8 |
| M5 多进程 SQLite 安全：WAL 重试、迁移内重读版本 | `app/db.py` | `tests/test_m5_schema.py` |
| M5 Redis 共享状态：Lua 令牌桶/预留/并发、失败关闭 | `app/shared_state.py`、`app/ratelimit.py`、`app/api_key_limits.py`、`app/quota_sqlite.py` | `tests/test_m5_shared.py`、G9 |
| M5 策略 Pub/Sub 失效通知 | `app/policy_client.py`、`app/policy.py`、`app/main.py` | `tests/test_m5_shared.py`、G9 |
| M5 审计 outbox 数据库租约 | `app/db.py`、`app/audit_outbox.py` | `tests/test_m5_outbox.py`、G9 |
| M5 多 worker 启动门槛与健康面 | `app/config.py`、`app/startup.py`、`app/admin.py`、`app/main.py` | G9 启动用例 |
| M6 数据库方言层与 Postgres 迁移（advisory lock、版本拒绝） | `app/db.py`、`app/config.py`、`app/startup.py` | `tests/test_m6_postgres.py`、G10 |
| M6 跨主机共享账本（配额/审计链/outbox）与连接串边界 | `app/db.py`、`app/admin.py` | `tests/test_m6_postgres.py`、G10 日志扫描 |
| M6 TPM 限流（主体/Key 两档、预留与退回） | `app/shared_state.py`、`app/api_key_limits.py`、`app/quota_sqlite.py` | `tests/test_m6_tpm.py`、G10 |
| M6 部门级管理角色（`X-TT-Admin-Depts`/`Tenant`） | `app/roles.py`、`app/admin.py` | `tests/test_m6_admin_scope.py`、G10 |
| M6 Redis Sentinel 发现与 `rediss://` TLS | `app/shared_state.py`、`app/config.py` | `tests/test_m6_sentinel.py`、G10 |
| M7 部署演练（不改产品代码）：真实 nginx TLS/mTLS、真三节点 Sentinel、真实 Postgres 迁移与备份恢复 | `scripts/m7_drill.py` | `docs/m7-drill.md`（52/52：TLS 19 步 + Sentinel 17 步 + Postgres 16 步） |
| M8.1 账本启动门槛：lifespan 内先连库+迁移再放流量，错误消息脱敏 | `app/startup.py:160`、`app/main.py:61` | `tests/test_m8_startup_ledger.py`（14 项，含真实 uvicorn 进程拒起）、G5、M7.3 重跑 |

## 4. 残余风险与处置建议

| 风险 | 影响 | 处置 |
|---|---|---|
| LiteLLM 默认跟随 3xx 重定向 | 已通过 SSRF 校验的地址可能被重定向到内网其他地址 | 出口防火墙按 provider 域名/IP 白名单；不要对不可信上游开启私网；后续版本评估关闭 redirect 或自行处理 |
| DNS rebinding（校验与连接两次解析） | 校验时公网、连接时变成内网 | 同上，出口白名单是主防线；内网部署下影响有限 |
| 策略过期仍放行 | 控制面不可达时沿用最后一份策略（响应带 `X-TT-Policy-Stale: 1`） | 设计如此，保证可用性；控制面恢复后自动刷新；需要强一致时缩短策略拉取间隔 |
| 共享限流依赖 Redis | Redis 故障时管理面/协议面返回 503（失败关闭），可用性下降 | 部署 Redis 高可用或接受短时不可用；失败关闭优于静默放开；代理层仍叠加速率限制 |
| Redis 单实例 | 单实例故障丢失桶与在途预留（M6 已支持 Sentinel 与 TLS，但不含 Cluster） | 热状态可重建：桶从空开始、预留等 TTL；生产部署 Sentinel（≥3）或托管高可用；TLS 用 `rediss://` |
| Redis Cluster / 读写分离 | 未接入；跨地域延迟与分片不在 M6 | 单主从容量与延迟满足前不引入 Cluster；扩容先按 `docs/deploy.md §11` 压测 |
| Sentinel 客户端鉴权与 Sentinel TLS 未演练 | M7.2 用真三节点跑通发现与切主，但 Sentinel 本身未配口令/TLS（`TT_GATEWAY_REDIS_SENTINEL_PASSWORD` 只有单测覆盖） | 生产按 `docs/deploy.md §3` 配 `TT_GATEWAY_REDIS_SENTINEL_PASSWORD`，Sentinel 只监听内网并加 ACL；Redis/Sentinel TLS 用 `rediss://` + `TT_GATEWAY_REDIS_TLS_CA` |
| 审计链全局单链 | 跨主机写入都串行追加，大规模写会放大等待 | 审计只记录管理写操作，量级有限；需要更高吞吐时评估分片链（会改变导出语义） |
| TPM 输入为本地估算 | 与上游 tokenizer 存在差异 | 预留偏保守、结算退回未用部分；不因估算差异导致超发 |
| 部门角色只覆盖网关管理面 | 控制面 UI 的部门筛选留待后续 | 控制面继续按会话角色鉴权；网关侧越权已 403 |
| Postgres 备份只演练了同实例库到库 | DSN 含密码，库权限即数据权限；M7.3 验证了 `pg_dump -Fc` → `pg_restore --exit-on-error` 与最小权限角色，但没验证异地/加密备份、流复制与 PITR | 备份文件按密钥级别保护（权限 600、加密后异地保存）；多可用区部署上线前自行做一次主从切换与时间点恢复；库账号保持 `NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION` 并 `REVOKE CONNECT ON postgres` |
| 管理面 mTLS 依赖部署边界 | 网关若被直接暴露，客户端可绕过反向代理 | 网关只监听回环/内网；防火墙限制来源；`TT_GATEWAY_TRUSTED_PROXIES` 只填 nginx 地址 |
| 受信代理头可被代理侧伪造 | 能访问代理配置的人可伪造证书主体 | 代理配置与证书私钥按运维权限管理；审计主体只作归因，不作鉴权 |
| 证书吊销/轮换流程未在真实 CA 上走过 | M7.1 用演练 CA + CRL 验证了 nginx 会拒吊销/过期/未知签发者证书，且拒绝请求不进应用；但生产 CA 的签发、CRL 更新频率与轮换顺序未联调 | 上线前用生产 CA 与真实客户端证书复跑 `scripts/m7_drill.py --only tls`；CRL 更新与 `ssl_crl` 重载纳入运维日历，轮换顺序按 `docs/deploy.md §6` |
| 代理漏配 `proxy_set_header` 时审计归因可被污染 | M7.1 实测：nginx 若不清空 `X-TT-Admin-Actor`，客户端伪造值可透传并写进审计 `actor`；管理令牌门槛仍在，无令牌仍 401，鉴权不受影响 | 部署清单逐条核对 `docs/deploy.md §6` 的三行 `proxy_set_header`（Actor 清空 + Verify/DN 由代理写入）；审计只作归因，不作授权依据 |
| 运行期账本失联只体现在读写与就绪上 | M8.1 已让进程启动即校验账本（连不上/版本过高直接拒起），但**启动之后**库宕机不会被进程自身发现：`/health` 不连库仍回 200，账本请求回 500 | 就绪/存活探针继续用 `GET /gateway/v1/admin/health`（`database.connected=false` 或 500 即摘流量）；对库做连接告警；如需“库宕机即自杀”要另加看门狗，会牺牲可用性，暂不引入 |
| `X-TT-Admin-Actor` 可伪造 | 审计归属不可信 | 仅直连管理 API 时存在；控制面代理会覆盖该头。M2 已增加证书主体回落，但仍优先该头；M7.1 已验证代理清空该头后可用 `$ssl_client_s_dn` 归因（代理漏配时的残留见“代理漏配 `proxy_set_header` 时审计归因可被污染”一行） |
| 413 前最多读取 4× 上限 | 瞬时内存 128MB/请求 | 反向代理先做 `client_max_body_size`；单机部署下可接受 |
| 单库 SQLite（单机多 worker） | 无跨主机横向扩展；文件权限即数据权限；写事务串行 | 数据目录仅服务账号可读写；备份见 `docs/deploy.md`；跨主机改用 M6 的 Postgres 共享账本 |
| API Key 无缓存吊销 | 引入跨主机共享缓存后，吊销可能存在传播窗口 | 同机多 worker 共读同一 SQLite，每请求查库即生效；跨主机共享缓存时补吊销事件或短 TTL |
| Key 权限不可后改 | scopes/白名单变更必须吊销重签 | 设计如此，避免权限静默扩大；操作留审计 |
| 共享热状态依赖 Redis | 多 worker 下 Redis 故障会拒绝限流/配额请求；Redis 丢数据会重置桶与在途预留 | 失败关闭（503）；预留按 TTL 保守回收，不超发；Redis 只监听回环/内网并配 `requirepass`/ACL |
| 策略传播非强一致 | Pub/Sub 丢消息时其他 worker 最迟一个 TTL 才切版 | 设计如此；需要更快收敛可缩短 TTL；控制面不可达仍有 stale 宽限 |
| 多 worker 共享前缀 | 同机多套实例若共用 `TT_GATEWAY_REDIS_PREFIX`，会互相清空桶/并发计数 | 每个实例独立前缀；已列入 `docs/deploy.md §13.1` 上线前检查表 |
| 审计链只能发现篡改 | 直接改库并重算整段可绕过校验 | 依赖数据目录权限 + 定期导出/外送留档；导出文件异地保存 |
| 审计外送至少一次 | 发送成功后、标记成功前崩溃仍会重发；租约只防多 worker 同时发同一批 | 接收端按 `(gatewayId, id)` 幂等去重；批次本身不保证严格顺序 |
| 审计外送无重放保护（网关侧） | 持有 secret 者可重放签名请求 | 接收端校验 `X-TT-Audit-Timestamp` 时间窗并记录已收事件 id |
| 直连管理 API 不受角色约束 | 持有共享管理令牌的直连调用等价 admin | 管理令牌只发控制面；反向代理限制来源网段；M4 角色仅覆盖控制面代理路径 |
| 轮换无重叠窗口 | 新 secret 生效即旧 secret 401，客户端需同步更新 | 设计如此，避免旧密长期可用；多客户端可先签发新 Key 并行，再吊销旧 Key |
| 依赖无自动漏洞扫描 | 供应链风险 | 版本全钉；升级 LiteLLM 前先跑 `scripts/spike_litellm.py`；接入镜像扫描后补 `pip-audit` |
| 协议面未启用 mTLS | 内网横向移动 | 协议面按 RS256 JWT + 策略鉴权；管理面已由 M2 覆盖；确有需要再评估协议面证书 |

## 5. 依赖与版本

`requirements.txt` 全量钉死：fastapi 0.136.3、uvicorn 0.49.0、pydantic 2.14.1、httpx 0.28.1、
litellm 1.106.6、cryptography 46.0.5、mcp 2.3.0、aiohttp 3.14.5。
本机为离线/受限网络环境，未运行 `pip-audit`；M1 以"钉版本 + 升级前跑 spike + 镜像扫描"作为补偿控制。
cryptography 与 LiteLLM 是主要攻击面，升级必须单独回归 `test_adapters` / `test_litellm_adapter` /
`test_hardening` 与 G3/G4。
