# CCM Architecture Reference

Authoritative documentation of all Claude Context Manager functionality. This document describes every component, behavior, configuration option, and state file.

---

## System Overview

CCM manages Claude Code's context window through:

1. **Intercept hooks** — Execute tool calls, cache large outputs, return references
2. **CCM cache** — Content-addressable durable storage with SHA256 deduplication and compression
3. **Thinking proxy** — Routes API traffic, manages thinking blocks, handles external compaction
4. **CLI patcher** — In-place fixes for Claude Code's context threshold bugs
5. **Session purge** — Reduces context by caching old tool outputs
6. **Context monitor** — Warns before context fills
7. **Learning system** — Records commands that produce large output
8. **Cache pruner** — Maintenance, garbage collection, pin management

---

## 1. Intercept Hooks

All hooks share common behavior:
- **Subagent bypass** — Task agents pass through unimpeded (full access)
- **Cache directory blocking** — Prevents main agent from reading cache files directly
- **`json_block()` with exit_code** — When exit_code=0, output is prefixed with `"None - "` so the CLI's `"Error: "` prefix becomes `"Error: None - "`, signaling the model that the operation succeeded despite the error-like display
- **Size-proportional guidance** — Cached responses include retrieval instructions based on output size category

### 1.1 Bash Intercept (`hooks/intercept-bash.py`)

Executes bash commands with intelligent routing.

**Flow:**
1. Allow subagents through
2. Pass CCM management scripts (ccm-get, ccm-setup, etc.)
3. Block cache file access
4. Pass trivial commands (fast-path, no execution needed)
5. Pass interactive commands (ssh, vim, etc.)
6. Classify unknown commands with Haiku
7. Execute via `probe_command()` (continuous monitoring, stdin closed)
8. Learn if output was large
9. Return inline (<8KB) or cached (≥8KB)

**Trivial commands (passthrough):**
- Simple: ls, pwd, whoami, date, echo, printf
- Git: status, branch, remote, log -N
- File ops: mkdir, touch, rm, mv, cp
- Utilities: which, type, head, tail, wc, stat
- Variable assignments

**Interactive detection:**
- TTY-based: ssh, vim, nano, top, htop, less, more
- Pattern matching in output: [Y/n], passwords, "Press Enter", etc.

**Output format (inline):**
```
None - Exit {code}:

{output}
```

**Output format (cached):**
```
None - [CCM_CACHED]
key: sha256:...
~tokens: Nk
lines: N
[/CCM_CACHED]
```

**Threshold:** 8000 bytes (configurable: `BASH_THRESHOLD`)

### 1.2 Grep Intercept (`hooks/intercept-grep.py`)

Executes search via ripgrep.

**Ripgrep discovery priority:**
1. Claude's bundled version (`vendor/ripgrep/{platform}/rg`)
2. Claude's native installer (`--ripgrep` flag)
3. System `rg`

**Parameters supported:**
- `pattern`, `output_mode` (files_with_matches/count/content)
- `-i` (case insensitive), `multiline`
- `glob` (file pattern), `type` (file type)
- `-A/-B/-C` (context lines), `-n` (line numbers)
- `head_limit/offset` (pagination)

**Exit code normalization:** Exit 0 or 1 (no matches) are treated as success (get "None - " prefix). Exit 2+ is actual error (bad pattern, permission denied).

**Output format (inline, exit 0 or 1):**
```
None - {output}
```
Or `"None - No matches."` if empty.

**Threshold:** 8000 bytes (configurable: `GREP_THRESHOLD`)

### 1.3 Glob Intercept (`hooks/intercept-glob.py`)

Executes file pattern matching.

**Implementation:**
- Uses `fd` if available (faster, respects .gitignore)
- Falls back to `find` with sorting
- Expands `~` in paths
- Timeout: 60 seconds

**Exit code normalization:** Same as Grep (exit 0-1 = success, exit 2+ = error).

**Output format:** Same as Grep.

**Threshold:** 8000 bytes (configurable: `GLOB_THRESHOLD`)

### 1.4 Read Intercept (`hooks/intercept-read.py`)

