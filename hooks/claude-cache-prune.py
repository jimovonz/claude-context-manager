#!/usr/bin/env python3
"""
Cache pruning tool for Claude Context Manager.
Manages CCM durable cache: stats, pruning, pinning, garbage collection.

Usage:
    claude-cache-prune.py --stats
    claude-cache-prune.py --max-age-days N [--dry-run]
    claude-cache-prune.py --max-size-mb N [--dry-run]
    claude-cache-prune.py --gc-unreferenced [--dry-run]
    claude-cache-prune.py --pin KEY --level hard|soft [--reason "..."]
    claude-cache-prune.py --unpin KEY
    claude-cache-prune.py --list-pins
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

from lib.ccm_cache import (
    init_ccm_cache, get_cache_stats, get_metadata, update_pin,
    delete_cached_content, list_all_keys, CCM_META_DIR, CCM_BLOBS_DIR,
    is_ccm_stub, parse_ccm_stub
)

# Default paths
CLAUDE_PROJECTS_DIR = Path.home() / '.claude' / 'projects'
CLAUDE_SESSIONS_DIR = Path.home() / '.claude' / 'sessions'


def find_all_sessions() -> list[Path]:
    """Find all Claude session JSONL files."""
    sessions = []

    # Check projects directory
    if CLAUDE_PROJECTS_DIR.is_dir():
        sessions.extend(CLAUDE_PROJECTS_DIR.rglob('*.jsonl'))

    # Check sessions directory
    if CLAUDE_SESSIONS_DIR.is_dir():
        sessions.extend(CLAUDE_SESSIONS_DIR.rglob('*.jsonl'))

    return sorted(set(sessions))


def extract_stub_keys_from_session(session_path: Path) -> set[str]:
    """Extract all CCM stub keys referenced in a session file."""
    keys = set()

    try:
        with open(session_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                # Look for CCM stub markers
                if '[CCM_CACHED]' in line:
                    try:
                        entry = json.loads(line)
                        # Check message content
                        if 'message' in entry:
                            msg = entry['message']
                            content = msg.get('content', [])
                            if isinstance(content, list):
                                for block in content:
                                    if isinstance(block, dict):
                                        text = block.get('text', '')
                                        if is_ccm_stub(text):
                                            parsed = parse_ccm_stub(text)
                                            if parsed and 'key' in parsed:
                                                keys.add(parsed['key'])
                            elif isinstance(content, str) and is_ccm_stub(content):
                                parsed = parse_ccm_stub(content)
                                if parsed and 'key' in parsed:
                                    keys.add(parsed['key'])
                    except json.JSONDecodeError:
                        pass
    except OSError:
        pass

    return keys


def show_stats() -> None:
    """Display cache statistics."""
    init_ccm_cache()
    stats = get_cache_stats()

    print("CCM Cache Statistics")
    print("=" * 40)
    print(f"Total items:           {stats['total_items']}")
    print(f"Compressed size:       {stats['total_bytes_compressed'] / 1024 / 1024:.2f} MB")
    print(f"Uncompressed size:     {stats['total_bytes_uncompressed'] / 1024 / 1024:.2f} MB")

    if stats['total_bytes_compressed'] > 0:
        ratio = stats['total_bytes_uncompressed'] / stats['total_bytes_compressed']
        print(f"Compression ratio:     {ratio:.2f}x")

    print()
    print(f"Hard pinned:           {stats['pinned_hard']}")
    print(f"Soft pinned:           {stats['pinned_soft']}")
    print(f"Unpinned:              {stats['unpinned']}")

    if stats['oldest_access']:
        print()
        print(f"Oldest access:         {stats['oldest_access'].strftime('%Y-%m-%d %H:%M')}")
        print(f"Newest access:         {stats['newest_access'].strftime('%Y-%m-%d %H:%M')}")


def prune_by_age(max_age_days: int, dry_run: bool = False) -> dict:
    """
    Prune cache entries older than max_age_days.
    Respects pin levels: hard pins never pruned, soft pins pruned last.

    Returns: {'deleted': int, 'skipped_hard': int, 'skipped_soft': int, 'bytes_freed': int}
    """
    init_ccm_cache()
    cutoff = datetime.now() - timedelta(days=max_age_days)
    results = {'deleted': 0, 'skipped_hard': 0, 'skipped_soft': 0, 'bytes_freed': 0}

    keys = list_all_keys()

    for key in keys:
        meta = get_metadata(key)
        if not meta:
            continue

        # Check pin level
        pin_level = meta.get('pinned', {}).get('level', 'none')
        if pin_level == 'hard':
            results['skipped_hard'] += 1
            continue

        # Check age
        last_access = meta.get('last_access_at', '')
        if not last_access:
            continue

        try:
            access_time = datetime.fromisoformat(last_access)
            if access_time >= cutoff:
                continue  # Not old enough
        except ValueError:
            continue

        # Soft pins: only prune if explicitly requested
        if pin_level == 'soft':
            results['skipped_soft'] += 1
            continue

        # Delete
        bytes_size = meta.get('bytes_uncompressed', 0)
        if dry_run:
            print(f"Would delete: {key[:20]}... ({bytes_size} bytes, last access: {last_access[:10]})")
        else:
            if delete_cached_content(key):
                results['bytes_freed'] += bytes_size
        results['deleted'] += 1

    return results


def prune_by_size(max_size_mb: int, dry_run: bool = False) -> dict:
    """
    Prune cache to stay under max_size_mb.
    Deletes oldest unpinned first, then soft-pinned if needed.
    Never deletes hard-pinned.

    Returns: {'deleted': int, 'bytes_freed': int, 'final_size_mb': float}
    """
    init_ccm_cache()
    max_bytes = max_size_mb * 1024 * 1024
    results = {'deleted': 0, 'bytes_freed': 0, 'final_size_mb': 0.0}

    # Get current size
    stats = get_cache_stats()
    current_size = stats['total_bytes_compressed']

    if current_size <= max_bytes:
        results['final_size_mb'] = current_size / 1024 / 1024
        return results

    # Build sorted list by last_access (oldest first), grouped by pin level
    unpinned = []
    soft_pinned = []

    for key in list_all_keys():
        meta = get_metadata(key)
        if not meta:
            continue

        pin_level = meta.get('pinned', {}).get('level', 'none')
        if pin_level == 'hard':
            continue  # Never delete hard pins

        last_access = meta.get('last_access_at', '')
        bytes_size = meta.get('bytes_uncompressed', 0)

        entry = (key, last_access, bytes_size)
        if pin_level == 'soft':
            soft_pinned.append(entry)
        else:
            unpinned.append(entry)

    # Sort by access time (oldest first)
    unpinned.sort(key=lambda x: x[1])
    soft_pinned.sort(key=lambda x: x[1])

    # Delete unpinned first, then soft-pinned
    to_delete = unpinned + soft_pinned

    for key, last_access, bytes_size in to_delete:
        if current_size <= max_bytes:
            break

        if dry_run:
            print(f"Would delete: {key[:20]}... ({bytes_size} bytes)")
        else:
            if delete_cached_content(key):
                current_size -= bytes_size
                results['bytes_freed'] += bytes_size
        results['deleted'] += 1

    results['final_size_mb'] = current_size / 1024 / 1024
    return results


def gc_unreferenced(dry_run: bool = False) -> dict:
    """
    Garbage collect unreferenced cache entries.
    Scans all session JSONLs to find referenced keys, deletes orphans.
    Respects pin levels.

    Returns: {'deleted': int, 'bytes_freed': int, 'sessions_scanned': int}
    """
    init_ccm_cache()
    results = {'deleted': 0, 'bytes_freed': 0, 'sessions_scanned': 0}

    # Find all referenced keys
    referenced_keys = set()
    sessions = find_all_sessions()

    for session_path in sessions:
        keys = extract_stub_keys_from_session(session_path)
        referenced_keys.update(keys)
        results['sessions_scanned'] += 1

    print(f"Scanned {results['sessions_scanned']} sessions, found {len(referenced_keys)} referenced keys")

    # Check all cached keys
    for key in list_all_keys():
        if key in referenced_keys:
            continue  # Referenced, keep it

        meta = get_metadata(key)
        if not meta:
            continue

        # Check pin level
        pin_level = meta.get('pinned', {}).get('level', 'none')
        if pin_level in ('hard', 'soft'):
            continue  # Pinned items are never GC'd

        bytes_size = meta.get('bytes_uncompressed', 0)

        if dry_run:
            print(f"Would delete orphan: {key[:20]}... ({bytes_size} bytes)")
        else:
            if delete_cached_content(key):
                results['bytes_freed'] += bytes_size
        results['deleted'] += 1

    return results


def pin_key(key: str, level: str, reason: str = '') -> bool:
    """Pin a cache key at specified level."""
    init_ccm_cache()

    if level not in ('soft', 'hard'):
        print(f"Error: Invalid pin level '{level}'. Use 'soft' or 'hard'.")
        return False

    meta = get_metadata(key)
    if not meta:
        print(f"Error: Key not found: {key}")
        return False

    if update_pin(key, level, reason):
        print(f"Pinned {key[:20]}... as {level}")
        if reason:
            print(f"  Reason: {reason}")
        return True

    return False


def unpin_key(key: str) -> bool:
    """Remove pin from a cache key."""
    init_ccm_cache()

    meta = get_metadata(key)
    if not meta:
        print(f"Error: Key not found: {key}")
        return False

    if update_pin(key, 'none', ''):
        print(f"Unpinned {key[:20]}...")
        return True

    return False


def list_pins() -> list[dict]:
    """List all pinned cache entries."""
    init_ccm_cache()
    pins = []

    for key in list_all_keys():
        meta = get_metadata(key)
        if not meta:
            continue

        pin_info = meta.get('pinned', {})
        level = pin_info.get('level', 'none')
        if level in ('soft', 'hard'):
            pins.append({
                'key': key,
                'level': level,
                'reason': pin_info.get('reason', ''),
                'pinned_at': pin_info.get('pinned_at', ''),
                'bytes': meta.get('bytes_uncompressed', 0),
                'source_tool': meta.get('source', {}).get('tool_name', 'unknown')
            })

    return pins


def main():
    parser = argparse.ArgumentParser(
        description='CCM Cache Pruning Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --stats                          Show cache statistics
  %(prog)s --max-age-days 30 --dry-run      Preview age-based pruning
  %(prog)s --max-size-mb 500                Prune to stay under 500MB
  %(prog)s --gc-unreferenced                Remove orphaned cache entries
  %(prog)s --pin sha256:abc... --level hard Pin a key permanently
  %(prog)s --list-pins                      Show all pinned entries
"""
    )

    parser.add_argument('--stats', action='store_true',
                        help='Show cache statistics')
    parser.add_argument('--max-age-days', type=int, metavar='N',
                        help='Prune entries older than N days')
    parser.add_argument('--max-size-mb', type=int, metavar='N',
                        help='Prune to keep cache under N megabytes')
    parser.add_argument('--gc-unreferenced', action='store_true',
                        help='Remove cache entries not referenced by any session')
    parser.add_argument('--pin', metavar='KEY',
                        help='Pin a cache key (requires --level)')
    parser.add_argument('--level', choices=['soft', 'hard'],
                        help='Pin level for --pin command')
    parser.add_argument('--reason', default='',
                        help='Reason for pinning (optional)')
    parser.add_argument('--unpin', metavar='KEY',
                        help='Remove pin from a cache key')
    parser.add_argument('--list-pins', action='store_true',
                        help='List all pinned cache entries')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be deleted without deleting')

    args = parser.parse_args()

    # Handle commands
    if args.stats:
        show_stats()
        return

    if args.list_pins:
        pins = list_pins()
        if not pins:
            print("No pinned entries.")
            return

        print(f"{'Level':<6} {'Bytes':>10} {'Tool':<10} {'Key':<25} Reason")
        print("-" * 70)
        for p in pins:
            key_short = p['key'][:22] + '...' if len(p['key']) > 25 else p['key']
            print(f"{p['level']:<6} {p['bytes']:>10} {p['source_tool']:<10} {key_short:<25} {p['reason'][:20]}")
        return

    if args.pin:
        if not args.level:
            print("Error: --pin requires --level (soft or hard)")
            sys.exit(1)
        success = pin_key(args.pin, args.level, args.reason)
        sys.exit(0 if success else 1)

    if args.unpin:
        success = unpin_key(args.unpin)
        sys.exit(0 if success else 1)

    if args.max_age_days is not None:
        if args.dry_run:
            print(f"Dry run: pruning entries older than {args.max_age_days} days")
        results = prune_by_age(args.max_age_days, dry_run=args.dry_run)
        print(f"\nResults: {results['deleted']} deleted, "
              f"{results['skipped_hard']} hard-pinned skipped, "
              f"{results['skipped_soft']} soft-pinned skipped, "
              f"{results['bytes_freed'] / 1024:.1f} KB freed")
        return

    if args.max_size_mb is not None:
        if args.dry_run:
            print(f"Dry run: pruning to stay under {args.max_size_mb} MB")
        results = prune_by_size(args.max_size_mb, dry_run=args.dry_run)
        print(f"\nResults: {results['deleted']} deleted, "
              f"{results['bytes_freed'] / 1024 / 1024:.2f} MB freed, "
              f"final size: {results['final_size_mb']:.2f} MB")
        return

    if args.gc_unreferenced:
        if args.dry_run:
            print("Dry run: garbage collecting unreferenced entries")
        results = gc_unreferenced(dry_run=args.dry_run)
        print(f"\nResults: {results['deleted']} orphans deleted, "
              f"{results['bytes_freed'] / 1024:.1f} KB freed")
        return

    # No command specified
    parser.print_help()


if __name__ == '__main__':
    main()
