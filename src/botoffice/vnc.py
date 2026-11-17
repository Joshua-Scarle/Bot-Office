"""Loopback-only desktop discovery and short-lived, single-use VNC tickets."""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from . import isolation

DESKTOP_RE = re.compile(r"^desktop-([0-9]{1,2})$")
TICKET_TTL = 30


def desktop_port(name: str) -> int:
    match = DESKTOP_RE.fullmatch(name)
    if not match or int(match.group(1)) == 0:
        raise ValueError("invalid desktop name")
    return 5900 + int(match.group(1))


def same_origin(origin: str | None, host: str) -> bool:
    if not origin:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and parsed.netloc.lower() == host.lower() and not parsed.path and not parsed.query and not parsed.fragment


async def desktops() -> list[dict]:
    try:
        options, header = isolation.exec_options()
        proc = await asyncio.create_subprocess_exec(
            *isolation.wrap(["podman", "ps", "-a", "--format", "json"], env_stdin=True, runtime_dir=True),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, **options,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(header) if header else proc.communicate(), 5)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return []
        if proc.returncode:
            return []
        data = json.loads(out)
    except (OSError, ValueError):
        return []
    found = []
    for item in data:
        names = item.get("Names", [])
        if isinstance(names, str):
            names = [names]
        for name in names:
            try:
                port = desktop_port(name)
            except (ValueError, TypeError):
                continue
            found.append({"name": name, "port": port, "status": "running" if str(item.get("State", "")).lower() == "running" else "stopped"})
    return found


class Tickets:
    def __init__(self) -> None:
        self._tickets: dict[str, tuple[str, float, str]] = {}

    def issue(self, name: str, mode: str = "view") -> str:
        now = time.monotonic()
        self._tickets = {key: value for key, value in self._tickets.items() if value[1] > now}
        ticket = secrets.token_urlsafe(32)
        self._tickets[ticket] = (name, now + TICKET_TTL, mode)
        return ticket

    def consume(self, ticket: str, name: str) -> str | None:
        entry = self._tickets.pop(ticket, None)
        return entry[2] if entry and entry[0] == name and entry[1] > time.monotonic() else None


class ViewFilter:
    """Frame the two-sided RFB handshake, then discard client input events."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.server_buffer = bytearray()
        self.stage = "server_version"
        self.version = b""
        self.server_version = b""
        self.security_types = b""

    def feed_server(self, data: bytes) -> None:
        if self.stage == "messages" or self.stage == "client_init":
            return
        self.server_buffer.extend(data)
        while self.server_buffer:
            if self.stage == "server_version":
                if len(self.server_buffer) < 12:
                    return
                self.server_version = bytes(self.server_buffer[:12])
                del self.server_buffer[:12]
                if self.server_version not in (b"RFB 003.007\n", b"RFB 003.008\n"):
                    raise ValueError("unsupported RFB server version")
                self.stage = "client_version"
            elif self.stage == "security_types":
                count = self.server_buffer[0]
                if not count:
                    raise ValueError("server rejected RFB connection")
                if len(self.server_buffer) < count + 1:
                    return
                self.security_types = bytes(self.server_buffer[1:count + 1])
                del self.server_buffer[:count + 1]
                if 1 not in self.security_types:
                    raise ValueError("RFB server requires authentication")
                self.stage = "security_choice"
            elif self.stage == "security_result":
                if len(self.server_buffer) < 4:
                    return
                if int.from_bytes(self.server_buffer[:4], "big") != 0:
                    raise ValueError("RFB security negotiation failed")
                del self.server_buffer[:4]
                self.stage = "client_init"
            else:
                raise ValueError("unexpected RFB server handshake data")

    def feed(self, data: bytes) -> bytes:
        self.buffer.extend(data)
        out = bytearray()
        while self.buffer:
            if self.stage == "client_version":
                if len(self.buffer) < 12:
                    break
                self.version = bytes(self.buffer[:12])
                if (self.version not in (b"RFB 003.007\n", b"RFB 003.008\n")
                        or self.version > self.server_version):
                    raise ValueError("unsupported RFB client version")
                out.extend(self.buffer[:12])
                del self.buffer[:12]
                self.stage = "security_types"
            elif self.stage == "security_choice":
                if self.buffer[0] != 1 or 1 not in self.security_types:
                    raise ValueError("unsupported RFB security type")
                out.append(self.buffer.pop(0))
                self.stage = "security_result" if self.version == b"RFB 003.008\n" else "client_init"
            elif self.stage == "client_init":
                out.append(self.buffer.pop(0))
                self.stage = "messages"
            elif self.stage == "messages":
                kind = self.buffer[0]
                fixed = {0: 20, 2: 4, 3: 10, 4: 8, 5: 6, 6: 8, 150: 10, 248: 9, 251: 8}
                if kind not in fixed:
                    raise ValueError("unknown RFB client message")
                length = fixed[kind]
                if len(self.buffer) < length:
                    break
                if kind == 2:
                    length += 4 * int.from_bytes(self.buffer[2:4], "big")
                elif kind == 6:
                    size = abs(int.from_bytes(self.buffer[4:8], "big", signed=True))
                    if size > 1024 * 1024:
                        raise ValueError("RFB clipboard too large")
                    length += size
                elif kind == 248:
                    size = self.buffer[8]
                    if size > 64:
                        raise ValueError("RFB fence too large")
                    length += size
                elif kind == 251:
                    length += 16 * self.buffer[6]
                if len(self.buffer) < length:
                    break
                if kind not in (4, 5, 6, 251):
                    out.extend(self.buffer[:length])
                del self.buffer[:length]
            else:
                raise ValueError("unexpected RFB client handshake data")
        return bytes(out)
