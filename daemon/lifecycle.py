"""Daemon lifecycle: manages file watchers, background tasks, and state."""

import logging
import asyncio
import threading
from pathlib import Path
from typing import Dict, Optional, Any

from gitpilot.infrastructure.db import managed_connection
from gitpilot.infrastructure.repositories.projects import ProjectsRepository
from gitpilot.core.watcher import WatcherService  # will be defined later
from gitpilot.core.executor import GitExecutor
from gitpilot.core.committer import AICommitter
from gitpilot.domain.policies import get_current_os_user

logger = logging.getLogger("gitpilot.lifecycle")


class DaemonLifecycle:
    """Manages watcher threads, AI committer, and executor."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.watcher: Optional[WatcherService] = None
        self.executor = GitExecutor(
            max_retries=config.get("max_commit_retries", 3),
            github_token=config.get("github_token"),
        )
        # Initialize AI committer with config
        ai_provider = config.get("ai_provider", "grok")
        ai_model = config.get("ai_model", "grok-2")
        temperature = float(config.get("ai_temperature", 0.5))
        self.committer = AICommitter(
            provider=ai_provider,
            model=ai_model,
            temperature=temperature,
            grok_api_key=config.get("grok_api_key"),
            openai_api_key=config.get("openai_api_key"),
            anthropic_api_key=config.get("anthropic_api_key"),
            ollama_base_url=config.get("ollama_base_url", "http://localhost:11434"),
            ollama_model=config.get("ollama_model", "llama3"),
        )
        self._watcher_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._db_conn = None  # managed connection for the lifecycle

    def start(self):
        """Start the file watcher and other background tasks."""
        logger.info("Starting daemon lifecycle")
        # Reload active projects from DB
        owner = get_current_os_user()
        with managed_connection() as conn:
            self._db_conn = conn  # keep connection? We'll use separate connections per request.
            repo = ProjectsRepository(conn)
            projects, _ = repo.list_all(owner, limit=1000)  # get all non-deleted

        if not projects:
            logger.warning("No active projects to watch")
            return

        self.watcher = WatcherService(
            executor=self.executor,
            committer=self.committer,
            debounce_interval=int(self.config.get("debounce_interval", 3)),
            smart_grouping=bool(self.config.get("smart_grouping", True)),
        )

        # Add all projects to the watcher
        for proj in projects:
            proj_path = Path(proj["path"])
            if proj_path.exists() and proj_path.is_dir():
                self.watcher.add_project(proj_path, proj["id"])
                logger.info("Watching project %d: %s", proj["id"], proj_path)
            else:
                logger.warning("Project %d path does not exist: %s", proj["id"], proj_path)

        # Start the watchdog observer in a background thread
        self._watcher_thread = threading.Thread(
            target=self.watcher.start,
            daemon=True,
            name="watcher-thread",
        )
        self._watcher_thread.start()
        logger.info("File watcher started")

    def stop(self):
        """Stop the file watcher and clean up resources."""
        logger.info("Stopping daemon lifecycle")
        self._stop_event.set()
        if self.watcher:
            self.watcher.stop()
        if self._watcher_thread and self._watcher_thread.is_alive():
            self._watcher_thread.join(timeout=5)
        logger.info("Daemon lifecycle stopped")

    def add_project(self, project_id: int, path: str) -> None:
        """Add a project to the watcher dynamically."""
        if self.watcher:
            self.watcher.add_project(Path(path), project_id)
            logger.info("Dynamic add project %d: %s", project_id, path)

    def remove_project(self, project_id: int, path: str) -> None:
        """Remove a project from the watcher dynamically."""
        if self.watcher:
            self.watcher.remove_project(Path(path))
            logger.info("Dynamic remove project %d: %s", project_id, path)