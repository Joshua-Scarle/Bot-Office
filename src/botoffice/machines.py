"""Paired computers (the device companion): pairing, device tokens, grants and approval binding.

A computer runs ``botoffice-agent`` (agent/ in this repo). It pairs once with a short code from
Settings > Computers, then keeps one outgoing WebSocket to ``/v1/machines/ws`` (machine_hub.py).
Bots reach it only through ``bin/machine`` and the job-scoped ``/v1/agent/machines/*`` routes
(machines_api.py); every call is one approval the owner answers, or matches an "Always" grant.

Security shape (docs/COMPUTERS.md):
- Pairing codes: 8 Crockford base32 characters, 10 minutes, single use, stored as sha256. Every
  wrong try counts against every live code; a code with 5 wrong tries is locked.
- Device token ``bom_<machine id>_<32 random bytes, base64url>``: only its sha256 is stored and it
  is compared in constant time. It works only after the owner clicks Activate with the device's
  fingerprint on screen. Rotation keeps the old token for 5 minutes. Revoke ends it at once. The
  master token is never a device token (other prefix, and the WebSocket takes nothing else).
- Each request stores the canonical arguments (paths resolved by the agent) and a hash binding
  machine, bot, job, tool and those arguments. The approval row carries that hash; execution is
  single use and re-checks the hash, the job and the machine, and for push the content sha256.
"""
from __future__ import annotations

import hashlib
import json
import posixpath
import re
import secrets
import shlex
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing
from datetime import datetime, timedelta

from .store import JobStore, add_columns, now

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 8
CODE_TTL = timedelta(minutes=10)
CODE_MAX_FAILURES = 5
LIVE_CODES_MAX = 5
PAIR_RATE = (10, 60.0)  # unauthenticated pair attempts per window (seconds), all callers together
TOKEN_PREFIX = "bom_"
TOKEN_RE = re.compile(r"bom_([0-9a-f]{12})_([A-Za-z0-9_-]{43})")
ROTATE_OVERLAP = timedelta(minutes=5)
ROTATE_AFTER = timedelta(days=30)
GRANT_TTL = timedelta(days=30)
PENDING_PER_BOT = 20
REQUESTS_PER_MINUTE = 30  # per bot, before any approval is asked
WAIT_CAP = timedelta(hours=24)  # an approval with the Settings timeout at 0 still ends
APPROVED_TTL = timedelta(minutes=10)  # approved but never executed (the helper died)
# Per-device flags the agent follows when its own config says "server". Both start off; flip a
# value here to change the default for newly paired computers.
DEVICE_FLAG_DEFAULTS = {"sandbox": False, "confirm_local": False}
CLASS_KEY = "computer"  # approval_classes: "Controlling a computer"

TOOLS = ("ls", "read", "run", "pull", "push", "shot")
LS_MAX_DEPTH = 3
LS_MAX_ENTRIES = 2000
READ_CHUNK_MAX = 1024 * 1024
READ_FILE_CAP = 16 * 1024 * 1024
RUN_TIMEOUT_DEFAULT = 30
RUN_TIMEOUT_MAX = 300
STREAM_CAP = 64 * 1024
TRANSFER_MAX = 100 * 1024 * 1024
SHOT_MAX = 5 * 1024 * 1024
PATH_MAX = 4096
ARGV_ITEMS_MAX = 256
SUMMARY_MAX = 4000  # rules.APPROVAL_LIMIT: the card must show the whole command
NAME_RE = re.compile(r"[^A-Za-z0-9._ -]")
SHELL_META = re.compile(r"[;|&$()`<>\n\r\\*?\[\]{}~!#]")
SHELLS = frozenset({"sh", "bash", "dash", "zsh", "fish", "ksh", "csh", "tcsh", "busybox", "env", "sudo",
                    "python", "python3", "perl", "ruby", "node", "xargs", "nohup", "timeout", "nice"})


