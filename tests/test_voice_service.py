"""The real worker process protocol with the worker's built-in fake engine (no models),
and availability probing in a genuinely separate venv."""

import asyncio
import hashlib
import json
import os
import resource
import subprocess
import sys
import venv
from dataclasses import replace

import pytest

from botoffice.voice import VoiceService
from botoffice.voice.engine import VoiceBusy, VoiceError, VoiceFailed, VoiceTimeout

SECOND = b"\1\0" * 16000


@pytest.fixture
def fake_settings(settings):
    return replace(settings, voice_python=sys.executable)


def test_lazy_start_and_transcribe(fake_settings):
    async def scenario():
        service = VoiceService(fake_settings, fake=True)
        assert not service.stt.loaded and service.stt.starts == 0
        result = await service.transcribe(SECOND)
        assert result["text"] == "fake transcript of 16000 samples"
        assert result["duration_s"] == 1.0 and result["model"] == "small.en"
        assert service.stt.loaded and service.stt.starts == 1
        await service.transcribe(SECOND)
        assert service.stt.starts == 1  # reused
        await service.close()
        assert not service.stt.loaded

    asyncio.run(scenario())


def test_stuck_worker_is_killed_and_recovers(fake_settings):
    async def scenario():
        service = VoiceService(fake_settings, fake=True)
        service.stt.timeout = 1.0
        await service.transcribe(SECOND)
        pid = service.stt.proc.pid
        with pytest.raises(VoiceTimeout):
            await service.transcribe(b"HANG" + SECOND)
        assert service.stt.proc is None
        with pytest.raises(ProcessLookupError):
            import os
            os.kill(pid, 0)
        result = await service.transcribe(SECOND)  # a fresh worker
        assert result["text"].startswith("fake transcript") and service.stt.starts == 2
        await service.close()

    asyncio.run(scenario())


def test_crashed_worker_fails_cleanly_and_restarts(fake_settings):
    async def scenario():
        service = VoiceService(fake_settings, fake=True)
        with pytest.raises(VoiceFailed):
            await service.transcribe(b"CRASH" + SECOND)
        assert (await service.transcribe(SECOND))["text"]
        await service.close()

    asyncio.run(scenario())


def test_queue_is_bounded_and_waiters_survive_a_timeout(fake_settings):
    async def scenario():
        service = VoiceService(fake_settings, fake=True)
        service.stt.timeout = 1.0
        stuck = asyncio.create_task(service.transcribe(b"HANG" + SECOND))
        await asyncio.sleep(0.2)
        waiters = [asyncio.create_task(service.transcribe(SECOND)) for _ in range(2)]
        await asyncio.sleep(0.05)
        with pytest.raises(VoiceBusy):
            await service.transcribe(SECOND)
        with pytest.raises(VoiceTimeout):
            await stuck
        results = await asyncio.gather(*waiters)
        assert all(r["text"].startswith("fake transcript") for r in results)
        await service.close()

    asyncio.run(scenario())


def test_idle_reaper_never_kills_live_work(fake_settings):
    now = [1000.0]

    async def scenario():
        service = VoiceService(fake_settings, fake=True, clock=lambda: now[0])
        assert not await service.stt.reap_if_idle(600)  # nothing loaded
        await service.transcribe(SECOND)
        now[0] += 599
        assert not await service.stt.reap_if_idle(600)
        now[0] += 2
        service.stt._inflight = 1  # a queued request
        assert not await service.stt.reap_if_idle(600)
        service.stt._inflight = 0
        assert await service.stt.reap_if_idle(600)
        assert not service.stt.loaded
        assert (await service.transcribe(SECOND))["text"]
        await service.close()

    asyncio.run(scenario())


def test_worker_gets_no_controller_secrets(fake_settings, monkeypatch):
    from botoffice.voice.engine import worker_env

    monkeypatch.setenv("BOT_OFFICE_TOKEN", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    env = worker_env()
    assert "BOT_OFFICE_TOKEN" not in env and "OPENAI_API_KEY" not in env
    assert env["HF_HUB_OFFLINE"] == "1"


def test_unavailable_without_install_or_when_off(settings):
    async def scenario():
        status = await VoiceService(settings).status()
        assert status["stt"]["available"] is False and "install_voice.sh" in status["stt"]["reason"]
        off = await VoiceService(replace(settings, voice_mode="off", voice_python=sys.executable), fake=True).status()
        assert off["stt"]["available"] is False and "off" in off["stt"]["reason"]

    asyncio.run(scenario())


def test_probe_runs_in_the_separate_voice_venv(settings, tmp_path):
    """Availability is decided by the voice venv's interpreter, not the controller's."""
    target = tmp_path / "voice-venv"
    venv.EnvBuilder(with_pip=False, system_site_packages=False).create(target)
    python = target / "bin" / "python"
    model = settings.voice_dir / "whisper" / "small.en"
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"x")
    voice_settings = replace(settings, voice_python=python)

    async def status():
        service = VoiceService(voice_settings)
        return (await service.status())["stt"]

    first = asyncio.run(status())
    assert first["available"] is False and "missing" in first["reason"]
    site = next((target / "lib").glob("python3*/site-packages"))
    for name in ("numpy", "faster_whisper"):
        (site / name).mkdir()
        (site / name / "__init__.py").write_text("raise RuntimeError('a probe must not import engines')\n")
    assert asyncio.run(status())["available"] is True
    # the controller interpreter itself cannot import the engine
    check = subprocess.run([sys.executable, "-c", "import faster_whisper"], capture_output=True, check=False)
    assert check.returncode != 0


