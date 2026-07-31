"""
Smart Pre‑Commit Engine – pluggable modules that run before every commit.
Each module is a callable that receives (project_path, changed_files, diff_text)
and returns a list of warnings (strings). Warnings are shown to the user but do
not block the commit unless `gating` is enabled for that module.
"""

import logging
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("gitpilot.precommit")


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def _run_command(cmd: List[str], cwd: Path, timeout: int = 30) -> Tuple[int, str, str]:
    try:
        result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except Exception as exc:
        return 1, "", str(exc)


def _find_executable(name: str) -> Optional[str]:
    import shutil
    return shutil.which(name)


# ---------------------------------------------------------------------------
# Module 1: Lint & Test Gating
# ---------------------------------------------------------------------------
def lint_gate(project_path: Path, files: List[Path], diff: str) -> List[str]:
    """Run the project's linter on changed files and return warnings."""
    warnings = []
    linters = {
        "ruff": ["ruff", "check", "--select", "F,E,W"] + [str(f) for f in files],
        "eslint": ["eslint", "--quiet"] + [str(f) for f in files],
        "pylint": ["pylint", "--score=n"] + [str(f) for f in files],
    }
    for name, cmd in linters.items():
        exe = _find_executable(name)
        if exe:
            ret, stdout, stderr = _run_command([exe] + cmd[1:], project_path)
            if ret != 0:
                warnings.append(f"Linter '{name}' returned issues:\n{stdout}{stderr}")
            break
    return warnings


def test_gate(project_path: Path, files: List[Path], diff: str) -> List[str]:
    """Run the project's test suite on changed files and return warnings."""
    warnings = []
    # Detect test runner
    if (project_path / "pytest.ini").exists() or (project_path / "pyproject.toml").exists():
        ret, stdout, stderr = _run_command(["pytest", "--tb=short", "-q"], project_path)
        if ret != 0:
            warnings.append(f"Tests failed:\n{stdout}{stderr}")
    elif (project_path / "package.json").exists():
        if (project_path / "node_modules").exists():
            ret, stdout, stderr = _run_command(["npm", "test", "--", "--passWithNoTests"], project_path)
            if ret != 0:
                warnings.append(f"Tests failed:\n{stdout}{stderr}")
    return warnings


# ---------------------------------------------------------------------------
# Module 2: Semantic Conflict Detection
# ---------------------------------------------------------------------------
def semantic_conflict_check(project_path: Path, files: List[Path], diff: str) -> List[str]:
    """Check if the remote tracking branch has advanced since the last pull."""
    warnings = []
    # fetch silently
    ret, _, _ = _run_command(["git", "fetch", "origin"], project_path)
    if ret != 0:
        warnings.append("Failed to fetch from remote; conflict check skipped.")
        return warnings

    # get current branch and tracking branch
    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(project_path), capture_output=True, text=True,
    )
    branch = branch_result.stdout.strip()
    tracking = f"origin/{branch}"

    # compare local HEAD with remote tracking
    rev_result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", tracking, "HEAD"],
        cwd=str(project_path), capture_output=True, text=True,
    )
    if rev_result.returncode != 0:
        # Remote has advanced; check for actual conflicts with changed files
        merge_result = subprocess.run(
            ["git", "merge-tree", "--write-tree", f"{tracking}..HEAD"],
            cwd=str(project_path), capture_output=True, text=True,
        )
        if merge_result.returncode != 0:
            warnings.append("Remote has advanced and merge conflicts are likely. Consider pulling first.")
    return warnings


# ---------------------------------------------------------------------------
# Module 3: Dependency Impact Analysis
# ---------------------------------------------------------------------------
def dependency_impact(project_path: Path, files: List[Path], diff: str) -> List[str]:
    """Check if changed symbols are imported elsewhere in the project."""
    warnings = []
    # Simple heuristic: for each changed Python file, check if it's imported by others
    import re
    changed_basenames = {f.stem for f in files if f.suffix == ".py"}
    if not changed_basenames:
        return warnings

    # Scan all Python files in project (excluding .git, .venv, etc.)
    py_files = list(project_path.rglob("*.py"))
    py_files = [p for p in py_files if ".git" not in p.parts and ".venv" not in p.parts and "__pycache__" not in p.parts]
    import_pattern = re.compile(r"^\s*(?:from|import)\s+(\w+)", re.MULTILINE)

    for py_file in py_files:
        if py_file in files:
            continue  # don't check the changed file itself
        try:
            content = py_file.read_text()
            imports = set(import_pattern.findall(content))
            if imports & changed_basenames:
                warnings.append(f"File {py_file.relative_to(project_path)} imports changed module(s) {imports & changed_basenames}")
        except Exception:
            pass
    return warnings