class MachineError(Exception):
    """A refused machine action: *status* is the HTTP status, *detail* what the caller is told."""

    def __init__(self, detail: str, status: int = 409):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def init_tables(db: sqlite3.Connection) -> None:
    """Called from store._init_control_tables (all additive)."""
    db.execute(
        """CREATE TABLE IF NOT EXISTS machines (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, hostname TEXT NOT NULL DEFAULT '',
            os TEXT NOT NULL DEFAULT '', agent_version TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending', token_hash TEXT, prev_token_hash TEXT,
            prev_expires_at TEXT, token_issued_at TEXT, sandbox INTEGER, confirm_local INTEGER,
            local_flags TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, activated_at TEXT,
            revoked_at TEXT, last_seen_at TEXT
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS machine_pairing_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, code_hash TEXT NOT NULL UNIQUE, name TEXT NOT NULL DEFAULT '',
            failures INTEGER NOT NULL DEFAULT 0, expires_at TEXT NOT NULL, used_at TEXT, machine_id TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS machine_grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, machine_id TEXT NOT NULL,
            tool TEXT NOT NULL, pattern TEXT NOT NULL, approval_id INTEGER, created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL, revoked_at TEXT
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS machine_grants_lookup ON machine_grants(bot_id, machine_id, tool)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS machine_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, machine_id TEXT NOT NULL, bot_id TEXT NOT NULL,
            job_id TEXT NOT NULL, tool TEXT NOT NULL, args_json TEXT NOT NULL, binding_hash TEXT NOT NULL,
            summary TEXT NOT NULL, approval_id INTEGER, grant_id INTEGER, status TEXT NOT NULL,
            error TEXT, result_json TEXT, expires_at TEXT, created_at TEXT NOT NULL, decided_at TEXT,
            executed_at TEXT, finished_at TEXT
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS machine_requests_job ON machine_requests(job_id)")
    db.execute("CREATE INDEX IF NOT EXISTS machine_requests_approval ON machine_requests(approval_id)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS machine_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, machine_id TEXT, bot_id TEXT, job_id TEXT,
            request_id INTEGER, event TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS machine_audit_machine ON machine_audit(machine_id, id)")
    add_columns(db, "approvals", (("machine_id", "TEXT"),))


def _stamp() -> str:
    return now().isoformat()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _clean(text: object, limit: int) -> str:
    """Printable, single-line, at most *limit* characters (hostnames, OS names from the device)."""
    value = "".join(char for char in str(text or "") if char.isprintable())
    return " ".join(value.split())[:limit]


def audit(db: sqlite3.Connection, event: str, *, machine_id: str | None = None, bot_id: str | None = None,
          job_id: str | None = None, request_id: int | None = None, detail: str = "") -> None:
    db.execute("INSERT INTO machine_audit (machine_id,bot_id,job_id,request_id,event,detail,created_at) "
               "VALUES (?,?,?,?,?,?,?)", (machine_id, bot_id, job_id, request_id, event, detail[:2000], _stamp()))


def _tx(store: JobStore):
    """A connection inside BEGIN IMMEDIATE; commit on success, roll back on any error."""
    class _Tx:
        def __enter__(self):
            self.db = store._connect()
            self.db.execute("BEGIN IMMEDIATE")
            return self.db

        def __exit__(self, kind, value, tb):
            try:
                if kind is None:
                    self.db.commit()
                else:
                    self.db.rollback()
            finally:
                self.db.close()
            return False
    return _Tx()


# --- pairing ---

class RateLimit:
    """At most *count* events per *window* seconds (all callers: the API sits behind one tunnel)."""

    def __init__(self, count: int, window: float):
        self.count, self.window = count, window
        self.events: deque[float] = deque()
        self.lock = threading.Lock()

    def allow(self) -> bool:
        stamp = time.monotonic()
        with self.lock:
            while self.events and stamp - self.events[0] > self.window:
                self.events.popleft()
            if len(self.events) >= self.count:
                return False
            self.events.append(stamp)
            return True


def _code_hash(code: str) -> str:
    return _sha("botoffice-pair:" + code)


def normalize_code(raw: object) -> str | None:
    """Crockford decoding: case-insensitive, I/L read as 1, O as 0, dashes and spaces ignored."""
    if not isinstance(raw, str) or len(raw) > 32:
        return None
    code = raw.upper().replace("-", "").replace(" ", "").translate(str.maketrans("ILO", "110"))
    if len(code) != CODE_LENGTH or any(char not in CROCKFORD for char in code):
        return None
    return code


def create_code(store: JobStore, name: str = "") -> dict:
    code = "".join(secrets.choice(CROCKFORD) for _ in range(CODE_LENGTH))
    stamp = now()
    expires = stamp + CODE_TTL
    with _tx(store) as db:
        live = db.execute("SELECT COUNT(*) FROM machine_pairing_codes WHERE used_at IS NULL AND expires_at>? "
                          "AND failures<?", (stamp.isoformat(), CODE_MAX_FAILURES)).fetchone()[0]
        if live >= LIVE_CODES_MAX:
            raise MachineError(f"{LIVE_CODES_MAX} pairing codes are already open; use one or wait 10 minutes")
        db.execute("INSERT INTO machine_pairing_codes (code_hash,name,expires_at,created_at) VALUES (?,?,?,?)",
                   (_code_hash(code), _clean(name, 60), expires.isoformat(), stamp.isoformat()))
        audit(db, "code_created", detail=_clean(name, 60))
    return {"code": code[:4] + "-" + code[4:], "expires_at": expires.isoformat(), "ttl_seconds": int(CODE_TTL.total_seconds())}


def token_hash(token: str) -> str:
    return _sha(token)


def fingerprint(digest: str | None) -> str:
    """What both the laptop (``botoffice-agent status``) and Settings show before Activate."""
    if not digest:
        return ""
    value = _sha("botoffice-device-fp:" + digest)[:16]
    return "-".join(value[i:i + 4] for i in range(0, 16, 4))


def _new_token(machine_id: str) -> str:
    return f"{TOKEN_PREFIX}{machine_id}_{secrets.token_urlsafe(32)}"


def _unique_name(db: sqlite3.Connection, wanted: str) -> str:
    base = _clean(NAME_RE.sub("", wanted), 40).strip() or "computer"
    taken = {row[0].lower() for row in db.execute("SELECT name FROM machines WHERE status<>'revoked'")}
    name, number = base, 2
    while name.lower() in taken:
        name, number = f"{base}-{number}", number + 1
    return name


def redeem(store: JobStore, raw_code: object, hostname: object, os_name: object, version: object) -> dict:
    """POST /v1/machines/pair: a live code becomes a *pending* machine and its token (shown once)."""
    code = normalize_code(raw_code)
    stamp = now().isoformat()
    with _tx(store) as db:
        row = db.execute("SELECT * FROM machine_pairing_codes WHERE code_hash=? AND used_at IS NULL AND expires_at>? "
                         "AND failures<?", (_code_hash(code), stamp, CODE_MAX_FAILURES)).fetchone() if code else None
        if row is None:
            # a wrong try counts against every live code: guessing 40 bits in 5 tries is hopeless
            db.execute("UPDATE machine_pairing_codes SET failures=failures+1 WHERE used_at IS NULL AND expires_at>?",
                       (stamp,))
            audit(db, "pair_failed")
            failed = True
        else:
            failed = False
            machine_id = secrets.token_hex(6)
            token = _new_token(machine_id)
            host = _clean(hostname, 64) or "unknown"
            name = _unique_name(db, row["name"] or host)
            db.execute("UPDATE machine_pairing_codes SET used_at=?, machine_id=? WHERE id=?", (stamp, machine_id, row["id"]))
            db.execute("INSERT INTO machines (id,name,hostname,os,agent_version,status,token_hash,token_issued_at,"
                       "sandbox,confirm_local,created_at) VALUES (?,?,?,?,?,'pending',?,?,?,?,?)",
                       (machine_id, name, host, _clean(os_name, 120), _clean(version, 40), token_hash(token), stamp,
                        int(DEVICE_FLAG_DEFAULTS["sandbox"]), int(DEVICE_FLAG_DEFAULTS["confirm_local"]), stamp))
            audit(db, "paired", machine_id=machine_id, detail=f"host {host}; waiting for Activate")
    if failed:
        raise MachineError("invalid, used, expired or locked pairing code", 403)
    return {"machine_id": machine_id, "token": token, "fingerprint": fingerprint(token_hash(token)), "name": name,
            "status": "pending"}


def authenticate(store: JobStore, token: object) -> tuple[dict | None, str]:
    """(machine, "ok") for a working device token, else (None, reason): invalid, not_activated, revoked.
    Only bom_ tokens of a known machine are looked at; the master or an agent token never matches."""
    if not isinstance(token, str):
        return None, "invalid"
    found = TOKEN_RE.fullmatch(token)
    if not found:
        return None, "invalid"
    digest = token_hash(token)
    with closing(store._connect()) as db:
        row = db.execute("SELECT * FROM machines WHERE id=?", (found.group(1),)).fetchone()
    if row is None:
        secrets.compare_digest(digest, digest)  # the same work either way
        return None, "invalid"
    current = bool(row["token_hash"]) and secrets.compare_digest(row["token_hash"], digest)
    previous = (bool(row["prev_token_hash"]) and bool(row["prev_expires_at"])
                and secrets.compare_digest(row["prev_token_hash"], digest)
                and row["prev_expires_at"] > now().isoformat())
    if not (current or previous):
        return None, "invalid"
    if row["status"] == "revoked":
        return None, "revoked"
    if row["status"] != "active":
        return None, "not_activated"
    return dict(row), "ok"


def get_machine(store: JobStore, machine_id: str) -> dict | None:
    with closing(store._connect()) as db:
        row = db.execute("SELECT * FROM machines WHERE id=?", (machine_id,)).fetchone()
    return dict(row) if row else None


def device_flags(machine: dict) -> dict:
    return {key: bool(machine[key]) if machine.get(key) is not None else default
            for key, default in DEVICE_FLAG_DEFAULTS.items()}


def public(machine: dict, online: bool = False) -> dict:
    """What Settings > Computers shows (never a token hash)."""
    try:
        local = json.loads(machine.get("local_flags") or "{}")
    except ValueError:
        local = {}
    return {"id": machine["id"], "name": machine["name"], "hostname": machine["hostname"], "os": machine["os"],
            "agent_version": machine["agent_version"], "status": machine["status"], "online": online,
            "last_seen_at": machine["last_seen_at"], "created_at": machine["created_at"],
            "activated_at": machine["activated_at"], "revoked_at": machine["revoked_at"],
            "token_issued_at": machine["token_issued_at"], "fingerprint": fingerprint(machine["token_hash"]),
            "flags": device_flags(machine), "local_flags": local if isinstance(local, dict) else {}}


def list_machines(store: JobStore, online: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    with closing(store._connect()) as db:
        rows = db.execute("SELECT * FROM machines ORDER BY status='revoked', created_at").fetchall()
    return [public(dict(row), row["id"] in online) for row in rows]


def activate(store: JobStore, machine_id: str, shown_fingerprint: object) -> dict:
    with _tx(store) as db:
        row = db.execute("SELECT * FROM machines WHERE id=?", (machine_id,)).fetchone()
        if row is None:
            raise MachineError("computer not found", 404)
        if row["status"] != "pending":
            raise MachineError(f"this computer is {row['status']}")
        if not isinstance(shown_fingerprint, str) or not secrets.compare_digest(
                shown_fingerprint.encode(), fingerprint(row["token_hash"]).encode()):
            raise MachineError("the fingerprint changed; reload Settings and compare it with the computer again")
        db.execute("UPDATE machines SET status='active', activated_at=? WHERE id=?", (_stamp(), machine_id))
        audit(db, "activated", machine_id=machine_id)
    return get_machine(store, machine_id)


def revoke(store: JobStore, machine_id: str) -> dict:
    """Ends the token(s) and every grant; pending requests are denied. The hub closes the socket."""
    stamp = _stamp()
    with _tx(store) as db:
        row = db.execute("SELECT * FROM machines WHERE id=?", (machine_id,)).fetchone()
        if row is None:
            raise MachineError("computer not found", 404)
        db.execute("UPDATE machines SET status='revoked', revoked_at=COALESCE(revoked_at, ?), token_hash=NULL, "
                   "prev_token_hash=NULL, prev_expires_at=NULL WHERE id=?", (stamp, machine_id))
        db.execute("UPDATE machine_grants SET revoked_at=? WHERE machine_id=? AND revoked_at IS NULL", (stamp, machine_id))
        db.execute("UPDATE approvals SET status='denied', decided_at=? WHERE machine_id=? AND status IN ('pending','approved')",
                   (stamp, machine_id))
        db.execute("UPDATE machine_requests SET status='cancelled', error='computer revoked', finished_at=? "
                   "WHERE machine_id=? AND status IN ('pending','approved')", (stamp, machine_id))
        audit(db, "revoked", machine_id=machine_id)
    return get_machine(store, machine_id)


def rotate(store: JobStore, machine_id: str) -> str:
    """A new token for an active machine; the current one keeps working for ROTATE_OVERLAP."""
    stamp = now()
    with _tx(store) as db:
        row = db.execute("SELECT * FROM machines WHERE id=?", (machine_id,)).fetchone()
        if row is None or row["status"] != "active":
            raise MachineError("only an active computer can rotate its token")
        if row["prev_expires_at"] and row["prev_expires_at"] > stamp.isoformat():
            # a second rotation inside the overlap would drop the token the agent may still hold
            raise MachineError("the token was rotated less than 5 minutes ago")
        token = _new_token(machine_id)
        db.execute("UPDATE machines SET prev_token_hash=token_hash, prev_expires_at=?, token_hash=?, token_issued_at=? "
                   "WHERE id=? AND status='active'",
                   ((stamp + ROTATE_OVERLAP).isoformat(), token_hash(token), stamp.isoformat(), machine_id))
        audit(db, "rotated", machine_id=machine_id)
    return token


def needs_rotation(machine: dict) -> bool:
    issued = machine.get("token_issued_at")
    return bool(issued) and datetime.fromisoformat(issued) + ROTATE_AFTER <= now()


def update_machine(store: JobStore, machine_id: str, *, name: str | None = None, sandbox: bool | None = None,
                   confirm_local: bool | None = None) -> dict:
    with _tx(store) as db:
        row = db.execute("SELECT * FROM machines WHERE id=?", (machine_id,)).fetchone()
        if row is None:
            raise MachineError("computer not found", 404)
        if name is not None:
            clean = _clean(NAME_RE.sub("", name), 40).strip()
            if not clean:
                raise MachineError("a name is 1 to 40 letters, digits, spaces, dots, dashes or underscores", 400)
            if db.execute("SELECT 1 FROM machines WHERE lower(name)=lower(?) AND id<>? AND status<>'revoked'",
                          (clean, machine_id)).fetchone():
                raise MachineError("another computer has that name", 400)
            db.execute("UPDATE machines SET name=? WHERE id=?", (clean, machine_id))
        for key, value in (("sandbox", sandbox), ("confirm_local", confirm_local)):
            if value is not None:
                db.execute(f"UPDATE machines SET {key}=? WHERE id=?", (int(bool(value)), machine_id))
                audit(db, "flag", machine_id=machine_id, detail=f"{key}={'on' if value else 'off'}")
    return get_machine(store, machine_id)


def seen(store: JobStore, machine_id: str, *, os_name: object = None, version: object = None,
         local_flags: object = None) -> None:
    with closing(store._connect()) as db:
        db.execute("UPDATE machines SET last_seen_at=? WHERE id=?", (_stamp(), machine_id))
        if os_name is not None:
            db.execute("UPDATE machines SET os=?, agent_version=? WHERE id=?",
                       (_clean(os_name, 120), _clean(version, 40), machine_id))
        if isinstance(local_flags, dict):
            flags = {key: value for key, value in local_flags.items()
                     if key in DEVICE_FLAG_DEFAULTS and value in ("on", "off", "server")}
            db.execute("UPDATE machines SET local_flags=? WHERE id=?", (json.dumps(flags), machine_id))


def log_event(store: JobStore, event: str, **fields) -> None:
    with closing(store._connect()) as db:
        audit(db, event, **fields)


def audit_log(store: JobStore, machine_id: str, limit: int = 200) -> list[dict]:
    with closing(store._connect()) as db:
        return [dict(row) for row in db.execute("SELECT * FROM machine_audit WHERE machine_id=? ORDER BY id DESC LIMIT ?",
                                                (machine_id, max(1, min(limit, 1000)))).fetchall()]


def find_for_bot(store: JobStore, ref: str | None) -> dict:
    """The active machine a bot named (id or name); with no name, the only active one."""
    with closing(store._connect()) as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM machines WHERE status='active' ORDER BY created_at")]
    if ref:
        for row in rows:
            if row["id"] == ref or row["name"].lower() == str(ref).lower():
                return row
        raise MachineError("no active computer with that name or id (machine list)", 404)
    if len(rows) == 1:
        return rows[0]
    raise MachineError("no active computer" if not rows else "more than one computer: pass --machine NAME", 404 if not rows else 400)


# --- arguments ---

def _path(value: object, what: str = "path") -> str:
    if not isinstance(value, str) or not value or len(value) > PATH_MAX or "\x00" in value or "\n" in value:
        raise MachineError(f"{what} must be 1 to {PATH_MAX} characters, one line", 400)
    return value


def _int(value: object, default: int, low: int, high: int, what: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise MachineError(f"{what} must be a whole number from {low} to {high}", 400)
    return value


def _bool(value: object, what: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise MachineError(f"{what} must be true or false", 400)
    return value


def validate_args(tool: str, raw: object) -> dict:
    """The bot's arguments, checked and with defaults, before the agent resolves paths."""
    if tool not in TOOLS:
        raise MachineError(f"unknown tool {tool!r}; one of {', '.join(TOOLS)}", 400)
    if not isinstance(raw, dict):
        raise MachineError("args must be an object", 400)
    if tool == "ls":
        return {"path": _path(raw.get("path")), "depth": _int(raw.get("depth"), 1, 1, LS_MAX_DEPTH, "depth")}
    if tool == "read":
        return {"path": _path(raw.get("path")), "offset": _int(raw.get("offset"), 0, 0, READ_FILE_CAP, "offset"),
                "length": _int(raw.get("length"), READ_CHUNK_MAX, 1, READ_CHUNK_MAX, "length"),
                "b64": _bool(raw.get("b64"), "b64")}
    if tool == "run":
        argv, sh = raw.get("argv"), raw.get("sh")
        if (argv is None) == (sh is None):
            raise MachineError("run takes argv (a list) or sh (one shell string), not both", 400)
        if argv is not None:
            if (not isinstance(argv, list) or not 1 <= len(argv) <= ARGV_ITEMS_MAX
                    or not all(isinstance(item, str) and "\x00" not in item and len(item) <= PATH_MAX for item in argv)
                    or not argv[0]):
                raise MachineError(f"argv is 1 to {ARGV_ITEMS_MAX} strings without NUL", 400)
        elif not isinstance(sh, str) or not sh.strip() or "\x00" in sh or len(sh) > SUMMARY_MAX:
            raise MachineError(f"sh is one command string, at most {SUMMARY_MAX} characters", 400)
        return {"argv": argv, "sh": sh, "cwd": _path(raw.get("cwd") or "~", "cwd"),
                "timeout": _int(raw.get("timeout"), RUN_TIMEOUT_DEFAULT, 1, RUN_TIMEOUT_MAX, "timeout")}
    if tool == "pull":
        return {"path": _path(raw.get("path")), "dest": _path(raw.get("dest"), "dest")}
    if tool == "push":
        digest = raw.get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise MachineError("sha256 must be 64 lowercase hex characters", 400)
        return {"path": _path(raw.get("path")), "source": _path(raw.get("source"), "source"),
                "size": _int(raw.get("size"), -1, 0, TRANSFER_MAX, "size"), "sha256": digest,
                "overwrite": _bool(raw.get("overwrite"), "overwrite")}
    return {"dest": _path(raw.get("dest"), "dest")}  # shot


RESOLVE_KIND = {"ls": "dir", "read": "file", "pull": "file", "push": "parent", "run": "dir"}


def canonical_args(tool: str, args: dict, resolved: dict | None) -> dict:
    """*args* with the agent's canonical path (cwd for run) in place of the bot's spelling."""
    out = dict(args)
    if resolved is None:
        return out
    canonical = resolved.get("canonical")
    if not isinstance(canonical, str) or not canonical.startswith("/") or "\x00" in canonical or len(canonical) > PATH_MAX:
        raise MachineError("the computer returned an invalid path", 502)
    size = resolved.get("size")
    if tool == "run":
        out["cwd"] = canonical
    else:
        out["requested"] = out["path"]
        out["path"] = canonical
    if tool == "read":
        if not isinstance(size, int) or size > READ_FILE_CAP:
            raise MachineError(f"read is for files up to {READ_FILE_CAP // (1024 * 1024)} MiB; use pull", 400)
        out["file_size"] = size
    if tool == "pull":
        if not isinstance(size, int) or size > TRANSFER_MAX:
            raise MachineError(f"pull is for files up to {TRANSFER_MAX // (1024 * 1024)} MiB", 400)
        out["file_size"] = size
    if tool == "push":
        out["exists"] = bool(resolved.get("exists"))
        if out["exists"] and not out["overwrite"]:
            raise MachineError("the destination exists; pass --overwrite to replace it", 409)
    return out


def binding_hash(machine_id: str, bot_id: str, job_id: str, tool: str, args: dict) -> str:
    blob = json.dumps({"v": 1, "machine": machine_id, "bot": bot_id, "job": job_id, "tool": tool, "args": args},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return _sha(blob)


def _size(value: int) -> str:
    for unit in ("bytes", "KiB", "MiB"):
        if value < 1024 or unit == "MiB":
            return f"{value} {unit}" if unit == "bytes" else f"{value / 1024:.1f} {unit}"
        value /= 1024
    return str(value)


def describe(tool: str, args: dict, machine: dict) -> str:
    """The approval's whole text: what runs where, with canonical paths."""
    where = f"on {machine['name']} ({machine['hostname']})"
    if tool == "ls":
        return f"list {args['path']} (depth {args['depth']}) {where}"
    if tool == "read":
        return (f"read {args['path']} {where}: bytes {args['offset']}-{args['offset'] + args['length']} of "
                f"{_size(args.get('file_size', 0))}{' as base64' if args['b64'] else ''}")
    if tool == "run":
        command = shlex.join(args["argv"]) if args["argv"] is not None else f"sh -c {shlex.quote(args['sh'])}"
        return f"run {where} in {args['cwd']} (timeout {args['timeout']} s): {command}"
    if tool == "pull":
        return f"copy {args['path']} ({_size(args.get('file_size', 0))}) {where} into the bot's workspace as {args['dest']}"
    if tool == "push":
        return (f"copy {args['source']} ({_size(args['size'])}, sha256 {args['sha256']}) to {args['path']} {where}"
                f"{' replacing the file there' if args['overwrite'] else ''}")
    return f"take a screenshot {where} into the bot's workspace as {args['dest']}"


# --- grants ("Always for this bot") ---

def glob_regex(pattern: str) -> re.Pattern:
    """``*`` and ``?`` stay inside one path component, ``**`` spans any; a trailing ``/**`` also
    matches the directory itself. Everything else is literal."""
    tail = ""
    if pattern.endswith("/**"):
        pattern, tail = pattern[:-3], "(?:/.*)?"
    out, index = [], 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out) + tail, re.DOTALL)


