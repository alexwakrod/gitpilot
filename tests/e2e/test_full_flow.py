"""End-to-end tests simulating full GitPilot flow with a temp git repo and mock AI."""

import json
import os
import subprocess
import time
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from fastapi.testclient import TestClient

from gitpilot.daemon.app import create_app
from gitpilot.daemon.lifecycle import DaemonLifecycle
from gitpilot.domain.policies import generate_api_token, get_token_path, get_gitpilot_dir
from gitpilot.domain.settings import SettingsManager, DEFAULT_CONFIG
from gitpilot.infrastructure.db import initialize_database


class TestEndToEndFlow:
    """Complete end-to-end test: file change → watcher → AI commit → DB record → SSE event."""

    @pytest.fixture(autouse=True)
    def setup_environment(self, monkeypatch, tmp_path):
        """Create a temporary GitPilot environment with config, token, DB, and a git repo."""
        # Override get_gitpilot_dir to use tmp_path
        monkeypatch.setattr("gitpilot.domain.settings.get_gitpilot_dir", lambda: tmp_path)
        monkeypatch.setattr("gitpilot.domain.policies.get_gitpilot_dir", lambda: tmp_path)
        monkeypatch.setattr("gitpilot.infrastructure.db.get_gitpilot_dir", lambda: tmp_path)

        # Write config with test AI provider (will be mocked)
        settings = SettingsManager(config_path=tmp_path / "config.json")
        settings.load()
        settings.set("ai_provider", "grok")
        settings.set("ai_model", "grok-2")
        settings.set("grok_api_key", "fake-key")
        settings.set("debounce_interval", 1)  # short debounce for testing
        settings.set("max_commit_retries", 0)  # no retries in test
        settings.save()

        # Generate token
        token = generate_api_token()
        token_path = get_token_path()
        token_path.write_text(token)
        token_path.chmod(0o600)

        # Initialize database
        initialize_database(tmp_path / "data.db")

        # Create temp git repo
        repo_path = tmp_path / "test-repo"
        repo_path.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@gitpilot.local"], cwd=str(repo_path), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_path), check=True, capture_output=True)
        # Create initial commit
        (repo_path / "README.md").write_text("# Test")
        subprocess.run(["git", "add", "README.md"], cwd=str(repo_path), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=str(repo_path), check=True, capture_output=True)

        self.tmp_path = tmp_path
        self.repo_path = repo_path
        self.token = token
        self.settings = settings

    def test_full_flow_file_change_triggers_commit(self):
        """Modify a file, wait for debounce, verify commit recorded and SSE event sent."""
        # Mock the AI committer to return a fixed message immediately
        with patch("gitpilot.core.committer.AICommitter.generate_message", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = "feat: test change"

            # Build config and lifecycle
            config = self.settings.load()
            lifecycle = DaemonLifecycle(config=config)

            # Create FastAPI app
            app = create_app(api_token=self.token, lifecycle=lifecycle, config=config)
            client = TestClient(app, raise_server_exceptions=False)

            # Register the temp repo as a project
            response = client.post(
                "/api/v1/projects",
                json={"name": "e2e-test", "path": str(self.repo_path)},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            assert response.status_code == 201, response.text
            project_id = response.json()["id"]

            # Start lifecycle (watcher) in a background thread to handle events
            # We need the watcher to respond to file changes.
            # The lifecycle.start() will spin up a watchdog thread.
            lifecycle.start()

            # Give the watcher a moment to initialize
            time.sleep(0.5)

            # Modify a file within the repo
            test_file = self.repo_path / "test.py"
            test_file.write_text("print('hello')")
            test_file.touch()  # ensure mtime updates

            # Wait for debounce interval (1s) + processing time (AI mock is instant)
            # The watcher should stage, diff, call AI, commit, and push (push will fail because no remote)
            time.sleep(2.5)

            # Stop lifecycle to avoid thread leaks
            lifecycle.stop()

            # Verify that the AI was called
            mock_gen.assert_called_once()
            args, kwargs = mock_gen.call_args
            assert "print('hello')" in kwargs.get("diff") or args[0]

            # Query commits from API
            response = client.get(
                f"/api/v1/commits",
                params={"project_id": project_id, "limit": 5},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            assert response.status_code == 200
            commits = response.json()["items"]
            assert len(commits) >= 1
            commit = commits[0]
            assert commit["message"] == "feat: test change"
            assert commit["project_id"] == project_id
            assert len(commit["hash"]) == 40

            # Verify project is active
            response = client.get(
                f"/api/v1/projects/{project_id}",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            assert response.status_code == 200
            assert response.json()["deleted_at"] is None

    def test_soft_delete_project_stops_watching(self):
        """After soft-deleting a project, further file changes should not cause commits."""
        with patch("gitpilot.core.committer.AICommitter.generate_message", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = "fix: not called"

            config = self.settings.load()
            lifecycle = DaemonLifecycle(config=config)
            app = create_app(api_token=self.token, lifecycle=lifecycle, config=config)
            client = TestClient(app)

            # Register project
            resp = client.post(
                "/api/v1/projects",
                json={"name": "delete-test", "path": str(self.repo_path)},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            project_id = resp.json()["id"]

            lifecycle.start()
            time.sleep(0.5)

            # Soft delete via API
            resp = client.delete(
                f"/api/v1/projects/{project_id}",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            assert resp.status_code == 200

            # Wait for watcher to stop
            time.sleep(0.5)

            # Modify file
            (self.repo_path / "delete_me.py").write_text("x=1")

            # Wait a bit more than debounce interval
            time.sleep(2)

            lifecycle.stop()

            # AI should NOT have been called
            mock_gen.assert_not_called()

            # No commits for this project should appear (old commits still visible)
            resp = client.get(
                f"/api/v1/commits",
                params={"project_id": project_id},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            # Only the initial commit from repo init (not recorded by GitPilot)
            # GitPilot only records commits it makes. So the list should be empty or only contain those from before delete if any.
            commits = resp.json()["items"]
            # No commits should have been generated after delete
            assert len(commits) == 0

    def test_push_failure_sends_sse_event(self):
        """Simulate push failure and verify SSE event is emitted."""
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

            # Start SSE listener in a background thread to collect events
            events = []

            def sse_listener():
                with client.stream(
                    "GET",
                    "/api/v1/events",
                    headers={"Authorization": f"Bearer {self.token}"},
                ) as response:
                    for line in response.iter_lines():
                        if line.startswith("event:"):
                            event_type = line.split(":")[1].strip()
                            # wait for data line
                            data_line = next(response.iter_lines())
                            if data_line.startswith("data:"):
                                data = json.loads(data_line[5:].strip())
                                events.append({"event": event_type, "data": data})
                        # only collect push_failed and commit_completed
                        if len(events) >= 2:
                            break

            import threading
            listener_thread = threading.Thread(target=sse_listener, daemon=True)
            listener_thread.start()

            # Allow SSE to connect
            time.sleep(0.5)

            # Modify a file (push will fail because no remote configured)
            (self.repo_path / "sse_test.txt").write_text("sse content")
            time.sleep(2.5)

            lifecycle.stop()
            listener_thread.join(timeout=3)

            # Should have received commit_completed and push_failed
            event_types = [e["event"] for e in events]
            assert "commit_completed" in event_types
            assert "push_failed" in event_types

            push_fail_event = next(e for e in events if e["event"] == "push_failed")
            assert push_fail_event["data"]["project_id"] == project_id
            assert "error" in push_fail_event["data"]

    def test_multiple_projects_independent(self):
        """Two projects watched concurrently; changes in one don't affect the other."""
        with patch("gitpilot.core.committer.AICommitter.generate_message", new_callable=AsyncMock) as mock_gen:
            mock_gen.return_value = "feat: independent"

            config = self.settings.load()
            lifecycle = DaemonLifecycle(config=config)
            app = create_app(api_token=self.token, lifecycle=lifecycle, config=config)
            client = TestClient(app)

            # Create a second repo
            repo2 = self.tmp_path / "repo2"
            repo2.mkdir()
            subprocess.run(["git", "init"], cwd=str(repo2), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test2@gitpilot.local"], cwd=str(repo2), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Test2"], cwd=str(repo2), check=True, capture_output=True)
            (repo2 / "README.md").write_text("# Repo2")
            subprocess.run(["git", "add", "README.md"], cwd=str(repo2), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo2), check=True, capture_output=True)

            # Register both projects
            resp1 = client.post(
                "/api/v1/projects",
                json={"name": "proj1", "path": str(self.repo_path)},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            assert resp1.status_code == 201
            pid1 = resp1.json()["id"]

            resp2 = client.post(
                "/api/v1/projects",
                json={"name": "proj2", "path": str(repo2)},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            assert resp2.status_code == 201
            pid2 = resp2.json()["id"]

            lifecycle.start()
            time.sleep(0.5)

            # Change file only in proj1
            (self.repo_path / "unique1.py").write_text("data1")
            time.sleep(2.5)

            # After debounce, commits should be created for proj1 only
            resp = client.get(
                f"/api/v1/commits",
                params={"project_id": pid1, "limit": 5},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            commits1 = resp.json()["items"]
            assert len(commits1) >= 1
            assert commits1[0]["project_id"] == pid1

            resp = client.get(
                f"/api/v1/commits",
                params={"project_id": pid2, "limit": 5},
                headers={"Authorization": f"Bearer {self.token}"},
            )
            commits2 = resp.json()["items"]
            assert len(commits2) == 0

            lifecycle.stop()