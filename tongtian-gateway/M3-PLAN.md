# M3 实施计划：CLI 网关 API Key

- 状态：**已完成**（M3.1–M3.6；G7 9/9，见 [docs/m3-acceptance.md](docs/m3-acceptance.md)）
- 上游依据：[CONTRACT.md](CONTRACT.md) §3.2（CLI：网关 API Key）、§9（管理 API）
- 范围：`tongtian-gateway` 的 `ttg_` API Key 签发、列表、吊销与协议面鉴权；控制面 `/ui/` 模型网关页签
- 不在范围：细粒度 RBAC、审计日志外送、多副本/Redis、per-key 独立配额或限流、Key 过期后的自动续签

---

## 0. M3 的完成定义（一句话）

管理员经管理面（控制面代理）为某个 `tenant:dept:sub` 签发长期 `ttg_` Key，
明文只在创建响应出现一次；Key 在 OpenAI / Anthropic / Gemini 三个协议面按 scopes 与
模型白名单鉴权，用量按绑定主体记账并带 `apiKeyId`；吊销后下一次请求立即失效。

---

## 1. M3 锁定的决策

| # | 决策 | 理由 | 备选（不采用） |
| --- | --- | --- | --- |
| D1 | 格式 `ttg_<keyId>_<secret>`：`keyId` 12 位十六进制，`secret` 32 字节随机数的 base64url | 与契约 §3.2 一致；前缀可与 JWT 无歧义分流 | 复用 JWT（CLI 不会刷新）、UUID 明文（过长且可枚举） |
| D2 | 只存 `sha256(secret)`（hex），不存明文、不加密存储 | Key 是随机高熵串，哈希足够；主密钥泄露也带不出历史 Key | 与厂商密钥共用主密钥加密（多一层可逆面） |
| D3 | 校验失败统一 `AUTH_REQUIRED`，不区分“不存在 / 已吊销 / 已过期” | 避免把 Key 状态当成枚举预言机 | 分别返回 404 / 401（泄露 Key 是否存在） |
| D4 | 不做进程内缓存，每个请求查一次 SQLite 主键 | 单副本 + 主键查询成本可忽略；吊销立即生效（契约要求 ≤30 秒） | 30 秒缓存（吊销有窗口，且多一份失效逻辑） |
| D5 | scopes 固定为 `chat` / `embeddings` / `models` / `messages` / `gemini`，端点级校验 | 契约 §3.2 已定义；按协议面一一对应，语义清楚 | 按 HTTP 方法（过粗）、按模型能力（与白名单重叠） |
| D6 | 模型白名单匹配**请求里的 `model`**（别名或具体模型名），仍受部门策略约束 | 别名 `auto` 可直接列入白名单；不会出现“白名单写了别名却解析失败” | 只匹配解析后的具体模型（别名白名单失效） |
| D7 | Key 绑定 `tenant/dept/sub`，角色固定 `member`；`X-TT-Department` 只能等于绑定部门 | Key 是主体凭据，不应带来管理员身份；部门切换对 Key 无意义 | 允许 Key 跨部门切换（配额与审计口径混乱） |
| D8 | `expiresAt` 可选（毫秒时间戳，`0`/`null` 为永不过期），创建时必须晚于当前时间 | CLI 长期凭据仍应有上限；过期由校验阶段执行 | 强制 90 天（CLI 无法自动续签会中断） |
| D9 | 管理面新增 `GET /api-keys` 列表（契约表补充该行），列表不含哈希与明文 | 运营页需要看到“谁签了哪些 Key”；只回 id/label/绑定/状态 | 不提供列表（页面无法吊销） |
| D10 | 吊销幂等：已吊销返回原 `revokedAt`；未知 id 返回 `400 INVALID_REQUEST` | 重复点击不报错；未知 id 与 provider 路由的既有口径一致 | 物理删除（丢失审计与用量关联） |
| D11 | 控制面代理不改代码；浏览器仍只调 `/api/v1/admin/gateway/*` | M1.4 的通用转发已覆盖新路径，管理令牌依旧不下发浏览器 | 为 Key 单独开一套代理 |
| D12 | JWT 主体不受 scopes / 白名单影响（字段为空即不限制） | 保持 M1/M2 协议面行为不变 | 给 JWT 也加 scopes（需要控制面改签） |

