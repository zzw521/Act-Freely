---
description: Multi-module integration debugging — start dependency modules, assign experts, switch Mock to real calls, verify all contract tests pass
---

Invoke Skill  `debug-integration`

Perform integration debugging with module A as the primary module. For each step, refer to the detailed rules and constraints in the skill:

1. **Create Git checkpoint** — `git stash` to ensure revertability
2. **Identify module scope** — determine module A and its dependencies' directories, startup commands, and ports
3. **Start all services** — check port occupancy; start new instances on different ports for services not started by this session
4. **Assign experts in parallel** — launch a sub-agent per module; experts maintain their respective modules and make forward-compatible changes
5. **Switch to real calls** — replace Mock calls in module A with real calls, one at a time
6. **Run contract tests** — iterate until all pass; fix → retest loop
7. **Output integration report** — startup status, change list, test results

