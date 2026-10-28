# Agent 平台层（Octop 四个设计思想的增量落地）

面向单用户本地运行的独立层：把「统一消息总线 / 技能包 / MCP Catalog / 通道网关」从主循环里抽出来，
复用现有 `engine/server.py` 的推理与工具逻辑，不重写 ReAct 主循环，也不引入 Octop 依赖。

- 代码位置：`tongtian-tauri/engine/agent_platform/`
- 管理入口：`tongtian-tauri/scripts/agentctl.py`
- 自测：`tongtian-tauri/tests/integration/engine_platform_selftest.py`
- 开关：默认关闭；`TT_PLATFORM=1` 或 `platform.yaml` 的 `enabled: true`
- 存量启动方式：桌面端 / HTTP / 原 IM 入口均未改动，`engine/server.py` 在本次改造中没有修改

## 1. 已有能力盘点（本次没有重复造轮子）

| 现有能力 | 位置 | 本次处理 |
| --- | --- | --- |
| ReAct 主循环、模型装配、上下文压缩 | `engine/server.py::build_agent` | 直接调用，不改一行 |
| 内置工具、`use_skill` 技能正文工具 | `engine/server.py::build_toolkit / build_skill_tool` | 复用，并在调度层追加白名单裁剪 |
| MCP stdio/HTTP 客户端构造与收尾 | `engine/mcp_runtime.py::build_client / close_all` | 直接调用，Catalog 只做注册表与形状转换 |
| IM 长连接与注册表接缝 | `engine/im_channels.py::ImChannel / ChannelRuntime.set_handler` | 包装成 `ImAdapter`，不改通道实现 |
| 文件产出快照/差异、消息文本提取 | `engine/server.py::_snapshot_files / _diff_files / _text_of` | 直接调用 |
| 专家（prompt + 技能白名单 + 模型绑定） | `engine/expert_runtime.py` | 保留；本层的 Skill 是面向单轮技能的独立能力，不复用也不替换专家体系 |
| HTTP CLI | `scripts/tt-cli.mjs` | 保留；本层新增的是 Python 侧 `agentctl`，不替换 |

结论：真正缺的是「统一消息结构 / YAML 技能包 / MCP 注册表与客户端池 / 统一的通道适配器」，
这四块以新包形式补齐，其余全部复用。

## 2. 分层与数据流

```
CLI 适配器 ─┐
IM  适配器 ─┼─→ AgentMessage ─→ MessageBus.inbound ─→ AgentDispatcher.handle
定时适配器 ─┘                                             │
                                                          ├─ SkillRegistry：热加载 + 匹配
                                                          ├─ 内置工具白名单裁剪
                                                          ├─ McpCatalog + McpClientPool：连接/发现/按技能注册
                                                          ├─ server.build_agent（存量主循环）
                                                          └─ AgentReply → 按 route_key 回投
                                                          ↓
                                     MessageBus.outbound[route_key] ─→ 各适配器取回自己的回复
```

模块边界：

| 层 | 文件 | 只负责 | 明确不做 |
| --- | --- | --- | --- |
| 消息 | `message.py` | 标准结构体、校验、入站队列、按路由隔离的出站队列 | 不认识 LLM/工具/IM 协议 |
| 技能 | `skills.py` | 加载 prompt、工具/MCP 白名单、上下文策略、热加载、匹配 | 不调用模型、不建连接 |
| MCP | `mcp_catalog.py` | Catalog 解析、按技能筛服务器、连接池、工具发现与代理调用 | 不处理对话业务 |
| 调度 | `dispatcher.py` | 校验消息、解析技能、裁剪工具、复用 `build_agent`、组装回复 | 不实现新的推理循环 |
| 通道 | `adapters.py` | 入口/出口消息转换、适配器生命周期、总线消费者 | 不写 Agent 思考逻辑 |
| 配置 | `config.py` | 平台根目录、开关、默认技能、定时任务 | 不读数据库 |

`route_key = 通道:渠道:会话`（如 `im:slack:c-123`、`cli:terminal:cli-ab12cd`），
出站队列按它隔离，多个会话并发时互不串线。

## 3. 文件变更清单

全部为新增文件，未修改存量代码：

