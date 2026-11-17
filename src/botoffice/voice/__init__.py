"""Optional local speech: speech-to-text (faster-whisper) and text-to-speech (Kokoro).

The controller never imports the engine packages (numpy, ctranslate2, onnxruntime).
Each engine runs in its own worker process started with the separate voice venv's
interpreter (installed by scripts/install_voice.sh), lazily on first use, and is shut
down after an idle period so its RAM is really returned. Everything stays on this host.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..config import Settings
from .engine import VoiceBusy, VoiceError, VoiceFailed, VoiceTimeout, WorkerEngine
from .wav import STT_RATE, write_pcm16_mono

__all__ = ["VOICES", "SpeakRequest", "SpeakableRequest", "VoiceBusy", "VoiceError", "VoiceFailed", "VoicePrefs", "VoiceService", "VoiceTimeout"]

WORKER = Path(__file__).with_name("worker.py")
STT_TIMEOUT = 90.0
TTS_TIMEOUT = 60.0
TTS_CACHE_ENTRIES = 64
TTS_CACHE_BYTES = 64 * 1024 * 1024
KOKORO_FILES = {"fp32": "kokoro-v1.0.onnx", "int8": "kokoro-v1.0.int8.onnx"}
KOKORO_VOICES = "voices-v1.0.bin"
PROBE_TTL = 300.0
PROBE_TIMEOUT = 20.0
MAX_TRANSCRIPT = 10_000
# The worker's built-in fake engine runs only when this is "1" in the environment; only
# the test suite sets it. The controller passes it on to the worker for fake services.
FAKE_ENV = "BOT_OFFICE_VOICE_FAKE"

# Stock Kokoro-82M v1.0 voicepacks only (Apache-2.0); ids are keys inside voices-v1.0.bin,
# never paths. Labels are descriptive; no voice imitates a real person.
VOICES: dict[str, tuple[str, str]] = {
    "bm_george": ("George - calm British male (default)", "C"),
    "bm_fable": ("Fable - British male", "C"),
    "bm_lewis": ("Lewis - deeper British male", "D+"),
    "bm_daniel": ("Daniel - British male", "D"),
    "bf_emma": ("Emma - British female", "B-"),
    "am_michael": ("Michael - US male", "C+"),
    "af_heart": ("Heart - US female", "A"),
}
DEFAULT_VOICE = "bm_george"
VoiceId = Literal["bm_george", "bm_fable", "bm_lewis", "bm_daniel", "bf_emma", "am_michael", "af_heart"]
PREFS_KEY = "voice_prefs"


class VoicePrefs(BaseModel):
    auto_read: bool = False
    voice: VoiceId = DEFAULT_VOICE
    speed: float = Field(default=1.0, ge=0.8, le=1.2)
    # opt-in fallback: browser dictation (Chrome sends the audio to Google)
    browser_stt: bool = False


class VoicePrefsUpdate(BaseModel):
    auto_read: bool | None = None
    voice: VoiceId | None = None
    speed: float | None = Field(default=None, ge=0.8, le=1.2)
    browser_stt: bool | None = None


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    voice: VoiceId = DEFAULT_VOICE
    speed: float = Field(default=1.0, ge=0.8, le=1.2)


class SpeakableRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


class VoiceService:
    """Status probing plus one lazily started worker per engine."""

    def __init__(self, settings: Settings, *, fake: bool = False, clock=time.monotonic):
        if fake and os.environ.get(FAKE_ENV) != "1":
            raise RuntimeError(f"the fake voice engine is for tests only ({FAKE_ENV}=1)")
        self.settings = settings
        self.fake = fake  # tests only: the worker's built-in fake engine, same process protocol
        worker_env = {FAKE_ENV: "1"} if fake else None
        home = settings.voice_dir / "home"
        self.clock = clock
        self._probe: tuple[float, dict] | None = None
        python = str(settings.voice_interpreter)
        logs = settings.data_dir / "logs"
        stt_args = [python, "-I", str(WORKER), "--engine", "stt"]
        stt_args += ["--fake"] if fake else ["--model-dir", str(self.stt_dir)]
        self.stt = WorkerEngine("speech-to-text", stt_args, timeout=STT_TIMEOUT,
                                log_path=logs / "voice-stt.log", home=home, env=worker_env, clock=clock)
        tts_args = [python, "-I", str(WORKER), "--engine", "tts"]
        tts_args += ["--fake"] if fake else ["--model-path", str(self.tts_model_path),
                                             "--voices-path", str(self.tts_voices_path)]
        self.tts = WorkerEngine("text-to-speech", tts_args, timeout=TTS_TIMEOUT,
                                log_path=logs / "voice-tts.log", home=home, env=worker_env, clock=clock)
        self._voice_keys: tuple[tuple[int, int], frozenset[str]] | None = None
        self._cache: OrderedDict[tuple, bytes] = OrderedDict()
        self._cache_bytes = 0

    @property
    def stt_dir(self) -> Path:
        return self.settings.voice_dir / "whisper" / self.settings.stt_model

    @property
    def tts_model_path(self) -> Path:
        return self.settings.voice_dir / "kokoro" / KOKORO_FILES[self.settings.tts_model]

    @property
    def tts_voices_path(self) -> Path:
        return self.settings.voice_dir / "kokoro" / KOKORO_VOICES

    def installed_voices(self) -> frozenset[str]:
        """Voice ids present in voices-v1.0.bin (an npz archive), cached by size and mtime."""
        if self.fake:
            return frozenset(VOICES)
        try:
            stat = self.tts_voices_path.stat()
            key = (stat.st_size, stat.st_mtime_ns)
            if self._voice_keys and self._voice_keys[0] == key:
                return self._voice_keys[1]
            names: frozenset[str] = frozenset()
            # only parse the archive in this process once it matches the installed hash
            if self._manifest_sha256(f"kokoro/{KOKORO_VOICES}") == _sha256(self.tts_voices_path):
                with zipfile.ZipFile(self.tts_voices_path) as archive:
                    names = frozenset(name.removesuffix(".npy") for name in archive.namelist())
        except (OSError, zipfile.BadZipFile):
            return frozenset()
        self._voice_keys = (key, names)
        return names

    def _manifest_sha256(self, rel: str) -> str | None:
        """The sha256 the installer verified and recorded for *rel*, if any."""
        try:
            data = json.loads((self.settings.voice_dir / "MANIFEST.json").read_text(encoding="utf-8"))
            digest = data["files"][rel]["sha256"]
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return digest if isinstance(digest, str) and len(digest) == 64 else None

    def _run_probe(self) -> dict:
        python = self.settings.voice_interpreter
        if not python.exists():
            return {"error": "voice environment not installed"}
        try:
            out = subprocess.run(
                [str(python), "-I", str(WORKER), "--probe"], capture_output=True, timeout=PROBE_TIMEOUT,
                cwd="/", env={"PATH": "/usr/bin:/bin"}, check=False,
            )
            return json.loads(out.stdout) if out.returncode == 0 else {"error": "voice environment probe failed"}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return {"error": "voice environment probe failed"}

    async def probe(self) -> dict:
        """Which engine packages import in the voice venv (no model is loaded); cached."""
        if self._probe and self.clock() - self._probe[0] < PROBE_TTL:
            return self._probe[1]
        result = {"stt": {"importable": True}, "tts": {"importable": True}} if self.fake else await asyncio.to_thread(self._run_probe)
        self._probe = (self.clock(), result)
        return result

    def invalidate(self) -> None:
        self._probe = None

    async def stt_status(self) -> dict:
        info = {"available": False, "engine": "faster-whisper", "model": self.settings.stt_model,
                "loaded": self.stt.loaded, "reason": None}
        if self.settings.voice_mode == "off":
            info["reason"] = "voice is turned off (BOT_OFFICE_VOICE=off)"
            return info
        if not self.fake and not (self.stt_dir / "model.bin").is_file():
            info["reason"] = "speech-to-text is not installed - run scripts/install_voice.sh"
            return info
        probe = await self.probe()
        if not probe.get("stt", {}).get("importable"):
            info["reason"] = probe.get("error") or "speech-to-text packages are missing in the voice environment"
            return info
        info["available"] = True
        return info

    async def tts_status(self) -> dict:
        info = {"available": False, "engine": "kokoro", "model": self.settings.tts_model,
                "loaded": self.tts.loaded, "reason": None}
        if self.settings.voice_mode == "off":
            info["reason"] = "voice is turned off (BOT_OFFICE_VOICE=off)"
            return info
        if not self.fake and not (self.tts_model_path.is_file() and self.installed_voices()):
            info["reason"] = "text-to-speech is not installed - run scripts/install_voice.sh"
            return info
        probe = await self.probe()
        if not probe.get("tts", {}).get("importable"):
            info["reason"] = probe.get("error") or "text-to-speech packages are missing in the voice environment"
            return info
        info["available"] = True
        return info

    def voices(self) -> list[dict]:
        installed = self.installed_voices()
        return [{"id": voice_id, "label": label, "grade": grade, "installed": voice_id in installed}
                for voice_id, (label, grade) in VOICES.items()]

    async def status(self) -> dict:
        return {"stt": await self.stt_status(), "tts": await self.tts_status(), "voices": self.voices()}

    async def transcribe(self, pcm: bytes) -> dict:
        if not (await self.stt_status())["available"]:
            raise VoiceError("speech-to-text is not installed")
        started = self.clock()
        try:
            reply, _ = await self.stt.request({"op": "transcribe", "rate": STT_RATE}, pcm)
        except VoiceFailed:
            self.invalidate()
            raise
        text = " ".join(str(reply.get("text", "")).split())[:MAX_TRANSCRIPT]
        return {"text": text, "duration_s": round(len(pcm) / 2 / STT_RATE, 2),
                "elapsed_ms": int((self.clock() - started) * 1000), "model": self.settings.stt_model}

    async def speak(self, text: str, voice: str, speed: float) -> bytes:
        """WAV bytes for one chunk of plain text; recent results are kept in a small LRU."""
        if voice not in VOICES:
            raise VoiceError("unknown voice")
        key = (self.settings.tts_model, voice, round(speed, 2), text)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        if not (await self.tts_status())["available"]:
            raise VoiceError("text-to-speech is not installed")
        if voice not in self.installed_voices():
            raise VoiceError("that voice is not installed")
        try:
            reply, pcm = await self.tts.request({"op": "speak", "text": text, "voice": voice, "speed": speed})
        except VoiceFailed:
            self.invalidate()
            raise
        wav = write_pcm16_mono(pcm, int(reply.get("rate", 24_000)))
        self._remember(key, wav)
        return wav

    def _remember(self, key: tuple, wav: bytes) -> None:
        if len(wav) > TTS_CACHE_BYTES:
            return
        self._cache[key] = wav
        self._cache_bytes += len(wav)
        while len(self._cache) > TTS_CACHE_ENTRIES or self._cache_bytes > TTS_CACHE_BYTES:
            _, dropped = self._cache.popitem(last=False)
            self._cache_bytes -= len(dropped)

    async def reap_idle(self) -> None:
        await self.stt.reap_if_idle(self.settings.voice_idle_seconds)
        await self.tts.reap_if_idle(self.settings.voice_idle_seconds)

    async def close(self) -> None:
        await self.stt.stop()
        await self.tts.stop()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
