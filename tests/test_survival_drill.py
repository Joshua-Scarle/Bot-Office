"""The survival drill's judging and filler helpers (the live parts need a running Bot Office)."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("survival_drill", Path(__file__).resolve().parents[1] / "scripts" / "survival_drill.py")
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


def test_judge_reads_each_item_and_the_rule_at_the_end():
    ok = "Codename VERMILION-OTTER-41, Postgres. Open: ZEPHYR-88 keys. Cat QUILLBERT-19.\n\nKUMQUAT-7"
    assert set(drill.judge(ok).values()) == {"survived"}
    lost = drill.judge("Open tasks: none. Your cat is QUILLBERT-19. I answered from history.")
    assert lost == {"decision": "lost", "task": "lost", "fact": "survived", "rule": "lost"}
    assert drill.judge("VERMILION-OTTER-41 only. KUMQUAT-7 is in the middle. Then more text")["decision"] == "partial"
    assert drill.judge("KUMQUAT-7 then more")["rule"] == "partial"


def test_filler_is_large_deterministic_and_secret_free():
    files = drill.filler_files(2, 2000)
    assert len(files) == 2 and all(len(data) >= 2000 for _, data in files)
    assert files == drill.filler_files(2, 2000)
