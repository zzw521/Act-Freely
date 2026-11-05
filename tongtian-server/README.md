# 通天办公服务端

通天办公的账号与在线市场服务端，提供：

- **账号登录**：注册、登录、刷新轮换、退出；兼容桌面端现有的网页验证登录（Loopback 授权码）流程。
- **在线技能市场**：技能包发布、版本管理、检索、分类、下载与收藏。
- **在线专家套件市场**：套件包发布、版本管理、指令解析、检索与下载。
- **MCP 插件市场**：MCP 连接器配置的发布、版本管理与分发（服务端只登记配置，不接触密钥值）。
- **项目协作**：项目、成员与角色、一次性邀请、共享任务、评论、结果回报与快照同步（只同步文本与产物文件名，不同步文件内容、对话或凭据）。
- **Web 控制台**：`/ui/` 自带纯静态市场浏览与管理员控制台，无需单独部署前端。
- **网关治理与审计**：管理角色（admin/operator/auditor）、网关管理代理读写分离、管理审计哈希链（分页/验链/NDJSON 导出）。

技术栈：FastAPI + SQLite（WAL）+ 本地文件存储。除运行依赖外，不依赖外部数据库、对象存储或消息队列。

---

## 1. 快速开始

要求：Python 3.10+，Git Bash（Windows）或任意 Bash。

```bash
cd tongtian-server
cp .env.example .env          # 可选：按需修改端口、密钥、管理员账号
bash init.sh
```

`init.sh` 会依次完成：

1. 创建 `.venv` 并安装 `requirements.txt`（已安装则跳过）；
2. 加载 `.env`，校验生产环境密钥；
3. 启动服务并在阻塞前打印访问信息。

启动后：

| 入口 | 地址 |
| --- | --- |
| 服务根路径 | `http://127.0.0.1:8000/` |
| Web 控制台 | `http://127.0.0.1:8000/ui/` |
| Swagger UI | `http://127.0.0.1:8000/docs` |
| OpenAPI | `http://127.0.0.1:8000/openapi.json` |
| 健康检查 | `http://127.0.0.1:8000/health` |
| 技能列表 | `http://127.0.0.1:8000/api/v1/skills` |
| 套件列表 | `http://127.0.0.1:8000/api/v1/suites` |
| MCP 列表 | `http://127.0.0.1:8000/api/v1/mcp` |

开发阶段 `init.sh` 默认开启演示数据（`TT_SEED_DEMO=true`），幂等写入，不覆盖已有条目：

- 演示发布账号：`demo@tongtian.local`
- 密码：优先取 `TT_DEMO_PASSWORD`；未设置时随机生成，**仅在首次创建时打印一次**
- 演示内容：当前 12 个技能、13 个专家套件、9 个 MCP 连接器（清单见 `app/seed.py` 的
  `SKILL_SEEDS` / `SUITE_SEEDS` / `_mcp_seeds()`，条目数由 `seed.SEED_COUNTS` 导出并被
  端到端测试校验），按 `app/catalog.py` 的分类口径把每个分类都铺上条目
