#!/usr/bin/env python3
"""
Claude Context Manager - Uninstaller

Removes hooks and commands, cleans up settings.json.
"""

import json
import shutil
import sys
from pathlib import Path

CLAUDE_DIR = Path.home() / '.claude'
HOOKS_DIR = CLAUDE_DIR / 'hooks'
COMMANDS_DIR = CLAUDE_DIR / 'commands'
SETTINGS_FILE = CLAUDE_DIR / 'settings.json'

# Files installed by this package
HOOK_FILES = [
    'config.py',
    'context-monitor.py',
    'intercept-bash.py',
    'intercept-glob.py',
    'intercept-grep.py',
    'intercept-read.py',
    'learn-large-commands.py',
    'review-learned-commands.py',
    'claude-session-purge.py',
    'pre-compact.py',
    'CONTEXT_MANAGEMENT.md',
    'lib/__init__.py',
    'lib/common.py',
]

COMMAND_FILES = [
    'purge.md',
]

# Config files in ~/.claude (not in hooks/)
CONFIG_FILES = [
    'setup.sh',
    'compact-instructions.txt',
]

# Hook matchers to remove from settings.json
HOOK_MATCHERS = {
    'UserPromptSubmit': [''],
    'PreToolUse': ['Bash', 'Glob', 'Grep', 'Read'],
    'PostToolUse': ['Bash'],
    'PreCompact': [''],
}


def remove_files(base_dir: Path, files: list, description: str) -> int:
    """Remove listed files, return count removed."""
    count = 0
    for rel_path in files:
        file_path = base_dir / rel_path
        if file_path.exists():
            file_path.unlink()
            print(f"  Removed: {rel_path}")
            count += 1

    # Clean up empty directories
    for rel_path in files:
        dir_path = (base_dir / rel_path).parent
        if dir_path != base_dir and dir_path.exists():
            try:
                dir_path.rmdir()  # Only removes if empty
                print(f"  Removed empty dir: {dir_path.relative_to(base_dir)}")
            except OSError:
                pass  # Directory not empty

    return count


def clean_settings():
    """Remove our hooks and env vars from settings.json."""
    if not SETTINGS_FILE.exists():
        return

    try:
        settings = json.loads(SETTINGS_FILE.read_text())
    except json.JSONDecodeError:
        print("  Warning: settings.json is invalid, skipping")
        return

    modified = False

    # Remove env vars we added
    env = settings.get('env', {})
    if 'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE' in env:
        del env['CLAUDE_AUTOCOMPACT_PCT_OVERRIDE']
        modified = True
        print("  Removed CLAUDE_AUTOCOMPACT_PCT_OVERRIDE env var")

    # Remove empty env object
    if not env and 'env' in settings:
        del settings['env']

    # Remove hooks
    hooks = settings.get('hooks', {})

    for event, matchers in HOOK_MATCHERS.items():
        if event not in hooks:
            continue

        original_len = len(hooks[event])
        hooks[event] = [
            h for h in hooks[event]
            if not (isinstance(h, dict) and h.get('matcher') in matchers and
                    any('~/.claude/hooks/' in str(hook.get('command', ''))
                        for hook in h.get('hooks', [])))
        ]

        if len(hooks[event]) < original_len:
            modified = True
            print(f"  Removed {event} hooks")

        # Remove empty event arrays
        if not hooks[event]:
            del hooks[event]

    # Remove empty hooks object
    if not hooks and 'hooks' in settings:
        del settings['hooks']

    if modified:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + '\n')


def clean_claude_md():
    """Remove our section from CLAUDE.md, delete file if empty."""
    import re

    claude_md = CLAUDE_DIR / 'CLAUDE.md'
    if not claude_md.exists():
        return

    content = claude_md.read_text()

    # Check if our section exists
    if '<!-- CONTEXT-MANAGER-START -->' not in content:
        print("  CLAUDE.md (no section to remove)")
        return

    # Remove our section (including surrounding whitespace)
    pattern = r'\n*<!-- CONTEXT-MANAGER-START -->.*?<!-- CONTEXT-MANAGER-END -->\n*'
    new_content = re.sub(pattern, '', content, flags=re.DOTALL)
    new_content = new_content.strip()

    if new_content:
        # Other content remains, keep the file
        claude_md.write_text(new_content + '\n')
        print("  CLAUDE.md (removed section, kept user content)")
    else:
        # File is empty, remove it
        claude_md.unlink()
        print("  CLAUDE.md (removed empty file)")


def uninstall():
    """Uninstall hooks and commands."""
    print("Claude Context Manager - Uninstalling\n")

    # Remove hook files
    if HOOKS_DIR.exists():
        print("Removing hooks:")
        count = remove_files(HOOKS_DIR, HOOK_FILES, "hooks")
        if count == 0:
            print("  No hook files found")
        print()

    # Remove command files
    if COMMANDS_DIR.exists():
        print("Removing commands:")
        count = remove_files(COMMANDS_DIR, COMMAND_FILES, "commands")
        if count == 0:
            print("  No command files found")
        print()

    # Remove config files from ~/.claude
    print("Removing configuration files:")
    config_count = 0
    for config_file in CONFIG_FILES:
        config_path = CLAUDE_DIR / config_file
        if config_path.exists():
            config_path.unlink()
            print(f"  Removed: {config_file}")
            config_count += 1
    if config_count == 0:
        print("  No config files found")
    clean_claude_md()
    print()

    # Clean settings.json
    print("Cleaning settings.json:")
    clean_settings()
    print()

    # Note about cache and state
    cache_dir = CLAUDE_DIR / 'cache'
    state_dir = CLAUDE_DIR / 'state'
    patterns_file = CLAUDE_DIR / 'learned-patterns.txt'

    remaining = []
    if cache_dir.exists() and any(cache_dir.iterdir()):
        remaining.append(f"  {cache_dir}/ (cached outputs)")
    if state_dir.exists() and any(state_dir.iterdir()):
        remaining.append(f"  {state_dir}/ (context monitor state)")
    if patterns_file.exists():
        remaining.append(f"  {patterns_file} (learned patterns)")

    if remaining:
        print("Optional cleanup (data files, not removed automatically):")
        for item in remaining:
            print(item)
        print()

    print("=" * 50)
    print("Uninstallation complete!")


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        print("Usage: python3 uninstall.py")
        sys.exit(0)

    uninstall()


if __name__ == '__main__':
    main()
