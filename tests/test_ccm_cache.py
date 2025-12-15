#!/usr/bin/env python3
"""
Test suite for CCM (Content Cache Manager) cache module.

Run with: python3 tests/test_ccm_cache.py
"""

import json
import sys
import tempfile
import shutil
from pathlib import Path

HOOKS_DIR = Path(__file__).parent.parent / 'hooks'
sys.path.insert(0, str(HOOKS_DIR))

from lib.ccm_cache import (
    init_ccm_cache, compute_content_key, store_content, retrieve_content,
    get_metadata, update_pin, build_ccm_stub, parse_ccm_stub, is_ccm_stub,
    list_all_keys, get_cache_stats, delete_cached_content, get_last_key,
    compress_content, decompress_content, get_compression_method
)


class TestContentAddressing:
    """Test SHA256-based content addressing."""

    def test_same_content_same_key(self):
        """Same content should produce same key."""
        content = "Hello, World!"
        key1 = compute_content_key(content)
        key2 = compute_content_key(content)
        assert key1 == key2
        assert key1.startswith('sha256:')

    def test_different_content_different_key(self):
        """Different content should produce different keys."""
        key1 = compute_content_key("Hello")
        key2 = compute_content_key("World")
        assert key1 != key2

    def test_key_format(self):
        """Key should be in sha256:hex format."""
        key = compute_content_key("test")
        assert key.startswith('sha256:')
        hex_part = key[7:]
        assert len(hex_part) == 64
        assert all(c in '0123456789abcdef' for c in hex_part)


class TestCompression:
    """Test compression functionality."""

    def test_compression_available(self):
        """Should detect available compression method."""
        method = get_compression_method()
        assert method in ('zstd', 'gzip', 'none')

    def test_roundtrip_integrity(self):
        """Compressed content should decompress to original."""
        original = b"This is test content " * 100  # Enough to compress
        method = get_compression_method()

        compressed = compress_content(original, method)
        decompressed = decompress_content(compressed, method)

        assert decompressed == original

    def test_small_content_not_compressed(self):
        """Small content should not be compressed."""
        small = b"tiny"
        method = get_compression_method()
        result = compress_content(small, method)
        assert result == small  # Should be unchanged


class TestStoreRetrieve:
    """Test content storage and retrieval."""

    def setup_method(self):
        """Create temp cache directory."""
        self.temp_dir = Path(tempfile.mkdtemp())
        init_ccm_cache(self.temp_dir)

    def teardown_method(self):
        """Clean up temp directory."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_store_and_retrieve(self):
        """Should store and retrieve content."""
        content = "Test content for caching"
        key = store_content(content)

        retrieved = retrieve_content(key)
        assert retrieved == content

    def test_deduplication(self):
        """Same content should not create duplicate blobs."""
        content = "Duplicate me"
        key1 = store_content(content)
        key2 = store_content(content)

        assert key1 == key2

    def test_retrieve_nonexistent(self):
        """Retrieving nonexistent key should return None."""
        result = retrieve_content('sha256:' + '0' * 64)
        assert result is None

    def test_last_key_tracking(self):
        """Should track most recent cache key."""
        content1 = "First content"
        content2 = "Second content"

        key1 = store_content(content1)
        assert get_last_key() == key1

        key2 = store_content(content2)
        assert get_last_key() == key2


class TestMetadata:
    """Test metadata handling."""

    def setup_method(self):
        """Create temp cache directory."""
        self.temp_dir = Path(tempfile.mkdtemp())
        init_ccm_cache(self.temp_dir)

    def teardown_method(self):
        """Clean up temp directory."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_metadata_created(self):
        """Storing content should create metadata."""
        content = "Content with metadata"
        key = store_content(content)

        meta = get_metadata(key)
        assert meta is not None
        assert meta['key'] == key
        assert meta['bytes_uncompressed'] == len(content.encode('utf-8'))
        assert 'created_at' in meta
        assert 'last_access_at' in meta

    def test_source_metadata(self):
        """Should store source metadata."""
        content = "Content with source info"
        source = {'tool_name': 'Bash', 'exit_code': 0, 'command': 'ls -la'}
        key = store_content(content, source=source)

        meta = get_metadata(key)
        assert meta['source']['tool_name'] == 'Bash'
        assert meta['source']['exit_code'] == 0


