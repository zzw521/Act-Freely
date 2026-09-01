---
description: Archive a completed change — verify implementation completeness, generate archive artifacts, and commit to closure.
---

Invoke Skill `openspec-archive-change`

Before archiving, ensure the following checks pass:

1. **Verify change completeness** — Invoke Skill `openspec-verify-change` to confirm implementation matches all change artifacts (tasks, specs, API contracts). Fix any discrepancies found.

2. **Finalize artifacts**
   - Update `progress.txt` or equivalent tracking file to mark tasks as done
   - Update `implementation-notes.md` with final notes if needed
   - Ensure all specs and docs reflect the final state

3. **Verify no regressions** — Run the full test suite (unit tests + contract tests + e2e tests) to ensure nothing is broken.

4. **Clean up environment** — Stop any running background services started during implementation.

5. **Commit archive state**

   ```bash
   git add -A && git commit -m "archive: <description>"
   ```

6. **Report** — Provide a summary of what was archived, including:
   - Changes delivered
   - Test results
   - Any known limitations or follow-up items

> **Note**: This command is the final step in the experimental workflow. After archiving, the change is considered complete and documented.
