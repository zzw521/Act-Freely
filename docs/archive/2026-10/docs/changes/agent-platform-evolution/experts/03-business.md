# 业务专家分析报告

> 专项：任务生命周期规则完整性 / 异常场景覆盖 / 三方闭环 / 跨端一致性业务规则
> 依据文档：`docs/spec/spec-通天办公.md` v2.1（下称「主 SPEC」）、`docs/spec/spec-通天办公-云端与账号体系.md` v1.0（下称「云端 SPEC」）、`docs/spec/spec-通天办公-占位能力清单.md` v1.0（下称「占位清单」）
> 约束基线：DAU ~50K，峰值并发会话 ~2000，并行任务受上限控制，模型调用限流降级，token 成本受预算管控

---

## 1. 结论摘要

主 SPEC §3.1.1 现有 9 状态机（idle/planning/pending_review/executing/paused/delivering/done/failed/rolled_back）是单机本地形态设计，云端化存在 5 处结构性缺口：**无排队态**（Runner 容器池限额下任务无处安放，云端 SPEC §10.2 仅以「排队降级」一句带过）、**无预算暂停态**（§8 用量配额只到告警、未下沉到任务状态）、**无自动重试态**（§3.2.4 的退避重试仅作用于模型调用层，任务层 retry_count 字段缺失）、**无取消态**（AgentError 有 Cancelled 但 TaskStatus 无对应）、**无归档态**（占位清单 ArchiveView 为纯占位）。本报告给出 14 状态 + 1 执行子态的云端状态机、35 条合法转移、14 个异常场景的完整处置矩阵、4 类外部依赖闭环（模型厂商/IM/对象存储/支付配额）及 11 项缺失环节、跨端裁决与续接规则 R1-R4/C1-C4。结论：任务状态机扩展与预算-排队联动为 P0，必须先于云端 SPEC Phase B（Runner + Gateway）落地，否则「对话→审阅→执行→交付」验收闭环在配额耗尽场景必然破裂。

---

## 2. 风险清单

### 2.1 云端形态任务状态机（完整枚举）

**对 Task 实体的字段增量**（基线：主 SPEC §4.2 `Task`）：

| 新增字段 | 类型 | 用途 |
|----------|------|------|
| `queue_position` | i32 | 排队位次（queued 态展示） |
| `enqueued_at` | DateTime | 入队时间（超时判定） |
| `lease_id` | String | Runner 执行租约（executing 独占锁） |
| `lease_owner` | String | 持有租约的 Runner 实例/设备端 |
| `retry_count` | u8 | 已重试次数（上限 3） |
| `status_reason_code` | String | 状态成因码（如 BUDGET_EXHAUSTED / QUEUE_TIMEOUT / LEASE_LOST） |
| `revision` | i64 | 乐观锁版本号（跨端写裁决） |
| `archived_at` | DateTime | 归档时间 |
| `wait_reason` | enum(null) | executing 子态：`rate_limited` \| `circuit_break` \| null |

**云端 TaskStatus 枚举**（粗体为对主 SPEC §3.1.1 的增量）：

`queued` **（新）**、`planning`、`pending_review`、`executing`（含子态 `wait_reason=rate_limited/circuit_break`）、`paused`、`budget_paused` **（新）**、`retry_pending` **（新）**、`delivering`、`done`、`failed`、`rolled_back`、`cancelled` **（新）**、`archived` **（新）**。

> 设计决策：限流等待不设独立状态，以 `executing.wait_reason` 子态表达，避免状态爆炸；`idle` 在云端语义并入 `queued`（提交即入队，资源充足时排队 <100ms 对用户透明）。

**合法转移全集（35 条）**：