def check_pattern(pattern: object, confirm_broad: bool = False) -> str:
    if not isinstance(pattern, str):
        raise MachineError("a pattern is an absolute path, optionally with * ? **", 400)
    pattern = pattern.strip()
    if (not pattern.startswith("/") or len(pattern) > 500 or "\x00" in pattern or "\n" in pattern
            or "//" in pattern or (len(pattern) > 1 and pattern.endswith("/") and not pattern.endswith("/**"))):
        raise MachineError("a pattern is an absolute path, optionally with * ? **", 400)
    parts = pattern.split("/")[1:]
    if any(part in (".", "..") for part in parts):
        raise MachineError("a pattern has no . or .. parts", 400)
    fixed = 0
    for part in parts:
        if any(char in part for char in "*?"):
            break
        fixed += 1
    if fixed == 0:
        raise MachineError("a pattern must start with a fixed directory, not a wildcard", 400)
    if fixed < 3 and any(char in pattern for char in "*?") and not confirm_broad:
        raise MachineError("this pattern covers a lot of the computer; confirm it to save it", 428)
    return pattern


def run_pattern(args: dict) -> str:
    """The exact command a run grant allows: argv and cwd, nothing looser."""
    if args.get("argv") is None:
        raise MachineError("a shell string (sh) cannot be allowed always; approve it once", 400)
    argv = args["argv"]
    if any(SHELL_META.search(item) for item in argv) or posixpath.basename(argv[0]) in SHELLS:
        raise MachineError("a command with shell characters or a shell/interpreter launcher cannot be allowed always", 400)
    return json.dumps({"argv": argv, "cwd": args["cwd"]}, sort_keys=True, separators=(",", ":"))