def test_speak_returns_wav_and_caches(fake_settings):
    async def scenario():
        service = VoiceService(fake_settings, fake=True)
        first = await service.speak("Hello there.", "bm_george", 1.0)
        assert first[:4] == b"RIFF" and first[8:12] == b"WAVE" and service.tts.starts == 1
        await service.tts.stop()
        assert await service.speak("Hello there.", "bm_george", 1.0) == first
        assert service.tts.starts == 1  # served from the cache, no worker start
        with pytest.raises(VoiceError):
            await service.speak("Hello", "../../etc/passwd", 1.0)
        await service.close()

    asyncio.run(scenario())


def test_speak_timeout_kills_the_tts_worker(fake_settings):
    async def scenario():
        service = VoiceService(fake_settings, fake=True)
        service.tts.timeout = 1.0
        with pytest.raises(VoiceTimeout):
            await service.speak("HANG", "bm_george", 1.0)
        assert service.tts.proc is None and not service.stt.loaded
        assert (await service.speak("ok", "bm_george", 1.0))[:4] == b"RIFF"
        await service.close()

    asyncio.run(scenario())


def test_tts_availability_reads_voice_keys_from_the_archive(settings, tmp_path):
    import zipfile

    kokoro = settings.voice_dir / "kokoro"
    kokoro.mkdir(parents=True)
    service = VoiceService(replace(settings, voice_python=sys.executable))
    service._probe = (service.clock(), {"stt": {"importable": True}, "tts": {"importable": True}})
    status = asyncio.run(service.tts_status())
    assert status["available"] is False and "install_voice.sh" in status["reason"]
    (kokoro / "kokoro-v1.0.onnx").write_bytes(b"x")
    with zipfile.ZipFile(kokoro / "voices-v1.0.bin", "w") as archive:
        archive.writestr("bm_george.npy", b"x")
        archive.writestr("af_heart.npy", b"x")
    # not parsed in the controller until it matches the sha256 the installer recorded
    assert asyncio.run(service.tts_status())["available"] is False
    digest = hashlib.sha256((kokoro / "voices-v1.0.bin").read_bytes()).hexdigest()
    manifest = settings.voice_dir / "MANIFEST.json"
    manifest.write_text(json.dumps({"files": {"kokoro/voices-v1.0.bin": {"sha256": "0" * 64}}}))
    assert asyncio.run(service.tts_status())["available"] is False
    manifest.write_text(json.dumps({"files": {"kokoro/voices-v1.0.bin": {"sha256": digest}}}))
    service._voice_keys = None
    assert asyncio.run(service.tts_status())["available"] is True
    installed = {v["id"]: v["installed"] for v in service.voices()}
    assert installed["bm_george"] and installed["af_heart"] and not installed["bm_lewis"]
    int8 = VoiceService(replace(settings, voice_python=sys.executable, tts_model="int8"))
    int8._probe = service._probe
    assert asyncio.run(int8.tts_status())["available"] is False  # the int8 file is absent


def test_cancellation_mid_request_never_reuses_the_worker(fake_settings):
    """A request cancelled after its frame was written must not leave its reply in the pipe."""
    async def scenario():
        service = VoiceService(fake_settings, fake=True)
        await service.transcribe(SECOND)
        first = service.stt.proc
        task = asyncio.create_task(service.transcribe(b"HANG" + SECOND))
        await asyncio.sleep(0.3)  # written, waiting for the reply
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.stt.proc is None and first.returncode is not None
        result = await service.transcribe(b"\1\0" * 8000)
        assert result["text"] == "fake transcript of 8000 samples"  # its own reply, not a stale one
        # cancelled while the model loads: the half-started worker is stopped as well
        await service.stt.stop()
        task = asyncio.create_task(service.transcribe(SECOND))
        while service.stt.proc is None:
            await asyncio.sleep(0)
        loading = service.stt.proc
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.stt.proc is None and loading.returncode is not None
        assert (await service.transcribe(SECOND))["text"] == "fake transcript of 16000 samples"
        await service.close()

    asyncio.run(scenario())


def test_worker_runs_with_resource_limits_and_a_scratch_home(fake_settings):
    async def scenario():
        service = VoiceService(fake_settings, fake=True)
        await service.transcribe(SECOND)
        pid = service.stt.proc.pid
        limits = (Path(f"/proc/{pid}/limits")).read_text()
        environ = dict(item.split(b"=", 1) for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0") if b"=" in item)
        await service.close()
        return limits, environ

    from pathlib import Path

    limits, environ = asyncio.run(scenario())
    core = next(line for line in limits.splitlines() if line.startswith("Max core file size"))
    assert core.split()[4:6] == ["0", "0"]
    space = next(line for line in limits.splitlines() if line.startswith("Max address space"))
    assert space.split()[3:5] == [str(8 * 1024**3)] * 2
    nproc = next(line for line in limits.splitlines() if line.startswith("Max processes"))
    assert nproc.split()[2] != "unlimited" or resource.getrlimit(resource.RLIMIT_NPROC)[1] == resource.RLIM_INFINITY
    assert environ[b"HOME"] == str(fake_settings.voice_dir / "home").encode()
    assert (fake_settings.voice_dir / "home").stat().st_mode & 0o777 == 0o700


def test_fake_engine_needs_the_test_flag(fake_settings, monkeypatch):
    monkeypatch.delenv("BOT_OFFICE_VOICE_FAKE")
    with pytest.raises(RuntimeError):
        VoiceService(fake_settings, fake=True)
    # the worker itself refuses --fake without the flag, too
    from botoffice.voice import WORKER

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    out = subprocess.run([sys.executable, "-I", str(WORKER), "--engine", "stt", "--fake"], input=b"",
                         capture_output=True, env=env, timeout=30, check=False)
    assert out.returncode == 2 and b"tests only" in out.stdout
