"""Integration tests for DuckDB repository layers with an in-memory database."""

import pytest
import duckdb

from gitpilot.infrastructure.db import run_migrations
from gitpilot.infrastructure.repositories.projects import ProjectsRepository
from gitpilot.infrastructure.repositories.commits import CommitsRepository
from gitpilot.infrastructure.repositories.settings_repo import SettingsRepository
from gitpilot.infrastructure.repositories.discord_webhooks import DiscordWebhooksRepository


@pytest.fixture
def db_conn():
    """Provide an in-memory DuckDB connection with migrated schema."""
    conn = duckdb.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON;")
    run_migrations(conn)
    yield conn
    conn.close()


class TestProjectsRepository:
    def test_create_and_get(self, db_conn):
        repo = ProjectsRepository(db_conn)
        pid = repo.create("test-project", "/home/user/project", "alex")
        assert pid > 0
        project = repo.get_by_id(pid)
        assert project is not None
        assert project["name"] == "test-project"
        assert project["path"] == "/home/user/project"
        assert project["owner"] == "alex"
        assert project["deleted_at"] is None

    def test_soft_delete(self, db_conn):
        repo = ProjectsRepository(db_conn)
        pid = repo.create("to-delete", "/tmp/del", "alex")
        assert repo.soft_delete(pid) is True
        assert repo.get_by_id(pid) is None

    def test_list_pagination(self, db_conn):
        repo = ProjectsRepository(db_conn)
        owner = "alex"
        for i in range(5):
            repo.create(f"proj-{i}", f"/path/{i}", owner)
        items, cursor = repo.list_all(owner, limit=3)
        assert len(items) == 3
        assert cursor is not None
        items2, cursor2 = repo.list_all(owner, limit=3, cursor=cursor)
        assert len(items2) == 2
        assert cursor2 is None

    def test_update_name(self, db_conn):
        repo = ProjectsRepository(db_conn)
        pid = repo.create("old-name", "/tmp/name", "alex")
        result = repo.update(pid, name="new-name")
        assert result is True
        updated = repo.get_by_id(pid)
        assert updated["name"] == "new-name"


class TestCommitsRepository:
    def test_create_and_list(self, db_conn):
        projects_repo = ProjectsRepository(db_conn)
        pid = projects_repo.create("proj", "/tmp/proj", "alex")
        commits_repo = CommitsRepository(db_conn)
        cid = commits_repo.create(pid, "a" * 40, "feat: test", "main")
        assert cid > 0
        commits, cursor = commits_repo.list_by_project(pid, limit=10)
        assert len(commits) == 1
        assert commits[0]["hash"] == "a" * 40
        assert cursor is None

    def test_soft_delete(self, db_conn):
        projects_repo = ProjectsRepository(db_conn)
        pid = projects_repo.create("p", "/p", "alex")
        commits_repo = CommitsRepository(db_conn)
        cid = commits_repo.create(pid, "b" * 40, "fix: bug")
        assert commits_repo.soft_delete(cid) is True
        assert commits_repo.get_by_id(cid) is None

    def test_update_message(self, db_conn):
        projects_repo = ProjectsRepository(db_conn)
        pid = projects_repo.create("p", "/p", "alex")
        commits_repo = CommitsRepository(db_conn)
        cid = commits_repo.create(pid, "c" * 40, "old msg")
        commits_repo.update_message(cid, "new msg")
        commit = commits_repo.get_by_id(cid)
        assert commit["message"] == "new msg"


class TestSettingsRepository:
    def test_upsert_and_get(self, db_conn):
        repo = SettingsRepository(db_conn)
        repo.upsert("theme", "dark", "string")
        setting = repo.get_by_key("theme")
        assert setting["value"] == "dark"
        repo.upsert("theme", "light", "string")
        assert repo.get_by_key("theme")["value"] == "light"

    def test_delete(self, db_conn):
        repo = SettingsRepository(db_conn)
        repo.upsert("key1", 123, "integer")
        assert repo.delete("key1") is True
        assert repo.get_by_key("key1") is None


class TestDiscordWebhooksRepository:
    def test_create_and_list(self, db_conn):
        projects_repo = ProjectsRepository(db_conn)
        pid = projects_repo.create("proj-webhook", "/tmp/web", "alex")
        hooks_repo = DiscordWebhooksRepository(db_conn)
        wid = hooks_repo.create(pid, "https://discord.com/api/webhooks/123/abc")
        assert wid > 0
        hooks = hooks_repo.list_by_project(pid)
        assert len(hooks) == 1
        assert hooks[0]["url"] == "https://discord.com/api/webhooks/123/abc"
        assert hooks[0]["enabled"] is True

    def test_set_enabled(self, db_conn):
        projects_repo = ProjectsRepository(db_conn)
        pid = projects_repo.create("p", "/p", "alex")
        hooks_repo = DiscordWebhooksRepository(db_conn)
        wid = hooks_repo.create(pid, "https://discord.com/api/webhooks/456/def")
        hooks_repo.set_enabled(wid, False)
        hooks = hooks_repo.list_by_project(pid)
        assert len(hooks) == 0  # list only enabled
        hook = hooks_repo.get_by_id(wid)
        assert hook["enabled"] is False

    def test_soft_delete(self, db_conn):
        projects_repo = ProjectsRepository(db_conn)
        pid = projects_repo.create("p", "/p", "alex")
        hooks_repo = DiscordWebhooksRepository(db_conn)
        wid = hooks_repo.create(pid, "https://discord.com/api/webhooks/789/ghi")
        assert hooks_repo.soft_delete(wid) is True
        assert hooks_repo.get_by_id(wid) is None