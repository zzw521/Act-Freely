# 服务化部署与命令行

本文只记录**实际能跑通**的部署方式，并明确哪些部分尚未在本仓库开发机验证。
桌面版（Tauri）不需要 Docker，也不经过这里的令牌流程。

## 验证状态（2026-11-02 服务化部署演练；逐条证据见 [`../docs/服务化部署演练报告.md`](../../docs/服务化部署演练报告.md)）

**已在 Windows 开发机验证**

- `engine/server.py --web-dir <dist>`：前端构建产物与 `/api/*` 同端口托管，API 不被静态站点覆盖。
- `TT_ENGINE_TOKEN`：`/api/*` 缺失或错误令牌返回 401 `ENGINE_UNAUTHORIZED`；`Authorization: Bearer` 与 `x-tt-engine-token` 两种头都可用；`/health` 与静态文件不要求令牌。
- 账号 JWT（`TT_ENGINE_JWT_SECRET`）：校验 HS256 签名、`exp`、`iss`、`aud`、`type=access`、`sub`、`sid`；与静态令牌并存；可选 `TT_ENGINE_ALLOWED_USERS` 白名单。
- 请求审计与用量归属：`GET /api/audit` 按调用主体查询；`/api/usage` 与 `/api/usage/report` 支持按用户过滤，JWT 非管理员强制只看自己。
- 非回环绑定准入：`--host 0.0.0.0` 必须同时设置 `TT_ALLOW_REMOTE_BIND=1` 与非空 `TT_ENGINE_TOKEN` 或 `TT_ENGINE_JWT_SECRET`，否则进程拒绝启动。
- `scripts/tt-cli.mjs` 的 `health` / `chat` / `doctor`：真实 HTTP/SSE、令牌只读环境变量、拒绝 `--token`。
- 网页令牌输入框：401 时提示输入，令牌只写当前标签页 `sessionStorage`。
- 以上由 `tests/integration/engine_service_cli_selftest.py`、`tests/unit/deploy/tt-cli.test.ts`、`tests/unit/deploy/engine-service-auth.test.ts` 覆盖。

**已在本轮容器演练中验证**（35 步全绿；镜像 `tongtian-engine:drill` 体积 753,344,646 字节 ≈ 718MB；反代 `nginx:1-alpine` + `caddy:2-alpine`；模型侧是假 OpenAI 兼容端点，全程零真实凭据、零 Token 消耗）

- 镜像构建与运行：`docker build -f deploy/Dockerfile -t tongtian-engine:drill .` 通过；容器内以 uid 10001（非 root）运行；`/health` 200、静态页 200、`/api/*` 无令牌 401。构建期需要 `package-lock.json` 与 `package.json` 同步（`npm ci` 对可选 peer `yaml@2.x` 有严格校验）。本轮复用了本机已缓存的层，从零构建的耗时没有计时。
- 卷权限：命名卷 `/data` 可写（`audit.jsonl` 与 `/data/work/<会话>` 目录由 uid 10001 落盘）；宿主绑定挂载在 Docker Desktop 语义下可写；`/data` 挂成只读时进程不退出，但会点名降级（检查点与审计都不落盘——这种状态不能当可上线）。
- 非回环绑定门槛在容器内同样生效：缺 `TT_ALLOW_REMOTE_BIND=1` 或缺凭据都是退出码 2 并打印中文原因（拒绝来自进程本身；演练镜像烤进了放行开关，需显式覆盖才生效）。
- Nginx 反代（`deploy/nginx.conf`）：80 端口只做 301 跳转；自签 CA 链校验通过，不信任该 CA 时握手被拒（TLS 不是明文冒充）；错误令牌经 TLS 仍 401；SSE 首包实测 527.9ms；事件不被攒包（经代理 18 分块 / 跨度 17.5s，与容器内直连同一量级）；**单条 SSE 流经代理连续 757 秒未断**，期间收到 50 次 15s keepalive。
- Caddy 反代（`deploy/Caddyfile`）：同一份自签证书完成 TLS 并透传 `/health`；错误令牌同样 401；SSE 首包 525.2ms 且未被压缩（`@compressible` 显式排除流式路径）；`flush_interval -1` 逐块下发（18 分块 / 9.4s）；撑过 150 秒模型静默（实测 157s）。
- 断线找回：客户端中途断开后任务仍在服务端继续产出，并可经 `GET /api/events/replay` 补齐（实测断开后 16.7 秒取回 seq 13–20；收尾后 seq 1–20 连续且以执行流的 `done` 结束）。`POST /api/chat/review` 的 `resume` 对仍在执行的任务返回 409 `CHECKPOINT_TASK_RUNNING`，对已正常完成的任务返回 409 `CHECKPOINT_UNAVAILABLE`——同一任务不会跑两遍。
- 部署自检脚本 `scripts/deploy-selfcheck.mjs`：经真实 nginx TLS 反代 11/11 全绿（含完成一次端到端对话）；对「空闲超时 5s」的反代配置能抓到并非零退出，输出可定位的中文原因并点名 `proxy_read_timeout`；拒绝把令牌写进命令行参数。
- 复跑：`python scripts/deploy_drill.py --report ../docs/服务化部署演练报告.md`（约 40 分钟，D 组那条流本身就要 12 分钟以上）；快速回归 `--skip-long`，单组 `--only C F`。

