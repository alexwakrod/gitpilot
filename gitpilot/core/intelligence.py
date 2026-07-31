"""Intelligence Engine: domain-aware commit separation, AST analysis, AI‑powered
   grouping of related changes, and optimization hints."""

import ast
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("gitpilot.intelligence")

# ---------------------------------------------------------------------------
# Domain rules – order matters: more specific rules must come first
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
# Domain classifier (unchanged logic, kept for fallback)
# ===========================================================================
class DomainClassifier:
    """Assigns a domain to each file path using path heuristics and AST analysis."""

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
# AI‑powered commit grouper
# ===========================================================================
class AICommitGrouper:
    """Uses the configured AI provider to group a set of changed files into
       logical commit groups.  Returns a list of group descriptors, each with
       a list of file paths and a suggested scope.
    """

    def __init__(self, ai_committer, project_root: Path):
        self.ai = ai_committer
        self.project_root = project_root
        self.classifier = DomainClassifier()

    async def group_files(
        self, files: List[Path]
    ) -> List[Dict[str, Any]]:
        """
        Ask the AI to group `files` into logical commits.
        Returns a list of dicts: {'files': [Path, ...], 'suggested_scope': str}
        Falls back to domain splitting if AI fails.
        """
        if len(files) <= 1:
            return [{"files": files, "suggested_scope": "general"}]

        # Build a concise representation for the prompt
        file_desc = []
        for f in files:
            try:
                rel = str(f.relative_to(self.project_root))
            except ValueError:
                rel = str(f)
            domain = self.classifier.classify(f, self.project_root)
            # Include a tiny preview of the file content (first 80 chars)
            preview = ""
            try:
                preview = f.read_text(encoding="utf-8")[:80].replace("\n", " ")
            except Exception:
                pass
            file_desc.append(f"  {rel}  (domain: {domain})  [{preview}]")

        prompt = (
            "You are an expert in conventional commits and semantic grouping of changes.\n"
            "Below is a list of files that were changed together. Group them into logical "
            "commits, where each group contains files that belong to the same feature, "
            "bug-fix, chore, or refactoring. Provide the grouping as a JSON array of objects, "
            "each with keys 'files' (list of relative paths, as given) and "
            "'scope' (a short conventional commit scope string, e.g., 'ui', 'backend', 'config').\n"
            "Do NOT add any other text.\n\n"
            "Example output:\n"
            '[{"files": ["backend/auth.py", "backend/models.py"], "scope": "backend"}, '
            '{"files": ["README.md"], "scope": "docs"}]\n\n'
            "Files:\n"
        ) + "\n".join(file_desc)

        try:
            raw_message = await self.ai.generate_message(
                diff="",           # not used for grouping
                branch=None,
                scope_hint=None,
                custom_prompt=prompt,
            )
            if not raw_message:
                return self._fallback(files)

            # The AI may wrap the JSON in backticks or prefix text – clean and extract JSON
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_message.strip(), flags=re.MULTILINE)
            cleaned = cleaned.strip().strip("'").strip('"')
            # Find the first '[' and last ']'
            start = cleaned.find("[")
            end = cleaned.rfind("]")
            if start == -1 or end == -1:
                raise ValueError("No JSON array found")
            json_str = cleaned[start:end+1]
            groups = json.loads(json_str)

            # Validate and rebuild groups with Path objects
            result = []
            for group in groups:
                paths = []
                for name in group.get("files", []):
                    full = self.project_root / name
                    if full.exists():
                        paths.append(full)
                if paths:
                    result.append({
                        "files": paths,
                        "suggested_scope": group.get("scope", "general"),
                    })
            if result:
                return result
        except Exception as exc:
            logger.warning("AI grouping failed, falling back to domain split: %s", exc)

        return self._fallback(files)

    def _fallback(self, files: List[Path]) -> List[Dict[str, Any]]:
        """Fall back to domain‑based splitting."""
        groups: Dict[str, List[Path]] = {}
        for f in files:
            domain = self.classifier.classify(f, self.project_root)
            groups.setdefault(domain, []).append(f)
        if "other" in groups:
            groups.setdefault("general", []).extend(groups.pop("other"))
        return [
            {"files": paths, "suggested_scope": domain}
            for domain, paths in groups.items()
        ]


# ===========================================================================
# Commit splitter (entry point – uses AI grouping if available, else domain)
# ===========================================================================
class CommitSplitter:
    """Splits a list of changed files into domain groups, with optional AI grouping."""

    def __init__(
        self,
        classifier: DomainClassifier,
        enable_splitting: bool = True,
        ai_committer=None,
        project_root: Optional[Path] = None,
        use_ai_grouping: bool = False,
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
        """Group files by domain, optionally using AI (sync wrapper – returns simple dict).
           The async AI path is handled in the watcher via commit_plan."""
        if not self.enable_splitting:
            return {"general": files}

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
        """Return a plan of commits. If AI grouping is enabled, use it asynchronously."""
        if not self.enable_splitting:
            return [{"files": files, "suggested_scope": "general"}]

        # If AI grouper is configured, use it (async call inside watcher)
        if self.ai_grouper:
            import asyncio
            try:
                plan = asyncio.run(self.ai_grouper.group_files(files))
                if plan:
                    # Add domain field for each group
                    for item in plan:
                        item["domain"] = item.get("suggested_scope", "general")
                    return plan
            except Exception as exc:
                logger.warning("AI grouping failed in commit_plan: %s", exc)

        # Fall back to classic domain split
        groups = self.split(files, project_root, project_id)
        plan = []
        for domain, domain_files in groups.items():
            scope = domain if domain != "general" else "misc"
            plan.append({
                "domain": domain,
                "files": domain_files,
                "suggested_scope": scope,
            })
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