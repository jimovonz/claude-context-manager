# Claude Context Manager

[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](https://github.com/rpl-james-overington2/claude-context-manager)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

Hooks and tools for managing Claude Code's context window to prevent premature compaction.

## The Problem

Claude Code's context window is finite. When filled, automatic compaction discards content - often removing critical reasoning chains and decisions mid-task. Large tool outputs (grep results, build logs, file listings) are the primary cause.

## The Solution

This system intercepts tool calls to manage context proactively:

1. **Execute in hooks** - Commands run inside hooks, results cached if large
2. **Return references** - Main agent gets pointers to cached data, not the data itself
3. **Delegate to subagents** - Task agents access full content without polluting main context
4. **Purge on demand** - `/purge` command truncates old outputs

## Requirements

- Python 3.10+
- Claude Code CLI installed

## Installation

```bash
git clone https://github.com/rpl-james-overington2/claude-context-manager.git
cd claude-context-manager
python3 install.py

# Optional: accurate token counting (recommended)
pip install tiktoken
```

Hooks activate on your next Claude Code session.

### Quick Start

After installation:
```bash
source ~/.claude/setup.sh  # Enable 'c' alias
c                          # Launch with --dangerously-skip-permissions
```

To make permanent, add to `~/.bashrc` or `~/.zshrc`:
```bash
source ~/.claude/setup.sh
```

## Uninstallation

```bash
python3 uninstall.py
```

## Quick Disable/Enable

If hooks cause issues, quickly disable without uninstalling:

```bash
python3 disable.py   # Disable all hooks (keeps files)
python3 enable.py    # Re-enable hooks
```

Or use environment variable for a single session:
```bash
CLAUDE_HOOKS_PASSTHROUGH=1 claude
```

## Surviving Claude Code Updates

**Your hooks will survive Claude Code updates.** The `~/.claude/` directory is user configuration space - Claude Code updates only touch the application in `~/.local/share/claude/` (native) or `node_modules/` (npm).

However, if Claude Code changes its hook API in a breaking way, hooks may need updating. Check the repository for compatibility updates after major Claude Code releases.

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
│   ├── pre-compact.py         # Custom compaction instructions
│   ├── claude-session-purge.py # Session purge tool
│   ├── config.py              # Configuration
│   └── lib/common.py          # Shared library
├── commands/
│   └── purge.md               # /purge slash command
├── setup.sh                   # Shell alias setup
├── compact-instructions.txt   # Compaction instructions (customizable)
└── settings.json              # Hook registration (merged)
```

## Usage

### Important: "Blocking" Is Not an Error

When hooks intercept a tool call, Claude Code displays it as an "error" or "blocking" message in the terminal. **This is normal behavior, not an actual error.** The hook has:

1. Executed the command successfully
2. Returned the results (inline or cached)
3. Prevented double-execution by "blocking" the original call

Treat these messages as successful results unless they explicitly indicate a failure.

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

At 70%, 80%, 90% context usage (configurable), you'll see warnings:
```
⚠️ WARNING: Context at 72% (~144,000 tokens, tiktoken). Consider running /purge soon.
```

For accurate token counting, install tiktoken: `pip install tiktoken`

### The `/purge` Command

When context is critical, run `/purge` to:
- Preserve thinking blocks (for future development)
- Truncate large tool outputs
- Repair any structural issues

### Auto-Compaction Control

Compaction triggers at 80% context by default (configurable). This is set via `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` in settings.json.

### Custom Compaction Instructions

When compaction occurs, `pre-compact.py` provides instructions for what to preserve. Customize by editing:
```bash
~/.claude/compact-instructions.txt
```

## Configuration

Edit `~/.claude/hooks/config.py`:

```python
# Cache settings
CACHE_DIR = Path.home() / '.claude' / 'cache'
CACHE_MAX_AGE_MINUTES = 60

# Output thresholds (bytes)
BASH_THRESHOLD = 2000
GLOB_THRESHOLD = 2000
GREP_THRESHOLD = 2000
READ_THRESHOLD = 25000

# Auto-compaction
AUTOCOMPACT_ENABLED = True
AUTOCOMPACT_THRESHOLD = 80   # percent of context

# Pre-compact hook
PRE_COMPACT_ENABLED = True

# Context monitor
CONTEXT_MONITOR_ENABLED = True
CONTEXT_WARN_THRESHOLDS = [70, 80, 90]

PATTERNS_EXPIRY_DAYS = 30
METRICS_ENABLED = False
```

## Files That Bypass Interception

These always pass through unmodified:
- `CLAUDE.md`, `README.md` - Project documentation
- `*.json`, `*.yaml`, `*.yml`, `*.toml` - Configuration
- `*.lock`, `*.env*` - Lock and environment files

## Thinking Proxy

The thinking proxy is an optional component that manages Claude's thinking blocks on a per-session basis. It allows sessions to benefit from extended thinking until purge, then automatically disables thinking to prevent API consistency errors.

### How It Works

1. **Before purge**: Sessions operate normally with full thinking enabled
2. **After `/purge`**: The session is flagged for no-thinking mode
3. **On resume**: The proxy strips thinking from requests/responses for that session

### Starting the Proxy

```bash
# Linux (systemd)
systemctl --user start ccm-thinking-proxy
systemctl --user enable ccm-thinking-proxy  # Start on login

# Manual (any platform)
~/.claude/hooks/thinking-proxy.py start   # Start daemon
~/.claude/hooks/thinking-proxy.py status  # Check status
~/.claude/hooks/thinking-proxy.py stop    # Stop daemon
```

### Requirements

The proxy requires aiohttp:
```bash
pip install aiohttp
```

### Disabling the Proxy

To bypass the proxy temporarily:
```bash
unset ANTHROPIC_BASE_URL
claude
```

Or set `USE_THINKING_PROXY=false` before sourcing setup.sh.

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
