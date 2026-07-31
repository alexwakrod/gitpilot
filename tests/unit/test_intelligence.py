"""Unit tests for the intelligence engine: domain classification, commit splitting, optimization scanning."""

import tempfile
from pathlib import Path

import pytest

from gitpilot.core.intelligence import DomainClassifier, CommitSplitter, OptimizationScanner


class TestDomainClassifier:
    def test_classify_ui_file_by_path(self):
        c = DomainClassifier()
        assert c.classify(Path("components/Button.jsx")) == "ui"
        assert c.classify(Path("pages/index.vue")) == "ui"
        assert c.classify(Path("static/style.css")) == "ui"

    def test_classify_backend_file_by_path(self):
        c = DomainClassifier()
        assert c.classify(Path("services/auth.py")) == "backend"
        assert c.classify(Path("controllers/user_controller.py")) == "backend"

    def test_classify_database_file_by_path(self):
        c = DomainClassifier()
        assert c.classify(Path("migrations/001_init.sql")) == "database"
        assert c.classify(Path("schema.sql")) == "database"

    def test_classify_test_file_by_path(self):
        c = DomainClassifier()
        assert c.classify(Path("tests/test_auth.py")) == "test"
        assert c.classify(Path("spec/user.spec.js")) == "test"

    def test_classify_config_file_by_path(self):
        c = DomainClassifier()
        assert c.classify(Path(".env")) == "config"
        assert c.classify(Path("Dockerfile")) == "config"
        assert c.classify(Path("config/settings.yaml")) == "config"

    def test_classify_docs_file_by_path(self):
        c = DomainClassifier()
        assert c.classify(Path("docs/README.md")) == "docs"
        assert c.classify(Path("CHANGELOG.md")) == "docs"

    def test_classify_unknown_file_as_general(self):
        c = DomainClassifier()
        assert c.classify(Path("unknown.xyz")) == "other"

    def test_user_override_takes_precedence(self, tmp_path):
        map_file = tmp_path / "domain_map.json"
        map_file.write_text('{"unknown.xyz": "ui"}')
        c = DomainClassifier(user_map_path=map_file)
        assert c.classify(Path("unknown.xyz")) == "ui"

    def test_python_ast_detects_django_model_as_database(self, tmp_path):
        py_file = tmp_path / "models.py"
        py_file.write_text("""
from django.db import models
class User(models.Model):
    pass
""")
        c = DomainClassifier()
        domain = c.classify(py_file)
        assert domain == "database"

    def test_python_ast_detects_flask_backend(self, tmp_path):
        py_file = tmp_path / "app.py"
        py_file.write_text("""
from flask import Flask
app = Flask(__name__)
""")
        c = DomainClassifier()
        domain = c.classify(py_file)
        assert domain == "backend"

    def test_python_ast_detects_ui_framework(self, tmp_path):
        py_file = tmp_path / "gui.py"
        py_file.write_text("""
import tkinter
root = tkinter.Tk()
""")
        c = DomainClassifier()
        domain = c.classify(py_file)
        assert domain == "ui"


class TestCommitSplitter:
    def test_split_enabled(self):
        classifier = DomainClassifier()
        splitter = CommitSplitter(classifier, enable_splitting=True)
        files = [
            Path("components/Button.jsx"),
            Path("services/auth.py"),
            Path("tests/test_auth.py"),
            Path(".env"),
        ]
        groups = splitter.split(files)
        assert "ui" in groups
        assert "backend" in groups
        assert "test" in groups
        assert "config" in groups
        assert len(groups) == 4

    def test_split_disabled_returns_general(self):
        classifier = DomainClassifier()
        splitter = CommitSplitter(classifier, enable_splitting=False)
        files = [Path("components/Button.jsx"), Path("services/auth.py")]
        groups = splitter.split(files)
        assert "general" in groups
        assert len(groups) == 1

    def test_commit_plan_returns_domain_scope(self):
        classifier = DomainClassifier()
        splitter = CommitSplitter(classifier, enable_splitting=True)
        files = [Path("components/Button.jsx")]
        plan = splitter.commit_plan(files)
        assert len(plan) == 1
        assert plan[0]["domain"] == "ui"
        assert plan[0]["suggested_scope"] == "ui"


class TestOptimizationScanner:
    def test_scan_diff_detects_n_plus_one(self):
        diff = """
+for user in User.objects.all():
+    print(user.name)
"""
        warnings = OptimizationScanner.scan_diff(diff)
        assert any("N+1" in w for w in warnings) or any("select_related" in w for w in warnings)

    def test_scan_diff_detects_debug_print(self):
        diff = """
+print("debug")
+console.log("test")
"""
        warnings = OptimizationScanner.scan_diff(diff)
        assert any("print" in w.lower() for w in warnings)

    def test_scan_diff_no_warnings_on_clean_code(self):
        diff = """
+def add(a, b):
+    return a + b
"""
        warnings = OptimizationScanner.scan_diff(diff)
        assert len(warnings) == 0