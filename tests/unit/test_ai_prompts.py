"""Unit tests for AI prompt builder."""

import pytest

from gitpilot.core.committer import build_commit_prompt


class TestBuildCommitPrompt:
    def test_prompt_without_branch(self):
        diff = "diff --git a/file.py b/file.py\n+print('hello')"
        prompt = build_commit_prompt(diff=diff, branch=None)
        assert "conventional commit message" in prompt.lower()
        assert diff in prompt
        assert "branch" not in prompt.lower()

    def test_prompt_with_branch_includes_scope_instruction(self):
        diff = "some diff"
        branch = "feature/login"
        prompt = build_commit_prompt(diff=diff, branch=branch)
        assert "feature/login" in prompt
        assert "scope" in prompt.lower()

    def test_prompt_with_scope_hint(self):
        diff = "diff content"
        prompt = build_commit_prompt(diff=diff, branch=None, scope_hint="ui")
        assert "scope 'ui'" in prompt.lower()

    def test_prompt_includes_diff_unchanged(self):
        diff = "diff content with special chars: @@@ &&&"
        prompt = build_commit_prompt(diff=diff, branch="main")
        assert diff in prompt

    def test_prompt_contains_format_specification(self):
        prompt = build_commit_prompt("diff", None)
        assert "format:" in prompt.lower() or "type:" in prompt.lower()

    def test_no_branch_section_if_none(self):
        prompt = build_commit_prompt("diff", branch=None)
        assert "branch:" not in prompt.lower()

    def test_handles_empty_diff(self):
        prompt = build_commit_prompt("", branch="main")
        assert prompt  # should not raise
        assert "diff" in prompt.lower()