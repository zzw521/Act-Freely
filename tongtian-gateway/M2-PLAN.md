# M2 实施计划：管理面 mTLS 与限流

- 状态：**已完成**（M2.1–M2.4 全部收口；G6 验收 12/12 通过，报告见 [docs/m2-acceptance.md](docs/m2-acceptance.md)）
- 上游依据：[CONTRACT.md](CONTRACT.md) §9（管理 API）、[docs/deploy.md](docs/deploy.md) §6（TLS 反向代理）
- 范围：`tongtian-gateway` 的 `/gateway/v1/admin/*`——客户端证书校验与按调用方限流
- 不在范围：协议面 mTLS/限流、`ttg_` API Key 签发、细粒度 RBAC、审计日志外送、多副本/Redis

---

## 0. M2 的完成定义（一句话）

生产环境默认要求管理面经受信反向代理完成 mTLS 客户端证书校验（缺失或校验失败返回 `401 MTLS_REQUIRED`），
管理请求按证书主体（或对端 IP）独立限流（超限 `429 RATE_LIMITED` + `Retry-After`），
显式关闭时启动告警、非法配置拒绝启动；协议面与 M1 行为不变，G5 回归全部通过。

---

## 1. M2 锁定的决策

| # | 决策 | 理由 | 备选（不采用） |
| --- | --- | --- | --- |
| D1 | mTLS **仅覆盖管理面**，协议面继续 RS256 JWT | 协议面已有短票与部门策略，再叠客户端证书会同时增加部署与客户端改造面 | 全站 mTLS（客户端/CLI 全部要装证书） |
| D2 | TLS 在反向代理终结，网关只采信代理写入的校验结果头 | uvicorn/ASGI 不向应用暴露客户端证书；内网已有 nginx 入口 | 网关内直接加载 CA/证书（引入第二套 TLS 配置与证书轮换） |
| D3 | 只信任 `TT_GATEWAY_TRUSTED_PROXIES` 内对端的 `X-TT-Client-Verify` / `X-TT-Client-DN` | 直连网关不能伪造证书校验结果 | 无条件信任请求头（可被任意客户端绕过） |
| D4 | 关闭 uvicorn 的 proxy headers（`--no-proxy-headers` / `proxy_headers=False`） | 对端地址是信任边界的判断依据，不能被 `X-Forwarded-For` 改写 | 依赖 `X-Forwarded-For`（客户端可伪造） |
| D5 | 生产默认开启（mTLS=proxy、管理限流 120 RPM），允许显式 `off`/`0` 并打印告警 | 安全默认 + 内网灰度留出口；G5 保持 M1 语义需显式关闭 | 生产强制不可关（老部署升级即中断） |
| D6 | 限流按“已验证证书 DN”优先、否则按对端 IP 分桶；进程内令牌桶 | 单副本够用；证书主体是管理调用方的最小身份 | 全局单桶（一个调用方拖垮其他人）、Redis（多副本未启用） |
| D7 | 限流放在管理令牌校验之前 | 同时限住令牌爆破 | 校验后再限流（爆破请求不计数） |
| D8 | 操作人优先 `X-TT-Admin-Actor`，其次证书主体，最后 `admin` | 控制面代理可传真实 uid；直连管理 API 时至少留下证书身份 | 仅保留共享令牌（审计无法区分调用方） |
| D9 | 新增 `MTLS_REQUIRED`（401）；限流复用 `RATE_LIMITED`（429，可重试） | 错误语义与既有协议错误码一致 | 复用 `AUTH_REQUIRED`（无法区分缺证书与缺令牌） |
| D10 | 控制面管理代理支持可选客户端证书（`TT_GATEWAY_ADMIN_CLIENT_CERT/KEY/CA`） | 生产里控制面经 nginx 访问管理面也必须出示证书 | 给控制面开 IP 白名单绕过 mTLS（弱于证书） |

### 1.1 默认值

| 项 | 开发默认 | 生产默认 | 说明 |
| --- | --- | --- | --- |
| `TT_GATEWAY_MTLS_MODE` | `off` | `proxy` | 取值仅 `off` / `proxy` |
| `TT_GATEWAY_TRUSTED_PROXIES` | `127.0.0.1,::1` | 同左，必须覆盖 nginx 出口地址 | CIDR 或单 IP，逗号分隔 |
| `TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE` | `0`（关闭） | `120` | `0` 关闭 |
| `TT_GATEWAY_ADMIN_RATE_LIMIT_BURST` | `0` | `40`（`max(10, RPM/3)`） | 桶容量；RPM>0 时至少 1 |

---

## 2. 里程碑与顺序