| 文件 | 作用 | 对标 Octop 的设计 |
| --- | --- | --- |
| `engine/agent_platform/config.py` | 配置根、开关、YAML/JSON 读取、结构错误留痕 | 单机配置中心 |
| `engine/agent_platform/message.py` | `AgentMessage` / `AgentReply` / `MessageBus` | 统一消息总线 |
| `engine/agent_platform/skills.py` | `Skill` / `SkillRegistry`、匹配与热加载 | Skill 与 prompt 解耦、可动态切换 |
| `engine/agent_platform/mcp_catalog.py` | `McpCatalog` / `McpClientPool` / 白名单 | MCP Catalog 与客户端池 |
| `engine/agent_platform/dispatcher.py` | 标准化消息 → 技能 → 工具 → 存量 Agent → 回复 | Agent 调度 / 任务路由 |
| `engine/agent_platform/adapters.py` | `ChannelAdapter` 基类、CLI/IM/定时适配器、`ChannelGateway` | 通道抽象网关 |
| `engine/agent_platform/cli.py` | 状态、技能、MCP、通道、单次对话、IM、定时、初始化 | 管理/调试入口 |
| `engine/agent_platform/builtin/skills/*.yaml` | `general` / `weekly-report` / `mcp-tools` 示例 | 内置技能包 |
| `engine/agent_platform/builtin/mcp_catalog.yaml` | 空 Catalog 与注释示例 | 连接器注册表 |
| `scripts/agentctl.py` | 薄启动脚本（把 `engine` 加进 `sys.path`） | CLI 管理入口 |
| `tests/integration/engine_platform_selftest.py` | 四层能力 + 与存量引擎联跑 | 可回归的自测 |
| `docs/agent-platform.md` | 本文档 | — |

## 4. 开关与配置

平台根目录按以下顺序确定：

1. `TT_PLATFORM_HOME`
2. `TT_DATA_HOME/agent-platform`
3. `~/.tongtianwork/agent-platform`

目录约定：

```
agent-platform/
  platform.yaml        # enabled / defaultSkill / maxParallel / schedules
  skills/*.yaml|json   # 技能包，一文件一个或 {"skills": [...]}
  mcp_catalog.yaml     # {"servers": [...]}；也接受 mcp_catalog.json
  workspace/<会话>/    # 未指定 workDir 时的默认工作目录
```

`platform.yaml`：

```yaml
enabled: false          # 或环境变量 TT_PLATFORM=1
defaultSkill: general
maxParallel: 2
schedules:
  - id: daily-report
    text: 汇总今天的进展并生成周报
    everySeconds: 3600
    skill: weekly-report
    workDir: D:/work/reports
```

环境变量优先级高于文件；配置结构错误只会进入 `errors` 并在 `status` 里摊开，
不会让检查类命令整体不可用。

## 5. 技能包

一个文件可以是一个技能，也可以是 `skills:` 数组：

```yaml
id: weekly-report
name: 周报助手
description: 汇总本周工作产出
match:
  keywords: [周报, 周总结, weekly report]
  priority: 10
tools:
  allow: [view_text_file, write_text_file, insert_text_file]
  deny: [generate_pptx]
mcp:
  servers: [filesystem]        # "*" = Catalog 里全部启用项；空 = 不接 MCP
  allow: ["filesystem/*"]      # "server/tool"、"server/*"、"*/*"、只写服务器名=全放行
context:
  mode: writing                # general / slides / writing / design
  max_iters: 20
  lazy_mcp_tools: true
  context_window: 128000
prompt: |
  你是周报助手……
```

解析规则：

- 用户目录 `skills/` 里的同名 `id` 覆盖内置项；
- 非法条目逐条报错（缺 `id`、空 `prompt`、非法 `mode`、越界 `max_iters` 等），合法条目照常加载；
- 运行中改文件，下一次调度前按 `(路径, mtime, size)` 自动重载，无需重启；
- 匹配优先级：显式 `--skill` / 消息里的 `skill` > 名称 > 关键词（`priority + 命中数 × 10`）> 默认技能；
- `prompt` 作为追加段进入 system prompt，同时以运行时技能形式注册 `use_skill` 正文；
- 白名单在两层生效：内置工具先裁剪，MCP 工具在注册时用 `enable_funcs` 过滤。

## 6. MCP Catalog 与客户端池

```yaml
servers:
  - id: echo
    name: 自测回显
    transport: stdio
    command: python
    args: ["tests/integration/fixtures/mcp_echo_server.py"]
    env: { TT_MCP_SELFTEST: seen-by-child }
    timeoutSecs: 30
    enabled: true

  - id: remote
    name: 远程连接器
    transport: http            # 也接受 sse / streamable_http
    url: http://127.0.0.1:9000/mcp
    headers: { Authorization: "Bearer ${TOKEN}" }
```

- 客户端构造复用 `mcp_runtime.build_client`，超时钳制在 1~600 秒，传输名归一；
- 池按一次调度使用（`async with McpClientPool(...)`）：stdio 子进程必须在创建它的 asyncio 任务里关闭；
- `discover()` 缓存工具清单，`call()` 走 `get_callable_function` 代理调用；
- 没有 `close` 的 HTTP 客户端不会误调，收尾统一交给 `mcp_runtime.close_all`；
- 技能只拿到自己绑定的服务器，且只注册通过 `mcp.allow` 的工具；
- 连接失败、工具不存在、服务器停用/缺失都会进入 `notes`，不静默换工具。

## 7. CLI 管理命令