def grant_matches(tool: str, pattern: str, args: dict) -> bool:
    try:
        if tool == "run":
            return args.get("argv") is not None and pattern == run_pattern(args)
        if tool == "shot":
            return pattern == "screen"
        if tool == "push":
            if args.get("overwrite"):
                return False  # replacing a file always asks
            return glob_regex(pattern).fullmatch(posixpath.dirname(args["path"])) is not None
        return glob_regex(pattern).fullmatch(args["path"]) is not None
    except (MachineError, KeyError, TypeError, re.error):
        return False


def _live_grant(db: sqlite3.Connection, bot_id: str, machine_id: str, tool: str, args: dict) -> dict | None:
    rows = db.execute("SELECT * FROM machine_grants WHERE bot_id=? AND machine_id=? AND tool=? AND revoked_at IS NULL "
                      "AND expires_at>? ORDER BY id", (bot_id, machine_id, tool, _stamp())).fetchall()
    return next((dict(row) for row in rows if grant_matches(tool, row["pattern"], args)), None)


def list_grants(store: JobStore, machine_id: str | None = None) -> list[dict]:
    with closing(store._connect()) as db:
        rows = db.execute(
            "SELECT g.*, b.name AS bot_name, m.name AS machine_name FROM machine_grants g "
            "LEFT JOIN bots b ON b.id=g.bot_id LEFT JOIN machines m ON m.id=g.machine_id "
            "WHERE g.revoked_at IS NULL AND g.expires_at>? AND (? IS NULL OR g.machine_id=?) ORDER BY g.id DESC",
            (_stamp(), machine_id, machine_id)).fetchall()
    return [dict(row) for row in rows]


