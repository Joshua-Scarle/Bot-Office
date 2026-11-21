"""Paired computers (the device companion): pairing, device tokens, session grants and audit.

A computer runs ``botoffice-agent`` (agent/ in this repo). It pairs once with a short code from
Settings > Computers, then keeps one outgoing WebSocket to ``/v1/machines/ws`` (machine_hub.py).
Bots reach it only through ``bin/machine`` and the job-scoped ``/v1/agent/machines/*`` routes
(machines_api.py).

Security shape (docs/COMPUTERS.md):
- Pairing codes: 8 Crockford base32 characters, 10 minutes, single use, stored as sha256. Every
  wrong try counts against every live code; a code with 5 wrong tries is locked.
- Device token ``bom_<machine id>_<32 random bytes, base64url>``: only its sha256 is stored and it
  is compared in constant time. It works only after the owner clicks Activate with the device's
  fingerprint on screen. Rotation keeps the old token for 5 minutes. Revoke ends it at once. The
  master token is never a device token (other prefix, and the WebSocket takes nothing else).
- Session grants (docs/COMPUTERS.md "The card: one per task"): the first call of a bot's job on a
  computer raises ONE card (bot, computer, task, scope, duration): Allow for this task / Deny.
  The grant is bound to computer, bot, job id and scope; it ends when the job ends or at the cap
  (GRANT_CAP_DEFAULT_MIN, Settings), whichever is first. Calls inside it raise no card; a call
  above its scope (read < files < shell) raises one card to widen it. Each call is still one
  single-use request row (canonical arguments, re-checked at execution) and one audit row.
- Per computer, ``prompt_mode``: "ask" (default), "remember" (one grant per bot and scope for
  REMEMBER_TTL) or "allow" (no card for any bot). The computer's own deny-list wins over all.
"""
from __future__ import annotations

import hashlib
import json
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
# Permission model (session grants). The defaults are these constants: change them here.
SCOPES = ("read", "files", "shell")  # each one includes the ones before it
TOOL_SCOPE = {"ls": "read", "read": "read", "pull": "read", "shot": "read", "push": "files", "run": "shell"}
SCOPE_WORDS = {"read": "list folders, read files, copy files to the bot, take screenshots",
               "files": "read, and copy files onto the computer", "shell": "files, and run commands as you"}
