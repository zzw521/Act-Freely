---
name: design-orchestrator
description: Design orchestrator that intelligently routes to the right design skill. Use when designing UI/UX, creating prototypes, or building frontend projects. Automatically chooses between rule-based design systems (ui-ux-pro-max) and creative breakthrough (frontend-design) based on task requirements.
---

# Design Orchestrator

## Overview

This is a meta-skill that coordinates design workflows by intelligently selecting the appropriate design skill based on task requirements. It handles everything from interactive design to visual design to frontend project creation.

## Constraints

**All designs and implementations MUST comply with:**

- [`frontend-agent-constraints.md`](../../../../references/frontend-agent-constraints.md) - 前端开发约束规范（最高优先级）

**Key UI constraints:**

- Colors: **≤3 colors per page** (excluding black/white/gray)
- Gradients: **Avoid blue-purple** (AI-like), prefer brand colors
- Icons: **lucide** only (no emoji) - `strokeWidth={1.5}`
- Animations: **150-300ms**, ease-out, transform/opacity only
- Buttons: Unified style system (Primary/Secondary/Text/Danger)
- Components: No native `alert()`/`confirm()`/`<select>`

## When to Use

- Designing user interfaces (web, mobile, desktop)
- Creating interactive prototypes
- Designing visual identity and aesthetics
- Setting up new frontend projects
- When unsure which design approach to use

## Intelligent Routing Logic

Analyze the user's request and route to the appropriate skill:

### Route to `ui-ux-pro-max` when:

**信号词**: 企业级、后台、管理系统、规范、一致性、行业标准、SaaS、B2B、金融、医疗、电商

**场景特征**:

- 需要行业最佳实践和设计规范
- 企业级应用（管理系统、仪表盘、表单流程）
- 强调一致性和可维护性
- 有明确的行业属性（金融、医疗、电商等）
- 需要无障碍访问（WCAG）支持
- 多平台适配（响应式、移动端）

**示例请求**:

```
"设计一个订单管理后台"
"创建一个数据仪表盘"
"做 B2B SaaS 产品的用户界面"
"设计电商网站的结账流程"
```

### Route to `frontend-design` when:

**信号词**: 品牌、营销、创意、独特、令人印象深刻、大胆、视觉冲击力、官网、落地页

**场景特征**:

- 需要创意突破和独特审美
- 品牌官网、营销落地页
- 需要强烈的视觉识别度
- 实验性或艺术性项目
- 需要大胆的排版、色彩、动画
- "让人眼前一亮"的设计

**示例请求**:

```
"设计一个让人印象深刻的品牌官网"
"创建一个大胆创意的营销活动页面"
"做一个独特视觉的 portfolio"
"设计一个实验性的交互艺术页面"
```

### Route to Both Sequentially when:

某些项目需要两者结合:

```
第一阶段: ui-ux-pro-max
  → 生成设计系统规范（配色、字体、组件规则）

第二阶段: frontend-design
  → 基于规范实现创意突破
```

**示例**:

```
"先确定设计系统，然后创建一个独特的品牌落地页"
```

## Design Workflow

### Phase 1: Understand Requirements

Before routing, clarify:

```
REQUIRED INFORMATION:
1. Project type: (企业应用 / 品牌官网 / 营销页面 / 实验项目 / 其他)
2. Target audience: (内部用户 / 消费者 / 专业人士 / 其他)
3. Design priority: (规范性 / 创意性 / 两者兼顾)
4. Industry context: (金融 / 电商 / SaaS / 医疗 / 其他)
5. Technical constraints: (React / Vue / 原生 HTML / 其他)

If any is unclear, ASK before proceeding.
```

### Phase 2: Select Design Approach

Based on the analysis:

```
┌─────────────────────────────────────────┐
│  User Request                           │
└──────────────┬──────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────┐
│  Analyze:                               │
│  - Project type                         │
│  - Industry context                     │
│  - Design priorities                    │
│  - Creativity vs Consistency            │
└──────────────┬──────────────────────────┘
               │
        ┌──────┴──────┐
        ▼             ▼
┌──────────────┐ ┌──────────────┐
│ Rule-Based   │ │ Creative     │
│ (规范性)     │ │ (创意性)     │
└──────┬───────┘ └──────┬───────┘
       │                │
       ▼                ▼
┌──────────────┐ ┌──────────────┐
│ ui-ux-pro-max│ │ frontend-    │
│              │ │ design       │
└──────────────┘ └──────────────┘
```

### Phase 3: Execute Design

**If ui-ux-pro-max**:

1. 使用 Skill 工具调用 `ui-ux-pro-max`
2. Apply industry-specific rules
3. Generate design system (colors, typography, patterns)
4. Create UI with accessibility and best practices
5. Output: Design spec + working prototype

**If frontend-design**:

