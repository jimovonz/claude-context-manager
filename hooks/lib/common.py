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