**未验证**（未跑过的组合不得当支持承诺）

- 公网域名与真实 CA（Let's Encrypt 等）签发链：只有演练自签 CA。
- 干净主机 15 分钟内从零部署：演练复用本机已缓存的镜像层，`docker build` 的真实耗时没计入。
- 真实厂商模型经服务化链路的对话质量与长任务表现：演练全程用假模型端点。
- Linux 生产宿主上的绑定挂载属主语义：Docker Desktop 默认放行宿主目录权限，不代表 Linux 行为。
- 小时级长期运行、多用户并发、引擎重启后的事件找回：演练只实证了单条流 757 秒。

**已实测到的边界（按这个口径措辞，别写成「事件全可回放」）**

- 事件重放窗口按**时间**裁剪：`REPLAY_WINDOW_SECONDS = 600`，超龄条目 drop-oldest 丢弃而 `windowExpired` 仍报 `False`。那条 757 秒的流收尾后实时流有 18 条事件，`from_seq=0` 只找回最近 10 条——任务超过 10 分钟后早期事件无法经重放找回（磁盘检查点不受此限）。
- 真正掐断 SSE 的是**空闲超时**，不是缓冲开关：只把 `proxy_read_timeout` 压到 5s，流就在 5s 处断（`IncompleteRead`）；反过来删掉模板里那五行无缓冲配置、超时仍 3600s，流照旧分批到达。五行是兜底，不是唯一防线。
- nginx 会消化上游的 `X-Accel-Buffering: no`，客户端侧看不到这个头：不能用它反证「代理没攒包」，要看分块到达时刻。
- plan 流在模型收尾前不发任何事件，所以「SSE 首包 ≤2s」实测衡量的是代理 + 引擎的转发开销；真实厂商模型的首 token 延迟不在这个数字里。

未验证的部分不应当作“已经可上线”；如果容器构建失败，以引擎直跑方式为准，不要把未跑通的镜像当成承诺。

## 一、本地：引擎 + 静态前端（已推荐用于自测）

```bash
cd tongtian-tauri
npm ci
# 必须显式关闭 Mock，否则构建产物仍是 Mock 通道，不会调用引擎
VITE_MOCK=false npm run build
python -m pip install -r engine/requirements.txt

# 回环地址可不开令牌；打开 http://127.0.0.1:18000
python engine/server.py --host 127.0.0.1 --port 18000 \
  --work-dir ./sessions --web-dir ./dist
```

模型配置有两种方式：页面里在「模型设置」新增（写入 `{TONGTIANWORK_HOME 或 ~/.tongtianwork}/models.json`），或设置引擎环境变量 `TT_API_KEY` / `TT_PROVIDER` / `TT_MODEL`。页面方式不会把密钥回传到界面。

## 二、服务化：令牌 + 静态前端

```bash
# 令牌由部署方的密钥管理生成并保存，不要写进仓库、镜像或命令行参数
export TT_ENGINE_TOKEN='<由密钥管理系统注入>'
export TT_ALLOW_REMOTE_BIND=1

python engine/server.py --host 0.0.0.0 --port 18000 \
  --work-dir /data/work --web-dir ./dist
```

打开 `http://<主机>:18000`。首次 API 请求会因 401 弹出「引擎需要服务令牌」，输入
`TT_ENGINE_TOKEN` 即可；令牌只保存在当前标签页 `sessionStorage`，关闭标签页失效，
不写 `localStorage`、不写 URL、不进构建产物。

若由反向代理统一注入 `x-tt-engine-token`，页面不会看到 401，也就不需要输入；此时
必须保证代理不会把该头透传给未授权调用方，并在代理层终止 TLS。

