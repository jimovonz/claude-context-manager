#!/usr/bin/env python3
"""
Intercepts Bash tool to:
1. Block main agent from accessing cache directly
2. Execute non-trivial commands, cache large output
3. Return all results via block (no double-execution)
4. Allow subagents through unimpeded
"""

import re
import sys
from pathlib import Path

# Add hooks dir to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from lib.common import (
    init_cache, check_passthrough, parse_hook_input, get_common_fields,
    allow_if_subagent, json_block, json_pass, cache_output, build_cache_response,
    log_metric, run_command, BASH_THRESHOLD, CACHE_DIR
)


def is_obviously_small(cmd: str) -> bool:
    """Check if command is trivially small output."""
    # Compound commands need execution to measure
    if re.search(r'(&&|;|\|)', cmd):
        return False

    small_patterns = [
        r'^ls$', r'^ls\s+-[alh]', r'^pwd$', r'^whoami$', r'^id$', r'^date$',
        r'^echo\s+', r'^printf\s+',
        r'^cd\s+', r'^mkdir\s+', r'^touch\s+', r'^rm\s+', r'^mv\s+', r'^cp\s+',
        r'^git\s+status$', r'^git\s+branch', r'^git\s+remote',
        r'^git\s+log\s+-\d+', r'^git\s+log\s+--oneline\s+-\d+',
        r'^which\s+', r'^type\s+', r'^command\s+-v\s+', r'^hash\s+',
        r'^head\s+', r'^tail\s+', r'^wc\s+', r'^stat\s+',
        r'^test\s+', r'^\[\s+',
        r'^[A-Za-z_][A-Za-z0-9_]*=',  # Variable assignments
    ]

    for pattern in small_patterns:
        if re.match(pattern, cmd):
            return True
    return False


def is_obviously_interactive(cmd: str) -> bool:
    """Check if command is interactive."""
    interactive_patterns = [
        r'^(vim|vi|nano|emacs|less|more|man|top|htop|btop|watch)\s+',
        r'^(ssh|telnet|ftp|sftp)\s+',
        r'^(python|python3|node|ruby|irb|ghci)$',
        # OAuth/device flow commands that wait for browser
        r'^gh\s+auth\s+(login|refresh|status)',
    ]

    for pattern in interactive_patterns:
        if re.match(pattern, cmd):
            return True

    # Check for -i flag
    if re.search(r'(^|\s)-i(\s|$)', cmd):
        return True

    return False


def main():
    init_cache()
    check_passthrough()

    input_data = parse_hook_input()
    tool, transcript_path, tool_use_id, cwd = get_common_fields(input_data)

    # Only handle Bash
    if tool != 'Bash':
        json_pass()
        return

    cmd = input_data.get('tool_input', {}).get('command', '')
    timeout_ms = input_data.get('tool_input', {}).get('timeout', 120000)

    # Allow subagents through
    allow_if_subagent(transcript_path, tool_use_id)

    # Cache access blocking
    if re.search(r'(/.claude/cache/|/tmp/claude-tool-cache/)', cmd):
        # Allow listing/cleaning/stat commands
        if re.match(r'^(ls|rm|find|wc|stat|du|df|/bin/ls|/usr/bin/find|/bin/rm)(\s|$)', cmd):
            json_pass()
            return
        json_block("Cache file - use Task agent to read.")
        return

    # Trivial commands: let Claude handle natively
    if is_obviously_small(cmd):
        log_metric("Bash", "pass", 0)
        json_pass()
        return

    # Interactive commands: let Claude handle
    if is_obviously_interactive(cmd):
        log_metric("Bash", "interactive", 0)
        json_pass()
        return

    # Execute everything else
    timeout_sec = min(max(timeout_ms // 1000, 1), 600)
    output, exit_code = run_command(cmd, cwd, timeout_sec)
    size = len(output)

    # Return result (always block to avoid double-execution)
    if size <= BASH_THRESHOLD:
        log_metric("Bash", "inline", size)
        reason = f"Exit {exit_code}:\n\n{output}"
        json_block(reason)
    else:
        file_uuid = cache_output(output)
        lines = output.count('\n')
        log_metric("Bash", "cached", size)
        reason = build_cache_response(file_uuid, lines, size, exit_code, cmd)
        json_block(reason)


if __name__ == '__main__':
    main()
