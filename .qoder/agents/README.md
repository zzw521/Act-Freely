# Agent Personas

Specialist personas that play a single role with a single perspective. Each persona is a Markdown file used as an agent definition by the Qoder harness.

| Persona                                   | Role                  | Best for                                                                                                                  |
| ----------------------------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| [auto-coding](auto-coding.md)             | Autonomous Coder      | Complete coding sessions based on OpenSpec design artifacts — self-select tasks, implement, verify, commit                 |
| [detailed-designer](detailed-designer.md) | Software Architect    | Detailed design from spec — architecture, data model, API contracts                                                       |
| [code-reviewer](code-reviewer.md)         | Senior Staff Engineer | Five-axis review before merge                                                                                             |
| [security-auditor](security-auditor.md)   | Security Engineer     | Vulnerability detection, OWASP-style audit                                                                                |
| [test-engineer](test-engineer.md)         | QA Engineer           | Test strategy, coverage analysis, Prove-It pattern                                                                        |
| [ui-to-code](ui-to-code.md)               | UI-to-Code Engineer   | Convert design mockups/screenshots into pixel-perfect frontend code via VL analysis + browser-verified iterative pipeline |

## How personas relate to skills and commands

Three layers, each with a distinct job:

| Layer       | What it is                                     | Example                       | Composition role                                     |
| ----------- | ---------------------------------------------- | ----------------------------- | ---------------------------------------------------- |
| **Skill**   | A workflow with steps and exit criteria        | `code-review-and-quality`     | The _how_ — invoked from inside a persona or command |
| **Persona** | A role with a perspective and an output format | `code-reviewer`               | The _who_ — adopts a viewpoint, produces a report    |
| **Command** | A user-facing entry point                      | `/apex-sdd:review`, `/ship`   | The _when_ — composes personas and skills            |

The user (or a slash command) is the orchestrator. **Personas do not call other personas.** Skills are mandatory hops inside a persona's workflow.

## When to use each

### Direct persona invocation (Agent tool)

Pick this when you want one perspective on the current change and the user is in the loop.

- "Design the system architecture for this PRD" → invoke `detailed-designer` directly
- "Review this PR" → invoke `code-reviewer` directly
- "Are there security issues in this code?" → invoke `security-auditor` directly
- "What tests are missing for the checkout flow?" → invoke `test-engineer` directly
- "Convert this design mockup to code" → invoke `ui-to-code` directly
- "Implement a coding session from artifacts" → invoke `auto-coding` directly

### Slash command (single persona/flow behind it)

Commands are under the `apex-sdd` namespace. Pick this when there's a repeatable workflow you'd otherwise re-explain every time.

| Command | Invokes | Description |
|---------|---------|-------------|
| `/apex-sdd:explore` | `idea-refine` skill | Explore business ideas through multi-expert refinement; produce MRD → PRD → Frontend Scaffold → Architecture Design |
| `/apex-sdd:spec` | `detailed-designer` (fan-out) + `prd-to-spec` skill | Transform PRD into technical SPEC documents — API design, data model, error handling |
| `/apex-sdd:ui-design` | `frontend-ui-designer` skill | Generate UI design specification from interaction prototype, with optional Figma integration |
| `/apex-sdd:plan` | `planning-and-task-breakdown` skill | Decompose spec into capability specs and an ordered task checklist |
| `/apex-sdd:build` | `auto-coding` (fan-out) | Implement tasks from an OpenSpec change, with visual fidelity verification for frontend |
| `/apex-sdd:bugfix` | `debugging-and-error-recovery` + `test-driven-development` skills | Systematic bugfix workflow — diagnose, fix, verify, review, commit |
| `/apex-sdd:code-simplify` | `code-simplification` skill | Simplify code for clarity without changing behavior |
| `/apex-sdd:test` | `test-driven-development` skill | Write failing tests, implement, verify (TDD or Prove-It pattern) |
| `/apex-sdd:review` | `code-review-and-quality` skill | Five-axis code review before merge |
| `/apex-sdd:integrate` | `debug-integration` skill | Multi-module integration debugging — switch Mock to real calls, verify contract tests |
| `/apex-sdd:ship` | `code-reviewer` + `security-auditor` + `test-engineer` (parallel fan-out) + `shipping-and-launch` skill | Pre-launch checklist via parallel specialist personas, then synthesize a go/no-go decision |

### Fan-out orchestration (`/apex-sdd:ship`)

Pick this only when **independent** investigations can run in parallel and produce reports that a single agent then merges.

- `/apex-sdd:ship` → fans out to `code-reviewer` + `security-auditor` + `test-engineer` in parallel, then synthesizes their reports into a go/no-go decision

The `build` command also fans out to `auto-coding` subagents for multi-module parallel implementation.

## Decision matrix

```
Is the work a single perspective on a single artifact?
├── Yes → Direct persona invocation
└── No  → Are the sub-tasks independent (no shared mutable state, no ordering)?
         ├── Yes → Slash command with parallel fan-out (e.g. /apex-sdd:ship)
         └── No  → Sequential slash commands run by the user
                    (/apex-sdd:explore → /apex-sdd:spec → /apex-sdd:ui-design (optional)
                     → /apex-sdd:plan → /apex-sdd:build → /apex-sdd:test
                     → /apex-sdd:review → /apex-sdd:integrate → /apex-sdd:ship)
```

## Worked example: valid orchestration

`/apex-sdd:ship` is the canonical fan-out orchestrator in this repo:

```
/apex-sdd:ship
  ├── (parallel) code-reviewer    → review report
  ├── (parallel) security-auditor → audit report
  └── (parallel) test-engineer    → coverage report
                  ↓
        merge phase (main agent)
                  ↓
        go/no-go decision + rollback plan
```

Why this works:

- Each sub-agent operates on the same diff but produces a **different perspective**
- They have no dependencies on each other → genuine parallelism, real wall-clock savings
- Each runs in a fresh context window → main session stays uncluttered
- The merge step is small and benefits from full context, so it stays in the main agent

## Worked example: invalid orchestration (do not build this)

A `meta-orchestrator` persona whose job is "decide which other persona to call":

```
/work-on-pr → meta-orchestrator
                  ↓ (decides "this needs a review")
              code-reviewer
                  ↓ (returns)
              meta-orchestrator (paraphrases result)
                  ↓
              user
```

Why this fails:

- Pure routing layer with no domain value
- Adds two paraphrasing hops → information loss + 2× token cost
- The user already knows they want a review; let them call `/apex-sdd:review` directly
- Replicates work that slash commands and `AGENTS.md` intent-mapping already do

## Rules for personas

1. A persona is a single role with a single output format. If you find yourself adding a second role, create a second persona.
2. **Personas do not invoke other personas.** Composition is the job of slash commands or the user.
3. A persona may invoke skills (the _how_).
4. Every persona file ends with a "Composition" block stating where it fits.

## Adding a new persona

1. Create `agents/<role>.md` with the same frontmatter format used by existing personas.
2. Define the role, scope, output format, and rules.
3. Add a **Composition** block at the bottom (Invoke directly when / Invoke via / Do not invoke from another persona).
4. Add the persona to the table at the top of this file.
