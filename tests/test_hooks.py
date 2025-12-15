#!/usr/bin/env python3
"""
Test suite for Claude Context Manager hooks.

Run with: python3 -m pytest tests/ -v
Or:       python3 tests/test_hooks.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Add hooks to path for imports
HOOKS_DIR = Path(__file__).parent.parent / 'hooks'
sys.path.insert(0, str(HOOKS_DIR))


def run_hook(hook_name: str, input_data: dict) -> dict:
    """Run a hook script with JSON input, return parsed output."""
    hook_path = HOOKS_DIR / hook_name
    result = subprocess.run(
        ['python3', str(hook_path)],
        input=json.dumps(input_data),
        capture_output=True,
        text=True,
        timeout=30
    )

    output = result.stdout.strip()
    if not output:
        return {}

    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {'raw_output': output, 'stderr': result.stderr}


def make_input(tool_name: str, tool_input: dict, cwd: str = '/tmp',
               transcript_path: str = '/tmp/test-session.jsonl',
               tool_use_id: str = 'test-tool-123') -> dict:
    """Create standard hook input structure."""
    return {
        'tool_name': tool_name,
        'tool_input': tool_input,
        'transcript_path': transcript_path,
        'tool_use_id': tool_use_id,
        'session': {'cwd': cwd}
    }


class TestSubagentDetection:
    """Test main agent vs subagent detection."""

    def setup_method(self):
        """Create temp directory with mock transcripts."""
        self.temp_dir = tempfile.mkdtemp()
        self.main_transcript = Path(self.temp_dir) / 'main-session.jsonl'
        self.agent_transcript = Path(self.temp_dir) / 'agent-abc123.jsonl'

        # Write mock transcripts
        self.main_transcript.write_text('{"id":"main-tool-001"}\n')
        self.agent_transcript.write_text('{"id":"subagent-tool-002"}\n')

    def teardown_method(self):
        """Clean up temp files."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_main_agent_intercepted(self):
        """Main agent calls should be intercepted."""
        input_data = make_input(
            'Glob',
            {'pattern': '*.py', 'path': str(HOOKS_DIR)},
            transcript_path=str(self.main_transcript),
            tool_use_id='main-tool-001'
        )
        result = run_hook('intercept-glob.py', input_data)

        assert result.get('decision') == 'block', "Main agent should be intercepted"
        assert 'reason' in result, "Should have results in reason"

    def test_subagent_passes_through(self):
        """Subagent calls should pass through."""
        input_data = make_input(
            'Glob',
            {'pattern': '*.py', 'path': str(HOOKS_DIR)},
            transcript_path=str(self.main_transcript),
            tool_use_id='subagent-tool-002'
        )
        result = run_hook('intercept-glob.py', input_data)

        assert result == {}, "Subagent should pass through (empty response)"