def revoke_grant(store: JobStore, grant_id: int) -> dict:
    with _tx(store) as db:
        row = db.execute("SELECT * FROM machine_grants WHERE id=?", (grant_id,)).fetchone()
        if row is None:
            raise MachineError("grant not found", 404)
        db.execute("UPDATE machine_grants SET revoked_at=COALESCE(revoked_at, ?) WHERE id=?", (_stamp(), grant_id))
        audit(db, "grant_revoked", machine_id=row["machine_id"], bot_id=row["bot_id"], detail=f"#{grant_id} {row['tool']}")
        return dict(db.execute("SELECT * FROM machine_grants WHERE id=?", (grant_id,)).fetchone())


def suggested_pattern(tool: str, args: dict) -> str | None:
    if tool == "run":
        try:
            return run_pattern(args)
        except MachineError:
            return None
    if tool == "shot":
        return "screen"
    if tool == "push":
        return posixpath.dirname(args["path"]) + "/**"
    if tool == "ls":
        return args["path"].rstrip("/") + "/**" if args["path"] != "/" else None
    return posixpath.dirname(args["path"]) + "/**"


# --- requests and their approvals ---

def _wait_minutes(store: JobStore) -> int:
    from . import approval_classes
    return approval_classes.timeout_min(store)


def class_enabled(store: JobStore) -> bool:
    from . import approval_classes
    return any(item.key == CLASS_KEY for item in approval_classes.active(store))


