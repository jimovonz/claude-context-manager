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
| `context-monitor.py` | Stop | Monitor context usage after each response |
| `pre-compact.py` | PreCompact | Inject custom compaction instructions |

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

## CCM (Content Cache Manager)

CCM provides durable, content-addressable caching with deduplication and pinning support. Large tool outputs are cached using SHA256 keys, enabling safe purging without data loss.

### How It Works

Instead of deleting large tool_results during purge, CCM:
1. Hashes content to generate a unique SHA256 key
2. Compresses and stores content in `~/.claude/cache/ccm/blobs/`
3. Replaces original content with a compact stub reference
4. Preserves tool_use/tool_result pairing (structural integrity)

### Stub Format

When tool_results are stubbed, they look like:
```
[CCM_CACHED]
key: sha256:abc123...
path: ~/.claude/cache/ccm/blobs/abc123.zst
bytes: 45678
lines: 1523
exit: 0
pinned: soft
[/CCM_CACHED]
```

The original content can be retrieved via Task agent or cache prune tool.

### Pin Directives

Mark important outputs for preservation using pin directives in your messages:

```
ccm:pin last level=soft reason="important build output"
ccm:pin next level=hard
ccm:pin start level=soft
... (all large outputs in range are pinned)
ccm:pin end
```

**Pin levels:**
- `none` - Pruned first by age/size
- `soft` - Pruned only after all unpinned content exhausted
- `hard` - Never auto-pruned

**Slash commands:**
- `/pin-last` - Pin the most recent large tool output
- `/pin-next` - Pin the next large tool output
- `/pin-start` - Start a pin range
- `/pin-end` - End the current pin range

### Cache Prune Tool

Manage the CCM cache with the prune tool:

```bash
# Show cache statistics
~/.claude/hooks/claude-cache-prune.py --stats

# Prune entries older than 30 days
~/.claude/hooks/claude-cache-prune.py --max-age-days 30

# Keep cache under 500MB
~/.claude/hooks/claude-cache-prune.py --max-size-mb 500

# Remove orphaned entries (not referenced by any session)
~/.claude/hooks/claude-cache-prune.py --gc-unreferenced

# Pin a specific key
~/.claude/hooks/claude-cache-prune.py --pin sha256:abc... --level hard --reason "important"

# List all pinned entries
~/.claude/hooks/claude-cache-prune.py --list-pins
```

### CCM Configuration

In `config.py`:
```python
CCM_ENABLED = True              # Enable CCM durable cache
CCM_COMPRESSION = 'auto'        # 'auto', 'zstd', 'gzip', or 'none'
CCM_DEFAULT_PIN_LEVEL = 'soft'  # Default pin level for cached content
CCM_PRUNE_MAX_AGE_DAYS = 30     # Delete unpinned items older than this
CCM_PRUNE_MAX_SIZE_MB = 500     # Max total cache size
CCM_STUB_THRESHOLD_BYTES = 5000 # tool_results larger than this get stubbed
CCM_RECENT_LINES_WINDOW = 20    # Keep recent tool_results (not stubbed)
```

### Storage Layout

```
~/.claude/cache/ccm/
├── blobs/           # Compressed content files
│   ├── abc123.zst   # zstd compressed (if available)
│   └── def456.gz    # gzip fallback
├── meta/            # Metadata sidecars
│   ├── abc123.json  # Pin status, source info, timestamps
│   └── def456.json
├── index.jsonl      # Append-only audit log
└── last_key         # Most recent cache key (for pin last)
```

### Privacy Note

The CCM cache contains tool outputs from your sessions. This may include:
- Command outputs (file listings, build logs)
- File contents (code, configs)
- Search results

The cache is stored locally in `~/.claude/cache/ccm/` with restricted permissions (700). Use `--gc-unreferenced` to clean up orphaned entries, or delete the entire directory to clear all cached content.

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

## Quick Launch with 'c' Alias

After installation, enable the quick-launch alias:

```bash
source ~/.claude/setup.sh
c  # Launches claude --dangerously-skip-permissions
```

To make permanent, add to `~/.bashrc` or `~/.zshrc`:
```bash
source ~/.claude/setup.sh
```

Or just add the alias directly:
```bash
alias c='claude --dangerously-skip-permissions'
```

### Configuration via setup.sh

```bash
COMPACT_PCT=70 source ~/.claude/setup.sh   # Custom compact threshold (percent)
SKIP_PERMISSIONS=false source ~/.claude/setup.sh  # Disable skip-permissions
```

## Auto-Compaction Control

Control when automatic compaction triggers via `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`.

### Configuration

In `config.py`:
```python
AUTOCOMPACT_ENABLED = True   # Enable threshold override
AUTOCOMPACT_THRESHOLD = 80   # Trigger at 80% context (percent)
```

The threshold is set in `~/.claude/settings.json`:
```json
{
  "env": {
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "80"
  }
}
```

### Why Control Compaction?

The default compaction threshold is often too aggressive. By setting it to 80%, you:
- Get more context utilization before compaction kicks in
- Maintain longer reasoning chains
- Allow the context monitor to warn you before compaction

## Custom Compaction Instructions

When compaction does occur, the `pre-compact.py` hook injects custom instructions to guide what gets preserved.

### Customizing

Edit `~/.claude/compact-instructions.txt`:

```
Focus on preserving:
- Current task context and objectives
- Key decisions made and their rationale
- Important file paths and code locations discovered
- Any pending actions or TODOs
- Error messages and debugging context being investigated
- Critical state (connections, configurations, credentials referenced)

Summarize completed work concisely. Prioritize actionable context over historical details.
Maintain enough context to continue the current task without re-reading files.
```

### Configuration

In `config.py`:
```python
PRE_COMPACT_ENABLED = True  # Set False to disable

# Default instructions (used if compact-instructions.txt doesn't exist)
COMPACT_INSTRUCTIONS = """Focus on preserving:
- Current task context and objectives
..."""
```
