"""End-to-end tests simulating full GitPilot flow with a temp git repo, mock AI, and domain splitting (AI grouping disabled for determinism)."""

import json
import os
import subprocess
import time
import asyncio
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import httpx
from fastapi.testclient import TestClient

from gitpilot.daemon.app import create_app
from gitpilot.daemon.lifecycle import DaemonLifecycle
from gitpilot.domain.policies import generate_api_token, get_token_path, get_gitpilot_dir
from gitpilot.domain.settings import SettingsManager
from gitpilot.infrastructure.db import initialize_database


class TestEndToEndFlow:
    @pytest.fixture(autouse=True)
    def setup_environment(self, monkeypatch, tmp_path):
        monkeypatch.setattr("gitpilot.domain.settings.get_gitpilot_dir", lambda: tmp_path)
        monkeypatch.setattr("gitpilot.domain.policies.get_gitpilot_dir", lambda: tmp_path)
        monkeypatch.setattr("gitpilot.infrastructure.db.get_gitpilot_dir", lambda: tmp_path)

        settings = SettingsManager(config_path=tmp_path / "config.json")
        settings.load()
        settings.set("ai_provider", "grok")
        settings.set("ai_model", "grok-2")
        settings.set("grok_api_key", "fake-key")
        settings.set("debounce_interval", 1)
        settings.set("max_commit_retries", 0)
        settings.set("enable_splitting", True)
        settings.set("enable_ai_grouping", False)   # <-- deterministic domain split
        settings.set("enable_optimizations", False)
        settings.set("enable_precommit_checks", False)
        settings.save()

        token = generate_api_token()
        token_path = get_token_path()
        token_path.write_text(token)
        token_path.chmod(0o600)

        initialize_database(tmp_path / "data.db")

        repo_path = tmp_path / "test-repo"
        repo_path.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_path), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@gitpilot.local"], cwd=str(repo_path), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_path), check=True, capture_output=True)
        (repo_path / "README.md").write_text("# Test")
        subprocess.run(["git", "add", "README.md"], cwd=str(repo_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=str(repo_path), check=True, capture_output=True)

        (repo_path / "backend").mkdir(exist_ok=True)
        (repo_path / "ui").mkdir(exist_ok=True)
        (repo_path / "tests").mkdir(exist_ok=True)
        (repo_path / "backend" / "app.py").write_text("# backend")
        (repo_path / "ui" / "Button.jsx").write_text("// ui")
        (repo_path / "tests" / "test_app.py").write_text("# test")
        subprocess.run(["git", "add", "-A"], cwd=str(repo_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add domain files"], cwd=str(repo_path), check=True, capture_output=True)

        self.tmp_path = tmp_path
        self.repo_path = repo_path
        self.token = token
        self.settings = settings

    def test_full_flow_file_change_triggers_domain_commit(self):
        with patch("gitpilot.core.committer.AICommitter.generate_message", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = "feat(backend): test change"

            config = self.settings.load()
            lifecycle = DaemonLifecycle(config=config)
            app = create_app(api_token=self.token, lifecycle=lifecycle, config=config)
            client = TestClient(app, raise_server_exceptions=False)

            response = client.post(
                "/api/v1/projects",
                json={"name": "e2e-test", "path": str(self.repo_path)},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            assert response.status_code == 201, response.text
            project_id = response.json()["id"]

            lifecycle.start()
            time.sleep(0.5)

            (self.repo_path / "backend" / "app.py").write_text("print('updated')")
            time.sleep(2.5)

            lifecycle.stop()

            assert mock_gen.call_count >= 1

            response = client.get(
                f"/api/v1/commits",
                params={"project_id": project_id, "limit": 5},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            assert response.status_code == 200
            commits = response.json()["items"]
            assert len(commits) >= 1
            commit = commits[0]
            assert commit["domain"] == "backend"
            assert commit["message"] == "feat(backend): test change"

    def test_multiple_domains_produce_separate_commits(self):
        with patch("gitpilot.core.committer.AICommitter.generate_message", new_callable=AsyncMock) as mock_gen:
            mock_gen.side_effect = [
                "feat(backend): update API",
                "feat(ui): update button",
            ]

            config = self.settings.load()
            lifecycle = DaemonLifecycle(config=config)
            app = create_app(api_token=self.token, lifecycle=lifecycle, config=config)
            client = TestClient(app)

            response = client.post(
                "/api/v1/projects",
                json={"name": "e2e-split", "path": str(self.repo_path)},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            project_id = response.json()["id"]

            lifecycle.start()
            time.sleep(0.5)

            (self.repo_path / "backend" / "app.py").write_text("backend change")
            (self.repo_path / "ui" / "Button.jsx").write_text("ui change")
            time.sleep(3)

            lifecycle.stop()

            response = client.get(
                f"/api/v1/commits",
                params={"project_id": project_id, "limit": 10},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            commits = response.json()["items"]
            domains = {c["domain"] for c in commits}
            assert "backend" in domains
            assert "ui" in domains

    def test_push_failure_sends_sse_event(self):
        with patch("gitpilot.core.committer.AICommitter.generate_message", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = "feat: sse push fail"

            config = self.settings.load()
            lifecycle = DaemonLifecycle(config=config)
            app = create_app(api_token=self.token, lifecycle=lifecycle, config=config)
            client = TestClient(app)

            resp = client.post(
                "/api/v1/projects",
                json={"name": "sse-test", "path": str(self.repo_path)},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            project_id = resp.json()["id"]

            lifecycle.start()
            time.sleep(0.5)

            (self.repo_path / "sse_test.txt").write_text("sse content")
            time.sleep(3.0)

            lifecycle.stop()

            response = client.get(
                f"/api/v1/commits",
                params={"project_id": project_id, "limit": 5},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            assert response.status_code == 200
            commits = response.json()["items"]
            assert len(commits) >= 1
            assert commits[0]["message"] == "feat: sse push fail"

    def test_project_ready_check_on_add(self):
        plain_dir = self.tmp_path / "not-a-repo"
        plain_dir.mkdir()
        (plain_dir / "file.txt").write_text("data")

        config = self.settings.load()
        lifecycle = DaemonLifecycle(config=config)
        app = create_app(api_token=self.token, lifecycle=lifecycle, config=config)
        client = TestClient(app)

        resp = client.post(
            "/api/v1/projects",
            json={"name": "bad", "path": str(plain_dir)},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        assert resp.status_code == 201
        assert (plain_dir / ".git").exists()