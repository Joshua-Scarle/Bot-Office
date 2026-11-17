"""Voice worker: one speech engine in its own process, run by the voice venv's interpreter.

This file is executed as a script (``<voice-venv>/bin/python -I worker.py ...``), never
imported by the controller, and depends only on the standard library plus the engine
packages installed by scripts/install_voice.sh. The controller talks to it over
stdin/stdout with length-prefixed frames:

    request : u32 header length, JSON header, u32 payload length, payload bytes
    response: the same; the header has "ok" and either results or "error"

Audio and text are never logged. ``--probe`` only checks that the engine packages are
importable (no model is loaded) and exits.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import struct
import sys
import time

MAX_FRAME = 16 * 1024 * 1024
PACKAGES = {"stt": ("numpy", "faster_whisper"), "tts": ("numpy", "kokoro_onnx")}
STT_THREADS = 4
TTS_THREADS = 3
TTS_RATE = 24_000


def probe() -> dict:
    result = {}
    for kind, names in PACKAGES.items():
        missing = [name for name in names if importlib.util.find_spec(name) is None]
        result[kind] = {"importable": not missing, "missing": missing}
    result["python"] = list(sys.version_info[:3])
    return result


def read_exact(stream, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def read_frame(stream) -> tuple[dict, bytes]:
    (size,) = struct.unpack(">I", read_exact(stream, 4))
    if size > MAX_FRAME:
        raise ValueError("frame too large")
    header = json.loads(read_exact(stream, size))
    (size,) = struct.unpack(">I", read_exact(stream, 4))
    if size > MAX_FRAME:
        raise ValueError("frame too large")
    return header, read_exact(stream, size)


def write_frame(stream, header: dict, payload: bytes = b"") -> None:
    raw = json.dumps(header).encode()
    stream.write(struct.pack(">I", len(raw)) + raw + struct.pack(">I", len(payload)) + payload)
    stream.flush()


class SpeechToText:
    def __init__(self, model_dir: str):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(model_dir, device="cpu", compute_type="int8", cpu_threads=STT_THREADS,
                                  num_workers=1, local_files_only=True)

    def handle(self, header: dict, payload: bytes) -> tuple[dict, bytes]:
        import numpy as np

        audio = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(audio, language="en", beam_size=1, vad_filter=True,
                                            condition_on_previous_text=False)
        return {"text": " ".join(segment.text.strip() for segment in segments).strip()}, b""


class TextToSpeech:
    def __init__(self, model_path: str, voices_path: str):
        import onnxruntime
        from kokoro_onnx import Kokoro

        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = TTS_THREADS
        options.inter_op_num_threads = 1
        session = onnxruntime.InferenceSession(model_path, sess_options=options,
                                               providers=["CPUExecutionProvider"])
        self.model = Kokoro.from_session(session, voices_path)

    def handle(self, header: dict, payload: bytes) -> tuple[dict, bytes]:
        import numpy as np

        voice = header["voice"]
        lang = "en-gb" if voice[:1] == "b" else "en-us"
        samples, rate = self.model.create(header["text"], voice=voice, speed=float(header["speed"]), lang=lang)
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        return {"rate": int(rate)}, pcm


class Fake:
    """Test double with the real process protocol (no models). Never used in production."""

    def __init__(self, kind: str):
        self.kind = kind

    def handle(self, header: dict, payload: bytes) -> tuple[dict, bytes]:
        if payload.startswith(b"HANG") or header.get("text") == "HANG":
            time.sleep(3600)
        if payload.startswith(b"CRASH") or header.get("text") == "CRASH":
            os._exit(3)
        if self.kind == "stt":
            return {"text": f"fake transcript of {len(payload) // 2} samples"}, b""
        return {"rate": TTS_RATE}, b"\0\0" * (TTS_RATE // 10)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--engine", choices=("stt", "tts"))
    parser.add_argument("--fake", action="store_true")
    parser.add_argument("--model-dir")
    parser.add_argument("--model-path")
    parser.add_argument("--voices-path")
    args = parser.parse_args()
    if args.probe:
        print(json.dumps(probe()))
        return 0
    # Keep the protocol pipe private: anything a library prints goes to stderr instead.
    protocol = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    stdin = sys.stdin.buffer
    with __import__("contextlib").suppress(OSError):
        os.nice(10)
    started = time.monotonic()
    try:
        if args.fake:
            if os.environ.get("BOT_OFFICE_VOICE_FAKE") != "1":
                raise RuntimeError("the fake engine is for tests only")
            engine = Fake(args.engine)
        elif args.engine == "stt":
            engine = SpeechToText(args.model_dir)
        else:
            engine = TextToSpeech(args.model_path, args.voices_path)
    except Exception as exc:  # noqa: BLE001 - reported to the controller, then exit
        write_frame(protocol, {"ok": False, "error": f"load failed: {type(exc).__name__}: {exc}"[:500]})
        return 2
    write_frame(protocol, {"ok": True, "ready": True, "load_ms": int((time.monotonic() - started) * 1000)})
    while True:
        try:
            header, payload = read_frame(stdin)
        except EOFError:
            return 0
        started = time.monotonic()
        try:
            result, data = engine.handle(header, payload)
            result.update(ok=True, elapsed_ms=int((time.monotonic() - started) * 1000))
        except Exception as exc:  # noqa: BLE001 - one bad request must not kill the engine
            result, data = {"ok": False, "error": type(exc).__name__}, b""
        write_frame(protocol, result, data)


if __name__ == "__main__":
    sys.exit(main())
