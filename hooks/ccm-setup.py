#!/usr/bin/env python3
"""
CCM Setup Hook - runs on session initialization.

Triggered via Setup hook event when Claude starts with --init, --init-only, or --maintenance.
Performs lightweight initialization tasks that should happen at session start.

Tasks:
1. Verify thinking proxy is running
2. Clean expired cache entries
3. Validate CLI patches are applied
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add hooks dir to path
sys.path.insert(0, str(Path(__file__).parent))


def check_proxy_running() -> tuple[bool, str]:
    """Check if thinking proxy is running."""
    try:
        proxy_script = Path(__file__).parent / 'thinking-proxy.py'
        result = subprocess.run(
            [sys.executable, str(proxy_script), 'status'],
            capture_output=True,
            text=True,
            timeout=5
        )
        running = 'running' in result.stdout.lower()
        return running, result.stdout.strip()
    except Exception as e:
        return False, str(e)


def clean_expired_cache(max_age_hours: int = 24) -> tuple[int, int]:
    """Remove cache entries older than max_age_hours.

    Returns: (files_removed, bytes_freed)
    """
    cache_dir = Path.home() / '.claude' / 'cache'
    if not cache_dir.exists():
        return 0, 0

    cutoff = datetime.now() - timedelta(hours=max_age_hours)
    cutoff_ts = cutoff.timestamp()

    files_removed = 0
    bytes_freed = 0

    # Clean legacy cache files (8-char hex)
    for f in cache_dir.iterdir():
        if f.is_file() and len(f.name) == 8:
            try:
                stat = f.stat()
                if stat.st_mtime < cutoff_ts:
                    bytes_freed += stat.st_size
                    f.unlink()
                    files_removed += 1
            except OSError:
                pass

    # Clean CCM blobs directory
    blobs_dir = cache_dir / 'ccm' / 'blobs'
    if blobs_dir.exists():
        for prefix_dir in blobs_dir.iterdir():
            if prefix_dir.is_dir():
                for blob in prefix_dir.iterdir():
                    try:
                        stat = blob.stat()
                        if stat.st_mtime < cutoff_ts:
                            bytes_freed += stat.st_size
                            blob.unlink()
                            files_removed += 1
                    except OSError:
                        pass

    return files_removed, bytes_freed


def check_patches() -> dict:
    """Check if CLI patches are applied."""
    try:
        patch_script = Path(__file__).parent / 'patch-autocompact.py'
        result = subprocess.run(
            [sys.executable, str(patch_script), '--check'],
            capture_output=True,
            text=True,
            timeout=10
        )
        # Parse status output
        status = {}
        if 'Status:' in result.stderr:
            status_line = result.stderr.split('Status:')[1].strip()
            for item in status_line.split(';'):
                item = item.strip()
                if ':' in item:
                    key, val = item.split(':', 1)
                    status[key.strip()] = val.strip()
        return status
    except Exception as e:
        return {'error': str(e)}


def main():
    """Run setup tasks and output results."""
    results = {
        'ccm_setup': True,
        'tasks': []
    }

    # Task 1: Check proxy
    proxy_ok, proxy_msg = check_proxy_running()
    results['tasks'].append({
        'name': 'thinking_proxy',
        'ok': proxy_ok,
        'message': proxy_msg
    })

    # Task 2: Clean cache (only remove very old entries on setup)
    files_removed, bytes_freed = clean_expired_cache(max_age_hours=48)
    results['tasks'].append({
        'name': 'cache_cleanup',
        'ok': True,
        'message': f'Removed {files_removed} expired entries ({bytes_freed:,} bytes)'
    })

    # Task 3: Check patches
    patch_status = check_patches()
    needs_patch = any('NEEDS PATCH' in str(v) for v in patch_status.values())
    results['tasks'].append({
        'name': 'cli_patches',
        'ok': not needs_patch,
        'message': 'All patches applied' if not needs_patch else f'Some patches needed: {patch_status}'
    })

    # Output JSON for hook response
    # Setup hooks should return {} to allow, or {"decision": "block", "reason": ...} to show message
    if all(t['ok'] for t in results['tasks']):
        # All OK - pass through silently
        print('{}')
    else:
        # Some issues - report but don't block
        issues = [t for t in results['tasks'] if not t['ok']]
        issue_msgs = [f"- {t['name']}: {t['message']}" for t in issues]
        reason = f"CCM Setup notices:\n" + "\n".join(issue_msgs)
        print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == '__main__':
    main()
