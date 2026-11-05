# M5 验收报告（G9：多 worker 与共享状态）

- 生成时间：2026-11-01 13:07:56
- 运行环境：Windows-11-10.0.26300-SP0 / Python 3.13.3 / 同机 2 个 worker + Redis 7 + 共享 SQLite
- 结论：**G9 通过**（16/16 个步骤通过）

## 步骤

| 步骤 | 结果 | 耗时 | 说明 |
|---|---|---|---|
| tests/test_m5_schema.py | 通过 | 1.5s | 退出码 0；通过 9 项，失败 0 项 |
| tests/test_m5_shared.py | 通过 | 2.1s | 退出码 0；通过 19 项，失败 0 项 |
| tests/test_m5_outbox.py | 通过 | 1.7s | 退出码 0；通过 7 项，失败 0 项 |
| tests/test_m4_governance.py | 通过 | 21.2s | 退出码 0；通过 30 项，失败 0 项 |
| 两个 worker 同时启动（共享 SQLite + Redis） | 通过 | 7.0s | 控制面、假上游、Webhook 与两个网关均就绪 |
| 多 worker 共享健康面 | 通过 | 0.3s | 全部断言通过 |
| per-key RPM 跨 worker | 通过 | 0.1s | 全部断言通过 |
| per-key 月配额跨 worker | 通过 | 0.1s | 全部断言通过 |
| 并发计数跨 worker | 通过 | 2.8s | 全部断言通过 |
| Redis 故障失败关闭 | 通过 | 4.0s | 全部断言通过 |
| 策略失效 Pub/Sub | 通过 | 2.7s | 全部断言通过 |
| 审计链与 outbox 恰好一次 | 通过 | 0.1s | 全部断言通过 |
| 敏感值不落日志 | 通过 | 17.7s | 管理令牌/服务令牌/主密钥/Key 明文/Redis URL 均未出现 |
| 共享管理面限流（重启 6 RPM / burst 6） | 通过 | 11.1s | 两个 worker 共用一个管理面令牌桶 |
| workers>1 缺 Redis 拒绝启动 | 通过 | 1.0s | 退出码 3；点名 TT_GATEWAY_REDIS_URL |
| workers>1 且 Redis 不可达拒绝启动 | 通过 | 1.6s | 退出码 3；点名 无法连接 Redis |

## 验收口径

- 同机多 worker：两个独立 uvicorn 进程使用同一 `TT_GATEWAY_DATA_DIR`、同一份
  SQLite、同一 `TT_GATEWAY_ID`，各自 pid；迁移在 `BEGIN IMMEDIATE` 后重读
  schema 版本，同时启动不会重复迁移或互相阻塞。
- Redis 共享热状态：管理面与 per-key 令牌桶（容量 `max(1,min(rpm,60))`）、
  配额在途预留（`quota:res:*` / `quota:kres:*`）、并发计数（`quota:active:*`），
  桶时间取 Redis `TIME`。已结算用量仍在 SQLite，账本可重建。
- 预留语义：读 SQLite 已用量 → Lua 原子检查 `used + reserved + tokens`、
  部门/Key 月配额与并发 → 写预留并 INCR 并发；结算先写 SQLite 再释放 Redis。
  崩溃留下的预留按 1h TTL 保守回收，不超发。
- 失败关闭：Redis 不可用（本报告用 `CLIENT PAUSE` 模拟）时，限流与配额路径
  返回 503 `SHARED_STATE_UNAVAILABLE`，OpenAI/Anthropic/Gemini 分别映射为
  `api_error` / `overloaded_error` / `UNAVAILABLE`，不静默放开限额。
- 策略失效：`TT_GATEWAY_POLICY_TTL_SECONDS` 到期拉到新版本并写共享 SQLite
  后向 `{prefix}:policy:events` 发布租户；其他 worker 清内存缓存后从共享
  SQLite 读到新版本。本报告 A 的 TTL=2s、B 的 TTL=300s，B 在 5 秒内切版。
- 审计 outbox：`lease_owner = gatewayId:pid:uuid`、租约至少 60s，
  `BEGIN IMMEDIATE` 内认领；两个 worker 并发投递同一批审计不会重复。
- 启动门槛：`TT_GATEWAY_WORKERS>1` 且缺 `TT_GATEWAY_REDIS_URL` 拒绝启动并点名
  变量；配置了 Redis 但 PING 失败同样拒绝启动（不退回本机限流）。
- 管理面限流：management 桶在 Redis 中按 rateKey（证书主体或对端 IP）共享。
  重启为 6 RPM / burst 6 后交替打两个 worker：前 6 次 200，第 7 次 429。
- 安全边界：Redis 连接串只来自环境变量，健康页只暴露模式/连通性，日志不出现
  URL 或密码；管理令牌、服务令牌、主密钥、provider 密钥与 API Key 明文均不落
  日志或审计外送载荷。

## 说明与残余

- 跨主机多副本仍需要 Postgres 或等价共享数据库；M5 只解决同机多 worker，
  `gatewayId` 唯一性与审计链仍是单库语义。
- Redis 假定单实例；哨兵/Cluster、凭据轮换与 TLS 客户端证书留到部署侧或后续
  里程碑。`TT_GATEWAY_REDIS_PREFIX` 必须在同机多实例间区分。
- 策略传播是“尽快”不是“强一致”：Pub/Sub 丢失时回到 TTL（默认 300s），
  控制面不可达仍可用 stale 宽限。
- `health.usageQueue` / `usageUpload` 只反映当前 worker；控制面合并视图仍是
  权威。用量上报器每 worker 一个，控制面按 `(gatewayId, requestId)` 去重。
- 共享 Redis 故障期间已发出的在途预留会等待 TTL 回收；恢复后无需重启。