| # | 转移 | 触发事件 | 条件 |
|---|------|----------|------|
| 1 | (创建)→queued | chat_submit 受理 | 恒成立 |
| 2 | queued→planning | Runner 获取 lease_id | 并行任务配额有余量 |
| 3 | queued→cancelled | 用户取消 | — |
| 4 | queued→failed | QUEUE_TIMEOUT | enqueued_at 超 10min（可配） |
| 5 | planning→pending_review | Plan 事件（主 SPEC §3.1.3） | — |
| 6 | planning→retry_pending | 模型调用失败/PlanParse | retry_count<3 |
| 7 | planning→failed | 重试耗尽或不可重试错误 | — |
| 8 | planning→cancelled | 用户取消 | — |
| 9 | pending_review→executing | chat_review(approve) | — |
| 10 | pending_review→done | chat_review(reject) | status_reason_code=REJECTED |
| 11 | pending_review→planning | chat_review(modify) | 携带 StepPatch（主 SPEC §5.2） |
| 12 | pending_review→archived | REVIEW_TTL | 7 天未审阅自动归档 |
| 13 | executing→delivering | 全部 Step success | — |
| 14 | executing→paused | chat_control(Pause) | — |
| 15 | executing→retry_pending | StepFailed 可重试错误 | retry_count<3 |
| 16 | executing→failed | StepFailed 不可重试 | SandboxViolation 等安全类错误不重试 |
| 17 | executing→budget_paused | Gateway 返回 BUDGET_EXHAUSTED | — |
| 18 | executing→queued | lease 心跳超时 60s（Runner 崩溃） | 从最后 success 的 Step 恢复 |
| 19 | executing→cancelled | 用户取消 | 触发回滚栈处置（主 SPEC §3.1.4） |
| 20 | executing.wait_reason=rate_limited→正常 | 退避窗口结束 | 1s→2s→4s→8s ×4（主 SPEC §3.2.4） |
| 21 | paused→executing | chat_control(Resume) | — |
| 22 | paused→rolled_back | chat_control(RollbackTo step_id) | — |
| 23 | paused→cancelled | 用户取消 | — |
| 24 | rolled_back→executing | 从目标 Step 续跑 | — |
| 25 | rolled_back→planning | 需重新规划 | — |
| 26 | budget_paused→executing | 预算恢复/管理员提额 | 按入队 FIFO 自动恢复 |
| 27 | budget_paused→failed | BUDGET_TTL 24h 未恢复 | — |
| 28 | budget_paused→cancelled | 用户取消 | — |
| 29 | retry_pending→executing | 退避结束且资源可用 | — |
| 30 | retry_pending→failed | retry_count≥3 | — |
| 31 | delivering→done | 产物入库 Files 成功 | — |
| 32 | delivering→retry_pending | 产物上传失败 | retry_count<3，重签 presigned URL |
| 33 | delivering→failed | 产物失败不可重试 | 已产出部分标记 artifacts=partial |
| 34 | failed→queued | 用户手动重试 | retry_count 累计不重置 |
| 35 | done/failed→archived | 用户手动或 30 天自动 | 产物元数据保留，本体走存储生命周期 |

**对主 SPEC §3.1.1 的增量结论**：新增 5 状态（queued/budget_paused/retry_pending/cancelled/archived）、1 子态（wait_reason）、9 字段、26 条新转移；原 `reject→done` 保留但补 `status_reason_code`；原状态机无并发锁概念，云端必须引入 `lease_id + revision` 双机制。

### 2.2 异常场景覆盖（14 场景）

