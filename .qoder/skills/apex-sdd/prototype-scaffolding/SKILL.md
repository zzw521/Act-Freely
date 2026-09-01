---
name: prototype-scaffolding
description: Generates the per-application 交互原型 frontend project scaffold (folder, not single HTML) for 前后端应用. Use after 业务模块说明 / 业务架构说明 / 交互流程说明 are produced. Internally orchestrates frontend-project-scaffolder (init project + tooling) then frontend-ui-designer (beautify pages). Output: docs/prd/prd-{业务}-交互原型/ (runnable Vite project).
---

# Skill · prototype-scaffolding

## Overview

将 `prd-{业务}-交互流程说明.md` 中的页面清单与流程，结合 `prd-{业务}-业务模块说明.md` 中的用户故事与验收标准，转化为一个**可运行、可点击、可预览**的前端工程目录，作为 `前后端应用` 的第 4 份需求交付物。

> **Core Philosophy**：交互原型不是静态 HTML，而是结构化的前端工程，便于下游直接复用骨架与路由。
> **命名约定**：所有 `{业务}` 为占位符。

本 skill 是一个**编排层**，自身不直接编写组件代码，而是顺序调用：

1. `frontend-project-scaffolder` — 初始化工程与工具链
2. `frontend-ui-designer` — 美化界面（按场景路由到 `ui-ux-pro-max` 或 `frontend-design`）

---

## Inputs / Preconditions

调用前必须满足：

- [ ] 当前应用类型为 `前后端应用`（`纯后端应用` 不调用本 skill）
- [ ] `docs/prd/prd-{业务}-业务模块说明.md` 已存在
- [ ] `docs/prd/prd-{业务}-业务架构说明.md` 已存在
- [ ] `docs/prd/prd-{业务}-交互流程说明.md` 已存在（含页面清单与用户流程图）
- [ ] 业务名 `{业务}` 已确定（短中英文名，如 `B2C商城`）

任一缺失 → 终止并要求 `product-designer` 先补齐。

---

## Output Contract

**输出目录**：`docs/prd/prd-{业务}-交互原型/`（注意：**目录**，非 `.html` 单文件）

**最小工程结构**（默认 Preact + Vite + Tailwind 4 栈）：

```
docs/prd/prd-{业务}-交互原型/
├── package.json
├── vite.config.ts
├── tsconfig.json
├── eslint.config.js
├── index.html
├── README.md                  # 启动方式、页面清单、与上游需求文档的对应关系
└── src/
    ├── main.tsx
    ├── App.tsx                # 路由表，与交互流程说明页面清单 1:1
    ├── index.css              # Tailwind 4 @theme（完整 50–900 色阶）
    ├── components/
    │   ├── ui/               # Button / Card / Input 等基础组件
    │   └── Layout/
    ├── pages/                # 与路由表 1:1 的页面文件
    ├── stores/               # zustand mock 状态
    ├── mocks/                # mock 数据（脱敏样本，覆盖正常+异常场景）
    └── types/
```

**可运行性要求**：

- `npm install && npm run dev` 可启动本地预览
- `npm run build` 通过
- `tsc --noEmit` 通过
- 所有页面在路由表中可达，每条核心流程有可点击的 demo 路径

---

## Execution Flow

### Step 1 — Plan pages & routes

读取 `docs/prd/prd-{业务}-交互流程说明.md`，抽取：

- **页面清单**：层级结构 → 路由表
- **用户流程图**：核心流程 → 页面跳转链路
- **关联 US**：从 `prd-{业务}-业务模块说明.md` 反查每页对应的用户故事，作为页面 mock 数据与交互的依据

输出物（内部使用，不落盘）：

| 路由路径 | 页面名称 | 关联 US 编号 | 流程依赖 |
|---------|---------|------------|---------|
| `/` | 首页 | US-XXX-001 | — |
| `/login` | 登录 | US-USER-001 | 进入受保护页前 |
| `...` | `...` | `...` | `...` |

**检查点**：

- [ ] 每个页面均有对应路由
- [ ] 每条核心流程的所有页面均在路由表内
- [ ] 异常分支页面（如 404、错误提示页）已纳入

### Step 2 — Scaffold

调用 `frontend-project-scaffolder` skill：

