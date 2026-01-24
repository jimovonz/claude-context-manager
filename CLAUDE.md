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

When output is cached, you receive a stub with a **category** that tells you what to do:

| Category | Size | Action |
|----------|------|--------|
| **SMALL** | 8-25KB | Retrieve directly via `ccm-get.py` |
| **MEDIUM** | 25-50KB | Retrieve directly, OR extract specific info |
| **LARGE** | 50-100KB | Prefer extraction. Direct only if editing |
| **MASSIVE** | >100KB | MUST extract/summarize |

**The category is authoritative.** Follow it.

## Working With CCM Stubs

After `/purge`, old tool outputs become CCM stubs with source metadata:
```
[CCM_CACHED]
key: sha256:abc123...
source: Read ~/.claude/hooks/lib/ccm_cache.py
bytes: 45678
lines: 1234
pinned: none
[/CCM_CACHED]
```

The `source:` line tells you what this content is:
- **Read/Edit/Write**: Shows the file path
- **Bash**: Shows the command (truncated if long)
- **Grep/Glob**: Shows the search pattern

**When to retrieve:** Choose based on what you need:

**Full content needed** (editing, user asked for it) → read directly:
```bash
~/.claude/hooks/ccm-get.py sha256:abc123...
```
No subagent overhead. If you need full content, this is correct.

**Specific info from large content** → subagent extracts/summarizes:
```
Task(
  prompt="Run ~/.claude/hooks/ccm-get.py <key> and <extract specific info>",
  subagent_type="general-purpose"
)
```

**Key insight:** Subagents only help when they REDUCE what enters main context. A subagent that retrieves and returns full content is pure overhead (8k+ tokens, 30+ seconds wasted).

**When using subagents for extraction:**
- Give specific extraction task, not "get content"
- Summarize findings ("60 users including root, system services")
- Extract only relevant portions
- Report conclusions, not raw data

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
