# 通天办公服务端

通天办公的账号与在线市场服务端，提供：

- **账号登录**：注册、登录、刷新轮换、退出；兼容桌面端现有的网页验证登录（Loopback 授权码）流程。
- **在线技能市场**：技能包发布、版本管理、检索、分类、下载与收藏。
- **在线专家套件市场**：套件包发布、版本管理、指令解析、检索与下载。
- **MCP 插件市场**：MCP 连接器配置的发布、版本管理与分发（服务端只登记配置，不接触密钥值）。
- **项目协作**：项目、成员与角色、一次性邀请、共享任务、评论、结果回报与快照同步（只同步文本与产物文件名，不同步文件内容、对话或凭据）。

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
| Swagger UI | `http://127.0.0.1:8000/docs` |
| OpenAPI | `http://127.0.0.1:8000/openapi.json` |
| 健康检查 | `http://127.0.0.1:8000/health` |
| 技能列表 | `http://127.0.0.1:8000/api/v1/skills` |
| 套件列表 | `http://127.0.0.1:8000/api/v1/suites` |
| MCP 列表 | `http://127.0.0.1:8000/api/v1/mcp` |

开发阶段 `init.sh` 默认开启演示数据（`TT_SEED_DEMO=true`），幂等写入，不覆盖已有条目：

- 演示发布账号：`demo@tongtian.local`
- 密码：优先取 `TT_DEMO_PASSWORD`；未设置时随机生成，**仅在首次创建时打印一次**
- 演示内容：2 个技能、2 个专家套件、3 个 MCP 连接器

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
- `tongtian-tauri/auth-server/`（默认 `127.0.0.1:18080`）是历史过渡实现，已不在默认链路上。

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

## 5. 管理员

通过环境变量引导管理员（启动时执行）：

```bash
TT_ADMIN_EMAIL=admin@example.com
TT_ADMIN_PASSWORD=change-me-please
```

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
python tests/test_units.py     # 71 项单元断言
python tests/test_server.py    # 267 项全链路断言
```

覆盖范围：

- JWT 签名/过期、PBKDF2、标识归一、ZIP 路径穿越/软链接/条目上限、frontmatter、套件命令解析、限流、增量补列；
- 注册/登录/刷新轮换/退出、v1 与 legacy 接口、loopback 网页登录、授权码一次性；
- 令牌声明（`iss`/`aud`/`sid`/`org`/`quota`）、会话绑定吊销、管理员改声明与强制下线；
- 技能/套件发布、版本、检索、分类、下载与 SHA-256；
- 包文件被篡改（体积不一致）时下载返回 `PACKAGE_CORRUPTED`；
- MCP 配置校验与密钥值拒绝；
- 超大请求体拒绝、收藏、管理员推荐、我的发布、分页、演示数据幂等、登录限流。

测试使用系统临时目录与随机端口，退出时自动清理，不访问项目 `storage/`，不会污染真实数据。

---

## 9. 部署与安全

### 9.1 单进程要求

一次性授权码与限流器位于进程内存，因此：

- 必须使用 **单 worker**（`--workers 1`）；
- 多节点部署前，需要把授权码与限流迁移到 Redis 等共享存储；
- 重启会丢失未完成的一次性授权码（用户重新登录即可）。

### 9.2 生产配置

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

### 9.3 TLS 与反向代理

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

### 9.4 备份

- 备份 `storage/market.db` 时使用 SQLite 在线备份或先做 WAL checkpoint，避免只拷贝主文件导致不一致；
- 备份 `storage/packages/` 与数据库保持同一时间点；
- 恢复后校验包文件大小与数据库登记的 SHA-256。

### 9.5 当前边界

- 未实现邮箱验证、找回密码、第三方 OAuth、支付与套餐计费；
- 未内置病毒扫描、内容审核与举报流程；
- 不支持多租户/组织隔离，所有条目共享一个公共市场；
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
│   ├── auth.py        # 账号、会话、网页登录、legacy 兼容接口
│   ├── catalog.py     # 市场类型与分类
│   ├── config.py      # 环境变量配置
│   ├── db.py          # SQLite 建表与事务
│   ├── errors.py      # 统一业务错误
│   ├── main.py        # FastAPI 装配、异常格式、启动检查
│   ├── market.py      # 市场发布、检索、下载、收藏、推荐
│   ├── packages.py    # ZIP 校验与 SKILL.md/plugin.json 解析
│   ├── security.py    # JWT、密码、标识、限流
│   └── seed.py        # 幂等演示数据
├── tests/
│   ├── test_units.py
│   └── test_server.py
├── init.sh
├── requirements.txt
├── .env.example
└── README.md
```
