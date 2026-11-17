import os

import pytest
from fastapi.testclient import TestClient

from botoffice import attachments
from botoffice.api import create_app
from botoffice.providers.fake import FakeAdapter

H = {"Authorization": "Bearer test-token"}
IMAGES = [b"\x89PNG\r\n\x1a\nimage", b"\xff\xd8\xffimage", b"GIF89aimage", b"RIFF1234WEBPimage"]


@pytest.fixture
def client_bot(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        bot = client.post('/v1/bots', headers=H, json={'name': 'pictures'}).json()['id']
        yield client, bot, settings


@pytest.mark.parametrize('data', IMAGES)
def test_upload_and_get(client_bot, data):
    client, bot, settings = client_bot
    base = f'/v1/bots/{bot}/attachments'
    response = client.post(base, headers=H, content=data)
    assert response.status_code == 201
    info = response.json()
    assert info['size'] == len(data)
    result = client.get(info['url'], headers=H)
    assert result.content == data and result.headers['content-type'] == info['mime']
    assert result.headers['x-content-type-options'] == 'nosniff'
    assert result.headers['content-disposition'] == 'inline'
    assert result.headers['cache-control'] == 'private, no-store'
    assert result.headers['content-security-policy'] == "default-src 'none'; sandbox"
    path = settings.data_dir / 'bots' / bot / 'attachments' / info['id']
    assert path.stat().st_mode & 0o777 == 0o400
    assert path.parent.stat().st_mode & 0o777 == 0o700
    message = client.post(f'/v1/bots/{bot}/messages', headers=H, json={'text': 'look', 'attachments': [info['id']]})
    assert message.status_code == 201
    assert client.get(f'/v1/bots/{bot}/messages', headers=H).json()[0]['attachments'] == [info['id']]


def test_rejections_and_quota(client_bot, monkeypatch):
    client, bot, settings = client_bot
    base = f'/v1/bots/{bot}/attachments'
    assert client.post(base, content=IMAGES[0]).status_code == 401
    assert client.post(base, headers=H, content=b'').status_code == 400
    for data in (b'<html>', b'<svg>', b'%PDF-1.0'):
        assert client.post(base, headers=H, content=data).status_code == 415
    assert client.post(base, headers=H, content=b'x' * (attachments.MAX_BYTES + 1)).status_code == 413
    name = client.post(base, headers=H, content=IMAGES[0]).json()['id']
    other = client.post('/v1/bots', headers=H, json={'name': 'other'}).json()['id']
    assert client.post(f'/v1/bots/{other}/messages', headers=H, json={'text': 'x', 'attachments': [name]}).status_code == 400
    for bad in ('%2e%2e', 'x' * 300, '%2Fetc%2Fpasswd'):
        assert client.get(f'{base}/{bad}', headers=H).status_code in (404, 422)
    monkeypatch.setattr(attachments, 'MAX_COUNT', 1)
    assert client.post(base, headers=H, content=IMAGES[0]).status_code == 507
    link = settings.data_dir / 'bots' / other / 'attachments'
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(settings.data_dir / 'bots' / bot / 'attachments', target_is_directory=True)
    assert client.post(f'/v1/bots/{other}/attachments', headers=H, content=IMAGES[0]).status_code == 400
    assert client.get(f'/v1/bots/{other}/attachments/{name}', headers=H).status_code == 404


def test_provider_delivery(settings, client_bot):
    from botoffice.domain import Job, JobStatus
    from botoffice.providers.codex import CodexAdapter
    from botoffice.providers.opencode import OpenCodeAdapter
    from botoffice.providers.antigravity import AntigravityAdapter
    from botoffice.providers.claude import ClaudeAdapter
    from botoffice.providers.cursor import CursorAdapter
    from botoffice.workspace import prepare_bot_workspace
    from botoffice.memory import Memory
    from botoffice.store import JobStore, now
    from uuid import uuid4

    client, bot_id, settings = client_bot
    base = f'/v1/bots/{bot_id}'
    ids = [client.post(base + '/attachments', headers=H, content=image).json()['id'] for image in IMAGES[:2]]
    posted = client.post(base + '/messages', headers=H, json={'text': 'look', 'attachments': ids}).json()
    store = JobStore(settings.data_dir / 'agentdesk.sqlite3')
    job = store.get(posted['job']['id'])
    workspace = settings.data_dir / 'test-workspace'
    workspace.mkdir()
    prompt, _, _ = prepare_bot_workspace(store, Memory(settings.data_dir), store.get_bot(bot_id), job, workspace)
    assert len(job.attachment_paths) == 2
    stable = (settings.data_dir / 'bots' / str(bot_id) / 'attachments').resolve().as_posix()
    assert all(path in prompt and path.startswith(stable + '/') for path in job.attachment_paths)
    assert all(path in (workspace / 'prompt-resume.txt').read_text() for path in job.attachment_paths)
    assert all(path in (workspace / 'history.md').read_text() for path in job.attachment_paths)
    index = (settings.data_dir / 'bots' / str(bot_id) / 'attachments' / 'INDEX.md').read_text()
    assert all(path in index for path in job.attachment_paths) and 'message ' in index
    codex = CodexAdapter()._exec_args(workspace, job)
    assert [arg for arg in codex if arg.startswith('--image=')] == ['--image=' + p for p in job.attachment_paths]
    assert '-i' not in codex
    opencode = OpenCodeAdapter()._exec_args(workspace, job)
    assert all('--file=' + path in opencode for path in job.attachment_paths)
    job.prompt = prompt
    assert str(workspace / 'prompt.txt') in AntigravityAdapter()._exec_args(workspace, job)[1]  # 0.10.5: absolute pointer
    assert all(path in (workspace / 'prompt.txt').read_text() for path in job.attachment_paths)
    assert str(workspace / 'prompt.txt') in CursorAdapter()._exec_args(workspace, job)[-1]
    assert ClaudeAdapter()._stdin(job) == prompt.encode()


def test_upload_rate_limit(client_bot):
    client, bot, _ = client_bot
    url = f'/v1/bots/{bot}/attachments'
    for _ in range(30):
        assert client.post(url, headers=H, content=b'invalid').status_code == 415
    assert client.post(url, headers=H, content=IMAGES[0]).status_code == 429


def test_new_bot_directory_and_retention(client_bot, monkeypatch):
    import time
    client, bot, settings = client_bot
    base = f'/v1/bots/{bot}'
    bot_dir = settings.data_dir / 'bots' / bot
    import shutil
    shutil.rmtree(bot_dir)
    old = client.post(base + '/attachments', headers=H, content=IMAGES[0]).json()['id']
    kept = client.post(base + '/attachments', headers=H, content=IMAGES[1]).json()['id']
    assert bot_dir.stat().st_mode & 0o777 == 0o700
    posted = client.post(base + '/messages', headers=H, json={'text': 'look', 'attachments': [kept, kept]})
    assert posted.json()['message']['attachments'] == [kept]
    assert client.delete(base + '/attachments/' + kept, headers=H).status_code == 409
    past = time.time() - attachments.UNREFERENCED_TTL - 10
    for name in (old, kept):
        os.utime(bot_dir / 'attachments' / name, (past, past))
    monkeypatch.setattr(attachments, 'MAX_COUNT', 2)
    new = client.post(base + '/attachments', headers=H, content=IMAGES[0])
    assert new.status_code == 201
    assert not (bot_dir / 'attachments' / old).exists()
    assert (bot_dir / 'attachments' / kept).exists()
    assert client.delete(base + '/attachments/' + new.json()['id'], headers=H).status_code == 204
    assert client.delete(base, headers=H).status_code == 204
    assert not bot_dir.exists()


def test_global_quota(client_bot, monkeypatch):
    client, bot, _ = client_bot
    other = client.post('/v1/bots', headers=H, json={'name': 'other quota'}).json()['id']
    monkeypatch.setattr(attachments, 'GLOBAL_MAX_TOTAL', len(IMAGES[0]))
    assert client.post(f'/v1/bots/{bot}/attachments', headers=H, content=IMAGES[0]).status_code == 201
    assert client.post(f'/v1/bots/{other}/attachments', headers=H, content=IMAGES[0]).status_code == 507


def test_atomic_message_failure(client_bot):
    import sqlite3
    client, bot, settings = client_bot
    path = settings.data_dir / 'agentdesk.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER reject_message BEFORE INSERT ON messages BEGIN SELECT RAISE(FAIL, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        client.post(f'/v1/bots/{bot}/messages', headers=H, json={'text': 'test'})
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM jobs WHERE bot_id=?', (bot,)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM messages WHERE bot_id=?', (bot,)).fetchone()[0] == 0


