"""Daemon lifecycle: manages file watchers, AI committer, and startup Git readiness checks."""

import logging
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from gitpilot.infrastructure.db import managed_connection
from gitpilot.infrastructure.repositories.projects import ProjectsRepository
from gitpilot.core.watcher import WatcherService
from gitpilot.core.executor import GitExecutor
from gitpilot.core.committer import AICommitter
from gitpilot.core.project_setup import is_git_repo, has_commits, ensure_initial_commit
from gitpilot.domain.policies import get_current_os_user

logger = logging.getLogger("gitpilot.lifecycle")


class DaemonLifecycle:
    """Manages watcher threads, AI committer, executor, and startup readiness."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.watcher: Optional[WatcherService] = None
        self.executor = GitExecutor(
            max_retries=config.get("max_commit_retries", 3),
            github_token=config.get("github_token"),
        )
        self.committer = self._create_committer(config)
        self._watcher_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.enable_splitting = bool(config.get("enable_splitting", True))
        self.enable_ai_grouping = bool(config.get("enable_ai_grouping", True))
        self.enable_optimizations = bool(config.get("enable_optimizations", False))
        self.on_commit_completed = None
        self.on_push_failed = None
        self.on_watcher_status = None

    def _create_committer(self, config: Dict[str, Any]) -> AICommitter:
        return AICommitter(
            provider=config.get("ai_provider", "grok"),
            model=config.get("ai_model", "grok-2"),
            temperature=float(config.get("ai_temperature", 0.5)),
            grok_api_key=config.get("grok_api_key"),
            groq_api_key=config.get("groq_api_key"),
            qwen_api_key=config.get("qwen_api_key"),
            openai_api_key=config.get("openai_api_key"),
            anthropic_api_key=config.get("anthropic_api_key"),
            ollama_base_url=config.get("ollama_base_url", "http://localhost:11434"),
            ollama_model=config.get("ollama_model", "llama3"),
            groq_model=config.get("groq_model", "llama3-70b-8192"),
            qwen_model=config.get("qwen_model", "qwen-plus"),
        )

    def start(self):
        """Start the file watcher after ensuring Git is ready."""
        logger.info("Starting daemon lifecycle")
        self._verify_global_git_config()
        self._validate_registered_projects()

        self.watcher = WatcherService(
            executor=self.executor,
            committer=self.committer,
            debounce_interval=int(self.config.get("debounce_interval", 120)),
            enable_splitting=self.enable_splitting,
            enable_ai_grouping=self.enable_ai_grouping,
            enable_optimizations=self.enable_optimizations,
            discord_webhook_enabled=bool(self.config.get("discord_webhook_enabled", False)),
            on_commit_completed=self.on_commit_completed,
            on_push_failed=self.on_push_failed,
            on_watcher_status=self.on_watcher_status,
        )

        owner = get_current_os_user()
        with managed_connection() as conn:
            repo = ProjectsRepository(conn)
            projects, _ = repo.list_all(owner, limit=1000)

        for proj in projects:
            proj_path = Path(proj["path"])
            if proj_path.exists() and proj_path.is_dir() and is_git_repo(proj_path):
                self.watcher.add_project(proj_path, proj["id"])
                logger.info("Watching project %d: %s", proj["id"], proj_path)
            else:
                logger.warning("Project %d path invalid or not a git repo: %s", proj["id"], proj_path)

        self._watcher_thread = threading.Thread(
            target=self.watcher.start,
            daemon=True,
            name="watcher-thread",
        )
        self._watcher_thread.start()
        logger.info("File watcher started")

    def _verify_global_git_config(self):
        try:
            name = subprocess.run(
                ["git", "config", "--global", "user.name"],
                capture_output=True, text=True
            ).stdout.strip()
            email = subprocess.run(
                ["git", "config", "--global", "user.email"],
                capture_output=True, text=True
            ).stdout.strip()
            if not name:
                logger.warning("Git global user.name is not set. Commits may fail.")
            if not email:
                logger.warning("Git global user.email is not set. Commits may fail.")
            if name and email:
                logger.info("Git global identity: %s <%s>", name, email)
        except Exception as exc:
            logger.error("Could not verify git global config: %s", exc)

    def _validate_registered_projects(self):
        owner = get_current_os_user()
        with managed_connection() as conn:
            repo = ProjectsRepository(conn)
            projects, _ = repo.list_all(owner, limit=1000)

        for proj in projects:
            proj_path = Path(proj["path"])
            if not proj_path.exists() or not proj_path.is_dir():
                logger.warning("Project %d directory missing: %s", proj["id"], proj_path)
                continue
            if not is_git_repo(proj_path):
                logger.warning("Project %d is not a git repository; attempting to init: %s", proj["id"], proj_path)
                if self.executor.init_repo(proj_path):
                    logger.info("Git repository initialized for project %d", proj["id"])
                else:
                    logger.error("Failed to initialize git repo for project %d; skipping", proj["id"])
                    continue
            if not has_commits(proj_path):
                logger.warning("Project %d has no commits; creating initial commit: %s", proj["id"], proj_path)
                if not ensure_initial_commit(proj_path):
                    logger.error("Failed to create initial commit for project %d; commits may fail", proj["id"])

    def stop(self):
        logger.info("Stopping daemon lifecycle")
        self._stop_event.set()
        if self.watcher:
            self.watcher.stop()
        if self._watcher_thread and self._watcher_thread.is_alive():
            self._watcher_thread.join(timeout=5)
        logger.info("Daemon lifecycle stopped")

    def add_project(self, project_id: int, path: str) -> None:
        if self.watcher:
            self.watcher.add_project(Path(path), project_id)
            logger.info("Dynamic add project %d: %s", project_id, path)

    def remove_project(self, project_id: int, path: str) -> None:
        if self.watcher:
            self.watcher.remove_project(Path(path))
            logger.info("Dynamic remove project %d: %s", project_id, path)

    def reload_config(self, new_config: Dict[str, Any]) -> None:
        """Reload configuration without restarting the daemon.
        Updates both the lifecycle flags and the shared AI committer."""
        self.config = new_config
        # Update committer in-place (since it's shared with watcher)
        self.committer.provider = new_config.get("ai_provider", self.committer.provider)
        self.committer.model = new_config.get("ai_model", self.committer.model)
        self.committer.grok_api_key = new_config.get("grok_api_key")
        self.committer.groq_api_key = new_config.get("groq_api_key")
        self.committer.qwen_api_key = new_config.get("qwen_api_key")
        self.committer.openai_api_key = new_config.get("openai_api_key")
        self.committer.anthropic_api_key = new_config.get("anthropic_api_key")
        self.committer.ollama_base_url = new_config.get("ollama_base_url", self.committer.ollama_base_url)
        self.committer.ollama_model = new_config.get("ollama_model", self.committer.ollama_model)
        self.committer.groq_model = new_config.get("groq_model", self.committer.groq_model)
        self.committer.qwen_model = new_config.get("qwen_model", self.committer.qwen_model)

        # Update watcher-level flags
        self.enable_splitting = bool(new_config.get("enable_splitting", self.enable_splitting))
        self.enable_ai_grouping = bool(new_config.get("enable_ai_grouping", self.enable_ai_grouping))
        self.enable_optimizations = bool(new_config.get("enable_optimizations", self.enable_optimizations))

        if self.watcher:
            for pw in self.watcher._watchers.values():
                pw.enable_splitting = self.enable_splitting
                pw.enable_ai_grouping = self.enable_ai_grouping
                pw.enable_optimizations = self.enable_optimizations
                pw.commit_splitter.enable_splitting = pw.enable_splitting
                # The AI grouper uses self.committer which is already updated

        logger.info("Configuration reloaded successfully")