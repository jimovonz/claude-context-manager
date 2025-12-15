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

# Default compaction instructions (used if ~/.claude/compact-instructions.txt doesn't exist)
COMPACT_INSTRUCTIONS = """Focus on preserving:
- Current task context and objectives
- Key decisions made and their rationale
- Important file paths and code locations discovered
- Any pending actions or TODOs
- Error messages and debugging context being investigated
- Critical state (connections, configurations, credentials referenced)

Summarize completed work concisely. Prioritize actionable context over historical details.
Maintain enough context to continue the current task without re-reading files."""

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
