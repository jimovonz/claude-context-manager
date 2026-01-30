#!/usr/bin/env python3
"""
Retrieve content from CCM cache with optional filtering.

Usage:
    ccm-get.py <key>                      # Full content
    ccm-get.py <key> --grep PATTERN       # Lines matching pattern
    ccm-get.py <key> --head N             # First N lines
    ccm-get.py <key> --tail N             # Last N lines
    ccm-get.py <key> --lines 100-200      # Line range (1-indexed)
    ccm-get.py <key> --grep error -C 3    # Matches with 3 lines context
    ccm-get.py <key> --info               # Show metadata only
"""

import re
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lib.ccm_cache import (
    init_ccm_cache, retrieve_content, get_metadata, get_last_key,
    list_all_keys, get_cache_stats
)


def main():
    parser = argparse.ArgumentParser(
        description='Retrieve content from CCM cache with optional filtering',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    ccm-get.py sha256:abc123              # Full content
    ccm-get.py sha256:abc123 --grep error # Lines containing 'error'
    ccm-get.py sha256:abc123 --grep "error|warn" -C 2  # With context
    ccm-get.py sha256:abc123 --head 50    # First 50 lines
    ccm-get.py sha256:abc123 --tail 20    # Last 20 lines
    ccm-get.py sha256:abc123 --lines 100-200  # Lines 100-200
    ccm-get.py sha256:abc123 --info       # Metadata only
    ccm-get.py --last --grep error        # Filter most recent
"""
    )
    parser.add_argument('key', nargs='?', help='Cache key (sha256:...)')

    # Filtering options
    filter_group = parser.add_argument_group('filtering')
    filter_group.add_argument('--grep', '-g', metavar='PATTERN',
                        help='Filter lines matching regex pattern')
    filter_group.add_argument('--head', type=int, metavar='N',
                        help='Show first N lines')
    filter_group.add_argument('--tail', type=int, metavar='N',
                        help='Show last N lines')
    filter_group.add_argument('--lines', metavar='START-END',
                        help='Show line range (1-indexed, e.g., 100-200)')
    filter_group.add_argument('-C', '--context', type=int, default=0, metavar='N',
                        help='Show N lines of context around grep matches')
    filter_group.add_argument('-i', '--ignore-case', action='store_true',
                        help='Case-insensitive grep')

    # Info/listing options
    parser.add_argument('--info', action='store_true',
                        help='Show metadata instead of content')
    parser.add_argument('--last', '-l', action='store_true',
                        help='Use most recently cached key')
    parser.add_argument('--list', action='store_true',
                        help='List recent cache entries')
    parser.add_argument('--stats', '-s', action='store_true',
                        help='Show cache statistics')
    parser.add_argument('--limit', '-n', type=int, default=20,
                        help='Limit for --list (default: 20)')

    args = parser.parse_args()

    init_ccm_cache()

    if args.stats:
        stats = get_cache_stats()
        print(f"Cache directory: {stats.get('cache_dir', 'unknown')}")
        print(f"Total entries: {stats.get('total_entries', 0)}")
        print(f"Total size: {stats.get('total_size_bytes', 0):,} bytes")
        print(f"Pinned entries: {stats.get('pinned_count', 0)}")
        return

    if args.list:
        keys = list_all_keys()
        if not keys:
            print("Cache is empty", file=sys.stderr)
            return

        print(f"Recent cache entries (showing {min(len(keys), args.limit)} of {len(keys)}):\n")
        for key in keys[:args.limit]:
            meta = get_metadata(key)
            if meta:
                pin_status = f" [pinned:{meta.get('pinned', {}).get('level', 'none')}]" if meta.get('pinned', {}).get('level', 'none') != 'none' else ""
                source = meta.get('source', {})
                tool = source.get('tool_name', 'unknown')
                size = meta.get('bytes_uncompressed', 0)
                print(f"  {key[:20]}...  {size:>8,} bytes  {tool}{pin_status}")
            else:
                print(f"  {key}")
        return

    # Resolve key
    key = args.key
    if args.last:
        key = get_last_key()
        if not key:
            print("No cached items found", file=sys.stderr)
            sys.exit(1)
        if not args.key:
            pass  # Use last key
        else:
            print(f"Note: Using --last key: {key}", file=sys.stderr)

    if not key:
        parser.print_help()
        sys.exit(1)

    if args.info:
        meta = get_metadata(key)
        if not meta:
            print(f"Key not found: {key}", file=sys.stderr)
            sys.exit(1)

        print(f"Key: {meta.get('key', key)}")
        print(f"Created: {meta.get('created_at', 'unknown')}")
        print(f"Last access: {meta.get('last_access_at', 'unknown')}")
        print(f"Size: {meta.get('bytes_uncompressed', 0):,} bytes")
        print(f"Lines: {meta.get('lines', 0)}")
        print(f"Compression: {meta.get('compression', 'unknown')}")

        source = meta.get('source', {})
        if source:
            print(f"\nSource:")
            print(f"  Tool: {source.get('tool_name', 'unknown')}")
            print(f"  Exit code: {source.get('exit_code', 'unknown')}")
            if source.get('command'):
                cmd = source['command']
                if len(cmd) > 80:
                    cmd = cmd[:77] + '...'
                print(f"  Command: {cmd}")

        pinned = meta.get('pinned', {})
        if pinned.get('level', 'none') != 'none':
            print(f"\nPinned:")
            print(f"  Level: {pinned.get('level')}")
            print(f"  Reason: {pinned.get('reason', '')}")
            print(f"  Pinned at: {pinned.get('pinned_at', 'unknown')}")
        return

    # Get content
    content = retrieve_content(key)
    if content is None:
        print(f"Key not found or content unavailable: {key}", file=sys.stderr)
        sys.exit(1)

    lines = content.splitlines()
    original_count = len(lines)
    filtered = False

    # Apply filters in order: lines range → grep → head/tail
    # This allows: --grep error --head 10 = "first 10 errors"

    # Line range filter (1-indexed) - applied first to limit search scope
    if args.lines:
        try:
            if '-' in args.lines:
                start, end = args.lines.split('-', 1)
                start = int(start) if start else 1
                end = int(end) if end else len(lines)
            else:
                start = end = int(args.lines)
            # Convert to 0-indexed
            lines = lines[max(0, start-1):end]
            filtered = True
        except ValueError:
            print(f"Invalid line range: {args.lines}", file=sys.stderr)
            sys.exit(1)

    # Grep filter - before head/tail so --head N means "first N matches"
    if args.grep:
        try:
            flags = re.IGNORECASE if args.ignore_case else 0
            pattern = re.compile(args.grep, flags)
        except re.error as e:
            print(f"Invalid regex: {e}", file=sys.stderr)
            sys.exit(1)

        if args.context > 0:
            # Grep with context
            matched_indices = set()
            for i, line in enumerate(lines):
                if pattern.search(line):
                    for j in range(max(0, i - args.context), min(len(lines), i + args.context + 1)):
                        matched_indices.add(j)

            result_lines = []
            prev_idx = -2
            for i in sorted(matched_indices):
                if prev_idx >= 0 and i > prev_idx + 1:
                    result_lines.append('--')  # Context separator
                result_lines.append(lines[i])
                prev_idx = i
            lines = result_lines
        else:
            # Simple grep
            lines = [l for l in lines if pattern.search(l)]
        filtered = True

    # Head filter - after grep, so --grep X --head N = "first N matches"
    if args.head:
        lines = lines[:args.head]
        filtered = True

    # Tail filter - after grep, so --grep X --tail N = "last N matches"
    if args.tail:
        lines = lines[-args.tail:]
        filtered = True

    # Output
    if filtered:
        print(f"[Filtered: {len(lines)} of {original_count} lines]", file=sys.stderr)

    output = '\n'.join(lines)
    sys.stdout.write(output)
    if output and not output.endswith('\n'):
        sys.stdout.write('\n')


if __name__ == '__main__':
    main()