| 阶段 | 内容 | 前置 | 出口门槛（Gate） |
| --- | --- | --- | --- |
| M2.1 ✅ | 管理面 mTLS：代理校验结果头、受信代理判断、启动校验、控制面客户端证书 | M1 G5 | 缺证书/校验失败 401 `MTLS_REQUIRED`；受信代理 + 有效证书可写管理面 |
| M2.2 ✅ | 管理面限流：按证书 DN / IP 分桶、令牌桶、`Retry-After`、生产默认值 | M2.1 | 同 DN 超限 429；不同 DN 独立；开发/显式关闭不受限 |
| M2.3 ✅ | 测试与 G6 验收：单元测试、G5 回归、生产默认值、显式关闭、非法配置 | M2.1、M2.2 | `scripts/m2_acceptance.py` 12/12 通过 |
| M2.4 ✅ | 计划与文档回填：本计划、契约、部署、安全复查、README、环境变量 | M2.3 | 文档与实现/验收一致 |

---

## 3. M2.1 管理面 mTLS

### 3.1 实现

```text
浏览器 / 运维 ──HTTPS(客户端证书)──> nginx ──> 网关 127.0.0.1:8001
                                      │
                                      ├─ X-TT-Client-Verify: SUCCESS
                                      └─ X-TT-Client-DN: CN=...
```

- `app/mtls.py`：`resolve_admin_peer(request)` 只在 `mtls_mode=proxy`、对端命中
  `TT_GATEWAY_TRUSTED_PROXIES`、且 `X-TT-Client-Verify == SUCCESS` 时返回已验证主体；
  证书 DN 去控制字符并截断 512 字节；IPv4-mapped IPv6 归一化后再判断网段。
- `app/admin.py`：`require_admin` 先判 mTLS（`proxy` 模式未验证即 `401 MTLS_REQUIRED`），
  再限流、再常量时间比较 `X-TT-Admin-Token`；操作人回落顺序见 D8。
- `app/startup.py`：`TT_GATEWAY_MTLS_MODE` 非法 → 拒绝启动；`proxy` 模式要求受信代理非空且可解析。
  生产环境 `off` 仅告警。
- `app/main.py` / `Dockerfile`：`--no-proxy-headers`（`proxy_headers=False`），保留真实对端地址。
- 控制面 `tongtian-server/app/gateway_admin.py`：`https://` + 配置了客户端证书时，
  用 `ssl.SSLContext.load_cert_chain` + 可选 CA 建 opener；证书配置错误映射
  `GATEWAY_ADMIN_TLS_CONFIG`（503），握手失败映射 `GATEWAY_ADMIN_TLS_ERROR`（502）。

### 3.2 信任边界

- 网关必须只监听回环/内网地址，管理面只经 nginx 暴露；否则客户端可直连网关。
- 受信代理名单必须与 nginx 的**真实出口地址**一致；名单缺失或非法时拒绝启动。
- 代理必须覆盖（而非追加）客户端传入的 `X-TT-Client-*` 头，防止透传伪造。
- 网关上禁止开启 proxy headers，防止 `X-Forwarded-For` 改写 `request.client.host`。

---

## 4. M2.2 管理面限流

- `app/ratelimit.py`：`AdminRateLimiter` 令牌桶，`per_minute/60` 的稳态速率、桶容量 `burst`；
  键为 `cert:<DN>`（已验证）或 `ip:<对端地址>`；返回建议等待秒数，429 时写入 `Retry-After`。
- 桶数超过 4096 时按空闲 10 分钟且未满桶的规则清理；`reset()` 供测试使用。
- `per_minute=0` 表示关闭；RPM>0 时桶容量最小按 1 处理（避免 `burst=0` 直接锁死）；
  生产默认 120 RPM / burst 40。
- 429 复用 `RATE_LIMITED`（管理信封 `{"ok": false, "code": "RATE_LIMITED", ...}`）。
- 进程内实现只适用于单副本；多副本前替换为 Redis 等共享计数。

---

## 5. M2.3 测试与 G6

### 5.1 单元测试（`tests/test_m2_admin.py`，31 项）

- 受信代理判断：命中、IPv4-mapped IPv6、CIDR、非法配置失败关闭。
- `resolve_admin_peer`：模式、受信对端、`SUCCESS`/`FAILED`/缺失、DN 清洗与截断。
- 令牌桶：突发、恢复、独立键、关闭态、清理。
- 启动校验：生产默认 proxy、显式关闭告警、非法模式、空/非法受信代理。
- HTTP：401 `MTLS_REQUIRED`（缺头/FAILED）、带证书 + 正确令牌 200、错误令牌 401、
  429 + `Retry-After`、按 DN 隔离、审计操作人回落证书主体。

### 5.2 G6 验收（`scripts/m2_acceptance.py`，12 步）

