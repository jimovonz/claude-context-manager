#!/usr/bin/env python3
"""
PreCompact hook - materializes pin directives and outputs compaction instructions.

Before compaction:
1. Scans session for ccm:pin directives
2. Materializes them (caches pinned content, replaces with stubs)
3. Outputs custom instructions for compaction

This ensures pinned content survives compaction.

Configuration: Edit config.py to adjust default instructions or disable.
"""

import json
import sys
from pathlib import Path

# Load configuration
HOOKS_DIR = Path(__file__).parent
sys.path.insert(0, str(HOOKS_DIR))

# Default compaction instructions
DEFAULT_COMPACT_INSTRUCTIONS = """Focus on preserving:
- Current task context and objectives
- Key decisions made and their rationale
- Important file paths and code locations discovered
- Any pending actions or TODOs
- Error messages and debugging context being investigated
- Critical state (connections, configurations, credentials referenced)

Summarize completed work concisely. Prioritize actionable context over historical details.
Maintain enough context to continue the current task without re-reading files."""

# Try to load from config
COMPACT_INSTRUCTIONS = DEFAULT_COMPACT_INSTRUCTIONS
PRE_COMPACT_ENABLED = True

CONFIG_FILE = HOOKS_DIR / 'config.py'
CCM_STUB_THRESHOLD = 5000

if CONFIG_FILE.exists():
    _config = {}
    try:
        exec(CONFIG_FILE.read_text(), _config)
        if 'COMPACT_INSTRUCTIONS' in _config:
            COMPACT_INSTRUCTIONS = _config['COMPACT_INSTRUCTIONS']
        if 'PRE_COMPACT_ENABLED' in _config:
            PRE_COMPACT_ENABLED = _config['PRE_COMPACT_ENABLED']
        if 'CCM_STUB_THRESHOLD_BYTES' in _config:
            CCM_STUB_THRESHOLD = _config['CCM_STUB_THRESHOLD_BYTES']
    except Exception:
        pass


# Try to import CCM functions
try:
    from lib.ccm_cache import init_ccm_cache, store_content, build_ccm_stub
    CCM_AVAILABLE = True
except ImportError:
    CCM_AVAILABLE = False

# Import pin directive parsing from purge script
try:
    from claude_session_purge import parse_pin_directives, resolve_pin_targets, parse_line
    PURGE_AVAILABLE = True
except ImportError:
    PURGE_AVAILABLE = False


def materialize_pins(session_path: Path) -> int:
    """
    Materialize pin directives in session file before compaction.
    Returns count of pins materialized.
    """
    if not CCM_AVAILABLE or not PURGE_AVAILABLE:
        return 0

    if not session_path.exists():
        return 0

    init_ccm_cache()

    # Read session lines
    lines = []
    try:
        with open(session_path, 'r') as f:
            for line in f:
                obj, original = parse_line(line)
                lines.append((obj, original))
    except Exception:
        return 0

    # Parse and resolve pin directives
    directives = parse_pin_directives(lines)
    if not directives:
        return 0

    pin_targets = resolve_pin_targets(lines, directives, CCM_STUB_THRESHOLD)
    if not pin_targets:
        return 0

    # Materialize pins
    materialized = 0
    for i, (obj, original) in enumerate(lines):
        if i not in pin_targets:
            continue
        if not obj:
            continue

        content = obj.get('message', {}).get('content', [])
        if not isinstance(content, list):
            continue

        directive = pin_targets[i]
        modified = False

        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') != 'tool_result':
                continue

            result_content = block.get('content', '')
            if not isinstance(result_content, str):
                continue
            if len(result_content) <= CCM_STUB_THRESHOLD:
                continue

            # Cache content and replace with stub
            source = {
                'tool_name': 'unknown',
                'exit_code': 0,
                'session_path': str(session_path),
            }

            key = store_content(
                result_content,
                source=source,
                pin_level=directive.level,
                pin_reason=directive.reason
            )

            stub = build_ccm_stub(
                key,
                len(result_content),
                result_content.count('\n'),
                0,
                directive.level
            )

            block['content'] = stub
            modified = True
            materialized += 1

        if modified:
            lines[i] = (obj, None)  # Mark as modified

    # Write back if changes were made
    if materialized > 0:
        try:
            with open(session_path, 'w') as f:
                for obj, original in lines:
                    if obj is not None and original is None:
                        f.write(json.dumps(obj, separators=(',', ':')) + '\n')
                    elif original is not None:
                        f.write(original.rstrip('\n') + '\n')
        except Exception:
            return 0

    return materialized


def main():
    if not PRE_COMPACT_ENABLED:
        return

    # Try to read hook input for session path
    session_path = None
    try:
        input_data = json.load(sys.stdin)
        transcript_path = input_data.get('transcript_path', '')
        if transcript_path:
            session_path = Path(transcript_path)
    except Exception:
        pass

    # Materialize any pin directives before compaction
    if session_path:
        pins_materialized = materialize_pins(session_path)
        if pins_materialized > 0:
            # Include note about materialized pins in output
            print(f"[CCM: {pins_materialized} pin directive(s) materialized before compaction]")
            print()

    # Check for custom instructions file
    custom_file = Path.home() / '.claude' / 'compact-instructions.txt'

    if custom_file.exists():
        try:
            instructions = custom_file.read_text().strip()
            if instructions:
                print(instructions)
                return
        except Exception:
            pass

    # Fall back to configured/default instructions
    print(COMPACT_INSTRUCTIONS)


if __name__ == '__main__':
    main()
