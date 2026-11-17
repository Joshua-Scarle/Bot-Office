import struct
import sys
from dataclasses import replace

from fastapi.testclient import TestClient

from botoffice.api import create_app
from botoffice.providers.fake import FakeAdapter
from botoffice.voice import VoiceService

H = {"Authorization": "Bearer test-token"}
WAV_H = {**H, "Content-Type": "audio/wav"}


def wav(pcm: bytes, rate: int = 16000) -> bytes:
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    body = b"WAVE" + fmt + b"data" + struct.pack("<I", len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", len(body)) + body


def app_with_voice(settings, **kwargs):
    voice_settings = replace(settings, voice_python=sys.executable)
    voice = VoiceService(voice_settings, fake=True)
    app = create_app(voice_settings, {"codex": FakeAdapter()}, voice=voice)
    return app, voice


def test_voice_routes_require_the_master_token(settings):
    app, _ = app_with_voice(settings)
    with TestClient(app) as client:
        assert client.get("/v1/voice").status_code == 401
        assert client.post("/v1/voice/transcribe", content=wav(b"\0\0" * 16000)).status_code == 401
        assert client.put("/v1/voice/prefs", json={"auto_read": True}).status_code == 401


def test_unavailable_voice_is_503_and_reported(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        status = client.get("/v1/voice", headers=H).json()
        assert status["stt"]["available"] is False and status["stt"]["reason"]
        response = client.post("/v1/voice/transcribe", content=wav(b"\0\0" * 16000), headers=WAV_H)
        assert response.status_code == 503 and response.json()["detail"] == "speech-to-text is not installed"


def test_transcribe_validation_and_success(settings):
    app, _ = app_with_voice(settings)
    with TestClient(app) as client:
        assert client.get("/v1/voice", headers=H).json()["stt"]["available"] is True
        good = wav(b"\0\0" * 16000)
        assert client.post("/v1/voice/transcribe", content=good, headers={**H, "Content-Type": "audio/webm"}).status_code == 415
        assert client.post("/v1/voice/transcribe", content=wav(b"\0\0" * 44100, rate=44100), headers=WAV_H).status_code == 415
        assert client.post("/v1/voice/transcribe", content=wav(b"\0\0" * 1000), headers=WAV_H).status_code == 422
        big = wav(b"\0\0" * 16000 * 121)
        assert client.post("/v1/voice/transcribe", content=big, headers=WAV_H).status_code == 422
        huge = b"\0" * (4 * 1024 * 1024 + 1)
        assert client.post("/v1/voice/transcribe", content=huge, headers=WAV_H).status_code == 413

        def chunks():
            for _ in range(5):
                yield b"\0" * (1024 * 1024)

        streamed = client.post("/v1/voice/transcribe", content=chunks(), headers=WAV_H)
        assert streamed.status_code == 413
        response = client.post("/v1/voice/transcribe", content=good, headers=WAV_H)
        assert response.status_code == 200
        body = response.json()
        assert body["text"] == "fake transcript of 16000 samples" and body["duration_s"] == 1.0


def test_transcribe_rate_limit_and_busy_and_timeout(settings):
    app, voice = app_with_voice(settings)
    with TestClient(app) as client:
        voice.stt.timeout = 1.0
        hang = wav(b"HANG" + b"\0\0" * 16000)
        response = client.post("/v1/voice/transcribe", content=hang, headers=WAV_H)
        assert response.status_code == 504
        voice.stt._inflight = 3  # one active plus two queued
        assert client.post("/v1/voice/transcribe", content=wav(b"\0\0" * 16000), headers=WAV_H).status_code == 429
        voice.stt._inflight = 0
        codes = [client.post("/v1/voice/transcribe", content=wav(b"\0\0" * 16000), headers=WAV_H).status_code
                 for _ in range(20)]
        assert codes[-1] == 429 and codes[0] == 200


def test_prefs_default_off_and_round_trip(settings):
    app, _ = app_with_voice(settings)
    with TestClient(app) as client:
        prefs = client.get("/v1/voice", headers=H).json()["prefs"]
        assert prefs == {"auto_read": False, "voice": "bm_george", "speed": 1.0, "browser_stt": False}
        updated = client.put("/v1/voice/prefs", json={"auto_read": True}, headers=H).json()
        assert updated["auto_read"] is True and updated["voice"] == "bm_george"
        assert client.put("/v1/voice/prefs", json={"voice": "../etc/passwd"}, headers=H).status_code == 422
        assert client.put("/v1/voice/prefs", json={"speed": 2}, headers=H).status_code == 422
        assert client.get("/v1/voice", headers=H).json()["prefs"]["auto_read"] is True
        assert client.get("/v1/voice/prefs", headers=H).json()["auto_read"] is True
        assert client.get("/v1/voice/prefs").status_code == 401


def test_page_csp_allows_blob_media_and_microphone_only(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        page = client.get("/")
        csp = page.headers["content-security-policy"]
        assert "media-src blob:" in csp and "unsafe-inline" not in csp
        assert page.headers["permissions-policy"] == "microphone=(self), camera=(), geolocation=()"


def test_speak_and_speakable(settings):
    app, voice = app_with_voice(settings)
    with TestClient(app) as client:
        status = client.get("/v1/voice", headers=H).json()
        assert status["tts"]["available"] is True
        assert status["voices"][0]["id"] == "bm_george"
        chunks = client.post("/v1/voice/speakable", json={"text": "# Hi\n```\ncode\n```"}, headers=H).json()
        assert chunks == {"chunks": ["Hi. (code block omitted)"]}
        response = client.post("/v1/voice/speak", json={"text": "Hello.", "voice": "bf_emma", "speed": 1.1}, headers=H)
        assert response.status_code == 200 and response.content[:4] == b"RIFF"
        assert response.headers["content-type"] == "audio/wav"
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-security-policy"] == "sandbox"
        assert client.post("/v1/voice/speak", json={"text": "x", "voice": "bm_george"}).status_code == 401
        for bad in ({"text": ""}, {"text": "x" * 1001}, {"text": "x", "voice": "../x"}, {"text": "x", "speed": 3}):
            assert client.post("/v1/voice/speak", json=bad, headers=H).status_code == 422
        voice.tts.timeout = 1.0
        assert client.post("/v1/voice/speak", json={"text": "HANG"}, headers=H).status_code == 504


def test_speak_unavailable_is_503(settings):
    with TestClient(create_app(settings, {"codex": FakeAdapter()})) as client:
        status = client.get("/v1/voice", headers=H).json()
        assert status["tts"]["available"] is False
        assert all(not v["installed"] for v in status["voices"])
        assert client.post("/v1/voice/speak", json={"text": "Hello."}, headers=H).status_code == 503


def test_slow_upload_times_out_and_invalid_uploads_keep_the_budget(settings, monkeypatch):
    import time

    monkeypatch.setattr("botoffice.api.STT_READ_SECONDS", 0.3)
    app, _ = app_with_voice(settings)
    import asyncio

    import httpx

    async def slow_upload():
        async def slow():
            for _ in range(4):
                await asyncio.sleep(0.2)
                yield b"\0" * 1024

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = time.monotonic()
            response = await client.post("/v1/voice/transcribe", content=slow(), headers=WAV_H)
            return response.status_code, time.monotonic() - started

    code, elapsed = asyncio.run(slow_upload())
    assert code == 408 and elapsed < 0.7
    with TestClient(app) as client:
        for _ in range(25):  # malformed and too-short recordings do not use up the 20/min
            assert client.post("/v1/voice/transcribe", content=b"junk", headers=WAV_H).status_code == 415
            assert client.post("/v1/voice/transcribe", content=wav(b"\0\0" * 100), headers=WAV_H).status_code == 422
        assert client.post("/v1/voice/transcribe", content=wav(b"\0\0" * 16000), headers=WAV_H).status_code == 200


def test_speakable_is_rate_limited(settings):
    app, _ = app_with_voice(settings)
    with TestClient(app) as client:
        codes = [client.post("/v1/voice/speakable", json={"text": "Hi."}, headers=H).status_code for _ in range(61)]
        assert codes[:60] == [200] * 60 and codes[60] == 429
