"""Intelligence Engine: atomic, context‑aware AI‑powered commit grouping, domain classification, and optimization hints."""

import ast
import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("gitpilot.intelligence")

# ---------------------------------------------------------------------------
# Domain rules (unchanged)
# ---------------------------------------------------------------------------
DOMAIN_RULES: Dict[str, List[str]] = {
    "test": [
        "tests/", "spec/", "__tests__/", "*.test.py", "*.spec.js",
        "*.test.js", "*.test.ts", "*.spec.ts",
    ],
    "ui": [
        "components/", "pages/", "views/", "layouts/", "frontend/",
        "*.vue", "*.jsx", "*.tsx", "*.html", "*.css", "*.scss", "*.less",
        "static/", "assets/", "templates/",
    ],
    "database": [
        "migrations/", "schema/", "*.sql", "alembic/", "prisma/",
    ],
    "backend": [
        "services/", "controllers/", "middleware/", "routes/", "api/",
        "*.py", "*.js", "*.ts",
        "server/", "handlers/",
    ],
    "config": [
        ".env", ".env.example", "*.yaml", "*.yml", "*.toml", "*.json",
        "config/", "settings/", "Dockerfile", "docker-compose*",
        "Makefile", "*.ini", "*.cfg",
    ],
    "docs": [
        "docs/", "*.md", "*.rst", "README*", "CHANGELOG*", "CONTRIBUTING*",
    ],
}

DJANGO_ORM_MODULES = {"django.db", "django.contrib"}
ORM_CLASSES = {"Model", "models.Model"}
UI_FRAMEWORKS = {"tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "kivy"}


# ===========================================================================
# Domain classifier 
# ===========================================================================
class DomainClassifier:
    def __init__(self, user_map_path: Optional[Path] = None):
        self.user_map: Dict[str, str] = {}
        if user_map_path and user_map_path.exists():
            try:
                raw = json.loads(user_map_path.read_text())
                self.user_map = {
                    k.strip(): v.strip()
                    for k, v in raw.items()
                    if v in DOMAIN_RULES
                }
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load user domain map: %s", exc)

    def classify(self, file_path: Path, project_root: Optional[Path] = None) -> str:
        try:
            rel = str(file_path.relative_to(project_root)) if project_root else str(file_path)
        except ValueError:
            rel = str(file_path)

        if rel in self.user_map:
            return self.user_map[rel]
        for user_path, domain in self.user_map.items():
            if rel.startswith(user_path) or rel.endswith(user_path):
                return domain

        path_str = rel.replace("\\", "/")
        for domain, patterns in DOMAIN_RULES.items():
            for pattern in patterns:
                if self._match_pattern(path_str, pattern):
                    if path_str.endswith(".py") and domain == "backend":
                        subdomain = self._classify_python_file(file_path)
                        if subdomain:
                            return subdomain
                    return domain
        return "other"

    def _match_pattern(self, path_str: str, pattern: str) -> bool:
        if pattern.startswith("*."):
            return path_str.endswith(pattern[1:])
        if "/" in pattern or pattern.endswith("/"):
            return f"/{pattern}" in f"/{path_str}" or path_str.startswith(pattern)
        return path_str == pattern or path_str.endswith(f"/{pattern}")

    def _classify_python_file(self, file_path: Path) -> Optional[str]:
        try:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, OSError, UnicodeDecodeError):
            return None

        imports_django = False
        imports_ui = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    if name.startswith("django.db") or name.startswith("django.contrib"):
                        imports_django = True
                    if name in UI_FRAMEWORKS:
                        imports_ui = True
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    if node.module.startswith("django.db") or node.module.startswith("django.contrib"):
                        imports_django = True
                    if node.module in UI_FRAMEWORKS:
                        imports_ui = True

        if imports_django:
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for base in node.bases:
                        base_name = self._get_base_name(base)
                        if base_name in ORM_CLASSES:
                            return "database"
            return "backend"
        if imports_ui:
            return "ui"

        stem = file_path.stem
        if stem.startswith("test") or stem.endswith("_test") or stem.endswith("_spec"):
            return "test"
        return None

    @staticmethod
    def _get_base_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            value = DomainClassifier._get_base_name(node.value)
            return f"{value}.{node.attr}"
        return ""


