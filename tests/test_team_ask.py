import threading
import time
from contextlib import closing
from dataclasses import replace

import pytest

from botoffice import team
from botoffice.providers.fake import FakeAdapter
from teamkit import MASTER, Desk, run_job, set_bot


def waits(d):
    with closing(d.store._connect()) as db:
        return db.execute("SELECT COUNT(*) FROM team_ask_waits").fetchone()[0]


def sent(d):
    with closing(d.store._connect()) as db:
        return db.execute("SELECT COUNT(*) FROM agent_messages").fetchone()[0]


def ask_in_thread(d, headers, to, timeout_s=5, text="q"):
    out = {}

    def run():
        start = time.monotonic()
        out["response"] = d.client.post("/v1/agent/team/ask", headers=headers,
                                        json={"to": to, "body": text, "timeout_s": timeout_s})
        out["seconds"] = time.monotonic() - start
    thread = threading.Thread(target=run)
    thread.start()
    return thread, out


def wait_until(predicate, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_steered_ask_returns_the_targets_answer_in_turn(settings):
    with Desk(settings) as d:
        asker, target = d.bot("Asker"), d.bot("Target")
        _, ha = run_job(d.store, asker)
        _, ht = run_job(d.store, target)
        thread, out = ask_in_thread(d, ha, "Target", timeout_s=30, text="reply only OK")
        assert wait_until(lambda: sent(d) == 1 and waits(d) == 1)  # delivered and holding one wait slot
        answer = d.client.post(f"/v1/bots/{target.id}/message-agent", headers=ht, json={"to": "Asker", "body": "OK"})
        assert answer.status_code == 200
        thread.join(15)
        body = out["response"].json()
        assert out["response"].status_code == 200 and body["status"] == "answered" and body["reply"] == "OK"
        assert body["delivery"] == "steer" and body["untrusted"] is True and out["seconds"] < 10
        assert waits(d) == 0


def test_timeout_returns_pending_with_waiting_on_bot_note(settings):
    with Desk(settings) as d:
        asker, target = d.bot("Asker"), d.bot("Target")
        _, ha = run_job(d.store, asker)
        run_job(d.store, target)
        thread, out = ask_in_thread(d, ha, "Target", timeout_s=5)
        thread.join(20)
        body = out["response"].json()
        assert body["status"] == "pending" and "waiting_on_bot" in body["note"] and out["seconds"] >= 4.5
        assert waits(d) == 0 and sent(d) == 1  # the message was still sent; the reply wakes the asker later


def test_never_waits_with_a_single_lane_but_still_sends(settings):
    with Desk(replace(settings, run_lanes=1)) as d:
        asker, target = d.bot("Asker"), d.bot("Target")
        _, ha = run_job(d.store, asker)
        run_job(d.store, target)
        start = time.monotonic()
        body = d.client.post("/v1/agent/team/ask", headers=ha, json={"to": "Target", "body": "q", "timeout_s": 60}).json()
        assert body["status"] == "pending" and time.monotonic() - start < 3 and sent(d) == 1


def test_wait_slots_are_lanes_minus_one_and_a_waiting_bot_is_not_asked_to_wait(settings):
    with Desk(settings) as d:  # 3 lanes
        a1, a2, a3, t = d.bot("A1"), d.bot("A2"), d.bot("A3"), d.bot("T")
        j1, h1 = run_job(d.store, a1)
        j2, h2 = run_job(d.store, a2)
        j3, h3 = run_job(d.store, a3)
        run_job(d.store, t)
        assert team.reserve_wait(d.store, j1, t.id, 3, 30) and team.reserve_wait(d.store, j2, d.bot("T2").id, 3, 30)
        # two waiters already: the third asker is answered at once, not made to wait
        start = time.monotonic()
        body = d.client.post("/v1/agent/team/ask", headers=h3, json={"to": "T", "body": "q", "timeout_s": 60}).json()
        assert body["status"] == "pending" and time.monotonic() - start < 3
        team.release_wait(d.store, j2)
        # a bot that is itself waiting is not worth waiting for (it cannot answer before its own timeout)
        asked_waiter = d.client.post("/v1/agent/team/ask", headers=h3, json={"to": "A1", "body": "q2", "timeout_s": 60}).json()
        assert asked_waiter["status"] == "pending"
        assert waits(d) == 1  # only j1 still holds its slot; every ask released its own


def test_no_lane_deadlock_with_many_askers(settings):
    """Five askers, 3 lanes, nobody answers: at most lanes-1 = 2 ever wait at once, the rest return at once,
    every wait ends at its timeout, and the table is empty afterwards."""
    with Desk(settings) as d:
        askers, targets, threads = [], [], []
        for i in range(3):
            askers.append(run_job(d.store, d.bot(f"A{i}"))[1])
            run_job(d.store, d.bot(f"T{i}"))
        peak, stop = [0], threading.Event()

        def sample():
            while not stop.is_set():
                peak[0] = max(peak[0], waits(d))
                time.sleep(0.02)
        sampler = threading.Thread(target=sample)
        sampler.start()
        for i, headers in enumerate(askers):
            threads.append(ask_in_thread(d, headers, f"T{i}", timeout_s=5))
        for thread, _ in threads:
            thread.join(30)
        stop.set()
        sampler.join()
        results = [out for _, out in threads]
        assert all(out["response"].json()["status"] == "pending" for out in results)
        quick = [out for out in results if out["seconds"] < 3]
        assert len(quick) == 1 and peak[0] == 2 and waits(d) == 0


class Replier(FakeAdapter):
    async def run(self, job, workspace, log_path, timeout):
        (workspace / "reply.txt").write_text("PONG")
        return await super().run(job, workspace, log_path, timeout)


def test_queued_turn_ask_is_answered_in_turn_with_the_fake_provider(settings):
    with Desk(settings, Replier(), lifespan=True) as d:
        a1, a2, a3 = d.bot("A1"), d.bot("A2"), d.bot("A3")
        for name in ("T1", "T2", "T3"):
            d.bot(name)
        h = [run_job(d.store, b)[1] for b in (a1, a2)]
        # an idle target starts its own turn: the answer is that turn's reply, delivered in this turn. The
        # lane guard is conservative: it only waits while running + queued-ahead jobs leave a lane free
        for asker_headers, name in ((h[0], "T1"), (h[1], "T2")):
            thread, out = ask_in_thread(d, asker_headers, name, timeout_s=30)
            thread.join(30)
            body = out["response"].json()
            assert body["status"] == "answered" and body["reply"] == "PONG" and body["delivery"] == "start", body
        # two askers still run, and a third arrives: all 3 lanes' worth is spoken for once its target queues
        h.append(run_job(d.store, a3)[1])
        thread, out = ask_in_thread(d, h[2], "T3", timeout_s=30)
        thread.join(30)
        assert out["response"].json()["status"] == "pending" and out["seconds"] < 5
        # the two answered in the turn do not wake their askers again
        with closing(d.store._connect()) as db:
            assert db.execute("SELECT COUNT(*) FROM agent_message_origin WHERE woke_at IS NULL").fetchone()[0] == 1  # only the pending third ask
        assert waits(d) == 0


def test_busy_target_that_cannot_be_steered_is_never_waited_on(settings):
    with Desk(settings) as d:
        asker, target = d.bot("Asker"), d.bot("Target")
        set_bot(d.store, target, bot_steer=0)
        _, ha = run_job(d.store, asker)
        run_job(d.store, target)
        start = time.monotonic()
        body = d.client.post("/v1/agent/team/ask", headers=ha, json={"to": "Target", "body": "q", "timeout_s": 60}).json()
        assert body["status"] == "pending" and body["delivery"] == "queue" and time.monotonic() - start < 3


def test_limits_still_apply_and_slot_is_released_on_error(settings):
    with Desk(settings) as d:
        asker, target = d.bot("Asker"), d.bot("Target")
        _, ha = run_job(d.store, asker)
        run_job(d.store, target)
        for bad in (4, 181, 0):
            assert d.client.post("/v1/agent/team/ask", headers=ha, json={"to": "Target", "body": "q", "timeout_s": bad}).status_code == 422
        assert d.client.post("/v1/agent/team/ask", headers=ha, json={"to": "Nobody", "body": "q"}).status_code == 404
        assert d.client.post("/v1/agent/team/ask", headers=ha, json={"to": "Asker", "body": "q"}).status_code == 400
        assert d.client.post("/v1/agent/team/message", headers=ha, json={"to": "Target", "body": "one", "mode": "steer"}).status_code == 200
        again = d.client.post("/v1/agent/team/ask", headers=ha, json={"to": "Target", "body": "two", "timeout_s": 5})
        assert again.status_code == 429  # 10 s per pair, as for bot-msg
        assert waits(d) == 0
        assert d.client.post("/v1/agent/team/message", headers=ha, json={"to": "Target", "body": "x" * 10001}).status_code == 422
        assert d.client.post("/v1/agent/team/message", headers=MASTER, json={"to": "Target", "body": "x"}).status_code == 401


def test_hop_cap_applies_to_team_messages(settings):
    from botoffice.domain import Slot
    with Desk(settings) as d:
        sender, target = d.bot("Sender"), d.bot("Target")
        job = d.store.create("parent", "codex", bot_id=sender.id, source="inbox")
        d.store.create_agent_message(target.id, sender.id, "parent", job.id, "thread", 3)
        assert d.store.claim(Slot.DESKTOP_1, {"codex"}).id == job.id
        h = {"Authorization": "Bearer " + d.store.issue_agent_token(job)}
        response = d.client.post("/v1/agent/team/message", headers=h, json={"to": "Target", "body": "next"})
        assert response.status_code == 429 and "hop" in response.json()["detail"]


def test_wait_ends_when_the_asking_turn_ends(settings):
    with Desk(settings) as d:
        asker, target = d.bot("Asker"), d.bot("Target")
        job, ha = run_job(d.store, asker)
        run_job(d.store, target)
        thread, out = ask_in_thread(d, ha, "Target", timeout_s=60)
        assert wait_until(lambda: waits(d) == 1)
        with closing(d.store._connect()) as db:
            db.execute("UPDATE jobs SET status='failed' WHERE id=?", (str(job.id),))
        thread.join(15)
        assert not thread.is_alive() and out["seconds"] < 10 and waits(d) == 0


def test_stalled_target_is_detected_when_queued_and_every_lane_is_taken(settings):
    from botoffice.domain import Slot
    with Desk(settings) as d:
        lanes = settings.run_lanes
        for i in range(lanes):
            run_job(d.store, d.bot(f"Busy{i}"))
        waiting_bot = d.bot("Waiter")
        queued = d.store.create("q", "codex", bot_id=waiting_bot.id, source="chat")
        with closing(d.store._connect()) as db:
            db.execute("INSERT INTO agent_messages (id, from_bot, to_bot, body, job_id, thread_id, hop, created_at) "
                       "VALUES ('m1','a','b','q',?, 't', 1, '2026-11-21T12:00:00+00:00')", (str(queued.id),))
        assert team.target_stalled(d.store, lanes, "m1") is True
        assert team.lane_free(d.store, lanes, str(queued.id)) is False
        # a lane frees up (claimed in between is the same snapshot): no longer stalled
        with closing(d.store._connect()) as db:
            db.execute("UPDATE jobs SET status='succeeded' WHERE status='running' AND id=(SELECT id FROM jobs WHERE status='running' LIMIT 1)")
        assert team.target_stalled(d.store, lanes, "m1") is False
        assert team.lane_free(d.store, lanes, str(queued.id)) is True
        assert team.target_stalled(d.store, lanes, "nope") is False  # a steered message has no job of its own


def test_ask_gives_up_early_when_its_queued_target_cannot_get_a_lane(settings, monkeypatch):
    with Desk(settings, Replier(), lifespan=False) as d:
        asker = d.bot("Asker")
        d.bot("Tgt")
        _, h = run_job(d.store, asker)
        monkeypatch.setattr(team, "lane_free", lambda *a: True)  # the pre-send check was fooled (claimed in between)
        monkeypatch.setattr(team, "target_stalled", lambda *a: True)  # ...and the recheck sees a queued target, no lane
        thread, out = ask_in_thread(d, h, "Tgt", timeout_s=30)
        thread.join(15)
        body = out["response"].json()
        assert body["status"] == "pending" and out["seconds"] < 5, body
        assert waits(d) == 0
