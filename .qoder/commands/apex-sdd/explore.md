---
description: Explore and refine business ideas through multi-expert iterative collaboration, producing structured design deliverables (MRD / PRD / Frontend Scaffold / Architecture Design)
---

Invoke the agent-skills: `idea-refine`, `grill-me`, `frontend-project-scaffolder` (Full-Stack only)

**Input**: The argument after `/apex-sdd:explore` is the reference file name OR a description of business idea, along with the user's specific task instructions.

---

## Core Loop

Step 1. If no input provided, ask what business idea or task the user wants to explore

Step 2. Read the provided input and understand the user's intent, referenced documents, and objectives

Step 3. Use Expert Plugins Mode — invoke the `idea-refine` skill, engaging multiple experts in discussion and refinement. Produce a draft deliverable and document the points of contention emerging from the multi-round deliberations among the experts.

Step 4. Use the `grill-me` skill to clarify contentious points with the user. Ask the user each question one by one. For every question, propose your recommended solution. Proceed to the next question only after the user has responded to the current one.

Step 5. Update the deliverable based on the user's input.

Step 6. Return to Step 3 — run the cycle (Step 3 → Step 4 → Step 5 → Step 6) **five times** to reach final results.

Step 7. Document consistency check — if changes impact other existing documents, identify and apply cross-document updates. Correct any conflicting points and confirm with the user when necessary.

Step 8. Structure findings into the required deliverable format using the appropriate template.

Step 9. **[Full-Stack only]** If the deliverable is a PRD for a Full-Stack application and no frontend scaffold exists yet, invoke the `prototype-scaffolding` skill to generate the interactive prototype project under `docs/prd/prd-{业务}-交互原型/`.

---

## Templates (Recommended, Not Mandatory)

The following are default templates. You may also provide your own custom template — simply reference the file path in your prompt and the experts will use it instead.

| Template | Default Path |
|----------|-------------|
| MRD | `docs/template/MRD-template.md` |
| PRD | `docs/template/PRD-template.md` |
| ARCH | `docs/template/ARCH-template.md` |

> **Custom template usage:** Include `模板: path/to/your-template.md` or `Template: path/to/your-template.md` in your prompt to override the default.

---

## Document Responsibility Boundaries (Mandatory Rules)

Each deliverable type has a strict scope. Content MUST NOT bleed across boundaries:

| Deliverable | Covers (IN Scope) | Explicitly Excludes (OUT of Scope) |
|---|---|---|
| **MRD** | Market opportunity, target users, value proposition, product portfolio (name + type + target user), phased direction, direction-level scale targets, business risks | Module breakdown, architecture layers/boundaries/dependencies, technical metrics (P99/QPS/SLA), system design, data models, API contracts |
| **PRD** | Feature lists, module breakdown, user stories, interaction flows, acceptance criteria, business rules | Architecture design, technology choices, deployment topology, detailed interface fields, performance SLA quantification |
| **ARCH** | System architecture, layer decomposition, module boundaries/dependencies, technology stack, performance SLAs (P99/QPS), capacity planning, security architecture, deployment topology | Market analysis, business model, user research, feature priority ranking |

> **Enforcement**: When producing any deliverable, check each section against this matrix. If content belongs to a downstream document, replace it with a forward reference (e.g., "See ARCH for detailed performance SLAs").

---

## Quality Gate

1. All decision branches have reached consensus with the user
2. All template necessary sections filled — no fuzzy language (no "fast" / "many" / "good"), application type declared, business goals quantified, cross-system dependencies explicit
3. Cross-document consistency verified — no conflicting points remain across related documents

> For deliverable-specific quality gates (MRD / PRD / ARCH), see `references/explore-best-practices.md`.

---

## Usage Guide

See `references/explore-best-practices.md` for detailed prompt patterns, expert panel configuration, deliverable specifications, quality gates, and scenario-specific examples.

---

## Next Step Guide

`/apex-sdd:explore` covers the full design pipeline internally. After completing each deliverable, proceed to the next:

```
MRD → PRD → Frontend Scaffold (Full-Stack only) → ARCH → exit /apex-sdd:explore
```

| Current Deliverable | Next Action |
|---|---|
| MRD complete | Run `/apex-sdd:explore` again to produce PRD (per-application) |
| PRD complete | Run `/apex-sdd:explore` again to generate Frontend Scaffold (Full-Stack apps only) |
| Frontend Scaffold complete | Run `/apex-sdd:explore` again to produce Architecture Design (ARCH + ADRs) |
| ARCH complete | **Exit `/apex-sdd:explore`**. Run `/apex-sdd:spec` to begin module-level detailed design. |