# ===========================================================================
# Atomic group detector (unchanged)
# ===========================================================================
class AtomicGroupDetector:
    ATOMIC_PATTERNS = [
        {r"(^|/)pyproject\.toml$", r"(^|/)gitpilot/__init__\.py$", r"(^|/)gitpilot/cli/main\.py$"},
        {r"(^|/)settings\.py$", r"(^|/)config\.py$"},
        {r"(^|/)models\.py$", r".*/migrations/.*\.py$"},
        {r"(^|/)tests/.*\.py$", r"(^|/)(?!tests/).*\.py$"},
    ]

    @classmethod
    def detect(cls, files: List[Path]) -> List[Set[Path]]:
        remaining = set(files)
        groups: List[Set[Path]] = []
        for pattern_set in cls.ATOMIC_PATTERNS:
            matched = set()
            for file in list(remaining):
                for regex in pattern_set:
                    if re.search(regex, str(file)):
                        matched.add(file)
                        break
            if len(matched) >= 2:
                groups.append(matched)
                remaining -= matched
        for f in sorted(remaining, key=str):
            groups.append({f})
        merged = True
        while merged:
            merged = False
            for i in range(len(groups)):
                for j in range(i + 1, len(groups)):
                    if groups[i] & groups[j]:
                        groups[i] |= groups[j]
                        del groups[j]
                        merged = True
                        break
                if merged:
                    break
        return groups


