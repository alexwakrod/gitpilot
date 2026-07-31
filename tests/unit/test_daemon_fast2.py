"""Fast daemon tests – no threads, reload_config coverage."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gitpilot.daemon.lifecycle import DaemonLifecycle
from gitpilot.daemon.server import find_free_port, save_port_file, setup_logging
from gitpilot.infrastructure.db import initialize_database
from gitpilot.cli.main import _run_setup_if_needed
from gitpilot.domain.settings import SettingsManager


class TestDaemonServerFast:
    def test_find_free_port(self):
        port = find_free_port()
        assert 1024 < port < 65535

    def test_save_port_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr("gitpilot.daemon.server.get_token_path", lambda: tmp_path / "auth")
        path = save_port_file(44444)
        assert path.read_text() == "44444"

    def test_setup_logging(self, tmp_path):
        setup_logging(tmp_path / "logs")


class TestDaemonLifecycleFast2:
    def test_start_stop_no_projects(self, monkeypatch, tmp_path):
        monkeypatch.setattr("gitpilot.domain.settings.get_gitpilot_dir", lambda: tmp_path)
        monkeypatch.setattr("gitpilot.infrastructure.db.get_gitpilot_dir", lambda: tmp_path)
        initialize_database(tmp_path / "data.db")
        # Prevent actual threading: mock Thread.start and Thread.join
        monkeypatch.setattr("threading.Thread.start", lambda self: None)
        monkeypatch.setattr("threading.Thread.join", lambda self, timeout=None: None)
        lc = DaemonLifecycle({"debounce_interval": 1, "max_commit_retries": 0})
        lc.watcher = MagicMock()
        lc.start()
        lc.stop()

    def test_add_remove_project(self):
        lc = DaemonLifecycle({"debounce_interval": 1})
        lc.watcher = MagicMock()
        lc.add_project(1, "/tmp/p")
        lc.watcher.add_project.assert_called_once()
        lc.remove_project(1, "/tmp/p")
        lc.watcher.remove_project.assert_called_once()

    def test_reload_config(self):
        lc = DaemonLifecycle({"enable_splitting": True, "enable_ai_grouping": False})
        lc.watcher = MagicMock()
        lc.watcher._watchers = {"proj": MagicMock()}
        lc.reload_config({"enable_splitting": False, "enable_ai_grouping": True, "enable_optimizations": True})
        # Committer provider should remain unchanged if not in new config
        assert lc.committer.provider == "grok"  # default
        # Flags should be updated
        assert lc.enable_splitting is False
        assert lc.enable_ai_grouping is True
        assert lc.enable_optimizations is True

    def test_verify_global_git_config(self, monkeypatch):
        lc = DaemonLifecycle({})
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "User\nemail@x.com\n"
            lc._verify_global_git_config()

    def test_validate_registered_projects(self, monkeypatch, tmp_path):
        monkeypatch.setattr("gitpilot.domain.settings.get_gitpilot_dir", lambda: tmp_path)
        monkeypatch.setattr("gitpilot.infrastructure.db.get_gitpilot_dir", lambda: tmp_path)
        initialize_database(tmp_path / "data.db")
        lc = DaemonLifecycle({"debounce_interval": 1})
        lc._validate_registered_projects()


class TestCLIAutoStartFast2:
    def test_run_setup_if_needed_skips(self, monkeypatch, tmp_path):
        monkeypatch.setattr("gitpilot.cli.main.get_token_path", lambda: tmp_path / "auth")
        (tmp_path / "auth").write_text("x")
        mgr = SettingsManager(config_path=tmp_path / "cfg.json")
        mgr.set("ai_provider", "grok")
        with patch("gitpilot.cli.main.console.print") as mp:
            _run_setup_if_needed()
            mp.assert_not_called()

    def test_run_setup_if_needed_triggers(self, monkeypatch, tmp_path):
        monkeypatch.setattr("gitpilot.cli.main.get_token_path", lambda: tmp_path / "noauth")
        with patch("gitpilot.cli.main.SettingsManager.load", return_value={}):
            with patch("gitpilot.cli.main.setup.callback") as mock_setup:
                _run_setup_if_needed()
                mock_setup.assert_called_once()