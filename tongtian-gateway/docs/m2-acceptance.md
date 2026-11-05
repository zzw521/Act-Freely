# M2 验收报告（G6：管理面 mTLS 与限流）

- 生成时间：2026-11-01 13:12:09
- 运行环境：Windows-11-10.0.26300-SP0 / Python 3.13.3 / 单副本单 worker
- 结论：**G6 通过**（12/12 个步骤通过）

## 步骤

| 步骤 | 结果 | 耗时 | 说明 |
|---|---|---|---|
| G5 回归（G3/G4/压测/生产模式） | 通过 | 79.9s | 退出码 0；通过 39 项，失败 0 项 |
| 生产默认模式启动（mTLS=proxy + 限流 6 RPM） | 通过 | 6.9s | 控制面、策略与网关均就绪 |
| 生产默认 mTLS 生效（协议面继续 RS256） | 通过 | 0.0s | 全部断言通过 |
| 管理面按证书 DN 限流 | 通过 | 0.0s | 全部断言通过 |
| mTLS 下登记 provider 并校验审计主体 | 通过 | 0.0s | 全部断言通过 |
| RS256/JWKS 对话不受管理面 mTLS 影响 | 通过 | 0.6s | 全部断言通过 |
| 生产默认值无 mTLS/限流关闭告警 | 通过 | 0.0s | 全部断言通过 |
| 显式关闭实例启动 | 通过 | 6.4s | mTLS=off、限流=0 |
| 显式关闭行为与告警 | 通过 | 0.0s | 全部断言通过 |
| 非法 mTLS 模式拒绝启动 | 通过 | 0.9s | 退出码 3；点名 TT_GATEWAY_MTLS_MODE |
| proxy 模式受信代理为空拒绝启动 | 通过 | 1.0s | 退出码 3；点名 TT_GATEWAY_TRUSTED_PROXIES |
| proxy 模式受信代理非法拒绝启动 | 通过 | 0.9s | 退出码 3；点名 TT_GATEWAY_TRUSTED_PROXIES |

## 验收口径

- 范围：仅管理面 `/gateway/v1/admin/*`；协议面（OpenAI/Anthropic/Gemini）继续使用 RS256 JWT。
- mTLS 由反向代理终结：nginx `ssl_verify_client on` 校验证书后写入
  `X-TT-Client-Verify` / `X-TT-Client-DN`；网关只采信
  `TT_GATEWAY_TRUSTED_PROXIES` 内对端的这两个头，且 uvicorn 禁用 proxy headers。
- 本脚本用回环地址模拟 nginx（直接注入校验头），验证的是网关侧信任边界与行为；
  真实 TLS 握手、证书链与吊销需在部署环境用 nginx 联调。
- 限流按证书 DN（已验证）或对端 IP 分桶，进程内令牌桶；429 携带 `Retry-After`。
- 生产默认值：mTLS=proxy、管理面限流 120 RPM（本脚本压到 6 RPM / burst 2）；
  可通过 `TT_GATEWAY_MTLS_MODE=off`、`TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE=0`
  显式关闭，启动会打印告警。G5 回归显式关闭以保留 M1 语义。

## 说明与残余

- 令牌桶在进程内，仅适用于单副本；多副本前需替换为 Redis 等共享计数。
- 网关必须只监听回环/内网地址，并由 nginx 唯一入口转发管理面；
  否则客户端可绕过代理直接访问管理面。
- 受信代理名单必须覆盖 nginx 的真实出口地址；名单为空或非法时拒绝启动。
- 管理面 mTLS 不覆盖协议面；协议面鉴权仍以 RS256 JWT 与策略为准。
