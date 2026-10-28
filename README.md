<div align="center">
  <img src="image/logo-square-1024.png" width="112" alt="任我行" />
  <h1>任我行 · Act-Freely</h1>
  <p>本地优先的 AI 办公工作台 —— 通用 / 设计 / 幻灯片三种默认模式（写作可选），对话、执行、产物一条链路</p>
  <p><a href="README.en.md">English</a> · 简体中文</p>
</div>

---

## 这是什么

任我行是一个跑在桌面上的 AI 办公工作台。它把「说需求 → 出计划 → 动手做 → 交产物」收在同一条链路里：界面是 Tauri + React 的本地应用，普通任务由客户端内嵌的 Rust harness 直接执行，重能力交给随包分发的 Python Agent 引擎，账号、技能 / 套件 / MCP 市场与项目协作由独立的 FastAPI 服务端承载。

它和纯聊天工具的区别主要在三点：

- **产物导向**：任务可以落到真实文件上 —— 单文件 HTML 原型、`.pptx` 演示文稿、Markdown 长文，而不是只回一段文字；`.xlsx` / `.docx` 生成尚未实现，不用改名文件冒充。
- **本地优先**：会话、知识库、记忆、产物默认都留在本机数据目录；模型密钥进系统钥匙串，不写入版本库。
- **可核查的执行面**：终端、浏览器、桌面操作、远程桌面都在显式授权下运行，高风险动作走审批，工具授权与 Hooks 可随时收回。

> 仓库内部分模块仍沿用项目早期名称「通天办公」，指同一个产品。

## 核心能力

- **三种默认工作模式**：通用、设计、幻灯片（写作模式可在设置中开启），各自有不同的画布与产物处理；设计模式内置 9 个推荐案例和任务模板（真实封面 + 可直接执行的提示词），切换到通用或幻灯片时不展示。
- **任务执行与回放**：Agent 以计划 / 步骤 / 交付事件流式输出，SSE 事件带单调 `seq`，支持断线重放；任务监控与画布分页查看，发送与停止是同一个按钮。
- **专家、技能与市场**：专家套件、技能包、MCP 连接器均可从在线市场检索、下载与收藏；安装前做静态风险扫描，发布者签名可校验。
- **知识、记忆与意识**：本地知识库支持排序与标题召回；`SOUL.md` / `WORK_MANUAL.md` / `USER_PROFILE.md` / `MEMORY.md` 等意识文件决定协作风格与长期记忆。
- **执行面**：终端（含 ACP）、浏览器操作、电脑操作、远程桌面、定时任务与 Hooks；工具与文件访问受工作区沙箱策略约束。Windows 上还可开启系统隔离，让终端、MCP stdio 与 ACP 子进程以 AppContainer 运行，越界写入与读取数据根密钥由系统拒绝；Hooks 与 macOS / Linux 目前仍在策略沙箱内。
- **模型中心**：多模型配置、按任务路由与故障转移、成本 / 延迟台账；零配置时可自动检测本机 Ollama。
- **账号与安全**：Loopback 网页验证登录、系统钥匙串存密钥、工具授权与审批、隐私守卫、静态风险扫描。
- **IM 通道**：Slack、钉钉、企业微信、飞书的长连接接入；目前只对本地假服务端验证，真实企业凭据与线上行为未验收。
- **中英双语界面**：设置内可切换中文 / English，日期与文案随语言切换。

## 架构

```text
┌──────────────────────────────────────────────────────────────┐
│ 渲染进程  React 18 + MUI 5 + Zustand + HashRouter             │
│ 工作台 / 设计画布 / 知识库 / 专家套件 / 扩展 / 协作 / 设置      │
└───────────────┬──────────────────────────────────────────────┘
                │ Tauri IPC（invoke / event）
┌───────────────▼──────────────────────────────────────────────┐
│ Tauri 2 核心（Rust）                                          │
│ 本地 harness（默认）：ReAct 循环 · 模型流式 · 文件工具 · 检查点 │
│ AgentBridge · 文件/知识/技能存储 · 账号与钥匙串 · 安全治理      │
│ 浏览器/电脑操作/远程桌面 · 计划与事件存储 · 品牌与系统集成      │
└───────────────┬──────────────────────────────────────────────┘
                │ HTTP + SSE（本地回环，能力越界时）
┌───────────────▼──────────────────────────────────────────────┐
│ Python Agent 引擎（FastAPI + AgentScope）                     │
│ 连接器 · 浏览器/电脑操控 · 知识库 · 专家 · 沙箱 · IM · 用量      │
└───────────────┬──────────────────────────────────────────────┘
                │ REST
┌───────────────▼──────────────────────────────────────────────┐
│ 综合服务端 tongtian-server（FastAPI + SQLite WAL）             │
│ 账号 · 技能/套件/MCP 市场 · 项目协作 · 产物快照                 │
└──────────────────────────────────────────────────────────────┘
```

