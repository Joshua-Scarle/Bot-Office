"""The connection to Bot Office: out through the SSH tunnel only, never listening.

One WebSocket to ``<server>/v1/machines/ws``; reconnects with backoff (1 s doubling to 60 s).
Close 4401 (revoked or bad token) ends the agent with exit 3; 4403 (not activated yet) waits.
Each call runs in a worker thread with every path checked again here, then is written to the
local audit log.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import platform
import socket
import threading
import time
import urllib.parse
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from . import __version__, ops, tunnel
from .config import Config, TokenStore, effective
from .paths import deny_roots

WS_PATH = "/v1/machines/ws"
BACKOFF_MAX = 60.0
PENDING_WAIT = 30.0
MAX_FRAME = 4 * 1024 * 1024
CLOSE_REPLACED, CLOSE_REVOKED, CLOSE_PENDING = 4000, 4401, 4403
EXIT_REVOKED = 3
OPS = ("resolve", "ls", "read", "run", "pull", "push", "shot")
CALLS_MAX = 8  # in flight here at once, whatever the server sends
UPLOAD_QUEUE = 16  # push chunks buffered per call: the credit granted to Bot Office at a time


class Revoked(Exception):
    """Bot Office refused this device token for good: pair again."""


def ws_url(server: str) -> str:
    server = server.rstrip("/")
    if server.startswith("http://"):
        server = "ws://" + server[len("http://"):]
    elif server.startswith("https://"):
        server = "wss://" + server[len("https://"):]
    return server + WS_PATH


class Audit:
    """One JSON line per call in audit.jsonl (mode 0600): what, for which bot, and the outcome.
    File contents and command output are never written here."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()

    def write(self, entry: dict) -> None:
        entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **entry}
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, ops.LocalDenied):
        return "denied_local"
    if isinstance(exc, ops.Declined):
        return "declined_local"
    if isinstance(exc, ops.PathChanged):
        return "path_changed"
    if isinstance(exc, FileNotFoundError):
        return "not_found"
    if isinstance(exc, FileExistsError):
        return "exists"
    if isinstance(exc, PermissionError):
        return "permission"
    if isinstance(exc, ValueError):
        return "bad_args"
    return "error"


def describe(op: str, args: dict, meta: dict) -> str:
    """The question for the confirm pop-up."""
    who = meta.get("bot") or "a bot"
    if op == "run":
        what = " ".join(args.get("argv") or []) if args.get("argv") else f"sh -c {args.get('sh')}"
        return f"{who} wants to run in {args.get('cwd')}:\n{what}"
    if op == "push":
        verb = "overwrite" if args.get("overwrite") else "create"
        return f"{who} wants to {verb} {args.get('path')} ({args.get('size')} bytes)"
    if op == "shot":
        return f"{who} wants a screenshot of this screen"
    verb = {"ls": "list", "read": "read", "pull": "copy"}[op]
    return f"{who} wants to {verb} {args.get('path')}"


def _audit_args(op: str, args: dict) -> dict:
    keep = ("path", "kind", "depth", "offset", "length", "argv", "sh", "cwd", "timeout", "size", "sha256", "overwrite")
    out = {key: args[key] for key in keep if key in args}
    if isinstance(out.get("sh"), str):
        out["sh"] = out["sh"][:2000]
    return out


