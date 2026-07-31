"""User identity and permission checks."""

import os
import secrets
from pathlib import Path

from gitpilot.domain.settings import get_gitpilot_dir


def get_current_os_user() -> str:
    """Return the current OS username."""
    username = os.environ.get("USER") or os.environ.get("USERNAME")
    if not username:
        import pwd
        username = pwd.getpwuid(os.getuid()).pw_name
    return username


def get_token_path() -> Path:
    """Return the path to the auth token file."""
    return get_gitpilot_dir() / "auth_token"


def generate_api_token() -> str:
    """Generate a cryptographically secure random API token."""
    return secrets.token_urlsafe(32)


def ensure_token_file() -> str:
    """Ensure the auth token file exists, creating it if necessary.
    Returns the token string.
    """
    token_path = get_token_path()
    if token_path.exists():
        return token_path.read_text().strip()
    token = generate_api_token()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(token)
    token_path.chmod(0o600)
    return token


def verify_owner(owner: str, token: str) -> bool:
    """Verify that the token matches the owner."""
    stored_token_path = get_token_path()
    if not stored_token_path.exists():
        return False
    stored_token = stored_token_path.read_text().strip()
    return secrets.compare_digest(stored_token, token)