_request_times: dict[str, deque[float]] = {}
_request_lock = threading.Lock()


def request_allowed(bot_id: str) -> bool:
    stamp = time.monotonic()
    with _request_lock:
        times = _request_times.setdefault(bot_id, deque())
        while times and stamp - times[0] > 60:
            times.popleft()
        if len(times) >= REQUESTS_PER_MINUTE:
            return False
        times.append(stamp)
        return True


def record_request(store: JobStore, job, machine: dict, tool: str, args: dict) -> dict:
    """Store the request with its binding; an approval card unless a live grant covers it."""
    bot_id, job_id = str(job.bot_id), str(job.id)
    summary = describe(tool, args, machine)
    if len(summary) > SUMMARY_MAX:
        raise MachineError(f"this {tool} is {len(summary)} characters, over the {SUMMARY_MAX} you can review in an "
                           "approval; write a script on the computer first or split it", 400)
    digest = binding_hash(machine["id"], bot_id, job_id, tool, args)
    stamp = now()
    minutes = _wait_minutes(store)
    expires = stamp + (min(timedelta(minutes=minutes), WAIT_CAP) if minutes else WAIT_CAP)
    with _tx(store) as db:
        grant = _live_grant(db, bot_id, machine["id"], tool, args)
        if grant is not None:
            cursor = db.execute(
                "INSERT INTO machine_requests (machine_id,bot_id,job_id,tool,args_json,binding_hash,summary,grant_id,"
                "status,expires_at,created_at,decided_at) VALUES (?,?,?,?,?,?,?,?,'approved',?,?,?)",
                (machine["id"], bot_id, job_id, tool, json.dumps(args, sort_keys=True), digest, summary, grant["id"],
                 (stamp + APPROVED_TTL).isoformat(), stamp.isoformat(), stamp.isoformat()))
            request_id = int(cursor.lastrowid)
            audit(db, "granted", machine_id=machine["id"], bot_id=bot_id, job_id=job_id, request_id=request_id,
                  detail=f"grant #{grant['id']}: {summary}")
            approval_id = None
        else:
            pending = db.execute("SELECT COUNT(*) FROM approvals WHERE bot_id=? AND source='machine' AND status='pending'",
                                 (bot_id,)).fetchone()[0]
            if pending >= PENDING_PER_BOT:
                raise MachineError(f"{PENDING_PER_BOT} computer approvals of this bot are already waiting", 429)
            cursor = db.execute(
                "INSERT INTO approvals (bot_id,job_id,assignment_id,tool,summary,input_hash,scope,source,preset,status,"
                "expires_at,created_at,machine_id) VALUES (?,?,?,?,?,?,?,'machine',?,'pending',?,?,?)",
                (bot_id, job_id, str(job.assignment_id) if job.assignment_id else None, f"machine.{tool}", summary,
                 "m1:" + digest, "attended", CLASS_KEY, expires.isoformat(), stamp.isoformat(), machine["id"]))
            approval_id = int(cursor.lastrowid)
            cursor = db.execute(
                "INSERT INTO machine_requests (machine_id,bot_id,job_id,tool,args_json,binding_hash,summary,approval_id,"
                "status,expires_at,created_at) VALUES (?,?,?,?,?,?,?,?,'pending',?,?)",
                (machine["id"], bot_id, job_id, tool, json.dumps(args, sort_keys=True), digest, summary, approval_id,
                 expires.isoformat(), stamp.isoformat()))
            request_id = int(cursor.lastrowid)
            short = summary if len(summary) <= 160 else summary[:160] + "…"
            db.execute("INSERT INTO messages (bot_id,role,source,text,created_at,approval_id) VALUES (?,?,?,?,?,?)",
                       (bot_id, "system", "chat", f"Approval #{approval_id} requested: {short}", stamp.isoformat(),
                        approval_id))
            audit(db, "requested", machine_id=machine["id"], bot_id=bot_id, job_id=job_id, request_id=request_id,
                  detail=f"approval #{approval_id}: {summary}")
    return get_request(store, request_id)


def get_request(store: JobStore, request_id: int) -> dict | None:
    with closing(store._connect()) as db:
        row = db.execute("SELECT * FROM machine_requests WHERE id=?", (request_id,)).fetchone()
    return dict(row) if row else None


def request_view(item: dict) -> dict:
    return {"id": item["id"], "status": item["status"], "tool": item["tool"], "summary": item["summary"],
            "approval_id": item["approval_id"], "grant_id": item["grant_id"], "error": item["error"],
            "expires_at": item["expires_at"], "machine_id": item["machine_id"]}


