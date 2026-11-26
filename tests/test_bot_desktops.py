import asyncio
import json
import uuid

import pytest

from botoffice import bot_desktops as bd
from botoffice.config import Settings

BOT_A = str(uuid.UUID(int=1))
BOT_B = str(uuid.UUID(int=2))
BOT_C = str(uuid.UUID(int=3))


class FakePodman:
    """Just enough podman: ps -a json, run, start, stop."""

    def __init__(self):
        self.containers = {}   # name -> {"running": bool, "labels": {}, "ports": [...]}
        self.calls = []
        self.fail = set()

    def add(self, name, running=True, labels=None, host_ip="127.0.0.1"):
        self.serial = getattr(self, "serial", 0) + 1
        self.containers[name] = {"running": running, "labels": labels or {}, "id": f"c{self.serial:04d}{name[8:]}",
                                 "ports": [{"host_ip": host_ip, "host_port": 5900, "container_port": 5900}]}

    async def __call__(self, argv, timeout):
        self.calls.append(argv)
        verb = argv[0]
        if verb in self.fail:
            return 1, b"", b"boom"
        if verb == "ps":
            out = [{"Names": [name], "Id": c["id"], "State": "running" if c["running"] else "exited",
                    "Labels": c["labels"], "Ports": c["ports"]} for name, c in self.containers.items()]
            return 0, json.dumps(out).encode(), b""
        if verb == "run":
            name = argv[argv.index("--name") + 1]
            labels = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "--label")
            publish = argv[argv.index("--publish") + 1]
            self.add(name, True, labels, publish.split(":")[0])
            return 0, self.containers[name]["id"].encode() + b"\n", b""
        if verb in ("start", "stop"):
            found = next((c for c in self.containers.values() if c["id"] == argv[-1]), None)
            if found is None:
                return 125, b"", b"no such container"
            found["running"] = verb == "start"
            return 0, b"", b""
        raise AssertionError(argv)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def _ready(port):
    return True


def make(tmp_path, busy=(), **over):
    settings = Settings(data_dir=tmp_path / "data", token="t", **over)
    podman = FakePodman()
    clock = Clock()
    busy_set = set(busy)
    manager = bd.DesktopManager(settings, busy_bots=lambda: set(busy_set), runner=podman, clock=clock,
                                ready=_ready, registry_path=tmp_path / "desktops.json")
    return manager, podman, clock, busy_set


def run(coro):
    return asyncio.run(coro)


def test_first_use_creates_a_locked_down_desktop(tmp_path):
    manager, podman, *_ = make(tmp_path)
    name = run(manager.ensure(BOT_A))
    assert name == "desktop-10"
    argv = next(a for a in podman.calls if a[0] == "run")
    joined = " ".join(argv)
    for flag in ("--cap-drop ALL", "--security-opt no-new-privileges", "--memory 1536m", "--cpus 1.5",
                 "--pids-limit 1024", "--publish 127.0.0.1:5910:5900", "--restart no", "--init",
                 f"--volume bo-desktop-home-{BOT_A}:/root", f"--label bot-office.bot-id={BOT_A}",
                 "--label bot-office.kind=per-bot", "localhost/light-desktop:v1"):
        assert flag in joined, flag
    assert "--privileged" not in argv and "--network" not in argv and "host" not in argv
    assert "-nolisten tcp" in argv[-1]


def test_mapping_is_stable_and_persisted(tmp_path):
    manager, podman, *_ = make(tmp_path)
    assert run(manager.ensure(BOT_A)) == "desktop-10"
    assert run(manager.ensure(BOT_B)) == "desktop-11"
    assert run(manager.ensure(BOT_A)) == "desktop-10"
    assert sum(1 for a in podman.calls if a[0] == "run") == 2
    again = bd.Registry(tmp_path / "desktops.json")
    assert again.number(BOT_A) == 10 and again.number(BOT_B) == 11
    assert again.bot("desktop-11") == BOT_B and again.bot("desktop-1") is None
    assert oct((tmp_path / "desktops.json").stat().st_mode & 0o777) == "0o600"


def test_stopped_desktop_is_started_not_recreated(tmp_path):
    manager, podman, *_ = make(tmp_path)
    run(manager.ensure(BOT_A))
    podman.containers["desktop-10"]["running"] = False
    assert run(manager.ensure(BOT_A)) == "desktop-10"
    assert podman.calls[-1] == ["start", podman.containers["desktop-10"]["id"]]
    assert sum(1 for a in podman.calls if a[0] == "run") == 1


