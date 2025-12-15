#!/usr/bin/env python3
"""
Shared library for Claude Code hooks.
Import this module at the start of each hook script.
"""

import json
import os
import sys
import subprocess
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Tuple

# Load configuration
HOOKS_DIR = Path(os.environ.get('HOOKS_DIR', Path.home() / '.claude' / 'hooks'))
CONFIG_FILE = HOOKS_DIR / 'config.py'

# Defaults (can be overridden in config.py)
CACHE_DIR = Path.home() / '.claude' / 'cache'
CACHE_MAX_AGE_MINUTES = 60
BASH_THRESHOLD = 2000
GLOB_THRESHOLD = 2000
GREP_THRESHOLD = 2000
READ_THRESHOLD = 25000
PATTERNS_EXPIRY_DAYS = 30
METRICS_ENABLED = False

# Load config if exists
if CONFIG_FILE.exists():
    _config = {}
    exec(CONFIG_FILE.read_text(), _config)
    for _key in ['CACHE_DIR', 'CACHE_MAX_AGE_MINUTES', 'BASH_THRESHOLD',
                 'GLOB_THRESHOLD', 'GREP_THRESHOLD', 'READ_THRESHOLD',
                 'PATTERNS_EXPIRY_DAYS', 'METRICS_ENABLED']:
        if _key in _config:
            globals()[_key] = _config[_key]


def init_cache() -> None:
    """Initialize cache directory and clean old files."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now().timestamp() - (CACHE_MAX_AGE_MINUTES * 60)
    for f in CACHE_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
            except OSError:
                pass


def check_passthrough() -> None:
    """Check for passthrough mode (bypass all hooks)."""
    if os.environ.get('CLAUDE_HOOKS_PASSTHROUGH') == '1':
        print('{}')
        sys.exit(0)


def is_subagent(transcript_path: str, tool_use_id: str) -> bool:
    """Check if current call is from a subagent."""
    if not transcript_path:
        return False
    transcript_dir = Path(transcript_path).parent

    # Quick check: any agent files exist?
    agent_files = list(transcript_dir.glob('agent-*.jsonl'))
    if not agent_files:
        return False

    search_pattern = f'"id":"{tool_use_id}"'

    for agent_file in agent_files:
        try:
            # Only check tail of file - recent tool calls are at the end
            # Tool use ID appears when assistant generates the call,
            # which is recent relative to PreToolUse hook firing
            file_size = agent_file.stat().st_size
            read_size = min(file_size, 64 * 1024)  # Last 64KB

            with open(agent_file, 'rb') as f:
                if file_size > read_size:
                    f.seek(-read_size, 2)  # Seek from end
                content = f.read().decode('utf-8', errors='ignore')

            if search_pattern in content:
                return True
        except OSError:
            pass
    return False


def allow_if_subagent(transcript_path: str, tool_use_id: str) -> None:
    """Allow subagent through without interception."""
    if is_subagent(transcript_path, tool_use_id):
        json_pass()
        sys.exit(0)


def cache_output(content: str) -> str:
    """Cache content to file, return UUID."""
    file_uuid = uuid.uuid4().hex[:8]
    cache_file = CACHE_DIR / file_uuid
    cache_file.write_text(content)
    return file_uuid


def json_block(reason: str) -> None:
    """Output JSON to block tool execution with reason."""
    print(json.dumps({"decision": "block", "reason": reason}))


def json_pass() -> None:
    """Output JSON to allow tool execution (pass through)."""
    print('{}')


def build_cache_response(file_uuid: str, lines: int, size: int, exit_code: int, original: str) -> str:
    """Build cache response message (minimal)."""
    return f"""Cached ({lines} lines, {size} bytes, exit {exit_code}).
File: ~/.claude/cache/{file_uuid}
Original: {original}

Options: Task agent (summarize or full content), or paginate with offset/limit."""


def log_metric(tool: str, action: str, size: int = 0) -> None:
    """Log metrics (if enabled)."""
    if not METRICS_ENABLED:
        return
    timestamp = datetime.now().isoformat()
    log_file = HOOKS_DIR / 'metrics.log'
    with open(log_file, 'a') as f:
        f.write(f"{timestamp} {tool} {action} {size}\n")


def parse_hook_input() -> dict:
    """Parse hook input from stdin."""
    return json.load(sys.stdin)


def get_common_fields(input_data: dict) -> Tuple[str, str, str, str]:
    """Extract common fields from hook input."""
    tool = input_data.get('tool_name', '')
    transcript_path = input_data.get('transcript_path', '')
    tool_use_id = input_data.get('tool_use_id', '')
    cwd = input_data.get('session', {}).get('cwd', '')
    return tool, transcript_path, tool_use_id, cwd


def run_command(cmd: str, cwd: Optional[str] = None, timeout: int = 120) -> Tuple[str, int]:
    """Run a shell command and return (output, exit_code)."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd if cwd and Path(cwd).is_dir() else None,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        output = result.stdout + result.stderr
        return output, result.returncode
    except subprocess.TimeoutExpired:
        return "Command timed out", 124
    except Exception as e:
        return str(e), 1