class Agent:
    def __init__(self, cfg: Config, device: dict, tokens: TokenStore, *, confirm=None):
        self.cfg = cfg
        self.device = device
        self.tokens = tokens
        self.audit = Audit(cfg.audit_path)
        self.roots = deny_roots(cfg.deny_entries(), str(cfg.home))
        self.server_flags: dict = {}
        self.confirm = confirm or ops.confirm_with(cfg.confirm_command, cfg.confirm_timeout)
        self.token = tokens.load()
        self.ws = None
        self.send_lock = asyncio.Lock()
        self.uploads: dict[str, asyncio.Queue] = {}
        self.cancels: dict[str, threading.Event] = {}
        self.tasks: set[asyncio.Task] = set()
        self.connected = asyncio.Event()

    # --- flags ---

    def flag(self, name: str) -> bool:
        return effective(getattr(self.cfg, name), self.server_flags.get(name))

    def context(self) -> ops.Context:
        return ops.Context(home=str(self.cfg.home), roots=self.roots, sandbox=self.flag("sandbox"))

    # --- connection ---

    async def run_forever(self) -> None:
        delay = 1.0
        while True:
            try:
                await self.session()
                delay = 1.0
            except Revoked:
                raise
            except _Pending:
                await asyncio.sleep(PENDING_WAIT)
                continue
            except tunnel.TunnelNotOurs as exc:
                print(f"botoffice-agent: {exc}", flush=True)
            except (OSError, ConnectionClosed, InvalidHandshake, asyncio.TimeoutError) as exc:
                if isinstance(exc, InvalidStatus) and exc.response.status_code in (401, 403):
                    raise Revoked("Bot Office refused the device token") from exc
            await asyncio.sleep(delay)
            delay = min(BACKOFF_MAX, delay * 2)

    async def session(self) -> None:
        headers = {"Authorization": "Bearer " + self.token}
        url = ws_url(self.device.get("server") or self.cfg.server)
        extra = {}
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme == "ws":
            # plain ws is loopback only (the SSH tunnel): first make sure the port is this user's
            extra["sock"] = await asyncio.to_thread(tunnel.connect, parsed.hostname or "", parsed.port or 80)
        async with connect(url, additional_headers=headers, max_size=MAX_FRAME, ping_interval=20, ping_timeout=20,
                           open_timeout=20, proxy=None, **extra) as ws:
            self.ws = ws
            try:
                await self.send({"type": "hello", "os": f"{platform.system()} {platform.release()}",
                                 "version": __version__, "hostname": socket.gethostname(),
                                 "local": self.cfg.local_flags()})
                async for text in ws:
                    if isinstance(text, bytes):
                        continue
                    try:
                        message = json.loads(text)
                    except ValueError:
                        continue
                    if isinstance(message, dict):
                        await self.handle(message)
            except ConnectionClosed:
                pass
            finally:
                self.connected.clear()
                self.ws = None
                for event in self.cancels.values():
                    event.set()
                for task in list(self.tasks):
                    task.cancel()
            code = ws.close_code
            if code == CLOSE_REVOKED:
                raise Revoked(ws.close_reason or "revoked")
            if code == CLOSE_PENDING:
                raise _Pending()

    async def send(self, message: dict) -> None:
        ws = self.ws
        if ws is None:
            raise ConnectionError("not connected")
        async with self.send_lock:
            await ws.send(json.dumps(message))

    async def handle(self, message: dict) -> None:
        kind = message.get("type")
        call_id = message.get("id") if isinstance(message.get("id"), str) else None
        if kind == "welcome":
            self.server_flags = message.get("settings") or {}
            self.connected.set()
        elif kind == "settings":
            self.server_flags = message.get("settings") or {}
        elif kind == "rotate" and isinstance(message.get("token"), str):
            await self.rotate(message["token"])
        elif kind == "call" and call_id:
            if len(self.tasks) >= CALLS_MAX or call_id in self.cancels:
                await self.send({"type": "result", "id": call_id, "ok": False, "code": "busy",
                                 "error": "this computer is busy with other calls"})
                return
            # before the task first runs: the upload chunks follow this frame at once
            self.cancels[call_id] = threading.Event()
            if message.get("op") == "push":
                self.uploads[call_id] = asyncio.Queue(UPLOAD_QUEUE)
            task = asyncio.get_running_loop().create_task(self.call(call_id, message.get("op"),
                                                                    message.get("args") or {}))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        elif kind in ("chunk", "chunk_end") and call_id in self.uploads:
            # never wait here: this loop also carries cancels. Bot Office sends only what we granted.
            try:
                self.uploads[call_id].put_nowait(message)
            except asyncio.QueueFull:
                self.cancels.get(call_id, threading.Event()).set()
                self.uploads.pop(call_id, None)
        elif kind == "cancel" and call_id in self.cancels:
            self.cancels[call_id].set()

    async def rotate(self, token: str) -> None:
        await asyncio.to_thread(self.tokens.save, token)
        self.token = token
        self.audit.write({"op": "rotate", "ok": True})
        with contextlib.suppress(Exception):
            await self.send({"type": "rotated"})

    # --- calls ---

    async def call(self, call_id: str, op: object, args: dict) -> None:
        meta = args.pop("meta", None) if isinstance(args, dict) else None
        meta = meta if isinstance(meta, dict) else {}
        entry = {"op": op, "request": meta.get("request"), "bot": meta.get("bot"),
                 "args": _audit_args(str(op), args) if isinstance(args, dict) else {}}
        cancel = self.cancels.setdefault(call_id, threading.Event())
        if op == "push":
            self.uploads.setdefault(call_id, asyncio.Queue(UPLOAD_QUEUE))
        try:
            if op not in OPS or not isinstance(args, dict):
                raise ValueError("unknown call")
            ctx = self.context()
            entry["sandbox"] = ctx.sandbox
            if op != "resolve" and self.flag("confirm_local"):
                entry["confirmed_local"] = True
                if not await asyncio.to_thread(self.confirm, describe(op, args, meta)):
                    raise ops.Declined("declined on this computer (or no answer)")
                if cancel.is_set():  # Bot Office gave up while the question was open: a late yes is void
                    raise ops.Declined("cancelled by Bot Office while waiting for the answer here")
            data = await getattr(self, "_" + op)(call_id, args, ctx, cancel)
            entry.update(ok=True, **{key: data[key] for key in ("sha256", "size", "exit", "timed_out") if key in data})
            await self.send({"type": "result", "id": call_id, "ok": True, "data": data})
        except asyncio.CancelledError:
            entry.update(ok=False, code="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - every failure goes back as a result
            code = _error_code(exc)
            entry.update(ok=False, code=code, error=str(exc)[:500])
            with contextlib.suppress(Exception):
                await self.send({"type": "result", "id": call_id, "ok": False, "code": code, "error": str(exc)[:500]})
        finally:
            self.cancels.pop(call_id, None)
            queue = self.uploads.pop(call_id, None)
            while queue is not None and not queue.empty():
                queue.get_nowait()  # frees a handler waiting to put a chunk for a call that ended
            if op != "resolve" or not entry.get("ok"):
                with contextlib.suppress(OSError):
                    self.audit.write(entry)

    async def _resolve(self, call_id, args, ctx, cancel) -> dict:
        return await asyncio.to_thread(ops.op_resolve, args, ctx)

    async def _ls(self, call_id, args, ctx, cancel) -> dict:
        return await asyncio.to_thread(ops.op_ls, args, ctx)

    async def _read(self, call_id, args, ctx, cancel) -> dict:
        return await asyncio.to_thread(ops.op_read, args, ctx)

    async def _run(self, call_id, args, ctx, cancel) -> dict:
        return await asyncio.to_thread(ops.op_run, args, ctx, cancel)

    async def _send_bytes(self, call_id: str, read, cancel: threading.Event) -> dict:
        import hashlib
        digest, size = hashlib.sha256(), 0
        while block := await asyncio.to_thread(read, ops.CHUNK):
            if cancel.is_set():
                raise ValueError("cancelled")
            digest.update(block)
            size += len(block)
            await self.send({"type": "chunk", "id": call_id, "data": base64.b64encode(block).decode()})
        return {"sha256": digest.hexdigest(), "size": size}

    async def _pull(self, call_id, args, ctx, cancel) -> dict:
        fd, size = await asyncio.to_thread(ops.read_chunks, args, ctx)
        with os.fdopen(fd, "rb") as handle:
            data = await self._send_bytes(call_id, handle.read, cancel)
        if data["size"] != size:
            raise ops.PathChanged("the file changed while it was copied")
        return data

    async def _shot(self, call_id, args, ctx, cancel) -> dict:
        import io
        png = await asyncio.to_thread(ops.take_shot)
        return await self._send_bytes(call_id, io.BytesIO(png).read, cancel)

    async def _push(self, call_id, args, ctx, cancel) -> dict:
        queue = self.uploads[call_id]
        writer = await asyncio.to_thread(ops.PushWriter, args, ctx)
        try:
            await self.send({"type": "credit", "id": call_id, "n": UPLOAD_QUEUE})
            while True:
                message = await asyncio.wait_for(queue.get(), 120)
                if cancel.is_set():
                    raise ValueError("cancelled")
                if message["type"] == "chunk":
                    await self.send({"type": "credit", "id": call_id, "n": 1})
                if message["type"] == "chunk_end":
                    return await asyncio.to_thread(writer.commit)
                try:
                    block = base64.b64decode(message.get("data") or "", validate=True)
                except ValueError as exc:
                    raise ValueError("bad chunk") from exc
                await asyncio.to_thread(writer.write, block)
        finally:
            await asyncio.to_thread(writer.close)


class _Pending(Exception):
    """Paired but not activated in Bot Office yet."""


def run(cfg: Config, device: dict, tokens: TokenStore) -> int:
    agent = Agent(cfg, device, tokens)
    try:
        asyncio.run(agent.run_forever())
    except Revoked as exc:
        print(f"botoffice-agent: {exc}; pair this computer again", flush=True)
        return EXIT_REVOKED
    except KeyboardInterrupt:
        return 0
    return 0