| # | 场景 | 触发条件 | 系统处理策略 | 用户可见反馈 | 恢复路径 |
|---|------|----------|--------------|--------------|----------|
| E1 | 模型全部不可用 | `call_with_fallback` 遍历全部 adapter 失败→`AllModelsUnavailable`（主 SPEC §3.2.3） | executing→retry_pending（×3）→failed；queued 任务冻结调度不丢弃；熔断 180s 后半开探测 | SSE Error 事件 code=ALL_MODELS_UNAVAILABLE；工作台横幅「模型服务暂不可用，任务已保留」 | 熔断恢复后 retry_pending 任务自动续跑；failed 任务可手动重试（转移 #34） |
| E2 | 限流触发 | 厂商 429 / P95>8s / 5min 错误率>15%（主 SPEC §3.2.4） | 指数退避 1→2→4→8s ×4；触发熔断切备用模型；组织级 Gateway QPM 超限→请求入队而非拒绝 | StepProgress 附注「模型繁忙，正在重试（第 n/4 次）」 | 退避成功后从当前 Step 断点续跑（Step 级幂等）；4 次失败转 E1 路径 |
| E3 | 组织预算耗尽 | Gateway 计量聚合 ≥ organizations 配额（云端 SPEC §8 用量与配额） | executing→budget_paused；queued 停止放行；新 chat_submit 返回 BUDGET_EXHAUSTED | 用户端横幅「组织本月额度已用完」；管理后台告警推送 org admin | admin 提额或周期重置→budget_paused 按 FIFO 自动恢复（转移 #26）；24h 未恢复转 failed |
| E4 | 用户中途离线/换端 | SSE 断连（断网/关页/切设备） | 云端 Runner 任务**不中断**；AgentEvent 写入事件存储带单调 seq；重连按 `since=seq` 补发 | 重连后顶部提示「任务在云端持续执行，已为你补齐进度」，事件回放 | 游标续接（C3）；离线 >24h 的事件存储过期则仅展示终态快照 |
| E5 | 双端并发执行同一任务 | 两端先后对同一 task 发起控制/提交 | 云端以 lease_id 独占裁决，后到方收 `TASK_ALREADY_RUNNING`（云端 SPEC §6.3），转只读订阅 | 后到端弹窗「该任务正在 {桌面端/Web 端} 执行」，仅提供查看与事件流订阅 | 持有端空闲 5min 自动释放 lease，另一端可接管（待澄清 Q2） |
| E6 | Runner 池满排队超时 | 并行任务数 > 容器池上限（峰值 2000 会话场景） | 任务驻留 queued 并更新 queue_position；交互任务优先级 > 定时任务；超 10min→failed(QUEUE_TIMEOUT) | 任务卡显示「排队中，当前第 n 位」 | 池扩容/任务释放后自动转 planning；failed 可手动重试重新入队 |
| E7 | 第三方工具调用超时 | Step 类型 mcp_tool/skill_call 超 30s→`McpServerTimeout`（主 SPEC §5.3） | Step 级重试 ×2→StepFailed→retry_pending；连续超时的 MCP server 标记 status=Error | Step 卡片标红「外部服务超时」+ 服务名 | 第三方恢复后从断点续跑；用户可在 Security 页禁用该工具后 re-plan |
| E8 | 任务产物失败 | delivering 阶段产物写入 Files 失败/presigned URL 过期 | delivering→retry_pending ×3，每次重签 URL；最终失败→failed 且 artifacts=partial | 交付面板逐文件列出失败项与原因，提供「重新交付」按钮 | 手动重新交付（仅重试失败文件）；部分产物可正常下载 |
| E9 | 附件上传被拒 | 病毒扫描不通过/超大小上限（云端 SPEC §2 Files「服务端病毒扫描」） | 上传请求直接拒绝，错误码 FILE_REJECTED，不入暂存区、不注入 Agent 上下文 | 上传组件红字提示具体原因（染毒/超限） | 用户处理文件后重新上传；任务保持 pending_review/planning 不推进 |
| E10 | 离线积压同步冲突 | 离线队列 pending_count>0 回传时 client_rev 与云端冲突；工作区/设置双端同改 | 任务/消息不可变追加无冲突（云端 SPEC §6.3）；workspace/settings last-write-wins 且桌面>Web；其余进「待处理冲突」列表 | 侧边栏「待处理冲突」红点，逐条展示两端版本 diff | 用户逐条裁决；云端保留 30 天版本可回退（云端 SPEC §10.2） |
| E11 | 会话过期/被踢下线 | access JWT(15min) 过期且 refresh 轮换失败，或 admin 设备下线（云端 SPEC §7） | SSE 断连返回 401；云端任务继续执行（归属用户不归属连接）；本地写操作入待同步队列 | 跳转登录页「登录已过期，任务仍在云端运行」 | 重新登录后 pull cursor 补齐 + 重连 SSE |
| E12 | 安全策略阻断 | SandboxViolation/DangerousOperationBlocked；组织出域硬开关开启后 Gateway 拒绝云端模型调用（云端 SPEC §7） | 当前 Step 立即失败且**不重试**；executing→failed(安全类)；硬开关场景整个组织云端任务排队冻结 | 明确违规原因与命中的策略条目 | 调整沙箱策略/联系 admin 关闭硬开关→用户对任务 re-plan |
| E13 | Runner 崩溃/容器被驱逐 | lease 心跳超时 60s（OOM/宿主机故障） | executing→queued（转移 #18）；从最后 success Step 恢复；非幂等 Step（如外部提交类）标记需人工确认 | 「执行环境已重置，将从第 n 步继续」；非幂等步弹确认框 | 自动重调度；同任务连续 2 次 lease 丢失→failed 防雪崩 |
| E14 | 计划生成解析失败 | PlanParse（主 SPEC §3.1.5），LLM 输出不合 JSON Schema | 自动 re-plan ×2（追加 schema 约束提示）→failed | 「计划生成失败，请尝试更具体的描述」+ 展示原始错误摘要 | 用户改写 goal 重新提交，或人工修改 steps（modify 路径） |

