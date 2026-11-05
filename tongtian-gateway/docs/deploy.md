# 网关部署手册（M1–M7）

面向内网/私有化部署：控制面 `tongtian-server` 负责身份与策略，网关 `tongtian-gateway`
负责鉴权、配额、路由、上游调用与用量记账。**同一主机可运行多个 worker**（M5）：它们共享
同一份账本与一个 Redis，热状态（令牌桶、在途预留、并发计数、策略失效通知）放在 Redis。
账本默认是 SQLite；配置 `TT_GATEWAY_DB_URL` 后改用 **Postgres**，从而支持**跨主机多副本**
（M6）：各副本同一 `TT_GATEWAY_ID`、各自独立数据目录，配额、审计哈希链与 outbox 在共享库中
保持一致。单 worker 不需要 Redis；`TT_GATEWAY_WORKERS>1` 或 `TT_GATEWAY_CROSS_HOST=1`
时 Redis 是硬前提。

## 1. 部署拓扑

```
桌面端/引擎 ──HTTPS──> TLS 反向代理 ──> 网关副本 1..N (127.0.0.1:8001) ──> 厂商 API / 内网模型
控制面 ──HTTPS(JWKS/策略/用量)──> 反向代理 ──────────────┘        │
                                                                  ├── Redis / Sentinel（热状态）
                                                                  │    令牌桶/预留/并发/策略事件
                                                                  └── Postgres（共享账本）
                                                                       配额/审计链/outbox
```

- 单机多 worker：所有 worker 使用同一 `TT_GATEWAY_DATA_DIR`、同一 `TT_GATEWAY_ID`、
  同一 `TT_GATEWAY_REDIS_URL` / `TT_GATEWAY_REDIS_PREFIX`；`health.workerPid` 不同。
- 跨主机多副本：各副本独立 `TT_GATEWAY_DATA_DIR`，同一 `TT_GATEWAY_ID` 与
  `TT_GATEWAY_DB_URL`，共享 Redis/Sentinel；由外部负载均衡（反向代理）分发。
- Redis 只承载热状态，`appendonly no` 即可；丢 Redis 等价于丢在途预留与令牌桶，
  限流/配额路径会失败关闭，恢复后自动继续（不需要重启 worker）。生产建议 Sentinel 主从
  或等价的高可用托管 Redis。M7.2 已用真 1 主 2 从 + 3 个 Sentinel 演练：杀主后自动提升副本，
  两个网关进程不重启即重连，限流桶随复制落到新主；主库网络阻塞期间一律 `503
  SHARED_STATE_UNAVAILABLE`，解除后自愈。

- 网关只监听回环（或内网网段），对外统一走反向代理；TLS 终止在代理。
- 网关的 `/gateway/v1/admin/*` 管理面由**控制面代持**，浏览器只带用户 JWT 访问控制面；
  管理令牌不得下发浏览器。M2 起管理面还要求经反向代理完成 mTLS 客户端证书校验，
  网关只采信受信代理写入的 `X-TT-Client-Verify` / `X-TT-Client-DN`。
- M4 起控制面按调用者角色下发 `X-TT-Admin-Roles`（admin/operator/auditor）：读操作三者均可，
  写操作仅 admin/operator；直连管理 API 不带该头时按 admin 处理，因此管理令牌只应发给控制面与运维。
- 协议面（`/v1/*`、`/v1beta/*`）不做 mTLS，继续使用 RS256 网关短票。
- 出口方向：网关需要访问控制面与各 provider；建议按域名/IP 做出口白名单
  （重定向与 DNS rebinding 的残余风险靠出口控制兜底，见 `docs/security-review.md`）。

## 2. 主机前置条件

| 项 | 要求 |
|---|---|
| 操作系统 | Linux x86_64 或 Windows Server（本项目在 Windows 10/11 + Python 3.13 验证） |
| Python | 3.13（镜像基于 `python:3.13-slim`）；本机部署用 `init.sh` 建 `.venv` |
| CPU/内存 | 单 worker 4 vCPU / 4GB 起步（压测峰值约 1 核满载）；每增加一个 worker 预留约 1 核 + LiteLLM 常驻内存 |
| Redis | 多 worker / 跨主机必需（7.x）；只监听回环/内网，生产建议 `requirepass` 或 ACL；`maxmemory-policy noeviction`；跨主机用 Sentinel 主从或托管高可用 |
| Postgres | 跨主机多副本必需（14+，16 验证）；单机单副本可继续用 SQLite；只允许网关网段访问，独立库与账号 |
| 磁盘 | 数据目录 ≥ 10GB（账本按调用量增长，建议按月归档）；Postgres 按其备份策略单独规划 |
| 时钟 | 必须 NTP 同步；JWT 校验允许 60 秒偏差 |
| 网络 | 出站可到控制面与 provider；入站仅反向代理 |

## 3. 生产环境变量

`TT_GATEWAY_ENV=production` 时启动自检会拒绝缺项（见 `app/startup.py`）。
下表为完整清单，标“必填”的项缺失会直接导致进程退出。

