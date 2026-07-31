"""Extensive unit tests for CLI commands, settings, project setup, and daemon lifecycle – using mocked daemon client."""

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import httpx
import pytest
import readchar
from click.testing import CliRunner

from gitpilot.cli.main import (
    cli,
    _prepare_project_directory,
    _validate_api_key_format,
    _prompt_api_key_with_test,
    _spawn_in_new_terminal,
    _get_daemon_port,
    _get_api_token,
    _get_client,
)
from gitpilot.core.project_setup import is_git_repo, ensure_initial_commit
from gitpilot.daemon.lifecycle import DaemonLifecycle
from gitpilot.daemon.app import create_app, sse_clients, _main_loop
from gitpilot.domain.settings import SettingsManager, get_gitpilot_dir
from gitpilot.domain.policies import get_token_path
from gitpilot.infrastructure.db import initialize_database


def _mock_client(status=200, json_data=None):
    """Create a fake httpx.Client that returns the given status and JSON."""
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = json_data or {}
    resp.text = ""
    client.get.return_value = resp
    client.post.return_value = resp
    client.put.return_value = resp
    client.delete.return_value = resp
    return client


class TestCLICommands:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch, tmp_path):
        """Mock the daemon port, token, and client so commands don't fail."""
        monkeypatch.setattr("gitpilot.cli.main._get_daemon_port", lambda: 12345)
        monkeypatch.setattr("gitpilot.cli.main._get_api_token", lambda: "faketoken")
        fake_client = _mock_client()
        monkeypatch.setattr("gitpilot.cli.main._get_client", lambda: fake_client)
        # Mock httpx.get used by daemon_status command
        monkeypatch.setattr("httpx.get", MagicMock(return_value=_mock_client(200).get.return_value))
        # Prevent auto-starting daemon background process
        monkeypatch.setattr("gitpilot.cli.main._run_setup_if_needed", lambda: None)
        self.client = fake_client
        self.runner = CliRunner()

    def test_daemon_status(self):
        result = self.runner.invoke(cli, ["daemon-status"])
        assert "Daemon is running" in result.output

    def test_add_project(self, tmp_path):
        # Create a temporary git repo so _prepare_project_directory succeeds
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
        (repo / "file").write_text("x")
        subprocess.run(["git", "add", "file"], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        self.client.post.return_value.status_code = 201
        self.client.post.return_value.json.return_value = {"id": 1, "name": "test"}
        result = self.runner.invoke(cli, ["add", str(repo), "--name", "test"])
        assert "added" in result.output

    def test_add_project_conflict(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
        (repo / "file").write_text("x")
        subprocess.run(["git", "add", "file"], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        self.client.post.return_value.status_code = 409
        result = self.runner.invoke(cli, ["add", str(repo), "--name", "test"])
        assert "already registered" in result.output

    def test_status(self):
        self.client.get.return_value.json.return_value = {"items": [{"id": 1, "name": "p", "path": "/p", "deleted_at": None}]}
        result = self.runner.invoke(cli, ["status"])
        assert "Watched Projects" in result.output
        assert "p" in result.output

    def test_log(self):
        self.client.get.return_value.json.return_value = {"items": [
            {"id": 1, "hash": "a"*40, "message": "fix: x", "domain": "backend", "branch": "main", "committed_at": "2025-01-01T00:00:00"}
        ]}
        result = self.runner.invoke(cli, ["log", "1"])
        assert "fix: x" in result.output
        assert "backend" in result.output

    def test_log_empty(self):
        self.client.get.return_value.json.return_value = {"items": []}
        result = self.runner.invoke(cli, ["log", "1"])
        assert "No commits" in result.output

    def test_remove(self):
        self.client.delete.return_value.status_code = 200
        result = self.runner.invoke(cli, ["remove", "1"])
        assert "removed" in result.output

    def test_config_list(self):
        self.client.get.return_value.json.return_value = {"items": [{"key": "theme", "value": "dark", "type": "string"}]}
        result = self.runner.invoke(cli, ["config-list"])
        assert "dark" in result.output

    def test_config_set(self):
        self.client.put.return_value.status_code = 200
        result = self.runner.invoke(cli, ["config-set", "key", "val"])
        assert "updated" in result.output

    def test_config_delete(self):
        self.client.delete.return_value.status_code = 200
        result = self.runner.invoke(cli, ["config-delete", "key"])
        assert "deleted" in result.output

    def test_split_status_not_git(self):
        result = self.runner.invoke(cli, ["split-status", str(Path.home())])
        assert "Not a Git repository" in result.output

    def test_suggest(self, monkeypatch, tmp_path):
        # Mock the DB and repositories
        monkeypatch.setattr("gitpilot.cli.main.managed_connection", lambda *a, **kw: MagicMock())
        monkeypatch.setattr("gitpilot.cli.main.ProjectsRepository", MagicMock())
        monkeypatch.setattr("gitpilot.cli.main.CommitsRepository", MagicMock())
        with patch("gitpilot.cli.main.ProjectsRepository.list_all", return_value=([], None)):
            with patch("gitpilot.cli.main.CommitsRepository.list_by_project", return_value=([], None)):
                result = self.runner.invoke(cli, ["suggest"])
                assert "No squash suggestions" in result.output or "Squash Suggestions" in result.output

    def test_optimize_not_git(self):
        result = self.runner.invoke(cli, ["optimize", str(Path.home())])
        assert "Not a Git repository" in result.output

    def test_stats(self, monkeypatch, tmp_path):
        monkeypatch.setattr("gitpilot.cli.main.managed_connection", lambda *a, **kw: MagicMock())
        monkeypatch.setattr("gitpilot.cli.main.PatternsRepository", MagicMock())
        monkeypatch.setattr("gitpilot.cli.main.CommitsRepository", MagicMock())
        monkeypatch.setattr("gitpilot.cli.main.ProjectsRepository", MagicMock())
        with patch("gitpilot.cli.main.PatternsRepository.list_by_owner", return_value=[]):
            with patch("gitpilot.cli.main.ProjectsRepository.list_all", return_value=([], None)):
                result = self.runner.invoke(cli, ["stats"])
                assert "No learned patterns" in result.output

    def test_config_review(self):
        with patch.object(SettingsManager, "set") as mock_set:
            result = self.runner.invoke(cli, ["config-review", "on"])
            assert result.exit_code == 0

    def test_watch(self):
        # Properly mock async stream for SSE
        async def async_stream():
            yield f"event: connected\ndata: {json.dumps({'status': 'connected'})}\n\n"
        mock_stream = MagicMock()
        mock_stream.return_value.__aiter__.return_value = async_stream()
        with patch("httpx.AsyncClient.stream", mock_stream):
            result = self.runner.invoke(cli, ["watch"])
            # watch command should not crash
            assert result.exit_code == 0


class TestKeyValidation:
    def test_validate_grok_valid(self):
        assert _validate_api_key_format("xai-something", "grok") is True

    def test_validate_grok_invalid(self):
        assert _validate_api_key_format("bad", "grok") is False

    def test_validate_groq_valid(self):
        assert _validate_api_key_format("gsk_xyz", "groq") is True

    def test_validate_qwen_valid(self):
        assert _validate_api_key_format("sk-ws-abc", "qwen") is True

    def test_validate_qwen_invalid(self):
        assert _validate_api_key_format("sk-abc", "qwen") is False

    def test_validate_openai_valid(self):
        assert _validate_api_key_format("sk-proj-abc", "openai") is True

    def test_validate_anthropic_valid(self):
        assert _validate_api_key_format("sk-ant-api-xyz", "anthropic") is True

    def test_validate_empty(self):
        assert _validate_api_key_format("", "grok") is False
        assert _validate_api_key_format(None, "grok") is False


class TestPrepareProjectDirectory:
    def test_existing_repo_ready(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
        (repo / "file").write_text("test")
        subprocess.run(["git", "add", "file"], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
        settings_mgr = SettingsManager(config_path=tmp_path / "config.json")
        settings_mgr.load()
        with patch("gitpilot.cli.main.Confirm.ask", return_value=False):
            assert _prepare_project_directory(repo, settings_mgr) is True

    def test_missing_git_repo(self, tmp_path):
        plain_dir = tmp_path / "plain"
        plain_dir.mkdir()
        settings_mgr = SettingsManager(config_path=tmp_path / "config.json")
        settings_mgr.load()
        with patch("gitpilot.cli.main.Confirm.ask", return_value=False):
            assert _prepare_project_directory(plain_dir, settings_mgr) is False

    def test_init_git_if_missing(self, tmp_path):
        plain_dir = tmp_path / "plain2"
        plain_dir.mkdir()
        settings_mgr = SettingsManager(config_path=tmp_path / "config.json")
        settings_mgr.load()
        with patch("gitpilot.cli.main.Confirm.ask", return_value=True):
            assert _prepare_project_directory(plain_dir, settings_mgr) is True
            assert (plain_dir / ".git").exists()


class TestDaemonLifecycleStartup:
    def test_verify_global_git_config(self, monkeypatch):
        config = {"debounce_interval": 120, "max_commit_retries": 3}
        lifecycle = DaemonLifecycle(config)
        mock_run = MagicMock()
        mock_run.return_value.stdout = "Test User\ntest@email.com\n"
        with patch("subprocess.run", return_value=mock_run.return_value):
            lifecycle._verify_global_git_config()

    def test_validate_registered_projects_skips_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr("gitpilot.domain.settings.get_gitpilot_dir", lambda: tmp_path)
        monkeypatch.setattr("gitpilot.infrastructure.db.get_gitpilot_dir", lambda: tmp_path)
        initialize_database(tmp_path / "data.db")

        config = {"debounce_interval": 120}
        lifecycle = DaemonLifecycle(config)
        from gitpilot.infrastructure.db import managed_connection
        with managed_connection(tmp_path / "data.db") as conn:
            conn.execute("INSERT INTO projects (name,path,owner) VALUES (?,?,?)",
                         ["test", "/nonexistent/path", "user"])
            conn.commit()
        lifecycle._validate_registered_projects()


class TestSSEBroadcasting:
    def test_create_app_sets_callbacks(self):
        lifecycle = DaemonLifecycle({})
        app = create_app(api_token="test", lifecycle=lifecycle, config={})
        assert lifecycle.on_commit_completed is not None
        assert lifecycle.on_push_failed is not None
        assert lifecycle.on_watcher_status is not None


class TestHelpers:
    def test_get_daemon_port_no_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr("gitpilot.cli.main.get_token_path", lambda: tmp_path / "nonexistent")
        assert _get_daemon_port() is None

    def test_get_client_no_daemon(self, monkeypatch):
        monkeypatch.setattr("gitpilot.cli.main._get_daemon_port", lambda: None)
        # _get_client prints an error and returns None; it does not call sys.exit
        client = _get_client()
        assert client is None

    def test_spawn_new_terminal_linux(self, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda x: "/usr/bin/xterm" if x == "xterm" else None)
        with patch("subprocess.Popen") as mock_popen, patch("sys.exit") as mock_exit:
            _spawn_in_new_terminal()
            mock_popen.assert_called_once()
            mock_exit.assert_called_once()

    def test_key_format_edge_cases(self):
        assert not _validate_api_key_format("xai", "grok")
        assert _validate_api_key_format("xai-123", "grok") is True