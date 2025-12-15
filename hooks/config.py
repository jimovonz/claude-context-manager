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
CONTEXT_WARN_THRESHOLDS = [50, 70, 80, 90]

# Estimation parameters (see notes below)
CONTEXT_CHARS_PER_TOKEN = 4
CONTEXT_OVERHEAD_TOKENS = 19500

# Notes on estimation accuracy:
# - CHARS_PER_TOKEN: ~4 is reasonable average for Claude's tokenizer
#   (code tends to be ~3, prose ~4-5, varies by language)
# - OVERHEAD_TOKENS: system prompt (~3k) + tools (~15k) + memory (~1.5k)
#   This varies based on enabled tools and MCP servers
# - The estimate is intentionally conservative - better to warn early
# - For precise measurement, use /context command in Claude Code
