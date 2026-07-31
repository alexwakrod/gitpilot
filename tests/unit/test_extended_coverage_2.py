"""Additional extensive unit tests to push coverage above 85%."""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import httpx
import pytest

from gitpilot.core.committer import AICommitter, build_commit_prompt, clean_commit_message
from gitpilot.core.executor import GitExecutor
from gitpilot.core.intelligence import DomainClassifier, CommitSplitter, OptimizationScanner
from gitpilot.core.project_setup import is_git_repo, ensure_initial_commit, create_github_repo, setup_project
from gitpilot.core.watcher import ChangeAccumulator, FileHashCache
from gitpilot.cli.main import _test_grok_api_key, _test_groq_api_key, _test_qwen_api_key, _test_openai_api_key, _test_anthropic_api_key, _test_ollama_connection


# ===========================================================================
# Committer – full coverage of error branches
# ===========================================================================
class TestAICommitterErrorBranches:
    """Cover missing lines in committer: missing API key, unknown provider, generate_message exception."""

    @pytest.mark.asyncio
    async def test_call_grok_no_key(self):
        c = AICommitter(provider="grok", grok_api_key=None)
        msg = await c._call_grok("diff")
        assert msg is None

    @pytest.mark.asyncio
    async def test_call_groq_no_key(self):
        c = AICommitter(provider="groq", groq_api_key=None)
        msg = await c._call_groq("diff")
        assert msg is None

    @pytest.mark.asyncio
    async def test_call_qwen_no_key(self):
        c = AICommitter(provider="qwen", qwen_api_key=None)
        msg = await c._call_qwen("diff")
        assert msg is None

    @pytest.mark.asyncio
    async def test_call_openai_no_key(self):
        c = AICommitter(provider="openai", openai_api_key=None)
        msg = await c._call_openai("diff")
        assert msg is None

    @pytest.mark.asyncio
    async def test_call_anthropic_no_key(self):
        c = AICommitter(provider="anthropic", anthropic_api_key=None)
        msg = await c._call_anthropic("diff")
        assert msg is None

    @pytest.mark.asyncio
    async def test_generate_message_unknown_provider(self):
        c = AICommitter(provider="unknown")
        msg = await c.generate_message("diff")
        assert msg is None

    @pytest.mark.asyncio
    async def test_generate_message_catches_exception_from_call_grok(self, monkeypatch):
        c = AICommitter(provider="grok", grok_api_key="xai-test")
        # Patch _call_grok to raise an exception
        monkeypatch.setattr(c, "_call_grok", AsyncMock(side_effect=httpx.ConnectError("timeout")))
        msg = await c.generate_message("diff")
        assert msg is None

    @pytest.mark.asyncio
    async def test_generate_message_catches_exception_from_call_groq(self, monkeypatch):
        c = AICommitter(provider="groq", groq_api_key="gsk_test")
        monkeypatch.setattr(c, "_call_groq", AsyncMock(side_effect=httpx.ConnectError("timeout")))
        msg = await c.generate_message("diff")
        assert msg is None

    @pytest.mark.asyncio
    async def test_generate_message_catches_exception_from_call_qwen(self, monkeypatch):
        c = AICommitter(provider="qwen", qwen_api_key="sk-ws-test")
        monkeypatch.setattr(c, "_call_qwen", AsyncMock(side_effect=httpx.ConnectError("timeout")))
        msg = await c.generate_message("diff")
        assert msg is None

    @pytest.mark.asyncio
    async def test_generate_message_catches_exception_from_call_openai(self, monkeypatch):
        c = AICommitter(provider="openai", openai_api_key="sk-test")
        monkeypatch.setattr(c, "_call_openai", AsyncMock(side_effect=httpx.ConnectError("timeout")))
        msg = await c.generate_message("diff")
        assert msg is None

    @pytest.mark.asyncio
    async def test_generate_message_catches_exception_from_call_anthropic(self, monkeypatch):
        c = AICommitter(provider="anthropic", anthropic_api_key="sk-ant-test")
        monkeypatch.setattr(c, "_call_anthropic", AsyncMock(side_effect=httpx.ConnectError("timeout")))
        msg = await c.generate_message("diff")
        assert msg is None

    @pytest.mark.asyncio
    async def test_generate_message_catches_exception_from_call_ollama(self, monkeypatch):
        c = AICommitter(provider="ollama", ollama_model="llama3")
        monkeypatch.setattr(c, "_call_ollama", AsyncMock(side_effect=httpx.ConnectError("timeout")))
        msg = await c.generate_message("diff")
        assert msg is None


