"""0.12.20: new bots land in their group on every creation path; retiring moves a bot to "Retired"."""
import pytest
from fastapi.testclient import TestClient

from botoffice import grouping
from botoffice.api import create_app
from botoffice.providers.fake import FakeAdapter
from test_agent_assignments import running_token

H = {"Authorization": "Bearer test-token"}


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter(enabled=False)})) as c:
        yield c


def group_of(client, bot_id):
    layout = client.get("/v1/sections", headers=H).json()
    for s in layout["sections"]:
        if bot_id in s["bot_ids"]:
            return s["name"]
    return "Ungrouped" if bot_id in layout["ungrouped"]["bot_ids"] else "Hidden"


def mk_section(client, name):
    r = client.post("/v1/sections", headers=H, json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def make(client, name, **extra):
    r = client.post("/v1/bots", headers=H, json={"name": name, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def test_explicit_group_by_name_and_id_beats_everything(client):
    mk_section(client, "MS")
    other = mk_section(client, "Other")
    assert group_of(client, make(client, "MS One", group="other")["id"]) == "Other"
    assert group_of(client, make(client, "MS Two", group=other["id"])["id"]) == "Other"


def test_explicit_group_unknown_name_creates_it_and_ungrouped_is_honoured(client):
    assert group_of(client, make(client, "Foo", group="Brand New")["id"]) == "Brand New"
    assert group_of(client, make(client, "MS Bar", group="Ungrouped")["id"]) == "Ungrouped"


def test_creator_group_beats_name_prefix(client):
    mk_section(client, "MS")
    x = mk_section(client, "X")
    lead = make(client, "Lead", group="X")
    assert group_of(client, make(client, "MS Child", created_by=lead["id"])["id"]) == x["name"]
    assert group_of(client, make(client, "MS Child2", created_by="Lead")["id"]) == "X"


def test_creator_in_unsorted_or_retired_falls_through_to_prefix(client):
    mk_section(client, "MS")
    lead = make(client, "Zzz Lead")           # lands in Unsorted
    assert group_of(client, lead["id"]) == "Unsorted"
    assert group_of(client, make(client, "MS Kid", created_by=lead["id"])["id"]) == "MS"


@pytest.mark.parametrize("name,want", [
    ("MS ML Engineer", "MS"), ("ms lower", "MS"), ("MPG DevOps (BO)", "MPG"), ("Web Lead", "Web"),
    ("BT Lead (BO)", "BT"), ("devops thing", "DevOps"), ("Bot-Office Ops", "Bot-Office"),
    ("BO Helper", "Bot-Office"), ("botoffice x", "Bot-Office"), ("Engineering Lead (BO)", "Engineering"),
])
def test_name_prefix_rules_with_existing_groups(client, name, want):
    for g in ("MS", "MPG", "Web", "BT", "DevOps", "Bot-Office", "Engineering"):
        mk_section(client, g)
    assert group_of(client, make(client, name)["id"]) == want


def test_alias_creates_missing_canonical_group_once(client):
    a = make(client, "MS One")
    b = make(client, "MS Two")
    assert group_of(client, a["id"]) == group_of(client, b["id"]) == "MS"
    assert [s["name"] for s in client.get("/v1/sections", headers=H).json()["sections"]].count("MS") == 1


def test_unknown_prefix_goes_to_unsorted_and_creates_it_once(client):
    a = make(client, "Zzz Probe")
    b = make(client, "Yyy Probe")
    assert group_of(client, a["id"]) == group_of(client, b["id"]) == "Unsorted"
    names = [s["name"] for s in client.get("/v1/sections", headers=H).json()["sections"]]
    assert names.count("Unsorted") == 1


def test_existing_unsorted_is_reused_case_insensitively(client):
    mk_section(client, "UNSORTED")
    assert group_of(client, make(client, "Qqq")["id"]) == "UNSORTED"


def test_bot_created_path_inherits_the_creators_group(client):
    store = client.app.state.store
    mk_section(client, "MS")
    mk_section(client, "X")
    lead = make(client, "Lead", group="X")
    _job, ah = running_token(store, store.get_bot(lead["id"]))
    r = client.post("/v1/agent/bots", headers=ah, json={"name": "MS Worker"})
    assert r.status_code == 201, r.text
    assert group_of(client, r.json()["id"]) == "X"
    r = client.post("/v1/agent/bots", headers=ah, json={"name": "Elsewhere", "group": "MS"})
    assert group_of(client, r.json()["id"]) == "MS"
    assert client.post("/v1/agent/bots", headers=H, json={"name": "nope"}).status_code == 401


def test_helper_is_callable_directly_for_a_router_intake(client):
    store = client.app.state.store
    mk_section(client, "MPG")
    bot = store.create_bot("MPG Router Made", "", "codex", None)
    section_id = grouping.place_new_bot(store, bot)
    assert group_of(client, str(bot.id)) == "MPG" and section_id


def test_retire_moves_to_retired_and_keeps_the_bot(client):
    bot = make(client, "MS Old")
    assert group_of(client, bot["id"]) == "MS"
    r = client.post(f"/v1/bots/{bot['id']}/retire", headers=H)
    assert r.status_code == 200
    assert group_of(client, bot["id"]) == "Retired"
    assert client.get(f"/v1/bots/{bot['id']}", headers=H).status_code == 200
    assert client.post("/v1/bots/00000000-0000-0000-0000-000000000000/retire", headers=H).status_code == 404


def test_retire_reuses_existing_retired_section(client):
    mk_section(client, "retired")
    bot = make(client, "Zzz Old")
    client.post(f"/v1/bots/{bot['id']}/retire", headers=H)
    assert group_of(client, bot["id"]) == "retired"


def test_placement_failure_never_fails_creation(client, monkeypatch):
    monkeypatch.setattr(grouping, "infer_section", lambda *a, **k: (_ for _ in ()).throw(grouping.sections_mod.SectionError(409, "x")))
    assert group_of(client, make(client, "Whatever")["id"]) == "Ungrouped"
