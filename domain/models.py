"""Pydantic data models for API requests and responses."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ProjectCreate(BaseModel):
    """Schema for creating a new watched project."""

    name: str = Field(..., min_length=1, max_length=255, description="Display name for the project")
    path: str = Field(..., min_length=1, description="Absolute path to the project directory")
    github_repo_name: Optional[str] = Field(
        None, min_length=1, max_length=100, description="Optional GitHub repository name to create"
    )

    @field_validator("path")
    @classmethod
    def path_must_be_absolute(cls, v: str) -> str:
        from pathlib import Path
        p = Path(v)
        if not p.is_absolute():
            raise ValueError("Path must be absolute")
        return str(p.resolve())


class ProjectUpdate(BaseModel):
    """Schema for updating a project."""

    name: Optional[str] = Field(None, min_length=1, max_length=255)
    path: Optional[str] = Field(None, min_length=1)

    @field_validator("path")
    @classmethod
    def path_must_be_absolute_if_provided(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        from pathlib import Path
        p = Path(v)
        if not p.is_absolute():
            raise ValueError("Path must be absolute")
        return str(p.resolve())


class ProjectResponse(BaseModel):
    """Project as returned by the API."""

    id: int
    name: str
    path: str
    owner: str
    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ProjectListResponse(BaseModel):
    """Paginated list of projects."""

    items: list[ProjectResponse]
    next_cursor: Optional[str] = None
    total: int


class SettingCreate(BaseModel):
    """Schema for creating a configuration setting."""

    key: str = Field(..., min_length=1, max_length=128)
    value: str = Field(...)
    type: str = Field(..., pattern=r"^(string|integer|boolean|json)$")


class SettingUpdate(BaseModel):
    """Schema for updating a configuration setting."""

    value: Optional[str] = None
    type: Optional[str] = Field(None, pattern=r"^(string|integer|boolean|json)$")


class SettingResponse(BaseModel):
    """Setting as returned by the API."""

    id: int
    key: str
    value: str
    type: str
    updated_at: datetime

    model_config = {"from_attributes": True}


class SettingListResponse(BaseModel):
    """List of all settings."""

    items: list[SettingResponse]


class CommitCreate(BaseModel):
    """Schema for manually logging a commit."""

    project_id: int = Field(..., gt=0)
    hash: str = Field(..., min_length=40, max_length=40, pattern=r"^[a-f0-9]{40}$")
    message: str = Field(..., min_length=1)


class CommitUpdate(BaseModel):
    """Schema for updating a stored commit message."""

    message: str = Field(..., min_length=1)


class CommitResponse(BaseModel):
    """Commit as returned by the API."""

    id: int
    project_id: int
    hash: str
    message: str
    branch: str
    committed_at: datetime
    created_at: datetime
    deleted_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class CommitListResponse(BaseModel):
    """Paginated list of commits."""

    items: list[CommitResponse]
    next_cursor: Optional[str] = None
    total: int


class DiscordWebhookCreate(BaseModel):
    """Schema for adding a Discord webhook to a project."""

    project_id: int = Field(..., gt=0)
    url: str = Field(..., min_length=1, pattern=r"^https://discord\.com/api/webhooks/")


class DiscordWebhookResponse(BaseModel):
    """Discord webhook as returned by the API."""

    id: int
    project_id: int
    url: str
    enabled: bool
    created_at: datetime
    deleted_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SSEClientEvent(BaseModel):
    """Base schema for SSE events."""

    event: str
    data: dict