def test_foreign_containers_are_skipped_and_never_touched(tmp_path):
    manager, podman, *_ = make(tmp_path)
    podman.add("desktop-10")                                   # hand-made, no labels
    podman.add("desktop-11", labels={bd.LABEL_KIND: "per-bot", bd.LABEL_BOT: BOT_B})  # another bot's
    assert run(manager.ensure(BOT_A)) == "desktop-12"
    foreign = {podman.containers[n]["id"] for n in ("desktop-10", "desktop-11")}
    assert not any(a[0] in ("stop", "start") and a[-1] in foreign for a in podman.calls)


def test_container_with_public_publish_is_refused(tmp_path):
    manager, podman, *_ = make(tmp_path)
    run(manager.ensure(BOT_A))
    podman.containers["desktop-10"]["ports"][0]["host_ip"] = "0.0.0.0"
    with pytest.raises(bd.DesktopError, match="127.0.0.1"):
        run(manager.ensure(BOT_A))


def test_range_exhausted(tmp_path):
    manager, podman, *_ = make(tmp_path)
    for n in range(bd.FIRST, bd.LAST + 1):
        podman.add(f"desktop-{n}", running=False)
    with pytest.raises(bd.DesktopError, match="no free desktop number"):
        run(manager.ensure(BOT_A))


def test_pool_mode_refuses(tmp_path):
    manager, *_ = make(tmp_path, desktop_mode="pool")
    with pytest.raises(bd.DesktopError, match="off"):
        run(manager.ensure(BOT_A))


def test_cap_stops_the_least_recently_used_idle_desktop(tmp_path):
    manager, podman, clock, busy = make(tmp_path, desktop_max_running=2)
    run(manager.ensure(BOT_A))
    clock.t += 10
    run(manager.ensure(BOT_B))
    clock.t += bd.RESERVE_SECONDS + 1       # past the just-started reservation of both
    assert run(manager.ensure(BOT_C)) == "desktop-12"
    assert ["stop", "--time", "10", podman.containers["desktop-10"]["id"]] in podman.calls     # A was used least recently
    assert podman.containers["desktop-11"]["running"]


def test_cap_never_stops_a_desktop_in_use(tmp_path):
    manager, podman, clock, busy = make(tmp_path, desktop_max_running=2)
    run(manager.ensure(BOT_A))
    run(manager.ensure(BOT_B))
    clock.t += bd.RESERVE_SECONDS + 1
    busy.add(BOT_A)                                   # A's bot has a running turn
    with manager.viewing("desktop-11", "view"):       # the owner watches B
        with pytest.raises(bd.DesktopError, match="all 2 desktops are in use"):
            run(manager.ensure(BOT_C))
    assert not any(a[0] == "stop" for a in podman.calls)


def test_idle_reaper(tmp_path):
    manager, podman, clock, busy = make(tmp_path, desktop_idle_minutes=20)
    run(manager.ensure(BOT_A))
    run(manager.ensure(BOT_B))
    run(manager.ensure(BOT_C))
    clock.t += 19 * 60
    assert run(manager.reap_idle()) == []
    clock.t += 2 * 60
    busy.add(BOT_A)                       # a running turn keeps A
    with manager.viewing("desktop-11", "view"):   # a viewer keeps B
        assert run(manager.reap_idle()) == ["desktop-12"]
    assert not podman.containers["desktop-12"]["running"]
    clock.t += 21 * 60
    busy.clear()
    # B's viewer left at the old time + touch on exit; A is now idle long enough too
    assert run(manager.reap_idle()) == ["desktop-10", "desktop-11"]
    # stopped desktops start again with their state on the next use
    assert run(manager.ensure(BOT_C)) == "desktop-12" and podman.calls[-1] == ["start", podman.containers["desktop-12"]["id"]]


def test_reaper_grace_after_restart_and_disabled(tmp_path):
    manager, podman, clock, _ = make(tmp_path)
    podman.add("desktop-10", labels={bd.LABEL_KIND: "per-bot", bd.LABEL_BOT: BOT_A})
    manager.registry.allocate(BOT_A, {})
    # a fresh controller does not know when desktop-10 was last used: it counts from now
    assert run(manager.reap_idle()) == []
    clock.t += 20 * 60
    assert run(manager.reap_idle()) == ["desktop-10"]
    manager, podman, clock, _ = make(tmp_path / "x", desktop_idle_minutes=0)
    run(manager.ensure(BOT_A))
    clock.t += 10 ** 6
    assert run(manager.reap_idle()) == []


