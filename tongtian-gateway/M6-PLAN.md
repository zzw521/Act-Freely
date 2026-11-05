# M6 实施计划：跨主机多副本、TPM、部门角色与 Redis 高可用

- 状态：**已完成**（M6.1–M6.8；G10 通过，见 [docs/m6-acceptance.md](docs/m6-acceptance.md)）
- 上游依据：[M5-PLAN.md](M5-PLAN.md) §3/§4、[CONTRACT.md](CONTRACT.md) §7.3/§7.4/§9、M4-PLAN D12「部门级管理角色」、CONTRACT §7.1「`tpm` 暂未实现」
- 范围：Postgres 作为跨主机共享数据库；TPM 限流；部门级管理角色；Redis Sentinel/TLS；G10 验收与文档
- 不在范围：跨地域双活、Postgres 读写分离/分片、Redis Cluster、SSO/计费、审计防删除

---

## 0. M6 的完成定义（一句话）

同一套网关可以跨主机起多个副本：它们共享 Postgres 账本与 Redis 热状态，月度配额、并发、TPM、审计链与 outbox 在副本间保持一致；控制面可以让运维只管理本部门；Redis 主节点故障后经 Sentinel 自动切换；不配置 Postgres/Sentinel 的单机部署行为与 M5 完全一致。

---

## 1. 锁定的决策

| # | 决策 | 理由 | 备选（不采用） |
| --- | --- | --- | --- |
| D1 | M6 分四条独立线：Postgres 跨主机、TPM、部门角色、Redis HA；每条线都可单独开启 | 任何一条出问题都不应拖垮其他线；单机部署零改动 | 一次性大版本切换（回归面不可控） |
| D2 | Postgres 通过 `TT_GATEWAY_DB_URL` 启用（`postgresql://`/`postgres://`）；未配置继续 SQLite | 兼容现有单机；跨主机必须共享数据库 | 用 `TT_GATEWAY_DB_BACKEND` 再选路径（多一层无意义配置） |
| D3 | 存储抽象停在 DB-API 包装层：统一 `?` 占位符、命名字段行、`RETURNING id`、`ON CONFLICT DO NOTHING`；不引入 ORM | 现有 SQL 与测试可复用；ORM 会重写全部存储代码 | SQLAlchemy Core/YAML 迁移（学习与迁移成本高） |
| D4 | 审计链跨主机用 Postgres 事务级 advisory lock 串行追加；SQLite 继续 `BEGIN IMMEDIATE` | 哈希链必须单写者；advisory lock 与事务同生共死 | 应用层 Redlock（多一个依赖且时钟敏感） |
| D5 | outbox 认领在 Postgres 用 `SELECT ... FOR UPDATE SKIP LOCKED`；SQLite 保留现有租约 | 两种数据库各用最稳的原子认领方式 | 统一租约（Postgres 下多一次写放大） |
| D6 | 已结算用量、Key、provider、策略缓存、审计在数据库；在途预留/并发/令牌桶在 Redis | 账本持久可重建，热状态易失可回收 | 把账本搬 Redis（丢持久性） |
| D7 | TPM 是「按分钟补充的 token 桶」：成本 = 输入估算 + 本次输出上限；结算按实际 `total_tokens` 退回未用部分 | 与月度预留口径一致；避免长输出请求绕过分钟限额 | 只按已完成 usage 计（并发突发可穿透） |
| D8 | TPM 作用域：主体 `tenant:dept:sub` 用策略 `tpm`；API Key 另有 `rateLimitTpm`；两者同时生效 | 契约 §7.1 写作“用户/Key”；两级都要能收紧 | 只做 Key 级（JWT 用户没有分钟限额） |
| D9 | TPM 共享状态：多 worker/多主机走 Redis；单 worker 无 Redis 用进程内；Redis 故障 fail-closed | 与 M5 限流/配额同一可用性口径 | TPM fail-open（多副本下等于没有限额） |
| D10 | 部门角色：控制面按操作者部门成员关系下发 `X-TT-Admin-Depts`；`admin` 始终全局；`operator` 带头即只能看/管本部门 Key、配额与用量；`auditor` 保持全局只读 | 部门管理员是“运维收窄”，不是新角色；审计链没有部门字段，硬切会误伤 | 新增 `dept_operator` 角色（控制面/UI/JWT 全链路都要改） |
| D11 | 部门范围只收窄身份、配额、用量；provider 与全局健康仍属平台级，scoped operator 只读 | provider 密钥是全局资源，按部门切会制造共享密钥的越权面 | 每个 provider 加部门归属（M6 外的新模型） |
| D12 | Redis HA：`TT_GATEWAY_REDIS_SENTINEL_MASTER` + `TT_GATEWAY_REDIS_SENTINEL_URLS` 启用 Sentinel；`rediss://`/`TT_GATEWAY_REDIS_TLS_*` 启用 TLS；连接串与密码不落日志 | Sentinel 是 Redis 最常用的 HA 形态；TLS 是托管 Redis 的硬要求 | Redis Cluster（键空间与 Lua 需要重做） |
| D13 | 健康页新增 `database` 与 `sharedState.mode=single|sentinel`、`sharedState.tls`；只暴露模式/连通性，不回显 DSN/URL/密码 | 运维需要定位，但不能泄露凭据 | 回显完整 DSN（泄露密码） |
| D14 | G10 用两个独立网关进程（不同端口与本地数据目录，模拟两台主机）共享 Postgres+Redis 验收 | 本机没有第二台主机；进程级隔离已能覆盖跨主机并发语义 | 只跑单进程（无法发现跨进程竞争） |

