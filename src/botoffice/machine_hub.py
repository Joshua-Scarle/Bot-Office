"""The WebSocket side of paired computers: ``/v1/machines/ws``.

Each active computer keeps one outgoing connection (through its SSH tunnel). The agent sends the
device token in the ``Authorization`` header, then a ``hello``; the hub answers ``welcome`` with
the computer's flags. Calls go out as ``{"type": "call", "id", "op", "args"}`` and come back as
``{"type": "result", "id", "ok", ...}``; file bodies travel as base64 ``chunk`` frames of at most
CHUNK bytes before the result (pull, shot) or after the call (push, closed by ``chunk_end``). A push
sends one chunk (or the ``chunk_end``) per credit the agent grants with ``{"type": "credit", "id",
"n"}``, so the agent never has to stall its receive loop for a slow disk or an open question.

A new connection for the same computer replaces the old one. Revoke closes the socket with 4401 and
every call in flight fails at once. Close codes the agent acts on: 4401 revoked or invalid token
(it exits), 4403 not activated yet (it waits and retries), 4000 replaced.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import secrets
from dataclasses import dataclass, field
from typing import IO

from fastapi import WebSocket, WebSocketDisconnect

from . import machines
from .store import JobStore

log = logging.getLogger("botoffice.machines")

CHUNK = 256 * 1024
FRAME_MAX = 2 * 1024 * 1024  # a base64 chunk plus its envelope, with room to spare
HELLO_TIMEOUT = 10.0
CREDIT_MAX = 64  # push chunks the agent may have asked for and not received yet
CANCEL_SEND_TIMEOUT = 5.0
INFLIGHT_MAX = 4
SEEN_EVERY = 60.0
CLOSE_REPLACED, CLOSE_REVOKED, CLOSE_PENDING = 4000, 4401, 4403


class HubError(Exception):
    def __init__(self, detail: str, status: int = 502):
        super().__init__(detail)
        self.detail = detail
        self.status = status


@dataclass
class _Call:
    future: asyncio.Future
    sink: IO[bytes] | None = None
    limit: int = 0
    received: int = 0
    credits: int = 0
    wake: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Connection:
    ws: WebSocket
    machine_id: str
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    calls: dict[str, _Call] = field(default_factory=dict)
    closed: bool = False

    async def send(self, message: dict) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(message))

    def fail_all(self, detail: str, status: int = 502) -> None:
        for call in list(self.calls.values()):
            if not call.future.done():
                call.future.set_exception(HubError(detail, status))
        self.calls.clear()


class Hub:
    def __init__(self, store: JobStore):
        self.store = store
        self.connections: dict[str, _Connection] = {}

    def online(self) -> set[str]:
        return {key for key, conn in self.connections.items() if not conn.closed}

    def is_online(self, machine_id: str) -> bool:
        conn = self.connections.get(machine_id)
        return conn is not None and not conn.closed

    async def serve(self, ws: WebSocket) -> None:
        # a browser always sends Origin; the agent never does (no cross-site WebSocket at all)
        if ws.headers.get("origin"):
            await ws.close(code=1008)
            return
        header = ws.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        machine, reason = machines.authenticate(self.store, token.strip()) if scheme.lower() == "bearer" else (None, "invalid")
        await ws.accept()
        if machine is None:
            await ws.close(code=CLOSE_PENDING if reason == "not_activated" else CLOSE_REVOKED, reason=reason)
            return
        machine_id = machine["id"]
        conn = _Connection(ws, machine_id)
        try:
            hello = json.loads(await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT))
            if not isinstance(hello, dict) or hello.get("type") != "hello":
                raise ValueError("expected hello")
        except (asyncio.TimeoutError, ValueError, WebSocketDisconnect, RuntimeError, KeyError, TypeError):
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await ws.close(code=1002)
            return
        old = self.connections.get(machine_id)
        self.connections[machine_id] = conn
        if old is not None:
            await self._close(old, CLOSE_REPLACED, "replaced by a new connection")
        machines.seen(self.store, machine_id, os_name=hello.get("os"), version=hello.get("version"),
                      local_flags=hello.get("local"))
        machines.log_event(self.store, "connected", machine_id=machine_id)
        try:
            await conn.send({"type": "welcome", "machine_id": machine_id, "name": machine["name"],
                             "settings": machines.device_flags(machine)})
            if machines.needs_rotation(machine):
                asyncio.get_running_loop().create_task(self.rotate(machine_id))
            await self._receive(conn)
        except (WebSocketDisconnect, RuntimeError, KeyError):
            pass
        finally:
            conn.closed = True
            conn.fail_all("the computer disconnected")
            if self.connections.get(machine_id) is conn:
                del self.connections[machine_id]
                machines.log_event(self.store, "disconnected", machine_id=machine_id)

    async def _receive(self, conn: _Connection) -> None:
        loop = asyncio.get_running_loop()
        last_seen = loop.time()
        while not conn.closed:
            text = await conn.ws.receive_text()
            if len(text) > FRAME_MAX:
                await self._close(conn, 1009, "frame too large")
                return
            if loop.time() - last_seen > SEEN_EVERY:
                last_seen = loop.time()
                current = machines.get_machine(self.store, conn.machine_id)
                if current is None or current["status"] != "active":
                    await self._close(conn, CLOSE_REVOKED, "revoked")
                    return
                machines.seen(self.store, conn.machine_id)
            try:
                message = json.loads(text)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            kind = message.get("type")
            call = conn.calls.get(message.get("id")) if isinstance(message.get("id"), str) else None
            if kind == "chunk" and call is not None:
                self._chunk(conn, message, call)
            elif kind == "credit" and call is not None:
                n = message.get("n")
                if isinstance(n, int) and not isinstance(n, bool) and 0 < n <= CREDIT_MAX:
                    call.credits = min(CREDIT_MAX, call.credits + n)
                    call.wake.set()
            elif kind == "result" and call is not None:
                if not call.future.done():
                    call.future.set_result(message)
            elif kind == "rotate_request":
                loop.create_task(self.rotate(conn.machine_id))

    def _chunk(self, conn: _Connection, message: dict, call: _Call) -> None:
        if call.future.done():
            return
        try:
            data = base64.b64decode(message.get("data") or "", validate=True)
        except (binascii.Error, ValueError, TypeError):
            call.future.set_exception(HubError("the computer sent a bad chunk"))
            return
        call.received += len(data)
        if call.sink is None or call.received > call.limit:
            call.future.set_exception(HubError("the computer sent more data than allowed", 413))
            return
        call.sink.write(data)

    async def call(self, machine_id: str, op: str, args: dict, *, timeout: float, upload: IO[bytes] | None = None,
                   sink: IO[bytes] | None = None, limit: int = 0) -> dict:
        """Send one call and wait for its result (HubError on offline, timeout, error or disconnect)."""
        conn = self.connections.get(machine_id)
        if conn is None or conn.closed:
            raise HubError("the computer is offline", 409)
        if len(conn.calls) >= INFLIGHT_MAX:
            raise HubError("the computer is busy with other calls; try again shortly", 429)
        call_id = secrets.token_hex(8)
        call = _Call(asyncio.get_running_loop().create_future(), sink, limit)
        conn.calls[call_id] = call
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        async def send_all() -> None:
            await conn.send({"type": "call", "id": call_id, "op": op, "args": args})
            if upload is not None:
                await self._send_upload(conn, call_id, call, upload, deadline)

        try:
            try:  # one deadline for everything: the call, every chunk (disk and socket) and the result
                await asyncio.wait_for(send_all(), max(0.0, deadline - loop.time()))
                result = await asyncio.wait_for(asyncio.shield(call.future), max(0.0, deadline - loop.time()))
            except asyncio.TimeoutError:
                await self._send_cancel(conn, call_id)
                raise HubError("the computer did not answer in time", 504) from None
            except asyncio.CancelledError:
                await asyncio.shield(self._send_cancel(conn, call_id))
                raise
        except (WebSocketDisconnect, RuntimeError, ConnectionError) as exc:
            raise HubError("the computer disconnected") from exc
        finally:
            conn.calls.pop(call_id, None)
        if not result.get("ok"):
            code = str(result.get("code") or "error")[:40]
            detail = machines._clean(result.get("error") or "failed", 500)
            raise HubError(f"{code}: {detail}", 403 if code in ("denied_local", "declined_local") else 422)
        data = result.get("data")
        return data if isinstance(data, dict) else {}

    @staticmethod
    async def _send_cancel(conn: _Connection, call_id: str) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(conn.send({"type": "cancel", "id": call_id}), CANCEL_SEND_TIMEOUT)

    @staticmethod
    async def _send_upload(conn: _Connection, call_id: str, call: _Call, upload: IO[bytes], deadline: float) -> None:
        """Send *upload* as chunks, one per credit; stop early once the call has a result."""
        loop = asyncio.get_running_loop()
        while True:
            while call.credits <= 0 and not call.future.done():
                if loop.time() >= deadline:
                    raise asyncio.TimeoutError
                call.wake.clear()
                waiter = asyncio.ensure_future(call.wake.wait())
                try:
                    await asyncio.wait({waiter, call.future}, timeout=deadline - loop.time(),
                                       return_when=asyncio.FIRST_COMPLETED)
                finally:
                    waiter.cancel()
            if call.future.done():
                return
            chunk = await asyncio.to_thread(upload.read, CHUNK)
            call.credits -= 1
            if not chunk:
                await conn.send({"type": "chunk_end", "id": call_id})
                return
            await conn.send({"type": "chunk", "id": call_id, "data": base64.b64encode(chunk).decode()})

    async def _close(self, conn: _Connection, code: int, reason: str) -> None:
        conn.closed = True
        conn.fail_all("the computer was " + ("revoked" if code == CLOSE_REVOKED else "disconnected"),
                      403 if code == CLOSE_REVOKED else 502)
        with contextlib.suppress(Exception):
            await conn.ws.close(code=code, reason=reason)

    async def disconnect(self, machine_id: str, code: int = CLOSE_REVOKED, reason: str = "revoked") -> None:
        conn = self.connections.pop(machine_id, None)
        if conn is not None:
            await self._close(conn, code, reason)

    async def push_settings(self, machine_id: str) -> None:
        conn = self.connections.get(machine_id)
        machine = machines.get_machine(self.store, machine_id)
        if conn is not None and not conn.closed and machine is not None:
            with contextlib.suppress(Exception):
                await conn.send({"type": "settings", "settings": machines.device_flags(machine)})

    async def rotate(self, machine_id: str) -> bool:
        """A new device token over the live connection; the old one works for 5 more minutes."""
        conn = self.connections.get(machine_id)
        if conn is None or conn.closed:
            return False
        try:
            token = machines.rotate(self.store, machine_id)
        except machines.MachineError:
            return False
        try:
            await conn.send({"type": "rotate", "token": token})
        except Exception:  # noqa: BLE001 - the old token still works for the overlap
            return False
        return True

    async def close_all(self) -> None:
        for machine_id in list(self.connections):
            await self.disconnect(machine_id, 1001, "server stopping")