1. 使用 Skill 工具调用 `frontend-design`
2. Define BOLD aesthetic direction
3. Implement with creative typography, colors, motion
4. Ensure production-grade quality
5. Output: Distinctive, memorable interface

## Figma Source Branch (Optional)

When the caller (e.g., the `/apex-sdd:ui-design` command) provides a Figma file URL or `fileKey:nodeId`, switch to the Figma source branch instead of (or in addition to) reverse-engineering the prototype project.

### Trigger

- Caller passes a Figma URL like `https://www.figma.com/file/{fileKey}/{name}?node-id={nodeId}`, or `fileKey:nodeId`.
- Both URL shapes are accepted: `figma.com/design/...` (new default), `figma.com/file/...` (legacy), and Dev Mode links with `?m=dev`. The `node-id` may use `-` or `:` (e.g. `361-109357` ≡ `361:109357`).
- Project root has a `.mcp.json` registering the `figma` server (Framelink `figma-developer-mcp`). If missing, instruct the user to configure it per the `/apex-sdd:ui-design` command doc and abort with a clear message.

### Tool Sequence (figma-context-mcp)

1. `get_file` (or `get_node` when `nodeId` provided) — fetch the layout tree and frame hierarchy
2. `get_styles` — extract color / text / effect / grid styles as the source-of-truth design tokens
3. `get_components` — enumerate published components with their variants/states
4. `download_images` — export icons & illustrations into `docs/spec/assets/{业务}/`

### Token Normalization

Figma-extracted tokens MUST still comply with [`frontend-agent-constraints.md`](../../../../references/frontend-agent-constraints.md):

- Colors: ≤3 brand colors per page (excluding black/white/gray); reject blue–purple gradients (AI-like)
- Typography: keep Figma family/size/weight/line-height; flag Inter/Roboto/Arial-only palettes for review
- Animations: clamp durations to 150–300ms ease-out, transform/opacity only
- Component variants: must cover default / hover / active / disabled

If the Figma source violates a constraint, record it in a `设计冲突` appendix of the UI spec rather than silently rewriting.

### Cross-Validation

After token extraction, compare against the runnable prototype at `docs/prd/prd-{业务}-交互原型/`:

- Diffs <5% — ignore
- Diffs ≥5% — log under `设计差异` appendix with both sides shown
- Figma is authoritative; the prototype is a sanity check, not an override source

### Failure Mode — Fallback

If any figma-context-mcp call fails (auth error, network, file not accessible):

1. Do NOT abort the whole skill
2. Fall back to the prototype-reverse-engineering path (Phases 1–3 above)
3. Prepend a warning block to the generated `spec-{业务}-UI规范文档.md`:

   ```
   > ⚠️ Figma source unreachable; tokens reverse-engineered from prototype project. Re-run with valid FIGMA_API_KEY for authoritative tokens.
   ```

### Output Marker

The generated UI spec MUST start with a `数据来源` field:

```
数据来源：Figma
Figma URL：https://www.figma.com/file/xxx/yyy?node-id=1:2
抓取时间：2026-05-25T10:30:00+08:00
```

### Phase 4: Frontend Project Setup (Optional)

```
DECISION TREE:
- Need a new project? → Create repository
- Have existing project? → Integrate into it
- Just need prototype? → Single HTML/React file
```

#### Option A: Create New Repository

When user asks for a "项目"、"仓库"、"完整的 frontend":

1. **Use `frontend-project-scaffolder` skill** (see below)
2. Create project with best practices:
   - Modern build tool (Vite/Next.js)
   - TypeScript configured
   - Tailwind CSS or design system
   - ESLint + Prettier
   - Git initialized
   - README with setup instructions

#### Option B: Prototype Only

When user asks for "原型"、"demo"、"示例":

1. Generate single HTML file or React component
2. Include all styles inline or in same file
3. Make it immediately runnable
4. Document how to use it

## Frontend Project Creation

When creating a new frontend project, follow these rules:

### Repository Structure

```
project-name/
├── .gitignore
├── package.json
├── tsconfig.json
├── vite.config.ts (or next.config.js)
├── tailwind.config.js
├── postcss.config.js
├── eslint.config.js
├── README.md
├── index.html
└── src/
    ├── main.tsx
    ├── App.tsx
    ├── App.css
    ├── components/
    │   └── (按功能模块组织)
    ├── pages/
    │   └── (如果使用路由)
    ├── hooks/
    │   └── (自定义 hooks)
    ├── utils/
    │   └── (工具函数)
    ├── types/
    │   └── (TypeScript 类型定义)
    └── assets/
        └── (图片、字体等)
```

### Technology Stack Selection

**Default Stack** (unless user specifies otherwise):

- **Framework**: Preact 10+ with TypeScript
- **Build Tool**: Vite 6+
- **Styling**: Tailwind CSS 4+ (Vite plugin mode, no config file)
- **State Management**: Zustand 5+ (client state)
- **Routing**: preact-router 4+
- **HTTP Client**: Ky 1+
- **Linting**: ESLint 9+ (flat config)
- **Git Hooks**: Husky + lint-staged

