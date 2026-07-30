"""Unit tests for commit message cleaner."""

import pytest

from gitpilot.core.committer import clean_commit_message


class TestCleanCommitMessage:
    def test_strips_whitespace(self):
        raw = "   feat: add login button   "
        assert clean_commit_message(raw) == "feat: add login button"

    def test_removes_triple_backticks(self):
        raw = "```\nfix: bug\n```"
        assert clean_commit_message(raw) == "fix: bug"

    def test_removes_quotes_around_message(self):
        raw = '"feat: new feature"'
        assert clean_commit_message(raw) == "feat: new feature"

    def test_removes_leading_bullet_or_dash(self):
        raw = "- docs: update readme"
        assert clean_commit_message(raw) == "docs: update readme"

    def test_collapses_multiple_newlines(self):
        raw = "feat: something\n\n\nbody"
        assert clean_commit_message(raw) == "feat: something\nbody"

    def test_handles_scope_with_parentheses(self):
        raw = "(feat(login): add button)"
        assert clean_commit_message(raw) == "feat(login): add button"

    def test_returns_empty_string_for_blank_input(self):
        assert clean_commit_message("") == ""
        assert clean_commit_message("   ") == ""

    def test_removes_ai_preamble_text(self):
        raw = "Here is the commit message:\nfeat: add endpoint"
        assert clean_commit_message(raw) == "feat: add endpoint"