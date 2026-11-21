"""HTTP and WebSocket routes for paired computers (see machines.py and machine_hub.py).

Owner (master token): /v1/machines... (list, pairing codes, activate, revoke, rotate, flags, grants,
audit) and the "Always" button of a machine approval card. Unauthenticated: POST /v1/machines/pair
(rate-limited; it only turns a live code into a computer that still waits for Activate). Device
token: the WebSocket /v1/machines/ws. Job agent token (bin/machine): /v1/agent/machines/*.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from collections.abc import Callable
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import machines
from .machine_hub import Hub, HubError
from .rules import untrusted
from .store import JobStore

PAIR_BODY_LIMIT = 4096
REQUEST_BODY_LIMIT = 64 * 1024
RESOLVE_TIMEOUT = 30.0
PUSH_UPLOADS_MAX = 2  # push bodies received at once (each up to 100 MiB, spooled to disk)
PUSH_BODY_TIMEOUT = 300.0  # the whole body; the helper sends it from this same server, so a stall is a fault
TIMEOUTS = {"ls": 60.0, "read": 60.0, "pull": 900.0, "push": 900.0, "shot": 60.0}


class CodeBody(BaseModel):
    name: str = Field(default="", max_length=60)


class ActivateBody(BaseModel):
    fingerprint: str = Field(max_length=40)


class MachinePatch(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    sandbox: bool | None = None
    confirm_local: bool | None = None


class AlwaysBody(BaseModel):
    pattern: str | None = Field(default=None, max_length=500)
    confirm_broad: bool = False


def _http(exc: machines.MachineError | HubError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.detail)


async def _body(request: Request, limit: int) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            raise HTTPException(status_code=413, detail="request body too large")
    return bytes(data)


def _render_ls(data: dict) -> str:
    lines = []
    for entry in data.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        kind = {"dir": "d", "file": "-", "link": "l"}.get(entry.get("type"), "?")
        size = entry.get("size") if isinstance(entry.get("size"), int) else ""
        lines.append(f"{kind} {size:>12} {entry.get('path', '')}" + (f" -> {entry['target']}" if entry.get("target") else ""))
    if data.get("truncated"):
        lines.append(f"... truncated at {machines.LS_MAX_ENTRIES} entries")
    return "\n".join(lines)


def _render_run(data: dict) -> str:
    parts = []
    for stream in ("stdout", "stderr"):
        text = data.get(stream) if isinstance(data.get(stream), str) else ""
        if text or data.get(f"{stream}_truncated"):
            parts.append(f"--- {stream}" + (" (cut at 64 KiB)" if data.get(f"{stream}_truncated") else "") + f" ---\n{text}")
    status = "timed out (process group killed)" if data.get("timed_out") else f"exit {data.get('exit')}"
    parts.append(f"--- {status}" + (", sandboxed (bwrap)" if data.get("sandboxed") else "") + " ---")
    return "\n".join(parts)


def register(app: FastAPI, *, store: JobStore, settings, auth: Callable, agent_scope: Callable) -> "MachineRoutes":
    hub = Hub(store)
    uploading: set[int] = set()  # push requests whose body is being received now
    app.state.machine_hub = hub
    pair_limit = machines.RateLimit(*machines.PAIR_RATE)
    spool = Path(settings.data_dir)

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except machines.MachineError as exc:
            raise _http(exc) from exc

    # --- owner ---

    @app.get("/v1/machines", dependencies=[Depends(auth)])
    async def list_machines() -> dict:
        machines.expire(store)
        return {"machines": machines.list_machines(store, hub.online()), "grants": machines.list_grants(store),
                "defaults": machines.DEVICE_FLAG_DEFAULTS}

    @app.post("/v1/machines/pairing-codes", status_code=201, dependencies=[Depends(auth)])
    async def pairing_code(body: CodeBody) -> dict:
        return call(machines.create_code, store, body.name)

    @app.post("/v1/machines/{machine_id}/activate", dependencies=[Depends(auth)])
    async def activate(machine_id: str, body: ActivateBody) -> dict:
        return machines.public(call(machines.activate, store, machine_id, body.fingerprint), hub.is_online(machine_id))

    @app.post("/v1/machines/{machine_id}/revoke", dependencies=[Depends(auth)])
    async def revoke(machine_id: str) -> dict:
        machine = call(machines.revoke, store, machine_id)
        await hub.disconnect(machine_id)
        return machines.public(machine)

    @app.post("/v1/machines/{machine_id}/rotate", dependencies=[Depends(auth)])
    async def rotate(machine_id: str) -> dict:
        if not hub.is_online(machine_id):
            raise HTTPException(status_code=409, detail="the computer must be online to get its new token")
        if not await hub.rotate(machine_id):
            raise HTTPException(status_code=409, detail="could not rotate now (rotated in the last 5 minutes, or not active)")
        return machines.public(machines.get_machine(store, machine_id), True)

    @app.patch("/v1/machines/{machine_id}", dependencies=[Depends(auth)])
    async def patch_machine(machine_id: str, body: MachinePatch) -> dict:
        machine = call(machines.update_machine, store, machine_id, name=body.name, sandbox=body.sandbox,
                       confirm_local=body.confirm_local)
        if body.sandbox is not None or body.confirm_local is not None:
            await hub.push_settings(machine_id)
        return machines.public(machine, hub.is_online(machine_id))

    @app.get("/v1/machines/{machine_id}/audit", dependencies=[Depends(auth)])
    async def machine_audit(machine_id: str, limit: int = 200) -> list[dict]:
        return machines.audit_log(store, machine_id, limit)

    @app.get("/v1/machine-grants", dependencies=[Depends(auth)])
    async def grants(machine_id: str | None = None) -> list[dict]:
        return machines.list_grants(store, machine_id)

    @app.post("/v1/machine-grants/{grant_id}/revoke", dependencies=[Depends(auth)])
    async def revoke_grant(grant_id: int) -> dict:
        return call(machines.revoke_grant, store, grant_id)

    @app.post("/v1/machines/approvals/{approval_id}/always", dependencies=[Depends(auth)])
    async def always(approval_id: int, body: AlwaysBody) -> dict:
        machines.expire(store)
        return call(machines.approve_always, store, approval_id, body.pattern, body.confirm_broad)

    # --- the computer itself ---

    @app.post("/v1/machines/pair")
    async def pair(request: Request) -> dict:
        if not pair_limit.allow():
            raise HTTPException(status_code=429, detail="too many pairing attempts; wait a minute")
        try:
            body = json.loads(await _body(request, PAIR_BODY_LIMIT) or b"{}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="expected JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected an object")
        return call(machines.redeem, store, body.get("code"), body.get("hostname"), body.get("os"), body.get("version"))

    @app.websocket("/v1/machines/ws")
    async def machine_ws(ws: WebSocket) -> None:
        await hub.serve(ws)

    # --- bots (job agent token) ---

    @app.get("/v1/agent/machines")
    async def agent_machines(job=Depends(agent_scope)) -> list[dict]:  # noqa: B008
        online = hub.online()
        return [{"id": item["id"], "name": item["name"], "hostname": item["hostname"], "os": item["os"],
                 "online": item["online"], "last_seen_at": item["last_seen_at"]}
                for item in machines.list_machines(store, online) if item["status"] == "active"]

    @app.post("/v1/agent/machines/requests", status_code=201)
    async def agent_request(request: Request, job=Depends(agent_scope)) -> dict:  # noqa: B008
        try:
            body = json.loads(await _body(request, REQUEST_BODY_LIMIT) or b"{}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="expected JSON") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="expected an object")
        if not machines.class_enabled(store):
            raise HTTPException(status_code=403, detail="Controlling a computer is switched off in Settings > Approvals")
        machine = call(machines.find_for_bot, store, body.get("machine"))
        tool = body.get("tool")
        args = call(machines.validate_args, tool, body.get("args"))
        if not machines.request_allowed(str(job.bot_id)):
            raise HTTPException(status_code=429, detail="too many computer requests this minute")
        resolved = None
        kind = machines.RESOLVE_KIND.get(tool)
        if kind is not None:
            target = args["cwd"] if tool == "run" else args["path"]
            try:
                resolved = await hub.call(machine["id"], "resolve", {"path": target, "kind": kind},
                                          timeout=RESOLVE_TIMEOUT)
            except HubError as exc:
                raise _http(exc) from exc
        elif not hub.is_online(machine["id"]):
            raise HTTPException(status_code=409, detail="the computer is offline")
        args = call(machines.canonical_args, tool, args, resolved)
        machines.expire(store)
        item = call(machines.record_request, store, job, machine, tool, args)
        return machines.request_view(item)

    def _own(request_id: int, job) -> dict:
        item = machines.get_request(store, request_id)
        if item is None or item["job_id"] != str(job.id):
            raise HTTPException(status_code=404, detail="request not found")
        return item

    @app.get("/v1/agent/machines/requests/{request_id}")
    async def agent_request_status(request_id: int, job=Depends(agent_scope)) -> dict:  # noqa: B008
        machines.expire(store)
        return machines.request_view(_own(request_id, job))

    @app.post("/v1/agent/machines/requests/{request_id}/cancel")
    async def agent_cancel(request_id: int, job=Depends(agent_scope)) -> dict:  # noqa: B008
        _own(request_id, job)
        return machines.request_view(call(machines.cancel, store, request_id, job))

    @app.post("/v1/agent/machines/requests/{request_id}/execute")
    async def agent_execute(request_id: int, request: Request, job=Depends(agent_scope)):  # noqa: B008
        item = _own(request_id, job)
        machines.expire(store)
        upload = None
        claimed_upload = False
        try:
            if item["tool"] == "push":
                # nothing is received before the request is approved, and one body at a time per request
                # and at most PUSH_UPLOADS_MAX at once: a bot cannot fill the disk with unapproved uploads
                item = _own(request_id, job)
                if item["status"] != "approved":
                    raise HTTPException(status_code=403, detail=f"request is {item['status']}, not approved")
                bound = json.loads(item["args_json"])
                declared = request.headers.get("content-length")
                if declared is not None and (not declared.isdigit() or int(declared) != bound["size"]):
                    machines.fail_unexecuted(store, request_id, job, "the upload is not the approved size")
                    raise HTTPException(status_code=409, detail="the file's size differs from what was approved; ask again")
                if request_id in uploading or len(uploading) >= PUSH_UPLOADS_MAX:
                    raise HTTPException(status_code=429, detail="another upload is in progress; try again shortly")
                uploading.add(request_id)
                claimed_upload = True
                upload = tempfile.TemporaryFile(dir=spool)
                digest = hashlib.sha256()

                async def receive() -> int:
                    size = 0
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > machines.TRANSFER_MAX or size > bound["size"]:
                            break
                        digest.update(chunk)
                        await asyncio.to_thread(upload.write, chunk)
                    return size

                try:
                    size = await asyncio.wait_for(receive(), PUSH_BODY_TIMEOUT)
                except asyncio.TimeoutError:
                    # a stalled body must not keep an upload slot: it uses the approval up
                    machines.fail_unexecuted(store, request_id, job, "the upload stalled")
                    raise HTTPException(status_code=408, detail="the upload took too long; ask again") from None
                if size != bound["size"] or digest.hexdigest() != bound["sha256"]:
                    # single use: content that differs from what was approved uses the approval up
                    machines.fail_unexecuted(store, request_id, job, "the file differs from the approved sha256")
                    raise HTTPException(status_code=409, detail="the file's content differs from what was approved "
                                        "(sha256/size); ask again")
                upload.seek(0)
            claimed = call(machines.begin_execute, store, request_id, job)
            return await _dispatch(claimed, upload)
        finally:
            if claimed_upload:
                uploading.discard(request_id)
            if upload is not None:
                upload.close()

    async def _dispatch(claimed: dict, upload) -> object:
        tool, args, machine = claimed["tool"], claimed["args"], claimed["machine"]
        request_id = claimed["id"]
        source = f"machine:{machine['name']}:{tool}"
        bot = store.get_bot(claimed["bot_id"])
        meta = {"request": request_id, "bot": bot.name if bot is not None else str(claimed["bot_id"])}
        sink = None
        try:
            if tool in ("pull", "shot"):
                sink = tempfile.TemporaryFile(dir=spool)
                limit = machines.TRANSFER_MAX if tool == "pull" else machines.SHOT_MAX
                data = await hub.call(machine["id"], tool, {**_wire(tool, args), "meta": meta},
                                      timeout=TIMEOUTS[tool], sink=sink, limit=limit)
                sink.seek(0)
                digest, size = hashlib.sha256(), 0
                while block := await asyncio.to_thread(sink.read, 1024 * 1024):
                    digest.update(block)
                    size += len(block)
                if digest.hexdigest() != data.get("sha256") or size != data.get("size"):
                    raise HubError("the transfer was incomplete (sha256 mismatch)")
                if tool == "pull" and size != args.get("file_size"):
                    raise HubError("the file changed size since it was approved")
                machines.finish(store, request_id, ok=True, result={"sha256": data["sha256"], "size": size})
                sink.seek(0)
                handle, sink = sink, None

                def stream():
                    with handle:
                        while block := handle.read(1024 * 1024):
                            yield block
                return StreamingResponse(stream(), media_type="application/octet-stream",
                                         headers={"X-Machine-Sha256": data["sha256"], "X-Machine-Size": str(size)})
            timeout = args["timeout"] + 30.0 if tool == "run" else TIMEOUTS[tool]
            data = await hub.call(machine["id"], tool, {**_wire(tool, args), "meta": meta}, timeout=timeout,
                                  upload=upload)
            machines.finish(store, request_id, ok=True, result=data)
        except HubError as exc:
            machines.finish(store, request_id, ok=False, error=exc.detail)
            raise _http(exc) from exc
        finally:
            if sink is not None:
                sink.close()
        if tool == "ls":
            return {"ok": True, "entries": len(data.get("entries") or []), "truncated": bool(data.get("truncated")),
                    "text": untrusted(source, _render_ls(data))}
        if tool == "read":
            body = data.get("data") if isinstance(data.get("data"), str) else ""
            return {"ok": True, "size": data.get("size"), "eof": bool(data.get("eof")),
                    "encoding": data.get("encoding"), "next_offset": data.get("next_offset"),
                    "text": untrusted(source, body)}
        if tool == "run":
            return {"ok": True, "exit": data.get("exit"), "timed_out": bool(data.get("timed_out")),
                    "sandboxed": bool(data.get("sandboxed")), "text": untrusted(source, _render_run(data))}
        return {"ok": True, "path": args["path"], "size": data.get("size"), "sha256": data.get("sha256")}

    return MachineRoutes(store, hub)


def _wire(tool: str, args: dict) -> dict:
    """What the agent gets: the canonical arguments it re-checks at execution."""
    if tool == "ls":
        return {"path": args["path"], "depth": args["depth"]}
    if tool == "read":
        return {"path": args["path"], "offset": args["offset"], "length": args["length"], "b64": args["b64"]}
    if tool == "run":
        return {"argv": args["argv"], "sh": args["sh"], "cwd": args["cwd"], "timeout": args["timeout"]}
    if tool == "pull":
        return {"path": args["path"], "size": args.get("file_size")}
    if tool == "push":
        return {"path": args["path"], "size": args["size"], "sha256": args["sha256"], "overwrite": args["overwrite"]}
    return {}


class MachineRoutes:
    """What api.py needs from this module: the Approve / Deny buttons of a machine card."""

    def __init__(self, store: JobStore, hub: Hub):
        self.store = store
        self.hub = hub

    def decide(self, approval_id: int, action: str) -> dict | None:
        """None when *approval_id* is not a machine approval (rules.py handles it)."""
        if not machines.is_machine_approval(self.store, approval_id):
            return None
        machines.expire(self.store)
        try:
            return (machines.approve if action == "approve" else machines.deny)(self.store, approval_id)
        except machines.MachineError as exc:
            raise _http(exc) from exc
