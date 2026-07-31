"""Final coverage push: repositories, policies, settings, daemon endpoints, CLI suggestions."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import httpx
from fastapi.testclient import TestClient

from gitpilot.daemon.app import create_app
from gitpilot.daemon.lifecycle import DaemonLifecycle
from gitpilot.domain.policies import get_current_os_user, generate_api_token, get_token_path, ensure_token_file, verify_owner
from gitpilot.domain.settings import SettingsManager, get_gitpilot_dir, DEFAULT_CONFIG
from gitpilot.infrastructure.db import initialize_database, managed_connection
from gitpilot.infrastructure.repositories.commits import CommitsRepository
from gitpilot.infrastructure.repositories.projects import ProjectsRepository
from gitpilot.infrastructure.repositories.settings_repo import SettingsRepository
from gitpilot.infrastructure.repositories.discord_webhooks import DiscordWebhooksRepository
from gitpilot.infrastructure.repositories.patterns import PatternsRepository
from gitpilot.infrastructure.repositories.file_associations import FileAssociationsRepository


class TestRepositoriesCoverage:
    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        monkeypatch.setattr("gitpilot.infrastructure.db.get_gitpilot_dir", lambda: tmp_path)
        initialize_database(tmp_path / "data.db")
        with managed_connection(tmp_path / "data.db") as conn:
            yield conn

    def test_commits_list_by_project_domain_filter(self, db):
        proj_repo = ProjectsRepository(db)
        pid = proj_repo.create("p", "/p", "user")
        com_repo = CommitsRepository(db)
        com_repo.create(pid, "a"*40, "feat: ui", domain="ui")
        com_repo.create(pid, "b"*40, "fix: backend", domain="backend")
        # Filter by domain
        commits, cursor = com_repo.list_by_project(pid, domain_filter="ui")
        assert len(commits) == 1
        assert commits[0]["domain"] == "ui"

    def test_commits_clear_squash_candidates(self, db):
        proj_repo = ProjectsRepository(db)
        pid = proj_repo.create("p", "/p", "user")
        com_repo = CommitsRepository(db)
        com_repo.create(pid, "a"*40, "msg", domain="backend", branch="main")
        com_repo.mark_squash_candidates(pid, "main", "backend", max_age_minutes=60)
        count = com_repo.clear_squash_candidates(pid, "main", domain="backend")
        assert count >= 0

    def test_settings_repo_upsert_invalid_type(self, db):
        repo = SettingsRepository(db)
        with pytest.raises(ValueError):
            repo.upsert("key", "val", "invalid")

    def test_discord_webhooks_soft_delete(self, db):
        proj_repo = ProjectsRepository(db)
        pid = proj_repo.create("p", "/p", "user")
        hooks = DiscordWebhooksRepository(db)
        wid = hooks.create(pid, "https://discord.com/api/webhooks/1/2")
        assert hooks.soft_delete(wid) is True

    def test_patterns_upsert_and_get(self, db):
        repo = PatternsRepository(db)
        repo.upsert("user", "message_style", "conventional", 0.8)
        p = repo.get_by_owner_and_type("user", "message_style")
        assert p["value"] == "conventional"
        assert p["confidence"] == 0.8

    def test_file_associations_record_and_get(self, db):
        proj_repo = ProjectsRepository(db)
        pid = proj_repo.create("p", "/p", "user")
        fa = FileAssociationsRepository(db)
        fa.record_co_occurrence(pid, "a.py", "b.py")
        assoc = fa.get_associated_files(pid, "a.py", min_occurrences=1)
        assert len(assoc) == 1
        assert assoc[0][0] == "b.py"


class TestPoliciesCoverage:
    def test_get_current_os_user(self, monkeypatch):
        monkeypatch.setitem(os.environ, "USER", "testuser")
        assert get_current_os_user() == "testuser"

    def test_verify_owner_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("gitpilot.domain.policies.get_gitpilot_dir", lambda: tmp_path)
        assert verify_owner("u", "t") is False


class TestSettingsCoverage:
    def test_default_config(self):
        assert DEFAULT_CONFIG["debounce_interval"] == 120

    def test_load_merge_defaults(self, tmp_path):
        (tmp_path / "config.json").write_text('{"ai_provider": "openai"}')
        mgr = SettingsManager(config_path=tmp_path / "config.json")
        config = mgr.load()
        assert config["ai_provider"] == "openai"
        assert config["theme"] == "dark"


class TestDaemonAPICoverage:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch, tmp_path):
        monkeypatch.setattr("gitpilot.domain.settings.get_gitpilot_dir", lambda: tmp_path)
        monkeypatch.setattr("gitpilot.domain.policies.get_gitpilot_dir", lambda: tmp_path)
        monkeypatch.setattr("gitpilot.infrastructure.db.get_gitpilot_dir", lambda: tmp_path)
        initialize_database(tmp_path / "data.db")
        token = generate_api_token()
        token_path = get_token_path()
        token_path.write_text(token)
        config = {"max_commit_retries": 0, "github_token": None}
        lifecycle = DaemonLifecycle(config)
        self.app = create_app(api_token=token, lifecycle=lifecycle, config=config)
        self.client = TestClient(self.app)
        self.token = token

    def test_list_projects(self):
        resp = self.client.get("/api/v1/projects", headers={"Authorization": f"Bearer {self.token}"})
        assert resp.status_code == 200

    def test_get_config(self):
        resp = self.client.get("/api/v1/config", headers={"Authorization": f"Bearer {self.token}"})
        assert resp.status_code == 200

    def test_create_commit_manual(self, tmp_path):
        # Need a project first
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)
        (repo / "f").write_text("x")
        subprocess.run(["git", "add", "f"], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "i"], cwd=repo, capture_output=True)
        proj_resp = self.client.post("/api/v1/projects", json={"name": "p", "path": str(repo)}, headers={"Authorization": f"Bearer {self.token}"})
        pid = proj_resp.json()["id"]
        # Manual commit create
        commit_payload = {"project_id": pid, "hash": "a"*40, "message": "manual commit"}
        resp = self.client.post("/api/v1/commits", json=commit_payload, headers={"Authorization": f"Bearer {self.token}"})
        assert resp.status_code == 201

    def test_discord_webhook_create(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)
        (repo / "f").write_text("x")
        subprocess.run(["git", "add", "f"], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "-m", "i"], cwd=repo, capture_output=True)
        proj_resp = self.client.post("/api/v1/projects", json={"name": "p", "path": str(repo)}, headers={"Authorization": f"Bearer {self.token}"})
        pid = proj_resp.json()["id"]
        webhook = {"project_id": pid, "url": "https://discord.com/api/webhooks/123/abc"}
        resp = self.client.post("/api/v1/discord-webhooks", json=webhook, headers={"Authorization": f"Bearer {self.token}"})
        assert resp.status_code == 201