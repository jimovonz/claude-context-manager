# CLI Patching and Launch System

## Overview

The `c` command launches Claude Code with CCM integration. It handles:
1. Starting the thinking proxy
2. Applying CLI patches (in-place)
3. Setting environment variables
4. Preserving skill loading

## The `c` Command

Location: `~/.claude/hooks/c`

### What It Does

```bash
c                    # Launch claude with CCM
c --resume abc123    # Resume a session
c -p "do something"  # Run with prompt (non-interactive)
```

### Execution Flow

1. **Find session ID** - From args or most recent session for current directory
2. **Ensure proxy running** - Start thinking proxy if not already up
3. **Check/apply patches** - Patch CLI in-place if needed
4. **Set environment variables** - Scoped to claude process only
5. **Execute claude** - With `--dangerously-skip-permissions`

### Environment Variables Set

| Variable | Purpose | Default |
|----------|---------|---------|
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | Compaction threshold | 95 |
| `ANTHROPIC_BASE_URL` | Route through proxy | `http://127.0.0.1:8080` |
| `ANTHROPIC_CUSTOM_HEADERS` | Session ID for tracking | `X-CCM-Session-ID:<id>` |

### Configuration

Override defaults with env vars:

```bash
COMPACT_PCT=90 c                    # Different threshold
SKIP_PERMISSIONS=false c            # Prompt for permissions
THINKING_PROXY_PORT=9000 c          # Different port
```

## CLI Patching

Location: `~/.claude/hooks/patch-autocompact.py`

### Why Patching Is Needed

Claude Code 2.1.x has several issues that require patching:

| Issue | Problem | Solution |
|-------|---------|----------|
| Trigger threshold | `Math.min` prevents raising threshold | Change to `Math.max` |
| Display calculation | `/context` ignores env override | Use same threshold function |
| Percentage base | Calculates from 136k not 200k | Calculate from total window |
| Hook reply | Block responses shown as "error" | Change tag to "reply" |
| File injection | Full diffs injected on external changes | Minimal notification |

### In-Place Patching (Critical)

**Patches MUST be applied in-place to the original CLI.**

Running a copied/patched CLI from a different location breaks skill loading:

```bash
# WRONG - Skills won't load
node ~/.claude/patched/cli-ccm-xxx.js  # Skills empty (1610 chars)

# CORRECT - Skills load properly
claude  # With in-place patches (2151 chars with skills)
```

**Root cause:** Node.js module resolution. When running from `~/.claude/patched/`,
the CLI cannot find its dependencies and skill loading silently fails. The Skill
tool gets sent with an empty "Available skills:" list.

### Patch Operations

```bash
# Check current status
~/.claude/hooks/patch-autocompact.py --check
# Output: "OK: Fully patched" or "Status: trigger: NEEDS PATCH; ..."

# Apply patches in-place
~/.claude/hooks/patch-autocompact.py --patch
# Output: "[CCM] Patched trigger: Math.min→Math.max; ..."

# Restore original (from backup)
~/.claude/hooks/patch-autocompact.py --restore

# Get patched CLI path (legacy - don't use for launching)
~/.claude/hooks/patch-autocompact.py --get-patched
```

### Backup Location

Original CLI is backed up before first patch:
```
~/.nvm/versions/node/vX.X.X/lib/node_modules/@anthropic-ai/claude-code/cli.js.ccm-backup
```

### After CLI Updates

When Claude Code auto-updates, patches are lost. The `c` command auto-detects
this and re-applies patches on next launch:

```bash
c  # Detects unpatched CLI, applies patches automatically
```

Or manually:
```bash
~/.claude/hooks/patch-autocompact.py --check   # See status
~/.claude/hooks/patch-autocompact.py --patch   # Re-apply
```

## Skills Handling

### Skill Locations

Claude Code loads skills from:
- `~/.claude/skills/` - User skills (global)
- `.claude/skills/` - Project skills (local)
- `~/.claude/commands/` - Legacy location

CCM symlinks for compatibility:
```
~/.claude/skills -> ~/.claude/commands
```

### Skill Loading Issue

**Problem:** Running patched CLI as `node path/to/cli.js` breaks skill loading.

