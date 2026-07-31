"""File watching service with debounce, AI‑powered grouping, domain isolation,
   optimization hints, file association learning, and behavior pattern tracking."""

import asyncio
import hashlib
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from gitpilot.core import git_utils
from gitpilot.core.executor import GitExecutor
from gitpilot.core.committer import AICommitter
from gitpilot.core.notifications import send_discord_notification
from gitpilot.core.intelligence import (
    DomainClassifier,
    CommitSplitter,
    OptimizationScanner,
)
from gitpilot.infrastructure.db import managed_connection
from gitpilot.infrastructure.repositories.commits import CommitsRepository
from gitpilot.infrastructure.repositories.discord_webhooks import DiscordWebhooksRepository
from gitpilot.infrastructure.repositories.file_associations import FileAssociationsRepository
from gitpilot.infrastructure.repositories.patterns import PatternsRepository
from gitpilot.domain.policies import get_current_os_user

logger = logging.getLogger("gitpilot.watcher")


class FileHashCache:
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
    def __init__(self):
        self._changes: Set[Path] = set()
        self._last_event_time = time.time()

    def add(self, path: Path) -> None:
        self._changes.add(path)
        self._last_event_time = time.time()

    def reset(self) -> List[Path]:
        changes = sorted(self._changes)
        self._changes.clear()
        return changes

    @property
    def size(self) -> int:
        return len(self._changes)

    @property
    def last_event(self) -> float:
        return self._last_event_time


