"""Configuration management for GitPilot."""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
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


def get_gitpilot_dir() -> Path:
    """Return the user's GitPilot configuration directory (cross-platform)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "gitpilot"


class SettingsManager:
    """Manages loading, updating, and persisting JSON configuration."""

    def __init__(self, config_path: Path | None = None):
        if config_path is None:
            config_path = get_gitpilot_dir() / "config.json"
        self.config_path = config_path
        self._data: dict[str, Any] = {}
        self.load()  # populate _data immediately

    def load(self) -> dict[str, Any]:
        """Load configuration from disk, applying defaults for missing keys."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if self.config_path.exists():
            try:
                raw = self.config_path.read_text(encoding="utf-8")
                self._data = json.loads(raw)
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Failed to load config, using defaults: %s", exc)
                self._data = {}
        else:
            self._data = {}
            self.save()

        # Merge with defaults for any missing keys
        merged = {**DEFAULT_CONFIG, **self._data}
        if merged != self._data:
            self._data = merged
            self.save()
        return self._data

    def get(self, key: str) -> Any | None:
        """Retrieve a specific configuration value."""
        if not self._data:
            self.load()
        return self._data.get(key)

    def set(self, key: str, value: Any) -> None:
        """Set a configuration value and persist."""
        self._data[key] = value
        self.save()

    def delete(self, key: str) -> None:
        """Remove a key, reverting to default on next load."""
        self._data.pop(key, None)
        self.save()

    def save(self) -> None:
        """Persist current configuration to disk."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.config_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp_path.replace(self.config_path)
        if sys.platform != "win32":
            self.config_path.chmod(0o600)