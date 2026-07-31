"""Comprehensive unit tests for GitPilot core modules: git_utils, committer, intelligence, executor."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gitpilot.core import git_utils
from gitpilot.core.committer import (
    AICommitter,
    build_commit_prompt,
    clean_commit_message,
)
from gitpilot.core.intelligence import (
    DomainClassifier,
    CommitSplitter,
    OptimizationScanner,
)
from gitpilot.core.executor import GitExecutor


# ===========================================================================
# Git Utils – mocked subprocess
# ===========================================================================
class TestGitUtilsExtended:
    """Full coverage for git_utils using mocked subprocess and Path.exists."""

    @pytest.fixture
    def mock_run(self):
        with patch("subprocess.run") as m:
            m.return_value.returncode = 0
            m.return_value.stdout = ""
            m.return_value.stderr = ""
            yield m

    def test_get_porcelain_status_modified(self, mock_run):
        mock_run.return_value.stdout = " M file.py\0"
        changes = git_utils.get_porcelain_status(Path("/repo"))
        assert len(changes) == 1
        assert changes[0].path == Path("file.py")
        assert changes[0].worktree_status == "M"

    def test_get_porcelain_status_untracked(self, mock_run):
        mock_run.return_value.stdout = "?? new.txt\0"
        changes = git_utils.get_porcelain_status(Path("/repo"))
        assert len(changes) == 1
        assert changes[0].is_untracked

    def test_get_porcelain_status_rename(self, mock_run):
        mock_run.return_value.stdout = "R  new.txt\0old.txt\0"
        changes = git_utils.get_porcelain_status(Path("/repo"))
        assert changes[0].is_renamed
        assert changes[0].original_path == Path("old.txt")

    def test_get_porcelain_status_deleted(self, mock_run):
        mock_run.return_value.stdout = " D deleted.py\0"
        changes = git_utils.get_porcelain_status(Path("/repo"))
        assert changes[0].is_deleted

    def test_get_changed_files_includes_untracked(self, mock_run, monkeypatch):
        mock_run.return_value.stdout = "?? new.py\0"
        monkeypatch.setattr(Path, "exists", lambda self: True)
        files = git_utils.get_changed_files(Path("/repo"), include_untracked=True)
        assert Path("/repo/new.py") in files

    def test_get_changed_files_excludes_untracked(self, mock_run, monkeypatch):
        mock_run.return_value.stdout = "?? new.py\0"
        monkeypatch.setattr(Path, "exists", lambda self: True)
        files = git_utils.get_changed_files(Path("/repo"), include_untracked=False)
        assert files == []

    def test_get_domain_split_plan(self, mock_run, monkeypatch):
        mock_run.return_value.stdout = " M backend/app.py\0"
        # Ensure file "exists" for the filter
        monkeypatch.setattr(Path, "exists", lambda self: True)
        monkeypatch.setattr(DomainClassifier, "classify", lambda *a, **kw: "backend")
        plan = git_utils.get_domain_split_plan(Path("/repo"))
        assert "backend" in plan
        assert plan["backend"] == ["backend/app.py"]

    def test_reset_index(self, mock_run):
        assert git_utils.reset_index(Path("/repo")) is True

    def test_stage_specific_files(self, mock_run):
        assert git_utils.stage_specific_files(Path("/repo"), [Path("/repo/file.py")]) is True

    def test_get_staged_diff(self, mock_run):
        mock_run.return_value.stdout = "diff --git"
        diff = git_utils.get_staged_diff(Path("/repo"))
        assert diff == "diff --git"

    def test_get_current_branch(self, mock_run):
        mock_run.return_value.stdout = "main\n"
        assert git_utils.get_current_branch(Path("/repo")) == "main"

    def test_get_tracking_branch(self, mock_run):
        mock_run.return_value.stdout = "origin/main\n"
        assert git_utils.get_tracking_branch(Path("/repo")) == "origin/main"

    def test_has_remote_origin_true(self, mock_run):
        mock_run.return_value.stdout = "https://github.com/u/r.git\n"
        assert git_utils.has_remote_origin(Path("/repo")) is True

    def test_has_remote_origin_false(self, mock_run):
        mock_run.return_value.returncode = 1
        assert git_utils.has_remote_origin(Path("/repo")) is False

    def test_has_commits_true(self, mock_run):
        mock_run.return_value.stdout = "abc123\n"
        assert git_utils.has_commits(Path("/repo")) is True

    def test_has_commits_false(self, mock_run):
        mock_run.return_value.returncode = 1
        assert git_utils.has_commits(Path("/repo")) is False

    def test_has_unpushed_commits_true(self, mock_run):
        mock_run.return_value.stdout = "abc123\n"
        # get_tracking_branch also mocked
        with patch.object(git_utils, "get_tracking_branch", return_value="origin/main"):
            assert git_utils.has_unpushed_commits(Path("/repo")) is True

    def test_has_unpushed_commits_no_tracking(self, mock_run):
        with patch.object(git_utils, "get_tracking_branch", return_value=None):
            assert git_utils.has_unpushed_commits(Path("/repo")) is False


# ===========================================================================
# Committer – prompt building, message cleaning, and mocked API calls
# ===========================================================================
class TestBuildCommitPrompt:
    def test_basic(self):
        prompt = build_commit_prompt("diff", None)
        assert "conventional" in prompt.lower()

    def test_with_scope_hint(self):
        prompt = build_commit_prompt("diff", None, scope_hint="ui")
        assert "scope 'ui'" in prompt.lower()

    def test_with_branch(self):
        prompt = build_commit_prompt("diff", "feat/auth")
        assert "feat/auth" in prompt


class TestCleanCommitMessage:
    def test_backticks_removed(self):
        assert clean_commit_message("```\nfix: bug\n```") == "fix: bug"

    def test_quotes_stripped(self):
        assert clean_commit_message('"feat: x"') == "feat: x"

    def test_bullet_removed(self):
        assert clean_commit_message("- chore: update") == "chore: update"

    def test_preamble_removed(self):
        assert clean_commit_message("Here is the commit message:\nfeat: new") == "feat: new"


class TestAICommitterAsync:
    """Test the AI committer with mocked HTTP responses."""

    @pytest.fixture
    def committer(self):
        return AICommitter(provider="groq", groq_api_key="gsk_test", groq_model="llama3-8b-8192")

    @pytest.mark.asyncio
    async def test_groq_success(self, committer):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "feat: test groq"}}]
        }
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            msg = await committer._call_groq("diff")
            assert msg == "feat: test groq"

    @pytest.mark.asyncio
    async def test_groq_api_error_returns_none(self, committer):
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.side_effect = httpx.HTTPStatusError("err", request=None, response=None)
            msg = await committer._call_groq("diff")
            assert msg is None

    @pytest.mark.asyncio
    async def test_generate_message_calls_provider(self, committer):
        committer._call_groq = AsyncMock(return_value="feat: x")
        msg = await committer.generate_message("diff", branch="main")
        assert msg == "feat: x"

    @pytest.mark.asyncio
    async def test_generate_message_unknown_provider(self, committer):
        committer.provider = "unknown"
        msg = await committer.generate_message("diff")
        assert msg is None

    @pytest.mark.asyncio
    async def test_qwen_success(self):
        qw = AICommitter(provider="qwen", qwen_api_key="sk-ws-test", qwen_model="qwen-plus")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "feat: qwen"}}]
        }
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            msg = await qw._call_qwen("diff")
            assert msg == "feat: qwen"

    @pytest.mark.asyncio
    async def test_openai_success(self):
        oa = AICommitter(provider="openai", openai_api_key="sk-test")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "feat: openai"}}]
        }
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            msg = await oa._call_openai("diff")
            assert msg == "feat: openai"

    @pytest.mark.asyncio
    async def test_anthropic_success(self):
        ant = AICommitter(provider="anthropic", anthropic_api_key="sk-ant-test")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "content": [{"text": "feat: anthropic"}]
        }
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            msg = await ant._call_anthropic("diff")
            assert msg == "feat: anthropic"

    @pytest.mark.asyncio
    async def test_ollama_success(self):
        ol = AICommitter(provider="ollama", ollama_model="llama3")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"response": "feat: ollama"}
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            msg = await ol._call_ollama("diff")
            assert msg == "feat: ollama"


# ===========================================================================
# Intelligence Engine – full coverage without real files
# ===========================================================================
class TestDomainClassifierEdge:
    def test_test_directory_overrides_backend_py(self):
        c = DomainClassifier()
        assert c.classify(Path("tests/test_auth.py")) == "test"

    def test_backend_services_py(self):
        c = DomainClassifier()
        assert c.classify(Path("services/auth.py")) == "backend"

    def test_ui_jsx(self):
        c = DomainClassifier()
        assert c.classify(Path("components/Button.jsx")) == "ui"

    def test_migrations_sql(self):
        c = DomainClassifier()
        assert c.classify(Path("migrations/001.sql")) == "database"

    def test_user_override_via_map_file(self, tmp_path):
        map_file = tmp_path / "domain_map.json"
        map_file.write_text('{"src/custom.ts": "ui"}')
        c = DomainClassifier(user_map_path=map_file)
        assert c.classify(Path("src/custom.ts")) == "ui"

    def test_unknown_file_is_other(self):
        c = DomainClassifier()
        assert c.classify(Path("somefile.xyz")) == "other"


class TestCommitSplitterEdge:
    def test_disabled_returns_general(self):
        c = DomainClassifier()
        splitter = CommitSplitter(c, enable_splitting=False)
        groups = splitter.split([Path("a.py"), Path("b.jsx")])
        assert "general" in groups
        assert len(groups) == 1

    def test_enabled_splits(self):
        c = DomainClassifier()
        splitter = CommitSplitter(c, enable_splitting=True)
        groups = splitter.split([Path("components/B.jsx"), Path("services/S.py")])
        assert "ui" in groups
        assert "backend" in groups

    def test_other_merges_to_general(self):
        c = DomainClassifier()
        splitter = CommitSplitter(c, enable_splitting=True)
        # Force other by using an unknown extension
        groups = splitter.split([Path("file.xyz")])
        assert "general" in groups


class TestOptimizationScannerEdge:
    def test_detects_n_plus_one(self):
        diff = "+for user in User.objects.all():"
        warnings = OptimizationScanner.scan_diff(diff)
        assert any("N+1" in w or "select_related" in w for w in warnings)

    def test_detects_console_log(self):
        diff = "+console.log('debug')"
        warnings = OptimizationScanner.scan_diff(diff)
        assert any("console.log" in w.lower() for w in warnings)

    def test_ignores_good_lines(self):
        diff = "+return a + b"
        warnings = OptimizationScanner.scan_diff(diff)
        assert len(warnings) == 0


# ===========================================================================
# Git Executor – basic unit tests with mocks
# ===========================================================================
class TestGitExecutorUnit:
    @pytest.fixture
    def executor(self):
        return GitExecutor()

    def test_init_repo_success(self, executor, monkeypatch):
        monkeypatch.setattr(git_utils, "run_git", lambda *a, **kw: MagicMock(returncode=0))
        assert executor.init_repo(Path("/tmp/test")) is True

    def test_get_current_branch_delegates(self, executor, monkeypatch):
        monkeypatch.setattr(git_utils, "get_current_branch", lambda *a: "main")
        assert executor.get_current_branch(Path("/tmp")) == "main"

    def test_stage_all_success(self, executor, monkeypatch):
        monkeypatch.setattr(git_utils, "run_git", lambda *a, **kw: MagicMock(returncode=0))
        assert executor.stage_all(Path("/tmp")) is True

    def test_get_diff_cached_delegates(self, executor, monkeypatch):
        monkeypatch.setattr(git_utils, "get_staged_diff", lambda *a: "diff")
        assert executor.get_diff_cached(Path("/tmp")) == "diff"

    def test_set_remote_origin(self, executor, monkeypatch):
        monkeypatch.setattr(git_utils, "run_git", lambda *a, **kw: MagicMock(returncode=0))
        assert executor.set_remote_origin(Path("/tmp"), "https://example.com/repo.git") is True

    @pytest.mark.asyncio
    async def test_push_with_retry_no_remote(self, executor, monkeypatch):
        # Simulate push failure
        monkeypatch.setattr(executor, "_try_push", AsyncMock(return_value=(False, "error")))
        success, err = await executor.push_with_retry(Path("/tmp"))
        assert success is False
        assert err == "error"