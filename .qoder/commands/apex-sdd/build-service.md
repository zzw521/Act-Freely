---
name: build-service
description: Iteratively dispatch auto-coding sub-agents based on OpenSpec tasks.md, using progress mechanism to solve long-task context loss
---

# /build

> ⚠️ **Role clarification**: This flow is executed directly by the **main agent** (the current agent running the `/build` command).
>
> The main agent acts as the **orchestrator** — responsible for iterative task dispatch, quality gates, and progress tracking.
> The `auto-coding` subagent acts as the **executor** — it completes one batch of tasks per iteration and exits.
>
> Do NOT delegate orchestration logic to the auto-coding subagent.
> If auto-coding exits early due to context window limits, that is normal. The main agent will dispatch the next iteration.

Iteratively dispatch `auto-coding` sub-agents to complete tasks from `tasks.md` one by one. Each iteration runs in a fresh context window, passing state via `tasks.md` checkboxes + `progress.txt`.

```
/build <change-name>
```

---

## Execution Flow

If no `<change-name>` is specified, list available changes or guide the user to the `/plan` command.

### 1. Gather Context

1. Verify `docs/changes/<change-name>/` exists
2. Read `docs/changes/<change-name>/tasks.md` and count progress (done/total)
3. Read progress.txt; create if not exists.

### 2. Confirm Start

Auto-detect all relevant context and present it to the user for confirmation **only before the first iteration**:
- Spec information: includes both `/spec` and `/plan` level specification artifacts
- Change information: the change directory

**Subsequent iterations proceed automatically without user confirmation.**
The loop continues until all tasks are done, max iterations exceeded, or a blocking issue is encountered.

### 3. Coding Loop

```
MAX_ITERATIONS = 10
STALL_THRESHOLD = 2
```

**Each iteration:**

1. **Exit condition**: Read `tasks.md` — all `[x]` / max iterations exceeded / consecutive stall → exit loop

2. **Dispatch auto-coding**: Use the Agent tool with `subagent_type: auto-coding`
   
   **⚠️ Main agent must NOT write code** — At this step, you MUST use the Agent tool to launch the auto-coding subagent.
   Do not implement any files yourself.
   
   **Prompt Design Key Points**:
   - Tell the subagent the current change name, completed tasks, and detailed description of the next task
   - Reference the spec file paths that need to be read
   - Provide the list of already created files (to prevent the subagent from recreating them)
   - Specify the required verification level (at minimum, Build must pass)
   
   **Expected behavior**: auto-coding subagent reads tasks.md independently, finds `[ ]` tasks → reads corresponding spec → implements code → verifies → updates tasks.md checkboxes → updates progress.txt → commits → exits

3. **Phase Gate Check** (Audit — not re-execute): After sub-agent completes, verify that auto-coding did NOT skip its own progressive verification:

   - Read `progress.txt` — check which verification levels the agent ran and their results
   - **Mandatory check**: Agent MUST have run at least Level 1 (Build). If not, reject.
   - **Conditional check**: If Level 2/3/4/5 prerequisites were met but the agent skipped them, reject.
   - **Spot-check** (optional, for high confidence): pick one level the agent claimed passed and re-run it to confirm
   - **Report**: Log the verification level the agent achieved (1-5) and audit result (PASS/FAIL)
   - If audit fails, **reject iteration** (see Gate failure handling below)

   The intent is NOT to duplicate auto-coding's work — it is to enforce that auto-coding did its work.

4. **Code review**: After Gate passes, invoke `code-review-and-quality` skill to review this iteration's git changes

5. **Update infrastructure readiness**: Track whether the project can now start in mock mode, update `progress.txt` with current startup_ready status

6. **Iteration summary**: iteration number / done count / total count / gate level / commit hash

### 3a. Gate Failure Handling

When a Phase Gate Check fails:

1. **Log the failure**: Record which verification level(s) failed and the error to `progress.txt`
2. **Increment gate failure counter**: If this is the N-th consecutive gate failure for the same iteration (after re-dispatch), escalate
3. **Re-dispatch**: Re-run the auto-coding agent with explicit instructions to fix the gate failure, including the error details in the prompt
4. **Escalation rule**: If the same iteration fails Gate Check >= 2 consecutive times, mark the iteration's task(s) as blocked in `tasks.md` with `[B]` prefix, print the failure details, and prompt the user for intervention rather than looping indefinitely

This prevents the common failure mode where an agent repeatedly produces code that compiles but cannot start, wasting iterations.

### 3b. Main Agent Red Lines

**Forbidden**:
- ❌ Calling Write/SearchReplace yourself to write business code
- ❌ Manually creating or modifying source files (Java/Go/Python/TypeScript etc.) in the project
- ❌ Directly running build commands like mvn/gradle/go build (this is the subagent's responsibility)

**Allowed**:
- ✅ Reading design documents, tasks.md, progress.txt to track progress
- ✅ Creating/updating orchestration-level files (tasks.md `[x]`/`[B]` markers, progress.txt)
- ✅ Launching subagents (Agent tool)
- ✅ Performing Phase Gate Check on code completed by subagent
- ✅ Invoking code-review skill for code review

### 4. Completion Handling

- **ALL_DONE** -> invoke `openspec-verify-change` skill for overall verification
- **Incomplete** → print remaining task count, prompt user to continue with `/build <change-name>`