### 1.1 端点与 scope 对照

| 协议面 | 端点 | scope |
| --- | --- | --- |
| OpenAI | `POST /v1/chat/completions` | `chat` |
| OpenAI | `POST /v1/embeddings` | `embeddings` |
| OpenAI | `GET /v1/models` | `models` |
| Anthropic | `POST /v1/messages` | `messages` |
| Anthropic | `POST /v1/messages/count_tokens` | `messages` |
| Gemini | `POST /v1beta/models/{model}:generateContent` | `gemini` |
| Gemini | `POST /v1beta/models/{model}:streamGenerateContent` | `gemini` |
| Gemini | `POST /v1beta/models/{model}:countTokens` | `gemini` |
| Gemini | `GET /v1beta/models` | `gemini` |

缺少 scope 返回 `403 FORBIDDEN`（三种协议各自的错误信封）。模型白名单不匹配返回
`403 MODEL_NOT_ALLOWED`。

### 1.2 管理接口请求/响应

`POST /gateway/v1/admin/api-keys`：

```json
{
  "label": "claude-code-laptop",
  "tenant": "acme",
  "dept": "dept-sales",
  "sub": "u-1",
  "scopes": ["chat", "models"],
  "modelAllowlist": ["auto", "chat-a"],
  "expiresAt": 1798761600000
}
```

`201`：

```json
{
  "ok": true,
  "key": {
    "id": "9f2c1a7b4e60",
    "label": "claude-code-laptop",
    "tenant": "acme",
    "dept": "dept-sales",
    "sub": "u-1",
    "scopes": ["chat", "models"],
    "modelAllowlist": ["auto", "chat-a"],
    "expiresAt": 1798761600000,
    "revokedAt": null,
    "createdAt": 1761825600000,
    "secret": "ttg_9f2c1a7b4e60_<43 位 base64url>"
  }
}
```

`GET /gateway/v1/admin/api-keys` → `{"ok": true, "keys": [ ... ]}`（无 `secret`、无哈希）。
`DELETE /gateway/v1/admin/api-keys/{keyId}` → `{"ok": true, "keyId": "...", "revokedAt": 1761825700000}`。

校验规则：

- `tenant` / `dept` / `sub` 必填，长度 1–128，不得含 `:`、`*`、空白或控制字符（配额主体用 `:` 分隔）。
- `label` 可选，≤128。
- `scopes` 非空、去重、必须全部来自固定集合。
- `modelAllowlist` 可选，≤100 项，每项 1–128。
- `expiresAt` 可选，必须为整数毫秒且大于当前时间；`null` 表示永不过期。

审计动作：`api_key.create`、`api_key.revoke`；对象为 `keyId`；只记绑定、scopes、白名单、
过期时间与吊销时间，不记明文与哈希。

## 2. 里程碑与顺序

| 里程碑 | 内容 | 完成判据 |
| --- | --- | --- |
| M3.1 | 计划与契约回填 | M3-PLAN、CONTRACT §3.2/§9/§9.1 一致 |
| M3.2 | `ApiKeyStore` + `authenticate` 接入 | 单元与 HTTP 鉴权用例通过 |
| M3.3 | scopes 与模型白名单 | 三协议面越权均被拒 |
| M3.4 | 管理路由 + `/ui/` 区块 | 页面可签发、复制一次、吊销 |
| M3.5 | 测试与 G7 | `tests/test_api_keys.py` 全过；G7 报告生成 |
| M3.6 | 文档回填与回归 | G5/G6 回归通过，README/安全复查同步 |

## 3. 残余与移交

- **单副本吊销**：没有缓存，所以单副本立即生效；将来多副本时每个副本各自查库即可，
  若改为共享缓存需补吊销传播。
- **Key 无独立配额/限流**：沿用绑定主体的部门配额；同一主体的多个 Key 共享额度。
- **scopes 不可后改**：需要变更时吊销重签；避免“权限悄悄扩大”的审计盲区。
- **审计仍可被 `X-TT-Admin-Actor` 覆盖**：与 M2 相同，可信度取决于控制面。
- **未覆盖控制面自建 Key**：控制面不保存 Key 明文，页面只负责一次性展示。
