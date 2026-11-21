#!/usr/bin/env python3
"""Rollback drain check (stdlib only). Exit 0 only when the notifier has recorded notify-freeze (it has seen
NOTIFY_OFF) AND nothing up to the freeze is unacknowledged. A missing marker means "not drained": the counts
mean nothing until the notifier has frozen. Usage: notify_drained.py <agentdesk.sqlite3>"""
import pathlib
import sqlite3
import sys


def drained(db_path: str) -> tuple[bool, str]:
    con = sqlite3.connect(pathlib.Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = con.execute("SELECT value FROM meta WHERE key='notify-freeze'").fetchone()
        if row is None:
            return False, "not drained: notifier has not seen NOTIFY_OFF (no notify-freeze marker)"
        cursor = con.execute("SELECT value FROM meta WHERE key='notify-cursor'").fetchone()
        n = con.execute("SELECT COUNT(*) FROM notify_events WHERE seq > ? AND seq <= ?",
                        (int(cursor[0]) if cursor else 0, int(row[0]))).fetchone()[0]
    finally:
        con.close()
    return n == 0, "drained" if n == 0 else f"not drained: {n} unacknowledged up to the freeze"


if __name__ == "__main__":
    try:
        ok, message = drained(sys.argv[1])
    except Exception as exc:  # an unreadable database is never "drained"
        ok, message = False, f"not drained: {type(exc).__name__}"
    print(message)
    sys.exit(0 if ok else 1)