PROMPT_MODES = ("ask", "remember", "allow")
PROMPT_MODE_DEFAULT = "ask"  # every new task or chat turn asks; "remember" and "allow" are opt-in
REMEMBER_TTL = timedelta(days=30)  # how long "remember for this bot" lasts
GRANT_CAP_DEFAULT_MIN = 240  # a task grant ends with the task, and at the latest after this
GRANT_CAP_MIN, GRANT_CAP_MAX = 1, 24 * 60
GRANT_CAP_META = "machine_grant_cap_min"
PENDING_PER_BOT = 20
REQUESTS_PER_MINUTE = 30  # per bot, before any approval is asked
WAIT_CAP = timedelta(hours=24)  # an approval with the Settings timeout at 0 still ends
APPROVED_TTL = timedelta(minutes=10)  # allowed but never executed (the helper died)
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
            prev_expires_at TEXT, token_issued_at TEXT, sandbox INTEGER, confirm_local INTEGER, prompt_mode TEXT,
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
            id INTEGER PRIMARY KEY AUTOINCREMENT, machine_id TEXT NOT NULL, bot_id TEXT NOT NULL, job_id TEXT,
            scope TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'card', approval_id INTEGER,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL, ended_at TEXT, end_reason TEXT
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS machine_grants_task ON machine_grants(machine_id, bot_id, job_id)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS machine_cards (
            approval_id INTEGER PRIMARY KEY, machine_id TEXT NOT NULL, bot_id TEXT NOT NULL, job_id TEXT NOT NULL,
            scope TEXT NOT NULL, widens INTEGER, created_at TEXT NOT NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS machine_cards_job ON machine_cards(machine_id, bot_id, job_id)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS machine_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT, machine_id TEXT NOT NULL, bot_id TEXT NOT NULL,
            job_id TEXT NOT NULL, tool TEXT NOT NULL, args_json TEXT NOT NULL, summary TEXT NOT NULL,
            approval_id INTEGER, grant_id INTEGER, status TEXT NOT NULL, error TEXT, result_json TEXT,
            expires_at TEXT, created_at TEXT NOT NULL, decided_at TEXT, executed_at TEXT, finished_at TEXT
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS machine_requests_job ON machine_requests(job_id)")
    db.execute("CREATE INDEX IF NOT EXISTS machine_requests_approval ON machine_requests(approval_id)")
    db.execute("CREATE INDEX IF NOT EXISTS machine_requests_grant ON machine_requests(grant_id)")
    db.execute(
        """CREATE TABLE IF NOT EXISTS machine_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, machine_id TEXT, bot_id TEXT, job_id TEXT,
            request_id INTEGER, grant_id INTEGER, event TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS machine_audit_machine ON machine_audit(machine_id, id)")
    db.execute("CREATE INDEX IF NOT EXISTS machine_audit_job ON machine_audit(job_id)")
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
          job_id: str | None = None, request_id: int | None = None, grant_id: int | None = None,
          detail: str = "") -> None:
    db.execute("INSERT INTO machine_audit (machine_id,bot_id,job_id,request_id,grant_id,event,detail,created_at) "
               "VALUES (?,?,?,?,?,?,?,?)", (machine_id, bot_id, job_id, request_id, grant_id, event, detail[:4000],
                                            _stamp()))


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
            "flags": device_flags(machine), "local_flags": local if isinstance(local, dict) else {},
            "prompt_mode": prompt_mode(machine)}


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
    """Ends the token(s) and every grant; cards are denied. The hub closes the socket."""
    stamp = _stamp()
    with _tx(store) as db:
        row = db.execute("SELECT * FROM machines WHERE id=?", (machine_id,)).fetchone()
        if row is None:
            raise MachineError("computer not found", 404)
        db.execute("UPDATE machines SET status='revoked', revoked_at=COALESCE(revoked_at, ?), token_hash=NULL, "
                   "prev_token_hash=NULL, prev_expires_at=NULL WHERE id=?", (stamp, machine_id))
        db.execute("UPDATE machine_grants SET ended_at=?, end_reason='computer revoked' WHERE machine_id=? "
                   "AND ended_at IS NULL", (stamp, machine_id))
        db.execute("UPDATE approvals SET status='denied', decided_at=? WHERE machine_id=? AND status='pending'",
                   (stamp, machine_id))
        _drop_approved(db, "computer revoked", machine_id=machine_id)
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
                   confirm_local: bool | None = None, mode: str | None = None) -> dict:
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
        if mode is not None:
            if mode not in PROMPT_MODES:
                raise MachineError(f"prompt mode is one of {', '.join(PROMPT_MODES)}", 400)
            db.execute("UPDATE machines SET prompt_mode=? WHERE id=?", (mode, machine_id))
            if mode != "remember":  # leaving "remember" forgets what was remembered
                db.execute("UPDATE machine_grants SET ended_at=?, end_reason='prompt setting changed' WHERE "
                           "machine_id=? AND kind='remember' AND ended_at IS NULL", (_stamp(), machine_id))
            audit(db, "prompt_mode", machine_id=machine_id, detail=mode)
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


def _size(value: int) -> str:
    if value < 1024:
        return f"{value} bytes"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.1f} MiB"


def describe(tool: str, args: dict, machine: dict) -> str:
    """One call in words, with canonical paths (audit and the bot's own output)."""
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


def audit_args(tool: str, args: dict) -> dict:
    """What the audit keeps of a call: paths, argv, sizes and hashes. Never file contents or output."""
    keep = ("path", "requested", "depth", "offset", "length", "argv", "sh", "cwd", "timeout", "dest", "source",
            "size", "file_size", "sha256", "overwrite", "scope")
    return {key: args[key] for key in keep if key in args and args[key] is not None}


# --- scopes, prompt modes and the grant cap ---

def scope_covers(granted: str | None, needed: str) -> bool:
    return granted in SCOPES and SCOPES.index(granted) >= SCOPES.index(needed)


def wider(first: str | None, second: str) -> str:
    return second if first not in SCOPES or SCOPES.index(second) > SCOPES.index(first) else first


def check_scope(value: object) -> str:
    if value not in SCOPES:
        raise MachineError(f"scope is one of {', '.join(SCOPES)}", 400)
    return value


def prompt_mode(machine: dict) -> str:
    value = machine.get("prompt_mode")
    return value if value in PROMPT_MODES else PROMPT_MODE_DEFAULT


def grant_cap_minutes(store: JobStore) -> int:
    try:
        value = int(store.get_meta(GRANT_CAP_META) or GRANT_CAP_DEFAULT_MIN)
    except ValueError:
        return GRANT_CAP_DEFAULT_MIN
    return value if GRANT_CAP_MIN <= value <= GRANT_CAP_MAX else GRANT_CAP_DEFAULT_MIN


def set_grant_cap(store: JobStore, minutes: object) -> int:
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not GRANT_CAP_MIN <= minutes <= GRANT_CAP_MAX:
        raise MachineError(f"the grant cap is {GRANT_CAP_MIN} to {GRANT_CAP_MAX} minutes", 400)
    store.set_meta(GRANT_CAP_META, str(minutes))
    log_event(store, "grant_cap", detail=f"{minutes} min")
    return minutes


def _hours(minutes: int) -> str:
    return f"{minutes // 60} h" + (f" {minutes % 60} min" if minutes % 60 else "") if minutes >= 60 else f"{minutes} min"


# --- grants ---

def _job_row(db: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return db.execute("SELECT id, status, prompt, assignment_id, source FROM jobs WHERE id=?", (job_id,)).fetchone()


def _task_label(db: sqlite3.Connection, job_id: str) -> str:
    """The task a grant is for: the assignment's title, else what the chat turn was asked."""
    job = _job_row(db, job_id)
    if job is None:
        return f"job {job_id[:8]}"
    if job["assignment_id"]:
        row = db.execute("SELECT title FROM assignments WHERE id=?", (job["assignment_id"],)).fetchone()
        if row is not None:
            return _clean(row["title"], 120)
    prompt = _clean(job["prompt"], 120)
    return f"{job['source'] or 'chat'} turn: {prompt}" if prompt else f"job {job_id[:8]}"


def _live_task_grant(db: sqlite3.Connection, machine_id: str, bot_id: str, job_id: str) -> dict | None:
    row = db.execute("SELECT * FROM machine_grants WHERE machine_id=? AND bot_id=? AND job_id=? AND kind='task' "
                     "AND ended_at IS NULL AND expires_at>? ORDER BY id DESC LIMIT 1",
                     (machine_id, bot_id, job_id, _stamp())).fetchone()
    return dict(row) if row else None


def _remembered(db: sqlite3.Connection, machine_id: str, bot_id: str) -> dict | None:
    row = db.execute("SELECT * FROM machine_grants WHERE machine_id=? AND bot_id=? AND kind='remember' "
                     "AND ended_at IS NULL AND expires_at>? ORDER BY id DESC LIMIT 1",
                     (machine_id, bot_id, _stamp())).fetchone()
    return dict(row) if row else None


def _owner_ended(db: sqlite3.Connection, machine_id: str, bot_id: str, job_id: str) -> bool:
    """The owner pressed End grant for this task: it asks again, whatever the prompt setting."""
    return db.execute("SELECT 1 FROM machine_grants WHERE machine_id=? AND bot_id=? AND job_id=? AND kind='task' "
                      "AND end_reason='ended by you' LIMIT 1", (machine_id, bot_id, job_id)).fetchone() is not None


def _open_task_grant(db: sqlite3.Connection, store: JobStore, machine_id: str, bot_id: str, job_id: str, scope: str,
                     source: str, approval_id: int | None = None) -> dict:
    """A new task grant, or the live one widened to *scope*. Its end never moves later."""
    current = _live_task_grant(db, machine_id, bot_id, job_id)
    if current is not None:
        if not scope_covers(current["scope"], scope):
            db.execute("UPDATE machine_grants SET scope=? WHERE id=?", (scope, current["id"]))
            audit(db, "grant_widened", machine_id=machine_id, bot_id=bot_id, job_id=job_id, grant_id=current["id"],
                  detail=json.dumps({"from": current["scope"], "to": scope, "by": source}))
        return dict(db.execute("SELECT * FROM machine_grants WHERE id=?", (current["id"],)).fetchone())
    stamp = now()
    expires = stamp + timedelta(minutes=grant_cap_minutes(store))
    cursor = db.execute("INSERT INTO machine_grants (machine_id,bot_id,job_id,scope,kind,source,approval_id,created_at,"
                        "expires_at) VALUES (?,?,?,?,'task',?,?,?,?)",
                        (machine_id, bot_id, job_id, scope, source, approval_id, stamp.isoformat(), expires.isoformat()))
    grant_id = int(cursor.lastrowid)
    audit(db, "grant_started", machine_id=machine_id, bot_id=bot_id, job_id=job_id, grant_id=grant_id,
          detail=json.dumps({"scope": scope, "by": source, "until": expires.isoformat()}))
    return dict(db.execute("SELECT * FROM machine_grants WHERE id=?", (grant_id,)).fetchone())


def _remember(db: sqlite3.Connection, machine_id: str, bot_id: str, scope: str) -> None:
    current = _remembered(db, machine_id, bot_id)
    if current is not None:
        if not scope_covers(current["scope"], scope):
            db.execute("UPDATE machine_grants SET scope=? WHERE id=?", (scope, current["id"]))
            audit(db, "remember_widened", machine_id=machine_id, bot_id=bot_id, grant_id=current["id"],
                  detail=json.dumps({"from": current["scope"], "to": scope}))
        return
    stamp = now()
    cursor = db.execute("INSERT INTO machine_grants (machine_id,bot_id,job_id,scope,kind,source,created_at,expires_at) "
                        "VALUES (?,?,NULL,?,'remember','card',?,?)",
                        (machine_id, bot_id, scope, stamp.isoformat(), (stamp + REMEMBER_TTL).isoformat()))
    audit(db, "remember_started", machine_id=machine_id, bot_id=bot_id, grant_id=int(cursor.lastrowid),
          detail=json.dumps({"scope": scope, "until": (stamp + REMEMBER_TTL).isoformat()}))


def _automatic(db: sqlite3.Connection, store: JobStore, machine: dict, bot_id: str, job_id: str,
               scope: str) -> dict | None:
    """A task grant without a card, when this computer's prompt setting allows it."""
    mode = prompt_mode(machine)
    if mode == "ask" or _owner_ended(db, machine["id"], bot_id, job_id):
        return None
    if mode == "allow":
        return _open_task_grant(db, store, machine["id"], bot_id, job_id, scope, "always allow")
    remembered = _remembered(db, machine["id"], bot_id)
    if remembered is None or not scope_covers(remembered["scope"], scope):
        return None
    return _open_task_grant(db, store, machine["id"], bot_id, job_id, scope, f"remembered #{remembered['id']}")


def grant_view(row: dict) -> dict:
    return {key: row.get(key) for key in ("id", "machine_id", "machine_name", "bot_id", "bot_name", "job_id", "scope",
                                          "kind", "source", "created_at", "expires_at", "ended_at", "end_reason",
                                          "task")}


def list_grants(store: JobStore, machine_id: str | None = None) -> list[dict]:
    """Live grants: task grants (the banner, with End grant) and remembered ones (with Forget)."""
    with closing(store._connect()) as db:
        rows = db.execute(
            "SELECT g.*, b.name AS bot_name, m.name AS machine_name FROM machine_grants g "
            "LEFT JOIN bots b ON b.id=g.bot_id LEFT JOIN machines m ON m.id=g.machine_id "
            "WHERE g.ended_at IS NULL AND g.expires_at>? AND (? IS NULL OR g.machine_id=?) ORDER BY g.id DESC",
            (_stamp(), machine_id, machine_id)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            if item["kind"] == "task":
                job = _job_row(db, item["job_id"])
                if job is None or job["status"] != "running":
                    continue  # ends at the next sweep
                item["task"] = _task_label(db, item["job_id"])
            out.append(grant_view(item))
    return out


def task_label(store: JobStore, job_id: str) -> str:
    with closing(store._connect()) as db:
        return _task_label(db, job_id)


def get_grant(store: JobStore, grant_id: int) -> dict | None:
    with closing(store._connect()) as db:
        row = db.execute("SELECT * FROM machine_grants WHERE id=?", (grant_id,)).fetchone()
    return dict(row) if row else None


def end_grant(store: JobStore, grant_id: int, reason: str = "ended by you") -> dict:
    """End grant (a task grant) or Forget (a remembered one). Calls in flight are the hub's part."""
    with _tx(store) as db:
        row = db.execute("SELECT * FROM machine_grants WHERE id=?", (grant_id,)).fetchone()
        if row is None:
            raise MachineError("grant not found", 404)
        if row["ended_at"] is None:
            db.execute("UPDATE machine_grants SET ended_at=?, end_reason=? WHERE id=? AND ended_at IS NULL",
                       (_stamp(), reason, grant_id))
            audit(db, "grant_ended", machine_id=row["machine_id"], bot_id=row["bot_id"], job_id=row["job_id"],
                  grant_id=grant_id, detail=reason)
            _drop_cards(db, row, "the grant was ended")
        _drop_approved(db, "the grant was ended", grant_id=grant_id)
        return dict(db.execute("SELECT * FROM machine_grants WHERE id=?", (grant_id,)).fetchone())


def grant_live(store: JobStore, grant_id: int, job_id: str) -> bool:
    """Whether calls under *grant_id* may go on: not ended, not past its cap, its job still running,
    its computer active and computer access not switched off."""
    if not class_enabled(store):
        return False
    with closing(store._connect()) as db:
        row = db.execute("SELECT g.*, j.status AS job_status, m.status AS machine_status FROM machine_grants g "
                         "LEFT JOIN jobs j ON j.id=g.job_id LEFT JOIN machines m ON m.id=g.machine_id WHERE g.id=?",
                         (grant_id,)).fetchone()
    return (row is not None and row["ended_at"] is None and row["expires_at"] > _stamp() and row["job_id"] == job_id
            and row["job_status"] == "running" and row["machine_status"] == "active")


def _drop_cards(db: sqlite3.Connection, grant: sqlite3.Row, why: str) -> None:
    """A task grant ended: its waiting widen card is denied with it, so a late Allow cannot open a
    fresh grant. The bot can ask again (a new card)."""
    if grant["kind"] != "task":
        return
    stamp = _stamp()
    rows = db.execute("SELECT a.id FROM machine_cards c JOIN approvals a ON a.id=c.approval_id WHERE c.widens=? "
                      "AND a.status='pending'", (grant["id"],)).fetchall()
    for row in rows:
        db.execute("UPDATE approvals SET status='denied', decided_at=? WHERE id=? AND status='pending'",
                   (stamp, row["id"]))
        for item in db.execute("SELECT * FROM machine_requests WHERE approval_id=? AND status='pending'",
                               (row["id"],)).fetchall():
            db.execute("UPDATE machine_requests SET status='denied', error=?, decided_at=?, finished_at=? WHERE id=?",
                       (why, stamp, stamp, item["id"]))
            if item["tool"] != "grant":
                audit(db, "refused", machine_id=item["machine_id"], bot_id=item["bot_id"], job_id=item["job_id"],
                      request_id=item["id"], detail=why)
        audit(db, "grant_denied", machine_id=grant["machine_id"], bot_id=grant["bot_id"], job_id=grant["job_id"],
              grant_id=grant["id"], detail=json.dumps({"approval": row["id"], "why": why}))


def _drop_approved(db: sqlite3.Connection, why: str, *, grant_id: int | None = None,
                   machine_id: str | None = None) -> None:
    """Approved calls not started yet under an ended grant (or on a computer) are cancelled."""
    if grant_id is not None:
        db.execute("UPDATE machine_requests SET status='cancelled', error=?, finished_at=? WHERE grant_id=? "
                   "AND status='approved'", (why, _stamp(), grant_id))
    if machine_id is not None:
        db.execute("UPDATE machine_requests SET status='cancelled', error=?, finished_at=? WHERE machine_id=? "
                   "AND status IN ('pending','approved')", (why, _stamp(), machine_id))


def sweep(store: JobStore) -> list[int]:
    """End task grants whose job stopped running or whose cap passed, and remembered grants past
    their 30 days. Returns the ended task grant ids (the hub cancels their calls)."""
    stamp = _stamp()
    ended: list[int] = []
    with _tx(store) as db:
        rows = db.execute("SELECT g.*, j.status AS job_status FROM machine_grants g LEFT JOIN jobs j ON j.id=g.job_id "
                          "WHERE g.ended_at IS NULL AND (g.expires_at<=? OR (g.kind='task' AND "
                          "(j.status IS NULL OR j.status<>'running')))", (stamp,)).fetchall()
        for row in rows:
            reason = "expired" if row["expires_at"] <= stamp else "task ended"
            if db.execute("UPDATE machine_grants SET ended_at=?, end_reason=? WHERE id=? AND ended_at IS NULL",
                          (stamp, reason, row["id"])).rowcount:
                audit(db, "grant_ended", machine_id=row["machine_id"], bot_id=row["bot_id"], job_id=row["job_id"],
                      grant_id=row["id"], detail=reason)
                _drop_approved(db, f"the grant {reason}", grant_id=row["id"])
                _drop_cards(db, row, f"the grant {reason}")
                if row["kind"] == "task":
                    ended.append(int(row["id"]))
    return ended


# --- requests and grant cards ---

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


def card_text(db: sqlite3.Connection, store: JobStore, machine: dict, bot_id: str, job_id: str, scope: str,
              current: str | None) -> str:
    bot = db.execute("SELECT name FROM bots WHERE id=?", (bot_id,)).fetchone()
    who = bot["name"] if bot else "a bot"
    verb = f"widen its access from {current} to {scope}" if current else f"use it with scope {scope}"
    return (f"{who} asks to {verb} on {machine['name']} ({machine['hostname']}) for this task "
            f"({_task_label(db, job_id)}): {SCOPE_WORDS[scope]}. Until the task ends, at most "
            f"{_hours(grant_cap_minutes(store))}.")


def _card(db: sqlite3.Connection, store: JobStore, job, machine: dict, scope: str) -> int:
    """The one pending card of this job on this computer, raised now or widened to *scope*."""
    bot_id, job_id = str(job.bot_id), str(job.id)
    current = _live_task_grant(db, machine["id"], bot_id, job_id)
    existing = db.execute("SELECT c.*, a.id AS approval_id FROM machine_cards c JOIN approvals a ON a.id=c.approval_id "
                          "WHERE c.machine_id=? AND c.bot_id=? AND c.job_id=? AND a.status='pending' ORDER BY a.id DESC "
                          "LIMIT 1", (machine["id"], bot_id, job_id)).fetchone()
    replaced = None
    if existing is not None:
        if scope_covers(existing["scope"], scope):
            return int(existing["approval_id"])
        # wider than the card on screen: that card is withdrawn (an Allow on it fails) and a new
        # one asks for the wider scope, so the owner never allows more than the card they saw
        replaced = int(existing["approval_id"])
        db.execute("UPDATE approvals SET status='expired', decided_at=? WHERE id=? AND status='pending'",
                   (_stamp(), replaced))
    pending = db.execute("SELECT COUNT(*) FROM approvals WHERE bot_id=? AND source='machine' AND status='pending'",
                         (bot_id,)).fetchone()[0]
    if pending >= PENDING_PER_BOT:
        raise MachineError(f"{PENDING_PER_BOT} computer approvals of this bot are already waiting", 429)
    stamp = now()
    minutes = _wait_minutes(store)
    expires = stamp + (min(timedelta(minutes=minutes), WAIT_CAP) if minutes else WAIT_CAP)
    summary = card_text(db, store, machine, bot_id, job_id, scope, current["scope"] if current else None)
    nonce = secrets.token_hex(8)
    cursor = db.execute(
        "INSERT INTO approvals (bot_id,job_id,assignment_id,tool,summary,input_hash,scope,source,preset,status,"
        "expires_at,created_at,machine_id) VALUES (?,?,?,?,?,?,?,'machine',?,'pending',?,?,?)",
        (bot_id, job_id, str(job.assignment_id) if job.assignment_id else None, "machine.grant", summary,
         "mg1:" + _sha(f"{machine['id']}:{bot_id}:{job_id}:{nonce}"), "attended", CLASS_KEY, expires.isoformat(),
         stamp.isoformat(), machine["id"]))
    approval_id = int(cursor.lastrowid)
    db.execute("INSERT INTO machine_cards (approval_id,machine_id,bot_id,job_id,scope,widens,created_at) "
               "VALUES (?,?,?,?,?,?,?)", (approval_id, machine["id"], bot_id, job_id, scope,
                                          current["id"] if current else None, stamp.isoformat()))
    short = summary if len(summary) <= 200 else summary[:200] + "…"
    db.execute("INSERT INTO messages (bot_id,role,source,text,created_at,approval_id) VALUES (?,?,?,?,?,?)",
               (bot_id, "system", "chat", f"Approval #{approval_id} requested: {short}", stamp.isoformat(), approval_id))
    audit(db, "card", machine_id=machine["id"], bot_id=bot_id, job_id=job_id,
          detail=json.dumps({"approval": approval_id, "scope": scope, "widen_from": current["scope"] if current else None,
                             "replaces": replaced}))
    if replaced is not None:  # its waiting calls wait on the new card
        db.execute("UPDATE machine_requests SET approval_id=?, expires_at=? WHERE approval_id=? AND status='pending'",
                   (approval_id, expires.isoformat(), replaced))
        db.execute("INSERT INTO messages (bot_id,role,source,text,created_at,approval_id) VALUES (?,?,?,?,?,?)",
                   (bot_id, "system", "chat", f"Approval #{replaced} was replaced by #{approval_id} (a wider scope).",
                    stamp.isoformat(), replaced))
    return approval_id


def record_request(store: JobStore, job, machine: dict, tool: str, args: dict, scope: str | None = None) -> dict:
    """One call (or, with tool "grant", only the ask for a scope): allowed at once under a live task
    grant that covers it, or waiting on the job's one card (raised, or widened, here)."""
    bot_id, job_id = str(job.bot_id), str(job.id)
    needed = scope if tool == "grant" else TOOL_SCOPE[tool]
    summary = (f"ask for scope {needed} on {machine['name']} ({machine['hostname']})" if tool == "grant"
               else describe(tool, args, machine))
    if len(summary) > SUMMARY_MAX:
        raise MachineError(f"this {tool} is {len(summary)} characters, over the {SUMMARY_MAX} the audit keeps; "
                           "write a script on the computer first or split it", 400)
    stamp = now()
    with _tx(store) as db:
        job_row = _job_row(db, job_id)
        if job_row is None or job_row["status"] != "running":
            raise MachineError("the bot's job is not running", 409)
        grant = _live_task_grant(db, machine["id"], bot_id, job_id)
        if grant is None or not scope_covers(grant["scope"], needed):
            grant = _automatic(db, store, machine, bot_id, job_id, wider(grant["scope"] if grant else None, needed)) \
                or grant
        if grant is not None and scope_covers(grant["scope"], needed):
            status = "done" if tool == "grant" else "approved"
            cursor = db.execute(
                "INSERT INTO machine_requests (machine_id,bot_id,job_id,tool,args_json,summary,grant_id,status,"
                "expires_at,created_at,decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (machine["id"], bot_id, job_id, tool, json.dumps(args, sort_keys=True), summary, grant["id"], status,
                 (stamp + APPROVED_TTL).isoformat(), stamp.isoformat(), stamp.isoformat()))
            approval_id = None
        else:
            approval_id = _card(db, store, job, machine, needed)
            expires = db.execute("SELECT expires_at FROM approvals WHERE id=?", (approval_id,)).fetchone()[0]
            cursor = db.execute(
                "INSERT INTO machine_requests (machine_id,bot_id,job_id,tool,args_json,summary,approval_id,status,"
                "expires_at,created_at) VALUES (?,?,?,?,?,?,?,'pending',?,?)",
                (machine["id"], bot_id, job_id, tool, json.dumps(args, sort_keys=True), summary, approval_id, expires,
                 stamp.isoformat()))
        request_id = int(cursor.lastrowid)
        if tool != "grant":
            audit(db, "allowed" if approval_id is None else "waiting", machine_id=machine["id"], bot_id=bot_id,
                  job_id=job_id, request_id=request_id, grant_id=grant["id"] if approval_id is None else None,
                  detail=json.dumps({"tool": tool, **audit_args(tool, args)}, sort_keys=True))
    return get_request(store, request_id)


def refused(store: JobStore, job, machine: dict, tool: str, args: dict, *, blocked: bool, why: str) -> None:
    """A call refused before it became a request (the computer's deny-list, a bad path)."""
    log_event(store, "blocked" if blocked else "failed", machine_id=machine["id"], bot_id=str(job.bot_id),
              job_id=str(job.id), detail=json.dumps({"tool": tool, **audit_args(tool, args), "error": why[:300]},
                                                    sort_keys=True))


def get_request(store: JobStore, request_id: int) -> dict | None:
    with closing(store._connect()) as db:
        row = db.execute("SELECT * FROM machine_requests WHERE id=?", (request_id,)).fetchone()
    return dict(row) if row else None


def request_view(item: dict) -> dict:
    return {"id": item["id"], "status": item["status"], "tool": item["tool"], "summary": item["summary"],
            "approval_id": item["approval_id"], "grant_id": item["grant_id"], "error": item["error"],
            "expires_at": item["expires_at"], "machine_id": item["machine_id"]}


def expire(store: JobStore) -> int:
    """Cards past their timeout or whose job stopped: expired (a deny), with their waiting calls.
    Allowed calls never started within APPROVED_TTL: expired too. Then the grant sweep."""
    stamp = _stamp()
    notes: list[tuple[str, int, str]] = []
    with _tx(store) as db:
        cards = db.execute(
            "SELECT a.id, a.bot_id, a.summary FROM approvals a LEFT JOIN jobs j ON j.id=a.job_id "
            "WHERE a.source='machine' AND a.status='pending' AND (a.expires_at<=? OR j.status IS NULL "
            "OR j.status<>'running')", (stamp,)).fetchall()
        for row in cards:
            if db.execute("UPDATE approvals SET status='expired', decided_at=? WHERE id=? AND status='pending'",
                          (stamp, row["id"])).rowcount:
                notes.append((row["bot_id"], row["id"], row["summary"]))
        rows = db.execute(
            "SELECT r.* FROM machine_requests r LEFT JOIN jobs j ON j.id=r.job_id LEFT JOIN approvals a "
            "ON a.id=r.approval_id WHERE r.status IN ('pending','approved') AND (r.expires_at<=? OR j.status IS NULL "
            "OR j.status<>'running' OR (r.status='pending' AND a.status<>'pending'))", (stamp,)).fetchall()
        for row in rows:
            db.execute("UPDATE machine_requests SET status='expired', finished_at=? WHERE id=? AND status=?",
                       (stamp, row["id"], row["status"]))
            if row["tool"] != "grant":
                audit(db, "expired", machine_id=row["machine_id"], bot_id=row["bot_id"], job_id=row["job_id"],
                      request_id=row["id"])
    for bot_id, approval_id, summary in notes:
        short = summary if len(summary) <= 160 else summary[:160] + "…"
        store.add_message(bot_id, "system", f"Approval #{approval_id} got no answer in time, so it was denied: {short}",
                          source="chat")
    sweep(store)
    return len(rows)


def is_machine_approval(store: JobStore, approval_id: int) -> bool:
    with closing(store._connect()) as db:
        row = db.execute("SELECT source FROM approvals WHERE id=?", (approval_id,)).fetchone()
    return row is not None and row["source"] == "machine"


def _decide(db: sqlite3.Connection, store: JobStore, approval_id: int, status: str) -> None:
    stamp = _stamp()
    row = db.execute("SELECT * FROM approvals WHERE id=? AND source='machine'", (approval_id,)).fetchone()
    card = db.execute("SELECT * FROM machine_cards WHERE approval_id=?", (approval_id,)).fetchone()
    if row is None or card is None:
        raise MachineError("approval not found", 404)
    if row["status"] != "pending":
        raise MachineError(f"approval is {row['status']}")
    grant = None
    if status == "approved":
        if row["expires_at"] and row["expires_at"] <= stamp:
            raise MachineError("approval is expired: the bot has stopped waiting")
        job = _job_row(db, card["job_id"])
        if job is None or job["status"] != "running":
            raise MachineError("the bot's task has ended; it can ask again")
        machine = db.execute("SELECT * FROM machines WHERE id=?", (card["machine_id"],)).fetchone()
        if machine is None or machine["status"] != "active":
            raise MachineError("the computer is not active")
        if not class_enabled(store):
            raise MachineError("Controlling a computer is switched off in Settings > Approvals", 403)
        live = _live_task_grant(db, card["machine_id"], card["bot_id"], card["job_id"])
        if card["widens"] is not None and (live is None or live["id"] != card["widens"]):
            raise MachineError("the grant this card would widen has ended; the bot can ask again")
        grant = _open_task_grant(db, store, card["machine_id"], card["bot_id"], card["job_id"], card["scope"], "card",
                                 approval_id)
        if prompt_mode(dict(machine)) == "remember":
            _remember(db, card["machine_id"], card["bot_id"], grant["scope"])
    db.execute("UPDATE approvals SET status=?, decided_at=? WHERE id=? AND status='pending'", (status, stamp, approval_id))
    waiting = db.execute("SELECT * FROM machine_requests WHERE approval_id=? AND status='pending'",
                         (approval_id,)).fetchall()
    for item in waiting:
        if grant is not None and scope_covers(grant["scope"], card["scope"]):
            db.execute("UPDATE machine_requests SET status=?, grant_id=?, decided_at=?, expires_at=? WHERE id=? "
                       "AND status='pending'", ("done" if item["tool"] == "grant" else "approved", grant["id"], stamp,
                                                (now() + APPROVED_TTL).isoformat(), item["id"]))
        else:
            db.execute("UPDATE machine_requests SET status='denied', decided_at=?, finished_at=? WHERE id=? "
                       "AND status='pending'", (stamp, stamp, item["id"]))
            if item["tool"] != "grant":
                audit(db, "refused", machine_id=item["machine_id"], bot_id=item["bot_id"], job_id=item["job_id"],
                      request_id=item["id"], detail="the owner denied the grant")
    audit(db, "grant_allowed" if status == "approved" else "grant_denied", machine_id=card["machine_id"],
          bot_id=card["bot_id"], job_id=card["job_id"], grant_id=grant["id"] if grant else None,
          detail=json.dumps({"approval": approval_id, "scope": card["scope"]}))


def approve(store: JobStore, approval_id: int) -> dict:
    with _tx(store) as db:
        _decide(db, store, approval_id, "approved")
    return approval_dict(store, approval_id)


def deny(store: JobStore, approval_id: int) -> dict:
    with _tx(store) as db:
        _decide(db, store, approval_id, "denied")
    return approval_dict(store, approval_id)


def approval_dict(store: JobStore, approval_id: int) -> dict:
    with closing(store._connect()) as db:
        row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
    return dict(row) if row else {}


def cancel(store: JobStore, request_id: int, job) -> dict:
    """The helper stopped waiting (its own timeout): the call is dropped. The card stays for the
    other calls of the task; it expires on its own."""
    stamp = _stamp()
    with _tx(store) as db:
        row = db.execute("SELECT * FROM machine_requests WHERE id=? AND job_id=?", (request_id, str(job.id))).fetchone()
        if row is None:
            raise MachineError("request not found", 404)
        if row["status"] in ("pending", "approved"):
            db.execute("UPDATE machine_requests SET status='cancelled', finished_at=? WHERE id=?", (stamp, request_id))
            if row["tool"] != "grant":
                audit(db, "cancelled", machine_id=row["machine_id"], bot_id=row["bot_id"], job_id=row["job_id"],
                      request_id=request_id)
    return get_request(store, request_id)


def begin_execute(store: JobStore, request_id: int, job) -> dict:
    """Claim an allowed call for execution, once. The grant behind it is checked again here: same
    computer, bot and job, not ended, before its cap, scope covering the tool; the job still
    running, the computer active and computer access on."""
    if not class_enabled(store):
        raise MachineError("Controlling a computer is switched off in Settings > Approvals", 403)
    stamp = _stamp()
    with _tx(store) as db:
        row = db.execute("SELECT * FROM machine_requests WHERE id=?", (request_id,)).fetchone()
        if row is None or row["job_id"] != str(job.id) or row["bot_id"] != str(job.bot_id) or row["tool"] == "grant":
            raise MachineError("request not found", 404)
        if row["status"] != "approved":
            raise MachineError(f"request is {row['status']}, not allowed", 403)
        if row["expires_at"] and row["expires_at"] <= stamp:
            raise MachineError("the call waited too long after it was allowed; ask again", 403)
        grant = db.execute("SELECT * FROM machine_grants WHERE id=?", (row["grant_id"],)).fetchone()
        if (grant is None or grant["kind"] != "task" or grant["ended_at"] is not None or grant["expires_at"] <= stamp
                or grant["machine_id"] != row["machine_id"] or grant["bot_id"] != row["bot_id"]
                or grant["job_id"] != row["job_id"] or not scope_covers(grant["scope"], TOOL_SCOPE[row["tool"]])):
            raise MachineError("the grant for this task was ended or has expired; ask again", 403)
        job_row = _job_row(db, row["job_id"])
        if job_row is None or job_row["status"] != "running":
            raise MachineError("the bot's job is not running", 403)
        machine = db.execute("SELECT * FROM machines WHERE id=?", (row["machine_id"],)).fetchone()
        if machine is None or machine["status"] != "active":
            raise MachineError("the computer is not active", 403)
        if db.execute("UPDATE machine_requests SET status='executing', executed_at=? WHERE id=? AND status='approved'",
                      (stamp, request_id)).rowcount != 1:
            raise MachineError("request already used", 403)
    return {**dict(row), "args": json.loads(row["args_json"]), "machine": dict(machine), "grant": dict(grant)}


def fail_unexecuted(store: JobStore, request_id: int, job, why: str) -> None:
    """An allowed call the bot cannot use as asked (push content changed): used up, failed."""
    stamp = _stamp()
    with _tx(store) as db:
        row = db.execute("SELECT * FROM machine_requests WHERE id=? AND job_id=?", (request_id, str(job.id))).fetchone()
        if row is None or row["status"] != "approved":
            return
        db.execute("UPDATE machine_requests SET status='failed', error=?, finished_at=? WHERE id=?", (why, stamp, request_id))
        audit(db, "failed", machine_id=row["machine_id"], bot_id=row["bot_id"], job_id=row["job_id"],
              request_id=request_id, grant_id=row["grant_id"], detail=json.dumps(
                  {"tool": row["tool"], **audit_args(row["tool"], json.loads(row["args_json"])), "error": why},
                  sort_keys=True))


RESULT_KEYS = ("exit", "timed_out", "cancelled", "sha256", "size", "entries", "truncated", "sandboxed",
               "confirmed_local", "eof", "stdout_bytes", "stderr_bytes")


def finish(store: JobStore, request_id: int, *, ok: bool, result: dict | None = None, error: str | None = None,
           blocked: bool = False) -> None:
    """The call's outcome: done, failed or blocked (the computer's deny-list). Sizes and exit codes
    only; what came back (file text, command output) is never stored."""
    data = result or {}
    keep = {key: data[key] for key in RESULT_KEYS if key in data and not isinstance(data[key], (list, dict))}
    if isinstance(data.get("entries"), list):
        keep["entries"] = len(data["entries"])
    for stream in ("stdout", "stderr"):
        if isinstance(data.get(stream), str):
            keep[f"{stream}_bytes"] = len(data[stream].encode())
    if isinstance(data.get("data"), str):
        keep["bytes"] = len(data["data"])
    status = "done" if ok else "blocked" if blocked else "failed"
    with _tx(store) as db:
        row = db.execute("SELECT * FROM machine_requests WHERE id=?", (request_id,)).fetchone()
        db.execute("UPDATE machine_requests SET status=?, error=?, result_json=?, finished_at=? WHERE id=? "
                   "AND status='executing'", (status, error, json.dumps(keep), _stamp(), request_id))
        if row is not None:
            detail = {"tool": row["tool"], **audit_args(row["tool"], json.loads(row["args_json"])), **keep}
            if error:
                detail["error"] = error[:300]
            audit(db, status, machine_id=row["machine_id"], bot_id=row["bot_id"], job_id=row["job_id"],
                  request_id=request_id, grant_id=row["grant_id"], detail=json.dumps(detail, sort_keys=True))


def stop_all(store: JobStore) -> list[int]:
    """Stop all computer access: the class "computer" off (every call refused), every grant ended
    and every card denied. Returns the ended task grant ids (the hub cancels their calls)."""
    from . import approval_classes
    approval_classes.update(store, enabled={CLASS_KEY: False})
    stamp = _stamp()
    with _tx(store) as db:
        ids = [int(row[0]) for row in db.execute("SELECT id FROM machine_grants WHERE ended_at IS NULL AND kind='task'")]
        db.execute("UPDATE machine_grants SET ended_at=?, end_reason='computer access stopped' WHERE ended_at IS NULL",
                   (stamp,))
        db.execute("UPDATE approvals SET status='denied', decided_at=? WHERE source='machine' AND status='pending'",
                   (stamp,))
        db.execute("UPDATE machine_requests SET status='cancelled', error='computer access stopped', finished_at=? "
                   "WHERE status IN ('pending','approved')", (stamp,))
        audit(db, "stopped_all", detail=f"{len(ids)} task grant(s) ended")
    return ids


def resume_all(store: JobStore) -> None:
    from . import approval_classes
    approval_classes.update(store, enabled={CLASS_KEY: True})
    log_event(store, "resumed_all")


def card(store: JobStore, item: dict) -> dict:
    """The machine part of an approval card (store.approval_card): one card per task and computer."""
    with closing(store._connect()) as db:
        row = db.execute("SELECT c.*, m.name AS machine_name, m.hostname, m.status AS machine_status FROM machine_cards c "
                         "LEFT JOIN machines m ON m.id=c.machine_id WHERE c.approval_id=?", (item["id"],)).fetchone()
        if row is None:
            return {}
        widens = db.execute("SELECT scope FROM machine_grants WHERE id=?", (row["widens"],)).fetchone() \
            if row["widens"] else None
        grant = db.execute("SELECT * FROM machine_grants WHERE approval_id=? OR id=? ORDER BY id DESC LIMIT 1",
                           (item["id"], row["widens"] or -1)).fetchone() if item["status"] == "approved" else None
        calls = [r["summary"] for r in db.execute(
            "SELECT summary FROM machine_requests WHERE approval_id=? AND tool<>'grant' ORDER BY id LIMIT 3",
            (item["id"],))]
        task = _task_label(db, row["job_id"])
    live = grant is not None and grant["ended_at"] is None and grant["expires_at"] > _stamp()
    return {"machine": {"id": row["machine_id"], "name": row["machine_name"], "hostname": row["hostname"],
                        "status": row["machine_status"], "scope": row["scope"], "scope_words": SCOPE_WORDS[row["scope"]],
                        "widen_from": widens["scope"] if widens else None, "task": task, "job_id": row["job_id"],
                        "cap_minutes": grant_cap_minutes(store), "calls": calls,
                        "grant": {"id": grant["id"], "scope": grant["scope"], "expires_at": grant["expires_at"],
                                  "live": live, "end_reason": grant["end_reason"]} if grant is not None else None,
                        "full_access": row["scope"] == "shell"}}


def audit_log(store: JobStore, *, machine_id: str | None = None, bot_id: str | None = None, job_id: str | None = None,
              limit: int = 200) -> list[dict]:
    """Settings > Computers > Audit, newest first, filterable by computer, bot and session (job)."""
    with closing(store._connect()) as db:
        return [dict(row) for row in db.execute(
            "SELECT a.*, b.name AS bot_name, m.name AS machine_name FROM machine_audit a LEFT JOIN bots b ON b.id=a.bot_id "
            "LEFT JOIN machines m ON m.id=a.machine_id WHERE (? IS NULL OR a.machine_id=?) AND (? IS NULL OR a.bot_id=?) "
            "AND (? IS NULL OR a.job_id=?) ORDER BY a.id DESC LIMIT ?",
            (machine_id, machine_id, bot_id, bot_id, job_id, job_id, max(1, min(limit, 1000)))).fetchall()]
