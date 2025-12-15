# Claude Context Manager

Hooks and tools for managing Claude Code's context window to prevent premature compaction.

## The Problem

Claude Code's context window is finite. When filled, automatic compaction discards content - often removing critical reasoning chains and decisions mid-task. Large tool outputs (grep results, build logs, file listings) are the primary cause.

## The Solution

This system intercepts tool calls to manage context proactively:

1. **Execute in hooks** - Commands run inside hooks, results cached if large
2. **Return references** - Main agent gets pointers to cached data, not the data itself
3. **Delegate to subagents** - Task agents access full content without polluting main context
4. **Purge on demand** - `/purge` command removes thinking blocks and truncates old outputs

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/claude-context-manager.git
cd claude-context-manager
python3 install.py
```

Hooks activate on your next Claude Code session.

## Uninstallation

```bash
python3 uninstall.py
```

## What Gets Installed

```
~/.claude/
├── hooks/
│   ├── intercept-bash.py      # Bash command interception
│   ├── intercept-glob.py      # File glob interception
│   ├── intercept-grep.py      # Grep/ripgrep interception
│   ├── intercept-read.py      # Large file read interception
│   ├── context-monitor.py     # Context usage warnings
│   ├── learn-large-commands.py # Pattern learning
│   ├── claude-session-purge.py # Session purge tool
│   ├── config.py              # Configuration
│   └── lib/common.py          # Shared library
├── commands/
│   └── purge.md               # /purge slash command
└── settings.json              # Hook registration (merged)
```

## Usage

### Automatic Interception

Once installed, hooks work automatically:

- **Small outputs** pass through normally
- **Large outputs** (>2KB) are cached to `~/.claude/cache/`
- **Subagent calls** bypass interception (full access for Task agents)

When you see a cache reference:
```
Cached (1523 lines, 45678 bytes, exit 0).
File: ~/.claude/cache/a1b2c3d4
```

Options:
1. Spawn a Task agent to summarize or extract from the cached file
2. Use offset/limit parameters to paginate the original
3. Have a Task agent return full content if truly needed

### Context Warnings

At 50%, 60%, 70%, 80%+ context usage, you'll see warnings:
```
⚠️ WARNING: Context at ~72%. Consider running /purge soon.
```

### The `/purge` Command

When context is critical, run `/purge` to:
- Remove thinking blocks (not needed for continuity)
- Truncate large tool outputs
- Repair any structural issues

## Configuration

Edit `~/.claude/hooks/config.py`:

```python
CACHE_DIR = Path.home() / '.claude' / 'cache'
CACHE_MAX_AGE_MINUTES = 60

BASH_THRESHOLD = 2000     # bytes
GLOB_THRESHOLD = 2000
GREP_THRESHOLD = 2000
READ_THRESHOLD = 25000

PATTERNS_EXPIRY_DAYS = 30
METRICS_ENABLED = False
```

## Files That Bypass Interception

These always pass through unmodified:
- `CLAUDE.md`, `README.md` - Project documentation
- `*.json`, `*.yaml`, `*.yml`, `*.toml` - Configuration
- `*.lock`, `*.env*` - Lock and environment files

## Troubleshooting

### Bypass hooks temporarily
```bash
CLAUDE_HOOKS_PASSTHROUGH=1 claude
```

### Analyze session without changes
```bash
~/.claude/hooks/claude-session-purge.py --current --analyze
```

### Clear cache
```bash
rm -rf ~/.claude/cache/*
```

## Documentation

Full documentation: `~/.claude/hooks/CONTEXT_MANAGEMENT.md`

## License

MIT
