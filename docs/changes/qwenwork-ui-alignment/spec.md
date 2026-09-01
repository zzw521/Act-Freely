# 通天办公 · 千问办公(QwenWork)对标整改 · UI 及功能设计方案 (SPEC)

> **文档版本**：v1.0  
> **更新日期**：2026-08-04  
> **负责人**：架构组  
> **关联 SPEC**：`docs/spec/spec-通天办公.md` v1.1（本整改方案沿用其模板风格与技术栈约定）  
> **对标对象**：千问办公(QwenWork)桌面端（qwenwork.cn/docs/desktop + help.aliyun.com/zh/qwenwork）

---

## 目录

- [1. 背景与对标调研](#1-背景与对标调研)
- [2. 差距分析矩阵](#2-差距分析矩阵)
- [3. 信息架构整改](#3-信息架构整改)
- [4. 页面级 UI 设计](#4-页面级-ui-设计)
- [5. 新功能模块设计](#5-新功能模块设计)
- [6. 增量数据模型与 IPC 契约](#6-增量数据模型与-ipc-契约)
- [7. 实施分期](#7-实施分期)

---

## 1. 背景与对标调研

### 1.1 对标产品概述

千问办公(QwenWork)桌面端是面向日常办公场景的 AI 智能工作台：用户以自然语言发起任务，结合**文件、技能、连接器、电脑操控、IM、Hooks** 等能力完成信息整理、内容创作、数据分析。其桌面端在「本地化能力 + 扩展生态 + 精细化设置」三个维度上形成了完整闭环，是本整改方案的对标基准。

### 1.2 QwenWork 桌面端功能全景

| 能力域 | 功能清单 |
|--------|----------|
| **任务与对话** | 自然语言任务、任务列表、任务归档、QuickPick 全局快速任务窗、弹出独立窗口 |
| **文件处理** | 本地文件读写/转换/整理，Office 产物（Word/Excel/PPT/PDF）生成与续改 |
| **工作台模式** | 通用（对话）/ 设计（无限画布）/ 幻灯片（AI Slides）/ 写作（Markdown 长文），幻灯片模板管理 |
| **模型选择** | 内置多模型档位，输入框旁按任务切换（效率/质量/成本平衡） |
| **扩展生态** | 专家套件（岗位工作流包）、技能（SKILL.md，与 Claude Code Skills 兼容）、连接器（MCP 实现） |
| **连接器** | 内置：浏览器（Chromium 扩展）、系统应用（日历/邮件/备忘录/通讯录）、Microsoft 365；集成市场：钉钉、飞书、Notion、Slack、Figma 等。全部默认关闭，授权后生效 |
| **电脑操控** | 截屏感知界面→操作描述→执行→结果确认；执行策略三档：每次询问（默认）/自动执行/禁用 |
| **应用快照** | 全局快捷键捕获最前方应用截图与可读文本，作为对话上下文 |
| **语音输入** | 全局快捷键录音+转录，可选仅应用内或全局生效 |
| **IM 频道** | 接入常用 IM 聊天工具，IM 内 @ 即可发起任务；25 项企业 IM 能力（群聊读取/考勤审批/日程/文档表格/知识库/邮件） |
| **定时任务** | 预设时间自动执行，支持结果推送（如钉钉日程推送、每日调研简报、工作日报） |
| **Hooks** | 12 类生命周期事件的确定性拦截/注入（见 §1.4） |
| **意识** | 协作风格定制、工作手册、自动记忆与反思，跨会话记忆 |
| **网盘** | 个人网盘存储 Agent 产物与网页文件 |
| **设置体系** | 三分组架构：通用 / 扩展与集成 / 高级设置（见 §1.3） |

### 1.3 QwenWork 系统设置三分组结构（关键对标项）

```
设置
├── 通用
│   ├── 偏好设置        语言/主题亮暗/界面风格/对话字体/字号/对话宽度/预览方式/
│   │                   提示词建议/默认展开工具调用/IM 展示工具调用过程
│   ├── 个人资料        头像(背景色+图标)/账户 UID/订阅套餐/退出登录
│   ├── 系统设置        开机自启动/保持系统唤醒/桌面通知/Dock 未读状态/声音通知/
│   │                   网络代理 + 系统权限(磁盘/录屏/辅助功能/麦克风/自动化/通知/位置)
│   │                   + 数据共享(隐私模式)
│   ├── 语音输入        启用开关/快捷键模式(单按键/组合键)/触发按键
│   ├── 应用快照        全局快捷键配置
│   ├── 快捷键          快捷键表(通用/任务/会话三组) + QuickPick 全局快速窗配置
│   ├── 意识            意识模式/自动记忆/协作风格/工作手册（跳转意识页）
│   ├── 更新应用        检查更新
│   └── 已归档          归档任务列表（恢复/删除/全部删除）
├── 扩展与集成
│   └── 工作台          工作模式开关(通用/设计/幻灯片/写作) + 新任务默认模式
│                       + 幻灯片模板管理(内置/自定义/导入)
└── 高级设置
    ├── 安全工作环境    启用隔离环境开关 + 清理环境文件(保留对话与产物)
    └── 实验特性        生成式 UI/弹出独立窗口/浏览器上下文接力/助手过程折叠/
                        修复历史消息/对话列表筛选
```

### 1.4 QwenWork Hooks 事件模型（关键对标项）

Hooks 是**确定性**拦截机制（不同于 Prompt 指令）：事件触发则脚本必执行。

- **配置位置**：`~/.qwenwork/settings.json` 的 `hooks` 字段（JSON，重启生效）
- **配置结构**：`事件名 → [{ matcher, hooks: [{ type: "command", command, timeout }] }]`
- **matcher 语义**：不填/`*`=全部；精确值；`|` 分隔多值；正则
- **脚本契约**：stdin 接收 JSON（通用字段 `session_id`/`cwd`/`hook_event_name`），exit code `0`=成功、`2`=阻塞（stderr 注入对话）、其他=非阻塞错误；exit 0 时可输出 stdout JSON 精细控制

| 事件 | 触发时机 | 可阻塞 |
|------|----------|:------:|
| SessionStart | 会话开始（startup/resume/compact） | 否 |
| SessionEnd | 会话结束 | 否 |
| UserPromptSubmit | 用户提交 Prompt 后、Agent 处理前 | 否 |
| PreToolUse | 工具执行前 | **是** |
| PostToolUse | 工具执行成功后 | 否 |
| PostToolUseFailure | 工具执行失败后 | 否 |
| Stop | Agent 完成响应 | **是**（可让其继续） |
| SubagentStart / SubagentStop | 子 Agent 启动/完成 | Stop 可 |
| PreCompact | 上下文压缩前 | 否 |
| Notification | 权限请求/结果通知 | 否 |
| PermissionRequest | 工具需用户授权 | 否 |

### 1.5 QwenWork 连接器授权模型

- 所有连接器**默认关闭**，仅在用户明确完成授权与配置后才获取访问权限
- 安装流程：扩展→连接器→推荐列表→「+」→授权弹窗（如浏览器需安装扩展，状态「等待扩展连接」→「扩展已连接」）
- 连接器基于 MCP 协议实现，连接成功后 AI 托管对应资源（如浏览器 Tab），任务期间避免人工操作冲突

---

## 2. 差距分析矩阵

### 2.1 现状盘点（tongtian-office/src/renderer）

| 现有页面/组件 | 职责 |
|---------------|------|
| `pages/Workbench` | 对话工作台（空态引导+技能标签+推荐案例+对话流） |
| `pages/ModelHub` | 模型中心 |
| `pages/Settings` | 设置页（10 平铺分区：通用/偏好/技能/IM/远程控制/模型/用量/授权/关于/实验室） |
| `pages/FilePanel` | 文件面板 |
| `pages/ScheduleView` | 定时任务 |
| `pages/Security` | 安全页（当前被侧边栏复用为「IM 频道」入口） |
| `pages/SkillMarket` | 技能市场 |
| `pages/AuditView` | 审计日志 |
| `pages/WorkspacePanel` | 工作区/专家套件 |
| `components/*` | ChatInput/ChatStream/PlanView/FilePanel/ModelForm/ModelSettings/ModelStatsPanel/AuditLog/DataClassification/DomainControl/SandboxPolicy/Layout |

侧边栏（`components/Layout/Sidebar.tsx`）已部分对齐 QwenWork 导航：新任务 / 扩展(专家套件/技能/连接器) / 定时任务 / IM 频道 / 任务·频道。

### 2.2 差距矩阵（带优先级）

| # | 领域 | QwenWork 能力 | 通天办公现状 | 差距 | 优先级 |
|---|------|---------------|--------------|------|:------:|
| G01 | 设置信息架构 | 三分组（通用/扩展与集成/高级设置） | 10 平铺分区 | 重组导航结构 | P0 |
| G02 | 偏好设置 | 10 项（含界面风格/对话字体字号宽度/预览方式/提示词建议/工具展开） | 部分 | 补齐设置项 | P0 |
| G03 | 个人资料 | 头像/UID/订阅/退出 | 仅侧边栏卡片 | 新增分区 | P0 |
| G04 | 系统设置 | 自启/唤醒/通知/代理/7 项系统权限/数据共享 | 无（「系统授权」雏形） | 新增分区 | P0 |
| G05 | 连接器中心 | 内置连接器+集成市场，授权流 | MCP 后端设计已有，UI 仅链接到模型分区 | 新建连接器中心页 | P0 |
| G06 | 意识（记忆） | 协作风格/工作手册/自动记忆/反思 | agent 侧 memory 模块已有、无 UI | 新增意识页+设置分区 | P0 |
| G07 | 任务归档 | 归档列表/恢复/删除 | 无 | 新增归档页 | P0 |
| G08 | Hooks | 12 事件+JSON+脚本拦截 | 无 | HookRunner 引擎+管理 UI | P1 |
| G09 | 电脑操控 | 截屏感知+三档策略 | Sandbox L0-L2 设计已有 | 操控面板+策略设置 | P1 |
| G10 | 工作台模式 | 通用/设计/幻灯片/写作+模板 | 无 | 模式切换+设置分区 | P1 |
| G11 | IM 频道 | IM 接入+25 项企业 IM | 导航占位指向 Security | 独立 IM 频道页 | P1 |
| G12 | 语音输入 | 全局快捷键+转录 | 无 | 主进程服务+设置分区 | P2 |
| G13 | 应用快照 | 前台截图+可读文本 | 无 | 主进程服务+设置分区 | P2 |
| G14 | QuickPick | 全局快速任务窗 | 无 | 独立小窗+全局快捷键 | P2 |
| G15 | 安全工作环境 | 隔离环境+清理 | Sandbox 设计已有 | 高级设置入口 | P2 |
| G16 | 实验特性 | 生成式 UI 等 6 项 | 「实验室特性」分区存在 | 对齐特性清单 | P2 |
| G17 | 快捷键管理 | 快捷键表+录制 | 无 | 快捷键设置分区 | P1 |
| G18 | 模型档位切换 | 输入框旁档位切换 | ModelSettings 已有 | 对话输入区增档位选择器 | P1 |

> 优先级约定：P0 = 信息架构与核心入口整改（首个迭代）；P1 = 核心扩展能力（第二迭代）；P2 = 增强型本地能力（第三迭代）。

---

## 3. 信息架构整改

### 3.1 新导航树

```
侧边栏 (Sidebar)
├── 新任务 (/)                        Workbench（含工作台模式切换）
├── 扩展（可折叠分组）
│   ├── 专家套件 (/workspace)         WorkspacePanel（保留）
│   ├── 技能 (/skill-market)          SkillMarket（保留）
│   └── 连接器 (/connectors)          【新增】连接器中心
├── 定时任务 (/schedule)              ScheduleView（保留增强）
├── IM 频道 (/im)                     【新增】独立 IM 频道页（不再复用 /security）
├── 任务·频道区
│   ├── 任务 (/files)                 FilePanel（保留）
│   └── 频道 (/workspace)             WorkspacePanel（保留）
├── （会话列表）
└── 底部用户卡片 → 个人中心菜单
    ├── 企业/团队
    ├── 偏好设置 (/settings?s=preferences)
    ├── 设置 (/settings)              三分组设置页
    ├── 新人指南 / 关于我们
    └── 退出登录

设置页 (/settings?s=<section>) 三分组导航
├── ── 通用 ──
│   ├── 偏好设置 (preferences)
│   ├── 个人资料 (profile)
│   ├── 系统设置 (system)
│   ├── 语音输入 (voice)
│   ├── 应用快照 (snapshot)
│   ├── 快捷键 (shortcuts)
│   ├── 意识 (awareness)
│   ├── 更新应用 (update)
│   └── 已归档 (archive)              跳转 /archive
└── ── 扩展与集成 ──
    └── 工作台 (workbench)
└── ── 高级设置 ──
    ├── 安全工作环境 (sandbox)
    └── 实验特性 (labs)
```

保留但不进侧边栏的页面：ModelHub（经设置「模型」入口或顶栏进入）、AuditView、Security（归入高级设置-安全工作环境与安全中心）。原 Settings 中的「模型」与「用量统计」并入「扩展与集成 → 模型与用量」子分区，保持 ModelSettings/ModelStatsPanel 组件复用。

### 3.2 路由映射表

| 路由 | 页面 | 状态 | 说明 |
|------|------|:----:|------|
| `/` | Workbench | 改造 | 增加工作台模式切换器、模型档位选择器 |
| `/connectors` | ConnectorCenter | **新增** | 连接器中心（内置+市场+授权） |
| `/hooks` | HooksManager | **新增** | Hooks 配置管理（高级开发者入口，设置页亦可达） |
| `/im` | IMChannel | **新增** | 独立 IM 频道页 |
| `/archive` | ArchiveView | **新增** | 归档任务管理 |
| `/awareness` | AwarenessPage | **新增** | 意识页（协作风格/工作手册/记忆浏览） |
| `/computer-use` | ComputerUsePanel | **新增** | 电脑操控面板与执行策略 |
| `/settings` | Settings | 重组 | 三分组导航（见 §3.1） |
| `/schedule` | ScheduleView | 保留增强 | 增加结果推送渠道配置 |
| `/workspace` `/skill-market` `/files` `/model-hub` `/audit` `/security` | 现有页 | 保留 | 结构不变，仅入口调整 |

---

## 4. 页面级 UI 设计

### 4.1 设置页重组（Settings）

**整体布局**：延续现有「顶部标题+关闭按钮 / 左侧导航 / 右侧内容」三栏结构；左侧导航改为**带分组标题的三段式**（通用/扩展与集成/高级设置），分组标题使用 12px 灰色小字，分区项沿用现有 13px 行样式与 `?s=xxx` 深度链接机制。

**分区一：偏好设置（preferences）**——设置项清单：

| 设置项 | 控件 | 选项/默认 |
|--------|------|-----------|
| 语言 | 下拉 | 中文 / English |
| 主题亮暗 | 分段控件 | 亮色 / 暗色 / 跟随系统 |
| 界面风格 | 下拉 | 默认 / 玻璃 / 经典 / 羊皮纸 |
| 对话字体 | 分段控件 | 无衬线 / 衬线 |
| 对话字号 | 滑杆 | 12-20px，默认 14 |
| 对话宽度 | 下拉 | 标准 / 宽 / 全宽 |
| 预览方式 | 下拉 | 应用内预览 / 系统默认程序 |
| 提示词建议 | Switch | 默认开（新会话生效） |
| 默认展开工具调用 | Switch | 默认关 |
| IM 展示工具执行过程 | Switch | 默认关 |

**分区二：个人资料（profile）**——头像编辑（11 色背景+首字符/Emoji 图标）、账户 UID（只读+复制）、订阅套餐卡片（当前版本+「升级」按钮）、退出登录。

**分区三：系统设置（system）**——

| 设置项 | 控件 |
|--------|------|
| 开机自启动 | Switch（`app.setLoginItemSettings`） |
| 保持系统唤醒 | Switch（`powerSaveBlocker`，Agent 运行时生效） |
| 桌面通知 | Switch（Electron Notification） |
| 任务栏未读状态 | Switch（`app.setBadgeCount` / 闪烁） |
| 声音通知 | Switch |
| 网络代理 | 下拉（跟随系统/自定义）+ 自定义地址输入 |
| 系统权限清单 | 权限行列表：磁盘访问/录屏/辅助功能/麦克风/自动化/通知/位置，每行显示状态徽标（已授权/未授权）+「去授权」按钮（Windows 上无系统级权限的项显示「无需授权」） |
| 数据共享 | 隐私模式 Switch + 说明文案 |

**分区四：语音输入（voice）**——启用 Switch、快捷键模式（单按键/组合键）单选、触发按键录制框（详见 §5.3）。

**分区五：应用快照（snapshot）**——全局快捷键录制框、启用 Switch、隐私提示（快照内容将作为上下文上送模型，受数据分级策略约束）。

**分区六：快捷键（shortcuts）**——三组快捷键表（通用：打开设置/切换侧边栏/切换任务监控；任务：快速切换/新建/全局搜索/当前搜索；会话：发送/换行），每行支持点击录制新组合；QuickPick 子区块（启用+触发模式：组合键/双击修饰键）。

**分区七：意识（awareness）**——意识模式 Switch、自动记忆 Switch、入口卡片跳转 `/awareness`（协作风格与工作手册编辑）。

**分区八：更新应用（update）**——当前版本号、检查更新按钮、更新日志摘要（electron-updater）。

**分区九：扩展与集成-工作台（workbench）**——四种工作模式开关（通用/设计/幻灯片/写作）、新任务默认模式下拉、幻灯片模板列表（内置+导入自定义）。

**分区十：扩展与集成-模型与用量（models）**——复用现有 ModelSettings + ModelStatsPanel 组件，保持 `?s=models` 深度链接兼容。

**分区十一：高级设置-安全工作环境（sandbox）**——启用 Switch（映射现有 Sandbox L2 隔离策略）、占用空间显示、「清理环境文件」危险操作按钮（二次确认，保留对话与产物）。

**分区十二：高级设置-实验特性（labs）**——对齐清单：生成式 UI / 弹出独立窗口 / 浏览器上下文接力 / 助手过程折叠（折叠力度三档）/ 对话列表筛选 / 修复历史消息显示。

### 4.2 连接器中心（ConnectorCenter，/connectors）

**布局**：页头「连接器」标题+说明（"连接器是通天办公与外部系统之间的桥梁，默认全部关闭"）；下方两个 Tab：**内置连接器** / **集成市场**。

- **内置连接器卡片**：浏览器（Chromium 扩展托管）、系统应用（日历/邮件/备忘录/通讯录，逐项开关）、Microsoft 365（Outlook/日历/待办/联系人/OneDrive）。卡片含：图标+名称+一句话说明+状态徽标（未连接/已连接/异常）+「连接/断开」按钮。
- **连接授权流**（对齐 QwenWork）：点击「+」→ 弹窗说明所需权限 → 用户确认授权 → 状态「等待连接」→ 成功后「已连接」并展示连接实例。浏览器连接器提供「安装扩展」引导与「手动安装」兜底入口。
- **集成市场列表**：钉钉、飞书、Notion、Todoist、Slack、Figma、Google 日历等卡片网格，一键安装+连接；市场数据源走 Mock 开关（开发阶段本地 JSON，联调阶段远端市场 API，遵循开发规范）。
- **已安装管理**：每个已连接连接器支持查看暴露工具列表（`mcp:list-tools`）、工具白名单/黑名单编辑（复用现有 MCPServer 实体的 whitelist/blacklist 字段）、断开与卸载。

### 4.3 Hooks 管理页（HooksManager，/hooks）

**布局**：左侧事件分组列表（12 事件，见 §1.4），右侧选中事件的 Hook 列表+编辑器。

- **Hook 条目**：matcher 描述（如 `Bash`）、command 摘要、启用 Switch、超时时间。
- **编辑器**：JSON 编辑模式（monaco 风格文本域）+ 表单模式（matcher/command/timeout 三字段）双模切换；保存时做 JSON Schema 校验。
- **脚本测试**：提供「试运行」按钮，构造样例 stdin JSON 执行一次脚本，回显 exit code / stdout / stderr。
- **生效策略**：配置保存后提示「重启应用生效」（对齐 QwenWork 不支持热加载的语义；通天办公实现上支持热加载，但 UI 明确标注）。
- **内置模板**：拦截危险命令（PreToolUse+Bash）、写文件后自动格式化（PostToolUse+Write|Edit）、任务完成桌面通知（Notification）。

### 4.4 意识页（AwarenessPage，/awareness）

**布局**：三区块纵向排列——

1. **协作风格**：人格化风格选择（专业严谨/轻松友好/简洁高效/自定义）+ 自定义提示词编辑区，写入 Agent system prompt 注入层。
2. **工作手册**：Markdown 编辑器，沉淀用户的工作规范/偏好/常用流程，作为跨会话长期上下文（映射现有 agent `memory/` 模块的长期记忆存储）。
3. **自动记忆**：自动记忆 Switch + 记忆条目列表（时间/内容摘要/来源会话），支持单条删除与手动「添加记忆」；反思记录折叠区（Agent 周期性反思产出的经验条目）。

### 4.5 电脑操控面板（ComputerUsePanel，/computer-use）

**布局**：

- **执行策略**：三档单选——每次询问（默认）/自动执行/禁用；说明各档风险。
- **权限引导**：系统权限状态卡（截屏权限/辅助功能权限），未授权时引导授权。
- **操控透明度展示规范**（对话流内）：截屏缩略图 → 操作描述文本 → 实际执行 → 结果截屏确认，四段式时间线渲染于 ChatStream 工具块内。
- **风险警示区**：不可撤销操作提示、敏感窗口建议关闭、验证码需人工介入的说明。

### 4.6 Workbench 改造

- **工作台模式切换器**：输入框上方新增模式胶囊组（通用/设计/幻灯片/写作，依设置页开关显示），切换后改变输入区占位文案与右侧画布形态（设计=无限画布占位、幻灯片=HTML 画布、写作=Markdown 编辑器）。
- **模型档位选择器**：输入框右侧新增档位下拉（自动/快速/旗舰，映射现有 ModelConfig 列表），对齐 QwenWork「输入框旁切换档位」。
- **应用快照入口**：输入框附件区新增「快照」按钮（快捷键触发），快照以图片+提取文本附件形式进入上下文。
- **语音输入入口**：输入框麦克风按钮（P2）。
- **提示词建议**：AI 回复完成后在输入框上方生成 2-3 条下一步建议胶囊（偏好开关控制）。

### 4.7 IM 频道页（IMChannel，/im）

**布局**：左侧渠道列表（钉钉/飞书/微信等，依连接器开通状态），右侧选中渠道的配置面板：绑定状态、消息接收范围（群/单聊白名单）、回复策略（仅最终回复/含工具过程——联动偏好设置项）、25 项企业 IM 能力的分组开关（消息读取/日程/审批/考勤/文档/知识库/邮件）。Mock 开关：开发阶段使用本地模拟消息流，联调阶段对接真实 IM Webhook/Bot SDK。

### 4.8 归档页（ArchiveView，/archive）

归档任务列表（标题/归档时间/所属工作区），行操作：恢复 / 彻底删除（二次确认）；右上角「全部删除」（强确认对话框输入确认词）。

### 4.9 ScheduleView 增强

在现有定时任务基础上补充：**结果推送渠道**配置（推送到 IM 频道/桌面通知/仅产物落盘）、任务模板（每日日程推送/每日调研简报/工作日报三个预置模板，对齐 QwenWork）。

---

## 5. 新功能模块设计

### 5.1 HookRunner 引擎（`src/main/hooks/`）

#### 5.1.1 架构

```
┌──────────────────────────────────────────────────┐
│                 HookManager (单例)                 │
│  loadConfig / validate / hotReload / list          │
├──────────────────────────────────────────────────┤
│  ┌────────────┐   ┌────────────┐   ┌───────────┐ │
│  │ HookRegistry│   │ HookPlanner │   │ HookRunner│ │
│  │ 配置解析索引 │ → │ 事件→命中项  │ → │ 并发执行   │ │
│  └────────────┘   └────────────┘   └─────┬─────┘ │
│                                    ┌─────▼──────┐│
│                                    │Aggregator   ││
│                                    │exit码/stdout ││
│                                    │合并→决策     ││
│                                    └─────────────┘│
└──────────────────────────────────────────────────┘
         ↑ 事件发射点（插桩位置）
 AgentEngine(pre-tool/post-tool/stop) · SessionManager(start/end)
 ChatInput(prompt-submit) · ContextCompactor(pre-compact)
 NotificationService · PermissionGate
```

#### 5.1.2 核心接口

```typescript
// src/main/hooks/types.ts
type HookEvent =
  | 'SessionStart' | 'SessionEnd' | 'UserPromptSubmit'
  | 'PreToolUse' | 'PostToolUse' | 'PostToolUseFailure'
  | 'Stop' | 'SubagentStart' | 'SubagentStop'
  | 'PreCompact' | 'Notification' | 'PermissionRequest';

interface HookCommand {
  type: 'command';
  command: string;      // shell 命令，支持 ~ 展开
  timeout?: number;     // 秒，默认 60
}

interface HookMatcherGroup {
  matcher?: string;     // 不填/"*"=全部；"A|B"=多值；支持正则
  hooks: HookCommand[];
}

interface HookInput {
  session_id: string;
  cwd: string;
  hook_event_name: HookEvent;
  [key: string]: unknown; // 事件附加字段（tool_name/tool_input/...）
}

interface HookResult {
  exitCode: number;     // 0=成功 2=阻塞 其他=非阻塞错误
  stdout: string;
  stderr: string;
  blocked: boolean;     // exitCode===2 且事件支持阻塞
  timedOut: boolean;
}

// src/main/hooks/runner.ts
class HookRunner {
  /** 触发事件，返回聚合结果；PreToolUse/Stop 的阻塞结果由调用方裁决 */
  async emit(event: HookEvent, input: HookInput): Promise<HookAggregate>;
}

interface HookAggregate {
  blocked: boolean;
  blockReason?: string;      // 阻塞 hook 的 stderr（注入对话/返回 Agent）
  results: HookResult[];
}
```

#### 5.1.3 关键语义（与 QwenWork 对齐）

- 确定性：事件命中即执行，不受模型影响；同一事件的多个 hook 顺序执行
- 阻塞仅对 `PreToolUse`（阻止工具执行）与 `Stop`/`SubagentStop`（注入消息让 Agent 继续）生效；其他事件 exit 2 视为普通错误
- 脚本通过 stdin 接收 JSON、不注入环境变量；exit 0 时解析 stdout JSON 做精细控制
- Windows 兼容：command 默认经 `cmd /c` 执行，脚本推荐 `.ps1/.bat`；超时强制 kill 进程树
- 安全约束：hook 命令执行受 SandboxPolicy 约束；配置变更写审计日志（`config.change`）

### 5.2 意识模块（`src/main/awareness/`）

复用现有 agent `memory/` 模块的存储能力，新增编排层：

```typescript
interface AwarenessProfile {
  mode: boolean;              // 意识模式总开关
  autoMemory: boolean;        // 自动记忆
  style: 'professional' | 'friendly' | 'concise' | 'custom';
  customPrompt?: string;      // 自定义协作风格
  workManual: string;         // 工作手册（Markdown）
  updatedAt: Date;
}

interface MemoryEntry {
  id: string;
  content: string;            // 记忆内容摘要
  source: 'auto' | 'manual' | 'reflection';
  sessionId?: string;
  createdAt: Date;
}

class AwarenessService {
  getProfile(): Promise<AwarenessProfile>;
  updateProfile(patch: Partial<AwarenessProfile>): Promise<AwarenessProfile>;
  listMemories(filter?: { source?: string }): Promise<MemoryEntry[]>;
  addMemory(content: string): Promise<MemoryEntry>;
  deleteMemory(id: string): Promise<void>;
}
```

注入机制：`AwarenessProfile` 在工作手册与风格提示词拼装后，作为 system prompt 前置层注入每次会话；记忆条目经相关性筛选后按需注入（沿用现有 memory 检索逻辑）。

### 5.3 全局快捷键与 QuickPick（`src/main/global-shortcut/`）

```typescript
interface ShortcutConfig {
  voiceInput?: { mode: 'single' | 'combo'; keys: string }; // 语音输入
  snapshot?: { keys: string };                              // 应用快照
  quickPick?: { enabled: boolean; mode: 'combo' | 'double-modifier'; keys?: string };
  custom: Record<string, string>;   // 用户录制的快捷键（action → combo）
}

class ShortcutService {
  register(config: ShortcutConfig): void;
  unregisterAll(): void;
}

class QuickPickWindow {
  // 独立小窗（无边框 600x80，alwaysOnTop），输入任务目标后
  // 走 chat:submit 提交到当前活跃工作区，提交后窗口隐藏
  show(): void;
  hide(): void;
}
```

### 5.4 语音输入与应用快照（`src/main/capture/`）

```typescript
class VoiceInputService {
  // 全局快捷键触发录音 → 本地缓冲 → 调用转录模型 API → 文本回填到输入框
  startRecording(): Promise<{ tempFile: string }>;
  stopAndTranscribe(): Promise<{ text: string; durationMs: number }>;
}

class AppSnapshotService {
  // 捕获最前方窗口：desktopCapturer 截图 + OCR 提取可读文本
  // Windows: 截图经本地 OCR 服务；产物受数据分级策略约束（CONFIDENTIAL 不上送）
  captureFrontApp(): Promise<{ imagePath: string; text: string; appName: string }>;
}
```

### 5.5 安全工作环境映射

QwenWork「安全工作环境」映射到通天办公现有 Sandbox 三层架构的 **L2 系统沙箱**：

- 启用开关 → 全局 `SandboxPolicy.executionLevel` 默认提升至 L2（任务在隔离目录+受限网络策略下运行）
- 「清理环境文件」→ 删除隔离工作目录缓存，保留 sqlite 对话记录与 outputs 产物目录
- 磁盘占用统计：`QuotaController.report()` 汇总隔离目录体积

### 5.6 电脑操控服务（`src/main/computer-use/`）

```typescript
type ComputerUsePolicy = 'ask_every_time' | 'auto' | 'disabled';

class ComputerUseService {
  getPolicy(): ComputerUsePolicy;
  setPolicy(p: ComputerUsePolicy): void;
  // 执行循环：截屏→视觉模型理解→操作描述→（按策略征求确认）→执行→结果截屏
  async executeStep(step: ComputerUseStep): Promise<ComputerUseResult>;
}
```

底层依赖 nut.js（对齐主 SPEC §3.4 桌面控制技术选型）；每次操控前按 `ask_every_time` 策略弹出确认卡（含目标应用与操作描述），全程产出四段式透明度事件推送至 ChatStream。

---

## 6. 增量数据模型与 IPC 契约

### 6.1 新增实体

```typescript
// HookConfig（持久化于 app_settings JSON 或独立表）
interface HookConfigStore {
  hooks: Partial<Record<HookEvent, HookMatcherGroup[]>>;
}

// AwarenessProfile / MemoryEntry —— 见 §5.2

// ConnectorState（连接器连接状态，扩展现有 MCPServer 实体）
interface ConnectorState {
  connectorId: string;        // builtin_browser | sys_calendar | m365 | dingtalk ...
  category: 'builtin' | 'market';
  installed: boolean;
  authStatus: 'none' | 'pending' | 'authorized';
  serverRef?: string;         // 关联 MCPServer.id
}

// WorkbenchModeConfig
interface WorkbenchModeConfig {
  enabledModes: ('general' | 'design' | 'slides' | 'writing')[];
  defaultMode: 'general' | 'design' | 'slides' | 'writing';
  slideTemplates: { id: string; name: string; source: 'builtin' | 'custom'; path: string }[];
}

// ArchiveRecord（任务归档）
interface ArchiveRecord {
  taskId: string;
  workspaceId: string;
  archivedAt: Date;
}

// AppSettings 增量字段
interface AppSettingsExtension {
  language: 'zh' | 'en';
  theme: 'light' | 'dark' | 'system';
  uiStyle: 'default' | 'glass' | 'classic' | 'parchment';
  chatFont: 'sans' | 'serif';
  chatFontSize: number;
  chatWidth: 'standard' | 'wide' | 'full';
  previewMode: 'in-app' | 'system';
  promptSuggestion: boolean;
  expandToolCalls: boolean;
  imShowToolProcess: boolean;
  autoStart: boolean;
  keepAwake: boolean;
  desktopNotify: boolean;
  badgeUnread: boolean;
  soundNotify: boolean;
  proxy: { mode: 'system' | 'custom'; url?: string };
  privacyMode: boolean;
  shortcuts: ShortcutConfig;
  computerUsePolicy: ComputerUsePolicy;
  secureEnv: boolean;          // 安全工作环境
  experiments: Record<string, boolean>; // 实验特性开关
}
```

### 6.2 SQLite 表增量

```sql
-- 意识工作手册与风格
CREATE TABLE awareness_profiles (
  id TEXT PRIMARY KEY,
  mode INTEGER DEFAULT 1,
  auto_memory INTEGER DEFAULT 1,
  style TEXT NOT NULL DEFAULT 'professional',
  custom_prompt TEXT DEFAULT '',
  work_manual TEXT DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 记忆条目
CREATE TABLE memory_entries (
  id TEXT PRIMARY KEY,
  content TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'auto',   -- auto | manual | reflection
  session_id TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 连接器状态
CREATE TABLE connector_states (
  connector_id TEXT PRIMARY KEY,
  category TEXT NOT NULL,                -- builtin | market
  installed INTEGER DEFAULT 0,
  auth_status TEXT NOT NULL DEFAULT 'none',
  server_ref TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 任务归档
CREATE TABLE archived_tasks (
  task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  workspace_id TEXT NOT NULL,
  archived_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Hooks 配置（JSON 整体存储，便于与 ~/.tongtian/settings.json 双写）
CREATE TABLE hook_configs (
  id TEXT PRIMARY KEY DEFAULT 'global',
  hooks_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_memory_source ON memory_entries(source);
CREATE INDEX idx_archived_workspace ON archived_tasks(workspace_id);
```

### 6.3 新增 IPC Channel

沿用 `domain:action` 命名规范与 `contextBridge` 白名单暴露：

| Channel | 方向 | 请求类型 | 响应类型 | 说明 |
|---------|------|----------|----------|------|
| `hooks:get` | R→M | `void` | `HookConfigStore` | 读取 Hooks 配置 |
| `hooks:set` | R→M | `HookConfigStore` | `{ ok: boolean }` | 保存配置（校验后） |
| `hooks:test` | R→M | `{ event: HookEvent; input: HookInput }` | `HookResult` | 试运行单个 Hook |
| `awareness:get` | R→M | `void` | `AwarenessProfile` | 获取意识配置 |
| `awareness:update` | R→M | `Partial<AwarenessProfile>` | `AwarenessProfile` | 更新意识配置 |
| `awareness:memories` | R→M | `{ source?: string }` | `MemoryEntry[]` | 记忆列表 |
| `awareness:memory:add` | R→M | `{ content: string }` | `MemoryEntry` | 手动添加记忆 |
| `awareness:memory:delete` | R→M | `{ id: string }` | `{ ok: boolean }` | 删除记忆 |
| `connector:list` | R→M | `{ category?: string }` | `ConnectorState[]` | 连接器列表 |
| `connector:authorize` | R→M | `{ connectorId: string }` | `ConnectorState` | 发起授权流 |
| `connector:revoke` | R→M | `{ connectorId: string }` | `{ ok: boolean }` | 撤销授权/断开 |
| `task:archive` | R→M | `{ taskId: string }` | `{ ok: boolean }` | 归档任务 |
| `task:unarchive` | R→M | `{ taskId: string }` | `{ ok: boolean }` | 恢复任务 |
| `task:archive:list` | R→M | `void` | `ArchiveRecord[]` | 归档列表 |
| `task:archive:clear` | R→M | `{ confirm: string }` | `{ ok: boolean }` | 清空归档（需确认词） |
| `workbench-mode:get` | R→M | `void` | `WorkbenchModeConfig` | 工作台模式配置 |
| `workbench-mode:set` | R→M | `WorkbenchModeConfig` | `{ ok: boolean }` | 更新模式配置 |
| `shortcut:register` | R→M | `ShortcutConfig` | `{ ok: boolean }` | 注册全局快捷键 |
| `quickpick:toggle` | R→M | `{ visible: boolean }` | `{ ok: boolean }` | QuickPick 显隐 |
| `voice:transcribe` | R→M | `{ start: boolean }` | `{ text?: string; tempFile?: string }` | 语音录音/转录 |
| `snapshot:capture` | R→M | `void` | `{ imagePath: string; text: string; appName: string }` | 应用快照 |
| `computer-use:get-policy` | R→M | `void` | `{ policy: ComputerUsePolicy }` | 操控策略 |
| `computer-use:set-policy` | R→M | `{ policy: ComputerUsePolicy }` | `{ ok: boolean }` | 更新策略 |
| `sandbox-env:toggle` | R→M | `{ enabled: boolean }` | `{ ok: boolean }` | 安全工作环境开关 |
| `sandbox-env:cleanup` | R→M | `{ confirm: string }` | `{ freedMB: number }` | 清理环境文件 |
| `app:update-check` | R→M | `void` | `{ hasUpdate: boolean; version?: string }` | 检查更新 |

新增事件（M→R）：`hook:blocked`（hook 阻塞通知）、`quickpick:submitted`（QuickPick 提交任务）、`computer-use:step`（操控四段式进度）。

### 6.4 Mock 开关（遵循开发规范）

| 依赖 | 开发模式（MOCK_ENABLED=true） | 联调模式 |
|------|------------------------------|----------|
| 连接器集成市场 | 本地 JSON 市场清单 | 远端市场 API |
| IM 频道 | 内存消息队列模拟 | 真实 IM Webhook/Bot SDK |
| 语音转录 | 返回固定样例文本 | 真实转录模型 API |
| OCR 快照文本 | 样例文本 | 本地 OCR 服务 |
| Hook 脚本执行 | 沙箱内可执行（本地命令，无需 mock） | 同左 |

---

## 7. 实施分期

### P0 · 信息架构与核心入口（第一迭代）

1. Settings 页三分组重组 + 偏好设置补齐 + 个人资料/系统设置分区（G01-G04）
2. 连接器中心页 + connector IPC + 授权流（G05）
3. 意识页 + awareness IPC + memory 模块对接（G06）
4. 归档页 + task:archive IPC（G07）
5. 侧边栏路由修正（IM 频道指向独立页预留）

### P1 · 核心扩展能力（第二迭代）

6. HookRunner 引擎 + Hooks 管理页（G08）
7. 电脑操控面板 + 策略设置 + ChatStream 四段式渲染（G09）
8. 工作台模式切换 + 设置分区（G10）
9. IM 频道独立页（G11）+ 快捷键设置分区（G17）+ 模型档位选择器（G18）

### P2 · 增强型本地能力（第三迭代）

10. 语音输入（G12）、应用快照（G13）、QuickPick（G14）
11. 安全工作环境设置入口（G15）、实验特性对齐（G16）

### 测试要点（对齐开发规范）

- 单元：HookRunner 事件匹配（matcher 四种写法）/ exit code 语义 / AwarenessService 注入逻辑 / ShortcutConfig 校验
- 契约：§6.3 全部新增 IPC channel 的 zod schema 一致性
- E2E：设置三分组深度链接导航；连接器授权流完整链路；Hook 拦截危险命令（PreToolUse exit 2）；归档恢复闭环

---

> **下一步**：运行 `/apex-sdd:plan` 将本整改方案拆解为有序实现任务。
