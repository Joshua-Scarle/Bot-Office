"""0.12.34: Clear hides a Needs-you notice until it changes; pending cards and questions stay."""
from botoffice import needs_you


def test_clear_hides_notices_but_never_pending_cards_or_questions(tmp_path, monkeypatch):
    from botoffice.store import JobStore
    store = JobStore(tmp_path / "db.sqlite3")
    raw = [
        {"id": "r:b:crash:j1", "kind": "repeat", "state": "failing"},
        {"id": "p:5:pending", "kind": "approval", "state": "pending"},
        {"id": "p:6:expired", "kind": "approval", "state": "expired"},
        {"id": "q:9:open", "kind": "question", "state": "open"},
    ]
    real = needs_you._without_cleared
    monkeypatch.setattr(needs_you, "needs_you", lambda s: real(s, [dict(item) for item in raw]))
    ids = [item["id"] for item in raw] + ["x:unknown"]
    assert needs_you.clear(store, ids) == 2
    left = needs_you.needs_you(store)
    assert [item["id"] for item in left] == ["p:5:pending", "q:9:open"]
    assert all(item["clearable"] is False for item in left)
    # the same failure with a new job id is a new item: it shows again
    raw[0]["id"] = "r:b:crash:j2"
    assert "r:b:crash:j2" in [item["id"] for item in needs_you.needs_you(store)]


def test_real_list_marks_clearable(tmp_path):
    from botoffice.store import JobStore
    store = JobStore(tmp_path / "db.sqlite3")
    assert needs_you.needs_you(store) == []
    assert needs_you.clear(store, ["nothing"]) == 0
