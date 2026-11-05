# M4 验收报告（G8：治理与审计）

- 生成时间：2026-11-01 13:14:02
- 运行环境：Windows-11-10.0.26300-SP0 / Python 3.13.3 / 单副本单 worker
- 结论：**G8 通过**（13/13 个步骤通过）

## 步骤

| 步骤 | 结果 | 耗时 | 说明 |
|---|---|---|---|
| tests/test_m4_roles.py | 通过 | 15.9s | 退出码 0；通过 18 项，失败 0 项 |
| tests/test_m4_audit.py | 通过 | 15.9s | 退出码 0；通过 23 项，失败 0 项 |
| tests/test_m4_webhook.py | 通过 | 23.0s | 退出码 0；通过 15 项，失败 0 项 |
| tests/test_m4_governance.py | 通过 | 17.0s | 退出码 0；通过 30 项，失败 0 项 |
| tests/test_m4_audit.py（控制面） | 通过 | 2.0s | 退出码 0；通过 22 项，失败 0 项 |
| 生产配置启动（RS256 + 控制面策略 + 审计外送） | 通过 | 7.5s | 控制面、策略、网关、假上游与 Webhook 接收端均就绪 |
| 角色权限（控制面代理） | 通过 | 0.1s | 全部断言通过 |
| 角色变更吊销会话 | 通过 | 0.3s | 全部断言通过 |
| 网关审计链与导出 | 通过 | 0.0s | 全部断言通过 |
| 控制面审计链与导出 | 通过 | 0.0s | 全部断言通过 |
| per-key 限流、配额与轮换 | 通过 | 0.6s | 全部断言通过 |
| 审计外送 HMAC | 通过 | 0.8s | 全部断言通过 |
| 敏感值不落日志与外送 | 通过 | 0.1s | 全部断言通过 |

## 验收口径

- 管理角色：控制面 `user.role` 快照进 JWT；代理下发 `X-TT-Admin-Roles`，
  读 = admin/operator/auditor，写 = admin/operator；审计员与普通用户的写/读越权
  均在控制面被拦截。M6 起带部门范围的 operator 另受 `X-TT-Admin-Depts` 收窄，
  不能读取全局审计（`403`），审计读仍由全局 auditor 覆盖。
  改角色必须由管理员操作、不能改自己，并吊销全部旧会话。
- 审计哈希链：`entry_hash = sha256(prev_hash\n at_ms\n actor\n actor_role\n
  action\n object\n before_json\n after_json)`，首条 `prev_hash` 为 64 个 0；
  两端验链接口返回 `brokenAt=null`，NDJSON 导出逐行可复算，网关直连导出带
  `X-TT-Audit-Head` / `X-TT-Audit-Next`。
- 审计外送：审计写入与 outbox 入队同事务；后台按批推送，HMAC-SHA256 覆盖
  `timestamp.body`，头带 `X-TT-Audit-Signature` / `X-TT-Audit-Timestamp` /
  `X-TT-Gateway-Id`；2xx 记为已投递，4xx（除 408/429）跳过，5xx/429/网络错误
  指数退避（上限 300 秒）——重试与跳过细节由 `tests/test_m4_webhook.py` 覆盖。
  健康面 `auditWebhook` 显示 pending / consecutiveFailures / lastDeliveredAt。
- Key 治理：`rateLimitRpm` 令牌桶（0 不限，超限 429 `RATE_LIMITED` +
  `Retry-After`）；`quotaMonthlyTokens` 月配额（超限 429
  `QUOTA_TOKENS_EXCEEDED`）；轮换保持同一 keyId、换新 secret，旧 secret 下一次
  请求立即 401，审计记 `api_key.rotate` 且不含明文；列表带 `rotatedAt` /
  `usedTokens`。
- 安全边界：管理令牌只存在控制面进程环境，浏览器只经代理；provider 密钥一次性
  返回，日志、审计与外送载荷均不含管理令牌、Webhook 密钥、provider 密钥与 Key
  明文；`/ui/` 静态资源不含任何服务端密钥。

## 说明与残余

- G8 网关管理面使用 `TT_GATEWAY_MTLS_MODE=off`：控制面代理不转发
  `X-TT-Client-Verify` / `X-TT-Client-DN`。mTLS 本身由 G6（代理模式）与 G7
  （直连三协议面）覆盖，生产部署仍应启用 `proxy` 模式并把网关管理端口限制在
  控制面网段。
- 单副本 + SQLite；`audit_outbox` 为至少一次投递，接收端应按 `gatewayId + id`
  去重。多副本需要把 outbox 与限流/配额计数迁到共享存储（Redis 接口已预留）。
- 角色变更即时生效依赖每请求校验会话；跨节点部署需共享会话吊销表。
- Key 轮换不会自动通知调用方，旧 secret 立即失效，需调用方先换后用。
- Webhook 签名不包含重放窗口校验，接收端可用 `X-TT-Audit-Timestamp` 自行判重。