def test_reaper_never_stops_pool_or_foreign_desktops(tmp_path):
    manager, podman, clock, _ = make(tmp_path)
    podman.add("desktop-1")
    podman.add("desktop-13", labels={bd.LABEL_KIND: "per-bot", bd.LABEL_BOT: BOT_B})  # labelled, not ours
    clock.t += 10 ** 6
    assert run(manager.reap_idle()) == []
    assert not any(a[0] == "stop" for a in podman.calls)


def test_controlling_flag(tmp_path):
    manager, *_ = make(tmp_path)
    assert not manager.owner_controlling("desktop-10")
    with manager.viewing("desktop-10", "control"):
        assert manager.owner_controlling("desktop-10")
    with manager.viewing("desktop-10", "view"):
        assert not manager.owner_controlling("desktop-10")
    assert manager.viewers["desktop-10"] == 0


def test_names_and_volume():
    assert bd.number_of("desktop-10") == 10 and bd.number_of("desktop-99") == 99
    for name in ("desktop-1", "desktop-9", "desktop-100", "desktop-010", "desk-10", "desktop-1x"):
        assert bd.number_of(name) is None
    with pytest.raises(ValueError):
        bd.volume_name("../etc")
    with pytest.raises(ValueError):
        bd.run_argv("desktop-1", BOT_A, Settings(token="t"))


def test_corrupt_registry_starts_empty(tmp_path):
    path = tmp_path / "desktops.json"
    path.write_text("{not json")
    assert bd.Registry(path).items() == []
    path.write_text(json.dumps({"bots": {BOT_A: 10, BOT_B: 10, "nope": 11, BOT_C: 5}}))
    assert bd.Registry(path).items() == [(BOT_A, 10)]


# ---------------------------------------------------------------- review round 1
def test_failed_eviction_never_exceeds_the_cap(tmp_path):
    manager, podman, clock, _ = make(tmp_path, desktop_max_running=1)
    run(manager.ensure(BOT_A))
    clock.t += bd.RESERVE_SECONDS + 1
    podman.fail.add("stop")
    with pytest.raises(bd.DesktopError, match="could not stop desktop-10"):
        run(manager.ensure(BOT_B))
    assert sum(1 for a in podman.calls if a[0] == "run") == 1


def test_unmanaged_labelled_desktops_count_toward_the_cap(tmp_path):
    manager, podman, clock, _ = make(tmp_path, desktop_max_running=1)
    podman.add("desktop-20", labels={bd.LABEL_KIND: "per-bot", bd.LABEL_BOT: "not-a-bot-id"})  # not ours to manage
    with pytest.raises(bd.DesktopError, match="in use"):
        run(manager.ensure(BOT_A))
    assert not any(a[0] == "stop" for a in podman.calls)


def test_a_just_started_desktop_is_not_evicted(tmp_path):
    manager, podman, clock, _ = make(tmp_path, desktop_max_running=1)
    run(manager.ensure(BOT_A))
    with pytest.raises(bd.DesktopError, match="in use"):
        run(manager.ensure(BOT_B))        # A was started seconds ago for someone about to connect
    clock.t += bd.RESERVE_SECONDS + 1
    assert run(manager.ensure(BOT_B)) == "desktop-11"


def test_no_viewer_is_admitted_while_stopping(tmp_path):
    manager, podman, clock, _ = make(tmp_path)
    run(manager.ensure(BOT_A))
    entered = []

    async def slow_stop(argv, timeout):
        if argv[0] == "stop":
            try:
                with manager.viewing("desktop-10", "view"):
                    entered.append(True)
            except bd.DesktopError:
                entered.append(False)
        return await FakePodman.__call__(podman, argv, timeout)
    manager.runner = slow_stop
    assert run(manager.stop("desktop-10", "test"))
    assert entered == [False] and "desktop-10" not in manager.stopping
    with manager.viewing("desktop-10", "view"):
        pass


def test_lost_registry_is_rebuilt_from_labels(tmp_path):
    manager, podman, *_ = make(tmp_path)
    podman.add("desktop-11", labels={bd.LABEL_KIND: "per-bot", bd.LABEL_BOT: BOT_A})
    (tmp_path / "desktops.json").write_text("{broken")
    manager.registry = bd.Registry(tmp_path / "desktops.json")
    assert list(tmp_path.glob("desktops.json.corrupt-*"))
    assert run(manager.ensure(BOT_A)) == "desktop-11"          # its existing desktop, not a new desktop-10
    assert not any(a[0] == "run" for a in podman.calls)
    assert bd.Registry(tmp_path / "desktops.json").number(BOT_A) == 11


