#!/usr/bin/env python3
"""
Re-enable Claude Context Manager hooks after disabling.

Restores hook registrations from backup or re-runs install.
"""

import json
import sys
from pathlib import Path

SETTINGS_FILE = Path.home() / '.claude' / 'settings.json'
BACKUP_FILE = Path.home() / '.claude' / 'settings.json.hooks-backup'

# Default hook config (same as install.py)
HOOK_CONFIG = {
    "hooks": {
        "UserPromptSubmit": [
            {
                "matcher": "",
                "hooks": [{"type": "command", "command": "~/.claude/hooks/context-monitor.py"}]
            }
        ],
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "~/.claude/hooks/intercept-bash.py"}]
            },
            {
                "matcher": "Glob",
                "hooks": [{"type": "command", "command": "~/.claude/hooks/intercept-glob.py"}]
            },
            {
                "matcher": "Grep",
                "hooks": [{"type": "command", "command": "~/.claude/hooks/intercept-grep.py"}]
            },
            {
                "matcher": "Read",
                "hooks": [{"type": "command", "command": "~/.claude/hooks/intercept-read.py"}]
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "~/.claude/hooks/learn-large-commands.py"}]
            }
        ],
        "PreCompact": [
            {
                "matcher": "",
                "hooks": [{"type": "command", "command": "~/.claude/hooks/pre-compact.py"}]
            }
        ]
    }
}


def merge_hooks(existing: dict, new_hooks: dict) -> dict:
    """Merge hook configurations."""
    result = existing.copy()

    if 'hooks' not in result:
        result['hooks'] = {}

    for event, hooks in new_hooks.items():
        if event not in result['hooks']:
            result['hooks'][event] = []

        # Add hooks that don't already exist
        existing_matchers = {h.get('matcher') for h in result['hooks'][event]}
        for hook in hooks:
            if hook.get('matcher') not in existing_matchers:
                result['hooks'][event].append(hook)

    return result


def enable():
    """Re-enable hooks from backup or defaults."""
    # Try to load existing settings
    if SETTINGS_FILE.exists():
        try:
            settings = json.loads(SETTINGS_FILE.read_text())
        except json.JSONDecodeError:
            print("Warning: settings.json invalid, creating new", file=sys.stderr)
            settings = {}
    else:
        settings = {}

    # Try to restore from backup first
    if BACKUP_FILE.exists():
        try:
            backup_hooks = json.loads(BACKUP_FILE.read_text())
            settings['hooks'] = backup_hooks
            SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + '\n')
            print("Hooks restored from backup")
            BACKUP_FILE.unlink()
            return
        except (json.JSONDecodeError, KeyError):
            print("Backup invalid, using defaults")

    # Fall back to default config
    settings = merge_hooks(settings, HOOK_CONFIG['hooks'])
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + '\n')
    print("Hooks enabled with default configuration")


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        sys.exit(0)
    enable()
