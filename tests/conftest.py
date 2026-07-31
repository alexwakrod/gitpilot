"""Shared fixtures for GitPilot tests – SQLite backend, temp git repos, config."""

import json
import os
import sqlite3
import subprocess
import tempfile
import shutil
from pathlib import Path

import pytest


@pytest.fixture
def temp_gitpilot_dir(monkeypatch):
    """Create a temporary GitPilot configuration directory and mock get_gitpilot_dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(
            "gitpilot.domain.settings.get_gitpilot_dir",
            lambda: Path(tmpdir),
        )
        monkeypatch.setattr(
            "gitpilot.domain.policies.get_gitpilot_dir",
            lambda: Path(tmpdir),
        )
        yield Path(tmpdir)


@pytest.fixture
def temp_config(temp_gitpilot_dir):
    """Create a temporary config.json with default values (including new defaults)."""
    config_path = temp_gitpilot_dir / "config.json"
    default_config = {
        "ai_provider": "grok",
        "ai_model": "grok-2",
        "ai_temperature": 0.5,
        "debounce_interval": 120,
        "smart_grouping": True,
        "branch_aware_messages": True,
        "max_commit_retries": 3,
        "discord_webhook_enabled": False,
        "theme": "dark",
    }
    config_path.write_text(json.dumps(default_config))
    return config_path


@pytest.fixture
def temp_git_repo():
    """Create a temporary directory initialized as a git repository with one commit."""
    repo_dir = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@gitpilot.local"],
        cwd=repo_dir, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_dir, check=True, capture_output=True,
    )
    (Path(repo_dir) / "README.md").write_text("# Test Repo")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True)
    yield Path(repo_dir)
    shutil.rmtree(repo_dir, ignore_errors=True)


@pytest.fixture
def temp_git_repo_with_files(temp_git_repo):
    """Temporary git repo with several files across different domains."""
    # Backend file
    (temp_git_repo / "app.py").write_text("def main(): pass")
    # UI file
    (temp_git_repo / "components").mkdir(exist_ok=True)
    (temp_git_repo / "components" / "Button.jsx").write_text("export default Button;")
    # Test file
    (temp_git_repo / "tests").mkdir(exist_ok=True)
    (temp_git_repo / "tests" / "test_app.py").write_text("def test(): assert True")
    # Config file
    (temp_git_repo / ".env.example").write_text("SECRET=xxx")
    # Stage and commit
    subprocess.run(["git", "add", "-A"], cwd=temp_git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add files for domain testing"],
        cwd=temp_git_repo, check=True, capture_output=True,
    )
    return temp_git_repo


@pytest.fixture
def memory_db():
    """Create an in-memory SQLite database with full GitPilot schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    from gitpilot.infrastructure.db import run_migrations
    run_migrations(conn)
    yield conn
    conn.close()