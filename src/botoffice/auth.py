from __future__ import annotations

import secrets
from collections.abc import Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import remote_access
from .config import Settings

bearer = HTTPBearer(auto_error=False)


def auth_dependency(settings: Settings) -> Callable[..., None]:
    async def require_token(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        # 0.12.23: RemoteAccessMiddleware (outermost) has already verified the Cloudflare Access JWT,
        # the host, the role and, for a service token, the route allowlist. Only it sets this.
        if remote_access.current_remote_identity.get() is not None:
            return
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
