# 工单执行体端点契约（D-48.7 接线半边）

> 落地：`tongtian-tauri/engine/server.py` 的 `/api/tickets/*` 与 `_ticket_*` 段（2026-11-03 更新廿六）。
> 这一页写给**下一个接线方**（桌面 Rust 领取回路、以后的桌面界面），字段名与错误码以源码为准，
> 自测是 `tongtian-tauri/tests/integration/engine_ticket_worker_routes_selftest.py`（9 段 81 项，真 HTTP 打到真 lifespan）。
> 引擎侧的装配、槽位、外发保序等口径不在这里重复，见台账 §D-48 与 `engine/ticket_worker_service.py` 的 docstring。

## 谁调、什么时候调

工单执行体是「一单一目录、一单一领取、一轮一回合」的那套运行时。引擎只把它暴露成进程内单例
（`ticket_worker_service.get_runtime()`）；这 13 个端点是它**唯一**对外的形状。典型的领取回路是：

1. 引擎起来（lifespan 会建装配并起外发周期泵），桌面先 `GET /api/tickets/status` 看模型通道是否就绪；
2. 服务端把一条工单唤醒交给桌面 → 桌面 `POST /api/tickets/wakes`（带 `assignmentId` 与**源头**的 `arrivedAtMs`）；
3. 要结果就轮询 `GET /api/tickets/state` / `GET /api/tickets/history`，别把 `wakes` 的返回当「模型已回话」；
4. 崩溃或重启后，先 `GET /api/tickets/resume/plan`（只读）分类，再决定 `POST /api/tickets/resume`。

## 鉴权：写面只对本机，查询面对所有已鉴权主体

一个工单回合会在工单目录里 `run_shell` **起真实进程**，所以写操作只接受桌面壳（本机令牌）发起：

| 面 | 端点 | 带着远程身份（服务令牌等）时 |
| --- | --- | --- |
| 写 | `config`、`wakes`、`kick`、`resume`、`outbox/run`、`outbox/retry`、`outbox/resolve` | **403** `TICKET_WORKER_DESKTOP_ONLY`，且被拒的那条唤醒**一条也没落进任何账** |
| 查询 | `status`、`state`、`history`、`inbox`、`outbox`、`resume/plan` | 200，照常返回（只读不动盘、不起进程） |

另两条边界：引擎没配任何凭据（普通桌面/开发模式）时按本机放行；配了凭据而不带凭据的请求由全局中间件
401 `ENGINE_UNAUTHORIZED`——工单端点不例外。

## 端点逐个

请求体一律 JSON 对象；查询参数用驼峰名（与渲染层既有约定一致）。所有响应的形状是执行体返回值原样过
严格 JSON 检查（没有 `Path`／元组／集合），失败时是 `{"ok": false, "code": …, "message": …}`。

| 方法 | 路径 | 必填 | 可选 | 说清楚的那件事 |
| --- | --- | --- | --- | --- |
| GET | `/api/tickets/status` | — | — | 模型模式、槽位、在飞回合、队列与收件箱条数、通道是否接线；**不含任何密钥**，网关模式下也不出现 `accessToken`/`apiKey`/`Bearer` |
| POST | `/api/tickets/config` | 至少一项 | `modelConfig`（对象）、`agentSlots` 1–16、`humanExtraSlots` 0–8、`maxParallelTurns` 1–16、`maxFollowupTurns` 1–64、`maxToolRounds` 1–32、`goal`、`defaultSessionKey` | 空请求被拒（`TICKET_CONFIG_EMPTY`）：空请求不该悄悄重建装配。模型是**推给活通道**（回报 `modelApplied`），并发口径与数据根才是构造期事实、改了会重建并当场说明原因。换了登录态要重新推一次（换票件按 access token 摘要缓存） |
| POST | `/api/tickets/wakes` | `ticketId`、`message`、`assignmentId` | `title`、`origin`（默认 `human`）、`prevAssignmentId`、`arrivedAtMs` | 执行体的唯一投递入口。`turn-scheduled` 只表示「进箱并起了或等着起一轮」。槽满、模型没配好都在**投递之前**拒，原话就在这条响应里 |
| GET | `/api/tickets/state` | `ticketId` | — | 这一单现在什么样：worker／领取／在飞作业／未读唤醒／对外状态（`waiting`\|`working`\|…） |
| GET | `/api/tickets/history` | — | `ticketId`、`limit` 1–500 | 每轮一条：第几轮、消费几条唤醒、怎么收的口、上下文来自哪份领取（`historySource`） |
| POST | `/api/tickets/kick` | `ticketId` | `prevAssignmentId` | 手动推进一轮，把箱里压着的或滞留的唤醒带走；带 `prevAssignmentId` 才是「借旧一轮上下文」那条路 |
| GET | `/api/tickets/resume/plan` | `ticketId`、`prevAssignmentId` | `candidateWakes`（**JSON 数组字符串**） | 只读分类：`unread`／`alreadyRead`／`alreadyInInbox`／`carriedWakes` + `evidence`。**不动盘、不起回合**，所以不设本机门槛 |
| POST | `/api/tickets/resume` | `ticketId`、`prevAssignmentId`、`newAssignmentId` | `candidateWakes`（数组）、`title`、`origin` | 只投**未读**，且先投完整批再起轮。`nothing-to-resume` 不建绑定、不占槽、不调模型；槽没到手回 `not-resumed` 并说「一条也没投出去，候选还在你那边」 |
| GET | `/api/tickets/inbox` | — | `ticketId` | 耐久收件箱；`forTicket` 是**列表**不是字典 |
| GET | `/api/tickets/outbox` | — | — | 外发快照；`records` 是**条数（int）**不是列表，判 done 看 `statusCounts`（它不含计数为 0 的状态），保序组**刻意不含 done** |
| POST | `/api/tickets/outbox/run` | — | `limit` 1–64（默认 16） | 手动推一把已提交的外发。与周期泵走同一个 `run_pending`，不存在两条会分道的口径 |
| POST | `/api/tickets/outbox/retry` | `recordId` | — | 终态失败改回待办；重试仍走同一把幂等键，不会二次送达 |
| POST | `/api/tickets/outbox/resolve` | `recordId`、`action` | — | 保序组逃生口：挡住后面那条要么 `skip` 放行，要么按原样重发 |

