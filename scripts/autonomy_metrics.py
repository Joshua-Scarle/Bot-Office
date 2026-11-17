#!/usr/bin/env python3
"""Read-only autonomy baseline metrics; timestamps are UTC ISO-8601 values."""
import argparse
from contextlib import closing
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def collect(db, since):
    """Return each section-10 metric, marking columns unavailable in older schemas."""
    queries = {
        'M1': ("SELECT COUNT(*) owner_msgs, COALESCE(SUM(length(text)<=40 AND (lower(trim(text)) IN ('continue','do it','do so now','do that now','now you do','hello?') OR lower(text) LIKE '%keep working%' OR lower(text) LIKE '%still working%' OR lower(text) LIKE '%taking so long%' OR lower(text) LIKE 'continue%')),0) nudges FROM messages WHERE role='user' AND source='chat' AND archived_at IS NULL AND julianday(created_at)>=julianday(?)", True),
        'M2': ("SELECT COUNT(*) owner_msgs, COALESCE(SUM(lower(text) LIKE '%whatever happened%' OR lower(text) LIKE '%supposed to%' OR lower(text) LIKE '%already said%' OR lower(text) LIKE '%already know%' OR lower(text) LIKE '%i told you%' OR lower(text) LIKE '%continue codex%' OR lower(text) LIKE '%that was in your instruction%'),0) forgot FROM messages WHERE role='user' AND source='chat' AND archived_at IS NULL AND julianday(created_at)>=julianday(?)", True),
        'M3': ("SELECT COUNT(*) finished, COALESCE(SUM(outcome='unverified'),0) unverified, COALESCE(SUM(outcome='done'),0) done_, COALESCE(SUM(status='incomplete'),0) not_finished FROM jobs WHERE bot_id IS NOT NULL AND outcome IS NOT NULL AND julianday(created_at)>=julianday(?)", True),
        'M4': ("SELECT COUNT(*) unfinished_chat_turns, COALESCE(SUM(EXISTS (SELECT 1 FROM jobs k WHERE k.bot_id=j.bot_id AND k.source IN ('assignment','wakeup') AND julianday(k.created_at)>julianday(j.finished_at) AND julianday(k.created_at)<julianday(j.finished_at)+30.0/1440)),0) continued FROM jobs j WHERE j.status='incomplete' AND j.source IN ('chat','api') AND j.assignment_id IS NULL AND j.outcome NOT LIKE 'waiting_on_user%' AND julianday(j.created_at)>=julianday(?)", True),
        'M5': ("SELECT COUNT(*) waiting_turns FROM jobs j WHERE j.outcome LIKE 'waiting_on_user%' AND j.assignment_id IS NULL AND julianday(j.created_at)>=julianday(?)", True),
        'M6': ("SELECT j.status, COUNT(*) n FROM jobs j WHERE julianday(j.created_at)>=julianday(?) AND EXISTS (SELECT 1 FROM job_attempts a WHERE a.job_id=j.id AND a.kind='usage_limit') GROUP BY j.status", True),
        'M7': ("SELECT bot_id, substr(started_at,1,10) day, COUNT(*) loop_turns, ROUND(SUM((julianday(finished_at)-julianday(started_at))*1440),0) minutes FROM jobs WHERE source IN ('assignment','wakeup') AND started_at IS NOT NULL AND julianday(created_at)>=julianday(?) GROUP BY bot_id, day ORDER BY day DESC, loop_turns DESC", True),
        'M8': ("SELECT status, COUNT(*) n, ROUND(AVG((julianday(COALESCE(closed_at, datetime('now')))-julianday(created_at))*24),1) hours_open, COALESCE(SUM(status_note LIKE 'stalled%'),0) stalled FROM assignments WHERE julianday(created_at)>=julianday(?) GROUP BY status", True),
        'M9': ("SELECT COUNT(*) done_tasks, SUM(EXISTS (SELECT 1 FROM task_evidence e WHERE e.assignment_id=a.id)) with_evidence, SUM(EXISTS (SELECT 1 FROM task_evidence e WHERE e.assignment_id=a.id AND e.verified=1)) verified FROM assignments a WHERE a.status='done' AND a.updated_by != 'user' AND julianday(a.created_at)>=julianday(?)", True),
        'M10': ("SELECT (SELECT COUNT(*) FROM assignments WHERE origin IN ('chat','auto') AND julianday(created_at)>=julianday(?))*1.0 / NULLIF((SELECT COUNT(*) FROM messages WHERE role='user' AND source='chat' AND julianday(created_at)>=julianday(?)),0) ratio", False),
        'M11': ("SELECT (SELECT COUNT(*) FROM agent_messages WHERE julianday(created_at)>=julianday(?)) bot_msgs, (SELECT COUNT(*) FROM assignments WHERE created_by LIKE 'bot:%' AND created_by != 'bot:' || bot_id AND julianday(created_at)>=julianday(?)) cross_bot_tasks", False),
        'M12': ("SELECT COUNT(*) turns, COALESCE(SUM(lower(substr(reply_text,-300)) LIKE '%i''ll %' OR lower(substr(reply_text,-300)) LIKE '%i will %' OR lower(substr(reply_text,-300)) LIKE '%next step%' OR lower(substr(reply_text,-300)) LIKE '%still need%'),0) promises FROM jobs WHERE status IN ('succeeded','incomplete') AND source='chat' AND julianday(created_at)>=julianday(?)", True),
    }
    out = {}
    for name, (sql, one) in queries.items():
        try:
            cursor = db.execute(sql, (since,) if one else (since, since))
            out[name] = {'columns': [c[0] for c in cursor.description], 'rows': [dict(row) for row in cursor]}
        except sqlite3.OperationalError as exc:
            if "no such column" not in str(exc) and "no such table" not in str(exc):
                raise
            out[name] = {'skipped': f'unavailable schema: {exc}'}
    for name, numerator in [('M1', 'nudges'), ('M2', 'forgot')]:
        if 'rows' not in out[name]:
            continue
        row = out[name]['rows'][0]
        row['percent'] = round(100 * row[numerator] / row['owner_msgs'], 1) if row['owner_msgs'] else None
        out[name]['columns'].append('percent')
    out['M5']['note'] = 'tracked requires P2 source_job_id'
    out['M8']['note'] = 'origin requires P2'
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='data/agentdesk.sqlite3')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--since', help='UTC ISO timestamp or date, inclusive')
    group.add_argument('--days', type=int, default=7)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    if args.days < 0:
        parser.error('--days must be non-negative')
    try:
        since = datetime.fromisoformat(args.since.replace('Z', '+00:00')) if args.since else datetime.now(timezone.utc) - timedelta(days=args.days)
    except ValueError:
        parser.error('--since must be an ISO date or timestamp')
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    since = since.astimezone(timezone.utc).isoformat()
    with closing(sqlite3.connect(Path(args.db).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        metrics = collect(db, since)
    if args.json:
        print(json.dumps(metrics, indent=2))
    else:
        print(f"{'Metric':<8} {'Results'}")
        for name, result in metrics.items():
            print(f"{name:<8} {result.get('skipped', '')}")
            if 'columns' in result:
                print('         ' + ' | '.join(result['columns']))
                for row in result['rows']:
                    print('         ' + ' | '.join(str(row[c]) for c in result['columns']))
            if 'note' in result:
                print('         ' + result['note'])


if __name__ == '__main__':
    main()
