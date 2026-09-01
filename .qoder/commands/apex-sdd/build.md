---
name: build
description: Implement tasks from an OpenSpec change, with mandatory visual fidelity verification for frontend tasks that have design mockups
---

## ⚠️ 路由决策（必须先执行此项）

**Step 0 — 判断 change 类型：**
- 读取 `docs/changes/<change-name>/` 下的设计文档，判断是前端/全栈应用还是纯后端服务
- 如果不确定，询问用户

**如果是后端应用或纯后端服务：**

→ **立即 STOP 阅读本文件**，不要再继续往下读任何前端相关的内容。

→ **主 agent 切换执行策略**：立即停止阅读 `build.md`，改为执行下文 `build-service.md` 描述的 Coding Loop。

→ **CRITICAL — 必须先读取 build-service.md**：立即使用 Read tool 读取`.qoder/commands/apex-sdd/build-service.md` 获取完整的后端编排流程，然后严格按照该流程执行。

  > **关键理解**：`build-service.md` 是**主 agent 的编排流程**，不是 auto-coding subagent 的流程。
  >
  > - **主 agent**（当前 agent）运行编排循环：dispatch subagent → Phase Gate Check → Code Review → 再 dispatch
  > - **auto-coding subagent** 是每轮迭代中的执行者：只完成一组任务就退出
  >
  > 不要试图让一个 subagent 在它内部再 dispatch subagent——这是主 agent 的责任。

→ **执行验证**：如果发现以下情况之一，意味着你没有正确切换策略，必须立即修正：
  - 你在自己写 Java/Go/Python 代码（而不是 dispatch subagent）
  - 你在手动创建项目文件（而不是让 auto-coding subagent 去做）
  - 你没有使用 Agent tool。
  
  **CHECK**: `build-service.md` 的内容是否已加载到当前上下文？如果没有 → 立即读取它。

**如果是前端或全栈应用：** → 继续阅读下文，按本文件的前端实现流程执行。

---

**Input:**

| 输入项               | 路径                                        | 说明                                           |
| -------------------- | ------------------------------------------- | ---------------------------------------------- |
| BE-API 契约文档      | `docs/spec/spec-{业务}-API契约文档.yaml`    | OpenAPI 规范接口契约，供前端接口联调使用       |
| FE-模块设计文档      | `docs/spec/spec-{业务}-前端模块设计文档.md` | 含页面、组件与状态拆分的模块设计说明书         |
| UI 规范文档（可选）  | `docs/spec/spec-{业务}-UI规范文档.md`       | 若已执行 `/apex-sdd:ui-design`，最高视觉优先级 |
| PRD 交互原型（可选） | `docs/prd/prd-{业务}-交互原型/`             | 未执行 ui-design 时作为视觉与交互参考          |

**Output:**

| 输出材料     | 格式     | 说明                                      |
| ------------ | -------- | ----------------------------------------- |
| 前端工程源码 | 源码文件 | 符合编码规范的生产级前端源代码            |
| 单元测试代码 | 源码文件 | 覆盖核心逻辑的前端单元测试                |
| 单元测试报告 | 报告文档 | 测试通过率与行覆盖率 ≥ 60% 汇总           |
| 视觉验收报告 | 报告文档 | 每个页面的设计稿对比截图及 P0/P1 问题清单 |

**Input (command arg):** Optionally specify a change name. If omitted, check if it can be inferred from conversation context. If vague or ambiguous you MUST prompt for available changes.

**Steps**

1. **Select the change**

   If a name is provided, use it. Otherwise:

   - Infer from conversation context if the user mentioned a change
   - Auto-select if only one active change exists
   - If ambiguous, list `docs/changes/` directories, and use the **AskUserQuestion tool** to let the user select

   Always announce: "Using change: <name>" and how to override (e.g., `/apex-sdd:build <other>`).

2. **Read status from filesystem**

   Read `tasks.md` directly and count `- [x]` (done) vs `- [ ]` (pending) tasks.

3. **Read context files (proposal, design, specs)**

   Read the following files for context:

   - `docs/changes/<name>/specs/**/*.md`
   - `docs/changes/<name>/tasks.md`