## 错误码

| code | HTTP | 什么时候 |
| --- | --- | --- |
| `VALIDATION_ERROR` | 400 | 请求体不是合法 JSON，或顶层不是对象（数组照样拒，不猜成空对象） |
| `TICKET_FIELD_MISSING` | 400 | 上表的必填字段缺失或全空格。端点不猜默认值——猜出来的 `ticketId` 会投到别人那一单 |
| `TICKET_FIELD_INVALID` | 400 | 整数越界（`limit`、槽位数）、类型不对（`modelConfig` 不是对象、`candidateWakes` 不是数组）、`arrivedAtMs` 不是数 |
| `TICKET_CONFIG_EMPTY` | 400 | `config` 一条要改的都没给 |
| `TICKET_WORKER_DESKTOP_ONLY` | 403 | 写面 + 远程身份（见上表） |
| `ENGINE_UNAUTHORIZED` | 401 | 引擎配了凭据而请求没带（全局中间件，非工单独有） |
| `TICKET_WORKER_ERROR` | 500 | 执行体调用抛出异常，或返回的不是字典 |
| `TICKET_RESULT_UNSERIALIZABLE` | 500 | 结果里有 JSON 装不下的东西。**故意不写成「服务出错」**：这类事实往往已经落盘，说成服务出错会让调用方重投一遍 |

## 进程与配置（引擎侧，不需要调用方传）

- `TT_TICKET_OUTBOX_POLL_SECONDS`：外发周期泵的间隔，默认 `20`、下限 `1.0`；`0`/`off`/`no`/`false` 关掉（只由 `outbox/run` 手动推），乱填回落默认。泵每 tick 先 `peek_runtime()`（没装配过就跳过），**不调 `get_runtime()`**——那会按当下环境变量把活装配重建成另一套。
- 工单装配起不来**只打印、不拦引擎启动**（IM 与聊天面照旧）；关停先取消泵任务，再有界 `reset_runtime(2.0)`，仍有在飞回合或未收的进程就点名条数。
- 网关模式换票件由引擎现造：`GatewayTokenProvider.get()` 是协程，worker 线程里跨循环回投，等待上限 15 秒，换不到就**本轮不起回合**（不静默退回直连）。切换 `modelConfig` 的模式（直连 ↔ 网关）会连着换这个换票件，切回直连时换回 `None`。
- 工单回合的模型用量进同一份用量台账，`origin=ticket-worker`，带端到端 `latencyMs`（含重试退避）。后台线程没有请求上下文，归属取最近一次打工单**写**端点的主体，取不到就与桌面聊天一样归 `local`。

## 三条「别这么用」

1. **别把 `prepare_fn` 想要一个 HTTP 入口**：git worktree 那类准备动作是领取回路的进程内回调，暴露成字段等于让远程主体挑目录，所以刻意**没有**这个口子。
2. **别靠 `wakes` 的返回判成败**：回合结果只从 `state`／`history` 看；`wait_idle` 那套是进程内调度器的东西，HTTP 侧没有对应「等这一轮结束」的端点（下一批要的话再按真实需要加，别先猜）。
3. **`resume/plan` 的候选要带 `arrivedAtMs`**：已读键是（正文 + 到达时间 + 领取），没有到达时间就没有可比对的键，那几条一律按未读重投——宁可重投也不丢唤醒，但重复的外发要靠幂等键挡。

## 怎么跑这层的自测

```bash
cd tongtian-tauri
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/integration/engine_ticket_worker_routes_selftest.py
```

离线、不起服务、不碰真实数据根（每节一个临时 `TONGTIANWORK_HOME`）；没装引擎依赖时整份 SKIP 并回 0，
不会把「环境缺件」报成实现红。
