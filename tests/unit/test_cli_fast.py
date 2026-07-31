"""Fast CLI tests – all mocked, no hangs, no stdin capture."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gitpilot.cli.main import cli, _validate_api_key_format, _get_daemon_port, _get_client, _spawn_in_new_terminal
from gitpilot.domain.settings import SettingsManager


class TestCLIFast:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr("gitpilot.cli.main._get_daemon_port", lambda: 12345)
        monkeypatch.setattr("gitpilot.cli.main._get_api_token", lambda: "token")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": 1, "name": "test"}
        resp.text = ""
        client = MagicMock()
        client.get.return_value = resp
        client.post.return_value = resp
        client.put.return_value = resp
        client.delete.return_value = resp
        monkeypatch.setattr("gitpilot.cli.main._get_client", lambda: client)
        monkeypatch.setattr("httpx.get", MagicMock(return_value=resp))
        monkeypatch.setattr("gitpilot.cli.main._run_setup_if_needed", lambda: None)
        # Prevent interactive prompts in prepare_project_directory
        monkeypatch.setattr("gitpilot.cli.main._prepare_project_directory", lambda *a, **kw: True)
        monkeypatch.setattr("gitpilot.cli.main.SettingsManager", lambda: SettingsManager(config_path=tmp_path / "cfg.json"))
        self.runner = CliRunner()

    def test_daemon_status(self):
        result = self.runner.invoke(cli, ["daemon-status"])
        assert "Daemon is running" in result.output

    def test_add_project(self):
        with patch("gitpilot.cli.main._get_client") as mc:
            mc.return_value.post.return_value.status_code = 201
            mc.return_value.post.return_value.json.return_value = {"id": 1, "name": "test"}
            result = self.runner.invoke(cli, ["add", str(Path.home()), "--name", "t"])
            assert "added" in result.output

    def test_add_conflict(self):
        with patch("gitpilot.cli.main._get_client") as mc:
            mc.return_value.post.return_value.status_code = 409
            result = self.runner.invoke(cli, ["add", str(Path.home())])
            assert "already registered" in result.output

    def test_status(self):
        client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"items": []}
        client.get.return_value = resp
        with patch("gitpilot.cli.main._get_client", return_value=client):
            result = self.runner.invoke(cli, ["status"])
            assert "No projects" in result.output

    def test_log(self):
        client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"items": []}
        client.get.return_value = resp
        with patch("gitpilot.cli.main._get_client", return_value=client):
            result = self.runner.invoke(cli, ["log", "1"])
            assert "No commits" in result.output

    def test_remove(self):
        result = self.runner.invoke(cli, ["remove", "1"])
        assert "removed" in result.output

    def test_config_list(self):
        client = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"items": []}
        client.get.return_value = resp
        with patch("gitpilot.cli.main._get_client", return_value=client):
            result = self.runner.invoke(cli, ["config-list"])
            assert "No configuration" in result.output

    def test_config_set(self):
        result = self.runner.invoke(cli, ["config-set", "k", "v"])
        assert "updated" in result.output

    def test_config_delete(self):
        result = self.runner.invoke(cli, ["config-delete", "k"])
        assert "deleted" in result.output

    def test_split_status_not_git(self):
        result = self.runner.invoke(cli, ["split-status", str(Path.home())])
        assert "Not a Git repository" in result.output

    def test_optimize_not_git(self):
        result = self.runner.invoke(cli, ["optimize", str(Path.home())])
        assert "Not a Git repository" in result.output

    def test_config_review(self):
        result = self.runner.invoke(cli, ["config-review", "on"])
        assert result.exit_code == 0

    def test_key_validation(self):
        assert _validate_api_key_format("xai-123", "grok")
        assert not _validate_api_key_format("bad", "grok")

    def test_get_daemon_port_none(self, tmp_path):
        with patch("gitpilot.cli.main.get_token_path", return_value=tmp_path / "nofile"):
            assert _get_daemon_port() is None

    def test_get_client_no_daemon(self):
        with patch("gitpilot.cli.main._get_daemon_port", return_value=None):
            assert _get_client() is None

    def test_spawn_new_terminal(self):
        with patch("shutil.which", return_value="/usr/bin/xterm"), \
             patch("subprocess.Popen"), patch("sys.exit"):
            _spawn_in_new_terminal()