#!/usr/bin/env python3
"""
Review learned large-output command patterns.
Usage: review-learned-commands.py [--project]
"""

import re
import sys
from pathlib import Path

GLOBAL_FILE = Path.home() / '.claude' / 'learned-patterns.txt'
PROJECT_FILE = Path('.claude/learned-patterns.txt')


def parse_patterns(file_path: Path) -> list[dict]:
    """Parse learned patterns file into structured data."""
    if not file_path.exists():
        return []

    patterns = []
    current_comment = None

    for line in file_path.read_text().splitlines():
        # Parse comment line: # Learned 2025-12-15: command (1234 bytes)
        match = re.match(r'^# Learned (\d{4}-\d{2}-\d{2}): (.+) \((\d+) bytes\)$', line)
        if match:
            current_comment = {
                'date': match.group(1),
                'command': match.group(2),
                'size': int(match.group(3))
            }
        elif line.strip() and not line.startswith('#') and current_comment:
            # Pattern line
            patterns.append({
                **current_comment,
                'pattern': line.strip()
            })
            current_comment = None

    return patterns


def main():
    # Select file
    if len(sys.argv) > 1 and sys.argv[1] == '--project' and PROJECT_FILE.exists():
        file_path = PROJECT_FILE
        print("=== Project-specific learned patterns ===")
    else:
        file_path = GLOBAL_FILE
        print("=== Global learned patterns ===")

    patterns = parse_patterns(file_path)

    if not patterns:
        print("\nNo learned patterns yet.")
        return

    print(f"\nPatterns learned from commands producing large output (>{2000} bytes):\n")

    # Sort by date (newest first)
    patterns.sort(key=lambda x: x['date'], reverse=True)

    for p in patterns:
        print(f"[{p['date']}, {p['size']} bytes]")
        print(f"  Command: {p['command']}")
        print(f"  Pattern: {p['pattern']}")
        print()

    print(f"Total: {len(patterns)} patterns")
    print(f"\nTo clear: rm {file_path}")


if __name__ == '__main__':
    main()
