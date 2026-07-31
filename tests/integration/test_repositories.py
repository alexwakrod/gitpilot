"""Integration tests for SQLite repository layers with an in-memory database."""

import pytest
import sqlite3

from gitpilot.infrastructure.db import run_migrations
from gitpilot.infrastructure.repositories.projects import ProjectsRepository
from gitpilot.infrastructure.repositories.commits import CommitsRepository
from gitpilot.infrastructure.repositories.settings_repo import SettingsRepository
from gitpilot.infrastructure.repositories.discord_webhooks import DiscordWebhooksRepository


class TestProjectsRepository:
    def test_create_and_get(self, memory_db):
        repo = ProjectsRepository(memory_db)
        pid = repo.create("test-project", "/home/user/project", "alex")
        assert pid > 0
        project = repo.get_by_id(pid)
        assert project is not None
        assert project["name"] == "test-project"
        assert project["path"] == "/home/user/project"
        assert project["owner"] == "alex"
        assert project["deleted_at"] is None

    def test_soft_delete(self, memory_db):
        repo = ProjectsRepository(memory_db)
        pid = repo.create("to-delete", "/tmp/del", "alex")
        assert repo.soft_delete(pid) is True
        assert repo.get_by_id(pid) is None

    def test_list_pagination(self, memory_db):
        repo = ProjectsRepository(memory_db)
        owner = "alex"
        for i in range(5):
            repo.create(f"proj-{i}", f"/path/{i}", owner)
        items, cursor = repo.list_all(owner, limit=3)
        assert len(items) == 3
        assert cursor is not None
        items2, cursor2 = repo.list_all(owner, limit=3, cursor=cursor)
        assert len(items2) == 2
        assert cursor2 is None

    def test_update_name(self, memory_db):
        repo = ProjectsRepository(memory_db)
        pid = repo.create("old-name", "/tmp/name", "alex")
        result = repo.update(pid, name="new-name")
        assert result is True
        updated = repo.get_by_id(pid)
        assert updated["name"] == "new-name"


class TestCommitsRepository:
    def test_create_and_list(self, memory_db):
        projects_repo = ProjectsRepository(memory_db)
        pid = projects_repo.create("proj", "/tmp/proj", "alex")
        commits_repo = CommitsRepository(memory_db)
        cid = commits_repo.create(pid, "a" * 40, "feat: test", "main")
        assert cid > 0
        commits, cursor = commits_repo.list_by_project(pid, limit=10)
        assert len(commits) == 1
        assert commits[0]["hash"] == "a" * 40
        assert cursor is None

    def test_domain_column_stored(self, memory_db):
        projects_repo = ProjectsRepository(memory_db)
        pid = projects_repo.create("p", "/p", "alex")
        commits_repo = CommitsRepository(memory_db)
        cid = commits_repo.create(
            pid, "b" * 40, "feat(ui): button", domain="ui",
            affected_symbols=["Button.jsx"], optimization_notes=["use memo"],
        )
        commit = commits_repo.get_by_id(cid)
        assert commit["domain"] == "ui"
        assert "Button.jsx" in commit["affected_symbols"]
        assert "use memo" in commit["optimization_notes"]

    def test_soft_delete(self, memory_db):
        projects_repo = ProjectsRepository(memory_db)
        pid = projects_repo.create("p", "/p", "alex")
        commits_repo = CommitsRepository(memory_db)
        cid = commits_repo.create(pid, "c" * 40, "fix: bug")
        assert commits_repo.soft_delete(cid) is True
        assert commits_repo.get_by_id(cid) is None

    def test_update_message(self, memory_db):
        projects_repo = ProjectsRepository(memory_db)
        pid = projects_repo.create("p", "/p", "alex")
        commits_repo = CommitsRepository(memory_db)
        cid = commits_repo.create(pid, "d" * 40, "old msg")
        commits_repo.update_message(cid, "new msg")
        commit = commits_repo.get_by_id(cid)
        assert commit["message"] == "new msg"

    def test_mark_squash_candidates(self, memory_db):
        projects_repo = ProjectsRepository(memory_db)
        pid = projects_repo.create("p", "/p", "alex")
        commits_repo = CommitsRepository(memory_db)
        cid = commits_repo.create(
            pid, "e" * 40, "feat: one", domain="backend", branch="feat/x",
        )
        # Mark squash candidates
        updated = commits_repo.mark_squash_candidates(pid, "feat/x", "backend", max_age_minutes=60)
        assert updated == 1
        commit = commits_repo.get_by_id(cid)
        assert commit["squash_candidate"] is True


class TestSettingsRepository:
    def test_upsert_and_get(self, memory_db):
        repo = SettingsRepository(memory_db)
        repo.upsert("theme", "dark", "string")
        setting = repo.get_by_key("theme")
        assert setting["value"] == "dark"
        repo.upsert("theme", "light", "string")
        assert repo.get_by_key("theme")["value"] == "light"

    def test_delete(self, memory_db):
        repo = SettingsRepository(memory_db)
        repo.upsert("key1", 123, "integer")
        assert repo.delete("key1") is True
        assert repo.get_by_key("key1") is None

    def test_get_all(self, memory_db):
        repo = SettingsRepository(memory_db)
        repo.upsert("a", 1, "integer")
        repo.upsert("b", "two", "string")
        all_settings = repo.get_all()
        assert len(all_settings) == 2
        assert all_settings["a"]["value"] == 1
        assert all_settings["b"]["value"] == "two"


class TestDiscordWebhooksRepository:
    def test_create_and_list(self, memory_db):
        projects_repo = ProjectsRepository(memory_db)
        pid = projects_repo.create("proj-webhook", "/tmp/web", "alex")
        hooks_repo = DiscordWebhooksRepository(memory_db)
        wid = hooks_repo.create(pid, "https://discord.com/api/webhooks/123/abc")
        assert wid > 0
        hooks = hooks_repo.list_by_project(pid)
        assert len(hooks) == 1
        assert hooks[0]["url"] == "https://discord.com/api/webhooks/123/abc"
        assert hooks[0]["enabled"] is True

    def test_set_enabled(self, memory_db):
        projects_repo = ProjectsRepository(memory_db)
        pid = projects_repo.create("p", "/p", "alex")
        hooks_repo = DiscordWebhooksRepository(memory_db)
        wid = hooks_repo.create(pid, "https://discord.com/api/webhooks/456/def")
        hooks_repo.set_enabled(wid, False)
        hooks = hooks_repo.list_by_project(pid)
        assert len(hooks) == 0  # list only enabled
        hook = hooks_repo.get_by_id(wid)
        assert hook["enabled"] is False

    def test_soft_delete(self, memory_db):
        projects_repo = ProjectsRepository(memory_db)
        pid = projects_repo.create("p", "/p", "alex")
        hooks_repo = DiscordWebhooksRepository(memory_db)
        wid = hooks_repo.create(pid, "https://discord.com/api/webhooks/789/ghi")
        assert hooks_repo.soft_delete(wid) is True
        assert hooks_repo.get_by_id(wid) is None