# ===========================================================================
# Executor – additional edge cases
# ===========================================================================
class TestGitExecutorEdgeCases:
    @pytest.fixture
    def executor(self):
        return GitExecutor()

    def test_stage_files_empty_list(self, executor):
        assert executor.stage_files(Path("/tmp"), []) is True

    def test_commit_failure_subprocess(self, executor, monkeypatch):
        # Simulate git commit failure
        mock = MagicMock()
        mock.returncode = 1
        mock.stdout = ""
        mock.stderr = "error"
        monkeypatch.setattr("gitpilot.core.executor.git_utils.run_git", lambda *a, **kw: mock)
        # ensure GITPYTHON_AVAILABLE is False to hit subprocess path
        monkeypatch.setattr("gitpilot.core.executor.GITPYTHON_AVAILABLE", False)
        commit_hash = executor.commit(Path("/tmp"), "test")
        assert commit_hash is None

    def test_init_repo_failure(self, executor, monkeypatch):
        mock = MagicMock()
        mock.returncode = 1
        monkeypatch.setattr("gitpilot.core.executor.git_utils.run_git", lambda *a, **kw: mock)
        assert executor.init_repo(Path("/tmp")) is False

    @pytest.mark.asyncio
    async def test_push_with_retry_success(self, executor, monkeypatch):
        # Mock _try_push to return success on second attempt
        executor._try_push = AsyncMock(side_effect=[(False, "err1"), (True, None)])
        success, err = await executor.push_with_retry(Path("/tmp"))
        assert success is True
        assert err is None

    @pytest.mark.asyncio
    async def test_push_with_retry_all_fail(self, executor):
        executor._try_push = AsyncMock(return_value=(False, "error"))
        success, err = await executor.push_with_retry(Path("/tmp"))
        assert success is False
        assert err == "error"

    def test_set_remote_origin_failure(self, executor, monkeypatch):
        mock = MagicMock()
        mock.returncode = 1
        mock.stderr = "fail"
        monkeypatch.setattr("gitpilot.core.executor.git_utils.run_git", lambda *a, **kw: mock)
        assert executor.set_remote_origin(Path("/tmp"), "url") is False

    def test_embed_token_in_url_failure_get_url(self, executor, monkeypatch):
        mock = MagicMock()
        mock.returncode = 1  # remote get-url fails
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock)
        assert executor._embed_token_in_url(Path("/tmp"), "token") is False

    def test_get_current_branch_gitpython_fallback(self, executor, monkeypatch):
        # Force GITPYTHON_AVAILABLE False, subprocess returns branch
        monkeypatch.setattr("gitpilot.core.executor.GITPYTHON_AVAILABLE", False)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "feature/abc\n"
            branch = executor.get_current_branch(Path("/tmp"))
            assert branch == "feature/abc"


# ===========================================================================
# Intelligence – additional classifier coverage
# ===========================================================================
class TestDomainClassifierAdditional:
    def test_ast_detects_test_file_by_stem(self, tmp_path):
        test_file = tmp_path / "test_something.py"
        test_file.write_text("def test(): pass")
        c = DomainClassifier()
        assert c.classify(test_file) == "test"

    def test_ast_detects_spec_file(self, tmp_path):
        spec_file = tmp_path / "user_spec.py"
        spec_file.write_text("def spec(): pass")
        c = DomainClassifier()
        assert c.classify(spec_file) == "test"

    def test_ast_detects_django_model_via_from_import(self, tmp_path):
        py = tmp_path / "models.py"
        py.write_text("from django.db import models\nclass User(models.Model):\n    pass")
        c = DomainClassifier()
        assert c.classify(py) == "database"

    def test_ast_no_model_class_still_backend(self, tmp_path):
        py = tmp_path / "views.py"
        py.write_text("from django.db import connection")
        c = DomainClassifier()
        assert c.classify(py) == "backend"

    def test_ast_ui_imports(self, tmp_path):
        py = tmp_path / "gui.py"
        py.write_text("import tkinter")
        c = DomainClassifier()
        assert c.classify(py) == "ui"

    def test_classify_returns_other_for_unmatched(self):
        c = DomainClassifier()
        assert c.classify(Path("random.xyz")) == "other"


