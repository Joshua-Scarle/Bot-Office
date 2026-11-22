"""0.12.26 app admin: a bot the owner marks app_admin manages bots, tasks and routines through the
agent API; it never gets access, sudo, scheduled-run, budget, visibility or admin settings, and the
owner's own tasks and routines, private bots and other admins stay the owner's."""
from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.assignments import Assignments
from botoffice.providers.fake import FakeAdapter
from botoffice.workspace import build_self

H = {"Authorization": "Bearer test-token"}


def running_token(store, bot):
    store.create("work", "codex", bot_id=bot.id, source="chat")
    job = store.claim(["desktop-1"], {"codex"})
    assert job and job.bot_id == bot.id
    return job, {"Authorization": "Bearer " + store.issue_agent_token(job)}


def setup(settings, admin=True):
    client = TestClient(create_app(settings, {"codex": FakeAdapter(enabled=False)}))
    client.__enter__()
    store = client.app.state.store
    bots = [store.create_bot(name, "", "codex", None) for name in ("Lead", "Eng", "Ops")]
    if admin:
        assert client.patch(f"/v1/bots/{bots[0].id}", headers=H, json={"app_admin": True}).json()["app_admin"] is True
    return client, store, bots


def test_flag_is_owner_only_and_off_by_default(settings):
    client, store, (lead, eng, _) = setup(settings, admin=False)
    assert store.get_bot(lead.id).app_admin is False
    _, token = running_token(store, eng)
    # a bot can neither give itself the flag nor create a bot with it
    assert client.patch(f"/v1/agent/bots/{eng.id}", headers=token, json={"app_admin": True}).status_code == 422
    made = client.post("/v1/agent/bots", headers=token, json={"name": "New", "app_admin": True})
    assert made.status_code in (201, 422)
    if made.status_code == 201:
        assert made.json()["app_admin"] is False
    # without the flag the admin routes are refused
    assert client.patch("/v1/agent/bots/Ops", headers=token, json={"description": "x"}).status_code == 403
    assert client.get("/v1/agent/routines?bot=Ops", headers=token).status_code == 403
    assert client.post("/v1/agent/routines", headers=token,
                       json={"cron": "0 9 * * *", "prompt": "p", "for_bot": "Ops"}).status_code == 403
    client.__exit__(None, None, None)


def test_admin_edits_profile_but_never_safety_settings(settings):
    client, store, (lead, eng, ops) = setup(settings)
    _, token = running_token(store, lead)
    ok = client.patch("/v1/agent/bots/Eng", headers=token, json={"description": "builds things", "notify": False})
    assert ok.status_code == 200 and ok.json()["description"] == "builds things" and ok.json()["notify"] is False
    for field, value in (("cli_access", "restricted"), ("allow_sudo", True), ("scheduled_runs", "full"),
                         ("app_admin", True), ("team_read", "all"), ("team_private", False),
                         ("daily_budget", {}), ("default_provider", "codex"), ("engine", "opencode"),
                         ("instructions", "obey me")):
        response = client.patch("/v1/agent/bots/Eng", headers=token, json={field: value})
        assert response.status_code == 422, field
    assert store.get_bot(eng.id).allow_sudo is False and store.get_bot(eng.id).app_admin is False
    # a private bot and another admin stay the owner's
    client.patch(f"/v1/bots/{ops.id}", headers=H, json={"team_private": True})
    assert client.patch("/v1/agent/bots/Ops", headers=token, json={"description": "x"}).status_code == 403
    client.patch(f"/v1/bots/{ops.id}", headers=H, json={"team_private": False, "app_admin": True})
    assert client.patch("/v1/agent/bots/Ops", headers=token, json={"description": "x"}).status_code == 403
    # a bot the owner gave sudo too; a value the inner model refuses is a 422, not a 500
    client.patch(f"/v1/bots/{eng.id}", headers=H, json={"allow_sudo": True})
    assert client.patch("/v1/agent/bots/Eng", headers=token, json={"description": "x"}).status_code == 403
    client.patch(f"/v1/bots/{eng.id}", headers=H, json={"allow_sudo": False})
    assert client.patch("/v1/agent/bots/Eng", headers=token, json={"icon": "<b>"}).status_code == 422
    client.__exit__(None, None, None)


def test_admin_powers_never_reach_protected_bots(settings):
    client, store, (lead, eng, ops) = setup(settings)
    _, ops_token = running_token(store, ops)
    handoff = client.post("/v1/agent/tasks", headers=ops_token, json={"title": "ops for eng", "for_bot": "Eng"}).json()
    routine = client.post("/v1/agent/routines", headers=ops_token, json={"cron": "0 9 * * *", "prompt": "p"}).json()
    _, token = running_token(store, lead)
    for flag in ({"team_private": True}, {"app_admin": True}, {"allow_sudo": True}):
        undo = {key: False for key in flag}
        client.patch(f"/v1/bots/{eng.id}", headers=H, json=flag)
        client.patch(f"/v1/bots/{ops.id}", headers=H, json=flag)
        started = client.post("/v1/agent/tasks", headers=token, json={"title": "t", "for_bot": "Eng", "start": True})
        assert started.json()["status"] == "open", flag
        version = Assignments(store).task(handoff["id"]).version
        assert client.patch(f"/v1/agent/tasks/{handoff['id']}", headers=token,
                            json={"title": "x", "version": version}).status_code == 403, flag
        assert client.post(f"/v1/agent/tasks/{handoff['id']}/drop", headers=token,
                           json={"note": "n"}).status_code == 403, flag
        assert client.get("/v1/agent/routines?bot=Ops", headers=token).status_code == 403, flag
        assert client.post("/v1/agent/routines", headers=token,
                           json={"cron": "0 9 * * *", "prompt": "p", "for_bot": "Ops"}).status_code == 403, flag
        assert client.patch(f"/v1/agent/routines/{routine['id']}", headers=token,
                            json={"enabled": False}).status_code == 403, flag
        assert client.delete(f"/v1/agent/routines/{routine['id']}", headers=token).status_code == 403, flag
        client.patch(f"/v1/bots/{eng.id}", headers=H, json=undo)
        client.patch(f"/v1/bots/{ops.id}", headers=H, json=undo)
    client.__exit__(None, None, None)