| 变量 | 必填 | 生产建议值 | 说明 |
|---|---|---|---|
| `TT_GATEWAY_ENV` | 是 | `production` | 控制生产门槛、关闭 `/docs`、禁用默认管理令牌 |
| `TT_GATEWAY_HOST` / `TT_GATEWAY_PORT` | 否 | `127.0.0.1` / `8001` | 裸机运行时生效（`python -m app.main`） |
| `TT_GATEWAY_DATA_DIR` | 是 | `/data/gateway` | 存 `gateway.db`、策略缓存；服务账号独占；同机所有 worker 必须指向同一目录 |
| `TT_GATEWAY_ID` | 是 | `gw-prod-01` | 账本 `(gatewayId, requestId)` 去重键的一半，部署后不要改；同机所有 worker 相同 |
| `TT_GATEWAY_WORKERS` | 否 | `1`（多 worker 时 `2`–`8` 起步） | 同机 worker 数（1–64）；`>1` 必须配置 Redis |
| `TT_GATEWAY_REDIS_URL` | 多 worker 必填 | `redis://:<密码>@127.0.0.1:6379/0` | 共享限流/预留/并发/策略事件；只从环境变量读取，不落日志 |
| `TT_GATEWAY_REDIS_PREFIX` | 否 | `ttgw` | 键前缀（`[A-Za-z0-9._-]{1,32}`）；同机多套实例必须区分 |
| `TT_GATEWAY_REDIS_TIMEOUT_MS` | 否 | `500` | Redis 命令超时（100–5000）；超时按不可用处理，失败关闭 |
| `TT_GATEWAY_REDIS_PASSWORD` / `TT_GATEWAY_REDIS_DB` | 否 | 空 / `0` | Redis 认证与库号；URL 已带时以 URL 为准 |
| `TT_GATEWAY_REDIS_SENTINEL_MASTER` | 高可用时是 | `mymaster` | Sentinel 主节点名；与 `_URLS` 必须同时配置 |
| `TT_GATEWAY_REDIS_SENTINEL_URLS` | 高可用时是 | `10.0.0.11:26379,10.0.0.12:26379,10.0.0.13:26379` | Sentinel 地址列表（`host:port` 逗号分隔）；生产至少 3 个 |
| `TT_GATEWAY_REDIS_SENTINEL_PASSWORD` | 否 | 空 | Sentinel 自身认证；业务 Redis 密码用 `TT_GATEWAY_REDIS_PASSWORD` |
| `TT_GATEWAY_REDIS_TLS` | 否 | `0` | 设 `1` 启用 TLS；URL 为 `rediss://` 时自动开启并强制校验 |
| `TT_GATEWAY_REDIS_TLS_CA` | 启用 TLS 时是 | `/etc/tls/redis-ca.crt` | 校验服务端证书的 CA 文件 |
| `TT_GATEWAY_REDIS_TLS_CERT` / `_KEY` | 否 | 空 | 双向认证客户端证书/私钥，必须成对配置 |
| `TT_GATEWAY_DB_URL` | 跨主机必填 | `postgresql://tongtian:<密码>@db:5432/tongtian_gateway` | 共享账本；留空用本地 SQLite。只从环境变量读取，不落日志/健康响应。启动期即校验连通性与 schema 版本，失败拒绝启动（M8.1） |
| `TT_GATEWAY_CROSS_HOST` | 跨主机时是 | `1` | 要求 Postgres + Sentinel/Redis；各副本同一 `TT_GATEWAY_ID`、独立 `DATA_DIR` |
| `TT_GATEWAY_JWT_MODE` | 是 | `rs256` | 生产只接受 RS256 |
| `TT_GATEWAY_JWKS_URL` | 是 | `https://<控制面>/.well-known/jwks.json` | 留空时由 `CONTROL_PLANE_URL` 推导；https，本机回环除外 |
| `TT_GATEWAY_JWT_ISSUER` | 否 | `tongtian-auth` | 与控制面签发一致 |
| `TT_GATEWAY_JWT_AUDIENCE` | 否 | `tongtian-gateway` | 与控制面签发一致 |
| `TT_GATEWAY_ADMIN_TOKEN` | 是 | 32 字节以上随机串 | 管理 API 与部署验收用；只给控制面/运维 |
| `TT_GATEWAY_MTLS_MODE` | 否 | `proxy`（生产默认） | `off` / `proxy`；非法值拒绝启动 |
| `TT_GATEWAY_TRUSTED_PROXIES` | proxy 模式是 | `127.0.0.1,::1` | 受信反向代理的 IP/CIDR，逗号分隔；为空或非法拒绝启动 |
| `TT_GATEWAY_ADMIN_RATE_LIMIT_PER_MINUTE` | 否 | `120` | 管理面每调用方每分钟请求数；`0` 关闭（生产会告警） |
| `TT_GATEWAY_ADMIN_RATE_LIMIT_BURST` | 否 | `40` | 令牌桶容量，默认 `max(10, RPM/3)` |
| `TT_GATEWAY_CONTROL_PLANE_URL` | 是 | `https://<控制面>` | 同时驱动 JWKS、策略拉取与用量上报 |
| `TT_GATEWAY_SERVICE_TOKEN` | 是 | 与控制面一致 | 配了控制面地址就必须有 |
| `TT_GATEWAY_POLICY_FILE` | 否 | 空 | 离线/单机时读本地策略；与控制在同时优先远程 |
| `TT_GATEWAY_POLICY_TTL_SECONDS` | 否 | `300` | 策略拉取间隔 |
| `TT_GATEWAY_POLICY_STALE_GRACE_SECONDS` | 否 | `86400` | 控制面不可达时允许沿用旧策略的时长，响应带 `X-TT-Policy-Stale: 1` |
| `TT_GATEWAY_MAX_REQUEST_BYTES` | 否 | `33554432` | 32MB；分块传输会被直接 413 |
| `TT_GATEWAY_MAX_OUTPUT_TOKENS` | 否 | `8192` | 单次输出上限 |
| `TT_GATEWAY_CONNECT_TIMEOUT_SECONDS` | 否 | `10` | 连接上游超时（1–60） |
| `TT_GATEWAY_UPSTREAM_TIMEOUT_SECONDS` | 否 | `120` | 上游整次响应超时（1–600） |
| `TT_GATEWAY_STREAM_IDLE_TIMEOUT_SECONDS` | 否 | `120` | 流式空闲超时（1–600） |
| `TT_GATEWAY_UPSTREAM_RETRIES` | 否 | `1` | 同 provider 有限重试次数（0–5），含 Retry-After 退避 |
| `TT_GATEWAY_USAGE_BATCH_SIZE` | 否 | `200` | 单批上报事件数 |
| `TT_GATEWAY_USAGE_FLUSH_SECONDS` | 否 | `5` | 上报间隔；失败指数退避 |
| `TT_GATEWAY_ALLOW_PRIVATE_PROVIDERS` | 否 | `true`（内网） | 允许私网 provider；公网部署保持 `false` |
| `TT_GATEWAY_MASTER_KEY` | 是 | `openssl rand -base64 32` | base64 编码的 32 字节 AEAD 主密钥 |
| `TT_GATEWAY_MASTER_KEY_VERSION` | 否 | `1` | 当前加密版本；轮换时递增 |
| `TT_GATEWAY_MASTER_KEYS` | 否 | `{"1":"...","2":"..."}` | 历史密钥环，轮换后仍需保留旧版本解密 |
| `TT_GATEWAY_AUDIT_WEBHOOK_URL` | 否 | 空 | 审计外送接收端；留空即关闭外送；必须 http(s)、不得含账号密码 |
| `TT_GATEWAY_AUDIT_WEBHOOK_SECRET` | 配 URL 时必填 | 32 字节以上随机串 | HMAC-SHA256 共享密钥，至少 16 字符；接收端独立保存 |
| `TT_GATEWAY_AUDIT_WEBHOOK_INTERVAL_SECONDS` | 否 | `10` | 外送轮询间隔（1–300） |
| `TT_GATEWAY_AUDIT_WEBHOOK_BATCH_SIZE` | 否 | `50` | 单批事件上限（1–500） |
| `TT_GATEWAY_AUDIT_WEBHOOK_TIMEOUT_SECONDS` | 否 | `10` | 单次投递超时（1–60） |