# Interactive commands blacklist
INTERACTIVE_COMMANDS_FILE = HOOKS_DIR / 'interactive-commands.txt'
PROBE_TIMEOUT = 2.0  # Seconds to wait before assuming command is interactive


def load_interactive_blacklist() -> set:
    """Load learned interactive command patterns."""
    if not INTERACTIVE_COMMANDS_FILE.exists():
        return set()
    patterns = set()
    for line in INTERACTIVE_COMMANDS_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            patterns.add(line)
    return patterns


def save_interactive_pattern(pattern: str) -> None:
    """Add a pattern to the interactive commands blacklist."""
    existing = load_interactive_blacklist()
    if pattern not in existing:
        with open(INTERACTIVE_COMMANDS_FILE, 'a') as f:
            f.write(f"{pattern}\n")


def extract_command_pattern(cmd: str) -> Optional[str]:
    """Extract a generalizable pattern from a command.

    Examples:
        'gh auth refresh -h github.com' -> 'gh auth'
        'ssh user@host' -> 'ssh'
        'python3 script.py' -> None (too generic)
    """
    import shlex
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()

    if not parts:
        return None

    base = parts[0]

    # Skip overly generic commands
    generic = {'python', 'python3', 'node', 'bash', 'sh', 'ruby', 'perl'}
    if base in generic:
        return None

    # For multi-level commands, include subcommand
    if len(parts) > 1 and not parts[1].startswith('-'):
        # Commands with subcommands: gh auth, git credential, docker login, etc.
        multi_level = {'gh', 'git', 'docker', 'kubectl', 'aws', 'gcloud', 'az', 'npm', 'yarn'}
        if base in multi_level:
            return f"{base} {parts[1]}"

    return base


def is_blacklisted_interactive(cmd: str) -> bool:
    """Check if command matches a known interactive pattern."""
    blacklist = load_interactive_blacklist()
    if not blacklist:
        return False

    import shlex
    try:
        parts = shlex.split(cmd)
    except ValueError:
        parts = cmd.split()

    if not parts:
        return False

    # Check exact base command
    if parts[0] in blacklist:
        return True

    # Check two-level pattern
    if len(parts) > 1:
        two_level = f"{parts[0]} {parts[1]}"
        if two_level in blacklist:
            return True

    return False


def probe_command(cmd: str, cwd: Optional[str] = None,
                  full_timeout: int = 120) -> Tuple[Optional[str], int, bool]:
    """
    Run command with stdin closed to detect interactive commands.

    Returns (output, exit_code, is_interactive).
    If is_interactive=True, the command was killed and pattern learned.
    """
    import select

    work_cwd = cwd if cwd and Path(cwd).is_dir() else None

    try:
        proc = subprocess.Popen(
            cmd,
            shell=True,
            cwd=work_cwd,
            stdin=subprocess.DEVNULL,  # Close stdin - interactive commands will fail/hang
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as e:
        return str(e), 1, False

    output_chunks = []
    start_time = datetime.now()

    try:
        # Probe phase: wait briefly to see if command completes or hangs
        while (datetime.now() - start_time).total_seconds() < PROBE_TIMEOUT:
            ret = proc.poll()
            if ret is not None:
                # Command completed during probe - not interactive
                stdout, stderr = proc.communicate(timeout=1)
                output_chunks.append(stdout)
                output_chunks.append(stderr)
                return ''.join(output_chunks), ret, False

            # Read available output
            if hasattr(select, 'select'):
                readable, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.1)
                for stream in readable:
                    chunk = stream.read(4096) if stream else ''
                    if chunk:
                        output_chunks.append(chunk)

        # Command still running after probe timeout
        # Check if it produced any output (slow but working) vs hung (interactive)
        partial = ''.join(output_chunks)

        # If no output after 2 seconds with stdin closed, likely interactive
        if len(partial.strip()) < 50:
            # Learn this pattern
            pattern = extract_command_pattern(cmd)
            if pattern:
                save_interactive_pattern(pattern)

            # Kill and signal interactive
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
            return partial, -1, True

        # Has output, continue waiting (slow command)
        remaining = full_timeout - PROBE_TIMEOUT
        try:
            stdout, stderr = proc.communicate(timeout=remaining)
            output_chunks.append(stdout)
            output_chunks.append(stderr)
            return ''.join(output_chunks), proc.returncode, False
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return ''.join(output_chunks) + "\nCommand timed out", 124, False

    except Exception as e:
        try:
            proc.kill()
        except:
            pass
        return str(e), 1, False