- **渲染进程**：页面与组件在 `src/renderer`，跨进程契约与纯逻辑在 `src/shared`。
- **Rust 核心**：注册 170 余个 Tauri command，负责进程与文件边界、桥接引擎、安全策略与本地存储；内置 harness 默认承接普通任务的计划、审批、工具循环与检查点。
- **Python 引擎**：通过 HTTP + SSE 与 Rust 桥接，承接连接器、浏览器/电脑操控、知识库、专家、Hooks、逐工具审批、出站脱敏等重能力；发布时以 embeddable Python 随包分发。
- **综合服务端**：只承载账号、市场与协作，不接触桌面端的对话内容、文件内容或凭据值。

## 目录结构

```text
.
├── tongtian-tauri/            # 桌面端
│   ├── src/renderer/          # React 渲染进程（pages / components / i18n）
│   ├── src/shared/            # 前后端共享契约与纯逻辑
│   ├── src-tauri/             # Rust 核心（commands、桥接、存储、安全、原生集成）
│   ├── engine/                # Python Agent 引擎（FastAPI + AgentScope）
│   ├── scripts/               # prepare-python.sh、mock-agent.py、tt-cli.mjs
│   ├── tests/                 # Vitest 单元/集成测试 + 引擎自测脚本
│   ├── deploy/                # 服务化部署与 CLI 说明（含未验证项清单）
│   └── init.sh                # 桌面端初始化与启动脚本
├── tongtian-server/           # 账号与在线市场服务端（FastAPI + SQLite）
│   ├── app/                   # 路由、存储、鉴权与市场逻辑
│   ├── tests/                 # 服务端全链路与单元测试
│   └── init.sh                # 服务端初始化与启动脚本
├── docs/                      # PRD、缺陷与实现计划、历史归档
├── image/                     # 品牌素材
├── LICENSE / NOTICE
└── README.md / README.en.md
```

文档入口见 [docs/README.md](docs/README.md)：现行 [PRD](docs/PRD-任我行.md) 与 [缺陷与实现计划](docs/缺陷与实现计划-任我行.md)，历史资料归档在 `docs/archive/`，只保留在本机、不随仓库分发。

## 快速开始

### 环境要求

| 组件 | 要求 | 说明 |
| --- | --- | --- |
| Node.js | 20+ | 前端构建与 Tauri CLI |
| Rust | stable | 只有运行桌面壳需要；缺失时 `init.sh` 降级为浏览器模式 |
| Python | 3.10+ | 引擎与服务端 |
| 系统 | Windows 10/11 + WebView2 | 主要验证平台；macOS / Linux 未在本仓库验证 |
| Shell | Git Bash | 两个 `init.sh` 均为 Bash 脚本 |

### 1. 启动综合服务端（账号 / 市场 / 协作）

```bash
cd tongtian-server
cp .env.example .env    # 可选：按需修改端口、密钥、管理员账号
bash init.sh
```

启动后默认地址：

| 入口 | 地址 |
| --- | --- |
| 服务根路径 | `http://127.0.0.1:8000/` |
| Swagger UI | `http://127.0.0.1:8000/docs` |
| 健康检查 | `http://127.0.0.1:8000/health` |

开发阶段 `init.sh` 默认写入演示数据（`TT_SEED_DEMO=true`，幂等且不覆盖已有条目），演示账号密码只在首次创建时打印一次。配置项与接口说明见 [tongtian-server/README.md](tongtian-server/README.md)。

### 2. 启动桌面端

```bash
cd tongtian-tauri
bash init.sh
```

`init.sh` 会检查 Node / Rust / Python、安装缺失依赖，并在阻塞前打印访问信息：

- 检测到 Rust：拉起 `npm run tauri dev`，打开桌面窗口。
- 未检测到 Rust：降级为浏览器前端 `http://localhost:7173`（仅用于前端自测）。
- 默认 `MOCK_ENABLED=true`（前端命令走本地 Mock）、`TT_AUTH_MOCK=false`（登录走真实服务端）。要联调全部真实后端能力，设 `MOCK_ENABLED=false`；完全离线开发可设 `TT_AUTH_MOCK=true`。

也可以手动启动：

```bash
cd tongtian-tauri
npm install
npm run tauri dev      # 桌面壳
npm run dev            # 仅浏览器前端，端口 7173
```

### 3. 配置模型

引擎从 `tongtian-tauri/.env` 读取默认模型（也可在应用内「模型中心」配置，密钥进系统钥匙串）：

```dotenv
TT_PROVIDER=dashscope
TT_MODEL=qwen-plus
TT_API_KEY=你的密钥
TT_BASE_URL=
```

模型名称、可用额度与计费以各厂商控制台为准；本机已装 Ollama 时可不配置云端模型。

### 4. 打包安装包

```bash
cd tongtian-tauri
bash scripts/prepare-python.sh    # 首次发布前执行：准备随包 Python 运行时
npm run tauri build
```

