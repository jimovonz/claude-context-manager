#!/usr/bin/env python3
"""
Claude Code Session Purge & Repair Tool

Safely reduces session file size by removing thinking blocks and truncating
large tool outputs while preserving structural integrity required by the Claude API.

Structural requirements:
1. parentUuid chain - messages form a linked list
2. tool_use → tool_result pairing - every tool_result needs matching tool_use
3. Compaction summaries - must never be deleted
"""

import json
import sys
import argparse
import shutil
import uuid
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

# Context budget defaults (tokens) - overridden if /context output found in session
CONTEXT_MAX_TOKENS = 200000
CONTEXT_SYSTEM_PROMPT = 3100
CONTEXT_SYSTEM_TOOLS = 15100
CONTEXT_MEMORY_FILES = 1000
CONTEXT_AUTOCOMPACT_BUFFER = 45000
BYTES_PER_TOKEN = 4


def parse_context_stats(session_path: Path) -> dict:
    """Extract most recent /context output from session file."""
    import re
    stats = {}

    # Patterns for /context output values
    patterns = {
        'system_prompt': r'System prompt: ([\d.]+)k',
        'system_tools': r'System tools: ([\d.]+)k',
        'memory_files': r'Memory files: ([\d,]+) tokens',
    }

    try:
        content = session_path.read_text()
        for key, pattern in patterns.items():
            matches = re.findall(pattern, content)
            if matches:
                val = matches[-1].replace(',', '')  # last occurrence
                if 'k' in pattern:
                    stats[key] = int(float(val) * 1000)
                else:
                    stats[key] = int(val)
    except Exception:
        pass

    return stats


def get_message_space(session_path: Path) -> tuple[int, bool]:
    """Calculate available message space. Returns (tokens, from_session)."""
    stats = parse_context_stats(session_path)

    if stats:
        sys_prompt = stats.get('system_prompt', CONTEXT_SYSTEM_PROMPT)
        sys_tools = stats.get('system_tools', CONTEXT_SYSTEM_TOOLS)
        mem_files = stats.get('memory_files', CONTEXT_MEMORY_FILES)
        space = CONTEXT_MAX_TOKENS - sys_prompt - sys_tools - mem_files - CONTEXT_AUTOCOMPACT_BUFFER
        return space, True

    # Fall back to defaults
    space = CONTEXT_MAX_TOKENS - CONTEXT_SYSTEM_PROMPT - CONTEXT_SYSTEM_TOOLS - CONTEXT_MEMORY_FILES - CONTEXT_AUTOCOMPACT_BUFFER
    return space, False


def find_current_session(cwd: Optional[str] = None) -> Optional[Path]:
    """
    Find the most recent session file for the given working directory.
    If cwd is None, uses current working directory.
    """
    if cwd is None:
        cwd = os.getcwd()

    # Convert CWD to project path format: /home/user/foo -> -home-user-foo
    project_path = cwd.replace('/', '-')
    if not project_path.startswith('-'):
        project_path = '-' + project_path

    sessions_dir = Path.home() / '.claude' / 'projects' / project_path

    if not sessions_dir.exists():
        return None

    # Find most recent .jsonl file (exclude backups and agent files)
    candidates = []
    for f in sessions_dir.glob('*.jsonl'):
        if '.backup' in f.name or f.name.startswith('agent-'):
            continue
        candidates.append((f.stat().st_mtime, f))

    if not candidates:
        return None

    # Return most recent
    candidates.sort(reverse=True)
    return candidates[0][1]


def parse_line(line: str) -> tuple[Optional[dict], str]:
    """Parse a JSONL line, returning (parsed_obj, original_line)"""
    line = line.rstrip('\n')
    if not line:
        return None, line
    try:
        return json.loads(line), line
    except json.JSONDecodeError:
        return None, line


def is_compaction_summary(obj: dict) -> bool:
    """Check if message is a compaction summary (protected)"""
    # isCompactSummary can be at top level OR inside message.content[0]
    if obj.get('isCompactSummary') == True:
        return True
    try:
        content = obj.get('message', {}).get('content', [])
        if isinstance(content, list) and len(content) > 0:
            return content[0].get('isCompactSummary') == True
    except:
        pass
    return False