### 2.3 三方依赖业务闭环

| 依赖 | 开通 | 使用 | 失败 | 对账/补偿 | **缺失环节** |
|------|------|------|------|-----------|--------------|
| 模型厂商 | 签约→平台 Key 入 Gateway KMS（云端 SPEC §7「平台 Key 仅存 KMS」）→模型注册表登记（model_name、单价、限流参数、SLA） | Gateway 统一调用；Metering 记录 token 用量/成本并按 org/user 归因（主 SPEC §3.2.1 观测层） | 熔断降级（主 SPEC §3.2.4）；AllModelsUnavailable 任务驻留（E1） | 日级对账：平台 Metering 账单 vs 厂商账单，偏差 >1% 触发人工稽核；归因错误走成本冲正 | ①无对账任务设计；②无单价版本管理（厂商调价时历史账单口径）；③无预付额度与实时余额联动 |
| IM（钉钉/飞书） | 企业自建应用创建→webhook/bot 授权→绑定 org_id（占位清单 IMChannel P2，契约未定义） | 任务交付通知、审阅提醒、预算告警卡片推送（云端 SPEC §8 告警出口） | 发送失败进重试队列 ×3→死信；access_token 过期自动刷新 | 送达状态回执对账；死信日报告知 admin | ①回执回调接口未定义；②无消息模板审核流程（厂商合规要求）；③钉钉 20 条/分钟机器人限流的合并降级策略未定义 |
| 对象存储 | bucket 开通→生命周期策略（产物 30 天转低频/归档态任务本体清理）→presigned 策略（云端 SPEC §2「短时效」） | Files 预签名上传/下载；服务端病毒扫描；元数据同步（云端 SPEC §6.5） | 上传断点续传；URL 过期重签（E8）；sha256 完整性校验 | 容量/流量账单 vs Files 计量对账；孤儿文件（无任务引用）清理任务 | ①孤儿文件清理 job 未定义；②跨区域容灾复制策略未定义；③生命周期与任务 archived 态的联动规则未定义 |
| 支付/配额 | 订阅档位 free/pro/enterprise（云端 SPEC §4.2 organizations.plan）→开通支付渠道→发放 QuotaPlan | Gateway 实时扣减 token 成本；阈值告警（云端 SPEC §8） | 扣减失败→budget_paused（E3）；支付失败进宽限期 | 支付订单 vs 配额发放对账；双扣退款、少发补发补偿工单 | ①支付 webhook 幂等键未定义；②宽限期时长与宽限期内任务行为未定义；③降级（pro→free 到期）时进行中任务与超额数据的处置未定义 |

### 2.4 跨端一致性业务规则

**裁决规则**：

