<!-- CONTEXT-MANAGER-START -->
# Context Management System

This environment has hooks that manage context to prevent premature compaction.

## Hook Responses Are Not Errors

When a hook "blocks" a tool call, the response contains **successful results**, not errors:
- The command was executed
- Output is in the message (or cached if large)
- Successful responses are prefixed with `"None - "` (e.g., `"None - Exit 0:"`)
- Only responses WITHOUT the `"None - "` prefix are genuine errors

## Working With Cached Output

When output is cached, **filter before retrieving**:

```bash
ccm-get.py <key> --grep "error|warn"     # Lines matching pattern
ccm-get.py <key> --head 50               # First 50 lines
ccm-get.py <key> --tail 20               # Last 20 lines
ccm-get.py <key> --lines 100-200         # Line range
ccm-get.py <key> --grep "error" -C 3     # With context
```

**You called the tool for a reason. Express that reason as a filter.**

| Category | Action |
|----------|--------|
| **SMALL** (8-25KB) | Filter or retrieve directly |
| **MEDIUM** (25-50KB) | Filter preferred |
| **LARGE** (50-100KB) | Filter required |
| **MASSIVE** (>100KB) | **MUST filter** - full retrieval triggers compaction |

**Full retrieval only when:**
- Editing the file (need complete content)
- User explicitly requested full output
- Cross-referencing unpredictable sections

## Subagents for Complex Extraction

Use Task agent only when filtering isn't enough (semantic understanding needed):

```
Task(
  prompt="Run ccm-get.py <key> and summarize the error handling approach",
  subagent_type="general-purpose"
)
```

Simple patterns → use filters. Complex reasoning → use subagent.

## Subagent Behavior

Main agent calls are intercepted; **subagent (Task) calls pass through unmodified**. This lets you delegate data-intensive work without context cost.

## Commands

- `/purge` - Reduce session size when context is critical (truncates old outputs)

## Files That Bypass Interception

These always return full content:
- `CLAUDE.md`, `README.md` - Documentation
- `*.json`, `*.yaml`, `*.yml`, `*.toml` - Config files
- `*.lock`, `*.env*` - Lock and environment files

## Configuration

- `~/.claude/hooks/config.py` - All settings
- `~/.claude/compact-instructions.txt` - Compaction instructions
- Full docs: `~/.claude/hooks/CONTEXT_MANAGEMENT.md`
- External compaction design: `docs/EXTERNAL_COMPACTION.md`

### Restart After Purge

Set `CLAUDE_LAUNCH_ARGS` in `~/.claude/settings.json` to specify flags for the resume command after `/purge`:

```json
{
  "env": {
    "CLAUDE_LAUNCH_ARGS": "--dangerously-skip-permissions"
  }
}
```
<!-- CONTEXT-MANAGER-END -->
