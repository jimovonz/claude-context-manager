# Context Management System

## The Problem

Claude Code's context window is finite. When filled, automatic compaction discards content to make room. This often removes critical reasoning chains, decisions, and intermediate results - effectively "lobotomizing" the agent mid-task.

Large tool outputs are the primary cause:
- A single `find` or `grep` can produce thousands of lines
- Build logs, test output, and file listings bloat context rapidly
- Raw data displaces the reasoning that interprets it

## The Solution

This hook system intercepts tool calls to manage context proactively:

1. **Execute commands in hooks** - Commands run inside the hook, not by Claude Code
2. **Cache large outputs** - Results exceeding thresholds are saved to `~/.claude/cache/`
3. **Return references** - The agent receives a pointer to cached data, not the data itself
4. **Delegate to subagents** - Task agents can access full cached content without polluting main context

### Architecture

```
Main Agent                          Subagent (Task)
    │                                    │
    ├─► Tool Call ──► Hook              │
    │                  │                 │
    │                  ├─► Execute       │
    │                  ├─► Cache if large│
    │                  └─► Return ref ◄──┼─► Pass through (no interception)
    │                       │            │
    │   ◄── Small result ───┘            │
    │   ◄── Cache reference ─────────────┼─► Full access to cached file
```

**Key design**: Main agent calls are intercepted; subagent calls pass through unmodified. This lets the main agent delegate data-intensive work without context cost.

### Hooks

| Hook | Trigger | Purpose |
|------|---------|---------|
| `intercept-bash.py` | PreToolUse:Bash | Execute commands, cache large output |
| `intercept-glob.py` | PreToolUse:Glob | Run fd/find, cache large file lists |
| `intercept-grep.py` | PreToolUse:Grep | Run ripgrep, cache large results |
| `intercept-read.py` | PreToolUse:Read | Cache large files, whitelist configs |
| `learn-large-commands.py` | PostToolUse:Bash | Learn patterns from large outputs |
| `context-monitor.py` | UserPromptSubmit | Monitor context usage |

## Working With This System

### Hook responses are not errors

When a hook "blocks" a tool call, the response contains:
- **Execution results** - The command ran; output is in the message
- **Cache references** - Large output saved; path provided
- **Actual errors** - Clearly indicated when they occur

Treat block messages as successful results unless explicitly marked as errors.

### When you see cached output

```
Cached (1523 lines, 45678 bytes, exit 0).
File: ~/.claude/cache/a1b2c3d4
Original: grep -r "pattern" ./src

Options: Task agent (summarize or full content), or paginate with offset/limit.
```

**Your options:**

1. **Task agent for analysis** - Spawn an agent to summarize, search, or extract specific information from the cached file

2. **Task agent for full content** - If you truly need everything, have a subagent read and return it (their calls aren't intercepted)

3. **Paginate the original** - Use `offset` and `limit` parameters on the original Read call to fetch incrementally

### Files that bypass interception

These always pass through unmodified (full content needed for correctness):
- `CLAUDE.md`, `README.md`, `README` - Project documentation
- `*.json`, `*.yaml`, `*.yml`, `*.toml` - Configuration files
- `*.lock` - Lock files
- `*.env*` - Environment files

### Commands that pass through

Small/trivial commands execute normally without interception:
- `pwd`, `whoami`, `which`, `type`, `command -v`
- `echo` (simple), `true`, `false`, `exit`
- `cd`, `pushd`, `popd`
- `export`, `set`, `unset`, `alias`
- `git status`, `git branch`, `git remote`, `git config`

## Configuration

Edit `~/.claude/hooks/config.py`:

```python
CACHE_DIR = Path.home() / '.claude' / 'cache'
CACHE_MAX_AGE_MINUTES = 60      # Auto-cleanup cached files

BASH_THRESHOLD = 2000           # Bash output limit (bytes)
GLOB_THRESHOLD = 2000           # Glob results limit (bytes)
GREP_THRESHOLD = 2000           # Grep results limit (bytes)
READ_THRESHOLD = 25000          # File read limit (bytes)

PATTERNS_EXPIRY_DAYS = 30       # Learned patterns retention
METRICS_ENABLED = False         # Enable metrics logging
```

## Context Monitor

The context monitor tracks usage and warns at configurable thresholds.

### Accurate Token Counting

For accurate counting, install tiktoken:
```bash
pip install tiktoken
```

Without tiktoken, falls back to character-based estimation (~4 chars/token).

### Configuration

In `config.py`:
```python
CONTEXT_MONITOR_ENABLED = True           # Set False to disable
CONTEXT_MAX_TOKENS = 200000              # Claude's context window
CONTEXT_WARN_THRESHOLDS = [70, 80, 90]   # Warn at these percentages
CONTEXT_CHARS_PER_TOKEN = 4              # Fallback ratio without tiktoken
CONTEXT_OVERHEAD_TOKENS = 19500          # System prompt + tools overhead
```

### Accuracy Notes

| Factor | With tiktoken | Without tiktoken |
|--------|---------------|------------------|
| Token counting | Accurate (cl100k_base) | ±20% estimate |
| Thinking blocks | Excluded (correct) | Excluded (correct) |
| Overhead | Fixed estimate | Fixed estimate |
| Images/PDFs | Not counted | Not counted |

The overhead (system prompt + tools) is estimated at ~19.5k tokens. Adjust `CONTEXT_OVERHEAD_TOKENS` if you have many MCP servers.

## The `/purge` Command

When context reaches critical levels, run `/purge` to reduce session file size:

```bash
~/.claude/hooks/claude-session-purge.py --current --verbose
```

**What it does:**
- Removes thinking blocks (internal reasoning, not needed for continuity)
- Truncates large tool outputs (keeps first 500 bytes + marker)
- Repairs broken `parentUuid` chains
- Repairs orphaned `tool_use`/`tool_result` pairs
- Injects synthetic compaction if needed

**Options:**
```bash
--analyze          # Show stats without changes
--repair-only      # Fix structural issues only
--threshold N      # Truncate outputs > N bytes (default: 5000)
--keep-thinking    # Preserve thinking blocks
--dry-run          # Preview changes
```

**Important:** Creates automatic backup before modifying (`*.backup.YYYYMMDD_HHMMSS`)

## Troubleshooting

### Bypass hooks temporarily

```bash
CLAUDE_HOOKS_PASSTHROUGH=1 claude
```

### Review learned patterns

```bash
~/.claude/hooks/review-learned-commands.py
~/.claude/hooks/review-learned-commands.py --project
```

### Clear cache

```bash
rm -rf ~/.claude/cache/*
```

### Check hook registration

Hooks must be registered in `~/.claude/settings.json` to be active. Placing files in `~/.claude/hooks/` alone does nothing.
