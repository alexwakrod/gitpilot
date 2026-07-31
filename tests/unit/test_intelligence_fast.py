"""Fast unit tests for the intelligence engine – no blocking I/O."""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from gitpilot.core.intelligence import DomainClassifier, CommitSplitter, OptimizationScanner


class TestDomainClassifierFast:
    def test_classify_ui(self):
        c = DomainClassifier()
        assert c.classify(Path("components/Button.jsx")) == "ui"

    def test_classify_backend(self):
        c = DomainClassifier()
        assert c.classify(Path("services/auth.py")) == "backend"

    def test_classify_test(self):
        c = DomainClassifier()
        assert c.classify(Path("tests/test_auth.py")) == "test"

    def test_classify_database(self):
        c = DomainClassifier()
        assert c.classify(Path("migrations/001.sql")) == "database"

    def test_classify_config(self):
        c = DomainClassifier()
        assert c.classify(Path(".env.example")) == "config"

    def test_classify_docs(self):
        c = DomainClassifier()
        assert c.classify(Path("docs/README.md")) == "docs"

    def test_user_override(self, tmp_path):
        map_file = tmp_path / "domain_map.json"
        map_file.write_text('{"custom.txt": "docs"}')
        c = DomainClassifier(user_map_path=map_file)
        assert c.classify(Path("custom.txt")) == "docs"

    def test_ast_django_model(self, tmp_path):
        py = tmp_path / "models.py"
        py.write_text("from django.db import models\nclass User(models.Model): pass")
        c = DomainClassifier()
        assert c.classify(py) == "database"

    def test_ast_flask_backend(self, tmp_path):
        py = tmp_path / "app.py"
        py.write_text("from flask import Flask\napp = Flask(__name__)")
        c = DomainClassifier()
        assert c.classify(py) == "backend"

    def test_ast_tkinter_ui(self, tmp_path):
        py = tmp_path / "gui.py"
        py.write_text("import tkinter")
        c = DomainClassifier()
        assert c.classify(py) == "ui"


class TestCommitSplitterFast:
    def test_split_enabled(self):
        c = DomainClassifier()
        s = CommitSplitter(c, enable_splitting=True)
        groups = s.split([Path("components/B.jsx"), Path("services/S.py")])
        assert "ui" in groups
        assert "backend" in groups

    def test_split_disabled(self):
        c = DomainClassifier()
        s = CommitSplitter(c, enable_splitting=False)
        groups = s.split([Path("a.py"), Path("b.jsx")])
        assert "general" in groups
        assert len(groups) == 1

    def test_commit_plan(self):
        c = DomainClassifier()
        s = CommitSplitter(c)
        plan = s.commit_plan([Path("components/B.jsx")])
        assert plan[0]["suggested_scope"] == "ui"


class TestOptimizationScannerFast:
    def test_n_plus_one(self):
        dif = "+for user in User.objects.all():"
        warns = OptimizationScanner.scan_diff(dif)
        assert any("N+1" in w or "select_related" in w for w in warns)

    def test_console_log(self):
        dif = "+console.log('x')"
        warns = OptimizationScanner.scan_diff(dif)
        assert any("console.log" in w.lower() for w in warns)

    def test_print(self):
        dif = "+print('x')"
        warns = OptimizationScanner.scan_diff(dif)
        assert any("print" in w.lower() for w in warns)