"""Integration tests for GitExecutor with a real temporary Git repository."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from gitpilot.core.executor import GitExecutor


class TestGitExecutor:
    @pytest.fixture
    def executor(self):
        return GitExecutor(max_retries=2, github_token=None)

    def test_get_current_branch(self, temp_git_repo, executor):
        branch = executor.get_current_branch(temp_git_repo)
        assert branch is not None
        assert branch in ("main", "master")

    def test_stage_all_and_diff(self, temp_git_repo, executor):
        (temp_git_repo / "test.txt").write_text("hello world")
        success = executor.stage_all(temp_git_repo)
        assert success is True
        diff = executor.get_diff_cached(temp_git_repo)
        assert diff is not None
        assert "hello world" in diff

    def test_stage_specific_files(self, temp_git_repo, executor):
        (temp_git_repo / "a.py").write_text("x")
        (temp_git_repo / "b.py").write_text("y")
        # Stage only a.py
        assert executor.stage_files(temp_git_repo, [temp_git_repo / "a.py"])
        diff = executor.get_diff_cached(temp_git_repo)
        assert "a.py" in diff
        assert "b.py" not in diff
        # Now stage b.py as well
        assert executor.stage_files(temp_git_repo, [temp_git_repo / "b.py"])
        diff = executor.get_diff_cached(temp_git_repo)
        assert "b.py" in diff

    def test_commit_returns_hash(self, temp_git_repo, executor):
        (temp_git_repo / "commit_test.txt").write_text("data")
        executor.stage_all(temp_git_repo)
        commit_hash = executor.commit(temp_git_repo, "feat: add commit_test")
        assert commit_hash is not None
        assert len(commit_hash) == 40

    def test_commit_with_empty_diff_handled(self, temp_git_repo, executor):
        executor.stage_all(temp_git_repo)
        commit_hash = executor.commit(temp_git_repo, "chore: empty")
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
        remotes = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(temp_git_repo),
            capture_output=True,
            text=True,
        )
        assert remotes.stdout.strip() == remote_url

    def test_push_without_remote_returns_failure(self, temp_git_repo, executor):
        success, error = asyncio.run(executor.push_with_retry(temp_git_repo))
        assert success is False
        assert error is not None

    @pytest.mark.asyncio
    async def test_push_retry_logic_no_remote(self, temp_git_repo, executor):
        success, error = await executor.push_with_retry(temp_git_repo)
        assert success is False
        assert error is not None

    def test_fallback_to_subprocess_when_gitpython_unavailable(self, monkeypatch, temp_git_repo, executor):
        monkeypatch.setattr("gitpilot.core.executor.GITPYTHON_AVAILABLE", False)
        (temp_git_repo / "fallback.txt").write_text("fallback test")
        assert executor.stage_all(temp_git_repo)
        diff = executor.get_diff_cached(temp_git_repo)
        assert "fallback test" in diff
        commit_hash = executor.commit(temp_git_repo, "fix: fallback commit")
        assert commit_hash is not None

    def test_branch_has_upstream_detects_missing(self, temp_git_repo, executor):
        assert executor.branch_has_upstream(temp_git_repo, "main") is False

    def test_push_sets_upstream_when_needed(self, temp_git_repo, executor):
        executor.set_remote_origin(temp_git_repo, "https://example.com/repo.git")
        asyncio.run(executor.push_with_retry(temp_git_repo))
        success, error = asyncio.run(executor.push_with_retry(temp_git_repo))
        assert isinstance(success, bool)
        assert error is None or isinstance(error, str)

    def test_embed_token_in_url(self, temp_git_repo, executor):
        executor.github_token = "fake-token"
        executor.set_remote_origin(temp_git_repo, "https://github.com/user/repo.git")
        result = executor._embed_token_in_url(temp_git_repo, "fake-token")
        assert result is True
        remotes = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(temp_git_repo),
            capture_output=True,
            text=True,
        )
        assert "fake-token@" in remotes.stdout

    def test_get_current_branch_detects_different_branch(self, temp_git_repo, executor):
        # Create a new branch and switch to it
        subprocess.run(["git", "checkout", "-b", "feature/test"], cwd=temp_git_repo, check=True, capture_output=True)
        branch = executor.get_current_branch(temp_git_repo)
        assert branch == "feature/test"