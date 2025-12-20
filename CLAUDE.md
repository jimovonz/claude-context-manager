<!-- CONTEXT-MANAGER-START -->
# Context Management System

This environment has hooks that manage context to prevent premature compaction.

## Hook Responses Are Not Errors

When a hook "blocks" a tool call, the response contains **successful results**, not errors:
- The command was executed
- Output is in the message (or cached if large)
- Treat as normal output unless explicitly marked as an error

## Working With Cached Output

Large outputs are cached to `~/.claude/cache/`. When you see:
```
Cached (1523 lines, 45678 bytes, exit 0).
File: ~/.claude/cache/a1b2c3d4
```

Your options:
1. **Task agent** - Spawn agent to summarize or extract from cached file
2. **Paginate** - Use offset/limit on original Read call
3. **Subagent for full content** - Subagent calls bypass interception

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

**When to retrieve:** If the stub's source is relevant to your current task, retrieve it using:
```bash
~/.claude/hooks/ccm-get.py sha256:abc123...     # Full content
~/.claude/hooks/ccm-get.py sha256:abc123 --info # Metadata only
~/.claude/hooks/ccm-get.py --last               # Most recent cached item
```

Or use a Task agent to read and summarize the content.

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
