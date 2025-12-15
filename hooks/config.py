#!/usr/bin/env python3
"""
Configuration for Claude Code hooks.
Edit these values to tune hook behavior.
"""

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
CONTEXT_CHARS_PER_TOKEN = 4    # Fallback when tiktoken not installed
CONTEXT_OVERHEAD_TOKENS = 19500  # System prompt + tools + memory

# Accuracy notes:
# - Install tiktoken for accurate counting: pip install tiktoken
# - Without tiktoken, uses CHARS_PER_TOKEN estimate (~4 chars/token average)
# - OVERHEAD_TOKENS: system prompt (~3k) + tools (~15k) + memory (~1.5k)
#   Adjust if you have many MCP servers or custom tools
# - Thinking blocks are excluded (only current turn's thinking is in context)
