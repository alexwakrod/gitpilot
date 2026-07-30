"""Integration tests for GitExecutor with a real temporary Git repository."""

import subprocess
from pathlib import Path

import pytest

from gitpilot.core.executor import GitExecutor


class TestGitExecutor:
    @pytest.fixture
    def executor(self):
        return GitExecutor(max_retries=1, github_token=None)

    def test_get_current_branch(self, temp_git_repo, executor):
        branch = executor.get_current_branch(temp_git_repo)
        assert branch is not None
        # Default branch can be main or master
        assert branch in ("main", "master")

    def test_stage_all_and_diff(self, temp_git_repo, executor):
        # Modify a file
        (temp_git_repo / "test.txt").write_text("hello world")
        success = executor.stage_all(temp_git_repo)
        assert success is True
        diff = executor.get_diff_cached(temp_git_repo)
        assert diff is not None
        assert "hello world" in diff

    def test_commit_returns_hash(self, temp_git_repo, executor):
        (temp_git_repo / "commit_test.txt").write_text("data")
        executor.stage_all(temp_git_repo)
        commit_hash = executor.commit(temp_git_repo, "feat: add commit_test")
        assert commit_hash is not None
        assert len(commit_hash) == 40

    def test_commit_with_empty_diff_handled(self, temp_git_repo, executor):
        # No changes staged, diff will be empty, commit should fail gracefully
        executor.stage_all(temp_git_repo)
        commit_hash = executor.commit(temp_git_repo, "chore: empty")
        # May return None or empty hash depending on git behavior
        # Just ensure it doesn't raise
        assert commit_hash is None or len(commit_hash) == 40

    def test_stage_and_commit_multiple_files(self, temp_git_repo, executor):
        for name in ["a.txt", "b.txt", "c.txt"]:
            (temp_git_repo / name).write_text(f"Content of {name}")
        assert executor.stage_all(temp_git_repo)
        diff = executor.get_diff_cached(temp_git_repo)
        assert diff
        assert "a.txt" in diff
        assert "b.txt" in diff
        assert "c.txt" in diff
        commit_hash = executor.commit(temp_git_repo, "feat: multiple files")
        assert commit_hash is not None

    def test_init_repo(self, tmp_path, executor):
        new_dir = tmp_path / "new_repo"
        new_dir.mkdir()
        result = executor.init_repo(new_dir)
        assert result is True
        assert (new_dir / ".git").is_dir()

    def test_set_remote_origin(self, temp_git_repo, executor):
        remote_url = "https://github.com/user/repo.git"
        result = executor.set_remote_origin(temp_git_repo, remote_url)
        assert result is True
        # Verify remote is set
        remotes = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(temp_git_repo),
            capture_output=True,
            text=True,
        )
        assert remotes.stdout.strip() == remote_url

    def test_push_without_remote_returns_failure(self, temp_git_repo, executor):
        # No remote configured, push should fail gracefully
        import asyncio
        success, error = asyncio.run(executor.push_with_retry(temp_git_repo))
        assert success is False
        assert error is not None

    @pytest.mark.asyncio
    async def test_push_retry_logic_no_remote(self, temp_git_repo, executor):
        # No remote, should fail with message, no retries if immediate failure
        success, error = await executor.push_with_retry(temp_git_repo)
        assert success is False
        assert error is not None

    def test_fallback_to_subprocess_when_gitpython_unavailable(self, monkeypatch, temp_git_repo, executor):
        # Simulate gitpython import failure
        monkeypatch.setattr("gitpilot.core.executor.GITPYTHON_AVAILABLE", False)
        (temp_git_repo / "fallback.txt").write_text("fallback test")
        assert executor.stage_all(temp_git_repo)
        diff = executor.get_diff_cached(temp_git_repo)
        assert "fallback test" in diff
        commit_hash = executor.commit(temp_git_repo, "fix: fallback commit")
        assert commit_hash is not None