# ---------------------------------------------------------------- review round 2
def test_labels_override_a_stale_registry_entry(tmp_path):
    manager, podman, *_ = make(tmp_path)
    podman.add("desktop-11", running=False, labels={bd.LABEL_KIND: "per-bot", bd.LABEL_BOT: BOT_A})
    manager.registry._map = {BOT_A: 15, BOT_B: 11}     # damaged: A points at nothing, B at A's container
    assert run(manager.ensure(BOT_A)) == "desktop-11"
    assert podman.calls[-1] == ["start", podman.containers["desktop-11"]["id"]] and not any(a[0] == "run" for a in podman.calls)
    assert manager.registry.number(BOT_B) is None
    assert run(manager.ensure(BOT_B)) == "desktop-10"   # B gets a fresh number, never A's desktop


def test_reaper_rechecks_busy_bots_before_each_stop(tmp_path):
    manager, podman, clock, busy = make(tmp_path)
    run(manager.ensure(BOT_A))
    run(manager.ensure(BOT_B))
    clock.t += 3600
    original = podman.__call__

    async def runner(argv, timeout):
        if argv[0] == "stop" and argv[-1] == podman.containers["desktop-10"]["id"]:
            busy.add(BOT_B)                  # B starts a turn while A's desktop is being stopped
        return await original(argv, timeout)
    manager.runner = runner
    assert run(manager.reap_idle()) == ["desktop-10"]
    assert podman.containers["desktop-11"]["running"]


# ---------------------------------------------------------------- review round 3
def test_mapping_to_a_foreign_container_is_dropped(tmp_path):
    manager, podman, *_ = make(tmp_path)
    manager.registry._map = {BOT_A: 10}       # mapped earlier, then creation failed ...
    podman.add("desktop-10", running=False)   # ... and someone made a desktop-10 by hand
    assert run(manager.ensure(BOT_A)) == "desktop-11"
    assert not any(a[0] in ("start", "stop") and a[-1] == podman.containers["desktop-10"]["id"] for a in podman.calls)


def test_refresh_reconciles_for_listing(tmp_path):
    manager, podman, *_ = make(tmp_path)
    podman.add("desktop-14", running=False, labels={bd.LABEL_KIND: "per-bot", bd.LABEL_BOT: BOT_C})
    assert manager.bot_of("desktop-14") is None
    run(manager.refresh())
    assert manager.bot_of("desktop-14") == BOT_C
    assert run(manager.start("desktop-14")) == "desktop-14"


# ---------------------------------------------------------------- review round 4
def test_a_container_replaced_since_the_listing_is_never_stopped(tmp_path):
    manager, podman, clock, _ = make(tmp_path)
    run(manager.ensure(BOT_A))
    run(manager.containers())                              # listing knows desktop-10 by its id
    podman.add("desktop-10")                               # removed and replaced by a hand-made one meanwhile
    assert not run(manager.stop("desktop-10", "test"))     # the old id is gone: nothing is stopped
    assert podman.containers["desktop-10"]["running"]
    assert not run(manager.stop("desktop-3", "test"))      # never anything outside the managed set


# ---------------------------------------------------------------- review round 6
def test_pool_mode_reaper_leaves_per_bot_desktops_alone(tmp_path):
    manager, podman, clock, _ = make(tmp_path)
    run(manager.ensure(BOT_A))
    manager.settings = Settings(data_dir=tmp_path / "data", token="t", desktop_mode="pool")
    clock.t += 10 ** 6
    assert run(manager.reap_idle()) == []
    assert podman.containers["desktop-10"]["running"]


# ---------------------------------------------------------------- review round 9
def test_reaper_rechecks_idleness_before_each_stop(tmp_path):
    manager, podman, clock, busy = make(tmp_path)
    run(manager.ensure(BOT_A))
    run(manager.ensure(BOT_B))
    clock.t += 3600
    original = podman.__call__

    async def runner(argv, timeout):
        if argv[0] == "stop" and argv[-1] == podman.containers["desktop-10"]["id"]:
            with manager.viewing("desktop-11", "view"):   # someone looks at B and leaves during A's stop
                pass
        return await original(argv, timeout)
    manager.runner = runner
    assert run(manager.reap_idle()) == ["desktop-10"]
    assert podman.containers["desktop-11"]["running"]


def test_cancelled_podman_command_is_ended_and_reaped(tmp_path, monkeypatch):
    import sys
    calls = {}

    def wrap(argv, **kw):
        return [sys.executable, "-c", "import time; time.sleep(30)"]
    monkeypatch.setattr(bd.isolation, "wrap", wrap)

    async def main():
        task = asyncio.ensure_future(bd.podman(["stop", "x"], 60))
        await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            calls["cancelled"] = True
    import time
    start = time.monotonic()
    run(main())
    assert calls["cancelled"] and time.monotonic() - start < 10