class TestCommitSplitterPlan:
    def test_commit_plan_general_domain(self):
        c = DomainClassifier()
        splitter = CommitSplitter(c, enable_splitting=True)
        plan = splitter.commit_plan([Path("random.xyz")])
        assert plan[0]["domain"] == "general"
        assert plan[0]["suggested_scope"] == "misc"

    def test_commit_plan_ui(self):
        c = DomainClassifier()
        splitter = CommitSplitter(c)
        plan = splitter.commit_plan([Path("components/Button.jsx")])
        assert plan[0]["suggested_scope"] == "ui"


# ===========================================================================
# Project setup – full coverage
# ===========================================================================
class TestProjectSetup:
    def test_is_git_repo_true(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        assert is_git_repo(repo) is True

    def test_is_git_repo_false(self, tmp_path):
        assert is_git_repo(tmp_path / "nonexistent") is False

    def test_ensure_initial_commit_empty_repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        # No files, should create empty commit
        assert ensure_initial_commit(repo) is True
        # Verify commit exists
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True)
        assert result.returncode == 0

    def test_ensure_initial_commit_with_files(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        (repo / "file.txt").write_text("hi")
        assert ensure_initial_commit(repo) is True

    def test_create_github_repo_success(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"clone_url": "https://github.com/test/repo.git"}
        with patch("httpx.post", return_value=mock_resp):
            url = create_github_repo("test-repo", private=True, github_token="fake")
            assert url == "https://github.com/test/repo.git"

    def test_create_github_repo_failure(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.status_code = 422
        mock_resp.text = "error"
        with patch("httpx.post", return_value=mock_resp):
            url = create_github_repo("test-repo", token="fake")
            assert url is None

    def test_setup_project_existing(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, capture_output=True)
        subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=repo, capture_output=True)
        ok, err = setup_project(repo)
        assert ok is True
        assert err is None

    def test_setup_project_not_exist(self, tmp_path):
        ok, err = setup_project(tmp_path / "ghost")
        assert ok is False
        assert "not exist" in err.lower()


# ===========================================================================
# Watcher components (ChangeAccumulator, FileHashCache)
# ===========================================================================
class TestWatcherComponents:
    def test_change_accumulator_add_reset(self):
        acc = ChangeAccumulator()
        acc.add(Path("a.txt"))
        acc.add(Path("b.txt"))
        assert acc.size == 2
        changes = acc.reset()
        assert len(changes) == 2
        assert acc.size == 0

    def test_file_hash_cache_ttl(self):
        cache = FileHashCache(ttl=1)
        cache.set_hash("a", "hash")
        assert cache.get_hash("a") == "hash"
        # Simulate TTL expiry by directly manipulating the stored timestamp
        # We'll just test the expiry by patching time.time
        import time
        with patch("time.time", return_value=time.time() + 2):
            assert cache.get_hash("a") is None

    def test_file_hash_cache_invalidate(self):
        cache = FileHashCache()
        cache.set_hash("a", "hash")
        cache.invalidate("a")
        assert cache.get_hash("a") is None


# ===========================================================================
# API key live test functions (coverage for async testers)
# ===========================================================================
class TestAPILiveTesters:
    @pytest.mark.asyncio
    async def test_test_grok_key_success(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            assert await _test_grok_api_key("xai-test", "grok-2") is True

    @pytest.mark.asyncio
    async def test_test_grok_key_failure(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            assert await _test_grok_api_key("xai-test", "grok-2") is False

    @pytest.mark.asyncio
    async def test_test_groq_key_success(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            assert await _test_groq_api_key("gsk_test", "llama3") is True

    @pytest.mark.asyncio
    async def test_test_qwen_key_success(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            assert await _test_qwen_api_key("sk-ws-test", "qwen-plus") is True

    @pytest.mark.asyncio
    async def test_test_openai_key_success(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            assert await _test_openai_api_key("sk-test", "gpt-4o") is True

    @pytest.mark.asyncio
    async def test_test_anthropic_key_success(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            assert await _test_anthropic_api_key("sk-ant-test", "claude") is True

    @pytest.mark.asyncio
    async def test_test_ollama_connection_success(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            assert await _test_ollama_connection("http://localhost:11434", "llama3") is True

    @pytest.mark.asyncio
    async def test_test_ollama_connection_failure(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            assert await _test_ollama_connection("http://localhost:11434", "llama3") is False