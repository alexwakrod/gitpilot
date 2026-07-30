"""FastAPI application with all API endpoints and SSE streaming."""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import StreamingResponse

from gitpilot.domain.models import (
    CommitCreate,
    CommitListResponse,
    CommitResponse,
    CommitUpdate,
    DiscordWebhookCreate,
    DiscordWebhookResponse,
    ProjectCreate,
    ProjectListResponse,
    ProjectResponse,
    ProjectUpdate,
    SettingCreate,
    SettingListResponse,
    SettingResponse,
    SettingUpdate,
)
from gitpilot.domain.policies import get_current_os_user, verify_owner
from gitpilot.domain.settings import SettingsManager
from gitpilot.infrastructure.db import managed_connection
from gitpilot.infrastructure.repositories.projects import ProjectsRepository
from gitpilot.infrastructure.repositories.commits import CommitsRepository
from gitpilot.infrastructure.repositories.settings_repo import SettingsRepository
from gitpilot.infrastructure.repositories.discord_webhooks import DiscordWebhooksRepository
from gitpilot.core.executor import GitExecutor
from gitpilot.core.committer import AICommitter
from gitpilot.daemon.lifecycle import DaemonLifecycle

logger = logging.getLogger("gitpilot.api")

# In-memory event queues for SSE clients
sse_clients: Dict[str, List[asyncio.Queue]] = {}
sse_lock = asyncio.Lock()