- **R1 执行权云端独占**：executing 任务的唯一控制方由 `lease_id` 决定，首个成功获取 lease 的端持有控制权；后到端收 `TASK_ALREADY_RUNNING`（对齐云端 SPEC §6.3）并自动降级为只读订阅（同一 SSE 事件流）。
- **R2 控制权移交**：持有端空闲（无控制指令）5min 后 lease 自动释放，另一端可重新获取；不支持强制抢占，避免执行中注入冲突指令。
- **R3 配置类写入**：workspace/settings 采用 last-write-wins，同秒冲突按设备优先级 桌面 > Web（对齐云端 SPEC §6.3），以 `updated_at + revision` 判定。
- **R4 追加类数据**：tasks/messages/file_ops 不可变追加，天然无冲突（对齐云端 SPEC §6.3）。

**续接规则**：

- **C1 离线写缓存**：离线期间操作写本地待同步队列（sync_meta.pending_count），联网后按 `client_rev` 单调上行；`sync_log` 以 UNIQUE(user_id, entity, entity_id, client_rev) 保证幂等（对齐云端 SPEC §4.2）。
- **C2 下行补齐**：`GET /sync/pull?since=cursor`，cursor 单调递增（对齐云端 SPEC §6.2）。
- **C3 执行中换端**：新设备以 task_id + 事件 seq 订阅 SSE 补齐，任务不因换端中断（E4）；事件存储保留 24h。
- **C4 冲突不自动裁决数据**：无法自动合并的条目进「待处理冲突」列表，禁止静默丢弃，云端保留 30 天版本（对齐云端 SPEC §10.2）。

### 2.5 风险清单汇总

| 编号 | 风险描述 | 等级 | 依据 |
|------|----------|:----:|------|
| R1 | 现有状态机无排队态，Runner 池满（峰值 2000 会话）时 chat_submit 只能报错，无排队-放行机制，Phase B 验收闭环在高峰必然破裂 | 高 | 主 SPEC §3.1.1 仅 9 状态；云端 SPEC §10.2「排队降级」无状态承接 |
| R2 | 预算管控只到「告警」（云端 SPEC §8），未下沉到任务状态，配额耗尽时 executing 任务无确定性落点，可能静默烧穿预算 | 高 | 云端 SPEC §8 用量与配额；主 SPEC §3.2 无预算信号入口 |
| R3 | 任务层无 retry_count/重试态，模型退避仅在调用层（主 SPEC §3.2.4），Step 失败后任务直接 failed，一次交付率 85% 目标（主 SPEC §9.3）云端难达成 | 高 | 主 SPEC §3.1.1/§3.2.4/§9.3 |
| R4 | lease 租约机制完全缺失，云端 SPEC §6.3 仅定义了 TASK_ALREADY_RUNNING 错误码，无锁获取/心跳/超时释放规则，E5/E13 场景无实现依据 | 高 | 云端 SPEC §6.3；主 SPEC §5.3 错误码表 |
| R5 | 用户离线后云端任务行为未定义：云端 SPEC 未明确「SSE 断连后任务是否继续」，若实现为中断则跨端体验崩塌，若继续则需事件存储（现无） | 高 | 云端 SPEC §5/§6 无断连语义；主 SPEC 事件流为进程内 broadcast（§3.1.3） |
| R6 | 错误码表（主 SPEC §5.3）缺云端必需码：BUDGET_EXHAUSTED、QUEUE_TIMEOUT、LEASE_LOST、FILE_REJECTED、SYNC_CONFLICT、PAYMENT_GRACE，前端无法精确呈现 | 中 | 主 SPEC §5.3 与本报告 E3/E6/E9/E10/E13 对照 |
| R7 | 三方闭环 11 项缺失环节（§2.3）集中在对账/补偿侧，上线后成本与账单纠纷无处理通道 | 中 | 云端 SPEC §8 仅计量展示，无对账设计 |
| R8 | pending_review 无 TTL，云端形态下用户关闭页面后任务永久悬挂，占用事件存储与列表位 | 中 | 主 SPEC §3.1.1 状态机无超时转移 |
| R9 | 取消语义缺失：AgentError::Cancelled 存在（主 SPEC §3.1.5）但无 Cancelled 状态与取消后回滚处置规则，云端长任务用户无法止损 | 中 | 主 SPEC §3.1.1/§3.1.5 |
| R10 | IM 通道（占位清单 P2）无回执与限流降级设计，预算告警/审阅提醒若只依赖 IM 单通道会静默丢失 | 低 | 占位清单 §2 IMChannel 行；云端 SPEC §8 告警无通道定义 |
| R11 | 归档态缺失导致 done/failed 任务无限累积；占位清单 ArchiveView 纯占位且依赖的 workspace 归档能力未定义生命周期联动 | 低 | 占位清单 §2 ArchiveView 行；云端 SPEC 无归档规则 |

