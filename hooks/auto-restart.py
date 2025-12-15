#!/usr/bin/env python3
"""
Auto-restart Claude after purge. Spawned as background process.
Waits, kills Claude, copies resume command to clipboard.
"""

import os
import sys
import time
import signal
import subprocess
import shutil
from pathlib import Path

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
    cmd_parts = ['claude']

    # Preserve flags from original command (skip 'claude' itself and any --resume/--continue)
    skip_next = False
    for arg in original_args[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in ('--resume', '--continue', '-c'):
            skip_next = arg == '--resume'
            continue
        if arg.startswith('--resume='):
            continue
        cmd_parts.append(arg)

    cmd_parts.append('--resume')
    cmd_parts.append(session_id)

    return ' '.join(cmd_parts)


def copy_to_clipboard(text: str) -> bool:
    """Copy text to clipboard. Returns True on success."""
    # Try wl-copy first (Wayland native)
    if shutil.which('wl-copy'):
        try:
            subprocess.run(['wl-copy', text], check=True)
            return True
        except:
            pass

    # Try xclip
    if shutil.which('xclip'):
        try:
            subprocess.run(['xclip', '-selection', 'clipboard'],
                         input=text.encode(), check=True)
            return True
        except:
            pass

    # Try xsel
    if shutil.which('xsel'):
        try:
            subprocess.run(['xsel', '--clipboard', '--input'],
                         input=text.encode(), check=True)
            return True
        except:
            pass

    return False


def get_tty() -> str:
    """Get the TTY of the Claude process."""
    # Will be passed as argument, but fallback to current
    return os.ttyname(sys.stdout.fileno()) if sys.stdout.isatty() else '/dev/tty'


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--pid', type=int, required=True, help='Claude PID to kill')
    parser.add_argument('--cwd', required=True, help='Working directory')
    parser.add_argument('--delay', type=int, default=DELAY_SECONDS)
    parser.add_argument('--session', help='Session ID to resume')
    parser.add_argument('--original-args', help='Original claude command line (colon-separated)')
    parser.add_argument('--tty', help='TTY to write message to')
    args = parser.parse_args()

    # Detach from parent (double fork)
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
    if not session_id:
        sys.exit(1)

    resume_cmd = build_resume_cmd(original_args, session_id)

    # Kill Claude
    try:
        os.kill(args.pid, signal.SIGTERM)
        time.sleep(0.5)
    except ProcessLookupError:
        pass

    # Make sure it's dead
    try:
        os.kill(args.pid, signal.SIGKILL)
        time.sleep(0.3)
    except ProcessLookupError:
        pass

    # Copy to clipboard and notify
    tty = args.tty or '/dev/tty'
    try:
        with open(tty, 'w') as f:
            if copy_to_clipboard(resume_cmd):
                f.write(f"\n\033[1;32mResume command copied to clipboard.\033[0m\n")
                f.write(f"Press \033[1mCtrl+Shift+V\033[0m then \033[1mEnter\033[0m\n\n")
            else:
                f.write(f"\n\033[1;33mRun:\033[0m {resume_cmd}\n\n")
    except:
        pass


if __name__ == '__main__':
    main()
