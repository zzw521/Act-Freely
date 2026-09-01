---
name: planning-and-task-breakdown
description: Breaks work into ordered tasks. Use when you have a spec or clear requirements and need to break work into implementable tasks. Use when a task feels too large to start, when you need to estimate scope, or when parallel work is possible.
---

# Planning and Task Breakdown

## Overview

Decompose work into small, verifiable tasks with explicit acceptance criteria. Good task breakdown is the difference between an agent that completes work reliably and one that produces a tangled mess. Every task should be small enough to implement, test, and verify in a single focused session.

## When to Use

- You have a spec and need to break it into implementable units
- A task feels too large or vague to start
- Work needs to be parallelized across multiple agents or sessions
- You need to communicate scope to a human
- The implementation order isn't obvious

**When NOT to use:** Single-file changes with obvious scope, or when the spec already contains well-defined tasks.

## The Planning Process

### Step 1: Enter Plan Mode

Before writing any code, operate in read-only mode:

- Read the spec and relevant codebase sections
- Identify existing patterns and conventions
- Map dependencies between components
- Note risks and unknowns

**Do NOT write code during planning.** The output is a plan document, not implementation.

### Step 2: Identify the Dependency Graph

Map what depends on what:

```
Database schema
    │
    ├── API models/types
    │       │
    │       ├── API endpoints
    │       │       │
    │       │       └── Frontend API client
    │       │               │
    │       │               └── UI components
    │       │
    │       └── Validation logic
    │
    └── Seed data / migrations
```

Implementation order follows the dependency graph bottom-up: build foundations first.

### Step 3: Slice Vertically

Instead of building all the database, then all the API, then all the UI — build one complete feature path at a time:

**Bad (horizontal slicing):**
```
Task 1: Build entire database schema
Task 2: Build all API endpoints
Task 3: Build all UI components
Task 4: Connect everything
```

**Good (vertical slicing):**
```
Task 1: User can create an account (schema + API + UI for registration)
Task 2: User can log in (auth schema + API + UI for login)
Task 3: User can create a task (task schema + API + UI for creation)
Task 4: User can view task list (query + API + UI for list view)
```

Each vertical slice delivers working, testable functionality.

### Step 4: Write Tasks

Each task follows this structure:

```markdown
## Task [N]: [Short descriptive title]

**Description:** One paragraph explaining what this task accomplishes.

**Acceptance criteria:**
- [ ] [Specific, testable condition]
- [ ] [Specific, testable condition]

**Verification:**
- [ ] Tests pass: `npm test -- --grep "feature-name"`
- [ ] Build succeeds: `npm run build`
- [ ] Manual check: [description of what to verify]

**Dependencies:** [Task numbers this depends on, or "None"]

**Files likely touched:**
- `src/path/to/file.ts`
- `tests/path/to/test.ts`

**Estimated scope:** [Small: 1-2 files | Medium: 3-5 files | Large: 5+ files]
```

### Step 5: Order and Checkpoint

Arrange tasks so that:

1. Dependencies are satisfied (build foundation first)
2. Each task leaves the system in a working state
3. Verification checkpoints occur after every 2-3 tasks
4. High-risk tasks are early (fail fast)

Add explicit checkpoints:

```markdown
## Checkpoint: After Tasks 1-3
- [ ] All tests pass
- [ ] Application builds without errors
- [ ] Core user flow works end-to-end
- [ ] Review with human before proceeding
```

## Task Sizing Guidelines

| Size | Files | Scope | Example |
|------|-------|-------|---------|
| **XS** | 1 | Single function or config change | Add a validation rule |
| **S** | 1-2 | One component or endpoint | Add a new API endpoint |
| **M** | 3-5 | One feature slice | User registration flow |
| **L** | 5-8 | Multi-component feature | Search with filtering and pagination |
| **XL** | 8+ | **Too large — break it down further** | — |

If a task is L or larger, it should be broken into smaller tasks. An agent performs best on S and M tasks.

**When to break a task down further:**
- It would take more than one focused session (roughly 2+ hours of agent work)
- You cannot describe the acceptance criteria in 3 or fewer bullet points
- It touches two or more independent subsystems (e.g., auth and billing)
- You find yourself writing "and" in the task title (a sign it is two tasks)

## Contract-First, Dual-Track Mode (用于 SDD 流水线 /plan)

当被 `/plan` command 调用时,本 skill 进入「契约先行 + 前后端双轨」模式,产出**三份**交付物而非单一 plan:

1. `tasks/api-contract.md` — 全量 API 契约表(唯一真相源)
2. `tasks/plan-backend.md` — 后端任务 plan
3. `tasks/plan-frontend.md` — 前端任务 plan

这三份交付物会被后续 `/build`(Leader-Worker 编排)、`/test`、`/review`(红队)、`/integrate`(联调)阶段共同引用。