### 1.1 Postgres 并发语义

| 场景 | SQLite（现状） | Postgres（M6） |
| --- | --- | --- |
| 迁移 | `BEGIN IMMEDIATE` 后重读版本 | `pg_advisory_xact_lock(<schema>)` 后重读版本 |
| 审计追加 | `BEGIN IMMEDIATE` 串行 | `pg_advisory_xact_lock(<audit>)` 串行，同一事务内读链头 |
| outbox 认领 | `UPDATE ... WHERE id IN (...)` + 租约 | `SELECT ... FOR UPDATE SKIP LOCKED` + 租约字段 |
| 配额结算 | `ON CONFLICT ... DO UPDATE` | 同一语法 |
| Key/provider 冲突 | 捕获 `sqlite3.IntegrityError` | 捕获 `psycopg.errors.UniqueViolation`，统一成 `db.is_integrity_error` |
| 用量聚合日期 | `strftime(...)` | `to_char(to_timestamp(...))`，由方言提供表达式 |

### 1.2 TPM 语义

1. 请求进入 `begin_chat` / `begin_embedding` 时，先算 `cost = max(1, prompt_estimate) + effective_max_output`。
2. 主体桶：`tpm = limits.tpm`；Key 桶：`tpm = apiKey.rateLimitTpm`；0/缺失表示不限。
3. 任一桶不足 → `429 RATE_LIMITED`，`Retry-After = ceil((cost - available) / (tpm / 60))`。
4. 结算：把 `cost - usage.total_tokens` 退回桶（不超过桶容量）；实际超出预留只记日志，不阻断回包。
5. 桶容量 = `max(1, min(tpm, 60000))`；单请求成本大于容量时直接拒绝（该请求本身超出分钟上限）。

### 1.3 部门范围

- 头：`X-TT-Admin-Depts: dept-sales,dept-eng`，逗号分隔，忽略空白与非法项；空头等同未下发。
  控制面同时下发 `X-TT-Admin-Tenant`（操作者组织），避免不同租户的同名部门互相越界；
  直连脚本只给部门头时退化为按部门名匹配。
- 控制面只在 `role=operator` 且成员部门非空时下发；`admin` 不下发（全局），`auditor` 不下发（全局只读）。
- 网关把范围放到 `request.state.admin_depts`；`None` 表示全局。
- 收窄点：
  - `GET /api-keys` 只返回范围内部门；`POST /api-keys` 的 `dept` 必须在范围内。
  - `rotate/revoke` 先读 Key 归属部门，越界 `403 FORBIDDEN`。
  - `GET /quotas`、`PATCH /quotas/{principal}` 只允许范围内部门；`sub='*'` 视为该部门级。
  - `GET /usage` 必须带范围内 `dept`（或用 `dept` 过滤后返回）；未带且范围多部门 → `400 INVALID_REQUEST` 要求指定部门。
  - provider 写、审计读取/导出、全局健康：scoped operator 只读或拒绝写。

### 1.4 Redis HA 配置

