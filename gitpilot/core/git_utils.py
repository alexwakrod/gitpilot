"""
Native Git utilities – parse `git status --porcelain -z` output, list all changes,
detect untracked files, renames, copies, and staged/unstaged changes exactly as Git
sees them.  Every GitPilot component that touches Git should use these helpers.
"""

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures for Git change records
# ---------------------------------------------------------------------------
@dataclass
class GitChange:
    """A single changed file as reported by `git status --porcelain -z`."""
    path: Path            # relative to repository root
    index_status: str     # ' ', 'M', 'A', 'D', 'R', 'C', 'T', 'U', 'X', '?'
    worktree_status: str  # same as above, ' ' if not modified in worktree
    original_path: Optional[Path] = None   # set for renames/copies

    @property
    def is_staged(self) -> bool:
        return self.index_status not in (' ', '?')

    @property
    def is_unstaged(self) -> bool:
        return self.worktree_status not in (' ', '?')

    @property
    def is_untracked(self) -> bool:
        return self.index_status == '?' and self.worktree_status == '?'

    @property
    def is_renamed(self) -> bool:
        return self.index_status == 'R' or self.worktree_status == 'R'

    @property
    def is_deleted(self) -> bool:
        return self.index_status == 'D' or self.worktree_status == 'D'

    @property
    def file_path(self) -> Path:
        """Return the current path (for renames, the new name)."""
        return self.path


def run_git(cmd: List[str], cwd: Path) -> subprocess.CompletedProcess:
    """Execute a Git command and return the CompletedProcess, raising on error."""
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        logger.warning("Git command failed: %s\n%s", cmd, result.stderr.strip())
    return result


# ---------------------------------------------------------------------------
# Status parsing (the core of native Git integration)
# ---------------------------------------------------------------------------
def get_porcelain_status(repo_root: Path) -> List[GitChange]:
    """
    Return every change Git knows about in the repository.
    Uses `git status --porcelain -z` (null‑separated, machine‑readable).
    Handles renames, copies, untracked directories.
    """
    cmd = ["git", "status", "--porcelain", "-z"]
    result = run_git(cmd, repo_root)
    if result.returncode != 0:
        return []

    raw = result.stdout.strip("\0")
    if not raw:
        return []

    changes: List[GitChange] = []
    tokens = raw.split("\0")
    # Tokens are entry pairs: XY<space>path\0[optional original path\0]
    # For renames: XY<space>new\0original\0
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if len(token) < 3:
            i += 1
            continue
        index_status = token[0]
        worktree_status = token[1]
        path_part = token[3:]  # after XY and space

        if index_status == 'R' or index_status == 'C' or worktree_status == 'R' or worktree_status == 'C':
            # Rename or copy: next token is the original path
            new_path = Path(path_part)
            original_path = None
            if i + 1 < len(tokens):
                original_path = Path(tokens[i + 1])
                i += 1
            changes.append(GitChange(
                path=new_path,
                index_status=index_status,
                worktree_status=worktree_status,
                original_path=original_path,
            ))
        else:
            changes.append(GitChange(
                path=Path(path_part),
                index_status=index_status,
                worktree_status=worktree_status,
            ))
        i += 1

    return changes


def get_changed_files(repo_root: Path, include_untracked: bool = True) -> List[Path]:
    """
    Convenience: return absolute paths of every file with any change.
    Includes staged, unstaged, and optionally untracked files.
    """
    changes = get_porcelain_status(repo_root)
    files = []
    for c in changes:
        if not include_untracked and c.is_untracked:
            continue
        # For renames, use new path; but we may want both? We'll use new path.
        abs_path = repo_root / c.file_path
        files.append(abs_path)
    return sorted(set(files))


def get_domain_split_plan(repo_root: Path) -> Dict[str, List[str]]:
    """
    Return the domain classification of all changed files using native Git status.
    Returns {domain: [relative_file_path, ...]}
    """
    from gitpilot.core.intelligence import DomainClassifier, CommitSplitter
    changes = get_porcelain_status(repo_root)
    file_paths = [repo_root / c.file_path for c in changes if (repo_root / c.file_path).exists()]
    if not file_paths:
        return {}

    classifier = DomainClassifier()
    splitter = CommitSplitter(classifier, enable_splitting=True)
    plan = splitter.split(file_paths, project_root=repo_root)
    # Convert absolute paths back to relative for display
    result = {}
    for domain, paths in plan.items():
        result[domain] = [str(p.relative_to(repo_root)) for p in paths]
    return result


# ---------------------------------------------------------------------------
# Index manipulation (for domain‑aware staging)
# ---------------------------------------------------------------------------
def reset_index(repo_root: Path) -> bool:
    """Unstage all files (git reset). Returns True on success."""
    result = run_git(["git", "reset"], repo_root)
    return result.returncode == 0


def stage_specific_files(repo_root: Path, files: List[Path]) -> bool:
    """Stage only the given files, using relative paths from repo_root."""
    if not files:
        return True
    rel_paths = []
    for f in files:
        try:
            rel = str(f.relative_to(repo_root))
        except ValueError:
            rel = str(f)
        rel_paths.append(rel)
    cmd = ["git", "add", "--"] + rel_paths
    result = run_git(cmd, repo_root)
    return result.returncode == 0


def get_staged_diff(repo_root: Path) -> Optional[str]:
    """Return `git diff --cached` output."""
    result = run_git(["git", "diff", "--cached"], repo_root)
    if result.returncode == 0:
        return result.stdout
    return None


# ---------------------------------------------------------------------------
# Branch and remote helpers
# ---------------------------------------------------------------------------
def get_current_branch(repo_root: Path) -> Optional[str]:
    result = run_git(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def get_tracking_branch(repo_root: Path) -> Optional[str]:
    branch = get_current_branch(repo_root)
    if not branch:
        return None
    result = run_git(["git", "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"], repo_root)
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def has_remote_origin(repo_root: Path) -> bool:
    result = run_git(["git", "remote", "get-url", "origin"], repo_root)
    return result.returncode == 0 and bool(result.stdout.strip())


def has_unpushed_commits(repo_root: Path) -> bool:
    """Check if there are commits ahead of the tracked remote."""
    tracking = get_tracking_branch(repo_root)
    if not tracking:
        return False
    result = run_git(["git", "log", f"HEAD..{tracking}", "--oneline"], repo_root)
    return result.returncode == 0 and bool(result.stdout.strip())


def has_commits(repo_root: Path) -> bool:
    result = run_git(["git", "rev-parse", "HEAD"], repo_root)
    return result.returncode == 0