**Symptoms:**
- `/skills` shows "No skills found"
- Skill tool description shows empty "Available skills:" (1610 chars vs 2151)
- Skills work fine with unpatched `claude` binary

**Diagnosis via proxy logs:**
```bash
# Check Skill tool size in proxy log
grep "Skill tool" ~/.claude/proxy.log | tail -5

# Empty: "Skill tool: 1610 chars, has_skills=True, preview=...Available skills:\n\n"
# Working: "Skill tool: 2151 chars, has_skills=True, preview=...ccm...recap...relay..."
```

**Solution:** Apply patches in-place, use original `claude` binary. Never run
a copied CLI file directly.

### Available Skills

| Skill | Command | Description |
|-------|---------|-------------|
| ccm | `/ccm` | Context manager operations |
| recap | `/recap` | Read project docs to get up to speed |
| relay | `/relay` | SSH relay for persistent connections |

Disabled skills (in `~/.claude/commands/disabled/`):
- `pin-start`, `pin-end`, `pin-next`, `pin-last` - Output pinning

## Thinking Proxy

Location: `~/.claude/hooks/thinking-proxy.py`

### Purpose

The proxy sits between Claude Code and the Anthropic API:

```
Claude Code → Proxy (localhost:8080) → Anthropic API
```

### Features

1. **System prompt abbreviation** - Replace verbose prompt (~13KB) with concise version (~4KB)
2. **Tool description abbreviation** - Compress tool schemas
3. **Session tracking** - Track sessions for state management
4. **External compaction** - Route `/compact` to OpenRouter (see EXTERNAL_COMPACTION.md)
5. **Skill preservation** - Extract and preserve whitelisted skills

### System Prompt Abbreviation

The proxy replaces Claude Code's default system prompt with `ABBREVIATED_SYSTEM_PROMPT`:

| Component | Original | Abbreviated |
|-----------|----------|-------------|
| System prompt | ~13KB | ~4KB |
| Tool descriptions | ~40KB | ~5KB |

Key preserved content:
- Core identity and purpose
- File operation guidelines
- Git safety rules
- Task planning emphasis
- Code quality guidelines

### Skill Preservation

The proxy extracts whitelisted skills from the original system prompt and appends
them to the abbreviated version:

```python
PRESERVED_SKILLS = ['relay', 'ccm', 'pin-next', 'pin-last', 'pin-start', 'pin-end', 'recap']
```

Skills are extracted using regex and appended under `# Skills` section.

### Proxy Operations

```bash
# Start/stop/restart
~/.claude/hooks/thinking-proxy.py start
~/.claude/hooks/thinking-proxy.py stop
~/.claude/hooks/thinking-proxy.py restart

# Check status
~/.claude/hooks/thinking-proxy.py status

# View logs
tail -f ~/.claude/proxy.log
```

### Debugging

Enable debug logging in `~/.claude/hooks/config.py`:
```python
THINKING_PROXY_DEBUG_LOG = True
```

Check proxy logs for:
- Request/response flow
- System block structure
- Skill tool content
- Compaction detection

## Troubleshooting

### Skills not loading

1. Check if using `c` command (not direct `claude`)
2. Verify symlink: `ls -la ~/.claude/skills`
3. Check proxy logs: `grep "Skill tool" ~/.claude/proxy.log`
4. Ensure patches applied in-place: `~/.claude/hooks/patch-autocompact.py --check`

### Patches not applying

1. Check CLI location: `which claude`
2. Verify backup exists: `ls ~/.nvm/.../cli.js.ccm-backup`
3. Run with verbose: `~/.claude/hooks/patch-autocompact.py --patch`

### Proxy not starting

1. Check port available: `lsof -i :8080`
2. Check logs: `tail ~/.claude/proxy.log`
3. Kill stale process: `kill $(cat ~/.claude/proxy.pid)`
4. Start manually: `~/.claude/hooks/thinking-proxy.py start`

### Environment not set

1. Verify using `c` command
2. Check env in claude: run `env | grep ANTHROPIC` in bash tool
3. Ensure proxy URL correct: `http://127.0.0.1:8080`
