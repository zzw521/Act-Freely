# M7 部署演练报告（真实 TLS/mTLS、Sentinel、Postgres）

- 生成时间：2026-11-01 13:04:54
- 运行环境：Windows-11-10.0.26300-SP0 / Python 3.13.3
- TLS 拓扑：nginx 容器（nginx:1-alpine）终结 TLS，网关容器（tongtian-gateway:drill）同网段互通且不发布宿主端口
- Sentinel 拓扑：1 主 2 从 + 3 个 Sentinel（redis:7-alpine）+ 2 个网关进程 + 3 个压测主体，全在同一隔离 bridge 网络内用真实内网地址互访
- Postgres 拓扑：postgres:16-alpine 单实例 + 1 个 Redis + 假上游 + 3 个网关进程（A/B 共享 `ttgw`，C 指向恢复库 `ttgw_backup`），请求主体在容器内发真实 HTTP
- 结论：**M7 通过**（52/52 步通过；本次范围：全部）

## 步骤

| 段落 | 步骤 | 结果 | 耗时 | 说明 |
|---|---|---|---|---|
| M7.1 真实 TLS/mTLS 反向代理 | 生成演练 PKI（CA、服务端、合法/吊销/过期/未知 CA 客户端与 CRL） | 通过 | 0.4s | CA=tt-drill-ca-94f0c8 吊销序列=7456950818738214768 过期证书 notAfter=2026-10-30T05:03:38+00:00 文件=ca.crt, ca.key, client-expired.crt, client-expired.key, client-ok.crt, client-ok.key, client-revoked.crt, client-revoked.key, client-rogue.crt, client-rogue.key, crl.pem, rogue-ca.crt, server.crt, server.key |
| M7.1 真实 TLS/mTLS 反向代理 | nginx 终结 TLS、网关只监听内网且不发端口（真实部署拓扑） | 通过 | 5.0s | 隔离网段 172.28.97.0/24，受信代理=172.28.97.21/32，网关容器无宿主端口映射 |
| M7.1 真实 TLS/mTLS 反向代理 | 真实 TLS 握手：服务端证书由演练 CA 签发、SAN 覆盖 localhost、协议 ≥ TLS1.2 | 通过 | 0.0s | TLSv1.3 / TLS_AES_256_GCM_SHA384 / subject=localhost issuer=tt-drill-ca-94f0c8 san=['localhost', '127.0.0.1'] |
| M7.1 真实 TLS/mTLS 反向代理 | TLS1.2 可握手（兼容旧客户端；配置只放行 TLSv1.2/1.3） | 通过 | 0.0s | TLS1.2 → HTTP 200 cipher=ECDHE-RSA-AES256-GCM-SHA384 |
| M7.1 真实 TLS/mTLS 反向代理 | 合法客户端证书 + 管理令牌 → 管理面 200，回包来自网关自身 | 通过 | 0.0s | HTTP 200 code=- body={"ok":true,"gatewayId":"gw-m7-drill","gatewayVersion":"0.1.0","environment":"development","workerPid":1,"policyVersion":"dev#1","policyStale":false,"providersConfigured":0,"usageQueue":0,"usageUpload":{"enabled":false,"pending":0,"lastSuccessAtMs":null,"lastEr；database={"backend": "sqlite", "connected": true, "schemaVersion": 7} sharedState={"mode": "memory", "connected": true, "tls": false, "workers": 1, "pid": 1, "policySubscriber": "disabled", "lastError": null} |
| M7.1 真实 TLS/mTLS 反向代理 | 证书 DN 写入审计主体（代理清空 X-TT-Admin-Actor 后仍可归因） | 通过 | 0.0s | 签发 HTTP 201；审计 actor=C=CN,O=Tongtian M7 Drill,CN=tt-drill-operator |
| M7.1 真实 TLS/mTLS 反向代理 | 不带客户端证书访问管理面 → 代理直接回 401（不是网关的错误体） | 通过 | 0.0s | HTTP 401 code=- body=<html> <head><title>401 Authorization Required</title></head> <body> <center><h1>401 Authorization Required</h1></center> <hr><center>nginx/1.31.8</center> </body> </html> |
| M7.1 真实 TLS/mTLS 反向代理 | 客户端自带的校验头被代理覆盖：无证书时伪造无效 | 通过 | 0.0s | HTTP 401 code=- body=<html> <head><title>401 Authorization Required</title></head> <body> <center><h1>401 Authorization Required</h1></center> <hr><center>nginx/1.31.8</center> </body> </html> |
| M7.1 真实 TLS/mTLS 反向代理 | 被 CRL 吊销的客户端证书 → 管理面不放行 | 通过 | 0.1s | HTTP 400 code=- body=<html> <head><title>400 The SSL certificate error</title></head> <body> <center><h1>400 Bad Request</h1></center> <center>The SSL certificate error</center> <hr><center>nginx/1.31.8</center> </body> </html>；代理判定 verify=FAILED:certificate revoked |
| M7.1 真实 TLS/mTLS 反向代理 | 已过期的客户端证书 → 管理面不放行 | 通过 | 0.1s | HTTP 400 code=- body=<html> <head><title>400 The SSL certificate error</title></head> <body> <center><h1>400 Bad Request</h1></center> <center>The SSL certificate error</center> <hr><center>nginx/1.31.8</center> </body> </html>；代理判定 verify=FAILED:certificate has expired |
| M7.1 真实 TLS/mTLS 反向代理 | 未知签发者的客户端证书 → 管理面不放行 | 通过 | 0.1s | HTTP 400 code=- body=<html> <head><title>400 The SSL certificate error</title></head> <body> <center><h1>400 Bad Request</h1></center> <center>The SSL certificate error</center> <hr><center>nginx/1.31.8</center> </body> </html>；代理判定 verify=FAILED:unable to verify the first certificate |
| M7.1 真实 TLS/mTLS 反向代理 | /health 不需要客户端证书：运维可无证书拨测存活 | 通过 | 0.0s | HTTP 200 code=- body={"ok":true,"service":"tongtian-gateway","version":"0.1.0","gatewayId":"gw-m7-drill","contractVersion":"gateway-contract.v1","policyVersion":"dev#1","timeMs":1793509424676} |
| M7.1 真实 TLS/mTLS 反向代理 | 协议面不受 mTLS 约束：无效令牌回 401 认证错误而不是 MTLS_REQUIRED | 通过 | 0.0s | 401 code=AUTH_REQUIRED body={"error":{"message":"令牌格式无效","type":"authentication_error","param":null,"code":"AUTH_REQUIRED"}} |
| M7.1 真实 TLS/mTLS 反向代理 | 代理只写校验头不设门槛（8443）时，网关自身失败关闭 → 401 MTLS_REQUIRED | 通过 | 0.0s | HTTP 401 code=MTLS_REQUIRED body={"ok":false,"code":"MTLS_REQUIRED","message":"管理面要求 mTLS 客户端证书（经受信反向代理校验）"} |
| M7.1 真实 TLS/mTLS 反向代理 | 同网段非受信主机直连网关伪造校验头 → 401 MTLS_REQUIRED（对端绑定生效） | 通过 | 1.4s | 伪造头：{"status": 401, "body": "{\"ok\":false,\"code\":\"MTLS_REQUIRED\",\"message\":\"管理面要求 mTLS 客户端证书（经受信反向代理校验）\"}"}；不带伪造头：{"status": 401, "body": "{\"ok\":false,\"code\":\"MTLS_REQUIRED\",\"message\":\"管理面要求 mTLS 客户端证书（经受信反向代理校验）\"}"} |
| M7.1 真实 TLS/mTLS 反向代理 | 代理漏配 proxy_set_header（9443）：伪造头仍过不了管理令牌这道门槛 | 通过 | 0.0s | 无令牌伪造：401/AUTH_REQUIRED；有令牌伪造：201，审计主体被伪造成 CN=attacker-from-rogue-host,O=NotTongtian（残余风险：归因可伪造，见文档处置） |
| M7.1 真实 TLS/mTLS 反向代理 | 网关侧证据：对端是代理的真实内网 IP，被代理拒绝的证书请求没进应用 | 通过 | 0.0s | 代理对端 172.28.97.21 出现在访问日志=True；应用内证书类 400=0；应用内 401（网关/代理失败关闭）=4 |
| M7.1 真实 TLS/mTLS 反向代理 | 凭据卫生：健康页不回显连接串，网关与代理日志不含管理令牌/私钥 | 通过 | 0.1s | 泄露项=无；健康页含 '://'=False |
| M7.1 真实 TLS/mTLS 反向代理 | TT_GATEWAY_TRUSTED_PROXIES 为空时容器拒绝启动并点名变量 | 通过 | 1.5s | RuntimeError: 网关无法启动，缺少或无效的配置：TT_GATEWAY_TRUSTED_PROXIES（mTLS proxy 模式不能为空） |
| M7.2 真三节点 Sentinel 主从切换 | 起真实 Redis 主从与 3 个 Sentinel（口令只写进容器内配置文件） | 通过 | 2.7s | 网段 172.21.7.0/24，主从 3 个 + 哨兵 3 个 |
| M7.2 真三节点 Sentinel 主从切换 | 起两个独立网关进程（各自 SQLite，共享同一套 Sentinel 发现地址） | 通过 | 1.6s | 网关 tt-m7-gwa-94f0c8 / tt-m7-gwb-94f0c8，前缀 ttm7s-94f0c8，限流 20/60 每分钟 |
| M7.2 真三节点 Sentinel 主从切换 | 1 主 2 从 + 3 个真 Sentinel 就位，三个哨兵对同一主库达成一致（quorum=2） | 通过 | 2.6s | [{"sentinel": "tt-m7-sn1-94f0c8", "master": "172.21.7.11:6379", "flags": "master", "replicas": "2", "peers": "2", "quorum": "2"}, {"sentinel": "tt-m7-sn2-94f0c8", "master": "172.21.7.11:6379", "flags": "master", "replicas": "2", "peers": "2", "quorum": "2"}, {"sentinel": "tt-m7-sn3-94f0c8", "master": "172.21.7.11:6379", "flags": "master", "replicas": "2", "peers": "2", "quorum": "2"}] |
| M7.2 真三节点 Sentinel 主从切换 | 两个独立网关进程经 Sentinel 发现主库（mode=sentinel、connected=true） | 通过 | 0.4s | {"A": {"mode": "sentinel", "connected": true, "tls": false, "workers": 1, "pid": 1, "policySubscriber": "listening", "lastError": null}, "B": {"mode": "sentinel", "connected": true, "tls": false, "workers": 1, "pid": 1, "policySubscriber": "listening", "lastError": null}, "启动时间/重启次数": {"A": "2026-11-01T05:03:51.577490232Z 0", "B": "2026-11-01T05:03:51.842144337Z 0"}} |
| M7.2 真三节点 Sentinel 主从切换 | 真实对端主体打满 A 的管理面令牌桶（容量 20／60 每分钟） | 通过 | 0.3s | 分布 200/-×20 429/RATE_LIMITED×12；首个 429 在第 21 次；Retry-After=1 |
| M7.2 真三节点 Sentinel 主从切换 | 同一主体转而请求 B 立即 429：桶在 Redis 里，两个进程共用同一份 | 通过 | 0.6s | B 返回 429/RATE_LIMITED×1；桶键 ttm7s-94f0c8:rl:admin:ip:172.21.7.41 |
| M7.2 真三节点 Sentinel 主从切换 | 另一来源主体在 B 上仍放行：共享不等于串桶 | 通过 | 0.6s | 对照主体 ip:172.21.7.42 返回 200/-×1 |
| M7.2 真三节点 Sentinel 主从切换 | 令牌按速率回填后同一主体恢复放行 | 通过 | 2.4s | 等待 2s 后 200/-×1 |
| M7.2 真三节点 Sentinel 主从切换 | 切换前先确认两个副本都追平了主库写入偏移 | 通过 | 0.3s | 主=tt-m7-rd-94f0c8 偏移=10267 落后=tt-m7-rd1-94f0c8:0 tt-m7-rd2-94f0c8:0 |
| M7.2 真三节点 Sentinel 主从切换 | 杀主（tt-m7-rd-94f0c8）窗口内两个进程都失败关闭：503 SHARED_STATE_UNAVAILABLE | 通过 | 8.1s | A 命中 3 次、B 命中 3 次；分布 200/-×10 503/SHARED_STATE_UNAVAILABLE×6；最慢一次 519ms（未挂死在 Redis 上） |
| M7.2 真三节点 Sentinel 主从切换 | Sentinel 三节点仲裁把副本自动提升为新主（无人工介入） | 通过 | 0.1s | 新主 172.21.7.12（tt-m7-rd1-94f0c8，原副本）；1:X 01 Nov 2026 05:04:05.157 # +switch-master tt-drill 172.21.7.11 6379 172.21.7.12 6379 |
| M7.2 真三节点 Sentinel 主从切换 | 两个进程不重启即自动重连新主（容器启动时间未变、RestartCount=0） | 通过 | 0.5s | {"A": {"mode": "sentinel", "connected": true, "tls": false, "workers": 1, "pid": 1, "policySubscriber": "listening", "lastError": null}, "B": {"mode": "sentinel", "connected": true, "tls": false, "workers": 1, "pid": 1, "policySubscriber": "listening", "lastError": null}, "同一进程": true, "重启次数": 0, "切换前": {"A": "2026-11-01T05:03:51.577490232Z 0", "B": "2026-11-01T05:03:51.842144337Z 0"}} |
| M7.2 真三节点 Sentinel 主从切换 | 被提升的副本带着切换前的共享限流桶（异步复制已落到新主） | 通过 | 0.2s | 新主 tt-m7-rd1-94f0c8 的桶键 ["ttm7s-94f0c8:rl:admin:ip:172.21.7.42", "ttm7s-94f0c8:rl:admin:ip:172.21.7.41", "ttm7s-94f0c8:rl:admin:ip:172.21.7.43"] |
| M7.2 真三节点 Sentinel 主从切换 | 新主上跨进程共享限流仍然成立（A 打满后同一主体在 B 立即 429） | 通过 | 0.6s | A：200/-×11 429/RATE_LIMITED×21；B：429/RATE_LIMITED×1 |
| M7.2 真三节点 Sentinel 主从切换 | 主库网络阻塞（pause tt-m7-rd1-94f0c8）同样失败关闭，解除后两个进程自愈 | 通过 | 8.5s | 阻塞期 503：A 4 次／B 3 次；分布 200/-×7 503/SHARED_STATE_UNAVAILABLE×7；自愈后 {"mode": "sentinel", "connected": true, "tls": false, "workers": 1, "pid": 1, "policySubscriber": "listening", "lastError": null} {"mode": "sentinel", "connected": true, "tls": false, "workers": 1, "pid": 1, "policySubscriber": "listening", "lastError": null} |
| M7.2 真三节点 Sentinel 主从切换 | 主库口令错误的新进程拒绝启动（不静默退回内存态，不带着半套状态服务） | 通过 | 1.8s | app.shared_state.SharedStateError: 共享状态不可用 \| "无法连接 Redis/Sentinel，共享状态不可用，拒绝启动" \| RuntimeError: 无法连接 Redis/Sentinel，共享状态不可用，拒绝启动 |
| M7.2 真三节点 Sentinel 主从切换 | Redis/Sentinel 口令、连接串不进日志与健康页，口令也不出现在容器命令行 | 通过 | 0.9s | 日志/健康页命中=无；容器 Args 命中口令=否；健康页摘要={"ok":true,"gatewayId":"gw-m7-drill","gatewayVersion":"0.1.0","environment":"development","workerPid":1,"policyVersion":"dev#1","policyStale":false,"providersConfigured":0,"usageQu |
| M7.3 Postgres 生产迁移与备份恢复 | 起 Postgres（初始化 SQL 经 docker cp 注入：最小权限角色 + 业务库/恢复库） | 通过 | 3.8s | 网段 172.25.19.0/24，实例 tt-m7-pg-94f0c8 @ 172.25.19.11，库 ttgw/ttgw_backup，角色 ttgw_app |
| M7.3 Postgres 生产迁移与备份恢复 | 起 Redis + 假上游 + 两个网关进程（TT_GATEWAY_CROSS_HOST=1，共享同一 Postgres 账本） | 通过 | 1.8s | 网关 ('172.25.19.31', '172.25.19.32')，策略文件只读挂载 /policy/policy.json，前缀 ttm7p-94f0c8 |
| M7.3 Postgres 生产迁移与备份恢复 | 两个独立进程连同一个 Postgres：健康页都报 backend=postgres、connected、schemaVersion | 通过 | 3.6s | {"A": {"status": 200, "code": "", "database": {"backend": "postgres", "connected": true, "schemaVersion": 7}, "sharedState": {"mode": "redis", "connected": true, "tls": false, "workers": 1, "pid": 1, "policySubscriber": "listening", "lastError": null}}, "B": {"status": 200, "code": "", "database": {"backend": "postgres", "connected": true, "schemaVersion": 7}, "sharedState": {"mode": "redis", "con；DSN/口令泄露=无 |
| M7.3 Postgres 生产迁移与备份恢复 | 空库并发首启：迁移在 pg_advisory_xact_lock 内只应用一次，schema_version 只有一行 | 通过 | 0.4s | schema_version 行数=1 version=7 public 表数=11（两个进程同时首启竞争） |
| M7.3 Postgres 生产迁移与备份恢复 | 应用角色最小权限：非超级用户/不建角色/不建库/不复制，越权语句与连 postgres 库全部被拒 | 通过 | 0.4s | ttgw_app=false,false,false,false；拒绝明细 {"CREATE ROLE": [1, "ERROR: permission denied to create role DETAIL: Only roles with the CREATEROLE attribute may create roles."], "pg_read_file": [1, "ERROR: permission denied for function pg_read_file"], "连 postgres 库": [2, "psql: error: connection to server at \"127.0.0.1\", port 5432 failed: FATAL: permission denied for database \"postgres\" DETAIL: User does not have CONNECT privileg"]} |
| M7.3 Postgres 生产迁移与备份恢复 | 重启第二个进程不重放迁移：schema_version 仍一行、日志无 already exists/Traceback | 通过 | 5.1s | 重启后健康 {"backend": "postgres", "connected": true, "schemaVersion": 7}；行数=1；命中词=无 |
| M7.3 Postgres 生产迁移与备份恢复 | 管理面写入 Postgres：provider、provider 密钥、签发 API Key 各一行 | 通过 | 1.2s | provider=201 密钥=201（明文只在此响应出现一次） API Key=201；providers=1 provider_keys=1 api_keys=1 |
| M7.3 Postgres 生产迁移与备份恢复 | 6 次真实对话（A/B 轮流）写进同一份 Postgres 账本 | 通过 | 2.3s | 对话 200=6/6 分布 200/-×6；usage_events=6 quota_counters=1 audit_log=3 |
| M7.3 Postgres 生产迁移与备份恢复 | A、B 读到的用量总额逐字段相同：跨主机账本不分叉 | 通过 | 0.6s | A={"calls": 6, "promptTokens": 72, "completionTokens": 48, "totalTokens": 120, "latencyMsTotal": 95, "costs": [{"currency": "USD", "micros": 168}], "avgLatencyMs": 15} B={"calls": 6, "promptTokens": 72, "completionTokens": 48, "totalTokens": 120, "latencyMsTotal": 95, "costs": [{"currency": "USD", "micros": 168}], "avgLatencyMs": 15} |
| M7.3 Postgres 生产迁移与备份恢复 | 审计哈希链在 Postgres 上自洽：brokenAt 空、legacyRows=0 | 通过 | 0.3s | {"entries": 3, "headHash": "0fcb026942ef6ef3c3740d5e5f63ddc44f1a614213b74b7aac0cd191e5ed79bd", "brokenAt": null, "legacyRows": 0} |
| M7.3 Postgres 生产迁移与备份恢复 | pg_dump -Fc 备份（20380 字节）→ 宿主落盘 → pg_restore --no-owner --exit-on-error 到恢复库 | 通过 | 0.7s | dump rc=0 cp rc=0 restore rc=0；表数 主库=11 恢复库=11；restore 无告警 |
| M7.3 Postgres 生产迁移与备份恢复 | 指向恢复库的第三个进程不重放迁移，审计条数/headHash 与用量总额和主库逐字段一致 | 通过 | 4.6s | C 健康 {"backend": "postgres", "connected": true, "schemaVersion": 7}；恢复库 schema_version 行数=1；审计 A=3/0fcb026942ef6ef3c374 C=3/0fcb026942ef6ef3c374；用量一致=True |
| M7.3 Postgres 生产迁移与备份恢复 | 恢复库直接可用：同一 master key 解密 provider 密钥，Bearer 对话在新进程上 200 | 通过 | 0.3s | {"status": 200, "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 0}}, "code": "", "body": "{\"id\":\"chatcmpl-m7pg\",\"object\":\"chat.completion\",\"created\":1793509487,\"model\":\"auto\",\"choices\":[{\"index\":0,\"message\":{\"role\":\"assistant\",\"content\":\"ok\"},\"log |
| M7.3 Postgres 生产迁移与备份恢复 | schema 版本被改到 99（高于程序支持的 7）时：启动门槛拒起进程，不放它出去服务、不改写回版本、不静默降级 | 通过 | 4.3s | 容器状态=exited；服务过请求=None；启动错误：RuntimeError: 账本不可用，拒绝启动：backend=postgres 数据库 schema 版本 99 高于程序支持的 7；点名两版本：RuntimeError: 账本不可用，拒绝启动：backend=postgres 数据库 schema 版本 99 高于程序支持的 7；版本未被改写回=99；迁移重放词=无；日志泄露=无；改回后 version=7 |
| M7.3 Postgres 生产迁移与备份恢复 | 提示词不落盘：唯一标记不出现在三个进程日志，也不在 usage_events/audit_log 任何行里 | 通过 | 0.6s | 日志命中=无；账本命中行数=0；文本/JSON 型内容列数=0；账本只存计数列：prompt_tokens,completion_tokens,cached_tokens,reasoning_tokens,total_tokens,used_tokens,quota_monthly_tokens,monthly_tokens,used_tokens |
| M7.3 Postgres 生产迁移与备份恢复 | 凭据卫生：DSN、Postgres/Redis 口令、管理令牌、provider 明文都不在日志/健康页/容器 Cmd | 通过 | 1.4s | 命中 {}（口令与 DSN 只经环境变量注入，`docker inspect .Config.Env` 仍可见，属宿主侧敏感信息） |

## 说明

- TLS 只在反向代理终结，网关按 `TT_GATEWAY_MTLS_MODE=proxy` 只采信来自
  `TT_GATEWAY_TRUSTED_PROXIES` 的 `$ssl_client_verify/$ssl_client_s_dn`；
  uvicorn `--no-proxy-headers` 保证对端地址不可被 `X-Forwarded-For` 改写。
- 吊销/过期/未知签发者的证书在代理侧即被拒（`ssl_crl` + `ssl_client_certificate`），
  管理面不放行；合法证书的主体经 `$ssl_client_s_dn` 落到审计 `actor`。
- 代理漏配 `proxy_set_header` 时客户端伪造的校验头可透传到网关：仍能挡住
  无管理令牌的调用，但会污染审计归因。部署清单（`docs/deploy.md §6`、`§13.1`）因此要求
  逐条核对三行：`X-TT-Client-Verify`/`X-TT-Client-DN` 由代理写入，`X-TT-Admin-Actor` 清空。
- 主从与 3 个 Sentinel 都是真容器（`redis:7-alpine`），配置经 `docker cp` 注入：
  Redis 口令不进命令行、不进容器 Args、不落日志，健康页只报模式与连通性。
- 主库被杀后由 Sentinel 仲裁提升副本，两个网关进程不重启自动重连；限流桶跟着
  异步复制落到新主，切换前脚本先等复制偏移追平，避免把复制延迟当成丢数据。
- 无论进程退出还是网络阻塞（`docker pause`），Redis 不可用一律 `503
  SHARED_STATE_UNAVAILABLE` 失败关闭，不回退成进程内计数、不静默放行。
- 本段只覆盖 Redis 侧共享状态：配额账本仍是各容器自己的 SQLite
  （`TT_GATEWAY_CROSS_HOST=0`）；跨主机共享账本属 Postgres，见 M7.3。
- 残留：Sentinel 自身未开启客户端鉴权（`TT_GATEWAY_REDIS_SENTINEL_PASSWORD`
  只有单测覆盖），Redis 侧 TLS（`rediss://`）与 Sentinel TLS 未在本段演练。
- Postgres 侧用最小权限角色：`LOGIN` 但非超级用户、不建角色/库、不复制；`CREATE ROLE`、
  `pg_read_file('pg_hba.conf')` 与连 `postgres` 库都被服务端拒绝（42501 / 无 CONNECT 权限）。
- 两个进程同时首启时迁移在 `pg_advisory_xact_lock` 内竞争，`schema_version` 只落一行；
  重启任一进程都不重放 DDL。版本高于程序支持值时不改写回，也不放进程出去服务：
  M8.1 起账本在启动期校验，容器直接退出并在日志里点名两个版本（旧行为是懒连接，
  进程照常起来、第一个请求才回 500）。
- `pg_dump -Fc` 备份 → 同实例恢复库 → 第三个进程指向恢复库：审计条数与 `headHash` 与主库
  逐字相同，用量总额一致，provider 密钥仍能用同一 `TT_GATEWAY_MASTER_KEY` 解密并跑通对话。
- 对话账本只存 token 计数、成本与错误码：public 模式没有文本/JSON 型内容列，唯一提示词标记
  既不进日志也不进 `usage_events`/`audit_log`。
- DSN、Postgres/Redis 口令、管理令牌只经环境变量注入，不出现在日志、健康页与容器 Cmd；
  但 `docker inspect` 的 `.Config.Env` 仍可见它们，宿主机上查看容器属于敏感操作。
- 残留：本段是单实例 Postgres（无流复制/PITR），备份恢复为同实例库到库还原；多可用区部署
  仍需自行验证主从复制与时间点恢复。
