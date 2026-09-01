---
name: prd-to-spec
description: "Transform a PRD into a technical SPEC document — architecture, API design, data model, error handling, and implementation contracts. Triggers on: prd-to-spec, prd to spec, prd转spec, 需求转设计, 需求转规格, generate spec from prd, design from prd, 技术方案, 设计方案."
user-invocable: true
---

# prd-to-spec — PRD to Technical Specification

Transform a Product Requirements Document (PRD) into a detailed technical SPEC that an engineer or AI agent can implement against. The PRD says *what* to build; the SPEC says *how* to build it.

---

## When to Use

- A `/explore` has been generated and you need to bridge the gap to implementation
- You want architecture decisions documented before coding starts
- Multiple developers/agents will implement the feature and need a shared contract
- You need to validate technical feasibility before committing to a PRD
- You want to catch design issues early — before code is written

---

## The Job

1. **Locate PRD** — find or receive the PRD document
2. **Analyze context (optional)** — if a codebase exists, scan it to understand current architecture, patterns, and constraints
3. **Ask clarifying questions** — resolve technical ambiguities (max 3-5 questions)
4. **Generate SPEC** — produce a structured technical specification
5. **Review** — present to user for feedback and iteration
6. **Save** — write final SPEC to agreed location

---

## Step 1: Locate PRD

Find the input PRD in one of these ways:

```
Provide the PRD to convert:

A. File path (e.g., tasks/prd-priority-system.md)
B. GitHub Issue URL
C. Paste PRD content directly
D. Auto-detect: scan tasks/ directory for recent PRDs
```

If auto-detecting, list available PRDs and let the user choose:

```
Found PRDs in tasks/:
  1. tasks/prd-priority-system.md (2024-03-15)
  2. tasks/prd-user-auth.md (2024-03-10)

Which PRD should I convert? [1/2]
```

---

## Step 2: Analyze Context (Optional)

**Skip this step if no codebase exists yet** (greenfield project). In that case, the SPEC will propose architecture from scratch based on the PRD requirements and clarifying questions.

If a codebase exists, scan it to understand:

- **Existing architecture** — how the current system is structured
- **Tech stack** — languages, frameworks, libraries already in use
- **Patterns** — naming conventions, file organization, error handling approach
- **Database** — current schema, migration tool, ORM
- **API style** — REST/GraphQL/gRPC, authentication method, response format
- **Testing** — test framework, coverage patterns, test utilities

This ensures the SPEC aligns with the existing system rather than proposing incompatible solutions.

---

## Step 3: Clarifying Questions

Ask only when the PRD leaves technical decisions ambiguous. Focus on:

- **Architecture choices** — where does this feature live? New service or extend existing?
- **Data storage** — new table? Extend existing? Cache strategy?
- **API design** — new endpoints? Extend existing? Breaking changes?
- **Dependencies** — any new libraries needed? Version constraints?
- **Performance** — expected load? Latency requirements? Batch size limits?

Format:
```
Technical questions before I generate the SPEC:

1. Where should the priority logic live?
   A. Extend existing TaskService
   B. New PriorityService
   C. Inline in controller
   D. Let me decide based on the codebase

2. Database migration approach?
   A. Add column to existing tasks table
   B. New priority table with FK
   C. JSON field on tasks
   D. Let me decide based on current schema

3. API versioning concern?
   A. Add to existing v1 endpoints
   B. New v2 endpoints
   C. No versioning needed
```

If user selects "let me decide" options, make the best choice based on codebase analysis and document the rationale in the SPEC.

---

## Step 4: Review & Iteration

After generating the SPEC, present it and ask:

```
SPEC generated from PRD. Please review:

- Are the architecture choices appropriate?
- Are there missing edge cases or error scenarios?
- Is the API design consistent with existing patterns?
- Should any section have more/less detail?

Reply OK to save, or provide feedback for iteration.
```

---

## Step 5: Save

Ask user for save location:

```
Where should I save the SPEC?

A. tasks/spec-[feature-name].md (alongside PRD, recommended)
B. docs/spec-[feature-name].md
C. Custom path: [specify]
```

---

## Quality Criteria

A good SPEC should pass these checks:

- [ ] Every PRD User Story has corresponding SPEC sections
- [ ] Every Functional Requirement maps to an API endpoint or business logic rule
- [ ] Every Acceptance Criterion maps to at least one test case
- [ ] Architecture choices are justified with rationale
- [ ] API schemas are specific enough to generate client code
- [ ] Error handling covers all identified failure modes
- [ ] Implementation order respects dependencies
- [ ] No "TBD" or "TODO" items — resolve or move to Open Questions

---

## Edge Cases & Fallback

| Scenario | Handling |
|----------|----------|
| PRD is vague or incomplete | Generate SPEC with best-effort choices, mark assumptions in Section 11.3 |
| PRD conflicts with existing code | Flag conflicts explicitly, propose resolution in Section 11.1 |
| Feature is too large for one SPEC | Split into multiple SPECs (one per service boundary), link them |
| No existing codebase (greenfield) | Skip Step 2, propose architecture from scratch based on PRD + clarifying questions |
| PRD has no User Stories (just bullet points) | Infer structure, map bullets to SPEC sections, note in Summary |
| User wants SPEC without reading codebase | Skip Step 2, note that assumptions about existing code are unverified |
| Multiple PRDs need one SPEC | Merge PRD inputs, deduplicate requirements, note source for each |

---

## Anti-Patterns to Avoid

- **Don't restate the PRD.** The SPEC adds technical depth, not a copy of requirements in different words.
- **Don't over-specify trivial operations.** CRUD with no special logic doesn't need a full algorithm section.
- **Don't pick technologies without context.** Always check what the project already uses before suggesting new tools.
- **Don't design in isolation.** The SPEC must fit the existing system — same patterns, same conventions, same style.
- **Don't leave decisions implicit.** If you made a choice (e.g., "add column to existing table"), state it and say why.
- **Don't write implementation code.** The SPEC describes contracts and behavior, not code. Pseudocode is acceptable for complex algorithms.

---

## Relationship to Other Skills

```
/prd  →  /prd-to-spec  →  /goal  →  /review-it  →  /ship-it
 │              │              │
 │  Requirements │  Technical   │  Implementation
 │  (what)       │  (how)       │  (code)
```

- **/prd** produces the PRD (input to this skill)
- **/prd-to-spec** produces the SPEC (this skill)
- **/goal** implements Issues with SPEC as the technical reference
- **/code-to-spec** reverse-engineers SPEC from existing code (complementary — forward vs. reverse)