---

## 3. 建议与优先级

| 编号 | 建议 | 优先级 | 理由与对现有设计的关系 |
|------|------|:------:|------------------------|
| B1 | 扩展 TaskStatus 为 14 状态（§2.1），新增 queued/budget_paused/retry_pending/cancelled/archived 与 9 个字段，同步修订主 SPEC §3.1.1 状态机图与 §5.3 错误码表（补 R6 所列 6 码） | P0 | 一切云端任务语义的地基；必须在云端 SPEC Phase B Runner/Gateway 开工前冻结，否则契约二次返工。是对主 SPEC §3.1.1 的增量修订而非替代 |
| B2 | 定义 lease 租约协议：获取（排队放行时签发）→心跳（30s）→超时释放（60s）→移交（R2），写入云端 SPEC §6 新增小节 | P0 | 补齐 R4；TASK_ALREADY_RUNNING（云端 SPEC §6.3）从「错误码」升级为「裁决机制」；E5/E13 依赖此协议 |
| B3 | Gateway 预算闸门：调用前余额预检 + 调用后实时扣减，超额返回 BUDGET_EXHAUSTED 驱动 budget_paused；与 organizations.plan/QuotaPlan（云端 SPEC §4.2）打通，告警从 §8 展示层下沉为状态机输入 | P0 | 补齐 R2；满足「token 成本受预算管控」硬约束；与云端 SPEC §8 是输入/输出关系而非重复 |
| B4 | 事件持久化 + seq 断点续传：AgentEvent 从进程内 broadcast（主 SPEC §3.1.3）升级为云端事件存储（保留 24h）+ SSE `since=seq` 补齐，定义「断连任务继续执行」为云端默认语义 | P0 | 补齐 R5/E4/C3；是跨端一致性的运行时前提；主 SPEC broadcast 设计在桌面端保持不变 |
| B5 | 排队调度器：queued 队列 + 优先级（交互>定时）+ queue_position 透出 + 10min QUEUE_TIMEOUT；与 Runner 容器池上限联动，作为「并行任务受上限控制」约束的落点 | P1 | 补齐 R1/E6；云端 SPEC §10.2「排队降级」的具体化；依赖 B1 的 queued 态 |
| B6 | 任务级重试策略：retry_pending 退避（对齐主 SPEC §3.2.4 的 1→2→4→8s 口径）+ retry_count≤3 + 安全类错误不重试白名单；Step 幂等键设计（非幂等 Step 人工确认，E13） | P1 | 补齐 R3；与主 SPEC §3.2.4 调用层重试形成两级重试体系，需在 SPEC 中明确边界避免双重重试 |
| B7 | pending_review TTL=7 天、budget_paused TTL=24h、事件存储 TTL=24h、冲突版本保留 30 天（后者对齐云端 SPEC §10.2），四组超时参数统一进配置表 | P1 | 补齐 R8；TTL 数值为业务建议值，需 TL 裁决（Q3） |
| B8 | 三方对账子系统：模型账单日级对账（偏差 >1% 稽核）、支付订单-配额发放对账、孤儿文件清理 job、IM 死信日报，统一每日 02:00 批处理窗口 | P1 | 补齐 R7 与 §2.3 缺失环节①③④类；是成本管控与商务结算的合规底线 |
| B9 | 新增 6 个错误码（BUDGET_EXHAUSTED/QUEUE_TIMEOUT/LEASE_LOST/FILE_REJECTED/SYNC_CONFLICT/PAYMENT_GRACE）并入主 SPEC §5.3 ErrorCode 枚举，前端按码映射文案 | P1 | 补齐 R6；错误码是前端反馈（§2.2 第四列）的载体，须与 B1 同批冻结 |
| B10 | cancelled 语义补全：取消时冻结当前 Step、按 rollbackable 标记处置回滚栈（主 SPEC §3.1.4）、已消耗 token 照常计入预算 | P1 | 补齐 R9；复用主 SPEC §3.1.4 回滚机制，仅补取消触发路径 |
| B11 | IM 通知双通道降级：IM 失败降级站内信/红点；钉钉限流时合并消息为摘要卡 | P2 | 补齐 R10；占位清单 IMChannel 为 P2，节奏对齐 |
| B12 | 归档闭环：done/failed 30 天自动 archived；archived 任务产物走对象存储生命周期（30 天转低频）；ArchiveView 从纯占位转 P1 排期 | P2 | 补齐 R11；联动占位清单 §5 第三批与 §2.3 对象存储缺失环节③ |

