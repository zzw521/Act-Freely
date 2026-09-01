---
description: Systematic bugfix workflow — diagnose, verify, review, commit, and report.
---

1. **Diagnose** — Analyze the codebase based on user input and related documentation. Invoke Skill `debugging-and-error-recovery` to locate the root cause. If information is insufficient, ask the user for clarification.

2. **Fix** — Invoke Skill `test-driven-development` to implement the fix.

3. **Verify**
   - **Backend**: compilation passes + unit tests + contract tests + e2e tests
   - **Frontend**: browser automation + no console errors
   - `init.sh` MUST run without errors
   - **Check** whether current changes deviate from the technology selections

   You MUST NOT skip verification.

4. **Review** — Invoke Skill `code-review-and-quality` to review the code. If any critical issues are found, they must be fixed.

5. **Cleanup**   Clean up the environment, e.g., stop any running services.

6. **Commit**

   ```bash
   git add -A && git commit -m "fix: <description>"
   ```

7. **Report** — Provide a summary of the change impact.