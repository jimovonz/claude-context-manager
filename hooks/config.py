#!/usr/bin/env python3
"""
Configuration for Claude Code hooks.
Edit these values to tune hook behavior.
"""

__version__ = "1.0.0"

from pathlib import Path

# Cache settings
CACHE_DIR = Path.home() / '.claude' / 'cache'
CACHE_MAX_AGE_MINUTES = 60

# Output size thresholds (bytes) - outputs larger than this get cached
BASH_THRESHOLD = 2000
GLOB_THRESHOLD = 2000
GREP_THRESHOLD = 2000
READ_THRESHOLD = 25000  # ~6k tokens

# Learned patterns settings
PATTERNS_EXPIRY_DAYS = 30

# Metrics logging (set to True to enable)
METRICS_ENABLED = False

# =============================================================================
# Context Monitor Settings
# =============================================================================

# Enable/disable context usage warnings
CONTEXT_MONITOR_ENABLED = True

# Claude's context window size (tokens)
CONTEXT_MAX_TOKENS = 200000

# Warn at these percentage thresholds (only warns once per threshold per session)
CONTEXT_WARN_THRESHOLDS = [70, 80, 90]

# Estimation parameters
CONTEXT_CHARS_PER_TOKEN = 2.5  # Fallback when tiktoken not installed (empirically ~2.4)
CONTEXT_OVERHEAD_TOKENS = 45000  # Visible (~20k) + hidden Claude overhead (~25k)
CONTEXT_MESSAGE_MULTIPLIER = 1.5  # Claude counts more than extracted text (structure, metadata)

# Accuracy notes:
# - Install tiktoken for accurate counting: pip install tiktoken
# - Without tiktoken, uses CHARS_PER_TOKEN estimate (~4 chars/token average)
# - OVERHEAD_TOKENS: system prompt (~3k) + tools (~15k) + memory (~1.5k)
#   Adjust if you have many MCP servers or custom tools
# - Thinking blocks are excluded (only current turn's thinking is in context)

# =============================================================================
# Auto-Compaction Settings
# =============================================================================

# Enable auto-compact threshold override (set via CLAUDE_AUTOCOMPACT_PCT_OVERRIDE env var)
AUTOCOMPACT_ENABLED = True

# Default threshold (percent): triggers compaction at this % of max context
AUTOCOMPACT_THRESHOLD = 80

# =============================================================================
# Pre-Compact Hook Settings
# =============================================================================

# Enable/disable PreCompact hook
PRE_COMPACT_ENABLED = True

# Two-pass distillation prompts
# Pass 1: Extract/update execution artefacts (delta mode)
# Pass 2: Generate full distillation using Pass 1 artefacts

COMPACT_INSTRUCTIONS_PASS1 = """ARTEFACT EXTRACTION (Pass 1)

Extract all execution-critical artefacts from this conversation.
Output ONLY artefacts, no narrative, no summary.

VERBATIM ZONES (mandatory):
For any shell command, build invocation, error message, stack trace, config
line, or path: copy it VERBATIM (no edits, no reordering flags, no cleanup).
Use code fences for commands and errors.

OUTPUT STRUCTURE:
```
REPO ROOTS:
- /path/to/repo — purpose tag (3-8 words)

KEY FILES:
- /path/to/file.py — purpose tag

ENTRY POINTS:
- script.sh — purpose tag
- service:port — purpose tag

COMMANDS:
build:
  ```
  exact command here
  ```
test:
  ```
  exact command here
  ```
run:
  ```
  exact command here
  ```

ACCESS:
local:
  - host/method — purpose
dev:
  - host/method — purpose

ERRORS (verbatim):
  ```
  exact error text
  ```
```

RULES:
- Each artefact gets a 3-8 word purpose tag
- Merge duplicates: one canonical, variants as sub-bullets
- If two items are similar but DISTINCT, keep them separate
- Commands/errors MUST be in code fences, verbatim
- Do NOT generalise, normalise, or "clean up" anything

DELTA MODE (if PREVIOUS ARTEFACTS provided below):
Output only: (a) NEW, (b) REMOVED, (c) CHANGED
For unchanged items: "STABLE: [brief list]"
If nothing changed: "NO CHANGE"

PREVIOUS ARTEFACTS:
{previous_artefacts}

---
CONVERSATION TO EXTRACT FROM:
"""

