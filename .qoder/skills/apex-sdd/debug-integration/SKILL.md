---
name: debug-integration
description: Multi-module integration debugging workflow. Start the primary module and its dependency modules, assign an expert to each module, switch Mock calls to real calls, and ensure all contract tests of the primary module pass.
---

# Debug Integration

## Overview

Cross-module integration debugging centered around a primary module A. Start module A and all its dependency modules' services, assign one expert to each module for maintenance and modification, replace Mock/Stub calls in module A with real calls, and verify integration correctness through contract tests.

## When to Use

- Multi-module backend integration, requiring a switch from Mock to real calls
- Cross-module interface debugging requiring multiple modules to be online simultaneously
- Module A's contract tests require real availability of its dependencies
- Need to verify inter-module interface consistency before integration testing

## Workflow

### Phase 0: Create Git Checkpoint

Before starting any operation, create a Git checkpoint to ensure all changes are revertable:

```bash
git add -A
git stash
```

Record the current stash hash for later recovery if needed.

### Phase 1: Identify Scope

1. Identify the primary module A
2. Analyze module A's dependencies, listing all dependency modules that need to be started
3. Determine for each module:
   - Code directory path
   - Startup command
   - Port number
   - Contract test execution command

### Phase 2: Service Startup

For each module (A + dependencies):

1. **Check if the service is already running**:
   - Check if the port is occupied
   - If occupied, determine whether it was started by this session
   - **If not started by this session**, start a new instance on a different port (or stop the original process and restart)
2. **Start the service**:
   - Execute the module's startup command
   - Wait for the service to be ready (health check)
   - Record the service address and port

### Phase 3: Parallel Expert Assignment

Use the Agent tool to launch sub-agents in parallel, assigning one expert per module. **All Agent calls must be issued in the same turn to ensure parallel execution.**

Each module expert's tasks:

```
Expert Responsibilities:
1. Read the current module's code and configuration
2. Ensure the module's service is running properly
3. If code changes are needed to support module A's real calls, make the modifications
4. Constraint: All changes must be forward-compatible (must not break existing interface contracts)
5. Output: Change list + current status report for the module
```

**Important Constraints (inherited from sub-agent model):**
- Sub-agents cannot spawn other sub-agents — do not let one expert delegate to another
- Each sub-agent has its own independent context window and only returns reports to the main session
- If experts need to communicate with each other, use the meet skill for multi-expert meetings

### Phase 4: Real Call Transformation

After all expert reports are returned:

1. Consolidate reports from all module experts
2. Modify module A's code:
   - Replace Mock/Stub calls with real calls to dependency modules
   - Ensure call addresses and ports point to the services started in Phase 2
   - Follow the incremental modification principle of `incremental-implementation`, changing one call at a time
3. Address compatibility issues found by experts (e.g., interface signature mismatches)

### Phase 5: Contract Test Verification

1. **Run all contract tests for module A**
2. **If tests fail:**
   - Analyze the failure cause
   - Identify the module and code that need changes
   - Fix following backward-compatibility principles
   - Re-run contract tests
   - **Loop until all contract tests pass**
3. **Important Principles:**
   - Prefer modifying module A's code to adapt to dependency modules, avoid modifying dependency module interfaces
   - If dependency module changes are unavoidable, ensure forward compatibility
   - Run the full contract test suite after every change

### Phase 6: Report

Output the final integration report:

```markdown
## Integration Results

### Startup Status
- Module A: [port] [status]
- Dependency Module B: [port] [status]
- Dependency Module C: [port] [status]
- ...

### Change List
- [module/file] → [change description] (Compatibility: forward-compatible/incompatible)

### Contract Test Results
- Total test cases: X
- Passed: X
- Failed: X

### Open Issues
- [issue description]
```

## See Also

- For systematic debugging workflow, see `debugging-and-error-recovery`
- For incremental code changes, see `incremental-implementation`
- For test-driven development cycle, see `test-driven-development`

## Common Rationalizations

| Wrong Belief | Reality |
|-------------|---------|
| "Just restart the service on the same port" | If the service was started externally, restarting it would interrupt other users. A new instance must be started on a different port |
| "It's fine to change the dependency module's interface" | Dependency modules are consumed by multiple callers; changing the interface would break other consumers. Must maintain forward compatibility |
| "Replace all Mock calls at once" | Changing multiple calls simultaneously makes it impossible to isolate the failure source. Change one call at a time |
| "Change the code first, test everything later" | Problems accumulate. Change one call, test once — that's the only way to pinpoint issues |

## Red Flags

- Skipping the Git checkpoint and modifying code directly
- Multiple modules starting on the same port, causing conflicts
- Sub-agents depending on and waiting for each other
- Modifying a dependency module's non-compatible interface
- Skipping per-change verification and accumulating changes before testing
- Starting to modify code before services are ready
