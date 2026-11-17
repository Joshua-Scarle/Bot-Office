"""Strict PCM WAV parsing and writing (stdlib only).

Uploads are never decoded with ffmpeg/PyAV: the browser decodes and resamples the
recording and sends 16 kHz mono 16-bit PCM WAV; anything else is rejected here.
"""

from __future__ import annotations

import io
import struct
import wave

STT_RATE = 16_000
MAX_CHUNKS = 16
# small metadata chunks (LIST/INFO, fact, ...) are skipped, but only this much of them
MAX_SKIPPED_BYTES = 64 * 1024


class WavError(ValueError):
    pass


def parse_pcm16_mono(data: bytes, rate: int = STT_RATE) -> bytes:
    """Return the PCM samples of a RIFF/WAVE file that is exactly PCM, 1 channel, 16 bit
    at *rate*; raise WavError on any deviation (odd sizes, duplicate/oversized chunks,
    truncation, other formats)."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise WavError("not a RIFF/WAVE file")
    riff_size = struct.unpack_from("<I", data, 4)[0]
    if riff_size + 8 > len(data) or riff_size < 4:
        raise WavError("truncated RIFF")
    end = riff_size + 8
    offset, fmt, pcm, skipped, chunks = 12, None, None, 0, 0
    while offset + 8 <= end:
        chunks += 1
        if chunks > MAX_CHUNKS:
            raise WavError("too many chunks")
        chunk_id = data[offset:offset + 4]
        size = struct.unpack_from("<I", data, offset + 4)[0]
        body_start = offset + 8
        if size > end - body_start:
            raise WavError("chunk length exceeds the file")
        body = data[body_start:body_start + size]
        if chunk_id == b"fmt ":
            if fmt is not None:
                raise WavError("duplicate fmt chunk")
            if size not in (16, 18):
                raise WavError("unsupported fmt chunk")
            fmt = struct.unpack_from("<HHIIHH", body)
        elif chunk_id == b"data":
            if pcm is not None:
                raise WavError("duplicate data chunk")
            if fmt is None:
                raise WavError("data before fmt")
            pcm = body
        else:
            skipped += size
            if skipped > MAX_SKIPPED_BYTES:
                raise WavError("too much non-audio data")
        offset = body_start + size + (size & 1)
    if fmt is None or pcm is None:
        raise WavError("missing fmt or data chunk")
    audio_format, channels, sample_rate, byte_rate, block_align, bits = fmt
    if audio_format != 1:
        raise WavError("only PCM WAV is accepted")
    if channels != 1:
        raise WavError("audio must be mono")
    if bits != 16:
        raise WavError("audio must be 16-bit")
    if sample_rate != rate:
        raise WavError(f"audio must be {rate} Hz")
    if block_align != 2 or byte_rate != rate * 2:
        raise WavError("inconsistent fmt chunk")
    if len(pcm) % 2:
        raise WavError("odd data length")
    return pcm


def duration(pcm: bytes, rate: int = STT_RATE) -> float:
    return len(pcm) / 2 / rate


def write_pcm16_mono(pcm: bytes, rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buffer.getvalue()
