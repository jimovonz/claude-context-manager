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


def get_claude_cmdline(pid: int) -> list[str]:
    """Get command line args for a process."""
    try:
        cmdline_path = Path(f'/proc/{pid}/cmdline')
        if cmdline_path.exists():
            cmdline = cmdline_path.read_bytes().decode().split('\0')
            return [c for c in cmdline if c]
    except Exception:
        pass
    return []


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


def build_resume_cmd(original_args: list[str], session_id: str) -> str:
    """Build resume command preserving original flags."""
    # Start with claude
    cmd_parts = ['claude']

    # Preserve flags from original command (skip 'claude' itself and any --resume/--continue)
    skip_next = False
    for arg in original_args[1:]:  # Skip first element (claude binary path)
        if skip_next:
            skip_next = False
            continue
        if arg in ('--resume', '--continue', '-c'):
            skip_next = arg == '--resume'  # --resume takes a value
            continue
        if arg.startswith('--resume='):
            continue
        cmd_parts.append(arg)

    # Add resume with session
    cmd_parts.append('--resume')
    cmd_parts.append(session_id)

    return ' '.join(cmd_parts)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--pid', type=int, required=True, help='Claude PID to kill')
    parser.add_argument('--cwd', required=True, help='Working directory')
    parser.add_argument('--delay', type=int, default=DELAY_SECONDS)
    parser.add_argument('--session', help='Session ID to resume')
    parser.add_argument('--original-args', help='Original claude command line (colon-separated)')
    args = parser.parse_args()

    # Detach from parent
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)

    # Wait
    time.sleep(args.delay)

    # Get original args
    original_args = []
    if args.original_args:
        original_args = args.original_args.split(':')
    else:
        original_args = get_claude_cmdline(args.pid)

    # Build resume command
    session_id = args.session or get_session_id(args.cwd)
    if session_id:
        cmd = build_resume_cmd(original_args, session_id)
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
