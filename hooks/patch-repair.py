#!/usr/bin/env python3
"""Auto-repair CLI patches when patterns no longer match.

Uses Claude to analyze the current CLI source and identify new patterns
for patches that have stopped matching after a CLI update.

Usage:
    patch-repair.py              # Repair failed patches, output patched CLI path
    patch-repair.py --check      # Check which patches need repair (dry-run)
    patch-repair.py --verbose    # Show detailed progress

Called automatically by the 'c' launcher when patching fails.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).parent
PATCHER = HOOKS_DIR / 'patch-autocompact.py'

# Stable anchors that persist across CLI versions
ANCHORS = {
    'autocompact_env': 'AUTOCOMPACT_PCT_OVERRIDE',
    'blocking_env': 'CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE',
    'hook_error': 'hook_blocking_error',
    'hook_reply': 'hook_blocking_reply',
    'edited_file': 'edited_text_file',
}

# Patch descriptions with intent and search strategy
PATCH_SPECS = {
    'trigger': {
        'name': 'Autocompact Trigger',
        'intent': 'The env var CLAUDE_AUTOCOMPACT_PCT_OVERRIDE sets a custom threshold. '
                  'The CLI uses Math.min(default, override) which prevents RAISING the threshold. '
                  'Fix: change Math.min to Math.max so the override can increase the threshold.',
        'anchor': 'AUTOCOMPACT_PCT_OVERRIDE',
        'context_before': 200,
        'context_after': 800,
        'example_old': 'Math.min(A,B)}}return B}',
        'example_new': 'Math.max(A,B)}}return B}',
        'constraint': 'Same length replacement. Only change "min" to "max".',
    },
    'display': {
        'name': 'Display Calculation',
        'intent': 'The /context display calculates the autocompact buffer as FUNC()-CONST where CONST=13000 (or 10000). '
                  'This shows a hardcoded buffer instead of the actual threshold. '
                  'Fix: remove the subtraction of CONST, just call the threshold function directly.',
        'anchor': 'AUTOCOMPACT_PCT_OVERRIDE',
        'context_before': 0,
        'context_after': 2000,
        'search_hint': 'Look for a ternary expression: VAR=BOOL?FUNC()-CONST:void 0 where CONST is 13000 or 10000',
        'example_old': 'c=a?s2A()-NC0:void 0',
        'example_new': 'c=a?Xk2()     :void 0',
        'constraint': 'Same length replacement. Pad with spaces if needed.',
    },
    'pct_base': {
        'name': 'Percentage Base',
        'intent': 'Threshold percentage is calculated from AVAILABLE context (total-overhead) not TOTAL. '
                  'Fix: replace the variable with the full context calculation (CONTEXT(MODEL(),OTHER())).',
        'anchor': 'AUTOCOMPACT_PCT_OVERRIDE',
        'context_before': 0,
        'context_after': 1500,
        'search_hint': 'Look for Math.floor(SINGLE_LETTER_VAR*(OTHER_VAR/100)) near the env var',
        'example_old': 'Math.floor(A*(G/100))',
        'example_new': 'Math.floor(Xk2()*(G/100))',
        'constraint': 'NOT same length. Replace the first variable with the threshold function call.',
    },
    'hook_reply': {
        'name': 'Hook Reply',
        'intent': 'Hook blocking responses use the word "error" in message types and display text. '
                  'Fix: replace "error" with "reply" in all hook blocking message strings.',
        'anchor': 'hook_blocking_error',
        'anchor_alt': 'hook_blocking_reply',
        'context_before': 50,
        'context_after': 50,
        'constraint': 'Same length replacements. Already defined in HOOK_PATTERNS constant.',
    },
    'file_injection': {
        'name': 'File Injection',
        'intent': 'When files are modified externally, the CLI injects the full diff into context via a template literal '
                  'containing ${A.snippet}. This can be enormous. '
                  'Fix: replace the template with a minimal notification string.',
        'anchor': 'edited_text_file',
        'context_before': 50,
        'context_after': 500,
        'search_hint': 'Look for case"edited_text_file":return FUNC([FUNC({content:`...${A.snippet}...`,...})])',
        'constraint': 'Replace the content template literal with a simple notification.',
    },
    'threshold': {
        'name': 'Threshold Values',
        'intent': 'Four threshold constants control when context warnings appear: '
                  'uL0=13000 (user warning), c97=20000 (context warning), p97=20000 (prompt warning), mL0=3000 (min buffer). '
                  'Fix: reduce to uL0=10000, c97=2000, p97=1000, mL0=3000 to trigger near autocompact.',
        'anchor': 'AUTOCOMPACT_PCT_OVERRIDE',
        'context_before': 0,
        'context_after': 3000,
        'search_hint': 'Look for var VAR=13000,VAR=20000,VAR=20000,VAR=3000 (a 4-variable declaration with these exact values)',
        'constraint': 'Same length replacement. Change values only: 13000→10000, 20000→02000, 20000→01000, 3000→3000.',
    },
    'blocking_limit': {
        'name': 'Blocking Limit',
        'intent': 'The context blocking limit is calculated as FUNC()-CONST which blocks at ~67%. '
                  'Fix: replace with just THRESHOLD_FUNC() to align with the autocompact threshold.',
        'anchor': 'CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE',
        'context_before': 200,
        'context_after': 100,
        'search_hint': 'Look for ,VAR=FUNC(...)-CONST,VAR=process.env.CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE',
        'constraint': 'Replace VAR=FUNC(...)-CONST with VAR=THRESHOLD_FUNC() (padding to same length with spaces).',
    },
}


def find_cli_js():
    """Find the Claude CLI js file."""
    result = subprocess.run(
        [sys.executable, str(PATCHER), '--find-cli'],
        capture_output=True, text=True
    )
    if result.returncode == 0 and result.stdout.strip():
        p = Path(result.stdout.strip())
        if p.exists():
            return p

    # Fallback: search common locations
    for base in [Path.home() / '.nvm', Path('/usr/lib'), Path('/usr/local/lib')]:
        for cli in base.rglob('**/claude/cli.js'):
            if cli.exists():
                return cli

    # Try which claude
    result = subprocess.run(['which', 'claude'], capture_output=True, text=True)
    if result.returncode == 0:
        claude_bin = Path(result.stdout.strip()).resolve()
        # Follow to the actual JS file
        cli_js = claude_bin.parent / 'cli.js'
        if cli_js.exists():
            return cli_js
        # npm global: ../lib/node_modules/@anthropic-ai/claude-code/cli.js
        cli_js = claude_bin.parent.parent / 'lib' / 'node_modules' / '@anthropic-ai' / 'claude-code' / 'cli.js'
        if cli_js.exists():
            return cli_js

    return None


def get_patch_status():
    """Run --check and parse which patches need repair.

    Output format: "Status: trigger: NEEDS PATCH; display: OK; ..."
    """
    result = subprocess.run(
        [sys.executable, str(PATCHER), '--check'],
        capture_output=True, text=True
    )
    output = result.stdout + result.stderr

    # Map from --check output names to our patch keys
    name_map = {
        'trigger': 'trigger',
        'display': 'display',
        'pct_base': 'pct_base',
        'hook_reply': 'hook_reply',
        'hook_is_error': None,  # Removed, ignore
        'file_injection': 'file_injection',
        'threshold': 'threshold',
        'blocking_limit': 'blocking_limit',
    }

    status = {}

    # Parse semicolon-separated format: "key: STATUS (details); key: STATUS; ..."
    # Strip "Status: " prefix if present
    status_line = output.strip()
    if 'Status:' in status_line:
        status_line = status_line.split('Status:', 1)[1].strip()

    for part in status_line.split(';'):
        part = part.strip()
        if ':' not in part:
            continue

        key_part, val_part = part.split(':', 1)
        key_part = key_part.strip()
        val_part = val_part.strip()

        # Match to our patch keys
        patch_key = name_map.get(key_part)
        if patch_key is None:
            continue

        if 'OK' in val_part:
            status[patch_key] = 'ok'
        elif 'NEEDS' in val_part:
            status[patch_key] = 'needs_patch'
        elif 'not found' in val_part:
            status[patch_key] = 'not_found'

    return status


def extract_context(content, anchor, before=200, after=800, anchor_alt=None):
    """Extract context window around an anchor string."""
    # Try primary anchor
    idx = content.find(anchor)
    if idx == -1 and anchor_alt:
        idx = content.find(anchor_alt)
        if idx != -1:
            # Already patched version found
            return None

    if idx == -1:
        return None

    start = max(0, idx - before)
    end = min(len(content), idx + len(anchor) + after)
    return content[start:end]


def build_repair_prompt(failed_patches, cli_content, cli_path):
    """Build a focused prompt for Claude to identify new patterns."""
    prompt_parts = [
        "You are analyzing a minified JavaScript CLI file to identify code patterns for patching.",
        f"CLI file: {cli_path}",
        "",
        "For each patch below, I provide:",
        "- The INTENT (what the patch should accomplish)",
        "- The CONTEXT (surrounding code from the CLI)",
        "- The OLD PATTERN (what used to work in previous versions)",
        "- CONSTRAINTS (length requirements, etc.)",
        "",
        "Your task: identify the EXACT current strings in the CLI that need to be replaced,",
        "and what they should be replaced WITH.",
        "",
        "Output ONLY a JSON object with this structure:",
        '{"patches": {"patch_name": {"old": "exact old string", "new": "exact new string", "found": true/false, "notes": "..."}, ...}}',
        "",
        "CRITICAL RULES:",
        "- 'old' must be an EXACT substring of the provided context",
        "- Same-length constraints must be respected",
        "- Variable names change between versions - match the CURRENT code, not the examples",
        "- If you cannot find the pattern, set found=false and explain in notes",
        "",
        "---",
        "",
    ]

    for patch_key, spec in failed_patches.items():
        anchor = spec.get('anchor', '')
        anchor_alt = spec.get('anchor_alt')
        context = extract_context(
            cli_content, anchor,
            spec.get('context_before', 200),
            spec.get('context_after', 800),
            anchor_alt
        )

        prompt_parts.append(f"## Patch: {spec['name']} (key: {patch_key})")
        prompt_parts.append(f"**Intent:** {spec['intent']}")
        if 'search_hint' in spec:
            prompt_parts.append(f"**Search hint:** {spec['search_hint']}")
        prompt_parts.append(f"**Old pattern example:** `{spec.get('example_old', 'N/A')}`")
        prompt_parts.append(f"**New pattern example:** `{spec.get('example_new', 'N/A')}`")
        prompt_parts.append(f"**Constraint:** {spec.get('constraint', 'None')}")
        prompt_parts.append("")

        if context:
            # Truncate to avoid token explosion
            if len(context) > 3000:
                context = context[:3000]
            prompt_parts.append(f"**Context ({len(context)} chars around '{anchor}'):**")
            prompt_parts.append(f"```javascript")
            prompt_parts.append(context)
            prompt_parts.append(f"```")
        else:
            prompt_parts.append(f"**Context:** Anchor '{anchor}' not found in CLI source.")
            # Try broader search
            broader = extract_context(cli_content, anchor[:20], 100, 2000) if len(anchor) > 20 else None
            if broader:
                prompt_parts.append(f"**Broader context ({len(broader)} chars):**")
                prompt_parts.append(f"```javascript")
                prompt_parts.append(broader[:2000])
                prompt_parts.append(f"```")

        prompt_parts.append("")
        prompt_parts.append("---")
        prompt_parts.append("")

    return '\n'.join(prompt_parts)


def call_claude(prompt, verbose=False):
    """Call Claude CLI in non-interactive mode."""
    if verbose:
        print("  Calling Claude for pattern analysis...", file=sys.stderr)

    cmd = ['claude', '--print', '-p', prompt]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
        if verbose:
            print(f"  Claude call failed: {result.stderr[:200]}", file=sys.stderr)
        return None

    return result.stdout


def parse_claude_response(response):
    """Extract JSON from Claude's response."""
    # Try to find JSON block
    json_match = re.search(r'```json\s*\n(.*?)\n```', response, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find raw JSON object
    json_match = re.search(r'\{[\s\S]*"patches"[\s\S]*\}', response)
    if json_match:
        # Find balanced braces
        text = response[json_match.start():]
        depth = 0
        end = 0
        for i, ch in enumerate(text):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end > 0:
            try:
                return json.loads(text[:end])
            except json.JSONDecodeError:
                pass

    return None


def apply_repairs(cli_content, repairs, verbose=False):
    """Apply repair patches to CLI content."""
    patched = cli_content
    applied = []
    failed = []

    for patch_key, repair in repairs.items():
        if not repair.get('found', False):
            if verbose:
                print(f"  {patch_key}: skipped (not found) - {repair.get('notes', '')}", file=sys.stderr)
            failed.append(patch_key)
            continue

        old = repair.get('old', '')
        new = repair.get('new', '')

        if not old or not new:
            failed.append(patch_key)
            continue

        if old not in patched:
            if verbose:
                print(f"  {patch_key}: old pattern not in CLI", file=sys.stderr)
                print(f"    old: {old[:80]}...", file=sys.stderr)
            failed.append(patch_key)
            continue

        # Check same-length constraint
        spec = PATCH_SPECS.get(patch_key, {})
        constraint = spec.get('constraint', '')
        if 'same length' in constraint.lower() and len(old) != len(new):
            if verbose:
                print(f"  {patch_key}: length mismatch ({len(old)} vs {len(new)}), padding", file=sys.stderr)
            if len(new) < len(old):
                new = new + ' ' * (len(old) - len(new))
            else:
                if verbose:
                    print(f"  {patch_key}: new pattern too long, cannot fix", file=sys.stderr)
                failed.append(patch_key)
                continue

        patched = patched.replace(old, new, 1)
        applied.append(patch_key)
        if verbose:
            print(f"  {patch_key}: applied", file=sys.stderr)

    return patched, applied, failed


def create_patched_mirror(cli_path, patched_content, verbose=False):
    """Create patched CLI mirror (same approach as patcher)."""
    mirror_dir = Path.home() / '.claude' / 'patched' / 'claude-code'
    mirror_dir.mkdir(parents=True, exist_ok=True)

    # Symlink everything except cli.js
    cli_dir = cli_path.parent
    for item in cli_dir.iterdir():
        mirror_item = mirror_dir / item.name
        if item.name == 'cli.js':
            continue
        if mirror_item.exists() or mirror_item.is_symlink():
            mirror_item.unlink()
        mirror_item.symlink_to(item)

    # Write patched cli.js
    patched_path = mirror_dir / 'cli.js'
    patched_path.write_text(patched_content)

    if verbose:
        print(f"  Mirror created: {mirror_dir}", file=sys.stderr)

    return patched_path


def main():
    verbose = '--verbose' in sys.argv or '-v' in sys.argv
    check_only = '--check' in sys.argv

    # Find CLI
    cli_path = find_cli_js()
    if not cli_path:
        print("Cannot find Claude CLI", file=sys.stderr)
        sys.exit(1)

    if verbose:
        print(f"CLI: {cli_path}", file=sys.stderr)

    # Read CLI content
    cli_content = cli_path.read_text()

    # Get patch status
    status = get_patch_status()
    if verbose:
        print(f"Patch status: {status}", file=sys.stderr)

    # Identify failed patches
    failed_patches = {}
    for key, state in status.items():
        if state in ('needs_patch', 'not_found') and key in PATCH_SPECS:
            failed_patches[key] = PATCH_SPECS[key]

    if not failed_patches:
        # Check if patches are already applied by looking for patched patterns
        # If --check wasn't helpful, try direct detection
        needs_repair = False
        if 'trigger' not in status:
            # Status parsing failed, check manually
            if 'Math.min' in cli_content and ANCHORS['autocompact_env'] in cli_content:
                ctx = extract_context(cli_content, ANCHORS['autocompact_env'], 200, 800)
                if ctx and 'Math.min' in ctx and 'parseFloat' in ctx:
                    failed_patches['trigger'] = PATCH_SPECS['trigger']
                    needs_repair = True

        if not needs_repair and not failed_patches:
            if verbose:
                print("All patches OK or already applied", file=sys.stderr)
            # Output existing patched path if it exists
            patched = Path.home() / '.claude' / 'patched' / 'claude-code' / 'cli.js'
            if patched.exists():
                print(str(patched))
            else:
                print(str(cli_path))
            sys.exit(0)

    print(f"[CCM] {len(failed_patches)} patch(es) need repair: {', '.join(failed_patches.keys())}", file=sys.stderr)

    if check_only:
        for key in failed_patches:
            print(f"  {key}: {PATCH_SPECS[key]['name']}", file=sys.stderr)
        sys.exit(1)

    # Build prompt
    prompt = build_repair_prompt(failed_patches, cli_content, cli_path)

    if verbose:
        print(f"  Prompt length: {len(prompt)} chars", file=sys.stderr)

    # Call Claude
    response = call_claude(prompt, verbose)
    if not response:
        print("[CCM] Failed to get repair response from Claude", file=sys.stderr)
        sys.exit(1)

    if verbose:
        print(f"  Response length: {len(response)} chars", file=sys.stderr)

    # Parse response
    repairs = parse_claude_response(response)
    if not repairs or 'patches' not in repairs:
        print("[CCM] Could not parse repair response", file=sys.stderr)
        if verbose:
            print(f"  Response: {response[:500]}", file=sys.stderr)
        sys.exit(1)

    # Apply repairs
    patched_content, applied, failed = apply_repairs(
        cli_content, repairs['patches'], verbose
    )

    if not applied:
        print(f"[CCM] No patches could be applied", file=sys.stderr)
        if failed:
            print(f"  Failed: {', '.join(failed)}", file=sys.stderr)
        # Fall through to use original
        print(str(cli_path))
        sys.exit(1)

    print(f"[CCM] Repaired {len(applied)} patch(es): {', '.join(applied)}", file=sys.stderr)
    if failed:
        print(f"[CCM] Could not repair: {', '.join(failed)}", file=sys.stderr)

    # Create patched mirror
    patched_path = create_patched_mirror(cli_path, patched_content, verbose)
    print(str(patched_path))
    sys.exit(0)


if __name__ == '__main__':
    main()
