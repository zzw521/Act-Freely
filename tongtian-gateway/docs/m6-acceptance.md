# M6 验收报告（G10：跨主机多副本）

- 生成时间：2026-11-01 13:10:13
- 运行环境：Windows-11-10.0.26300-SP0 / Python 3.13.3 / Postgres + Redis（redis://127.0.0.1:52773/0）/ 两个独立 gateway 进程
- Postgres：postgresql://127.0.0.1:52766/tongtian_m6
- 结论：**G10 通过**（17/17 个步骤通过）

## 步骤

| 步骤 | 结果 | 耗时 | 说明 |
|---|---|---|---|
| tests/test_m6_postgres.py | 通过 | 17.4s | 退出码 0；通过 21 项，失败 0 项 |
| tests/test_m6_sentinel.py | 通过 | 14.5s | 退出码 0；通过 23 项，失败 0 项 |
| tests/test_m6_tpm.py | 通过 | 18.3s | 退出码 0；通过 27 项，失败 0 项 |
| tests/test_m6_admin_scope.py | 通过 | 16.7s | 退出码 0；通过 32 项，失败 0 项 |
| tests/test_m5_shared.py | 通过 | 2.1s | 退出码 0；通过 19 项，失败 0 项 |
| tests/test_api_keys.py | 通过 | 19.0s | 退出码 0；通过 45 项，失败 0 项 |
| 两进程并发首次迁移 Postgres | 通过 | 6.6s | 两个进程同时启动，只应用一次 schema v7 |
| 重启不重复迁移 | 通过 | 19.0s | 两个进程各重启一次后 schema v7 未重复 |
| 跨进程健康面 | 通过 | 0.4s | 全部断言通过 |
| per-key RPM 跨进程 | 通过 | 0.2s | 全部断言通过 |
| 月配额跨进程 | 通过 | 0.2s | 全部断言通过 |
| 并发计数跨进程 | 通过 | 2.9s | 全部断言通过 |
| 主体 TPM 跨进程 | 通过 | 2.3s | 全部断言通过 |
| Key 级 TPM | 通过 | 0.0s | 全部断言通过 |
| 审计链与 outbox 恰好一次 | 通过 | 0.0s | 全部断言通过 |
| 敏感值不落日志 | 通过 | 25.9s | 管理令牌/服务令牌/主密钥/Key 明文/DB 与 Redis URL 均未出现 |
| Sentinel 主从切换后两进程恢复 | 通过 | 13.5s | mode=sentinel / connected=true；副本保留桶并继续限流 |

## 验收口径

- 两个独立 uvicorn 进程：各自 data 目录、同一 `TT_GATEWAY_ID`、同一
  `TT_GATEWAY_DB_URL`（Postgres）与同一 Redis；`TT_GATEWAY_CROSS_HOST=1`。
- 迁移：空库同时启动只应用一次 schema v7；重启后版本表仍只有一条。
- 配额：Postgres 保存已结算用量，Redis 保存在途预留与并发计数；跨进程
  读到同一份月度余量，不超发。
- TPM：主体桶按策略 `quota.tpm`，Key 桶按 `rateLimitTpm`；预留成本
  `max(1, 输入估算) + 输出上限`，结算退回未用部分；Redis 故障时失败关闭。
- 审计：Postgres 内单链串行追加；两进程验链 headHash 相同、导出连续。
- outbox：租约认领跨进程互斥，接收端按事件 id 去重，只投递一次。
- 部门角色：scoped operator 的列表/签发/轮换/吊销、配额与用量按部门
  + 租户收窄，provider 写与审计读 403；admin/auditor 不受限（单测覆盖）。
- Sentinel：真实 Docker 主从 + 假 Sentinel 提供 `SENTINEL MASTERS` 与
  `get-master-addr-by-name`；杀主并提升副本后两进程重连，健康页保持
  `mode=sentinel`、`connected=true`，副本保留切换前的共享桶。
- 安全边界：DB DSN、Redis URL/密码只来自环境变量；健康页只披露模式、
  连通性与 worker/pid；日志不出现令牌、主密钥、Key 明文或连接串。

## 说明与残余

- 仍假定单一 Postgres 主库与单一 Redis 主从；读写分离、分片、跨地域
  延迟与 Redis Cluster 不在 M6。
- Sentinel 用例使用假 Sentinel 只实现发现接口；生产应部署至少 3 个
  Sentinel 并配置 `TT_GATEWAY_REDIS_SENTINEL_URLS`。
- TPM 输入 token 为本地估算；与上游 tokenizer 的差异由结算退回吸收。
- 部门角色先覆盖网关管理面；控制面 UI 的部门筛选留待后续。
- 审计链是全局单链，跨主机串行化会放大大规模写入等待。
