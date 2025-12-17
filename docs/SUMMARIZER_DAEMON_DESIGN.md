# Background Summarizer Daemon Design

## Overview

A background process that monitors live Claude session files, identifies cacheable content, and uses an LLM to generate summaries. When purge runs, matching SHA keys already have summaries available.

## Architecture

```
Session File ──▶ Watcher Daemon ──▶ Haiku ──▶ CCM Cache
                     │                         (content + summary)
                     │
                     └── Tracks: last_position, processed_hashes
```

## Session File Location

```
~/.claude/projects/<hashed-path>/<session-id>.jsonl
```

Where `<hashed-path>` is the project directory with `/` replaced by `-`:
- `/home/jameo/Projects/foo` → `-home-jameo-Projects-foo`

### File Types

| Pattern | Description | Process? |
|---------|-------------|----------|
| `<uuid>.jsonl` | Main conversation sessions | Yes |
| `agent-<id>.jsonl` | Subagent (Task tool) sessions | No (responses already in main) |
| `*.jsonl.backup.*` | Backups created by purge | No |

## Identifying Current/Active Sessions

- **Current session**: Most recent `st_mtime` on `.jsonl` files
- **Active session**: `mtime` within last N minutes (e.g., 30 min)
- **Ordering**: Line order in JSONL = chronological (append-only)

## Daemon State

```python
daemon_state = {
    "sessions": {
        "~/.claude/projects/-home-user-project/abc123.jsonl": {
            "last_position": 45678,    # byte offset
            "last_mtime": 1702748123.0,
            "last_line": 234           # for debugging
        }
    }
}
```

## Processing Flow

1. Scan `~/.claude/projects/*/*.jsonl` (exclude backups, agents)
2. For files with `mtime > last_mtime` → has new content
3. Seek to `last_position`, read new lines only
4. Parse JSONL, find large tool_results/content (>10KB threshold)
5. Compute SHA256, check if already summarized in CCM
6. If new → call Haiku for summary → store in CCM metadata
7. Update `last_position` and `last_mtime`

## Race Condition Mitigation

The purge only stubs content older than `recent_lines` (default 50 lines). By the time content is eligible for stubbing, the summarizer has had plenty of time to process it.

## Multi-Session Handling

**Recommended: Single daemon watching all sessions**

- One process, shared LLM connection
- CCM cache is content-addressable (same content = same SHA = shared summary)
- Naturally deduplicates across sessions

**Detection logic:**
```python
def is_active_session(path):
    return (
        path.name.endswith('.jsonl') and
        '.backup.' not in path.name and
        not path.name.startswith('agent-')
    )
```

## Cost Considerations

- Only summarize content >10KB threshold
- Use Haiku (~$0.25/MTok input, ~$1.25/MTok output)
- Typical summary: ~50 tokens output = ~$0.0001 per summary
- Smart deduplication via SHA prevents re-summarizing same content

## Implementation Options

1. **Polling daemon** - Check files every 5-10 seconds
2. **inotify watcher** - React to file changes (Linux-specific)
3. **Hook integration** - Summarize at intercept time (adds latency)

**Recommendation:** Start with polling daemon for simplicity.

## Future Enhancements

- Summarize images (describe visual content)
- Prioritize summaries by content type (code vs logs vs output)
- Configurable summary prompts per tool type
- Integration with purge to wait for pending summaries

---

# Slash Command Direct Execution

## Problem

Current slash command flow is wasteful:
```
/ccm -s → expand markdown (tokens) → LLM reads (tokens) → LLM runs bash → output
```

For deterministic commands like `/ccm -s`, the LLM adds zero value - it's just a slow, token-wasting intermediary executing a fixed script.

## Proposed Solution

Intercept `/ccm` commands at `UserPromptSubmit` hook level:

```python
# UserPromptSubmit hook
def handle_prompt(prompt):
    if prompt.strip().startswith('/ccm'):
        args = prompt.strip()[4:].strip()
        output = run_ccm_command(args)
        return {"block": True, "result": output}
    return None
```

## Benefits

- No markdown expansion (saves tokens)
- No LLM interpretation step (saves tokens + latency)
- Direct execution → output appears in context
- LLM sees output and can respond naturally

## Implementation

1. Add `/ccm` detection to `UserPromptSubmit` pre-hook
2. Parse arguments (`-s`, `-p`, `-r`, etc.)
3. Execute corresponding script directly
4. Return output as blocked result
5. LLM receives output, continues conversation

This is the same pattern used for Bash/Read/Grep tool interception - block the call but return the actual output so the LLM can proceed with the result.