class TestPinning:
    """Test pin functionality."""

    def setup_method(self):
        """Create temp cache directory."""
        self.temp_dir = Path(tempfile.mkdtemp())
        init_ccm_cache(self.temp_dir)

    def teardown_method(self):
        """Clean up temp directory."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_pin_levels(self):
        """Should support none, soft, and hard pin levels."""
        content = "Pinnable content"

        # Test storing with pin
        key = store_content(content, pin_level='soft', pin_reason='test')
        meta = get_metadata(key)
        assert meta['pinned']['level'] == 'soft'
        assert meta['pinned']['reason'] == 'test'

    def test_pin_update(self):
        """Should update pin level."""
        content = "Content to repin"
        key = store_content(content, pin_level='soft')

        # Update to hard
        success = update_pin(key, 'hard', 'upgraded')
        assert success

        meta = get_metadata(key)
        assert meta['pinned']['level'] == 'hard'
        assert meta['pinned']['reason'] == 'upgraded'

    def test_unpin(self):
        """Should be able to remove pin."""
        content = "Content to unpin"
        key = store_content(content, pin_level='hard')

        update_pin(key, 'none')

        meta = get_metadata(key)
        assert meta['pinned']['level'] == 'none'


class TestStubFormat:
    """Test CCM stub generation and parsing."""

    def test_stub_generation(self):
        """Should generate valid stub format."""
        stub = build_ccm_stub(
            key='sha256:abc123',
            bytes_uncompressed=12345,
            lines=100,
            exit_code=0,
            pin_level='soft'
        )

        assert '[CCM_CACHED]' in stub
        assert '[/CCM_CACHED]' in stub
        assert 'sha256:abc123' in stub
        assert '12345' in stub
        assert 'soft' in stub

    def test_stub_parsing(self):
        """Should parse stub back to dict."""
        stub = build_ccm_stub(
            key='sha256:def456',
            bytes_uncompressed=5000,
            lines=50,
            exit_code=1,
            pin_level='hard'
        )

        parsed = parse_ccm_stub(stub)
        assert parsed is not None
        assert parsed['key'] == 'sha256:def456'
        assert parsed['bytes'] == 5000
        assert parsed['lines'] == 50
        assert parsed['exit_code'] == 1
        assert parsed['pinned'] == 'hard'

    def test_is_ccm_stub(self):
        """Should detect CCM stubs."""
        stub = build_ccm_stub('sha256:test', 100, 10, 0, 'none')
        assert is_ccm_stub(stub) is True
        assert is_ccm_stub("Regular content") is False
        assert is_ccm_stub("") is False


class TestCacheManagement:
    """Test cache listing and stats."""

    def setup_method(self):
        """Create temp cache directory."""
        self.temp_dir = Path(tempfile.mkdtemp())
        init_ccm_cache(self.temp_dir)

    def teardown_method(self):
        """Clean up temp directory."""
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_list_all_keys(self):
        """Should list all cached keys."""
        key1 = store_content("Content 1")
        key2 = store_content("Content 2")

        keys = list_all_keys()
        assert key1 in keys
        assert key2 in keys

    def test_cache_stats(self):
        """Should return accurate cache stats."""
        store_content("Unpinned content")
        store_content("Soft pinned", pin_level='soft')
        store_content("Hard pinned", pin_level='hard')

        stats = get_cache_stats()
        assert stats['total_items'] == 3
        assert stats['unpinned'] == 1
        assert stats['pinned_soft'] == 1
        assert stats['pinned_hard'] == 1

    def test_delete_content(self):
        """Should delete cached content."""
        content = "To be deleted"
        key = store_content(content)

        # Verify it exists
        assert retrieve_content(key) == content

        # Delete
        success = delete_cached_content(key)
        assert success

        # Verify gone
        assert retrieve_content(key) is None
        assert get_metadata(key) is None


def run_tests():
    """Run tests without pytest."""
    import traceback

    test_classes = [
        TestContentAddressing,
        TestCompression,
        TestStoreRetrieve,
        TestMetadata,
        TestPinning,
        TestStubFormat,
        TestCacheManagement,
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

            # Run setup if exists
            if hasattr(instance, 'setup_method'):
                try:
                    instance.setup_method()
                except Exception as e:
                    print(f"  ✗ {name} (setup failed): {e}")
                    failed += 1
                    continue

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
            finally:
                # Run teardown if exists
                if hasattr(instance, 'teardown_method'):
                    try:
                        instance.teardown_method()
                    except Exception:
                        pass

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
