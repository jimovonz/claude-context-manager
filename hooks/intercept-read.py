#!/usr/bin/env python3
"""
Intercepts Read tool for large files, caches and returns reference.
Small files and paginated reads pass through to Claude.
"""

import re
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.common import (
    init_cache, check_passthrough, parse_hook_input, get_common_fields,
    allow_if_subagent, json_block, json_pass, log_metric, CACHE_DIR, READ_THRESHOLD
)

# File patterns that should never be intercepted (full content required)
PASSTHROUGH_PATTERNS = re.compile(
    r'^CLAUDE\.md$|^README\.md$|^README$|\.json$|\.yaml$|\.yml$|\.toml$|\.lock$|\.env',
    re.IGNORECASE
)


def main():
    init_cache()
    check_passthrough()

    input_data = parse_hook_input()
    tool, transcript_path, tool_use_id, cwd = get_common_fields(input_data)

    # Only handle Read
    if tool != 'Read':
        json_pass()
        return

    # Allow subagents through
    allow_if_subagent(transcript_path, tool_use_id)

    # Extract file path
    tool_input = input_data.get('tool_input', {})
    file_path = tool_input.get('file_path', '')

    # Expand ~ first before any path resolution
    if file_path.startswith('~'):
        file_path = str(Path(file_path).expanduser())

    # Resolve relative paths against cwd
    if cwd and not file_path.startswith('/'):
        file_path = str(Path(cwd) / file_path)

    file_path = Path(file_path)

    # Block main agent from reading cache files
    file_path_str = str(file_path)
    if '/tmp/claude-tool-cache/' in file_path_str:
        json_block("Cache file - use Task agent to read.")
        return
    if '/.claude/cache/' in file_path_str:
        json_block("Cache file - use Task agent to read.")
        return

    # Extract remaining Read parameters
    offset = tool_input.get('offset')
    limit = tool_input.get('limit')

    # If offset/limit specified, user is already paginating - let it through
    if offset is not None or limit is not None:
        log_metric("Read", "paginated", 0)
        json_pass()
        return

    # Check file exists
    if not file_path.is_file():
        log_metric("Read", "notfound", 0)
        json_pass()
        return

    # File whitelist: never intercept these
    if PASSTHROUGH_PATTERNS.search(file_path.name):
        log_metric("Read", "whitelist", 0)
        json_pass()
        return

    # Check file size
    file_size = file_path.stat().st_size

    # Small files pass through
    if file_size < READ_THRESHOLD:
        log_metric("Read", "pass", file_size)
        json_pass()
        return

    # Large file - cache it and return reference
    file_uuid = uuid.uuid4().hex[:8]
    cache_file = CACHE_DIR / file_uuid
    shutil.copy2(file_path, cache_file)

    lines = sum(1 for _ in open(cache_file))
    log_metric("Read", "cached", file_size)

    reason = f"""Cached ({lines} lines, {file_size} bytes).
File: ~/.claude/cache/{file_uuid}
Original: {file_path}

Options: Task agent (summarize or full content), or paginate with offset/limit."""

    json_block(reason)


if __name__ == '__main__':
    main()