### 接入账号服务 JWT（可选）

```bash
# 与账号服务 TT_SERVER_JWT_SECRET 相同的密钥；也可沿用旧变量 TT_AUTH_JWT_SECRET
export TT_ENGINE_JWT_SECRET='<与账号服务一致的 HS256 密钥>'
export TT_ALLOW_REMOTE_BIND=1
# 可选：只允许名单内的 uid（逗号分隔）；静态令牌始终保留为应急通道
export TT_ENGINE_ALLOWED_USERS='tt-xxxxxxxx,tt-yyyyyyyy'

python engine/server.py --host 0.0.0.0 --port 18000 \
  --work-dir /data/work --web-dir ./dist
```

- 引擎校验 HS256 签名、`exp`、`iss`（默认 `tongtian-auth`，可用 `TT_ENGINE_JWT_ISSUER` 覆盖）、`aud`（默认 `tongtian-engine`，可用 `TT_ENGINE_JWT_AUDIENCE` 覆盖）、`type=access`，并要求 `sub` 与 `sid` 非空；refresh token 与升级前无 `sid` 的旧令牌一律拒绝。
- 调用方把账号服务的 access token 放进 `Authorization: Bearer`；CLI 可把同一令牌放进 `TT_ENGINE_TOKEN`（变量名只是“引擎凭据”的存放位置）。
- 每个 `/api/*` 请求写入审计：`sub/org/role/sid/method/path/status/durationMs/ip`，`GET /api/audit` 查询；默认内存保留 2000 条，设置 `TT_ENGINE_AUDIT_LOG=/data/audit.jsonl` 后追加 JSONL。
- 模型用量按 `sub` 归集：`GET /api/usage?sub=<uid>` 与 `GET /api/usage/report` 的 `users` 字段；JWT 非管理员只能看到自己的审计与用量，管理员（`role=admin`）可查全部。
- **按人隔离（事件访问面）**：`/api/chat` 首建时给会话盖章 `owner_sub`，`review`/`cancel`/`events/replay`/检查点查询按 owner 校验，跨用户返回 403 `TASK_FORBIDDEN`；磁盘检查点带 owner，重启恢复后仍按原归属隔离。管理员与桌面/无令牌主体放行，未归属的存量会话不误拦。
- **配额硬停**：仅对 JWT 主体生效——`quota.monthlyTokens` 按当月 `sub` 累计用量、`concurrentTasks` 按名下在执行任务数，超限返回 429 `QUOTA_TOKENS_EXCEEDED` / `QUOTA_CONCURRENCY_EXCEEDED` 且不执行；值为 0 或缺失表示不限制，静态令牌与桌面不带配额声明、不做硬停。
- **边界**：引擎不连接账号库，也不做组织级的数据分区——工作目录与产物仍是共享的，`USAGE_LEDGER` 只在进程内累计、重启即丢（见缺陷 D-43），所以配额计数在引擎重启后重新归零。账号侧强制下线后的失效窗口：未开启内省时只按令牌 `exp`（最长 `TT_ACCESS_TTL_SECONDS`，默认 15 分钟）；设置 `TT_ENGINE_INTROSPECT_URL` + `TT_ENGINE_INTROSPECT_TOKEN` 后按 `sid` 向账号服务内省，结果缓存 30 秒，账号服务不可达时 fail-open 放行。

**安全边界**

- 未设置任何凭据时没有用户体系：回环桌面模式下，能访问本机端口的进程都可调用 `/api/*`。设置 `TT_ENGINE_JWT_SECRET` 后每个请求都有 `sub` 审计，任务会话的事件访问也按 owner 拦跨用户请求，但工作目录与产物仍是共享的、未按 `org` 物理分区；不要把多租户隔离当作已实现。
- 静态令牌仍是全权限应急通道：任何持有者都能读写 `/data/work`、调用已启用的工具。只应在可信网络内使用，不要直接暴露到公网。
- 服务化模式没有桌面专属能力：终端 AI+、电脑操控、浏览器 AI+、本机画面、应用内 Hook 与审计视图、Tauri 文件对话框/系统应用打开、任务栏角标、独立任务窗口都依赖桌面壳，页面会如实标注不可用。
- 引擎不是操作系统级沙箱；模型调用会把任务内容发送给所选模型服务商。`run_command` / `fetch_url` 仍受工作区沙箱显式授权与白名单约束，未授权时不注册；`run_shell` 还必须在命令执行之上再单独授权，命令正文只以哈希进入审计。
- `/health` 与静态资源不需要令牌；不要在这些路径上放敏感内容。