def create_synthetic_compaction(
    session_id: str,
    cwd: str,
    version: str = "2.0.65",
    git_branch: str = "",
    summary_text: str = "Session context preserved for purging."
) -> dict:
    """
    Create a synthetic compaction summary that enables full purging.

    This creates a minimal compaction entry that:
    1. Has isCompactSummary=true to enable thinking block purging
    2. Uses a null parentUuid to be the chain root
    3. Contains a placeholder summary
    """
    return {
        "parentUuid": None,  # Root of the chain
        "isSidechain": False,
        "userType": "external",
        "cwd": cwd,
        "sessionId": session_id,
        "version": version,
        "gitBranch": git_branch,
        "type": "user",
        "message": {
            "role": "user",
            "content": f"This session is being continued from a previous conversation that ran out of context. The conversation is summarized below:\nAnalysis:\n{summary_text}\n\nSummary:\n1. Primary Request and Intent:\n- Session context preserved via synthetic compaction\n\n2. Current Work:\n- Continuing from previous context"
        },
        "isVisibleInTranscriptOnly": True,
        "isCompactSummary": True,
        "uuid": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    }


def inject_synthetic_compaction(lines: list[tuple[Optional[dict], str]], verbose: bool = False) -> bool:
    """
    Inject a synthetic compaction summary into a session that doesn't have one.
    Inserts after system messages, before the first user/assistant message.
    Returns True if injection was performed.
    """
    # Check if already has compaction
    if any(is_compaction_summary(obj) for obj, _ in lines if obj):
        if verbose:
            print("  Session already has compaction summary - skipping injection")
        return False

    # Find the insertion point: after system/file-history-snapshot messages, before first user/assistant
    insert_idx = 0
    last_system_uuid = None
    first_content_idx = None
    session_metadata = None

    for i, (obj, _) in enumerate(lines):
        if not obj:
            continue

        msg_type = obj.get('type', '')

        # Track session metadata from first message that has it
        if not session_metadata and obj.get('sessionId'):
            session_metadata = obj

        # System and file-history-snapshot messages come first
        if msg_type in ('system', 'file-history-snapshot'):
            insert_idx = i + 1
            if obj.get('uuid'):
                last_system_uuid = obj['uuid']
        elif msg_type in ('user', 'assistant'):
            first_content_idx = i
            break

    if not session_metadata:
        if verbose:
            print("  Cannot find session metadata - skipping injection")
        return False

    if first_content_idx is None:
        if verbose:
            print("  No user/assistant messages found - skipping injection")
        return False

    # Create synthetic compaction
    synthetic = create_synthetic_compaction(
        session_id=session_metadata.get('sessionId', str(uuid.uuid4())),
        cwd=session_metadata.get('cwd', '/'),
        version=session_metadata.get('version', '2.0.65'),
        git_branch=session_metadata.get('gitBranch', ''),
    )

    # Link synthetic to last system message
    if last_system_uuid:
        synthetic['parentUuid'] = last_system_uuid

    if verbose:
        print(f"  Creating synthetic compaction with uuid: {synthetic['uuid'][:8]}...")
        print(f"  Inserting at line {insert_idx} (after system messages)")

    # Update first content message's parentUuid to point to synthetic
    first_content_obj, _ = lines[first_content_idx]
    first_content_obj['parentUuid'] = synthetic['uuid']
    lines[first_content_idx] = (first_content_obj, None)  # Mark as modified

    # Insert synthetic at the correct position
    lines.insert(insert_idx, (synthetic, None))

    if verbose:
        print(f"  Updated first content message parentUuid to point to synthetic")

    return True


