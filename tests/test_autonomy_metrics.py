import json
import subprocess
import sys
from pathlib import Path

from botoffice.store import JobStore


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'autonomy_metrics.py'


def test_metrics_readonly_and_counts(tmp_path):
    path = tmp_path / 'metrics.db'
    store = JobStore(path)
    bot = store.create_bot('Ada', '', 'codex', None)
    for text in ['Continue', 'still working?', *['ordinary message'] * 8]:
        store.add_message(bot.id, 'user', text)
    job = store.create('test', bot_id=bot.id, source='chat')
    with store._connect() as db:
        db.execute("UPDATE jobs SET status='incomplete', outcome='not_done' WHERE id=?", (str(job.id),))
    before = path.read_bytes()
    output = subprocess.check_output([sys.executable, str(SCRIPT), '--db', str(path), '--since', '2000-01-01', '--json'], text=True)
    metrics = json.loads(output)
    assert set(metrics) == {f'M{i}' for i in range(1, 13)}
    assert metrics['M1']['rows'][0] == {'owner_msgs': 10, 'nudges': 2, 'percent': 20.0}
    assert metrics['M3']['rows'][0]['not_finished'] == 1
    assert metrics['M4']['rows'][0]['unfinished_chat_turns'] == 1
    assert metrics['M5']['columns'] == ['waiting_turns']
    # task_evidence is P5; assignments.origin exists since 0.11.4 (P2), so M10 is computed
    assert 'skipped' in metrics['M9'] and metrics['M10']['rows'][0]['ratio'] == 0
    assert metrics['M11']['rows'][0]['bot_msgs'] == 0
    assert path.read_bytes() == before
    assert 'owner_msgs | nudges | percent' in subprocess.check_output(
        [sys.executable, str(SCRIPT), '--db', str(path), '--days', '7'], text=True)


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / 'missing.db'
    result = subprocess.run([sys.executable, str(SCRIPT), '--db', str(path)], capture_output=True)
    assert result.returncode != 0
    assert not path.exists()


def test_since_and_days_filter(settings, tmp_path):
    from datetime import UTC, datetime, timedelta
    path = tmp_path / 'filter.db'
    store = JobStore(path)
    bot = store.create_bot('Ada', '', 'codex', None)
    for text in ('old', 'new'):
        store.add_message(bot.id, 'user', text)
    now = datetime.now(UTC)
    with store._connect() as db:
        db.execute("UPDATE messages SET created_at=? WHERE text='old'", ((now - timedelta(days=10)).isoformat(),))
        db.execute("UPDATE messages SET created_at=? WHERE text='new'", ((now - timedelta(days=1)).isoformat(),))
    for flag, value in [('--days', '7'), ('--since', (now - timedelta(days=7)).isoformat())]:
        data = json.loads(subprocess.check_output([sys.executable, str(SCRIPT), '--db', str(path), flag, value, '--json'], text=True))
        assert data['M1']['rows'][0]['owner_msgs'] == 1


def test_all_metrics_handle_missing_columns(tmp_path):
    import sqlite3
    import importlib.util
    spec = importlib.util.spec_from_file_location('autonomy_metrics', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with sqlite3.connect(':memory:') as db:
        db.row_factory = sqlite3.Row
        for table in ('messages', 'jobs', 'assignments', 'agent_messages', 'job_attempts'):
            db.execute(f'CREATE TABLE {table} (id TEXT)')
        metrics = module.collect(db, '2000-01-01T00:00:00+00:00')
        assert all('skipped' in metric for metric in metrics.values())