**Alternative Stacks**:

- **React 18+**: If user prefers React ecosystem (React Router v6, TanStack Query, shadcn/ui)
- **Vue 3**: If user prefers Vue ecosystem (Pinia, Element Plus)
- **Astro**: For content-heavy, performance-critical sites
- **Next.js**: For SSR/SSG, SEO-critical sites

### Project Creation Steps

When user requests a new project, delegate to `frontend-project-scaffolder` skill which handles:

1. **Initialize Preact + Vite project** with TypeScript
2. **Install Tailwind CSS 4** (Vite plugin mode, no tailwind.config.js needed)
3. **Configure routing** with preact-router
4. **Setup state management** with Zustand
5. **Configure ESLint 9+** with flat config
6. **Create project structure** following best practices
7. **Initialize Git** with clean commit

**Key difference from React**: Uses `@theme` in CSS instead of tailwind.config.js for design tokens.

## Quality Gates

After design is complete:

### For ui-ux-pro-max outputs:

- [ ] Follows industry-specific design rules
- [ ] Color contrast meets WCAG AA (4.5:1)
- [ ] Responsive at 375px, 768px, 1024px, 1440px
- [ ] No anti-patterns from the industry list
- [ ] Typography hierarchy is clear
- [ ] Interactive elements have visible focus states

### For frontend-design outputs:

- [ ] Aesthetic direction is clear and intentional
- [ ] Typography is distinctive (no Inter/Roboto/Arial)
- [ ] Color palette is cohesive and bold
- [ ] Layout is memorable (not generic card grid)
- [ ] Animations enhance the experience
- [ ] No "AI slop" patterns (purple gradients, etc.)

### For frontend projects:

- [ ] Project builds without errors (`npm run build`)
- [ ] TypeScript compiles cleanly (`tsc --noEmit`)
- [ ] ESLint passes with no errors
- [ ] Tailwind CSS 4 `@theme` is configured in index.css
- [ ] README has setup instructions
- [ ] Git history is clean
- [ ] Dependencies are up-to-date

## Common Scenarios

### Scenario 1: Enterprise Dashboard

```
User: "设计一个数据分析仪表盘"

Route: ui-ux-pro-max
Reason: 企业级、数据可视化、需要行业规范
Output: Design system + React dashboard component
```

### Scenario 2: Brand Landing Page

```
User: "为我们的 AI 产品设计一个令人印象深刻的官网首页"

Route: frontend-design
Reason: 品牌官网、需要创意突破、视觉冲击力
Output: Distinctive, memorable landing page
```

### Scenario 3: Full Project

```
User: "创建一个完整的电商前台项目"

Route:
1. ui-ux-pro-max (design system)
2. frontend-project-scaffolder (Preact + Vite 6 + Tailwind CSS 4 project)
3. incremental-implementation (build features)

Output: Complete Preact project with e-commerce UI
```

### Scenario 4: Hybrid Approach

```
User: "先确定设计规范，然后做一个有创意的营销活动页"

Route:
1. ui-ux-pro-max (generate design system)
2. frontend-design (creative implementation using the system)

Output: Design spec + creative landing page
```

## Failure Modes to Avoid

1. **Routing to wrong skill**: Carefully analyze the request before choosing
2. **Skipping requirements clarification**: Ask questions if context is unclear
3. **Generic output**: Both skills should produce distinctive results
4. **Overcomplicating**: Start simple, add complexity only if needed
5. **Ignoring constraints**: Respect technical requirements (framework, performance, etc.)
6. **No verification**: Always run quality gates before delivering

## Integration with Other Skills

This orchestrator works with:

- **interactive-prototype**: For quick wireframes and flow validation
- **ui-ux-pro-max**: For rule-based, industry-specific design
- **frontend-design**: For creative, distinctive interfaces
- **incremental-implementation**: For production implementation
- **frontend-project-scaffolder**: For project setup
- **api-and-interface-design**: For API contract design
- **incremental-implementation**: For building features
- **browser-testing-with-devtools**: For testing in real browser

## Quick Reference

| Request Type                    | Route To                                 | Why                         |
| ------------------------------- | ---------------------------------------- | --------------------------- |
| Enterprise app, dashboard, form | ui-ux-pro-max                            | Industry rules, consistency |
| Brand site, marketing, creative | frontend-design                          | Unique aesthetics           |
| Quick wireframe, flow           | interactive-prototype                    | Fast validation             |
| Full project setup              | frontend-project-scaffolder              | Best practices              |
| Production implementation       | frontend-ui-designer                     | Code quality                |
| Figma source provided           | figma-context-mcp + frontend-ui-designer | Authoritative design tokens |