> `TT_GATEWAY_NO_LOG` 已在 M1.5 移除：网关从不记录 prompt/response，没有可开关的“内容日志”。

> M2 起生产默认 `mTLS=proxy` + 管理面 120 RPM。确实无法上 mTLS/限流的受信内网部署可显式关闭，
> 启动日志会各打一条告警；G5 验收脚本为保持 M1 语义也显式关闭这两项。
>
> **M5 多 worker**：`TT_GATEWAY_WORKERS>1` 时启动自检要求 `TT_GATEWAY_REDIS_URL` 非空且
> PING 成功，否则进程直接退出并点名变量；运行期 Redis 失联不会退出，而是让限流/配额路径
> 返回 `503 SHARED_STATE_UNAVAILABLE`（失败关闭），恢复后自动继续。Redis 连接串
> 只从环境变量注入，不要写进 `.env` 之外的配置文件、命令行或日志；`.env` 权限按密钥管理。
>
> **M6 跨主机多副本**：`TT_GATEWAY_CROSS_HOST=1` 时启动自检要求 `TT_GATEWAY_DB_URL` 与
> Sentinel/Redis 共享状态齐全。迁移在 `pg_advisory_xact_lock` 内执行，多个副本同时首次启动
> 只会应用一次 schema；库中版本高于程序支持版本时拒绝启动。DB DSN、Redis URL 与密码同样
> 只从环境变量注入，`/health` 只披露后端模式与连通性，不回显地址；日志不记录连接串。
> 各副本必须使用同一 `TT_GATEWAY_ID`（审计与用量去重键），数据目录必须独立。
>
> Redis 高可用：配置 `TT_GATEWAY_REDIS_SENTINEL_MASTER` + `TT_GATEWAY_REDIS_SENTINEL_URLS`
> 后由 Sentinel 发现主节点，主从切换后客户端自动重连；`health.sharedState.mode` 为
> `sentinel`。TLS 用 `rediss://` 或 `TT_GATEWAY_REDIS_TLS=1`（此时 URL 必须是 `rediss://`），
> 可配自定义 CA 与双向认证。
>
> 控制面访问 `https://` 管理面时使用 `TT_GATEWAY_ADMIN_CLIENT_CERT` / `TT_GATEWAY_ADMIN_CLIENT_KEY`
> / `TT_GATEWAY_ADMIN_CA`（均在 `tongtian-server` 进程环境，私钥仅服务账号可读）。

## 4. 主密钥：生成、备份与轮换

生成（Linux/macOS，Windows 可用 Git Bash 或 `python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"`）：

```bash
openssl rand -base64 32
```

- **备份**：主密钥丢失 = 所有已存 provider 密钥密文不可解 → 只能重新录入密钥。
  把主密钥放进组织的密钥管理系统（KMS/Vault）并离线备份一份；不要和 `gateway.db` 放在一起。
- **轮换**：
  1. 生成新主密钥，记为版本 N+1；
  2. 把旧版本与新版本都写入 `TT_GATEWAY_MASTER_KEYS`，`TT_GATEWAY_MASTER_KEY` 用新版本，
     `TT_GATEWAY_MASTER_KEY_VERSION=N+1`；
  3. 重启网关（旧密文仍可解，因为版本写在密文里）；
  4. 通过管理 API 对每个 provider key 执行 `rotate`，新密文落到版本 N+1；
  5. 全部轮换完成后，才能从密钥环里移除旧版本。

## 5. JWKS 与密钥轮换

- 网关缓存 JWKS 10 分钟；遇到未知 `kid` 会立即刷新一次（两次刷新最小间隔 30 秒）。
- 控制面轮换签名密钥后，新令牌到达网关时最多一次刷新即可验签，不需要重启网关。
- 滚动窗口内请同时保留新旧公钥（控制面的 JWKS 会同时发布多把），至少保留超过令牌最大
  有效期（网关限制 `exp-iat ≤ 900s`）。

