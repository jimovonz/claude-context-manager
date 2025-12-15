#!/usr/bin/env python3
"""
Quickly disable all Claude Context Manager hooks without uninstalling.

This comments out hook registrations in settings.json, leaving files intact.
Use enable.py to re-enable.
"""

import json
import sys
from pathlib import Path

SETTINGS_FILE = Path.home() / '.claude' / 'settings.json'
BACKUP_FILE = Path.home() / '.claude' / 'settings.json.hooks-backup'

# Markers for our hooks
OUR_HOOK_PATHS = [
    '~/.claude/hooks/context-monitor.py',
    '~/.claude/hooks/intercept-bash.py',
    '~/.claude/hooks/intercept-glob.py',
    '~/.claude/hooks/intercept-grep.py',
    '~/.claude/hooks/intercept-read.py',
    '~/.claude/hooks/learn-large-commands.py',
]


def is_our_hook(hook_entry: dict) -> bool:
    """Check if a hook entry belongs to us."""
    for hook in hook_entry.get('hooks', []):
        cmd = hook.get('command', '')
        if any(our_path in cmd for our_path in OUR_HOOK_PATHS):
            return True
    return False


def disable():
    """Disable hooks by removing them from settings.json."""
    if not SETTINGS_FILE.exists():
        print("No settings.json found - nothing to disable")
        return

    try:
        settings = json.loads(SETTINGS_FILE.read_text())
    except json.JSONDecodeError:
        print("Error: settings.json is invalid", file=sys.stderr)
        sys.exit(1)

    if 'hooks' not in settings:
        print("No hooks configured - nothing to disable")
        return

    # Backup current hooks config
    hooks_backup = settings.get('hooks', {})
    BACKUP_FILE.write_text(json.dumps(hooks_backup, indent=2))

    # Remove our hooks from each event
    modified = False
    for event in list(settings['hooks'].keys()):
        original_len = len(settings['hooks'][event])
        settings['hooks'][event] = [
            h for h in settings['hooks'][event] if not is_our_hook(h)
        ]

        if len(settings['hooks'][event]) < original_len:
            modified = True
            removed = original_len - len(settings['hooks'][event])
            print(f"Disabled {removed} hook(s) from {event}")

        # Remove empty event arrays
        if not settings['hooks'][event]:
            del settings['hooks'][event]

    # Remove empty hooks object
    if not settings['hooks']:
        del settings['hooks']

    if modified:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + '\n')
        print(f"\nHooks disabled. Backup saved to: {BACKUP_FILE}")
        print("Run 'python3 enable.py' to re-enable")
    else:
        print("No Context Manager hooks found to disable")


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        sys.exit(0)
    disable()