def test_workspace_copies_and_missing(client_bot, tmp_path):
    from botoffice.workspace import prepare_bot_workspace
    from botoffice.memory import Memory
    from botoffice.store import JobStore
    client, bot, settings = client_bot
    base = f'/v1/bots/{bot}'
    name = client.post(base + '/attachments', headers=H, content=IMAGES[0]).json()['id']
    job_id = client.post(base + '/messages', headers=H, json={'text': 'look', 'attachments': [name]}).json()['job']['id']
    store = JobStore(settings.data_dir / 'agentdesk.sqlite3')
    job = store.get(job_id)
    source = settings.data_dir / 'bots' / bot / 'attachments' / name
    workspace = tmp_path / 'job'; workspace.mkdir()
    def prepare():
        return prepare_bot_workspace(store, Memory(settings.data_dir), store.get_bot(bot), job, workspace)
    prepare()
    # the bot gets the stable stored file itself (read-only), not a per-job copy
    assert job.attachment_paths == [str(source.resolve())]
    assert not (workspace / '.bot-office-attachments').exists()
    source.unlink()
    with pytest.raises(FileNotFoundError, match='attachment .* is missing'):
        prepare()


def test_startup_prunes_unreferenced(settings):
    import time
    app = create_app(settings, {"codex": FakeAdapter()})
    with TestClient(app) as client:
        bot = client.post('/v1/bots', headers=H, json={'name': 'startup'}).json()['id']
        base = f'/v1/bots/{bot}'
        old = client.post(base + '/attachments', headers=H, content=IMAGES[0]).json()['id']
        kept = client.post(base + '/attachments', headers=H, content=IMAGES[1]).json()['id']
        client.post(base + '/messages', headers=H, json={'text': 'keep', 'attachments': [kept]})
        directory = settings.data_dir / 'bots' / bot / 'attachments'
        past = time.time() - attachments.UNREFERENCED_TTL - 10
        for name in (old, kept): os.utime(directory / name, (past, past))
    with TestClient(create_app(settings, {"codex": FakeAdapter()})):
        assert not (directory / old).exists()
        assert (directory / kept).exists()


