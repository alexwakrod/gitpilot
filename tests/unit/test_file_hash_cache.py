"""Unit tests for FileHashCache."""

import time

import pytest

from gitpilot.core.watcher import FileHashCache


class TestFileHashCache:
    def test_set_and_get_hash(self):
        cache = FileHashCache(ttl=60)
        cache.set_hash("/path/to/file.py", "abc123")
        assert cache.get_hash("/path/to/file.py") == "abc123"

    def test_get_nonexistent_key_returns_none(self):
        cache = FileHashCache()
        assert cache.get_hash("/nonexistent") is None

    def test_invalidate_removes_entry(self):
        cache = FileHashCache()
        cache.set_hash("/file.txt", "hashval")
        cache.invalidate("/file.txt")
        assert cache.get_hash("/file.txt") is None

    def test_ttl_expiration(self, monkeypatch):
        now = time.time()
        monkeypatch.setattr(time, "time", lambda: now)
        cache = FileHashCache(ttl=10)
        cache.set_hash("/stale.py", "stalehash")
        monkeypatch.setattr(time, "time", lambda: now + 11)
        assert cache.get_hash("/stale.py") is None

    def test_ttl_fresh_entry_still_valid(self, monkeypatch):
        now = time.time()
        monkeypatch.setattr(time, "time", lambda: now)
        cache = FileHashCache(ttl=10)
        cache.set_hash("/fresh.py", "freshhash")
        monkeypatch.setattr(time, "time", lambda: now + 9)
        assert cache.get_hash("/fresh.py") == "freshhash"

    def test_overwrite_updates_hash_and_timestamp(self, monkeypatch):
        now = time.time()
        monkeypatch.setattr(time, "time", lambda: now)
        cache = FileHashCache(ttl=10)
        cache.set_hash("/file.py", "first")
        monkeypatch.setattr(time, "time", lambda: now + 5)
        cache.set_hash("/file.py", "second")
        assert cache.get_hash("/file.py") == "second"