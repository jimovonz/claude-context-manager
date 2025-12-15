#!/usr/bin/env python3
"""
Auto-restart Claude after purge. Spawned as background process.
Waits, kills Claude, injects resume command via X11.
"""

import os
import sys
import time
import signal
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'lib'))

DELAY_SECONDS = 3


def find_claude_pid(exclude_pid: int = None) -> tuple[int, list[str]]:
    """Find Claude process and its command line args."""
    try:
        result = subprocess.run(
            ['pgrep', '-a', 'claude'],
            capture_output=True, text=True
        )
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split(None, 1)
            pid = int(parts[0])
            if exclude_pid and pid == exclude_pid:
                continue
            # Get full cmdline
            cmdline_path = Path(f'/proc/{pid}/cmdline')
            if cmdline_path.exists():
                cmdline = cmdline_path.read_bytes().decode().split('\0')
                cmdline = [c for c in cmdline if c]  # Remove empty
                return pid, cmdline
    except Exception:
        pass
    return None, []


def get_session_id(cwd: str) -> str:
    """Find most recent session ID for directory."""
    project_path = cwd.replace('/', '-')
    if not project_path.startswith('-'):
        project_path = '-' + project_path

    sessions_dir = Path.home() / '.claude' / 'projects' / project_path
    if not sessions_dir.exists():
        return None

    candidates = []
    for f in sessions_dir.glob('*.jsonl'):
        if '.backup' in f.name or f.name.startswith('agent-'):
            continue
        candidates.append((f.stat().st_mtime, f.stem))

    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]
    return None


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--pid', type=int, required=True, help='Claude PID to kill')
    parser.add_argument('--cwd', required=True, help='Working directory')
    parser.add_argument('--delay', type=int, default=DELAY_SECONDS)
    parser.add_argument('--session', help='Session ID to resume')
    args = parser.parse_args()

    # Detach from parent
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)

    # Wait
    time.sleep(args.delay)

    # Build resume command
    session_id = args.session or get_session_id(args.cwd)
    if session_id:
        cmd = f"claude --resume {session_id}"
    else:
        cmd = "claude --continue"

    # Kill Claude
    try:
        os.kill(args.pid, signal.SIGTERM)
        time.sleep(0.5)  # Let it exit gracefully
    except ProcessLookupError:
        pass  # Already dead

    # Small delay for terminal to be ready
    time.sleep(0.3)

    # Type resume command
    try:
        from x11_type import type_string
        type_string(cmd + '\n', delay=0.008)
    except Exception as e:
        # Fallback: print to stderr (won't be visible but logged)
        print(f"Auto-restart failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
