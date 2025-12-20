#!/usr/bin/env python3
"""
Patch Claude CLI autocompact buffer logic.

Two patches are applied:

1. TRIGGER PATCH: Math.min → Math.max in threshold calculation
   The CLI has a bug where CLAUDE_AUTOCOMPACT_PCT_OVERRIDE uses Math.min which
   only allows LOWERING the threshold (larger buffer), not RAISING it (smaller buffer).

2. DISPLAY PATCH: Fix /context display to use patched threshold
   The display calculates buffer as `EHA()-bH0` (hardcoded default) instead of
   calling ET2() which respects the env var override.

Usage:
    # Check if patch is needed
    ./patch-autocompact.py --check

    # Apply patch
    ./patch-autocompact.py --patch

    # Restore from backup
    ./patch-autocompact.py --restore

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


def check_already_patched(content: str) -> tuple[bool, bool]:
    """Check if the file is already patched.

    Returns: (trigger_patched, display_patched)
    """
    trigger_patched = False
    display_patched = False

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
            patched_pattern = r'Math\.max\([A-Z],[A-Z]\)\}\}return\s+[A-Z]\}'
            if re.search(patched_pattern, window):
                trigger_patched = True
                break

    # Check display patch: look for the threshold function call pattern
    # Patched: c=s?ET2():void 0  (or similar function name)
    # We detect by absence of the hardcoded pattern: FUNC()-CONST:void 0
    # where CONST is the 13000 buffer constant (minified name varies)
    display_pattern = r',c=s\?[A-Za-z0-9_$]+\(\)-[A-Za-z0-9_$]+:void 0'
    if not re.search(display_pattern, content):
        # Pattern not found means either already patched or different structure
        # Check for patched pattern: c=s?FUNC():void 0 (just function call, no subtraction)
        patched_display = r',c=s\?[A-Za-z0-9_$]+\(\):void 0'
        if re.search(patched_display, content):
            display_patched = True

    return (trigger_patched, display_patched)


def find_display_pattern(content: str) -> tuple[int, int, str, str] | None:
    """
    Find the display threshold calculation pattern.

    Looking for: c=s?EHA()-bH0:void 0
    (where EHA and bH0 are minified names that vary)

    Returns: (start_offset, end_offset, matched_string, func_name) or None
    """
    # Pattern: ,c=s?FUNC()-CONST:void 0,
    # where FUNC is the available tokens function and CONST is 13000 buffer
    pattern = r',c=s\?([A-Za-z0-9_$]+)\(\)-[A-Za-z0-9_$]+:void 0,'

    match = re.search(pattern, content)
    if match:
        # We need to find what threshold function to use
        # It's typically defined near AUTOCOMPACT_PCT_OVERRIDE
        # Look for: function XXXX(){...AUTOCOMPACT_PCT_OVERRIDE...Math.max
        threshold_func = find_threshold_function_name(content)
        if threshold_func:
            return (match.start(), match.end(), match.group(), threshold_func)

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


def apply_patch(cli_path: Path, dry_run: bool = False) -> tuple[bool, str]:
    """
    Apply both patches:
    1. Math.min → Math.max (trigger)
    2. Display calculation fix

    Returns: (success, message)
    """
    content = cli_path.read_text()
    messages = []

    # Check current patch status
    trigger_patched, display_patched = check_already_patched(content)

    if trigger_patched and display_patched:
        return (True, "Already fully patched")

    # Create backup if it doesn't exist
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
            messages.append(f"Patched trigger: {matched} → {replacement}")
    else:
        messages.append("Trigger already patched")

    # Patch 2: Display calculation
    if not display_patched:
        display_result = find_display_pattern(content)
        if display_result:
            start, end, matched, threshold_func = display_result
            # Replace: ,c=s?EHA()-bH0:void 0, → ,c=s?ET2():void 0,
            # Extract the original function name to verify we found the right pattern
            replacement = f',c=s?{threshold_func}():void 0,'

            if dry_run:
                messages.append(f"Would patch display: {matched} → {replacement}")
            else:
                content = content[:start] + replacement + content[end:]
                messages.append(f"Patched display: {matched} → {replacement}")
        else:
            messages.append("Display pattern not found (may be different CLI version)")
    else:
        messages.append("Display already patched")

    if dry_run:
        return (True, "; ".join(messages))

    # Write patched content
    cli_path.write_text(content)

    # Verify
    verify_content = cli_path.read_text()
    trigger_ok, display_ok = check_already_patched(verify_content)

    if trigger_ok:
        if display_ok:
            return (True, "; ".join(messages))
        else:
            # Trigger worked, display didn't - still success
            return (True, "; ".join(messages) + " (display patch may need manual verification)")
    else:
        # Restore backup
        shutil.copy2(backup_path, cli_path)
        return (False, "Patch verification failed, restored backup")


def restore_backup(cli_path: Path) -> tuple[bool, str]:
    """Restore from backup."""
    backup_path = cli_path.with_suffix(".js.autocompact-backup")
    if not backup_path.exists():
        return (False, "No backup found")

    shutil.copy2(backup_path, cli_path)
    return (True, f"Restored from {backup_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Patch Claude CLI autocompact buffer logic"
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Check if patch is needed (exit 0 if patched/not needed, 1 if needs patch)"
    )
    parser.add_argument(
        "--patch", action="store_true",
        help="Apply the patch"
    )
    parser.add_argument(
        "--restore", action="store_true",
        help="Restore from backup"
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
        "remainder", nargs="*",
        help="Command to run after patching (use with --auto)"
    )

    args = parser.parse_args()

    # Find CLI
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
        trigger_patched, display_patched = check_already_patched(content)

        if trigger_patched and display_patched:
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
                status.append(f"display: NEEDS PATCH ({display_result[2][:50]}...)")
            else:
                status.append("display: pattern not found")

        print(f"Status: {'; '.join(status)}")
        sys.exit(1 if not (trigger_patched and display_patched) else 0)

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
            trigger_patched, display_patched = check_already_patched(content)
            if trigger_patched and display_patched:
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
