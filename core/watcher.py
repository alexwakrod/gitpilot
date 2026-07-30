"""File watching service with debounce and smart grouping."""

import fnmatch
import hashlib
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from gitpilot.core.executor import GitExecutor
from gitpilot.core.committer import AICommitter
from gitpilot.core.notifications import send_discord_notification
from gitpilot.infrastructure.db import managed_connection
from gitpilot.infrastructure.repositories.commits import CommitsRepository
from gitpilot.infrastructure.repositories.discord_webhooks import DiscordWebhooksRepository

logger = logging.getLogger("gitpilot.watcher")


class FileHashCache:
    """In‑memory mapping of file paths to MD5 hashes with TTL."""

    def __init__(self, ttl: int = 60):
        self._cache: Dict[str, Tuple[str, float]] = {}
        self._ttl = ttl

    def set_hash(self, file_path: str, file_hash: str) -> None:
        self._cache[file_path] = (file_hash, time.time())

    def get_hash(self, file_path: str) -> Optional[str]:
        entry = self._cache.get(file_path)
        if entry is None:
            return None
        file_hash, timestamp = entry
        if time.time() - timestamp > self._ttl:
            del self._cache[file_path]
            return None
        return file_hash

    def invalidate(self, file_path: str) -> None:
        self._cache.pop(file_path, None)


class ChangeAccumulator:
    """Accumulates file changes during a debounce window, supporting smart grouping."""

    def __init__(self, smart_grouping: bool = True):
        self.smart_grouping = smart_grouping
        self._changes: Set[Path] = set()
        self._last_event_time = time.time()

    def add(self, path: Path) -> None:
        self._changes.add(path)
        self._last_event_time = time.time()

    def reset(self) -> List[Path]:
        """Return current changes and clear for next window."""
        changes = sorted(self._changes)
        self._changes.clear()
        return changes

    @property
    def size(self) -> int:
        return len(self._changes)

    @property
    def last_event(self) -> float:
        return self._last_event_time

    def group_changes(self) -> List[List[Path]]:
        """Group changes by logical relatedness (same subdirectory, similar names)."""
        if not self.smart_grouping or len(self._changes) <= 1:
            return [self.reset()]

        changes = list(self._changes)
        groups: List[List[Path]] = []
        used = set()

        # Group by common parent directory
        dir_map: Dict[Path, List[Path]] = defaultdict(list)
        for p in changes:
            dir_map[p.parent].append(p)
        for parent, files in dir_map.items():
            if len(files) > 1:
                groups.append(files)
                used.update(files)

        # Remaining singletons: try grouping by naming pattern (file stem similarity)
        remaining = [p for p in changes if p not in used]
        if remaining:
            # Group by stem prefix (e.g., test_file_a, test_file_b)
            stem_map: Dict[str, List[Path]] = defaultdict(list)
            for p in remaining:
                stem = p.stem
                # Use first segment before underscore or dash as group key
                key = stem.split("_")[0].split("-")[0].lower()
                stem_map[key].append(p)
            for key_files in stem_map.values():
                if len(key_files) > 1:
                    groups.append(key_files)
                    used.update(key_files)

            # Any remaining singletons form their own groups
            for p in remaining:
                if p not in used:
                    groups.append([p])

        self._changes.clear()
        return groups


