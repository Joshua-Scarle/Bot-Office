"""0.10.0 with 0.9.2: images in assignment turns reach chat like chat replies."""
from test_loop import Scripted, run_next

from botoffice import attachments
from botoffice.domain import JobStatus
from botoffice.outgoing_images import SIDECAR
from botoffice.providers.base import RunResult
from botoffice.scheduler import Scheduler
from botoffice.store import JobStore

PNG = b"\x89PNG\r\n\x1a\nimage"


class Drawing(Scripted):
    """Writes chart.png, and with ``attach`` records it in the sidecar like `desk attach`."""

    def __init__(self, reply, attach=False):
        super().__init__(reply)
        self.attach = attach

    async def run(self, job, workspace, log_path, timeout):
        (workspace / "chart.png").write_bytes(PNG)
        if self.attach:
            (workspace / SIDECAR).write_text(f"{workspace / 'chart.png'}\n")
        result = await super().run(job, workspace, log_path, timeout)
        (workspace / "reply.txt").write_text(result.final_text)
        return RunResult("succeeded", exit_code=0, final_text=result.final_text)


def setup(settings, adapter, **fields):
    settings.ensure_dirs()
    store = JobStore(settings.data_dir / "agentdesk.sqlite3")
    scheduler = Scheduler(store, settings, {"codex": adapter})
    bot = store.create_bot("Ada", "", "codex", None)
    item = scheduler.assignments.create(bot.id, {"title": "Chart", "goal": "draw it", **fields, "start": True})
    return store, scheduler, bot, item


def assistant(store, bot):
    return [m for m in store.list_messages(bot.id) if m.role == "assistant"][-1]


def test_assignment_turn_reply_markdown_image_is_attached(settings):
    adapter = Drawing("Chart: ![chart](chart.png)\nBOT-OFFICE-STATUS: done")
    store, scheduler, bot, item = setup(settings, adapter)
    job = run_next(store, scheduler)
    assert job.status == JobStatus.SUCCEEDED and job.assignment_id == item.id
    message = assistant(store, bot)
    assert message.source == "assignment" and len(message.attachments) == 1
    assert attachments.read(settings.data_dir, bot.id, message.attachments[0]) == PNG
    assert "![chart](chart.png)" not in message.text and "BOT-OFFICE-STATUS" not in message.text
    assert scheduler.assignments.get(item.id).status == "done"


def test_assignment_turn_attach_helper_works_on_a_continue_turn(settings):
    adapter = Drawing("Half way.\nBOT-OFFICE-STATUS: continue", attach=True)
    store, scheduler, bot, _ = setup(settings, adapter, budget_turns=1)
    run_next(store, scheduler)
    message = assistant(store, bot)
    assert len(message.attachments) == 1
    assert attachments.read(settings.data_dir, bot.id, message.attachments[0]) == PNG
