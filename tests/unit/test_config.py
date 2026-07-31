"""Unit tests for configuration management."""

import json
from pathlib import Path

import pytest

from gitpilot.domain.settings import SettingsManager


class TestSettingsManager:
    @pytest.fixture
    def temp_config_dir(self, tmp_path):
        config_path = tmp_path / "config.json"
        default_config = {
            "ai_provider": "grok",
            "ai_model": "grok-2",
            "ai_temperature": 0.5,
            "debounce_interval": 120,
            "smart_grouping": True,
            "branch_aware_messages": True,
            "max_commit_retries": 3,
            "discord_webhook_enabled": False,
            "theme": "dark",
        }
        config_path.write_text(json.dumps(default_config))
        return config_path

    def test_load_existing_config(self, temp_config_dir):
        manager = SettingsManager(config_path=temp_config_dir)
        config = manager.load()
        assert config["ai_provider"] == "grok"
        assert config["debounce_interval"] == 120

    def test_get_single_value(self, temp_config_dir):
        manager = SettingsManager(config_path=temp_config_dir)
        assert manager.get("theme") == "dark"

    def test_set_value_persists(self, temp_config_dir):
        manager = SettingsManager(config_path=temp_config_dir)
        manager.set("theme", "light")
        assert manager.get("theme") == "light"
        # Reload from file to check persistence
        manager2 = SettingsManager(config_path=temp_config_dir)
        assert manager2.get("theme") == "light"

    def test_get_nonexistent_key_returns_none(self, temp_config_dir):
        manager = SettingsManager(config_path=temp_config_dir)
        assert manager.get("nonexistent") is None

    def test_defaults_applied_when_file_missing(self, tmp_path):
        manager = SettingsManager(config_path=tmp_path / "nonexistent.json")
        config = manager.load()
        assert config["ai_provider"] == "grok"
        assert config["theme"] == "dark"
        assert config["debounce_interval"] == 120

    def test_delete_key_reverts_to_default_on_reload(self, temp_config_dir):
        manager = SettingsManager(config_path=temp_config_dir)
        manager.delete("theme")
        # After deletion, value should be removed from current in‑memory data
        assert manager.get("theme") is None
        # Reload manager: defaults should re‑apply
        manager2 = SettingsManager(config_path=temp_config_dir)
        assert manager2.get("theme") == "dark"