---

## 4. 待澄清问题

1. **排队任务是否消耗并行配额？** 即「并行任务上限」的口径是 executing 数还是 queued+executing 数。
   **推荐答案**：仅 executing（持有 lease）计数；queued 不限数量但受 10min QUEUE_TIMEOUT 自然约束，避免低峰排队被配额卡死。
2. **控制权是否支持强制接管？** E5 场景中另一端能否强制剥夺 lease。
   **推荐答案**：不支持强制抢占，仅支持「持有端空闲 5min 自动释放 + 用户显式在本端点『接管』触发对端确认弹窗」，防止执行中双端指令注入。
3. **四组 TTL 数值（pending_review 7 天 / budget_paused 24h / 事件存储 24h / 冲突版本 30 天）是否接受？**
   **推荐答案**：接受本报告建议值并全部做成组织级可配（enterprise 档可调），其中冲突版本 30 天已被云端 SPEC §10.2 隐含确认。
4. **手动重试（转移 #34）的 retry_count 是否重置？**
   **推荐答案**：不重置、跨手动重试累计，防止无限重试烧穿预算；但预算暂停恢复（#26）与 lease 丢失重调度（#18）不计数。
5. **预算暂停的计量粒度：组织级还是用户级？** 云端 SPEC §4.2 QuotaPlan 挂在 Organization，但成员个人滥用可拖垮全组织。
   **推荐答案**：预算以组织级为主闸门（对齐现有 QuotaPlan），另加成员级软限额（默认组织配额/成员数的 3 倍封顶），软限额触发仅告警不暂停。
6. **reject 的终态语义**：主 SPEC §3.1.1 为 reject→done，云端是否改为 reject→cancelled 以区分「自然完成」与「用户否决」，影响交付率统计（§9.3 的 85% 口径）。
   **推荐答案**：保留 reject→done 转移，以 status_reason_code=REJECTED 区分；一次交付率统计剔除 REJECTED 样本。
7. **支付宽限期规则**：欠费后宽限几天、宽限期内新任务是否受理。
   **推荐答案**：宽限 3 天；宽限期内已有 executing 任务可跑完当前 Step 后进 budget_paused，新任务 chat_submit 直接拒绝（PAYMENT_GRACE 提示续费）。
8. **桌面端本地执行的任务上云后状态冲突**：桌面端离线期间本地推进的任务状态与云端 Runner 上同 goal 任务并存时以谁为准。
   **推荐答案**：云端仅为执行方时以云端 lease 持有方为准；桌面端本地任务同步上行时若云端同 task_id 已在 executing，走 C4 冲突列表人工裁决，不自动覆盖。

---

*报告完。本报告全部结论基于 docs/spec 三份现有文档的章节级引用；状态机增量（§2.1）、异常矩阵（§2.2）、闭环缺失（§2.3）、跨端规则（§2.4）为云端化新增业务规则，建议并入云端 SPEC 修订稿。*
