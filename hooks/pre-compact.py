#!/usr/bin/env python3
"""
PreCompact hook - outputs custom instructions for context compaction.

When compaction is triggered, this hook provides instructions to guide
what should be preserved vs summarized. Instructions can be customized
by editing ~/.claude/compact-instructions.txt.

Configuration: Edit config.py to adjust default instructions or disable.
"""

import sys
from pathlib import Path

# Load configuration
HOOKS_DIR = Path(__file__).parent
sys.path.insert(0, str(HOOKS_DIR))

# Default compaction instructions
DEFAULT_COMPACT_INSTRUCTIONS = """Focus on preserving:
- Current task context and objectives
- Key decisions made and their rationale
- Important file paths and code locations discovered
- Any pending actions or TODOs
- Error messages and debugging context being investigated
- Critical state (connections, configurations, credentials referenced)

Summarize completed work concisely. Prioritize actionable context over historical details.
Maintain enough context to continue the current task without re-reading files."""

# Try to load from config
COMPACT_INSTRUCTIONS = DEFAULT_COMPACT_INSTRUCTIONS
PRE_COMPACT_ENABLED = True

CONFIG_FILE = HOOKS_DIR / 'config.py'
if CONFIG_FILE.exists():
    _config = {}
    try:
        exec(CONFIG_FILE.read_text(), _config)
        if 'COMPACT_INSTRUCTIONS' in _config:
            COMPACT_INSTRUCTIONS = _config['COMPACT_INSTRUCTIONS']
        if 'PRE_COMPACT_ENABLED' in _config:
            PRE_COMPACT_ENABLED = _config['PRE_COMPACT_ENABLED']
    except Exception:
        pass


def main():
    if not PRE_COMPACT_ENABLED:
        return

    # Check for custom instructions file
    custom_file = Path.home() / '.claude' / 'compact-instructions.txt'

    if custom_file.exists():
        try:
            instructions = custom_file.read_text().strip()
            if instructions:
                print(instructions)
                return
        except Exception:
            pass

    # Fall back to configured/default instructions
    print(COMPACT_INSTRUCTIONS)


if __name__ == '__main__':
    main()
