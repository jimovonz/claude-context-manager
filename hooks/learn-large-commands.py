#!/usr/bin/env python3
"""
PostToolUse hook: Learn commands that produce large output.
Note: With unified execution model, this is mainly for analytics.
Patterns are no longer used for interception (we measure directly).
"""

import json
import random
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.common import (
    parse_hook_input, log_metric, BASH_THRESHOLD, PATTERNS_EXPIRY_DAYS, HOOKS_DIR
)

PATTERNS_FILE = Path.home() / '.claude' / 'learned-patterns.txt'
PROJECT_PATTERNS_FILE = Path('.claude/learned-patterns.txt')

# Commands that are already handled or shouldn't be learned
SKIP_PATTERNS = re.compile(r'^(ls|cd|cat|echo|git\s+log|git\s+diff|make|cargo|npm|go\s+)')


def cleanup_expired_patterns(file_path: Path) -> None:
    """Remove patterns older than expiry days."""
    if not file_path.exists():
        return

    cutoff = datetime.now() - timedelta(days=PATTERNS_EXPIRY_DAYS)
    cutoff_str = cutoff.strftime('%Y-%m-%d')

    lines = file_path.read_text().splitlines()
    new_lines = []
    keep = False

    for line in lines:
        # Check for learned comment with date
        match = re.match(r'^# Learned (\d{4}-\d{2}-\d{2}):', line)
        if match:
            date_str = match.group(1)
            keep = date_str >= cutoff_str
            if keep:
                new_lines.append(line)
        elif line.startswith('#'):
            # Other comments - keep
            new_lines.append(line)
        elif line.strip():
            # Pattern line - keep if previous comment was recent
            if keep:
                new_lines.append(line)

    file_path.write_text('\n'.join(new_lines) + '\n' if new_lines else '')


def main():
    input_data = parse_hook_input()

    tool = input_data.get('tool_name', '')
    if tool != 'Bash':
        return

    # Get command and output
    cmd = input_data.get('tool_input', {}).get('command', '')
    output = input_data.get('tool_result', {}).get('stdout', '')
    size = len(output)

    # Skip if output is small
    if size <= BASH_THRESHOLD:
        return

    # Skip if command was already intercepted
    if 'Output cached' in output or '/.claude/cache/' in output or '/tmp/claude-tool-cache/' in output:
        return

    # Run cleanup occasionally (1 in 10 chance)
    if random.randint(1, 10) == 1:
        cleanup_expired_patterns(PATTERNS_FILE)
        if PROJECT_PATTERNS_FILE.exists():
            cleanup_expired_patterns(PROJECT_PATTERNS_FILE)

    # Extract base command
    # e.g., "cd foo && kubectl get pods -A" -> "kubectl get pods"
    base_cmd = re.sub(r'.*(&& |; )', '', cmd)
    parts = base_cmd.split()[:3]
    base_cmd = ' '.join(parts)

    # Skip common commands
    if SKIP_PATTERNS.match(base_cmd):
        return

    # Create regex pattern
    escaped = re.escape(base_cmd)
    pattern = f"(^|&&|;)\\s*{escaped}"

    # Check if pattern already exists
    if PATTERNS_FILE.exists() and pattern in PATTERNS_FILE.read_text():
        return

    # Add to global patterns file
    date_str = datetime.now().strftime('%Y-%m-%d')
    entry = f"# Learned {date_str}: {cmd} ({size} bytes)\n{pattern}\n"

    with open(PATTERNS_FILE, 'a') as f:
        f.write(entry)

    # Also add to project patterns if in a project
    if Path('.claude').is_dir():
        PROJECT_PATTERNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not PROJECT_PATTERNS_FILE.exists() or pattern not in PROJECT_PATTERNS_FILE.read_text():
            with open(PROJECT_PATTERNS_FILE, 'a') as f:
                f.write(entry)

    log_metric("Learn", "pattern", size)


if __name__ == '__main__':
    main()