def expire(store: JobStore) -> int:
    """Pending past their timeout, or whose job no longer runs: expired (a deny). Approved but never
    executed within APPROVED_TTL: expired too."""
    stamp = _stamp()
    notes: list[tuple[str, int, str]] = []
    with _tx(store) as db:
        rows = db.execute(
            "SELECT r.* FROM machine_requests r LEFT JOIN jobs j ON j.id=r.job_id WHERE r.status IN ('pending','approved') "
            "AND (r.expires_at<=? OR j.status IS NULL OR j.status<>'running')", (stamp,)).fetchall()
        for row in rows:
            db.execute("UPDATE machine_requests SET status='expired', finished_at=? WHERE id=? AND status=?",
                       (stamp, row["id"], row["status"]))
            if row["approval_id"]:
                if db.execute("UPDATE approvals SET status='expired', decided_at=COALESCE(decided_at, ?) WHERE id=? "
                              "AND status IN ('pending','approved')", (stamp, row["approval_id"])).rowcount \
                        and row["status"] == "pending":
                    notes.append((row["bot_id"], row["approval_id"], row["summary"]))
            audit(db, "expired", machine_id=row["machine_id"], bot_id=row["bot_id"], job_id=row["job_id"],
                  request_id=row["id"])
    for bot_id, approval_id, summary in notes:
        short = summary if len(summary) <= 160 else summary[:160] + "…"
        store.add_message(bot_id, "system", f"Approval #{approval_id} got no answer in time, so it was denied: {short}",
                          source="chat")
    return len(rows)


def is_machine_approval(store: JobStore, approval_id: int) -> bool:
    with closing(store._connect()) as db:
        row = db.execute("SELECT source FROM approvals WHERE id=?", (approval_id,)).fetchone()
    return row is not None and row["source"] == "machine"