4. **Detect design mockups & UI spec (frontend tasks only)**

   Before implementing any frontend task (page, component, layout), check for design mockups & UI 规范 in the following priority order:

   | Source       | 查找路径/方式                                                                       |
   | ------------ | ----------------------------------------------------------------------------------- |
   | UI 规范文档  | `docs/spec/spec-{业务}-UI规范文档.md`（若已执行 `/apex-sdd:ui-design`，最高优先级） |
   | 交互原型截图 | `docs/prd/prd-{业务}-交互原型/` 下的页面截图或原型工程                              |
   | 设计稿图片   | 用户在对话中提供的图片附件（PNG / JPG / Figma 链接）                                |
   | 设计稿路径   | 任务描述或 spec 中明确指定的 `design:` 字段                                         |

   **Decision:**

   - **有设计稿或 UI 规范文档** → 标记为 `visual-gate: required`，任务完成前必须通过视觉验收
   - **无设计稿且无 UI 规范文档** → 标记为 `visual-gate: skipped`，跳过视觉验收步骤，按普通 TDD 流程完成

   > 如果不确定是否存在设计稿/规范，询问用户："此前端任务是否有对应的设计稿、原型截图或 UI 规范文档？"

   **加载设计上下文（若存在 UI 规范文档）：**

   - 调用 `frontend-ui-designer` skill 加载并解析 `spec-{业务}-UI规范文档.md`，提取：design tokens（色板/字号/间距/圆角/阴影）、组件规范（Button/Input/Modal 等）、交互动效约束、无障碍要求
   - 将解析结果作为 `ui-to-code` agent 的强制输入约束（Tailwind theme / CSS 变量 / 组件库映射），确保实现与设计系统一致
   - 同步遵循 [`references/frontend-agent-constraints.md`](../../../references/frontend-agent-constraints.md)（最高优先级前端开发约束）

5. **Show current progress**

   Display:

   - Schema being used
   - Progress: "N/M tasks complete"
   - Remaining tasks overview
   - Visual gate status per frontend task (`required` / `skipped`)

