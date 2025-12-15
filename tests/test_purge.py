#!/usr/bin/env python3
"""
Test suite for claude-session-purge.py

Run with: python3 tests/test_purge.py
"""

import json
import sys
import tempfile
from pathlib import Path

HOOKS_DIR = Path(__file__).parent.parent / 'hooks'
sys.path.insert(0, str(HOOKS_DIR))

# Import after path setup
import importlib.util
spec = importlib.util.spec_from_file_location("purge", HOOKS_DIR / "claude-session-purge.py")
purge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(purge)


def create_test_session(lines: list[dict]) -> Path:
    """Create a test session file with given message objects."""
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False)
    for line in lines:
        f.write(json.dumps(line) + '\n')
    f.close()
    return Path(f.name)


class TestCompactionDetection:
    """Test compaction summary detection."""

    def test_detects_top_level_compaction(self):
        """Should detect isCompactSummary at top level."""
        obj = {'isCompactSummary': True, 'message': {'content': []}}
        assert purge.is_compaction_summary(obj) is True

    def test_detects_nested_compaction(self):
        """Should detect isCompactSummary in content[0]."""
        obj = {'message': {'content': [{'isCompactSummary': True, 'text': 'summary'}]}}
        assert purge.is_compaction_summary(obj) is True

    def test_ignores_false_positives(self):
        """Should not match isCompactSummary in nested tool output."""
        obj = {
            'message': {
                'content': [{
                    'type': 'tool_result',
                    'content': 'grep found: "isCompactSummary":true in file.py'
                }]
            }
        }
        assert purge.is_compaction_summary(obj) is False

    def test_normal_message_not_compaction(self):
        """Normal messages should not be detected as compaction."""
        obj = {'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]}}
        assert purge.is_compaction_summary(obj) is False


class TestToolPairing:
    """Test tool_use/tool_result ID extraction."""

    def test_extracts_tool_use_ids(self):
        """Should extract tool_use IDs."""
        obj = {
            'message': {
                'content': [
                    {'type': 'tool_use', 'id': 'toolu_abc'},
                    {'type': 'tool_use', 'id': 'toolu_def'},
                ]
            }
        }
        ids = purge.get_tool_use_ids(obj)
        assert ids == {'toolu_abc', 'toolu_def'}

    def test_extracts_tool_result_ids(self):
        """Should extract tool_result IDs."""
        obj = {
            'message': {
                'content': [
                    {'type': 'tool_result', 'tool_use_id': 'toolu_abc'},
                    {'type': 'tool_result', 'tool_use_id': 'toolu_def'},
                ]
            }
        }
        ids = purge.get_tool_result_ids(obj)
        assert ids == {'toolu_abc', 'toolu_def'}


class TestSessionAnalysis:
    """Test session file analysis."""

    def test_counts_messages(self):
        """Should count message types correctly."""
        lines = [
            {'type': 'system', 'uuid': '1', 'message': {'role': 'system', 'content': []}},
            {'type': 'user', 'uuid': '2', 'parentUuid': '1', 'message': {'role': 'user', 'content': []}},
            {'type': 'assistant', 'uuid': '3', 'parentUuid': '2', 'message': {'role': 'assistant', 'content': []}},
        ]
        session_file = create_test_session(lines)

        try:
            stats = purge.analyze_session(session_file)
            assert stats['messages']['system'] == 1
            assert stats['messages']['user'] == 1
            assert stats['messages']['assistant'] == 1
        finally:
            session_file.unlink()

    def test_detects_broken_parent_links(self):
        """Should detect broken parentUuid references."""
        lines = [
            {'type': 'user', 'uuid': '1', 'message': {'content': []}},
            {'type': 'assistant', 'uuid': '2', 'parentUuid': 'nonexistent', 'message': {'content': []}},
        ]
        session_file = create_test_session(lines)

        try:
            stats = purge.analyze_session(session_file)
            assert stats['broken_parent_links'] == 1
        finally:
            session_file.unlink()

    def test_counts_thinking_blocks(self):
        """Should count thinking blocks."""
        lines = [
            {
                'type': 'assistant',
                'uuid': '1',
                'message': {
                    'content': [
                        {'type': 'thinking', 'thinking': 'Let me think about this...'},
                        {'type': 'text', 'text': 'Here is my answer'},
                    ]
                }
            },
        ]
        session_file = create_test_session(lines)

        try:
            stats = purge.analyze_session(session_file)
            assert stats['thinking_blocks'] == 1
        finally:
            session_file.unlink()


class TestPurgeOperations:
    """Test actual purge operations."""

    def test_removes_thinking_blocks(self):
        """Should remove thinking blocks when compaction exists."""
        lines = [
            {'type': 'user', 'uuid': '1', 'isCompactSummary': True, 'message': {'content': []}},
            {
                'type': 'assistant',
                'uuid': '2',
                'parentUuid': '1',
                'message': {
                    'content': [
                        {'type': 'thinking', 'thinking': 'x' * 1000},
                        {'type': 'text', 'text': 'answer'},
                    ]
                }
            },
        ]
        session_file = create_test_session(lines)

        try:
            results = purge.purge_session(session_file, dry_run=False, verbose=False)
            assert results['thinking_removed'] >= 1

            # Verify file was modified
            with open(session_file) as f:
                content = f.read()
                # Thinking block should be gone
                assert 'x' * 1000 not in content
                # Text should remain
                assert 'answer' in content
        finally:
            # Clean up backup
            for backup in session_file.parent.glob('*.backup.*'):
                backup.unlink()
            session_file.unlink()

    def test_preserves_compaction_summaries(self):
        """Should never modify compaction summaries."""
        compaction_text = "This is a compaction summary with important context"
        lines = [
            {
                'type': 'user',
                'uuid': '1',
                'isCompactSummary': True,
                'message': {'content': [{'type': 'text', 'text': compaction_text}]}
            },
        ]
        session_file = create_test_session(lines)

        try:
            purge.purge_session(session_file, dry_run=False, verbose=False)

            with open(session_file) as f:
                content = f.read()
                assert compaction_text in content
        finally:
            for backup in session_file.parent.glob('*.backup.*'):
                backup.unlink()
            session_file.unlink()

    def test_repairs_broken_parent_chain(self):
        """Should repair broken parentUuid links."""
        lines = [
            {'type': 'user', 'uuid': '1', 'message': {'content': []}},
            {'type': 'assistant', 'uuid': '2', 'parentUuid': 'broken', 'message': {'content': []}},
        ]
        session_file = create_test_session(lines)

        try:
            results = purge.purge_session(session_file, repair_only=True, verbose=False)
            assert results['parent_links_repaired'] == 1

            # Verify repair
            with open(session_file) as f:
                for line in f:
                    obj = json.loads(line)
                    if obj.get('uuid') == '2':
                        assert obj['parentUuid'] == '1'
        finally:
            for backup in session_file.parent.glob('*.backup.*'):
                backup.unlink()
            session_file.unlink()

    def test_dry_run_makes_no_changes(self):
        """Dry run should not modify the file."""
        original_content = json.dumps({'type': 'user', 'uuid': '1', 'message': {'content': []}})
        session_file = create_test_session([json.loads(original_content)])

        try:
            original_mtime = session_file.stat().st_mtime
            purge.purge_session(session_file, dry_run=True, verbose=False)

            # File should be unchanged
            assert session_file.stat().st_mtime == original_mtime
        finally:
            session_file.unlink()


class TestPinDirectives:
    """Test pin directive parsing and resolution."""

    def test_parse_pin_last(self):
        """Should parse ccm:pin last directive."""
        lines = [
            ({'message': {'content': [{'type': 'text', 'text': 'ccm:pin last level=soft reason="important"'}]}}, None),
        ]
        directives = purge.parse_pin_directives(lines)
        assert len(directives) == 1
        assert directives[0].directive_type == 'last'
        assert directives[0].level == 'soft'
        assert directives[0].reason == 'important'

    def test_parse_pin_next(self):
        """Should parse ccm:pin next directive."""
        lines = [
            ({'message': {'content': 'ccm:pin next level=hard'}}, None),
        ]
        directives = purge.parse_pin_directives(lines)
        assert len(directives) == 1
        assert directives[0].directive_type == 'next'
        assert directives[0].level == 'hard'

    def test_parse_pin_range(self):
        """Should parse ccm:pin start/end range."""
        lines = [
            ({'message': {'content': 'ccm:pin start'}}, None),
            ({'message': {'content': 'some other message'}}, None),
            ({'message': {'content': 'ccm:pin end'}}, None),
        ]
        directives = purge.parse_pin_directives(lines)
        assert len(directives) == 2
        assert directives[0].directive_type == 'start'
        assert directives[1].directive_type == 'end'

    def test_resolve_pin_last(self):
        """Should resolve pin last to preceding tool_result."""
        lines = [
            ({'message': {'content': [{'type': 'tool_result', 'content': 'x' * 6000}]}}, None),
            ({'message': {'content': 'ccm:pin last'}}, None),
        ]
        directives = purge.parse_pin_directives(lines)
        targets = purge.resolve_pin_targets(lines, directives, threshold=5000)
        assert 0 in targets

    def test_resolve_pin_next(self):
        """Should resolve pin next to following tool_result."""
        lines = [
            ({'message': {'content': 'ccm:pin next'}}, None),
            ({'message': {'content': [{'type': 'tool_result', 'content': 'x' * 6000}]}}, None),
        ]
        directives = purge.parse_pin_directives(lines)
        targets = purge.resolve_pin_targets(lines, directives, threshold=5000)
        assert 1 in targets


class TestCCMStubGeneration:
    """Test CCM stub generation during purge."""

    def test_large_tool_result_becomes_stub(self):
        """Large old tool_results should become CCM stubs."""
        # Create session with large tool_result far from end
        lines = [
            {'type': 'user', 'uuid': '1', 'isCompactSummary': True, 'message': {'content': []}},
        ]
        # Add many messages to push tool_result far from end
        for i in range(30):
            lines.append({
                'type': 'assistant' if i % 2 == 0 else 'user',
                'uuid': str(i + 2),
                'parentUuid': str(i + 1),
                'message': {'content': [{'type': 'text', 'text': f'message {i}'}]}
            })

        # Insert large tool_result early
        lines.insert(2, {
            'type': 'user',
            'uuid': '2a',
            'parentUuid': '1',
            'message': {
                'content': [{
                    'type': 'tool_result',
                    'tool_use_id': 'toolu_test',
                    'content': 'Large output ' * 1000  # ~13000 bytes
                }]
            }
        })

        session_file = create_test_session(lines)

        try:
            results = purge.purge_session(
                session_file,
                threshold=5000,
                dry_run=False,
                verbose=False,
                use_ccm=True
            )

            # Should have stubbed at least one result
            # Note: depends on CCM being available
            if purge.CCM_AVAILABLE:
                assert results.get('tool_results_stubbed', 0) >= 1 or results.get('tool_results_truncated', 0) >= 1
        finally:
            for backup in session_file.parent.glob('*.backup.*'):
                backup.unlink()
            session_file.unlink()

    def test_recent_tool_result_truncated_not_stubbed(self):
        """Recent tool_results should be truncated, not stubbed."""
        lines = [
            {'type': 'user', 'uuid': '1', 'isCompactSummary': True, 'message': {'content': []}},
            {
                'type': 'user',
                'uuid': '2',
                'parentUuid': '1',
                'message': {
                    'content': [{
                        'type': 'tool_result',
                        'tool_use_id': 'toolu_recent',
                        'content': 'Recent large output ' * 500
                    }]
                }
            },
        ]
        session_file = create_test_session(lines)

        try:
            results = purge.purge_session(
                session_file,
                threshold=5000,
                recent_lines=50,  # Everything is "recent"
                dry_run=False,
                verbose=False
            )

            # Should truncate, not stub (it's within recent_lines)
            assert results.get('tool_results_truncated', 0) >= 1
        finally:
            for backup in session_file.parent.glob('*.backup.*'):
                backup.unlink()
            session_file.unlink()


def run_tests():
    """Run tests without pytest."""
    import traceback

    test_classes = [
        TestCompactionDetection,
        TestToolPairing,
        TestSessionAnalysis,
        TestPurgeOperations,
        TestPinDirectives,
        TestCCMStubGeneration,
    ]

    passed = 0
    failed = 0

    for test_class in test_classes:
        print(f"\n{test_class.__name__}")
        print("=" * len(test_class.__name__))

        instance = test_class()

        for name in dir(instance):
            if not name.startswith('test_'):
                continue

            method = getattr(instance, name)

            try:
                method()
                print(f"  ✓ {name}")
                passed += 1
            except AssertionError as e:
                print(f"  ✗ {name}: {e}")
                failed += 1
            except Exception as e:
                print(f"  ✗ {name}: {type(e).__name__}: {e}")
                traceback.print_exc()
                failed += 1

    print(f"\n{'=' * 40}")
    print(f"Results: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == '__main__':
    try:
        import pytest
        sys.exit(pytest.main([__file__, '-v'] + sys.argv[1:]))
    except ImportError:
        success = run_tests()
        sys.exit(0 if success else 1)