## 6. TLS 反向代理（nginx 示例）

```nginx
server {
    listen 443 ssl http2;
    server_name gateway.example.internal;

    ssl_certificate     /etc/ssl/gateway.crt;
    ssl_certificate_key /etc/ssl/gateway.key;

    # 管理面 mTLS：协议面不要求证书，所以用 optional；管理面 location 再强制 SUCCESS
    ssl_client_certificate /etc/ssl/admin-clients-ca.crt;
    ssl_crl                /etc/ssl/admin-clients.crl.pem;   # 吊销名单，缺则吊销证书仍被放行
    ssl_verify_client      optional;
    ssl_protocols          TLSv1.2 TLSv1.3;
    ssl_ciphers            HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    client_max_body_size 32m;          # 与 TT_GATEWAY_MAX_REQUEST_BYTES 对齐

    # 协议面 + 能力发现 + 健康检查
    location /v1/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;            # SSE 必须关缓冲
        proxy_read_timeout 300s;        # 大于流式空闲/上游超时
        proxy_send_timeout 300s;
    }
    location /v1beta/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
    location /gateway/v1/capabilities { proxy_pass http://127.0.0.1:8001; }
    location /health { proxy_pass http://127.0.0.1:8001; }

    # 管理面：客户端证书 + 来源网段双门槛
    location /gateway/v1/admin/ {
        if ($ssl_client_verify != SUCCESS) { return 401; }
        allow 10.0.0.0/8;
        deny all;
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        # 只由代理写入证书结论；网关只采信受信代理来源（TT_GATEWAY_TRUSTED_PROXIES）
        proxy_set_header X-TT-Client-Verify $ssl_client_verify;
        proxy_set_header X-TT-Client-DN     $ssl_client_s_dn;
        # 必须显式清空：否则客户端自带的 Actor 会透传并污染审计归因
        proxy_set_header X-TT-Admin-Actor "";
        # 管理令牌由控制面/运维携带，代理不注入
    }
}
```

要点：

- SSE 关闭缓冲；读超时大于 `STREAM_IDLE_TIMEOUT_SECONDS`。
- 管理面同时校验客户端证书与来源网段；`X-TT-Client-*` 只允许由本代理写入。
  如果客户端自己带了同名头，nginx 的 `proxy_set_header` 会覆盖。
- 上面这套配置已用真实 nginx 与真实证书链联调过（`scripts/m7_drill.py --only tls`，报告
  `docs/m7-drill.md`）：吊销、过期、未知签发者的客户端证书都在代理侧被拒且不进应用，合法
  证书的 `$ssl_client_s_dn` 落到审计 `actor`；漏配 `proxy_set_header X-TT-Admin-Actor ""`
  时伪造值可透传（鉴权仍靠管理令牌挡住，但审计归因被污染）。
- 吊销依赖 `ssl_crl`：CRL 过期或未重载等于没吊销，更新 CRL 与 `nginx -s reload` 要进运维日历。
- 网关必须只监听回环/内网，并禁用 proxy headers（Dockerfile 已带 `--no-proxy-headers`）；
  否则客户端可绕过代理或伪造 `X-Forwarded-For` 改变对端地址。演练拓扑里网关容器不发布任何
  宿主端口，只有代理可达。
- `TT_GATEWAY_TRUSTED_PROXIES` 必须包含 nginx 到网关的真实源地址；容器部署时通常是
  容器网络中的宿主/网关地址，而不是 `127.0.0.1`。
- 管理面自带按证书 DN/IP 的令牌桶限流；代理层仍建议叠加来源速率限制与鉴权失败告警。
- 客户端证书轮换：先更新 `ssl_client_certificate` 信任链，再替换调用方证书；网关只读
  代理结论，无需重启。
- 控制面经 `https://` 管理面访问时，在 `tongtian-server` 配置
  `TT_GATEWAY_ADMIN_CLIENT_CERT` / `TT_GATEWAY_ADMIN_CLIENT_KEY` / `TT_GATEWAY_ADMIN_CA`。

## 7. 启动方式

### 7.1 Docker Compose（推荐）

`docker-compose.yml` 默认单 worker：端口只绑 `127.0.0.1:8001`、数据卷 `gateway-data`、
健康检查 `/health`。多 worker 用 `shared` profile 一并启动 Redis，并把 worker 数传给容器：

```bash
cd tongtian-gateway
cp .env.example .env        # 按 §3 改生产值
# 单 worker
docker compose up -d --build

# 多 worker（Redis 仅本机可达）
# .env 中设置：
#   TT_GATEWAY_WORKERS=4
#   TT_GATEWAY_REDIS_URL=redis://redis:6379/0     # compose 内置 Redis 未设密码
#   TT_GATEWAY_REDIS_PREFIX=ttgw
# 生产请给 Redis 配 requirepass/ACL，并把 URL 写成 redis://:<密码>@redis:6379/0；
# 连接串只放 .env，不要写进 compose 或命令行。
TT_GATEWAY_WORKERS=4 docker compose --profile shared up -d --build
docker compose logs -f gateway redis
```

> **跨主机**：compose 只适合单机演示。多副本请把 Postgres 与 Sentinel 作为外部受管服务，
> 每个副本单独部署（同一镜像、同一 `TT_GATEWAY_ID`、独立 `gateway-data` 卷），在 `.env` 中
> 注入 `TT_GATEWAY_DB_URL`、Sentinel 地址与 `TT_GATEWAY_CROSS_HOST=1`，由反向代理/负载
> 均衡统一入口。不要给多个副本挂同一个 SQLite 数据卷。