def _decide(db: sqlite3.Connection, approval_id: int, status: str) -> dict:
    stamp = _stamp()
    row = db.execute("SELECT * FROM approvals WHERE id=? AND source='machine'", (approval_id,)).fetchone()
    if row is None:
        raise MachineError("approval not found", 404)
    request = db.execute("SELECT * FROM machine_requests WHERE approval_id=?", (approval_id,)).fetchone()
    if request is None:
        raise MachineError("approval not found", 404)
    if row["status"] != "pending" or request["status"] != "pending":
        raise MachineError(f"approval is {row['status']}")
    if status == "approved":
        if row["expires_at"] and row["expires_at"] <= stamp:
            raise MachineError("approval is expired: the bot has stopped waiting")
        job = db.execute("SELECT status FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
        if job is None or job["status"] != "running":
            raise MachineError("the bot's turn has ended; it can ask again")
        machine = db.execute("SELECT status FROM machines WHERE id=?", (request["machine_id"],)).fetchone()
        if machine is None or machine["status"] != "active":
            raise MachineError("the computer is not active")
    db.execute("UPDATE approvals SET status=?, decided_at=? WHERE id=? AND status='pending'", (status, stamp, approval_id))
    db.execute("UPDATE machine_requests SET status=?, decided_at=?, expires_at=? WHERE id=? AND status='pending'",
               (status, stamp, (now() + APPROVED_TTL).isoformat() if status == "approved" else request["expires_at"],
                request["id"]))
    audit(db, status, machine_id=request["machine_id"], bot_id=request["bot_id"], job_id=request["job_id"],
          request_id=request["id"], detail=f"approval #{approval_id}")
    return dict(request)


def approve(store: JobStore, approval_id: int) -> dict:
    with _tx(store) as db:
        _decide(db, approval_id, "approved")
    return approval_dict(store, approval_id)


def deny(store: JobStore, approval_id: int) -> dict:
    with _tx(store) as db:
        _decide(db, approval_id, "denied")
    return approval_dict(store, approval_id)


def approve_always(store: JobStore, approval_id: int, pattern: object = None, confirm_broad: bool = False) -> dict:
    """Approve once and save an "Always" grant for this bot, computer and tool (30 days)."""
    with _tx(store) as db:
        row = db.execute("SELECT * FROM machine_requests WHERE approval_id=?", (approval_id,)).fetchone()
        if row is None:
            raise MachineError("approval not found", 404)
        args = json.loads(row["args_json"])
        tool = row["tool"]
        if tool == "run":
            saved = run_pattern(args)
        elif tool == "shot":
            saved = "screen"
        else:
            saved = check_pattern(pattern if pattern is not None else suggested_pattern(tool, args), confirm_broad)
            if not grant_matches(tool, saved, {**args, "overwrite": False}):
                raise MachineError("the pattern does not cover this request", 400)
        request = _decide(db, approval_id, "approved")
        stamp = now()
        cursor = db.execute("INSERT INTO machine_grants (bot_id,machine_id,tool,pattern,approval_id,created_at,expires_at) "
                            "VALUES (?,?,?,?,?,?,?)", (request["bot_id"], request["machine_id"], tool, saved, approval_id,
                                                       stamp.isoformat(), (stamp + GRANT_TTL).isoformat()))
        audit(db, "grant_created", machine_id=request["machine_id"], bot_id=request["bot_id"],
              detail=f"#{cursor.lastrowid} {tool} {saved}")
    return approval_dict(store, approval_id)


def approval_dict(store: JobStore, approval_id: int) -> dict:
    with closing(store._connect()) as db:
        row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
    return dict(row) if row else {}


def cancel(store: JobStore, request_id: int, job) -> dict:
    """The helper stopped waiting (its own timeout): a pending request is denied by default."""
    stamp = _stamp()
    with _tx(store) as db:
        row = db.execute("SELECT * FROM machine_requests WHERE id=? AND job_id=?", (request_id, str(job.id))).fetchone()
        if row is None:
            raise MachineError("request not found", 404)
        if row["status"] in ("pending", "approved"):
            db.execute("UPDATE machine_requests SET status='cancelled', finished_at=? WHERE id=?", (stamp, request_id))
            if row["approval_id"]:
                db.execute("UPDATE approvals SET status='expired', decided_at=COALESCE(decided_at, ?) WHERE id=? "
                           "AND status IN ('pending','approved')", (stamp, row["approval_id"]))
            audit(db, "cancelled", machine_id=row["machine_id"], bot_id=row["bot_id"], job_id=row["job_id"],
                  request_id=request_id)
    return get_request(store, request_id)


def begin_execute(store: JobStore, request_id: int, job) -> dict:
    """Claim an approved request for execution, once. Everything the approval was bound to is
    checked again here: the job, the stored arguments' hash, the approval row (or the grant), and
    that the computer is still active and the class still on."""
    if not class_enabled(store):
        raise MachineError("Controlling a computer is switched off in Settings > Approvals", 403)
    stamp = _stamp()
    with _tx(store) as db:
        row = db.execute("SELECT * FROM machine_requests WHERE id=?", (request_id,)).fetchone()
        if row is None or row["job_id"] != str(job.id) or row["bot_id"] != str(job.bot_id):
            raise MachineError("request not found", 404)
        if row["status"] != "approved":
            raise MachineError(f"request is {row['status']}, not approved", 403)
        if row["expires_at"] and row["expires_at"] <= stamp:
            raise MachineError("the approval expired before it was used", 403)
        args = json.loads(row["args_json"])
        digest = binding_hash(row["machine_id"], row["bot_id"], row["job_id"], row["tool"], args)
        if not secrets.compare_digest(digest, row["binding_hash"]):
            raise MachineError("the request changed after it was approved", 403)
        if row["approval_id"] is not None:
            approval = db.execute("SELECT * FROM approvals WHERE id=?", (row["approval_id"],)).fetchone()
            if (approval is None or approval["status"] != "approved" or approval["source"] != "machine"
                    or not secrets.compare_digest(approval["input_hash"], "m1:" + digest)
                    or approval["machine_id"] != row["machine_id"] or approval["bot_id"] != row["bot_id"]
                    or approval["job_id"] != row["job_id"] or approval["tool"] != "machine." + row["tool"]):
                raise MachineError("the approval does not match this request", 403)
        elif row["grant_id"] is not None:
            grant = db.execute("SELECT * FROM machine_grants WHERE id=? AND revoked_at IS NULL AND expires_at>?",
                               (row["grant_id"], stamp)).fetchone()
            if (grant is None or grant["bot_id"] != row["bot_id"] or grant["machine_id"] != row["machine_id"]
                    or grant["tool"] != row["tool"] or not grant_matches(row["tool"], grant["pattern"], args)):
                raise MachineError("the grant behind this request was revoked or expired", 403)
        else:
            raise MachineError("request has no approval", 403)
        machine = db.execute("SELECT * FROM machines WHERE id=?", (row["machine_id"],)).fetchone()
        if machine is None or machine["status"] != "active":
            raise MachineError("the computer is not active", 403)
        if db.execute("UPDATE machine_requests SET status='executing', executed_at=? WHERE id=? AND status='approved'",
                      (stamp, request_id)).rowcount != 1:
            raise MachineError("request already used", 403)
        if row["approval_id"] is not None and db.execute(
                "UPDATE approvals SET status='consumed', consumed_by=?, consumed_at=? WHERE id=? AND status='approved'",
                (str(job.id), stamp, row["approval_id"])).rowcount != 1:
            raise MachineError("approval already used", 403)
        audit(db, "executing", machine_id=row["machine_id"], bot_id=row["bot_id"], job_id=row["job_id"],
              request_id=request_id)
    return {**dict(row), "args": args, "machine": dict(machine)}


def fail_unexecuted(store: JobStore, request_id: int, job, why: str) -> None:
    """An approved request the bot cannot use as approved (push content changed): used up, failed."""
    stamp = _stamp()
    with _tx(store) as db:
        row = db.execute("SELECT * FROM machine_requests WHERE id=? AND job_id=?", (request_id, str(job.id))).fetchone()
        if row is None or row["status"] != "approved":
            return
        db.execute("UPDATE machine_requests SET status='failed', error=?, finished_at=? WHERE id=?", (why, stamp, request_id))
        if row["approval_id"]:
            db.execute("UPDATE approvals SET status='consumed', consumed_by=?, consumed_at=? WHERE id=? AND status='approved'",
                       (str(job.id), stamp, row["approval_id"]))
        audit(db, "refused", machine_id=row["machine_id"], bot_id=row["bot_id"], job_id=row["job_id"],
              request_id=request_id, detail=why)


def finish(store: JobStore, request_id: int, *, ok: bool, result: dict | None = None, error: str | None = None) -> None:
    keep = {key: value for key, value in (result or {}).items()
            if key in ("exit", "timed_out", "sha256", "size", "entries", "truncated", "sandboxed", "confirmed_local", "eof")}
    with _tx(store) as db:
        row = db.execute("SELECT * FROM machine_requests WHERE id=?", (request_id,)).fetchone()
        db.execute("UPDATE machine_requests SET status=?, error=?, result_json=?, finished_at=? WHERE id=? "
                   "AND status='executing'", ("done" if ok else "failed", error, json.dumps(keep), _stamp(), request_id))
        if row is not None:
            audit(db, "done" if ok else "failed", machine_id=row["machine_id"], bot_id=row["bot_id"],
                  job_id=row["job_id"], request_id=request_id, detail=error or json.dumps(keep))


def card(store: JobStore, item: dict) -> dict:
    """The machine part of an approval card (store.approval_card)."""
    with closing(store._connect()) as db:
        row = db.execute("SELECT r.*, m.name AS machine_name, m.hostname, m.status AS machine_status FROM machine_requests r "
                         "LEFT JOIN machines m ON m.id=r.machine_id WHERE r.approval_id=?", (item["id"],)).fetchone()
    if row is None:
        return {}
    args = json.loads(row["args_json"])
    shown = {key: value for key, value in args.items() if key not in ("requested",)}
    return {"machine": {"id": row["machine_id"], "name": row["machine_name"], "hostname": row["hostname"],
                        "status": row["machine_status"], "tool": row["tool"], "args": shown,
                        "requested": args.get("requested"), "request_status": row["status"],
                        "always": suggested_pattern(row["tool"], args),
                        "can_always": row["tool"] != "run" or suggested_pattern("run", args) is not None,
                        "full_access": row["tool"] == "run"}}
