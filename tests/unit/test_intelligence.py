"""Intelligence Engine: domain-aware commit separation, AST analysis, optimization hints."""

import ast
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("gitpilot.intelligence")

# ---------------------------------------------------------------------------
# Domain rules **order matters** – more specific rules must come first
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

# Django / ORM detection in AST
DJANGO_ORM_MODULES = {"django.db", "django.contrib"}
ORM_CLASSES = {"Model", "models.Model"}
UI_FRAMEWORKS = {"tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "kivy"}


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

        # 1. User override
        if rel in self.user_map:
            return self.user_map[rel]
        for user_path, domain in self.user_map.items():
            if rel.startswith(user_path) or rel.endswith(user_path):
                return domain

        path_str = rel.replace("\\", "/")

        # 2. Path heuristics – test must be checked before *.py
        for domain, patterns in DOMAIN_RULES.items():
            for pattern in patterns:
                if self._match_pattern(path_str, pattern):
                    # For Python files that matched backend, further check with AST
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
        """Use AST to detect Django ORM models, UI frameworks, or test files."""
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
            # Check for model class
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for base in node.bases:
                        base_name = self._get_base_name(base)
                        if base_name in ORM_CLASSES:
                            return "database"
            # If ORM imports but no model, still backend
            return "backend"

        if imports_ui:
            return "ui"

        # If file stem contains 'test', it's a test
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


class CommitSplitter:
    """Splits a list of changed files into domain groups for separate commits."""

    def __init__(self, classifier: DomainClassifier, enable_splitting: bool = True):
        self.classifier = classifier
        self.enable_splitting = enable_splitting

    def split(
        self,
        files: List[Path],
        project_root: Optional[Path] = None,
    ) -> Dict[str, List[Path]]:
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
    ) -> List[Dict[str, Any]]:
        groups = self.split(files, project_root)
        plan = []
        for domain, domain_files in groups.items():
            scope = domain if domain != "general" else "misc"
            plan.append({
                "domain": domain,
                "files": domain_files,
                "suggested_scope": scope,
            })
        return plan


class OptimizationScanner:
    """Scans diffs for common performance issues and returns suggestions."""

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