镜像启动顺序是：配置门槛 → **账本校验（连库 + 幂等迁移，M8.1）** → M1.5 的 LiteLLM 预热
（约 1.5 秒），预热完成后 `/health` 才返回 200；`wait_ready`/健康检查以 200 为准，
不会打到“半冷”的进程。账本连不上、缺 psycopg 或库里版本高于程序支持值时进程直接退出，
所以编排层看到的是重启重试，而不是一个“健康但首个请求 500”的副本。容器内 uvicorn 使用
`--workers "$TT_GATEWAY_WORKERS"`，所有 worker 共享 `/data/gateway.db`；SQLite 已开 WAL，
迁移在 `BEGIN IMMEDIATE` 后重读版本，多 worker 同时启动不会重复迁移。

### 7.2 裸机（systemd）

```bash
./init.sh    # 建 .venv、装依赖、导出 schema、跑契约测试，然后前台启动
```

生产建议用 systemd 管理：

```ini
[Unit]
Description=tongtian-gateway
After=network-online.target

[Service]
WorkingDirectory=/opt/tongtian-gateway
EnvironmentFile=/etc/tongtian/gateway.env
ExecStart=/opt/tongtian-gateway/.venv/bin/python -m app.main
Restart=always
RestartSec=2
User=tongtian
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

`python -m app.main` 会按 `TT_GATEWAY_WORKERS` 选择 uvicorn `--workers`；单进程部署保持
`TT_GATEWAY_WORKERS=1`。多 worker 时 systemd 的 `MemoryMax`/`TasksMax` 要按 worker 数放大
（每个 worker 都常驻一份 LiteLLM），并确保 `EnvironmentFile` 里含 `TT_GATEWAY_REDIS_URL`。

## 8. 账本备份与恢复

账本是唯一事实来源（`usage_events`）且配额计数可重建，但 provider 密钥密文只存在库里，
**备份必须包含账本与主密钥**（分开保管）。SQLite 单机部署按下述步骤；Postgres 部署改用
`pg_dump`/PITR，并确保备份覆盖 `usage_events`、`api_keys`、`audit_log`、`audit_outbox`、
`api_key_counters`、`schema_version` 等表。

在线备份（SQLite WAL 模式，不停服务）：

```bash
sqlite3 /data/gateway/gateway.db ".backup '/backup/gateway-$(date +%F-%H%M).db'"
```

Postgres 部署（M7.3 实测：备份→恢复到独立库→新进程直接可用，不重放迁移）：

```bash
# 口令只走 PGPASSWORD，不进命令行历史
export PGPASSWORD="$(cat /run/secrets/gateway_pg_password)"
pg_dump -Fc -h "$PG_HOST" -U ttgw_app -d ttgw -f /backup/ttgw-$(date +%F-%H%M).dump
pg_restore --no-owner --exit-on-error -h "$PG_HOST" -U ttgw_app -d ttgw_restore /backup/ttgw-*.dump
```

- 恢复目标库要事先由 DBA（或超级用户）建好并把 owner 给应用角色：演练用的最小权限角色
  带 `NOCREATEDB`，`createdb` 会被拒；`pg_restore` 本身只需要对目标库有 CREATE 权限。
- 用应用角色备份即可（它拥有这些表）；`--exit-on-error` 保证部分失败不会被当成成功。
- 恢复后的库直接能用：`audit_log` 条数与 `headHash`、`/admin/usage` 总额与主库逐字段一致，
  provider 密钥用同一 `TT_GATEWAY_MASTER_KEY` 仍能解密（主密钥与备份分开保管）。
- 指向恢复库的新进程不会重放迁移：`schema_version` 仍只有一行。

- 备份文件含密文与 last4，请按密钥级别保护（权限 600、加密存储）。
- 每周至少一次全量 + 每日增量；保留 30 天。
- **多 worker 在线备份**：`.backup` 可以在所有 worker 运行时执行（WAL 快照）；恢复必须
  先停掉全部 worker，再替换 `gateway.db` 并删除 `gateway.db-wal`/`gateway.db-shm`。
- **跨主机（Postgres）**：审计链与 outbox 也在共享库中，一次 `pg_dump` 即可拿到一致快照；
  恢复时停掉全部副本，恢复后启动任一副本会自动执行幂等迁移。
- Redis 不参与备份：令牌桶与在途预留是可重建的热状态；恢复后限流从空桶开始、
  崩溃遗留的预留等 1 小时 TTL 回收（保守，不超发）。
- 配额计数异常可用 `scripts/rebuild_quota.py` 从 `usage_events` 重建（先备份，停 worker）。
- 控制面侧另有 30 天 `gateway_usage_events` 去重窗口；网关本地账本不要早于控制面窗口清理。

## 9. 监控与告警

| 信号 | 来源 | 建议阈值 |
|---|---|---|
| 存活 | `GET /health`（无需鉴权） | 连续 3 次失败告警；它只校验进程与策略快照，不连库（账本已在启动期校验，M8.1），运行期库失联要靠下一条 |
| 管理健康 | `GET /gateway/v1/admin/health`（mTLS + 管理令牌） | `providersConfigured=0`、`usageQueue` 持续增长；运行期库不可达时该接口直接 500，可用作就绪探针 |
| 管理面拒绝 | 日志/代理 401 `MTLS_REQUIRED`、429 `RATE_LIMITED` | 401 突然增多查证书/受信代理；429 查调用方频率 |
| 错误率 | 访问日志/代理日志 | 5xx > 1%、429 > 5% 持续 5 分钟 |
| 上游失败 | 网关日志 `PROVIDER_UNAVAILABLE`/`UPSTREAM_TIMEOUT` | 按 provider 分别看 |
| 上报积压 | `admin/health.usageQueue` + 控制面接收时间差 | 超过 10 分钟未上报 |
| 审计外送积压 | `admin/health.auditWebhook`（`pending` / `consecutiveFailures` / `lastError`） | `pending` 持续增长或连续失败 ≥ 3 次告警 |
| 共享状态 | `admin/health.sharedState`（`mode` / `connected` / `lastError`） | `connected=false` 或 `lastError` 非空持续 1 分钟告警 |
| Redis 高可用 | `health.sharedState.mode` / Sentinel 日志 | 期望 `sentinel` 却出现 `redis` 或切换告警；主从切换后确认两副本都 `connected=true` |
| 数据库后端 | `health.database.backend` / `connected` / `schemaVersion`（不披露 DSN） | 跨主机副本应为 `postgres`。启动期已校验账本（M8.1：连不上、迁移失败或库里版本高于程序支持值都拒绝启动），因此 `connected=false` 只可能是运行期库失联或回退到了旧版本程序 |
| TPM 拒绝 | 429 `RATE_LIMITED` 与 `rateLimitTpm`/`quota.tpm` 配置 | 按主体/Key 观察，持续紧贴上限时调整策略 |
| 策略订阅 | `admin/health.sharedState.policySubscriber` | 多 worker 时非 `listening` 告警 |
| Redis 资源 | Redis `INFO memory` / `evicted_keys` | 发生 eviction 或内存接近上限告警（热状态不应被逐出） |
| SQLite 锁 | 日志 `database is locked` | 多 worker 下偶发且自动重试可接受；持续出现告警 |
| JWKS 刷新 | 日志 `刷新 JWKS 失败` | 连续失败告警 |
| 策略过期 | 响应头 `X-TT-Policy-Stale: 1` 比例 | 超过 10% 告警 |

M1 没有内置指标端点；以上均来自日志与代理层统计。接入 Prometheus 前不要自造仪表盘。

### 9.1 审计外送接收端

配置 `TT_GATEWAY_AUDIT_WEBHOOK_URL` 后，每条网关审计先在同一事务写入 `audit_outbox`，后台按
`TT_GATEWAY_AUDIT_WEBHOOK_INTERVAL_SECONDS` 批量推送：

```http
POST /audit
Content-Type: application/json
X-TT-Audit-Timestamp: 1761825700
X-TT-Audit-Signature: sha256=<hex>
X-TT-Gateway-Id: gw-prod-01
```

```json
{"gatewayId":"gw-prod-01","sentAtMs":1761825700000,"events":[{"id":42,"atMs":...,"actor":"u-1","actorRole":"operator","action":"api_key.rotate","object":"9f2c1a7b4e60","before":{},"after":{},"prevHash":"…","entryHash":"…"}]}
```

- 签名 = `HMAC-SHA256(secret, f"{X-TT-Audit-Timestamp}.{原始 body}")`，十六进制小写；接收端必须用
  原始字节复算，并拒绝时间戳偏差过大的请求（建议 ±5 分钟）。
- 2xx 视为投递成功；4xx（除 408/429）视为确定性拒绝：记 `lastError` 后跳过该批，避免毒批次堵队列；
  5xx/429/网络错误保留在 outbox，指数退避重试（最长 300 秒）。
- 不保证恰好一次：接收端按 `gatewayId + id` 去重。
- 外送关闭时 `audit_outbox` 不写入；重新启用后只外送启用之后的审计。
- 外送内容与本地审计一致，不含明文密钥、Key secret 或哈希。

## 10. 升级与回滚

1. 备份 `gateway.db` 与 `.env`（含主密钥版本信息，不含明文密钥）。
2. 新版本镜像/代码先在旁路环境跑：`python tests/test_contract.py`、`test_core.py`、
   `test_protocols.py`、`test_hardening.py`、`test_ops.py`、`test_m2_admin.py`、
   `test_api_keys.py`、`test_m4_roles.py`、`test_m4_audit.py`、`test_m4_webhook.py`、
   `test_m4_governance.py`、`test_m5_schema.py`、`test_m5_shared.py`（需 Redis）、
   `test_m5_outbox.py`、`test_m6_postgres.py`（需 Postgres）、`test_m6_sentinel.py`、
   `test_m6_tpm.py`（需 Redis）、`test_m6_admin_scope.py`。
3. LiteLLM 或 cryptography 升级前先跑 `scripts/spike_litellm.py`，并复核 `docs/security-review.md`。
4. 停旧容器（或 systemd 服务）→ 启新版本；数据库迁移在启动期执行（版本号单调）。
   M8.1 起进程先校验账本再放流量：连不上、迁移失败或库里版本高于程序支持值都会拒绝启动，
   日志点名后端与两个版本号（不回显 DSN/口令）。启动成功即代表账本可用，启完仍建议看
   `GET /gateway/v1/admin/health` 的 `database.schemaVersion`。
5. 升级后跑 `scripts/acceptance.py`（G5，含 G3/G4 + 生产模式启动 + 压测冒烟）、
   `scripts/m2_acceptance.py`（G6，管理面 mTLS + 限流）、`scripts/m3_acceptance.py`（G7，CLI API Key）、
   `scripts/m4_acceptance.py`（G8，角色、审计链/外送、per-key 治理）、
   `scripts/m5_acceptance.py`（G9，同机双 worker 共享状态；需 Docker 或 `M5_REDIS_URL`）与
   `scripts/m6_acceptance.py`（G10，跨主机双副本 + Sentinel；需 Docker 或 `M6_PG_URL`/`M6_REDIS_URL`）。
6. 多 worker 升级：先停全部 worker，再替换代码/镜像并启动；滚动重启会让新旧进程短暂共享
   同一账本，旧版本不认识 schema v7 的列与 advisory-lock 迁移约定，因此不做混版本滚动。
   跨主机副本同理：先停全部副本，再统一升级。
7. 回滚：切回上一镜像标签；若新版本执行过迁移，用升级前的备份恢复数据库（迁移只增不删，
   但不要假设可逆）。

## 11. 容量与压测

`scripts/loadtest.py` 起真实进程（假上游固定 20ms 延迟）测网关净开销，本机（Windows 开发机，
`TT_GATEWAY_WORKERS=1`）基线的验收门槛：

- 非流式 50 并发 / 250 请求：p95 < 1500ms（实测约 325ms，净开销约 305ms）；
- 流式 50 并发 / 50 请求：p95 < 2500ms（实测约 719ms）；
- 账本条数 = 成功请求数，上游收到的请求数一致，日志无 SQLite 锁与未处理异常。

这些数字用于**回归**，不是容量承诺。真实容量取决于上游延迟与模型，上线前用生产 provider
重跑（去掉固定延迟），并按 p95 决定反向代理超时与告警阈值。

多 worker 主要解决单进程 GIL 与 LiteLLM 常驻内存的瓶颈，不改变 SQLite 单写者语义：
审计、配额结算与 outbox 写入仍是串行事务，worker 数超过 4–8 后收益递减。先按单 worker
压出基线，再逐个增加 worker 观察 p95、`database is locked` 重试与 Redis 延迟。

## 12. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 启动即退出，提示缺变量 | 生产门槛 | 按提示补齐 §3 的必填项；mTLS 相关注意 `TT_GATEWAY_MTLS_MODE` 与 `TT_GATEWAY_TRUSTED_PROXIES` |
| 管理面 401 `MTLS_REQUIRED` | 未带客户端证书、证书链不受信、nginx 未写校验头，或源地址不在 `TRUSTED_PROXIES` | 检查 nginx `ssl_client_verify`、`X-TT-Client-*` 与受信代理名单 |
| 管理面 429 `RATE_LIMITED` | 单证书 DN 或对端 IP 超过管理面 RPM | 看 `Retry-After`；确属正常调用再调高 RPM/burst |
| `AUTH_REQUIRED` / `AUTH_UNAVAILABLE` | 令牌 issuer/audience/kid 不匹配；JWKS 拉不到 | 对齐控制面配置；检查网关到控制面的网络与 `SERVICE_TOKEN` |
| `PROVIDER_UNAVAILABLE` | provider 地址被 SSRF 拦截、私网开关未开、上游不可达 | 查日志中 provider id；内网模型需 `ALLOW_PRIVATE_PROVIDERS=1` |
| 429 `QUOTA_*` | 月配额或并发超限 | 管理 API 调配额，或排查客户端重试风暴 |
| 429 `RATE_LIMITED`（协议面） | Key 的 `rateLimitRpm` 超限 | 看 `Retry-After`；必要时调高该 Key 的 `rateLimitRpm` 或改为 0 |
| 429 `QUOTA_TOKENS_EXCEEDED`（API Key） | 该 Key 的月度 token 配额用尽 | 续期/调高 `quotaMonthlyTokens`，或换用其他 Key |
| 请求 413 | 超过 32MB 或使用分块传输 | 客户端改带 `Content-Length` 并压缩输入 |
| 503 `SHARED_STATE_UNAVAILABLE` | Redis 不可达、认证失败或命令超时 | 查 Redis 进程/网络/`TT_GATEWAY_REDIS_URL` 密码与 `TT_GATEWAY_REDIS_TIMEOUT_MS`；这是失败关闭，恢复后无需重启 |
| 启动退出并点名 `TT_GATEWAY_REDIS_URL` | `TT_GATEWAY_WORKERS>1` 但缺 Redis 配置 | 配置可连通的 Redis；单 worker 请把 `TT_GATEWAY_WORKERS` 设为 1 |
| 启动退出并提示“无法连接 Redis” | URL 错、密码错、Redis 未启动 | 用 `redis-cli -u <url> ping` 复核；确认监听地址与 ACL |
| 启动退出并提示 Sentinel 相关变量 | 只配置了 `SENTINEL_MASTER` 或 `SENTINEL_URLS` | 两者必须同时配置；地址用 `host:port,host:port` |
| `No master found` / 切主后 503 持续 | Sentinel 未收敛（不足 quorum），或副本读到旧主 | `redis-cli -p <sentinel> SENTINEL get-master-addr-by-name <name>` 核对；确认至少 3 个 Sentinel 与 quorum。M7.2 演练：杀主后由 3 节点仲裁自动提升副本，两个网关进程不重启即重连，切换期间的 503 属失败关闭、恢复后自愈 |
| TLS 握手失败（`rediss://`） | CA 不匹配、证书过期、服务器未启用 TLS | 核对 `TT_GATEWAY_REDIS_TLS_CA`；URL 必须是 `rediss://`，启用 TLS 时不要再用 `redis://` |
| 启动退出并点名 `TT_GATEWAY_DB_URL` | `TT_GATEWAY_CROSS_HOST=1` 但未配 Postgres | 配置共享 Postgres；单机单副本可改用 SQLite 并关闭 cross-host |
| Postgres 迁移报 `pg_type_typname_nsp_index` | 旧版本并发建表的竞态 | 升级到 M6；迁移已在 `pg_advisory_xact_lock` 内串行化 |
| 启动即退出，日志点名“账本不可用，拒绝启动” | 库连不上（Postgres 未起、口令错、数据目录不可写、SQLite 路径被占）或库里 schema 版本高于程序支持值 | 按日志点名的后端与版本处理：修连通性/权限，或用 ≥ 该 schema 版本的程序；M8.1 起不再放进程出去带病服务，`/health` 也不会出现 200 |
| 旧版本回退后管理接口一律 500（升级前部署的旧程序） | 库里 schema 已被新版本迁移过 | 不要混版本滚动：按 §10 先停全部副本再统一升级；确需回退请用升级前的备份恢复库 |
| TPM 429 `RATE_LIMITED` | `quota.tpm` / `rateLimitTpm` 容量不足以覆盖单次 `max_tokens` | 调大配额；单次成本 = `max(1, 输入估算) + max_tokens`，容量上限 60000 |
| 多 worker 策略版本不一致 | Pub/Sub 丢消息、或 worker 尚未到 TTL | 查 `health.sharedState.policySubscriber`；最迟一个 TTL（默认 300s）自愈 |
| 管理面/Key 限流被“误伤” | 多 worker 共用一个桶（设计如此）；或前序调用占满 | 看 `Retry-After`；按调用方调整 RPM/burst；确认没有多套实例共用同一 Redis 前缀 |
| 管理台用量为空 | 未配控制面/服务令牌，用量只在本地账本 | 配 `CONTROL_PLANE_URL`+`SERVICE_TOKEN`；或查管理 API `/gateway/v1/admin/usage` |
| 首请求偶发慢 | 进程刚启动（旧版本未预热） | M1.5 已把预热放到启动期；若仍出现，检查是否回滚到了旧镜像 |