```bash
cd tongtian-tauri
python scripts/agentctl.py status [--json]
python scripts/agentctl.py skills list|show <id>|reload [--json]
python scripts/agentctl.py mcp list [--json]
python scripts/agentctl.py mcp tools <id> [--json]      # 连接并列出工具
python scripts/agentctl.py mcp call <id> <tool> --args '{"text":"hi"}' [--json]
python scripts/agentctl.py channels [--json]
python scripts/agentctl.py init [--home <dir>]
python scripts/agentctl.py chat "帮我写周报" [-s <skill>] [--model <name>] [--work-dir <dir>] [--conversation <id>] [--json]
python scripts/agentctl.py im <slack|dingtalk|wecom|feishu>
python scripts/agentctl.py schedule run
```

`chat` / `im` / `schedule` 需要平台层已启用；`status` / `skills` / `mcp` / `channels` / `init` 在关闭状态下也可用。
`--home` 等价于设置 `TT_PLATFORM_HOME`，适合隔离调试。

## 8. 新增一个通道

通道只需要新增适配器，调度层与总线不变：

```python
from agent_platform import ChannelAdapter, AgentMessage

class WebhookAdapter(ChannelAdapter):
    name = "webhook"

    def to_message(self, payload: dict) -> AgentMessage:
        return AgentMessage(
            text=payload["text"], channel="im", channel_id="webhook",
            conversation_id=payload.get("chat_id") or "default",
            sender=payload.get("user") or "",
        )

    async def start(self) -> None:
        ...  # 启收发；收到消息后 await self.dispatch(self.to_message(payload))

    async def stop(self) -> None:
        ...
```

把它 `gateway.register(adapter)` 即可；如果是对接现有 IM，优先复用
`im_channels.runtime_of(kind).set_handler(...)`（`ImAdapter` 已经这样做）。

## 9. 测试方式

### 9.1 离线自测（推荐，不联网、无需 API Key）

```bash
cd tongtian-tauri
python tests/integration/engine_platform_selftest.py
```

覆盖：

1. 消息校验、往返序列化、入站顺序、按会话隔离出站、关闭后拒绝；
2. 技能内置 + 用户目录加载、非法条目报错、匹配优先级、热加载、内置/MCP 白名单；
3. Catalog 条目校验、`*`/显式绑定、客户端池发现/调用/未知工具/连接失败/收尾；
4. 注入 runner 的调度与网关；IM/定时适配器转换；
5. CLI `status` / `skills` / `mcp` / `init` / 关闭态 `chat`；
6. 真实 `server.build_agent` + 离线假模型：技能提示词进入 system prompt、
   白名单裁剪内置工具、MCP 工具按白名单注册、工具调用结果回到模型上下文。

依赖缺失时的行为：`agentscope` / `mcp` 缺失会跳过 MCP 池与引擎联跑分组；
`PyYAML` 缺失会跳过内置 YAML 技能分组，其余 JSON 技能用例照常执行。

### 9.2 CLI 冒烟

```bash
python scripts/agentctl.py init --home .devtmp/platform-mcp-home
# 在 .devtmp/platform-mcp-home/mcp_catalog.yaml 里加入 stdio 服务器 echo（command=python，
# args=["tests/integration/fixtures/mcp_echo_server.py"]）后：
python scripts/agentctl.py --home .devtmp/platform-mcp-home mcp tools echo --json
python scripts/agentctl.py --home .devtmp/platform-mcp-home mcp call echo echo --args '{"text":"hi"}' --json
```

实测结果：工具发现返回 `echo` / `now_word` / `env_probe`，代理调用返回 `echo:hi`。

### 9.3 真实模型联调

```bash
TT_PLATFORM=1 python scripts/agentctl.py chat "你好"
```

需要本机已有可用的模型配置（与桌面端同一套 `models.json` / 环境变量）。
本次自动化自测使用的是假模型，真实模型链路需要在有凭据的环境里再跑一次。
IM 通道还需要先在桌面端完成对应企业的凭据配置，未配置时 `im` 命令会明确拒绝启动。

## 10. 已知边界

- 单用户本地运行：没有用户管理、JWT、数据库、Web 后台、权限系统（按约定不引入）。
- 平台层默认关闭；关闭时 `chat` / `im` / `schedule` 返回 `PLATFORM_DISABLED` 并给出开关办法。
- 定时任务只有 `everySeconds` 周期，不解析 cron 表达式，最小间隔 5 秒。
- stdio MCP 连接池按单次调度关闭，不跨调度常驻；需要常驻时应在同一 `async with` 内复用。
- 会话历史是调度器内存里的轻量窗口（最多 10 条），不落盘、不做长期记忆替代。
- 技能热加载按文件签名判断，同一毫秒内等长修改可能漏检；可运行 `skills reload` 确认扫描结果，运行中的调度器会在下次处理消息前自动重载。
- 与专家体系（`expert_runtime.py`）并存：专家面向多专家派发与模型绑定，本层技能面向单轮提示词/工具/上下文策略，两者尚未合并成同一份注册表。
