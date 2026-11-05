# M5 实施计划：多 worker 与共享状态

- 状态：**已完成**（M5.1–M5.8；G9 16/16，G5–G9 回归全绿，报告见 [docs/m5-acceptance.md](docs/m5-acceptance.md)）
- 上游依据：[M4-PLAN.md](M4-PLAN.md) §3「残余与移交」、[CONTRACT.md](CONTRACT.md) §7.3（配额存储）、§9（管理 API）、M2/M3/M4 的“单副本”记账
- 范围：同一主机内多个 gateway worker 进程共享一份 SQLite；Redis 承载管理面/per-key 限流、配额在途预留与并发计数、策略失效广播；审计 outbox 用数据库租约避免重复投递；启动门槛、健康页、G9 验收与文档
- 不在范围：跨主机多副本（需要 Postgres 等共享数据库）、Redis Cluster/哨兵、SSO/计费、部门级管理角色、审计防删除

---

## 0. M5 的完成定义（一句话）

同一主机上可以起多个 gateway worker：它们共享一份 SQLite 账本与配置，限流、配额在途预留/并发和策略缓存失效通过 Redis 保持一致；审计外送由数据库租约保证同一事件只被一个 worker 投递；单 worker 不配 Redis 时行为与 M1–M4 完全一致。

---

## 1. M5 锁定的决策

| # | 决策 | 理由 | 备选（不采用） |
| --- | --- | --- | --- |
| D1 | “多副本”在本里程碑限定为**同主机多 worker**：所有进程使用同一个 `TT_GATEWAY_DATA_DIR` 与 `gateway.db` | SQLite 支持 WAL 多进程读写；跨主机共享 SQLite 不可行，必须先换 Postgres | 直接上 Postgres（存储层重写，超出本里程碑） |
| D2 | Redis 可选：默认单 worker 不配置；`TT_GATEWAY_WORKERS>1` 时**必须**配置 `TT_GATEWAY_REDIS_URL` | 保持现有部署零改动；多 worker 没有共享计数等于把限额按进程放大 | 强制所有部署上 Redis（单机小部署成本过高） |
| D3 | 管理面限流与 per-key RPM 都用 Redis 令牌桶；桶时间取 **Redis `TIME`**，桶容量仍为 `max(1, min(rpm, 60))` | 多 worker 不能各用本机时钟；Redis 单点时间保证补充速率一致 | 各 worker 本地时钟（时钟漂移导致桶速率不一致） |
| D4 | 配额“已结算”仍在 SQLite（`quota_counters` / `api_key_counters`）；“在途预留”和“并发数”放 Redis | 账本必须持久且可重建；在途状态本来就是易失的 | 把已用量也搬 Redis（丢失持久性） |
| D5 | 预留校验顺序：先读 SQLite 已用量，再在 Redis Lua 内原子检查 `used + reserved + tokens` 并递增预留；结算先写 SQLite 再释放 Redis | SQLite 先写会让并发窗口短暂“多算预留”，只会更保守，不会超发 | 先释放 Redis 再写 SQLite（可能超发） |
| D6 | Redis 故障**fail-closed**：限流与配额路径返回 `503 SHARED_STATE_UNAVAILABLE` | 配额/限流是治理边界，不能因共享状态故障静默放开 | fail-open（多 worker 下等于没有限额） |
| D7 | 审计 outbox 用 SQLite 租约（`lease_owner` + `leased_until_ms`）而不是 Redis 锁 | outbox 与审计同库，`BEGIN IMMEDIATE` 本身就能原子认领；少一个依赖 | Redis SETNX 锁（多一层失效与续租逻辑） |
| D8 | 策略失效走 Redis Pub/Sub：某 worker 拉到新 `policyVersion` 并写库后广播租户，其他 worker 清内存缓存，下一次请求从共享 SQLite 读新版本 | 多 worker 不再各自等满 TTL；控制面不可达时仍退回 TTL + stale 宽限 | 轮询 SQLite（每请求多一次读）；缩短 TTL（放大控制面压力） |
| D9 | 同一实例的 worker 共享 `TT_GATEWAY_ID`；outbox 租约 owner 追加 pid，便于区分进程 | 用量幂等键是 `(gatewayId, requestId)`，同一实例不应换 id | 每个 worker 一个 gatewayId（用量归属被打散） |
| D10 | 用量上报器仍每 worker 一个后台线程；控制面按 `(gatewayId, requestId)` 幂等 | 批内事件来自共享 SQLite，重复上报由控制面去重；无需 Redis 队列 | 全局单上报器（需要 leader 选举，收益低） |
| D11 | Redis 连接串只从环境变量读取，**不写日志**；健康页只暴露模式与连通性，不回显 URL | URL 可能含密码；沿用“密钥不落日志”约束 | 健康页回显 URL（泄露凭据） |
| D12 | 单 worker + 无 Redis 时走原进程内实现，行为与 M1–M4 一致 | 回归面最小；现有测试与验收无需 Redis | 全部路径强制 Redis（离线环境不可用） |

### 1.1 Redis 键空间

