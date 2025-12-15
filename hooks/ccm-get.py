#!/usr/bin/env python3
"""
Retrieve content from CCM cache by key.

Usage:
    ccm-get.py <key>           # Output content to stdout
    ccm-get.py <key> --info    # Show metadata only
    ccm-get.py --last          # Get most recently cached item
    ccm-get.py --list          # List recent cache entries
"""

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
        description='Retrieve content from CCM cache',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    ccm-get.py sha256:abc123...         # Get content by key
    ccm-get.py sha256:abc123 --info     # Show metadata
    ccm-get.py --last                   # Get most recent
    ccm-get.py --last --info            # Info on most recent
    ccm-get.py --list                   # List recent keys
    ccm-get.py --stats                  # Show cache statistics
"""
    )
    parser.add_argument('key', nargs='?', help='Cache key (sha256:...)')
    parser.add_argument('--info', '-i', action='store_true',
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

    # Output to stdout
    sys.stdout.write(content)
    if not content.endswith('\n'):
        sys.stdout.write('\n')


if __name__ == '__main__':
    main()
