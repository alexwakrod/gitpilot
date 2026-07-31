"""Fast CLI and daemon tests – no blocking calls."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from gitpilot.cli.main import (
    cli, _validate_api_key_format, _get_daemon_port, _get_client, _spawn_in_new_terminal,
    _prepare_project_directory,
)
from gitpilot.daemon.app import create_app
from gitpilot.daemon.lifecycle import DaemonLifecycle
from gitpilot.domain.settings import SettingsManager
from gitpilot.domain.policies import get_token_path, generate_api_token
from gitpilot.infrastructure.db import initialize_database


class TestCLICommandsFast:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr("gitpilot.cli.main._get_daemon_port", lambda: 12345)
        monkeypatch.setattr("gitpilot.cli.main._get_api_token", lambda: "token")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {}
        resp.text = ""
        client = MagicMock()
        client.get.return_value = resp
        client.post.return_value = resp
        client.put.return_value = resp
        client.delete.return_value = resp
        monkeypatch.setattr("gitpilot.cli.main._get_client", lambda: client)
        monkeypatch.setattr("httpx.get", MagicMock(return_value=resp))
        monkeypatch.setattr("gitpilot.cli.main._run_setup_if_needed", lambda: None)
        monkeypatch.setattr("gitpilot.cli.main.SettingsManager", lambda: SettingsManager(config_path=tmp_path / "cfg.json"))
        self.runner = CliRunner()

    def test_daemon_status(self):
        result = self.runner.invoke(cli, ["daemon-status"])
        assert "Daemon is running" in result.output

    def test_add_project(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)
        (repo / "file").write_text("x")
        subprocess.run(["git", "add", "file"], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        result = self.runner.invoke(cli, ["add", str(repo), "--name", "t"])
        assert "added" in result.output

    def test_add_conflict(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)
        (repo / "f").write_text("x")
        subprocess.run(["git", "add", "f"], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "i"], cwd=repo, capture_output=True)
        with patch("gitpilot.cli.main._get_client") as mc:
            mc.return_value.post.return_value.status_code = 409
            result = self.runner.invoke(cli, ["add", str(repo)])
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
        assert _validate_api_key_format("gsk_123", "groq")
        assert _validate_api_key_format("sk-ws-123", "qwen")
        assert _validate_api_key_format("sk-proj-123", "openai")
        assert _validate_api_key_format("sk-ant-api-123", "anthropic")

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
            # no crash