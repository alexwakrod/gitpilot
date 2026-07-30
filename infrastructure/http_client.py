"""HTTP client session manager using httpx with connection pooling."""

import httpx

_client: httpx.AsyncClient | None = None


async def get_http_client() -> httpx.AsyncClient:
    """Return a shared async HTTP client with connection pooling."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=50),
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": "GitPilot/0.1.0"},
        )
    return _client


async def close_http_client() -> None:
    """Gracefully close the HTTP client."""
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


def get_sync_client() -> httpx.Client:
    """Return a synchronous HTTP client for non-async contexts."""
    return httpx.Client(
        limits=httpx.Limits(max_keepalive_connections=5, max_connections=20),
        timeout=httpx.Timeout(30.0, connect=10.0),
        headers={"User-Agent": "GitPilot/0.1.0"},
    )