## 13. 验收

```bash
python scripts/g3_acceptance.py       # 控制面 + 网关 + 假上游，换票/JWKS/部门
python scripts/g4_acceptance.py       # 用量上报、幂等、合并视图、配额 429
python scripts/loadtest.py            # 并发与账本一致性
python scripts/acceptance.py          # G5：以上 + 生产模式启动 + 压测冒烟
python scripts/m2_acceptance.py       # G6：管理面 mTLS + 限流（含 G5 回归）
python scripts/m3_acceptance.py       # G7：CLI API Key 签发/scope/吊销（含 G6 回归）
python scripts/m4_acceptance.py       # G8：角色、审计链/外送、per-key 治理（含 G7 回归）
python scripts/m5_acceptance.py       # G9：同机双 worker 共享状态（自起 Redis 容器）
python scripts/m6_acceptance.py       # G10：跨主机双副本 + Sentinel（自起 Postgres/Redis）
python scripts/m7_drill.py            # M7：真实部署演练（需 Docker，见下）
```

`scripts/m7_drill.py` 不改功能，只把部署侧的三个“上线前联调”项做成可复跑的证据，分三段：

```bash
python scripts/m7_drill.py --only tls        # 真实 nginx 终结 TLS + 真实证书链/CRL（19 步）
python scripts/m7_drill.py --only sentinel   # 1 主 2 从 + 3 个 Sentinel 的杀主与阻塞切主（17 步）
python scripts/m7_drill.py --only postgres   # 并发首启迁移、最小权限角色、pg_dump/pg_restore（16 步）
python scripts/m7_drill.py --keep            # 保留容器与生成的 PKI/备份，便于事后排查
```

