"""Git command execution with native Git porcelain, token embedding, and retry logic."""

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Optional, Tuple, List

from gitpilot.core import git_utils

try:
    import git
    GITPYTHON_AVAILABLE = True
except ImportError:
    GITPYTHON_AVAILABLE = False

logger = logging.getLogger(__name__)


class GitExecutor:
    """Handles git operations using native porcelain whenever possible."""

    def __init__(self, max_retries: int = 3, github_token: Optional[str] = None):
        self.max_retries = max_retries
        self.github_token = github_token

    # ------------------------------------------------------------------
    # Branch detection – delegate to git_utils
    # ------------------------------------------------------------------
    def get_current_branch(self, repo_path: Path) -> Optional[str]:
        return git_utils.get_current_branch(repo_path)

    def branch_has_upstream(self, repo_path: Path, branch: str) -> bool:
        return git_utils.get_tracking_branch(repo_path) is not None

    # ------------------------------------------------------------------
    # Staging – delegate to git_utils
    # ------------------------------------------------------------------
    def stage_all(self, repo_path: Path) -> bool:
        """Run `git add --all` (not porcelain, but universal)."""
        try:
            if GITPYTHON_AVAILABLE:
                repo = git.Repo(str(repo_path))
                repo.git.add(all=True)
                return True
        except Exception as exc:
            logger.debug("gitpython stage_all failed: %s", exc)
        result = git_utils.run_git(["git", "add", "--all"], repo_path)
        return result.returncode == 0

    def stage_files(self, repo_path: Path, files: List[Path]) -> bool:
        """Stage only the given files (relative paths)."""
        if not files:
            return True
        rel_paths = []
        for f in files:
            try:
                rel = str(f.relative_to(repo_path))
            except ValueError:
                rel = str(f)
            rel_paths.append(rel)
        return git_utils.stage_specific_files(repo_path, files)

    # ------------------------------------------------------------------
    # Diff – delegate to git_utils
    # ------------------------------------------------------------------
    def get_diff_cached(self, repo_path: Path) -> Optional[str]:
        return git_utils.get_staged_diff(repo_path)

    # ------------------------------------------------------------------
    # Commit – subprocess (gitpython fallback)
    # ------------------------------------------------------------------
    def commit(self, repo_path: Path, message: str) -> Optional[str]:
        try:
            if GITPYTHON_AVAILABLE:
                repo = git.Repo(str(repo_path))
                commit = repo.index.commit(message)
                return commit.hexsha
        except Exception as exc:
            logger.debug("gitpython commit failed: %s", exc)

        result = git_utils.run_git(["git", "commit", "-m", message], repo_path)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("[") and "]" in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        return parts[1].rstrip("]")
            hash_result = git_utils.run_git(["git", "rev-parse", "HEAD"], repo_path)
            if hash_result.returncode == 0:
                return hash_result.stdout.strip()
        else:
            logger.error("git commit failed: %s", result.stderr.strip())
        return None

    # ------------------------------------------------------------------
    # Push – token embedding and auto‑upstream
    # ------------------------------------------------------------------
    def _embed_token_in_url(self, repo_path: Path, token: str) -> bool:
        try:
            result = git_utils.run_git(["git", "remote", "get-url", "origin"], repo_path)
            if result.returncode != 0:
                return False
            old_url = result.stdout.strip()
            if "@" in old_url:
                return True
            new_url = old_url.replace("https://", f"https://{token}@")
            subprocess.run(
                ["git", "remote", "set-url", "origin", new_url],
                cwd=str(repo_path), check=True, timeout=5,
            )
            logger.info("Embedded GitHub token in remote origin URL")
            return True
        except Exception as exc:
            logger.error("Failed to embed token in remote URL: %s", exc)
            return False

    async def push_with_retry(self, repo_path: Path) -> Tuple[bool, Optional[str]]:
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
        branch = self.get_current_branch(repo_path)
        if not branch:
            return False, "Could not detect current branch"

        if self.github_token:
            self._embed_token_in_url(repo_path, self.github_token)

        has_upstream = self.branch_has_upstream(repo_path, branch)

        try:
            if GITPYTHON_AVAILABLE and has_upstream:
                repo = git.Repo(str(repo_path))
                remote = repo.remote(name="origin")
                remote.push(refspec=f"{branch}:{branch}")
                return True, None
        except Exception as exc:
            logger.debug("gitpython push failed: %s", exc)

        if has_upstream:
            cmd = ["git", "push", "origin", branch]
        else:
            cmd = ["git", "push", "--set-upstream", "origin", branch]
            logger.info("Setting upstream for branch '%s'", branch)

        result = git_utils.run_git(cmd, repo_path)
        if result.returncode == 0:
            return True, None
        error_msg = result.stderr.strip() or result.stdout.strip()
        return False, error_msg

    # ------------------------------------------------------------------
    # Repository setup
    # ------------------------------------------------------------------
    def init_repo(self, directory: Path) -> bool:
        result = git_utils.run_git(["git", "init"], directory)
        return result.returncode == 0

    def set_remote_origin(self, repo_path: Path, remote_url: str) -> bool:
        # Remove existing origin if present
        git_utils.run_git(["git", "remote", "remove", "origin"], repo_path)
        result = git_utils.run_git(["git", "remote", "add", "origin", remote_url], repo_path)
        if result.returncode == 0:
            return True
        logger.error("git remote add failed: %s", result.stderr.strip())
        return False