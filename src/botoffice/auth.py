from __future__ import annotations

import secrets
from collections.abc import Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings

bearer = HTTPBearer(auto_error=False)


def auth_dependency(settings: Settings) -> Callable[..., None]:
    async def require_token(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        try:
            expected = settings.read_token()
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail="API token is unavailable") from exc
        if not expected:
            raise HTTPException(status_code=503, detail="API token is not configured")
        # bytes, not str: compare_digest raises TypeError (a 500) on a non-ASCII str
        if not credentials or credentials.scheme.lower() != "bearer" or not secrets.compare_digest(
            credentials.credentials.encode(), expected.encode()
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_token