@pytest.mark.parametrize('outcome', ['succeeded', 'failed', 'cancelled'])
def test_job_attachment_cleanup(settings, outcome):
    import asyncio
    from botoffice.domain import Slot
    from botoffice.scheduler import Scheduler
    from botoffice.store import JobStore
    async def scenario():
        settings.ensure_dirs()
        store = JobStore(settings.data_dir / 'agentdesk.sqlite3')
        adapter = FakeAdapter(result=outcome, delay=10 if outcome == 'cancelled' else 0)
        scheduler = Scheduler(store, settings, {'codex': adapter})
        bot = store.create_bot('cleanup', '', 'codex', None)
        name = attachments.save(settings.data_dir, bot.id, IMAGES[0], 'image/png')
        _, job = scheduler.turns.post_message(bot, 'look', attachments=[name])
        task = asyncio.create_task(scheduler._execute(Slot.DESKTOP_1, job))
        if outcome == 'cancelled':
            while not adapter.started: await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
        else:
            await task
        assert not (settings.data_dir / 'workspaces' / str(job.id) / '.bot-office-attachments').exists()
        assert (settings.data_dir / 'bots' / str(bot.id) / 'attachments' / name).exists()
    asyncio.run(scenario())


def test_cleanup_preserves_agent_output_and_attachment_free_job(settings):
    import asyncio
    from botoffice.domain import Slot
    from botoffice.scheduler import Scheduler
    from botoffice.store import JobStore

    async def scenario():
        settings.ensure_dirs()
        store = JobStore(settings.data_dir / 'agentdesk.sqlite3')
        scheduler = Scheduler(store, settings, {'codex': FakeAdapter()})
        bot = store.create_bot('output', '', 'codex', None)
        for with_image in (False, True):
            ids = [attachments.save(settings.data_dir, bot.id, IMAGES[0], 'image/png')] if with_image else []
            _, job = scheduler.turns.post_message(bot, 'look', attachments=ids)
            workspace = settings.data_dir / 'workspaces' / str(job.id)
            (workspace / 'attachments').mkdir(parents=True)
            (workspace / 'attachments' / 'output').write_text('keep')
            owned = workspace / '.bot-office-attachments'
            owned.mkdir()
            (owned / 'existing').write_text('keep')
            await scheduler._execute(Slot.DESKTOP_1, job)
            assert (workspace / 'attachments' / 'output').read_text() == 'keep'
            assert (owned / 'existing').read_text() == 'keep'  # jobs no longer copy or delete images
    asyncio.run(scenario())