class ProjectWatcher:
    """Watches a single project directory, uses AI to group related changes,
       commits each group separately, learns associations and patterns."""

    def __init__(
        self,
        project_path: Path,
        project_id: int,
        executor: GitExecutor,
        committer: AICommitter,
        debounce_interval: float = 120.0,
        enable_splitting: bool = True,
        enable_ai_grouping: bool = True,
        enable_optimizations: bool = False,
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
        self.enable_splitting = enable_splitting
        self.enable_ai_grouping = enable_ai_grouping
        self.enable_optimizations = enable_optimizations
        self.branch_aware = branch_aware
        self.discord_webhook_enabled = discord_webhook_enabled
        self.on_commit_completed = on_commit_completed
        self.on_push_failed = on_push_failed
        self.on_watcher_status = on_watcher_status

        self.accumulator = ChangeAccumulator()
        self.hash_cache = FileHashCache(ttl=60)
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._running = True

        self.domain_classifier = DomainClassifier()
        self.commit_splitter = CommitSplitter(
            classifier=self.domain_classifier,
            enable_splitting=self.enable_splitting,
            ai_committer=self.committer if self.enable_ai_grouping else None,
            project_root=self.project_path,
            use_ai_grouping=self.enable_ai_grouping,
        )

    def handle_change(self, event: FileSystemEvent) -> None:
        if not self._running:
            return
        src_path = Path(event.src_path)
        if self._is_git_path(src_path):
            return
        if src_path.name.endswith(("~", ".swp", ".swx", ".tmp", ".bak")):
            return
        if not src_path.exists():
            return
        if src_path.is_dir():
            return

        try:
            content = src_path.read_bytes()
            new_hash = hashlib.md5(content).hexdigest()
        except OSError:
            return

        old_hash = self.hash_cache.get_hash(str(src_path))
        if old_hash == new_hash:
            return
        self.hash_cache.set_hash(str(src_path), new_hash)

        self._prefetch_associated_files(src_path)

        with self._lock:
            self.accumulator.add(src_path)
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_interval, self._on_debounce_expired)
            self._timer.daemon = True
            self._timer.start()

    def _is_git_path(self, path: Path) -> bool:
        try:
            path.relative_to(self.project_path / ".git")
            return True
        except ValueError:
            return False

    def _prefetch_associated_files(self, file_path: Path) -> None:
        try:
            with managed_connection() as conn:
                repo = FileAssociationsRepository(conn)
                associated = repo.get_associated_files(
                    project_id=self.project_id,
                    file_path=str(file_path.relative_to(self.project_path)),
                    min_occurrences=2,
                    max_results=5,
                )
            for other_file, _ in associated:
                full_path = self.project_path / other_file
                if full_path.exists():
                    if not self.hash_cache.get_hash(str(full_path)):
                        try:
                            content = full_path.read_bytes()
                            self.hash_cache.set_hash(str(full_path), hashlib.md5(content).hexdigest())
                        except OSError:
                            pass
        except Exception as exc:
            logger.debug("Predictive fetch failed: %s", exc)

    def _on_debounce_expired(self) -> None:
        with self._lock:
            pass
        self._process_actual_changes()

    def _process_actual_changes(self) -> None:
        changes = git_utils.get_porcelain_status(self.project_path)
        if not changes:
            return

        rel_paths: List[Path] = []
        for c in changes:
            abs_path = self.project_path / c.file_path
            if c.is_deleted:
                rel_paths.append(c.file_path)
            elif abs_path.exists():
                rel_paths.append(c.file_path)

        if not rel_paths:
            return

        # Use AI‑powered grouping or domain split
        commit_plan = self.commit_splitter.commit_plan(
            files=[self.project_path / p for p in rel_paths],
            project_root=self.project_path,
            project_id=self.project_id,
        )

        for plan_item in commit_plan:
            try:
                self._commit_domain_group(
                    rel_paths=[self.project_path / p for p in plan_item["files"]],
                    domain=plan_item.get("domain", "general"),
                    suggested_scope=plan_item.get("suggested_scope", "misc"),
                )
            except Exception as exc:
                logger.exception(
                    "Failed to process commit group %s: %s",
                    plan_item.get("domain", "?"), exc,
                )

    def _commit_domain_group(
        self,
        rel_paths: List[Path],
        domain: str,
        suggested_scope: str,
    ) -> None:
        if not git_utils.reset_index(self.project_path):
            return
        if not git_utils.stage_specific_files(self.project_path, rel_paths):
            return

        diff = git_utils.get_staged_diff(self.project_path)
        if not diff or diff.strip() == "":
            return

        branch = None
        if self.branch_aware:
            branch = git_utils.get_current_branch(self.project_path)

        optimization_notes = []
        if self.enable_optimizations:
            optimization_notes = OptimizationScanner.scan_diff(diff)

        message = None
        try:
            context_diff = diff
            if optimization_notes:
                context_diff += "\n\nOptimization notes:\n" + "\n".join(optimization_notes)
            message = asyncio.run(self.committer.generate_message(
                diff=context_diff,
                branch=branch,
                scope_hint=suggested_scope,
            ))
        except Exception as exc:
            logger.error("AI message generation error: %s", exc)

        if not message:
            file_names = [f.name for f in rel_paths]
            message = f"update({suggested_scope}): {', '.join(file_names[:3])}"
            if len(file_names) > 3:
                message += f" and {len(file_names)-3} more"

        if optimization_notes:
            message += "\n\nOptimization notes:\n" + "\n".join(f"- {n}" for n in optimization_notes)

        commit_hash = self.executor.commit(self.project_path, message)
        if not commit_hash:
            return

        logger.info("Committed [%s] %s: %s", domain, commit_hash[:8], message)

        try:
            with managed_connection() as conn:
                repo = CommitsRepository(conn)
                repo.create(
                    project_id=self.project_id,
                    hash=commit_hash,
                    message=message,
                    branch=branch or "main",
                    domain=domain,
                    affected_symbols=[],
                    optimization_notes=optimization_notes,
                    committed_at=datetime.now(timezone.utc).isoformat(),
                )
                repo.mark_squash_candidates(
                    project_id=self.project_id,
                    branch=branch or "main",
                    domain=domain,
                    max_age_minutes=10,
                )
        except Exception as exc:
            logger.exception("Failed to log commit to DB: %s", exc)

        self._learn_file_associations(rel_paths)
        self._learn_behavior_patterns(message, branch or "main", domain)

        push_success, push_error = asyncio.run(self.executor.push_with_retry(self.project_path))
        if not push_success:
            logger.error("Push failed: %s", push_error)
            if self.on_push_failed:
                self.on_push_failed(self.project_id, push_error)
        else:
            if self.on_commit_completed:
                self.on_commit_completed(
                    project_id=self.project_id,
                    commit_hash=commit_hash,
                    message=message,
                    branch=branch or "main",
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )

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

        if self.on_watcher_status:
            self.on_watcher_status(
                project_id=self.project_id,
                status="monitoring",
                pending_changes=0,
                last_event=datetime.now(timezone.utc).isoformat(),
            )

    def _learn_file_associations(self, files: List[Path]) -> None:
        try:
            with managed_connection() as conn:
                repo = FileAssociationsRepository(conn)
                rel_paths = []
                for f in files:
                    try:
                        rel = str(f.relative_to(self.project_path))
                    except ValueError:
                        rel = str(f)
                    rel_paths.append(rel)
                for i in range(len(rel_paths)):
                    for j in range(i + 1, len(rel_paths)):
                        repo.record_co_occurrence(self.project_id, rel_paths[i], rel_paths[j])
        except Exception as exc:
            logger.debug("Failed to learn file associations: %s", exc)

    def _learn_behavior_patterns(self, message: str, branch: str, domain: str) -> None:
        try:
            with managed_connection() as conn:
                repo = PatternsRepository(conn)
                owner = get_current_os_user()
                style_value = "conventional" if ":" in message else "descriptive"
                repo.upsert(
                    owner=owner,
                    pattern_type="message_style",
                    value=style_value,
                    confidence=0.5,
                )
                if "/" in branch:
                    prefix = branch.split("/")[0]
                    repo.upsert(
                        owner=owner,
                        pattern_type="branch_prefix",
                        value=prefix,
                        confidence=0.5,
                    )
        except Exception as exc:
            logger.debug("Failed to learn behavior patterns: %s", exc)

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
        debounce_interval: float = 120.0,
        enable_splitting: bool = True,
        enable_ai_grouping: bool = True,
        enable_optimizations: bool = False,
        discord_webhook_enabled: bool = False,
        on_commit_completed: Optional[Callable] = None,
        on_push_failed: Optional[Callable] = None,
        on_watcher_status: Optional[Callable] = None,
    ):
        self.executor = executor
        self.committer = committer
        self.debounce_interval = debounce_interval
        self.enable_splitting = enable_splitting
        self.enable_ai_grouping = enable_ai_grouping
        self.enable_optimizations = enable_optimizations
        self.discord_webhook_enabled = discord_webhook_enabled
        self.on_commit_completed = on_commit_completed
        self.on_push_failed = on_push_failed
        self.on_watcher_status = on_watcher_status

        self.observer = Observer()
        self._watchers: Dict[Path, ProjectWatcher] = {}
        self._handler_map: Dict[Path, FileSystemEventHandler] = {}
        self._watched_paths: Set[Path] = set()

    def add_project(self, project_path: Path, project_id: int) -> None:
        if project_path in self._watched_paths:
            logger.warning("Already watching %s", project_path)
            return

        if not project_path.exists() or not project_path.is_dir():
            logger.error("Cannot watch non-existent directory: %s", project_path)
            return

        project_watcher = ProjectWatcher(
            project_path=project_path,
            project_id=project_id,
            executor=self.executor,
            committer=self.committer,
            debounce_interval=self.debounce_interval,
            enable_splitting=self.enable_splitting,
            enable_ai_grouping=self.enable_ai_grouping,
            enable_optimizations=self.enable_optimizations,
            branch_aware=True,
            discord_webhook_enabled=self.discord_webhook_enabled,
            on_commit_completed=self.on_commit_completed,
            on_push_failed=self.on_push_failed,
            on_watcher_status=self.on_watcher_status,
        )

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
        if project_path not in self._watched_paths:
            return
        handler = self._handler_map.pop(project_path, None)
        if handler:
            self.observer.unschedule(handler)
        self._watchers.pop(project_path, None)
        self._watched_paths.discard(project_path)
        logger.info("Stopped watching %s", project_path)

    def start(self) -> None:
        if self.observer.is_alive():
            return
        self.observer.start()
        logger.info("WatcherService started (observer running)")
        while self.observer.is_alive():
            self.observer.join(timeout=1)

    def stop(self) -> None:
        logger.info("Stopping WatcherService")
        for pw in self._watchers.values():
            pw.stop()
        self.observer.stop()
        self.observer.join(timeout=5)
        logger.info("WatcherService stopped")