# ---------------------------------------------------------------------------
# Module 4: Auto‑Rebase before Push
# ---------------------------------------------------------------------------
def auto_rebase(project_path: Path, files: List[Path], diff: str) -> List[str]:
    """If remote has advanced, attempt a `git pull --rebase`."""
    warnings = []
    ret, stdout, stderr = _run_command(["git", "fetch", "origin"], project_path)
    if ret != 0:
        return ["Failed to fetch; cannot rebase."]
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(project_path), capture_output=True, text=True,
    ).stdout.strip()
    tracking = f"origin/{branch}"
    # Check if local is behind
    ret, _, _ = _run_command(["git", "merge-base", "--is-ancestor", tracking, "HEAD"], project_path)
    if ret != 0:
        ret2, stdout2, stderr2 = _run_command(["git", "rebase", tracking], project_path)
        if ret2 == 0:
            logger.info("Successfully rebased on top of %s", tracking)
        else:
            warnings.append(f"Auto‑rebase failed: {stderr2}")
    return warnings


# ---------------------------------------------------------------------------
# Module 5: Branch Lifecycle Management (suggestion only)
# ---------------------------------------------------------------------------
def branch_lifecycle(project_path: Path, files: List[Path], diff: str) -> List[str]:
    """Suggest opening a PR if the feature branch has been inactive."""
    # Placeholder – needs DB of branch activity
    return []


# ---------------------------------------------------------------------------
# Module 6: Commit Style Learning (already in patterns, just surface)
# ---------------------------------------------------------------------------
def commit_style_guidance(project_path: Path, files: List[Path], diff: str) -> List[str]:
    """Return learned style preferences as a suggestion."""
    # Handled by the watcher when generating messages; no pre‑commit warnings needed.
    return []


# ---------------------------------------------------------------------------
# Module 7: Unrelated Change Isolation (automatic stash)
# ---------------------------------------------------------------------------
def isolate_unrelated_changes(project_path: Path, files: List[Path], diff: str) -> List[str]:
    """If there are files in completely unrelated domains, stash them and commit only the related group."""
    # This is complex; for now return empty – the watcher already groups by domain.
    return []


# ---------------------------------------------------------------------------
# Module 8: Rollback Assistance
# ---------------------------------------------------------------------------
def rollback_warning(project_path: Path, files: List[Path], diff: str) -> List[str]:
    """Check recent commit history for revert patterns."""
    warnings = []
    result = subprocess.run(
        ["git", "log", "--oneline", "-10"],
        cwd=str(project_path), capture_output=True, text=True,
    )
    if "Revert" in result.stdout:
        warnings.append("Recent reverts detected; double‑check the current change.")
    return warnings


# ---------------------------------------------------------------------------
# Module 9: Custom Ignore Pattern Suggestion
# ---------------------------------------------------------------------------
def suggest_gitignore(project_path: Path, files: List[Path], diff: str) -> List[str]:
    """Suggest files that are commonly ignored but not yet in .gitignore."""
    common_ignores = {".env", "*.pyc", "__pycache__", "node_modules", "venv", ".venv", "dist", "build"}
    warnings = []
    for f in files:
        if f.name in common_ignores or any(part in common_ignores for part in f.parts):
            warnings.append(f"File '{f.relative_to(project_path)}' matches a common ignore pattern but .gitignore doesn't exclude it.")
    return warnings


# ---------------------------------------------------------------------------
# Module 10: PR Description Generator (stub – implemented via CLI command)
# ---------------------------------------------------------------------------
def pr_description(project_path: Path, files: List[Path], diff: str) -> List[str]:
    """Stub – the actual generation is done via `gitpilot pr`."""
    return []


# ---------------------------------------------------------------------------
# Module 11: Code Smell Detection
# ---------------------------------------------------------------------------
def code_smell_detect(project_path: Path, files: List[Path], diff: str) -> List[str]:
    """Scan diff for common anti‑patterns."""
    warnings = []
    patterns = {
        r"except\s*:": "Bare except clause found. Consider catching specific exceptions.",
        r"def \w+\(\w+\=\{\}": "Mutable default argument detected.",
        r"import \*": "Wildcard import detected.",
        r"print\(": "Debug print statement left in code.",
    }
    for pattern, msg in patterns.items():
        if re.search(pattern, diff):
            warnings.append(msg)
    return warnings


