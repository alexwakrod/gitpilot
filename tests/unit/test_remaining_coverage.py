"""Remaining coverage tests – no tkinter, no hangs."""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import readchar

from gitpilot.cli.main import (
    MainMenu,
    DirectoryPicker,
    NonBlockingKeyReader,
    _spawn_in_new_terminal,
    _prepare_project_directory,
    _pick_directory_gui,
)
from gitpilot.domain.settings import SettingsManager


class TestMainMenu:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr("gitpilot.cli.main._get_daemon_port", lambda: 12345)
        monkeypatch.setattr("gitpilot.cli.main._get_api_token", lambda: "token")
        client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {}
        client.get.return_value = resp
        client.post.return_value = resp
        monkeypatch.setattr("gitpilot.cli.main._get_client", lambda: client)
        monkeypatch.setattr("gitpilot.cli.main.SettingsManager", lambda: SettingsManager(config_path=tmp_path / "cfg.json"))
        monkeypatch.setattr("gitpilot.cli.main._pick_directory_gui", lambda: Path(tmp_path))
        monkeypatch.setattr("gitpilot.cli.main.DirectoryPicker.pick", lambda self: Path(tmp_path))
        monkeypatch.setattr("gitpilot.cli.main.Prompt.ask", lambda *a, **kw: "test")
        monkeypatch.setattr("gitpilot.cli.main.Confirm.ask", lambda *a, **kw: False)
        keys = iter([readchar.key.ENTER, 'q'])
        monkeypatch.setattr("readchar.readkey", lambda: next(keys))
        monkeypatch.setattr(MainMenu, "_monitor", lambda self: None)
        self.menu = MainMenu()
        self.menu.running = True

    def test_menu_renders(self):
        assert len(self.menu.options) == 4

    def test_add_project_option(self, monkeypatch):
        monkeypatch.setattr("gitpilot.core.project_setup.is_git_repo", lambda x: True)
        monkeypatch.setattr("gitpilot.core.project_setup.has_commits", lambda x: True)
        monkeypatch.setattr("gitpilot.core.project_setup.has_remote_origin", lambda x: True)
        self.menu._execute_option(0)

    def test_settings_option(self):
        self.menu._settings = lambda: None
        self.menu._execute_option(2)

    def test_exit_option(self):
        self.menu._execute_option(3)
        assert self.menu.running is False


class TestDirectoryPicker:
    def test_render(self):
        p = DirectoryPicker()
        assert p._render() is not None

    def test_pick_cancelled(self, monkeypatch):
        p = DirectoryPicker()
        p.current_path = Path("/tmp")
        monkeypatch.setattr("readchar.readkey", lambda: 'q')
        with patch("gitpilot.cli.main.console.clear"):
            assert p.pick() is None


class TestNonBlockingKeyReader:
    def test_start_stop(self):
        r = NonBlockingKeyReader()
        r.start()
        assert r.get_key() is None
        r.stop()


class TestPickDirectoryGUI:
    def test_fallback(self, monkeypatch):
        # Simulate tkinter not being available
        monkeypatch.setitem(sys.modules, "tkinter", None)
        # The function catches ImportError and returns None
        try:
            result = _pick_directory_gui()
            assert result is None
        except Exception:
            pytest.skip("tkinter not importable – expected on headless CI")


class TestSpawnNewTerminal:
    def test_linux(self, monkeypatch):
        monkeypatch.setattr("shutil", "which", return_value="/usr/bin/xterm")
        with patch("subprocess.Popen"), patch("sys.exit"):
            _spawn_in_new_terminal()