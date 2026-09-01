---
name: apex-sdd:plan
description: Decompose a spec into fine-grained capability specs and a task list.
---

## Artifacts

- `specs/<capability>/spec.md` — per-capability specification file
- `tasks.md` — implementation step checklist

> When ready to implement, run `/apex-sdd:build`.

---

## Workflow

### Step 1 — Locate Input

- Input MUST be a spec file under `docs/spec/`.
- Auto-detect candidate specs, then confirm with the user before proceeding.
- **IMPORTANT**: Do NOT proceed without understanding what the user wants to plan.
- If no candidate spec is provided, ask what they want to build using the **AskUserQuestion tool** (open-ended, no preset options), then identify the corresponding spec.

### Step 2 — Create Change Directory

Derive a kebab-case name from the description (e.g., "add user authentication" → `add-user-auth`):

```bash
mkdir -p docs/changes/<name>
```

### Step 3 — Generate Capability Specs

Create a spec file for each capability declared in the input spec.

#### Requirements

- Create `specs/<capability>/spec.md` for each **New Capabilities** entry.
- Create or update `specs/<capability>/spec.md` for each **Modified Capabilities** entry.
- Each spec file MUST include: overview, API definition, request/response DTOs, error codes, data model.

#### Delta Operations

Use `##` headers for each delta operation:

| Operation | Description |
|-----------|-------------|
| **ADDED Requirements** | New capability |
| **MODIFIED Requirements** | Changed behavior — MUST include the full updated content |
| **REMOVED Requirements** | Deprecated feature — MUST include Reason and Migration |
| **RENAMED Requirements** | Name change only — use `FROM:` / `TO:` format |

#### Formatting Rules

- Each requirement: `### Requirement: <name>` followed by a description.
- Use **SHALL** / **MUST** for normative requirements (avoid should / may).
- Each scenario: `#### Scenario: <name>` using WHEN / THEN format.
- **Critical**: Scenarios MUST use 4 hashtags (`####`). Using 3 or bullets will silently fail.
- Every requirement MUST include at least one scenario.

#### MODIFIED Requirements Workflow

1. Locate the existing requirement in `openspec/specs/<capability>/spec.md`.
2. Copy the entire requirement block (from `### Requirement:` through all scenarios).
3. Paste under `## MODIFIED Requirements` and edit to reflect the new behavior.
4. Ensure the header text matches exactly (whitespace-insensitive).

#### Common Pitfalls

- Using MODIFIED with only partial content → details lost during archive.
- If adding a new concern without changing existing behavior, use **ADDED** instead of MODIFIED.

#### spec.md Template

```markdown
## ADDED Requirements

### Requirement: <!-- requirement name -->
<!-- requirement text -->

#### Scenario: <!-- scenario name -->
- **WHEN** <!-- condition -->
- **THEN** <!-- expected outcome -->
```

### Step 4 — User Confirmation

Present Step 3 output to the user for confirmation. Proceed only after explicit approval.

### Step 5 — Task Breakdown

Decompose work into executable tasks based on the input spec and generated capability specs.

<!-- Invoke skill `planning-and-task-breakdown` to decompose tasks. -->

#### Interactive Prototype Copy (Frontend-Backend App Only)

**For Frontend Apps**: Before generating tasks.md, copy the interactive prototype project:

- Source: `docs/prd/prd-{business}-prototype/`
- Target: `web/{frontend project}/` (e.g., `web/b2c-shop/`)
- Verify: Check that `web/{frontend project}/package.json` and `web/{frontend project}/src/` exist
- This ensures the build phase has a working frontend project scaffold with routes, components, and styling already in place.

#### Requirements

- Group tasks by module; each group with a heading.
- Each task includes: file path, implementation notes, concerns (idempotency / rate-limiting / degradation).
- Use Markdown checkboxes `- [ ]` to track progress.
- Tasks MUST be granular enough to implement and verify independently.
- Order by dependency (what comes first must be listed first).
- The first section of `tasks.md` MUST list: referenced specs, schema YAML, DDL SQL, technology stack, and project structure.
- Project initialization and testing take priority over business implementation.

#### MANDATORY sections in tasks.md

`tasks.md` MUST include the following additional sections:

**① Parallel Execution Guide** (at the beginning of the file):
- List parallelizable task groups (operating on different directories/files with no cross-dependencies)
- Specify parallelism constraints (✅ parallel / ❌ serial)
- **Critical constraint**: Only tasks from **different applications** can be parallel; tasks within the **same application** MUST be serial (applications include backend and frontend — e.g., b2c-shop-be and b2c-shop are the same application)

**② Task Dependency Graph** (using Mermaid):
- Auto-generate a task dependency graph showing relationships between tasks

**③ Suggested Prompt - Parallel Execution Template** (at the end of the file):
- Generate concise parallel execution instructions for each parallelizable group
- Format: `Agent A - {module}: Implement Task {number}, responsible for {core file}`
- Each Prompt MUST include "Do not execute serially, start all simultaneously"
- MUST include conflict prevention notes (each Agent only operates on its own module's files)
- MUST specify for each Agent: working directory, design documents, output files, constraints
- **Critical constraint**: Only parallelize tasks from **different applications**; tasks within the **same application** (backend + frontend included) MUST run serially

#### Suggested Prompt Reference Templates

The following are commonly used parallel execution Suggested Prompt templates. Adjust as needed before placing at the end of tasks.md:

**Multiple Different Apps in Parallel:**

```
Start N sub-agents to execute the following applications in parallel (**do not execute serially, start all simultaneously**):

Sub-agent A - {App 1}: Execute /apex-sdd:build, business: {business-1}, reference {spec-files-for-app-1}, implement pending tasks in {tasks.md-path-for-app-1}

Sub-agent B - {App 2}: Execute /apex-sdd:build, business: {business-2}, reference {spec-files-for-app-2}, implement pending tasks in {tasks.md-path-for-app-2}

Constraint: {App 1} and {App 2} are different applications with no overlapping code — each Agent operates on its own module's files independently. Start simultaneously and report results separately.
```

**Backend-Only App:**

```
/apex-sdd:build business: {business}, reference docs/spec/spec-{business}-backend-module-design.md + spec-{business}-API-contract.yaml + spec-{business}-database-DDL.sql, implement pending tasks in openspec/changes/{business}-backend-plan/tasks.md
```

#### tasks.md Template

```markdown
## 0. Technology Stack & Project Structure
- Technology stack list
- Project structure

## 1. <!-- Task Group Name -->

- [ ] 1.1 <!-- Task description --> (file path)
- [ ] 1.2 <!-- Task description -->

## 2. <!-- Task Group Name -->

- [ ] 2.1 <!-- Task description -->
- [ ] 2.2 <!-- Task description -->
```