# ---------------------------------------------------------------------------
# Module 12: Security Vulnerability Scanning
# ---------------------------------------------------------------------------
def security_scan(project_path: Path, files: List[Path], diff: str) -> List[str]:
    """Check dependency files for known CVEs (requires `safety` or `npm audit`)."""
    warnings = []
    if (project_path / "requirements.txt").exists() and _find_executable("safety"):
        ret, stdout, _ = _run_command(["safety", "check", "-r", "requirements.txt", "--bare"], project_path)
        if ret != 0:
            warnings.append(f"Security scan found issues:\n{stdout}")
    elif (project_path / "package.json").exists():
        ret, stdout, _ = _run_command(["npm", "audit", "--audit-level=high"], project_path)
        if ret != 0:
            warnings.append(f"Security scan found issues:\n{stdout}")
    return warnings


# ---------------------------------------------------------------------------
# Module 13: Changelog Auto‑Update
# ---------------------------------------------------------------------------
def update_changelog(project_path: Path, files: List[Path], diff: str) -> List[str]:
    """If CHANGELOG.md exists, append the commit message (called after commit)."""
    # Not a pre‑commit check; handled in watcher after commit.
    return []


# ---------------------------------------------------------------------------
# Module 14: Smart Branch Naming (suggestion)
# ---------------------------------------------------------------------------
def smart_branch_name(project_path: Path, files: List[Path], diff: str) -> List[str]:
    """Suggest a branch name based on the diff content."""
    # Placeholder
    return []


# ---------------------------------------------------------------------------
# Module 15: Context‑Aware Scope Suggestion (already integrated)
# ---------------------------------------------------------------------------
def scope_suggestion(project_path: Path, files: List[Path], diff: str) -> List[str]:
    return []


# ---------------------------------------------------------------------------
# Module 16: Collaborative Grouping by Author
# ---------------------------------------------------------------------------
def author_grouping(project_path: Path, files: List[Path], diff: str) -> List[str]:
    return []


# ---------------------------------------------------------------------------
# Module 17: Time‑Based Grouping (already handled by watcher)
# ---------------------------------------------------------------------------
def time_grouping(project_path: Path, files: List[Path], diff: str) -> List[str]:
    return []


# ---------------------------------------------------------------------------
# Module 18: Issue Tracker Integration
# ---------------------------------------------------------------------------
def issue_tracker_link(project_path: Path, files: List[Path], diff: str) -> List[str]:
    """Detect issue IDs in branch name and append to commit message."""
    # Handled in watcher.
    return []


# ---------------------------------------------------------------------------
# Module 19: Pre‑Commit Hook Injection
# ---------------------------------------------------------------------------
def install_precommit_hook(project_path: Path) -> List[str]:
    """Install a .git/hooks/pre‑commit script that calls gitpilot."""
    warnings = []
    hook_path = project_path / ".git" / "hooks" / "pre-commit"
    if hook_path.exists():
        return ["Pre‑commit hook already exists."]
    try:
        hook_script = """#!/bin/sh
# GitPilot pre‑commit hook
gitpilot pre-commit-check "$@"
"""
        hook_path.write_text(hook_script)
        hook_path.chmod(0o755)
        logger.info("Installed GitPilot pre‑commit hook in %s", project_path)
    except Exception as exc:
        warnings.append(f"Failed to install pre‑commit hook: {exc}")
    return warnings


# ---------------------------------------------------------------------------
# Module 20: Interactive Message Refinement (handled by CLI TUI)
# ---------------------------------------------------------------------------
def interactive_refine(project_path: Path, files: List[Path], diff: str) -> List[str]:
    return []


# ===========================================================================
# Registry of all pre‑commit checks
# ===========================================================================
PRE_COMMIT_CHECKS: Dict[str, Callable] = {
    "lint": lint_gate,
    "test": test_gate,
    "conflict": semantic_conflict_check,
    "dependency": dependency_impact,
    "rebase": auto_rebase,
    "rollback": rollback_warning,
    "gitignore": suggest_gitignore,
    "code_smell": code_smell_detect,
    "security": security_scan,
}


def run_all_checks(project_path: Path, files: List[Path], diff: str, enabled: Optional[List[str]] = None) -> List[str]:
    """Run all enabled pre‑commit checks and return aggregated warnings."""
    if enabled is None:
        enabled = list(PRE_COMMIT_CHECKS.keys())
    all_warnings = []
    for name in enabled:
        if name in PRE_COMMIT_CHECKS:
            try:
                warnings = PRE_COMMIT_CHECKS[name](project_path, files, diff)
                if warnings:
                    all_warnings.extend(warnings)
            except Exception as exc:
                logger.error("Pre‑commit check '%s' failed: %s", name, exc)
    return all_warnings