def test_startup_cleans_only_inactive_job_copies(settings):
    import asyncio
    from botoffice.domain import JobStatus, Slot
    from botoffice.scheduler import Scheduler
    from botoffice.store import JobStore

    async def scenario():
        settings.ensure_dirs()
        store = JobStore(settings.data_dir / 'agentdesk.sqlite3')
        scheduler = Scheduler(store, settings, {'codex': FakeAdapter()})
        bot = store.create_bot('recovery', '', 'codex', None)
        jobs = [scheduler.turns.post_message(bot, str(i))[1] for i in range(3)]
        assert store.claim(Slot.DESKTOP_1, frozenset({'codex'})).id == jobs[0].id
        store.finish(jobs[0].id, JobStatus.SUCCEEDED)
        claimed = store.claim(Slot.DESKTOP_1, frozenset({'codex'}))
        assert claimed.id == jobs[1].id
        for job in jobs:
            workspace = settings.data_dir / 'workspaces' / str(job.id)
            for name in ('.bot-office-attachments', 'attachments'):
                (workspace / name).mkdir(parents=True)
                (workspace / name / 'image').write_text('data')
        await scheduler.start()
        try:
            # jobs[1] was running: restart recovery re-queues it, so it is active, not stale
            for job in jobs[:1]:
                workspace = settings.data_dir / 'workspaces' / str(job.id)
                assert not (workspace / '.bot-office-attachments').exists()
                assert (workspace / 'attachments' / 'image').exists()
            assert (settings.data_dir / 'workspaces' / str(jobs[2].id) / '.bot-office-attachments' / 'image').exists()
        finally:
            await scheduler.stop()
    asyncio.run(scenario())


def test_parallel_upload_limits(settings, monkeypatch):
    import asyncio
    import httpx
    async def scenario():
        app = create_app(settings, {'codex': FakeAdapter()})
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                bot = (await client.post('/v1/bots', headers=H, json={'name': 'parallel'})).json()['id']
                url = f'/v1/bots/{bot}/attachments'
                assert (await client.post(url, headers={**H, 'Content-Length': str(attachments.MAX_BYTES + 1)}, content=b'')).status_code == 413
                started = 0
                ready = asyncio.Event()
                release = asyncio.Event()
                async def stream():
                    nonlocal started
                    started += 1
                    if started == 4: ready.set()
                    await release.wait()
                    yield IMAGES[0]
                tasks = [asyncio.create_task(client.post(url, headers=H, content=stream())) for _ in range(4)]
                await asyncio.wait_for(ready.wait(), 2)
                assert (await client.post(url, headers=H, content=IMAGES[0])).status_code == 429
                monkeypatch.setattr(attachments, 'MAX_COUNT', 1)
                release.set()
                results = await asyncio.gather(*tasks)
                assert sorted(r.status_code for r in results) == [201, 507, 507, 507]
    asyncio.run(scenario())