报告会写到 `docs/m7-drill.md`（含每步结论、耗时与实测明细）。演练全部在隔离的 Docker
bridge 网络里用真实内网地址互访，网关容器不发布宿主端口；口令与 DSN 只经环境变量注入。

验收报告见 `docs/m1-acceptance.md`、`docs/m2-acceptance.md`、`docs/m3-acceptance.md`、
`docs/m4-acceptance.md`、`docs/m5-acceptance.md`、`docs/m6-acceptance.md` 与
`docs/m7-drill.md`。发布前要求 G5–G10 全绿，且 M7 演练三段 52 步全通过；生产模式用例会验证
缺变量拒绝启动、`/docs` 关闭、RS256 + 控制面 JWKS 能完成一次真实对话，以及管理面缺证书 401、
超限 429、显式关闭告警与非法配置拒绝启动。

### 13.1 上线前检查表

括号里是给出过证据的演练或验收编号，逐条核对后再放流量：

- [ ] 反向代理：`ssl_crl` 指向未过期的 CRL，且 CRL 更新与 `nginx -s reload` 进运维日历；
      `ssl_protocols` 只放行 TLSv1.2/1.3（M7.1）
- [ ] 管理面 location 三行 `proxy_set_header` 齐备：`X-TT-Client-Verify`/`X-TT-Client-DN`
      由代理写入，`X-TT-Admin-Actor` 显式清空（M7.1 实测漏配会污染审计归因）
