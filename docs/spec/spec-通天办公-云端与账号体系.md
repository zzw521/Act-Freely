# 通天办公 · 云端与账号体系（子系统 SPEC）

> **文档版本**：v1.0
> **创建日期**：2026-08-05
> **负责人**：架构组
> **关联 SPEC**：`docs/spec/spec-通天办公.md` v2.1（主 SPEC，§10 云端版本架构预留）
> **关联设计**：`tongtian-office-mvp-design.md` v2.1

---

## 目录

- [1. 定位与设计原则](#1-定位与设计原则)
- [2. 总体架构](#2-总体架构)
- [3. 云端技术选型](#3-云端技术选型)
- [4. 账号与数据模型](#4-账号与数据模型)
- [5. Web 端 API 契约](#5-web-端-api-契约)
- [6. 数据同步策略](#6-数据同步策略)
- [7. 安全与合规](#7-安全与合规)
- [8. 管理后台](#8-管理后台)
- [9. 分阶段路线](#9-分阶段路线)
- [10. 非功能指标与风险](#10-非功能指标与风险)

---

## 1. 定位与设计原则

**定位**：桌面端（Tauri + Rust）是产品本体，local-first 不变；云端是扩展形态，提供账号、跨端同步、网页版与团队管理能力。用户不登录云端时，全部本地能力完整可用。

**设计原则**：

1. **契约复用**：Web 端 API 与主 SPEC §5.1 的 32 个 command 一一映射（REST+SSE 等价暴露），前端业务代码零分叉
2. **密钥永不上云**：用户自备模型 API Key 仅存本机 keyring；云端模型能力经「模型网关」以平台托管 Key 提供，双轨并存
3. **离线优先**：桌面端所有写操作先落本地 SQLite，再增量同步上云；断网不影响使用
4. **文件不出域原则延续**：Web 端文件类能力走服务端受控执行或降级，不改变「敏感数据最小出域」基线（主 SPEC §6）
5. **合规延续**：遵循主 SPEC §6.5 设计原创性原则；云端 UI 沿用自有设计体系

## 2. 总体架构

```
┌─────────────────────────────────────────────────────────────┐
│              React 前端（同一套代码）                          │
│   统一 tongtianAPI 接口（主 SPEC §5 契约）                    │
│   ├─ tauri-api.ts  （桌面形态：invoke/event）                │
│   ├─ http-api.ts   （Web 形态：REST + SSE）                  │
│   └─ mock-api.ts   （开发形态）                               │
└───────────────┬─────────────────────────┬───────────────────┘
                │ Tauri IPC               │ HTTPS (JWT)
        ┌───────▼────────┐        ┌───────▼──────────────────┐
        │  桌面 Rust core │        │        云端服务集群        │
        │ （主 SPEC 全量） │        │                          │
        │  SQLite 本地库   │◄──增量同步──► 认证服务 (Auth)      │
        │  keyring 密钥    │        │  ├─ 账号/登录/OAuth      │
        └────────────────┘        │  └─ JWT 签发/刷新         │
                                   │  数据同步服务 (Sync)      │
                                   │  ├─ 任务/会话/工作区同步   │
                                   │  └─ 冲突裁决             │
                                   │  Agent 执行服务 (Runner)  │
                                   │  ├─ Web 端任务执行        │
                                   │  └─ 沙箱容器池            │
                                   │  模型网关 (Gateway)       │
                                   │  ├─ 平台托管 Key          │
                                   │  └─ 计量/限流/审计        │
                                   │  文件服务 (Files)         │
                                   │  └─ 产物/附件对象存储      │
                                   │  管理后台 API (Admin)     │
                                   └──────────────────────────┘
```

**组件职责**：

| 组件 | 职责 | 关键约束 |
|------|------|----------|
| Auth | 注册/登录（手机验证码、扫码、OAuth）、组织与成员、JWT 会话 | 密码仅存哈希（argon2id）；登录态设备管理 |
| Sync | 桌面端 ↔ 云端增量同步、冲突裁决、同步游标管理 | 幂等写入；游标单调递增 |
| Runner | Web 端任务的 Agent 执行（容器化沙箱） | 每任务独立容器；资源配额对齐主 SPEC §3.8 |
| Gateway | 平台托管模型 Key 的统一调用、计量计费、限流 | 用户永远看不到平台 Key |
| Files | 附件/产物对象存储（预签名上传/下载） | 预签名 URL 短时效；服务端病毒扫描 |
| Admin | 组织管理、用量配额、审计查询、成员权限 | 仅 org admin 角色可访问 |

## 3. 云端技术选型

| 维度 | A: Rust (axum) | B: Python (FastAPI) |
|------|----------------|---------------------|
| 与桌面端类型共享 | ts-rs/共享 crate，契约零漂移 | 需另行维护 schema |
| SSE/长连接 | tokio 原生，资源占用低 | 可行，并发成本高 |
| 团队技能延续 | 与桌面端同栈 | 需双栈维护 |
| 开发速度 | 中 | 快 |
| **推荐** | **首选（与主架构同栈、契约同步成本最低）** | Runner 内的 Agent 编排脚本可用 Python 辅助 |

**结论**：云端服务以 **Rust axum + tokio-postgres** 为主栈；Runner 容器内的可选辅助工具沿用 python-bridge 同款约束（按需、隔离）。

## 4. 账号与数据模型

### 4.1 实体关系

```
User ──*< Member >*── Organization
  │                       │
  │1──* Session           │1──* Workspace(云端镜像)
  │                       │
  └──1── SyncCursor ──*── SyncLog

Organization ──1── QuotaPlan（配额/计费档位）
Organization ──*< AuditLogCloud
```

### 4.2 核心实体（云端 PostgreSQL，DDL 概要）

```sql
CREATE TABLE users (
  id UUID PRIMARY KEY,
  phone VARCHAR(32) UNIQUE,
  email VARCHAR(255) UNIQUE,
  password_hash TEXT,              -- argon2id；OAuth 用户可空
  nickname TEXT NOT NULL,
  avatar_url TEXT,
  status TEXT NOT NULL DEFAULT 'active',  -- active | disabled
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE organizations (
  id UUID PRIMARY KEY,
  name TEXT NOT NULL,
  owner_user_id UUID NOT NULL REFERENCES users(id),
  plan TEXT NOT NULL DEFAULT 'free',      -- free | pro | enterprise
  egress_policy_json JSONB DEFAULT '{}',  -- 出域策略（硬开关云端联动，见 §7）
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE members (
  org_id UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role TEXT NOT NULL DEFAULT 'member',    -- owner | admin | member
  joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, user_id)
);

CREATE TABLE sessions (
  id UUID PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  device_kind TEXT NOT NULL,              -- desktop | web
  device_name TEXT,
  refresh_hash TEXT NOT NULL,             -- refresh token 哈希
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 桌面端 SQLite 预留扩展（主 SPEC §10.3）
-- CREATE TABLE sync_meta (
--   entity TEXT PRIMARY KEY,      -- tasks | messages | workspaces | ...
--   cursor TEXT NOT NULL,         -- 云端同步游标
--   last_sync_at TEXT,
--   pending_count INTEGER DEFAULT 0
-- );

CREATE TABLE sync_log (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL,
  entity TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  op TEXT NOT NULL,                 -- upsert | delete
  payload JSONB NOT NULL,
  client_rev BIGINT NOT NULL,       -- 客户端单调版本号
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, entity, entity_id, client_rev)
);
```

## 5. Web 端 API 契约

### 5.1 通用约定

- 基础路径：`/api/v1`；认证：`Authorization: Bearer <JWT>`
- command → REST 映射规则：`domain_action` → `POST /api/v1/{domain}/{action}`（查询类可用 GET）
- 流式事件：`chat_submit` 返回 `{ task_id, stream_url }`，前端经 SSE（`GET /api/v1/chat/stream?task_id=`）订阅 `AgentEvent`（载荷与主 SPEC §3.1.3 完全一致）
- 错误结构复用 `AppError { code, message, details }`（主 SPEC §5.3）

### 5.2 command 映射与可用性维度（对齐主 SPEC §10.2）

| command | Web 端映射 | 可用性 | 说明 |
|---------|-----------|:------:|------|
| `chat_submit` | `POST /chat/submit` + SSE | cloud-ok | Runner 容器执行 |
| `chat_review` / `chat_control` / `chat_history` | `POST /chat/*`、`GET /chat/history` | cloud-ok | |
| `model_list/add/update/delete` | `GET/POST/PUT/DELETE /models/*` | cloud-ok | 用户自备 Key 仅存浏览器加密存储或引导回桌面端 |
| `model_test` | `POST /models/test` | cloud-ok | 经 Gateway 代理探测 |
| `file_list/read/preview` | `GET /files/*` | degraded | 仅列运行目录内云端镜像文件；本地文件系统不可达 |
| `settings_get/set` | `GET/PUT /settings` | cloud-ok | 云端保存用户偏好 |
| `workspace_*` | `POST/GET /workspaces/*` | cloud-ok | |
| `skill_list/install/...` | `GET/POST /skills/*` | cloud-ok | 脚本类技能在 Web 端仅可编排、不可本地执行 |
| `mcp_connect/...` | `POST /mcp/*` | degraded | 仅支持 streamable-http 传输；stdio 类不可用 |
| `sandbox_*` | `GET/PUT /sandbox/*` | degraded | 策略作用于 Runner 容器 |
| `agent_delegate` | `POST /agents/delegate` | cloud-ok | |
| 桌面控制/截图类 | — | local-only | Web 端入口隐藏 + 提示「桌面端专属」 |

## 6. 数据同步策略

1. **同步单元**：tasks / messages / file_ops / workspaces / skills（元数据）/ settings；模型 Key 与沙箱策略不同步（设备本地属性）
2. **增量机制**：客户端维护 `sync_meta.cursor`；上行以 `sync_log` 追加（携带 `client_rev`），下行为 `GET /sync/pull?since=cursor`
3. **冲突裁决**：
   - 任务/消息：不可变追加，无冲突
   - 工作区/设置：last-write-wins（以 `updated_at` + 设备优先级：桌面 > Web）
   - 同一任务并发控制：两端同时执行同一任务时，后提交方收到 `TASK_ALREADY_RUNNING`
4. **离线优先**：离线期间操作写入本地待同步队列（`pending_count`），联网后自动续传；冲突条目进入「待处理冲突」列表供用户裁决
5. **产物文件**：不同步文件本体，仅同步元数据；跨端访问经 Files 服务按需下载

## 7. 安全与合规

| 项 | 措施 |
|----|------|
| 认证 | JWT（access 15min + refresh 30d 轮换）；异地登录提醒；设备下线能力 |
| 传输 | 全链路 TLS 1.3；SSE 经同源 |
| 密钥边界 | 用户模型 Key 永不上云（主 SPEC §10.3）；平台 Key 仅存 Gateway 服务端 KMS |
| RBAC | org 三级角色（owner/admin/member）；管理 API 强制 admin+ |
| 出域硬开关联动 | 组织级 `egress_policy` 下发：企业开启硬开关后，该组织全部设备的云端模型调用被 Gateway 拒绝，仅本地模型可用（与桌面端硬开关语义一致） |
| 审计 | 云端审计表记录登录、同步、模型调用、管理操作；组织 admin 可查询导出 |
| Runner 隔离 | 每任务独立容器、网络出站白名单、资源配额、执行超时（对齐主 SPEC §3.8 L2 语义） |
| 合规 | 个人信息最小化收集；数据出境评估先行（遵循部署地法规）；延续主 SPEC §6.5 原创性原则 |

## 8. 管理后台

Web 管理端（org admin 视角）：

| 模块 | 能力 |
|------|------|
| 成员管理 | 邀请/移除、角色调整、用量查看 |
| 用量与配额 | 组织 token 消耗、费用、按成员/工作区聚合；配额阈值告警 |
| 审计中心 | 模型调用/数据流向/敏感操作检索导出（对齐 PRD §11） |
| 安全策略 | 出域硬开关、数据分级规则下发、MCP/Skill 组织级白名单 |
| 资产中心 | 组织级技能与连接器分发（对应占位清单中 ConnectorCenter 的云端形态） |

## 9. 分阶段路线

```
Phase A（桌面端云接入，约 4 周）
├─ Auth 服务 + 桌面端登录（Settings「账号与同步」区块）
├─ Sync 服务 v1（任务/会话/工作区单向备份上云）
└─ 验收：未登录全功能可用；登录后数据可见于云端

Phase B（网页版，约 6 周）
├─ http-api.ts 传输适配层 + Web 构建产物
├─ Runner 容器池 + Gateway v1（平台托管 Key 计量）
├─ Files 服务（附件/产物）
└─ 验收：Web 端完成「对话 → 计划审阅 → 执行 → 交付」闭环（降级项按 §5.2 标注）

Phase C（团队与管理后台，约 6 周）
├─ Organization/RBAC + Admin API
├─ 管理后台前端（成员/用量/审计/安全策略）
├─ 组织级技能与连接器分发
└─ 验收：企业组织全流程（开通 → 邀请 → 策略下发 → 审计导出）
```

## 10. 非功能指标与风险

### 10.1 指标

| 指标 | 目标 |
|------|------|
| 登录/注册 P95 | < 1s |
| 同步延迟（增量） | < 3s（正常网络） |
| Web 端首屏 | < 2s |
| SSE 事件端到端延迟 | < 500ms（服务内） |
| 云端可用性 | 99.5%（桌面端离线不受影响） |

### 10.2 风险

| 风险 | 缓解 |
|------|------|
| 双端行为不一致（契约漂移） | 契约单一来源（主 SPEC §5 + ts-rs/共享类型）；契约测试双端共用用例 |
| Runner 成本失控 | 容器池限额 + 组织配额 + 排队降级 |
| 同步冲突误删用户数据 | 冲突不自动删除，仅标记待裁决；云端保留 30 天版本 |
| Web 端成为敏感数据出域新通道 | Gateway 数据分级检测复用桌面端规则（主 SPEC §6.3）；企业硬开关联动 |
| 账号体系引入隐私合规负担 | Phase A 仅收集必要信息；隐私政策与数据清单随版本评审 |

---

*下一步：运行 `/apex-sdd:plan` 将本 spec 按 Phase A/B/C 拆解为实施任务（云端为独立工程，不影响桌面端 tongtian-tauri 主线）。*
