from __future__ import annotations

import hmac

from app.core.config import settings
from fastapi import Header, HTTPException, Query, status


def token_is_valid(token: str | None) -> bool:
    expected = settings.resolved_api_token
    if not expected:
        # Production without a token is a configuration error, fail closed.
        return False
    return bool(token) and hmac.compare_digest(token, expected)


async def require_token(
    x_jarvis_token: str | None = Header(default=None, alias="X-Jarvis-Token"),
) -> None:
    if not token_is_valid(x_jarvis_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API token.",
        )


async def require_token_query(token: str | None = Query(default=None)) -> None:
    """WebSocket connections cannot set custom headers from browsers."""
    if not token_is_valid(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API token.",
        )