COMPACT_INSTRUCTIONS_PASS2 = """DISTILLATION (Pass 2)

Generate a context distillation for agent continuity.
You are given ARTEFACTS (already extracted) and the conversation.

This is NOT a summary. This is execution-critical state preservation.

ACTIVE THREAD SELECTION:
- ONE primary objective (current focus)
- Up to TWO secondary threads (if actively relevant)
- Everything else → DEAD ENDS

OUTPUT STRUCTURE (truncation-resilient order):
1. CURRENT OBJECTIVE
   Primary: [one sentence]
   Secondary: [optional, max 2]

2. OPEN TASKS / TODOs
   - [ ] task with enough context to execute
   - [ ] next task

3. EXECUTION ARTEFACTS
   [Insert Pass 1 artefacts here]

4. DECISIONS & CONSTRAINTS
   - Decision made — why, what was rejected

5. CURRENT STATE
   - What's done vs in-progress

6. ERRORS / DIAGNOSTICS (if any)
   [verbatim in code fences]

7. DEAD ENDS
   - Parked thread — one line why

SELF-AUDIT (append at end):
```
CHECKS: commands[Y/N] paths[Y/N] errors-quoted[Y/N] TODOs[Y/N]
```

BUDGET ENFORCEMENT:
If over budget:
1. Remove narration first
2. Compress DEAD ENDS to one-liners
3. NEVER remove: artefacts, constraints, TODOs, verbatim errors

CRITICAL:
If truncated, sections 1-3 (objective, TODOs, artefacts) MUST appear first.

Target output size: <N tokens.

ARTEFACTS FROM PASS 1:
{pass1_artefacts}

---
CONVERSATION:
"""

# Legacy single-pass (deprecated, kept for compatibility)
COMPACT_INSTRUCTIONS = COMPACT_INSTRUCTIONS_PASS2.replace("{pass1_artefacts}", "[Extract inline]").replace("{previous_artefacts}", "None")

# File-based override
_COMPACT_INSTRUCTIONS_FILE = Path.home() / '.claude' / 'compact-instructions.txt'

def _load_compact_instructions():
    """Load from file if exists, else use default."""
    if _COMPACT_INSTRUCTIONS_FILE.exists():
        return _COMPACT_INSTRUCTIONS_FILE.read_text().strip()
    return COMPACT_INSTRUCTIONS

# Re-export for imports expecting single COMPACT_INSTRUCTIONS
COMPACT_INSTRUCTIONS = _load_compact_instructions()

# Also export pass-specific instructions
__all__ = ['COMPACT_INSTRUCTIONS', 'COMPACT_INSTRUCTIONS_PASS1', 'COMPACT_INSTRUCTIONS_PASS2']


# =============================================================================
# CCM (Content Cache Manager) Settings
# =============================================================================

# Enable CCM durable cache (SHA256-based, compressed, with pinning)
CCM_ENABLED = True

# Compression method: 'auto' (zstd > gzip > none), 'zstd', 'gzip', or 'none'
CCM_COMPRESSION = 'auto'

# Default pin level for content cached via pin directives
CCM_DEFAULT_PIN_LEVEL = 'soft'

# Cache pruning defaults
CCM_PRUNE_MAX_AGE_DAYS = 30      # Delete unpinned items older than this
CCM_PRUNE_MAX_SIZE_MB = 500      # Max total cache size

# Stub threshold: tool_results larger than this get stubbed during purge
CCM_STUB_THRESHOLD_BYTES = 5000

# Recent lines window: tool_results within this many lines of end are kept
CCM_RECENT_LINES_WINDOW = 20

# =============================================================================
# Thinking Proxy Settings
# =============================================================================

# Enable thinking proxy (requires ANTHROPIC_BASE_URL to be set)
THINKING_PROXY_ENABLED = True

# Port for the proxy to listen on
THINKING_PROXY_PORT = 8080

# Enable debug logging (writes detailed request/response info to proxy-debug.log)
THINKING_PROXY_DEBUG_LOG = False

# =============================================================================
# External Compaction Settings
# =============================================================================

import os
import json

# Enable external compaction routing (routes /compact to external LLM)
EXTERNAL_COMPACTION_ENABLED = True

# OpenRouter API key (from credentials file or environment)
def _load_openrouter_key():
    """Load OpenRouter API key from credentials file or environment."""
    # Try credentials file first
    creds_file = Path.home() / '.claude' / 'credentials.json'
    if creds_file.exists():
        try:
            creds = json.loads(creds_file.read_text())
            key = creds.get('openrouter', {}).get('api_key')
            if key:
                return key
        except (json.JSONDecodeError, KeyError):
            pass
    # Fall back to environment variable
    return os.environ.get('OPENROUTER_API_KEY')

OPENROUTER_API_KEY = _load_openrouter_key()

# OpenRouter API base URL
OPENROUTER_API_BASE = 'https://openrouter.ai/api/v1'

# Model selection by compaction number (OpenRouter model IDs)
# Early compactions (1-5): cheaper model with generous output
# Late compactions (6+): more capable model for dense content
COMPACTION_MODELS = {
    'early': 'x-ai/grok-4.1-fast',   # Compactions 1-5
    'late': 'x-ai/grok-4.1-fast',    # Compactions 6+
}

# Output token limits per compaction number (tight early, generous late)
# Early: content is verbose, easy to compress
# Late: content is dense, needs more tokens to preserve
COMPACTION_MAX_TOKENS = {
    1: 20000,
    2: 36000,
    3: 52000,
    4: 68000,
    5: 84000,
    # Later compactions: content is very dense, need maximum budget
    'default': 100000
}
