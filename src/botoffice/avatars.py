"""Bot avatars (0.12.0): a deterministic generator and the upload normaliser.

The generator needs no API key, no network and no image library: a seed picks a curated gradient
palette and a geometric motif, and a small ``zlib`` + ``struct`` writer encodes the PNG. The same
seed always gives the same bytes. Uploads are validated, centre-cropped to a square, resized to
256 px and re-encoded as PNG with Pillow (only that part needs it; everything else works without).
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import math
import re
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

SIZE = 256
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_PIXELS = 12_000_000          # PNG, WebP, GIF: decoded at full size
MAX_JPEG_PIXELS = 40_000_000            # a JPEG is decoded at reduced size (draft), so phone photos fit
PRESET_COUNT = 12
SEED_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# (from, to, motif tint, tint strength): hand-picked, readable on the dark UI
PALETTES = (
    ((0x3B, 0x5B, 0xDB), (0x74, 0xC0, 0xFC), (255, 255, 255), 0.30),   # blue
    ((0x0C, 0x85, 0x99), (0x63, 0xE6, 0xBE), (255, 255, 255), 0.28),   # teal
    ((0x2B, 0x8A, 0x3E), (0xC0, 0xEB, 0x75), (255, 255, 255), 0.30),   # green
    ((0xE6, 0x77, 0x00), (0xFF, 0xD4, 0x3B), (255, 255, 255), 0.32),   # amber
    ((0xC9, 0x2A, 0x2A), (0xFF, 0x8C, 0x6B), (255, 255, 255), 0.30),   # coral
    ((0xA6, 0x1E, 0x4D), (0xF7, 0x83, 0xAC), (255, 255, 255), 0.30),   # rose
    ((0x86, 0x3C, 0xC4), (0xDA, 0xA5, 0xF5), (255, 255, 255), 0.30),   # violet
    ((0x36, 0x40, 0x9F), (0x91, 0xA7, 0xFF), (255, 255, 255), 0.28),   # indigo
    ((0x34, 0x3A, 0x40), (0x86, 0x8E, 0x96), (255, 255, 255), 0.26),   # slate
    ((0x08, 0x7F, 0x5B), (0x3B, 0xC9, 0xDB), (255, 255, 255), 0.30),   # sea
    ((0x9C, 0x36, 0xB5), (0xFF, 0x92, 0x6B), (255, 255, 255), 0.30),   # dusk
    ((0x1B, 0x4D, 0x7A), (0xF0, 0xB4, 0x29), (255, 255, 255), 0.28),   # harbor
)
MOTIFS = 6
PRESET_SEEDS = tuple(f"preset-{n}" for n in range(PRESET_COUNT))


class AvatarError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def valid_seed(seed: str) -> bool:
    return isinstance(seed, str) and SEED_RE.fullmatch(seed) is not None


def default_seed(bot_id, used=()) -> str:
    """The default avatar of a bot: one of the curated presets. Among the presets used least by *used*
    (the seeds other bots already have) the one the bot's id hashes to, so a small office gets distinct
    avatars and the choice is stable for the same inputs."""
    counts = {seed: 0 for seed in PRESET_SEEDS}
    for seed in used:
        if seed in counts:
            counts[seed] += 1
    least = min(counts.values())
    pool = [seed for seed in PRESET_SEEDS if counts[seed] == least]
    digest = hashlib.sha256(str(bot_id).encode()).digest()
    return pool[int.from_bytes(digest[:4], "big") % len(pool)]


def _params(seed: str) -> dict:
    h = hashlib.sha256(("botoffice-avatar:" + seed).encode()).digest()
    match = re.fullmatch(r"preset-(\d+)", seed)
    if match and int(match.group(1)) < PRESET_COUNT:
        n = int(match.group(1))
        palette, motif = n, (n * 5 + n // 6) % MOTIFS   # 12 distinct palettes, every motif twice
    else:
        palette, motif = h[0] % len(PALETTES), h[1] % MOTIFS
    return {
        "palette": PALETTES[palette], "motif": motif,
        "angle": (h[2] / 255.0) * math.pi * 2,
        "cx": 0.35 + h[3] / 255.0 * 0.30, "cy": 0.35 + h[4] / 255.0 * 0.30,
        "freq": 3 + h[5] % 4, "dark": h[6] % 5 == 0,
    }


def _smooth(edge: float, width: float) -> float:
    """1 inside (edge <= -width), 0 outside (edge >= width), linear across the 1-px border."""
    if edge <= -width:
        return 1.0
    if edge >= width:
        return 0.0
    return 0.5 - edge / (2 * width)


def _coverage(motif: int, u: float, v: float, p: dict, px: float) -> float:
    cx, cy, freq = p["cx"], p["cy"], p["freq"]
    if motif == 0:  # concentric rings
        d = math.hypot(u - cx, v - cy) * freq * 1.6
        k = abs((d % 1.0) - 0.5)           # 0 mid-band .. .5 at the gap
        return _smooth(k - 0.22, px * freq * 1.6)
    if motif == 1:  # two overlapping circles
        a = _smooth(math.hypot(u - cx, v - cy) - 0.27, px)
        b = _smooth(math.hypot(u - (1 - cx), v - (1 - cy)) - 0.13, px)
        return max(a, b)
    if motif == 2:  # diagonal bands
        t = (u * math.cos(p["angle"]) + v * math.sin(p["angle"])) * freq * 1.4
        return _smooth(abs((t % 1.0) - 0.5) - 0.17, px * freq * 1.4)
    if motif == 3:  # dot grid
        n = 3 + freq % 3
        fu, fv = (u * n) % 1.0 - 0.5, (v * n) % 1.0 - 0.5
        return _smooth((math.hypot(fu, fv) - 0.28) / n, px)
    if motif == 4:  # crescent
        a = math.hypot(u - cx, v - cy) - 0.30
        b = math.hypot(u - cx - 0.12, v - cy - 0.06) - 0.26
        return _smooth(max(a, -b), px)
    # facets: a diamond with a diamond cut out of it
    d = abs(u - cx) + abs(v - cy)
    return _smooth(max(d - 0.34, -(d - 0.16)), px)


def _png(rows: list[bytes], size: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    raw = b"".join(b"\x00" + row for row in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


# avatar work (CPU-bound) runs on its own two threads, so a burst of previews or uploads never starves the shared pool
EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="avatar")


async def run(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(EXECUTOR, fn, *args)


GENERATOR_VERSION = 1                           # part of every ETag: bump it if the drawing changes
_PRESET_PNGS: dict[str, bytes] = {}             # the 12 defaults are kept for good, whatever other seeds do


def render_png(seed: str) -> bytes:
    """The generated avatar for a seed: a SIZE x SIZE RGB PNG (the UI rounds the corners)."""
    if not valid_seed(seed):
        raise ValueError("invalid avatar seed")
    if seed in PRESET_SEEDS:
        if seed not in _PRESET_PNGS:
            _PRESET_PNGS[seed] = _render(seed)
        return _PRESET_PNGS[seed]
    return _render(seed)


@lru_cache(maxsize=64)
def _render(seed: str) -> bytes:
    p = _params(seed)
    (c1, c2, tint, strength) = p["palette"]
    if p["dark"]:
        tint, strength = (0, 0, 0), strength * 0.7
    ga = math.cos(p["angle"] * 0.5 + 0.6)
    gb = math.sin(p["angle"] * 0.5 + 0.6)
    px = 1.0 / SIZE
    rows: list[bytes] = []
    for y in range(SIZE):
        v = (y + 0.5) / SIZE
        row = bytearray()
        for x in range(SIZE):
            u = (x + 0.5) / SIZE
            t = min(1.0, max(0.0, 0.5 + ((u - 0.5) * ga + (v - 0.5) * gb)))
            cov = _coverage(p["motif"], u, v, p, px) * strength
            for ch in range(3):
                base = c1[ch] + (c2[ch] - c1[ch]) * t
                row.append(int(base + (tint[ch] - base) * cov + 0.5))
        rows.append(bytes(row))
    return _png(rows, SIZE)


def etag(data: bytes) -> str:
    return '"' + hashlib.sha256(bytes([GENERATOR_VERSION]) + data).hexdigest()[:24] + '"'


def normalise_upload(data: bytes) -> bytes:
    """Validate an uploaded PNG, JPEG, WebP or GIF and return a 256 px square PNG.

    The file is sniffed, fully decoded (so a truncated or lying file fails), centre-cropped, resized
    and re-encoded: the stored bytes are ours, with no metadata and nothing appended. SVG and every
    other type are refused. Raises AvatarError (400/413/415/503)."""
    if not data:
        raise AvatarError(400, "icon image is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise AvatarError(413, "icon image is too large (limit 2 MB)")
    head = data[:16]
    is_png = head.startswith(b"\x89PNG\r\n\x1a\n")
    is_jpeg = head.startswith(b"\xff\xd8\xff")
    is_webp = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    is_gif = head[:6] in (b"GIF87a", b"GIF89a")
    if not (is_png or is_jpeg or is_webp or is_gif):
        raise AvatarError(415, "icon must be a PNG, JPEG or WebP image")
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise AvatarError(503, "image uploads need the Pillow package on the server") from exc
    try:
        return _square_png(data, Image, ImageOps)
    except AvatarError:
        raise
    except Exception as exc:  # corrupt, truncated, bomb, unsupported: all the same to the caller
        raise AvatarError(415, "icon is not a valid PNG, JPEG or WebP image") from exc


def _square_png(data: bytes, Image, ImageOps) -> bytes:
    img = Image.open(io.BytesIO(data), formats=["PNG", "JPEG", "WEBP", "GIF"])
    width, height = img.size
    if width < 1 or height < 1 or width * height > (MAX_JPEG_PIXELS if img.format == "JPEG" else MAX_UPLOAD_PIXELS):
        raise AvatarError(413, "icon image has too many pixels")
    img.seek(0)
    if img.format == "JPEG":
        img.draft("RGB", (SIZE * 2, SIZE * 2))   # decode a JPEG at reduced size: much less memory
        if img.size[0] * img.size[1] > MAX_UPLOAD_PIXELS:   # draft cannot shrink a very wide, short JPEG
            raise AvatarError(413, "icon image has too many pixels")
    img.load()
    img = ImageOps.exif_transpose(img) or img
    img = img.convert("RGBA" if "A" in img.getbands() or img.mode == "P" else "RGB")
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((SIZE, SIZE), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
