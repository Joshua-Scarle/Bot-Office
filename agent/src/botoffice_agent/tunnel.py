"""Is the local end of the tunnel really ours?

Port 18787 on 127.0.0.1 can be bound by any user of this computer while the SSH tunnel is down.
Before the device token (or a pairing code) is sent there, the agent connects a socket itself and
looks the other end of that very connection up in /proc/net/tcp: the accepting socket must belong
to this user (the ``ssh -L`` this user runs). Anything else is refused; nothing is sent.
"""
from __future__ import annotations

import os
import socket
import time

LOOPBACK_V4 = "0100007F"  # 127.0.0.1 as /proc/net/tcp writes it
LOOPBACK_V6 = ("00000000000000000000000001000000", "0000000000000000FFFF00000100007F")


class TunnelNotOurs(Exception):
    """The local port is held by another user (or cannot be checked)."""


def _owner(tables: list[str], peer_port: int, own_port: int) -> int | None:
    """The uid of the socket whose local end is *peer_port* and remote end *own_port* (both loopback)."""
    for table in tables:
        for line in table.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 8:
                continue
            local, remote = fields[1].rsplit(":", 1), fields[2].rsplit(":", 1)
            if (int(local[1], 16) == peer_port and int(remote[1], 16) == own_port
                    and local[0] in (LOOPBACK_V4, *LOOPBACK_V6) and remote[0] in (LOOPBACK_V4, *LOOPBACK_V6)):
                return int(fields[7])
    return None


def _tables() -> list[str]:
    found = []
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(name, encoding="ascii") as handle:
                found.append(handle.read())
        except OSError:
            continue
    return found


def check(sock: socket.socket, tables: list[str] | None = None, uid: int | None = None, wait: float = 3.0) -> None:
    """The kernel shows the accepted end with uid 0 until the listener calls accept(); then with the
    listener's uid. So wait a moment for a real owner: one that never accepts is refused too."""
    expected = os.getuid() if uid is None else uid
    if expected == 0:  # root cannot tell its listener from one that has not accepted yet (both show 0)
        raise TunnelNotOurs("do not run the agent as root over the plain tunnel; run it as your own user")
    own_port = sock.getsockname()[1]
    peer_port = sock.getpeername()[1]
    deadline = time.monotonic() + wait
    while True:
        owner = _owner(_tables() if tables is None else tables, peer_port, own_port)
        if owner == expected or (owner not in (None, 0)) or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    if owner is None or owner == 0:
        raise TunnelNotOurs("cannot tell who holds the local tunnel port; nothing sent")
    if owner != expected:
        raise TunnelNotOurs(f"the local tunnel port is held by another user (uid {owner}); nothing sent")


def connect(host: str, port: int, timeout: float = 20.0) -> socket.socket:
    """A connected socket to a loopback *host*:*port* whose other end belongs to this user."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise TunnelNotOurs("plain ws/http is only for the local tunnel (127.0.0.1); use wss/https otherwise")
    family = socket.AF_INET6 if host == "::1" else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(("::1" if family == socket.AF_INET6 else "127.0.0.1", port))
        check(sock)
        sock.settimeout(None)
        return sock
    except BaseException:
        sock.close()
        raise