- [ ] `client_max_body_size` 与 `TT_GATEWAY_MAX_REQUEST_BYTES` 对齐；网关容器不发布宿主端口，
      `TT_GATEWAY_TRUSTED_PROXIES` 只填到网关的真实代理地址（M7.1、G5）
- [ ] 就绪/存活：账本在启动期校验（连不上或版本过高直接拒起，M8.1），`/health` 只证明进程在；
      就绪探针仍用 `GET /gateway/v1/admin/health` 兜运行期失联，升级后核对 `database.schemaVersion`
- [ ] Redis：≥3 个 Sentinel 且 quorum 达标、本实例独立 `TT_GATEWAY_REDIS_PREFIX`、
      `requirepass`/ACL、TLS 走 `rediss://`；切主演练过一次（M7.2、G10）
- [ ] Postgres：应用角色 `NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION` 且
      `REVOKE CONNECT ON DATABASE postgres`；DSN 只进环境变量；`pg_dump`/`pg_restore`
      到独立库恢复过一次并核对 `headHash` 与用量（M7.3）
- [ ] 各副本同一 `TT_GATEWAY_ID`、数据目录互相独立；跨主机部署 `TT_GATEWAY_CROSS_HOST=1`（G10）
- [ ] 审计外送：接收端按 `(gatewayId, id)` 幂等去重并校验 `X-TT-Audit-Timestamp` 时间窗；
      导出文件异地保存（G8）
- [ ] Key 与 provider：scopes/白名单变更走吊销重签；非 `vllm`/`ollama` 的 provider 要指向
      私网需显式 `TT_GATEWAY_ALLOW_PRIVATE_PROVIDERS=1`，仅限内网部署（G7、G8）
