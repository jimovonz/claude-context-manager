#!/usr/bin/env python3
"""
Context monitor - warns at each 10% increment.
Runs on UserPromptSubmit, injects warning into context.
"""

import json
import sys
from pathlib import Path

# Config
MAX_TOKENS = 200000
WARNING_INCREMENT = 10  # Warn at each 10% increment
CHARS_PER_TOKEN = 4  # Rough estimate

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

            # Text blocks
            if block_type == 'text':
                total += len(block.get('text', ''))

            # Tool use - count name and input
            elif block_type == 'tool_use':
                total += len(block.get('name', ''))
                inp = block.get('input', {})
                if isinstance(inp, dict):
                    # Serialize input to approximate token count
                    total += len(json.dumps(inp, separators=(',', ':')))

            # Tool result - count content
            elif block_type == 'tool_result':
                result = block.get('content', '')
                if isinstance(result, str):
                    total += len(result)
                elif isinstance(result, list):
                    total += extract_content_chars(result)

            # Thinking blocks (only counted for current turn, but estimate anyway)
            elif block_type == 'thinking':
                total += len(block.get('thinking', ''))

    return total

def estimate_context(session_path):
    """Estimate current context usage as percentage."""
    try:
        with open(session_path) as f:
            lines = f.readlines()

        # Only count content after last compaction
        last_compact = find_last_compaction(lines)
        recent_lines = lines[last_compact:]

        # Extract ONLY message.role and message.content - nothing else
        content_chars = 0
        for line in recent_lines:
            try:
                obj = json.loads(line)
                msg = obj.get('message', {})
                if not msg:
                    continue

                # Count role (small but part of payload)
                content_chars += len(msg.get('role', ''))

                # Count content
                content_chars += extract_content_chars(msg.get('content', ''))
            except:
                pass

        # ~4 chars per token (Claude's tokenizer is similar to GPT)
        estimated_tokens = content_chars / 4

        # Known overhead from /context: system prompt (3k) + tools (15.2k) + memory (1.3k) = 19.5k
        estimated_tokens += 19500

        return min(100, (estimated_tokens / MAX_TOKENS) * 100)
    except:
        return 0

def get_warning_level(pct):
    """Get current warning level (0, 10, 20, ... 90)."""
    return int(pct // WARNING_INCREMENT) * WARNING_INCREMENT

def get_last_warning(state_file):
    """Get last warning level from state file."""
    try:
        with open(state_file) as f:
            return int(f.read().strip())
    except:
        return 0

def set_last_warning(state_file, level):
    """Save last warning level."""
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(state_file, 'w') as f:
            f.write(str(level))
    except:
        pass

def main():
    input_data = json.load(sys.stdin)
    session_path = input_data.get('session_id', '')
    transcript_path = input_data.get('transcript_path', '')

    if not transcript_path:
        print('{}')
        return

    # Use transcript path to derive state file
    session_id = Path(transcript_path).stem
    state_file = Path.home() / '.claude' / 'state' / f'{session_id}-context-level'

    # Estimate current context
    pct = estimate_context(transcript_path)
    current_level = get_warning_level(pct)
    last_level = get_last_warning(state_file)

    # Only warn if we've crossed a new threshold
    if current_level > last_level and current_level >= 50:
        set_last_warning(state_file, current_level)

        if current_level >= 80:
            urgency = "⚠️ CRITICAL"
            action = "Run /purge NOW or compaction is imminent!"
        elif current_level >= 70:
            urgency = "⚠️ WARNING"
            action = "Consider running /purge soon."
        else:
            urgency = "📊 NOTE"
            action = "/purge available if needed."

        warning = f"{urgency}: Context at ~{int(pct)}%. {action}"
        print(warning)
    else:
        # Update state even if no warning (tracks decreases after purge)
        if current_level != last_level:
            set_last_warning(state_file, current_level)

if __name__ == '__main__':
    main()