def get_tool_use_ids(obj: dict) -> set:
    """Extract all tool_use IDs from a message"""
    ids = set()
    try:
        content = obj.get('message', {}).get('content', [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'tool_use':
                    if 'id' in block:
                        ids.add(block['id'])
    except:
        pass
    return ids


def get_tool_result_ids(obj: dict) -> set:
    """Extract all tool_result IDs from a message"""
    ids = set()
    try:
        content = obj.get('message', {}).get('content', [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'tool_result':
                    if 'tool_use_id' in block:
                        ids.add(block['tool_use_id'])
    except:
        pass
    return ids


def analyze_session(filepath: Path, verbose: bool = False) -> dict:
    """Analyze session file and return statistics"""
    stats = {
        'total_lines': 0,
        'json_lines': 0,
        'non_json_lines': 0,
        'messages': {'user': 0, 'assistant': 0, 'system': 0, 'other': 0},
        'compaction_summaries': 0,
        'thinking_blocks': 0,
        'tool_uses': 0,
        'tool_results': 0,
        'large_tool_results': 0,
        'broken_parent_links': 0,
        'orphan_tool_results': 0,
        'orphan_tool_uses': 0,
        'total_bytes': filepath.stat().st_size,
        'thinking_bytes': 0,
        'tool_result_bytes': 0,
    }

    uuids = set()
    parent_uuids = []
    all_tool_use_ids = set()
    all_tool_result_ids = set()

    with open(filepath, 'r') as f:
        for line_num, line in enumerate(f, 1):
            stats['total_lines'] += 1
            obj, original = parse_line(line)

            if obj is None:
                stats['non_json_lines'] += 1
                continue

            stats['json_lines'] += 1

            # Track UUIDs
            if obj.get('uuid'):
                uuids.add(obj['uuid'])
            if obj.get('parentUuid'):
                parent_uuids.append((line_num, obj['parentUuid']))

            # Message types
            msg_type = obj.get('type', 'other')
            if msg_type in stats['messages']:
                stats['messages'][msg_type] += 1
            else:
                stats['messages']['other'] += 1

            # Compaction summaries
            if is_compaction_summary(obj):
                stats['compaction_summaries'] += 1
                if verbose:
                    print(f"  Line {line_num}: Compaction summary (protected)")

            # Content analysis
            content = obj.get('message', {}).get('content', [])
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue

                    block_type = block.get('type')

                    if block_type == 'thinking':
                        stats['thinking_blocks'] += 1
                        thinking_text = block.get('thinking', '')
                        stats['thinking_bytes'] += len(thinking_text)

                    elif block_type == 'tool_use':
                        stats['tool_uses'] += 1
                        if 'id' in block:
                            all_tool_use_ids.add(block['id'])

                    elif block_type == 'tool_result':
                        stats['tool_results'] += 1
                        result_content = block.get('content', '')
                        if isinstance(result_content, str):
                            stats['tool_result_bytes'] += len(result_content)
                            if len(result_content) > 5000:
                                stats['large_tool_results'] += 1
                        if 'tool_use_id' in block:
                            all_tool_result_ids.add(block['tool_use_id'])

    # Check parent links
    for line_num, parent_uuid in parent_uuids:
        if parent_uuid not in uuids:
            stats['broken_parent_links'] += 1
            if verbose:
                print(f"  Line {line_num}: Broken parentUuid -> {parent_uuid[:8]}...")

    # Check tool pairing
    stats['orphan_tool_results'] = len(all_tool_result_ids - all_tool_use_ids)
    stats['orphan_tool_uses'] = len(all_tool_use_ids - all_tool_result_ids)

    # Check adjacency - tool_result must immediately follow tool_use
    stats['non_adjacent_pairs'] = 0

    return stats


def repair_parent_chain(lines: list[tuple[Optional[dict], str]]) -> int:
    """Repair broken parentUuid links. Returns count of repairs."""
    # Build uuid index
    uuids = {}
    for i, (obj, _) in enumerate(lines):
        if obj and obj.get('uuid'):
            uuids[obj['uuid']] = i

    repairs = 0
    for i, (obj, original) in enumerate(lines):
        if not obj or not obj.get('parentUuid'):
            continue

        parent = obj['parentUuid']
        if parent not in uuids:
            # Find nearest previous line with a uuid
            for prev_i in range(i - 1, -1, -1):
                prev_obj, _ = lines[prev_i]
                if prev_obj and prev_obj.get('uuid'):
                    obj['parentUuid'] = prev_obj['uuid']
                    lines[i] = (obj, None)  # Mark as modified
                    repairs += 1
                    break

    return repairs


def repair_tool_pairing(lines: list[tuple[Optional[dict], str]], verbose: bool = False) -> int:
    """
    Repair broken tool_use/tool_result pairing by removing orphaned blocks.
    Returns count of repairs.
    """
    # First pass: collect all tool_use and tool_result IDs with their locations
    tool_uses = {}  # id -> (line_index, block_index)
    tool_results = {}  # tool_use_id -> (line_index, block_index)

    for i, (obj, _) in enumerate(lines):
        if not obj:
            continue
        content = obj.get('message', {}).get('content', [])
        if not isinstance(content, list):
            continue

        for j, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'tool_use' and 'id' in block:
                tool_uses[block['id']] = (i, j)
            elif block.get('type') == 'tool_result' and 'tool_use_id' in block:
                tool_results[block['tool_use_id']] = (i, j)

    # Find orphans
    orphan_results = set(tool_results.keys()) - set(tool_uses.keys())
    orphan_uses = set(tool_uses.keys()) - set(tool_results.keys())

    if verbose and orphan_results:
        print(f"  Orphan tool_results (no matching tool_use): {len(orphan_results)}")
    if verbose and orphan_uses:
        print(f"  Orphan tool_uses (no matching tool_result): {len(orphan_uses)}")

    # Remove orphan tool_results
    repairs = 0
    for orphan_id in orphan_results:
        line_i, block_i = tool_results[orphan_id]
        obj, _ = lines[line_i]
        if obj:
            content = obj.get('message', {}).get('content', [])
            if isinstance(content, list) and block_i < len(content):
                # Mark block for removal by setting to None
                content[block_i] = None
                repairs += 1
                if verbose:
                    print(f"    Removing orphan tool_result at line {line_i + 1}")

    # Remove orphan tool_uses (tool calls that never got responses)
    # But keep if it's the only content (to avoid empty content array)
    for orphan_id in orphan_uses:
        line_i, block_i = tool_uses[orphan_id]
        obj, _ = lines[line_i]
        if obj:
            content = obj.get('message', {}).get('content', [])
            if isinstance(content, list) and block_i < len(content):
                # Check if this is the only content
                non_none_blocks = sum(1 for b in content if b is not None)
                if non_none_blocks <= 1:
                    if verbose:
                        print(f"    Keeping orphan tool_use at line {line_i + 1} (only content)")
                    continue
                # Mark block for removal by setting to None
                content[block_i] = None
                repairs += 1
                if verbose:
                    print(f"    Removing orphan tool_use at line {line_i + 1}")

    # Clean up None blocks
    for i, (obj, _) in enumerate(lines):
        if not obj:
            continue
        content = obj.get('message', {}).get('content', [])
        if isinstance(content, list):
            new_content = [b for b in content if b is not None]
            if len(new_content) != len(content):
                obj['message']['content'] = new_content
                lines[i] = (obj, None)  # Mark as modified

    return repairs


def purge_session(
    filepath: Path,
    threshold: int = 5000,
    keep_thinking: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    repair_only: bool = False,
    inject_compaction: bool = False,
) -> dict:
    """
    Purge session file of thinking blocks and large tool outputs.
    Returns statistics about changes made.
    """
    results = {
        'thinking_removed': 0,
        'tool_results_truncated': 0,
        'bytes_saved': 0,
        'parent_links_repaired': 0,
        'tool_pairs_repaired': 0,
        'synthetic_compaction_injected': False,
        'original_size': filepath.stat().st_size,
        'new_size': 0,
    }

    # Read all lines
    lines = []
    with open(filepath, 'r') as f:
        for line in f:
            obj, original = parse_line(line)
            lines.append((obj, original))

    if verbose:
        print(f"Loaded {len(lines)} lines")

    # Check for compaction summary - affects what we can safely purge
    has_compaction = any(is_compaction_summary(obj) for obj, _ in lines if obj)

    # If no compaction and injection requested, create synthetic compaction
    if not has_compaction and inject_compaction:
        if verbose:
            print("Injecting synthetic compaction summary...")
        if inject_synthetic_compaction(lines, verbose):
            results['synthetic_compaction_injected'] = True
            has_compaction = True  # Now we have compaction

    if not has_compaction and not keep_thinking:
        if verbose:
            print("Note: No compaction summary - preserving thinking blocks (only truncating tool_results)")
            print("      Use --inject-compaction to enable full purging")
        keep_thinking = True  # Force safe mode for non-compacted sessions

    # Repair parent chain first
    if verbose:
        print("Repairing parentUuid chain...")
    results['parent_links_repaired'] = repair_parent_chain(lines)
    if verbose and results['parent_links_repaired']:
        print(f"  Repaired {results['parent_links_repaired']} broken links")

    # Repair tool pairing
    if verbose:
        print("Repairing tool_use/tool_result pairing...")
    results['tool_pairs_repaired'] = repair_tool_pairing(lines, verbose)
    if verbose and results['tool_pairs_repaired']:
        print(f"  Repaired {results['tool_pairs_repaired']} orphaned blocks")

    if not repair_only:
        # Process content
        if verbose:
            print("Processing content...")

        for i, (obj, original) in enumerate(lines):
            if not obj:
                continue

            # Skip compaction summaries
            if is_compaction_summary(obj):
                if verbose:
                    print(f"  Line {i + 1}: Skipping compaction summary")
                continue

            content = obj.get('message', {}).get('content', [])
            if not isinstance(content, list):
                continue

            modified = False
            new_content = []

            for block in content:
                if not isinstance(block, dict):
                    new_content.append(block)
                    continue

                block_type = block.get('type')

                # Remove thinking blocks entirely (skip adding to new_content)
                if block_type == 'thinking' and not keep_thinking:
                    thinking_len = len(block.get('thinking', ''))
                    if thinking_len > 20:  # Only if there's meaningful content to clear
                        results['thinking_removed'] += 1
                        results['bytes_saved'] += thinking_len
                        modified = True
                        if verbose:
                            print(f"  Line {i + 1}: Removing thinking block ({thinking_len} bytes)")
                    # Don't append block to new_content - removes it entirely
                    continue

                # Handle large tool_results based on position
                if block_type == 'tool_result':
                    result_content = block.get('content', '')
                    if isinstance(result_content, str) and len(result_content) > threshold:
                        original_len = len(result_content)
                        lines_from_end = len(lines) - i

                        if lines_from_end > 20:
                            # Old tool result (>20 lines back) - remove entirely
                            results['tool_results_truncated'] += 1
                            results['bytes_saved'] += original_len
                            modified = True
                            if verbose:
                                print(f"  Line {i + 1}: Removing old tool_result ({original_len} bytes, {lines_from_end} lines back)")
                            continue  # Skip adding to new_content
                        else:
                            # Recent tool result - truncate but keep
                            truncated = result_content[:500] + f"\n\n[TRUNCATED - original: {original_len} bytes]"
                            block['content'] = truncated
                            results['tool_results_truncated'] += 1
                            results['bytes_saved'] += original_len - len(truncated)
                            modified = True
                            if verbose:
                                print(f"  Line {i + 1}: Truncating tool_result ({original_len} -> {len(truncated)} bytes)")

                new_content.append(block)

            if modified:
                obj['message']['content'] = new_content
                lines[i] = (obj, None)  # Mark as modified

    if dry_run:
        print("\n[DRY RUN - no changes written]")
        results['new_size'] = results['original_size'] - results['bytes_saved']
        return results

    # Write output
    backup_path = filepath.with_suffix(filepath.suffix + f'.backup.{datetime.now().strftime("%Y%m%d_%H%M%S")}')
    shutil.copy2(filepath, backup_path)
    if verbose:
        print(f"Backup created: {backup_path}")

    with open(filepath, 'w') as f:
        for obj, original in lines:
            if obj is not None and original is None:
                # Modified object
                f.write(json.dumps(obj, separators=(',', ':')) + '\n')
            elif original is not None:
                # Unchanged line
                f.write(original + '\n')
            else:
                # Empty line
                f.write('\n')

    results['new_size'] = filepath.stat().st_size
    return results


def main():
    parser = argparse.ArgumentParser(
        description='Claude Code Session Purge & Repair Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  %(prog)s ~/.claude/projects/.../session.jsonl --analyze
  %(prog)s ~/.claude/projects/.../session.jsonl --repair-only
  %(prog)s ~/.claude/projects/.../session.jsonl --threshold 5000
  %(prog)s ~/.claude/projects/.../session.jsonl --dry-run --verbose
        '''
    )
    parser.add_argument('session_file', type=Path, nargs='?', help='Path to session JSONL file (optional if --current used)')
    parser.add_argument('--current', '-c', action='store_true', help='Find and use the current session for CWD')
    parser.add_argument('--analyze', action='store_true', help='Analyze file without making changes')
    parser.add_argument('--repair-only', action='store_true', help='Only repair structural issues, no purging')
    parser.add_argument('--threshold', type=int, default=5000, help='Truncate tool outputs larger than this (default: 5000)')
    parser.add_argument('--keep-thinking', action='store_true', help="Don't remove thinking blocks")
    parser.add_argument('--no-inject-compaction', action='store_true',
                        help='Disable automatic synthetic compaction injection (by default, sessions without compaction get one injected to enable full purging)')
    parser.add_argument('--dry-run', action='store_true', help='Report changes without writing')
    parser.add_argument('--verbose', '-v', action='store_true', help='Show detailed progress')

    args = parser.parse_args()

    # Resolve session file
    session_file = args.session_file
    if args.current:
        session_file = find_current_session()
        if not session_file:
            print("Error: Could not find current session for this directory", file=sys.stderr)
            sys.exit(1)
    elif not session_file:
        print("Error: Either provide a session file or use --current", file=sys.stderr)
        sys.exit(1)

    if not session_file.exists():
        print(f"Error: File not found: {session_file}", file=sys.stderr)
        sys.exit(1)

    print(f"Session: {session_file.name}")
    print(f"Size: {session_file.stat().st_size:,} bytes")
    print()

    if args.analyze:
        print("=== Analysis ===")
        stats = analyze_session(session_file, verbose=args.verbose)

        print(f"Lines: {stats['total_lines']} ({stats['json_lines']} JSON, {stats['non_json_lines']} other)")
        print(f"Messages: user={stats['messages']['user']}, assistant={stats['messages']['assistant']}, system={stats['messages']['system']}")
        print(f"Compaction summaries: {stats['compaction_summaries']} (protected)")
        print()
        print(f"Thinking blocks: {stats['thinking_blocks']} ({stats['thinking_bytes']:,} bytes)")
        print(f"Tool uses: {stats['tool_uses']}")
        print(f"Tool results: {stats['tool_results']} ({stats['large_tool_results']} large, {stats['tool_result_bytes']:,} bytes)")
        print()
        print("=== Integrity ===")
        print(f"Broken parentUuid links: {stats['broken_parent_links']}")
        print(f"Orphan tool_results (no matching tool_use): {stats['orphan_tool_results']}")
        print(f"Orphan tool_uses (no matching tool_result): {stats['orphan_tool_uses']}")

        if stats['broken_parent_links'] or stats['orphan_tool_results']:
            print()
            print("⚠️  Structural issues detected. Run with --repair-only or without --analyze to fix.")

        return

    print("=== Processing ===")
    results = purge_session(
        session_file,
        threshold=args.threshold,
        keep_thinking=args.keep_thinking,
        dry_run=args.dry_run,
        verbose=args.verbose,
        repair_only=args.repair_only,
        inject_compaction=not args.no_inject_compaction,
    )

    print()
    print("=== Results ===")
    if results.get('synthetic_compaction_injected'):
        print("Synthetic compaction: INJECTED (enables full purging)")
    if not args.repair_only:
        print(f"Thinking blocks emptied: {results['thinking_removed']}")
        print(f"Tool results truncated: {results['tool_results_truncated']}")
    print(f"Parent links repaired: {results['parent_links_repaired']}")
    print(f"Tool pairs repaired: {results['tool_pairs_repaired']}")
    print()

    saved_bytes = results['original_size'] - results['new_size']
    saved_tokens = saved_bytes // BYTES_PER_TOKEN
    message_space, from_session = get_message_space(session_file)
    pct_of_message_space = (saved_tokens / message_space * 100)

    source = "from /context" if from_session else "estimated"
    print(f"Saved: ~{saved_tokens:,} tokens ({pct_of_message_space:.0f}% of message space, {source})")
    print(f"       {saved_bytes:,} bytes")


if __name__ == '__main__':
    main()