# ---------------------------------------------------------------- review round 11
def test_configured_pool_names_are_never_per_bot(tmp_path):
    manager, podman, *_ = make(tmp_path, desktops=("desktop-1", "desktop-10"))
    assert run(manager.ensure(BOT_A)) == "desktop-11"
    podman.add("desktop-10", labels={bd.LABEL_KIND: "per-bot", bd.LABEL_BOT: BOT_B})
    run(manager.refresh())
    assert manager.bot_of("desktop-10") is None
    assert bd.Registry(tmp_path / "x.json", ("desktop-12",)).allocate(BOT_C, {}) == 10


# ---------------------------------------------------------------- live test finding: service restarts
def test_run_and_start_get_their_own_named_scope(monkeypatch):
    monkeypatch.setattr(bd.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(bd.isolation, "active", lambda: None)
    for verb in ("run", "start"):
        assert bd.scope_prefix([verb, "x"], "bo-desktop-u1", True) == [
            "/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect", "--unit=bo-desktop-u1", "--"]
        assert bd.scope_prefix([verb, "x"], "u", False) == []          # probe said no: direct, as before
    for verb in ("stop", "ps", "exec"):
        assert bd.scope_prefix([verb, "x"], "u", True) == []
    monkeypatch.setattr(bd.isolation, "active", lambda: object())
    assert bd.scope_prefix(["start", "x"], "u", True) == []             # isolated layout: unchanged


def test_scope_probe_once_and_warns(monkeypatch, caplog):
    monkeypatch.setattr(bd, "_scope_ok", None)
    monkeypatch.setattr(bd.isolation, "active", lambda: None)
    monkeypatch.setattr(bd.shutil, "which", lambda name: None)
    assert run(bd._scopes_available({"XDG_RUNTIME_DIR": "/run/user/1"})) is False
    assert "without their own systemd scope" in caplog.text
    monkeypatch.setattr(bd.shutil, "which", lambda name: "/bin/true")   # would succeed now, but probed once
    assert run(bd._scopes_available({"XDG_RUNTIME_DIR": "/run/user/1"})) is False


def test_aborted_scoped_start_ends_podman_and_stops_the_container(monkeypatch):
    import sys
    calls = []
    monkeypatch.setattr(bd, "_scope_ok", True)
    monkeypatch.setattr(bd.isolation, "active", lambda: None)
    monkeypatch.setattr(bd, "scope_prefix", lambda argv, unit, ok: [sys.executable, "-c",
                                                                    "import time; time.sleep(30)", "--"] if ok else [])
    real_once = bd._podman_once

    async def once(argv, timeout, scoped):
        if argv[0] == "stop":
            calls.append(argv)
            return 0, b"", b""
        return await real_once(argv, timeout, scoped)
    monkeypatch.setattr(bd, "_podman_once", once)
    code, _, _ = run(bd.podman(["start", "desktop-12"], 0.5))
    assert code == 124 and calls == [["stop", "--time", "5", "desktop-12"]]


def test_scope_failure_falls_back_once(monkeypatch):
    seen = []
    monkeypatch.setattr(bd, "_scope_ok", True)

    async def once(argv, timeout, scoped):
        seen.append(scoped)
        return (1, b"", b"Failed to start transient scope unit: x") if scoped else (0, b"id\n", b"")
    monkeypatch.setattr(bd, "_podman_once", once)
    assert run(bd.podman(["start", "desktop-12"]))[0] == 0
    assert seen == [True, False] and bd._scope_ok is False


def test_double_cancel_waits_for_cleanup(monkeypatch):
    import sys
    monkeypatch.setattr(bd, "_scope_ok", False)
    monkeypatch.setattr(bd.isolation, "active", lambda: None)
    monkeypatch.setattr(bd.isolation, "wrap", lambda argv, **kw: [sys.executable, "-c", "import time; time.sleep(30)"])
    state = {}
    real_once = bd._podman_once

    async def once(argv, timeout, scoped):
        if argv[0] == "stop":
            await asyncio.sleep(0.3)
            state["stopped"] = True
            return 0, b"", b""
        return await real_once(argv, timeout, scoped)
    monkeypatch.setattr(bd, "_podman_once", once)

    async def main():
        task = asyncio.ensure_future(bd.podman(["start", "desktop-12"], 60))
        await asyncio.sleep(0.5)
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            state["cancelled_after_stop"] = state.get("stopped", False)
    run(main())
    assert state["cancelled_after_stop"] is True
