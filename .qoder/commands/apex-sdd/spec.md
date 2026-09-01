---
description: Start spec-driven development — write a structured specification before writing code
---

First, identify the scenario. If it is a **feature** scenario, follow steps 1–6 below.
If it is a **bugfix** scenario, first roughly analyze the scope of the change, then ask the user whether to directly follow the `/apex-sdd:bugfix` flow to fix the bug, or proceed with steps 1–6.

1. **Locate the spec template** — auto-detect from the project. If none exists, ask the user to specify one.
2. **Confirm the spec template** with the user — the template defines the focus areas of the spec.
3. **Discuss and confirm technology-related content** (tech stack and dev environment) with the user.
4. **Determine whether a change directory is needed** — based on existing specs in the project and the scale of the described feature, decide if a change directory should be created:
    - **Feature-level (small scope)**: e.g., adding a seckill spec for a C-end shop, which is merely a single feature — create a change directory. Derive a kebab-case name from the description (e.g., "add user authentication" → `add-user-auth`):
      ```bash
      mkdir -p docs/changes/<name>
      ```
    - **Subsystem-level (large scope)**: e.g., generating a spec for an entire C-end shop subsystem — no change directory needed.
5. Prefer calling skill `apex-sdd:prd-to-spec` to generate the spec; If there are multiple modules, invoke the agent `apex-sdd:detailed-designer` and launch multiple subagents in parallel.

6. **Output deliverables**: spec (required), API schema YAML (on demand), database DDL SQL (on demand).

**Next step:** Run `/apex-sdd:plan` to break down the spec into implementation tasks.

> For full-stack applications, you may run the optional step `/apex-sdd:ui-design` before plan to generate UI specification docs for more precise frontend implementation.

> Suggested prompt (full-stack application, backend and frontend in parallel):
>
> ```
> Launch two sub-agents in parallel to execute the following two tasks, do NOT run them sequentially:
>
> Sub-agent 1 (Backend): Run the /apex-sdd:plan command, business: {business}, detailed design doc: docs/spec/spec-{business}-backend-module-design.md, decompose the backend module detailed design into ordered implementation tasks, output to openspec/changes/{business}-backend-plan/ directory
>
> Sub-agent 2 (Frontend): Run the /apex-sdd:plan command, business: {business}, detailed design doc: docs/spec/spec-{business}-frontend-module-design.md, decompose the frontend module detailed design into page-grouped implementation tasks, output to openspec/changes/{business}-frontend-plan/ directory
>
> Both sub-agents start simultaneously, complete independently, and finally report their respective outputs.
> ```
>
> Suggested prompt (backend-only application): `/apex-sdd:plan business: {business}, detailed design doc: docs/spec/spec-{business}-backend-module-design.md, decompose the backend module detailed design into ordered implementation tasks, output to openspec/changes/{business}-backend-plan/ directory`