def create_app(
    api_token: str,
    lifecycle: DaemonLifecycle,
    config: Dict[str, Any],
) -> FastAPI:
    """Create and configure the FastAPI application."""

    app = FastAPI(
        title="GitPilot Daemon",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
    )

    security = HTTPBearer()

    async def validate_token(
        credentials: HTTPAuthorizationCredentials = Depends(security),
    ) -> str:
        """Validate the Bearer token and return the owner OS username."""
        token = credentials.credentials
        if not verify_owner(get_current_os_user(), token):
            raise HTTPException(status_code=403, detail="Invalid token")
        return get_current_os_user()

    # ------------------------------------------------------------------
    # Event callbacks for watcher -> SSE
    # ------------------------------------------------------------------
    def _on_commit_completed(
        project_id: int,
        commit_hash: str,
        message: str,
        branch: str,
        timestamp: str,
    ) -> None:
        event = {
            "event": "commit_completed",
            "data": {
                "project_id": project_id,
                "commit_hash": commit_hash,
                "message": message,
                "branch": branch,
                "timestamp": timestamp,
            },
        }
        asyncio.create_task(_broadcast_event(event))

    def _on_push_failed(project_id: int, error: str) -> None:
        event = {
            "event": "push_failed",
            "data": {
                "project_id": project_id,
                "error": error,
                "retry_count": 3,  # TODO: store actual retry count
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }
        asyncio.create_task(_broadcast_event(event))

    def _on_watcher_status(
        project_id: int,
        status: str,
        pending_changes: int,
        last_event: str,
    ) -> None:
        event = {
            "event": "watcher_status",
            "data": {
                "project_id": project_id,
                "status": status,
                "pending_changes": pending_changes,
                "last_event": last_event,
            },
        }
        asyncio.create_task(_broadcast_event(event))

    # Wire up callbacks to lifecycle's watcher
    # (The watcher is created in lifecycle.start; we set them before start)
    lifecycle.on_commit_completed = _on_commit_completed
    lifecycle.on_push_failed = _on_push_failed
    lifecycle.on_watcher_status = _on_watcher_status

    async def _broadcast_event(event: Dict[str, Any]) -> None:
        """Push an SSE event to all connected clients."""
        async with sse_lock:
            for queues in sse_clients.values():
                for q in queues:
                    try:
                        q.put_nowait(event)
                    except asyncio.QueueFull:
                        pass

    # ------------------------------------------------------------------
    # Projects endpoints
    # ------------------------------------------------------------------
    @app.get("/api/v1/projects", response_model=ProjectListResponse)
    async def list_projects(
        limit: int = Query(20, ge=1, le=100),
        cursor: Optional[str] = None,
        owner: str = Depends(validate_token),
    ):
        with managed_connection() as conn:
            repo = ProjectsRepository(conn)
            items, next_cursor = repo.list_all(owner, limit=limit, cursor=cursor)
        return ProjectListResponse(
            items=[ProjectResponse(**item) for item in items],
            next_cursor=next_cursor,
            total=len(items),
        )

    @app.get("/api/v1/projects/{project_id}", response_model=ProjectResponse)
    async def get_project(project_id: int, owner: str = Depends(validate_token)):
        with managed_connection() as conn:
            repo = ProjectsRepository(conn)
            project = repo.get_by_id(project_id)
        if not project or project["owner"] != owner:
            raise HTTPException(status_code=404, detail="Project not found")
        return ProjectResponse(**project)

    @app.post("/api/v1/projects", response_model=ProjectResponse, status_code=201)
    async def create_project(
        body: ProjectCreate,
        owner: str = Depends(validate_token),
    ):
        path = Path(body.path)
        if not path.exists():
            raise HTTPException(status_code=400, detail="Directory does not exist")
        if not path.is_dir():
            raise HTTPException(status_code=400, detail="Path is not a directory")

        with managed_connection() as conn:
            repo = ProjectsRepository(conn)
            # Check for duplicate path
            existing, _ = repo.list_all(owner, limit=100)
            for p in existing:
                if p["path"] == str(path):
                    raise HTTPException(status_code=409, detail="Project path already registered")
            project_id = repo.create(name=body.name, path=str(path), owner=owner)

        # Initialize git if needed
        git_executor = lifecycle.executor
        if not (path / ".git").exists():
            if not git_executor.init_repo(path):
                raise HTTPException(status_code=500, detail="Failed to initialize git repository")

        # If GitHub repo name provided, attempt to create remote
        if body.github_repo_name:
            github_token = config.get("github_token")
            if github_token:
                try:
                    import httpx
                    resp = httpx.post(
                        "https://api.github.com/user/repos",
                        headers={
                            "Authorization": f"token {github_token}",
                            "Accept": "application/vnd.github.v3+json",
                        },
                        json={
                            "name": body.github_repo_name,
                            "private": True,
                            "auto_init": False,
                        },
                        timeout=15.0,
                    )
                    if resp.status_code == 201:
                        repo_data = resp.json()
                        clone_url = repo_data.get("clone_url")
                        if clone_url:
                            git_executor.set_remote_origin(path, clone_url)
                    else:
                        logger.warning("GitHub repo creation failed: %s", resp.text)
                except Exception as exc:
                    logger.warning("GitHub API error: %s", exc)

        # Add to watcher dynamically
        lifecycle.add_project(project_id, str(path))

        with managed_connection() as conn:
            repo = ProjectsRepository(conn)
            project = repo.get_by_id(project_id)
        if not project:
            raise HTTPException(status_code=500, detail="Project created but not found")
        return ProjectResponse(**project)

    @app.put("/api/v1/projects/{project_id}", response_model=ProjectResponse)
    async def update_project(
        project_id: int,
        body: ProjectUpdate,
        owner: str = Depends(validate_token),
    ):
        with managed_connection() as conn:
            repo = ProjectsRepository(conn)
            project = repo.get_by_id(project_id)
            if not project or project["owner"] != owner:
                raise HTTPException(status_code=404, detail="Project not found")

            # Validate new path if provided
            if body.path:
                new_path = Path(body.path)
                if not new_path.exists() or not new_path.is_dir():
                    raise HTTPException(status_code=400, detail="New path does not exist or is not a directory")
                # Check duplicate
                existing, _ = repo.list_all(owner, limit=100)
                for p in existing:
                    if p["id"] != project_id and p["path"] == str(new_path):
                        raise HTTPException(status_code=409, detail="Path already registered")

            update_kwargs = {k: v for k, v in body.dict(exclude_none=True).items() if v is not None}
            if update_kwargs:
                repo.update(project_id, **update_kwargs)
            project = repo.get_by_id(project_id)
        return ProjectResponse(**project)

    @app.delete("/api/v1/projects/{project_id}", status_code=200)
    async def delete_project(project_id: int, owner: str = Depends(validate_token)):
        with managed_connection() as conn:
            repo = ProjectsRepository(conn)
            project = repo.get_by_id(project_id)
            if not project or project["owner"] != owner:
                raise HTTPException(status_code=404, detail="Project not found")
            repo.soft_delete(project_id)
        # Stop watching
        lifecycle.remove_project(project_id, project["path"])
        return {"detail": "Project deleted"}

    # ------------------------------------------------------------------
    # Configuration endpoints
    # ------------------------------------------------------------------
    @app.get("/api/v1/config", response_model=SettingListResponse)
    async def get_config(owner: str = Depends(validate_token)):
        with managed_connection() as conn:
            repo = SettingsRepository(conn)
            all_settings = repo.get_all()
        items = [
            SettingResponse(
                id=0,  # id not relevant for list
                key=key,
                value=json.dumps(val["value"]),
                type=val["type"],
                updated_at=val["updated_at"],
            )
            for key, val in all_settings.items()
        ]
        return SettingListResponse(items=items)

    @app.get("/api/v1/config/{key}", response_model=SettingResponse)
    async def get_config_key(key: str, owner: str = Depends(validate_token)):
        with managed_connection() as conn:
            repo = SettingsRepository(conn)
            setting = repo.get_by_key(key)
        if not setting:
            raise HTTPException(status_code=404, detail="Setting not found")
        return SettingResponse(
            id=0,
            key=setting["key"],
            value=json.dumps(setting["value"]),
            type=setting["type"],
            updated_at=setting["updated_at"],
        )

    @app.post("/api/v1/config", response_model=SettingResponse, status_code=201)
    async def create_config(body: SettingCreate, owner: str = Depends(validate_token)):
        with managed_connection() as conn:
            repo = SettingsRepository(conn)
            existing = repo.get_by_key(body.key)
            if existing:
                raise HTTPException(status_code=409, detail="Setting key already exists")
            # Parse value according to type
            parsed_value = _parse_setting_value(body.value, body.type)
            repo.upsert(body.key, parsed_value, body.type)
            setting = repo.get_by_key(body.key)
        if not setting:
            raise HTTPException(status_code=500)
        return SettingResponse(
            id=0,
            key=setting["key"],
            value=json.dumps(setting["value"]),
            type=setting["type"],
            updated_at=setting["updated_at"],
        )

    @app.put("/api/v1/config/{key}", response_model=SettingResponse)
    async def update_config(key: str, body: SettingUpdate, owner: str = Depends(validate_token)):
        with managed_connection() as conn:
            repo = SettingsRepository(conn)
            setting = repo.get_by_key(key)
            if not setting:
                raise HTTPException(status_code=404, detail="Setting not found")
            new_value = body.value
            new_type = body.type or setting["type"]
            if new_value is not None:
                parsed_value = _parse_setting_value(new_value, new_type)
            else:
                parsed_value = setting["value"]
            repo.upsert(key, parsed_value, new_type)
            setting = repo.get_by_key(key)
        if not setting:
            raise HTTPException(status_code=500)
        return SettingResponse(
            id=0,
            key=setting["key"],
            value=json.dumps(setting["value"]),
            type=setting["type"],
            updated_at=setting["updated_at"],
        )

    @app.delete("/api/v1/config/{key}", status_code=200)
    async def delete_config(key: str, owner: str = Depends(validate_token)):
        with managed_connection() as conn:
            repo = SettingsRepository(conn)
            deleted = repo.delete(key)
        if not deleted:
            raise HTTPException(status_code=404, detail="Setting not found")
        return {"detail": f"Setting '{key}' deleted"}

    # ------------------------------------------------------------------
    # Commit history endpoints
    # ------------------------------------------------------------------
    @app.get("/api/v1/commits", response_model=CommitListResponse)
    async def list_commits(
        project_id: int = Query(..., description="Project ID"),
        limit: int = Query(20, ge=1, le=100),
        cursor: Optional[str] = None,
        owner: str = Depends(validate_token),
    ):
        with managed_connection() as conn:
            proj_repo = ProjectsRepository(conn)
            project = proj_repo.get_by_id(project_id)
            if not project or project["owner"] != owner:
                raise HTTPException(status_code=404, detail="Project not found")
            commits_repo = CommitsRepository(conn)
            items, next_cursor = commits_repo.list_by_project(project_id, limit, cursor)
        return CommitListResponse(
            items=[CommitResponse(**item) for item in items],
            next_cursor=next_cursor,
            total=len(items),
        )

    @app.get("/api/v1/commits/{commit_id}", response_model=CommitResponse)
    async def get_commit(commit_id: int, owner: str = Depends(validate_token)):
        with managed_connection() as conn:
            repo = CommitsRepository(conn)
            commit = repo.get_by_id(commit_id)
        if not commit:
            raise HTTPException(status_code=404, detail="Commit not found")
        # Validate project ownership
        with managed_connection() as conn:
            proj_repo = ProjectsRepository(conn)
            project = proj_repo.get_by_id(commit["project_id"])
            if not project or project["owner"] != owner:
                raise HTTPException(status_code=404, detail="Commit not found")
        return CommitResponse(**commit)

    @app.post("/api/v1/commits", response_model=CommitResponse, status_code=201)
    async def create_commit(body: CommitCreate, owner: str = Depends(validate_token)):
        with managed_connection() as conn:
            proj_repo = ProjectsRepository(conn)
            project = proj_repo.get_by_id(body.project_id)
            if not project or project["owner"] != owner:
                raise HTTPException(status_code=404, detail="Project not found")
            commits_repo = CommitsRepository(conn)
            commit_id = commits_repo.create(
                project_id=body.project_id,
                hash=body.hash,
                message=body.message,
            )
            commit = commits_repo.get_by_id(commit_id)
        if not commit:
            raise HTTPException(status_code=500)
        return CommitResponse(**commit)

    @app.put("/api/v1/commits/{commit_id}", response_model=CommitResponse)
    async def update_commit(commit_id: int, body: CommitUpdate, owner: str = Depends(validate_token)):
        with managed_connection() as conn:
            repo = CommitsRepository(conn)
            commit = repo.get_by_id(commit_id)
            if not commit:
                raise HTTPException(status_code=404, detail="Commit not found")
            proj_repo = ProjectsRepository(conn)
            project = proj_repo.get_by_id(commit["project_id"])
            if not project or project["owner"] != owner:
                raise HTTPException(status_code=404)
            repo.update_message(commit_id, body.message)
            commit = repo.get_by_id(commit_id)
        return CommitResponse(**commit)

    @app.delete("/api/v1/commits/{commit_id}", status_code=200)
    async def delete_commit(commit_id: int, owner: str = Depends(validate_token)):
        with managed_connection() as conn:
            repo = CommitsRepository(conn)
            commit = repo.get_by_id(commit_id)
            if not commit:
                raise HTTPException(status_code=404)
            proj_repo = ProjectsRepository(conn)
            project = proj_repo.get_by_id(commit["project_id"])
            if not project or project["owner"] != owner:
                raise HTTPException(status_code=404)
            repo.soft_delete(commit_id)
        return {"detail": "Commit deleted"}

    # ------------------------------------------------------------------
    # Discord webhook endpoints (minimal)
    # ------------------------------------------------------------------
    @app.post("/api/v1/discord-webhooks", response_model=DiscordWebhookResponse, status_code=201)
    async def create_webhook(body: DiscordWebhookCreate, owner: str = Depends(validate_token)):
        with managed_connection() as conn:
            proj_repo = ProjectsRepository(conn)
            project = proj_repo.get_by_id(body.project_id)
            if not project or project["owner"] != owner:
                raise HTTPException(status_code=404, detail="Project not found")
            hooks_repo = DiscordWebhooksRepository(conn)
            webhook_id = hooks_repo.create(body.project_id, body.url)
            webhook = hooks_repo.get_by_id(webhook_id)
        if not webhook:
            raise HTTPException(status_code=500)
        return DiscordWebhookResponse(**webhook)

    # ------------------------------------------------------------------
    # SSE events endpoint
    # ------------------------------------------------------------------
    @app.get("/api/v1/events")
    async def events(request: Request, owner: str = Depends(validate_token)):
        """Server-Sent Events stream for real-time updates."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        client_id = f"{owner}_{id(queue)}"

        async with sse_lock:
            sse_clients.setdefault(client_id, []).append(queue)

        async def event_generator():
            try:
                # Send an initial connected event
                yield f"event: connected\ndata: {json.dumps({'status': 'connected'})}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield f"event: {event['event']}\ndata: {json.dumps(event['data'])}\n\n"
                        queue.task_done()
                    except asyncio.TimeoutError:
                        # Send keep-alive comment
                        yield ": keepalive\n\n"
            except asyncio.CancelledError:
                pass
            finally:
                async with sse_lock:
                    queues = sse_clients.get(client_id, [])
                    if queue in queues:
                        queues.remove(queue)
                    if not queues:
                        sse_clients.pop(client_id, None)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def _parse_setting_value(value: str, type_str: str) -> Any:
    """Parse setting value string to Python type based on declared type."""
    if type_str == "string":
        return value
    elif type_str == "integer":
        return int(value)
    elif type_str == "boolean":
        return value.lower() in ("true", "1", "yes")
    elif type_str == "json":
        return json.loads(value)
    else:
        raise ValueError(f"Unknown setting type: {type_str}")