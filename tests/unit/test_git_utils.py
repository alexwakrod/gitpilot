"""Unit tests for native Git porcelain utilities (mocked subprocess)."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from gitpilot.core import git_utils


class TestRunGit:
    def test_run_git_success(self):
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = "output"
        mock.stderr = ""
        with patch("subprocess.run", return_value=mock) as run:
            result = git_utils.run_git(["git", "status"], Path("/tmp"))
            assert result.returncode == 0
            assert result.stdout == "output"

    def test_run_git_failure_logs_warning(self, caplog):
        mock = MagicMock()
        mock.returncode = 1
        mock.stderr = "error"
        with patch("subprocess.run", return_value=mock):
            result = git_utils.run_git(["git", "bad"], Path("/tmp"))
            assert result.returncode == 1
            assert "Git command failed" in caplog.text


class TestGetPorcelainStatus:
    def test_parses_modified_file(self):
        raw = " M file.txt\0"
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = raw
            changes = git_utils.get_porcelain_status(Path("/tmp"))
            assert len(changes) == 1
            assert changes[0].path == Path("file.txt")
            assert changes[0].index_status == " "
            assert changes[0].worktree_status == "M"
            assert not changes[0].is_staged
            assert changes[0].is_unstaged

    def test_parses_rename(self):
        raw = "R  new.txt\0old.txt\0"
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = raw
            changes = git_utils.get_porcelain_status(Path("/tmp"))
            assert len(changes) == 1
            assert changes[0].path == Path("new.txt")
            assert changes[0].original_path == Path("old.txt")
            assert changes[0].is_renamed

    def test_handles_empty_output(self):
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            changes = git_utils.get_porcelain_status(Path("/tmp"))
            assert changes == []

    def test_handles_command_failure(self):
        with patch("subprocess.run") as run:
            run.return_value.returncode = 1
            changes = git_utils.get_porcelain_status(Path("/tmp"))
            assert changes == []


class TestGetChangedFiles:
    def test_returns_absolute_paths(self):
        raw = " M file.py\0"
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = raw
            files = git_utils.get_changed_files(Path("/tmp"), include_untracked=True)
            assert files == [Path("/tmp/file.py")]


class TestGetDomainSplitPlan:
    def test_mocked(self, monkeypatch):
        raw = " M backend/app.py\0"
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = raw
            # The domain classifier needs the file to exist on disk
            monkeypatch.setattr(Path, "exists", lambda self: True)
            plan = git_utils.get_domain_split_plan(Path("/tmp"))
            assert "backend" in plan
            assert plan["backend"] == ["backend/app.py"]


class TestIndexManipulation:
    def test_reset_index(self):
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            assert git_utils.reset_index(Path("/tmp")) is True

    def test_stage_specific_files(self):
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            assert git_utils.stage_specific_files(Path("/tmp"), [Path("/tmp/file.py")]) is True

    def test_get_staged_diff(self):
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "diff content"
            diff = git_utils.get_staged_diff(Path("/tmp"))
            assert diff == "diff content"


class TestBranchAndRemote:
    def test_get_current_branch(self):
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "main\n"
            assert git_utils.get_current_branch(Path("/tmp")) == "main"

    def test_get_tracking_branch(self):
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "origin/main\n"
            assert git_utils.get_tracking_branch(Path("/tmp")) == "origin/main"

    def test_has_remote_origin_true(self):
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "https://github.com/user/repo.git\n"
            assert git_utils.has_remote_origin(Path("/tmp")) is True

    def test_has_commits_true(self):
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "abc123\n"
            assert git_utils.has_commits(Path("/tmp")) is True