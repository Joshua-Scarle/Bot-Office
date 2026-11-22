"""0.12.24 one-time: date the notes of memory files written before note ages existed.

Usage: python -I scripts/backfill_note_ages.py [--dry-run]   (the server's environment: BOT_OFFICE_DATA_DIR etc.;
run it while the server is paused or stopped, before or right after the 0.12.24 restart)

Reads each bot's memory.md and WORKING.md and the shared memory.md; for every file that has no
``.ages.json`` ledger yet, dates each note from the recorded memory-edit job events (time and the bot
that wrote it), else from the newest date written in the note, else "on or before" the file's time.
Files that already have a ledger are left alone, so running it twice is harmless.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from botoffice import freshness, isolation, safefs
    from botoffice.config import Settings
    from botoffice.memory import Memory

    settings = Settings.from_env()
    isolation.configure(settings)  # ledgers live where the server keeps controller-only state
    data_dir = settings.data_dir.resolve()
    memory = Memory(data_dir)
    safefs.add_root(data_dir)
    db = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)  # private_dir when isolated
    db.row_factory = sqlite3.Row
    with closing(db):
        names = {row["id"]: row["name"] for row in db.execute("SELECT id, name FROM bots")}
        events: dict[str, list] = {}
        for row in db.execute("SELECT e.created_at, j.bot_id, e.data FROM job_events e JOIN jobs j ON j.id=e.job_id "
                              "WHERE e.kind='memory'"):
            try:
                data = json.loads(row["data"])
                moment = datetime.fromisoformat(row["created_at"])
            except (ValueError, TypeError):
                continue
            target = "shared" if data.get("file") == "shared" else row["bot_id"]
            events.setdefault(target, []).append((moment, names.get(row["bot_id"], "a bot"), data.get("diff") or ""))
    files = [(data_dir / "shared" / "memory.md", events.get("shared", []))]
    for folder in sorted((data_dir / "bots").iterdir()):
        files.append((folder / "memory.md", events.get(folder.name, [])))
        files.append((folder / "WORKING.md", []))
    total = 0
    for path, found in files:
        if not path.is_file() or safefs.lstat(freshness.ledger_path(path)) is not None:
            continue
        if dry:
            print(f"would date {path} ({len(found)} edit events)")
            continue
        with memory._lock(path.parent):  # the same lock as every memory write
            count = freshness.backfill(path, found)
        total += count
        if count:
            print(f"dated {count} notes in {path.relative_to(data_dir)}")
    print(f"done: {total} notes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
