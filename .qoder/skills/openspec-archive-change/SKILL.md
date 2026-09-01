---
name: openspec-archive-change
description: Archive a completed change in the experimental workflow. Use when the user wants to finalize and archive a change after implementation is complete.
license: MIT
compatibility: No external dependencies
metadata:
  author: openspec
  version: "1.0"
  generatedBy: "1.3.1"
---

Archive a completed change in the experimental workflow.

**Input**: Optionally specify a change name. If omitted, check if it can be inferred from conversation context. If vague or ambiguous you MUST prompt for available changes.

**Steps**

1. **If no change name provided, prompt for selection**

   List `docs/changes/` directories (excluding `archive/`) that contain `tasks.md`. Use the **AskUserQuestion tool** to let the user select.

   Show only active changes (not already archived).

   **IMPORTANT**: Do NOT guess or auto-select a change. Always let the user choose.

2. **Check task completion status (the only criterion)**

   Read `tasks.md` to check for incomplete tasks.

   Count tasks marked with `- [ ]` (incomplete) vs `- [x]` (complete).

   **If incomplete tasks found:**
   - Display warning showing count of incomplete tasks
   - Use **AskUserQuestion tool** to confirm user wants to proceed
   - Proceed if user confirms

   **If no tasks file exists:** Proceed without task-related warning.

3. **Assess delta spec sync state**

   Check for delta specs at `docs/changes/<name>/specs/`. If none exist, proceed without sync prompt.

   **If delta specs exist:**
   - Compare each delta spec with its corresponding main spec at `docs/specs/<capability>/spec.md`
   - Determine what changes would be applied (adds, modifications, removals, renames)
   - Show a combined summary before prompting

   **Prompt options:**
   - If changes needed: "Sync now (recommended)", "Archive without syncing"
   - If already synced: "Archive now", "Sync anyway", "Cancel"

   If user chooses sync, use Task tool (subagent_type: "general-purpose", prompt: "Use Skill tool to invoke openspec-sync-specs for change '<name>'. Delta spec analysis: <include the analyzed delta spec summary>"). Proceed to archive regardless of choice.

4. **Perform the archive**

   Create the archive directory if it doesn't exist:
   ```bash
   mkdir -p docs/changes/archive
   ```

   Generate target name using current date: `YYYY-MM-DD-<change-name>`

   **Check if target already exists:**
   - If yes: Fail with error, suggest renaming existing archive or using different date
   - If no: Move the change directory to archive

   **Clean up change directory (only keep spec and task):**
   After moving to archive, remove all files in the change directory except `specs/` directory and `tasks.md`:
   ```bash
   # Remove everything except specs/ and tasks.md
   find docs/changes/archive/YYYY-MM-DD-<name> -maxdepth 1 -type f ! -name 'tasks.md' -delete
   find docs/changes/archive/YYYY-MM-DD-<name> -maxdepth 1 -type d ! -name 'specs' ! -name '.' -exec rm -rf {} +
   ```

   This ensures the archived change directory only contains:
   - `specs/` - delta spec files
   - `tasks.md` - task tracking file

5. **Display summary**

   Show archive completion summary including:
   - Change name
   - Archive location
   - Whether specs were synced (if applicable)
   - Note about any warnings (incomplete tasks)

**Output On Success**

```
## Archive Complete

**Change:** <change-name>
**Archived to:** docs/changes/archive/YYYY-MM-DD-<name>/
**Contents:** specs/ · tasks.md
**Specs:** ✓ Synced to main specs (or "No delta specs" or "Sync skipped")

All tasks complete.
```

**Guardrails**
- Always prompt for change selection if not provided
- Don't block archive on warnings - just inform and confirm
- After archiving, the change directory will only contain `specs/` and `tasks.md` (other files like `.status.json`, `design.md`, `progress.txt`, `proposal.md`, `implementation-notes.md` etc. are removed)
- Show clear summary of what happened
- If delta specs exist, always run the sync assessment and show the combined summary before prompting