6. **Implement tasks**

   **前端编码原则（所有前端任务适用）：**

   - **遵循编码规范**：遵循团队命名、分层、异常处理等编码规范；参考代码库同类实现，保持风格一致
   - **遵循分层架构**：Page → Container → Component，模块高内聚低耦合，单向数据流

   ### 6-Pre-Check — 编码前约束自检（每个前端任务开始前强制执行）

   在编写任何前端代码前，必须逐项确认以下约束，**违反任一项则不允许提交代码**：

   | #   | 约束项                                                                           | 检查方式                                                                                             |
   | --- | -------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
   | 1   | **禁止使用 Emoji 作为图标**（🛒📦📍💳🎁 等）                                     | 搜索代码中的 emoji 字符，全部替换为 `lucide-react` 图标                                              |
   | 2   | **禁止蓝紫渐变**（`from-blue-* to-purple-*`、`from-indigo-*`、`to-violet-*` 等） | 搜索 `from-blue\|to-purple\|from-purple\|to-blue\|from-indigo\|to-indigo` 并替换为品牌色或同色系渐变 |
   | 3   | **图标统一使用 lucide-react**，strokeWidth 1.5~2.2                               | 检查无 `<img>` 小图标、无 SVG 内联、无第三方图标库                                                   |
   | 4   | **单页面颜色 ≤ 3 种**（黑白灰不计）                                              | 视觉审查：品牌主色 + 辅助色 + 功能色                                                                 |
   | 5   | **按钮风格统一**：使用全局定义的 `.btn-primary-gradient` / `bg-mall-primary` 等  | 禁止页内临时定义按钮颜色                                                                             |
   | 6   | **占位图来自 Unsplash/Pexels**，使用 CDN URL                                     | 禁止 placehold.co / 本地图片                                                                         |

   > **执行规则**：每次生成代码前先过一遍以上清单，生成代码后用 `grep` 验证 emoji 和蓝紫渐变为 0。
   >
   > **典型错误示例（禁止出现）**：
   >
   > ```tsx
   > // ❌ 禁止
   > const STATUS_ICONS = { PENDING: '📦', SHIPPED: '🚚' };
   > <div class='bg-gradient-to-r from-mall-primary to-blue-600'>
   > const menuItems = [{ icon: '📍', ... }, { icon: '🎉', ... }];
   >
   > // ✅ 正确
   > import { Package, Truck, MapPin, Gift } from 'lucide-react';
   > const STATUS_ICONS = { PENDING: Package, SHIPPED: Truck };
   > <div class='bg-gradient-to-r from-mall-primary to-mall-primary-light'>
   > ```

   ### 6-Pre — Frontend 工程初始化（首次执行前端任务前运行一次）

   在开始任何前端编码任务前，检查目标前端工程是否已初始化：

   1. **检查目标工程目录**：确认 `web/{业务}/` 是否已存在且包含工程文件（`package.json` 等）
   2. **若目标工程不存在**，按以下优先级初始化：

      | 情况                                                 | 操作                                                                                                                            |
      | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
      | `docs/prd/prd-{业务}-交互原型/` 存在且为完整前端工程 | **拷贝交互原型工程**到 `web/{业务}/`，作为初始工程骨架                                                                          |
      | 交互原型不存在                                       | 读取 FE-模块设计文档（技术栈、路由规划、状态管理需求、目录结构约定），调用 `frontend-project-scaffolder` skill 按规格初始化工程 |

   3. **拷贝后校验**：确认工程可正常安装依赖并启动（`npm install && npm run dev`），无编译错误后再开始编码
   4. **若目标工程已存在**：跳过此步骤，直接进入 6A/6B/6C

   > 此步骤**只执行一次**，工程初始化完成后标记为已完成，后续任务不再重复执行。

   ### 6A — Frontend tasks with `visual-gate: required`（有设计稿/UI 规范，必须视觉验收）

   **加载设计系统**（编码前置步骤，二选一路径）：

   | 路径                  | 触发条件                                            | 操作                                                                                                                    |
   | --------------------- | --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
   | **Path A — 规范驱动** | 存在 `spec-{业务}-UI规范文档.md`                    | 调用 `frontend-ui-designer` skill 加载 design tokens、组件规范、交互约束，作为后续编码的强制输入                        |
   | **Path B — 视觉驱动** | 仅有参考视觉（设计稿图片/原型截图），无 UI 规范文档 | 先调用 `ui-to-code` agent 做 VL 分析，提取组件树 + 视觉规格（色值/间距/字号/圆角/阴影）作为虚拟设计系统，再进入编码阶段 |

   > Path B 中 `ui-to-code` 的 VL 分析输出替代 UI 规范文档的角色，后续阶段 2【视觉】的差异对比以该提取规格为基准。

   **单元测试规范（适用于 6A/6B 所有任务）：**

   | 要素     | 规范                                  |
   | -------- | ------------------------------------- |
   | 覆盖率   | 行覆盖率 ≥ 60%，关键路径必须覆盖      |
   | 用例范围 | 正常路径、异常路径、边界值均需覆盖    |
   | 断言方式 | 针对返回值、状态变更与副作用分别断言  |
   | 依赖处理 | 外部依赖使用 Mock，禁止访问真实中间件 |

   按以下 **5 阶段渐进式编码** 顺序执行：

   #### 阶段 1 —【框架】实现静态页面结构

   对照原型图/原型工程，遵循「先布局外层容器，再开发内部细节；先实现原子组件，再完成原子组合」原则：

   | 原型资源   | 执行方式                                               |
   | ---------- | ------------------------------------------------------ |
   | 有原型工程 | 拷贝原型文件到 web 目录，参照 FE-模块设计文档更新框架  |
   | 有原型图   | 参照 FE-模块设计文档，调用 `ui-to-code` agent 生成代码 |
   | 原型缺失   | 参照 FE-模块设计文档，手动完成布局框架                 |

   #### 阶段 2 —【视觉】UI 还原（视觉门禁核心）

   1. **VL 分析**：
      - Path A：调用 `ui-to-code` agent 对照 UI 规范文档分析设计稿/原型截图，输出组件树 + 视觉规格，并与 UI 规范对齐
      - Path B：前置步骤已完成 VL 分析，直接使用提取的视觉规格开始还原
   2. **像素级还原**：所有 token（颜色/字号/间距/圆角/阴影）必须引用设计系统定义的 CSS 变量或 Tailwind theme，禁止硬编码魔法值
   3. **浏览器验证**：启动开发服务器，调用 `browser-testing-with-devtools` skill 截图
   4. **差异分析**：对比设计稿/UI 规范与截图，输出 P0~P3 分级差异报告
   5. **差异修复**：修复所有 P0 + P1 问题，迭代直至视觉相似度 ≥ 90/100

   #### 阶段 3 —【交互】交互逻辑与 Mock 数据调试

   对照原型工程/FE-模块设计文档：

   - 实现页面内交互逻辑（表单校验、状态切换、路由跳转、动效等）
   - 接入 Mock 数据，验证数据驱动的 UI 状态（加载态、空态、错误态、正常态）
   - 调用 `browser-testing-with-devtools` skill 验证交互无 Console 错误

   #### 阶段 4 —【数据】接口对接与联调

   对照 BE-API 契约文档：

   - 关闭 Mock 开关，替换为真实 API 调用
   - 验证请求参数、响应数据映射、错误处理逻辑
   - 确保接口联调通过，数据流与业务逻辑一致

   #### 阶段 5 —【优化】性能优化与验收

   - 响应式适配：验证移动端/桌面端布局正确
   - 边界测试：空数据、超长文本、网络异常等边界场景
   - 性能检查：按需加载、无不必要重渲染
   - 运行完整测试套件 + 构建检查
   - Commit with a descriptive message（提交信息注明视觉相似度得分）
   - Mark task complete: `- [ ]` → `- [x]`

   **Frontend task 交付审核清单（标记完成前全部通过）：**

   **架构合规**

   - [ ] 代码遵循模块边界，无跨域非法调用
   - [ ] 全局 State 使用规范，无滞用替代 Props 传递的情况
   - [ ] 严格使用约定技术栈，无引入额外依赖
   - [ ] 遵循分层架构：Pages → Components → UI/Base → Hooks/Utils

   **业务逻辑**

   - [ ] 交互行为与原型/设计稿一致（重点：响应式布局与间距）
   - [ ] 异常处理覆盖关键边界（网络超时、404/500、空数据 Empty State）
   - [ ] 异步操作有 Loading 状态与防抖/节流，无重复提交风险
   - [ ] 路由跳转与权限控制与设计文档一致

   **代码质量**

   - [ ] 命名与注释符合前端规范
   - [ ] 无重复 UI 结构或逻辑，公共部分已抽取为组件或 Custom Hook
   - [ ] 无安全漏洞（XSS、不安全的 `dangerouslySetInnerHTML`、敏感信息泄露）
   - [ ] **无 Emoji 图标**（执行 `grep -rn '[\U0001F300-\U0001F9FF]' src/` 结果为 0）
   - [ ] **无蓝紫渐变**（执行 `grep -rn 'from-blue\|to-purple\|from-indigo\|to-violet' src/` 结果为 0）

   **契约一致性**

   - [ ] 请求参数与响应解析与 API 契约完全一致（字段名、数据类型）
   - [ ] 接口联调全部通过，错误码与提示信息已闭环
   - [ ] 无过期接口版本，新增字段已同步更新 TS 类型定义

   **交付前确认**

   - [ ] P0 视觉问题全部清除
   - [ ] P1 视觉问题全部清除
   - [ ] 视觉相似度评分 ≥ 90/100
   - [ ] 浏览器 Console 无错误
   - [ ] TypeScript 无类型错误
   - [ ] 单元测试全部通过，行覆盖率 ≥ 60%
   - [ ] 响应式适配验证通过
   - [ ] 实现遵循 UI 规范文档（design tokens / 组件规范 / 交互约束）
   - [ ] 符合 `references/frontend-agent-constraints.md` 约束（颜色 ≤3、lucide 图标、动效 150-300ms 等）
   - [ ] 前端构建通过，无 ESLint/Prettier 报错

   ### 6B — Frontend tasks with `visual-gate: skipped`（无设计稿）

   同样按 5 阶段渐进式编码执行，视觉阶段降级为布局正常性检查：

   - **阶段 1【框架】**：同 6A 阶段 1，根据原型资源情况选择对应方式
   - **阶段 2【视觉】（降级）**：启动开发服务器，截图确认页面正常渲染（无白屏、无布局断裂），Console 无错误即可通过
   - **阶段 3【交互】**：同 6A 阶段 3
   - **阶段 4【数据】**：同 6A 阶段 4
   - **阶段 5【优化】**：同 6A 阶段 5

   **Pause if:**

   - Task is unclear → ask for clarification
   - Implementation reveals a design issue → suggest updating artifacts
   - Error or blocker encountered → report and wait for guidance
   - User interrupts