class ProjectWatcher:
    """Watches a single project directory for file system events."""

    def __init__(
        self,
        project_path: Path,
        project_id: int,
        executor: GitExecutor,
        committer: AICommitter,
        debounce_interval: float = 3.0,
        smart_grouping: bool = True,
        branch_aware: bool = True,
        discord_webhook_enabled: bool = False,
        on_commit_completed: Optional[Callable] = None,
        on_push_failed: Optional[Callable] = None,
        on_watcher_status: Optional[Callable] = None,
    ):
        self.project_path = project_path
        self.project_id = project_id
        self.executor = executor
        self.committer = committer
        self.debounce_interval = debounce_interval
        self.smart_grouping = smart_grouping
        self.branch_aware = branch_aware
        self.discord_webhook_enabled = discord_webhook_enabled
        self.on_commit_completed = on_commit_completed
        self.on_push_failed = on_push_failed
        self.on_watcher_status = on_watcher_status

        self.accumulator = ChangeAccumulator(smart_grouping=smart_grouping)
        self.hash_cache = FileHashCache(ttl=60)
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._running = True

    def handle_change(self, event: FileSystemEvent) -> None:
        """Called by watchdog observer on any file system event."""
        if not self._running:
            return

        src_path = Path(event.src_path)
        # Ignore .git directory changes
        if self._is_git_path(src_path):
            return
        # Ignore temporary/editor swap files
        if src_path.name.endswith(("~", ".swp", ".swx", ".tmp", ".bak")):
            return
        if not src_path.exists():
            return
        # Avoid acting on directory modifications
        if src_path.is_dir():
            return

        # Compute hash to skip duplicate events for same content
        try:
            content = src_path.read_bytes()
            new_hash = hashlib.md5(content).hexdigest()
        except OSError:
            return

        old_hash = self.hash_cache.get_hash(str(src_path))
        if old_hash == new_hash:
            return  # file content unchanged, ignore duplicate event
        self.hash_cache.set_hash(str(src_path), new_hash)

        with self._lock:
            self.accumulator.add(src_path)
            # Reset debounce timer
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_interval, self._on_debounce_expired)
            self._timer.daemon = True
            self._timer.start()

    def _is_git_path(self, path: Path) -> bool:
        """Check if a path is inside the .git directory."""
        try:
            path.relative_to(self.project_path / ".git")
            return True
        except ValueError:
            return False

    def _on_debounce_expired(self) -> None:
        """Called when the debounce timer fires, triggering commit processing."""
        with self._lock:
            if self.accumulator.size == 0:
                return
            groups = self.accumulator.group_changes()
            # Release lock before heavy I/O
        self._process_groups(groups)

    def _process_groups(self, groups: List[List[Path]]) -> None:
        """For each group, stage, diff, generate commit message, commit, and push."""
        for group in groups:
            try:
                self._process_single_group(group)
            except Exception as exc:
                logger.exception("Failed to process change group for project %d: %s", self.project_id, exc)

    def _process_single_group(self, paths: List[Path]) -> None:
        """Process one logical group of changed files."""
        # Stage all changed files (git add all within the repo, but we could scope)
        # For simplicity, stage everything; our diff will include all pending changes anyway.
        if not self.executor.stage_all(self.project_path):
            logger.error("Failed to stage changes for project %d", self.project_id)
            return

        diff = self.executor.get_diff_cached(self.project_path)
        if not diff or diff.strip() == "":
            logger.info("Empty diff after staging for project %d, skipping commit", self.project_id)
            return

        branch = None
        if self.branch_aware:
            branch = self.executor.get_current_branch(self.project_path)

        # Generate commit message via AI (with fallback)
        message = None
        try:
            message = asyncio.run(self.committer.generate_message(diff, branch))
        except Exception as exc:
            logger.error("AI message generation error for project %d: %s", self.project_id, exc)

        if not message:
            # Fallback message based on file names
            file_names = [p.name for p in paths]
            message = f"update: {', '.join(file_names[:3])}"
            if len(file_names) > 3:
                message += f" and {len(file_names)-3} more"

        # Commit
        commit_hash = self.executor.commit(self.project_path, message)
        if not commit_hash:
            logger.error("Commit failed for project %d", self.project_id)
            return

        logger.info("Committed %s: %s", commit_hash[:8], message)

        # Record commit in DB
        try:
            with managed_connection() as conn:
                repo = CommitsRepository(conn)
                repo.create(
                    project_id=self.project_id,
                    hash=commit_hash,
                    message=message,
                    branch=branch or "main",
                    committed_at=datetime.now(timezone.utc).isoformat(),
                )
        except Exception as exc:
            logger.exception("Failed to log commit to DB: %s", exc)

        # Push with retry
        push_success, push_error = asyncio.run(self.executor.push_with_retry(self.project_path))
        if not push_success:
            logger.error("Push failed for project %d: %s", self.project_id, push_error)
            if self.on_push_failed:
                self.on_push_failed(self.project_id, push_error)
        else:
            # Send commit_completed event
            if self.on_commit_completed:
                self.on_commit_completed(
                    project_id=self.project_id,
                    commit_hash=commit_hash,
                    message=message,
                    branch=branch or "main",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )

        # Discord notification
        if self.discord_webhook_enabled:
            try:
                with managed_connection() as conn:
                    webhooks_repo = DiscordWebhooksRepository(conn)
                    webhooks = webhooks_repo.list_by_project(self.project_id)
                for wh in webhooks:
                    asyncio.run(
                        send_discord_notification(
                            webhook_url=wh["url"],
                            project_name=str(self.project_path.name),
                            commit_hash=commit_hash,
                            message=message,
                            branch=branch or "main",
                            timestamp=datetime.now(timezone.utc).isoformat(),
                        )
                    )
            except Exception as exc:
                logger.exception("Discord notification failed: %s", exc)

        # Watcher status
        if self.on_watcher_status:
            self.on_watcher_status(
                project_id=self.project_id,
                status="monitoring",
                pending_changes=self.accumulator.size,
                last_event=datetime.now(timezone.utc).isoformat(),
            )

    def stop(self) -> None:
        self._running = False
        if self._timer:
            self._timer.cancel()


