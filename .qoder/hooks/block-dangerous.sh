#!/bin/bash
# Qoder Hook: Block dangerous shell commands
# Event: PreToolUse (matcher: Bash)
# Exit 0 = allow, Exit 2 = block (stderr fed back to Agent)

input=$(cat)
command=$(echo "$input" | jq -r '.tool_input.command // empty')

if [ -z "$command" ]; then
  exit 0
fi

# Block rm -rf / rm -r with dangerous targets
if echo "$command" | grep -qE 'rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+|.*--no-preserve-root)'; then
  echo "BLOCKED: Dangerous rm command detected: $command" >&2
  exit 2
fi

# Block destructive database operations
if echo "$command" | grep -qiE 'DROP\s+(TABLE|DATABASE|INDEX|SCHEMA)|TRUNCATE\s+TABLE'; then
  echo "BLOCKED: Destructive database command detected: $command" >&2
  exit 2
fi

# Block format/disk operations
if echo "$command" | grep -qE 'mkfs\.|dd\s+if=|fdisk|parted'; then
  echo "BLOCKED: Disk operation command detected: $command" >&2
  exit 2
fi

# Block chmod 777 (overly permissive)
if echo "$command" | grep -qE 'chmod\s+777'; then
  echo "BLOCKED: Overly permissive chmod 777 detected: $command" >&2
  exit 2
fi

# Block dangerous git commands
# git push --force (any branch)
if echo "$command" | grep -qE 'git\s+push\s+.*(-f|--force)'; then
  echo "BLOCKED: it lacks authority to access these commands: $command" >&2
  exit 2
fi

# git reset --hard
if echo "$command" | grep -qE 'git\s+reset\s+--hard'; then
  echo "BLOCKED: it lacks authority to access these commands: $command" >&2
  exit 2
fi

# git clean -f / -fd
if echo "$command" | grep -qE 'git\s+clean\s+.*-[a-zA-Z]*f'; then
  echo "BLOCKED: it lacks authority to access these commands: $command" >&2
  exit 2
fi

# git branch -D
if echo "$command" | grep -qE 'git\s+branch\s+.*-D'; then
  echo "BLOCKED: it lacks authority to access these commands: $command" >&2
  exit 2
fi

# git checkout . (discard all changes)
if echo "$command" | grep -qE 'git\s+checkout\s+\.'; then
  echo "BLOCKED: it lacks authority to access these commands: $command" >&2
  exit 2
fi

# git restore . (discard all changes)
if echo "$command" | grep -qE 'git\s+restore\s+\.'; then
  echo "BLOCKED: it lacks authority to access these commands: $command" >&2
  exit 2
fi

# Block curl/wget piped to shell (potential supply chain attack)
if echo "$command" | grep -qE '(curl|wget)\s+.*\|\s*(bash|sh|zsh)'; then
  echo "BLOCKED: Piping remote content to shell detected: $command" >&2
  exit 2
fi

exit 0