- **目标目录**：`docs/prd/prd-{业务}-交互原型/`
- **项目名**：`prd-{业务}-交互原型`
- **栈**：默认（Preact + Vite 6 + Tailwind 4 + zustand + preact-router + ky）
- **要求**：
  - 路由表与 Step 1 输出 1:1
  - 每个页面先生成占位组件（含页面标题 + TODO 注释 + 关联 US 链接）
  - `README.md` 包含：启动方式、页面清单表、与 `prd-{业务}-交互流程说明.md` 的对应关系

完成后产物：可 `npm run dev` 启动的最小骨架。

### Step 3 — Beautify

调用 `frontend-ui-designer` skill（编排器）：

- **路由决策**：
  - 企业级/管理类应用（如 B 端控制台、BPaaS 中台）→ `ui-ux-pro-max`
  - 营销/品牌/2C 创意 → `frontend-design`
- **任务范围**：
  - 实现 Layout（Header / Sidebar / Footer，按页面层级）
  - 充实每个页面的内容、组件、表单、列表、状态空态
  - 注入 `mocks/` 数据，覆盖正常 + 异常分支
  - 实现交互流程图中的关键跳转与状态变化
  - 遵循 `frontend-agent-constraints.md`（lucide 图标、≤3 主色、150–300ms 动效、统一按钮系统）

---

## Quality Gates

| 检查项 | 规则 | 阈值 |
|--------|------|------|
| 工程可构建 | `npm run build` 成功 | 0 错误 |
| 类型检查 | `tsc --noEmit` 通过 | 0 错误 |
| Lint | `npm run lint` 通过 | 0 错误 |
| 路由覆盖 | 路由数 = 交互流程说明页面数 | 100% |
| 流程可达 | 每条核心流程在浏览器可完整点完 | 100% |
| 异常分支 | 异常页面/状态可触达 | 100% |
| UI 约束 | lucide 图标、无 emoji、≤3 主色 | 100% |
| 输出形态 | 目录而非单 HTML | 必须 |

任何一项 FAIL 即返回对应步骤修复。

---

## Skill Orchestration

| Sub-skill | 调用阶段 | 角色 |
|-----------|---------|------|
| `frontend-project-scaffolder` | Step 2 | 初始化 Vite 工程与工具链 |
| `frontend-ui-designer` | Step 3 | 路由到 `ui-ux-pro-max` / `frontend-design`，完成视觉与交互实现 |

```mermaid
graph TB
    A[Read 交互流程说明 + 业务模块说明] --> B[Step 1: Plan routes]
    B --> C[Step 2: frontend-project-scaffolder]
    C --> D[Step 3: frontend-ui-designer]
    D --> E[Quality gates]
    E -->|PASS| F[Done: docs/prd/prd-{业务}-交互原型/]
    E -->|FAIL| C
```

---

## Rules

1. 输出形态**必须是目录**，禁止产出单个 `.html` 文件作为交互原型
2. 工程目录必须自包含（`npm install` 后即可运行，不依赖外部全局配置）
3. 项目目录名固定为 `prd-{业务}-交互原型`，置于 `docs/prd/` 下
4. 路由表必须严格对齐 `prd-{业务}-交互流程说明.md` 的页面清单
5. 必须先调用 `frontend-project-scaffolder` 再调用 `frontend-ui-designer`，顺序不可颠倒
6. 美化阶段必须遵循 `references/frontend-agent-constraints.md`
7. 不得在原型中接入真实后端 API，全部使用 mock 数据
8. README.md 必须列出页面清单与上游 `prd-{业务}-交互流程说明.md` 的双向对应关系

## 禁止事项

- 禁止将交互原型生成为单个 HTML 文件
- 禁止跳过 Step 1 的路由规划，直接进入 scaffold
- 禁止省略 Step 3 的美化阶段（占位骨架不算交付物）
- 禁止与生产前端工程混用同一目录（原型属于 `docs/`，不属于 `web/`）
- 禁止由 `纯后端应用` 调用本 skill

---

## Composition

- **Invoke directly when**：`product-designer` 已完成 `前后端应用` 的前 3 份需求交付物，需要补齐第 4 份交互原型工程目录
- **Caller**：仅 `product-designer`（personas 不互相调用）
- **Typical workflow**：
  ```
  product-designer
    → requirements-split（产 业务模块说明 + 业务架构说明 + 交互流程说明）
    → prototype-scaffolding（产 prd-{业务}-交互原型/ 工程目录）
        ├── frontend-project-scaffolder（初始化）
        └── frontend-ui-designer（美化）
  ```
- **Do not invoke from another persona.** 编排由 `product-designer` 或用户/slash command 触发。