- 技能正文放在 `app/demo_skills/<slug>/`：其中 10 条取自公开技能库
  [skills.sh](https://skills.sh/) 上的开源仓库（MIT / Apache-2.0），整目录镜像、未作改写，
  随包 `LICENSE.txt` 与 `SOURCE.txt` 记录来源仓库、上游路径、提交号与获取时间；
  `weekly-report`、`meeting-minutes` 为通天办公原创演示内容。中文摘要与分类是服务端发布
  参数，不属于上游内容
- MCP 配置取自真实存在的 npm 包或已连通验证的远程端点；凭据只登记键名与说明，值由客户端
  保存，服务端不落盘

不想要演示数据：在 `.env` 中设置 `TT_SEED_DEMO=false`，或使用全新数据目录。

> 直接运行 `uvicorn app.main:app` 时不会自动开启演示数据（配置默认值为 false）。

---

## 2. 配置项

所有配置来自环境变量，启动时固化。`.env` 会被 `init.sh` 自动加载。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TT_SERVER_ENV` | `development` | `production`/`prod` 时启用生产校验 |
| `TT_SERVER_HOST` | `127.0.0.1` | 监听地址；对外部署改为 `0.0.0.0` |
| `TT_SERVER_PORT` | `8000` | 监听端口 |
| `TT_SERVER_DATA_DIR` | `tongtian-server/storage` | SQLite 与包文件目录 |
| `TT_SERVER_JWT_SECRET` | 开发默认值 | JWT HS256 密钥；生产环境必须设置 |
| `TT_AUTH_JWT_SECRET` | 空 | 兼容旧版 Auth 服务的密钥变量，优先级低于上一项 |
| `TT_AUTH_ISSUER` | `tongtian-auth` | JWT `iss`；校验端必须一致 |
| `TT_AUTH_AUDIENCE` | `tongtian-engine` | JWT `aud`；引擎侧按此值校验 |
| `TT_ACCESS_TTL_SECONDS` | `900` | access token 有效期（15 分钟） |
| `TT_REFRESH_TTL_SECONDS` | `2592000` | refresh token 有效期（30 天） |
| `TT_GATEWAY_AUDIENCE` | `tongtian-gateway` | 网关短期票的 `aud`；只给网关，引擎不接受 |
| `TT_GATEWAY_TOKEN_TTL_SECONDS` | `900` | 网关短期票上限；实际取 `min(now+该值, access 的 exp)` |
| `TT_GATEWAY_SERVICE_TOKEN` | 空 | 网关服务令牌：拉策略与批量上报用量共用；未配置时相关接口返回 503 |
| `TT_GATEWAY_ADMIN_URL` | 空 | 网关管理 API 地址；`/api/v1/admin/gateway/*` 的转发目标，未配置时代理返回 503 |
| `TT_GATEWAY_ADMIN_TOKEN` | 空 | 代理转发时补上的 `X-TT-Admin-Token`；只留在服务端进程环境，不进浏览器 |
| `TT_GATEWAY_ADMIN_CLIENT_CERT` | 空 | 管理面启用 mTLS 时，控制面访问 `https://` 网关管理 API 的客户端证书路径 |
| `TT_GATEWAY_ADMIN_CLIENT_KEY` | 空 | 上述证书的私钥路径；仅服务账号可读 |
| `TT_GATEWAY_ADMIN_CA` | 空 | 校验网关/入口证书的 CA 文件；留空使用系统信任库 |
| `TT_GATEWAY_PROXY_TIMEOUT_SECONDS` | `10` | 管理代理连网关的连接/读取超时 |
| `TT_GATEWAY_USAGE_RETENTION_DAYS` | `30` | 网关用量判重键 `(gatewayId, requestId)` 的保留天数 |
| `TT_SERVER_JWT_KEY_MAX_AGE_SECONDS` | `2592000` | RS256 签名密钥最长使用时长，到期轮换（JWKS 保留新旧两把） |
| `TT_ALLOW_REGISTRATION` | `true` | 关闭后注册接口返回 403 |
| `TT_CORS_ORIGINS` | `*` | 逗号分隔的来源列表 |
| `TT_TRUST_PROXY` | `false` | 仅当位于可信反向代理后时开启，用于读取 `X-Forwarded-For` |
| `TT_SEED_DEMO` | `false`（`init.sh` 中默认 true） | 是否写入演示数据 |
| `TT_DEMO_PASSWORD` | 空 | 演示账号首次创建时使用的密码 |
| `TT_ADMIN_EMAIL` | 空 | 管理员邮箱；需与密码同时配置 |
| `TT_ADMIN_PASSWORD` | 空 | 管理员密码，至少 8 位 |
| `TT_ADMIN_NICKNAME` | `平台管理员` | 新建管理员账号的昵称 |
| `TT_MAX_ZIP_BYTES` | 16MB | 单次上传 ZIP 上限 |
| `TT_MAX_ZIP_ENTRIES` | `256` | ZIP 条目数上限 |
| `TT_MAX_ENTRY_BYTES` | 4MB | 单条目解压上限 |
| `TT_MAX_UNCOMPRESSED_BYTES` | 32MB | 整包解压总量上限 |
| `TT_MAX_MANIFEST_BYTES` | 256KB | `plugin.json` 上限 |
| `TT_MAX_SKILL_MD_BYTES` | 512KB | `SKILL.md` 上限 |

管理员引导规则：若邮箱不存在则创建；若已存在则只把角色提升为 `admin`，**不会修改已有密码和昵称**。

---

## 3. 账号接口

### 3.1 v1 JSON API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/auth/register` | 注册并返回 token 与 profile |
| `POST` | `/api/v1/auth/login` | 登录 |
| `POST` | `/api/v1/auth/refresh` | 刷新并轮换 refresh token |
| `POST` | `/api/v1/auth/logout` | 吊销 refresh token（幂等） |
| `GET` | `/api/v1/auth/me` | 当前账号资料（Bearer access token） |

注册示例：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email":"me@example.com","password":"secret123","nickname":"我"}'
```

响应：

```json
{
  "ok": true,
  "accessToken": "...",
  "refreshToken": "...",
  "expiresIn": 900,
  "profile": {
    "uid": "tt-...",
    "nickname": "我",
    "email": "me@example.com",
    "plan": "个人免费版",
    "role": "user",
    "org": "personal-tt-...",
    "quota": {"monthlyTokens": 100000, "concurrentTasks": 2},
    "mock": false
  }
}
```

约束：

- 密码 8~128 位，不能为空白或含控制字符；
- 昵称最长 24 字符，缺省取邮箱前缀；
- 邮箱统一小写；
- 注册与登录均有限流（返回 `429` 与 `Retry-After`）；
- access token 绑定会话（`sid` 对应 `sessions.id`）：退出、刷新轮换或管理员强制下线后，旧 access token 立即失效，不再等待 15 分钟自然过期；
- access/refresh token 均携带 `iss`/`aud`/`org`/`quota`/`sid`；`org` 与 `quota` 是签发时刻的快照，管理员修改后需重新登录才能拿到新值；
- 升级前签发的、没有 `sid` 的旧 access token 会被拒绝，用户重新登录即可。

多用户共享同一引擎时，引擎侧设置与 `TT_SERVER_JWT_SECRET` 相同的 `TT_ENGINE_JWT_SECRET`，并把 `TT_AUTH_ISSUER`/`TT_AUTH_AUDIENCE` 同步为引擎的 `TT_ENGINE_JWT_ISSUER`/`TT_ENGINE_JWT_AUDIENCE`（默认值一致）。引擎只校验签名与过期，不内省本服务，因此在本服务吊销后旧 access token 仍可用到 `exp`（最长 15 分钟）。

### 3.2 桌面端兼容：网页验证登录

桌面端现有流程（`TT_AUTH_MOCK=false` 时）与本服务端完全兼容：

1. 桌面端启动本机 loopback 回调服务，生成 `state`；
2. 打开浏览器访问 `GET /auth/login?state=...&redirect_uri=http://127.0.0.1:<port>/callback`；
3. 用户在网页注册或登录，服务端校验回调地址必须是本机（`127.0.0.1`/`localhost`/`::1`）；
4. 服务端 `302` 回跳，携带一次性 `code` 与 `state`；
5. 桌面端调用 `POST /api/auth/exchange` 用 `code/state` 换 token；
6. 刷新 token 存入系统 keyring，profile 存入本地 settings。

兼容接口：

| 方法 | 路径 | 形式 |
| --- | --- | --- |
| `GET` | `/auth/login`、`/auth/register` | HTML 页面 |
| `POST` | `/api/auth/register`、`/api/auth/login` | 表单，校验后 302 |
| `POST` | `/api/auth/exchange` | JSON `{code, state}` |
| `POST` | `/api/auth/refresh` | JSON `{refreshToken}` |
| `POST` | `/api/auth/logout` | JSON `{refreshToken}`，幂等 |

安全约束：

- 回调只允许 loopback 地址，且 `state` 长度 1~256、无控制字符；
- 授权码一次性、5 分钟过期，并与 `state`/回调地址绑定；
- 网页使用 CSP、HTML 转义与 `no-store`；
- 登录失败统一返回「邮箱或密码错误」，未知邮箱也执行一次 PBKDF2，降低枚举风险。

### 3.3 桌面端接入

桌面端默认即连接本服务，无需额外配置：

```bash
bash init.sh   # 默认 TT_AUTH_MOCK=false、TT_AUTH_SERVER_URL=http://127.0.0.1:8000
```

需要指向其他实例时：

```bash
TT_AUTH_SERVER_URL=https://auth.example.com bash init.sh
```

- 远程地址必须 HTTPS，本机回环允许 HTTP；桌面端在打开浏览器前会探活 `/health`，并要求 `service=tongtian-server`，服务未启动会直接给出可操作的错误，而不是打开一个连不上的登录页；
- `TT_AUTH_MOCK=true` 仍是离线开发开关：直接模拟登录，不连接服务端；
- `tongtian-tauri/auth-server/`（历史 `127.0.0.1:18080`）已退役：不在任何启动脚本里，直接运行会被退役守卫拒绝；它的 `auth.db` 是第二份账号库，账号数据只存在本服务的 `data/market.db`。

### 3.4 Web 控制台（/ui）

服务端自带一个纯静态的轻量控制台（`app/web/`，原生 HTML/CSS/JS，无构建步骤），直接调用前文的 API：

- **市场浏览**：技能 / 专家套件 / MCP 的分类、搜索、排序、分页，条目详情、README、版本下载、收藏与 MCP 配置查看/复制；
- **账号**：页面内登录/注册，access token 过期时用 refresh token 自动续期；令牌只存 `sessionStorage`，关闭标签页即失效；
- **管理员控制台**：推荐位开关、版本撤销/恢复、用户列表搜索与强制下线、审计与哈希链校验、模型网关、组织与部门。未登录或以普通用户登录时导航栏只有「市场」，「管理控制台」入口需以 `admin` / `operator` / `auditor` 登录后才会出现；管理员账号由 `TT_ADMIN_EMAIL` / `TT_ADMIN_PASSWORD` 初始化（本机 Docker 见 `tongtian-server/.env`）。
- **模型网关**（管理员）：网关健康与用量上报队列、provider 与密钥（新建后只显示 `last4`，明文只在创建响应里出现一次）、CLI API Key 签发/列表/吊销（明文只显示一次，吊销立即失效）、当月配额快照与调整、网关本地用量、租户策略读写；页面所有请求都走 `/api/v1/admin/gateway/*` 代理，浏览器永远拿不到网关管理令牌。
- **组织与部门**（管理员）：新建/改名部门、把成员分配进部门；改动会吊销该用户会话，需重新登录才能拿到新 claims，页面会明确提示这一点。

访问 `http://127.0.0.1:8000/ui/`。页面由 `GET /ui/*` 提供，响应附带严格 CSP（`default-src 'self'`），所有用户内容按纯文本渲染，避免注入。

---

## 4. 市场接口

### 4.1 类型与分类

| 类型 | 列表路径 | 分类 |
| --- | --- | --- |
| 技能 `skill` | `/api/v1/skills` | 效率、文档、数据、开发、运营、内容、其他 |
| 专家套件 `suite` | `/api/v1/suites` | 行政、HR、财务、律师、市场、销售、产研、运营、数据分析、供应链、设计、政务公共、其他 |
| MCP 插件 `mcp` | `/api/v1/mcp` | 数据、开发、办公、搜索、通讯、其他 |

`GET /api/v1/categories?type=skill|suite|mcp` 返回分类与条目计数。

### 4.2 浏览、检索与详情

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/{skills\|suites\|mcp}` | 列表 |
| `GET` | `/api/v1/{plural}/{id}` | 详情（含 readme、版本列表） |
| `GET` | `/api/v1/{plural}/{id}/download?version=` | 下载 ZIP；缺省为最新版 |
| `GET` | `/api/v1/mcp/{id}/config?version=` | 获取 MCP 配置；缺省为最新版 |
| `GET` | `/api/v1/revoked?type=&page=&pageSize=` | 已撤销版本的下架公告，按撤销时间倒序 |
| `POST` | `/api/v1/{plural}/{id}/star` | 收藏/取消收藏（需登录，幂等切换） |
| `GET` | `/api/v1/me/items` | 我发布的条目（需登录） |

列表参数：

- `q`：搜索名称、摘要、描述、标签（最长 100 字）；
- `category`：分类精确匹配；
- `sort`：`recommended`（默认）、`latest`、`downloads`、`stars`、`name`；
- `featured`：`true`/`false`；
- `page`（从 1 开始）、`pageSize`（1~100，默认 20）。

响应：

```json
{
  "ok": true,
  "type": "skill",
  "typeLabel": "技能",
  "items": [
    {
      "id": "weekly-report",
      "type": "skill",
      "name": "weekly-report",
      "summary": "把零散进展整理成周报",
      "category": "效率",
      "tags": ["周报", "总结"],
      "icon": "🧠",
      "author": {"uid": "tt-...", "nickname": "发布者"},
      "featured": false,
      "revoked": false,
      "latestVersion": "1.1.0",
      "versionCount": 2,
      "downloads": 3,
      "stars": 1,
      "starred": false,
      "publishedAtMs": 1760000000000,
      "createdAtMs": 1760000000000,
      "updatedAtMs": 1760000000000,
      "downloadUrl": "/api/v1/skills/weekly-report/download?version=1.1.0"
    }
  ],
  "total": 1,
  "page": 1,
  "pageSize": 20,
  "hasMore": false
}
```

详情额外返回 `description`、`readme`、`versions`、`install`；套件额外返回 `commands` 与 `packageInfo`，技能返回 `packageInfo`，MCP 返回 `mcp` 配置。

每个版本视图带 `revoked` / `revokedAtMs` / `revokedReason`；撤销版本的 `downloadUrl` 为 `null`，且 `readme` 取最近一个未撤销版本。条目摘要的 `revoked` 仅在全部版本都被撤销时为 `true`，此时 `latestVersion` 与 `downloadUrl` 均为 `null`，`versionCount` 仍保留历史总数。

下载响应带：

- `X-Checksum-Sha256`：包文件 SHA-256；
- `X-Item-Version`：版本号；
- `ETag` 与不可变缓存头。

指定版本被撤销时返回 `410 ITEM_REVOKED`；缺省下载会跳过撤销版本，全部版本都被撤销时同样返回 `410`。

### 4.3 发布技能包

ZIP 内 `SKILL.md` 必须位于根目录或单层目录内。示例：

```text
weekly-report.zip
├── SKILL.md
└── references/
    └── checklist.md
```

`SKILL.md`：

```markdown
---
name: weekly-report
description: 把一周的零散进展整理成可直接发送的周报
version: 1.0.0
category: 效率
tags: [周报, 总结]
---

# 周报生成器

## 使用方式

提供本周完成事项、风险与下周计划，按「结果 / 数据 / 风险 / 待办」输出。
```

发布：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/skills \
  -H "Authorization: Bearer $TOKEN" \
  -F package=@weekly-report.zip \
  -F summary="把零散进展整理成周报" \
  -F changelog="首次发布"
```

可选表单字段：`summary`、`description`、`category`、`tags`（逗号分隔或 JSON 数组）、`readme`、`changelog`、`icon`、`version`。

新版本：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/skills/weekly-report/versions \
  -H "Authorization: Bearer $TOKEN" \
  -F package=@weekly-report-1.1.0.zip \
  -F changelog="新增模板"
```

规则：

- 首次发布未声明版本时默认 `1.0.0`；新版本必须由包内声明或表单提供；
- 表单版本与包内声明不一致会被拒绝（`VERSION_MISMATCH`）；
- **版本不可覆盖**，重复版本返回 `409 VERSION_EXISTS`；
- 只有作者或管理员能发布新版本；
- 包内 `name` 推导出的标识必须与路径一致（`PACKAGE_ID_MISMATCH`）。

### 4.4 发布专家套件包

ZIP 内 `plugin.json` 位于根目录或单层目录内：

```json
{
  "name": "劳动用工专家套件",
  "version": "1.2.0",
  "description": "覆盖劳动合同审查、试用期风险与劳动争议应对的岗位工作流包。",
  "author": "某律师事务所",
  "category": "律师",
  "commands": [
    {"name": "contract-review", "description": "审查劳动合同的关键条款与风险点"},
    {"name": "probation-risk", "description": "评估试用期约定与解除风险"}
  ],
  "skills": [{"name": "contract-review"}, {"name": "probation-risk"}],
  "dataLinks": [{"name": "本地工作目录"}]
}
```

发布方式同技能：`POST /api/v1/suites`（multipart 字段名 `package`），新版本 `POST /api/v1/suites/{id}/versions`。

`commands` 兼容三种写法：字符串数组、`{name, description}` 数组、`{name: description}` 对象。`plugin.json` 的 `description` 会作为市场摘要来源。

### 4.5 发布 MCP 插件

MCP 市场只分发**配置**，服务端不接收、不保存任何环境变量值或请求头值。发布接口使用 JSON：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "Repo Insights MCP",
    "version": "1.0.0",
    "summary": "读取仓库 Issue 与 PR 信息",
    "description": "需要配置个人访问令牌，令牌值不会上传到市场。",
    "category": "开发",
    "tags": ["GitHub", "仓库"],
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "envKeys": [
      {
        "key": "GITHUB_PERSONAL_ACCESS_TOKEN",
        "description": "个人访问令牌",
        "required": true,
        "sensitive": true
      }
    ],
    "timeoutSecs": 30,
    "readme": "# Repo Insights MCP\n\n需要令牌。"
  }'
```

远程连接器：

```json
{
  "name": "Remote Search MCP",
  "version": "1.0.0",
  "summary": "远程搜索连接器示例",
  "transport": "streamable_http",
  "url": "https://mcp.example.com/mcp",
  "headerKeys": [
    {"key": "Authorization", "description": "Bearer <token>", "sensitive": true}
  ],
  "timeoutSecs": 20
}
```

校验规则：

- `transport`：`stdio`、`streamable_http`（兼容 `http`/`streamablehttp`）、`sse`；
- `stdio` 必须有 `command`，不能有 `url`/`headerKeys`；
- 远程连接器必须有完整 `http(s)` URL，不能有 `command`/`args`/`envKeys`，URL 中不能内嵌账号密码；
- `envKeys`/`headerKeys` 只接受 `key`、`description`、`required`、`sensitive`；
- **携带 `env`、`headers` 等值字段会返回 422**；
- `timeoutSecs` 1~300；
- `stdio` 条目返回 `requiresLocalExecution=true`、`riskLevel=high`，客户端应向用户明确提示本地执行风险。

新版本：`POST /api/v1/mcp/{id}/versions`，请求体与发布相同但**不含 `name`**。

### 4.6 包校验与标识规则

所有 ZIP 统一校验：

- 仅接受 ZIP；拒绝加密条目、软链接、绝对路径、盘符、`..` 穿越、重复路径；
- 条目数、单文件解压大小、整包解压总量均有上限；
- `SKILL.md`/`plugin.json` 必须位于根目录或单层目录内，且只能有一个；
- 解析失败返回 `400`，错误码为 `INVALID_SKILL_PACKAGE` / `INVALID_SUITE_PACKAGE`。

标识归一（与桌面端一致）：

- ASCII 名称 → 小写 slug（字母、数字、下划线，连续其他字符转 `-`，最长 64）；
- 含非 ASCII 字符的名称 → `suite-<crc32:08x>`、`skill-<crc32:08x>`、`mcp-<crc32:08x>`；
- 中文名中即使包含 `MCP` 等 ASCII 词，也走 CRC-32 回退，不会退化成 `mcp`。

---

### 4.7 项目协作

项目协作解决「不同账号、不同电脑之间交接任务」：项目所有者邀请成员，把任务指派给成员；成员在自己的桌面端打开项目，点「在本机执行」用自己的模型与工作目录执行，完成后由桌面端把结果摘要与产物文件名回传。服务端不运行 Agent，也不转发文件内容。

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/api/v1/projects` | 登录 | 我加入的项目列表（含角色、成员数、任务计数、最新 `seq`） |
| `POST` | `/api/v1/projects` | 登录 | 创建项目，创建者自动成为所有者 |
| `GET` | `/api/v1/projects/{id}` | 成员 | 项目快照：项目、成员、任务、评论、待接受邀请（仅管理员/所有者可见） |
| `POST` | `/api/v1/projects/{id}/invites` | 管理员/所有者 | 按邮箱生成一次性邀请码（明文只返回一次，7 天有效） |
| `GET` | `/api/v1/projects/{id}/invites` | 管理员/所有者 | 待接受邀请列表（不回显邀请码） |
| `POST` | `/api/v1/projects/invites/accept` | 登录 | 接受邀请；邀请邮箱必须与当前账号一致 |
| `POST` | `/api/v1/projects/{id}/members/{uid}/role` | 所有者 | 调整管理员/成员/只读角色 |
| `POST` | `/api/v1/projects/{id}/members/{uid}/remove` | 管理员/所有者 | 软移除成员，并收回其未完成任务 |
| `POST` | `/api/v1/projects/{id}/transfer` | 所有者 | 转移所有权 |
| `POST` | `/api/v1/projects/{id}/leave` | 非所有者成员 | 退出项目（所有者必须先转移） |
| `POST` | `/api/v1/projects/{id}/tasks` | 成员及以上 | 创建共享任务，可指派给非只读成员 |
| `PATCH` | `/api/v1/projects/{id}/tasks/{taskId}` | 按字段判断 | 修改标题/说明/负责人需管理员或创建者；状态可由管理员、创建者或负责人修改 |
| `POST` | `/api/v1/projects/{id}/tasks/{taskId}/comments` | 成员及以上 | 追加评论 |
| `POST` | `/api/v1/projects/{id}/tasks/{taskId}/result` | 负责人 | 回报结果摘要（`review` / `done`）与产物文件名 |
| `GET` | `/api/v1/projects/{id}/events?since=0&limit=100` | 成员 | 按游标补拉项目事件（成员变更、任务、评论、结果回报），返回 `events` / `cursor` / `hasMore` |
| `GET` | `/api/v1/projects/{id}/events/stream?since=0` | 成员 | SSE 事件流；支持 `Last-Event-ID` 续传，500ms 轮询、15 秒心跳，成员被移除时发送 `closed` 后结束 |

约束：

- 角色四级：`owner` / `admin` / `member` / `viewer`；只读成员不能创建任务、评论或执行。
- 非成员访问项目统一返回 `404 PROJECT_NOT_FOUND`，不泄露项目是否存在。
- 任务更新必须带 `version`；版本不一致返回 `409 PROJECT_CONFLICT` 与当前版本号，避免两台机器互相覆盖。
- 邀请码只存 SHA-256 摘要，一次性使用且绑定被邀请邮箱；转发给其他账号会被拒绝。
- 成员视图不返回邮箱；邀请列表仅管理员/所有者可见。
- 结果中的产物只保留文件名（服务端会剥离本地绝对路径），不同步文件内容。
- 所有查询都是快照 + `seq`；实时更新走 SSE（`events/stream`），断线后按 `since` / `Last-Event-ID` 补拉，可回退到 10 秒轮询。
- SSE 需要 `Authorization: Bearer` 头；浏览器 `EventSource` 不能自定义请求头时，客户端应使用 fetch 流式读取，或改用增量事件接口。

### 4.8 模型用量上报与合并视图

桌面端与引擎各自有本地台账（桌面 `tongtian.db` 的 `model_usage` 表、引擎 `usage/model-usage.jsonl`）。服务化部署时客户端把**已经累计好的行**按报文推上来，服务端只接收与聚合，不读取任何客户端库文件。

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| `POST` | `/api/v1/usage/report` | 登录（非管理员只能上报本人 `sub`） | 接收客户端累计好的用量行，按主键幂等入库 `usage_rollup` |
| `POST` | `/api/v1/gateway/usage` | 网关服务令牌 | 接收网关的批量事件，按 `(gatewayId, requestId)` 幂等并入按日台账 |
| `GET` | `/api/v1/usage/me?period=daily\|monthly\|total` | 登录 | 本人合并视图：`totals` / `models` / `days` / `sources` |
| `GET` | `/api/v1/usage/users/{sub}?period=…` | 管理员 | 同一份合并视图，按用户查询（与 `/me` 同一算法） |
| `GET` | `/api/v1/admin/usage/departments?period=…[&org=]` | 管理员 | 部门维度汇总，行过滤与 `/users/{sub}` 完全一致 |

上报报文（字段名与引擎 `GET /api/usage/export` 的导出行、桌面契约同名，无需改名）：

```json
{
  "source": "engine",
  "deviceId": "engine-1",
  "schemaVersion": 1,
  "rows": [{
    "sub": "uid-xxx", "org": "org-a", "modelName": "qwen-plus", "day": "2026-10-29",
    "origin": "im", "promptTokens": 40, "completionTokens": 10, "tokensUsed": 50,
    "calls": 2, "pricedCalls": 1, "unpricedCalls": 1,
    "latencyMsTotal": 300, "latencySamples": 2, "costs": [{ "currency": "CNY", "micros": 1080 }],
    "lastUsedAtMs": 1793241291839
  }]
}
```

约束：

- **不重复计数**：桌面行按 `source: "desktop"` 上报；引擎台账里明确标记为 `origin: "desktop"` 的样本（同一批调用被两边记到）在合并视图里排除，排除的数量与 token 在 `sources.engineDeduped*` 里披露。
- **不静默丢数据**：`web` / `im` / `cli` / `schedule` 的调用桌面根本没记，原样补入；没有来源标记的归入 `unknown`，照常计入总量并在 `sources.unattributedTokens` 单列。
- **幂等**：来的是累计值，按 `(source, deviceId, sub, modelName, day, origin)` 覆盖；同一账号的多台设备各算一份，互不覆盖。
- **不伪造指标**：`tokensUsed` 必须等于 `prompt + completion`，否则整批拒绝（`USAGE_TOKENS_MISMATCH`）；没有延迟样本时 `avgLatencyMs` 为 `null`，没有单价的调用只计入 `unpricedCalls`，不写成 0 元；平均耗时一律向下取整，与桌面 Rust、引擎三处同口径。
- 周期只接受 `daily` / `monthly` / `total`（UTC 日/月），非法值返回 `400 USAGE_INVALID_PERIOD` 而不是降级成全量。
- 单次上报不超过 2000 行；跨层数值一致性由客户端仓库的 `tongtian-tauri/tests/integration/engine_usage_report_selftest.py` 端到端验证。
- **经网关的调用只算一次**：`source=gateway` 的行计入合并视图并计入 `sources.gatewayRows`；客户端把同一批调用按 `origin=gateway` 再报一遍时，这些行被排除，数量与 token 在 `sources.gatewayDedupedRows/Tokens` 披露；BYOK 直连（`source=desktop`）照常计入 `desktopRows`，不受网关账本影响。

#### 网关批量上报（M1.4）

网关是经网关调用的唯一事实来源：本地 `usage_events` 落库后由后台线程每 5 秒推一批（单批最多 200 条），控制面按 `(gatewayId, requestId)` 判重后并入 `usage_rollup`（`source=gateway`、`origin=gateway`）。

```json
{
  "contractVersion": "gateway-contract.v1",
  "gatewayId": "gw-local-01",
  "batchId": "batch_3f2a…",
  "events": [{
    "requestId": "req_…", "tenantId": "acme", "departmentId": "dept-eng", "userId": "uid-xxx",
    "origin": "gateway", "protocol": "openai", "model": "qwen-plus", "modelAlias": "auto",
    "promptTokens": 12, "completionTokens": 8, "totalTokens": 20,
    "cost": "0.000028", "currency": "USD", "priceVersion": "2026-10",
    "latencyMs": 812, "status": "ok", "stream": false, "estimated": false,
    "startedAt": 1793241291027, "finishedAt": 1793241291839
  }]
}
```

- 鉴权与拉策略共用同一个服务令牌（`TT_GATEWAY_SERVICE_TOKEN`，常量时间比较）；未配置 503，令牌错误 401。
- 响应只回 `{ok, accepted, duplicates}`：重放同一批不增长台账，部分重复的批次只计入新行。
- 整批校验：任一行不合法（`origin` 不是 `gateway`、`totalTokens` 与 prompt+completion 不符、契约版本不认识、单批超过 1000 条等）整批 `400` 拒绝，不写半批——写半批会让网关以为整批失败而重传，两边账目长期对不上。
- 事件只带计量字段，不含 prompt/response；判重键保留 `TT_GATEWAY_USAGE_RETENTION_DAYS`（默认 30 天）后清理，网关重启后补报同一 `requestId` 在保留期内仍判重。

#### 部门用量报表

`GET /api/v1/admin/usage/departments?period=daily|monthly|total[&org=]` 按 `(org, dept)` 汇总，行过滤与 `/api/v1/usage/users/{sub}` 用同一套规则（同样排除 `origin=gateway` 的客户端副本与引擎 `desktop` 重复样本），因此逐部门相加等于逐用户相加。每行给 `tokensUsed`、`calls`、`avgLatencyMs`、`pricedCalls`/`unpricedCalls`、`costs`、`users`、`models`，未分配部门的行名为「未分配」。

### 4.9 组织、部门与网关身份

网关只接受控制面签发的 RS256 短期票，不再信任客户端自签的开发令牌。链路：登录拿 HS256 access token → 换票拿 RS256 网关票 → 带票调用网关。

#### JWKS 与换票

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/.well-known/jwks.json` | 无 | 当前与上一把 RS256 公钥；`Cache-Control: max-age=300` |
| `POST` | `/api/v1/auth/gateway-token` | access token | 换取网关闭环短期票 |

换票请求体可带 `{"dept": "dept-sales"}`（缺省取主部门）。响应：

```json
{
  "ok": true,
  "token": "<RS256 JWT>",
  "expiresAt": 1761826500,
  "expiresIn": 900,
  "tenant": "acme",
  "dept": "dept-sales"
}
```

- 票据 claims：`sub/sid/tenant/org/dept/depts/roles/role/iss/aud/iat/exp`；`aud` 只有 `TT_GATEWAY_AUDIENCE`。
- 有效期 `exp = min(now + TT_GATEWAY_TOKEN_TTL_SECONDS, access 的 exp)`；不提供 refresh，过期后用 access token 重新换。
- 只接受 `type=access` 且会话未吊销的令牌；个人账号（无组织）→ 403，组织内无部门 → 403，指定部门不在成员内 → 403。
- 响应 `Cache-Control: no-store`；每用户每分钟限流 60 次。
- 签名密钥落盘在 `{data_dir}/keys/jwt-rs256-<kid>.pem`，`index.json` 记录当前 `kid`；重启复用，超过 `TT_SERVER_JWT_KEY_MAX_AGE_SECONDS` 轮换，旧钥在 JWKS 中保留到下一次轮换。

#### 组织与部门（管理员）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/admin/departments` | 新建部门：`{org, id, name}`；`id` 需匹配 `^[a-z0-9][a-z0-9_-]{0,63}$` |
| `GET` | `/api/v1/admin/departments?org=` | 组织下的部门 |
| `PATCH` | `/api/v1/admin/users/{uid}` | 支持 `org`、`depts`（数组，`null` 清空）、`primaryDept` |

- 注册后的个人账号默认没有组织；管理员分配 `org` 后自动加入 `dept-default`（默认部门）。
- 改 `org`/`depts` 会吊销该用户全部会话，需要重新登录才能拿到新 claims。
- 同一部门 `id` 在不同组织下互不影响。

#### 网关策略

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/api/v1/gateway/policy?tenant=` | 服务令牌 | 网关拉取；`ETag`=`"policyVersion"`，`If-None-Match` 命中返回 304 |
| `GET` | `/api/v1/admin/gateway/policies` | 管理员 | 已配置策略的租户与版本 |
| `GET` | `/api/v1/admin/gateway/policy/{tenant}` | 管理员 | 单租户策略详情 |
| `PUT` | `/api/v1/admin/gateway/policy/{tenant}` | 管理员 | 保存策略，服务端校验 `PolicyDocument` 并自增版本 |

- 服务令牌缺失时接口返回 `503 AUTH_UNAVAILABLE`，令牌错误返回 `401 AUTH_REQUIRED`（常量时间比较）。
- 未配置策略的租户返回 `404 POLICY_NOT_FOUND`；网关据此直接拒绝请求（403），不会使用旧缓存。
- 策略结构以网关仓库 [CONTRACT.md](../tongtian-gateway/CONTRACT.md) §5 为准。

#### 网关管理代理（M1.4，M2/M3/M4 增补）

浏览器只与控制面打交道：`/api/v1/admin/gateway/*` 用管理角色 JWT 鉴权，服务端补上 `X-TT-Admin-Token`、`X-TT-Admin-Actor`（操作人 `uid`）与 `X-TT-Admin-Roles`（调用者角色；部门范围运维再带 `X-TT-Admin-Depts`/`X-TT-Admin-Tenant`）后转发到网关 `/gateway/v1/admin/*`，网关管理令牌因此永不进浏览器、不进页面源码。

- 读操作（GET）允许 admin / operator / auditor；写操作（POST/PUT/PATCH/DELETE）只允许 admin / operator；普通用户读 `403`，审计员写 `403 FORBIDDEN` 且不会发往网关。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/admin/gateway/health` | 网关健康、策略版本、provider 数量、上报队列与审计外送状态 |
| `GET` `POST` `PATCH` `DELETE` | `/api/v1/admin/gateway/providers…` | provider 与密钥管理（密钥只在创建/轮换响应里出现一次） |
| `GET` `POST` `DELETE` | `/api/v1/admin/gateway/api-keys…` | CLI API Key 签发/列表/吊销（明文只在 201 响应里出现一次，列表无明文与哈希） |
| `POST` | `/api/v1/admin/gateway/api-keys/{id}/rotate` | 轮换 Key（同一 keyId、新 secret，旧 secret 立即失效） |
| `GET` `POST` | `/api/v1/admin/gateway/audit…` | 网关审计分页/验链/NDJSON 导出（审计员可读） |
| `GET` | `/api/v1/admin/gateway/usage?tenant=&dept=&sub=&model=&period=` | 网关本地账本聚合 |
| `GET` `PATCH` | `/api/v1/admin/gateway/quotas…` | 配额快照与当月额度调整（主体 `tenant:dept:sub`，部门级用 `*`） |

- 转发规则是加法：路径穿越（`.`/`..`/反斜杠）→ `400`，请求体上限 2MB → `413`，连不上 → `502 GATEWAY_ADMIN_UNREACHABLE`，超时 → `504 GATEWAY_ADMIN_TIMEOUT`，未配置地址或令牌 → `503`；网关自身的 4xx 管理错误信封原样透传给页面。
- 网关管理面启用 mTLS 时：`TT_GATEWAY_ADMIN_URL` 用 `https://` 并配置 `TT_GATEWAY_ADMIN_CLIENT_CERT/KEY/CA`；证书/私钥不可读 → `503 GATEWAY_ADMIN_TLS_CONFIG`，握手失败 → `502 GATEWAY_ADMIN_TLS_ERROR`。私钥只在服务端进程环境使用。
- 未登录 `401`、无管理角色 `403` 由 `require_gateway_staff` 挡在转发之前；审计员的写操作在控制面就被拦下。所有响应带 `Cache-Control: no-store`。
- 写操作（POST/PUT/PATCH/DELETE）落控制面审计 `action=gateway.admin`，请求体按脱敏规则写入（密钥字段只留 `***`），网关侧同时记自己的审计。

#### 管理审计（M4.3）

控制面把管理动作写入 `admin_audit` 哈希链（`entry_hash = sha256(prev_hash\n at_ms\n actor\n actor_role\n action\n object\n before_json\n after_json)`，首条 `prev_hash` 为 64 个 0），并提供只读接口：

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/api/v1/admin/audit?after=&limit=` | admin/operator/auditor | 按 `id` 游标分页，返回 `entries` / `nextAfter` / `headHash` |
| `GET` | `/api/v1/admin/audit/verify` | admin/operator/auditor | 全链复算；篡改或断链时 `brokenAt` 指向首条异常行 |
| `GET` | `/api/v1/admin/audit/export?after=&limit=` | admin/operator/auditor | NDJSON，响应头 `X-TT-Audit-Head` / `X-TT-Audit-Next`，`no-store` |

- 审计只记动作、对象与脱敏后的前后值（`secret`/`token`/`password`/`authorization` 等替换为 `***`，超过 4KB 标注截断），不保存登录令牌与密钥明文。
- 查询审计本身不写审计；角色变更写 `user.role`，网关代理写操作写 `gateway.admin`。

## 5. 管理员

通过环境变量引导管理员（启动时执行）：

```bash
TT_ADMIN_EMAIL=admin@example.com
TT_ADMIN_PASSWORD=change-me-please
```

用户列表（管理员）：

```http
GET /api/v1/admin/users?q=&page=1&pageSize=20
Authorization: Bearer <admin access token>
```

返回 `uid`/`email`/`nickname`/`role`/`plan`/`org`/`createdAtMs`，不包含密码哈希；`q` 按邮箱或昵称模糊匹配（最长 100 字），普通用户返回 403。

推荐条目：

```http
POST /api/v1/admin/items/{skill|suite|mcp}/{id}/feature
Authorization: Bearer <admin access token>
Content-Type: application/json

{"featured": true}
```

普通用户返回 403。

版本撤销与恢复（管理员）：

```http
POST /api/v1/admin/items/{skill|suite|mcp}/{id}/versions/{version}/revoke
Authorization: Bearer <admin access token>
Content-Type: application/json

{"reason": "存在越权读取，禁止安装"}
```

```http
POST /api/v1/admin/items/{skill|suite|mcp}/{id}/versions/{version}/unrevoke
Authorization: Bearer <admin access token>
```

- `reason` 必填，去除首尾空白后 1~500 字；返回 `changed` 与 `revokedAtMs`；
- 重复撤销或重复恢复均为幂等：返回 `200` 且 `changed=false`，不覆盖首次撤销时间与原因；
- 撤销只影响版本分发，不删除包文件与历史行；恢复后版本重新可下载；
- 忘记版本号时可用 `GET /api/v1/revoked` 查询当前下架公告。

用户组织与配额（管理员）：

```http
PATCH /api/v1/admin/users/{uid}
Authorization: Bearer <admin access token>
Content-Type: application/json

{"org": "acme", "quota": {"monthlyTokens": 500000, "concurrentTasks": 4}}
```

- `org` 为 1~64 位小写字母、数字、下划线或连字符；传 `null` 或空串表示回到个人组织 `personal-<uid>`；
- `quota` 支持 `monthlyTokens`（0~10 亿）与 `concurrentTasks`（1~100），未提供的键沿用默认值；传 `null` 表示清空覆盖、回到角色默认；
- 普通用户默认 `{monthlyTokens: 100000, concurrentTasks: 2}`，管理员默认 `{monthlyTokens: 2000000, concurrentTasks: 8}`；
- 至少提供 `org` 或 `quota` 之一，否则返回 `400 NOTHING_TO_UPDATE`；
- 修改成功后吊销该用户全部活跃会话，响应返回 `sessionsRevoked`；旧令牌立即失效，重新登录后携带新声明。

管理角色（管理员，M4.2）：

```http
PATCH /api/v1/admin/users/{uid}
Authorization: Bearer <admin access token>
Content-Type: application/json

{"role": "operator"}
```

- `role` 只接受 `admin` / `operator` / `auditor` / `user`；非法值返回 `400 INVALID_ROLE`；
- 管理员不能修改自己的角色（`400 SELF_ROLE_CHANGE`），避免误降权把自己锁在门外；
- 角色写入 JWT 快照，变更后吊销该账号全部会话（响应 `sessionsRevoked`），旧令牌立即失效；
- 角色变更写审计 `user.role`（前后值 + 操作人）。网关管理代理按角色区分只读/可写，见 §4.9。

强制下线（管理员）：

```http
POST /api/v1/admin/users/{uid}/revoke-sessions
Authorization: Bearer <admin access token>
```

返回 `{"ok": true, "revoked": <吊销会话数>}`；`uid` 不存在返回 `404 USER_NOT_FOUND`。

---

## 6. 错误格式

所有业务错误统一为：

```json
{
  "ok": false,
  "code": "INVALID_MCP_CONFIG",
  "message": "stdio 连接器必须填写 command"
}
```

常见错误码：

| 错误码 | HTTP | 场景 |
| --- | --- | --- |
| `VALIDATION_ERROR` | 422 | 字段缺失、类型错误或包含未允许字段 |
| `AUTH_REQUIRED` | 401 | 未登录或 access token 无效 |
| `INVALID_CREDENTIALS` | 401 | 邮箱或密码错误 |
| `EMAIL_TAKEN` | 409 | 邮箱已注册 |
| `INVALID_REFRESH` | 401 | refresh 无效、过期或已吊销 |
| `RATE_LIMITED` | 429 | 触发限流，带 `Retry-After` |
| `INVALID_SKILL_PACKAGE` / `INVALID_SUITE_PACKAGE` | 400 | 包结构或内容不合法 |
| `ITEM_EXISTS` | 409 | 同名条目已存在 |
| `VERSION_EXISTS` | 409 | 版本已存在（版本不可覆盖） |
| `PACKAGE_ID_MISMATCH` | 400 | 包标识与路径不一致 |
| `INVALID_MCP_CONFIG` | 400 | MCP 配置不合法 |
| `NO_PACKAGE` | 404 | MCP 没有可下载包 |
| `ITEM_NOT_FOUND` / `VERSION_NOT_FOUND` | 404 | 条目或版本不存在 |
| `FORBIDDEN` | 403 | 非作者/非管理员 |
| `PACKAGE_TOO_LARGE` | 413 | ZIP 超过上限 |
| `PAYLOAD_TOO_LARGE` | 413 | 非 multipart 请求体超过 2MB |
| `PACKAGE_MISSING` | 410 | 包文件不存在 |
| `PACKAGE_CORRUPTED` | 410 | 包文件大小与登记不一致 |
| `ITEM_REVOKED` | 410 | 版本已被撤销，禁止下载该版本或获取其 MCP 配置 |
| `USER_NOT_FOUND` | 404 | 管理员操作的用户不存在 |
| `INVALID_ORG` | 400 | 组织标识不合法 |
| `INVALID_QUOTA` | 400 | 配额超出允许范围 |
| `NOTHING_TO_UPDATE` | 400 | 管理员更新请求未提供任何字段 |

---

## 7. 存储与数据保护

```text
storage/
├── market.db
└── packages/
    ├── skill/
    │   └── <item_id>/<version>-<随机串>.zip
    └── suite/
        └── <item_id>/<version>-<随机串>.zip
```

- SQLite 开启 WAL 与外键；
- 表结构只做增量变更（`CREATE TABLE IF NOT EXISTS` + 必要的 `ALTER TABLE ADD COLUMN`）；
- 版本包写入采用临时文件 + 原子替换，文件名为不可变版本文件；
- 取消收藏采用 `active` 软删除，不物理删除行；
- 版本撤销写入 `item_versions.revoked_at_ms/revoked_reason/revoked_by`，只做字段更新，不删除版本、包文件与下载计数；
- 过期会话保留为审计记录，不做运行时批量删除；如需归档，建议独立迁移任务并先备份；
- 市场包文件只落本地磁盘，不执行包内任何代码；MCP 命令仅登记，不在服务端启动。

---

## 8. 测试

```bash
cd tongtian-server
python tests/test_units.py         # 76 项单元断言
python tests/test_server.py        # 306 项全链路断言
python tests/test_identity.py      # 71 项身份与策略断言（JWKS/换票/组织部门/策略分发）
python tests/test_gateway_usage.py # 35 项网关用量接收断言（幂等批量、合并视图、部门报表、保留期清理）
python tests/test_gateway_admin.py # 28 项网关管理代理断言（鉴权、透传、审计脱敏、路径校验）
python tests/test_m4_audit.py      # 22 项 M4 审计哈希链断言（分页、验链、导出、角色边界、篡改定位）
```

覆盖范围：

- JWT 签名/过期、PBKDF2、标识归一、ZIP 路径穿越/软链接/条目上限、frontmatter、套件命令解析、限流、增量补列；
- 注册/登录/刷新轮换/退出、v1 与 legacy 接口、loopback 网页登录、授权码一次性；
- 令牌声明（`iss`/`aud`/`sid`/`org`/`quota`）、会话绑定吊销、管理员改声明与强制下线；
- 用量上报幂等入库、按 JWT 到人的合并视图（引擎 `desktop` 样本去重、`unknown` 单列披露、多设备互不覆盖）；
- 网关批量上报的 `(gatewayId, requestId)` 幂等（重放、部分重复、整批拒绝）、合并视图的 `gatewayRows`/`gatewayDedupedRows` 口径、部门报表与逐用户明细同源、判重键 30 天清理；
- 管理代理：未登录 401 / 非管理员 403 / 未配置 503、路径穿越与体积上限、网关错误信封透传、审计脱敏（密钥只留 `***`）、`no-store`；
- M4 管理审计：角色变更入链、游标分页、全链验链、篡改定位、NDJSON 导出与头哈希、审计员可读 / 普通用户 403、未登录 401；
- JWKS 形状、换票（部门选择、主部门兜底、无组织/无部门拒绝、会话吊销、`no-store`）、RS256 轮换、组织/部门管理、策略服务令牌与 ETag/304；
- 技能/套件发布、版本、检索、分类、下载与 SHA-256；
- 包文件被篡改（体积不一致）时下载返回 `PACKAGE_CORRUPTED`；
- MCP 配置校验与密钥值拒绝；
- 超大请求体拒绝、收藏、管理员推荐、我的发布、分页、演示数据幂等、登录限流。

测试使用系统临时目录与随机端口，退出时自动清理，不访问项目 `storage/`，不会污染真实数据。

---

## 9. 部署与安全

### 9.1 容器部署（Docker Compose）

镜像固定单进程运行（`--workers 1`），只持久化 `/data`（SQLite 与包文件），以非 root 用户启动，并内置 `GET /health` 健康检查。

```bash
cd tongtian-server
cp .env.example .env
# 生成 JWT 密钥写入 .env（生产模式会校验密钥非默认且不短于 16 字符）
SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
sed -i "s|^TT_SERVER_JWT_SECRET=.*|TT_SERVER_JWT_SECRET=${SECRET}|" .env
sed -i "s|^TT_SERVER_ENV=.*|TT_SERVER_ENV=production|" .env

docker compose up -d --build
docker compose ps                    # STATUS 显示 healthy 即就绪
curl -s http://127.0.0.1:8000/health
```

Compose 直接读取同目录 `.env`，常用变量如下：

| 变量 | 容器默认 | 作用 |
| --- | --- | --- |
| `TT_SERVER_BIND` | `127.0.0.1` | 宿主机绑定地址；直接对外时改为 `0.0.0.0`，更推荐放在反向代理后面 |
| `TT_SERVER_PORT` | `8000` | 容器监听与宿主机发布端口 |
| `TT_SERVER_ENV` | `production` | 生产模式，密钥缺失或过短会拒绝启动 |
| `TT_SEED_DEMO` | `false` | 首次启动时写入演示技能/套件数据 |

- 数据卷 `tongtian-server-data` 挂载到 `/data`，`docker compose down` 不会删除；只有 `down -v` 才会连数据库和包文件一起删除。
- 仅修改 `.env` 时执行 `docker compose up -d`；代码更新后执行 `docker compose up -d --build`。
- 备份与恢复见 9.5，容器内数据目录为 `/data`。
- 不使用 Compose 时：

```bash
docker build -t tongtian-server:0.1.0 .
docker run -d --name tongtian-server -p 127.0.0.1:8000:8000 \
  -e TT_SERVER_ENV=production \
  -e TT_SERVER_JWT_SECRET="$SECRET" \
  -v tongtian-server-data:/data \
  tongtian-server:0.1.0
```

### 9.2 单进程要求

一次性授权码与限流器位于进程内存，因此：

- 必须使用 **单 worker**（`--workers 1`）；
- 多节点部署前，需要把授权码与限流迁移到 Redis 等共享存储；
- 重启会丢失未完成的一次性授权码（用户重新登录即可）。

### 9.3 生产配置

```bash
export TT_SERVER_ENV=production
export TT_SERVER_HOST=0.0.0.0
export TT_SERVER_PORT=8000
export TT_SERVER_JWT_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export TT_AUTH_ISSUER=tongtian-auth
export TT_AUTH_AUDIENCE=tongtian-engine
export TT_CORS_ORIGINS="https://your-app.example.com"
export TT_ALLOW_REGISTRATION=true
export TT_ADMIN_EMAIL=admin@example.com
export TT_ADMIN_PASSWORD='一个足够长的密码'
export TT_TRUST_PROXY=true   # 仅当位于可信反向代理后
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

生产环境若仍使用开发默认 JWT 密钥，或密钥短于 16 个字符，应用会拒绝启动。非 multipart 的 JSON/表单请求体超过 2MB 会被直接拒绝（413）；multipart 上传由 ZIP 体积与解压上限单独约束。

### 9.4 TLS 与反向代理

服务本身不处理 TLS，建议放在 Caddy/Nginx 之后：

```nginx
server {
    listen 443 ssl;
    server_name market.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`TT_TRUST_PROXY=true` 时才会读取 `X-Forwarded-For`；不要在不可信网络直接暴露并开启该选项，否则限流可被伪造请求头绕过。

### 9.5 备份

- 备份 `storage/market.db` 时使用 SQLite 在线备份或先做 WAL checkpoint，避免只拷贝主文件导致不一致；
- 备份 `storage/packages/` 与数据库保持同一时间点；
- 恢复后校验包文件大小与数据库登记的 SHA-256。

### 9.6 当前边界

- 未实现邮箱验证、找回密码、第三方 OAuth、支付与套餐计费；
- 未内置病毒扫描、内容审核与举报流程；
- 组织/部门只用于身份与网关策略；市场条目仍共享一个公共市场，未按组织隔离；
- 未实现对象存储与多节点一致性；
- 桌面端市场页面目前仍使用本地目录/模拟数据；本服务端提供 API，客户端接入需要额外的页面与安装链路改造（见下节）；
- 技能市场安装：桌面端目前只支持本地 `SKILL.md` 安装，接入市场 ZIP 需要客户端补充解包与落盘逻辑；套件安装已支持 `zipBase64`，MCP 已有 `mcp_add` 命令，接入成本更低。

---

## 10. 对接桌面端

| 服务端能力 | 桌面端现有契约 | 接入方式 |
| --- | --- | --- |
| 登录 | `TT_AUTH_SERVER_URL` / `TT_AUTH_MOCK` | 桌面端默认连接本服务（8000）；环境变量可覆盖地址或切离线 Mock，无需改 Rust 代码 |
| 套件列表/详情 | `InstalledSuite` 形状基本一致 | 映射字段后渲染市场页 |
| 套件安装 | `SuiteInstallReq.zipBase64` | 下载服务端 ZIP → base64 → 现有安装 command |
| MCP 列表/详情 | `McpAddReq` | 将 `transport`/`command`/`args`/`url` 映射；`envKeys`/`headerKeys` 在客户端收集值 |
| 技能列表/详情 | `InstalledSkill` | 渲染市场页 |
| 技能安装 | 仅本地 `SKILL.md` | 需客户端补充 ZIP 解包与 `skills/{id}` 落盘 |

接入步骤：

1. 启动本服务端；
2. 启动桌面端（默认即连接 `http://127.0.0.1:8000`，无需设置开关）；
3. 在侧边栏或设置页点「登录 / 注册」，完成网页登录；
4. 逐步把市场页数据源从本地模拟切换到上述 API。

---

## 11. 目录结构

```text
tongtian-server/
├── app/
│   ├── auth.py        # 账号、会话、网页登录、legacy 兼容接口、JWKS、换票、部门/用户/角色管理
│   ├── audit.py       # 管理审计写入、脱敏、分页、验链与 NDJSON 导出
│   ├── audit_api.py   # /api/v1/admin/audit* 只读接口
│   ├── audit_chain.py # 审计哈希链规范化、摘要与存量回填
│   ├── roles.py       # 管理角色常量与读写判定
│   ├── catalog.py     # 市场类型与分类
│   ├── config.py      # 环境变量配置
│   ├── db.py          # SQLite 建表与事务
│   ├── errors.py      # 统一业务错误
│   ├── gateway_admin.py   # /api/v1/admin/gateway/* 管理代理：补管理令牌、透传、审计脱敏
│   ├── gateway_policy.py # 网关策略校验、按组织存储与 ETag/304 分发
│   ├── gateway_usage.py  # 网关批量用量接收（幂等）、部门用量报表
│   ├── keys.py        # RS256 签名密钥生成、持久化、轮换与 JWKS
│   ├── main.py        # FastAPI 装配、异常格式、启动检查
│   ├── market.py      # 市场发布、检索、下载、收藏、推荐
│   ├── orgs.py        # 组织与部门
│   ├── packages.py    # ZIP 校验与 SKILL.md/plugin.json 解析
│   ├── security.py    # JWT、密码、标识、限流
│   ├── seed.py        # 幂等演示数据
│   ├── usage.py       # 用量上报入库与「桌面为主、引擎补入、网关为准」的合并视图
│   └── web/           # 纯静态 Web 控制台（index.html / app.js / styles.css）
├── docker/
│   └── healthcheck.py # 容器健康检查
├── tests/
│   ├── test_identity.py
│   ├── test_gateway_admin.py
│   ├── test_gateway_usage.py
│   ├── test_m4_audit.py
│   ├── test_units.py
│   └── test_server.py
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── init.sh
├── requirements.txt
├── .env.example
└── README.md
```
