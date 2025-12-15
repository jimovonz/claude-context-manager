#!/usr/bin/env python3
"""
Claude Context Manager - Installer

Installs hooks and commands to ~/.claude/ and configures settings.json.
"""

import json
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
CLAUDE_DIR = Path.home() / '.claude'
HOOKS_DIR = CLAUDE_DIR / 'hooks'
COMMANDS_DIR = CLAUDE_DIR / 'commands'
SETTINGS_FILE = CLAUDE_DIR / 'settings.json'

# Default autocompact threshold in percent (can be changed in config.py)
DEFAULT_AUTOCOMPACT_THRESHOLD = "80"

# Hook configurations to merge into settings.json
HOOK_CONFIG = {
    "env": {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": DEFAULT_AUTOCOMPACT_THRESHOLD
    },
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


def copy_directory(src: Path, dst: Path, description: str) -> int:
    """Copy directory contents, return count of files copied."""
    count = 0
    dst.mkdir(parents=True, exist_ok=True)

    for item in src.rglob('*'):
        if item.is_file() and '__pycache__' not in str(item):
            rel_path = item.relative_to(src)
            dest_path = dst / rel_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest_path)
            count += 1
            print(f"  {rel_path}")

    return count


def merge_settings(existing: dict, new: dict) -> dict:
    """Deep merge new settings into existing, preserving existing values."""
    result = existing.copy()

    for key, value in new.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_settings(result[key], value)
        elif key in result and isinstance(result[key], list) and isinstance(value, list):
            # For lists (like hook arrays), append new items that don't exist
            existing_matchers = {item.get('matcher') for item in result[key] if isinstance(item, dict)}
            for item in value:
                if isinstance(item, dict):
                    if item.get('matcher') not in existing_matchers:
                        result[key].append(item)
                else:
                    if item not in result[key]:
                        result[key].append(item)
        else:
            result[key] = value

    return result


def install():
    """Install hooks and commands."""
    print("Claude Context Manager - Installing\n")

    # Check source directories exist
    hooks_src = SCRIPT_DIR / 'hooks'
    commands_src = SCRIPT_DIR / 'commands'

    if not hooks_src.exists():
        print(f"Error: hooks/ directory not found in {SCRIPT_DIR}", file=sys.stderr)
        sys.exit(1)

    # Create ~/.claude if needed
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)

    # Copy hooks
    print("Installing hooks:")
    hook_count = copy_directory(hooks_src, HOOKS_DIR, "hooks")
    print(f"  ({hook_count} files)\n")

    # Copy commands
    if commands_src.exists():
        print("Installing commands:")
        cmd_count = copy_directory(commands_src, COMMANDS_DIR, "commands")
        print(f"  ({cmd_count} files)\n")

    # Copy setup.sh, compact-instructions.txt, and CLAUDE.md
    print("Installing configuration files:")
    setup_src = SCRIPT_DIR / 'setup.sh'
    compact_src = SCRIPT_DIR / 'compact-instructions.txt'
    claude_md_src = SCRIPT_DIR / 'CLAUDE.md'

    if setup_src.exists():
        shutil.copy2(setup_src, CLAUDE_DIR / 'setup.sh')
        (CLAUDE_DIR / 'setup.sh').chmod(0o755)
        print("  setup.sh")

    if compact_src.exists():
        # Only copy if doesn't exist (preserve user customizations)
        compact_dst = CLAUDE_DIR / 'compact-instructions.txt'
        if not compact_dst.exists():
            shutil.copy2(compact_src, compact_dst)
            print("  compact-instructions.txt")
        else:
            print("  compact-instructions.txt (kept existing)")

    if claude_md_src.exists():
        claude_md_dst = CLAUDE_DIR / 'CLAUDE.md'
        our_content = claude_md_src.read_text()

        if claude_md_dst.exists():
            existing = claude_md_dst.read_text()
            # Check if our section already exists
            if '<!-- CONTEXT-MANAGER-START -->' in existing:
                print("  CLAUDE.md (section already present)")
            else:
                # Append our section
                with open(claude_md_dst, 'a') as f:
                    f.write('\n\n' + our_content)
                print("  CLAUDE.md (appended section)")
        else:
            # Create new file
            claude_md_dst.write_text(our_content)
            print("  CLAUDE.md (created)")
    print()

    # Make scripts executable
    print("Setting permissions...")
    for py_file in HOOKS_DIR.rglob('*.py'):
        py_file.chmod(0o755)
    print()

    # Create CCM cache directory structure
    print("Initializing CCM cache...")
    ccm_dir = CLAUDE_DIR / 'cache' / 'ccm'
    (ccm_dir / 'blobs').mkdir(parents=True, exist_ok=True)
    (ccm_dir / 'meta').mkdir(parents=True, exist_ok=True)
    print("  ~/.claude/cache/ccm/blobs/")
    print("  ~/.claude/cache/ccm/meta/")
    print()

    # Update settings.json
    print("Configuring settings.json...")
    if SETTINGS_FILE.exists():
        try:
            existing = json.loads(SETTINGS_FILE.read_text())
            print("  Merging with existing settings")
        except json.JSONDecodeError:
            print("  Warning: existing settings.json is invalid, backing up")
            shutil.copy2(SETTINGS_FILE, SETTINGS_FILE.with_suffix('.json.bak'))
            existing = {}
    else:
        existing = {}

    merged = merge_settings(existing, HOOK_CONFIG)
    SETTINGS_FILE.write_text(json.dumps(merged, indent=2) + '\n')
    print("  Done\n")

    print("=" * 50)
    print("Installation complete!")
    print()
    print("Hooks will activate on next Claude Code session.")
    print()
    print("Quick start:")
    print("  source ~/.claude/setup.sh  # Enable 'c' alias")
    print("  c                          # Launch with --dangerously-skip-permissions")
    print()
    print("Configuration:")
    print("  ~/.claude/hooks/config.py            # All settings")
    print("  ~/.claude/compact-instructions.txt   # Compaction instructions")
    print("  ~/.claude/setup.sh                   # Shell alias setup")
    print()
    print("Documentation: ~/.claude/hooks/CONTEXT_MANAGEMENT.md")
    print()
    print("To uninstall: python3 uninstall.py")


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
        print(__doc__)
        print("Usage: python3 install.py")
        sys.exit(0)

    install()


if __name__ == '__main__':
    main()