| 变量 | 说明 |
| --- | --- |
| `TT_GATEWAY_REDIS_URL` | 单实例或 `rediss://`；未启用 Sentinel 时必填 |
| `TT_GATEWAY_REDIS_SENTINEL_MASTER` | Sentinel 主节点名；配置后启用 Sentinel 模式 |
| `TT_GATEWAY_REDIS_SENTINEL_URLS` | `host:port,host:port`；Sentinel 地址，不写日志 |
| `TT_GATEWAY_REDIS_SENTINEL_PASSWORD` | Sentinel 自身密码（可选） |
| `TT_GATEWAY_REDIS_PASSWORD` | Redis 数据节点密码（可选；URL 中已有密码时以 URL 为准） |
| `TT_GATEWAY_REDIS_DB` | 数据库编号，默认 0 |
| `TT_GATEWAY_REDIS_TLS` | `1` 时 Sentinel/数据连接使用 TLS |
| `TT_GATEWAY_REDIS_TLS_CA` / `CERT` / `KEY` | 可选 CA 与客户端证书 |

Sentinel 模式下 `TT_GATEWAY_REDIS_URL` 可留空；仍要求 `TT_GATEWAY_WORKERS>1` 或跨主机部署时共享状态可用。

---

## 2. 里程碑与顺序

| 里程碑 | 内容 | 完成判据 |
| --- | --- | --- |
| M6.1 | 计划与契约 | 本文件、CONTRACT §7.5/§7.6/§9.4/§10 与实现一致 |
| M6.2 | 数据库方言层与 SQLite 回归 | 现有 G5–G9 全绿；SQL 改写成双方言可执行 |
| M6.3 | Postgres 后端与跨主机语义 | Postgres 容器内建表/迁移/重启幂等；两进程并发审计链无断点、outbox 不重复 |
| M6.4 | TPM 限流 | 单进程/Redis 两种模式；跨进程共享桶；结算退回；429 带 Retry-After |
| M6.5 | 部门级管理角色 | 控制面下发部门头；网关越界 403；列表/用量/配额按部门过滤 |
| M6.6 | Redis Sentinel/TLS | Sentinel 客户端、健康披露、主从切换后恢复；TLS 配置解析与连通性 |
| M6.7 | G10 验收 | `scripts/m6_acceptance.py` 生成 `docs/m6-acceptance.md` |
| M6.8 | 文档与回归 | README/deploy/security-review 更新跨主机口径；G5–G10 全绿 |

---

## 3. 验收（G10）

1. **Postgres 迁移**：空库启动两个进程，只应用一次 schema；重启不重复；版本高于程序时拒绝启动。
2. **跨进程配额**：两个进程共享一份月度计数；A 用掉一半后 B 看到剩余量；并发预留不超发。
3. **跨进程审计链**：两进程并发写审计，`audit/verify` 无断点；`audit/export` 可按 id 连续导出。
4. **outbox 租约**：两进程同时抢同一批事件，接收端只收到一份；崩溃租约过期后可被另一进程接手。
5. **TPM**：两个进程共用一个主体桶，A 打满后 B 立即 429；结算后未用部分可被后续请求使用；Key 级 `rateLimitTpm` 独立生效。
6. **部门角色**：scoped operator 只能列出/签发/轮换/吊销本部门 Key，只能调整本部门配额；越界 403；admin 不受限。
7. **Redis Sentinel**：杀掉主节点后 Sentinel 提升副本，两个进程在重连窗口内恢复共享限流与配额；健康页 `mode=sentinel`、`connected=true`。
8. **回归**：G5–G9 全部通过；单机 SQLite + 无 Redis 的行为不变。

---

## 4. 残余与移交

- **跨主机仍假定单一 Postgres 主库**：读写分离、分片、跨地域延迟不在 M6。
- **Redis Cluster 未支持**：Lua 键空间与 Pub/Sub 需要重做；M6 只支持单实例与 Sentinel。
- **审计链是全局单链**：跨主机串行化会放大大规模写入的等待；需要分片链时另立里程碑。
- **部门角色只覆盖网关管理面**：控制面 UI 的部门筛选、Key 绑定的“当前部门”选择仍由控制面后续补齐。
- **TPM 是估算口径**：输入 token 用本地估算器，输出按上限预留；与上游 tokenizer 的差异由结算退回吸收，不承诺逐 token 精确。
- **outbox 仍是至少一次**：控制面按 `(gatewayId, requestId)` 幂等；接收端必须保留去重窗口。