## 三、命令行（已实现）

```bash
# 令牌只从环境变量读取，--token 会被明确拒绝
export TT_ENGINE_URL='http://127.0.0.1:18000'
export TT_ENGINE_TOKEN='<令牌>'   # 回环且未开令牌时可不设

node scripts/tt-cli.mjs doctor
node scripts/tt-cli.mjs health --json
node scripts/tt-cli.mjs chat "汇总本周待办并写入 report.md" --work-dir ./work
```

`chat` 会流式打印 plan / step / step_done / deliver / done；引擎报错或流提前结束时
以非 0 退出，不把半截结果当成功。CLI 不提供终端、电脑操控、本机画面等桌面能力。

设置 `TT_ENGINE_JWT_SECRET` 时，`TT_ENGINE_TOKEN` 也可以放账号服务签发的 access token，引擎会按 JWT 校验并记录调用主体。

## 四、Docker（已在本机演练验证）

```bash
cd tongtian-tauri
docker build -f deploy/Dockerfile -t tongtian-office:dev .

# TT_ENGINE_TOKEN 或 TT_ENGINE_JWT_SECRET 至少设置一个；否则引擎会拒绝绑定 0.0.0.0 并退出
docker run --rm -p 18000:18000 \
  -e TT_ENGINE_TOKEN='<令牌>' \
  -v tt-data:/data \
  tongtian-office:dev
```

- 接入账号服务 JWT 时，把 `TT_ENGINE_TOKEN` 换成 `-e TT_ENGINE_JWT_SECRET=<与账号服务一致的密钥>`；审计落盘可再加 `-e TT_ENGINE_AUDIT_LOG=/data/audit.jsonl`。

- 镜像内以非 root 用户运行，`/data` 是唯一需要持久化的目录：`models.json`、意识文件、IM 凭据、检查点、`work/`。
- 镜像的 `ENV` 里烤进了 `TT_ALLOW_REMOTE_BIND=1`（`CMD` 绑 `0.0.0.0:18000`，容器里这是常态），所以拦住裸奔的只剩凭据门槛：`TT_ENGINE_TOKEN` 与 `TT_ENGINE_JWT_SECRET` 一个都不给就是退出码 2 并打印中文原因。演练也实测了在容器内把该开关覆盖成空值同样拒起——默认拒绝这件事活在进程里，不是活在镜像里。
- 反代的上游写在配置里（`engine:18000`），引擎容器必须挂 `--network-alias engine`；见第六节。
- 镜像使用 `engine/requirements.txt`（跨平台直接依赖），**不是** `engine/requirements.lock.txt`；后者是 Windows 3.12 embeddable 运行时的随包锁表，Linux 容器不能照用。
- 构建期固定 `VITE_MOCK=false`；若自行改 Dockerfile 构建前端，必须保留这个设置。
- 没有 Docker 环境时请直接使用第二节的引擎直跑方式；不要把未构建过的镜像写进交付承诺。

## 五、数据与升级

- 升级前备份 `/data`（或 `TONGTIANWORK_HOME` 指向的目录）与工作目录；本仓库的迁移只做增量新增，不删除已有数据。
- 不要用 `rm -rf` 清理服务化数据目录；确需清理时先停引擎、确认路径、移入回收站/备份。
- 更换 `TT_ENGINE_TOKEN` 后，所有已打开的页面需要刷新并重新输入；CLI 重新读取环境变量即可。
- 反向代理会掐断 SSE 的是**空闲超时**，不是缓冲开关：`proxy_read_timeout` 短于模型静默时长时，流在超时点被切（演练实测 5s 配置直接 `IncompleteRead`）。模板里 `proxy_buffering off` 等五行是兜底：删掉它们、超时仍是 3600s 的话，事件照旧分批到达。

## 六、反向代理样例：Nginx 与 Caddy（均已验证）

两份模板同源同语义：TLS 在代理终止、80 只做 301 跳转、流式路径单独一段配置、静态资源照常压缩而 SSE 排除在压缩之外。

