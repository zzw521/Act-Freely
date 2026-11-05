# M4 实施计划：管理面治理与审计

- 状态：**已完成（M4.1–M4.7）**；G8 验收 13/13 通过，见 [docs/m4-acceptance.md](docs/m4-acceptance.md)
- 上游依据：[M2-PLAN.md](M2-PLAN.md) §3「后续里程碑」、[CONTRACT.md](CONTRACT.md) §9（管理 API）、§10（数据归属）
- 范围：`tongtian-server` 管理角色与审计、`tongtian-gateway` 管理面角色校验、审计哈希链/导出/Webhook、CLI API Key 的 per-key 限流与月度配额、Key 轮换
- 不在范围：多副本/Redis、SSO/OIDC/SAML/SCIM、部门级管理角色、计费与账单、审计防删除（只做防篡改检测）

---

## 0. M4 的完成定义（一句话）

平台管理员可把账号分为 **平台管理员 / 网关运维 / 审计员** 三种管理角色；网关只放行角色允许的读写，
所有管理动作进入**可验链、可导出、可外送**的审计；CLI API Key 具备独立的每分钟限流、月度 token 配额与
轮换能力，旧 secret 轮换后立即失效。

---

## 1. M4 锁定的决策

| # | 决策 | 理由 | 备选（不采用） |
| --- | --- | --- | --- |
| D1 | 管理角色固定四种：`admin`（平台管理员）、`operator`（网关运维）、`auditor`（审计员）、`user`（普通用户） | 与 M1 §11「只保留 admin/user/guest」的最小扩展一致；三角色覆盖“改、看、审” | 自定义权限点（过度设计，UI 与测试成本高） |
| D2 | 网关管理动作按 `read` / `write` 两档：read = admin/operator/auditor；write = admin/operator | 审计员必须能看用量与审计，但不能改 provider/配额/Key；网关运维负责日常变更 | 每个端点单独权限矩阵（维护成本高） |
| D3 | 控制面代理用 `X-TT-Admin-Roles` 下发调用者角色；网关常量时间校验管理令牌后按角色放行；**没有该头**时按 `admin` 处理 | 直连管理 API 的运维脚本只持有共享令牌，本身即 root；控制面代理必须显式带角色，否则越权来自控制面 bug | 无头即拒绝（会打断存量脚本与验收） |
| D4 | 角色变更只能由 `admin` 执行，且禁止修改自己的角色；改角色会吊销该账号全部会话 | 防止误降权把自己锁死；JWT 里的 role 是签发时快照，必须重新登录 | 允许自降（可能把最后一个管理员锁在门外） |
| D5 | 审计链：`entry_hash = sha256(prev_hash + "\n" + at_ms + "\n" + actor + "\n" + actor_role + "\n" + action + "\n" + object + "\n" + before_json + "\n" + after_json)`；首条 `prev_hash` 为 64 个 `0` | 简单、可独立复算；链头可随 Webhook 外送做锚点 | Merkle 树（查询收益低）、签名链（密钥管理复杂） |
| D6 | 两端审计都加链：网关 `audit_log` 与控制面 `admin_audit`；迁移时按 id 顺序回填存量行 | 代理会在两边各记一条，只有一边有链会留下盲区 | 只链网关（控制面角色变更不可验） |
| D7 | 审计导出为 NDJSON；读取/验链/导出都走管理 API，支持 `after` 游标 | 便于 SIEM 消费；游标避免一次拉全量 | CSV（嵌套 before/after 难表达）、只开 Webhook（无法补拉） |
| D8 | Webhook 外送：`audit_outbox` 落库 + 后台批量推送；签名 `HMAC-SHA256(secret, f"{timestamp}.{body}")`，头为 `X-TT-Audit-Timestamp` 与 `X-TT-Audit-Signature: sha256=...` | 写入与推送解耦，网关崩溃不丢；时间戳防重放 | 同步推送（管理请求被外部依赖拖慢） |
| D9 | per-key 限流为进程内令牌桶：`rateLimitRpm` 0–10000，0 表示不限；桶容量 `max(1, min(rpm, 60))` | 单副本足够；CLI 偶发并发不需要更大突发 | 分布式限流（M4 不做多副本） |
| D10 | per-key 月度配额按 token 计：`quotaMonthlyTokens` 0–10^15，0 表示不限；与部门配额**同时**生效，先到先限 | 与契约 §7 现有口径一致，可直接复用预留/结算语义 | 按请求数/金额计（计价币种尚不统一） |
| D11 | 轮换是“同 keyId 换 secret”：更新 `secret_hash` 与 `rotatedAt`，旧 secret 立即失效，不做重叠期 | 与 M3 无缓存吊销一致；泄漏场景要求立即失效 | 双 secret 重叠（多一份校验与过期清理） |
| D12 | 部门级管理角色（只能看/管本部门）留到后续里程碑 | 需要“当前部门”选择、Key 绑定约束与页面过滤，M4 先把全局三角色跑通 | 用 `X-TT-Admin-Dept` 临时拼（语义与 UI 都不完整） |
| D13 | 控制面代理对非 JSON 响应（NDJSON 导出）按原始字节透传 | 审计导出必须经代理，不能要求浏览器拿管理令牌 | 只让运维直连网关导出（绕开控制面审计） |