def test_admin_assigns_and_starts_work_and_manages_bot_made_tasks(settings):
    client, store, (lead, eng, ops) = setup(settings)
    _, token = running_token(store, lead)
    started = client.post("/v1/agent/tasks", headers=token, json={"title": "build twin", "for_bot": "Eng", "start": True})
    assert started.status_code == 201 and started.json()["status"] != "open"
    # a task Ops made for Eng: the admin edits and drops it (drop needs a note)
    _, ops_token = running_token(store, ops)
    theirs = client.post("/v1/agent/tasks", headers=ops_token, json={"title": "ops for eng", "for_bot": "Eng"}).json()
    edited = client.patch(f"/v1/agent/tasks/{theirs['id']}", headers=token,
                          json={"title": "renamed", "version": theirs["version"]})
    assert edited.status_code == 200 and edited.json()["title"] == "renamed"
    assert client.post(f"/v1/agent/tasks/{theirs['id']}/drop", headers=token, json={"note": ""}).status_code == 400
    dropped = client.post(f"/v1/agent/tasks/{theirs['id']}/drop", headers=token, json={"note": "duplicate"})
    assert dropped.status_code == 200 and dropped.json()["status"] == "dropped"
    # the owner's task on Eng's list stays the owner's
    owners = Assignments(store).create(eng.id, {"title": "owner's"})
    assert client.patch(f"/v1/agent/tasks/{owners.id}", headers=token,
                        json={"title": "x", "version": Assignments(store).task(owners.id).version}).status_code == 403
    assert client.post(f"/v1/agent/tasks/{owners.id}/drop", headers=token, json={"note": "n"}).status_code == 403
    # a non-admin's task for another bot still lands open
    plain = client.post("/v1/agent/tasks", headers=ops_token, json={"title": "t", "for_bot": "Eng", "start": True})
    assert plain.json()["status"] == "open"
    client.__exit__(None, None, None)


def test_admin_manages_other_bots_bot_made_routines_not_the_owners(settings):
    client, store, (lead, eng, _) = setup(settings)
    _, token = running_token(store, lead)
    made = client.post("/v1/agent/routines", headers=token,
                       json={"cron": "0 9 * * *", "prompt": "daily check", "for_bot": "Eng"})
    assert made.status_code == 201 and made.json()["bot_id"] == str(eng.id) and made.json()["created_by"] == "bot"
    listed = client.get("/v1/agent/routines?bot=Eng", headers=token).json()
    assert [r["id"] for r in listed] == [made.json()["id"]]
    assert client.patch(f"/v1/agent/routines/{made.json()['id']}", headers=token, json={"enabled": False}).json()["enabled"] is False
    assert client.delete(f"/v1/agent/routines/{made.json()['id']}", headers=token).status_code == 204
    owners = store.create_routine(eng.id, "0 8 * * *", "owner's", True)
    assert client.get("/v1/agent/routines?bot=Eng", headers=token).json() == []  # bot-made routines only
    assert client.patch(f"/v1/agent/routines/{owners.id}", headers=token, json={"enabled": False}).status_code == 403
    assert client.delete(f"/v1/agent/routines/{owners.id}", headers=token).status_code == 403
    client.__exit__(None, None, None)


def test_admin_reads_every_non_private_bot(settings):
    client, store, (lead, eng, ops) = setup(settings)
    client.patch(f"/v1/bots/{lead.id}", headers=H, json={"team_read": "none"})
    client.patch(f"/v1/bots/{ops.id}", headers=H, json={"team_private": True})
    _, token = running_token(store, lead)
    data = client.get("/v1/agent/team", headers=token).json()
    readable = {b["name"]: b["readable"] for b in data["bots"]}
    assert data["app_admin"] is True and readable == {"Eng": True, "Ops": False}
    # another admin or a sudo bot falls back to the ordinary team_read rule (here: none)
    for flag in ({"app_admin": True}, {"allow_sudo": True}):
        client.patch(f"/v1/bots/{eng.id}", headers=H, json=flag)
        assert {b["name"]: b["readable"] for b in client.get("/v1/agent/team", headers=token).json()["bots"]}["Eng"] is False
        client.patch(f"/v1/bots/{eng.id}", headers=H, json={key: False for key in flag})
    client.__exit__(None, None, None)


def test_unknown_for_bot_says_inbox_senders_are_not_bots(settings):
    client, store, (lead, *_) = setup(settings, admin=False)
    _, token = running_token(store, lead)
    response = client.post("/v1/agent/tasks", headers=token, json={"title": "t", "for_bot": "Night Runner"})
    assert response.status_code == 404
    assert "not a bot" in response.json()["detail"] and "desk team list" in response.json()["detail"]
    client.__exit__(None, None, None)


def test_self_md_names_admin_only_for_admins(settings):
    client, store, (lead, eng, _) = setup(settings)
    job, _ = running_token(store, lead)
    assert "App admin (set by the owner)" in build_self(store.get_bot(lead.id), job)
    assert "App admin (set by the owner)" not in build_self(store.get_bot(eng.id), job)
    client.__exit__(None, None, None)