产物在 `src-tauri/target/release/bundle/` 下（Windows 为 `msi/` 与 `nsis/`）。`prepare-python.sh` 只处理 Windows embeddable amd64；随包运行时缺失时安装包能启动，本地 harness 仍可处理普通任务，需要引擎的能力会明确报不可用。首次打包会下载 WiX / NSIS 工具链，离线环境需按 `WixTools314` / `NSIS` 目录名预置缓存。

## 模式与开关

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `MOCK_ENABLED` | `true` | 前端命令是否走本地 Mock；设 `false` 调用真实 Tauri command |
| `TT_AUTH_MOCK` | `false` | 设 `true` 时离线模拟登录，不连接服务端 |
| `TT_AUTH_SERVER_URL` | `http://127.0.0.1:8000` | 桌面端指向的账号服务地址 |
| `TT_SEED_DEMO` | `init.sh` 中 `true` | 服务端是否幂等写入演示数据 |
| `TT_ENGINE_TOKEN` | 空 | 服务化部署时的引擎访问令牌；桌面端本地回环不需要 |

## 测试

```bash
# 前端：类型检查 + Vitest（单元 + TS 集成）
cd tongtian-tauri
npm run typecheck
npm run test

# Rust 单元测试
cd src-tauri
cargo test --lib

# 服务端（标准库测试，无需 pytest）
cd ../../tongtian-server
.venv/Scripts/python.exe tests/test_server.py   # Windows
.venv/bin/python tests/test_server.py            # macOS / Linux
.venv/Scripts/python.exe tests/test_units.py
```

`tongtian-tauri/tests/integration/engine_*_selftest.py` 是引擎能力自测脚本，按需逐个运行（部分需要外部凭据或本机浏览器，未满足时会如实跳过或失败）。

## 数据与配置

| 位置 | 内容 |
| --- | --- |
| `TONGTIAN_HOME`（默认 `%APPDATA%\tongtian-office`，非 Windows 为 `~/.tongtian`） | 工作区、产物、模型配置、任务事件 |
| `TONGTIANWORK_HOME`（默认 `~/.tongtianwork`） | 意识文件、知识库、浏览器专用数据 |
| `tongtian-tauri/.env` | 引擎默认模型（`TT_PROVIDER` / `TT_MODEL` / `TT_API_KEY` / `TT_BASE_URL`） |
| `tongtian-server/.env` | 服务端监听地址、JWT 密钥、演示数据等 |
| `tongtian-server/storage/` | SQLite 数据库与市场包文件 |

## 当前边界

本项目仍在快速迭代，以下事项不要当成已交付能力：

- **服务化部署**：引擎直跑 + 静态前端 + 令牌链路已在本机验证；`deploy/Dockerfile`、真实反向代理、TLS 与公网 SSE 长连接未验证。详见 [tongtian-tauri/deploy/README.md](tongtian-tauri/deploy/README.md)。
- **IM 通道**：Slack / 钉钉 / 企业微信 / 飞书只对本地假服务端验证过，真实企业应用凭据与线上行为未验收。
- **办公产物**：HTML、PPTX、Markdown 为真实生成；`.xlsx` / `.docx` 尚未实现，不能用改名文件冒充。
- **跨平台**：桌面端主要用于 Windows 开发与验证；macOS / Linux 的构建、随包 Python 与安装包未验证，系统级隔离也只有 Windows 版。
- **系统隔离**：只覆盖终端、MCP stdio 与 ACP 子进程，Hooks 仍在应用权限下执行；关闭出网时容器内进程连回环与局域网也不可达，需要管理员配置 loopback exemption 才能访问本机服务。
- **完整缺口**：以 [PRD](docs/PRD-任我行.md) 的状态列与 [缺陷与实现计划](docs/缺陷与实现计划-任我行.md) 为准；README 不重复维护明细。

## 安全提示

- 不要提交 `.env`、模型密钥、企业应用凭据或任何含真实 Key 的验证脚本。
- `tongtian-tauri/tests/test.py` 是本地临时验证脚本，含明文 API Key，已在 `.gitignore` 中排除且从未入库；其中的 Key 需要自行吊销 / 轮换。
- 对外推送前先做一次密钥扫描；发现泄露立即轮换，而不是只删文件。

## 参与贡献

欢迎通过 [GitHub Issues](https://github.com/zzw521/Act-Freely/issues) 反馈问题或提交 Pull Request。提交前请确保 `npm run typecheck`、`npm run test` 与相关 Rust / 服务端测试通过，并避免把本机数据、会话记录或凭据带进提交。

## 许可证

本项目以 [Apache License 2.0](LICENSE) 发布。部分源码派生自 QwenPaw 项目（Apache-2.0），归属与派生范围见 [NOTICE](NOTICE)。

© 2026 北京通天云计算有限公司
