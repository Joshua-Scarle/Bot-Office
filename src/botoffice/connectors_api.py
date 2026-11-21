"""Routes of the connectors panel . Owner (master token) only: a job's agent token is not
accepted here, so a bot cannot read or set a connector secret. No response carries a secret value."""
from __future__ import annotations

import asyncio
import html

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from . import connectors


class BotFlags(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    write_enabled: bool | None = None


class TokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(repr=False, max_length=8192)


_PAGE = ("<!doctype html><meta charset=utf-8><title>Bot Office</title><body style=\"font:16px system-ui;"
         "max-width:34em;margin:3em auto\"><h2>{title}</h2><p>{text}</p><p><a href=\"/\">Back to Bot Office</a></p>")


def register(app: FastAPI, auth, store, settings, transport=None) -> None:
    kwargs = {"transport": transport} if transport else {}

    def known(connector_id: str) -> connectors.Connector:
        try:
            return connectors.get(connector_id)
        except connectors.ConnectorError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def view() -> list[dict]:
        bots = [{"id": str(bot.id), "name": bot.name} for bot in store.list_bots()]
        return connectors.listing(store, bots)

    @app.get("/v1/connectors", dependencies=[Depends(auth)])
    async def list_connectors() -> dict:
        return {"enabled": connectors.enabled(), "callback": connectors.redirect_uri(settings.port),
                "connectors": await asyncio.to_thread(view)}

    # The owner's browser lands here (via the SSH tunnel to 127.0.0.1): no bearer header, so the one-time
    # state of start_oauth is what authorises it. Registered before the {connector_id} routes.
    @app.get(connectors.CALLBACK_PATH, response_class=HTMLResponse)
    async def oauth_callback(code: str | None = Query(None, max_length=4096), state: str | None = Query(None, max_length=256),
                             error: str | None = Query(None, max_length=200), iss: str | None = Query(None, max_length=500)) -> HTMLResponse:
        if error or not code or not state:
            if state:
                await asyncio.to_thread(connectors._delete_secret, connectors._pending_name(state))
            return HTMLResponse(_PAGE.format(title="Not connected", text="The provider reported: "
                                             + html.escape(error or "no code") + ". Press Connect again."), status_code=400)
        try:
            connector_id = await asyncio.to_thread(connectors.complete_oauth, store, state, code, iss, **kwargs)
        except connectors.ConnectorError as exc:
            return HTMLResponse(_PAGE.format(title="Not connected", text=html.escape(str(exc))), status_code=400)
        await asyncio.to_thread(connectors.probe, store, connector_id, **kwargs)
        label = html.escape(connectors.get(connector_id).label)
        return HTMLResponse(_PAGE.format(title=f"{label} connected", text="You can close this tab and "
                                         "check the status in Settings, Connectors."))

    @app.post("/v1/connectors/{connector_id}/oauth/start", dependencies=[Depends(auth)])
    async def oauth_start(connector_id: str) -> dict:
        known(connector_id)
        try:
            url = await asyncio.to_thread(connectors.start_oauth, connector_id, settings.port, **kwargs)
        except connectors.ConnectorError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"authorize_url": url}  # a URL for the browser: state and PKCE challenge, no secret

    @app.post("/v1/connectors/{connector_id}/token", dependencies=[Depends(auth)])
    async def paste_token(connector_id: str, body: TokenBody) -> dict:
        known(connector_id)
        try:
            await asyncio.to_thread(connectors.save_token, store, connector_id, body.token)
        except connectors.ConnectorError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return await asyncio.to_thread(connectors.probe, store, connector_id, **kwargs)  # state only

    @app.post("/v1/connectors/{connector_id}/health", dependencies=[Depends(auth)])
    async def check_health(connector_id: str) -> dict:
        known(connector_id)
        return await asyncio.to_thread(connectors.probe, store, connector_id, **kwargs)

    @app.post("/v1/connectors/{connector_id}/revoke", dependencies=[Depends(auth)])
    async def revoke(connector_id: str) -> dict:
        known(connector_id)
        await asyncio.to_thread(connectors.revoke, store, connector_id)
        return connectors.state(store, connector_id)

    @app.put("/v1/connectors/{connector_id}/bots/{bot_id}", dependencies=[Depends(auth)])
    async def set_bot(connector_id: str, bot_id: str, body: BotFlags) -> dict:
        known(connector_id)
        if store.get_bot(bot_id) is None:
            raise HTTPException(status_code=404, detail="no such bot")
        return connectors.set_bot(store, connector_id, str(store.get_bot(bot_id).id), body.enabled, body.write_enabled)
