"""One-off (NOT run): place every ungrouped bot by its name prefix. Usage: python scripts_regroup_existing.py [--apply]"""
import sys
from botoffice import grouping
from botoffice.config import Settings
from botoffice.store import JobStore

store = JobStore(Settings.from_env().db_path)
layout = store.sections.layout()
for bot in store.list_bots():
    if str(bot.id) in layout["ungrouped"]["bot_ids"]:
        target = grouping.prefix_group(bot.name, [s["name"] for s in layout["sections"]])
        print(bot.name, "->", target or "(leave)")
        if target and "--apply" in sys.argv:
            store.sections.move_bot(str(bot.id), grouping.ensure_section(store, target), 10**6)