Caches large files before they enter context.

**Passthrough file types (always full content):**
- CLAUDE.md, README.md
- *.json, *.yaml, *.yml, *.toml
- *.lock, *.env*

**Flow:**
1. Allow subagents through
2. Block cache file reads (return retrieval instructions)
3. Allow paginated reads (offset/limit specified)
4. Pass small files (<25KB)
5. Pass whitelisted file types
6. Cache large files (≥25KB) and return stub

**Threshold:** 25000 bytes (configurable: `READ_THRESHOLD`)

---

## 2. Common Library (`hooks/lib/common.py`)

Shared utilities for all hooks.

### 2.1 json_block() — Hook Response

```python
def json_block(reason: str, exit_code: int = None) -> None:
```

When `exit_code=0`, prepends `"None - "` to the reason. This is the mechanism by which successful hook blocks are distinguished from errors in the CLI display:
- CLI always shows: `Error: {reason}`
- With prefix: `Error: None - Exit 0: ...` — model recognizes success
- Without prefix: `Error: Exit 1: ...` — model recognizes failure
- Error messages (no exit_code passed): `Error: Error reading file: ...` — genuine error

### 2.2 Subagent Detection

Checks `tool_use_id` against agent JSONL files to detect Task agent calls. Searches:
- New structure: `session/subagents/*.jsonl`
- Old structure: `agent-*.jsonl`

Only checks last 64KB of each file for performance.

### 2.3 Command Execution

**`run_command(cmd, timeout)`** — Simple execution with timeout.

**`probe_command(cmd, timeout, no_output_timeout)`** — Continuous monitoring:
- Closes stdin (fails interactive commands immediately)
- Streams output with 0.5s polling
- Checks for interactive patterns in real-time
- Dual timeout: full timeout + no-output timeout

### 2.4 Command Classification (Haiku)

For unknown commands, asks Claude Haiku:
- `interactive`: 0|1 (prompts for user input?)
- `large_output`: 0|1 (produces >50 lines?)

Results cached in `~/.claude/hooks/command-cache.json`.

### 2.5 Size Categories

| Category | Size | Guidance |
|----------|------|----------|
| SMALL | 8-25KB | Retrieve directly via ccm-get.py |
| MEDIUM | 25-50KB | Retrieve directly OR extract specific info |
| LARGE | 50-100KB | Extract via subagent (direct only if editing) |
| MASSIVE | >100KB | MUST use subagent — will trigger compaction |

### 2.6 Learning

`learn_command_classification()` records runtime behavior (large output) to patterns files for future fast-path decisions.

---

## 3. CCM Cache (`hooks/lib/ccm_cache.py`)

Content-addressable durable cache with SHA256 deduplication, compression, and pinning.

### 3.1 Storage Layout

```
~/.claude/cache/ccm/
    blobs/{first2}/{remaining62}.zst   # Compressed content
    meta/{first2}/{remaining62}.json   # Metadata sidecar
    index.jsonl                        # Append-only log
    last_key                           # Most recent key
```

### 3.2 Metadata Structure

```json
{
  "key": "sha256:...",
  "created_at": "ISO-8601",
  "last_access_at": "ISO-8601",
  "access_count": 1,
  "bytes_uncompressed": 45678,
  "bytes_compressed": 12345,
  "lines": 500,
  "compression": "zstd",
  "source": {
    "session_path": "...",
    "tool_name": "Bash",
    "exit_code": 0,
    "command": "find . -name '*.py'",
    "cwd": "/home/user/project"
  },
  "pinned": {
    "level": "soft",
    "reason": "user pinned",
    "pinned_at": "ISO-8601"
  }
}
```

### 3.3 Compression

