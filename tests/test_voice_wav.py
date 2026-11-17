import struct

import pytest

from botoffice.voice.wav import WavError, parse_pcm16_mono, write_pcm16_mono


def wav(pcm=b"\0\0" * 16000, rate=16000, channels=1, bits=16, fmt=1, extra=b"", data_id=b"data"):
    block = channels * bits // 8
    fmt_chunk = b"fmt " + struct.pack("<IHHIIHH", 16, fmt, channels, rate, rate * block, block, bits)
    body = b"WAVE" + fmt_chunk + extra + data_id + struct.pack("<I", len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_accepts_16k_mono_s16_and_round_trips():
    pcm = struct.pack("<4h", 1, -1, 32767, -32768)
    assert parse_pcm16_mono(wav(pcm)) == pcm
    assert parse_pcm16_mono(write_pcm16_mono(pcm, 16000)) == pcm
    # small metadata chunks are skipped
    assert parse_pcm16_mono(wav(pcm, extra=b"LIST" + struct.pack("<I", 4) + b"INFO")) == pcm


@pytest.mark.parametrize("data", [
    b"not a wav at all",
    wav(channels=2),
    wav(rate=44100),
    wav(bits=8),
    wav(fmt=3),  # float
    wav()[:-100],  # truncated data
    wav(data_id=b"junk"),  # no data chunk
    wav(pcm=b"\0" * 3),  # odd length
    wav(extra=b"LIST" + struct.pack("<I", 10**9)),  # oversized chunk length
    wav(extra=b"LIST" + struct.pack("<I", 70000) + b"\0" * 70000),  # too much non-audio data
    wav(extra=b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)),  # duplicate fmt
])
def test_rejects_anything_else(data):
    with pytest.raises(WavError):
        parse_pcm16_mono(data)