7. **On completion or pause, show status**

   Display:

   - Tasks completed this session
   - Overall progress: "N/M tasks complete"
   - For each frontend task: visual similarity score + P0/P1 issue count
   - If all done: suggest archive
   - If paused: explain why and wait for guidance

**Output During Implementation**

```
## Implementing: <change-name> (schema: <schema-name>)

Working on task 3/7: <task description> [frontend · visual-gate: required]
[...VL 分析 → 编码 → 浏览器截图 → 差异修复...]
✓ Task complete (视觉相似度: 94/100, P0: 0, P1: 0)

Working on task 4/7: <task description> [frontend · visual-gate: skipped]
[...布局检查 → 交互调试 → 接口联调...]
✓ Task complete
```

**Output On Completion**

```
## Implementation Complete

**Change:** <change-name>
**Schema:** <schema-name>
**Progress:** 7/7 tasks complete ✓

### Completed This Session
- [x] Task 1 (frontend · visual-gate: required · 视觉相似度: 94/100)
- [x] Task 2 (frontend · visual-gate: skipped)
...

### Visual Fidelity Summary
| 任务 | 设计稿 | 相似度 | P0 | P1 | Known Issues |
|------|--------|--------|----|----|--------------|
| 首页 Banner | prototype-screenshot.png | 94/100 | 0 | 0 | P2: 圆角差异 |

All tasks complete! You can move to testing with `/apex-sdd:test`.
```

