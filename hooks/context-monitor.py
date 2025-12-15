#!/usr/bin/env python3
"""
Context monitor - warns when context usage crosses configured thresholds.
Runs on UserPromptSubmit, injects warning into context.

Configuration: Edit config.py to adjust thresholds or disable.
"""

import json
import sys
from pathlib import Path

# Load configuration
HOOKS_DIR = Path(__file__).parent
sys.path.insert(0, str(HOOKS_DIR))

# Defaults (overridden by config.py if present)
CONTEXT_MONITOR_ENABLED = True
CONTEXT_MAX_TOKENS = 200000
CONTEXT_WARN_THRESHOLDS = [50, 70, 80, 90]
CONTEXT_CHARS_PER_TOKEN = 4
CONTEXT_OVERHEAD_TOKENS = 19500

# Load from config
CONFIG_FILE = HOOKS_DIR / 'config.py'
if CONFIG_FILE.exists():
    _config = {}
    try:
        exec(CONFIG_FILE.read_text(), _config)
        for key in ['CONTEXT_MONITOR_ENABLED', 'CONTEXT_MAX_TOKENS',
                    'CONTEXT_WARN_THRESHOLDS', 'CONTEXT_CHARS_PER_TOKEN',
                    'CONTEXT_OVERHEAD_TOKENS']:
            if key in _config:
                globals()[key] = _config[key]
    except Exception:
        pass  # Use defaults on config error


def find_last_compaction(lines):
    """Find index of last compaction summary."""
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if '"isCompactSummary":true' in line or '"isCompactSummary": true' in line:
            return i
    return 0


def extract_content_chars(content):
    """Extract character count from message content (string or list of blocks)."""
    if isinstance(content, str):
        return len(content)

    if not isinstance(content, list):
        return 0

    total = 0
    for block in content:
        if isinstance(block, str):
            total += len(block)
        elif isinstance(block, dict):
            block_type = block.get('type', '')

            if block_type == 'text':
                total += len(block.get('text', ''))
            elif block_type == 'tool_use':
                total += len(block.get('name', ''))
                inp = block.get('input', {})
                if isinstance(inp, dict):
                    total += len(json.dumps(inp, separators=(',', ':')))
            elif block_type == 'tool_result':
                result = block.get('content', '')
                if isinstance(result, str):
                    total += len(result)
                elif isinstance(result, list):
                    total += extract_content_chars(result)
            elif block_type == 'thinking':
                total += len(block.get('thinking', ''))

    return total


def estimate_context(session_path):
    """Estimate current context usage as percentage."""
    try:
        with open(session_path) as f:
            lines = f.readlines()

        last_compact = find_last_compaction(lines)
        recent_lines = lines[last_compact:]

        content_chars = 0
        for line in recent_lines:
            try:
                obj = json.loads(line)
                msg = obj.get('message', {})
                if not msg:
                    continue
                content_chars += len(msg.get('role', ''))
                content_chars += extract_content_chars(msg.get('content', ''))
            except:
                pass

        estimated_tokens = content_chars / CONTEXT_CHARS_PER_TOKEN
        estimated_tokens += CONTEXT_OVERHEAD_TOKENS

        return min(100, (estimated_tokens / CONTEXT_MAX_TOKENS) * 100)
    except:
        return 0


def get_crossed_threshold(pct, last_threshold):
    """Find highest threshold crossed that's above last warning."""
    for threshold in sorted(CONTEXT_WARN_THRESHOLDS, reverse=True):
        if pct >= threshold > last_threshold:
            return threshold
    return None


def get_last_warning(state_file):
    """Get last warning threshold from state file."""
    try:
        with open(state_file) as f:
            return int(f.read().strip())
    except:
        return 0


def set_last_warning(state_file, level):
    """Save last warning threshold."""
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(state_file, 'w') as f:
            f.write(str(level))
    except:
        pass


def main():
    # Check if enabled
    if not CONTEXT_MONITOR_ENABLED:
        return

    input_data = json.load(sys.stdin)
    transcript_path = input_data.get('transcript_path', '')

    if not transcript_path:
        return

    session_id = Path(transcript_path).stem
    state_file = Path.home() / '.claude' / 'state' / f'{session_id}-context-level'

    pct = estimate_context(transcript_path)
    last_threshold = get_last_warning(state_file)
    crossed = get_crossed_threshold(pct, last_threshold)

    if crossed:
        set_last_warning(state_file, crossed)

        # Determine urgency based on threshold
        max_threshold = max(CONTEXT_WARN_THRESHOLDS)
        high_threshold = sorted(CONTEXT_WARN_THRESHOLDS)[-2] if len(CONTEXT_WARN_THRESHOLDS) > 1 else max_threshold

        if crossed >= max_threshold:
            urgency = "⚠️ CRITICAL"
            action = "Run /purge NOW or compaction is imminent!"
        elif crossed >= high_threshold:
            urgency = "⚠️ WARNING"
            action = "Consider running /purge soon."
        else:
            urgency = "📊 NOTE"
            action = "/purge available if needed."

        print(f"{urgency}: Context at ~{int(pct)}%. {action}")
    else:
        # Track decreases (e.g., after purge) to reset warning state
        current_max_crossed = 0
        for threshold in CONTEXT_WARN_THRESHOLDS:
            if pct >= threshold:
                current_max_crossed = threshold

        if current_max_crossed < last_threshold:
            set_last_warning(state_file, current_max_crossed)


if __name__ == '__main__':
    main()