| 步骤 | 验证点 |
| --- | --- |
| G5 回归 | 子进程重跑 `scripts/acceptance.py`（G3/G4/压测/生产模式） |
| 生产默认启动 | mTLS=proxy + 6 RPM/burst 2（压测参数），控制面与策略就绪 |
| 默认 mTLS 生效 | 缺头/FAILED → 401 `MTLS_REQUIRED`；错误令牌 → 401 `AUTH_REQUIRED`；正确 → 200 |
| 按 DN 限流 | 同 DN 第 3 次 429 + `Retry-After`；不同 DN 200 |
| mTLS 下登记 provider | 创建 provider/密钥成功，审计 actor = 证书 DN |
| 协议面不变 | RS256 换票 + 对话成功 |
| 默认值告警 | 日志无“未启用 mTLS/限流” |
| 显式关闭 | 启动打印两条告警；免证书 200；连续请求不受限 |
| 非法配置 | 非法模式、空/非法受信代理均退出码 3 且点名变量 |

G6 结果：**12/12 通过**；G5 回归 39 项全过。报告：[docs/m2-acceptance.md](docs/m2-acceptance.md)。

---

## 6. 配置矩阵

| 变量 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `TT_GATEWAY_MTLS_MODE` | 否 | 生产 `proxy`，开发 `off` | 仅 `off` / `proxy`；非法值拒绝启动 |
| `TT_GATEWAY_TRUSTED_PROXIES` | proxy 模式必填 | `127.0.0.1,::1` | 逗号分隔 IP/CIDR；为空或非法拒绝启动 |
| `TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE` | 否 | 生产 `120`，开发 `0` | 0 关闭；生产关闭仅告警 |
| `TT_GATEWAY_ADMIN_RATE_LIMIT_BURST` | 否 | `max(10, RPM/3)` | 桶容量；RPM>0 时最小 1 |
| `TT_GATEWAY_ADMIN_CLIENT_CERT` | 否（控制面） | 空 | 控制面访问 `https://` 管理面的客户端证书 |
| `TT_GATEWAY_ADMIN_CLIENT_KEY` | 否（控制面） | 空 | 客户端私钥；权限仅服务账号可读 |
| `TT_GATEWAY_ADMIN_CA` | 否（控制面） | 空 | 校验网关/入口证书的 CA；空用系统信任库 |

---

## 7. 部署要求

1. nginx 管理面 location 开 `ssl_client_certificate` + `ssl_verify_client on`，
   并设置 `X-TT-Client-Verify $ssl_client_verify`、`X-TT-Client-DN $ssl_client_s_dn`。
2. 协议面与健康检查 location **不要**透传 `X-TT-Client-*`；管理面 location 必须覆盖客户端同名头。
3. 网关只绑定回环/内网；Docker 启动参数保留 `--no-proxy-headers`。
4. `TT_GATEWAY_TRUSTED_PROXIES` 填写 nginx 到网关的源地址（容器网络下是对端容器/宿主地址）。
5. 控制面经 nginx 访问管理面时配置 `TT_GATEWAY_ADMIN_CLIENT_CERT/KEY/CA`；证书与私钥只进服务端环境。
6. 证书轮换：先在 nginx 信任链中加入新 CA，再替换客户端证书；网关侧无需重启（只读请求头）。

示例配置见 [docs/deploy.md](docs/deploy.md) §6。

---

## 8. 残余与移交

- **单副本限流**：进程内令牌桶，多副本各算一份；多副本前替换为共享计数。
- **验收不含真实 TLS 握手**：G6 用回环 + 注入校验头模拟 nginx，证书链/吊销/OCSP 需在部署环境联调。
- **信任边界依赖部署**：网关若被直接暴露，mTLS 可被绕过；必须靠监听地址、防火墙与 nginx 唯一入口兜底。
- **审计主体仍可被控制面覆盖**：`X-TT-Admin-Actor` 优先于证书主体，可信度取决于控制面。
- **未覆盖协议面**：协议面仍按 RS256 JWT + 策略鉴权，不做 mTLS 与管理面限流。
- **后续里程碑**：`ttg_` API Key 签发/吊销、细粒度 RBAC、审计日志外送、多副本共享限流与配额。

---

## 9. 变更文件

```text
app/mtls.py            新增：受信代理判断、证书主体解析
app/ratelimit.py       新增：管理面令牌桶
app/config.py          改：mTLS/受信代理/限流配置
app/admin.py           改：mTLS 门槛、限流、审计主体回落
app/errors.py          改：新增 MTLS_REQUIRED（401）
app/startup.py         改：mTLS 模式与受信代理校验、生产关闭告警
app/main.py            改：uvicorn 禁用 proxy headers（直接运行入口）
Dockerfile             改：启动参数加 --no-proxy-headers
tests/test_m2_admin.py 新增：31 项自测
scripts/m2_acceptance.py 新增：G6 验收
scripts/acceptance.py  改：G5 显式关闭 mTLS/限流，保持 M1 语义
docs/m2-acceptance.md  新增：G6 报告
```