- Threshold: 1KB (don't compress small content)
- Priority: zstd > gzip > none
- Auto-detects available compression
- Configurable: `CCM_COMPRESSION` (auto/zstd/gzip/none)

### 3.4 Pin Levels

- **none** — Subject to age/size pruning and GC
- **soft** — Protected from automatic pruning, removable by explicit prune
- **hard** — Never deleted by any automatic process

### 3.5 Stub Format

```
[CCM_CACHED]
key: sha256:abc123...
~tokens: 11k
lines: 500
exit: 1          # Only shown if non-zero
[/CCM_CACHED]
```

### 3.6 Key Functions

- `compute_content_key()` — SHA256 of content
- `store_content()` — Store with deduplication (returns existing key if match)
- `retrieve_content()` — Fetch and decompress (updates access stats)
- `get_metadata()` — Load metadata JSON
- `update_pin()` — Change pin level
- `build_ccm_stub()` — Format minimal stub
- `list_all_keys()` — Enumerate cache
- `get_cache_stats()` — Totals, pin counts, access info
- `delete_cached_content()` — Remove blob + metadata

---

## 4. Thinking Proxy (`hooks/thinking-proxy.py`)

Async HTTP proxy managing thinking blocks, system prompt abbreviation, and external compaction routing.

### 4.1 System Prompt Abbreviation

Replaces Claude Code's verbose ~19.5KB system prompt with a ~2.9KB version:
- Preserves core behavioral instructions
- Extracts whitelisted skills (relay, ccm) from full prompt
- Saves ~16KB per request
- Configurable: `PRESERVED_SKILLS` list

### 4.2 Tool Description Abbreviation

Replaces verbose tool descriptions with parameter signatures:
- `Task` → "description, prompt, subagent_type, model?, resume?, run_in_background?"
- `Bash` → "command, description?, timeout?, run_in_background?"
- `Read` → "file_path, offset?, limit?"
- etc.
- Saves ~40KB if all tools abbreviated
- Skips `Skill` (dynamic content)

### 4.3 Session State: No-Thinking Mode

After first compaction, a session is marked for no-thinking:
- Flag file: `~/.claude/proxy-state/no-thinking/{session_id}`
- Strips thinking blocks from requests (prevents API errors)
- Strips thinking blocks from responses
- Created by purge tool or after external compaction

### 4.4 ThinkingBlockFilter

Processes SSE stream to remove thinking blocks:
- Tracks thinking block indices during streaming
- Adjusts block indices after removal (renumbering)
- Handles incomplete SSE events (buffering)
- Processes `content_block_start`, `content_block_delta`, `content_block_stop` events

### 4.5 External Compaction

Routes compaction requests to external LLMs via OpenRouter.

**Detection:** System prompt contains "summarizing conversations" (Claude Code's compaction marker).

**Flow:**
1. Detect compaction request
2. Strip thinking blocks from messages
3. Split messages: bulk + preserved recent
4. Select model based on compaction count
5. Transform to OpenAI format
6. Send to OpenRouter with custom distillation prompt
7. Extract artefacts from response
8. Stream response in Claude SSE format
9. Append preserved messages verbatim
10. Mark session for no-thinking
11. Write debug files

**Model selection:**
- Compactions 1-5: `COMPACTION_MODELS['early']` (default: gemini-3-flash-preview)
- Compactions 6+: `COMPACTION_MODELS['late']` (default: gemini-3-flash-preview)

**Token limits (progressive):**
| Compaction # | Max Tokens |
|---|---|
| 1 | 20000 |
| 2 | 36000 |
| 3 | 52000 |
| 4 | 64000 |
| 5+ | 64000 |

**Artefact extraction:** Parses structured ARTEFACTS section from compaction output. Stored for next compaction's delta mode (only new artefacts added).

**Debug files:**
- `~/.claude/last-compaction-request.json`
- `~/.claude/last-artefacts.txt`
- `~/.claude/last-distillation.txt`

### 4.6 Daemon Management

- `start` — Double-fork daemon, writes PID file
- `stop` — SIGTERM from PID file
- `status` — Running check, no-thinking sessions
- `restart` — Stop then start
- `serve` — Foreground mode

### 4.7 Systemd Service

Auto-installed at `~/.config/systemd/user/ccm-thinking-proxy.service`.

---

## 5. CLI Patcher (`hooks/patch-autocompact.py`)

Applies 8 in-place patches to Claude Code's `cli.js`.

### 5.1 Patch List

| # | Name | Problem | Fix |
|---|------|---------|-----|
| 1 | Trigger | `Math.min` prevents raising threshold | `Math.min` → `Math.max` |
| 2 | Display | `/context` shows hardcoded buffer | Calls threshold function |
| 3 | Pct Base | % from available (136k) not total (200k) | Uses total context window |
| 4 | Hook Reply | Block responses tagged "error" | Changed to "reply" |
| 5 | File Injection | Full diffs on external file changes | Minimal notification |
| 6 | Threshold | Warning/error buffers too large (20k) | Reduced: 10k/2k/1k/3k |
| 7 | Blocking Limit | Context blocking at 67% | Aligned with autocompact threshold |

**Thinking budget alignment:** The API rejects requests when `input_tokens + max_tokens > context_window`. With extended thinking, `max_tokens` includes the thinking budget. `MAX_THINKING_TOKENS` (default: 10000, set in `config.py`) caps thinking output so the API input limit (200k - 10k = 190k) aligns with the 95% autocompact threshold and the blocking limit patch.

**Note:** The former "Hook is_error" patch (changing `Error:` to `None:` in CLI display) has been removed. This is now handled at the hook level via the `"None - "` prefix in `json_block()`, which is CLI-version-independent and permanent.

### 5.2 Auto-Repair (`hooks/patch-repair.py`)

When heuristic patterns stop matching after a CLI update:

1. `c` detects `--get-patched` failure
2. Invokes `patch-repair.py --verbose`
3. Repair script:
   - Runs `--check` to identify failed patches
   - Extracts context from cli.js around stable anchors (env var names, string literals)
   - Builds a focused prompt describing each patch's intent and constraints
   - Calls `claude --print` for pattern analysis
   - Parses JSON response with `(old, new)` pairs
   - Applies repairs with length constraint enforcement
   - Creates patched mirror
4. `c` continues with repaired CLI

**Stable anchors** (persist across versions):
- `AUTOCOMPACT_PCT_OVERRIDE` — locates threshold calculation
- `CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE` — locates blocking limit
- `hook_blocking_error` / `hook_blocking_reply` — locates hook message strings
- `edited_text_file` — locates file injection case

**Commands:**
```bash
patch-repair.py              # Repair and output patched CLI path
patch-repair.py --check      # Show which patches need repair
patch-repair.py --verbose    # Detailed progress
```

### 5.3 Persistent Patched Copy

The patcher creates a mirror at `~/.claude/patched/claude-code/`:
- Symlinks all files except `cli.js`
- Copies and patches `cli.js`
- Survives auto-updates (separate location)
- Re-patches automatically when CLI hash changes

### 5.4 Cache

Patch status cached in `~/.claude/patch-cache/autocompact-patch.json` by file hash. Avoids re-checking on every invocation.

### 5.5 Commands

```bash
patch-autocompact.py --check    # Show patch status
patch-autocompact.py --patch    # Apply patches
patch-autocompact.py --restore  # Restore original
```

---

## 6. Session Purge (`hooks/claude-session-purge.py`)

Reduces session size by caching old tool outputs.

### 6.1 Structural Requirements

Session JSONL must maintain:
1. **parentUuid chain** — Linked list integrity between messages
2. **tool_use → tool_result pairing** — Every tool_use has matching tool_result
3. **Compaction summaries** — Never deleted (marked `isCompactSummary`)

### 6.2 Purge Flow

1. Load session JSONL
2. Check for compaction summary (inject synthetic if missing)
3. Repair parent chain (fix broken parentUuid links)
4. Repair tool pairing (remove orphaned blocks)
5. Parse and resolve pin directives
6. Process content:
   - Skip compaction summaries (protected)
   - Stub old tool_results (>20 lines from end): cache to CCM with metadata
   - Stub old images: cache base64 data
   - Keep recent content (≤20 lines from end): truncate but keep
7. Write backup
8. Write modified session
9. Signal thinking proxy (create no-thinking flag)
10. Auto-restart if requested

### 6.3 Pin Directives

Pattern: `ccm:pin <type> [level=soft|hard] [reason="..."]`

Types:
- **last** — Pin nearest preceding large tool_result
- **next** — Pin next large tool_result after directive
- **start...end** — Pin all large tool_results in range

Pin directives are emitted in assistant responses and resolved during purge.

### 6.4 Synthetic Compaction

If no compaction summary exists in session, injects a minimal one:
- Sets `isCompactSummary=true`
- Uses null parentUuid as chain root
- Enables thinking block purging
- Updates first content message's parentUuid

### 6.5 Structural Repair

**Parent chain:** Finds broken parentUuid references, links to nearest previous UUID.

**Tool pairing:** Removes orphaned tool_results without tool_use, and tool_uses without tool_result. Keeps messages with other content alongside orphans.

### 6.6 Commands

```bash
claude-session-purge.py --current --verbose          # Purge current session
claude-session-purge.py --current --analyze          # Stats only
claude-session-purge.py --current --repair-only      # Fix structure only
claude-session-purge.py --current --restart          # Purge and restart
claude-session-purge.py --current --threshold 3000   # Custom threshold
claude-session-purge.py --current --recent-lines 30  # Larger recent window
```

---

## 7. Context Monitor (`hooks/context-monitor.py`)

UserPromptSubmit hook that warns when context crosses thresholds.

### 7.1 Token Counting

- Primary: tiktoken (accurate)
- Fallback: ~2.5 chars/token estimation

### 7.2 Estimation Method

1. Find last compaction in session (scan in reverse)
2. Extract text from all messages after compaction
3. Count tokens with multiplier (1.5x for structure/metadata)
4. Add overhead (proxy-aware: 10k if proxied, 45k if not)
5. Calculate percentage of 200k total

**Proxy awareness:** If `ANTHROPIC_BASE_URL` is set (proxy active, abbreviated prompt/tools), uses 10k overhead. Without proxy (full system prompt + tools), uses 45k.

### 7.3 Warning Levels

| Threshold | Level | Message |
|-----------|-------|---------|
| ≥90% | CRITICAL | "Run /purge NOW" |
| ≥80% | WARNING | "Consider /purge soon" |
| <80% | NOTE | "/purge available" |

### 7.4 TTY Output

Warnings written directly to terminal TTY:
1. Walk process tree to find Claude's TTY
2. Try parent TTYs via `/proc/{pid}/fd/1`
3. Fall back to `/dev/tty`
4. ANSI yellow coloring

### 7.5 State Tracking

- State file: `~/.claude/state/{session_id}-context-level`
- Tracks last warned threshold
- Each threshold fires once only
- Resets on decrease (after purge/compaction)

---

## 8. Pre-Compact Hook (`hooks/pre-compact.py`)

Fires before compaction to materialize pin directives and inject instructions.

### 8.1 Pin Materialization

1. Scan session for `ccm:pin` directives
2. Resolve targets (pin → specific tool_result lines)
3. Cache pinned content to CCM
4. Replace with CCM stubs in session
5. Write back modified session

### 8.2 Compaction Instructions

Priority:
1. Custom file: `~/.claude/compact-instructions.txt`
2. Default from config: `COMPACT_INSTRUCTIONS_SINGLE_PASS`

Instructions are output as the hook response, injected into the compaction prompt.

---

## 9. Launch Script (`hooks/c`)

Bash script that launches Claude Code with CCM integration.

### 9.1 Startup Sequence

1. Read settings from `config.py` (AUTOCOMPACT_THRESHOLD, THINKING_PROXY_ENABLED, THINKING_PROXY_PORT)
2. Trap `c d..` typo → `cd ..`
3. Find session ID (from args or auto-detect most recent for CWD)
4. If proxy enabled: ensure thinking proxy is running, set proxy env vars
5. Get patched CLI (create/update if needed)
6. Set environment variables (process-local only):
   - `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE={threshold}`
   - `ANTHROPIC_BASE_URL=http://127.0.0.1:{port}` (if proxy enabled)
   - `ANTHROPIC_CUSTOM_HEADERS=X-CCM-Session-ID:{session_id}` (if proxy enabled)
7. Execute with `--dangerously-skip-permissions` (if configured)

### 9.2 Configuration

Settings are read from `config.py` with env var overrides:

| Variable | Config Key | Default | Description |
|----------|-----------|---------|-------------|
| `COMPACT_PCT` | `AUTOCOMPACT_THRESHOLD` | 95 | Autocompact threshold |
| `THINKING_PROXY_PORT` | `THINKING_PROXY_PORT` | 8080 | Proxy port |
| `THINKING_PROXY_ENABLED` | `THINKING_PROXY_ENABLED` | True | Enable/disable proxy |
| `SKIP_PERMISSIONS` | — | true | Add --dangerously-skip-permissions |

---

## 10. Auto-Restart (`hooks/auto-restart.py`)

Background process that restarts Claude after purge.

### 10.1 Flow

1. Double fork to detach from terminal
2. Wait delay seconds (default: 3)
3. Get launch args from `CLAUDE_LAUNCH_ARGS` setting
4. Build resume command (using `c` function)
5. Kill Claude with SIGKILL (prevents overwriting purged session)
6. Copy resume command to clipboard (wl-copy > xclip > xsel)
7. Write notification to TTY

### 10.2 Resume Command

Uses `c` launcher with:
- Original flags preserved
- `--resume/-r` replaced with new session ID
- `--continue/-c` removed
- `--dangerously-skip-permissions` added by `c`

---

## 11. Cache Pruner (`hooks/claude-cache-prune.py`)

Manages CCM durable cache lifecycle.

### 11.1 Pruning Strategies

**By age:** Delete unpinned entries older than N days. Respects pin levels.

**By size:** Prune to stay under N megabytes. Deletes oldest unpinned first, then soft-pinned if needed. Never deletes hard-pinned.

**Garbage collection:** Scans all sessions for referenced keys, deletes unreferenced unpinned entries.

### 11.2 Pin Management

```bash
claude-cache-prune.py --pin KEY --level hard --reason "important"
claude-cache-prune.py --unpin KEY
claude-cache-prune.py --list-pins
```

### 11.3 Commands

```bash
claude-cache-prune.py --stats                    # Cache statistics
claude-cache-prune.py --max-age-days 30          # Prune old entries
claude-cache-prune.py --max-size-mb 500          # Prune to size
claude-cache-prune.py --gc-unreferenced          # Remove orphans
claude-cache-prune.py --dry-run                  # Preview only
```

---

## 12. Learning System

### 12.1 Learn Large Commands (`hooks/learn-large-commands.py`)

PostToolUse hook that records commands producing large output.

**Flow:**
1. Check if Bash tool with large output
2. Extract base command (first 3 words)
3. Skip common commands (ls, cat, git, make, etc.)
4. Create regex pattern
5. Store in patterns file with date and size

**Pattern format:**
```
# Learned 2025-01-24: kubectl get pods -A (12345 bytes)
(^|&&|;)\s*kubectl\ get\ pods
```

**Storage:**
- Global: `~/.claude/learned-patterns.txt`
- Project: `.claude/learned-patterns.txt`

**Expiry:** Patterns older than `PATTERNS_EXPIRY_DAYS` (30) are cleaned up.

### 12.2 Review Learned Commands (`hooks/review-learned-commands.py`)

Display utility for learned patterns.

```bash
review-learned-commands.py            # Global patterns
review-learned-commands.py --project  # Project patterns
```

---

## 13. CCM Get Tool (`hooks/ccm-get.py`)

Retrieves cached content by key.

```bash
ccm-get.py <key>          # Output content to stdout
ccm-get.py <key> --info   # Show metadata only
ccm-get.py --last         # Get most recent
ccm-get.py --list [-n N]  # List recent entries
ccm-get.py --stats        # Cache statistics
```

---

## 14. Setup Hook (`hooks/ccm-setup.py`)

Runs on session start (2.1.9+ setup hook support):
- Validates proxy is running
- Cleans expired cache entries
- Checks patch status
- Reports any issues

---

## 15. Utilities

### 15.1 X11 Type (`hooks/lib/x11_type.py`)

Keystroke injection via ctypes (no dependencies):
- Loads X11 and XTest libraries
- Character-to-keysym mapping
- Shift handling for uppercase/symbols
- Configurable inter-key delay (default: 0.01s)

Usage: `x11_type.py "text to type"` (3s delay before typing)

---

## 16. Configuration Reference

All settings in `~/.claude/hooks/config.py`:

### Cache Settings
| Setting | Default | Description |
|---------|---------|-------------|
| `CACHE_DIR` | `~/.claude/cache` | Cache storage location |
| `CACHE_MAX_AGE_MINUTES` | 60 | Legacy cache TTL |
| `BASH_THRESHOLD` | 8000 | Bash output caching threshold (bytes) |
| `GLOB_THRESHOLD` | 8000 | Glob output caching threshold (bytes) |
| `GREP_THRESHOLD` | 8000 | Grep output caching threshold (bytes) |
| `READ_THRESHOLD` | 25000 | File read caching threshold (bytes) |
| `PATTERNS_EXPIRY_DAYS` | 30 | Learned pattern retention |
| `METRICS_ENABLED` | False | Enable metrics logging |

### Context Monitor
| Setting | Default | Description |
|---------|---------|-------------|
| `CONTEXT_MONITOR_ENABLED` | True | Enable warnings |
| `CONTEXT_MAX_TOKENS` | 200000 | Claude's context window |
| `CONTEXT_WARN_THRESHOLDS` | [70, 80, 90] | Warning percentages |
| `CONTEXT_CHARS_PER_TOKEN` | 2.5 | Fallback estimation |
| `CONTEXT_OVERHEAD_TOKENS` | 45000 | System prompt + tools overhead (no proxy) |
| `CONTEXT_OVERHEAD_TOKENS_PROXIED` | 10000 | Overhead with proxy (abbreviated prompt/tools) |
| `CONTEXT_MESSAGE_MULTIPLIER` | 1.5 | Metadata overhead multiplier |

### Auto-Compaction
| Setting | Default | Description |
|---------|---------|-------------|
| `AUTOCOMPACT_ENABLED` | True | Enable threshold override |
| `AUTOCOMPACT_THRESHOLD` | 95 | Compaction trigger percentage |

### Pre-Compact
| Setting | Default | Description |
|---------|---------|-------------|
| `PRE_COMPACT_ENABLED` | True | Enable pre-compact hook |
| `COMPACT_INSTRUCTIONS_SINGLE_PASS` | (long) | Distillation prompt |
| `COMPACT_INSTRUCTIONS` | = SINGLE_PASS | Alias (or file override from `~/.claude/compact-instructions.txt`) |

### CCM Cache
| Setting | Default | Description |
|---------|---------|-------------|
| `CCM_ENABLED` | True | Enable durable cache |
| `CCM_COMPRESSION` | 'auto' | Compression (auto/zstd/gzip/none) |
| `CCM_DEFAULT_PIN_LEVEL` | 'soft' | Default pin level |
| `CCM_PRUNE_MAX_AGE_DAYS` | 30 | Unpinned item expiry |
| `CCM_PRUNE_MAX_SIZE_MB` | 500 | Maximum cache size |
| `CCM_STUB_THRESHOLD_BYTES` | 5000 | Stub creation threshold |
| `CCM_RECENT_LINES_WINDOW` | 20 | Recent content window |

### Thinking Proxy
| Setting | Default | Description |
|---------|---------|-------------|
| `THINKING_PROXY_ENABLED` | True | Enable proxy |
| `THINKING_PROXY_PORT` | 8080 | Listen port |
| `THINKING_PROXY_DEBUG_LOG` | False | Debug logging |
| `PRESERVED_SKILLS` | ['relay', 'ccm'] | Skills to keep in abbreviated prompt |

### External Compaction
| Setting | Default | Description |
|---------|---------|-------------|
| `EXTERNAL_COMPACTION_ENABLED` | True | Route to external LLM |
| `OPENROUTER_API_KEY` | (from credentials) | API key |
| `OPENROUTER_API_BASE` | openrouter.ai/api/v1 | API endpoint |
| `COMPACTION_MODELS` | {early/late: gemini-3-flash} | Model selection |
| `COMPACTION_MAX_TOKENS` | {1:20k...5+:64k} | Progressive limits |
| `COMPACTION_PRESERVE_TOKENS` | 10000 | Recent message preservation |

---

## 17. State Files Reference

### Runtime State
| Path | Purpose |
|------|---------|
| `~/.claude/proxy.pid` | Thinking proxy daemon PID |
| `~/.claude/proxy.log` | Proxy main log |
| `~/.claude/proxy-debug.log` | Proxy debug log (if enabled) |
| `~/.claude/proxy-state/no-thinking/{session_id}` | No-thinking mode flag |
| `~/.claude/state/{session_id}-context-level` | Last context warning level |

### Cache Storage
| Path | Purpose |
|------|---------|
| `~/.claude/cache/ccm/blobs/{hash}.zst` | Compressed content |
| `~/.claude/cache/ccm/meta/{hash}.json` | Metadata sidecar |
| `~/.claude/cache/ccm/index.jsonl` | Append-only log |
| `~/.claude/cache/ccm/last_key` | Most recent cache key |

### Patching
| Path | Purpose |
|------|---------|
| `~/.claude/patch-cache/autocompact-patch.json` | Patch status by hash |
| `~/.claude/patched/claude-code/` | Patched CLI mirror |

### Learning
| Path | Purpose |
|------|---------|
| `~/.claude/hooks/command-cache.json` | Haiku classification cache |
| `~/.claude/learned-patterns.txt` | Global learned patterns |
| `.claude/learned-patterns.txt` | Project learned patterns |

### Debug (Compaction)
| Path | Purpose |
|------|---------|
| `~/.claude/last-compaction-request.json` | Full request to OpenRouter |
| `~/.claude/last-artefacts.txt` | Extracted artefacts |
| `~/.claude/last-distillation.txt` | Final distillation output |

### Configuration
| Path | Purpose |
|------|---------|
| `~/.claude/hooks/config.py` | All settings |
| `~/.claude/compact-instructions.txt` | Custom compaction instructions |
| `~/.claude/credentials.json` | API keys (OpenRouter) |
| `~/.claude/settings.json` | Claude settings (CLAUDE_LAUNCH_ARGS) |
| `~/.claude/hooks/metrics.log` | Metrics (if enabled) |
| `~/.claude/context-monitor.log` | Context monitor debug log |

---

## 18. Key Design Decisions

### "None - " Prefix Convention

The CLI always prepends `"Error: "` to hook blocking responses. This misleads the model into treating successful operations as failures. The fix:
- `json_block()` accepts optional `exit_code`
- When `exit_code=0`, prepends `"None - "` to the reason
- Result: `Error: None - Exit 0: ...` — model recognizes non-error
- Non-zero exit codes: no prefix → `Error: Exit 1: ...` — genuine failure
- For grep/glob, exit code 1 (no matches) is normalized to 0 (success)
- This is CLI-version-independent (no binary patching required)
- Supersedes the former Patch 5 (is_error) which modified CLI display

### Single Source of Truth

Configuration flows from `config.py`:
- `c` script reads Python config at startup (with env var overrides)
- Context monitor reads config and adapts to proxy presence
- No hardcoded defaults that diverge from config

### Subagent Bypass

Task agents bypass all interception. This is fundamental to the architecture:
- Main agent sees stubs → minimal context usage
- Subagents see full content → can extract/summarize
- Only the extracted result enters main context

### External Compaction vs /purge

- **External compaction** — Automatic, uses cheaper models, preserves artefacts, progressive token limits
- **/purge** — Manual, caches old outputs, repairs structure, requires restart
- Both can coexist; external compaction reduces need for manual purge

### Persistent Patched CLI

The patcher creates a separate copy rather than patching in-place because:
- Auto-updates would overwrite in-place patches
- Separate location survives updates
- Re-patches automatically when hash changes
- But must mirror directory structure for Node.js module resolution

### Content-Addressable Cache

SHA256 deduplication means:
- Same content cached once regardless of source
- Multiple sessions can reference same blob
- Safe concurrent access
- No naming conflicts
