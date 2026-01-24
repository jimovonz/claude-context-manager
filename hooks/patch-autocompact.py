#!/usr/bin/env python3
"""
Patch Claude CLI.

Patches applied:

1. TRIGGER PATCH: Math.min → Math.max in threshold calculation
   The CLI has a bug where CLAUDE_AUTOCOMPACT_PCT_OVERRIDE uses Math.min which
   only allows LOWERING the threshold (larger buffer), not RAISING it (smaller buffer).

2. DISPLAY PATCH: Fix /context display to use patched threshold
   The display calculates buffer as `EHA()-bH0` (hardcoded default) instead of
   calling ET2() which respects the env var override.

3. HOOK REPLY PATCH: <error> → <reply> for hook block responses
   When hooks block tool calls, they return results via the "block" decision.
   The CLI displays these as <error> which is misleading since the hook succeeded.
   This patch changes the tag to <reply>.

4. FILE INJECTION PATCH: Prevent full file content in system-reminders
   When external processes modify files Claude has read, the CLI injects the
   entire diff into context via system-reminders. For large files or complete
   rewrites, this can be huge (2x file size) and cause 413 errors.
   This patch replaces the diff with a short notification.

5. THRESHOLD PATCH: Reduce warning/error buffer sizes
   Original: uL0=13000 (autocompact), c97=20000 (warning), p97=20000 (error)
   Patched:  uL0=10000 (5% of 200k), c97=2000 (1% before), p97=1000 (0.5% before)
   This ensures warnings only appear close to the autocompact trigger (~94%)
   instead of at ~83.5% with the default 20k buffers.

6. BLOCKING LIMIT PATCH: Align blocking with autocompact threshold
   Original: D=q3A()-mL0 (136k - 3k = 133k, ignores COMPACT_PCT setting)
   Patched:  D=xs2() (uses autocompact threshold, e.g., 190k with 95%)
   This prevents "Context limit reached" errors at ~67% when threshold is set higher.

Usage:
    # Check if patch is needed
    ./patch-autocompact.py --check

    # Apply patch in-place
    ./patch-autocompact.py --patch

    # Restore from backup
    ./patch-autocompact.py --restore

    # Get/create patched copy (for c command)
    ./patch-autocompact.py --get-patched
    # Returns path to patched CLI, creating if needed

    # Auto mode: patch if needed, then exec claude
    ./patch-autocompact.py --auto -- claude [args...]
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


# Cache file to avoid re-checking on every invocation
CACHE_DIR = Path.home() / ".claude" / "patch-cache"
CACHE_FILE = CACHE_DIR / "autocompact-patch.json"
PATCHED_DIR = Path.home() / ".claude" / "patched"

# Hook reply patch patterns - replace "error" with "reply" in hook blocking messages
# These are same-length replacements for safe sed-style patching
HOOK_PATTERNS = [
    # Message type (6 occurrences)
    ('"hook_blocking_error"', '"hook_blocking_reply"'),
    # Display text (2 occurrences)
    ('hook returned blocking error', 'hook returned blocking reply'),
    # System message (1 occurrence)
    ('hook blocking error from command', 'hook blocking reply from command'),
]

# Hook is_error patch - REMOVED (superseded by hook-level "None - " prefix in json_block)
# Kept as empty list for compatibility with check/apply logic references.
HOOK_IS_ERROR_PATTERNS = []

# File injection patch - replace diff injection with minimal notification
# The pattern spans two lines due to template literal with embedded newline
FILE_INJECTION_NOTIFICATION = 'Note: ${A.filename} was modified externally. Use Read tool if needed.'

# Threshold values patch - reduce warning/error buffers so they don't trigger until close to autocompact
# Original: uL0=13000 (autocompact buffer), c97=20000 (warning), p97=20000 (error), mL0=3000 (blocking)
# Patched:  uL0=10000 (5% of 200k), c97=2000 (warning 1% before), p97=1000 (error 0.5% before)
# Pattern matches: var VARNAME=13000,VARNAME=20000,VARNAME=20000,VARNAME=3000
THRESHOLD_PATTERN_RE = r'var\s+([a-zA-Z_$][a-zA-Z0-9_$]*)=13000,([a-zA-Z_$][a-zA-Z0-9_$]*)=20000,([a-zA-Z_$][a-zA-Z0-9_$]*)=20000,([a-zA-Z_$][a-zA-Z0-9_$]*)=3000'
THRESHOLD_PATCHED_RE = r'var\s+[a-zA-Z_$][a-zA-Z0-9_$]*=10000,[a-zA-Z_$][a-zA-Z0-9_$]*=2000,[a-zA-Z_$][a-zA-Z0-9_$]*=1000,[a-zA-Z_$][a-zA-Z0-9_$]*=3000'

# Blocking limit patch - align blocking with autocompact threshold instead of available context
# Original: D=q3A()-mL0 where q3A()=136k (available), mL0=3k → blocks at 133k (67% of 200k)
# Patched:  D=xs2() where xs2()=autocompact threshold → blocks at same point as autocompact
# Pattern: D=AVAILABLE_FUNC(...)-BLOCKING_CONST in ic() function
# Note: In 2.1.14+, the function call has args: kE(W,Nq()) instead of just func()
# The (?:[^)]*(?:\([^)]*\)[^)]*)*) handles one level of nested parentheses
BLOCKING_LIMIT_PATTERN_RE = r',([A-Za-z_$])=([a-zA-Z0-9_$]+)\((?:[^)]*(?:\([^)]*\)[^)]*)*)\)-([a-zA-Z0-9_$]+),([A-Za-z_$])=process\.env\.CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE'


def find_cli_path() -> Path | None:
    """Find the Claude CLI path."""
    # Try which first
    try:
        result = subprocess.run(
            ["which", "claude"], capture_output=True, text=True, check=True
        )
        claude_path = Path(result.stdout.strip())
        # Follow symlinks to get the actual file
        if claude_path.is_symlink():
            claude_path = claude_path.resolve()
        # Go from bin/claude to lib/.../cli.js
        # Typical: ~/.nvm/versions/node/v20.x.x/bin/claude -> ../lib/node_modules/@anthropic-ai/claude-code/cli.js
        if claude_path.name == "claude":
            # Check parent for lib
            lib_path = claude_path.parent.parent / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
            if lib_path.exists():
                return lib_path
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    # Try common locations
    common_paths = [
        Path.home() / ".nvm" / "versions" / "node",
        Path("/usr/local/lib/node_modules/@anthropic-ai/claude-code"),
        Path("/usr/lib/node_modules/@anthropic-ai/claude-code"),
    ]

    for base in common_paths:
        if base.exists():
            if base.name == "node":
                # NVM structure - find version dirs
                for version_dir in base.iterdir():
                    cli_path = version_dir / "lib" / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
                    if cli_path.exists():
                        return cli_path
            else:
                cli_path = base / "cli.js"
                if cli_path.exists():
                    return cli_path

    return None


def get_file_hash(path: Path) -> str:
    """Get SHA256 hash of file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def load_cache() -> dict:
    """Load patch cache."""
    if CACHE_FILE.exists():
        try:
            import json
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_cache(cache: dict):
    """Save patch cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    import json
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def find_autocompact_mathmin(content: str) -> tuple[int, int, str] | None:
    """
    Find the Math.min in the autocompact function using heuristics.

    Returns: (start_offset, end_offset, matched_string) or None
    """
    # Heuristic 1: Find AUTOCOMPACT_PCT_OVERRIDE reference
    env_pattern = r'AUTOCOMPACT_PCT_OVERRIDE'
    env_matches = list(re.finditer(env_pattern, content))

    if not env_matches:
        return None

    # For each env var reference, look for nearby Math.min
    for env_match in env_matches:
        env_pos = env_match.start()

        # Look in a window around the env var (the function it's in)
        # The function is typically within 500 chars before and after
        window_start = max(0, env_pos - 200)
        window_end = min(len(content), env_pos + 800)
        window = content[window_start:window_end]

        # Heuristic 2: Must have parseFloat nearby (parsing the env var value)
        if 'parseFloat' not in window:
            continue

        # Heuristic 3: Must have percentage calculation (G/100 or similar)
        if '/100' not in window and '/ 100' not in window:
            continue

        # Heuristic 4: Find Math.min(X,Y) pattern that's returned
        # The pattern is typically: Math.min(VAR,VAR)}}return VAR}
        # where VAR is a single letter (minified variable name)
        mathmin_pattern = r'Math\.min\(([A-Za-z_$][A-Za-z0-9_$]*),([A-Za-z_$][A-Za-z0-9_$]*)\)\}?\}?return\s+[A-Za-z_$][A-Za-z0-9_$]*\}'

        for match in re.finditer(mathmin_pattern, window):
            # Calculate absolute position
            abs_start = window_start + match.start()
            # Just the Math.min part
            mathmin_only = re.search(r'Math\.min\([^)]+\)', match.group())
            if mathmin_only:
                actual_start = window_start + match.start() + mathmin_only.start()
                actual_end = window_start + match.start() + mathmin_only.end()
                return (actual_start, actual_end, content[actual_start:actual_end])

        # Heuristic 5: Simpler pattern - just Math.min with two single-letter vars
        # followed by }} (closing the if block)
        simple_pattern = r'Math\.min\([A-Z],[A-Z]\)\}\}'
        for match in re.finditer(simple_pattern, window):
            mathmin_match = re.search(r'Math\.min\([^)]+\)', match.group())
            if mathmin_match:
                actual_start = window_start + match.start()
                actual_end = actual_start + mathmin_match.end()
                return (actual_start, actual_end, content[actual_start:actual_end])

    return None


def check_already_patched(content: str) -> tuple[bool, bool, bool, bool, bool, bool, bool, bool]:
    """Check if the file is already patched.

    Returns: (trigger_patched, display_patched, pct_base_patched, hook_reply_patched, hook_is_error_patched, file_injection_patched, threshold_patched, blocking_limit_patched)
    """
    trigger_patched = False
    display_patched = False
    pct_base_patched = False
    hook_reply_patched = False
    hook_is_error_patched = False
    file_injection_patched = False
    threshold_patched = False
    blocking_limit_patched = False

    # Check trigger patch: Math.max in autocompact function
    env_pattern = r'AUTOCOMPACT_PCT_OVERRIDE'
    env_matches = list(re.finditer(env_pattern, content))

    for env_match in env_matches:
        env_pos = env_match.start()
        window_start = max(0, env_pos - 200)
        window_end = min(len(content), env_pos + 800)
        window = content[window_start:window_end]

        if 'parseFloat' in window and '/100' in window:
            # Check for the specific patched pattern
            # Math.max(VAR,VAR)}} followed by return
            patched_pattern = r'Math\.max\([A-Za-z_$],[A-Za-z_$]\)\}\}return\s+[A-Za-z_$]\}'
            if re.search(patched_pattern, window):
                trigger_patched = True
                break

    # Check display patch: Xk2() instead of s2A()-NC0
    # Patched pattern: c=a?FUNC():void 0 (single function call, no subtraction)
    # - lowercase var, bool can be lowercase or uppercase, function call WITHOUT subtraction
    patched_display = r'[a-z]=[a-zA-Z]+\?[A-Za-z0-9_$]+\(\):void 0'
    # Unpatched has subtraction: c=a?s2A()-NC0:void 0
    unpatched_display = r'[a-z]=[a-zA-Z]+\?[A-Za-z0-9_$]+\(\)-[A-Za-z0-9_$]+:void 0'
    if re.search(patched_display, content) and not re.search(unpatched_display, content):
        display_patched = True

    # Check percentage base patch: CONTEXT(MODEL(),OTHER())*(G/100) instead of A*(G/100)
    # This ensures percentage is of total context (200k) not available (136k)
    pct_base_patched = False
    # Look for the patched pattern near AUTOCOMPACT - dynamically built
    total_context_call = find_total_context_call(content)
    if total_context_call:
        env_pattern = r'AUTOCOMPACT_PCT_OVERRIDE'
        for match in re.finditer(env_pattern, content):
            window_start = max(0, match.start() - 100)
            window_end = min(len(content), match.start() + 500)
            window = content[window_start:window_end]
            # Check for patched pattern: TOTAL_CONTEXT_CALL*(G/100)
            escaped_call = re.escape(total_context_call)
            if re.search(escaped_call + r'\*\([A-Z]/100\)', window):
                pct_base_patched = True
                break

    # Check hook reply patch: "error" → "reply" in hook blocking messages
    # Patched if all error patterns are replaced with reply patterns
    hook_reply_patched = True
    for error_pat, reply_pat in HOOK_PATTERNS:
        if error_pat in content:
            hook_reply_patched = False
            break
        # Verify the replacement exists (unless it's a version without this pattern)
        # We only require the first pattern (message type) to exist

    # Check hook is_error patch: is_error:!0 → is_error:!1 for hook blocks
    # Patched if original pattern is gone and replacement exists
    hook_is_error_patched = True
    for error_pat, reply_pat in HOOK_IS_ERROR_PATTERNS:
        if error_pat in content:
            hook_is_error_patched = False
            break

    # Check file injection patch: minimal notification instead of full diff
    file_injection_patched = check_file_injection_patched(content)

    # Check threshold patch: reduced warning/error buffers
    # Patched if new pattern exists and old pattern doesn't
    threshold_patched = (
        re.search(THRESHOLD_PATCHED_RE, content) is not None and
        re.search(THRESHOLD_PATTERN_RE, content) is None
    )

    # Check blocking limit patch: D=xs2() instead of D=q3A()-mL0
    # Patched if the original pattern (FUNC()-CONST) is replaced with just the threshold func call
    threshold_func = find_threshold_function_name(content)
    if threshold_func:
        # Original pattern: D=q3A()-mL0,W=process.env.CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE
        # Patched pattern: D=xs2(),W=process.env.CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE
        blocking_match = re.search(BLOCKING_LIMIT_PATTERN_RE, content)
        if blocking_match:
            # Still has the subtraction pattern - not patched
            blocking_limit_patched = False
        else:
            # Check if the patched pattern exists (D=THRESHOLD_FUNC(),W=...)
            patched_blocking_pattern = rf',([A-Za-z_$])={re.escape(threshold_func)}\(\),([A-Za-z_$])=process\.env\.CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE'
            blocking_limit_patched = bool(re.search(patched_blocking_pattern, content))

    return (trigger_patched, display_patched, pct_base_patched, hook_reply_patched, hook_is_error_patched, file_injection_patched, threshold_patched, blocking_limit_patched)


def find_display_pattern(content: str) -> tuple[int, int, str, str] | None:
    """
    Find the display threshold calculation pattern.

    Looking for: c=a?s2A()-NC0:void 0
    (where variable names are minified and vary between versions)

    The pattern is: VAR=BOOL?FUNC()-CONST:void 0
    where VAR is lowercase, BOOL can be lowercase or uppercase, FUNC is effective context func,
    CONST is hardcoded buffer constant (NC0=13000).

    Returns: (start_offset, end_offset, matched_string, threshold_func) or None
    """
    # Pattern: c=a?s2A()-NC0:void 0
    # - lowercase var (c)
    # - bool var can be lowercase (a) or uppercase (AA) depending on version
    # - function call minus constant (s2A()-NC0)
    # - void 0 fallback
    # Updated pattern to handle both uppercase and lowercase bool vars
    pattern = r'([a-z])=([a-zA-Z]+)\?([A-Za-z0-9_$]+)\(\)-([A-Za-z0-9_$]+):void 0'

    match = re.search(pattern, content)
    if match:
        # Verify this is the autocompact display pattern by checking context
        # It should be near "Autocompact buffer" string or in context display code
        func_name = match.group(3)  # e.g., s2A
        const_name = match.group(4)  # e.g., NC0

        # Verify the constant is the autocompact buffer (NC0=13000)
        const_pattern = rf'{re.escape(const_name)}=13000'
        if not re.search(const_pattern, content):
            # Also check for the patched threshold value (10000)
            const_pattern_patched = rf'{re.escape(const_name)}=10000'
            if not re.search(const_pattern_patched, content):
                return None

        # We need to find what threshold function to use
        # It's typically defined near AUTOCOMPACT_PCT_OVERRIDE
        threshold_func = find_threshold_function_name(content)
        if threshold_func:
            return (match.start(), match.end(), match.group(), threshold_func)

    return None


def find_total_context_call(content: str) -> str | None:
    """
    Dynamically find the total context function call by tracing through the code.

    Structure in CLI:
        function THRESHOLD_FUNC(){
            let A=AVAILABLE_FUNC(), ...
            ...Math.floor(A*(G/100))...
        }
        function AVAILABLE_FUNC(){
            let A=MODEL_FUNC(), Q=SYS_FUNC(A);
            return CONTEXT_FUNC(A,OTHER_FUNC())-Q
        }

    We want: CONTEXT_FUNC(MODEL_FUNC(),OTHER_FUNC()) for total context.
    """
    # Step 1: Find threshold function (contains AUTOCOMPACT_PCT_OVERRIDE)
    env_pattern = r'AUTOCOMPACT_PCT_OVERRIDE'
    env_match = re.search(env_pattern, content)
    if not env_match:
        return None

    # Step 2: Find the function definition containing env var
    # Look backwards for "function NAME(){"
    window_start = max(0, env_match.start() - 500)
    window = content[window_start:env_match.start()]
    func_def_match = re.search(r'function\s+([A-Za-z0-9_$]+)\(\)\{[^}]*$', window)
    if not func_def_match:
        return None

    threshold_func = func_def_match.group(1)

    # Step 3: Find "let A=FUNC()" at start of threshold function to get available context func
    # Pattern: function THRESHOLD(){let A=AVAILABLE()
    threshold_pattern = rf'function\s+{re.escape(threshold_func)}\(\)\{{let\s+([A-Z])=([A-Za-z0-9_$]+)\(\)'
    threshold_match = re.search(threshold_pattern, content)
    if not threshold_match:
        return None

    available_func = threshold_match.group(2)

    # Step 4: Find available context function definition
    # Pattern: function AVAILABLE(){let A=MODEL(),Q=SYS(A);return CONTEXT(A,OTHER())-Q}
    available_pattern = rf'function\s+{re.escape(available_func)}\(\)\{{let\s+([A-Z])=([A-Za-z0-9_$]+)\(\),([A-Z])=([A-Za-z0-9_$]+)\(\1\);return\s+([A-Za-z0-9_$]+)\(\1,([A-Za-z0-9_$]+)\(\)\)-\3\}}'
    available_match = re.search(available_pattern, content)
    if not available_match:
        return None

    model_func = available_match.group(2)      # H3
    context_func = available_match.group(5)    # Lz
    other_func = available_match.group(6)      # Iw

    # Step 5: Build total context call
    return f"{context_func}({model_func}(),{other_func}())"


def find_pct_base_pattern(content: str) -> tuple[int, int, str, str, str] | None:
    """
    Find the percentage base pattern in the autocompact calculation.

    Looking for: Math.floor(A*(G/100)) near AUTOCOMPACT_PCT_OVERRIDE
    where A is the available context variable.

    Dynamically discovers the total context function call to use as replacement.

    Returns: (start_offset, end_offset, matched_string, replacement_base, pct_var) or None
    """
    # First, dynamically find the total context call
    total_context_call = find_total_context_call(content)
    if not total_context_call:
        return None

    # Find the AUTOCOMPACT_PCT_OVERRIDE location
    env_pattern = r'AUTOCOMPACT_PCT_OVERRIDE'
    env_matches = list(re.finditer(env_pattern, content))

    for env_match in env_matches:
        env_pos = env_match.start()
        # Look forward for the Math.floor pattern
        window_start = env_pos
        window_end = min(len(content), env_pos + 500)
        window = content[window_start:window_end]

        # Pattern: Math.floor(A*(G/100)) - simple single-letter vars
        pct_pattern = r'Math\.floor\(([A-Z])\*\(([A-Z])/100\)\)'
        match = re.search(pct_pattern, window)
        if match:
            base_var = match.group(1)  # A (available context)
            pct_var = match.group(2)   # G (percentage)

            abs_start = window_start + match.start()
            abs_end = window_start + match.end()
            return (abs_start, abs_end, match.group(), total_context_call, pct_var)

    return None


def find_threshold_function_name(content: str) -> str | None:
    """Find the name of the threshold calculation function (contains env var check)."""
    # Look for function definition containing AUTOCOMPACT_PCT_OVERRIDE
    # Pattern: function NAME(){...AUTOCOMPACT_PCT_OVERRIDE...
    env_pattern = r'AUTOCOMPACT_PCT_OVERRIDE'
    env_matches = list(re.finditer(env_pattern, content))

    for env_match in env_matches:
        env_pos = env_match.start()
        # Look backwards for function definition
        window_start = max(0, env_pos - 300)
        window = content[window_start:env_pos]

        # Find the function name
        func_pattern = r'function ([A-Za-z0-9_$]+)\(\)\{[^}]*$'
        func_match = re.search(func_pattern, window)
        if func_match:
            return func_match.group(1)

    return None


def find_file_injection_pattern(content: str) -> tuple[int, int, str, str, str] | None:
    """
    Find the file injection pattern in the CLI.

    Looking for: case"edited_text_file":return XX([YY({content:`Note: ${A.filename} was modified...
    ...${A.snippet}`,isMeta:!0})]);

    Where XX and YY are minified function names (e.g., V5, F5, $0, C0).

    Returns: (start_offset, end_offset, matched_string, func1, func2) or None
    """
    # The pattern includes a template literal with embedded newline
    # We need to match from case"edited_text_file" to the closing ])]);
    # Use flexible function name matching for minified code
    pattern = r'case"edited_text_file":return ([A-Z0-9]+)\(\[([A-Z0-9]+)\(\{content:`[^`]*\$\{A\.snippet\}`,[^)]+\)\]\)'

    match = re.search(pattern, content, re.DOTALL)
    if match:
        return (match.start(), match.end(), match.group(), match.group(1), match.group(2))

    return None


def check_file_injection_patched(content: str) -> bool:
    """Check if file injection is already patched (uses minimal notification)."""
    # Check if the patched pattern exists - use flexible function names
    patched_pattern = r'case"edited_text_file":return [A-Z0-9]+\(\[[A-Z0-9]+\(\{content:`Note: \$\{A\.filename\} was modified externally\. Use Read tool if needed\.`'
    return bool(re.search(patched_pattern, content))


def apply_patch(cli_path: Path, dry_run: bool = False, create_backup: bool = True) -> tuple[bool, str]:
    """
    Apply all patches:
    1. Math.min → Math.max (trigger)
    2. Display calculation (EHA()-bH0 → ET2())
    3. Percentage base (A*(G/100) → TOTAL_CONTEXT*(G/100))
    4. Hook reply (<error> → <reply>)
    5. Hook is_error (is_error:!0 → is_error:!1)
    6. File injection (full diff → minimal notification)
    7. Threshold values (warning/error buffers reduced)
    8. Blocking limit (D=q3A()-mL0 → D=xs2())

    Returns: (success, message)
    """
    content = cli_path.read_text()
    messages = []

    # Check current patch status
    trigger_patched, display_patched, pct_base_patched, hook_reply_patched, hook_is_error_patched, file_injection_patched, threshold_patched, blocking_limit_patched = check_already_patched(content)

    if trigger_patched and display_patched and pct_base_patched and hook_reply_patched and hook_is_error_patched and file_injection_patched and threshold_patched and blocking_limit_patched:
        return (True, "Already fully patched")

    # Create backup if it doesn't exist (only for in-place patching)
    if create_backup:
        backup_path = cli_path.with_suffix(".js.autocompact-backup")
        if not backup_path.exists() and not dry_run:
            shutil.copy2(cli_path, backup_path)

    # Patch 1: Math.min → Math.max (trigger logic)
    if not trigger_patched:
        result = find_autocompact_mathmin(content)
        if result is None:
            return (False, "Could not find autocompact Math.min pattern")

        start, end, matched = result
        replacement = matched.replace("Math.min", "Math.max")

        if dry_run:
            messages.append(f"Would patch trigger: {matched} → {replacement}")
        else:
            content = content[:start] + replacement + content[end:]
            messages.append(f"Patched trigger: Math.min→Math.max")
    else:
        messages.append("Trigger already patched")

    # Patch 2: Display calculation (s2A()-NC0 → Xk2())
    if not display_patched:
        display_result = find_display_pattern(content)
        if display_result:
            start, end, matched, threshold_func = display_result
            # Replace: c=a?s2A()-NC0:void 0 → c=a?Xk2():void 0
            # Extract variable names from the matched pattern
            import re as re2
            m = re2.search(r'([a-z])=([a-zA-Z]+)\?', matched)
            if not m:
                messages.append(f"Display pattern matched but couldn't extract vars from: {matched[:50]}")
            else:
                var1, var2 = m.group(1), m.group(2)
                replacement = f'{var1}={var2}?{threshold_func}():void 0'

                if dry_run:
                    messages.append(f"Would patch display: {matched[:30]}... → {replacement}")
                else:
                    content = content[:start] + replacement + content[end:]
                    messages.append(f"Patched display: →{threshold_func}()")
        else:
            messages.append("Display pattern not found (may be different CLI version)")
    else:
        messages.append("Display already patched")

    # Patch 3: Percentage base (A*(G/100) → NO(p3())*(G/100))
    if not pct_base_patched:
        pct_result = find_pct_base_pattern(content)
        if pct_result:
            start, end, matched, context_func, pct_var = pct_result
            replacement = f'Math.floor({context_func}*({pct_var}/100))'

            if dry_run:
                messages.append(f"Would patch pct base: {matched} → {replacement}")
            else:
                content = content[:start] + replacement + content[end:]
                messages.append(f"Patched pct base: →{context_func}")
        else:
            messages.append("Pct base pattern not found (may be different CLI version)")
    else:
        messages.append("Pct base already patched")

    # Patch 4: Hook reply (error → reply in hook blocking messages)
    if not hook_reply_patched:
        patched_any = False
        for error_pat, reply_pat in HOOK_PATTERNS:
            if error_pat in content:
                if dry_run:
                    messages.append(f"Would patch: {error_pat[:30]}...→{reply_pat[:30]}...")
                else:
                    content = content.replace(error_pat, reply_pat)
                    patched_any = True
        if patched_any:
            messages.append("Patched hook reply: error→reply")
        elif not dry_run:
            messages.append("Hook reply patterns not found (may be different CLI version)")
    else:
        messages.append("Hook reply already patched")

    # Patch 5: Hook is_error (is_error:!0 → is_error:!1 for hook blocks)
    if not hook_is_error_patched:
        patched_any = False
        for error_pat, reply_pat in HOOK_IS_ERROR_PATTERNS:
            if error_pat in content:
                if dry_run:
                    messages.append(f"Would patch is_error: {error_pat[:40]}...→{reply_pat[:40]}...")
                else:
                    content = content.replace(error_pat, reply_pat)
                    patched_any = True
        if patched_any:
            messages.append("Patched hook is_error: !0→!1")
        elif not dry_run:
            messages.append("Hook is_error patterns not found (may be different CLI version)")
    else:
        messages.append("Hook is_error already patched")

    # Patch 6: File injection (full diff → minimal notification)
    if not file_injection_patched:
        injection_result = find_file_injection_pattern(content)
        if injection_result:
            start, end, matched, func1, func2 = injection_result
            # Replace with minimal notification, preserving the minified function names
            replacement = f'case"edited_text_file":return {func1}([{func2}({{content:`{FILE_INJECTION_NOTIFICATION}`,isMeta:!0}})])'

            if dry_run:
                messages.append(f"Would patch file injection: {len(matched)} chars → {len(replacement)} chars")
            else:
                content = content[:start] + replacement + content[end:]
                messages.append("Patched file injection: diff→notification")
        else:
            messages.append("File injection pattern not found (may be different CLI version)")
    else:
        messages.append("File injection already patched")

    # Patch 7: Threshold values (reduced warning/error buffers)
    if not threshold_patched:
        threshold_match = re.search(THRESHOLD_PATTERN_RE, content)
        if threshold_match:
            # Extract variable names from the match to preserve them
            var1, var2, var3, var4 = threshold_match.groups()
            old_str = threshold_match.group(0)
            new_str = f'var {var1}=10000,{var2}=2000,{var3}=1000,{var4}=3000'
            if dry_run:
                messages.append(f"Would patch thresholds: {old_str} → {new_str}")
            else:
                content = content[:threshold_match.start()] + new_str + content[threshold_match.end():]
                messages.append("Patched thresholds: warning/error buffers reduced")
        else:
            messages.append("Threshold pattern not found (may be different CLI version)")
    else:
        messages.append("Thresholds already patched")

    # Patch 8: Blocking limit (D=q3A()-mL0 → D=xs2())
    if not blocking_limit_patched:
        threshold_func = find_threshold_function_name(content)
        if threshold_func:
            blocking_match = re.search(BLOCKING_LIMIT_PATTERN_RE, content)
            if blocking_match:
                var_d, avail_func, blocking_const, var_w = blocking_match.groups()
                old_str = blocking_match.group(0)
                # Replace D=q3A()-mL0 with D=xs2() (uses autocompact threshold)
                new_str = f',{var_d}={threshold_func}(),{var_w}=process.env.CLAUDE_CODE_BLOCKING_LIMIT_OVERRIDE'
                if dry_run:
                    messages.append(f"Would patch blocking limit: {old_str} → {new_str}")
                else:
                    content = content[:blocking_match.start()] + new_str + content[blocking_match.end():]
                    messages.append(f"Patched blocking limit: {avail_func}()-{blocking_const}→{threshold_func}()")
            else:
                messages.append("Blocking limit pattern not found (may be different CLI version)")
        else:
            messages.append("Could not find threshold function for blocking limit patch")
    else:
        messages.append("Blocking limit already patched")

    if dry_run:
        return (True, "; ".join(messages))

    # Write patched content
    cli_path.write_text(content)

    # Verify
    verify_content = cli_path.read_text()
    trigger_ok, display_ok, pct_ok, hook_ok, hook_is_error_ok, file_injection_ok, threshold_ok, blocking_ok = check_already_patched(verify_content)

    # Core patches: trigger, hook_reply, threshold, blocking_limit are essential
    # file_injection and hook_is_error are only required if the patterns exist in the CLI
    # Display and pct_base are optional (may not match on all versions)
    file_injection_required = find_file_injection_pattern(content) is not None
    file_injection_check = file_injection_ok or not file_injection_required

    # hook_is_error patterns don't exist in 2.1.14+ (structure changed)
    hook_is_error_required = any(pat in content for pat, _ in HOOK_IS_ERROR_PATTERNS)
    hook_is_error_check = hook_is_error_ok or not hook_is_error_required

    if trigger_ok and hook_ok and hook_is_error_check and file_injection_check and threshold_ok and blocking_ok:
        return (True, "; ".join(messages))
    else:
        # Restore backup if we created one
        if create_backup:
            backup_path = cli_path.with_suffix(".js.autocompact-backup")
            if backup_path.exists():
                shutil.copy2(backup_path, cli_path)
        return (False, "Patch verification failed, restored backup")


def get_patched_cli(cli_path: Path | None = None) -> tuple[Path | None, str]:
    """
    Get path to a patched CLI copy.

    Creates a mirrored directory structure in ~/.claude/patched/claude-code/ where:
    - All files/dirs except cli.js are symlinked to the original
    - cli.js is copied and patched

    This survives auto-updates since we run from a separate location.

    Returns: (patched_path, message) or (None, error_message)
    """
    if cli_path is None:
        cli_path = find_cli_path()

    if cli_path is None:
        return (None, "Could not find Claude CLI")

    if not cli_path.exists():
        return (None, f"CLI not found at {cli_path}")

    # Source directory (e.g., .../node_modules/@anthropic-ai/claude-code/)
    source_dir = cli_path.parent

    # Compute source hash to detect CLI updates
    source_hash = get_file_hash(cli_path)
    hash_marker = f"// CCM-PATCHED: {source_hash}"

    # Mirror directory in ~/.claude/patched/claude-code/
    mirror_dir = PATCHED_DIR / "claude-code"
    patched_path = mirror_dir / "cli.js"
    hash_file = mirror_dir / ".ccm-hash"

    # Check if existing mirror is valid and up-to-date
    if mirror_dir.exists() and patched_path.exists() and hash_file.exists():
        try:
            cached_hash = hash_file.read_text().strip()
            if cached_hash == source_hash:
                content = patched_path.read_text()
                if hash_marker in content[:500]:
                    trigger_ok, display_ok, pct_ok, hook_ok, hook_err_ok, file_inj_ok, threshold_ok, blocking_ok = check_already_patched(content)
                    if trigger_ok and hook_ok and threshold_ok and blocking_ok:  # Core patches OK
                        return (patched_path, "Using cached patched CLI")
        except Exception:
            pass  # Rebuild mirror

    # Clean up old mirror
    if mirror_dir.exists():
        shutil.rmtree(mirror_dir)

    # Create mirror directory
    mirror_dir.mkdir(parents=True, exist_ok=True)

    # Symlink all files/dirs except cli.js
    for item in source_dir.iterdir():
        if item.name == "cli.js":
            continue  # Will copy and patch this one
        link_path = mirror_dir / item.name
        link_path.symlink_to(item)

    # Copy cli.js with hash marker
    content = cli_path.read_text()
    if content.startswith('#!'):
        newline_idx = content.index('\n')
        content = content[:newline_idx + 1] + hash_marker + '\n' + content[newline_idx + 1:]
    else:
        content = hash_marker + '\n' + content
    patched_path.write_text(content)

    # Apply patches
    success, msg = apply_patch(patched_path, dry_run=False, create_backup=False)

    if success:
        # Write hash file for cache validation
        hash_file.write_text(source_hash)
        # Update skills config
        try:
            update_skills_config(quiet=True)
        except Exception:
            pass
        return (patched_path, f"Created patched CLI: {msg}")
    else:
        # Clean up failed patch
        shutil.rmtree(mirror_dir)
        return (None, f"Patch failed: {msg}")


def restore_backup(cli_path: Path) -> tuple[bool, str]:
    """Restore from backup."""
    backup_path = cli_path.with_suffix(".js.autocompact-backup")
    if not backup_path.exists():
        return (False, "No backup found")

    shutil.copy2(backup_path, cli_path)
    return (True, f"Restored from {backup_path}")


def update_skills_config(quiet: bool = False):
    """Update config.py with a comment listing all available skills."""
    commands_dir = Path.home() / '.claude' / 'commands'
    config_path = Path.home() / '.claude' / 'hooks' / 'config.py'

    if not commands_dir.exists():
        if not quiet:
            print(f"No commands directory found at {commands_dir}")
        return

    # Get all skill names from .md files
    skills = []
    for md_file in sorted(commands_dir.glob('*.md')):
        skill_name = md_file.stem
        # Read first line for description
        try:
            first_line = md_file.read_text().split('\n')[0].strip()
            # Clean up markdown
            first_line = first_line.lstrip('#').strip()
            if len(first_line) > 60:
                first_line = first_line[:57] + '...'
            skills.append((skill_name, first_line))
        except:
            skills.append((skill_name, ''))

    if not skills:
        if not quiet:
            print("No skills found in ~/.claude/commands/")
        return

    # Build the comment block
    comment_lines = [
        "# Available skills (from ~/.claude/commands/):",
    ]
    for name, desc in skills:
        if desc:
            comment_lines.append(f"#   {name}: {desc}")
        else:
            comment_lines.append(f"#   {name}")
    comment_block = '\n'.join(comment_lines)

    # Read config
    if not config_path.exists():
        if not quiet:
            print(f"Config not found at {config_path}")
        return

    content = config_path.read_text()

    # Find and update PRESERVED_SKILLS section
    # Look for existing comment block + PRESERVED_SKILLS
    pattern = r'(# Available skills \(from ~/\.claude/commands/\):.*?\n)?# Skills to preserve'
    replacement = f'{comment_block}\n# Skills to preserve'

    if '# Available skills (from ~/.claude/commands/):' in content:
        # Update existing comment block
        pattern = r'# Available skills \(from ~/\.claude/commands/\):.*?(?=\n# Skills to preserve)'
        new_content = re.sub(pattern, comment_block + '\n', content, flags=re.DOTALL)
    else:
        # Insert new comment block before PRESERVED_SKILLS
        new_content = content.replace(
            '# Skills to preserve',
            f'{comment_block}\n# Skills to preserve'
        )

    config_path.write_text(new_content)
    if not quiet:
        print(f"Updated {config_path} with {len(skills)} skills:")
        for name, desc in skills:
            print(f"  - {name}")


def main():
    parser = argparse.ArgumentParser(
        description="Patch Claude CLI"
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Check if patch is needed (exit 0 if patched/not needed, 1 if needs patch)"
    )
    parser.add_argument(
        "--patch", action="store_true",
        help="Apply the patch in-place"
    )
    parser.add_argument(
        "--restore", action="store_true",
        help="Restore from backup"
    )
    parser.add_argument(
        "--get-patched", action="store_true",
        help="Get path to patched CLI copy (creates if needed). Prints path to stdout."
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="Auto mode: patch if needed, no output unless error"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without making changes"
    )
    parser.add_argument(
        "--cli-path", type=Path,
        help="Override CLI path detection"
    )
    parser.add_argument(
        "--update-skills-config", action="store_true",
        help="Update config.py with list of available skills as comment"
    )
    parser.add_argument(
        "remainder", nargs="*",
        help="Command to run after patching (use with --auto)"
    )

    args = parser.parse_args()

    # Handle --update-skills-config
    if args.update_skills_config:
        update_skills_config()
        sys.exit(0)

    # --get-patched doesn't need cli_path validation upfront
    if args.get_patched:
        patched_path, msg = get_patched_cli(args.cli_path)
        if patched_path:
            print(patched_path)
            sys.exit(0)
        else:
            print(f"ERROR: {msg}", file=sys.stderr)
            sys.exit(1)

    # Find CLI for other modes
    cli_path = args.cli_path or find_cli_path()
    if cli_path is None:
        print("ERROR: Could not find Claude CLI", file=sys.stderr)
        sys.exit(1)

    if not cli_path.exists():
        print(f"ERROR: CLI not found at {cli_path}", file=sys.stderr)
        sys.exit(1)

    # Load cache
    cache = load_cache()
    file_hash = get_file_hash(cli_path)

    if args.check:
        content = cli_path.read_text()
        trigger_patched, display_patched, pct_base_patched, hook_reply_patched, hook_is_error_patched, file_injection_patched, threshold_patched, blocking_limit_patched = check_already_patched(content)

        # file_injection is only required if the pattern exists in the CLI
        file_injection_required = find_file_injection_pattern(content) is not None
        file_injection_ok = file_injection_patched or not file_injection_required

        if trigger_patched and display_patched and pct_base_patched and hook_reply_patched and hook_is_error_patched and file_injection_ok and threshold_patched and blocking_limit_patched:
            print(f"OK: Fully patched ({cli_path})")
            sys.exit(0)

        status = []
        if trigger_patched:
            status.append("trigger: OK")
        else:
            result = find_autocompact_mathmin(content)
            if result:
                status.append(f"trigger: NEEDS PATCH ({result[2]})")
            else:
                status.append("trigger: pattern not found")

        if display_patched:
            status.append("display: OK")
        else:
            display_result = find_display_pattern(content)
            if display_result:
                status.append("display: NEEDS PATCH")
            else:
                status.append("display: pattern not found")

        if pct_base_patched:
            status.append("pct_base: OK")
        else:
            pct_result = find_pct_base_pattern(content)
            if pct_result:
                status.append("pct_base: NEEDS PATCH")
            else:
                status.append("pct_base: pattern not found")

        if hook_reply_patched:
            status.append("hook_reply: OK")
        else:
            # Check if any error patterns exist
            has_error_pattern = any(error_pat in content for error_pat, _ in HOOK_PATTERNS)
            if has_error_pattern:
                status.append("hook_reply: NEEDS PATCH")
            else:
                status.append("hook_reply: pattern not found")

        if hook_is_error_patched:
            status.append("hook_is_error: OK")
        else:
            # Check if any is_error patterns exist
            has_is_error_pattern = any(error_pat in content for error_pat, _ in HOOK_IS_ERROR_PATTERNS)
            if has_is_error_pattern:
                status.append("hook_is_error: NEEDS PATCH")
            else:
                status.append("hook_is_error: pattern not found")

        if file_injection_patched:
            status.append("file_injection: OK")
        else:
            injection_result = find_file_injection_pattern(content)
            if injection_result:
                status.append("file_injection: NEEDS PATCH")
            else:
                status.append("file_injection: pattern not found")

        if threshold_patched:
            status.append("threshold: OK")
        else:
            if re.search(THRESHOLD_PATTERN_RE, content):
                status.append("threshold: NEEDS PATCH")
            else:
                status.append("threshold: pattern not found")

        if blocking_limit_patched:
            status.append("blocking_limit: OK")
        else:
            if re.search(BLOCKING_LIMIT_PATTERN_RE, content):
                status.append("blocking_limit: NEEDS PATCH")
            else:
                status.append("blocking_limit: pattern not found")

        print(f"Status: {'; '.join(status)}")
        # Core patches: trigger, hook_reply, threshold, blocking_limit
        # file_injection and hook_is_error only required if patterns exist in CLI
        # display and pct_base are optional (may not match on all versions)
        file_injection_required = find_file_injection_pattern(content) is not None
        hook_is_error_required = any(pat in content for pat, _ in HOOK_IS_ERROR_PATTERNS)
        file_injection_check = file_injection_patched or not file_injection_required
        hook_is_error_check = hook_is_error_patched or not hook_is_error_required
        fully_patched = trigger_patched and hook_reply_patched and hook_is_error_check and file_injection_check and threshold_patched and blocking_limit_patched
        sys.exit(0 if fully_patched else 1)

    elif args.restore:
        success, msg = restore_backup(cli_path)
        print(msg)
        # Clear cache
        if file_hash in cache:
            del cache[file_hash]
            save_cache(cache)
        sys.exit(0 if success else 1)

    elif args.patch or args.auto:
        # Check cache first
        if cache.get(file_hash) == "patched":
            if not args.auto:
                print(f"Already patched (cached): {cli_path}")
            # Continue to exec if remainder provided
        else:
            content = cli_path.read_text()
            trigger_patched, display_patched, pct_base_patched, hook_reply_patched, hook_is_error_patched, file_injection_patched, threshold_patched, blocking_limit_patched = check_already_patched(content)
            # Core patches are trigger, hook_reply, hook_is_error, threshold, blocking_limit
            # file_injection only required if pattern exists in CLI
            file_injection_required = find_file_injection_pattern(content) is not None
            file_injection_ok = file_injection_patched or not file_injection_required
            if trigger_patched and hook_reply_patched and hook_is_error_patched and file_injection_ok and threshold_patched and blocking_limit_patched:
                cache[file_hash] = "patched"
                save_cache(cache)
                if not args.auto:
                    print(f"Already patched: {cli_path}")
            else:
                success, msg = apply_patch(cli_path, dry_run=args.dry_run)
                if success:
                    if not args.dry_run:
                        cache[file_hash] = "patched"
                        save_cache(cache)
                    if not args.auto:
                        print(msg)
                else:
                    print(f"ERROR: {msg}", file=sys.stderr)
                    sys.exit(1)

        # If remainder provided, exec it
        if args.remainder:
            os.execvp(args.remainder[0], args.remainder)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
