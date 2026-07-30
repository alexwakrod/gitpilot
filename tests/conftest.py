"""Shared fixtures for GitPilot tests."""

import json
import os
import tempfile
import shutil
from pathlib import Path

import pytest
import duckdb


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
    """Create a temporary config.json with default values."""
    config_path = temp_gitpilot_dir / "config.json"
    default_config = {
        "ai_provider": "grok",
        "ai_model": "grok-2",
        "ai_temperature": 0.5,
        "debounce_interval": 3,
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
    """Create a temporary directory initialized as a git repository."""
    repo_dir = tempfile.mkdtemp()
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@gitpilot.local"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    # Create an initial commit to have a HEAD
    (Path(repo_dir) / "README.md").write_text("# Test Repo")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, check=True, capture_output=True)
    yield Path(repo_dir)
    shutil.rmtree(repo_dir, ignore_errors=True)


@pytest.fixture
def memory_db():
    """Create an in-memory DuckDB database with schema applied."""
    conn = duckdb.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON;")
    from gitpilot.infrastructure.db import run_migrations
    run_migrations(conn)
    yield conn
    conn.close()