class TestBashHook:
    """Test intercept-bash.py"""

    def test_small_command_passes(self):
        """Trivial commands should pass through."""
        for cmd in ['pwd', 'whoami', 'echo hi', 'git status']:
            input_data = make_input('Bash', {'command': cmd})
            result = run_hook('intercept-bash.py', input_data)
            assert result == {}, f"'{cmd}' should pass through"

    def test_command_executes(self):
        """Non-trivial commands should execute and return results."""
        input_data = make_input('Bash', {'command': 'ls -la /tmp | head -5'})
        result = run_hook('intercept-bash.py', input_data)

        assert result.get('decision') == 'block'
        assert 'reason' in result
        # Should contain either results or "Exit" status
        assert 'Exit' in result['reason'] or 'tmp' in result['reason'].lower()

    def test_cwd_respected(self):
        """Commands should run in specified cwd."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a unique file
            marker = Path(tmpdir) / 'test-marker-file.txt'
            marker.write_text('test')

            input_data = make_input(
                'Bash',
                {'command': 'ls *.txt'},
                cwd=tmpdir
            )
            result = run_hook('intercept-bash.py', input_data)

            assert 'test-marker-file.txt' in result.get('reason', '')


class TestGlobHook:
    """Test intercept-glob.py"""

    def test_glob_executes(self):
        """Glob should find files."""
        input_data = make_input('Glob', {'pattern': '*.py', 'path': str(HOOKS_DIR)})
        result = run_hook('intercept-glob.py', input_data)

        assert result.get('decision') == 'block'
        assert 'intercept-bash.py' in result.get('reason', '')

    def test_glob_no_matches(self):
        """Glob with no matches should return appropriately."""
        input_data = make_input('Glob', {'pattern': '*.nonexistent', 'path': '/tmp'})
        result = run_hook('intercept-glob.py', input_data)

        assert result.get('decision') == 'block'
        # Either empty or "No matches"
        reason = result.get('reason', '')
        assert reason == '' or 'No matches' in reason or reason.strip() == ''

    def test_relative_path_resolution(self):
        """Relative paths should resolve against cwd."""
        input_data = make_input(
            'Glob',
            {'pattern': '*.py', 'path': '.'},
            cwd=str(HOOKS_DIR)
        )
        result = run_hook('intercept-glob.py', input_data)

        assert 'intercept-bash.py' in result.get('reason', '')


class TestGrepHook:
    """Test intercept-grep.py"""

    def test_grep_finds_pattern(self):
        """Grep should find matching patterns."""
        input_data = make_input(
            'Grep',
            {'pattern': 'def main', 'path': str(HOOKS_DIR), 'output_mode': 'files_with_matches'}
        )
        result = run_hook('intercept-grep.py', input_data)

        assert result.get('decision') == 'block'
        reason = result.get('reason', '')
        # Should find files with def main
        assert '.py' in reason or 'ripgrep not found' in reason

    def test_grep_content_mode(self):
        """Grep content mode should show matching lines."""
        input_data = make_input(
            'Grep',
            {'pattern': 'BASH_THRESHOLD', 'path': str(HOOKS_DIR / 'config.py'),
             'output_mode': 'content', '-n': True}
        )
        result = run_hook('intercept-grep.py', input_data)

        assert result.get('decision') == 'block'
        reason = result.get('reason', '')
        assert 'BASH_THRESHOLD' in reason or 'ripgrep not found' in reason


class TestReadHook:
    """Test intercept-read.py"""

    def test_small_file_passes(self):
        """Small files should pass through."""
        input_data = make_input(
            'Read',
            {'file_path': str(HOOKS_DIR / 'config.py')}
        )
        result = run_hook('intercept-read.py', input_data)

        assert result == {}, "Small files should pass through"

    def test_whitelisted_extensions_pass(self):
        """Whitelisted file types should always pass through."""
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            # Write a large JSON file
            f.write(b'{"data": "' + b'x' * 50000 + b'"}')
            f.flush()

            input_data = make_input('Read', {'file_path': f.name})
            result = run_hook('intercept-read.py', input_data)

            os.unlink(f.name)
            assert result == {}, "JSON files should pass through regardless of size"

    def test_paginated_read_passes(self):
        """Reads with offset/limit should pass through."""
        input_data = make_input(
            'Read',
            {'file_path': str(HOOKS_DIR / 'lib' / 'common.py'), 'offset': 0, 'limit': 100}
        )
        result = run_hook('intercept-read.py', input_data)

        assert result == {}, "Paginated reads should pass through"


class TestPassthroughMode:
    """Test CLAUDE_HOOKS_PASSTHROUGH environment variable."""

    def test_passthrough_bypasses_all(self):
        """Setting CLAUDE_HOOKS_PASSTHROUGH=1 should bypass hooks."""
        env = os.environ.copy()
        env['CLAUDE_HOOKS_PASSTHROUGH'] = '1'

        hook_path = HOOKS_DIR / 'intercept-bash.py'
        input_data = make_input('Bash', {'command': 'find / -type f'})

        result = subprocess.run(
            ['python3', str(hook_path)],
            input=json.dumps(input_data),
            capture_output=True,
            text=True,
            env=env,
            timeout=10
        )

        output = result.stdout.strip()
        assert output == '{}', "Passthrough mode should return empty JSON"


class TestCommonLibrary:
    """Test lib/common.py functions."""

    def test_config_loads(self):
        """Config should load without errors."""
        from lib import common

        assert hasattr(common, 'BASH_THRESHOLD')
        assert hasattr(common, 'CACHE_DIR')
        assert common.BASH_THRESHOLD > 0

    def test_json_block_format(self):
        """json_block should produce valid JSON."""
        from lib import common
        import io
        from contextlib import redirect_stdout

        f = io.StringIO()
        with redirect_stdout(f):
            common.json_block("test reason")

        output = f.getvalue().strip()
        parsed = json.loads(output)

        assert parsed['decision'] == 'block'
        assert parsed['reason'] == 'test reason'

    def test_cache_output(self):
        """cache_output should create file and return uuid."""
        from lib import common

        common.init_cache()
        content = "test content for caching"
        file_uuid = common.cache_output(content)

        assert len(file_uuid) == 8
        cache_file = common.CACHE_DIR / file_uuid
        assert cache_file.exists()
        assert cache_file.read_text() == content

        # Cleanup
        cache_file.unlink()


def run_tests():
    """Run tests without pytest."""
    import traceback

    test_classes = [
        TestSubagentDetection,
        TestBashHook,
        TestGlobHook,
        TestGrepHook,
        TestReadHook,
        TestPassthroughMode,
        TestCommonLibrary,
    ]

    passed = 0
    failed = 0
    errors = []

    for test_class in test_classes:
        print(f"\n{test_class.__name__}")
        print("=" * len(test_class.__name__))

        instance = test_class()

        for name in dir(instance):
            if not name.startswith('test_'):
                continue

            method = getattr(instance, name)

            # Setup
            if hasattr(instance, 'setup_method'):
                try:
                    instance.setup_method()
                except Exception as e:
                    print(f"  ✗ {name} (setup failed: {e})")
                    failed += 1
                    continue

            # Run test
            try:
                method()
                print(f"  ✓ {name}")
                passed += 1
            except AssertionError as e:
                print(f"  ✗ {name}: {e}")
                failed += 1
                errors.append((name, str(e)))
            except Exception as e:
                print(f"  ✗ {name}: {type(e).__name__}: {e}")
                failed += 1
                errors.append((name, traceback.format_exc()))

            # Teardown
            if hasattr(instance, 'teardown_method'):
                try:
                    instance.teardown_method()
                except:
                    pass

    print(f"\n{'=' * 40}")
    print(f"Results: {passed} passed, {failed} failed")

    if errors:
        print("\nFailures:")
        for name, error in errors:
            print(f"\n{name}:\n{error}")

    return failed == 0


if __name__ == '__main__':
    # Check for pytest
    try:
        import pytest
        sys.exit(pytest.main([__file__, '-v'] + sys.argv[1:]))
    except ImportError:
        # Fall back to basic runner
        success = run_tests()
        sys.exit(0 if success else 1)
