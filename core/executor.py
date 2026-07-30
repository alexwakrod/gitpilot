"""Git command execution with retry logic."""

import asyncio
import logging
import shlex
import subprocess
from pathlib import Path
from typing import Optional, Tuple

try:
    import git
    GITPYTHON_AVAILABLE = True
except ImportError:
    GITPYTHON_AVAILABLE = False

logger = logging.getLogger(__name__)


class GitExecutor:
    """Handles git operations: add, commit, push, branch detection."""

    def __init__(self, max_retries: int = 3, github_token: Optional[str] = None):
        self.max_retries = max_retries
        self.github_token = github_token

    def get_current_branch(self, repo_path: Path) -> Optional[str]:
        """Get the current branch name for the given repository."""
        try:
            if GITPYTHON_AVAILABLE:
                repo = git.Repo(str(repo_path))
                return repo.active_branch.name
        except Exception as exc:
            logger.debug("gitpython failed to get branch, falling back to subprocess: %s", exc)

        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            else:
                logger.warning("Failed to detect branch: %s", result.stderr.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.error("Git command error while detecting branch: %s", exc)
        return None

    def stage_all(self, repo_path: Path) -> bool:
        """Run `git add --all` in the repository directory."""
        try:
            if GITPYTHON_AVAILABLE:
                repo = git.Repo(str(repo_path))
                repo.git.add(all=True)
                return True
        except Exception as exc:
            logger.debug("gitpython stage failed: %s", exc)

        try:
            result = subprocess.run(
                ["git", "add", "--all"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                return True
            logger.error("git add failed: %s", result.stderr.strip())
            return False
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.error("git add execution error: %s", exc)
            return False

    def get_diff_cached(self, repo_path: Path) -> Optional[str]:
        """Get the staged diff via `git diff --cached`."""
        try:
            if GITPYTHON_AVAILABLE:
                repo = git.Repo(str(repo_path))
                diff = repo.git.diff("--cached")
                return diff
        except Exception as exc:
            logger.debug("gitpython diff failed: %s", exc)

        try:
            result = subprocess.run(
                ["git", "diff", "--cached"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                return result.stdout
            logger.error("git diff failed: %s", result.stderr.strip())
            return None
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.error("git diff execution error: %s", exc)
            return None

    def commit(self, repo_path: Path, message: str) -> Optional[str]:
        """Create a commit with the given message. Returns the commit hash or None."""
        try:
            if GITPYTHON_AVAILABLE:
                repo = git.Repo(str(repo_path))
                commit = repo.index.commit(message)
                return commit.hexsha
        except Exception as exc:
            logger.debug("gitpython commit failed: %s", exc)

        try:
            result = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                # Extract hash from output
                for line in result.stdout.splitlines():
                    if line.startswith("[") and "]" in line:
                        # e.g., [main a1b2c3d] message
                        parts = line.split()
                        if len(parts) >= 2:
                            return parts[1].rstrip("]")
                # Fallback: read from rev-parse
                hash_result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if hash_result.returncode == 0:
                    return hash_result.stdout.strip()
                return None
            logger.error("git commit failed: %s", result.stderr.strip())
            return None
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.error("git commit execution error: %s", exc)
            return None

    async def push_with_retry(self, repo_path: Path) -> Tuple[bool, Optional[str]]:
        """Push commits to origin with exponential backoff.
        Returns (success, error_message).
        """
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            success, error = await self._try_push(repo_path)
            if success:
                logger.info("Push succeeded on attempt %d", attempt)
                return True, None
            last_error = error
            logger.warning("Push attempt %d failed: %s", attempt, error)
            if attempt < self.max_retries:
                delay = 2 ** (attempt - 1)
                await asyncio.sleep(delay)
        return False, last_error

    async def _try_push(self, repo_path: Path) -> Tuple[bool, Optional[str]]:
        """Perform a single push attempt."""
        env = None
        if self.github_token:
            env = {"GIT_ASKPASS": "echo", "GIT_USERNAME": "gitpilot"}
        try:
            if GITPYTHON_AVAILABLE:
                repo = git.Repo(str(repo_path))
                remote = repo.remote(name="origin")
                push_kwargs = {}
                if self.github_token:
                    push_kwargs["env"] = {
                        "GIT_ASKPASS": "echo",
                        "GIT_USERNAME": "gitpilot",
                    }
                remote.push(
                    refspec=f"{repo.active_branch.name}:{repo.active_branch.name}",
                    **push_kwargs,
                )
                return True, None
        except Exception as exc:
            logger.debug("gitpython push failed: %s", exc)

        cmd = ["git", "push", "origin", "HEAD"]
        try:
            result = subprocess.run(
                cmd,
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            if result.returncode == 0:
                return True, None
            error_msg = result.stderr.strip() or result.stdout.strip()
            return False, error_msg
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return False, str(exc)

    def init_repo(self, directory: Path) -> bool:
        """Initialize a new Git repository in the given directory."""
        try:
            if GITPYTHON_AVAILABLE:
                git.Repo.init(str(directory))
                return True
        except Exception as exc:
            logger.debug("gitpython init failed: %s", exc)

        try:
            result = subprocess.run(
                ["git", "init"],
                cwd=str(directory),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return True
            logger.error("git init failed: %s", result.stderr.strip())
            return False
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.error("git init error: %s", exc)
            return False

    def set_remote_origin(self, repo_path: Path, remote_url: str) -> bool:
        """Set or update the origin remote."""
        try:
            if GITPYTHON_AVAILABLE:
                repo = git.Repo(str(repo_path))
                if "origin" in [r.name for r in repo.remotes]:
                    repo.delete_remote("origin")
                repo.create_remote("origin", remote_url)
                return True
        except Exception as exc:
            logger.debug("gitpython set remote failed: %s", exc)

        try:
            # Remove existing origin if present
            subprocess.run(
                ["git", "remote", "remove", "origin"],
                cwd=str(repo_path),
                capture_output=True,
                timeout=10,
            )
            result = subprocess.run(
                ["git", "remote", "add", "origin", remote_url],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return True
            logger.error("git remote add failed: %s", result.stderr.strip())
            return False
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.error("git remote add error: %s", exc)
            return False