**Output On Pause (Issue Encountered)**

```
## Implementation Paused

**Change:** <change-name>
**Schema:** <schema-name>
**Progress:** 4/7 tasks complete

### Issue Encountered
<description of the issue>

**Options:**
1. <option 1>
2. <option 2>
3. Other approach

What would you like to do?
```

**Guardrails**

> **[最高优先级 - 凌驾所有规则]** 当用户提供了设计工程（交互原型工程、Figma 导出工程、UI 设计工程等任何形式的设计工程），前端页面实现**必须与设计工程完全一致**，涵盖布局结构、视觉样式、组件层次、交互行为、动效细节、色彩/字体/间距等全部设计细节。**不得以任何理由（包括但不限于简化实现、技术限制、规范冲突、视觉相似度已达标）偏离设计工程**。此规则的优先级高于本文件所有其他规则、编码规范及视觉相似度评分标准。

- Keep going through tasks until done or blocked
- Always read context files before starting (from the apply instructions output)
- If task is ambiguous, pause and ask before implementing
- If implementation reveals issues, pause and suggest artifact updates
- Keep code changes minimal and scoped to each task
- Update task checkbox immediately after completing each task
- Pause on errors, blockers, or unclear requirements - don't guess
- Use contextFiles from CLI output, don't assume specific file names
- **[通用]** 遵循团队命名、分层、异常处理等编码规范；参考代码库同类实现，保持风格一致
- **[Frontend]** 遵循分层架构：Page → Container → Component，模块高内聚低耦合，单向数据流
- NEVER mark a task complete if `visual-gate: required` and P0/P1 issues remain unresolved
- NEVER skip browser screenshot verification — screenshots are the acceptance standard
- NEVER skip 阶段 3【交互】和阶段 4【数据】— 视觉通过不等于任务完成
- If design mockup is discovered mid-task (user provides it late), switch to `visual-gate: required` immediately and run the ui-to-code pipeline before marking complete
- **[Frontend]** 若存在 `spec-{业务}-UI规范文档.md`，**必须**先调用 `frontend-ui-designer` skill 加载设计系统再开始编码，禁止脱离规范自由发挥
- **[Frontend]** 所有视觉相关 token（color/spacing/font/radius/shadow）**必须**通过 UI 规范定义的 CSS 变量或 Tailwind theme 引用，禁止硬编码

**Next step:** Run `/apex-sdd:test` and `/apex-sdd:review` in parallel to verify implementations and conduct code review simultaneously.

> Suggested prompt（测试与审查并行执行）：
>
> ```
> 请用两个子 Agent 并行执行以下两个任务，不要串行：
>
> 子 Agent 1（测试）：执行 /apex-sdd:test 命令，业务：{业务}，基于 docs/prd/prd-{业务}-交互流程说明.md 和 docs/spec/spec-{业务}-API契约文档.md 生成端到端测试用例并执行，覆盖所有业务流程
>
> 子 Agent 2（审查）：执行 /apex-sdd:review 命令，对 {业务} 最新提交的代码进行五轴质量评审（正确性、可读性、架构、安全、性能）
>
> 两个子 Agent 同时启动，各自独立完成，最后汇报各自产出结果。
> ```

**Fluid Workflow Integration**

This skill supports the "actions on a change" model:

- **Can be invoked anytime**: Before all artifacts are done (if tasks exist), after partial implementation, interleaved with other actions
- **Allows artifact updates**: If implementation reveals design issues, suggest updating artifacts - not phase-locked, work fluidly