| 键 | 类型 | 说明 |
| --- | --- | --- |
| `{prefix}:rl:admin:<rateKey>` | hash | 管理面桶：`tokens` / `ts`，PEXPIRE 600s |
| `{prefix}:rl:key:<keyId>` | hash | per-key RPM 桶，同上 |
| `{prefix}:quota:res:<tenant>:<dept>:<sub>:<period>` | hash | 在途预留：字段为 `reservationId`，`__total` 为合计；TTL 1h |
| `{prefix}:quota:active:<tenant>:<dept>:<sub>` | string | 并发计数，TTL 1h，归零即删 |
| `{prefix}:quota:kres:<keyId>:<period>` | hash | per-key 在途预留，结构同上 |
| `{prefix}:policy:events` | pub/sub | 消息为租户名或 `*`，表示内存策略缓存失效 |

`prefix` 来自 `TT_GATEWAY_REDIS_PREFIX`（默认 `ttgw`）；同机多实例共用 Redis 时必须改成不同前缀。

### 1.2 预留与释放语义

1. `reserve`：读 SQLite 已用量 → Redis Lua 原子检查部门配额、Key 配额、并发上限 → 写预留字段并 `INCR` 并发。
2. `settle`：先按实际用量更新 SQLite 计数 → Redis Lua 删除预留字段、回退 `__total`、`DECR` 并发（下限 0）。
3. 进程崩溃：预留与并发键在 1h 后过期，最多让配额保守 1h；已结算数据不受影响。
4. 结算 SQLite 成功但 Redis 释放失败：仅记日志并暴露到健康页，预留等 TTL 回收（保守，不超发）。

### 1.3 启动门槛

- `TT_GATEWAY_WORKERS` 默认 `1`（1–64）。
- `TT_GATEWAY_WORKERS>1` 且未配置 `TT_GATEWAY_REDIS_URL` → 拒绝启动并点名变量。
- 配置了 Redis 但启动时 PING 失败 → 拒绝启动（不静默退回本机限流）。
- `TT_GATEWAY_REDIS_TIMEOUT_MS` 默认 `500`（100–5000），连接/读写超时都用它。
- 迁移在 `BEGIN IMMEDIATE` 后重新读取 schema 版本，避免两个 worker 同时迁移。

---

## 2. 里程碑与顺序

| 里程碑 | 内容 | 完成判据 |
| --- | --- | --- |
| M5.1 | 计划与契约 | M5-PLAN、CONTRACT §7.3/§9/§16 一致 |
| M5.2 | 多进程 SQLite 安全 | 两个进程同时启动不重复迁移；健康页可区分 worker |
| M5.3 | Redis 共享状态层 | 令牌桶/配额 Lua、PING、健康状态、Pub/Sub 可独立测试 |
| M5.4 | 共享限流与配额接线 | 两个 worker 共用管理 RPM、per-key RPM 与月度配额；Redis 故障 503 |
| M5.5 | 审计 outbox 租约 | 两个 worker 同库投递同一事件不重复；崩溃后租约过期可重投 |
| M5.6 | 启动与策略失效 | workers>1 强制 Redis；策略新版本经 Pub/Sub 立即传播；Docker 支持 workers |
| M5.7 | 测试与 G9 | `tests/test_m5_shared.py`、`test_m5_outbox.py` 全过；`scripts/m5_acceptance.py` 生成 `docs/m5-acceptance.md` |
| M5.8 | 文档与回归 | G5–G9 全绿；README/deploy/security-review 更新单副本口径 |

## 3. 验收结果（2026-10-31）

- `tests/test_m5_schema.py` 9/9、`tests/test_m5_shared.py` 19/19、`tests/test_m5_outbox.py` 7/7、
  `tests/test_m4_governance.py` 30/30。
- G9 16/16：双 worker 共享健康面、per-key RPM/月配额、并发计数、`CLIENT PAUSE` 失败关闭与恢复、
  TTL=300s 的 worker 经 Pub/Sub 立即切策略版本、审计链两端一致与 outbox 恰好一次、
  管理面令牌桶共享、`workers>1` 缺 Redis/连不上拒绝启动。
- 文档回填：`README.md`、`docs/deploy.md`、`docs/security-review.md`、`CONTRACT.md` 已把
  “单副本”口径改为“同机多 worker + Redis”，并保留跨主机边界说明。

## 4. 残余与移交

- **跨主机多副本**：仍需要 Postgres 或等价共享数据库；M5 只解决同机多 worker。`gatewayId` 唯一性与审计链仍是单库语义。
- **Redis 高可用**：M5 假定单实例 Redis；哨兵/Cluster、凭据轮换与 TLS 客户端证书留到部署侧或后续里程碑。
- **策略传播是“尽快”不是“强一致”**：Pub/Sub 丢失时回到 TTL（默认 300s），控制面不可达仍可用 stale 宽限。
- **用量视图按 worker**：`health.usageQueue`、`usageUpload` 只反映当前 worker；控制面合并视图仍是权威。
- **审计链跨 worker**：同一 SQLite 的 `BEGIN IMMEDIATE` 保证追加顺序；跨主机会断链，属于 D1 的边界。