### 1.1 角色矩阵

| 操作 | admin | operator | auditor | user |
| --- | --- | --- | --- | --- |
| 查看健康 / provider / 用量 / 配额 / Key 列表 | ✓ | ✓ | ✓ | ✗ |
| 新增/修改 provider、增删轮换厂商密钥 | ✓ | ✓ | ✗ | ✗ |
| 调整配额 | ✓ | ✓ | ✗ | ✗ |
| 签发 / 轮换 / 吊销 CLI API Key | ✓ | ✓ | ✗ | ✗ |
| 读取 / 验链 / 导出审计（含网关审计） | ✓ | ✓ | ✓ | ✗ |
| 分配管理角色 | ✓ | ✗ | ✗ | ✗ |
| 市场条目、用户、组织与部门管理 | ✓ | ✗ | ✗ | ✗ |

`user` 访问 `/api/v1/admin/gateway/*` 返回 `403 FORBIDDEN`；`auditor` 的任何写请求由控制面在转发前
拒绝（页面也不显示写按钮），网关侧再做一次同样的校验。

### 1.2 审计条目与导出

NDJSON 每行：

```json
{"id":42,"atMs":1761825700000,"actor":"u-1","actorRole":"operator","action":"api_key.rotate","object":"9f2c1a7b4e60","before":{"rotatedAt":null},"after":{"rotatedAt":1761825700000},"prevHash":"…64hex…","entryHash":"…64hex…"}
```

- `GET .../audit?after=0&limit=100` → `{"ok":true,"entries":[...],"nextAfter":42,"headHash":"...","legacyRows":0}`。
- `GET .../audit/verify` → `{"ok":true,"entries":N,"headHash":"...","brokenAt":null}`；篡改后
  `ok=false`、`brokenAt=<id>`，HTTP 仍为 `200`（验链结果本身是数据，不是请求错误）。
- `GET .../audit/export?after=0&limit=1000` → `application/x-ndjson`，每行如上。
- 审计不记录明文密钥、Key secret、哈希；`before/after` 只留元数据。

### 1.3 CLI API Key 新增字段

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `rateLimitRpm` | int | 0–10000，0 = 不限；按 key 令牌桶 |
| `quotaMonthlyTokens` | int | 0–10^15，0 = 不限；按 UTC 自然月 |
| `rotatedAt` | int/null | 最近一次轮换时间（响应只读） |
| `usedTokens` | int | 列表响应中给出当前月已结算 token（只读） |

`POST /gateway/v1/admin/api-keys/{keyId}/rotate` 返回 `{"ok":true,"key":{...,"secret":"ttg_..."}}`；
已吊销/未知 Key 返回 `400 INVALID_REQUEST`。签发、轮换、吊销都写审计，动作分别为
`api_key.create` / `api_key.rotate` / `api_key.revoke`。

