"""Shared helpers for the teammate-tool tests: bots with running jobs and their job-scoped tokens."""
from contextlib import closing
from uuid import uuid4

from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.domain import Slot
from botoffice.providers.fake import FakeAdapter

MASTER = {"Authorization": "Bearer test-token"}


def make_app(settings, adapter=None):
    return create_app(settings, {"codex": adapter or FakeAdapter()})


def put_section(store, name, *bots):
    sid = uuid4().hex[:8]
    with closing(store._connect()) as db:
        db.execute("INSERT INTO bot_sections (id, name, position, collapsed, created_at) VALUES (?,?,0,0,'2026-01-01')",
                   (sid, name))
        for position, bot in enumerate(bots):
            db.execute("INSERT OR REPLACE INTO bot_section_members (bot_id, section_id, position) VALUES (?,?,?)",
                       (str(bot.id), sid, position))
    return sid


def set_bot(store, bot, **columns):
    with closing(store._connect()) as db:
        for key, value in columns.items():
            db.execute(f"UPDATE bots SET {key}=? WHERE id=?", (value, str(bot.id)))


def run_job(store, bot, source="chat", prompt="work", provider="codex"):
    """A running job of *bot* and its agent token (claimed one at a time, so it is the job claimed)."""
    job = store.create(prompt, provider, bot_id=bot.id, source=source)
    claimed = store.claim(Slot.DESKTOP_1, {provider})
    if claimed is None:  # a running scheduler (lifespan desks) may have claimed it between create and claim
        claimed = store.get(job.id)
    assert claimed is not None and claimed.id == job.id
    return claimed, {"Authorization": "Bearer " + store.issue_agent_token(claimed)}


def add_message(store, bot, role, text, **extra):
    cols = {"bot_id": str(bot.id), "role": role, "source": "chat", "text": text, "created_at": "2026-11-21T12:00:00+00:00", **extra}
    with closing(store._connect()) as db:
        cur = db.execute(f"INSERT INTO messages ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", tuple(cols.values()))
        return cur.lastrowid


class Desk:
    """An app plus helpers; use as a context manager (no scheduler unless *lifespan*)."""

    def __init__(self, settings, adapter=None, lifespan=False):
        self.app = make_app(settings, adapter)
        self.store = self.app.state.store
        self.lifespan = lifespan
        self.client = TestClient(self.app)

    def __enter__(self):
        if self.lifespan:
            self.client.__enter__()
        else:
            self.store.initialize()
        return self

    def __exit__(self, *exc):
        if self.lifespan:
            self.client.__exit__(*exc)

    def bot(self, name, **columns):
        bot = self.store.create_bot(name, "", "codex", None)
        if columns:
            set_bot(self.store, bot, **columns)
        return bot