```bash
# 共用一个 bridge 网络；反代配置里的上游写的是 engine:18000，
# 所以引擎容器必须挂 --network-alias engine（容器名本身不是 engine）
docker network create tt-prod
docker volume create tt-data

# 引擎：令牌用 --env-file 注入，不要写进命令行；env 文件必须是 LF 换行
docker run -d --name engine --network tt-prod --network-alias engine \
  --env-file ./engine.env -v tt-data:/data tongtian-engine:drill

# Nginx：证书放到 /etc/nginx/tls（server.crt / server.key / ca.crt）
docker run -d --name proxy --network tt-prod \
  -v "$PWD/nginx.conf:/etc/nginx/nginx.conf:ro" \
  -v "$PWD/tls:/etc/nginx/tls:ro" \
  -p 127.0.0.1:443:443 -p 127.0.0.1:80:80 nginx:1-alpine

# 或 Caddy：同一份证书，路径是 /etc/caddy/tls
docker run -d --name proxy --network tt-prod \
  -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -v "$PWD/tls:/etc/caddy/tls:ro" \
  -p 127.0.0.1:443:443 -p 127.0.0.1:80:80 caddy:2-alpine
```

必须保留的配置（演练里逐条对照实测过）：

- Nginx：`/api/chat` 段 `proxy_buffering off; proxy_cache off; chunked_transfer_encoding on; tcp_nodelay on; gzip off;` + `proxy_read_timeout 3600s; proxy_send_timeout 3600s;`。空闲超时是决定性的一项。
- Caddy：SSE 走独立 `handle @sse`，`flush_interval -1`、`stream_timeout 0`、`transport http { read_timeout 0 write_timeout 0 }`；`encode` 用 `@compressible` 排除流式路径——给 SSE 开压缩会把事件攒在压缩缓冲区里。
- 引擎自己会发 `X-Accel-Buffering: no`，但 nginx 消化它、不会转发给客户端：别把这个头当成「代理没攒包」的证据，判断依据是事件的分块到达时刻。
- 上游主机名解析不了时 nginx 会直接退出，而 Docker 的端口转发还可能应一下——`curl` 连得上不代表代理活着，要看 `docker logs`。

证书要求（自签演练踩过的坑）：CA 必须带 `basicConstraints=critical,CA:TRUE,pathlen:0` 与 `keyUsage=critical,digitalSignature,keyCertSign,cRLSign`，服务端证书带 `keyUsage`、`extendedKeyUsage=serverAuth` 和 `subjectAltName`（含 `IP:127.0.0.1` 与 `DNS:localhost`）。缺 `keyUsage` 的 CA 会被 Python 3.12 / OpenSSL 直接拒掉，看起来像「TLS 不工作」，其实是证书链不合规。

## 七、部署自检与演练

上线后对**已经跑起来**的地址做体检（令牌只从环境变量读，拒绝 `--token`）：

```bash
export TT_ENGINE_TOKEN='<部署侧的令牌>'
node scripts/deploy-selfcheck.mjs --url https://<主机> --run-task
```

退出码 0 全绿 / 1 有失败 / 2 用法错误。演练里这套判定共 11 项，覆盖：`/health` 免令牌、静态首页同端口托管、无令牌 401 `ENGINE_UNAUTHORIZED`、带令牌 plan 流与 SSE 首包 ≤2s、事件序号单调、攒包判定（用执行流的到达节奏，plan 流一次成型判不出）、`from_seq` 重放找回、`--run-task` 端到端完成一次对话、失败时输出可定位的中文原因（含 `proxy_read_timeout` 修法提示）。

不接浏览器、不花 Token 的端到端演练（真实 nginx + TLS + 引擎镜像 + 假 OpenAI 端点，35 步）：

```bash
cd tongtian-tauri
python scripts/deploy_drill.py --report ../docs/服务化部署演练报告.md   # 约 40 分钟
python scripts/deploy_drill.py --skip-long                              # 快速回归，跳过十分钟长流
python scripts/deploy_drill.py --only C F                               # 只跑某几组
```

分组：A 镜像与卷权限、B 引擎服务化门槛、C Nginx TLS 与 SSE、D 缓冲/超时对照与十分钟长流、E 断线找回与续跑语义、F Caddy、G 自检脚本本身（含反面样例）。假模型节奏由 `deploy/drill/modelctl.py` 调（`plan_delay_s` / `exec_delay_s` / `tool_rounds` / `chunks`）。

本机没有 `tongtian-engine:drill` 时演练会就地构建一次（`npm ci` + 依赖安装，数分钟；存在则直接复用，所以复跑通常只做容器级冒烟）。也就是说：**这条命令验证的是部署链路，不重复计构建时间**——干净主机从零部署的耗时仍需在干净 Linux 主机实测后才能写进承诺。