# ===========================================================================
# AI‑powered commit grouper (context‑aware)
# ===========================================================================
class AICommitGrouper:
    def __init__(self, ai_committer, project_root: Path):
        self.ai = ai_committer
        self.project_root = project_root
        self.classifier = DomainClassifier()

    async def group_files(
        self, files: List[Path], project_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        if len(files) <= 1:
            return [{"description": "chore: update files", "files": files, "suggested_scope": "general"}]

        file_desc = []
        for f in files:
            try:
                rel = str(f.relative_to(self.project_root))
            except ValueError:
                rel = str(f)
            domain = self.classifier.classify(f, self.project_root)
            preview = ""
            try:
                preview = f.read_text(encoding="utf-8")[:80].replace("\n", " ")
            except Exception:
                pass
            file_desc.append(f"  {rel}  (domain: {domain})  [{preview}]")

        # Build context from recent commits
        context = ""
        if project_id is not None:
            try:
                from gitpilot.infrastructure.db import managed_connection
                from gitpilot.infrastructure.repositories.commits import CommitsRepository
                with managed_connection() as conn:
                    repo = CommitsRepository(conn)
                    recent, _ = repo.list_by_project(project_id, limit=10)
                if recent:
                    context = "Recent commits in this project (most recent first):\n"
                    for c in recent:
                        context += f"  {c['hash'][:8]} {c['message']} ({c['branch']})\n"
                    context += "\n"
            except Exception as exc:
                logger.debug("Failed to fetch recent commits for context: %s", exc)

        prompt = (
            "You are an expert developer assistant.  A developer has just modified "
            "several files in their project.  Your job is to understand the *intent* "
            "behind the changes and group the files into logical commits.\n\n"
            + context +
            "For each group, write a short conventional‑commit message (type(scope): description) "
            "that explains the true purpose of that group of changes.  Then list the relative "
            "file paths that belong to that group.\n\n"
            "CRITICAL: if the changes are part of the same overall purpose (e.g. a version bump, "
            "a bug fix, a feature addition), they MUST stay together in the same group.  "
            "Only create separate groups when the changes are truly independent.\n\n"
            "If you are unsure, put everything in ONE group with a message like "
            "\"chore: batch update\".\n\n"
            "Output ONLY a JSON array of objects, each with keys:\n"
            "  - \"description\": a short commit message (string)\n"
            "  - \"files\": list of relative paths (strings)\n\n"
            "Example output:\n"
            '[{"description": "feat(backend): add user authentication module", '
            '"files": ["backend/auth.py", "backend/models.py", "tests/test_auth.py"]}]\n\n'
            "Files:\n"
        ) + "\n".join(file_desc)

        try:
            raw_message = await self.ai.generate_message(
                diff="",
                branch=None,
                scope_hint=None,
                custom_prompt=prompt,
            )
            if not raw_message:
                return self._fallback_mixed(files)

            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_message.strip(), flags=re.MULTILINE)
            cleaned = cleaned.strip().strip("'").strip('"')
            start = cleaned.find("[")
            end = cleaned.rfind("]")
            if start == -1 or end == -1:
                return self._fallback_mixed(files)

            json_str = cleaned[start:end+1]
            groups = json.loads(json_str)

            result = []
            for group in groups:
                paths = []
                for name in group.get("files", []):
                    full = self.project_root / name
                    if full.exists():
                        paths.append(full)
                if paths:
                    result.append({
                        "description": group.get("description", "chore: update files"),
                        "files": paths,
                        "suggested_scope": "general",
                    })
            if result:
                result = self._enforce_atomic_merge(files, result)
                return result
            else:
                return self._fallback_mixed(files)
        except Exception as exc:
            logger.warning("AI grouping failed (%s) – falling back to mixed commit", exc)
            return self._fallback_mixed(files)

    def _fallback_mixed(self, files: List[Path]) -> List[Dict[str, Any]]:
        return [{"description": "chore: batch update", "files": files, "suggested_scope": "mixed"}]

    def _enforce_atomic_merge(
        self, all_files: List[Path], groups: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        atomic_sets = AtomicGroupDetector.detect(all_files)
        file_to_group: Dict[Path, int] = {}
        for idx, g in enumerate(groups):
            for f in g["files"]:
                file_to_group[f] = idx

        merged_indices: Set[int] = set()
        for atom in atomic_sets:
            group_indices = set()
            for f in atom:
                if f in file_to_group:
                    group_indices.add(file_to_group[f])
            if len(group_indices) > 1:
                main_idx = min(group_indices)
                for idx in group_indices:
                    if idx != main_idx:
                        groups[main_idx]["files"].extend(groups[idx]["files"])
                        merged_indices.add(idx)
        if merged_indices:
            groups = [g for i, g in enumerate(groups) if i not in merged_indices]
            seen = set()
            for g in groups:
                g["files"] = [f for f in g["files"] if not (f in seen or seen.add(f))]
        return groups


# ===========================================================================
# Commit splitter (orchestrator)
# ===========================================================================
class CommitSplitter:
    def __init__(
        self,
        classifier: DomainClassifier,
        enable_splitting: bool = True,
        ai_committer=None,
        project_root: Optional[Path] = None,
        use_ai_grouping: bool = True,
    ):
        self.classifier = classifier
        self.enable_splitting = enable_splitting
        self.ai_grouper = None
        if use_ai_grouping and ai_committer and project_root:
            self.ai_grouper = AICommitGrouper(ai_committer, project_root)

    def split(
        self,
        files: List[Path],
        project_root: Optional[Path] = None,
        project_id: Optional[int] = None,
    ) -> Dict[str, List[Path]]:
        if not self.enable_splitting:
            return {"general": files}
        if self.ai_grouper:
            return {"mixed": files}
        groups: Dict[str, List[Path]] = {}
        for f in files:
            domain = self.classifier.classify(f, project_root)
            groups.setdefault(domain, []).append(f)
        if "other" in groups:
            groups.setdefault("general", []).extend(groups.pop("other"))
        return groups

    def commit_plan(
        self,
        files: List[Path],
        project_root: Optional[Path] = None,
        branch: Optional[str] = None,
        project_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if not self.enable_splitting:
            return [{"files": files, "suggested_scope": "mixed", "domain": "mixed"}]

        if self.ai_grouper:
            try:
                plan = asyncio.run(self.ai_grouper.group_files(files, project_id))
                if plan:
                    for item in plan:
                        item["domain"] = item.get("suggested_scope", "mixed")
                    return plan
            except Exception as exc:
                logger.warning("AI grouping failed: %s", exc)
            return [{"files": files, "suggested_scope": "mixed", "domain": "mixed"}]

        groups = self.split(files, project_root, project_id)
        plan = []
        for domain, domain_files in groups.items():
            scope = domain if domain != "general" else "misc"
            plan.append({"domain": domain, "files": domain_files, "suggested_scope": scope})
        return plan


# ===========================================================================
# Optimization scanner (unchanged)
# ===========================================================================
class OptimizationScanner:
    WARNING_PATTERNS = [
        (r"select_related\s*\(.*\)", None),
        (r"\.all\(\)(?!.*select_related)", "Consider adding select_related() to reduce queries"),
        (r"for\s+\w+\s+in\s+.*\.objects\.all\(\)", "Potential N+1 query: use select_related or prefetch_related"),
        (r"console\.log\(", "Remove debug console.log before commit"),
        (r"print\(", "Remove debug print() before commit"),
    ]

    @classmethod
    def scan_diff(cls, diff_text: str) -> List[str]:
        warnings = []
        lines = diff_text.splitlines()
        added_lines = [line[1:] for line in lines if line.startswith("+") and not line.startswith("+++")]
        for pattern, message in cls.WARNING_PATTERNS:
            if message is None:
                continue
            for line in added_lines:
                if re.search(pattern, line):
                    warnings.append(message)
                    break
        return warnings