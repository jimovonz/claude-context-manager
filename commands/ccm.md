Context Manager (CCM) command. Parse the argument to determine the action:

**Usage:** `/ccm <action>` where action is one of:

## Actions

### `--purge` or `-p`
Reduce context usage by removing thinking blocks and truncating/caching large tool outputs.

Execute:
```bash
~/.claude/hooks/claude-session-purge.py --current --verbose --restart
```

Report results (bytes saved, blocks removed, repairs made).

NOTE: The `--restart` flag kills Claude after purge to prevent it from overwriting the purged file. The resume command will be copied to clipboard.

### `--purge --dry-run` or `-p -n`
Preview purge without making changes.

Execute:
```bash
~/.claude/hooks/claude-session-purge.py --current --verbose --dry-run
```

Report what WOULD be changed without actually modifying the session.

### `--repair` or `-r`
Repair a corrupted session (fix broken tool pairing, parent chain issues, missing thinking blocks).

Execute:
```bash
~/.claude/hooks/claude-session-purge.py --current --repair-only --verbose --restart
```

Report what was repaired. Kills Claude after repair to apply changes.

### `--status` or `-s`
Show CCM cache statistics and current session info.

Execute:
```bash
python3 -c "
import sys
sys.path.insert(0, '$HOME/.claude/hooks')
from lib.ccm_cache import get_cache_stats
stats = get_cache_stats()
print('=== CCM Cache Stats ===')
print(f'Total items: {stats.get(\"total_items\", 0)}')
print(f'Uncompressed size: {stats.get(\"total_bytes_uncompressed\", 0):,} bytes')
print(f'Compressed size: {stats.get(\"total_bytes_compressed\", 0):,} bytes')
print()
print('--- Pinning ---')
print(f'Pinned (hard): {stats.get(\"pinned_hard\", 0)}')
print(f'Pinned (soft): {stats.get(\"pinned_soft\", 0)}')
print(f'Unpinned: {stats.get(\"unpinned\", 0)}')
print()
print('--- Access Statistics ---')
print(f'Total retrievals: {stats.get(\"total_accesses\", 0)}')
print(f'Items accessed: {stats.get(\"items_accessed\", 0)}')
print(f'Items never accessed: {stats.get(\"items_never_accessed\", 0)}')
print(f'Max access count: {stats.get(\"max_access_count\", 0)}')
if stats.get('total_items', 0) > 0:
    pct = 100 * stats.get('items_accessed', 0) / stats.get('total_items', 1)
    print(f'Access rate: {pct:.1f}% of cached items have been accessed')
"
```

Also run `~/.claude/hooks/claude-session-purge.py --current --analyze` to show session statistics.

### `--clear-cache` or `-c`
Clear the CCM blob cache (keeps index for deduplication).

Execute:
```bash
rm -rf ~/.claude/cache/ccm/blobs/*
echo "CCM cache cleared"
```

### `--restart` or `-x`
Test the restart mechanism without purging. Identifies Claude PID, spawns the restart helper, and terminates Claude.

Execute:
```bash
python3 -c "
import os
import sys
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path.home() / '.claude' / 'hooks'))
from importlib import import_module
purge = import_module('claude-session-purge')

# Find Claude PID
claude_pid = purge.find_claude_pid_from_parent_chain()
if not claude_pid:
    print('ERROR: Could not find Claude PID in parent chain')
    sys.exit(1)

# Get session
session_file = purge.find_current_session()
if not session_file:
    print('ERROR: Could not find current session')
    sys.exit(1)

# Get TTY from Claude process
tty = purge.get_process_tty(claude_pid) or '/dev/tty'

# Show CLAUDE_LAUNCH_ARGS (used by restart helper)
launch_args = os.environ.get('CLAUDE_LAUNCH_ARGS', '(not set)')

print(f'Claude PID: {claude_pid}')
print(f'Session: {session_file.stem}')
print(f'TTY: {tty}')
print(f'CLAUDE_LAUNCH_ARGS: {launch_args}')
print()
print('Spawning restart helper (3s delay)...')

restart_script = Path.home() / '.claude' / 'hooks' / 'auto-restart.py'
subprocess.Popen([
    sys.executable, str(restart_script),
    '--pid', str(claude_pid),
    '--cwd', os.getcwd(),
    '--delay', '3',
    '--session', session_file.stem,
    '--tty', tty,
], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
"
```

Report the detected Claude PID, session, and TTY. The restart helper will kill Claude after 3 seconds and copy the resume command.

Note: The restart helper uses the `CLAUDE_LAUNCH_ARGS` env var (set in `~/.claude/settings.json`) to build the resume command.

### `--help` or `-h` (or no argument)
Show this help message - list available CCM commands and their descriptions.

---

If no argument or `--help` is provided, display the available actions to the user.
