"""Final coverage push: notifications, database, server, models, policies, settings, http_client."""

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gitpilot.core.notifications import send_discord_notification
from gitpilot.infrastructure.db import initialize_database, managed_connection, get_db_path, get_connection, run_migrations
from gitpilot.domain.models import ProjectCreate, ProjectUpdate, SettingCreate, SettingUpdate
from gitpilot.domain.policies import generate_api_token, ensure_token_file, get_token_path, verify_owner, get_current_os_user
from gitpilot.domain.settings import SettingsManager, get_gitpilot_dir, DEFAULT_CONFIG
from gitpilot.infrastructure.http_client import get_http_client, close_http_client, get_sync_client


# ===========================================================================
# Notifications – full coverage
# ===========================================================================
class TestNotificationsFull:
    @pytest.mark.asyncio
    async def test_send_discord_notification_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            result = await send_discord_notification(
                "https://discord.com/api/webhooks/123/abc",
                "proj", "hash", "msg", "main", "2025-01-01T00:00:00",
            )
            assert result is True

    @pytest.mark.asyncio
    async def test_send_discord_notification_http_error(self):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = httpx.ConnectError("timeout")
            result = await send_discord_notification(
                "https://discord.com/api/webhooks/123/abc",
                "proj", "hash", "msg", "main", "2025-01-01T00:00:00",
            )
            assert result is False


# ===========================================================================
# Database – initialize_database with real file path
# ===========================================================================
class TestDatabaseInitialize:
    def test_initialize_database_with_path(self, tmp_path):
        db_path = tmp_path / "test.db"
        initialize_database(db_path)
        assert db_path.exists()

    def test_managed_connection_rollback(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        monkeypatch.setattr("gitpilot.infrastructure.db.get_gitpilot_dir", lambda: tmp_path)
        initialize_database(db_path)
        # Force an exception inside managed connection to test rollback
        with pytest.raises(RuntimeError):
            with managed_connection(db_path) as conn:
                conn.execute("INSERT INTO projects (name, path, owner) VALUES (?,?,?)",
                             ["test", "/path", "owner"])
                raise RuntimeError("forced")
        # Verify no data was committed
        with managed_connection(db_path) as conn:
            rows = conn.execute("SELECT * FROM projects").fetchall()
            assert len(rows) == 0

    def test_run_migrations_error(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        monkeypatch.setattr("gitpilot.infrastructure.db.get_gitpilot_dir", lambda: tmp_path)
        with patch("gitpilot.infrastructure.db.SCHEMA_SQL", "INVALID SQL;"):
            with pytest.raises(Exception):
                initialize_database(db_path)


# ===========================================================================
# Domain models – validators edge cases
# ===========================================================================
class TestDomainModels:
    def test_project_create_relative_path_fails(self):
        with pytest.raises(ValueError, match="Path must be absolute"):
            ProjectCreate(name="test", path="relative/path")

    def test_project_update_relative_path_fails(self):
        with pytest.raises(ValueError, match="Path must be absolute"):
            ProjectUpdate(path="relative/path")

    def test_project_update_no_path_ok(self):
        pu = ProjectUpdate(name="new name")
        assert pu.name == "new name"
        assert pu.path is None

    def test_setting_create_invalid_type(self):
        with pytest.raises(ValueError):
            SettingCreate(key="k", value="v", type="invalid")


# ===========================================================================
# Policies – token generation and verification
# ===========================================================================
class TestPolicies:
    def test_generate_api_token_length(self):
        token = generate_api_token()
        assert len(token) >= 32

    def test_ensure_token_file_creates_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr("gitpilot.domain.policies.get_gitpilot_dir", lambda: tmp_path)
        token = ensure_token_file()
        assert (tmp_path / "auth_token").exists()
        assert (tmp_path / "auth_token").read_text() == token

    def test_ensure_token_file_permissions(self, monkeypatch, tmp_path):
        if sys.platform == "win32":
            pytest.skip("Unix permissions not applicable")
        monkeypatch.setattr("gitpilot.domain.policies.get_gitpilot_dir", lambda: tmp_path)
        ensure_token_file()
        file_stat = os.stat(tmp_path / "auth_token")
        assert stat.S_IMODE(file_stat.st_mode) == 0o600

    def test_verify_owner_valid_token(self, monkeypatch, tmp_path):
        monkeypatch.setattr("gitpilot.domain.policies.get_gitpilot_dir", lambda: tmp_path)
        token = ensure_token_file()
        assert verify_owner("user", token) is True

    def test_verify_owner_invalid_token(self, monkeypatch, tmp_path):
        monkeypatch.setattr("gitpilot.domain.policies.get_gitpilot_dir", lambda: tmp_path)
        ensure_token_file()
        assert verify_owner("user", "wrong-token") is False

    def test_verify_owner_no_token_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr("gitpilot.domain.policies.get_gitpilot_dir", lambda: tmp_path)
        assert verify_owner("user", "any") is False

    def test_get_current_os_user(self, monkeypatch):
        monkeypatch.setitem(os.environ, "USER", "testuser")
        assert get_current_os_user() == "testuser"


# ===========================================================================
# Settings – defaults, persistence, edge cases
# ===========================================================================
class TestSettingsEdge:
    def test_default_config_values(self):
        assert DEFAULT_CONFIG["debounce_interval"] == 120

    def test_load_with_defaults_merged(self, tmp_path):
        # File with only one key; missing keys should be filled from defaults
        config_path = tmp_path / "config.json"
        config_path.write_text('{"theme": "custom"}')
        mgr = SettingsManager(config_path=config_path)
        config = mgr.load()
        assert config["theme"] == "custom"
        assert config["ai_provider"] == "grok"  # from default

    def test_save_creates_directory(self, tmp_path):
        mgr = SettingsManager(config_path=tmp_path / "subdir" / "config.json")
        mgr.set("key", "value")
        assert (tmp_path / "subdir" / "config.json").exists()

    def test_load_broken_json_falls_back(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text("{broken")
        mgr = SettingsManager(config_path=config_path)
        config = mgr.load()
        assert config["ai_provider"] == "grok"  # default applied

    def test_delete_key_persists(self, tmp_path):
        mgr = SettingsManager(config_path=tmp_path / "config.json")
        mgr.set("key1", "val")
        mgr.delete("key1")
        assert mgr.get("key1") is None
        # Reload – key should still be gone
        mgr2 = SettingsManager(config_path=tmp_path / "config.json")
        assert mgr2.get("key1") is None


# ===========================================================================
# HTTP client – connection management
# ===========================================================================
class TestHTTPClient:
    @pytest.mark.asyncio
    async def test_get_http_client_returns_singleton(self):
        client1 = await get_http_client()
        client2 = await get_http_client()
        assert client1 is client2

    @pytest.mark.asyncio
    async def test_close_http_client(self):
        client = await get_http_client()
        await close_http_client()
        # After closing, a new client should be created
        new_client = await get_http_client()
        assert new_client is not client

    def test_get_sync_client(self):
        client = get_sync_client()
        assert isinstance(client, httpx.Client)
        client.close()