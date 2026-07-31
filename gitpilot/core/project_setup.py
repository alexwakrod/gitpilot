"""Intelligent project setup: git init, remote configuration, initial commit."""

import logging
import subprocess
from pathlib import Path
from typing import Optional, Tuple

import httpx

from gitpilot.core.executor import GitExecutor

logger = logging.getLogger(__name__)


def is_git_repo(path: Path) -> bool:
    """Return True if the directory is a Git repository."""
    return (path / ".git").exists() and (path / ".git").is_dir()


def has_remote_origin(path: Path) -> bool:
    """Return True if the repository has a remote named 'origin'."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


def has_commits(path: Path) -> bool:
    """Return True if the repository has at least one commit."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def ensure_initial_commit(path: Path) -> bool:
    """If the repository has no commits, create an empty initial commit."""
    if not is_git_repo(path):
        return False
    if has_commits(path):
        return True

    try:
        # Check if there are any files to commit
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if not result.stdout.strip():
            # No files, create an empty commit
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "Initial commit"],
                cwd=str(path),
                check=True,
                capture_output=True,
                timeout=15,
            )
        else:
            # Stage everything and commit
            subprocess.run(
                ["git", "add", "-A"],
                cwd=str(path),
                check=True,
                capture_output=True,
                timeout=15,
            )
            subprocess.run(
                ["git", "commit", "-m", "Initial commit"],
                cwd=str(path),
                check=True,
                capture_output=True,
                timeout=15,
            )
        logger.info("Initial commit created in %s", path)
        return True
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to create initial commit in %s: %s", path, exc.stderr)
        return False


def create_github_repo(
    name: str,
    private: bool = True,
    github_token: Optional[str] = None,
    description: str = "",
) -> Optional[str]:
    """Create a GitHub repository and return the clone URL, or None if failed."""
    if not github_token:
        logger.warning("GitHub token not provided; skipping remote creation")
        return None
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {
        "name": name,
        "private": private,
        "auto_init": False,
        "description": description,
    }
    try:
        resp = httpx.post(
            "https://api.github.com/user/repos",
            headers=headers,
            json=payload,
            timeout=15.0,
        )
        if resp.status_code == 201:
            data = resp.json()
            clone_url = data.get("clone_url")
            if clone_url:
                return clone_url
            else:
                logger.error("GitHub repository created but no clone_url in response")
        else:
            logger.warning(
                "GitHub repo creation failed: %d %s",
                resp.status_code,
                resp.text[:500],
            )
    except httpx.HTTPError as exc:
        logger.error("GitHub API request failed: %s", exc)
    return None


def setup_project(
    path: Path,
    name: Optional[str] = None,
    github_token: Optional[str] = None,
    create_remote: bool = False,
    private: bool = True,
    auto_init_commit: bool = True,
) -> Tuple[bool, Optional[str]]:
    """
    Setup a project directory for GitPilot monitoring.

    Returns (success, error_message).
    """
    if not path.exists():
        return False, "Directory does not exist"
    if not path.is_dir():
        return False, "Path is not a directory"

    executor = GitExecutor()

    # 1. Initialize git if needed
    if not is_git_repo(path):
        if not executor.init_repo(path):
            return False, "Failed to initialize git repository"

    # 2. Ensure initial commit
    if auto_init_commit:
        if not ensure_initial_commit(path):
            return False, "Failed to create initial commit"

    # 3. Set up GitHub remote if requested
    if create_remote and github_token:
        if has_remote_origin(path):
            logger.info("Remote origin already exists; skipping remote creation")
        else:
            repo_name = name or path.name
            clone_url = create_github_repo(
                name=repo_name,
                private=private,
                github_token=github_token,
                description=f"Auto-created by GitPilot for {repo_name}",
            )
            if clone_url:
                if not executor.set_remote_origin(path, clone_url):
                    return True, "GitHub repo created but failed to set remote origin"
            else:
                return True, "Failed to create GitHub repository; project added locally"

    return True, None