### 1.4 审计外送（M4.4）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TT_GATEWAY_AUDIT_WEBHOOK_URL` | 空 | 留空关闭外送；仅 http(s)、不得含账号密码 |
| `TT_GATEWAY_AUDIT_WEBHOOK_SECRET` | 空 | 配 URL 时必填且 ≥16 字符；HMAC-SHA256 共享密钥 |
| `TT_GATEWAY_AUDIT_WEBHOOK_INTERVAL_SECONDS` | `10` | 轮询间隔 1–300 |
| `TT_GATEWAY_AUDIT_WEBHOOK_BATCH_SIZE` | `50` | 单批上限 1–500 |
| `TT_GATEWAY_AUDIT_WEBHOOK_TIMEOUT_SECONDS` | `10` | 单次超时 1–60 |

- 写入路径：审计行与 `audit_outbox` 在同一事务落库，后台线程批量推送，网关崩溃不丢事件。
- 签名：`HMAC-SHA256(secret, f"{timestamp}.{body}")`，头 `X-TT-Audit-Timestamp`、
  `X-TT-Audit-Signature: sha256=<hex>`、`X-TT-Gateway-Id`；body 为
  `{"gatewayId","sentAtMs","events":[...]}`。
- 2xx 成功；4xx（除 408/429）确定性拒绝并跳过；5xx/429/网络错误指数退避（上限 300 秒）。
- 健康页 `auditWebhook` 暴露 `enabled/pending/consecutiveFailures/lastError/lastDeliveredAt`，
  `/ui` 网关健康表直接展示。
- 不保证恰好一次，接收端按 `gatewayId + id` 去重；不含明文密钥与 Key secret。

## 2. 里程碑与顺序

| 里程碑 | 内容 | 完成判据 |
| --- | --- | --- |
| M4.1 | 计划与契约回填 | M4-PLAN、CONTRACT §3.2/§7/§9/§10 一致 |
| M4.2 | 管理角色（控制面 + 网关 + UI） | 审计员可读不可写；运维可写；普通用户 403；角色变更审计并吊销会话 |
| M4.3 | 审计哈希链与导出（两端） | 两端验链通过；篡改行可定位；NDJSON 导出经代理可用 |
| M4.4 | 审计外送（Webhook HMAC） | 假接收端验签通过；失败重试且健康页可见 |
| M4.5 | Key 配额、限流与轮换 | per-key RPM/月度配额生效；轮换后旧 secret 立即 401 |
| M4.6 | 测试与 G8 | 两端新用例全过；`docs/m4-acceptance.md` 生成 |
| M4.7 | 文档回填与回归 | G5/G6/G7 回归通过，README/安全复查同步 |

## 3. 残余与移交

- **部门级管理角色**：M4 的 operator/auditor 是全局角色；部门管理员需要“当前部门”选择与
  Key 绑定约束，留到后续里程碑。
- **Webhook 不保证恰好一次**：接收端应按 `gatewayId + id` 去重；4xx 确定性拒绝会记错误并跳过，
  避免毒批次堵住整条队列。
- **审计只防篡改不防删除**：链能发现改动与断链，但拥有数据库写权限的人仍可删尾；需要外部锚点
  （Webhook 接收端留存链头）才能发现尾部截断。
- **per-key 配额与部门配额叠加**：同一主体的多个 Key 各自有月度额度，同时共享部门额度；
  未做 per-key 成本（金额）配额。
- **单副本限流与配额**：RPM 桶与在途预留仍在进程内，多副本需替换为 Redis（与 M1/M2 口径一致）。
- **控制面审计不外送**：M4 只为网关审计实现 Webhook；控制面审计提供本地验链与导出，外送随
  D-27 的合规方案统一处理。

## 4. 验收与回归结果（2026-10-31）

- 网关单测：`tests/test_*.py` 15 个文件全部通过；M4 新增 4 个套件分别 18 / 23 / 15 / 30 项。
- 控制面单测：`tests/test_*.py` 6 个文件全部通过（43 / 35 / 71 / 22 / 306 / 79 项）。
- 验收：G5、G6、G7、G8 顺序复跑全部通过（G8 13/13），报告见 `docs/m1-acceptance.md`、
  `docs/m2-acceptance.md`、`docs/m3-acceptance.md`、`docs/m4-acceptance.md`。
- 文档：`README.md`、`docs/deploy.md`、`docs/security-review.md` 已同步 M4 角色、审计与 Key 治理。