class WatcherService:
    """Manages multiple ProjectWatcher instances and the watchdog Observer."""

    def __init__(
        self,
        executor: GitExecutor,
        committer: AICommitter,
        debounce_interval: float = 3.0,
        smart_grouping: bool = True,
        discord_webhook_enabled: bool = False,
        on_commit_completed: Optional[Callable] = None,
        on_push_failed: Optional[Callable] = None,
        on_watcher_status: Optional[Callable] = None,
    ):
        self.executor = executor
        self.committer = committer
        self.debounce_interval = debounce_interval
        self.smart_grouping = smart_grouping
        self.discord_webhook_enabled = discord_webhook_enabled
        self.on_commit_completed = on_commit_completed
        self.on_push_failed = on_push_failed
        self.on_watcher_status = on_watcher_status

        self.observer = Observer()
        self._watchers: Dict[Path, ProjectWatcher] = {}
        self._handler_map: Dict[Path, FileSystemEventHandler] = {}
        self._watched_paths: Set[Path] = set()

    def add_project(self, project_path: Path, project_id: int) -> None:
        """Add a project directory to the watchdog."""
        if project_path in self._watched_paths:
            logger.warning("Already watching %s", project_path)
            return

        # Ensure directory exists
        if not project_path.exists() or not project_path.is_dir():
            logger.error("Cannot watch non-existent directory: %s", project_path)
            return

        project_watcher = ProjectWatcher(
            project_path=project_path,
            project_id=project_id,
            executor=self.executor,
            committer=self.committer,
            debounce_interval=self.debounce_interval,
            smart_grouping=self.smart_grouping,
            branch_aware=True,
            discord_webhook_enabled=self.discord_webhook_enabled,
            on_commit_completed=self.on_commit_completed,
            on_push_failed=self.on_push_failed,
            on_watcher_status=self.on_watcher_status,
        )

        # Create a watchdog event handler that delegates to ProjectWatcher
        class Handler(FileSystemEventHandler):
            def __init__(self, pw: ProjectWatcher):
                self.pw = pw

            def on_created(self, event):
                self.pw.handle_change(event)

            def on_modified(self, event):
                self.pw.handle_change(event)

            def on_deleted(self, event):
                self.pw.handle_change(event)

            def on_moved(self, event):
                self.pw.handle_change(event)

        handler = Handler(project_watcher)
        self.observer.schedule(handler, str(project_path), recursive=True)
        self._watchers[project_path] = project_watcher
        self._handler_map[project_path] = handler
        self._watched_paths.add(project_path)
        logger.info("Started watching %s (project %d)", project_path, project_id)

    def remove_project(self, project_path: Path) -> None:
        """Stop watching a project directory."""
        if project_path not in self._watched_paths:
            return
        # Unschedule the handler
        handler = self._handler_map.pop(project_path, None)
        if handler:
            self.observer.unschedule(handler)
        self._watchers.pop(project_path, None)
        self._watched_paths.discard(project_path)
        logger.info("Stopped watching %s", project_path)

    def start(self) -> None:
        """Start the watchdog observer (blocking)."""
        if self.observer.is_alive():
            return
        self.observer.start()
        logger.info("WatcherService started (observer running)")
        while self.observer.is_alive():
            self.observer.join(timeout=1)

    def stop(self) -> None:
        """Stop all watchers and the observer."""
        logger.info("Stopping WatcherService")
        # Stop individual project watchers
        for pw in self._watchers.values():
            pw.stop()
        self.observer.stop()
        self.observer.join(timeout=5)
        logger.info("WatcherService stopped")