### API 契约表 Schema

所有跨前后端的 HTTP 端点都必须先在契约表中登记,再切分双轨任务。Schema 如下:

```markdown
| API ID | Method | Endpoint | Provider 模块 | Consumer 模块 | Request DTO | Response DTO | Error Codes | 依赖 API ID | 实现顺序 |
|--------|--------|----------|---------------|---------------|-------------|--------------|-------------|-------------|----------|
| API-001 | POST | /api/v1/users | server/user-service | web/b2c-console | CreateUserReq{name,email,role} | UserVO{id,...} | 400, 409 | — | 1 |
```

字段必填规则:
- `API ID` 全局唯一,形如 `API-001`,后续 plan/build/review/integrate 通过此 ID 追溯
- `Provider/Consumer 模块` 必须给出具体工程目录
- `Request/Response DTO` 给字段清单,可引用 spec 中的 DTO 定义
- `Error Codes` 至少列业务错误码 + HTTP 状态码
- `依赖 API ID` 用于排序,`实现顺序` 是建议批次编号

### 双轨切分粒度

| Track | 单任务粒度 | 文件触及上限 |
|-------|-----------|-------------|
| Backend | 1 个 endpoint + service + DTO + 测试(垂直切片) | ≤5 个文件 |
| Frontend | 1 个 page/container + api client + hook + 组件 + 测试 | ≤5 个文件 |

### 双轨任务条目格式

```markdown
### Task B-001 / Task F-001

- **contract-id**: API-001(基础设施任务填 `infra`)
- **track**: backend / frontend
- **description**: 一句话
- **acceptance criteria**: [ ] ... [ ] ...
- **verification**: 测试命令 + 构建命令
- **dependencies**: 上游任务编号或 `无`
- **files likely touched**: 路径列表
- **scope**: S / M(L 必须拆分)
```

### 依赖与并行规则

- **同 contract-id 的前后端任务可并行**:契约一旦冻结即可并行开发,Worker 各自走 TDD
- **跨 contract-id 任务**:遵循契约表中的「依赖 API ID」与「实现顺序」字段
- **契约变更**:必须先回到 `/plan` 阶段更新契约表并重新审核,不允许在 build 中私自调整

### 与后续阶段的契约

- `/build`:Leader 按 contract-id 与依赖顺序派发 Worker,Worker 上下文中只携带与当前任务相关的契约表片段(不传整表)
- `/test`:测试用例必须覆盖契约表中列出的所有 Error Codes
- `/review`(红队):强制比对实现与契约表,字段/状态码/错误码任一不一致即 REJECT
- `/integrate`:以契约表作为 mock-to-real 切换清单基准,逐 API ID 验证

## Plan Document Template

```markdown
# Implementation Plan: [Feature/Project Name]

## Overview
[One paragraph summary of what we're building]

## Architecture Decisions
- [Key decision 1 and rationale]
- [Key decision 2 and rationale]

## Task List

### Phase 1: Foundation
- [ ] Task 1: ...
- [ ] Task 2: ...

### Checkpoint: Foundation
- [ ] Tests pass, builds clean

### Phase 2: Core Features
- [ ] Task 3: ...
- [ ] Task 4: ...

### Checkpoint: Core Features
- [ ] End-to-end flow works

### Phase 3: Polish
- [ ] Task 5: ...
- [ ] Task 6: ...

### Checkpoint: Complete
- [ ] All acceptance criteria met
- [ ] Ready for review

## Risks and Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| [Risk] | [High/Med/Low] | [Strategy] |

## Open Questions
- [Question needing human input]
```

## Parallelization Opportunities

When multiple agents or sessions are available:

- **Safe to parallelize:** Independent feature slices, tests for already-implemented features, documentation
- **Must be sequential:** Database migrations, shared state changes, dependency chains
- **Needs coordination:** Features that share an API contract (define the contract first, then parallelize)

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "I'll figure it out as I go" | That's how you end up with a tangled mess and rework. 10 minutes of planning saves hours. |
| "The tasks are obvious" | Write them down anyway. Explicit tasks surface hidden dependencies and forgotten edge cases. |
| "Planning is overhead" | Planning is the task. Implementation without a plan is just typing. |
| "I can hold it all in my head" | Context windows are finite. Written plans survive session boundaries and compaction. |

## Red Flags

- Starting implementation without a written task list
- Tasks that say "implement the feature" without acceptance criteria
- No verification steps in the plan
- All tasks are XL-sized
- No checkpoints between tasks
- Dependency order isn't considered

## Verification

Before starting implementation, confirm:

- [ ] Every task has acceptance criteria
- [ ] Every task has a verification step
- [ ] Task dependencies are identified and ordered correctly
- [ ] No task touches more than ~5 files
- [ ] Checkpoints exist between major phases
- [ ] The human has reviewed and approved the plan
